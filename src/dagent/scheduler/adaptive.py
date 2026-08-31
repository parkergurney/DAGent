"""Small deterministic scheduling heuristics used only by the orchestrator policy."""
from functools import lru_cache


def task_scores(conn, candidates: list[dict]) -> dict[str, dict]:
    dependents: dict[str, list[str]] = {row["id"]: [] for row in candidates}
    for edge in conn.execute("SELECT task_id, depends_on FROM task_deps"):
        dependents.setdefault(edge["depends_on"], []).append(edge["task_id"])

    @lru_cache(maxsize=None)
    def depth(task_id: str) -> int:
        children = dependents.get(task_id, [])
        return 1 + max((depth(child) for child in children), default=0)

    scores = {}
    for row in candidates:
        children = dependents.get(row["id"], [])
        scores[row["id"]] = {
            "critical_path_depth": depth(row["id"]),
            "downstream_count": len(children),
            "score": depth(row["id"]) * 100 + len(children) * 10,
        }
    return scores


def choose_task(conn, candidates: list[dict]) -> tuple[dict, dict[str, dict]]:
    scores = task_scores(conn, candidates)
    selected = min(candidates, key=lambda row: (-scores[row["id"]]["score"],
                                                 row["created_at"], row["id"]))
    return selected, scores


def effective_limit(conn, base_limit: int, active: int) -> tuple[int, dict]:
    """Reduce concurrency only after a measurable recent latency/cost signal."""
    rows = conn.execute(
        "SELECT worker_started_at, worker_ended_at FROM attempts "
        "WHERE worker_started_at IS NOT NULL AND worker_ended_at IS NOT NULL "
        "ORDER BY attempt_no DESC LIMIT 8"
    ).fetchall()
    avg_s = 0.0
    if rows:
        from datetime import datetime
        durations = [(datetime.fromisoformat(row["worker_ended_at"]) -
                      datetime.fromisoformat(row["worker_started_at"])).total_seconds()
                     for row in rows]
        avg_s = sum(max(item, 0.0) for item in durations) / len(durations)
    limit = max(1, base_limit - 1) if avg_s > 60 and base_limit > 1 else base_limit
    return limit, {"recent_worker_avg_s": round(avg_s, 3), "active_workers": active,
                   "base_limit": base_limit, "effective_limit": limit}
