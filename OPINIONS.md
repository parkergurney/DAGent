# Opinions

What this project is for, and how the user wants the orchestrator skill
to behave day to day. Lighter than `CLAUDE.md` (which owns the architecture)
- this is working preferences, meant to be edited freely.

## Thesis

A deterministic orchestration daemon that runs a team of Claude Code
sessions in parallel, using LLM judgment only at the edges (triage
decisions), never in the control loop itself. See `CLAUDE.md` section 1 and
`docs/design.md` for the full architecture.

## What the user cares about

- Deterministic-first. Scheduler, state machine, and verify gate are plain
  code. If an `if` statement fixes it, don't reach for a smarter prompt.
- Minimal moving parts. No new frameworks, no new persistent daemons, no
  config for values that never change. Additions on top (CLI sugar, skills,
  this file) stay cheap and reversible precisely because the engine
  underneath doesn't change.
- LLM judgment stays at the edges: triage (the supervisor) and, in this
  skill layer, translating raw state into plain language. Never a chat loop
  driving the control plane - see `docs/design.md`'s non-goals (no chat
  liaison front-end, no LangGraph/Temporal/Celery-style orchestration).
- Escalate honestly. When a worker is stuck, say so plainly and ask; don't
  guess on the user's behalf, don't paper over a `needs_human` task.
- Terse by default. Prefer a short digest over a wall of raw rows unless
  detail is asked for.

## How the orchestrator skill should behave

- Read `status` back in plain English: what's blocked and why, not just
  state names.
- Never silently pick `--repo`, `--delivery-mode`, or launch `daemon` - ask
  first (see `.claude/skills/orchestrator/SKILL.md` Guardrails).
- Treat `--yolo` as an explicit opt-in only, never a default.

Edit this file directly to change any of the above - it's user-owned,
not regenerated.
