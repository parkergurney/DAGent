import asyncio
import json

import pytest

from dagent.scheduler import Scheduler
from dagent.store import connect, create_task
from dagent.supervisor.llm import SupervisorResult
from dagent.supervisor.schema import Escalate, Restart
from dagent.verify.gate import normalize_failure_signature
from tests.helpers import init_repo


class OneRestart:
    def __init__(self):
        self.calls = 0
        self.packets = []

    async def __call__(self, packet):
        self.calls += 1
        self.packets.append(packet)
        if self.calls == 1:
            action = Restart(feedback="repair the retained candidate", reason="public check failed")
        else:
            action = Escalate(summary="unexpected", question="review", options=["review"],
                              reason="test should have delivered")
        return SupervisorResult(action=action, ok=True, tokens_in=1, tokens_out=1,
                                cost_usd=0.001, raw_text=None)


def _events(conn, task_id):
    return [dict(row) for row in conn.execute(
        "SELECT * FROM events WHERE task_id = ? ORDER BY seq", (task_id,)
    )]


def _task(conn, repo):
    return create_task(conn, title="retry", brief="retry_candidate", repo=str(repo),
                       delivery_mode="scout",
                       verify_cmd="test ! -e retry_marker.txt && "
                                  "(test ! -e retry_solution.txt || "
                                  "test \"$(cat retry_solution.txt)\" = fixed)")


def _run(conn, repo, tmp_path, supervisor, **kwargs):
    scheduler = Scheduler(conn, repo, tmp_path / "worktrees", max_concurrency=1,
                           verify_timeout_s=10, supervisor=supervisor, **kwargs)
    asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=20))


def test_failed_candidate_is_parent_of_successful_retry(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _task(conn, repo)
    supervisor = OneRestart()

    _run(conn, repo, tmp_path, supervisor)

    attempts = conn.execute(
        "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_no", (task_id,)
    ).fetchall()
    assert len(attempts) == 2
    assert attempts[1]["parent_attempt_id"] == attempts[0]["id"]
    assert attempts[0]["candidate_sha"] != attempts[0]["base_sha"]
    assert attempts[1]["base_sha"] == attempts[0]["candidate_sha"]
    assert attempts[1]["candidate_sha"] != attempts[1]["base_sha"]
    assert attempts[1]["supervisor_feedback"] == "repair the retained candidate"
    assert attempts[0]["failure_cause"] == "tests_failed"
    assert attempts[0]["failure_signature"]
    assert attempts[1]["disposition"] == "delivered"
    assert conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"] == "delivered"
    assert conn.execute(
        "SELECT COUNT(*) c FROM events WHERE task_id = ? AND type = 'verification.recovered'",
        (task_id,),
    ).fetchone()["c"] == 1


def test_execution_contract_is_public_and_persisted(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _task(conn, repo)
    _run(conn, repo, tmp_path, OneRestart())
    contract = conn.execute(
        "SELECT execution_contract FROM attempts WHERE task_id = ? ORDER BY attempt_no LIMIT 1",
        (task_id,),
    ).fetchone()["execution_contract"]
    assert "test ! -e retry_marker.txt" in contract
    assert "Run the visible verification command" in contract
    assert "evaluator" not in contract.lower()


def test_interruption_after_triage_reuses_persisted_action_and_candidate(tmp_path):
    repo = init_repo(tmp_path)
    db = tmp_path / "orchestrator.db"
    conn = connect(str(db))
    task_id = _task(conn, repo)
    supervisor = OneRestart()

    scheduler = Scheduler(conn, repo, tmp_path / "worktrees", max_concurrency=1,
                          verify_timeout_s=10, supervisor=supervisor)
    launches = 0
    reached_retry = asyncio.Event()

    async def interrupt_on_retry(task, *, retries=None):
        nonlocal launches
        launches += 1
        if launches == 2:
            reached_retry.set()
            await asyncio.sleep(3600)
        return await scheduler._original_launch(task, retries=retries)

    scheduler._original_launch = scheduler._launch
    scheduler._launch = interrupt_on_retry

    async def interrupt():
        run = asyncio.create_task(scheduler.run_until_settled())
        await asyncio.wait_for(reached_retry.wait(), timeout=10)
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run

    asyncio.run(interrupt())

    assert conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"] == "triage"
    parent = conn.execute(
        "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_no DESC LIMIT 1", (task_id,)
    ).fetchone()
    acted = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND type = 'supervisor.acted'",
        (task_id,),
    ).fetchone()
    assert json.loads(acted["payload"])["action"] == "restart"

    class MustNotInvoke:
        async def __call__(self, packet):
            raise AssertionError("durable supervisor action should have been reused")

    conn.close()
    conn = connect(str(db))
    _run(conn, repo, tmp_path, MustNotInvoke())
    attempts = conn.execute(
        "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_no", (task_id,)
    ).fetchall()
    assert len(attempts) == 2
    assert attempts[1]["parent_attempt_id"] == parent["id"]
    assert attempts[1]["base_sha"] == parent["candidate_sha"]
    assert attempts[1]["supervisor_feedback"] == "repair the retained candidate"
    intervention = conn.execute(
        "SELECT * FROM supervisor_interventions WHERE task_id = ?", (task_id,)
    ).fetchone()
    assert intervention["target_attempt_id"] == attempts[1]["id"]
    assert conn.execute(
        "SELECT COUNT(*) c FROM supervisor_interventions WHERE task_id = ?", (task_id,)
    ).fetchone()["c"] == 1
    assert conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"] == "delivered"


def test_failure_signature_changes_and_equivalent_output_is_stable():
    first = normalize_failure_signature("tests_failed", "/tmp/run-a/project.py:10: AssertionError: expected 1\n")
    equivalent = normalize_failure_signature("tests_failed", "/private/tmp/run-b/project.py:99: AssertionError: expected 1\n")
    changed = normalize_failure_signature("tests_failed", "/tmp/run-c/project.py:10: AssertionError: expected 2\n")
    assert first == equivalent
    assert first != changed


def test_failed_retry_does_not_count_as_verification_recovery(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = create_task(conn, title="retry", brief="no_commit", repo=str(repo),
                          delivery_mode="scout", verify_cmd="true")

    class RestartThenAbandon:
        def __init__(self):
            self.n = 0

        async def __call__(self, packet):
            self.n += 1
            if self.n == 1:
                return SupervisorResult(
                    action=Restart(feedback=None, reason="retry"), ok=True,
                    tokens_in=1, tokens_out=1, cost_usd=0, raw_text=None,
                )
            return SupervisorResult(
                action=Escalate(summary="failed", question="review", options=["review"],
                                reason="still failed"), ok=True,
                tokens_in=1, tokens_out=1, cost_usd=0, raw_text=None,
            )

    _run(conn, repo, tmp_path, RestartThenAbandon())
    assert conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"] == "needs_human"
    assert conn.execute(
        "SELECT COUNT(*) c FROM events WHERE type = 'verification.recovered'"
    ).fetchone()["c"] == 0
