"""A slow SIM answer must not leave the line silently unregistered for an hour.

A SIM behind a Quectel EC25 answers the IMS-AKA challenge in ~2.6 s; sysmocom waited 3 s and,
on timeout, only logged and dropped the pending response. No retry was scheduled and the status
stayed "Registered" until the carrier closed the transport, ~70 minutes later.

The timer and a late AMI answer run on different threads and both consume the one pending
response, so it is claimed under the client state's lock.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATCHER = ROOT / "engine" / "patches" / "asterisk" / "volte_sim_timeout_retry.py"

# The pinned sysmocom source, verbatim, around the two anchors.
SOURCE = (
    "#define SIM_TIMEOUT 3\n"
    "\n"
    "/*! \\brief Timer callback function, used just for registrations */\n"
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
    "\n"
    "static void cancel_sim_timer(struct registration_response *response)\n"
    "{\n"
    "\tif (pj_timer_heap_cancel_if_active(pjsip_endpt_get_timer_heap(ast_sip_get_pjsip_endpoint()),\n"
    "\t\t&response->sim_timer, response->sim_timer.id)) {\n"
    "\t}\n"
    "}\n"
    "\n"
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
    "\t\t\tret = 0;\n"
    "\t\t\tgoto out;\n"
    "\t\t}\n"
    "\n"
    "static int ami_authresponse(struct mansession *s, const struct message *m)\n"
    "{\n"
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
    "\n"
    "\tif (res_str[0] && ik_str[0] && ck_str[0] && !auts_str[0]) {\n"
)


class VolteSimTimeoutPatchTests(unittest.TestCase):
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

    def test_the_timeout_leaves_room_for_an_ec25_answer(self):
        first, patched, twice, second = self._apply(SOURCE)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("#define SIM_TIMEOUT 10\n", patched)
        self.assertNotIn("#define SIM_TIMEOUT 3\n", patched)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(twice, patched)

    def test_a_timeout_takes_the_sim_failed_path_so_the_registration_retries(self):
        _first, patched, _twice, _second = self._apply(SOURCE)
        callback = patched[patched.index("static void sim_timeout_cb"):]

        # The same handling as the bridge's own "SIM failed" reply, which reaches volte_failed
        # and schedules fatal_retry_interval.
        self.assertIn("volte_set_state(response->client_state, VOLTE_STATE_SIM_FAILED);",
                      callback)
        self.assertIn("ast_sip_push_task(response->client_state->serializer,\n"
                      "\t\t\t\thandle_registration_response, response)", callback)
        # The task is declared before use: handle_registration_response is defined later.
        self.assertLess(patched.index("static int handle_registration_response(void *data);"),
                        patched.index("static void sim_timeout_cb"))

    def test_a_late_answer_cannot_process_the_response_twice(self):
        _first, patched, _twice, _second = self._apply(SOURCE)
        callback = patched[patched.index("static void sim_timeout_cb"):]

        clear = callback.index("response->client_state->volte_response = NULL;")
        queue = callback.index("ast_sip_push_task(")
        self.assertLess(clear, queue)
        # Only the response still pending is requeued.
        self.assertIn("pending = response->client_state->volte_response == response;", callback)

    def test_each_path_releases_the_response_exactly_once(self):
        _first, patched, _twice, _second = self._apply(SOURCE)
        callback = patched[patched.index("static void sim_timeout_cb"):]
        callback = callback[:callback.index("\n}\n") + 3]

        destroy = callback[callback.index("if (response->client_state->destroy)"):]
        destroy = destroy[:destroy.index("return;")]
        self.assertIn("ao2_ref(response, -1);", destroy)
        # When the task takes the reference the callback returns without releasing it.
        queued = callback[callback.index("ast_sip_push_task("):]
        self.assertLess(queued.index("return;"), queued.index("ao2_ref(response, -1);"))

    def _function(self, patched, signature):
        body = patched[patched.index(signature):]
        return body[:body.index("\n}\n") + 3]

    def test_the_timeout_claims_the_response_under_the_client_state_lock(self):
        _first, patched, _twice, _second = self._apply(SOURCE)
        callback = self._function(patched, "static void sim_timeout_cb")

        lock = callback.index("ao2_lock(response->client_state);")
        check = callback.index("response->client_state->volte_response == response")
        clear = callback.index("response->client_state->volte_response = NULL;")
        unlock = callback.index("ao2_unlock(response->client_state);")
        queue = callback.index("ast_sip_push_task(")
        # Checked and cleared inside the lock; queued outside it.
        self.assertLess(lock, check)
        self.assertLess(check, clear)
        self.assertLess(clear, unlock)
        self.assertLess(unlock, queue)

    def test_a_late_answer_takes_the_response_only_by_cancelling_its_timer(self):
        _first, patched, _twice, _second = self._apply(SOURCE)
        answer = patched[patched.index("static int ami_authresponse"):]
        answer = answer[:answer.index("if (res_str[0]")]

        lock = answer.index("ao2_lock(state->client_state);")
        cancel = answer.index("cancel_sim_timer(state->client_state->volte_response)")
        take = answer.index("response = state->client_state->volte_response;")
        unlock = answer.index("ao2_unlock(state->client_state);")
        self.assertLess(lock, cancel)
        self.assertLess(cancel, take)
        self.assertLess(take, unlock)
        # A timer that already fired leaves no response: the answer is refused, not processed.
        refuse = answer.index("if (!response) {")
        self.assertLess(unlock, refuse)
        self.assertIn("No pending AuthRequest", answer[refuse:])
        # The old unconditional cancel after taking the response is gone.
        self.assertEqual(answer.count("cancel_sim_timer("), 1)

    def test_cancelling_reports_whether_the_timer_was_still_pending(self):
        _first, patched, _twice, _second = self._apply(SOURCE)
        cancel = self._function(patched, "static int cancel_sim_timer")

        self.assertIn("return pj_timer_heap_cancel_if_active(", cancel)
        self.assertIn(") > 0;", cancel)
        self.assertNotIn("static void cancel_sim_timer", patched)

    def test_the_response_is_pending_with_its_timer_armed_before_the_sim_is_asked(self):
        _first, patched, _twice, _second = self._apply(SOURCE)
        request = patched[patched.index("\t\tif (auth->usim_ami) {"):]
        request = request[:request.index("\t\t}\n\n")]

        lock = request.index("ao2_lock(response->client_state);")
        publish = request.index("response->client_state->volte_response = response;")
        arm = request.index("pjsip_endpt_schedule_timer(")
        unlock = request.index("ao2_unlock(response->client_state);")
        ask = request.index("volte_send_authrequest(")
        self.assertLess(lock, publish)
        self.assertLess(publish, arm)
        self.assertLess(arm, unlock)
        self.assertLess(unlock, ask)
        # A timer that cannot be armed does not ask the SIM.
        self.assertLess(request.index("goto out;"), ask)

    def test_an_upstream_refactor_fails_the_build_instead_of_being_skipped(self):
        first, _patched, _twice, _second = self._apply(
            SOURCE.replace("#define SIM_TIMEOUT 3", "#define SIM_TIMEOUT_SECONDS 3"))

        self.assertEqual(first.returncode, 1)
        self.assertIn("SIM_TIMEOUT definition not found", first.stderr)

        first, _patched, _twice, _second = self._apply(
            SOURCE.replace("\tao2_ref(response, -1);\n}\n", "\tao2_cleanup(response);\n}\n"))
        self.assertEqual(first.returncode, 1)
        self.assertIn("sim_timeout_cb not found", first.stderr)

        first, _patched, _twice, _second = self._apply(
            SOURCE.replace("\tcancel_sim_timer(response);\n", ""))
        self.assertEqual(first.returncode, 1)
        self.assertIn("ami_authresponse claim not found", first.stderr)


if __name__ == "__main__":
    unittest.main()
