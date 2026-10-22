"""Country-aware ePDG egress coordination.

The control plane never edits host routes from its container namespace.  Instead it publishes a
small desired-state document under the shared data directory.  The host-side
``mdd-sim-gateway-orchestrator`` resolves each line's ePDG and owns the per-country sing-box TUN + /32
routes.  Engine startup waits for the corresponding line to become ready, preventing an IKE
attempt from leaking through the wrong country's default route.

The opt-in container transport instead consumes a separate, versioned SOCKS status.
That status confirms listener readiness only; it never claims a host route exists.
"""
from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from copy import deepcopy
from urllib.parse import unquote, urlsplit

from . import config as cfg
from .egress_contract import current_status, socks_endpoint

_HERE = os.path.dirname(__file__)
_MCC_PATH = os.path.join(_HERE, "mcc_country.json")
_ORCH_DIR = os.path.join(cfg.DATA_DIR, "orchestrator")
_DESIRED = os.path.join(_ORCH_DIR, "desired.json")
_STATUS = os.path.join(_ORCH_DIR, "proxy-status.json")
_RESELECT = os.path.join(_ORCH_DIR, "exit-reselect.json")
_STALLED = os.path.join(_ORCH_DIR, "exit-stalled.json")
# A line healthy at least this long has proved its exit node can carry IMS.
RESELECT_MIN_STABLE_SECONDS = float(os.environ.get("MDD_EXIT_RESELECT_MIN_STABLE", "600"))


class EgressError(RuntimeError):
    pass


def udp_probe_targets() -> list[str]:
    """Public resolvers the UDP probe may use, in order.

    One hard-coded resolver made the probe a test of that address rather than of UDP: an
    exit whose provider blackholes 1.1.1.1, or whose panel routes port 53 somewhere of its
    own, failed a check it should have passed — and the exit it condemned carries IKE on
    UDP 500/4500, which has nothing to do with DNS. Any one answer proves the path.
    """
    raw = os.environ.get("MDD_UDP_PROBE_TARGETS", "1.1.1.1,8.8.8.8,9.9.9.9")
    targets = []
    for value in raw.split(","):
        value = value.strip()
        try:
            socket.inet_aton(value)
        except OSError:
            continue
        if value not in targets:
            targets.append(value)
    return targets or ["1.1.1.1"]


def stun_probe_targets() -> list[tuple[str, int]]:
    """STUN servers the UDP probe may use, in order.

    STUN is what the DNS probe should have been: a bare UDP round trip, on ports no
    resolver policy rewrites, answered by servers that exist to confirm exactly this.
    """
    raw = os.environ.get("MDD_UDP_STUN_TARGETS",
                         "stun.cloudflare.com:3478,stun.l.google.com:19302")
    targets = []
    for value in raw.split(","):
        value = value.strip()
        if not value:
            continue
        name, _, port = value.rpartition(":")
        if not name or not port.isdigit() or not 0 < int(port) <= 65535:
            continue
        if (name, int(port)) not in targets:
            targets.append((name, int(port)))
    return targets


def udp_probe_plan() -> list[tuple]:
    """DNS and STUN probes interleaved, so neither family decides the verdict alone.

    Interleaved rather than appended: a hard-coded resolver being unreachable must not push
    the STUN probes past the time budget, which is how one blocked address used to condemn
    an exit that carries IKE.
    """
    dns = [("dns", target, 53) for target in udp_probe_targets()]
    stun = [("stun", name, port) for name, port in stun_probe_targets()]
    plan = []
    for index in range(max(len(dns), len(stun))):
        if index < len(dns):
            plan.append(dns[index])
        if index < len(stun):
            plan.append(stun[index])
    return plan or [("dns", "1.1.1.1", 53)]


