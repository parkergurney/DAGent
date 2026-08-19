"""Unit tests for sdk_worker.py's pure logic (design.md section 8, M3) --
sentinel parsing and the worktree-escape guard. No SDK client, no network, no
API cost: this is the deterministic part of the real-worker integration, kept
testable the same way FakeWorker keeps the scheduler testable.
"""
import asyncio
import io
import json

from claude_agent_sdk import AssistantMessage, ClaudeSDKError, ResultMessage, TextBlock

from orchestrator.worker import sdk_worker
from orchestrator.worker.sdk_worker import (
    _agent_options, _audit_tool, _make_pre_tool_use, _parse_terminal,
    _path_escapes_worktree,
    _prompt_with_protocol,
)


def test_tool_audit_redacts_credentials_and_flags_network_commands(monkeypatch, tmp_path):
    audit = tmp_path / "tool_audit.jsonl"
    monkeypatch.setenv("ORCH_TOOL_AUDIT_PATH", str(audit))
    _audit_tool(
        phase="pre",
        input_data={
            "tool_name": "Bash",
            "tool_input": {"command": "curl https://example.test -H 'Authorization: Bearer secret'"},
        },
        task_id="task-1",
        decision="allowed",
    )
    record = json.loads(audit.read_text())
    assert record["likely_network_or_history_attempt"] is True
    assert "https://example.test" in record["target"]
    assert "Bearer [REDACTED]" in record["target"]
    assert "secret" not in record["target"]


def test_pre_tool_hook_denies_package_install_and_records_reason(monkeypatch, tmp_path):
    audit = tmp_path / "tool_audit.jsonl"
    monkeypatch.setenv("ORCH_TOOL_AUDIT_PATH", str(audit))
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    hook = _make_pre_tool_use(worktree, "task-1")
    decision = asyncio.run(hook({
        "tool_name": "Bash",
        "tool_input": {"command": "cd repo && pip install fqdn"},
    }, "tool-1", None))
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    record = json.loads(audit.read_text())
    assert record["decision"] == "denied"
    assert "preinstalled" in record["reason"]


