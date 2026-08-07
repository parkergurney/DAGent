"""Spawns fake_worker.py as a real subprocess -- the M2 stand-in for the
Agent SDK session M3 will spawn instead. Same call shape (task, worktree) ->
Process, so the scheduler doesn't change when M3 swaps this out.
"""
import asyncio
import os
import sys
from pathlib import Path

# src/orchestrator/worker/fake.py -> src/. The subprocess isn't launched
# through an installed console entry point, so it needs this on PYTHONPATH
# to import `orchestrator` at all -- pytest's `pythonpath` ini setting only
# reaches this (parent) process, not children.
_SRC = str(Path(__file__).resolve().parents[2])


async def spawn_fake_worker(task: dict, worktree, *, model: str | None = None
                            ) -> asyncio.subprocess.Process:
    # model is part of the spawn_worker interface (Scheduler passes it
    # uniformly to whichever backend it's driving) but FakeWorker has no use
    # for it.
    del model
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([_SRC, os.environ.get("PYTHONPATH", "")])}
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "orchestrator.worker.fake_worker",
        "--scenario", task.get("_fake_scenario", task["brief"]),
        "--worktree", str(worktree),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=str(worktree),
        env=env,
        start_new_session=True,  # own process group, so teardown can killpg
    )
