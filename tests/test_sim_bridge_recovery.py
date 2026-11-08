import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from host import vpcd_modem_bridge as bridge
from host.mdd_orchestrator import Orchestrator
from runtime.hardware import HardwareSupervisor


class SIMAccessRecoveryTests(unittest.TestCase):
    def card(self):
        card = bridge.ModemCard.__new__(bridge.ModemCard)
        card.debug = False
        card.access_health = bridge.SIMAccessHealth()
        return card

    def test_phone_failure_triggers_recovery_despite_healthy_other_channels(self):
        card = self.card()
        now = [0.0]

        def at(command):
            if '"01' in command:
                raise bridge.ModemError("MobileEquipment.PhoneFailure: Phone failure")
            return b'+CSIM: 4,"9000"'

        card._at = at
        with patch.object(bridge.time, "monotonic", side_effect=lambda: now[0]):
            for second in (0, 15, 31):
                now[0] = second
                self.assertEqual(card.transmit(bridge.SELECT_MF, 1), bytes.fromhex("6F00"))
                self.assertEqual(card.transmit(bridge.SELECT_MF, 2), bytes.fromhex("9000"))
                card.csim(bridge.SELECT_MF)  # basic-channel identity must not clear channel 1
                if second < 31:
                    self.assertFalse(card.access_health.failed.is_set())
        self.assertTrue(card.access_health.failed.is_set())

    def test_a_single_failure_and_successful_channel_recovery_do_not_restart(self):
        card = self.card()
        card._at = Mock(side_effect=[bridge.ModemError("PhoneFailure"),
                                    b'+CSIM: 4,"9000"', bridge.ModemError("PhoneFailure")])
        with patch.object(bridge.time, "monotonic", side_effect=(0, 100)):
            card.transmit(bridge.SELECT_MF, 1)
            card.transmit(bridge.SELECT_MF, 1)
            card.transmit(bridge.SELECT_MF, 1)
        self.assertFalse(card.access_health.failed.is_set())
        self.assertEqual(card.access_health.streaks[1][0], 1)

    def test_pin_file_and_authentication_card_errors_never_trigger_recovery(self):
        card = self.card()
        for status in ("63C2", "6982", "6A82", "9862", "6F00"):
            card._at = Mock(return_value=('+CSIM: 4,"%s"' % status).encode())
            for _ in range(10):
                self.assertEqual(card.transmit(bytes.fromhex("0020000100"), 1),
                                 bytes.fromhex(status))
        self.assertFalse(card.access_health.failed.is_set())
        self.assertEqual(card._at.call_count, 10, "each APDU is sent exactly once")

    def test_invalidated_logical_channel_is_rebuilt(self):
        card = self.card()
        card._at = Mock(return_value=b'+CSIM: 4,"6881"')
        with patch.object(bridge.time, "monotonic", side_effect=(0, 15, 31)):
            for _ in range(3):
                card.transmit(bridge.SELECT_MF, 1)
        self.assertTrue(card.access_health.failed.is_set())

    def test_many_errors_in_one_burst_do_not_rebuild(self):
        health = bridge.SIMAccessHealth()
        with patch.object(bridge.time, "monotonic", return_value=0):
            for _ in range(100):
                health.observe(1, True)
        self.assertFalse(health.failed.is_set())

    def test_failed_bridge_releases_channels_and_exits_with_recovery_code(self):
        with tempfile.TemporaryDirectory() as temp:
            metadata = Path(temp) / "modem.json"
            card = Mock()
            card.identity.return_value = {"imei": "", "iccid": ""}
            card.open_channel.side_effect = (1, 2, 3)
            thread = Mock()
            # Real main establishes channels and arms the monitor before starting slots.
            thread.start.side_effect = lambda: card.access_health.failed.set()
            with patch.object(bridge.sys, "argv", ["bridge", "--modemmanager", "0",
                                                  "--metadata-file", str(metadata),
                                                  "--identity-refresh", "0"]), \
                    patch.object(bridge, "ModemManagerCard", return_value=card), \
                    patch.object(bridge.threading, "Thread", return_value=thread), \
                    patch.object(bridge.signal, "signal"):
                self.assertEqual(bridge.main(), bridge.SIM_ACCESS_RECOVERY_EXIT)
            self.assertEqual([call.args[0] for call in card.close_channel.call_args_list],
                             [1, 2, 3, 1, 2, 3])
            state = json.loads(metadata.read_text())
            self.assertEqual(state["channel_status"], "error")
            self.assertIn("SIM access failed", state["channel_error"])
            card.close.assert_called_once()


