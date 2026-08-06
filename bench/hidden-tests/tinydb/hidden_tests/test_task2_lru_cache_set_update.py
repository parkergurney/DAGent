"""
Hidden tests for task: LRUCache.set() must update the value of an existing key.

Seeded from the merged fix (tinydb PR fixing GitHub issue #560).
"""
from tinydb.utils import LRUCache


def test_lru_cache_set_update():
    cache = LRUCache(capacity=3)
    cache["a"] = 1
    cache["a"] = 2

    assert cache["a"] == 2
