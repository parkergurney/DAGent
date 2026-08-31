"""Typed, bounded recovery decisions.

The scheduler still owns state transitions and candidate lineage.  This module
only turns public evidence into a small, deterministic vocabulary so reports
can distinguish a worker crash from a protocol mistake or a bad candidate.
"""
from dataclasses import dataclass
from enum import StrEnum


class FailureClass(StrEnum):
    WORKER_CRASH = "worker_crash"
    PROTOCOL_INCOMPLETE = "protocol_incomplete"
    STARTUP_FAILURE = "startup_authentication_failure"
    SDK_TIMEOUT = "sdk_timeout"
    TIMEOUT_STALL = "timeout_stall"
    UNCOMMITTED_CHANGES = "uncommitted_changes"
    EMPTY_DIFF = "empty_diff"
    VISIBLE_VERIFICATION_FAILURE = "visible_verification_failure"
    DELIVERY_FAILURE = "delivery_failure"
    REPEATED_EQUIVALENT_FAILURE = "repeated_equivalent_failure"


class RecoveryAction(StrEnum):
    RETRY_RETAINED_CANDIDATE = "retry_retained_candidate"
    REPAIR = "repair"
    RERUN_VERIFICATION = "rerun_verification"
    ESCALATE = "escalate"
    TERMINATE = "terminate"


@dataclass(frozen=True)
class RecoveryDecision:
    failure_class: FailureClass
    action: RecoveryAction
    bounded: bool = True
    reason: str = ""


_EVENT_FAILURES = {
    "worker.exited": FailureClass.WORKER_CRASH,
    "worker.protocol_incomplete": FailureClass.PROTOCOL_INCOMPLETE,
    "worker.unclaimed": FailureClass.PROTOCOL_INCOMPLETE,
    "worker.stalled": FailureClass.TIMEOUT_STALL,
    "worker.timeout": FailureClass.TIMEOUT_STALL,
    "worker.startup_failed": FailureClass.STARTUP_FAILURE,
    "worker.sdk_timeout": FailureClass.SDK_TIMEOUT,
    "verify.failed": FailureClass.VISIBLE_VERIFICATION_FAILURE,
    "artifact.validation_failed": FailureClass.VISIBLE_VERIFICATION_FAILURE,
    "interface.validation_failed": FailureClass.VISIBLE_VERIFICATION_FAILURE,
    "delivery.failed": FailureClass.DELIVERY_FAILURE,
}


def classify_failure(event_type: str, payload: dict | None = None) -> FailureClass:
    """Normalize a durable event into a stable failure class."""
    payload = payload or {}
    explicit = payload.get("failure_class") or payload.get("category")
    if explicit:
        try:
            return FailureClass(str(explicit))
        except ValueError:
            pass
    if event_type == "verify.failed":
        cause = str(payload.get("cause") or "")
        if cause in {"uncommitted_changes", "dirty_worktree"}:
            return FailureClass.UNCOMMITTED_CHANGES
        if cause == "empty_diff":
            return FailureClass.EMPTY_DIFF
    return _EVENT_FAILURES.get(event_type, FailureClass.VISIBLE_VERIFICATION_FAILURE)


def choose_recovery(failure_class: FailureClass | str, *, retries: int,
                    max_retries: int, protocol_retries: int = 0,
                    equivalent: bool = False) -> RecoveryDecision:
    """Select one bounded action from public evidence and remaining budget."""
    failure_class = FailureClass(failure_class)
    if equivalent:
        return RecoveryDecision(failure_class, RecoveryAction.ESCALATE,
                                reason="equivalent public failure repeated")
    if failure_class in {FailureClass.STARTUP_FAILURE, FailureClass.SDK_TIMEOUT}:
        return RecoveryDecision(failure_class, RecoveryAction.TERMINATE,
                                reason="SDK startup/turn timeouts are infrastructure-owned")
    if failure_class is FailureClass.DELIVERY_FAILURE:
        return RecoveryDecision(failure_class, RecoveryAction.ESCALATE,
                                reason="delivery requires an explicit policy or human decision")
    if retries >= max_retries:
        return RecoveryDecision(failure_class, RecoveryAction.ESCALATE,
                                reason="retry budget exhausted")
    if failure_class is FailureClass.PROTOCOL_INCOMPLETE:
        if protocol_retries >= 1:
            return RecoveryDecision(failure_class, RecoveryAction.ESCALATE,
                                    reason="bounded protocol repair already attempted")
        return RecoveryDecision(failure_class, RecoveryAction.REPAIR,
                                reason="successful SDK result lacked terminal metadata")
    if failure_class in {
        FailureClass.WORKER_CRASH, FailureClass.UNCOMMITTED_CHANGES,
        FailureClass.EMPTY_DIFF,
    }:
        return RecoveryDecision(failure_class, RecoveryAction.RETRY_RETAINED_CANDIDATE,
                                reason="public worker/candidate evidence is unambiguous")
    if failure_class is FailureClass.TIMEOUT_STALL:
        return RecoveryDecision(failure_class, RecoveryAction.ESCALATE,
                                reason="watchdog evidence does not identify a safe repair")
    if failure_class is FailureClass.VISIBLE_VERIFICATION_FAILURE:
        return RecoveryDecision(failure_class, RecoveryAction.REPAIR,
                                reason="visible verification output can guide one bounded repair")
    return RecoveryDecision(failure_class, RecoveryAction.ESCALATE,
                            reason="no deterministic recovery rule matched")


def recovery_payload(decision: RecoveryDecision, *, cause_seq: int | None = None,
                     attempt_id: str | None = None, **extra) -> dict:
    payload = {
        "failure_class": decision.failure_class.value,
        "action": decision.action.value,
        "bounded": decision.bounded,
        "reason": decision.reason,
    }
    if cause_seq is not None:
        payload["cause_seq"] = cause_seq
    if attempt_id is not None:
        payload["attempt_id"] = attempt_id
    payload.update(extra)
    return payload
