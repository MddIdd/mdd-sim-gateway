"""Issue #90: a dial that dies in the browser must say so, in the browser's terms.

The reported symptom was "click dial, instant disconnect". The console held the answer —
getUserMedia rejected with NotFoundError, i.e. the machine has no microphone — but JsSIP
collapses every media failure into one cause ('User Denied Media Access') and the call
screen collapsed that into "Call ended". The user was left reading a carrier-shaped
failure for something that never left the page, and chased two websocket ports instead.

These tests hold the three pieces that make the real reason reachable: a probe that runs
before the call, a named cause after it, and a Call button that reads the status dot it
already draws.
"""
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOFTPHONE_LIB = (ROOT / "webui/src/softphone.js").read_text(encoding="utf-8")
SOFTPHONE = (ROOT / "webui/src/views/Softphone.jsx").read_text(encoding="utf-8")
GLOBAL = (ROOT / "webui/src/GlobalSoftphone.jsx").read_text(encoding="utf-8")
I18N = (ROOT / "webui/src/i18n.jsx").read_text(encoding="utf-8")


def zh_block() -> str:
    return I18N[I18N.index("const zh"):I18N.index("const en")]


def messages() -> dict:
    """The English strings microphoneMessage() returns, keyed by the reason it maps from."""
    body = SOFTPHONE_LIB[SOFTPHONE_LIB.index("export function microphoneMessage"):]
    body = body[:body.index("\n}")]
    out, pending = {}, []
    for line in body.splitlines():
        case = re.match(r"\s*case '([A-Za-z]+)':", line)
        if case:
            pending.append(case.group(1))
            continue
        ret = re.match(r"\s*return '(.+)'$", line.rstrip())
        if ret:
            for reason in pending or ["default"]:
                out[reason] = ret.group(1)
            pending = []
    return out


class MicrophoneProbeTests(unittest.TestCase):
    def test_presence_is_decided_without_opening_the_device(self):
        """enumerateDevices() needs no permission and starts no capture, so the warning can
        be shown before the user ever clicks. Opening the microphone to test it would
        prompt, and would hand JsSIP a device it then has to re-acquire."""
        probe = SOFTPHONE_LIB[SOFTPHONE_LIB.index("export async function audioInputPresence"):]
        probe = probe[:probe.index("\n}")]
        self.assertIn("enumerateDevices()", probe)
        self.assertNotIn("getUserMedia({", probe)
        self.assertIn("device.kind === 'audioinput'", probe)

    def test_a_withheld_device_list_is_not_read_as_a_missing_microphone(self):
        """Browsers that hide device info until permission is granted return an empty list.
        Warning on that would accuse every such browser of having no microphone."""
        probe = SOFTPHONE_LIB[SOFTPHONE_LIB.index("export async function audioInputPresence"):]
        probe = probe[:probe.index("\n}")]
        self.assertIn("if (!devices.length) return 'unknown'", probe)
        self.assertIn("catch { return 'unknown' }", probe)

    def test_the_banner_only_appears_once_the_probe_has_answered(self):
        self.assertIn("useState('present')", SOFTPHONE)
        self.assertIn("micPresence === 'none' || micPresence === 'insecure'", SOFTPHONE)
        self.assertIn("'devicechange', probe", SOFTPHONE)

    def test_every_failure_the_browser_can_report_has_its_own_advice(self):
        found = messages()
        for reason in ("NotFoundError", "NotAllowedError", "NotReadableError",
                       "insecure", "default"):
            self.assertIn(reason, found, f"no microphone message for {reason}")
        self.assertEqual(len(set(found.values())), 5, "advice must differ per reason")

    def test_every_microphone_message_is_translated(self):
        block = zh_block()
        missing = [text for text in set(messages().values()) if f"'{text}'" not in block]
        self.assertEqual(missing, [], f"untranslated microphone messages: {missing}")


class PreDialGuardTests(unittest.TestCase):
    def test_the_microphone_is_checked_before_the_invite(self):
        dial = SOFTPHONE[SOFTPHONE.index("const placeCall ="):]
        dial = dial[:dial.index("\n  const answer =")]
        self.assertLess(dial.index("await audioInputPresence()"), dial.index("phone.current.call(target)"))
        self.assertIn("toast(t(microphoneMessage(presence)))", dial)

    def test_the_audio_sink_is_still_primed_inside_the_click(self):
        """unlockAudio() must run before the first await or the transient user activation is
        gone and remote audio silently fails to play later."""
        dial = SOFTPHONE[SOFTPHONE.index("const placeCall ="):]
        dial = dial[:dial.index("\n  const answer =")]
        self.assertLess(dial.index("phone.current.unlockAudio()"), dial.index("await audioInputPresence()"))

    def test_the_call_button_reads_the_registration_state_it_displays(self):
        dial = SOFTPHONE[SOFTPHONE.index("const placeCall ="):]
        dial = dial[:dial.index("\n  const answer =")]
        guard = re.search(r"\[([^\]]+)\]\.includes\(reg\)", dial)
        self.assertIsNotNone(guard, "the Call button does not consult the registration state")
        blocked = set(re.findall(r"'([a-z]+)'", guard.group(1)))
        self.assertEqual(blocked, {"disconnected", "failed", "unregistered"})
        # A websocket that is still opening may well carry the call; do not block it.
        self.assertNotIn("connecting", blocked)


class NamedCauseTests(unittest.TestCase):
    def test_a_media_failure_is_not_reported_as_a_finished_call(self):
        self.assertIn("export const MEDIA_FAIL_CAUSE = 'User Denied Media Access'", SOFTPHONE_LIB)
        self.assertIn("c === MEDIA_FAIL_CAUSE ? 'Microphone unavailable'", SOFTPHONE)
        self.assertIn("'Microphone unavailable':", zh_block())

    def test_the_browser_s_own_error_name_is_carried_up(self):
        """JsSIP fires the session's 'failed' BEFORE 'getusermediafailed', so the generic
        cause arrives first and only this event carries the DOMException name."""
        self.assertIn("session.on('getusermediafailed'", SOFTPHONE_LIB)
        self.assertIn("this.emit('mediafail', (err && err.name) || 'MediaError')", SOFTPHONE_LIB)

    def test_both_call_surfaces_explain_it(self):
        # Answering an incoming call starts with getUserMedia too, and the global overlay
        # has no pre-dial check to fall back on.
        self.assertIn("type === 'mediafail'", SOFTPHONE)
        self.assertIn("type === 'mediafail'", GLOBAL)
        self.assertIn("microphoneMessage(data)", GLOBAL)


if __name__ == "__main__":
    unittest.main()
