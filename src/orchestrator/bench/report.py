"""SQL-derived benchmark metrics."""
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from statistics import mean

from orchestrator.store import connect


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    condition: str
    suite: str
    seed: int
    tasks: int
    executed: int
    delivered: int
    failed: int
    cancelled: int
    needs_human: int
    dependency_blocked: int
    infrastructure_aborted: int
    wall_s: float
    tasks_per_hour: float
    tokens_in: int
    tokens_out: int
    cost_usd: float
    worker_cost_usd: float
    supervisor_cost_usd: float
    interventions: int
    recovered_faults: int
    verification_recoveries: int
    protected_path_modified: int
    verify_failed: int
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


@dataclass(frozen=True)
class GroupSummary:
    group: str
    runs: int
    tasks: int
    executed: int
    delivered: int
    failed: int
    needs_human: int
    dependency_blocked: int
    cancelled: int
    infrastructure_aborted: int
    mean_rate: float
    min_rate: float
    max_rate: float
    mean_wall_min: float
    mean_tasks_per_hour: float
    total_tokens_in: int
    total_tokens_out: int
    mean_tokens_in: float
    mean_tokens_out: float
    mean_cost_usd: float
    total_cost_usd: float
    interventions: int
    recovered_faults: int
    verification_recoveries: int
    verify_failed: int
    protected_path_modified: int


def summarize_db(db_path: str | Path) -> RunSummary:
    conn = connect(str(db_path))
    meta = _metadata(conn, db_path)
    counts = {
        row["state"]: row["c"]
        for row in conn.execute("SELECT state, COUNT(*) c FROM tasks GROUP BY state")
    }
    tasks = sum(counts.values())
    executed = conn.execute(
        "SELECT COUNT(DISTINCT task_id) c FROM events WHERE type = 'worker.spawned'"
    ).fetchone()["c"]
    started = conn.execute(
        "SELECT 1 FROM events WHERE type = 'bench.run_started' LIMIT 1"
    ).fetchone()
    finished = conn.execute(
        "SELECT 1 FROM events WHERE type = 'bench.run_finished' LIMIT 1"
    ).fetchone()
    wall_s = _wall_s(conn)
    cost = _cost(conn)
    token_row = conn.execute(
        "SELECT COALESCE(SUM(tokens_in), 0) tokens_in, "
        "COALESCE(SUM(tokens_out), 0) tokens_out FROM events"
    ).fetchone()
    source_cost = {
        row["source"]: row["cost"] or 0.0
        for row in conn.execute("SELECT source, SUM(cost_usd) cost FROM events GROUP BY source")
    }
    intervention_row = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(cost_usd), 0.0) cost "
        "FROM supervisor_interventions"
    ).fetchone()
    interventions = intervention_row["c"]
    if interventions:
        source_cost["supervisor"] = intervention_row["cost"]
    else:
        # Preserve reports for phase-one/manual databases with no rows here.
        interventions = conn.execute(
            "SELECT COUNT(*) c FROM events WHERE type = 'supervisor.acted' "
            "AND json_extract(payload, '$.action') = 'escalate'"
        ).fetchone()["c"]
    recovered_faults = conn.execute(
        "SELECT COUNT(DISTINCT task_id) c FROM events WHERE type = 'bench.fault_recovered'"
    ).fetchone()["c"]
    verification_recoveries = conn.execute(
        "SELECT COUNT(DISTINCT task_id) c FROM events WHERE type = 'verification.recovered'"
    ).fetchone()["c"]
    protected = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE type = 'verify.failed' "
        "AND json_extract(payload, '$.cause') = 'protected_path_modified'"
    ).fetchone()["c"]
    verify_failed = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE type = 'verify.failed'"
    ).fetchone()["c"]
    delivered = counts.get("delivered", 0)
    timing = _scheduler_timing(conn)
    return RunSummary(
        run_id=meta.get("run_id", Path(db_path).stem),
        condition=meta.get("condition", "unknown"),
        suite=meta.get("suite", "unknown"),
        seed=int(meta.get("seed", 0)),
        tasks=tasks,
        executed=executed,
        delivered=delivered,
        failed=counts.get("failed", 0),
        cancelled=counts.get("cancelled", 0),
        needs_human=counts.get("needs_human", 0),
        dependency_blocked=counts.get("dependency_blocked", 0),
        infrastructure_aborted=int(bool(started and not finished)),
        wall_s=wall_s,
        tasks_per_hour=(delivered / wall_s * 3600) if wall_s else 0.0,
        tokens_in=token_row["tokens_in"],
        tokens_out=token_row["tokens_out"],
        cost_usd=cost,
        worker_cost_usd=source_cost.get("worker", 0.0),
        supervisor_cost_usd=source_cost.get("supervisor", 0.0),
        interventions=interventions,
        recovered_faults=recovered_faults,
        verification_recoveries=verification_recoveries,
        protected_path_modified=protected,
        verify_failed=verify_failed,
        **timing,
    )


