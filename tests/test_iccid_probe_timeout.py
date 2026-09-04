"""The EF.ICCID binding probe must never be able to hang a line.

pcsc-lite's transmit() takes no timeout. On a VPCD logical channel the probe can HANG rather
than fail, and the "an unreadable card is not evidence of a swap" rule the callers implement
only runs if the read comes back at all — so a line whose PIN channel never answers stalls
forever instead of proceeding.

The probe itself stays in force on EVERY reader, VPCD included: two serial-less modems that
swap USB paths is exactly the mix-up it exists to catch, so these also pin down that a card
which really is the wrong one is still refused.

swu_ike.py carries the same change but pulls in serial/requests at import time, so it is not
loadable here; it is covered by the shared-shape assertions at the bottom.
"""
import importlib.util
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

VPCD = "VoWiFi Modem 2c7c-0125-1-1.4.4 00 00"
VPCD_IMS = "VoWiFi Modem 2c7c-0125-1-1.4.4 00 02"
PHYSICAL = "Alcor Link AK9563 01 00"
OURS = "8900000000000000013"
THEIRS = "8900000000000000059"


def _iccid_apdu_bytes(iccid):
    padded = iccid if len(iccid) % 2 == 0 else iccid + "f"
    swapped = "".join(x + y for x, y in zip(padded[1::2], padded[0::2]))
    return list(bytes.fromhex(swapped))


class _Connection:
    """A card connection. `hangs` models the VPCD channel that never answers."""

    def __init__(self, name, iccid=None, hangs=False, short_read=None):
        self.name = name
        self.iccid = iccid
        self.hangs = hangs
        # Bytes to hand back for EF.ICCID instead of all 10 — a partial read, which decodes
        # into a shorter number that is NOT the card's identity.
        self.short_read = short_read
        self.disconnected = False
        self.transmits = 0
        self.released = threading.Event()

    def connect(self):
        pass

    def disconnect(self):
        self.disconnected = True

    def transmit(self, apdu):
        self.transmits += 1
        if self.hangs:
            # Park like a real blocking transmit() would; released so the test never leaks a
            # thread that outlives the run.
            self.released.wait(10)
            raise RuntimeError("unblocked")
        if self.iccid is None:
            raise RuntimeError("card does not answer")
        if str(apdu).lower().startswith("00b0"):
            body = _iccid_apdu_bytes(self.iccid)
            if self.short_read is not None:
                body = body[:self.short_read]
            return body, 0x90, 0x00
        return [], 0x90, 0x00


class _Reader:
    def __init__(self, name, iccid=None, hangs=False):
        self.name = name
        self.iccid = iccid
        self.hangs = hangs

    def __str__(self):
        return self.name

    def createConnection(self):
        return _Connection(self.name, self.iccid, self.hangs)


def _load(filename, module_name):
    smartcard = types.ModuleType("smartcard")
    system = types.ModuleType("smartcard.System")
    system.readers = lambda: []
    util = types.ModuleType("smartcard.util")
    util.toBytes = lambda value: value
    util.toHexString = lambda value: ""
    exceptions = types.ModuleType("smartcard.Exceptions")
    exceptions.NoCardException = type("NoCardException", (Exception,), {})
    exceptions.CardConnectionException = type("CardConnectionException", (Exception,), {})
    scard = types.ModuleType("smartcard.scard")
    scard.SCardBeginTransaction = lambda *_: None
    scard.SCardEndTransaction = lambda *_: None
    scard.SCARD_LEAVE_CARD = 0
    panoramisk = types.ModuleType("panoramisk")
    panoramisk.Manager = object
    mods = {"smartcard": smartcard, "smartcard.System": system, "smartcard.util": util,
            "smartcard.Exceptions": exceptions, "smartcard.scard": scard,
            "panoramisk": panoramisk}
    spec = importlib.util.spec_from_file_location(module_name, ROOT / "engine" / filename)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, mods):
        spec.loader.exec_module(module)
    return module


class DeadlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load("pin_keeper.py", "timeout_pin_keeper")

    def test_returns_the_value_when_the_read_completes(self):
        self.assertEqual(self.mod._with_deadline(lambda: "8944", timeout=5), "8944")

    def test_reraises_the_read_error_on_the_calling_thread(self):
        def boom():
            raise ValueError("card refused")
        with self.assertRaises(ValueError):
            self.mod._with_deadline(boom, timeout=5)

    def test_a_read_that_never_returns_raises_instead_of_hanging(self):
        started = time.monotonic()
        with self.assertRaises(self.mod.CardReadTimeout):
            self.mod._with_deadline(lambda: time.sleep(30), timeout=0.2)
        self.assertLess(time.monotonic() - started, 5, "the deadline did not cut the read off")

    def test_timeout_falls_back_to_the_module_constant(self):
        with patch.object(self.mod, "ICCID_READ_TIMEOUT", 0.2):
            with self.assertRaises(self.mod.CardReadTimeout):
                self.mod._with_deadline(lambda: time.sleep(30))


class PinKeeperProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load("pin_keeper.py", "probe_pin_keeper")

    def test_a_hanging_vpcd_channel_lets_the_line_proceed(self):
        """The line-1 failure: the PIN channel accepts the APDU and never answers."""
        conn = _Connection(VPCD, hangs=True)
        self.addCleanup(conn.released.set)
        with patch.object(self.mod, "ICCID_READ_TIMEOUT", 0.2):
            started = time.monotonic()
            reader, got, iccid = self.mod._accept(_Reader(VPCD), conn, OURS)
        self.assertLess(time.monotonic() - started, 5, "the probe hung the line")
        self.assertIs(got, conn)
        self.assertIsNone(iccid)

    def test_a_vpcd_channel_holding_another_lines_sim_is_still_refused(self):
        """Two serial-less modems swapping USB paths — the case the probe exists for."""
        conn = _Connection(VPCD, iccid=THEIRS)
        with self.assertRaises(self.mod.WrongCard) as caught:
            self.mod._accept(_Reader(VPCD), conn, OURS)
        self.assertEqual(caught.exception.actual, THEIRS)

    def test_a_hanging_physical_read_lets_the_line_proceed(self):
        conn = _Connection(PHYSICAL, hangs=True)
        self.addCleanup(conn.released.set)
        with patch.object(self.mod, "ICCID_READ_TIMEOUT", 0.2):
            started = time.monotonic()
            reader, got, iccid = self.mod._accept(_Reader(PHYSICAL), conn, OURS)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIs(got, conn, "a card that would not answer is not evidence of a swap")
        self.assertIsNone(iccid)
        self.assertFalse(conn.disconnected)

    def test_the_matching_card_is_still_accepted(self):
        conn = _Connection(PHYSICAL, iccid=OURS)
        reader, got, iccid = self.mod._accept(_Reader(PHYSICAL), conn, OURS)
        self.assertIs(got, conn)
        self.assertEqual(iccid, OURS)

    def test_a_genuine_wrong_card_is_still_refused(self):
        conn = _Connection(PHYSICAL, iccid=THEIRS)
        with self.assertRaises(self.mod.WrongCard) as caught:
            self.mod._accept(_Reader(PHYSICAL), conn, OURS)
        self.assertEqual(caught.exception.actual, THEIRS)
        self.assertEqual(caught.exception.expected, OURS)
        self.assertTrue(conn.disconnected)

    def test_a_partial_iccid_read_does_not_convict_the_reader(self):
        """A short EF.ICCID decodes into a number that is not the card's identity. Convicting
        on it would strand a correctly bound line, which is exactly what _accept documents it
        must not do: only an ICCID we actually READ can convict."""
        conn = _Connection(PHYSICAL, iccid=OURS, short_read=6)
        reader, got, iccid = self.mod._accept(_Reader(PHYSICAL), conn, OURS)
        self.assertIs(got, conn)
        self.assertIsNone(iccid, "a truncated read must not pass itself off as an identity")
        self.assertFalse(conn.disconnected)

    def test_read_iccid_rejects_a_short_or_garbled_answer(self):
        self.assertEqual(self.mod.read_iccid(_Connection(PHYSICAL, iccid=OURS)), OURS)
        self.assertIsNone(self.mod.read_iccid(_Connection(PHYSICAL, iccid=OURS, short_read=9)))
        self.assertIsNone(self.mod.read_iccid(_Connection(PHYSICAL, iccid=OURS, short_read=0)))


class AmiUsimProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load("ami_usim.py", "probe_ami_usim")

    def _env(self, iccid=OURS):
        return patch.dict(self.mod.os.environ, {"USIM_ICCID": iccid})

    def test_a_hanging_vpcd_channel_is_not_treated_as_a_swap(self):
        conn = _Connection(VPCD_IMS, hangs=True)
        self.addCleanup(conn.released.set)
        with self._env(), patch.object(self.mod, "ICCID_READ_TIMEOUT", 0.2):
            started = time.monotonic()
            self.assertIsNone(self.mod.foreign_iccid(conn))
        self.assertLess(time.monotonic() - started, 5)

    def test_a_vpcd_channel_holding_another_lines_sim_is_still_reported(self):
        conn = _Connection(VPCD_IMS, iccid=THEIRS)
        with self._env():
            self.assertEqual(self.mod.foreign_iccid(conn), THEIRS)

    def test_a_hanging_physical_read_is_not_treated_as_a_swap(self):
        conn = _Connection(PHYSICAL, hangs=True)
        self.addCleanup(conn.released.set)
        with self._env(), patch.object(self.mod, "ICCID_READ_TIMEOUT", 0.2):
            started = time.monotonic()
            self.assertIsNone(self.mod.foreign_iccid(conn))
        self.assertLess(time.monotonic() - started, 5)

    def test_the_line_s_own_card_is_not_flagged(self):
        conn = _Connection(PHYSICAL, iccid=OURS)
        with self._env():
            self.assertIsNone(self.mod.foreign_iccid(conn))

    def test_a_genuine_wrong_card_is_still_reported(self):
        conn = _Connection(PHYSICAL, iccid=THEIRS)
        with self._env():
            self.assertEqual(self.mod.foreign_iccid(conn), THEIRS)


