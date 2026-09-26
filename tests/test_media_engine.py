"""The engine's side of the two media modes: what each renders and how its container is made.

Direct mode must render exactly what it did before relay mode existed; relay mode changes only
the browser leg, never the IMS leg inside the tunnel.
"""
import importlib
import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jinja2 import Environment, FileSystemLoader

from control.app import config

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "engine" / "templates"
RELAY = {"mode": "relay", "subnet": "172.30.0.0/16", "rtp_start": 10000, "rtp_end": 10199}


def engine_render():
    spec = importlib.util.spec_from_file_location("mdd_render_media", ROOT / "engine" / "render.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def instance_json(media=None) -> dict:
    cfg = {
        "id": "3", "imsi": "001010000000000", "mcc": "001", "mnc": "01",
        "ami_secret": "test-secret", "local_addr": "172.17.0.2", "pcscf": "10.0.0.9",
        "msisdn": "61400000000", "rtp_start": 12000, "rtp_end": 12011,
        "sip": {"webrtc": {"enable": True, "password": "test-password"},
                "advertise_address": "192.168.1.5", "ice_advertise_address": "192.168.1.5"},
    }
    if media:
        cfg["media"] = media
        cfg["rtp_start"], cfg["rtp_end"] = media["rtp_start"], media["rtp_end"]
    return cfg


def render(media=None, interface=("eth1", "172.30.0.3")) -> dict:
    module = engine_render()
    with patch.object(module, "media_interface", return_value=interface):
        ctx = module.build_context(instance_json(media))
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), trim_blocks=True,
                      lstrip_blocks=True, keep_trailing_newline=True)
    return {name: env.get_template(f"{name}.conf.j2").render(**ctx) for name in ("rtp", "pjsip")}


def section(text: str, header: str, type_: str) -> str:
    """The body of the pjsip section ``[header]`` whose type= is ``type_``."""
    for block in re.split(r"(?m)^(?=\[)", text):
        # [webrtc](endpoint-local) takes its type from the template it names.
        if (block.startswith(f"[{header}]") and f"type={type_}\n" in block
                or block.startswith(f"[{header}]({type_}-local)")):
            return block
    raise AssertionError(f"no [{header}] type={type_}")


