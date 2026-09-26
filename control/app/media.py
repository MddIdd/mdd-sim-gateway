"""How call media reaches a line's Asterisk from the browser softphone and native clients.

Two modes, each with its own code path:

``direct`` (the default)
    Every line publishes its own RTP range on the host and Asterisk rewrites its ICE host
    candidate to the host's LAN address (engine/templates/rtp.conf.j2). Nothing in this module
    runs, and the provisioning below hands clients no ICE servers.

``relay``
    No engine publishes a port. One TURN relay (coturn) publishes a single port, and it reaches
    the engines only over the media network, an internal Docker network holding the engines and
    the relay::

        client --TURN (one port, UDP or TCP)--> relay --media network--> engine RTP

    Every engine uses the same fixed RTP range (``RTP_PORTS``) on its own media address, so
    nothing depends on how many lines exist. Two layers keep the relay to call media:

    * coturn relays UDP only (``no-tcp-relay``) and only to addresses on the media network,
      the host's gateway address excluded;
    * each engine drops everything arriving on its media interface except UDP to
      ``RTP_PORTS`` (engine/render.py writes the ruleset, the entrypoint loads it). The engine
      already holds NET_ADMIN; the relay, which faces the internet, holds no capability at all.

The mode is switched by ``python -m app.media`` (install.sh ``media`` for a host install,
``docker exec`` for the container stack). Enabling prepares and verifies everything before it
records the mode, and cleans up after itself on any failure. The control plane then rebuilds
the running lines one at a time (main.media_converge), since port publishing is fixed when a
container is created.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import sys
import threading
import time

import docker

from . import config as cfg
from .version import VERSION

log = logging.getLogger("mdd.media")

DIRECT = "direct"
RELAY = "relay"
MODES = (DIRECT, RELAY)

NETWORK = os.environ.get("MDD_MEDIA_NETWORK", "mdd-sim-gateway-media")
CONTAINER = "mdd-sim-gateway-relay"
MANAGED_LABEL = "io.mdd-sim-gateway.managed"
COMPONENT_LABEL = "io.mdd-sim-gateway.component"
CONFIG_LABEL = "io.mdd-sim-gateway.relay-config"
# Carried by an engine container started in relay mode. An engine without it runs in direct
# mode, which keeps every container created before this existed where it is.
MODE_LABEL = "io.mdd-sim-gateway.media-mode"

DEFAULT_PORT = 8478
LISTEN_PORT = 3478                  # inside the relay container
# Every engine's Asterisk RTP pool in relay mode. Engines have their own addresses on the media
# network, so the range is shared rather than staggered per line. One call uses one port.
RTP_PORTS = (10000, 10199)
# The relay's own allocations, on the media network only; one call holds one.
RELAY_PORTS = (49152, 49251)
REALM = "mdd-sim-gateway"
CREDENTIAL_TTL = 12 * 60 * 60
HEALTH_TIMEOUT = 20


class MediaError(RuntimeError):
    pass


# ------------------------------------------------------------------ recorded mode
def _state_path() -> str:
    return os.path.join(cfg.DATA_DIR, "media", "state.json")


def load_state() -> dict:
    """The recorded mode and relay settings. Missing or unreadable means direct."""
    try:
        with open(_state_path(), encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    # Its own file rather than config.json: the switch runs in a separate process, and the
    # control plane rewrites config.json under an in-process lock only.
    path = _state_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def mode(state: dict | None = None) -> str:
    state = load_state() if state is None else state
    return RELAY if state.get("mode") == RELAY else DIRECT


def default_image() -> str:
    override = os.environ.get("MDD_RELAY_IMAGE", "").strip()
    if override:
        return override
    return f"ghcr.io/mddidd/mdd-sim-gateway-relay:v{VERSION}"


# ------------------------------------------------------------------ media network
def _client():
    from . import engine
    return engine._client()


def _managed(attrs: dict) -> bool:
    labels = (attrs.get("Labels") or (attrs.get("Config") or {}).get("Labels") or {})
    return labels.get(MANAGED_LABEL) == "true"


def ensure_network(client):
    """The internal media network. Docker picks its subnet, so it cannot collide with one the
    host already uses."""
    try:
        network = client.networks.get(NETWORK)
    except docker.errors.NotFound:
        network = client.networks.create(
            NETWORK, driver="bridge", internal=True,
            labels={MANAGED_LABEL: "true", COMPONENT_LABEL: "media-network"})
        network.reload()
    attrs = network.attrs or {}
    if not _managed(attrs):
        raise MediaError(f"Docker network {NETWORK} exists but was not created by MDD")
    if not attrs.get("Internal"):
        raise MediaError(f"Docker network {NETWORK} is not internal; remove it and try again")
    return network


def network_addressing(network) -> tuple[ipaddress.IPv4Network, ipaddress.IPv4Address]:
    """(subnet, gateway) of the media network's IPv4 pool."""
    for entry in ((network.attrs or {}).get("IPAM") or {}).get("Config") or []:
        try:
            subnet = ipaddress.ip_network(entry.get("Subnet") or "")
        except ValueError:
            continue
        if subnet.version != 4:
            continue
        gateway = entry.get("Gateway") or str(subnet.network_address + 1)
        return subnet, ipaddress.ip_address(gateway.split("/")[0])
    raise MediaError(f"Docker network {NETWORK} has no IPv4 subnet")


