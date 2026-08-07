"""Phase-two supervision policy regressions.

These tests use the real scheduler, worktree pool, verifier, and SQLite store.
The only fake is the supervisor callable, so a passing first attempt can prove
that no model boundary was crossed and recovery accounting can be inspected
as durable state.
"""
import asyncio
import json
import sys

from orchestrator.scheduler import Scheduler
from orchestrator.store import connect, create_task
from orchestrator.supervisor.llm import SupervisorResult
from orchestrator.supervisor.schema import Escalate, Nudge, Restart
from tests.helpers import init_repo


class CountingSupervisor:
    def __init__(self, *actions, tokens_in=11, tokens_out=7, cost_usd=0.12):
        self.actions = list(actions)
        self.calls = 0
        self.packets = []
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.cost_usd = cost_usd

    async def __call__(self, packet):
        self.calls += 1
        self.packets.append(packet)
        action = self.actions.pop(0)
        return SupervisorResult(
            action=action, ok=True, tokens_in=self.tokens_in, tokens_out=self.tokens_out,
            cost_usd=self.cost_usd, raw_text='{"action":"test"}',
        )


def _run(conn, repo, tmp_path, supervisor, **kwargs):
    scheduler = Scheduler(
        conn, repo, tmp_path / "worktrees", max_concurrency=1,
        stall_threshold_s=0.2, watchdog_interval_s=0.03, verify_timeout_s=10,
        supervisor=supervisor, **kwargs,
    )
    asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=30))


def _task(conn, repo, brief, *, verify_cmd="true", hidden_cmd=None, max_retries=2):
    return create_task(
        conn, title=brief, brief=brief, repo=str(repo), delivery_mode="scout",
        verify_cmd=verify_cmd, hidden_cmd=hidden_cmd, max_retries=max_retries,
    )


def _events(conn, task_id, event_type=None):
    sql = "SELECT * FROM events WHERE task_id = ?"
    args = [task_id]
    if event_type:
        sql += " AND type = ?"
        args.append(event_type)
    sql += " ORDER BY seq"
    return [dict(row) for row in conn.execute(sql, args)]


def _scripted_worker(*, exit_without_done=False):
    phases = {"n": 0}
    script = r'''
from pathlib import Path
import json
import subprocess
import sys

wt = Path(sys.argv[1])
phase = sys.argv[2]
if phase == "first":
    (wt / "failure_marker").write_text("one\n")
    (wt / "failure_reason.txt").write_text("reason-one\n")
else:
    (wt / "failure_reason.txt").write_text("reason-two\n")
(wt / ("first.txt" if phase == "first" else "second.txt")).write_text("x\n")
subprocess.run(["git", "-C", str(wt), "-c", "user.email=test@local", "-c", "user.name=test",
                "add", "-A"], check=True)
subprocess.run(["git", "-C", str(wt), "-c", "user.email=test@local", "-c", "user.name=test",
                "commit", "-qm", "candidate"], check=True)
if not sys.argv[3] == "exit":
    print(json.dumps({"type": "done_claimed", "payload": {"result": "DONE_CLAIM: test"}}), flush=True)
else:
    raise SystemExit(3)
'''

    async def spawn(task, worktree, *, model=None):
        del task, model
        phases["n"] += 1
        phase = "first" if phases["n"] == 1 else "second"
        return await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, str(worktree), phase,
            "exit" if exit_without_done else "done",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
        )

    return spawn


