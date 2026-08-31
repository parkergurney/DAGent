"""Pure direct-Claude-CLI adapter tests using recorded stream-json fixtures."""

import asyncio
import io
import json
from pathlib import Path

import pytest

from dagent.worker import cli_worker
from dagent.worker.cli_worker import (
    CliRecordError,
    aggregate_usage,
    assistant_text,
    claude_command,
    encode_prompt,
    parse_cli_line,
    parse_terminal,
    prompt_with_protocol,
    result_snapshot,
    startup_failure,
    tool_uses,
    user_envelope,
)


FIXTURES = Path(__file__).parent / "fixtures" / "claude_cli"


def _fixture_records(name):
    return [
        parse_cli_line(line)
        for line in (FIXTURES / name).read_text().splitlines()
    ]


def test_claude_command_is_direct_and_uses_requested_stream_flags():
    command = claude_command("claude-sonnet-5")
    assert command == [
        "claude", "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--replay-user-messages",
        "--no-session-persistence",
        "--model", "claude-sonnet-5",
    ]
    assert "--bare" not in command
    assert "ANTHROPIC_API_KEY" not in command


def test_every_prompt_uses_the_documented_user_envelope():
    envelope = user_envelope("please continue")
    assert envelope == {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "please continue"}],
        },
    }
    assert json.loads(encode_prompt("please continue")) == envelope
    assert "DONE_CLAIM:" in prompt_with_protocol("brief")


def test_recorded_success_fixture_maps_text_tools_and_terminal_result():
    records = _fixture_records("success.jsonl")
    assistant = next(record for record in records if record["type"] == "assistant")
    stream = next(record for record in records if record["type"] == "stream_event")
    result = next(record for record in records if record["type"] == "result")

    assert assistant_text(assistant) == "I inspected the worktree."
    assert assistant_text(stream) == "The change is ready."
    assert tool_uses(assistant) == [{"tool": "Read", "target": "README.md"}]
    assert parse_terminal(result["result"]) == (
        "done_claimed", {"result": "implemented the change"}
    )
    assert result_snapshot(result)["tokens_in"] == 123
    assert result_snapshot(result)["tokens_out"] == 45
    assert result_snapshot(result)["cost_usd"] == 0.0123


def test_recorded_ask_fixture_maps_question_and_aggregate_usage():
    records = _fixture_records("ask.jsonl")
    result = next(record for record in records if record["type"] == "result")

    assert parse_terminal(result["result"]) == (
        "asked", {"question": "which port should I use?"}
    )
    assert aggregate_usage(result) == (40, 12, 0.004)


def test_model_usage_is_an_aggregate_fallback():
    assert aggregate_usage({
        "modelUsage": {
            "sonnet": {"inputTokens": 10, "outputTokens": 4},
            "haiku": {"inputTokens": 3, "outputTokens": 2},
        },
        "total_cost_usd": 0.02,
    }) == (13, 6, 0.02)


def test_malformed_cli_output_is_rejected():
    with pytest.raises(ValueError, match="malformed"):
        parse_cli_line('{"type":"assistant"')
    with pytest.raises(ValueError, match="malformed"):
        parse_cli_line(b"\xff\xfe")
    with pytest.raises(ValueError, match="object with a string type"):
        parse_cli_line("[]")


def test_unknown_and_malformed_record_shapes_are_typed():
    with pytest.raises(CliRecordError) as unknown:
        parse_cli_line('{"type":"future_record"}')
    assert unknown.value.category == "unknown_record_type"

    with pytest.raises(CliRecordError) as malformed:
        parse_cli_line('{"type":"assistant","session_id":"s1"}')
    assert malformed.value.category == "malformed_output"


def test_authentication_result_is_a_startup_failure():
    failure = startup_failure({
        "type": "result",
        "is_error": False,
        "result": "Not logged in. Please run /login.",
    })
    assert failure == (
        "authentication_failure", "Claude Code reported an authentication failure"
    )


def test_backend_result_has_a_distinct_failure_classification():
    failure = startup_failure({
        "type": "result",
        "subtype": "error",
        "is_error": True,
        "result": "API Error: overloaded",
        "session_id": "backend-session",
    })
    assert failure == (
        "backend_failure", "Claude Code backend returned an error result"
    )


def test_run_loop_retains_authentication_failure_classification(monkeypatch, capsys):
    code, process, _, events = _run_fake_cli(monkeypatch, capsys, [
        {"type": "system", "subtype": "init", "session_id": "auth-session"},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": "Not logged in. Please run /login.", "session_id": "auth-session"},
    ])

    assert code == 1
    assert events[-1]["type"] == "startup_failed"
    assert events[-1]["payload"]["category"] == "authentication_failure"
    assert events[-1]["payload"]["session_id"] == "auth-session"
    assert process.killed is True


