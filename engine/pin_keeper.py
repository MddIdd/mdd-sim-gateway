#!/usr/bin/env python3
"""
pin_keeper.py - Keep the USIM CHV1 (PIN) verified for the whole engine lifetime.

Why: swu_ike's EAP-AKA (IKE) and Asterisk's ims_aka (SIP) both run AUTHENTICATE against
the USIM, which requires CHV1 verified. Empirically, PIN verification persists at the card
level across separate PC/SC connections as long as the card stays powered. So we verify
once, then hold an idle connection open and only re-verify when the card is re-inserted /
reset. We do NOT poll with SELECTs (that would race with swu_ike/ami_usim APDU sequences,
which pcscd does not serialize as groups).

Config via env (set by entrypoint from /config/instance.json):
  USIM_PIN        - the CHV1 PIN (digits). If empty/"none", PIN is assumed disabled.
  USIM_READER     - exact PC/SC reader name, "imsi:<IMSI>", "iccid:<ICCID>", or
                    integer reader index. Default 0.
  MDD_RUNDIR   - status dir (default /run/mdd-sim-gateway)

Writes JSON status to $MDD_RUNDIR/pin_status.json:
  {"state": "...", "tries_left": N, "reader": "...", "ts": ...}
States: NO_READER, NO_CARD, PIN_DISABLED, VERIFIED, WRONG_PIN, PIN_BLOCKED, ERROR
Exit code is always 0 while running; it loops forever. A non-recoverable PIN problem
(WRONG_PIN / PIN_BLOCKED) is reported in status and the process keeps running so the
manager can surface it (it will retry on next card insert).
"""
import json
import os
import sys
import threading
import time

from smartcard.System import readers
from smartcard.util import toBytes, toHexString
from smartcard.Exceptions import NoCardException, CardConnectionException
from smartcard.scard import SCardBeginTransaction, SCardEndTransaction, SCARD_LEAVE_CARD


def _hcard(conn):
    obj = conn
    for _ in range(5):
        if hasattr(obj, "hcard"):
            return obj.hcard
        if hasattr(obj, "component") and obj.component is not None:
            obj = obj.component
            continue
        break
    return None


class _Tx:
    """Best-effort PC/SC transaction: exclusive card access for a short APDU sequence."""
    def __init__(self, conn):
        self.conn = conn
        self.hcard = None

    def __enter__(self):
        self.hcard = _hcard(self.conn)
        if self.hcard is not None:
            try:
                SCardBeginTransaction(self.hcard)
            except Exception:
                self.hcard = None
        return self.conn

    def __exit__(self, *a):
        if self.hcard is not None:
            try:
                SCardEndTransaction(self.hcard, SCARD_LEAVE_CARD)
            except Exception:
                pass

RUNDIR = os.environ.get("MDD_RUNDIR", "/run/mdd-sim-gateway")
STATUS_PATH = os.path.join(RUNDIR, "pin_status.json")
MIN_TRIES = 2  # never spend the PIN when only this many attempts remain (avoid PUK lock)


def log(msg):
    print(f"[pin_keeper] {msg}", flush=True)


def write_status(state, tries_left=None, reader=None, detail=None, iccid=None):
    # `iccid` is what the card in `reader` actually said it was. The manager needs it to tell a
    # reader name that merely drifted (USB-port binding legitimately opens a renamed slot) from
    # one that is pointing at the wrong card — the two are indistinguishable from the name alone.
    os.makedirs(RUNDIR, exist_ok=True)
    data = {"state": state, "tries_left": tries_left, "reader": reader,
            "iccid": iccid, "detail": detail, "ts": int(time.time())}
    tmp = STATUS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, STATUS_PATH)
    log(f"status={state} tries_left={tries_left} detail={detail}")


ICCID_BYTES = 10
ICCID_MIN_DIGITS = 15


def swap_nibbles(s):
    return "".join([x + y for x, y in zip(s[1::2], s[0::2])])


def _apdu_with_le(apdu, le):
    """Rebuild an APDU carrying the Le the card asked for in a 6Cxx status. Case 2 replaces
    the trailing Le byte; case 3/4 keeps Lc and the command data and re-stamps only Le.
    Truncating to CLA/INS/P1/P2 (what the first version did) turned a case-4 SELECT into a
    malformed command, so the retry failed and a healthy file read as unselectable."""
    head = list(apdu[:4])
    if len(apdu) <= 5:
        return head + [le]
    lc = apdu[4]
    return head + [lc] + list(apdu[5:5 + lc]) + [le]


