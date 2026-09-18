import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from control.app import mms, mms_pdu as m, mms_transport as t, store

PROVIDERS = """<?xml version="1.0"?>
<serviceproviders format="2.0">
  <country code="xx">
    <provider><name>Virtual</name>
      <gsm><network-id mcc="001" mnc="01"/><apn value="internet"><usage type="internet"/></apn></gsm>
    </provider>
    <provider><name>Example Mobile</name>
      <gsm><network-id mcc="001" mnc="01"/>
        <apn value="mms"><usage type="mms"/><mmsc>http://mmsc.example.test:8002/</mmsc>
          <mmsproxy>192.0.2.10:8070</mmsproxy></apn>
      </gsm>
    </provider>
  </country>
</serviceproviders>
"""


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "serviceproviders.xml"
        self.db.write_text(PROVIDERS)

    def tearDown(self):
        self.temp.cleanup()

    def test_provider_database_supplies_mms_settings_by_network_code(self):
        found = t.lookup_provider("001", "001", str(self.db))
        self.assertEqual((found["apn"], found["mmsc"], found["proxy"]),
                         ("mms", "http://mmsc.example.test:8002/", "192.0.2.10:8070"))
        self.assertIsNone(t.lookup_provider("002", "01", str(self.db)))
        self.assertIsNone(t.lookup_provider("001", "01", "/nonexistent.xml"))

    def test_line_values_override_detection_as_a_whole(self):
        inst = {"mcc": "001", "mnc": "01"}
        detected = t.resolve_settings(inst, provider_path=str(self.db))
        self.assertEqual((detected["source"], detected["apn"]), ("provider", "mms"))
        self.assertTrue(detected["configured"])
        inst["mms"] = {"mmsc": "http://other.example.test/", "apn": "wap", "auto_download": False}
        own = t.resolve_settings(inst, provider_path=str(self.db))
        self.assertEqual((own["source"], own["apn"], own["proxy"]), ("line", "wap", ""))
        self.assertFalse(own["auto_download"])

    def test_proxy_parsing(self):
        self.assertEqual(t.parse_proxy("192.0.2.10:8070"), ("192.0.2.10", 8070))
        self.assertEqual(t.parse_proxy("http://proxy.example.test:3128"),
                         ("proxy.example.test", 3128))
        self.assertIsNone(t.parse_proxy(""))
        self.assertIsNone(t.parse_proxy("host:notaport"))


class FramingTests(unittest.TestCase):
    def test_request_uses_absolute_uri_through_a_proxy(self):
        raw, host, port = t.build_request("GET", "http://mmsc.example.test:8002/?id=1",
                                          headers={"Accept": "*/*"}, via_proxy=True)
        self.assertTrue(raw.startswith(b"GET http://mmsc.example.test:8002/?id=1 HTTP/1.1\r\n"))
        self.assertIn(b"Host: mmsc.example.test:8002\r\n", raw)
        direct, _h, _p = t.build_request("POST", "http://mmsc.example.test/", body=b"xy",
                                         via_proxy=False)
        self.assertTrue(direct.startswith(b"POST / HTTP/1.1\r\n"))
        self.assertTrue(direct.endswith(b"Content-Length: 2\r\n\r\nxy"))
        with self.assertRaises(t.MmsTransportError):
            t.build_request("GET", "https://mmsc.example.test/", via_proxy=False)

    def test_response_framings(self):
        head = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\n"
        self.assertEqual(t.parse_response(head + b"abc")[1], False)
        response, complete = t.parse_response(head + b"abcde")
        self.assertTrue(complete)
        self.assertEqual(response.body, b"abcde")
        chunked = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n"
        response, complete = t.parse_response(chunked)
        self.assertEqual((response.body, complete), (b"abcde", True))
        self.assertEqual(t.parse_response(b"HTTP/1.1 200")[0], None)


