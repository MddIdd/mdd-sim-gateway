"""Closing a country exit's dead sessions, without ever moving a node.

sing-box retires a UDP session on an IDLE timer. A line rebuilding its tunnel retransmits IKE
every few seconds, and each retransmit refreshes that timer — so a session whose outbound has
already died is held open by the very retries meant to recover it, and the line feeds every
later packet to a connection that can never answer. Nothing times out, sing-box logs nothing
(no NEW session is created), and the line never recovers on its own.

Closing the session is the remedy. These pin down that it is only ever done with evidence, and
that it stays inside the failover principles: no node change, no pin overridden, and never
while a sibling line is registered over the same exit.
"""
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from control.app import egress, failover

ROOT = Path(__file__).resolve().parents[1]


class ReportWritingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "orchestrator").mkdir()
        patcher = patch.multiple(egress, _ORCH_DIR=str(root / "orchestrator"),
                                 _STALLED=str(root / "orchestrator" / "exit-stalled.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = root / "orchestrator" / "exit-stalled.json"

    def _read(self):
        return json.loads(self.path.read_text())

    def test_a_report_names_the_country_node_and_line(self):
        self.assertTrue(egress.report_stalled_exit("gb", "WG-UK-01", "health-freeze:x", "7"))
        entry = self._read()["countries"]["gb"]
        self.assertEqual(entry["node"], "WG-UK-01")
        self.assertEqual(entry["line"], "7")
        self.assertEqual(entry["reason"], "health-freeze:x")
        self.assertGreater(entry["ts"], 0)

    def test_a_later_report_supersedes_the_earlier_one(self):
        egress.report_stalled_exit("gb", "WG-UK-01", "first", "7")
        first = self._read()["countries"]["gb"]["ts"]
        egress.report_stalled_exit("gb", "WG-UK-01", "second", "5")
        entry = self._read()["countries"]["gb"]
        self.assertGreaterEqual(entry["ts"], first)
        self.assertEqual(entry["reason"], "second")

    def test_countries_do_not_overwrite_each_other(self):
        egress.report_stalled_exit("gb", "WG-UK-01", "x", "7")
        egress.report_stalled_exit("us", "NS-US-02", "y", "1")
        self.assertEqual(set(self._read()["countries"]), {"gb", "us"})

    def test_a_line_with_no_country_writes_nothing(self):
        self.assertFalse(egress.report_stalled_exit("", "WG-UK-01", "x", "7"))
        self.assertFalse(self.path.exists())


def _load_orchestrator():
    """Import the host orchestrator with the heavy optional deps stubbed out."""
    stubs = {}
    for name in ("yaml", "serial", "requests"):
        if name not in sys.modules:
            module = types.ModuleType(name)
            if name == "yaml":
                module.safe_load = lambda *_a, **_k: {}
                module.safe_dump = lambda *_a, **_k: ""
            stubs[name] = module
    spec = importlib.util.spec_from_file_location(
        "mdd_orchestrator_stalled", ROOT / "host" / "mdd_orchestrator.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


class DropConnectionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.orch = _load_orchestrator()

    def _agent(self):
        agent = MagicMock()
        agent.drop_exit_connections = self.orch.Orchestrator.drop_exit_connections.__get__(agent)
        return agent

    def _urlopen(self, connections, deleted):
        def fake(request, timeout=None):
            url = request if isinstance(request, str) else request.full_url
            handle = MagicMock()
            handle.__enter__ = lambda _s: handle
            handle.__exit__ = lambda *_a: False
            if url.endswith("/connections"):
                handle.read.return_value = json.dumps({"connections": connections}).encode()
                return handle
            deleted.append(url.rsplit("/", 1)[-1])
            return handle
        return fake

    def _run(self, connections):
        deleted = []
        with patch.object(self.orch.urllib.request, "urlopen",
                          side_effect=self._urlopen(connections, deleted)), \
                patch.object(self.orch.json, "load",
                             side_effect=lambda h: json.loads(h.read().decode())):
            count = self._agent().drop_exit_connections("gb")
        return count, deleted

    def test_closes_the_countrys_selector_and_member_connections(self):
        count, deleted = self._run([
            {"id": "a", "chains": ["exit-gb-0", "exit-gb"]},
            {"id": "b", "chains": ["exit-gb"]},
        ])
        self.assertEqual(count, 2)
        self.assertEqual(sorted(deleted), ["a", "b"])

    def test_leaves_other_countries_alone(self):
        count, deleted = self._run([
            {"id": "gb", "chains": ["exit-gb-0", "exit-gb"]},
            {"id": "us", "chains": ["exit-us-7", "exit-us"]},
        ])
        self.assertEqual(count, 1)
        self.assertEqual(deleted, ["gb"])

    def test_a_lookalike_tag_is_not_matched(self):
        """"exit-gbr" must not be swept up by a request for "gb"."""
        count, deleted = self._run([{"id": "x", "chains": ["exit-gbr-1", "exit-gbr"]}])
        self.assertEqual((count, deleted), (0, []))

    def test_a_connection_without_an_id_is_skipped(self):
        count, deleted = self._run([{"chains": ["exit-gb-0"]}])
        self.assertEqual((count, deleted), (0, []))

    def test_an_unreachable_api_drops_nothing_and_does_not_raise(self):
        with patch.object(self.orch.urllib.request, "urlopen", side_effect=OSError("refused")):
            self.assertEqual(self._agent().drop_exit_connections("gb"), 0)


class PrincipleTests(unittest.TestCase):
    """The report must stay inside the rules that govern every exit change."""

    def test_the_verdict_that_triggers_it_is_the_one_that_blames_the_exit(self):
        self.assertEqual(failover.classify("CONNECTING", 0, reason_code="tunnel_network"),
                         failover.BLAMES_EXIT)
        # A tunnel that came up and carried traffic is not the exit's fault, so no report.
        self.assertEqual(failover.classify("CONNECTED", 0), failover.BLAMES_ELSEWHERE)

    def test_a_long_healthy_stretch_never_blames_the_exit(self):
        self.assertEqual(
            failover.classify("CONNECTING", 0, stable_seconds=600, stable_threshold=300),
            failover.BLAMES_ELSEWHERE)

    def test_the_call_site_is_guarded_by_the_peer_shield_and_the_verdict(self):
        source = (ROOT / "control" / "app" / "main.py").read_text()
        body = source[source.index("def _judge_exit_failure("):]
        body = body[:body.index("\ndef ")]
        guard = "elif verdict == failover.BLAMES_EXIT and not peer_registered and country:"
        self.assertIn(guard, body,
                      "the report must require an exit verdict and no registered sibling")
        # It must be the alternative to SWITCH, never something that also runs alongside it.
        self.assertLess(body.index("if action == failover.SWITCH:"), body.index(guard))
        self.assertIn("egress.report_stalled_exit(", body)

    def test_reporting_does_not_swallow_the_operator_notification(self):
        """The two must be independent branches.

        Chaining the report onto the same if/elif as the notification meant a backed-off line
        (exit blamed, pool exhausted, no peer) quietly stopped telling anyone it was stuck —
        it took the report branch and never reached the notify one.
        """
        source = (ROOT / "control" / "app" / "main.py").read_text()
        body = source[source.index("def _judge_exit_failure("):]
        body = body[:body.index("\ndef ")]
        notify = "    if action in (failover.GIVE_UP, failover.REPORT) or ("
        self.assertIn(notify, body,
                      "the notification must be its own `if`, not an `elif` after the report")
        self.assertNotIn("    elif action in (failover.GIVE_UP, failover.REPORT)", body)

    def test_the_cleanup_changes_no_node(self):
        source = (ROOT / "host" / "mdd_orchestrator.py").read_text()
        body = source[source.index("def drop_exit_connections("):]
        body = body[:body.index("\n    def ", 10)]
        for forbidden in ("select_member", "rank_and_select", "record_exit_node"):
            self.assertNotIn(forbidden, body,
                             f"the cleanup must not touch node selection ({forbidden})")


if __name__ == "__main__":
    unittest.main()
