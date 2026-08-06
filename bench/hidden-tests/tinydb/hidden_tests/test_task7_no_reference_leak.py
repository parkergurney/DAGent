"""
Hidden tests for task: TinyDB must not retain live references to
caller-supplied nested mutable data (and must not hand out live references
to its own internal storage either).

Seeded from GitHub issue #551 ("MemoryStorage incorrectly keeps references
to nested dicts"). No merged code fix exists upstream (issue was closed as
inactive); these tests are fresh and strictly behavior-based.
"""
from tinydb import TinyDB, Query
from tinydb.storages import MemoryStorage


def make_table():
    db = TinyDB(storage=MemoryStorage)
    return db.table('t', cache_size=0)


def test_insert_does_not_retain_reference_to_source_dict():
    table = make_table()
    obj = {'a': 'a', 'nested': {'c': 'c'}}
    table.insert(obj)

    obj['nested']['c'] = 'X'

    stored = table.get(Query().a == 'a')
    assert stored['nested']['c'] == 'c'


def test_insert_does_not_retain_reference_to_source_list():
    table = make_table()
    obj = {'a': 'a', 'tags': ['x', 'y']}
    table.insert(obj)

    obj['tags'].append('INJECTED')

    stored = table.get(Query().a == 'a')
    assert stored['tags'] == ['x', 'y']


def test_get_result_is_independent_of_stored_data():
    table = make_table()
    table.insert({'a': 1, 'nested': {'c': 'orig'}})

    doc = table.get(Query().a == 1)
    doc['nested']['c'] = 'MUTATED'

    doc_again = table.get(Query().a == 1)
    assert doc_again['nested']['c'] == 'orig'


def test_update_does_not_retain_reference_to_source_dict():
    table = make_table()
    table.insert({'a': 1, 'nested': {'c': 'orig'}})

    new_fields = {'nested': {'c': 'updated'}}
    table.update(new_fields, Query().a == 1)
    new_fields['nested']['c'] = 'MUTATED-AFTER-UPDATE'

    stored = table.get(Query().a == 1)
    assert stored['nested']['c'] == 'updated'
