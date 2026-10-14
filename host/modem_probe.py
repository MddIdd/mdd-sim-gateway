"""Find and test a USB modem that is not in the gateway's list of known models.

Shared by the native orchestrator and the container Hardware service. A modem is recognised
by vid/pid plus the USB interface of its AT port; anything else is dropped silently, so a
module the list does not name can never be used. This lists such devices and, only when the
operator asks, tests one with the same commands the SIM bridge relies on.

The test is deliberately brand-neutral. The bridge reaches the SIM with nothing but
3GPP TS 27.007 AT+CSIM (logical channels are MANAGE CHANNEL APDUs sent through it), so a
module that answers AT and lets AT+CSIM open and close a logical channel can carry VoWiFi,
whoever made it.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

try:
    from host import vpcd_modem_bridge as bridge
except ImportError:  # run as host/mdd_orchestrator.py, with host/ itself on the path
    import vpcd_modem_bridge as bridge

USB_ROOT = Path("/sys/bus/usb/devices")
TTY_RE = re.compile(r"tty(?:USB|ACM)\d+")
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_.-]{1,120}")
USB_PATH_RE = re.compile(r"\d+-[\d.]+")
HUB_CLASS = "09"
# The candidate list is published on every pass; ModemManager is only asked about devices
# that are unknown and have a serial interface, and even then no more often than this.
SCAN_MAX_AGE = 30.0
# Status documents are only read while the WebUI waits for them.
STATUS_RETENTION = 3600.0
SERIAL_TIMEOUT = 2.0
MODEMMANAGER_TIMEOUT = 8.0
OPEN_CHANNEL = bytes.fromhex("0070000001")

def esim_identity_matches(observed: dict, expected: dict) -> bool:
    """Match a readable ICCID exactly, falling back to IMSI only when absent."""
    expected_iccid = str(expected.get("iccid") or "").strip()
    observed_iccid = str(observed.get("iccid") or "").strip()
    if expected_iccid.isdigit() and observed_iccid.isdigit():
        return observed_iccid == expected_iccid
    expected_imsi = str(expected.get("imsi") or "").strip()
    observed_imsi = str(observed.get("imsi") or "").strip()
    return bool(expected_imsi.isdigit() and observed_imsi == expected_imsi)


# Outcomes. "usable" and "unverified" are saved as a model; the rest are explained instead.
USABLE = "usable"            # AT answered and AT+CSIM opened and closed a logical channel
UNVERIFIED = "unverified"    # AT answered, but no ready SIM to try a logical channel on
UNSUPPORTED = "unsupported"  # a ready SIM refused the logical channel through AT+CSIM
NO_AT_PORT = "no_at_port"
PORT_BUSY = "port_busy"
MODEMMANAGER_FAILED = "modemmanager_failed"
NOT_FOUND = "not_found"
SAVEABLE = {USABLE, UNVERIFIED}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _atomic_json(path: Path, value: dict):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def tty_interfaces(usb: Path) -> dict[int, str]:
    """USB interface number -> the tty on it. ttyUSB sits directly under the interface
    directory, ttyACM one level down under tty/."""
    result = {}
    pattern = re.compile(rf"{re.escape(usb.name)}:\d+\.(\d+)")
    try:
        entries = list(usb.parent.iterdir())
    except OSError:
        return result
    for interface in entries:
        match = pattern.fullmatch(interface.name)
        if not match:
            continue
        names = []
        for directory in (interface, interface / "tty"):
            try:
                names += [p.name for p in directory.iterdir() if TTY_RE.fullmatch(p.name)]
            except OSError:
                pass
        if names:
            result[int(match.group(1))] = sorted(names)[0]
    return result


def modemmanager_modems(run) -> dict[str, dict]:
    """USB path -> the ModemManager object for it and the ttys it uses as AT ports."""
    listing = run(["mmcli", "-L"])
    if getattr(listing, "returncode", 1):
        return {}
    result = {}
    for obj in re.findall(r"(/org/freedesktop/ModemManager1/Modem/\d+)", listing.stdout or ""):
        detail = run(["mmcli", "-m", obj, "--output-keyvalue"])
        if getattr(detail, "returncode", 1):
            continue
        text = detail.stdout or ""
        device = re.search(r"^modem\.generic\.device\s*:\s*(\S+)", text, re.MULTILINE)
        if not device:
            continue
        ports = re.findall(r"^modem\.generic\.ports\.value\[\d+\]\s*:\s*(\S+)\s+\(at\)",
                           text, re.MULTILINE)
        primary = re.search(r"^modem\.generic\.primary-port\s*:\s*(\S+)", text, re.MULTILINE)
        # The primary port first: it is the one ModemManager sends --command to.
        if primary and primary.group(1) in ports:
            ports.remove(primary.group(1))
            ports.insert(0, primary.group(1))
        result[Path(device.group(1)).name] = {"object": obj, "at_ports": ports}
    return result


def list_candidates(known: set[tuple[str, str]], run=None, usb_root: Path = USB_ROOT) -> list[dict]:
    """USB devices that look like a modem but match no configured model.

    A lone serial interface is a USB-serial cable, a GPS or a UPS far more often than a
    modem, so a device is only offered when ModemManager has claimed it or it exposes at
    least two serial interfaces, as cellular modules do.
    """
    unknown = []
    try:
        devices = sorted(usb_root.iterdir())
    except OSError:
        return []
    for usb in devices:
        if ":" in usb.name or not USB_PATH_RE.fullmatch(usb.name):
            continue
        vid, pid = _read(usb / "idVendor").lower(), _read(usb / "idProduct").lower()
        if not vid or not pid or (vid, pid) in known or _read(usb / "bDeviceClass") == HUB_CLASS:
            continue
        interfaces = tty_interfaces(usb)
        if interfaces:
            unknown.append((usb, vid, pid, interfaces))
    if not unknown:
        return []
    claimed = modemmanager_modems(run) if run else {}
    result = []
    for usb, vid, pid, interfaces in unknown:
        owner = claimed.get(usb.name) or {}
        if not owner and len(interfaces) < 2:
            continue
        result.append({
            "usb_path": usb.name, "vid": vid, "pid": pid,
            "manufacturer": _read(usb / "manufacturer"), "product": _read(usb / "product"),
            "interfaces": {str(number): tty for number, tty in sorted(interfaces.items())},
            "mm_object": owner.get("object") or "",
            "mm_at_ports": list(owner.get("at_ports") or []),
        })
    return result


def display_name(candidate: dict) -> str:
    product = str(candidate.get("product") or "").strip()
    maker = str(candidate.get("manufacturer") or "").strip()
    if product and maker and maker.lower() not in product.lower():
        product = f"{maker} {product}"
    return (product or f"USB modem {candidate.get('vid')}:{candidate.get('pid')}")[:80]


def open_serial_card(port: str):
    return bridge.ModemCard(port, timeout=SERIAL_TIMEOUT)


def open_modemmanager_card(obj: str):
    return bridge.ModemManagerCard(obj, timeout=MODEMMANAGER_TIMEOUT)


def _outcome(result: str, detail: str, steps: list[str], interface: int | None = None) -> dict:
    value = {"result": result, "detail": detail[:300], "steps": steps[-20:]}
    if interface is not None:
        value["at_interface"] = interface
    return value


def _check_sim(card, interface: int, steps: list[str]) -> dict:
    """Open one logical channel through AT+CSIM and close it again at once."""
    try:
        response = card.csim(OPEN_CHANNEL)
    except bridge.ModemError as exc:
        response, error = b"", str(exc)
    else:
        error = ""
    if len(response) == 3 and response[-2:] == b"\x90\x00":
        card.close_channel(response[0])
        steps.append(f"AT+CSIM MANAGE CHANNEL: opened and closed channel {response[0]}")
        return _outcome(USABLE, "", steps, interface)
    steps.append("AT+CSIM MANAGE CHANNEL: " + (error or f"status {response.hex() or 'none'}"))
    try:
        pin = card._at("AT+CPIN?").decode("ascii", "replace")
    except bridge.ModemError as exc:
        pin = str(exc)
    pin = " ".join(pin.split())
    steps.append(f"AT+CPIN?: {pin}")
    if "READY" not in pin.upper():
        # No card, or one still behind its PIN: the channel test says nothing yet.
        return _outcome(UNVERIFIED, pin, steps, interface)
    return _outcome(UNSUPPORTED, error or response.hex(), steps, interface)


def probe(candidate: dict, serial_card=open_serial_card, modemmanager_card=open_modemmanager_card) -> dict:
    """Find the AT port of ``candidate`` and test SIM access through it."""
    steps: list[str] = []
    interfaces = {int(number): tty for number, tty in (candidate.get("interfaces") or {}).items()}
    obj = str(candidate.get("mm_object") or "")
    card = None
    interface = None
    try:
        if obj:
            # ModemManager owns every port of a modem it claimed. Going through it, like the
            # bridge does, avoids racing its own traffic on the tty.
            by_tty = {tty: number for number, tty in interfaces.items()}
            interface = next((by_tty[tty] for tty in candidate.get("mm_at_ports") or []
                              if tty in by_tty), None)
            if interface is None:
                return _outcome(NO_AT_PORT, "ModemManager reports no AT port", steps)
            try:
                card = modemmanager_card(obj)
            except bridge.ModemError as exc:
                steps.append(f"mmcli --command=AT: {exc}")
                return _outcome(MODEMMANAGER_FAILED, str(exc), steps)
            steps.append(f"interface {interface} ({interfaces[interface]}) via ModemManager: AT OK")
        else:
            busy = []
            for number, tty in sorted(interfaces.items()):
                try:
                    card = serial_card(f"/dev/{tty}")
                except (bridge.ModemError, OSError) as exc:
                    text = " ".join(str(exc).split())
                    if "lock" in text.lower() or "busy" in text.lower():
                        busy.append(tty)
                    steps.append(f"interface {number} ({tty}): {text[:120]}")
                    continue
                interface = number
                steps.append(f"interface {number} ({tty}): AT OK")
                break
            if card is None:
                if busy:
                    return _outcome(PORT_BUSY, "in use: " + ", ".join(busy), steps)
                return _outcome(NO_AT_PORT, "no serial interface answered AT", steps)
        return _check_sim(card, interface, steps)
    finally:
        if card is not None:
            card.close()


class CandidateScanner:
    """Publishes the candidate list without asking ModemManager on every pass."""

    def __init__(self, path: Path):
        self.path = path
        self._key = None
        self._at = 0.0
        self.candidates: list[dict] = []

    def scan(self, known: set[tuple[str, str]], run, usb_root: Path = USB_ROOT) -> list[dict]:
        try:
            tree = (usb_root.stat().st_mtime, len(os.listdir(usb_root)))
        except OSError:
            tree = (0.0, 0)
        key = (tree, tuple(sorted(known)))
        now = time.monotonic()
        if key == self._key and now - self._at < SCAN_MAX_AGE:
            return self.candidates
        self.candidates = list_candidates(known, run, usb_root)
        self._key, self._at = key, now
        _atomic_json(self.path, {"updated_at": time.time(), "candidates": self.candidates})
        return self.candidates


class ProbeRequests:
    """The request/status handshake with Control, one file per request."""

    def __init__(self, root: Path):
        self.request_dir = root / "modem-probe-requests"
        self.status_dir = root / "modem-probe-status"

    def _status(self, request_id: str, state: str, **extra):
        _atomic_json(self.status_dir / f"{request_id}.json",
                     {"request_id": request_id, "state": state, "updated_at": time.time(), **extra})

    def process(self, candidates: list[dict], log=print, **probe_options):
        try:
            paths = sorted(self.request_dir.glob("*.json"))
        except OSError:
            paths = []
        for path in paths:
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                request = {}
            try:
                path.unlink()
            except OSError:
                pass
            request_id = str(request.get("request_id") or "")
            if not REQUEST_ID_RE.fullmatch(request_id) or request_id != path.stem:
                continue
            usb_path = str(request.get("usb_path") or "")
            candidate = next((item for item in candidates if item["usb_path"] == usb_path
                              and item["vid"] == str(request.get("vid") or "").lower()
                              and item["pid"] == str(request.get("pid") or "").lower()), None)
            if candidate is None:
                self._status(request_id, "done", usb_path=usb_path,
                             **_outcome(NOT_FOUND, "device is no longer present or already known", []))
                continue
            self._status(request_id, "probing", usb_path=usb_path)
            try:
                outcome = probe(candidate, **probe_options)
            except Exception as exc:  # a probe must never take the reconcile loop down
                outcome = _outcome(NO_AT_PORT, f"probe failed: {exc}", [])
            self._status(request_id, "done", usb_path=usb_path, vid=candidate["vid"],
                         pid=candidate["pid"], name=display_name(candidate), **outcome)
            log(f"modem probe {candidate['vid']}:{candidate['pid']} at {usb_path}: "
                f"{outcome['result']}")
        self._expire()

    def _expire(self):
        cutoff = time.time() - STATUS_RETENTION
        try:
            for path in self.status_dir.glob("*.json"):
                if path.stat().st_mtime < cutoff:
                    path.unlink()
        except OSError:
            pass
