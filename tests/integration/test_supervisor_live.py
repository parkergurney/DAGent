"""Live check on the real supervisor -- one
live Messages-API-equivalent call, closed-enum response -- making an actual
decision. Paired with FakeWorker (free, deterministic) rather than a real SDK
worker, so this only pays for the supervisor call itself, not also a worker
session; that's what tests/integration/test_sdk_worker_live.py already
covers.

Opt-in only, same posture as the other live integration tests:

    ORCH_LIVE_SDK_TESTS=1 pytest tests/integration -q
"""
import asyncio
import os
import subprocess
import sys

import pytest

from dagent.scheduler import Scheduler
from dagent.store import connect, create_task
from tests.helpers import init_repo

pytestmark = pytest.mark.skipif(
    os.environ.get("ORCH_LIVE_SDK_TESTS") != "1",
    reason="live supervisor integration test: costs real API money, set ORCH_LIVE_SDK_TESTS=1 to run",
)

MODEL = "claude-haiku-4-5"


def _run(conn, repo, tmp_path, **kw):
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    from dagent.supervisor.llm import invoke_supervisor

    async def supervisor(packet):
        return await invoke_supervisor(packet, model=MODEL)

    sched = Scheduler(conn, repo, worktree_root, max_concurrency=1,
                      stall_threshold_s=0.5, watchdog_interval_s=0.1, verify_timeout_s=30,
                      supervisor=supervisor, **kw)
    asyncio.run(asyncio.wait_for(sched.run_until_settled(), timeout=120))


def test_real_supervisor_restarts_or_escalates_a_recoverable_failure(tmp_path):
    """no_commit -> verify.failed(uncommitted_changes), no live session, so
    the menu is exactly {restart, escalate}. Either is a defensible real
    call; asserting a valid resting state (not a hang or a crash) is the bar,
    same posture as test_sdk_worker_live.py."""
    repo = init_repo(tmp_path)
    conn = connect(str(tmp_path / "orch.db"))
    task_id = create_task(conn, title="no_commit", brief="no_commit", repo=str(repo),
                          delivery_mode="scout", verify_cmd="true")

    _run(conn, repo, tmp_path)

    row = conn.execute("SELECT state, retries FROM tasks WHERE id = ?", (task_id,)).fetchone()
    acted = [dict(r) for r in conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND type = 'supervisor.acted'", (task_id,))]
    assert len(acted) >= 1
    import json
    first_action = json.loads(acted[0]["payload"])["action"]
    assert first_action in ("restart", "escalate")
    assert row["state"] in ("needs_human", "delivered")  # delivered if restart then succeeded


def test_real_supervisor_escalates_an_unanswerable_question(tmp_path):
    """ask -> worker.asked with a brief that says nothing about the
    question. README.md's heuristic: never guess on the manager's
    behalf. A live session IS available here, so nudge is on the menu --
    the point of this test is that a competent supervisor declines it."""
    repo = init_repo(tmp_path)
    conn = connect(str(tmp_path / "orch.db"))
    task_id = create_task(conn, title="ask", brief="ask", repo=str(repo),
                          delivery_mode="scout", verify_cmd="true")

    _run(conn, repo, tmp_path)

    row = conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["state"] == "needs_human"

    import json
    acted = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND type = 'supervisor.acted' LIMIT 1",
        (task_id,)).fetchone()
    assert json.loads(acted["payload"])["action"] == "escalate"


def test_packet_dump_and_replay_round_trip(tmp_path, monkeypatch):
    """A packet from a real invocation gets dumped to disk, and
    supervisor-replay can re-run it against the current prompt/model."""
    monkeypatch.chdir(tmp_path)
    repo = init_repo(tmp_path)
    conn = connect(str(tmp_path / "orch.db"))
    task_id = create_task(conn, title="crash", brief="crash", repo=str(repo),
                          delivery_mode="scout", verify_cmd="true")

    _run(conn, repo, tmp_path)

    packets_dir = tmp_path / "data" / task_id / "packets"
    saved = list(packets_dir.glob("*.json"))
    assert len(saved) >= 1

    out = subprocess.run([sys.executable, "-m", "dagent.supervisor.replay",
                         str(saved[0]), "--model", MODEL],
                         capture_output=True, text=True, cwd=tmp_path, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "REPLAYED:" in out.stdout
