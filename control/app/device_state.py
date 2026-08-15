"""Unprivileged control channel for per-modem cellular and VoWiFi capabilities.

The control container writes desired state while the host orchestrator publishes observed state.
ModemManager is a host-wide dependency, so its shared state is reported separately
instead of pretending every modem owns an isolated copy of the service.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import time

from . import config as cfg

ROOT = os.path.join(cfg.DATA_DIR, "orchestrator")
DESIRED = os.path.join(ROOT, "devices-desired.json")
STATUS = os.path.join(ROOT, "devices-status.json")
HARDWARE = os.path.join(ROOT, "devices-hardware.json")

DEFAULT_CAPABILITIES = {"cellular_enabled": False, "vowifi_enabled": True,
                        "flight_mode": False}

_VPCD_MODEM_RE = re.compile(r"^VoWiFi Modem (.+?)\s+\d{2}\s+\d{2}$")


def vpcd_modem_hardware_id(reader_name: str | None) -> str:
    """Extract the physical modem id embedded in an orchestrator VPCD reader name."""
    match = _VPCD_MODEM_RE.fullmatch(str(reader_name or "").strip())
    return match.group(1) if match else ""


def native_reader_devices(cards: list[dict]) -> dict[str, dict]:
    """Return stable VoWiFi-only devices for native readers, excluding modem VPCD slots."""
    result = {}
    for card_info in cards:
        if vpcd_modem_hardware_id(card_info.get("name")):
            continue
        # The vsmartcard package's own reader definition. The installer disables it, but a
        # package reinstall or a hand edit can bring it back — and when it came back it
        # appeared here as two phantom card readers that exist whether or not any hardware
        # does. It is a software endpoint, never a reader an operator plugged in.
        if str(card_info.get("name") or "").startswith("Virtual PCD"):
            continue
        if card_info.get("hardware_kind") != "reader":
            continue
        identity = str(card_info.get("reader_port") or card_info.get("name") or "")
        if not identity:
            continue
        device_id = "reader-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        result[device_id] = card_info
    return result


def native_vowifi_capability(desired: bool, running: bool, line_status: dict | None) -> dict:
    """Map a native reader's engine state without inventing a cellular/bridge dependency."""
    if not desired:
        return {"desired": False, "actual": "stopping" if running else "off", "reason": ""}
    if not running:
        return {"desired": True, "actual": "degraded",
                "reason": "VoWiFi is configured but the line is not running"}
    raw = str((line_status or {}).get("state") or (line_status or {}).get("label") or "").lower()
    if raw in {"ok", "working", "registered"}:
        return {"desired": True, "actual": "on", "reason": ""}
    if raw in {"error", "failed", "stopped"}:
        return {"desired": True, "actual": "error",
                "reason": str((line_status or {}).get("reason") or "VoWiFi failed")}
    return {"desired": True, "actual": "starting", "reason": ""}


def logical_channel_view(identity: dict | None, bridge_active: bool) -> dict:
    """Normalize bridge metadata into a bounded per-UICC resource view for the UI."""
    identity = identity or {}
    try:
        capacity = max(1, min(3, int(identity.get("channel_capacity") or 3)))
    except (TypeError, ValueError):
        capacity = 3
    raw_items = identity.get("logical_channels") or []
    items = []
    roles = {"pin", "swu", "ims"}
    for value in raw_items[:capacity]:
        if not isinstance(value, dict):
            continue
        try:
            channel = int(value.get("channel"))
            slot = int(value.get("slot"))
        except (TypeError, ValueError):
            continue
        role = str(value.get("role") or "")
        if channel not in range(1, capacity + 1) or slot not in range(capacity) or role not in roles:
            continue
        items.append({"slot": slot, "channel": channel, "role": role})
    try:
        allocated = max(0, min(capacity, int(identity.get("channel_allocated", len(items)))))
    except (TypeError, ValueError):
        allocated = len(items)
    status = str(identity.get("channel_status") or "")
    if status not in {"allocating", "ready", "error", "stopped"}:
        status = "ready" if bridge_active else "stopped"
        # Older bridge metadata had only `slots`. During a rolling upgrade, expose
        # the occupied capacity without inventing specific channel IDs.
        allocated = capacity if bridge_active else 0
    if not bridge_active and status in {"allocating", "ready"}:
        status, allocated, items = "stopped", 0, []
    error = str(identity.get("channel_error") or "")[:300]
    return {"capacity": capacity, "allocated": allocated, "status": status,
            "items": items, "error": error}


def _read(path: str, default):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError, TypeError):
        return default


