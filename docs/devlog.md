# Devlog

A few lines per working session. What was decided, what surprised you, what
the agent nailed or fumbled. This file writes the Substack post.

## 2026-07-18
- Consolidated design into docs/design.md (state machine, event schema,
  supervisor contract, verify gate contract, benchmark plan, milestones).
- Repo scaffolded. Next: M0 - schema + event store + replay invariant test.

## 2026-07-19
- M0 landed: storage layer only, stdlib-only.
  store/db.py (connect + schema bootstrap, WAL, foreign_keys),
  store/events.py (append_event, create_task, transition, replay, vendored
  ULID, state machine), config.py, and the invariant test suite.
- Trimmed pyproject to M0 reality: dropped claude-agent-sdk / pydantic /
  python-ulid / pytest-asyncio and the not-yet-existing console scripts.
  Runtime deps come back with the milestones that need them. Bumped
  requires-python to 3.12.
- ULID is ~3 lines vendored (Crockford base32 over a 128-bit int), not a
  dependency. 1000-id uniqueness check in the suite.
- Design call for replay fidelity: the write path records into event payloads
  exactly what replay needs to reconstruct - task.created carries the full
  static task definition, task.state_changed carries {from,to,cause_seq} plus
  any mutated columns (session_id, worktree, retries). Both the row write and
  its event share a single timestamp so updated_at matches under replay.
  That is what makes replay(events) == tasks bit-for-bit, not just
  structurally.
- transition() is the sole state writer and validates against the section 4
  edge table; "any -> cancelled" is a rule, not 9 table rows. It also refuses
  to set columns outside a small allowlist, so it can never smuggle a
  non-state write past the event log.
- Surprise: nothing hairy. The one thing worth remembering is the shared-ts
  trick above; without it updated_at drifts by microseconds and the invariant
  fails intermittently.
- Kept the empty future-milestone package dirs (verify/, scheduler/, ...) since
  they match the README layout; did not add code to them. Exit criterion green:
  8 tests pass, replay(events) == tasks asserted.

## 2026-07-19 - M1: SDK spike

Spike script at spike/m1_spike.py (throwaway, keep for reference); full event log in spike/m1_spike_log.jsonl.
Three runs against real claude-haiku-4-5 sessions in a throwaway git worktree, ~$0.06-0.09 per full run.
SDK: claude-agent-sdk 0.2.123 (Python), driving Claude Code CLI 2.1.215.

The four section 8 questions, answered:

1. Mid-session injection: YES.
   Calling client.query() on a live ClaudeSDKClient while a turn is running delivers the message into the running turn.
   The worker reacted within ~1s of the in-flight tool call completing, folded the nudge into its plan, and produced a single ResultMessage (no second turn; 60s of post-result silence confirmed nothing was queued).
   Supervisor nudge = client.query() on the held session handle. No workaround needed.

2. Cost granularity: tokens per message, dollars per turn-run.
   Every AssistantMessage carries a usage dict (input/output tokens, cache_creation, cache_read, inference_geo), including subagent messages.
   cost_usd exists only on ResultMessage (total_cost_usd, plus a per-model model_usage breakdown and a per-iteration usage list).
   So worker.* events can carry token counts per event; cost lands once per completed turn.

3. Done in the stream: ResultMessage.
   Shape: {subtype: "success"|..., is_error, num_turns, stop_reason: "end_turn", result: <final assistant text verbatim>, usage, total_cost_usd, session_id, duration_ms}.
   A sentinel final line (we used "DONE_CLAIM: <word>") surfaces verbatim in ResultMessage.result, so done-claim detection can be a cheap parse of result rather than prose-scraping mid-stream.
   Recommendation for section 8: require a sentinel line in the brief and parse ResultMessage.result; worker.exited maps to the ResultMessage itself.

4. PostToolUse for subagent tool calls: YES.
   The hook fires for tools run inside Task-spawned subagents, with agent_id set (absent on main-thread calls).
   Subagent AssistantMessages also carry parent_tool_use_id pointing at the spawning Agent tool_use id.
   Full attribution of worker.tool_used to main thread vs subagent is free.

Surprises:

- The worktree cwd is NOT a boundary.
  Haiku workers wrote answer.txt to $HOME in two runs and foo.txt to /tmp in another, despite cwd being set to the worktree.
  Consequences: the brief must pin the absolute worktree path; consider a PreToolUse hook that denies writes outside the worktree; the verify gate's empty_diff check would have caught all three cases.
- One Write in run 2 produced no PostToolUse event while runs 1 and 3 had full hook coverage.
  Treat the hook stream as advisory rather than guaranteed-complete; the watchdog already derives stall from absence of events, which is the right posture.
- PostToolUseFailure is a separate hook event and must be mapped too, or failed tool calls vanish from the event log.
- SystemMessage subtypes (task_started, task_progress, task_notification, thinking_tokens) are free extra liveness signals for the watchdog.
- HookMatcher with no matcher string matches all tools; hook callbacks get cwd, session_id, transcript_path, duration_ms in the payload, which covers everything the worker.tool_used payload needs.
