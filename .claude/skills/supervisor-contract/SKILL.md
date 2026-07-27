---
name: supervisor-contract
description: The supervisor's TriagePacket/action-response contract, enforcement rules, and prompt heuristics. Use when touching supervisor.py or triage-invocation code, writing or tuning the supervisor prompt, or asked how nudge/restart/wait/escalate/abandon decisions are made.
---

# Supervisor contract

One function, one contract: packet in, action out, no side effects. A single
Messages API call — no tools, no session, no memory. Stateless and therefore
replayable: every packet is dumped to disk; prompts and models can be re-run
against saved packets offline.

Invocation points — exactly the five triage-entry events: `worker.stalled`,
`worker.asked`, `worker.exited` (no done-claim), `verify.failed`,
`delivery.failed`. Happy-path completions never touch the supervisor; the
verify gate is the judge of "done."

## Packet

```python
class TriagePacket(BaseModel):
    task_id: str
    brief: str
    repo: str
    delivery_mode: Literal["pr", "local", "scout"]
    verify_cmd: str | None

    trigger: TriggerEvent           # the cause event, verbatim
    verify_output: str | None       # tail, only on verify.failed

    event_history: list[EventRow]   # this task's events, compacted
    transcript_tail: str            # last ~3k tokens of the worker session

    allowed_actions: list[ActionType]   # computed by orchestrator
    nudges_remaining: int
    retries_remaining: int
    yolo: bool
```

Exclusions, deliberate: no team state (per-task judge; digest batching is a
presentation concern), no filesystem access (if the packet isn't enough,
escalate or restart — investigation belongs to workers), no memory (but prior
`supervisor.acted` events are IN event_history, so it sees its own past
actions on this task for free).

event_history compaction: collapse runs of `worker.tool_used` into counts
("47 tool calls: 31 Read, 9 Edit, 7 Bash over 14 min"); keep state changes,
questions, supervisor actions verbatim. Target: a few hundred tokens.

## Response (closed union)

```python
class Nudge(BaseModel):
    action: Literal["nudge"]
    message: str                    # injected into the live session
    reason: str

class Restart(BaseModel):
    action: Literal["restart"]
    feedback: str | None            # appended to brief for fresh session
    reason: str

class Wait(BaseModel):
    action: Literal["wait"]
    seconds: int                    # orchestrator-capped, max 1800
    reason: str

class Escalate(BaseModel):
    action: Literal["escalate"]
    summary: str                    # 2-3 sentences, manager-facing
    question: str
    options: list[str]              # 2-4 concrete choices
    recommended: int | None         # index; benchmark metric: how often
    reason: str                     #   manager picks the recommendation

class Abandon(BaseModel):
    action: Literal["abandon"]      # yolo mode only
    reason: str
```

Notes: `restart` with feedback subsumes retry-with-feedback. `wait` exists for
declared external waits (CI) — re-arms the watchdog with a longer deadline;
without it the only options for a healthy-but-waiting task are a pointless
nudge or a destructive restart. `Escalate` is deliberately the fattest schema:
it renders directly in the manager UI.

## Enforcement (all orchestrator-side)

1. `allowed_actions` computed deterministically pre-call: no nudges left drops
   `nudge`; no retries left drops `restart`; `abandon` only when yolo; `wait`
   only on stall triggers. Out-of-menu response is rejected.
2. When retries are exhausted, still invoke with
   `allowed_actions=["escalate"]` — the decision is forced, the articulation
   (summary/question/options) is the value.
3. Validation failure → one re-ask with the error appended → fallback to a
   synthetic Escalate ("supervisor failed to produce a valid action") and a
   `supervisor.failed` event. Fallback is ALWAYS escalate. When the judgment
   layer breaks, degrade to the human, visibly.
4. Caps (max_nudges=2, max_retries=2, wait ceiling 30 min) live in config.

## Prompt heuristics (iterate against saved packets)

- `worker.asked`: answer via nudge only if the answer is unambiguously in the
  brief; else escalate. Never guess on the manager's behalf.
- `worker.stalled`: declared external wait → `wait`. Transcript shows repeated
  similar tool calls (spinning) → `restart`; a confused session rarely
  un-confuses.
- `verify.failed`: restart with failure as feedback, unless history shows the
  same failure signature twice → escalate (the brief is the problem).
