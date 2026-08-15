"""A deterministic, cheap-to-expensive visible verification evidence ladder.

The ladder is deliberately independent of the scheduler.  It can be used by
the existing verify gate today and gives the scheduler a structured result to
record when it is ready to emit stage events.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class EvidenceStage(StrEnum):
    """Ordered stages, from protocol checks to the complete visible gate."""

    PROTOCOL_RESULT = "protocol_result"
    GIT_CANDIDATE = "git_candidate"
    ARTIFACT_SCHEMA = "artifact_schema"
    TARGETED_CHECKS = "targeted_checks"
    FULL_VISIBLE_VERIFICATION = "full_visible_verification"


@dataclass(frozen=True)
class EvidenceStagePlan:
    stage: str
    applicable: bool
    commands: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        return _json_safe(asdict(self))


@dataclass(frozen=True)
class EvidencePlan:
    stages: list[EvidenceStagePlan]
    changed_paths: list[str] = field(default_factory=list)
    targeted_commands: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return _json_safe(asdict(self))

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


@dataclass
class EvidenceStageResult:
    stage: str
    applicable: bool
    passed: bool | None
    decisive: bool
    duration_s: float
    cost_usd: float
    commands: list[str] = field(default_factory=list)
    output_tail: str = ""
    failure_signature: str | None = None
    repair_feedback: str | None = None
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return _json_safe(asdict(self))


@dataclass
class EvidenceRunResult:
    """JSON-compatible outcome of a ladder execution."""

    passed: bool
    cause: str
    exit_code: int | None
    duration_s: float
    output_tail: str
    diff_stat: str
    tests_modified: list[str]
    output_path: str | None
    patch_path: str | None
    failure_signature: str | None
    stages: list[EvidenceStageResult]
    decisive_stage: str | None
    repair_feedback: str | None
    flaky: bool = False

    def to_dict(self) -> dict:
        value = _json_safe(asdict(self))
        value["stages"] = [stage.to_dict() for stage in self.stages]
        return value

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _json_safe(value: Any) -> Any:
    """Keep public evidence payloads serializable even for SDK objects."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {str(key): _json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe(item) for item in value]
        return str(value)


def _tail(value: str, limit: int = 2000) -> str:
    return (value or "")[-limit:]


def _run(command: str, cwd: Path, timeout_s: float) -> tuple[int | None, str, bool]:
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        shell=True,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        output, _ = proc.communicate(timeout=timeout_s)
        return proc.returncode, output or "", False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, 9)
        except ProcessLookupError:
            pass
        return None, proc.communicate()[0] or "", True


def _request_value(request: Any, name: str, default: Any = None) -> Any:
    if isinstance(request, dict):
        return request.get(name, default)
    return getattr(request, name, default)


def _json_value(value: Any) -> Any:
    if value in (None, "", {}):
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


def _artifact_specs(value: Any) -> list[dict]:
    value = _json_value(value)
    if value is None:
        return []
    if isinstance(value, dict) and "artifacts" in value:
        value = value["artifacts"]
    if isinstance(value, str):
        value = [value]
    if isinstance(value, dict):
        value = [{"path": key, **(item if isinstance(item, dict) else {})}
                 for key, item in value.items()]
    if not isinstance(value, list):
        raise ValueError("output artifacts must be a list or path mapping")
    result = []
    for item in value:
        if isinstance(item, str):
            result.append({"path": item, "required": True})
        elif isinstance(item, dict) and item.get("path"):
            result.append({"required": True, **item})
        else:
            raise ValueError("each output artifact must declare a path")
    return result


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _changed_paths(request: Any) -> tuple[str, str, list[str]]:
    worktree = Path(_request_value(request, "worktree"))
    repo = Path(_request_value(request, "repo") or worktree).resolve()
    candidate_sha = _request_value(request, "candidate_sha")
    git_cwd = repo if candidate_sha else worktree
    candidate_ref = candidate_sha or "HEAD"
    diff = _git("diff", "--stat", _request_value(request, "base_sha"), candidate_ref,
                cwd=git_cwd).stdout
    names = _git("diff", "--name-only", _request_value(request, "base_sha"), candidate_ref,
                 cwd=git_cwd).stdout
    return git_cwd, diff, [line.strip() for line in names.splitlines() if line.strip()]


def _test_path_candidates(root: Path, changed: str) -> list[Path]:
    path = Path(changed)
    stem = path.stem
    candidates = []
    if path.parts and path.parts[0] == "tests" and path.suffix == ".py":
        candidates.append(root / path)
    if path.suffix == ".py":
        candidates.extend([
            root / "tests" / f"test_{stem}.py",
            root / "tests" / f"{stem}_test.py",
            path.with_name(f"test_{stem}.py"),
            path.with_name(f"{stem}_test.py"),
        ])
    return candidates


