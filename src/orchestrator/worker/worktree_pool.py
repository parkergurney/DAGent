"""Pooled git worktrees (design.md section 8 / M5): "raw git worktree, no
treehouse dependency." M2 through M4 created and destroyed a worktree per
task attempt via worktree.create_worktree/remove_worktree -- fine for one
task at a time, wasteful once parallel batches make `git worktree add`'s
metadata churn worth avoiding.

This pool pre-creates a fixed number of worktree directories once and resets
a slot to a clean checkout of the task's own branch on each acquire, instead
of creating and removing a worktree directory per attempt. Pool size always
equals the scheduler's max_concurrency -- there is never a reason for more
slots than tasks that can be running at once, so acquire() blocking on a
free slot is just concurrency control falling out of the pool for free,
not a second limiter to keep in sync with the first.
"""
import asyncio
import shutil
import subprocess
from pathlib import Path


def _git(*args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _git_ok(*args, cwd) -> str:
    proc = _git(*args, cwd=cwd)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


class WorktreePool:
    def __init__(self, repo_root, worktree_root, size: int):
        # Resolved eagerly: git worktree add (open(), cwd=repo_root) and the
        # per-slot git calls in acquire() (cwd=<slot path>) both receive
        # these as `cwd=`, but subprocess resolves a relative cwd against
        # the CALLING process's own cwd, not against repo_root -- a
        # relative worktree_root would then mean two different directories
        # depending on which call site touched it.
        self.repo_root = Path(repo_root).resolve()
        self.worktree_root = Path(worktree_root).resolve()
        self.size = size
        self._slots: list[Path] = []
        self._free: asyncio.Queue = asyncio.Queue()

    def open(self) -> None:
        """Pre-create every slot, each on its own scratch branch. Wipes
        anything already sitting at a slot path first -- a prior orchestrator
        process that crashed mid-run (design.md section 4's reconciliation
        pass) can leave a live worktree there; every fresh process gets clean
        slots unconditionally, no special recovery path to keep correct.
        """
        _git("worktree", "prune", cwd=self.repo_root)
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        for i in range(self.size):
            wt = self.worktree_root / f"slot-{i}"
            branch = f"pool/slot-{i}"
            if wt.exists():
                _git("worktree", "remove", "--force", str(wt), cwd=self.repo_root)
                shutil.rmtree(wt, ignore_errors=True)
            _git("branch", "-D", branch, cwd=self.repo_root)
            _git_ok("worktree", "add", "-q", "-b", branch, str(wt), "HEAD", cwd=self.repo_root)
            self._slots.append(wt)
            self._free.put_nowait(wt)

    async def acquire(self, task_id: str, base_branch: str = "main") -> tuple[Path, str]:
        """Wait for a free slot, reset it to a clean checkout of a fresh
        task/<id> branch off base_branch, and return (path, base_sha)."""
        wt = await self._free.get()
        branch = f"task/{task_id}"
        _git("branch", "-D", branch, cwd=self.repo_root)
        # reset+clean before checkout: the previous occupant may have crashed
        # mid-edit and left uncommitted or untracked files behind.
        _git_ok("reset", "--hard", "HEAD", cwd=wt)
        _git_ok("clean", "-fdx", cwd=wt)
        _git_ok("checkout", "-B", branch, base_branch, cwd=wt)
        base_sha = _git_ok("rev-parse", "HEAD", cwd=wt)
        return wt, base_sha

    def release(self, wt: Path) -> None:
        self._free.put_nowait(wt)

    def close(self) -> None:
        """Remove every pooled worktree. Call once, at shutdown."""
        for wt in self._slots:
            _git("worktree", "remove", "--force", str(wt), cwd=self.repo_root)
        self._slots.clear()
