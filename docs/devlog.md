# Devlog

A few lines per working session. What was decided, what surprised you, what
the agent nailed or fumbled. This file writes the Substack post.

## 2026-07-18
- Consolidated design into docs/design.md (state machine, event schema,
  supervisor contract, verify gate contract, benchmark plan, milestones).
- Repo scaffolded. Next: M0 - schema + event store + replay invariant test.

## 2026-07-19
- M0 landed: storage layer only, stdlib-only.
  store/db.py (connect + schema bootstrap, WAL, foreign_keys),
  store/events.py (append_event, create_task, transition, replay, vendored
  ULID, state machine), config.py, and the invariant test suite.
- Trimmed pyproject to M0 reality: dropped claude-agent-sdk / pydantic /
  python-ulid / pytest-asyncio and the not-yet-existing console scripts.
  Runtime deps come back with the milestones that need them. Bumped
  requires-python to 3.12.
- ULID is ~3 lines vendored (Crockford base32 over a 128-bit int), not a
  dependency. 1000-id uniqueness check in the suite.
- Design call for replay fidelity: the write path records into event payloads
  exactly what replay needs to reconstruct - task.created carries the full
  static task definition, task.state_changed carries {from,to,cause_seq} plus
  any mutated columns (session_id, worktree, retries). Both the row write and
  its event share a single timestamp so updated_at matches under replay.
  That is what makes replay(events) == tasks bit-for-bit, not just
  structurally.
- transition() is the sole state writer and validates against the section 4
  edge table; "any -> cancelled" is a rule, not 9 table rows. It also refuses
  to set columns outside a small allowlist, so it can never smuggle a
  non-state write past the event log.
- Surprise: nothing hairy. The one thing worth remembering is the shared-ts
  trick above; without it updated_at drifts by microseconds and the invariant
  fails intermittently.
- Kept the empty future-milestone package dirs (verify/, scheduler/, ...) since
  they match the README layout; did not add code to them. Exit criterion green:
  8 tests pass, replay(events) == tasks asserted.

## 2026-07-19 - M1: SDK spike

Spike script at spike/m1_spike.py (throwaway, keep for reference); full event log in spike/m1_spike_log.jsonl.
Three runs against real claude-haiku-4-5 sessions in a throwaway git worktree, ~$0.06-0.09 per full run.
SDK: claude-agent-sdk 0.2.123 (Python), driving Claude Code CLI 2.1.215.

The four section 8 questions, answered:

1. Mid-session injection: YES.
   Calling client.query() on a live ClaudeSDKClient while a turn is running delivers the message into the running turn.
   The worker reacted within ~1s of the in-flight tool call completing, folded the nudge into its plan, and produced a single ResultMessage (no second turn; 60s of post-result silence confirmed nothing was queued).
   Supervisor nudge = client.query() on the held session handle. No workaround needed.

2. Cost granularity: tokens per message, dollars per turn-run.
   Every AssistantMessage carries a usage dict (input/output tokens, cache_creation, cache_read, inference_geo), including subagent messages.
   cost_usd exists only on ResultMessage (total_cost_usd, plus a per-model model_usage breakdown and a per-iteration usage list).
   So worker.* events can carry token counts per event; cost lands once per completed turn.

3. Done in the stream: ResultMessage.
   Shape: {subtype: "success"|..., is_error, num_turns, stop_reason: "end_turn", result: <final assistant text verbatim>, usage, total_cost_usd, session_id, duration_ms}.
   A sentinel final line (we used "DONE_CLAIM: <word>") surfaces verbatim in ResultMessage.result, so done-claim detection can be a cheap parse of result rather than prose-scraping mid-stream.
   Recommendation for section 8: require a sentinel line in the brief and parse ResultMessage.result; worker.exited maps to the ResultMessage itself.

4. PostToolUse for subagent tool calls: YES.
   The hook fires for tools run inside Task-spawned subagents, with agent_id set (absent on main-thread calls).
   Subagent AssistantMessages also carry parent_tool_use_id pointing at the spawning Agent tool_use id.
   Full attribution of worker.tool_used to main thread vs subagent is free.

