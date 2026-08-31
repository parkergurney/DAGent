"""Focused proofs for the deterministic Phase 1 reliability contract."""

import asyncio
import json
import subprocess

from dagent import execution_lease
from dagent.scheduler import Scheduler
from dagent.store import append_event, connect, create_attempt, create_task, transition
from tests.helpers import init_repo


def test_stale_attempt_output_is_fenced_after_replacement_lease(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = create_task(
        conn, title="stale", brief="clean", repo=str(repo),
        delivery_mode="scout", verify_cmd="true",
    )
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    first = create_attempt(
        conn, task_id=task_id, run_id="run", attempt_no=1, base_sha=base_sha,
        candidate_branch="attempt/first", execution_contract="public",
    )
    old_lease = execution_lease.acquire(conn, first, "scheduler:old", ttl_s=None)
    execution_lease.recover(conn, old_lease, reason="replacement")
    second = create_attempt(
        conn, task_id=task_id, run_id="run", attempt_no=2, parent_attempt_id=first,
        base_sha=base_sha, candidate_branch="attempt/second", execution_contract="public",
    )
    new_lease = execution_lease.acquire(conn, second, "scheduler:new", ttl_s=None)

    scheduler = Scheduler(conn, repo, tmp_path / "worktrees")
    scheduler._leases[task_id] = new_lease

    assert not scheduler._validate_worker_lease(
        task_id, expected_attempt_id=first, expected_lease=old_lease,
        event_type="done_claimed",
    )
    task_row = conn.execute(
        "SELECT current_attempt_id, candidate_sha, state FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    assert tuple(task_row) == (None, None, "blocked")
    rejected = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND type = 'worker.event_rejected'",
        (task_id,),
    ).fetchone()
    payload = json.loads(rejected["payload"])
    assert payload["attempt_id"] == first
    assert payload["current_attempt_id"] is None
    assert payload["reason"] == "worker attempt is no longer current"


def test_duplicate_triage_signal_does_not_repeat_supervision(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = create_task(
        conn, title="crash", brief="crash", repo=str(repo),
        delivery_mode="scout", verify_cmd="true",
    )
    scheduler = Scheduler(
        conn, repo, tmp_path / "worktrees", max_concurrency=1,
        stall_threshold_s=1, watchdog_interval_s=.05,
    )
    asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=10))

    cause = conn.execute(
        "SELECT seq FROM events WHERE task_id = ? AND type = 'worker.exited' "
        "ORDER BY seq LIMIT 1", (task_id,),
    ).fetchone()["seq"]
    before = {
        event_type: conn.execute(
            "SELECT COUNT(*) AS count FROM events WHERE task_id = ? AND type = ?",
            (task_id, event_type),
        ).fetchone()["count"]
        for event_type in ("supervisor.invoked", "supervisor.acted", "task.state_changed")
    }

    assert asyncio.run(scheduler._handle_triage(task_id, cause, live_proc=None)) is False
    after = {
        event_type: conn.execute(
            "SELECT COUNT(*) AS count FROM events WHERE task_id = ? AND type = ?",
            (task_id, event_type),
        ).fetchone()["count"]
        for event_type in before
    }
    assert after == before
    assert conn.execute(
        "SELECT state FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["state"] == "needs_human"


def test_duplicate_exit_and_watchdog_signals_are_ignored_per_attempt(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = create_task(
        conn, title="duplicate", brief="clean", repo=str(repo),
        delivery_mode="scout", verify_cmd="true",
    )
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    attempt_id = create_attempt(
        conn, task_id=task_id, run_id="run", attempt_no=1, base_sha=base_sha,
        candidate_branch="attempt/duplicate", execution_contract="public",
    )
    cause = append_event(conn, source="scheduler", type="dep.satisfied", task_id=task_id)
    transition(conn, task_id, "queued", cause_seq=cause)
    cause = append_event(conn, source="scheduler", type="worker.spawned", task_id=task_id)
    transition(
        conn, task_id, "running", cause_seq=cause, current_attempt_id=attempt_id,
        session_id="old-session",
    )
    scheduler = Scheduler(conn, repo, tmp_path / "worktrees")

    first_exit = scheduler._mark_running_failure(
        task_id, source="worker", event_type="worker.exited",
        payload={"exit_code": 17}, session_id="old-session",
    )
    duplicate_exit = scheduler._mark_running_failure(
        task_id, source="worker", event_type="worker.exited",
        payload={"exit_code": 17}, session_id="old-session",
    )
    first_stall = scheduler._mark_running_failure(
        task_id, source="watchdog", event_type="worker.stalled",
        payload={"silent_for_s": 1},
    )
    duplicate_stall = scheduler._mark_running_failure(
        task_id, source="watchdog", event_type="worker.stalled",
        payload={"silent_for_s": 2},
    )

    assert first_exit and duplicate_exit is None
    assert first_stall is None and duplicate_stall is None
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM events WHERE task_id = ? AND type = 'worker.exited'",
        (task_id,),
    ).fetchone()["count"] == 1
    assert conn.execute(
        "SELECT COUNT(*) AS count FROM events WHERE task_id = ? "
        "AND type IN ('worker.exited', 'worker.stalled')", (task_id,),
    ).fetchone()["count"] == 1
