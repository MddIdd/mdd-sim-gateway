"""Issue #90: a browser with no microphone must still be able to call, and must say so.

The reported symptom was "click dial, instant disconnect". The console held the answer —
getUserMedia rejected with NotFoundError, i.e. the machine has no microphone — but JsSIP
collapses every media failure into one cause ('User Denied Media Access') and the call
screen collapsed that into "Call ended". The user was left reading a carrier-shaped failure
for something that never left the page, and chased two websocket ports instead.

WebRTC cannot offer a call without a local audio track, but it does not care what is on it.
A call placed on silence still carries the carrier's audio, which is the whole point of
dialling a voicemail box, a service code or an announcement — so the microphone decides how
the call sounds, not whether it happens. These tests hold that: the call goes out either
way, every surface says which of the two it got, and the substituted track is cleaned up.
"""
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOFTPHONE_LIB = (ROOT / "webui/src/softphone.js").read_text(encoding="utf-8")
SOFTPHONE = (ROOT / "webui/src/views/Softphone.jsx").read_text(encoding="utf-8")
GLOBAL = (ROOT / "webui/src/GlobalSoftphone.jsx").read_text(encoding="utf-8")
I18N = (ROOT / "webui/src/i18n.jsx").read_text(encoding="utf-8")

LISTEN_ONLY = "Listen only · the other side cannot hear you"


def zh_block() -> str:
    return I18N[I18N.index("const zh"):I18N.index("const en")]


def block(source: str, start: str, end: str = "\n  }") -> str:
    body = source[source.index(start):]
    return body[:body.index(end)]


def messages() -> dict:
    """The English strings microphoneMessage() returns, keyed by the reason it maps from."""
    body = block(SOFTPHONE_LIB, "export function microphoneMessage", "\n}")
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


class CallsSurviveAMissingMicrophoneTests(unittest.TestCase):
    def test_the_local_audio_is_acquired_by_us_not_by_jssip(self):
        """JsSIP's own getUserMedia call ends the session when it rejects — that is the bug.
        Passing it a stream (mediaStream) skips that path entirely."""
        acquire = block(SOFTPHONE_LIB, "  async _acquireLocal()")
        self.assertIn("getUserMedia({ audio: true })", acquire)
        self.assertIn("silentAudioStream()", acquire)
        dial = block(SOFTPHONE_LIB, "  async call(number)")
        self.assertIn("mediaStream: local.stream || undefined", dial)

    def test_answering_gets_the_same_fallback_as_dialling(self):
        answer = block(SOFTPHONE_LIB, "  async answer()")
        self.assertIn("await this._acquireLocal()", answer)
        self.assertIn("mediaStream: local.stream || undefined", answer)
        self.assertIn("this.emit('mediafallback'", answer)

    def test_the_silent_track_really_produces_frames(self):
        """A destination node with nothing connected to it can deliver no audio at all; an
        oscillator at zero gain keeps the graph running while staying silent."""
        silent = block(SOFTPHONE_LIB, "function silentAudioStream()", "\n}")
        self.assertIn("createMediaStreamDestination()", silent)
        self.assertIn("gain.gain.value = 0", silent)
        self.assertIn("osc.start()", silent)

    def test_a_stream_we_passed_in_is_released_by_us(self):
        """JsSIP only stops tracks it generated itself, so a microphone handed to it stays
        open — and the browser's recording indicator stays lit — unless we stop it."""
        release = block(SOFTPHONE_LIB, "  _releaseLocal()")
        self.assertIn("track.stop()", release)
        self.assertIn("local.ctx?.close()", release)
        self.assertIn("this._releaseLocal()", block(SOFTPHONE_LIB, "  hangup()"))
        # stop() releases by going through hangup(); a second call would be dead code.
        self.assertIn("this.hangup()", block(SOFTPHONE_LIB, "  stop()"))
        self.assertIn("this._releaseLocal(); this.emit('ended'", SOFTPHONE_LIB)
        self.assertIn("this._releaseLocal(); this.emit('failed'", SOFTPHONE_LIB)

    def test_a_teardown_during_the_prompt_does_not_raise_a_call(self):
        """getUserMedia can sit on a permission prompt for as long as the user likes; the
        line may be switched away in the meantime."""
        dial = block(SOFTPHONE_LIB, "  async call(number)")
        self.assertIn("if (this._dead || !this.ua) { this._releaseLocal(); return }", dial)

    def test_nothing_in_the_dial_path_refuses_the_call(self):
        dial = block(SOFTPHONE, "  const placeCall =", "\n  const answer =")
        self.assertNotIn("audioInputPresence()", dial)
        self.assertIn("phone.current.call(target)", dial)

    def test_the_audio_sink_is_still_primed_inside_the_click(self):
        """unlockAudio() must run before call() awaits anything, or the transient user
        activation is gone and remote audio silently fails to play."""
        dial = block(SOFTPHONE, "  const placeCall =", "\n  const answer =")
        self.assertLess(dial.index("phone.current.unlockAudio()"), dial.index("phone.current.call(target)"))


