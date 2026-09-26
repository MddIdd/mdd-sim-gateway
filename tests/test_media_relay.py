import base64
import hashlib
import hmac
import importlib
import ipaddress
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def _docker():
    not_found = type("NotFound", (Exception,), {})
    return SimpleNamespace(
        from_env=lambda **_: None,
        errors=SimpleNamespace(NotFound=not_found,
                               ImageNotFound=type("ImageNotFound", (not_found,), {})),
    )


def media_module():
    with patch.dict(sys.modules, {"docker": _docker()}):
        for name in ("control.app.media", "control.app.engine"):
            sys.modules.pop(name, None)
        return importlib.import_module("control.app.media")


class _Network:
    def __init__(self, subnet="172.30.0.0/16", gateway="172.30.0.1", internal=True,
                 managed=True, containers=None):
        self.attrs = {
            "Internal": internal,
            "Labels": {"io.mdd-sim-gateway.managed": "true"} if managed else {},
            "IPAM": {"Config": [{"Subnet": subnet, "Gateway": gateway}]},
            "Containers": containers or {},
        }
        self.connected = []
        self.removed = False

    def reload(self):
        pass

    def connect(self, container):
        self.connected.append(container)

    def remove(self):
        self.removed = True


class _Container:
    def __init__(self, fingerprint="", status="running", exit_code=0):
        self.attrs = {"Config": {"Labels": {"io.mdd-sim-gateway.managed": "true",
                                            "io.mdd-sim-gateway.relay-config": fingerprint}}}
        self.status = status
        self.exit_code = exit_code
        self.removed = False
        self.started = False

    def remove(self, force=False):
        self.removed = True

    def start(self):
        self.started = True
        self.status = "running"

    def exec_run(self, cmd, demux=False):
        return SimpleNamespace(exit_code=self.exit_code, output=b"")


class _Client:
    def __init__(self, media, network=None, container=None, image_id="sha256:relay"):
        self.media = media
        self.network = network
        self.container = container
        self.created = []
        client = self

        class Networks:
            def get(self, name):
                if client.network is None:
                    raise media.docker.errors.NotFound(name)
                return client.network

            def create(self, name, **kwargs):
                client.network = _Network()
                client.network.create_kwargs = kwargs
                return client.network

        class Containers:
            def get(self, name):
                if client.container is None or client.container.removed:
                    raise media.docker.errors.NotFound(name)
                return client.container

            def create(self, image, **kwargs):
                fingerprint = kwargs["labels"][media.CONFIG_LABEL]
                client.container = _Container(fingerprint, status="created")
                client.created.append(kwargs)
                return client.container

        class Images:
            def get(self, reference):
                return SimpleNamespace(id=image_id)

            def pull(self, reference):
                raise AssertionError("no pull expected")

        self.networks = Networks()
        self.containers = Containers()
        self.images = Images()


