"""Closed-enum action schema validation (see README.md)."""
import pytest
from pydantic import ValidationError

from dagent.supervisor.schema import ACTION_MODELS, Escalate, Nudge


def test_nudge_requires_message_and_reason():
    with pytest.raises(ValidationError):
        Nudge.model_validate({"action": "nudge"})
    n = Nudge.model_validate({"action": "nudge", "message": "hi", "reason": "why"})
    assert n.message == "hi"


def test_escalate_options_is_a_list():
    e = Escalate.model_validate({"action": "escalate", "summary": "s", "question": "q",
                                 "options": ["a", "b"], "reason": "r"})
    assert e.options == ["a", "b"]
    assert e.recommended is None


def test_action_models_cover_the_closed_enum():
    assert set(ACTION_MODELS) == {"nudge", "restart", "wait", "escalate", "abandon"}


def test_action_literal_rejects_mismatch():
    with pytest.raises(ValidationError):
        ACTION_MODELS["nudge"].model_validate({"action": "restart", "message": "m", "reason": "r"})
