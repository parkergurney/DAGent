"""FakeWorker scenario suite = M2's regression suite (design.md section 11):
"fault injection is a test case, not a prayer." Each scenario proves the
scheduler drives one exact transition sequence end-to-end -- real git
worktree, real (unmocked) verify gate, real subprocess -- with no supervisor
in the loop (M2 has none; every triage entry resolves to needs_human).
"""
import asyncio
import json
from pathlib import Path

from orchestrator.scheduler import Scheduler
from orchestrator.store import connect, create_task, replay
from tests.helpers import init_repo

SCENARIOS = ["clean", "no_commit", "empty_diff", "protected_edit",
            "stall", "ask", "crash", "wait"]


def _create(conn, repo, scenario):
    return create_task(conn, title=scenario, brief=scenario, repo=str(repo),
                       delivery_mode="scout", verify_cmd="true")


def _events(conn, task_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM events WHERE task_id = ? ORDER BY seq", (task_id,))]


def _run_fleet(conn, repo, tmp_path):
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    sched = Scheduler(conn, repo, worktree_root, max_concurrency=len(SCENARIOS),
                      stall_threshold_s=0.3, watchdog_interval_s=0.05, verify_timeout_s=10)
    asyncio.run(asyncio.wait_for(sched.run_until_settled(), timeout=30))


def test_all_scenarios_reach_correct_states(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    ids = {name: _create(conn, repo, name) for name in SCENARIOS}

    _run_fleet(conn, repo, tmp_path)

    state = {name: conn.execute("SELECT state FROM tasks WHERE id = ?", (tid,)).fetchone()["state"]
            for name, tid in ids.items()}

    assert state["clean"] == "delivered"
    for name in SCENARIOS:
        if name != "clean":
            assert state[name] == "needs_human", f"{name}: {state[name]}"

    # design.md invariant 3, exercised across a whole fault-injection run:
    # replay(events) must still equal the live tasks table.
    live = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM tasks").fetchall()}
    rebuilt = replay(conn.execute("SELECT * FROM events ORDER BY seq").fetchall())
    assert rebuilt == live


def test_clean_delivers_via_scout_report(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _create(conn, repo, "clean")

    _run_fleet(conn, repo, tmp_path)

    types = [e["type"] for e in _events(conn, task_id)]
    # design.md section 4's happy path, in order: dep resolution, spawn,
    # done-claim, a passing verify, and a scout report -- one state_changed
    # per hop, cause-chained back to the event that triggered it.
    milestones = ["task.created", "dep.satisfied", "worker.spawned",
                 "worker.done_claimed", "verify.started", "verify.passed",
                 "delivery.report_written"]
    positions = [types.index(m) for m in milestones]
    assert positions == sorted(positions)
    # blocked->queued, queued->running, running->verifying, verifying->delivering, delivering->delivered
    assert types.count("task.state_changed") == 5

    assert (Path("data") / task_id / "report.md").exists()


def test_no_commit_fails_verify_with_uncommitted_changes(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _create(conn, repo, "no_commit")

    _run_fleet(conn, repo, tmp_path)

    failures = [e for e in _events(conn, task_id) if e["type"] == "verify.failed"]
    assert len(failures) == 1
    assert json.loads(failures[0]["payload"])["cause"] == "uncommitted_changes"


def test_empty_diff_fails_verify(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _create(conn, repo, "empty_diff")

    _run_fleet(conn, repo, tmp_path)

    failures = [e for e in _events(conn, task_id) if e["type"] == "verify.failed"]
    assert json.loads(failures[0]["payload"])["cause"] == "empty_diff"


def test_protected_edit_fails_verify(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _create(conn, repo, "protected_edit")

    _run_fleet(conn, repo, tmp_path)

    failures = [e for e in _events(conn, task_id) if e["type"] == "verify.failed"]
    assert json.loads(failures[0]["payload"])["cause"] == "protected_path_modified"


def test_stall_detected_by_watchdog(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _create(conn, repo, "stall")

    _run_fleet(conn, repo, tmp_path)

    types = [e["type"] for e in _events(conn, task_id)]
    assert "worker.stalled" in types
    stalled = [e for e in _events(conn, task_id) if e["type"] == "worker.stalled"][0]
    assert stalled["source"] == "watchdog"


def test_ask_routes_to_triage_on_question(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _create(conn, repo, "ask")

    _run_fleet(conn, repo, tmp_path)

    types = [e["type"] for e in _events(conn, task_id)]
    assert "worker.asked" in types
    assert "worker.stalled" not in types  # answered directly, never went silent


def test_crash_produces_unclaimed_exit(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _create(conn, repo, "crash")

    _run_fleet(conn, repo, tmp_path)

    types = [e["type"] for e in _events(conn, task_id)]
    assert "worker.exited" in types
    assert "worker.done_claimed" not in types


def test_wait_still_resolves_via_stall(tmp_path):
    repo = init_repo(tmp_path)
    conn = connect()
    task_id = _create(conn, repo, "wait")

    _run_fleet(conn, repo, tmp_path)

    types = [e["type"] for e in _events(conn, task_id)]
    assert types.index("worker.messaged") < types.index("worker.stalled")
