"""Verify gate causes not already exercised by the FakeWorker scenario suite
(design.md section 7): a real failing test, a repo that was already broken at
base_sha, a hang that gets killed on timeout, and the fail-then-pass flake
protocol. All against real git worktrees and real subprocesses -- no mocks.
"""
from orchestrator.verify.gate import VerifyRequest, run_verify
from tests.helpers import git, init_repo


def _child_worktree(repo, name, edit):
    """Branch off HEAD, apply `edit(path)` inside a fresh worktree, commit,
    and return (worktree_path, base_sha)."""
    base_sha = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    wt = repo.parent / name
    git("worktree", "add", "-q", "-b", name, str(wt), "main", cwd=repo)
    edit(wt)
    git("add", "-A", cwd=wt)
    git("commit", "-qm", "change", cwd=wt)
    return wt, base_sha


def test_tests_failed_when_base_passes_and_child_breaks(tmp_path):
    repo = init_repo(tmp_path)
    (repo / "flag.txt").write_text("present\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "add flag", cwd=repo)

    wt, base_sha = _child_worktree(repo, "break", lambda wt: (wt / "flag.txt").unlink())

    req = VerifyRequest(task_id="t1", worktree=str(wt), base_sha=base_sha,
                        verify_cmd="test -f flag.txt", repo=str(repo), timeout_s=10)
    result = run_verify(req)

    assert not result.passed
    assert result.cause == "tests_failed"


def test_baseline_broken_when_base_already_fails(tmp_path):
    repo = init_repo(tmp_path)  # no flag.txt at base
    wt, base_sha = _child_worktree(repo, "unrelated",
                                   lambda wt: (wt / "other.txt").write_text("x\n"))

    req = VerifyRequest(task_id="t2", worktree=str(wt), base_sha=base_sha,
                        verify_cmd="test -f flag.txt", repo=str(repo), timeout_s=10)
    result = run_verify(req)

    assert not result.passed
    assert result.cause == "baseline_broken"


def test_timeout_when_child_hangs_but_base_does_not(tmp_path):
    repo = init_repo(tmp_path)
    (repo / "check.sh").write_text("#!/bin/sh\nexit 0\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "add check", cwd=repo)

    wt, base_sha = _child_worktree(
        repo, "hang", lambda wt: (wt / "check.sh").write_text("#!/bin/sh\nsleep 5\n"))

    req = VerifyRequest(task_id="t3", worktree=str(wt), base_sha=base_sha,
                        verify_cmd="sh check.sh", repo=str(repo), timeout_s=0.5)
    result = run_verify(req)

    assert not result.passed
    assert result.cause == "timeout"


def test_flaky_pass_on_rerun_does_not_fail(tmp_path):
    """verify_cmd flakes (fail, then pass) only when a `trigger` file is
    present -- which the child commit adds and base_sha doesn't have, so the
    single untouched baseline run passes trivially and the flakiness is
    isolated to the (rerun-protected) main run."""
    repo = init_repo(tmp_path)
    wt, base_sha = _child_worktree(repo, "flake", lambda wt: (wt / "trigger").write_text(""))

    req = VerifyRequest(
        task_id="t4", worktree=str(wt), base_sha=base_sha, repo=str(repo), timeout_s=10,
        verify_cmd="test -f trigger || exit 0; test -f ran_once || (touch ran_once && exit 1)",
    )
    result = run_verify(req)

    assert result.passed
    assert result.cause == "tests_passed"
    assert result.flaky is True
