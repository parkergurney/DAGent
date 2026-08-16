"""Phase-three worker-capacity regressions.

These tests deliberately use real subprocess workers and a fake supervisor.
The supervisor waits are deterministic asyncio delays; no model behavior is
part of the scheduler timing assertions.
"""
import asyncio
import json
import subprocess
import sys

from orchestrator.scheduler import Scheduler
from orchestrator.store import append_event, connect, create_attempt, create_task, transition, ulid, update_attempt
from orchestrator.supervisor.llm import SupervisorResult
from orchestrator.supervisor.schema import Escalate, Restart
from orchestrator.worker.fake import spawn_fake_worker
from tests.helpers import init_repo


def _escalate(reason="review"):
    return Escalate(summary="review", question="what next?", options=["review"], reason=reason)


def _events(conn, task_id, event_type=None):
    sql = "SELECT * FROM events WHERE task_id = ?"
    args = [task_id]
    if event_type:
        sql += " AND type = ?"
        args.append(event_type)
    sql += " ORDER BY seq"
    return [dict(row) for row in conn.execute(sql, args)]


def _task(conn, repo, brief, *, verify_cmd="true", max_retries=2):
    return create_task(conn, title=brief, brief=brief, repo=str(repo), delivery_mode="scout",
                       verify_cmd=verify_cmd, max_retries=max_retries)


async def _slow_worker(task, worktree, *, model=None):
    del model
    if task["brief"] != "slow":
        return await spawn_fake_worker(task, worktree)
    script = (
        "from pathlib import Path; import subprocess, time, json; "
        "Path('slow.txt').write_text('slow\\n'); "
        "subprocess.run(['git','-c','user.name=t','-c','user.email=t@local','add','-A'], check=True); "
        "subprocess.run(['git','-c','user.name=t','-c','user.email=t@local','commit','-qm','slow'], check=True); "
        "time.sleep(.6); print(json.dumps({'type':'done_claimed','payload':{'result':'ok'}}), flush=True)"
    )
    return await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, cwd=str(worktree),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
    )


async def _sdk_result_then_done_worker(task, worktree, *, model=None):
    del task, model
    script = (
        "from pathlib import Path; import json, subprocess; "
        "Path('output.txt').write_text('done\\n'); "
        "subprocess.run(['git','-c','user.name=t','-c','user.email=t@local','add','-A'], check=True); "
        "subprocess.run(['git','-c','user.name=t','-c','user.email=t@local','commit','-qm','work'], check=True); "
        "print(json.dumps({'type':'result','payload':{'subtype':'success','is_error':False,"
        "'result':'completed','session_id':'sdk-session','cost_usd':0.123,"
        "'tokens_in':10,'tokens_out':20}}), flush=True); "
        "print(json.dumps({'type':'execution_started','payload':{'session_id':'sdk-session',"
        "'subtype':'success'}}), flush=True); "
        "print(json.dumps({'type':'done_claimed','payload':{'result':'ok'}}), flush=True)"
    )
    return await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, cwd=str(worktree),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
    )


def _run(conn, repo, tmp_path, supervisor, **kwargs):
    scheduler = Scheduler(conn, repo, tmp_path / "worktrees", max_concurrency=1,
                           stall_threshold_s=2, watchdog_interval_s=.05,
                           verify_timeout_s=10, supervisor=supervisor, **kwargs)
    asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=30))
    return scheduler


def test_successful_worker_releases_slot_before_verification(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _task(conn, repo, "clean")
    _run(conn, repo, tmp_path, lambda packet: None)

    events = _events(conn, task_id)
    released = next(e for e in events if e["type"] == "worker.slot_released")
    verify = next(e for e in events if e["type"] == "verify.started")
    assert released["seq"] < verify["seq"]
    assert json.loads(released["payload"])["occupancy"] == 0


def test_done_claim_preserves_usage_from_prior_sdk_result(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _task(conn, repo, "sdk result accounting")
    _run(conn, repo, tmp_path, lambda packet: None,
         spawn_worker=_sdk_result_then_done_worker)

    done = _events(conn, task_id, "worker.done_claimed")
    assert len(done) == 1
    assert done[0]["tokens_in"] == 10
    assert done[0]["tokens_out"] == 20
    assert done[0]["cost_usd"] == 0.123


def test_verification_failure_releases_slot_before_supervisor(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _task(conn, repo, "no_commit")
    seen = {}

    async def supervisor(packet):
        seen["leases"] = len(scheduler._worker_slots)
        return SupervisorResult(action=_escalate(), ok=True, tokens_in=0, tokens_out=0,
                                cost_usd=0, raw_text=None)

    scheduler = Scheduler(conn, repo, tmp_path / "worktrees", max_concurrency=1,
                          stall_threshold_s=2, watchdog_interval_s=.05,
                          verify_timeout_s=10, supervisor=supervisor)
    asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=30))
    events = _events(conn, task_id)
    released = next(e for e in events if e["type"] == "worker.slot_released")
    triage = next(e for e in events if e["type"] == "triage.started")
    assert released["seq"] < triage["seq"]
    assert seen["leases"] == 0


def test_worker_exit_releases_slot_before_supervisor(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _task(conn, repo, "crash")
    seen = {}

    async def supervisor(packet):
        seen["leases"] = len(scheduler._worker_slots)
        return SupervisorResult(action=_escalate(), ok=True, tokens_in=0, tokens_out=0,
                                cost_usd=0, raw_text=None)

    scheduler = Scheduler(conn, repo, tmp_path / "worktrees", max_concurrency=1,
                          stall_threshold_s=2, watchdog_interval_s=.05,
                          verify_timeout_s=10, supervisor=supervisor)
    asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=30))
    events = _events(conn, task_id)
    exited = next(e for e in events if e["type"] == "worker.exited")
    released = next(e for e in events if e["type"] == "worker.slot_released")
    triage = next(e for e in events if e["type"] == "triage.started")
    assert exited["seq"] < released["seq"] < triage["seq"]
    assert seen["leases"] == 0


