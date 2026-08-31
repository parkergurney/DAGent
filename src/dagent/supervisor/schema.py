"""Closed-enum supervisor contract (see README.md). Validated pydantic
models: the packet the supervisor reads, and the five actions it may return.
Nothing here touches the database -- invariant 2 (see README.md): "The
supervisor returns one action from a closed enum and never touches the
database."
"""
from typing import Literal, Union

from pydantic import BaseModel, Field


class TriggerEvent(BaseModel):
    seq: int
    type: str
    source: str
    payload: dict = Field(default_factory=dict)


class EventRow(BaseModel):
    seq: int
    type: str
    source: str
    summary: str  # compacted: counts for tool-call runs, verbatim for the rest


class TriagePacket(BaseModel):
    task_id: str
    brief: str
    repo: str
    delivery_mode: Literal["pr", "local", "scout"]
    verify_cmd: str | None = None

    trigger: TriggerEvent
    verify_output: str | None = None

    event_history: list[EventRow]
    transcript_tail: str

    allowed_actions: list[str]
    nudges_remaining: int
    retries_remaining: int
    yolo: bool


class Nudge(BaseModel):
    action: Literal["nudge"] = "nudge"
    message: str = Field(max_length=2000)
    reason: str
    diagnosis_code: str = "worker_assistance"


class Restart(BaseModel):
    action: Literal["restart"] = "restart"
    feedback: str | None = None
    reason: str
    # Explicit worker-facing text is preferred to verbose supervisor prose.
    worker_instruction: str | None = Field(default=None, max_length=2000)
    diagnosis_code: str = "recovery_required"


class Wait(BaseModel):
    action: Literal["wait"] = "wait"
    seconds: int = Field(ge=1, le=1800)
    reason: str
    diagnosis_code: str = "external_wait"


class Escalate(BaseModel):
    action: Literal["escalate"] = "escalate"
    summary: str
    question: str
    options: list[str]
    recommended: int | None = None
    reason: str
    diagnosis_code: str = "human_review"


class Abandon(BaseModel):
    action: Literal["abandon"] = "abandon"
    reason: str
    diagnosis_code: str = "terminal_failure"


SupervisorAction = Union[Nudge, Restart, Wait, Escalate, Abandon]

ACTION_MODELS: dict[str, type[BaseModel]] = {
    "nudge": Nudge, "restart": Restart, "wait": Wait,
    "escalate": Escalate, "abandon": Abandon,
}

# Canonical v2 accounting names. The legacy wire names above remain accepted
# by the current CLI and saved packet fixtures; the mapping keeps persisted
# intervention reports independent of that compatibility surface.
CANONICAL_ACTION_TYPES = {
    "restart": "RETRY",
    "wait": "WAIT",
    "escalate": "ESCALATE_HUMAN",
    "abandon": "TERMINATE",
    "nudge": "NUDGE",
}


def canonical_action_type(action: str) -> str:
    return CANONICAL_ACTION_TYPES.get(action, action.upper())


# Readable aliases for integrations using the v2 vocabulary. They intentionally
# retain the established legacy literal values for wire compatibility.
Retry = Restart
EscalateHuman = Escalate
Terminate = Abandon
