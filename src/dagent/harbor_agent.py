"""Harbor installed-agent adapter for the orchestrator.

The adapter is deliberately an installed agent rather than a Harbor external
agent: the scheduler must be able to create several local Claude SDK workers
inside the same Harbor task container.  Harbor remains responsible for the
outer container and separate verifier boundary.
"""

import json
import os
import shlex
import tarfile
import tempfile
import tomllib
from pathlib import Path

try:  # Keep the scheduler package importable in local development/tests.
    from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
except ImportError:  # pragma: no cover - Harbor supplies these in a trial image.
    def with_prompt_template(function):
        return function

    class BaseInstalledAgent:  # type: ignore[no-redef]
        SUPPORTS_CONFIG = True

        def __init__(self, logs_dir: Path, *args, extra_env=None, config=None, **kwargs):
            del args, kwargs
            self.logs_dir = Path(logs_dir)
            self._extra_env = dict(extra_env or {})
            self._config = config

        @staticmethod
        def name() -> str:
            return "orchestrator"

        async def ensure_system_dependencies(self, environment, dependencies):
            del environment, dependencies

        async def exec_as_agent(self, environment, **kwargs):
            return await environment.exec(**kwargs)

        async def exec_as_root(self, environment, **kwargs):
            return await environment.exec(**kwargs)


