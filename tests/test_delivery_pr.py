"""pr delivery mode (design.md section 9): "push branch, open PR via gh.
Delivered = PR open." The push half is real and tested against a local
bare-repo remote; opening the PR is injected (open_pr) so this suite never
needs gh or network -- the default `_gh_pr_create` is exercised live in
tests/integration instead, wherever gh auth actually lives.
"""
import asyncio
import subprocess

import pytest

from orchestrator import delivery
from orchestrator.scheduler import Scheduler
from orchestrator.store import connect, create_task
from tests.helpers import git, init_repo


def _add_bare_origin(repo, tmp_path):
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
    git("remote", "add", "origin", str(bare), cwd=repo)
    return bare


def _task_with_committed_branch(repo, task_id="t1"):
    branch = f"task/{task_id}"
    git("checkout", "-b", branch, cwd=repo)
    (repo / "change.txt").write_text("worker's change\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "worker change", cwd=repo)
    return {"id": task_id, "worktree": str(repo)}


def test_pr_pushes_branch_and_calls_open_pr(tmp_path):
    repo = init_repo(tmp_path)
    bare = _add_bare_origin(repo, tmp_path)
    task = {**_task_with_committed_branch(repo), "delivery_mode": "pr"}

    calls = []

    def fake_open_pr(wt, branch):
        calls.append((wt, branch))
        return "https://example.invalid/pr/1"

    etype, payload = delivery.deliver(task, open_pr=fake_open_pr)

    assert etype == "delivery.pr_opened"
    assert payload["url"] == "https://example.invalid/pr/1"
    assert calls == [(str(repo), "task/t1")]

    # the branch really landed on the remote.
    ls_remote = subprocess.run(["git", "ls-remote", "--heads", str(bare), "task/t1"],
                               capture_output=True, text=True, check=True)
    assert "task/t1" in ls_remote.stdout


def test_pr_push_failure_raises_delivery_error_without_calling_open_pr(tmp_path):
    repo = init_repo(tmp_path)  # no origin remote configured
    task = {**_task_with_committed_branch(repo), "delivery_mode": "pr"}

    called = []
    with pytest.raises(delivery.DeliveryError):
        delivery.deliver(task, open_pr=lambda wt, branch: called.append(1))

    assert called == []  # push failed before open_pr was ever reached


def test_pr_open_failure_after_a_successful_push_raises_delivery_error(tmp_path):
    repo = init_repo(tmp_path)
    bare = _add_bare_origin(repo, tmp_path)
    task = {**_task_with_committed_branch(repo), "delivery_mode": "pr"}

    def failing_open_pr(wt, branch):
        raise RuntimeError("gh not authenticated")

    with pytest.raises(delivery.DeliveryError, match="gh not authenticated"):
        delivery.deliver(task, open_pr=failing_open_pr)

    # the push itself still succeeded -- a retry wouldn't need to re-push.
    ls_remote = subprocess.run(["git", "ls-remote", "--heads", str(bare), "task/t1"],
                               capture_output=True, text=True, check=True)
    assert "task/t1" in ls_remote.stdout


def test_pr_mode_end_to_end_through_the_scheduler(tmp_path, monkeypatch):
    """Real FakeWorker task, real pooled worktree, real push to a local bare
    remote, PR-open swapped out so this stays gh/network-free."""
    repo = init_repo(tmp_path)
    bare = _add_bare_origin(repo, tmp_path)

    monkeypatch.setattr(delivery, "_gh_pr_create", lambda wt, branch: "https://example.invalid/pr/7")

    conn = connect()
    task_id = create_task(conn, title="clean", brief="clean", repo=str(repo),
                          delivery_mode="pr", verify_cmd="true")

    worktree_root = tmp_path / "worktrees"
    worktree_root.mkdir()
    sched = Scheduler(conn, repo, worktree_root, max_concurrency=1,
                      stall_threshold_s=5, watchdog_interval_s=1, verify_timeout_s=10)
    asyncio.run(asyncio.wait_for(sched.run_until_settled(), timeout=30))

    row = conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row["state"] == "delivered"

    opened = conn.execute(
        "SELECT payload FROM events WHERE task_id = ? AND type = 'delivery.pr_opened'",
        (task_id,)).fetchone()
    assert opened is not None

    ls_remote = subprocess.run(["git", "ls-remote", "--heads", str(bare), f"task/{task_id}"],
                               capture_output=True, text=True, check=True)
    assert f"task/{task_id}" in ls_remote.stdout
