"""Engine -> Control event delivery.

A Pi migrated from a native install to the container stack kept
settings.manager_url = https://host.docker.internal:8443. The explicit setting won over the
container's MDD_MANAGER_URL, the Engine (on an internal network) could not reach the host,
and notify.py swallowed every timeout: inbound SMS reached the Engine but never the web UI.
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

from control.app import config

ROOT = Path(__file__).resolve().parents[1]

LEGACY = "https://host.docker.internal:8443"
CONTAINER = "https://mdd-control:8443"


class ManagerUrlTests(unittest.TestCase):
    def url(self, settings, env):
        with patch.dict(os.environ, env, clear=False):
            for key in ("MDD_CONTAINER_STACK", "MDD_MANAGER_URL"):
                if key not in env:
                    os.environ.pop(key, None)
            return config._engine_manager_url(settings)

    def test_container_stack_ignores_a_saved_host_url(self):
        self.assertEqual(
            self.url({"manager_url": LEGACY},
                     {"MDD_CONTAINER_STACK": "1", "MDD_MANAGER_URL": CONTAINER}),
            CONTAINER)

    def test_native_install_keeps_the_explicit_setting(self):
        self.assertEqual(
            self.url({"manager_url": "https://10.0.0.5:9443"},
                     {"MDD_MANAGER_URL": LEGACY}),
            "https://10.0.0.5:9443")

    def test_native_install_falls_back_to_env_then_default(self):
        self.assertEqual(self.url({}, {"MDD_MANAGER_URL": LEGACY}), LEGACY)
        self.assertEqual(self.url({"http_port": 9443}, {}), "https://host.docker.internal:9443")

    def test_container_stack_without_env_keeps_the_old_fallbacks(self):
        self.assertEqual(self.url({"manager_url": LEGACY}, {"MDD_CONTAINER_STACK": "1"}), LEGACY)


def _load_notify():
    spec = importlib.util.spec_from_file_location("engine_notify_under_test",
                                                  ROOT / "engine" / "notify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NotifyDeliveryTests(unittest.TestCase):
    def run_notify(self, post):
        notify = _load_notify()
        requests = types.ModuleType("requests")
        requests.post = post
        urllib3 = types.ModuleType("urllib3")
        urllib3.disable_warnings = lambda *a, **k: None
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(sys.modules, {"requests": requests, "urllib3": urllib3}), \
                patch.dict(os.environ, {"MANAGER_URL": LEGACY, "MDD_ID": "5",
                                        "MDD_ENV": str(Path(temp) / "none")}), \
                patch.object(notify.os, "makedirs"), \
                patch("builtins.open", side_effect=OSError), \
                patch.object(sys, "argv", ["notify.py", "sms_in", "+44700", "aGk="]), \
                redirect_stderr(err):
            notify.main()
        return err.getvalue()

    def test_an_unreachable_manager_is_reported_not_swallowed(self):
        def post(*a, **k):
            raise TimeoutError("timed out")
        out = self.run_notify(post)
        self.assertIn("event not delivered", out)
        self.assertIn("sms_in", out)
        self.assertIn(LEGACY, out)

    def test_a_rejected_event_is_reported(self):
        out = self.run_notify(MagicMock(return_value=MagicMock(status_code=401)))
        self.assertIn("HTTP 401", out)

    def test_a_delivered_event_is_silent(self):
        post = MagicMock(return_value=MagicMock(status_code=200))
        self.assertEqual(self.run_notify(post), "")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(json.loads(json.dumps(payload))["event"], "sms_in")


if __name__ == "__main__":
    unittest.main()
