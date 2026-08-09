"""Benchmark condition runners.

Each run writes a normal orchestrator event DB so reports can be computed from
the same task/event model across baselines and the orchestrator condition.
"""
import asyncio
from io import BytesIO
import json
import os
import random
import signal
import shutil
import subprocess
import tarfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from orchestrator import config, delivery
from orchestrator.scheduler import (
    Scheduler, WorkerStartupFailure, advance_dependency_states, validate_dependency_graph,
)
from orchestrator.store import append_event, connect, create_task, transition
from orchestrator.supervisor import invoke_supervisor
from orchestrator.verify.gate import VerifyRequest, run_verify
from orchestrator.bench.preflight import (
    BenchmarkInfrastructureError, validate_benchmark_auth, validate_benchmark_isolation,
)
from orchestrator.worker import (
    WorktreePool,
    build_execution_contract,
    cleanup_worker_sandbox,
    spawn_fake_worker,
    spawn_sdk_worker,
)
from orchestrator.bench.suite import BenchSuite, load_suite

CONDITIONS = {"sequential", "naive-parallel", "orchestrator"}


@dataclass(frozen=True)
class BenchRun:
    run_id: str
    condition: str
    suite: str
    seed: int
    run_dir: str
    db: str


def run_benchmark(
    suite_path: str | Path,
    *,
    condition: str,
    out_dir: str | Path = "data/bench",
    seed: int = 1,
    repo_root: str | Path | None = None,
    worktree_root: str | Path | None = None,
    max_concurrency: int = 4,
    worker_model: str | None = None,
    supervisor_model: str | None = None,
    config_path: str | Path | None = None,
    fake_worker: bool = False,
    fake_supervisor: bool = False,
    kill_one_after_s: float | None = None,
    overwrite: bool = False,
) -> BenchRun:
    if condition not in CONDITIONS:
        raise ValueError(f"unsupported condition {condition!r}; choose one of {sorted(CONDITIONS)}")

    suite = load_suite(suite_path)
    repo_value = repo_root or suite.repo
    if not repo_value:
        raise ValueError("suite must define [bench].repo or --repo-root must be passed")
    repo = Path(repo_value)
    cfg = config.load(config_path)
    worker_model = worker_model or cfg.model_worker

    worktrees = Path(worktree_root) if worktree_root else (
        Path(out_dir) / suite.name / f"{condition}-seed{seed}" / "worktrees"
    )
    # This must precede overwrite handling and run materialization.  A
    # contaminated target is an operator finding, not disposable scratch.
    validate_benchmark_isolation(
        suite, repo, worktrees,
        worker_slots=1 if condition == "sequential" else max_concurrency,
    )
    if not fake_worker:
        # This happens before run materialization, task creation, and any
        # supervisor can be invoked. A failed auth smoke test cannot consume
        # task retries or create an infrastructure-shaped task failure.
        validate_benchmark_auth(worker_model)

    run_id = f"{suite.name}-{condition}-seed{seed}"
    run_dir = Path(out_dir) / suite.name / f"{condition}-seed{seed}"
    if run_dir.exists():
        if not overwrite:
            raise FileExistsError(f"{run_dir} already exists; pass --overwrite to replace it")
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    shutil.copy2(suite_path, run_dir / "suite.toml")
    if config_path:
        shutil.copy2(config_path, run_dir / "config.toml")

    # The source repository is an operator/verifier input, not a worker
    # trust-boundary input.  A fresh public snapshot gives workers normal Git
    # behavior without giving them the source repository's historical object
    # database, refs, alternates, or reflogs.
    worker_repo = _materialize_worker_repo(repo, suite.base_branch, run_dir / "worker-repo")
    validate_benchmark_isolation(
        suite, worker_repo, worktrees,
        worker_slots=1 if condition == "sequential" else max_concurrency,
    )

    db_path = run_dir / "run.db"
    worktrees = Path(worktree_root) if worktree_root else run_dir / "worktrees"
    supervisor_model = supervisor_model or cfg.model_supervisor

    conn = connect(str(db_path))
    _materialize_tasks(conn, suite, str(worker_repo), suite.max_retries)
    append_event(conn, source="system", type="bench.run_started", payload={
        "run_id": run_id,
        "suite": suite.name,
        "condition": condition,
        "seed": seed,
        "repo": str(repo),
        "worker_repo": str(worker_repo),
        "max_concurrency": max_concurrency,
        "worker_model": worker_model,
        "supervisor_model": supervisor_model if condition == "orchestrator" else None,
        "fake_worker": fake_worker,
        "fake_supervisor": fake_supervisor,
        "kill_one_after_s": kill_one_after_s,
        "artifact_root": str(run_dir / "artifacts"),
    })

    started = time.monotonic()
    random.seed(seed)
    try:
        if condition == "orchestrator":
            asyncio.run(_run_orchestrator(
                conn, worker_repo, worktrees, cfg, max_concurrency, worker_model, supervisor_model,
                fake_worker, fake_supervisor, kill_one_after_s, suite.base_branch,
                run_dir / "artifacts", run_id,
            ))
        else:
            concurrency = 1 if condition == "sequential" else max_concurrency
            asyncio.run(_run_baseline(
                conn, worker_repo, worktrees, cfg, concurrency, worker_model, fake_worker, kill_one_after_s,
                suite.base_branch, run_dir / "artifacts", run_id,
            ))
    except WorkerStartupFailure as exc:
        append_event(conn, source="system", type="bench.run_aborted", payload={
            "run_id": run_id,
            "category": exc.category,
            "task_id": exc.task_id,
            "reason": exc.reason,
        })
        conn.close()
        raise BenchmarkInfrastructureError(str(exc)) from exc

    append_event(conn, source="system", type="bench.run_finished", payload={
        "duration_s": round(time.monotonic() - started, 3),
    })
    manifest = BenchRun(
        run_id=run_id, condition=condition, suite=suite.name, seed=seed,
        run_dir=str(run_dir), db=str(db_path),
    )
    (run_dir / "manifest.json").write_text(json.dumps(asdict(manifest), indent=2))
    conn.close()
    return manifest


