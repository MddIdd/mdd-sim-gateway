"""The registration refresh must use the expiry the carrier granted, not the one we asked for.

After a tunnel rebuild the UE registers from a new inner address and the IMS core answers with
our new binding plus the stale one (expires=0). pjproject then cannot match our Contact — the
sec-agree REGISTER leaves from the protected port — and its fallback only trusts a response
whose Contact count matches ours, so it returns the 600000 seconds we requested. Asterisk
scheduled the refresh a week out, the binding expired at the carrier 40 min later (EE; 60 min
on Vodafone UK), and the line sat unreachable for ~12-17s until the dead transport was noticed.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATCHER = ROOT / "engine" / "patches" / "asterisk" / "volte_registration_expiry_from_contact.py"

# The part of sip_outbound_registration_response_cb() the patch anchors on.
CALLBACK = (
    "static void sip_outbound_registration_response_cb(struct pjsip_regc_cbparam *param)\n"
    "{\n"
    "\tstruct sip_outbound_registration_client_state *client_state = param->token;\n"
    "\tstruct registration_response *response;\n"
    "\n"
    "\tresponse->code = param->code;\n"
    "\tresponse->expiration = param->expiration;\n"
    "\tresponse->client_state = client_state;\n"
    "}\n"
)


class VolteRegistrationExpiryPatchTests(unittest.TestCase):
    def _apply(self, source):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "res" / "res_pjsip_outbound_registration.c"
            target.parent.mkdir(parents=True)
            target.write_text(source)
            first = subprocess.run([sys.executable, str(PATCHER)],
                                   env={**os.environ, "AST_SRC": temp},
                                   capture_output=True, text=True)
            patched = target.read_text()
            second = subprocess.run([sys.executable, str(PATCHER)],
                                    env={**os.environ, "AST_SRC": temp},
                                    capture_output=True, text=True)
            return first, patched, target.read_text(), second

    def test_the_granted_expiry_is_read_from_our_own_contact(self):
        first, patched, twice, second = self._apply(CALLBACK)

        self.assertEqual(first.returncode, 0, first.stderr)
        # Correct after pjproject's value, never instead of the assignment.
        self.assertLess(patched.index("response->expiration = param->expiration;"),
                        patched.index("PATCH volte_registration_expiry_from_contact"))
        # Our Contact is identified by what we actually sent, not by position in the response.
        self.assertIn("client_state->last_tdata->msg", patched)
        self.assertIn("pj_stricmp(&uri->host, &sent_uri->host)", patched)
        self.assertIn("uri->port != sent_uri->port", patched)
        self.assertIn("pj_strcmp(&uri->user, &sent_uri->user)", patched)
        # Re-running the build must not stack a second copy.
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(twice, patched)

    def test_only_volte_registrations_are_touched(self):
        _first, patched, _twice, _second = self._apply(CALLBACK)

        guard = patched.index("client_state->volte")
        self.assertLess(guard, patched.index("for (i = 0; sent_uri"))

    def test_a_stale_binding_cannot_become_the_refresh_interval(self):
        """The stale binding of the previous inner address arrives as expires=0. Taking it
        would schedule the refresh immediately and read as an unregistration."""
        _first, patched, _twice, _second = self._apply(CALLBACK)

        self.assertIn("hdr->expires == 0", patched)
        self.assertIn("hdr->expires == PJSIP_EXPIRES_NOT_SPECIFIED", patched)
        # expires is unsigned and response->expiration is a signed int.
        self.assertIn("hdr->expires > (pj_uint32_t) 0x7FFFFFFF", patched)
        self.assertIn("response->expiration = (int) hdr->expires;", patched)

    def test_an_upstream_refactor_fails_the_build_instead_of_being_skipped(self):
        first, _patched, _twice, _second = self._apply(
            "static void sip_outbound_registration_response_cb(struct pjsip_regc_cbparam *p)\n"
            "{\n\tresponse->expiration = compute_expiration(p);\n}\n")

        self.assertEqual(first.returncode, 1)
        self.assertIn("response->expiration assignment not found", first.stderr)

    def test_a_duplicated_anchor_fails_the_build(self):
        first, _patched, _twice, _second = self._apply(CALLBACK + CALLBACK)

        self.assertEqual(first.returncode, 1)
        self.assertIn("not unique", first.stderr)


if __name__ == "__main__":
    unittest.main()
