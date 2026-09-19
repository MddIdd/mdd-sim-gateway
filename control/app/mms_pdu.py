"""MMS / WAP Push codec: turn the bytes a carrier's MMSC puts on the wire into Python
objects, and turn a message this gateway wants to send back into those bytes.

Two encodings are involved and both are decoded here because an MMS never travels alone:

  - WAP Push (WAP-230-WSP 8.4, "connectionless session"): the thin envelope a WAP-push SMS
    carries. All it says is "here is a Content-Type and a body" -- for MMS that body is an
    M-Notification.ind telling the handset there is a message waiting at an MMSC URL.
  - MMS PDU encoding (OMA-TS-MMS_ENC-V1_3, formerly WAP-209-MMSEncapsulation): the actual
    protocol data units -- m-notification-ind, m-retrieve-conf, m-send-req and friends --
    built out of WSP's generic primitives (uintvar, short-integer, long-integer,
    value-length, text-string) per WAP-230-WSP 8.4.

This module is pure stdlib and self-contained on purpose: the gateway's MMS path talks to
whatever the carrier's MMSC serves, which ranges from spec-perfect to years-old and mildly
broken, and a third-party codec library is one more thing to vendor and trust with hostile
input. Every public decode entry point is bounds-checked against truncated or garbage bytes
and never raises anything other than MmsDecodeError -- a malformed notification SMS or a
flaky MMSC response should degrade to "we couldn't read this," not take down the request
that was fetching it.

What lives here:
  - Low-level WSP primitive readers/writers (uintvar, short/long-integer, value-length,
    text-string, encoded-string-value) -- the alphabet everything else is spelled with.
  - parse_wap_push / is_mms_wap_push / extract_wdp_port for the SMS/WAP-push envelope.
  - decode_pdu for any MMS PDU (notification, retrieve-conf, send-conf, delivery-ind, ...).
  - encode_notifyresp_ind / encode_acknowledge_ind / encode_send_req plus build_smil for the
    PDUs this gateway needs to originate.

A few header value grammars in OMA-TS-MMS_ENC assign literal octet values (e.g. "Personal =
<Octet 128>", "m-send-req = <Octet 128>"). Unlike a true WSP Short-integer (whose wire byte
is 0x80 | value, so the *decoded* value is what you compare against), these MMS-specific
enumerations already bake the 0x80 into their assigned constant. So MESSAGE_TYPE, the
X-Mms-Status/Response-Status/Retrieve-Status families and the report/read-report booleans
are read and written as the *raw wire byte*, not masked through the generic short-integer
primitive -- confirmed against the m-notification-ind wire format (see the module tests),
where X-Mms-Message-Type's wire byte 0x82 must equal M_NOTIFICATION_IND=0x82 unmasked.
MMS-Version is the opposite case: its assigned value genuinely is major<<4|minor and the
wire byte is 0x80 | that value, so it decodes correctly through the normal masked
short-integer rule.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


class MmsDecodeError(ValueError):
    """Raised by every public decode entry point in this module, and only this. Malformed,
    truncated, or hostile input must degrade to this one exception -- never IndexError,
    UnicodeDecodeError, RecursionError, etc. -- so callers can catch a single type."""


# ---------------------------------------------------------------------------------------
# WSP generic primitives (WAP-230-WSP 8.4.2.1 "Basic rules", 8.1.2 "uintvar")
# ---------------------------------------------------------------------------------------

_MAX_UINTVAR_OCTETS = 9  # 9*7 = 63 bits; more than that is not a real length/id, it's noise


def read_uintvar(data: bytes, pos: int) -> tuple[int, int]:
    """WAP-230-WSP 8.1.2: a base-128 integer, continuation bit (0x80) set on every octet
    but the last. Returns (value, next_pos)."""
    value = 0
    count = 0
    while True:
        if pos >= len(data):
            raise MmsDecodeError("truncated uintvar")
        b = data[pos]
        pos += 1
        count += 1
        value = (value << 7) | (b & 0x7F)
        if not (b & 0x80):
            return value, pos
        if count >= _MAX_UINTVAR_OCTETS:
            raise MmsDecodeError("uintvar too long")


def write_uintvar(value: int) -> bytes:
    if value < 0:
        raise ValueError("uintvar must be non-negative")
    out = [value & 0x7F]
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


def read_short_integer(data: bytes, pos: int) -> tuple[int, int]:
    """WAP-230-WSP 8.4.2.1: one octet, top bit set, value in the low 7 bits (0-127)."""
    if pos >= len(data):
        raise MmsDecodeError("truncated short-integer")
    b = data[pos]
    if not (b & 0x80):
        raise MmsDecodeError("not a short-integer")
    return b & 0x7F, pos + 1


def write_short_integer(value: int) -> bytes:
    if not 0 <= value <= 0x7F:
        raise ValueError("short-integer value must fit in 7 bits")
    return bytes([0x80 | value])


def read_long_integer(data: bytes, pos: int) -> tuple[int, int]:
    """WAP-230-WSP 8.4.2.1: a Short-length octet (0-30) then that many big-endian bytes."""
    if pos >= len(data):
        raise MmsDecodeError("truncated long-integer")
    length = data[pos]
    if length > 30:
        raise MmsDecodeError("invalid long-integer length")
    pos += 1
    end = pos + length
    if end > len(data):
        raise MmsDecodeError("truncated long-integer value")
    value = int.from_bytes(data[pos:end], "big") if length else 0
    return value, end


def write_long_integer(value: int) -> bytes:
    if value < 0:
        raise ValueError("long-integer must be non-negative")
    nbytes = max(1, (value.bit_length() + 7) // 8)
    body = value.to_bytes(nbytes, "big")
    if len(body) > 30:
        raise ValueError("long-integer value too large")
    return bytes([len(body)]) + body


def read_value_length(data: bytes, pos: int) -> tuple[int, int]:
    """WAP-230-WSP 8.4.2.2: Short-length (0-30) directly, or 31 ("length-quote") followed
    by a uintvar for lengths that don't fit in 5 bits."""
    if pos >= len(data):
        raise MmsDecodeError("truncated value-length")
    b = data[pos]
    if b < 31:
        return b, pos + 1
    if b == 31:
        return read_uintvar(data, pos + 1)
    raise MmsDecodeError("invalid value-length prefix")


def write_value_length(length: int) -> bytes:
    if length < 0:
        raise ValueError("value-length must be non-negative")
    if length <= 30:
        return bytes([length])
    return bytes([31]) + write_uintvar(length)