class RecoveryBudgetTests(unittest.TestCase):
    def test_stable_replacement_ordinary_exit_retires_old_incident(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "budget.json"
            with patch.object(bridge.time, "time", return_value=10000):
                budget = bridge.BridgeRecoveryBudget(path)
                budget.record_exit("a", 75)
                budget.record_exit("a", 1, uptime=bridge.BRIDGE_STABLE_SECONDS)
                self.assertEqual(budget.retry_at("a"), 0)
                self.assertNotIn("a", bridge.BridgeRecoveryBudget(path).records)

    def test_slow_watchdog_failures_still_exhaust_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            budget = bridge.BridgeRecoveryBudget(Path(temp) / "budget.json")
            with patch.object(bridge.time, "time", return_value=10000):
                for _ in range(4):
                    budget.record_exit("a", 75, uptime=120)
                self.assertTrue(budget.records["a"]["limited"])

    def test_diagnostic_snapshot_is_normalized_detached_and_expires(self):
        with tempfile.TemporaryDirectory() as temp:
            budget = bridge.BridgeRecoveryBudget(Path(temp) / "budget.json")
            with patch.object(bridge.time, "time", return_value=10000):
                budget.record_exit("a", 75)
                snapshot = budget.snapshot()
                self.assertEqual(snapshot["a"]["count"], 1)
                self.assertEqual(snapshot["a"]["at"], 10000)
                self.assertIn("waiting", snapshot["a"]["reason"])
                snapshot["a"]["count"] = 100
                self.assertEqual(budget.records["a"]["failures"], 1)
            with patch.object(bridge.time, "time", return_value=13600):
                self.assertEqual(budget.snapshot(), {})

    def test_legacy_mode_publishes_and_logs_recovery_pause(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp), Path(temp), dry_run=True)
            process = Mock(returncode=75)
            with patch.object(bridge.time, "time", return_value=10000), \
                    patch.object(app, "log") as log, \
                    patch.object(app, "_bridge_stderr_tail", return_value="SIM access failed"):
                for _ in range(4):
                    app._record_bridge_exit("a", process, 9900)
                self.assertTrue(any("paused after three" in call.args[0]
                                    for call in log.call_args_list))
                with patch.object(app, "service_active", return_value=False):
                    app.publish_device_status({}, {"a": {"id": "a", "tty": "ttyUSB2"}})
                state = json.loads(app.device_status_path.read_text())
                self.assertIn("paused after three", state["devices"]["a"]["error"])

    def test_cooldown_limit_survives_restart_and_counts_replacement_startup_failures(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "budget.json"
            with patch.object(bridge.time, "time", return_value=10000):
                budget = bridge.BridgeRecoveryBudget(path)
                budget.record_exit("a", bridge.SIM_ACCESS_RECOVERY_EXIT)
                self.assertEqual(budget.retry_at("a"), 10015)
                self.assertEqual(budget.retry_at("b"), 0)
            for now, code, expected in ((10020, 1, 10050), (10060, 75, 10120),
                                         (10130, 1, 13600)):
                with patch.object(bridge.time, "time", return_value=now):
                    budget = bridge.BridgeRecoveryBudget(path)
                    budget.record_exit("a", code)
                    self.assertEqual(budget.retry_at("a"), expected)
            with patch.object(bridge.time, "time", return_value=11000):
                self.assertIn("paused after three", budget.error("a"))
            with patch.object(bridge.time, "time", return_value=13601):
                self.assertEqual(budget.error("a"), "")
                budget.record_exit("a", 75)
                self.assertEqual(budget.records["a"]["failures"], 1)

    def test_normal_startup_errors_do_not_consume_runtime_recovery_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            budget = bridge.BridgeRecoveryBudget(Path(temp) / "budget.json")
            budget.record_exit("a", 1)
            self.assertEqual(budget.records, {})

    def test_legacy_supervisor_respects_persistent_recovery_gate(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp), Path(temp), dry_run=True)
            with patch.object(bridge.time, "time", return_value=10000):
                app.bridge_recovery.record_exit("a", 75)
                self.assertFalse(app._bridge_retry_due("a"))
                self.assertTrue(app._bridge_retry_due("b"))


