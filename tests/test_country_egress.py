import base64
import json
import os
import time
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from control.app import egress
from host.mdd_orchestrator import (Orchestrator, clash_outbound, node_needs_xray,
                                   parse_manual_outbound, parse_proxy_url, parse_share_link,
                                   xray_outbound)


class CountryEgressTests(unittest.TestCase):
    def test_mcc_mapping_and_override(self):
        self.assertEqual(egress.country_for_mcc("234"), "gb")
        self.assertEqual(egress.line_country({"mcc": "234", "proxy_country": "US"}), "us")

    def test_epdg_name(self):
        self.assertEqual(egress.epdg_for({"mcc": "310", "mnc": "260"}),
                         "epdg.epc.mnc260.mcc310.pub.3gppnetwork.org")

    def test_manual_proxy_url(self):
        outbound = parse_proxy_url("socks5://alice:secret@127.0.0.1:1080", "exit-gb")
        self.assertEqual(outbound["type"], "socks")
        self.assertEqual(outbound["username"], "alice")

    def test_existing_country_outbounds_are_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            existing = root / "existing.json"
            existing.write_text(json.dumps({"outbounds": [
                {"type": "socks", "tag": "uk", "server": "127.0.0.1", "server_port": 1080},
                {"type": "socks", "tag": "us", "server": "127.0.0.1", "server_port": 1081},
            ]}))
            app = Orchestrator(root / "data", Path.cwd(), dry_run=True)
            config, states = app.build_proxy_config({
                "existing_singbox_config": str(existing),
                "exits": {
                    "gb": {"enabled": True, "mode": "existing", "outbound_tag": "uk"},
                    "us": {"enabled": True, "mode": "existing", "outbound_tag": "us"},
                },
            })
            self.assertTrue(states["gb"]["ready"])
            self.assertNotEqual(states["gb"]["interface"], states["us"]["interface"])
            self.assertEqual({x["tag"] for x in config["outbounds"]}, {"exit-gb", "exit-us"})

    def test_urltest_tag_is_mapped_to_subscription_node_name(self):
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *_): self.close()

        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp), Path.cwd(), dry_run=True)
            state = {"gb": {"ready": True, "mode": "subscription", "node": "",
                            "_member_names": {"exit-gb-1": "WG-UK London"}}}
            with patch("host.mdd_orchestrator.urllib.request.urlopen",
                       return_value=Response(b'{"now":"exit-gb-1"}')):
                app.update_selected_nodes(state)
            self.assertEqual(state["gb"]["node"], "WG-UK London")
            self.assertEqual(state["gb"]["node_tag"], "exit-gb-1")
            self.assertNotIn("_member_names", state["gb"])


class SubscriptionNodeConversionTests(unittest.TestCase):
    """Fields dropped here produce outbounds that look valid and never connect."""

    def test_hysteria2_obfuscation_is_carried_over(self):
        outbound = clash_outbound({
            "name": "US VPS", "type": "hysteria2", "server": "s.example.net", "port": 443,
            "password": "pw", "sni": "s.example.net",
            "obfs": "salamander", "obfs-password": "obfs-pw"}, "exit-us")
        self.assertEqual(outbound["obfs"], {"type": "salamander", "password": "obfs-pw"})

    def test_hysteria2_alpn_is_carried_over(self):
        outbound = clash_outbound({
            "name": "N", "type": "hysteria2", "server": "h.example.net", "port": 8882,
            "password": "pw", "sni": "addons.mozilla.org", "alpn": ["h3"],
            "skip-cert-verify": True}, "exit-us")
        self.assertEqual(outbound["tls"]["alpn"], ["h3"])
        self.assertTrue(outbound["tls"]["insecure"])

    def test_vless_reality_is_carried_over_with_utls(self):
        outbound = clash_outbound({
            "name": "KR FF", "type": "vless", "server": "r.example.net", "port": 48781,
            "uuid": "uuid-1", "tls": True, "network": "tcp", "flow": "xtls-rprx-vision",
            "servername": "www.microsoft.com", "client-fingerprint": "chrome",
            "skip-cert-verify": False,
            "reality-opts": {"public-key": "pubkey", "short-id": "abcd"}}, "exit-kr")
        self.assertEqual(outbound["tls"]["reality"],
                         {"enabled": True, "public_key": "pubkey", "short_id": "abcd"})
        self.assertEqual(outbound["tls"]["utls"], {"enabled": True, "fingerprint": "chrome"})
        self.assertEqual(outbound["tls"]["server_name"], "www.microsoft.com")
        # REALITY does its own server authentication.
        self.assertFalse(outbound["tls"]["insecure"])

    def test_reality_without_a_fingerprint_still_gets_utls(self):
        outbound = clash_outbound({
            "name": "R", "type": "vless", "server": "r.example.net", "port": 443,
            "uuid": "uuid-1", "tls": True,
            "reality-opts": {"public-key": "pubkey", "short-id": ""}}, "exit-kr")
        # sing-box requires uTLS alongside REALITY.
        self.assertTrue(outbound["tls"]["utls"]["enabled"])

    def test_nodes_without_these_extras_are_unchanged(self):
        outbound = clash_outbound({
            "name": "T", "type": "trojan", "server": "t.example.net", "port": 443,
            "password": "pw", "sni": "t.example.net"}, "exit-us")
        for absent in ("obfs", "reality", "utls", "alpn"):
            self.assertNotIn(absent, outbound.get("tls", {}))
            self.assertNotIn(absent, outbound)


