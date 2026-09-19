"""Tests for control/app/mms_pdu.py.

Test 1 is an m-notification-ind laid out the way carriers send it over a single WAP-push
SMS (field codes, lengths, the 0xBE well-known content-type short form), with fictional
values throughout. Test 5 is an MMSC error response in the same form, also fictional.
"""
from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from control.app import mms_pdu as m


class TestLowLevelPrimitives(unittest.TestCase):
    def test_uintvar_roundtrip(self):
        for value in (0, 1, 127, 128, 16383, 16384, 2097151, 2097152, 10000000):
            encoded = m.write_uintvar(value)
            decoded, pos = m.read_uintvar(encoded, 0)
            self.assertEqual(decoded, value)
            self.assertEqual(pos, len(encoded))
        self.assertEqual(m.write_uintvar(128), bytes([0x81, 0x00]))

    def test_uintvar_truncated(self):
        with self.assertRaises(m.MmsDecodeError):
            m.read_uintvar(bytes([0x81]), 0)  # continuation bit set, nothing follows

    def test_short_integer_roundtrip(self):
        encoded = m.write_short_integer(5)
        self.assertEqual(encoded, bytes([0x85]))
        value, pos = m.read_short_integer(encoded, 0)
        self.assertEqual((value, pos), (5, 1))

    def test_short_integer_rejects_non_short_integer(self):
        with self.assertRaises(m.MmsDecodeError):
            m.read_short_integer(bytes([0x05]), 0)  # top bit clear

    def test_long_integer_roundtrip(self):
        for value in (0, 1, 255, 65536, 300000, 172287):
            encoded = m.write_long_integer(value)
            decoded, pos = m.read_long_integer(encoded, 0)
            self.assertEqual(decoded, value)
            self.assertEqual(pos, len(encoded))

    def test_value_length_roundtrip(self):
        for length in (0, 5, 30, 31, 127, 5000):
            encoded = m.write_value_length(length)
            decoded, pos = m.read_value_length(encoded, 0)
            self.assertEqual(decoded, length)
            self.assertEqual(pos, len(encoded))

    def test_text_string_roundtrip(self):
        encoded = m.write_text_string("hello")
        self.assertEqual(encoded, b"hello\x00")
        decoded, pos = m.read_text_string(encoded, 0)
        self.assertEqual((decoded, pos), ("hello", len(encoded)))

    def test_text_string_unterminated_raises(self):
        with self.assertRaises(m.MmsDecodeError):
            m.read_text_string(b"no-nul-here", 0)

    def test_encoded_string_value_ascii_uses_plain_text_string(self):
        encoded = m.write_encoded_string_value("plain ascii")
        self.assertEqual(encoded, b"plain ascii\x00")
        decoded, pos = m.read_encoded_string_value(encoded, 0)
        self.assertEqual((decoded, pos), ("plain ascii", len(encoded)))


class TestExtractWdpPort(unittest.TestCase):
    def test_16bit_port_ie(self):
        # A WAP-push port-addressed UDH minus its UDHL octet: IEI 0x05 (16-bit
        # application port addressing), IEDL 4, dest 0x0B84=2948, src 0x23F0=9200 -- the
        # standard WSP push-connectionless / MMS notification port pair.
        self.assertEqual(m.extract_wdp_port(bytes.fromhex("05040b8423f0")), (2948, 9200))

    def test_8bit_port_ie(self):
        # IEI 0x04 (8-bit ports), IEDL 2, dest 11, src 144.
        self.assertEqual(m.extract_wdp_port(bytes.fromhex("04020b90")), (11, 144))

    def test_no_port_ie_returns_none(self):
        self.assertEqual(m.extract_wdp_port(b""), (None, None))
        self.assertEqual(m.extract_wdp_port(bytes.fromhex("0102")), (None, None))


