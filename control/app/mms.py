"""Receive, retrieve and send MMS for a line.

An MMS arrives in two halves: a notification (m-notification-ind) carried by a WAP Push SMS,
and the message itself, fetched from the carrier's MMSC over HTTP on the carrier's MMS APN.
The notification can reach the gateway over VoWiFi and on the modem alike; both paths hand
the WAP Push payload to handle_wap_push(), which stores one pending MMS per MMSC location.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager

from . import cellular_sms, mms_convert, mms_media, mms_pdu, mms_transport, store

log = logging.getLogger("vowifi.mms")

# WDP destination port of the WAP Push connectionless session service (WAP-259-WDP).
WAP_PUSH_PORT = 2948
# Retrieval retries after a transient failure. An MMSC keeps a message for days, but a
# notification can be retried sooner than its expiry is worth waiting for.
RETRY_DELAYS = (60, 300, 900, 3600, 4 * 3600)
# One MMSC exchange at a time per modem: its AT port and its socket id are single-use, so
# two exchanges on one modem would trample each other. Different modems -- and the host --
# have nothing in common and run side by side.
_io_locks: dict[str, threading.Lock] = {}
_io_locks_guard = threading.Lock()


def io_lock(key: str) -> threading.Lock:
    with _io_locks_guard:
        return _io_locks.setdefault(str(key), threading.Lock())

def is_wap_push_udh(udh_hex: str) -> bool:
    """Whether an SMS User Data Header addresses the WAP Push port."""
    try:
        dest, _src = mms_pdu.extract_wdp_port(bytes.fromhex(str(udh_hex or "")))
    except ValueError:
        return False
    return dest == WAP_PUSH_PORT


def handle_wap_push(instance: str, sender: str, data: bytes, *, transport: str,
                    sent_ts: int | None = None, now: int | None = None) -> dict:
    """Consume one WAP Push payload addressed to the MMS user agent.

    Returns {"handled": False} when the payload is not an MMS push (the caller files it as a
    binary SMS). Otherwise "message" is a newly stored pending MMS (None for a notification
    already held), or "delivery" the outgoing MMS a delivery report updated.
    """
    result = store.apply_mms_push(instance, sender, data, transport=transport,
                                  sent_ts=sent_ts, now=now)
    if not result.get("handled"):
        if result.get("error"):
            log.info("undecodable MMS push on line %s: %s", instance, result["error"])
        return {"handled": False}
    if result["kind"] == "notification":
        rec = store.get_message(result["message_id"]) if result["message_id"] else None
        if rec:
            log.info("MMS notification on line %s from %s (%s bytes)", instance,
                     result["peer"], result["size"])
        return {"handled": True, "message": rec}
    if result["kind"] == "delivery":
        rec = store.get_message(result["message_id"]) if result["message_id"] else None
        return {"handled": True, "delivery": rec}
    # A read report or anything else the MMS user agent receives: nothing to show.
    return {"handled": True}


def _modem_for(inst: dict, runner=subprocess.run) -> str | None:
    iccid = cellular_sms._normalize_iccid(inst.get("iccid"))
    if not iccid:
        return None
    path, _problem = cellular_sms._find_modem(
        iccid, runner, 10.0, imsi=cellular_sms._normalize_imsi(inst.get("imsi")))
    return path


def _request_headers(settings: dict, content_type: str | None = None) -> dict:
    headers = {"Accept": f"{mms_transport.MMS_CONTENT_TYPE}, */*",
               "User-Agent": settings.get("user_agent") or mms_transport.DEFAULT_USER_AGENT}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def open_client(inst: dict, settings: dict, *, runner=subprocess.run):
    if not settings.get("enabled"):
        raise mms_transport.MmsTransportError("MMS is turned off for this line",
                                              retryable=False)
    if not settings.get("configured"):
        raise mms_transport.MmsTransportError(
            "no MMSC is known for this line's carrier; set it in the line's MMS settings",
            retryable=False)
    modem_path = _modem_for(inst, runner)
    client = mms_transport.client_for(settings, modem_path, runner=runner)
    return client, (modem_path if isinstance(client, mms_transport.ModemSocketHttp) else "host")


@contextmanager
def _exchange(inst: dict, settings: dict, client, runner):
    """The client for this line, held under its modem's lock for the whole exchange."""
    if client is not None:
        key = f"client:{id(client)}"
    else:
        client, key = open_client(inst, settings, runner=runner)
    with io_lock(key):
        yield client


