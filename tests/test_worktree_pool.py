"""WorktreePool (design.md section 8 / M5): fixed-size slot reuse instead of
create/destroy per task attempt, and the crash-recovery interaction with
design.md section 4's reconciliation pass -- a fresh process must get clean
slots even if a previous one crashed mid-run and left a live worktree behind.
"""
import asyncio

from orchestrator.worker.worktree_pool import WorktreePool
from tests.helpers import git, init_repo


def test_open_creates_exactly_size_slots(tmp_path):
    repo = init_repo(tmp_path)
    pool = WorktreePool(repo, tmp_path / "worktrees", size=3)
    pool.open()
    assert len(pool._slots) == 3
    for wt in pool._slots:
        assert wt.exists()
    pool.close()


def test_acquire_gives_a_clean_checkout_of_a_fresh_branch(tmp_path):
    repo = init_repo(tmp_path)
    pool = WorktreePool(repo, tmp_path / "worktrees", size=2)
    pool.open()

    wt, base_sha = asyncio.run(pool.acquire("taskA"))
    assert (wt / "README.md").exists()
    branch = git("branch", "--show-current", cwd=wt).stdout.strip()
    assert branch == "task/taskA"
    assert base_sha == git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    pool.close()


def test_release_and_reacquire_reuses_the_slot_and_wipes_prior_work(tmp_path):
    repo = init_repo(tmp_path)
    pool = WorktreePool(repo, tmp_path / "worktrees", size=1)
    pool.open()

    wt1, _ = asyncio.run(pool.acquire("taskA"))
    (wt1 / "leftover.txt").write_text("uncommitted junk\n")
    pool.release(wt1)

    wt2, _ = asyncio.run(pool.acquire("taskB"))
    assert wt2 == wt1  # only one slot; must be the same directory
    assert not (wt2 / "leftover.txt").exists()  # wiped on acquire
    branch = git("branch", "--show-current", cwd=wt2).stdout.strip()
    assert branch == "task/taskB"

    pool.close()


def test_acquire_blocks_until_a_slot_is_released(tmp_path):
    repo = init_repo(tmp_path)
    pool = WorktreePool(repo, tmp_path / "worktrees", size=1)
    pool.open()

    async def scenario():
        wt1, _ = await pool.acquire("first")
        second_done = asyncio.Event()

        async def acquire_second():
            await pool.acquire("second")
            second_done.set()

        task = asyncio.create_task(acquire_second())
        await asyncio.sleep(0.1)
        assert not second_done.is_set()  # still blocked, no free slot

        pool.release(wt1)
        await asyncio.wait_for(second_done.wait(), timeout=2)
        await task

    asyncio.run(scenario())
    pool.close()


def test_open_recovers_from_a_stale_slot_left_by_a_crashed_process(tmp_path):
    """Simulates the design.md section 4 crash-recovery scenario for
    worktrees: a previous orchestrator process died with a slot still
    checked out and dirty. A fresh process's pool.open() must not choke on
    it -- every slot starts clean unconditionally, no special code path."""
    repo = init_repo(tmp_path)
    worktree_root = tmp_path / "worktrees"

    stale_pool = WorktreePool(repo, worktree_root, size=2)
    stale_pool.open()
    stale_wt, _ = asyncio.run(stale_pool.acquire("orphaned-task"))
    (stale_wt / "mid_edit.txt").write_text("never finished\n")
    # no close(): simulates the process dying without cleanup

    fresh_pool = WorktreePool(repo, worktree_root, size=2)
    fresh_pool.open()  # must not raise
    assert len(fresh_pool._slots) == 2

    wt, _ = asyncio.run(fresh_pool.acquire("new-task"))
    assert not (wt / "mid_edit.txt").exists()

    fresh_pool.close()
