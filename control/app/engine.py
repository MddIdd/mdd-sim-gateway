"""
engine.py - Per-SIM engine container lifecycle via the Docker SDK.

Each instance runs one `mdd-sim-gateway/engine` container that owns its ePDG tunnel + Asterisk.
The manager renders instance.json, starts/stops/recreates the container with the right
mounts/caps/ports, and reads the engine's runtime status files (bind-mounted run dir):
the swu_ike daemon publishes swu_status.json {state: CONNECTED} for tunnel state.

PC/SC: engine containers are pcscd CLIENTS — they mount the HOST pcscd socket (/run/pcscd).
The pcsc-lite client library in the engine image is pinned to the SAME version as the host
pcscd (Dockerfile PCSC_VERSION == install.sh PCSC_VERSION) so client/server protocol matches.
"""
from __future__ import annotations

from datetime import datetime
import json
import logging
import os
import re
import shutil
import threading
import time

import docker

from . import config as cfg, egress, sysinfo

log = logging.getLogger("mdd.engine")

# Bounded so a line that rebuilds every two minutes cannot fill a Pi's SD card. Only the
# recent tail is diagnostically useful.
DIAGNOSTIC_RECORDS = 200
LIFECYCLE_RECORDS = 500
LIFECYCLE_EVENTS = {
    "recovery_scheduled", "recovery_blocked", "recovery_started", "recovery_failed",
    "recovery_succeeded", "recovery_cancelled", "vowifi_disabled",
    # A start/reprovision refused by the PIN/identity preflight before the engine ran.
    # reason_code carries the closed code (no_card / pin_required / pin_invalid /
    # card_mismatch / card_unreadable); the ICCID itself never enters this public record.
    "preflight_blocked",
}
_LIFECYCLE_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_LIFECYCLE_INSTANCE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_diagnostic_jsonl_lock = threading.Lock()
_lifecycle_jsonl_lock = threading.Lock()
# Asterisk writes colour escapes even when captured to a file; strip them so the stored
# record stays greppable.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Lines worth keeping from a container that is about to be destroyed: the IMS registration
# exchange and the reasons Asterisk gives for not completing it. Matching on words such as
# "registration" or the endpoint name pulls in DEBUG chatter instead — nearly every debug
# line names res_pjsip_outbound_registration.c — which then crowds the real evidence out of
# the bounded tail. Match protocol lines and operator-visible failures only.
_SIP_EVIDENCE = re.compile(
    r"SIP/2\.0 \d{3}"                       # response status line
    r"|^(?:REGISTER|INVITE|MESSAGE|SUBSCRIBE) sip:"   # request line
    r"|No response received"                # the registration timed out with no answer
    r"|transport '[^']+' failed"            # the tunnel died under an established transport
    r"|Status: \w+"                         # Asterisk's own registration verdict
    r"|Failed to authenticate"
    r"|[Uu]nable to register")
# A DEBUG line only earns its place when it carries an actual SIP status line.
_DEBUG_LINE = re.compile(r"\bDEBUG\b")
_DISPLAY_TIMESTAMP = re.compile(
    r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{4}\] ")
_DOCKER_TIMESTAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2}) (.*)$")
_ASTERISK_TIMESTAMP = re.compile(r"^\[[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\]\s*")
# Enough to cover a full REGISTER exchange plus the failures around it.
SIP_EVIDENCE_LINES = 40

DATA_DIR = cfg.DATA_DIR
IMAGE = os.environ.get("MDD_ENGINE_IMAGE", "mdd-sim-gateway/engine")
PCSCD_SOCK = os.environ.get("MDD_PCSCD_DIR", "/run/pcscd")
# Absolute host path to the project data dir (needed for bind mounts when the manager
# itself runs in a container; defaults to DATA_DIR on the host).
HOST_DATA_DIR = os.environ.get("MDD_HOST_DATA", DATA_DIR)
MANAGED_LABEL = "io.mdd-sim-gateway.managed"


def _owned(container) -> bool:
    labels = (container.attrs.get("Config") or {}).get("Labels") or {}
    image = str((container.attrs.get("Config") or {}).get("Image") or "")
    return labels.get(MANAGED_LABEL) == "true" or image.startswith("mdd-sim-gateway/")


