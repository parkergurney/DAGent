# Opinions

What this project is for, and how the user wants the orchestrator skill
to behave day to day. Lighter than `CLAUDE.md` (which owns the architecture)
- this is working preferences, meant to be edited freely.

Preferences here never override an invariant in `CLAUDE.md` section 3 or
`docs/design.md`. If something below conflicts with one, the invariant wins
and this file is wrong - fix the conflict rather than following it.

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

- Be the operator interface. The user should say what they want in natural
  language; the agent should run `dagent` commands and summarize results
  instead of handing back shell snippets for routine add/run/status/answer work.
- Read `status` back in plain English: what's blocked and why, not just
  state names.
- Never silently pick `--repo`, `--delivery-mode`, or launch `daemon` - ask
  first (see `.claude/skills/orchestrator/SKILL.md` Guardrails).
- Treat `--yolo` as an explicit opt-in only, never a default.

## When the project is done

"Done" means through M7 (Harbor eval runs + writeup) per the `milestones`
skill. The system is integrated, evaluated through Harbor, and written up.
No more architecture-level changes are expected after that - this section
is forward-looking guidance for that future cleanup pass, not something to
act on now.
The six skills below stay in place until M7 actually closes.

Once "done", prune the dev-time-only "deep reference" topic skills under
`.claude/skills/` - they only trigger on "touching X code", i.e. building or
maintaining the orchestrator's own internals, not on using the finished tool:

- `milestones`
- `storage-schema`
- `supervisor-contract`
- `task-state-machine`
- `verify-gate`
- `worker-lifecycle-delivery`

Keep:

- `.claude/skills/orchestrator/SKILL.md` - the natural-language front end for
  actually *using* the shipped `dagent` CLI (add-task/run/daemon/
  status/answer). Still needed after completion.
- `OPINIONS.md` (this file) - user preferences don't expire.
- `docs/devlog.md` - historical record, not reference.

Trim, don't delete:

- `AGENTS.md` / `CLAUDE.md` and `docs/design.md` - cut down to what a future
  contributor needs to orient in the finished codebase, once the deep-
  reference skills they point to are gone.
- `docs/design.md` specifically keeps its architecture narrative (still
  useful for onboarding); only its "Deep reference: topic skills" section
  goes, since it just links to the seven skills above.

Edit this file directly to change any of the above - it's user-owned,
not regenerated.
