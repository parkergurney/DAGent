"""Spawn the direct Claude Code CLI worker bridge.

This adapter does not provide an OS sandbox.  Direct host execution is only
supported as explicit trusted development mode; Harbor/container isolation
must be supplied by the caller for benchmark runs.
"""

import asyncio
import os
import string
import sys
import tempfile
from pathlib import Path

from orchestrator.worker.contract import build_execution_contract

_SRC = str(Path(__file__).resolve().parents[2])
_AUTH_ENV = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "AWS_REGION", "AWS_DEFAULT_REGION",
})


def _safe_task_id(task_id: str) -> str:
    return "".join(
        char if char in string.ascii_letters + string.digits + "-_" else "_"
        for char in str(task_id)
    )


async def spawn_cli_worker(task: dict, worktree, *, model: str | None = None,
                           env: dict[str, str] | None = None
                           ) -> asyncio.subprocess.Process:
    """Launch the installed Claude CLI in a supervised process group."""
    worktree = Path(worktree)
    fd, name = tempfile.mkstemp(prefix=f"orch-{_safe_task_id(task['id'])}-", suffix=".brief")
    os.close(fd)
    brief_path = Path(name)
    brief_path.write_text(task.get("execution_contract") or
                          build_execution_contract(task, str(worktree)))
    child_env = {**os.environ}
    if env is not None:
        for name in _AUTH_ENV:
            child_env.pop(name, None)
        child_env.update(env)
    child_env["PYTHONPATH"] = os.pathsep.join(
        part for part in (_SRC, child_env.get("PYTHONPATH", "")) if part
    )
    args = [sys.executable, "-m", "orchestrator.worker.cli_worker",
            "--task-id", task["id"], "--worktree", str(worktree),
            "--brief-file", str(brief_path)]
    if model:
        args += ["--model", model]
    try:
        return await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, cwd=str(worktree), env=child_env,
            start_new_session=True,
        )
    except Exception:
        brief_path.unlink(missing_ok=True)
        raise