class PastedNodeTests(unittest.TestCase):
    """A node handed over as a share link must work without hand-conversion."""

    def test_vless_link_keeps_tls_and_websocket_transport(self):
        outbound = parse_manual_outbound(
            "vless://uuid-1@us.example.net:443?type=ws&security=tls&sni=cdn.example.net"
            "&path=%2Fws&host=cdn.example.net&flow=xtls-rprx-vision#US-01", "exit-us")
        self.assertEqual(outbound["type"], "vless")
        self.assertEqual((outbound["server"], outbound["server_port"]), ("us.example.net", 443))
        self.assertEqual(outbound["uuid"], "uuid-1")
        self.assertEqual(outbound["flow"], "xtls-rprx-vision")
        self.assertEqual(outbound["tls"]["server_name"], "cdn.example.net")
        self.assertEqual(outbound["transport"], {"type": "ws", "path": "/ws",
                                                 "headers": {"Host": "cdn.example.net"}})

    def test_reality_link_keeps_public_key_short_id_and_fingerprint(self):
        node = parse_share_link(
            "vless://uuid-1@reality.example.net:443?security=reality&type=tcp"
            "&sni=www.microsoft.com&fp=firefox&pbk=public-key&sid=abcd")
        outbound = clash_outbound(node, "exit-us")
        self.assertEqual(outbound["tls"]["reality"]["public_key"], "public-key")
        self.assertEqual(outbound["tls"]["reality"]["short_id"], "abcd")
        self.assertEqual(outbound["tls"]["utls"]["fingerprint"], "firefox")

    def test_reality_xhttp_link_builds_native_xray_outbound(self):
        node = parse_share_link(
            "vless://uuid-2@xhttp.example.net:443?security=reality&type=xhttp"
            "&sni=www.microsoft.com&fp=chrome&pbk=public-key&sid=0123"
            "&host=cdn.example.net&path=%2Fupdates&mode=packet-up&packetEncoding=xudp")
        outbound = xray_outbound(node, "out-one")
        stream = outbound["streamSettings"]
        self.assertEqual(stream["network"], "xhttp")
        self.assertEqual(stream["realitySettings"]["publicKey"], "public-key")
        self.assertEqual(stream["xhttpSettings"]["path"], "/updates")
        self.assertEqual(outbound["settings"]["vnext"][0]["users"][0]["packetEncoding"],
                         "xudp")

    def _packet_encoding(self, node):
        return xray_outbound(node, "out-one")["settings"]["vnext"][0]["users"][0][
            "packetEncoding"]

    def test_a_node_that_declares_a_packet_encoding_keeps_it(self):
        """packetEncoding decides how UDP rides the VLESS connection, so the node decides.

        The tunnel these exits carry is IKEv2/ESP on UDP 500 and 4500. A server that speaks
        packetaddr and is handed xudp does not fail loudly — UDP simply stops arriving, the
        tunnel never comes up, and the failover logic goes looking for a reason on the SIM
        side. Defaulting to xudp is right; overwriting a declared value with it is not, and
        nothing else in the suite would notice if the default were hardcoded.
        """
        node = parse_share_link(
            "vless://uuid-3@r.example.net:443?security=reality&type=tcp"
            "&sni=www.microsoft.com&fp=chrome&pbk=public-key&sid=0123")
        node["packet-encoding"] = "packetaddr"
        self.assertEqual(self._packet_encoding(node), "packetaddr")

    def test_a_node_with_no_packet_encoding_falls_back_to_xudp(self):
        # Both the parser (no packetEncoding in the link) and the builder (an empty value on
        # the node) have to land on the same default, or a link and a hand-edited profile
        # would behave differently for the same node.
        node = parse_share_link(
            "vless://uuid-4@r.example.net:443?security=reality&type=tcp"
            "&sni=www.microsoft.com&fp=chrome&pbk=public-key&sid=0123")
        self.assertEqual(self._packet_encoding(node), "xudp")
        node["packet-encoding"] = ""
        self.assertEqual(self._packet_encoding(node), "xudp")

    def test_xhttp_profile_is_bridged_through_loopback_socks(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp), Path.cwd(), dry_run=True)
            config, states = app.build_proxy_config({
                "profiles": {"node-one": {"name": "Tokyo XHTTP", "type": "node",
                    "value": "vless://uuid@x.example.net:443?security=reality&type=xhttp"
                             "&sni=www.microsoft.com&pbk=public-key&sid=abcd"}},
                "exits": {"jp": {"enabled": True, "profile_id": "node-one"}},
            })
            bridge = next(x for x in config["outbounds"] if x["tag"] == "exit-jp")
            self.assertEqual((bridge["type"], bridge["server"]), ("socks", "127.0.0.1"))
            self.assertEqual(app.next_xray_config["inbounds"][0]["settings"]["udp"], True)
            self.assertEqual(states["jp"]["node"], "Tokyo XHTTP")

    def test_trojan_link_honours_insecure_flag(self):
        outbound = parse_manual_outbound(
            "trojan://s3cr3t@us2.example.net:4001?sni=us2.example.net&allowInsecure=1#US-02",
            "exit-us")
        self.assertEqual(outbound["type"], "trojan")
        self.assertEqual(outbound["password"], "s3cr3t")
        self.assertTrue(outbound["tls"]["insecure"])

    def test_hysteria2_link_and_its_short_scheme(self):
        for link in ("hysteria2://pa55@us3.example.net:8882?sni=us3.example.net#US-03",
                     "hy2://pa55@us3.example.net:8882?sni=us3.example.net#US-03"):
            outbound = parse_manual_outbound(link, "exit-us")
            self.assertEqual(outbound["type"], "hysteria2")
            self.assertEqual(outbound["password"], "pa55")
            # QUIC-based: it must not gain a stream transport.
            self.assertNotIn("transport", outbound)

    def test_shadowsocks_link_in_both_encodings(self):
        # ss://base64(method:password)@host:port
        split = parse_manual_outbound(
            "ss://YWVzLTI1Ni1nY206cGFzc3dvcmQ=@us5.example.net:8388#US-05", "exit-us")
        # ss://base64(method:password@host:port)
        joined = parse_manual_outbound(
            "ss://YWVzLTI1Ni1nY206cGFzc3dvcmRAdXM1LmV4YW1wbGUubmV0OjgzODg=#US-05", "exit-us")
        for outbound in (split, joined):
            self.assertEqual(outbound["type"], "shadowsocks")
            self.assertEqual(outbound["method"], "aes-256-gcm")
            self.assertEqual(outbound["password"], "password")
            self.assertEqual(outbound["server"], "us5.example.net")
            self.assertEqual(outbound["server_port"], 8388)

    def test_vmess_link_is_a_base64_json_blob(self):
        payload = base64.b64encode(json.dumps({
            "add": "us6.example.net", "port": 443, "id": "uuid-2", "aid": 0,
            "net": "ws", "path": "/vm", "host": "cdn.example.net", "tls": "tls",
        }).encode()).decode()
        outbound = parse_manual_outbound(f"vmess://{payload}", "exit-us")
        self.assertEqual(outbound["type"], "vmess")
        self.assertEqual(outbound["uuid"], "uuid-2")
        self.assertEqual(outbound["transport"]["path"], "/vm")
        self.assertTrue(outbound["tls"]["enabled"])

    def test_socks5_url_still_works(self):
        outbound = parse_manual_outbound("socks5://alice:secret@127.0.0.1:1080", "exit-us")
        self.assertEqual(outbound["type"], "socks")
        self.assertEqual(outbound["username"], "alice")

    def test_raw_singbox_outbound_is_accepted_and_retagged(self):
        outbound = parse_manual_outbound(
            '{"type": "vless", "tag": "ignored", "server": "x.example.net",'
            ' "server_port": 443, "uuid": "u"}', "exit-us")
        self.assertEqual(outbound["tag"], "exit-us")
        self.assertEqual(outbound["server"], "x.example.net")

    def test_tcp_only_node_is_rejected_before_it_can_break_ike(self):
        # An HTTP proxy cannot carry IKE's UDP; without this check the failure only shows up
        # much later as a tunnel that never establishes.
        with self.assertRaises(ValueError) as caught:
            parse_manual_outbound('{"type": "http", "server": "x.example.net",'
                                  ' "server_port": 8080}', "exit-us")
        self.assertIn("UDP", str(caught.exception))

    def test_unusable_input_is_reported_clearly(self):
        for value, expected in (("", "no node link"),
                                ("ftp://example.net:21", "unsupported node link scheme"),
                                ("{not json}", "not valid JSON")):
            with self.assertRaises(ValueError) as caught:
                parse_manual_outbound(value, "exit-us")
            self.assertIn(expected, str(caught.exception))


SUBSCRIPTION = {"proxies": [
    {"name": "US Alpha", "type": "trojan", "server": "a.test", "port": 443, "password": "x"},
    {"name": "US Bravo", "type": "trojan", "server": "b.test", "port": 443, "password": "x"},
]}


def _subscription_orchestrator(root):
    app = Orchestrator(root, Path.cwd(), dry_run=True)
    app.subscription = lambda *_args, **_kwargs: SUBSCRIPTION
    return app


def _build(app, exit_cfg):
    return app.build_proxy_config({
        "subscription_url": "https://example.test/sub",
        "exits": {"us": {"enabled": True, "mode": "subscription",
                         "keywords": ["US"], **exit_cfg}},
    })