class StartupProbeTests(unittest.TestCase):
    """The binding probe must be settled before Asterisk can ask for an AKA response.

    Asterisk allows the whole exchange SIM_TIMEOUT = 3s. Probing the card inside that window
    is what turned a slow (or silent) EF.ICCID read into a failed registration: the verdict
    arrived after Asterisk had already given up.
    """

    def setUp(self):
        self.mod = _load("ami_usim.py", "startup_ami_usim")
        self.mod._foreign_verdict = None
        self.mod._foreign_decided = False

    def _with_card(self, iccid, hangs=False):
        conn = _Connection(PHYSICAL, iccid=iccid, hangs=hangs)
        if hangs:
            self.addCleanup(conn.released.set)
        return conn, patch.object(self.mod, "open_usim", return_value=conn)

    def test_the_probe_runs_once_and_is_cached(self):
        conn, opener = self._with_card(OURS)
        with patch.dict(self.mod.os.environ, {"USIM_ICCID": OURS}), opener as opened:
            self.assertIsNone(self.mod.probe_foreign_card_once(PHYSICAL))
            self.assertIsNone(self.mod.probe_foreign_card_once(PHYSICAL))
            self.assertIsNone(self.mod.probe_foreign_card_once(PHYSICAL))
        self.assertEqual(opened.call_count, 1, "the card must be opened once, not per call")

    def test_a_foreign_card_is_remembered(self):
        conn, opener = self._with_card(THEIRS)
        with patch.dict(self.mod.os.environ, {"USIM_ICCID": OURS}), opener:
            self.assertEqual(self.mod.probe_foreign_card_once(PHYSICAL), THEIRS)
        self.assertEqual(self.mod._foreign_verdict, THEIRS)

    def test_the_probe_releases_the_card_for_the_aka_path(self):
        conn, opener = self._with_card(OURS)
        with patch.dict(self.mod.os.environ, {"USIM_ICCID": OURS}), opener:
            self.mod.probe_foreign_card_once(PHYSICAL)
        self.assertTrue(conn.disconnected, "the startup probe must not hold the card open")

    def test_a_hanging_probe_is_bounded_and_does_not_convict(self):
        conn, opener = self._with_card(None, hangs=True)
        with patch.dict(self.mod.os.environ, {"USIM_ICCID": OURS}), opener, \
                patch.object(self.mod, "ICCID_READ_TIMEOUT", 0.2):
            started = time.monotonic()
            self.assertIsNone(self.mod.probe_foreign_card_once(PHYSICAL))
        self.assertLess(time.monotonic() - started, 3, "a startup probe must not stall the line")

    def test_no_card_at_startup_leaves_the_verdict_open(self):
        with patch.object(self.mod, "open_usim", return_value=None):
            self.assertIsNone(self.mod.probe_foreign_card_once(PHYSICAL))

    def test_the_aka_path_performs_no_card_read_of_its_own(self):
        """read_res_ck_ik must consume the verdict, never re-probe."""
        src = (ROOT / "engine" / "ami_usim.py").read_bytes().decode()
        body = src[src.index("def read_res_ck_ik("):]
        body = body[:body.index("\ndef ")]
        self.assertNotIn("foreign_iccid(", body,
                         "the AKA path probes the card again inside Asterisk's 3s window")
        self.assertIn("_foreign_verdict", body)


class SharedShapeTests(unittest.TestCase):
    """swu_ike.py cannot be imported here, so assert it carries the same bounded read."""

    def test_every_engine_component_bounds_its_iccid_read(self):
        for name in ("pin_keeper.py", "ami_usim.py", "swu_ike.py"):
            src = (ROOT / "engine" / name).read_bytes().decode()
            self.assertIn("_with_deadline(lambda:", src, f"{name} still reads without a deadline")
            self.assertIn("class CardReadTimeout", src, name)


if __name__ == "__main__":
    unittest.main()
