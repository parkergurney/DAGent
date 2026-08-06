"""
When a property fails its own "properties" subschema, it shouldn't also
be reported as an unevaluated property. It was already "seen" by the
schema (and already produced an error); reporting it a second time under
unevaluatedProperties is confusing double-reporting of the same problem.
"""
from unittest import TestCase

from jsonschema import Draft202012Validator


class TestUnevaluatedPropertiesDoubleJeopardy(TestCase):
    def test_property_failing_its_own_schema_is_not_also_unevaluated(self):
        schema = {
            "type": "object",
            "properties": {"foo": {"type": "string"}},
            "unevaluatedProperties": False,
        }
        instance = {"foo": 123}

        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(instance))

        self.assertEqual(len(errors), 1)
        self.assertIn("123", errors[0].message)
        self.assertNotIn("unevaluated", errors[0].message.lower())

    def test_property_passing_its_own_schema_is_evaluated(self):
        schema = {
            "type": "object",
            "properties": {"foo": {"type": "string"}},
            "unevaluatedProperties": False,
        }
        instance = {"foo": "bar"}

        validator = Draft202012Validator(schema)
        self.assertTrue(validator.is_valid(instance))

    def test_unlisted_property_is_still_unevaluated(self):
        schema = {
            "type": "object",
            "properties": {"foo": {"type": "string"}},
            "unevaluatedProperties": False,
        }
        instance = {"foo": "bar", "extra": 1}

        validator = Draft202012Validator(schema)
        errors = list(validator.iter_errors(instance))

        self.assertEqual(len(errors), 1)
        self.assertIn("unevaluated", errors[0].message.lower())
