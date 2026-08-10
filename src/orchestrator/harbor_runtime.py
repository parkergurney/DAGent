"""In-container entry point used by :mod:`orchestrator.harbor_agent`.

The Harbor controller cannot use its own filesystem as the task checkout.  The
installed-agent wrapper therefore invokes this module through Harbor's
``exec_as_agent`` helper.  This module is intentionally small: all scheduling,
recovery, verification, and metrics behavior remains in ``harbor.run_instruction``.
"""

import argparse
import json
import os
import subprocess
from pathlib import Path

from orchestrator.harbor import export_patch, run_instruction
from orchestrator.metrics import export_metrics

_AUTH_ENV_NAMES = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "AWS_REGION", "AWS_DEFAULT_REGION",
})
_TRUE = frozenset({"1", "true", "yes", "on"})


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


def _repo_sha(repo_root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


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
    settings = _config(str(config_file) if config_file else None)
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
        )
        result_metadata.update({
            "task_id": result.task_id,
            "state": result.state,
            "base_sha": result.base_sha,
            "candidate_sha": result.candidate_sha,
            "metrics": result.metrics,
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
        except Exception:
            # The original failure is authoritative; a missing/incomplete DB
            # must not prevent Harbor from receiving failure artifacts.
            pass
        result_metadata["failure"] = _safe_failure(exc)
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
