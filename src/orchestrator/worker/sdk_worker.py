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
from datetime import datetime, timezone
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
    HookMatcher, ResultMessage,
)
from orchestrator.diagnostics import live_diagnostic

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
_NETWORK_ATTEMPT = re.compile(
    r"(?i)(curl|wget|fetch|http://|https://|git\s+(clone|fetch|pull|remote)|"
    r"pip\s+install|uv\s+(pip\s+)?install|npm\s+install|yarn\s+add|"
    r"requests|urllib|socket|nc\s|ssh\s)"
)
_BLOCKED_COMMAND = re.compile(
    r"(?i)(?:\b(?:curl|wget)\b|"
    r"\b(?:pip3?|python(?:3)?\s+-m\s+pip)\s+install\b|"
    r"\buv\s+(?:pip\s+)?install\b|"
    r"\b(?:npm\s+install|pnpm\s+add|yarn\s+add)\b|"
    r"\bgit(?:\s+-[A-Za-z]+\s+\S+)*\s+(?:clone|fetch|pull|remote|submodule)\b)"
)


def _sdk_timeout_s() -> float:
    try:
        value = float(os.environ.get("ORCH_SDK_TIMEOUT_S", "300"))
    except ValueError:
        value = 300.0
    return max(value, 0.1)


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
    if result.get("api_error_status") == 401:
        return "authentication_failure", "Claude backend rejected authentication (HTTP 401)"
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


def _audit_tool(*, phase: str, input_data: dict, task_id: str | None,
                decision: str, error: object = None, reason: str | None = None) -> None:
    """Persist a small, credential-redacted tool-use audit when configured."""
    audit_path = os.environ.get("ORCH_TOOL_AUDIT_PATH")
    if not audit_path:
        return
    tool_input = input_data.get("tool_input", {}) or {}
    tool = _redact_text(input_data.get("tool_name"), limit=100)
    target = _redact_text(
        tool_input.get("file_path") or tool_input.get("command") or "", limit=2000,
    )
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "task_id": _redact_text(task_id, limit=100),
        "phase": phase,
        "tool": tool,
        "target": target,
        "decision": decision,
        "likely_network_or_history_attempt": bool(_NETWORK_ATTEMPT.search(target)),
    }
    if error is not None:
        record["error"] = _redact_text(error, limit=1000)
    if reason is not None:
        record["reason"] = _redact_text(reason, limit=1000)
    path = Path(audit_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        # Auditing must never change the worker's execution result.
        return


def _make_pre_tool_use(worktree: Path, task_id: str | None = None):
    async def hook(input_data, tool_use_id, context):
        del tool_use_id, context
        tool_input = input_data.get("tool_input", {}) or {}
        tool_name = str(input_data.get("tool_name") or "")
        command = str(tool_input.get("command") or "")
        if tool_name == "Bash" and _BLOCKED_COMMAND.search(command):
            reason = (
                "network, package-install, or remote-Git commands are disabled "
                "for this benchmark; use the preinstalled dependencies and local sources"
            )
            _audit_tool(
                phase="pre", input_data=input_data, task_id=task_id,
                decision="denied", reason=reason,
            )
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }}
        path = tool_input.get("file_path")
        if path and _path_escapes_worktree(path, worktree):
            _audit_tool(phase="pre", input_data=input_data, task_id=task_id, decision="denied")
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": f"path {path!r} escapes the task worktree",
            }}
        _audit_tool(phase="pre", input_data=input_data, task_id=task_id, decision="allowed")
        return {}
    return hook


async def _post_tool_use(input_data, tool_use_id, context, task_id=None):
    del tool_use_id, context
    tool_input = input_data.get("tool_input", {}) or {}
    target = _redact_text(tool_input.get("file_path") or tool_input.get("command") or "")
    _audit_tool(phase="post", input_data=input_data, task_id=task_id, decision="completed")
    emit("tool_used", tool=input_data.get("tool_name"),
         target=target,
         agent_id=input_data.get("agent_id"))
    return {}


async def _post_tool_use_failure(input_data, tool_use_id, context, task_id=None):
    del tool_use_id, context
    error = _redact_text(input_data.get("error"), limit=1000)
    _audit_tool(phase="post", input_data=input_data, task_id=task_id,
                decision="failed", error=error)
    emit("tool_used", tool=input_data.get("tool_name"), error=error,
         agent_id=input_data.get("agent_id"))
    return {}


def _agent_options(worktree: Path, model: str | None, *, stderr=None,
                   task_id: str | None = None) -> ClaudeAgentOptions:
    async def post_tool_use(input_data, tool_use_id, context):
        return await _post_tool_use(input_data, tool_use_id, context, task_id)

    async def post_tool_use_failure(input_data, tool_use_id, context):
        return await _post_tool_use_failure(input_data, tool_use_id, context, task_id)

    options = {
        "cwd": str(worktree),
        "model": model,
        # Workers run without an interactive approval channel. Harbor or
        # trusted-development mode supplies the outer isolation boundary;
        # the worktree hook below still denies structured paths outside it.
        "permission_mode": "bypassPermissions",
        "hooks": {
            "PreToolUse": [HookMatcher(hooks=[_make_pre_tool_use(worktree, task_id)])],
            "PostToolUse": [HookMatcher(hooks=[post_tool_use])],
            "PostToolUseFailure": [HookMatcher(hooks=[post_tool_use_failure])],
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
            "thinking": {"type": "disabled"},
        })
    return ClaudeAgentOptions(**options)


