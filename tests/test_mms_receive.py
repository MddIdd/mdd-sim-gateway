import asyncio
import base64
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from control.app import main, mms, mms_pdu as m, store

LOCATION = "http://mmsc.example.test:8002/?id=abc"


def notification_push(location=LOCATION, *, tid="T1", sender="447700900123/TYPE=PLMN",
                      size=24_000) -> bytes:
    sender_value = bytes([0x80]) + m.write_encoded_string_value(sender)
    expiry = bytes([0x81]) + m.write_long_integer(172_800)
    body = (bytes([0x8C, 0x82]) + b"\x98" + m.write_text_string(tid) + b"\x8D\x92"
            + b"\x89" + m.write_value_length(len(sender_value)) + sender_value
            + b"\x8A\x80" + b"\x88" + m.write_value_length(len(expiry)) + expiry
            + b"\x8E" + m.write_long_integer(size) + b"\x83" + m.write_text_string(location))
    return bytes([0x01, 0x06, 0x01, 0xBE]) + body


def delivery_push(message_id: str, status: int = m.STATUS_RETRIEVED) -> bytes:
    body = (bytes([0x8C, 0x86]) + b"\x8D\x92" + b"\x8B" + m.write_text_string(message_id)
            + b"\x97" + m.write_text_string("+447700900123/TYPE=PLMN")
            + b"\x85" + m.write_long_integer(1_800_000_000) + bytes([0x95, status]))
    return bytes([0x02, 0x06, 0x01, 0xBE]) + body


