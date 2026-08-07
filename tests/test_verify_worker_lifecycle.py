"""Worker/verify ordering checks independent of the live Claude SDK."""

import asyncio
import json
import os
import platform
import sys

import pytest

from orchestrator.scheduler import Scheduler
from orchestrator.store import connect, create_task
from orchestrator.verify import gate
from orchestrator.worker.sandbox import prepare_worker_sandbox, register_worker_sandbox
from tests.helpers import init_repo


def test_worker_is_reaped_before_verification_starts(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = create_task(
        conn, title="long-lived done claim", brief="ignored", repo=str(repo),
        delivery_mode="scout", verify_cmd="true",
    )
    seen = {}

    async def spawn_worker(task, worktree, *, model=None):
        del task, model
        script = (
            "from pathlib import Path; import subprocess, time, json; "
            "Path('public.txt').write_text('public\\n'); "
            "subprocess.run(['git','-c','user.name=t','-c','user.email=t@local','add','public.txt'], check=True); "
            "subprocess.run(['git','-c','user.name=t','-c','user.email=t@local','commit','-qm','change'], check=True); "
            "print(json.dumps({'type':'done_claimed','payload':{'result':'ok'}}), flush=True); "
            "time.sleep(60)"
        )
        return await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, cwd=str(worktree),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )

    def verify(req):
        del req
        proc = seen["proc"]
        try:
            os.kill(proc.pid, 0)
        except ProcessLookupError:
            seen["reaped"] = True
        else:
            seen["reaped"] = False
        return gate.VerifyResult(
            passed=True, cause="tests_passed", exit_code=0, duration_s=0,
            flaky=False, output_tail="", diff_stat="public.txt | 1 +",
        )

    original_spawn = spawn_worker

    async def capture_spawn(*args, **kwargs):
        proc = await original_spawn(*args, **kwargs)
        seen["proc"] = proc
        return proc

    monkeypatch.setattr("orchestrator.scheduler.core.run_verify", verify)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    scheduler = Scheduler(
        conn, repo, worktrees, max_concurrency=1, spawn_worker=capture_spawn,
        stall_threshold_s=10, watchdog_interval_s=0.1, verify_timeout_s=10,
    )
    asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=20))

    assert seen["reaped"]
    assert conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()["state"] == "delivered"


@pytest.mark.skipif(
    platform.system() != "Darwin" or os.environ.get("ORCH_LIVE_SANDBOX_TESTS") != "1",
    reason="requires an explicitly permitted macOS Seatbelt host",
)
def test_detached_worker_child_is_gone_before_verification(tmp_path, monkeypatch):
    """Seatbelt prevents process/session escape before verifier setup."""
    repo = init_repo(tmp_path)
    conn = connect()
    _task_id = create_task(conn, title="detached child", brief="ignored", repo=str(repo),
                           delivery_mode="scout", verify_cmd="true")
    seen = {}

    async def spawn_worker(task, worktree, *, model=None):
        del task, model
        sandbox = prepare_worker_sandbox("detached-child", worktree)
        status = sandbox.private_dir / "escape-status.json"
        escaped = sandbox.private_dir / "escaped-pids.json"
        script = f'''\
import json, os, time, subprocess
status = {str(status)!r}
escaped = {str(escaped)!r}
states = {{}}
def save():
    try:
        existing = json.load(open(status))
    except (FileNotFoundError, json.JSONDecodeError):
        existing = {{}}
    existing.update(states)
    open(status, "w").write(json.dumps(existing))
def child_escape(name, operation):
    pid = os.fork()
    if pid == 0:
        try:
            operation()
        except PermissionError:
            states[name] = "denied"
            save()
            os._exit(0)
        states[name] = "escaped"
        open(escaped, "a").write(str(os.getpid()) + "\\n")
        save()
        time.sleep(60)
        os._exit(0)
    os.waitpid(pid, 0)
child_escape("setsid", os.setsid)
child_escape("setpgid", lambda: os.setpgid(0, 0))
def setpgrp_escape():
    original = os.getpgrp()
    os.setpgrp()
    if os.getpgrp() == original:
        raise PermissionError("sandbox prevented a new process group")
child_escape("setpgrp", setpgrp_escape)
def double_fork():
    grandchild = os.fork()
    if grandchild == 0:
        os.setsid()
        os._exit(0)
    os._exit(0)
child_escape("double_fork_setsid", double_fork)
open("public.txt", "w").write("public\\n")
subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@local", "add", "public.txt"], check=True)
subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@local", "commit", "-qm", "change"], check=True)
print(json.dumps({{"type":"done_claimed","payload":{{"result":"ok"}}}}), flush=True)
time.sleep(60)
'''
        proc = await asyncio.create_subprocess_exec(
            *sandbox.command([sys.executable, "-c", script]), cwd=str(worktree),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
            env=sandbox.environment(os.environ),
        )
        register_worker_sandbox(proc, sandbox)
        seen["sandbox"] = sandbox
        return proc

    def verify(req):
        del req
        statuses = json.loads((seen["sandbox"].private_dir / "escape-status.json").read_text())
        seen["statuses"] = statuses
        escaped = seen["sandbox"].private_dir / "escaped-pids.json"
        seen["escaped"] = escaped.read_text() if escaped.exists() else ""
        return gate.VerifyResult(
            passed=True, cause="tests_passed", exit_code=0, duration_s=0,
            flaky=False, output_tail="", diff_stat="public.txt | 1 +",
        )

    async def capture_spawn(*args, **kwargs):
        proc = await spawn_worker(*args, **kwargs)
        seen["proc"] = proc
        seen["worktree"] = args[1]
        return proc

    monkeypatch.setattr("orchestrator.scheduler.core.run_verify", verify)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    scheduler = Scheduler(conn, repo, worktrees, max_concurrency=1,
                          spawn_worker=capture_spawn, stall_threshold_s=10,
                          watchdog_interval_s=0.1, verify_timeout_s=10)
    try:
        asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=20))
        assert "statuses" in seen, [dict(row) for row in conn.execute(
            "SELECT type, payload FROM events ORDER BY seq")]
        assert seen["statuses"] == {
            "setsid": "denied", "setpgid": "denied", "setpgrp": "denied",
            "double_fork_setsid": "denied",
        }
        assert not seen["escaped"]
    finally:
        conn.close()
