# Agent orchestrator design

The orchestrator is a deterministic asyncio/SQLite control plane for Claude
worker sessions. Harbor supplies one outer task environment, selects a policy,
and owns hidden evaluation. The orchestrator owns scheduling, retries,
dependencies, worker supervision, candidate lineage, delivery, and durable
metrics.

## Current security model

Harbor is the supported benchmark isolation boundary. Workers inside one
Harbor trial share that trial's container resources. The orchestrator does not
provide a general-purpose OS sandbox or protect the host filesystem from a
live Claude worker. Its Git worktrees isolate concurrent edits, not workers
from the host.

Hidden tests and scoring run in Harbor's separate verifier environment, and
hidden verifier results never enter the agent environment. Visible
verification is public worker feedback only, uses agent-visible repository
state, and inherits the worker environment; benchmark use therefore requires
the same trusted outer boundary as the worker. Caller-supplied worker
environment variables may contain authentication material and are never
persisted or logged. The orchestrator does not access the macOS Keychain.
Direct live workers on the host are trusted development mode only.

## Invariants

1. Only the scheduler writes `tasks.state`; every write emits
   `task.state_changed` atomically.
2. The supervisor returns one action from a closed enum and never touches the DB.
3. `replay(events) == tasks` is asserted in CI.
4. State-change payloads carry `{from, to, cause_seq}`.
5. Stall is derived by the watchdog from event silence.
6. Retry and nudge caps are orchestrator configuration.

## State machine

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
- Startup reconciliation fences orphaned worker leases and resumes durable
  checkpoints in order: incomplete verification is rerun, a persisted
  verification result advances to delivery, and a persisted delivery result
  advances to delivered without repeating the handoff. This is process-restart
  recovery for one trusted host, not distributed failover.
<!-- /sync:task-states -->

## Storage

<!-- sync:storage-schema -->
```sql
CREATE TABLE tasks (
  id            TEXT PRIMARY KEY,        -- ULID, sortable by creation time
  title         TEXT NOT NULL,
  brief         TEXT NOT NULL,           -- the worker's prompt
  repo          TEXT NOT NULL,
  delivery_mode TEXT NOT NULL,           -- 'pr' | 'local' | 'scout'
  verify_cmd    TEXT,                    -- null for scout
  output_artifacts TEXT,                  -- public JSON declaration
  output_schema TEXT,                     -- public schema/required fields
  input_contract TEXT,                    -- dependency inputs required by node
  node_verify_cmd TEXT,                   -- optional public node gate
  repair_policy TEXT,                     -- bounded recovery policy metadata
  state         TEXT NOT NULL DEFAULT 'blocked',
  retries       INTEGER NOT NULL DEFAULT 0,
  max_retries   INTEGER NOT NULL DEFAULT 2,
  worktree      TEXT,
  session_id    TEXT,
  base_sha      TEXT,
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
  source     TEXT NOT NULL,           -- scheduler|worker|watchdog|verifier|supervisor|delivery|human|system
  type       TEXT NOT NULL,           -- dotted domain.verb, past tense
  payload    TEXT NOT NULL DEFAULT '{}',  -- FLAT json, no nesting
  session_id TEXT,
  tokens_in  INTEGER,                 -- null except on LLM-touching events
  tokens_out INTEGER,
  cost_usd   REAL
);
CREATE INDEX idx_events_task ON events(task_id, seq);
```

## Event taxonomy

```
task.created          task.state_changed      dep.satisfied       dep.blocked
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
  ~100x; tool-call counts per task are an experiment metric.
- `worker.startup_failed` fires when the worker's own connect step raises
  before any session starts. Distinct from `worker.exited` (a session that
  started and then crashed or finished without a claim).
- `verify.failed` payload includes a normalized failure signature (last
  assertion line, stripped of addresses/line numbers) so "same failure twice"
  is a cheap comparison, not vibes.
- Token counts ride on `worker.*` and `supervisor.*` events. Supervision
  overhead = `SELECT SUM(cost_usd) FROM events WHERE source='supervisor'`.
- Full logs/transcripts go to disk under `data/<task_id>/...`, referenced by
  path. Not in SQLite.
<!-- /sync:storage-schema -->

## Supervisor contract

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

<!-- /sync:supervisor-contract -->

## Verify gate

<!-- sync:verify-gate -->
The verify gate is deterministic and contains no LLM or evaluator-only logic.
Harbor owns hidden tests and scoring. The gate turns a worker's committed
candidate into public evidence, a normalized failure signature, and a patch.
Visible verification inherits the agent environment and is not a host sandbox;
benchmark use requires Harbor or another trusted outer isolation boundary.

```python
class VerifyRequest:
    task_id: str
    worktree: str
    base_sha: str
    verify_cmd: str
    timeout_s: int = 600
    rerun_on_fail: bool = True
    repo: str | None = None
    candidate_sha: str | None = None
    worker_dirty: str | None = None
    artifact_root: str | None = None