class MediaRenderTests(unittest.TestCase):
    def test_direct_mode_rewrites_the_host_candidate_and_publishes_the_lan_address(self):
        out = render()
        self.assertIn("[ice_host_candidates]\n172.17.0.2 => 192.168.1.5,include_local_address",
                      out["rtp"])
        self.assertIn("rtpstart=12000\nrtpend=12011", out["rtp"])
        self.assertIn("external_media_address=192.168.1.5", out["pjsip"])
        self.assertNotIn("media_address=", section(out["pjsip"], "webrtc", "endpoint")
                         .replace("external_media_address", ""))

    def test_relay_mode_offers_only_the_media_address_to_the_browser(self):
        out = render(RELAY)
        self.assertIn("icesupport=yes", out["rtp"])
        self.assertNotIn("ice_host_candidates", out["rtp"])
        self.assertIn("rtpstart=10000\nrtpend=10199", out["rtp"])
        self.assertNotIn("external_media_address", out["pjsip"])
        # Signalling still names the host, exactly as in direct mode.
        self.assertIn("external_signaling_address=192.168.1.5", out["pjsip"])
        webrtc = section(out["pjsip"], "webrtc", "endpoint")
        self.assertIn("media_address=172.30.0.3\nbind_rtp_to_media_address=yes", webrtc)

    def test_relay_mode_leaves_the_ims_leg_as_it_was(self):
        direct, relay = render(), render(RELAY)
        for type_ in ("transport", "registration", "endpoint", "aor", "identify"):
            self.assertEqual(section(direct["pjsip"], "volte_ims", type_),
                             section(relay["pjsip"], "volte_ims", type_))
        self.assertNotIn("media_address", section(relay["pjsip"], "volte_ims", "endpoint"))

    def test_relay_mode_without_a_media_address_binds_nothing_specific(self):
        out = render(RELAY, interface=("", ""))
        self.assertNotIn("bind_rtp_to_media_address", out["pjsip"])
        self.assertNotIn("ice_host_candidates", out["rtp"])

    def test_the_media_interface_admits_only_rtp_to_specifically_bound_sockets(self):
        rules = engine_render().media_ruleset("eth1", 10000, 10199)
        self.assertIn('iifname "eth1" udp dport 10000-10199 socket wildcard 0 accept', rules)
        self.assertIn('iifname "eth1" drop', rules)
        self.assertIn("table inet mdd_media", rules)
        # Reloadable in place: the table is created, dropped and defined again.
        self.assertLess(rules.index("delete table inet mdd_media"),
                        rules.index("table inet mdd_media {"))

    def test_the_media_interface_is_found_by_subnet(self):
        module = engine_render()
        out = ("1: lo    inet 127.0.0.1/8 scope host lo\n"
               "40: eth0@if41    inet 172.17.0.2/16 brd 172.17.255.255 scope global eth0\n"
               "42: eth1@if43    inet 172.30.0.3/16 brd 172.30.255.255 scope global eth1\n"
               "44: ipsec0    inet 10.44.1.2/32 scope global ipsec0\n")
        with patch.object(module.subprocess, "check_output", return_value=out):
            self.assertEqual(module.media_interface("172.30.0.0/16"), ("eth1", "172.30.0.3"))
            self.assertEqual(module.media_interface("10.9.0.0/16"), ("", ""))

    def test_render_writes_the_ruleset_and_mode_only_in_relay_mode(self):
        for media, expected in ((None, False), (RELAY, True)):
            module = engine_render()
            with tempfile.TemporaryDirectory() as tmp:
                cfg_path = Path(tmp, "instance.json")
                cfg_path.write_text(__import__("json").dumps(instance_json(media)))
                env_path = Path(tmp, "run", "engine.env")
                outputs = Path(tmp, "out")
                with patch.object(module, "CFG_PATH", str(cfg_path)), \
                        patch.object(module, "TPL_DIR", str(TEMPLATES)), \
                        patch.object(module, "media_interface",
                                     return_value=("eth1", "172.30.0.3")), \
                        patch.dict(os.environ, {"MDD_ENV": str(env_path)}), \
                        patch.object(module.os, "makedirs", lambda *a, **k: None), \
                        patch("builtins.open", _redirect_open(outputs, tmp)):
                    Path(tmp, "run").mkdir()
                    outputs.mkdir()
                    module.main()
                env_text = env_path.read_text()
                self.assertEqual(Path(tmp, "run", "media.nft").exists(), expected)
                self.assertEqual("MDD_MEDIA_MODE=relay" in env_text, expected)
                self.assertEqual("MDD_MEDIA_IF=eth1" in env_text, expected)


def _redirect_open(outputs: Path, tmp: str):
    """Send render.main's writes to /etc and /usr into a scratch directory."""
    real_open = open

    def fake_open(path, mode="r", *args, **kwargs):
        path = str(path)
        if not path.startswith(tmp) and ("w" in mode):
            path = str(outputs / path.strip("/").replace("/", "_"))
        return real_open(path, mode, *args, **kwargs)
    return fake_open


class MediaInstanceJsonTests(unittest.TestCase):
    def instance(self, **extra):
        return {"id": "3", "index": 1, "imsi": "001010000000000", "mcc": "001", "mnc": "01",
                "ami_secret": "a", "ports": config._alloc_ports(1),
                "sip": {"webrtc": {"enable": True, "password": "p"}}, **extra}

    def test_direct_mode_keeps_the_lines_own_rtp_block(self):
        rendered = config.render_instance_json(self.instance(), {})
        self.assertNotIn("media", rendered)
        self.assertEqual(rendered["rtp_start"], config._alloc_ports(1)["rtp_start"])

    def test_relay_mode_uses_the_shared_range_and_keeps_the_saved_block(self):
        inst = self.instance(media=RELAY)
        rendered = config.render_instance_json(inst, {})
        self.assertEqual(rendered["media"], RELAY)
        self.assertEqual((rendered["rtp_start"], rendered["rtp_end"]), (10000, 10199))
        self.assertEqual(inst["ports"], config._alloc_ports(1))


