---
name: verify-gate
description: The deterministic visible verification contract and failure-signature rules. Use when touching verify-gate code or debugging a verify failure.
---

# Verify gate contract

The verify gate is deterministic and contains no LLM or evaluator-only logic.
Harbor owns hidden tests and scoring. The gate turns a worker's committed
candidate into public evidence, a normalized failure signature, and a patch.
Visible verification inherits the agent environment and is not a host sandbox;
benchmark use requires Harbor or another trusted outer isolation boundary.

```python
class VerifyRequest:
    task_id: str
    worktree: str
    base_sha: str
    verify_cmd: str
    timeout_s: int = 600
    rerun_on_fail: bool = True
    repo: str | None = None
    candidate_sha: str | None = None
    worker_dirty: str | None = None
    artifact_root: str | None = None

class VerifyResult:
    passed: bool
    cause: Literal[
        "tests_passed", "tests_failed", "timeout", "candidate_checkout_failed",
        "uncommitted_changes", "empty_diff",
    ]
    exit_code: int | None
    duration_s: float
    flaky: bool
    output_tail: str
    diff_stat: str
    tests_modified: list[str]
    output_path: str
    patch_path: str | None
    failure_signature: str | None
```

Execution is: dirty-worktree and empty-diff checks, patch export, materialize
the durable candidate in an internal disposable checkout when needed, run the
public command with a timeout, and rerun one failure to identify flakes.
The candidate checkout is not given evaluator-only material and is removed
afterward. Timeout cleanup kills the check's process group.

| cause | supervisor heuristic |
|---|---|
| tests_failed | restart with output; equivalent signatures escalate |
| uncommitted_changes | nudge |
| empty_diff | restart with a pointed commit/change reminder |
| timeout | inspect duration and transcript |
| candidate_checkout_failed | escalate as infrastructure failure |

Events are `verify.started`, then `verify.passed` or `verify.failed` with the
cause, duration, output/patch paths, and failure signature. Verification attempt
counts remain available to generic experiment metrics.
