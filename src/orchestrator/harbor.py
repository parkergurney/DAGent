"""Minimal adapter boundary for Harbor task environments."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

from orchestrator.metrics import export_metrics
from orchestrator.policies import build_scheduler
from orchestrator.store import connect, create_task


@dataclass(frozen=True)
class HarborRun:
    task_id: str
    state: str
    candidate_sha: str | None
    metrics: dict
    base_sha: str | None = None


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
                          base_branch: str = "main"
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
        task_id = create_task(conn, title=title, brief=instruction, repo=str(repo_root),
                              delivery_mode="scout", verify_cmd=verify_cmd,
                              max_retries=max_retries)
        scheduler = build_scheduler(
            conn, repo_root, worktree_root, policy=policy, max_concurrency=max_concurrency,
            worker_env=worker_env, fake_worker=fake_worker, fake_supervisor=fake_supervisor,
            external_isolation=external_isolation,
            worker_model=worker_model, supervisor_model=supervisor_model,
            artifact_root=artifact_root, verify_timeout_s=verify_timeout_s,
            stall_threshold_s=stall_threshold_s, wait_ceiling_s=wait_ceiling_s,
            config_path=config_path, base_branch=base_branch,
        )
        await scheduler.run_until_settled()
        task = conn.execute(
            "SELECT state, candidate_sha FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return HarborRun(task_id, task["state"], task["candidate_sha"],
                         export_metrics(conn), base_sha=base_sha)
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
