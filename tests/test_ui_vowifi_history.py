import shutil
import subprocess
import unittest
from pathlib import Path


class VowifiHistoryRaceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for UI race tests")
    def test_request_generations(self):
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            ["node", "--test", "webui/tests/vowifi-history.test.mjs"],
            cwd=root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