def _parts_for_store(pdu: mms_pdu.MmsPdu) -> list[dict]:
    parts = []
    for part in pdu.parts:
        text = part.text() if part.content_type.startswith("text/plain") else None
        parts.append({"content_type": part.content_type, "data": part.data,
                      "name": part.name or part.content_location, "content_id": part.content_id,
                      "charset": part.charset, "text": text})
    return parts


def download(inst: dict, message_id: int, *, client=None, now: int | None = None,
             runner=subprocess.run) -> dict:
    """Fetch one notified MMS from the MMSC and store its content.

    Returns {"ok": True} or {"ok": False, "error", "final"}; "final" means no retry is
    scheduled. The MMSC is told the message was retrieved, which is what stops it resending
    the notification; that acknowledgement is best effort, since the content is already safe.
    """
    now = int(now or time.time())
    row = store.mms_for_download(message_id)
    if not row or row["direction"] != "in":
        return {"ok": False, "error": "no such MMS", "final": True}
    settings = mms_transport.resolve_settings(inst)
    expired = bool(row.get("expiry_ts")) and now > int(row["expiry_ts"])
    if expired and not int(row.get("attempts") or 0):
        # Already past the MMSC's retention when first seen (a backlog on a modem that was
        # offline): nothing to fetch, and nothing worth announcing.
        store.set_mms_state(message_id, "expired", error="The MMS expired before it could "
                            "be downloaded.", next_attempt_ts=None)
        return {"ok": False, "error": "expired", "final": True, "expired": True}
    store.set_mms_state(message_id, "downloading")
    try:
        with _exchange(inst, settings, client, runner) as client:
            response = client.request("GET", row["content_location"],
                                      headers=_request_headers(settings))
            if response.status != 200:
                raise mms_transport.MmsTransportError(
                    f"the MMSC answered HTTP {response.status}",
                    retryable=response.status >= 500 or response.status in (408, 429))
            pdu = mms_pdu.decode_pdu(response.body, now=now)
            if pdu.message_type != mms_pdu.M_RETRIEVE_CONF:
                raise mms_transport.MmsTransportError(
                    f"the MMSC answered with MMS message type {pdu.message_type:#x}")
            status = pdu.retrieve_status
            if status not in (None, mms_pdu.RETRIEVE_STATUS_OK):
                text = pdu.headers.get("retrieve-text") or \
                    mms_pdu.RETRIEVE_STATUS_DESCRIPTIONS.get(status, f"status {status:#x}")
                raise mms_transport.MmsTransportError(
                    f"the MMSC could not deliver the MMS: {text}",
                    retryable=0xC0 <= status < 0xE0)
            parts = _parts_for_store(pdu)
            body = "\n".join(p["text"] for p in parts if p["text"]) or pdu.subject
            store.save_mms_content(
                message_id, parts, subject=pdu.subject, body=body,
                from_addr=pdu.from_address or None, to_addrs=pdu.to,
                cc_addrs=pdu.headers.get("cc") or [], size=len(response.body))
            store.set_mms_state(message_id, "retrieved", error="", next_attempt_ts=None,
                                attempts_increment=1, message_status="ok")
            if row.get("transaction_id") and settings.get("mmsc"):
                try:
                    client.request("POST", settings["mmsc"],
                                   body=mms_pdu.encode_notifyresp_ind(
                                       row["transaction_id"], mms_pdu.STATUS_RETRIEVED),
                                   headers=_request_headers(settings,
                                                            mms_transport.MMS_CONTENT_TYPE))
                except Exception as exc:  # noqa: BLE001 -- the message itself is stored
                    log.info("MMS %s retrieved but the MMSC acknowledgement failed: %s",
                             message_id, exc)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 -- every failure must leave "downloading"
        # Transport and decoding errors are expected; anything else -- a full disk while the
        # parts are written, a database error -- is treated as transient. What matters is
        # that the MMS leaves "downloading": neither the queue nor a manual retry picks up
        # an MMS in that state, so an escape here would strand it until a restart.
        if not isinstance(exc, (mms_transport.MmsTransportError, mms_pdu.MmsDecodeError)):
            log.warning("MMS %s download failed unexpectedly: %r", message_id, exc)
        attempts = int(row.get("attempts") or 0) + 1
        retryable = getattr(exc, "retryable", True) and not expired
        if retryable and attempts <= len(RETRY_DELAYS):
            store.set_mms_state(message_id, "failed", error=str(exc),
                                next_attempt_ts=now + RETRY_DELAYS[attempts - 1],
                                attempts_increment=1)
            return {"ok": False, "error": str(exc), "final": False}
        store.set_mms_state(message_id, "expired" if expired else "failed", error=str(exc),
                            next_attempt_ts=None, attempts_increment=1)
        return {"ok": False, "error": str(exc), "final": True}


