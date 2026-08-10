"""Seatbelt worker-boundary tests that do not require a Claude API call."""

import asyncio
import json
import sys

import pytest

from orchestrator.worker import sandbox
from orchestrator.worker.sdk import spawn_sdk_worker
from tests.helpers import git, init_repo


def _git_worktree(repo, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-q", str(path), "HEAD", cwd=repo)
    return path


@pytest.mark.skipif(sandbox.platform.system() != "Darwin", reason="Seatbelt is macOS-only")
def test_profile_allows_public_worktree_but_not_its_parent_or_hidden_source(tmp_path):
    repo = init_repo(tmp_path)
    wt = _git_worktree(repo, tmp_path / "worker-slots" / "slot-0")
    worker = sandbox.prepare_worker_sandbox("task-1", wt)
    try:
        parent = str(wt.parent.resolve())
        hidden_source = str(tmp_path / "bench" / "hidden-tests")
        assert f'(allow file-read* (subpath "{wt.resolve()}"))' in worker.profile
        assert f'(allow file-write* (subpath "{wt.resolve()}"))' in worker.profile
        assert f'(allow file-read* (subpath "{parent}"))' not in worker.profile
        assert hidden_source not in worker.profile
        assert "Keychains" not in worker.profile
        assert "securityd" not in worker.profile
        assert "SecurityServer" not in worker.profile
        assert '(allow network' not in worker.profile
        assert str(worker.private_dir) in worker.profile
        assert sandbox.path_is_worker_visible(wt / "public.py", wt, allowlist=worker.allowlist)
        assert not sandbox.path_is_worker_visible(tmp_path / "bench" / "hidden.py", wt,
                                                  allowlist=worker.allowlist)
    finally:
        worker.cleanup()
        git("worktree", "remove", "--force", str(wt), cwd=repo)


def test_real_worker_fails_closed_on_unsupported_host(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.platform, "system", lambda: "Linux")
    with pytest.raises(sandbox.WorkerSandboxUnavailable, match="refusing to run unsandboxed"):
        asyncio.run(spawn_sdk_worker({"id": "task-1", "brief": "ignored"}, tmp_path))


def test_runtime_allowlist_ignores_caller_import_roots(tmp_path, monkeypatch):
    """A caller's PYTHONPATH must not turn the benchmark root into worker input."""
    project_root = tmp_path / "benchmark-root"
    project_root.mkdir()
    monkeypatch.setattr(sys, "path", [str(project_root), str(tmp_path / "other-import-root")])

    runtime_paths = sandbox._runtime_paths()

    assert project_root.resolve() not in runtime_paths
    assert (tmp_path / "other-import-root").resolve() not in runtime_paths


def test_runtime_allowlist_includes_python_extension_dependencies():
    extension_dir = sandbox.sysconfig.get_config_var("DESTSHARED")
    if not extension_dir:
        pytest.skip("Python does not expose a standard-library extension directory")
    extension_root = sandbox._resolve_existing(extension_dir)
    if not extension_root.is_dir():
        pytest.skip("Python standard-library extension directory is unavailable")

    runtime_paths = sandbox._runtime_paths()
    assert extension_root in runtime_paths
    ssl_extensions = [path for path in extension_root.glob("_ssl*.so") if path.is_file()]
    if ssl_extensions:
        assert sandbox._resolve_existing(ssl_extensions[0]) in runtime_paths


def test_worker_config_does_not_import_host_auth_state(tmp_path):
    """Worker config must not inherit the operator's Claude state."""
    host_home = tmp_path / "host-home"
    (host_home / ".claude").mkdir(parents=True)
    credentials = host_home / ".claude" / ".credentials.json"
    credentials.write_text('{"refreshToken":"secret"}')
    (host_home / ".claude" / "settings.json").write_text('{"permissions":{"allow":[]}}')
    (host_home / ".claude" / "history.jsonl").write_text("private history\n")
    (host_home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"accountUuid": "account-1", "organizationUuid": "org-1"},
        "projects": {"/private/project": {"lastTotalInputTokens": 123}},
        "secretCache": "must-not-copy",
    }))
    private = tmp_path / "private"
    private.mkdir()
    sandbox._prepare_claude_config(private)

    assert not (private / ".claude.json").exists()
    assert not (private / "claude-config" / ".credentials.json").exists()
    assert not (private / "claude-config" / "settings.json").exists()
    assert not (private / "claude-config" / "history.jsonl").exists()


def test_staging_never_copies_auth_credentials(tmp_path):
    host_home = tmp_path / "host-home"
    (host_home / ".claude").mkdir(parents=True)
    (host_home / ".claude" / ".credentials.json").write_text('{"refreshToken":"secret"}')

    private = tmp_path / "private"
    private.mkdir()
    sandbox._prepare_claude_config(private)

    assert not (private / "claude-config" / ".credentials.json").exists()


def test_worker_environment_uses_private_home(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    worker = sandbox.WorkerSandbox(
        worktree=tmp_path,
        private_dir=private,
        sandbox_exec=tmp_path / "sandbox-exec",
        allowlist=(tmp_path,),
        profile="",
    )

    env = worker.environment({"HOME": "/host-home", "PATH": "/bin",
                              "ANTHROPIC_API_KEY": "must-not-pass",
                              "ANTHROPIC_AUTH_TOKEN": "must-not-pass",
                              "CLAUDE_CODE_OAUTH_TOKEN": "must-not-pass",
                              "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "must-not-pass"})

    assert env["HOME"] == str(private)
    assert env["PATH"] == "/bin"
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in env


def test_subscription_authentication_proof_requires_first_party_oauth():
    assert sandbox.subscription_authentication_proven(json.dumps({
        "loggedIn": True, "authMethod": "oauth", "apiProvider": "firstParty",
    }))
    assert sandbox.subscription_authentication_proven(json.dumps({
        "loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
    }))
    assert not sandbox.subscription_authentication_proven(json.dumps({
        "loggedIn": True, "authMethod": "apiKey", "apiProvider": "firstParty",
    }))
    assert not sandbox.subscription_authentication_proven(
        '{"loggedIn":true,"authMethod":"oauth","apiProvider":"other"}'
    )
    assert not sandbox.subscription_authentication_proven("not JSON")
