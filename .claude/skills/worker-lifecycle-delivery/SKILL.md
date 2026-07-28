---
name: worker-lifecycle-delivery
description: Worker session lifecycle (SDK spike questions, event mapping, worktree pool, intervention mechanics) and the three delivery modes (pr/local/scout). Use when touching worker spawning, the Agent SDK integration, or delivery/git-push code, or asked how sessions get created, interrupted, or how a task gets delivered.
---

# Worker lifecycle (to be detailed after M1 spike)

<!-- sync:worker-lifecycle -->
Sketch — the SDK spike (M1) answers the open questions before this section
gets fully specced:

- One Agent SDK session per task, cwd = a pooled git worktree, per-task
  permission policy.
- `worker.*` events map from: PostToolUse hook → `worker.tool_used`; result
  messages → `worker.messaged` / `worker.asked` / `worker.done_claimed`;
  session end → `worker.exited`.
- Done-claim detection protocol: TBD in M1 (likely a required final structured
  message or sentinel; do not rely on parsing prose).
- Intervention = injecting a message into the live session (supervisor
  nudge) or, once escalation has already torn the session down, requeuing
  with the intervention folded into the brief for a fresh one (manager
  answer via `orchestrator answer`, docs/usage.md). Logged as events either
  way; the orchestrator always knows a human intervened.
- Worktree pool: raw `git worktree`, ~50 lines, no treehouse dependency.
- Spike questions: does mid-session message injection work as assumed? cost
  granularity per message or per session? what does "done" look like in the
  stream? does PostToolUse fire for subagent tool calls (parent_tool_use_id)?
<!-- /sync:worker-lifecycle -->

# Delivery modes

<!-- sync:delivery-modes -->
Per-task `delivery_mode`, firstmate-style, explicit:

- `pr`: push branch, open PR via gh. Delivered = PR open. Merge is the
  manager's call; merge tracking is an event.
- `local`: approved fast-forward merge into the local default branch.
- `scout`: no push ever; report written to `data/<task_id>/report.md`.

Delivery failures (push rejected, conflict) → `delivery.failed` → triage.
<!-- /sync:delivery-modes -->
