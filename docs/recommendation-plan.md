# Reliability-first implementation and evaluation plan

Status: implementation and local validation first; benchmark execution is the
final gate.

## Objective

Turn the current strong systems project into a defensible AI-infrastructure
project by proving a small number of difficult claims:

> The orchestrator executes dependency graphs across isolated Git worktrees,
> persists state, verifies candidates, recovers from worker failures, and can
> deliver accepted changes. Under controlled failures, supervised orchestration
> trades additional latency and compute for higher completion reliability than
> naive concurrency.

The project does not need more orchestration features unless a failed proof
step demonstrates that one is necessary. The target is evidence, not feature
count.

## Current position

Already present:

- Event-sourced SQLite state and replay checks.
- Dependency-aware scheduling and terminal dependency blocking.
- Git worktree isolation and pooled worker slots.
- Claude Agent SDK workers plus deterministic FakeWorker scenarios.
- Candidate lineage, verification, delivery modes, and durable metrics.
- Supervisor recovery actions with bounded retries and escalation.
- Harbor boundary integration and separate hidden verification.
- Execution leases, workflow preflight, conflict-aware admission, and an
  evidence ladder in the current implementation.

Known evidence limits:

- The existing Harbor matrix has only three seeds, one graph, one model, and
  one machine.
- The orchestrator completed 3/3 cells, but sequential also completed 3/3 and
  was faster on that graph.
- The naive-parallel runtime average is censored by early failures.
- The controlled-fault follow-up did not always reach its injected task, so it
  cannot establish a recovery-rate comparison.
- Local Ollama inference introduces hardware contention that can be confused
  with scheduler behavior.

## The sequence

### Phase 0 — Freeze the claim and establish the baseline

Do this before adding code.

1. Commit the current implementation and documentation as a named baseline.
2. Run the complete local validation suite and record its result.
3. Draw one concise architecture diagram covering scheduler, worker lease,
   candidate lineage, verification, supervisor, delivery, and Harbor.
4. Write down the primary outcome metric: verified task completion rate.
5. Define secondary metrics before experiments begin:
   completion state, wall time, worker execution time, queue wait, attempts,
   retries, verification failures, supervisor interventions/time, tokens,
   estimated cost, peak concurrency, and censored/inconclusive status.

Exit criteria:

- The baseline commit is reproducible.
- The README can explain the system and its security boundary in one page.
- No new feature is accepted without being tied to a proof gap below.

### Phase 1 — Finish the deterministic reliability contract

Use FakeWorker and local subprocesses first. Do not spend model/API budget on
these cases.

Add or confirm deterministic profiles for:

1. Worker crash midway through a task.
2. Worker timeout or silent stall.
3. Worker exits without a candidate.
4. Worker leaves a dirty worktree.
5. Worker produces a committed but verification-failing patch.
6. Verification rejects a candidate.
7. Worker dies after modifying its worktree but before normal teardown.
8. A dependency task fails and downstream tasks settle as blocked.
9. Duplicate process-exit, watchdog, and recovery signals.
10. A stale attempt emits output after a replacement attempt owns the lease.

Keep fault injection small and explicit. A seeded profile should select the
task, failure mode, timing, and attempt—not embed a large random simulation
framework in the scheduler.

Exit criteria:

- Every profile has a deterministic test and a clear expected terminal state.
- A stale generation cannot mutate current state, attempts, or candidate refs.
- Recovery is idempotent under duplicate signals.
- `replay(events)` matches live task state after every profile.
- Sequential and naive-parallel policies remain valid unchanged baselines.

### Phase 2 — Demonstrate restart recovery

This is the highest-value systems demonstration and should precede any large
benchmark.

The reproducible operator procedure is in
[docs/phase-2-exit-runbook.md](phase-2-exit-runbook.md). It separates the
deterministic checkpoint assertions from the literal process-kill transcript
required by this phase.

Create a ten-node DAG using FakeWorker or a deterministic local worker. Start
the run, kill the orchestrator process at several checkpoints, restart it with
the same database, and let it settle.

