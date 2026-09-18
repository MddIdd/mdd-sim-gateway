"""install.sh refuses engine subnets that would collide with anything already routed."""
import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("mdd_networks", ROOT / "host" / "mdd_networks.py")
networks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(networks)
INSTALL = (ROOT / "install.sh").read_text(encoding="utf-8")
OWN = {"mdd-engine", "mdd-media"}


def check(subnet, docker=None, routes=(), own_interfaces=(), other=None):
    return networks.conflicts("mdd-engine", subnet, docker or {}, list(routes), OWN,
                              set(own_interfaces), other)


class SubnetConflictTests(unittest.TestCase):
    def test_defaults_are_free_on_a_typical_host(self):
        docker = {"bridge": ["172.17.0.0/16"], "host": [], "none": []}
        routes = [("default", "ens18"), ("192.168.3.0/24", "ens18"), ("172.17.0.0/16", "docker0"),
                  ("10.175.213.180/30", "wws27u1i4"), ("172.29.20.0/30", "mdd-au")]
        self.assertEqual(check("172.29.0.0/24", docker, routes, other="172.29.1.0/24"), [])
        self.assertEqual(check("172.29.1.0/24", docker, routes), [])

    def test_lan_docker_and_tunnel_overlaps_are_named(self):
        problems = check("192.168.3.0/24", {"proj_default": ["192.168.0.0/20"]},
                         [("192.168.3.0/24", "ens18")])
        self.assertTrue(any("Docker network 'proj_default'" in p for p in problems))
        self.assertTrue(any("host route 192.168.3.0/24 on ens18" in p for p in problems))
        self.assertTrue(any("country tunnel" in p for p in check("172.29.40.0/24")))
        self.assertTrue(any("other MDD network" in p
                            for p in check("172.29.0.0/23", other="172.29.1.0/24")))

    def test_mdd_networks_own_routes_and_entries_are_expected(self):
        docker = {"mdd-engine": ["172.29.0.0/24"]}
        routes = [("172.29.0.0/24", "br-0123456789ab")]
        self.assertEqual(check("172.29.0.0/24", docker, routes, own_interfaces={"br-0123456789ab"}),
                         [])

    def test_malformed_ipv6_and_too_small_subnets_are_refused(self):
        self.assertTrue(check("172.29.0.1/24"))          # host bits set
        self.assertTrue(check("fd00::/64"))
        self.assertTrue(any("too small" in p for p in check("172.29.0.0/28")))


class InstallerTests(unittest.TestCase):
    def test_networks_and_relay_image_are_ready_before_the_control_plane_starts(self):
        for command in ("cmd_install", "cmd_reload"):
            body = INSTALL[INSTALL.index(f"{command}() {{"):]
            body = body[:body.index("\n}\n")]
            with self.subTest(command=command):
                self.assertLess(body.index("ensure_turn_image"), body.index("run_control"))
                self.assertLess(body.index("ensure_networks"), body.index("run_control"))

    def test_media_network_is_internal_and_docker_control_joins_the_engine_network(self):
        self.assertIn('[ "$name" = "$MEDIA_NETWORK" ] && set -- "$@" --internal', INSTALL)
        self.assertIn('docker network connect "$ENGINE_NETWORK" "$CONTROL_NAME"', INSTALL)

    def test_both_control_planes_receive_the_relay_settings(self):
        unit = INSTALL[INSTALL.index('cat > "$SYSTEMD_UNIT" <<EOF'):]
        unit = unit[:unit.index("\nEOF\n")]
        docker_run = INSTALL[INSTALL.index('docker run -d --name "$CONTROL_NAME"'):]
        docker_run = docker_run[:docker_run.index('"$CONTROL_IMAGE"')]
        for name in ("MDD_TURN_IMAGE", "MDD_TURN_PORT", "MDD_TURN_HOST", "MDD_TURN_PUBLIC_PORT",
                     "MDD_ENGINE_SUBNET", "MDD_MEDIA_SUBNET"):
            with self.subTest(name=name):
                self.assertIn(f"Environment={name}=", unit)
                self.assertRegex(docker_run, rf"-e {name}=")

    def test_relay_port_is_checked_and_uninstall_removes_the_relay(self):
        preflight = INSTALL[INSTALL.index("docker_preflight() {"):]
        preflight = preflight[:preflight.index("\n}\n")]
        self.assertIn('--filter "publish=$MDD_TURN_PORT"', preflight)
        self.assertIn("ss -lntuH", preflight)
        uninstall = INSTALL[INSTALL.index("cmd_uninstall() {"):]
        uninstall = uninstall[:uninstall.index("\n}\n")]
        self.assertIn("remove_turn", uninstall)
        self.assertTrue(re.search(r'docker rmi -f [^\n]*"\$TURN_IMAGE"', uninstall))

    def test_relay_image_pins_coturn_by_digest(self):
        dockerfile = (ROOT / "turn" / "Dockerfile").read_text()
        self.assertRegex(dockerfile, r"coturn/coturn:4\.17\.2-debian@sha256:[0-9a-f]{64}")
        self.assertIn('TURN_VERSION="4.17.2"', INSTALL)
        entrypoint = (ROOT / "turn" / "entrypoint.sh").read_text()
        self.assertLess(entrypoint.index("nft -f"), entrypoint.index("exec turnserver"))
        self.assertIn("set -eu", entrypoint)


if __name__ == "__main__":
    unittest.main()