def _host_data_path(path: str) -> str:
    """Translate a control-container path under MDD_DATA to the same file on the host.

    Explicit TLS files are configured from the WebUI as /data/... in Docker mode. Sibling
    engine containers cannot bind-mount that container-only path; Docker needs the host's
    /opt/... data path instead. Paths outside DATA_DIR are already host/native paths.
    """
    absolute = os.path.abspath(path)
    data_root = os.path.abspath(DATA_DIR)
    try:
        if os.path.commonpath([absolute, data_root]) == data_root:
            return os.path.join(os.path.abspath(HOST_DATA_DIR), os.path.relpath(absolute, data_root))
    except ValueError:
        pass
    return absolute


def _runtime_data_path(path: str) -> str:
    """Translate a TLS path persisted while the manager used Docker's /data mount.

    The native control plane and sibling engine both use the host data directory.  Without this
    migration, the WebUI can have the public certificate while Asterisk silently falls back to
    an old self-signed certificate, causing browsers to reject the softphone WSS connection.
    """
    value = str(path or "")
    if value.startswith("/data/") and os.path.abspath(DATA_DIR) != "/data":
        translated = os.path.join(DATA_DIR, os.path.relpath(value, "/data"))
        if os.path.exists(translated):
            return translated
    return value


_docker_client = None
_docker_client_lock = threading.Lock()


def _client():
    """Reuse the Docker HTTP connection pool instead of rebuilding it on every status sample."""
    global _docker_client
    if _docker_client is None:
        with _docker_client_lock:
            if _docker_client is None:
                _docker_client = docker.from_env(timeout=30)
    return _docker_client


def close_client():
    """Release the shared Docker client during control-plane shutdown."""
    global _docker_client
    with _docker_client_lock:
        client, _docker_client = _docker_client, None
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


def container_name(iid: str) -> str:
    return f"mdd-sim-gateway-engine-{iid}"


def _instance_paths(iid: str):
    base = os.path.join(DATA_DIR, "instances", str(iid))
    host_base = os.path.join(HOST_DATA_DIR, "instances", str(iid))
    os.makedirs(os.path.join(base, "run"), exist_ok=True)
    os.makedirs(os.path.join(base, "logs"), exist_ok=True)
    return base, host_base


def _clear_runtime_state(base: str):
    """Remove observations owned by the previous engine process.

    Runtime files are bind-mounted outside the container and therefore survive a container
    recreation.  Keeping an old CONNECTED marker makes the new process look online before it
    has completed IKE and IMS registration.
    """
    run_dir = os.path.join(base, "run")
    for name in ("swu_status.json", "pcscf", "pcscf.applied", "pin_status.json",
                 "usim_status.json", "engine.env", "swu.ctl"):
        try:
            os.unlink(os.path.join(run_dir, name))
        except FileNotFoundError:
            pass


def _tail_lines(path: str, limit: int) -> list[str]:
    try:
        with open(path, errors="replace") as handle:
            return handle.read().splitlines()[-limit:]
    except OSError:
        return []


def _charon_evidence(base: str) -> dict:
    """Summarise IKE health from the tunnel log.

    Retransmits and outright IKE timeouts are the signature of a lossy country exit, which
    looks identical to a carrier problem from the status machine's point of view.
    """
    lines = _tail_lines(os.path.join(base, "run", "charon.log"), 400)
    last_state = ""
    for line in reversed(lines):
        bare = _DISPLAY_TIMESTAMP.sub("", line, count=1)
        if bare.startswith("STATE ") or "tunnel CONNECTED" in bare:
            last_state = line.strip()
            break
    return {"available": bool(lines),
            "retransmits": sum(1 for line in lines if "retransmit" in line),
            "timeouts": sum(1 for line in lines if "TIMEOUT" in line),
            "last_state": last_state, "tail": lines[-40:]}


