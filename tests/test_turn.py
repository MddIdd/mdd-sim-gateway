"""The browser softphone's media relay: addresses, credentials, lock-down and lifecycle."""
import base64
import hashlib
import hmac
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from control.app import config, turn


class AddressTests(unittest.TestCase):
    def test_each_line_keeps_fixed_addresses_on_both_networks(self):
        self.assertEqual(turn.engine_address({"index": 0}), "172.29.0.10")
        self.assertEqual(turn.media_address({"index": 3}), "172.29.1.13")
        self.assertEqual(turn.turn_media_address(), "172.29.1.2")

    def test_an_index_without_a_slot_is_refused(self):
        for index in (-1, turn.LINE_SLOTS, "x"):
            with self.subTest(index=index), self.assertRaises(turn.TurnError):
                turn.media_address({"index": index})

    def test_peers_are_the_engines_only_never_the_gateway_or_the_relay(self):
        low, high = turn.peer_range()
        self.assertEqual((low, high), ("172.29.1.10", "172.29.1.29"))
        # .1 is the host's address on the internal network; .2 is coturn itself.
        self.assertNotIn(turn.gateway(turn.MEDIA_SUBNET), (low, high))
        self.assertLess(turn.TURN_HOST, turn.FIRST_LINE_HOST)


class CredentialTests(unittest.TestCase):
    def test_credentials_follow_the_turn_rest_scheme_and_expire(self):
        with patch.object(turn, "secret", return_value="s3cret"):
            creds = turn.credentials("1", ttl=60, now=1000)
        self.assertEqual(creds["username"], "1060:1")
        expected = base64.b64encode(
            hmac.new(b"s3cret", b"1060:1", hashlib.sha1).digest()).decode()
        self.assertEqual(creds["credential"], expected)

    def test_secret_is_created_once_and_kept(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "CONFIG_PATH", os.path.join(temp, "config.yaml")):
            first = turn.secret()
            self.assertEqual(turn.secret(), first)
            self.assertGreaterEqual(len(first), 32)

    def test_ice_servers_use_the_browser_host_and_both_transports(self):
        with patch.object(turn, "secret", return_value="s"), \
                patch.object(turn, "PUBLIC_HOST", ""), patch.object(turn, "PUBLIC_PORT", 8478):
            servers = turn.ice_servers("2", "gw.example")
            v6 = turn.ice_servers("2", "fd00::5")
        self.assertEqual(servers[0]["urls"], ["turn:gw.example:8478?transport=udp",
                                              "turn:gw.example:8478?transport=tcp"])
        self.assertTrue(servers[0]["username"].endswith(":2"))
        self.assertEqual(v6[0]["urls"][0], "turn:[fd00::5]:8478?transport=udp")


class LockDownTests(unittest.TestCase):
    def test_relay_denies_everything_but_the_engines_media_range(self):
        conf = turn.render_config("s").splitlines()
        self.assertIn("denied-peer-ip=0.0.0.0-255.255.255.255", conf)
        self.assertIn("denied-peer-ip=::-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", conf)
        self.assertEqual([line for line in conf if line.startswith("allowed-peer-ip")],
                         ["allowed-peer-ip=172.29.1.10-172.29.1.29"])
        for option in ("no-tcp-relay", "no-multicast-peers", "no-cli", "no-rfc5780",
                       "use-auth-secret", "relay-ip=172.29.1.2", "proc-user=nobody"):
            self.assertIn(option, conf)

    def test_ruleset_only_lets_udp_reach_rtp_ports_of_the_engines(self):
        rules = turn.render_nftables()
        self.assertIn("policy drop;", rules)
        self.assertIn("ip daddr 172.29.1.10-172.29.1.29 udp dport 10000-19999 accept", rules)
        accepts = [line.strip() for line in rules.splitlines() if line.strip().endswith("accept")]
        self.assertEqual(len(accepts), 3)   # loopback, established flows, relayed RTP

    def test_every_line_rtp_pool_fits_the_allowed_port_range(self):
        # Five lines, including ones that kept the legacy 60-port pool.
        for index in range(5):
            start = config._alloc_ports(index)["rtp_start"]
            with self.subTest(index=index):
                self.assertGreaterEqual(start, turn.RTP_PORTS[0])
                self.assertLessEqual(start + config.LEGACY_RTP_SPAN - 1, turn.RTP_PORTS[1])


class _Network:
    def __init__(self, name, subnet, internal, labels=None):
        self.name = name
        self.attrs = {"Labels": {turn.MANAGED_LABEL: "true"} if labels is None else labels,
                      "IPAM": {"Config": [{"Subnet": subnet}]}, "Internal": internal}
        self.connected = []

    def connect(self, container, ipv4_address=None):
        self.connected.append((container.name, ipv4_address))