_RECIPIENT_RE = re.compile(r"^\+?\d{3,32}$|^[^@\s]+@[^@\s]+\.[^@\s]+$")


def parse_recipients(value) -> list[str]:
    items = value if isinstance(value, (list, tuple)) else str(value or "").replace(";", ",").split(",")
    recipients = []
    for item in items:
        text = "".join(str(item).split())
        if text and text not in recipients:
            recipients.append(text)
    return recipients


def _limit(settings: dict) -> int:
    return int(settings.get("max_size") or mms_transport.DEFAULT_MAX_SIZE)


def _size_problem(size: int, settings: dict) -> str | None:
    if size <= _limit(settings):
        return None
    return f"the MMS is {-(-size // 1024)} KB once packaged; this line allows " \
           f"{_limit(settings) // 1024} KB"


def _recipient_problem(recipients: list[str]) -> str | None:
    if not recipients:
        return "at least one recipient is required"
    if len(recipients) > 20:
        return "at most 20 recipients"
    bad = [r for r in recipients if not _RECIPIENT_RE.match(r)]
    if bad:
        return f"not a phone number or email address: {bad[0]}"
    return None


def prepare_outgoing(recipients: list[str], text: str, attachments: list[dict],
                     settings: dict, subject: str = "") -> tuple[list[dict], str | None, dict]:
    """Attachments ready to store and send, or why the MMS cannot be sent as composed.

    `attachments` are the files as the user picked them ({name, content_type, data}); they are
    checked, converted and fitted by fit_attachments(). Returns (attachments, problem,
    summary)."""
    problem = _recipient_problem(recipients)
    if problem:
        return [], problem, {}
    if not (text or "").strip() and not attachments:
        return [], "an MMS needs text or an attachment", {}
    return fit_attachments(attachments, text, subject, recipients, settings)


def validate_outgoing(recipients: list[str], text: str, attachments: list[dict],
                      settings: dict, subject: str = "") -> str | None:
    """Why this MMS cannot be sent as composed, or None (see prepare_outgoing)."""
    return prepare_outgoing(recipients, text, attachments, settings, subject)[1]


def _can_convert(kind: str, content_type: str) -> bool:
    return mms_convert.converter_for(kind, content_type) is not None


def attachment_formats() -> list[dict]:
    """The capability table as this gateway applies it (see mms_media.capabilities)."""
    return mms_media.capabilities(_can_convert)


