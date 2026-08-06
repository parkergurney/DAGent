"""
When additionalProperties (or unevaluatedProperties) is a subschema
rather than a plain boolean, an instance property should count as
"evaluated" only if it actually validates against that subschema, the
same way "properties" evaluates each named property against its own
subschema. Instance properties must not be checked against the
subschema's own keys.
"""
from unittest import TestCase

from jsonschema import Draft202012Validator


class TestAdditionalPropertiesAreEvaluated(TestCase):
    def test_property_valid_under_additional_properties_is_evaluated(self):
        schema = {
            "type": "object",
            "properties": {"foo": {"type": "string"}},
            "additionalProperties": {"type": "number"},
            "unevaluatedProperties": False,
        }
        instance = {"foo": "a", "bar": 5}

        validator = Draft202012Validator(schema)
        self.assertTrue(validator.is_valid(instance))

    def test_property_invalid_under_additional_properties_is_reported(self):
        schema = {
            "type": "object",
            "properties": {"foo": {"type": "string"}},
            "additionalProperties": {"type": "number"},
            "unevaluatedProperties": False,
        }
        instance = {"foo": "a", "bar": "not a number"}

        validator = Draft202012Validator(schema)
        self.assertFalse(validator.is_valid(instance))

    def test_property_valid_under_unevaluated_properties_subschema(self):
        schema = {
            "type": "object",
            "properties": {"foo": {"type": "string"}},
            "unevaluatedProperties": {"type": "number"},
        }
        instance = {"foo": "a", "bar": 5}

        validator = Draft202012Validator(schema)
        self.assertTrue(validator.is_valid(instance))
