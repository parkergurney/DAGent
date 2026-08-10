"""Visible verify-gate behavior and failure-signature accounting."""

from dataclasses import fields

from orchestrator.verify.gate import VerifyRequest, normalize_failure_signature, run_verify
from tests.helpers import git, init_repo


def test_verify_request_contains_only_public_worker_visible_inputs():
    assert {field.name for field in fields(VerifyRequest)} == {
        "task_id", "worktree", "base_sha", "verify_cmd", "timeout_s", "rerun_on_fail",
        "repo", "candidate_sha", "worker_dirty", "artifact_root",
    }


def _child_worktree(repo, name, edit):
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

    result = run_verify(VerifyRequest(
        task_id="t1", worktree=str(wt), base_sha=base_sha,
        verify_cmd="test -f flag.txt", repo=str(repo), timeout_s=10,
        candidate_sha=git("rev-parse", "HEAD", cwd=wt).stdout.strip(),
    ))
    assert not result.passed
    assert result.cause == "tests_failed"
    assert result.failure_signature


def test_verify_saves_review_patch_for_committed_diff(tmp_path):
    repo = init_repo(tmp_path)
    wt, base_sha = _child_worktree(
        repo, "review-patch", lambda wt: (wt / "feature.txt").write_text("review me\n")
    )
    candidate = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
    result = run_verify(VerifyRequest(
        task_id="review-task", worktree=str(wt), base_sha=base_sha,
        verify_cmd="true", repo=str(repo), candidate_sha=candidate, timeout_s=10,
    ))
    assert result.passed
    patch = open(result.patch_path).read()
    assert "feature.txt" in patch and "+review me" in patch
    assert open(result.patch_path.rsplit("/", 1)[0] + "/review.patch").read() == patch


def test_empty_diff_is_a_failure(tmp_path):
    repo = init_repo(tmp_path)
    base = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    result = run_verify(VerifyRequest(
        task_id="empty", worktree=str(repo), base_sha=base, verify_cmd="true",
    ))
    assert not result.passed and result.cause == "empty_diff"


def test_timeout_kills_check_process_group(tmp_path):
    repo = init_repo(tmp_path)
    (repo / "check.sh").write_text("#!/bin/sh\nsleep 5\n")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "add check", cwd=repo)
    wt, base_sha = _child_worktree(repo, "hang", lambda wt: (wt / "change.txt").write_text("change\n"))
    candidate = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
    result = run_verify(VerifyRequest(
        task_id="hang", worktree=str(wt), base_sha=base_sha, verify_cmd="sh check.sh",
        repo=str(repo), candidate_sha=candidate, timeout_s=0.1,
    ))
    assert not result.passed and result.cause == "timeout"


def test_flaky_failure_passes_on_rerun(tmp_path):
    repo = init_repo(tmp_path)
    wt, base_sha = _child_worktree(repo, "flake", lambda wt: (wt / "trigger").write_text(""))
    candidate = git("rev-parse", "HEAD", cwd=wt).stdout.strip()
    result = run_verify(VerifyRequest(
        task_id="flake", worktree=str(wt), base_sha=base_sha,
        verify_cmd="test -f trigger && test -f ran_once || (touch ran_once && exit 1)",
        repo=str(repo), candidate_sha=candidate, timeout_s=10,
    ))
    assert result.passed and result.flaky


def test_failure_signature_ignores_paths_addresses_and_lines():
    first = normalize_failure_signature("tests_failed", "/tmp/a/project.py:12: AssertionError at 0xabc")
    second = normalize_failure_signature("tests_failed", "/tmp/b/project.py:98: AssertionError at 0xdef")
    assert first == second