def check_attachments(attachments: list[dict], *,
                      convert: bool = False) -> tuple[list[dict], str | None]:
    """Attachments ({name, content_type, data}) as the gateway reads them -- the type their
    content shows, a safe display name, the playing time of audio and video, the policy -- or
    the reason the first unacceptable one cannot be sent (see mms_media.check_attachment).
    "convert" formats pass only with `convert`, for a caller that converts them."""
    checked = []
    for index, item in enumerate(attachments):
        declared = str(item.get("content_type") or "")
        name = mms_media.display_name(item.get("name") or "", declared) \
            if item.get("name") else ""
        result = mms_media.check_attachment(name or f"attachment {index + 1}", declared,
                                            item.get("data") or b"",
                                            can_convert=_can_convert if convert else None)
        if result.error:
            return [], result.error
        checked.append({"name": name or f"attachment{index + 1}."
                        f"{mms_media.file_extension(result.content_type)}",
                        "content_type": result.content_type, "data": bytes(item["data"]),
                        "duration_ms": result.duration_ms, "kind": result.kind,
                        "policy": result.policy})
    return checked, None


def _allocate(sizes: list[int], budget: int) -> list[int]:
    """Split `budget` bytes between attachments of these sizes: one smaller than its equal
    share keeps its size, and what it leaves goes to the larger ones."""
    targets = [0] * len(sizes)
    remaining = max(0, budget)
    order = sorted(range(len(sizes)), key=sizes.__getitem__)
    for position, index in enumerate(order):
        share = remaining // (len(order) - position)
        targets[index] = share
        remaining -= min(sizes[index], share)
    return targets


# Recent fits by (original digest, type, byte target, force): composing re-plans the whole
# message each time an attachment is added or removed, mostly with the same targets.
_FIT_CACHE: dict[tuple, mms_convert.Fitted] = {}
_FIT_CACHE_SIZE = 64
_fit_cache_lock = threading.Lock()


def _fit(converter, content_type: str, data: bytes, target: int, force: bool):
    key = (hashlib.sha256(data).digest(), content_type, int(target), bool(force))
    with _fit_cache_lock:
        if key in _FIT_CACHE:
            return _FIT_CACHE[key]
    fitted = converter.fit(content_type, data, target, force=force)
    with _fit_cache_lock:
        _FIT_CACHE[key] = fitted
        while len(_FIT_CACHE) > _FIT_CACHE_SIZE:
            _FIT_CACHE.pop(next(iter(_FIT_CACHE)))
    return fitted


def _renamed(name: str, content_type: str) -> str:
    extension = mms_media.file_extension(content_type)
    stem, dot, _old = name.rpartition(".")
    return f"{stem if dot and stem else name}.{extension}"


