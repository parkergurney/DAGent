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
