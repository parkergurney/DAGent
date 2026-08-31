# Benchmarking DAGent

The benchmark tests whether deterministic orchestration completes more of a
coding-task graph than simpler scheduling policies under matched conditions.

## Policies

All policies use the same task package, worker backend, verifier, resource
limits, and artifact format.

- `sequential`: one worker at a time; no recovery.
- `naive-parallel`: concurrent workers; no dependency settlement or recovery.
- `orchestrator`: DAGent state machine, verify gate, dependency settlement, and
  bounded recovery.

The policy is the independent variable. Backend, model, graph, verifier,
authentication, concurrency limit, and resource configuration remain fixed
within a comparison.

## Evaluation boundary

Workers receive source code and public tests. Hidden tests and scoring run in a
separate verifier environment. Hidden results are not sent to workers or used
by the scheduler or supervisor.

Visible verification and hidden evaluation measure different outcomes:

- Visible verification determines whether DAGent may deliver a candidate.
- Hidden evaluation measures task quality without entering the control loop.

## Metrics

The primary metric is verified task completion rate:

```text
verified_task_completion_rate = delivered_tasks / manifest_task_count
```

The denominator is fixed by the experiment manifest. Failed, cancelled,
escalated, and dependency-blocked tasks remain incomplete outcomes.

Secondary metrics:

| Metric | Definition |
|---|---|
| Terminal-state counts | Counts for `delivered`, `failed`, `needs_human`, `cancelled`, `dependency_blocked`, and remaining states |
| Wall time | Cell start to settlement |
| Worker execution time | Sum of worker durations across attempts |
| Queue wait | Runnable-to-worker-slot delay |
| Attempts and retries | Worker attempts, recovery attempts, and retries |
| Verification failures | Failed visible verification attempts by class |
| Supervisor interventions | Decision count, duration, and triage time |
| Tokens | Worker and supervisor input/output tokens when reported |
| Estimated cost | Worker and supervisor cost at the fixed model configuration |
| Peak concurrency | Maximum occupied worker slots |
| Validity status | `successful`, `failed`, `censored`, or `inconclusive` |

Each cell writes `metrics.json`. Aggregated reports retain the cell manifest and
raw metrics.

## Validity rules

- A fault target that never launched is `inconclusive`.
- Runtime from a failed cell is `censored` and is not treated as a speed result.
- Outcome reports include numerators, denominators, and terminal-state counts.
- Runtime overhead is calculated only from successful cells.
- Hidden verifier results never enter prompts, visible evidence, or scheduler
  decisions.
- Verified outcome quality is reported before cost and latency.

`dagent-report` enforces the outcome/runtime split.

## Reproducing a benchmark

Fixed inputs are under [`benchmarks/package`](benchmarks/package/).

Prepare the experiment matrix:

```bash
dagent-experiment prepare --output-dir results/matrix
```

Run and report one cell:

```bash
dagent-experiment run \
  --graph wide \
  --policy orchestrator \
  --seed 0 \
  --profile clean \
  --backend-track cloud-claude \
  --repo-root /path/to/throwaway-repo \
  --output-dir results/cell-01

dagent-report results/cell-01 --output-dir results/summary
```

Cell outputs:

- `run_manifest.json`
- `metrics.json`
- `result.json`
- `task_summary.json`
- `candidate.patch`

Fault profiles use deterministic FakeWorker scenarios. Live backends require
`--live` and an external isolation boundary. Local Ollama results remain in a
separate resource-contention track because local model throughput competes for
the same machine resources as orchestration.

## Execution track

The dependency-aware Harbor launcher is in
[`harbor/tasks/orchestrator-dag-canary-claude/`](harbor/tasks/orchestrator-dag-canary-claude/).

Supported graph shapes:

```text
serial
wide
diamond
mixed
```

The launcher records the graph shape and graph hash in the pre-run manifest.
Published outputs are:

- `base_sha.txt`
- `candidate.patch`
- `result.json`
- `metrics.json`
- `run_manifest.json`

Scheduler packets and verification logs remain in the container's private
runtime directory.

## Semantic-quality track

