"""Focused local proof cases for the reliability guarantees."""
import asyncio
import json

from dagent.recovery import (
    FailureClass, RecoveryAction, choose_recovery, classify_failure,
)
from dagent.scheduler import Scheduler
from dagent.store import connect, create_task, replay
from tests.helpers import init_repo


def _run(conn, repo, tmp_path, **kwargs):
    scheduler = Scheduler(conn, repo, tmp_path / "worktrees", max_concurrency=2,
                          verify_timeout_s=10, watchdog_interval_s=0.05, **kwargs)
    asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=20))


def test_recovery_matrix_is_typed_and_bounded():
    assert classify_failure("verify.failed", {"cause": "empty_diff"}) is FailureClass.EMPTY_DIFF
    decision = choose_recovery(FailureClass.PROTOCOL_INCOMPLETE, retries=0, max_retries=2)
    assert decision.action is RecoveryAction.REPAIR
    assert choose_recovery(FailureClass.PROTOCOL_INCOMPLETE, retries=1, max_retries=2,
                           protocol_retries=1).action is RecoveryAction.ESCALATE


def test_successful_sdk_result_without_claim_gets_one_protocol_repair(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = create_task(conn, title="protocol", brief="sdk_result_no_claim", repo=str(repo),
                          delivery_mode="scout", verify_cmd="true")
    _run(conn, repo, tmp_path, protocol_recovery_v2=True)

    attempts = conn.execute("SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_no",
                            (task_id,)).fetchall()
    assert len(attempts) == 2
    assert conn.execute(
        "SELECT COUNT(*) c FROM events WHERE task_id = ? AND type = 'recovery.attempted'",
        (task_id,),
    ).fetchone()["c"] == 1
    assert conn.execute(
        "SELECT COUNT(*) c FROM events WHERE task_id = ? AND type = 'worker.protocol_incomplete'",
        (task_id,),
    ).fetchone()["c"] >= 1
    assert conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"] == "needs_human"


def test_typed_artifact_gate_blocks_only_affected_descendant(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    first = create_task(conn, title="first", brief="clean", repo=str(repo),
                        delivery_mode="local", verify_cmd="true",
                        output_artifacts=["missing.json"])
    second = create_task(conn, title="second", brief="clean", repo=str(repo),
                         delivery_mode="local", verify_cmd="true", depends_on=[first])
    _run(conn, repo, tmp_path)

    assert conn.execute("SELECT state FROM tasks WHERE id = ?", (first,)).fetchone()["state"] == "needs_human"
    assert conn.execute("SELECT state FROM tasks WHERE id = ?", (second,)).fetchone()["state"] == "dependency_blocked"
    event = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND type = 'artifact.validation_failed'",
        (first,),
    ).fetchone()
    assert json.loads(event["payload"])["reason"] == "missing_output_artifact"


def test_adaptive_scheduler_records_critical_path_decision(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    root = create_task(conn, title="root", brief="clean", repo=str(repo),
                       delivery_mode="scout", verify_cmd="true")
    create_task(conn, title="child", brief="clean", repo=str(repo),
                delivery_mode="scout", verify_cmd="true", depends_on=[root])
    _run(conn, repo, tmp_path, adaptive_scheduling=True)
    row = conn.execute("SELECT payload FROM events WHERE type = 'scheduler.decision'").fetchone()
    payload = json.loads(row["payload"])
    assert payload["policy"] == "adaptive"
    assert payload["selected_task"] == root
    assert payload["candidate_scores"][root]["critical_path_depth"] >= 1


def test_replay_preserves_typed_task_contract(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    create_task(conn, title="typed", brief="clean", repo=str(repo), delivery_mode="scout",
                output_artifacts=["output.txt"], output_schema={"required": ["output.txt"]},
                input_contract={"required_artifacts": ["input.txt"]},
                node_verify_cmd="true", repair_policy={"max_repairs": 1})
    live = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM tasks")}
    assert replay(conn.execute("SELECT * FROM events ORDER BY seq")) == live
