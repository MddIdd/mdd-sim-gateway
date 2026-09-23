"""What an MMS attachment may be, and whether a given file really is one.

One capability table serves every client that composes an MMS -- the WebUI today, a SIP user
agent submitting MMS through the gateway later -- and the gateway itself, which is what
decides: a client may filter ahead of time to spare the user a round trip, but nothing is sent
that check_attachment() has not accepted. Converting and shrinking happen on the gateway too
(mms_convert), so every client gets the same result.

Each format has a policy:
  send     sent as is once its content has been checked;
  convert  never sent as is: the gateway re-encodes it (see mms_convert) when it has a
           converter for it, and refuses it otherwise;
  receive  never sent; a copy that arrives is stored and offered for download.
Anything not in the table is treated like "receive".

The sendable set is deliberately narrow: the image, audio and video types of the OMA MMS
content classes up to Video Rich (JPEG, GIF, PNG; AMR, AMR-WB; H.263, MPEG-4 Visual and
H.264 in 3GPP/MP4 with AMR or AAC sound) plus MP3/AAC audio, plain text and vCard 2.1/3.0 /
vCalendar / iCalendar cards, which phones commonly accept. Checking is structural (magic
numbers, the codecs an MP4/3GP file declares, a card's VERSION), not a full decode.
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass

SEND, CONVERT, RECEIVE = "send", "convert", "receive"


@dataclass(frozen=True)
class MediaFormat:
    content_type: str          # as written on the wire
    kind: str                  # image | audio | video | text | contact | calendar
    extensions: tuple          # the first one names stored files
    policy: str
    preview: bool = False      # a browser can show it inline (it may still fail to play)
    aliases: tuple = ()        # other spellings clients and MMSCs use


FORMATS = (
    MediaFormat("image/jpeg", "image", ("jpg", "jpeg", "jpe"), SEND, True,
                ("image/jpg", "image/pjpeg")),
    MediaFormat("image/gif", "image", ("gif",), SEND, True),
    MediaFormat("image/png", "image", ("png",), SEND, True, ("image/x-png",)),
    MediaFormat("image/webp", "image", ("webp",), CONVERT, True),
    MediaFormat("image/bmp", "image", ("bmp",), CONVERT, True, ("image/x-ms-bmp",)),
    MediaFormat("image/heic", "image", ("heic",), CONVERT),
    MediaFormat("image/heif", "image", ("heif",), CONVERT),
    MediaFormat("image/avif", "image", ("avif",), CONVERT, True),
    MediaFormat("image/vnd.wap.wbmp", "image", ("wbmp",), RECEIVE),
    MediaFormat("audio/amr", "audio", ("amr",), SEND),
    MediaFormat("audio/amr-wb", "audio", ("awb",), SEND),
    MediaFormat("audio/mpeg", "audio", ("mp3",), SEND, True, ("audio/mp3", "audio/mpeg3")),
    MediaFormat("audio/mp4", "audio", ("m4a",), SEND, True, ("audio/x-m4a", "audio/m4a")),
    MediaFormat("audio/3gpp", "audio", ("3ga",), SEND),
    MediaFormat("audio/wav", "audio", ("wav",), RECEIVE, True, ("audio/x-wav", "audio/wave")),
    MediaFormat("video/3gpp", "video", ("3gp",), SEND),
    MediaFormat("video/mp4", "video", ("mp4", "m4v"), SEND, True),
    MediaFormat("video/quicktime", "video", ("mov",), RECEIVE),
    MediaFormat("text/plain", "text", ("txt",), SEND, True),
    MediaFormat("text/x-vCard", "contact", ("vcf",), SEND, False,
                ("text/vcard", "text/directory")),
    MediaFormat("text/x-vCalendar", "calendar", ("vcs",), SEND),
    MediaFormat("text/calendar", "calendar", ("ics",), SEND),
)
_BY_TYPE = {f.content_type.lower(): f for f in FORMATS}
_BY_TYPE.update({a.lower(): f for f in FORMATS for a in f.aliases})
_BY_EXTENSION: dict[str, MediaFormat] = {}
for _f in FORMATS:
    for _e in _f.extensions:
        _BY_EXTENSION.setdefault(_e, _f)
_GENERIC_TYPES = {"", "application/octet-stream", "binary/octet-stream",
                  "application/x-unknown"}


def base_type(content_type: str) -> str:
    return str(content_type or "").split(";")[0].strip().lower()


def lookup(content_type: str) -> MediaFormat | None:
    return _BY_TYPE.get(base_type(content_type))


def _extension_of(name: str) -> str:
    stem, dot, extension = str(name or "").rpartition(".")
    return extension.lower() if dot and stem and extension.isascii() \
        and extension.isalnum() and len(extension) <= 10 else ""


def file_extension(content_type: str) -> str:
    """The extension a stored file of this type gets; "bin" for anything unknown."""
    found = lookup(content_type)
    return found.extensions[0] if found else "bin"


def previewable(content_type: str) -> bool:
    found = lookup(content_type)
    return bool(found and found.preview)


def capabilities(can_convert=None) -> list[dict]:
    """The table as data, for clients choosing what to offer. "attachable" says whether this
    gateway takes the format from a client: a "send" format always, a "convert" one when
    `can_convert(kind, content_type)` says it has a converter for it."""
    return [{"content_type": f.content_type, "kind": f.kind, "extensions": list(f.extensions),
             "policy": f.policy, "preview": f.preview, "aliases": list(f.aliases),
             "attachable": f.policy == SEND or (f.policy == CONVERT and can_convert is not None
                                                and bool(can_convert(f.kind, f.content_type)))}
            for f in FORMATS]


# ------------------------------- content checks -------------------------------

@dataclass
class Checked:
    content_type: str = ""
    kind: str = ""
    duration_ms: int | None = None
    error: str | None = None
    policy: str = ""           # SEND, or CONVERT when the caller said it can convert it


def _sniff(data: bytes) -> str | None:
    """The binary format `data` starts like, as a canonical type; "iso" for any ISO base media
    (MP4/3GP/QuickTime/HEIF) file, which the box structure resolves further."""
    head = data[:16]
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"BM") and len(data) > 26 and int.from_bytes(data[2:6], "little") \
            in (0, len(data)):
        return "image/bmp"
    if head.startswith(b"#!AMR-WB\n"):
        return "audio/amr-wb"
    if head.startswith(b"#!AMR\n"):
        return "audio/amr"
    if data[4:8] == b"ftyp":
        return "iso"
    if head.startswith(b"ID3") or (len(head) > 2 and head[0] == 0xFF and head[1] & 0xE6 == 0xE2):
        return "audio/mpeg"  # an MPEG-1/2 layer III frame sync, or an ID3v2 tag before one
    return None


def _boxes(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size, header = int.from_bytes(data[pos + 8:pos + 16], "big"), 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return
        yield kind, pos + header, pos + size
        pos += size


def _child(data: bytes, start: int, end: int, kind: bytes):
    return next(((s, e) for k, s, e in _boxes(data, start, end) if k == kind), None)


# Sample entries of the codecs the sendable MP4/3GP formats may carry.
_VIDEO_CODECS = {b"s263": "H.263", b"h263": "H.263", b"mp4v": "MPEG-4 Visual",
                 b"avc1": "H.264", b"avc3": "H.264"}
_AUDIO_CODECS = {b"samr": "AMR", b"sawb": "AMR-WB", b"mp4a": "AAC"}
_CODEC_NAMES = {b"hvc1": "HEVC (H.265)", b"hev1": "HEVC (H.265)", b"av01": "AV1",
                b"vp09": "VP9", b"encv": "encrypted video", b"enca": "encrypted audio",
                b"ac-3": "AC-3", b"ec-3": "E-AC-3", b"Opus": "Opus", b"alac": "ALAC"}


def _inspect_iso(data: bytes) -> tuple[dict, str | None]:
    """{"brand", "video", "audio", "duration_ms"} of an ISO base media file, or an error."""
    ftyp = _child(data, 0, len(data), b"ftyp")
    brand = data[ftyp[0]:ftyp[0] + 4] if ftyp else b""
    info = {"brand": brand, "video": [], "audio": [], "duration_ms": None}
    moov = _child(data, 0, len(data), b"moov")
    if brand in (b"heic", b"heix", b"mif1", b"msf1", b"avif", b"avis"):
        info["image"] = b"avif" if brand in (b"avif", b"avis") else b"heic"
        return info, None
    if not moov:
        return info, "the file is incomplete (it has no movie header)"
    mvhd = _child(data, *moov, b"mvhd")
    if mvhd:
        # A file that stops mid-upload still has boxes; one of them can be a movie header with
        # nothing, or almost nothing, in it. Read a field only once the box is long enough to
        # hold it -- a header that stops short simply has no duration, which is what a file
        # without one gives the caller anyway.
        start, stop = mvhd
        version = data[start] if stop > start else None
        scale = length = 0
        if version == 1 and stop - start >= 32:
            scale = int.from_bytes(data[start + 20:start + 24], "big")
            length = int.from_bytes(data[start + 24:start + 32], "big")
        elif version == 0 and stop - start >= 20:
            scale = int.from_bytes(data[start + 12:start + 16], "big")
            length = int.from_bytes(data[start + 16:start + 20], "big")
        if scale and 0 < length < 2 ** 62:
            info["duration_ms"] = -(-length * 1000 // scale)
    for kind, start, end in _boxes(data, *moov):
        if kind != b"trak":
            continue
        mdia = _child(data, start, end, b"mdia")
        hdlr = mdia and _child(data, *mdia, b"hdlr")
        minf = mdia and _child(data, *mdia, b"minf")
        stbl = minf and _child(data, *minf, b"stbl")
        stsd = stbl and _child(data, *stbl, b"stsd")
        if not (hdlr and stsd):
            continue
        handler = data[hdlr[0] + 8:hdlr[0] + 12]
        entry = next(_boxes(data, stsd[0] + 8, stsd[1]), None)
        if entry is None:
            continue
        if handler == b"vide":
            info["video"].append(entry[0])
        elif handler == b"soun":
            info["audio"].append(entry[0])
    return info, None


_AMR_FRAME = {"audio/amr": (6, (12, 13, 15, 17, 19, 20, 26, 31, 5, 0, 0, 0, 0, 0, 0, 0)),
              "audio/amr-wb": (9, (17, 23, 32, 36, 40, 46, 50, 58, 60, 5, 0, 0, 0, 0, 0, 0))}


def _amr_duration(content_type: str, data: bytes) -> int | None:
    """Playing time of an AMR storage-format file (RFC 4867 section 5): 20 ms per frame."""
    pos, sizes = _AMR_FRAME[content_type]
    frames = 0
    while pos < len(data):
        frame_type = (data[pos] >> 3) & 0x0F
        if frame_type not in (*range(10), 15):
            return None
        pos += 1 + sizes[frame_type]
        frames += 1
    return frames * 20 or None


def _decode_text(data: bytes) -> str | None:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _card_version(text: str, begin: str) -> str | None:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines or lines[0].upper() != f"BEGIN:{begin}":
        return None
    for line in lines[1:]:
        key, _sep, value = line.partition(":")
        if key.strip().upper() == "VERSION":
            return value.strip()
    return ""


def _check_text(fmt: MediaFormat, data: bytes, label: str) -> Checked:
    text = _decode_text(data)
    if text is None:
        return Checked(error=f"{label} is not UTF-8 text")
    if fmt.kind == "text":
        return Checked(fmt.content_type, fmt.kind)
    if fmt.kind == "contact":
        version = _card_version(text, "VCARD")
        if version is None:
            return Checked(error=f"{label} is not a vCard")
        if version not in ("2.1", "3.0"):
            return Checked(error=f"{label} is a vCard {version or 'of unknown version'}; "
                                 "phones read vCard 2.1 or 3.0, so export it in one of those")
        return Checked(fmt.content_type, fmt.kind)
    version = _card_version(text, "VCALENDAR")
    if version is None:
        return Checked(error=f"{label} is not a calendar file")
    # The VERSION decides which of the two calendar formats it is, whatever it was called.
    canonical = "text/x-vCalendar" if version == "1.0" else "text/calendar"
    return Checked(canonical, "calendar")


def check_attachment(name: str, content_type: str, data: bytes, *,
                     can_convert=None) -> Checked:
    """Decide whether an attachment can be sent, from what it contains.

    The declared type (or, when it is missing or generic, the file extension) says what the
    client thinks it is; the content has the last word. A file whose content is a different
    kind of media than declared -- a video labelled as a picture -- is refused as mislabelled;
    within one kind the content's own type is used (a PNG sent as image/jpeg goes out as
    image/png). A "convert" format passes only when `can_convert(kind, content_type)` says the
    caller will convert it (policy CONVERT). Returns a Checked with the canonical content type
    and, for audio and video, the playing time when it can be read; or with `error` set."""
    label = str(name or "the attachment")
    declared = lookup(content_type)
    if declared is None and base_type(content_type) in _GENERIC_TYPES:
        declared = _BY_EXTENSION.get(_extension_of(name))
    data = bytes(data or b"")
    if not data:
        return Checked(error=f"{label} is empty")

    sniffed = _sniff(data)
    duration = None
    if sniffed == "iso":
        info, problem = _inspect_iso(data)
        if problem:
            return Checked(error=f"{label}: {problem}")
        if info.get("image"):
            sniffed = "image/avif" if info["image"] == b"avif" else "image/heic"
        else:
            unknown = [c for c in info["video"] if c not in _VIDEO_CODECS] + \
                [c for c in info["audio"] if c not in _AUDIO_CODECS]
            if unknown:
                codec = _CODEC_NAMES.get(unknown[0], unknown[0].decode("latin-1").strip())
                return Checked(error=f"{label} uses {codec}, which MMS phones cannot play; "
                                     "export it as H.264 video with AAC or AMR sound")
            if not info["video"] and not info["audio"]:
                return Checked(error=f"{label} has no audio or video track")
            three_gp = info["brand"].startswith((b"3gp", b"3g2", b"3gr", b"3gs"))
            if info["brand"] == b"qt  ":
                sniffed = "video/quicktime"
            elif info["video"]:
                sniffed = "video/3gpp" if three_gp else "video/mp4"
            else:
                sniffed = "audio/3gpp" if three_gp else "audio/mp4"
            duration = info["duration_ms"]
    elif sniffed in _AMR_FRAME:
        duration = _amr_duration(sniffed, data)

    if sniffed is None:
        if declared is None:
            return Checked(error=f"{label}: {base_type(content_type) or 'this type of file'} "
                                 "cannot be sent by MMS")
        if declared.kind not in ("text", "contact", "calendar"):
            return Checked(error=f"{label} does not contain {declared.content_type} data")
        checked = _check_text(declared, data, label)
        fmt = lookup(checked.content_type) if not checked.error else None
    else:
        fmt = _BY_TYPE[sniffed]
        media = ("audio", "video")
        if declared is not None and declared.kind != fmt.kind and \
                not (declared.kind in media and fmt.kind in media):
            return Checked(error=f"{label} is labelled {declared.content_type} but contains "
                                 f"{fmt.content_type} data")
        checked = Checked(fmt.content_type, fmt.kind, duration)
    if checked.error:
        return checked
    if fmt.policy == CONVERT:
        if can_convert is not None and can_convert(fmt.kind, fmt.content_type):
            checked.policy = CONVERT
            return checked
        return Checked(error=f"{label}: {fmt.content_type} is not sent by MMS as it is, and "
                             "this gateway cannot convert it; convert it to JPEG first")
    if fmt.policy != SEND:
        return Checked(error=f"{label}: {fmt.content_type} cannot be sent by MMS")
    checked.policy = SEND
    return checked


# ------------------------------- names -------------------------------

# Longest original name kept, in UTF-8 bytes: well under any file system's 255-byte limit
# and a header's reasonable length, with room left for the extension.
MAX_NAME_BYTES = 120
_UNSAFE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')


def _clip_utf8(text: str, limit: int) -> str:
    return text.encode("utf-8")[:max(0, limit)].decode("utf-8", errors="ignore")


def display_name(name: str, content_type: str = "", *, max_bytes: int = MAX_NAME_BYTES) -> str:
    """Return a safe, byte-bounded display/download name without losing its extension."""
    text = unicodedata.normalize("NFC", str(name or ""))
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
    text = _UNSAFE.sub("_", text).strip().lstrip(".").strip()
    extension = _extension_of(text)
    if not text or text == f".{extension}":
        return f"attachment.{file_extension(content_type)}"
    if len(text.encode("utf-8")) <= max_bytes:
        return text
    suffix = f".{extension}" if extension else ""
    stem = text[:-len(suffix)] if suffix else text
    return _clip_utf8(stem, max_bytes - len(suffix)).rstrip() + suffix


def storage_name(seq: int, content_type: str) -> str:
    """Return a fresh on-disk name that never derives from a sender-supplied name."""
    return f"{int(seq):02d}-{os.urandom(8).hex()}.{file_extension(content_type)}"