def test_run_loop_rejects_non_list_result_errors_as_malformed_output(monkeypatch, capsys):
    code, process, _, events = _run_fake_cli(monkeypatch, capsys, [
        {"type": "system", "subtype": "init", "session_id": "errors-session"},
        {"type": "result", "subtype": "success", "is_error": False,
         "result": "not completed", "errors": 3, "session_id": "errors-session"},
    ])

    assert code == 1
    assert events[-1]["type"] == "startup_failed"
    assert events[-1]["payload"]["category"] == "malformed_output"
    assert "errors" in events[-1]["payload"]["error"]
    assert process.killed is True


class _FakeStdout:
    def __init__(self, lines):
        self.lines = iter(lines)

    async def readline(self):
        return next(self.lines, b"")


class _FakeStdin:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class _FakeProcess:
    def __init__(self, lines, returncode=0):
        self.stdout = _FakeStdout(lines)
        self.stdin = _FakeStdin()
        self.returncode = returncode
        self.killed = False
        self.waited = False

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True
        return self.returncode


def _run_fake_cli(monkeypatch, capsys, records, *, returncode=0, stdin=""):
    process = _FakeProcess(
        [(json.dumps(record) + "\n").encode() for record in records],
        returncode=returncode,
    )
    calls = []

    async def fake_create(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(cli_worker.asyncio, "create_subprocess_exec", fake_create)
    monkeypatch.setattr(cli_worker.sys, "stdin", io.StringIO(stdin))
    code = asyncio.run(cli_worker.run(Path("/tmp/worktree"), "brief", "sonnet"))
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    return code, process, calls, events


def test_run_loop_translates_success_and_propagates_session_id(monkeypatch, capsys):
    records = _fixture_records("success.jsonl")
    code, process, calls, events = _run_fake_cli(monkeypatch, capsys, records)

    assert code == 0
    assert process.waited is True
    assert process.killed is False
    assert calls[0][0][0] == "claude"
    assert [event["type"] for event in events] == [
        "execution_started", "messaged", "tool_used", "messaged",
        "result", "done_claimed",
    ]
    assert all(event["payload"]["session_id"] == "fixture-session-1" for event in events)
    assert json.loads(process.stdin.writes[0]) == user_envelope(prompt_with_protocol("brief"))


def test_run_loop_wraps_a_raw_nudge_and_keeps_the_same_session(monkeypatch, capsys):
    records = _fixture_records("ask.jsonl") + [{
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "DONE_CLAIM: continued",
        "session_id": "fixture-session-2",
        "usage": {"input_tokens": 50, "output_tokens": 8},
        "total_cost_usd": 0.005,
    }]
    code, process, _, events = _run_fake_cli(
        monkeypatch, capsys, records, stdin="use port 8080\n"
    )

    assert code == 0
    assert [json.loads(data) for data in process.stdin.writes] == [
        user_envelope(prompt_with_protocol("brief")),
        user_envelope("use port 8080"),
    ]
    assert [event["type"] for event in events] == [
        "execution_started", "messaged", "result", "asked", "result", "done_claimed",
    ]
    assert all(event["payload"]["session_id"] == "fixture-session-2" for event in events)


@pytest.mark.parametrize(
    ("records", "returncode", "category"),
    [
        ([{"type": "future_record"}], 0, "unknown_record_type"),
        ([{"type": "system", "subtype": "init", "session_id": "s1"},
          {"type": "assistant", "session_id": "s1"}], 0, "malformed_output"),
        ([{"type": "system", "subtype": "init", "session_id": "s1"},
          {"type": "result", "subtype": "error", "is_error": True,
           "result": "API Error: overloaded", "session_id": "s1"}],
         0, "backend_failure"),
        ([], 17, "cli_process_failure"),
    ],
)
def test_run_loop_surfaces_unknown_malformed_and_process_failures(
    monkeypatch, capsys, records, returncode, category,
):
    code, process, _, events = _run_fake_cli(
        monkeypatch, capsys, records, returncode=returncode
    )

    assert code == 1
    failure = events[-1]
    assert failure["type"] == "startup_failed"
    assert failure["payload"]["category"] == category
    assert process.waited is True
    assert process.killed is (category in {
        "unknown_record_type", "malformed_output", "backend_failure",
    })
