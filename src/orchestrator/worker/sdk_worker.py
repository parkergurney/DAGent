"""Real Agent SDK worker (design.md section 8, M3). Runs the Claude Agent SDK
in a dedicated subprocess and speaks the exact wire protocol fake_worker.py
speaks (JSON lines: tool_used, messaged, asked, done_claimed), so the M2
scheduler needs zero changes to drive real sessions instead of FakeWorker --
same call shape, same event shapes, same pid-based liveness story for
reconcile().

Run: python -m orchestrator.worker.sdk_worker --task-id <id> --worktree <path>
     --brief-file <path> [--model <model>]

Built on the four M1 spike findings (spike/m1_spike.py, docs/devlog.md):

  - Done-claim protocol: a required sentinel line, parsed from
    ResultMessage.result (_parse_terminal), never scraped from prose.
    ASK: is the same idea for "blocked on a human decision" -- there is no
    interactive prompt in a headless SDK session, so the brief has to ask
    for a sentinel there too.
  - The worktree cwd is NOT a sandbox boundary; a real run wrote outside it
    in two of three spike runs. _path_escapes_worktree + the PreToolUse hook
    below close that for file-editing tools. Bash is NOT guarded here --
    shelling out to write files bypasses this; the verify gate's
    uncommitted_changes/empty_diff checks are the backstop for that gap.
  - PostToolUseFailure is a distinct hook event from PostToolUse and must be
    wired separately or failed tool calls vanish from the log.
  - Cost lands once per completed turn (ResultMessage.total_cost_usd); token
    counts ride per AssistantMessage (usage dict).

M2's no-supervisor policy is unchanged here: a mid-session ASK still just
blocks on stdin (a real supervisor reply would land there in M4) and the
scheduler's watchdog/teardown ends it the same way it ends FakeWorker's ask
scenario.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
)

_PROTOCOL = """
When you are completely finished, end your final message with exactly one line:
DONE_CLAIM: <one-line summary of what you did>

If you are blocked on a decision only a human can make, end your message with
exactly one line instead, and stop there:
ASK: <your question>
"""


def _prompt_with_protocol(brief: str) -> str:
    return f"{brief.rstrip()}\n\n{_PROTOCOL.strip()}\n"


def _parse_terminal(result_text: str | None) -> tuple:
    """Pull the sentinel line out of a ResultMessage.result. Returns
    (kind, payload): ("done_claimed", {...}), ("asked", {...}), or (None, {})
    if neither sentinel is present -- an unclaimed exit, exactly like
    fake_worker's crash scenario: the scheduler already treats a stream that
    ends without one of these as worker.exited."""
    if not result_text:
        return None, {}
    for line in result_text.splitlines():
        line = line.strip()
        if line.startswith("DONE_CLAIM:"):
            return "done_claimed", {"result": line[len("DONE_CLAIM:"):].strip()}
        if line.startswith("ASK:"):
            return "asked", {"question": line[len("ASK:"):].strip()}
    return None, {}


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


async def run(worktree: Path, brief: str, model: str | None) -> None:
    options = ClaudeAgentOptions(
        cwd=str(worktree),
        model=model,
        permission_mode="bypassPermissions",  # worktree + verify gate is the isolation boundary
        hooks={
            "PreToolUse": [HookMatcher(hooks=[_make_pre_tool_use(worktree)])],
            "PostToolUse": [HookMatcher(hooks=[_post_tool_use])],
            "PostToolUseFailure": [HookMatcher(hooks=[_post_tool_use_failure])],
        },
    )
    prompt = _prompt_with_protocol(brief)

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                text = "".join(getattr(b, "text", "") or "" for b in msg.content)
                if text.strip():
                    usage = msg.usage or {}
                    emit("messaged", text=text[:500],
                        tokens_in=usage.get("input_tokens"),
                        tokens_out=usage.get("output_tokens"))
            elif isinstance(msg, ResultMessage):
                kind, extra = _parse_terminal(msg.result)
                if kind == "done_claimed":
                    emit("done_claimed", cost_usd=msg.total_cost_usd,
                        session_id=msg.session_id, **extra)
                elif kind == "asked":
                    emit("asked", cost_usd=msg.total_cost_usd,
                        session_id=msg.session_id, **extra)
                    sys.stdin.readline()  # a real supervisor reply would land here (M4)
                # else: no sentinel -- exit without a claim, on purpose.


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--task-id", required=True)
    p.add_argument("--worktree", required=True)
    p.add_argument("--brief-file", required=True)
    p.add_argument("--model")
    args = p.parse_args()

    brief_path = Path(args.brief_file)
    brief = brief_path.read_text()
    brief_path.unlink(missing_ok=True)  # never let the brief file taint git status

    asyncio.run(run(Path(args.worktree), brief, args.model))


if __name__ == "__main__":
    main()