class PinnedExitNodeTests(unittest.TestCase):
    def test_candidates_are_published_for_the_node_picker(self):
        with tempfile.TemporaryDirectory() as temp:
            _config, states = _build(_subscription_orchestrator(Path(temp)), {})
            self.assertEqual(states["us"]["candidates"], ["US Alpha", "US Bravo"])
            self.assertEqual(states["us"]["selection"], "managed")
            self.assertFalse(states["us"]["pinned_missing"])

    def test_pinned_node_is_fixed_with_a_selector(self):
        with tempfile.TemporaryDirectory() as temp:
            config, states = _build(_subscription_orchestrator(Path(temp)),
                                    {"pinned_node": "US Bravo"})
            exit_outbound = next(x for x in config["outbounds"] if x["tag"] == "exit-us")
            self.assertEqual(exit_outbound["type"], "selector")
            # Members are sorted by name, so "US Bravo" is index 1. Pinning must not switch.
            self.assertEqual(exit_outbound["default"], "exit-us-1")
            self.assertIn(exit_outbound["default"], exit_outbound["outbounds"])
            self.assertEqual(states["us"]["selection"], "manual")
            self.assertEqual(states["us"]["pinned_node"], "US Bravo")
            self.assertFalse(states["us"]["pinned_missing"])

    def test_pinned_node_missing_from_feed_falls_back_to_managed(self):
        with tempfile.TemporaryDirectory() as temp:
            config, states = _build(_subscription_orchestrator(Path(temp)),
                                    {"pinned_node": "US Retired"})
            exit_outbound = next(x for x in config["outbounds"] if x["tag"] == "exit-us")
            # Falling back stays inside this country's keyword pool, so it cannot leak into
            # another geography; the exit must stay ready and report the stale pin instead.
            self.assertEqual(exit_outbound["default"], "exit-us-0")
            self.assertTrue(states["us"]["ready"])
            self.assertTrue(states["us"]["pinned_missing"])
            # Managed, not manual: a stale pin must not freeze the exit on a dead node.
            self.assertEqual(states["us"]["selection"], "managed")


class ReselectAttributionTests(unittest.TestCase):
    """A node that carried a registered line for a long time is not the cause of a later
    failure, and moving the exit costs another teardown while changing nothing."""

    def _request(self, stable_for, exits=None):
        written = {}
        exits = exits if exits is not None else {"us": {"selection": "preferred",
                                                        "mode": "subscription",
                                                        "node": "US Alpha"}}
        real_read = egress._read_json
        # Only the reselect document is faked; the MCC country table must still load or
        # line_country() cannot resolve a country at all.
        def read(path):
            return {} if path == egress._RESELECT else real_read(path)

        with patch.object(egress, "status", lambda: {"exits": exits}), \
                patch.object(egress, "_read_json", read), \
                patch.object(egress, "_atomic_json",
                             lambda path, value: written.update(value)):
            country = egress.request_reselect({"id": "1", "mcc": "310"}, "health-freeze:x",
                                              stable_for=stable_for)
        return country, written

    def test_a_long_healthy_stretch_keeps_the_node(self):
        country, written = self._request(egress.RESELECT_MIN_STABLE_SECONDS + 1)
        self.assertEqual(country, "")
        self.assertEqual(written, {})

    def test_an_immediate_failure_still_moves_the_exit(self):
        country, written = self._request(5)
        self.assertEqual(country, "us")
        self.assertEqual(written["countries"]["us"]["node"], "US Alpha")

    def test_a_locked_exit_is_never_moved_however_briefly_it_worked(self):
        country, written = self._request(0, exits={"us": {"selection": "manual",
                                                          "mode": "subscription",
                                                          "node": "US Alpha"}})
        self.assertEqual(country, "")
        self.assertEqual(written, {})