Checkpoints should include:

- Before a worker starts.
- During a running worker.
- After a candidate is committed but before verification.
- During verification.
- During triage/recovery.
- After some dependencies have delivered and others remain queued.

The restart must preserve or correctly reconstruct:

- Completed and in-flight task state.
- Worker lease ownership and stale-process rejection.
- Candidate SHA and attempt lineage.
- Valid verification evidence.
- Eligible queued tasks and dependency-blocked descendants.
- Delivery state and review artifacts.

Exit criteria:

- The same run can be killed and resumed without corrupting the repository.
- Completed work is not unnecessarily repeated.
- No duplicate delivery or recovery action is produced.
- A short screen recording or terminal transcript can demonstrate the kill /
  restart behavior.

If verified checkpoints are not sufficient to avoid replaying completed work,
implement only the smallest checkpoint mechanism needed for this demonstration.
Do not build general distributed recovery.

### Phase 3 — Make measurement trustworthy

Before any expensive run, make the metrics and experiment validity rules
explicit in code and manifests.

The implementation lives in `orchestrator.harbor_runtime` and
`orchestrator.experiment`. Each cell publishes a manifest, metrics, result,
and task summary; `orchestrator-report` aggregates saved cells while applying
the validity rules below in code.

Every cell must record:

- Policy, seed, graph identifier, task-package hash, base SHA, model/backend,
  context length, concurrency limit, and resource configuration.
- Task completion and terminal-state counts.
- Wall-clock, queue wait, worker execution, verification, and supervisor time.
- Worker attempts, retries, verification failures, interventions, tokens, and
  estimated cost.
- Fault profile, intended target, target-reached evidence, and failure class.
- Whether the cell is successful, failed, censored, or inconclusive.

Rules:

- A fault cell whose target was never launched is inconclusive, not a failure.
- A failed cell's short runtime is censored and cannot be compared to a
  successful cell as a speed result.
- Hidden verifier results stay in Harbor and never enter the worker context.
- Policy inputs, model, graph, resource limits, and authentication mechanism
  stay fixed within a comparison.
- The primary report ranks verified outcome quality first, then cost/latency.

Exit criteria:

- A saved manifest and metrics file are sufficient to reproduce every table.
- A report can separate outcome quality from orchestration overhead.
- Invalid and censored cells are excluded by code, not editorial judgment.

Example report command:

```sh
orchestrator-report old/jobs/*/artifacts --output-dir results/summary
```

### Phase 4 — Validate real backends without benchmarking

Run only smoke tests here. These are integration checks, not performance
experiments.

1. Run one small real Claude Agent SDK task against a throwaway repository.
2. Run one real supervisor recovery decision.
3. If local inference is part of the final story, run one Ollama canary with
   fixed context and concurrency settings.
4. Confirm PR/local/scout delivery and review artifacts on the intended path.
5. Confirm the Harbor agent can publish a candidate patch and metrics without
   exposing hidden verifier material.

Use the live tests only when needed:

```sh
ORCH_LIVE_SDK_TESTS=1 .venv/bin/pytest -q tests/integration
```

They cost real API money and are nondeterministic. They should prove backend
connectivity and lifecycle compatibility, not serve as the benchmark.

Exit criteria:

- At least one real Claude run completes or escalates safely.
- At least one real supervisor call returns a valid closed action.
- The local-model path, if used, has a documented resource profile.
- No backend-specific failure is being misclassified as a scheduler failure.

### Phase 5 — Prepare the benchmark package, but do not run it yet

Implementation: `benchmarks/phase5/` contains the fixed task package, four
ten-node graph shapes, seeded profiles, and separate cloud/local backend
tracks. `orchestrator-experiment prepare` validates and enumerates the matrix;
`orchestrator-experiment run` executes one cell (FakeWorker by default), and
`orchestrator-report` summarizes saved artifacts with comparison-input checks.

Build all experiment inputs and reporting code before spending benchmark time.

Use a small, fixed task repository with approximately ten tasks and several
graph shapes:

