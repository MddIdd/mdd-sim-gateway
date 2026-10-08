import unittest

from control.app import esim_identity


class ModemIdentityConvergenceTests(unittest.TestCase):
    TARGET = {"iccid": "89000000000000000011", "imsi": "001010000000001"}

    def test_matching_iccid_is_converged(self):
        self.assertTrue(esim_identity.identity_matches(
            {"iccid": self.TARGET["iccid"], "imsi": ""}, self.TARGET))

    def test_matching_imsi_is_converged_when_modemmanager_has_no_iccid(self):
        self.assertTrue(esim_identity.identity_matches(
            {"iccid": "", "imsi": self.TARGET["imsi"]}, self.TARGET))

    def test_previous_imsi_is_not_converged(self):
        self.assertFalse(esim_identity.identity_matches(
            {"iccid": "", "imsi": "001010000000002"}, self.TARGET))

    def test_different_iccid_is_not_converged_even_with_matching_imsi(self):
        self.assertFalse(esim_identity.identity_matches(
            {"iccid": "89000000000000000022", "imsi": self.TARGET["imsi"]}, self.TARGET))

    def test_placeholder_iccid_does_not_block_imsi_match(self):
        self.assertTrue(esim_identity.identity_matches(
            {"iccid": "--", "imsi": self.TARGET["imsi"]}, self.TARGET))

    def test_missing_target_identity_cannot_match(self):
        self.assertFalse(esim_identity.identity_matches(
            {"iccid": "", "imsi": self.TARGET["imsi"]}, {"iccid": "", "imsi": ""}))


if __name__ == "__main__":
    unittest.main()
