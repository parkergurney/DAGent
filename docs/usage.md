# Using this repo to orchestrate agents

As of this session there's an `orchestrator` CLI (`add-task`, `run`, `daemon`,
`answer`, `status`) layered over the `Scheduler` library, so day-to-day use
doesn't require writing a Python script. The library is still there underneath
for anyone embedding this in something else — see section 9.

This doc is the practical "how do I actually run tasks" companion to
[design.md](design.md), which is the architecture reference.

## 0. Intended operator experience

The primary interface is natural language through an agent session that has
this repo loaded. You should be able to say:

- "queue a task to fix empty CSV imports in sqlite-utils"
- "start the batch"
- "what's blocked?"
- "answer the task about URI paths with option 2"
- "keep watching this repo for new tasks"

The agent should translate that into `orchestrator add-task`, `run`, `status`,
`answer`, or `daemon`, run the command, and report back in plain English. The
CLI is the implementation boundary, not something the operator should have to
drive by hand for ordinary use.

The agent must still ask before crossing real trust boundaries: which repo to
act on if ambiguous, which delivery mode to use, whether to launch the
never-ending daemon, and whether to enable `--yolo`. Those choices can spawn
real worker sessions, commit code, merge locally, or open PRs.

## 1. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Requires `claude-agent-sdk` (real worker sessions) and a Claude Code / API
auth setup that the SDK can pick up in your environment — same auth the
`claude` CLI itself uses. This registers the `orchestrator`, `verify-gate`,
and `supervisor-replay` console scripts.

## 2. Add a task

```bash
orchestrator add-task \
  --repo /path/to/target/repo \
  --title "Add input validation to parse_csv" \
  --brief "parse_csv() in src/csv_utils.py crashes on empty files. Make it raise a clear ValueError instead. Add a test." \
  --delivery-mode pr \
  --verify-cmd "pytest tests/test_csv_utils.py"
```

Pass `--setup-cmd` for any repo whose verify command needs an install step
first (e.g. `--setup-cmd "npm install"`).
It runs before `verify_cmd` in both the worker's own worktree and the
verify gate's throwaway baseline checkout.
Skipping it for a repo that needs one caches `baseline_broken` against the
base branch forever, since the baseline checkout has no dependencies
installed and nothing else ever retries it (design.md section 7).

Pass `--protected-paths` (repeatable) only for benchmark, hidden, or
instructor-owned checks the worker must not rewrite. The default is empty
because visible project tests are normal feature-work surface and often need
to change with the implementation.

Prints the new task's id (a ULID). Defaults to `data/orchestrator.db`; pass
`--db` to use a different file. Chain tasks into a DAG with repeatable
`--depends-on <task_id>` — a task sits in `blocked` until every dependency
reaches `delivered`, and cascades to `cancelled` if any dependency fails.

`--repo` also accepts a short name registered in `repos.toml` (repo root)
instead of a full path -- see that file for the format. It's a flat,
manually-edited `name = "path"` table; nothing clones or initializes repos
for you, and there's no per-repo policy config, just a lookup so you don't
have to retype paths. Add an entry, then pass the name: `--repo
agent-orchestrator` instead of `--repo /path/to/checkout`. Anything not
found in the table is used as a literal path, unchanged.

## 3. Run it

```bash
orchestrator run --repo-root /path/to/target/repo --db data/orchestrator.db
```

Drives every pending task (any task not already in a resting state) to
`delivered` / `failed` / `cancelled` / `needs_human`, then exits and prints a
status table. This is the whole of a batch run: real Claude Code worker
sessions in pooled git worktrees, real triage decisions from a live LLM
supervisor, real verify-gate grading, real delivery.

It also streams one line to stdout the moment any task lands in
`needs_human`, `delivered`, or `failed` — you don't have to wait for the
whole batch or poll `status` to find out. Run this in the background and
watch its stdout (e.g. Claude Code's `Monitor` tool, or `tail -f` on a
redirected log) to get notified as it happens.

Useful flags (all optional):

- `--worker-model` / `--supervisor-model` — override `config.py`'s defaults.
- `--max-concurrency N` — worktree pool size / parallel task cap (default 4).
- `--fake-worker` — scripted `FakeWorker` instead of a real session (free,
  deterministic — for dry runs).
- `--fake-supervisor` — always escalate instead of calling a live LLM
  supervisor.
- `--yolo` — let the supervisor `abandon` tasks and auto-fail dependents
  instead of always escalating.
- `--config path.toml` — override `config.py` defaults (design.md section 12).

A fully free, deterministic dry run that still exercises worktrees, DAG
resolution, the verify gate, and delivery:

```bash
orchestrator run --repo-root /path/to/repo --fake-worker --fake-supervisor
```

## 4. Keep it running (daemon mode)

```bash
orchestrator daemon --repo-root /path/to/target/repo
```

Same as `run`, except it never exits on its own: once every task settles it
keeps polling the SQLite file (every `--poll-interval` seconds, default 1) for
newly added tasks, so a separate `orchestrator add-task` call — from another
terminal, or a script, or a cron job — gets picked up without restarting this
process. Stop it with Ctrl-C; it tears down any live worker cleanly before
exiting. Same stdout notification stream as `run` above — since `daemon`
never exits, that stream is the only way to hear about a `needs_human` task
without going and asking for `status` yourself.

