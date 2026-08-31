"""Worker/verify ordering checks independent of the live Claude SDK."""

import asyncio
import os
import sys

from dagent.scheduler import Scheduler
from dagent.store import connect, create_task
from dagent.verify import gate
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

    async def capture_spawn(*args, **kwargs):
        proc = await spawn_worker(*args, **kwargs)
        seen["proc"] = proc
        return proc

    monkeypatch.setattr("dagent.scheduler.core.run_verify", verify)
    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    scheduler = Scheduler(
        conn, repo, worktrees, max_concurrency=1, spawn_worker=capture_spawn,
        stall_threshold_s=10, watchdog_interval_s=0.1, verify_timeout_s=10,
    )
    asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=20))

    assert seen["reaped"]
    assert conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()["state"] == "delivered"
