#!/usr/bin/env python3
"""
ami_usim.py - Bridge Asterisk (ims_aka) SIP authentication to the physical USIM via PC/SC.

Derived from phcoder/asterisk-docker (jolly). Changes:
  - VERIFY CHV1 (PIN) after selecting ADF.USIM, before AUTHENTICATE (reference didn't).
  - Clean type-annotation bug from the original (undefined Hexstr/Optional names).
  - Emit status/heartbeat JSON to $MDD_RUNDIR/usim_status.json for the manager FSM.

On Asterisk 'AuthRequest' it runs USIM AUTHENTICATE and returns RES/CK/IK (or AUTS on
sync failure). Triggers registration on FullyBooted and confirms dedicated bearers.
"""
import asyncio
import configparser
import json
import os
import sys
import threading
import time

from panoramisk import Manager
from smartcard.System import readers
from smartcard.util import toHexString, toBytes
from smartcard.scard import SCardBeginTransaction, SCardEndTransaction, SCARD_LEAVE_CARD

RUNDIR = os.environ.get("MDD_RUNDIR", "/run/mdd-sim-gateway")
USIM_PIN = os.environ.get("USIM_PIN", "")


# --- Reader binding by physical USB port -----------------------------------------------------
# Resolve a reader by its STABLE physical USB port path (USIM_READER_PORT, e.g. "3-2") so the SIP
# IMS-AKA path addresses the same physical reader as swu_ike/pin_keeper — even when pcscd flips
# two identical (serial-less) readers' enumeration order. Mirrors control/app/usbreader.py.
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
    """Best-effort PC/SC transaction: exclusive card access for the auth sequence, so it
    cannot interleave with pin_keeper / swu_ike APDUs on the shared card."""
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


def write_status(**kw):
    os.makedirs(RUNDIR, exist_ok=True)
    kw["ts"] = int(time.time())
    tmp = os.path.join(RUNDIR, "usim_status.json.tmp")
    with open(tmp, "w") as f:
        json.dump(kw, f)
    os.replace(tmp, os.path.join(RUNDIR, "usim_status.json"))


ICCID_BYTES = 10
ICCID_MIN_DIGITS = 15


def swap_nibbles(s):
    return "".join([x + y for x, y in zip(s[1::2], s[0::2])])


def dec_imsi(ef):
    """Decode EF_IMSI; None for anything that is not a plausible IMSI, so a garbled
    read surfaces as a failure instead of a bogus identity."""
    if len(ef) < 4:
        return None
    try:
        length = int(ef[0:2], 16)
    except ValueError:
        return None
    if not (1 <= length <= 8):
        return None
    swapped = swap_nibbles(ef[2:]).rstrip("f")
    imsi = swapped[1:length * 2] if swapped else ""
    if not (5 <= len(imsi) <= 15) or not imsi.isdigit():
        return None
    return imsi


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


def _xfr(connection, apdu):
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
    data, sw1, sw2 = connection.transmit(apdu)
    data = list(data)
    accepted = False
    for _ in range(8):      # bound: a card that keeps re-asking cannot spin us forever
        if sw1 == 0x61:
            accepted = True
            more, sw1, sw2 = connection.transmit([0x00, 0xC0, 0x00, 0x00, sw2])
            data += list(more)
            continue
        if sw1 == 0x6C and sw2 and not accepted:
            data, sw1, sw2 = connection.transmit(_apdu_with_le(apdu, sw2))
            data = list(data)
            continue
        break
    if accepted and sw1 != 0x90:
        return data, 0x90, 0x00
    return data, sw1, sw2


# 3GPP USIM AID prefix. EF_DIR record 1 is NOT always the USIM (China Telecom cards
# list CSIM first), so scan the records and pick the USIM by AID.
USIM_AID_PREFIX = "A0000000871002"


