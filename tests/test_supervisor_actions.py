"""Deterministic menu enforcement (see README.md). Pure function, no
LLM, no DB -- this is the layer that makes an out-of-menu response
impossible regardless of what any supervisor implementation returns.
"""
from dagent.supervisor.actions import compute_allowed_actions


def test_stalled_live_session_offers_wait_not_nudge():
    a = compute_allowed_actions("worker.stalled", nudges_remaining=2, retries_remaining=1,
                                yolo=False, live_session=True)
    assert set(a) == {"restart", "escalate", "wait"}


def test_asked_live_session_offers_nudge_not_wait():
    a = compute_allowed_actions("worker.asked", nudges_remaining=2, retries_remaining=1,
                                yolo=False, live_session=True)
    assert set(a) == {"restart", "escalate", "nudge"}


def test_no_live_session_never_offers_nudge_or_wait():
    for trigger in ["worker.exited", "verify.failed", "delivery.failed"]:
        a = compute_allowed_actions(trigger, nudges_remaining=2, retries_remaining=1,
                                    yolo=False, live_session=False)
        assert set(a) == {"restart", "escalate"}, trigger


def test_nudges_exhausted_drops_nudge():
    a = compute_allowed_actions("worker.asked", nudges_remaining=0, retries_remaining=1,
                                yolo=False, live_session=True)
    assert "nudge" not in a


def test_retries_exhausted_forces_escalate_only():
    a = compute_allowed_actions("worker.stalled", nudges_remaining=2, retries_remaining=0,
                                yolo=False, live_session=True)
    assert a == ["escalate"]


def test_retries_exhausted_yolo_allows_abandon():
    a = compute_allowed_actions("worker.stalled", nudges_remaining=2, retries_remaining=0,
                                yolo=True, live_session=True)
    assert set(a) == {"escalate", "abandon"}


def test_yolo_adds_abandon_when_retries_remain():
    a = compute_allowed_actions("verify.failed", nudges_remaining=2, retries_remaining=1,
                                yolo=True, live_session=False)
    assert set(a) == {"restart", "escalate", "abandon"}
