---
name: orchestrator
description: Talk to this project's `orchestrator` CLI in natural language instead of typing bash commands - add tasks, run or watch a batch, check status, and answer escalations. Use whenever the user asks to create/add/queue a coding task, run or start a batch, watch for new tasks, check on tasks or the workers, or answer/resolve/respond to a task that's stuck or waiting on a decision. Also use for phrases like "orchestrate this", "spin up a task", "what's the status", "it's asking me something", or naming a task by id.
allowed-tools:
  - Bash
  - AskUserQuestion
---

# orchestrator

Natural-language front end for the `orchestrator` CLI (`src/orchestrator/cli.py`
in this repo). Translate what the user says into the right subcommand below,
run it via Bash, and read the result back to them in plain language - don't
just dump raw CLI output.

The intended UX is conversational. The user should be able to say things like:

- "queue a task to fix the CSV import bug in sqlite-utils"
- "start the batch"
- "what's blocked?"
- "answer the URI task with option 2"
- "keep watching this repo for more tasks"

Your job is to operate the CLI for them. Do not respond with instructions for
the user to copy-paste unless they explicitly ask for commands or you are
blocked by missing local setup/approval.

This skill only dispatches commands; it doesn't add judgment beyond what's
below. `orchestrator run`/`daemon` themselves spawn real Claude Code worker
sessions that write code, run tests, and can push branches or open PRs -
autonomously, with no further approval once launched. The guardrails in this
file exist because one Bash approval for `run`/`daemon` unlocks a lot of
unattended, hard-to-interrupt activity - keep them, don't route around them
just because a request seems simple.

## Opinions

If `OPINIONS.md` exists at the repo root, read it before acting. It holds the
user's working preferences (deterministic-first, minimal moving parts,
terse-by-default status, honest escalation) layered on top of the guardrails
below - honor it the same way you'd honor an explicit instruction from the
user. Don't wait to be asked; check for it once per session.

## Prereq check

If `which orchestrator` fails, tell the user to run `pip install -e .` from
the repo root first (`pip install -e ".[dev]"` for development installs,
registers the console scripts) rather than trying to invoke
`python -m orchestrator.cli` yourself as a workaround.

## Conversation flow

For ordinary operator requests, follow this loop:

1. Resolve any missing trust-boundary inputs (`--repo`/`--repo-root`,
   `--delivery-mode`, daemon confirmation, yolo confirmation).
2. Run the CLI command yourself.
3. Summarize what changed: task ids created, run started, task state, or the
   answer that was folded into the brief.
4. If a run/daemon emits `needs_human`, immediately fetch task detail and ask
   the user how to answer it.

Do not make the user manage task ids manually when the state can be read from
SQLite. If they say "the stuck one" or "the URI task", run status/detail queries
to resolve the task, then confirm if more than one task matches.

## Command mapping

**"add a task to fix X" / "create a task for Y" / "queue up..."**
```
orchestrator add-task --repo <path> --title "<short title>" --brief "<what to do>" \
  --delivery-mode {pr|local|scout} [--verify-cmd "<test command>"] [--depends-on <task_id>]...
```
Never guess `--repo` or `--delivery-mode` - see Guardrails. Prints the new
task's id; tell the user what it is, since they'll need it for `answer`.
If the user gives a repo short name, use it through `repos.toml`; if they give
a natural repo name, inspect `repos.toml` before asking them for a path.

**"run it" / "start the batch" / "go"**
```
orchestrator run --repo-root <path> [--fake-worker --fake-supervisor for a dry run]
```
Launch with Bash `run_in_background: true` - this drives every pending task
to a resting state and can take anywhere from seconds to a long time, and
blocking the conversation on it is worse than reporting back when it's done.
Tell the user you're doing this and that you'll report back; don't silently
background it. Immediately follow with `Monitor` on that background task:
`run`/`daemon` print a line the instant a task lands in `needs_human`,
`delivered`, or `failed` (not just at the very end), so Monitor is what
turns those into a live notification in this chat instead of you finding out
only when the whole batch finishes or the user asks for `status`.

**"keep it running" / "start the daemon" / "watch for new tasks"**
```
orchestrator daemon --repo-root <path> [--poll-interval <seconds>]
```
See Guardrails - this one needs an explicit separate confirmation before you
launch it, every time, regardless of how the request is phrased. Same
Monitor pattern as `run` above - `daemon` never exits on its own, so Monitor
is the only way you'll hear about a `needs_human` task without the user
having to ask.

**"what's the status" / "how's it going" / "show me the tasks"**
```
orchestrator status
```
**"give me the short version" / "quick summary" / "how many are left"**
```
orchestrator status --digest
```
Terse mode: state counts, any `needs_human` tasks with their questions, and
the total worker-session count, instead of one row per task. Reach for this
when the user wants a pulse check rather than the full list, or when the
task count is large enough that the full table would bury the thing they
actually need to know (a stuck task). It's a one-shot read, same as plain
`status` - not a live loop, don't reach for it repeatedly to "watch" state;
use `Monitor` on a backgrounded `run`/`daemon` for that instead.

