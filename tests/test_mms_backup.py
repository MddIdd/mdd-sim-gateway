"""MMS attachments are part of every history backup, and a restored backup reads them back."""
from __future__ import annotations

import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import config, mms_staging, operations, store


class _Store:
    def __init__(self, root: Path):
        self.root = root
        self.db = root / "mdd-sim-gateway.sqlite"
        self.patch = patch.multiple(store, DATA_DIR=str(root), DB_PATH=str(self.db),
                                    PREVIOUS_DB_PATH=str(root / "vowifi.sqlite"))

    def __enter__(self):
        self.patch.start()
        store.init()
        return self

    def __exit__(self, *exc):
        self.patch.stop()


def add_mms(image: bytes = b"\xff\xd8\xff-picture") -> int:
    rec = store.create_outgoing_mms("1", "+447700900123", to_addrs=["+447700900123"],
                                    subject="", body="hi", transaction_id=f"T{len(image)}")
    store.save_mms_content(rec["id"], [
        {"content_type": "text/plain", "data": b"hi", "charset": "utf-8", "text": "hi"},
        {"content_type": "image/jpeg", "data": image, "name": "photo.jpg"}])
    return rec["id"]


def read_back(root: Path, message_id: int) -> list[bytes]:
    with _Store(root):
        return [p["data"] for p in store.mms_parts_with_data(message_id)]


class MigrationBackupTests(unittest.TestCase):
    def test_the_pre_migration_backup_restores_whole_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            live = Path(temp, "live")
            with _Store(live) as ctx:
                mid = add_mms()
                with sqlite3.connect(ctx.db) as db:     # pretend one schema step is pending
                    db.execute(f"PRAGMA user_version={len(store._MIGRATIONS) - 1}")
                store.init()
                backups = sorted(Path(store.backup_dir()).glob("*.sqlite"))
                self.assertEqual(len(backups), 1)
                attachments = backups[0].with_suffix(".mms")
                self.assertTrue(attachments.is_dir())
                self.assertFalse(list(Path(store.backup_dir()).glob("*.partial")))

            restored = Path(temp, "restored")
            restored.mkdir()
            shutil.copy2(backups[0], restored / "mdd-sim-gateway.sqlite")
            shutil.copytree(attachments, restored / "mms")
            self.assertEqual(read_back(restored, mid), [b"hi", b"\xff\xd8\xff-picture"])

    def test_a_part_already_missing_does_not_block_the_upgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            with _Store(Path(temp)) as ctx:
                mid = add_mms()
                for path in (Path(store.mms_dir()) / str(mid)).iterdir():
                    path.unlink()
                with sqlite3.connect(ctx.db) as db:
                    db.execute(f"PRAGMA user_version={len(store._MIGRATIONS) - 1}")
                store.init()
                self.assertEqual(store.schema_version(), len(store._MIGRATIONS))


class LocalBackupTests(unittest.TestCase):
    def test_a_full_backup_holds_a_consistent_history_and_its_attachments(self):
        with tempfile.TemporaryDirectory() as temp:
            live = Path(temp, "live")
            with _Store(live), patch.object(config, "DATA_DIR", str(live)):
                Path(live, "config.yaml").write_text("settings: {}\ninstances: {}\n")
                mid = add_mms()
                stray = Path(store.mms_dir()) / str(mid) / "unreferenced.jpg"
                stray.write_bytes(b"x")
                draft = mms_staging.stage("1", "draft.jpg", "image/jpeg", b"draft")
                result = operations.create_local_backup("Test Gateway")
                archive_path = Path(store.backup_dir()) / result["name"]
                self.assertFalse(list(Path(store.backup_dir()).glob(".staging-*")))
            with tarfile.open(archive_path) as archive:
                names = archive.getnames()
                restored = Path(temp, "restored")
                archive.extractall(restored, filter="data")
            self.assertIn("config.yaml", names)
            self.assertNotIn(f"mms/{mid}/unreferenced.jpg", names)
            self.assertFalse([n for n in names if draft["id"] in n], "uploads being composed")
            self.assertEqual(read_back(restored, mid), [b"hi", b"\xff\xd8\xff-picture"])

    def test_a_live_attachment_omitted_from_the_snapshot_makes_the_backup_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            live = Path(temp)
            with _Store(live), patch.object(config, "DATA_DIR", str(live)):
                add_mms()
                snapshot_history = store.snapshot_history

                def omit_copied_part(database_target, mms_target, **kwargs):
                    result = snapshot_history(database_target, mms_target, **kwargs)
                    next(path for path in Path(mms_target).rglob("*") if path.is_file()).unlink()
                    return result

                with patch.object(store, "snapshot_history", side_effect=omit_copied_part):
                    with self.assertRaisesRegex(RuntimeError, "missing 1 MMS attachment"):
                        operations.create_local_backup("Test Gateway")
                self.assertEqual(operations.list_local_backups(), [])

    def test_an_attachment_already_missing_is_reported_without_blocking_the_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            live = Path(temp)
            with _Store(live), patch.object(config, "DATA_DIR", str(live)):
                mid = add_mms()
                next((Path(store.mms_dir()) / str(mid)).iterdir()).unlink()
                result = operations.create_local_backup("Test Gateway")
                self.assertEqual(result["missing_attachments"], 1)
                self.assertTrue((Path(store.backup_dir()) / result["name"]).is_file())


if __name__ == "__main__":
    unittest.main()
