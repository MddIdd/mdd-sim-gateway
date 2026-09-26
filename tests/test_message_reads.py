"""How far a reader has got through a conversation, and why that is not on the message.

"Read" is something a reader did, so the marker belongs to an owner rather than to the message
-- and it is a position rather than a flag: a conversation is read up to a point, and anything
that arrives after that point is new again.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import store

READER, OTHER_READER = 2, 3
LINE, OTHER_LINE = "1", "2"
PEER = "+44 7700 900123"


class ReadStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patch = patch.multiple(store, DATA_DIR=str(root),
                                    DB_PATH=str(root / "mdd-sim-gateway.sqlite"),
                                    PREVIOUS_DB_PATH=str(root / "vowifi.sqlite"))
        self.patch.start()
        store.init()
        self.clock = 1_700_000_000

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def arrive(self, body="hello", peer=PEER, instance=LINE, direction="in", ts=None):
        """One message on the wire, a minute after the last unless the test says otherwise."""
        self.clock += 60
        record = store.add_message(instance, direction, peer, body, ts=ts or self.clock)
        return int(record["id"])

    def test_an_upgrade_does_not_present_the_history_as_unread(self):
        self.arrive()
        self.arrive("on the other line", instance=OTHER_LINE)
        # A gateway from before read state: the schema step has not run, and nothing is marked.
        with store._conn() as c:
            c.execute("DELETE FROM message_reads")
            c.execute(f"PRAGMA user_version={len(store._MIGRATIONS) - 1}")
        store.init()
        for line in (LINE, OTHER_LINE):
            self.assertEqual(store.unread_counts(store.ADMIN_OWNER, line), {}, line)
        # What arrives afterwards is new.
        self.arrive("after the upgrade")
        self.assertEqual(store.unread_counts(store.ADMIN_OWNER, LINE), {PEER: 1})

    def test_a_conversation_starts_unread_and_stops_being_so_when_read(self):
        self.arrive()
        self.arrive("and another")
        self.assertEqual(store.unread_counts(READER, LINE), {PEER: 2})
        store.mark_thread_read(READER, LINE, PEER)
        self.assertEqual(store.unread_counts(READER, LINE), {})

    def test_a_message_that_arrives_after_reading_is_unread_again(self):
        self.arrive()
        store.mark_thread_read(READER, LINE, PEER)
        self.arrive("later")
        self.assertEqual(store.unread_counts(READER, LINE), {PEER: 1})

    def test_reading_never_moves_backwards(self):
        first = self.arrive()
        self.arrive("newer")
        store.mark_thread_read(READER, LINE, PEER)
        # Opening an older message again must not resurrect what was already read.
        store.mark_thread_read(READER, LINE, PEER, message_id=first)
        self.assertEqual(store.unread_counts(READER, LINE), {})

    def test_a_marker_cannot_run_ahead_of_what_has_arrived(self):
        # A client that names an id not yet used must not file the next arrival as read.
        newest = self.arrive()
        self.assertEqual(store.mark_thread_read(READER, LINE, PEER, message_id=newest + 1000),
                         newest)
        self.arrive("next")
        self.assertEqual(store.unread_counts(READER, LINE), {PEER: 1})

    def test_a_message_id_that_is_not_one_is_refused_with_a_400(self):
        from control.app import main
        request = main.Request({"type": "http", "headers": [],
                                "state": {"principal": main.gate.Principal("admin")}})
        for body in ({"peer": PEER, "message_id": "latest"}, {"all": True, "message_id": [1]}):
            with self.subTest(body=body), self.assertRaises(main.HTTPException) as refused:
                asyncio.run(main.api_messages_read(LINE, body, request))
            self.assertEqual(refused.exception.status_code, 400)

    def test_a_delayed_message_is_unread_even_though_it_is_older(self):
        """An inbound SMS carries the network's timestamp, which can predate one already read."""
        self.arrive("newest")
        store.mark_thread_read(READER, LINE, PEER)
        self.arrive("delayed in the network", ts=1_600_000_000)
        self.assertEqual(store.unread_counts(READER, LINE), {PEER: 1})

    def test_what_you_sent_is_never_unread(self):
        self.arrive(direction="out", body="mine")
        self.assertEqual(store.unread_counts(READER, LINE), {})

    def test_one_reader_s_marker_never_moves_another_s(self):
        self.arrive()
        store.mark_thread_read(READER, LINE, PEER)
        self.assertEqual(store.unread_counts(READER, LINE), {})
        self.assertEqual(store.unread_counts(OTHER_READER, LINE), {PEER: 1})

    def test_marking_the_line_read_covers_every_conversation_including_later_ones(self):
        self.arrive(peer=PEER)
        self.arrive(peer="+15550100")
        store.mark_line_read(READER, LINE)
        self.assertEqual(store.unread_counts(READER, LINE), {})
        # This is what a client's first run does, so an upgrade does not present history as new.
        self.arrive(peer="+15550101")
        self.assertEqual(store.unread_counts(READER, LINE), {"+15550101": 1})

    def test_a_line_baseline_does_not_leak_into_another_line(self):
        self.arrive(instance=LINE)
        self.arrive(instance=OTHER_LINE)
        store.mark_line_read(READER, LINE)
        self.assertEqual(store.unread_counts(READER, LINE), {})
        self.assertEqual(store.unread_counts(READER, OTHER_LINE), {PEER: 1})

    def test_the_badge_counts_every_line_it_is_given(self):
        self.arrive(instance=LINE)
        self.arrive(instance=LINE, body="two")
        self.arrive(instance=OTHER_LINE)
        self.assertEqual(store.unread_total(READER, [LINE, OTHER_LINE]), 3)
        self.assertEqual(store.unread_total(READER, [OTHER_LINE]), 1)
        store.mark_thread_read(READER, OTHER_LINE, PEER)
        self.assertEqual(store.unread_total(READER, [LINE, OTHER_LINE]), 2)


if __name__ == "__main__":
    unittest.main()
