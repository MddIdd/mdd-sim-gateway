"""The control plane's part in the media modes: moving running lines and telling clients."""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from control.app import main, media

INSTANCE = {"id": "1", "mcc": "001", "mnc": "01",
            "sip": {"webrtc": {"enable": True, "username": "webrtc", "password": "p"}}}


def _request(host="gw.example:10443"):
    return SimpleNamespace(headers={"host": host},
                           url=SimpleNamespace(hostname=host.rsplit(":", 1)[0]))


class MediaControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(media.cfg, "DATA_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def converge(self, modes):
        lines = [{"id": iid} for iid in modes]
        started = []
        with patch.object(main.cfg, "list_instances", return_value=lines), \
                patch.object(main.cfg, "get_instance", side_effect=lambda iid: {"id": iid}), \
                patch.object(main.cfg, "get_settings", return_value={}), \
                patch.object(main.engine, "media_mode_of", side_effect=modes.get), \
                patch.object(main, "_start_engine_checked",
                             side_effect=lambda inst, *a: started.append((inst["id"], a))), \
                patch.object(main, "_record_lifecycle"), \
                patch.object(main.hub, "drop_ami", AsyncMock()), \
                patch.object(main.hub, "reset_health"):
            rebuilt = asyncio.run(main._media_converge_once())
        return rebuilt, started

    def test_lines_already_in_the_recorded_mode_are_left_alone(self):
        rebuilt, started = self.converge({"1": "direct", "2": None})
        self.assertFalse(rebuilt)
        self.assertEqual(started, [])

    def test_one_line_at_a_time_moves_to_the_recorded_mode(self):
        media.save_state({"mode": "relay", "secret": "x"})
        rebuilt, started = self.converge({"1": "relay", "2": "direct", "3": "direct"})
        self.assertTrue(rebuilt)
        self.assertEqual([iid for iid, _ in started], ["2"])
        self.assertEqual(started[0][1][-1], "media_mode")

    def test_stopped_lines_are_not_started_by_a_mode_change(self):
        media.save_state({"mode": "relay", "secret": "x"})
        rebuilt, started = self.converge({"1": None})
        self.assertFalse(rebuilt)

    def test_direct_mode_provisioning_carries_no_ice_servers(self):
        with patch.object(main.cfg, "get_instance", return_value=INSTANCE):
            prov = main.api_softphone("1", _request())
        self.assertEqual(prov["media_mode"], "direct")
        self.assertEqual(prov["ice_servers"], [])
        self.assertEqual(prov["ice_transport_policy"], "all")

    def test_relay_provisioning_is_ready_only_when_relay_and_line_both_are(self):
        media.save_state({"mode": "relay", "port": 8478, "secret": "x"})
        for relay_state, line_state, ready in (("ready", "ready", True),
                                               ("ready", "firewall_failed", False),
                                               ("unavailable", "ready", False)):
            with patch.object(main.cfg, "get_instance", return_value=INSTANCE), \
                    patch.object(media, "relay_status", return_value={"state": relay_state}), \
                    patch.object(main, "_line_media_state", return_value=line_state):
                prov = main.api_softphone("1", _request())
            self.assertEqual(prov["relay_ready"], ready, (relay_state, line_state))
            self.assertEqual(prov["ice_transport_policy"], "relay")
            self.assertEqual(prov["ice_servers"][0]["urls"][0],
                             "turn:gw.example:8478?transport=udp")


if __name__ == "__main__":
    unittest.main()
