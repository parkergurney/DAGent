"""Spawns sdk_worker.py as a real subprocess -- same call shape as
spawn_fake_worker: (task, worktree) -> Process. The scheduler doesn't know or
care which backend it's driving; swap Scheduler(spawn_worker=...) to switch
from FakeWorker to real Agent SDK sessions.
"""
import asyncio
import os
import sys
from pathlib import Path

# src/orchestrator/worker/sdk.py -> src/. Not launched through an installed
# console entry point, so it needs this on PYTHONPATH to import `orchestrator`
# at all (see the identical note in fake.py).
_SRC = str(Path(__file__).resolve().parents[2])


async def spawn_sdk_worker(task: dict, worktree, *, model: str | None = None
                           ) -> asyncio.subprocess.Process:
    worktree = Path(worktree)
    # A sibling of the worktree, not inside it -- a file living in the
    # worktree would show up as an untracked change in the verify gate's
    # `git status --porcelain` preflight. sdk_worker.py deletes it once read.
    brief_file = worktree.parent / f"{task['id']}.brief"
    brief_file.write_text(task["brief"])

    env = {**os.environ, "PYTHONPATH": os.pathsep.join([_SRC, os.environ.get("PYTHONPATH", "")])}
    args = [sys.executable, "-m", "orchestrator.worker.sdk_worker",
           "--task-id", task["id"], "--worktree", str(worktree),
           "--brief-file", str(brief_file)]
    if model:
        args += ["--model", model]

    return await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(worktree),
        env=env,
        start_new_session=True,  # own process group, so teardown can killpg
    )