def _materialize_worker_repo(source_repo: str | Path, base_branch: str,
                             destination: str | Path) -> Path:
    """Create a worker-visible Git repository from one public source tree.

    This intentionally does not clone or add an object alternates path.  The
    resulting repository has one commit containing only the selected public
    tree, so objects reachable only from source history cannot be recovered by
    a worker through Git plumbing.
    """
    source = Path(source_repo).resolve()
    dest = Path(destination).resolve()
    if dest.exists():
        raise FileExistsError(f"worker repository already exists: {dest}")
    dest.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", base_branch],
        cwd=source, check=True, capture_output=True,
    ).stdout
    with tarfile.open(fileobj=BytesIO(archive), mode="r:") as tar:
        # git archive is produced by the trusted source repository; data
        # filtering additionally prevents a malformed archive from escaping
        # the destination directory.
        tar.extractall(dest, filter="data")

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=dest, check=True,
                       capture_output=True, text=True)

    git("init", "-q", "-b", base_branch)
    subprocess.run(
        ["git", "-c", "user.name=orchestrator benchmark",
         "-c", "user.email=benchmark@localhost", "add", "-A"],
        cwd=dest, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=orchestrator benchmark",
         "-c", "user.email=benchmark@localhost", "commit", "-qm",
         "public benchmark snapshot"],
        cwd=dest, check=True, capture_output=True, text=True,
    )
    return dest


def _materialize_tasks(conn, suite: BenchSuite, repo: str, default_max_retries: int) -> dict[str, str]:
    task_ids: dict[str, str] = {}
    for task in suite.tasks:
        deps = [task_ids[d] for d in task.depends_on]
        task_ids[task.key] = create_task(
            conn,
            task_id=f"bench-{task.key}",
            title=task.title,
            brief=task.brief,
            repo=repo,
            delivery_mode=task.delivery_mode,
            verify_cmd=task.verify_cmd or suite.verify_cmd,
            hidden_cmd=task.hidden_cmd or suite.hidden_cmd,
            setup_cmd=task.setup_cmd or suite.setup_cmd,
            protected_paths=task.protected_paths or suite.protected_paths,
            max_retries=task.max_retries if task.max_retries is not None else default_max_retries,
            depends_on=deps,
        )
    return task_ids