class TestNormalizeAddress(unittest.TestCase):
    def test_strips_type_plmn_suffix(self):
        self.assertEqual(m.normalize_address("+447700900123/TYPE=PLMN"), "+447700900123")

    def test_email_unchanged(self):
        self.assertEqual(m.normalize_address("a@b.c"), "a@b.c")

    def test_strips_whitespace(self):
        self.assertEqual(m.normalize_address("  +447700900123/TYPE=PLMN  "), "+447700900123")


# An m-notification-ind as carriers deliver it over a single WAP-push SMS (UDH 05040b8423f0,
# the port pair tested above). All values are fictional: a number in the Ofcom 07700 900xxx
# range, mmsc.example.test as the MMSC, a made-up transaction token and message size.
REAL_NOTIFICATION_PUSH_HEX = (
    "010607BEAF848DE8B4848C829854455354544F4B454E303031008D928918803434373730303930303132"
    "332F545950453D504C4D4E008A808805810302A2FF8E0301000083687474703A2F2F6D6D73632E6578616D"
    "706C652E746573743A383030322F3F54455354544F4B454E30303100"
)


class TestRealNotificationPush(unittest.TestCase):
    def setUp(self):
        self.data = bytes.fromhex(REAL_NOTIFICATION_PUSH_HEX)

    def test_wap_push_envelope(self):
        push = m.parse_wap_push(self.data)
        self.assertEqual(push.transaction_id, 0x01)
        self.assertEqual(push.pdu_type, 0x06)
        self.assertEqual(push.content_type, "application/vnd.wap.mms-message")
        self.assertTrue(m.is_mms_wap_push(self.data))

    def test_notification_ind_body(self):
        push = m.parse_wap_push(self.data)
        pdu = m.decode_pdu(push.body, now=1_700_000_000)
        self.assertEqual(pdu.message_type, m.M_NOTIFICATION_IND)
        self.assertEqual(pdu.transaction_id, "TESTTOKEN001")
        self.assertEqual(pdu.mms_version, (1, 2))
        self.assertEqual(pdu.from_address, "447700900123")
        self.assertEqual(pdu.headers.get("message-class"), "personal")
        self.assertEqual(pdu.message_size, 65536)
        # Expiry is a relative delta (0x02A2FF = 172799s) off the `now` we supplied.
        self.assertEqual(pdu.expiry, 1_700_000_000 + 172799)
        self.assertEqual(pdu.content_location,
                          "http://mmsc.example.test:8002/?TESTTOKEN001")

    def test_is_mms_wap_push_false_for_other_content_type(self):
        # Same envelope shape, but a different well-known content type (0x02 = text/html).
        other = bytes([0x01, 0x06]) + m.write_uintvar(1) + bytes([0x82])
        self.assertFalse(m.is_mms_wap_push(other))


class TestEncodeNotifyRespAndAcknowledge(unittest.TestCase):
    def test_encode_notifyresp_ind_exact_bytes(self):
        encoded = m.encode_notifyresp_ind("abc123", m.STATUS_RETRIEVED,
                                           report_allowed=True, version=(1, 2))
        expected = (
            bytes([0x8C, 0x83])            # X-Mms-Message-Type: m-notifyresp-ind
            + bytes([0x98]) + b"abc123\x00"  # X-Mms-Transaction-ID
            + bytes([0x8D, 0x92])           # X-Mms-MMS-Version 1.2
            + bytes([0x95, 0x81])           # X-Mms-Status: Retrieved
            + bytes([0x91, 0x80])           # X-Mms-Report-Allowed: Yes
        )
        self.assertEqual(encoded, expected)

    def test_encode_acknowledge_ind_exact_bytes(self):
        encoded = m.encode_acknowledge_ind("abc123", report_allowed=False, version=(1, 2))
        expected = (
            bytes([0x8C, 0x85])
            + bytes([0x98]) + b"abc123\x00"
            + bytes([0x8D, 0x92])
            + bytes([0x91, 0x81])           # Report-Allowed: No
        )
        self.assertEqual(encoded, expected)

    def test_notifyresp_round_trips_through_decode(self):
        encoded = m.encode_notifyresp_ind("txn-1", m.STATUS_EXPIRED)
        pdu = m.decode_pdu(encoded)
        self.assertEqual(pdu.message_type, m.M_NOTIFYRESP_IND)
        self.assertEqual(pdu.transaction_id, "txn-1")
        self.assertEqual(pdu.status, m.STATUS_EXPIRED)