def _decode_text_bytes(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")  # never fails: every byte value is a valid code point


def read_text_string(data: bytes, pos: int) -> tuple[str, int]:
    """WAP-230-WSP 8.4.2.1: NUL-terminated text, optionally led by a 0x7F quote octet used
    when the first character would otherwise be mistaken for a Short-integer/Value-length
    lead byte. The quote is stripped from the returned string."""
    if pos >= len(data):
        raise MmsDecodeError("truncated text-string")
    start = pos + 1 if data[pos] == 0x7F else pos
    nul = data.find(b"\x00", start)
    if nul == -1:
        raise MmsDecodeError("unterminated text-string")
    return _decode_text_bytes(data[start:nul]), nul + 1


def write_text_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    if raw and (raw[0] & 0x80 or raw[0] <= 0x1F):
        raw = b"\x7f" + raw  # quote so the first byte can't be read as a length/short-int
    return raw + b"\x00"


def read_quoted_string(data: bytes, pos: int) -> tuple[str, int]:
    """WAP-230-WSP 8.4.2.1: like Text-string but led by a literal '"' (0x22), used for
    Content-ID so the value can carry the RFC 2045 angle-bracket form unambiguously."""
    if pos >= len(data) or data[pos] != 0x22:
        raise MmsDecodeError("expected quoted-string")
    nul = data.find(b"\x00", pos + 1)
    if nul == -1:
        raise MmsDecodeError("unterminated quoted-string")
    return _decode_text_bytes(data[pos + 1:nul]), nul + 1


def write_quoted_string(value: str) -> bytes:
    return b'"' + value.encode("utf-8") + b"\x00"


# ---------------------------------------------------------------------------------------
# Charsets (OMA-TS-MMS_ENC 7.3.x "Encoded-string-value" / IANA MIBenum registry)
# ---------------------------------------------------------------------------------------

# Only the handful an MMSC actually uses. MIBenum -> Python codec name. utf-8 (106) fits a
# Short-integer; the wider CJK/UCS-2 values need the Long-integer form of Char-set.
_CHARSETS: dict[int, str] = {
    3: "us-ascii",
    4: "iso-8859-1",
    17: "shift_jis",
    106: "utf-8",
    1000: "utf-16-be",   # iso-10646-ucs-2, big-endian per the MIB definition
    1015: "utf-16",
    2025: "gb2312",
    2026: "big5",
}
_CHARSET_ALIASES = {"ascii": "us-ascii", "latin-1": "iso-8859-1", "latin1": "iso-8859-1"}
_CHARSET_TO_MIB: dict[str, int] = {}
for _mib, _name in _CHARSETS.items():
    _CHARSET_TO_MIB.setdefault(_name, _mib)


def charset_name(mib: int) -> str:
    """MIBenum -> Python codec name, defaulting to utf-8 for anything we don't recognise
    (an unrecognised charset is still overwhelmingly likely to decode sanely as utf-8)."""
    return _CHARSETS.get(mib, "utf-8")


def charset_mib(name: str) -> int:
    key = _CHARSET_ALIASES.get((name or "utf-8").lower(), (name or "utf-8").lower())
    return _CHARSET_TO_MIB.get(key, 106)


def _read_charset_value(data: bytes, pos: int, end: int) -> tuple[int, int]:
    """Char-set = Short-integer (well-known MIBenum <=127) | Long-integer (wider MIBenum).
    Same short-vs-long discrimination as everywhere else: top bit set -> short-integer."""
    if pos >= end:
        raise MmsDecodeError("truncated charset")
    b = data[pos]
    if b & 0x80:
        return b & 0x7F, pos + 1
    length = b
    pos += 1
    cend = pos + length
    if cend > end:
        raise MmsDecodeError("truncated charset long-integer")
    return (int.from_bytes(data[pos:cend], "big") if length else 0), cend


def read_encoded_string_value(data: bytes, pos: int) -> tuple[str, int]:
    """OMA-TS-MMS_ENC 7.3.x: Encoded-string-value = Text-string | Value-length Char-set
    Text-string. A Value-length's lead byte is always <=31 (30 = max Short-length, 31 =
    length-quote); a Text-string's lead byte is either a quote (0x7F) or a printable ASCII
    character (>=0x20) -- ranges that never overlap, so peeking one byte discriminates."""
    if pos >= len(data):
        raise MmsDecodeError("truncated encoded-string-value")
    if data[pos] > 31:
        return read_text_string(data, pos)
    length, pos2 = read_value_length(data, pos)
    end = pos2 + length
    if end > len(data):
        raise MmsDecodeError("truncated encoded-string-value")
    mib, pos3 = _read_charset_value(data, pos2, end)
    raw = data[pos3:end]
    if raw[:1] == b"\x7f":
        raw = raw[1:]
    if raw[-1:] == b"\x00":
        raw = raw[:-1]
    try:
        return raw.decode(charset_name(mib), errors="replace"), end
    except LookupError:
        return raw.decode("utf-8", errors="replace"), end


def write_encoded_string_value(value: str, charset: str = "utf-8") -> bytes:
    """Plain ASCII writes as a bare Text-string (self-delimiting, no charset needed at all);
    anything else gets the Value-length-wrapped Char-set form."""
    try:
        return write_text_string(value.encode("ascii").decode("ascii"))
    except UnicodeEncodeError:
        pass
    body = value.encode(charset, errors="replace")
    mib = charset_mib(charset)
    charset_bytes = write_short_integer(mib) if mib <= 0x7F else write_long_integer(mib)
    payload = charset_bytes + body + b"\x00"
    return write_value_length(len(payload)) + payload


def _read_generic_value_bytes(data: bytes, pos: int) -> tuple[bytes, int]:
    """The generic WSP "Value" grammar (short-integer / long-integer & value-length /
    text-string / quoted-string), used to skip past a header or parameter this module
    doesn't specifically decode without losing sync with the rest of the PDU. Discriminated
    the same way as everywhere else: top bit -> short-integer; <=31 -> a Value-length
    (covers both Long-integer's own Short-length and the general opaque-value form); 0x22
    -> quoted-string; anything else -> a plain text-string."""
    if pos >= len(data):
        raise MmsDecodeError("truncated value")
    b = data[pos]
    if b & 0x80:
        return bytes([b]), pos + 1
    if b <= 0x1F:
        length, pos2 = read_value_length(data, pos)
        end = pos2 + length
        if end > len(data):
            raise MmsDecodeError("truncated value-length value")
        return data[pos2:end], end
    if b == 0x22:
        s, pos2 = read_quoted_string(data, pos)
        return s.encode("utf-8", "replace"), pos2
    s, pos2 = read_text_string(data, pos)
    return s.encode("utf-8", "replace"), pos2


# ---------------------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------------------

# MMS addresses travel as plain strings everywhere in this module's public API (headers,
# MmsPart, MmsPdu) rather than as a dedicated address type: an MMS address is either an
# MSISDN with a "/TYPE=PLMN" (or /TYPE=IPv4 etc.) suffix or an email address, and every
# caller just wants the clean string. A wrapper class would only add ceremony around what
# is, structurally, a str with one optional suffix to strip.
def normalize_address(value: str) -> str:
    """"+447700900123/TYPE=PLMN" -> "+447700900123"; "a@b.c" is returned unchanged."""
    value = value.strip()
    idx = value.upper().find("/TYPE=")
    return value[:idx] if idx != -1 else value


# ---------------------------------------------------------------------------------------
# SMS User Data Header port addressing (3GPP TS 23.040 9.2.3.24, WAP-230-WSP 8.4.2.4 for
# the port numbers a WAP-push SMS's WSP payload is "addressed" to)
# ---------------------------------------------------------------------------------------

def encode_address(value: str) -> str:
    """The wire form of a recipient (OMA-TS-MMS_ENC 8, Address): an email address as-is, a
    number with its "/TYPE=PLMN" suffix. MMSCs reject a bare number in To/Cc."""
    text = str(value or "").strip()
    if "@" in text or "/TYPE=" in text.upper():
        return text
    return "".join(ch for ch in text if ch.isdigit() or ch == "+") + "/TYPE=PLMN"


def extract_wdp_port(udh: bytes) -> tuple[int | None, int | None]:
    """(dest_port, src_port) from a UDH's Information Elements, or (None, None) if no port
    IE is present. IE 0x05 = 16-bit application port addressing (dest hi/lo, src hi/lo);
    IE 0x04 = the 8-bit variant (dest, src).

    Whether the caller's ``udh`` bytes include the leading UDHL length octet or not is
    genuinely ambiguous from the argument alone, so this tries both: first as a bare IE
    stream (no UDHL), and -- only if that does not resolve to a complete port IE -- again
    after skipping one byte as UDHL. For the canonical port-addressed WAP-push UDH
    (05 04 <dest:16> <src:16>, i.e. no UDHL prefix at all, since the WDP header IS the
    whole UDH here) the first attempt succeeds directly.
    """
    def scan(buf: bytes, start: int) -> tuple[int | None, int | None]:
        pos = start
        dest = src = None
        while pos + 2 <= len(buf):
            iei = buf[pos]
            iedl = buf[pos + 1]
            pos += 2
            if pos + iedl > len(buf):
                break
            ie = buf[pos:pos + iedl]
            pos += iedl
            if iei == 0x05 and iedl >= 4:
                dest = (ie[0] << 8) | ie[1]
                src = (ie[2] << 8) | ie[3]
            elif iei == 0x04 and iedl >= 2:
                dest = ie[0]
                src = ie[1]
        return dest, src

    dest, src = scan(udh, 0)
    if dest is None and len(udh) >= 1:
        dest, src = scan(udh, 1)
    return dest, src


# ---------------------------------------------------------------------------------------
# WAP Push envelope (WAP-230-WSP 8.4, connectionless session PDUs 0x06 Push / 0x07
# Confirmed Push -- what a WAP-push SMS actually carries)
# ---------------------------------------------------------------------------------------

PUSH_PDU_TYPE = 0x06
CONFIRMED_PUSH_PDU_TYPE = 0x07


@dataclass
class WapPush:
    transaction_id: int
    pdu_type: int
    content_type: str
    headers: dict
    body: bytes


def parse_wap_push(data: bytes) -> WapPush:
    """Decode the connectionless-WSP envelope: octet TID, octet PDU type, uintvar
    HeadersLen, HeadersLen bytes of headers (Content-Type first), then the body.

    HeadersLen is authoritative for where the body starts, so the body offset is correct
    even if a header inside that span uses a grammar this function doesn't specifically
    know (WSP push headers are a much bigger table than the MMS header table decode_pdu
    understands, and only Content-Type matters for routing a push to the MMS path) --
    ``headers`` is filled in on a strict best-effort basis and parsing simply stops
    collecting more of it on the first thing it can't make sense of.
    """
    try:
        if len(data) < 3:
            raise MmsDecodeError("push PDU too short")
        pos = 0
        transaction_id = data[pos]
        pos += 1
        pdu_type = data[pos]
        pos += 1
        if pdu_type not in (PUSH_PDU_TYPE, CONFIRMED_PUSH_PDU_TYPE):
            raise MmsDecodeError(f"not a WAP push PDU (type {pdu_type:#x})")
        headers_len, pos = read_uintvar(data, pos)
        headers_start = pos
        headers_end = headers_start + headers_len
        if headers_end > len(data):
            raise MmsDecodeError("push HeadersLen exceeds available data")
        content_type, _params, hp = _read_content_type_value(data, headers_start)
        headers: dict = {}
        while hp < headers_end:
            try:
                b = data[hp]
                if b & 0x80:
                    hp += 1
                    _raw, hp = _read_generic_value_bytes(data, hp)
                else:
                    name, hp = read_text_string(data, hp)
                    raw, hp = _read_generic_value_bytes(data, hp)
                    headers[name.lower()] = _decode_text_bytes(raw.rstrip(b"\x00"))
            except MmsDecodeError:
                break  # best-effort: stop collecting extra headers, body offset is unaffected
        body = bytes(data[headers_end:])
        return WapPush(transaction_id=transaction_id, pdu_type=pdu_type,
                        content_type=content_type, headers=headers, body=body)
    except MmsDecodeError:
        raise
    except Exception as exc:  # pragma: no cover - defensive: never let anything else escape
        raise MmsDecodeError(f"malformed WAP push: {exc}") from exc


def is_mms_wap_push(data: bytes) -> bool:
    try:
        return parse_wap_push(data).content_type == "application/vnd.wap.mms-message"
    except MmsDecodeError:
        return False


# ---------------------------------------------------------------------------------------
# WSP Content-Type / media-type grammar (WAP-230-WSP 8.4.2.4), shared by the push envelope,
# the top-level MMS Content-Type header, and every multipart entry's own Content-Type.
# ---------------------------------------------------------------------------------------

# WAP-230-WSP Table 40 / OMNA "WSP Content Type Codes". Entries this module's own tests
# depend on (0x03, the image block 0x1D-0x21, 0x23 multipart.mixed, 0x33 multipart.related,
# 0x3E mms-message) are verified against the m-notification-ind wire format and this
# module's own encoder/decoder round trip. The remaining entries are filled in as best-
# effort from the registry; a code this table doesn't have decodes to a synthetic
# "application/x-wsp-NN" placeholder rather than guessing wrong and losing data.
WELL_KNOWN_CONTENT_TYPES: dict[int, str] = {
    0x00: "*/*",
    0x01: "text/*",
    0x02: "text/html",
    0x03: "text/plain",
    0x04: "text/x-hdml",
    0x05: "text/x-ttml",
    0x06: "text/x-vCalendar",
    0x07: "text/x-vCard",
    0x08: "text/vnd.wap.wml",
    0x09: "text/vnd.wap.wmlscript",
    0x0A: "text/vnd.wap.wta-event",
    0x0B: "multipart/*",
    0x0C: "multipart/mixed",
    0x0D: "multipart/form-data",
    0x0E: "multipart/byteranges",
    0x0F: "multipart/alternative",
    0x10: "application/*",
    0x11: "application/java-vm",
    0x12: "application/x-www-form-urlencoded",
    0x13: "application/x-hdmlc",
    0x14: "application/vnd.wap.wmlc",
    0x15: "application/vnd.wap.wmlscriptc",
    0x16: "application/vnd.wap.wta-eventc",
    0x17: "application/vnd.wap.uaprof",
    0x18: "application/vnd.wap.wtls-ca-certificate",
    0x19: "application/vnd.wap.wtls-user-certificate",
    0x1A: "application/x-x509-ca-cert",
    0x1B: "application/x-x509-user-cert",
    0x1D: "image/gif",
    0x1E: "image/jpeg",
    0x1F: "image/tiff",
    0x20: "image/png",
    0x21: "image/vnd.wap.wbmp",
    0x22: "application/vnd.wap.multipart.*",
    0x23: "application/vnd.wap.multipart.mixed",
    0x24: "application/vnd.wap.multipart.form-data",
    0x25: "application/vnd.wap.multipart.byteranges",
    0x26: "application/vnd.wap.multipart.alternative",
    0x27: "application/xml",
    0x28: "text/xml",
    0x29: "application/vnd.wap.wbxml",
    0x2A: "application/x-x968-cross-cert",
    0x2B: "application/x-x968-ca-cert",
    0x2C: "application/x-x968-user-cert",
    0x2D: "text/vnd.wap.si",
    0x2E: "application/vnd.wap.sic",
    0x2F: "text/vnd.wap.sl",
    0x30: "application/vnd.wap.slc",
    0x31: "text/vnd.wap.co",
    0x32: "application/vnd.wap.coc",
    0x33: "application/vnd.wap.multipart.related",
    0x34: "application/vnd.wap.sia",
    0x35: "text/vnd.wap.connectivity-xml",
    0x36: "application/vnd.wap.connectivity-wbxml",
    0x37: "application/pkcs7-mime",
    0x38: "application/vnd.wap.hashed-certificate",
    0x39: "application/vnd.wap.signed-certificate",
    0x3A: "application/vnd.wap.cert-response",
    0x3B: "application/vnd.wap.xhtml+xml",
    0x3C: "application/vnd.wap.wml+xml",
    0x3E: "application/vnd.wap.mms-message",
    0x3F: "application/vnd.wap.rollover-certificate",
    0x40: "application/vnd.wap.locc+wbxml",
    0x41: "application/vnd.wap.loc+xml",
    0x42: "application/vnd.syncml.dm+wbxml",
    0x43: "application/vnd.syncml.dm+xml",
    0x44: "application/vnd.syncml.notification",
    0x45: "text/css",
    0x46: "application/vnd.oma.dd+xml",
    0x47: "application/vnd.oma.drm.message",
    0x48: "application/vnd.oma.drm.content",
    0x49: "application/vnd.oma.drm.rights+xml",
    0x4A: "application/vnd.oma.drm.rights+wbxml",
    0x4B: "application/vnd.wv.csp+xml",
}
_CONTENT_TYPE_TO_CODE: dict[str, int] = {}
for _code, _name in WELL_KNOWN_CONTENT_TYPES.items():
    _CONTENT_TYPE_TO_CODE.setdefault(_name.lower(), _code)


def _media_placeholder(code: int) -> str:
    return f"application/x-wsp-{code:02x}"


def _read_media(data: bytes, pos: int, end: int) -> tuple[str, int]:
    """Well-known-media | Extension-Media (a plain Text-string), per WSP 8.4.2.4."""
    if pos >= end:
        raise MmsDecodeError("truncated media")
    b = data[pos]
    if b & 0x80:
        code = b & 0x7F
        return WELL_KNOWN_CONTENT_TYPES.get(code, _media_placeholder(code)), pos + 1
    return read_text_string(data, pos)


def _read_params(data: bytes, pos: int, end: int) -> tuple[dict, int]:
    """WAP-230-WSP Table 38 well-known parameters, limited to the ones MMS content types
    actually use (charset, name, filename, type, start, start-info across both the v1.1
    and v1.4 field-code assignments), plus the generic Typed-parameter/Untyped-parameter
    fallback for everything else so an unrecognised parameter is skipped, not fatal."""
    params: dict = {}
    while pos < end:
        if pos >= len(data):
            raise MmsDecodeError("truncated parameter")
        code_byte = data[pos]
        if code_byte & 0x80:
            code = code_byte & 0x7F
            pos += 1
            if code == 0x01:  # Charset
                mib, pos = _read_charset_value(data, pos, end)
                params["charset"] = charset_name(mib)
            elif code in (0x05, 0x17):  # Name (deprecated / current)
                params["name"], pos = read_text_string(data, pos)
            elif code in (0x06, 0x18):  # Filename (deprecated / current)
                params["filename"], pos = read_text_string(data, pos)
            elif code == 0x09:  # Type (multipart/related, Constrained-encoding)
                if pos < len(data) and data[pos] & 0x80:
                    v = data[pos] & 0x7F
                    params["type"] = WELL_KNOWN_CONTENT_TYPES.get(v, _media_placeholder(v))
                    pos += 1
                else:
                    params["type"], pos = read_text_string(data, pos)
            elif code in (0x0A, 0x19):  # Start (deprecated / current)
                params["start"], pos = read_text_string(data, pos)
            elif code in (0x0B, 0x1A):  # Start-info (deprecated / current)
                params["start-info"], pos = read_text_string(data, pos)
            else:
                _raw, pos = _read_generic_value_bytes(data, pos)
        else:
            token, pos = read_text_string(data, pos)
            raw, pos = _read_generic_value_bytes(data, pos)
            params[token] = _decode_text_bytes(raw.rstrip(b"\x00"))
    return params, pos


def _read_content_type_value(data: bytes, pos: int) -> tuple[str, dict, int]:
    """Content-type-value = Constrained-media | Media-type, where Media-type is a
    Value-length-wrapped (Media *Parameter). Constrained-media has no parameters, so it's
    just a bare well-known short-integer or a bare (NUL-terminated) Extension-Media string.
    """
    if pos >= len(data):
        raise MmsDecodeError("truncated content-type")
    b = data[pos]
    if b & 0x80:
        code = b & 0x7F
        return WELL_KNOWN_CONTENT_TYPES.get(code, _media_placeholder(code)), {}, pos + 1
    if b <= 0x1F:
        length, pos2 = read_value_length(data, pos)
        end = pos2 + length
        if end > len(data):
            raise MmsDecodeError("truncated content-type value")
        media, pos3 = _read_media(data, pos2, end)
        params, _pos4 = _read_params(data, pos3, end)
        return media, params, end
    media, pos2 = read_text_string(data, pos)
    return media, {}, pos2


# ---------------------------------------------------------------------------------------
# MMS parts and PDU
# ---------------------------------------------------------------------------------------

@dataclass
class MmsPart:
    content_type: str
    data: bytes
    name: str = ""
    content_id: str = ""
    content_location: str = ""
    charset: str = ""
    # Playing time of an audio or video part, when known; used for the SMIL, never encoded.
    duration_ms: int | None = None

    def text(self) -> str:
        try:
            return self.data.decode(self.charset or "utf-8", errors="replace")
        except LookupError:
            return self.data.decode("utf-8", errors="replace")


M_SEND_REQ = 0x80
M_SEND_CONF = 0x81
M_NOTIFICATION_IND = 0x82
M_NOTIFYRESP_IND = 0x83
M_RETRIEVE_CONF = 0x84
M_ACKNOWLEDGE_IND = 0x85
M_DELIVERY_IND = 0x86
M_READ_REC_IND = 0x87
M_READ_ORIG_IND = 0x88

STATUS_EXPIRED = 0x80
STATUS_RETRIEVED = 0x81
STATUS_REJECTED = 0x82
STATUS_DEFERRED = 0x83
STATUS_UNRECOGNISED = 0x84
STATUS_INDETERMINATE = 0x85
STATUS_FORWARDED = 0x86
STATUS_UNREACHABLE = 0x87

RESPONSE_STATUS_OK = 0x80
RETRIEVE_STATUS_OK = 0x80

# OMA-TS-MMS_ENC 7.3.x X-Mms-Response-Status descriptions, keyed by the raw wire octet.
RESPONSE_STATUS_DESCRIPTIONS: dict[int, str] = {
    0x80: "Ok",
    0x81: "Error-unspecified",
    0x82: "Error-service-denied",
    0x83: "Error-message-format-corrupt",
    0x84: "Error-sending-address-unresolved",
    0x85: "Error-message-not-found",
    0x86: "Error-network-problem",
    0x87: "Error-content-not-accepted",
    0x88: "Error-unsupported-message",
    0xC0: "Error-transient-failure",
    0xC1: "Error-transient-sending-address-unresolved",
    0xC2: "Error-transient-message-not-found",
    0xC3: "Error-transient-network-problem",
    0xC4: "Error-transient-partial-success",
    0xC5: "Error-transient-content-not-accepted",
    0xC6: "Error-transient-unsupported-message",
    0xC7: "Error-transient-service-denied",
    0xE0: "Error-permanent-failure",
    0xE1: "Error-permanent-service-denied",
    0xE2: "Error-permanent-message-format-corrupt",
    0xE3: "Error-permanent-sending-address-unresolved",
    0xE4: "Error-permanent-message-not-found",
    0xE5: "Error-permanent-content-not-accepted",
    0xE6: "Error-permanent-reply-charging-limitations-not-met",
    0xE7: "Error-permanent-reply-charging-request-not-accepted",
    0xE8: "Error-permanent-reply-charging-forwarding-denied",
    0xE9: "Error-permanent-reply-charging-not-supported",
    0xEA: "Error-permanent-address-hiding-not-supported",
    0xEB: "Error-permanent-lack-of-prepaid",
    0xEC: "Error-permanent-unsupported-message",
}

# OMA-TS-MMS_ENC 7.3.x X-Mms-Retrieve-Status descriptions.
RETRIEVE_STATUS_DESCRIPTIONS: dict[int, str] = {
    0x80: "Ok",
    0xC0: "Error-transient-failure",
    0xC1: "Error-transient-message-not-found",
    0xC2: "Error-transient-network-problem",
    0xE0: "Error-permanent-failure",
    0xE1: "Error-permanent-service-denied",
    0xE2: "Error-permanent-message-format-corrupt",
    0xE3: "Error-permanent-message-not-found",
}

_MESSAGE_CLASS_BY_BYTE = {0x80: "personal", 0x81: "advertisement",
                          0x82: "informational", 0x83: "auto"}
_MESSAGE_CLASS_TO_BYTE = {v: k for k, v in _MESSAGE_CLASS_BY_BYTE.items()}

# OMA-TS-MMS_ENC 7.3.x header field names -> field code (the wire byte is 0x80 | code).
_FIELD_CONTENT_TYPE = 0x04
_FIELD_FROM = 0x09
_FIELD_MESSAGE_TYPE = 0x0C
_FIELD_MMS_VERSION = 0x0D
_FIELD_MESSAGE_SIZE = 0x0E


@dataclass
class MmsPdu:
    message_type: int
    transaction_id: str = ""
    mms_version: tuple[int, int] | None = None
    headers: dict = field(default_factory=dict)
    content_type: str = ""
    content_type_params: dict = field(default_factory=dict)
    parts: list = field(default_factory=list)

    @property
    def from_address(self) -> str:
        return self.headers.get("from", "")

    @property
    def to(self) -> list:
        return self.headers.get("to", [])

    @property
    def subject(self) -> str:
        return self.headers.get("subject", "")

    @property
    def date(self):
        return self.headers.get("date")

    @property
    def content_location(self) -> str:
        return self.headers.get("content-location", "")

    @property
    def expiry(self):
        return self.headers.get("expiry")

    @property
    def message_size(self):
        return self.headers.get("message-size")

    @property
    def status(self):
        return self.headers.get("status")

    @property
    def response_status(self):
        return self.headers.get("response-status")

    @property
    def retrieve_status(self):
        return self.headers.get("retrieve-status")

    @property
    def message_id(self) -> str:
        return self.headers.get("message-id", "")


def _read_expiry_like(data: bytes, pos: int, now: int) -> tuple[int, int]:
    """Expiry / Delivery-Time = Value-length (Absolute-token Date-value |
    Relative-token Delta-seconds-value). Both Date-value and Delta-seconds-value are
    Long-integer in the spec; a couple of MMSCs squeeze small deltas into a bare
    short-integer instead, so both are accepted defensively."""
    length, pos = read_value_length(data, pos)
    end = pos + length
    if end > len(data):
        raise MmsDecodeError("truncated expiry/delivery-time")
    if pos >= end:
        raise MmsDecodeError("empty expiry/delivery-time")
    token = data[pos]
    pos += 1
    if pos < end and data[pos] & 0x80:
        value = data[pos] & 0x7F
    else:
        value, _ = read_long_integer(data, pos)
    result = value if token == 0x80 else now + value
    return result, end


def _decode_part_headers(data: bytes, pos: int, end: int) -> dict:
    """Multipart entry headers (WAP-230-WSP 8.5): well-known 0x8E Content-Location,
    0xC0 Content-ID (quoted-string), 0xAE Content-Disposition, plus a fallback for the
    textual "Name: value" style some encoders (including this module's own, historically)
    emit for anything else."""
    headers: dict = {}
    while pos < end:
        if pos >= len(data):
            raise MmsDecodeError("truncated part header")
        b = data[pos]
        if b == 0x8E:  # Content-Location
            pos += 1
            headers["content-location"], pos = read_text_string(data, pos)
        elif b == 0xC0:  # Content-ID
            pos += 1
            if pos < len(data) and data[pos] == 0x22:
                val, pos = read_quoted_string(data, pos)
            else:
                val, pos = read_text_string(data, pos)
            headers["content-id"] = val.strip("<>")
        elif b == 0xAE:  # Content-Disposition
            pos += 1
            length, pos = read_value_length(data, pos)
            dend = pos + length
            if dend > end:
                raise MmsDecodeError("truncated content-disposition")
            pos += 1  # disposition token (form-data/attachment/inline) -- not needed here
            dparams, _pos2 = _read_params(data, pos, dend)
            pos = dend
            if "filename" in dparams:
                headers["filename"] = dparams["filename"]
        elif b & 0x80:
            pos += 1
            _raw, pos = _read_generic_value_bytes(data, pos)  # unknown well-known header
        else:
            token, pos = read_text_string(data, pos)
            if ":" in token:  # "Name: value" packed into a single text-string
                name, _, val = token.partition(":")
                headers[name.strip().lower()] = val.strip()
            else:
                raw, pos = _read_generic_value_bytes(data, pos)
                headers[token.lower()] = _decode_text_bytes(raw.rstrip(b"\x00"))
    return headers


def _decode_multipart(data: bytes, pos: int, limit: int) -> tuple[list, int]:
    """WAP-230-WSP 8.5 multipart body: uintvar nEntries, then per entry uintvar
    HeadersLen, uintvar DataLen, ContentType, the rest of the entry's headers, and
    finally DataLen bytes of part data."""
    n, pos = read_uintvar(data, pos)
    if n > 10000:
        raise MmsDecodeError("implausible multipart entry count")
    parts = []
    for _ in range(n):
        headers_len, pos = read_uintvar(data, pos)
        data_len, pos = read_uintvar(data, pos)
        headers_start = pos
        content_type, ct_params, pos = _read_content_type_value(data, pos)
        headers_end = headers_start + headers_len
        if headers_end > limit:
            raise MmsDecodeError("truncated multipart entry headers")
        part_headers = _decode_part_headers(data, pos, headers_end) if pos < headers_end else {}
        pos = headers_end
        data_end = pos + data_len
        if data_end > limit:
            raise MmsDecodeError("truncated multipart entry data")
        part_data = bytes(data[pos:data_end])
        pos = data_end
        name = ct_params.get("name") or part_headers.get("content-location") \
            or part_headers.get("filename") or ""
        parts.append(MmsPart(
            content_type=content_type,
            data=part_data,
            name=name,
            content_id=part_headers.get("content-id", ""),
            content_location=part_headers.get("content-location", ""),
            charset=ct_params.get("charset", ""),
        ))
    return parts, pos


def _decode_pdu_inner(data: bytes, now: int) -> MmsPdu:
    pos = 0
    message_type = 0
    transaction_id = ""
    mms_version = None
    headers: dict = {}
    content_type = ""
    content_type_params: dict = {}
    parts: list = []

    while pos < len(data):
        code_byte = data[pos]
        if not (code_byte & 0x80):
            # Application-header: a custom textual field name (Token-text) + generic Value.
            name, pos = read_text_string(data, pos)
            raw, pos = _read_generic_value_bytes(data, pos)
            headers[name.lower()] = _decode_text_bytes(raw.rstrip(b"\x00"))
            continue

        code = code_byte & 0x7F
        pos += 1

        if code == _FIELD_CONTENT_TYPE:  # Content-Type is always the last header
            content_type, content_type_params, pos = _read_content_type_value(data, pos)
            headers["content-type"] = content_type
            break
        elif code == _FIELD_MESSAGE_TYPE:
            if pos >= len(data):
                raise MmsDecodeError("truncated message-type")
            message_type = data[pos]  # raw octet -- see module docstring
            pos += 1
        elif code == 0x18:  # Transaction-Id
            transaction_id, pos = read_text_string(data, pos)
        elif code == _FIELD_MMS_VERSION:
            v, pos = read_short_integer(data, pos)  # genuinely major<<4|minor, masking applies
            mms_version = (v >> 4, v & 0x0F)
        elif code == _FIELD_FROM:
            length, pos = read_value_length(data, pos)
            end = pos + length
            if end > len(data):
                raise MmsDecodeError("truncated from")
            if pos < end and data[pos] == 0x81:  # Insert-address-token
                headers["from"] = ""
            else:
                addr, _ = read_encoded_string_value(data, pos + 1)
                headers["from"] = normalize_address(addr)
            pos = end
        elif code in (0x01, 0x02, 0x17):  # Bcc, Cc, To
            addr, pos = read_encoded_string_value(data, pos)
            key = {0x01: "bcc", 0x02: "cc", 0x17: "to"}[code]
            headers.setdefault(key, []).append(normalize_address(addr))
        elif code == 0x16:  # Subject
            headers["subject"], pos = read_encoded_string_value(data, pos)
        elif code == 0x0B:  # Message-ID
            headers["message-id"], pos = read_text_string(data, pos)
        elif code == 0x03:  # Content-Location (top-level, e.g. m-notification-ind)
            headers["content-location"], pos = read_text_string(data, pos)
        elif code == 0x05:  # Date
            headers["date"], pos = read_long_integer(data, pos)
        elif code in (0x08, 0x07):  # Expiry, Delivery-Time
            key = "expiry" if code == 0x08 else "delivery-time"
            headers[key], pos = _read_expiry_like(data, pos, now)
        elif code == _FIELD_MESSAGE_SIZE:
            headers["message-size"], pos = read_long_integer(data, pos)
        elif code == 0x0A:  # Message-Class
            if pos >= len(data):
                raise MmsDecodeError("truncated message-class")
            if data[pos] & 0x80:
                headers["message-class"] = _MESSAGE_CLASS_BY_BYTE.get(data[pos], "personal")
                pos += 1
            else:
                headers["message-class"], pos = read_text_string(data, pos)
        elif code in (0x06, 0x10, 0x11):  # Delivery-Report, Read-Report, Report-Allowed
            if pos >= len(data):
                raise MmsDecodeError("truncated boolean header")
            key = {0x06: "delivery-report", 0x10: "read-report", 0x11: "report-allowed"}[code]
            headers[key] = data[pos] == 0x80
            pos += 1
        elif code == 0x12:  # Response-Status
            if pos >= len(data):
                raise MmsDecodeError("truncated response-status")
            headers["response-status"] = data[pos]
            pos += 1
        elif code == 0x13:  # Response-Text
            headers["response-text"], pos = read_encoded_string_value(data, pos)
        elif code == 0x14:  # Sender-Visibility
            if pos >= len(data):
                raise MmsDecodeError("truncated sender-visibility")
            headers["sender-visibility"] = data[pos]
            pos += 1
        elif code == 0x15:  # Status
            if pos >= len(data):
                raise MmsDecodeError("truncated status")
            headers["status"] = data[pos]
            pos += 1
        elif code == 0x19:  # Retrieve-Status
            if pos >= len(data):
                raise MmsDecodeError("truncated retrieve-status")
            headers["retrieve-status"] = data[pos]
            pos += 1
        elif code == 0x1A:  # Retrieve-Text
            headers["retrieve-text"], pos = read_encoded_string_value(data, pos)
        elif code == 0x1B:  # Read-Status
            if pos >= len(data):
                raise MmsDecodeError("truncated read-status")
            headers["read-status"] = data[pos]
            pos += 1
        elif code == 0x0F:  # Priority
            if pos >= len(data):
                raise MmsDecodeError("truncated priority")
            headers["priority"] = data[pos]
            pos += 1
        else:
            # Every other field code up to 0x3F (Reply-Charging*, Previously-Sent-*, and
            # anything newer than this module knows about): skip via the generic grammar
            # rather than aborting the whole decode over one header we don't act on.
            _raw, pos = _read_generic_value_bytes(data, pos)

    if content_type:
        if content_type.startswith("application/vnd.wap.multipart"):
            parts, _pos = _decode_multipart(data, pos, len(data))
        elif pos < len(data):
            body = bytes(data[pos:])
            parts = [MmsPart(
                content_type=content_type,
                data=body,
                name=content_type_params.get("name", ""),
                charset=content_type_params.get("charset", ""),
            )]

    return MmsPdu(
        message_type=message_type,
        transaction_id=transaction_id,
        mms_version=mms_version,
        headers=headers,
        content_type=content_type,
        content_type_params=content_type_params,
        parts=parts,
    )


def decode_pdu(data: bytes, *, now: int | None = None) -> MmsPdu:
    """Decode any MMS PDU (OMA-TS-MMS_ENC 1.2/1.3): m-notification-ind, m-retrieve-conf,
    m-send-req/conf, m-delivery-ind, m-read-orig/rec-ind. ``now`` anchors a relative
    Expiry/Delivery-Time; defaults to the current time."""
    resolved_now = int(time.time()) if now is None else now
    try:
        return _decode_pdu_inner(bytes(data), resolved_now)
    except MmsDecodeError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise MmsDecodeError(f"malformed MMS PDU: {exc}") from exc


# ---------------------------------------------------------------------------------------
# Encoding: the handful of PDUs this gateway originates.
# ---------------------------------------------------------------------------------------

def encode_notifyresp_ind(transaction_id: str, status: int, *,
                           report_allowed: bool = True, version: tuple[int, int] = (1, 2)) -> bytes:
    """X-Mms-Message-Type(0x8C M_NOTIFYRESP_IND), X-Mms-Transaction-ID(0x98 text),
    X-Mms-MMS-Version(0x8D major<<4|minor|0x80), X-Mms-Status(0x95 status),
    X-Mms-Report-Allowed(0x91 yes/no)."""
    out = bytearray()
    out += bytes([0x8C, M_NOTIFYRESP_IND])
    out += bytes([0x98]) + write_text_string(transaction_id)
    out += bytes([0x8D, 0x80 | ((version[0] << 4) | version[1])])
    out += bytes([0x95, status])
    out += bytes([0x91, 0x80 if report_allowed else 0x81])
    return bytes(out)


def encode_acknowledge_ind(transaction_id: str, *,
                            report_allowed: bool = True, version: tuple[int, int] = (1, 2)) -> bytes:
    """X-Mms-Message-Type(0x8C M_ACKNOWLEDGE_IND), X-Mms-Transaction-ID, X-Mms-MMS-Version,
    X-Mms-Report-Allowed."""
    out = bytearray()
    out += bytes([0x8C, M_ACKNOWLEDGE_IND])
    out += bytes([0x98]) + write_text_string(transaction_id)
    out += bytes([0x8D, 0x80 | ((version[0] << 4) | version[1])])
    out += bytes([0x91, 0x80 if report_allowed else 0x81])
    return bytes(out)


def _encode_part_content_type(part: MmsPart) -> bytes:
    code = _CONTENT_TYPE_TO_CODE.get(part.content_type.lower())
    media = bytes([0x80 | code]) if code is not None else write_text_string(part.content_type)
    params = bytearray()
    if part.name:
        params += bytes([0x85]) + write_text_string(part.name)  # Name (v1.1 code 0x05)
    if part.content_type.startswith("text/") or part.content_type == "application/smil":
        params += bytes([0x81]) + write_short_integer(charset_mib(part.charset or "utf-8"))
    if not params:
        return media  # Constrained-media: no parameters, so no Value-length wrapper needed
    value = media + bytes(params)
    return write_value_length(len(value)) + value


def encode_send_req(*, transaction_id: str, to: list, parts: list, subject: str = "",
                     from_insert: bool = True, delivery_report: bool = True,
                     read_report: bool = False, message_class: str = "personal",
                     expiry_seconds: int | None = None,
                     version: tuple[int, int] = (1, 2)) -> bytes:
    """Build an m-send-req. Header order matters (OMA-TS-MMS_ENC 7.2.1 lists Message-Type,
    Transaction-ID and MMS-Version as required to come first in practice, and Content-Type
    is defined to always be the LAST header since everything after it is the body)."""
    out = bytearray()
    out += bytes([0x8C, M_SEND_REQ])
    out += bytes([0x98]) + write_text_string(transaction_id)
    out += bytes([0x8D, 0x80 | ((version[0] << 4) | version[1])])

    if from_insert:
        out += bytes([0x89, 0x01, 0x81])  # From: Value-length(1), Insert-address-token

    for addr in to:
        out += bytes([0x97]) + write_encoded_string_value(encode_address(addr))

    if subject:
        out += bytes([0x96]) + write_encoded_string_value(subject)

    out += bytes([0x8A])  # Message-Class
    class_byte = _MESSAGE_CLASS_TO_BYTE.get(message_class)
    out += bytes([class_byte]) if class_byte is not None else write_text_string(message_class)

    out += bytes([0x86, 0x80 if delivery_report else 0x81])
    out += bytes([0x90, 0x80 if read_report else 0x81])

    if expiry_seconds is not None:
        delta = write_long_integer(int(expiry_seconds))
        out += bytes([0x88]) + write_value_length(1 + len(delta)) + bytes([0x81]) + delta

    smil_part = next((p for p in parts if p.content_type == "application/smil"), None)
    if smil_part is not None:
        related_params = bytearray()
        related_params += bytes([0x89]) + write_text_string("application/smil")  # Type
        related_params += bytes([0x8A]) + write_text_string(f"<{smil_part.content_id or 'smil'}>")  # Start
        ct_value = bytes([0x80 | _CONTENT_TYPE_TO_CODE["application/vnd.wap.multipart.related"]]) \
            + bytes(related_params)
    else:
        ct_value = bytes([0x80 | _CONTENT_TYPE_TO_CODE["application/vnd.wap.multipart.mixed"]])
    out += bytes([0x84]) + write_value_length(len(ct_value)) + ct_value

    body = bytearray()
    body += write_uintvar(len(parts))
    for part in parts:
        ct_bytes = _encode_part_content_type(part)
        location = part.content_location or part.name or "part"
        loc_header = bytes([0x8E]) + write_text_string(location)
        cid = part.content_id or location
        cid_header = bytes([0xC0]) + write_quoted_string(f"<{cid}>")
        part_headers = ct_bytes + loc_header + cid_header
        body += write_uintvar(len(part_headers))
        body += write_uintvar(len(part.data))
        body += part_headers
        body += part.data
    out += bytes(body)
    return bytes(out)


# SMIL (OMA MMS Conformance Document, "SMIL for MMS"): one <par> per slide, the regions
# named "Image" and "Text" that handsets expect, and every part referenced by its
# Content-Location. The conformance document allows that or a "cid:" URL; handsets observed on
# a live line (iPhone through the carrier's MMSC) reference by location, so this does too --
# with locations assign_references() made unique and ASCII, never the raw file name.
SMIL_CONTENT_ID = "smil"
# How long a slide with no timed media (a picture, some text) stays up; what phones use.
SLIDE_MS = 5000
_SMIL_ELEMENTS = (("image/", "img"), ("video/", "video"), ("audio/", "audio"),
                  ("text/plain", "text"))
_SMIL_REGIONS = {"img": "Image", "video": "Image", "text": "Text"}
_REFERENCE_SAFE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                            "0123456789._-")