def test_pre_tool_hook_allows_local_git_commit(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    hook = _make_pre_tool_use(worktree, "task-1")
    decision = asyncio.run(hook({
        "tool_name": "Bash",
        "tool_input": {"command": "git add arrow.py && git commit -m 'fix'"},
    }, "tool-1", None))
    assert decision == {}


def test_ollama_uses_compact_headless_tool_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCH_BACKEND", "ollama")
    options = _agent_options(tmp_path, "qwen3-coder:30b")
    assert options.tools == ["Bash", "Read", "Edit", "Write", "Glob", "Grep"]
    assert options.permission_mode == "bypassPermissions"
    assert options.thinking == {"type": "disabled"}


def test_claude_uses_headless_permission_mode(monkeypatch, tmp_path):
    monkeypatch.delenv("ORCH_BACKEND", raising=False)
    options = _agent_options(tmp_path, "claude-sonnet-5")
    assert options.tools is None
    assert options.permission_mode == "bypassPermissions"


def test_prompt_appends_protocol_to_brief():
    prompt = _prompt_with_protocol("fix the bug in foo.py")
    assert prompt.startswith("fix the bug in foo.py")
    assert "DONE_CLAIM:" in prompt
    assert "ASK:" in prompt


def test_parse_terminal_done_claim():
    kind, extra = _parse_terminal("I fixed it.\nDONE_CLAIM: added a null check\n")
    assert kind == "done_claimed"
    assert extra == {"result": "added a null check"}


def test_parse_terminal_ask():
    kind, extra = _parse_terminal("Not sure how to proceed.\nASK: which logging library?")
    assert kind == "asked"
    assert extra == {"question": "which logging library?"}


def test_parse_terminal_no_change():
    kind, extra = _parse_terminal("NO_CHANGE: the requested setting is already present")
    assert kind == "no_change"
    assert extra == {"result": "the requested setting is already present"}


def test_parse_terminal_no_sentinel_is_unclaimed():
    kind, extra = _parse_terminal("I think I'm done but forgot to say so.")
    assert kind is None
    assert extra == {}


def test_parse_terminal_handles_none():
    assert _parse_terminal(None) == (None, {})


def test_path_inside_worktree_is_allowed(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    assert _path_escapes_worktree(str(wt / "output.txt"), wt) is False
    assert _path_escapes_worktree("output.txt", wt) is False  # relative, resolved against wt


def test_path_outside_worktree_is_denied(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    assert _path_escapes_worktree(str(tmp_path / "other" / "x.txt"), wt) is True
    assert _path_escapes_worktree("/etc/passwd", wt) is True


def test_path_traversal_out_of_worktree_is_denied(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    assert _path_escapes_worktree("../escape.txt", wt) is True


class _FakeClient:
    """Stands in for ClaudeSDKClient: turn 1 asks a question, turn 2 (after
    whatever run() queries back in) claims done. Lets the stdin-nudge path
    (design.md section 6's live intervention) be tested without a live SDK
    session or API cost."""

    def __init__(self, options=None):
        self.queries = []

    async def connect(self, prompt=None):
        pass

    async def disconnect(self):
        pass

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)

    async def receive_response(self):
        if len(self.queries) == 1:
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                is_error=False, num_turns=1, session_id="s1",
                                total_cost_usd=0.01, result="ASK: which port?")
        else:
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1,
                                is_error=False, num_turns=2, session_id="s1",
                                total_cost_usd=0.02, result="DONE_CLAIM: used port 8080")


def test_run_resumes_the_same_session_after_a_stdin_reply(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdk_worker.sys, "stdin", io.StringIO("use port 8080\n"))
    monkeypatch.setenv("ORCH_LIVE_DIAGNOSTICS_PATH", str(tmp_path / "live.jsonl"))
    fake = _FakeClient()
    monkeypatch.setattr(sdk_worker, "ClaudeSDKClient", lambda options=None: fake)

    asyncio.run(sdk_worker.run(tmp_path, "set up a server", None))

    # one query for the brief, a second carrying the stdin reply back in --
    # not a fresh session, the same conversation resumed.
    assert fake.queries == [_prompt_with_protocol("set up a server"), "use port 8080"]

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["type"] for e in events] == [
        "result", "execution_started", "asked",
        "result", "done_claimed",
    ]
    assert events[-1]["payload"]["result"] == "used port 8080"
    diagnostics = [
        json.loads(line)["event"]
        for line in (tmp_path / "live.jsonl").read_text().splitlines()
    ]
    assert {
        "sdk.worker_started", "sdk.connect_started", "sdk.connect_succeeded",
        "sdk.turn_started", "sdk.prompt_submitted", "sdk.result_received",
        "sdk.turn_stream_ended", "sdk.disconnect_started", "sdk.worker_finished",
    } <= set(diagnostics)


def test_run_exits_without_resuming_when_stdin_closes(tmp_path, capsys, monkeypatch):
    """No reply ever lands (escalate/abandon tore the process down and closed
    its stdin) -- run() must not hang or fabricate a second query."""
    monkeypatch.setattr(sdk_worker.sys, "stdin", io.StringIO(""))  # EOF immediately
    fake = _FakeClient()
    monkeypatch.setattr(sdk_worker, "ClaudeSDKClient", lambda options=None: fake)

    asyncio.run(sdk_worker.run(tmp_path, "set up a server", None))

    assert fake.queries == [_prompt_with_protocol("set up a server")]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["type"] for e in events] == ["result", "execution_started", "asked"]


class _ConnectFailsClient:
    """Stands in for a ClaudeSDKClient whose connect() raises."""

    def __init__(self, options=None):
        pass

    async def connect(self, prompt=None):
        raise ClaudeSDKError("SDK initialization failed")


def test_run_exits_loudly_when_sdk_fails_to_start(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(sdk_worker, "ClaudeSDKClient", lambda options=None: _ConnectFailsClient())

    assert asyncio.run(sdk_worker.run(tmp_path, "set up a server", None)) == 1

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["type"] for e in events] == ["startup_failed"]
    assert events[0]["payload"]["category"] == "sdk_initialization_failure"


def test_run_redacts_unexpected_sdk_startup_exception(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        sdk_worker, "_agent_options",
        lambda worktree, model, **kwargs: (_ for _ in ()).throw(RuntimeError("bad option")),
    )

    assert asyncio.run(sdk_worker.run(tmp_path, "set up a server", None)) == 1

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events == [{
        "type": "startup_failed",
        "payload": {"category": "sdk_initialization_failure", "error": "bad option"},
    }]


class _HangingConnectClient:
    def __init__(self, options=None):
        pass

    async def connect(self, prompt=None):
        await asyncio.sleep(1)


