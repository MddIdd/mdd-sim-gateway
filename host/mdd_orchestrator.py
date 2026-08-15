#!/usr/bin/env python3
"""Host-side country egress and modem/VPCD orchestrator.

The manager writes ``data/orchestrator/desired.json``.  This process owns the host network
namespace, sing-box process, ePDG host routes, USB-modem discovery and the VPCD bridge children.
Native PC/SC readers are deliberately ignored.
"""
from __future__ import annotations

import argparse
import base64
import collections
import hashlib
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import serial
except ImportError:  # pragma: no cover - host installer provides pyserial
    serial = None

try:
    import yaml
except ImportError:  # pragma: no cover - installer provides PyYAML
    yaml = None

# 0x8C7B (35963) is vpcd's own default port, which the distribution package hands to its
# "Virtual PCD" reader. Two pcscd readers cannot listen on one port, so sharing that base
# made the modem readers and the packaged reader fight over it — directory order decided
# who won and the loser never bound at all. Stay off it, and below the ephemeral range so
# an outbound socket cannot squat a slot either.
BASE_VPCD_PORT = 0x3C00
VPCD_PORT_STRIDE = 0x100
VPCD_PORT_SLOTS = 64
# Slots the upstream libifdvpcd is compiled for (--enable-vpcdslots, default 2). Assumed when
# the installer has not recorded a rebuilt driver, because asking for slots the driver does not
# have is what leaves a bridge thread dialling a socket that never appears.
VPCD_PACKAGED_SLOTS = 2
VPCD_DRIVER_DIRS = ("/usr/lib", "/usr/local/lib")
# Logical channels the bridge implements, one per slot. It rejects anything above this on its
# command line, so a larger configured or driver-provided count must not reach it: the bridge
# would exit at startup and be respawned every cycle. The installer deliberately builds the
# driver with a spare slot, so this is the binding limit rather than the driver's.
VPCD_CHANNEL_CAPACITY = 3
# The reader definition shipped by the vsmartcard-vpcd package, renamed out of the way
# (pcsc-lite skips dot files) rather than deleted, so an operator can restore it.
DISTRO_VPCD_READER = "vpcd"
DISTRO_VPCD_READER_DISABLED = ".vpcd.mdd-disabled"
MANAGED_ROUTE_PROTO = "186"
CLASH_API = os.environ.get("MDD_CLASH_API", "127.0.0.1:19090")


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def append_jsonl(path: Path, record: dict, limit: int = 500):
    """Append one bounded diagnostic record, trimming to the newest ``limit`` lines.

    Rewriting the file keeps it from growing without bound on a Pi's SD card. These records
    exist to correlate a line's failure window with exit-node changes, so only the recent
    tail has any value.
    """
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lines = []
        if path.exists():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        lines.append(json.dumps(record, sort_keys=True))
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(lines[-limit:]) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        # Diagnostics must never take the orchestrator's reconcile loop down.
        pass


def run(args, *, check=False, capture=True):
    return subprocess.run(args, check=check, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "-", value).strip("-") or "modem"


def tun_name(country: str) -> str:
    return f"mdd-{country.lower()}"[:15]


def parse_proxy_url(url: str, tag: str) -> dict:
    from urllib.parse import unquote, urlsplit
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in ("socks", "socks5") or not parsed.hostname:
        raise ValueError("VoWiFi requires UDP; manual proxy must be a UDP-capable socks5:// URL")
    out = {"type": "socks", "tag": tag, "version": "5",
           "server": parsed.hostname, "server_port": parsed.port or 1080}
    if parsed.username:
        out["username"] = unquote(parsed.username)
    if parsed.password:
        out["password"] = unquote(parsed.password)
    return out


def b64_padded(value: str) -> str:
    """Decode base64 that share links emit without padding, in either alphabet."""
    text = value.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(text + "=" * (-len(text) % 4)).decode("utf-8", errors="replace")


def parse_share_link(url: str) -> dict:
    """Convert one protocol share link into the Clash-style node dict clash_outbound() takes.

    Reusing that converter — rather than emitting sing-box outbounds directly — keeps pasted
    nodes on exactly the same code path as subscription nodes, including the UDP admission
    rules that decide whether a node may carry IKE at all.
    """
    from urllib.parse import parse_qs, unquote, urlsplit
    text = url.strip()
    scheme = text.split("://", 1)[0].lower() if "://" in text else ""

    if scheme == "vmess":
        # vmess links are a base64 JSON blob rather than a URL.
        payload = json.loads(b64_padded(text.split("://", 1)[1].split("#", 1)[0]))
        node = {"type": "vmess", "server": payload.get("add"), "port": payload.get("port"),
                "uuid": payload.get("id"), "alterId": payload.get("aid") or 0,
                "cipher": payload.get("scy") or "auto",
                "tls": str(payload.get("tls") or "").lower() in ("tls", "true"),
                "servername": payload.get("sni") or payload.get("host") or "",
                "network": str(payload.get("net") or "tcp")}
        if node["network"] == "ws":
            host = payload.get("host")
            node["ws-opts"] = {"path": payload.get("path") or "/",
                               "headers": {"Host": host} if host else {}}
        return node

    if scheme == "ss":
        # Either ss://base64(method:password)@host:port or ss://base64(method:password@host:port)
        body = text.split("://", 1)[1].split("#", 1)[0].split("?", 1)[0]
        if "@" in body:
            credentials, _, hostport = body.rpartition("@")
            credentials = b64_padded(credentials)
        else:
            credentials, _, hostport = b64_padded(body).rpartition("@")
        method, _, password = credentials.partition(":")
        host, _, port = hostport.rpartition(":")
        return {"type": "ss", "server": host, "port": port,
                "cipher": method, "password": password}

    parsed = urlsplit(text)
    if not parsed.hostname or not parsed.port:
        raise ValueError("node link is missing a host or port")
    query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
    userinfo = unquote(parsed.username or "")
    node = {"server": parsed.hostname, "port": parsed.port,
            "network": query.get("type") or query.get("network") or "tcp",
            "servername": query.get("sni") or query.get("peer") or query.get("host") or "",
            "skip-cert-verify": str(query.get("allowInsecure")
                                    or query.get("insecure") or "").lower() in ("1", "true")}
    if scheme == "vless":
        node.update({"type": "vless", "uuid": userinfo, "flow": query.get("flow") or "",
                     "tls": str(query.get("security") or "").lower()
                     in ("tls", "reality", "xtls")})
        if str(query.get("security") or "").lower() == "reality":
            node["reality-opts"] = {"public-key": query.get("pbk") or query.get("publicKey") or "",
                                    "short-id": query.get("sid") or query.get("shortId") or ""}
            node["client-fingerprint"] = query.get("fp") or "chrome"
        if query.get("alpn"):
            node["alpn"] = [x for x in query["alpn"].split(",") if x]
        if node["network"] == "xhttp":
            try:
                extra = json.loads(unquote(query.get("extra") or "{}"))
            except (TypeError, ValueError):
                extra = {}
            node["xhttp-opts"] = {"host": query.get("host") or "",
                                  "path": unquote(query.get("path") or "/"),
                                  "mode": query.get("mode") or "auto",
                                  "extra": extra if isinstance(extra, dict) else {}}
            node["packet-encoding"] = query.get("packetEncoding") or "xudp"
    elif scheme == "trojan":
        node.update({"type": "trojan", "password": userinfo})
    elif scheme in ("hysteria2", "hy2"):
        # QUIC-based, so it has no stream transport to describe.
        node.update({"type": "hysteria2", "password": userinfo, "network": "tcp"})
    else:
        raise ValueError(f"unsupported node link scheme {scheme or text[:12]!r}")
    if node["network"] == "ws":
        host = query.get("host")
        node["ws-opts"] = {"path": unquote(query.get("path") or "/"),
                           "headers": {"Host": host} if host else {}}
    return node


def parse_manual_outbound(value, tag: str) -> dict:
    """Build one exit outbound from whatever an operator pasted.

    Three accepted forms: a socks5:// URL, a protocol share link (the format nodes are
    normally handed out in), or a raw sing-box outbound object. All three are checked for UDP
    capability here, because a TCP-only exit otherwise stays invisible until IKE times out.
    """
    if isinstance(value, dict):
        outbound = dict(value)
        outbound["tag"] = tag
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("no node link or proxy URL provided")
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except ValueError as exc:
                raise ValueError(f"node configuration is not valid JSON: {exc}") from exc
            if not isinstance(parsed, dict):
                raise ValueError("node configuration must be a single outbound object")
            outbound = {**parsed, "tag": tag}
        elif text.split("://", 1)[0].lower() in ("socks", "socks5"):
            outbound = parse_proxy_url(text, tag)
        else:
            outbound = clash_outbound(parse_share_link(text), tag)
    if not outbound_supports_udp(outbound):
        raise ValueError(f"node type {outbound.get('type') or 'unknown'!r} cannot carry the "
                         "UDP traffic VoWiFi IKE requires")
    return outbound


UDP_PROXY_TYPES = {"ss", "shadowsocks", "trojan", "vless", "vmess", "hysteria2", "hy2"}
# Carrier ePDG names rotate their A record every ~30 seconds. Routing only the newest answer
# pulls the route out from under a tunnel that is still talking to the previous address, which
# drops that traffic onto the host default route — the wrong country, and the carrier drops the
# IKE SA. Addresses are therefore retained well beyond one DNS answer.
EPDG_ADDRESS_TTL = float(os.environ.get("MDD_EPDG_ADDRESS_TTL", "21600"))
EPDG_ADDRESS_MAX = int(os.environ.get("MDD_EPDG_ADDRESS_MAX", "64"))
# How long a node that just failed a line is kept out of the candidate pool.
EXIT_RESELECT_COOLDOWN = float(os.environ.get("MDD_EXIT_RESELECT_COOLDOWN", "900"))
EXIT_RESELECT_MAX_AGE = float(os.environ.get("MDD_EXIT_RESELECT_MAX_AGE", "600"))
EXIT_RESELECT_RETRY_SECONDS = float(os.environ.get("MDD_EXIT_RESELECT_RETRY", "60"))
EXIT_RESELECT_MAX_ATTEMPTS = int(os.environ.get("MDD_EXIT_RESELECT_ATTEMPTS", "3"))
EXIT_PROBE_URL = os.environ.get("MDD_EXIT_PROBE_URL", "https://www.gstatic.com/generate_204")
EXIT_PROBE_TIMEOUT_MS = int(os.environ.get("MDD_EXIT_PROBE_TIMEOUT_MS", "3000"))
# Probes of a pinned node before giving up on it, so one spike cannot void the preference.
EXIT_PREFERRED_PROBE_ATTEMPTS = int(os.environ.get("MDD_EXIT_PREFERRED_PROBE_ATTEMPTS", "2"))
# Ranking a sing-box that has only just started measures its cold start rather than the nodes:
# a QUIC outbound must complete a fresh handshake and loses to TCP candidates that are slower
# in steady state. Wait for the process to settle before believing any measurement.
EXIT_RANK_WARMUP_SECONDS = float(os.environ.get("MDD_EXIT_RANK_WARMUP", "25"))
# A full reconcile shells out to mmcli and ip about fifteen times. At the base interval that
# was the largest source of process creation on the box, and almost all of it re-derived a
# state that had not changed. When a cycle finds nothing to do the loop backs off, while still
# waking on the base interval to stat the input documents so an operator action is never
# delayed by more than one base tick.
IDLE_INTERVAL_SECONDS = float(os.environ.get("MDD_IDLE_INTERVAL", "15"))
# How long a tty may stay unclaimed before the bridge stops waiting for ModemManager and talks
# to the serial port itself. ModemManager needs on the order of ten to thirty seconds to probe
# an EC25-class module, so this is set far beyond any healthy first pass: reaching it means
# ModemManager has decided not to manage this hardware, not that it is still working on it.
MM_CLAIM_GRACE_SECONDS = float(os.environ.get("MDD_MM_CLAIM_GRACE", "180"))
# A bridge that exits is respawned, but blindly and instantly respawning turned one broken
# serial port into a fifteen-second crash loop that device status reported as a running
# bridge. Retries double from base to ceiling; a bridge that survives the stable window has
# its failure history forgotten; and a freshly spawned process only counts as running once
# it has outlived the settle window when there is a recorded failure to live down.
BRIDGE_RETRY_BASE_SECONDS = 15.0
BRIDGE_RETRY_CEILING_SECONDS = 600.0
BRIDGE_STABLE_SECONDS = 60.0
BRIDGE_SETTLE_SECONDS = 5.0
COUNTRY_PROXY_LISTEN = os.environ.get("MDD_COUNTRY_PROXY_LISTEN", "172.17.0.1")
COUNTRY_PROXY_PORT_BASE = int(os.environ.get("MDD_COUNTRY_PROXY_PORT_BASE", "22000"))


def country_proxy_port(country: str) -> int:
    """Stable, collision-free port for two-letter ISO codes (22000..22675 by default)."""
    code = str(country).lower()
    if not re.fullmatch(r"[a-z]{2}", code):
        raise ValueError("country must be a two-letter ISO code")
    return COUNTRY_PROXY_PORT_BASE + (ord(code[0]) - 97) * 26 + ord(code[1]) - 97


def node_keyword_matches(name: str, keyword: str) -> bool:
    name, keyword = str(name).lower(), str(keyword).lower().strip()
    if not keyword:
        return False
    # ISO/country abbreviations must be standalone tokens: GB must not match a quota such as
    # "58.5GB", and US must not match an unrelated longer word.
    if re.fullmatch(r"[a-z0-9]{2,3}", keyword):
        return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", name) is not None
    return keyword in name


