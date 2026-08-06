"""Standalone `verify-gate` CLI (design.md section 7): "so the benchmark
harness grades ALL conditions ... with identical machinery." Exercises the
console entry point end to end against a real task row and a real worktree.
"""
import json

from orchestrator.store import connect, create_task, transition
from orchestrator.verify.cli import main as verify_gate_main
from orchestrator.worker.worktree import create_worktree
from tests.helpers import git, init_repo


def _make_passing_task(tmp_path):
    repo = init_repo(tmp_path)
    db = tmp_path / "orch.db"
    conn = connect(str(db))
    task_id = create_task(conn, title="t", brief="clean", repo=str(repo),
                          delivery_mode="scout", verify_cmd="true")
    wt, base_sha = create_worktree(repo, tmp_path / "worktrees", task_id)
    (wt / "output.txt").write_text("done\n")
    git("add", "-A", cwd=wt)
    git("commit", "-qm", "work", cwd=wt)
    conn.execute("UPDATE tasks SET worktree=?, base_sha=? WHERE id=?", (str(wt), base_sha, task_id))
    conn.commit()
    conn.close()
    return db, task_id


def test_cli_json_output_reports_pass(tmp_path, capsys):
    db, task_id = _make_passing_task(tmp_path)

    code = verify_gate_main(["--task", task_id, "--db", str(db), "--json"])

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["passed"] is True
    assert out["cause"] == "tests_passed"


def test_cli_record_transitions_task_state(tmp_path, capsys):
    db, task_id = _make_passing_task(tmp_path)
    conn = connect(str(db))
    s = conn.execute("SELECT seq FROM events WHERE task_id=? ORDER BY seq DESC LIMIT 1",
                     (task_id,)).fetchone()["seq"]
    transition(conn, task_id, "queued", cause_seq=s)
    s2 = conn.execute("SELECT seq FROM events ORDER BY seq DESC LIMIT 1").fetchone()["seq"]
    transition(conn, task_id, "running", cause_seq=s2, session_id="1")
    s3 = conn.execute("SELECT seq FROM events ORDER BY seq DESC LIMIT 1").fetchone()["seq"]
    transition(conn, task_id, "verifying", cause_seq=s3)
    conn.close()

    code = verify_gate_main(["--task", task_id, "--db", str(db), "--record"])
    assert code == 0

    conn = connect(str(db))
    row = conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
    assert row["state"] == "delivering"


def test_cli_falls_back_to_the_task_rows_setup_cmd(tmp_path, capsys):
    """--setup-cmd is an override, not the only source (design.md section 7:
    this CLI must use "the exact machinery the scheduler uses"). A task
    created with its own setup_cmd must get it run even when the CLI
    invocation doesn't pass --setup-cmd itself."""
    repo = init_repo(tmp_path)
    db = tmp_path / "orch.db"
    conn = connect(str(db))
    task_id = create_task(conn, title="t", brief="clean", repo=str(repo),
                          delivery_mode="scout", setup_cmd="touch installed.marker",
                          verify_cmd="test -f installed.marker")
    wt, base_sha = create_worktree(repo, tmp_path / "worktrees", task_id)
    (wt / "output.txt").write_text("done\n")
    git("add", "-A", cwd=wt)
    git("commit", "-qm", "work", cwd=wt)
    conn.execute("UPDATE tasks SET worktree=?, base_sha=? WHERE id=?", (str(wt), base_sha, task_id))
    conn.commit()
    conn.close()

    code = verify_gate_main(["--task", task_id, "--db", str(db), "--json"])

    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["passed"] is True


def test_cli_falls_back_to_the_task_rows_hidden_cmd(tmp_path, capsys):
    repo = init_repo(tmp_path)
    db = tmp_path / "orch.db"
    conn = connect(str(db))
    task_id = create_task(conn, title="t", brief="clean", repo=str(repo),
                          delivery_mode="scout", verify_cmd="true",
                          hidden_cmd="test -f hidden.marker")
    wt, base_sha = create_worktree(repo, tmp_path / "worktrees", task_id)
    (wt / "output.txt").write_text("done\n")
    git("add", "-A", cwd=wt)
    git("commit", "-qm", "work", cwd=wt)
    conn.execute("UPDATE tasks SET worktree=?, base_sha=? WHERE id=?", (str(wt), base_sha, task_id))
    conn.commit()
    conn.close()

    code = verify_gate_main(["--task", task_id, "--db", str(db), "--json"])

    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["cause"] == "hidden_tests_failed"


def test_cli_unknown_task_errors(tmp_path):
    db = tmp_path / "orch.db"
    connect(str(db)).close()
    code = verify_gate_main(["--task", "nope", "--db", str(db)])
    assert code == 2
