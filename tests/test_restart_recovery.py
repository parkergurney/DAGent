"""Phase 2 restart checkpoints using the deterministic FakeWorker backend."""

import asyncio
import subprocess

import pytest

from orchestrator import execution_lease
from orchestrator.scheduler import Scheduler, reconcile
from orchestrator.store import append_event, connect, create_attempt, create_task, replay, transition
from orchestrator.supervisor import always_escalate
from orchestrator.worker.fake import spawn_fake_worker
from tests.helpers import init_repo


def _graph(conn, repo):
    specs = {
        "a": (), "b": (), "c": (),
        "d": ("a",), "e": ("a", "b"), "f": ("b",), "g": ("c",),
        "h": ("d", "e"), "i": ("f", "g"), "j": ("h", "i"),
    }
    return {
        task_id: create_task(
            conn, task_id=task_id, title=task_id, brief="clean", repo=str(repo),
            delivery_mode="scout", verify_cmd="true", depends_on=depends_on,
        )
        for task_id, depends_on in specs.items()
    }


def _scheduler(conn, repo, worktrees, **kwargs):
    return Scheduler(
        conn, repo, worktrees, max_concurrency=1, stall_threshold_s=.5,
        watchdog_interval_s=.05, verify_timeout_s=5, spawn_worker=spawn_fake_worker,
        **kwargs,
    )


async def _cancel_after(event, scheduler):
    run = asyncio.create_task(scheduler.run_until_settled())
    await asyncio.wait_for(event.wait(), timeout=15)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run


def test_ten_node_dag_restart_resumes_delivery_without_repeating_work(tmp_path):
    repo = init_repo(tmp_path)
    db = tmp_path / "restart.db"
    conn = connect(str(db))
    ids = _graph(conn, repo)
    paused = asyncio.Event()
    scheduler = _scheduler(conn, repo, tmp_path / "worktrees")
    original_deliver = scheduler._deliver
    original_launch_ready = scheduler._launch_ready

    async def pause_at_delivery(task_id):
        if task_id == "b" and not paused.is_set():
            paused.set()
            await asyncio.Event().wait()
        await original_deliver(task_id)

    async def hold_admission_while_checkpointed():
        if conn.execute("SELECT state FROM tasks WHERE id = 'b'").fetchone()["state"] in {
            "verifying", "delivering",
        }:
            await asyncio.Event().wait()
        await original_launch_ready()

    scheduler._deliver = pause_at_delivery
    scheduler._launch_ready = hold_admission_while_checkpointed
    asyncio.run(_cancel_after(paused, scheduler))

    assert conn.execute("SELECT state FROM tasks WHERE id = 'a'").fetchone()["state"] == "delivered"
    assert conn.execute("SELECT state FROM tasks WHERE id = 'b'").fetchone()["state"] == "delivering"
    assert conn.execute("SELECT state FROM tasks WHERE id = 'c'").fetchone()["state"] == "queued"
    conn.close()

    conn = connect(str(db))
    resumed = _scheduler(conn, repo, tmp_path / "worktrees")
    asyncio.run(asyncio.wait_for(resumed.run_until_settled(), timeout=30))

    assert {row["state"] for row in conn.execute("SELECT state FROM tasks")} == {"delivered"}
    for task_id in ids:
        counts = {
            event_type: conn.execute(
                "SELECT COUNT(*) AS count FROM events WHERE task_id = ? AND type = ?",
                (task_id, event_type),
            ).fetchone()["count"]
            for event_type in ("worker.spawned", "verify.passed", "delivery.report_written")
        }
        assert counts == {"worker.spawned": 1, "verify.passed": 1, "delivery.report_written": 1}
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM attempts WHERE task_id = ?", (task_id,)
        ).fetchone()["count"] == 1
    live = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM tasks")}
    assert replay(conn.execute("SELECT * FROM events ORDER BY seq")) == live


def test_restart_preserves_candidate_after_commit_before_verification(tmp_path):
    repo = init_repo(tmp_path)
    db = tmp_path / "candidate-checkpoint.db"
    conn = connect(str(db))
    task_id = create_task(
        conn, task_id="candidate", title="candidate", brief="clean", repo=str(repo),
        delivery_mode="scout", verify_cmd="true",
    )
    paused = asyncio.Event()
    scheduler = _scheduler(conn, repo, tmp_path / "worktrees")

    async def pause_verification(_task_id):
        paused.set()
        await asyncio.Event().wait()

    scheduler._run_verify = pause_verification
    asyncio.run(_cancel_after(paused, scheduler))
    checkpoint = conn.execute(
        "SELECT state, candidate_sha FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    attempt = conn.execute(
        "SELECT base_sha, candidate_sha FROM attempts WHERE task_id = ?", (task_id,)
    ).fetchone()
    assert checkpoint["state"] == "verifying"
    assert checkpoint["candidate_sha"] == attempt["candidate_sha"] != attempt["base_sha"]
    conn.close()

    conn = connect(str(db))
    resumed = _scheduler(conn, repo, tmp_path / "worktrees")
    asyncio.run(asyncio.wait_for(resumed.run_until_settled(), timeout=15))
    assert conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"] == "delivered"
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM events WHERE task_id = ? AND type = 'verify.passed'",
        (task_id,),
    ).fetchone()["count"] == 1
    assert conn.execute(
        "SELECT candidate_sha FROM attempts WHERE task_id = ?", (task_id,)
    ).fetchone()["candidate_sha"] == attempt["candidate_sha"]


