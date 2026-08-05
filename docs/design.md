# Design: agent orchestration system for Claude Code

Status: M5 complete (worktree pool, dep resolution, all three delivery modes),
plus an `orchestrator` CLI (add-task/run/daemon/answer/status, docs/usage.md)
layered on top so the system is usable without hand-writing a Python script.

This is the full architecture design document, the source of truth for
architecture decisions. `CLAUDE.md` carries a trimmed copy of the
always-relevant parts (thesis, architecture overview, invariants) plus
pointers into `.claude/skills/` for topic-specific detail, so every Claude
Code session gets the core context without paying for all of it on every
task; this file remains the complete reference for deep review. Update it
when decisions change; log the change and rationale in devlog.md.

Working name TBD. "agent-orchestrator" is a placeholder.

---

## 1. Thesis

A deterministic orchestration daemon — real code, real state machine,
event-driven — that runs a team of Claude Code sessions in parallel, using LLM
judgment only at the edges (triage decisions), built natively on the Claude
Agent SDK. Benchmarked against baselines, which almost no system in this space
does.

Prior art: kunchenguid/firstmate (AGENTS.md prompt + bash toolbelt + tmux
scraping). Ideas kept from it: event-driven wake instead of polling, worktree
isolation, explicit per-project delivery modes, "delivered = PR open, merge is
the manager's call", restart-proof state on disk. Ideas rejected: pane-scraping
transport (we use SDK hooks + structured streams), LLM-in-the-control-loop for
scheduling (deterministic code), harness-agnosticism (we commit to Claude Code
and take the SDK's structured integration).

### Non-goals (v1)

- Multi-machine / multi-user. Single manager, single box.
- Container isolation. Worktree + process-group + timeout. Documented limitation.
- Chat liaison front-end. A TUI tailing the events table is the operator UI.
- Adversarial LLM reviewer in the verify gate. Slots in later as optional
  stage 5; the gate ships fully deterministic.
- LangGraph / Temporal / Celery. Workers are jobs, not graph nodes. asyncio +
  SQLite + a topological sort. "Why not X" gets a section in the writeup.

---

## 2. Architecture overview

```
 manager (TUI / CLI)
      │
      ▼
 ┌──────────────────────────────────────────────┐
 │ orchestrator daemon (python, asyncio)        │
 │                                              │
 │  scheduler ── state machine ── watchdog      │
 │      │              │                        │
 │      │        events + tasks (SQLite)        │
 │      │              │                        │
 │  supervisor ─── verify gate ─── delivery     │
 │  (one LLM call) (deterministic) (git/gh)     │
 └──────┬───────────────────────────────────────┘
        │ spawn / inject / observe (Agent SDK)
        ▼
  worker sessions, one per task, each in its own git worktree
```

Control plane is deterministic. The only LLM calls in the control plane are
single-shot supervisor invocations. Workers are full Claude Code sessions and
are the only things that write project code.

---

## 3. Core principle: event-sourced state

`events` is an append-only table of facts. `tasks.state` is a derived cache.
The scheduler is the only writer of state transitions, and every transition is
itself an event, written in the same SQLite transaction.

### Invariants (the contract — enforce in tests from M0)

1. Only the scheduler writes `tasks.state`; every write emits
   `task.state_changed` atomically with it.
2. The supervisor returns one action from a closed enum and never touches the
   database.
3. `tasks` is rebuildable from `events`: `replay(events) == tasks` is asserted
   in CI.
4. `task.state_changed` payloads carry `{from, to, cause_seq}` — the seq of the
   event that caused the transition. Full causality chain.
5. Stall is never self-reported. The watchdog derives `worker.stalled` from the
   absence of events past a threshold.
6. Retry/nudge caps are orchestrator config, never prompt suggestions.

---

## 4. Task state machine

<!-- sync:task-states -->
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
<!-- /sync:task-states -->

---

## 5. Storage schema

<!-- sync:storage-schema -->
```sql
CREATE TABLE tasks (
  id            TEXT PRIMARY KEY,        -- ULID, sortable by creation time
  title         TEXT NOT NULL,
  brief         TEXT NOT NULL,           -- the worker's prompt
  repo          TEXT NOT NULL,
  delivery_mode TEXT NOT NULL,           -- 'pr' | 'local' | 'scout'
  verify_cmd    TEXT,                    -- null for scout
  state         TEXT NOT NULL DEFAULT 'blocked',
  retries       INTEGER NOT NULL DEFAULT 0,
  max_retries   INTEGER NOT NULL DEFAULT 2,
  worktree      TEXT,
  session_id    TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE task_deps (
  task_id    TEXT NOT NULL REFERENCES tasks(id),
  depends_on TEXT NOT NULL REFERENCES tasks(id),
  PRIMARY KEY (task_id, depends_on)
);

CREATE TABLE events (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,  -- global monotonic order
  ts         TEXT NOT NULL,
  task_id    TEXT,                    -- null = team-level event
  source     TEXT NOT NULL,           -- scheduler|worker|watchdog|verifier|supervisor|delivery|human
  type       TEXT NOT NULL,           -- dotted domain.verb, past tense
  payload    TEXT NOT NULL DEFAULT '{}',  -- FLAT json, no nesting
  session_id TEXT,
  tokens_in  INTEGER,                 -- null except on LLM-touching events
  tokens_out INTEGER,
  cost_usd   REAL
);
CREATE INDEX idx_events_task ON events(task_id, seq);
```

### Event taxonomy

```
task.created          task.state_changed      dep.satisfied
worker.spawned        worker.tool_used        worker.messaged
worker.asked          worker.done_claimed     worker.exited
worker.stalled        (watchdog only)      worker.startup_failed
verify.started        verify.passed           verify.failed
supervisor.invoked    supervisor.acted        supervisor.failed
delivery.started      delivery.pr_opened      delivery.merged_local
delivery.report_written                       delivery.failed
human.messaged        human.approved          human.cancelled
system.started        system.reconciled
```

- `worker.tool_used` comes from a PostToolUse hook; log EVERY call but keep the
  payload minimal (tool name, target, duration_ms). Highest-volume event by
  ~100x; tool-call counts per task are a benchmark metric.
- `worker.startup_failed` fires when the worker's own connect step raises
  before any session starts — currently only `failIfUnavailable`'s hard fail
  when the OS-level Bash sandbox can't start (docs/design.md section 8).
  Distinct from `worker.exited` (a session that started and then crashed or
  finished without a claim) so triage/operators can tell "never sandboxed"
  from "sandboxed session died."
- `verify.failed` payload includes a normalized failure signature (last
  assertion line, stripped of addresses/line numbers) so "same failure twice"
  is a cheap comparison, not vibes.
- Token counts ride on `worker.*` and `supervisor.*` events. Supervision
  overhead = `SELECT SUM(cost_usd) FROM events WHERE source='supervisor'`.
- Full logs/transcripts go to disk under `data/<task_id>/...`, referenced by
  path. Not in SQLite.
<!-- /sync:storage-schema -->

---

## 6. Supervisor contract

<!-- sync:supervisor-contract -->
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

### Response (closed union)

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

### Enforcement (all orchestrator-side)

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

### Prompt heuristics (iterate against saved packets)

- `worker.asked`: answer via nudge only if the answer is unambiguously in the
  brief; else escalate. Never guess on the manager's behalf.
- `worker.stalled`: declared external wait → `wait`. Transcript shows repeated
  similar tool calls (spinning) → `restart`; a confused session rarely
  un-confuses.
- `verify.failed`: restart with failure as feedback, unless history shows the
  same failure signature twice → escalate (the brief is the problem).
<!-- /sync:supervisor-contract -->

---

## 7. Verify gate contract

<!-- sync:verify-gate -->
Boring, deterministic, paranoid. No LLM anywhere in it. Converts "done" claims
into evidence; its failure taxonomy is what makes the supervisor smart.

Also a standalone CLI (`verify-gate --task <id> --json`) so the benchmark
harness grades ALL conditions — including non-orchestrated baselines — with
identical machinery.

```python
class VerifyRequest(BaseModel):
    task_id: str
    worktree: str
    base_sha: str
    verify_cmd: str                 # visible to the worker
    hidden_cmd: str | None          # NOT in the brief; benchmark/paranoia
    setup_cmd: str | None           # cached per repo
    timeout_s: int = 600
    protected_paths: list[str]      # opt-in globs the worker may not modify (existing files only)
    rerun_on_fail: bool = True      # flake detection

class VerifyResult(BaseModel):
    passed: bool
    cause: Literal[
        "tests_passed",
        "tests_failed", "hidden_tests_failed",
        "timeout", "setup_failed",
        "uncommitted_changes", "empty_diff",
        "protected_path_modified",
        "baseline_broken",
    ]
    exit_code: int | None
    duration_s: float
    flaky: bool                     # failed once, passed on rerun
    output_tail: str                # ~2k chars, feeds the supervisor packet
    diff_stat: str
    tests_modified: list[str]
    output_path: str                # full logs on disk
    patch_path: str | None          # saved review patch for committed diffs
```

### Execution order (cheapest first)

1. **Preflight (git, ms):** dirty worktree → `uncommitted_changes` (supervisor
   nudges "commit and re-claim"). Empty diff on a ship task → `empty_diff`
   (hallucinated completion). Diff modifies (edits, deletes, or renames) a
   file that already existed at base_sha and matches explicit
   `protected_paths` → `protected_path_modified` — the anti-gaming check for
   benchmark/hidden/instructor-owned checks an agent must not rewrite. New
   files under protected_paths are exempt — a brand-new test is a
   contribution, not gaming. Default protected_paths is empty: visible project
   tests are normal feature-work surface and often need to change with the
   implementation.
2. **Baseline (cached on (repo, base_sha, verify_cmd, setup_cmd)):** run setup+verify on
   base_sha itself. Baseline red → `baseline_broken` → escalate, never retry.
   No number of retries fixes a repo whose tests were already failing; without
   this check a flaky upstream test burns the whole retry budget for nothing.
3. **The run:** setup_cmd (own cause — env problem ≠ code problem), then
   verify_cmd under timeout, worker session inactive. Kill the process GROUP
   on timeout; test runners orphan children.
4. **Flake protocol + hidden check:** fail → rerun once. Fail-fail →
   `tests_failed`. Fail-pass → PASSED with `flaky=true` (don't burn retries on
   nondeterminism the worker didn't cause) — but log loudly; flake rate per
   repo is a benchmark covariate and a finding. If visible passed, run
   hidden_cmd. `hidden_tests_failed` restart feedback must NOT leak hidden
   output — say the change didn't hold up under additional checks, without
   revealing which. Otherwise hidden tests train the worker to overfit them.

### Cause → supervisor heuristic

| cause | heuristic |
|---|---|
| tests_failed | restart w/ output_tail; same signature twice → escalate |
| hidden_tests_failed | restart w/ non-revealing feedback; twice → escalate |
| uncommitted_changes | nudge |
| empty_diff | restart, pointed "you changed nothing" |
| protected_path_modified | restart/escalate "revert X or request an explicit protected-path exception"; also a benchmark metric (gaming attempts per condition) |
| baseline_broken, setup_failed | escalate, never retry |
| timeout | ambiguous — supervisor reads duration vs baseline + transcript |

Events: `verify.started`, then passed/failed with payload
`{cause, exit_code, duration_s, flaky, diff_stat, tests_modified,
output_path, patch_path}`.
<!-- /sync:verify-gate -->

---

## 8. Worker lifecycle (to be detailed after M1 spike)

<!-- sync:worker-lifecycle -->
Sketch — the SDK spike (M1) answers the open questions before this section
gets fully specced:

- One Agent SDK session per task, cwd = a pooled git worktree, per-task
  permission policy.
- `worker.*` events map from: PostToolUse hook → `worker.tool_used`; result
  messages → `worker.messaged` / `worker.asked` / `worker.done_claimed`;
  session end → `worker.exited`.
- Done-claim detection protocol: TBD in M1 (likely a required final structured
  message or sentinel; do not rely on parsing prose).
- Intervention = injecting a message into the live session (supervisor
  nudge) or, once escalation has already torn the session down, requeuing
  with the intervention folded into the brief for a fresh one (manager
  answer via `orchestrator answer`, docs/usage.md). Logged as events either
  way; the orchestrator always knows a human intervened.
- Worktree pool: raw `git worktree`, ~50 lines, no treehouse dependency.
- Spike questions: does mid-session message injection work as assumed? cost
  granularity per message or per session? what does "done" look like in the
  stream? does PostToolUse fire for subagent tool calls (parent_tool_use_id)?
- Worktree escape is a two-layer defense, not one. The PreToolUse hook
  (`_path_escapes_worktree` in sdk_worker.py) denies escaping paths for
  structured file tools (Read/Edit/Write) only — it never inspected Bash,
  and batch01 dogfooding hit that gap twice: a worker ran `sed -i` against
  an absolute path in the main checkout (`~/Development/sqlite-utils`),
  dirtying it and causing unrelated tasks' `local` delivery to fail with
  `dirty_tree`. Claude Code's native OS-level Bash sandbox (Seatbelt on
  macOS, bubblewrap on Linux, v2.0.24+) closes that gap by restricting the
  Bash tool's *process*, not its declared intent — enforced by the kernel,
  so it holds regardless of what the model claims the command does. Workers
  set `sandbox.enabled=True` on `ClaudeAgentOptions`; the default write
  policy (cwd + subdirs + session temp dir) already matches worker cwd =
  worktree, so no custom path rules are needed. `sandbox.
  allowUnsandboxedCommands=False` makes the model's `dangerouslyDisableSandbox`
  escape hatch a no-op — commands can no longer opt back out. `sandbox.
  failIfUnavailable=True` turns a missing dependency or unsupported platform
  into a hard connect failure instead of the CLI's default warn-and-run-
  unsandboxed — sdk_worker.py catches that failure, emits a `worker.
  startup_failed` event (stderr is discarded by the spawning process, so
  this is the only way it reaches the operator), and exits rather than
  proceeding unsandboxed.
- Network denial does NOT come from `permission_mode`. Workers used
  `permission_mode="bypassPermissions"` pre-sandbox to avoid hanging on
  approval prompts headless sessions can't answer; with the sandbox in
  place that flag turned out to auto-grant the sandbox's own network-domain
  approval too — the SDK exposes "does this Bash command get to reach a new
  host" as a synthetic `SandboxNetworkAccess` tool call routed through the
  same decision pipeline as any other tool, and `bypassPermissions`
  auto-approves that pipeline wholesale. `sandbox.network.strictAllowlist=
  True` alone did nothing against it — verified live: a sandboxed `curl` to
  an unlisted host returned a real HTTP response under `bypassPermissions`
  despite `strictAllowlist`. Workers now pass `can_use_tool=_can_use_tool`
  instead of `permission_mode`: that callback denies exactly
  `tool_name == "SandboxNetworkAccess"` and allows everything else, which
  keeps sessions headless (no hang on a plain in-worktree Read/Edit/Write)
  while this codebase, not the CLI's blanket bypass, owns the one decision
  that has to stay a real deny. Re-verified live after the fix: the same
  `curl` now fails with a proxy-level 403 (`CONNECT tunnel failed`), fast
  and clean, no hang. `strictAllowlist` is left set as defense in depth in
  case `can_use_tool` isn't consulted in some future CLI path, but it is
  not what's doing the denying today. Workers don't need network anyway —
  verify/setup_cmd run outside the session, in the gate.
- Both denials — filesystem escape and network — were spot-checked against
  a negative control, not just observed once: disabling the mechanism under
  test (dropping `sandbox=` entirely for the filesystem case; forcing
  `_path_escapes_worktree` to always return `False` for the FakeWorker
  scenario) reproduces the original batch01 failure (the write succeeds),
  confirming the passing case denies for the right reason and isn't an
  artifact of the test environment.
<!-- /sync:worker-lifecycle -->

## 9. Delivery modes

<!-- sync:delivery-modes -->
Per-task `delivery_mode`, firstmate-style, explicit:

- `pr`: push branch, open PR via gh. Delivered = PR open. Merge is the
  manager's call; merge tracking is an event. The delivery payload carries
  `{url, branch, commit_sha}` so review does not depend on a live worktree.
- `local`: approved fast-forward merge into the local default branch. The
  delivery payload carries `{before_sha, after_sha, commit_sha}` so the
  manager can review with `git diff before_sha..after_sha` after pooled
  worktrees have been torn down.
- `scout`: no push ever; report written to `data/<task_id>/report.md`.

Delivery failures (push rejected, conflict) → `delivery.failed` → triage.
<!-- /sync:delivery-modes -->

---

## 10. Benchmark plan

<!-- sync:benchmark-plan -->
Conditions (identical model, pinned version in config day one):

- (a) single Claude Code session, tasks sequential
- (b) naive parallel: N independent headless sessions, no supervision
- (c) firstmate
- (d) this system

All graded by the verify-gate CLI. (a)+(b) run first: they calibrate task
difficulty (if naive-parallel resolves 90%, the suite is too easy and the
comparison is dead) and exercise the harness before (d) exists.

Workload: multi-task batches. Both of: a subset of SWE-bench Verified grouped
by repo (parallel batches force worktree contention; free test-based grading;
note contamination caveats honestly) and 2-3 seeded repos with 10-20
hand-written issues, each with a hidden verification test.

Metrics (all SQL over events):

- verified resolution rate (tests pass, not self-report)
- wall-clock per batch; throughput (tasks/hour)
- total cost, split worker vs supervision overhead
- human interventions count; escalation precision (were interruptions
  warranted; did manager pick `recommended`)
- fault recovery: kill a worker mid-task every run; does the system recover
  without losing the task
- gaming attempts (`protected_path_modified` count) per condition
- flake rate per repo (covariate + finding)

Rigor: 3-5 seeds per condition, mean + spread, publish harness, configs, and
full transcripts. Estimate API cost before launching eval runs.

Ablation slots (cheap, thanks to architecture): supervisor model swap
(Haiku vs Sonnet) via packet replay — action agreement rate + cost delta;
supervision context serialization (JSON vs TOON) if overhead is non-trivial.

Scope levers if the calendar slips, in order: cut condition (c), cut the DAG,
shrink the task suite. NEVER cut seeds-per-condition.
<!-- /sync:benchmark-plan -->

---

## 11. Milestones

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

### FakeWorker (build first, in M2)

A scripted subprocess impersonating a Claude Code session. Scenarios: complete
cleanly, claim done without committing, empty diff, modify a protected test,
stall silently, ask a question, crash mid-task, declare an external wait. The
scenario suite IS the regression suite; fault injection is a test case, not a
prayer. Never debug the orchestrator through paid nondeterministic workers.
<!-- /sync:milestones -->

---

## 12. Config defaults

<!-- sync:config-defaults -->
```
max_concurrency        = 4
max_retries            = 2
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

## 13. Open questions

<!-- sync:open-questions -->
- Done-claim protocol (M1 decides).
- transcript_tail sizing (ship fixed, log packet sizes, watch escalate
  reasons).
- SWE-bench subset selection + contamination framing for the post.
- Name.
<!-- /sync:open-questions -->

## 14. Devlog discipline

A few lines per session in docs/devlog.md: what was decided, what surprised
you, what the agent building this nailed or fumbled. The post is 70% written
if this is kept; a slog if reconstructed in week eight.
