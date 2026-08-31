---
name: supervisor-contract
description: The supervisor's triage packet, closed action union, and deterministic enforcement rules.
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

### Packet

```python
class TriagePacket(BaseModel):
    task_id: str
    brief: str
    repo: str
    delivery_mode: Literal["pr", "local", "scout"]
    verify_cmd: str | None
    trigger: TriggerEvent
    verify_output: str | None
    event_history: list[EventRow]
    transcript_tail: str
    allowed_actions: list[ActionType]
    nudges_remaining: int
    retries_remaining: int
    yolo: bool
```

Exclusions, deliberate: no team state, no filesystem access, no evaluator-only
configuration (Harbor owns hidden checks), and no memory.

### Response

The closed response union is `nudge`, `restart`, `wait`, `escalate`, or
`abandon`; orchestrator-side enforcement computes allowed actions and caps.
Validation failure falls back visibly to human escalation.
