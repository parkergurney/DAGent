# Ollama Harbor pilot matrix — 2026-08-10

Status: complete.

This report records the first controlled Harbor pilot for the orchestrator
using local Ollama with `qwen3-coder:30b`. It is a boundary/pilot result, not a
meaningful concurrency comparison: the task package contained one task, so no
trial could occupy more than one worker slot.

## Controlled inputs

- Harbor: `0.20.0`
- Backend: Ollama, Anthropic-compatible local endpoint
- Model: `qwen3-coder:30b`
- Context: `32768`
- Worker concurrency ceiling: `2`
- Worker timeout: `1200s`
- Verifier: separate Harbor environment with hidden `tests/grader.py`
- Base SHA: `a463b345f0af92d91e5b1843edf1234adff3afe8`
- Task definition SHA256: `84fe6b710126894b92c1ecb0e7af5abf194a45ba4a7db9155ad90f07905be832`
- Authentication: Harbor-injected environment; no credential values recorded
- Actual API charge: `$0`; `worker_cost_usd` below is the SDK's local estimate

## Cell results

Every cell produced a candidate, passed the separate hidden verifier, and
ended in `delivered`. There were no Harbor exceptions, retries, dependency
blocks, or supervisor interventions.

| Policy | Seed | Harbor job | Reward | Worker seconds | Input tokens | Output tokens | Estimated cost | Peak workers |
|---|---:|---|---:|---:|---:|---:|---:|---:|
| sequential | 0 | `2026-08-10__19-55-59` | 1.0 | 220.202 | 21,646 | 375 | $0.1215 | 1 |
| sequential | 1 | `2026-08-10__20-20-17` | 1.0 | 239.372 | 25,512 | 448 | $0.1427 | 1 |
| sequential | 2 | `2026-08-10__20-24-56` | 1.0 | 242.766 | 25,589 | 437 | $0.1428 | 1 |
| naive-parallel | 0 | `2026-08-10__20-01-25` | 1.0 | 202.456 | 21,703 | 378 | $0.1219 | 1 |
| naive-parallel | 1 | `2026-08-10__20-29-39` | 1.0 | 230.704 | 24,919 | 280 | $0.1355 | 1 |
| naive-parallel | 2 | `2026-08-10__20-34-08` | 1.0 | 169.934 | 17,835 | 323 | $0.1012 | 1 |
| orchestrator | 0 | `2026-08-10__20-06-36` | 1.0 | 243.886 | 28,774 | 306 | $0.1555 | 1 |
| orchestrator | 1 | `2026-08-10__20-37-31` | 1.0 | 229.566 | 25,523 | 407 | $0.1417 | 1 |
| orchestrator | 2 | `2026-08-10__20-41-54` | 1.0 | 199.408 | 21,641 | 373 | $0.1215 | 1 |

## Policy averages

| Policy | Reward rate | Avg worker seconds | Avg input tokens | Avg output tokens | Avg estimated cost |
|---|---:|---:|---:|---:|---:|
| sequential | 3/3 | 234.1 | 24,249 | 420 | $0.1357 |
| naive-parallel | 3/3 | 201.0 | 21,486 | 327 | $0.1195 |
| orchestrator | 3/3 | 224.3 | 25,313 | 362 | $0.1396 |

These averages should not be interpreted as evidence that naive parallelism is
faster. With one task, `sequential`, `naive-parallel`, and `orchestrator` all
execute one worker; the observed differences are model/runtime variation.
The orchestrator also made zero supervisor calls because no trial encountered
a failure requiring triage.

## Artifact locations

Each job directory contains Harbor's result and the transferred agent/verifier
artifacts. For example:

```text
old/jobs/2026-08-10__20-41-54/orchestrator-canary__hzd2NYH/
```

The relevant files are:

- `artifacts/logs/artifacts/run_manifest.json`
- `artifacts/logs/artifacts/result.json`
- `artifacts/logs/artifacts/metrics.json`
- `artifacts/logs/artifacts/candidate.patch`
- `verifier/reward.txt`

## Decision and next benchmark

The Harbor boundary and one-task pilot are validated. The next benchmark
should use a multi-task graph with both independent branches and dependent
fan-in, for example:

```text
schema ───────┐
              ├── integration ── release-check
implementation┘

documentation  (independent branch)
```

This structure exercises concurrency between `schema`, `implementation`, and
`documentation`, then tests dependency settlement and recovery at
`integration` and `release-check`.

Build that package before drawing conclusions about policy performance. Run a
one-seed canary for all three policies first, then repeat the controlled
3-policy × 3-seed matrix with the same graph, base SHA, model, limits, and
verifier.