class EpdgAddressRetentionTests(unittest.TestCase):
    """A carrier rotates its ePDG A record every few seconds while an IKE SA lives for hours.

    Routing only the newest answer revoked the route under a live tunnel, dropping that
    traffic onto the host default route — the wrong country, and the carrier then killed the
    SA. That is the fail-closed guarantee being broken by a DNS refresh.
    """

    def _app(self, temp):
        return Orchestrator(Path(temp), Path.cwd(), dry_run=True)

    def test_previous_answers_stay_routed(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._app(temp)
            app.retained_epdg_addresses("us:epdg.test", ["208.54.2.163"])
            app.retained_epdg_addresses("us:epdg.test", ["208.54.34.3"])
            kept = app.retained_epdg_addresses("us:epdg.test", ["208.54.39.163"])
            self.assertEqual(kept, ["208.54.2.163", "208.54.34.3", "208.54.39.163"])

    def test_addresses_age_out(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._app(temp)
            app.retained_epdg_addresses("us:epdg.test", ["208.54.2.163"])
            # Expire the first answer without touching the clock the code reads.
            app.epdg_seen["us:epdg.test"]["208.54.2.163"] = time.time() - 1
            self.assertEqual(app.retained_epdg_addresses("us:epdg.test", ["208.54.34.3"]),
                             ["208.54.34.3"])

    def test_a_repeated_answer_refreshes_its_lifetime(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._app(temp)
            app.retained_epdg_addresses("us:epdg.test", ["208.54.2.163"])
            first = app.epdg_seen["us:epdg.test"]["208.54.2.163"]
            app.retained_epdg_addresses("us:epdg.test", ["208.54.2.163"])
            self.assertGreaterEqual(app.epdg_seen["us:epdg.test"]["208.54.2.163"], first)

    def test_retention_is_capped(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._app(temp)
            from host import mdd_orchestrator as orch
            with patch.object(orch, "EPDG_ADDRESS_MAX", 3):
                for index in range(8):
                    kept = app.retained_epdg_addresses("us:epdg.test", [f"208.54.0.{index}"])
            # Bounded, and it is the oldest entries that go.
            self.assertEqual(kept, ["208.54.0.5", "208.54.0.6", "208.54.0.7"])

    def test_countries_do_not_share_retention(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._app(temp)
            app.retained_epdg_addresses("us:epdg.us", ["208.54.2.163"])
            self.assertEqual(app.retained_epdg_addresses("gb:epdg.gb", ["31.94.76.1"]),
                             ["31.94.76.1"])


class ManagedReselectTests(unittest.TestCase):
    """Node changes must follow line failures, never a latency timer."""

    def _orchestrator(self, temp, measurements):
        app = _subscription_orchestrator(Path(temp))
        app.selected = []
        app.measure_member = lambda tag: measurements.get(tag)
        app.select_member = lambda country, tag: app.selected.append((country, tag)) or True
        return app

    @staticmethod
    def _state(app, temp, exit_cfg=None):
        _config, states = _build(app, exit_cfg or {})
        return states

    def test_exit_is_a_selector_that_never_switches_on_its_own(self):
        with tempfile.TemporaryDirectory() as temp:
            config, states = _build(_subscription_orchestrator(Path(temp)), {})
            exit_outbound = next(x for x in config["outbounds"] if x["tag"] == "exit-us")
            # A urltest would re-rank on a timer and move the exit under a live IKE SA.
            self.assertEqual(exit_outbound["type"], "selector")
            self.assertNotIn("interval", exit_outbound)
            self.assertEqual(states["us"]["selection"], "managed")

    def test_reselect_avoids_the_node_that_just_failed(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {"exit-us-0": 300, "exit-us-1": 800})
            states = self._state(app, temp)
            app.reselect_path.parent.mkdir(parents=True, exist_ok=True)
            app.reselect_path.write_text(json.dumps({"countries": {"us": {
                "ts": time.time(), "reason": "health-freeze:reg_rejected", "node": "US Alpha"}}}))

            app.process_reselect_requests(states)

            # "US Alpha" is exit-us-0 and the fastest, but it is the node that just failed.
            self.assertEqual(app.selected, [("us", "exit-us-1")])
            self.assertEqual(states["us"]["node"], "US Bravo")
            self.assertIn("US Alpha", app.exit_cooldown["us"])

    def test_reselect_request_is_served_once(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {"exit-us-0": 300, "exit-us-1": 800})
            app.reselect_path.parent.mkdir(parents=True, exist_ok=True)
            app.reselect_path.write_text(json.dumps({"countries": {"us": {
                "ts": time.time(), "reason": "health-freeze:registering", "node": "US Alpha"}}}))

            app.process_reselect_requests(self._state(app, temp))
            app.process_reselect_requests(self._state(app, temp))

            self.assertEqual(len(app.selected), 1)

    def test_served_reselect_is_not_replayed_after_orchestrator_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {"exit-us-0": 300, "exit-us-1": 800})
            app.reselect_path.parent.mkdir(parents=True, exist_ok=True)
            app.reselect_path.write_text(json.dumps({"countries": {"us": {
                "ts": time.time(), "reason": "health-freeze:registering",
                "node": "US Alpha"}}}))
            app.process_reselect_requests(self._state(app, temp))
            self.assertEqual(len(app.selected), 1)

            restarted = self._orchestrator(temp, {"exit-us-0": 300, "exit-us-1": 800})
            restarted.process_reselect_requests(self._state(restarted, temp))
            self.assertEqual(restarted.selected, [])

    def test_stale_reselect_is_ignored_and_persistently_acknowledged(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {"exit-us-0": 300, "exit-us-1": 800})
            app.reselect_path.parent.mkdir(parents=True, exist_ok=True)
            stale_at = time.time() - 3600
            app.reselect_path.write_text(json.dumps({"countries": {"us": {
                "ts": stale_at, "reason": "health-freeze:registering",
                "node": "US Alpha"}}}))
            app.process_reselect_requests(self._state(app, temp))
            self.assertEqual(app.selected, [])
            handled = json.loads(app.reselect_handled_path.read_text())["countries"]
            self.assertEqual(handled["us"], stale_at)

    def test_failed_reselect_is_rate_limited(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {})
            app.measure_member = Mock(return_value=None)
            app.reselect_path.parent.mkdir(parents=True, exist_ok=True)
            app.reselect_path.write_text(json.dumps({"countries": {"us": {
                "ts": time.time(), "reason": "health-freeze:registering",
                "node": "US Alpha"}}}))
            states = self._state(app, temp)
            app.process_reselect_requests(states)
            first_probe_count = app.measure_member.call_count
            app.process_reselect_requests(states)
            self.assertEqual(app.measure_member.call_count, first_probe_count)
            self.assertEqual(app.reselect_retries["us"]["attempts"], 1)

    def test_failed_reselect_is_abandoned_after_bounded_attempts(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {})
            app.measure_member = Mock(return_value=None)
            app.reselect_path.parent.mkdir(parents=True, exist_ok=True)
            requested_at = time.time()
            app.reselect_path.write_text(json.dumps({"countries": {"us": {
                "ts": requested_at, "reason": "health-freeze:registering",
                "node": "US Alpha"}}}))
            states = self._state(app, temp)
            from host import mdd_orchestrator as orch
            with patch.object(orch, "EXIT_RESELECT_RETRY_SECONDS", 0), \
                    patch.object(orch, "EXIT_RESELECT_MAX_ATTEMPTS", 3):
                for _ in range(3):
                    app.process_reselect_requests(states)
            handled = json.loads(app.reselect_handled_path.read_text())["countries"]
            self.assertEqual(handled["us"], requested_at)
            self.assertNotIn("us", app.reselect_retries)

    def test_failed_initial_selection_is_rate_limited_but_never_abandoned(self):
        """Initial selection reaches ranking through the unranked set rather than a request, and
        a pool that answers nothing there would otherwise be re-probed on every reconcile cycle.
        Ranking is synchronous and each unreachable member costs seconds, which starves the modem
        and SIM work sharing this loop. There is no request to abandon, so unlike a reselect it
        keeps retrying until the pool answers — at the same slow cadence."""
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {})
            app.measure_member = Mock(return_value=None)
            config, states = _build(app, {})
            app.apply_singbox(config)          # dry_run: records the restart, marks it unranked
            from host import mdd_orchestrator as orch
            with patch.object(orch, "EXIT_RANK_WARMUP_SECONDS", 0):
                app.process_reselect_requests(states)
                swept = app.measure_member.call_count
                self.assertGreater(swept, 0)
                app.process_reselect_requests(states)
                self.assertEqual(app.measure_member.call_count, swept)   # no second sweep yet
                for _ in range(orch.EXIT_RESELECT_MAX_ATTEMPTS + 2):
                    self.assertIn("us", app.reselect_retries)            # never given up on
                    app.reselect_retries["us"]["next_at"] = 0.0          # the interval elapsed
                    app.process_reselect_requests(states)
            self.assertGreater(app.measure_member.call_count, swept)
            self.assertIn("us", app.exit_unranked)                       # still owed a ranking
            self.assertFalse(app.reselect_handled_path.exists())

    def test_preferred_pin_moves_away_from_a_failing_node(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {"exit-us-0": 300, "exit-us-1": 800})
            states = self._state(app, temp, {"pinned_node": "US Alpha", "pin_mode": "prefer"})
            self.assertEqual(states["us"]["selection"], "preferred")
            app.reselect_path.parent.mkdir(parents=True, exist_ok=True)
            app.reselect_path.write_text(json.dumps({"countries": {"us": {
                "ts": time.time(), "reason": "health-freeze:registering", "node": "US Alpha"}}}))

            app.process_reselect_requests(states)

            # Unlike a lock, a preferred pin gives way when its node is what just failed.
            self.assertEqual(app.selected, [("us", "exit-us-1")])

    def test_preferred_node_is_chosen_again_once_its_cooldown_expires(self):
        with tempfile.TemporaryDirectory() as temp:
            # "US Bravo" measures faster, but the preference outranks latency.
            app = self._orchestrator(temp, {"exit-us-0": 900, "exit-us-1": 100})
            states = self._state(app, temp, {"pinned_node": "US Alpha", "pin_mode": "prefer"})

            chosen = app.rank_and_select("us", states["us"], prefer="US Alpha")

            self.assertEqual(chosen, "exit-us-0")
            self.assertEqual(app.selected, [("us", "exit-us-0")])

    def test_one_failed_probe_does_not_void_the_preference(self):
        """The pinned node has occasional latency spikes; a single miss must not pass it over."""
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {})
            attempts = {"exit-us-0": [None, 400]}   # first probe times out, second succeeds
            app.measure_member = lambda tag: (attempts.get(tag, [800]).pop(0)
                                              if attempts.get(tag) else 800)
            states = self._state(app, temp, {"pinned_node": "US Alpha", "pin_mode": "prefer"})

            self.assertEqual(app.rank_and_select("us", states["us"], prefer="US Alpha"),
                             "exit-us-0")

    def test_preferred_node_is_skipped_while_it_is_unusable(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {"exit-us-0": None, "exit-us-1": 800})
            states = self._state(app, temp, {"pinned_node": "US Alpha", "pin_mode": "prefer"})

            self.assertEqual(app.rank_and_select("us", states["us"], prefer="US Alpha"),
                             "exit-us-1")

    def test_preferred_node_is_skipped_while_it_is_cooling_down(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {"exit-us-0": 100, "exit-us-1": 800})
            states = self._state(app, temp, {"pinned_node": "US Alpha", "pin_mode": "prefer"})
            app.exit_cooldown["us"] = {"US Alpha": time.time() + 900}

            self.assertEqual(app.rank_and_select("us", states["us"], prefer="US Alpha"),
                             "exit-us-1")

    def test_pinned_exit_ignores_reselect_requests(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {"exit-us-0": 300, "exit-us-1": 800})
            states = self._state(app, temp, {"pinned_node": "US Alpha"})
            app.reselect_path.parent.mkdir(parents=True, exist_ok=True)
            app.reselect_path.write_text(json.dumps({"countries": {"us": {
                "ts": time.time(), "reason": "health-freeze:registering", "node": "US Alpha"}}}))

            app.process_reselect_requests(states)

            # An operator pinned this exit; a failing line must not override that.
            self.assertEqual(app.selected, [])

    def test_cooldown_is_cleared_rather_than_leaving_no_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {"exit-us-0": 300, "exit-us-1": 800})
            app.exit_cooldown["us"] = {"US Alpha": time.time() + 900,
                                       "US Bravo": time.time() + 900}
            states = self._state(app, temp)

            chosen = app.rank_and_select("us", states["us"])

            self.assertEqual(chosen, "exit-us-0")
            self.assertEqual(app.exit_cooldown["us"], {})

    def test_unreachable_candidates_are_never_selected(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {"exit-us-0": None, "exit-us-1": 800})
            states = self._state(app, temp)

            self.assertEqual(app.rank_and_select("us", states["us"]), "exit-us-1")

            app.measure_member = lambda tag: None
            self.assertEqual(app.rank_and_select("us", states["us"]), "")

    def test_a_cold_singbox_is_not_ranked(self):
        """A QUIC outbound has to complete a fresh handshake after a restart and loses to TCP
        candidates that are far slower in steady state, which silently demoted a pinned node
        on every restart."""
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {"exit-us-0": 300, "exit-us-1": 800})
            config, states = _build(app, {})
            app.apply_singbox(config)          # marks the restart, starting the warm-up

            app.process_reselect_requests(states)
            self.assertEqual(app.selected, [])
            self.assertIn("us", app.exit_unranked)   # still pending, not discarded

            from host import mdd_orchestrator as orch
            with patch.object(orch, "EXIT_RANK_WARMUP_SECONDS", 0):
                app.process_reselect_requests(states)
            self.assertEqual(app.selected, [("us", "exit-us-0")])

    def test_a_first_start_ranks_the_pool(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {"exit-us-0": 300, "exit-us-1": 800})
            config, states = _build(app, {})
            app.apply_singbox(config)          # dry_run: records the restart only

            from host import mdd_orchestrator as orch
            with patch.object(orch, "EXIT_RANK_WARMUP_SECONDS", 0):
                app.process_reselect_requests(states)

            # Nothing was running before, so there is nothing to land back on.
            self.assertEqual(app.selected, [("us", "exit-us-0")])
            self.assertNotIn("us", app.exit_unranked)

    def test_a_restart_lands_back_on_the_node_that_was_running(self):
        """A config rewrite must not move a healthy line. Latency is the one input this
        design refuses to act on, and a restart is not a reason to start acting on it."""
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {"exit-us-0": 300, "exit-us-1": 800})
            # US Bravo is carrying tunnels even though US Alpha measures faster.
            app.last_exit_node["us"] = "US Bravo"
            config, states = _build(app, {})

            selector = next(x for x in config["outbounds"]
                            if x.get("tag") == "exit-us" and x["type"] == "selector")
            self.assertEqual(selector["default"], "exit-us-1")

            app.apply_singbox(config)
            self.assertNotIn("us", app.exit_unranked)
            self.assertEqual(app.last_exit_node.get("us"), "US Bravo")

            from host import mdd_orchestrator as orch
            with patch.object(orch, "EXIT_RANK_WARMUP_SECONDS", 0):
                app.process_reselect_requests(states)
            self.assertEqual(app.selected, [])      # nothing measured, nothing moved

    def test_persisting_a_selected_default_does_not_restart_any_country(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {})
            config, _states = _build(app, {})
            app.apply_singbox(config)
            running = Mock()
            running.poll.return_value = None
            app.singbox = running
            app.dry_run = False
            started_at = app.singbox_started_at
            app.exit_unranked.clear()
            app.last_exit_node["us"] = "US Bravo"
            updated, _states = _build(app, {})
            with patch("host.mdd_orchestrator.subprocess.Popen") as spawn:
                app.apply_singbox(updated)
                app.apply_singbox(updated)
            spawn.assert_not_called()
            running.terminate.assert_not_called()
            self.assertEqual(app.singbox_started_at, started_at)
            self.assertEqual(app.exit_unranked, set())
            self.assertEqual(json.loads(app.generated.read_text()), updated)
            self.assertEqual(app.last_proxy_config, updated)

    def test_real_config_changes_and_new_locks_still_restart(self):
        for change in ("outbound", "lock", "dead_process"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp:
                app = self._orchestrator(temp, {})
                app.last_exit_node["us"] = "US Bravo"
                config, _states = _build(app, {})
                app.apply_singbox(config)
                old = Mock()
                old.poll.return_value = 1 if change == "dead_process" else None
                app.singbox = old
                app.dry_run = False
                updated, _states = _build(app, {"pinned_node": "US Alpha"}
                                          if change == "lock" else {})
                if change == "outbound":
                    member = next(outbound for outbound in updated["outbounds"]
                                  if outbound.get("tag") == "exit-us-0")
                    member["server_port"] += 1
                replacement = Mock()
                replacement.poll.return_value = None
                with patch("host.mdd_orchestrator.shutil.which", return_value="sing-box"), \
                        patch("host.mdd_orchestrator.run", return_value=SimpleNamespace(returncode=0)), \
                        patch("host.mdd_orchestrator.time.sleep"), \
                        patch("host.mdd_orchestrator.subprocess.Popen", return_value=replacement) as spawn:
                    app.apply_singbox(updated)
                spawn.assert_called_once()
                self.assertIs(app.singbox, replacement)
                self.assertEqual(app.last_proxy_config, updated)

    def test_a_node_that_did_not_survive_the_rewrite_starts_over(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {"exit-us-0": 300, "exit-us-1": 800})
            app.last_exit_node["us"] = "US Gone"     # dropped by the feed
            config, states = _build(app, {})

            selector = next(x for x in config["outbounds"]
                            if x.get("tag") == "exit-us" and x["type"] == "selector")
            self.assertEqual(selector["default"], "exit-us-0")

            app.apply_singbox(config)
            self.assertIn("us", app.exit_unranked)
            self.assertNotIn("us", app.last_exit_node)

    def test_a_locked_pin_still_wins_over_what_was_running(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {"exit-us-0": 300, "exit-us-1": 800})
            app.last_exit_node["us"] = "US Bravo"
            config, _states = _build(app, {"pinned_node": "US Alpha"})
            selector = next(x for x in config["outbounds"]
                            if x.get("tag") == "exit-us" and x["type"] == "selector")
            self.assertEqual(selector["default"], "exit-us-0")

    def test_a_restart_does_not_undo_a_preferred_nodes_failure_fallback(self):
        """A preferred pin is retried when a failure asks us to select again, not when an
        unrelated config rewrite restarts sing-box while its fallback is carrying tunnels."""
        with tempfile.TemporaryDirectory() as temp:
            app = self._orchestrator(temp, {"exit-us-0": 300, "exit-us-1": 800})
            app.last_exit_node["us"] = "US Bravo"
            config, states = _build(app, {"pinned_node": "US Alpha", "pin_mode": "prefer"})

            selector = next(x for x in config["outbounds"]
                            if x.get("tag") == "exit-us" and x["type"] == "selector")
            self.assertEqual(selector["default"], "exit-us-1")

            app.apply_singbox(config)
            self.assertNotIn("us", app.exit_unranked)
            self.assertEqual(app.last_exit_node.get("us"), "US Bravo")
            from host import mdd_orchestrator as orch
            with patch.object(orch, "EXIT_RANK_WARMUP_SECONDS", 0):
                app.process_reselect_requests(states)
            self.assertEqual(app.selected, [])

    def test_an_orchestrator_restart_recovers_the_running_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "orchestrator"
            root.mkdir()
            (root / "proxy-status.json").write_text(json.dumps({"exits": {"us": {
                "ready": True,
                "node": "US Bravo",
                "last_change": {"from": "US Alpha", "to": "US Bravo"},
            }}}))

            app = self._orchestrator(temp, {"exit-us-0": 300, "exit-us-1": 800})
            config, _states = _build(app, {"pinned_node": "US Alpha", "pin_mode": "prefer"})
            selector = next(x for x in config["outbounds"]
                            if x.get("tag") == "exit-us" and x["type"] == "selector")

            self.assertEqual(app.last_exit_node["us"], "US Bravo")
            self.assertEqual(selector["default"], "exit-us-1")
            self.assertEqual(app.exit_last_change["us"]["to"], "US Bravo")


class ExitNodeHistoryTests(unittest.TestCase):
    def test_first_observation_is_not_recorded_as_a_switch(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp), Path.cwd(), dry_run=True)
            app.record_exit_node("us", {"node": "US Alpha", "node_tag": "exit-us-0"})
            self.assertFalse(app.exit_node_history.exists())

    def test_node_change_is_appended_with_both_names(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp), Path.cwd(), dry_run=True)
            app.record_exit_node("us", {"node": "US Alpha", "node_tag": "exit-us-0"})
            with patch("host.mdd_orchestrator.urllib.request.urlopen",
                       side_effect=OSError("clash api unavailable")):
                app.record_exit_node("us", {"node": "US Bravo", "node_tag": "exit-us-1"})
                # An unchanged node must not produce a second record.
                app.record_exit_node("us", {"node": "US Bravo", "node_tag": "exit-us-1"})
            records = [json.loads(line) for line
                       in app.exit_node_history.read_text().splitlines() if line]
            self.assertEqual(len(records), 1)
            self.assertEqual((records[0]["from"], records[0]["to"]), ("US Alpha", "US Bravo"))

    def test_history_is_bounded(self):
        from host.mdd_orchestrator import append_jsonl
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "history.jsonl"
            for index in range(12):
                append_jsonl(path, {"index": index}, limit=5)
            records = [json.loads(line) for line in path.read_text().splitlines() if line]
            self.assertEqual([x["index"] for x in records], [7, 8, 9, 10, 11])