def _recv_exact(stream: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining:
        chunk = stream.recv(remaining)
        if not chunk:
            raise EgressError("SOCKS5 proxy closed the UDP negotiation")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _dns_name(value: str) -> bytes:
    labels = value.rstrip(".").encode("idna").split(b".")
    if not labels or any(not label or len(label) > 63 for label in labels):
        raise EgressError("invalid ePDG DNS name")
    return b"".join(bytes([len(label)]) + label for label in labels) + b"\x00"


def _dns_ipv4_answer(message: bytes, transaction: bytes) -> str:
    if len(message) < 12 or message[:2] != transaction or not (message[2] & 0x80) \
            or message[3] & 0x0f:
        raise EgressError("ePDG DNS response was invalid")
    questions, answers = struct.unpack("!HH", message[4:8])

    def skip_name(offset):
        while offset < len(message):
            length = message[offset]
            if length & 0xc0 == 0xc0:
                return offset + 2
            offset += 1
            if length == 0:
                return offset
            offset += length
        raise EgressError("ePDG DNS response was truncated")

    offset = 12
    for _ in range(questions):
        offset = skip_name(offset) + 4
    for _ in range(answers):
        offset = skip_name(offset)
        if offset + 10 > len(message):
            break
        record_type, record_class, _ttl, length = struct.unpack(
            "!HHIH", message[offset:offset + 10])
        offset += 10
        value = message[offset:offset + length]
        if record_type == 1 and record_class == 1 and length == 4:
            return socket.inet_ntoa(value)
        offset += length
    raise EgressError("ePDG DNS response had no IPv4 address")


def _udp_probe_once(host: str, port: int, probe: tuple, timeout: float,
                    username: str = "", password: str = "") -> int | str:
    """One complete SOCKS5 UDP ASSOCIATE carrying `probe`, returning the round trip in ms.

    `probe` is (kind, target_host, target_port): "dns" sends an A query, "stun" a Binding
    Request. The target may be a name — the relay resolves it, which is also what a real
    exit has to do.
    """
    kind, target_host, target_port, *parameters = probe
    if kind == "stun":
        transaction = os.urandom(12)
        payload = struct.pack("!HH", 0x0001, 0) + b"\x21\x12\xa4\x42" + transaction
    else:
        transaction = os.urandom(2)
        query_name = parameters[0] if parameters else "cloudflare.com"
        payload = transaction + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" \
            + _dns_name(query_name) + b"\x00\x01\x00\x01"

    started = time.monotonic()
    with socket.create_connection((host, int(port)), timeout=timeout) as stream:
        stream.settimeout(timeout)
        methods = b"\x00\x02" if username or password else b"\x00"
        stream.sendall(b"\x05" + bytes([len(methods)]) + methods)
        method = _recv_exact(stream, 2)
        if method == b"\x05\x02":
            user, secret = username.encode(), password.encode()
            if not user or len(user) > 255 or len(secret) > 255:
                raise EgressError("SOCKS5 username or password is invalid")
            stream.sendall(b"\x01" + bytes([len(user)]) + user
                           + bytes([len(secret)]) + secret)
            if _recv_exact(stream, 2) != b"\x01\x00":
                raise EgressError("SOCKS5 username or password was rejected")
        elif method != b"\x05\x00":
            raise EgressError("SOCKS5 proxy rejected UDP test negotiation")
        stream.sendall(b"\x05\x03\x00\x01\x00\x00\x00\x00\x00\x00")
        head = _recv_exact(stream, 4)
        if head[:2] != b"\x05\x00":
            raise EgressError(f"SOCKS5 proxy rejected UDP associate (code {head[1]})")
        atyp = head[3]
        if atyp == 1:
            relay_host = socket.inet_ntoa(_recv_exact(stream, 4))
        elif atyp == 3:
            relay_host = _recv_exact(stream, _recv_exact(stream, 1)[0]).decode("ascii")
        elif atyp == 4:
            relay_host = socket.inet_ntop(socket.AF_INET6, _recv_exact(stream, 16))
        else:
            raise EgressError("SOCKS5 proxy returned an invalid UDP relay address")
        relay_port = struct.unpack("!H", _recv_exact(stream, 2))[0]
        if relay_host in {"0.0.0.0", "::"}:
            relay_host = host

        try:
            destination = b"\x01" + socket.inet_aton(target_host)
        except OSError:
            name = target_host.encode("idna")
            destination = b"\x03" + bytes([len(name)]) + name
        packet = b"\x00\x00\x00" + destination + struct.pack("!H", target_port) + payload
        family = socket.AF_INET6 if ":" in relay_host else socket.AF_INET
        with socket.socket(family, socket.SOCK_DGRAM) as udp:
            udp.settimeout(timeout)
            udp.sendto(packet, (relay_host, relay_port))
            response, _ = udp.recvfrom(4096)
        if len(response) < 10 or response[0:3] != b"\x00\x00\x00":
            raise EgressError("SOCKS5 proxy returned an invalid UDP response")
        # Skip the variable SOCKS destination header before reading the answer itself.
        response_atyp = response[3]
        offset = 4 + (4 if response_atyp == 1 else 16 if response_atyp == 4
                      else 1 + response[4] if response_atyp == 3 else -100) + 2
        if offset < 6:
            raise EgressError("SOCKS5 proxy returned an invalid UDP response")
        answer = response[offset:]
        if kind == "stun":
            # Binding Success, same magic cookie and transaction id.
            if len(answer) < 20 or answer[0:2] != b"\x01\x01" \
                    or answer[4:8] != b"\x21\x12\xa4\x42" or answer[8:20] != transaction:
                raise EgressError("STUN response did not match the test request")
        elif len(answer) < 4 or answer[0:2] != transaction or not (answer[2] & 0x80):
            raise EgressError("UDP DNS response did not match the test request")
        if kind == "resolve":
            return _dns_ipv4_answer(answer, transaction)
    return max(1, round((time.monotonic() - started) * 1000))


def resolve_ipv4_via_socks(proxy_url: str, name: str, timeout: float = 8.0) -> str:
    """Resolve an ePDG through a public resolver reached from the selected country exit."""
    parsed = urlsplit(proxy_url)
    if parsed.scheme != "socks5" or not parsed.hostname or parsed.port is None:
        raise EgressError("invalid internal SOCKS endpoint")
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    failures = []
    for resolver in udp_probe_targets():
        try:
            return str(_udp_probe_once(parsed.hostname, parsed.port,
                                      ("resolve", resolver, 53, name), timeout,
                                      username, password))
        except (EgressError, OSError, ValueError, struct.error) as exc:
            failures.append(str(exc))
    raise EgressError("ePDG DNS resolution failed through country exit: " + "; ".join(failures))


def test_udp_proxy(host: str, port: int, timeout: float = 8.0,
                   username: str = "", password: str = "") -> int:
    """Prove a SOCKS5 listener carries UDP, and return the round trip in ms.

    Country exits expose a loopback/bridge-only SOCKS5 listener. Testing that listener checks
    the complete configured outbound, including the UDP path VoWiFi IKE actually requires.

    DNS alone was the wrong question to ask. VoWiFi carries IKE on UDP 500/4500 and never
    queries these resolvers, while port 53 is among the most intercepted, redirected and
    rewritten ports there is — a panel with a DNS rule of its own, or a provider hijacking
    53, failed exits that carry IMS perfectly well. STUN probes are therefore interleaved
    with the DNS ones: they answer the actual question (does a UDP round trip survive this
    exit) on a port nobody rewrites. Any single answer passes.

    Each probe gets its own ASSOCIATE: a relay binds to the source port of the first
    datagram it sees, so a second target sent through the same session is discarded rather
    than answered.
    """
    plan = udp_probe_plan()
    per_probe = max(2.0, timeout / len(plan))
    deadline = time.monotonic() + timeout
    failures = []
    for index, probe in enumerate(plan):
        if index and time.monotonic() >= deadline:
            break
        try:
            return _udp_probe_once(host, port, probe, per_probe, username, password)
        except (EgressError, OSError, ValueError, struct.error) as exc:
            failures.append(f"{probe[0]}/{probe[1]}: {exc}")
    raise EgressError("UDP test failed — " + "; ".join(failures))


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_private_json(path: Path, value: dict):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _process_detail(process: subprocess.Popen | None, limit: int = 300) -> str:
    """Whatever the test proxy printed before giving up, trimmed to one readable line.

    Both streams are drained: sing-box reports on stderr while Xray-core writes its log to
    stdout, so reading only one of them left whichever engine actually failed unquoted.
    Call this only after the process has been stopped — these reads run to EOF.
    """
    if not process:
        return ""
    lines: list[str] = []
    for stream in (process.stdout, process.stderr):
        if not stream:
            continue
        try:
            text = stream.read() or ""
        except (OSError, ValueError):
            continue
        if isinstance(text, bytes):
            text = text.decode("utf-8", "replace")
        lines += [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-3:])[:limit]


