import json
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


class DeviceStateTests(unittest.TestCase):
    def test_native_reader_is_stable_and_never_collides_with_modem_vpcd_slots(self):
        cards = [
            {"name": "USB Smart Card Reader 00 00", "reader_port": "3-2",
             "hardware_kind": "reader", "present": True},
            {"name": "VPCD modem slot", "hardware_kind": "modem", "present": True},
        ]
        first = device_state.native_reader_devices(cards)
        second = device_state.native_reader_devices(list(reversed(cards)))
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertTrue(next(iter(first)).startswith("reader-"))

    def test_native_reader_vowifi_state_is_independent_of_cellular(self):
        self.assertEqual(device_state.native_vowifi_capability(False, False, None)["actual"], "off")
        self.assertEqual(device_state.native_vowifi_capability(True, True, {"state": "OK"})["actual"], "on")
        self.assertEqual(device_state.native_vowifi_capability(True, False, None)["actual"], "degraded")

    def test_logical_channel_view_is_bounded_and_preserves_roles(self):
        value = device_state.logical_channel_view({
            "channel_capacity": 3, "channel_allocated": 3, "channel_status": "ready",
            "logical_channels": [
                {"slot": 0, "channel": 1, "role": "pin"},
                {"slot": 1, "channel": 2, "role": "swu"},
                {"slot": 2, "channel": 3, "role": "ims"},
                {"slot": 3, "channel": 4, "role": "invalid"},
            ],
        }, True)
        self.assertEqual(value["allocated"], 3)
        self.assertEqual(value["capacity"], 3)
        self.assertEqual(len(value["items"]), 3)
        self.assertEqual(value["items"][1]["role"], "swu")

    def test_legacy_channel_metadata_uses_bridge_state_without_inventing_ids(self):
        value = device_state.logical_channel_view({"slots": 3}, True)
        self.assertEqual(value["status"], "ready")
        self.assertEqual(value["allocated"], 3)
        self.assertEqual(value["items"], [])

    def test_stale_ready_channel_metadata_cannot_claim_a_stopped_bridge(self):
        value = device_state.logical_channel_view({
            "channel_status": "ready", "channel_allocated": 3,
            "logical_channels": [{"slot": 0, "channel": 1, "role": "pin"}],
        }, False)
        self.assertEqual(value["status"], "stopped")
        self.assertEqual(value["allocated"], 0)
        self.assertEqual(value["items"], [])

    def test_one_device_update_preserves_other_device(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.multiple(device_state, ROOT=str(root),
                                DESIRED=str(root / "desired.json"),
                                STATUS=str(root / "status.json")):
                device_state.set_desired("modem-a", cellular_enabled=True,
                                         vowifi_enabled=False)
                device_state.set_desired("modem-b", vowifi_enabled=True)
                value = device_state.desired()["devices"]
                self.assertEqual(value["modem-a"], {
                    "cellular_enabled": True, "vowifi_enabled": False,
                    "flight_mode": False})
                self.assertEqual(value["modem-b"], {
                    "cellular_enabled": False, "vowifi_enabled": True,
                    "flight_mode": False})

    def test_new_device_defaults_are_persisted_without_changing_existing_devices(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.multiple(device_state, ROOT=str(root),
                                DESIRED=str(root / "desired.json"),
                                STATUS=str(root / "status.json")):
                device_state.set_desired("existing", cellular_enabled=False, vowifi_enabled=True)
                device_state.set_defaults(cellular_enabled=True, vowifi_enabled=False)
                value = device_state.desired()
                self.assertEqual(value["defaults"], {
                    "cellular_enabled": True, "vowifi_enabled": False,
                    "flight_mode": False})
                self.assertEqual(value["devices"]["existing"], {
                    "cellular_enabled": False, "vowifi_enabled": True,
                    "flight_mode": False})

    def test_hardware_imei_is_stored_per_device_and_never_in_capability_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.multiple(device_state, ROOT=str(root),
                                DESIRED=str(root / "desired.json"),
                                STATUS=str(root / "status.json"),
                                HARDWARE=str(root / "hardware.json")):
                device_state.set_desired("reader-a", vowifi_enabled=True)
                value = device_state.set_hardware("reader-a", {
                    "device_type": "reader", "imei": "490154203237518"})
                self.assertEqual(value["imei"], "490154203237518")
                self.assertNotIn("imei", device_state.desired()["devices"]["reader-a"])
                self.assertEqual(device_state.hardware()["reader-a"]["device_type"], "reader")

    def test_vpcd_reader_name_preserves_modem_identity_without_metadata(self):
        name = "VoWiFi Modem 2c7c-0125-1-1.2 00 03"
        self.assertEqual(device_state.vpcd_modem_hardware_id(name), "2c7c-0125-1-1.2")
        self.assertEqual(device_state.vpcd_modem_hardware_id(
            "SCR Prime CCID Reader (000000000001) 00 00"), "")

    def test_the_packaged_virtual_pcd_endpoint_is_never_a_device(self):
        """The vsmartcard package's own reader definition rendered as two phantom devices
        that exist whether or not any hardware does. The installer disables the file, but a
        package reinstall can restore it — the device list must not trust that."""
        cards = [
            {"name": "Virtual PCD 00 00", "hardware_kind": "reader", "present": True},
            {"name": "Virtual PCD 00 01", "hardware_kind": "reader", "present": True},
            {"name": "SCR Prime CCID Reader (000000000001) 00 00",
             "hardware_kind": "reader", "reader_port": "1-1.5"},
        ]
        readers = device_state.native_reader_devices(cards)
        self.assertEqual(len(readers), 1)
        self.assertEqual(next(iter(readers.values()))["reader_port"], "1-1.5")

    def test_native_readers_exclude_orchestrator_vpcd_slots(self):
        cards = [
            {"name": "VoWiFi Modem 2c7c-0125-1-1.2 00 00", "hardware_kind": "reader"},
            {"name": "SCR Prime CCID Reader (000000000001) 00 00",
             "hardware_kind": "reader", "reader_port": "1-1.5"},
        ]
        readers = device_state.native_reader_devices(cards)
        self.assertEqual(len(readers), 1)
        self.assertEqual(next(iter(readers.values()))["reader_port"], "1-1.5")

    def test_forgetting_hardware_and_preferences_is_scoped_to_one_device(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.multiple(device_state, ROOT=str(root),
                                DESIRED=str(root / "desired.json"),
                                STATUS=str(root / "status.json"),
                                HARDWARE=str(root / "hardware.json")):
                for device_id in ("a", "b"):
                    device_state.set_desired(device_id, vowifi_enabled=True)
                    device_state.set_hardware(device_id, {"device_type": "reader"})
                self.assertTrue(device_state.remove_desired("a"))
                self.assertTrue(device_state.remove_hardware("a"))
                self.assertNotIn("a", device_state.desired()["devices"])
                self.assertNotIn("a", device_state.hardware())
                self.assertIn("b", device_state.desired()["devices"])
                self.assertIn("b", device_state.hardware())

    def test_all_eight_per_device_capability_combinations(self):
        cases = (
            # flight, cellular preference, VoWiFi, RF, effective data, bridge
            (False, False, False, True, False, False),
            (False, False, True,  True, False, True),
            (False, True,  False, True, True,  False),
            (False, True,  True,  True, True,  True),
            (True,  False, False, False, False, False),
            (True,  False, True,  False, False, True),
            (True,  True,  False, False, False, False),
            (True,  True,  True,  False, False, True),
        )
        for flight, cellular, vowifi, radio, data, line in cases:
            with self.subTest(flight=flight, cellular=cellular, vowifi=vowifi):
                plan = Orchestrator.device_capability_plan({
                    "flight_mode": flight, "cellular_enabled": cellular,
                    "vowifi_enabled": vowifi})
                self.assertEqual(plan["radio_enabled"], radio)
                self.assertEqual(plan["cellular_data_requested"], cellular)
                self.assertEqual(plan["cellular_data_enabled"], data)
                self.assertEqual(plan["vowifi_line_enabled"], line)

                aggregate = Orchestrator.capability_plan({"m": {
                    "flight_mode": flight, "cellular_enabled": cellular,
                    "vowifi_enabled": vowifi}})
                self.assertTrue(aggregate["cellular_backend_required"])
                self.assertEqual(aggregate["country_egress_required"], vowifi)
                self.assertEqual(aggregate["vowifi_devices"], ["m"] if line else [])
                self.assertEqual(aggregate["effective_cellular_devices"], ["m"] if data else [])
                self.assertEqual(aggregate["flight_mode_devices"], ["m"] if flight else [])
                self.assertEqual(aggregate["radio_enabled_devices"], ["m"] if radio else [])

    def test_native_reader_line_keeps_country_egress_without_a_usb_modem(self):
        empty_modem_plan = Orchestrator.capability_plan({})
        self.assertTrue(Orchestrator.country_egress_required({
            "lines": [{"id": "reader-line", "enabled": True}]
        }, empty_modem_plan))
        self.assertFalse(Orchestrator.country_egress_required({
            "lines": [{"id": "reader-line", "enabled": False}]
        }, empty_modem_plan))

    def test_any_cellular_device_forces_all_vowifi_bridges_through_mm(self):
        plan = Orchestrator.capability_plan({
            "cellular-only": {"cellular_enabled": True, "vowifi_enabled": False},
            "vowifi-only": {"cellular_enabled": False, "vowifi_enabled": True},
        })
        self.assertTrue(plan["vowifi_through_modemmanager"])
        self.assertEqual(plan["vowifi_devices"], ["vowifi-only"])

    def test_multi_modem_effective_states_remain_independent(self):
        plan = Orchestrator.capability_plan({
            "flight-vowifi": {"flight_mode": True, "cellular_enabled": True,
                              "vowifi_enabled": True},
            "cellular-only": {"flight_mode": False, "cellular_enabled": True,
                              "vowifi_enabled": False},
        })
        self.assertEqual(plan["cellular_devices"], ["cellular-only", "flight-vowifi"])
        self.assertEqual(plan["effective_cellular_devices"], ["cellular-only"])
        self.assertEqual(plan["flight_mode_devices"], ["flight-vowifi"])
        self.assertEqual(plan["vowifi_devices"], ["flight-vowifi"])

    def test_cellular_profile_is_stable_and_unique_per_physical_device(self):
        first = Orchestrator.cellular_profile_name("2c7c-0125-1-1.2")
        self.assertEqual(first, Orchestrator.cellular_profile_name("2c7c-0125-1-1.2"))
        self.assertNotEqual(first, Orchestrator.cellular_profile_name("2c7c-0125-1-1.3"))
        self.assertTrue(first.startswith("mdd-cell-"))

    def test_native_cellular_plan_scales_per_physical_modem(self):
        plan = Orchestrator.capability_plan({
            "modem-a": {"cellular_enabled": True, "vowifi_enabled": False},
            "modem-b": {"cellular_enabled": True, "vowifi_enabled": True},
        })
        self.assertTrue(plan["cellular_backend_required"])
        self.assertEqual(plan["cellular_devices"], ["modem-a", "modem-b"])

    def test_modem_snapshot_is_scoped_to_its_mm_object_and_bearer(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp) / "data", Path(temp), dry_run=True)
            modem_detail = """modem.generic.primary-port : cdc-wdm1
modem.generic.sim : /org/freedesktop/ModemManager1/SIM/7
modem.generic.own-numbers.value[1] : +1 (202) 555-0100
modem.generic.ports.value[1] : cdc-wdm1 (qmi)
modem.generic.ports.value[2] : wwan1 (net)
modem.generic.state : connected
modem.generic.power-state : on
modem.generic.signal-quality.value : 77
modem.3gpp.operator-name : Example
modem.3gpp.registration-state : roaming
modem.generic.bearers.value[1] : /org/freedesktop/ModemManager1/Bearer/9
"""
            bearer = """bearer.status.connected : yes
bearer.properties.apn : internet
bearer.ipv4-config.address : 10.9.0.2
bearer.stats.rx-bytes : 123
bearer.stats.tx-bytes : 456
"""
            def fake_run(args, **_kwargs):
                if args[:2] == ["mmcli", "-m"]:
                    return SimpleNamespace(returncode=0, stdout=modem_detail, stderr="")
                if args[:2] == ["mmcli", "-i"]:
                    return SimpleNamespace(returncode=0,
                                           stdout="sim.properties.iccid : 8901000000000000001\n",
                                           stderr="")
                if args[:2] == ["mmcli", "-b"]:
                    return SimpleNamespace(returncode=0, stdout=bearer, stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            with patch.object(app, "modemmanager_modem_for_tty",
                              return_value="/org/freedesktop/ModemManager1/Modem/4"), patch(
                                  "host.mdd_orchestrator.run", side_effect=fake_run):
                value = app.modem_snapshot({"id": "modem-b", "tty": "/dev/ttyUSB6"})
            self.assertTrue(value["data_active"])
            self.assertTrue(value["radio_enabled"])
            self.assertEqual(value["primary_port"], "cdc-wdm1")
            self.assertEqual(value["network_interface"], "wwan1")
            self.assertEqual(value["apn"], "internet")
            self.assertEqual(value["rx_bytes"], 123)
            self.assertEqual(value["msisdn"], "+12025550100")
            self.assertEqual(value["sim_iccid"], "8901000000000000001")

    def test_modem_number_normalization_rejects_placeholders_and_status_text(self):
        self.assertEqual(Orchestrator.normalize_msisdn("--"), "")
        self.assertEqual(Orchestrator.normalize_msisdn("not available"), "")
        self.assertEqual(Orchestrator.normalize_msisdn("+44 7700-900123"), "+447700900123")

    def test_modem_snapshot_retains_apn_from_disconnected_bearer(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp) / "data", Path(temp), dry_run=True)
            modem_detail = """modem.generic.primary-port : cdc-wdm0
modem.generic.state : registered
modem.generic.power-state : on
modem.3gpp.registration-state : home
modem.generic.bearers.value[1] : /org/freedesktop/ModemManager1/Bearer/1
"""
            bearer = """bearer.status.connected : no
bearer.properties.apn : carrier-apn
"""

            def fake_run(args, **_kwargs):
                value = bearer if args[:2] == ["mmcli", "-b"] else modem_detail
                return SimpleNamespace(returncode=0, stdout=value, stderr="")

            with patch.object(app, "modemmanager_modem_for_tty", return_value="0"), patch(
                    "host.mdd_orchestrator.run", side_effect=fake_run):
                value = app.modem_snapshot({"id": "modem-a", "tty": "/dev/ttyUSB2"})
            self.assertFalse(value["data_active"])
            self.assertTrue(value["radio_enabled"])
            self.assertEqual(value["apn"], "carrier-apn")

    def test_modemmanager_disabled_state_is_flight_mode_even_if_hardware_power_is_on(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp) / "data", Path(temp), dry_run=True)
            detail = """modem.generic.primary-port : cdc-wdm0
modem.generic.state : disabled
modem.generic.power-state : on
modem.3gpp.registration-state : unknown
"""
            with patch.object(app, "modemmanager_modem_for_tty", return_value="0"), patch(
                    "host.mdd_orchestrator.run",
                    return_value=SimpleNamespace(returncode=0, stdout=detail, stderr="")):
                value = app.modem_snapshot({"id": "modem-a", "tty": "/dev/ttyUSB2"})
            self.assertTrue(value["powered"])
            self.assertFalse(value["radio_enabled"])

    def test_missing_device_state_gets_safe_native_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=True)
            devices, created = app.desired_devices([{"id": "modem-a"}])
            self.assertTrue(created)
            self.assertEqual(devices["modem-a"], {
                "cellular_enabled": False, "vowifi_enabled": True,
                "flight_mode": False})
            document = device_state._read(str(app.device_desired_path), {})
            self.assertEqual(document["version"], 2)
            self.assertNotIn("mode", document)

    class Process:
        def __init__(self, command):
            self.command = command
            self.running = True

        def poll(self):
            return None if self.running else 0

        def terminate(self):
            self.running = False

    def reconcile(self, app, modems, desired_devices, config_path):
        """Run one reconcile pass with the host calls stubbed out, returning the spawns."""
        processes = []

        def spawn(command, **kwargs):
            process = self.Process(command)
            processes.append(process)
            return process

        with patch.object(app, "usb_modems", return_value=modems), patch(
                "host.mdd_orchestrator.run",
                return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), patch(
                "host.mdd_orchestrator.subprocess.Popen", side_effect=spawn), patch.dict(
                "os.environ", {"MDD_VPCD_READER_CONFIG": str(config_path)}):
            app.reconcile_hardware({"hardware": {"auto_detect": True, "vpcd_slots": 3}},
                                   desired_devices)
        return processes

    def test_the_card_bridge_survives_turning_vowifi_off(self):
        """Reading the SIM is what lets a line exist at all, and the VoWiFi switch stays
        disabled until one does — so a bridge that followed that switch deadlocked every
        fresh modem, and emptied the reader under any eSIM operation."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=False)
            app.root.mkdir(parents=True)
            modems = [
                {"id": "a", "name": "A", "tty": "/dev/a"},
                {"id": "b", "name": "B", "tty": "/dev/b"},
            ]
            config = root / "readers.conf"

            spawned = self.reconcile(app, modems, {
                "a": {"vowifi_enabled": True}, "b": {"vowifi_enabled": True}}, config)
            bridge_a, bridge_b = app.bridges["a"], app.bridges["b"]
            again = self.reconcile(app, modems, {
                "a": {"vowifi_enabled": False}, "b": {"vowifi_enabled": True}}, config)

            self.assertEqual(len(spawned), 2)
            self.assertEqual(again, [])
            self.assertIs(app.bridges["a"], bridge_a)
            self.assertIs(app.bridges["b"], bridge_b)
            self.assertTrue(bridge_a.running)

    def test_unplugging_a_modem_stops_only_its_bridge(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=False)
            app.root.mkdir(parents=True)
            modems = [
                {"id": "a", "name": "A", "tty": "/dev/a"},
                {"id": "b", "name": "B", "tty": "/dev/b"},
            ]
            config = root / "readers.conf"
            desired = {"a": {"vowifi_enabled": True}, "b": {"vowifi_enabled": True}}

            self.reconcile(app, modems, desired, config)
            bridge_a, bridge_b = app.bridges["a"], app.bridges["b"]
            self.reconcile(app, modems[1:], desired, config)

            self.assertNotIn("a", app.bridges)
            self.assertFalse(bridge_a.running)
            self.assertIs(app.bridges["b"], bridge_b)
            self.assertTrue(bridge_b.running)

    def test_the_packaged_virtual_pcd_definition_is_moved_out_of_the_way(self):
        """vsmartcard-vpcd ships a reader on vpcd's default port. pcscd can bind that port
        for it or for a modem reader, never both, and readdir order picks the winner."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=False)
            app.root.mkdir(parents=True)
            config = root / "conf.d" / "mdd-sim-gateway-modems"
            config.parent.mkdir(parents=True)
            packaged = config.with_name("vpcd")
            packaged.write_text('FRIENDLYNAME "Virtual PCD"\nCHANNELID 0x8C7B\n')

            self.reconcile(app, [{"id": "a", "name": "A", "tty": "/dev/a"}],
                           {"a": {"vowifi_enabled": True}}, config)

            self.assertFalse(packaged.exists())
            self.assertTrue(config.with_name(".vpcd.mdd-disabled").is_file())
            # pcsc-lite skips dot files, so the definition is only parked, not destroyed.
            self.assertIn("Virtual PCD", config.with_name(".vpcd.mdd-disabled").read_text())
            self.assertNotIn("0x8C7B", config.read_text())

    def test_a_port_saved_on_the_vpcd_default_is_migrated_away(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=False)
            app.root.mkdir(parents=True)
            app.hw_state_path.parent.mkdir(parents=True, exist_ok=True)
            app.hw_state_path.write_text(json.dumps({"assignments": {
                "a": {"id": "a", "tty": "/dev/a", "base_port": 0x8C7B}}}))

            self.reconcile(app, [{"id": "a", "name": "A", "tty": "/dev/a"}],
                           {"a": {"vowifi_enabled": True}}, root / "readers.conf")

            saved = json.loads(app.hw_state_path.read_text())["assignments"]["a"]
            self.assertNotEqual(saved["base_port"], 0x8C7B)
            self.assertEqual(saved["base_port"], mdd_orchestrator.BASE_VPCD_PORT)

    def test_a_bridge_is_respawned_when_its_port_moves(self):
        """The bridge dials the port pcscd listens on, so it cannot outlive a migration."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=False)
            app.root.mkdir(parents=True)
            modems = [{"id": "a", "name": "A", "tty": "/dev/a"}]
            desired = {"a": {"vowifi_enabled": True}}
            config = root / "readers.conf"

            self.reconcile(app, modems, desired, config)
            stale = app.bridges["a"]
            self.assertIn(str(mdd_orchestrator.BASE_VPCD_PORT), stale.command)
            moved = mdd_orchestrator.BASE_VPCD_PORT + mdd_orchestrator.VPCD_PORT_STRIDE
            app.hw_state_path.write_text(json.dumps({"assignments": {
                "a": {"id": "a", "tty": "/dev/a", "base_port": moved}}}))
            respawned = self.reconcile(app, modems, desired, config)

            self.assertFalse(stale.running)
            self.assertEqual(len(respawned), 1)
            self.assertIn(str(moved), app.bridges["a"].command)

    def test_unclaimed_tty_records_the_modemmanager_state_a_maintainer_would_ask_for(self):
        """A bridge that never starts leaves only a repeating log line to go on."""
        listing = ("    /org/freedesktop/ModemManager1/Modem/5 [Baiwang] QDC507\n")
        detail = ("modem.generic.state                     : failed\n"
                  "modem.generic.ports.value[1]            : ttyUSB9 (at)\n"
                  "modem.generic.equipment-identifier      : 123456789012345\n")

        def fake_run(args, **kwargs):
            if args[:2] == ["mmcli", "-L"]:
                return SimpleNamespace(returncode=0, stdout=listing, stderr="")
            if args[:2] == ["mmcli", "-m"]:
                return SimpleNamespace(returncode=0, stdout=detail, stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=False)
            app.root.mkdir(parents=True)
            modems = [{"id": "a", "name": "A", "tty": "/dev/ttyUSB2"}]

            with patch.object(app, "usb_modems", return_value=modems), patch(
                    "host.mdd_orchestrator.run", side_effect=fake_run), patch.dict(
                    "os.environ", {"MDD_VPCD_READER_CONFIG": str(root / "readers.conf")}):
                assignments = app.reconcile_hardware(
                    {"hardware": {"auto_detect": True}}, {"a": {"vowifi_enabled": True}},
                    through_modemmanager=True)
                app.publish_host_diagnostics(modems, assignments, False, True, True)

            self.assertNotIn("a", app.bridges)
            document = device_state._read(str(app.host_diagnostics_path), {})
            evidence = document["modemmanager"]["unclaimed"]
            self.assertEqual(evidence["unclaimed_ttys"], ["/dev/ttyUSB2"])
            # The modem exists but owns a different port — the claim check's actual input.
            self.assertEqual(evidence["modem_objects"],
                             ["/org/freedesktop/ModemManager1/Modem/5"])
            ports = evidence["modem_ports"]["/org/freedesktop/ModemManager1/Modem/5"]
            self.assertIn("modem.generic.ports.value[1]            : ttyUSB9 (at)", ports)
            self.assertEqual(document["modemmanager"]["unit_active"], False)
            self.assertIn("waiting for ModemManager to claim /dev/ttyUSB2",
                          " ".join(document["recent_log"]))
            # Identity lines are not port lines and must not ride along into the bundle.
            self.assertNotIn("123456789012345", json.dumps(document))

    def test_a_modem_modemmanager_refuses_still_gets_a_vowifi_bridge(self):
        """ModemManager cannot create a modem without a net port, which a container never
        has. Waiting forever costs the operator VoWiFi as well as the 4G that was already
        impossible, so the bridge eventually drives the serial port itself."""
        def fake_run(args, **kwargs):
            if args[:2] == ["mmcli", "-L"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if args[0] == "journalctl":
                return SimpleNamespace(
                    returncode=0,
                    stdout="Failed to find a net port in the QMI modem\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=False)
            app.root.mkdir(parents=True)
            modems = [{"id": "a", "name": "A", "tty": "/dev/ttyUSB2"}]
            commands = []

            def spawn(command, **kwargs):
                commands.append(command)
                return SimpleNamespace(poll=lambda: None, pid=7)

            with patch.object(app, "usb_modems", return_value=modems), patch(
                    "host.mdd_orchestrator.run", side_effect=fake_run), patch(
                    "host.mdd_orchestrator.subprocess.Popen", side_effect=spawn), patch.dict(
                    "os.environ", {"MDD_VPCD_READER_CONFIG": str(root / "readers.conf")}):
                desired = {"hardware": {"auto_detect": True}}
                wanted = {"a": {"vowifi_enabled": True}}
                app.reconcile_hardware(desired, wanted, through_modemmanager=True)
                # Still inside the grace period: ModemManager may simply be probing.
                self.assertEqual(commands, [])
                self.assertNotIn("a", app.bridges)

                app._unclaimed_since["/dev/ttyUSB2"] -= (
                    mdd_orchestrator.MM_CLAIM_GRACE_SECONDS + 1)
                assignments = app.reconcile_hardware(desired, wanted,
                                                     through_modemmanager=True)
                app.publish_device_status(wanted, assignments)

            self.assertIn("a", app.bridges)
            self.assertNotIn("--modemmanager", commands[0])
            self.assertIn("/dev/ttyUSB2", commands[0])
            device = device_state._read(str(app.device_status_path), {})["devices"]["a"]
            self.assertEqual(device["actual"]["vowifi_backend"], "direct-serial")
            self.assertTrue(device["actual"]["vowifi_bridge_active"])
            # The reason replaces the indefinite spinner this state used to render as.
            self.assertFalse(device["transitioning"])
            self.assertIn("ModemManager did not create a modem", device["error"])

    def test_a_bridge_never_asks_for_slots_the_driver_lacks(self):
        """Slot count is compiled into libifdvpcd, so a configured three against a packaged
        two-slot driver leaves one bridge thread dialling a socket that never appears."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=False)
            app.root.mkdir(parents=True)
            commands = []

            def spawn(command, **kwargs):
                commands.append(command)
                return SimpleNamespace(poll=lambda: None, pid=9)

            def reconcile():
                with patch.object(app, "usb_modems",
                                  return_value=[{"id": "a", "name": "A", "tty": "/dev/ttyUSB2"}]), \
                        patch("host.mdd_orchestrator.run",
                              return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), \
                        patch("host.mdd_orchestrator.subprocess.Popen", side_effect=spawn), \
                        patch.dict("os.environ",
                                   {"MDD_VPCD_READER_CONFIG": str(root / "readers.conf")}):
                    app.reconcile_hardware({"hardware": {"auto_detect": True, "vpcd_slots": 3}},
                                           {"a": {"vowifi_enabled": True}})
                return commands[-1][commands[-1].index("--slots") + 1]

            with patch.object(app, "driver_slots", return_value=2):
                self.assertEqual(reconcile(), "2")
            app.bridges.clear()
            with patch.object(app, "driver_slots", return_value=4):
                # The configured value still caps it; a wider driver does not widen the request.
                self.assertEqual(reconcile(), "3")

    def test_a_slot_count_above_the_bridge_limit_cannot_reach_it(self):
        """The bridge rejects more slots than it has logical channels and exits, which the
        reconcile loop reads as a dead bridge and respawns — every cycle, forever. The
        installer leaves the driver a spare slot, so this is reachable by configuration."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=False)
            app.root.mkdir(parents=True)
            commands = []

            with patch.object(app, "usb_modems",
                              return_value=[{"id": "a", "name": "A", "tty": "/dev/ttyUSB2"}]), \
                    patch.object(app, "driver_slots", return_value=4), \
                    patch("host.mdd_orchestrator.run",
                          return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), \
                    patch("host.mdd_orchestrator.subprocess.Popen",
                          side_effect=lambda command, **kwargs: commands.append(command) or
                          SimpleNamespace(poll=lambda: None, pid=9)), \
                    patch.dict("os.environ",
                               {"MDD_VPCD_READER_CONFIG": str(root / "readers.conf")}):
                app.reconcile_hardware({"hardware": {"auto_detect": True, "vpcd_slots": 4}},
                                       {"a": {"vowifi_enabled": True}})

            requested = commands[0][commands[0].index("--slots") + 1]
            self.assertEqual(requested, str(mdd_orchestrator.VPCD_CHANNEL_CAPACITY))
            self.assertIn(int(requested), vpcd_modem_bridge_slot_choices())

    def test_driver_slots_reads_the_installer_marker_and_falls_back_conservatively(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=True)
            drivers = root / "usr" / "lib" / "pcsc" / "drivers" / "serial"
            drivers.mkdir(parents=True)

            with patch.object(mdd_orchestrator, "VPCD_DRIVER_DIRS", (str(root / "usr/lib"),)):
                # No marker: the packaged build is in place and guessing higher is what
                # leaves a bridge thread dialling a socket that never appears.
                self.assertEqual(app.driver_slots(), mdd_orchestrator.VPCD_PACKAGED_SLOTS)
                drivers.joinpath(".mdd-vpcd-slots-4").touch()
                self.assertEqual(app.driver_slots(), 4)

    def test_a_logged_refusal_skips_the_remaining_grace_period(self):
        """ModemManager writes its refusal to its own journal. Waiting the full grace
        period after that is on record costs an affected host three minutes of dead air
        on every boot, forever."""
        def fake_run(args, **kwargs):
            if args[:2] == ["mmcli", "-L"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if args[0] == "journalctl":
                return SimpleNamespace(returncode=0, stdout=(
                    "couldn't create modem for device '/sys/devices/pci0000:00/usb3/"
                    "3-4/3-4.1': Failed to find a net port in the QMI modem\n"), stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=False)
            app.root.mkdir(parents=True)
            modems = [{"id": "a", "name": "A", "tty": "/dev/ttyUSB2", "usb_path": "3-4.1"}]
            commands = []

            with patch.object(app, "usb_modems", return_value=modems), \
                    patch("host.mdd_orchestrator.run", side_effect=fake_run), \
                    patch("host.mdd_orchestrator.subprocess.Popen",
                          side_effect=lambda command, **kwargs: commands.append(command) or
                          SimpleNamespace(poll=lambda: None, pid=9)), \
                    patch.dict("os.environ",
                               {"MDD_VPCD_READER_CONFIG": str(root / "readers.conf")}):
                desired = {"hardware": {"auto_detect": True}}
                wanted = {"a": {"vowifi_enabled": True}}
                # First cycle: unclaimed, no evidence yet — waits, and captures the journal.
                app.reconcile_hardware(desired, wanted, through_modemmanager=True)
                self.assertEqual(commands, [])
                self.assertTrue(app._claim_evidence.get("modemmanager_journal"))
                # Second cycle: the refusal is on record — degrades without the 180s wait.
                app.reconcile_hardware(desired, wanted, through_modemmanager=True)

            self.assertIn("a", app.bridges)
            self.assertNotIn("--modemmanager", commands[0])
            self.assertIn("a", app._degraded)

    def test_a_crashing_bridge_backs_off_and_tells_the_truth(self):
        """A bridge that dies on spawn used to be respawned every cycle while device
        status reported each fresh corpse as a running bridge."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=False)
            app.root.mkdir(parents=True)
            modems = [{"id": "a", "name": "A", "tty": "/dev/ttyUSB2"}]
            spawned = []

            def spawn(command, **kwargs):
                spawned.append(command)
                return SimpleNamespace(poll=lambda: 1, returncode=1, pid=9)

            with patch.object(app, "usb_modems", return_value=modems), \
                    patch("host.mdd_orchestrator.run",
                          return_value=SimpleNamespace(returncode=0, stdout="", stderr="")), \
                    patch("host.mdd_orchestrator.subprocess.Popen", side_effect=spawn), \
                    patch.dict("os.environ",
                               {"MDD_VPCD_READER_CONFIG": str(root / "readers.conf")}):
                desired = {"hardware": {"auto_detect": True}}
                wanted = {"a": {"vowifi_enabled": True}}
                app.reconcile_hardware(desired, wanted)          # spawns, process is dead
                # What the child wrote before dying; the spawn truncated the previous file.
                app._bridge_stderr_path("a").write_text(
                    "Traceback (most recent call last):\nOSError: [Errno 71] Protocol error\n")
                app.reconcile_hardware(desired, wanted)          # notices, records, gates
                self.assertEqual(len(spawned), 1, "no instant respawn inside the backoff")
                self.assertEqual(app._bridge_failures["a"]["count"], 1)
                self.assertIn("Errno 71", app._bridge_failures["a"]["reason"])

                # Backoff elapsed: one more attempt is allowed, then the gate closes again.
                app._bridge_failures["a"]["at"] -= 3600
                app.reconcile_hardware(desired, wanted)
                app._bridge_stderr_path("a").write_text(
                    "OSError: [Errno 71] Protocol error\n")
                app.reconcile_hardware(desired, wanted)
                self.assertEqual(len(spawned), 2)

                app.publish_device_status(wanted, {"a": {**modems[0], "base_port": 15360}})
            document = device_state._read(str(app.device_status_path), {})["devices"]["a"]
            self.assertFalse(document["actual"]["vowifi_bridge_active"])
            self.assertIn("keeps exiting", document["error"])
            self.assertIn("Errno 71", document["error"])

    def test_a_freshly_respawned_bridge_only_counts_once_it_survives_the_settle_window(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=True)
            app.root.mkdir(parents=True)
            app.bridges["a"] = SimpleNamespace(poll=lambda: None, pid=9)
            app._bridge_started["a"] = time.time()
            app._bridge_failures["a"] = {"count": 2, "at": time.time(), "reason": "x",
                                         "returncode": 1, "uptime": 0.1}
            stub = SimpleNamespace(returncode=0, stdout="", stderr="")
            with patch("host.mdd_orchestrator.run", return_value=stub):
                app.publish_device_status({"a": {"vowifi_enabled": True}},
                                      {"a": {"id": "a", "name": "A", "tty": "/dev/x",
                                             "base_port": 15360}})
                fresh = device_state._read(str(app.device_status_path), {})["devices"]["a"]
                self.assertFalse(fresh["actual"]["vowifi_bridge_active"])

                app._bridge_started["a"] = time.time() - 10
                app.publish_device_status({"a": {"vowifi_enabled": True}},
                                          {"a": {"id": "a", "name": "A", "tty": "/dev/x",
                                                 "base_port": 15360}})
            settled = device_state._read(str(app.device_status_path), {})["devices"]["a"]
            self.assertTrue(settled["actual"]["vowifi_bridge_active"])

    def test_replug_retires_the_degraded_verdict(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=True)
            app.root.mkdir(parents=True)
            app._degraded = {"a": "gave up"}
            app._unclaimed_since = {"/dev/ttyUSB2": 0.0}

            with patch.object(app, "usb_modems", return_value=[]), patch(
                    "host.mdd_orchestrator.run",
                    return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
                app.reconcile_hardware({"hardware": {"auto_detect": True}}, {})

            self.assertEqual(app._degraded, {})
            self.assertEqual(app._unclaimed_since, {})

    def test_claimed_tty_leaves_no_stale_unclaimed_evidence(self):
        detail = ("modem.generic.ports.value[1]            : ttyUSB2 (at)\n")

        def fake_run(args, **kwargs):
            if args[:2] == ["mmcli", "-L"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout="    /org/freedesktop/ModemManager1/Modem/5 [Quectel] EC25\n",
                    stderr="")
            if args[:2] == ["mmcli", "-m"]:
                return SimpleNamespace(returncode=0, stdout=detail, stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=False)
            app.root.mkdir(parents=True)
            app._claim_evidence = {"unclaimed_ttys": ["/dev/ttyUSB2"]}
            modems = [{"id": "a", "name": "A", "tty": "/dev/ttyUSB2"}]

            with patch.object(app, "usb_modems", return_value=modems), patch(
                    "host.mdd_orchestrator.run", side_effect=fake_run), patch(
                    "host.mdd_orchestrator.subprocess.Popen",
                    return_value=SimpleNamespace(poll=lambda: None, pid=1)), patch.dict(
                    "os.environ", {"MDD_VPCD_READER_CONFIG": str(root / "readers.conf")}):
                app.reconcile_hardware(
                    {"hardware": {"auto_detect": True}}, {"a": {"vowifi_enabled": True}},
                    through_modemmanager=True)

            self.assertIn("a", app.bridges)
            self.assertEqual(app._claim_evidence, {})

    def test_orchestrator_stop_marks_pcsc_maintenance_immediately(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=True)
            app.bridges["modem-a"] = SimpleNamespace()

            app.request_stop()

            marker = app.root / "pcsc-maintenance"
            self.assertTrue(app.stop)
            self.assertTrue(marker.is_file())
            self.assertLessEqual(abs(time.time() - int(marker.read_text())), 2)


if __name__ == "__main__":
    unittest.main()


class ReaderRecordMigrationTests(unittest.TestCase):
    """A reader's id comes from its USB port, so moving it to a hub mints a new one and
    strands the old record — rendering a connected reader twice, once permanently offline."""

    NAME = "SCR Prime CCID Reader (000000000001) 00 00"

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self._patch = patch.object(
            device_state, "HARDWARE",
            str(Path(self._temp.name) / "devices-hardware.json"))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._temp.cleanup()

    def test_the_record_follows_the_reader_to_its_new_port(self):
        device_state.set_hardware("reader-old", {
            "device_type": "reader", "name": self.NAME,
            "imei": "860349057116642", "stable_path": "1-1.5"})
        moved = device_state.migrate_reader_records(
            {"reader-new": {"name": self.NAME, "reader_port": "1-1.4.1"}})

        self.assertEqual(moved, [("reader-old", "reader-new")])
        records = device_state.hardware()
        self.assertEqual(set(records), {"reader-new"})
        # The IMEI is what the line presents to the carrier and is refreshed from here on
        # every start; losing it would silently change the device identity.
        self.assertEqual(records["reader-new"]["imei"], "860349057116642")
        self.assertEqual(records["reader-new"]["stable_path"], "1-1.4.1")

    def test_an_unplugged_reader_keeps_its_record(self):
        device_state.set_hardware("reader-old", {
            "device_type": "reader", "name": self.NAME, "imei": "860349057116642"})
        self.assertEqual(device_state.migrate_reader_records({}), [])
        self.assertIn("reader-old", device_state.hardware())

    def test_an_ambiguous_set_is_left_for_a_person(self):
        # Two identical readers replugged at once cannot be told apart by name.
        device_state.set_hardware("reader-a", {"device_type": "reader", "name": self.NAME})
        device_state.set_hardware("reader-b", {"device_type": "reader", "name": self.NAME})
        self.assertEqual(device_state.migrate_reader_records(
            {"reader-c": {"name": self.NAME}}), [])
        self.assertEqual(set(device_state.hardware()), {"reader-a", "reader-b"})

    def test_a_reader_that_did_not_move_is_untouched(self):
        device_state.set_hardware("reader-a", {"device_type": "reader", "name": self.NAME})
        self.assertEqual(device_state.migrate_reader_records(
            {"reader-a": {"name": self.NAME}}), [])
        self.assertIn("reader-a", device_state.hardware())

    def test_a_modem_record_is_never_claimed_by_a_reader(self):
        device_state.set_hardware("2c7c-0125-1-1.4",
                                  {"device_type": "modem", "name": self.NAME})
        self.assertEqual(device_state.migrate_reader_records(
            {"reader-new": {"name": self.NAME}}), [])
        self.assertIn("2c7c-0125-1-1.4", device_state.hardware())


def vpcd_modem_bridge_slot_choices():
    """The --slots values the bridge actually accepts, read from its own parser.

    Asserting against the parser rather than a copied literal keeps the orchestrator's cap
    and the bridge's limit from drifting apart silently.
    """
    import argparse
    from host import vpcd_modem_bridge
    parser = argparse.ArgumentParser()
    source = Path(vpcd_modem_bridge.__file__).read_text()
    match = re.search(r'"--slots".*choices=\(([^)]*)\)', source)
    return tuple(int(value) for value in match.group(1).split(","))