def _xfr(conn, apdu):
    """Transmit one APDU, normalizing reader/protocol variance (issue #51). TPDU-level
    T=0 readers answer case-4 commands with 61xx and expect an explicit GET RESPONSE;
    APDU-level readers and T=1 hand back the data with 9000 directly. 6Cxx means
    "wrong Le, retry with mine". Callers see one shape: (data, 0x90, 0x00) on success.

    61xx already means the card ACCEPTED the command -- the GET RESPONSE that follows only
    fetches the body. Reporting that fetch's status as the command's status made a good
    SELECT ADF.USIM read as a card fault on cards whose GET RESPONSE answers anything but
    9000, which is issue #60: read_card bailed out with no PIN state at all and the start
    preflight then asked for a PIN the card never wanted. Callers that need the body check
    the body they got, so answering "accepted, here is what we could fetch" is safe."""
    data, s1, s2 = conn.transmit(apdu)
    data = list(data)
    accepted = False
    for _ in range(8):      # bound: a card that keeps re-asking cannot spin us forever
        if s1 == 0x61:
            accepted = True
            more, s1, s2 = conn.transmit([0x00, 0xC0, 0x00, 0x00, s2])
            data += list(more)
            continue
        if s1 == 0x6C and s2 and not accepted:
            data, s1, s2 = conn.transmit(_apdu_with_le(apdu, s2))
            data = list(data)
            continue
        break
    if accepted and s1 != 0x90:
        return data, 0x90, 0x00
    return data, s1, s2


def dec_imsi(ef_hex):
    """Decode EF_IMSI; None for anything that is not a plausible IMSI, so a garbled
    read surfaces as a failure instead of a bogus identity."""
    if len(ef_hex) < 4:
        return None
    try:
        length = int(ef_hex[0:2], 16)
    except ValueError:
        return None
    if not (1 <= length <= 8):
        return None
    swapped = swap_nibbles(ef_hex[2:]).rstrip("f")
    imsi = swapped[1:length * 2] if swapped else ""
    if not (5 <= len(imsi) <= 15) or not imsi.isdigit():
        return None
    return imsi


# 3GPP USIM AID prefix. EF_DIR record 1 is NOT always the USIM (China Telecom cards
# list CSIM first), so scan the records and pick the USIM by AID.
USIM_AID_PREFIX = "A0000000871002"


def _usim_aid_from_dir(conn):
    """Scan EF_DIR records for the USIM AID; prefer 3GPP USIM, fall back to the first
    application. Returns (aid_len, aid_hex) or None."""
    fcp, s1, s2 = _xfr(conn, toBytes("00a40004022f0000"))  # SELECT EF.DIR
    if s1 != 0x90 or len(fcp) < 8:
        return None
    rec_len = fcp[7]
    first = None
    for rec in range(1, 11):
        d, s1, s2 = _xfr(conn, toBytes("00b2") + [rec, 0x04, rec_len])
        # record template: 61 <len> 4F <aidlen> <AID...> [50 <len> label]
        if s1 != 0x90 or len(d) < 5 or d[0] != 0x61 or d[2] != 0x4F:
            break
        aid_len = d[3]
        aid = "".join("%02X" % b for b in d[4:4 + aid_len])
        if len(aid) < aid_len * 2:
            break
        if aid.startswith(USIM_AID_PREFIX):
            return aid_len, aid
        if first is None:
            first = (aid_len, aid)
    return first


def select_adf_usim(conn):
    """SELECT MF -> EF.DIR -> USIM AID -> ADF.USIM. Returns True on success."""
    conn.transmit(toBytes("00a40004023f0000"))          # SELECT MF
    got = _usim_aid_from_dir(conn)
    if not got:
        return False
    aid_len, aid = got
    d, s1, s2 = _xfr(conn, toBytes("00a40404") + [aid_len] + toBytes(aid))
    return s1 == 0x90


def read_imsi(conn):
    _xfr(conn, toBytes("00a40004026f0700"))
    d, s1, s2 = _xfr(conn, toBytes("00b0000009"))
    if s1 != 0x90:
        return None
    return dec_imsi(bytes(d).hex())


def pin_tries_left(conn):
    """VERIFY with empty body -> 63Cx returns remaining tries without spending one."""
    d, s1, s2 = conn.transmit(toBytes("0020000100"))
    if s1 == 0x63:
        return s2 & 0x0F
    if (s1, s2) == (0x90, 0x00):
        return None  # PIN already verified / not required in this state
    if (s1, s2) == (0x69, 0x83):
        return 0     # blocked
    return None


