"""Dependency resolution and parallel batches (see README.md):
"worktree pool, concurrency limits, dep resolution ... Test 10-task parallel
batches with fakes." All against the real Scheduler and real FakeWorker
subprocesses -- the DAG logic (_advance_deps) came early, but nothing
before this exercised it through a live scheduler run with real dependent
tasks; test_replay.py's dep chain only ever drove it by hand-applying events.
"""
import asyncio

from dagent.scheduler import Scheduler
from dagent.store import append_event, connect, create_task, replay, transition
from tests.helpers import init_repo


def _create(conn, repo, scenario, *, delivery_mode="scout", depends_on=()):
    return create_task(conn, title=scenario, brief=scenario, repo=str(repo),
                       delivery_mode=delivery_mode, verify_cmd="true", depends_on=depends_on)


def _run(conn, repo, tmp_path, *, max_concurrency, timeout=60):
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    sched = Scheduler(conn, repo, worktree_root, max_concurrency=max_concurrency,
                      stall_threshold_s=5, watchdog_interval_s=0.2, verify_timeout_s=10)
    asyncio.run(asyncio.wait_for(sched.run_until_settled(), timeout=timeout))


def _state(conn, task_id):
    return conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()["state"]


def _spawned_seq(conn, task_id):
    row = conn.execute(
        "SELECT seq FROM events WHERE task_id=? AND type='worker.spawned'", (task_id,)).fetchone()
    return row["seq"] if row else None


def _delivered_seq(conn, task_id):
    row = conn.execute(
        "SELECT seq FROM events WHERE task_id=? AND type='task.state_changed' "
        "AND json_extract(payload, '$.to')='delivered'", (task_id,)).fetchone()
    return row["seq"] if row else None


def test_dep_chain_resolves_in_order(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    a = _create(conn, repo, "clean")
    b = _create(conn, repo, "clean", depends_on=[a])
    c = _create(conn, repo, "clean", depends_on=[b])

    _run(conn, repo, tmp_path, max_concurrency=3)

    assert _state(conn, a) == _state(conn, b) == _state(conn, c) == "delivered"
    # b can't have started before a delivered, and c not before b delivered.
    assert _delivered_seq(conn, a) < _spawned_seq(conn, b)
    assert _delivered_seq(conn, b) < _spawned_seq(conn, c)


def test_fan_in_waits_for_every_dependency(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    a = _create(conn, repo, "clean")
    b = _create(conn, repo, "clean", depends_on=[a])
    c = _create(conn, repo, "clean", depends_on=[a])
    d = _create(conn, repo, "clean", depends_on=[b, c])

    _run(conn, repo, tmp_path, max_concurrency=4)

    assert all(_state(conn, t) == "delivered" for t in (a, b, c, d))
    assert _delivered_seq(conn, b) < _spawned_seq(conn, d)
    assert _delivered_seq(conn, c) < _spawned_seq(conn, d)


def test_dep_failure_cascades_to_dependency_blocked(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    a = _create(conn, repo, "clean")
    b = _create(conn, repo, "clean", depends_on=[a])

    # manager kills a before it ever runs (any non-terminal -> cancelled).
    s = append_event(conn, source="human", type="human.cancelled", task_id=a)
    transition(conn, a, "cancelled", cause_seq=s)

    _run(conn, repo, tmp_path, max_concurrency=2)

    assert _state(conn, a) == "cancelled"
    assert _state(conn, b) == "dependency_blocked"
    assert _spawned_seq(conn, b) is None  # never ran at all


def test_ten_task_parallel_batch_all_deliver_with_a_small_pool(tmp_path):
    """10 independent tasks, only 3 worktree-pool slots -- every task must
    queue for and reuse a slot at least twice, and the state machine's
    replay invariant must still hold under real concurrent scheduling."""
    repo = init_repo(tmp_path)
    conn = connect()
    ids = [_create(conn, repo, "clean") for _ in range(10)]

    _run(conn, repo, tmp_path, max_concurrency=3, timeout=90)

    states = {t: _state(conn, t) for t in ids}
    assert all(s == "delivered" for s in states.values()), states

    live = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM tasks").fetchall()}
    rebuilt = replay(conn.execute("SELECT * FROM events ORDER BY seq").fetchall())
    assert rebuilt == live

    # the pool really was reused: 10 tasks through 3 slots means at most 3
    # distinct worktree paths across every running-transition, ever. (The
    # pool itself removes its worktrees at shutdown, so this reads the paths
    # back from the event log rather than the now-empty directory.)
    import json
    worktrees = {
        json.loads(r["payload"])["worktree"]
        for r in conn.execute(
            "SELECT payload FROM events WHERE type = 'task.state_changed' "
            "AND json_extract(payload, '$.to') = 'running'")
    }
    assert len(worktrees) == 3
