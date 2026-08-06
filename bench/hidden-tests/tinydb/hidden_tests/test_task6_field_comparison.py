"""
Hidden tests for task: field-to-field query comparison.

Seeded from GitHub issue #574 ("comparing two columns leads to always
True"). No merged code fix exists upstream (only a documentation note was
added); these tests are fresh and strictly behavior-based.
"""
from tinydb import TinyDB, Query
from tinydb.storages import MemoryStorage


def make_db():
    db = TinyDB(storage=MemoryStorage)
    return db


def test_field_to_field_equality_compares_actual_values():
    db = make_db()
    db.insert({'name': 'bob', 'belly': 'blown'})
    db.insert({'name': 'dylan', 'belly': 'flat'})
    db.insert({'name': 'inflated', 'belly': 'inflated'})

    result = db.search(Query().belly == Query().name)

    assert result == [{'name': 'inflated', 'belly': 'inflated'}]


def test_field_to_field_equality_missing_field_does_not_match():
    db = make_db()
    db.insert({'a': 1})
    db.insert({'a': 1, 'b': 1})

    result = db.search(Query().a == Query().b)

    assert result == [{'a': 1, 'b': 1}]


def test_field_to_field_equality_type_mismatch_does_not_crash_or_match():
    db = make_db()
    db.insert({'a': 1, 'b': '1'})

    result = db.search(Query().a == Query().b)

    assert result == []


def test_field_to_field_equality_nested_paths():
    db = make_db()
    db.insert({'x': {'y': 5}, 'z': 5})
    db.insert({'x': {'y': 5}, 'z': 6})

    result = db.search(Query().x.y == Query().z)

    assert result == [{'x': {'y': 5}, 'z': 5}]
