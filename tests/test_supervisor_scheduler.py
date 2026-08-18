"""Scheduler-side dispatch of every supervisor action (design.md section 6),
driven deterministically against FakeWorker via a ScriptedSupervisor -- no
LLM, no network, same "never debug the orchestrator through paid
nondeterministic workers" posture as tests/scenarios/. This is what proves
the wiring itself (nudge writes to a live stdin, restart relaunches with
feedback and retries+=1, wait re-arms the watchdog without killing the
session, escalate/abandon land in the right terminal state) independent of
any particular LLM's behavior.
"""
import asyncio
import json
import os
import time

import pytest

from orchestrator.scheduler import Scheduler, SchedulerCleanupFailure, WorkerStartupFailure
from orchestrator.store import connect, create_task
from orchestrator.supervisor.llm import SupervisorResult
from orchestrator.supervisor.schema import Abandon, Escalate, Nudge, Restart, Wait
from orchestrator.worker.fake import spawn_fake_worker
from tests.helpers import ScriptedSupervisor, init_repo


def _create(conn, repo, scenario, **kw):
    return create_task(conn, title=scenario, brief=scenario, repo=str(repo),
                       delivery_mode=kw.pop("delivery_mode", "scout"),
                       verify_cmd=kw.pop("verify_cmd", "true"), **kw)


