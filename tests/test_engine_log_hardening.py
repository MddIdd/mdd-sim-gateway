import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from control.app import config


class ProductionDebugTests(unittest.TestCase):
    def _paths(self, temp):
        return patch.multiple(
            config, DATA_DIR=temp, CONFIG_PATH=os.path.join(temp, "config.yaml"))

    def test_legacy_saved_asterisk_debug_is_disabled_on_load(self):
        with tempfile.TemporaryDirectory() as temp, self._paths(temp):
            with open(config.CONFIG_PATH, "w", encoding="utf-8") as handle:
                yaml.safe_dump({
                    "settings": {"debug": {"asterisk": True, "charon": True}},
                    "instances": {"1": {
                        "id": "1", "debug": {"asterisk": True, "charon": True},
                    }},
                }, handle)

            loaded = config.load()

            self.assertFalse(loaded["instances"]["1"]["debug"]["asterisk"])
            self.assertTrue(loaded["instances"]["1"]["debug"]["charon"])

    def test_upsert_cannot_persist_asterisk_debug(self):
        with tempfile.TemporaryDirectory() as temp, self._paths(temp), \
                patch.object(config, "alloc_ports_auto", return_value=config._alloc_ports(0)):
            saved = config.upsert_instance({
                "id": "1", "debug": {"asterisk": True, "charon": True},
            })

            self.assertFalse(saved["debug"]["asterisk"])
            self.assertTrue(saved["debug"]["charon"])

    def test_engine_contract_forces_debug_off_even_for_imported_config(self):
        inst = {
            "id": "1", "imsi": "001010000000001", "mcc": "001", "mnc": "01",
            "imei": "123456789012345", "ami_secret": "secret",
            "sip": {"webrtc": {"password": "password"}},
            "debug": {"asterisk": True, "charon": True},
        }
        settings = {**config.DEFAULTS["settings"],
                    "debug": {"asterisk": True, "charon": False}}
        with tempfile.TemporaryDirectory() as temp, self._paths(temp):
            rendered = config.render_instance_json(inst, settings)

        self.assertFalse(rendered["debug"]["asterisk"])
        self.assertTrue(rendered["debug"]["charon"])


class AsteriskModulePolicyTests(unittest.TestCase):
    def test_unused_error_generating_modules_are_excluded(self):
        root = Path(__file__).resolve().parent.parent
        policy = (root / "engine" / "templates" / "modules.conf.j2").read_text()

        self.assertIn("autoload = yes", policy)
        for module in (
                "app_adsiprog.so", "app_getcpeid.so", "codec_vevs.so", "res_adsi.so",
                "res_ari.so", "res_config_ldap.so",
                "res_odbc.so", "res_phoneprov.so", "res_pjsip_config_wizard.so"):
            self.assertIn(f"noload => {module}", policy)
        for required in ("chan_pjsip.so", "codec_amr.so", "res_http_websocket.so",
                         "res_pjsip_messaging.so", "res_rtp_asterisk.so"):
            self.assertNotIn(f"noload => {required}", policy)

    def test_catch_all_dialplan_avoids_bare_dot_wildcard(self):
        root = Path(__file__).resolve().parent.parent
        dialplan = (root / "engine" / "templates" / "extensions.conf.j2").read_text()

        self.assertIn("{% set any_extension = '_[!-~]!' %}", dialplan)
        self.assertNotIn("exten => _.,", dialplan)
        self.assertEqual(dialplan.count("exten => {{ any_extension }},1"), 3)

    def test_private_resolve_fields_use_nodoc_registration(self):
        root = Path(__file__).resolve().parent.parent
        patcher = (root / "engine" / "patches" / "asterisk" /
                   "resolve_config_docs.py").read_text()

        self.assertIn("ast_sorcery_object_field_register_nodoc", patcher)
        self.assertIn("expected 3 resolve field registrations", patcher)

    def test_missing_security_server_reauth_keeps_established_sas(self):
        root = Path(__file__).resolve().parent.parent
        patcher = (root / "engine" / "patches" / "asterisk" /
                   "reauth_missing_security_server.py").read_text()

        self.assertIn("handle_volte_unauthorized", patcher)
        self.assertIn("transport_state->volte.registered", patcher)
        self.assertIn("VOLTE_STATE_RESPONSE", patcher)
        # The fallback must only swallow an ABSENT header; parse failures stay fatal.
        self.assertIn("pjsip_msg_find_hdr_by_name", patcher)

    def test_swu_workers_keep_fork_semantics_on_python_314(self):
        root = Path(__file__).resolve().parent.parent
        swu = (root / "engine" / "swu_ike.py").read_text()

        self.assertIn('multiprocessing.set_start_method("fork", force=True)', swu)


