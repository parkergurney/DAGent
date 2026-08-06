"""
The "hostname" format should accept single-label hostnames like
"localhost", not just fully-qualified multi-label domain names.
"""
from unittest import TestCase

from jsonschema import FormatChecker


class TestSingleLabelHostname(TestCase):
    def setUp(self):
        self.format_checker = FormatChecker()

    def test_single_label_hostname_is_valid(self):
        self.assertTrue(self.format_checker.conforms("localhost", "hostname"))

    def test_multi_label_hostname_is_still_valid(self):
        self.assertTrue(self.format_checker.conforms("example.com", "hostname"))

    def test_empty_hostname_is_still_invalid(self):
        self.assertFalse(self.format_checker.conforms("", "hostname"))
