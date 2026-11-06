"""Regression coverage for per-device capability operation state."""
import re
import unittest
from pathlib import Path


SOURCE = (Path(__file__).resolve().parent.parent
          / "webui/src/views/UnifiedPages.jsx").read_text(encoding="utf-8")


class CapabilitySwitchStateTests(unittest.TestCase):
    def test_every_capability_switch_is_keyed_by_device_and_capability(self):
        switches = re.findall(r"<CapabilitySwitch\b[^>]*>", SOURCE)
        self.assertTrue(switches)
        for switch in switches:
            kind = re.search(r'kind="([^"]+)"', switch)
            key = re.search(r'key=\{`\$\{d\.id\}:([^`]+)`\}', switch)
            self.assertIsNotNone(kind, switch)
            self.assertIsNotNone(key, switch)
            self.assertEqual(key.group(1), kind.group(1), switch)

    def test_enabled_capability_can_be_disabled_during_starting_or_stopping_when_idle(self):
        unavailable_decl = "const unavailable = !c.available"
        can_disable_decl = (
            "const canDisableWhileStarting = !submitting && c.desired && "
            "(c.actual === 'starting' || c.actual === 'stopping')"
        )
        switch_disabled_decl = (
            "const switchDisabled = (pending && !canDisableWhileStarting) || unavailable"
        )
        self.assertIn(unavailable_decl, SOURCE)
        self.assertIn(can_disable_decl, SOURCE)
        self.assertIn(switch_disabled_decl, SOURCE)
        self.assertLess(
            SOURCE.index(unavailable_decl),
            SOURCE.index(switch_disabled_decl),
            "unavailable must be declared before switchDisabled to avoid a TDZ ReferenceError",
        )
        self.assertIn("disabled={switchDisabled}", SOURCE)

    def test_registration_reason_uses_translatable_template(self):
        self.assertIn(
            "t('Waiting for cellular registration ({registration})', "
            "{ registration: reasonMatch[1] })",
            SOURCE,
        )


if __name__ == "__main__":
    unittest.main()
