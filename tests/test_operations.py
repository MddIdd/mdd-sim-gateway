import json
import os
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from control.app import config, operations


class OperationsTests(unittest.TestCase):
    def test_engine_sources_do_not_log_authentication_secrets(self):
        root = Path(__file__).resolve().parents[1]
        sources = "\n".join(
            (root / name).read_text(errors="replace")
            for name in ("engine/swu_ike.py", "engine/ami_usim.py",
                         "host/vpcd_modem_bridge.py", "control/app/lpa.py")
        )
        forbidden = (
            "print('CK'", "print('IK'", "print('MSK'", "print('EMSK'",
            "print('KENCR'", "print('KAUT'", "DIFFIE-HELLMAN KEY",
            "IKEv2 DECRYPTION TABLE INFO", "ESP SA INFO (wireshark)",
            "AuthResponse sent: RES=",
            "print(a.get_imsi())", "device identity set: IMEI=",
            'AT <-- %s" % response.hex()',
            '" ".join(cmd)', 'lpac non-json stdout: %s',
        )
        self.assertEqual([item for item in forbidden if item in sources], [])

    def test_redaction_removes_identities_credentials_and_key_material(self):
        value = operations.redact({
            "pin": "1234",
            "nested": {"token": "secret"},
            "note": "call +441234567890",
            "subscription_url": "https://example.test/sub?token=secret",
            "headers_json": '{"Authorization":"Bearer secret"}',
            "activation_code": "LPA:1$smdp.example$MATCHING-ID",
        })
        self.assertEqual(value["pin"], "<redacted>")
        self.assertEqual(value["nested"]["token"], "<redacted>")
        self.assertNotIn("441234567890", value["note"])
        self.assertTrue(all("secret" not in str(value[key]).lower()
                            for key in ("subscription_url", "headers_json")))
        self.assertEqual(value["activation_code"], "<redacted>")
        log = operations.redact_log(
            "IKEv2 DECRYPTION TABLE INFO (Wireshark):\n"
            "aabbccddeeff00112233445566778899\n"
            "00112233445566778899aabbccddeeff\n"
            "CK=00112233445566778899aabbccddeeff\nnormal"
        )
        self.assertNotIn("001122", log)
        self.assertTrue(log.endswith("normal"))

    def test_redaction_preserves_non_secret_eap_aka_diagnostics(self):
        diagnostic = (
            "IKE_AUTH rejected with AUTHENTICATION_FAILED before any EAP-AKA challenge "
            "(SIM not queried); the SIM may not be provisioned for VoWiFi"
        )
        self.assertEqual(operations.redact_log(diagnostic), diagnostic)

    def test_apdu_trace_fallback_does_not_repeat_failed_unpack(self):
        source = (Path(__file__).resolve().parents[1] / "engine/swu_ike.py").read_text(
            errors="replace")
        self.assertNotIn("_data, _sw1, _sw2 = res", source)
        self.assertIn("unexpected response type=%s", source)

    def test_local_backup_is_not_exposed_as_file_contents(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            Path(temp, "config.yaml").write_text("settings: {}\ninstances: {}\n")
            result = operations.create_local_backup("Test Gateway")
            self.assertEqual(result["location"], "gateway-local")
            self.assertNotIn("path", result)
            self.assertTrue(Path(temp, "backups", result["name"]).is_file())

    def test_support_bundle_contains_only_redacted_documents(self):
        settings_value = {
            "telegram": {"bot_token": "secret"},
            "proxy": {"subscription_url": "https://example.test/sub?token=url-secret"},
            "webhook": {"headers_json": '{"Authorization":"Bearer header-secret"}'},
        }
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp), patch.object(
                config, "get_settings", return_value=settings_value):
            run = Path(temp, "instances", "sim1", "run")
            run.mkdir(parents=True)
            run.joinpath("charon.log").write_text(
                "ESP SA INFO (wireshark):\nsecret-table-row-1\nsecret-table-row-2\n"
                "CK=00112233445566778899aabbccddeeff\n")
            content = operations.support_bundle({"imei": "123456789012345"})
            with zipfile.ZipFile(BytesIO(content)) as archive:
                settings = archive.read("settings-redacted.yaml").decode()
                status = json.loads(archive.read("status-redacted.json"))
                log = archive.read("logs/sim1-charon.log").decode()
            self.assertNotIn("secret", settings)
            self.assertNotIn("001122", log)
            self.assertEqual(status["imei"], "<redacted>")

    def test_support_bundle_carries_the_host_view_the_control_plane_cannot_observe(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            orchestrator = Path(temp, "orchestrator")
            orchestrator.mkdir(parents=True)
            orchestrator.joinpath("host-diagnostics.json").write_text(json.dumps({
                "virtualization": "lxc",
                "modemmanager": {"unit_active": False,
                                 "unclaimed": {"unclaimed_ttys": ["/dev/ttyUSB2"]}},
                "recent_log": ["2026-08-14 12:09:16 waiting for ModemManager to claim "
                               "/dev/ttyUSB2"],
                "imei": "123456789012345",
            }))

            content = operations.support_bundle({})

            with zipfile.ZipFile(BytesIO(content)) as archive:
                host = json.loads(archive.read("host-diagnostics-redacted.json"))
            self.assertTrue(host["available"])
            self.assertEqual(host["virtualization"], "lxc")
            self.assertFalse(host["modemmanager"]["unit_active"])
            self.assertEqual(host["modemmanager"]["unclaimed"]["unclaimed_ttys"],
                             ["/dev/ttyUSB2"])
            self.assertIn("waiting for ModemManager", host["recent_log"][0])
            self.assertEqual(host["imei"], "<redacted>")

    def test_support_bundle_reports_a_missing_host_view_instead_of_omitting_it(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            content = operations.support_bundle({})
            with zipfile.ZipFile(BytesIO(content)) as archive:
                host = json.loads(archive.read("host-diagnostics-redacted.json"))
        # Silence here would read as a healthy host; a stopped orchestrator is a finding.
        self.assertFalse(host["available"])
        self.assertIn("orchestrator", host["note"])

    def test_support_bundle_includes_retained_ike_segment_tail(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp):
            archive_dir = Path(temp) / "instances" / "sim1" / "logs" / "ike"
            archive_dir.mkdir(parents=True)
            archive_dir.joinpath("charon-20260807-110000.log").write_text(
                "[2026-08-07 11:00:00+0800] STATE 1\n"
                "[2026-08-07 11:00:01+0800] STATE 2\n")

            content = operations.support_bundle({}, log_lines=50)

            with zipfile.ZipFile(BytesIO(content)) as bundle:
                retained = bundle.read(
                    "logs/sim1-charon-20260807-110000.log").decode()
            self.assertIn("STATE 2", retained)

    def test_sensitive_config_files_are_owner_only(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "CONFIG_PATH", os.path.join(temp, "config.yaml")):
            config.save({"settings": {}, "instances": {}, "internal": {}})
            self.assertEqual(os.stat(config.CONFIG_PATH).st_mode & 0o777, 0o600)
            inst = {
                "id": "sim1", "imsi": "001010123456789", "mcc": "001", "mnc": "01",
                "ami_secret": "random-ami-secret",
                "sip": {"webrtc": {"enable": True, "password": "random-web-secret"}},
            }
            path = config.write_instance_json(inst, {})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(temp).st_mode & 0o777, 0o700)

    def test_engine_configuration_fails_closed_without_generated_credentials(self):
        inst = {"id": "sim1", "imsi": "001010123456789", "mcc": "001", "mnc": "01"}
        with self.assertRaisesRegex(ValueError, "AMI credential"):
            config.render_instance_json(inst, {})
        inst["ami_secret"] = "random-ami-secret"
        with self.assertRaisesRegex(ValueError, "WebRTC credential"):
            config.render_instance_json(inst, {})


if __name__ == "__main__":
    unittest.main()
