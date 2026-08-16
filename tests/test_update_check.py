import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from control.app import config, update_check


class _Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class UpdateCheckTests(unittest.TestCase):
    def setUp(self):
        update_check._cache = None
        update_check._stars_cache = None
        direct = patch.object(update_check, "_network_selection", return_value={
            "proxy_mode": "direct", "proxy_profile_id": ""})
        direct.start()
        self.addCleanup(direct.stop)

    def test_newer_release_is_reported_without_applying_it(self):
        newer = list(update_check._version_tuple(update_check.VERSION))
        newer[-1] += 1
        payload = {"tag_name": "v" + ".".join(map(str, newer)),
                   "html_url": "https://example.invalid/release",
                   "published_at": "2026-08-01T00:00:00Z", "body": "notes"}
        with patch("control.app.update_check.requests.Session.get",
                   return_value=_Response(payload)):
            result = update_check.check(True)
        self.assertTrue(result["update_available"])
        self.assertEqual(result["current"], update_check.VERSION)
        self.assertNotIn("apply", result)

    def test_semantic_comparison(self):
        self.assertGreater(update_check._version_tuple("v1.10.0"), update_check._version_tuple("1.9.9"))

    def test_update_network_defaults_to_auto_and_requires_a_library_entry(self):
        self.assertEqual(update_check.validate_network_settings(None)["proxy_mode"], "auto")
        with self.assertRaises(update_check.UpdateNetworkError):
            update_check.validate_network_settings({"proxy_mode": "library",
                                                     "proxy_profile_id": ""})
        self.assertEqual(update_check.validate_network_settings({
            "proxy_mode": "country", "proxy_country": "us"})["proxy_mode"], "auto")

    def test_repository_can_be_overridden_without_changing_the_ui(self):
        self.assertEqual(update_check.repository(), "MddIdd/mdd-sim-gateway")
        with patch.dict("os.environ", {"MDD_UPDATE_REPOSITORY": "example/private"}):
            self.assertEqual(update_check.repository(), "example/private")

    def test_release_request_never_sends_a_github_token(self):
        payload = {"tag_name": "v1.0.0"}
        captured = {}

        def get(url, headers, timeout):
            captured["authorization"] = headers.get("Authorization")
            return _Response(payload)

        with patch.dict("os.environ", {"MDD_GITHUB_TOKEN": "must-not-be-used"}), patch(
                "control.app.update_check.requests.Session.get", side_effect=get):
            update_check.check(True)
        self.assertIsNone(captured["authorization"])

    def test_private_repository_does_not_prompt_for_authentication(self):
        with patch("control.app.update_check.requests.Session.get",
                   return_value=_Response({}, 401)):
            result = update_check.check(True)
        self.assertEqual(result["error_code"], "update.error.no_release")
        self.assertNotIn("auth", result["error"].lower())

    def test_library_entry_is_used_as_socks_proxy(self):
        session = MagicMock()
        session.proxies = {}
        session.get.return_value = _Response({"tag_name": "v1.0.0"})
        with patch.object(update_check, "_network_selection", return_value={
                "proxy_mode": "library", "proxy_profile_id": "primary"}), \
                patch.object(update_check, "_proxy_url",
                             return_value="socks5h://172.17.0.1:22538"), \
                patch("control.app.update_check.requests.Session", return_value=session):
            update_check.check(True)
        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies["https"], "socks5h://172.17.0.1:22538")

    def test_socks5_library_credentials_are_url_encoded(self):
        self.assertEqual(update_check._socks5_profile_url({
            "server": "proxy.example", "port": 1080,
            "username": "a@b", "password": "p:/w",
        }), "socks5h://a%40b:p%3A%2Fw@proxy.example:1080")

    def test_auto_falls_back_to_library_and_records_the_working_route(self):
        direct = MagicMock()
        direct.get.side_effect = requests.ConnectionError("blocked")
        proxied = MagicMock()
        proxied.get.return_value = _Response({"tag_name": "v9.9.9"})
        candidates = [
            {"proxy_mode": "direct", "proxy_profile_id": ""},
            {"proxy_mode": "library", "proxy_profile_id": "primary"},
        ]
        with patch.object(update_check, "_network_candidates", return_value=candidates), \
                patch.object(update_check, "_session", side_effect=[direct, proxied]):
            result = update_check.check(True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["network"], candidates[1])


class UpdateProxyMigrationTests(unittest.TestCase):
    def _load(self, settings):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "CONFIG_PATH", str(Path(temp, "config.yaml"))):
            Path(temp, "config.yaml").write_text(
                "settings:\n" + "\n".join(f"  {line}" for line in settings.splitlines())
                + "\ninstances: {}\n", encoding="utf-8")
            return config.load()["settings"]

    def test_old_country_selection_migrates_to_auto_and_keeps_library_profile(self):
        settings = self._load("""proxy:
  profiles:
    primary: {name: Primary, type: node, value: 'vless://example'}
  exits:
    us: {enabled: true, profile_id: primary}
updates: {proxy_mode: country, proxy_country: us}""")
        self.assertEqual(settings["updates"], {
            "proxy_mode": "auto", "proxy_profile_id": ""})
        self.assertIn("primary", settings["proxy"]["profiles"])

    def test_old_socks_update_proxy_moves_into_the_library(self):
        settings = self._load("""proxy: {}
updates:
  proxy_mode: manual
  proxy_url: 'socks5h://alice:secret@proxy.example:1081'""")
        self.assertEqual(settings["updates"], {
            "proxy_mode": "auto", "proxy_profile_id": ""})
        profile = settings["proxy"]["profiles"]["legacy-update-proxy"]
        self.assertEqual((profile["server"], profile["port"], profile["username"]),
                         ("proxy.example", 1081, "alice"))

if __name__ == "__main__":
    unittest.main()
