-- Source of truth for storage. Applied by store/db.py at startup.
-- Invariant: tasks is a derived cache of events; replay(events) == tasks.

CREATE TABLE IF NOT EXISTS tasks (
  id            TEXT PRIMARY KEY,
  title         TEXT NOT NULL,
  brief         TEXT NOT NULL,
  repo          TEXT NOT NULL,
  delivery_mode TEXT NOT NULL CHECK (delivery_mode IN ('pr','local','scout')),
  verify_cmd    TEXT,
  state         TEXT NOT NULL DEFAULT 'blocked' CHECK (state IN
    ('blocked','queued','running','verifying','triage','needs_human',
     'delivering','delivered','failed','cancelled','dependency_blocked')),
  retries       INTEGER NOT NULL DEFAULT 0,
  max_retries   INTEGER NOT NULL DEFAULT 2,
  worktree      TEXT,
  session_id    TEXT,
  base_sha      TEXT,
  run_id        TEXT,
  current_attempt_id TEXT,
  candidate_sha TEXT,
  candidate_branch TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_deps (
  task_id    TEXT NOT NULL REFERENCES tasks(id),
  depends_on TEXT NOT NULL REFERENCES tasks(id),
  PRIMARY KEY (task_id, depends_on)
);

CREATE TABLE IF NOT EXISTS events (
  seq        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  task_id    TEXT,
  source     TEXT NOT NULL CHECK (source IN
    ('scheduler','worker','watchdog','verifier','supervisor','delivery','human','system')),
  type       TEXT NOT NULL,
  payload    TEXT NOT NULL DEFAULT '{}',
  session_id TEXT,
  tokens_in  INTEGER,
  tokens_out INTEGER,
  cost_usd   REAL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, seq);

-- An attempt is the durable unit of worker execution.  Its Git ref is never
-- a pooled worktree slot: the slot is disposable, while this ref is the
-- candidate lineage used by retries and reconciliation.
CREATE TABLE IF NOT EXISTS attempts (
  id                    TEXT PRIMARY KEY,
  task_id               TEXT NOT NULL REFERENCES tasks(id),
  run_id                TEXT NOT NULL,
  attempt_no            INTEGER NOT NULL,
  parent_attempt_id     TEXT REFERENCES attempts(id),
  base_sha              TEXT NOT NULL,
  candidate_sha         TEXT,
  candidate_branch      TEXT NOT NULL,
  worker_dirty          TEXT,
  failure_cause         TEXT,
  failure_signature     TEXT,
  supervisor_feedback   TEXT,
  execution_contract    TEXT NOT NULL,
  worker_started_at     TEXT,
  worker_ended_at       TEXT,
  verification_started_at TEXT,
  verification_ended_at TEXT,
  supervisor_started_at TEXT,
  supervisor_ended_at   TEXT,
  disposition           TEXT NOT NULL DEFAULT 'created',
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  UNIQUE(task_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS idx_attempts_task ON attempts(task_id, attempt_no);
CREATE INDEX IF NOT EXISTS idx_attempts_parent ON attempts(parent_attempt_id);

-- One row per actual supervisor model intervention. Deterministic policy
-- decisions are events, not interventions, so they cannot be mistaken for
-- model overhead.
CREATE TABLE IF NOT EXISTS supervisor_interventions (
  id                         TEXT PRIMARY KEY,
  task_id                    TEXT NOT NULL REFERENCES tasks(id),
  source_attempt_id          TEXT REFERENCES attempts(id),
  source_candidate_sha       TEXT,
  source_failure_signature   TEXT,
  action_type                TEXT,
  diagnosis_code             TEXT,
  worker_instruction         TEXT,
  target_attempt_id          TEXT REFERENCES attempts(id),
  child_candidate_sha        TEXT,
  child_failure_signature    TEXT,
  eventual_delivery_outcome TEXT,
  verification_recovery_outcome TEXT,
  tokens_in                  INTEGER,
  tokens_out                 INTEGER,
  cost_usd                   REAL,
  started_at                 TEXT NOT NULL,
  ended_at                   TEXT,
  created_at                 TEXT NOT NULL,
  updated_at                 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_interventions_task
  ON supervisor_interventions(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_interventions_target
  ON supervisor_interventions(target_attempt_id);