def _smil_element(part: MmsPart) -> str:
    content_type = part.content_type.lower()
    for prefix, element in _SMIL_ELEMENTS:
        if content_type.startswith(prefix):
            return element
    return "ref"  # a contact or calendar card: presented as an attachment


def _bare_content_id(value: str) -> str:
    return str(value or "").strip().strip("<>").strip()


def assign_references(parts: list, *, reserved: tuple = (SMIL_CONTENT_ID,)) -> list:
    """Give every part a Content-ID and a Content-Location unique within the message.

    Both are plain ASCII tokens: a part keeps its own when it is already safe and unused, and
    otherwise gets "partN" (with the file's extension for the location). Parts are updated in
    place and returned, so a SMIL built afterwards can reference each one unambiguously -- two
    attachments that were both called photo.jpg included."""
    ids, locations = {r.lower() for r in reserved}, {f"{r}.xml".lower() for r in reserved}
    for index, part in enumerate(parts, 1):
        cid = _bare_content_id(part.content_id)
        if not cid or cid.lower() in ids or not set(cid) <= _REFERENCE_SAFE:
            cid = f"part{index}"
            while cid.lower() in ids:
                cid = f"part{index}-{len(ids)}"
        ids.add(cid.lower())
        part.content_id = cid
        location = str(part.content_location or part.name or "").replace("\\", "/")
        location = location.rsplit("/", 1)[-1].strip()
        if not location or location.lower() in locations or len(location) > 64 \
                or not set(location) <= _REFERENCE_SAFE or location.startswith("."):
            stem, dot, extension = location.rpartition(".")
            extension = extension if dot and stem and set(extension) <= _REFERENCE_SAFE \
                and 0 < len(extension) <= 8 else ""
            base = cid if set(cid) <= _REFERENCE_SAFE else f"part{index}"
            location = f"{base}.{extension}" if extension else base
            while location.lower() in locations:
                location = f"part{index}-{len(locations)}" + (f".{extension}" if extension else "")
        locations.add(location.lower())
        part.content_location = location
    return parts