def peer_ranges(subnet: ipaddress.IPv4Network,
                gateway: ipaddress.IPv4Address) -> list[tuple[str, str]]:
    """The addresses the relay may send to: the media network's hosts except the gateway,
    which is the host itself."""
    first = subnet.network_address + 1
    last = subnet.broadcast_address - 1
    ranges = []
    if first < gateway:
        ranges.append((first, gateway - 1))
    if gateway < last:
        ranges.append((max(first, gateway + 1), last))
    if not first <= gateway <= last:
        ranges = [(first, last)]
    return [(str(low), str(high)) for low, high in ranges]


# ------------------------------------------------------------------ relay configuration
def render_config(secret: str, subnet: ipaddress.IPv4Network,
                  gateway: ipaddress.IPv4Address) -> str:
    """coturn's configuration. The entrypoint adds the addresses it only knows once started
    (relay-ip on the media network, listening-ip everywhere else)."""
    lines = [
        "# Written by the MDD control plane; the relay container is recreated when it changes.",
        f"listening-port={LISTEN_PORT}",
        f"min-port={RELAY_PORTS[0]}",
        f"max-port={RELAY_PORTS[1]}",
        f"realm={REALM}",
        # TURN REST credentials: short-lived, signed by the control plane, nothing to revoke.
        "use-auth-secret",
        f"static-auth-secret={secret}",
        "fingerprint",
        # Media is DTLS-SRTP end to end and TURN authenticates with HMAC, so the relay holds
        # nothing a TLS listener would protect; a self-signed one would not be trusted anyway.
        # (DTLS listeners are off unless asked for.)
        "no-tls",
        # UDP media only: no TCP relay (RFC 6062), so AMI, SIP over TCP and every other TCP
        # listener stay out of reach whatever the peer list says.
        "no-tcp-relay",
        "no-multicast-peers",
        "no-cli",
        "no-rfc5780",
        "no-stun-backward-compatibility",
        "no-software-attribute",
        "stale-nonce=600",
        "user-quota=10",
        "total-quota=50",
        "max-bps=64000",
        # Deny everything, then allow only the media network. allowed-peer-ip wins over
        # denied-peer-ip; the IPv6 range also covers IPv4-mapped addresses, a known bypass of
        # IPv4-only deny lists.
        "denied-peer-ip=0.0.0.0-255.255.255.255",
        "denied-peer-ip=::-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
        *(f"allowed-peer-ip={low}-{high}" for low, high in peer_ranges(subnet, gateway)),
        "userdb=/tmp/turndb",
        "pidfile=/tmp/turnserver.pid",
        "log-file=stdout",
        "simple-log",
    ]
    return "\n".join(lines) + "\n"


def credentials(principal: str, secret: str, ttl: int = CREDENTIAL_TTL,
                now: float | None = None) -> dict:
    """TURN REST API credentials: ``<expiry>:<principal>`` signed with the shared secret.
    They expire on their own, so a copied one stops working without any revocation step."""
    expiry = int(now if now is not None else time.time()) + int(ttl)
    username = f"{expiry}:{principal}"
    digest = hmac.new(secret.encode(), username.encode(), hashlib.sha1).digest()
    return {"username": username, "credential": base64.b64encode(digest).decode(),
            "expires": expiry}


def provisioning(principal: str, request_host: str, state: dict | None = None) -> dict:
    """What a softphone or client needs for media, one shape for both modes."""
    state = load_state() if state is None else state
    if mode(state) != RELAY:
        return {"media_mode": DIRECT, "ice_servers": [], "ice_transport_policy": "all",
                "relay_ready": None}
    host = str(state.get("public_host") or request_host or "").strip()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = int(state.get("public_port") or state.get("port") or DEFAULT_PORT)
    creds = credentials(principal, str(state.get("secret") or ""))
    return {
        "media_mode": RELAY,
        "ice_servers": [{"urls": [f"turn:{host}:{port}?transport=udp",
                                  f"turn:{host}:{port}?transport=tcp"],
                         "username": creds["username"], "credential": creds["credential"]}],
        "ice_transport_policy": "relay",
        "relay_ready": relay_status().get("state") == "ready",
    }


