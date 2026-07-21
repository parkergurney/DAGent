"""Crash recovery (design.md section 4): "a reconciliation pass at startup:
for every task in running, check whether session_id is a live session; dead
ones get a synthetic worker.exited event and route through triage like any
other crash. No special recovery code path."

reconcile() does exactly that and nothing more -- it does not resolve triage
itself. Whatever normally drains triage (Scheduler, in M2 always straight to
needs_human -- see scheduler.core) picks these tasks up the same way it would
a live crash.
"""
import os

from orchestrator.store import append_event, transition


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
    rows = conn.execute("SELECT id, session_id FROM tasks WHERE state = 'running'").fetchall()
    for row in rows:
        if _pid_alive(row["session_id"]):
            continue
        s = append_event(conn, source="system", type="worker.exited", task_id=row["id"],
                         payload={"reason": "reconciled: session not alive"})
        transition(conn, row["id"], "triage", cause_seq=s)
