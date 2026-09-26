"""install.sh: the relay image and the media-mode switch.

The shell functions are cut out of install.sh and run with docker replaced by a stub that
records how it was called and answers as configured.
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


class InstallMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        (self.repo / "relay").mkdir(parents=True)
        (self.repo / "relay" / "Dockerfile").write_text("FROM scratch\n")
        (self.repo / "VERSION").write_text("1.13.0\n")
        self.data = root / "data"
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "docker.log"

    def tearDown(self):
        self.temp.cleanup()

    def docker(self, inspect=1, pull=1, build=0):
        stub = self.bin / "docker"
        stub.write_text(
            "#!/bin/sh\n"
            f'echo "docker $*" >> "{self.log}"\n'
            'case "$1 $2" in\n'
            f'  "image inspect") exit {inspect} ;;\n'
            f'  "pull "*) exit {pull} ;;\n'
            f'  "build "*) exit {build} ;;\n'
            "esac\nexit 0\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    def run_installer(self, *calls: str, env=None) -> subprocess.CompletedProcess:
        self.log.write_text("")
        script = "\n".join([
            'info() { echo "info: $*"; }',
            f'REPO_DIR="{self.repo}"',
            f'MDD_DATA_DIR="{self.data}"',
            shell_function("relay_image_ref"),
            shell_function("media_mode_recorded"),
            shell_function("ensure_relay_image"),
            *calls,
        ])
        return subprocess.run(
            ["sh", "-c", script], capture_output=True, text=True,
            env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", **(env or {})})

    def calls(self):
        return [line for line in self.log.read_text().splitlines() if line]

    def test_the_relay_image_is_named_after_this_version(self):
        self.docker()
        out = self.run_installer("relay_image_ref")
        self.assertEqual(out.stdout, "ghcr.io/mddidd/mdd-sim-gateway-relay:v1.13.0")

    def test_a_present_image_is_neither_fetched_nor_built(self):
        self.docker(inspect=0)
        self.assertEqual(self.run_installer("ensure_relay_image").returncode, 0)
        self.assertEqual(len(self.calls()), 1)

    def test_the_release_image_is_fetched_before_anything_is_built(self):
        self.docker(pull=0)
        self.assertEqual(self.run_installer("ensure_relay_image").returncode, 0)
        self.assertFalse(any(" build " in call for call in self.calls()))

    def test_a_source_checkout_builds_it_when_the_registry_cannot_be_reached(self):
        self.docker(pull=1)
        self.assertEqual(self.run_installer("ensure_relay_image").returncode, 0)
        build = [call for call in self.calls() if call.startswith("docker build")]
        self.assertEqual(len(build), 1)
        self.assertIn("-t ghcr.io/mddidd/mdd-sim-gateway-relay:v1.13.0", build[0])
        self.assertTrue(build[0].endswith("/relay"))

    def test_mdd_build_images_skips_the_registry(self):
        self.docker(pull=0)
        self.run_installer("ensure_relay_image", env={"MDD_BUILD_IMAGES": "1"})
        self.assertFalse(any(call.startswith("docker pull") for call in self.calls()))

    def test_the_recorded_mode_is_direct_until_relay_is_written(self):
        self.docker()
        self.assertEqual(self.run_installer("media_mode_recorded").stdout.strip(), "direct")
        (self.data / "media").mkdir(parents=True)
        (self.data / "media" / "state.json").write_text('{\n  "mode": "relay"\n}\n')
        self.assertEqual(self.run_installer("media_mode_recorded").stdout.strip(), "relay")

    def test_reload_looks_for_the_relay_image_only_in_relay_mode(self):
        reload = shell_function("cmd_reload")
        self.assertRegex(reload, r'if \[ "\$\(media_mode_recorded\)" = relay \]; then\n'
                                 r'\s+ensure_relay_image \|\| warn ')


if __name__ == "__main__":
    unittest.main()
