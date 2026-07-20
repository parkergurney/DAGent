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
