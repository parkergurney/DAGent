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
    return conn