# ------------------------------------------------------------------ relay container
def ensure_image(client, reference: str):
    try:
        return client.images.get(reference)
    except docker.errors.ImageNotFound:
        from .engine import _names_a_registry
        if not _names_a_registry(reference):
            raise MediaError(f"relay image {reference} is not present") from None
    try:
        client.images.pull(reference)
        return client.images.get(reference)
    except Exception as exc:  # noqa: BLE001 - offline, registry refused: say which image
        raise MediaError(f"cannot fetch relay image {reference}: {exc}") from exc


def _container(client):
    try:
        return client.containers.get(CONTAINER)
    except docker.errors.NotFound:
        return None


def ensure_relay(client, state: dict, network=None):
    """Make the relay container match ``state``. It is replaced only when what it runs would
    change, so calling this repeatedly never drops a live call."""
    network = network or ensure_network(client)
    subnet, gateway = network_addressing(network)
    secret = str(state.get("secret") or "")
    if not secret:
        raise MediaError("relay secret is missing")
    config = render_config(secret, subnet, gateway)
    image = ensure_image(client, str(state.get("image") or default_image()))
    port = int(state.get("port") or DEFAULT_PORT)
    bind = str(state.get("bind") or "")
    fingerprint = hashlib.sha256("\0".join(
        [config, image.id, str(port), bind, str(subnet)]).encode()).hexdigest()[:32]

    current = _container(client)
    if current is not None:
        labels = (current.attrs.get("Config") or {}).get("Labels") or {}
        if labels.get(MANAGED_LABEL) != "true":
            raise MediaError(f"refusing to replace foreign container {CONTAINER}")
        if labels.get(CONFIG_LABEL) == fingerprint:
            if current.status != "running":
                current.start()
            return current
        current.remove(force=True)

    publish = (bind, port) if bind else port
    container = client.containers.create(
        image.id,
        name=CONTAINER,
        # The published port lives on the default bridge; the media network is internal.
        network="bridge",
        ports={f"{LISTEN_PORT}/udp": publish, f"{LISTEN_PORT}/tcp": publish},
        environment={"MDD_RELAY_CONFIG": config, "MDD_MEDIA_SUBNET": str(subnet)},
        cap_drop=["ALL"],
        security_opt=["no-new-privileges:true"],
        read_only=True,
        tmpfs={"/tmp": "rw,nosuid,nodev,size=8m"},
        pids_limit=64,
        mem_limit="128m",
        restart_policy={"Name": "unless-stopped"},
        labels={MANAGED_LABEL: "true", COMPONENT_LABEL: "relay", CONFIG_LABEL: fingerprint},
        log_config={"Type": "json-file", "Config": {"max-size": "5m", "max-file": "2"}},
    )
    try:
        network.connect(container)
        container.start()
    except Exception:
        container.remove(force=True)
        raise
    log.info("started media relay %s on port %s", CONTAINER, port)
    return container


def check_relay(client) -> tuple[bool, str]:
    """(ready, reason). Ready means turnserver answers a STUN binding request."""
    container = _container(client)
    if container is None:
        return False, "relay_missing"
    if container.status != "running":
        return False, "relay_stopped"
    try:
        result = container.exec_run(
            ["turnutils_stunclient", "-p", str(LISTEN_PORT), "127.0.0.1"], demux=False)
    except Exception:  # noqa: BLE001 - Docker busy or the container going away
        return False, "relay_unreachable"
    return (True, "") if result.exit_code == 0 else (False, "relay_not_answering")


def wait_ready(client, timeout: float = HEALTH_TIMEOUT) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    while True:
        ready, reason = check_relay(client)
        if ready or time.monotonic() >= deadline:
            return ready, reason
        time.sleep(1)


def remove_relay(client) -> None:
    container = _container(client)
    if container is not None:
        labels = (container.attrs.get("Config") or {}).get("Labels") or {}
        if labels.get(MANAGED_LABEL) == "true":
            container.remove(force=True)


def remove_network(client) -> bool:
    """Remove the media network once nothing is attached. False while engines still are."""
    try:
        network = client.networks.get(NETWORK)
    except docker.errors.NotFound:
        return True
    network.reload()
    if not _managed(network.attrs or {}):
        return True
    if (network.attrs or {}).get("Containers"):
        return False
    network.remove()
    return True