def test_run_bounds_sdk_connect(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ORCH_SDK_TIMEOUT_S", "0.1")
    monkeypatch.setattr(sdk_worker, "ClaudeSDKClient", _HangingConnectClient)

    assert asyncio.run(sdk_worker.run(tmp_path, "start", None)) == 1

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events == [{
        "type": "startup_failed",
        "payload": {
            "category": "sdk_timeout",
            "phase": "connect",
            "timeout_s": 0.1,
            "reason": "Claude SDK connection exceeded its bounded timeout",
        },
    }]


class _HangingTurnClient:
    def __init__(self, options=None):
        pass

    async def connect(self, prompt=None):
        pass

    async def query(self, prompt, session_id="default"):
        await asyncio.sleep(1)

    async def disconnect(self):
        pass


def test_run_bounds_sdk_turn(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ORCH_SDK_TIMEOUT_S", "0.1")
    monkeypatch.setattr(sdk_worker, "ClaudeSDKClient", _HangingTurnClient)

    assert asyncio.run(sdk_worker.run(tmp_path, "start", None)) == 1

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events == [{
        "type": "sdk_timeout",
        "payload": {
            "category": "sdk_timeout",
            "phase": "turn",
            "timeout_s": 0.1,
            "reason": "Claude SDK turn exceeded its bounded timeout",
        },
    }]


class _ResultClient:
    def __init__(self, result, assistant_text=None, options=None):
        self.result = result
        self.assistant_text = assistant_text

    async def connect(self, prompt=None):
        pass

    async def disconnect(self):
        pass

    async def query(self, prompt, session_id="default"):
        pass

    async def receive_response(self):
        if self.assistant_text is not None:
            yield AssistantMessage(
                content=[TextBlock(self.assistant_text)], model="claude-sonnet-5",
                usage={"input_tokens": 3, "output_tokens": 4}, session_id="s1",
            )
        yield self.result


def test_result_message_persists_safe_aggregate_usage_and_starts_execution(
    tmp_path, capsys, monkeypatch,
):
    result = ResultMessage(
        subtype="success", duration_ms=10, duration_api_ms=8, is_error=False,
        num_turns=1, session_id="s1", total_cost_usd=0.12,
        usage={"input_tokens": 30, "output_tokens": 40},
        model_usage={"claude-sonnet-5": {"inputTokens": 30, "outputTokens": 40}},
        result="short model response",
    )
    monkeypatch.setattr(
        sdk_worker, "ClaudeSDKClient", lambda options=None: _ResultClient(result, "hello")
    )

    assert asyncio.run(sdk_worker.run(tmp_path, "reply", None)) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    result_event = next(event for event in events if event["type"] == "result")
    assert result_event["payload"]["usage"] == {"input_tokens": 30, "output_tokens": 40}
    assert result_event["payload"]["model_usage"]["claude-sonnet-5"]["inputTokens"] == 30
    assert result_event["payload"]["tokens_in"] == 30
    assert result_event["payload"]["tokens_out"] == 40
    assert result_event["payload"]["cost_usd"] == 0.12
    assert [event["type"] for event in events].count("execution_started") == 1
    assert events[-1]["type"] == "unclaimed"
    assert events[-1]["payload"]["reason"] == "result_missing_terminal_marker"
    # Per-message usage remains diagnostic, while the result aggregate is the
    # only accounting record with canonical token/cost fields.
    message_event = next(event for event in events if event["type"] == "messaged")
    assert message_event["payload"]["tokens_in"] == 3
    assert message_event["payload"]["tokens_out"] == 4


def test_authentication_result_fails_even_with_success_exit_code(tmp_path, capsys, monkeypatch):
    result = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=0, is_error=False,
        num_turns=0, session_id="auth-session", result="Not logged in · Please run /login",
    )
    monkeypatch.setattr(
        sdk_worker, "ClaudeSDKClient",
        lambda options=None: _ResultClient(result, "Not logged in · Please run /login"),
    )

    assert asyncio.run(sdk_worker.run(tmp_path, "reply", None)) == 1
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event["type"] for event in events] == ["messaged", "result", "startup_failed"]
    assert events[-1]["payload"]["category"] == "authentication_failure"
    assert "execution_started" not in [event["type"] for event in events]


def test_http_401_result_is_classified_as_authentication_failure():
    assert sdk_worker._startup_failure_category(
        {"api_error_status": 401, "session_id": "s1"}, []
    ) == ("authentication_failure", "Claude backend rejected authentication (HTTP 401)")