def ike_evidence(iid: str) -> dict:
    """Retransmit/timeout counts for one line, without building a whole diagnostic snapshot.

    The failover policy needs to know whether the tunnel's own signalling was answered; it
    runs on the freeze path, where reading the container's logs would be far too heavy.
    """
    base, _host_base = _instance_paths(str(iid))
    return _charon_evidence(base)


def registration_failure_evidence(log_tail: str) -> dict:
    """Classify the newest concrete REGISTER failure and retain its SIP response code.

    Asterisk reports both as "Rejected", but they are different events: a "Fatal response
    '403'" is the IMS refusing this line, while "No response received" is the IMS no longer
    hearing it — on this gateway almost always an ESP session the carrier aged out while
    the IKE side still answered keepalives. The newest marker in the log decides.
    """
    for line in reversed(log_tail.splitlines()):
        low = line.lower()
        # The real Asterisk message says "on registration attempt", not "on REGISTER
        # attempt".  ``registration`` does not contain the substring ``register``, so the
        # old extra guard made this production path unreachable.  This exact marker is emitted
        # by outbound registration's timeout path and is already the evidence retained by
        # _SIP_EVIDENCE.  A Docker log read failure is returned as "error: ...", which
        # deliberately does not match and therefore remains on the conservative slow path.
        if "no response received" in low:
            return {"kind": "unanswered"}
        match = re.search(r"fatal response '(\d+)'", low)
        if match:
            return {"kind": "rejected", "sip_status": int(match.group(1))}
    return {"kind": "unknown"}


def _sip_evidence(raw: str) -> list[str]:
    """Keep the SIP protocol lines and registration failures from a container log."""
    kept = []
    for line in raw.splitlines():
        line = _ANSI.sub("", line).rstrip()
        bare = _DISPLAY_TIMESTAMP.sub("", line, count=1)
        if not _SIP_EVIDENCE.search(bare):
            continue
        if _DEBUG_LINE.search(bare) and "SIP/2.0" not in bare:
            continue
        kept.append(line)
    return kept[-SIP_EVIDENCE_LINES:]


def _egress_evidence(inst: dict) -> dict:
    """Which exit node this line was using when it failed."""
    try:
        country = egress.line_country(inst)
        current = (egress.status().get("exits") or {}).get(country) or {}
        return {"country": country, "node": current.get("node", ""),
                "selection": current.get("selection", ""),
                "candidate_count": current.get("candidate_count"),
                "ready": current.get("ready")}
    except Exception:
        return {}


def _host_evidence() -> dict:
    """The host conditions that can take every line down at once, plus what they mean."""
    try:
        snapshot = sysinfo.collect(DATA_DIR)
        return {"alerts": [item["code"] for item in sysinfo.alerts(snapshot)],
                "throttling": snapshot.get("throttling") or {},
                "undervoltage": snapshot.get("undervoltage") or {},
                "temperature_c": snapshot.get("temperature_c"),
                "load": (snapshot.get("load") or {}).get("per_core"),
                "memory": snapshot.get("memory") or {},
                "network": snapshot.get("network") or {}}
    except Exception:
        return {}


def _append_bounded_jsonl(path: str, record: dict, limit: int,
                          lock: threading.Lock, *, create_parent: bool = True) -> None:
    """Atomically append one record while retaining only a bounded tail."""
    if create_parent:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    with lock:
        keep = max(0, int(limit) - 1)
        lines = _tail_lines(path, keep) if keep else []
        lines.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.replace(tmp, path)


def _append_diagnostic(base: str, record: dict):
    _append_bounded_jsonl(
        os.path.join(base, "logs", "diagnostics.jsonl"), record, DIAGNOSTIC_RECORDS,
        _diagnostic_jsonl_lock)


def valid_lifecycle_reason(value: object) -> bool:
    """One shared validator for producers and the persistence boundary."""
    return bool(_LIFECYCLE_REASON.fullmatch(str(value or "")))