class _Container:
    def __init__(self, name, labels, status="running"):
        self.name = name
        self.status = status
        self.attrs = {"Config": {"Labels": labels}}
        self.removed = self.started = False

    def remove(self, force=False):
        self.removed = True

    def start(self):
        self.started = True


class _Docker:
    def __init__(self, networks=None, container=None):
        errors = turn.docker.errors
        self.created_networks = []
        self.created = []
        existing = networks or {}
        docker = self

        class Networks:
            def get(self, name):
                if name not in existing:
                    raise errors.NotFound(name)
                return existing[name]

            def create(self, name, **kwargs):
                net = _Network(name, kwargs["ipam"]["Config"][0]["Subnet"], kwargs["internal"])
                existing[name] = net
                docker.created_networks.append((name, kwargs))
                return net

        class Containers:
            def get(self, name):
                if container is None:
                    raise errors.NotFound(name)
                return container

            def create(self, image, **kwargs):
                made = _Container(kwargs["name"], kwargs["labels"], status="created")
                docker.created.append((image, kwargs, made))
                return made

        class Images:
            def get(self, name):
                return SimpleNamespace(id="sha256:image")

        self.networks, self.containers, self.images = Networks(), Containers(), Images()


class LifecycleTests(unittest.TestCase):
    def ensure(self, client):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(turn, "secret", return_value="s"), \
                patch.object(turn, "_config_dir", return_value=(temp, "/host/turn")):
            result = turn.ensure(client)
            files = {name: oct(os.stat(os.path.join(temp, name)).st_mode & 0o777)
                     for name in os.listdir(temp)}
        return result, files

    def test_first_start_creates_networks_and_a_locked_down_relay(self):
        client = _Docker()
        result, files = self.ensure(client)
        self.assertTrue(result["changed"])
        created = {name: kwargs for name, kwargs in client.created_networks}
        self.assertFalse(created[turn.ENGINE_NETWORK]["internal"])
        self.assertTrue(created[turn.MEDIA_NETWORK]["internal"])
        self.assertEqual(files, {"turnserver.conf": "0o600", "nftables.conf": "0o600"})
        image, kwargs, container = client.created[0]
        self.assertEqual(image, turn.IMAGE)
        self.assertEqual(kwargs["network"], "bridge")   # never on mdd-engine
        self.assertEqual(set(kwargs["ports"]), {"3478/udp", "3478/tcp"})
        self.assertEqual(kwargs["cap_add"], ["NET_ADMIN"])
        self.assertEqual(kwargs["volumes"], {"/host/turn": {"bind": "/etc/mdd-turn", "mode": "ro"}})
        media = client.networks.get(turn.MEDIA_NETWORK)
        self.assertEqual(media.connected, [(turn.CONTAINER, "172.29.1.2")])
        self.assertTrue(container.started)

    def test_an_unchanged_relay_is_left_running(self):
        first = _Docker()
        self.ensure(first)
        fingerprint = first.created[0][1]["labels"][turn.CONFIG_LABEL]
        running = _Container(turn.CONTAINER, {turn.MANAGED_LABEL: "true",
                                              turn.CONFIG_LABEL: fingerprint})
        networks = {name: first.networks.get(name)
                    for name in (turn.ENGINE_NETWORK, turn.MEDIA_NETWORK)}
        client = _Docker(networks=networks, container=running)
        result, _ = self.ensure(client)
        self.assertFalse(result["changed"])
        self.assertFalse(running.removed)
        self.assertEqual(client.created, [])

    def test_a_changed_relay_is_replaced(self):
        stale = _Container(turn.CONTAINER, {turn.MANAGED_LABEL: "true", turn.CONFIG_LABEL: "old"})
        client = _Docker(container=stale)
        result, _ = self.ensure(client)
        self.assertTrue(result["changed"])
        self.assertTrue(stale.removed)

    def test_foreign_network_or_wrong_subnet_is_refused(self):
        cases = [
            _Network(turn.ENGINE_NETWORK, "172.29.0.0/24", False, labels={}),
            _Network(turn.ENGINE_NETWORK, "10.9.0.0/24", False),
        ]
        for network in cases:
            with self.subTest(attrs=network.attrs), self.assertRaises(turn.TurnError):
                turn.ensure_networks(_Docker(networks={turn.ENGINE_NETWORK: network}))

    def test_a_foreign_container_with_the_relay_name_is_never_replaced(self):
        client = _Docker(container=_Container(turn.CONTAINER, {}))
        with self.assertRaises(turn.TurnError):
            self.ensure(client)


if __name__ == "__main__":
    unittest.main()
