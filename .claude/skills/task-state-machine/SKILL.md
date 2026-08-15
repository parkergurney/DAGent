---
name: task-state-machine
description: Task states, transitions, dependency blocking, and crash recovery for the orchestrator.
---

# Task state machine

<!-- sync:task-states -->
States: `blocked, queued, running, verifying, triage, needs_human, delivering,
delivered, failed, cancelled, dependency_blocked`. Terminal: delivered, failed,
cancelled, dependency_blocked. `needs_human` is a settled resting state for a
finite batch, but remains recoverable in daemon mode.

| From | To | Trigger |
|---|---|---|
| blocked | queued | all deps reached `delivered` |
| blocked | dependency_blocked | a required dep is terminal and cannot succeed in this run |
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

- Every exception path funnels through `triage`.
- `delivered` means artifact handed off, not merged.
- Crash recovery reconciles dead running sessions through `worker.exited`. The
  orchestrator policy has one deterministic fast path for a non-zero worker
  exit: when retry budget remains, it records `recovery.policy_applied` and
  retries through the ordinary candidate-lineage path without an LLM triage
  call. Startup/authentication failures and ambiguous failures still escalate
  through the supervisor; baseline policies keep the fast path disabled.
- Candidate SHA and dirty-worktree facts are durable before the worker lease is
  released; verification reads the durable candidate ref.
- SDK `ResultMessage` is the primary completion record. `DONE_CLAIM`, `ASK`,
  and `NO_CHANGE` are optional metadata; a completion still requires a clean
  worker exit, a committed candidate (or explicit no-change), and public
  verification. The scheduler records `completed`, `asked`,
  `protocol_incomplete`, `sdk_failure`, `worker_crash`, `timeout`, and
  `startup_failure` classifications. Protocol repair is at most one retry and
  is enabled by default; a benchmark manifest can explicitly disable it for a
  legacy fallback comparison.
<!-- /sync:task-states -->