def fit_attachments(attachments: list[dict], text: str, subject: str,
                    recipients: list[str], settings: dict
                    ) -> tuple[list[dict], str | None, dict]:
    """Check, convert and shrink attachments so the packaged m-send-req fits the line's limit.

    What cannot be made smaller -- sound, video (until a video converter exists), an animated
    GIF -- is counted as it is. The room left once those, the text and the packaging are paid
    for is shared between the attachments a converter can shrink (mms_convert), each re-encoded
    from its original; a "convert" format is always re-encoded. The result is packaged and
    measured, and the shares tightened if it is still over. Returns (attachments ready for
    create_outgoing, problem or None, summary): the summary gives the packaged size, the limit
    and, per attachment, its size before and after and whether it was converted."""
    checked, problem = check_attachments(attachments, convert=True)
    if problem:
        return [], problem, {}
    limit = _limit(settings)
    items = []
    for item in checked:
        converter = mms_convert.converter_for(item["kind"], item["content_type"])
        try:
            adjustable = bool(converter) and converter.adjustable(item["content_type"],
                                                                  item["data"])
        except mms_convert.ConversionError as exc:
            return [], f"{item['name']}: {exc}", {}
        items.append({**item, "original": item["data"], "original_type": item["content_type"],
                      "original_name": item["name"], "adjustable": adjustable,
                      "converter": converter, "force": item["policy"] == mms_media.CONVERT,
                      "converted": False, "width": None, "height": None})
    # Before any recipient is typed a placeholder of typical length stands in; sending
    # measures again with the real ones.
    to = recipients or ["+10000000000"]

    def package() -> bytes:
        return build_request("0" * 20, to, subject, _compose_parts(text, items))

    shrinkable = [i for i in items if i["adjustable"] or i["force"]]
    for item in shrinkable:
        item["data"] = b""
    # The packaging around empty shrinkable parts, plus a few bytes for their length fields.
    budget = limit - len(package()) - 4 * len(shrinkable)
    request = b""
    for _attempt in range(4):
        targets = _allocate([len(i["original"]) for i in shrinkable], budget)
        for item, target in zip(shrinkable, targets):
            try:
                fitted = _fit(item["converter"], item["original_type"], item["original"],
                              target, item["force"])
            except mms_convert.ConversionError as exc:
                return [], f"{item['original_name']}: {exc}", {}
            item.update(data=fitted.data, content_type=fitted.content_type,
                        width=fitted.width, height=fitted.height, converted=fitted.converted,
                        name=_renamed(item["original_name"], fitted.content_type)
                        if fitted.content_type != item["original_type"] else item["original_name"])
        request = package()
        if len(request) <= limit or not shrinkable:
            break
        budget -= len(request) - limit + 256
    summary = {
        "limit": limit, "size": len(request), "fits": len(request) <= limit,
        "attachments": [{"name": i["name"], "content_type": i["content_type"],
                         "size": len(i["data"]), "original_name": i["original_name"],
                         "original_type": i["original_type"],
                         "original_size": len(i["original"]), "converted": i["converted"],
                         "adjustable": i["adjustable"], "width": i["width"],
                         "height": i["height"]} for i in items]}
    fitted = [{k: i[k] for k in ("name", "content_type", "data", "duration_ms")} for i in items]
    return fitted, _size_problem(len(request), settings), summary


def _compose_parts(text: str, attachments: list[dict]) -> list[dict]:
    """Stored parts for a composed message; `attachments` come from check_attachments()."""
    parts = []
    if (text or "").strip():
        parts.append({"content_type": "text/plain", "data": text.encode("utf-8"),
                      "name": "text.txt", "content_id": "text", "charset": "utf-8",
                      "text": text})
    for index, item in enumerate(attachments):
        parts.append({"content_type": item["content_type"], "data": item["data"],
                      "name": item["name"], "content_id": f"part{index + 1}",
                      "duration_ms": item.get("duration_ms")})
    return parts


def _duration_ms(part: dict) -> int | None:
    if part.get("duration_ms"):
        return int(part["duration_ms"])
    if not str(part.get("content_type") or "").lower().startswith(("audio/", "video/")):
        return None
    return mms_media.check_attachment("", part["content_type"], part["data"]).duration_ms


def build_request(transaction_id: str, recipients: list[str], subject: str,
                  parts: list[dict]) -> bytes:
    """The m-send-req for stored or composed parts ({content_type, data, name, content_id,
    charset}): unique references, a checked SMIL presentation, then the PDU itself."""
    pdu_parts = mms_pdu.assign_references([
        mms_pdu.MmsPart(p["content_type"], p["data"], name=p.get("name") or "",
                        content_id=p.get("content_id") or "",
                        content_location=p.get("name") or "", charset=p.get("charset") or "",
                        duration_ms=_duration_ms(p))
        for p in parts])
    pdu_parts.insert(0, mms_pdu.build_smil(pdu_parts))
    return mms_pdu.encode_send_req(transaction_id=transaction_id, to=recipients,
                                   parts=pdu_parts, subject=subject or "",
                                   delivery_report=True)