async def _run_orchestrator(
    conn, repo: Path, worktrees: Path, cfg: config.Config, max_concurrency: int,
    worker_model: str, supervisor_model: str, fake_worker: bool, fake_supervisor: bool,
    kill_one_after_s: float | None, base_branch: str, artifact_root: Path,
    run_id: str,
) -> None:
    spawn_worker = spawn_fake_worker if fake_worker else spawn_sdk_worker
    if fake_supervisor:
        from orchestrator.supervisor import always_escalate

        supervisor = always_escalate
    else:
        async def supervisor(packet):
            return await invoke_supervisor(packet, model=supervisor_model,
                                           artifact_root=artifact_root / "supervisor")

    scheduler = Scheduler(
        conn, repo, worktrees, max_concurrency=max_concurrency,
        stall_threshold_s=cfg.stall_threshold_s, verify_timeout_s=cfg.verify_timeout_s,
        spawn_worker=spawn_worker, worker_model=worker_model, supervisor=supervisor,
        max_nudges=cfg.max_nudges, wait_ceiling_s=cfg.wait_ceiling_s,
        transcript_tail_tokens=cfg.transcript_tail_tokens,
        base_branch=base_branch,
        artifact_root=artifact_root,
        run_id=run_id,
    )
    run_task = asyncio.create_task(scheduler.run_until_settled())
    killer = None
    if kill_one_after_s is not None:
        killer = asyncio.create_task(_kill_one_scheduler_worker(conn, scheduler, kill_one_after_s))
    try:
        await run_task
        _mark_recovered_faults(conn)
    finally:
        if killer:
            killer.cancel()
            await asyncio.gather(killer, return_exceptions=True)


async def _kill_one_scheduler_worker(conn, scheduler: Scheduler, delay_s: float) -> None:
    await asyncio.sleep(delay_s)
    while not scheduler._procs:
        await asyncio.sleep(0.05)
    task_id, proc = next(iter(scheduler._procs.items()))
    append_event(conn, source="system", type="bench.fault_injected", task_id=task_id,
                 payload={"kind": "kill_worker", "pid": proc.pid})
    try:
        os.killpg(proc.pid, 9)
    except ProcessLookupError:
        proc.kill()