def summarize_groups(rows: list[RunSummary], group_by: str = "condition") -> list[GroupSummary]:
    groups: dict[str, list[RunSummary]] = {}
    for row in rows:
        key = _group_key(row, group_by)
        groups.setdefault(key, []).append(row)

    summaries = []
    for key, items in sorted(groups.items()):
        rates = [(r.delivered / r.tasks) if r.tasks else 0.0 for r in items]
        summaries.append(GroupSummary(
            group=key,
            runs=len(items),
            tasks=sum(r.tasks for r in items),
            executed=sum(r.executed for r in items),
            delivered=sum(r.delivered for r in items),
            failed=sum(r.failed for r in items),
            needs_human=sum(r.needs_human for r in items),
            dependency_blocked=sum(r.dependency_blocked for r in items),
            cancelled=sum(r.cancelled for r in items),
            infrastructure_aborted=sum(r.infrastructure_aborted for r in items),
            mean_rate=mean(rates) if rates else 0.0,
            min_rate=min(rates) if rates else 0.0,
            max_rate=max(rates) if rates else 0.0,
            mean_wall_min=mean(r.wall_s / 60 for r in items) if items else 0.0,
            mean_tasks_per_hour=mean(r.tasks_per_hour for r in items) if items else 0.0,
            total_tokens_in=sum(r.tokens_in for r in items),
            total_tokens_out=sum(r.tokens_out for r in items),
            mean_tokens_in=mean(r.tokens_in for r in items) if items else 0.0,
            mean_tokens_out=mean(r.tokens_out for r in items) if items else 0.0,
            mean_cost_usd=mean(r.cost_usd for r in items) if items else 0.0,
            total_cost_usd=sum(r.cost_usd for r in items),
            interventions=sum(r.interventions for r in items),
            recovered_faults=sum(r.recovered_faults for r in items),
            verification_recoveries=sum(r.verification_recoveries for r in items),
            verify_failed=sum(r.verify_failed for r in items),
            protected_path_modified=sum(r.protected_path_modified for r in items),
        ))
    return summaries


def format_table(rows: list[RunSummary]) -> str:
    headers = [
        "run_id", "condition", "seed", "tasks", "executed", "delivered", "failed",
        "needs_human", "dependency_blocked", "cancelled", "infra_aborted", "rate", "wall_min",
        "tasks/hr", "tokens_in", "tokens_out", "cost", "worker", "supervisor", "human", "recovered",
        "verify_failed", "verification_recoveries", "protected", "queue_wait_s",
        "slot_occupancy_s", "worker_execution_s", "verification_s", "supervisor_s",
        "triage_s", "candidate_to_retry_s", "peak_workers", "worker_limit", "attempts",
        "verification_attempts",
    ]
    rendered = ["\t".join(headers)]
    for r in rows:
        rate = f"{(r.delivered / r.tasks * 100):.1f}%" if r.tasks else "0.0%"
        rendered.append("\t".join([
            r.run_id,
            r.condition,
            str(r.seed),
            str(r.tasks),
            str(r.executed),
            str(r.delivered),
            str(r.failed),
            str(r.needs_human),
            str(r.dependency_blocked),
            str(r.cancelled),
            str(r.infrastructure_aborted),
            rate,
            f"{r.wall_s / 60:.1f}",
            f"{r.tasks_per_hour:.1f}",
            str(r.tokens_in),
            str(r.tokens_out),
            f"{r.cost_usd:.4f}",
            f"{r.worker_cost_usd:.4f}",
            f"{r.supervisor_cost_usd:.4f}",
            str(r.interventions),
            str(r.recovered_faults),
            str(r.verify_failed),
            str(r.verification_recoveries),
            str(r.protected_path_modified),
            f"{r.queue_wait_s:.3f}",
            f"{r.slot_occupancy_s:.3f}",
            f"{r.worker_execution_s:.3f}",
            f"{r.verification_s:.3f}",
            f"{r.supervisor_s:.3f}",
            f"{r.triage_s:.3f}",
            f"{r.candidate_to_retry_s:.3f}",
            str(r.peak_live_workers),
            str(r.worker_limit),
            str(r.attempts),
            str(r.verification_attempts),
        ]))
    return "\n".join(rendered)


