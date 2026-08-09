"""Bridge the worker wire protocol to the installed Claude Code CLI.

The scheduler owns a deliberately small protocol: it writes raw nudge text to
stdin and reads JSONL worker events from stdout.  Claude Code's stream-json
mode has a different protocol, so this module is the adapter between the two.
It intentionally does not import the Agent SDK; the only model process it
starts is the installed ``claude`` executable.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path


CLAUDE_EXECUTABLE = "claude"
_KNOWN_RECORD_TYPES = frozenset({
    "system", "user", "assistant", "stream_event", "result",
    "rate_limit_event", "prompt_suggestion",
})
_KNOWN_STREAM_EVENT_TYPES = frozenset({
    "message_start", "content_block_start", "content_block_delta",
    "content_block_stop", "message_delta", "message_stop", "ping",
})
_PROTOCOL = """
Commit your changes in the worktree before claiming done. Uncommitted work
will fail verification.

When you are completely finished, end your final message with exactly one line:
DONE_CLAIM: <one-line summary of what you did>

If you are blocked on a decision only a human can make, end your message with
exactly one line instead, and stop there:
ASK: <your question>
"""
_AUTH_FAILURE_MARKERS = (
    "not logged in",
    "please run /login",
    "authentication required",
    "authentication failed",
    "unauthorized",
    "invalid oauth",
)
_BACKEND_FAILURE_MARKERS = (
    "api error", "backend", "overloaded", "rate limit", "service unavailable",
    "internal server error", "connection refused", "connection reset",
)
_SECRET_PATTERNS = (
    (re.compile(r"(?i)\bsk-ant-[a-z0-9_-]+"), "[REDACTED_ANTHROPIC_CREDENTIAL]"),
    (re.compile(r"(?i)\bbearer\s+[a-z0-9._-]+"), "Bearer [REDACTED]"),
)


def _redact_text(value: object, *, limit: int = 2000) -> str:
    text = str(value or "")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:limit]


def prompt_with_protocol(brief: str) -> str:
    """Append the worker's done/ask protocol to a task brief."""
    return f"{brief.rstrip()}\n\n{_PROTOCOL.strip()}\n"


