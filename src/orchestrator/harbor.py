"""Minimal adapter boundary for Harbor task environments."""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.metrics import export_metrics
from orchestrator.policies import build_scheduler
from orchestrator.store import connect, create_task
from orchestrator.workflow_preflight import compile_preflight_plan, validate_fault_target


@dataclass(frozen=True)
class HarborRun:
    task_id: str
    state: str
    candidate_sha: str | None
    metrics: dict
    base_sha: str | None = None
    task_ids: tuple[str, ...] = ()
    task_states: dict[str, str] = field(default_factory=dict)
    preflight_plan: dict | None = None


def _validate_task_specs(task_specs: list[dict]) -> list[dict]:
    """Validate and normalize the public Harbor task graph before insertion."""
    return compile_preflight_plan(task_specs)["tasks"]


def _aggregate_state(task_states: dict[str, str]) -> str:
    if all(state == "delivered" for state in task_states.values()):
        return "delivered"
    for state in ("failed", "cancelled", "needs_human", "dependency_blocked"):
        if state in task_states.values():
            return state
    return next(iter(task_states.values()), "failed")


def _repo_head(repo_root: str | Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def export_task_summary(source: str | Path) -> list[dict]:
    """Return durable, credential-free task/attempt diagnostics for Harbor."""
    conn = connect(str(source))
    try:
        summary = []
        for task in conn.execute(
            "SELECT id, state, retries, candidate_sha, current_attempt_id "
            "FROM tasks ORDER BY created_at, id"
        ).fetchall():
            attempts = conn.execute(
                "SELECT attempt_no, disposition, candidate_sha, failure_cause, "
                "failure_signature, worker_ended_at, verification_ended_at "
                "FROM attempts WHERE task_id = ? ORDER BY attempt_no",
                (task["id"],),
            ).fetchall()
            summary.append({
                "id": task["id"],
                "state": task["state"],
                "retries": task["retries"],
                "candidate_sha": task["candidate_sha"],
                "current_attempt_id": task["current_attempt_id"],
                "attempts": [dict(attempt) for attempt in attempts],
            })
        return summary
    finally:
        conn.close()


async def run_instruction(*, instruction: str, repo_root: str | Path,
                          db_path: str | Path = ":memory:", worktree_root: str | Path = "data/worktrees",
                          title: str = "Harbor task", policy: str = "orchestrator",
                          verify_cmd: str | None = None, max_retries: int = 2,
                          max_concurrency: int = 4, worker_env: dict[str, str] | None = None,
                          fake_worker: bool = False, fake_supervisor: bool = False,
                          external_isolation: bool = False,
                          worker_model: str | None = None, supervisor_model: str | None = None,
                          artifact_root: str | Path | None = None,
                          verify_timeout_s: int | None = None,
                          stall_threshold_s: int | None = None,
                          wait_ceiling_s: int | None = None,
                          config_path: str | Path | None = None,
                          base_branch: str = "main",
                          task_specs: list[dict] | None = None,
                          fault_injection: dict | None = None,
                          deterministic_crash_recovery: bool | None = None,
                          adaptive_scheduling: bool | None = None,
                          protocol_recovery_v2: bool | None = None,
                          deterministic_recovery: bool | None = None,
                          evidence_ladder: bool | None = None,
                          preflight_plan: dict | None = None,
                          ) -> HarborRun:
    """Create and execute one task inside Harbor's already-isolated checkout.

    Real workers require ``external_isolation=True``.  This is an explicit
    declaration by the Harbor caller, not a local sandbox switch; the
    orchestrator cannot verify or supply the outer OS boundary.
    """
    base_sha_result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True,
        capture_output=True, text=True,
    )
    base_sha = base_sha_result.stdout.strip()
    conn = connect(str(db_path))
    try:
        preflight_plan = None
        if task_specs is None:
            if fault_injection and fault_injection.get("target_reachable"):
                raise ValueError(
                    "target-reachable fault injection requires an explicit task graph"
                )
            task_ids = [create_task(
                conn, title=title, brief=instruction, repo=str(repo_root),
                delivery_mode="scout", verify_cmd=verify_cmd,
                max_retries=max_retries,
            )]
        else:
            # This is intentionally before the first INSERT.  A malformed
            # graph or contract must not leave durable tasks behind or consume
            # worker resources.  The plan is also a public manifest-ready
            # record; scheduler adoption of its conflict recommendations is a
            # separate integration step.
            preflight_plan = compile_preflight_plan(task_specs, repo_root=str(repo_root))
            if fault_injection and fault_injection.get("target_reachable"):
                preflight_plan["fault_target"] = validate_fault_target(
                    task_specs, fault_injection,
                )
            normalized = preflight_plan["tasks"]
            task_ids = [create_task(
                conn, title=spec["title"], brief=spec["brief"], repo=str(repo_root),
                delivery_mode=spec["delivery_mode"], verify_cmd=spec["verify_cmd"],
                max_retries=spec["max_retries"], depends_on=spec["depends_on"],
                output_artifacts=spec["output_artifacts"], output_schema=spec["output_schema"],
                input_contract=spec["input_contract"], node_verify_cmd=spec["node_verify_cmd"],
                repair_policy=spec["repair_policy"],
                task_id=spec["id"],
            ) for spec in normalized]
        scheduler = build_scheduler(
            conn, repo_root, worktree_root, policy=policy, max_concurrency=max_concurrency,
            worker_env=worker_env, fake_worker=fake_worker, fake_supervisor=fake_supervisor,
            external_isolation=external_isolation,
            fault_injection=fault_injection,
            deterministic_crash_recovery=deterministic_crash_recovery,
            adaptive_scheduling=adaptive_scheduling,
            protocol_recovery_v2=protocol_recovery_v2,
            deterministic_recovery=deterministic_recovery,
            evidence_ladder=evidence_ladder,
            preflight_plan=preflight_plan,
            worker_model=worker_model, supervisor_model=supervisor_model,
            artifact_root=artifact_root, verify_timeout_s=verify_timeout_s,
            stall_threshold_s=stall_threshold_s, wait_ceiling_s=wait_ceiling_s,
            config_path=config_path, base_branch=base_branch,
        )
        await scheduler.run_until_settled()
        rows = conn.execute(
            "SELECT id, state, candidate_sha FROM tasks ORDER BY created_at, id"
        ).fetchall()
        task_states = {row["id"]: row["state"] for row in rows if row["id"] in task_ids}
        if task_specs is None:
            # Preserve the original single-task scout contract: scout records
            # the candidate commit without advancing the repository's main
            # branch.
            final_sha = next(
                (row["candidate_sha"] for row in rows if row["id"] == task_ids[0]),
                None,
            )
        else:
            # A graph uses local delivery so each settled dependency is
            # available to its children; HEAD is therefore the final graph
            # candidate exported to Harbor.
            final_sha = _repo_head(repo_root) if any(
                state == "delivered" for state in task_states.values()
            ) else None
        return HarborRun(
            task_ids[0], _aggregate_state(task_states), final_sha,
            export_metrics(conn), base_sha=base_sha,
            task_ids=tuple(task_ids), task_states=task_states,
            preflight_plan=preflight_plan,
        )
    finally:
        conn.close()


def export_patch(repo_root: str | Path, *, base_sha: str, candidate_sha: str,
                 destination: str | Path | None = None) -> str:
    """Export the declared candidate as a binary patch for Harbor's verifier."""
    result = subprocess.run(
        ["git", "diff", "--binary", base_sha, candidate_sha], cwd=repo_root,
        check=True, capture_output=True, text=True,
    )
    patch = result.stdout
    if destination is not None:
        Path(destination).write_text(patch)
    return patch
