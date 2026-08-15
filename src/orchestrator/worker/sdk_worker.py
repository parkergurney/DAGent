"""Real Agent SDK worker speaking the common JSON-lines protocol.

The worktree path hook is a convenience check for structured file tools, not
an OS sandbox. Bash and other subprocesses remain able to reach the host
unless Harbor or another trusted outer environment supplies containment.
"""
import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
    HookMatcher, ResultMessage,
)

_PROTOCOL = """
You must perform the requested work in the worktree using the available coding
tools. A text-only explanation is not completion. Begin by inspecting or
editing the relevant file immediately; do not just describe what should be
done.

Commit your changes in the worktree before claiming done. Uncommitted work
will fail verification.

Run the requested visible verification after editing and correct any failure.

When you are completely finished, end your final message with exactly one line:
DONE_CLAIM: <one-line summary of what you did>

If you are blocked on a decision only a human can make, end your message with
exactly one line instead, and stop there:
ASK: <your question>

If the requested work is already satisfied and no file change is required,
end your final message with exactly one line instead:
NO_CHANGE: <one-line explanation>
"""
_AUTH_FAILURE_MARKERS = (
    "not logged in", "please run /login", "authentication required",
    "authentication failed", "unauthorized", "invalid oauth",
)
_SECRET_PATTERNS = (
    (re.compile(r"(?i)\bsk-ant-[a-z0-9_-]+"), "[REDACTED_ANTHROPIC_CREDENTIAL]"),
    (re.compile(r"(?i)\bbearer\s+[a-z0-9._-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)(oauth[_ -]?token|access[_ -]?token|refresh[_ -]?token)\s*[:=]\s*\S+"),
     r"\1=[REDACTED]"),
)
_OLLAMA_TOOLS = ["Bash", "Read", "Edit", "Write", "Glob", "Grep"]


def _prompt_with_protocol(brief: str) -> str:
    return f"{brief.rstrip()}\n\n{_PROTOCOL.strip()}\n"


def _parse_terminal(result_text: str | None) -> tuple:
    if not result_text:
        return None, {}
    for line in result_text.splitlines():
        line = line.strip()
        if line.startswith("DONE_CLAIM:"):
            return "done_claimed", {"result": line[len("DONE_CLAIM:"):].strip()}
        if line.startswith("ASK:"):
            return "asked", {"question": line[len("ASK:"):].strip()}
        if line.startswith("NO_CHANGE:"):
            return "no_change", {"result": line[len("NO_CHANGE:"):].strip()}
    return None, {}


def _redact_text(value: object, *, limit: int = 2000) -> str:
    text = str(value or "")
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:limit]


def _normalize_json(value, *, depth: int = 0):
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if depth >= 3:
        return _redact_text(value, limit=500)
    if isinstance(value, dict):
        return {_redact_text(k, limit=100): _normalize_json(v, depth=depth + 1)
                for k, v in list(value.items())[:64]}
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item, depth=depth + 1) for item in value[:64]]
    return _redact_text(f"<{type(value).__name__}>", limit=100)


def _result_snapshot(message: ResultMessage) -> dict:
    usage = _normalize_json(message.usage or {})
    model_usage = _normalize_json(message.model_usage or {})
    return {
        "subtype": _redact_text(getattr(message, "subtype", None), limit=100),
        "is_error": bool(message.is_error), "errors": _normalize_json(message.errors or []),
        "api_error_status": message.api_error_status,
        "result": _redact_text(message.result), "session_id": _redact_text(message.session_id, limit=200),
        "usage": usage, "model_usage": model_usage, "total_cost_usd": message.total_cost_usd,
        "stop_reason": _redact_text(message.stop_reason, limit=100), "num_turns": message.num_turns,
        "duration_ms": message.duration_ms, "duration_api_ms": message.duration_api_ms,
        "tokens_in": usage.get("input_tokens") if isinstance(usage, dict) else None,
        "tokens_out": usage.get("output_tokens") if isinstance(usage, dict) else None,
        "cost_usd": message.total_cost_usd,
    }


def _startup_failure_category(snapshot: dict | None, transcript: list[str]) -> tuple[str, str] | None:
    result = snapshot or {}
    text = "\n".join([*transcript, str(result.get("result") or ""),
                       *[str(item) for item in result.get("errors") or []]])
    lowered = text.lower()
    if any(marker in lowered for marker in _AUTH_FAILURE_MARKERS):
        return "authentication_failure", "Claude Code reported an authentication failure"
    if result.get("is_error"):
        if result.get("api_error_status"):
            return ("sdk_failure" if result.get("session_id") else "backend_initialization_failure",
                    "Claude backend returned an API error")
        return ("sdk_failure" if result.get("session_id") else "backend_initialization_failure",
                "Claude Code returned an error ResultMessage")
    if not result.get("session_id") or result.get("subtype") not in ("success", "completion"):
        return "sdk_initialization_failure", "Claude SDK did not produce a successful session result"
    return None


def _path_escapes_worktree(path_str: str, worktree: Path) -> bool:
    candidate = Path(path_str)
    resolved = candidate if candidate.is_absolute() else worktree / candidate
    try:
        resolved.resolve().relative_to(worktree.resolve())
        return False
    except (ValueError, OSError):
        return True


