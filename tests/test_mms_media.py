"""Tests for control/app/mms_media.py: the MMS attachment capability table and content checks."""
from __future__ import annotations

import struct
import unittest

from control.app import mms, mms_media as media

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def box(kind: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def full_box(kind: bytes, payload: bytes) -> bytes:
    return box(kind, b"\x00\x00\x00\x00" + payload)


def track(handler: bytes, codec: bytes) -> bytes:
    hdlr = full_box(b"hdlr", b"\x00" * 4 + handler + b"\x00" * 12 + b"\x00")
    stsd = full_box(b"stsd", struct.pack(">I", 1) + box(codec, b"\x00" * 16))
    stbl = box(b"stbl", stsd)
    return box(b"trak", box(b"mdia", hdlr + box(b"minf", stbl)))


def movie(brand: bytes = b"isom", tracks=((b"vide", b"avc1"), (b"soun", b"mp4a")),
          seconds: float = 12.5) -> bytes:
    mvhd = full_box(b"mvhd", b"\x00" * 8 + struct.pack(">II", 1000, int(seconds * 1000))
                    + b"\x00" * 80)
    return box(b"ftyp", brand + b"\x00\x00\x00\x00" + brand) + \
        box(b"moov", mvhd + b"".join(track(h, c) for h, c in tracks)) + box(b"mdat", b"\x00" * 64)


def amr(frames: int) -> bytes:
    # Frame type 7 (12.2 kbit/s): one header byte and 31 bytes of speech per 20 ms.
    return b"#!AMR\n" + (bytes([7 << 3 | 0x04]) + b"\x00" * 31) * frames


class CheckAttachmentTests(unittest.TestCase):
    def check(self, name, content_type, data):
        return media.check_attachment(name, content_type, data)

    def test_supported_pictures_pass_and_the_content_decides_the_type(self):
        self.assertEqual(self.check("a.jpg", "image/jpeg", JPEG).content_type, "image/jpeg")
        result = self.check("a.jpg", "image/jpeg", PNG)
        self.assertEqual((result.error, result.content_type), (None, "image/png"))
        self.assertEqual(self.check("a.png", "", PNG).content_type, "image/png")

    def test_a_different_kind_of_media_than_declared_is_refused(self):
        self.assertIn("labelled image/jpeg", self.check("a.jpg", "image/jpeg", movie()).error)
        self.assertIn("does not contain image/png", self.check("a.png", "image/png",
                                                               b"hello").error)
        self.assertIn("labelled text/plain", self.check("a.txt", "text/plain", JPEG).error)

    def test_pictures_to_convert_are_refused_until_converted(self):
        webp = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16
        self.assertIn("convert it to JPEG", self.check("a.webp", "image/webp", webp).error)
        heic = box(b"ftyp", b"heic\x00\x00\x00\x00mif1heic")
        self.assertIn("image/heic", self.check("a.heic", "image/heic", heic).error)

    def test_video_codecs_and_duration(self):
        result = self.check("clip.mp4", "video/mp4", movie())
        self.assertEqual((result.error, result.content_type, result.duration_ms),
                         (None, "video/mp4", 12_500))
        self.assertEqual(self.check("c.3gp", "video/3gpp", movie(b"3gp5", (
            (b"vide", b"s263"), (b"soun", b"samr")))).content_type, "video/3gpp")
        hevc = self.check("IMG_0001.mp4", "video/mp4", movie(tracks=((b"vide", b"hvc1"),)))
        self.assertIn("HEVC", hevc.error)
        self.assertIn("cannot be sent", self.check("a.mov", "video/quicktime",
                                                   movie(b"qt  ")).error)
        self.assertIn("incomplete", self.check("a.mp4", "video/mp4",
                                               box(b"ftyp", b"isom" * 3)).error)

    def test_audio_only_mp4_is_audio_whatever_it_was_called(self):
        result = self.check("memo.m4a", "video/mp4", movie(tracks=((b"soun", b"mp4a"),)))
        self.assertEqual(result.content_type, "audio/mp4")

    def test_amr_duration_is_counted_in_frames(self):
        result = self.check("memo.amr", "audio/amr", amr(250))
        self.assertEqual((result.error, result.duration_ms), (None, 5_000))
        result = self.check("memo.amr", "application/octet-stream", amr(3))
        self.assertEqual(result.content_type, "audio/amr")

    def test_cards(self):
        v3 = b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Ann\r\nEND:VCARD\r\n"
        self.assertEqual(self.check("ann.vcf", "text/vcard", v3).content_type, "text/x-vCard")
        v4 = v3.replace(b"3.0", b"4.0")
        self.assertIn("vCard 4.0", self.check("ann.vcf", "text/vcard", v4).error)
        self.assertIn("not a vCard", self.check("ann.vcf", "text/vcard", b"hello").error)
        ics = b"BEGIN:VCALENDAR\nVERSION:2.0\nEND:VCALENDAR\n"
        self.assertEqual(self.check("a.ics", "", ics).content_type, "text/calendar")
        vcs = ics.replace(b"2.0", b"1.0")
        self.assertEqual(self.check("a.ics", "text/calendar", vcs).content_type,
                         "text/x-vCalendar")
        self.assertIn("not UTF-8", self.check("a.txt", "text/plain", b"\xc3\x28").error)

    def test_unknown_and_empty_files(self):
        self.assertIn("cannot be sent", self.check("a.pdf", "application/pdf",
                                                   b"%PDF-1.7").error)
        self.assertIn("is empty", self.check("a.jpg", "image/jpeg", b"").error)

    def test_a_video_that_stops_short_is_refused_rather_than_read_past_its_end(self):
        # An upload cut off part way through still parses as boxes; the movie header is then
        # there but empty. Reading it as if it were whole used to raise IndexError, which
        # reaches the client as a 500 instead of "this file cannot be sent".
        head = box(b"ftyp", b"isom" + b"\x00" * 8)
        for tail in (box(b"mvhd"), box(b"mvhd", b"\x00" * 3), box(b"mvhd", b"\x01" + b"\x00" * 8)):
            result = self.check("clip.mp4", "video/mp4", head + box(b"moov", tail))
            self.assertIn("no audio or video track", result.error)
        self.assertIn("no movie header", self.check("clip.mp4", "video/mp4", head).error)


class CapabilityTableTests(unittest.TestCase):
    def test_every_format_has_one_policy_and_a_unique_type(self):
        rows = media.capabilities()
        types = [r["content_type"].lower() for r in rows]
        self.assertEqual(len(types), len(set(types)))
        self.assertTrue(all(r["policy"] in (media.SEND, media.CONVERT, media.RECEIVE)
                            for r in rows))

    def test_lookup_follows_aliases_and_parameters(self):
        self.assertEqual(media.lookup("text/vcard; charset=utf-8").content_type, "text/x-vCard")
        self.assertEqual(media.lookup("IMAGE/JPG").content_type, "image/jpeg")
        self.assertIsNone(media.lookup("application/pdf"))
        self.assertTrue(media.previewable("image/png"))
        self.assertFalse(media.previewable("audio/amr"))


class NameTests(unittest.TestCase):
    def test_long_names_keep_their_extension_within_the_byte_limit(self):
        name = media.display_name("照片" * 100 + ".jpeg")
        self.assertTrue(name.endswith(".jpeg"))
        self.assertLessEqual(len(name.encode("utf-8")), media.MAX_NAME_BYTES)
        emoji = media.display_name("\U0001F600" * 80 + ".png")
        self.assertLessEqual(len(emoji.encode("utf-8")), media.MAX_NAME_BYTES)
        self.assertTrue(emoji.endswith(".png"))

    def test_paths_and_control_characters_are_removed(self):
        self.assertEqual(media.display_name("../../etc/pa\x00ss\u202e.txt"), "pass.txt")
        self.assertEqual(media.display_name("C:\\Users\\a\\b.jpg"), "b.jpg")
        self.assertEqual(media.display_name('a"b<c>.jpg'), "a_b_c_.jpg")
        self.assertEqual(media.display_name("", "image/png"), "attachment.png")
        self.assertEqual(media.display_name("...", "text/x-vCard"), "attachment.vcf")

    def test_storage_names_are_fresh_and_typed_by_content(self):
        a, b = media.storage_name(1, "image/jpeg"), media.storage_name(1, "image/jpeg")
        self.assertNotEqual(a, b)
        self.assertRegex(a, r"^01-[0-9a-f]{16}\.jpg$")
        self.assertTrue(media.storage_name(2, "application/x-evil").endswith(".bin"))


class ComposeTests(unittest.TestCase):
    def test_validate_outgoing_reports_the_content_problem(self):
        settings = {"max_size": 300 * 1024}
        problem = mms.validate_outgoing(["+447700900123"], "", [
            {"name": "IMG_1.mov", "content_type": "video/mp4",
             "data": movie(tracks=((b"vide", b"hvc1"),))}], settings)
        self.assertIn("HEVC", problem)
        checked, problem = mms.check_attachments([
            {"name": "", "content_type": "application/octet-stream", "data": amr(10)}])
        self.assertIsNone(problem)
        self.assertEqual((checked[0]["name"], checked[0]["content_type"],
                          checked[0]["duration_ms"]), ("attachment1.amr", "audio/amr", 200))

    def test_the_smil_times_audio_by_its_length(self):
        parts = mms._compose_parts("hi", mms.check_attachments(
            [{"name": "memo.amr", "content_type": "audio/amr", "data": amr(300)}])[0])
        request = mms.build_request("0" * 20, ["+447700900123"], "", parts)
        self.assertIn(b'dur="6000ms"', request)
        parts[1].pop("duration_ms")      # as read back from storage: measured again
        self.assertIn(b'dur="6000ms"', mms.build_request("0" * 20, ["+447700900123"], "", parts))


if __name__ == "__main__":
    unittest.main()