def clash_node_supports_udp(node: dict) -> bool:
    """Conservative subscription admission check for IKE UDP 500/4500.

    Clash's `udp` flag is optional for protocols that inherently support UDP, so only an
    explicit false rejects them. Shadowsocks SIP003 plugins are TCP-only in this converter.
    Unsupported node/transport types are rejected before they can poison the whole pool.
    """
    kind = str(node.get("type") or "").lower()
    if kind not in UDP_PROXY_TYPES or node.get("udp") is False:
        return False
    if kind in {"ss", "shadowsocks"} and node.get("plugin"):
        return False
    network = str(node.get("network") or "").lower()
    return network in {"", "tcp", "ws", "xhttp"}


def xray_xhttp_outbound(node: dict, tag: str) -> dict:
    """Convert a VLESS XHTTP node to Xray-core's native outbound form."""
    if str(node.get("type") or "").lower() != "vless" \
            or str(node.get("network") or "").lower() != "xhttp":
        raise ValueError("XHTTP currently requires a VLESS node")
    reality = node.get("reality-opts") or {}
    if not reality.get("public-key"):
        raise ValueError("Reality XHTTP node is missing its public key (pbk)")
    user = {"id": str(node.get("uuid") or ""), "encryption": "none",
            "flow": str(node.get("flow") or "")}
    if node.get("packet-encoding"):
        user["packetEncoding"] = str(node["packet-encoding"])
    xhttp = node.get("xhttp-opts") or {}
    stream = {"network": "xhttp", "security": "reality",
              "realitySettings": {
                  "serverName": node.get("servername") or node.get("server"),
                  "fingerprint": node.get("client-fingerprint") or "chrome",
                  "publicKey": reality.get("public-key"),
                  "shortId": reality.get("short-id") or "",
              },
              "xhttpSettings": {"host": xhttp.get("host") or "",
                                "path": xhttp.get("path") or "/",
                                "mode": xhttp.get("mode") or "auto"}}
    if isinstance(xhttp.get("extra"), dict) and xhttp["extra"]:
        stream["xhttpSettings"]["extra"] = xhttp["extra"]
    return {"protocol": "vless", "tag": tag,
            "settings": {"vnext": [{"address": node.get("server"),
                                     "port": int(node.get("port") or 0), "users": [user]}]},
            "streamSettings": stream}


def outbound_supports_udp(outbound: dict) -> bool:
    kind = str(outbound.get("type") or "").lower()
    if outbound.get("network") == "tcp":
        return False
    if kind == "socks":
        return str(outbound.get("version") or "5") == "5"
    return kind in {"shadowsocks", "trojan", "vless", "vmess", "hysteria", "hysteria2",
                    "tuic", "wireguard"}


def clash_outbound(node: dict, tag: str) -> dict:
    """Convert the common Clash subscription node types used by the gateway."""
    kind = str(node.get("type", "")).lower()
    base = {"type": kind, "tag": tag, "server": node.get("server"),
            "server_port": int(node.get("port") or 0)}
    if not base["server"] or not base["server_port"]:
        raise ValueError("subscription node lacks server/port")
    if kind == "trojan":
        base["password"] = node.get("password", "")
    elif kind == "vless":
        base["uuid"] = node.get("uuid", "")
        base["flow"] = node.get("flow", "")
    elif kind == "vmess":
        base["uuid"] = node.get("uuid", "")
        base["security"] = node.get("cipher") or "auto"
        base["alter_id"] = int(node.get("alterId") or 0)
    elif kind in ("ss", "shadowsocks"):
        base["type"] = "shadowsocks"
        base["method"] = node.get("cipher", "")
        base["password"] = node.get("password", "")
    elif kind in ("hysteria2", "hy2"):
        base["type"] = "hysteria2"
        base["password"] = node.get("password") or node.get("auth", "")
    else:
        raise ValueError(f"unsupported subscription node type {kind!r}")
    if node.get("tls") is True or kind in ("trojan", "hysteria2", "hy2"):
        tls = {"enabled": True,
               "server_name": node.get("servername") or node.get("sni") or node["server"],
               "insecure": bool(node.get("skip-cert-verify", False))}
        if node.get("alpn"):
            tls["alpn"] = list(node["alpn"])
        fingerprint = str(node.get("client-fingerprint") or "")
        reality = node.get("reality-opts") or {}
        if reality:
            # Dropping these produced an outbound that looked valid and simply never
            # completed a handshake — the server cannot answer a client that omits them.
            tls["reality"] = {"enabled": True,
                              "public_key": reality.get("public-key", ""),
                              "short_id": reality.get("short-id", "")}
            # REALITY authenticates the server through its own key exchange, and sing-box
            # requires uTLS alongside it.
            tls["insecure"] = False
            fingerprint = fingerprint or "chrome"
        if fingerprint:
            tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
        base["tls"] = tls
    if kind in ("hysteria2", "hy2") and node.get("obfs"):
        # A server expecting salamander silently discards unobfuscated packets, so the client
        # only ever sees "no recent network activity".
        base["obfs"] = {"type": str(node["obfs"]),
                        "password": str(node.get("obfs-password") or "")}
    network = node.get("network")
    if network == "ws":
        ws = node.get("ws-opts") or {}
        base["transport"] = {"type": "ws", "path": ws.get("path", "/"),
                             "headers": ws.get("headers") or {}}
    return {k: v for k, v in base.items() if v not in (None, "")}


