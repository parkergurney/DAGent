# DAGent: repo context for agent sessions

Read [README.md](README.md) first - it is the source of truth for the thesis,
architecture, state machine, supervisor contract, verify gate, worker
lifecycle, delivery modes, and security model.
[BENCHMARK.md](BENCHMARK.md) covers evaluation methodology and results.
This file holds only what those two don't: the invariants that must never be
violated, and where the deeper reference lives.

`repos.toml` (repo root) is a flat, manually-edited short-name -> path registry
that `add-task --repo` can resolve.
`OPINIONS.md` (repo root), when present, holds the user's working preferences
for how the `dagent` CLI/skill should behave.
Neither changes anything under `src/dagent/`.

## Invariants (the contract - enforced in tests)

1. Only the scheduler writes `tasks.state`; every write emits
   `task.state_changed` atomically with it.
2. The supervisor returns one action from a closed enum and never touches the
   database.
3. `tasks` is rebuildable from `events`: `replay(events) == tasks` is asserted
   in CI.
4. `task.state_changed` payloads carry `{from, to, cause_seq}` - the seq of the
   event that caused the transition. Full causality chain.
5. Stall is never self-reported. The watchdog derives `worker.stalled` from the
   absence of events past a threshold.
6. Retry/nudge caps are orchestrator config, never prompt suggestions.

The control plane is deterministic. The only LLM calls in it are single-shot
supervisor invocations. Workers are full Claude Code sessions and are the only
things that write project code.

## Security boundary

DAGent does not provide host isolation. A trusted outer environment
must isolate benchmark workers; direct host execution is trusted development
mode only. Visible verification uses only public worker-visible repository
state. Hidden verifier results never enter the agent environment.
Caller-supplied worker environment variables are never persisted or logged, and
DAGent never accesses the macOS Keychain.

## Deep reference: topic skills

Detail for each area is split into a Claude Code skill under `.claude/skills/`
so it loads only when relevant, instead of on every task:

- `task-state-machine` - states, transition table, crash recovery. For
  scheduler/state-machine code or task transitions.
- `storage-schema` - SQLite schema, event taxonomy. For persistence code or a
  new event type.
- `supervisor-contract` - TriagePacket, actions, enforcement, prompt
  heuristics. For supervisor/triage code.
- `verify-gate` - VerifyRequest/Result, execution order, cause -> heuristic
  table. For verify-gate code or debugging a verify failure.
- `worker-lifecycle-delivery` - Agent SDK session lifecycle, delivery modes.
  For worker spawning or delivery/git code.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in
this project. Do not repeat what README.md, BENCHMARK.md, or the codebase
already shows; point to the authoritative file or command instead. Prefer
rewriting or pruning existing entries over appending new ones.