Surprises:

- The worktree cwd is NOT a boundary.
  Haiku workers wrote answer.txt to $HOME in two runs and foo.txt to /tmp in another, despite cwd being set to the worktree.
  Consequences: the brief must pin the absolute worktree path; consider a PreToolUse hook that denies writes outside the worktree; the verify gate's empty_diff check would have caught all three cases.
- One Write in run 2 produced no PostToolUse event while runs 1 and 3 had full hook coverage.
  Treat the hook stream as advisory rather than guaranteed-complete; the watchdog already derives stall from absence of events, which is the right posture.
- PostToolUseFailure is a separate hook event and must be mapped too, or failed tool calls vanish from the event log.
- SystemMessage subtypes (task_started, task_progress, task_notification, thinking_tokens) are free extra liveness signals for the watchdog.
- HookMatcher with no matcher string matches all tools; hook callbacks get cwd, session_id, transcript_path, duration_ms in the payload, which covers everything the worker.tool_used payload needs.

## 2026-07-20 - M2: core loop, FakeWorker, verify gate

Landed the whole M2 slice in one pass: scheduler/core.py + reconcile.py,
worker/fake_worker.py (scripted subprocess) + fake.py (spawn) + worktree.py,
verify/gate.py + cli.py, delivery/__init__.py (pr | local | scout). Still
zero runtime dependencies -- asyncio + subprocess + sqlite3 covers all of it,
including the verify-gate console script.

Design calls made that design.md didn't pin down:

