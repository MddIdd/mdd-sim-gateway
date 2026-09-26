"""A modem ModemManager has put in state "failed" is reported and rebooted, not retried.

Seen on a test gateway: ModemManager was restarted while the module's QMI clients were still
held, initialisation failed ("unknown-capabilities") and ModemManager never tried again. The
orchestrator kept running `mmcli --enable` every cycle, got "Wrong state" every time, and kept
the device marked transitioning, so the WebUI showed VoWiFi as starting although the line was
registered. A module reboot (AT+CFUN=1,1) cleared it.
"""
import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from control.app import device_state
from host import mdd_orchestrator
from host.mdd_orchestrator import Orchestrator

ROOT = Path(__file__).resolve().parents[1]
MODEM = {"id": "a", "tty": "/dev/ttyUSB2"}
WANTED = {"a": {"cellular_enabled": True, "flight_mode": False, "vowifi_enabled": True}}


class FailedModemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Orchestrator(Path(self.temp.name) / "data", Path(self.temp.name))
        self.app.root.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def apply(self, snapshot):
        calls = []
        stub = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(self.app, "modemmanager_modem_for_tty", return_value="/mm/0"), \
                patch.object(self.app, "modem_snapshot", return_value=dict(snapshot)), \
                patch.object(self.app, "reboot_modem") as reboot, \
                patch("host.mdd_orchestrator.serial", SimpleNamespace()), \
                patch("host.mdd_orchestrator.run",
                      side_effect=lambda args, **k: calls.append(args) or stub):
            self.app.apply_device_radios([MODEM], WANTED, through_modemmanager=True)
        return calls, reboot

    def failed(self, reason="unknown-capabilities"):
        return {"available": True, "state": "failed", "failed_reason": reason,
                "radio_enabled": False, "data_active": False}

    def test_a_failed_modem_is_not_asked_to_enable_again(self):
        calls, _reboot = self.apply(self.failed())
        self.assertFalse(any("--enable" in args for args in calls))
        state = self.app.cellular_states["a"]
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["failure"], {"reason": "unknown-capabilities",
                                            "resettable": True, "resets": 0,
                                            "exhausted": False})

    def test_the_module_is_rebooted_after_a_grace_period_spaced_out_and_bounded(self):
        _calls, reboot = self.apply(self.failed())
        reboot.assert_not_called()                      # ModemManager gets a minute first
        record = self.app._modem_failed["a"]
        reboots = 0
        for _ in range(10):
            record["since"] -= mdd_orchestrator.MM_FAILED_GRACE_SECONDS
            if record["last_reset"]:
                record["last_reset"] -= mdd_orchestrator.MM_RESET_BACKOFF_SECONDS * 8
            _calls, reboot = self.apply(self.failed())
            reboots += reboot.call_count
        self.assertEqual(reboots, mdd_orchestrator.MM_RESET_ATTEMPTS)
        self.assertTrue(self.app.cellular_states["a"]["failure"]["exhausted"])

    def test_the_next_reboot_waits_for_the_backoff(self):
        self.apply(self.failed())
        self.app._modem_failed["a"]["since"] -= mdd_orchestrator.MM_FAILED_GRACE_SECONDS
        _calls, first = self.apply(self.failed())
        _calls, second = self.apply(self.failed())
        self.assertEqual((first.call_count, second.call_count), (1, 0))

    def test_a_missing_sim_is_reported_but_never_rebooted(self):
        self.apply(self.failed("sim-missing"))
        self.app._modem_failed["a"]["since"] -= 10 * mdd_orchestrator.MM_FAILED_GRACE_SECONDS
        _calls, reboot = self.apply(self.failed("sim-missing"))
        reboot.assert_not_called()
        self.assertFalse(self.app.cellular_states["a"]["failure"]["resettable"])

    def test_recovery_clears_the_record_and_enables_the_radio_again(self):
        self.apply(self.failed())
        calls, _reboot = self.apply({"available": True, "state": "disabled",
                                     "radio_enabled": False, "data_active": False})
        self.assertNotIn("a", self.app._modem_failed)
        self.assertIn(["mmcli", "-m", "/mm/0", "--enable"], calls)

    def test_a_failed_modem_is_a_settled_status_and_leaves_vowifi_alone(self):
        self.apply(self.failed())
        self.app.bridges["a"] = SimpleNamespace(poll=lambda: None, pid=9)
        self.app._bridge_started["a"] = time.time() - 10
        with patch.object(self.app, "service_active", return_value=True), \
                patch.object(self.app, "modemmanager_modem_for_tty", return_value="/mm/0"):
            self.app.publish_device_status(WANTED, {"a": {**MODEM, "name": "A"}})
        device = device_state._read(str(self.app.device_status_path), {})["devices"]["a"]
        self.assertFalse(device["transitioning"])
        self.assertEqual(device["error"], "")
        self.assertTrue(device["actual"]["vowifi_bridge_active"])
        self.assertEqual(device["cellular"]["failure"]["reason"], "unknown-capabilities")

    def test_the_failed_reason_is_read_from_modemmanager(self):
        text = ("modem.generic.state                 : failed\n"
                "modem.generic.state-failed-reason   : unknown-capabilities\n"
                "modem.generic.power-state           : on\n")
        with patch.object(self.app, "modemmanager_modem_for_tty", return_value="/mm/0"), \
                patch("host.mdd_orchestrator.run",
                      return_value=SimpleNamespace(returncode=0, stdout=text, stderr="")):
            snapshot = self.app.modem_snapshot(MODEM)
        self.assertEqual((snapshot["state"], snapshot["failed_reason"], snapshot["radio_enabled"]),
                         ("failed", "unknown-capabilities", False))

    def test_a_rebooting_module_keeps_its_count_while_its_ports_are_gone(self):
        self.app._modem_failed = {"a": {"reason": "unknown-capabilities", "since": 0.0,
                                        "resets": 2, "last_reset": time.time()},
                                  "b": {"reason": "unknown-capabilities", "since": 0.0,
                                        "resets": 1, "last_reset": 0.0}}
        self.app.forget_absent_modem_failures(set())
        self.assertIn("a", self.app._modem_failed)
        self.assertNotIn("b", self.app._modem_failed)


class FailedModemWordingTests(unittest.TestCase):
    def test_every_failed_modem_reason_is_translated(self):
        source = (ROOT / "control" / "app" / "main.py").read_text(encoding="utf-8")
        start = source.index('elif host_cell.get("state") == "failed":')
        block = source[start:source.index("elif radio_on and registered", start)]
        sentences = ["".join(re.findall(r'"([^"]*)"', part))
                     for part in re.split(r"\bif\b|\belse\b", block.split("cell_reason = (")[1])]
        sentences = [s for s in sentences if s.startswith("ModemManager")]
        self.assertEqual(len(sentences), 3)
        i18n = (ROOT / "webui" / "src" / "i18n.jsx").read_text(encoding="utf-8")
        zh = i18n[i18n.index("const zh"):i18n.index("const en")]
        for sentence in sentences:
            self.assertIn(f"'{sentence}'", zh)


if __name__ == "__main__":
    unittest.main()
