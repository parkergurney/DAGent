# Using this repo to orchestrate agents

Status as of M5: there is no daemon process and no `orchestrator run` CLI yet.
The orchestrator is a library — `Scheduler` — driven by a short Python script
you write per batch.
That script is the "manager" front-end until a TUI shows up (unscheduled, see
design.md section 11).
Two standalone CLIs already exist for adjacent jobs: `verify-gate` (grade one
task's worktree) and `supervisor-replay` (re-run a saved triage packet against
the current supervisor prompt).

This doc is the practical "how do I actually run tasks" companion to
[design.md](design.md), which is the architecture reference.

## 1. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Requires `claude-agent-sdk` (real worker sessions) and a Claude Code / API
auth setup that the SDK can pick up in your environment — same auth the
`claude` CLI itself uses.

## 2. The moving pieces you'll wire together

- `orchestrator.store.connect(path)` — opens/creates the SQLite DB (schema
  auto-bootstraps).
- `orchestrator.store.create_task(...)` — inserts one task (state=`blocked`)
  and emits `task.created`.
- `orchestrator.scheduler.Scheduler(...)` — the whole control loop.
  `await scheduler.run_until_settled()` drives every task from wherever it
  is to a resting state (`delivered` / `failed` / `cancelled` / `needs_human`),
  then returns. That's the whole batch run.
- `spawn_worker=` — `spawn_fake_worker` (scripted, free, deterministic — use
  for dry runs) or `spawn_sdk_worker` (real Claude Code session per task, in
  its own pooled git worktree).
- `supervisor=` — `always_escalate` (default; every triage → `needs_human`,
  no LLM calls, fully deterministic) or
  `functools.partial(invoke_supervisor, model="claude-sonnet-5")` for a live
  LLM triage decision.

## 3. Minimal real run: one task, real worker, real supervisor

```python
import asyncio
from functools import partial
from pathlib import Path

from orchestrator.store import connect, create_task
from orchestrator.scheduler import Scheduler
from orchestrator.worker import spawn_sdk_worker
from orchestrator.supervisor import invoke_supervisor

conn = connect("data/orchestrator.db")

task_id = create_task(
    conn,
    title="Add input validation to parse_csv",
    brief="parse_csv() in src/csv_utils.py crashes on empty files. "
          "Make it raise a clear ValueError instead. Add a test.",
    repo="/path/to/target/repo",       # a real git repo, clean working tree
    delivery_mode="pr",                # "pr" | "local" | "scout"
    verify_cmd="pytest tests/test_csv_utils.py",
)

scheduler = Scheduler(
    conn,
    repo_root="/path/to/target/repo",
    worktree_root=Path("data/worktrees"),
    max_concurrency=4,
    spawn_worker=spawn_sdk_worker,
    worker_model="claude-sonnet-5",
    supervisor=partial(invoke_supervisor, model="claude-sonnet-5"),
)

asyncio.run(scheduler.run_until_settled())

row = conn.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
print(task_id, row["state"])
```

Run it, then inspect what happened:

```bash
sqlite3 data/orchestrator.db "SELECT seq, type, task_id FROM events ORDER BY seq"
```

## 4. Multiple tasks / a DAG

Pass `depends_on=[other_task_id, ...]` to `create_task`. A task sits in
`blocked` until every dependency reaches `delivered`; if any dependency
lands in `failed`/`cancelled`, it cascades to `cancelled` instead of running.
Create all the tasks up front, then a single `run_until_settled()` call
drives the whole batch — the scheduler launches whatever's ready, up to
`max_concurrency`, and reuses a fixed-size worktree pool as slots free up.

```python
a = create_task(conn, title="...", brief="...", repo=repo, delivery_mode="pr", verify_cmd="pytest")
b = create_task(conn, title="...", brief="...", repo=repo, delivery_mode="pr", verify_cmd="pytest",
                depends_on=[a])
c = create_task(conn, title="...", brief="...", repo=repo, delivery_mode="pr", verify_cmd="pytest",
                depends_on=[a])
# ... same Scheduler + run_until_settled() call as above, once, for all of them
```

## 5. Delivery modes

- `pr` — pushes the worktree's branch and opens a PR via `gh`. `delivered`
  means the PR is open, not merged; merging is your call.
- `local` — fast-forward merges into the repo's local default branch.
- `scout` — never pushes; writes `data/<task_id>/report.md` instead. Use
  this for investigation-only tasks, or for dry runs against a repo you
  don't want touched yet.

## 6. Dry run with no LLM calls at all

Swap in `spawn_fake_worker` and leave `supervisor` at its default
(`always_escalate`) to exercise the whole state machine — worktrees, DAG
resolution, verify gate, delivery — with zero API cost and full
determinism. This is exactly what `tests/scenarios/` does; read
`tests/scenarios/test_dag_and_parallelism.py` for more end-to-end examples,
including a 10-task parallel batch against a 3-slot pool.

## 7. When a task lands in `needs_human`

That's the escalation state: the supervisor (or the exhausted-retries path)
handed you a summary, a question, and a short list of options. Read it from
the events table:

```bash
sqlite3 data/orchestrator.db \
  "SELECT payload FROM events WHERE task_id='<id>' AND type='supervisor.acted' ORDER BY seq DESC LIMIT 1"
```

There's no manager-answer-injection helper yet (design.md's `needs_human →
running` transition is specced but the "inject the manager's answer"
plumbing hasn't landed). For now, resolving one means writing the state
transition and any follow-up event by hand via `orchestrator.store.transition`.

## 8. Standalone CLIs

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

## 9. Crash recovery

Nothing special to do. If the process handling `run_until_settled()` dies,
just start a new script and call `Scheduler(...).run_until_settled()` again
against the same DB — reconciliation runs automatically at the top of every
call: tasks stuck in `running` with a dead session get a synthetic
`worker.exited` and route through triage like any other crash, and the
worktree pool wipes and re-checks-out every slot unconditionally.

## 10. What's not here yet

- No daemon / long-running process that watches for new tasks — each
  `run_until_settled()` call is a fixed batch to completion (M11's TUI is
  the eventual front end for a live queue).
- No CLI for creating tasks; use `create_task` from a script as above.
- No manager-answer injection helper for `needs_human` tasks.
- Benchmark harness (M6/M7) isn't built yet; `bench/` is scaffolding only.
