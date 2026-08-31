"""The `orchestrator` console script (cli.py): add-task, run, daemon, answer,
status. Each subcommand is a thin wrapper over pieces already covered
elsewhere (create_task, Scheduler, transition) -- these tests exercise the
CLI wiring itself (argument parsing, DB path plumbing, output), calling
main(argv) directly rather than shelling out, same posture as
supervisor/replay.py's own tests.
"""
import asyncio
import json

from dagent.cli import _notify_loop, main
from dagent.scheduler import Scheduler
from dagent.store import append_event, connect, create_task, transition
from tests.helpers import init_repo


def test_add_task_prints_id_and_persists(tmp_path, capsys):
    db = str(tmp_path / "orch.db")
    rc = main(["add-task", "--db", db, "--title", "t", "--brief", "b",
              "--repo", "r", "--delivery-mode", "scout"])
    assert rc == 0
    task_id = capsys.readouterr().out.strip()
    assert len(task_id) == 26  # ULID

    conn = connect(db)
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row["title"] == "t" and row["state"] == "blocked"


def test_add_task_with_deps(tmp_path, capsys):
    db = str(tmp_path / "orch.db")
    main(["add-task", "--db", db, "--title", "a", "--brief", "b", "--repo", "r",
         "--delivery-mode", "scout"])
    a = capsys.readouterr().out.strip()
    main(["add-task", "--db", db, "--title", "b", "--brief", "b", "--repo", "r",
         "--delivery-mode", "scout", "--depends-on", a])
    b = capsys.readouterr().out.strip()

    conn = connect(db)
    deps = [r["depends_on"] for r in
           conn.execute("SELECT depends_on FROM task_deps WHERE task_id=?", (b,))]
    assert deps == [a]


def test_status_empty(tmp_path, capsys):
    db = str(tmp_path / "orch.db")
    rc = main(["status", "--db", db])
    assert rc == 0
    assert "no tasks" in capsys.readouterr().out


def test_status_lists_tasks(tmp_path, capsys):
    db = str(tmp_path / "orch.db")
    main(["add-task", "--db", db, "--title", "my task", "--brief", "b", "--repo", "r",
         "--delivery-mode", "scout"])
    task_id = capsys.readouterr().out.strip()

    main(["status", "--db", db])
    out = capsys.readouterr().out
    assert task_id in out and "my task" in out and "blocked" in out


def test_status_detail_unknown_task(tmp_path, capsys):
    db = str(tmp_path / "orch.db")
    rc = main(["status", "nonexistent", "--db", db])
    assert rc == 2
    assert "unknown task" in capsys.readouterr().err