class FakeQuectel:
    """A Quectel module behind ModemManager's command channel, with one MMSC behind it."""

    def __init__(self, reply: bytes, *, contexts=None, supported=True, active=False):
        self.reply = reply
        self.contexts = contexts or {1: "internet", 2: "ims"}
        self.supported = supported
        self.active = {c for c, a in self.contexts.items() if active and a.casefold() == "mms"}
        self.commands = []
        self.sent = bytearray()
        self.unread = b""
        self.state = 0
        self.connected_to = None

    def __call__(self, args, **kwargs):
        command, timeout = args[-2], int(args[-1])
        self.commands.append(command)
        ok, text = self.handle(command, timeout)
        if not ok:
            return Result("", 1, f"Call failed: {text}")
        return Result(json.dumps({"type": "s", "data": [text]}))

    def handle(self, command, timeout):
        if command == "AT+QICSGP=?":
            return (True, '+QICSGP: (1-16),(1-3)') if self.supported else (False, "Unknown error")
        if command == "AT+CGDCONT?":
            return True, "\r\n".join(f'+CGDCONT: {c},"IP","{a}","0.0.0.0",0,0'
                                     for c, a in sorted(self.contexts.items()))
        match = re.fullmatch(r'AT\+QICSGP=(\d+),1,"([^"]*)".*', command)
        if match:
            self.contexts[int(match.group(1))] = match.group(2)
            return True, ""
        if command == "AT+QIACT?":
            return True, "\r\n".join(f'+QIACT: {c},1,1,"10.0.0.2"' for c in sorted(self.active))
        if command.startswith("AT+QIACT="):
            self.active.add(int(command.split("=")[1]))
            return True, ""
        if command.startswith("AT+QIDEACT="):
            self.active.discard(int(command.split("=")[1]))
            return True, ""
        if command.startswith('AT+QICFG="dataformat"'):
            return True, ""
        if command.startswith("AT+QICLOSE="):
            self.state = 0
            return True, ""
        match = re.fullmatch(r'AT\+QIOPEN=(\d+),11,"TCP","([^"]+)",(\d+),0,0', command)
        if match:
            self.connected_to = (match.group(2), int(match.group(3)))
            self.state = 2
            return True, ""
        if command == "AT+QISTATE=1,11":
            if not self.state:
                return True, ""
            return True, f'+QIOPEN: 11,0\r\n\r\n+QISTATE: 11,"TCP","{self.connected_to[0]}",' \
                         f'{self.connected_to[1]},9000,{self.state},5,11,0,"usbat"'
        match = re.fullmatch(r'AT\+QISENDEX=11,"([0-9a-f]+)"', command)
        if match:
            data = bytes.fromhex(match.group(1))
            if len(data) > 256:
                return False, "Unknown error"
            self.sent += data
            if self.sent.endswith(b"\r\n\r\n") or b"Content-Length" not in self.sent or \
                    len(self.sent) >= self._expected_length():
                self.unread, self.state = self.reply, 4
            return False, "Response timeout: Serial command timed out"
        if command == "AT+QISEND=11,0":
            return True, f"SEND OK\r\n\r\n+QISEND: {len(self.sent)},0,{len(self.sent)}"
        if command.startswith("AT+QIRD=11,"):
            size = int(command.split(",")[1])
            chunk, self.unread = self.unread[:size], self.unread[size:]
            return True, f"+QIRD: {len(chunk)}" + (f"\r\n{chunk.hex().upper()}" if chunk else "")
        return False, "Unknown error"

    def _expected_length(self):
        head, _, body = bytes(self.sent).partition(b"\r\n\r\n")
        match = re.search(rb"Content-Length: (\d+)", head)
        return len(head) + 4 + int(match.group(1)) if match else 0


class Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


SETTINGS = {"enabled": True, "configured": True, "apn": "mms", "mmsc": "http://mmsc.example.test:8002/",
            "proxy": "192.0.2.10:8070", "username": "", "password": "", "transport": "auto",
            "user_agent": "Android-Mms/2.0"}