class RegisteredIdentityLogTests(unittest.TestCase):
    """Reading a line's phone number must not require SIP tracing or an extra REGISTER.

    The packet logger writes authentication headers to the container log, and the forced
    re-registration it existed to produce is answered with 503 by some IMS cores (issue #8),
    which Asterisk reports as a rejected registration and the health policy acts on. The
    patched engine announces the identity of the registrations it makes anyway.
    """

    PATCHER = (Path(__file__).resolve().parent.parent / "engine" / "patches" / "asterisk"
               / "ims_public_identity_log.py")

    PROLOGUE = (
        "static int store_volte_p_associated_uri(struct registration_response *response)\n"
        "{\n"
        "\tstruct ast_sip_transport_state *transport_state = NULL;\n"
        "\tint ret = -1;\n"
        "\n"
    )
    REST = (
        "\tif (get_endpoint_transport_transport_state(response->client_state, NULL, NULL, "
        "&transport_state))\n"
        "\t\tgoto out;\n"
        "out:\n"
        "\treturn ret;\n"
        "}\n"
    )

    def _apply(self, source):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "res" / "res_pjsip_outbound_registration.c"
            target.parent.mkdir(parents=True)
            target.write_text(source)
            first = subprocess.run([sys.executable, str(self.PATCHER)],
                                   env={**os.environ, "AST_SRC": temp},
                                   capture_output=True, text=True)
            patched = target.read_text() if target.exists() else source
            second = subprocess.run([sys.executable, str(self.PATCHER)],
                                    env={**os.environ, "AST_SRC": temp},
                                    capture_output=True, text=True)
            return first, patched, target.read_text() if target.exists() else source, second

    def test_the_registered_identity_is_logged_on_the_ordinary_path(self):
        first, patched, twice, second = self._apply(self.PROLOGUE + self.REST)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('ast_log(LOG_NOTICE, "IMS public identity: %s', patched)
        # Every associated identity, not just the first: carriers list the IMSI-derived IMPU
        # ahead of the dialable number.
        self.assertIn("pjsip_msg_find_hdr_by_name", patched)
        self.assertIn("pau ? pau->next : NULL", patched)
        # Nothing may be sent: the log line is emitted from the response handler itself.
        self.assertNotIn("pjsip_endpt_send_request", patched)
        # Re-running the build must not stack a second copy.
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(twice, patched)

    def test_an_upstream_refactor_fails_the_build_instead_of_being_skipped(self):
        first, _patched, _twice, _second = self._apply(
            "static int store_volte_p_associated_uri(void)\n{\n\treturn -1;\n}\n")

        self.assertEqual(first.returncode, 1)
        self.assertIn("prologue not found", first.stderr)


