"""
notify_push.py - Outbound push notifications for incoming events (SMS / calls).

Four independent, separately-configurable channels, all driven from global settings:
  - webhook : GET or POST standard/custom fields to a user-supplied URL.
  - telegram: send a formatted message to a chat/channel via a Telegram bot.
  - pushplus: send through the official PushPlus HTTP API.
  - feishu  : send text notifications through a Feishu/Lark custom bot webhook.

Both fire on the SAME internal events (incoming_sms, incoming_call) and carry the same
core fields (SIM ICCID, the line's own MSISDN, the event's source number, the event type,
and the SMS text when applicable). Delivery is best-effort and MUST NOT block or break the
engine-event path: callers dispatch fire-and-forget and every network call is wrapped so a
failing/slow endpoint only logs a warning.
"""
from __future__ import annotations

import collections
import base64
import hashlib
import hmac
import logging
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import Any
from urllib.parse import quote

import requests

from . import egress

log = logging.getLogger("vowifi.push")

# Webhook targets are often internal hosts with self-signed TLS; we POST with verify=False
# (see _post_webhook), so silence urllib3's per-request InsecureRequestWarning to keep logs clean.
try:
    from urllib3.exceptions import InsecureRequestWarning
    requests.packages.urllib3.disable_warnings(InsecureRequestWarning)  # type: ignore[attr-defined]
except Exception:  # noqa
    pass

# Event identifiers shared by both channels and the settings UI checkboxes.
EV_INCOMING_SMS = "incoming_sms"
EV_INCOMING_CALL = "incoming_call"
# The host itself became unable to do its job (brown-out, thermal throttling, a full disk).
# Every line drops together when this happens, so it is reported once for the box rather than
# once per line.
EV_HOST_ALERT = "host_alert"
# The carrier started answering registration with a different public number, which is what a
# number port looks like from here. It changes the line's caller identity, so it is announced
# rather than silently corrected.
EV_NUMBER_CHANGED = "number_changed"
# The gateway stopped trying to fix a line by itself — either every exit was tried and none
# could carry a tunnel, or the failures were never the exit's fault to begin with. Both need
# a person, and a gateway that cannot recover should say so rather than rebuild forever.
EV_LINE_UNRECOVERABLE = "line_unrecoverable"
# The scheduled number-keeping action ran. Both outcomes are announced, not just failures:
# the successful case spent the user's money on their SIM, and that deserves a receipt.
EV_KEEPALIVE_RESULT = "keepalive_result"
# A plan SIM renews itself, so what threatens the number is a balance too low to pay the next
# cycle. Repeated on a slow cadence while it lasts rather than once, because it stays true.
EV_BALANCE_LOW = "balance_low"
# An inbound call that nobody answered. Separate from EV_INCOMING_CALL because that one fires
# when the phone starts ringing (and is useful only while a browser is open to answer it),
# whereas this one is the outcome — the notification that matters when nobody was there.
# A call the user actively declined is NOT a missed call and never raises this.
EV_MISSED_CALL = "missed_call"
# Somebody left a message. Announced separately from the missed call itself because the
# action it invites is different: there is now something to listen to.
EV_VOICEMAIL = "voicemail_received"
# A new MDD Sim Gateway Release is available. This is a gateway-level event and therefore
# carries no SIM identity; update settings decide whether patch-only releases are announced.
EV_SOFTWARE_UPDATE = "software_update"

_TIMEOUT = 8  # seconds; keep short so a dead endpoint never piles up threads
_DELIVERY_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="mdd-notify")
_TOKEN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_HISTORY_LOCK = threading.RLock()
_PENDING: dict[str, dict] = {}
# Telegram message id of a delivered incoming-SMS notification -> the line and peer it came
# from. Kept in memory only for notification formatting and deduplication
# and bounded: a stale mapping just means the operator has to use /sms explicitly.
_REPLY_TARGETS: "collections.OrderedDict[int, dict]" = collections.OrderedDict()
_REPLY_TARGET_LIMIT = 200


def _history_path() -> str:
    root = os.environ.get("MDD_DATA", os.path.join(os.getcwd(), "data"))
    return os.path.join(root, "notifications", "deliveries.jsonl")


def _record_delivery(record: dict) -> None:
    """Append metadata only: notification bodies, numbers and credentials are never logged."""
    path = _history_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    safe = {key: record.get(key) for key in (
        "id", "created_at", "finished_at", "channel", "event", "instance",
        "status", "attempts", "status_code", "error",
    ) if record.get(key) is not None}
    with _HISTORY_LOCK, open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, ensure_ascii=False) + "\n")


