"""Live subscription-auth smoke test for the production worker boundary."""

import os
import platform

import pytest

from orchestrator.worker.sdk import run_worker_auth_smoke_test

pytestmark = pytest.mark.skipif(
    os.environ.get("ORCH_LIVE_SANDBOX_TESTS") != "1"
    or platform.system() != "Darwin",
    reason="live macOS worker authentication requires explicit opt-in",
)


def test_live_worker_uses_host_claude_auth_without_api_credentials():
    forbidden = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    )
    assert not any(os.environ.get(name) for name in forbidden)

    result = run_worker_auth_smoke_test("claude-sonnet-5")

    assert result.returncode == 0
    assert result.result_success
    assert result.model_response
    assert result.session_id
    assert "result" in result.event_types
    assert "execution_started" in result.event_types
    assert "startup_failed" not in result.event_types
    assert result.startup_failure_category is None
