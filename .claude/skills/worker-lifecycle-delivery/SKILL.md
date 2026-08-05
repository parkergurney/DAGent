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
- Worktree escape is a two-layer defense, not one. The PreToolUse hook
  (`_path_escapes_worktree` in sdk_worker.py) denies escaping paths for
  structured file tools (Read/Edit/Write) only — it never inspected Bash,
  and batch01 dogfooding hit that gap twice: a worker ran `sed -i` against
  an absolute path in the main checkout (`~/Development/sqlite-utils`),
  dirtying it and causing unrelated tasks' `local` delivery to fail with
  `dirty_tree`. Claude Code's native OS-level Bash sandbox (Seatbelt on
  macOS, bubblewrap on Linux, v2.0.24+) closes that gap by restricting the
  Bash tool's *process*, not its declared intent — enforced by the kernel,
  so it holds regardless of what the model claims the command does. Workers
  set `sandbox.enabled=True` on `ClaudeAgentOptions`; the default write
  policy (cwd + subdirs + session temp dir) already matches worker cwd =
  worktree, so no custom path rules are needed. `sandbox.
  allowUnsandboxedCommands=False` makes the model's `dangerouslyDisableSandbox`
  escape hatch a no-op — commands can no longer opt back out. `sandbox.
  failIfUnavailable=True` turns a missing dependency or unsupported platform
  into a hard connect failure instead of the CLI's default warn-and-run-
  unsandboxed — sdk_worker.py catches that failure, emits a `worker.
  startup_failed` event (stderr is discarded by the spawning process, so
  this is the only way it reaches the operator), and exits rather than
  proceeding unsandboxed.
- Network denial does NOT come from `permission_mode`. Workers used
  `permission_mode="bypassPermissions"` pre-sandbox to avoid hanging on
  approval prompts headless sessions can't answer; with the sandbox in
  place that flag turned out to auto-grant the sandbox's own network-domain
  approval too — the SDK exposes "does this Bash command get to reach a new
  host" as a synthetic `SandboxNetworkAccess` tool call routed through the
  same decision pipeline as any other tool, and `bypassPermissions`
  auto-approves that pipeline wholesale. `sandbox.network.strictAllowlist=
  True` alone did nothing against it — verified live: a sandboxed `curl` to
  an unlisted host returned a real HTTP response under `bypassPermissions`
  despite `strictAllowlist`. Workers now pass `can_use_tool=_can_use_tool`
  instead of `permission_mode`: that callback denies exactly
  `tool_name == "SandboxNetworkAccess"` and allows everything else, which
  keeps sessions headless (no hang on a plain in-worktree Read/Edit/Write)
  while this codebase, not the CLI's blanket bypass, owns the one decision
  that has to stay a real deny. Re-verified live after the fix: the same
  `curl` now fails with a proxy-level 403 (`CONNECT tunnel failed`), fast
  and clean, no hang. `strictAllowlist` is left set as defense in depth in
  case `can_use_tool` isn't consulted in some future CLI path, but it is
  not what's doing the denying today. Workers don't need network anyway —
  verify/setup_cmd run outside the session, in the gate.
- Both denials — filesystem escape and network — were spot-checked against
  a negative control, not just observed once: disabling the mechanism under
  test (dropping `sandbox=` entirely for the filesystem case; forcing
  `_path_escapes_worktree` to always return `False` for the FakeWorker
  scenario) reproduces the original batch01 failure (the write succeeds),
  confirming the passing case denies for the right reason and isn't an
  artifact of the test environment.
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