class SayingSoTests(unittest.TestCase):
    def test_every_failure_the_browser_can_report_has_its_own_wording(self):
        found = messages()
        for reason in ("NotFoundError", "NotAllowedError", "NotReadableError",
                       "insecure", "default"):
            self.assertIn(reason, found, f"no microphone message for {reason}")
        self.assertEqual(len(set(found.values())), 5, "wording must differ per reason")

    def test_no_message_claims_the_call_cannot_be_placed(self):
        for text in messages().values():
            self.assertIn("can still be placed", text, text)
            self.assertIn("will not hear you", text, text)

    def test_every_microphone_message_is_translated(self):
        block_zh = zh_block()
        missing = [text for text in set(messages().values()) if f"'{text}'" not in block_zh]
        self.assertEqual(missing, [], f"untranslated microphone messages: {missing}")
        self.assertIn(f"'{LISTEN_ONLY}'", block_zh)

    def test_the_notice_is_shown_idle_and_for_the_whole_call(self):
        # Idle: the probe needs no permission, so the page can warn before anything is dialled.
        self.assertIn("useState('present')", SOFTPHONE)
        self.assertIn("micPresence === 'none' || micPresence === 'insecure'", SOFTPHONE)
        self.assertIn("'devicechange', probe", SOFTPHONE)
        # In call: a toast scrolls away, the call screen does not.
        self.assertIn(f"t('{LISTEN_ONLY}')", SOFTPHONE)
        self.assertEqual(SOFTPHONE.count("<ListenOnlyNote t={t} />"), 2,
                         "both the ringing and the connected screen must say it")

    def test_the_flag_belongs_to_one_call(self):
        """A call placed before the headset was unplugged is still a normal call; the notice
        must come from what THIS call actually got, not from the current device list."""
        self.assertIn("const [listenOnly, setListenOnly] = useState(null)", SOFTPHONE)
        self.assertIn("type === 'mediafallback') { setListenOnly(data)", SOFTPHONE)
        self.assertIn("setListenOnly(null); setCall({ dir: 'out'", SOFTPHONE)
        self.assertIn("if (!call) setListenOnly(null)", SOFTPHONE)

    def test_mute_is_not_offered_on_a_silent_track(self):
        """A toggle that cannot change anything reads as a broken button."""
        self.assertIn("disabled={Boolean(listenOnly)}", SOFTPHONE)
        self.assertIn("function RoundBtn({ icon, label, color, bg, onClick, active, disabled = false })", SOFTPHONE)

    def test_the_global_overlay_says_it_too(self):
        # Answering from the overlay has no dialler screen to fall back on.
        self.assertIn("type === 'mediafallback'", GLOBAL)
        self.assertIn("{ ...current, listenOnly: true }", GLOBAL)
        self.assertIn(f"t('{LISTEN_ONLY}')", GLOBAL)

    def test_a_hard_media_failure_is_still_named(self):
        """If even a silent track cannot be built, JsSIP asks for the microphone itself and
        the call does die — with the one generic cause the UI used to render as 'Call ended'."""
        self.assertIn("export const MEDIA_FAIL_CAUSE = 'User Denied Media Access'", SOFTPHONE_LIB)
        self.assertIn("session.on('getusermediafailed'", SOFTPHONE_LIB)
        self.assertIn("c === MEDIA_FAIL_CAUSE ? 'Microphone unavailable'", SOFTPHONE)
        self.assertIn("'Microphone unavailable':", zh_block())


class RegistrationGuardTests(unittest.TestCase):
    def test_the_call_button_reads_the_registration_state_it_displays(self):
        dial = block(SOFTPHONE, "  const placeCall =", "\n  const answer =")
        guard = re.search(r"\[([^\]]+)\]\.includes\(reg\)", dial)
        self.assertIsNotNone(guard, "the Call button does not consult the registration state")
        blocked = set(re.findall(r"'([a-z]+)'", guard.group(1)))
        self.assertEqual(blocked, {"disconnected", "failed", "unregistered"})
        # A websocket that is still opening may well carry the call; do not block it.
        self.assertNotIn("connecting", blocked)


if __name__ == "__main__":
    unittest.main()