# ------------------------------------------------------------------ supervision
_status_lock = threading.Lock()
_status: dict = {"state": "off", "reason": ""}
_image_checked = False


def relay_status() -> dict:
    with _status_lock:
        return dict(_status)


def _set_status(state: str, reason: str = "") -> None:
    with _status_lock:
        _status.update({"state": state, "reason": reason, "checked_at": int(time.time())})


def _refresh_image(client, state: dict) -> dict:
    """After an update, move the relay to this version's image. A fetch failure keeps the
    image already in use: the update itself has succeeded, and the old relay still works."""
    global _image_checked
    if _image_checked:
        return state
    _image_checked = True
    wanted = default_image()
    if state.get("image") == wanted:
        return state
    try:
        ensure_image(client, wanted)
    except MediaError as exc:
        log.warning("keeping relay image %s: %s", state.get("image"), exc)
        return state
    state = {**state, "image": wanted}
    save_state(state)
    return state


def supervise(client=None) -> dict:
    """One pass: in relay mode bring the relay back if it is gone or stopped, and record
    whether it answers. Called periodically by the control plane."""
    state = load_state()
    if mode(state) != RELAY:
        _set_status("off")
        return relay_status()
    try:
        client = client or _client()
        state = _refresh_image(client, state)
        ensure_relay(client, state)
        ready, reason = check_relay(client)
        _set_status("ready" if ready else "unavailable", reason)
    except Exception as exc:  # noqa: BLE001 - reported, retried on the next pass
        log.warning("media relay not ready: %s", exc)
        _set_status("unavailable", "relay_error")
    return relay_status()


def engine_attachment(client) -> dict | None:
    """In relay mode: the network an engine joins and what its instance.json needs to know.
    None in direct mode. A media network that cannot be prepared leaves the line without
    browser media, never without registration and SMS."""
    if mode() != RELAY:
        return None
    try:
        network = ensure_network(client)
        subnet, _gateway = network_addressing(network)
    except Exception as exc:  # noqa: BLE001
        log.error("media network unavailable, starting the line without it: %s", exc)
        return {"network": None, "instance": {"mode": RELAY, "subnet": "",
                                              "rtp_start": RTP_PORTS[0], "rtp_end": RTP_PORTS[1]}}
    return {"network": network,
            "instance": {"mode": RELAY, "subnet": str(subnet),
                         "rtp_start": RTP_PORTS[0], "rtp_end": RTP_PORTS[1]}}


# ------------------------------------------------------------------ switching
# What engine/render.py's media_ruleset relies on, tried once in a throwaway engine container
# before any line is moved: nf_tables, and its socket match on the input hook. A kernel without
# them would leave every line's media interface down.
PROBE_RULESET = (
    "table inet mdd_media_probe {\n"
    "  chain input {\n"
    "    type filter hook input priority filter; policy accept;\n"
    f"    udp dport {RTP_PORTS[0]}-{RTP_PORTS[1]} socket wildcard 0 accept\n"
    "  }\n"
    "}\n")


def probe_engine_firewall(client, network) -> None:
    from . import engine
    try:
        image = engine.ensure_image(client)
    except Exception as exc:  # noqa: BLE001
        raise MediaError(f"engine image unavailable: {exc}") from exc
    try:
        client.containers.run(
            image.id,
            entrypoint=["sh", "-c", 'printf "%s" "$RULES" | nft -f -'],
            environment={"RULES": PROBE_RULESET},
            network=network.name,
            cap_add=["NET_ADMIN"],
            labels={MANAGED_LABEL: "true", COMPONENT_LABEL: "media-probe"},
            remove=True, stdout=True, stderr=True)
    except Exception as exc:  # noqa: BLE001 - ContainerError carries nft's own message
        detail = getattr(exc, "stderr", b"") or str(exc)
        if isinstance(detail, bytes):
            detail = detail.decode(errors="replace")
        raise MediaError("engines cannot filter the media network on this host (the engine "
                         "image needs nftables, the kernel nf_tables with its socket match): "
                         f"{detail.strip()}") from exc


