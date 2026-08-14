"""
config.py - Persistent manager state (global settings + per-SIM instances).

Stored as YAML at $MDD_DATA/config.yaml. Threadsafe-ish via a module lock; the
manager is single-process. Instances describe SIMs; render_instance_json() converts an
instance into the engine's /config/instance.json contract.
"""
from __future__ import annotations

import json
import hashlib
import ipaddress
import os
import re
import secrets
import socket
import threading
import urllib.parse
from copy import deepcopy

import yaml

DATA_DIR = os.environ.get("MDD_DATA", os.path.join(os.getcwd(), "data"))
CONFIG_PATH = os.path.join(DATA_DIR, "config.yaml")
_lock = threading.RLock()

# Product safety boundary. This is intentionally a source-level limit rather than an environment
# variable: operators must not be able to turn the gateway into a bulk-SIM service by changing
# deployment configuration.
MAX_SIM_LINES = 5


class LineLimitError(ValueError):
    pass


def _private_dir(path: str) -> None:
    """Create a runtime directory and keep it inaccessible to non-root host users."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def _private_text_writer(path: str):
    """Open a file for atomic private-state writes without a world-readable umask window."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")

DEFAULTS = {
    "internal": {},
    "settings": {
        "timezone": "Asia/Shanghai",
        "device_defaults": {"cellular_enabled": False, "vowifi_enabled": True},
        "http_port": 8443,
        "bind": "0.0.0.0",
        "tls": {"self_signed": True, "domain": "", "cert_path": "", "key_path": ""},
        "debug": {"asterisk": False, "charon": False, "pcap": False, "ami": False},
        "manager_url": "",          # reachable URL engines POST events to (auto if empty)
        "retry": {"max": 3, "interval": 30},   # auto-retry attempts + seconds per attempt
        # Proactive IKEv2 SA rekey. IKEv2 does NOT negotiate SA lifetime on the wire (RFC 7296
        # dropped it), so rekey timing is local policy (3GPP TS 24.302 clause 7.2.2C: use a
        # configured value, else an implementation value). We rekey the CHILD (ESP) SA every
        # `minutes` from its establishment. 0 disables proactive rekey (passive only — the SA
        # is only refreshed if the ePDG initiates a rekey). Default 30 min.
        "rekey": {"minutes": 30},
        # Outbound ring timeout (s): how long Asterisk lets an outgoing call ring before it
        # gives up and CANCELs. 35 covers a normal answer window; most carriers roll to
        # voicemail by ~30s. Shorter = the callee is re-alerted fewer times when unanswered.
        "ring_timeout": 35,
        # Country-aware outer ePDG routing. Disabled preserves the legacy host routing until the
        # host-side orchestrator is installed/configured. When enabled, a line fails closed if
        # its SIM country has no healthy exit (unless that country explicitly selects direct).
        "proxy": {
            "schema_version": 2,
            "enabled": False,
            "missing_policy": "error",
            "profiles": {},
            "subscription_url": "",
            "existing_singbox_config": "",
            "refresh_minutes": 30,
            "exits": {},
        },
        # Host hardware orchestration: known modem profiles are turned into three internal VPCD
        # logical slots; native PC/SC readers pass through untouched.
        "hardware": {
            "auto_detect": True,
            "vpcd_slots": 3,
            "modem_profiles": [
                {"name": "DJI/Quectel EC25", "vid": "2c7c", "pid": "0125",
                 "at_interface": 2},
            ],
        },
        # Outbound push notifications for incoming events (SMS / calls). Every channel is
        # independent and gated on their own `enabled` flag + per-event checkboxes.
        "webhook": {
            "enabled": False,
            "format": "generic",
            "method": "POST",
            "body_mode": "json",
            "url": "",
            "headers_json": "{}",
            "payload_template": "",
            "verify_tls": True,
            "events": {"incoming_sms": True, "incoming_call": True,
                       "activation_reminder": True},
        },
        "telegram": {
            "enabled": False,
            "bot_token": "",
            "chat_id": "",
            "proxy_mode": "direct",
            "proxy_url": "",
            "proxy_country": "",
            "events": {"incoming_sms": True, "incoming_call": True,
                       "activation_reminder": True},
        },
        "pushplus": {
            "enabled": False,
            "token": "",
            "topic": "",
            "template": "html",
            "channel": "wechat",
            "events": {"incoming_sms": True, "incoming_call": True,
                       "activation_reminder": True},
        },
        "security": {
            "https_only": True,
            "trusted_proxies": [],
            "audit_enabled": True,
        },
        "maintenance": {
            "notification_history_days": 30,
            "support_bundle_log_lines": 500,
        },
        # Software update traffic is direct by default. Operators on filtered networks can
        # opt into a manual proxy or reuse one of the host orchestrator's country exits.
        "updates": {
            "proxy_mode": "direct",
            "proxy_url": "",
            "proxy_country": "",
        },
        # Local lpac (eSIM LPA) integration. Binary is built by `./install.sh build-lpac` into
        # $MDD_DATA/lpac/ (STANDALONE layout). Empty lpac_bin → default path below.
        "esim": {
            "lpac_bin": "",
            "download_timeout": 300,
            "auto_process_notifications": True,
        },
    },
    "instances": {},
}


def default_lpac_bin() -> str:
    """Default path for the locally-built STANDALONE lpac binary."""
    return os.path.join(DATA_DIR, "lpac", "lpac")


def internal_event_token() -> str:
    """Return a persistent secret used only for engine-to-manager callbacks."""
    with _lock:
        data = load()
        token = str((data.get("internal") or {}).get("event_token") or "")
        if token:
            return token
        token = secrets.token_urlsafe(32)
        data["internal"] = {**(data.get("internal") or {}), "event_token": token}
        save(data)
        return token

