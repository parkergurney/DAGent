"""Deterministic, orchestrator-side menu computation (see README.md): "allowed_actions computed deterministically pre-call ... an
out-of-menu response is rejected." The LLM never decides what it's allowed
to choose from -- this does, before the call is even made.
"""


def compute_allowed_actions(trigger_type: str, *, nudges_remaining: int,
                            retries_remaining: int, yolo: bool, live_session: bool) -> list[str]:
    """live_session: whether a worker process is still alive to act on --
    True only for worker.stalled and worker.asked triggers caught before
    teardown. worker.exited/verify.failed/delivery.failed always run after
    the worker has already exited, so nudge/wait are never meaningful there.

    Retries exhausted forces the menu down to escalate (+ abandon in yolo
    mode) outright: "the decision is forced, the articulation is the value."
    """
    if retries_remaining <= 0:
        return ["escalate", "abandon"] if yolo else ["escalate"]

    actions = ["restart", "escalate"]
    if live_session:
        if trigger_type == "worker.stalled":
            actions.append("wait")
        elif trigger_type == "worker.asked" and nudges_remaining > 0:
            # Nudge is only ever wired to a live channel for worker.asked --
            # fake_worker.py/sdk_worker.py block on stdin right after
            # emitting "asked", specifically for this. A silently-stalled
            # session has no such channel without a concurrent stdin-listener
            # rework of sdk_worker.py, and README.md's heuristics favor
            # wait/restart for stalls anyway, never nudge -- so it's left off
            # the menu here rather than offered and silently dropped.
            actions.append("nudge")
    if yolo:
        actions.append("abandon")
    return actions