def format_summary_table(rows: list[GroupSummary]) -> str:
    headers = [
        "group", "runs", "tasks", "executed", "delivered", "failed", "needs_human",
        "dependency_blocked", "cancelled", "infra_aborted", "mean_rate", "rate_range",
        "mean_wall_min", "mean_tasks/hr", "total_tokens_in", "total_tokens_out",
        "mean_tokens_in", "mean_tokens_out", "mean_cost", "total_cost", "human",
        "recovered", "verify_failed", "verification_recoveries", "protected",
    ]
    rendered = ["\t".join(headers)]
    for r in rows:
        rendered.append("\t".join([
            r.group,
            str(r.runs),
            str(r.tasks),
            str(r.executed),
            str(r.delivered),
            str(r.failed),
            str(r.needs_human),
            str(r.dependency_blocked),
            str(r.cancelled),
            str(r.infrastructure_aborted),
            f"{r.mean_rate * 100:.1f}%",
            f"{r.min_rate * 100:.1f}-{r.max_rate * 100:.1f}%",
            f"{r.mean_wall_min:.1f}",
            f"{r.mean_tasks_per_hour:.1f}",
            str(r.total_tokens_in),
            str(r.total_tokens_out),
            f"{r.mean_tokens_in:.1f}",
            f"{r.mean_tokens_out:.1f}",
            f"{r.mean_cost_usd:.4f}",
            f"{r.total_cost_usd:.4f}",
            str(r.interventions),
            str(r.recovered_faults),
            str(r.verify_failed),
            str(r.verification_recoveries),
            str(r.protected_path_modified),
        ]))
    return "\n".join(rendered)


def find_run_dbs(path: str | Path) -> list[Path]:
    """Select only manifest-backed runs in the requested collection.

    A benchmark root contains suite directories, and a suite directory
    contains run directories.  Deliberately do not recurse beyond those two
    levels: archived/nested historical databases are not part of a report
    unless the operator names that run directory explicitly.
    """
    path = Path(path)
    if path.is_file():
        dbs = [path]
    elif not path.is_dir():
        dbs = []
    elif (path / "run.db").is_file() and (path / "manifest.json").is_file():
        dbs = [path / "run.db"]
    else:
        direct = _manifest_dbs(path)
        if direct:
            dbs = direct
        else:
            dbs = [db for suite_dir in sorted(p for p in path.iterdir() if p.is_dir())
                   for db in _manifest_dbs(suite_dir)
                   if _manifest_suite(db) == suite_dir.name]

    dbs = sorted(dict.fromkeys(db.resolve() for db in dbs))
    run_ids = {}
    for db in dbs:
        run_id = _read_metadata(db).get("run_id")
        if run_id in run_ids:
            raise ValueError(
                f"duplicate benchmark run_id {run_id!r} in {run_ids[run_id]} and {db}"
            )
        run_ids[run_id] = db
    return dbs


def _manifest_dbs(directory: Path) -> list[Path]:
    dbs = []
    for run_dir in sorted(p for p in directory.iterdir() if p.is_dir()):
        db = run_dir / "run.db"
        manifest = run_dir / "manifest.json"
        if db.is_file() and manifest.is_file():
            dbs.append(db)
    return dbs


def _manifest_suite(db: Path) -> str | None:
    try:
        return _read_metadata(db).get("suite")
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None


def _read_metadata(db: Path) -> dict:
    conn = connect(str(db))
    try:
        return _metadata(conn, db)
    finally:
        conn.close()


def _group_key(row: RunSummary, group_by: str) -> str:
    fields = {
        "condition": row.condition,
        "suite": row.suite,
        "seed": str(row.seed),
    }
    parts = [part.strip() for part in group_by.split(",") if part.strip()]
    bad = [part for part in parts if part not in fields]
    if bad:
        raise ValueError(f"unknown group field(s): {', '.join(bad)}")
    return "/".join(fields[part] for part in parts)


def _metadata(conn: sqlite3.Connection, db_path: str | Path) -> dict:
    row = conn.execute(
        "SELECT payload FROM events WHERE type = 'bench.run_started' ORDER BY seq LIMIT 1"
    ).fetchone()
    if row:
        return json.loads(row["payload"])
    return {"run_id": Path(db_path).stem}


