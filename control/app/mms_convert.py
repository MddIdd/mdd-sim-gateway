"""Converting and shrinking MMS attachments on the gateway.

Every client -- the WebUI, a script calling the API, later a SIP user agent -- hands the
gateway the file as the user picked it; the gateway turns it into something the carrier and the
recipient's phone accept, within the line's size limit. Doing it here rather than in a browser
means one implementation, the same result for every client, and the original kept at hand
while a message is composed so each re-fit starts from full quality (see mms.fit_attachments).

Converters are registered per kind of media (mms_media.MediaFormat.kind). A converter says
which types it can take, whether a given file can be made smaller ("adjustable"), and fits one
file into a byte budget. Only pictures have one today. A video converter -- re-encoding to
H.264/AAC in MP4 or 3GP at a lower bitrate, e.g. with ffmpeg -- slots in as CONVERTERS["video"]
with the same three methods; the planner, the staging API and the WebUI already treat every
adjustable attachment alike, and the capability table offers a "convert" format as soon as a
converter takes it.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - the control requirements install Pillow
    Image = ImageOps = None
try:
    import pillow_heif
except ImportError:  # pragma: no cover - HEIC/HEIF is then simply not convertible
    pillow_heif = None
else:
    pillow_heif.register_heif_opener()

if Image is not None:
    # A picture this large is not a photo anyone means to send by MMS; refusing it also keeps a
    # crafted file from making the decoder allocate gigabytes.
    Image.MAX_IMAGE_PIXELS = 64_000_000


class ConversionError(ValueError):
    """The file cannot be converted, or not made small enough; the message says why."""


@dataclass
class Fitted:
    content_type: str
    data: bytes
    width: int | None = None
    height: int | None = None
    converted: bool = False     # re-encoded, as opposed to passed through unchanged


# Longest edges tried from largest to smallest; about what phones themselves send by MMS. At
# each size the highest JPEG quality that fits is searched for, and a size is given up once
# even its quality floor is too big -- higher for large sizes, so a tight budget buys a smaller
# clean picture rather than a big blocky one.
IMAGE_EDGES = (1600, 1280, 1024, 800, 640, 480, 320, 240)
MAX_QUALITY, MIN_QUALITY, LAST_RESORT_QUALITY = 90, 60, 40
QUALITY_STEPS = 6
# Sent as they are when they fit and are no larger than the first edge; anything else that can
# be decoded goes out as baseline JPEG, the one picture type every MMS phone shows.
PASS_THROUGH_IMAGES = ("image/jpeg", "image/png", "image/gif")


class ImageConverter:
    kind = "image"

    def can_convert(self, content_type: str) -> bool:
        if Image is None:
            return False
        if content_type in ("image/heic", "image/heif"):
            return pillow_heif is not None
        return content_type in ("image/jpeg", "image/png", "image/gif", "image/webp",
                                "image/bmp", "image/avif")

    def _open(self, data: bytes, *, longest: int | None = None):
        """The decoded picture. With `longest`, a JPEG is decoded straight at the smallest
        scale that still covers that edge (DCT scaling), which is several times faster for a
        camera photo that is going to be shrunk anyway."""
        try:
            image = Image.open(io.BytesIO(data))
            width, height = image.size
            if longest and image.format == "JPEG" and max(width, height) > longest:
                scale = longest / max(width, height)
                image.draft("RGB", (int(width * scale) + 1, int(height * scale) + 1))
            image.load()
        except Image.DecompressionBombError:
            raise ConversionError("the picture is too large to convert") from None
        except Exception as exc:  # noqa: BLE001 -- any decoder failure means "unreadable"
            raise ConversionError(f"the picture could not be read ({exc})") from None
        return image

    @staticmethod
    def _size(data: bytes) -> tuple[int, int, int]:
        """The picture's own size as shown and its EXIF orientation, read from its header
        without decoding it."""
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            orientation = image.getexif().get(0x0112, 1)
        if orientation in (5, 6, 7, 8):
            width, height = height, width
        return width, height, orientation

    def adjustable(self, content_type: str, data: bytes) -> bool:
        """Whether this picture can be made smaller: an animated GIF cannot without losing
        its animation, so it is sent as it is or not at all."""
        if not self.can_convert(content_type):
            return False
        if content_type != "image/gif":
            return True
        return not getattr(self._open(data), "is_animated", False)

    @staticmethod
    def _flatten(image):
        image = ImageOps.exif_transpose(image)
        if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and
                                                  "transparency" in image.info):
            image = image.convert("RGBA")
            # JPEG has no alpha: paint transparent areas white rather than black.
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.getchannel("A"))
            return background
        return image.convert("RGB")

    @staticmethod
    def _encode(image, quality: int) -> bytes:
        out = io.BytesIO()
        # Baseline (not progressive) JPEG with no metadata: what older handsets decode, and
        # nothing of the camera's EXIF -- location included -- leaves with the picture.
        image.save(out, "JPEG", quality=int(quality), optimize=True, progressive=False)
        return out.getvalue()

    def fit(self, content_type: str, data: bytes, target: int, *,
            force: bool = False) -> Fitted:
        """`data` as a picture of at most `target` bytes: unchanged when it already is one a
        phone shows, fits and is no larger than IMAGE_EDGES[0] (and `force` is not set);
        otherwise the largest size, then the highest quality, whose JPEG fits."""
        image = self._open(data, longest=IMAGE_EDGES[0])
        width, height, orientation = self._size(data)
        if content_type == "image/gif" and getattr(image, "is_animated", False):
            if len(data) <= target:
                return Fitted(content_type, data, width, height)
            raise ConversionError("an animated GIF cannot be made smaller without losing its "
                                  "animation")
        longest = max(width, height)
        if not force and content_type in PASS_THROUGH_IMAGES and longest <= IMAGE_EDGES[0] \
                and orientation == 1:
            # Sent as it is, apart from its metadata: a phone photo's EXIF carries where it
            # was taken. (A rotated one is re-encoded instead: dropping its EXIF would drop
            # the rotation with it.)
            clean = strip_metadata(content_type, data)
            if clean is not None and len(clean) <= target:
                return Fitted(content_type, clean, width, height)
        if target <= 0:
            raise ConversionError("there is no room left for this picture")
        base = self._flatten(image)
        longest = max(base.size)           # after any decoding scale, still >= the first edge
        edges = [e for e in IMAGE_EDGES if e < longest]
        edges.insert(0, min(longest, IMAGE_EDGES[0]))
        for edge in dict.fromkeys(edges):
            scaled = base.copy()
            if max(scaled.size) > edge:
                scaled.thumbnail((edge, edge), Image.LANCZOS)
            floor_quality = MIN_QUALITY if edge > 640 else LAST_RESORT_QUALITY
            best = self._encode(scaled, floor_quality)
            if len(best) > target:
                continue
            top = self._encode(scaled, MAX_QUALITY)
            if len(top) <= target:
                best = top
            else:
                low, high = floor_quality, MAX_QUALITY
                for _ in range(QUALITY_STEPS):
                    middle = (low + high) // 2
                    if middle in (low, high):
                        break
                    candidate = self._encode(scaled, middle)
                    if len(candidate) <= target:
                        best, low = candidate, middle
                    else:
                        high = middle
            return Fitted("image/jpeg", best, *scaled.size, converted=True)
        raise ConversionError(f"the picture cannot be made smaller than {target // 1024 + 1} KB")


# JPEG segments and PNG chunks that describe a picture rather than draw it: EXIF (camera,
# time, location), XMP, IPTC, and PNG text. Removing them changes no pixel.
_JPEG_METADATA_MARKERS = {0xE1, 0xED}          # APP1 (EXIF, XMP), APP13 (IPTC)
_PNG_METADATA_CHUNKS = {b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"tIME"}


def _strip_jpeg(data: bytes) -> bytes | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    out, pos = bytearray(data[:2]), 2
    while pos + 2 <= len(data):
        if data[pos] != 0xFF:
            return None
        marker = data[pos + 1]
        if marker == 0xFF:                      # fill byte before a marker
            pos += 1
            continue
        if marker == 0xDA or marker == 0xD9:    # start of scan: the rest is image data
            return bytes(out + data[pos:])
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            out += data[pos:pos + 2]
            pos += 2
            continue
        if pos + 4 > len(data):
            return None
        end = pos + 2 + int.from_bytes(data[pos + 2:pos + 4], "big")
        if end > len(data):
            return None
        if marker not in _JPEG_METADATA_MARKERS:
            out += data[pos:end]
        pos = end
    return None


def _strip_png(data: bytes) -> bytes | None:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    out, pos = bytearray(data[:8]), 8
    while pos + 12 <= len(data):
        end = pos + 12 + int.from_bytes(data[pos:pos + 4], "big")
        if end > len(data):
            return None
        kind = data[pos + 4:pos + 8]
        if kind not in _PNG_METADATA_CHUNKS:
            out += data[pos:end]
        pos = end
        if kind == b"IEND":
            return bytes(out)
    return None


def strip_metadata(content_type: str, data: bytes) -> bytes | None:
    """`data` without its descriptive metadata, losslessly; the bytes unchanged for a type
    that carries none worth removing (GIF); None when the file's structure is not what it
    should be, so the caller re-encodes it instead."""
    if content_type == "image/jpeg":
        return _strip_jpeg(data)
    if content_type == "image/png":
        return _strip_png(data)
    return data


CONVERTERS = {"image": ImageConverter()}


def converter_for(kind: str, content_type: str):
    """The converter that takes this kind and type, or None."""
    converter = CONVERTERS.get(kind)
    return converter if converter is not None and converter.can_convert(content_type) else None
