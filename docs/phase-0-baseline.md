# Phase 0 baseline

Baseline name: `phase-0-reliability-2026-08-15`

This is the reproducible snapshot taken before reliability features or new
benchmark inputs are added. The named baseline commit contains the current
implementation, the recommendation plan, and this evidence record. Recreate
it with:

```sh
git show --stat <baseline-commit>
.venv/bin/pytest -q
git diff --check
```

## Validation

The complete local validation suite is the repository's default pytest suite;
opt-in live SDK tests are deliberately excluded because they require real API
access and are nondeterministic.

Command:

```sh
.venv/bin/pytest -q
```

Result: `211 passed, 4 skipped in 94.29s` (2026-08-15).

The repository must also have no whitespace errors:

```sh
git diff --check
```

## Outcome metric

The primary outcome is **verified task completion rate**:

```text
verified_task_completion_rate = delivered_tasks / manifest_task_count
```

`delivered` means the candidate passed the visible verification gate and the
configured delivery step. The denominator is the fixed number of tasks in the
experiment manifest, not the number of tasks that happened to start. Failed,
cancelled, escalated, and dependency-blocked tasks therefore remain visible
as incomplete outcomes rather than disappearing from the denominator.

For every cell, report the numerator, denominator, and terminal-state counts
alongside the rate. Harbor's hidden verifier result remains a separate
evaluation result and never enters worker context or changes this metric's
definition.

## Secondary metrics

These are defined before experiments begin. Durations are in seconds, counts
are integers, and costs are estimates in USD.

| Metric | Definition |
| --- | --- |
| Terminal-state counts | Number of tasks in each terminal or unresolved state: `delivered`, `failed`, `needs_human`, `cancelled`, `dependency_blocked`, and any remaining nonterminal state. |
| Wall time | End-to-end cell duration from run start to run settlement. |
| Worker execution time | Sum of worker start-to-end durations across attempts. |
| Queue wait | Time tasks waited from becoming runnable/queued until acquiring a worker slot. |
| Attempts and retries | Worker attempts, recovery attempts, and retry count, reported separately from task count. |
| Verification failures | Count of failed visible verification attempts, with failure class. |
| Supervisor interventions/time | Number and duration of supervisor decisions, including triage time. |
| Tokens | Input and output tokens for workers and supervisor, when the backend reports them. |
| Estimated cost | Worker plus supervisor cost, using the fixed backend price/model configuration for the cell. |
| Peak concurrency | Maximum simultaneously occupied worker slots and configured worker limit. |
| Validity status | `successful`, `failed`, `censored`, or `inconclusive`, determined by the rules below. |

The durable implementation currently exports these concepts through
`metrics.json`, including queue wait, worker execution, verification,
supervisor, attempts, recovery, token, cost, concurrency, failure-class, and
fault-target fields. New experiment summaries must retain the raw metrics and
the manifest used to produce them.

## Cell validity rules

- A fault target that was never launched is `inconclusive`, not a failure.
- A failed cell's short runtime is `censored`; it is not a successful-runtime
  speed observation.
- Backend, model, task package, graph, verifier, authentication mechanism,
  concurrency limit, and resource configuration remain fixed within a policy
  comparison.
- Hidden verifier output is retained by Harbor and is not copied into worker
  prompts, visible evidence, or scheduler decisions.
- Reports rank verified outcome quality first, then cost and latency.
