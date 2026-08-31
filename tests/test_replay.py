"""Invariant tests for the event-sourced store (design.md section 3).

The headline invariant (3): tasks is rebuildable from events -- replay(events)
must equal the live tasks table after any lifecycle. This test drives a
representative team through the state machine and asserts it. It will catch
every place someone was tempted to mutate task state without an event.
"""
import json

import pytest

from dagent import config
from dagent.store import append_event, connect, create_task, replay, transition, ulid


def _tasks_table(conn) -> dict:
    """Snapshot the live tasks table as {id: {column: value}}."""
    rows = conn.execute("SELECT * FROM tasks").fetchall()
    return {r["id"]: dict(r) for r in rows}


def _all_events(conn):
    return conn.execute("SELECT * FROM events ORDER BY seq").fetchall()


def _drive_representative_team(conn):
    """Create tasks with a dep and walk them through happy, retry, and cancel
    paths. Returns the created task ids."""
    # a: the dependency, walked all the way to delivered (happy path).
    a = create_task(conn, title="dep", brief="build the lib", repo="r",
                    delivery_mode="pr", verify_cmd="pytest")
    # b: depends on a; failed verify -> triage -> restart -> ... -> delivered.
    b = create_task(conn, title="feature", brief="use the lib", repo="r",
                    delivery_mode="local", verify_cmd="pytest", depends_on=[a])
    # c: gets cancelled by the manager mid-flight.
    c = create_task(conn, title="scout", brief="investigate", repo="r",
                    delivery_mode="scout")

    # --- a: blocked -> queued -> running -> verifying -> delivering -> delivered
    s = append_event(conn, source="scheduler", type="dep.satisfied", task_id=a)
    transition(conn, a, "queued", cause_seq=s)
    s = append_event(conn, source="scheduler", type="worker.spawned", task_id=a,
                     session_id="sess-a")
    transition(conn, a, "running", cause_seq=s, session_id="sess-a", worktree="/wt/a")
    s = append_event(conn, source="worker", type="worker.done_claimed", task_id=a,
                     session_id="sess-a")
    transition(conn, a, "verifying", cause_seq=s)
    s = append_event(conn, source="verifier", type="verify.passed", task_id=a,
                     payload={"cause": "tests_passed"})
    transition(conn, a, "delivering", cause_seq=s)
    s = append_event(conn, source="delivery", type="delivery.pr_opened", task_id=a,
                     payload={"url": "http://pr/1"})
    transition(conn, a, "delivered", cause_seq=s)

    # --- b: dep delivered -> queued -> running -> verifying -> triage
    #        -> running (restart, retries+=1) -> verifying -> delivering -> delivered
    s = append_event(conn, source="scheduler", type="dep.satisfied", task_id=b,
                     payload={"depends_on": a})
    transition(conn, b, "queued", cause_seq=s)
    s = append_event(conn, source="scheduler", type="worker.spawned", task_id=b)
    transition(conn, b, "running", cause_seq=s, session_id="sess-b1", worktree="/wt/b")
    s = append_event(conn, source="worker", type="worker.done_claimed", task_id=b)
    transition(conn, b, "verifying", cause_seq=s)
    fail_seq = append_event(conn, source="verifier", type="verify.failed", task_id=b,
                            payload={"cause": "tests_failed", "failure_signature": "AssertionError"})
    transition(conn, b, "triage", cause_seq=fail_seq)
    # supervisor decides restart -> new session, retries bumped.
    s = append_event(conn, source="supervisor", type="supervisor.acted", task_id=b,
                     payload={"action": "restart"}, tokens_in=1200, tokens_out=80, cost_usd=0.02)
    transition(conn, b, "running", cause_seq=s, session_id="sess-b2", retries=1)
    s = append_event(conn, source="worker", type="worker.done_claimed", task_id=b)
    transition(conn, b, "verifying", cause_seq=s)
    s = append_event(conn, source="verifier", type="verify.passed", task_id=b)
    transition(conn, b, "delivering", cause_seq=s)
    s = append_event(conn, source="delivery", type="delivery.merged_local", task_id=b)
    transition(conn, b, "delivered", cause_seq=s)

    # --- c: manager cancels from blocked (any -> cancelled)
    s = append_event(conn, source="human", type="human.cancelled", task_id=c)
    transition(conn, c, "cancelled", cause_seq=s)

    return a, b, c