def user_envelope(text: str) -> dict:
    """Build the JSONL input envelope required by Claude Code stream mode."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }


def encode_prompt(text: str) -> bytes:
    """Encode one prompt as one newline-delimited Claude Code input record."""
    return (json.dumps(user_envelope(text), separators=(",", ":")) + "\n").encode()


def claude_command(model: str | None = None, *, executable: str = CLAUDE_EXECUTABLE) -> list[str]:
    """Return the direct Claude Code command, without credentials or ``--bare``."""
    args = [
        executable,
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--replay-user-messages",
        "--no-session-persistence",
    ]
    if model:
        args.extend(["--model", model])
    return args


class CliRecordError(ValueError):
    """A Claude stream-json record is unknown or violates its known shape."""

    def __init__(self, message: str, *, category: str = "malformed_output"):
        self.category = category
        super().__init__(message)


def _record_error(message: str) -> CliRecordError:
    return CliRecordError(message)


def _require_string(record: dict, key: str, *, record_type: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value:
        raise _record_error(f"{record_type} record requires non-empty string {key!r}")
    return value


def _require_session_id(record: dict, *, record_type: str) -> str:
    return _require_string(record, "session_id", record_type=record_type)


def _validate_content(content, *, record_type: str) -> None:
    if not isinstance(content, list):
        raise _record_error(f"{record_type} message.content must be a list")
    for index, block in enumerate(content):
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise _record_error(f"{record_type} content block {index} must have a type")
        block_type = block["type"]
        if block_type == "text" and not isinstance(block.get("text"), str):
            raise _record_error(f"{record_type} text block {index} requires string text")
        if block_type in {"tool_use", "server_tool_use"}:
            if not isinstance(block.get("name"), str) or not isinstance(block.get("input"), dict):
                raise _record_error(
                    f"{record_type} {block_type} block {index} requires name and object input"
                )


def _validate_message(record: dict, *, role: str, record_type: str) -> None:
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role") != role:
        raise _record_error(f"{record_type} record requires message.role={role!r}")
    _validate_content(message.get("content"), record_type=record_type)


def validate_cli_record(record: dict) -> dict:
    """Validate one known Claude Code stream-json record and return it.

    Validation is deliberately strict at the adapter boundary. A new CLI
    record type should fail visibly as infrastructure drift instead of being
    silently ignored and leaving the scheduler with an ambiguous worker exit.
    """
    if not isinstance(record, dict) or not isinstance(record.get("type"), str):
        raise _record_error("Claude Code output record must be an object with a string type")
    record_type = record["type"]
    if record_type not in _KNOWN_RECORD_TYPES:
        raise CliRecordError(
            f"unknown Claude Code output record type {record_type!r}",
            category="unknown_record_type",
        )

    if record_type == "system":
        _require_string(record, "subtype", record_type=record_type)
        _require_session_id(record, record_type=record_type)
    elif record_type == "user":
        _validate_message(record, role="user", record_type=record_type)
    elif record_type == "assistant":
        _require_session_id(record, record_type=record_type)
        _validate_message(record, role="assistant", record_type=record_type)
    elif record_type == "stream_event":
        _require_session_id(record, record_type=record_type)
        event = record.get("event")
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise _record_error("stream_event record requires event.type")
        if event["type"] not in _KNOWN_STREAM_EVENT_TYPES:
            raise CliRecordError(
                f"unknown Claude Code stream event type {event['type']!r}",
                category="unknown_record_type",
            )
        if event["type"] == "content_block_delta":
            delta = event.get("delta")
            if not isinstance(delta, dict) or not isinstance(delta.get("type"), str):
                raise _record_error("content_block_delta requires delta.type")
            if delta["type"] == "text_delta" and not isinstance(delta.get("text"), str):
                raise _record_error("text_delta requires string text")
    elif record_type == "result":
        _require_string(record, "subtype", record_type=record_type)
        _require_session_id(record, record_type=record_type)
        if not isinstance(record.get("is_error"), bool):
            raise _record_error("result record requires boolean is_error")
        if "result" not in record or record["result"] is not None and not isinstance(record["result"], str):
            raise _record_error("result record requires string or null result")
        if "errors" in record and not isinstance(record["errors"], list):
            raise _record_error("result record field 'errors' must be a list")
        for key in ("usage", "modelUsage", "model_usage"):
            if key in record and not isinstance(record[key], dict):
                raise _record_error(f"result record field {key!r} must be an object")
    elif record_type == "rate_limit_event":
        _require_session_id(record, record_type=record_type)
        if not isinstance(record.get("rate_limit_info"), dict):
            raise _record_error("rate_limit_event requires object rate_limit_info")
    elif record_type == "prompt_suggestion":
        _require_session_id(record, record_type=record_type)
        _require_string(record, "suggestion", record_type=record_type)
    return record


def parse_cli_line(line: str | bytes) -> dict:
    """Parse and strictly validate one Claude Code JSONL output line."""
    try:
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        if not isinstance(line, str) or not line.strip():
            raise _record_error("empty Claude Code output line")
        record = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _record_error("malformed Claude Code JSONL output") from exc
    return validate_cli_record(record)


def _text_blocks(content) -> list[str]:
    if not isinstance(content, list):
        return []
    return [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
        and isinstance(block.get("text", ""), str)
    ]


def assistant_text(record: dict) -> str:
    """Extract assistant text from a full or partial stream-json record."""
    if record.get("type") == "assistant":
        message = record.get("message") or {}
        return "".join(_text_blocks(message.get("content")))
    if record.get("type") == "stream_event":
        event = record.get("event") or {}
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
            return delta["text"]
    return ""


def tool_uses(record: dict) -> list[dict]:
    """Extract tool-use blocks from an assistant record for worker.tool_used."""
    if record.get("type") != "assistant":
        return []
    message = record.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return []
    uses = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        tool_input = block.get("input") or {}
        target = ""
        if isinstance(tool_input, dict):
            target = tool_input.get("file_path") or tool_input.get("command") or ""
        uses.append({"tool": block.get("name"), "target": target})
    return uses


def parse_terminal(result_text: str | None) -> tuple[str | None, dict]:
    """Parse the worker sentinel from Claude Code's result text."""
    if not result_text:
        return None, {}
    for line in result_text.splitlines():
        line = line.strip()
        if line.startswith("DONE_CLAIM:"):
            return "done_claimed", {"result": line[len("DONE_CLAIM:"):].strip()}
        if line.startswith("ASK:"):
            return "asked", {"question": line[len("ASK:"):].strip()}
    return None, {}


