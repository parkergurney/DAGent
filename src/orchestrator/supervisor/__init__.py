"""TriagePacket -> single LLM call -> closed action enum (design.md section 6)."""
from orchestrator.supervisor.actions import compute_allowed_actions
from orchestrator.supervisor.fake import always_escalate
from orchestrator.supervisor.llm import SupervisorResult, invoke_supervisor
from orchestrator.supervisor.packet import build_packet
from orchestrator.supervisor.schema import (
    ACTION_MODELS,
    Abandon,
    Escalate,
    EventRow,
    Nudge,
    Restart,
    SupervisorAction,
    TriagePacket,
    TriggerEvent,
    Wait,
)

__all__ = [
    "TriagePacket", "TriggerEvent", "EventRow",
    "Nudge", "Restart", "Wait", "Escalate", "Abandon", "SupervisorAction", "ACTION_MODELS",
    "compute_allowed_actions", "build_packet",
    "invoke_supervisor", "SupervisorResult", "always_escalate",
]