if __name__ == "__main__":
    unittest.main()


class IdleBackoffTests(unittest.TestCase):
    """A full reconcile shells out about fifteen times. Repeating that every few seconds to
    re-derive an unchanged state was the largest source of process creation on the box."""

    def _app(self, temp):
        app = Orchestrator(Path(temp), Path.cwd(), interval=3.0, dry_run=True)
        app.root.mkdir(parents=True, exist_ok=True)
        return app

    def test_waiting_ends_early_when_an_input_document_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._app(temp)
            app.desired_path.write_text("{}")
            slept = []

            clock = [1000.0]

            def fake_sleep(seconds):
                slept.append(seconds)
                clock[0] += seconds
                if len(slept) == 2:              # an operator saves settings mid-wait
                    app.desired_path.write_text('{"changed": true}')

            with patch("host.mdd_orchestrator.time.sleep", fake_sleep), \
                    patch("host.mdd_orchestrator.time.time", lambda: clock[0]):
                app._sleep_for_work(60.0)
            # It returned long before the 60s backoff, without a full reconcile in between.
            self.assertLess(sum(slept), 60.0)
            self.assertEqual(len(slept), 2)

    def test_each_wait_slice_is_at_most_one_base_interval(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._app(temp)
            slept = []
            clock = [1000.0]

            # The clock has to advance with the fake sleep, or the wait spins on real time.
            def fake_sleep(seconds):
                slept.append(seconds)
                clock[0] += seconds

            with patch("host.mdd_orchestrator.time.sleep", fake_sleep), \
                    patch("host.mdd_orchestrator.time.time", lambda: clock[0]):
                app._sleep_for_work(15.0)
            self.assertTrue(all(x <= app.interval + 0.001 for x in slept),
                            "a long slice would delay noticing an operator action")
            self.assertAlmostEqual(sum(slept), 15.0, places=3)
            self.assertEqual(len(slept), 5)          # 15s in 3s slices

    def test_a_stop_request_is_not_ignored_while_backing_off(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._app(temp)
            calls = []

            clock = [1000.0]

            def fake_sleep(seconds):
                calls.append(seconds)
                clock[0] += seconds
                app.stop = True

            with patch("host.mdd_orchestrator.time.sleep", fake_sleep), \
                    patch("host.mdd_orchestrator.time.time", lambda: clock[0]):
                app._sleep_for_work(600.0)
            self.assertEqual(len(calls), 1)

    def test_missing_documents_do_not_break_the_comparison(self):
        with tempfile.TemporaryDirectory() as temp:
            app = self._app(temp)
            # None of them exist yet on a fresh install.
            self.assertEqual(len(app._input_mtimes()), 7)


class HotplugResponsivenessTests(unittest.TestCase):
    """Backing off must not make the hardware feel unresponsive: plugging a modem in is not a
    document change, so the wait has to notice the USB tree as well."""

    def test_the_usb_tree_is_part_of_the_change_detector(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp), Path.cwd(), interval=3.0, dry_run=True)
            app.root.mkdir(parents=True, exist_ok=True)
            with patch.object(Orchestrator, "_usb_fingerprint", staticmethod(
                    lambda: (1000.0, 2))):
                two_devices = app._input_mtimes()
            with patch.object(Orchestrator, "_usb_fingerprint", staticmethod(
                    lambda: (1001.0, 3))):
                three_devices = app._input_mtimes()
            self.assertNotEqual(two_devices, three_devices,
                                "a newly plugged modem must end the backoff")
            # A platform without a USB tree still returns a stable shape.
            self.assertEqual(len(app._input_mtimes()), 7)


class PastedNodeFidelityTests(unittest.TestCase):
    """A link this gateway reads differently from every other client is a silent outage.

    Issue #27: nodes that v2rayN connected to failed here with a generic message, because
    the parameters that make them work were dropped on the way in.
    """

    def test_hysteria2_link_keeps_its_obfuscation(self):
        node = parse_share_link(
            "hysteria2://letmein@hk.example.net:8443/?obfs=salamander"
            "&obfs-password=obfs-pw&sni=hk.example.net#HK")
        outbound = clash_outbound(node, "exit-hk")
        # Without this the server discards every packet and the node looks simply dead.
        self.assertEqual(outbound["obfs"], {"type": "salamander", "password": "obfs-pw"})

    def test_hysteria2_link_keeps_a_colon_inside_its_auth_string(self):
        node = parse_share_link("hysteria2://user:secret@hk.example.net:8443#HK")
        self.assertEqual(node["password"], "user:secret")

    def test_alpn_is_read_for_protocols_other_than_vless(self):
        node = parse_share_link("hysteria2://pw@hk.example.net:8443/?alpn=h3&sni=hk.example.net")
        self.assertEqual(clash_outbound(node, "exit-hk")["tls"]["alpn"], ["h3"])

    def test_an_unsupported_transport_is_refused_instead_of_silently_downgraded(self):
        # It used to yield a plain-TCP outbound that passed every check and never connected.
        for link in (
            "vless://uuid-1@h.example.net:443?security=tls&type=grpc&serviceName=g",
            "vless://uuid-1@h.example.net:443?security=tls&type=httpupgrade&path=%2Fu",
        ):
            with self.assertRaises(ValueError) as caught:
                parse_share_link(link)
            self.assertIn("not supported", str(caught.exception))

    def test_supported_transports_still_parse(self):
        self.assertEqual(parse_share_link(
            "vless://uuid-1@h.example.net:443?security=tls&type=ws&path=%2Fws")["network"], "ws")
        self.assertEqual(parse_share_link(
            "trojan://pw@h.example.net:443?sni=h.example.net")["network"], "tcp")


class ProxyProfileDescriptionTests(unittest.TestCase):
    """The parsed view is what answers "it works in my other client"."""

    def test_it_reports_the_switches_that_decide_whether_a_node_carries_ike(self):
        parsed = egress.describe_proxy_profile({
            "type": "node",
            "value": "hysteria2://pw@hk.example.net:8443/?obfs=salamander"
                     "&obfs-password=o&sni=hk.example.net&insecure=1"})
        self.assertEqual(parsed["protocol"], "hysteria2")
        self.assertEqual(parsed["obfs"], "salamander")
        self.assertTrue(parsed["obfs_password_set"])
        self.assertTrue(parsed["udp_capable"])
        self.assertEqual(parsed["sni"], "hk.example.net")

    def test_it_never_carries_a_secret_or_an_address(self):
        parsed = egress.describe_proxy_profile({
            "type": "node",
            "value": "vless://uuid-secret@hk.example.net:443?security=reality"
                     "&type=tcp&sni=www.microsoft.com&pbk=public-key&sid=abcd"})
        blob = json.dumps(parsed)
        for secret in ("uuid-secret", "hk.example.net", "public-key", "abcd", "443"):
            self.assertNotIn(secret, blob)
        self.assertTrue(parsed["reality"])

    def test_a_link_it_cannot_read_reports_the_reason(self):
        parsed = egress.describe_proxy_profile({
            "type": "node",
            "value": "vless://uuid-1@h.example.net:443?security=tls&type=grpc"})
        self.assertIn("not supported", parsed["error"])


class ExitReadinessHonestyTests(unittest.TestCase):
    def test_a_singbox_that_refused_to_start_leaves_no_exit_marked_ready(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp), Path.cwd(), dry_run=True)
            desired = {"proxy": {"enabled": True,
                                 "profiles": {"n": {"name": "HK", "type": "node",
                                                    "value": "trojan://pw@hk.example.net:443"}},
                                 "exits": {"hk": {"enabled": True, "profile_id": "n"}}},
                       "lines": [{"id": "1", "country": "hk", "epdg": "e.example.net"}]}
            with patch.object(Orchestrator, "apply_singbox",
                              side_effect=RuntimeError("sing-box exited during startup")):
                app.reconcile_proxy(desired)
            published = json.loads((app.root / "proxy-status.json").read_text())
        exit_state = published["exits"]["hk"]
        # Publishing ready here sent the UDP test at a socket nobody was serving.
        self.assertFalse(exit_state["ready"])
        self.assertIn("sing-box exited", exit_state["error"])


