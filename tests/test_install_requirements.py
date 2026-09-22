"""install.sh: a reload stops when the control plane's Python dependencies are not usable.

The picture conversion added for MMS needs Pillow and pillow-heif, which carry native
libraries. The control plane starts without them on purpose -- conversion then simply does
not happen -- so the installer is the only place that can tell an operator their reload did
not give them what the release says it does.

setup_venv is cut out of install.sh and run against a stub `python` whose three steps (the
offline proof, the install, the import probe) succeed or fail as each test asks.
"""
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

INSTALL = (Path(__file__).resolve().parent.parent / "install.sh").read_text(encoding="utf-8")


def shell_function(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", INSTALL, re.M | re.S)
    assert match, name
    return match.group(0)


PYTHON_STUB = """#!/bin/sh
case "$*" in
  *--no-index*)  echo "offline" >> "$LOG"; exit "$OFFLINE_RC" ;;
  *"pip install"*) echo "install" >> "$LOG"; exit "$INSTALL_RC" ;;
  *-c*)          echo "import" >> "$LOG"; exit "$IMPORT_RC" ;;
esac
exit 0
"""


class SetupVenvTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.log = root / "calls.log"
        self.venv = root / "venv"
        (self.venv / "bin").mkdir(parents=True)
        stub = self.venv / "bin" / "python"
        stub.write_text(PYTHON_STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        self.repo = root / "repo"
        (self.repo / "control").mkdir(parents=True)
        (self.repo / "control" / "requirements.txt").write_text("Pillow==12.3.0\n")

    def tearDown(self):
        self.temp.cleanup()

    def run_setup(self, offline=0, install=0, probe=0):
        self.log.write_text("")
        script = "\n".join([
            "set -eu",
            "info() { :; }",
            'die() { echo "died: $*" >&2; exit 1; }',
            shell_function("setup_venv"),
            "setup_venv",
        ])
        done = subprocess.run(
            ["sh", "-c", script], capture_output=True, text=True,
            env={**os.environ, "VENV_DIR": str(self.venv), "REPO_DIR": str(self.repo),
                 "LOG": str(self.log), "OFFLINE_RC": str(offline),
                 "INSTALL_RC": str(install), "IMPORT_RC": str(probe)})
        return done, [line for line in self.log.read_text().splitlines() if line]

    def test_dependencies_already_present_are_reused_and_still_proved_to_import(self):
        done, calls = self.run_setup()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(calls, ["offline", "import"], "no index is consulted needlessly")

    def test_an_install_that_fails_stops_the_reload(self):
        done, calls = self.run_setup(offline=1, install=1)
        self.assertEqual(done.returncode, 1)
        self.assertIn("installing the control requirements failed", done.stderr)
        self.assertEqual(calls, ["offline", "install"], "nothing is probed or restarted")

    def test_a_dependency_that_installs_but_does_not_import_stops_the_reload(self):
        done, calls = self.run_setup(offline=1, probe=1)
        self.assertEqual(done.returncode, 1)
        self.assertIn("do not import", done.stderr)
        self.assertEqual(calls, ["offline", "install", "import"])

    def test_a_venv_that_satisfies_pip_but_cannot_import_stops_the_reload(self):
        # The case the offline proof cannot see: the wheel is installed, its native library
        # is gone. Without the probe the reload would restart into silent degradation.
        done, _calls = self.run_setup(probe=1)
        self.assertEqual(done.returncode, 1)
        self.assertIn("do not import", done.stderr)


if __name__ == "__main__":
    unittest.main()
