"""Give the SIM time to answer an AKA challenge, and retry when it still does not.

The IMS-AKA response for a VoLTE REGISTER is computed by the SIM through the ami_usim bridge.
sysmocom waits SIM_TIMEOUT = 3 seconds for it.  A SIM in a Quectel EC25 answers through the
modem's serial bridge, and that path takes ~2.6 seconds every time (measured 2.57-3.33 s; a
SIM in a plain PC/SC reader answers in 0.2-0.4 s), so it sits ~0.4 s under the limit and
misses it now and then: five times in six days on the T-Mobile US line.

Missing it was far worse than the delay.  sim_timeout_cb() only logged "Sim did not respond,
authentication failed." and dropped the pending response: no retry was scheduled and the
registration status was left as it was.  The late AuthResponse then found nothing pending and
was discarded.  The line kept showing "Registered" while the carrier's binding ran out, and
nothing registered again until the carrier closed the TCP connection (~70 minutes each time,
observed 2026-09-16, 09-17 and 09-20; `pjsip show registrations` read "exp. 1846s ago").

Two changes:

* SIM_TIMEOUT 3 -> 10 seconds, so the ordinary EC25 answer arrives in time.
* When the SIM still does not answer, handle it exactly as the bridge's own "SIM failed" reply
  is handled (ami_authresponse sets VOLTE_STATE_SIM_FAILED and queues the response to
  handle_registration_response).  That path already ends in volte_failed, which marks the
  registration as temporarily rejected and schedules the configured fatal_retry_interval.

The pending response has one reference, and both the timer callback and ami_authresponse
consume it.  They run on different threads (the pjsip timer thread and an AMI session thread)
and upstream coordinates them with nothing: an answer read as pending just before the timer
fires would reach the task queue after the timer had already queued the same response, and
with the timeout path above that task would get a NULL response.  So the pending response is
now claimed under the client state's lock:

* The challenge handler publishes the pending response and arms the timer under the lock, and
  only then asks the SIM, so no answer can find the response pending without its timer.
* The timer callback clears the pending pointer under the lock before queueing.
* ami_authresponse, under the lock, takes the pending response only if it cancels the timer.
  pjsip removes an expired timer from its heap before running the callback, so a failed cancel
  means the timeout owns the response and the late answer is refused as "No pending
  AuthRequest".

Lock order is client state, then the timer heap, on every path: pjsip runs timer callbacks with
the heap unlocked.
"""

import os
import sys
from pathlib import Path


SOURCE = Path(os.environ.get("AST_SRC", "/home/asterisk-build/asterisk")) \
    / "res/res_pjsip_outbound_registration.c"

MARKER = "PATCH volte_sim_timeout_retry"

TIMEOUT_ANCHOR = "#define SIM_TIMEOUT 3\n"
TIMEOUT_REPLACEMENT = (
    "/* " + MARKER + ": a SIM behind a Quectel EC25 serial bridge answers in ~2.6 s,\n"
    " * which left almost no margin under the original 3 s. */\n"
    "#define SIM_TIMEOUT 10\n"
)

CALLBACK_ORIGINAL = (
    "static void sim_timeout_cb(pj_timer_heap_t *timer_heap, struct pj_timer_entry *entry)\n"
    "{\n"
    "\tstruct registration_response *response = entry->user_data;\n"
    "\n"
    "\tast_log(LOG_ERROR, \"Sim did not respond, authentication failed.\\n\");\n"
    "\n"
    "\tif (response->client_state->destroy) {\n"
    "\t\t/* We have a pending deferred destruction to complete now. */\n"
    "\t\tao2_ref(response->client_state, +1);\n"
    "\t\thandle_client_state_destruction(response->client_state);\n"
    "\t}\n"
    "\n"
    "\tao2_ref(response, -1);\n"
    "}\n"
)