class TempStore(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
        self.patch = patch.multiple(store, DATA_DIR=str(root),
                                    DB_PATH=str(root / "mdd-sim-gateway.sqlite"),
                                    PREVIOUS_DB_PATH=str(root / "vowifi.sqlite"))
        self.patch.start()
        store.init()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()


class HandleWapPushTests(TempStore):
    def test_notification_becomes_one_pending_mms_in_the_existing_conversation(self):
        store.add_message("1", "in", "+447700900123", "earlier text")
        result = mms.handle_wap_push("1", "99", notification_push(), transport="cellular",
                                     sent_ts=1_000)
        rec = result["message"]
        self.assertTrue(result["handled"])
        self.assertEqual(rec["kind"], "mms")
        self.assertEqual(rec["peer"], "+447700900123")
        self.assertEqual(rec["mms"]["state"], "notified")
        self.assertEqual(rec["mms"]["size"], 24_000)
        self.assertNotIn("content_location", rec["mms"], "the MMSC URL stays server-side")
        self.assertEqual(store.mms_for_download(rec["id"])["content_location"], LOCATION)
        self.assertEqual(len(store.due_mms_downloads()), 1)

        again = mms.handle_wap_push("1", "99", notification_push(), transport="vowifi",
                                    sent_ts=9_000)
        self.assertTrue(again["handled"])
        self.assertIsNone(again["message"], "the same MMS over the other transport")

    def test_non_mms_push_and_garbage_are_not_handled(self):
        other = bytes([0x01, 0x06, 0x01, 0xAE]) + b"\x00\x01"   # application/vnd.wap.sic
        self.assertFalse(mms.handle_wap_push("1", "99", other, transport="vowifi")["handled"])
        self.assertFalse(mms.handle_wap_push("1", "99", b"\xff", transport="vowifi")["handled"])

    def test_delivery_report_marks_outgoing_mms_delivered(self):
        rec = store.create_outgoing_mms("1", "+447700900123", to_addrs=["+447700900123"],
                                        subject="", body="hi", transaction_id="TX")
        store.set_mms_state(rec["id"], "sent", message_ref="MSG-1", message_status="sent")
        result = mms.handle_wap_push("1", "99", delivery_push("MSG-1"), transport="vowifi")
        self.assertEqual(result["delivery"]["status"], "delivered")
        self.assertEqual(result["delivery"]["mms"]["state"], "delivered")
        self.assertIn("+447700900123", result["delivery"]["mms"]["delivery"])

    def outgoing(self, recipients):
        rec = store.create_outgoing_mms("1", ", ".join(recipients), to_addrs=recipients,
                                        subject="", body="hi", transaction_id="TX")
        store.set_mms_state(rec["id"], "sent", message_ref="MSG-9", message_status="sent")
        return rec

    @staticmethod
    def report(recipient, status):
        body = (bytes([0x8C, 0x86]) + b"\x8D\x92" + b"\x8B" + m.write_text_string("MSG-9")
                + (b"\x97" + m.write_text_string(f"{recipient}/TYPE=PLMN") if recipient else b"")
                + b"\x85" + m.write_long_integer(1_800_000_000) + bytes([0x95, status]))
        return bytes([0x03, 0x06, 0x01, 0xBE]) + body

    def deliver(self, recipient, status):
        return mms.handle_wap_push("1", "99", self.report(recipient, status),
                                   transport="vowifi")["delivery"]

    def test_group_mms_is_delivered_only_when_every_recipient_retrieved_it(self):
        self.outgoing(["+447700900123", "+447700900124"])
        first = self.deliver("447700900123", m.STATUS_RETRIEVED)
        self.assertEqual((first["status"], first["mms"]["state"]), ("sent", "sent"))
        both = self.deliver("+447700900124", m.STATUS_RETRIEVED)
        self.assertEqual((both["status"], both["mms"]["state"]), ("delivered", "delivered"))
        self.assertEqual(sorted(both["mms"]["delivery"]), ["+447700900123", "+447700900124"],
                         "one entry per recipient, whatever spelling the report used")

    def test_a_rejection_after_a_retrieval_is_not_hidden(self):
        self.outgoing(["+447700900123", "+447700900124"])
        self.deliver("+447700900123", m.STATUS_RETRIEVED)
        rec = self.deliver("+447700900124", m.STATUS_REJECTED)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("Delivered to 1 of 2", rec["error"])
        self.assertIn("+447700900124: rejected", rec["error"])

    def test_retrieval_after_a_rejection_does_not_mark_the_group_delivered(self):
        self.outgoing(["+447700900123", "+447700900124"])
        self.deliver("+447700900124", m.STATUS_REJECTED)
        rec = self.deliver("+447700900123", m.STATUS_RETRIEVED)
        self.assertEqual(rec["status"], "failed")

    def test_non_final_report_keeps_the_message_sent(self):
        self.outgoing(["+447700900123"])
        rec = self.deliver("+447700900123", m.STATUS_DEFERRED)
        self.assertEqual((rec["status"], rec["error"]), ("sent", None))
        rec = self.deliver("", m.STATUS_RETRIEVED)
        self.assertEqual(rec["status"], "delivered", "a report without To is the sole recipient's")

    def test_wap_push_udh_detection(self):
        self.assertTrue(mms.is_wap_push_udh("05040b8423f0"))
        self.assertFalse(mms.is_wap_push_udh("0003a70201"))
        self.assertFalse(mms.is_wap_push_udh("zz"))


class PartStorageTests(TempStore):
    def test_parts_are_files_and_deleting_the_message_removes_them(self):
        rec = mms.handle_wap_push("1", "99", notification_push(), transport="vowifi")["message"]
        store.save_mms_content(rec["id"], [
            {"content_type": "text/plain", "data": "你好".encode(), "name": "t.txt",
             "charset": "utf-8", "text": "你好"},
            {"content_type": "image/jpeg", "data": b"\xff\xd8\xff", "name": "../../evil.jpg"},
        ], body="你好", subject="")
        stored = store.get_message(rec["id"])
        self.assertEqual(stored["body"], "你好")
        image = stored["mms"]["parts"][1]
        found = store.mms_part_file("1", rec["id"], image["id"])
        self.assertTrue(found["file"].startswith(os.path.realpath(self.root / "mms")))
        self.assertEqual(Path(found["file"]).read_bytes(), b"\xff\xd8\xff")
        self.assertIsNone(store.mms_part_file("2", rec["id"], image["id"]), "other line")
        store.delete_messages("1", [rec["id"]])
        self.assertFalse(os.path.exists(os.path.dirname(found["file"])))
        self.assertIsNone(store.mms_for_download(rec["id"]))


class PartReplacementTests(TempStore):
    def setUp(self):
        super().setUp()
        self.rec = mms.handle_wap_push("1", "99", notification_push(),
                                       transport="vowifi")["message"]
        self.directory = self.root / "mms" / str(self.rec["id"])

    def parts(self, tag=b"1", name="photo.jpg"):
        return [{"content_type": "image/jpeg", "data": b"\xff\xd8\xff" + tag, "name": name},
                {"content_type": "text/plain", "data": b"hi" + tag, "charset": "utf-8"}]

    def stored(self):
        return {p["seq"]: p for p in store.mms_parts_with_data(self.rec["id"])}

    def test_files_get_internal_names_and_the_original_name_is_metadata(self):
        long_name = "照片" * 60 + ".jpeg"
        store.save_mms_content(self.rec["id"], self.parts(name=long_name))
        first, text = self.stored()[0], self.stored()[1]
        self.assertRegex(first["path"], r"^00-[0-9a-f]{16}\.jpg$")
        self.assertRegex(text["path"], r"^01-[0-9a-f]{16}\.txt$")
        self.assertTrue(first["name"].endswith(".jpeg"))
        self.assertLessEqual(len(first["name"].encode()), 120)
        self.assertEqual(text["name"], "")

    def test_a_save_that_fails_leaves_the_previous_content_intact(self):
        store.save_mms_content(self.rec["id"], self.parts(b"1"))
        before = sorted(os.listdir(self.directory))
        real_replace, calls = os.replace, []

        def fail_second(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError(28, "No space left on device")
            return real_replace(src, dst)

        with patch.object(os, "replace", side_effect=fail_second):
            with self.assertRaises(OSError):
                store.save_mms_content(self.rec["id"], self.parts(b"2"))
        self.assertEqual(sorted(os.listdir(self.directory)), before)
        self.assertEqual(self.stored()[0]["data"], b"\xff\xd8\xff1")

        with patch.object(store, "_conn", side_effect=RuntimeError("database is locked")):
            with self.assertRaises(RuntimeError):
                store.save_mms_content(self.rec["id"], self.parts(b"3"))
        self.assertEqual(sorted(os.listdir(self.directory)), before)

    def test_a_successful_save_replaces_the_old_files(self):
        store.save_mms_content(self.rec["id"], self.parts(b"1"))
        old = set(os.listdir(self.directory))
        store.save_mms_content(self.rec["id"], self.parts(b"2"))
        new = set(os.listdir(self.directory))
        self.assertFalse(old & new)
        self.assertEqual(self.stored()[0]["data"], b"\xff\xd8\xff2")

    def test_a_save_for_a_deleted_message_keeps_nothing(self):
        store.delete_messages("1", [self.rec["id"]])
        with self.assertRaises(LookupError):
            store.save_mms_content(self.rec["id"], self.parts())
        self.assertFalse(self.directory.exists())

    def test_the_sweep_removes_only_old_unreferenced_files(self):
        store.save_mms_content(self.rec["id"], self.parts())
        (self.directory / "stray.jpg").write_bytes(b"x")
        (self.directory / ".00-abc.jpg.tmp").write_bytes(b"x")
        orphan = self.root / "mms" / "999"
        orphan.mkdir()
        (orphan / "00-x.jpg").write_bytes(b"x")
        self.assertEqual(store.sweep_mms_orphans(), 0, "recent files are left alone")
        later = time.time() + store.MMS_ORPHAN_GRACE_SECONDS + 1
        self.assertEqual(store.sweep_mms_orphans(now=later), 3)
        self.assertFalse(orphan.exists())
        self.assertEqual(sorted(os.listdir(self.directory)),
                         sorted(p["path"] for p in self.stored().values()))


class InboundEventTests(unittest.IsolatedAsyncioTestCase, TempStore):
    def setUp(self):
        TempStore.setUp(self)
        self.broadcast = AsyncMock()
        self.push = Mock()
        for target, attr, new in ((main.hub, "broadcast", self.broadcast),
                                  (main, "_dispatch_push", self.push)):
            p = patch.object(target, attr, new)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        TempStore.tearDown(self)

    @staticmethod
    def event(payload: bytes, triplet=("", "", ""), udh="05040b8423f0"):
        widened = payload.decode("latin-1")
        return {"instance": "1", "event": "sms_in", "args": [
            "99", base64.b64encode(widened.encode()).decode(), *map(str, triplet),
            "0", "4", udh, ""]}

    async def test_vowifi_wap_push_is_stored_as_mms_not_filed(self):
        result = await main.api_engine_event(self.event(notification_push()))
        self.assertEqual(result.get("stored"), "mms")
        self.assertEqual(store.list_binary_sms("1"), [])
        threads = store.list_threads("1")
        self.assertEqual(threads[0]["last_kind"], "mms")
        self.assertEqual(self.push.call_count, 0, "pushed once the worker knows the content")
        self.assertTrue(main.hub.mms_wakeup.is_set())

    async def test_payload_is_taken_from_the_tpdu_not_the_nul_truncated_body(self):
        payload = notification_push()
        udh = bytes.fromhex("0605040b8423f0")
        user_data = udh + payload
        tpdu = (bytes([0x44, 0x02, 0x81, 0x99, 0x00, 0x04]) + bytes.fromhex("62907080000000")
                + bytes([len(user_data)]) + user_data).hex()
        truncated = payload[:payload.index(b"\x00")]
        event = self.event(truncated)
        event["args"][-1] = tpdu
        result = await main.api_engine_event(event)
        self.assertEqual(result.get("stored"), "mms")
        self.assertEqual(len(store.due_mms_downloads()), 1)

    async def test_long_wap_push_is_reassembled_from_its_parts(self):
        payload = notification_push()
        first, second = payload[:20], payload[20:]
        udh = "0003070201" + "05040b8423f0"
        r1 = await main.api_engine_event(self.event(first, (7, 2, 1), udh))
        self.assertIn("buffered", r1)
        await main.api_engine_event(self.event(second, (7, 2, 2), udh))
        self.assertEqual(len(store.due_mms_downloads()), 1)

    async def test_other_binary_payload_is_still_filed(self):
        result = await main.api_engine_event(self.event(b"\x00\x01\x02\x7f", udh=""))
        self.assertEqual(result.get("stored"), "binary")
        self.assertEqual(self.push.call_count, 0)


def filed_push_tpdu(payload: bytes, scts: str = "62304151906280") -> str:
    """An SMS-DELIVER from short code 99 carrying `payload` to the WAP Push port."""
    user_data = bytes.fromhex("0605040b8423f0") + payload
    return (bytes([0x44, 0x02, 0x81, 0x99, 0x00, 0x04]) + bytes.fromhex(scts)
            + bytes([len(user_data)]) + user_data).hex()


class FiledPushConversionTests(TempStore):
    def file_row(self, payload=None, *, udh="05040b8423f0", concat=None, tpdu=None):
        payload = notification_push() if payload is None else payload
        return store.add_binary_sms(
            "1", "99", ts=1_000, transport="vowifi", tp_pid=0, tp_dcs=4, concat=concat,
            udh_hex=udh, tpdu_hex=filed_push_tpdu(payload) if tpdu is None else tpdu,
            # What 1.9.x stored: the body cut at the first 0x00.
            body_hex=payload[:payload.index(b"\x00")].hex() if b"\x00" in payload else payload.hex())

    def restart_at_version(self, version):
        import sqlite3
        with sqlite3.connect(store.DB_PATH) as db:
            db.execute(f"PRAGMA user_version={version}")
        store.init()

    def test_upgrade_turns_filed_notifications_into_mms_and_keeps_other_payloads(self):
        self.file_row()
        other = self.file_row(b"\x00\x01\x02\x7f", udh="")
        part = self.file_row(concat=(7, 2, 1))
        self.restart_at_version(4)
        remaining = [row["id"] for row in store.list_binary_sms("1")]
        self.assertEqual(sorted(remaining), sorted([other["id"], part["id"]]))
        threads = store.list_threads("1")
        self.assertEqual([t["last_kind"] for t in threads], ["mms"])
        mms_row = store.due_mms_downloads(now=10**10)[0]
        self.assertEqual(mms_row["content_location"], LOCATION)
        self.assertEqual(mms_row["transport"], "vowifi")
        self.assertEqual(store.get_message(mms_row["message_id"])["ts"], 1773493766,
                         "dated by the notification's own SCTS")

    def test_notification_already_held_is_not_duplicated(self):
        mms.handle_wap_push("1", "99", notification_push(), transport="cellular", sent_ts=500)
        self.file_row()
        self.restart_at_version(4)
        self.assertEqual(store.list_binary_sms("1"), [])
        self.assertEqual(len(store.list_threads("1")), 1)
        self.assertEqual(store.list_threads("1")[0]["n"], 1)

    def test_rows_filed_again_by_an_older_version_are_converted_on_the_next_start(self):
        store.init()
        self.file_row(notification_push("http://mmsc.example.test/?id=later"))
        store.init()                                  # already current: the reconciliation does it
        self.assertEqual(store.list_binary_sms("1"), [])
        self.assertEqual(len(store.due_mms_downloads(now=10**10)), 1)

    def test_row_without_a_complete_tpdu_stays_filed(self):
        self.file_row(tpdu="")
        self.restart_at_version(4)
        self.assertEqual(len(store.list_binary_sms("1")), 1)


if __name__ == "__main__":
    unittest.main()
