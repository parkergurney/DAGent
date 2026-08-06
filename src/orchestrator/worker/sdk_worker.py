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
  - The worktree cwd is NOT a permission-system boundary; a real run wrote
    outside it in two of three spike runs. _path_escapes_worktree + the
    PreToolUse hook below close that for file-editing tools (Read/Edit/
    Write), but Bash was left uninspected -- shelling out (`sed -i` on an
    absolute path, batch01) bypassed it twice in dogfooding. Claude Code's
    native OS-level Bash sandbox (Seatbelt/bubblewrap, docs/design.md
    section 8) now closes that gap: it restricts the Bash tool's process at
    the OS level, independent of what the hook inspects. The two layers
    compose -- hook = intent for structured tools, sandbox = capability for
    Bash -- neither alone was sufficient.
  - PostToolUseFailure is a distinct hook event from PostToolUse and must be
    wired separately or failed tool calls vanish from the log.
  - Cost lands once per completed turn (ResultMessage.total_cost_usd); token
    counts ride per AssistantMessage (usage dict).

A mid-session ASK blocks on stdin; a supervisor `nudge` (scheduler/core.py's
_handle_triage) writes its message there, which this resumes the SDK
conversation with -- the live-intervention path design.md section 6
describes. If nothing ever lands on stdin (escalate/abandon instead), the
scheduler's teardown kills the process group and the blocked readline dies
with it, same as any other torn-down attempt.
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
    ClaudeSDKError,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
)

_GITHUB_HOSTS = ("github.com", "api.github.com", "raw.githubusercontent.com",
                 "gist.github.com", "objects.githubusercontent.com")
_HOSTED_WEB_TOOLS = {"WebFetch", "WebSearch"}
_GITHUB_COMMAND_MARKERS = (
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "gist.github.com",
    "objects.githubusercontent.com",
    "gh ",
    "git fetch",
    "git pull",
    "git clone",
    "git ls-remote",
    "git remote update",
)

_PROTOCOL = """
Commit your changes in the worktree before claiming done. Uncommitted work
will fail verification.

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


async def _can_use_tool(tool_name, tool_input, context):
    """Replaces permission_mode="bypassPermissions": that flag auto-grants
    every decision the CLI would otherwise need to make, INCLUDING the
    sandbox's own network-domain approval, which the SDK exposes as a
    synthetic "SandboxNetworkAccess" tool call routed through this same
    callback -- confirmed empirically, bypassPermissions let a sandboxed
    curl through with a live HTTP response despite sandbox.network.
    strictAllowlist=True, because there was no decision point left for
    strictAllowlist to convert into a deny. Auto-allowing everything else
    here (instead of bypassPermissions) keeps sessions headless -- no
    hang waiting on a human for a plain in-worktree Read/Edit/Write -- while
    this callback, not the CLI's blanket bypass, keeps ownership of the one
    decision that must stay a real deny."""
    if tool_name == "SandboxNetworkAccess":
        host = str(tool_input.get("host", "")).lower()
        if any(host == gh or host.endswith(f".{gh}") for gh in _GITHUB_HOSTS):
            return PermissionResultDeny(
                message="GitHub access is not permitted for benchmark worker sessions")
        return PermissionResultDeny(
            message="network access is not permitted for worker sessions "
                    "(verify/setup_cmd run outside the session, in the gate)")
    if tool_name in _HOSTED_WEB_TOOLS:
        return PermissionResultDeny(
            message="hosted web/search tools are not permitted for benchmark worker sessions")
    if _mentions_github(tool_input):
        return PermissionResultDeny(
            message="GitHub access is not permitted for benchmark worker sessions")
    return PermissionResultAllow()


def _mentions_github(value) -> bool:
    if isinstance(value, dict):
        return any(_mentions_github(v) for v in value.values())
    if isinstance(value, list | tuple):
        return any(_mentions_github(v) for v in value)
    text = str(value).lower()
    return any(marker in text for marker in _GITHUB_COMMAND_MARKERS)


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
        can_use_tool=_can_use_tool,  # headless auto-allow, except SandboxNetworkAccess (see docstring)
        sandbox={
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "allowUnsandboxedCommands": False,  # dangerouslyDisableSandbox escape hatch is a no-op
            "failIfUnavailable": True,  # hard-fail startup instead of silently running unsandboxed
            "network": {"strictAllowlist": True},  # belt-and-suspenders; _can_use_tool is the real gate
        },
        hooks={
            "PreToolUse": [HookMatcher(hooks=[_make_pre_tool_use(worktree)])],
            "PostToolUse": [HookMatcher(hooks=[_post_tool_use])],
            "PostToolUseFailure": [HookMatcher(hooks=[_post_tool_use_failure])],
        },
    )
    prompt = _prompt_with_protocol(brief)

    client = ClaudeSDKClient(options=options)
    try:
        await client.connect()
    except ClaudeSDKError as e:
        # failIfUnavailable=True makes a missing sandbox dependency (or an
        # unsupported platform) a hard connect failure instead of the CLI's
        # default warn-and-run-unsandboxed. Stderr is DEVNULL'd by the
        # spawning process (orchestrator/worker/sdk.py), so emit() is the
        # only way this reaches the operator -- a bare exit code looks like
        # any other worker.exited crash.
        emit("startup_failed", error=str(e))
        sys.exit(1)

    try:
        await client.query(prompt)
        while True:
            kind, extra, cost_usd, session_id = None, {}, None, None
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
                    cost_usd, session_id = msg.total_cost_usd, msg.session_id

            if kind == "asked":
                emit("asked", cost_usd=cost_usd, session_id=session_id, **extra)
                reply = sys.stdin.readline()
                if not reply:
                    return  # stdin closed -- torn down (escalate/abandon), nothing to resume
                await client.query(reply.rstrip("\n"))
                continue  # a nudge landed on stdin -- resume this same conversation

            if kind == "done_claimed":
                emit("done_claimed", cost_usd=cost_usd, session_id=session_id, **extra)
            return  # done_claimed, or no sentinel -- exit without a claim, on purpose
    finally:
        await client.disconnect()


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