def record_lifecycle(iid: str, event: str, *, reason_code: str = "", **facts) -> None:
    """Persist one recovery decision without exporting identifiers or exception text.

    This is intentionally a closed schema.  Free-form exception strings, paths, hardware ids
    and subscriber identifiers must never enter a file intended for public support bundles.
    """
    event = str(event or "")
    reason_code = str(reason_code or "")
    iid = str(iid or "")
    if event not in LIFECYCLE_EVENTS:
        raise ValueError("invalid lifecycle event")
    if not _LIFECYCLE_INSTANCE.fullmatch(iid):
        raise ValueError("invalid lifecycle instance")
    if reason_code and not valid_lifecycle_reason(reason_code):
        raise ValueError("invalid lifecycle reason code")
    record = {"ts": int(time.time()), "instance": iid, "event": event}
    if reason_code:
        record["reason_code"] = reason_code
    for key in ("retry_count", "delay_seconds"):
        if key in facts and facts[key] is not None:
            record[key] = max(0, int(facts[key]))
    # The SIP response code behind a reg_rejected freeze (e.g. 403). A bare code is
    # support-safe and is exactly what distinguishes a carrier refusal from a dead tunnel.
    if facts.get("sip_status") is not None:
        record["sip_status"] = max(0, min(999, int(facts["sip_status"])))
    for key in ("card_present", "imei_valid", "imei_source_matches", "iccid_matches"):
        if key in facts and facts[key] is not None:
            record[key] = bool(facts[key])
    base = os.path.join(DATA_DIR, "instances", iid)
    # A lifecycle record may only extend an existing line directory.  Creating it here would
    # resurrect deleted/unknown ids and violate the API's "delete history" guarantee.
    if not os.path.isdir(base):
        return
    _append_bounded_jsonl(
        os.path.join(base, "logs", "lifecycle.jsonl"), record, LIFECYCLE_RECORDS,
        _lifecycle_jsonl_lock, create_parent=False)


def capture_diagnostics(iid: str, inst: dict, base: str, reason: str):
    """Persist the evidence that recreating the container is about to destroy.

    Container logs and Asterisk's live registration view disappear with ``remove()``. A line
    stuck in the health policy's rebuild loop destroys its own evidence every couple of
    minutes, which is precisely when that evidence is needed. Never raises: a failed capture
    must not block the rebuild it is documenting.
    """
    try:
        record = {"ts": int(time.time()), "instance": str(iid), "reason": reason,
                  "registration": registration_state(iid),
                  "pcscf": read_pcscf(iid) or "",
                  "charon": _charon_evidence(base),
                  "egress": _egress_evidence(inst),
                  # A brown-out takes out the USB-attached NIC, the modem and the reader at
                  # once, and reaches the status machine as a tunnel that simply stopped
                  # passing traffic. Recorded here so that cause is visible beside the effect
                  # instead of being reconstructed from a shell session days later.
                  "host": _host_evidence()}
        for name in ("swu_status.json", "usim_status.json", "pin_status.json"):
            record[name[:-5]] = read_run_json(iid, name) or {}
        tail = logs(iid, 600)
        record["sip"] = _sip_evidence(tail)
        # The bare "Rejected" registration string cannot say WHY (issue #33 shipped a bundle
        # whose SIP response code was already rotated away); keep the classified verdict and
        # its numeric status code beside it. Digits-only, so redaction passes it through.
        record["registration_evidence"] = registration_failure_evidence(tail)
        _append_diagnostic(base, record)
    except Exception as exc:  # noqa
        log.warning("diagnostic capture failed for instance %s: %s", iid, exc)


