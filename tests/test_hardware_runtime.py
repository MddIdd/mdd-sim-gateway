import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from runtime.hardware import HardwareSupervisor, kernel_objects


class HardwareRuntimeTests(unittest.TestCase):
    def test_loop_retries_a_transient_reconcile_failure_without_stopping_services(self):
        app = HardwareSupervisor(interval=0)
        app.start = Mock()
        app.dbus = app.modemmanager = app.networkmanager = Mock()
        app.dbus.poll.return_value = None
        app.publish_reconcile_error = Mock()

        attempts = iter((RuntimeError("mmcli unavailable"), None))
        def reconcile():
            outcome = next(attempts)
            if outcome:
                raise outcome
            app.stop = True
        app.reconcile = Mock(side_effect=reconcile)
        app.loop()

        self.assertEqual(app.reconcile.call_count, 2)
        app.publish_reconcile_error.assert_called_once()

    def test_close_marks_published_devices_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            root = data / "orchestrator"
            root.mkdir()
            (root / "devices-status.json").write_text(json.dumps({
                "version": 2,
                "devices": {"modem-1": {"present": True, "transitioning": True,
                                           "actual": {"vowifi_bridge_active": True,
                                                      "cellular_backend_active": True}}},
                "shared": {"modemmanager_active": True}}))
            app = HardwareSupervisor(status_path=data / "status.json", data_path=data)
            app.close()

            state = json.loads((root / "devices-status.json").read_text())
            self.assertFalse(state["devices"]["modem-1"]["present"])
            self.assertFalse(state["shared"]["modemmanager_active"])
    def test_esim_bridge_restart_waits_for_new_ready_pid_and_target_iccid(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            app = HardwareSupervisor(data_path=data)
            old = Mock()
            old.poll.return_value = None
            app.bridges = {"modem-1": old}
            request_id = "switch-1"
            app.bridge_restart_request_dir.mkdir(parents=True)
            (app.bridge_restart_request_dir / f"{request_id}.json").write_text(json.dumps({
                "request_id": request_id, "device_id": "modem-1",
                "expected_iccid_sha256": hashlib.sha256(b"profile-target").hexdigest(),
                "requested_at": 100,
            }))

            app.process_bridge_restart_requests()

            old.terminate.assert_called_once()
            old.wait.assert_called_once_with(8)
            status_path = app.bridge_restart_status_dir / f"{request_id}.json"
            self.assertEqual(json.loads(status_path.read_text())["state"], "stopped")

            replacement = Mock(pid=22)
            replacement.poll.return_value = None
            app.bridges["modem-1"] = replacement
            identity_path = data / "modems" / "modem-1.json"
            identity_path.parent.mkdir(parents=True)
            identity_path.write_text(json.dumps({
                "bridge_pid": 22, "channel_status": "ready", "channel_allocated": 3,
                "iccid": "profile-old"}))
            app.finish_bridge_restart_requests({"modem-1"})
            self.assertEqual(json.loads(status_path.read_text())["state"], "spawned")

            identity_path.write_text(json.dumps({
                "bridge_pid": 22, "channel_status": "ready", "channel_allocated": 3,
                "iccid": "profile-target"}))
            app.finish_bridge_restart_requests({"modem-1"})
            status = json.loads(status_path.read_text())
            self.assertEqual(status["state"], "channels_ready")
            self.assertEqual(status["bridge_pid"], 22)

    def test_invalid_esim_bridge_restart_is_rejected_without_stopping_bridge(self):
        with tempfile.TemporaryDirectory() as temp:
            app = HardwareSupervisor(data_path=Path(temp))
            process = Mock()
            process.poll.return_value = None
            app.bridges = {"modem-1": process}
            app.bridge_restart_request_dir.mkdir(parents=True)
            (app.bridge_restart_request_dir / "switch-1.json").write_text(json.dumps({
                "request_id": "switch-1", "device_id": "../modem-1",
                "expected_iccid_sha256": "bad"}))

            app.process_bridge_restart_requests()

            process.terminate.assert_not_called()
            status = json.loads((app.bridge_restart_status_dir /
                                 "switch-1.json").read_text())
            self.assertEqual(status["state"], "failed")

    def test_replugged_serialless_modem_migrates_saved_device_id(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            root = data / "orchestrator"
            identities = data / "modems"
            root.mkdir()
            identities.mkdir()
            old_id = "2c7c-0125-1-3"
            new_id = "2c7c-0125-1-3.1"
            wanted = {"cellular_enabled": False, "vowifi_enabled": True,
                      "flight_mode": False}
            (root / "devices-desired.json").write_text(json.dumps({
                "version": 2, "devices": {old_id: wanted}}))
            (root / "hardware-state.json").write_text(json.dumps({
                "assignments": {old_id: {"usb_path": "1-3"}}}))
            (root / "devices-status.json").write_text(json.dumps({
                "devices": {old_id: {"present": False}}}))
            for device_id in (old_id, new_id):
                (identities / f"{device_id}.json").write_text(json.dumps({
                    "hardware_id": device_id, "imei": "350000000000036"}))

            app = HardwareSupervisor(data_path=data)
            moved = app.migrate_device_ids([{
                "id": new_id, "tty": "/dev/ttyUSB2", "usb_path": "1-3.1",
                "vid": "2c7c", "pid": "0125"}])

            self.assertEqual(moved, [(old_id, new_id)])
            desired = json.loads((root / "devices-desired.json").read_text())["devices"]
            self.assertEqual(desired, {new_id: wanted})
            self.assertFalse((identities / f"{old_id}.json").exists())
            self.assertTrue((identities / f"{new_id}.json").exists())
            assignments = json.loads((root / "hardware-state.json").read_text())["assignments"]
            self.assertNotIn(old_id, assignments)

    def test_modem_id_migration_waits_for_matching_hardware_imei(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            root = data / "orchestrator"
            identities = data / "modems"
            root.mkdir()
            identities.mkdir()
            old_id = "2c7c-0125-1-3"
            new_id = "2c7c-0125-1-3.1"
            (root / "devices-desired.json").write_text(json.dumps({
                "version": 2, "devices": {old_id: {"vowifi_enabled": True}}}))
            (identities / f"{old_id}.json").write_text(json.dumps({
                "imei": "350000000000036"}))
            app = HardwareSupervisor(data_path=data)
            modem = {"id": new_id, "vid": "2c7c", "pid": "0125"}

            self.assertEqual(app.migrate_device_ids([modem]), [])
            (identities / f"{new_id}.json").write_text(json.dumps({
                "imei": "490154203237518"}))
            self.assertEqual(app.migrate_device_ids([modem]), [])
            desired = json.loads((root / "devices-desired.json").read_text())["devices"]
            self.assertIn(old_id, desired)

    def test_control_status_reports_present_cellular_modem(self):
        with tempfile.TemporaryDirectory() as temp:
            app = HardwareSupervisor(data_path=Path(temp))
            modem = {"id": "2c7c-0125-port", "tty": "/dev/ttyUSB2",
                     "usb_path": "1-3", "vid": "2c7c", "pid": "0125"}
            app.publish_control_state([modem], {modem["id"]})
            status = json.loads((Path(temp) / "orchestrator" /
                                 "devices-status.json").read_text())
            observed = status["devices"][modem["id"]]
            self.assertTrue(observed["present"])
            self.assertTrue(observed["actual"]["vowifi_bridge_active"])
            self.assertTrue(observed["actual"]["cellular_supported"])
            hardware = json.loads((Path(temp) / "orchestrator" /
                                   "hardware-state.json").read_text())
            self.assertIn(modem["id"], hardware["assignments"])

    def test_a_card_the_bridge_cannot_use_is_named_not_left_starting(self):
        """A registered modem with a working bearer showed "Cellular modem is starting"
        forever when the card refused the bridge's logical channels."""
        with tempfile.TemporaryDirectory() as temp:
            app = HardwareSupervisor(data_path=Path(temp))
            modem = {"id": "2c7c-0125-port", "tty": "/dev/ttyUSB2",
                     "usb_path": "1-3", "vid": "2c7c", "pid": "0125"}
            app.cellular_states = {modem["id"]: {"available": True, "data_active": False}}
            app.publish_control_state([modem], set(), {
                modem["id"]: "MANAGE CHANNEL OPEN failed: 006a81"})
            observed = json.loads((Path(temp) / "orchestrator" / "devices-status.json")
                                  .read_text())["devices"][modem["id"]]
            self.assertIn("006a81", observed["error"])
            self.assertNotIn("starting", observed["error"])
            self.assertFalse(observed["transitioning"])
            self.assertFalse(observed["actual"]["vowifi_bridge_active"])

    def test_snapshot_reports_the_sim_iccid_and_number(self):
        """Control shows "no SIM" unless the VPCD reader or this ICCID says otherwise, so a
        container build without it lost the SIM whenever the bridge was down."""
        obj = "/org/freedesktop/ModemManager1/Modem/1"
        modem_detail = ("modem.generic.state : registered\n"
                        "modem.generic.power-state : on\n"
                        "modem.generic.sim : /org/freedesktop/ModemManager1/SIM/1\n"
                        "modem.generic.own-numbers.value[1] : +86 130 0000 0000\n"
                        "modem.generic.ports.value[1] : ttyUSB2 (at)\n")
        sim_detail = "sim.properties.iccid : 89860100000000000000\n"
        app = HardwareSupervisor()

        def command(*args):
            if args[:2] == ("mmcli", "-i"):
                return Mock(returncode=0, stdout=sim_detail)
            return Mock(returncode=0, stdout=modem_detail)

        app.command = command
        snapshot = app.modem_snapshot({"id": "modem-a", "tty": "/dev/ttyUSB2"}, [obj])
        self.assertEqual(snapshot["sim_iccid"], "89860100000000000000")
        self.assertEqual(snapshot["msisdn"], "+8613000000000")

    def test_an_unreadable_iccid_is_unknown_not_an_identity(self):
        self.assertEqual(HardwareSupervisor.normalize_iccid("--"), "")
        self.assertEqual(HardwareSupervisor.normalize_iccid("1234"), "")

    def test_container_hardware_publishes_redacted_support_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            app = HardwareSupervisor(data_path=data)
            modem = {"id": "2c7c-0125-port", "tty": "/dev/ttyUSB2",
                     "usb_path": "1-3", "vid": "2c7c", "pid": "0125",
                     "base_port": 15360}
            process = Mock(pid=42)
            process.poll.return_value = None
            app.bridges = {modem["id"]: process}
            app.modemmanager = Mock()
            app.modemmanager.poll.return_value = None
            identity = data / "modems" / f'{modem["id"]}.json'
            identity.parent.mkdir(parents=True)
            identity.write_text(json.dumps({
                "imei": "350000000000036", "iccid": "8900000000000000022",
                "updated_at": int(__import__("time").time()),
                "channel_status": "ready", "channel_requested": 3,
                "channel_allocated": 3}))
            app.listening_tcp_ports = Mock(return_value={15360, 15361, 15362})

            app.publish_host_diagnostics(
                [modem], ["/org/freedesktop/ModemManager1/Modem/0"])

            path = data / "orchestrator" / "host-diagnostics.json"
            diagnostic = json.loads(path.read_text())
            bridge = diagnostic["bridges"][modem["id"]]
            self.assertTrue(bridge["channels_ready"])
            self.assertTrue(bridge["imei_valid"])
            self.assertTrue(bridge["iccid_valid"])
            self.assertEqual(diagnostic["virtualization"], "docker")
            self.assertNotIn("350000000000036", path.read_text())
            self.assertNotIn("8900000000000000022", path.read_text())

    def test_kernel_objects_are_restricted_to_modem_names(self):
        class Root:
            def __init__(self, names):
                self.names = names
            def glob(self, _):
                return [Path(name) for name in self.names]
        roots = (("tty", Root(["ttyUSB2", "tty0", "ttyACM1"]),
                  re.compile(r"tty(?:USB|ACM)\d+")),)
        with patch("runtime.hardware.EVENT_ROOTS", roots):
            self.assertEqual(kernel_objects(), {("tty", "ttyUSB2"), ("tty", "ttyACM1")})

    def test_reconcile_reports_add_remove_and_publishes_no_identifiers(self):
        with tempfile.TemporaryDirectory() as temp:
            app = HardwareSupervisor(Path(temp) / "status.json", data_path=Path(temp))
            app.reported = {("tty", "ttyUSB9")}
            events = []
            app.report_event = lambda action, subsystem, name: events.append((action, subsystem, name))
            app.command = Mock(return_value=Mock(
                returncode=0,
                stdout="/org/freedesktop/ModemManager1/Modem/0\n"
                       "/org/freedesktop/ModemManager1/Modem/0\n"))
            app.discover_modems = Mock(return_value=[])
            app.reconcile_pcsc = Mock()
            with patch("runtime.hardware.kernel_objects", return_value={
                    ("tty", "ttyUSB2"), ("usbmisc", "cdc-wdm0"), ("net", "wwan0")}):
                app.reconcile()
            self.assertEqual(events[0], ("remove", "tty", "ttyUSB9"))
            self.assertEqual(set(events[1:]), {
                ("add", "tty", "ttyUSB2"), ("add", "usbmisc", "cdc-wdm0"),
                ("add", "net", "wwan0")})
            status = json.loads(app.status_path.read_text())
            self.assertEqual(status["modem_count"], 1)
            self.assertEqual(status["modems"], ["/org/freedesktop/ModemManager1/Modem/0"])
            self.assertEqual(status["hardware_count"], 0)
            self.assertEqual(status["pcsc_reader_count"], 0)
            self.assertEqual(status["ready_bridge_count"], 0)
            self.assertNotIn("imei", app.status_path.read_text().lower())

    def test_modem_object_is_matched_by_owned_tty(self):
        app = HardwareSupervisor()
        details = {
            "/org/freedesktop/ModemManager1/Modem/0": "modem.generic.ports : ttyUSB9 (at)",
            "/org/freedesktop/ModemManager1/Modem/1": "modem.generic.ports : ttyUSB2 (at)",
        }
        app.command = Mock(side_effect=lambda *args: Mock(
            returncode=0, stdout=details[args[2]]))
        self.assertEqual(app.modem_object_for_tty(
            "/dev/ttyUSB2", list(details)), "/org/freedesktop/ModemManager1/Modem/1")

    def test_one_pass_reads_each_modem_once(self):
        """A reconcile pass used to run `mmcli -m ... --output-keyvalue` four times for a
        single modem: once to match the tty, once per snapshot, and once more at the end."""
        obj = "/org/freedesktop/ModemManager1/Modem/0"
        detail = ("modem.generic.state : connected\n"
                  "modem.generic.power-state : on\n"
                  "modem.3gpp.registration-state : home\n"
                  "modem.generic.primary-port : cdc-wdm0\n"
                  "modem.generic.ports.value[1] : ttyUSB2 (at)\n"
                  "modem.generic.ports.value[2] : wwan0 (net)\n")
        app = HardwareSupervisor()
        app.assert_networkmanager_isolated = Mock()
        app.desired_devices = Mock(return_value={
            "modem-a": {"cellular_enabled": True, "vowifi_enabled": True,
                        "flight_mode": False}})
        calls = []

        def command(*args):
            calls.append(list(args))
            if args[0] == "mmcli":
                return Mock(returncode=0, stdout=detail)
            return Mock(returncode=0, stdout="")

        app.command = command
        app.reconcile_cellular([{"id": "modem-a", "tty": "/dev/ttyUSB2"}], [obj])

        reads = [call for call in calls
                 if call[:2] == ["mmcli", "-m"] and "--output-keyvalue" in call]
        self.assertEqual(len(reads), 1, reads)
        self.assertTrue(app.cellular_states["modem-a"]["data_active"])

    def test_a_command_that_changes_the_modem_drops_the_cached_read(self):
        app = HardwareSupervisor()
        app.command = Mock(return_value=Mock(returncode=0, stdout="modem.generic.state : on"))

        app.mmcli_keyvalue("-m", "/modem/0")
        app.mmcli_keyvalue("-m", "/modem/0")
        self.assertEqual(app.command.call_count, 1)

        app.forget_mmcli_details()
        app.mmcli_keyvalue("-m", "/modem/0")
        self.assertEqual(app.command.call_count, 2)

    def test_rejected_event_fails_closed(self):
        app = HardwareSupervisor()
        app.command = Mock(return_value=Mock(returncode=1, stdout="denied"))
        with self.assertRaisesRegex(RuntimeError, "rejected add event"):
            app.report_event("add", "tty", "ttyUSB2")

    def test_networkmanager_refuses_to_continue_if_it_claims_a_nas_interface(self):
        app = HardwareSupervisor()
        app.command = Mock(return_value=Mock(
            returncode=0, stdout="wwan0:disconnected\novs_eth0:connected\nlo:unmanaged\n"))
        with self.assertRaisesRegex(RuntimeError, "ovs_eth0"):
            app.assert_networkmanager_isolated()

    def test_cellular_profile_can_never_autoconnect_or_become_default(self):
        app = HardwareSupervisor()
        calls = []

        def command(*args, **_kwargs):
            calls.append(list(args))
            if args[:3] == ("nmcli", "connection", "show"):
                return Mock(returncode=1, stdout="")
            return Mock(returncode=0, stdout="")

        app.command = command
        app.ensure_modem_data(
            {"id": "modem-a"},
            {"powered": True, "data_active": False, "registration": "home",
             "primary_port": "cdc-wdm0", "apn": "internet"})

        add = next(call for call in calls if call[:3] == ["nmcli", "connection", "add"])
        self.assertEqual(add[add.index("connection.autoconnect") + 1], "no")
        self.assertEqual(add[add.index("ipv4.never-default") + 1], "yes")
        self.assertEqual(add[add.index("ipv6.never-default") + 1], "yes")
        self.assertIn(["nmcli", "connection", "up", app.cellular_profile_name("modem-a")],
                      calls)

    def test_at_only_modem_with_qmi_kernel_ports_is_reset_for_recovery(self):
        app = HardwareSupervisor()
        app.assert_networkmanager_isolated = Mock()
        app.desired_devices = Mock(return_value={
            "modem-a": {"cellular_enabled": True, "vowifi_enabled": True,
                        "flight_mode": False}})
        app.modem_snapshot = Mock(return_value={
            "available": True, "mm_object": "/org/freedesktop/ModemManager1/Modem/0",
            "network_interface": "", "registration": "roaming", "data_active": False})
        app.command = Mock(return_value=Mock(returncode=0, stdout=""))
        app.terminate_qmi_proxy = Mock()
        fake_paths = [Mock()]
        fake_paths[0].exists.return_value = True
        with patch("runtime.hardware.Path.glob", return_value=fake_paths):
            app.reconcile_cellular([{"id": "modem-a", "tty": "/dev/ttyUSB2"}],
                                   ["/org/freedesktop/ModemManager1/Modem/0"])
        app.command.assert_called_once_with(
            "mmcli", "-m", "/org/freedesktop/ModemManager1/Modem/0", "--reset")
        app.terminate_qmi_proxy.assert_called_once_with()

    def test_qmi_recovery_is_not_suppressed_during_the_first_minutes_of_uptime(self):
        """The rate limit must not treat "never reset" as "reset at monotonic zero".

        A freshly booted host reports a small monotonic clock, and a restarted Hardware
        container is exactly when a stale QMI session needs the reset.
        """
        app = HardwareSupervisor()
        app.assert_networkmanager_isolated = Mock()
        app.desired_devices = Mock(return_value={
            "modem-a": {"cellular_enabled": True, "vowifi_enabled": True,
                        "flight_mode": False}})
        app.modem_snapshot = Mock(return_value={
            "available": True, "mm_object": "/org/freedesktop/ModemManager1/Modem/0",
            "network_interface": "", "registration": "roaming", "data_active": False})
        app.command = Mock(return_value=Mock(returncode=0, stdout=""))
        app.terminate_qmi_proxy = Mock()
        fake_paths = [Mock()]
        fake_paths[0].exists.return_value = True

        with patch("runtime.hardware.Path.glob", return_value=fake_paths), \
                patch("runtime.hardware.time.monotonic", return_value=5.0):
            app.reconcile_cellular([{"id": "modem-a", "tty": "/dev/ttyUSB2"}],
                                   ["/org/freedesktop/ModemManager1/Modem/0"])

        app.command.assert_called_once_with(
            "mmcli", "-m", "/org/freedesktop/ModemManager1/Modem/0", "--reset")
        app.terminate_qmi_proxy.assert_called_once_with()

    def unclaimed_modem_app(self):
        """A supervisor whose one modem is present but has no ModemManager object."""
        app = HardwareSupervisor()
        app.assert_networkmanager_isolated = Mock()
        app.desired_devices = Mock(return_value={
            "modem-a": {"cellular_enabled": True, "vowifi_enabled": True,
                        "flight_mode": False}})
        app.modem_snapshot = Mock(return_value={
            "available": False, "registration": "unknown", "data_active": False})
        app.command = Mock(return_value=Mock(returncode=0, stdout=""))
        app.ensure_modem_data = Mock(return_value=False)
        app.disconnect_modem_data = Mock(return_value=False)
        app.log = Mock()
        return app

    def pass_at(self, app, now, port_class):
        with patch("runtime.hardware.Path.glob", return_value=[]), \
                patch("runtime.hardware.time.monotonic", return_value=now), \
                patch("runtime.hardware.ATPort", port_class):
            app.reconcile_cellular([{"id": "modem-a", "tty": "/dev/ttyUSB2"}], [])

    @staticmethod
    def fake_port():
        port_class = MagicMock()
        port = port_class.return_value.__enter__.return_value
        port.read.return_value = b"\r\nOK\r\n"
        return port_class, port

    def test_unclaimed_modem_is_left_alone_while_modemmanager_may_still_probe_it(self):
        app = self.unclaimed_modem_app()
        port_class, _port = self.fake_port()

        self.pass_at(app, 5.0, port_class)
        self.pass_at(app, 5.0 + 119, port_class)

        port_class.assert_not_called()
        self.assertEqual(app.unclaimed_since, {"modem-a": 5.0})

    def test_modem_modemmanager_gave_up_on_is_reset_through_its_at_port(self):
        """The EC25 whose QMI port timed out at enumeration: no object, 4G never came up."""
        app = self.unclaimed_modem_app()
        port_class, port = self.fake_port()
        app.forget_mmcli_details = Mock()

        self.pass_at(app, 5.0, port_class)
        self.pass_at(app, 5.0 + 120, port_class)

        port_class.assert_called_once()
        self.assertEqual(port_class.call_args.args[0], "/dev/ttyUSB2")
        self.assertFalse(port_class.call_args.kwargs["exclusive"])
        port.write.assert_called_once_with(b"AT+CFUN=1,1\r")
        app.forget_mmcli_details.assert_called()
        # The reset re-enumerates the modem; there is nothing to configure on this pass.
        app.ensure_modem_data.assert_called_once()

    def test_unclaimed_modem_reset_is_rate_limited(self):
        app = self.unclaimed_modem_app()
        port_class, port = self.fake_port()

        self.pass_at(app, 5.0, port_class)
        self.pass_at(app, 125.0, port_class)
        # Still unclaimed after the reset: the grace period elapses again, but the last
        # reset was under five minutes ago.
        self.pass_at(app, 250.0, port_class)
        self.pass_at(app, 424.0, port_class)
        self.assertEqual(port.write.call_count, 1)

        self.pass_at(app, 425.0, port_class)
        self.assertEqual(port.write.call_count, 2)

    def test_grace_clock_clears_once_modemmanager_claims_the_modem(self):
        app = self.unclaimed_modem_app()
        port_class, _port = self.fake_port()

        self.pass_at(app, 5.0, port_class)
        self.assertIn("modem-a", app.unclaimed_since)

        app.modem_snapshot.return_value = {
            "available": True, "mm_object": "/org/freedesktop/ModemManager1/Modem/0",
            "network_interface": "wwan0", "radio_enabled": True,
            "registration": "home", "data_active": True}
        self.pass_at(app, 60.0, port_class)
        self.assertNotIn("modem-a", app.unclaimed_since)

        # Losing the object again starts a new grace period rather than resuming the old one.
        app.modem_snapshot.return_value = {
            "available": False, "registration": "unknown", "data_active": False}
        self.pass_at(app, 130.0, port_class)
        port_class.assert_not_called()
        self.assertEqual(app.unclaimed_since, {"modem-a": 130.0})

    def test_grace_clock_clears_when_the_modem_disappears(self):
        app = self.unclaimed_modem_app()
        port_class, _port = self.fake_port()

        self.pass_at(app, 5.0, port_class)
        with patch("runtime.hardware.Path.glob", return_value=[]):
            app.reconcile_cellular([], [])
        self.assertEqual(app.unclaimed_since, {})

    def test_a_serial_error_during_the_reset_does_not_escape_reconcile(self):
        import serial

        app = self.unclaimed_modem_app()
        for error in (serial.SerialException("could not open port /dev/ttyUSB2"),
                      OSError(71, "Protocol error"), RuntimeError("unexpected")):
            with self.subTest(error=type(error).__name__):
                app.unclaimed_since.clear()
                app.unclaimed_reset_at.clear()
                app.log.reset_mock()
                port_class = Mock(side_effect=error)

                self.pass_at(app, 5.0, port_class)
                self.pass_at(app, 125.0, port_class)

                port_class.assert_called_once()
                self.assertIn("failed", app.log.call_args.args[0])
                # A failed attempt still counts against the rate limit.
                self.pass_at(app, 250.0, port_class)
                port_class.assert_called_once()

    def test_a_port_that_vanishes_after_the_write_still_counts_as_a_reset(self):
        """CFUN=1,1 drops the modem off the bus, which can fail the read or the close."""
        app = self.unclaimed_modem_app()
        app.forget_mmcli_details = Mock()
        port_class, port = self.fake_port()
        port.read.side_effect = OSError(5, "Input/output error")

        self.pass_at(app, 5.0, port_class)
        self.pass_at(app, 125.0, port_class)

        port.write.assert_called_once_with(b"AT+CFUN=1,1\r")
        app.forget_mmcli_details.assert_called()
        self.assertIn("sent", app.log.call_args.args[0])

    def test_at_port_tolerates_missing_modem_control_lines(self):
        import errno
        import serial

        from runtime.hardware import ATPort

        for code in (errno.EPROTO, errno.ENOTTY):
            with patch.object(serial.Serial, "_update_dtr_state",
                              side_effect=OSError(code, "control")), \
                    patch.object(serial.Serial, "_update_rts_state",
                                 side_effect=OSError(code, "control")):
                port = ATPort.__new__(ATPort)
                port._update_dtr_state()
                port._update_rts_state()
        with patch.object(serial.Serial, "_update_dtr_state",
                          side_effect=OSError(errno.EIO, "io")):
            with self.assertRaises(OSError):
                ATPort.__new__(ATPort)._update_dtr_state()



class ModemProfileTests(unittest.TestCase):
    """Containers read the profiles from config.yaml; they used to look for a config.json no
    container deployment has, so every modem was called "Cellular modem"."""

    def test_without_a_configuration_the_default_profile_names_the_dji_module(self):
        with tempfile.TemporaryDirectory() as temp:
            app = HardwareSupervisor(interval=0, data_path=Path(temp))
            self.assertEqual(app.modem_profiles(), [("2c7c", "0125", 2, "DJI/Quectel EC25")])

    def test_configured_profiles_come_from_config_yaml_with_their_names(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            (data / "config.yaml").write_text(
                "hardware:\n  modem_profiles:\n"
                "    - {name: Quectel EG25-G, vid: 2C7C, pid: '0125', at_interface: 3}\n"
                "    - {vid: 1e0e, pid: '9001'}\n")
            app = HardwareSupervisor(interval=0, data_path=data)
            self.assertEqual(app.modem_profiles(), [("2c7c", "0125", 3, "Quectel EG25-G"),
                                                    ("1e0e", "9001", 2, "USB modem")])

    def test_a_configuration_without_profiles_keeps_the_default(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            (data / "config.yaml").write_text("settings: {}\n")
            app = HardwareSupervisor(interval=0, data_path=data)
            self.assertEqual(app.modem_profiles()[0][3], "DJI/Quectel EC25")

    def test_an_unreadable_configuration_keeps_the_default(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            (data / "config.yaml").write_text("hardware: [unclosed\n")
            app = HardwareSupervisor(interval=0, data_path=data)
            self.assertEqual(app.modem_profiles()[0][3], "DJI/Quectel EC25")

    def test_the_file_is_parsed_once_until_it_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            (data / "config.yaml").write_text("settings: {}\n")
            app = HardwareSupervisor(interval=0, data_path=data)
            import yaml
            with patch.object(yaml, "load", wraps=yaml.load) as load:
                app.modem_profiles()
                app.modem_profiles()
            self.assertEqual(load.call_count, 1)


class AdaptiveCadenceTests(unittest.TestCase):
    """A pass forks mmcli/nmcli per modem. At a fixed 3 s cadence that was about a fifth of a
    Raspberry Pi core for a modem nobody was touching."""

    def run_wait(self, app, signatures):
        clock = [0.0]
        with patch("runtime.hardware.time.monotonic", side_effect=lambda: clock[0]), \
                patch("runtime.hardware.time.sleep",
                      side_effect=lambda s: clock.__setitem__(0, clock[0] + s)), \
                patch.object(app, "wake_signature", side_effect=signatures):
            app.wait_for_next_pass()
        return clock[0]

    def test_a_settled_plane_waits_the_idle_interval(self):
        from runtime import hardware
        app = HardwareSupervisor(interval=3.0)
        app.settled = True
        waited = self.run_wait(app, lambda: "same")
        self.assertGreaterEqual(waited, hardware.IDLE_INTERVAL - 0.11)
        self.assertLess(waited, hardware.IDLE_INTERVAL + 0.2)

    def test_a_change_wakes_the_next_pass_at_once(self):
        app = HardwareSupervisor(interval=3.0)
        app.settled = True
        calls = iter(["before", "before", "after"])
        waited = self.run_wait(app, lambda: next(calls, "after"))
        self.assertLess(waited, 1.2)

    def test_work_in_flight_keeps_the_fast_cadence(self):
        app = HardwareSupervisor(interval=3.0)
        app.settled = False
        signature = Mock(return_value="same")
        clock = [0.0]
        with patch("runtime.hardware.time.monotonic", side_effect=lambda: clock[0]), \
                patch("runtime.hardware.time.sleep",
                      side_effect=lambda s: clock.__setitem__(0, clock[0] + s)), \
                patch.object(app, "wake_signature", signature):
            app.wait_for_next_pass()
        self.assertLess(clock[0], 3.2)
        signature.assert_not_called()

    def test_stopping_ends_the_wait(self):
        app = HardwareSupervisor(interval=3.0)
        app.settled = True
        app.stop = True
        self.assertEqual(self.run_wait(app, lambda: "same"), 0.0)

    def test_the_signature_sees_a_new_kernel_device_and_a_bridge_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            app = HardwareSupervisor(data_path=Path(temp))
            process = Mock(); process.poll.return_value = None
            app.bridges = {"modem-a": process}
            with patch("runtime.hardware.kernel_objects", return_value={("tty", "ttyUSB2")}):
                before = app.wake_signature()
            with patch("runtime.hardware.kernel_objects",
                       return_value={("tty", "ttyUSB2"), ("tty", "ttyUSB3")}):
                self.assertNotEqual(app.wake_signature(), before)
            process.poll.return_value = 1
            with patch("runtime.hardware.kernel_objects", return_value={("tty", "ttyUSB2")}):
                self.assertNotEqual(app.wake_signature(), before)

    def test_the_idle_interval_leaves_room_for_the_health_check(self):
        from runtime import hardware
        # Dockerfile.hardware fails the check when status.json is older than 15 s.
        self.assertLessEqual(hardware.IDLE_INTERVAL + 5, 15)


if __name__ == "__main__":
    unittest.main()