def delivery_status(limit: int = 100) -> dict:
    limit = max(1, min(500, int(limit)))
    rows = []
    try:
        with _HISTORY_LOCK, open(_history_path(), encoding="utf-8") as handle:
            rows = [json.loads(line) for line in handle if line.strip()][-limit:]
    except (OSError, ValueError):
        rows = []
    with _HISTORY_LOCK:
        pending = [dict(value) for value in _PENDING.values()]
    return {"pending": pending, "history": list(reversed(rows))}


def clear_delivery_history() -> None:
    try:
        os.remove(_history_path())
    except FileNotFoundError:
        pass


def _deliver_with_retry(channel: str, sender, channel_cfg: dict, payload: dict) -> None:
    delivery_id = uuid.uuid4().hex
    entry = {
        "id": delivery_id, "created_at": int(time.time()), "channel": channel,
        "event": payload.get("event"), "instance": payload.get("instance"),
        "status": "pending", "attempts": 0,
    }
    with _HISTORY_LOCK:
        _PENDING[delivery_id] = dict(entry)
    error = ""
    result = {}
    for attempt, delay in enumerate((0, 2, 5), start=1):
        if delay:
            time.sleep(delay)
        entry["attempts"] = attempt
        try:
            result = sender(channel_cfg, payload) or {}
            entry.update(status="delivered", status_code=result.get("status_code"))
            error = ""
            break
        except Exception as exc:  # noqa: delivery errors are deliberately sanitized
            error = f"{type(exc).__name__}: delivery failed"
            entry.update(status="retrying" if attempt < 3 else "failed", error=error)
            with _HISTORY_LOCK:
                _PENDING[delivery_id] = dict(entry)
    entry["finished_at"] = int(time.time())
    if error:
        entry["error"] = error
    with _HISTORY_LOCK:
        _PENDING.pop(delivery_id, None)
    _record_delivery(entry)


def _events_enabled(chan: dict) -> dict:
    ev = chan.get("events") or {}
    # default: both on if the key is absent (a freshly-enabled channel notifies everything)
    return {
        EV_INCOMING_SMS: ev.get(EV_INCOMING_SMS, True),
        EV_INCOMING_CALL: ev.get(EV_INCOMING_CALL, True),
        EV_HOST_ALERT: ev.get(EV_HOST_ALERT, True),
        EV_NUMBER_CHANGED: ev.get(EV_NUMBER_CHANGED, True),
        EV_LINE_UNRECOVERABLE: ev.get(EV_LINE_UNRECOVERABLE, True),
        EV_KEEPALIVE_RESULT: ev.get(EV_KEEPALIVE_RESULT, True),
        EV_BALANCE_LOW: ev.get(EV_BALANCE_LOW, True),
        EV_MISSED_CALL: ev.get(EV_MISSED_CALL, True),
        EV_VOICEMAIL: ev.get(EV_VOICEMAIL, True),
        EV_SOFTWARE_UPDATE: ev.get(EV_SOFTWARE_UPDATE, True),
    }


def has_enabled_channel(settings: dict, event: str) -> bool:
    standard = any(bool((settings.get(key) or {}).get("enabled"))
               and bool(_events_enabled(settings.get(key) or {}).get(event))
               for key in ("webhook", "telegram", "pushplus"))
    return standard or any(
        bool(channel.get("enabled")) and bool(_events_enabled(channel).get(event))
        for channel in feishu_channels(settings.get("feishu") or {})
    )


def feishu_channels(cfg: dict) -> list[dict]:
    """Normalized Feishu destinations, including one legacy single-bot destination.

    An explicit ``channels`` list owns delivery as soon as it is non-empty. This prevents the
    legacy top-level URL from causing duplicate fan-out after the first multi-channel save.
    """
    channels = cfg.get("channels")
    if isinstance(channels, list):
        return [dict(channel) for channel in channels if isinstance(channel, dict)]
    if cfg.get("url") or cfg.get("enabled"):
        legacy = dict(cfg)
        legacy.pop("channels", None)
        legacy.setdefault("id", "legacy")
        legacy.setdefault("name", "Feishu / Lark")
        legacy.setdefault("instances", [])
        return [legacy]
    return []