class VerifyResult:
    passed: bool
    cause: Literal[
        "tests_passed", "tests_failed", "timeout", "candidate_checkout_failed",
        "uncommitted_changes", "empty_diff",
    ]
    exit_code: int | None
    duration_s: float
    flaky: bool
    output_tail: str
    diff_stat: str
    tests_modified: list[str]
    output_path: str
    patch_path: str | None
    failure_signature: str | None
```

Execution is: dirty-worktree and empty-diff checks, patch export, materialize
the durable candidate in an internal disposable checkout when needed, run the
public command with a timeout, and rerun one failure to identify flakes.
The candidate checkout is not given evaluator-only material and is removed
afterward. Timeout cleanup kills the check's process group.

| cause | supervisor heuristic |
|---|---|
| tests_failed | restart with output; equivalent signatures escalate |
| uncommitted_changes | nudge |
| empty_diff | restart with a pointed commit/change reminder |
| timeout | inspect duration and transcript |
| candidate_checkout_failed | escalate as infrastructure failure |

Events are `verify.started`, then `verify.passed` or `verify.failed` with the
cause, duration, output/patch paths, and failure signature. Verification attempt
counts remain available to generic experiment metrics.
<!-- /sync:verify-gate -->

## Worker lifecycle and delivery

<!-- sync:worker-lifecycle -->
Worker lifecycle is implemented by the SDK worker and scheduler:

- One Agent SDK session per task, cwd = a pooled internal Git worktree.
- `worker.*` events map from hooks and structured result messages; session end
  maps to `worker.exited`.
- ResultMessage success is authoritative; the DONE_CLAIM/ASK/NO_CHANGE lines
  are optional protocol metadata. Missing metadata is recorded as
  protocol_incomplete and can receive one bounded corrective retry when the v2
  protocol flag is enabled (the production default).
- Intervention is a live stdin message for nudge, or a fresh retry with folded
  feedback after escalation. Every intervention is logged.
- The worktree pool is internal worker isolation and remains even when Harbor
  supplies the outer task container. Attempt refs preserve candidate lineage;
  pooled checkout slots remain disposable.
- The orchestrator does not provide OS-level host isolation. Real workers
  require Harbor/another trusted outer boundary or explicit trusted host
  development mode; the latter is never benchmark isolation.
- Real and fake workers share a JSON-lines protocol. The caller supplies worker
  environment variables; the launcher never reads credentials or a Keychain.
- The SDK worker's path hook rejects structured file-tool paths outside its
  assigned worktree. Harbor owns broader task isolation and hidden evaluation.
- A done claim is followed by process-group termination/reaping before the
  candidate is verified. Scheduler teardown is idempotent and closes child
  transports, releases the worktree slot, and preserves the candidate ref.
<!-- /sync:worker-lifecycle -->

<!-- sync:delivery-modes -->
Per-task `delivery_mode` remains explicit:

- `pr` — push branch and open a PR; delivered means PR open.
- `local` — approved fast-forward merge into the local default branch.
- `scout` — no push; write a report for investigation tasks.

Delivery failures (`delivery.failed`) route through supervisor triage.
<!-- /sync:delivery-modes -->

## Harbor boundary

`orchestrator.harbor.run_instruction` starts one task from an instruction and
repository path, selects `sequential`, `naive-parallel`, or `orchestrator`,
accepts caller environment variables, waits for reliable scheduler teardown,
and returns the final candidate SHA plus metrics. Real workers require the
caller to declare `external_isolation=True`; this declaration does not create
or verify a sandbox. Direct host execution is trusted development mode only.
`export_patch` exports only the declared base-to-candidate diff. Harbor
transfers that patch to its separate verifier; no hidden evaluator material or
verifier result enters the agent environment.

## Core v2 execution shape

```text
task graph
    ↓
scheduler
    ↓
worker attempts
    ↓
durable candidate state
    ↓
public verification
    ↓
event-triggered supervisor when recovery is needed
    ↓
retry, continue, escalate, or finish
    ↓
final candidate SHA
```

Sequential, naive-parallel, and orchestrator are policy selections over this
same execution machinery. The benchmark-specific hidden evaluator, scoring,
and outer filesystem isolation remain outside this flow in Harbor. The
repository provides the Python boundary (`orchestrator.harbor`), the installed
agent wrapper (`orchestrator.harbor_agent:HarborOrchestratorAgent`), an
in-container runtime, and a separate-verifier canary under
`harbor/tasks/orchestrator-canary/`. A live multi-seed comparison remains M7.

## Milestones

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

## Config defaults

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

## Open questions

<!-- sync:open-questions -->
- Done-claim protocol (M1 decides).
- transcript_tail sizing (ship fixed, log packet sizes, watch escalate
  reasons).
- Harbor workload selection and contamination framing for the post.
- Name.
<!-- /sync:open-questions -->
