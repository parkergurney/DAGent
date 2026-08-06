"""
Hidden tests for task: doc_ids consistency for Table.update()/Table.remove().

Seeded from the merged fix (tinydb PR fixing GitHub issue #591).
"""
from tinydb import TinyDB, where
from tinydb.storages import MemoryStorage


def make_db():
    db = TinyDB(storage=MemoryStorage)
    db.drop_tables()
    db.insert_multiple({'int': 1, 'char': c} for c in 'abc')
    return db


def test_remove_ids_missing():
    db = make_db()
    assert db.remove(doc_ids=[99]) == []
    assert len(db) == 3


def test_remove_ids_mixed():
    db = make_db()
    assert sorted(db.remove(doc_ids=[1, 99])) == [1]
    assert len(db) == 2
    assert db.get(doc_id=1) is None
    assert db.get(doc_id=2) is not None


def test_update_ids_missing():
    db = make_db()
    assert db.update({'int': 9}, doc_ids=[99]) == []
    assert db.count(where('int') == 9) == 0
    assert db.count(where('int') == 1) == 3


def test_update_ids_mixed():
    db = make_db()
    assert sorted(db.update({'int': 9}, doc_ids=[1, 99])) == [1]
    assert db.count(where('int') == 9) == 1
    assert db.count(where('int') == 1) == 2


def test_doc_id_missing_consistency():
    db = make_db()
    assert db.get(doc_id=99) is None
    assert db.update({'int': 9}, doc_ids=[99]) == []
    assert db.remove(doc_ids=[99]) == []
