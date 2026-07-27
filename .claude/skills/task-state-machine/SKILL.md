---
name: task-state-machine
description: Full task state machine reference for this orchestrator - states, transition table, and design notes on triage funneling, delivered semantics, and crash recovery. Use when touching scheduler or state-machine code, working with task states (blocked/queued/running/verifying/triage/needs_human/delivering/delivered/failed/cancelled), or asked about task transitions or crash recovery behavior.
---

# Task state machine

States: `blocked, queued, running, verifying, triage, needs_human, delivering,
delivered, failed, cancelled`. Terminal: delivered, failed, cancelled.

| From | To | Trigger |
|---|---|---|
| blocked | queued | all deps reached `delivered` |
| blocked | cancelled | a dep failed (default policy; `+yolo` may auto-fail) |
| queued | running | scheduler acquired slot + worktree, spawned session |
| running | verifying | `worker.done_claimed` |
| running | triage | `worker.stalled` \| `worker.asked` \| `worker.exited` without done-claim |
| verifying | delivering | `verify.passed` |
| verifying | triage | `verify.failed` |
| triage | running | supervisor `nudge` (same session) or `restart` (new session, retries += 1) |
| triage | needs_human | supervisor `escalate`, or retries exhausted |
| triage | failed | supervisor `abandon` — yolo mode only |
| needs_human | running | manager's answer injected into session |
| needs_human | queued | manager answered (`orchestrator answer`); requeued with the answer folded into the brief |
| needs_human | delivering | manager overrides a failed verification |
| delivering | delivered | PR opened / local ff-merge done / scout report written |
| delivering | triage | push rejected, merge conflict |
| any | cancelled | manager kills the task |

Design notes:

- Every exception path funnels through `triage`: stall, crash, failed verify,
  failed delivery become the same shape of problem. The supervisor is one
  function with one prompt, not five special cases.
- `delivered` means artifact handed off (PR open), not merged. Post-delivery
  merge tracking is an event, not a state.
- Crash recovery is a reconciliation pass at startup: for every task in
  `running`, check whether `session_id` is a live session; dead ones get a
  synthetic `worker.exited` event and route through triage like any other
  crash. No special recovery code path.