CALLBACK_PATCHED = (
    "static int handle_registration_response(void *data);\n"
    "\n"
    "static void sim_timeout_cb(pj_timer_heap_t *timer_heap, struct pj_timer_entry *entry)\n"
    "{\n"
    "\tstruct registration_response *response = entry->user_data;\n"
    "\tint pending;\n"
    "\n"
    "\tast_log(LOG_ERROR, \"Sim did not respond, authentication failed.\\n\");\n"
    "\n"
    "\tif (response->client_state->destroy) {\n"
    "\t\t/* We have a pending deferred destruction to complete now. */\n"
    "\t\tao2_ref(response->client_state, +1);\n"
    "\t\thandle_client_state_destruction(response->client_state);\n"
    "\t\tao2_ref(response, -1);\n"
    "\t\treturn;\n"
    "\t}\n"
    "\n"
    "\t/* " + MARKER + ": the original dropped the response here, leaving the\n"
    "\t * registration stuck -- no retry, status unchanged -- until the carrier's binding\n"
    "\t * expired and it closed the transport (~70 min).  Take the path the bridge's own\n"
    "\t * \"SIM failed\" reply takes: volte_failed then schedules fatal_retry_interval.\n"
    "\t * Clearing the pending pointer under the lock hands the response to this callback\n"
    "\t * alone; ami_authresponse sees it gone, or fails to cancel this timer, and refuses a\n"
    "\t * late AuthResponse as \"No pending AuthRequest\". */\n"
    "\tao2_lock(response->client_state);\n"
    "\tpending = response->client_state->volte_response == response;\n"
    "\tif (pending) {\n"
    "\t\tresponse->client_state->volte_response = NULL;\n"
    "\t\tvolte_set_state(response->client_state, VOLTE_STATE_SIM_FAILED);\n"
    "\t}\n"
    "\tao2_unlock(response->client_state);\n"
    "\tif (pending) {\n"
    "\t\tif (!ast_sip_push_task(response->client_state->serializer,\n"
    "\t\t\t\thandle_registration_response, response)) {\n"
    "\t\t\t/* The response reference now belongs to the queued task. */\n"
    "\t\t\treturn;\n"
    "\t\t}\n"
    "\t\tast_log(LOG_WARNING, \"Failed to queue the SIM timeout as a registration failure; \"\n"
    "\t\t\t\"the registration will not retry until its transport closes.\\n\");\n"
    "\t}\n"
    "\n"
    "\tao2_ref(response, -1);\n"
    "}\n"
)


CANCEL_ORIGINAL = (
    "static void cancel_sim_timer(struct registration_response *response)\n"
    "{\n"
    "\tif (pj_timer_heap_cancel_if_active(pjsip_endpt_get_timer_heap(ast_sip_get_pjsip_endpoint()),\n"
    "\t\t&response->sim_timer, response->sim_timer.id)) {\n"
    "\t}\n"
    "}\n"
)

CANCEL_PATCHED = (
    "/* " + MARKER + ": report whether the timer was still pending.  pjsip takes an\n"
    " * expired timer off the heap before running its callback, so 0 means the callback owns the\n"
    " * response (or the timer was never armed). */\n"
    "static int cancel_sim_timer(struct registration_response *response)\n"
    "{\n"
    "\treturn pj_timer_heap_cancel_if_active(pjsip_endpt_get_timer_heap(ast_sip_get_pjsip_endpoint()),\n"
    "\t\t&response->sim_timer, response->sim_timer.id) > 0;\n"
    "}\n"
)

REQUEST_ORIGINAL = (
    "\t\tif (auth->usim_ami) {\n"
    "\t\t\tpj_time_val delay = { .sec = SIM_TIMEOUT, };\n"
    "\t\t\tast_debug(1, \"Asking SIM card via AMI to authenticate with the callenge.\\n\");\n"
    "\t\t\tvolte_send_authrequest(response->client_state->registration_name, &algo, rand, autn);\n"
    "\t\t\tresponse->client_state->volte_response = response;\n"
    "\t\t\tvolte_set_state(response->client_state, VOLTE_STATE_SIM_REQUEST);\n"
    "\t\t\tif (pjsip_endpt_schedule_timer(ast_sip_get_pjsip_endpoint(), &response->sim_timer, &delay) != PJ_SUCCESS) {\n"
    "\t\t\t\tast_log(LOG_WARNING, \"Failed to schedule SIM response timer\\n\");\n"
    "\t\t\t\tgoto out;\n"
    "\t\t\t}\n"
)

