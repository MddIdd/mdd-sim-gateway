import os
import tempfile
import unittest
from unittest.mock import patch

from control.app import config


class PortAllocationTests(unittest.TestCase):
    def test_new_blocks_use_the_compact_rtp_span(self):
        block = config._alloc_ports(0)
        self.assertEqual(block["rtp_span"], config.DEFAULT_RTP_SPAN)
        self.assertEqual(config.rtp_span(block), 12)
        self.assertEqual(len(config._block_ports(block)), 15)

    def test_saved_blocks_without_span_keep_legacy_width(self):
        block = {key: value for key, value in config._alloc_ports(0).items()
                 if key != "rtp_span"}
        self.assertEqual(config.rtp_span(block), config.LEGACY_RTP_SPAN)
        self.assertIn(block["rtp_start"] + 59, config._block_ports(block))

    def test_the_softphone_takes_no_host_port(self):
        # It reaches the engine through the control surface relay, so neither a new block nor
        # the reservation of a block saved by an older version holds a port for it.
        self.assertNotIn("webrtc", config._alloc_ports(0))
        saved = {**config._alloc_ports(0), "webrtc": 8089}
        self.assertNotIn(8089, config._block_ports(saved))
        with patch.object(config, "_host_port_free", lambda port: port != 8089):
            self.assertTrue(config._block_free(saved, set()))

    def test_rendered_instance_has_no_softphone_listen_address(self):
        base = {
            "id": "1", "imsi": "001010000000001", "mcc": "001", "mnc": "01",
            "imei": "123456789012345", "ami_secret": "secret",
            "sip": {"listen_addr": "0.0.0.0", "webrtc": {"password": "password"}},
            "ports": config._alloc_ports(0),
        }
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "CONFIG_PATH", os.path.join(temp, "config.yaml")):
            rendered = config.render_instance_json(base, config.DEFAULTS["settings"])
        self.assertNotIn("listen_addr", rendered["sip"])
        self.assertNotIn("port", rendered["sip"]["webrtc"])

    def test_rendered_rtp_end_matches_the_effective_span(self):
        base = {
            "id": "1", "imsi": "001010000000001", "mcc": "001", "mnc": "01",
            "imei": "123456789012345", "ami_secret": "secret",
            "sip": {"webrtc": {"password": "password"}},
        }
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "CONFIG_PATH", os.path.join(temp, "config.yaml")):
            compact = {**base, "ports": config._alloc_ports(0)}
            rendered = config.render_instance_json(compact, config.DEFAULTS["settings"])
            self.assertEqual(rendered["rtp_end"], rendered["rtp_start"] + 11)

            legacy_ports = {key: value for key, value in config._alloc_ports(0).items()
                            if key != "rtp_span"}
            legacy = {**base, "ports": legacy_ports}
            rendered = config.render_instance_json(legacy, config.DEFAULTS["settings"])
            self.assertEqual(rendered["rtp_end"], rendered["rtp_start"] + 59)


if __name__ == "__main__":
    unittest.main()