class MediaRelayTests(unittest.TestCase):
    def setUp(self):
        self.media = media_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(self.media.cfg, "DATA_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_recorded_mode_is_direct_and_hands_out_no_ice_servers(self):
        self.assertEqual(self.media.mode(), "direct")
        self.assertEqual(self.media.provisioning("1", "gw.example"), {
            "media_mode": "direct", "ice_servers": [], "ice_transport_policy": "all",
            "relay_ready": None})

    def test_the_relay_may_reach_the_media_network_but_not_the_host(self):
        ranges = self.media.peer_ranges(ipaddress.ip_network("172.30.0.0/16"),
                                        ipaddress.ip_address("172.30.0.1"))
        self.assertEqual(ranges, [("172.30.0.2", "172.30.255.254")])
        ranges = self.media.peer_ranges(ipaddress.ip_network("10.9.8.0/24"),
                                        ipaddress.ip_address("10.9.8.100"))
        self.assertEqual(ranges, [("10.9.8.1", "10.9.8.99"), ("10.9.8.101", "10.9.8.254")])

    def test_relay_config_denies_everything_but_udp_to_the_media_network(self):
        config = self.media.render_config("s3cret", ipaddress.ip_network("172.30.0.0/16"),
                                          ipaddress.ip_address("172.30.0.1"))
        lines = config.splitlines()
        self.assertIn("no-tcp-relay", lines)
        self.assertIn("denied-peer-ip=0.0.0.0-255.255.255.255", lines)
        self.assertIn("denied-peer-ip=::-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff", lines)
        self.assertEqual([line for line in lines if line.startswith("allowed-peer-ip")],
                         ["allowed-peer-ip=172.30.0.2-172.30.255.254"])
        self.assertIn("static-auth-secret=s3cret", lines)
        self.assertFalse(any(line.startswith(("relay-ip", "listening-ip")) for line in lines))

    def test_credentials_follow_the_turn_rest_scheme(self):
        creds = self.media.credentials("3", "s3cret", ttl=60, now=1000)
        self.assertEqual(creds["username"], "1060:3")
        expected = base64.b64encode(
            hmac.new(b"s3cret", b"1060:3", hashlib.sha1).digest()).decode()
        self.assertEqual(creds["credential"], expected)

    def test_relay_provisioning_names_the_host_clients_reached(self):
        self.media.save_state({"mode": "relay", "port": 8478, "public_port": 3479,
                               "secret": "s3cret"})
        prov = self.media.provisioning("2", "fd00::5")
        self.assertEqual(prov["media_mode"], "relay")
        self.assertEqual(prov["ice_transport_policy"], "relay")
        self.assertEqual(prov["ice_servers"][0]["urls"],
                         ["turn:[fd00::5]:3479?transport=udp", "turn:[fd00::5]:3479?transport=tcp"])
        self.media.save_state({"mode": "relay", "port": 8478, "public_host": "gw.example",
                               "secret": "s3cret"})
        prov = self.media.provisioning("2", "192.168.1.5")
        self.assertEqual(prov["ice_servers"][0]["urls"][0], "turn:gw.example:8478?transport=udp")

    def test_state_file_is_private(self):
        self.media.save_state({"mode": "relay", "secret": "x"})
        mode = os.stat(os.path.join(self.tmp.name, "media", "state.json")).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_relay_container_holds_no_capability_and_is_not_replaced_needlessly(self):
        network = _Network()
        client = _Client(self.media, network=network)
        state = {"mode": "relay", "port": 8478, "secret": "s3cret", "image": "relay:test"}
        first = self.media.ensure_relay(client, state)
        kwargs = client.created[0]
        self.assertEqual(kwargs["cap_drop"], ["ALL"])
        self.assertNotIn("cap_add", kwargs)
        self.assertTrue(kwargs["read_only"])
        self.assertEqual(kwargs["network"], "bridge")
        self.assertEqual(kwargs["ports"], {"3478/udp": 8478, "3478/tcp": 8478})
        self.assertEqual(kwargs["environment"]["MDD_MEDIA_SUBNET"], "172.30.0.0/16")
        self.assertEqual(network.connected, [first])
        self.assertTrue(first.started)

        again = self.media.ensure_relay(client, state)
        self.assertIs(again, first)
        self.assertEqual(len(client.created), 1)

        self.media.ensure_relay(client, {**state, "port": 9478})
        self.assertTrue(first.removed)
        self.assertEqual(len(client.created), 2)

    def test_a_foreign_network_of_the_same_name_is_refused(self):
        client = _Client(self.media, network=_Network(managed=False))
        with self.assertRaisesRegex(self.media.MediaError, "not created by MDD"):
            self.media.ensure_network(client)
        client = _Client(self.media, network=_Network(internal=False))
        with self.assertRaisesRegex(self.media.MediaError, "not internal"):
            self.media.ensure_network(client)

    def test_the_media_network_is_created_internal_without_a_fixed_subnet(self):
        client = _Client(self.media)
        self.media.ensure_network(client)
        kwargs = client.network.create_kwargs
        self.assertTrue(kwargs["internal"])
        self.assertNotIn("ipam", kwargs)

    def test_a_relay_that_never_answers_leaves_direct_mode_and_nothing_behind(self):
        client = _Client(self.media)
        with patch.object(self.media, "wait_ready", return_value=(False, "relay_not_answering")):
            with self.assertRaisesRegex(self.media.MediaError, "did not become ready"):
                self.media.enable(client, port=8478, image="relay:test")
        self.assertEqual(self.media.mode(), "direct")
        self.assertEqual(self.media.load_state(), {})
        self.assertTrue(client.container.removed)
        self.assertTrue(client.network.removed)

    def test_enabling_records_relay_mode_only_after_the_relay_answers(self):
        client = _Client(self.media)
        with patch.object(self.media, "wait_ready", return_value=(True, "")):
            state = self.media.enable(client, port=8478, image="relay:test")
        self.assertEqual(self.media.mode(), "relay")
        self.assertEqual(state["public_port"], 8478)
        self.assertTrue(state["secret"])
        # The secret survives a later change of port, so issued credentials stay valid.
        with patch.object(self.media, "wait_ready", return_value=(True, "")):
            again = self.media.enable(client, port=9000, image="relay:test")
        self.assertEqual(again["secret"], state["secret"])

    def test_the_media_network_outlives_engines_still_attached(self):
        network = _Network(containers={"abc": {"Name": "mdd-sim-gateway-engine-1"}})
        client = _Client(self.media, network=network)
        self.assertFalse(self.media.remove_network(client))
        self.assertFalse(network.removed)
        network.attrs["Containers"] = {}
        self.assertTrue(self.media.remove_network(client))
        self.assertTrue(network.removed)

    def test_engines_join_nothing_in_direct_mode(self):
        self.assertIsNone(self.media.engine_attachment(_Client(self.media)))

    def test_engines_learn_the_media_subnet_and_shared_rtp_range_in_relay_mode(self):
        self.media.save_state({"mode": "relay", "secret": "x"})
        attachment = self.media.engine_attachment(_Client(self.media, network=_Network()))
        self.assertEqual(attachment["instance"], {
            "mode": "relay", "subnet": "172.30.0.0/16", "rtp_start": 10000, "rtp_end": 10199})

    def test_supervision_keeps_the_previous_image_when_the_new_one_cannot_be_fetched(self):
        self.media.save_state({"mode": "relay", "secret": "x", "image": "relay:old"})
        client = _Client(self.media, network=_Network())
        self.media._image_checked = False
        with patch.object(self.media, "ensure_image",
                          side_effect=[self.media.MediaError("offline"),
                                       SimpleNamespace(id="sha256:old")]), \
                patch.object(self.media, "default_image", return_value="relay:new"), \
                patch.object(self.media, "check_relay", return_value=(True, "")):
            status = self.media.supervise(client)
        self.assertEqual(status["state"], "ready")
        self.assertEqual(self.media.load_state()["image"], "relay:old")


class RelayEntrypointTests(unittest.TestCase):
    def run_entrypoint(self, addresses):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp, "bin")
            bin_dir.mkdir()
            Path(bin_dir, "hostname").write_text(f"#!/bin/sh\necho '{addresses}'\n")
            Path(bin_dir, "turnserver").write_text("#!/bin/sh\ncat \"$2\"\n")
            for name in ("hostname", "turnserver"):
                os.chmod(Path(bin_dir, name), 0o755)
            script = (ROOT / "relay" / "entrypoint.sh").read_text().replace(
                "/tmp/turnserver.conf", f"{tmp}/turnserver.conf")
            return subprocess.run(
                ["sh", "-c", script],
                env={"PATH": f"{bin_dir}:/usr/bin:/bin", "MDD_RELAY_CONFIG": "realm=x",
                     "MDD_MEDIA_SUBNET": "172.30.0.0/16"},
                capture_output=True, text=True)

    def test_allocations_live_on_the_media_network_and_listeners_everywhere_else(self):
        result = self.run_entrypoint("172.17.0.5 172.30.0.3 ")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "realm=x")
        self.assertIn("relay-ip=172.30.0.3", lines)
        self.assertIn("listening-ip=172.17.0.5", lines)
        self.assertIn("listening-ip=127.0.0.1", lines)
        self.assertNotIn("listening-ip=172.30.0.3", lines)

    def test_without_a_media_address_the_relay_does_not_start(self):
        result = self.run_entrypoint("172.17.0.5")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no address on the media network", result.stderr)


if __name__ == "__main__":
    unittest.main()