def test_answer_requeues_with_message_folded_into_brief(tmp_path, capsys):
    from dagent.store import append_event, transition

    db = str(tmp_path / "orch.db")
    conn = connect(db)
    task_id = create_task(conn, title="ask", brief="original brief", repo="r",
                          delivery_mode="scout")
    # Manually drive it to needs_human, same as a real escalation would.
    s = append_event(conn, source="scheduler", type="dep.satisfied", task_id=task_id)
    transition(conn, task_id, "queued", cause_seq=s)
    s = append_event(conn, source="scheduler", type="worker.spawned", task_id=task_id)
    transition(conn, task_id, "running", cause_seq=s, session_id="x", worktree="/wt")
    s = append_event(conn, source="worker", type="worker.asked", task_id=task_id)
    transition(conn, task_id, "triage", cause_seq=s)
    s = append_event(conn, source="supervisor", type="supervisor.acted", task_id=task_id,
                     payload={"action": "escalate", "summary": "s", "question": "q?",
                              "options": ["a", "b"], "recommended": 0})
    transition(conn, task_id, "needs_human", cause_seq=s)
    conn.close()

    rc = main(["answer", task_id, "use option a", "--db", db])
    assert rc == 0
    assert "needs_human -> queued" in capsys.readouterr().out

    conn = connect(db)
    row = conn.execute("SELECT state, brief FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row["state"] == "queued"
    assert "original brief" in row["brief"]
    assert "use option a" in row["brief"]

    events = [dict(r) for r in conn.execute(
        "SELECT type, payload FROM events WHERE task_id=? ORDER BY seq", (task_id,))]
    assert any(e["type"] == "human.messaged" and
              json.loads(e["payload"])["message"] == "use option a" for e in events)


def test_answer_rejects_task_not_in_needs_human(tmp_path, capsys):
    db = str(tmp_path / "orch.db")
    conn = connect(db)
    task_id = create_task(conn, title="t", brief="b", repo="r", delivery_mode="scout")

    rc = main(["answer", task_id, "whatever", "--db", db])
    assert rc == 1
    assert "not needs_human" in capsys.readouterr().err


def test_status_detail_shows_escalation_and_answer_hint(tmp_path, capsys):
    from dagent.store import append_event, transition

    db = str(tmp_path / "orch.db")
    conn = connect(db)
    task_id = create_task(conn, title="ask", brief="b", repo="r", delivery_mode="scout")
    s = append_event(conn, source="scheduler", type="dep.satisfied", task_id=task_id)
    transition(conn, task_id, "queued", cause_seq=s)
    s = append_event(conn, source="scheduler", type="worker.spawned", task_id=task_id)
    transition(conn, task_id, "running", cause_seq=s, session_id="x", worktree="/wt")
    s = append_event(conn, source="worker", type="worker.asked", task_id=task_id)
    transition(conn, task_id, "triage", cause_seq=s)
    s = append_event(conn, source="supervisor", type="supervisor.acted", task_id=task_id,
                     payload={"action": "escalate", "summary": "worker asked something odd",
                              "question": "which config?", "options": ["prod", "dev"],
                              "recommended": 1})
    transition(conn, task_id, "needs_human", cause_seq=s)
    conn.close()

    main(["status", task_id, "--db", db])
    out = capsys.readouterr().out
    assert "worker asked something odd" in out
    assert "which config?" in out
    assert "[1] dev (recommended)" in out
    assert f"orchestrator answer {task_id}" in out


def test_status_detail_shows_local_delivery_review_commands(tmp_path, capsys):
    db = str(tmp_path / "orch.db")
    conn = connect(db)
    task_id = create_task(conn, title="delivered local", brief="b", repo="/repo",
                          delivery_mode="local", verify_cmd="true")

    s = append_event(conn, source="scheduler", type="dep.satisfied", task_id=task_id)
    transition(conn, task_id, "queued", cause_seq=s)
    s = append_event(conn, source="scheduler", type="worker.spawned", task_id=task_id)
    transition(conn, task_id, "running", cause_seq=s, session_id="x", worktree="/wt",
               base_sha="base")
    s = append_event(conn, source="worker", type="worker.done_claimed", task_id=task_id)
    transition(conn, task_id, "verifying", cause_seq=s)
    s = append_event(conn, source="verifier", type="verify.passed", task_id=task_id,
                     payload={"patch_path": "data/t/review.patch"})
    transition(conn, task_id, "delivering", cause_seq=s)
    s = append_event(conn, source="delivery", type="delivery.merged_local", task_id=task_id,
                     payload={"branch": f"task/{task_id}", "before_sha": "abc123",
                              "after_sha": "def456", "commit_sha": "def456"})
    transition(conn, task_id, "delivered", cause_seq=s)
    conn.close()

    main(["status", task_id, "--db", db])
    out = capsys.readouterr().out

    assert "merged locally abc123..def456" in out
    assert "git -C /repo diff abc123..def456" in out
    assert "git -C /repo log --oneline abc123..def456" in out
    assert "data/t/review.patch" in out


def test_run_command_drives_a_real_task_to_delivered(tmp_path, capsys):
    """End-to-end through the CLI: add-task, then run with --fake-worker
    --fake-supervisor (free, deterministic) against a real toy repo."""
    repo = init_repo(tmp_path)
    db = str(tmp_path / "orch.db")
    worktrees = tmp_path / "worktrees"

    rc = main(["add-task", "--db", db, "--title", "clean task", "--brief", "clean",
              "--repo", str(repo), "--delivery-mode", "scout"])
    assert rc == 0
    task_id = capsys.readouterr().out.strip()

    rc = main(["run", "--db", db, "--repo-root", str(repo), "--worktree-root", str(worktrees),
              "--fake-worker", "--fake-supervisor"])
    assert rc == 0
    out = capsys.readouterr().out
    assert task_id in out and "delivered" in out

    conn = connect(db)
    assert conn.execute(
        "SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()["state"] == "delivered"


def test_live_cli_run_requires_an_explicit_boundary(tmp_path, capsys):
    repo = init_repo(tmp_path)
    db = str(tmp_path / "orch.db")
    main(["add-task", "--db", db, "--title", "live task", "--brief", "clean",
          "--repo", str(repo), "--delivery-mode", "scout"])
    capsys.readouterr()

    rc = main(["run", "--db", db, "--repo-root", str(repo),
               "--worktree-root", str(tmp_path / "worktrees")])

    assert rc == 2
    assert "external isolation boundary" in capsys.readouterr().err


def test_notify_loop_prints_only_states_a_human_should_hear_about(tmp_path, capsys):
    """The event-driven wake the orchestrator Skill relies on to Monitor a
    backgrounded run/daemon instead of polling `status` -- a second reader
    connection over the same events-are-truth table (design.md section 3),
    filtered to the "your crew needs you" / "here's your PR" states."""
    db_path = str(tmp_path / "orch.db")
    conn = connect(db_path)
    task_id = create_task(conn, title="stuck task", brief="b", repo="/tmp/x",
                          delivery_mode="scout")

    async def scenario():
        # Start the notifier before anything happens -- it must only report
        # transitions that land *after* it starts watching, tail -f style,
        # not replay a db's whole history on every launch.
        notifier = asyncio.create_task(_notify_loop(db_path, poll_s=0.02))
        await asyncio.sleep(0.05)

        s = append_event(conn, source="scheduler", type="dep.satisfied", task_id=task_id)
        transition(conn, task_id, "queued", cause_seq=s)  # noisy, not in _NOTIFY_STATES
        s = append_event(conn, source="scheduler", type="worker.spawned", task_id=task_id)
        transition(conn, task_id, "running", cause_seq=s)  # noisy, not in _NOTIFY_STATES
        s = append_event(conn, source="worker", type="worker.asked", task_id=task_id)
        transition(conn, task_id, "triage", cause_seq=s)
        s = append_event(conn, source="supervisor", type="supervisor.acted", task_id=task_id)
        transition(conn, task_id, "needs_human", cause_seq=s)

        await asyncio.sleep(0.1)
        notifier.cancel()
        await asyncio.gather(notifier, return_exceptions=True)

    asyncio.run(scenario())
    conn.close()

    out = capsys.readouterr().out
    assert f"[needs_human] {task_id}  stuck task" in out
    assert "[queued]" not in out and "[running]" not in out and "[triage]" not in out


def test_scheduler_forever_picks_up_task_added_by_another_connection(tmp_path):
    """The daemon-mode mechanism itself: run_until_settled(forever=True)
    must keep polling the SQLite file for tasks written by a *separate*
    connection (standing in for a concurrent `orchestrator add-task`
    process) instead of exiting just because it started out empty."""
    repo = init_repo(tmp_path)
    db_path = str(tmp_path / "orch.db")
    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()

    conn = connect(db_path)
    scheduler = Scheduler(conn, repo, worktree_root, max_concurrency=2,
                         stall_threshold_s=5, watchdog_interval_s=0.2, verify_timeout_s=10)

    async def scenario():
        run_task = asyncio.create_task(
            scheduler.run_until_settled(forever=True, poll_interval_s=0.1))
        await asyncio.sleep(0.2)  # let it start up with nothing to do

        other_conn = connect(db_path)
        task_id = create_task(other_conn, title="late arrival", brief="clean", repo=str(repo),
                             delivery_mode="scout", verify_cmd="true")
        other_conn.close()

        for _ in range(200):
            row = conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row["state"] == "delivered":
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError(f"task never delivered, stuck at {row['state']!r}")

        run_task.cancel()
        try:
            await run_task
        except asyncio.CancelledError:
            pass
        return task_id

    task_id = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    assert conn.execute(
        "SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()["state"] == "delivered"
