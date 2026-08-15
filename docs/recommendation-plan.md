# Recommendation implementation plan

This plan follows the current benchmark evidence: the orchestrator is more
reliable than naive parallelism on the existing graph, but it is slower than
Sequential and the controlled-fault comparison did not reach its injected
node. The next milestone therefore improves causal measurement before adding
more scheduling intelligence.

## Milestone 1 — target-reachable recovery canary

Goal: make every intended fault cell prove that the injected task was actually
launched before interpreting recovery results.

Implementation:

- Add an explicit `fault_injection.target_reachable` contract.
- Require the target to exist and be a root task when that contract is enabled.
- Emit a durable `fault_injection.target_reached` event when the target worker
  is spawned, including target, mode, delay, and attempt identity.
- Export `fault_target_reached` and `fault_target` in run metrics.
- Add a deterministic FakeWorker test and a Harbor graph canary with a target
  root plus a dependent observer.
- Treat cells without the reachability event as invalid/inconclusive, never as
  fast failures.

Acceptance:

- Invalid target or non-root target is rejected before task insertion.
- Every intended fault cell has exactly one target-reached event.
- Recovery rate is calculated only over reached fault cells.
- Existing Sequential and naive-parallel policies remain unchanged.

## Milestone 2 — verification cost and delivery efficiency

Goal: reduce repeated work without weakening the final visible or hidden gate.

- Cache successful evidence by candidate SHA, command hash, and declared
  contract hash.
- Reuse protocol, Git, artifact, and targeted-test results when the inputs are
  identical; never reuse a result across a changed candidate or command.
- Add transactional delivery with explicit merge/rebase validation and a
  recoverable delivery checkpoint.
- Measure cache hits, merge retries, rollback time, and verification savings.

Acceptance: equal candidates do not rerun identical public checks; changed
candidates always invalidate the cache; delivery conflicts identify the first
bad node and preserve candidate lineage.

## Milestone 3 — resumable long-horizon execution

Goal: preserve verified progress across retries and daemon restarts.

- Persist per-node verified checkpoints and artifact digests.
- Resume from the latest valid checkpoint instead of replaying completed work.
- Add partial-progress and censored-runtime reports for long graphs.
- Add hidden cross-task interface checks and negative/mutation checks.

Acceptance: a restart or bounded recovery reruns only invalidated descendants;
partial progress is reported separately from final reward.

## Milestone 4 — calibration and benchmark proof

After Milestones 1–3, run at least 5–10 paired seeds across multiple graphs.
Use the resulting queue wait, contention, latency, verification, and cost data
to calibrate deterministic concurrency and verification thresholds. Keep all
policy inputs fixed and report successful runtime separately from censored
cells. Do not introduce an LLM into every scheduling decision until the
deterministic policy has a measured failure mode.

## Immediate implementation scope

This change starts Milestone 1 only. It does not alter the benchmark result or
claim superiority; it adds the reachability evidence needed to make the next
comparison interpretable.
