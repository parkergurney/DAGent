"""In-container entry point used by :mod:`orchestrator.harbor_agent`.

The Harbor controller cannot use its own filesystem as the task checkout.  The
installed-agent wrapper therefore invokes this module through Harbor's
``exec_as_agent`` helper.  This module is intentionally small: all scheduling,
recovery, verification, and metrics behavior remains in ``harbor.run_instruction``.
"""

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import time
from pathlib import Path

from orchestrator.harbor import export_patch, export_task_summary, run_instruction
from orchestrator.metrics import export_metrics
from orchestrator.experiment import classify_cell

_AUTH_ENV_NAMES = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "AWS_REGION", "AWS_DEFAULT_REGION",
})
_TRUE = frozenset({"1", "true", "yes", "on"})
_SENSITIVE_CONFIG_WORDS = frozenset({"key", "token", "secret", "password", "credential"})


def _bool(value, default=False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in _TRUE


def _config(path: str | None) -> dict:
    if not path:
        return {}
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError("orchestrator configuration must be a JSON object")
    nested = data.get("orchestrator")
    return nested if isinstance(nested, dict) else data


def _value(settings: dict, name: str, env_name: str, default):
    value = settings.get(name)
    if value is not None:
        return value
    return os.environ.get(env_name, default)


def _int(settings: dict, name: str, env_name: str, default: int) -> int:
    return int(_value(settings, name, env_name, default))


def _task_specs(settings: dict) -> list[dict] | None:
    specs = settings.get("tasks")
    if specs is None:
        specs = settings.get("task_graph")
    if specs is None:
        return None
    if not isinstance(specs, list):
        raise ValueError("orchestrator tasks/task_graph must be a list")
    return specs


def _task_graph_metadata(settings: dict) -> dict:
    specs = _task_specs(settings)
    if specs is None:
        return {"count": 1, "sha256": None, "tasks": []}
    canonical = json.dumps(specs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "count": len(specs),
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "tasks": [
            {"id": str(spec.get("id")), "depends_on": list(spec.get("depends_on") or [])}
            for spec in specs
        ],
    }


def _fault_injection(settings: dict) -> dict | None:
    fault = settings.get("fault_injection")
    if fault is None:
        return None
    if not isinstance(fault, dict):
        raise ValueError("orchestrator fault_injection must be an object")
    return dict(fault)


def _repo_sha(repo_root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _public_resource_config(value, key: str = ""):
    """Keep resource metadata while preventing accidental credential copies."""
    if any(word in key.lower() for word in _SENSITIVE_CONFIG_WORDS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(name): _public_resource_config(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_public_resource_config(item, key) for item in value]
    return value


def _package_version() -> str:
    try:
        return importlib.metadata.version("agent-orchestrator")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.1"


def _manifest(settings: dict, *, instruction: str, repo_root: Path, base_sha: str,
              policy: str, verify_cmd: str, artifact_root: Path,
              task_graph: dict, fault_injection: dict | None,
              deterministic_crash_recovery: bool,
              adaptive_scheduling: bool = True,
              protocol_recovery_v2: bool = True,
              evidence_ladder: bool = True) -> dict:
    """Build the immutable comparison record before any worker is launched."""
    instruction_sha = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    task_hash = _value(
        settings, "task_definition_sha256", "ORCH_TASK_DEFINITION_SHA256", None
    )
    graph_sha = task_graph.get("sha256")
    package_sha = task_hash or hashlib.sha256(json.dumps(
        {"instruction_sha256": instruction_sha, "task_graph_sha256": graph_sha},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    graph_id = str(_value(
        settings, "graph_id", "ORCH_GRAPH_ID", graph_sha[:12] if graph_sha else "single-task"
    ))
    resource_config = settings.get("resource_config", {})
    if not isinstance(resource_config, dict):
        raise ValueError("resource_config must be an object")
    return {
        "schema_version": 1,
        "orchestrator_version": _package_version(),
        "harbor_version": _value(settings, "harbor_version", "HARBOR_VERSION", "unknown"),
        "trial_id": _value(settings, "trial_id", "HARBOR_TRIAL_ID", "unknown"),
        "seed": _value(settings, "seed", "HARBOR_TRIAL_SEED", "unknown"),
        "task_definition_sha256": task_hash or "not-provided",
        "task_package_sha256": package_sha,
        "graph_id": graph_id,
        "graph_shape": _value(settings, "graph_shape", "ORCH_GRAPH_SHAPE", graph_id),
        "backend_track": _value(settings, "backend_track", "ORCH_BACKEND_TRACK", "unspecified"),
        "fault_profile": _value(settings, "fault_profile", "ORCH_FAULT_PROFILE", "clean"),
        "task_graph": task_graph,
        "fault_injection": fault_injection,
        "fault_target_reachability": {
            "enabled": bool(fault_injection and fault_injection.get("target_reachable")),
            "target": (fault_injection or {}).get("task_id") if fault_injection else None,
            "requires_root": bool(fault_injection and fault_injection.get("target_reachable")),
        },
        "deterministic_crash_recovery": deterministic_crash_recovery,
        "protocol_recovery_v2": protocol_recovery_v2,
        "deterministic_recovery": _bool(_value(settings, "deterministic_recovery",
                                                "ORCH_DETERMINISTIC_RECOVERY", True)),
        "adaptive_scheduling": adaptive_scheduling,
        "evidence_ladder": evidence_ladder,
        "instruction_sha256": instruction_sha,
        "repository": {"root": str(repo_root), "base_sha": base_sha},
        "policy": policy,
        "backend": _value(settings, "backend", "ORCH_BACKEND", "anthropic"),
        "context_length": _value(
            settings, "context_length", "ORCH_CONTEXT_LENGTH", "unknown"
        ),
        "model": {
            "worker": _value(settings, "worker_model", "ORCH_WORKER_MODEL", "claude-sonnet-5"),
            "supervisor": _value(
                settings, "supervisor_model", "ORCH_SUPERVISOR_MODEL", "claude-sonnet-5"
            ),
        },
        "limits": {
            "max_concurrency": _int(settings, "max_concurrency", "ORCH_MAX_CONCURRENCY", 4),
            "max_retries": _int(settings, "max_retries", "ORCH_MAX_RETRIES", 2),
            "worker_timeout_s": _int(settings, "stall_threshold_s", "ORCH_WORKER_TIMEOUT_S", 300),
            "verify_timeout_s": _int(settings, "verify_timeout_s", "ORCH_VERIFY_TIMEOUT_S", 600),
            "wait_ceiling_s": _int(settings, "wait_ceiling_s", "ORCH_WAIT_CEILING_S", 1800),
        },
        "resource_config": _public_resource_config(resource_config),
        "verifier": {
            "mode": _value(settings, "verifier_mode", "ORCH_VERIFIER_MODE", "separate"),
            "visible_command": verify_cmd,
            "identity": _value(settings, "verifier_identity", "ORCH_VERIFIER_IDENTITY", "unspecified"),
            "artifact_root": str(artifact_root),
        },
        "authentication": {
            "source": "Harbor-injected environment",
            "mechanism": _value(settings, "authentication_mechanism", "ORCH_AUTH_MECHANISM", "environment"),
            "values_recorded": False,
        },
    }


def _safe_failure(exc: BaseException) -> dict:
    # Do not serialize arbitrary exception context.  In particular, worker
    # authentication values are never copied into result metadata.
    message = str(exc)
    for name in _AUTH_ENV_NAMES:
        secret = os.environ.get(name)
        if secret:
            message = message.replace(secret, "[REDACTED_CREDENTIAL]")
    return {"type": type(exc).__name__, "message": message[:1000]}


async def run_from_files(instruction_file: str | Path, config_file: str | Path | None = None) -> int:
    started = time.monotonic()
    settings = _config(str(config_file) if config_file else None)
    task_specs = _task_specs(settings)
    task_graph = _task_graph_metadata(settings)
    fault_injection = _fault_injection(settings)
    deterministic_crash_recovery = _bool(
        _value(settings, "deterministic_crash_recovery", "ORCH_FAST_CRASH_RECOVERY", True),
        True,
    )
    instruction = Path(instruction_file).read_text()
    repo_root = Path(_value(settings, "repo_root", "ORCH_REPO_ROOT", "/app")).resolve()
    # Harbor implicitly transfers the complete /logs/artifacts directory to a
    # separate verifier. Keep scheduler diagnostics in a private run directory
    # and publish only the declared patch/metadata files below.
    artifact_root = Path(_value(settings, "artifact_root", "ORCH_ARTIFACT_ROOT", "/logs/artifacts"))
    run_artifact_root = Path(_value(
        settings, "run_artifact_root", "ORCH_RUN_ARTIFACT_ROOT",
        f"/tmp/orchestrator-artifacts-{os.getpid()}",
    ))
    artifact_root.mkdir(parents=True, exist_ok=True)
    run_artifact_root.mkdir(parents=True, exist_ok=True)
    base_sha = _repo_sha(repo_root)
    (artifact_root / "base_sha.txt").write_text(base_sha + "\n")

    # The marker is part of the Harbor task image, not a user prompt or a
    # worker suggestion.  A direct invocation without the marker remains
    # rejected by run_instruction's existing external-isolation guard.
    harbor_isolated = _bool(
        os.environ.get("ORCH_HARBOR_ISOLATED") or os.environ.get("HARBOR_ISOLATED")
    )
    requested_isolation = _bool(settings.get("external_isolation"), harbor_isolated)
    worker_env = {
        name: value for name, value in os.environ.items() if name in _AUTH_ENV_NAMES
    }
    db_path = _value(settings, "db_path", "ORCH_DB_PATH", "/tmp/orchestrator.db")
    worktree_root = _value(
        settings, "worktree_root", "ORCH_WORKTREE_ROOT", "/tmp/orchestrator-worktrees"
    )
    policy = str(_value(settings, "policy", "ORCH_POLICY", "orchestrator"))
    title = str(_value(settings, "title", "ORCH_TITLE", "Harbor task"))
    verify_cmd = _value(settings, "verify_cmd", "ORCH_VERIFY_CMD", "true")
    manifest = _manifest(
        settings, instruction=instruction, repo_root=repo_root, base_sha=base_sha,
        policy=policy, verify_cmd=verify_cmd, artifact_root=artifact_root,
        task_graph=task_graph, fault_injection=fault_injection,
        deterministic_crash_recovery=deterministic_crash_recovery,
        adaptive_scheduling=_bool(_value(settings, "adaptive_scheduling",
                                          "ORCH_ADAPTIVE_SCHEDULING", True)),
        protocol_recovery_v2=_bool(_value(settings, "protocol_recovery_v2",
                                          "ORCH_PROTOCOL_RECOVERY_V2", True)),
        evidence_ladder=_bool(_value(settings, "evidence_ladder",
                                     "ORCH_EVIDENCE_LADDER", True)),
    )
    _write_json(
        artifact_root / "run_manifest.json",
        manifest,
    )

    result_metadata = {
        "schema_version": 1,
        "policy": policy,
        "base_sha": base_sha,
        "candidate_sha": None,
        "state": "failed",
        "metrics": {},
    }
    try:
        result = await run_instruction(
            instruction=instruction,
            repo_root=repo_root,
            db_path=db_path,
            worktree_root=worktree_root,
            title=title,
            policy=policy,
            verify_cmd=verify_cmd,
            max_retries=_int(settings, "max_retries", "ORCH_MAX_RETRIES", 2),
            max_concurrency=_int(settings, "max_concurrency", "ORCH_MAX_CONCURRENCY", 4),
            worker_env=worker_env,
            fake_worker=_bool(_value(settings, "fake_worker", "ORCH_FAKE_WORKER", False)),
            fake_supervisor=_bool(_value(settings, "fake_supervisor", "ORCH_FAKE_SUPERVISOR", False)),
            external_isolation=requested_isolation and harbor_isolated,
            worker_model=_value(settings, "worker_model", "ORCH_WORKER_MODEL", None),
            supervisor_model=_value(settings, "supervisor_model", "ORCH_SUPERVISOR_MODEL", None),
            artifact_root=run_artifact_root,
            verify_timeout_s=_int(settings, "verify_timeout_s", "ORCH_VERIFY_TIMEOUT_S", 600),
            stall_threshold_s=_int(settings, "stall_threshold_s", "ORCH_WORKER_TIMEOUT_S", 300),
            wait_ceiling_s=_int(settings, "wait_ceiling_s", "ORCH_WAIT_CEILING_S", 1800),
            config_path=_value(settings, "config_path", "ORCH_CONFIG_PATH", None),
            base_branch=str(_value(settings, "base_branch", "ORCH_BASE_BRANCH", "main")),
            task_specs=task_specs,
            fault_injection=fault_injection,
            deterministic_crash_recovery=deterministic_crash_recovery,
            adaptive_scheduling=_bool(_value(settings, "adaptive_scheduling",
                                              "ORCH_ADAPTIVE_SCHEDULING", True)),
            protocol_recovery_v2=_bool(_value(settings, "protocol_recovery_v2",
                                               "ORCH_PROTOCOL_RECOVERY_V2", True)),
            evidence_ladder=_bool(_value(settings, "evidence_ladder",
                                         "ORCH_EVIDENCE_LADDER", True)),
            deterministic_recovery=_bool(_value(settings, "deterministic_recovery",
                                                "ORCH_DETERMINISTIC_RECOVERY", True)),
        )
        result_metadata.update({
            "task_id": result.task_id,
            "task_ids": list(result.task_ids),
            "task_states": result.task_states,
            "state": result.state,
            "base_sha": result.base_sha,
            "candidate_sha": result.candidate_sha,
            "metrics": result.metrics,
        })
        result.metrics["wall_time_s"] = round(time.monotonic() - started, 3)
        result.metrics.update(classify_cell(
            manifest=manifest, metrics=result.metrics, result=result_metadata,
            run_completed=True,
        ))
        result_metadata["metrics"] = result.metrics
        _write_json(artifact_root / "task_summary.json", {
            "schema_version": 1,
            "tasks": export_task_summary(db_path),
        })
        if result.candidate_sha:
            export_patch(
                repo_root, base_sha=result.base_sha or base_sha,
                candidate_sha=result.candidate_sha,
                destination=artifact_root / "candidate.patch",
            )
        else:
            (artifact_root / "candidate.patch").write_text("")
        (artifact_root / "metrics.json").write_text(
            json.dumps(result.metrics, indent=2, sort_keys=True) + "\n"
        )
        _write_json(artifact_root / "result.json", result_metadata)
        return 0
    except BaseException as exc:
        try:
            result_metadata["metrics"] = export_metrics(db_path)
            result_metadata["metrics"]["wall_time_s"] = round(time.monotonic() - started, 3)
            result_metadata["metrics"].update(classify_cell(
                manifest=manifest, metrics=result_metadata["metrics"],
                result=result_metadata, run_completed=False,
            ))
        except Exception:
            # The original failure is authoritative; a missing/incomplete DB
            # must not prevent Harbor from receiving failure artifacts.
            pass
        result_metadata["failure"] = _safe_failure(exc)
        try:
            _write_json(artifact_root / "task_summary.json", {
                "schema_version": 1,
                "tasks": export_task_summary(db_path),
            })
        except Exception:
            pass
        _write_json(artifact_root / "result.json", result_metadata)
        _write_json(artifact_root / "metrics.json", result_metadata["metrics"])
        (artifact_root / "candidate.patch").write_text("")
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instruction-file", required=True)
    parser.add_argument("--config-file")
    args = parser.parse_args(argv)
    import asyncio
    asyncio.run(run_from_files(args.instruction_file, args.config_file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
