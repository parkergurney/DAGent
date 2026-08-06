"""
_Error.json_path renders a JSONPath-like locator for where a validation
error occurred. Property names that are empty or contain characters like
'.', '[', quotes, backslashes, or spaces must be quoted and escaped so
the result is an unambiguous, valid path, e.g. a property literally
named "." should render as $['.'], not $..
"""
from unittest import TestCase

from jsonschema import Draft202012Validator


def json_path_for_property(name):
    schema = {"properties": {name: {"type": "string"}}}
    instance = {name: None}
    validator = Draft202012Validator(schema)
    (error,) = validator.iter_errors(instance)
    return error.json_path


class TestJSONPathEscaping(TestCase):
    def test_plain_property_name_uses_dot_notation(self):
        self.assertEqual(json_path_for_property("foo"), "$.foo")

    def test_empty_property_name_is_quoted(self):
        self.assertEqual(json_path_for_property(""), "$['']")

    def test_property_name_with_dot_is_quoted(self):
        self.assertEqual(json_path_for_property("."), "$['.']")

    def test_property_name_with_bracket_is_quoted(self):
        self.assertEqual(json_path_for_property("["), "$['[']")

    def test_property_name_with_space_is_quoted(self):
        self.assertEqual(json_path_for_property(" "), "$[' ']")

    def test_property_name_with_single_quote_is_escaped(self):
        self.assertEqual(json_path_for_property("'"), "$['\\'']")

    def test_property_name_with_backslash_is_escaped(self):
        self.assertEqual(json_path_for_property("\\"), "$['\\\\']")

    def test_array_index_is_unaffected(self):
        schema = {"items": {"type": "integer"}}
        instance = [1, "not an int"]
        validator = Draft202012Validator(schema)
        (error,) = validator.iter_errors(instance)
        self.assertEqual(error.json_path, "$[1]")
