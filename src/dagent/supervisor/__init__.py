"""TriagePacket -> single LLM call -> closed action enum (design.md section 6)."""
from dagent.supervisor.actions import compute_allowed_actions
from dagent.supervisor.fake import always_escalate
from dagent.supervisor.llm import SupervisorResult, invoke_supervisor
from dagent.supervisor.packet import build_packet
from dagent.supervisor.schema import (
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
    CANONICAL_ACTION_TYPES,
    EscalateHuman,
    Retry,
    Terminate,
    canonical_action_type,
)

__all__ = [
    "TriagePacket", "TriggerEvent", "EventRow",
    "Nudge", "Restart", "Retry", "Wait", "Escalate", "EscalateHuman", "Abandon", "Terminate",
    "SupervisorAction", "ACTION_MODELS", "CANONICAL_ACTION_TYPES", "canonical_action_type",
    "compute_allowed_actions", "build_packet",
    "invoke_supervisor", "SupervisorResult", "always_escalate",
]