class Orchestrator:
    def __init__(self, data: Path, repo: Path, interval: float = 3.0, dry_run=False):
        self.data, self.repo, self.interval, self.dry_run = data, repo, interval, dry_run
        self.root = data / "orchestrator"
        self.desired_path = self.root / "desired.json"
        self.status_path = self.root / "proxy-status.json"
        self.hw_state_path = self.root / "hardware-state.json"
        self.device_desired_path = self.root / "devices-desired.json"
        self.device_status_path = self.root / "devices-status.json"
        self.generated = self.root / "sing-box.json"
        self.xray_generated = self.root / "xray.json"
        self.cache = self.root / "subscription.yaml"
        # The selector process is our child, so a service restart also restarts sing-box.
        # Recover the last observed choices before rendering its new defaults; otherwise a
        # preferred pin that just failed wins again merely because the orchestrator restarted.
        prior_exits = (read_json(self.status_path).get("exits") or {})
        if not isinstance(prior_exits, dict):
            prior_exits = {}
        # "<country>:<epdg host>" -> {address: time it may stop being routed}
        self.epdg_seen: dict[str, dict[str, float]] = {}
        # Last node change per country, so the UI can say why the exit is not the pinned one.
        self.exit_last_change: dict[str, dict] = {
            str(country): dict(state["last_change"])
            for country, state in prior_exits.items()
            if isinstance(state, dict) and isinstance(state.get("last_change"), dict)
        }
        # When sing-box last (re)started; measurements before it settles are cold-start
        # numbers, not node quality.
        self.singbox_started_at = 0.0
        self.exit_node_history = self.root / "exit-node-history.jsonl"
        self.reselect_path = self.root / "exit-reselect.json"
        self.reselect_handled_path = self.root / "exit-reselect-handled.json"
        self.last_exit_node: dict[str, str] = {
            str(country): str(state.get("node") or "")
            for country, state in prior_exits.items()
            if isinstance(state, dict) and state.get("ready") and state.get("node")
        }
        # country -> {node name: time it may be chosen again}
        self.exit_cooldown: dict[str, dict[str, float]] = {}
        # country -> timestamp of the newest reselect request already served. This must survive
        # an orchestrator restart: exit-reselect.json is deliberately written by another process
        # and retained for diagnostics, so an in-memory watermark would replay old failures.
        handled = (read_json(self.reselect_handled_path).get("countries") or {})
        self.handled_reselect: dict[str, float] = {}
        if isinstance(handled, dict):
            for country, requested_at in handled.items():
                try:
                    self.handled_reselect[str(country)] = float(requested_at)
                except (TypeError, ValueError):
                    pass
        # country -> retry state for the current request. Ranking is synchronous and each
        # unreachable member can consume five seconds, so retrying on every reconcile cycle would
        # starve modem/SIM work. This state is intentionally ephemeral; the persistent handled
        # watermark still prevents a completed/abandoned request replay after restart.
        self.reselect_retries: dict[str, dict] = {}
        # Countries whose selector has not been ranked since sing-box last (re)started.
        self.exit_unranked: set[str] = set()
        # Countries whose freshly rendered config already defaults to the node
        # currently carrying their tunnels, so a restart needs no re-ranking.
        self.exit_resume: dict[str, str] = {}
        self.singbox = None
        self.xray = None
        self.last_xray_fingerprint = ""
        self.next_xray_config = None
        self._xray_inbounds: list[dict] = []
        self._xray_outbounds: list[dict] = []
        self._xray_rules: list[dict] = []
        self._xray_ports: dict[str, int] = {}
        self.bridges: dict[str, subprocess.Popen] = {}
        self.bridge_ports: dict[str, int] = {}
        self.last_proxy_fingerprint = ""
        self.applied_cellular_backend: bool | None = None
        self.radio_states: dict[str, bool] = {}
        self.cellular_states: dict[str, dict] = {}
        self.data_attempt_at: dict[str, float] = {}
        self.applied_timezone = ""
        self.obsolete_services_retired = False
        self.reader_config_path = Path(os.environ.get(
            "MDD_VPCD_READER_CONFIG", "/etc/reader.conf.d/mdd-sim-gateway-modems"))
        try: self.last_reader_config = self.reader_config_path.read_text(encoding="utf-8")
        except OSError: self.last_reader_config = ""
        self.stop = False
        # What the previous cycle concluded, for deciding whether this one changed anything.
        self._last_conclusion = ""
        # ---------------------------------------------------------- support diagnostics
        # Everything below exists so the redacted support bundle can answer host-side
        # questions on its own. Without it a modem/ModemManager fault is invisible to the
        # control plane, which only ever sees the documents this process publishes.
        self.host_diagnostics_path = self.root / "host-diagnostics.json"
        # Our own recent output. journalctl is unreachable from the control-plane container,
        # so the bundle would otherwise carry no trace of what this loop decided.
        self._log_ring: collections.deque[str] = collections.deque(maxlen=200)
        # Populated only while a tty stays unclaimed; a healthy gateway pays nothing for it.
        self._claim_evidence: dict = {}
        self._virtualization: str | None = None
        # tty -> when it was first seen unclaimed, for the grace period below.
        self._unclaimed_since: dict[str, float] = {}
        # device id -> why its bridge talks to the serial port instead of ModemManager.
        self._degraded: dict[str, str] = {}
        # device id -> when its bridge process was spawned, and the record of its exits.
        # Together these are what keep a crash-looping bridge visible: without them the
        # status document reported every freshly respawned process as a running bridge.
        self._bridge_started: dict[str, float] = {}
        self._bridge_failures: dict[str, dict] = {}

    @staticmethod
    def service_active(name: str) -> bool:
        return run(["systemctl", "is-active", "--quiet", name]).returncode == 0

    # ------------------------------------------------------------------ self-update
    def process_update_request(self):
        """Launch the detached self-updater when the control plane requests one.

        The updater must outlive this process (``install.sh reload`` restarts the
        orchestrator and the control plane), so it runs as a transient systemd unit from a
        staged copy of ``host/mdd_update.py`` — the checkout under ``self.repo`` is replaced
        while it runs.  The request file is consumed before launching so a restart loop can
        never spawn a second updater for the same request.
        """
        request_path = self.root / "update-request.json"
        request = read_json(request_path)
        if not request:
            return
        try:
            request_path.unlink()
        except OSError:
            pass
        status_path = self.root / "update-status.json"
        version = str(request.get("version") or "")
        repository = str(request.get("repository") or "")
        network = request.get("network") or {}

        def fail(reason: str):
            atomic_json(status_path, {"state": "failed", "phase": "launch", "error": reason,
                                      "target": version, "updated_at": int(time.time())})

        if not re.fullmatch(r"\d+(?:\.\d+)*(?:-[0-9A-Za-z.]+)?", version) \
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            fail("invalid update request")
            return
        mode = str(network.get("proxy_mode") or "direct").lower()
        proxy_url = ""
        if mode == "manual":
            proxy_url = str(network.get("proxy_url") or "").strip()
            parsed = urllib.parse.urlsplit(proxy_url)
            if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"} \
                    or not parsed.hostname or any(ch in proxy_url for ch in "\r\n"):
                fail("invalid update proxy configuration")
                return
        elif mode == "country":
            country = str(network.get("proxy_country") or "").strip().lower()
            state = (read_json(self.status_path).get("exits") or {}).get(country) or {}
            try:
                proxy_port = int(state.get("proxy_port") or 0)
            except (TypeError, ValueError):
                proxy_port = 0
            proxy_host = str(state.get("proxy_host") or "").strip()
            if not re.fullmatch(r"[a-z]{2}", country) or not state.get("ready") \
                    or proxy_host != COUNTRY_PROXY_LISTEN or not 1 <= proxy_port <= 65535:
                fail("selected update country exit is not ready")
                return
            proxy_url = f"socks5h://{proxy_host}:{proxy_port}"
        elif mode != "direct":
            fail("invalid update proxy mode")
            return
        if self.dry_run:
            fail("dry-run orchestrator does not apply updates")
            return
        if self.service_active("mdd-sim-gateway-update.service"):
            return  # an update is already running; drop the duplicate request
        runner = self.data / "update" / "runner.py"
        try:
            runner.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(self.repo / "host" / "mdd_update.py", runner)
            network_path = runner.parent / "network.json"
            # Proxy credentials stay in a root-only file and never appear in systemd's command
            # line, unit metadata, progress status or journal output.
            atomic_json(network_path, {"proxy_url": proxy_url})
        except OSError as exc:
            fail(f"could not stage the updater: {exc}")
            return
        run(["systemctl", "reset-failed", "mdd-sim-gateway-update.service"])
        atomic_json(status_path, {"state": "running", "phase": "launching", "target": version,
                                  "updated_at": int(time.time())})
        result = run(["systemd-run", "--unit", "mdd-sim-gateway-update", "--collect",
                      "--description", "MDD Sim Gateway self-update",
                      sys.executable, str(runner), "--repo", str(self.repo),
                      "--data", str(self.data), "--version", version,
                      "--repository", repository, "--network-config", str(network_path)])
        if result.returncode != 0:
            fail(f"systemd-run failed: {(result.stderr or result.stdout or '').strip()}")

    def publish_device_status(self, desired_devices: dict, assignments: dict,
                              *, transitioning=False, error="", disruption=None,
                              affected_devices=None):
        mm_active = self.service_active("ModemManager.service")
        devices = {}
        for device_id in sorted(set(desired_devices) | set(assignments)):
            assignment = assignments.get(device_id) or {}
            wanted = desired_devices.get(device_id) or {
                "cellular_enabled": False, "vowifi_enabled": True,
                "flight_mode": False}
            bridge = self.bridges.get(device_id)
            bridge_failure = self._bridge_failures.get(device_id)
            bridge_alive = bool(bridge and bridge.poll() is None)
            # A process that was just respawned over a recorded failure has not proven
            # anything yet: reporting it as a running bridge is what made a crash loop
            # read as healthy. It counts once it has outlived the settle window.
            vowifi_actual = bridge_alive and (
                not bridge_failure or
                time.time() - self._bridge_started.get(device_id, 0.0)
                >= BRIDGE_SETTLE_SECONDS)
            present = device_id in assignments
            backend_active = mm_active
            cellular_state = self.cellular_states.get(device_id) or {}
            radio_enabled = self.radio_states.get(device_id)
            target_data_active = bool(wanted.get("cellular_enabled")) and not bool(
                wanted.get("flight_mode"))
            observed_data_active = bool(cellular_state.get("data_active"))
            # The bridge is no longer a VoWiFi actual: it runs for every present modem so
            # the card stays reachable. Comparing it against the VoWiFi switch would park
            # every switched-off modem in "stopping" forever.
            #
            # A modem bridged over the serial port is a settled outcome too, not work in
            # progress. Leaving it marked transitioning is what made a modem ModemManager
            # refused read as an indefinite spinner with no explanation; the reason belongs
            # in the error field instead.
            degraded = self._degraded.get(device_id, "")
            device_transitioning = bool(transitioning or (not degraded and
                present and (target_data_active != observed_data_active or
                             (backend_active and radio_enabled is not None and
                              bool(wanted.get("flight_mode")) == radio_enabled) or
                             (bool(desired_devices) and not backend_active))))
            devices[device_id] = {
                "id": device_id,
                "name": assignment.get("name") or "USB modem",
                "tty": assignment.get("tty") or "",
                "mm_object": self.modemmanager_modem_for_tty(assignment.get("tty") or "")
                    if mm_active and assignment.get("tty") else "",
                "desired": wanted,
                # Registration and bearer state come from this device's ModemManager object.
                "actual": {"cellular_backend_active": backend_active,
                           "cellular_radio_enabled": radio_enabled,
                           "flight_mode_active": radio_enabled is False,
                           "vowifi_bridge_active": vowifi_actual,
                           # Which path is carrying SIM traffic, so a modem serving VoWiFi
                           # without any cellular capability is not read as half-broken.
                           "vowifi_backend": ("direct-serial" if degraded else
                                              "modemmanager" if vowifi_actual else "")},
                "cellular": cellular_state,
                "present": present,
                "transitioning": device_transitioning,
                "error": (error or ("device is not connected" if not present else "")
                          or " ".join(part for part in (
                              degraded,
                              # The exit record is the only place the actual exception
                              # lands; without it a failing takeover reads as success.
                              (f"The SIM bridge keeps exiting "
                               f"({bridge_failure['count']} attempt(s), last after "
                               f"{bridge_failure['uptime']}s"
                               + (f": {bridge_failure['reason']}"
                                  if bridge_failure.get("reason") else "") + ")")
                              if bridge_failure and not vowifi_actual else "",
                          ) if part)),
            }
        atomic_json(self.device_status_path, {
            "version": 2, "updated_at": int(time.time()), "devices": devices,
            "shared": {
                "cellular_backend": "modemmanager-per-device" if mm_active else "unavailable",
                "modemmanager_active": mm_active,
                "transitioning": bool(transitioning), "error": error,
                "disruption": disruption or "",
                "affected_devices": sorted(affected_devices or []),
                "isolation": "ModemManager is shared; radio, registration and NetworkManager data profiles are scoped per modem",
            },
        })

    def stop_bridges(self):
        """Release the exclusive AT port before ModemManager starts."""
        for hwid, proc in list(self.bridges.items()):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            self.bridges.pop(hwid, None)
            self.bridge_ports.pop(hwid, None)

    def reset_modems_after_cellular(self):
        """Reset EC25-class modems after ModemManager releases QMI/UIM ownership."""
        if serial is None:
            raise RuntimeError("pyserial is required to reset the modem after ModemManager releases it")
        assignments = read_json(self.hw_state_path).get("assignments") or {}
        ports = sorted({str(value.get("tty") or "") for value in assignments.values()
                        if value.get("tty")})
        if not ports:
            ports = sorted(str(path) for path in Path("/dev").glob("ttyUSB2"))
        errors = []
        reset = 0
        for port in ports:
            if not Path(port).exists():
                continue
            try:
                modem = serial.Serial(port, 115200, timeout=.5, write_timeout=2,
                                      exclusive=True)
                try:
                    modem.reset_input_buffer()
                    modem.write(b"AT+CFUN=1,1\r")
                    modem.flush()
                    time.sleep(1)
                finally:
                    modem.close()
                reset += 1
            except Exception as exc:
                errors.append(f"{port}: {exc}")
        if not reset and errors:
            raise RuntimeError("modem reset failed: " + "; ".join(errors))
        if reset:
            # USB serial ports disappear and return after the module reboot.
            time.sleep(12)

    def _bridge_stderr_path(self, hwid: str):
        return self.root / f"bridge-{hwid}.stderr.log"

    def _bridge_stderr_tail(self, hwid: str) -> str:
        """The last lines a dead bridge wrote — normally the exception that killed it."""
        try:
            text = self._bridge_stderr_path(hwid).read_text(errors="replace")
        except OSError:
            return ""
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return " | ".join(lines[-2:])[-300:]

    def _record_bridge_exit(self, hwid: str, proc, started: float) -> None:
        """Keep an exited bridge visible instead of silently respawning over it."""
        uptime = time.time() - started if started else 0.0
        previous = self._bridge_failures.get(hwid)
        # A crash after a long healthy run is a fresh incident, not an escalation.
        count = previous["count"] + 1 if previous and uptime < BRIDGE_STABLE_SECONDS else 1
        reason = self._bridge_stderr_tail(hwid)
        self._bridge_failures[hwid] = {"count": count, "at": time.time(), "reason": reason,
                                       "returncode": proc.returncode,
                                       "uptime": round(uptime, 1)}
        self.log(f"SIM bridge for {hwid} exited after {uptime:.0f}s "
                 f"(rc={proc.returncode}, attempt {count})"
                 + (f": {reason}" if reason else ""))

    def _bridge_retry_due(self, hwid: str) -> bool:
        failure = self._bridge_failures.get(hwid)
        if not failure:
            return True
        delay = min(BRIDGE_RETRY_BASE_SECONDS * (2 ** (failure["count"] - 1)),
                    BRIDGE_RETRY_CEILING_SECONDS)
        return time.time() - failure["at"] >= delay

    def mm_refusal_logged(self, modem: dict) -> bool:
        """ModemManager has already said it will not create a modem for this hardware.

        Its refusal only ever appears in its own journal, which the previous cycle's claim
        evidence captured. Acting on it immediately spares the affected host the full grace
        period on every boot — three minutes of dead air per start, forever, on a machine
        whose ModemManager can never claim the modem.
        """
        usb_path = str(modem.get("usb_path") or "")
        if not usb_path:
            return False
        for line in self._claim_evidence.get("modemmanager_journal") or []:
            if "couldn't create modem" in line and usb_path in line:
                return True
        return False

    def claim_wait(self, tty: str) -> float:
        """Seconds this tty has been continuously unclaimed by ModemManager.

        The clock starts on the first cycle that finds it unclaimed, so a modem still being
        probed is never mistaken for one ModemManager has refused. It is reset the moment a
        claim succeeds, and a replug retires the entry with the rest of the device state.
        """
        first_seen = self._unclaimed_since.setdefault(tty, time.time())
        return time.time() - first_seen

    def driver_slots(self) -> int:
        """Slots the installed libifdvpcd was compiled for.

        The installer records this beside the driver when it builds one; without that marker
        the packaged build is in place, which upstream compiles for two. Guessing higher
        would recreate the very symptom this exists to avoid, so the fallback is the
        conservative one.
        """
        for parent in (Path(item) for item in VPCD_DRIVER_DIRS):
            for marker in parent.glob("**/.mdd-vpcd-slots-*"):
                try:
                    return max(1, int(marker.name.rsplit("-", 1)[1]))
                except (IndexError, ValueError):
                    continue
        return VPCD_PACKAGED_SLOTS

    def virtualization(self) -> str:
        """Cached hypervisor/container label; deployments differ mostly in this one word."""
        if self._virtualization is None:
            result = run(["systemd-detect-virt"])
            self._virtualization = (result.stdout or "").strip() or "none"
        return self._virtualization

    def claim_evidence(self, ttys: list[str]) -> dict:
        """Record why ModemManager owns no object for these ttys.

        A bridge that never starts leaves no trace beyond one repeating log line, so the
        support bundle has to carry what an operator would otherwise be asked to run by
        hand. Port lines are kept because they are exactly what the claim check matches
        against; everything else mmcli prints is identity material the bundle must not
        grow.
        """
        listing = run(["mmcli", "-L"])
        objects = re.findall(r"(/org/freedesktop/ModemManager1/Modem/\d+)",
                             listing.stdout or "")
        ports = {}
        for obj in objects:
            detail = run(["mmcli", "-m", obj, "--output-keyvalue"])
            ports[obj] = [line.strip() for line in (detail.stdout or "").splitlines()
                          if re.search(r"\.(?:ports|state)\b", line)]
        evidence = {"observed_at": int(time.time()), "unclaimed_ttys": sorted(ttys),
                    "mmcli_list_returncode": listing.returncode,
                    "modem_objects": objects, "modem_ports": ports}
        if ttys:
            # ModemManager only ever explains a refusal in its own journal — which the
            # control-plane container cannot read, and which the next cycle also uses to
            # skip the remaining grace period once a refusal for this hardware is on
            # record. Captured whenever a tty is unclaimed, because ModemManager may hold
            # objects for other modems while refusing this one.
            journal = run(["journalctl", "-u", "ModemManager.service", "-n", "40",
                           "--no-pager", "-p", "warning"])
            evidence["modemmanager_journal"] = [
                line for line in (journal.stdout or "").splitlines() if line.strip()]
        return evidence

    def publish_host_diagnostics(self, discovered: list[dict], assignments: dict,
                                 mm_active: bool, cellular_required: bool,
                                 vowifi_required: bool) -> None:
        """Publish the host-side view the control plane cannot observe for itself.

        The control plane runs in a container without systemd or the host USB tree, so
        every fact here would otherwise reach a maintainer only by asking the operator to
        run commands. Fields are drawn from state this cycle already computed; nothing is
        collected merely to fill the file.
        """
        atomic_json(self.host_diagnostics_path, {
            "version": 1,
            "updated_at": int(time.time()),
            "virtualization": self.virtualization(),
            "modemmanager": {
                # The claim check depends on this unit being reported active; a mismatch
                # against mmcli working by hand is itself the diagnosis.
                "unit_active": mm_active,
                "required": cellular_required,
                "applied": self.applied_cellular_backend,
                "unclaimed": self._claim_evidence,
                "claim_grace_seconds": MM_CLAIM_GRACE_SECONDS,
                # Modems whose bridge gave up on ModemManager and drives the tty itself.
                "degraded_to_direct_serial": dict(self._degraded),
                "bridge_failures": dict(self._bridge_failures),
            },
            "discovered_modems": discovered,
            "assignments": assignments,
            "bridges": {hwid: {"pid": proc.pid, "running": proc.poll() is None}
                        for hwid, proc in self.bridges.items()},
            "country_egress_required": vowifi_required,
            "reader_config": {"path": str(self.reader_config_path),
                              "stanzas": self.last_reader_config.count("FRIENDLYNAME")},
            "recent_log": list(self._log_ring),
        })

    @staticmethod
    def modemmanager_modem_for_tty(tty: str) -> str:
        """Return the ModemManager object owning a tty, supporting multiple modules."""
        listing = run(["mmcli", "-L"])
        if listing.returncode:
            return ""
        objects = re.findall(r"(/org/freedesktop/ModemManager1/Modem/\d+)", listing.stdout)
        basename = Path(tty).name
        for obj in objects:
            detail = run(["mmcli", "-m", obj, "--output-keyvalue"])
            if detail.returncode == 0 and re.search(rf"(?<![A-Za-z0-9_.-]){re.escape(basename)}(?![A-Za-z0-9_.-])",
                                                   detail.stdout):
                return obj
        return ""

    @staticmethod
    def _kv(text: str, key: str) -> str:
        match = re.search(rf"^{re.escape(key)}\s*:\s*(.*?)\s*$", text or "", re.MULTILINE)
        return match.group(1).strip() if match else ""

    @staticmethod
    def normalize_msisdn(value: str) -> str:
        """Return a conservative E.164-like number from ModemManager OwnNumbers.

        Modems commonly add spaces, dashes or parentheses.  Reject placeholders and
        anything containing other characters so a driver status string can never be
        persisted as a line number.
        """
        text = str(value or "").strip()
        if not text or text in {"--", "unknown", "none"}:
            return ""
        if not re.fullmatch(r"\+?[0-9 ()-]+", text):
            return ""
        number = ("+" if text.startswith("+") else "") + re.sub(r"\D", "", text)
        digits = number.lstrip("+")
        return number if 5 <= len(digits) <= 20 else ""

    @staticmethod
    def cellular_profile_name(device_id: str) -> str:
        digest = hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:12]
        return f"mdd-cell-{digest}"

    def modem_snapshot(self, modem: dict) -> dict:
        obj = self.modemmanager_modem_for_tty(modem.get("tty") or "")
        if not obj:
            return {"available": False, "registration": "unknown", "data_active": False}
        detail = run(["mmcli", "-m", obj, "--output-keyvalue"])
        if detail.returncode:
            return {"available": False, "registration": "unknown", "data_active": False}
        text = detail.stdout or ""
        power = self._kv(text, "modem.generic.power-state").lower()
        state = self._kv(text, "modem.generic.state").lower()
        primary = self._kv(text, "modem.generic.primary-port")
        ports = re.findall(r"modem\.generic\.ports\.value\[\d+\]\s*:\s*([^ ]+) \(([^)]+)\)", text)
        network_port = next((name for name, kind in ports if kind == "net"), "")
        registration = self._kv(text, "modem.3gpp.registration-state").lower() or "unknown"
        if registration in {"--", "none", "n/a"}:
            registration = "unknown"
        signal = self._kv(text, "modem.generic.signal-quality.value")
        own_numbers = re.findall(
            r"^modem\.generic\.own-numbers\.value\[\d+\]\s*:\s*(.*?)\s*$",
            text, re.MULTILINE)
        msisdn = next((number for raw in own_numbers
                       if (number := self.normalize_msisdn(raw))), "")
        sim_iccid = ""
        sim_object = self._kv(text, "modem.generic.sim")
        if sim_object and sim_object not in {"--", "/"}:
            sim_detail = run(["mmcli", "-i", sim_object, "--output-keyvalue"])
            if sim_detail.returncode == 0:
                sim_iccid = self._kv(sim_detail.stdout or "", "sim.properties.iccid")
        # Many USB modems keep their hardware power-state at "on" after
        # ModemManager --disable.  The generic state is the authoritative RF state.
        radio_enabled = power == "on" and state not in {
            "disabled", "disabling", "failed", "unknown"}
        operator = self._kv(text, "modem.3gpp.operator-name")
        if operator.casefold() in {"--", "unknown", "none", "n/a"}:
            operator = ""
        snapshot = {
            "available": True, "mm_object": obj, "powered": power == "on",
            "radio_enabled": radio_enabled,
            "state": state, "registration": registration,
            "operator": operator,
            "signal": int(signal) if signal.isdigit() else None,
            "primary_port": primary, "network_interface": network_port,
            "data_active": state == "connected", "apn": "", "ip": "",
            "rx_bytes": 0, "tx_bytes": 0,
            "profile": self.cellular_profile_name(modem["id"]),
            # These fields are sensitive and are redacted from support bundles by key.
            # The control plane uses the ICCID pair as a fail-closed match before it
            # copies OwnNumbers into a line configuration.
            "msisdn": msisdn, "sim_iccid": sim_iccid,
        }
        bearer_paths = re.findall(r"modem\.generic\.bearers\.value\[\d+\]\s*:\s*(\S+)", text)
        for bearer in bearer_paths:
            info = run(["mmcli", "-b", bearer, "--output-keyvalue"])
            if info.returncode:
                continue
            body = info.stdout or ""
            # ModemManager keeps the last carrier APN on disconnected bearers.  Preserve it so
            # a freshly-created NetworkManager profile can reconnect without carrier-specific
            # hard-coding (auto-config is not implemented by every modem/operator combination).
            bearer_apn = self._kv(body, "bearer.properties.apn")
            if bearer_apn and not snapshot["apn"]:
                snapshot["apn"] = bearer_apn
            if self._kv(body, "bearer.status.connected").lower() == "yes":
                snapshot["data_active"] = True
                snapshot["apn"] = bearer_apn
                snapshot["ip"] = (self._kv(body, "bearer.ipv4-config.address") or
                                  self._kv(body, "bearer.ipv6-config.address"))
                rx = self._kv(body, "bearer.stats.rx-bytes")
                tx = self._kv(body, "bearer.stats.tx-bytes")
                snapshot["rx_bytes"] = int(rx) if rx.isdigit() else 0
                snapshot["tx_bytes"] = int(tx) if tx.isdigit() else 0
                break
        return snapshot

    @staticmethod
    def _active_gsm_profiles() -> list[tuple[str, str]]:
        result = run(["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"])
        profiles = []
        if result.returncode == 0:
            for line in (result.stdout or "").splitlines():
                parts = line.rsplit(":", 2)
                if len(parts) == 3 and parts[1] == "gsm":
                    profiles.append((parts[0].replace(r"\:", ":"), parts[2]))
        return profiles

    def ensure_modem_data(self, modem: dict, snapshot: dict) -> None:
        """Give each modem its own NetworkManager GSM profile and bearer."""
        if not snapshot.get("powered") or snapshot.get("data_active"):
            return
        registration = snapshot.get("registration")
        if registration not in {"home", "roaming", "registered"}:
            return
        device_id = modem["id"]
        if time.monotonic() - self.data_attempt_at.get(device_id, 0) < 45:
            return
        self.data_attempt_at[device_id] = time.monotonic()
        primary = snapshot.get("primary_port") or snapshot.get("network_interface")
        if not primary:
            return
        active = self._active_gsm_profiles()
        if any(device == primary for _name, device in active):
            return
        profile = self.cellular_profile_name(device_id)
        apn = str(snapshot.get("apn") or "").strip()
        exists = run(["nmcli", "connection", "show", profile]).returncode == 0
        if not exists:
            command = ["nmcli", "connection", "add", "type", "gsm", "ifname", primary,
                       "con-name", profile, "connection.autoconnect", "yes",
                       "connection.autoconnect-retries", "0"]
            if apn:
                command.extend(["gsm.apn", apn, "gsm.auto-config", "no"])
            else:
                command.extend(["gsm.auto-config", "yes"])
            result = run(command)
            if result.returncode:
                self.log(f"could not create cellular profile for {device_id}: "
                         f"{(result.stderr or result.stdout).strip()}")
                return
        elif apn:
            # A profile may have been created before the retained bearer APN became visible.
            result = run(["nmcli", "connection", "modify", profile,
                          "gsm.apn", apn, "gsm.auto-config", "no"])
            if result.returncode:
                self.log(f"could not update cellular APN for {device_id}: "
                         f"{(result.stderr or result.stdout).strip()}")
                return
        result = run(["nmcli", "connection", "up", profile])
        if result.returncode:
            self.log(f"could not activate cellular profile for {device_id}: "
                     f"{(result.stderr or result.stdout).strip()}")

    def disconnect_modem_data(self, snapshot: dict) -> None:
        primary = snapshot.get("primary_port") or snapshot.get("network_interface")
        for name, device in self._active_gsm_profiles():
            if primary and device == primary:
                run(["nmcli", "connection", "down", name])

    def apply_cellular_backend(self, enabled: bool):
        """Apply the shared cellular backend required by one or more physical modems.

        ModemManager cannot be toggled per modem. Starting/stopping it is therefore deliberately
        based on the aggregate request, while bridge creation remains per-device.
        """
        if self.dry_run:
            self.applied_cellular_backend = enabled
            return
        self.retire_obsolete_services()
        if enabled:
            self.stop_bridges()
            # ModemManager may have been installed after this already-enumerated USB modem.
            # Re-run its udev candidate rules or every EC25 port is ignored until the next
            # physical replug/reboot.
            run(["udevadm", "control", "--reload-rules"])
            for subsystem in ("tty", "usbmisc", "net"):
                run(["udevadm", "trigger", "--action=add",
                     f"--subsystem-match={subsystem}"])
            run(["udevadm", "settle"])
            result = run(["systemctl", "start", "ModemManager.service"])
            if result.returncode:
                raise RuntimeError("could not start ModemManager: " +
                                   (result.stderr or result.stdout).strip())
            # Ask ModemManager to enumerate immediately; the recovery unit remains an explicit
            # fallback because it intentionally waits 60 seconds and is unsuitable inline.
            run(["mmcli", "--scan-modems"])
            self.applied_cellular_backend = True
            return

        # Stop an MM-backed bridge before taking ModemManager down; it cannot be reused as a
        # direct-serial bridge after the modem reset.
        modemmanager_was_active = self.service_active("ModemManager.service")
        if modemmanager_was_active:
            self.stop_bridges()
        run(["systemctl", "stop", "ModemManager.service"])
        run(["pkill", "-x", "qmi-proxy"])
        time.sleep(1)
        if modemmanager_was_active:
            self.reset_modems_after_cellular()

        self.applied_cellular_backend = False

    def retire_obsolete_services(self):
        """Keep removed stack units out of modem ownership; no code path starts them."""
        if self.obsolete_services_retired or self.dry_run:
            return
        for unit in ("singbox-node-sync.timer", "singbox-node-sync.service",
                     "vohive.service", "vohive-webhook-adapter.service",
                     "singbox-vowifi.service"):
            # Missing units and already-disabled units are harmless. --now prevents a formerly
            # enabled unit from racing for the modem during any later host reboot.
            run(["systemctl", "disable", "--now", unit])
        self.obsolete_services_retired = True

    @staticmethod
    def normalize_capabilities(value: dict | None) -> dict:
        value = value or {}
        return {"cellular_enabled": bool(value.get("cellular_enabled", False)),
                "vowifi_enabled": bool(value.get("vowifi_enabled", True)),
                "flight_mode": bool(value.get("flight_mode", False))}

    @staticmethod
    def device_capability_plan(value: dict | None) -> dict:
        """Resolve one modem's three desired switches into effective hardware actions."""
        wanted = Orchestrator.normalize_capabilities(value)
        flight_mode = wanted["flight_mode"]
        return {
            "flight_mode": flight_mode,
            "radio_enabled": not flight_mode,
            "cellular_data_requested": wanted["cellular_enabled"],
            # Flight mode always wins over a still-saved 4G preference.  Keeping the
            # preference lets data reconnect when flight mode is later disabled.
            "cellular_data_enabled": wanted["cellular_enabled"] and not flight_mode,
            # The VoWiFi switch governs the line engine only. The card bridge follows the
            # hardware, because provisioning a line requires reading the card first.
            "vowifi_line_enabled": wanted["vowifi_enabled"],
        }

    @staticmethod
    def capability_plan(desired_devices: dict) -> dict:
        """Pure aggregate plan built from each modem's effective hardware actions."""
        cellular = sorted(device_id for device_id, state in desired_devices.items()
                          if state.get("cellular_enabled"))
        vowifi = sorted(device_id for device_id, state in desired_devices.items()
                        if state.get("vowifi_enabled"))
        effective = {device_id: Orchestrator.device_capability_plan(state)
                     for device_id, state in desired_devices.items()}
        return {"cellular_devices": cellular, "vowifi_devices": vowifi,
                "effective_cellular_devices": sorted(
                    device_id for device_id, plan in effective.items()
                    if plan["cellular_data_enabled"]),
                "flight_mode_devices": sorted(
                    device_id for device_id, plan in effective.items()
                    if plan["flight_mode"]),
                "radio_enabled_devices": sorted(
                    device_id for device_id, plan in effective.items()
                    if plan["radio_enabled"]),
                # Keep ModemManager active while a physical modem is present.  The 4G switch
                # controls only its data bearer; flight mode separately controls RF power.
                "cellular_backend_required": bool(desired_devices),
                "country_egress_required": bool(vowifi),
                "vowifi_through_modemmanager": bool(desired_devices)}

    @staticmethod
    def country_egress_required(desired: dict, modem_plan: dict) -> bool:
        """Keep egress active for native-reader lines even with no USB modem present."""
        if modem_plan.get("country_egress_required"):
            return True
        return any(bool(line.get("enabled", True))
                   for line in (desired.get("lines") or []) if isinstance(line, dict))

    def desired_devices(self, discovered: list[dict]) -> tuple[dict, bool]:
        """Load per-device state, creating safe defaults once when necessary."""
        document = read_json(self.device_desired_path)
        migrated = False
        if not document:
            defaults = {"cellular_enabled": False, "vowifi_enabled": True,
                        "flight_mode": False}
            document = {"version": 2, "defaults": defaults, "devices": {},
                        "updated_at": int(time.time())}
            atomic_json(self.device_desired_path, document)
            migrated = True
        defaults = self.normalize_capabilities(document.get("defaults"))
        configured = document.get("devices") or {}
        devices = {str(device_id): self.normalize_capabilities(state)
                   for device_id, state in configured.items() if str(device_id)}
        for modem in discovered:
            devices.setdefault(modem["id"], defaults.copy())
        return devices, migrated

    def apply_device_radios(self, discovered: list[dict], desired_devices: dict,
                            through_modemmanager: bool):
        """Apply independent RF (flight mode) and cellular-data intent per modem."""
        for modem in discovered:
            device_id = modem["id"]
            wanted = desired_devices.get(device_id) or {}
            plan = self.device_capability_plan(wanted)
            data_enabled = plan["cellular_data_enabled"]
            radio_enabled = plan["radio_enabled"]
            if not through_modemmanager and self.radio_states.get(device_id) == radio_enabled:
                continue
            if self.dry_run:
                self.radio_states[device_id] = radio_enabled
                continue
            if through_modemmanager:
                obj = self.modemmanager_modem_for_tty(modem["tty"])
                if not obj:
                    continue
                snapshot = self.modem_snapshot(modem)
                observed = snapshot.get("radio_enabled") if snapshot.get("available") else None
                if observed == radio_enabled:
                    self.radio_states[device_id] = radio_enabled
                    if radio_enabled and data_enabled:
                        self.ensure_modem_data(modem, snapshot)
                    else:
                        self.disconnect_modem_data(snapshot)
                    self.cellular_states[device_id] = self.modem_snapshot(modem)
                    continue
                if not radio_enabled:
                    self.disconnect_modem_data(snapshot)
                result = run(["mmcli", "-m", obj,
                              "--enable" if radio_enabled else "--disable"])
                # ModemManager may report 'already enabled/disabled' as an error; verify by
                # accepting that wording, while preserving unknown failures for retry.
                output = (result.stdout or "") + (result.stderr or "")
                if result.returncode and "already" not in output.lower():
                    self.log(f"could not set cellular radio for {device_id}: {output.strip()}")
                    continue
            else:
                if serial is None:
                    continue
                try:
                    modem_port = serial.Serial(modem["tty"], 115200, timeout=.5,
                                               write_timeout=2, exclusive=True)
                    try:
                        modem_port.write(b"AT+CFUN=1\r" if radio_enabled else b"AT+CFUN=4\r")
                        modem_port.flush()
                        time.sleep(.3)
                    finally:
                        modem_port.close()
                except Exception as exc:
                    self.log(f"could not set cellular radio for {device_id}: {exc}")
                    continue
            self.radio_states[device_id] = radio_enabled
            if through_modemmanager:
                fresh = self.modem_snapshot(modem)
                if radio_enabled and data_enabled:
                    self.ensure_modem_data(modem, fresh)
                else:
                    self.disconnect_modem_data(fresh)
                self.cellular_states[device_id] = self.modem_snapshot(modem)

    def log(self, message):
        line = f"{time.strftime('%F %T')} {message}"
        # Retained as well as printed: journalctl is out of reach from the control-plane
        # container, so the support bundle would otherwise show no reason for a stall.
        self._log_ring.append(line)
        print(line, flush=True)

    def reconcile_timezone(self):
        """Apply the validated WebUI timezone to the host without changing its hostname."""
        try:
            document = yaml.safe_load((self.data / "config.yaml").read_text()) or {}
            timezone = str((document.get("settings") or {}).get("timezone") or "").strip()
        except Exception:
            return
        if not timezone or timezone == self.applied_timezone:
            return
        zone = Path("/usr/share/zoneinfo") / timezone
        try:
            if not zone.resolve().is_relative_to(Path("/usr/share/zoneinfo").resolve()) or not zone.is_file():
                raise ValueError("invalid timezone")
        except (OSError, ValueError):
            self.log(f"ignored invalid timezone {timezone!r}")
            return
        if not self.dry_run:
            result = run(["timedatectl", "set-timezone", timezone])
            if result.returncode:
                self.log(f"could not apply timezone: {(result.stderr or result.stdout).strip()}")
                return
        self.applied_timezone = timezone

    def subscription(self, url: str, refresh_minutes: int, profile_id: str = "legacy") -> dict:
        safe_id = slug(profile_id)[:64]
        cache = self.root / "subscriptions" / f"{safe_id}.yaml"
        # Preserve the pre-profile cache for a seamless first restart after upgrade.
        if profile_id == "legacy" and self.cache.exists() and not cache.exists():
            cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(self.cache, cache)
        stale = not cache.exists() or time.time() - cache.stat().st_mtime > max(1, refresh_minutes) * 60
        if stale:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "mdd-sim-gateway/1"})
                with urllib.request.urlopen(req, timeout=20) as response:
                    body = response.read(8 * 1024 * 1024)
                cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                cache.write_bytes(body)
                os.chmod(cache, 0o600)
            except Exception:
                # A refresh outage must not discard the last known-good subscription. The
                # urltest pool keeps probing its members while we retry the feed next cycle.
                if not cache.exists():
                    raise
        if yaml is None:
            raise RuntimeError("PyYAML is required for subscription mode")
        return yaml.safe_load(cache.read_text(encoding="utf-8")) or {}

    def xhttp_bridge_outbound(self, node: dict, sing_tag: str, runtime_id: str) -> dict:
        """Register one loopback-only Xray XHTTP endpoint and return its sing-box detour."""
        if runtime_id not in self._xray_ports:
            # Stable allocation with collision probing. Ports never leave loopback.
            port = 24000 + int(hashlib.sha256(runtime_id.encode()).hexdigest()[:6], 16) % 1000
            used = set(self._xray_ports.values())
            while port in used:
                port = 24000 + ((port - 23999) % 1000)
            self._xray_ports[runtime_id] = port
            inbound_tag, outbound_tag = f"in-{slug(runtime_id)}", f"out-{slug(runtime_id)}"
            self._xray_inbounds.append({"listen": "127.0.0.1", "port": port,
                                        "protocol": "socks", "tag": inbound_tag,
                                        "settings": {"auth": "noauth", "udp": True,
                                                     "ip": "127.0.0.1"}})
            self._xray_outbounds.append(xray_xhttp_outbound(node, outbound_tag))
            self._xray_rules.append({"type": "field", "inboundTag": [inbound_tag],
                                     "outboundTag": outbound_tag})
        return {"type": "socks", "tag": sing_tag, "version": "5",
                "server": "127.0.0.1", "server_port": self._xray_ports[runtime_id]}

    def node_outbound(self, node: dict, tag: str, runtime_id: str) -> dict:
        if str(node.get("network") or "").lower() == "xhttp":
            return self.xhttp_bridge_outbound(node, tag, runtime_id)
        return clash_outbound(node, tag)

    def build_proxy_config(self, proxy: dict) -> tuple[dict, dict]:
        inbounds, outbounds, rules, state = [], [], [], {}
        self._xray_inbounds, self._xray_outbounds, self._xray_rules, self._xray_ports = [], [], [], {}
        tun_index = 0
        existing_path = str(proxy.get("existing_singbox_config") or "").strip()
        existing = read_json(Path(existing_path)) if existing_path else {}
        existing_by_tag = {x.get("tag"): x for x in existing.get("outbounds", []) if x.get("tag")}
        subscriptions: dict[str, dict] = {}
        profiles = proxy.get("profiles") or {}
        for country, exit_cfg in sorted((proxy.get("exits") or {}).items()):
            country = str(country).lower()
            if not re.fullmatch(r"[a-z]{2}", country) or not exit_cfg.get("enabled"):
                continue
            mode = str(exit_cfg.get("mode") or "subscription").lower()
            profile_id = str(exit_cfg.get("profile_id") or "").strip()
            profile = profiles.get(profile_id) if profile_id else None
            if isinstance(profile, dict):
                profile_type = str(profile.get("type") or "").lower()
                mode = "subscription" if profile_type == "subscription" else "existing" \
                    if profile_type == "existing" else "manual"
            tag = f"exit-{country}"
            if mode == "direct":
                state[country] = {"ready": True, "mode": mode, "interface": ""}
                continue
            try:
                if mode == "manual":
                    value = (profile or {}).get("value") if profile else None
                    if profile and profile.get("type") == "socks5" and not value:
                        host = str(profile.get("server") or "").strip()
                        port = int(profile.get("port") or 1080)
                        outbound = {"type": "socks", "tag": tag, "version": "5",
                                    "server": host, "server_port": port}
                        if profile.get("username"):
                            outbound["username"] = str(profile["username"])
                        if profile.get("password"):
                            outbound["password"] = str(profile["password"])
                        if not host:
                            raise ValueError("SOCKS5 server is empty")
                    else:
                        value = value or exit_cfg.get("outbound_json") or exit_cfg.get("proxy_url") or ""
                        text = str(value or "").strip()
                        if text.lower().startswith("vless://"):
                            node = parse_share_link(text)
                            outbound = self.node_outbound(node, tag, profile_id or f"country-{country}")
                        else:
                            outbound = parse_manual_outbound(value, tag)
                elif mode == "existing":
                    source_tag = str((profile or {}).get("outbound_tag")
                                     or exit_cfg.get("outbound_tag") or "").strip()
                    if source_tag not in existing_by_tag:
                        raise ValueError(f"outbound tag {source_tag!r} not found in existing config")
                    outbound = dict(existing_by_tag[source_tag]); outbound["tag"] = tag
                    if not outbound_supports_udp(outbound):
                        raise ValueError(f"outbound {source_tag!r} is not UDP-capable")
                elif mode == "subscription":
                    subscription_id = profile_id or "legacy"
                    if subscription_id not in subscriptions:
                        url = str((profile or {}).get("url") or proxy.get("subscription_url") or "").strip()
                        if not url:
                            raise ValueError("subscription URL is empty")
                        refresh = int((profile or {}).get("refresh_minutes")
                                      or proxy.get("refresh_minutes") or 30)
                        subscriptions[subscription_id] = self.subscription(url, refresh, subscription_id)
                    words = [str(x).lower() for x in (exit_cfg.get("keywords") or []) if str(x).strip()]
                    nodes = subscriptions[subscription_id].get("proxies") or []
                    named = [n for n in nodes if not words or any(
                        node_keyword_matches(n.get("name", ""), w) for w in words)]
                    matches = [n for n in named if clash_node_supports_udp(n)]
                    if not matches:
                        if named:
                            raise ValueError("matching nodes exist, but none are UDP-capable for VoWiFi IKE")
                        raise ValueError("no subscription node matched country keywords")
                    matches.sort(key=lambda n: str(n.get("name", "")))
                    member_tags = []
                    member_names = {}
                    for index, node in enumerate(matches[:32]):
                        member_tag = f"{tag}-{index}"
                        outbounds.append(self.node_outbound(
                            node, member_tag, f"{subscription_id}-{hashlib.sha256(str(node).encode()).hexdigest()[:12]}"))
                        member_tags.append(member_tag)
                        member_names[member_tag] = str(node.get("name") or member_tag)
                    # An operator can pin one node by its subscription name. Pinning matches on
                    # the name because member tags are positional and shift whenever the feed
                    # adds or drops a node.
                    pinned_name = str(exit_cfg.get("pinned_node") or "").strip()
                    pinned_tag = next((member for member, name in member_names.items()
                                       if name == pinned_name), "") if pinned_name else ""
                    # "lock" keeps the exit on this node whatever happens, which is what an
                    # operator running a controlled comparison needs. "prefer" gives up that
                    # guarantee in exchange for failover.
                    pin_mode = str(exit_cfg.get("pin_mode") or "lock").lower()
                    # Always a selector: it never switches on its own. A urltest re-ranks the
                    # pool on a timer and switches whenever another node measures faster, which
                    # changes the outer source address underneath an established IKE SA. For a
                    # VoWiFi tunnel that trade is pure loss — latency does not matter once the
                    # tunnel is up, and the switch costs a re-registration. Node changes are
                    # driven instead by reselect requests, i.e. by a line actually failing.
                    # A restart resets the selector to whatever this config names as its
                    # default. Naming the node that is already carrying tunnels makes the
                    # restart land back where it was, instead of ranking by latency and moving
                    # a healthy line — which is how a config rewrite flipped a country between
                    # two nodes four times in an hour without a single line ever failing.
                    running = self.last_exit_node.get(country) or ""
                    resumed = next((member for member, name in member_names.items()
                                    if name == running), "") if running else ""
                    # A lock is a standing instruction and therefore still wins on every
                    # restart.  A preference is different: it is consulted when an exit has
                    # to be selected, but must not undo a failure-driven fallback merely
                    # because an unrelated config change restarts sing-box.
                    if pinned_tag and pin_mode == "lock":
                        default_tag = pinned_tag
                    else:
                        default_tag = resumed or pinned_tag or member_tags[0]
                    self.exit_resume[country] = (
                        default_tag if running and member_names.get(default_tag) == running
                        else "")
                    outbound = {"type": "selector", "tag": tag, "outbounds": member_tags,
                                "default": default_tag}
                else:
                    raise ValueError(f"unknown exit mode {mode!r}")
                iface = tun_name(country)
                proxy_port = country_proxy_port(country)
                inbounds.append({"type": "tun", "tag": f"tun-{country}", "interface_name": iface,
                                 "address": [f"172.29.{20 + tun_index}.1/30"], "auto_route": False,
                                 "strict_route": True})
                tun_index += 1
                # TCP/UDP SOCKS entry reachable only from Docker's host bridge. The control
                # container uses host.docker.internal, while LAN clients cannot reach it.
                inbounds.append({"type": "socks", "tag": f"proxy-{country}",
                                 "listen": COUNTRY_PROXY_LISTEN, "listen_port": proxy_port})
                outbounds.append(outbound)
                rules.append({"inbound": [f"tun-{country}", f"proxy-{country}"], "outbound": tag})
                state[country] = {"ready": True, "mode": mode, "interface": iface,
                                  "proxy_host": COUNTRY_PROXY_LISTEN,
                                  "proxy_port": proxy_port,
                                  "node": str((profile or {}).get("name")
                                              or outbound.get("server") or outbound.get("type"))}
                if mode == "subscription":
                    # Kept only in memory until the Clash API reports which urltest member is
                    # active. proxy-status.json publishes the original subscription node name,
                    # never the internal exit-gb-1 tag or the generic word "urltest".
                    state[country]["node"] = ""
                    state[country]["_member_names"] = member_names
                    state[country]["candidate_count"] = len(member_names)
                    state[country]["udp_rejected_count"] = len(named) - len(matches)
                    state[country]["udp_required"] = True
                    # The WebUI builds its node picker from this list, so it must carry the
                    # UDP-capable candidates only — offering a node the pool rejected would
                    # produce a pin that silently falls back to automatic.
                    state[country]["candidates"] = [member_names[member] for member in member_tags]
                    state[country]["pinned_node"] = pinned_name
                    state[country]["pin_mode"] = pin_mode if pinned_tag else ""
                    state[country]["selection"] = (
                        ("preferred" if pin_mode == "prefer" else "manual")
                        if pinned_tag else "managed")
                    # A pinned node disappears whenever the feed renames or drops it. Automatic
                    # selection still respects this country's keywords, so falling back cannot
                    # leak into another geography; surface it instead of failing the exit.
                    state[country]["pinned_missing"] = bool(pinned_name and not pinned_tag)
            except Exception as exc:
                state[country] = {"ready": False, "mode": mode, "error": str(exc), "terminal": True}
        config = {"log": {"level": "info"}, "inbounds": inbounds,
                  "outbounds": outbounds, "route": {"rules": rules, "auto_detect_interface": True}}
        self.next_xray_config = ({"log": {"loglevel": "warning"},
                                  "inbounds": self._xray_inbounds,
                                  "outbounds": self._xray_outbounds,
                                  "routing": {"domainStrategy": "AsIs", "rules": self._xray_rules}}
                                 if self._xray_inbounds else None)
        if any(value.get("mode") == "subscription" and value.get("ready")
               for value in state.values()):
            # Loopback-only: used to resolve urltest's current member tag to the human-readable
            # node name. It is not exposed on LAN and does not proxy user traffic.
            config["experimental"] = {"clash_api": {"external_controller": CLASH_API}}
        return config, state

    def update_selected_nodes(self, exits_state: dict):
        """Replace each subscription urltest placeholder with its selected node's real name."""
        now = time.time()
        for country, state in exits_state.items():
            # Why the exit is where it is. Without this the UI can only say the running node
            # disagrees with the pinned one, which reads as an unexplained override.
            change = self.exit_last_change.get(country)
            if change:
                state["last_change"] = change
            pinned = str(state.get("pinned_node") or "")
            cooling = (self.exit_cooldown.get(country) or {}).get(pinned) if pinned else None
            if cooling and cooling > now:
                state["pinned_cooldown_seconds"] = int(cooling - now)
            members = state.pop("_member_names", None)
            if not members or not state.get("ready"):
                continue
            tag = f"exit-{country}"
            try:
                url = f"http://{CLASH_API}/proxies/{urllib.parse.quote(tag, safe='')}"
                with urllib.request.urlopen(url, timeout=1.5) as response:
                    selected = str((json.load(response) or {}).get("now") or "")
                if selected:
                    state["node_tag"] = selected
                    state["node"] = members.get(selected, selected)
                    self.record_exit_node(country, state)
            except Exception:
                # Immediately after a restart the first health check may not have selected a
                # member yet. The next 3-second reconcile updates it; never show "urltest".
                pass

    def measure_member(self, tag: str) -> int | None:
        """Latency of one pool member, measured on demand. None when it cannot be reached.

        Measuring only at selection time replaces the timer that used to probe every member
        every three minutes. The probe still runs over TCP while the exit carries UDP, so it
        is a liveness signal for ranking candidates, never a reason to move a working line.
        """
        query = urllib.parse.urlencode({"url": EXIT_PROBE_URL, "timeout": EXIT_PROBE_TIMEOUT_MS})
        url = f"http://{CLASH_API}/proxies/{urllib.parse.quote(tag, safe='')}/delay?{query}"
        try:
            with urllib.request.urlopen(url, timeout=EXIT_PROBE_TIMEOUT_MS / 1000 + 2) as response:
                return int((json.load(response) or {}).get("delay"))
        except Exception:
            # 503 for an unusable member, or the API is briefly unavailable. Either way this
            # candidate is not a safe choice right now.
            return None

    def select_member(self, country: str, tag: str) -> bool:
        """Point a country's selector at ``tag`` through the Clash API."""
        url = f"http://{CLASH_API}/proxies/{urllib.parse.quote(f'exit-{country}', safe='')}"
        request = urllib.request.Request(
            url, data=json.dumps({"name": tag}).encode(), method="PUT",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=3):
                return True
        except Exception as exc:
            print(f"exit reselect: cannot select {tag} for {country}: {exc}", flush=True)
            return False

    def rank_and_select(self, country: str, state: dict, avoid: str = "", prefer: str = "") -> str:
        """Measure the candidates and move the selector to the best usable one.

        ``avoid`` is the node that just failed a line; it enters a cooldown so the next
        attempt tries something else. When every candidate is cooling down the cooldown is
        cleared rather than leaving the country with no exit at all.

        ``prefer`` wins over latency whenever it is usable and not cooling down. It is applied
        only here, at a moment the exit is being changed anyway — returning to a preferred node
        while a line is established would tear down a working tunnel, which is the disruption
        this whole design exists to avoid.
        """
        members = state.get("_member_names") or {}
        if not members:
            return ""
        now = time.time()
        cooldown = self.exit_cooldown.setdefault(country, {})
        if avoid:
            cooldown[avoid] = now + EXIT_RESELECT_COOLDOWN
        for name, until in list(cooldown.items()):
            if until <= now:
                cooldown.pop(name, None)
        allowed = [tag for tag, name in members.items() if name not in cooldown]
        if not allowed:
            print(f"exit reselect: every {country.upper()} candidate is cooling down; "
                  "clearing the cooldown and retrying the whole pool", flush=True)
            cooldown.clear()
            allowed = list(members)
        preferred_tag = next((tag for tag in allowed if members.get(tag) == prefer), "") if prefer else ""
        if preferred_tag:
            # A pin is an explicit operator choice, so it gets more evidence than one probe
            # before being passed over. A single sample on a path with occasional latency
            # spikes would otherwise make the preference silently ineffective.
            delay = None
            for _ in range(EXIT_PREFERRED_PROBE_ATTEMPTS):
                delay = self.measure_member(preferred_tag)
                if delay is not None:
                    break
            if delay is not None and self.select_member(country, preferred_tag):
                print(f"exit reselect: {country.upper()} -> {prefer} ({delay}ms, preferred)",
                      flush=True)
                return preferred_tag
            print(f"exit reselect: preferred {country.upper()} node {prefer} is unusable; "
                  "falling back to the rest of the pool", flush=True)
        measured = [(delay, tag) for tag, delay in
                    ((tag, self.measure_member(tag)) for tag in allowed) if delay is not None]
        if not measured:
            print(f"exit reselect: no {country.upper()} candidate answered its probe", flush=True)
            return ""
        delay, best = min(measured)
        if not self.select_member(country, best):
            return ""
        print(f"exit reselect: {country.upper()} -> {members.get(best, best)} "
              f"({delay}ms, {len(measured)}/{len(allowed)} candidates usable)", flush=True)
        return best

    def process_reselect_requests(self, exits_state: dict):
        """Move a country's exit after the control plane gave up on one of its lines.

        This is the only automatic node change. A line that cannot register is evidence the
        current exit is unusable for VoWiFi in a way no latency probe detects — the tunnel
        can be established while SIP inside it goes unanswered.

        Nothing is measured until sing-box has settled. Requests are left pending rather than
        served with cold-start numbers, so a restart cannot quietly demote a pinned node.
        """
        if time.time() - self.singbox_started_at < EXIT_RANK_WARMUP_SECONDS:
            return
        requests = (read_json(self.reselect_path) or {}).get("countries") or {}
        handled_changed = False
        for country, state in exits_state.items():
            if not state.get("ready") or state.get("mode") != "subscription":
                continue
            if state.get("selection") == "manual":
                # Locked: an operator wants this exact node, including while it is failing.
                continue
            # Preferred: the pin still steers the choice, but a failing line may move away
            # from it and come back on a later reselect once its cooldown has expired.
            prefer = (str(state.get("pinned_node") or "")
                      if state.get("selection") == "preferred" else "")
            request = requests.get(country) or {}
            requested_at = float(request.get("ts") or 0)
            pending = requested_at > self.handled_reselect.get(country, 0)
            if not pending and country not in self.exit_unranked:
                continue
            if pending and time.time() - requested_at > EXIT_RESELECT_MAX_AGE:
                # A line failure is evidence only while it is current. Replaying a days-old
                # request after a service restart would move a healthy live tunnel based on an
                # obsolete event (and on today's latency ranking). Persist the rejection so the
                # retained diagnostic document stays harmless across every later restart.
                self.handled_reselect[country] = requested_at
                self.reselect_retries.pop(country, None)
                handled_changed = True
                continue
            # Both entry points rank synchronously and every unreachable member costs seconds,
            # so a failed ranking must never be retried on the very next reconcile cycle. The
            # zero key stands for initial selection: it has no request to abandon, so it keeps
            # retrying until the pool answers — just at the same slow cadence.
            attempt_key = requested_at if pending else 0.0
            retry = self.reselect_retries.get(country) or {}
            if not retry or float(retry.get("requested_at") or 0) != attempt_key:
                retry = {"requested_at": attempt_key, "attempts": 0, "next_at": 0.0}
                self.reselect_retries[country] = retry
            if time.monotonic() < float(retry.get("next_at") or 0):
                continue
            chosen = self.rank_and_select(country, state, prefer=prefer,
                                          # The first attempt establishes the failed node's full
                                          # cooldown. Retrying must not extend that cooldown.
                                          avoid=(str(request.get("node") or "")
                                                 if pending and not retry.get("attempts") else ""))
            if chosen:
                self.reselect_retries.pop(country, None)
                if pending:
                    # A failed measurement/selection remains pending until its short TTL expires;
                    # only a successfully applied selector change consumes the request.
                    self.handled_reselect[country] = requested_at
                    handled_changed = True
                self.exit_unranked.discard(country)
                members = state.get("_member_names") or {}
                state["node_tag"] = chosen
                state["node"] = members.get(chosen, chosen)
                self.record_exit_node(country, state,
                                      reason=str(request.get("reason") or "") if pending
                                      else "initial-selection")
            else:
                retry["attempts"] = int(retry.get("attempts") or 0) + 1
                if pending and retry["attempts"] >= max(1, EXIT_RESELECT_MAX_ATTEMPTS):
                    print(f"exit reselect: abandoning {country.upper()} request after "
                          f"{retry['attempts']} failed rankings", flush=True)
                    self.handled_reselect[country] = requested_at
                    self.reselect_retries.pop(country, None)
                    handled_changed = True
                else:
                    retry["next_at"] = time.monotonic() + EXIT_RESELECT_RETRY_SECONDS
        if handled_changed:
            atomic_json(self.reselect_handled_path,
                        {"version": 1, "countries": self.handled_reselect})

    def record_exit_node(self, country: str, state: dict, reason: str = "observed"):
        """Log every exit-node change so a line's failure window can be correlated with it.

        Changing the exit changes the outer source address, which invalidates the ePDG's IKE SA
        and forces the line to rebuild its tunnel. Without this history a reconnect storm and a
        node change are indistinguishable after the fact.
        """
        node = str(state.get("node") or "")
        previous = self.last_exit_node.get(country)
        if not node or node == previous:
            return
        self.last_exit_node[country] = node
        if previous is None:
            # First observation after an orchestrator start is not a switch.
            return
        record = {"ts": int(time.time()), "country": country, "from": previous, "to": node,
                  "selection": state.get("selection") or "managed", "reason": reason}
        # One extra request, only on an actual change: the delay that made urltest switch is
        # the whole reason the record is useful.
        try:
            member = urllib.parse.quote(str(state.get("node_tag") or ""), safe="")
            with urllib.request.urlopen(f"http://{CLASH_API}/proxies/{member}", timeout=1.5) as response:
                history = (json.load(response) or {}).get("history") or []
            if history:
                record["delay_ms"] = history[-1].get("delay")
        except Exception:
            pass
        self.exit_last_change[country] = record
        append_jsonl(self.exit_node_history, record)

    def apply_singbox(self, config: dict):
        fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        if fingerprint == self.last_proxy_fingerprint and self.singbox and self.singbox.poll() is None:
            return
        # Restarting resets every selector to its configured default. Where that default is
        # the node already carrying this country's tunnels the restart is a no-op for the
        # exit, so the memory of it is kept and nothing is ranked: re-ranking there would
        # move a working line on latency grounds, which is the one input this design refuses
        # to act on. Only a country whose node did not survive the rewrite starts over.
        for tag in [str(x.get("tag") or "") for x in config.get("outbounds") or []
                    if x.get("type") == "selector"]:
            if tag.startswith("exit-"):
                country = tag[len("exit-"):]
                if self.exit_resume.get(country):
                    continue
                self.exit_unranked.add(country)
                self.last_exit_node.pop(country, None)
        self.singbox_started_at = time.time()
        if self.dry_run:
            atomic_json(self.generated, config)
            self.last_proxy_fingerprint = fingerprint
            return
        binary = shutil.which(os.environ.get("MDD_SINGBOX_BIN", "sing-box"))
        if not binary:
            raise RuntimeError("sing-box executable not found")
        candidate = self.generated.with_name("sing-box.candidate.json")
        atomic_json(candidate, config)
        check = run([binary, "check", "-c", str(candidate)])
        if check.returncode:
            raise RuntimeError("sing-box config invalid: " + (check.stderr or check.stdout).strip())
        old = self.singbox
        old_config = self.generated.read_bytes() if self.generated.exists() else None
        if old and old.poll() is None:
            old.terminate()
            try: old.wait(5)
            except subprocess.TimeoutExpired: old.kill(); old.wait()
        os.replace(candidate, self.generated)
        self.singbox = subprocess.Popen([binary, "run", "-c", str(self.generated)])
        time.sleep(0.8)
        if self.singbox.poll() is not None:
            # Restore and restart the last checked/running config. Routes are kept only after
            # apply_singbox succeeds, so a broken update cannot silently fall through direct.
            if old_config is not None:
                self.generated.write_bytes(old_config)
                self.singbox = subprocess.Popen([binary, "run", "-c", str(self.generated)])
            else:
                self.singbox = None
            raise RuntimeError("sing-box exited during startup")
        self.last_proxy_fingerprint = fingerprint

    def apply_xray(self, config: dict | None):
        if not config:
            if self.xray and self.xray.poll() is None:
                self.xray.terminate()
                try: self.xray.wait(5)
                except subprocess.TimeoutExpired: self.xray.kill(); self.xray.wait()
            self.xray = None
            self.last_xray_fingerprint = ""
            return
        fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        if fingerprint == self.last_xray_fingerprint and self.xray and self.xray.poll() is None:
            return
        if self.dry_run:
            atomic_json(self.xray_generated, config)
            self.last_xray_fingerprint = fingerprint
            return
        binary = shutil.which(os.environ.get("MDD_XRAY_BIN", "xray"))
        if not binary:
            raise RuntimeError("Xray-core executable not found; it is required by XHTTP nodes")
        candidate = self.xray_generated.with_name("xray.candidate.json")
        atomic_json(candidate, config)
        check = run([binary, "run", "-test", "-config", str(candidate)])
        if check.returncode:
            raise RuntimeError("Xray config invalid: " + (check.stderr or check.stdout).strip())
        old, old_config = self.xray, self.xray_generated.read_bytes() if self.xray_generated.exists() else None
        if old and old.poll() is None:
            old.terminate()
            try: old.wait(5)
            except subprocess.TimeoutExpired: old.kill(); old.wait()
        os.replace(candidate, self.xray_generated)
        self.xray = subprocess.Popen([binary, "run", "-config", str(self.xray_generated)])
        time.sleep(0.6)
        if self.xray.poll() is not None:
            if old_config is not None:
                self.xray_generated.write_bytes(old_config)
                self.xray = subprocess.Popen([binary, "run", "-config", str(self.xray_generated)])
            else:
                self.xray = None
            raise RuntimeError("Xray exited during startup")
        self.last_xray_fingerprint = fingerprint

    @staticmethod
    def resolve(host: str) -> list[str]:
        result = set()
        for family, _, _, _, address in socket.getaddrinfo(host, 0, type=socket.SOCK_DGRAM):
            ip = address[0]
            if family == socket.AF_INET:
                result.add(ip)
        return sorted(result)

    def retained_epdg_addresses(self, key: str, resolved: list[str]) -> list[str]:
        """Every ePDG address that must stay routed, not just the one DNS just returned.

        A carrier rotates this record every few seconds while an IKE SA lives for hours, so the
        set of addresses in use is always wider than the current answer. Entries age out on a
        timer and are capped, which keeps the route table bounded without ever revoking the
        address an established tunnel is still using.
        """
        now = time.time()
        seen = self.epdg_seen.setdefault(key, {})
        for address in resolved:
            seen[address] = now + EPDG_ADDRESS_TTL
        for address, expiry in list(seen.items()):
            if expiry <= now:
                del seen[address]
        if len(seen) > EPDG_ADDRESS_MAX:
            for address, _ in sorted(seen.items(), key=lambda item: item[1])[:len(seen) - EPDG_ADDRESS_MAX]:
                del seen[address]
        return sorted(seen)

    def current_managed_routes(self) -> set[tuple[str, str]]:
        result = run(["ip", "-4", "route", "show", "proto", MANAGED_ROUTE_PROTO])
        found = set()
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                parts = line.split()
                if parts and "dev" in parts:
                    found.add((parts[0].split("/")[0], parts[parts.index("dev") + 1]))
        return found

    def apply_routes(self, wanted: set[tuple[str, str]]):
        if self.dry_run:
            return
        current = self.current_managed_routes()
        for ip, iface in current - wanted:
            run(["ip", "-4", "route", "del", f"{ip}/32", "dev", iface, "proto", MANAGED_ROUTE_PROTO])
        for ip, iface in wanted:
            run(["ip", "-4", "route", "replace", f"{ip}/32", "dev", iface, "proto", MANAGED_ROUTE_PROTO])

    def reconcile_proxy(self, desired: dict):
        proxy = desired.get("proxy") or {}
        lines_status, wanted, owners = {}, set(), {}
        exits_state = {}
        try:
            if not proxy.get("enabled"):
                self.apply_routes(set())
                if self.singbox and self.singbox.poll() is None: self.singbox.terminate()
                self.singbox = None; self.last_proxy_fingerprint = ""
                self.apply_xray(None)
                for line in desired.get("lines") or []:
                    lines_status[str(line.get("id"))] = {"ready": True, "mode": "direct"}
                atomic_json(self.status_path, {"updated_at": int(time.time()), "enabled": False,
                                               "exits": {}, "lines": lines_status})
                return
            config, exits_state = self.build_proxy_config(proxy)
            configured = [x for x in exits_state.values() if x.get("mode") != "direct" and x.get("ready")]
            if configured:
                self.apply_xray(self.next_xray_config)
                self.apply_singbox(config)
            else:
                self.apply_xray(None)
            # Ranking must come first: update_selected_nodes then reports the node this cycle
            # actually settled on, so proxy-status.json never shows the pre-selection default.
            self.process_reselect_requests(exits_state)
            self.update_selected_nodes(exits_state)
            for line in desired.get("lines") or []:
                iid, country, host = str(line.get("id")), str(line.get("country") or "").lower(), str(line.get("epdg") or "")
                exit_state = exits_state.get(country) or {"ready": False, "terminal": True,
                                                           "error": f"no enabled {country.upper()} exit"}
                state = dict(exit_state)
                if not line.get("enabled", True):
                    state = {"ready": True, "mode": "disabled"}
                elif exit_state.get("ready") and exit_state.get("mode") != "direct":
                    try:
                        resolved = self.resolve(host)
                        if not resolved: raise RuntimeError("ePDG DNS returned no IPv4 address")
                        addresses = self.retained_epdg_addresses(f"{country}:{host}", resolved)
                        iface = exit_state["interface"]
                        for ip in addresses:
                            if ip in owners and owners[ip] != iface:
                                if ip not in resolved:
                                    # A retained address the carrier has since handed to another
                                    # country. The fresh answer wins; stop routing the old one.
                                    self.epdg_seen.get(f"{country}:{host}", {}).pop(ip, None)
                                    continue
                                raise RuntimeError(f"ePDG {ip} is already assigned to another country exit")
                            owners[ip] = iface; wanted.add((ip, iface))
                        state["epdg"] = host
                        # What DNS says right now vs everything kept routed for live tunnels.
                        state["addresses"] = resolved
                        state["routed_addresses"] = addresses
                    except Exception as exc:
                        state = {**state, "ready": False, "error": str(exc)}
                lines_status[iid] = state
            self.apply_routes(wanted)
        except Exception as exc:
            for line in desired.get("lines") or []:
                lines_status[str(line.get("id"))] = {"ready": False, "error": str(exc)}
        atomic_json(self.status_path, {"updated_at": int(time.time()), "enabled": True,
                                       "exits": exits_state, "lines": lines_status})

    def usb_modems(self, hardware: dict) -> list[dict]:
        profiles = {(str(p.get("vid", "")).lower(), str(p.get("pid", "")).lower()): p
                    for p in hardware.get("modem_profiles") or []}
        result = []
        for node in Path("/sys/bus/usb/devices").glob("*"):
            try:
                key = (node.joinpath("idVendor").read_text().strip().lower(),
                       node.joinpath("idProduct").read_text().strip().lower())
            except OSError:
                continue
            profile = profiles.get(key)
            if not profile: continue
            interface = int(profile.get("at_interface", 2))
            usb_root = Path("/sys/bus/usb/devices")
            ports = sorted(usb_root.glob(f"{node.name}:1.{interface}/ttyUSB*"))
            ports += sorted(usb_root.glob(f"{node.name}:1.{interface}/ttyACM*"))
            if not ports: continue
            tty = Path("/dev") / ports[0].name
            serial = ""
            try: serial = node.joinpath("serial").read_text().strip()
            except OSError: pass
            hwid = slug(f"{key[0]}-{key[1]}-{serial or node.name}")
            result.append({"id": hwid, "name": profile.get("name") or "USB modem", "tty": str(tty),
                           "usb_path": node.name, "vid": key[0], "pid": key[1]})
        return sorted(result, key=lambda x: x["id"])

    def migrate_device_ids(self, discovered: list[dict]):
        """Fold a stale device id into its re-enumerated successor.

        A modem that exposes no USB serial falls back to its USB path for identity
        (``vid-pid-1-1.2``), so replugging it into another port mints a fresh id while the
        old one lingers forever as an absent ghost device in the UI. When exactly one new
        id appears and exactly one configured id of the same vid-pid family is absent,
        treat them as the same hardware: move the desired capabilities and the VPCD port
        assignment, and drop the ghost from the status document. A matching USB model is
        not sufficient evidence by itself: both the old and new bridge metadata must report
        the same hardware IMEI. Until the new bridge has published its identity, migration
        waits for a later reconciliation pass. Ambiguous or mismatched devices are untouched.
        """
        current = {modem["id"] for modem in discovered}

        def imei(device_id: str) -> str:
            value = str(read_json(self.data / "modems" / f"{device_id}.json").get("imei") or "")
            digits = re.sub(r"\D", "", value)
            return digits if len(digits) == 15 else ""

        def family(device_id: str) -> str:
            parts = str(device_id).split("-")
            return "-".join(parts[:2])

        document = read_json(self.device_desired_path)
        configured = document.get("devices")
        if not isinstance(configured, dict):
            return
        for modem in discovered:
            new_id = modem["id"]
            if new_id in configured:
                continue
            fam = f"{modem['vid']}-{modem['pid']}"
            stale = [device_id for device_id in configured
                     if device_id not in current and family(device_id) == fam]
            if len(stale) != 1:
                continue
            old_id = stale[0]
            old_imei, new_imei = imei(old_id), imei(new_id)
            if not old_imei or not new_imei or old_imei != new_imei:
                continue
            configured[new_id] = configured.pop(old_id)
            document["devices"] = configured
            document["updated_at"] = int(time.time())
            atomic_json(self.device_desired_path, document)
            hardware_doc = read_json(self.hw_state_path)
            assignments = hardware_doc.get("assignments") or {}
            if old_id in assignments and new_id not in assignments:
                moved = assignments.pop(old_id)
                moved.update({key: modem[key] for key in ("id", "tty", "usb_path")
                              if key in modem})
                hardware_doc["assignments"] = assignments | {new_id: moved}
                atomic_json(self.hw_state_path, hardware_doc)
            status_doc = read_json(self.device_status_path)
            status_devices = status_doc.get("devices")
            if isinstance(status_devices, dict) and old_id in status_devices:
                status_devices.pop(old_id)
                atomic_json(self.device_status_path, status_doc)
            # The identity document is what proved these are the same hardware, and the
            # control plane builds its device list from every file in that directory. Leaving
            # the old one behind resurrects the id this migration just retired, as a nameless
            # offline device the operator cannot get rid of.
            self.retire_identity_document(old_id, new_id)
            print(f"device id migrated: {old_id} -> {new_id} "
                  f"(same {fam} model and hardware IMEI on a new USB path)", flush=True)

    def retire_identity_document(self, old_id: str, new_id: str) -> None:
        """Drop the retired identity file, keeping whichever document describes the live path."""
        old_path = self.data / "modems" / f"{old_id}.json"
        new_path = self.data / "modems" / f"{new_id}.json"
        try:
            if not new_path.exists() and old_path.exists():
                # The bridge has not published under the new id yet; carry the record over so
                # the IMEI that justified this migration is not lost.
                record = read_json(old_path)
                record["hardware_id"] = new_id
                atomic_json(new_path, record)
            old_path.unlink(missing_ok=True)
        except OSError as exc:
            print(f"could not retire identity document for {old_id}: {exc}", flush=True)

    def reader_stanza(self, modem: dict, base: int) -> str:
        candidates = list(Path("/usr/local/lib").glob("**/libifdvpcd.so"))
        candidates += list(Path("/usr/lib").glob("**/libifdvpcd.so"))
        library = str(candidates[0]) if candidates else "/usr/local/lib/libifdvpcd.so"
        return (f"FRIENDLYNAME \"VoWiFi Modem {modem['id']}\"\n"
                f"DEVICENAME /dev/null:0x{base:04X}\nLIBPATH {library}\n"
                f"CHANNELID 0x{base:04X}\n")

    def disable_distro_vpcd_reader(self, config_path: Path) -> bool:
        """Move the vsmartcard-vpcd package's own reader definition out of pcscd's way.

        That package ships /etc/reader.conf.d/vpcd: a two-slot "Virtual PCD" reader on
        vpcd's default port, present whether or not any modem is. pcscd cannot register
        it and a modem reader on the same port, and readdir order — not policy — decides
        which one binds, so on some hosts every modem reader silently failed to appear
        while two phantom devices did. Reinstalling the package restores the file, hence
        the check on every pass. It is renamed rather than deleted (pcsc-lite skips dot
        files) so an operator who wants a virtual card back can rename it again.

        Returns True when this pass changed something, so the caller restarts pcscd.
        """
        packaged = config_path.with_name(DISTRO_VPCD_READER)
        if not packaged.is_file() or packaged == config_path:
            return False
        if self.dry_run:
            return True
        disabled = config_path.with_name(DISTRO_VPCD_READER_DISABLED)
        try:
            os.replace(packaged, disabled)
        except OSError as exc:
            self.log(f"could not disable the packaged vpcd reader definition: {exc}")
            return False
        self.log(f"disabled the packaged vpcd reader definition ({packaged} -> {disabled}); "
                 "it collides with this gateway's per-modem virtual readers")
        return True

    def reconcile_hardware(self, desired: dict, desired_devices: dict,
                           through_modemmanager=False) -> dict:
        hardware = (desired.get("hardware") or {})
        if not hardware.get("auto_detect", True):
            # Nothing is managed, so any retained claim failure describes a past cycle.
            self._claim_evidence, self._degraded, self._unclaimed_since = {}, {}, {}
            self._bridge_failures, self._bridge_started = {}, {}
            return {}
        modems = self.usb_modems(hardware)
        # A replug retires both the grace clock and the degraded verdict, so re-seating a
        # modem is the operator's way to ask for ModemManager to be tried again.
        live_ttys = {modem["tty"] for modem in modems}
        live_ids = {modem["id"] for modem in modems}
        self._unclaimed_since = {tty: seen for tty, seen in self._unclaimed_since.items()
                                 if tty in live_ttys}
        self._degraded = {device_id: reason for device_id, reason in self._degraded.items()
                          if device_id in live_ids}
        self._bridge_failures = {device_id: value for device_id, value
                                 in self._bridge_failures.items() if device_id in live_ids}
        old = read_json(self.hw_state_path).get("assignments") or {}
        ports = [BASE_VPCD_PORT + i * VPCD_PORT_STRIDE for i in range(VPCD_PORT_SLOTS)]
        # A port saved by a release that started at vpcd's own default is migrated here:
        # keeping it would leave that modem fighting the packaged "Virtual PCD" reader.
        used = {int(v.get("base_port")) for k, v in old.items()
                if k in {m["id"] for m in modems} and int(v.get("base_port") or 0) in ports}
        assignments = {}
        for modem in modems:
            saved = old.get(modem["id"]) or {}
            base = int(saved.get("base_port") or 0)
            if base not in ports:
                base = next(port for port in ports if port not in used)
            used.add(base); assignments[modem["id"]] = {**modem, "base_port": base}
        # Never ask for more slots than the installed driver was compiled with: the count is a
        # build-time constant, and a slot with no socket behind it leaves a bridge thread
        # dialling a port pcscd will never listen on for the life of the process.
        slots = max(1, min(VPCD_CHANNEL_CAPACITY, self.driver_slots(),
                           int(hardware.get("vpcd_slots") or VPCD_CHANNEL_CAPACITY)))
        # Keep reader definitions stable for every detected modem. A capability toggle only
        # starts/stops that modem's bridge; it must not restart pcscd and disturb other lines.
        reader_config = "\n".join(self.reader_stanza(m, assignments[m["id"]]["base_port"])
                                  for m in modems)
        # Resolved per cycle, not cached: the path is an environment override and tests (and
        # a relocated install) expect a change to take effect without a restart.
        config_path = Path(os.environ.get(
            "MDD_VPCD_READER_CONFIG", "/etc/reader.conf.d/mdd-sim-gateway-modems"))
        self.reader_config_path = config_path
        legacy_config = config_path.with_name("vowifi-modems")
        legacy_present = legacy_config.exists() and legacy_config != config_path
        distro_disabled = self.disable_distro_vpcd_reader(config_path)
        if reader_config != self.last_reader_config or distro_disabled:
            if not self.dry_run:
                config_path.parent.mkdir(parents=True, exist_ok=True)
                config_path.write_text(reader_config, encoding="utf-8")
                if legacy_present:
                    legacy_config.unlink(missing_ok=True)
                (self.root / "pcsc-maintenance").write_text(str(int(time.time())), encoding="ascii")
                run(["systemctl", "restart", "pcscd.service"])
            self.last_reader_config = reader_config
        elif legacy_present and not self.dry_run:
            # Releases before the orchestrator used a different filename. Remove it
            # even when the new file already matches, otherwise ghost VPCD readers
            # remain after the physical modem is unplugged.
            legacy_config.unlink(missing_ok=True)
            (self.root / "pcsc-maintenance").write_text(str(int(time.time())), encoding="ascii")
            run(["systemctl", "restart", "pcscd.service"])
        # Card access is not a capability. The virtual reader has to hold a card before a
        # line can exist at all — detecting the SIM, verifying its PIN and managing eSIM
        # profiles all go through it — while the VoWiFi switch stays disabled until such a
        # line exists. Tying the bridge to that switch therefore deadlocked every fresh
        # modem, and turning VoWiFi off to run an eSIM operation emptied the reader under
        # it. Bridges follow the hardware instead: every present modem gets one.
        live_ids = {m["id"] for m in modems}
        for hwid in list(self.bridges):
            proc = self.bridges[hwid]
            exited = proc.poll() is not None
            # A bridge that has run stably has lived its failure history down.
            if not exited and self._bridge_failures.get(hwid) and \
                    time.time() - self._bridge_started.get(hwid, 0.0) >= BRIDGE_STABLE_SECONDS:
                self._bridge_failures.pop(hwid, None)
            # A bridge that was started on a now-migrated port would keep dialling a socket
            # pcscd no longer listens on, so a port change has to respawn it.
            moved = (hwid in live_ids
                     and self.bridge_ports.get(hwid) != assignments[hwid]["base_port"])
            if hwid not in live_ids or moved or exited:
                self.bridges.pop(hwid)
                self.bridge_ports.pop(hwid, None)
                started = self._bridge_started.pop(hwid, 0.0)
                if exited and hwid in live_ids and not moved:
                    self._record_bridge_exit(hwid, proc, started)
                if proc.poll() is None: proc.terminate()
        unclaimed = []
        for modem in modems:
            if modem["id"] in self.bridges: continue
            if not self._bridge_retry_due(modem["id"]):
                continue
            bridge = self.repo / "host" / "vpcd_modem_bridge.py"
            metadata = self.data / "modems" / f"{modem['id']}.json"
            command = [sys.executable, str(bridge), "--modem", modem["tty"], "--slots", str(slots),
                       "--base-port", str(assignments[modem["id"]]["base_port"]),
                       "--metadata-file", str(metadata), "--hardware-id", modem["id"]]
            if through_modemmanager:
                mm_modem = self.modemmanager_modem_for_tty(modem["tty"])
                refused = self.mm_refusal_logged(modem)
                if mm_modem:
                    self._unclaimed_since.pop(modem["tty"], None)
                    self._degraded.pop(modem["id"], None)
                    command += ["--modemmanager", mm_modem, "--identity-refresh", "60"]
                elif not refused and self.claim_wait(modem["tty"]) < MM_CLAIM_GRACE_SECONDS:
                    self.log(f"waiting for ModemManager to claim {modem['tty']}")
                    unclaimed.append(modem["tty"])
                    continue
                else:
                    unclaimed.append(modem["tty"])
                    if modem["id"] not in self._degraded:
                        why = ("ModemManager logged that it cannot create a modem for it"
                               if refused else
                               f"ModemManager has not claimed it in {int(MM_CLAIM_GRACE_SECONDS)}s")
                        self.log(f"bridging {modem['tty']} over the serial port directly "
                                 f"({why}). VoWiFi works; cellular data and flight mode do "
                                 f"not, because they need a ModemManager modem.")
                    self._degraded[modem["id"]] = (
                        "ModemManager did not create a modem for this hardware, so VoWiFi is "
                        "bridged over the serial port directly. Cellular data and flight mode "
                        "are unavailable until ModemManager claims it.")
            if not self.dry_run:
                # stderr goes to a file, not the journal: it is what names the exception
                # when a bridge dies, and a pipe nobody drains would block a healthy one.
                with open(self._bridge_stderr_path(modem["id"]), "wb") as sink:
                    self.bridges[modem["id"]] = subprocess.Popen(command, stderr=sink)
                self._bridge_started[modem["id"]] = time.time()
                self.bridge_ports[modem["id"]] = assignments[modem["id"]]["base_port"]
        # One capture per cycle rather than one per modem: this is the state an operator has to
        # be asked for by hand today, and asking costs a support round trip. Once a bridge has
        # degraded it holds the port exclusively and nothing further can change, so the capture
        # taken at that moment is retained rather than re-derived on every idle cycle.
        if unclaimed:
            self._claim_evidence = self.claim_evidence(unclaimed)
        elif not self._degraded:
            self._claim_evidence = {}
        atomic_json(self.hw_state_path, {"updated_at": int(time.time()), "assignments": assignments})
        return assignments

    def loop(self):
        self.root.mkdir(parents=True, exist_ok=True)
        while not self.stop:
            self.process_update_request()
            self.retire_obsolete_services()
            self.reconcile_timezone()
            desired = read_json(self.desired_path)
            desired["hardware"] = desired.get("hardware") or read_json(
                self.data / "config.json").get("hardware", {})
            # hardware lives beside proxy in settings; desired v1 publishers may omit it.
            if not desired.get("hardware"):
                try:
                    conf = yaml.safe_load((self.data / "config.yaml").read_text()) or {}
                    desired["hardware"] = (conf.get("settings") or {}).get("hardware") or {}
                except Exception:
                    pass
            discovered = self.usb_modems(desired.get("hardware") or {})
            self.migrate_device_ids(discovered)
            desired_devices, _migrated = self.desired_devices(discovered)
            present_ids = {modem["id"] for modem in discovered}
            active_desired = {device_id: state for device_id, state in desired_devices.items()
                              if device_id in present_ids}
            plan = self.capability_plan(active_desired)
            cellular_required = plan["cellular_backend_required"]
            vowifi_required = self.country_egress_required(desired, plan)
            mm_active = self.service_active("ModemManager.service")
            previous = mm_active
            if self.applied_cellular_backend is None:
                self.applied_cellular_backend = mm_active
            # Reconcile partial service failures as well as user-requested changes, including
            # after a reboot where no in-memory applied state exists.
            switching = mm_active != cellular_required
            assignments = read_json(self.hw_state_path).get("assignments") or {}
            affected = list(active_desired) if switching else []
            disruption = ("shared cellular backend transition recreates every active VoWiFi bridge"
                          if switching else "")
            if switching:
                self.publish_device_status(desired_devices, assignments, transitioning=True,
                                           disruption=disruption, affected_devices=affected)
                try:
                    self.apply_cellular_backend(cellular_required)
                except Exception as exc:
                    error = str(exc)
                    try:
                        self.apply_cellular_backend(previous)
                    except Exception as rollback_exc:
                        error += f"; rollback failed: {rollback_exc}"
                    self.publish_device_status(desired_devices, assignments, error=error,
                                               disruption=disruption,
                                               affected_devices=affected)
                    time.sleep(self.interval)
                    continue

            self.apply_device_radios(discovered, active_desired,
                                     through_modemmanager=cellular_required)
            # Country egress only exists to carry VoWiFi IKE/ePDG traffic.
            proxy_desired = desired
            if not vowifi_required:
                proxy_desired = dict(desired)
                proxy_desired["proxy"] = {"enabled": False}
            self.reconcile_proxy(proxy_desired)
            # If any modem needs cellular, ModemManager owns every modem tty. Consequently every
            # enabled VoWiFi bridge (including VoWiFi-only devices) must use its serialized AT
            # command path. Devices with VoWiFi disabled retain an empty PC/SC reader definition
            # for stable enumeration, but never receive a bridge process or usable SIM channel.
            assignments = self.reconcile_hardware(
                desired, active_desired, through_modemmanager=cellular_required)
            self.publish_device_status(desired_devices, assignments)
            self.publish_host_diagnostics(discovered, assignments, mm_active,
                                          cellular_required, vowifi_required)
            # Compare what this cycle concluded, not what it observed: timestamps and counters
            # differ every time and would defeat the comparison.
            fingerprint = json.dumps([sorted(present_ids), active_desired, cellular_required,
                                      vowifi_required, mm_active, assignments], sort_keys=True)
            idle = fingerprint == self._last_conclusion
            self._last_conclusion = fingerprint
            self._sleep_for_work(IDLE_INTERVAL_SECONDS if idle else self.interval)

    def _input_mtimes(self) -> tuple:
        """Cheap change detector for the documents an operator action writes."""
        stamps = []
        for path in (self.desired_path, self.device_desired_path,
                     self.data / "config.yaml", self.reselect_path):
            try:
                stamps.append(path.stat().st_mtime)
            except OSError:
                stamps.append(0.0)
        return tuple(stamps) + self._usb_fingerprint()

    @staticmethod
    def _usb_fingerprint() -> tuple:
        """Cheap change detector for the USB tree.

        Plugging a modem in is not a document change, and waiting out the backoff to notice it
        would make the hardware feel unresponsive. The directory mtime moves when a device
        appears or leaves; the entry count catches a same-second replug.
        """
        try:
            usb = Path("/sys/bus/usb/devices")
            return (usb.stat().st_mtime, len(os.listdir(usb)))
        except OSError:
            return (0.0, 0)

    def _sleep_for_work(self, seconds: float) -> None:
        """Wait, but wake immediately when an input document changes.

        Backing off must not make the gateway feel unresponsive: a settings save or a line
        start writes one of these files, and noticing that costs a stat rather than the
        fifteen subprocesses a full reconcile spends.
        """
        watched = self._input_mtimes()
        deadline = time.time() + seconds
        while not self.stop:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            time.sleep(min(self.interval, max(0.1, remaining)))
            if self._input_mtimes() != watched:
                return

    def request_stop(self):
        """Publish the PC/SC maintenance window before bridge teardown can begin.

        The signal handler must do this immediately.  Waiting for ``loop()`` to leave its
        reconciliation/sleep cycle creates a race where the control plane sees all virtual
        readers disappear first and removes an otherwise healthy VoWiFi engine container.
        """
        self.stop = True
        if self.bridges:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / "pcsc-maintenance").write_text(str(int(time.time())), encoding="ascii")

    def close(self):
        # A service restart tears down/recreates bridge children. Tell the card monitor this is
        # planned enumeration churn so it does not interpret the brief disappearance as a user
        # unplug and stop otherwise healthy line containers. request_stop() is idempotent and is
        # repeated here for non-signal exits.
        self.request_stop()
        if self.singbox and self.singbox.poll() is None: self.singbox.terminate()
        if self.xray and self.xray.poll() is None: self.xray.terminate()
        self.stop_bridges()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--interval", type=float, default=3)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    app = Orchestrator(args.data.resolve(), args.repo.resolve(), args.interval, args.dry_run)
    signal.signal(signal.SIGTERM, lambda *_: app.request_stop())
    signal.signal(signal.SIGINT, lambda *_: app.request_stop())
    try: app.loop()
    finally: app.close()


if __name__ == "__main__":
    main()
