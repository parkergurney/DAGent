"""Crash recovery (see README.md): "a reconciliation pass at startup:
for every task in running, check whether session_id is a live session; dead
ones get a synthetic worker.exited event and route through triage like any
other crash. No special recovery code path."

reconcile() does exactly that and nothing more -- it does not resolve triage
itself. Whatever normally drains triage (see scheduler.core) picks these
tasks up the same way it would a live crash.
"""
import os
import subprocess

from dagent import execution_lease
from dagent.store import append_event, transition, update_attempt


def _pid_alive(session_id) -> bool:
    try:
        pid = int(session_id)
    except (TypeError, ValueError):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours


def reconcile(conn) -> None:
    rows = conn.execute(
        "SELECT id, state, session_id, current_attempt_id, repo, worktree, candidate_branch FROM tasks "
        "WHERE state IN ('running', 'verifying')"
    ).fetchall()
    for row in rows:
        if row["state"] == "running" and _pid_alive(row["session_id"]):
            continue
        if row["state"] == "verifying":
            _recover_orphaned_lease(conn, row, reason="reconciled_verifying")
            _close_orphaned_slot(conn, row)
            continue

        done = conn.execute(
            "SELECT seq, payload FROM events WHERE task_id = ? AND type = 'worker.done_claimed' "
            "AND (json_extract(payload, '$.attempt_id') = ? OR "
            "json_extract(payload, '$.attempt_id') IS NULL) "
            "ORDER BY seq DESC LIMIT 1", (row["id"], row["current_attempt_id"])
        ).fetchone()
        if done:
            _recover_orphaned_lease(conn, row, reason="reconciled_done_claim")
            _close_orphaned_slot(conn, row)
            candidate = conn.execute(
                "SELECT candidate_sha FROM attempts WHERE id = ?", (row["current_attempt_id"],)
            ).fetchone()
            transition(conn, row["id"], "verifying", cause_seq=done["seq"],
                       **({"candidate_sha": candidate["candidate_sha"]}
                          if candidate and candidate["candidate_sha"] else {}))
            continue

        _recover_orphaned_lease(conn, row, reason="reconciled_worker_exit")
        s = append_event(conn, source="system", type="worker.exited", task_id=row["id"],
                         session_id=row["session_id"],
                         payload={"attempt_id": row["current_attempt_id"],
                                  "reason": "reconciled: session not alive"})
        candidate = None
        worker_dirty = None
        if row["candidate_branch"]:
            proc = subprocess.run(["git", "rev-parse", row["candidate_branch"]],
                                  cwd=row["repo"], capture_output=True, text=True)
            if proc.returncode == 0:
                candidate = proc.stdout.strip()
        if row["worktree"]:
            try:
                status = subprocess.run(["git", "status", "--porcelain"], cwd=row["worktree"],
                                        capture_output=True, text=True)
            except OSError:
                status = None
            if status and status.returncode == 0 and status.stdout.strip():
                worker_dirty = status.stdout
        if row["current_attempt_id"]:
            update_attempt(conn, row["current_attempt_id"], worker_ended_at=_now(),
                           candidate_sha=candidate,
                           worker_dirty=worker_dirty,
                           failure_cause="worker.exited", disposition="interrupted")
        transition(conn, row["id"], "triage", cause_seq=s,
                   candidate_sha=candidate) if candidate else transition(
                       conn, row["id"], "triage", cause_seq=s)
        _close_orphaned_slot(conn, row)


def _recover_orphaned_lease(conn, row, *, reason: str) -> None:
    """Fence the lease owned by a process that the restarted daemon cannot observe."""
    if not row["current_attempt_id"]:
        return
    lease = conn.execute(
        "SELECT * FROM execution_leases WHERE attempt_id = ? AND status = 'active'",
        (row["current_attempt_id"],),
    ).fetchone()
    if lease is None:
        return
    execution_lease.recover(
        conn, row["current_attempt_id"], lease["lease_id"], lease["generation"],
        reason=reason, source="system",
    )


def _close_orphaned_slot(conn, row) -> None:
    if not row["current_attempt_id"]:
        return
    slot_open = conn.execute(
        "SELECT 1 FROM events WHERE task_id = ? "
        "AND type = 'worker.slot_acquired' "
        "AND json_extract(payload, '$.attempt_id') = ? "
        "AND NOT EXISTS (SELECT 1 FROM events released "
        "WHERE released.task_id = events.task_id "
        "AND released.type = 'worker.slot_released' "
        "AND json_extract(released.payload, '$.attempt_id') = "
        "json_extract(events.payload, '$.attempt_id')) LIMIT 1",
        (row["id"], row["current_attempt_id"]),
    ).fetchone()
    if not slot_open:
        return
    attempt = conn.execute(
        "SELECT run_id FROM attempts WHERE id = ?", (row["current_attempt_id"],)
    ).fetchone()
    append_event(
        conn, source="system", type="worker.slot_released", task_id=row["id"],
        payload={"attempt_id": row["current_attempt_id"], "reason": "reconciled",
                 "reconciled": True, "occupancy": 0,
                 "run_id": attempt["run_id"] if attempt else None},
    )


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