async def _run_baseline(
    conn, repo: Path, worktrees: Path, cfg: config.Config, concurrency: int,
    worker_model: str, fake_worker: bool, kill_one_after_s: float | None,
    base_branch: str, artifact_root: Path, run_id: str,
) -> None:
    validate_dependency_graph(conn)
    pool = WorktreePool(repo, worktrees, concurrency)
    pool.open()
    running: dict[asyncio.Task, str] = {}
    killed = False
    kill_at = time.monotonic() + kill_one_after_s if kill_one_after_s is not None else None
    try:
        while True:
            advance_dependency_states(conn, run_id=run_id, block_needs_human=True)
            while len(running) < concurrency:
                # create_task() does not run the coroutine until this loop
                # yields, so a plain SELECT here can select the same queued
                # row once per available slot.  Reserve by task id in the
                # local running map before creating the coroutine; otherwise
                # parallel baseline sessions compete for one task/<id> branch.
                active_ids = set(running.values())
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE state = 'queued' ORDER BY created_at"
                ).fetchall()
                row = next((candidate for candidate in rows
                            if candidate["id"] not in active_ids), None)
                if row is None:
                    break
                task = asyncio.create_task(_run_baseline_task(
                    conn, dict(row), pool, cfg, worker_model, fake_worker, base_branch,
                    artifact_root,
                ))
                running[task] = row["id"]
            if not running:
                if _baseline_settled(conn):
                    return
                await asyncio.sleep(0.05)
                continue
            if kill_at is not None and not killed and time.monotonic() >= kill_at:
                killed = _kill_one_baseline_worker(conn)
            done, _ = await asyncio.wait(running, timeout=0.05,
                                         return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                running.pop(task)
                await task
    finally:
        await asyncio.gather(*running, return_exceptions=True)
        pool.close()


async def _run_baseline_task(
    conn, task: dict, pool: WorktreePool, cfg: config.Config,
    worker_model: str, fake_worker: bool, base_branch: str, artifact_root: Path,
) -> None:
    wt = None
    proc = None
    reaped = False
    task_id = task["id"]
    spawn_worker = spawn_fake_worker if fake_worker else spawn_sdk_worker
    try:
        wt, base_sha = await pool.acquire(task_id, base_branch=base_branch)
        worker_task = {**task, "execution_contract": build_execution_contract(task, str(wt))}
        proc = await spawn_worker(worker_task, wt, model=worker_model)
        s = append_event(conn, source="scheduler", type="worker.spawned",
                         task_id=task_id, session_id=str(proc.pid))
        transition(conn, task_id, "running", cause_seq=s,
                   session_id=str(proc.pid), worktree=str(wt), base_sha=base_sha)

        done = False
        while True:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=cfg.stall_threshold_s)
            except asyncio.TimeoutError:
                s = append_event(conn, source="watchdog", type="worker.stalled", task_id=task_id,
                                 session_id=str(proc.pid),
                                 payload={"silent_for_s": cfg.stall_threshold_s})
                _fail_running_or_triage(conn, task_id, s, "worker stalled without supervision")
                return
            if not line:
                break
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = rec.get("type")
            payload = rec.get("payload", {})
            usage_kwargs = {}
            if etype == "result":
                # ResultMessage is the complete session aggregate. Do not
                # count per-AssistantMessage usage a second time.
                usage_kwargs = dict(tokens_in=payload.get("tokens_in"),
                                    tokens_out=payload.get("tokens_out"),
                                    cost_usd=payload.get("cost_usd"))
            if etype == "done_claimed":
                s = append_event(conn, source="worker", type="worker.done_claimed",
                                 task_id=task_id, session_id=str(proc.pid), payload=payload,
                                 tokens_in=payload.get("tokens_in"),
                                 tokens_out=payload.get("tokens_out"),
                                 cost_usd=payload.get("cost_usd"))
                transition(conn, task_id, "verifying", cause_seq=s)
                # Hidden setup happens in run_verify's detached verifier
                # worktree.  Reap the worker first so no live process can
                # observe the verifier material.
                await _reap_worker(proc)
                reaped = True
                _verify_and_finish_baseline(conn, task_id, cfg, artifact_root)
                done = True
                return
            if etype == "startup_failed":
                category = str(payload.get("category") or "other_infrastructure_startup_failure")
                reason = str(payload.get("reason") or payload.get("error") or category)[:500]
                append_event(conn, source="worker", type="worker.startup_failed",
                             task_id=task_id, session_id=str(proc.pid),
                             payload={**payload, "category": category, "reason": reason},
                             **usage_kwargs)
                raise WorkerStartupFailure(task_id, category, reason)
            if etype == "asked":
                s = append_event(conn, source="worker", type="worker.asked",
                                 task_id=task_id, session_id=str(proc.pid), payload=payload)
                _fail_running_or_triage(conn, task_id, s, "worker asked for human help")
                return
            append_event(conn, source="worker", type=f"worker.{etype}", task_id=task_id,
                         session_id=str(proc.pid), payload=payload,
                         **usage_kwargs)

        if not done:
            code = await proc.wait()
            await _reap_worker(proc)
            reaped = True
            s = append_event(conn, source="worker", type="worker.exited", task_id=task_id,
                             session_id=str(proc.pid), payload={"exit_code": code})
            _fail_running_or_triage(conn, task_id, s, "worker exited without done claim")
    finally:
        if proc is not None:
            await _reap_worker(proc, terminate=not reaped)
        cleanup_worker_sandbox(proc)
        if wt is not None:
            pool.release(wt)


async def _reap_worker(proc, *, terminate: bool = True) -> None:
    if terminate:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        if not terminate:
            await proc.wait()
            return
        proc.kill()
        await proc.wait()