def test_successful_first_attempt_makes_zero_supervisor_calls(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _task(conn, repo, "clean")
    supervisor = CountingSupervisor()

    # The clean fake worker has no supervisor-triggering event.
    _run(conn, repo, tmp_path, supervisor)

    assert conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"] == "delivered"
    assert supervisor.calls == 0
    assert not _events(conn, task_id, "supervisor.invoked")
    assert conn.execute("SELECT COUNT(*) c FROM supervisor_interventions").fetchone()["c"] == 0


def test_first_public_failure_has_one_structured_intervention(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _task(conn, repo, "no_commit")
    supervisor = CountingSupervisor(Escalate(
        summary="public check failed", question="review the candidate", options=["review"],
        reason="actionable visible failure",
    ))

    _run(conn, repo, tmp_path, supervisor)

    assert supervisor.calls == 1
    attempt = conn.execute("SELECT * FROM attempts WHERE task_id = ?", (task_id,)).fetchone()
    assert attempt["failure_signature"]
    intervention = conn.execute(
        "SELECT * FROM supervisor_interventions WHERE task_id = ?", (task_id,)
    ).fetchone()
    assert intervention["source_attempt_id"] == attempt["id"]
    assert intervention["action_type"] == "ESCALATE_HUMAN"
    assert intervention["diagnosis_code"] == "public_failure_actionable"
    assert intervention["tokens_in"] == 11
    assert intervention["tokens_out"] == 7
    assert intervention["cost_usd"] == 0.12
    assert intervention["started_at"] and intervention["ended_at"]


def test_retry_inherits_candidate_guidance_and_records_recovery(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _task(
        conn, repo, "retry_candidate",
        verify_cmd="test ! -e retry_marker.txt && (test ! -e retry_solution.txt || "
                   "test \"$(cat retry_solution.txt)\" = fixed)",
    )
    supervisor = CountingSupervisor(Restart(
        feedback="repair the retained candidate", reason="visible check failed",
    ))

    _run(conn, repo, tmp_path, supervisor)

    attempts = conn.execute(
        "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_no", (task_id,)
    ).fetchall()
    assert len(attempts) == 2
    assert attempts[1]["parent_attempt_id"] == attempts[0]["id"]
    assert attempts[1]["base_sha"] == attempts[0]["candidate_sha"]
    assert "repair the retained candidate" in attempts[1]["execution_contract"]
    intervention = conn.execute(
        "SELECT * FROM supervisor_interventions WHERE task_id = ?", (task_id,)
    ).fetchone()
    assert intervention["target_attempt_id"] == attempts[1]["id"]
    assert intervention["child_candidate_sha"] == attempts[1]["candidate_sha"]
    assert intervention["verification_recovery_outcome"] == "improved"
    assert intervention["eventual_delivery_outcome"] == "delivered"
    assert conn.execute(
        "SELECT COUNT(*) c FROM events WHERE task_id = ? AND type = 'verification.recovered'",
        (task_id,),
    ).fetchone()["c"] == 1


def test_repeated_identical_failure_uses_policy_without_second_call(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _task(conn, repo, "no_commit")
    supervisor = CountingSupervisor(Restart(feedback=None, reason="retry once"))

    _run(conn, repo, tmp_path, supervisor)

    assert supervisor.calls == 1
    policy = _events(conn, task_id, "recovery.policy_applied")[-1]
    payload = json.loads(policy["payload"])
    assert payload["diagnosis_code"] == "repeated_identical_failure"
    assert payload["candidate_changed"] is False
    assert conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"] == "needs_human"


def test_material_candidate_change_allows_same_signature_decision(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _task(conn, repo, "custom", verify_cmd="test ! -e failure_marker")
    supervisor = CountingSupervisor(
        Restart(feedback="try a different fix", reason="first visible failure"),
        Escalate(summary="still failing", question="review", options=["review"], reason="new evidence"),
    )

    _run(conn, repo, tmp_path, supervisor, spawn_worker=_scripted_worker())

    assert supervisor.calls == 2
    evaluations = [_ for _ in _events(conn, task_id, "recovery.evaluated")]
    second = json.loads(evaluations[-1]["payload"])
    assert second["previous_failure_signature"] == second["failure_signature"]
    assert second["candidate_changed"] is True


def test_changed_signature_allows_another_decision_with_lineage_relation(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _task(
        conn, repo, "custom",
        verify_cmd="if test -e failure_marker; then cat failure_reason.txt >&2; exit 1; fi",
    )
    supervisor = CountingSupervisor(
        Restart(feedback="change the failing behavior", reason="first failure"),
        Escalate(summary="new public failure", question="review", options=["review"], reason="changed signature"),
    )

    _run(conn, repo, tmp_path, supervisor, spawn_worker=_scripted_worker())

    assert supervisor.calls == 2
    evaluation = json.loads(_events(conn, task_id, "recovery.evaluated")[-1]["payload"])
    assert evaluation["previous_failure_signature"] != evaluation["failure_signature"]
    assert evaluation["candidate_changed"] is True
    attempts = conn.execute(
        "SELECT id, parent_attempt_id, failure_signature FROM attempts WHERE task_id = ? ORDER BY attempt_no",
        (task_id,),
    ).fetchall()
    assert attempts[1]["parent_attempt_id"] == attempts[0]["id"]


def test_retry_budget_stops_changed_candidate_without_call(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _task(conn, repo, "custom", verify_cmd="test ! -e failure_marker", max_retries=1)
    supervisor = CountingSupervisor(Restart(feedback="retry", reason="visible failure"))

    _run(conn, repo, tmp_path, supervisor, spawn_worker=_scripted_worker())

    assert supervisor.calls == 1
    policy = json.loads(_events(conn, task_id, "recovery.policy_applied")[-1]["payload"])
    assert policy["diagnosis_code"] == "retry_budget_exhausted"


def test_opaque_evaluator_mismatch_escalates_truthfully_without_model_call(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _task(conn, repo, "clean", hidden_cmd="test -f missing-hidden-file")
    supervisor = CountingSupervisor()

    _run(conn, repo, tmp_path, supervisor)

    assert supervisor.calls == 0
    policy = json.loads(_events(conn, task_id, "recovery.policy_applied")[-1]["payload"])
    assert policy["diagnosis_code"] == "opaque_evaluator_mismatch"
    assert "hidden" not in policy["reason"]
    assert conn.execute("SELECT COUNT(*) c FROM supervisor_interventions").fetchone()["c"] == 0


def test_worker_ask_is_tied_to_attempt_and_assistance_is_accounted(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _task(conn, repo, "ask")
    supervisor = CountingSupervisor(Nudge(message="use the visible contract", reason="bounded answer"))

    _run(conn, repo, tmp_path, supervisor)

    attempt = conn.execute("SELECT id FROM attempts WHERE task_id = ?", (task_id,)).fetchone()
    asked = _events(conn, task_id, "worker.asked")[0]
    assert json.loads(asked["payload"])["attempt_id"] == attempt["id"]
    intervention = conn.execute(
        "SELECT source_attempt_id, action_type FROM supervisor_interventions WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    assert intervention["source_attempt_id"] == attempt["id"]
    assert intervention["action_type"] == "NUDGE"


def test_unexpected_exit_preserves_committed_candidate_lineage(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _task(conn, repo, "custom")
    supervisor = CountingSupervisor(Escalate(
        summary="worker exited", question="review candidate", options=["review"], reason="terminal worker failure",
    ))

    _run(conn, repo, tmp_path, supervisor, spawn_worker=_scripted_worker(exit_without_done=True))

    attempt = conn.execute("SELECT * FROM attempts WHERE task_id = ?", (task_id,)).fetchone()
    assert attempt["candidate_sha"] != attempt["base_sha"]
    intervention = conn.execute(
        "SELECT source_candidate_sha FROM supervisor_interventions WHERE task_id = ?", (task_id,)
    ).fetchone()
    assert intervention["source_candidate_sha"] == attempt["candidate_sha"]


def test_intervention_outcome_and_accounting_are_reconstructable(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _task(
        conn, repo, "retry_candidate",
        verify_cmd="test ! -e retry_marker.txt && (test ! -e retry_solution.txt || "
                   "test \"$(cat retry_solution.txt)\" = fixed)",
    )
    supervisor = CountingSupervisor(Restart(feedback="repair", reason="visible failure"))

    _run(conn, repo, tmp_path, supervisor)

    row = conn.execute(
        "SELECT source_attempt_id, source_candidate_sha, source_failure_signature, action_type, "
        "target_attempt_id, child_candidate_sha, child_failure_signature, eventual_delivery_outcome, "
        "verification_recovery_outcome, tokens_in, tokens_out, cost_usd FROM supervisor_interventions "
        "WHERE task_id = ?", (task_id,),
    ).fetchone()
    for key in ("source_attempt_id", "source_candidate_sha", "source_failure_signature",
                "action_type", "target_attempt_id", "child_candidate_sha", "eventual_delivery_outcome",
                "verification_recovery_outcome", "tokens_in", "tokens_out", "cost_usd"):
        assert row[key] is not None
    assert row["action_type"] == "RETRY"
    assert row["eventual_delivery_outcome"] == "delivered"
    assert row["verification_recovery_outcome"] == "improved"
