"""Issue #51: SELECT/READ paths must work on both reader response shapes.

TPDU-level T=0 readers answer case-4 APDUs with 61xx and expect an explicit
GET RESPONSE; APDU-level readers (e.g. some SCR Prime firmware batches) and T=1
cards hand the data back with 9000 directly. The old code treated SW1=61 as the
only SELECT success, so the second shape read as "ADF.USIM select failed" and the
UI showed a healthy card as PIN-locked.
"""
import sys
import types
import unittest

# Pure-logic tests must stay runnable on hosts without libpcsclite/pyscard.
try:
    from control.app import sim
except ModuleNotFoundError as exc:
    if not str(exc.name).startswith("smartcard"):
        raise
    smartcard = types.ModuleType("smartcard")
    system = types.ModuleType("smartcard.System"); system.readers = lambda: []
    connection = types.ModuleType("smartcard.CardConnection"); connection.CardConnection = object
    exceptions = types.ModuleType("smartcard.Exceptions")
    exceptions.NoCardException = exceptions.CardConnectionException = RuntimeError
    scard = types.ModuleType("smartcard.scard")
    scard.SCardBeginTransaction = scard.SCardEndTransaction = lambda *args: None
    scard.SCARD_LEAVE_CARD = 0
    sys.modules.update({"smartcard": smartcard, "smartcard.System": system,
                        "smartcard.CardConnection": connection,
                        "smartcard.Exceptions": exceptions, "smartcard.scard": scard})
    from control.app import sim

IMSI = "234101234567890"
USIM_AID = "A0000000871002FF86FF0389FFFFFFFF"
PIN = "1234"


def _ef_imsi_bytes():
    # Inverse of dec_imsi: [len=8][swap_nibbles(parity nibble + digits)]
    swapped = "9" + IMSI
    return sim._hx("08" + sim.swap_nibbles(swapped))


class FakeUsim:
    """Scripted USIM behind a reader of the given response shape.

    mode='tpdu':   case-4 commands answer (no data, 61, len); data comes only via
                   an explicit GET RESPONSE — classic T=0 TPDU-level reader.
    mode='direct': case-4 commands answer (data, 90, 00) in one step — APDU-level
                   reader or T=1 card, the shape issue #51's reader produces.
    """

    def __init__(self, mode, pin_enabled=False, broken_get_response=()):
        assert mode in ("tpdu", "direct")
        self.mode = mode
        self.pin_enabled = pin_enabled
        # Files whose 61xx the card raises but whose GET RESPONSE it then refuses. Issue #60:
        # 61xx already means "command accepted", so a refused body fetch must not be read as
        # a failed SELECT.
        self.broken_get_response = set(broken_get_response)
        self.verified = False
        self.selected = None
        self.pending = None
        self.pending_from = None
        self.log = []

    # -- response shaping ------------------------------------------------------
    def _case4(self, data, origin=None):
        if self.mode == "direct":
            return list(data), 0x90, 0x00
        self.pending = list(data)
        self.pending_from = origin
        return [], 0x61, len(data)

    def transmit(self, apdu):
        self.log.append(bytes(apdu).hex())
        ins, p1 = apdu[1], apdu[2]
        if ins == 0xC0:  # GET RESPONSE
            origin, self.pending_from = self.pending_from, None
            if origin in self.broken_get_response:
                self.pending = None
                return [], 0x6F, 0x00
            data, self.pending = self.pending or [], None
            return data, 0x90, 0x00
        if ins == 0xA4:  # SELECT
            body = bytes(apdu[5:5 + apdu[4]]).hex().upper() if len(apdu) > 5 else ""
            if p1 == 0x04 and body == USIM_AID:
                self.selected = "adf"
                return self._case4(sim._hx("62058A0105FFFF"), "adf")
            if body == "2F00":
                self.selected = "dir"
                # FCP: 62 06 82 04 42 21 00 26 -> record length fcp[7] = 0x26
                return self._case4(sim._hx("6206820442210026"), "dir")
            if body in ("3F00", "6F07"):
                self.selected = body
                return self._case4(sim._hx("62058A0105FFFF"), body)
            self.selected = None
            return [], 0x6A, 0x82  # file not found (EF_AD/SPN/GID/SMSP not scripted)
        if ins == 0xB2 and self.selected == "dir":  # READ RECORD in EF_DIR
            if apdu[2] != 1:
                return [], 0x6A, 0x83  # record not found
            rec = [0x61, 0x14, 0x4F, 0x10] + sim._hx(USIM_AID)
            rec += [0xFF] * (0x26 - len(rec))
            return rec, 0x90, 0x00
        if ins == 0x20:  # VERIFY CHV1
            if apdu[4] == 0:  # retry-counter probe
                if self.pin_enabled and not self.verified:
                    return [], 0x63, 0xC3
                return [], 0x90, 0x00
            digits = bytes(b for b in apdu[5:] if b != 0xFF).decode()
            if digits == PIN:
                self.verified = True
                return [], 0x90, 0x00
            return [], 0x63, 0xC2
        if ins == 0xB0 and self.selected == "6F07":  # READ BINARY EF_IMSI
            if self.pin_enabled and not self.verified:
                return [], 0x69, 0x82
            return _ef_imsi_bytes(), 0x90, 0x00
        return [], 0x6D, 0x00

    def connect(self):
        pass

    def disconnect(self):
        pass


