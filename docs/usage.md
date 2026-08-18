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
`supervisor-replay`, `orchestrator-experiment`, and `orchestrator-report`
console scripts.

## 1.5 Prepare a benchmark cell

Phase 5 inputs are committed under
[`benchmarks/phase5`](../benchmarks/phase5/README.md). Validate and enumerate
the fixed ten-task graphs, seeded fault profiles, three policies, and separate
cloud/local backend tracks:

```bash
orchestrator-experiment prepare --output-dir results/phase5-matrix
```

Run one free deterministic cell against a throwaway repository, then report
saved cells. The cell writes `run_manifest.json`, `metrics.json`,
`result.json`, `task_summary.json`, and `candidate.patch`:

```bash
orchestrator-experiment run --graph wide --policy orchestrator --seed 0 \
  --profile clean --backend-track cloud-claude \
  --repo-root /path/to/throwaway-repo --output-dir results/phase5/cell-01
orchestrator-report results/phase5/cell-01 --output-dir results/phase5/summary
```

Fault profiles are deterministic FakeWorker cells. A live backend requires the
explicit `--live` flag and a trusted Harbor/container boundary; local Ollama
cells are reported as a separate resource-contention track.

## 2. Add a task

```bash
orchestrator add-task \
  --repo /path/to/target/repo \
  --title "Add input validation to parse_csv" \
  --brief "parse_csv() in src/csv_utils.py crashes on empty files. Make it raise a clear ValueError instead. Add a test." \
  --delivery-mode pr \
  --verify-cmd "pytest tests/test_csv_utils.py"
```

Prints the new task's id (a ULID). Defaults to `data/orchestrator.db`; pass
`--db` to use a different file. Chain tasks into a DAG with repeatable
`--depends-on <task_id>` — a task sits in `blocked` until every dependency
reaches `delivered`; if a prerequisite is terminally unsuccessful, it settles
as `dependency_blocked` without launching a worker. Dependency cycles and
missing prerequisites fail closed before workers launch.

`--repo` also accepts a short name registered in `repos.toml` (repo root)
instead of a full path -- see that file for the format. It's a flat,
manually-edited `name = "path"` table; nothing clones or initializes repos
for you, and there's no per-repo policy config, just a lookup so you don't
have to retype paths. Add an entry, then pass the name: `--repo
agent-orchestrator` instead of `--repo /path/to/checkout`. Anything not
found in the table is used as a literal path, unchanged.

## 3. Run it

```bash
# Supported benchmark shape: Harbor/container isolation must already exist.
orchestrator run --repo-root /path/to/target/repo --db data/orchestrator.db \
  --external-isolation
```

Drives every pending task (any task not already in a resting state) to
`delivered` / `failed` / `cancelled` / `dependency_blocked` / `needs_human`, then exits and prints a
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
- `--base-branch BRANCH` — branch each attempt starts from (default `main`).
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
# Use --external-isolation inside Harbor or another trusted outer environment.
orchestrator daemon --repo-root /path/to/target/repo --external-isolation
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

For the full ten-node restart-recovery demonstration, including literal
process-kill checkpoints and evidence capture, see the
[Phase 2 exit runbook](phase-2-exit-runbook.md).

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

Task specifications may additionally declare `output_artifacts`,
`output_schema`, `input_contract`, `node_verify_cmd`, and `repair_policy`.
These public contracts are persisted with the task, validated before
dependents run, and reported as interface validation events. The
`protocol_recovery_v2`, `deterministic_recovery`, `adaptive_scheduling`, and
`evidence_ladder`
configuration flags are enabled by default and can be set to `false` for
legacy fallback behavior; sequential and naive-parallel remain unchanged
baselines. Attempts also carry durable execution leases, and preflight
write-conflict groups are serialized before workers are admitted.

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

This low-level constructor is for code that already controls the outer
execution environment. It does not enforce host isolation; use the Harbor or
CLI boundary when you need the explicit isolation declaration.

`spawn_worker=spawn_fake_worker` and `supervisor=always_escalate` (the
`Scheduler` defaults) give the same free/deterministic dry run as
`--fake-worker --fake-supervisor` above.

