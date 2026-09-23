"""Attachments uploaded while an MMS is being composed.

A client uploads each file as the user adds it, so the gateway can check it, convert it and
tell the user straight away what it will cost (mms.fit_attachments). The original is kept
here until the message is sent or the attachment removed: every re-fit -- another attachment
added, one removed -- starts again from full quality rather than from an earlier, smaller
encoding. Once sent, only what was sent is stored with the message; the original goes.

Uploads live under <data>/mms-staging/<line>/<id>/ (the original, its metadata and the last
few fitted versions for previews) and are removed by sweep() a day after their last use.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time

from . import mms_media, store

MAX_PER_LINE = 20
# A count is not a disk budget: MAX_PER_LINE uploads of the largest file the API accepts are
# half a gigabyte, on a line, on an appliance whose disk is often an SD card. These are the
# bytes the staging area may hold -- about twenty phone photos for one line, and a ceiling for
# the gateway, which has as many lines as it has modems and only the one disk.
MAX_BYTES_PER_LINE = 64 * 1024 * 1024
MAX_BYTES_TOTAL = 256 * 1024 * 1024
TTL_SECONDS = 24 * 3600
_ID = re.compile(r"^[0-9a-f]{16}$")
_LINE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_lock = threading.Lock()


def _root() -> str:
    return os.path.join(store.DATA_DIR, "mms-staging")


def _directory(instance: str, attachment_id: str) -> str:
    if not _LINE.match(str(instance)) or not _ID.match(str(attachment_id)):
        raise KeyError(attachment_id)
    return os.path.join(_root(), str(instance), str(attachment_id))


def _write(path: str, data: bytes) -> None:
    temporary = f"{path}.tmp"
    with open(temporary, "wb") as handle:
        handle.write(data)
    os.replace(temporary, path)


def _meta(directory: str) -> dict:
    with open(os.path.join(directory, "meta.json"), encoding="utf-8") as handle:
        return json.load(handle)


def _bytes_under(path: str) -> int:
    total = 0
    for root, _directories, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:      # swept between the walk and the stat
                continue
    return total


def usage(instance: str | None = None) -> int:
    """Bytes held for one line, or for the whole staging area."""
    base = _root() if instance is None else os.path.join(_root(), str(instance))
    return _bytes_under(base) if os.path.isdir(base) else 0


def list_ids(instance: str) -> list[str]:
    base = os.path.join(_root(), str(instance))
    if not _LINE.match(str(instance)) or not os.path.isdir(base):
        return []
    return [name for name in os.listdir(base) if _ID.match(name)]


def _room_for(instance: str, size: int) -> None:
    """Raise OverflowError unless this line, and the gateway, can still hold `size` bytes."""
    if len(list_ids(instance)) >= MAX_PER_LINE:
        raise OverflowError(f"at most {MAX_PER_LINE} attachments can be waiting to be sent")
    if usage(instance) + size > MAX_BYTES_PER_LINE:
        raise OverflowError(f"this line is already holding close to "
                            f"{MAX_BYTES_PER_LINE // (1024 * 1024)} MB of attachments waiting "
                            f"to be sent; send or remove some first")
    if usage() + size > MAX_BYTES_TOTAL:
        raise OverflowError(f"the gateway is already holding close to "
                            f"{MAX_BYTES_TOTAL // (1024 * 1024)} MB of attachments waiting to "
                            f"be sent; send or remove some first")


def stage(instance: str, name: str, content_type: str, data: bytes) -> dict:
    """Keep one uploaded original; returns its public record. Raises OverflowError when the
    line already holds MAX_PER_LINE uploads, or when the line or the gateway has no room for
    it."""
    data = bytes(data)
    meta = {"name": mms_media.display_name(name, content_type),
            "content_type": mms_media.base_type(content_type), "size": len(data),
            "created": int(time.time())}
    # The whole upload is written under the lock, so what the next one measures is what is
    # really there: a budget checked before the previous write finished is not a budget. A
    # draft abandoned in a closed tab is swept before the room is declared full, so a gateway
    # left at the ceiling yesterday still takes an attachment today.
    with _lock:
        try:
            _room_for(instance, len(data))
        except OverflowError:
            if not sweep():
                raise
            _room_for(instance, len(data))
        meta["id"] = attachment_id = os.urandom(8).hex()
        directory = _directory(instance, attachment_id)
        os.makedirs(directory, mode=0o700)
        _write(os.path.join(directory, "original"), data)
        _write(os.path.join(directory, "meta.json"), json.dumps(meta).encode("utf-8"))
    return meta


def load(instance: str, ids: list[str]) -> list[dict]:
    """The staged originals, in the order asked, as {id, name, content_type, data}. Raises
    KeyError for an id this line does not hold."""
    items = []
    for attachment_id in ids:
        directory = _directory(instance, attachment_id)
        try:
            meta = _meta(directory)
            with open(os.path.join(directory, "original"), "rb") as handle:
                data = handle.read()
        except (OSError, ValueError):
            raise KeyError(attachment_id) from None
        os.utime(directory)   # in use: keep it past the sweep
        items.append({**meta, "data": data})
    return items


# Fitted versions kept per upload. The same attachment fits differently when it shares a
# message with others and when it is sent on its own; the composer may switch between the two,
# and each preview must show the version its own fit produced.
FITTED_VERSIONS = 4


def save_fitted(instance: str, attachment_id: str, content_type: str, data: bytes) -> str:
    """Keep a fitted version for the composer's preview; returns its version token (a digest of
    the content), which preview_file() takes to serve exactly that version."""
    directory = _directory(instance, attachment_id)
    token = hashlib.sha256(bytes(data)).hexdigest()[:16]
    if not os.path.isdir(directory):
        return token
    with _lock:
        meta = _meta(directory)
        versions = [v for v in meta.get("fitted") or [] if v.get("token") != token]
        versions.append({"token": token, "content_type": content_type})
        _write(os.path.join(directory, f"fitted-{token}"), bytes(data))
        for old in versions[:-FITTED_VERSIONS]:
            try:
                os.remove(os.path.join(directory, f"fitted-{old['token']}"))
            except OSError:
                pass
        meta["fitted"] = versions[-FITTED_VERSIONS:]
        _write(os.path.join(directory, "meta.json"), json.dumps(meta).encode("utf-8"))
    return token


def preview_file(instance: str, attachment_id: str,
                 version: str | None = None) -> tuple[str, str] | None:
    """(path, content type) of what the composer should show: the fitted version named by
    `version`, else the most recent one, else the original."""
    try:
        directory = _directory(instance, attachment_id)
        meta = _meta(directory)
    except (KeyError, OSError, ValueError):
        return None
    versions = meta.get("fitted") or []
    chosen = next((v for v in versions if v.get("token") == version), None) \
        or (versions[-1] if versions and not version else None)
    if chosen:
        path = os.path.join(directory, f"fitted-{chosen['token']}")
        if os.path.isfile(path):
            return path, chosen["content_type"]
    if version:
        return None
    return os.path.join(directory, "original"), meta.get("content_type") or ""


def remove(instance: str, ids: list[str]) -> None:
    for attachment_id in ids:
        try:
            shutil.rmtree(_directory(instance, attachment_id), ignore_errors=True)
        except KeyError:
            continue


def sweep(now: float | None = None, ttl: int = TTL_SECONDS) -> int:
    """Remove uploads not used for `ttl` seconds -- a draft abandoned in a closed tab."""
    now = time.time() if now is None else now
    root = _root()
    removed = 0
    if not os.path.isdir(root):
        return 0
    for line in os.scandir(root):
        if not line.is_dir(follow_symlinks=False):
            continue
        for entry in os.scandir(line.path):
            try:
                if now - entry.stat(follow_symlinks=False).st_mtime > ttl:
                    shutil.rmtree(entry.path, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    return removed
