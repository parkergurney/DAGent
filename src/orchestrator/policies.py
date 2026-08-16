"""Small policy boundary for Harbor experiments."""

import asyncio
import os
import signal
from functools import partial
from pathlib import Path

from orchestrator import config
from orchestrator.scheduler import Scheduler
from orchestrator.supervisor import always_escalate, invoke_supervisor
from orchestrator.worker import (
    spawn_cli_worker, spawn_fake_worker, spawn_sdk_worker, validate_worker_boundary,
)

POLICIES = ("sequential", "naive-parallel", "orchestrator")


def _fault_injecting_worker(worker, fault_injection):
    """Inject one controlled worker exit for a Harbor fault experiment."""
    if not isinstance(fault_injection, dict):
        raise ValueError("fault_injection must be an object")
    mode = str(fault_injection.get("mode") or "worker_exit")
    supported_modes = {"worker_exit", "crash", "timeout", "no_candidate", "verify_fail", "latency"}
    if mode not in supported_modes:
        raise ValueError(f"unsupported fault injection mode {mode!r}")
    task_id = str(fault_injection.get("task_id") or "").strip()
    if not task_id:
        raise ValueError("fault_injection.task_id is required")
    delay_s = max(0.1, float(fault_injection.get("delay_s", 1.0)))
    try:
        target_attempt = int(fault_injection.get("attempt", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("fault_injection.attempt must be a positive integer") from exc
    if target_attempt < 1:
        raise ValueError("fault_injection.attempt must be a positive integer")
    injected = False
    launches = 0

    async def spawn(task, worktree, *, model=None):
        nonlocal injected, launches
        proc = await worker(task, worktree, model=model)
        if task.get("id") == task_id:
            launches += 1
        if not injected and task.get("id") == task_id and launches == target_attempt:
            injected = True
            task["_fault_injection_reached"] = True
            task["_fault_injection_target"] = task_id
            task["_fault_injection_attempt"] = launches
            task["_fault_injection_mode"] = mode

            if mode in {"worker_exit", "crash"}:
                async def terminate_first_attempt():
                    await asyncio.sleep(delay_s)
                    if proc.returncode is None:
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass

                asyncio.create_task(terminate_first_attempt())
            elif mode == "latency":
                # The delay is deliberately outside the worker implementation:
                # it makes the latency profile backend-neutral and deterministic
                # for both FakeWorker and SDK workers.
                await asyncio.sleep(delay_s)
        return proc

    return spawn


def build_scheduler(conn, repo_root, worktree_root, *, policy="orchestrator",
                    max_concurrency=4, worker_model=None, supervisor_model=None,
                    worker_env=None, config_path=None, fake_worker=False,
                    fake_supervisor=False, external_isolation=False,
                    trusted_development=False, artifact_root=None,
                    verify_timeout_s=None, stall_threshold_s=None,
                    wait_ceiling_s=None, base_branch="main",
                    deterministic_crash_recovery=None, adaptive_scheduling=None,
                    protocol_recovery_v2=None,
                    deterministic_recovery=None,
                    evidence_ladder=None,
                    **kwargs) -> Scheduler:
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
    fault_injection = kwargs.pop("fault_injection", None)
    if fault_injection is not None:
        worker = _fault_injecting_worker(worker, fault_injection)
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
        deterministic_crash_recovery=(policy == "orchestrator"
                                      if deterministic_crash_recovery is None
                                      else deterministic_crash_recovery),
        adaptive_scheduling=(policy == "orchestrator" and cfg.adaptive_scheduling
                             if adaptive_scheduling is None else adaptive_scheduling),
        protocol_recovery_v2=(policy == "orchestrator" and cfg.protocol_recovery_v2
                              if protocol_recovery_v2 is None else protocol_recovery_v2),
        deterministic_recovery=(policy == "orchestrator" and cfg.deterministic_recovery
                                if deterministic_recovery is None else deterministic_recovery),
        evidence_ladder=(policy == "orchestrator" and cfg.evidence_ladder
                         if evidence_ladder is None else evidence_ladder),
    )


async def run_policy(conn, repo_root, worktree_root, *, policy="orchestrator", **kwargs):
    """Run one selected policy; cancellation still reaches Scheduler teardown."""
    scheduler = build_scheduler(conn, repo_root, worktree_root, policy=policy, **kwargs)
    await scheduler.run_until_settled()
    return scheduler