def verify_pin(conn, pin):
    if not (4 <= len(pin) <= 8) or not pin.isdigit():
        return 0x67, 0x00  # refuse to send a malformed VERIFY body (would spend a try)
    body = [ord(c) for c in pin] + [0xFF] * (8 - len(pin))
    d, s1, s2 = conn.transmit(toBytes("00200001") + [0x08] + body)
    return s1, s2


# EF.ICCID is read with pcsc-lite's blocking transmit(), which takes no timeout. On a VPCD
# logical channel that read can HANG rather than fail: the bridge multiplexes several channels
# onto one physical SIM, so a channel it has not wired up leaves transmit() parked forever and
# the caller never returns. Every ICCID check here is written to let an unreadable card through
# -- a card that will not answer is a fault, not proof of a swap -- but that rule can only run
# if the read comes back at all. Bound it, and surface the deadline as the exception those
# handlers already treat as "could not read", so the line proceeds exactly as intended.
#
# The check itself stays in force on every reader, VPCD included: two serial-less modems that
# swap USB paths is precisely the mix-up it exists to catch.
# Asterisk allows the whole AKA exchange SIM_TIMEOUT = 3s, so nothing on a card path may
# block longer than that; this probe runs once at startup rather than per authentication.
ICCID_READ_TIMEOUT = 2.0


class CardReadTimeout(Exception):
    """EF.ICCID did not answer within the deadline."""


def _with_deadline(fn, timeout=None):
    """Run a blocking card read on a throwaway thread and give up after `timeout` seconds.

    The worker is a daemon: when the card never answers it stays parked inside pcsc-lite
    instead of holding up shutdown. A timed-out read is reported as unreadable, and callers
    must not reuse its result -- there is none.
    """
    # Resolved per call, not bound as a default, so the module constant stays adjustable at
    # runtime (and patchable in tests) instead of being frozen when this function is defined.
    timeout = ICCID_READ_TIMEOUT if timeout is None else timeout
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa - handed back to the calling thread below
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise CardReadTimeout("EF.ICCID read did not answer within %gs" % timeout)
    if "error" in box:
        raise box["error"]
    return box.get("value")


def read_iccid(conn):
    """Read EF.ICCID (no PIN required). None when the card will not answer readably.

    EF.ICCID is exactly 10 BCD bytes (TS 31.102). A short read, or a value that is not
    all digits, is a card or reader fault rather than an identity -- and every caller
    convicts a reader on ANY non-empty ICCID that differs from the line's, so handing one
    a truncated value would strand a line whose binding is perfectly correct. Returning
    None keeps the documented fail-open direction: we simply cannot convict.
    """
    conn.transmit(toBytes("00a40004023f0000"))
    _xfr(conn, toBytes("00a40004022fe200"))
    d, s1, s2 = _xfr(conn, toBytes("00b000000a"))
    if s1 != 0x90 or len(d) != ICCID_BYTES:
        return None
    iccid = swap_nibbles(bytes(d).hex()).rstrip("f")
    return iccid if iccid.isdigit() and len(iccid) >= ICCID_MIN_DIGITS else None


class WrongCard(Exception):
    """The reader this line is bound to demonstrably holds a different line's SIM."""

    def __init__(self, reader, expected, actual):
        super().__init__(f"{reader} holds ICCID {actual}, line expects {expected}")
        self.reader = reader
        self.expected = expected
        self.actual = actual


def _accept(reader, conn, expected):
    """Return (reader, conn, iccid) once the card in it is proven to belong to this line.

    The third element is the ICCID this function actually read, or None when it did not look
    or the card would not answer. Reporting it lets the caller record which CARD replied
    without opening the card a second time to ask the same question.

    Binding a line by reader NAME, USB PORT or INDEX says which slot to open; it says nothing
    about which SIM is sitting in that slot. When two identical serial-less modems swap USB
    paths -- or the engine image predates a binding fix and silently falls back to index 0 --
    the wrong slot opens and the only symptom is the carrier's AKA challenge failing with
    SW=9862, which reads as a carrier fault and sends the operator hunting upstream. EF.ICCID
    needs no PIN, so the card can identify itself before anything else touches it.

    Only an ICCID we actually READ can convict a reader. An unreadable EF.ICCID is a transient
    card or reader fault, not evidence of a swap; failing closed on it would strand a line
    whose binding is perfectly correct.
    """
    if not expected:
        return reader, conn, None
    try:
        actual = _with_deadline(lambda: read_iccid(conn))
    except Exception:  # noqa - unreadable EF.ICCID is not evidence; let the line proceed
        return reader, conn, None
    if not actual or actual == expected:
        return reader, conn, actual
    try:
        conn.disconnect()
    except Exception:
        pass
    raise WrongCard(str(reader), expected, actual)


