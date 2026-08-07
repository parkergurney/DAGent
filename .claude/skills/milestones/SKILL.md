---
name: milestones
description: Full milestone roadmap (M0-M7), the FakeWorker scenario suite, config defaults, and open questions. Use when planning next work, checking what milestone we're on or what's left, adding a FakeWorker scenario, tuning a config default, or asked about the project's open questions.
---

# Milestones

<!-- sync:milestones -->
- **M0 — skeleton:** scaffold, schema, event store, replay + invariant test in
  CI. Exit: `replay(events) == tasks` asserted green.
- **M1 — SDK spike (throwaway):** one script; spawn session in worktree,
  PostToolUse hook, token capture, mid-session injection, end detection.
  Exit: the four spike questions in §8 answered in devlog.
- **M2 — core loop, fake workers only:** scheduler, state machine,
  spawn/teardown vs FakeWorker, watchdog, verify gate CLI. Exit: all fake
  scenarios drive correct transition sequences; `kill -9` the orchestrator at
  arbitrary points → clean reconcile on restart.
- **M3 — real workers:** SDK sessions on a toy repo with 3-4 seeded issues.
- **M4 — supervisor:** packet builder, closed-enum validation, packet
  dump/replay tooling BEFORE prompt tuning; iterate heuristics against saved
  packets generated with the fake worker.
- **M5 — parallelism, DAG, delivery:** worktree pool, concurrency limits, dep
  resolution, three delivery modes. Test 10-task parallel batches with fakes.
- **M6 — benchmark harness:** runner + grading via verify-gate CLI; conditions
  (a),(b) first, then (d), then (c) last.
- **M7 — eval runs + writeup.** Budget generously; days of wall-clock.

TUI: unscheduled. Tail of the events table suffices through M7. Timebox
Textual to one weekend, after M3, whenever.

## FakeWorker (build first, in M2)

A scripted subprocess impersonating a Claude Code session. Scenarios: complete
cleanly, claim done without committing, empty diff, modify a protected test,
stall silently, ask a question, crash mid-task, declare an external wait. The
scenario suite IS the regression suite; fault injection is a test case, not a
prayer. Never debug the orchestrator through paid nondeterministic workers.
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
- SWE-bench subset selection + contamination framing for the post.
- Name.
<!-- /sync:open-questions -->
