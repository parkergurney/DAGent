"""Event store: the append-only facts, the one privileged state-write path, and
replay.

Invariants enforced here (design.md section 3):
  1. Only transition() writes tasks.state, and it always emits task.state_changed
     in the SAME transaction.
  4. task.state_changed payloads carry {from, to, cause_seq}.
  3. replay(events) reconstructs the tasks table exactly.

Everything that mutates tasks records enough in the event payload for replay to
rebuild it: task.created carries the full static task definition, and
task.state_changed carries the new state plus any mutated columns.
"""
import json
import os
import time
from datetime import datetime, timezone

# --- ULID -------------------------------------------------------------------
# ponytail: vendored ~15-line ULID (spec: 48-bit ms timestamp + 80-bit random,
# Crockford base32, 26 chars). Swap for the `python-ulid` package only if we
# ever need monotonic-within-ms generation.
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    chars = [_CROCKFORD[(value >> shift) & 0x1F] for shift in range(125, -1, -5)]
    return "".join(chars)


# --- state machine (design.md section 4) ------------------------------------
STATES = {
    "blocked", "queued", "running", "verifying", "triage", "needs_human",
    "delivering", "delivered", "failed", "cancelled",
}
TERMINAL = {"delivered", "failed", "cancelled"}

# Legal (from -> to) edges, excluding the universal "any -> cancelled" which is
# handled in _legal() (captain may kill any non-terminal task).
_TRANSITIONS = {
    ("blocked", "queued"),
    ("queued", "running"),
    ("running", "verifying"),
    ("running", "triage"),
    ("verifying", "delivering"),
    ("verifying", "triage"),
    ("triage", "running"),
    ("triage", "needs_human"),
    ("triage", "failed"),
    ("needs_human", "running"),
    ("needs_human", "delivering"),
    ("delivering", "delivered"),
    ("delivering", "triage"),
}

# Columns transition() may update alongside the state change.
_UPDATABLE = {"session_id", "worktree", "retries", "max_retries", "base_sha"}


def _legal(frm: str, to: str) -> bool:
    if frm in TERMINAL:
        return False  # terminal states are final
    if to == "cancelled":
        return True  # any non-terminal -> cancelled
    return (frm, to) in _TRANSITIONS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_event(conn, ts, *, source, type, task_id=None, payload=None,
                  session_id=None, tokens_in=None, tokens_out=None, cost_usd=None):
    cur = conn.execute(
        "INSERT INTO events "
        "(ts, task_id, source, type, payload, session_id, tokens_in, tokens_out, cost_usd) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (ts, task_id, source, type, json.dumps(payload or {}),
         session_id, tokens_in, tokens_out, cost_usd),
    )
    return cur.lastrowid


def append_event(conn, *, source, type, task_id=None, payload=None,
                 session_id=None, tokens_in=None, tokens_out=None, cost_usd=None) -> int:
    """Append one fact to the event log. Returns its seq.

    This is the generic path for events that do NOT change task state
    (worker.*, verify.*, supervisor.*, etc). State changes go through
    transition().
    """
    with conn:
        return _insert_event(
            conn, _now(), source=source, type=type, task_id=task_id, payload=payload,
            session_id=session_id, tokens_in=tokens_in, tokens_out=tokens_out,
            cost_usd=cost_usd,
        )


def create_task(conn, *, title, brief, repo, delivery_mode, verify_cmd=None,
                max_retries=2, depends_on=(), task_id=None) -> str:
    """Insert a task (state='blocked') and emit task.created atomically.

    The task.created payload carries the full static definition so replay can
    rebuild the row from events alone.
    """
    task_id = task_id or ulid()
    ts = _now()
    payload = {
        "title": title,
        "brief": brief,
        "repo": repo,
        "delivery_mode": delivery_mode,
        "verify_cmd": verify_cmd,
        "max_retries": max_retries,
        "depends_on": list(depends_on),
    }
    with conn:
        conn.execute(
            "INSERT INTO tasks "
            "(id, title, brief, repo, delivery_mode, verify_cmd, state, retries, "
            " max_retries, worktree, session_id, base_sha, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,'blocked',0,?,NULL,NULL,NULL,?,?)",
            (task_id, title, brief, repo, delivery_mode, verify_cmd, max_retries, ts, ts),
        )
        for dep in depends_on:
            conn.execute(
                "INSERT INTO task_deps (task_id, depends_on) VALUES (?,?)",
                (task_id, dep),
            )
        _insert_event(conn, ts, source="scheduler", type="task.created",
                      task_id=task_id, payload=payload)
    return task_id


def transition(conn, task_id, to_state, *, cause_seq, source="scheduler", **fields) -> int:
    """The one privileged state-write path.

    Reads the current state, rejects illegal transitions, then updates
    tasks.state (+ updated_at + any updatable **fields) and emits
    task.state_changed with {from, to, cause_seq} plus the mutated columns, all
    in one transaction. Returns the seq of the state_changed event.
    """
    if to_state not in STATES:
        raise ValueError(f"unknown state {to_state!r}")
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"cannot set {bad} via transition (allowed: {_UPDATABLE})")

    row = conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown task {task_id!r}")
    frm = row["state"]
    if not _legal(frm, to_state):
        raise ValueError(f"illegal transition {frm} -> {to_state} for task {task_id}")

    ts = _now()
    payload = {"from": frm, "to": to_state, "cause_seq": cause_seq, **fields}
    with conn:
        cols = ", ".join(f"{c} = ?" for c in fields)
        sql = "UPDATE tasks SET state = ?, updated_at = ?"
        params = [to_state, ts]
        if cols:
            sql += ", " + cols
            params += [fields[c] for c in fields]
        sql += " WHERE id = ?"
        params.append(task_id)
        conn.execute(sql, params)
        return _insert_event(conn, ts, source=source, type="task.state_changed",
                             task_id=task_id, payload=payload)


def replay(events) -> dict:
    """Fold the event stream into the derived tasks-table state.

    Pure function: takes an iterable of event rows (sqlite3.Row or any mapping
    with seq/ts/task_id/type/payload), ordered by seq, and returns
    {task_id: {column: value}} mirroring the tasks table. This is invariant 3 --
    replay(all events) must equal the live tasks table.

    task_deps is intentionally NOT rebuilt here; the invariant is over the tasks
    table only. Deps ride in the task.created payload if a future milestone wants
    to rebuild them too.
    """
    tasks: dict = {}
    for e in events:
        payload = json.loads(e["payload"])
        if e["type"] == "task.created":
            tasks[e["task_id"]] = {
                "id": e["task_id"],
                "title": payload["title"],
                "brief": payload["brief"],
                "repo": payload["repo"],
                "delivery_mode": payload["delivery_mode"],
                "verify_cmd": payload.get("verify_cmd"),
                "state": "blocked",
                "retries": 0,
                "max_retries": payload.get("max_retries", 2),
                "worktree": None,
                "session_id": None,
                "base_sha": None,
                "created_at": e["ts"],
                "updated_at": e["ts"],
            }
        elif e["type"] == "task.state_changed":
            t = tasks[e["task_id"]]
            t["state"] = payload["to"]
            t["updated_at"] = e["ts"]
            for col in _UPDATABLE:
                if col in payload:
                    t[col] = payload[col]
    return tasks