# --- Reader binding by physical USB port ----------------------------------------------------
# Resolve a reader by its STABLE physical USB port path (e.g. "3-2") instead of the pcscd
# enumeration index, which can flip between two identical (serial-less) readers on re-enumeration.
# Mirrors control/app/usbreader.py and swu_ike.resolve_reader_index_by_port so pin_keeper, swu_ike
# and the manager all agree on which physical reader a line is bound to. Best-effort: returns None
# when the port can't be resolved so callers fall back to the ICCID/index strategy.
try:
    from smartcard.scard import (
        SCardEstablishContext as _SCEstablish, SCardReleaseContext as _SCRelease,
        SCardConnect as _SCConnect,
        SCardGetAttrib as _SCGetAttrib, SCardDisconnect as _SCDisconnect,
        SCARD_SCOPE_USER as _SC_SCOPE_USER, SCARD_SHARE_DIRECT as _SC_SHARE_DIRECT,
        SCARD_LEAVE_CARD as _SC_LEAVE, SCARD_PROTOCOL_T0 as _SC_T0,
        SCARD_PROTOCOL_T1 as _SC_T1, SCARD_ATTR_CHANNEL_ID as _SC_CHANNEL_ID,
        SCARD_S_SUCCESS as _SC_OK,
    )
    _SC_PORT_OK = True
except Exception:                        # pragma: no cover
    _SC_PORT_OK = False


def _reader_bus_dev(reader_name):
    if not _SC_PORT_OK:
        return None
    hctx = hcard = None
    try:
        hr, hctx = _SCEstablish(_SC_SCOPE_USER)
        if hr != _SC_OK:
            return None
        hr, hcard, _p = _SCConnect(hctx, reader_name, _SC_SHARE_DIRECT, _SC_T0 | _SC_T1)
        if hr != _SC_OK:
            return None
        hr, val = _SCGetAttrib(hcard, _SC_CHANNEL_ID)
        if hr != _SC_OK or not val or len(val) < 4:
            return None
        v = val[0] | (val[1] << 8) | (val[2] << 16) | (val[3] << 24)
        if (v >> 16) != 0x0020:
            return None
        return (v >> 8) & 0xff, v & 0xff
    except Exception:
        return None
    finally:
        if hcard is not None:
            try:
                _SCDisconnect(hcard, _SC_LEAVE)
            except Exception:
                pass
        if hctx is not None:
            try:
                _SCRelease(hctx)
            except Exception:
                pass


def _usb_port_path(bus, devnum):
    import glob as _glob
    try:
        entries = _glob.glob("/sys/bus/usb/devices/*/")
    except Exception:
        return None
    for d in entries:
        try:
            with open(d + "busnum") as f:
                b = int(f.read())
            with open(d + "devnum") as f:
                n = int(f.read())
        except Exception:
            continue
        if b == bus and n == devnum:
            return os.path.basename(d.rstrip("/"))
    return None


def index_for_port(port):
    """Live reader index whose physical USB port == `port`, or None."""
    if not port:
        return None
    try:
        rlist = readers()
    except Exception:
        return None
    for i, r in enumerate(rlist):
        bd = _reader_bus_dev(str(r))
        if bd and _usb_port_path(bd[0], bd[1]) == port:
            return i
    return None


