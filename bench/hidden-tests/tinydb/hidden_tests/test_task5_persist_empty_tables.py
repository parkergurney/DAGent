"""
Hidden tests for task: allow a table to be persisted even with no documents.

Seeded from the merged fix (tinydb PR fixing GitHub issue #513).
"""
from tinydb import TinyDB
from tinydb.storages import MemoryStorage


def test_persist_table():
    db = TinyDB(storage=MemoryStorage)

    db.table("persisted", persist_empty=True)
    assert "persisted" in db.tables()

    db.table("nonpersisted", persist_empty=False)
    assert "nonpersisted" not in db.tables()