def build_smil(parts: list) -> MmsPart:
    """The SMIL presentation for `parts`, which must already carry unique Content-IDs and
    Content-Locations (see assign_references).

    Each picture, video, sound or card is its own slide; a message's text joins the first
    slide so a photo and its caption show together, as phones compose them. A slide with a
    video or sound lasts as long as that media when its duration is known (MmsPart.duration_ms)
    and otherwise carries no dur at all, so the player lets it run to its end instead of
    cutting it off; a slide with only a picture or text lasts SLIDE_MS. Regions exist only
    for what is shown: an "Image" region when a picture or video is, a "Text" region when
    text is. The document is built with an XML library, so any name or ID is escaped."""
    import xml.etree.ElementTree as ET

    slides: list[list[MmsPart]] = []
    texts = []
    for part in parts:
        if part.content_type.lower() == "application/smil":
            continue
        if _smil_element(part) == "text":
            texts.append(part)
        else:
            slides.append([part])
    if texts:
        if slides:
            slides[0].append(texts[0])
        else:
            slides.append([texts[0]])
        slides.extend([t] for t in texts[1:])

    elements = {_smil_element(p) for slide in slides for p in slide}
    visual, textual = bool(elements & {"img", "video"}), "text" in elements
    smil = ET.Element("smil")
    layout = ET.SubElement(ET.SubElement(smil, "head"), "layout")
    ET.SubElement(layout, "root-layout")
    if visual:
        ET.SubElement(layout, "region", id="Image", top="0%", left="0%", width="100%",
                      height="80%" if textual else "100%", fit="meet")
    if textual:
        ET.SubElement(layout, "region", id="Text", top="80%" if visual else "0%", left="0%",
                      width="100%", height="20%" if visual else "100%")
    body = ET.SubElement(smil, "body")
    for slide in slides:
        par = ET.SubElement(body, "par")
        timed = [p for p in slide if _smil_element(p) in ("audio", "video")]
        durations = [p.duration_ms for p in timed]
        if not timed:
            par.set("dur", f"{SLIDE_MS}ms")
        elif all(d and d > 0 for d in durations):
            par.set("dur", f"{max(int(d) for d in durations)}ms")
        for part in slide:
            element = _smil_element(part)
            node = ET.SubElement(par, element, src=part.content_location)
            if element in _SMIL_REGIONS:
                node.set("region", _SMIL_REGIONS[element])
    smil_part = MmsPart(
        content_type="application/smil",
        data=ET.tostring(smil, encoding="unicode").encode("utf-8"),
        name="smil.xml",
        content_id=SMIL_CONTENT_ID,
        content_location="smil.xml",
        charset="utf-8",
    )
    check_smil(smil_part, parts)
    return smil_part