def _write(path: str, value):
    os.makedirs(ROOT, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def _capabilities(value: dict | None) -> dict:
    value = value or {}
    return {
        "cellular_enabled": bool(value.get("cellular_enabled", False)),
        "vowifi_enabled": bool(value.get("vowifi_enabled", True)),
        "flight_mode": bool(value.get("flight_mode", False)),
    }


def desired() -> dict:
    """Return normalized desired state keyed by stable physical hardware id."""
    value = _read(DESIRED, {})
    devices = value.get("devices") or {}
    return {
        "version": 2,
        "defaults": _capabilities(value.get("defaults") or DEFAULT_CAPABILITIES),
        "devices": {str(device_id): _capabilities(state)
                    for device_id, state in devices.items() if str(device_id)},
        "updated_at": value.get("updated_at"),
    }


def set_desired(device_id: str, *, cellular_enabled: bool | None = None,
                vowifi_enabled: bool | None = None,
                flight_mode: bool | None = None) -> dict:
    """Atomically update one modem without changing any other modem's request."""
    device_id = str(device_id).strip()
    if not device_id:
        raise ValueError("device_id is required")
    value = desired()
    current = dict(value["devices"].get(device_id) or value["defaults"])
    if cellular_enabled is not None:
        current["cellular_enabled"] = bool(cellular_enabled)
    if vowifi_enabled is not None:
        current["vowifi_enabled"] = bool(vowifi_enabled)
    if flight_mode is not None:
        current["flight_mode"] = bool(flight_mode)
    value["devices"][device_id] = current
    value["updated_at"] = int(time.time())
    _write(DESIRED, value)
    return current


def remove_desired(device_id: str) -> bool:
    """Forget the saved capability preferences for one physical device."""
    value = desired()
    removed = value["devices"].pop(str(device_id), None) is not None
    if removed:
        value["updated_at"] = int(time.time())
        _write(DESIRED, value)
    return removed


def hardware() -> dict[str, dict]:
    """User-managed physical-device metadata; never contains SIM identity."""
    value = _read(HARDWARE, {})
    devices = value.get("devices") or {}
    return {str(device_id): dict(record) for device_id, record in devices.items()
            if str(device_id) and isinstance(record, dict)}


def migrate_reader_records(live_readers: dict[str, dict]) -> list[tuple[str, str]]:
    """Move a saved reader record onto the id its hardware now derives.

    A reader's id comes from its USB port, so moving it to a hub mints a new one and strands
    the old record: the control plane unions saved records into its device list, rendering a
    connected reader twice — once live, once permanently offline, with nothing in the UI able
    to remove the copy.

    The record is moved rather than dropped because it carries the IMEI this line presents to
    the carrier, and the line refreshes that from here on every start. Matching is on the
    reader's own name, which contains its serial and therefore survives a change of port; a
    record is only moved when exactly one stranded record and one unclaimed live reader share
    that name, so an ambiguous set is left untouched for a person to resolve.
    """
    value = _read(HARDWARE, {})
    devices = value.get("devices") or {}
    saved_readers = {device_id: record for device_id, record in devices.items()
                     if isinstance(record, dict) and record.get("device_type") == "reader"}
    stranded = {device_id: record for device_id, record in saved_readers.items()
                if device_id not in live_readers}
    unclaimed = {device_id: card for device_id, card in live_readers.items()
                 if device_id not in saved_readers}
    moved = []
    for new_id, card in unclaimed.items():
        name = str(card.get("name") or "")
        if not name:
            continue
        matches = [old_id for old_id, record in stranded.items()
                   if str(record.get("name") or "") == name]
        if len(matches) != 1:
            continue
        old_id = matches[0]
        record = dict(stranded.pop(old_id))
        record["stable_path"] = str(card.get("reader_port") or "")
        record["updated_at"] = int(time.time())
        devices.pop(old_id, None)
        devices[new_id] = record
        moved.append((old_id, new_id))
    if moved:
        _write(HARDWARE, {"version": 1, "updated_at": int(time.time()), "devices": devices})
    return moved


def set_hardware(device_id: str, patch: dict) -> dict:
    device_id = str(device_id).strip()
    if not device_id:
        raise ValueError("device_id is required")
    value = _read(HARDWARE, {})
    devices = value.get("devices") or {}
    current = dict(devices.get(device_id) or {})
    current.update(dict(patch or {}))
    current["updated_at"] = int(time.time())
    devices[device_id] = current
    _write(HARDWARE, {"version": 1, "updated_at": int(time.time()), "devices": devices})
    return current


def remove_hardware(device_id: str) -> bool:
    value = _read(HARDWARE, {})
    devices = value.get("devices") or {}
    removed = devices.pop(str(device_id), None) is not None
    if removed:
        _write(HARDWARE, {"version": 1, "updated_at": int(time.time()), "devices": devices})
    return removed


def set_defaults(*, cellular_enabled: bool | None = None,
                 vowifi_enabled: bool | None = None,
                 flight_mode: bool | None = None) -> dict:
    """Update defaults used only for hardware IDs discovered in the future."""
    value = desired()
    current = dict(value["defaults"])
    if cellular_enabled is not None:
        current["cellular_enabled"] = bool(cellular_enabled)
    if vowifi_enabled is not None:
        current["vowifi_enabled"] = bool(vowifi_enabled)
    if flight_mode is not None:
        current["flight_mode"] = bool(flight_mode)
    value["defaults"] = current
    value["updated_at"] = int(time.time())
    _write(DESIRED, value)
    return current


def status() -> dict:
    """Observed state; absence is represented explicitly, never as a false success."""
    value = _read(STATUS, {})
    if value:
        return value
    return {"version": 2, "devices": {}, "shared": {
        "cellular_backend": "unavailable", "modemmanager_active": False,
        "transitioning": False,
    }, "updated_at": None}
