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

This skill only dispatches commands; it doesn't add judgment beyond what's
below. `orchestrator run`/`daemon` themselves spawn real Claude Code worker
sessions that write code, run tests, and can push branches or open PRs -
autonomously, with no further approval once launched. The guardrails in this
file exist because one Bash approval for `run`/`daemon` unlocks a lot of
unattended, hard-to-interrupt activity - keep them, don't route around them
just because a request seems simple.

## Prereq check

If `which orchestrator` fails, tell the user to run `pip install -e .` from
the repo root first (registers the console script) rather than trying to
invoke `python -m orchestrator.cli` yourself as a workaround.

## Command mapping

**"add a task to fix X" / "create a task for Y" / "queue up..."**
```
orchestrator add-task --repo <path> --title "<short title>" --brief "<what to do>" \
  --delivery-mode {pr|local|scout} [--verify-cmd "<test command>"] [--depends-on <task_id>]...
```
Never guess `--repo` or `--delivery-mode` - see Guardrails. Prints the new
task's id; tell the user what it is, since they'll need it for `answer`.

**"run it" / "start the batch" / "go"**
```
orchestrator run --repo-root <path> [--fake-worker --fake-supervisor for a dry run]
```
Launch with Bash `run_in_background: true` - this drives every pending task
to a resting state and can take anywhere from seconds to a long time, and
blocking the conversation on it is worse than reporting back when it's done.
Tell the user you're doing this and that you'll report back; don't silently
background it.

**"keep it running" / "start the daemon" / "watch for new tasks"**
```
orchestrator daemon --repo-root <path> [--poll-interval <seconds>]
```
See Guardrails - this one needs an explicit separate confirmation before you
launch it, every time, regardless of how the request is phrased.

**"what's the status" / "how's it going" / "show me the tasks"**
```
orchestrator status
```
**"what's going on with <task>" / "tell me about task <id>"**
```
orchestrator status <task_id>
```
Translate the table/detail output into prose - task titles and plain-English
states, not a raw dump. If a task is `needs_human`, always surface the
question and options conversationally (see Guardrails) even if the user only
asked for status in passing.

**"answer <task> with X" / "tell it to use Y" / "resolve that"**
```
orchestrator answer <task_id> "<message>"
```
Only valid from `needs_human`; if the user references "that task" or "the
one that's stuck" without an id, run `orchestrator status` first to find it
rather than guessing.

All commands take `--db <path>` (default `data/orchestrator.db`); only pass
it if the user names a specific db file or you're clearly working with more
than one.

Less common flags (`--max-concurrency`, `--worker-model`, `--supervisor-model`,
`--worktree-root`, `--max-retries`, `--config`) aren't listed above - run
`orchestrator <command> --help` if the user asks for something not covered
here, rather than guessing a flag name.

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
6. **Surface `needs_human` proactively, don't just report state.** After any
   `run` finishes, or when checking `status` for any reason, if a task is
   `needs_human`, always relay its summary/question/options in plain
   language and ask the user how to answer - don't wait for them to notice
   and ask separately. This is the one moment the system actually asks for a
   human, so don't let it pass silently.
7. **When in doubt about scope or consequences, ask before running** rather
   than picking a default and mentioning it after the fact - a single `run`
   already commits to a lot of autonomous activity, so "ask first" is
   cheaper than "explain after."
