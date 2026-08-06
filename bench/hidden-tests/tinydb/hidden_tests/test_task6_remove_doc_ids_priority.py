"""
Hidden tests for task: Table.remove() should prefer doc_ids over cond.

Seeded from the merged PR "Refactor Table.delete() to prefer argument
doc_ids over cond" (GitHub issue/PR #424). get()/contains()/update()
already prefer an explicit doc_ids argument over cond when both are
given; remove() is the odd one out. No tests were included in the linked
PR, so these are fresh, strictly behavior-based.
"""
from tinydb import TinyDB, where
from tinydb.storages import MemoryStorage


def make_db():
    db = TinyDB(storage=MemoryStorage)
    db.drop_tables()
    db.insert_multiple({'int': 1, 'char': c} for c in 'abc')
    return db


def test_remove_prefers_doc_ids_over_cond():
    db = make_db()

    # cond only matches doc_id 1 (char == 'a'); doc_ids explicitly asks
    # for doc_id 2. doc_ids should win, matching get()/contains()/update().
    removed = db.remove(cond=where('char') == 'a', doc_ids=[2])

    assert removed == [2]
    assert db.get(doc_id=2) is None
    assert db.get(doc_id=1) is not None
    assert len(db) == 2


def test_remove_by_doc_ids_alone_is_unaffected():
    db = make_db()

    removed = db.remove(doc_ids=[1, 3])

    assert sorted(removed) == [1, 3]
    assert len(db) == 1
    assert db.get(doc_id=2) is not None


def test_remove_by_cond_alone_is_unaffected():
    db = make_db()

    removed = db.remove(where('char') == 'b')

    assert removed == [2]
    assert len(db) == 2
