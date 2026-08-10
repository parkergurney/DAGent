"""Small policy boundary for Harbor experiments."""

from functools import partial
from pathlib import Path

from orchestrator import config
from orchestrator.scheduler import Scheduler
from orchestrator.supervisor import always_escalate, invoke_supervisor
from orchestrator.worker import (
    spawn_cli_worker, spawn_fake_worker, spawn_sdk_worker, validate_worker_boundary,
)

POLICIES = ("sequential", "naive-parallel", "orchestrator")


def build_scheduler(conn, repo_root, worktree_root, *, policy="orchestrator",
                    max_concurrency=4, worker_model=None, supervisor_model=None,
                    worker_env=None, config_path=None, fake_worker=False,
                    fake_supervisor=False, external_isolation=False,
                    trusted_development=False, artifact_root=None,
                    verify_timeout_s=None, stall_threshold_s=None,
                    wait_ceiling_s=None, base_branch="main", **kwargs) -> Scheduler:
    """Build the common scheduler for a Harbor-selected execution policy.

    Sequential and naive-parallel retain the same worker/verification lifecycle
    while selecting one or many worker slots.  The orchestrator policy enables
    the caller's supervisor; baseline policies use deterministic escalation.
    """
    if policy not in POLICIES:
        raise ValueError(f"unsupported policy {policy!r}; choose one of {POLICIES}")
    validate_worker_boundary(
        fake_worker=fake_worker,
        external_isolation=external_isolation,
        trusted_development=trusted_development,
    )
    cfg = config.load(config_path)
    concurrency = 1 if policy == "sequential" else max_concurrency
    worker = spawn_fake_worker if fake_worker else spawn_sdk_worker
    if worker_env and worker in (spawn_sdk_worker, spawn_cli_worker):
        worker = partial(worker, env=dict(worker_env))
    supervisor = always_escalate if fake_supervisor or policy != "orchestrator" else partial(
        invoke_supervisor, model=supervisor_model or cfg.model_supervisor,
        artifact_root=artifact_root,
    )
    return Scheduler(
        conn, repo_root, Path(worktree_root), max_concurrency=concurrency,
        spawn_worker=worker, worker_model=worker_model or cfg.model_worker,
        supervisor=supervisor, max_nudges=cfg.max_nudges,
        artifact_root=artifact_root, base_branch=base_branch,
        stall_threshold_s=stall_threshold_s or cfg.stall_threshold_s,
        repeated_failure_threshold=cfg.repeated_failure_threshold,
        wait_ceiling_s=wait_ceiling_s or cfg.wait_ceiling_s,
        verify_timeout_s=verify_timeout_s or cfg.verify_timeout_s,
        transcript_tail_tokens=cfg.transcript_tail_tokens, **kwargs,
    )


async def run_policy(conn, repo_root, worktree_root, *, policy="orchestrator", **kwargs):
    """Run one selected policy; cancellation still reaches Scheduler teardown."""
    scheduler = build_scheduler(conn, repo_root, worktree_root, policy=policy, **kwargs)
    await scheduler.run_until_settled()
    return scheduler
