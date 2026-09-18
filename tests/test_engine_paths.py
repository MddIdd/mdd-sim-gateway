import importlib
import json
import sys
import tempfile
import unittest
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


class EnginePathTests(unittest.TestCase):
    @staticmethod
    def engine_module():
        fake_docker = SimpleNamespace(
            from_env=lambda: None,
            errors=SimpleNamespace(NotFound=type("NotFound", (Exception,), {})),
        )
        with patch.dict(sys.modules, {"docker": fake_docker}):
            sys.modules.pop("control.app.engine", None)
            return importlib.import_module("control.app.engine")

    def test_normal_docker_calls_reuse_one_client(self):
        engine = self.engine_module()
        client = SimpleNamespace(close=lambda: None)
        with patch.object(engine.docker, "from_env", return_value=client) as factory:
            self.assertIs(engine._client(), client)
            self.assertIs(engine._client(), client)
            factory.assert_called_once_with(timeout=30)
            engine.close_client()

    def start_engine(self, inst, settings):
        """Run engine.start against a fake Docker; return what it created and how it wired it."""
        engine = self.engine_module()
        record = {"order": []}

        class _Container:
            id = "container-id"

            def __init__(self, name):
                self.name = name

            def start(self):
                record["order"].append("start")

        class _Containers:
            def get(self, name):
                raise engine.docker.errors.NotFound(name)

            def create(self, image, **kwargs):
                record["create"] = kwargs
                record["order"].append("create")
                return _Container(kwargs["name"])

        class _Media:
            def connect(self, container, ipv4_address=None):
                record["media"] = ipv4_address
                record["order"].append("connect")

        api = SimpleNamespace(create_endpoint_config=lambda **kw: {"IPAMConfig": kw})
        client = SimpleNamespace(containers=_Containers(), api=api)
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(engine, "_client", lambda: client), \
                patch.object(engine, "_instance_paths", lambda iid: (temp, temp)), \
                patch.object(engine, "_clear_runtime_state", lambda base: None), \
                patch.object(engine.egress, "ensure_line", lambda i, s: None), \
                patch.object(engine.cfg, "write_instance_json", lambda i, s: None), \
                patch.object(engine.turn, "ensure_networks", lambda c: (None, _Media())), \
                patch.object(engine.turn, "ensure", lambda c: None):
            engine.start(inst, settings)
        return engine, record

    def test_ami_debug_port_is_published_on_loopback_only(self):
        """The optional AMI diagnostic port must never reach the LAN."""
        inst = {"id": "sim1", "index": 0, "ports": {"sip_udp": 5060, "sip_tls": 5061,
                                                    "ami": 5038, "rtp_start": 10000}}
        _, record = self.start_engine(inst, {"debug": {"ami": True}})
        created = record["create"]
        self.assertEqual(created["ports"], {"5038/tcp": ("127.0.0.1", 5038)})
        self.assertEqual(created["volumes"]["/etc/localtime"],
                         {"bind": "/etc/localtime", "mode": "ro"})

    def test_engine_publishes_nothing_and_joins_both_networks_before_starting(self):
        inst = {"id": "sim1", "index": 2, "ports": {"sip_udp": 5080, "sip_tls": 5081,
                "ami": 5058, "rtp_start": 14000, "rtp_span": 12}}
        engine, record = self.start_engine(inst, {})
        created = record["create"]
        # Signalling goes through the control surface relay and media through the TURN
        # relay, so not even the RTP range is published any more.
        self.assertEqual(created["ports"], {})
        self.assertEqual(created["network"], engine.turn.ENGINE_NETWORK)
        self.assertEqual(created["networking_config"][engine.turn.ENGINE_NETWORK],
                         {"IPAMConfig": {"ipv4_address": "172.29.0.12"}})
        self.assertEqual(record["media"], "172.29.1.12")
        self.assertEqual(created["environment"]["MDD_MEDIA_ADDR"], "172.29.1.12")
        # eth1 must exist before the entrypoint renders the config that binds to it.
        self.assertEqual(record["order"], ["create", "connect", "start"])
        self.assertFalse(any(v.get("bind", "").startswith("/etc/asterisk/certificate")
                             for v in created["volumes"].values()))

    def test_control_surface_uses_the_engine_network_address(self):
        engine = self.engine_module()
        networks = {engine.turn.MEDIA_NETWORK: {"IPAddress": "172.29.1.10"},
                    engine.turn.ENGINE_NETWORK: {"IPAddress": "172.29.0.10"}}
        container = SimpleNamespace(status="running", id="c",
                                    attrs={"NetworkSettings": {"Networks": networks}})
        client = SimpleNamespace(containers=SimpleNamespace(get=lambda name: container))
        with patch.object(engine, "_client", lambda: client):
            self.assertEqual(engine.container_ip("sim1"), "172.29.0.10")
            del networks[engine.turn.ENGINE_NETWORK]
            # An engine not recreated yet (still on the default bridge) stays reachable, but
            # never through the media network.
            networks["bridge"] = {"IPAddress": "172.17.0.3"}
            self.assertEqual(engine.container_ip("sim1"), "172.17.0.3")

    def test_engine_recreation_clears_stale_runtime_observations(self):
        engine = self.engine_module()
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"
            run.mkdir()
            stale = ["swu_status.json", "pcscf", "pin_status.json", "usim_status.json"]
            for name in stale:
                (run / name).write_text("old")
            (run / "charon.log").write_text("keep diagnostics")

            engine._clear_runtime_state(temp)

            self.assertTrue(all(not (run / name).exists() for name in stale))
            self.assertEqual((run / "charon.log").read_text(), "keep diagnostics")

    def test_docker_log_event_time_is_rendered_in_local_ike_format(self):
        engine = self.engine_module()
        raw = ("2026-08-07T03:17:18.123456789Z [Aug  7 11:17:18] "
               "REGISTER sip:ims.example SIP/2.0\n"
               "2026-08-07T03:17:19Z Via: SIP/2.0/TCP example\n")
        rendered = engine._format_docker_logs(raw, timezone(timedelta(hours=8)))
        self.assertEqual(rendered,
                         "[2026-08-07 11:17:18+0800] REGISTER sip:ims.example SIP/2.0\n"
                         "[2026-08-07 11:17:19+0800] Via: SIP/2.0/TCP example\n")

    def test_engine_log_read_requests_docker_source_timestamps(self):
        engine = self.engine_module()
        captured = {}

        class _Container:
            def logs(self, **kwargs):
                captured.update(kwargs)
                return b"2026-08-07T03:17:18Z Asterisk Ready.\n"

        client = SimpleNamespace(containers=SimpleNamespace(
            get=lambda name: _Container()))
        with patch.object(engine, "_client", lambda: client):
            rendered = engine.logs("1", 25, since=123)

        self.assertEqual(captured, {"tail": 25, "timestamps": True, "since": 123})
        self.assertRegex(rendered,
                         r"^\[2026-08-07 \d{2}:17:18[+-]\d{4}\] Asterisk Ready\.\n$")


