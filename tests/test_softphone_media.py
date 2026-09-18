"""The browser softphone's media goes through the TURN relay, and each call leg's RTP listens on
one address only: the browser leg on the internal media network, the IMS leg in the tunnel."""
import importlib.util
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader

from control.app import main

ROOT = Path(__file__).resolve().parent.parent
SOFTPHONE_LIB = (ROOT / "webui/src/softphone.js").read_text(encoding="utf-8")
GLOBAL = (ROOT / "webui/src/GlobalSoftphone.jsx").read_text(encoding="utf-8")
VIEW = (ROOT / "webui/src/views/Softphone.jsx").read_text(encoding="utf-8")
I18N = (ROOT / "webui/src/i18n.jsx").read_text(encoding="utf-8")


def render(template: str, **ctx) -> str:
    env = Environment(loader=FileSystemLoader(str(ROOT / "engine" / "templates")),
                      trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    base = dict(webrtc_enable=True, webrtc_user="webrtc", realm="ims.example", pcscf="2001:db8::a",
                pcscf_is_v6=True, media_addr="172.29.1.10", ims_media_addr="2001:db8::5",
                rtp_start=10000, rtp_end=10011, advertise_addr="192.0.2.7")
    return env.get_template(template).render(**{**base, **ctx})


def section(conf: str, header: str) -> str:
    """The body of the section introduced by exactly this header line."""
    for match in re.finditer(r"^(\[[^\n]*)\n(.*?)(?=^\[|\Z)", conf, re.M | re.S):
        body = match.group(2)
        # Endpoints are either declared with type=endpoint or inherit it from a template.
        if match.group(1).strip() == header and ("type=endpoint" in body or "(endpoint" in header):
            return body
    raise AssertionError(f"no endpoint section {header}")


class LegBindingTests(unittest.TestCase):
    def test_browser_leg_rtp_binds_to_the_media_network_only(self):
        endpoint = section(render("pjsip.conf.j2"), "[webrtc](endpoint-local)")
        self.assertIn("media_address=172.29.1.10\n", endpoint)
        self.assertIn("bind_rtp_to_media_address=yes\n", endpoint)

    def test_ims_leg_rtp_binds_to_the_tunnel_address(self):
        endpoint = section(render("pjsip.conf.j2"), "[volte_ims]")
        self.assertIn("media_address=2001:db8::5\n", endpoint)
        self.assertIn("bind_rtp_to_media_address=yes\n", endpoint)
        # Before the tunnel has an address the IMS endpoint falls back to the transport.
        early = section(render("pjsip.conf.j2", ims_media_addr=""), "[volte_ims]")
        self.assertNotIn("media_address", early)

    def test_nothing_advertises_a_host_media_address_any_more(self):
        conf = render("pjsip.conf.j2")
        self.assertNotIn("external_media_address", conf)
        self.assertIn("external_signaling_address=192.0.2.7", conf)
        rtp = render("rtp.conf.j2")
        self.assertIn("icesupport=yes", rtp)
        self.assertNotIn("ice_host_candidates", rtp)
        self.assertNotIn("stunaddr", rtp)


class RenderContextTests(unittest.TestCase):
    @staticmethod
    def render_module():
        spec = importlib.util.spec_from_file_location("mdd_render", ROOT / "engine" / "render.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_tunnel_address_is_the_global_address_of_the_pcscf_family(self):
        render = self.render_module()
        out = ("5: ipsec0    inet6 2001:db8::5/64 scope global \\       valid_lft forever\n")
        with patch.object(render.subprocess, "check_output", return_value=out) as run:
            self.assertEqual(render.tunnel_address(True), "2001:db8::5")
        self.assertIn("-6", run.call_args[0][0])
        with patch.object(render.subprocess, "check_output", side_effect=OSError):
            self.assertEqual(render.tunnel_address(False), "")

    def test_watcher_and_render_agree_on_how_the_tunnel_address_is_read(self):
        # swu_ike cannot import render.py, so the two copies must stay the same command.
        render_src = (ROOT / "engine/render.py").read_text()
        swu_src = (ROOT / "engine/swu_ike.py").read_text()
        command = '["ip", "-o", "-6" if v6 else "-4", "addr", "show", "dev", "ipsec0", "scope", "global"]'
        self.assertIn(command, render_src)
        self.assertIn(command, swu_src)
        self.assertIn('f.write(f"{ctx[\'pcscf\']} {ctx[\'ims_media_addr\']}")', render_src)
        self.assertIn('current = "%s %s" % (addr, swu_tunnel_address(":" in addr))', swu_src)


class ProvisioningTests(unittest.TestCase):
    def test_softphone_is_told_to_use_the_relay_only(self):
        inst = {"id": "1", "mcc": "505", "mnc": "02",
                "sip": {"webrtc": {"username": "webrtc", "password": "p"}}}
        request = SimpleNamespace(headers={"host": "gw.example:8443"},
                                  url=SimpleNamespace(hostname="gw.example"))
        with patch.object(main.cfg, "get_instance", return_value=inst), \
                patch.object(main.turn, "secret", return_value="s"), \
                patch.object(main.turn, "status", return_value={"running": True}):
            prov = main.api_softphone("1", request)
        self.assertEqual(prov["ice_transport_policy"], "relay")
        self.assertTrue(prov["relay_ready"])
        self.assertEqual(prov["ice_servers"][0]["urls"][0],
                         f"turn:gw.example:{main.turn.PUBLIC_PORT}?transport=udp")


class BrowserTests(unittest.TestCase):
    def test_calls_and_answers_use_fresh_relay_configuration(self):
        pc = SOFTPHONE_LIB[SOFTPHONE_LIB.index("  async _pcConfig()"):]
        pc = pc[:pc.index("\n  }\n") + 4]
        self.assertIn("await this._provision()", pc)
        self.assertIn("iceServers: prov.ice_servers", pc)
        self.assertIn("iceTransportPolicy: prov.ice_transport_policy", pc)
        self.assertNotIn("iceServers: []", SOFTPHONE_LIB)
        for method in ("  async call(number) {", "  async answer() {"):
            body = SOFTPHONE_LIB[SOFTPHONE_LIB.index(method):]
            body = body[:body.index("\n  }\n")]
            self.assertIn("await this._pcConfig()", body)
            self.assertIn("pcConfig", body[body.index("_acquireLocal"):])

    def test_the_first_relay_candidate_sends_the_offer_or_answer(self):
        handler = SOFTPHONE_LIB[SOFTPHONE_LIB.index("session.on('icecandidate'"):]
        handler = handler[:handler.index("\n    })")]
        self.assertIn("event.candidate.type !== 'relay'", handler)
        self.assertIn("event.ready()", handler)

    def test_an_unreachable_relay_ends_the_attempt_early_and_says_why(self):
        body = SOFTPHONE_LIB[SOFTPHONE_LIB.index("const relayUnreachable = () => {"):]
        body = body[:body.index("session.on('ended', () => clearTimeout(relayTimer))")]
        # Only once gathering has started, so an incoming call may ring as long as it likes.
        self.assertIn("state === 'gathering' && !relayTimer", body)
        # JsSIP creates an outgoing call's connection before handleSession can listen for it.
        self.assertIn("watchGathering(session.connection)", body)
        self.assertIn("session.on('peerconnection', ({ peerconnection }) => watchGathering(peerconnection))", body)
        self.assertIn("setTimeout(relayUnreachable, RELAY_GATHER_TIMEOUT_MS)", body)
        self.assertIn("state === 'complete') relayUnreachable()", body)
        self.assertIn("if (relayCandidate || this.session !== session) return", body)
        self.assertIn("this.emit('relayunreachable')", body)
        self.assertIn("session.terminate()", body)

    def test_both_softphones_can_refresh_provisioning_and_report_relay_problems(self):
        zh = I18N[I18N.index("const zh"):I18N.index("const en")]
        for event, name in (("relayunavailable", "RELAY_UNAVAILABLE"),
                            ("relayunreachable", "RELAY_UNREACHABLE")):
            for source in (GLOBAL, VIEW):
                self.assertIn("() => api.softphone(id))", source)
                self.assertIn(f"'{event}'", source)
                self.assertIn(f"t({name})", source)
            key = SOFTPHONE_LIB[SOFTPHONE_LIB.index(f"{name} ="):].split("'")[1]
            self.assertIn(f"'{key}'", zh)


if __name__ == "__main__":
    unittest.main()
