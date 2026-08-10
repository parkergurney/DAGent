---
name: worker-lifecycle-delivery
description: Worker session lifecycle, internal worktree pooling, intervention mechanics, and delivery modes. Use when touching worker spawning or delivery/git code.
---

# Worker lifecycle

<!-- sync:worker-lifecycle -->
Worker lifecycle is implemented by the SDK worker and scheduler:

- One Agent SDK session per task, cwd = a pooled internal Git worktree.
- `worker.*` events map from hooks and structured result messages; session end
  maps to `worker.exited`.
- Done-claim detection uses a required sentinel in the final structured result.
- Intervention is a live stdin message for nudge, or a fresh retry with folded
  feedback after escalation. Every intervention is logged.
- The worktree pool is internal worker isolation and remains even when Harbor
  supplies the outer task container. Attempt refs preserve candidate lineage;
  pooled checkout slots remain disposable.
- The orchestrator does not provide OS-level host isolation. Real workers
  require Harbor/another trusted outer boundary or explicit trusted host
  development mode; the latter is never benchmark isolation.
- Real and fake workers share a JSON-lines protocol. The caller supplies worker
  environment variables; the launcher never reads credentials or a Keychain.
- The SDK worker's path hook rejects structured file-tool paths outside its
  assigned worktree. Harbor owns broader task isolation and hidden evaluation.
- A done claim is followed by process-group termination/reaping before the
  candidate is verified. Scheduler teardown is idempotent and closes child
  transports, releases the worktree slot, and preserves the candidate ref.
<!-- /sync:worker-lifecycle -->

# Delivery modes

<!-- sync:delivery-modes -->
Per-task `delivery_mode` remains explicit:

- `pr` — push branch and open a PR; delivered means PR open.
- `local` — approved fast-forward merge into the local default branch.
- `scout` — no push; write a report for investigation tasks.

Delivery failures (`delivery.failed`) route through supervisor triage.
<!-- /sync:delivery-modes -->
