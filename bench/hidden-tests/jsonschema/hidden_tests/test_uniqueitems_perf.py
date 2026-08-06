"""
uniqueItems validation must stay usable on large arrays whose elements
can't be sorted (e.g. objects). A quadratic fallback makes this task
class of schema effectively unusable at scale.
"""
from unittest import TestCase
import time

from jsonschema import Draft202012Validator


class TestUniqueItemsPerformance(TestCase):
    def test_correctness_is_preserved_for_unsortable_items(self):
        schema = {"type": "array", "uniqueItems": True}
        validator = Draft202012Validator(schema)

        self.assertTrue(validator.is_valid([{"a": 1}, {"a": 2}, {"a": 3}]))
        self.assertFalse(validator.is_valid([{"a": 1}, {"a": 1}]))

    def test_large_array_of_objects_validates_quickly(self):
        schema = {"type": "array", "uniqueItems": True}
        validator = Draft202012Validator(schema)

        instance = [{"id": i, "payload": "x" * 20} for i in range(3000)]

        start = time.perf_counter()
        result = validator.is_valid(instance)
        elapsed = time.perf_counter() - start

        self.assertTrue(result)
        self.assertLess(
            elapsed, 3.0,
            "uniqueItems validation of a large array of distinct objects "
            "should not degrade to quadratic brute-force comparison",
        )
