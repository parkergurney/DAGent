"""
Hidden tests for task: custom document classes.

Table.insert()/insert_multiple()/upsert() must recognize a caller-supplied
document as "a document with an explicit ID" by checking against
``self.document_class`` (which a Table subclass may override), not against
the hardcoded ``tinydb.table.Document`` class.

Fresh tests, strictly behavior-based (seeded from GitHub issue #545 - no
tests were included in the linked PR).
"""
from tinydb import TinyDB
from tinydb.storages import MemoryStorage
from tinydb.table import Table


class CustomDocument(dict):
    """A document class that does NOT inherit from tinydb.table.Document."""

    def __init__(self, value, doc_id):
        super().__init__(value)
        self.doc_id = doc_id


class CustomTable(Table):
    document_class = CustomDocument


class CustomTinyDB(TinyDB):
    table_class = CustomTable


def make_db():
    db = CustomTinyDB(storage=MemoryStorage)
    return db.table('t')


def test_insert_honors_explicit_id_from_custom_document_class():
    table = make_db()
    doc_id = table.insert(CustomDocument({'a': 1}, 42))

    assert doc_id == 42
    assert table.get(doc_id=42) == {'a': 1}


def test_insert_multiple_honors_explicit_id_from_custom_document_class():
    table = make_db()
    doc_ids = table.insert_multiple([
        CustomDocument({'a': 1}, 7),
        {'a': 2},
    ])

    assert doc_ids[0] == 7
    assert table.get(doc_id=7) == {'a': 1}


def test_upsert_extracts_doc_id_from_custom_document_class():
    table = make_db()
    table.insert(CustomDocument({'a': 1}, 5))

    updated = table.upsert(CustomDocument({'a': 2}, 5))

    assert updated == [5]
    assert table.get(doc_id=5) == {'a': 2}
