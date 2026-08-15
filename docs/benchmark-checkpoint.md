# Benchmark checkpoint: controlled fault-recovery sequence

Updated 2026-08-11. The active Harbor trial has finished. No Harbor, Claude,
orchestrator, or Ollama worker processes remain. Do not start another trial
from this checkpoint.

The preserved local job artifacts live under `old/jobs/` and are intentionally
gitignored; the committed benchmark notes below are the portable record.

## Already preserved

- No-fault dependency-DAG baseline: recorded in `docs/devlog.md`.
- Naive-parallel fault baseline: `old/jobs/2026-08-11__09-22-57/`.
  It completed in 23m46s with reward 0.0 and no Harbor exception.
- Sequential fault baseline: `old/jobs/2026-08-11__09-01-10/`.
- Pre-improvement orchestrator fault cell: `old/jobs/2026-08-11__09-48-00/`.
  It reached Harbor's one-hour agent timeout (`AgentTimeoutError`) after the
  supervisor restarted the injected worker failure. Its manifest records
  `deterministic_crash_recovery: false`.
- Post-improvement orchestrator fault cell: `old/jobs/2026-08-11__10-49-42/`.
  It completed in 42m41s with reward 0.0 and no Harbor exception, but an
  unrelated schema worker failed twice before the injected `integration` task
  became runnable. The new fast path was not exercised; do not use this cell
  as a recovery comparison.
- The implementation change is in `src/orchestrator/scheduler/core.py` and is
  enabled only for the orchestrator policy. A non-zero worker exit with retry
  budget remaining is retried through the normal candidate-lineage path without
  an LLM supervisor call. Sequential and naive-parallel remain unchanged.
- Focused tests pass. The final local suite is green: `181 passed, 4 skipped`
  in 31.07s.

## Completed active work

The completed post-improvement orchestrator Harbor trial used:

- policy: `orchestrator`
- seed: `0`
- model: `qwen3-coder:30b` via Ollama
- context: 32K
- concurrency ceiling: 2
- injected fault: non-zero worker exit on `integration`
- deterministic crash recovery: enabled
- Harbor agent timeout: one hour

The process completed and its artifacts are preserved. The post-cell summary
is `needs_human`: implementation and documentation delivered; schema needed
human review; integration and release-check were dependency-blocked.

## Immediate handoff

1. Inspect the post-cell `result.json`, the trial `result.json`,
   `artifacts/logs/artifacts/run_manifest.json`, `metrics.json`,
   `task_summary.json`, and `verifier/reward.txt`.
2. Inspect `exception.txt` if present.
3. Confirm all Harbor containers, Claude workers, and orchestrator processes
   have exited. Do not delete the job directory.
4. Add the post-improvement result and the inconclusive before/after
   interpretation to
   `docs/devlog.md`.
5. Run the complete local suite:

   ```sh
   .venv/bin/pytest -q
   git diff --check
   ```

6. Commit the implementation and documentation changes. Keep benchmark job
   directories as local evidence even if they are not committed to Git.
7. It is then safe to stop Ollama, Docker Desktop, or the laptop.

## Resume sequence

On a later session, start here:

```sh
cd /Users/parkergurney/Development/agent-orchestrator
git status --short
harbor view jobs
```

Read this file, `docs/devlog.md`, and the saved job manifests before running
anything. The next experiment should be chosen from evidence:

- Compare the saved cells only as lifecycle evidence. The post cell did not
  reach the injected fault, so it cannot establish a recovery improvement.
- Do not increase complexity or launch the 9-cell matrix yet. First reduce
  model/runtime noise or use a smaller controlled fault task whose target is
  guaranteed to run, while keeping the same task package and manifest controls.
- Do not claim an orchestrator improvement from reward alone. Separate verified
  outcome quality from supervisor cost, worker time, queue wait, retries,
  recovery outcome, and Harbor timeout behavior.