def _usage_value(usage: dict, *names: str) -> int | None:
    for name in names:
        value = usage.get(name)
        if isinstance(value, (int, float)):
            return int(value)
    return None


def aggregate_usage(record: dict) -> tuple[int | None, int | None, float | None]:
    """Return aggregate input tokens, output tokens, and cost from a result."""
    usage = record.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    tokens_in = _usage_value(usage, "input_tokens", "inputTokens")
    tokens_out = _usage_value(usage, "output_tokens", "outputTokens")

    # Older/newer CLI versions have exposed aggregate usage under modelUsage
    # instead of usage.  Sum models only when the top-level values are absent.
    model_usage = record.get("modelUsage") or record.get("model_usage") or {}
    if isinstance(model_usage, dict):
        values = [value for value in model_usage.values() if isinstance(value, dict)]
        if tokens_in is None:
            numbers = [_usage_value(value, "inputTokens", "input_tokens") for value in values]
            tokens_in = sum(number for number in numbers if number is not None) or None
        if tokens_out is None:
            numbers = [_usage_value(value, "outputTokens", "output_tokens") for value in values]
            tokens_out = sum(number for number in numbers if number is not None) or None

    cost = record.get("total_cost_usd", record.get("cost_usd"))
    if not isinstance(cost, (int, float)):
        cost = None
    return tokens_in, tokens_out, cost


def result_snapshot(record: dict) -> dict:
    """Keep the bounded result fields used by scheduler accounting/diagnostics."""
    tokens_in, tokens_out, cost = aggregate_usage(record)
    usage = record.get("usage") if isinstance(record.get("usage"), dict) else {}
    model_usage = record.get("modelUsage") or record.get("model_usage") or {}
    return {
        "subtype": _redact_text(record.get("subtype"), limit=100),
        "is_error": bool(record.get("is_error")),
        "errors": record.get("errors") or [],
        "result": _redact_text(record.get("result")),
        "session_id": _redact_text(record.get("session_id"), limit=200),
        "usage": usage,
        "model_usage": model_usage,
        "total_cost_usd": cost,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost,
    }


def startup_failure(record: dict, transcript: str = "") -> tuple[str, str] | None:
    """Classify a Claude result failure without conflating backend/auth errors."""
    snapshot = result_snapshot(record)
    text = "\n".join([
        transcript,
        str(snapshot.get("result") or ""),
        *(str(error) for error in snapshot.get("errors") or []),
    ]).lower()
    if any(marker in text for marker in _AUTH_FAILURE_MARKERS):
        return "authentication_failure", "Claude Code reported an authentication failure"
    if (
        snapshot["is_error"]
        or record.get("api_error_status")
        or record.get("subtype") in {"error", "api_error"}
        or any(marker in text for marker in _BACKEND_FAILURE_MARKERS)
    ):
        return "backend_failure", "Claude Code backend returned an error result"
    return None


def emit(type_: str, **payload) -> None:
    print(json.dumps({"type": type_, "payload": payload}), flush=True)


async def _send_prompt(process: asyncio.subprocess.Process, text: str) -> None:
    process.stdin.write(encode_prompt(text))
    await process.stdin.drain()


def _emit_session_event(type_: str, session_id: str | None, **payload) -> None:
    if session_id:
        payload["session_id"] = session_id
    emit(type_, **payload)