def derive_targeted_checks(worktree: str | Path, changed_paths: list[str],
                           failure_signature: str | None = None) -> list[str]:
    """Derive only checks with an unambiguous test-file target.

    We intentionally do not turn arbitrary source changes into a full test
    command.  That would make the supposedly cheap stage duplicate the final
    gate and could give a false sense of targeted coverage.
    """
    root = Path(worktree)
    paths = list(changed_paths)
    if failure_signature:
        for token in failure_signature.replace("\n", " ").split():
            token = token.strip("'\"(),:;")
            candidate = Path(token)
            if candidate.suffix == ".py" and (root / candidate).is_file():
                paths.append(str(candidate))
    selected: set[str] = set()
    for changed in paths:
        for candidate in _test_path_candidates(root, changed):
            if candidate.is_file():
                selected.add(str(candidate.resolve().relative_to(root.resolve())))
    return [shlex.join([sys.executable, "-m", "pytest", "-q", "--", path])
            for path in sorted(selected)]


def _explicit_targeted_commands(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(command) for command in value if str(command).strip()]


def plan_evidence_ladder(request: Any, *, protocol_result: Any = None,
                         artifact_specs: Any = None, output_schema: Any = None,
                         targeted_commands: Any = None,
                         targeted_checks: Any = None,
                         failure_signature: str | None = None) -> EvidencePlan:
    """Build an ordered plan without invoking a worker or verification command."""
    try:
        _, _, changed = _changed_paths(request)
    except (OSError, TypeError):
        changed = []
    protocol = protocol_result if protocol_result is not None else _request_value(
        request, "protocol_result", None)
    specs = artifact_specs if artifact_specs is not None else _request_value(
        request, "output_artifacts", None)
    schema = output_schema if output_schema is not None else _request_value(
        request, "output_schema", None)
    explicit = targeted_commands if targeted_commands is not None else targeted_checks
    commands = _explicit_targeted_commands(explicit)
    if not commands:
        commands = derive_targeted_checks(
            _request_value(request, "worktree"), changed,
            failure_signature or _request_value(request, "failure_signature", None),
        )
    try:
        artifact_applicable = bool(_artifact_specs(specs) or _json_value(schema))
        artifact_reason = "declared artifacts or schema" if artifact_applicable else "no declarations"
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        artifact_applicable, artifact_reason = True, str(exc)
    verify_cmd = str(_request_value(request, "verify_cmd", "") or "")
    stages = [
        EvidenceStagePlan(EvidenceStage.PROTOCOL_RESULT.value, protocol is not None,
                          reason="protocol/result supplied" if protocol is not None else "not supplied"),
        EvidenceStagePlan(EvidenceStage.GIT_CANDIDATE.value, True,
                          reason="candidate and repository evidence"),
        EvidenceStagePlan(EvidenceStage.ARTIFACT_SCHEMA.value, artifact_applicable,
                          reason=artifact_reason),
        EvidenceStagePlan(EvidenceStage.TARGETED_CHECKS.value, bool(commands), commands=commands,
                          reason="safe test targets discovered" if commands else "no safe target"),
        EvidenceStagePlan(EvidenceStage.FULL_VISIBLE_VERIFICATION.value, bool(verify_cmd),
                          commands=[verify_cmd] if verify_cmd else [],
                          reason="final visible gate" if verify_cmd else "no command supplied"),
    ]
    return EvidencePlan(stages=stages, changed_paths=changed, targeted_commands=commands)


def _protocol_ok(value: Any) -> tuple[bool, str]:
    if isinstance(value, bool):
        return value, "protocol result supplied"
    if not isinstance(value, dict):
        return False, "protocol result must be a boolean or object"
    sdk_ok = value.get("sdk_success", value.get("result_ok", value.get("success", False)))
    exited = value.get("worker_exited", value.get("process_exited", True))
    metadata_ok = value.get("terminal_metadata_ok", value.get("metadata_ok", True))
    return bool(sdk_ok and exited and metadata_ok), str(value.get("message") or "protocol/result evidence")


