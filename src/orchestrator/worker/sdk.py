"""Spawns sdk_worker.py as a real subprocess -- same call shape as
spawn_fake_worker: (task, worktree) -> Process. The scheduler doesn't know or
care which backend it's driving; swap Scheduler(spawn_worker=...) to switch
from FakeWorker to real Agent SDK sessions.
"""
import asyncio
import json
import os
import signal
import string
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from orchestrator.worker.sandbox import (
    WorkerSandboxUnavailable,
    cleanup_worker_sandbox,
    prepare_worker_sandbox,
    register_worker_sandbox,
)
from orchestrator.worker.contract import build_execution_contract
from orchestrator.worker.sdk_worker import _AUTH_FAILURE_MARKERS

# src/orchestrator/worker/sdk.py -> src/. Not launched through an installed
# console entry point, so it needs this on PYTHONPATH to import `orchestrator`
# at all (see the identical note in fake.py).
_SRC = str(Path(__file__).resolve().parents[2])


@dataclass(frozen=True)
class WorkerAuthSmokeResult:
    returncode: int
    event_types: tuple[str, ...]
    model_response: bool
    result_success: bool = False
    session_id: str | None = None
    startup_failure_category: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None


def _auth_failure_in_record(record: dict) -> bool:
    payload = record.get("payload") or {}
    text = " ".join(str(payload.get(key) or "") for key in ("text", "result", "reason", "error"))
    lowered = text.lower()
    return any(marker in lowered for marker in _AUTH_FAILURE_MARKERS)


async def _run_worker_auth_smoke(model: str, worktree: Path) -> WorkerAuthSmokeResult:
    task = {
        "id": "benchmark-auth-smoke",
        "title": "Benchmark worker authentication smoke test",
        "brief": (
            "Do not use tools or modify files. Reply with one short confirmation that "
            "the session is alive."
        ),
        "delivery_mode": "scout",
        "verify_cmd": "true",
    }
    proc = None
    try:
        proc = await spawn_sdk_worker(task, worktree, model=model)
        event_types = []
        records = []
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=120)
            if not line:
                break
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.append(record)
            event_types.append(str(record.get("type")))
        returncode = await proc.wait()
        result_record = next((r for r in reversed(records) if r.get("type") == "result"), None)
        result_payload = (result_record or {}).get("payload") or {}
        startup = next((r for r in records if r.get("type") == "startup_failed"), None)
        startup_payload = (startup or {}).get("payload") or {}
        auth_failure = any(_auth_failure_in_record(record) for record in records)
        result_success = bool(
            result_record
            and not result_payload.get("is_error")
            and result_payload.get("subtype") in ("success", "completion")
            and result_payload.get("session_id")
            and not auth_failure
            and "execution_started" in event_types
        )
        return WorkerAuthSmokeResult(
            returncode=returncode,
            event_types=tuple(event_types),
            model_response=result_success and bool(result_payload.get("result")),
            result_success=result_success,
            session_id=result_payload.get("session_id"),
            startup_failure_category=startup_payload.get("category"),
            tokens_in=result_payload.get("tokens_in"),
            tokens_out=result_payload.get("tokens_out"),
            cost_usd=result_payload.get("cost_usd"),
        )
    finally:
        if proc is not None:
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                await proc.wait()
            cleanup_worker_sandbox(proc)


def run_worker_auth_smoke_test(model: str) -> WorkerAuthSmokeResult:
    """Run one disposable model turn through the production worker launcher."""
    with tempfile.TemporaryDirectory(prefix="orchestrator-auth-smoke-") as raw_root:
        root = Path(raw_root)
        subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True,
                       capture_output=True, text=True)
        (root / "README.md").write_text("benchmark authentication smoke worktree\n")
        subprocess.run(
            ["git", "-C", str(root), "add", "README.md"], check=True,
            capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "-c", "user.name=orchestrator smoke",
             "-c", "user.email=smoke@localhost", "commit", "-qm", "smoke"],
            check=True, capture_output=True, text=True,
        )
        return asyncio.run(_run_worker_auth_smoke(model, root))


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
    brief_file.write_text(task.get("execution_contract") or
                          build_execution_contract(task, str(worktree)))

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
