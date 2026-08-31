"""Deterministic supervisor stand-in: always escalates. This is Scheduler's
default `supervisor=` (mirrors spawn_worker defaulting to FakeWorker) so the
regression suite stays free and deterministic -- a live manager wires
up supervisor.llm.invoke_supervisor explicitly instead.
"""
from dagent.supervisor.llm import SupervisorResult
from dagent.supervisor.packet import TriagePacket
from dagent.supervisor.schema import ACTION_MODELS


async def always_escalate(packet: TriagePacket) -> SupervisorResult:
    action = ACTION_MODELS["escalate"](
        summary=f"no supervisor configured; {packet.trigger.type} needs a human",
        question="how should this task proceed?", options=["review manually"],
        recommended=0, reason="default (no-LLM) supervisor",
    )
    return SupervisorResult(action=action, ok=True, tokens_in=None, tokens_out=None,
                            cost_usd=None, raw_text=None)
