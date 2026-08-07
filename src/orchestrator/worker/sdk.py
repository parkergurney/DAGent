"""Spawns sdk_worker.py as a real subprocess -- same call shape as
spawn_fake_worker: (task, worktree) -> Process. The scheduler doesn't know or
care which backend it's driving; swap Scheduler(spawn_worker=...) to switch
from FakeWorker to real Agent SDK sessions.
"""
import asyncio
import os
import string
import sys
from pathlib import Path

from orchestrator.worker.sandbox import (
    WorkerSandboxUnavailable,
    prepare_worker_sandbox,
    register_worker_sandbox,
)

# src/orchestrator/worker/sdk.py -> src/. Not launched through an installed
# console entry point, so it needs this on PYTHONPATH to import `orchestrator`
# at all (see the identical note in fake.py).
_SRC = str(Path(__file__).resolve().parents[2])


async def spawn_sdk_worker(task: dict, worktree, *, model: str | None = None
                           ) -> asyncio.subprocess.Process:
    worktree = Path(worktree)
    # This preflight is deliberately before the brief is written or any
    # subprocess is created.  Unsupported hosts and malformed profiles fail
    # closed; there is no unsandboxed fallback for a real worker.
    sandbox = prepare_worker_sandbox(task["id"], worktree)
    # Keep the brief in the private allowlisted directory.  It must not be in
    # the worktree (where it would taint git status) or an arbitrary sibling
    # (which the outer Seatbelt deliberately cannot read).
    safe_task_id = "".join(
        char if char in string.ascii_letters + string.digits + "-_" else "_"
        for char in str(task["id"])
    )
    brief_file = sandbox.private_dir / f"{safe_task_id}.brief"
    brief_file.write_text(task["brief"])

    env = {**os.environ, "PYTHONPATH": os.pathsep.join([_SRC, os.environ.get("PYTHONPATH", "")])}
    env = sandbox.environment(env)
    # Validate Seatbelt application before launching a real SDK session. A
    # nonzero probe is a hard startup failure, never a reason to retry the
    # command without the outer profile.
    try:
        probe = await asyncio.create_subprocess_exec(
            *sandbox.command([sys.executable, "-c", "pass"]),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, cwd=str(worktree), env=env,
            start_new_session=True,
        )
        probe_code = await probe.wait()
    except Exception as exc:
        sandbox.cleanup()
        raise WorkerSandboxUnavailable(
            "Seatbelt probe could not start; refusing to run unsandboxed"
        ) from exc
    if probe_code != 0:
        sandbox.cleanup()
        raise WorkerSandboxUnavailable(
            f"Seatbelt probe failed with exit code {probe_code}; refusing to run unsandboxed"
        )
    args = [sys.executable, "-m", "orchestrator.worker.sdk_worker",
           "--task-id", task["id"], "--worktree", str(worktree),
           "--brief-file", str(brief_file)]
    if model:
        args += ["--model", model]

    try:
        proc = await asyncio.create_subprocess_exec(
            *sandbox.command(args),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(worktree),
            env=env,
            start_new_session=True,  # own process group, so teardown can killpg
        )
    except Exception:
        sandbox.cleanup()
        raise
    register_worker_sandbox(proc, sandbox)
    return proc