def test_queued_worker_starts_during_long_supervisor_triage(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    first = _task(conn, repo, "crash")
    second = _task(conn, repo, "slow")
    third = _task(conn, repo, "clean")
    supervisor_started = asyncio.Event()
    release_supervisor = asyncio.Event()
    seen_peak = []

    async def supervisor(packet):
        if packet.task_id == first:
            supervisor_started.set()
            await release_supervisor.wait()
        return SupervisorResult(action=_escalate(), ok=True, tokens_in=0, tokens_out=0,
                                cost_usd=0, raw_text=None)

    scheduler = Scheduler(conn, repo, tmp_path / "worktrees", max_concurrency=2,
                          stall_threshold_s=2, watchdog_interval_s=.05,
                          verify_timeout_s=10, supervisor=supervisor,
                          spawn_worker=_slow_worker)

    async def drive():
        run = asyncio.create_task(scheduler.run_until_settled())
        await asyncio.wait_for(supervisor_started.wait(), timeout=10)
        for _ in range(100):
            if any(e["type"] == "worker.started" for e in _events(conn, third)):
                break
            await asyncio.sleep(.01)
        else:
            raise AssertionError("queued task did not start while triage was waiting")
        release_supervisor.set()
        await asyncio.wait_for(run, timeout=20)

    asyncio.run(drive())
    first_events = _events(conn, first)
    third_started = next(e for e in _events(conn, third) if e["type"] == "worker.started")
    first_finished = next(e for e in first_events if e["type"] == "triage.finished")
    assert third_started["seq"] < first_finished["seq"]
    for event in _events(conn, first) + _events(conn, second) + _events(conn, third):
        if event["type"] in ("worker.slot_acquired", "worker.slot_released"):
            seen_peak.append(json.loads(event["payload"])["occupancy"])
    assert max(seen_peak) <= 2


def test_retry_reacquires_capacity_and_candidate_survives_reuse(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _task(
        conn, repo, "retry_candidate",
        verify_cmd="test ! -e retry_marker.txt && (test ! -e retry_solution.txt || "
                   "test \"$(cat retry_solution.txt)\" = fixed)",
    )
    other = _task(conn, repo, "clean")
    calls = 0

    async def supervisor(packet):
        nonlocal calls
        calls += 1
        await asyncio.sleep(.15)
        return SupervisorResult(
            action=Restart(feedback="repair retained candidate", reason="visible failure")
            if calls == 1 else _escalate(), ok=True, tokens_in=0, tokens_out=0,
            cost_usd=0, raw_text=None,
        )

    _run(conn, repo, tmp_path, supervisor, spawn_worker=_slow_worker)
    attempts = conn.execute(
        "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_no", (task_id,)
    ).fetchall()
    assert len(attempts) == 2
    assert attempts[1]["base_sha"] == attempts[0]["candidate_sha"]
    assert conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"] == "delivered"
    assert conn.execute("SELECT state FROM tasks WHERE id = ?", (other,)).fetchone()["state"] == "delivered"
    acquired = _events(conn, task_id, "worker.slot_acquired")
    released = _events(conn, task_id, "worker.slot_released")
    assert len(acquired) == len(released) == 2
    assert acquired[1]["seq"] > released[0]["seq"]


def test_slot_accounting_is_nonnegative_and_idempotent(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _task(conn, repo, "crash")
    _run(conn, repo, tmp_path, lambda packet: None)
    rows = _events(conn, task_id)
    acquired = [e for e in rows if e["type"] == "worker.slot_acquired"]
    released = [e for e in rows if e["type"] == "worker.slot_released"]
    assert len(acquired) == len(released) == 1
    assert all(json.loads(e["payload"])["occupancy"] >= 0 for e in acquired + released)
    assert len({json.loads(e["payload"])["attempt_id"] for e in released}) == 1


def test_reconcile_closes_slot_after_crash_before_release(tmp_path):
    repo = init_repo(tmp_path)
    db = tmp_path / "orch.db"
    conn = connect(str(db))
    task_id = _task(conn, repo, "crash")
    base_sha = subprocess.run(
        ["git", "rev-parse", "main"], cwd=repo, capture_output=True, text=True,
        check=True,
    ).stdout.strip()
    attempt_id = ulid()
    branch = f"attempt/{attempt_id}"
    create_attempt(conn, task_id=task_id, run_id="run", attempt_no=1, base_sha=base_sha,
                   candidate_branch=branch, execution_contract="public", attempt_id=attempt_id)
    update_attempt(conn, attempt_id, worker_started_at="2026-01-01T00:00:00+00:00",
                   disposition="running")
    queued = append_event(conn, source="scheduler", type="dep.satisfied", task_id=task_id)
    transition(conn, task_id, "queued", cause_seq=queued)
    spawned = append_event(conn, source="scheduler", type="worker.spawned", task_id=task_id,
                           session_id="99999999", payload={"attempt_id": attempt_id})
    transition(conn, task_id, "running", cause_seq=spawned, session_id="99999999",
               worktree=str(repo), current_attempt_id=attempt_id,
               candidate_sha=base_sha, candidate_branch=branch)
    append_event(conn, source="scheduler", type="worker.slot_acquired", task_id=task_id,
                 payload={"attempt_id": attempt_id, "occupancy": 1, "limit": 1})
    conn.close()
    conn = connect(str(db))
    from orchestrator.scheduler.reconcile import reconcile
    reconcile(conn)
    events = _events(conn, task_id)
    assert conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"] == "triage"
    assert len([e for e in events if e["type"] == "worker.slot_released"]) == 1
    assert json.loads([e for e in events if e["type"] == "worker.slot_released"][0]["payload"])["reconciled"]


def test_reconcile_after_done_claim_recovers_verification_without_worker(tmp_path):
    repo = init_repo(tmp_path)
    db = tmp_path / "orch.db"
    conn = connect(str(db))
    task_id = _task(conn, repo, "clean")
    base_sha = subprocess.run(
        ["git", "rev-parse", "main"], cwd=repo, capture_output=True, text=True,
        check=True,
    ).stdout.strip()
    attempt_id = ulid()
    branch = f"attempt/{attempt_id}"
    subprocess.run(["git", "branch", branch, base_sha], cwd=repo, check=True,
                   capture_output=True, text=True)
    create_attempt(conn, task_id=task_id, run_id="run", attempt_no=1, base_sha=base_sha,
                   candidate_branch=branch, execution_contract="public", attempt_id=attempt_id)
    update_attempt(conn, attempt_id, candidate_sha=base_sha,
                   worker_ended_at="2026-01-01T00:00:00+00:00", disposition="worker_ended")
    queued = append_event(conn, source="scheduler", type="dep.satisfied", task_id=task_id)
    transition(conn, task_id, "queued", cause_seq=queued)
    spawned = append_event(conn, source="scheduler", type="worker.spawned", task_id=task_id,
                           session_id="99999998", payload={"attempt_id": attempt_id})
    transition(conn, task_id, "running", cause_seq=spawned, session_id="99999998",
               worktree=str(repo), current_attempt_id=attempt_id,
               candidate_sha=base_sha, candidate_branch=branch)
    done = append_event(conn, source="worker", type="worker.done_claimed", task_id=task_id,
                        session_id="99999998", payload={"attempt_id": attempt_id})
    append_event(conn, source="scheduler", type="worker.slot_acquired", task_id=task_id,
                 payload={"attempt_id": attempt_id, "occupancy": 1, "limit": 1})
    conn.close()

    conn = connect(str(db))
    from orchestrator.scheduler.reconcile import reconcile
    reconcile(conn)
    assert conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"] == "verifying"
    scheduler = Scheduler(conn, repo, tmp_path / "worktrees", max_concurrency=1,
                          supervisor=lambda packet: None)
    asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=20))
    assert conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"] == "needs_human"
    assert conn.execute("SELECT COUNT(*) c FROM events WHERE task_id = ? AND type = 'worker.spawned'",
                        (task_id,)).fetchone()["c"] == 1
    assert done > 0