def _events(conn, task_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE task_id = ? ORDER BY seq", (task_id,))]


def _run(conn, repo, tmp_path, supervisor, **sched_kwargs):
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    sched = Scheduler(conn, repo, worktree_root, max_concurrency=1,
                      stall_threshold_s=0.3, watchdog_interval_s=0.05, verify_timeout_s=10,
                      supervisor=supervisor, **sched_kwargs)
    asyncio.run(asyncio.wait_for(sched.run_until_settled(), timeout=30))
    return sched


def _process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for_process_exit(pid, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return
        time.sleep(0.02)
    raise AssertionError(f"process {pid} remained alive")


def _release_failure_scheduler(tmp_path, monkeypatch, *, fail_count, task_count=2):
    repo = init_repo(tmp_path)
    conn = connect()
    task_ids = [
        _create(conn, repo, f"clean-{index}")
        for index in range(task_count)
    ]
    seen = []

    async def spawn(task, worktree, *, model=None):
        proc = await spawn_fake_worker(task, worktree, model=model)
        seen.append(proc)
        return proc

    scheduler = Scheduler(
        conn, repo, tmp_path / "worktrees", max_concurrency=task_count,
        spawn_worker=spawn, artifact_root=tmp_path / "artifacts",
        verify_timeout_s=10,
    )
    original_release = scheduler._pool.release
    calls = 0

    def release(wt, *, preserve_branch=False):
        nonlocal calls
        calls += 1
        if calls <= fail_count:
            raise RuntimeError(f"intentional cleanup failure {calls}")
        return original_release(wt, preserve_branch=preserve_branch)

    monkeypatch.setattr(scheduler._pool, "release", release)
    return scheduler, conn, task_ids, seen, lambda: calls


def _assert_scheduler_registries_empty(scheduler):
    assert scheduler._teardown_tasks == {}
    assert scheduler._procs == {}
    assert scheduler._worker_slots == {}
    assert scheduler._worktrees == {}
    assert scheduler._watchers == {}
    assert scheduler._exit_watchers == {}
    assert scheduler._reap_locks == {}
    assert scheduler._last_event_ts == {}
    assert scheduler._wait_grace == {}
    assert scheduler._reaped_tasks == set()


def test_nudge_reaches_the_live_session_and_delivers(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _create(conn, repo, "ask")
    supervisor = ScriptedSupervisor([Nudge(message="go ahead and finish", reason="answer is in the brief")])

    _run(conn, repo, tmp_path, supervisor)

    state = conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()["state"]
    assert state == "delivered"
    types = [e["type"] for e in _events(conn, task_id)]
    assert types.count("worker.spawned") == 1  # same session throughout, no restart
    assert "worker.asked" in types
    assert "supervisor.acted" in types


def test_restart_relaunches_and_bumps_retries(tmp_path):
    # feedback=None here: FakeWorker's --scenario is matched against brief
    # verbatim (argparse choices=SCENARIOS), so an augmented brief would just
    # make the restarted subprocess fail argparse instead of re-running the
    # scenario. The feedback-augmentation logic itself is covered separately
    # below with a spy spawn_worker, decoupled from FakeWorker's scenario
    # selection mechanism.
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _create(conn, repo, "no_commit")
    supervisor = ScriptedSupervisor([Restart(feedback=None, reason="uncommitted changes")])

    _run(conn, repo, tmp_path, supervisor)

    state = conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()["state"]
    assert state == "needs_human"
    row = conn.execute("SELECT retries FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row["retries"] == 1

    types = [e["type"] for e in _events(conn, task_id)]
    assert types.count("worker.spawned") == 2  # original + restart
    assert types.count("verify.failed") == 2

    # The equivalent second public failure is handled deterministically; it
    # must not purchase another supervisor decision.
    assert len(supervisor.packets) == 1
    assert supervisor.packets[0].retries_remaining == 2
    policy = [e for e in _events(conn, task_id) if e["type"] == "recovery.policy_applied"]
    assert json.loads(policy[-1]["payload"])["diagnosis_code"] == "repeated_identical_failure"


class _EmptyStream:
    async def readline(self):
        return b""


class _StartupFailureStream:
    def __init__(self):
        self.lines = [
            json.dumps({"type": "messaged", "payload": {
                "text": "Not logged in · Please run /login",
                "tokens_in": 0, "tokens_out": 0,
            }}).encode() + b"\n",
            json.dumps({"type": "result", "payload": {
                "subtype": "success", "is_error": False,
                "session_id": "startup-session", "result": "Not logged in",
                "tokens_in": 0, "tokens_out": 0, "cost_usd": 0,
            }}).encode() + b"\n",
            json.dumps({"type": "startup_failed", "payload": {
                "category": "authentication_failure",
                "reason": "Claude Code reported an authentication failure",
            }}).encode() + b"\n",
            b"",
        ]

    async def readline(self):
        return self.lines.pop(0)


class _StartupFailureProc:
    pid = 7788
    returncode = 1
    stdin = None

    def __init__(self):
        self.stdout = _StartupFailureStream()

    async def wait(self):
        return self.returncode


class _SDKFailureStream:
    def __init__(self):
        self.lines = [
            json.dumps({"type": "result", "payload": {
                "subtype": "success", "is_error": True,
                "session_id": "sdk-session", "result": "backend error",
                "api_error_status": 502,
            }}).encode() + b"\n",
            json.dumps({"type": "sdk_failed", "payload": {
                "category": "sdk_failure", "reason": "Claude backend returned an API error",
            }}).encode() + b"\n",
            b"",
        ]

    async def readline(self):
        return self.lines.pop(0)


class _SDKFailureProc:
    pid = 7799
    returncode = 1
    stdin = None

    def __init__(self):
        self.stdout = _SDKFailureStream()

    async def wait(self):
        return self.returncode


def test_startup_auth_failure_aborts_without_supervisor_or_retry(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _create(conn, repo, "startup auth")
    calls = []

    async def spawn(_task, _worktree, *, model=None):
        return _StartupFailureProc()

    async def supervisor(_packet):
        calls.append(True)
        raise AssertionError("startup failure must not enter supervisor triage")

    scheduler = Scheduler(
        conn, repo, tmp_path / "worktrees", max_concurrency=1,
        spawn_worker=spawn, supervisor=supervisor,
    )
    with pytest.raises(WorkerStartupFailure, match="authentication_failure"):
        asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=10))

    assert calls == []
    assert conn.execute("SELECT retries FROM tasks WHERE id = ?", (task_id,)).fetchone()["retries"] == 0
    types = [row["type"] for row in _events(conn, task_id)]
    assert "worker.startup_failed" in types
    assert "worker.exited" not in types
    assert "supervisor.acted" not in types
    attempt = conn.execute("SELECT disposition, failure_cause FROM attempts WHERE task_id = ?",
                           (task_id,)).fetchone()
    assert attempt["disposition"] == "startup_failed"
    assert attempt["failure_cause"] == "authentication_failure"


def test_sdk_result_failure_aborts_before_scheduler_can_hang(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _create(conn, repo, "sdk failure")

    async def spawn(_task, _worktree, *, model=None):
        return _SDKFailureProc()

    scheduler = Scheduler(
        conn, repo, tmp_path / "worktrees", max_concurrency=1,
        spawn_worker=spawn,
    )
    with pytest.raises(WorkerStartupFailure, match="sdk_failure"):
        asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=10))

    types = [row["type"] for row in _events(conn, task_id)]
    assert "worker.sdk_failure" in types
    assert "worker.exited" not in types


class _SpyProc:
    """A minimal stand-in for asyncio.subprocess.Process: exits immediately
    with no output, so _watch() treats every spawn as an unclaimed crash --
    just enough to drive triage repeatedly without a real subprocess."""

    def __init__(self, pid):
        self.pid = pid
        self.returncode = 0
        self.stdout = _EmptyStream()
        self.stdin = None

    async def wait(self):
        return 1


def test_restart_appends_feedback_to_the_relaunched_brief(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _create(conn, repo, "original brief text")
    supervisor = ScriptedSupervisor([
        Restart(feedback="remember to commit next time", reason="crashed"),
        Escalate(summary="s", question="q", options=["o"], reason="still crashing"),
    ])
    seen_briefs = []
    pids = iter([111, 222])

    async def spy_spawn(task, worktree, *, model=None):
        seen_briefs.append(task["brief"])
        return _SpyProc(next(pids))

    _run(conn, repo, tmp_path, supervisor, spawn_worker=spy_spawn)

    state = conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()["state"]
    assert state == "needs_human"
    assert seen_briefs == [
        "original brief text",
        "original brief text\n\nFeedback from a previous attempt:\nremember to commit next time",
    ]
    # tasks.brief itself is never mutated -- only the spawned worker's prompt is.
    assert conn.execute("SELECT brief FROM tasks WHERE id=?", (task_id,)).fetchone()["brief"] \
        == "original brief text"


def test_wait_extends_the_deadline_without_killing_the_session(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _create(conn, repo, "stall")
    seen = {}

    async def spawn(task, worktree, *, model=None):
        proc = await spawn_fake_worker(task, worktree, model=model)
        seen["proc"] = proc
        return proc

    class WaitThenEscalate:
        def __init__(self):
            self.calls = 0

        async def __call__(self, packet):
            del packet
            self.calls += 1
            assert _process_alive(seen["proc"].pid)
            assert scheduler._procs[task_id] is seen["proc"]
            if self.calls == 1:
                action = Wait(seconds=1, reason="might be doing something external")
            else:
                action = Escalate(summary="still silent", question="how to proceed?", options=["review"],
                                  reason="stalled again after the wait")
            return SupervisorResult(action=action, ok=True, tokens_in=0, tokens_out=0,
                                    cost_usd=0, raw_text=None)

    supervisor = WaitThenEscalate()
    scheduler = Scheduler(
        conn, repo, tmp_path / "worktrees", max_concurrency=1,
        stall_threshold_s=0.3, watchdog_interval_s=0.05, verify_timeout_s=10,
        supervisor=supervisor, spawn_worker=spawn,
    )

    asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=30))

    state = conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()["state"]
    assert state == "needs_human"
    types = [e["type"] for e in _events(conn, task_id)]
    assert types.count("worker.spawned") == 1  # never restarted, same stalled session throughout
    assert types.count("worker.stalled") == 2  # detected, waited, detected again
    _wait_for_process_exit(seen["proc"].pid)
    assert scheduler._procs == {}
    assert scheduler._worker_slots == {}
    assert scheduler._teardown_tasks == {}
    assert len([e for e in _events(conn, task_id) if e["type"] == "worker.slot_released"]) == 1
    asyncio.run(scheduler._teardown(task_id))
    asyncio.run(scheduler._teardown(task_id))


def test_cleanup_failure_is_not_reported_as_clean_success(tmp_path, monkeypatch):
    scheduler, _conn, _task_ids, seen, calls = _release_failure_scheduler(
        tmp_path, monkeypatch, fail_count=1,
    )

    with pytest.raises(SchedulerCleanupFailure, match="intentional cleanup failure 1"):
        asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=30))

    assert calls() == 2
    for proc in seen:
        _wait_for_process_exit(proc.pid)
    _assert_scheduler_registries_empty(scheduler)
    # The failed worker's durable candidate can still be verified on a
    # repeated shutdown/run, and a second shutdown remains harmless.
    asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=30))
    _assert_scheduler_registries_empty(scheduler)


