"""M1 SDK spike (throwaway). Answers the four questions in CLAUDE.md section 8:

1. Does mid-session message injection work as assumed?
2. Cost granularity: per message or per session?
3. What does "done" look like in the stream?
4. Does PostToolUse fire for subagent tool calls (parent_tool_use_id / agent_id)?

Run: .venv/bin/python spike/m1_spike.py
Writes a full event log to spike/m1_spike_log.jsonl and prints findings.
"""

import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    UserMessage,
)

LOG = Path(__file__).parent / "m1_spike_log.jsonl"
T0 = time.monotonic()


def log(kind: str, **data):
    rec = {"t": round(time.monotonic() - T0, 2), "kind": kind, **data}
    with LOG.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    print(f"[{rec['t']:7.2f}] {kind}: {json.dumps(data, default=str)[:200]}")


def make_toy_worktree() -> Path:
    root = Path(tempfile.mkdtemp(prefix="m1spike_"))
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "README.md").write_text("Toy repo for the M1 spike. The magic word is XYZZY.\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=spike@local", "-c", "user.name=spike",
         "commit", "-qm", "init"], cwd=repo, check=True)
    wt = root / "wt"
    subprocess.run(["git", "worktree", "add", "-q", str(wt), "-b", "task"], cwd=repo, check=True)
    return wt


async def main():
    LOG.write_text("")
    wt = make_toy_worktree()
    log("setup", worktree=str(wt))

    hook_events = []

    async def post_tool_use(input_data, tool_use_id, context):
        rec = {
            "tool_name": input_data.get("tool_name"),
            "tool_use_id": input_data.get("tool_use_id"),
            "agent_id": input_data.get("agent_id"),
            "cwd": input_data.get("cwd"),
            "tool_input": str(input_data.get("tool_input"))[:200],
        }
        hook_events.append(rec)
        log("hook.PostToolUse", **rec)
        return {}

    options = ClaudeAgentOptions(
        cwd=str(wt),
        model="claude-haiku-4-5",
        permission_mode="bypassPermissions",  # throwaway toy repo, fully isolated
        hooks={"PostToolUse": [HookMatcher(hooks=[post_tool_use])]},
    )

    prompt = (
        "Do these steps in order:\n"
        "1. Use the Task tool to launch a subagent that reads README.md and "
        "reports the magic word it contains.\n"
        "2. Create a file answer.txt containing that magic word.\n"
        "3. Run `sleep 15` using Bash.\n"
        "4. Reply with exactly one final line: DONE_CLAIM: <the magic word>"
    )

    nudge_sent_at = None
    result_count = 0
    assistant_usages = []

    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)

        async def nudger():
            nonlocal nudge_sent_at
            await asyncio.sleep(20)  # land while the worker is mid-task
            nudge_sent_at = round(time.monotonic() - T0, 2)
            log("inject.sent", text="append ' nudged' to answer.txt")
            await client.query(
                "SUPERVISOR NUDGE: before you finish, also append the word "
                "'nudged' to answer.txt.")

        nudge_task = asyncio.create_task(nudger())

        async def consume():
            it = aiter(client.receive_messages())
            while True:
                # After the first result, allow 60s for a queued nudge to start
                # a second turn; silence past that answers the question too.
                per_msg_timeout = 60 if result_count >= 1 else 240
                try:
                    msg = await asyncio.wait_for(anext(it), per_msg_timeout)
                except (StopAsyncIteration, asyncio.TimeoutError) as e:
                    log("stream_end", why=type(e).__name__, after_results=result_count)
                    return
                if handle(msg):
                    return

        def handle(msg):
            nonlocal result_count
            if isinstance(msg, AssistantMessage):
                text = " | ".join(
                    getattr(b, "text", "") for b in msg.content if getattr(b, "text", ""))
                tools = [getattr(b, "name", None) for b in msg.content
                         if type(b).__name__ == "ToolUseBlock"]
                assistant_usages.append(msg.usage)
                log("assistant", model=msg.model, parent_tool_use_id=msg.parent_tool_use_id,
                    tools=tools, text=text[:150],
                    usage_keys=sorted(msg.usage.keys()) if msg.usage else None,
                    output_tokens=(msg.usage or {}).get("output_tokens"))
            elif isinstance(msg, ResultMessage):
                result_count += 1
                log("result", subtype=msg.subtype, is_error=msg.is_error,
                    num_turns=msg.num_turns, total_cost_usd=msg.total_cost_usd,
                    stop_reason=msg.stop_reason, result=(msg.result or "")[:200],
                    usage=msg.usage, session_id=msg.session_id)
                if result_count >= 2:
                    return True  # original turn + nudge turn both completed
                if nudge_sent_at is not None:
                    log("waiting", why="listening for a possible nudge-triggered second turn")
            elif isinstance(msg, SystemMessage):
                log("system", subtype=msg.subtype)
            elif isinstance(msg, UserMessage):
                content = msg.content
                kinds = ([type(b).__name__ for b in content]
                         if isinstance(content, list) else "str")
                log("user_echo", kinds=kinds)
            else:
                log("other", type=type(msg).__name__)
            return False

        await consume()
        nudge_task.cancel()

    answer = (wt / "answer.txt").read_text() if (wt / "answer.txt").exists() else "<missing>"
    log("verify", answer_txt=answer, result_messages=result_count,
        hook_count=len(hook_events),
        subagent_hooks=[h for h in hook_events if h.get("agent_id")])
    print("\nFull log:", LOG)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
