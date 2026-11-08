import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from runtime import hardware
from runtime.hardware import HardwareSupervisor


class ContainerIdentityRecoveryTests(unittest.TestCase):
    TARGET = {"iccid": "89000000000000000011", "imsi": "001010000000001"}
    OBJECT = "/org/freedesktop/ModemManager1/Modem/7"
    SIM = "/org/freedesktop/ModemManager1/SIM/9"
    MODEM = {"id": "modem-1", "tty": "/dev/ttyUSB2"}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = HardwareSupervisor(data_path=Path(self.temp.name))
        self.request = {
            "request_id": "switch-1", "device_id": "modem-1",
            "require_cellular_identity": True, "started_at": time.time(),
            "expected_iccid_sha256": hashlib.sha256(self.TARGET["iccid"].encode()).hexdigest(),
        }
        self.app.bridge_restart_status(self.request, "stopped")
        self.other = Mock()
        self.other.poll.return_value = None
        self.app.bridges["other"] = self.other
        self.spawn(22)

    def spawn(self, pid):
        process = Mock(pid=pid)
        process.poll.return_value = None
        self.app.bridges["modem-1"] = process
        hardware.atomic_json(self.app.data_path / "modems" / "modem-1.json", {
            **self.TARGET, "bridge_pid": pid,
            "channel_status": "ready", "channel_allocated": 3})
        return process

    def status(self):
        return json.loads((self.app.bridge_restart_status_dir / "switch-1.json").read_text())

    def responses(self, iccid=None, imsi=None, reset_code=0):
        return [
            SimpleNamespace(returncode=0, stdout=f"modem.generic.sim : {self.SIM}\n"),
            SimpleNamespace(returncode=0, stdout=(
                f"sim.properties.iccid : {self.TARGET['iccid'] if iccid is None else iccid}\n"
                f"sim.properties.imsi : {self.TARGET['imsi'] if imsi is None else imsi}\n")),
            SimpleNamespace(returncode=reset_code, stdout=""),
        ]

    def finish(self, responses):
        with patch.object(self.app, "modem_object_for_tty", return_value=self.OBJECT) as lookup, \
                patch.object(self.app, "command", side_effect=responses) as command:
            self.app.finish_bridge_restart_requests({"modem-1", "other"}, [self.MODEM], [self.OBJECT])
        return lookup, command

    def test_request_consumer_preserves_required_verification(self):
        hardware.atomic_json(self.app.bridge_restart_request_dir / "switch-1.json", self.request)
        self.app.process_bridge_restart_requests()
        self.assertTrue(self.status()["require_cellular_identity"])
        self.other.terminate.assert_not_called()

    def test_matching_identity_satisfies_control_handshake(self):
        lookup, command = self.finish(self.responses())
        lookup.assert_called_once_with("/dev/ttyUSB2", [self.OBJECT])
        self.assertEqual(self.status()["state"], "channels_ready")
        self.assertEqual(self.status()["cellular_identity"], "matched")
        self.assertEqual(command.call_count, 2)

    def test_placeholder_iccid_can_match_imsi(self):
        self.finish(self.responses(iccid="--"))
        self.assertEqual(self.status()["cellular_identity"], "matched")

    def test_conflicting_iccid_resets_only_target_and_rechecks_replacement(self):
        old = self.app.bridges["modem-1"]
        _, command = self.finish(self.responses(iccid="89000000000000000022"))
        self.assertEqual(self.status()["state"], "stopped")
        self.assertTrue(self.status()["modem_reset_attempted"])
        command.assert_called_with("mmcli", "-m", self.OBJECT, "--reset", timeout=20)
        old.terminate.assert_called_once()
        self.other.terminate.assert_not_called()
        self.spawn(23)
        _, command = self.finish(self.responses(iccid="89000000000000000022"))
        self.assertEqual(command.call_count, 2)
        self.assertEqual(self.status()["state"], "spawned")
        self.finish(self.responses())
        self.assertEqual(self.status()["state"], "channels_ready")
        self.assertEqual(self.status()["bridge_pid"], 23)

    def test_reset_failure_is_terminal(self):
        self.finish(self.responses(iccid="89000000000000000022", reset_code=1))
        self.assertEqual(self.status()["state"], "failed")

    def test_unreadable_identity_does_not_reset(self):
        _, command = self.finish(self.responses(iccid="--", imsi="--"))
        self.assertEqual(command.call_count, 2)
        self.assertEqual(self.status()["state"], "spawned")

    def test_command_exception_fails_closed(self):
        self.finish(hardware.subprocess.TimeoutExpired("mmcli", 10))
        self.assertEqual(self.status()["state"], "failed")

    def test_missing_modem_object_cannot_complete(self):
        with patch.object(self.app, "modem_object_for_tty", return_value=""), \
                patch.object(self.app, "command") as command:
            self.app.finish_bridge_restart_requests({"modem-1"}, [self.MODEM], [])
        command.assert_not_called()
        self.assertEqual(self.status()["state"], "spawned")

    def test_reenumeration_does_not_rebuild_other_pcsc_bridges(self):
        request = {**self.request, "modem_reset_attempted": True}
        self.app.bridge_restart_status(request, "stopped")
        with patch.object(self.app, "stop_pcsc") as stop, \
                patch.object(self.app, "render_reader_config") as render:
            self.app.reconcile_pcsc([], [])
        stop.assert_not_called()
        render.assert_not_called()
        self.other.terminate.assert_not_called()
        self.app.finish_bridge_restart_requests({"other"}, [], [])
        self.assertEqual(self.status()["state"], "stopped")
        request["started_at"] = time.time() - 91
        self.app.bridge_restart_status(request, "stopped")
        self.app.finish_bridge_restart_requests({"other"}, [], [])
        self.assertEqual(self.status()["state"], "failed")

    def test_supervisor_restart_preserves_reset_attempt(self):
        self.app.bridge_restart_status({**self.request, "modem_reset_attempted": True}, "stopped")
        restarted = HardwareSupervisor(data_path=self.app.data_path)
        self.assertTrue(restarted.bridge_restarts["switch-1"]["modem_reset_attempted"])

    def test_qmi_power_cycle_converges_and_falls_back_to_reset_after_settling_window(self):
        old = self.app.bridges["modem-1"]
        with patch.object(self.app, "qmi_wdm_for_usb_path", return_value="/dev/cdc-wdm0"), \
                patch.object(self.app, "qmi_uim_power_cycle_sim", return_value=(True, "")) as qmi:
            _, command = self.finish(self.responses(iccid="89000000000000000022"))
        qmi.assert_called_once_with("/dev/cdc-wdm0")
        old.terminate.assert_called_once()
        self.other.terminate.assert_not_called()
        self.assertEqual(command.call_count, 2)
        self.assertEqual(self.status()["state"], "stopped")
        self.assertTrue(self.status()["qmi_power_cycle_attempted"])
        self.assertTrue(self.status()["qmi_power_cycle_succeeded"])
        self.assertFalse(self.status().get("modem_reset_attempted", False))

        # During the 25s QMI settling window, a still-stale ICCID waits without calling mmcli --reset.
        self.spawn(23)
        _, command = self.finish(self.responses(iccid="89000000000000000022"))
        self.assertEqual(command.call_count, 2)
        self.assertEqual(self.status()["state"], "spawned")
        self.assertFalse(self.status().get("modem_reset_attempted", False))

        # Once the settling window expires without convergence, it falls back to mmcli --reset.
        self.app.bridge_restart_status(
            self.status(), "spawned", qmi_power_cycle_at=time.time() - 26)
        _, command = self.finish(self.responses(iccid="89000000000000000022"))
        command.assert_called_with("mmcli", "-m", self.OBJECT, "--reset", timeout=20)
        self.assertEqual(self.status()["state"], "stopped")
        self.assertTrue(self.status()["modem_reset_attempted"])

        # After hot-swap or reset, matching identity satisfies the handshake.
        self.spawn(24)
        self.finish(self.responses())
        self.assertEqual(self.status()["state"], "channels_ready")
        self.assertEqual(self.status()["bridge_pid"], 24)

    def test_failed_qmi_power_cycle_falls_back_immediately_to_reset(self):
        with patch.object(self.app, "qmi_wdm_for_usb_path", return_value="/dev/cdc-wdm0"), \
                patch.object(self.app, "qmi_uim_power_cycle_sim",
                             return_value=(False, "QMI protocol error")) as qmi:
            _, command = self.finish(self.responses(iccid="89000000000000000022"))
        qmi.assert_called_once_with("/dev/cdc-wdm0")
        command.assert_called_with("mmcli", "-m", self.OBJECT, "--reset", timeout=20)
        self.assertEqual(self.status()["state"], "stopped")
        self.assertTrue(self.status()["qmi_power_cycle_attempted"])
        self.assertFalse(self.status()["qmi_power_cycle_succeeded"])
        self.assertTrue(self.status()["modem_reset_attempted"])

    def test_qmi_power_cycle_timeout_attempts_bounded_power_on_recovery(self):
        with tempfile.TemporaryDirectory() as dev_dir:
            wdm = Path(dev_dir) / "cdc-wdm0"
            wdm.write_text("")
            timeout_exc = hardware.subprocess.TimeoutExpired("python3", 26)
            with patch.object(
                self.app, "command",
                side_effect=[timeout_exc, SimpleNamespace(returncode=0, stdout="OK\n")],
            ) as cmd:
                ok, err = self.app.qmi_uim_power_cycle_sim(str(wdm), slot=1, off_seconds=2.0)
            self.assertFalse(ok)
            self.assertIn("timed out", err)
            self.assertIn("power-on recovery succeeded", err)
            self.assertEqual(cmd.call_count, 2)
            first_args = cmd.call_args_list[0].args
            second_args = cmd.call_args_list[1].args
            self.assertEqual(first_args[3:7], (str(wdm), "1", "2.0", "cycle"))
            self.assertEqual(cmd.call_args_list[0].kwargs.get("timeout"), 26)
            self.assertEqual(second_args[3:7], (str(wdm), "1", "0", "on"))
            self.assertEqual(cmd.call_args_list[1].kwargs.get("timeout"), 14)

    def test_reconcile_pcsc_respawns_bridge_when_modemmanager_object_path_changes(self):
        old_obj = "/org/freedesktop/ModemManager1/Modem/0"
        new_obj = "/org/freedesktop/ModemManager1/Modem/1"
        modem = {"id": "modem-1", "tty": "/dev/ttyUSB2", "base_port": 0x8C7C}
        self.app.pcsc_fingerprint = hashlib.sha256(
            json.dumps([(modem["id"], modem["tty"])]).encode()).hexdigest()
        old_proc = self.app.bridges["modem-1"]
        self.app.bridge_objects["modem-1"] = old_obj
        new_proc = Mock(pid=99)
        new_proc.poll.return_value = None

        with patch.object(self.app, "modem_object_for_tty", return_value=new_obj), \
                patch("runtime.hardware.subprocess.Popen", return_value=new_proc) as popen, \
                patch("runtime.hardware.time.monotonic", side_effect=[100.0, 100.0, 102.0, 102.0]):
            self.app.reconcile_pcsc([modem], [new_obj])
            old_proc.terminate.assert_called_once()
            self.assertNotIn("modem-1", self.app.bridges)
            # After the 1-second stale-object backoff, the replacement spawns on new_obj.
            self.app.reconcile_pcsc([modem], [new_obj])
            popen.assert_called_once()
            spawn_cmd = popen.call_args.args[0]
            self.assertEqual(spawn_cmd[spawn_cmd.index("--modemmanager") + 1], new_obj)
            self.assertEqual(self.app.bridge_objects["modem-1"], new_obj)


if __name__ == "__main__":
    unittest.main()