class MoSubmitReportTests(unittest.TestCase):
    """An RP-ACK/RP-ERROR is the SMSC reporting on a message we sent, not a message for us.

    Both used to fall through to the "Unknown RP-DATA" branch and were then handed to the
    messaging core with no TPDU, so every submitted segment filed one empty inbound SMS. They
    must be answered (an unanswered report is repeated) and stop before the dialplan, while the
    debug hex dump the control plane parses for the delivery verdict stays where it is.
    """

    PATCHER = (Path(__file__).resolve().parent.parent / "engine" / "patches" / "asterisk"
               / "mo_submit_report.py")

    SOURCE = (
        "static void parse_rpdata(pjsip_rx_data *rdata, struct ast_msg *msg, int *ack_ref)\n"
        "{\n"
        "\tast_log(LOG_DEBUG, \"SMS RP-DATA '%s'.\\n\", buf2);\n"
        "\tswitch (buf[0])\n"
        "\t{\n"
        "\tcase 0x01: {\n"
        "\t\t*ack_ref = buf[1] & 0xff;\n"
        "\t\treturn;\n"
        "\t}\n"
        "\tcase 0x03: /* RP-ACK */\n"
        "\tcase 0x05: /* RP-ERROR */\n"
        "\tdefault:\n"
        "\t\tast_log(LOG_WARNING, \"Unknown RP-DATA 0x%02x. Dropping message\\n\", buf[0]);\n"
        "\t\treturn;\n"
        "\t}\n"
        "}\n"
        "\n"
        "static pj_bool_t module_on_rx_request(pjsip_rx_data *rdata)\n"
        "{\n"
        "\tcode = rx_data_to_ast_msg(rdata, msg, is_sms, &ack_ref);\n"
        "\tif (code != PJSIP_SC_OK) {\n"
        "\t\tsend_response(rdata, code, NULL, NULL);\n"
        "\t\tast_msg_destroy(msg);\n"
        "\t\treturn PJ_TRUE;\n"
        "\t}\n"
        "\n"
        "\tif (!ast_msg_has_destination(msg)) {\n"
        "\t\treturn PJ_TRUE;\n"
        "\t}\n"
        "\n"
        "\tif (!send_response(rdata, is_sms ? PJSIP_SC_OK : PJSIP_SC_ACCEPTED, NULL, NULL)) {\n"
        "\t\tast_msg_queue(msg);\n"
        "\t}\n"
        "\n"
        "\treturn PJ_TRUE;\n"
        "}\n"
    )

    def _apply(self, source):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "res" / "res_pjsip_messaging.c"
            target.parent.mkdir(parents=True)
            target.write_text(source)
            run = lambda: subprocess.run([sys.executable, str(self.PATCHER)],
                                         env={**os.environ, "AST_SRC": temp},
                                         capture_output=True, text=True)
            first = run()
            patched = target.read_text()
            second = run()
            return first, patched, target.read_text(), second

    def test_a_submit_report_is_answered_but_never_reaches_the_dialplan(self):
        first, patched, twice, second = self._apply(self.SOURCE)

        self.assertEqual(first.returncode, 0, first.stderr)
        # Both report types are recognised instead of sharing the unknown-type branch.
        self.assertIn("SMS submit report: RP-ACK", patched)
        self.assertIn("SMS submit report: RP-ERROR", patched)
        # The refusal reason is only ever stated by the RP cause.
        self.assertIn("RP cause %d", patched)
        # Answered, then stopped: the messaging core never sees it.
        self.assertIn("if (ack_ref == MDD_RP_SUBMIT_REPORT) {", patched)
        report_at = patched.index("if (ack_ref == MDD_RP_SUBMIT_REPORT) {")
        queue_at = patched.index("ast_msg_queue(msg);")
        self.assertLess(report_at, queue_at,
                        "the report must return before the message is queued")
        self.assertLess(report_at, patched.index("ast_msg_has_destination"),
                        "a report has no dialplan destination; it must leave before that check")
        # It is answered rather than dropped, and its allocation is released.
        stop = patched[report_at:patched.index("\n\tif (!ast_msg_has_destination", report_at)]
        self.assertIn("send_response(rdata, PJSIP_SC_OK, NULL, NULL);", stop)
        self.assertIn("ast_msg_destroy(msg);", stop)
        # A genuinely unknown type keeps the original warning.
        self.assertIn('Unknown RP-DATA 0x%02x. Dropping message', patched)
        # Re-running the build must not stack a second copy.
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(patched, twice)

    def test_the_hex_dump_the_delivery_watcher_parses_is_left_alone(self):
        # control/app/main.py turns 'sent' into 'delivered'/'failed' by parsing
        # "parse_rpdata: SMS RP-DATA '<hex>'" out of the engine log. Removing or renaming it
        # would silently strand every outbound message at 'sent'.
        _, patched, _, _ = self._apply(self.SOURCE)
        self.assertIn("static void parse_rpdata(", patched)
        self.assertIn('ast_log(LOG_DEBUG, "SMS RP-DATA', patched)

    def test_an_upstream_refactor_fails_the_build_instead_of_being_skipped(self):
        first, patched, _, _ = self._apply(
            self.SOURCE.replace("case 0x03: /* RP-ACK */\n", ""))

        self.assertEqual(first.returncode, 1)
        self.assertIn("not found", first.stderr)
        self.assertNotIn("MDD_RP_SUBMIT_REPORT", patched)


if __name__ == "__main__":
    unittest.main()