class ContainerRecoveryTests(unittest.TestCase):
    def test_container_supplies_runtime_before_counting_an_ordinary_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            app = HardwareSupervisor(data_path=Path(temp))
            modems = [{"id": "a", "tty": "ttyUSB2"}]
            app.pcsc_fingerprint = hashlib.sha256(json.dumps(
                [("a", "ttyUSB2")]).encode()).hexdigest()
            with patch.object(bridge.time, "time", return_value=10000):
                app.bridge_recovery.record_exit("a", 75)
                process = Mock(returncode=1)
                process.poll.return_value = 1
                app.bridges["a"] = process
                app.bridge_started_at["a"] = 100
                with patch.object(bridge.time, "monotonic", return_value=200), \
                        patch.object(app, "modem_object_for_tty", return_value=""):
                    app.reconcile_pcsc(modems, [])
                self.assertNotIn("a", app.bridge_recovery.records)
                self.assertNotIn("a", app.bridge_started_at)

    def test_only_failed_bridge_is_rebuilt_after_cooldown(self):
        with tempfile.TemporaryDirectory() as temp:
            app = HardwareSupervisor(data_path=Path(temp))
            modems = [{"id": name, "tty": "/dev/ttyUSB" + str(index)}
                      for index, name in enumerate(("a", "b"))]
            app.pcsc_fingerprint = hashlib.sha256(json.dumps(
                [(item["id"], item["tty"]) for item in modems]).encode()).hexdigest()
            failed, healthy, replacement = Mock(), Mock(), Mock()
            failed.poll.return_value = failed.returncode = 75
            healthy.poll.return_value = None
            replacement.poll.return_value = None
            app.bridges = {"a": failed, "b": healthy}
            with patch.object(bridge.time, "time", return_value=10000), \
                    patch.object(bridge.time, "monotonic", return_value=100), \
                    patch.object(app, "modem_object_for_tty", return_value="/modem/0"), \
                    patch("runtime.hardware.subprocess.Popen", return_value=replacement) as spawn:
                app.reconcile_pcsc(modems, [])
                spawn.assert_not_called()
                self.assertNotIn("a", app.bridges)
                app.bridge_retry_at["a"] = 0
                with patch.object(bridge.time, "time", return_value=10016):
                    app.reconcile_pcsc(modems, [])
                spawn.assert_called_once()
                self.assertIs(app.bridges["a"], replacement)
                self.assertIs(app.bridges["b"], healthy)
                healthy.terminate.assert_not_called()

    def test_live_bridge_does_not_consume_budget_or_report_recovery_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            app = HardwareSupervisor(data_path=Path(temp))
            app.publish_control_state([{"id": "a", "tty": "ttyUSB2", "usb_path": "1-2",
                                        "vid": "2c7c", "pid": "0125"}], set(),
                                      {"a": "SIM access recovery paused"})
            state = json.loads((Path(temp) / "orchestrator/devices-status.json").read_text())
            self.assertFalse(state["devices"]["a"]["actual"]["vowifi_bridge_active"])
            self.assertIn("recovery paused", state["devices"]["a"]["error"])
            self.assertEqual(app.bridge_recovery.records, {})


if __name__ == "__main__":
    unittest.main()