def _usim_aid_from_dir(connection):
    """Scan EF_DIR records for the USIM AID; prefer 3GPP USIM, fall back to the first
    application. EF.DIR must be selectable from the current DF. Returns (len, hex) or None."""
    fcp, sw1, sw2 = _xfr(connection, toBytes("00a40004022f0000"))  # SELECT EF.DIR
    if sw1 != 0x90 or len(fcp) < 8:
        return None
    record_length = fcp[7]
    first = None
    for rec in range(1, 11):
        data, sw1, sw2 = _xfr(connection, toBytes("00b2") + [rec, 0x04, record_length])
        if sw1 != 0x90 or len(data) < 5 or data[0] != 0x61 or data[2] != 0x4F:
            break
        aid_length = data[3]
        aid = "".join("%02X" % b for b in data[4:4 + aid_length])
        if len(aid) < aid_length * 2:
            break
        if aid.startswith(USIM_AID_PREFIX):
            return aid_length, aid
        if first is None:
            first = (aid_length, aid)
    return first


def make_connection_index(reader_index):
    r = readers()
    if reader_index >= len(r):
        return None
    connection = r[reader_index].createConnection()
    connection.connect()
    connection.transmit(toBytes("00a40004023f0000"))
    got = _usim_aid_from_dir(connection)
    if got is None:
        print("Failed to find USIM AID in EF.DIR")
        return None
    aid_length, aid = got
    print(f"Using aid={aid}")
    data, sw1, sw2 = _xfr(connection, toBytes("00a40404") + [aid_length] + toBytes(aid))
    if sw1 != 0x90:
        print("Failed to select AID")
        return None
    return connection


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
# Asterisk allows the whole AKA exchange SIM_TIMEOUT = 3s (res_pjsip_outbound_registration.c),
# so any card read on that path must finish well inside it — see probe_foreign_card_once(),
# which is why this budget is only ever spent at startup.
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


def read_iccid(connection):
    """Read EF.ICCID (no PIN required). None when the card will not answer readably.

    EF.ICCID is exactly 10 BCD bytes (TS 31.102). A short read, or a value that is not
    all digits, is a card or reader fault rather than an identity -- and every caller
    convicts a reader on ANY non-empty ICCID that differs from the line's, so handing one
    a truncated value would strand a line whose binding is perfectly correct. Returning
    None keeps the documented fail-open direction: we simply cannot convict.
    """
    connection.transmit(toBytes("00a40004023f0000"))
    connection.transmit(toBytes("00a40004022fe200"))
    data, sw1, sw2 = connection.transmit(toBytes("00b000000a"))
    if sw1 != 0x90 or len(data) != ICCID_BYTES:
        return None
    iccid = swap_nibbles(bytes(data).hex()).rstrip("f")
    return iccid if iccid.isdigit() and len(iccid) >= ICCID_MIN_DIGITS else None


def foreign_iccid(connection):
    """The ICCID in this connection when it provably is not this line's card, else None.

    A reader name, USB port or index only names a slot; it says nothing about which SIM is
    sitting in it. IMS-AKA run against the wrong card fails with SW=9862 -- byte for byte what
    a carrier returns when it genuinely rejects a subscriber -- so nothing downstream can tell
    the two apart. EF.ICCID needs no PIN, so one read here settles it.

    Only an ICCID actually read convicts a reader. An unreadable EF.ICCID is a transient card
    fault, not evidence of a swap, and failing closed on it would strand a correctly bound line.
    """
    expected = os.environ.get("USIM_ICCID", "").strip()
    if connection is None or not expected:
        return None
    try:
        actual = _with_deadline(lambda: read_iccid(connection))
    except Exception:  # noqa - unreadable EF.ICCID is not evidence of a swap
        return None
    return actual if actual and actual != expected else None


# Whether the bound slot holds another line's SIM is decided ONCE, at startup, and reused.
#
# It used to be re-decided on every AKA challenge, inside the 3s Asterisk allows for the whole
# exchange. A card that answers slowly -- or, on a VPCD channel, not at all -- then turned a
# diagnostic read into a failed registration: Asterisk gave up before AuthResponse was sent.
# A slot cannot begin holding a different SIM midway through a registration; a swap needs a
# re-enumeration, which restarts this process anyway. So decide before serving traffic, and
# keep the AKA path free of card reads it does not need.
_foreign_verdict = None
_foreign_decided = False


