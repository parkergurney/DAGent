"""
Hidden tests for task: LRUCache must treat falsy cached values (0, False, "")
as present when deciding whether a key already exists.

Seeded from the merged fix (tinydb PR fixing GitHub issue #596).

Note: this depends on LRUCache.set() also correctly updating the value of an
existing key (see test_task2_lru_cache_set_update.py) - the last assertion
below only holds once both fixes are in place.
"""
from tinydb.utils import LRUCache


def test_lru_cache_falsy_values_bug():
    cache = LRUCache(capacity=3)

    # Set up cache with a falsy value
    cache["a"] = 0
    cache["b"] = 1
    cache["c"] = 2

    assert cache.lru == ["a", "b", "c"]

    # Update existing key with a falsy value - should move to end
    cache.set("a", 3)
    assert cache.lru == ["b", "c", "a"]

    # Add new item - should evict oldest ("b"), not "a"
    cache.set("d", 4)
    assert cache.lru == ["c", "a", "d"]
    assert "b" not in cache
    assert cache["a"] == 3