def find_reader(reader_spec):
    """Return (reader, open_connection, iccid) for the target SIM.

    ``iccid`` is the card that actually answered, when this search read it — None when no read
    happened or the card would not identify itself. It is reported here because this is where
    the card is already open and, on most paths, already asked; the caller records it so a
    reader name that merely drifted can be told apart from one holding the wrong card.

    Matching strategy that works with multiple readers (some empty) WITHOUT needing the
    PIN first:
      - USIM_READER_PORT set -> resolve the reader at that STABLE physical USB port first
                        (survives pcscd flipping two identical readers' indices).
      - imsi:<IMSI>  -> single reader: use it. Multiple readers: read ICCID (no PIN needed)
                        on each present card; if the target's ICCID was learned (see
                        USIM_ICCID env) match on it, otherwise fall back to trying IMSI
                        (works only once PIN is already satisfied) and finally the first
                        readable card.
      - iccid:<ICCID> -> match by ICCID (always readable, no PIN).
      - <reader name> -> exact PC/SC reader-name match.
      - <index>      -> that reader index.
    """
    rlist = readers()
    if not rlist:
        return None, None, None

    def _open(r):
        try:
            c = r.createConnection()
            c.connect()
            return c
        except Exception:
            return None

    target_iccid = os.environ.get("USIM_ICCID", "").strip()

    # 0) Highest priority: the stable USB-port binding. If it resolves to a present, openable
    # reader, use it — this is the physical reader the line is bound to regardless of index order.
    port = os.environ.get("USIM_READER_PORT", "").strip()
    if port:
        pidx = index_for_port(port)
        if pidx is not None and pidx < len(rlist):
            conn = _open(rlist[pidx])
            if conn is not None:
                log(f"bound to USB port {port} (reader index {pidx})")
                return _accept(rlist[pidx], conn, target_iccid)

    if isinstance(reader_spec, str) and reader_spec.startswith("iccid:"):
        want = reader_spec[6:]
        for r in rlist:
            conn = _open(r)
            if conn is None:
                continue
            if read_iccid(conn) == want:
                return r, conn, want
            try: conn.disconnect()
            except Exception: pass
        return None, None, None

    if isinstance(reader_spec, str) and reader_spec.startswith("imsi:"):
        target = reader_spec[5:]
        if len(rlist) == 1:
            conn = _open(rlist[0])
            return (rlist[0], conn, None) if conn else (None, None, None)
        # Multiple readers: only consider readers that actually have a card (open succeeds),
        # then match by ICCID (no PIN), then by IMSI (if PIN already satisfied), then first.
        candidates = []
        for r in rlist:
            conn = _open(r)
            if conn is None:
                continue                      # empty reader -> skip
            candidates.append((r, conn))
        if not candidates:
            return None, None, None
        # 1) match by stored ICCID (always readable)
        identified = []
        # Keyed by reader name: what each candidate card said when asked, so a later match on
        # IMSI can still report the ICCID already learned here instead of asking twice.
        seen = {}
        if target_iccid:
            for r, conn in candidates:
                try:
                    actual = read_iccid(conn)
                except Exception:  # noqa - unreadable card; it simply cannot be matched here
                    actual = None
                if actual:
                    identified.append(actual)
                    seen[str(r)] = actual
                if actual == target_iccid:
                    _close_others(candidates, conn)
                    return r, conn, actual
        # 2) match by IMSI (only works if PIN already verified on the card)
        for r, conn in candidates:
            if select_adf_usim(conn) and read_imsi(conn) == target:
                _close_others(candidates, conn)
                return r, conn, seen.get(str(r))
        # 3) Every present card said who it was and none of them was this line's. Falling back
        # to the first one would run the carrier's AKA challenge against a stranger's SIM and
        # report it as SW=9862. Refuse instead, and name both sides so the mix-up is obvious.
        if target_iccid and len(identified) == len(candidates):
            _close_others(candidates, None)
            raise WrongCard(str(candidates[0][0]), target_iccid, ", ".join(identified))
        # 4) fall back to the first card-bearing reader
        r, conn = candidates[0]
        _close_others(candidates, conn)
        return r, conn, seen.get(str(r))

    # Modem-backed lines deliberately bind PIN, SWu and IMS to separate VPCD logical
    # channels.  Those bindings are persisted as full PC/SC reader names (for example
    # "VoWiFi Modem ... 00 00").  Falling through to int() used to turn every such name
    # into index 0, so the second modem opened the first modem's PIN slot and reported
    # NO_CARD forever.  Match the exact name before accepting a legacy numeric index.
    if isinstance(reader_spec, str):
        wanted = reader_spec.strip()
        for r in rlist:
            if str(r) == wanted:
                conn = _open(r)
                return _accept(r, conn, target_iccid) if conn else (None, None, None)

    try:
        idx = int(reader_spec)
    except (TypeError, ValueError):
        return None, None, None
    if idx < 0 or idx >= len(rlist):
        return None, None, None
    conn = _open(rlist[idx])
    return _accept(rlist[idx], conn, target_iccid) if conn else (None, None, None)


def _close_others(candidates, keep):
    for _, c in candidates:
        if c is not keep:
            try: c.disconnect()
            except Exception: pass