def _docker_errors():
    not_found = type("NotFound", (Exception,), {})
    return SimpleNamespace(NotFound=not_found,
                           ImageNotFound=type("ImageNotFound", (not_found,), {}))


class MediaEngineContainerTests(unittest.TestCase):
    def engine_module(self):
        with patch.dict(sys.modules, {"docker": SimpleNamespace(from_env=lambda **_: None,
                                                                 errors=_docker_errors())}):
            for name in ("control.app.engine", "control.app.media"):
                sys.modules.pop(name, None)
            return importlib.import_module("control.app.engine")

    def start(self, engine, attachment):
        calls = []
        container = Mock(id="cid", name="engine")

        class Containers:
            def get(self, name):
                raise engine.docker.errors.NotFound(name)

            def run(self, image, **kwargs):
                calls.append(("run", kwargs))
                return container

            def create(self, image, **kwargs):
                calls.append(("create", kwargs))
                return container

        client = SimpleNamespace(containers=Containers())
        inst = {"id": "sim1", "ports": {"sip_udp": 5070, "sip_tls": 5071, "ami": 5048,
                                        "rtp_start": 12000, "rtp_span": 12}}
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(engine, "_client", lambda: client), \
                patch.object(engine, "ENGINE_NETWORK", ""), \
                patch.object(engine, "_instance_paths", lambda iid: (temp, temp)), \
                patch.object(engine, "_clear_runtime_state", lambda base: None), \
                patch.object(engine.egress, "ensure_line", lambda i, s: None), \
                patch.object(engine.media, "engine_attachment", return_value=attachment), \
                patch.object(engine.cfg, "write_instance_json") as write:
            engine.start(inst, {})
        return calls, container, write.call_args[0][0]

    def test_direct_mode_runs_the_engine_as_before(self):
        engine = self.engine_module()
        calls, _container, written = self.start(engine, None)
        self.assertEqual([kind for kind, _ in calls], ["run"])
        kwargs = calls[0][1]
        self.assertEqual(sorted(kwargs["ports"]), [f"{p}/udp" for p in range(12000, 12012)])
        self.assertNotIn(engine.media.MODE_LABEL, kwargs["labels"])
        self.assertNotIn("media", written)

    def test_relay_mode_publishes_nothing_and_joins_the_media_network_before_starting(self):
        engine = self.engine_module()
        network = Mock()
        order = []
        network.connect.side_effect = lambda c: order.append("connect")
        attachment = {"network": network, "instance": RELAY}
        calls, container, written = self.start(engine, attachment)
        container.start.side_effect = None
        self.assertEqual([kind for kind, _ in calls], ["create"])
        kwargs = calls[0][1]
        self.assertEqual(kwargs["ports"], {})
        self.assertEqual(kwargs["labels"][engine.media.MODE_LABEL], "relay")
        network.connect.assert_called_once_with(container)
        container.start.assert_called_once_with()
        self.assertEqual(written["media"], RELAY)

    def test_a_line_whose_media_network_cannot_be_joined_is_not_left_half_made(self):
        engine = self.engine_module()
        network = Mock()
        network.connect.side_effect = RuntimeError("gone")
        with self.assertRaises(RuntimeError):
            _calls, container, _written = self.start(
                engine, {"network": network, "instance": RELAY})

    def test_control_never_addresses_an_engine_by_its_media_address(self):
        engine = self.engine_module()
        container = SimpleNamespace(
            status="running", id="cid",
            attrs={"NetworkSettings": {"Networks": {
                engine.media.NETWORK: {"IPAddress": "172.30.0.3"},
                "bridge": {"IPAddress": "172.17.0.2"}}}, "RestartCount": 0, "State": {}})
        client = SimpleNamespace(containers=SimpleNamespace(get=lambda name: container))
        with patch.object(engine, "_client", lambda: client), \
                patch.object(engine, "ENGINE_NETWORK", ""):
            self.assertEqual(engine.container_runtime("1")["ip"], "172.17.0.2")


if __name__ == "__main__":
    unittest.main()