- Serial chain: little available parallelism.
- Wide graph: many independent roots.
- Diamond/fan-in graph: concurrency followed by dependency settlement.
- Mixed graph: independent roots, fan-in, fan-out, and a longer critical path.

Keep task file scopes distinct in the scheduling suite so accidental merge
conflicts do not become an uncontrolled variable. Add a separate conflict-heavy
case only when intentionally measuring conflict-aware admission.

Prepare deterministic profiles for:

- Worker crash.
- Worker timeout.
- No candidate.
- Invalid or verification-failing candidate.
- Dependency failure.
- Worker latency distribution.

Each profile must be seeded and target-reachable. Keep the current three policy
conditions:

- `sequential`
- `naive-parallel`
- `orchestrator`

Prepare two backend tracks instead of mixing them:

1. Cloud Claude track: measures orchestration behavior with inference capacity
   supplied remotely.
2. Local Ollama track: measures orchestration plus constrained inference
   capacity and must be reported as a separate resource-contention experiment.

Do not change model, context, task package, verifier, or resource limits inside
one comparison matrix.

Exit criteria:

- One command can run a single cell and one command can summarize cells.
- A dry-run or FakeWorker execution exercises the complete reporting path.
- The benchmark protocol states the primary metric and exclusion rules before
  any real cells are launched.

### Phase 6 — Final benchmark gate

Only start this phase after Phases 0–5 are complete and the repository is on a
clean, committed snapshot.

Run in this order:

1. One clean canary for each policy on one graph and seed.
2. One target-reachable fault canary for each policy.
3. A small paired matrix across at least three graph shapes and five seeds.
4. Expand to ten seeds only if the first matrix is stable and the compute
   budget supports it.
5. Repeat the selected matrix on the separate backend track if comparing cloud
   and local inference.

Do not launch the large matrix if the canary is invalid, the target is not
reachable, the resource profile changes, or the local suite fails.

Report at minimum:

- Verified completion probability versus injected failure probability.
- Verified completion probability versus cost.
- Latency versus graph width/concurrency.
- Retry and verification behavior versus failure probability.
- Successful runtime separately from censored runtime.
- Resource contention separately from orchestration policy.

The likely useful conclusion is not that the orchestrator always wins. It may
show that sequential execution is preferable for narrow or reliable workloads,
while supervised orchestration becomes worthwhile once graph width or worker
failure rates cross a threshold. That is a stronger and more credible systems
result.

### Phase 7 — Publish and stop adding features

After the final benchmark:

1. Write one results document with methodology, exclusions, raw artifact paths,
   aggregate tables, and limitations.
2. Add the architecture diagram and restart-recovery demo instructions to the
   README.
3. Add one reproducible command for a small FakeWorker experiment.
4. Preserve benchmark artifacts locally under the ignored archive directory;
   commit portable manifests, summaries, and conclusions.
5. Stop feature work unless a reviewer identifies a concrete correctness gap.

## Work that is explicitly out of scope

Do not spend the next cycle on distributed execution, multi-user auth,
Kubernetes, billing, many worker providers, a SaaS dashboard, enterprise
permissions, or automatic production merging. None is needed to establish the
core claim and each creates a new surface that would dilute the evidence.

## Definition of done

The project is resume-ready when it has:

1. Deterministic fault profiles and seeded target reachability.
2. Sequential, naive-concurrent, and supervised baselines.
3. Several DAG shapes.
4. Durable process-death and restart-recovery evidence.
5. Completion, latency, cost, retry, verification, and contention metrics.
6. One real Claude backend smoke test.
7. Separate local-inference resource-contention results.
8. One concise architecture diagram.
9. One command reproducing a small experiment.
10. A results document explaining when orchestration helps and when it does
    not.
11. A clean repository that another engineer can run.

At that point, the research question is more valuable than another thousand
lines of orchestration code:

> Agent concurrency does not necessarily increase coding throughput. Worker
> reliability, dependency structure, verification cost, retry policy, and
> inference capacity jointly determine the useful level of parallelism.