async def run(worktree: Path, brief: str, model: str | None, task_id: str | None = None) -> int:
    sdk_stderr: list[str] = []
    sdk_timeout_s = _sdk_timeout_s()

    def diagnostic(event: str, **payload) -> None:
        if task_id is not None:
            payload["task_id"] = task_id
        live_diagnostic(event, **payload)

    async def phase_heartbeat(phase: str, **payload) -> None:
        while True:
            diagnostic("sdk.heartbeat", phase=phase, **payload)
            await asyncio.sleep(15)

    async def with_heartbeat(awaitable, phase: str, **payload):
        heartbeat = asyncio.create_task(phase_heartbeat(phase, **payload))
        try:
            return await awaitable
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    def capture_sdk_stderr(line: str) -> None:
        if line:
            sdk_stderr.append(_redact_text(line, limit=1000))

    try:
        diagnostic("sdk.worker_started")
        options = _agent_options(worktree, model, stderr=capture_sdk_stderr, task_id=task_id)
        client = ClaudeSDKClient(options=options)
        diagnostic("sdk.connect_started", timeout_s=sdk_timeout_s)
        async with asyncio.timeout(sdk_timeout_s):
            await with_heartbeat(client.connect(), "connect")
        diagnostic("sdk.connect_succeeded")
    except asyncio.TimeoutError:
        diagnostic("sdk.timeout", phase="connect", timeout_s=sdk_timeout_s)
        emit("startup_failed", category="sdk_timeout", phase="connect",
             timeout_s=sdk_timeout_s,
             reason="Claude SDK connection exceeded its bounded timeout")
        return 1
    except Exception as exc:
        diagnostic("sdk.connect_failed", failure_type=type(exc).__name__)
        category = "authentication_failure" if any(
            marker in _redact_text(exc).lower() for marker in _AUTH_FAILURE_MARKERS
        ) else "sdk_initialization_failure"
        error = _redact_text(exc)
        if sdk_stderr:
            error = f"{error}; sdk_stderr: {_redact_text(' '.join(sdk_stderr), limit=2000)}"
        emit("startup_failed", category=category, error=error)
        return 1

    execution_started = False
    next_prompt = _prompt_with_protocol(brief)

    async def receive_turn(prompt):
        """Run one complete SDK turn while preserving streamed worker events."""
        nonlocal execution_started, turn_number
        turn_number += 1
        diagnostic("sdk.turn_started", turn=turn_number, timeout_s=sdk_timeout_s)
        heartbeat = asyncio.create_task(phase_heartbeat("turn", turn=turn_number))
        kind, extra, cost_usd, session_id = None, {}, None, None
        transcript = []
        result_snapshot = None
        first_response = False
        try:
            await client.query(prompt)
            diagnostic("sdk.prompt_submitted", turn=turn_number)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    if not first_response:
                        diagnostic("sdk.first_response", turn=turn_number)
                        first_response = True
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
                    diagnostic("sdk.result_received", turn=turn_number, is_error=bool(msg.is_error))
                    emit("result", **result_snapshot)
                    failure = _startup_failure_category(result_snapshot, transcript)
                    if failure:
                        category, reason = failure
                        diagnostic(
                            "sdk.failed", turn=turn_number, category=category,
                            api_error_status=result_snapshot.get("api_error_status"),
                            has_session=bool(result_snapshot.get("session_id")),
                        )
                        emit("sdk_failed" if category == "sdk_failure" else "startup_failed",
                             category=category, reason=reason, result=result_snapshot)
                        return False, kind, extra, cost_usd, session_id, result_snapshot
                    if not execution_started:
                        emit("execution_started", session_id=session_id,
                             subtype=result_snapshot.get("subtype"))
                        execution_started = True
            diagnostic("sdk.turn_stream_ended", turn=turn_number)
            return True, kind, extra, cost_usd, session_id, result_snapshot
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    turn_number = 0
    try:
        while True:
            try:
                ok, kind, extra, cost_usd, session_id, result_snapshot = await asyncio.wait_for(
                    receive_turn(next_prompt),
                    timeout=sdk_timeout_s,
                )
            except asyncio.TimeoutError:
                diagnostic("sdk.timeout", phase="turn", turn=turn_number, timeout_s=sdk_timeout_s)
                emit("sdk_timeout", category="sdk_timeout", phase="turn",
                     timeout_s=sdk_timeout_s,
                     reason="Claude SDK turn exceeded its bounded timeout")
                return 1
            if not ok:
                return 1
            if result_snapshot is None:
                emit("startup_failed", category="sdk_initialization_failure",
                     reason="Claude SDK stream ended without a ResultMessage")
                return 1
            if kind == "asked":
                emit("asked", cost_usd=cost_usd, session_id=session_id, **extra)
                reply = sys.stdin.readline()
                if not reply:
                    return 0
                next_prompt = reply.rstrip("\n")
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
        diagnostic("sdk.disconnect_started")
        try:
            async with asyncio.timeout(sdk_timeout_s):
                await with_heartbeat(client.disconnect(), "disconnect")
        except Exception:
            # The worker is already terminating; disconnect must not turn a
            # bounded SDK failure into another unbounded shutdown hang.
            pass
        diagnostic("sdk.worker_finished")


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
    raise SystemExit(asyncio.run(run(Path(args.worktree), brief, args.model, args.task_id)))


if __name__ == "__main__":
    main()