class TestSendReqRoundTrip(unittest.TestCase):
    def test_round_trip_text_image_smil(self):
        text_part = m.MmsPart(content_type="text/plain",
                               data="你好，世界".encode("utf-8"),
                               name="text.txt", content_id="text1",
                               content_location="text.txt", charset="utf-8")
        jpeg_part = m.MmsPart(content_type="image/jpeg",
                               data=b"\xff\xd8\xff\xe0fakejpegbytes",
                               name="photo.jpg", content_id="img1",
                               content_location="photo.jpg")
        smil_part = m.build_smil([jpeg_part, text_part])

        encoded = m.encode_send_req(
            transaction_id="txn-send-1",
            to=["+447700900456", "friend@example.test"],
            parts=[smil_part, jpeg_part, text_part],
            subject="你好主题",
            message_class="personal",
            delivery_report=True,
            read_report=False,
            expiry_seconds=86400,
        )

        pdu = m.decode_pdu(encoded, now=1_700_000_000)
        self.assertEqual(pdu.message_type, m.M_SEND_REQ)
        self.assertEqual(pdu.transaction_id, "txn-send-1")
        self.assertEqual(pdu.mms_version, (1, 2))
        self.assertEqual(pdu.to, ["+447700900456", "friend@example.test"])
        self.assertEqual(pdu.subject, "你好主题")
        self.assertEqual(pdu.headers.get("message-class"), "personal")
        self.assertTrue(pdu.headers.get("delivery-report"))
        self.assertFalse(pdu.headers.get("read-report"))
        self.assertEqual(pdu.expiry, 1_700_000_000 + 86400)
        self.assertEqual(pdu.content_type, "application/vnd.wap.multipart.related")
        self.assertEqual(pdu.content_type_params.get("type"), "application/smil")
        self.assertEqual(pdu.content_type_params.get("start"), "<smil>")

        self.assertEqual(len(pdu.parts), 3)
        by_cid = {p.content_id: p for p in pdu.parts}

        smil_out = by_cid["smil"]
        self.assertEqual(smil_out.content_type, "application/smil")
        self.assertEqual(smil_out.name, "smil.xml")
        self.assertEqual(smil_out.charset, "utf-8")
        self.assertIn('src="photo.jpg"', smil_out.text())
        self.assertIn('src="text.txt"', smil_out.text())

        jpeg_out = by_cid["img1"]
        self.assertEqual(jpeg_out.content_type, "image/jpeg")
        self.assertEqual(jpeg_out.name, "photo.jpg")
        self.assertEqual(jpeg_out.data, jpeg_part.data)

        text_out = by_cid["text1"]
        self.assertEqual(text_out.content_type, "text/plain")
        self.assertEqual(text_out.name, "text.txt")
        self.assertEqual(text_out.charset, "utf-8")
        self.assertEqual(text_out.text(), "你好，世界")

    def test_round_trip_without_smil_uses_multipart_mixed(self):
        part = m.MmsPart(content_type="text/plain", data=b"plain ascii body",
                          name="note.txt", content_id="note1", content_location="note.txt")
        encoded = m.encode_send_req(transaction_id="txn-2", to=["+447700900789"],
                                     parts=[part], from_insert=True)
        pdu = m.decode_pdu(encoded)
        self.assertEqual(pdu.content_type, "application/vnd.wap.multipart.mixed")
        self.assertEqual(len(pdu.parts), 1)
        self.assertEqual(pdu.parts[0].text(), "plain ascii body")