def feishu_channel_matches(channel: dict, instance_id) -> bool:
    selected = {str(value) for value in (channel.get("instances") or []) if str(value).strip()}
    return not selected or str(instance_id or "") in selected


def validate_feishu_channels(cfg: dict) -> list[dict]:
    channels = feishu_channels(cfg)
    seen = set()
    for position, channel in enumerate(channels, start=1):
        channel_id = str(channel.get("id") or position)
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", channel_id) or channel_id in seen:
            raise ValueError("Feishu channel IDs must be unique and use letters, digits, ., _ or -")
        seen.add(channel_id)
        if len(str(channel.get("name") or "")) > 120 \
                or any(char in str(channel.get("name") or "") for char in "\r\n"):
            raise ValueError("Feishu channel name is invalid")
        instances = channel.get("instances") or []
        if not isinstance(instances, list) or any(
                not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(value)) for value in instances):
            raise ValueError("Feishu instance filters are invalid")
        validate_message_templates(channel)
        if channel.get("enabled"):
            validate_feishu_url(channel.get("url"))
    return channels


def build_payload(event: str, instance: dict, source: str, text: str | None) -> dict:
    """The canonical event body both channels are built from. `instance` is the stored
    line config (for ICCID + MSISDN); `source` is the event's originating number; `text`
    is the SMS body (None for calls)."""
    return {
        "event": event,                                   # incoming_sms | incoming_call
        "instance": str(instance.get("id", "")),
        "sim_name": instance.get("name", "") or "",
        "iccid": instance.get("iccid", "") or "",
        "msisdn": instance.get("msisdn", "") or "",       # the line's own number (may be "")
        "from": source or "",                             # the event's source number
        "text": text if event in (EV_INCOMING_SMS, EV_HOST_ALERT, EV_NUMBER_CHANGED,
                                  EV_LINE_UNRECOVERABLE, EV_KEEPALIVE_RESULT,
                                  EV_BALANCE_LOW, EV_VOICEMAIL,
                                  EV_SOFTWARE_UPDATE) else None,
    }


# Every notification leads with this. A push arrives out of context — on a lock screen, in a
# Telegram list beside a dozen other bots — and "未接来电" alone does not say which machine is
# talking. Kept short because notification titles are truncated aggressively.
BRAND = "MDD"


def _titled(text: str) -> str:
    """Prefix once. The software-update title already carries the product name, so a blind
    prefix would render it as "MDD · MDD Sim Gateway …"."""
    return text if text.startswith(BRAND) else f"{BRAND} · {text}"


def _default_notification_message(payload: dict) -> dict:
    """Build the human-readable title/content shared by templates and vendor channels."""
    event = payload.get("event")
    sim = payload.get("sim_name") or payload.get("iccid") or payload.get("instance") or "SIM"
    own = payload.get("msisdn") or ""
    sender = payload.get("from") or "unknown"
    if event == EV_HOST_ALERT:
        # Not about one SIM: the box is degraded and every line is affected at once.
        return {"title": _titled(f"网关主机异常 · {sender}"),
                "content": payload.get("text") or ""}
    if event == EV_NUMBER_CHANGED:
        return {"title": _titled(f"线路号码已变更 · {sim}"), "content": payload.get("text") or ""}
    if event == EV_LINE_UNRECOVERABLE:
        return {"title": _titled(f"线路无法自动恢复 · {sim}"), "content": payload.get("text") or ""}
    if event == EV_KEEPALIVE_RESULT:
        return {"title": _titled(f"保号执行结果 · {sim}"), "content": payload.get("text") or ""}
    if event == EV_BALANCE_LOW:
        return {"title": _titled(f"余额不足 · {sim}"), "content": payload.get("text") or ""}
    if event == EV_VOICEMAIL:
        return {"title": _titled(f"新留言 · {sim}"), "content": payload.get("text") or ""}
    if event == EV_MISSED_CALL:
        lines = [f"SIM: {sim}"]
        if own:
            lines.append(f"本机号码: {own}")
        lines.append(f"来源号码: {sender}")
        return {"title": _titled(f"未接来电 · {sim}"), "content": "\n".join(lines)}
    if event == EV_SOFTWARE_UPDATE:
        return {"title": _titled(f"MDD Sim Gateway 新版本 · v{sender}"),
                "content": payload.get("text") or ""}
    title = _titled(f"VoWiFi {'短信' if event == EV_INCOMING_SMS else '来电'} · {sim}")
    lines = [f"SIM: {sim}"]
    if own:
        lines.append(f"本机号码: {own}")
    lines.append(f"来源号码: {sender}")
    if event == EV_INCOMING_SMS:
        lines.extend(["", payload.get("text") or ""])
    return {
        "title": title,
        "content": "\n".join(lines),
    }