def start(inst: dict, settings: dict, dev_mounts: bool = False, reason: str = "rebuild"):
    """(Re)create and start the engine container for an instance."""
    iid = str(inst["id"])
    # Fail closed before creating the container when country routing is enabled. The host-side
    # orchestrator confirms that this carrier's outer ePDG address is routed through the selected
    # country TUN; inner IMS/SIP/RTP then stays inside the resulting IPsec tunnel.
    egress.ensure_line(inst, settings)
    cfg.write_instance_json(inst, settings)
    base, host_base = _instance_paths(iid)
    ports = inst.get("ports", {})
    client = _client()
    # remove any existing container
    try:
        old = client.containers.get(container_name(iid))
        if not _owned(old):
            raise RuntimeError(f"refusing to replace foreign container {old.name}")
        # Only a replacement destroys evidence; a first start has none to keep.
        capture_diagnostics(iid, inst, base, reason)
        old.remove(force=True)
    except docker.errors.NotFound:
        pass

    _clear_runtime_state(base)

    volumes = {
        os.path.join(host_base, "instance.json"): {"bind": "/config/instance.json", "mode": "ro"},
        os.path.join(host_base, "logs"): {"bind": "/logs", "mode": "rw"},
        os.path.join(host_base, "run"): {"bind": "/run/mdd-sim-gateway", "mode": "rw"},
        PCSCD_SOCK: {"bind": "/run/pcscd", "mode": "rw"},
    }
    # The image has no timezone, so every engine log (IKE, Asterisk) was stamped in UTC while
    # the timeline, the WebUI and the operator's shell read local time. Correlating a rekey or
    # a teardown with an outage meant doing the offset in your head. Give the container the
    # host's zone; nothing parses these timestamps, so this is display only.
    if os.path.exists("/etc/localtime"):
        volumes["/etc/localtime"] = {"bind": "/etc/localtime", "mode": "ro"}
    # TLS cert for the local SIP-TLS / WebRTC (WSS 8089) transport. An explicit cert in
    # settings.tls wins; otherwise fall back to the control plane's own self-signed cert
    # (generated by run.py under $MDD_DATA/certs) so the engine's WSS listener always has
    # a cert — without it Asterisk fails to bind 8089 and the browser softphone can't connect.
    # Bind-mounts must use the HOST path (engine is a sibling container); check existence via
    # the in-container DATA_DIR but mount the HOST_DATA_DIR path.
    tls = settings.get("tls", {})
    configured_cert = _runtime_data_path(tls.get("cert_path"))
    configured_key = _runtime_data_path(tls.get("key_path"))
    cert_host = key_host = None
    if configured_cert and os.path.exists(configured_cert) and \
            configured_key and os.path.exists(configured_key):
        cert_host = _host_data_path(configured_cert)
        key_host = _host_data_path(configured_key)
    else:
        # self-signed pair written by run.py: $MDD_DATA/certs/self-signed.{crt,key}
        ss_crt = os.path.join(DATA_DIR, "certs", "self-signed.crt")
        ss_key = os.path.join(DATA_DIR, "certs", "self-signed.key")
        if os.path.exists(ss_crt) and os.path.exists(ss_key):
            cert_host = os.path.join(HOST_DATA_DIR, "certs", "self-signed.crt")
            key_host = os.path.join(HOST_DATA_DIR, "certs", "self-signed.key")
        else:
            log.warning("no TLS cert available for engine %s WSS/8089 — browser softphone will "
                        "not connect until a cert exists (control plane cert at %s missing)", iid, ss_crt)
    if cert_host and key_host:
        volumes[cert_host] = {"bind": "/etc/asterisk/certificate.crt", "mode": "ro"}
        volumes[key_host] = {"bind": "/etc/asterisk/certificate.key", "mode": "ro"}

    if dev_mounts:
        eng = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "engine")
        for f in ["pin_keeper.py", "ami_usim.py", "render.py", "notify.py", "swu_ike.py",
                  "log_capture.py"]:
            volumes[os.path.join(eng, f)] = {"bind": f"/usr/local/bin/{f}", "mode": "ro"}
        volumes[os.path.join(eng, "entrypoint.sh")] = {"bind": "/entrypoint.sh", "mode": "ro"}
        volumes[os.path.join(eng, "templates")] = {"bind": "/opt/mdd-sim-gateway/templates", "mode": "ro"}

    # Expose only the authenticated browser softphone transport. Standalone
    # SIP UDP/TCP/TLS listeners are not published to the host.
    port_bindings = {f"{8089}/tcp": ports.get("webrtc", 8089)}
    # AMI grants system/command/originate. The manager dials the container bridge directly, so a
    # host mapping is unnecessary in normal operation and costs another docker-proxy. Keep the
    # loopback-only mapping as an explicit diagnostic option.
    if (settings.get("debug") or {}).get("ami", False):
        port_bindings[f"{5038}/tcp"] = ("127.0.0.1", ports.get("ami", 5038))
    # RTP range
    rtp_start = ports.get("rtp_start", 10000)
    for p in range(rtp_start, rtp_start + cfg.rtp_span(ports)):
        port_bindings[f"{p}/udp"] = p

    c = client.containers.run(
        IMAGE,
        name=container_name(iid),
        detach=True,
        cap_add=["NET_ADMIN"],
        devices=["/dev/net/tun:/dev/net/tun:rwm"],
        volumes=volumes,
        ports=port_bindings,
        restart_policy={"Name": "unless-stopped"},
        labels={MANAGED_LABEL: "true", "io.mdd-sim-gateway.component": "engine"},
        environment={
            "MDD_ID": iid,
            "SWU_LIVENESS_PERIOD": str(inst.get("liveness_period", 0)),
        },
        sysctls={
            "net.ipv6.conf.all.accept_ra": "0",
            "net.ipv6.conf.default.accept_ra": "0",
            "net.ipv6.conf.all.autoconf": "0",
            "net.ipv6.conf.default.autoconf": "0",
            "net.ipv6.conf.all.use_tempaddr": "0",
            "net.ipv6.conf.default.use_tempaddr": "0",
        },
        extra_hosts={"host.docker.internal": "host-gateway"},  # so notify.py can reach the manager
    )
    log.info("started engine container %s", c.name)
    return c.id