## 10. Standalone CLIs

```bash
# Run the public verification command for a durable task candidate.
verify-gate --task <task_id> --db data/orchestrator.db --json --record

# Re-run a saved triage packet (data/<task_id>/packets/<seq>.json, written by
# every invoke_supervisor call) against the CURRENT supervisor prompt/model —
# for iterating on supervisor heuristics without paying for a live run.
supervisor-replay data/<task_id>/packets/<seq>.json --model claude-sonnet-5

# Aggregate saved benchmark cells. Outcome quality includes successful and
# settled failed cells; runtime overhead includes successful cells only.
orchestrator-report old/jobs/*/artifacts --output-dir results/summary
```

`orchestrator-report` writes `report.json` and `report.md`. A cell whose fault
target was never launched is `inconclusive`; an unsettled or interrupted cell
is `censored`; settled unsuccessful cells remain eligible for outcome-quality
counts but are excluded from runtime comparisons. The report therefore keeps
verified outcome quality separate from orchestration overhead.

## 11. Harbor integration boundary

Harbor creates the isolated task environment and owns hidden tests/scoring. Its
adapter can call the library boundary directly:

```python
from orchestrator.harbor import export_patch, run_instruction

result = await run_instruction(
    instruction="Fix the parser bug and commit the change.",
    repo_root="/workspace/repo",
    policy="orchestrator",  # sequential | naive-parallel | orchestrator
    worker_env=harbor_worker_env,
    external_isolation=True,
)
patch = export_patch("/workspace/repo", base_sha=base, candidate_sha=result.candidate_sha)
```

The adapter transfers only `patch` to Harbor's separate verifier. Worker
environment variables are used for the child process and never written to
SQLite, events, logs, or artifacts. Scheduler teardown reaps all worker
process groups and releases internal worktree slots.

`external_isolation=True` is a required caller declaration for real workers;
it does not create a sandbox. The caller must actually place the process in
Harbor or another trusted outer environment. Without it, `run_instruction`
fails closed. `fake_worker=True` remains available for deterministic local
tests without an outer boundary.

The direct CLI has the same contract. Use `--external-isolation` only when the
caller has supplied Harbor/container isolation. Use `--trusted-development`
for an intentional live host run; that mode is not a benchmark path and does
not protect the host filesystem.

The legacy local-Ollama canary is in
[`harbor/tasks/orchestrator-canary/`](../harbor/tasks/orchestrator-canary/).
The dependency-aware Claude benchmark launcher is in
[`harbor/tasks/orchestrator-dag-canary-claude/`](../harbor/tasks/orchestrator-dag-canary-claude/)
accepts `ORCH_GRAPH_SHAPE=serial|wide|diamond|mixed` and its benchmark runner
defaults to the first three shapes. Shape cells use the committed Phase 5
topologies, real-worker public artifact tasks, and the same separate verifier;
the selected shape and graph hash are recorded in the manifest.
It loads the installed agent by import path and uses Harbor's separate
verifier mode. The wrapper publishes only `base_sha.txt`, `candidate.patch`,
`result.json`, `metrics.json`, and the pre-run `run_manifest.json`; scheduler
packets and verification logs stay in the container's private runtime
directory.

## 12. Security model

1. Harbor is the supported benchmark isolation boundary.
2. Workers inside one Harbor trial share that trial's container resources.
3. Internal Git worktrees isolate concurrent edits, not the host from workers.
4. Hidden tests use Harbor's separate verifier environment.
5. Hidden verifier results are never passed back into the agent environment.
6. Visible verification is public worker feedback only.
7. Caller worker environment variables may contain authentication material and
   are never persisted or logged.
8. The orchestrator does not access the macOS Keychain.
9. The orchestrator is not a general-purpose local security sandbox.
10. Live workers directly on the host are trusted development mode only.

## 13. What's not here yet

- No `needs_human -> delivering` override (design.md's "manager overrides a
  failed verification" edge is specced but has no CLI path yet).
- No auth/access control — this assumes a single trusted operator on one box,
  per design.md's stated non-goals.