def _template_context(cfg: dict, payload: dict) -> dict:
    return {**payload, **build_notification_message(payload, cfg)}


def _render(value, context: dict):
    """Render {{field}} placeholders recursively while preserving an exact field's type."""
    if isinstance(value, dict):
        return {str(_render(k, context)): _render(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, context) for v in value]
    if not isinstance(value, str):
        return value
    exact = _TOKEN.fullmatch(value)
    if exact:
        return context.get(exact.group(1))
    return _TOKEN.sub(lambda m: str(context.get(m.group(1)) or ""), value)


_MESSAGE_TEMPLATE_FIELDS = {
    "event", "instance", "sim_name", "iccid", "msisdn", "from", "text",
    "title", "content",
}
_MAX_MESSAGE_TEMPLATE_LENGTH = 4000
NOTIFICATION_EVENTS = {
    EV_INCOMING_SMS, EV_INCOMING_CALL, EV_HOST_ALERT, EV_NUMBER_CHANGED,
    EV_LINE_UNRECOVERABLE, EV_KEEPALIVE_RESULT, EV_BALANCE_LOW, EV_MISSED_CALL,
    EV_VOICEMAIL, EV_SOFTWARE_UPDATE,
}


def _event_message_template(cfg: dict, event: str) -> dict:
    if not isinstance(cfg, dict):
        raise ValueError("notification channel configuration must be an object")
    templates = cfg.get("message_templates") or {}
    if not isinstance(templates, dict):
        raise ValueError("notification message templates must be an object")
    value = templates.get(event) or {}
    if not isinstance(value, dict):
        raise ValueError("notification event template must be an object")
    return value