def test_replay_rebuilds_tasks():
    conn = connect()
    _drive_representative_team(conn)

    live = _tasks_table(conn)
    rebuilt = replay(_all_events(conn))

    assert rebuilt == live


def test_replay_matches_after_reopen(tmp_path):
    """Same invariant, but against a real on-disk WAL db reopened fresh."""
    db = str(tmp_path / "orch.db")
    conn = connect(db)
    _drive_representative_team(conn)
    conn.close()

    conn = connect(db)  # reopen; schema apply is idempotent
    assert replay(_all_events(conn)) == _tasks_table(conn)


def test_illegal_transition_rejected():
    conn = connect()
    a = create_task(conn, title="t", brief="b", repo="r", delivery_mode="pr")
    # blocked -> running is not a legal edge (must go through queued).
    with pytest.raises(ValueError, match="illegal transition"):
        transition(conn, a, "running", cause_seq=0)
    # state unchanged and no rogue state_changed emitted.
    assert conn.execute("SELECT state FROM tasks WHERE id=?", (a,)).fetchone()["state"] == "blocked"
    assert conn.execute(
        "SELECT COUNT(*) c FROM events WHERE type='task.state_changed'"
    ).fetchone()["c"] == 0


def test_terminal_states_are_final():
    conn = connect()
    a = create_task(conn, title="t", brief="b", repo="r", delivery_mode="pr")
    s = append_event(conn, source="human", type="human.cancelled", task_id=a)
    transition(conn, a, "cancelled", cause_seq=s)
    with pytest.raises(ValueError, match="illegal transition"):
        transition(conn, a, "queued", cause_seq=s)


def test_state_changed_carries_cause_seq_chain():
    """Invariant 4: every state_changed payload names the event that caused it."""
    conn = connect()
    a = create_task(conn, title="t", brief="b", repo="r", delivery_mode="pr")
    cause = append_event(conn, source="scheduler", type="dep.satisfied", task_id=a)
    changed = transition(conn, a, "queued", cause_seq=cause)

    payload = json.loads(
        conn.execute("SELECT payload FROM events WHERE seq=?", (changed,)).fetchone()["payload"]
    )
    assert payload == {"from": "blocked", "to": "queued", "cause_seq": cause}
    # the cause seq points at a real earlier event.
    assert cause < changed
    assert conn.execute(
        "SELECT type FROM events WHERE seq=?", (cause,)
    ).fetchone()["type"] == "dep.satisfied"


def test_transition_rejects_unknown_field():
    conn = connect()
    a = create_task(conn, title="t", brief="b", repo="r", delivery_mode="pr")
    s = append_event(conn, source="scheduler", type="dep.satisfied", task_id=a)
    with pytest.raises(ValueError, match="cannot set"):
        transition(conn, a, "queued", cause_seq=s, state="hacked")


def test_config_defaults_and_override(tmp_path):
    assert config.load() == config.Config()
    assert config.Config().max_concurrency == 4
    assert config.Config().stall_threshold_s == 300

    cfg_file = tmp_path / "orch.toml"
    cfg_file.write_text('max_concurrency = 8\nunknown_key = "ignored"\n')
    cfg = config.load(cfg_file)
    assert cfg.max_concurrency == 8
    assert cfg.max_retries == 2  # untouched default


def test_ulid_sortable_and_unique():
    ids = [ulid() for _ in range(1000)]
    assert len(set(ids)) == 1000  # no collisions
    assert all(len(i) == 26 for i in ids)