**"what's going on with <task>" / "tell me about task <id>"**
```
orchestrator status <task_id>
```
Translate the table/detail/digest output into prose - task titles and
plain-English states, not a raw dump. See "Translating status" below for how
to phrase each state and a `needs_human` escalation. If a task is
`needs_human`, always surface the question and options conversationally (see
Guardrails) even if the user only asked for status in passing.

**"answer <task> with X" / "tell it to use Y" / "resolve that"**
```
orchestrator answer <task_id> "<message>"
```
Only valid from `needs_human`; if the user references "that task" or "the
one that's stuck" without an id, run `orchestrator status` first to find it
rather than guessing.

All commands take `--db <path>` (default `data/orchestrator.db`); only pass
it if the user names a specific db file or you're clearly working with more
than one. When a run is already active or the user is referring to an existing
batch, reuse that batch's db path.

Less common flags (`--max-concurrency`, `--worker-model`, `--supervisor-model`,
`--worktree-root`, `--max-retries`, `--config`) aren't listed above - run
`orchestrator <command> --help` if the user asks for something not covered
here, rather than guessing a flag name.

## Translating status into plain language

Never hand back a raw `state` column or a `needs_human` payload verbatim -
say what it means and, where it matters, why the user might care:

| state | say something like |
|---|---|
| `blocked` | "waiting on another task to finish first" |
| `queued` | "waiting for a free worker slot" |
| `running` | "a worker is actively on it" |
| `verifying` | "worker claimed it's done, tests are being checked" |
| `triage` | "something went wrong (stall, crash, or failed check) and the supervisor is deciding what to do next" |
| `needs_human` | "stuck - it's asking you something" (always surface the question, see below) |
| `delivering` | "verified, opening the PR / merging / writing the report now" |
| `delivered` | "done - PR open" (or merged, or report written, per delivery mode) |
| `failed` | "gave up - out of retries or the supervisor abandoned it (yolo mode only)" |
| `cancelled` | "killed, or a dependency it needed failed" |

For a `needs_human` task, always relay: what it was trying to do (title/brief
in short), the escalation summary, the actual question, and the numbered
options with the recommended one flagged - then ask the user how to answer
rather than guessing. This is the one moment the system is explicitly asking
for a human; don't compress it down to "1 task needs attention."

`--digest`'s state-count line is meant to be read aloud as a sentence ("12
tasks: 6 delivered, 3 running, 2 needs_human, 1 failed"), not pasted as-is -
lead with whatever's actionable (`needs_human`, `failed`) rather than the
raw ordering the command prints.

## Guardrails

These come directly from a trust-boundary discussion about this tool: a
single Bash approval to launch `run` or `daemon` hands off control to a
process that then acts autonomously - spawning worker sessions, making
triage decisions, pushing code - without further per-action confirmation.
The mitigations below exist to keep a human genuinely in the loop despite
that, not to slow things down for their own sake.

1. **Never assume `--repo-root` / `--repo`.** Ask if it's not obvious from
   context. Getting this wrong means real worker sessions editing the wrong
   repository.
2. **Never assume `--delivery-mode`.** Ask, and briefly explain the
   consequence of each when the user seems unsure: `scout` never touches the
   remote (writes a report only - safest default for a first try or a dry
   run); `local` fast-forward-merges into the repo's default branch; `pr`
   pushes a branch and opens a real PR via `gh`. Don't default to `pr`
   silently just because it's the "real" option.
3. **First run against a repo or brief you haven't run before**: suggest
   `--fake-worker --fake-supervisor` (free, deterministic, exercises the
   whole pipeline with no LLM calls or real edits) before a real run, unless
   the user is clearly past that point already.
4. **Never pass `--yolo`** unless the user explicitly asks for it by name or
   explicitly asks for auto-abandon/auto-fail-cascade behavior. It's an
   opt-in escape hatch from the escalate-by-default posture, not a default.
5. **`daemon` requires an explicit, separate confirmation every time**,
   naming what it actually means: it does not stop on its own, it will keep
   accepting and acting on any task added to that same database for as long
   as it runs, and each accepted task gets a real worker session with the
   same autonomy as `run`. Use `AskUserQuestion` (or a direct question) if
   the intent is at all ambiguous - "keep this running in the background" is
   not the same as "run it once."
   - Launch it with Bash `run_in_background: true` (it never exits, so this
     is the only sane way to launch it) and tell the user how to stop it:
     ask this session to stop it (tracked via the background task id so
     `TaskStop` can end it cleanly), or `pkill -f "orchestrator daemon"`
     themselves if this session isn't around anymore.
6. **Surface `needs_human` proactively, don't just report state.** A
   `[needs_human] <task_id> <title>` line from `Monitor` on a backgrounded
   `run`/`daemon` is your cue to act on immediately - run
   `orchestrator status <task_id>` for the full escalation and relay its
   summary/question/options in plain language, then ask the user how to
   answer. Same rule applies after any `run` finishes or whenever you check
   `status` for any other reason: don't wait for the user to notice and ask
   separately. This is the one moment the system actually asks for a human,
   so don't let it pass silently.
7. **When in doubt about scope or consequences, ask before running** rather
   than picking a default and mentioning it after the fact - a single `run`
   already commits to a lot of autonomous activity, so "ask first" is
   cheaper than "explain after."
