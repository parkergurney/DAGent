"""
Importing jsonschema should not eagerly import urllib.request, since most
users never resolve a remote http(s) $ref and shouldn't pay for that
module's import cost (or its side effects) up front.
"""
from unittest import TestCase
import subprocess
import sys


class TestLazyUrllibImport(TestCase):
    def test_importing_jsonschema_does_not_import_urllib_request(self):
        result = subprocess.run(
            [
                sys.executable, "-c",
                "import sys\n"
                "import jsonschema\n"
                "assert 'urllib.request' not in sys.modules, (\n"
                "    'urllib.request should not be imported as a side '\n"
                "    'effect of importing jsonschema'\n"
                ")\n",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