def emit(type_, **payload) -> None:
    print(json.dumps({"type": type_, "payload": payload}), flush=True)


def _make_pre_tool_use(worktree: Path):
    async def hook(input_data, tool_use_id, context):
        path = input_data.get("tool_input", {}).get("file_path")
        if path and _path_escapes_worktree(path, worktree):
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": f"path {path!r} escapes the task worktree",
            }}
        return {}
    return hook


async def _post_tool_use(input_data, tool_use_id, context):
    tool_input = input_data.get("tool_input", {}) or {}
    emit("tool_used", tool=input_data.get("tool_name"),
         target=tool_input.get("file_path") or tool_input.get("command") or "",
         agent_id=input_data.get("agent_id"))
    return {}


async def _post_tool_use_failure(input_data, tool_use_id, context):
    emit("tool_used", tool=input_data.get("tool_name"), error=input_data.get("error"),
         agent_id=input_data.get("agent_id"))
    return {}


def _agent_options(worktree: Path, model: str | None, *, stderr=None) -> ClaudeAgentOptions:
    options = {
        "cwd": str(worktree),
        "model": model,
        "hooks": {
            "PreToolUse": [HookMatcher(hooks=[_make_pre_tool_use(worktree)])],
            "PostToolUse": [HookMatcher(hooks=[_post_tool_use])],
            "PostToolUseFailure": [HookMatcher(hooks=[_post_tool_use_failure])],
        },
    }
    if stderr is not None:
        options["stderr"] = stderr
    # Claude Code's complete tool/system profile is roughly 18K tokens. That
    # leaves no usable generation room for the local Qwen model at 16K and
    # makes prompt evaluation unacceptably slow at 32K. The compact profile is
    # sufficient for the worker contract's coding tasks and keeps the normal
    # Claude backend behavior unchanged.
    if os.environ.get("ORCH_BACKEND", "").strip().lower() == "ollama":
        options.update({
            "tools": list(_OLLAMA_TOOLS),
            "permission_mode": "bypassPermissions",
            "thinking": {"type": "disabled"},
        })
    return ClaudeAgentOptions(**options)


async def run(worktree: Path, brief: str, model: str | None) -> int:
    sdk_stderr: list[str] = []

    def capture_sdk_stderr(line: str) -> None:
        if line:
            sdk_stderr.append(_redact_text(line, limit=1000))

    try:
        options = _agent_options(worktree, model, stderr=capture_sdk_stderr)
        client = ClaudeSDKClient(options=options)
        await client.connect()
    except Exception as exc:
        category = "authentication_failure" if any(
            marker in _redact_text(exc).lower() for marker in _AUTH_FAILURE_MARKERS
        ) else "sdk_initialization_failure"
        error = _redact_text(exc)
        if sdk_stderr:
            error = f"{error}; sdk_stderr: {_redact_text(' '.join(sdk_stderr), limit=2000)}"
        emit("startup_failed", category=category, error=error)
        return 1

    try:
        await client.query(_prompt_with_protocol(brief))
        execution_started = False
        while True:
            kind, extra, cost_usd, session_id = None, {}, None, None
            transcript = []
            result_snapshot = None
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    text = "".join(getattr(block, "text", "") or "" for block in msg.content)
                    if text.strip():
                        transcript.append(text)
                        usage = msg.usage or {}
                        emit("messaged", text=text[:500], tokens_in=usage.get("input_tokens"),
                             tokens_out=usage.get("output_tokens"))
                elif isinstance(msg, ResultMessage):
                    result_snapshot = _result_snapshot(msg)
                    kind, extra = _parse_terminal(msg.result)
                    cost_usd, session_id = msg.total_cost_usd, msg.session_id
                    emit("result", **result_snapshot)
                    failure = _startup_failure_category(result_snapshot, transcript)
                    if failure:
                        category, reason = failure
                        emit("sdk_failed" if category == "sdk_failure" else "startup_failed",
                             category=category, reason=reason, result=result_snapshot)
                        return 1
                    if not execution_started:
                        emit("execution_started", session_id=session_id,
                             subtype=result_snapshot.get("subtype"))
                        execution_started = True
            if result_snapshot is None:
                emit("startup_failed", category="sdk_initialization_failure",
                     reason="Claude SDK stream ended without a ResultMessage")
                return 1
            if kind == "asked":
                emit("asked", cost_usd=cost_usd, session_id=session_id, **extra)
                reply = sys.stdin.readline()
                if not reply:
                    return 0
                await client.query(reply.rstrip("\n"))
                continue
            if kind == "no_change":
                emit("no_change", cost_usd=cost_usd, session_id=session_id, **extra)
                return 0
            if kind == "done_claimed":
                emit("done_claimed", cost_usd=cost_usd, session_id=session_id, **extra)
            else:
                # A successful SDK result without DONE_CLAIM/ASK is a real
                # worker protocol failure. Preserve the distinction from an
                # infrastructure startup failure so the scheduler/artifacts
                # explain why no candidate was produced.
                emit(
                    "unclaimed",
                    reason="result_missing_terminal_marker",
                    session_id=session_id,
                    result=_redact_text(result_snapshot.get("result")),
                )
            return 0
    finally:
        await client.disconnect()


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
