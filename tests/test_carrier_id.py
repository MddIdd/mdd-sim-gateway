import unittest
from unittest.mock import patch

from control.app import carrier_id


class CarrierIdTests(unittest.TestCase):
    def test_exact_three_digit_mnc_wins_over_an_earlier_two_digit_record(self):
        value = carrier_id.lookup({"mcc": "405", "mnc": "045"})
        self.assertEqual(value["name"], "TATA DOCOMO")
        self.assertEqual(value["plmn"], "405-045")

    def test_exact_record_wins_even_when_shorter_plmn_has_more_matching_attributes(self):
        carriers = [
            {"carrier_name": "Short", "attributes": [
                {"mccmnc_tuple": ["40545"], "spn": ["Test"]}]},
            {"carrier_name": "Exact", "attributes": [{"mccmnc_tuple": ["405045"]}]},
        ]
        with patch.object(carrier_id, "_database", return_value=(carriers, {}, 1)):
            value = carrier_id.lookup({"mcc": "405", "mnc": "045",
                                       "carrier_identity": {"spn": "Test"}})
        self.assertEqual(value["name"], "Exact")

    def test_exact_parent_fallback_wins_over_an_unconditional_shorter_plmn(self):
        carriers = [
            {"carrier_name": "Short", "attributes": [{"mccmnc_tuple": ["40545"]}]},
            {"carrier_name": "Exact", "attributes": [
                {"mccmnc_tuple": ["405045"], "spn": ["Missing"]}]},
        ]
        with patch.object(carrier_id, "_database", return_value=(carriers, {}, 1)):
            value = carrier_id.lookup({"mcc": "405", "mnc": "045"})
        self.assertEqual(value["name"], "Exact")
        self.assertEqual(value["plmn"], "405-045")

    def test_plain_plmn_resolves_the_home_network(self):
        value = carrier_id.lookup({"mcc": "234", "mnc": "10"})
        self.assertEqual(value["name"], "O2")
        self.assertEqual(value["home_network"], "O2")
        self.assertEqual(value["plmn"], "234-10")
        self.assertFalse(value["specific"])

    def test_spn_selects_a_specific_mvno(self):
        value = carrier_id.lookup({
            "mcc": "234", "mnc": "10",
            "carrier_identity": {"spn": "GIFFGAFF"},
        })
        self.assertEqual(value["name"], "giffgaff")
        self.assertEqual(value["home_network"], "O2")
        self.assertEqual(value["match_source"], "mccmnc+spn")
        self.assertTrue(value["specific"])

    def test_gid_prefix_is_case_insensitive_and_tolerates_sim_padding(self):
        value = carrier_id.lookup({
            "mcc": "310", "mnc": "240",
            "carrier_identity": {"gid1": "354dffffffff"},
        })
        self.assertEqual(value["name"], "Ultra/Univision")
        self.assertEqual(value["home_network"], "T-Mobile - US")

    def test_legacy_three_digit_padding_recovers_a_two_digit_mnc(self):
        value = carrier_id.lookup({"mcc": "234", "mnc": "015"})
        self.assertEqual(value["name"], "Vodafone")
        self.assertEqual(value["plmn"], "234-15")

    def test_unknown_plmn_stays_explicit(self):
        value = carrier_id.lookup({"mcc": "999", "mnc": "99"})
        self.assertEqual(value["name"], "")
        self.assertEqual(value["plmn"], "999-99")
        self.assertEqual(value["database"], "none")


if __name__ == "__main__":
    unittest.main()