def http_reply(body: bytes) -> bytes:
    return (b"HTTP/1.1 200 OK\r\nContent-Type: application/vnd.wap.mms-message\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)


class ModemSocketTests(unittest.TestCase):
    def client(self, fake):
        return t.ModemSocketHttp(t.ModemCommand("/org/freedesktop/ModemManager1/Modem/0", fake),
                                 SETTINGS, sleep=lambda _s: None)

    def test_get_through_proxy_on_a_new_mms_context(self):
        payload = bytes(range(256)) * 12           # longer than one QIRD read
        fake = FakeQuectel(http_reply(payload))
        response = self.client(fake).request("GET", "http://mmsc.example.test:8002/?id=1",
                                             headers={"Accept": "*/*"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, payload)
        self.assertEqual(fake.contexts[4], "mms", "first free context above the defaults")
        self.assertEqual(fake.connected_to, ("192.0.2.10", 8070))
        self.assertTrue(fake.sent.startswith(b"GET http://mmsc.example.test:8002/?id=1 "))
        self.assertIn("AT+QIDEACT=4", fake.commands, "a context it activated is released")
        self.assertEqual(fake.commands[-1], "AT+QIDEACT=4")

    def test_existing_mms_context_is_reused_and_left_active(self):
        fake = FakeQuectel(http_reply(b"ok"), contexts={1: "internet", 5: "MMS"}, active=True)
        self.client(fake).request("POST", "http://mmsc.example.test:8002/", body=b"x" * 700)
        self.assertNotIn(4, fake.contexts)
        self.assertFalse(any(c.startswith("AT+QICSGP=5") for c in fake.commands))
        self.assertFalse(any(c.startswith("AT+QIDEACT") for c in fake.commands))
        self.assertTrue(fake.sent.endswith(b"x" * 700))
        chunks = [c for c in fake.commands if c.startswith("AT+QISENDEX")]
        self.assertTrue(all(len(c) <= len('AT+QISENDEX=11,""') + 512 for c in chunks))

    def test_command_channel_refuses_large_requests_before_touching_the_modem(self):
        fake = FakeQuectel(http_reply(b"ok"))
        with self.assertRaises(t.MmsTransportError) as raised:
            self.client(fake).request("POST", "http://mmsc.example.test:8002/", body=b"x" * 5000)
        self.assertFalse(raised.exception.retryable)
        self.assertFalse(any(c.startswith("AT+QIOPEN") for c in fake.commands))

    def test_every_chunk_is_followed_by_a_completing_query(self):
        fake = FakeQuectel(http_reply(b"ok"))
        self.client(fake).request("POST", "http://mmsc.example.test:8002/", body=b"x" * 900)
        sends = [i for i, c in enumerate(fake.commands) if c.startswith("AT+QISENDEX")]
        self.assertGreater(len(sends), 1)
        for index in sends:
            self.assertEqual(fake.commands[index + 1], "AT+QISEND=11,0")

    def test_short_send_is_an_error(self):
        fake = FakeQuectel(http_reply(b"ok"))
        original = fake.handle

        def lossy(command, timeout):
            if command == "AT+QISEND=11,0":
                return True, "+QISEND: 1,0,1"
            return original(command, timeout)

        fake.handle = lossy
        with self.assertRaises(t.MmsTransportError):
            self.client(fake).request("GET", "http://mmsc.example.test:8002/?id=1")

    def test_unsupported_modem_falls_back_to_host_in_auto(self):
        fake = FakeQuectel(b"", supported=False)
        client = t.client_for(SETTINGS, "/org/freedesktop/ModemManager1/Modem/0", runner=fake)
        self.assertIsInstance(client, t.HostHttp)
        with self.assertRaises(t.MmsTransportError):
            t.client_for({**SETTINGS, "transport": "modem"}, None, runner=fake)


class FakeSerialPort:
    """A serial port in front of FakeQuectel's command handling, with QISEND's '>' prompt."""

    def __init__(self, modem: "FakeQuectel"):
        self.modem = modem
        self.out = b""
        self.pending = None
        self.writes = []
        self.closed = False

    def reset_input_buffer(self):
        self.out = b""

    def read(self, size):
        chunk, self.out = self.out[:size], self.out[size:]
        return chunk

    def write(self, data):
        self.writes.append(data)
        if self.pending is not None:
            sid, length = self.pending
            self.pending = None
            self.modem.sent += data[:length]
            if len(self.modem.sent) >= self.modem._expected_length() or \
                    b"Content-Length" not in self.modem.sent:
                self.modem.unread, self.modem.state = self.modem.reply, 4
            self.out += b"\r\nSEND OK\r\n"
            return
        command = data.decode("ascii").rstrip("\r")
        self.modem.commands.append(command)
        match = re.fullmatch(r"AT\+QISEND=(\d+),(\d+)", command)
        if match and match.group(2) != "0":
            self.pending = (int(match.group(1)), int(match.group(2)))
            self.out += b"\r\n> "
            return
        if command == "ATE0":
            ok, text = True, ""
        else:
            ok, text = self.modem.handle(command, 5)
        self.out += (f"\r\n{text}\r\n" if text else "").encode() + \
            (b"\r\nOK\r\n" if ok else b"\r\nERROR\r\n")

    def close(self):
        self.closed = True


class SerialChannelTests(unittest.TestCase):
    def test_large_upload_uses_the_binary_prompt_send(self):
        payload = bytes(range(256)) * 4
        modem = FakeQuectel(http_reply(payload))
        ports = []
        channel = t.SerialAtChannel("/dev/ttyFAKE",
                                    serial_factory=lambda _p: ports.append(FakeSerialPort(modem))
                                    or ports[-1])
        client = t.ModemSocketHttp(channel, SETTINGS, sleep=lambda _s: None)
        body = b"\x00\xff" * 20_000           # 40 KB, binary, far over the command-channel cap
        response = client.request("POST", "http://mmsc.example.test:8002/", body=body)
        self.assertEqual(response.body, payload)
        self.assertTrue(modem.sent.endswith(body))
        self.assertIn('AT+QICFG="dataformat",0,1', modem.commands)
        self.assertFalse(any(c.startswith("AT+QISENDEX") for c in modem.commands))
        self.assertTrue(ports[0].closed, "the port is released after each exchange")

    def test_port_discovery_takes_an_ignored_at_port_only(self):
        def runner(args, **kwargs):
            if args[:2] == ["mmcli", "-m"]:
                return Result(json.dumps({"modem": {"generic": {"ports": [
                    "cdc-wdm0 (qmi)", "ttyUSB0 (ignored)", "ttyUSB2 (at)", "ttyUSB3 (ignored)"]}}}))
            if args[:2] == ["udevadm", "info"]:
                typed = {"/dev/ttyUSB0": "ID_MM_PORT_TYPE_QCDM=1",
                         "/dev/ttyUSB3": "ID_MM_PORT_TYPE_AT_SECONDARY=1\nID_MM_PORT_IGNORE=1"}
                return Result(typed.get(args[-1], ""))
            return Result(returncode=1)

        self.assertEqual(t.find_at_port("/m/0", runner, environ={}), "/dev/ttyUSB3")
        self.assertIsNone(t.find_at_port("/m/0", lambda *a, **k: Result(returncode=1), environ={}))
        self.assertIsNone(t.find_at_port("/m/0", runner,
                                         environ={t.AT_PORT_ENV: "/dev/does-not-exist"}))


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, *, body=b"", headers=None, timeout=60):
        self.requests.append((method, url, body, headers))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def retrieve_conf(text="你好", image=b"\xff\xd8\xff\xe0", status=None) -> bytes:
    parts = [m.MmsPart("text/plain", text.encode(), name="text.txt", content_id="text",
                       content_location="text.txt", charset="utf-8"),
             m.MmsPart("image/jpeg", image, name="photo.jpg", content_id="photo",
                       content_location="photo.jpg")]
    parts.insert(0, m.build_smil(parts))
    raw = bytearray(m.encode_send_req(transaction_id="R1", to=["+447700900123"], parts=parts,
                                      subject="Hi"))
    raw[1] = m.M_RETRIEVE_CONF
    if status is not None:
        index = raw.index(b"\x8d") + 2
        raw[index:index] = bytes([0x99, status])
    return bytes(raw)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patch = patch.multiple(store, DATA_DIR=str(root),
                                    DB_PATH=str(root / "mdd-sim-gateway.sqlite"),
                                    PREVIOUS_DB_PATH=str(root / "vowifi.sqlite"))
        self.patch.start()
        store.init()
        from tests.test_mms_receive import notification_push
        self.rec = mms.handle_wap_push("1", "99", notification_push(), transport="vowifi",
                                       sent_ts=1_000, now=1_000)["message"]
        self.inst = {"id": "1", "mms": {k: SETTINGS[k] for k in ("apn", "mmsc", "proxy")}}

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_retrieved_mms_is_stored_and_acknowledged(self):
        client = FakeClient([t.HttpResponse(200, {}, retrieve_conf()),
                             t.HttpResponse(204, {}, b"")])
        result = mms.download(self.inst, self.rec["id"], client=client, now=2_000)
        self.assertTrue(result["ok"])
        rec = store.get_message(self.rec["id"])
        self.assertEqual(rec["body"], "你好")
        self.assertEqual(rec["mms"]["state"], "retrieved")
        self.assertEqual([p["content_type"] for p in rec["mms"]["parts"]],
                         ["application/smil", "text/plain", "image/jpeg"])
        method, url, body, headers = client.requests[1]
        self.assertEqual((method, url), ("POST", SETTINGS["mmsc"]))
        ack = m.decode_pdu(body)
        self.assertEqual((ack.message_type, ack.transaction_id, ack.status),
                         (m.M_NOTIFYRESP_IND, "T1", m.STATUS_RETRIEVED))
        self.assertEqual(store.due_mms_downloads(now=10**10), [])

    def test_storage_failure_while_saving_parts_leaves_the_mms_retryable(self):
        import errno
        client = FakeClient([t.HttpResponse(200, {}, retrieve_conf())])
        full = OSError(errno.ENOSPC, "No space left on device")
        with patch.object(store, "save_mms_content", side_effect=full):
            result = mms.download(self.inst, self.rec["id"], client=client, now=2_000)
        self.assertEqual((result["ok"], result["final"]), (False, False))
        row = store.mms_for_download(self.rec["id"])
        self.assertEqual((row["state"], row["next_attempt_ts"]), ("failed", 2_060))
        self.assertIn("No space left", row["last_error"])
        self.assertEqual([r["message_id"] for r in store.due_mms_downloads(now=2_060)],
                         [self.rec["id"]], "the queue picks it up again")

        retry = FakeClient([t.HttpResponse(200, {}, retrieve_conf()),
                            t.HttpResponse(204, {}, b"")])
        self.assertTrue(mms.download(self.inst, self.rec["id"], client=retry, now=2_060)["ok"])
        self.assertEqual(store.mms_for_download(self.rec["id"])["state"], "retrieved")

    def test_worker_requeues_a_download_whose_failure_could_not_be_recorded(self):
        import asyncio
        from control.app import main

        def crash(inst, mid):
            store.set_mms_state(mid, "downloading")
            raise RuntimeError("database is locked")

        row = store.mms_for_download(self.rec["id"])
        inst = {"id": "1", "mms": {"mmsc": SETTINGS["mmsc"], "apn": "mms"}}
        with patch.object(main.cfg, "get_instance", return_value=inst), \
                patch.object(main.mms, "download", side_effect=crash):
            asyncio.run(main._process_mms_download(row))
        row = store.mms_for_download(self.rec["id"])
        self.assertEqual(row["state"], "failed")
        self.assertIsNotNone(row["next_attempt_ts"])

    def test_a_download_stuck_in_progress_can_be_retried_by_hand_once_stale(self):
        store.set_mms_state(self.rec["id"], "downloading")
        updated = store.mms_for_download(self.rec["id"])["updated_ts"]
        self.assertFalse(store.schedule_mms_download("1", self.rec["id"], now=updated + 5),
                         "a download in progress is not restarted underneath itself")
        self.assertTrue(store.schedule_mms_download(
            "1", self.rec["id"], now=updated + store.STUCK_DOWNLOAD_SECONDS))

    def test_transient_failure_is_rescheduled_and_permanent_one_is_final(self):
        client = FakeClient([t.MmsTransportError("timed out")])
        result = mms.download(self.inst, self.rec["id"], client=client, now=2_000)
        self.assertFalse(result["final"])
        row = store.mms_for_download(self.rec["id"])
        self.assertEqual((row["state"], row["next_attempt_ts"]), ("failed", 2_060))

        gone = FakeClient([t.HttpResponse(200, {}, retrieve_conf(status=0xE2))])
        result = mms.download(self.inst, self.rec["id"], client=gone, now=2_100)
        self.assertTrue(result["final"])
        row = store.mms_for_download(self.rec["id"])
        self.assertEqual((row["state"], row["next_attempt_ts"]), ("failed", None))
        self.assertTrue(store.schedule_mms_download("1", self.rec["id"], now=3_000))

    def test_expired_notification_is_neither_fetched_nor_retried(self):
        client = FakeClient([])
        result = mms.download(self.inst, self.rec["id"], client=client, now=10**9 * 3)
        self.assertTrue(result["final"] and result["expired"])
        self.assertEqual(client.requests, [])
        self.assertEqual(store.mms_for_download(self.rec["id"])["state"], "expired")
        store.schedule_mms_download("1", self.rec["id"], now=10**9 * 3)
        client = FakeClient([t.MmsTransportError("timed out")])
        result = mms.download(self.inst, self.rec["id"], client=client, now=10**9 * 3)
        self.assertEqual(len(client.requests), 1, "a manual retry still asks the MMSC")
        self.assertEqual(store.mms_for_download(self.rec["id"])["next_attempt_ts"], None)

    def test_interrupted_work_is_recovered_after_restart(self):
        store.set_mms_state(self.rec["id"], "downloading")
        out = store.create_outgoing_mms("1", "+447700900123", to_addrs=["+447700900123"],
                                        subject="", body="x", transaction_id="TX")
        self.assertEqual(store.reset_interrupted_mms(now=5_000), 2)
        self.assertEqual(store.mms_for_download(self.rec["id"])["state"], "notified")
        self.assertEqual(store.get_message(out["id"])["status"], "unknown")


class SendTests(DownloadTests):
    def compose(self, text="hello", attachments=None):
        return mms.create_outgoing("1", ["+447700900123"], text,
                                   attachments if attachments is not None else
                                   [{"name": "p.jpg", "content_type": "image/jpeg",
                                     "data": b"\xff\xd8" * 10}])

    def test_validation(self):
        settings = {"max_size": 1024}
        self.assertIn("recipient", mms.validate_outgoing([], "x", [], settings))
        self.assertIn("not a phone", mms.validate_outgoing(["12; rm"], "x", [], settings))
        self.assertIn("text or an attachment", mms.validate_outgoing(["+447700900123"], " ",
                                                                      [], settings))
        self.assertIn("cannot be sent", mms.validate_outgoing(
            ["+447700900123"], "", [{"content_type": "text/html", "data": b"x"}], settings))
        self.assertIn("allows 1 KB", mms.validate_outgoing(
            ["+447700900123"], "", [{"content_type": "image/png", "data": b"x" * 2000}],
            settings))
        self.assertIsNone(mms.validate_outgoing(["+447700900123", "a@example.test"], "hi", [],
                                                settings))
        self.assertEqual(mms.parse_recipients("+447700900123; +447700900124,+447700900123"),
                         ["+447700900123", "+447700900124"])

    def test_accepted_send_records_the_mmsc_message_id(self):
        rec = self.compose()
        self.assertEqual((rec["status"], rec["mms"]["state"]), ("pending", "sending"))
        conf = bytes([0x8C, 0x81, 0x98]) + m.write_text_string("x") + b"\x8D\x92" + \
            bytes([0x92, 0x80]) + b"\x8B" + m.write_text_string("MSG-7")
        client = FakeClient([t.HttpResponse(200, {}, conf)])
        result = mms.send(self.inst, rec["id"], client=client)
        self.assertEqual(result["status"], "sent")
        sent = m.decode_pdu(client.requests[0][2])
        self.assertEqual(sent.message_type, m.M_SEND_REQ)
        self.assertEqual(sent.to, ["+447700900123"])
        self.assertEqual([p.content_type for p in sent.parts],
                         ["application/smil", "text/plain", "image/jpeg"])
        stored = store.get_message(rec["id"])
        self.assertEqual((stored["status"], stored["mms"]["state"], stored["mms"]["message_ref"]),
                         ("sent", "sent", "MSG-7"))

    def test_same_named_attachments_are_sent_with_distinct_references(self):
        jpeg = b"\xff\xd8\xff\xe0" + b"x" * 8
        rec = self.compose("caption & more", [
            {"name": "photo.jpg", "content_type": "image/jpeg", "data": jpeg + b"1"},
            {"name": "photo.jpg", "content_type": "image/jpeg", "data": jpeg + b"2"}])
        conf = bytes([0x8C, 0x81, 0x98]) + m.write_text_string("x") + b"\x8D\x92" + \
            bytes([0x92, 0x80])
        client = FakeClient([t.HttpResponse(200, {}, conf)])
        self.assertEqual(mms.send(self.inst, rec["id"], client=client)["status"], "sent")
        sent = m.decode_pdu(client.requests[0][2])
        smil = sent.parts[0]
        by_id = {p.content_id: p for p in sent.parts}
        self.assertEqual(len(by_id), 4)
        m.check_smil(smil, sent.parts[1:])
        images = [p for p in sent.parts if p.content_type == "image/jpeg"]
        self.assertEqual([p.name for p in images], ["photo.jpg", "photo.jpg"])
        self.assertEqual(len({p.content_location for p in images}), 2)

    def test_refusal_is_failed_and_a_lost_answer_is_unknown(self):
        rec = self.compose()
        refused = bytes([0x8C, 0x81, 0x98]) + m.write_text_string("x") + b"\x8D\x92" + \
            bytes([0x92, 0xE3])
        result = mms.send(self.inst, rec["id"],
                          client=FakeClient([t.HttpResponse(200, {}, refused)]))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(store.get_message(rec["id"])["status"], "failed")

        rec = self.compose("again")
        lost = t.MmsTransportError("timed out waiting for the MMSC", after_send=True)
        result = mms.send(self.inst, rec["id"], client=FakeClient([lost]))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(store.get_message(rec["id"])["status"], "unknown")


class ExchangeLockTests(unittest.TestCase):
    def test_exchanges_share_a_lock_only_when_they_share_a_modem(self):
        import threading
        a, b = mms.io_lock("/modem/0"), mms.io_lock("/modem/1")
        self.assertIs(a, mms.io_lock("/modem/0"))
        self.assertIsNot(a, b)
        with a:
            acquired = []
            other = threading.Thread(target=lambda: acquired.append(b.acquire(timeout=1)))
            other.start(); other.join()
            same = threading.Thread(target=lambda: acquired.append(a.acquire(timeout=0.05)))
            same.start(); same.join()
        b.release()
        self.assertEqual(acquired, [True, False])

    def test_the_client_is_opened_before_its_modem_lock_is_taken(self):
        opened = []

        def fake_open(inst, settings, runner=None):
            opened.append(mms.io_lock("/modem/7").locked())
            return FakeClient([]), "/modem/7"

        with patch.object(mms, "open_client", side_effect=fake_open):
            with mms._exchange({}, {}, None, None) as client:
                self.assertTrue(mms.io_lock("/modem/7").locked())
                self.assertIsInstance(client, FakeClient)
        self.assertEqual(opened, [False])
        self.assertFalse(mms.io_lock("/modem/7").locked())


if __name__ == "__main__":
    unittest.main()