def probe_foreign_card_once(reader_spec):
    """Read EF.ICCID once and remember whether this slot holds a foreign card."""
    global _foreign_verdict, _foreign_decided
    if _foreign_decided:
        return _foreign_verdict
    _foreign_decided = True
    connection = open_usim(reader_spec)
    if connection is None:
        return _foreign_verdict                # no card yet: the AKA path reports NO_CARD
    try:
        _foreign_verdict = foreign_iccid(connection)
    finally:
        try:
            connection.disconnect()
        except Exception:
            pass
    return _foreign_verdict


def make_connection_name(reader_name):
    if isinstance(reader_name, str) and reader_name.startswith("imsi:"):
        target_imsi = reader_name[5:]
        for idx in range(len(readers())):
            connection = make_connection_index(idx)
            if connection is None:
                continue
            data, sw1, sw2 = _xfr(connection, toBytes("00a40004026f0700"))
            if sw1 != 0x90:
                continue
            data, sw1, sw2 = _xfr(connection, toBytes("00b0000009"))
            if (sw1, sw2) != (0x90, 0x00):
                continue
            imsi = dec_imsi(bytes(data).hex())
            if imsi == target_imsi:
                print(f"Found target SIM on reader {idx}")
                # re-select ADF.USIM after reading IMSI (IMSI read left EF selected)
                make_reselect_adf(connection)
                return connection
        print("Target SIM not found")
        return None
    # Persisted modem bindings use the full PC/SC reader name so each engine role stays
    # on its own VPCD logical channel.  Resolve that name explicitly; int(reader_name)
    # rejects it and prevents SIP IMS-AKA from ever reaching the selected channel.
    if isinstance(reader_name, str):
        wanted = reader_name.strip()
        for idx, reader in enumerate(readers()):
            if str(reader) == wanted:
                return make_connection_index(idx)
    try:
        return make_connection_index(int(reader_name))
    except (TypeError, ValueError):
        return None


def make_reselect_adf(connection):
    connection.transmit(toBytes("00a40004023f0000"))
    got = _usim_aid_from_dir(connection)
    if got is None:
        return
    aid_length, aid = got
    connection.transmit(toBytes("00a40404") + [aid_length] + toBytes(aid))


def select_adf_usim(connection):
    """SELECT MF -> EF.DIR -> USIM AID -> ADF.USIM. Returns True on success."""
    connection.transmit(toBytes("00a40004023f0000"))
    got = _usim_aid_from_dir(connection)
    if got is None:
        return False
    aid_length, aid = got
    data, sw1, sw2 = _xfr(connection, toBytes("00a40404") + [aid_length] + toBytes(aid))
    return sw1 == 0x90


