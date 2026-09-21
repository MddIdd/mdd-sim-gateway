"""Take the granted registration expiry from our own Contact in the 200 OK.

After a tunnel rebuild the UE has a new inner address, so the IMS core answers the first
REGISTER with our new binding AND the stale binding of the previous inner address
(`expires=0`).  pjproject's calculate_response_expiration() then finds no match for our
Contact — the sec-agree REGISTER is sent from the protected port while regc still holds the
Contact of the unprotected one — and its fallback only trusts the response when it carries
exactly as many Contact headers as we sent.  Two headers against our one, no Expires header in
the response, so it falls back to the value we ASKED for: 600000 seconds (TS 24.229 5.1.1.2).

The VoLTE re-registration timer is derived from that number, so Asterisk scheduled the refresh
~7 days out and never sent it (observed: `pjsip show registrations` reporting "exp. 599398s"
one second after a 200 OK that granted 2400).  The binding then expired at the carrier: EE
closed the TCP connection 12-15s after the 2400s it had granted, Asterisk logged "PJSIP
transport 'volte_ims' failed" and registered again from scratch.  That second registration
matches the fallback's contact-count test — the stale binding is gone by then — so it gets the
real value and every later refresh is correct.  The visible symptom was a ~12-17s window with
no registration ~40 min (EE) or ~60 min (Vodafone UK) after every tunnel rebuild, twice a day.

Fix it where the evidence is unambiguous: the response lists our Contact with the granted
expires.  In the VoLTE path, find the Contact whose URI matches the one we actually sent (host,
port and user, taken from the REGISTER in last_tdata) and use its expires.  Only a positive
value replaces what pjproject computed, so an unregistration (expires 0) still reads as one,
and a response without our Contact leaves the existing behaviour untouched.

Upstream has no fix: this is sysmocom's VoLTE Contact rewrite meeting pjproject's fallback, and
neither the sysmocom Asterisk branches (jolly/testing, 2026-04-28) nor pjproject master
(2026-09-19) changes either side.
"""

import os
import sys
from pathlib import Path


SOURCE = Path(os.environ.get("AST_SRC", "/home/asterisk-build/asterisk")) \
    / "res/res_pjsip_outbound_registration.c"

MARKER = "PATCH volte_registration_expiry_from_contact"

ANCHOR = "\tresponse->code = param->code;\n\tresponse->expiration = param->expiration;\n"

CORRECTION = """
\t/* """ + MARKER + """: pjproject cannot match our Contact in a VoLTE 200 OK (the
\t * sec-agree REGISTER goes out from the protected port) and its fallback only trusts the
\t * response when the Contact count matches ours.  After a tunnel rebuild the core also
\t * lists the stale binding of the previous inner address, so the count differs and the
\t * expiry falls back to the 600000 we requested -- the refresh is then scheduled a week
\t * out, the binding expires at the carrier and the transport is torn down mid-session.
\t * Read the granted value from our own Contact in the response instead. */
\tif (client_state->volte && param->contact_cnt > 0 && client_state->last_tdata) {
\t\tpjsip_contact_hdr *sent_contact;
\t\tpjsip_sip_uri *sent_uri = NULL;
\t\tint i;

\t\tsent_contact = pjsip_msg_find_hdr(client_state->last_tdata->msg,
\t\t\tPJSIP_H_CONTACT, NULL);
\t\tif (sent_contact && !sent_contact->star && sent_contact->uri) {
\t\t\tpjsip_uri *uri = (pjsip_uri *) pjsip_uri_get_uri(sent_contact->uri);

\t\t\tif (PJSIP_URI_SCHEME_IS_SIP(uri) || PJSIP_URI_SCHEME_IS_SIPS(uri)) {
\t\t\t\tsent_uri = (pjsip_sip_uri *) uri;
\t\t\t}
\t\t}

\t\tfor (i = 0; sent_uri && i < param->contact_cnt; ++i) {
\t\t\tpjsip_contact_hdr *hdr = param->contact[i];
\t\t\tpjsip_sip_uri *uri;

\t\t\t/* expires is unsigned; the stale binding of a previous inner address
\t\t\t * arrives as 0, and response->expiration below is a signed int. */
\t\t\tif (!hdr || hdr->star || !hdr->uri
\t\t\t\t|| hdr->expires == PJSIP_EXPIRES_NOT_SPECIFIED
\t\t\t\t|| hdr->expires == 0 || hdr->expires > (pj_uint32_t) 0x7FFFFFFF) {
\t\t\t\tcontinue;
\t\t\t}

\t\t\turi = (pjsip_sip_uri *) pjsip_uri_get_uri(hdr->uri);
\t\t\tif (!PJSIP_URI_SCHEME_IS_SIP(uri) && !PJSIP_URI_SCHEME_IS_SIPS(uri)) {
\t\t\t\tcontinue;
\t\t\t}
\t\t\t/* Same binding, ignoring the parameters the core may rewrite (the
\t\t\t * transport parameter comes back lower-cased, for one). */
\t\t\tif (pj_stricmp(&uri->host, &sent_uri->host)
\t\t\t\t|| uri->port != sent_uri->port
\t\t\t\t|| pj_strcmp(&uri->user, &sent_uri->user)) {
\t\t\t\tcontinue;
\t\t\t}

\t\t\tif ((int) hdr->expires != response->expiration) {
\t\t\t\tast_log(LOG_NOTICE, "VoLTE registration expiry %u from our Contact in the "
\t\t\t\t\t"response, not %d as computed; %d Contact header(s) present.\\n",
\t\t\t\t\t(unsigned int) hdr->expires, response->expiration, param->contact_cnt);
\t\t\t\tresponse->expiration = (int) hdr->expires;
\t\t\t}
\t\t\tbreak;
\t\t}
\t}
"""


def patch(source: str) -> str:
    if MARKER in source:
        return source

    fn_start = source.find("static void sip_outbound_registration_response_cb(")
    if fn_start < 0:
        raise ValueError("sip_outbound_registration_response_cb not found")

    anchor_at = source.find(ANCHOR, fn_start)
    if anchor_at < 0:
        raise ValueError("response->expiration assignment not found in the response callback")
    if source.find(ANCHOR, anchor_at + 1) >= 0:
        raise ValueError("response->expiration assignment is not unique")

    insert_at = anchor_at + len(ANCHOR)
    return source[:insert_at] + CORRECTION + source[insert_at:]


try:
    original = SOURCE.read_text()
    updated = patch(original)
except (OSError, ValueError) as exc:
    print(f"VoLTE registration-expiry patch failed: {exc}", file=sys.stderr)
    raise SystemExit(1) from exc

if updated == original:
    print("VoLTE registration expiry already patched")
else:
    SOURCE.write_text(updated)
    print("patched sip_outbound_registration_response_cb to read the granted registration "
          "expiry from our own Contact in the response")