def _verify_and_finish_baseline(
    conn, task_id: str, cfg: config.Config, artifact_root: Path,
) -> None:
    task = dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
    req_kwargs = {}
    if task["protected_paths"]:
        req_kwargs["protected_paths"] = tuple(json.loads(task["protected_paths"]))
    req = VerifyRequest(
        task_id=task_id,
        worktree=task["worktree"],
        base_sha=task["base_sha"],
        verify_cmd=task["verify_cmd"] or "true",
        hidden_cmd=task["hidden_cmd"],
        setup_cmd=task["setup_cmd"],
        timeout_s=cfg.verify_timeout_s,
        repo=task["repo"],
        artifact_root=str(artifact_root / task_id),
        **req_kwargs,
    )
    append_event(conn, source="verifier", type="verify.started", task_id=task_id)
    result = run_verify(req)
    payload = {
        "cause": result.cause,
        "exit_code": result.exit_code,
        "duration_s": result.duration_s,
        "flaky": result.flaky,
        "output_tail": result.output_tail,
        "diff_stat": result.diff_stat,
        "tests_modified": result.tests_modified,
        "output_path": result.output_path,
        "patch_path": result.patch_path,
        "failure_signature": result.failure_signature,
    }
    if result.passed:
        s = append_event(conn, source="verifier", type="verify.passed",
                         task_id=task_id, payload=payload)
        transition(conn, task_id, "delivering", cause_seq=s)
        try:
            event_type, event_payload = delivery.deliver(
                task,
                artifact_root=str(artifact_root / task_id),
            )
        except delivery.DeliveryError as exc:
            s = append_event(conn, source="delivery", type="delivery.failed",
                             task_id=task_id, payload={"error": str(exc)})
            transition(conn, task_id, "triage", cause_seq=s)
            s = append_event(conn, source="system", type="bench.unsupervised_failed",
                             task_id=task_id, payload={"reason": "delivery_failed"})
            transition(conn, task_id, "failed", cause_seq=s)
            return

        s = append_event(conn, source="delivery", type=event_type,
                         task_id=task_id, payload=event_payload)
        transition(conn, task_id, "delivered", cause_seq=s)
        append_event(conn, source="system", type="bench.delivered", task_id=task_id)
        if conn.execute(
            "SELECT 1 FROM events WHERE task_id = ? AND type = 'bench.fault_injected'",
            (task_id,),
        ).fetchone():
            append_event(conn, source="system", type="bench.fault_recovered", task_id=task_id)
    else:
        s = append_event(conn, source="verifier", type="verify.failed",
                         task_id=task_id, payload=payload)
        transition(conn, task_id, "triage", cause_seq=s)
        s = append_event(conn, source="system", type="bench.unsupervised_failed",
                         task_id=task_id, payload={"reason": result.cause})
        transition(conn, task_id, "failed", cause_seq=s)


def _fail_running_or_triage(conn, task_id: str, cause_seq: int, reason: str) -> None:
    state = conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"]
    if state == "running":
        transition(conn, task_id, "triage", cause_seq=cause_seq)
    s = append_event(conn, source="system", type="bench.unsupervised_failed",
                     task_id=task_id, payload={"reason": reason})
    transition(conn, task_id, "failed", cause_seq=s)


def _advance_baseline_deps(conn) -> None:
    """Compatibility wrapper for callers/tests of the old baseline helper."""
    validate_dependency_graph(conn)
    advance_dependency_states(conn, run_id="baseline", block_needs_human=True)


def _baseline_settled(conn) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) c FROM tasks WHERE state NOT IN "
        "('needs_human', 'delivered', 'failed', 'cancelled', 'dependency_blocked')"
    ).fetchone()
    return row["c"] == 0


def _kill_one_baseline_worker(conn) -> bool:
    row = conn.execute(
        "SELECT id, session_id FROM tasks WHERE state = 'running' AND session_id IS NOT NULL "
        "ORDER BY created_at LIMIT 1"
    ).fetchone()
    if not row:
        return False
    try:
        os.killpg(int(row["session_id"]), 9)
    except ProcessLookupError:
        return False
    append_event(conn, source="system", type="bench.fault_injected", task_id=row["id"],
                 payload={"kind": "kill_worker", "pid": int(row["session_id"])})
    return True


def _mark_recovered_faults(conn) -> None:
    rows = conn.execute(
        "SELECT DISTINCT e.task_id FROM events e "
        "JOIN tasks t ON t.id = e.task_id "
        "WHERE e.type = 'bench.fault_injected' AND t.state = 'delivered'"
    ).fetchall()
    for row in rows:
        if not conn.execute(
            "SELECT 1 FROM events WHERE task_id = ? AND type = 'bench.fault_recovered'",
            (row["task_id"],),
        ).fetchone():
            append_event(conn, source="system", type="bench.fault_recovered",
                         task_id=row["task_id"])