def open_usim(reader_spec):
    """Return an open connection positioned at ADF.USIM. For a single reader we use it
    directly (IMSI can't be read before PIN). For imsi:<IMSI> with multiple readers we
    verify PIN then match IMSI. Selection/verify happen under a transaction by the caller."""
    rlist = readers()
    if not rlist:
        return None
    # Highest priority: the stable USB-port binding. Open that physical reader directly so we
    # don't probe/burn PIN tries on the wrong card when indices flipped. Applied whatever the
    # reader count is, exactly as pin_keeper does: a host with one reader is the case where a
    # stale index hurts most, because there is no second slot for the fallbacks to land on.
    port = os.environ.get("USIM_READER_PORT", "").strip()
    if port:
        pidx = index_for_port(port)
        if pidx is not None and pidx < len(rlist):
            try:
                conn = rlist[pidx].createConnection()
                conn.connect()
                return conn
            except Exception:
                pass
    # Full names are the stable role binding for modem VPCD slots.  They must win over
    # the legacy index fallback; otherwise every non-numeric name silently selects slot 0.
    if isinstance(reader_spec, str):
        wanted = reader_spec.strip()
        for reader in rlist:
            if str(reader) == wanted:
                try:
                    conn = reader.createConnection()
                    conn.connect()
                    return conn
                except Exception:
                    return None
    if isinstance(reader_spec, str) and reader_spec.startswith("imsi:"):
        target = reader_spec[5:]
        if len(rlist) == 1:
            # One reader holds the only card there is; IMSI cannot be read before the PIN is
            # verified anyway, so scanning for it would just burn PIN tries on that same card.
            conn = rlist[0].createConnection()
            conn.connect()
            return conn
        for r in rlist:
            try:
                conn = r.createConnection()
                conn.connect()
            except Exception:
                continue
            with _Tx(conn):
                if select_adf_usim(conn) and verify_pin(conn):
                    _xfr(conn, toBytes("00a40004026f0700"))
                    d, s1, s2 = _xfr(conn, toBytes("00b0000009"))
                    if s1 == 0x90 and dec_imsi(bytes(d).hex()) == target:
                        return conn
            try:
                conn.disconnect()
            except Exception:
                pass
        return None
    # single reader, or explicit index
    try:
        idx = int(reader_spec)
    except (TypeError, ValueError):
        return None
    if idx >= len(rlist) and len(rlist) == 1:
        # An index past the end names no slot. On a host with a single reader there is still
        # exactly one card this line can mean, and refusing it reports the SIM as NO_CARD while
        # it sits readable in the only reader present (issue #8: a stored ami_reader of 2 on a
        # one-reader host). With several readers the index is genuinely ambiguous -- refuse.
        idx = 0
    if idx < 0 or idx >= len(rlist):
        return None
    conn = rlist[idx].createConnection()
    conn.connect()
    return conn


def verify_pin(connection):
    """Verify CHV1 if a PIN is configured. Idempotent: skips if already verified (9000)."""
    if not USIM_PIN or USIM_PIN.lower() in ("none", "disabled", ""):
        return True
    d, s1, s2 = connection.transmit(toBytes("0020000100"))
    if (s1, s2) == (0x90, 0x00):
        return True  # already verified in this card session
    if s1 == 0x63 and (s2 & 0x0F) < 2:
        print(f"Refusing PIN verify: only {s2 & 0x0F} tries left", flush=True)
        return False
    if not (4 <= len(USIM_PIN) <= 8) or not USIM_PIN.isdigit():
        print("Refusing PIN verify: malformed USIM_PIN (want 4-8 digits)", flush=True)
        return False
    body = [ord(c) for c in USIM_PIN] + [0xFF] * (8 - len(USIM_PIN))
    d, s1, s2 = connection.transmit(toBytes("00200001") + [0x08] + body)
    if (s1, s2) == (0x90, 0x00):
        return True
    print(f"PIN verify failed sw={s1:02x}{s2:02x}", flush=True)
    return False