def test_cleanup_failure_does_not_block_other_worker_cleanup(tmp_path, monkeypatch):
    scheduler, _conn, _task_ids, seen, calls = _release_failure_scheduler(
        tmp_path, monkeypatch, fail_count=1,
    )

    with pytest.raises(SchedulerCleanupFailure):
        asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=30))

    assert calls() == 2
    for proc in seen:
        _wait_for_process_exit(proc.pid)
    _assert_scheduler_registries_empty(scheduler)


def test_multiple_cleanup_failures_are_deterministic_and_preserved(tmp_path, monkeypatch):
    scheduler, _conn, task_ids, seen, calls = _release_failure_scheduler(
        tmp_path, monkeypatch, fail_count=2,
    )

    with pytest.raises(BaseExceptionGroup) as raised:
        asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=30))

    assert calls() == 2
    labels = [failure.label for failure in raised.value.exceptions]
    assert labels == sorted(labels)
    assert labels == [f"teardown task {task_id}" for task_id in sorted(task_ids)]
    assert all("intentional cleanup failure" in str(failure)
               for failure in raised.value.exceptions)
    for proc in seen:
        _wait_for_process_exit(proc.pid)
    _assert_scheduler_registries_empty(scheduler)


def test_cancellation_during_teardown_does_not_hide_cleanup_failure(tmp_path):
    scheduler = Scheduler(
        connect(), ".", tmp_path / "worktrees", max_concurrency=1,
    )

    async def scenario():
        started = asyncio.Event()
        finish = asyncio.Event()

        async def failing_owned(task_id, **kwargs):
            del task_id, kwargs
            started.set()
            await finish.wait()
            raise RuntimeError("cleanup failed after cancellation")

        scheduler._teardown_owned = failing_owned
        runner = asyncio.create_task(scheduler._teardown("cancelled-cleanup"))
        await started.wait()
        runner.cancel()
        finish.set()
        with pytest.raises(RuntimeError, match="cleanup failed after cancellation"):
            await runner

    asyncio.run(scenario())
    assert scheduler._teardown_tasks == {}


