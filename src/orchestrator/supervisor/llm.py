"""One-shot, stateless supervisor call (design.md section 6): "A single
Messages API call -- no tools, no session, no memory. Stateless and
therefore replayable." claude_agent_sdk.query() in single-shot mode is what
this environment can actually authenticate through (Claude Code CLI auth;
no ANTHROPIC_API_KEY is configured here) -- tools=[] and max_turns=1 keep it
a pure completion rather than an agentic session.

Every invocation is dumped to disk (packet + raw response + final action)
BEFORE any prompt tuning happens, per M4's milestone note -- see
supervisor/replay.py for re-running a saved packet against the current
prompt/model offline.
"""
import json
import os
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, query

from orchestrator.supervisor.schema import ACTION_MODELS, SupervisorAction, TriagePacket

DATA_DIR = Path(os.environ.get("ORCH_DATA_DIR", "data"))

SYSTEM_PROMPT = """You are the triage supervisor for an autonomous coding-agent \
orchestrator. A worker task hit a problem; you decide what happens next from a \
CLOSED set of actions. Respond with ONLY a single JSON object, no prose, no \
markdown fences, matching exactly one of the allowed action schemas given to you.

Never guess on the manager's behalf: if the worker asked a question and the \
brief doesn't unambiguously answer it, escalate rather than nudge. Prefer \
escalate over a second restart against a failure signature you've already seen \
once in this task's history -- a confused session rarely un-confuses itself, \
and repeating the same failure twice means the brief is the problem, not the \
worker. For a stalled worker, prefer wait if the transcript suggests it's \
plausibly waiting on something external, and restart if it looks like it's \
spinning on the same few tool calls.
"""


@dataclass
class SupervisorResult:
    action: SupervisorAction
    ok: bool  # False means this is the fallback-to-escalate after two invalid responses
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: float | None
    raw_text: str | None


def _schema_block(allowed_actions: list[str]) -> str:
    parts = [f"{name}: {ACTION_MODELS[name].model_json_schema()}" for name in allowed_actions]
    return "\n".join(parts)


def _parse(text: str, allowed_actions: list[str]) -> SupervisorAction:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    text = text[text.find("{"):text.rfind("}") + 1]
    data = json.loads(text)
    action_name = data.get("action")
    if action_name not in allowed_actions:
        raise ValueError(f"action {action_name!r} not in allowed_actions {allowed_actions}")
    return ACTION_MODELS[action_name].model_validate(data)


async def _one_shot(prompt: str, model: str | None) -> tuple:
    """A single stateless completion via the Agent SDK's query() function --
    no tools, no session, no memory."""
    options = ClaudeAgentOptions(model=model, tools=[], max_turns=1, system_prompt=SYSTEM_PROMPT)
    text_parts = []
    usage = {"tokens_in": None, "tokens_out": None, "cost_usd": None}
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            text_parts.append("".join(getattr(b, "text", "") or "" for b in msg.content))
        elif isinstance(msg, ResultMessage):
            usage = {"tokens_in": (msg.usage or {}).get("input_tokens"),
                    "tokens_out": (msg.usage or {}).get("output_tokens"),
                    "cost_usd": msg.total_cost_usd}
    return "".join(text_parts), usage


def _dump(packet: TriagePacket, result: SupervisorResult, artifact_root=None) -> None:
    out_dir = (Path(artifact_root) if artifact_root else DATA_DIR / packet.task_id) / "packets"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{packet.trigger.seq}.json"
    path.write_text(json.dumps({
        "packet": packet.model_dump(),
        "action": result.action.model_dump(),
        "ok": result.ok,
        "raw_text": result.raw_text,
        "tokens_in": result.tokens_in, "tokens_out": result.tokens_out, "cost_usd": result.cost_usd,
    }, indent=2))


def _fallback_escalate(packet: TriagePacket, error: str, raw_text: str | None) -> SupervisorResult:
    action = ACTION_MODELS["escalate"](
        summary="supervisor failed to produce a valid action",
        question=f"Task hit {packet.trigger.type} and the supervisor's response was invalid twice: {error}",
        options=["review manually", "abandon task"], recommended=None, reason=error,
    )
    return SupervisorResult(action=action, ok=False, tokens_in=None, tokens_out=None,
                            cost_usd=None, raw_text=raw_text)


async def invoke_supervisor(packet: TriagePacket, *, model: str | None = None,
                            artifact_root=None) -> SupervisorResult:
    prompt = f"{_schema_block(packet.allowed_actions)}\n\nPacket:\n{packet.model_dump_json(indent=2)}"
    text = None

    for attempt in range(2):
        try:
            text, usage = await _one_shot(prompt, model)
            action = _parse(text, packet.allowed_actions)
        except Exception as e:
            # Broad on purpose: this is the explicit "when the judgment layer
            # breaks, degrade to the human, visibly" boundary (design.md
            # section 6, enforcement rule 3) -- a malformed response and a
            # transport-level SDK failure both end the same way, a synthetic
            # Escalate, never a crash.
            if attempt == 0:
                prompt += f"\n\nYour previous response was invalid ({e}). Respond with ONLY the corrected JSON object."
                continue
            result = _fallback_escalate(packet, str(e), text)
            _dump(packet, result, artifact_root)
            return result
        else:
            result = SupervisorResult(action=action, ok=True, raw_text=text, **usage)
            _dump(packet, result, artifact_root)
            return result

    # unreachable, but keeps type checkers happy about the loop always returning
    result = _fallback_escalate(packet, "exhausted retries", text)
    _dump(packet, result)
    return result
