import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from control.app import device_state, main
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

    def _orchestrator_calls(self, method, snapshot, *, exists=True, active=(), **kwargs):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp) / "data", Path(temp), dry_run=False)
            calls = []

            def fake_run(args, **_kwargs):
                calls.append(args)
                if args[:3] == ["nmcli", "connection", "show"] and len(args) == 4:
                    return SimpleNamespace(returncode=0 if exists else 1, stdout="", stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.object(app, "_active_gsm_profiles", return_value=list(active)), patch(
                    "host.mdd_orchestrator.run", side_effect=fake_run):
                getattr(app, method)(snapshot, **kwargs) if method == "disconnect_modem_data" \
                    else getattr(app, method)({"id": "modem-a"}, snapshot)
            return calls, app

    def test_a_modem_profile_never_autoconnects_or_carries_the_default_route(self):
        """A modem is plugged in to have its SIM read. If NetworkManager dials it on its own
        and the result becomes the default route, the VoWiFi tunnel authenticating that very
        SIM leaves through that SIM's own carrier."""
        policy = Orchestrator.modem_profile_policy()
        self.assertEqual(policy[policy.index("connection.autoconnect") + 1], "no")
        self.assertEqual(policy[policy.index("ipv4.never-default") + 1], "yes")
        self.assertEqual(policy[policy.index("ipv6.never-default") + 1], "yes")

    def test_the_default_route_guard_is_released_for_a_modem_only_uplink(self):
        with patch.object(mdd_orchestrator, "MODEM_MAY_PROVIDE_DEFAULT_ROUTE", True):
            policy = Orchestrator.modem_profile_policy()
        # The switch is about the default route only: autoconnect stays off either way,
        # because the orchestrator owns the desired state and brings the profile up itself.
        self.assertEqual(policy[policy.index("connection.autoconnect") + 1], "no")
        self.assertNotIn("ipv4.never-default", policy)
        self.assertNotIn("ipv6.never-default", policy)

    def test_a_new_cellular_profile_is_created_with_the_policy(self):
        snapshot = {"powered": True, "data_active": False, "registration": "roaming",
                    "primary_port": "ttyUSB5", "apn": "ims",
                    "profile": Orchestrator.cellular_profile_name("modem-a")}
        calls, _app = self._orchestrator_calls("ensure_modem_data", snapshot, exists=False)
        add = next(call for call in calls if call[:3] == ["nmcli", "connection", "add"])
        self.assertEqual(add[add.index("connection.autoconnect") + 1], "no")
        self.assertEqual(add[add.index("ipv4.never-default") + 1], "yes")

    def test_the_first_dial_is_not_held_back_on_a_freshly_booted_host(self):
        """The 45-second limit stored "never attempted" as monotonic zero, so on a host up
        for less than 45 seconds the first dial was skipped."""
        snapshot = {"powered": True, "data_active": False, "registration": "home",
                    "primary_port": "ttyUSB5", "apn": "internet",
                    "profile": Orchestrator.cellular_profile_name("modem-a")}
        with patch("host.mdd_orchestrator.time.monotonic", return_value=5.0):
            calls, _app = self._orchestrator_calls("ensure_modem_data", snapshot, exists=False)
        self.assertTrue(any(call[:3] == ["nmcli", "connection", "up"] for call in calls))

    def test_an_existing_legacy_profile_is_corrected_even_without_an_apn(self):
        """Profiles written by an older version carry autoconnect=yes and no route guard, and
        they outlive the upgrade. Correcting them only when an APN was known left them."""
        snapshot = {"powered": True, "data_active": False, "registration": "home",
                    "primary_port": "ttyUSB5", "apn": "",
                    "profile": Orchestrator.cellular_profile_name("modem-a")}
        calls, _app = self._orchestrator_calls("ensure_modem_data", snapshot, exists=True)
        modify = next(call for call in calls if call[:3] == ["nmcli", "connection", "modify"])
        self.assertEqual(modify[modify.index("connection.autoconnect") + 1], "no")
        self.assertEqual(modify[modify.index("ipv4.never-default") + 1], "yes")

    def test_disabling_data_reaches_a_profile_whose_modem_reports_no_port(self):
        """A modem in a failed or SIM-less state reports no primary port. Matching only on the
        port found nothing to do in exactly the state where an autoconnecting profile is most
        likely to dial on its own."""
        snapshot = {"profile": "mdd-cell-legacy", "primary_port": "", "network_interface": ""}
        calls, app = self._orchestrator_calls(
            "disconnect_modem_data", snapshot, active=[("mdd-cell-legacy", "wwan0")])
        modify = next(call for call in calls if call[:3] == ["nmcli", "connection", "modify"])
        self.assertEqual(modify[3], "mdd-cell-legacy")
        self.assertEqual(modify[modify.index("connection.autoconnect") + 1], "no")
        self.assertIn(["nmcli", "connection", "down", "mdd-cell-legacy"], calls)
        self.assertIn("mdd-cell-legacy", app.modem_profile_policed)

    def test_a_policed_profile_is_not_rewritten_on_every_reconcile(self):
        """Data-off runs on every cycle. Rewriting settings that already say what we want was
        the largest avoidable source of process creation in the old proposal for this fix."""
        snapshot = {"profile": "mdd-cell-legacy", "primary_port": "", "network_interface": ""}
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp) / "data", Path(temp), dry_run=False)
            calls = []

            def fake_run(args, **_kwargs):
                calls.append(args)
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.object(app, "_active_gsm_profiles", return_value=[]), patch(
                    "host.mdd_orchestrator.run", side_effect=fake_run):
                app.disconnect_modem_data(dict(snapshot))
                first = len(calls)
                for _ in range(5):
                    app.disconnect_modem_data(dict(snapshot))
        self.assertEqual(len([c for c in calls if c[:3] == ["nmcli", "connection", "modify"]]), 1)
        self.assertLess(len(calls) - first, first * 5)

    def _sweep(self, listing, returncode=0):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp) / "data", Path(temp), dry_run=False)
            calls = []

            def fake_run(args, **_kwargs):
                calls.append(args)
                if args[:2] == ["nmcli", "-t"]:
                    return SimpleNamespace(returncode=returncode, stdout=listing, stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("host.mdd_orchestrator.run", side_effect=fake_run):
                app.police_orphaned_modem_profiles()
                app.police_orphaned_modem_profiles()
            return calls, app

    def test_a_leftover_profile_is_secured_when_cellular_is_simply_turned_off(self):
        """Every data path is gated on the cellular backend being up, so turning cellular data
        off -- ModemManager stood down, the GSM profile left behind -- is the one state where
        nothing corrects a profile that still says autoconnect forever."""
        listing = ("Wired connection 1:802-3-ethernet\n"
                   "mdd-cell-0a05dad4d32d:gsm\n")
        calls, app = self._sweep(listing)
        modify = [call for call in calls if call[:3] == ["nmcli", "connection", "modify"]]
        self.assertEqual(len(modify), 1, "swept once per stand-down, not per cycle")
        self.assertEqual(modify[0][3], "mdd-cell-0a05dad4d32d")
        self.assertEqual(modify[0][modify[0].index("connection.autoconnect") + 1], "no")
        self.assertEqual(modify[0][modify[0].index("ipv4.never-default") + 1], "yes")
        self.assertIn("mdd-cell-0a05dad4d32d", app.modem_profile_policed)

    def test_the_sweep_leaves_connections_it_does_not_own_alone(self):
        listing = ("Wired connection 1:802-3-ethernet\n"
                   "some-other-modem:gsm\n"
                   "mdd-cell-keep:bridge\n")
        calls, _app = self._sweep(listing)
        self.assertEqual([c for c in calls if c[:3] == ["nmcli", "connection", "modify"]], [])

    def test_an_unreadable_connection_list_is_retried_rather_than_swallowed(self):
        calls, app = self._sweep("", returncode=1)
        self.assertFalse(app.modem_profiles_swept)
        self.assertEqual(len([c for c in calls if c[:2] == ["nmcli", "-t"]]), 2)

    def test_unreadable_sim_iccid_is_not_reported_as_an_identity(self):
        """mmcli prints "--" for a property it could not read. Passed through, the control
        plane treats it as a live ICCID that matches no line and never falls through to the
        PC/SC bridge, which can still read the card over a logical channel."""
        self.assertEqual(Orchestrator.normalize_iccid("--"), "")
        self.assertEqual(Orchestrator.normalize_iccid("unknown"), "")
        self.assertEqual(Orchestrator.normalize_iccid(""), "")
        self.assertEqual(Orchestrator.normalize_iccid("8901000000000000001"),
                         "8901000000000000001")
        # Not an ICCID: wrong issuer prefix, or too short to be one.
        self.assertEqual(Orchestrator.normalize_iccid("1234567890123456789"), "")
        self.assertEqual(Orchestrator.normalize_iccid("890100000"), "")

    def test_snapshot_drops_a_placeholder_sim_iccid(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp) / "data", Path(temp), dry_run=False)
            modem_detail = """modem.generic.primary-port : cdc-wdm1
modem.generic.sim : /org/freedesktop/ModemManager1/SIM/1
modem.generic.state : connected
modem.generic.power-state : on
"""

            def fake_run(args, **_kwargs):
                if args[:2] == ["mmcli", "-m"]:
                    return SimpleNamespace(returncode=0, stdout=modem_detail, stderr="")
                if args[:2] == ["mmcli", "-i"]:
                    return SimpleNamespace(returncode=0,
                                           stdout="sim.properties.iccid : --\n", stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.object(app, "modemmanager_modem_for_tty",
                              return_value="/org/freedesktop/ModemManager1/Modem/4"), patch(
                                  "host.mdd_orchestrator.run", side_effect=fake_run):
                value = app.modem_snapshot({"id": "modem-c", "tty": "/dev/ttyUSB7"})
        self.assertEqual(value["sim_iccid"], "")

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
            self.pid = 7

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

    def test_host_diagnostics_reports_only_bridge_identity_health(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=False)
            app.root.mkdir(parents=True)
            modem = {"id": "a", "name": "A", "tty": "/dev/a"}
            assignments = {"a": {**modem, "base_port": 15360}}
            app.bridges["a"] = self.Process(["bridge"])
            app._bridge_commands["a"] = ["bridge"]
            metadata = app.data / "modems" / "a.json"
            metadata.parent.mkdir(parents=True)
            metadata.write_text(json.dumps({
                "updated_at": int(time.time()) - 12,
                "imei": "123456789012345", "iccid": "8944000000000000000",
                "channel_status": "ready", "channel_requested": 3,
                "channel_allocated": 3,
            }))
            with patch.object(app, "virtualization", return_value="kvm"), \
                    patch.object(app, "vpcd_port_status", return_value={}), \
                    patch.object(app, "reader_definitions_listing", return_value=[]):
                app.publish_host_diagnostics([modem], assignments, False, False, True)

            document = device_state._read(str(app.host_diagnostics_path), {})
            bridge = document["bridges"]["a"]

        self.assertTrue(bridge["imei_valid"])
        self.assertTrue(bridge["iccid_valid"])
        self.assertTrue(bridge["channels_ready"])
        self.assertGreaterEqual(bridge["metadata_age_seconds"], 12)
        self.assertNotIn("123456789012345", json.dumps(bridge))
        self.assertNotIn("8944000000000000000", json.dumps(bridge))

    def test_bad_bridge_metadata_is_bounded_instead_of_crashing_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=False)
            app.root.mkdir(parents=True)
            modem = {"id": "a", "name": "A", "tty": "/dev/a"}
            assignments = {"a": {**modem, "base_port": 15360}}
            app.bridges["a"] = self.Process(["bridge"])
            metadata = app.data / "modems" / "a.json"
            metadata.parent.mkdir(parents=True)
            metadata.write_text(json.dumps({
                "updated_at": "not-a-number", "imei": "123456789012345",
                "iccid": "89" + "0" * 20, "channel_status": "ready",
                "channel_requested": {}, "channel_allocated": "nan",
            }))
            with patch.object(app, "virtualization", return_value="kvm"), \
                    patch.object(app, "vpcd_port_status", return_value={}), \
                    patch.object(app, "reader_definitions_listing", return_value=[]):
                app.publish_host_diagnostics([modem], assignments, False, False, True)

            bridge = device_state._read(str(app.host_diagnostics_path), {})["bridges"]["a"]

        self.assertIsNone(bridge["metadata_age_seconds"])
        self.assertTrue(bridge["iccid_valid"])
        self.assertFalse(bridge["channels_ready"])

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

    def test_reader_definitions_stay_readable_by_an_unprivileged_pcscd(self):
        """The service's UMask=0077 made the file 0600 root, which a pcscd that runs as
        its own user (Ubuntu 26.04) skips — the host then shows no modem reader at all."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=False)
            app.root.mkdir(parents=True)
            modems = [{"id": "a", "name": "A", "tty": "/dev/a"}]
            desired = {"a": {"vowifi_enabled": True}}
            config = root / "readers.conf"
            previous = os.umask(0o077)
            try:
                self.reconcile(app, modems, desired, config)
            finally:
                os.umask(previous)
            self.assertEqual(config.stat().st_mode & 0o777, 0o644)

            # A file an earlier release left at 0600 already holds the right content, so
            # only its mode can prompt the repair — and pcscd must re-read it afterwards.
            os.chmod(config, 0o600)
            with patch("host.mdd_orchestrator.run",
                       return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as run, \
                    patch.object(app, "usb_modems", return_value=modems), \
                    patch("host.mdd_orchestrator.subprocess.Popen",
                          side_effect=lambda command, **kwargs: self.Process(command)), \
                    patch.dict("os.environ", {"MDD_VPCD_READER_CONFIG": str(config)}):
                app.reconcile_hardware({"hardware": {"auto_detect": True, "vpcd_slots": 3}},
                                       desired)
            self.assertEqual(config.stat().st_mode & 0o777, 0o644)
            run.assert_any_call(["systemctl", "restart", "pcscd.service"])

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

    def test_each_modem_reader_uses_an_independent_vpcd_library_image(self):
        """libifdvpcd's ctx array is process-global, so two readers must not share a DSO."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "libifdvpcd.so"
            source.write_bytes(b"test-vpcd-driver")
            config_path = root / "reader.conf.d" / "mdd-sim-gateway-modems"
            app = Orchestrator(root / "data", root, dry_run=False)

            with patch.dict(os.environ, {
                    "MDD_VPCD_LIBRARY": str(source),
                    "MDD_VPCD_DRIVER_INSTANCE_DIR": str(root / "isolated"),
            }):
                first = app.reader_stanza({"id": "modem-a"}, 0x3C00, config_path)
                second = app.reader_stanza({"id": "modem-b"}, 0x3D00, config_path)

            first_path = root / "isolated" / "libifdvpcd-mdd-3c00.so"
            second_path = root / "isolated" / "libifdvpcd-mdd-3d00.so"
            self.assertIn(f"LIBPATH {first_path}", first)
            self.assertIn(f"LIBPATH {second_path}", second)
            self.assertNotEqual(first_path, second_path)
            self.assertEqual(first_path.read_bytes(), source.read_bytes())
            self.assertEqual(second_path.read_bytes(), source.read_bytes())
            self.assertNotEqual(first_path.stat().st_ino, second_path.stat().st_ino)

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

    def test_repeated_modemmanager_phone_failure_degrades_to_direct_serial(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=True)
            app.root.mkdir(parents=True)
            app._bridge_stderr_path("a").write_text(
                "ModemError: SIM logical channel allocation failed (0/3 allocated): "
                "GDBus.Error:org.freedesktop.ModemManager1.Error.MobileEquipment."
                "PhoneFailure: Phone failure\n")
            proc = SimpleNamespace(returncode=1)

            for _ in range(3):
                app._record_bridge_exit("a", proc, time.time() - 1)

            self.assertIn("a", app._degraded)
            self.assertIn("direct serial", app._degraded["a"])
            plan = Orchestrator.capability_plan({
                "a": {"cellular_enabled": False, "flight_mode": True,
                      "vowifi_enabled": True},
            })
            self.assertFalse(app.cellular_backend_needed(plan, {"a"}, {}))

    def test_modemmanager_stands_down_once_it_has_refused_every_modem(self):
        """Without a modem object ModemManager provides nothing, but its probes share the
        AT ports with the direct bridges and corrupt SIM channel allocation. Field log: a
        bridge read the reply to ModemManager's own +QGPS probe where its +CSIM answer
        should have been."""
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp) / "data", Path(temp), dry_run=True)
            plan = Orchestrator.capability_plan({"a": {"vowifi_enabled": True},
                                                "b": {"vowifi_enabled": True}})
            # Refused only one of two: the healthy modem still needs the backend.
            app._degraded = {"a": "refused"}
            self.assertTrue(app.cellular_backend_needed(plan, {"a", "b"}, {}))
            # Refused both, nobody wants cellular: stand down.
            app._degraded = {"a": "refused", "b": "refused"}
            self.assertFalse(app.cellular_backend_needed(plan, {"a", "b"}, {}))
            # An operator turning cellular on brings it back — that request must fail
            # visibly through ModemManager, not be silently pre-empted here.
            wants = Orchestrator.capability_plan({"a": {"vowifi_enabled": True,
                                                        "cellular_enabled": True},
                                                 "b": {"vowifi_enabled": True}})
            self.assertTrue(app.cellular_backend_needed(wants, {"a", "b"}, {}))
            # No modems at all: nothing to run a backend for.
            self.assertFalse(app.cellular_backend_needed(
                Orchestrator.capability_plan({}), set(), {}))

    def test_configured_serial_mode_outranks_everything(self):
        """An explicit VoWiFi-only configuration keeps ModemManager down even when a
        stale desired flag still asks for cellular: with the backend disabled the UI
        presents cellular as unsupported, so nothing may resurrect it."""
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp) / "data", Path(temp), dry_run=True)
            wants = Orchestrator.capability_plan(
                {"a": {"vowifi_enabled": True, "cellular_enabled": True}})
            serial = {"modem_backend": "serial"}
            self.assertFalse(app.cellular_backend_needed(wants, {"a"}, serial))
            self.assertTrue(app.cellular_backend_needed(wants, {"a"}, {}))
            self.assertTrue(app.cellular_backend_needed(
                wants, {"a"}, {"modem_backend": "auto"}))

    def test_serial_mode_publishes_cellular_as_unsupported_capability(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = Orchestrator(root / "data", root, dry_run=True)
            app.root.mkdir(parents=True)
            app._serial_mode = True
            app.bridges["a"] = SimpleNamespace(poll=lambda: None, pid=9)
            app._bridge_started["a"] = time.time() - 60
            stub = SimpleNamespace(returncode=1, stdout="", stderr="")
            with patch("host.mdd_orchestrator.run", return_value=stub):
                app.publish_device_status(
                    {"a": {"vowifi_enabled": True}},
                    {"a": {"id": "a", "name": "A", "tty": "/dev/x", "base_port": 15360}})
            document = device_state._read(str(app.device_status_path), {})
            device = document["devices"]["a"]
            self.assertFalse(device["actual"]["cellular_supported"])
            self.assertEqual(device["actual"]["vowifi_backend"], "direct-serial")
            self.assertFalse(device["transitioning"])
            self.assertEqual(document["shared"]["modem_backend"], "serial")
            self.assertEqual(document["shared"]["cellular_backend"],
                             "disabled-by-configuration")

    def test_flight_mode_only_uses_direct_serial_without_modemmanager(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp) / "data", Path(temp), dry_run=True)
            flight_only = Orchestrator.capability_plan({
                "a": {"cellular_enabled": False, "flight_mode": True,
                      "vowifi_enabled": True},
            })
            self.assertFalse(app.cellular_backend_needed(flight_only, {"a"}, {}))

            with_cellular = Orchestrator.capability_plan({
                "a": {"cellular_enabled": True, "flight_mode": True,
                      "vowifi_enabled": True},
            })
            self.assertTrue(app.cellular_backend_needed(with_cellular, {"a"}, {}))

    def test_flight_mode_direct_bridge_is_a_settled_status(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp) / "data", Path(temp), dry_run=True)
            app.root.mkdir(parents=True)
            app.bridges["a"] = SimpleNamespace(poll=lambda: None, pid=9)
            app._bridge_started["a"] = time.time() - 10
            desired = {"a": {"cellular_enabled": False, "flight_mode": True,
                              "vowifi_enabled": True}}
            assignments = {"a": {"id": "a", "name": "A", "tty": "/dev/ttyUSB2"}}

            with patch.object(app, "service_active", return_value=False):
                app.publish_device_status(desired, assignments)

            device = device_state._read(str(app.device_status_path), {})["devices"]["a"]
            self.assertFalse(device["transitioning"])
            self.assertEqual(device["actual"]["vowifi_backend"], "direct-serial")

    def test_serial_mode_never_touches_the_modem_radio_port(self):
        """VoWiFi-only mode gives the direct bridge exclusive ownership of the AT port.

        The PVE field host rejects ordinary pyserial DTR/RTS setup with EPROTO.  Sending
        AT+CFUN before the virtualisation-tolerant bridge starts left both modems unable to
        answer its first ATE0 command, despite cellular and flight mode being unsupported.
        """
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp) / "data", Path(temp), dry_run=False)
            app._serial_mode = True
            serial_module = SimpleNamespace(Serial=Mock())
            with patch("host.mdd_orchestrator.serial", serial_module):
                app.apply_device_radios(
                    [{"id": "a", "tty": "/dev/ttyUSB2"}],
                    {"a": {"cellular_enabled": True, "flight_mode": False}},
                    through_modemmanager=False)
            serial_module.Serial.assert_not_called()
            self.assertEqual(app.radio_states, {})

    def test_standing_down_skips_the_modem_reset(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Orchestrator(Path(temp) / "data", Path(temp), dry_run=False)
            app.obsolete_services_retired = True
            calls = []
            stub = SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("host.mdd_orchestrator.run",
                       side_effect=lambda args, **k: calls.append(args) or stub), \
                    patch.object(app, "service_active", return_value=True), \
                    patch.object(app, "stop_bridges"), \
                    patch.object(app, "reset_modems_after_cellular") as reset, \
                    patch("host.mdd_orchestrator.time.sleep"):
                app.apply_cellular_backend(False, reset_modems=False)
                reset.assert_not_called()
                self.assertIn(["systemctl", "stop", "ModemManager.service"], calls)

                app.apply_cellular_backend(False)
                reset.assert_called_once()

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
            "imei": "350000000000018", "stable_path": "1-1.5"})
        moved = device_state.migrate_reader_records(
            {"reader-new": {"name": self.NAME, "reader_port": "1-1.4.1"}})

        self.assertEqual(moved, [("reader-old", "reader-new")])
        records = device_state.hardware()
        self.assertEqual(set(records), {"reader-new"})
        # The IMEI is what the line presents to the carrier and is refreshed from here on
        # every start; losing it would silently change the device identity.
        self.assertEqual(records["reader-new"]["imei"], "350000000000018")
        self.assertEqual(records["reader-new"]["stable_path"], "1-1.4.1")

    def test_an_unplugged_reader_keeps_its_record(self):
        device_state.set_hardware("reader-old", {
            "device_type": "reader", "name": self.NAME, "imei": "350000000000018"})
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


class DeviceDisplayNameTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_nickname_then_imei_keeps_raw_name_for_port_migration(self):
        raw_name = "Alcor Link AK9563 01 00"
        device = {"id": "reader-old", "device_type": "reader", "instance_id": None,
                  "name": "Desk reader", "reader": raw_name, "stable_path": "1-2"}
        with tempfile.TemporaryDirectory() as temp:
            with patch.multiple(device_state, ROOT=temp,
                                HARDWARE=str(Path(temp) / "devices-hardware.json")), \
                    patch.object(main, "_unified_devices", new=AsyncMock(return_value=[device])), \
                    patch.object(main.hub, "broadcast", new=AsyncMock()):
                await main.api_device_name("reader-old", {"display_name": " Desk reader "})
                await main.api_device_hardware("reader-old", {"imei": "490154203237518"})
                record = device_state.hardware()["reader-old"]
                self.assertEqual(record["display_name"], "Desk reader")
                self.assertEqual(record["name"], raw_name)
                self.assertEqual(record["imei"], "490154203237518")

                moved = device_state.migrate_reader_records({
                    "reader-new": {"name": raw_name, "reader_port": "1-3"}})
                self.assertEqual(moved, [("reader-old", "reader-new")])
                self.assertEqual(device_state.hardware()["reader-new"]["display_name"],
                                 "Desk reader")

    async def test_name_validation_and_missing_device(self):
        with patch.object(main, "_unified_devices", new=AsyncMock(return_value=[])):
            with self.assertRaises(main.HTTPException) as invalid:
                await main.api_device_name("reader-a", {"display_name": "bad\nname"})
            self.assertEqual(invalid.exception.status_code, 422)
            with self.assertRaises(main.HTTPException) as missing:
                await main.api_device_name("reader-a", {"display_name": "Desk reader"})
            self.assertEqual(missing.exception.status_code, 404)


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