class FakeReader:
    def __init__(self, card):
        self.card = card

    def createConnection(self):
        return self.card

    def __str__(self):
        return "Fake Reader 00 00"


class ReaderShapeTests(unittest.TestCase):
    def _read(self, mode, pin_enabled=False, pin=None, broken_get_response=()):
        card = FakeUsim(mode, pin_enabled=pin_enabled,
                        broken_get_response=broken_get_response)
        old = sim.readers
        sim.readers = lambda: [FakeReader(card)]
        try:
            return sim.read_card(0, pin), card
        finally:
            sim.readers = old

    def test_tpdu_reader_reads_imsi(self):
        info, _ = self._read("tpdu")
        self.assertEqual(info.imsi, IMSI)
        self.assertIs(info.pin_enabled, False)
        self.assertIsNone(info.error)

    def test_direct_reader_reads_imsi(self):
        # Issue #51 regression: this shape used to die at "ADF.USIM select failed"
        # and render as a PIN-locked card.
        info, _ = self._read("direct")
        self.assertEqual(info.imsi, IMSI)
        self.assertIs(info.pin_enabled, False)
        self.assertIsNone(info.error)

    def test_direct_reader_locked_card_still_reports_pin(self):
        info, _ = self._read("direct", pin_enabled=True)
        self.assertIsNone(info.imsi)
        self.assertIs(info.pin_enabled, True)
        self.assertEqual(info.pin_tries, 3)

    def test_direct_reader_verify_pin_unlocks(self):
        info, card = self._read("direct", pin_enabled=True, pin=PIN)
        self.assertTrue(card.verified)
        self.assertEqual(info.imsi, IMSI)

    def test_select_survives_a_refused_get_response(self):
        """Issue #60 regression: v1.9.0 read SELECT ADF.USIM's success off the GET RESPONSE
        that follows it, so a card that raises 61xx (accepted) but refuses to hand the FCP
        back read as "ADF.USIM select failed" — read_card returned with every PIN field
        unset and the start preflight asked for a PIN the card does not even use."""
        info, _ = self._read("tpdu", broken_get_response={"adf"})
        self.assertEqual(info.imsi, IMSI)
        self.assertIs(info.pin_enabled, False)
        self.assertIsNone(info.error)

    def test_verify_pin_on_direct_reader(self):
        card = FakeUsim("direct", pin_enabled=True)
        old = sim.readers
        sim.readers = lambda: [FakeReader(card)]
        try:
            result = sim.verify_pin(PIN, 0)
        finally:
            sim.readers = old
        self.assertTrue(result["ok"], result)


