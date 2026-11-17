"""A saved MTU must reach rebuilt lines, even when Control has a legacy override."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

from control.app import config as cfg, engine, main


class VowifiMtuSettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "config.yaml"
        for context in (
            patch.multiple(cfg, DATA_DIR=temp.name, CONFIG_PATH=str(self.path), _loaded=None),
            patch.dict(os.environ, {"SWU_TUN_MTU": ""}),
            patch.object(main.egress, "publish"),
        ):
            context.start()
            self.addCleanup(context.stop)

    def test_fresh_install_defaults_to_1280(self):
        self.assertEqual(main.api_get_settings()["vowifi_mtu"], 1280)

    def test_legacy_environment_is_shown_and_saved_setting_wins_after_reload(self):
        cfg.save({"settings": {"timezone": "UTC"}, "instances": {}})
        with patch.dict(os.environ, {"SWU_TUN_MTU": "1400"}):
            self.assertEqual(main.api_get_settings()["vowifi_mtu"], 1400)
            saved = main.api_put_settings({"vowifi_mtu": 1300})
            self.assertEqual(saved["vowifi_mtu"], 1300)
            cfg._loaded = None
            self.assertEqual(main.api_get_settings()["vowifi_mtu"], 1300)
            self.assertEqual(cfg.vowifi_mtu(cfg.get_settings()), 1300)
        self.assertEqual(yaml.safe_load(self.path.read_text())["settings"]["vowifi_mtu"], 1300)

    def test_a_fresh_config_also_preserves_the_legacy_environment(self):
        with patch.dict(os.environ, {"SWU_TUN_MTU": "1350"}):
            self.assertEqual(main.api_get_settings()["vowifi_mtu"], 1350)

    def test_partial_settings_save_preserves_mtu(self):
        main.api_put_settings({"vowifi_mtu": 1400})
        self.assertEqual(main.api_put_settings({"timezone": "UTC"})["vowifi_mtu"], 1400)

    def test_valid_values_save_without_restarting_lines(self):
        with patch.object(main.engine, "start") as start, patch.object(main.engine, "stop") as stop:
            for value in (1280, 1300, 1500):
                with self.subTest(value=value):
                    self.assertEqual(main.api_put_settings({"vowifi_mtu": value})["vowifi_mtu"], value)
            start.assert_not_called()
            stop.assert_not_called()

    def test_invalid_api_values_do_not_partially_save_other_fields(self):
        main.api_put_settings({"vowifi_mtu": 1300})
        original = self.path.read_bytes()
        for value in (1279, 1501, 0, -1, 1280.5, 1280.0, True, False, None, "1280", "", [], {}):
            with self.subTest(value=value):
                with self.assertRaises(main.HTTPException) as exc:
                    main.api_put_settings({"vowifi_mtu": value, "timezone": "UTC"})
                self.assertEqual(exc.exception.status_code, 400)
                self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_legacy_or_hand_edited_values_use_the_safe_default(self):
        for value in ("0", "1279", "1501", "bad", "1280.5", "9" * 5000):
            with self.subTest(value=value[:20]), patch.dict(os.environ, {"SWU_TUN_MTU": value}):
                self.assertEqual(cfg.vowifi_mtu({}), 1280)
        for value in (True, None, "1400", 1279, 1501):
            with self.subTest(saved=value), patch.dict(os.environ, {"SWU_TUN_MTU": "1400"}):
                self.assertEqual(cfg.vowifi_mtu({"vowifi_mtu": value}), 1280)

    def test_rebuilt_engines_receive_the_saved_value_or_compatible_default(self):
        client = Mock()
        client.containers.get.side_effect = engine.docker.errors.NotFound("sim1")
        client.containers.run.return_value = SimpleNamespace(id="new", name="engine")
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(engine, "_client", return_value=client), \
                patch.object(engine, "ENGINE_NETWORK", ""), \
                patch.object(engine, "DIRECT_NETWORK", ""), \
                patch.object(engine, "_instance_paths", return_value=(temp, temp)), \
                patch.object(engine, "_clear_runtime_state"), \
                patch.object(engine, "_engine_network_subnet", return_value=None), \
                patch.object(engine.media, "engine_attachment", return_value=None), \
                patch.object(engine.egress, "ensure_line", return_value={}), \
                patch.object(engine.cfg, "write_instance_json"):
            for settings, legacy, expected in (({}, "", "1280"), ({}, "1400", "1400"),
                                               ({"vowifi_mtu": 1300}, "1400", "1300")):
                with self.subTest(settings=settings, legacy=legacy), \
                        patch.dict(os.environ, {"SWU_TUN_MTU": legacy}):
                    engine.start({"id": "sim1"}, settings)
                    passed = client.containers.run.call_args.kwargs["environment"]
                    self.assertEqual(passed["SWU_TUN_MTU"], expected)


class StandaloneMtuTests(unittest.TestCase):
    def test_standalone_engine_uses_the_same_safe_default_and_honours_overrides(self):
        # Execute only the real environment parser, without importing SIM bindings or
        # opening sockets. Control normally supplies this value when creating the container.
        source = (Path(__file__).resolve().parents[1] / "engine" / "swu_ike.py").read_text()
        block = source[source.index("DEFAULT_TUN_MTU ="):source.index("SWU_OUTER_MTU_ENV =")]
        for value, expected in ((None, 1280), ("", 1280), ("0", 1280), ("bad", 1280),
                                ("1279", 1280), ("1501", 1280), ("1400", 1400), ("1500", 1500)):
            with self.subTest(value=value):
                namespace = {"os": SimpleNamespace(environ={} if value is None else {"SWU_TUN_MTU": value})}
                exec(compile(block, "engine/swu_ike.py", "exec"), namespace)
                self.assertEqual(namespace["SWU_TUN_MTU_ENV"], expected)


if __name__ == "__main__":
    unittest.main()
