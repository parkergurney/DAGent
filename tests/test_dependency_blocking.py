"""Deterministic dependency settlement and graph validation."""
import asyncio
import json

import pytest

from orchestrator.scheduler import (
    Scheduler,
    advance_dependency_states,
    validate_dependency_graph,
)
from orchestrator.store import append_event, connect, create_task, transition
from orchestrator.supervisor import always_escalate
from orchestrator.worker import spawn_fake_worker
from orchestrator.bench.report import format_table, summarize_db
from tests.helpers import init_repo


def _task(conn, repo, title, *, depends_on=(), brief="clean"):
    return create_task(
        conn, title=title, brief=brief, repo=str(repo), delivery_mode="scout",
        verify_cmd="true", depends_on=depends_on,
    )


def _state(conn, task_id):
    return conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"]


def _mark_terminal(conn, task_id, state):
    """Drive a task through legal transitions to a terminal unsuccessful state."""
    cause = append_event(conn, source="scheduler", type="dep.satisfied", task_id=task_id)
    transition(conn, task_id, "queued", cause_seq=cause)
    cause = append_event(conn, source="scheduler", type="worker.spawned", task_id=task_id)
    transition(conn, task_id, "running", cause_seq=cause)
    cause = append_event(conn, source="worker", type="worker.exited", task_id=task_id)
    transition(conn, task_id, "triage", cause_seq=cause)
    cause = append_event(conn, source="supervisor", type="supervisor.acted", task_id=task_id)
    transition(conn, task_id, state, cause_seq=cause)


def _mark_delivered(conn, task_id):
    cause = append_event(conn, source="scheduler", type="dep.satisfied", task_id=task_id)
    transition(conn, task_id, "queued", cause_seq=cause)
    cause = append_event(conn, source="scheduler", type="worker.spawned", task_id=task_id)
    transition(conn, task_id, "running", cause_seq=cause)
    cause = append_event(conn, source="worker", type="worker.done_claimed", task_id=task_id)
    transition(conn, task_id, "verifying", cause_seq=cause)
    cause = append_event(conn, source="verifier", type="verify.passed", task_id=task_id)
    transition(conn, task_id, "delivering", cause_seq=cause)
    cause = append_event(conn, source="delivery", type="delivery.report_written", task_id=task_id)
    transition(conn, task_id, "delivered", cause_seq=cause)


def test_failed_prerequisite_blocks_without_execution_or_cost(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    a = _task(conn, repo, "a")
    b = _task(conn, repo, "b", depends_on=[a])
    _mark_terminal(conn, a, "failed")

    assert advance_dependency_states(conn, run_id="run-1")
    assert _state(conn, b) == "dependency_blocked"
    event = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND type = 'dep.blocked'", (b,)
    ).fetchone()
    payload = json.loads(event["payload"])
    assert payload["run_id"] == "run-1"
    assert payload["blocking_prerequisites"] == [{"task_id": a, "state": "failed"}]
    assert conn.execute("SELECT COUNT(*) c FROM attempts WHERE task_id = ?", (b,)).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM supervisor_interventions WHERE task_id = ?", (b,)
    ).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) c FROM events WHERE task_id = ?", (b,)
    ).fetchone()["c"] == 0


def test_transitive_and_multiple_dependency_blocking(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    a = _task(conn, repo, "a")
    b = _task(conn, repo, "b", depends_on=[a])
    c = _task(conn, repo, "c", depends_on=[b])
    d = _task(conn, repo, "d")
    e = _task(conn, repo, "e", depends_on=[a, d])
    _mark_terminal(conn, a, "failed")
    _mark_delivered(conn, d)

    advance_dependency_states(conn, run_id="run-2")
    assert _state(conn, b) == _state(conn, c) == _state(conn, e) == "dependency_blocked"
    for task_id in (b, c, e):
        assert conn.execute(
            "SELECT COUNT(*) c FROM events WHERE task_id = ? AND type = 'dep.blocked'",
            (task_id,),
        ).fetchone()["c"] == 1


def test_recoverable_human_escalation_waits_and_success_unlocks(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    a = _task(conn, repo, "a")
    b = _task(conn, repo, "b", depends_on=[a])
    _mark_terminal(conn, a, "needs_human")

    advance_dependency_states(conn, run_id="daemon", block_needs_human=False)
    assert _state(conn, b) == "blocked"
    _mark_delivered(conn, a)  # models a later human answer and successful run
    advance_dependency_states(conn, run_id="daemon", block_needs_human=False)
    assert _state(conn, b) == "queued"


def test_run_terminates_after_five_escalations_and_one_blocked_task(tmp_path):
    """The six-task shape from the previous Arrow run must not hang."""
    repo = init_repo(tmp_path)
    conn = connect()
    upstream = [_task(conn, repo, f"upstream-{i}", brief="ask") for i in range(5)]
    downstream = _task(conn, repo, "downstream", depends_on=[upstream[0]])

    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    scheduler = Scheduler(
        conn, repo, worktrees, max_concurrency=5, spawn_worker=spawn_fake_worker,
        supervisor=always_escalate, stall_threshold_s=2, watchdog_interval_s=0.05,
    )
    asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=10))

    assert all(_state(conn, task_id) == "needs_human" for task_id in upstream)
    assert _state(conn, downstream) == "dependency_blocked"
    assert conn.execute(
        "SELECT COUNT(*) c FROM events WHERE task_id = ? AND type = 'worker.spawned'",
        (downstream,),
    ).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM supervisor_interventions WHERE task_id = ?", (downstream,)
    ).fetchone()["c"] == 0


def test_cycle_and_missing_prerequisite_fail_preflight(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    a = _task(conn, repo, "a")
    b = _task(conn, repo, "b")
    conn.execute("INSERT INTO task_deps (task_id, depends_on) VALUES (?, ?)", (a, b))
    conn.execute("INSERT INTO task_deps (task_id, depends_on) VALUES (?, ?)", (b, a))
    with pytest.raises(ValueError, match="cycle"):
        validate_dependency_graph(conn)

    conn = connect()
    a = _task(conn, repo, "a")
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO task_deps (task_id, depends_on) VALUES (?, ?)", (a, "missing")
    )
    with pytest.raises(ValueError, match="missing prerequisite"):
        validate_dependency_graph(conn)


def test_report_distinguishes_dependency_blocked_and_infrastructure_abort(tmp_path):
    db = tmp_path / "run.db"
    conn = connect(str(db))
    a = _task(conn, tmp_path, "a")
    _task(conn, tmp_path, "b", depends_on=[a])
    _mark_terminal(conn, a, "failed")
    append_event(conn, source="system", type="bench.run_started", payload={"run_id": "r"})
    advance_dependency_states(conn, run_id="r")
    conn.close()

    summary = summarize_db(db)
    assert summary.failed == 1
    assert summary.dependency_blocked == 1
    assert summary.executed == 1
    assert summary.infrastructure_aborted == 1
    assert "dependency_blocked" in format_table([summary]).splitlines()[0]
