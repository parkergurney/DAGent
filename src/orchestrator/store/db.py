"""SQLite connection + schema bootstrap.

The schema in schema.sql is the source of truth for storage. connect() applies
it idempotently (CREATE ... IF NOT EXISTS), so opening an existing db is a no-op.
"""
import sqlite3
from pathlib import Path

_SCHEMA = Path(__file__).with_name("schema.sql").read_text()


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # no-op on :memory:, real on file
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply small additive migrations and remove retired task fields.

    schema.sql remains the fresh-database source of truth; this only covers
    columns added after early local DBs already existed.
    """
    task_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    for name, sql_type in (
        ("run_id", "TEXT"),
        ("current_attempt_id", "TEXT"),
        ("candidate_sha", "TEXT"),
        ("candidate_branch", "TEXT"),
    ):
        if name not in task_cols:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {sql_type}")
    attempt_cols = {row["name"] for row in conn.execute("PRAGMA table_info(attempts)")}
    if "worker_dirty" not in attempt_cols:
        conn.execute("ALTER TABLE attempts ADD COLUMN worker_dirty TEXT")

    # SQLite cannot alter a CHECK constraint or drop columns in place. Older
    # local databases may have either; rebuild the derived task cache while
    # retaining all durable task state and the append-only event log.
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
    ).fetchone()["sql"]
    legacy_columns = {"hidden_cmd", "setup_cmd", "protected_paths"} & task_cols
    if table_sql and ("dependency_blocked" not in table_sql or legacy_columns):
        conn.commit()
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("""
            CREATE TABLE tasks_migrated (
              id TEXT PRIMARY KEY,
              title TEXT NOT NULL,
              brief TEXT NOT NULL,
              repo TEXT NOT NULL,
              delivery_mode TEXT NOT NULL CHECK (delivery_mode IN ('pr','local','scout')),
              verify_cmd TEXT,
              state TEXT NOT NULL DEFAULT 'blocked' CHECK (state IN
                ('blocked','queued','running','verifying','triage','needs_human',
                 'delivering','delivered','failed','cancelled','dependency_blocked')),
              retries INTEGER NOT NULL DEFAULT 0,
              max_retries INTEGER NOT NULL DEFAULT 2,
              worktree TEXT,
              session_id TEXT,
              base_sha TEXT,
              run_id TEXT,
              current_attempt_id TEXT,
              candidate_sha TEXT,
              candidate_branch TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            INSERT INTO tasks_migrated
            SELECT id, title, brief, repo, delivery_mode, verify_cmd, state, retries, max_retries,
                   worktree, session_id, base_sha, run_id, current_attempt_id,
                   candidate_sha, candidate_branch, created_at, updated_at
            FROM tasks
        """)
        conn.execute("DROP TABLE tasks")
        conn.execute("ALTER TABLE tasks_migrated RENAME TO tasks")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
