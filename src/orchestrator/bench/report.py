"""SQL-derived benchmark metrics."""
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from orchestrator.store import connect


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    condition: str
    suite: str
    seed: int
    tasks: int
    delivered: int
    failed: int
    cancelled: int
    needs_human: int
    wall_s: float
    tasks_per_hour: float
    cost_usd: float
    worker_cost_usd: float
    supervisor_cost_usd: float
    interventions: int
    recovered_faults: int
    protected_path_modified: int
    verify_failed: int


def summarize_db(db_path: str | Path) -> RunSummary:
    conn = connect(str(db_path))
    meta = _metadata(conn, db_path)
    counts = {
        row["state"]: row["c"]
        for row in conn.execute("SELECT state, COUNT(*) c FROM tasks GROUP BY state")
    }
    tasks = sum(counts.values())
    wall_s = _wall_s(conn)
    cost = _cost(conn)
    source_cost = {
        row["source"]: row["cost"] or 0.0
        for row in conn.execute("SELECT source, SUM(cost_usd) cost FROM events GROUP BY source")
    }
    interventions = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE type = 'supervisor.acted' "
        "AND json_extract(payload, '$.action') = 'escalate'"
    ).fetchone()["c"]
    recovered_faults = conn.execute(
        "SELECT COUNT(DISTINCT task_id) c FROM events WHERE type = 'bench.fault_recovered'"
    ).fetchone()["c"]
    protected = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE type = 'verify.failed' "
        "AND json_extract(payload, '$.cause') = 'protected_path_modified'"
    ).fetchone()["c"]
    verify_failed = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE type = 'verify.failed'"
    ).fetchone()["c"]
    delivered = counts.get("delivered", 0)
    return RunSummary(
        run_id=meta.get("run_id", Path(db_path).stem),
        condition=meta.get("condition", "unknown"),
        suite=meta.get("suite", "unknown"),
        seed=int(meta.get("seed", 0)),
        tasks=tasks,
        delivered=delivered,
        failed=counts.get("failed", 0),
        cancelled=counts.get("cancelled", 0),
        needs_human=counts.get("needs_human", 0),
        wall_s=wall_s,
        tasks_per_hour=(delivered / wall_s * 3600) if wall_s else 0.0,
        cost_usd=cost,
        worker_cost_usd=source_cost.get("worker", 0.0),
        supervisor_cost_usd=source_cost.get("supervisor", 0.0),
        interventions=interventions,
        recovered_faults=recovered_faults,
        protected_path_modified=protected,
        verify_failed=verify_failed,
    )


def format_table(rows: list[RunSummary]) -> str:
    headers = [
        "run_id", "condition", "seed", "tasks", "delivered", "rate", "wall_min",
        "tasks/hr", "cost", "worker", "supervisor", "human", "recovered",
        "verify_failed", "protected",
    ]
    rendered = ["\t".join(headers)]
    for r in rows:
        rate = f"{(r.delivered / r.tasks * 100):.1f}%" if r.tasks else "0.0%"
        rendered.append("\t".join([
            r.run_id,
            r.condition,
            str(r.seed),
            str(r.tasks),
            str(r.delivered),
            rate,
            f"{r.wall_s / 60:.1f}",
            f"{r.tasks_per_hour:.1f}",
            f"{r.cost_usd:.4f}",
            f"{r.worker_cost_usd:.4f}",
            f"{r.supervisor_cost_usd:.4f}",
            str(r.interventions),
            str(r.recovered_faults),
            str(r.verify_failed),
            str(r.protected_path_modified),
        ]))
    return "\n".join(rendered)


def find_run_dbs(path: str | Path) -> list[Path]:
    path = Path(path)
    if path.is_file():
        return [path]
    return sorted(path.glob("**/run.db"))


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
