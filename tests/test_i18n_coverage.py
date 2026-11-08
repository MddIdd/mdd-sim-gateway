"""Every sentence the backend sends to the browser must have a Chinese translation.

The WebUI uses the English sentence itself as the lookup key and falls back to the key when
there is no entry, so a missing translation is invisible until a user reaches that exact
state — a wrong-card binding fault shipped untranslated for exactly that reason. Reading the
tables from both sides here is what makes the gap fail a build instead of a support thread.
"""
import re
import unittest
from pathlib import Path

from control.app import status as status_mod

ROOT = Path(__file__).resolve().parent.parent
I18N = ROOT / "webui" / "src" / "i18n.jsx"
HISTORY = ROOT / "webui" / "src" / "views" / "VowifiHistory.jsx"


def zh_block() -> str:
    text = I18N.read_text(encoding="utf-8")
    return text[text.index("const zh"):text.index("const en")]


def translated(value: str, block: str) -> bool:
    # Keys are single- or double-quoted; those containing an apostrophe use double quotes.
    return f"'{value}'" in block or f'"{value}"' in block


def js_map(source: str, name: str) -> dict:
    block = source[source.index(f"const {name} = {{"):]
    block = block[:block.index("\n}")]
    pairs = re.findall(r"(\w+):\s*(?:'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\")", block)
    return {key: (single or double).replace("\\'", "'") for key, single, double in pairs}


class BackendStringCoverageTests(unittest.TestCase):
    def test_every_device_capability_reason_is_translated(self):
        """`reason="..."` on a device capability is shown through t() on the device page.
        The draft line's IMEI hint shipped in English in the Chinese UI for this reason."""
        source = (ROOT / "control" / "app" / "main.py").read_text(encoding="utf-8")
        reasons = set(re.findall(r'reason=\(?\s*"([^"{}]+)"', source))
        self.assertTrue(reasons)
        block = zh_block()
        self.assertEqual(sorted(r for r in reasons if not translated(r, block)), [])

    def test_every_status_reason_is_translated(self):
        block = zh_block()
        missing = [code for code, text in status_mod.REASONS.items()
                   if not translated(text, block)]
        self.assertEqual(missing, [], f"untranslated status reasons: {missing}")

    def test_every_modem_ims_message_is_translated(self):
        source = (ROOT / "control" / "app" / "modem_ims.py").read_text(encoding="utf-8")
        messages = set(re.findall(r'"reason":\s*"([^"]+)"', source))
        messages |= set(re.findall(r'"reason":\s*\(?\s*"([^"]+)"', source))
        self.assertTrue(messages)
        block = zh_block()
        self.assertEqual(sorted(m for m in messages if not translated(m, block)), [])

    def test_every_vowifi_support_reason_is_translated(self):
        from control.app import vowifi_support
        block = zh_block()
        missing = [key for key, text in vowifi_support.REASONS.items()
                   if not translated(text, block)]
        self.assertEqual(missing, [], f"untranslated VoWiFi support reasons: {missing}")

    def test_the_degraded_vowifi_reason_is_translated(self):
        self.assertTrue(translated("VoWiFi is enabled but no configured line is running",
                                   zh_block()))

    def test_the_degraded_cellular_reasons_are_translated(self):
        block = zh_block()
        self.assertTrue(translated("Cellular data bearer is not connected", block))
        self.assertTrue(translated("Waiting for cellular registration ({registration})", block))

    def test_every_modem_voice_reason_is_translated(self):
        from control.app import modem_voice
        block = zh_block()
        missing = [key for key, text in modem_voice.REASONS.items()
                   if not translated(text, block)]
        self.assertEqual(missing, [], f"untranslated call-audio reasons: {missing}")

    def test_every_status_label_is_translated(self):
        block = zh_block()
        missing = [code for code, text in status_mod.LABELS.items()
                   if not translated(text, block)]
        self.assertEqual(missing, [], f"untranslated status labels: {missing}")

    def test_the_timeline_can_label_every_reason_the_backend_records(self):
        # An unmapped code renders as the raw code, because reasonLabel falls back to it.
        known = js_map(HISTORY.read_text(encoding="utf-8"), "REASON_LABEL")
        # 'ok' is never written as an outage cause; only down segments carry a reason.
        missing = [code for code in status_mod.REASONS if code != "ok" and code not in known]
        self.assertEqual(missing, [], f"timeline has no label for: {missing}")

    def test_every_timeline_label_is_translated(self):
        block = zh_block()
        source = HISTORY.read_text(encoding="utf-8")
        for name in ("REASON_LABEL", "EVIDENCE_LABEL"):
            missing = [key for key, text in js_map(source, name).items()
                       if not translated(text, block)]
            self.assertEqual(missing, [], f"untranslated {name}: {missing}")


if __name__ == "__main__":
    unittest.main()
