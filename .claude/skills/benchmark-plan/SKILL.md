---
name: benchmark-plan
description: Benchmark conditions, workload, metrics, rigor, and scope-cutting levers for evaluating this system against baselines. Use when touching the benchmark harness, grading logic, or asked about eval conditions, metrics, or what to cut if the calendar slips.
---

# Benchmark plan

<!-- sync:benchmark-plan -->
Conditions (identical model, pinned version in config day one):

- (a) single Claude Code session, tasks sequential
- (b) naive parallel: N independent headless sessions, no supervision
- (c) firstmate
- (d) this system

All graded by the verify-gate CLI. (a)+(b) run first: they calibrate task
difficulty (if naive-parallel resolves 90%, the suite is too easy and the
comparison is dead) and exercise the harness before (d) exists.

Workload: multi-task batches. Both of: a subset of SWE-bench Verified grouped
by repo (parallel batches force worktree contention; free test-based grading;
note contamination caveats honestly) and 2-3 seeded repos with 10-20
hand-written issues, each with a hidden verification test.

Metrics (all SQL over events):

- verified resolution rate (tests pass, not self-report)
- wall-clock per batch; throughput (tasks/hour)
- total cost, split worker vs supervision overhead
- human interventions count; escalation precision (were interruptions
  warranted; did manager pick `recommended`)
- fault recovery: kill a worker mid-task every run; does the system recover
  without losing the task
- gaming attempts (`protected_path_modified` count) per condition
- flake rate per repo (covariate + finding)

Rigor: 3-5 seeds per condition, mean + spread, publish harness, configs, and
full transcripts. Estimate API cost before launching eval runs.

Ablation slots (cheap, thanks to architecture): supervisor model swap
(Haiku vs Sonnet) via packet replay — action agreement rate + cost delta;
supervision context serialization (JSON vs TOON) if overhead is non-trivial.

Scope levers if the calendar slips, in order: cut condition (c), cut the DAG,
shrink the task suite. NEVER cut seeds-per-condition.
<!-- /sync:benchmark-plan -->