REQUEST_PATCHED = (
    "\t\tif (auth->usim_ami) {\n"
    "\t\t\tpj_time_val delay = { .sec = SIM_TIMEOUT, };\n"
    "\t\t\tint armed;\n"
    "\t\t\tast_debug(1, \"Asking SIM card via AMI to authenticate with the callenge.\\n\");\n"
    "\t\t\t/* " + MARKER + ": publish the pending response and arm its timer\n"
    "\t\t\t * before asking, under the lock ami_authresponse and sim_timeout_cb take. */\n"
    "\t\t\tao2_lock(response->client_state);\n"
    "\t\t\tresponse->client_state->volte_response = response;\n"
    "\t\t\tvolte_set_state(response->client_state, VOLTE_STATE_SIM_REQUEST);\n"
    "\t\t\tarmed = pjsip_endpt_schedule_timer(ast_sip_get_pjsip_endpoint(), &response->sim_timer, &delay) == PJ_SUCCESS;\n"
    "\t\t\tao2_unlock(response->client_state);\n"
    "\t\t\tif (!armed) {\n"
    "\t\t\t\tast_log(LOG_WARNING, \"Failed to schedule SIM response timer\\n\");\n"
    "\t\t\t\tgoto out;\n"
    "\t\t\t}\n"
    "\t\t\tvolte_send_authrequest(response->client_state->registration_name, &algo, rand, autn);\n"
)

ANSWER_ORIGINAL = (
    "\tif (!state->client_state || !state->client_state->volte_response) {\n"
    "\t\tast_debug(1, \"SIM card responded: No pending AuthRequest.\\n\");\n"
    "\t\tastman_send_error(s, m, \"No pending AuthRequest\\n\");\n"
    "\t\tao2_ref(state, -1);\n"
    "\t\treturn 0;\n"
    "\t}\n"
    "\tresponse = state->client_state->volte_response;\n"
    "\n"
    "\tast_debug(1, \"SIM card responded. RES=%s IK=%s CK=%s AUTS=%s\\n\", res_str, ik_str, ck_str, auts_str);\n"
    "\n"
    "\tcancel_sim_timer(response);\n"
)

ANSWER_PATCHED = (
    "\t/* " + MARKER + ": take the pending response only by cancelling its timer,\n"
    "\t * under the lock sim_timeout_cb clears it under; once the timer has fired the\n"
    "\t * response is the timeout's. */\n"
    "\tresponse = NULL;\n"
    "\tif (state->client_state) {\n"
    "\t\tao2_lock(state->client_state);\n"
    "\t\tif (state->client_state->volte_response &&\n"
    "\t\t\t\tcancel_sim_timer(state->client_state->volte_response)) {\n"
    "\t\t\tresponse = state->client_state->volte_response;\n"
    "\t\t}\n"
    "\t\tao2_unlock(state->client_state);\n"
    "\t}\n"
    "\tif (!response) {\n"
    "\t\tast_debug(1, \"SIM card responded: No pending AuthRequest.\\n\");\n"
    "\t\tastman_send_error(s, m, \"No pending AuthRequest\\n\");\n"
    "\t\tao2_ref(state, -1);\n"
    "\t\treturn 0;\n"
    "\t}\n"
    "\n"
    "\tast_debug(1, \"SIM card responded. RES=%s IK=%s CK=%s AUTS=%s\\n\", res_str, ik_str, ck_str, auts_str);\n"
)


def _replace_once(source: str, old: str, new: str, what: str) -> str:
    at = source.find(old)
    if at < 0:
        raise ValueError(f"{what} not found")
    if source.find(old, at + 1) >= 0:
        raise ValueError(f"{what} is not unique")
    return source[:at] + new + source[at + len(old):]


def patch(source: str) -> str:
    if MARKER in source:
        return source
    source = _replace_once(source, TIMEOUT_ANCHOR, TIMEOUT_REPLACEMENT, "SIM_TIMEOUT definition")
    source = _replace_once(source, CANCEL_ORIGINAL, CANCEL_PATCHED, "cancel_sim_timer")
    source = _replace_once(source, CALLBACK_ORIGINAL, CALLBACK_PATCHED, "sim_timeout_cb")
    source = _replace_once(source, REQUEST_ORIGINAL, REQUEST_PATCHED, "SIM AuthRequest")
    source = _replace_once(source, ANSWER_ORIGINAL, ANSWER_PATCHED, "ami_authresponse claim")
    return source


try:
    original = SOURCE.read_text()
    updated = patch(original)
except (OSError, ValueError) as exc:
    print(f"VoLTE SIM-timeout patch failed: {exc}", file=sys.stderr)
    raise SystemExit(1) from exc

if updated == original:
    print("VoLTE SIM timeout already patched")
else:
    SOURCE.write_text(updated)
    print("patched SIM_TIMEOUT to 10 s, sim_timeout_cb to retry the registration, "
          "and the pending SIM response to be claimed under the client state lock")
