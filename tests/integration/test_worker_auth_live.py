"""Opt-in live test for the direct CLI's subscription-auth boundary."""

import json
import os
import platform
import subprocess
import sys

import pytest

from orchestrator.worker.cli_worker import claude_command, encode_prompt, parse_cli_line
from orchestrator.worker.sandbox import (
    prepare_worker_sandbox,
    subscription_authentication_proven,
)
from tests.helpers import git, init_repo

pytestmark = pytest.mark.skipif(
    os.environ.get("ORCH_LIVE_SANDBOX_TESTS") != "1"
    or platform.system() != "Darwin",
    reason="live macOS worker authentication requires explicit opt-in",
)


def test_live_direct_cli_uses_host_auth_and_preserves_isolation(tmp_path):
    forbidden = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    )
    repo = init_repo(tmp_path)
    wt = tmp_path / "worker-slots" / "slot-0"
    wt.parent.mkdir()
    git("worktree", "add", "-q", str(wt), "HEAD", cwd=repo)
    hidden = tmp_path / "hidden-verifier"
    hidden.mkdir()
    (hidden / "known_hidden.py").write_text("SECRET_HIDDEN_CONTENT = 1\n")
    (wt / "public.txt").write_text("public content\n")

    sandbox = prepare_worker_sandbox("live-auth", wt)
    try:
        clean_host_env = {
            key: value for key, value in os.environ.items() if key not in forbidden
        }
        clean_host_env["ORCH_AUDIT_HIDDEN_SECRET"] = "SECRET_ENV_CONTENT"
        env = sandbox.environment(clean_host_env)
        claude = "claude"
        auth = subprocess.run(
            sandbox.command([claude, "auth", "status", "--json"]),
            cwd=wt, env=env, capture_output=True, text=True, timeout=30,
        )
        assert auth.returncode == 0
        assert subscription_authentication_proven(auth.stdout)

        # One bounded, non-tool turn proves the direct CLI can use the host
        # subscription login. The independent probe below exercises the same
        # Seatbelt with read/write attempts and reports no secret values.
        turn = subprocess.run(
            sandbox.command(claude_command("claude-sonnet-5") + ["--tools", ""]),
            input=encode_prompt("Reply with exactly: authenticated"),
            cwd=wt, env=env, capture_output=True, text=True, timeout=120,
        )
        assert turn.returncode == 0
        records = [parse_cli_line(line) for line in turn.stdout.splitlines() if line.strip()]
        result = next(record for record in records if record["type"] == "result")
        assert not result["is_error"]

        probe = f'''\
import json, os
from pathlib import Path

wt = Path({str(wt)!r})
hidden = Path({str(hidden)!r})
forbidden = {forbidden!r}
hidden_read = False
try:
    hidden_read = bool((hidden / "known_hidden.py").read_text())
except OSError:
    pass
(wt / "worker-proof.txt").write_text("worktree only\\n")
print(json.dumps({{
    "public": (wt / "public.txt").read_text(),
    "hidden_read": hidden_read,
    "secret_env_present": any(os.environ.get(name) for name in forbidden),
}}))
'''
        isolation = subprocess.run(
            sandbox.command([sys.executable, "-c", probe]),
            cwd=wt, env=env, capture_output=True, text=True, timeout=30,
        )
        assert isolation.returncode == 0
        report = json.loads(isolation.stdout)
        assert report == {
            "public": "public content\n",
            "hidden_read": False,
            "secret_env_present": False,
        }
        assert (wt / "worker-proof.txt").read_text() == "worktree only\n"
        assert "SECRET_HIDDEN_CONTENT" not in isolation.stdout
        assert "SECRET_ENV_CONTENT" not in isolation.stdout
    finally:
        sandbox.cleanup()
        git("worktree", "remove", "--force", str(wt), cwd=repo)