def test_cancellation_during_wait_reaps_the_live_worker(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _create(conn, repo, "stall")
    seen = {}
    waited = asyncio.Event()

    async def spawn(task, worktree, *, model=None):
        proc = await spawn_fake_worker(task, worktree, model=model)
        seen["proc"] = proc
        return proc

    class WaitSupervisor:
        async def __call__(self, packet):
            del packet
            waited.set()
            return SupervisorResult(
                action=Wait(seconds=30, reason="keep the live session"), ok=True,
                tokens_in=0, tokens_out=0, cost_usd=0, raw_text=None,
            )

    scheduler = Scheduler(
        conn, repo, tmp_path / "worktrees", max_concurrency=1,
        stall_threshold_s=0.2, watchdog_interval_s=0.05, verify_timeout_s=10,
        supervisor=WaitSupervisor(), spawn_worker=spawn,
    )

    async def run_and_cancel():
        runner = asyncio.create_task(scheduler.run_until_settled(forever=True))
        await asyncio.wait_for(waited.wait(), timeout=10)
        deadline = time.monotonic() + 2
        while conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()["state"] != "running":
            if time.monotonic() >= deadline:
                raise AssertionError("WAIT action did not return the task to running")
            await asyncio.sleep(0.01)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

    asyncio.run(run_and_cancel())
    _wait_for_process_exit(seen["proc"].pid)
    assert scheduler._procs == {}
    assert scheduler._worker_slots == {}


def test_exception_during_wait_supervision_reaps_the_live_worker(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _create(conn, repo, "stall")
    seen = {}

    async def spawn(task, worktree, *, model=None):
        proc = await spawn_fake_worker(task, worktree, model=model)
        seen["proc"] = proc
        return proc

    class RaisingSupervisor:
        async def __call__(self, packet):
            del packet
            raise RuntimeError("supervisor transport failed")

    scheduler = Scheduler(
        conn, repo, tmp_path / "worktrees", max_concurrency=1,
        stall_threshold_s=0.2, watchdog_interval_s=0.05, verify_timeout_s=10,
        supervisor=RaisingSupervisor(), spawn_worker=spawn,
    )
    asyncio.run(asyncio.wait_for(scheduler.run_until_settled(), timeout=30))

    _wait_for_process_exit(seen["proc"].pid)
    assert scheduler._procs == {}
    assert scheduler._worker_slots == {}
    assert conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()["state"] == "needs_human"


def test_abandon_only_reachable_in_yolo_mode(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _create(conn, repo, "crash", max_retries=0)
    supervisor = ScriptedSupervisor([Abandon(reason="not worth another attempt")])

    _run(conn, repo, tmp_path, supervisor, yolo=True)

    state = conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()["state"]
    assert state == "failed"


def test_escalate_payload_carries_summary_and_question_for_the_user(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _create(conn, repo, "crash")
    supervisor = ScriptedSupervisor([
        Escalate(summary="worker crashed immediately", question="retry or investigate?",
                 options=["retry", "investigate"], recommended=1, reason="no output at all"),
    ])

    _run(conn, repo, tmp_path, supervisor)

    acted = [e for e in _events(conn, task_id) if e["type"] == "supervisor.acted"][0]
    payload = json.loads(acted["payload"])
    assert payload["summary"] == "worker crashed immediately"
    assert payload["options"] == ["retry", "investigate"]
    assert payload["recommended"] == 1


def test_out_of_menu_action_is_rejected_and_falls_back_to_escalate(tmp_path):
    """A supervisor (real or scripted) that returns nudge for a trigger with
    no live session is out of menu -- the orchestrator, not the supervisor,
    enforces this."""
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _create(conn, repo, "crash")  # worker.exited: no live session
    supervisor = ScriptedSupervisor([Nudge(message="hi", reason="bogus")])

    _run(conn, repo, tmp_path, supervisor)

    state = conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()["state"]
    assert state == "needs_human"
    failed = [e for e in _events(conn, task_id) if e["type"] == "supervisor.failed"]
    assert len(failed) == 1
