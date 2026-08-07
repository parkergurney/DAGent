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


def test_setup_cmd_runs_before_verify_in_both_baseline_and_worktree(tmp_path):
    """Guards the real bug this fixes: a project needing an install step
    (e.g. `npm install`) before its build/verify command can run. setup_cmd
    must execute in the baseline's throwaway scratch checkout too, not just
    the worker's own worktree -- otherwise baseline_broken gets cached
    forever against a base_sha that's actually fine (design.md section 7)."""
    repo = init_repo(tmp_path)
    wt, base_sha = _child_worktree(repo, "feature",
                                   lambda wt: (wt / "feature.txt").write_text("x\n"))

    req = VerifyRequest(task_id="t3", worktree=str(wt), base_sha=base_sha,
                        setup_cmd="touch installed.marker",
                        verify_cmd="test -f installed.marker",
                        repo=str(repo), timeout_s=10)
    result = run_verify(req)

    assert result.passed
    assert result.cause == "tests_passed"


def test_hidden_setup_runs_only_in_detached_verifier_worktree(tmp_path):
    repo = init_repo(tmp_path)
    wt, base_sha = _child_worktree(
        repo, "hidden-boundary",
        lambda wt: (wt / "feature.txt").write_text("committed worker change\n"),
    )

    req = VerifyRequest(
        task_id="hidden-boundary", worktree=str(wt), base_sha=base_sha,
        setup_cmd="mkdir -p hidden_tests && printf secret > hidden_tests/secret.txt",
        verify_cmd="true",
        hidden_cmd="test -f hidden_tests/secret.txt && test -f feature.txt",
        repo=str(repo), timeout_s=10,
    )
    result = run_verify(req)

    assert result.passed
    assert not (wt / "hidden_tests").exists()
    assert not git("status", "--porcelain", cwd=wt).stdout.strip()


def test_verify_saves_review_patch_for_committed_diff(tmp_path):
    repo = init_repo(tmp_path)
    wt, base_sha = _child_worktree(
        repo, "review-patch",
        lambda wt: (wt / "feature.txt").write_text("review me\n"),
    )

    req = VerifyRequest(task_id="review-task", worktree=str(wt), base_sha=base_sha,
                        verify_cmd="true", repo=str(repo), timeout_s=10)
    result = run_verify(req)

    assert result.passed
    assert result.patch_path
    patch = open(result.patch_path).read()
    assert "feature.txt" in patch
    assert "+review me" in patch
    latest = open(result.patch_path.rsplit("/", 1)[0] + "/review.patch").read()
    assert latest == patch


def test_setup_failed_when_setup_cmd_errors(tmp_path):
    """setup_cmd fails only in the worker's worktree (not at base_sha, where
    the baseline scratch checkout's setup_cmd run succeeds) -- e.g. the
    diff itself broke the install step. Distinct from baseline_broken, where
    setup_cmd (or verify_cmd) fails identically at base_sha too."""
    repo = init_repo(tmp_path)
    wt, base_sha = _child_worktree(repo, "feature2",
                                   lambda wt: (wt / "break_setup.txt").write_text("x\n"))

    req = VerifyRequest(task_id="t4", worktree=str(wt), base_sha=base_sha,
                        setup_cmd="test ! -f break_setup.txt", verify_cmd="true",
                        repo=str(repo), timeout_s=10)
    result = run_verify(req)

    assert not result.passed
    assert result.cause == "setup_failed"


def test_baseline_cache_key_includes_setup_cmd(tmp_path):
    """Two tasks can share (repo, base_sha, verify_cmd) but need different
    setup_cmd. Regression for a real bug: the baseline cache key used to omit
    setup_cmd, so the second task reused the first's cached baseline verdict
    and a missing-dependency failure got misclassified as tests_failed
    (blaming the worker) instead of baseline_broken (an environment
    problem) -- or, the other direction, a genuinely broken base_sha could
    grade as passing just because an earlier task's setup_cmd happened to
    paper over it."""
    repo = init_repo(tmp_path)

    def edit(wt):
        (wt / "unrelated.txt").write_text("x\n")

    wt_a, base_sha = _child_worktree(repo, "needs-setup-a", edit)
    wt_b, base_sha_b = _child_worktree(repo, "needs-setup-b", edit)
    assert base_sha == base_sha_b  # same base commit -> same cache key modulo setup_cmd

    # Task A: setup_cmd creates the file verify_cmd checks for -- baseline
    # (and the worker's own worktree) both pass.
    req_a = VerifyRequest(task_id="a", worktree=str(wt_a), base_sha=base_sha,
                          setup_cmd="touch installed.marker",
                          verify_cmd="test -f installed.marker",
                          repo=str(repo), timeout_s=10)
    result_a = run_verify(req_a)
    assert result_a.passed
    assert result_a.cause == "tests_passed"

    # Task B: same repo/base_sha/verify_cmd, no setup_cmd, a different
    # worktree. The baseline itself can't pass without the marker, so this
    # must be baseline_broken -- not a stale "passed" reused from task A's
    # cache entry, and not tests_failed (which would wrongly blame the
    # worker for an environment gap the task never asked to fix).
    req_b = VerifyRequest(task_id="b", worktree=str(wt_b), base_sha=base_sha,
                          verify_cmd="test -f installed.marker",
                          repo=str(repo), timeout_s=10)
    result_b = run_verify(req_b)
    assert not result_b.passed
    assert result_b.cause == "baseline_broken"


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


def test_editing_existing_protected_file_fails_verify(tmp_path):
    repo = init_repo(tmp_path)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "add existing test", cwd=repo)

    wt, base_sha = _child_worktree(
        repo, "edit_test",
        lambda wt: (wt / "tests" / "test_x.py").write_text("def test_x():\n    assert False\n"))

    req = VerifyRequest(task_id="t5", worktree=str(wt), base_sha=base_sha,
                        verify_cmd="true", repo=str(repo), timeout_s=10,
                        protected_paths=("tests/**",))
    result = run_verify(req)

    assert not result.passed
    assert result.cause == "protected_path_modified"


def test_default_allows_editing_existing_test_file(tmp_path):
    """Visible project tests are normal feature-work surface by default.
    protected_paths is opt-in for benchmark/hidden/instructor-owned checks."""
    repo = init_repo(tmp_path)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "add existing test", cwd=repo)

    wt, base_sha = _child_worktree(
        repo, "edit_test_default",
        lambda wt: (wt / "tests" / "test_x.py").write_text(
            "def test_x():\n    assert True\n\n"
            "def test_new_behavior():\n    assert True\n"))

    req = VerifyRequest(task_id="t6", worktree=str(wt), base_sha=base_sha,
                        verify_cmd="true", repo=str(repo), timeout_s=10)
    result = run_verify(req)

    assert result.passed
    assert result.cause == "tests_passed"
    assert result.tests_modified == ["tests/test_x.py"]


def test_adding_new_protected_file_passes_verify(tmp_path):
    """A brand-new file under tests/ didn't exist at base_sha to be gamed --
    only edits to a pre-existing protected file trip the check."""
    repo = init_repo(tmp_path)

    wt, base_sha = _child_worktree(
        repo, "new_test",
        lambda wt: ((wt / "tests").mkdir(), (wt / "tests" / "test_new.py").write_text(
            "def test_x():\n    assert True\n")))

    req = VerifyRequest(task_id="t7", worktree=str(wt), base_sha=base_sha,
                        verify_cmd="true", repo=str(repo), timeout_s=10)
    result = run_verify(req)

    assert result.passed
    assert result.cause == "tests_passed"