The execution track uses exact-file tasks and measures orchestration
completion. The semantic-quality track uses maintenance tasks from pinned
Arrow, JSONSchema, and TinyDB fixtures. Workers receive source and public tests;
recovered hidden tests remain in the verifier.

Fixtures must be available at `../bench-dirs`, or their parent directory must
be supplied through `ORCH_QUALITY_SOURCE_ROOT`. The builder rejects dirty or
mismatched source commits.

Run a task-level canary:

```bash
ORCH_AUTH_ENV_FILE="$AUTH_FILE" \
ORCH_QUALITY_TASK=arrow-shift-check-imaginary \
harbor/tasks/orchestrator-quality-claude/run_canary.sh \
  orchestrator 0 claude-sonnet-4-6 task
```

Run the three-task calibration matrix:

```bash
ORCH_AUTH_ENV_FILE="$AUTH_FILE" \
ORCH_QUALITY_TASKS="arrow-shift-check-imaginary jsonschema-hostname-single-label tinydb-lru-cache-set-update" \
harbor/tasks/orchestrator-quality-claude/run_benchmark.sh 0
```

Each job writes `verifier/quality_metrics.json` and a fractional score to
`verifier/reward.txt`. Quality scores and orchestration completion rates are
reported separately.

Multi-task quality graphs use `ORCH_QUALITY_GRAPH_SHAPE` and an explicit
`ORCH_QUALITY_TASKS` list. Wide graphs are rejected when declared write scopes
overlap unless `ORCH_QUALITY_ALLOW_UNSAFE_WIDE=1` is set.

Each job also publishes a credential-redacted tool audit at:

```text
artifacts/logs/artifacts/tool_audit.jsonl
```

The audit flags likely web access, package installation, and Git-history
access. It records activity but does not enforce isolation.

```bash
jq -r 'select(.likely_network_or_history_attempt) |
  [.timestamp, .task_id, .tool, .target, .decision] | @tsv' \
  jobs/<job>/artifacts/logs/artifacts/tool_audit.jsonl
```

## Supporting tools

Run visible verification for a stored candidate:

```bash
dagent-verify-gate \
  --task <task_id> \
  --db data/dagent.db \
  --json \
  --record
```

Replay a saved supervisor packet against the current prompt and model:

```bash
dagent-supervisor-replay \
  data/<task_id>/packets/<seq>.json \
  --model claude-sonnet-5
```

## Current results

### Local test baseline

```bash
pytest -q
git diff --check
```

Recorded baseline: `252 passed, 4 skipped`. Live SDK tests are excluded because
they require external authentication and are nondeterministic.

### Local Ollama boundary pilot

The first controlled pilot used Harbor `0.20.0` and local Ollama with
`qwen3-coder:30b`, context length 32768, worker concurrency 2, and a 1200-second
worker timeout. A separate verifier ran hidden `tests/grader.py`. Base commit
and task-definition hashes were fixed.

The package contained one task. All policies therefore executed one worker and
could not exercise dependency settlement or parallel scheduling. Every cell
produced a candidate, passed hidden verification, and reached `delivered` with
no retries, dependency blocks, supervisor calls, or exceptions.

| Policy | Delivered | Avg worker seconds | Avg input tokens | Avg output tokens | Avg estimated cost |
|---|---:|---:|---:|---:|---:|
| sequential | 3/3 | 234.1 | 24,249 | 420 | $0.1357 |
| naive-parallel | 3/3 | 201.0 | 21,486 | 327 | $0.1195 |
| orchestrator | 3/3 | 224.3 | 25,313 | 362 | $0.1396 |

The pilot validates the execution and isolation boundary. It is not a policy
comparison. Runtime differences in this single-task setup are model and runtime
variation.

## Required next experiment

The policy comparison requires a multi-task graph with independent branches
and dependent fan-in:

```text
schema ───────┐
              ├── integration ── release-check
implementation┘

documentation  (independent)
```

The planned sequence is one canary for each policy followed by a fixed
3-policy × 3-seed matrix. Graph, base commit, model, concurrency, limits, and
verifier remain fixed. Until this matrix is complete, the repository does not
support a policy-performance claim.
