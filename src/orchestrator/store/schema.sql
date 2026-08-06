-- Source of truth for storage. Applied by store/db.py at startup.
-- Invariant: tasks is a derived cache of events; replay(events) == tasks.

CREATE TABLE IF NOT EXISTS tasks (
  id            TEXT PRIMARY KEY,
  title         TEXT NOT NULL,
  brief         TEXT NOT NULL,
  repo          TEXT NOT NULL,
  delivery_mode TEXT NOT NULL CHECK (delivery_mode IN ('pr','local','scout')),
  verify_cmd    TEXT,
  hidden_cmd    TEXT,
  setup_cmd     TEXT,
  protected_paths TEXT,        -- JSON array of opt-in globs; NULL = no protected paths
  state         TEXT NOT NULL DEFAULT 'blocked' CHECK (state IN
    ('blocked','queued','running','verifying','triage','needs_human',
     'delivering','delivered','failed','cancelled')),
  retries       INTEGER NOT NULL DEFAULT 0,
  max_retries   INTEGER NOT NULL DEFAULT 2,
  worktree      TEXT,
  session_id    TEXT,
  base_sha      TEXT,
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