class RealityRunsOnXrayTests(unittest.TestCase):
    """REALITY is an Xray protocol, so Xray is what must speak it.

    Issue #27: a server on a newer Xray build answered Xray clients and failed sing-box with
    "reality verification failed". Routing REALITY through the Xray already installed for
    XHTTP removes that version skew instead of chasing it.
    """

    REALITY_LINK = ("vless://uuid-1@r.example.net:443?security=reality&type=tcp"
                    "&sni=www.microsoft.com&fp=chrome&pbk=public-key&sid=abcd"
                    "&spx=%2F&flow=xtls-rprx-vision")

    def test_reality_and_xhttp_pick_xray_while_plain_tls_stays_on_singbox(self):
        self.assertTrue(node_needs_xray(parse_share_link(self.REALITY_LINK)))
        self.assertTrue(node_needs_xray(parse_share_link(
            "vless://uuid-2@x.example.net:443?security=reality&type=xhttp"
            "&sni=www.microsoft.com&pbk=public-key&sid=abcd")))
        # A node sing-box handles correctly must not be moved off it.
        self.assertFalse(node_needs_xray(parse_share_link(
            "vless://uuid-3@t.example.net:443?security=tls&type=ws&path=%2Fws")))
        self.assertFalse(node_needs_xray(parse_share_link(
            "trojan://pw@t.example.net:443?sni=t.example.net")))

    def test_vision_reality_over_raw_tcp_converts_for_xray(self):
        outbound = xray_outbound(parse_share_link(self.REALITY_LINK), "out-one")
        stream = outbound["streamSettings"]
        # Xray calls the plain TCP transport "raw".
        self.assertEqual(stream["network"], "raw")
        self.assertEqual(stream["security"], "reality")
        self.assertEqual(stream["realitySettings"]["publicKey"], "public-key")
        self.assertEqual(stream["realitySettings"]["shortId"], "abcd")
        self.assertEqual(stream["realitySettings"]["serverName"], "www.microsoft.com")
        self.assertEqual(stream["realitySettings"]["spiderX"], "/")
        self.assertEqual(outbound["settings"]["vnext"][0]["users"][0]["flow"],
                         "xtls-rprx-vision")

    def test_a_reality_exit_is_built_as_a_loopback_bridge_to_xray(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp), Path.cwd(), dry_run=True)
            config, states = app.build_proxy_config({
                "profiles": {"node-one": {"name": "UK Reality", "type": "node",
                                          "value": self.REALITY_LINK}},
                "exits": {"gb": {"enabled": True, "profile_id": "node-one"}},
            })
        self.assertTrue(states["gb"]["ready"])
        exit_outbound = next(x for x in config["outbounds"] if x.get("tag") == "exit-gb")
        # sing-box only carries it to the local Xray; the node itself is Xray's business.
        self.assertEqual(exit_outbound["type"], "socks")
        self.assertEqual(exit_outbound["server"], "127.0.0.1")
        self.assertTrue(app.next_xray_config)
        xray_out = app.next_xray_config["outbounds"][0]
        self.assertEqual(xray_out["protocol"], "vless")
        self.assertEqual(xray_out["streamSettings"]["security"], "reality")

    def test_the_node_test_reports_which_engine_carries_it(self):
        parsed = egress.describe_proxy_profile({"type": "node", "value": self.REALITY_LINK})
        self.assertEqual(parsed["engine"], "xray")
        self.assertTrue(parsed["reality"])
        self.assertEqual(egress.describe_proxy_profile(
            {"type": "node", "value": "trojan://pw@t.example.net:443"})["engine"], "sing-box")


