---
name: milestones
description: Full milestone roadmap (M0-M7), the FakeWorker scenario suite, config defaults, and open questions. Use when planning next work, checking what milestone we're on or what's left, adding a FakeWorker scenario, tuning a config default, or asked about the project's open questions.
---

# Milestones

<!-- sync:milestones -->
- **M0 — durable state and replay:** scaffold the SQLite event store, task
  graph, state machine, and invariant tests. Attempts are first-class durable
  records with lineage, timestamps, candidate/base SHAs, failure data,
  feedback, disposition, and execution contract. Exit: state is reconstructible
  after a crash and `replay(events) == tasks` is asserted green.
- **M1 — worker protocol:** establish the Claude Code session contract,
  worktree execution, hooks, token capture, mid-session messages, done/ask
  signals, and startup-failure classification. The public contract contains
  the task, working directory, visible verification, commit expectations, and
  delivery rules; it contains no hidden evaluator material.
- **M2 — recoverable core loop with FakeWorker:** implement scheduler,
  watchdog, process-group ownership, teardown/reaping, public verify gate, and
  crash reconciliation. A worker that exits hands off a persisted candidate;
  its slot and worktree are released before verification or triage. FakeWorker
  scenarios remain the deterministic regression suite.
- **M3 — real workers:** run SDK sessions on toy repositories while keeping
  infrastructure failures (authentication, SDK initialization, and backend
  failures) separate from task failures. Real workers require the caller to
  provide the outer isolation boundary; the orchestrator does not claim to
  sandbox the host.
- **M4 — event-triggered supervision:** build a closed-action supervisor,
  packet dump/replay tooling, durable interventions, and deterministic policy
  checks. Successful first attempts make zero supervisor calls. Supervision is
  entered only for stalls, asks, incomplete exits, public verification
  failures, delivery failures, or other uncertain states. The canonical
  implementation actions are `restart` (retry), `wait`, `escalate` (human),
  `abandon` (terminate), and `nudge`; repeated equivalent failures can
  deterministically escalate without another model call.
- **M5 — v2 execution and coordination:** add stateful retries that inherit
  the previous candidate SHA and preserved edits, record whether the candidate
  materially changed, and fold feedback into the next attempt. Add explicit
  dependency resolution with missing-reference and cycle validation,
  multi-dependency propagation, and `dependency_blocked` tasks that consume no
  workers, retries, verification attempts, or supervisor calls. Pool workers
  independently from verification/triage, run slow verification off the async
  event loop, and track teardown tasks through shutdown. Deliver through the
  configured git modes and record queue wait, execution, slot occupancy,
  verification, supervisor/triage time, retry gaps, peak/limit, attempts,
  verification attempts, tokens, costs, and recovery events. Sequential,
  naive-parallel, and orchestrator policies use this same scheduler and worker
  machinery; only concurrency and supervisor policy differ.
- **M6 — Harbor boundary integration:** expose policy selection, candidate patch
  export, and durable metrics to a Harbor adapter. Harbor owns outer task
  isolation, hidden evaluation, and scoring; the orchestrator returns the final
  candidate SHA and exports the declared base-to-candidate patch. Package the
  installed agent and a canary task with a separate verifier; keep scheduler
  diagnostics outside Harbor's published artifact directory.
- **M7 — eval runs + writeup.** Harbor owns task isolation, hidden evaluation,
  and scoring.

TUI: unscheduled. Tail of the events table suffices through M7. Timebox
Textual to one weekend, after M3, whenever.

## FakeWorker (build first, in M2)

A scripted subprocess impersonating a Claude Code session. Scenarios: complete
cleanly, claim done without committing, commit a visible-verification failure,
leave a dirty draft before crashing, empty diff, stall silently, ask a question,
crash mid-task, and declare an external wait. The scenario suite IS the
regression suite; fault injection is a test case, not a prayer. Never debug the
orchestrator through paid nondeterministic workers.
<!-- /sync:milestones -->

# Config defaults

<!-- sync:config-defaults -->
```
max_concurrency        = 4
max_retries            = 2
repeated_failure_threshold = 1  # equivalent descendant failures before deterministic escalation
max_nudges             = 2
stall_threshold_s      = 300      # watchdog silence before worker.stalled
wait_ceiling_s         = 1800
verify_timeout_s       = 600
transcript_tail_tokens = 3000     # revisit if escalate reasons say
                                  # "insufficient context"
model_worker           = <pinned>
model_supervisor       = <pinned>
```
<!-- /sync:config-defaults -->

# Open questions

<!-- sync:open-questions -->
- Done-claim protocol (M1 decides).
- transcript_tail sizing (ship fixed, log packet sizes, watch escalate
  reasons).
- Harbor workload selection and contamination framing for the post.
- Name.
<!-- /sync:open-questions -->
