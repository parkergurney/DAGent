"""Closed-enum supervisor contract (design.md section 6). Validated pydantic
models: the packet the supervisor reads, and the five actions it may return.
Nothing here touches the database -- invariant 2 (design.md section 3): "The
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
    message: str
    reason: str


class Restart(BaseModel):
    action: Literal["restart"] = "restart"
    feedback: str | None = None
    reason: str


class Wait(BaseModel):
    action: Literal["wait"] = "wait"
    seconds: int
    reason: str


class Escalate(BaseModel):
    action: Literal["escalate"] = "escalate"
    summary: str
    question: str
    options: list[str]
    recommended: int | None = None
    reason: str


class Abandon(BaseModel):
    action: Literal["abandon"] = "abandon"
    reason: str


SupervisorAction = Union[Nudge, Restart, Wait, Escalate, Abandon]

ACTION_MODELS: dict[str, type[BaseModel]] = {
    "nudge": Nudge, "restart": Restart, "wait": Wait,
    "escalate": Escalate, "abandon": Abandon,
}
