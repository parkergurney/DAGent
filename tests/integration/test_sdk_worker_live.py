"""M3 exit criterion (design.md section 11): "real workers: SDK sessions on a
toy repo with 3-4 seeded issues." Drives the full M2 scheduler with real
Agent SDK sessions (worker/sdk_worker.py) against three seeded issues in a
throwaway repo -- no FakeWorker, no mocks.

Opt-in only: costs real API money and depends on live model behavior, so it
never runs by accident or in CI. Set ORCH_LIVE_SDK_TESTS=1 to run it:

    ORCH_LIVE_SDK_TESTS=1 pytest tests/integration -q

design.md section 1: "Never debug the orchestrator through paid
nondeterministic workers." The deterministic contract (state machine, verify
gate, watchdog, reconcile) is already proven by tests/scenarios/ against
FakeWorker; this file only proves the real SDK backend plugs into that same
contract -- same wire protocol, same scheduler, zero special-casing.
"""
import asyncio
import os
import subprocess
import sys

import pytest

from orchestrator.scheduler import Scheduler
from orchestrator.store import connect, create_task
from orchestrator.worker.sdk import spawn_sdk_worker
from tests.helpers import git, init_repo

pytestmark = pytest.mark.skipif(
    os.environ.get("ORCH_LIVE_SDK_TESTS") != "1",
    reason="live SDK integration test: costs real API money, set ORCH_LIVE_SDK_TESTS=1 to run",
)

MODEL = "claude-haiku-4-5"  # cheap, matches the M1 spike's model choice


def _seed_greet_bug(repo):
    (repo / "greet.py").write_text(
        'def greet(name):\n    return "Hello " + name\n'
    )
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "add buggy greet.py", cwd=repo)


def test_three_seeded_issues_delivered_by_real_sdk_workers(tmp_path):
    repo = init_repo(tmp_path)
    _seed_greet_bug(repo)
    conn = connect(str(tmp_path / "orch.db"))

    add_fn = create_task(
        conn, title="add math_utils.add", repo=str(repo), delivery_mode="local", verify_cmd="true",
        brief=("Create a file named math_utils.py in the repo root with a single "
              "function `add(a, b)` that returns a + b. Do not add anything else. "
              "Commit your change with git."),
    )
    fix_bug = create_task(
        conn, title="fix greet bug", repo=str(repo), delivery_mode="local", verify_cmd="true",
        brief=("There's a bug in greet.py: greet('World') currently returns 'Hello World' "
              "but it should return 'Hello, World!' (with a comma and an exclamation mark). "
              "Fix the greet function in greet.py and commit your change with git."),
    )
    add_doc = create_task(
        conn, title="update README", repo=str(repo), delivery_mode="local", verify_cmd="true",
        brief=("Add a new line to README.md containing exactly this text: "
              "'Maintained by the toy-repo team.' Keep the rest of README.md unchanged. "
              "Commit your change with git."),
    )

    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    # sequential (max_concurrency=1): all three ff-merge into the same repo's
    # main checkout, so this keeps output easy to attribute; nothing about
    # the scheduler requires it (verify/delivery run as blocking calls within
    # one coroutine turn, so concurrent local deliveries can't interleave).
    sched = Scheduler(conn, repo, worktree_root, max_concurrency=1,
                      stall_threshold_s=120, watchdog_interval_s=2, verify_timeout_s=90,
                      spawn_worker=spawn_sdk_worker, worker_model=MODEL)

    asyncio.run(asyncio.wait_for(sched.run_until_settled(), timeout=600))

    names = {add_fn: "add_fn", fix_bug: "fix_bug", add_doc: "add_doc"}
    states = {
        tid: conn.execute("SELECT state FROM tasks WHERE id = ?", (tid,)).fetchone()["state"]
        for tid in (add_fn, fix_bug, add_doc)
    }
    for tid, state in states.items():
        if state != "delivered":
            print(f"\n--- {names[tid]} ended in {state}, not delivered ---")
            for e in conn.execute(
                "SELECT seq, source, type, payload FROM events WHERE task_id = ? ORDER BY seq", (tid,)):
                print(dict(e))

    # Every state reached must be a valid resting state -- the scheduler must
    # never hang or crash on a real session. Full 3/3 success is NOT asserted:
    # a cheap model occasionally forgets the exact DONE_CLAIM sentinel, which
    # is not an orchestrator bug -- it's exactly the case M2's no-supervisor
    # policy is built to fail safe on (unclaimed exit -> needs_human), and
    # design.md section 1 is explicit that real workers are nondeterministic
    # and this suite isn't where that gets debugged. Observed in practice:
    # 5/6 seeded-issue runs across two live runs delivered; the other landed
    # in needs_human via exactly that path.
    assert set(states.values()) <= {"delivered", "needs_human"}
    delivered = sum(1 for s in states.values() if s == "delivered")
    assert delivered >= 2, f"only {delivered}/3 delivered: {states}"

    # every worker.done_claimed event should carry real cost/token data --
    # this is the M3-specific bit FakeWorker never exercises.
    done_claims = conn.execute(
        "SELECT cost_usd FROM events WHERE type = 'worker.done_claimed'"
    ).fetchall()
    assert len(done_claims) == delivered
    assert all(row["cost_usd"] and row["cost_usd"] > 0 for row in done_claims)

    if states[add_fn] == "delivered":
        assert (repo / "math_utils.py").exists()
        out = subprocess.run(
            [sys.executable, "-c", "import math_utils; assert math_utils.add(2, 3) == 5"],
            cwd=repo, capture_output=True, text=True)
        assert out.returncode == 0, out.stderr

    if states[fix_bug] == "delivered":
        out = subprocess.run(
            [sys.executable, "-c", "import greet; assert greet.greet('World') == 'Hello, World!'"],
            cwd=repo, capture_output=True, text=True)
        assert out.returncode == 0, out.stderr

    if states[add_doc] == "delivered":
        assert "Maintained by the toy-repo team." in (repo / "README.md").read_text()