class XfrTests(unittest.TestCase):
    def test_chained_61_responses_are_concatenated(self):
        class Conn:
            def __init__(self):
                self.calls = 0

            def transmit(self, apdu):
                self.calls += 1
                if self.calls == 1:
                    return [], 0x61, 2
                if self.calls == 2:
                    self.assertion = apdu == [0x00, 0xC0, 0x00, 0x00, 2]
                    return [1, 2], 0x61, 1
                return [3], 0x90, 0x00

        data, s1, s2 = sim._xfr(Conn(), sim._hx("00a40004023f0000"))
        self.assertEqual((data, s1, s2), ([1, 2, 3], 0x90, 0x00))

    def test_6c_retries_with_the_cards_le(self):
        seen = []

        class Conn:
            def transmit(self, apdu):
                seen.append(list(apdu))
                if len(seen) == 1:
                    return [], 0x6C, 0x0A
                return list(range(10)), 0x90, 0x00

        data, s1, s2 = sim._xfr(Conn(), sim._hx("00b0000009"))
        self.assertEqual(seen[1], [0x00, 0xB0, 0x00, 0x00, 0x0A])
        self.assertEqual((len(data), s1), (10, 0x90))

    def test_6c_retry_keeps_the_command_body(self):
        """A case-4 SELECT retried on 6Cxx must keep Lc and the file id. Truncating to
        CLA/INS/P1/P2 re-sent "00A40004 <Le>", a malformed command the card can only
        reject, which surfaced as an unselectable file (issue #60)."""
        seen = []

        class Conn:
            def transmit(self, apdu):
                seen.append(list(apdu))
                if len(seen) == 1:
                    return [], 0x6C, 0x07
                return list(range(7)), 0x90, 0x00

        data, s1, s2 = sim._xfr(Conn(), sim._hx("00a40004022f0000"))
        self.assertEqual(seen[1], sim._hx("00a40004022f00") + [0x07])
        self.assertEqual((len(data), s1, s2), (7, 0x90, 0x00))

    def test_61_is_success_even_when_get_response_is_refused(self):
        """61xx is the card accepting the command; the GET RESPONSE only fetches the body.
        Callers that need the body validate it, so a refused fetch must not be reported as
        a failed command (issue #60)."""
        class Conn:
            def __init__(self):
                self.calls = 0

            def transmit(self, apdu):
                self.calls += 1
                return ([], 0x61, 0x1F) if self.calls == 1 else ([], 0x6F, 0x00)

        data, s1, s2 = sim._xfr(Conn(), sim._hx("00a40404") + [4] + sim._hx("A0000000"))
        self.assertEqual((data, s1, s2), ([], 0x90, 0x00))

    def test_apdu_with_le_shapes(self):
        self.assertEqual(sim._apdu_with_le(sim._hx("00b0000009"), 0x0A),
                         sim._hx("00b000000a"))                       # case 2: replace Le
        self.assertEqual(sim._apdu_with_le(sim._hx("00a40004022f0000"), 0x07),
                         sim._hx("00a40004022f0007"))                 # case 4: keep Lc+data
        self.assertEqual(sim._apdu_with_le(sim._hx("00a4040402ff01"), 0x12),
                         sim._hx("00a4040402ff0112"))                 # case 3: append Le


class ValidationTests(unittest.TestCase):
    def test_dec_imsi_round_trip(self):
        self.assertEqual(sim.dec_imsi(bytes(_ef_imsi_bytes()).hex()), IMSI)

    def test_dec_imsi_rejects_garbage(self):
        self.assertIsNone(sim.dec_imsi("ff" * 9))          # unprogrammed EF
        self.assertIsNone(sim.dec_imsi("62058A0105FFFF"))  # an FCP misread as EF data
        self.assertIsNone(sim.dec_imsi(""))

    def test_dec_iccid_rejects_short_or_garbled_reads(self):
        """The start preflight convicts a reader whose live ICCID differs from the line's, so
        a truncated or non-numeric EF.ICCID must decode to "" rather than to a wrong number."""
        iccid = "8900010000000000013"                  # 19 digits, F-padded to 10 bytes
        on_card = sim.swap_nibbles(iccid + "f")
        self.assertEqual(sim.dec_iccid(on_card), iccid)
        self.assertEqual(sim.dec_iccid(on_card[:12]), "")   # short read -> not an identity
        self.assertEqual(sim.dec_iccid("ff" * 10), "")      # unprogrammed EF
        self.assertEqual(sim.dec_iccid(""), "")

    def test_pin_body_validation(self):
        self.assertEqual(len(sim._pin_body("1234")), 8)
        self.assertIsNone(sim._pin_body("123"))        # too short
        self.assertIsNone(sim._pin_body("123456789"))  # would produce a negative pad
        self.assertIsNone(sim._pin_body("12ab"))

    def test_verify_pin_rejects_malformed_pin_without_card_io(self):
        old = sim.readers
        sim.readers = lambda: (_ for _ in ()).throw(AssertionError("card was touched"))
        try:
            result = sim.verify_pin("123456789", 0)
        finally:
            sim.readers = old
        self.assertFalse(result["ok"])
        self.assertIn("invalid PIN", result["error"])


if __name__ == "__main__":
    unittest.main()