def stop(iid: str, expected_container_id: str | None = None):
    try:
        c = _client().containers.get(container_name(iid))
        if not _owned(c):
            raise RuntimeError(f"refusing to remove foreign container {c.name}")
        if expected_container_id and str(c.id) != str(expected_container_id):
            log.info("not stopping replacement engine %s (expected generation %s, found %s)",
                     iid, expected_container_id, c.id)
            return False
        c.remove(force=True)
        return True
    except docker.errors.NotFound:
        return False


def capture_and_stop(iid: str, inst: dict, reason: str,
                     expected_container_id: str | None = None) -> bool:
    """Snapshot a failing line, then remove its container.

    The health policy gives up by stopping the container and only rebuilds it after a
    cooldown, so by the time ``start()`` runs there is nothing left to read. This is the
    path that destroys the evidence in practice, and it is also the exact moment worth
    recording: the policy has just concluded the line cannot register.

    Blocking (Docker exec + log read); callers on the event loop must use a worker thread.
    """
    if expected_container_id:
        try:
            current = _client().containers.get(container_name(iid))
            if not _owned(current):
                raise RuntimeError(f"refusing to inspect foreign container {current.name}")
            if str(current.id) != str(expected_container_id):
                return False
        except docker.errors.NotFound:
            return False
    base, _ = _instance_paths(iid)
    capture_diagnostics(iid, inst, base, reason)
    return (stop(iid, expected_container_id=expected_container_id)
            if expected_container_id else stop(iid))


def delete_instance_data(iid: str) -> bool:
    """Remove one deleted line's rendered config, runtime markers and bounded logs."""
    root = os.path.realpath(os.path.join(DATA_DIR, "instances"))
    target = os.path.realpath(os.path.join(root, str(iid)))
    if os.path.dirname(target) != root:
        raise ValueError("invalid instance id")
    if not os.path.isdir(target):
        return False
    shutil.rmtree(target)
    return True


def is_running(iid: str) -> bool:
    return container_runtime(iid)["running"]


def container_runtime(iid: str) -> dict:
    """Return running state and bridge address from one Docker inspect operation."""
    try:
        c = _client().containers.get(container_name(iid))
        running = c.status == "running"
        ip = None
        if running:
            for network in c.attrs.get("NetworkSettings", {}).get("Networks", {}).values():
                if network.get("IPAddress"):
                    ip = network["IPAddress"]
                    break
        return {"running": running, "ip": ip, "container_id": getattr(c, "id", None)}
    except docker.errors.NotFound:
        return {"running": False, "ip": None, "container_id": None}


def container_ip(iid: str) -> str | None:
    return container_runtime(iid)["ip"]