def check_smil(smil_part: MmsPart, parts: list) -> None:
    """Raise ValueError unless the SMIL parses and every region and src it uses exists:
    each region named by a media element is declared, and each src resolves to exactly one
    of `parts` by Content-ID ("cid:") or, failing that, by Content-Location."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(smil_part.data)
    except ET.ParseError as exc:
        raise ValueError(f"the SMIL presentation is not valid XML: {exc}") from None
    regions = {r.get("id") for r in root.iter("region")}
    by_id: dict[str, int] = {}
    by_location: dict[str, int] = {}
    for part in parts:
        if part is smil_part:
            continue
        cid = _bare_content_id(part.content_id)
        by_id[cid] = by_id.get(cid, 0) + 1
        by_location[part.content_location] = by_location.get(part.content_location, 0) + 1
    for node in root.iter():
        region = node.get("region")
        if region is not None and region not in regions:
            raise ValueError(f"the SMIL presentation uses the undeclared region {region!r}")
        src = node.get("src")
        if src is None:
            continue
        matches = by_id.get(src[4:], 0) if src.startswith("cid:") else by_location.get(src, 0)
        if matches != 1:
            raise ValueError(f"the SMIL presentation references {src!r}, which "
                             f"{'no part' if not matches else 'more than one part'} has")