def _stage(stage: EvidenceStagePlan, started: float, passed: bool | None,
           *, decisive: bool = False, output: str = "", commands: list[str] | None = None,
           details: dict | None = None, feedback: str | None = None,
           exit_code: int | None = None) -> EvidenceStageResult:
    detail = dict(details or {})
    if exit_code is not None:
        detail["exit_code"] = exit_code
    return EvidenceStageResult(
        stage=stage.stage,
        applicable=stage.applicable,
        passed=passed,
        decisive=decisive,
        duration_s=round(time.monotonic() - started, 3),
        cost_usd=0.0,
        commands=list(commands if commands is not None else stage.commands),
        output_tail=_tail(output),
        repair_feedback=feedback,
        details=detail,
    )


def _skipped(stage: EvidenceStagePlan, reason: str) -> EvidenceStageResult:
    return EvidenceStageResult(stage=stage.stage, applicable=False, passed=None, decisive=False,
                               duration_s=0.0, cost_usd=0.0, commands=stage.commands,
                               details={"skipped_reason": reason})


def _validate_artifacts(root: Path, specs_value: Any, schema_value: Any) -> tuple[bool, str, dict]:
    try:
        specs = _artifact_specs(specs_value)
        schema = _json_value(schema_value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return False, str(exc), {"reason": "malformed_artifact_schema"}
    checked = []
    for spec in specs:
        relative = str(spec["path"])
        path = (root / relative).resolve()
        if not _inside(path, root):
            return False, "artifact path escapes repository", {"reason": "artifact_escapes_repository", "path": relative}
        checked.append(relative)
        if spec.get("required", True) and not path.exists():
            return False, f"missing output artifact: {relative}", {"reason": "missing_output_artifact", "path": relative, "checked": checked}
        if path.exists() and spec.get("sha256"):
            import hashlib
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != spec["sha256"]:
                return False, f"artifact digest mismatch: {relative}", {"reason": "artifact_digest_mismatch", "path": relative, "checked": checked}
    if isinstance(schema, dict) and schema.get("required"):
        missing = sorted(set(map(str, schema["required"])) - set(checked))
        if missing:
            return False, "schema-required artifacts are missing", {"reason": "output_schema_missing", "missing": missing, "checked": checked}
    # Validate a small, deterministic JSON-schema subset when a JSON artifact
    # is explicitly declared.  Unsupported keywords are intentionally ignored.
    if isinstance(schema, dict) and (schema.get("type") or schema.get("properties")):
        json_files = [root / path for path in checked if path.lower().endswith(".json")]
        if json_files:
            try:
                document = json.loads(json_files[0].read_text())
            except (OSError, json.JSONDecodeError) as exc:
                return False, f"invalid JSON artifact: {exc}", {"reason": "invalid_json_artifact"}
            if schema.get("type") == "object" and not isinstance(document, dict):
                return False, "JSON artifact is not an object", {"reason": "schema_type_mismatch"}
            missing = sorted(set(schema.get("required", [])) - set(document))
            if missing:
                return False, "JSON artifact is missing required fields", {"reason": "schema_required_missing", "missing": missing}
    return True, "artifact/schema checks passed", {"checked": checked}


def _candidate_checkout(request: Any) -> tuple[Path, Path] | None:
    candidate = _request_value(request, "candidate_sha", None)
    if not candidate:
        return None
    repo = Path(_request_value(request, "repo") or _request_value(request, "worktree")).resolve()
    checkout = Path(tempfile.mkdtemp(prefix="orch_evidence_candidate_"))
    head = _git("rev-parse", candidate, cwd=repo)
    if head.returncode != 0 or not head.stdout.strip():
        checkout.rmdir()
        return None
    added = _git("worktree", "add", "-q", "--detach", str(checkout), head.stdout.strip(), cwd=repo)
    if added.returncode != 0:
        import shutil
        shutil.rmtree(checkout, ignore_errors=True)
        return None
    return checkout, repo


def _remove_candidate_checkout(checkout: Path, repo: Path) -> None:
    _git("worktree", "remove", "--force", str(checkout), cwd=repo)
    import shutil
    shutil.rmtree(checkout, ignore_errors=True)


def run_evidence_ladder(request: Any, *, protocol_result: Any = None,
                        artifact_specs: Any = None, output_schema: Any = None,
                        targeted_commands: Any = None, targeted_checks: Any = None,
                        failure_signature: str | None = None,
                        allow_empty_diff: bool | None = None) -> EvidenceRunResult:
    """Run applicable stages and stop at the first decisive failure."""
    started = time.monotonic()
    plan = plan_evidence_ladder(
        request, protocol_result=protocol_result, artifact_specs=artifact_specs,
        output_schema=output_schema, targeted_commands=targeted_commands,
        targeted_checks=targeted_checks, failure_signature=failure_signature,
    )
    stages: list[EvidenceStageResult] = []
    output = ""
    diff_stat = ""
    tests_modified: list[str] = []
    patch_path = None
    output_path = None
    exit_code = None
    cause = "tests_passed"
    decisive_stage = None
    repair_feedback = None
    flaky = False
    checkout_info = None
    check_root = Path(_request_value(request, "worktree"))
    allow_empty = (_request_value(request, "allow_empty_diff", False)
                   if allow_empty_diff is None else allow_empty_diff)

    def failure(cause_value: str, stage: EvidenceStageResult, *, code: int | None = None,
                feedback: str | None = None) -> None:
        nonlocal cause, decisive_stage, repair_feedback, exit_code
        cause, decisive_stage, repair_feedback, exit_code = cause_value, stage.stage, feedback, code
        stages.append(stage)

    for stage_plan in plan.stages:
        if decisive_stage:
            stages.append(_skipped(stage_plan, f"stopped after {decisive_stage}"))
            continue
        if not stage_plan.applicable:
            stages.append(_skipped(stage_plan, "not applicable"))
            continue
        stage_started = time.monotonic()
        if stage_plan.stage == EvidenceStage.PROTOCOL_RESULT.value:
            value = protocol_result if protocol_result is not None else _request_value(request, "protocol_result")
            passed, message = _protocol_ok(value)
            result = _stage(stage_plan, stage_started, passed, decisive=not passed, output=message,
                            details={"protocol_result": value},
                            feedback="repair worker terminal metadata and retry once" if not passed else None)
            if not passed:
                failure("protocol_failure", result, feedback=result.repair_feedback)
            else:
                stages.append(result)
        elif stage_plan.stage == EvidenceStage.GIT_CANDIDATE.value:
            worktree = Path(_request_value(request, "worktree"))
            repo = Path(_request_value(request, "repo") or worktree).resolve()
            dirty = _request_value(request, "worker_dirty")
            if dirty is None:
                dirty = _git("status", "--porcelain", cwd=worktree).stdout
            if str(dirty).strip():
                result = _stage(stage_plan, stage_started, False, decisive=True, output=str(dirty),
                                details={"reason": "uncommitted_changes"},
                                feedback="commit or discard the dirty candidate before verification")
                failure("uncommitted_changes", result, feedback=result.repair_feedback)
                continue
            candidate = _request_value(request, "candidate_sha")
            candidate_ref = candidate or "HEAD"
            git_cwd = repo if candidate else worktree
            base = _request_value(request, "base_sha")
            if candidate:
                ref = _git("rev-parse", candidate, cwd=repo)
                if ref.returncode != 0:
                    result = _stage(stage_plan, stage_started, False, decisive=True,
                                    output="candidate commit is unavailable",
                                    details={"reason": "candidate_missing"},
                                    feedback="retain or recreate the committed candidate")
                    failure("candidate_missing", result, feedback=result.repair_feedback)
                    continue
            diff_stat = _git("diff", "--stat", base, candidate_ref, cwd=git_cwd).stdout
            names = _git("diff", "--name-status", base, candidate_ref, cwd=git_cwd).stdout
            diff_names = [line.split("\t", 1)[-1] for line in names.splitlines() if line]
            tests_modified = [name for name in diff_names if "test" in Path(name).name.lower()]
            if not diff_names and not bool(allow_empty):
                result = _stage(stage_plan, stage_started, False, decisive=True,
                                details={"reason": "empty_diff"},
                                feedback="produce a committed candidate or declare an explicit no-change outcome")
                failure("empty_diff", result, feedback=result.repair_feedback)
                continue
            if not diff_names:
                cause = "no_change"
            patch = _git("diff", "--binary", base, candidate_ref, cwd=git_cwd).stdout
            root = Path(_request_value(request, "artifact_root") or "data") / str(_request_value(request, "task_id"))
            root.mkdir(parents=True, exist_ok=True)
            patch_path = str(root / "review.patch")
            Path(patch_path).write_text(patch)
            result = _stage(stage_plan, stage_started, True, details={"changed_paths": diff_names},
                            output=diff_stat)
            stages.append(result)
            checkout_info = _candidate_checkout(request)
            if _request_value(request, "candidate_sha") and checkout_info is None:
                result = _stage(stage_plan, stage_started, False, decisive=True,
                                output="could not materialize the durable candidate",
                                details={"reason": "candidate_checkout_failed"},
                                feedback="retain a candidate reachable from the repository")
                failure("candidate_checkout_failed", result, feedback=result.repair_feedback)
        elif stage_plan.stage == EvidenceStage.ARTIFACT_SCHEMA.value:
            check_root = checkout_info[0] if checkout_info else Path(_request_value(request, "worktree"))
            specs = artifact_specs if artifact_specs is not None else _request_value(request, "output_artifacts")
            schema = output_schema if output_schema is not None else _request_value(request, "output_schema")
            passed, message, details = _validate_artifacts(check_root, specs, schema)
            result = _stage(stage_plan, stage_started, passed, decisive=not passed, output=message,
                            details=details,
                            feedback="repair the declared output artifact or schema contract" if not passed else None)
            if not passed:
                failure("artifact_validation_failed", result, feedback=result.repair_feedback)
            else:
                stages.append(result)
        elif stage_plan.stage == EvidenceStage.TARGETED_CHECKS.value:
            check_root = checkout_info[0] if checkout_info else Path(_request_value(request, "worktree"))
            command_outputs = []
            passed = True
            for command in stage_plan.commands:
                code, command_output, timed_out = _run(command, check_root,
                                                        float(_request_value(request, "timeout_s", 600)))
                command_outputs.append(command_output)
                if timed_out:
                    passed, exit_code, cause = False, None, "targeted_checks_timeout"
                    break
                if code != 0:
                    passed, exit_code, cause = False, code, "targeted_checks_failed"
                    break
            result = _stage(stage_plan, stage_started, passed, decisive=not passed,
                            output="\n".join(command_outputs), details={"checks_run": len(command_outputs)},
                            feedback="repair the targeted failure before running full verification" if not passed else None,
                            exit_code=exit_code)
            if not passed:
                failure(cause, result, code=exit_code, feedback=result.repair_feedback)
            else:
                stages.append(result)
        elif stage_plan.stage == EvidenceStage.FULL_VISIBLE_VERIFICATION.value:
            check_root = checkout_info[0] if checkout_info else Path(_request_value(request, "worktree"))
            command = stage_plan.commands[0]
            code, command_output, timed_out = _run(command, check_root,
                                                   float(_request_value(request, "timeout_s", 600)))
            if timed_out:
                passed, exit_code, cause = False, None, "timeout"
            else:
                passed, exit_code, cause = code == 0, code, "tests_passed" if code == 0 else "tests_failed"
            rerun = bool(_request_value(request, "rerun_on_fail", True))
            if not passed and not timed_out and rerun:
                code2, output2, timed_out2 = _run(command, check_root,
                                                   float(_request_value(request, "timeout_s", 600)))
                if not timed_out2 and code2 == 0:
                    passed, exit_code, command_output, flaky = True, code2, output2, True
                else:
                    exit_code, command_output = code2, output2
            result = _stage(stage_plan, stage_started, passed, decisive=not passed,
                            output=command_output, details={"rerun_on_fail": rerun},
                            feedback="repair the visible verification failure" if not passed else None,
                            exit_code=exit_code)
            if not passed:
                failure(cause, result, code=exit_code, feedback=result.repair_feedback)
            else:
                stages.append(result)
            output = command_output

    if checkout_info:
        _remove_candidate_checkout(*checkout_info)
    passed = decisive_stage is None
    if not passed:
        output_path_root = Path(_request_value(request, "artifact_root") or "data") / str(_request_value(request, "task_id"))
        output_path_root.mkdir(parents=True, exist_ok=True)
        output_path = str(output_path_root / f"evidence_{decisive_stage}_{int(time.time() * 1000)}.log")
        Path(output_path).write_text(output or "\n".join(stage.output_tail for stage in stages))
    return EvidenceRunResult(
        passed=passed,
        cause=cause,
        exit_code=exit_code,
        duration_s=round(time.monotonic() - started, 3),
        output_tail=_tail(output or "\n".join(stage.output_tail for stage in stages)),
        diff_stat=diff_stat,
        tests_modified=tests_modified,
        output_path=output_path,
        patch_path=patch_path,
        failure_signature=None,
        stages=stages,
        decisive_stage=decisive_stage,
        repair_feedback=repair_feedback,
        flaky=flaky,
    )


# Short aliases make the API discoverable without coupling callers to the
# implementation's internal naming.
plan_evidence = plan_evidence_ladder
run_evidence = run_evidence_ladder


__all__ = [
    "EvidenceStage", "EvidenceStagePlan", "EvidencePlan", "EvidenceStageResult",
    "EvidenceRunResult", "derive_targeted_checks", "plan_evidence_ladder",
    "run_evidence_ladder", "plan_evidence", "run_evidence",
]
