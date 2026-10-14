"""Same-hardware device-id migration when a serial-less modem changes USB port."""
import json
import tempfile
import unittest
from pathlib import Path

from host.mdd_orchestrator import Orchestrator


def _modem(device_id, usb_path, vid="2c7c", pid="0125"):
    return {"id": device_id, "name": "USB modem", "tty": "/dev/ttyUSB2",
            "usb_path": usb_path, "vid": vid, "pid": pid}


class DeviceMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        data = Path(self.tmp.name)
        self.app = Orchestrator(data, data, dry_run=True)
        self.app.root.mkdir(parents=True)

    def _write(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def _desired(self):
        return json.loads(self.app.device_desired_path.read_text())["devices"]

    def _identity(self, device_id, imei="350000000000027"):
        path = self.app.data / "modems" / f"{device_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write(path, {"imei": imei})

    def test_replugged_modem_keeps_configuration_and_sheds_ghost(self):
        state = {"cellular_enabled": True, "vowifi_enabled": False, "flight_mode": False}
        self._write(self.app.device_desired_path,
                    {"version": 2, "devices": {"2c7c-0125-1-1.2": state}})
        self._write(self.app.hw_state_path,
                    {"assignments": {"2c7c-0125-1-1.2": {"id": "2c7c-0125-1-1.2",
                                                         "usb_path": "1-1.2",
                                                         "base_port": 35963}}})
        self._write(self.app.device_status_path,
                    {"devices": {"2c7c-0125-1-1.2": {"present": False}}})
        user_hardware_path = self.app.root / "devices-hardware.json"
        self._write(user_hardware_path, {"devices": {
            "2c7c-0125-1-1.2": {"device_type": "modem", "display_name": "Desk modem"}}})
        self._identity("2c7c-0125-1-1.2")
        self._identity("2c7c-0125-1-1.4")

        self.app.migrate_device_ids([_modem("2c7c-0125-1-1.4", "1-1.4")])

        self.assertEqual(self._desired(), {"2c7c-0125-1-1.4": state})
        assignments = json.loads(self.app.hw_state_path.read_text())["assignments"]
        self.assertNotIn("2c7c-0125-1-1.2", assignments)
        moved = assignments["2c7c-0125-1-1.4"]
        self.assertEqual(moved["base_port"], 35963,
                         "the VPCD port must survive so readers stay stable")
        self.assertEqual(moved["usb_path"], "1-1.4")
        status = json.loads(self.app.device_status_path.read_text())["devices"]
        self.assertEqual(status, {})
        records = json.loads(user_hardware_path.read_text())["devices"]
        self.assertEqual(records, {"2c7c-0125-1-1.4": {
            "device_type": "modem", "display_name": "Desk modem"}})

    def test_same_model_requires_matching_hardware_imei(self):
        devices = {"2c7c-0125-1-1.2": {"vowifi_enabled": True}}
        self._write(self.app.device_desired_path, {"version": 2, "devices": dict(devices)})
        self._identity("2c7c-0125-1-1.2")

        # The new bridge has not published identity yet: wait instead of guessing.
        self.app.migrate_device_ids([_modem("2c7c-0125-1-1.4", "1-1.4")])
        self.assertEqual(self._desired(), devices)

        # A different modem of the same USB model must not inherit this configuration.
        self._identity("2c7c-0125-1-1.4", "350000000000036")
        self.app.migrate_device_ids([_modem("2c7c-0125-1-1.4", "1-1.4")])
        self.assertEqual(self._desired(), devices)

    def test_existing_new_id_name_wins_and_old_record_is_removed(self):
        old_id, new_id = "2c7c-0125-1-1.2", "2c7c-0125-1-1.4"
        self._write(self.app.device_desired_path,
                    {"devices": {old_id: {"vowifi_enabled": True}}})
        self._identity(old_id)
        self._identity(new_id)
        user_hardware_path = self.app.root / "devices-hardware.json"
        self._write(user_hardware_path, {"devices": {
            old_id: {"display_name": "Old name"},
            new_id: {"display_name": "New name"}}})

        self.app.migrate_device_ids([_modem(new_id, "1-1.4")])

        records = json.loads(user_hardware_path.read_text())["devices"]
        self.assertEqual(records, {new_id: {"display_name": "New name"}})

    def test_ambiguous_or_unrelated_devices_are_left_alone(self):
        devices = {"2c7c-0125-1-1.2": {"vowifi_enabled": True},
                   "2c7c-0125-1-1.3": {"vowifi_enabled": False}}
        self._write(self.app.device_desired_path, {"version": 2, "devices": dict(devices)})

        # Two absent same-family ids: cannot tell which one replugged — no migration.
        self.app.migrate_device_ids([_modem("2c7c-0125-1-1.5", "1-1.5")])
        self.assertEqual(self._desired(), devices)

        # A different vid/pid never adopts another family's configuration.
        self.app.migrate_device_ids([_modem("1e0e-9001-1-1.5", "1-1.5", vid="1e0e", pid="9001")])
        self.assertEqual(self._desired(), devices)

    def test_still_present_device_is_never_treated_as_stale(self):
        devices = {"2c7c-0125-1-1.2": {"vowifi_enabled": True}}
        self._write(self.app.device_desired_path, {"version": 2, "devices": dict(devices)})
        self.app.migrate_device_ids([
            _modem("2c7c-0125-1-1.2", "1-1.2"),
            _modem("2c7c-0125-1-1.4", "1-1.4"),
        ])
        self.assertEqual(self._desired(), devices)


if __name__ == "__main__":
    unittest.main()


class RetiredIdentityDocumentTests(unittest.TestCase):
    """The identity file is what proves two USB paths are the same hardware, and the control
    plane builds its device list from every file in that directory. Leaving the old one behind
    resurrects the id the migration just retired, as a nameless offline device."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.app = Orchestrator(self.root, Path.cwd(), dry_run=True)
        (self.root / "modems").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._temp.cleanup()

    def _identity(self, device_id, imei="350000000000027"):
        path = self.root / "modems" / f"{device_id}.json"
        path.write_text(json.dumps({"hardware_id": device_id, "imei": imei,
                                    "base_port": 35963}))
        return path

    def test_the_retired_document_is_removed(self):
        old = self._identity("2c7c-0125-1-1.2")
        new = self._identity("2c7c-0125-1-1.4")
        self.app.retire_identity_document("2c7c-0125-1-1.2", "2c7c-0125-1-1.4")
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_the_record_is_carried_over_when_the_bridge_has_not_published_yet(self):
        # Migration can win the race against the new bridge; the IMEI must not be lost.
        self._identity("2c7c-0125-1-1.2")
        self.app.retire_identity_document("2c7c-0125-1-1.2", "2c7c-0125-1-1.4")
        carried = json.loads((self.root / "modems" / "2c7c-0125-1-1.4.json").read_text())
        self.assertEqual(carried["hardware_id"], "2c7c-0125-1-1.4")
        self.assertEqual(carried["imei"], "350000000000027")
        self.assertEqual(carried["base_port"], 35963)
        self.assertFalse((self.root / "modems" / "2c7c-0125-1-1.2.json").exists())

    def test_a_missing_document_is_not_an_error(self):
        self.app.retire_identity_document("absent", "also-absent")