# Port block allocation per instance index (avoids collisions across SIMs)
PORT_BASE = {"sip_udp": 5060, "sip_tls": 5061, "webrtc": 8089, "ami": 5038,
             "rtp_start": 10000, "rtp_end": 11000}
PORT_STRIDE = {"sip_udp": 10, "sip_tls": 10, "webrtc": 10, "ami": 10,
               "rtp_start": 2000, "rtp_end": 2000}


def _host_lan_ipv4() -> str:
    """Best-effort primary LAN IPv4 of the host the manager runs on. Used as the address
    Asterisk advertises to LOCAL SIP clients (Contact + SDP), so a LAN MicroSIP can route
    in-dialog requests (BYE) back to the published host port instead of the unroutable
    docker-bridge container IP. Uses a UDP connect (no traffic sent) to learn the source
    address the kernel would pick for outbound; returns "" if it can't be determined."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        return ip if ip and not ip.startswith("127.") else ""
    except Exception:
        return ""
    finally:
        s.close()


def ims_realm(mcc: str, mnc: str) -> str:
    """The carrier's IMS home-network realm, derived purely from the SIM's MCC/MNC per the
    3GPP naming scheme: ims.mnc<MNC>.mcc<MCC>.3gppnetwork.org, with the MNC zero-padded to 3
    digits (matches the engine's render.py so control-side SMS/AMI addressing and the engine's
    registration realm agree, including for 2-digit-MNC carriers)."""
    return f"ims.mnc{str(mnc).zfill(3)}.mcc{str(mcc)}.3gppnetwork.org"


def advertise_address(settings: dict) -> str:
    """The host-reachable address to advertise to local SIP clients. Precedence: explicit
    TLS domain (already used as the TLS external address) > MDD_ADVERTISE_ADDR env >
    settings.advertise_address > auto-detected host LAN IPv4.

    The env override matters when the control plane itself runs in a (bridge-networked)
    container: _host_lan_ipv4() would then return the container's docker-bridge IP, not the
    host LAN IP a SIP/WebRTC client must reach. The installer passes the real host IP in
    MDD_ADVERTISE_ADDR."""
    tls_domain = (settings.get("tls", {}) or {}).get("domain", "")
    return (tls_domain or os.environ.get("MDD_ADVERTISE_ADDR", "")
            or settings.get("advertise_address", "") or _host_lan_ipv4())


def ice_advertise_address(settings: dict) -> str:
    """Return a literal host IP for Asterisk's ICE candidate rewrite.

    PJSIP external signaling/media addresses may be DNS names, so ``advertise_address``
    correctly prefers the configured TLS domain.  ``rtp.conf``'s ice_host_candidates parser,
    however, accepts only an IP address; feeding the domain there discards the mapping and can
    leave a browser with only the unroutable Docker address.  Prefer the installer's explicit
    host address and ignore non-IP values before falling back to the detected LAN IPv4.
    """
    for value in (os.environ.get("MDD_ADVERTISE_ADDR", ""),
                  settings.get("advertise_address", ""), _host_lan_ipv4()):
        candidate = str(value or "").strip().strip("[]")
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return ""


def _ensure():
    _private_dir(DATA_DIR)
    if not os.path.exists(CONFIG_PATH):
        with _private_text_writer(CONFIG_PATH) as f:
            yaml.safe_dump(DEFAULTS, f)
    os.chmod(CONFIG_PATH, 0o600)


def load() -> dict:
    with _lock:
        _ensure()
        with open(CONFIG_PATH) as f:
            data = yaml.safe_load(f) or {}
        # merge defaults (shallow for settings)
        out = deepcopy(DEFAULTS)
        out["settings"].update(data.get("settings", {}))
        if "tls" in data.get("settings", {}):
            out["settings"]["tls"] = {**DEFAULTS["settings"]["tls"], **data["settings"]["tls"]}
        out["settings"]["retry"] = {**DEFAULTS["settings"]["retry"],
                                    **(data.get("settings", {}).get("retry", {}))}
        out["settings"]["rekey"] = {**DEFAULTS["settings"]["rekey"],
                                    **(data.get("settings", {}).get("rekey", {}))}
        out["settings"]["debug"] = {**DEFAULTS["settings"]["debug"],
                                    **(data.get("settings", {}).get("debug", {}))}
        # notification channels: merge one level deep (like tls/retry) so a saved config that
        # predates these keys — or omits the nested `events` map — still gets full defaults.
        for key in ("webhook", "telegram", "pushplus"):
            saved = data.get("settings", {}).get(key, {}) or {}
            merged = {**DEFAULTS["settings"][key], **saved}
            merged["events"] = {**DEFAULTS["settings"][key]["events"],
                                **(saved.get("events", {}) or {})}
            out["settings"][key] = merged
        # Telegram is notification-only. Drop command settings left by an older configuration
        # so an upgrade cannot preserve a remote call/SMS control channel.
        out["settings"]["telegram"].pop("commands", None)
        # One private deployment previously appeared as a product-level "Universal Push"
        # preset. Present it as the ordinary custom webhook it really is, preserving its URL,
        # source field and token header. Saving Settings persists the standard representation.
        webhook = out["settings"]["webhook"]
        if webhook.get("format") == "universal_push":
            source = str(webhook.pop("source", "") or "otptool")
            token = str(webhook.pop("token", "") or "")
            try:
                headers = json.loads(webhook.get("headers_json") or "{}")
                if not isinstance(headers, dict):
                    headers = {}
            except (TypeError, ValueError):
                headers = {}
            if token:
                headers.setdefault("X-App-Token", token)
            webhook.update({
                "format": "custom",
                "body_mode": "json",
                "headers_json": json.dumps(headers, ensure_ascii=False),
                "payload_template": json.dumps({
                    "source": source,
                    "title": "{{title}}",
                    "content": "{{content}}",
                }, ensure_ascii=False),
            })
        esim_saved = data.get("settings", {}).get("esim", {}) or {}
        out["settings"]["esim"] = {**DEFAULTS["settings"]["esim"], **esim_saved}
        for key in ("proxy", "hardware", "security", "maintenance", "device_defaults",
                    "updates"):
            saved = data.get("settings", {}).get(key, {}) or {}
            out["settings"][key] = {**DEFAULTS["settings"][key], **saved}
        # Proxy profiles were introduced after the original single-subscription/country-form
        # layout.  Expose a lossless v2 view immediately, but do not rewrite config.yaml until
        # the operator next saves Settings.
        proxy = out["settings"]["proxy"]
        profiles = deepcopy(proxy.get("profiles") or {})
        exits = deepcopy(proxy.get("exits") or {})
        legacy_url = str(proxy.get("subscription_url") or "").strip()
        if legacy_url and "legacy-subscription" not in profiles:
            profiles["legacy-subscription"] = {
                "name": "Original subscription", "type": "subscription",
                "url": legacy_url, "refresh_minutes": int(proxy.get("refresh_minutes") or 30),
            }
        for country, exit_cfg in list(exits.items()):
            if not isinstance(exit_cfg, dict) or exit_cfg.get("profile_id"):
                continue
            mode = str(exit_cfg.get("mode") or "subscription").lower()
            if mode == "subscription" and legacy_url:
                exit_cfg["profile_id"] = "legacy-subscription"
            elif mode == "manual":
                profile_id = f"legacy-{country}"
                value = exit_cfg.get("outbound_json") or exit_cfg.get("proxy_url") or ""
                kind = "socks5" if str(value).lower().startswith(("socks://", "socks5://")) else "node"
                migrated = {"name": f"{str(country).upper()} legacy proxy", "type": kind}
                if kind == "socks5":
                    parsed = urllib.parse.urlsplit(str(value))
                    migrated.update({"server": parsed.hostname or "", "port": parsed.port or 1080,
                                     "username": urllib.parse.unquote(parsed.username or ""),
                                     "password": urllib.parse.unquote(parsed.password or "")})
                else:
                    migrated["value"] = value
                profiles.setdefault(profile_id, migrated)
                exit_cfg["profile_id"] = profile_id
            elif mode == "existing":
                profile_id = f"legacy-{country}"
                profiles.setdefault(profile_id, {"name": f"{str(country).upper()} imported outbound",
                                                  "type": "existing",
                                                  "outbound_tag": exit_cfg.get("outbound_tag") or ""})
                exit_cfg["profile_id"] = profile_id
        proxy["schema_version"] = 2
        proxy["profiles"] = profiles
        proxy["exits"] = exits
        # Asterisk debug includes complete SIP messages and IMS identities.  Older manual
        # provisioning forms accidentally enabled it by default, so normalize every loaded
        # line as well as new writes; this makes an upgrade safe before the operator next edits
        # the line and prevents a stale saved value from reaching an engine restart.
        out["instances"] = deepcopy(data.get("instances", {}))
        for inst in out["instances"].values():
            inst["debug"] = {**(inst.get("debug") or {}), "asterisk": False}
            # Browser WebRTC remains available through the authenticated Web UI, but the
            # the product never provisions standalone SIP accounts.
            (inst.setdefault("sip", {}))["external"] = []
        out["internal"] = data.get("internal", {})
        return out


def esim_settings() -> dict:
    """Resolved eSIM/lpac settings (fills empty lpac_bin with the default path)."""
    s = dict(get_settings().get("esim") or {})
    if not (s.get("lpac_bin") or "").strip():
        s["lpac_bin"] = default_lpac_bin()
    return s


def save(data: dict):
    with _lock:
        _private_dir(DATA_DIR)
        tmp = CONFIG_PATH + ".tmp"
        with _private_text_writer(tmp) as f:
            yaml.safe_dump(data, f, sort_keys=False)
        os.replace(tmp, CONFIG_PATH)
        os.chmod(CONFIG_PATH, 0o600)


def get_settings() -> dict:
    return load()["settings"]


def update_settings(patch: dict) -> dict:
    data = load()
    data["settings"].update(patch)
    save(data)
    return data["settings"]


def list_instances() -> list:
    return list(load()["instances"].values())


def get_instance(iid: str) -> dict | None:
    return load()["instances"].get(str(iid))


def _alloc_ports(index: int) -> dict:
    """The nominal port block for an instance index (no conflict checking)."""
    block = {k: PORT_BASE[k] + index * PORT_STRIDE[k] for k in PORT_BASE}
    block["rtp_span"] = DEFAULT_RTP_SPAN
    return block


# New lines only need a small RTP pool: one call normally consumes an RTP/RTCP pair. Existing
# lines predate the explicit field and retain the historical 60-port pool on upgrade; silently
# shrinking them could break an installation with multiple concurrent browser calls.
DEFAULT_RTP_SPAN = 12
LEGACY_RTP_SPAN = 60
# Valid user-selectable SIP port range (avoid well-known/privileged ports).
MIN_USER_PORT, MAX_USER_PORT = 1024, 65535


def rtp_span(block: dict) -> int:
    """Effective published RTP width, with backward compatibility for saved port blocks."""
    default = LEGACY_RTP_SPAN if "rtp_span" not in block else DEFAULT_RTP_SPAN
    try:
        return max(2, min(LEGACY_RTP_SPAN, int(block.get("rtp_span", default))))
    except (TypeError, ValueError):
        return default


def _block_ports(block: dict) -> set[int]:
    """Every host port a port-block occupies: the 4 fixed services + the RTP span."""
    used = {block["sip_udp"], block["sip_tls"], block["webrtc"], block["ami"]}
    used |= set(range(block["rtp_start"], block["rtp_start"] + rtp_span(block)))
    return used


def _reserved_ports(data: dict, exclude_iid: str | None = None) -> set[int]:
    """All host ports already reserved by OTHER instances (from their stored port blocks)."""
    used: set[int] = set()
    for iid, inst in data["instances"].items():
        if exclude_iid is not None and str(iid) == str(exclude_iid):
            continue
        p = inst.get("ports")
        if p:
            used |= _block_ports(p)
    return used


def _host_port_free(port: int) -> bool:
    """True if the host isn't already LISTENing on this TCP or UDP port. Best-effort:
    we try to bind; EADDRINUSE => taken. Uses SO_REUSEADDR off so an active listener
    is detected. Any unexpected error is treated as 'free' (don't block provisioning)."""
    for fam, typ in ((socket.AF_INET, socket.SOCK_STREAM), (socket.AF_INET, socket.SOCK_DGRAM)):
        s = socket.socket(fam, typ)
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return False
        except Exception:
            pass
        finally:
            s.close()
    return True


def _block_free(block: dict, reserved: set[int]) -> bool:
    """A candidate block is usable if none of its ports collide with reserved ports and
    none of its 4 service ports are already listening on the host."""
    bp = _block_ports(block)
    if bp & reserved:
        return False
    # Only probe the 4 service ports on the host (probing 60 RTP ports every try is slow;
    # RTP conflicts are caught by the reserved-set check against other instances).
    for port in (block["sip_udp"], block["sip_tls"], block["webrtc"], block["ami"]):
        if not _host_port_free(port):
            return False
    return True


def alloc_ports_auto(data: dict, exclude_iid: str | None = None) -> dict:
    """Automatic port allocation: scan index blocks from 0 upward and take the first whose
    whole port block collides with neither another instance nor a live host listener. This
    is the default behaviour. Starting at 0 (not next_index) means re-provisioning a line
    back to Auto reclaims the lowest free block instead of drifting ever upward."""
    reserved = _reserved_ports(data, exclude_iid)
    for index in range(0, 500):                    # generous bound; ~500 lines is absurd
        block = _alloc_ports(index)
        if max(_block_ports(block)) > MAX_USER_PORT:
            break
        if _block_free(block, reserved):
            return block
    raise ValueError("no free port block available for a new line")


def ports_from_sip_base(data: dict, sip_udp: int, exclude_iid: str | None = None) -> dict:
    """Manual port selection: the user picks the SIP UDP port; the rest of the block is
    derived from it at the same offsets as the nominal layout, so one number configures
    the whole line. Validates range and checks the whole derived block for conflicts.
    Raises ValueError with a user-facing message on any problem."""
    if not isinstance(sip_udp, int):
        raise ValueError("port must be a number")
    if not (MIN_USER_PORT <= sip_udp <= MAX_USER_PORT):
        raise ValueError(f"port must be between {MIN_USER_PORT} and {MAX_USER_PORT}")
    # Derive the block from the SIP UDP base using the nominal per-service offsets.
    base0 = _alloc_ports(0)
    off = {k: base0[k] - base0["sip_udp"] for k in PORT_BASE}   # sip_udp offset = 0
    block = {k: sip_udp + off[k] for k in PORT_BASE}
    block["rtp_span"] = DEFAULT_RTP_SPAN
    if max(_block_ports(block)) > MAX_USER_PORT:
        raise ValueError(f"port {sip_udp} is too high — its RTP range would exceed {MAX_USER_PORT}")
    reserved = _reserved_ports(data, exclude_iid)
    clash = _block_ports(block) & reserved
    if clash:
        if sip_udp in clash or block["sip_tls"] in clash:
            raise ValueError(f"port {sip_udp} is already used by another line. "
                             f"Choose a different port or use Automatic.")
        # A derived service/RTP port (WebRTC/control/RTP) overlaps a neighbouring line's
        # block. Tell the user what to avoid without exposing internal port math.
        raise ValueError(f"port {sip_udp} overlaps another line's port range "
                         f"(conflict at {min(clash)}). Try a port at least 10 away, or "
                         f"use Automatic.")
    for port, name in ((block["sip_udp"], "SIP/UDP"), (block["sip_tls"], "SIP/TLS"),
                       (block["webrtc"], "WebRTC"), (block["ami"], "control")):
        if not _host_port_free(port):
            raise ValueError(f"port {port} ({name}) is already in use on the host. "
                             f"Choose a different port or use Automatic.")
    return block


def next_index(data: dict) -> int:
    used = {inst.get("index", 0) for inst in data["instances"].values()}
    i = 0
    while i in used:
        i += 1
    return i


def default_instance_name(mcc: str, mnc: str, iccid: str) -> str:
    """The generated label for a new line: carrier plus the SIM's last serial digits, e.g.
    `234-10-4409`. MCC/MNC alone repeats for every SIM of one carrier; the ICCID tail is
    always available at creation time (a line is never created without an ICCID) and its
    last digits differ between cards of the same batch. Collisions remain possible — only
    four digits, one of them E.118's check digit — so callers generating a name pass
    unique_name=True to upsert_instance, which resolves the rest."""
    carrier = f"{mcc}-{mnc}" if mcc and mnc else ""
    tail = re.sub(r"\D", "", str(iccid or ""))[-4:]
    if carrier and tail:
        return f"{carrier}-{tail}"
    return carrier or (f"SIM-{tail}" if tail else "New SIM")


def _instance_names(data: dict, exclude_iid: str = "") -> set[str]:
    """Existing line names, casefolded — the Telegram bot resolves names
    case-insensitively, so `Giff` and `giff` are the same name for uniqueness too."""
    return {str(inst.get("name") or "").strip().casefold()
            for iid, inst in data["instances"].items()
            if str(iid) != str(exclude_iid) and str(inst.get("name") or "").strip()}


def instance_name_taken(name: str, exclude_iid: str = "") -> bool:
    """Whether another line already uses this name. An empty name is never a conflict:
    lines are allowed to be unnamed and fall back to their id for display."""
    name = str(name or "").strip()
    if not name:
        return False
    with _lock:
        return name.casefold() in _instance_names(load(), exclude_iid)


def _free_instance_name(data: dict, name: str, iid: str) -> str:
    name = str(name or "").strip()
    if not name:
        return name
    taken = _instance_names(data, iid)
    if name.casefold() not in taken:
        return name
    for suffix in range(2, 100):
        candidate = f"{name} ({suffix})"
        if candidate.casefold() not in taken:
            return candidate
    return f"{name} ({iid})"      # ids are unique, so this always terminates


def upsert_instance(inst: dict, unique_name: bool = False) -> dict:
    """Create or update one line. `unique_name` marks the name as GENERATED, letting this
    function append a counter when it collides; an operator's explicit rename is never
    silently altered — the API rejects that instead. Holding the lock across the whole
    read-modify-write keeps two concurrent hotplug creations from choosing the same name
    (or index) after both read a config that still lacked the other."""
    with _lock:
        return _upsert_instance_locked(inst, unique_name)


def _upsert_instance_locked(inst: dict, unique_name: bool = False) -> dict:
    data = load()
    iid = str(inst["id"])
    # Runtime-only fields sometimes ride along on the instance object (the API returns
    # instances with a computed `status` and `has_pin`); never persist them to config.
    inst = {k: v for k, v in inst.items() if k not in ("status", "has_pin")}
    existing = data["instances"].get(iid, {})
    if not existing and len(data["instances"]) >= MAX_SIM_LINES:
        raise LineLimitError(
            f"MDD Sim Gateway supports at most {MAX_SIM_LINES} SIM lines")
    if "index" not in existing:
        inst["index"] = next_index(data)
    else:
        inst["index"] = existing["index"]
    # Port block: keep an existing/explicit block; otherwise auto-allocate a conflict-free
    # one (checks other instances AND live host listeners, stepping forward on collision).
    if "ports" not in inst:
        inst["ports"] = existing.get("ports") or alloc_ports_auto(data, exclude_iid=iid)
    # Treat an empty/missing ami_secret the same: a WebUI save that carries a blank
    # secret must never overwrite the real one (control would then log in to the
    # engine's Asterisk with the wrong credential forever).
    if not inst.get("ami_secret"):
        inst.pop("ami_secret", None)
        inst["ami_secret"] = existing.get("ami_secret") or secrets.token_urlsafe(16)
    # The SIM PIN is a locally-saved credential tied to this IMSI/ICCID; it is used on
    # every engine start. A config edit that doesn't carry a (new, non-empty) PIN must NOT
    # wipe the stored one — otherwise saving unrelated fields (IMEI, SMSC, SIP accounts…)
    # would silently drop the PIN and break the next start. Only an explicit non-empty PIN
    # updates it; clearing is done deliberately elsewhere (wrong-PIN / PIN-removed handling).
    if not inst.get("pin"):
        inst.pop("pin", None)
        if existing.get("pin"):
            inst["pin"] = existing["pin"]
    merged = {**existing, **inst}
    # Production Asterisk debug can expose complete SIP messages and subscriber identities.
    # Diagnostic SIP logging is enabled briefly at runtime by the dedicated number-learning
    # flow instead; it must never be persisted on a line.
    merged["debug"] = {**(merged.get("debug") or {}), "asterisk": False}
    # Ensure a STABLE WebRTC softphone credential (used by both the Asterisk config and
    # the softphone provisioning endpoint — they must match).
    sip = merged.setdefault("sip", {})
    # Ignore stale clients and hand-written API requests that try to restore remote SIP
    # accounts. Only the authenticated browser softphone endpoint is rendered.
    sip["external"] = []
    wr = sip.setdefault("webrtc", {})
    wr.setdefault("username", "webrtc")
    if not wr.get("password"):
        prev = (existing.get("sip", {}) or {}).get("webrtc", {}) or {}
        wr["password"] = prev.get("password") or secrets.token_urlsafe(12)
    if unique_name:
        merged["name"] = _free_instance_name(data, merged.get("name"), iid)
    data["instances"][iid] = merged
    save(data)
    return merged


def line_allowed(iid: str) -> bool:
    """Whether a saved line is inside the product's deterministic five-line set.

    Old/self-use installations may already contain more than five records. Keep their data so
    an upgrade is non-destructive, but prevent every engine start path from using line six and
    above. Existing UI order (`index`) wins; ids break ties deterministically.
    """
    def order(item: dict):
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            index = 1 << 30
        return index, str(item.get("id") or "")

    allowed = {str(item.get("id")) for item in
               sorted(list_instances(), key=order)[:MAX_SIM_LINES]}
    return str(iid) in allowed


def clear_pin(iid: str) -> bool:
    """Delete the saved SIM PIN for an instance. Returns True if a PIN was removed. The
    line will then require the PIN to be re-entered before it can start again (used by the
    'Delete saved PIN' action and by wrong-PIN / PIN-removed handling)."""
    data = load()
    inst = data["instances"].get(str(iid))
    if not inst:
        return False
    had = bool(inst.get("pin"))
    inst["pin"] = ""
    save(data)
    return had


def delete_instance(iid: str):
    data = load()
    data["instances"].pop(str(iid), None)
    save(data)


def _card_fingerprint(iccid: str) -> str:
    """Non-reversible identity used only to suppress immediate re-creation after deletion."""
    return hashlib.sha256(str(iccid or "").strip().encode("utf-8")).hexdigest()


def suppress_card_until_removal(iccid: str) -> None:
    """Do not auto-create a deleted line again while that same card remains inserted."""
    if not str(iccid or "").strip():
        return
    data = load()
    internal = data.setdefault("internal", {})
    suppressed = set(internal.get("suppressed_cards") or [])
    suppressed.add(_card_fingerprint(iccid))
    internal["suppressed_cards"] = sorted(suppressed)
    save(data)


def unsuppress_card(iccid: str) -> None:
    """A physical removal completes deletion; a later insertion may auto-provision again."""
    if not str(iccid or "").strip():
        return
    data = load()
    internal = data.setdefault("internal", {})
    suppressed = set(internal.get("suppressed_cards") or [])
    suppressed.discard(_card_fingerprint(iccid))
    internal["suppressed_cards"] = sorted(suppressed)
    save(data)


def card_auto_create_suppressed(iccid: str) -> bool:
    if not str(iccid or "").strip():
        return False
    suppressed = (load().get("internal") or {}).get("suppressed_cards") or []
    return _card_fingerprint(iccid) in suppressed


def normalize_imei(imei: str) -> str:
    """Strip any formatting (dashes/spaces) from an IMEI and return just the digits."""
    return "".join(ch for ch in (imei or "") if ch.isdigit())


def imeisv_from_imei(imei: str, imeisv: str = "", svn: str = "00") -> str:
    """Return a 16-digit IMEISV.

    IMEISV = 14-digit IMEI base (TAC+SNR, i.e. the IMEI WITHOUT its Luhn check digit) + a
    2-digit SVN (Software Version Number). If the caller supplied an explicit IMEISV we honour
    it (digits only, padded/truncated to 16); otherwise we derive it from the IMEI's first 14
    digits and append the given SVN (default '00'). Used to answer the ePDG's DEVICE_IDENTITY
    request. Returns '' if there is no usable IMEI/IMEISV.
    """
    isv = "".join(ch for ch in (imeisv or "") if ch.isdigit())
    if isv:
        return (isv + "0" * 16)[:16]
    digits = normalize_imei(imei)
    if not digits:
        return ""
    base14 = digits[:14].ljust(14, "0")   # drop the 15th (check) digit; TAC+SNR = 14 digits
    svn2 = ("".join(ch for ch in (svn or "") if ch.isdigit()) or "00")[:2].rjust(2, "0")
    return base14 + svn2


def _clamp_rekey(v) -> int:
    """0 disables proactive rekey; otherwise clamp to 1..1440 minutes."""
    try:
        m = int(v)
    except (TypeError, ValueError):
        return 30
    if m <= 0:
        return 0
    return max(1, min(1440, m))


def normalize_apn(apn: str) -> str:
    """The APN (access point name) to attach to for VoWiFi. Blank falls back to the standard
    IMS APN 'ims'. Lowercased and trimmed; a carrier that needs a different APN (e.g. a data
    APN or a regional IMS APN) can set it explicitly."""
    a = (apn or "").strip().lower()
    return a or "ims"


def normalize_idr_mode(mode: str) -> str:
    """How the ePDG identity (IDr) is encoded in IKE_AUTH (3GPP TS 24.302 clause 7.2.2.1):
      'apn' (default) — the bare APN string (e.g. 'ims'). Most carriers' ePDGs expect this, and
                        it is the empirically-proven, widely-accepted form.
      'fqdn'          — the operator APN-FQDN a real UE builds:
                        <apn>.apn.epc.mnc<MNC3>.mcc<MCC3>.pub.3gppnetwork.org
                        A minority of stricter ePDGs require this form; note it is rejected by some
                        networks (they fail EAP with AUTHENTICATION_FAILED on it).
    Defaults to 'apn' as the safe, widely-accepted form; falls back to 'apn' for any unrecognised
    value. Set 'fqdn' per-line only for a carrier that needs it."""
    m = (mode or "").strip().lower()
    return m if m in ("apn", "fqdn") else "apn"


def normalize_cp_mode(mode: str) -> str:
    """Address family of the SWu CFG (config) request, which MUST match the carrier's IMS PDN or
    the ePDG rejects the PDN connection at the final IKE_AUTH (after EAP succeeds):
      'auto' (default) — try a discovery ladder (carrier-DB preference first) and keep the family
                         that yields a usable PDN; the engine reports the winner back and the line
                         is repinned to it. Seamless: no per-carrier knowledge needed from the user.
      'v6'             — request INTERNAL_IP6_ADDRESS + P_CSCF_IP6_ADDRESS. Telus/EE (IPv6 IMS).
      'v4'             — request the IPv4 attrs. Vodafone UK (IPv4 IMS; v6-only -> Notify 16375).
      'dual'           — request both (note dual suppresses Telus's P-CSCF).
    Defaults to 'auto'; falls back to 'auto' for any unrecognised value."""
    m = (mode or "").strip().lower()
    return m if m in ("auto", "v6", "v4", "dual") else "auto"


# Carrier CP-mode preference database, keyed by "mcc-mnc" (MNC as stored on the line — may be 2- or
# 3-digit; render_instance_json tries both). Value = the family a real UE uses on that network, used
# as the FIRST rung of the auto discovery ladder (a starting hint, NOT a hard override — the ladder
# still falls back if it fails post-EAP). Extend as new carriers are characterised.
CARRIER_CP_PREF = {
    "302-220": "v6",     # Telus (Canada) — IPv6 IMS PDN; v4/dual returns no P-CSCF
    "234-15":  "dual",   # Vodafone UK — IPv4 IMS PDN; v6-only rejected with private Notify 16375
    "234-30":  "v6",     # EE (UK) — IPv6 IMS PDN
    "234-33":  "v6",     # EE/CTExcel MVNO (UK) — IPv6 IMS PDN
}

# Carrier-specific SIP presentation required after the secure ePDG tunnel is established.
# These are protocol defaults, not retained per-SIM data: deleting a line still removes its
# complete configuration, and a later insertion deterministically rebuilds a valid profile.
# Explicit values stored under instance.sip always win over these defaults.
CARRIER_SIP_PROFILES = {
    "234-10": {  # O2 UK and MVNOs such as giffgaff
        "pani_country": "GB",
        "access_type": "wlan1",
        "user_eq_phone": True,
    },
}


def carrier_sip_defaults(mcc: str, mnc: str, identity: str = "") -> dict:
    """Return safe SIP presentation defaults for a characterised carrier.

    P-Access-Network-Info needs a plausible, stable Wi-Fi node identity. Derive a locally
    administered unicast BSSID from the SIM identity instead of using the old all-``f``
    placeholder. Only the derived BSSID is rendered; the source identity is never exposed.
    """
    keys = (
        "%s-%s" % (mcc, mnc),
        "%s-%s" % (str(mcc).zfill(3), str(mnc).zfill(3)),
        "%s-%s" % (mcc, str(mnc).lstrip("0") or mnc),
    )
    profile = next((CARRIER_SIP_PROFILES[key] for key in keys
                    if key in CARRIER_SIP_PROFILES), None)
    if not profile:
        return {}
    seed = str(identity or keys[0]).strip()
    node = bytearray(hashlib.sha256(("mdd-pani:" + seed).encode("utf-8")).digest()[:6])
    node[0] = (node[0] | 0x02) & 0xFE  # locally administered, never multicast
    node_id = "".join("%02x" % value for value in node)
    country = profile["pani_country"]
    return {
        "pani": (r'IEEE-802.11\; i-wlan-node-id="%s"\;country=%s'
                 % (node_id, country)),
        "access_type": profile["access_type"],
        "user_eq_phone": bool(profile["user_eq_phone"]),
    }


def merge_carrier_sip_defaults(mcc: str, mnc: str, identity: str,
                               sip: dict | None) -> dict:
    """Merge carrier SIP defaults, treating blank text fields as 'use automatic'."""
    defaults = carrier_sip_defaults(mcc, mnc, identity)
    explicit = dict(sip or {})
    for key in defaults:
        if explicit.get(key) in (None, ""):
            explicit.pop(key, None)
    return {**defaults, **explicit}

# Default auto discovery ladder for carriers not in CARRIER_CP_PREF. v6 first (most VoLTE/VoWiFi IMS
# cores are IPv6 and connect on attempt 1); dual catches v4/dual-only carriers (e.g. Vodafone); v4
# last. The DB-preferred family (if any) is moved to the front and deduped by render_instance_json.
CP_MODE_LADDER_DEFAULT = ["v6", "dual", "v4"]


def cp_mode_order_for(mcc: str, mnc: str) -> str:
    """Compute the comma-separated auto discovery ladder for a line: carrier-DB preference (matched
    on mcc-mnc, trying both the stored MNC and its 3-digit zfill) first, then the default ladder,
    deduped. Consumed by the engine as SWU_CP_MODE_ORDER."""
    order = []
    pref = None
    for key in ("%s-%s" % (mcc, mnc), "%s-%s" % (str(mcc).zfill(3), str(mnc).zfill(3)),
                "%s-%s" % (mcc, str(mnc).lstrip("0") or mnc)):
        if key in CARRIER_CP_PREF:
            pref = CARRIER_CP_PREF[key]
            break
    if pref:
        order.append(pref)
    for m in CP_MODE_LADDER_DEFAULT:
        if m not in order:
            order.append(m)
    return ",".join(order)


def render_instance_json(inst: dict, settings: dict) -> dict:
    """Convert a stored instance into the engine /config/instance.json contract."""
    ports = inst.get("ports", _alloc_ports(inst.get("index", 0)))
    sip = merge_carrier_sip_defaults(
        inst.get("mcc", ""), inst.get("mnc", ""),
        inst.get("iccid") or inst.get("imsi") or inst.get("imei"), inst.get("sip"))
    webrtc = sip.get("webrtc", {}) or {}
    ami_secret = str(inst.get("ami_secret") or "")
    webrtc_password = str(webrtc.get("password") or "")
    if not ami_secret:
        raise ValueError("instance AMI credential is missing")
    if webrtc.get("enable", True) and not webrtc_password:
        raise ValueError("instance WebRTC credential is missing")
    return {
        "id": str(inst["id"]),
        "imsi": inst["imsi"],
        "mcc": inst["mcc"],
        "mnc": inst["mnc"],
        "imei": inst.get("imei", ""),
        # IMEISV (16 digits) for the ePDG DEVICE_IDENTITY response. Explicit stored value wins;
        # otherwise auto-derive from the IMEI (14-digit base + '00' SVN). Empty stays empty
        # (swu_ike then derives its own or falls back).
        "imeisv": inst.get("imeisv", "") or imeisv_from_imei(inst.get("imei", ""), inst.get("imeisv", "")),
        "pin": inst.get("pin", ""),
        "reader": inst.get("reader") or f"imsi:{inst['imsi']}",
        "pin_reader": inst.get("pin_reader", "0"),
        "ami_reader": inst.get("ami_reader", "2"),
        # PC/SC reader index the engine addresses the SIM by (passed to swu_ike as -m / pin_keeper
        # / ami_usim). MUST be emitted: without it the engine's render.py defaults to 0, so a line
        # on any reader other than 0 authenticates against the wrong physical SIM (USIM AUTHENTICATE
        # returns 0x9862 "incorrect MAC"). Kept in sync with the live ICCID-matched reader at start.
        "reader_index": inst.get("reader_index", 0),
        # Stable physical USB port path of the reader (e.g. "3-2"). The engine resolves this back
        # to a live PC/SC index in-container so its self-heal restarts address the right physical
        # reader even when pcscd re-enumerates two identical readers in a different order. Empty
        # -> engine falls back to reader_index. See control/app/usbreader.py.
        "reader_port": inst.get("reader_port", ""),
        "iccid": inst.get("iccid", ""),
        "msisdn": inst.get("msisdn", ""),
        "smsc": inst.get("smsc", ""),
        "pcscf": inst.get("pcscf", ""),
        "ami_user": inst.get("ami_user", "vowifi"),
        "ami_secret": ami_secret,
        # Where engine notify.py POSTs events. Explicit setting wins; else MDD_MANAGER_URL
        # env (the installer sets this to the PUBLISHED host port when the control plane runs
        # in a bridge-networked container with a non-8443 port map); else the default assumes
        # a 1:1 host.docker.internal:<http_port> mapping.
        "manager_url": settings.get("manager_url")
                       or os.environ.get("MDD_MANAGER_URL")
                       or f"https://host.docker.internal:{settings.get('http_port', 8443)}",
        "manager_event_token": internal_event_token(),
        "domain": settings.get("tls", {}).get("domain", ""),
        "rtp_start": ports["rtp_start"],
        # The engine publishes only the configured RTP span (engine.start),
        # so the Asterisk RTP pool (rtp.conf rtpend) MUST match that published window — a port
        # picked above it would be unreachable from a LAN WebRTC client → no/one-way audio. Cap
        # rtp_end to the published span rather than the (larger) block-allocation rtp_end.
        "rtp_end": min(ports["rtp_end"], ports["rtp_start"] + rtp_span(ports) - 1),
        "sip": {
            "listen_addr": sip.get("listen_addr", "0.0.0.0"),
            "external": [],
            "advertise_address": advertise_address(settings),
            # ICE host-candidate mappings require an IP literal even when PJSIP itself uses
            # the public TLS domain for signaling and SDP rewriting.
            "ice_advertise_address": ice_advertise_address(settings),
            # Outbound ring timeout: per-line override (sip.ring_timeout) wins, else the global
            # settings default, else 35s. Clamped to a sane 5..180 range.
            "ring_timeout": max(5, min(180, int(
                sip.get("ring_timeout") or settings.get("ring_timeout", 35)))),
            # Public builds identify themselves honestly; stale/manual settings cannot make
            # the gateway impersonate a handset model.
            "user_agent": "MDD-Sim-Gateway",
            # Some IMS cores require telephone-number request URIs to carry ;user=phone before
            # they will route an originating voice INVITE. Keep this carrier-configurable because
            # other networks reject SMS MESSAGE request URIs when the parameter is present.
            "user_eq_phone": bool(sip.get("user_eq_phone", False)),
            "pani": sip.get("pani", ""),
            "access_type": sip.get("access_type", ""),
            "webrtc": {
                "enable": bool(webrtc.get("enable", True)),
                "username": webrtc.get("username", "webrtc"),
                "password": webrtc_password,
                "port": 8089,
            },
        },
        # Defence in depth for instance.json files rendered from old or imported configs.
        "debug": {**(settings.get("debug") or {}), **(inst.get("debug") or {}),
                  "asterisk": False},
        # Proactive CHILD-SA rekey period in minutes (0 = disabled). Per-line override
        # (inst.rekey_minutes) wins, else the global settings default, else 30. Clamped to 0
        # (off) or a sane 1..1440 window so a typo can't set an absurd sub-minute rekey storm.
        "rekey_minutes": _clamp_rekey(inst.get("rekey_minutes",
                                               (settings.get("rekey", {}) or {}).get("minutes", 30))),
        # Accept an ePDG-initiated ESP rekey in place rather than refusing it and rebuilding the
        # tunnel. Per-line, and off unless asked for: the responder-side key direction it relies
        # on can only be proven against a carrier that actually initiates a rekey.
        "accept_epdg_esp_rekey": bool(inst.get("accept_epdg_esp_rekey",
                                               (settings.get("rekey", {}) or {}).get("accept_epdg", False))),
        # APN + ePDG-identity (IDr) encoding for the SWu tunnel. apn defaults to the standard IMS
        # APN 'ims'; idr_mode defaults to 'apn' (the bare-APN form most carriers' ePDGs expect and
        # the proven-safe default) and may be set to 'fqdn' for the stricter ePDGs that require the
        # operator APN-FQDN. See swu_ike.py (SWU_APN / SWU_IDR_MODE).
        "apn": normalize_apn(inst.get("apn", "")),
        "idr_mode": normalize_idr_mode(inst.get("idr_mode", "")),
        # SWu CFG request address family (must match the carrier's IMS PDN). Defaults to 'auto'
        # (discovery ladder + carrier DB); 'v6' Telus/EE, 'v4' Vodafone UK, 'dual' both. When auto,
        # cp_mode_order gives the engine the discovery ladder (carrier-DB preference first).
        # See swu_ike.py (SWU_CP_MODE / SWU_CP_MODE_ORDER).
        "cp_mode": normalize_cp_mode(inst.get("cp_mode", "")),
        "cp_mode_order": cp_mode_order_for(inst["mcc"], inst["mnc"]),
    }


def write_instance_json(inst: dict, settings: dict) -> str:
    d = os.path.join(DATA_DIR, "instances", str(inst["id"]))
    _private_dir(DATA_DIR)
    _private_dir(os.path.join(DATA_DIR, "instances"))
    _private_dir(d)
    path = os.path.join(d, "instance.json")
    tmp = path + ".tmp"
    with _private_text_writer(tmp) as f:
        json.dump(render_instance_json(inst, settings), f, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path