## 5. When a task lands in `needs_human`

```bash
orchestrator status                    # list every task and its state
orchestrator status <task_id>          # that task's escalation: summary, question, options
orchestrator status --digest           # one-shot terse summary (see below)
orchestrator answer <task_id> "use the prod config, not staging"
```

`--digest` prints task counts by state, any `needs_human` tasks with their
questions, and the total worker-session count, instead of the full row-per-task
table. It's a single synchronous read of the same SQLite store as plain
`status` -- not a loop, not polling, not a background watcher -- so it's safe
to run any time you just want the short version.

`answer` folds your message into the task's brief and requeues it
(`needs_human -> queued`) for a fresh worker attempt with that context — by
the time a task escalates, the worker session that asked the original
question is already gone (escalation always tears it down), so this is a
restart-with-feedback, not a live nudge into an old session. If a `run` or
`daemon` process is already watching the same DB, it'll pick the requeued
task up on its own; otherwise run `orchestrator run` again.

## 6. Delivery modes

- `pr` — pushes the worktree's branch and opens a PR via `gh`. `delivered`
  means the PR is open, not merged; merging is your call.
- `local` — fast-forward merges into the repo's local default branch.
- `scout` — never pushes; writes `data/<task_id>/report.md` instead. Use
  this for investigation-only tasks, or for dry runs against a repo you
  don't want touched yet.

## 7. Crash recovery

Nothing special to do. If `run` or `daemon` dies, just run `orchestrator run`
(or `daemon`) again against the same DB — reconciliation happens
automatically at the top of every invocation: tasks stuck in `running` with a
dead session get a synthetic `worker.exited` and route through triage like
any other crash, and the worktree pool wipes and re-checks-out every slot
unconditionally.

## 8. Reviewing delivered work and cleanup

Do not review pooled worktree directories after a run. They are scratch slots,
and the scheduler removes/reuses them during teardown. Review the delivered
artifact recorded in events instead:

```bash
orchestrator status <task_id> --db data/orchestrator.db
```

For `pr`, status prints the PR URL, branch, and commit SHA. Review the PR.

For `local`, status prints exact review commands:

```bash
git -C /path/to/repo diff <before_sha>..<after_sha>
git -C /path/to/repo log --oneline <before_sha>..<after_sha>
```

For `scout`, read `data/<task_id>/report.md`.

The verify gate also saves an event-specific patch for committed diffs and
keeps `data/<task_id>/review.patch` as the latest convenience copy, so a patch
artifact remains after pooled worktrees are torn down.

To confirm cleanup after a completed non-daemon run:

```bash
git -C /path/to/repo worktree list
git -C /path/to/repo branch --list 'pool/slot-*'
ls -la data/worktrees
git -C /path/to/repo status --short
```

Expected: no pooled `slot-*` worktrees remain registered, `data/worktrees`
has no live slot dirs, no `pool/slot-*` scratch branches remain, and the
target repo is clean except for intentional `local` deliveries already merged
into `main`. For `daemon`, cleanup happens when the daemon exits.

## 9. Using the library directly

The CLI is a thin wrapper (`src/orchestrator/cli.py`) over `Scheduler` and
`create_task`; embedding the orchestrator in something else (a larger tool, a
notebook, a custom front-end) means calling those directly instead:

```python
import asyncio
from functools import partial
from pathlib import Path

from orchestrator.store import connect, create_task
from orchestrator.scheduler import Scheduler
from orchestrator.worker import spawn_sdk_worker
from orchestrator.supervisor import invoke_supervisor

conn = connect("data/orchestrator.db")
task_id = create_task(conn, title="...", brief="...", repo="/path/to/repo",
                      delivery_mode="pr", verify_cmd="pytest tests/")

scheduler = Scheduler(conn, repo_root="/path/to/repo",
                      worktree_root=Path("data/worktrees"),
                      spawn_worker=spawn_sdk_worker, worker_model="claude-sonnet-5",
                      supervisor=partial(invoke_supervisor, model="claude-sonnet-5"))
asyncio.run(scheduler.run_until_settled())          # one batch, like `orchestrator run`
# asyncio.run(scheduler.run_until_settled(forever=True))   # like `orchestrator daemon`
```

`spawn_worker=spawn_fake_worker` and `supervisor=always_escalate` (the
`Scheduler` defaults) give the same free/deterministic dry run as
`--fake-worker --fake-supervisor` above.

## 10. Standalone CLIs

```bash
# Grade one task's worktree against its verify_cmd, independent of any
# scheduler run — this is also the exact machinery the benchmark harness
# uses to grade non-orchestrated baselines.
verify-gate --task <task_id> --db data/orchestrator.db --json --record

# Re-run a saved triage packet (data/<task_id>/packets/<seq>.json, written by
# every invoke_supervisor call) against the CURRENT supervisor prompt/model —
# for iterating on supervisor heuristics without paying for a live run.
supervisor-replay data/<task_id>/packets/<seq>.json --model claude-sonnet-5
```

## 11. What's not here yet

- No `needs_human -> delivering` override (design.md's "manager overrides a
  failed verification" edge is specced but has no CLI path yet).
- Benchmark harness (M6/M7) isn't built yet; `bench/` is scaffolding only.
- No auth/access control — this assumes a single trusted operator on one box,
  per design.md's stated non-goals.