class UdpProbeTargetTests(unittest.TestCase):
    """One hard-coded resolver made the probe a test of that address, not of UDP."""

    def test_defaults_are_three_distinct_public_resolvers(self):
        self.assertEqual(egress.udp_probe_targets(), ["1.1.1.1", "8.8.8.8", "9.9.9.9"])

    def test_the_list_is_configurable_and_rejects_junk(self):
        with patch.dict(os.environ, {"MDD_UDP_PROBE_TARGETS": "8.8.4.4, not-an-ip ,8.8.4.4,9.9.9.9"}):
            self.assertEqual(egress.udp_probe_targets(), ["8.8.4.4", "9.9.9.9"])
        with patch.dict(os.environ, {"MDD_UDP_PROBE_TARGETS": "garbage"}):
            self.assertEqual(egress.udp_probe_targets(), ["1.1.1.1"])

    def test_a_blackholed_first_resolver_falls_back_instead_of_failing(self):
        calls = []

        def probe(host, port, target, timeout, username="", password=""):
            calls.append(target)
            if target != ("dns", "9.9.9.9", 53):
                raise egress.EgressError("timed out")
            return 42

        with patch.object(egress, "_udp_probe_once", side_effect=probe):
            self.assertEqual(egress.test_udp_proxy("127.0.0.1", 1080), 42)
        self.assertEqual(calls[0], ("dns", "1.1.1.1", 53))
        self.assertEqual(calls[-1], ("dns", "9.9.9.9", 53))

    def test_every_probe_failing_names_each_one(self):
        with patch.object(egress, "_udp_probe_once",
                          side_effect=egress.EgressError("timed out")):
            with self.assertRaises(egress.EgressError) as caught:
                egress.test_udp_proxy("127.0.0.1", 1080)
        message = str(caught.exception)
        for name in ("1.1.1.1", "8.8.8.8", "9.9.9.9",
                     "stun.cloudflare.com", "stun.l.google.com"):
            self.assertIn(name, message)

    def test_stun_carries_the_verdict_when_port_53_is_intercepted(self):
        """The reported case: DNS is rewritten end to end, the exit itself carries UDP.

        VoWiFi runs IKE on UDP 500/4500 and never queries these resolvers, so a panel or
        provider rewriting port 53 must not be able to condemn the exit.
        """
        def probe(host, port, target, timeout, username="", password=""):
            if target[0] == "dns":
                raise egress.EgressError("timed out")
            return 17

        with patch.object(egress, "_udp_probe_once", side_effect=probe):
            self.assertEqual(egress.test_udp_proxy("127.0.0.1", 1080), 17)

    def test_the_plan_interleaves_so_neither_family_decides_alone(self):
        kinds = [probe[0] for probe in egress.udp_probe_plan()]
        self.assertEqual(kinds[:4], ["dns", "stun", "dns", "stun"])
        # A blocked resolver must not push every STUN probe past the time budget.
        self.assertIn("stun", kinds[:2])

    def test_stun_targets_are_configurable_and_reject_junk(self):
        with patch.dict(os.environ, {"MDD_UDP_STUN_TARGETS":
                                     "a.example:3478, nonsense ,b.example:0,c.example:19302"}):
            self.assertEqual(egress.stun_probe_targets(),
                             [("a.example", 3478), ("c.example", 19302)])


