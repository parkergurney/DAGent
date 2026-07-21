"""M2 exit criterion: "kill -9 the orchestrator at arbitrary points -> clean
reconcile on restart" (design.md section 11). We can't literally SIGKILL the
test process, so we reproduce what that leaves behind: a task stuck in
'running' whose session_id (pid) is no longer a live process, on a real
on-disk db a fresh connection reopens -- exactly what a restarted orchestrator
sees.
"""
import asyncio
import subprocess
import sys

from orchestrator.scheduler import Scheduler, reconcile
from orchestrator.store import append_event, connect, create_task, transition
from tests.helpers import init_repo


def _dead_pid() -> str:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return str(p.pid)  # exited; guaranteed not alive (barring pid reuse mid-test)


def _stick_task_in_running(conn, repo, session_id, worktree) -> str:
    task_id = create_task(conn, title="orphan", brief="clean", repo=str(repo),
                          delivery_mode="scout", verify_cmd="true")
    s = append_event(conn, source="scheduler", type="dep.satisfied", task_id=task_id)
    transition(conn, task_id, "queued", cause_seq=s)
    s = append_event(conn, source="scheduler", type="worker.spawned", task_id=task_id,
                     session_id=session_id)
    transition(conn, task_id, "running", cause_seq=s, session_id=session_id, worktree=str(worktree))
    return task_id


def test_reconcile_routes_orphaned_task_through_triage(tmp_path):
    repo = init_repo(tmp_path)
    db = tmp_path / "orch.db"
    conn = connect(str(db))
    task_id = _stick_task_in_running(conn, repo, _dead_pid(), tmp_path)

    # simulate the crash: drop the connection without any teardown, then
    # reopen it fresh -- what a restarted orchestrator process would do.
    conn.close()
    conn = connect(str(db))
    reconcile(conn)

    row = conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["state"] == "triage"
    types = [r["type"] for r in conn.execute(
        "SELECT type FROM events WHERE task_id = ? ORDER BY seq", (task_id,))]
    assert types[-2:] == ["worker.exited", "task.state_changed"]


def test_reconcile_leaves_live_running_task_alone(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        task_id = _stick_task_in_running(conn, repo, str(live.pid), tmp_path)
        reconcile(conn)
        row = conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
        assert row["state"] == "running"
    finally:
        live.kill()
        live.wait()


def test_full_restart_drains_reconciled_task_to_needs_human(tmp_path):
    repo = init_repo(tmp_path)
    db = tmp_path / "orch.db"
    conn = connect(str(db))
    task_id = _stick_task_in_running(conn, repo, _dead_pid(), tmp_path)
    conn.close()

    # "restart": a brand new connection and a brand new Scheduler.
    conn = connect(str(db))
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    sched = Scheduler(conn, repo, worktree_root, stall_threshold_s=0.3, watchdog_interval_s=0.05)
    asyncio.run(asyncio.wait_for(sched.run_until_settled(), timeout=10))

    row = conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["state"] == "needs_human"
