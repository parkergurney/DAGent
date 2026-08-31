"""Metrics exported from the durable orchestrator event database."""

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dagent.store import connect
from dagent.recovery import classify_failure


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
    recovery_attempts: int
    recovery_verified: int
    recovery_failed: int
    first_failure_class: str | None
    first_failure_event_seq: int | None
    failure_classes: dict
    terminal_classifications: dict
    recovery_time_s: float
    evidence_stage_counts: dict = field(default_factory=dict)
    evidence_stage_timing_s: dict = field(default_factory=dict)
    fault_target_reached: bool = False
    fault_target: str | None = None
    state_counts: dict = field(default_factory=dict)
    terminal_state_counts: dict = field(default_factory=dict)
    wall_time_s: float = 0.0

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


def _evidence_metrics(events: list[dict]) -> dict:
    """Aggregate additive evidence-stage metrics from future/current events.

    The scheduler is intentionally not coupled here: once it emits a
    ``verify.evidence_stage`` event, or embeds the ladder result in a verify
    event, metrics become available without a schema migration.  Unknown
    payloads are ignored so old databases remain readable.
    """
    counts: dict[str, int] = {}
    timing: dict[str, float] = {}

    def record(stage: object, duration: object) -> None:
        if not stage:
            return
        name = str(stage)
        counts[name] = counts.get(name, 0) + 1
        try:
            seconds = max(float(duration or 0.0), 0.0)
        except (TypeError, ValueError):
            seconds = 0.0
        timing[name] = round(timing.get(name, 0.0) + seconds, 3)

    for event in events:
        payload = json.loads(event["payload"] or "{}")
        if event["type"] in {"verify.evidence_stage", "evidence.stage_completed",
                              "verify.stage_completed"}:
            record(payload.get("stage"), payload.get("duration_s", payload.get("duration")))
        embedded = payload.get("evidence")
        if isinstance(embedded, dict):
            for stage in embedded.get("stages", []):
                if isinstance(stage, dict) and stage.get("applicable", True):
                    record(stage.get("stage"), stage.get("duration_s", stage.get("duration")))
    return {"evidence_stage_counts": counts, "evidence_stage_timing_s": timing}


def metrics_for(conn: sqlite3.Connection) -> RunMetrics:
    events = [dict(row) for row in conn.execute("SELECT * FROM events ORDER BY seq")]
    counts = {row["state"]: row["c"] for row in conn.execute(
        "SELECT state, COUNT(*) c FROM tasks GROUP BY state"
    )}
    event_times = [event["ts"] for event in events if event["ts"]]
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
    evidence_metrics = _evidence_metrics(events)
    fault_event = next(
        (event for event in events if event["type"] == "fault_injection.target_reached"),
        None,
    )
    failure_events = []
    failure_counts = {}
    for row in conn.execute("SELECT seq, type, payload FROM events ORDER BY seq"):
        payload = json.loads(row["payload"] or "{}")
        if row["type"] in {"worker.exited", "worker.protocol_incomplete", "worker.stalled",
                            "worker.timeout", "worker.sdk_timeout", "worker.startup_failed",
                            "worker.sdk_failure", "verify.failed",
                            "artifact.validation_failed", "interface.validation_failed",
                            "delivery.failed"}:
            value = ("sdk_failure" if row["type"] == "worker.sdk_failure"
                     else classify_failure(row["type"], payload).value)
            failure_events.append((row["seq"], value))
            failure_counts[value] = failure_counts.get(value, 0) + 1
    terminal = {}
    for row in conn.execute("SELECT payload FROM events WHERE type = 'task.terminal_classified'"):
        value = json.loads(row["payload"] or "{}").get("classification")
        if value:
            terminal[value] = terminal.get(value, 0) + 1
    recovery_time = sum(_duration(row["started_at"], row["ended_at"])
                        for row in conn.execute(
                            "SELECT started_at, ended_at FROM supervisor_interventions"))
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
        fault_target_reached=fault_event is not None,
        fault_target=(json.loads(fault_event["payload"]).get("target")
                      if fault_event else None),
        recovery_attempts=conn.execute(
            "SELECT COUNT(*) c FROM events WHERE type = 'recovery.attempted'"
        ).fetchone()["c"],
        recovery_verified=conn.execute(
            "SELECT COUNT(*) c FROM events WHERE type = 'recovery.verified'"
        ).fetchone()["c"],
        recovery_failed=conn.execute(
            "SELECT COUNT(*) c FROM events WHERE type = 'recovery.failed'"
        ).fetchone()["c"],
        first_failure_class=failure_events[0][1] if failure_events else None,
        first_failure_event_seq=failure_events[0][0] if failure_events else None,
        failure_classes=failure_counts,
        terminal_classifications=terminal,
        recovery_time_s=round(recovery_time, 3),
        state_counts=counts,
        terminal_state_counts={
            state: counts.get(state, 0)
            for state in ("delivered", "failed", "cancelled", "dependency_blocked")
            if counts.get(state, 0)
        },
        wall_time_s=round(
            _duration(event_times[0], event_times[-1]) if len(event_times) > 1 else 0.0, 3
        ),
        **evidence_metrics,
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