- No supervisor exists yet (that's M4). Every triage entry resolves straight
  to needs_human, deterministically, via a tiny _resolve_triage() that uses
  the triage-entry state_changed event's own seq as cause_seq. This isn't a
  guess at M4's heuristics -- it's the honest answer for "no judgment layer,"
  and it's what makes all eight FakeWorker scenarios end in one deterministic
  state apiece instead of needing invented restart/nudge logic I'd just have
  to throw away in M4.
- Added tasks.base_sha (nullable TEXT), set once at spawn alongside worktree
  and session_id via the same transition() call. Verify's diff/baseline logic
  needs a base commit to diff against and none of the M0 schema had one.
  Flows through the existing _UPDATABLE allowlist and replay() unchanged --
  no new privileged write path.
- FakeWorker speaks the same wire protocol (JSON lines: tool_used, messaged,
  asked, done_claimed) a real SDK worker will map onto in M3, and it mutates
  a real git worktree per scenario so the (real, unmocked) verify gate derives
  causes from actual git state instead of FakeWorker asserting its own.
- One throwaway `git worktree` per task, created and removed each run; the
  pooled/reused version design.md defers to M5 is exactly that, deferred.
- Baseline runs are cached to disk on (repo, base_sha, verify_cmd) as the
  design calls for, in a scratch worktree that's removed after.

Surprise: the fake_worker.py subprocess couldn't import `orchestrator` at
all on first run -- pytest's `pythonpath` ini setting only reaches the test
process, not children it spawns. Fixed by having spawn_fake_worker compute
src/ from its own file path and inject it via PYTHONPATH explicitly. Will
matter again for M3's real SDK sessions if they ever shell out to anything
under this package.

Also noticed: docs/design.md and design.md are gitignored by the `design.md`
pattern in .gitignore (no leading slash, so it matches at any depth) -- only
CLAUDE.md is actually tracked, and the two design.md copies aren't symlinks
to it despite the header comment claiming so, they're independent files that
have to be kept in sync by hand. Left alone for now (out of scope for M2),
but worth fixing before this drifts.

Exit criteria: 27 tests green, including the 8-scenario FakeWorker suite
(tests/scenarios/) driving every fault-injection path to its correct state,
and a kill -9 simulation (tests/test_reconcile.py) that closes a db
connection mid-'running', reopens it fresh, and confirms reconcile() +
Scheduler.run_until_settled() route the orphaned task through triage to
needs_human with no special-cased recovery code.

## 2026-07-20 - M3: real Agent SDK workers

Design call that made this small: don't touch the scheduler's worker
abstraction at all. worker/sdk_worker.py runs the Agent SDK inside its own
dedicated subprocess and speaks the *exact* wire protocol fake_worker.py
already speaks (JSON lines: tool_used, messaged, asked, done_claimed) --
so spawn_sdk_worker(task, worktree) -> Process is a drop-in for
spawn_fake_worker, and scheduler/core.py needed exactly two changes: thread
`model` through to spawn_worker, and surface tokens_in/tokens_out/cost_usd
from event payloads into their dedicated event columns (design.md section 5).
No new abstraction layer, no rewrite of _watch()/_teardown()/reconcile() --
real sessions still have real OS pids, so reconcile()'s liveness check needed
zero changes either.

Sentinel protocol, extended: the M1 spike validated DONE_CLAIM parsed from
ResultMessage.result. Added the same idea for "blocked on a human decision":
an ASK: sentinel line, since a headless SDK session has no interactive prompt
to ask through. Absence of either sentinel -> exit unclaimed, same as
fake_worker's crash scenario -- one parser (_parse_terminal), three outcomes,
all unit-testable without touching the network (tests/test_sdk_worker.py).

Closed the gap the spike flagged ("the worktree cwd is NOT a sandbox
boundary" -- Haiku wrote outside cwd in two of three spike runs): a
PreToolUse hook denies any file_path tool call that resolves outside the
worktree. Bash-based writes aren't covered (would need shell parsing, not
worth it) -- verify gate's uncommitted_changes/empty_diff checks are the
backstop there, per the spike's own recommendation.

Real findings from running this live against claude-haiku-4-5 (three seeded
issues -- add a function, fix a seeded bug, edit a doc -- delivery_mode=local,
~$0.10 total across two runs):

- First live run of a single greenfield "add math_utils.py" task hit
  baseline_broken, not tests_failed. Cause: I'd written verify_cmd as a
  task-specific one-liner assertion, which by construction fails at base_sha
  (the file doesn't exist yet) -- that's not a broken baseline, that's a
  mismatch between the task shape and the gate's model. design.md's
  verify_cmd is meant to be a stable, repo-wide suite that already passes at
  base; task-specific correctness is hidden_cmd's job (benchmark-only, M6).
  Fixed by using verify_cmd="true" for the toy repo (no real suite yet) and
  checking task-specific correctness myself, post-delivery, outside the gate.
  Not a bug in the gate -- a reminder that toy repos for this system need a
  real (if trivial) verify_cmd, not an ad hoc per-task check.
- Across 6 seeded-issue runs (two live runs x 3 tasks), 5 delivered and 1
  landed in needs_human -- a haiku session that didn't end its turn with the
  exact DONE_CLAIM line. This is not an orchestrator bug; it's the failure
  mode M2's no-supervisor policy exists to catch (unclaimed exit -> triage ->
  needs_human), and it's why design.md section 1 says real workers don't
  belong in the deterministic test gate. tests/integration/test_sdk_worker_live.py
  is opt-in (ORCH_LIVE_SDK_TESTS=1) and asserts >=2/3 delivered plus "every
  outcome is a valid resting state," not 3/3 -- asserting 3/3 would make the
  test flaky for a reason that has nothing to do with orchestrator
  correctness.
- Cosmetic-only: real SDK worker subprocesses occasionally trigger
  "Exception ignored in: BaseSubprocessTransport.__del__ ... Event loop is
  closed" at interpreter shutdown, even after explicitly closing
  proc._transport in _teardown() (added anyway -- correct hygiene regardless).
  FakeWorker never triggers it. Best guess: the Agent SDK's own grandchild
  `claude` CLI process holds an inherited copy of a pipe fd slightly past
  sdk_worker.py's own exit. Doesn't affect task state, doesn't fail tests,
  didn't chase further given each repro costs real API calls.

Added the one real runtime dependency: claude-agent-sdk (already the version
the M1 spike used, 0.2.123). Everything else through M3 is still stdlib.

## 2026-07-20 - M4: supervisor

Built the full section-6 contract: supervisor/schema.py (TriagePacket +
Nudge/Restart/Wait/Escalate/Abandon as pydantic models -- design.md's own
code blocks specify pydantic for these, unlike verify gate's dataclasses, so
that's the one place pydantic earns its keep as a real dependency),
supervisor/actions.py (compute_allowed_actions, orchestrator-side and
LLM-blind), supervisor/packet.py (build_packet: compaction, verify_output
extraction, nudges_remaining derived from counting supervisor.acted events
rather than a new schema column -- no tasks table change needed this
milestone), supervisor/llm.py (invoke_supervisor: claude_agent_sdk.query()
in single-shot no-tools mode, since this environment authenticates through
the `claude` CLI and has no ANTHROPIC_API_KEY for a raw Messages API call;
validate -> one re-ask -> fallback Escalate + supervisor.failed, always),
supervisor/replay.py (the dump/replay CLI the milestone note calls for
building before any prompt tuning).

Scheduler rework was the bulk of the work, not the contract itself. Two
things forced it:

- The async LLM call means triage handling can no longer assume no other
  coroutine interleaves (M2's whole "no await between check and write, so no
  lock needed" argument breaks once there's a real await in the middle).
  Fixed by making the state column itself the coordination mechanism:
  _handle_triage's first move is always the synchronous transition into
  'triage', committed before anything is awaited, so the watchdog's own
  "is this task still running?" check (also synchronous, no await) can never
  race it -- once a task leaves 'running', every other caller just sees that
  and backs off. No lock, no flag, the event-sourced state IS the lock.
- nudge and wait need a session to still be alive when the supervisor
  decides, but the old code killed the process immediately on stall
  detection and immediately after "asked". Restructured _watch()'s asked
  branch and the watchdog's stall path to invoke the supervisor BEFORE
  deciding whether to tear down, with _handle_triage returning a bool the
  caller uses to either keep reading the same process's stdout (nudge/wait)
  or tear down (restart/escalate/abandon).
- Scope cut, deliberate: nudge only ever gets a live channel for
  worker.asked. fake_worker.py/sdk_worker.py block on stdin specifically
  after emitting "asked"; a silently-stalled session has no such channel
  without giving sdk_worker.py a concurrent stdin listener running
  alongside receive_response(), and design.md's own heuristics never
  recommend nudge for a stall anyway (wait or restart, always). Documented
  in supervisor/actions.py rather than silently dropped.

Real bug the scripted-supervisor tests caught before any live call did:
restart reusing the same task_id hit "branch already exists" on the second
`git worktree add -b` -- removing a worktree doesn't delete the branch it
was on. create_worktree now deletes the branch first, so a restart starts
genuinely fresh from base_branch rather than resuming a possibly-broken
prior attempt. Would never have shown up in the FakeWorker suite alone,
since nothing in M2/M3 ever relaunched a task under its own id.

Second bug, self-caught before it shipped: Scheduler accepted a
`supervisor_model` constructor param and never used it anywhere --
`self.supervisor` is a plain `packet -> SupervisorResult` callable, not
`(packet, model) -> ...` the way spawn_worker takes model explicitly, so
there was nothing to thread it into. Removed; a live caller binds the model
via a closure over invoke_supervisor instead (see
tests/integration/test_supervisor_live.py for the pattern).

Also added, on the orchestrator side, defense the design implies but doesn't
spell out: _handle_triage re-checks that whatever action.action the
supervisor callable returned is actually in packet.allowed_actions,
regardless of which backend produced it, and falls back to a synthetic
escalate + supervisor.failed if not. "Enforcement lives orchestrator-side"
should hold for every supervisor implementation, not just invoke_supervisor's
own internal validation.

Kept the M2/M3 regression suites completely unmodified: Scheduler's default
`supervisor=always_escalate` reproduces M2's old blanket policy exactly, so
none of tests/scenarios/ needed to change. New coverage: scripted-supervisor
scheduler tests (tests/test_supervisor_scheduler.py) exercise every action's
real dispatch logic deterministically -- no LLM, FakeWorker only, same "never
debug the orchestrator through paid nondeterministic workers" posture as
M2/M3. Live validation (tests/integration/test_supervisor_live.py, opt-in,
~$0.05 for 3 tests): a real haiku supervisor correctly restarted-or-escalated
a recoverable failure, correctly refused to guess at an unanswerable question
(design.md's own heuristic, followed), and a dumped packet replayed cleanly
through the supervisor-replay CLI.

## 2026-07-20 - M5: worktree pool, dep resolution, three delivery modes

Committed M2-M4 first (they'd been sitting uncommitted through three
milestones of work) as one combined commit -- the interleaved edits to
pyproject.toml/README/devlog/design docs across those three milestones
couldn't be cleanly un-interleaved into separate historically-accurate
commits after the fact without risking a misleading or broken intermediate
state, and an honest single commit beat a fake-precise three.

worker/worktree_pool.py: fixed-size slot reuse instead of
create/destroy per task attempt. Pool size is always max_concurrency --
"never a reason for more slots than tasks that can be running at once," so
acquire() blocking on a free slot is concurrency control falling out of the
pool for free rather than a second limiter to keep in sync with the first.
Reset-on-acquire (git reset --hard + clean -fdx + checkout -B) rather than
remove/recreate is the actual saving: no more worktree-add metadata churn
per attempt.

The one real design question was the interaction with crash reconciliation
(design.md section 4): a prior orchestrator process can die mid-run with a
slot still checked out and dirty. Rather than teaching reconcile() anything
about worktrees, pool.open() just wipes and recreates every slot
unconditionally on every fresh process -- consistent with "no special
recovery code path." Tested directly: open a pool, acquire and dirty a slot,
never close it (simulating the crash), then open a second pool at the same
path and confirm it doesn't choke and hands back a clean slot.

Delivery: scout and local already had real coverage from M2/M3; pr never
did. Split _pr() into the push (real git, now tested against a local
bare-repo remote) and open_pr (injected, defaults to real `gh pr create`) so
the mechanics are testable without gh or network -- same
dependency-injection shape as spawn_worker/supervisor. Added a full
scheduler-level pr test too: real FakeWorker task, real pooled worktree,
real push, PR-open swapped out.

Dependency resolution (_advance_deps) has existed since M2 but was only ever
exercised by hand-applying events in test_replay.py, never through a live
Scheduler run with actually-dependent tasks. New coverage: a 3-task chain
(asserts b's spawn happens after a's delivery by event seq, not just by
final state), a fan-in (d depends on both b and c), and a dep-failure
cascade (cancelling a mid-chain task cancels everything downstream that
never got the chance to run).

10-task parallel batch (the milestone's explicit test), 3 pool slots: all
10 deliver, replay(events) == tasks still holds under real concurrent
scheduling, and the running-transition worktree paths collapse to exactly
3 distinct values across all 10 tasks -- the reuse is real, not just
plumbed.

73 tests now (up from 60), all still free/deterministic except the 4
pre-existing opt-in live ones. No new dependencies.

## 2026-07-21 - `orchestrator` CLI: add-task, run, daemon, answer, status

The system was usable only by hand-writing a Python script per batch
(Scheduler + create_task called directly). Added src/orchestrator/cli.py, a
thin argparse wrapper -- no new control flow, just the plumbing design.md's
non-goals never ruled out: `orchestrator add-task/run/daemon/answer/status`.
Registered as the `orchestrator` console script alongside the existing
verify-gate/supervisor-replay ones.

`daemon` needed one real Scheduler change: run_until_settled() gained
`forever: bool = False, poll_interval_s: float = 1.0` kwargs. With
forever=True the loop never exits just because the team is momentarily
settled -- it keeps re-querying the same SQLite file for newly-blocked/queued
rows, so a separate `add-task` process (or any other connection) writing to
that file gets picked up without restarting the daemon. Verified this isn't
just plausible reasoning about WAL mode: a real test starts run_until_settled
(forever=True) against an empty DB, waits, creates a task on a *second*
connection to the same file, and asserts it gets spawned and delivered
before cancelling the loop.

`answer` was the one with a real design question: design.md's state table
says `needs_human -> running`, "manager's answer injected into session" --
but by the time a task reaches needs_human, _handle_triage's escalate branch
has already torn the worker down. There's no session left to inject into.
So `answer` actually does `needs_human -> queued`, folding the message into
the task's `brief` (symmetric to how the supervisor's own `restart` appends
feedback) for a fresh attempt next time something runs the scheduler. Added
"queued" as a legal needs_human transition and added `brief` to
transition()'s `_UPDATABLE` set -- replay() needed no changes since it
already walks `_UPDATABLE` generically. Updated the design.md table with the
new edge and corrected the worker-lifecycle note (section 8) that had
implied live-session injection for this case.

Real bug found via a shell smoke test, not the test suite: WorktreePool
never resolved repo_root/worktree_root to absolute paths. `git worktree add`
in open() runs with cwd=repo_root, so a relative worktree_root resolved
against the repo; the per-slot git calls in acquire() run with cwd=<slot>,
which is relative to whatever process is calling the scheduler -- two
different interpretations of the same relative path depending on call site.
Every existing test happened to pass an absolute tmp_path-derived
worktree_root, so this never surfaced until the CLI's relative
`data/worktrees` default hit it in a real terminal. Fixed by resolving both
paths once in WorktreePool.__init__ rather than working around it in cli.py.

`status <task_id>` prints the live escalation (summary/question/options/
recommended, pulled from the last supervisor.acted event) plus the exact
`orchestrator answer ...` command to resolve it -- the point of the command
is to remove the "go write SQL by hand" step docs/usage.md used to describe.

10 new tests in tests/test_cli.py (81 total). docs/usage.md rewritten to
lead with the CLI; the direct-library path is now section 8, for embedding
rather than day-to-day use.

## 2026-07-22 - closed the live-nudge gap, added event-driven notifications

First: a real live nudge.
Section 6's supervisor contract has always specified `nudge` as "message
injected into the live session," and scheduler/core.py's `_handle_triage`
already wrote the message to the worker subprocess's stdin.
But worker/sdk_worker.py never read it back into the conversation -- the
`asked` branch called `sys.stdin.readline()` and discarded the result, then
exited.
The nudge landed on the pipe and went nowhere.
Restructured `run()` into a loop: after an ASK, block on stdin, and if a
reply arrives, `client.query()` it back into the same `ClaudeSDKClient`
session (confirmed via the SDK source that `query()` is safely re-callable
mid-session) instead of tearing down.
No reply (stdin closes because escalate/abandon tore the process down
instead) still exits clean, same as before.
Covered by two new unit tests in test_sdk_worker.py against a fake
`ClaudeSDKClient` stand-in, no live API calls.

Second: nothing proactively told the human anything.
`run` blocked silently until the batch settled; `daemon` polled forever and
never printed anything after its startup line, so the only way to learn a
task hit `needs_human` was to remember to run `status`.
Added `_notify_loop` in cli.py: a second SQLite connection (safe under WAL,
concurrent with the scheduler's own writer) that tails `events` for
`task.state_changed` rows landing in needs_human/delivered/failed and prints
one line per hit.
Wired into both `run` and `daemon` via `_run_with_notify`, which races it
alongside `run_until_settled()` and cancels it on exit.
No changes to Scheduler itself -- this reads the same events table
everything else already treats as the source of truth, from the outside.
Updated the `orchestrator` skill to launch a backgrounded `run`/`daemon` and
then `Monitor` its stdout instead of only checking `status` after the fact,
so a chat session gets woken the way firstmate's bash watcher wakes its
captain.
Tested with a dedicated `_notify_loop` unit test (start the loop, then
transition a task while it's already polling -- it must ignore
already-existing history and only report what happens next) rather than
relying on race-prone timing inside a full `cmd_run` run.

Deliberately did not touch: visible/watchable worker sessions (tmux-style
panes) or the `answer` command's restart-vs-nudge semantics for
`needs_human` -- design.md's non-goals rule the first out for v1, and the
second is an intentional, already-logged decision (2026-07-21 entry above),
not a bug.