def create_outgoing(instance: str, recipients: list[str], text: str, attachments: list[dict],
                    subject: str = "") -> dict:
    """Store a composed MMS (state "sending") and return its message record. `attachments`
    come from prepare_outgoing(); anything a phone could not take as it is raises ValueError."""
    attachments, problem = check_attachments(attachments)
    if problem:
        raise ValueError(problem)
    peer = store.canonical_peer(instance, recipients[0]) if len(recipients) == 1 \
        else ", ".join(recipients)
    rec = store.create_outgoing_mms(instance, peer, to_addrs=recipients, subject=subject,
                                    body=text or subject,
                                    transaction_id=uuid.uuid4().hex[:20])
    parts = _compose_parts(text, attachments)
    store.save_mms_content(rec["id"], parts, subject=subject, body=text or subject,
                           size=sum(len(p["data"]) for p in parts))
    return store.get_message(rec["id"])


def send(inst: dict, message_id: int, *, client=None, runner=subprocess.run) -> dict:
    """Submit one stored outgoing MMS to the MMSC.

    The result is "sent" once the MMSC accepts it (m-send-conf OK), "failed" when it refuses
    or nothing reached it, and "unknown" when the request went out but no answer came back --
    never retried automatically, since a second submission would deliver a second MMS.
    """
    row = store.mms_for_download(message_id)
    if not row or row["direction"] != "out":
        return {"ok": False, "status": "failed", "error": "no such MMS"}
    settings = mms_transport.resolve_settings(inst)
    try:
        request = build_request(row["transaction_id"], json.loads(row["to_addrs"] or "[]"),
                                row["subject"] or "", store.mms_parts_with_data(message_id))
        # Checked again as submitted: the line's limit may have changed since it was composed.
        too_big = _size_problem(len(request), settings)
        if too_big:
            store.set_mms_state(message_id, "failed", error=too_big, message_status="failed")
            return {"ok": False, "status": "failed", "error": too_big}
        with _exchange(inst, settings, client, runner) as client:
            response = client.request(
                "POST", settings["mmsc"], body=request,
                headers=_request_headers(settings, mms_transport.MMS_CONTENT_TYPE),
                timeout=max(180.0, len(request) / 100.0))
        if response.status != 200:
            raise mms_transport.MmsTransportError(f"the MMSC answered HTTP {response.status}",
                                                  after_send=True)
        conf = mms_pdu.decode_pdu(response.body)
        if conf.message_type != mms_pdu.M_SEND_CONF:
            raise mms_transport.MmsTransportError(
                f"the MMSC answered with MMS message type {conf.message_type:#x}",
                after_send=True)
        if conf.response_status not in (None, mms_pdu.RESPONSE_STATUS_OK):
            reason = conf.headers.get("response-text") or mms_pdu.RESPONSE_STATUS_DESCRIPTIONS.get(
                conf.response_status, f"status {conf.response_status:#x}")
            error = f"the MMSC refused the MMS: {reason}"
            store.set_mms_state(message_id, "failed", error=error, message_status="failed")
            return {"ok": False, "status": "failed", "error": error}
        store.set_mms_state(message_id, "sent", error="", message_ref=conf.message_id,
                            message_status="sent")
        return {"ok": True, "status": "sent", "error": None}
    except (mms_transport.MmsTransportError, mms_pdu.MmsDecodeError, OSError,
            ValueError) as exc:
        status = "unknown" if getattr(exc, "after_send", False) or \
            isinstance(exc, mms_pdu.MmsDecodeError) else "failed"
        store.set_mms_state(message_id, "failed", error=str(exc), message_status=status)
        if status == "unknown":
            store.set_message_status(message_id, "unknown", str(exc))
        return {"ok": False, "status": status, "error": str(exc)}
