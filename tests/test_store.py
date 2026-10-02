import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import store


class StoreMigrationTests(unittest.TestCase):
    def test_previous_database_is_copied_once_and_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            previous = root / "vowifi.sqlite"
            current = root / "mdd-sim-gateway.sqlite"
            with sqlite3.connect(previous) as connection:
                connection.execute("CREATE TABLE marker (value TEXT)")
                connection.execute("INSERT INTO marker VALUES ('kept')")
            with patch.multiple(store, DATA_DIR=str(root), DB_PATH=str(current),
                                PREVIOUS_DB_PATH=str(previous)):
                store.init()
            self.assertTrue(previous.exists())
            with sqlite3.connect(current) as connection:
                self.assertEqual(connection.execute("SELECT value FROM marker").fetchone()[0],
                                 "kept")

    def test_existing_database_merges_named_legacy_history_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = root / "mdd-sim-gateway.sqlite"
            previous = root / "vowifi.sqlite"
            with sqlite3.connect(previous) as db:
                db.executescript("""
                    CREATE TABLE calls (id INTEGER PRIMARY KEY, instance TEXT, direction TEXT,
                        peer TEXT, status TEXT, start_ts INTEGER, end_ts INTEGER);
                    CREATE TABLE messages (id INTEGER PRIMARY KEY, instance TEXT, direction TEXT,
                        peer TEXT, body TEXT, status TEXT, ts INTEGER);
                    INSERT INTO calls VALUES(7,'giff','out','service','ended',100,101);
                    INSERT INTO messages VALUES(9,'giff','in','service','hello','ok',102);
                """)
            # Make the current DB exist first: this is the upgrade case the old copy-only
            # migration skipped.
            current.touch()
            with patch.multiple(store, DATA_DIR=str(root), DB_PATH=str(current),
                                PREVIOUS_DB_PATH=str(previous)):
                store.init()
                first = store.migrate_legacy_history({"giff": "3"})
                second = store.migrate_legacy_history({"giff": "3"})

                self.assertEqual(first, {"calls": 1, "messages": 1})
                self.assertEqual(second, {"calls": 0, "messages": 0})
                self.assertEqual(len(store.list_calls("3")), 1)
                self.assertEqual(store.list_threads("3")[0]["n"], 1)


if __name__ == "__main__":
    unittest.main()