class VlessEncryptionTests(unittest.TestCase):
    """Xray 26.7+ VLESS Encryption: the client must echo the server's declared value.

    Issue #27's actual cause. The parameter was dropped and "none" hard-coded, so the
    client connected, could not be understood, and every probe timed out with the server
    logging nothing — indistinguishable from a dead node.
    """

    ENC = "mlkem768x25519plus.native.0rtt.6wD_327L54KNiTgD9LOucKtF6abQ3_-U9qWY7mXTjlk"

    def link(self, encryption=None):
        extra = f"&encryption={encryption}" if encryption is not None else ""
        return ("vless://uuid-1@r.example.net:443?security=reality&type=tcp"
                "&sni=www.apple.com&pbk=public-key&sid=abcd&flow=xtls-rprx-vision" + extra)

    def test_the_encryption_value_reaches_the_xray_outbound(self):
        node = parse_share_link(self.link(self.ENC))
        self.assertEqual(node["encryption"], self.ENC)
        user = xray_outbound(node, "out")["settings"]["vnext"][0]["users"][0]
        self.assertEqual(user["encryption"], self.ENC)

    def test_an_encrypted_node_is_routed_to_xray(self):
        # Even without REALITY: only Xray implements this.
        plain = ("vless://uuid-1@r.example.net:443?security=tls&type=tcp"
                 f"&sni=x.example.net&encryption={self.ENC}")
        self.assertTrue(node_needs_xray(parse_share_link(plain)))

    def test_sing_box_refuses_it_by_name_instead_of_timing_out(self):
        node = parse_share_link(self.link(self.ENC))
        with self.assertRaises(ValueError) as caught:
            clash_outbound(node, "exit-gb")
        self.assertIn("VLESS Encryption", str(caught.exception))

    def test_ordinary_nodes_still_say_none(self):
        for link in (self.link(), self.link("none")):
            node = parse_share_link(link)
            self.assertEqual(node["encryption"], "none")
            user = xray_outbound(node, "out")["settings"]["vnext"][0]["users"][0]
            self.assertEqual(user["encryption"], "none")
            # And they remain describable through the sing-box converter.
            self.assertEqual(clash_outbound(node, "exit-gb")["type"], "vless")

    def test_the_summary_names_the_family_without_the_key_material(self):
        parsed = egress.describe_proxy_profile({"type": "node", "value": self.link(self.ENC)})
        self.assertEqual(parsed["encryption"], "mlkem768x25519plus")
        self.assertNotIn(self.ENC, json.dumps(parsed))
        self.assertEqual(parsed["engine"], "xray")


class TestProxyDiagnosticsTests(unittest.TestCase):
    def test_both_engine_streams_are_quoted(self):
        """Xray logs to stdout and sing-box to stderr; reading one left the other silent."""
        process = SimpleNamespace(
            stdout=io.StringIO("xray: proxy/vless/outbound: connection ends\n"),
            stderr=io.StringIO("sing-box: outbound/socks: timeout\n"))
        detail = egress._process_detail(process)
        self.assertIn("xray", detail)
        self.assertIn("sing-box", detail)

    def test_a_process_without_pipes_is_not_an_error(self):
        self.assertEqual(egress._process_detail(None), "")
        self.assertEqual(
            egress._process_detail(SimpleNamespace(stdout=None, stderr=None)), "")


class XrayFailureBlastRadiusTests(unittest.TestCase):
    """REALITY made Xray load-bearing; its failure must not take unrelated exits down."""

    REALITY = ("vless://uuid-1@r.example.net:443?security=reality&type=tcp"
               "&sni=www.apple.com&pbk=public-key&sid=abcd&flow=xtls-rprx-vision")
    PLAIN = "trojan://pw@t.example.net:443?sni=t.example.net"

    def _reconcile(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp), Path.cwd(), dry_run=True)
            desired = {"proxy": {"enabled": True, "profiles": {
                            "r": {"name": "R", "type": "node", "value": self.REALITY},
                            "p": {"name": "P", "type": "node", "value": self.PLAIN}},
                        "exits": {"gb": {"enabled": True, "profile_id": "r"},
                                  "us": {"enabled": True, "profile_id": "p"}}},
                       "lines": []}
            with patch.object(Orchestrator, "apply_xray",
                              side_effect=RuntimeError("Xray-core executable not found")), \
                    patch.object(Orchestrator, "apply_routes"):
                app.reconcile_proxy(desired)
            return json.loads((app.root / "proxy-status.json").read_text())

    def test_only_the_xray_backed_country_fails(self):
        exits = self._reconcile()["exits"]
        self.assertFalse(exits["gb"]["ready"])
        self.assertIn("Xray is unavailable", exits["gb"]["error"])
        # The trojan exit never touches Xray and must still be routed.
        self.assertTrue(exits["us"]["ready"], "an unrelated exit was taken down with Xray")
