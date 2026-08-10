"""Metrics exported from the durable orchestrator event database."""

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from orchestrator.store import connect


@dataclass(frozen=True)
class RunMetrics:
    tasks: int
    executed: int
    delivered: int
    failed: int
    cancelled: int
    needs_human: int
    dependency_blocked: int
    tokens_in: int
    tokens_out: int
    cost_usd: float
    worker_cost_usd: float
    supervisor_cost_usd: float
    interventions: int
    verify_failed: int
    verification_recoveries: int
    queue_wait_s: float
    slot_occupancy_s: float
    worker_execution_s: float
    verification_s: float
    supervisor_s: float
    triage_s: float
    candidate_to_retry_s: float
    peak_live_workers: int
    worker_limit: int
    attempts: int
    verification_attempts: int

    def to_dict(self) -> dict:
        return asdict(self)


def _timestamp(value: str):
    from datetime import datetime
    return datetime.fromisoformat(value)


def _duration(start: str | None, end: str | None) -> float:
    if not start or not end:
        return 0.0
    return max((_timestamp(end) - _timestamp(start)).total_seconds(), 0.0)


def _timing(conn: sqlite3.Connection) -> dict:
    events = [dict(row) for row in conn.execute("SELECT * FROM events ORDER BY seq")]
    queued: dict[str, list[str]] = {}
    acquired: dict[str, str] = {}
    queue_wait = slot_occupancy = 0.0
    peak = limit = 0
    triage_started: dict[int, str] = {}
    triage = 0.0
    for event in events:
        payload = json.loads(event["payload"] or "{}")
        task_id = event["task_id"]
        if event["type"] == "task.state_changed" and payload.get("to") == "queued":
            queued.setdefault(task_id, []).append(event["ts"])
        elif event["type"] == "worker.slot_acquired":
            attempt_id = payload.get("attempt_id")
            if attempt_id and attempt_id not in acquired:
                acquired[attempt_id] = event["ts"]
                if queued.get(task_id):
                    queue_wait += _duration(queued[task_id].pop(0), event["ts"])
            peak = max(peak, int(payload.get("occupancy", 0)))
            limit = max(limit, int(payload.get("limit", 0)))
        elif event["type"] == "worker.slot_released":
            attempt_id = payload.get("attempt_id")
            if attempt_id in acquired:
                slot_occupancy += _duration(acquired.pop(attempt_id), event["ts"])
    for event in events:
        payload = json.loads(event["payload"] or "{}")
        cause_seq = payload.get("cause_seq")
        if cause_seq is None:
            continue
        if event["type"] == "triage.started":
            triage_started[cause_seq] = event["ts"]
        elif event["type"] == "triage.finished" and cause_seq in triage_started:
            triage += _duration(triage_started.pop(cause_seq), event["ts"])

    attempts = [dict(row) for row in conn.execute("SELECT * FROM attempts")]
    candidate_to_retry = 0.0
    by_task: dict[str, list[dict]] = {}
    for attempt in sorted(attempts, key=lambda row: (row["task_id"], row["attempt_no"])):
        by_task.setdefault(attempt["task_id"], []).append(attempt)
    for lineage in by_task.values():
        for previous, child in zip(lineage, lineage[1:]):
            candidate_to_retry += _duration(previous["worker_ended_at"], child["worker_started_at"])
    return {
        "queue_wait_s": round(queue_wait, 3),
        "slot_occupancy_s": round(slot_occupancy, 3),
        "worker_execution_s": round(sum(_duration(a["worker_started_at"], a["worker_ended_at"])
                                         for a in attempts), 3),
        "verification_s": round(sum(_duration(a["verification_started_at"], a["verification_ended_at"])
                                    for a in attempts), 3),
        "supervisor_s": round(sum(_duration(row["started_at"], row["ended_at"])
                                  for row in conn.execute("SELECT started_at, ended_at FROM supervisor_interventions")), 3),
        "triage_s": round(triage, 3),
        "candidate_to_retry_s": round(candidate_to_retry, 3),
        "peak_live_workers": peak, "worker_limit": limit,
        "attempts": len(attempts),
        "verification_attempts": sum(1 for event in events if event["type"] == "verify.started"),
    }


def metrics_for(conn: sqlite3.Connection) -> RunMetrics:
    counts = {row["state"]: row["c"] for row in conn.execute(
        "SELECT state, COUNT(*) c FROM tasks GROUP BY state"
    )}
    tokens = conn.execute(
        "SELECT COALESCE(SUM(tokens_in), 0) tokens_in, COALESCE(SUM(tokens_out), 0) tokens_out FROM events"
    ).fetchone()
    costs = {row["source"]: row["cost"] or 0.0 for row in conn.execute(
        "SELECT source, SUM(cost_usd) cost FROM events GROUP BY source"
    )}
    interventions = conn.execute(
        "SELECT COUNT(*) c FROM supervisor_interventions"
    ).fetchone()["c"]
    timing = _timing(conn)
    return RunMetrics(
        tasks=sum(counts.values()),
        executed=conn.execute("SELECT COUNT(DISTINCT task_id) c FROM events WHERE type='worker.spawned'").fetchone()["c"],
        delivered=counts.get("delivered", 0), failed=counts.get("failed", 0),
        cancelled=counts.get("cancelled", 0), needs_human=counts.get("needs_human", 0),
        dependency_blocked=counts.get("dependency_blocked", 0),
        tokens_in=tokens["tokens_in"], tokens_out=tokens["tokens_out"],
        cost_usd=sum(costs.values()), worker_cost_usd=costs.get("worker", 0.0),
        supervisor_cost_usd=costs.get("supervisor", 0.0), interventions=interventions,
        verify_failed=conn.execute("SELECT COUNT(*) c FROM events WHERE type='verify.failed'").fetchone()["c"],
        verification_recoveries=conn.execute(
            "SELECT COUNT(*) c FROM events WHERE type='verification.recovered'"
        ).fetchone()["c"],
        **timing,
    )


def export_metrics(source: sqlite3.Connection | str | Path) -> dict:
    """Return JSON-compatible metrics for Harbor or another experiment runner."""
    if isinstance(source, sqlite3.Connection):
        return metrics_for(source).to_dict()
    conn = connect(str(source))
    try:
        return metrics_for(conn).to_dict()
    finally:
        conn.close()
