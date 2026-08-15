# Orchestrator improvement program

This is the prioritized improvement backlog for making the orchestrator more
reliable, efficient, and scientifically defensible. It records the rationale,
scope, and acceptance bar so implementation and benchmark claims stay aligned.

## Current evidence

The local benchmark history shows two recurring problems: root-task protocol
failures can prevent the intended injected fault from becoming reachable, and
failed cells can appear faster than successful cells unless runtime is treated
as censored. The current v2 work addresses typed recovery, interface gates,
adaptive scheduling, and durable attribution, but it does not yet fence stale
workers, preflight graph conflicts, or stage verification by cost.

## Priority 1 — execution leases and fencing

Add a monotonically increasing execution generation to every task attempt.
Workers, watchdogs, reconcilers, and recovery actions must carry
`(task_id, attempt_id, execution_generation)` and be rejected when they no
longer own the current lease. Persist lease acquisition, renewal, expiry, and
release events. Recovery must be idempotent under duplicate process-exit
events and late worker output.

Acceptance:

- stale worker output cannot change current task state or candidate pointers;
- duplicate watchdog/reconciler events produce one recovery action;
- lease loss is distinguishable from worker failure;
- restart tests cover crashes before and after lease release;
- replay and live state remain equal.

## Priority 2 — workflow preflight and conflict-aware planning

Before launching workers, validate the task DAG and compile a public execution
plan containing artifact contracts, file scopes, likely write conflicts,
critical-path depth, verification cost, and resource estimates. Serialize
conflict-heavy tasks even when the dependency graph allows parallelism. Reject
ambiguous contracts before consuming worker tokens, while preserving the
sequential and naive-parallel policies as comparison baselines.

Acceptance:

- malformed or ambiguous contracts fail before worker spawn;
- overlapping write scopes produce a durable conflict decision;
- high-conflict tasks are serialized and independent tasks remain parallel;
- the plan, inputs, and selected decisions are in the run manifest;
- preflight failures identify the first invalid node and only block affected
  descendants.

## Priority 3 — targeted verification and evidence ladder

Stage public verification from cheap evidence to expensive evidence:

1. process/result/protocol checks;
2. Git cleanliness and candidate existence;
3. artifact and schema validation;
4. changed-file or targeted tests;
5. full visible verification;
6. hidden evaluation owned by Harbor.

Use changed paths and normalized failure signatures to select targeted checks.
Reserve full verification and any model-based semantic review for candidates
that pass cheaper gates or remain ambiguous. Preserve every evidence step and
its cost in durable events and metrics.

Acceptance:

- obvious protocol, dirty-tree, artifact, and scope failures stop early;
- targeted checks run before full verification when mappings exist;
- a failed targeted check yields bounded repair feedback;
- full verification remains the final public gate;
- verification time, attempt count, and recovery attribution are reportable.

## Later improvements

- Independent semantic invariants and negative/mutation checks for silent
  wrong-but-plausible patches.
- Reversible checkpoints and selective rollback of invalidated descendants.
- Explicit provenance binding for every delegation, tool action, candidate, and
  recovery generation, plus counterfactual attribution reports.
- Long-horizon benchmark graphs with guaranteed fault reachability, partial
  progress scoring, paired seeds, confidence intervals, conflict-heavy nodes,
  and censored-runtime reporting.
- Adaptive model/workflow routing only after deterministic preflight and
  evidence ladders have stable measurements.

## Research basis

- [Self-Healing Agentic Orchestrators](https://arxiv.org/abs/2606.01416):
  failure-aware, budgeted, verification-guided recovery.
- [Meta-Agent](https://arxiv.org/abs/2605.25233): explicit DAG contracts,
  verification criteria, and local/upstream/structural attribution.
- [LLM-as-Scheduler](https://aclanthology.org/2026.acl-long.581/): lightweight
  gates and runtime signals before expensive workflow routing.
- [E2EDevBench](https://arxiv.org/abs/2511.04064): requirement omission and
  inadequate self-verification as major coding-agent bottlenecks.
- [SWE-EVO](https://arxiv.org/abs/2512.18470) and
  [RoadmapBench](https://arxiv.org/abs/2605.15846): short coding tasks can
  overstate long-horizon software-engineering capability.
- [Long-Horizon Agent Trajectory Attribution](https://arxiv.org/abs/2608.06909)
  and [Delegated Execution Observability](https://arxiv.org/abs/2606.09692):
  timestamped logs alone are insufficient for reliable responsibility
  attribution.