class TestRetrieveConfMultipart(unittest.TestCase):
    @staticmethod
    def _multipart_entry(content_type_bytes: bytes, headers_bytes: bytes, data: bytes) -> bytes:
        headers_len = m.write_uintvar(len(content_type_bytes) + len(headers_bytes))
        data_len = m.write_uintvar(len(data))
        return headers_len + data_len + content_type_bytes + headers_bytes + data

    def test_decode_hand_built_multipart_related(self):
        text_data = "hello world".encode("utf-8")
        text_media = bytes([0x80 | 0x03])  # text/plain, well-known
        text_params = bytes([0x81]) + m.write_short_integer(106)  # Charset: utf-8
        text_ct_value = text_media + text_params
        text_ct = m.write_value_length(len(text_ct_value)) + text_ct_value
        text_headers = (bytes([0x8E]) + m.write_text_string("text.txt")
                         + bytes([0xC0]) + m.write_quoted_string("<text1>"))
        text_entry = self._multipart_entry(text_ct, text_headers, text_data)

        png_data = b"\x89PNG\r\n\x1a\nfakepngdata"
        png_ct = bytes([0x80 | 0x20])  # image/png, well-known, no params
        png_headers = (bytes([0x8E]) + m.write_text_string("image.png")
                        + bytes([0xC0]) + m.write_quoted_string("<img1>"))
        png_entry = self._multipart_entry(png_ct, png_headers, png_data)

        multipart_body = m.write_uintvar(2) + text_entry + png_entry

        header = bytearray()
        header += bytes([0x8C, m.M_RETRIEVE_CONF])
        header += bytes([0x98]) + m.write_text_string("txn-retr-1")
        header += bytes([0x8D, 0x92])
        from_value = bytes([0x80]) + b"447700900222/TYPE=PLMN\x00"
        header += bytes([0x89]) + m.write_value_length(len(from_value)) + from_value
        header += bytes([0x96]) + m.write_encoded_string_value("hello")
        related_params = (bytes([0x89]) + m.write_text_string("text/plain")
                           + bytes([0x8A]) + m.write_text_string("<text1>"))
        ct_value = bytes([0x80 | 0x33]) + related_params
        header += bytes([0x84]) + m.write_value_length(len(ct_value)) + ct_value

        pdu = m.decode_pdu(bytes(header) + multipart_body)

        self.assertEqual(pdu.message_type, m.M_RETRIEVE_CONF)
        self.assertEqual(pdu.from_address, "447700900222")
        self.assertEqual(pdu.subject, "hello")
        self.assertEqual(pdu.content_type, "application/vnd.wap.multipart.related")
        self.assertEqual(len(pdu.parts), 2)

        text_part, png_part = pdu.parts
        self.assertEqual(text_part.content_type, "text/plain")
        self.assertEqual(text_part.charset, "utf-8")
        self.assertEqual(text_part.content_id, "text1")
        self.assertEqual(text_part.text(), "hello world")

        self.assertEqual(png_part.content_type, "image/png")
        self.assertEqual(png_part.content_id, "img1")
        self.assertEqual(png_part.data, png_data)


class TestRetrieveConfError(unittest.TestCase):
    def test_mmsc_error_response(self):
        # An m-retrieve-conf error body as an MMSC returns it (fictional status text).
        body = (b'\x8c\x84\x8d\x93\x99\xe2\x9a9000:Message expired\x00'
                b'\x85\x04j\xaa\xbb\xb3\x84\x839000:Message expired')
        pdu = m.decode_pdu(body, now=1_700_000_000)
        self.assertEqual(pdu.message_type, m.M_RETRIEVE_CONF)
        self.assertEqual(pdu.mms_version, (1, 3))
        self.assertEqual(pdu.retrieve_status, 0xE2)
        self.assertEqual(pdu.headers.get("retrieve-text"), "9000:Message expired")
        self.assertEqual(len(pdu.parts), 1)
        self.assertEqual(pdu.parts[0].content_type, "text/plain")
        self.assertEqual(pdu.parts[0].text(), "9000:Message expired")