def enable(client, *, port: int, bind: str = "", public_host: str = "",
           public_port: int | None = None, image: str = "") -> dict:
    """Prepare and verify the relay, then record relay mode. On any failure everything this
    call created is removed and the recorded mode is left as it was."""
    previous = load_state()
    had_network = True
    try:
        client.networks.get(NETWORK)
    except docker.errors.NotFound:
        had_network = False
    state = {
        "mode": RELAY,
        "port": int(port),
        "bind": bind,
        "public_host": public_host,
        "public_port": int(public_port or port),
        "image": image or str(previous.get("image") or "") or default_image(),
        "secret": str(previous.get("secret") or "") or secrets.token_urlsafe(32),
    }
    try:
        network = ensure_network(client)
        probe_engine_firewall(client, network)
        ensure_relay(client, state, network)
        ready, reason = wait_ready(client)
        if not ready:
            raise MediaError(f"relay did not become ready ({reason})")
    except Exception:
        remove_relay(client)
        if mode(previous) == RELAY:
            try:
                ensure_relay(client, previous)
            except Exception as exc:  # noqa: BLE001 - supervision retries it
                log.warning("could not restore the previous relay: %s", exc)
        elif not had_network:
            remove_network(client)
        raise
    save_state(state)
    return state


def disable(client, wait: float = 600) -> bool:
    """Record direct mode and remove the relay. The control plane rebuilds the lines; the
    media network goes once the last one has left it. False if that did not happen in time
    (the network is harmless and is removed by the next ``disable``)."""
    state = load_state()
    if state:
        save_state({**state, "mode": DIRECT})
    remove_relay(client)
    deadline = time.monotonic() + wait
    while True:
        if remove_network(client):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(3)


def attached_engines(client) -> list[str]:
    try:
        network = client.networks.get(NETWORK)
        network.reload()
    except docker.errors.NotFound:
        return []
    return sorted(str(entry.get("Name") or "")
                  for entry in ((network.attrs or {}).get("Containers") or {}).values()
                  if str(entry.get("Name") or "") != CONTAINER)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.media", description="Show or switch how call media is carried.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("status", help="show the current media mode")
    relay = sub.add_parser("relay", help="carry media through the built-in TURN relay")
    relay.add_argument("--port", type=int, default=None,
                       help=f"host port the relay publishes, UDP and TCP (default {DEFAULT_PORT})")
    relay.add_argument("--bind", default=None, help="host address to publish it on (default: all)")
    relay.add_argument("--public-host", default=None,
                       help="host name clients use for the relay (default: the one they reached "
                            "the WebUI by)")
    relay.add_argument("--public-port", type=int, default=None,
                       help="port clients use, when a router forwards a different one")
    relay.add_argument("--image", default="", help="relay image to use (default: this version's)")
    direct = sub.add_parser("direct", help="publish each line's RTP ports (the default)")
    direct.add_argument("--wait", type=float, default=600,
                        help="seconds to wait for the lines to leave the media network")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    client = docker.from_env(timeout=60)
    state = load_state()
    if args.command in (None, "status"):
        current = mode(state)
        print(f"media mode: {current}")
        if current == RELAY:
            ready, reason = check_relay(client)
            print(f"relay: {'ready' if ready else 'unavailable (' + reason + ')'}; "
                  f"port {state.get('port')}"
                  + (f", clients use {state.get('public_host') or '<WebUI host>'}:"
                     f"{state.get('public_port')}"))
            print(f"lines on the media network: {', '.join(attached_engines(client)) or 'none'}")
        return 0
    if args.command == "relay":
        port = args.port if args.port is not None else int(state.get("port") or DEFAULT_PORT)
        if not 1 <= port <= 65535:
            parser.error("--port must be 1-65535")
        try:
            new = enable(
                client, port=port,
                bind=args.bind if args.bind is not None else str(state.get("bind") or ""),
                public_host=(args.public_host if args.public_host is not None
                             else str(state.get("public_host") or "")),
                public_port=(args.public_port if args.public_port is not None
                             else (state.get("public_port") if args.port is None else None)),
                image=args.image)
        except Exception as exc:  # noqa: BLE001 - the operator reads this, not a traceback
            print(f"media mode unchanged: {exc}", file=sys.stderr)
            return 1
        print(f"media mode: relay (port {new['port']}/udp+tcp)."
              + ("" if mode(state) == RELAY else
                 " Running lines are rebuilt one at a time and register again."))
        return 0
    if args.command == "direct":
        if mode(state) == DIRECT and not attached_engines(client) and _container(client) is None:
            remove_network(client)
            print("media mode: direct (unchanged)")
            return 0
        print("media mode: direct. Running lines are rebuilt one at a time and register again.")
        if disable(client, wait=args.wait):
            print("relay and media network removed")
            return 0
        print(f"still on the media network: {', '.join(attached_engines(client))}; "
              "run this again once they have been rebuilt", file=sys.stderr)
        return 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
