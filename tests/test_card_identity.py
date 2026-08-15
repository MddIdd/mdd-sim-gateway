import unittest
from unittest.mock import patch

from control.app import main


class CardIdentityGuardTests(unittest.TestCase):
    def test_same_swu_reader_name_with_different_iccid_is_a_mismatch(self):
        instance = {
            "id": "line-target",
            "iccid": "profile-target",
            "swu_reader": "slot 1",
            "reader_index": 1,
            "reader_port": "1-2",
        }
        live_card = {
            "name": "slot 1",
            "index": 1,
            "present": True,
            "iccid": "profile-other",
            "reader_port": "1-2",
        }

        with patch.object(main.hub, "cards", {"slot-1": live_card}):
            mismatch = main._card_identity_mismatch(instance)

        self.assertIsNotNone(mismatch)
        self.assertEqual(mismatch["iccid"], live_card["iccid"])
