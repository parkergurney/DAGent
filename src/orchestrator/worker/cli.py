"""Spawn the direct Claude Code CLI worker bridge."""

import asyncio
import os
import string
import sys
import tempfile
from pathlib import Path

from orchestrator.worker.contract import build_execution_contract


_SRC = str(Path(__file__).resolve().parents[2])


async def spawn_cli_worker(task: dict, worktree, *, model: str | None = None
                           ) -> asyncio.subprocess.Process:
    """Return a worker process with the same contract as FakeWorker/SDKWorker.

    The bridge owns the process group and launches the installed ``claude``
    executable directly. Its stdin remains raw nudge text for the scheduler.
    """
    worktree = Path(worktree)
    safe_task_id = "".join(
        char if char in string.ascii_letters + string.digits + "-_" else "_"
        for char in str(task["id"])
    )
    fd, raw_path = tempfile.mkstemp(prefix=f"orchestrator-{safe_task_id}-", suffix=".brief")
    os.close(fd)
    brief_path = Path(raw_path)
    brief_path.write_text(task.get("execution_contract") or
                          build_execution_contract(task, str(worktree)))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([_SRC, os.environ.get("PYTHONPATH", "")])}
    args = [sys.executable, "-m", "orchestrator.worker.cli_worker",
            "--task-id", task["id"], "--worktree", str(worktree),
            "--brief-file", str(brief_path)]
    if model:
        args += ["--model", model]
    try:
        return await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(worktree),
            env=env,
            start_new_session=True,
        )
    except Exception:
        brief_path.unlink(missing_ok=True)
        raise
