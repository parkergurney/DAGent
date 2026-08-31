"""Builds a TriagePacket from event history (design.md section 6). Pure
read: no side effects, no LLM call -- keeps the packet reproducible, which is
what makes the dump/replay tooling in supervisor/llm.py meaningful.
"""
import json

from dagent.supervisor.actions import compute_allowed_actions
from dagent.supervisor.schema import EventRow, TriagePacket, TriggerEvent


def _compact_history(rows: list[dict]) -> list[EventRow]:
    """Collapse consecutive worker.tool_used rows into one counted summary
    ("47 tool calls: 31 Read, 9 Edit, 7 Bash"); keep state changes,
    questions, and supervisor actions verbatim, per design.md section 6."""
    out: list[EventRow] = []
    run: list[dict] = []

    def flush():
        if not run:
            return
        counts: dict[str, int] = {}
        for r in run:
            tool = json.loads(r["payload"]).get("tool", "?")
            counts[tool] = counts.get(tool, 0) + 1
        parts = ", ".join(f"{n} {name}" for name, n in sorted(counts.items(), key=lambda kv: -kv[1]))
        out.append(EventRow(seq=run[-1]["seq"], type="worker.tool_used", source="worker",
                            summary=f"{len(run)} tool calls: {parts}"))
        run.clear()

    for r in rows:
        if r["type"] == "worker.tool_used":
            run.append(r)
            continue
        flush()
        payload = json.loads(r["payload"])
        if r["type"] == "worker.messaged":
            summary = (payload.get("text") or "")[:200]
        elif r["type"] == "worker.asked":
            summary = f"asked: {payload.get('question', '')}"
        elif r["type"] == "supervisor.acted":
            summary = f"supervisor {payload.get('action')}: {payload.get('reason', '')}"
        elif r["type"] == "task.state_changed":
            summary = f"{payload.get('from')} -> {payload.get('to')}"
        else:
            summary = json.dumps(payload)[:200]
        out.append(EventRow(seq=r["seq"], type=r["type"], source=r["source"], summary=summary))
    flush()
    return out


def _transcript_tail(rows: list[dict], char_budget: int) -> str:
    lines = []
    for r in rows:
        if r["type"] not in ("worker.messaged", "worker.asked"):
            continue
        payload = json.loads(r["payload"])
        text = payload.get("text") or payload.get("question") or ""
        if text:
            lines.append(text)
    return "\n".join(lines)[-char_budget:]


def build_packet(conn, task_id: str, cause_seq: int, *, yolo: bool, live_session: bool,
                 max_nudges: int, transcript_tail_tokens: int) -> TriagePacket:
    task = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
    trigger_row = dict(conn.execute("SELECT * FROM events WHERE seq = ?", (cause_seq,)).fetchone())
    trigger = TriggerEvent(seq=trigger_row["seq"], type=trigger_row["type"],
                           source=trigger_row["source"], payload=json.loads(trigger_row["payload"]))

    verify_output = trigger.payload.get("output_tail") if trigger.type == "verify.failed" else None

    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE task_id = ? ORDER BY seq", (task_id,))]
    event_history = _compact_history(rows)
    transcript_tail = _transcript_tail(rows, transcript_tail_tokens * 4)  # ~4 chars/token

    nudges_used = sum(
        1 for r in rows
        if r["type"] == "supervisor.acted" and json.loads(r["payload"]).get("action") == "nudge"
    )
    nudges_remaining = max(0, max_nudges - nudges_used)
    retries_remaining = max(0, task["max_retries"] - task["retries"])

    allowed = compute_allowed_actions(trigger.type, nudges_remaining=nudges_remaining,
                                      retries_remaining=retries_remaining, yolo=yolo,
                                      live_session=live_session)

    return TriagePacket(
        task_id=task_id, brief=task["brief"], repo=task["repo"],
        delivery_mode=task["delivery_mode"], verify_cmd=task["verify_cmd"],
        trigger=trigger, verify_output=verify_output,
        event_history=event_history, transcript_tail=transcript_tail,
        allowed_actions=allowed, nudges_remaining=nudges_remaining,
        retries_remaining=retries_remaining, yolo=yolo,
    )