class TestSendConfAndDeliveryInd(unittest.TestCase):
    def test_send_conf(self):
        data = bytearray()
        data += bytes([0x8C, m.M_SEND_CONF])
        data += bytes([0x98]) + m.write_text_string("txn-send-1")
        data += bytes([0x92, m.RESPONSE_STATUS_OK])
        data += bytes([0x93]) + m.write_encoded_string_value("Ok")
        data += bytes([0x8B]) + m.write_text_string("<msgid123@mmsc.example.test>")
        pdu = m.decode_pdu(bytes(data))
        self.assertEqual(pdu.message_type, m.M_SEND_CONF)
        self.assertEqual(pdu.response_status, m.RESPONSE_STATUS_OK)
        self.assertEqual(pdu.headers.get("response-text"), "Ok")
        self.assertEqual(pdu.message_id, "<msgid123@mmsc.example.test>")

    def test_delivery_ind(self):
        data = bytearray()
        data += bytes([0x8C, m.M_DELIVERY_IND])
        data += bytes([0x8B]) + m.write_text_string("<msgid123@mmsc.example.test>")
        data += bytes([0x97]) + m.write_text_string("+447700900456/TYPE=PLMN")
        data += bytes([0x85]) + m.write_long_integer(1_700_000_000)
        data += bytes([0x95, m.STATUS_RETRIEVED])
        pdu = m.decode_pdu(bytes(data))
        self.assertEqual(pdu.message_type, m.M_DELIVERY_IND)
        self.assertEqual(pdu.message_id, "<msgid123@mmsc.example.test>")
        self.assertEqual(pdu.to, ["+447700900456"])
        self.assertEqual(pdu.date, 1_700_000_000)
        self.assertEqual(pdu.status, m.STATUS_RETRIEVED)

    def test_read_orig_ind(self):
        data = bytearray()
        data += bytes([0x8C, m.M_READ_ORIG_IND])
        data += bytes([0x8B]) + m.write_text_string("<msgid123@mmsc.example.test>")
        from_addr = b"447700900456/TYPE=PLMN\x00"
        data += bytes([0x89]) + m.write_value_length(1 + len(from_addr)) \
            + bytes([0x80]) + from_addr
        data += bytes([0x97]) + m.write_text_string("447700900789/TYPE=PLMN")
        data += bytes([0x85]) + m.write_long_integer(1_700_000_000)
        data += bytes([0x9B, 0x81])  # X-Mms-Read-Status: deleted-without-being-read
        pdu = m.decode_pdu(bytes(data))
        self.assertEqual(pdu.message_type, m.M_READ_ORIG_IND)
        self.assertEqual(pdu.from_address, "447700900456")
        self.assertEqual(pdu.to, ["447700900789"])
        self.assertEqual(pdu.headers.get("read-status"), 0x81)


class TestEncodedStringValueCharsets(unittest.TestCase):
    def test_ucs2_charset(self):
        encoded = m.write_encoded_string_value("你好", charset="utf-16-be")
        decoded, pos = m.read_encoded_string_value(encoded, 0)
        self.assertEqual(decoded, "你好")
        self.assertEqual(pos, len(encoded))
        # Charset is a Long-integer here (MIBenum 1000 doesn't fit a Short-integer).
        self.assertFalse(encoded[1] & 0x80)

    def test_gb2312_charset(self):
        encoded = m.write_encoded_string_value("你好", charset="gb2312")
        decoded, pos = m.read_encoded_string_value(encoded, 0)
        self.assertEqual(decoded, "你好")
        self.assertEqual(pos, len(encoded))

    def test_charset_name_and_mib_roundtrip(self):
        self.assertEqual(m.charset_name(106), "utf-8")
        self.assertEqual(m.charset_mib("utf-8"), 106)
        self.assertEqual(m.charset_name(2025), "gb2312")
        self.assertEqual(m.charset_mib("gb2312"), 2025)


