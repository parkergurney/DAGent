import asyncio
import json
import shutil
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator import harbor_runtime
from orchestrator.harbor_agent import HarborOrchestratorAgent
from orchestrator.worker import WorkerIsolationError
from tests.helpers import init_repo


class FakeEnvironment:
    def __init__(self, root: Path):
        self.root = root
        self.commands = []

    async def upload_file(self, source, destination):
        target = self.root / destination.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)

    async def exec(self, **kwargs):
        self.commands.append(kwargs)
        return SimpleNamespace(return_code=0, stdout="", stderr="")


def test_installed_agent_contract_is_importable_without_harbor_runtime():
    agent = HarborOrchestratorAgent(logs_dir=Path("/tmp/harbor-agent-test"))
    assert agent.name() == "orchestrator"
    assert agent.version()
    for method in ("install", "run", "populate_context_post_run"):
        assert callable(getattr(agent, method))


def test_wrapper_invokes_in_container_runtime_without_secret_in_command(tmp_path):
    env = FakeEnvironment(tmp_path)
    agent = HarborOrchestratorAgent(
        logs_dir=tmp_path / "logs",
        config={"orchestrator": {"policy": "sequential", "max_concurrency": 1}},
    )

    asyncio.run(agent.run("change the file", env, SimpleNamespace()))

    command = env.commands[-1]["command"]
    assert "orchestrator.harbor_runtime" in command
    assert "ANTHROPIC_API_KEY" not in command
    assert "change the file" not in command


def test_claude_canary_uses_startup_env_for_credentials():
    task_dir = Path(__file__).parents[1] / "harbor/tasks/orchestrator-dag-canary-claude"
    task = tomllib.loads((task_dir / "task.toml").read_text())
    compose = (task_dir / "environment/docker-compose.yaml").read_text()

    assert task["environment"]["env"]["ORCH_AUTH_ENV_FILE"] == (
        "/run/secrets/orchestrator-claude-auth.env"
    )
    assert "services: {}" in compose
    assert "CLAUDE_CODE_OAUTH_TOKEN=" not in compose


def test_runtime_loads_only_allowlisted_auth_file_values(tmp_path, monkeypatch):
    token = "oauth-mounted-test-token"
    auth_file = tmp_path / "auth.env"
    auth_file.write_text(
        f"CLAUDE_CODE_OAUTH_TOKEN={token}\nUNSAFE_VALUE=should-not-load\n"
    )
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("UNSAFE_VALUE", raising=False)

    harbor_runtime._load_auth_env_file(auth_file)

    assert harbor_runtime.os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == token
    assert "UNSAFE_VALUE" not in harbor_runtime.os.environ


def test_runtime_exports_patch_metrics_and_result(tmp_path):
    repo = init_repo(tmp_path)
    instruction = tmp_path / "instruction.md"
    instruction.write_text("clean")
    config = tmp_path / "config.json"
    artifacts = tmp_path / "artifacts"
    config.write_text(json.dumps({
        "repo_root": str(repo),
        "artifact_root": str(artifacts),
        "db_path": str(tmp_path / "state.db"),
        "worktree_root": str(tmp_path / "worktrees"),
        "policy": "sequential",
        "fake_worker": True,
        "fake_supervisor": True,
        "verify_cmd": "true",
    }))

    assert asyncio.run(harbor_runtime.run_from_files(instruction, config)) == 0
    result = json.loads((artifacts / "result.json").read_text())
    metrics = json.loads((artifacts / "metrics.json").read_text())
    manifest = json.loads((artifacts / "run_manifest.json").read_text())
    assert result["state"] == "delivered"
    assert result["base_sha"] == (artifacts / "base_sha.txt").read_text().strip()
    assert result["candidate_sha"]
    assert metrics["attempts"] == 1
    assert manifest["policy"] == "sequential"
    assert manifest["repository"]["base_sha"] == result["base_sha"]
    assert manifest["authentication"]["values_recorded"] is False
    assert manifest["fault_target_reachability"]["enabled"] is False
    assert "output.txt" in (artifacts / "candidate.patch").read_text()
    live_events = [
        json.loads(line)["event"]
        for line in (artifacts / "live_diagnostics.jsonl").read_text().splitlines()
    ]
    assert live_events[:3] == [
        "runtime.started", "runtime.manifest_written", "runtime.scheduler_starting",
    ]
    assert "runtime.scheduler_finished" in live_events
    assert json.loads((artifacts / "live_status.json").read_text())["event"] == (
        "runtime.artifacts_finalized"
    )


