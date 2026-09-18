"""Attachments uploaded while an MMS is being composed.

A client uploads each file as the user adds it, so the gateway can check it, convert it and
tell the user straight away what it will cost (mms.fit_attachments). The original is kept
here until the message is sent or the attachment removed: every re-fit -- another attachment
added, one removed -- starts again from full quality rather than from an earlier, smaller
encoding. Once sent, only what was sent is stored with the message; the original goes.

Uploads live under <data>/mms-staging/<line>/<id>/ (the original, its metadata and the most
recent fitted version for previews) and are removed by sweep() a day after their last use.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time

from . import mms_media, store

MAX_PER_LINE = 20
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


def list_ids(instance: str) -> list[str]:
    base = os.path.join(_root(), str(instance))
    if not _LINE.match(str(instance)) or not os.path.isdir(base):
        return []
    return [name for name in os.listdir(base) if _ID.match(name)]


def stage(instance: str, name: str, content_type: str, data: bytes) -> dict:
    """Keep one uploaded original; returns its public record. Raises OverflowError when the
    line already holds MAX_PER_LINE uploads."""
    with _lock:
        if len(list_ids(instance)) >= MAX_PER_LINE:
            raise OverflowError(f"at most {MAX_PER_LINE} attachments can be waiting to be sent")
        attachment_id = os.urandom(8).hex()
        directory = _directory(instance, attachment_id)
        os.makedirs(directory, mode=0o700)
    meta = {"id": attachment_id, "name": mms_media.display_name(name, content_type),
            "content_type": mms_media.base_type(content_type), "size": len(data),
            "created": int(time.time())}
    _write(os.path.join(directory, "original"), bytes(data))
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


def save_fitted(instance: str, attachment_id: str, content_type: str, data: bytes) -> None:
    """Keep the latest fitted version, for the composer's preview."""
    directory = _directory(instance, attachment_id)
    if not os.path.isdir(directory):
        return
    _write(os.path.join(directory, "fitted"), bytes(data))
    meta = _meta(directory)
    meta["fitted_type"] = content_type
    _write(os.path.join(directory, "meta.json"), json.dumps(meta).encode("utf-8"))


def preview_file(instance: str, attachment_id: str) -> tuple[str, str] | None:
    """(path, content type) of what the composer should show: the fitted version once there
    is one, else the original."""
    try:
        directory = _directory(instance, attachment_id)
        meta = _meta(directory)
    except (KeyError, OSError, ValueError):
        return None
    fitted = os.path.join(directory, "fitted")
    if meta.get("fitted_type") and os.path.isfile(fitted):
        return fitted, meta["fitted_type"]
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
