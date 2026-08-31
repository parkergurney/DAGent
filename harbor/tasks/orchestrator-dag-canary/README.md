# Dependency-aware Harbor benchmark package

This is the first meaningful concurrency benchmark package for the
orchestrator. Its public graph has three independent roots, a fan-in
integration task, and a final release task. The `local` delivery mode is
intentional: every delivered dependency is merged into `main` before its
children are eligible, so the graph tests both scheduling and dependency
settlement.

The hidden verifier is under `tests/` and is transferred only to Harbor's
separate verifier environment. The agent image contains the same clean Git
baseline and public task instructions, but no hidden grader code.

The launcher accepts `dag` (the original five-node canary) plus the benchmark
shapes `serial`, `wide`, `diamond`, and `mixed`. Shape cells use ten distinct
public output artifacts, so the same hidden verifier can validate any
selected shape.

Run one canary per policy first on one shape:

```sh
ORCH_GRAPH_SHAPE=serial harbor/tasks/orchestrator-dag-canary/run_canary.sh sequential 0
ORCH_GRAPH_SHAPE=serial harbor/tasks/orchestrator-dag-canary/run_canary.sh naive-parallel 0
ORCH_GRAPH_SHAPE=serial harbor/tasks/orchestrator-dag-canary/run_canary.sh orchestrator 0
```

After all three pass, run the controlled matrix with seeds 0, 1, and 2. The
launcher records the package hash, graph hash, model, context, policy, seed,
and Harbor version in `run_manifest.json` before workers start. It also emits
`task_summary.json` with credential-free per-node state and failure categories
for diagnosing a failed cell.

For a controlled fault cell, set `ORCH_FAULT_TASK` to a root task and enable
the reachability contract:

```sh
ORCH_GRAPH_SHAPE=serial ORCH_FAULT_TASK=serial-00 ORCH_TARGET_REACHABLE=1 \
  harbor/tasks/orchestrator-dag-canary/run_canary.sh orchestrator 0
```

The resulting manifest records the contract and the metrics must contain
`fault_target_reached: true`. A cell without that event is inconclusive and
must not be counted as a recovery failure or a fast runtime observation.

To run the complete fail-fast sequence—local validation, three clean policy
cells, and three target-reachable fault cells—execute:

```sh
ORCH_BENCHMARK_GRAPHS="serial wide diamond" \
  harbor/tasks/orchestrator-dag-canary/run_benchmark.sh 0
```

The script stops on any local-test failure, Harbor command failure, invalid
clean result, or fault cell that does not prove target reachability. Baseline
fault cells are allowed to end in failure because that is the recovery outcome
being measured. Run several paired seeds with, for example,
`ORCH_BENCHMARK_SEEDS="0 1 2 3 4"`. Add `mixed` when the initial matrix is
stable: `ORCH_BENCHMARK_GRAPHS="serial wide diamond mixed"`.
