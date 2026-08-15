---
name: storage-schema
description: SQLite schema (tasks, task_deps, events tables) and the full event-type taxonomy for this orchestrator. Use when touching persistence code, writing migrations, adding a new event type, or asked about the events table, task columns, or how token/cost accounting rides on events.
---

# Storage schema

<!-- sync:storage-schema -->
```sql
CREATE TABLE tasks (
  id            TEXT PRIMARY KEY,        -- ULID, sortable by creation time
  title         TEXT NOT NULL,
  brief         TEXT NOT NULL,           -- the worker's prompt
  repo          TEXT NOT NULL,
  delivery_mode TEXT NOT NULL,           -- 'pr' | 'local' | 'scout'
  verify_cmd    TEXT,                    -- null for scout
  output_artifacts TEXT,                  -- public JSON declaration
  output_schema TEXT,                     -- public schema/required fields
  input_contract TEXT,                    -- dependency inputs required by node
  node_verify_cmd TEXT,                   -- optional public node gate
  repair_policy TEXT,                     -- bounded recovery policy metadata
  state         TEXT NOT NULL DEFAULT 'blocked',
  retries       INTEGER NOT NULL DEFAULT 0,
  max_retries   INTEGER NOT NULL DEFAULT 2,
  worktree      TEXT,
  session_id    TEXT,
  base_sha      TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE task_deps (
  task_id    TEXT NOT NULL REFERENCES tasks(id),
  depends_on TEXT NOT NULL REFERENCES tasks(id),
  PRIMARY KEY (task_id, depends_on)
);

CREATE TABLE events (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,  -- global monotonic order
  ts         TEXT NOT NULL,
  task_id    TEXT,                    -- null = team-level event
  source     TEXT NOT NULL,           -- scheduler|worker|watchdog|verifier|supervisor|delivery|human|system
  type       TEXT NOT NULL,           -- dotted domain.verb, past tense
  payload    TEXT NOT NULL DEFAULT '{}',  -- FLAT json, no nesting
  session_id TEXT,
  tokens_in  INTEGER,                 -- null except on LLM-touching events
  tokens_out INTEGER,
  cost_usd   REAL
);
CREATE INDEX idx_events_task ON events(task_id, seq);
```

## Event taxonomy

```
task.created          task.state_changed      dep.satisfied       dep.blocked
worker.spawned        worker.tool_used        worker.messaged
worker.asked          worker.done_claimed     worker.exited
worker.stalled        (watchdog only)      worker.startup_failed
verify.started        verify.passed           verify.failed
supervisor.invoked    supervisor.acted        supervisor.failed
delivery.started      delivery.pr_opened      delivery.merged_local
delivery.report_written                       delivery.failed
human.messaged        human.approved          human.cancelled
system.started        system.reconciled
```

- `worker.tool_used` comes from a PostToolUse hook; log EVERY call but keep the
  payload minimal (tool name, target, duration_ms). Highest-volume event by
  ~100x; tool-call counts per task are an experiment metric.
- `worker.startup_failed` fires when the worker's own connect step raises
  before any session starts. Distinct from `worker.exited` (a session that
  started and then crashed or finished without a claim).
- `verify.failed` payload includes a normalized failure signature (last
  assertion line, stripped of addresses/line numbers) so "same failure twice"
  is a cheap comparison, not vibes.
- Token counts ride on `worker.*` and `supervisor.*` events. Supervision
  overhead = `SELECT SUM(cost_usd) FROM events WHERE source='supervisor'`.
- Full logs/transcripts go to disk under `data/<task_id>/...`, referenced by
  path. Not in SQLite.
<!-- /sync:storage-schema -->
