"""Focused tests for the opt-in visible verification evidence ladder."""

from dataclasses import fields

from dagent.metrics import metrics_for
from dagent.store import append_event, connect
from dagent.verify.evidence import EvidenceStage, run_evidence_ladder
from dagent.verify.gate import VerifyRequest, run_verify
from tests.helpers import git, init_repo


def _candidate(repo, tmp_path, name, edit):
    base = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    worktree = tmp_path / name
    git("worktree", "add", "-q", "-b", name, str(worktree), "main", cwd=repo)
    edit(worktree)
    git("add", "-A", cwd=worktree)
    git("commit", "-qm", name, cwd=worktree)
    candidate = git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
    return worktree, base, candidate


def _request(worktree, base, *, repo=None, candidate=None, verify_cmd="true"):
    return VerifyRequest(
        task_id="evidence-test",
        worktree=str(worktree),
        base_sha=base,
        verify_cmd=verify_cmd,
        timeout_s=10,
        repo=str(repo or worktree),
        candidate_sha=candidate,
    )


def _stages(result):
    return {stage["stage"]: stage for stage in result.evidence["stages"]}


def test_protocol_failure_is_decisive_and_skips_expensive_checks(tmp_path):
    repo = init_repo(tmp_path)
    base = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    marker = tmp_path / "full-ran"
    result = run_verify(
        _request(repo, base, verify_cmd=f"touch {marker} && false"),
        evidence_ladder=True,
        protocol_result=False,
    )

    assert not result.passed
    assert result.cause == "protocol_failure"
    assert result.evidence["decisive_stage"] == EvidenceStage.PROTOCOL_RESULT.value
    assert not marker.exists()
    assert _stages(result)[EvidenceStage.FULL_VISIBLE_VERIFICATION.value]["passed"] is None


def test_dirty_tree_fails_before_candidate_or_verification(tmp_path):
    repo = init_repo(tmp_path)
    base = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    (repo / "dirty.txt").write_text("not committed\n")

    result = run_verify(_request(repo, base, verify_cmd="false"), evidence_ladder=True)

    assert not result.passed
    assert result.cause == "uncommitted_changes"
    assert result.evidence["decisive_stage"] == EvidenceStage.GIT_CANDIDATE.value


def test_artifact_failure_stops_before_targeted_and_full_checks(tmp_path):
    repo = init_repo(tmp_path)
    worktree, base, candidate = _candidate(
        repo, tmp_path, "artifact", lambda path: (path / "feature.txt").write_text("candidate\n")
    )
    marker = tmp_path / "checks-ran"
    result = run_verify(
        _request(worktree, base, repo=repo, candidate=candidate,
                 verify_cmd=f"touch {marker} && true"),
        evidence_ladder=True,
        artifact_specs=["missing.json"],
    )

    assert not result.passed
    assert result.cause == "artifact_validation_failed"
    assert result.evidence["decisive_stage"] == EvidenceStage.ARTIFACT_SCHEMA.value
    assert not marker.exists()


def test_targeted_failure_is_decisive_and_full_gate_is_not_run(tmp_path):
    repo = init_repo(tmp_path)
    worktree, base, candidate = _candidate(
        repo, tmp_path, "targeted", lambda path: (path / "feature.txt").write_text("candidate\n")
    )
    marker = tmp_path / "full-ran"
    result = run_verify(
        _request(worktree, base, repo=repo, candidate=candidate,
                 verify_cmd=f"touch {marker} && true"),
        evidence_ladder=True,
        targeted_commands=["false"],
    )

    assert not result.passed
    assert result.cause == "targeted_checks_failed"
    assert result.evidence["decisive_stage"] == EvidenceStage.TARGETED_CHECKS.value
    assert not marker.exists()


def test_full_visible_verification_runs_after_cheap_stages(tmp_path):
    repo = init_repo(tmp_path)
    worktree, base, candidate = _candidate(
        repo, tmp_path, "full", lambda path: (path / "feature.txt").write_text("candidate\n")
    )
    result = run_verify(
        _request(worktree, base, repo=repo, candidate=candidate,
                 verify_cmd="test -f feature.txt"),
        evidence_ladder=True,
    )

    assert result.passed
    assert result.cause == "tests_passed"
    assert result.evidence["decisive_stage"] is None
    stages = result.evidence["stages"]
    assert stages[-1]["stage"] == EvidenceStage.FULL_VISIBLE_VERIFICATION.value
    assert stages[-1]["passed"] is True
    assert all("duration_s" in stage and "commands" in stage for stage in stages)


def test_targeted_checks_are_derived_only_for_matching_test_files(tmp_path):
    repo = init_repo(tmp_path)
    worktree, base, candidate = _candidate(
        repo, tmp_path, "derive", lambda path: (
            (path / "feature.py").write_text("VALUE = 1\n"),
            (path / "tests").mkdir(),
            (path / "tests" / "test_feature.py").write_text("def test_feature(): pass\n"),
        )
    )
    request = _request(worktree, base, repo=repo, candidate=candidate)
    result = run_evidence_ladder(request)

    targeted = next(stage for stage in result.stages
                    if stage.stage == EvidenceStage.TARGETED_CHECKS.value)
    assert targeted.applicable
    assert "test_feature.py" in targeted.commands[0]


def test_metrics_aggregate_evidence_stages_additively():
    conn = connect(":memory:")
    append_event(conn, source="verifier", type="verify.evidence_stage", task_id=None,
                 payload={"stage": "protocol_result", "duration_s": 0.2})
    append_event(conn, source="verifier", type="verify.evidence_stage", task_id=None,
                 payload={"stage": "full_visible_verification", "duration_s": 1.5})
    metrics = metrics_for(conn)

    assert metrics.evidence_stage_counts == {
        "protocol_result": 1, "full_visible_verification": 1,
    }
    assert metrics.evidence_stage_timing_s == {
        "protocol_result": 0.2, "full_visible_verification": 1.5,
    }


def test_legacy_verify_request_and_behavior_remain_compatible(tmp_path):
    assert {field.name for field in fields(VerifyRequest)} == {
        "task_id", "worktree", "base_sha", "verify_cmd", "timeout_s", "rerun_on_fail",
        "repo", "candidate_sha", "worker_dirty", "artifact_root",
    }
    repo = init_repo(tmp_path)
    worktree, base, candidate = _candidate(
        repo, tmp_path, "legacy", lambda path: (path / "feature.txt").write_text("candidate\n")
    )
    result = run_verify(_request(worktree, base, repo=repo, candidate=candidate,
                                 verify_cmd="true"))

    assert result.passed
    assert result.evidence is None