async def run(worktree: Path, brief: str, model: str | None) -> int:
    """Run one long-lived Claude Code stream and translate its events."""
    try:
        process = await asyncio.create_subprocess_exec(
            *claude_command(model),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(worktree),
        )
    except OSError as exc:
        emit("startup_failed", category="cli_startup_failure", error=_redact_text(exc))
        return 1

    execution_started = False
    terminal_kind = None
    session_id: str | None = None
    transcript: list[str] = []
    try:
        await _send_prompt(process, prompt_with_protocol(brief))
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            try:
                record = parse_cli_line(line)
            except CliRecordError as exc:
                _emit_session_event("startup_failed", session_id,
                                     category=exc.category, error=str(exc))
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
                return 1

            record_session_id = record.get("session_id")
            if record_session_id:
                if session_id and record_session_id != session_id:
                    _emit_session_event(
                        "startup_failed", session_id, category="malformed_output",
                        error=(f"Claude Code session_id changed from {session_id!r} "
                               f"to {record_session_id!r}"),
                    )
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
                    return 1
                session_id = record_session_id
            if record.get("type") in {"system", "assistant", "stream_event", "result"}:
                if not execution_started:
                    execution_started = True
                    _emit_session_event("execution_started", session_id,
                                         subtype=record.get("subtype"))

            text = assistant_text(record)
            if text and record.get("type") == "assistant":
                transcript.append(text)
                _emit_session_event("messaged", session_id, text=text[:500])
            elif text and record.get("type") == "stream_event":
                # Partial chunks keep the watchdog alive. The full assistant
                # record is deliberately not emitted a second time below.
                _emit_session_event("messaged", session_id, text=text[:500])
            for tool in tool_uses(record):
                _emit_session_event("tool_used", session_id, **tool)

            if record.get("type") != "result":
                continue

            snapshot = result_snapshot(record)
            emit("result", **snapshot)
            failure = startup_failure(record, "\n".join(transcript))
            if failure:
                category, reason = failure
                _emit_session_event("startup_failed", session_id,
                                     category=category, reason=reason, result=snapshot)
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
                return 1
            if not execution_started:
                emit("startup_failed", category="cli_initialization_failure",
                     reason="Claude Code returned no executable session events")
                return 1

            terminal_kind, extra = parse_terminal(record.get("result"))
            if terminal_kind == "asked":
                _emit_session_event("asked", session_id, **extra,
                                    tokens_in=snapshot["tokens_in"],
                                    tokens_out=snapshot["tokens_out"],
                                    cost_usd=snapshot["cost_usd"])
                reply = await asyncio.to_thread(sys.stdin.readline)
                if not reply:
                    try:
                        process.stdin.close()
                    except (BrokenPipeError, AttributeError):
                        pass
                    return 0
                await _send_prompt(process, reply.rstrip("\n"))
            elif terminal_kind == "done_claimed":
                _emit_session_event("done_claimed", session_id, **extra,
                                    tokens_in=snapshot["tokens_in"],
                                    tokens_out=snapshot["tokens_out"],
                                    cost_usd=snapshot["cost_usd"])
                # The scheduler reaps the process as soon as it sees this
                # event. Waiting here also handles direct CLI use cleanly.
                break
    except (BrokenPipeError, ConnectionError, OSError) as exc:
        if not execution_started:
            emit("startup_failed", category="cli_process_failure", error=_redact_text(exc))
        return 1

    returncode = await process.wait()
    if returncode != 0 and terminal_kind is None:
        # With no worker claim, the scheduler's normal EOF handling records
        # worker.exited. A startup failure is reserved for a CLI that never
        # established a usable session or could not be parsed.
        if not execution_started:
            emit("startup_failed", category="cli_process_failure",
                 error=f"claude exited with code {returncode}")
            return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--brief-file", required=True)
    parser.add_argument("--model")
    args = parser.parse_args()

    brief_path = Path(args.brief_file)
    brief = brief_path.read_text()
    brief_path.unlink(missing_ok=True)
    raise SystemExit(asyncio.run(run(Path(args.worktree), brief, args.model)))


if __name__ == "__main__":
    main()