def test_runtime_records_dependency_graph_manifest_and_result(tmp_path):
    repo = init_repo(tmp_path)
    instruction = tmp_path / "instruction.md"
    instruction.write_text("dependency graph")
    config = tmp_path / "config.json"
    artifacts = tmp_path / "artifacts"
    graph = [
        {"id": "first", "brief": "clean", "depends_on": [],
         "delivery_mode": "scout", "verify_cmd": "true"},
        {"id": "second", "brief": "clean", "depends_on": ["first"],
         "delivery_mode": "scout", "verify_cmd": "true"},
    ]
    config.write_text(json.dumps({
        "repo_root": str(repo), "artifact_root": str(artifacts),
        "db_path": str(tmp_path / "state.db"),
        "worktree_root": str(tmp_path / "worktrees"),
        "policy": "sequential", "fake_worker": True, "fake_supervisor": True,
        "tasks": graph,
    }))

    assert asyncio.run(harbor_runtime.run_from_files(instruction, config)) == 0
    result = json.loads((artifacts / "result.json").read_text())
    manifest = json.loads((artifacts / "run_manifest.json").read_text())
    assert result["state"] == "delivered"
    assert result["task_ids"] == ["first", "second"]
    assert result["task_states"] == {"first": "delivered", "second": "delivered"}
    assert manifest["task_graph"]["count"] == 2
    assert manifest["task_graph"]["tasks"][1]["depends_on"] == ["first"]
    assert manifest["task_graph"]["sha256"]


def test_runtime_fails_closed_without_harbor_isolation(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    instruction = tmp_path / "instruction.md"
    instruction.write_text("real worker")
    config = tmp_path / "config.json"
    artifacts = tmp_path / "artifacts"
    config.write_text(json.dumps({
        "repo_root": str(repo), "artifact_root": str(artifacts),
        "db_path": str(tmp_path / "state.db"),
        "worktree_root": str(tmp_path / "worktrees"),
        "policy": "sequential",
    }))
    monkeypatch.delenv("ORCH_HARBOR_ISOLATED", raising=False)
    monkeypatch.delenv("HARBOR_ISOLATED", raising=False)

    with pytest.raises(WorkerIsolationError):
        asyncio.run(harbor_runtime.run_from_files(instruction, config))
    metadata = json.loads((artifacts / "result.json").read_text())
    assert metadata["failure"]["type"] == "WorkerIsolationError"


def test_post_run_context_contains_state_and_metrics_without_credentials(tmp_path):
    logs = tmp_path / "logs" / "artifacts"
    logs.mkdir(parents=True)
    (logs / "result.json").write_text(json.dumps({
        "state": "delivered", "task_id": "task", "base_sha": "base",
        "candidate_sha": "candidate", "policy": "orchestrator",
    }))
    (logs / "metrics.json").write_text(json.dumps({
        "tokens_in": 3, "tokens_out": 4, "cost_usd": 0.01,
    }))
    context = SimpleNamespace(metadata=None, n_input_tokens=None,
                              n_output_tokens=None, cost_usd=None)

    HarborOrchestratorAgent(logs_dir=tmp_path / "logs").populate_context_post_run(context)

    observed = context.metadata["orchestrator"]
    assert observed["candidate_sha"] == "candidate"
    assert context.n_input_tokens == 3
    assert "ANTHROPIC_API_KEY" not in json.dumps(context.metadata)


def test_runtime_redacts_credentials_from_failure_metadata(tmp_path, monkeypatch):
    repo = init_repo(tmp_path)
    instruction = tmp_path / "instruction.md"
    instruction.write_text("task")
    artifacts = tmp_path / "artifacts"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "repo_root": str(repo), "artifact_root": str(artifacts),
        "db_path": str(tmp_path / "state.db"),
        "worktree_root": str(tmp_path / "worktrees"),
    }))
    secret = "sk-ant-test-secret"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)

    async def fail(**kwargs):
        del kwargs
        raise RuntimeError(f"backend rejected {secret}")

    monkeypatch.setattr(harbor_runtime, "run_instruction", fail)
    with pytest.raises(RuntimeError):
        asyncio.run(harbor_runtime.run_from_files(
            instruction,
            config,
        ))
    metadata = json.loads((artifacts / "result.json").read_text())
    assert secret not in json.dumps(metadata)
    assert (artifacts / "metrics.json").exists()
    assert (artifacts / "task_summary.json").exists()
    assert (artifacts / "candidate.patch").read_text() == ""


def test_runtime_redacts_subscription_oauth_token_from_failure_metadata(monkeypatch):
    token = "oauth-subscription-test-token"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)

    failure = harbor_runtime._safe_failure(RuntimeError(f"backend rejected {token}"))

    assert token not in json.dumps(failure)
    assert "[REDACTED_CREDENTIAL]" in failure["message"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" in harbor_runtime._AUTH_ENV_NAMES
