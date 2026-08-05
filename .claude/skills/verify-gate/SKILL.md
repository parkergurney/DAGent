---
name: verify-gate
description: The verify-gate contract - VerifyRequest/VerifyResult, execution order (preflight, baseline, run, flake+hidden check), and the cause->supervisor heuristic table. Use when touching verify-gate code, debugging a verify failure cause, or asked how "done" claims get checked.
---

# Verify gate contract

<!-- sync:verify-gate -->
Boring, deterministic, paranoid. No LLM anywhere in it. Converts "done" claims
into evidence; its failure taxonomy is what makes the supervisor smart.

Also a standalone CLI (`verify-gate --task <id> --json`) so the benchmark
harness grades ALL conditions — including non-orchestrated baselines — with
identical machinery.

```python
class VerifyRequest(BaseModel):
    task_id: str
    worktree: str
    base_sha: str
    verify_cmd: str                 # visible to the worker
    hidden_cmd: str | None          # NOT in the brief; benchmark/paranoia
    setup_cmd: str | None           # cached per repo
    timeout_s: int = 600
    protected_paths: list[str]      # globs the worker may not modify (existing files only)
    rerun_on_fail: bool = True      # flake detection

class VerifyResult(BaseModel):
    passed: bool
    cause: Literal[
        "tests_passed",
        "tests_failed", "hidden_tests_failed",
        "timeout", "setup_failed",
        "uncommitted_changes", "empty_diff",
        "protected_path_modified",
        "baseline_broken",
    ]
    exit_code: int | None
    duration_s: float
    flaky: bool                     # failed once, passed on rerun
    output_tail: str                # ~2k chars, feeds the supervisor packet
    diff_stat: str
    tests_modified: list[str]
    output_path: str                # full logs on disk
```

## Execution order (cheapest first)

1. **Preflight (git, ms):** dirty worktree → `uncommitted_changes` (supervisor
   nudges "commit and re-claim"). Empty diff on a ship task → `empty_diff`
   (hallucinated completion). Diff modifies (edits, deletes, or renames) a
   file that already existed at base_sha and matches `protected_paths` →
   `protected_path_modified` — the anti-gaming check; an agent that can't make
   the tests pass will make the tests different. New files under
   protected_paths are exempt — a brand-new test is a contribution, not
   gaming — so TDD-shaped tasks need no opt-out; only edits to pre-existing
   tests are gated. Default protected_paths to the test dirs verify_cmd
   exercises.
2. **Baseline (cached on (repo, base_sha, verify_cmd, setup_cmd)):** run setup+verify on
   base_sha itself. Baseline red → `baseline_broken` → escalate, never retry.
   No number of retries fixes a repo whose tests were already failing; without
   this check a flaky upstream test burns the whole retry budget for nothing.
3. **The run:** setup_cmd (own cause — env problem ≠ code problem), then
   verify_cmd under timeout, worker session inactive. Kill the process GROUP
   on timeout; test runners orphan children.
4. **Flake protocol + hidden check:** fail → rerun once. Fail-fail →
   `tests_failed`. Fail-pass → PASSED with `flaky=true` (don't burn retries on
   nondeterminism the worker didn't cause) — but log loudly; flake rate per
   repo is a benchmark covariate and a finding. If visible passed, run
   hidden_cmd. `hidden_tests_failed` restart feedback must NOT leak hidden
   output — say the change didn't hold up under additional checks, without
   revealing which. Otherwise hidden tests train the worker to overfit them.

## Cause → supervisor heuristic

| cause | heuristic |
|---|---|
| tests_failed | restart w/ output_tail; same signature twice → escalate |
| hidden_tests_failed | restart w/ non-revealing feedback; twice → escalate |
| uncommitted_changes | nudge |
| empty_diff | restart, pointed "you changed nothing" |
| protected_path_modified | restart "revert X, solve without editing existing tests"; also a benchmark metric (gaming attempts per condition) |
| baseline_broken, setup_failed | escalate, never retry |
| timeout | ambiguous — supervisor reads duration vs baseline + transcript |

Events: `verify.started`, then passed/failed with payload
`{cause, exit_code, duration_s, flaky, rerun_count, failure_signature}`.
<!-- /sync:verify-gate -->