def _validate_message_template(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("notification message template must be text")
    text = value
    if len(text) > _MAX_MESSAGE_TEMPLATE_LENGTH:
        raise ValueError("notification message template is too long")
    unknown = sorted(set(_TOKEN.findall(text)) - _MESSAGE_TEMPLATE_FIELDS)
    if unknown:
        raise ValueError(f"unknown notification template field: {unknown[0]}")
    return text


def validate_message_templates(cfg: dict) -> None:
    if not isinstance(cfg, dict):
        raise ValueError("notification channel configuration must be an object")
    templates = cfg.get("message_templates") or {}
    if not isinstance(templates, dict):
        raise ValueError("notification message templates must be an object")
    for event, template in templates.items():
        if event not in NOTIFICATION_EVENTS:
            raise ValueError(f"unknown notification template event: {event}")
        if not isinstance(template, dict):
            raise ValueError("notification event template must be an object")
        unknown = sorted(set(template) - {"title", "content"})
        if unknown:
            raise ValueError(f"unknown notification template property: {unknown[0]}")
        for value in template.values():
            _validate_message_template(value)


def _render_notification_message(payload: dict, cfg: dict, default: dict) -> dict:
    template = _event_message_template(cfg or {}, str(payload.get("event") or ""))
    context = {**payload, **default}
    result = dict(default)
    for field in ("title", "content"):
        if field in template and str(template.get(field) or "").strip():
            result[field] = str(_render(_validate_message_template(template[field]), context))
    return result


def build_notification_message(payload: dict, cfg: dict | None = None) -> dict:
    """Build the shared message, applying an optional per-event title/content override.

    Templates deliberately support field replacement only. They cannot evaluate expressions,
    access files or call code, and an unknown field is rejected instead of silently disappearing.
    """
    return _render_notification_message(payload, cfg or {}, _default_notification_message(payload))


def _json_setting(value, fallback):
    if isinstance(value, (dict, list)):
        return value
    if not str(value or "").strip():
        return fallback
    return json.loads(str(value))


def build_webhook_request(cfg: dict, payload: dict) -> tuple[str, str, dict]:
    """Build a requests.request call for standard or custom webhooks.

    GET sends the rendered fields as query parameters. POST supports JSON, form-encoded and raw
    bodies. Custom headers and URL/body placeholders make the adapter usable with arbitrary
    webhook receivers without hard-coding each vendor's schema.
    """
    method = str(cfg.get("method") or "POST").upper()
    if method not in {"GET", "POST"}:
        raise ValueError("webhook method must be GET or POST")
    context = _template_context(cfg, payload)
    url = _render(str(cfg.get("url") or "").strip(), context)
    if not url:
        raise ValueError("webhook URL is empty")

    headers = {"User-Agent": "mdd-sim-gateway"}
    custom_headers = _json_setting(cfg.get("headers_json") or cfg.get("headers"), {})
    if not isinstance(custom_headers, dict):
        raise ValueError("webhook headers must be a JSON object")
    headers.update({str(k): str(_render(v, context)) for k, v in custom_headers.items()})

    preset = str(cfg.get("format") or "generic")
    if preset == "custom":
        raw_template = cfg.get("payload_template") or "{}"
        mode = str(cfg.get("body_mode") or "json")
        if mode == "raw":
            body = _render(str(raw_template), context)
        else:
            body = _render(_json_setting(raw_template, {}), context)
    else:
        # Standard webhooks carry both canonical machine fields and the same rendered title and
        # content used by vendor channels. Existing receivers keep every original field, while a
        # receiver that wants human-readable text no longer has to recreate event wording.
        body = {**payload, **build_notification_message(payload, cfg)}

    kwargs = {"headers": headers, "timeout": _TIMEOUT,
              "verify": bool(cfg.get("verify_tls", True))}
    if method == "GET":
        kwargs["params"] = body if isinstance(body, dict) else {"payload": str(body)}
    else:
        mode = str(cfg.get("body_mode") or "json")
        if mode == "form":
            if not isinstance(body, dict):
                raise ValueError("form webhook payload must be a JSON object")
            kwargs["data"] = body
        elif mode == "raw":
            kwargs["data"] = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        else:
            kwargs["json"] = body
    return method, url, kwargs


def _post_webhook(cfg: dict, payload: dict):
    try:
        result = send_webhook(cfg, payload)
        log.info("webhook %s -> %s", payload.get("event"), result["status_code"])
    except Exception as e:  # noqa
        log.warning("webhook delivery failed: %r", e)


def send_webhook(cfg: dict, payload: dict) -> dict:
    """Send one webhook or raise a sanitized error. Used by delivery and the test API."""
    method, url, kwargs = build_webhook_request(cfg, payload)
    try:
        response = requests.request(method, url, **kwargs)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError("webhook request failed") from None
    return {"ok": True, "status_code": response.status_code}


def send_pushplus(cfg: dict, payload: dict) -> dict:
    """Send one message with PushPlus' documented JSON API."""
    token = str(cfg.get("token") or "").strip()
    if not token:
        raise ValueError("PushPlus token is required")
    template = str(cfg.get("template") or "html").strip().lower()
    if template not in {"html", "txt", "markdown", "json"}:
        raise ValueError("unsupported PushPlus template")
    channel = str(cfg.get("channel") or "wechat").strip().lower()
    if channel not in {"wechat", "webhook", "cp", "mail", "sms", "voice", "extension", "app", "clawbot"}:
        raise ValueError("unsupported PushPlus channel")
    message = build_notification_message(payload, cfg)
    body = {"token": token, **message, "template": template, "channel": channel}
    topic = str(cfg.get("topic") or "").strip()
    if topic:
        body["topic"] = topic
    try:
        response = requests.post("https://www.pushplus.plus/send", json=body, timeout=_TIMEOUT)
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError("PushPlus request failed") from None
    # PushPlus returns HTTP 200 for application-level errors; its documented success code is 200.
    if not isinstance(result, dict) or str(result.get("code") or "") != "200":
        raise RuntimeError("PushPlus rejected the message")
    return {"ok": True, "status_code": response.status_code}


def feishu_signature(timestamp: int | str, secret: str) -> str:
    """Return the signature required by Feishu/Lark custom bots.

    The platform defines the HMAC key as ``<timestamp>\n<secret>`` and signs an empty
    message. Keeping this in a small pure function makes the slightly unusual contract
    explicit and gives callers a deterministic unit-test target.
    """
    key = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(key, b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _feishu_text(payload: dict, cfg: dict | None = None) -> str:
    message = build_notification_message(payload, cfg or {})
    return "\n\n".join(part for part in (message["title"], message["content"]) if part)


def validate_feishu_url(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        raise ValueError("Feishu webhook URL is required")
    if not re.fullmatch(
            r"https://(?:open\.feishu\.cn|open\.larksuite\.com)/open-apis/bot/v2/hook/[^/?#]+",
            value, re.IGNORECASE):
        raise ValueError("Feishu webhook URL is invalid")
    return value


def send_feishu(cfg: dict, payload: dict) -> dict:
    """Send one Feishu/Lark custom-bot text message and validate its JSON result.

    Feishu commonly returns HTTP 200 even when it rejects a payload, so transport success
    alone is insufficient. Both Feishu's ``code`` response and Lark-compatible
    ``StatusCode``/``status`` variants are accepted only when they explicitly report success.
    """
    url = validate_feishu_url(cfg.get("url"))
    body: dict[str, Any] = {
        "msg_type": "text",
        "content": {"text": _feishu_text(payload, cfg)},
    }
    secret = str(cfg.get("secret") or "").strip()
    if secret:
        timestamp = int(time.time())
        body.update({"timestamp": str(timestamp), "sign": feishu_signature(timestamp, secret)})
    try:
        response = requests.post(url, json=body, timeout=_TIMEOUT)
        response.raise_for_status()
        result = response.json()
    except requests.RequestException:
        # Never include the exception: its URL contains the bot token.
        raise RuntimeError("Feishu request failed") from None
    except ValueError:
        raise RuntimeError("Feishu returned an invalid response") from None
    if not isinstance(result, dict):
        raise RuntimeError("Feishu returned an invalid response")
    success = False
    if "code" in result:
        success = str(result.get("code")) == "0"
    elif "StatusCode" in result:
        success = str(result.get("StatusCode")) == "0"
    elif "status" in result:
        success = str(result.get("status")).lower() in {"ok", "success"}
    if not success:
        raise RuntimeError("Feishu rejected the message")
    return {"ok": True, "status_code": response.status_code}


def _telegram_headline(icon: str, text: str) -> str:
    """Telegram's own first line. The icon stays leftmost — it is what makes the event type
    scannable in a chat list — and the brand follows it, for the same reason the titles carry
    one: a bot's messages arrive with no other indication of which machine sent them."""
    text = text.removeprefix(f"{BRAND} ").removeprefix(f"{BRAND} · ")
    return f"{icon} {BRAND} · {text}" if icon else f"{BRAND} · {text}"


def _default_telegram_text(payload: dict) -> str:
    ev = payload.get("event")
    if ev == EV_LINE_UNRECOVERABLE:
        return "\n".join([_telegram_headline("🛑", f"线路无法自动恢复 · {payload.get('sim_name') or payload.get('instance')}"),
                           "", payload.get("text") or ""])
    if ev == EV_NUMBER_CHANGED:
        return "\n".join([_telegram_headline("🔄", f"线路号码已变更 · {payload.get('sim_name') or payload.get('instance')}"),
                           "", payload.get("text") or ""])
    if ev == EV_HOST_ALERT:
        # Not a SIM event: the box itself is degraded, so the SIM/From lines would be
        # meaningless (a previous version rendered this as an incoming call from the
        # hardware model).
        return "\n".join([_telegram_headline("⚠️", "网关主机异常"), str(payload.get("from") or ""), "",
                          payload.get("text") or ""])
    if ev == EV_KEEPALIVE_RESULT:
        return "\n".join([_telegram_headline("◷", f"保号执行结果 · {payload.get('sim_name') or payload.get('instance')}"),
                           "", payload.get("text") or ""])
    if ev == EV_BALANCE_LOW:
        return "\n".join([_telegram_headline("⚠️", f"余额不足 · {payload.get('sim_name') or payload.get('instance')}"),
                           "", payload.get("text") or ""])
    if ev == EV_VOICEMAIL:
        return "\n".join([_telegram_headline("🎙", f"新留言 · {payload.get('sim_name') or payload.get('instance')}"),
                           "", payload.get("text") or ""])
    if ev == EV_MISSED_CALL:
        name = payload.get("sim_name") or payload.get("iccid") or payload.get("instance")
        msisdn = payload.get("msisdn")
        return "\n".join([_telegram_headline("📵", "Missed call"),
                          f"SIM: {name}" + (f" ({msisdn})" if msisdn else ""),
                          f"From: {payload.get('from') or 'unknown'}"])
    if ev == EV_SOFTWARE_UPDATE:
        return "\n".join([_telegram_headline("🆕", f"Sim Gateway 新版本 · v{payload.get('from') or ''}"),
                           "", payload.get("text") or ""])
    head = _telegram_headline("📩" if ev == EV_INCOMING_SMS else "📞",
                              "Incoming SMS" if ev == EV_INCOMING_SMS else "Incoming call")
    name = payload.get("sim_name") or payload.get("iccid") or payload.get("instance")
    msisdn = payload.get("msisdn")
    sim_line = f"SIM: {name}" + (f" ({msisdn})" if msisdn else "")
    lines = [head, sim_line, f"From: {payload.get('from') or 'unknown'}"]
    if ev == EV_INCOMING_SMS:
        lines.append("")
        lines.append(payload.get("text") or "")
    return "\n".join(lines)


def _telegram_text(payload: dict, cfg: dict | None = None) -> str:
    """Keep Telegram's established layout unless this event has an explicit override."""
    default_text = _default_telegram_text(payload)
    if not _event_message_template(cfg or {}, str(payload.get("event") or "")):
        return default_text
    title, separator, content = default_text.partition("\n")
    default = {"title": title, "content": content.lstrip("\n") if separator else ""}
    message = _render_notification_message(payload, cfg or {}, default)
    return "\n\n".join(part for part in (message["title"], message["content"]) if part)


def telegram_session(cfg: dict) -> requests.Session:
    mode = str(cfg.get("proxy_mode") or "direct").lower()
    session = requests.Session()
    session.trust_env = False
    if mode == "manual":
        # Kept for configurations saved before the shared proxy library was introduced.
        proxy = str(cfg.get("proxy_url") or "").strip()
        if not re.match(r"^(?:https?|socks5h?)://", proxy, re.IGNORECASE):
            raise ValueError("Telegram proxy must be an HTTP(S) or SOCKS5 URL")
        session.proxies.update({"http": proxy, "https": proxy})
    elif mode == "library":
        from . import config as settings_config
        settings = settings_config.get_settings()
        profile_id = str(cfg.get("proxy_profile_id") or "").strip()
        profile = ((settings.get("proxy") or {}).get("profiles") or {}).get(profile_id) or {}
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", profile_id) or not profile:
            raise RuntimeError("selected Telegram proxy is no longer in the proxy library")
        if profile.get("type") == "socks5":
            host = str(profile.get("server") or "").strip()
            try:
                port = int(profile.get("port") or 1080)
            except (TypeError, ValueError):
                port = 0
            if not host or not 1 <= port <= 65535 or any(ch in host for ch in "\r\n/@"):
                raise RuntimeError("selected Telegram SOCKS5 proxy is invalid")
            username = str(profile.get("username") or "")
            password = str(profile.get("password") or "")
            auth = f"{quote(username, safe='')}:{quote(password, safe='')}@" \
                if username or password else ""
            proxy = f"socks5h://{auth}{host}:{port}"
        else:
            exits = (settings.get("proxy") or {}).get("exits") or {}
            live = egress.status().get("exits") or {}
            state = next((live.get(country) or {} for country, exit_cfg in exits.items()
                          if isinstance(exit_cfg, dict) and exit_cfg.get("enabled")
                          and exit_cfg.get("profile_id") == profile_id
                          and (live.get(country) or {}).get("ready")), {})
            try:
                port = int(state.get("proxy_port") or 0)
            except (TypeError, ValueError):
                port = 0
            host = str(state.get("proxy_host") or "").strip()
            if not host or not 1 <= port <= 65535:
                raise RuntimeError("selected Telegram proxy has no ready country exit")
            proxy = f"socks5h://{host}:{port}"
        session.proxies.update({"http": proxy, "https": proxy})
    elif mode == "country":
        country = egress.normalize_country(cfg.get("proxy_country"))
        state = (egress.status().get("exits") or {}).get(country) or {}
        interface = str(state.get("interface") or "")
        try:
            proxy_port = int(state.get("proxy_port") or 0)
        except (TypeError, ValueError):
            proxy_port = 0
        if (not country or not state.get("ready")
                or not re.fullmatch(r"mdd-[a-z]{2}", interface)
                or not 1 <= proxy_port <= 65535):
            raise RuntimeError("selected country exit is not ready")
        # Binding a socket to the country TUN still resolves api.telegram.org through the
        # host's DNS first. On filtered networks that can yield a poisoned address even though
        # the selected node itself reaches Telegram. The orchestrator's country SOCKS inbound
        # follows the same verified outbound, while socks5h delegates DNS to sing-box.
        proxy_host = str(state.get("proxy_host") or "172.17.0.1").strip()
        if not proxy_host:
            raise RuntimeError("selected country exit is not ready")
        proxy = f"socks5h://{proxy_host}:{proxy_port}"
        session.proxies.update({"http": proxy, "https": proxy})
    elif mode != "direct":
        raise ValueError("Telegram proxy mode must be direct, library, country or legacy manual")
    return session


def remember_reply_target(message_id, payload: dict) -> None:
    """Record which line/peer a delivered SMS notification came from so a Telegram reply to
    notification delivery keeps the original sender context."""
    peer = str(payload.get("from") or "").strip()
    instance = str(payload.get("instance") or "").strip()
    if payload.get("event") != EV_INCOMING_SMS or not peer:
        return
    try:
        key = int(message_id)
    except (TypeError, ValueError):
        return
    with _HISTORY_LOCK:
        _REPLY_TARGETS[key] = {"instance": instance, "peer": peer}
        while len(_REPLY_TARGETS) > _REPLY_TARGET_LIMIT:
            _REPLY_TARGETS.popitem(last=False)


def reply_target(message_id) -> dict | None:
    """The {instance, peer} a Telegram message was a notification for, if still known."""
    if message_id is None:
        return None
    try:
        key = int(message_id)
    except (TypeError, ValueError):
        return None
    with _HISTORY_LOCK:
        target = _REPLY_TARGETS.get(key)
    return dict(target) if target else None


def send_telegram(cfg: dict, payload: dict) -> dict:
    token = (cfg.get("bot_token") or "").strip()
    chat = str(cfg.get("chat_id") or "").strip()
    if not token or not chat:
        raise ValueError("Telegram bot token and chat ID are required")
    session = telegram_session(cfg)
    try:
        response = session.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": _telegram_text(payload, cfg),
                  "disable_web_page_preview": True},
            timeout=_TIMEOUT)
        response.raise_for_status()
        body = response.json()
    except requests.RequestException:
        # Never include the requests exception: its URL contains the bot token.
        raise RuntimeError("Telegram request failed") from None
    except ValueError:
        body = {}
    finally:
        session.close()
    result = body.get("result") if isinstance(body, dict) else None
    if isinstance(result, dict):
        remember_reply_target(result.get("message_id"), payload)
    return {"ok": True, "status_code": response.status_code}


def _post_telegram(cfg: dict, payload: dict):
    try:
        send_telegram(cfg, payload)
        log.info("telegram %s delivered", payload.get("event"))
    except Exception as exc:  # noqa
        log.warning("telegram delivery failed: %s", type(exc).__name__)


def dispatch(settings: dict, event: str, instance: dict, source: str, text: str | None = None):
    """Queue independent deliveries on the notification pool and return their futures.

    HTTP and retry waits never run on the caller's thread. Each destination is gated on its
    own enable flag, event selection and line filter.
    """
    futures = []
    try:
        payload = build_payload(event, instance, source, text)
        deliveries = []
        wh = settings.get("webhook") or {}
        if wh.get("enabled") and _events_enabled(wh).get(event):
            deliveries.append(("webhook", send_webhook, wh))
        tg = settings.get("telegram") or {}
        if tg.get("enabled") and _events_enabled(tg).get(event):
            deliveries.append(("telegram", send_telegram, tg))
        pp = settings.get("pushplus") or {}
        if pp.get("enabled") and _events_enabled(pp).get(event):
            deliveries.append(("pushplus", send_pushplus, pp))
        for position, fs in enumerate(feishu_channels(settings.get("feishu") or {}), start=1):
            if (fs.get("enabled") and _events_enabled(fs).get(event)
                    and feishu_channel_matches(fs, payload.get("instance"))):
                channel_id = str(fs.get("id") or position)
                delivery_channel = "feishu" if channel_id == "legacy" else f"feishu:{channel_id}"
                deliveries.append((delivery_channel, send_feishu, fs))
        for channel, sender, channel_cfg in deliveries:
            futures.append(_DELIVERY_EXECUTOR.submit(
                _deliver_with_retry, channel, sender, deepcopy(channel_cfg), deepcopy(payload)))
    except Exception as e:  # noqa
        log.warning("push dispatch error: %r", e)
    return futures
