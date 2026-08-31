# Benchmarking

How DAGent is measured, how to reproduce a run, and what the results
have been so far.

The question under test is whether deterministic orchestration beats simpler
policies at getting a graph of coding tasks actually finished.
Three policies run over identical machinery, so a comparison isolates the
scheduling strategy rather than the harness:

- `sequential` - one task at a time, no recovery.
- `naive-parallel` - concurrent workers, no dependency settlement or recovery.
- `dagent` - the full state machine, verify gate, and bounded recovery.

Evaluation is deliberately split from execution.
DAGent sees only public, worker-visible repository state; hidden tests
and scoring run in a separate verifier environment, and hidden results never
enter the agent environment or influence a scheduler decision.

## Metrics

The primary outcome is **verified task completion rate**:

```text
verified_task_completion_rate = delivered_tasks / manifest_task_count
```

`delivered` means the candidate passed the visible verification gate and the
configured delivery step.
The denominator is the fixed task count in the experiment manifest, not the
number of tasks that happened to start - so failed, cancelled, escalated, and
dependency-blocked tasks stay visible as incomplete outcomes instead of
quietly leaving the denominator.
Every cell reports numerator, denominator, and terminal-state counts alongside
the rate.

Secondary metrics, all defined before any experiment ran:

| Metric | Definition |
| --- | --- |
| Terminal-state counts | Tasks in each terminal or unresolved state: `delivered`, `failed`, `needs_human`, `cancelled`, `dependency_blocked`, plus any remaining nonterminal state. |
| Wall time | End-to-end cell duration, run start to settlement. |
| Worker execution time | Sum of worker start-to-end durations across attempts. |
| Queue wait | Time tasks waited from runnable/queued until acquiring a worker slot. |
| Attempts and retries | Worker attempts, recovery attempts, and retry count, reported separately from task count. |
| Verification failures | Failed visible verification attempts, with failure class. |
| Supervisor interventions | Number and duration of supervisor decisions, including triage time. |
| Tokens | Input and output tokens for workers and supervisor, when the backend reports them. |
| Estimated cost | Worker plus supervisor cost at the cell's fixed price/model configuration. |
| Peak concurrency | Maximum simultaneously occupied worker slots, against the configured limit. |
| Validity status | `successful`, `failed`, `censored`, or `inconclusive`. |

These are exported durably as `metrics.json` per cell.
Any summary must retain the raw metrics and the manifest that produced them.

### Cell validity

The rules that keep a comparison honest, fixed in advance:

- A fault target that was never launched is `inconclusive`, not a failure.
- A failed cell's short runtime is `censored` - it is not evidence of speed.
- Backend, model, task package, graph, verifier, authentication mechanism,
  concurrency limit, and resource configuration stay fixed within a policy
  comparison.
- Hidden verifier output is never copied into worker prompts, visible evidence,
  or scheduler decisions.
- Reports rank verified outcome quality first, then cost and latency.

`dagent-report` enforces this split: outcome quality counts successful
and settled failed cells, while runtime overhead counts successful cells only.

## Running a benchmark

### Prepare the matrix

Fixed inputs are committed under [`benchmarks/package`](benchmarks/package/).
Validate and enumerate the task graphs, seeded fault profiles, policies, and
backend tracks:

```bash
dagent-experiment prepare --output-dir results/matrix
```

### Run one cell

```bash
dagent-experiment run --graph wide --policy orchestrator --seed 0 \
  --profile clean --backend-track cloud-claude \
  --repo-root /path/to/throwaway-repo --output-dir results/cell-01

dagent-report results/cell-01 --output-dir results/summary
```

Each cell writes `run_manifest.json`, `metrics.json`, `result.json`,
`task_summary.json`, and `candidate.patch`.
`dagent-report` aggregates saved cells into `report.json` and
`report.md`.

Fault profiles are deterministic FakeWorker cells and cost nothing.
A live backend requires the explicit `--live` flag and a trusted container
boundary; local Ollama cells are reported as a separate resource-contention
track, since a local model's throughput is itself a contended resource.

### Execution track

The dependency-aware launcher lives in
[`harbor/tasks/orchestrator-dag-canary-claude/`](harbor/tasks/orchestrator-dag-canary-claude/).
It takes `ORCH_GRAPH_SHAPE=serial|wide|diamond|mixed`, uses the committed graph
topologies and the separate verifier, and records the selected shape and graph
hash in the manifest.
It publishes only `base_sha.txt`, `candidate.patch`, `result.json`,
`metrics.json`, and the pre-run `run_manifest.json`; scheduler packets and
verification logs stay in the container's private runtime directory.

### Semantic-quality track

The execution track uses exact-file tasks, which measure whether the machinery
completes work but not whether the work is any good.
The quality track runs real Arrow, JSONSchema, and TinyDB maintenance tasks
against pinned source fixtures.
Workers get source code and public tests; the recovered hidden tests are copied
only into the separate verifier.

Source repositories must be present at `../bench-dirs` relative to this
checkout, or set `ORCH_QUALITY_SOURCE_ROOT` to their parent directory.
The builder verifies the pinned commits and refuses dirty or mismatched
fixtures.

```bash
# One task-level canary first.
ORCH_AUTH_ENV_FILE="$AUTH_FILE" \
ORCH_QUALITY_TASK=arrow-shift-check-imaginary \
harbor/tasks/orchestrator-quality-claude/run_canary.sh \
  orchestrator 0 claude-sonnet-4-6 task

# Then the three-task calibration matrix.
ORCH_AUTH_ENV_FILE="$AUTH_FILE" \
ORCH_QUALITY_TASKS="arrow-shift-check-imaginary jsonschema-hostname-single-label tinydb-lru-cache-set-update" \
harbor/tasks/orchestrator-quality-claude/run_benchmark.sh 0
```

Each job stores `verifier/quality_metrics.json`, whose fractional
`quality_score` is also written to `verifier/reward.txt`.
Reports keep that score in its own table - hidden-test quality is not the same
thing as orchestrator completion or latency, and merging them would hide which
one moved.

Multi-task graph modes are available through `ORCH_QUALITY_GRAPH_SHAPE` with
explicit `ORCH_QUALITY_TASKS`.
Wide graphs are rejected when their declared write scopes overlap, unless
`ORCH_QUALITY_ALLOW_UNSAFE_WIDE=1` is set.

Each quality job also publishes a credential-redacted
`artifacts/logs/artifacts/tool_audit.jsonl` recording tool calls and flagging
anything that looks like web, package-install, or Git-history access.
It is an audit trail, not network enforcement - the container still allows
network access outside the denied tool patterns.

```bash
jq -r 'select(.likely_network_or_history_attempt) |
  [.timestamp, .task_id, .tool, .target, .decision] | @tsv' \
  jobs/<job>/artifacts/logs/artifacts/tool_audit.jsonl
```

### Other tools

```bash
# Run the public verification command for a durable task candidate.
dagent-verify-gate --task <task_id> --db data/dagent.db --json --record

# Re-run a saved triage packet against the CURRENT supervisor prompt/model,
# to iterate on heuristics without paying for a live run.
dagent-supervisor-replay data/<task_id>/packets/<seq>.json --model claude-sonnet-5
```