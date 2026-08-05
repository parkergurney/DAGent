"""local delivery mode (design.md section 9): ff-only merge into main, with a
rebase-and-retry when main has moved on but the rebase is textually clean."""
import subprocess
from pathlib import Path

import pytest

from orchestrator import delivery
from tests.helpers import git, init_repo


def _task_with_branch_worktree(repo, tmp_path, task_id="t1"):
    branch = f"task/{task_id}"
    wt = tmp_path / "wt"
    git("worktree", "add", "-b", branch, str(wt), "main", cwd=repo)
    (wt / "change.txt").write_text("worker's change\n")
    git("add", "-A", cwd=wt)
    git("commit", "-qm", "worker change", cwd=wt)
    return {"id": task_id, "repo": str(repo), "worktree": str(wt), "delivery_mode": "local"}


def test_local_clean_ff_only_merge(tmp_path):
    repo = init_repo(tmp_path)
    task = _task_with_branch_worktree(repo, tmp_path)
    before = git("rev-parse", "main", cwd=repo).stdout.strip()
    commit = git("rev-parse", "task/t1", cwd=repo).stdout.strip()

    etype, payload = delivery.deliver(task)

    assert etype == "delivery.merged_local"
    assert payload["branch"] == "task/t1"
    assert payload["before_sha"] == before
    assert payload["after_sha"] == commit
    assert payload["commit_sha"] == commit
    log = git("log", "-1", "--pretty=%s", cwd=repo).stdout.strip()
    assert log == "worker change"


def test_local_rebases_and_retries_when_main_advanced_cleanly(tmp_path):
    repo = init_repo(tmp_path)
    task = _task_with_branch_worktree(repo, tmp_path)

    # main moves on with an unrelated file -- branch no longer fast-forwards.
    (repo / "other.txt").write_text("unrelated change\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "main moved on", cwd=repo)

    etype, payload = delivery.deliver(task)

    assert etype == "delivery.merged_local"
    assert payload["rebased"] is True
    assert payload["original_commit_sha"] != payload["commit_sha"]
    assert payload["after_sha"] == git("rev-parse", "main", cwd=repo).stdout.strip()
    assert (repo / "other.txt").exists()
    assert (repo / "change.txt").exists()


def test_local_raises_and_aborts_rebase_on_conflict(tmp_path):
    repo = init_repo(tmp_path)
    task = _task_with_branch_worktree(repo, tmp_path)

    # conflicting edit to the same file on main.
    (repo / "change.txt").write_text("main's conflicting change\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "main conflicts", cwd=repo)

    with pytest.raises(delivery.DeliveryError):
        delivery.deliver(task)

    # the aborted rebase left no in-progress marker behind in the worktree.
    wt = task["worktree"]
    rebase_path = subprocess.run(["git", "rev-parse", "--git-path", "rebase-merge"],
                                 cwd=wt, capture_output=True, text=True).stdout.strip()
    assert not (Path(wt) / rebase_path).exists()