class TestMalformedInputIsRobust(unittest.TestCase):
    def test_truncated_decode_pdu_raises_mms_decode_error(self):
        with self.assertRaises(m.MmsDecodeError):
            m.decode_pdu(b"\x8c")  # message-type field code with no value byte

    def test_truncated_parse_wap_push_raises_mms_decode_error(self):
        with self.assertRaises(m.MmsDecodeError):
            m.parse_wap_push(b"\x01")

    def test_empty_input_raises_mms_decode_error(self):
        with self.assertRaises(m.MmsDecodeError):
            m.parse_wap_push(b"")
        # An empty PDU body is a legal (if useless) decode: no headers, no parts.
        pdu = m.decode_pdu(b"")
        self.assertEqual(pdu.message_type, 0)
        self.assertEqual(pdu.parts, [])

    def test_is_mms_wap_push_never_raises_on_garbage(self):
        self.assertFalse(m.is_mms_wap_push(b"\xff\xff\xff"))
        self.assertFalse(m.is_mms_wap_push(b""))

    def test_fuzz_random_bytes_never_raise_unexpected_exceptions(self):
        import os
        for _ in range(500):
            junk = os.urandom(40)
            try:
                m.decode_pdu(junk)
            except m.MmsDecodeError:
                pass
            try:
                m.parse_wap_push(junk)
            except m.MmsDecodeError:
                pass

    def test_unknown_header_code_is_skipped_not_fatal(self):
        data = bytearray()
        data += bytes([0x8C, m.M_NOTIFICATION_IND])
        data += bytes([0xBF, 0x81])  # unrecognised field code 0x3F, short-integer value
        data += bytes([0x98]) + m.write_text_string("after-unknown")
        pdu = m.decode_pdu(bytes(data))
        self.assertEqual(pdu.transaction_id, "after-unknown")

    def test_truncated_multipart_raises_mms_decode_error(self):
        header = bytearray()
        header += bytes([0x8C, m.M_RETRIEVE_CONF])
        header += bytes([0x98]) + m.write_text_string("txn")
        header += bytes([0x8D, 0x92])
        ct_value = bytes([0x80 | 0x23])  # multipart.mixed
        header += bytes([0x84]) + m.write_value_length(len(ct_value)) + ct_value
        # Claim 1 entry with a data length far larger than what actually follows.
        truncated_body = m.write_uintvar(1) + m.write_uintvar(1) + m.write_uintvar(9999) \
            + bytes([0x80 | 0x03])
        with self.assertRaises(m.MmsDecodeError):
            m.decode_pdu(bytes(header) + truncated_body)

class AddressEncodingTests(unittest.TestCase):
    def test_numbers_carry_the_plmn_type_and_email_is_untouched(self):
        self.assertEqual(m.encode_address("+44 7700 900123"), "+447700900123/TYPE=PLMN")
        self.assertEqual(m.encode_address("447700900123/TYPE=PLMN"), "447700900123/TYPE=PLMN")
        self.assertEqual(m.encode_address("user@example.test"), "user@example.test")
        raw = m.encode_send_req(transaction_id="T", to=["+447700900123"],
                                parts=[m.MmsPart("text/plain", b"x", charset="utf-8")])
        self.assertIn(b"+447700900123/TYPE=PLMN\x00", raw)
        self.assertEqual(m.decode_pdu(raw).to, ["+447700900123"])


