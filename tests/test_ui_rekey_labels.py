"""The two rekey settings used to read as "Rekey" and "IKE rekey", which says nothing about how
they differ, and a line whose ePDG renews the data-channel keys itself showed its 0 as "0
minutes" — indistinguishable from a misconfiguration.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGES = (ROOT / "webui" / "src" / "views" / "UnifiedPages.jsx").read_text(encoding="utf-8")
I18N = (ROOT / "webui" / "src" / "i18n.jsx").read_text(encoding="utf-8")
ZH = I18N[I18N.index("const zh"):I18N.index("const en")]
MAIN = (ROOT / "control" / "app" / "main.py").read_text(encoding="utf-8")

LABELS = [
    "Data channel rekey",
    "Control channel rekey",
    "Initiated by carrier",
    "Data channel rekey interval (minutes, 0 = not initiated by the gateway)",
    "Control channel rekey interval (minutes, 0 = off)",
]


class RekeyLabelTests(unittest.TestCase):
    def test_labels_are_used_and_translated(self):
        for label in LABELS:
            self.assertIn(f"t('{label}'", PAGES.replace("t(d.vowifi?.accept_epdg_rekey ? '", "t('"),
                          label)
            self.assertTrue(f"'{label}':" in ZH or f"{label}:" in ZH, f"missing zh: {label}")

    def test_old_ambiguous_labels_are_gone(self):
        for old in ("t('Rekey')", "t('IKE rekey')", "t('Rekey minutes')", "t('IKE rekey minutes')"):
            self.assertNotIn(old, PAGES)

    def test_zero_data_channel_rekey_says_who_renews_the_keys(self):
        detail = PAGES[PAGES.index("t('Data channel rekey')"):]
        detail = detail[:detail.index("</b>")]
        self.assertIn("=== 0", detail)
        self.assertIn("accept_epdg_rekey ? 'Initiated by carrier' : 'Off'", detail)

    def test_api_exposes_whether_the_line_accepts_the_epdg_rekey(self):
        block = MAIN[MAIN.index('"vowifi": {"epdg"'):]
        block = block[:block.index('"egress"')]
        self.assertIn('"accept_epdg_rekey"', block)
        # Same key and fallback as the engine config, so the label cannot disagree with what runs.
        self.assertIn('get("accept_epdg_esp_rekey"', block)
        self.assertIn('get("accept_epdg", False)', block)


if __name__ == "__main__":
    unittest.main()