def _wall_s(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT MIN(ts) first, MAX(ts) last FROM events").fetchone()
    if not row["first"] or not row["last"]:
        return 0.0
    start = _parse_ts(row["first"])
    end = _parse_ts(row["last"])
    return max((end - start).total_seconds(), 0.0)


def _cost(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT SUM(cost_usd) c FROM events").fetchone()
    return row["c"] or 0.0


def _parse_ts(ts: str):
    from datetime import datetime

    return datetime.fromisoformat(ts)


def _duration(start: str | None, end: str | None) -> float:
    if not start or not end:
        return 0.0
    return max((_parse_ts(end) - _parse_ts(start)).total_seconds(), 0.0)


def _scheduler_timing(conn: sqlite3.Connection) -> dict:
    """Derive phase timing from durable events/attempt rows.

    Slot accounting is event based: an acquire/release pair is one lease, and
    the occupancy recorded on each event makes peak capacity auditable without
    polling. Older benchmark DBs simply report zero for the phase-three fields.
    """
    events = [dict(row) for row in conn.execute(
        "SELECT * FROM events ORDER BY seq"
    )]
    queued: dict[str, list[str]] = {}
    queue_wait = 0.0
    slot_acquired: dict[str, str] = {}
    slot_occupancy = 0.0
    peak = 0
    observed_limit = 0
    for event in events:
        payload = json.loads(event["payload"] or "{}")
        task_id = event["task_id"]
        if event["type"] == "task.state_changed" and payload.get("to") == "queued":
            queued.setdefault(task_id, []).append(event["ts"])
        elif event["type"] == "worker.slot_acquired":
            attempt_id = payload.get("attempt_id")
            if attempt_id and attempt_id not in slot_acquired:
                slot_acquired[attempt_id] = event["ts"]
                if queued.get(task_id):
                    queue_wait += _duration(queued[task_id].pop(0), event["ts"])
            peak = max(peak, int(payload.get("occupancy", 0)))
            observed_limit = max(observed_limit, int(payload.get("limit", 0)))
        elif event["type"] == "worker.slot_released":
            attempt_id = payload.get("attempt_id")
            if attempt_id in slot_acquired:
                slot_occupancy += _duration(slot_acquired.pop(attempt_id), event["ts"])

    attempts = [dict(row) for row in conn.execute(
        "SELECT * FROM attempts ORDER BY task_id, attempt_no"
    )]
    worker_execution = sum(_duration(a["worker_started_at"], a["worker_ended_at"])
                           for a in attempts)
    verification = sum(_duration(a["verification_started_at"], a["verification_ended_at"])
                       for a in attempts)
    candidate_to_retry = 0.0
    by_task: dict[str, list[dict]] = {}
    for attempt in attempts:
        by_task.setdefault(attempt["task_id"], []).append(attempt)
    for lineage in by_task.values():
        for previous, child in zip(lineage, lineage[1:]):
            candidate_to_retry += _duration(previous["worker_ended_at"],
                                            child["worker_started_at"])

    supervisor = sum(_duration(row["started_at"], row["ended_at"]) for row in conn.execute(
        "SELECT started_at, ended_at FROM supervisor_interventions"
    ))
    triage_started: dict[int, str] = {}
    triage = 0.0
    for event in events:
        payload = json.loads(event["payload"] or "{}")
        cause_seq = payload.get("cause_seq")
        if cause_seq is None:
            continue
        if event["type"] == "triage.started":
            triage_started[cause_seq] = event["ts"]
        elif event["type"] == "triage.finished" and cause_seq in triage_started:
            triage += _duration(triage_started.pop(cause_seq), event["ts"])

    metadata = _metadata(conn, "run.db")
    return {
        "queue_wait_s": round(queue_wait, 3),
        "slot_occupancy_s": round(slot_occupancy, 3),
        "worker_execution_s": round(worker_execution, 3),
        "verification_s": round(verification, 3),
        "supervisor_s": round(supervisor, 3),
        "triage_s": round(triage, 3),
        "candidate_to_retry_s": round(candidate_to_retry, 3),
        "peak_live_workers": peak,
        "worker_limit": int(metadata.get("max_concurrency", 0) or observed_limit),
        "attempts": len(attempts),
        "verification_attempts": sum(1 for e in events if e["type"] == "verify.started"),
    }