def _wait_tcp(port: int, process: subprocess.Popen, timeout: float = 4.0, what: str = "proxy"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            # The reason the process printed is the whole diagnosis; discarding it left the
            # operator with a generic failure for a node their other clients accept.
            detail = _process_detail(process)
            raise EgressError(f"{what} exited during startup"
                              + (f": {detail}" if detail else ""))
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=.2):
                return
        except OSError:
            time.sleep(.08)
    raise EgressError(f"{what} did not become ready")


def _stop_process(process: subprocess.Popen | None):
    if not process or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _config_error(summary: str, completed) -> str:
    """Attach the checker's own complaint; it names the field an operator must fix."""
    text = ((completed.stderr or "") + "\n" + (completed.stdout or "")).strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    detail = " | ".join(lines[-2:])[:300]
    return f"{summary}: {detail}" if detail else summary


def _orchestrator_module():
    path = Path(__file__).resolve().parents[2] / "host" / "mdd_orchestrator.py"
    # The control container runs from /app/control, with host/ mounted separately.
    # The loaded module imports host.modem_probe, so make its package root importable.
    package_root = str(path.parent.parent)
    if package_root not in sys.path:
        sys.path.insert(0, package_root)
    spec = importlib.util.spec_from_file_location("mdd_proxy_test_orchestrator", path)
    if not spec or not spec.loader:
        raise EgressError("proxy protocol support is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def describe_proxy_profile(profile: dict) -> dict:
    """Summarize how the gateway understood a pasted node, with no secret in the result.

    "It works in my other client" is only answerable by showing what this gateway parsed:
    a missing obfs or an SNI read from the wrong parameter is invisible otherwise. Server
    host, ports, passwords, UUIDs and keys are deliberately absent — only whether they are
    present, and the switches that decide whether the node can carry IKE at all.
    """
    kind = str(profile.get("type") or "").lower()
    if kind == "socks5":
        return {"protocol": "socks5", "udp_capable": True}
    value = str(profile.get("value") or "").strip()
    if not value:
        return {"error": "node share link is empty"}
    try:
        helper = _orchestrator_module()
        if value.startswith("{") or value.split("://", 1)[0].lower() in ("socks", "socks5"):
            outbound = helper.parse_manual_outbound(value, "describe")
            node = {}
        else:
            node = helper.parse_share_link(value)
            # Only to read TLS/transport back out for display. A node carried by Xray is
            # not describable through the sing-box converter, and refusing to describe it
            # would hide the very summary that explains why it goes to Xray.
            outbound = helper.clash_outbound({**node, "encryption": "none"}, "describe")
    except (ValueError, EgressError) as exc:
        return {"error": str(exc)}
    tls = outbound.get("tls") or {}
    # Report the algorithm family only; the value itself is client key material.
    encryption = str((node or {}).get("encryption") or "none")
    encryption = "" if encryption.lower() in ("", "none") else encryption.split(".")[0]
    summary = {
        "protocol": str(outbound.get("type") or ""),
        "transport": str(node.get("network") or "tcp") if node else "",
        "tls": bool(tls.get("enabled")),
        "sni": str(tls.get("server_name") or ""),
        "alpn": list(tls.get("alpn") or []),
        "skip_cert_verify": bool(tls.get("insecure")),
        "reality": bool((tls.get("reality") or {}).get("enabled")),
        "utls_fingerprint": str((tls.get("utls") or {}).get("fingerprint") or ""),
        "obfs": str((outbound.get("obfs") or {}).get("type") or ""),
        "obfs_password_set": bool((outbound.get("obfs") or {}).get("password")),
        "flow": str(outbound.get("flow") or ""),
        "encryption": encryption,
        "udp_capable": helper.outbound_supports_udp(outbound),
        # Which engine actually carries this node — the answer to "my other client works".
        "engine": "xray" if node and helper.node_needs_xray(node) else "sing-box",
    }
    return {key: value for key, value in summary.items() if value not in ("", [], False)} \
        | {"udp_capable": summary["udp_capable"], "protocol": summary["protocol"],
           "engine": summary["engine"]}


def test_proxy_profile(profile: dict, timeout: float = 8.0) -> int:
    """Test a node/SOCKS5 profile without assigning it to or changing a country exit."""
    kind = str(profile.get("type") or "").lower()
    if kind == "socks5":
        host = str(profile.get("server") or "").strip()
        port = int(profile.get("port") or 1080)
        if not host or not 0 < port <= 65535:
            raise EgressError("SOCKS5 server or port is invalid")
        return test_udp_proxy(host, port, timeout, str(profile.get("username") or ""),
                              str(profile.get("password") or ""))
    if kind != "node":
        raise EgressError("only individual nodes and SOCKS5 proxies can be tested here")

    value = str(profile.get("value") or "").strip()
    if not value:
        raise EgressError("node share link is empty")
    singbox = shutil.which(os.environ.get("MDD_SINGBOX_BIN", "sing-box"))
    if not singbox:
        raise EgressError("sing-box executable not found")
    helper = _orchestrator_module()
    node = helper.parse_share_link(value) if value.lower().startswith("vless://") else None
    # REALITY and XHTTP are Xray protocols; testing them through sing-box measured sing-box's
    # compatibility with the server's Xray build rather than whether the node works.
    via_xray = bool(node and helper.node_needs_xray(node))
    local_port, bridge_port = _free_loopback_port(), _free_loopback_port()
    if via_xray:
        outbound = {"type": "socks", "tag": "test-out", "version": "5",
                    "server": "127.0.0.1", "server_port": bridge_port}
    else:
        outbound = (helper.clash_outbound(node, "test-out") if node
                    else helper.parse_manual_outbound(value, "test-out"))
    if not helper.outbound_supports_udp(outbound):
        raise EgressError("this node protocol does not support UDP")

    sing_config = {
        "log": {"level": "warn"},
        "inbounds": [{"type": "socks", "tag": "test-in", "listen": "127.0.0.1",
                      "listen_port": local_port}],
        "outbounds": [outbound],
        "route": {"rules": [{"inbound": ["test-in"], "outbound": "test-out"}],
                  "auto_detect_interface": True},
    }
    sing_process = xray_process = None
    with tempfile.TemporaryDirectory(prefix="mdd-proxy-test-") as directory:
        root = Path(directory)
        sing_path = root / "sing-box.json"
        _write_private_json(sing_path, sing_config)
        check = subprocess.run([singbox, "check", "-c", str(sing_path)], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8)
        if check.returncode:
            raise EgressError(_config_error("node configuration is invalid", check))
        try:
            if via_xray:
                xray = shutil.which(os.environ.get("MDD_XRAY_BIN", "xray"))
                if not xray:
                    raise EgressError("Xray-core executable not found for this node")
                xray_config = {
                    "log": {"loglevel": "warning"},
                    "inbounds": [{"listen": "127.0.0.1", "port": bridge_port,
                                  "protocol": "socks", "tag": "test-in",
                                  "settings": {"auth": "noauth", "udp": True,
                                               "ip": "127.0.0.1"}}],
                    "outbounds": [helper.xray_outbound(node, "test-out")],
                    "routing": {"domainStrategy": "AsIs", "rules": [{"type": "field",
                                "inboundTag": ["test-in"], "outboundTag": "test-out"}]},
                }
                xray_path = root / "xray.json"
                _write_private_json(xray_path, xray_config)
                xcheck = subprocess.run([xray, "run", "-test", "-config", str(xray_path)],
                                        text=True, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, timeout=8)
                if xcheck.returncode:
                    raise EgressError(
                        _config_error("Xray node configuration is invalid", xcheck))
                xray_process = subprocess.Popen([xray, "run", "-config", str(xray_path)],
                                                stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE, text=True)
                _wait_tcp(bridge_port, xray_process, what="Xray-core")
            sing_process = subprocess.Popen([singbox, "run", "-c", str(sing_path)],
                                            stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True)
            _wait_tcp(local_port, sing_process, what="sing-box")
            try:
                return test_udp_proxy("127.0.0.1", local_port, timeout)
            except EgressError as exc:
                # The proxy is up but the node did not carry the probe. What the engines
                # logged while trying (handshake refused, auth rejected, no activity) is the
                # difference between "your node is wrong" and "we built it wrong". The node's
                # own engine is quoted first: for a REALITY node that is Xray, and a bare
                # "timed out" with sing-box's silence behind it explains nothing.
                _stop_process(sing_process)
                _stop_process(xray_process)
                details = [text for text in (_process_detail(xray_process),
                                             _process_detail(sing_process)) if text]
                raise EgressError(
                    f"{exc}" + (f" — {' ;; '.join(details)}" if details else "")) from exc
        finally:
            _stop_process(sing_process)
            _stop_process(xray_process)


def _atomic_json(path: str, value: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _mcc_map() -> dict[str, str]:
    return _read_json(_MCC_PATH)


def normalize_country(value: str | None) -> str:
    value = str(value or "").strip().lower().replace("_", "-")
    # Accept en-GB / zh_US style locale values but store ISO-3166 alpha-2 only.
    if "-" in value:
        value = value.rsplit("-", 1)[-1]
    return value if len(value) == 2 and value.isalpha() else ""


def country_for_mcc(mcc: str | int | None) -> str:
    return normalize_country(_mcc_map().get(str(mcc or "").zfill(3)))


def line_country(inst: dict) -> str:
    """Per-line override wins; otherwise infer the SIM home country from its MCC."""
    return normalize_country(inst.get("proxy_country")) or country_for_mcc(inst.get("mcc"))


def epdg_for(inst: dict) -> str:
    if inst.get("epdg"):
        return str(inst["epdg"]).strip()
    mcc = str(inst.get("mcc") or "").zfill(3)
    mnc = str(inst.get("mnc") or "").zfill(3)
    if not mcc.strip("0") or not mnc.strip("0"):
        return ""
    return f"epdg.epc.mnc{mnc}.mcc{mcc}.pub.3gppnetwork.org"


def desired_document(instances: list[dict], settings: dict) -> dict:
    proxy = deepcopy(settings.get("proxy") or {})
    lines = []
    for inst in instances:
        lines.append({
            "id": str(inst.get("id", "")),
            "name": inst.get("name", ""),
            "enabled": bool(inst.get("enabled", True)),
            "mcc": str(inst.get("mcc", "")),
            "mnc": str(inst.get("mnc", "")),
            "country": line_country(inst),
            "epdg": epdg_for(inst),
        })
    return {"version": 1, "updated_at": int(time.time()), "proxy": proxy,
            "hardware": deepcopy(settings.get("hardware") or {}), "lines": lines}


def publish(instances: list[dict] | None = None, settings: dict | None = None) -> dict:
    instances = instances if instances is not None else cfg.list_instances()
    settings = settings if settings is not None else cfg.get_settings()
    document = desired_document(instances, settings)
    _atomic_json(_DESIRED, document)
    return document


def status() -> dict:
    if transport() == "socks5":
        return _read_json(os.path.join(_ORCH_DIR, "socks-egress-status.json"))
    return _read_json(_STATUS)


def transport() -> str:
    value = os.environ.get("MDD_EGRESS_TRANSPORT", "host").strip()
    if value not in {"host", "socks5"}:
        raise EgressError("MDD_EGRESS_TRANSPORT must be host or socks5")
    return value


def _ensure_socks_exit(country, proxy, timeout):
    deadline = time.monotonic() + max(1.0, timeout)
    reason = "container egress status is missing, stale or for a different configuration"
    while time.monotonic() < deadline:
        state = status()
        if current_status(state, proxy, time.time()):
            exits = state.get("exits")
            last = exits.get(country) if isinstance(exits, dict) else None
            if isinstance(last, dict):
                if last.get("ready") is True:
                    if last.get("transport") == "direct" and last.get("mode") == "direct":
                        # Only an explicit country setting can authorize direct access.
                        selected = proxy["exits"][country]
                        if selected.get("mode") == "direct" and not selected.get("profile_id"):
                            return {"ready": True, "mode": "direct", "transport": "direct"}
                    elif last.get("transport") == "socks5" and last.get("mode") != "direct":
                        try:
                            endpoint = socks_endpoint(last)
                        except ValueError:
                            raise EgressError("invalid container egress endpoint") from None
                        return {**last, "proxy_url": endpoint}
                    raise EgressError("container egress transport does not match configuration")
                if last.get("terminal"):
                    raise EgressError(f"{country.upper()} container exit configuration rejected")
                reason = "container country exit is not ready"
        time.sleep(0.4)
    raise EgressError(f"{country.upper()} exit unavailable: {reason}")


def exit_probe_context(inst: dict, settings: dict) -> dict | None:
    """Identify a current, automatically selected container exit we can safely probe.

    Listener readiness alone is not connectivity evidence. Stale/mismatched configuration,
    direct routes, manual pins and pools without alternatives must not start a node walk.
    """
    proxy = settings.get("proxy") or {}
    if transport() != "socks5" or not proxy.get("enabled"):
        return None
    state = status()
    if not current_status(state, proxy, time.time()):
        return None
    country = line_country(inst)
    selected = (state.get("exits") or {}).get(country) or {}
    if (not (proxy.get("exits") or {}).get(country, {}).get("enabled")
            or selected.get("ready") is not True or selected.get("mode") != "subscription"
            or selected.get("transport") != "socks5"
            or selected.get("selection") == "manual" or not selected.get("node")
            or len(selected.get("candidates") or []) < 2
            or selected["node"] not in selected["candidates"]):
        return None
    try:
        socks_endpoint(selected)
    except ValueError:
        return None
    return {"country": country, "config_fingerprint": state["config_fingerprint"],
            **{key: selected.get(key) for key in
               ("node", "node_tag", "proxy_host", "proxy_port")}}


def probe_current_exit(inst: dict, settings: dict) -> dict | None:
    """Probe only after a failed start, returning evidence tied to one selector/config.

    Probe independent public DNS/STUN targets, not the carrier's ePDG hostname: a bad
    carrier DNS name must not blame a healthy proxy. Never expose raw probe exceptions.
    """
    context = exit_probe_context(inst, settings)
    if context is None:
        return None
    try:
        test_udp_proxy(context["proxy_host"], context["proxy_port"], timeout=8.0)
        reachable = True
    except EgressError:
        reachable = False
    # Probing blocks while the selector/configuration may change. Evidence from the old
    # node must not be charged to its replacement, even when both use the same listener.
    if exit_probe_context(inst, cfg.get_settings()) != context:
        return None
    return {"context": context, "reachable": reachable}


def request_reselect(inst: dict, reason: str, stable_for: float = 0.0) -> str:
    """Ask the host to move this SIM's country exit to a different node.

    Raised when the control plane gives up on a line. A line that cannot register is the only
    reliable evidence that an exit is unusable for VoWiFi: the ePDG tunnel can be established
    over a path on which SIP then goes unanswered, so no latency probe would catch it. The
    orchestrator owns sing-box and performs the change; this only records the request.

    ``stable_for`` is how long the line was healthy before it broke. Past a threshold the exit
    has demonstrably carried IMS, so the failure belongs to something else — a carrier-side
    problem, or a rekey a marginal path failed to survive. Moving the exit then costs another
    tunnel teardown, changes nothing, and evicts a node the operator may have pinned.

    Returns the country whose exit was asked to move, or "" when there is nothing to ask.
    """
    if stable_for >= RESELECT_MIN_STABLE_SECONDS:
        return ""
    country = line_country(inst)
    current = (status().get("exits") or {}).get(country) or {}
    # A locked exit is a deliberate operator choice — including while it is failing — and a
    # country routed direct has no node to move. A "preferred" pin still allows the move: the
    # host applies the preference when it picks the replacement. The orchestrator enforces
    # this too; skipping here keeps the file quiet.
    if not country or current.get("selection") == "manual" or current.get("mode") == "direct":
        return ""
    document = _read_json(_RESELECT)
    countries = document.get("countries")
    if not isinstance(countries, dict):
        countries = {}
    countries[country] = {"ts": time.time(), "reason": reason,
                          # The node that was in use when the line failed, so the orchestrator
                          # can put it in a cooldown instead of picking it again immediately.
                          "node": str(current.get("node") or ""),
                          "line": str(inst.get("id") or "")}
    _atomic_json(_RESELECT, {"version": 1, "countries": countries})
    return country


def report_stalled_exit(country: str, node: str, reason: str, line: str) -> bool:
    """Tell the host this country's exit is holding connections that carry nothing.

    Deliberately weaker than request_reselect: that one says "this node is bad, move off it",
    which tears down every tunnel on it. This says "the node may well be fine — but the
    sessions pinned to it are dead, close them so the next packet dials again".

    It exists because a stalled session cannot time out on its own. sing-box retires a UDP
    session on an IDLE timer, and a line rebuilding its tunnel retransmits IKE every few
    seconds; each retransmit refreshes the timer on the very session whose outbound already
    died. The line then retries forever against a connection that can never carry a packet.

    The caller must have attributed the failure to the exit AND established that no sibling
    line is registered over it, so this never disturbs a working tunnel. Returns True when a
    report was written.
    """
    country = str(country or "").strip().lower()
    if not country:
        return False
    document = _read_json(_STALLED)
    countries = document.get("countries")
    if not isinstance(countries, dict):
        countries = {}
    countries[country] = {"ts": time.time(), "reason": str(reason or ""),
                          "node": str(node or ""), "line": str(line or "")}
    _atomic_json(_STALLED, {"version": 1, "countries": countries})
    return True


def ensure_line(inst: dict, settings: dict, timeout: float = 18.0) -> dict:
    """Publish desired state and wait for the selected transport's readiness contract.

    Proxy routing is opt-in globally.  With it enabled, missing/unhealthy exits fail closed unless
    the country entry explicitly selects ``direct``.  That is intentional: silently using the
    host default route can expose the wrong geography to an operator ePDG.
    """
    proxy = settings.get("proxy") or {}
    selected_transport = transport()
    publish(settings=settings)
    if not proxy.get("enabled", False):
        return {"ready": True, "mode": "legacy"}
    country = line_country(inst)
    if not country:
        raise EgressError("cannot determine SIM country from MCC; set a line country override")
    exits = proxy.get("exits") or {}
    exit_cfg = exits.get(country) or {}
    if not exit_cfg.get("enabled", False):
        raise EgressError(f"no enabled proxy exit configured for country {country.upper()}")
    if selected_transport == "socks5":
        return _ensure_socks_exit(country, proxy, timeout)
    deadline = time.monotonic() + max(1.0, timeout)
    iid = str(inst.get("id", ""))
    last = {}
    while time.monotonic() < deadline:
        state = status()
        last = (state.get("lines") or {}).get(iid) or {}
        if last.get("ready"):
            return last
        # A terminal config error should be returned immediately; DNS/probe errors may recover.
        if last.get("terminal"):
            break
        time.sleep(0.4)
    reason = last.get("error") or "country egress route was not ready before engine startup"
    raise EgressError(f"{country.upper()} exit unavailable: {reason}")
