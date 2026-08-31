"""Raw `git worktree`, no treehouse dependency (see README.md).

This creates one throwaway worktree and removes it on teardown, for
standalone use outside the scheduler. Concurrent batches use
worktree_pool.py instead, which reuses a fixed set of slots.
"""
import subprocess
from pathlib import Path


def _git(*args, cwd) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def create_worktree(repo_root, worktree_root, task_id, base_branch="main") -> tuple[Path, str]:
    """Create a worktree for task_id off base_branch. Returns (path, base_sha).

    Idempotent across a restart: removing a worktree at teardown doesn't
    delete the branch it was on, so a restart reusing the same task_id would
    otherwise hit "branch already exists" on `-b`. Deleting it first makes a
    restart start genuinely fresh from base_branch, not from wherever the
    previous attempt's (possibly broken) commits left off.
    """
    wt = Path(worktree_root) / task_id
    branch = f"task/{task_id}"
    subprocess.run(["git", "branch", "-D", branch], cwd=repo_root, capture_output=True)
    _git("worktree", "add", "-q", "-b", branch, str(wt), base_branch, cwd=repo_root)
    base_sha = _git("rev-parse", "HEAD", cwd=wt)
    return wt, base_sha


def remove_worktree(repo_root, worktree_path) -> None:
    subprocess.run(["git", "worktree", "remove", "--force", str(worktree_path)],
                   cwd=repo_root, check=False, capture_output=True)