class HarborOrchestratorAgent(BaseInstalledAgent):
    """Run the existing orchestrator scheduler inside a Harbor trial."""

    SUPPORTS_ATIF = False
    SUPPORTS_CONFIG = True
    VERSION = "0.0.1"
    _AUTH_MOUNT_PATH = "/run/secrets/dagent-claude-auth.env"
    _AUTH_RUNTIME_PATH = "/tmp/dagent-claude-auth.env"

    def __init__(self, logs_dir: Path, *args, config=None, **kwargs):
        # Harbor 0.20 forwards unknown agent kwargs to BaseAgent, which
        # intentionally discards them. Capture our comparison configuration
        # before delegating so policy/seed/limits reach the in-container
        # runtime. Newer Harbor versions may also populate ``_config``.
        self._orchestrator_config = config
        super().__init__(logs_dir, *args, **kwargs)

    @staticmethod
    def name() -> str:
        return "orchestrator"

    def version(self) -> str | None:
        return self.VERSION

    def _settings(self) -> dict:
        source = self._orchestrator_config
        if source is None:
            source = getattr(self, "_config", None)
        if isinstance(source, dict):
            data = source
        elif source:
            try:
                data = json.loads(Path(source).read_text())
            except (OSError, UnicodeError, json.JSONDecodeError):
                try:
                    with Path(source).open("rb") as config_file:
                        data = tomllib.load(config_file)
                except (OSError, UnicodeError, tomllib.TOMLDecodeError):
                    data = {}
        else:
            data = {}
        if not isinstance(data, dict):
            return {}
        nested = data.get("orchestrator")
        return dict(nested) if isinstance(nested, dict) else dict(data)

    def _env(self, name: str, default=None):
        getter = getattr(self, "_get_env", None)
        if getter is not None:
            value = getter(name)
            return default if value is None else value
        return os.environ.get(name, default)

    def _source_root(self) -> Path:
        current = Path(__file__).resolve()
        for parent in [current.parent, *current.parents]:
            if (parent / "pyproject.toml").is_file() and (parent / "src/dagent").is_dir():
                return parent
        raise RuntimeError("cannot locate the orchestrator source tree for Harbor install")

    def _source_archive(self) -> Path:
        root = self._source_root()
        handle = tempfile.NamedTemporaryFile(prefix="dagent-", suffix=".tar.gz", delete=False)
        handle.close()
        archive = Path(handle.name)
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(root / "pyproject.toml", arcname="dagent/pyproject.toml")
            bundle.add(root / "src", arcname="dagent/src")
        return archive

    async def _ensure_system_dependencies(self, environment) -> None:
        """Ensure the small runtime toolset across Harbor versions.

        Harbor 0.20's BaseInstalledAgent does not provide the convenience
        package helper available in newer Harbor revisions. The task image
        already contains these tools; this probe is the normal path, with an
        apt fallback for compatible Debian images.
        """
        probe = await environment.exec(
            command=(
                "command -v git && command -v python3 && command -v tar && "
                "python3 -m pip --version"
            ),
            user="root",
        )
        if probe.return_code == 0:
            return
        await self.exec_as_root(
            environment,
            command=(
                "apt-get update && apt-get install -y --no-install-recommends "
                "git python3 python3-pip tar ca-certificates"
            ),
            timeout_sec=600,
        )

    async def install(self, environment) -> None:
        """Install system tools and this checkout's package in the agent image."""
        await self._ensure_system_dependencies(environment)
        archive = self._source_archive()
        try:
            await environment.upload_file(archive, "/tmp/dagent-source.tar.gz")
            # The runtime may use Claude Code's headless bypass-permissions
            # mode for local Ollama. Install the package as root so a
            # non-root Harbor agent user can import it without needing a
            # writable system site-packages directory.
            await self.exec_as_root(
                environment,
                command=(
                    "rm -rf /tmp/dagent-source && "
                    "mkdir -p /tmp/dagent-source && "
                    "tar -xzf /tmp/dagent-source.tar.gz "
                    "-C /tmp/dagent-source --strip-components=1 && "
                    "python3 -m pip install --no-cache-dir /tmp/dagent-source"
                ),
                timeout_sec=600,
            )
        finally:
            archive.unlink(missing_ok=True)

    @staticmethod
    def _remote_file_name(prefix: str, suffix: str) -> str:
        return f"/tmp/{prefix}-{os.getpid()}{suffix}"

    async def _upload_text(self, environment, text: str, remote_path: str) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt") as source:
            source.write(text)
            source.flush()
            await environment.upload_file(source.name, remote_path)
        # Harbor's upload helper creates files as root-owned 0600. The task
        # instruction and scheduler config contain no credentials, but the
        # configured non-root agent still needs to read them.
        await self.exec_as_root(
            environment,
            command=f"chmod 0644 {shlex.quote(remote_path)}",
        )

    @with_prompt_template
    async def run(self, instruction: str, environment, context) -> None:
        """Invoke the scheduler in the task container and publish its artifacts."""
        settings = self._settings()
        instruction_path = self._remote_file_name("orchestrator-instruction", ".md")
        config_path = self._remote_file_name("orchestrator-config", ".json")
        await self._upload_text(environment, instruction, instruction_path)
        await self._upload_text(environment, json.dumps(settings), config_path)

        # Harbor 0.20's Docker environment can put task env values into every
        # `docker compose exec -e` invocation. The launcher therefore mounts
        # the mode-600 auth file by path. Copy it once inside the container as
        # root so the configured Harbor agent user can read it; the command
        # line contains only paths, never the credential value.
        await self.exec_as_root(
            environment,
            command=(
                f"if test -f {shlex.quote(self._AUTH_MOUNT_PATH)}; then "
                f"install -m 0644 {shlex.quote(self._AUTH_MOUNT_PATH)} "
                f"{shlex.quote(self._AUTH_RUNTIME_PATH)}; fi"
            ),
        )

        repo_root = str(settings.get("repo_root") or self._env("ORCH_REPO_ROOT", "/app"))
        timeout = settings.get("agent_timeout_s") or self._env("ORCH_AGENT_TIMEOUT_S")
        command = (
            f"ORCH_AUTH_ENV_FILE={shlex.quote(self._AUTH_RUNTIME_PATH)} "
            "python3 -m dagent.harbor_runtime "
            f"--instruction-file {shlex.quote(instruction_path)} "
            f"--config-file {shlex.quote(config_path)}"
        )
        await self.exec_as_agent(
            environment,
            command=command,
            cwd=repo_root,
            timeout_sec=int(timeout) if timeout is not None else None,
        )

    def populate_context_post_run(self, context) -> None:
        """Expose final orchestrator state without exposing runtime secrets."""
        artifact_dir = self.logs_dir / "artifacts"
        result_path = artifact_dir / "result.json"
        metrics_path = artifact_dir / "metrics.json"
        result = {}
        metrics = {}
        if result_path.is_file():
            try:
                loaded = json.loads(result_path.read_text())
                if isinstance(loaded, dict):
                    result = loaded
            except (OSError, UnicodeError, json.JSONDecodeError):
                result = {"failure": {"type": "invalid_result_metadata"}}
        if metrics_path.is_file():
            try:
                loaded = json.loads(metrics_path.read_text())
                if isinstance(loaded, dict):
                    metrics = loaded
            except (OSError, UnicodeError, json.JSONDecodeError):
                metrics = {}

        metadata = dict(getattr(context, "metadata", None) or {})
        metadata["orchestrator"] = {
            "state": result.get("state"),
            "task_id": result.get("task_id"),
            "task_ids": result.get("task_ids", []),
            "task_states": result.get("task_states", {}),
            "base_sha": result.get("base_sha"),
            "candidate_sha": result.get("candidate_sha"),
            "policy": result.get("policy"),
            "metrics": metrics,
            "failure": result.get("failure"),
        }
        context.metadata = metadata
        if metrics.get("tokens_in") is not None:
            context.n_input_tokens = metrics["tokens_in"]
        if metrics.get("tokens_out") is not None:
            context.n_output_tokens = metrics["tokens_out"]
        if metrics.get("cost_usd") is not None:
            context.cost_usd = metrics["cost_usd"]


# Friendly import-path aliases for Harbor job definitions.
OrchestratorAgent = HarborOrchestratorAgent
HarborAgent = HarborOrchestratorAgent

__all__ = ["HarborOrchestratorAgent", "OrchestratorAgent", "HarborAgent"]