def read_run_json(iid: str, name: str) -> dict | None:
    path = os.path.join(DATA_DIR, "instances", str(iid), "run", name)
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def read_pcscf(iid: str) -> str | None:
    path = os.path.join(DATA_DIR, "instances", str(iid), "run", "pcscf")
    try:
        with open(path) as f:
            v = f.read().strip()
            return v or None
    except Exception:
        return None


def tunnel_installed(iid: str) -> bool:
    """True if the ims tunnel is up: the swu_ike daemon writes run/swu_status.json
    {state: CONNECTED} once the SWu (ePDG) IPsec tunnel is established."""
    st = read_run_json(iid, "swu_status.json")
    return st is not None and st.get("state") == "CONNECTED"


def exec_cli(iid: str, command: str) -> str:
    try:
        c = _client().containers.get(container_name(iid))
        rc, out = c.exec_run(["asterisk", "-rx", command])
        return out.decode(errors="replace") if isinstance(out, bytes) else str(out)
    except Exception as e:  # noqa
        return f"error: {e}"


def registration_state(iid: str) -> str:
    """Read IMS registration through the local Asterisk CLI.

    Some IMS-patched Asterisk builds accept AMI's PJSIPShowRegistrationsDetailed action but
    never complete it. Treating that management timeout as a carrier failure eventually stops
    an otherwise healthy line. The CLI is the authoritative local view and has proven reliable
    on those same builds.
    """
    client = None
    try:
        # Use a short-lived client with an HTTP read timeout. Asterisk's remote CLI can block
        # behind an IMS TCP connect; the normal shared helper intentionally has no global Docker
        # timeout, so using it here would leave one worker thread behind on every status poll.
        client = docker.from_env(timeout=5)
        container = client.containers.get(container_name(iid))
        rc, raw = container.exec_run(["asterisk", "-rx", "pjsip show registrations"])
        output = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
    except Exception:
        return "unknown"
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    if re.search(r"\bRejected\b", output, re.I):
        return "Rejected"
    if re.search(r"\bUnregistered\b", output, re.I):
        return "Unregistered"
    if re.search(r"\bRegistered\b", output):
        return "Registered"
    return "unknown"


def _format_docker_logs(raw: str, local_tz=None) -> str:
    """Render Docker's per-record UTC time in the same local format as the IKE log."""
    rendered = []
    for line in raw.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        ending = line[len(content):]
        match = _DOCKER_TIMESTAMP.match(content)
        if not match:
            rendered.append(line)
            continue
        zone = "+00:00" if match.group(2) == "Z" else match.group(2)
        event_time = datetime.fromisoformat(match.group(1) + zone)
        event_time = event_time.astimezone(local_tz) if local_tz else event_time.astimezone()
        message = _ASTERISK_TIMESTAMP.sub("", match.group(3), count=1)
        rendered.append("[%s] %s%s" % (
            event_time.strftime("%Y-%m-%d %H:%M:%S%z"), message, ending))
    return "".join(rendered)


def logs(iid: str, tail: int = 200, since=None) -> str:
    try:
        c = _client().containers.get(container_name(iid))
        # Docker records the emission time for every physical stdout/stderr line. Request that
        # source timestamp rather than stamping at page-refresh time, then normalize it to the
        # same local display format as charon.log.
        kwargs = {"tail": tail, "timestamps": True}
        if since is not None:
            # docker SDK accepts an int (unix ts) or datetime; used by the SMS delivery
            # watcher to read only the lines emitted after a send.
            kwargs["since"] = since
        raw = c.logs(**kwargs).decode(errors="replace")
        return _format_docker_logs(raw)
    except Exception as e:  # noqa
        return f"error: {e}"


def charon_log(iid: str, tail: int = 200) -> str:
    """Recent SWu tunnel (IKE) log lines from the instance run dir. The file is named
    charon.log for control-plane/WebUI compatibility (the log-view key is 'charon')."""
    path = os.path.join(DATA_DIR, "instances", str(iid), "run", "charon.log")
    try:
        with open(path, errors="replace") as f:
            return "".join(f.readlines()[-tail:])
    except Exception:
        return ""


def usim_status(iid: str) -> dict:
    return read_run_json(iid, "usim_status.json") or {}
