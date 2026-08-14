"""One-click self-update: control-plane request publishing + host updater file handling."""
import importlib.util
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import time
import unittest
from types import SimpleNamespace
import requests
from pathlib import Path
from unittest.mock import patch

from control.app import config, update_check
from host import mdd_orchestrator

_SPEC = importlib.util.spec_from_file_location(
    "mdd_update", Path(__file__).resolve().parent.parent / "host" / "mdd_update.py")
mdd_update = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mdd_update)

_AVAILABLE = {"ok": True, "update_available": True, "latest": "9.9.9",
              "release_url": "https://example.invalid/release"}


class RequestApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(config, "DATA_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.request_path = os.path.join(self.tmp.name, "orchestrator", "update-request.json")
        self.status_path = os.path.join(self.tmp.name, "orchestrator", "update-status.json")

    def test_request_is_published_with_version_and_repository(self):
        with patch.object(update_check, "check", return_value=dict(_AVAILABLE)), \
                patch.object(update_check, "_network_selection", return_value={
                    "proxy_mode": "country", "proxy_url": "", "proxy_country": "us"}):
            result = update_check.request_apply()
        self.assertTrue(result["ok"])
        with open(self.request_path, encoding="utf-8") as handle:
            request = json.load(handle)
        self.assertEqual(request["version"], "9.9.9")
        self.assertEqual(request["repository"], update_check.repository())
        self.assertEqual(request["network"]["proxy_country"], "us")
        with open(self.status_path, encoding="utf-8") as handle:
            status = json.load(handle)
        self.assertEqual(status["state"], "running")
        self.assertEqual(status["phase"], "requested")

    def test_no_available_update_publishes_nothing(self):
        with patch.object(update_check, "check",
                          return_value={"ok": True, "update_available": False}):
            result = update_check.request_apply()
        self.assertFalse(result["ok"])
        self.assertFalse(os.path.exists(self.request_path))

    def test_running_update_is_not_requested_twice(self):
        os.makedirs(os.path.dirname(self.status_path))
        with open(self.status_path, "w", encoding="utf-8") as handle:
            json.dump({"state": "running", "phase": "reloading",
                       "updated_at": int(time.time())}, handle)
        with patch.object(update_check, "check", return_value=dict(_AVAILABLE)):
            result = update_check.request_apply()
        self.assertEqual(result["error_code"], "update.error.in_progress")
        self.assertFalse(os.path.exists(self.request_path))

    def test_unconsumed_request_is_reported_as_stalled(self):
        os.makedirs(os.path.dirname(self.request_path))
        with open(self.request_path, "w", encoding="utf-8") as handle:
            json.dump({"version": "9.9.9", "requested_at": int(time.time()) - 300}, handle)
        status = update_check.apply_status()
        self.assertTrue(status["requested"])
        self.assertEqual(status["state"], "stalled")


class UpdaterTests(unittest.TestCase):
    def test_release_archive_checksum_is_required_and_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp, "mdd-sim-gateway-v9.9.9.tar.gz")
            archive.write_bytes(b"release")
            digest = hashlib.sha256(b"release").hexdigest()
            sums = Path(tmp, "SHA256SUMS")
            sums.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
            mdd_update.verify_release_archive(archive, sums)
            archive.write_bytes(b"changed")
            with self.assertRaises(mdd_update.UpdateError):
                mdd_update.verify_release_archive(archive, sums)

    def test_proxy_environment_is_explicit_and_not_added_to_curl_arguments(self):
        proxy = "socks5h://user:secret@127.0.0.1:1080"
        env = mdd_update.network_environment(proxy)
        self.assertEqual(env["ALL_PROXY"], proxy)
        completed = type("Completed", (), {"returncode": 0, "stderr": ""})()
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                mdd_update.subprocess, "run", return_value=completed) as run:
            mdd_update.download("https://example.invalid/release.tar.gz",
                                Path(tmp, "release.tar.gz"), env, proxy)
        args = run.call_args.args[0]
        self.assertNotIn(proxy, args)
        self.assertEqual(run.call_args.kwargs["env"]["HTTPS_PROXY"], proxy)

    def test_apply_tree_preserves_installation_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, source = Path(tmp, "repo"), Path(tmp, "source")
            for relative in ["data/auth.json", ".git/config", "control/.venv/bin/python",
                            "control/app/stale.py", "webui/node_modules/pkg/index.js",
                            "webui/dist/index.html", "README.md"]:
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("old", encoding="utf-8")
            (repo / ".env").write_text("MDD_PORT=9999", encoding="utf-8")
            for relative in ["control/app/new.py", "webui/src/App.jsx", "README.md", "VERSION"]:
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("new", encoding="utf-8")

            mdd_update.apply_tree(source, repo)

            self.assertEqual((repo / "data/auth.json").read_text(encoding="utf-8"), "old")
            self.assertEqual((repo / ".env").read_text(encoding="utf-8"), "MDD_PORT=9999")
            self.assertEqual((repo / ".git/config").read_text(encoding="utf-8"), "old")
            self.assertEqual((repo / "control/.venv/bin/python").read_text(encoding="utf-8"), "old")
            self.assertTrue((repo / "webui/node_modules/pkg/index.js").exists())
            self.assertEqual((repo / "webui/dist/index.html").read_text(encoding="utf-8"), "old",
                             "the served dist must survive so a failed rebuild keeps the UI up")
            self.assertEqual((repo / "README.md").read_text(encoding="utf-8"), "new")
            self.assertEqual((repo / "control/app/new.py").read_text(encoding="utf-8"), "new")
            self.assertFalse((repo / "control/app/stale.py").exists(),
                             "files removed upstream must not linger in managed directories")

    def test_apply_tree_replaces_only_marked_release_dist(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, source = Path(tmp, "repo"), Path(tmp, "source")
            (repo / "webui/dist").mkdir(parents=True)
            (repo / "webui/dist/index.html").write_text("old", encoding="utf-8")
            (source / "webui/dist").mkdir(parents=True)
            (source / "webui/dist/index.html").write_text("new", encoding="utf-8")
            (source / "webui/dist/.mdd-release-version").write_text(
                "9.9.9\n", encoding="utf-8")
            mdd_update.apply_tree(source, repo)
            self.assertEqual((repo / "webui/dist/index.html").read_text(), "new")

    def test_perform_accepts_release_without_distribution_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo, data, payload = base / "repo", base / "data", base / "payload"
            source = payload / "mdd-sim-gateway-v9.9.9"
            (source / "webui/dist").mkdir(parents=True)
            (source / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (source / "VERSION").write_text("9.9.9\n", encoding="utf-8")
            (source / "webui/dist/index.html").write_text("new", encoding="utf-8")
            (source / "webui/dist/.mdd-release-version").write_text(
                "9.9.9\n", encoding="utf-8")
            archive = base / "release.tar.gz"
            with tarfile.open(archive, "w:gz") as handle:
                handle.add(source, arcname=source.name)
            sums = base / "SHA256SUMS"
            sums.write_text(
                f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  "
                "mdd-sim-gateway-v9.9.9.tar.gz\n", encoding="utf-8")
            repo.mkdir()
            data.mkdir()
            (repo / "VERSION").write_text("1.3.4\n", encoding="utf-8")
            status = mdd_update.Status(data / "orchestrator/status.json", "9.9.9")

            def fake_download(_url, destination, _env, _proxy=""):
                shutil.copy2(sums if destination.name == "SHA256SUMS" else archive,
                             destination)

            completed = type("Completed", (), {"returncode": 0})()
            with patch.object(mdd_update, "download", side_effect=fake_download), \
                    patch.object(mdd_update.subprocess, "run", return_value=completed):
                mdd_update.perform(repo, data, "9.9.9", "MddIdd/mdd-sim-gateway", status)

            self.assertEqual((repo / "VERSION").read_text().strip(), "9.9.9")
            self.assertFalse((repo / "EDITION").exists())

    def test_perform_rejects_malformed_version_and_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = mdd_update.Status(Path(tmp, "status.json"), "x")
            with self.assertRaises(mdd_update.UpdateError):
                mdd_update.perform(Path(tmp), Path(tmp), "../evil", "MddIdd/mdd-sim-gateway", status)
            with self.assertRaises(mdd_update.UpdateError):
                mdd_update.perform(Path(tmp), Path(tmp), "1.0.2", "MddIdd/x/../y", status)


class OrchestratorUpdateTests(unittest.TestCase):
    def test_country_proxy_is_resolved_into_private_file_not_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            root = data / "orchestrator"
            root.mkdir()
            (root / "update-request.json").write_text(json.dumps({
                "version": "9.9.9", "repository": "MddIdd/mdd-sim-gateway",
                "network": {"proxy_mode": "country", "proxy_country": "us"},
            }), encoding="utf-8")
            (root / "proxy-status.json").write_text(json.dumps({"exits": {"us": {
                "ready": True, "proxy_host": mdd_orchestrator.COUNTRY_PROXY_LISTEN,
                "proxy_port": 22538,
            }}}), encoding="utf-8")
            app = mdd_orchestrator.Orchestrator(
                data, Path(__file__).resolve().parent.parent)
            completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            with patch.object(app, "service_active", return_value=False), \
                    patch.object(mdd_orchestrator, "run", return_value=completed) as run:
                app.process_update_request()
            network_path = data / "update" / "network.json"
            self.assertEqual(json.loads(network_path.read_text())["proxy_url"],
                             f"socks5h://{mdd_orchestrator.COUNTRY_PROXY_LISTEN}:22538")
            command = run.call_args_list[-1].args[0]
            self.assertNotIn("socks5h://", " ".join(command))
            self.assertEqual(network_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()


class StarCountTests(unittest.TestCase):
    """The count decorates a link. It must never turn a working update check into an error,
    and an unreadable count must stay absent rather than being reported as zero."""

    def test_a_failed_star_lookup_leaves_the_release_check_intact(self):
        session = SimpleNamespace(get=lambda *a, **k: (_ for _ in ()).throw(
            requests.RequestException("offline")))
        self.assertIsNone(update_check._stargazers(session, {}, "owner/repo"))

    def test_a_star_count_is_read_from_the_repository_endpoint(self):
        calls = []

        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"stargazers_count": 13300}

        def get(url, **kwargs):
            calls.append(url)
            return Response()

        value = update_check._stargazers(SimpleNamespace(get=get), {}, "owner/repo")
        self.assertEqual(value, 13300)
        self.assertEqual(calls, ["https://api.github.com/repos/owner/repo"])

    def test_a_malformed_star_count_is_absent_rather_than_zero(self):
        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"stargazers_count": None}

        self.assertIsNone(update_check._stargazers(
            SimpleNamespace(get=lambda *a, **k: Response()), {}, "owner/repo"))