def ensure_pin(reader_spec, pin):
    """Connect, verify PIN if enabled, return an open connection to HOLD (keeps the card
    powered so PIN stays verified). All card I/O happens inside a transaction."""
    try:
        r, conn, card_iccid = find_reader(reader_spec)
    except WrongCard as wrong:
        # Naming both ICCIDs here is the whole point: SW=9862 alone sends people hunting the
        # carrier, while "this slot holds someone else's SIM" points straight at the binding.
        write_status("WRONG_CARD", reader=wrong.reader, iccid=wrong.actual,
                     detail=f"reader holds ICCID {wrong.actual}, this line is {wrong.expected}")
        log(f"refusing to use {wrong.reader}: {wrong}")
        return None
    if r is None:
        write_status("NO_CARD", reader=str(reader_spec))
        return None
    rname = str(r)

    # Record WHICH card answered, not just which slot was opened. A USB-port binding
    # deliberately opens the reader that physically holds the SIM even when its generated name
    # has changed, so a name that differs from the stored one is normal there; only the ICCID
    # separates that from a slot pointing at another line's card. find_reader reports what it
    # read while resolving, so this costs no extra APDU exchange — and, importantly, no card
    # I/O outside the transaction below.
    def status(state, **kw):
        write_status(state, iccid=card_iccid, **kw)

    try:
        with _Tx(conn):
            if not select_adf_usim(conn):
                status("NO_CARD", reader=rname, detail="ADF.USIM select failed")
                conn.disconnect()
                return None

            tries = pin_tries_left(conn)
            if not pin or pin.lower() in ("none", "disabled", ""):
                status("PIN_DISABLED", tries_left=tries, reader=rname)
                return conn
            if tries is None:
                # already verified in this card session (9000) -> nothing to do
                status("VERIFIED", tries_left=None, reader=rname)
                return conn
            if tries == 0:
                status("PIN_BLOCKED", tries_left=0, reader=rname)
                return conn
            if tries < MIN_TRIES:
                status("PIN_BLOCKED", tries_left=tries, reader=rname,
                             detail=f"refusing verify with only {tries} tries left (PUK risk)")
                return conn

            s1, s2 = verify_pin(conn, pin)
            if (s1, s2) == (0x90, 0x00):
                status("VERIFIED", tries_left=3, reader=rname)
                return conn
            if s1 == 0x63:
                status("WRONG_PIN", tries_left=s2 & 0x0F, reader=rname)
                return conn
            if (s1, s2) == (0x69, 0x83):
                status("PIN_BLOCKED", tries_left=0, reader=rname)
                return conn
            status("ERROR", reader=rname, detail=f"verify sw={s1:02x}{s2:02x}")
            return conn
    except Exception as e:  # noqa
        status("ERROR", reader=rname, detail=repr(e))
        try:
            conn.disconnect()
        except Exception:
            pass
        return None


def main():
    pin = os.environ.get("USIM_PIN", "")
    reader_spec = os.environ.get("USIM_READER", "0")
    log(f"starting; reader={reader_spec} pin={'set' if pin else 'none'}")

    # Verify PIN once, then HOLD the connection open indefinitely. An open handle keeps
    # the card powered, so CHV1 verification persists for swu_ike (IKE EAP-AKA) and
    # ami_usim (SIP IMS-AKA). We do NOT poll the card (polling races with their APDU
    # sequences); we only re-acquire if the held connection genuinely dies.
    conn = None
    idle_ticks = 0
    while True:
        try:
            if conn is None:
                conn = ensure_pin(reader_spec, pin)
                if conn is None:
                    time.sleep(3)
                    continue
                idle_ticks = 0
            time.sleep(5)
            idle_ticks += 1
            # Rare, cheap liveness probe via a SEPARATE short-lived shared connection so we
            # never disturb the held connection's selected file / PIN state. Every ~60s.
            if idle_ticks >= 12:
                idle_ticks = 0
                if not _card_present(reader_spec):
                    log("card removed; will re-verify on re-insert")
                    try:
                        conn.disconnect()
                    except Exception:
                        pass
                    conn = None
                    write_status("NO_CARD", reader=str(reader_spec))
        except KeyboardInterrupt:
            break
        except Exception as e:  # noqa
            log(f"exception: {e!r}")
            try:
                if conn:
                    conn.disconnect()
            except Exception:
                pass
            conn = None
            write_status("ERROR", reader=str(reader_spec), detail=repr(e))
            time.sleep(3)


def _card_present(reader_spec):
    """Presence check that does not touch the held connection: list readers only."""
    try:
        rlist = readers()
        return len(rlist) > 0
    except Exception:
        return False


if __name__ == "__main__":
    main()
