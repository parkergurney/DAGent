# Design: agent orchestration system for Claude Code

Status: M5 complete (worktree pool, dep resolution, all three delivery modes),
plus an `orchestrator` CLI (add-task/run/daemon/answer/status, docs/usage.md)
layered on top so the system is usable without hand-writing a Python script.

This file carries the always-relevant core: thesis, architecture, and the
invariants that must never be violated. The full design doc lives in
docs/design.md and is the source of truth for architecture decisions; the
rest of it (task state machine, storage schema, supervisor contract, verify
gate, worker lifecycle, delivery modes, benchmark plan, milestones, config,
open questions) is split into topic skills under `.claude/skills/` (see
"Deep reference" below) so a session only loads what its task actually
touches. Update docs/design.md when decisions change; log the change and
rationale in devlog.md.

Working name TBD. "agent-orchestrator" is a placeholder.

`CHARTER.md` (repo root) holds the captain's working preferences for how the
`orchestrator` CLI/skill should behave - lighter-weight than this file,
captain-owned, edited freely. `repos.toml` (repo root) is a flat, manually-
edited short-name -> path registry that `add-task --repo` can resolve; see
docs/usage.md. Neither changes anything under `src/orchestrator/`.

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

## Deep reference: topic skills

Full detail for each area below lives in docs/design.md and is split into a
Claude Code skill so it loads only when relevant, instead of on every task:

- `task-state-machine` — states, transition table, crash recovery. Relevant
  when touching scheduler/state-machine code or task transitions.
- `storage-schema` — SQLite schema, event taxonomy. Relevant when touching
  persistence code or adding a new event type.
- `supervisor-contract` — TriagePacket, actions, enforcement, prompt
  heuristics. Relevant when touching supervisor/triage code.
- `verify-gate` — VerifyRequest/Result, execution order, cause→heuristic
  table. Relevant when touching verify-gate code or debugging a verify
  failure.
- `worker-lifecycle-delivery` — Agent SDK session lifecycle, delivery modes.
  Relevant when touching worker spawning or delivery/git-push code.
- `benchmark-plan` — conditions, workload, metrics, scope levers. Relevant
  when touching the benchmark harness or eval design.
- `milestones` — M0-M7 roadmap, FakeWorker suite, config defaults, open
  questions. Relevant when planning work or checking project scope/status.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
