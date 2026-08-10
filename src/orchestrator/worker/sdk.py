"""Spawn a real Agent SDK worker with caller-controlled environment.

The scheduler owns process-group lifecycle.  This launcher only adapts the
SDK worker to the common JSON-lines worker contract; Harbor supplies the outer
task isolation and any environment needed by the worker.  It does not provide
an OS sandbox, protect the host filesystem, or access authentication stores.
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


async def spawn_sdk_worker(task: dict, worktree, *, model: str | None = None,
                           env: dict[str, str] | None = None
                           ) -> asyncio.subprocess.Process:
    """Return an SDK worker process in its own process group.

    ``env`` is merged for this child only and is never persisted by the
    orchestrator.  In particular, authentication remains the caller/runtime's
    responsibility; this code does not inspect credentials or the Keychain.
    """
    worktree = Path(worktree)
    brief_fd, brief_name = tempfile.mkstemp(
        prefix=f"orch-{_safe_task_id(task['id'])}-", suffix=".brief"
    )
    os.close(brief_fd)
    brief_path = Path(brief_name)
    brief_path.write_text(task.get("execution_contract") or
                          build_execution_contract(task, str(worktree)))
    child_env = {**os.environ}
    if env is not None:
        # Harbor supplies authentication explicitly to this launcher.  Keep
        # ordinary process configuration (PATH, locale, etc.) but do not
        # accidentally pass unrelated credential variables inherited by the
        # in-container orchestrator process to a worker.
        for name in _AUTH_ENV:
            child_env.pop(name, None)
        child_env.update(env)
    child_env["PYTHONPATH"] = os.pathsep.join(
        part for part in (_SRC, child_env.get("PYTHONPATH", "")) if part
    )
    args = [sys.executable, "-m", "orchestrator.worker.sdk_worker",
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
