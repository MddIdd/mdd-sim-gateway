"""install.sh: SWU_TUN_MTU survives the docker-mode control container being recreated.

The engines take their ipsec0 MTU from the control plane's environment (#79). A native install
keeps it in a systemd drop-in; docker mode recreates the control container on every reload,
so the installer has to carry the value over itself.
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


class InstallTunMtuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.bin = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def tun_mtu(self, container_env: str | None, **env) -> subprocess.CompletedProcess:
        docker = self.bin / "docker"
        if container_env is None:
            docker.write_text("#!/bin/sh\nexit 1\n")
        else:
            docker.write_text(f"#!/bin/sh\nprintf '%s\\n' {container_env!r} | tr ';' '\\n'\n")
        docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
        script = "\n".join([
            'warn() { echo "warn: $*"; }',
            'CONTROL_NAME=mdd-sim-gateway-control',
            shell_function("control_tun_mtu"),
            "control_tun_mtu",
        ])
        environ = {k: v for k, v in os.environ.items() if k != "SWU_TUN_MTU"}
        return subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                              env={**environ, "PATH": f"{self.bin}:{environ['PATH']}", **env})

    def test_the_installers_environment_wins(self):
        out = self.tun_mtu("MDD_DATA=/data;SWU_TUN_MTU=1280", SWU_TUN_MTU="1300")
        self.assertEqual(out.stdout, "1300")

    def test_the_replaced_container_keeps_its_value(self):
        out = self.tun_mtu("MDD_DATA=/data;SWU_TUN_MTU=1280;PATH=/usr/bin")
        self.assertEqual(out.stdout, "1280")

    def test_nothing_set_leaves_the_engine_default(self):
        self.assertEqual(self.tun_mtu("MDD_DATA=/data").stdout, "")
        self.assertEqual(self.tun_mtu(None).stdout, "")

    def test_a_non_numeric_value_is_ignored_and_reported_aside(self):
        out = self.tun_mtu(None, SWU_TUN_MTU="1280; rm -rf /")
        self.assertEqual(out.stdout, "")
        self.assertIn("not a number", out.stderr)

    def test_default_drops_a_carried_over_value(self):
        out = self.tun_mtu("SWU_TUN_MTU=1280", SWU_TUN_MTU="default")
        self.assertEqual(out.stdout, "")

    def test_a_value_outside_the_usable_range_is_ignored(self):
        for value in ("0", "1279", "1501", "09999", "123456789012345678901234567890"):
            out = self.tun_mtu(None, SWU_TUN_MTU=value)
            self.assertEqual(out.stdout, "", value)
            self.assertIn("outside 1280-1500", out.stderr, value)
        self.assertEqual(self.tun_mtu(None, SWU_TUN_MTU="1500").stdout, "1500")

    def test_docker_mode_passes_it_only_when_there_is_one(self):
        run_control = shell_function("run_control")
        self.assertIn("${TUN_MTU:+-e SWU_TUN_MTU=$TUN_MTU}", run_control)
        # Read before the old container is removed, or there is nothing left to read.
        self.assertLess(run_control.index("TUN_MTU=$(control_tun_mtu)"),
                        run_control.index('docker rm -f "$CONTROL_NAME"'))


if __name__ == "__main__":
    unittest.main()