def read_res_ck_ik(reader_spec, rand, autn):
    res = ck = ik = auts = None
    conn = open_usim(reader_spec)
    if conn is None:
        write_status(state="NO_CARD")
        return res, ck, ik, auts
    # The CARD check itself happened at startup (probe_foreign_card_once): it covers the
    # USB-port, exact-name and index bindings at once, and naming both ICCIDs turns what would
    # surface as an SW=9862 "carrier rejected us" into a binding fault anyone can act on. Only
    # the verdict is consulted here — reading the card again would spend part of the 3s
    # Asterisk allows for this exchange on a question already answered.
    intruder = _foreign_verdict
    if intruder:
        write_status(state="WRONG_CARD", iccid=intruder,
                     detail=f"reader holds ICCID {intruder}, this line is "
                            f"{os.environ.get('USIM_ICCID', '').strip()}")
        try:
            conn.disconnect()
        except Exception:
            pass
        return res, ck, ik, auts
    try:
        with _Tx(conn):
            if not select_adf_usim(conn):
                write_status(state="NO_CARD", detail="ADF.USIM select failed")
                return res, ck, ik, auts
            if not verify_pin(conn):
                write_status(state="PIN_FAIL")
                return res, ck, ik, auts
            data, sw1, sw2 = _xfr(conn, toBytes(
                "008800812210" + rand.upper() + "10" + autn.upper()))
            if (sw1, sw2) == (0x90, 0x00) and data:
                result = toHexString(data).replace(" ", "")
                rc = result[0:2]
                if rc == "DB":  # success
                    res_length = data[1]
                    res = result[4:(4 + res_length * 2)]
                    ck_length = data[2 + res_length]
                    ck = result[(6 + res_length * 2):(6 + res_length * 2 + ck_length * 2)]
                    ik_length = data[2 + res_length + 1 + ck_length]
                    ik = result[(8 + res_length * 2 + ck_length * 2):
                                (8 + res_length * 2 + ck_length * 2 + ik_length * 2)]
                    write_status(state="AUTH_OK")
                elif rc == "DC":  # sync failure -> AUTS
                    auts = result[4:32]
                    write_status(state="AUTH_SYNC")
            else:
                print(f"Authentication failed sw={sw1:02x}{sw2:02x}", flush=True)
                write_status(state="AUTH_FAIL", detail=f"sw={sw1:02x}{sw2:02x}")
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass
    return res, ck, ik, auts


def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} <ini-file>")
        sys.exit(1)
    config = configparser.ConfigParser()
    config.read(sys.argv[1])
    cfg_endpoint = config.sections()[0]
    cfg_reader = config.get(cfg_endpoint, "reader")
    cfg_host = config.get(cfg_endpoint, "host")
    cfg_username = config.get(cfg_endpoint, "username")
    cfg_secret = config.get(cfg_endpoint, "secret")
    print(f"Endpoint={cfg_endpoint} reader={cfg_reader} host={cfg_host} user={cfg_username}")
    write_status(state="STARTING")
    # Settle the binding question here, off the AKA path and before Asterisk can ask anything.
    intruder = probe_foreign_card_once(cfg_reader)
    if intruder:
        print(f"reader holds ICCID {intruder}, this line is "
              f"{os.environ.get('USIM_ICCID', '').strip()} -- refusing to authenticate")

    manager = Manager(loop=asyncio.get_event_loop(), host=cfg_host,
                      username=cfg_username, secret=cfg_secret)

    @manager.register_event("FullyBooted")
    def on_booted(manager, message):
        print("Asterisk ready, triggering registration...")
        manager.send_action({"Action": "PJSIPRegister", "Registration": cfg_endpoint})

    @manager.register_event("AuthRequest")
    def on_auth(manager, message):
        algo = message.Algorithm
        rand = message.RAND
        autn = message.AUTN
        print(f"AuthRequest: Algorithm={algo}")
        res, ck, ik, auts = read_res_ck_ik(cfg_reader, rand, autn)
        if res is not None:
            manager.send_action({"Action": "AuthResponse", "Registration": cfg_endpoint,
                                 "RES": res, "CK": ck, "IK": ik})
        elif auts is not None:
            manager.send_action({"Action": "AuthResponse", "Registration": cfg_endpoint,
                                 "AUTS": auts})
        else:
            manager.send_action({"Action": "AuthResponse", "Registration": cfg_endpoint})
        print("AuthResponse sent")

    @manager.register_event("Newchannel")
    def on_newchannel(manager, message):
        context = message.Context
        channel = message.Channel
        time.sleep(0.5)
        if context == cfg_endpoint:
            manager.send_action({"Action": "DedicatedBearerStatus", "Channel": channel,
                                 "Status": "Up"})
            print(f"DedicatedBearerStatus sent: Channel={channel}")

    manager.connect()
    try:
        manager.loop.run_forever()
    except KeyboardInterrupt:
        manager.loop.close()


if __name__ == "__main__":
    main()
