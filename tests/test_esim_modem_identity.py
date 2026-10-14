import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from host import mdd_orchestrator
from host.mdd_orchestrator import Orchestrator


class ModemIdentityTests(unittest.TestCase):
    TARGET = {"iccid": "89000000000000000011", "imsi": "001010000000001"}

    def test_identity_matching(self):
        cases = [
            ({"iccid": self.TARGET["iccid"]}, True),
            ({"iccid": "", "imsi": self.TARGET["imsi"]}, True),
            ({"iccid": "--", "imsi": self.TARGET["imsi"]}, True),
            ({"iccid": "", "imsi": "001010000000002"}, False),
            ({"iccid": "89000000000000000022", "imsi": self.TARGET["imsi"]}, False),
            ({"iccid": "", "imsi": ""}, False),
        ]
        for observed, expected in cases:
            with self.subTest(observed=observed):
                self.assertEqual(Orchestrator.esim_identity_matches(observed, self.TARGET), expected)
        self.assertFalse(Orchestrator.esim_identity_matches(self.TARGET, {}))


class ModemRecoveryHandshakeTests(unittest.TestCase):
    TARGET = ModemIdentityTests.TARGET
    OBJECT = "/org/freedesktop/ModemManager1/Modem/7"
    SIM = "/org/freedesktop/ModemManager1/SIM/9"
    DEVICE = "2c7c-0125-testserial"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Orchestrator(Path(self.temp.name) / "data", Path(self.temp.name))
        self.request = {
            "request_id": "switch-1", "device_id": self.DEVICE,
            "require_cellular_identity": True, "started_at": time.time(),
            "expected_iccid_sha256": hashlib.sha256(self.TARGET["iccid"].encode()).hexdigest(),
        }
        self.app._bridge_restart_status(self.request, "stopped")
        self.other = Mock()
        self.other.poll.return_value = None
        self.app.bridges["other"] = self.other
        self.spawn(22)

    def spawn(self, pid, cellular=True):
        proc = Mock(pid=pid)
        proc.poll.return_value = None
        self.app.bridges[self.DEVICE] = proc
        self.app.bridge_ports[self.DEVICE] = 15360
        command = ["python", "bridge.py", "--modem", "/dev/ttyUSB2"]
        if cellular:
            command += ["--modemmanager", self.OBJECT]
        self.app._bridge_commands[self.DEVICE] = command
        mdd_orchestrator.atomic_json(self.app.data / "modems" / f"{self.DEVICE}.json", {
            **self.TARGET, "bridge_pid": pid,
            "channel_status": "ready", "channel_allocated": 3,
        })
        return proc

    def status(self):
        return mdd_orchestrator.read_json(
            self.app.bridge_restart_status_dir / "switch-1.json")

    def responses(self, iccid=None, imsi=None, reset_code=0):
        return [
            SimpleNamespace(returncode=0, stdout=f"modem.generic.sim : {self.SIM}\n"),
            SimpleNamespace(returncode=0, stdout=(
                f"sim.properties.iccid : {self.TARGET['iccid'] if iccid is None else iccid}\n"
                f"sim.properties.imsi : {self.TARGET['imsi'] if imsi is None else imsi}\n")),
            SimpleNamespace(returncode=reset_code, stdout=""),
        ]

    def finish(self, responses):
        with patch.object(self.app, "modemmanager_modem_for_tty", return_value=self.OBJECT) as lookup, \
                patch.object(mdd_orchestrator.subprocess, "run", side_effect=responses) as runner:
            self.app.finish_bridge_restart_requests({self.DEVICE, "other"})
        return lookup, runner

    def test_live_identity_is_read_through_the_selected_tty(self):
        lookup, runner = self.finish(self.responses())
        lookup.assert_called_once_with("/dev/ttyUSB2", timeout=10)
        self.assertEqual(self.status()["state"], "channels_ready")
        self.assertEqual(self.status()["cellular_identity"], "matched")
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args_list[1].args[0],
                         ["mmcli", "-i", self.SIM, "--output-keyvalue"])

    def test_physical_port_lookup_ignores_other_modems(self):
        other = "/org/freedesktop/ModemManager1/Modem/3"
        results = [
            SimpleNamespace(returncode=0, stdout=f"{other}\n{self.OBJECT}\n"),
            SimpleNamespace(returncode=0, stdout="modem.generic.primary-port : ttyUSB20\n"),
            SimpleNamespace(returncode=0, stdout="modem.generic.primary-port : ttyUSB2\n"),
        ]
        with patch.object(mdd_orchestrator, "run", side_effect=results):
            self.assertEqual(self.app.modemmanager_modem_for_tty("/dev/ttyUSB2"), self.OBJECT)

    def test_sim_slot_keys_cannot_replace_the_primary_sim_path(self):
        responses = self.responses()
        responses[0].stdout += "modem.generic.sim-slots.value[1] : /wrong/path\n"
        _, runner = self.finish(responses)
        self.assertEqual(runner.call_args.args[0], ["mmcli", "-i", self.SIM, "--output-keyvalue"])
        self.assertEqual(self.status()["cellular_identity"], "matched")

    def test_placeholder_iccid_falls_back_to_live_imsi(self):
        self.finish(self.responses(iccid="--"))
        self.assertEqual(self.status()["cellular_identity"], "matched")

    def test_stale_identity_resets_only_target_then_requires_new_bridge_and_identity(self):
        old = self.app.bridges[self.DEVICE]
        _, runner = self.finish(self.responses(iccid="89000000000000000022"))
        self.assertEqual(self.status()["state"], "stopped")
        self.assertTrue(self.status()["modem_reset_attempted"])
        old.terminate.assert_called_once()
        self.other.terminate.assert_not_called()
        self.assertEqual(runner.call_args.args[0], ["mmcli", "-m", self.OBJECT, "--reset"])
        self.assertNotIn(self.DEVICE, self.app.bridges)
        self.spawn(23)
        _, runner = self.finish(self.responses(iccid="89000000000000000022"))
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(self.status()["state"], "spawned")
        self.finish(self.responses())
        self.assertEqual(self.status()["state"], "channels_ready")
        self.assertEqual(self.status()["bridge_pid"], 23)

    def test_direct_serial_bridge_needs_no_mm_or_systemctl(self):
        self.spawn(23, cellular=False)
        with patch.object(mdd_orchestrator.subprocess, "run") as runner:
            self.app.finish_bridge_restart_requests({self.DEVICE, "other"})
        runner.assert_not_called()
        self.assertEqual(self.status()["state"], "channels_ready")
        self.assertEqual(self.status()["cellular_identity"], "not_applicable")

    def test_unreadable_identity_waits_without_resetting(self):
        _, runner = self.finish(self.responses(iccid="--", imsi="--"))
        self.assertEqual(self.status()["state"], "spawned")
        self.assertEqual(runner.call_count, 2)

    def test_unmatched_tty_cannot_complete(self):
        with patch.object(self.app, "modemmanager_modem_for_tty", return_value=""), \
                patch.object(mdd_orchestrator.subprocess, "run") as runner:
            self.app.finish_bridge_restart_requests({self.DEVICE, "other"})
        runner.assert_not_called()
        self.assertEqual(self.status()["state"], "spawned")

    def test_reset_failure_is_terminal_and_does_not_restart_shared_service(self):
        self.finish(self.responses(iccid="89000000000000000022", reset_code=1))
        self.assertEqual(self.status()["state"], "failed")
        self.other.terminate.assert_not_called()

    def test_missing_mmcli_and_timeout_fail_closed(self):
        for error in (FileNotFoundError("mmcli"), mdd_orchestrator.subprocess.TimeoutExpired("mmcli", 10)):
            with self.subTest(error=type(error).__name__):
                self.app._bridge_restart_status(self.request, "stopped")
                self.finish(error)
                self.assertEqual(self.status()["state"], "failed")

    def test_reset_reenumeration_waits_until_deadline(self):
        request = {**self.request, "modem_reset_attempted": True}
        self.app._bridge_restart_status(request, "stopped")
        self.app.finish_bridge_restart_requests({"other"})
        self.assertEqual(self.status()["state"], "stopped")
        request["started_at"] = time.time() - 91
        self.app._bridge_restart_status(request, "stopped")
        self.app.finish_bridge_restart_requests({"other"})
        self.assertEqual(self.status()["state"], "failed")

    def test_reset_reenumeration_keeps_shared_backend_running(self):
        self.app.applied_cellular_backend = True
        request = {**self.request, "modem_reset_attempted": True}
        self.app._bridge_restart_status(request, "stopped")
        plan = self.app.capability_plan({})
        self.assertTrue(self.app.cellular_backend_needed(plan, set(), {}))
        self.assertFalse(self.app.cellular_backend_needed(plan, set(), {"modem_backend": "serial"}))
        self.app._bridge_restart_status(request, "failed")
        self.assertFalse(self.app.cellular_backend_needed(plan, set(), {}))

    def test_fallback_after_reset_cannot_hide_cellular_failure(self):
        self.app._bridge_restart_status({**self.request, "modem_reset_attempted": True}, "stopped")
        self.spawn(23, cellular=False)
        self.app.finish_bridge_restart_requests({self.DEVICE, "other"})
        self.assertEqual(self.status()["state"], "failed")

    def test_missing_backend_metadata_fails_closed(self):
        self.app._bridge_commands.clear()
        self.app.finish_bridge_restart_requests({self.DEVICE, "other"})
        self.assertEqual(self.status()["state"], "failed")

    def test_host_restart_retains_reset_attempt(self):
        self.app._bridge_restart_status({**self.request, "modem_reset_attempted": True}, "stopped")
        restarted = Orchestrator(self.app.data, self.app.repo)
        self.assertTrue(restarted._bridge_restarts["switch-1"]["modem_reset_attempted"])

    def test_pending_recovery_does_not_use_idle_backoff(self):
        clock = [1000.0]
        slept = []

        def sleep(seconds):
            clock[0] += seconds
            slept.append(seconds)

        with patch.object(mdd_orchestrator.time, "time", side_effect=lambda: clock[0]), \
                patch.object(mdd_orchestrator.time, "sleep", side_effect=sleep), \
                patch.object(self.app, "_input_mtimes", return_value=()):
            self.app._sleep_for_work(60)
        self.assertEqual(sum(slept), self.app.interval)

    def test_request_consumption_retains_cellular_requirement(self):
        self.app._bridge_restarts.clear()
        mdd_orchestrator.atomic_json(
            self.app.bridge_restart_request_dir / "switch-1.json", self.request)
        self.app.process_bridge_restart_requests()
        self.assertTrue(self.status()["require_cellular_identity"])


if __name__ == "__main__":
    unittest.main()