def test_restart_during_verification_reuses_incomplete_checkpoint(tmp_path):
    repo = init_repo(tmp_path)
    db = tmp_path / "verification-checkpoint.db"
    conn = connect(str(db))
    task_id = create_task(
        conn, task_id="verification", title="verification", brief="clean", repo=str(repo),
        delivery_mode="scout", verify_cmd="true",
    )
    paused = asyncio.Event()
    scheduler = _scheduler(conn, repo, tmp_path / "worktrees")
    async def pause_verification(_task_id):
        paused.set()
        await asyncio.Event().wait()

    scheduler._run_verify = pause_verification
    asyncio.run(_cancel_after(paused, scheduler))
    assert conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"] == "verifying"
    attempt_id = conn.execute(
        "SELECT current_attempt_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["current_attempt_id"]
    append_event(
        conn, source="verifier", type="verify.started", task_id=task_id,
        payload={"attempt_id": attempt_id},
    )
    conn.close()

    conn = connect(str(db))
    resumed = _scheduler(conn, repo, tmp_path / "worktrees")
    asyncio.run(asyncio.wait_for(resumed.run_until_settled(), timeout=15))
    assert conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"] == "delivered"
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM events WHERE task_id = ? AND type = 'verify.passed'",
        (task_id,),
    ).fetchone()["count"] == 1


def test_restart_during_triage_does_not_duplicate_supervisor_recovery(tmp_path):
    repo = init_repo(tmp_path)
    db = tmp_path / "triage-checkpoint.db"
    conn = connect(str(db))
    task_id = create_task(
        conn, task_id="triage", title="triage", brief="crash", repo=str(repo),
        delivery_mode="scout", verify_cmd="true",
    )
    invoked = asyncio.Event()
    release = asyncio.Event()

    async def blocking_supervisor(_packet):
        invoked.set()
        await release.wait()
        return None

    scheduler = _scheduler(conn, repo, tmp_path / "worktrees", supervisor=blocking_supervisor)

    async def run_and_cancel():
        run = asyncio.create_task(scheduler.run_until_settled())
        await asyncio.wait_for(invoked.wait(), timeout=15)
        assert conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"] == "triage"
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run

    asyncio.run(run_and_cancel())
    conn.close()

    conn = connect(str(db))
    resumed = _scheduler(conn, repo, tmp_path / "worktrees", supervisor=always_escalate)
    asyncio.run(asyncio.wait_for(resumed.run_until_settled(), timeout=15))
    assert conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"] == "needs_human"
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM events WHERE task_id = ? AND type = 'supervisor.invoked'",
        (task_id,),
    ).fetchone()["count"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM supervisor_interventions WHERE task_id = ?", (task_id,)
    ).fetchone()["count"] == 1


def test_reconcile_recovers_orphaned_worker_lease(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = create_task(
        conn, title="lease", brief="clean", repo=str(repo),
        delivery_mode="scout", verify_cmd="true",
    )
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    attempt_id = create_attempt(
        conn, task_id=task_id, run_id="run", attempt_no=1, base_sha=base_sha,
        candidate_branch="attempt/lease", execution_contract="public",
    )
    lease = execution_lease.acquire(conn, attempt_id, "scheduler:old", ttl_s=None)
    cause = append_event(conn, source="scheduler", type="dep.satisfied", task_id=task_id)
    transition(conn, task_id, "queued", cause_seq=cause)
    cause = append_event(conn, source="scheduler", type="worker.spawned", task_id=task_id)
    transition(
        conn, task_id, "running", cause_seq=cause, current_attempt_id=attempt_id,
        session_id="99999999",
    )

    reconcile(conn)

    assert conn.execute(
        "SELECT status FROM execution_leases WHERE lease_id = ?", (lease.lease_id,)
    ).fetchone()["status"] == "recovered"
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM events WHERE type = 'execution_lease.recovered'"
    ).fetchone()["count"] == 1