class DiagnosticCaptureTests(unittest.TestCase):
    """A line stuck rebuilding destroys its own evidence every couple of minutes."""

    @staticmethod
    def _instance_dir(temp):
        base = Path(temp)
        (base / "run").mkdir()
        (base / "logs").mkdir()
        return base

    def test_snapshot_records_registration_tunnel_and_exit_evidence(self):
        engine = EnginePathTests.engine_module()
        with tempfile.TemporaryDirectory() as temp:
            base = self._instance_dir(temp)
            (base / "run" / "charon.log").write_text(
                "sending IKE_SA_INIT\n"
                "[swu_ike] IKE request retransmit 2/3 (message_id=0, same bytes)\n"
                "[swu_ike] IKE request retransmit 3/3 (message_id=0, same bytes)\n"
                "TIMEOUT : TIMEOUT\n"
                "[2026-08-07 11:17:18+0800] STATE 2:\n")
            engine_log = ("Asterisk ready, triggering registration...\n"
                          "unrelated module chatter\n"
                          "res_pjsip: SIP/2.0 403 Forbidden\n"
                          "res_pjsip_outbound_registration.c: Fatal response '403' received "
                          "from 'sip:pcscf.example' on registration attempt\n")
            with patch.object(engine, "DATA_DIR", temp), \
                    patch.object(engine, "registration_state", lambda iid: "Unregistered"), \
                    patch.object(engine, "read_run_json", lambda iid, name: {"state": "CONNECTED"}), \
                    patch.object(engine, "read_pcscf", lambda iid: "fd00::5"), \
                    patch.object(engine, "logs", lambda iid, tail: engine_log), \
                    patch.object(engine.egress, "line_country", lambda inst: "us"), \
                    patch.object(engine.egress, "status", lambda: {"exits": {"us": {
                        "node": "US Bravo", "selection": "manual", "candidate_count": 11,
                        "ready": True}}}):
                engine.capture_diagnostics("1", {"mcc": "310"}, str(base), "auto-recover:ims")

            records = [json.loads(line) for line
                       in (base / "logs" / "diagnostics.jsonl").read_text().splitlines() if line]
            self.assertEqual(len(records), 1)
            record = records[0]
            self.assertEqual(record["reason"], "auto-recover:ims")
            self.assertEqual(record["registration"], "Unregistered")
            # Lossy-exit signature: what distinguishes a bad node from a carrier rejection.
            self.assertEqual(record["charon"]["retransmits"], 2)
            self.assertEqual(record["charon"]["timeouts"], 1)
            self.assertEqual(record["charon"]["last_state"],
                             "[2026-08-07 11:17:18+0800] STATE 2:")
            self.assertEqual(record["egress"]["node"], "US Bravo")
            self.assertIn("SIP/2.0 403 Forbidden", "\n".join(record["sip"]))
            self.assertNotIn("unrelated module chatter", "\n".join(record["sip"]))
            # Issue #33: the bundle's SIP lines had rotated away, so the snapshot must keep
            # the classified verdict with its numeric code, not only the raw log lines.
            self.assertEqual(record["registration_evidence"],
                             {"kind": "rejected", "sip_status": 403})

    def test_snapshots_are_bounded(self):
        engine = EnginePathTests.engine_module()
        with tempfile.TemporaryDirectory() as temp:
            base = self._instance_dir(temp)
            with patch.object(engine, "DIAGNOSTIC_RECORDS", 3):
                for index in range(6):
                    engine._append_diagnostic(str(base), {"index": index})
            records = [json.loads(line) for line
                       in (base / "logs" / "diagnostics.jsonl").read_text().splitlines() if line]
            self.assertEqual([x["index"] for x in records], [3, 4, 5])

    def test_sip_evidence_keeps_protocol_lines_and_drops_debug_chatter(self):
        """Built from a real capture: the debug stream names the registration module on
        almost every line, which crowded the actual failure out of the bounded tail."""
        engine = EnginePathTests.engine_module()
        raw = "\n".join([
            "\x1b[1;30m    -- \x1b[0mRemote UNIX connection",
            "[Aug  4 09:19:06] \x1b[1;32mDEBUG\x1b[0m[1562]: "
            "\x1b[1;37mres_pjsip_outbound_registration.c\x1b[0m:1241 handle_client_registration",
            "[Aug  4 09:19:06] DEBUG[1562]: res_pjsip/pjsip_resolver.c:495 "
            "sip_resolve: Transport 'volte_ims' ...",
            "[Aug  4 09:16:28] WARNING[133]: res_pjsip_outbound_registration.c:1522 "
            "registration_transport_shutdown_cb: PJSIP transport 'volte_ims' failed.",
            "[2026-08-07 11:17:18+0800] REGISTER "
            "sip:ims.mnc240.mcc310.3gppnetwork.org SIP/2.0",
            "[Aug  4 09:18:36] WARNING[1562]: res_pjsip_outbound_registration.c:1456 "
            "schedule_retry: No response received from 'sip:ims.mnc240.mcc310...'",
            "\x1b[1;32mSIP/2.0 401 Unauthorized\x1b[0m",
            "Status: Rejected",
        ])
        kept = engine._sip_evidence(raw)

        self.assertNotIn("Remote UNIX connection", "\n".join(kept))
        # Module names in DEBUG lines must not qualify as evidence on their own.
        self.assertFalse(any("handle_client_registration" in line for line in kept))
        self.assertFalse(any("sip_resolve" in line for line in kept))
        self.assertEqual(kept, [
            "[Aug  4 09:16:28] WARNING[133]: res_pjsip_outbound_registration.c:1522 "
            "registration_transport_shutdown_cb: PJSIP transport 'volte_ims' failed.",
            "[2026-08-07 11:17:18+0800] REGISTER "
            "sip:ims.mnc240.mcc310.3gppnetwork.org SIP/2.0",
            "[Aug  4 09:18:36] WARNING[1562]: res_pjsip_outbound_registration.c:1456 "
            "schedule_retry: No response received from 'sip:ims.mnc240.mcc310...'",
            # Colour escapes stripped so the stored record stays greppable.
            "SIP/2.0 401 Unauthorized",
            "Status: Rejected",
        ])

    def test_sip_evidence_is_bounded_to_the_newest_lines(self):
        engine = EnginePathTests.engine_module()
        raw = "\n".join(f"SIP/2.0 {200 + index % 100} OK" for index in range(120))
        kept = engine._sip_evidence(raw)
        self.assertEqual(len(kept), engine.SIP_EVIDENCE_LINES)
        self.assertEqual(kept[-1], "SIP/2.0 219 OK")

    def test_health_freeze_captures_before_removing_the_container(self):
        """The freeze path removes the container and only rebuilds after a cooldown, so a
        capture that runs at start() time finds nothing left to read."""
        engine = EnginePathTests.engine_module()
        order = []
        with tempfile.TemporaryDirectory() as temp:
            base = self._instance_dir(temp)
            with patch.object(engine, "_instance_paths", lambda iid: (str(base), str(base))), \
                    patch.object(engine, "capture_diagnostics",
                                 lambda iid, inst, b, reason: order.append(("capture", reason))), \
                    patch.object(engine, "stop", lambda iid: order.append(("stop", iid)) or True):
                engine.capture_and_stop("1", {"mcc": "310"}, "health-freeze:registering")
        self.assertEqual(order, [("capture", "health-freeze:registering"), ("stop", "1")])

    def test_late_capture_never_removes_a_replacement_container(self):
        engine = EnginePathTests.engine_module()
        current = SimpleNamespace(id="new", name="mdd-sim-gateway-engine-1",
                                  attrs={"Config": {"Labels": {
                                      engine.MANAGED_LABEL: "true"}}})
        current.remove = lambda force: self.fail("replacement container was removed")
        client = SimpleNamespace(containers=SimpleNamespace(get=lambda name: current))
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(engine, "_client", lambda: client), \
                patch.object(engine, "_instance_paths", lambda iid: (temp, temp)), \
                patch.object(engine, "capture_diagnostics") as capture:
            stopped = engine.capture_and_stop(
                "1", {"mcc": "310"}, "health-freeze:registering", "old")
        self.assertFalse(stopped)
        capture.assert_not_called()

    def test_capture_failure_never_blocks_the_rebuild(self):
        engine = EnginePathTests.engine_module()
        with tempfile.TemporaryDirectory() as temp:
            base = self._instance_dir(temp)
            with patch.object(engine, "registration_state",
                              side_effect=RuntimeError("docker is unreachable")):
                engine.capture_diagnostics("1", {}, str(base), "rebuild")
            self.assertFalse((base / "logs" / "diagnostics.jsonl").exists())


if __name__ == "__main__":
    unittest.main()


class RenderEnvTests(unittest.TestCase):
    """The engine reads its policy from engine.env, so a value that cannot survive the trip
    is a policy that cannot be set."""

    @staticmethod
    def _render():
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "mdd_render", root / "engine" / "render.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_zero_survives_as_zero(self):
        # rekey_minutes 0 is how a line turns proactive rekey off. Written as an empty string
        # it came back as swu_ike's 30-minute default, silently ignoring the setting.
        value = self._render().env_value(0)
        self.assertEqual(value, "0")
        self.assertEqual(float(value or 30), 0.0)

    def test_none_is_still_written_as_empty(self):
        self.assertEqual(self._render().env_value(None), "''")

    def test_values_needing_quotes_stay_one_shell_word(self):
        self.assertEqual(self._render().env_value("a b"), "'a b'")