class SmilTests(unittest.TestCase):
    @staticmethod
    def smil(parts):
        parts = m.assign_references(parts)
        return ET.fromstring(m.build_smil(parts).data), parts

    def test_names_that_need_escaping_still_make_valid_xml(self):
        root, parts = self.smil([m.MmsPart("image/jpeg", b"x", name='a&b <"1">.jpg')])
        self.assertEqual([i.get("src") for i in root.iter("img")], ["part1.jpg"])
        self.assertEqual(parts[0].content_location, "part1.jpg")
        self.assertEqual(parts[0].name, 'a&b <"1">.jpg', "the display name is kept")

    def test_a_video_alone_gets_the_region_it_is_shown_in(self):
        root, _ = self.smil([m.MmsPart("video/mp4", b"x", name="v.mp4")])
        self.assertEqual([r.get("id") for r in root.iter("region")], ["Image"])
        self.assertEqual(root.find("body/par/video").get("region"), "Image")

    def test_attachments_with_the_same_name_are_referenced_apart(self):
        root, parts = self.smil([m.MmsPart("image/jpeg", b"a", name="photo.jpg"),
                                 m.MmsPart("image/jpeg", b"b", name="photo.jpg")])
        srcs = [i.get("src") for i in root.iter("img")]
        self.assertEqual(len(set(srcs)), 2)
        self.assertEqual(len({p.content_location for p in parts}), 2)
        self.assertEqual(len({p.content_id for p in parts}), 2)
        encoded = m.encode_send_req(transaction_id="t", to=["+447700900123"],
                                    parts=[m.build_smil(parts), *parts])
        decoded = {p.content_location: p.data for p in m.decode_pdu(encoded).parts}
        self.assertEqual(sorted(decoded[src] for src in srcs), [b"a", b"b"])

    def test_timed_media_lasts_as_long_as_it_plays(self):
        root, _ = self.smil([m.MmsPart("audio/amr", b"x", duration_ms=42_300),
                             m.MmsPart("video/3gpp", b"y"),
                             m.MmsPart("image/png", b"z")])
        durations = [p.get("dur") for p in root.iter("par")]
        self.assertEqual(durations, ["42300ms", None, "5000ms"],
                         "an unknown length leaves the slide to end with its media")

    def test_text_joins_the_first_slide_and_cards_are_referenced(self):
        root, _ = self.smil([m.MmsPart("image/gif", b"g"),
                             m.MmsPart("text/x-vCard", b"BEGIN:VCARD", name="Ann.vcf"),
                             m.MmsPart("text/plain", b"hi", charset="utf-8")])
        pars = root.findall("body/par")
        self.assertEqual([[c.tag for c in p] for p in pars], [["img", "text"], ["ref"]])
        self.assertEqual({r.get("id") for r in root.iter("region")}, {"Image", "Text"})
        text_only, _ = self.smil([m.MmsPart("text/plain", b"hi")])
        self.assertEqual(text_only.find("head/layout/region").get("height"), "100%")

    def test_a_part_keeps_a_safe_unique_content_id(self):
        _, parts = self.smil([m.MmsPart("image/png", b"a", content_id="<img1>"),
                              m.MmsPart("image/png", b"b", content_id="img1"),
                              m.MmsPart("image/png", b"c", content_id="smil"),
                              m.MmsPart("image/png", b"d", content_id="a b")])
        self.assertEqual(parts[0].content_id, "img1")
        self.assertEqual(len({p.content_id.lower() for p in parts} | {"smil"}), 5)

    def test_check_smil_also_resolves_cid_references(self):
        parts = m.assign_references([m.MmsPart("image/png", b"a")])
        smil = m.MmsPart("application/smil", b'<smil><head><layout><region id="Image"/></layout>'
                         b'</head><body><par><img region="Image" src="cid:part1"/></par></body>'
                         b'</smil>')
        m.check_smil(smil, parts)

    def test_check_smil_rejects_dangling_references(self):
        parts = m.assign_references([m.MmsPart("image/png", b"a")])
        smil = m.build_smil(parts)
        with self.assertRaisesRegex(ValueError, "no part"):
            m.check_smil(smil, [])
        broken = m.MmsPart("application/smil", b'<smil><body><par><img region="Image" '
                           b'src="cid:part1"/></par></body></smil>')
        with self.assertRaisesRegex(ValueError, "undeclared region"):
            m.check_smil(broken, parts)
        with self.assertRaisesRegex(ValueError, "not valid XML"):
            m.check_smil(m.MmsPart("application/smil", b"<smil><a&b/></smil>"), parts)


if __name__ == "__main__":
    unittest.main()
