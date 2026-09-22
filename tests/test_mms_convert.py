"""Attachments are converted and shrunk on the gateway to fit a line's MMS limit."""
from __future__ import annotations

import asyncio
import io
import os
import random
import struct
import tempfile
import time
import unittest
import warnings
import zlib
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from control.app import main, mms, mms_convert, mms_pdu, mms_staging, store

TO = ["+447700900123"]


def photo(width=3000, height=2000, fmt="JPEG", mode="RGB", **options) -> bytes:
    """A noisy picture, which compresses about as badly as a real photo."""
    rng = random.Random(width * height)
    image = Image.frombytes(mode, (width // 8, height // 8),
                            bytes(rng.randrange(256) for _ in range(
                                (width // 8) * (height // 8) * len(mode))))
    image = image.resize((width, height), Image.BILINEAR)
    out = io.BytesIO()
    image.save(out, fmt, **options)
    return out.getvalue()


def png_header(width: int, height: int) -> bytes:
    """A PNG that says how big it is and carries almost nothing: what a decoder allocates for
    is the header's claim, not the number of bytes that arrived."""
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload)))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00"))
            + chunk(b"IEND", b""))


def fit(attachments, limit, text="hi"):
    return mms.fit_attachments(attachments, text, "", TO, {"max_size": limit})


class ImageFitTests(unittest.TestCase):
    def test_a_camera_photo_is_shrunk_into_the_limit(self):
        original = photo()
        self.assertGreater(len(original), 300 * 1024)
        fitted, problem, summary = fit([{"name": "IMG_1.jpg", "content_type": "image/jpeg",
                                         "data": original}], 100 * 1024)
        self.assertIsNone(problem)
        self.assertTrue(summary["fits"])
        self.assertLessEqual(summary["size"], 100 * 1024)
        item = summary["attachments"][0]
        self.assertTrue(item["converted"])
        self.assertEqual((item["original_size"], item["content_type"]),
                         (len(original), "image/jpeg"))
        self.assertLessEqual(max(item["width"], item["height"]), mms_convert.IMAGE_EDGES[0])
        picture = Image.open(io.BytesIO(fitted[0]["data"]))
        self.assertEqual(picture.format, "JPEG")
        self.assertFalse(picture.info.get("progressive"), "baseline JPEG for older phones")

    def test_a_small_picture_that_fits_is_sent_untouched(self):
        original = photo(400, 300, "PNG")
        fitted, problem, summary = fit([{"name": "a.png", "content_type": "image/png",
                                         "data": original}], 600 * 1024)
        self.assertIsNone(problem)
        self.assertEqual(fitted[0]["data"], original)
        self.assertFalse(summary["attachments"][0]["converted"])

    def test_a_picture_sent_as_it_is_loses_its_location_but_no_pixel(self):
        image = Image.open(io.BytesIO(photo(400, 300)))
        exif = Image.Exif()
        exif[0x010F] = "PhoneMaker"                               # Make
        exif.get_ifd(0x8825)[2] = (51.0, 30.0, 0.0)               # GPSLatitude
        out = io.BytesIO()
        image.save(out, "JPEG", exif=exif, quality=85)
        original = out.getvalue()
        self.assertIn(b"PhoneMaker", original)
        fitted, problem, summary = fit([{"name": "a.jpg", "content_type": "image/jpeg",
                                         "data": original}], 600 * 1024)
        self.assertIsNone(problem)
        sent = fitted[0]["data"]
        self.assertNotIn(b"Exif", sent)
        self.assertNotIn(b"PhoneMaker", sent)
        self.assertFalse(summary["attachments"][0]["converted"], "not re-encoded")
        self.assertEqual(Image.open(io.BytesIO(sent)).tobytes(),
                         Image.open(io.BytesIO(original)).tobytes(), "the same pixels")

        png = io.BytesIO()
        info = __import__("PIL.PngImagePlugin", fromlist=["PngInfo"]).PngInfo()
        info.add_text("Location", "somewhere")
        Image.new("RGB", (40, 40), "red").save(png, "PNG", pnginfo=info)
        fitted, _problem, _summary = fit([{"name": "a.png", "content_type": "image/png",
                                           "data": png.getvalue()}], 600 * 1024)
        self.assertNotIn(b"somewhere", fitted[0]["data"])
        self.assertEqual(Image.open(io.BytesIO(fitted[0]["data"])).getpixel((5, 5)),
                         (255, 0, 0))

    def test_a_rotated_photo_is_turned_upright_rather_than_losing_its_rotation(self):
        image = Image.open(io.BytesIO(photo(400, 300)))
        exif = Image.Exif()
        exif[0x0112] = 6                                          # rotate 90 degrees
        out = io.BytesIO()
        image.save(out, "JPEG", exif=exif)
        fitted, _problem, summary = fit([{"name": "r.jpg", "content_type": "image/jpeg",
                                          "data": out.getvalue()}], 600 * 1024)
        sent = Image.open(io.BytesIO(fitted[0]["data"]))
        self.assertEqual(sent.size, (300, 400))
        self.assertNotIn(0x0112, sent.getexif())
        self.assertTrue(summary["attachments"][0]["converted"])

    def test_formats_phones_do_not_show_become_jpeg_even_when_small(self):
        for fmt, content_type, name in (("WEBP", "image/webp", "a.webp"),
                                        ("BMP", "image/bmp", "a.bmp"),
                                        ("HEIF", "image/heic", "IMG_2.HEIC"),
                                        ("AVIF", "image/avif", "a.avif")):
            with self.subTest(fmt):
                fitted, problem, summary = fit([{"name": name, "content_type": content_type,
                                                 "data": photo(320, 240, fmt)}], 600 * 1024)
                self.assertIsNone(problem)
                self.assertEqual(fitted[0]["content_type"], "image/jpeg")
                self.assertTrue(fitted[0]["name"].endswith(".jpg"))
                self.assertEqual(summary["attachments"][0]["original_type"], content_type)

    def test_transparency_becomes_white(self):
        image = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        out = io.BytesIO()
        image.save(out, "WEBP", lossless=True)
        fitted, _problem, _summary = fit([{"name": "t.webp", "content_type": "image/webp",
                                          "data": out.getvalue()}], 600 * 1024)
        pixel = Image.open(io.BytesIO(fitted[0]["data"])).getpixel((20, 20))
        self.assertTrue(all(channel > 240 for channel in pixel))

    def test_pictures_share_the_room_and_a_small_one_keeps_its_size(self):
        small = photo(300, 200)
        big = [photo(2400 + i * 8, 1800) for i in range(2)]
        attachments = [{"name": "s.jpg", "content_type": "image/jpeg", "data": small}] + \
            [{"name": "photo.jpg", "content_type": "image/jpeg", "data": b} for b in big]
        fitted, problem, summary = fit(attachments, 200 * 1024)
        self.assertIsNone(problem)
        self.assertLessEqual(summary["size"], 200 * 1024)
        self.assertEqual(fitted[0]["data"], small)
        sizes = [a["size"] for a in summary["attachments"][1:]]
        self.assertLess(abs(sizes[0] - sizes[1]), 0.3 * max(sizes), "an even share each")

    def test_what_cannot_shrink_is_counted_as_it_is(self):
        amr = b"#!AMR\n" + (bytes([7 << 3 | 0x04]) + b"\x00" * 31) * 2000   # 40 s, 64 KB
        _fitted, problem, summary = fit([{"name": "m.amr", "content_type": "audio/amr",
                                          "data": amr},
                                         {"name": "p.jpg", "content_type": "image/jpeg",
                                          "data": photo()}], 100 * 1024)
        self.assertIsNone(problem)
        self.assertFalse(summary["attachments"][0]["adjustable"])
        self.assertEqual(summary["attachments"][0]["size"], len(amr))
        _fitted, problem, _summary = fit([{"name": "m.amr", "content_type": "audio/amr",
                                           "data": amr}], 32 * 1024)
        self.assertIn("once packaged", problem)

    def test_an_animated_gif_is_never_re_encoded(self):
        frames = [Image.new("L", (400, 400), i * 12) for i in range(20)]
        out = io.BytesIO()
        frames[0].save(out, "GIF", save_all=True, append_images=frames[1:])
        gif = {"name": "a.gif", "content_type": "image/gif", "data": out.getvalue()}
        fitted, problem, summary = fit([gif], 600 * 1024)
        self.assertEqual((problem, fitted[0]["data"]), (None, gif["data"]))
        self.assertFalse(summary["attachments"][0]["adjustable"])
        _fitted, problem, _summary = fit([gif], len(gif["data"]) // 2)
        self.assertIn("once packaged", problem)

    def test_an_unreadable_picture_is_refused(self):
        _fitted, problem, _summary = fit([{"name": "bad.jpg", "content_type": "image/jpeg",
                                           "data": b"\xff\xd8\xff\xe0" + b"x" * 64}], 600 * 1024)
        self.assertIn("bad.jpg", problem)
        self.assertIn("could not be read", problem)

    def test_a_picture_larger_than_the_pixel_limit_is_refused_before_it_is_decoded(self):
        # Between MAX_PIXELS and twice it, Pillow's own guard does no more than warn, so a
        # file claiming this many pixels used to be decoded in full.
        edge = int((mms_convert.MAX_PIXELS * 1.5) ** 0.5)
        with warnings.catch_warnings():     # Pillow's warning is the point being made here
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            _fitted, problem, _summary = fit([{"name": "huge.png", "content_type": "image/png",
                                               "data": png_header(edge, edge)}], 600 * 1024)
        self.assertIn("huge.png", problem)
        self.assertIn("megapixels", problem)

    def test_a_limit_too_small_for_any_picture_says_so(self):
        _fitted, problem, _summary = fit([{"name": "p.jpg", "content_type": "image/jpeg",
                                           "data": photo()}], 2 * 1024)
        self.assertIn("p.jpg", problem)

    def test_the_fitted_message_packages_with_a_valid_smil(self):
        fitted, _problem, _summary = fit([{"name": "p.heic", "content_type": "image/heic",
                                           "data": photo(800, 600, "HEIF")}], 300 * 1024)
        request = mms.build_request("0" * 20, TO, "", mms._compose_parts("hi", fitted))
        pdu = mms_pdu.decode_pdu(request)
        mms_pdu.check_smil(pdu.parts[0], pdu.parts[1:])
        self.assertIn(b'src="p.jpg"', pdu.parts[0].data)

    def test_the_table_offers_what_the_gateway_can_convert(self):
        formats = {f["content_type"]: f for f in mms.attachment_formats()}
        self.assertTrue(formats["image/heic"]["attachable"])
        self.assertTrue(formats["image/jpeg"]["attachable"])
        self.assertFalse(formats["video/quicktime"]["attachable"])
        with patch.dict(mms_convert.CONVERTERS, clear=True):
            self.assertFalse({f["content_type"]: f for f in mms.attachment_formats()}
                             ["image/heic"]["attachable"])
            _fitted, problem, _summary = fit([{"name": "a.webp", "content_type": "image/webp",
                                               "data": photo(64, 64, "WEBP")}], 600 * 1024)
            self.assertIn("cannot convert", problem)


class SplitTests(unittest.TestCase):
    def attachments(self, count=3):
        return [{"name": f"p{i}.jpg", "content_type": "image/jpeg",
                 "data": photo(3000 + 8 * i, 2000)} for i in range(count)]

    def test_one_message_shares_the_limit_and_split_gives_each_the_whole_limit(self):
        limit = 200 * 1024
        together, problem, shared = mms.plan_messages(self.attachments(), "hello", "Hi", TO,
                                                      {"max_size": limit})
        self.assertIsNone(problem)
        self.assertEqual((len(together), shared["split"]), (1, False))
        self.assertLessEqual(shared["size"], limit)
        apart, problem, own = mms.plan_messages(self.attachments(), "hello", "Hi", TO,
                                                {"max_size": limit}, split=True)
        self.assertIsNone(problem)
        self.assertEqual((len(apart), own["split"], len(own["messages"])), (3, True, 3))
        self.assertTrue(all(m["size"] <= limit for m in own["messages"]))
        self.assertEqual([m["text"] for m in apart], ["hello", "", ""])
        self.assertEqual([m["subject"] for m in apart], ["Hi", "", ""])
        for alone, sharing in zip(own["attachments"], shared["attachments"]):
            self.assertGreater(alone["size"], sharing["size"] * 2,
                               "on its own a picture keeps far more of its quality")
        self.assertEqual(own["size"], sum(m["size"] for m in own["messages"]))

    def test_switching_modes_always_starts_from_the_originals(self):
        items = self.attachments(2)
        first, _p, _s = mms.plan_messages(items, "", "", TO, {"max_size": 200 * 1024})
        mms.plan_messages(items, "", "", TO, {"max_size": 200 * 1024}, split=True)
        again, _p, _s = mms.plan_messages(items, "", "", TO, {"max_size": 200 * 1024})
        self.assertEqual([a["data"] for a in first[0]["attachments"]],
                         [a["data"] for a in again[0]["attachments"]])

    def test_a_problem_in_one_split_message_names_its_attachment(self):
        amr = b"#!AMR\n" + (bytes([7 << 3 | 0x04]) + b"\x00" * 31) * 2000
        _messages, problem, summary = mms.plan_messages(
            [{"name": "p.jpg", "content_type": "image/jpeg", "data": photo()},
             {"name": "memo.amr", "content_type": "audio/amr", "data": amr}],
            "", "", TO, {"max_size": 32 * 1024}, split=True)
        self.assertTrue(problem.startswith("memo.amr"))
        self.assertEqual([m["fits"] for m in summary["messages"]], [True, False])

    def test_a_single_attachment_is_one_message_either_way(self):
        messages, _problem, summary = mms.plan_messages(self.attachments(1), "x", "", TO,
                                                        {"max_size": 300 * 1024}, split=True)
        self.assertEqual((len(messages), summary["split"]), (1, False))


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(store, "DATA_DIR", self.temp.name)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_an_upload_is_kept_until_removed(self):
        meta = mms_staging.stage("1", "../IMG 1.heic", "image/heic", b"original")
        self.assertEqual(meta["name"], "IMG 1.heic")
        self.assertEqual(mms_staging.load("1", [meta["id"]])[0]["data"], b"original")
        with self.assertRaises(KeyError):
            mms_staging.load("2", [meta["id"]])
        with self.assertRaises(KeyError):
            mms_staging.load("1", ["../../etc"])
        self.assertEqual(mms_staging.preview_file("1", meta["id"])[1], "image/heic")
        shared = mms_staging.save_fitted("1", meta["id"], "image/jpeg", b"small")
        alone = mms_staging.save_fitted("1", meta["id"], "image/jpeg", b"larger")
        path, content_type = mms_staging.preview_file("1", meta["id"])
        self.assertEqual((Path(path).read_bytes(), content_type), (b"larger", "image/jpeg"))
        path, _type = mms_staging.preview_file("1", meta["id"], shared)
        self.assertEqual(Path(path).read_bytes(), b"small", "each version stays addressable")
        self.assertEqual(Path(mms_staging.preview_file("1", meta["id"], alone)[0]).read_bytes(),
                         b"larger")
        self.assertIsNone(mms_staging.preview_file("1", meta["id"], "0" * 16))
        for i in range(mms_staging.FITTED_VERSIONS):
            mms_staging.save_fitted("1", meta["id"], "image/jpeg", b"v%d" % i)
        self.assertIsNone(mms_staging.preview_file("1", meta["id"], shared), "old ones go")
        directory = Path(self.temp.name, "mms-staging", "1", meta["id"])
        self.assertEqual(len(list(directory.glob("fitted-*"))), mms_staging.FITTED_VERSIONS)
        mms_staging.remove("1", [meta["id"]])
        with self.assertRaises(KeyError):
            mms_staging.load("1", [meta["id"]])

    def test_a_line_holds_a_bounded_number_and_old_uploads_are_swept(self):
        ids = [mms_staging.stage("1", f"{i}.jpg", "image/jpeg", b"x")["id"]
               for i in range(mms_staging.MAX_PER_LINE)]
        with self.assertRaises(OverflowError):
            mms_staging.stage("1", "one-more.jpg", "image/jpeg", b"x")
        self.assertEqual(mms_staging.sweep(), 0)
        self.assertEqual(mms_staging.sweep(now=time.time() + mms_staging.TTL_SECONDS + 1),
                         len(ids))
        self.assertEqual(mms_staging.list_ids("1"), [])


class FitEndpointTests(StagingTests):
    def test_the_composer_learns_each_size_and_the_total(self):
        original = photo()
        meta = mms_staging.stage("1", "IMG_1.jpg", "image/jpeg", original)
        settings = {"enabled": True, "configured": True, "max_size": 150 * 1024}
        with patch.object(main.cfg, "get_instance", return_value={"id": "1"}), \
                patch.object(main.mms_transport, "resolve_settings", return_value=settings):
            result = asyncio.run(main.api_mms_attachments_fit(
                "1", {"ids": [meta["id"]], "text": "hello", "to": "+447700900123"}))
        self.assertTrue(result["ok"])
        self.assertLessEqual(result["size"], result["limit"])
        entry = result["attachments"][0]
        self.assertEqual((entry["id"], entry["original_size"]), (meta["id"], len(original)))
        path, content_type = mms_staging.preview_file("1", meta["id"], entry["preview"])
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(os.path.getsize(path), entry["size"])

    def test_split_sends_are_submitted_in_order(self):
        order = []

        async def fake_send(iid, mid):
            await asyncio.sleep(0.01 * (3 - mid))
            order.append(mid)

        with patch.object(main, "_send_mms_task", side_effect=fake_send):
            asyncio.run(main._send_mms_sequence("1", [1, 2, 3]))
        self.assertEqual(order, [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
