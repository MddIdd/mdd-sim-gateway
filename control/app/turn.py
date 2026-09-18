"""Media relay (coturn) for the browser softphone, and the Docker networks the engines live on.

No engine publishes a port. The browser reaches its line's Asterisk only through one TURN
port on the host:

    browser --TURN (MDD_TURN_PORT, udp+tcp)--> coturn --mdd-media (internal)--> engine eth1

Each engine has two interfaces. eth0 is on ``mdd-engine``: the SWu tunnel's outer traffic, the
control surface's AMI and softphone WebSocket. eth1 is on ``mdd-media``, an internal network
holding only the engines and coturn, where the only listener is the browser leg's RTP. coturn is
not on ``mdd-engine`` and the IMS leg lives inside the tunnel, so the relay cannot reach the
carrier side at all.

Both networks use fixed subnets (overridable) so the relay's allow-lists are static and every
line keeps its addresses across rebuilds. Line ``index`` N gets host .10+N on both networks.

Relaying is restricted twice. coturn itself only accepts peers in the engines' address range
(``allowed-peer-ip`` after denying everything). coturn cannot filter by port, so an nftables
ruleset loaded in coturn's own network namespace additionally drops anything but UDP to the RTP
port range: the engines also hold wildcard sockets (pjsip's DNS resolver) that must stay out of
reach. The media network's gateway address belongs to the host, which is why the allow-list is
the engines' range rather than the whole subnet.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import time

import docker

from . import config as cfg

log = logging.getLogger("mdd.turn")

ENGINE_NETWORK = os.environ.get("MDD_ENGINE_NETWORK", "mdd-engine")
MEDIA_NETWORK = os.environ.get("MDD_MEDIA_NETWORK", "mdd-media")
ENGINE_SUBNET = ipaddress.ip_network(os.environ.get("MDD_ENGINE_SUBNET", "172.29.0.0/24"))
MEDIA_SUBNET = ipaddress.ip_network(os.environ.get("MDD_MEDIA_SUBNET", "172.29.1.0/24"))
IMAGE = os.environ.get("MDD_TURN_IMAGE", "mdd-sim-gateway/turn:4.17.2")
CONTAINER = "mdd-sim-gateway-turn"
MANAGED_LABEL = "io.mdd-sim-gateway.managed"
CONFIG_LABEL = "io.mdd-sim-gateway.turn-config"

# Host port browsers use, and the one to put in their ICE servers when a proxy or NAT in front of
# the gateway forwards a different one.
PORT = int(os.environ.get("MDD_TURN_PORT", "8478"))
PUBLIC_HOST = os.environ.get("MDD_TURN_HOST", "").strip()
PUBLIC_PORT = int(os.environ.get("MDD_TURN_PUBLIC_PORT", "") or PORT)
BIND = os.environ.get("MDD_TURN_BIND", "").strip()

LISTEN_PORT = 3478                 # inside the container
REALM = "mdd-sim-gateway"
# One slot per line index. The product runs at most five lines; the headroom covers indices
# that stay allocated while lines are deleted and recreated.
FIRST_LINE_HOST = 10
LINE_SLOTS = 20
TURN_HOST = 2                      # coturn's address on the media network
# Relay ports live on the media network only; one call holds one allocation.
RELAY_PORTS = (49152, 49251)
# Every line's Asterisk RTP pool falls inside this range (config.PORT_BASE/PORT_STRIDE).
RTP_PORTS = (10000, 19999)
CREDENTIAL_TTL = 24 * 60 * 60


class TurnError(RuntimeError):
    pass


def _host(network: ipaddress.IPv4Network, offset: int) -> str:
    return str(network.network_address + offset)


def gateway(network: ipaddress.IPv4Network) -> str:
    return _host(network, 1)


def turn_media_address() -> str:
    return _host(MEDIA_SUBNET, TURN_HOST)


def line_slot(inst: dict) -> int:
    try:
        index = int(inst.get("index", 0))
    except (TypeError, ValueError):
        index = -1
    if not 0 <= index < LINE_SLOTS:
        raise TurnError(f"line index {inst.get('index')!r} has no network address slot")
    return index


def engine_address(inst: dict) -> str:
    return _host(ENGINE_SUBNET, FIRST_LINE_HOST + line_slot(inst))


def media_address(inst: dict) -> str:
    return _host(MEDIA_SUBNET, FIRST_LINE_HOST + line_slot(inst))


def peer_range() -> tuple[str, str]:
    """The engines' media addresses: the only peers the relay may send to."""
    return (_host(MEDIA_SUBNET, FIRST_LINE_HOST),
            _host(MEDIA_SUBNET, FIRST_LINE_HOST + LINE_SLOTS - 1))


def secret() -> str:
    """The shared secret coturn verifies TURN REST credentials with; created on first use."""
    with cfg._lock:
        data = cfg.load()
        value = str((data.get("internal") or {}).get("turn_secret") or "")
        if value:
            return value
        value = secrets.token_urlsafe(32)
        data["internal"] = {**(data.get("internal") or {}), "turn_secret": value}
        cfg.save(data)
        return value


def credentials(iid: str, ttl: int = CREDENTIAL_TTL, now: float | None = None) -> dict:
    """TURN REST API credentials: ``<expiry>:<line>`` signed with the shared secret.

    Only the admin session's softphone provisioning hands these out. They expire on their own,
    so a copied credential stops working without any revocation step."""
    expiry = int(now if now is not None else time.time()) + int(ttl)
    username = f"{expiry}:{iid}"
    digest = hmac.new(secret().encode(), username.encode(), hashlib.sha1).digest()
    return {"username": username, "credential": base64.b64encode(digest).decode(),
            "expires": expiry}


def ice_servers(iid: str, request_host: str) -> list[dict]:
    host = PUBLIC_HOST or request_host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    creds = credentials(iid)
    return [{"urls": [f"turn:{host}:{PUBLIC_PORT}?transport=udp",
                      f"turn:{host}:{PUBLIC_PORT}?transport=tcp"],
             "username": creds["username"], "credential": creds["credential"]}]


def render_config(turn_secret: str) -> str:
    low, high = peer_range()
    lines = [
        "# Written by the MDD control plane on every start; local edits are overwritten.",
        f"listening-port={LISTEN_PORT}",
        f"relay-ip={turn_media_address()}",
        f"min-port={RELAY_PORTS[0]}",
        f"max-port={RELAY_PORTS[1]}",
        f"realm={REALM}",
        "use-auth-secret",
        f"static-auth-secret={turn_secret}",
        "fingerprint",
        # Bind first, then give up root (and with it NET_ADMIN).
        "proc-user=nobody",
        "proc-group=nogroup",
        # Media is DTLS-SRTP end to end and TURN authenticates with HMAC; the relay carries no
        # secret worth a TLS listener, and a self-signed one would not be trusted by browsers.
        "no-tls",
        "no-dtls",
        # UDP media only: no TCP relay (RFC 6062) means no reaching AMI, the WS listener or any
        # other TCP service through the relay.
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
        # Deny every address, then allow only the engines' media addresses. allowed-peer-ip is
        # checked first; it does not deny anything by itself. The IPv6 range also covers
        # IPv4-mapped addresses, a known bypass of IPv4-only deny lists.
        "denied-peer-ip=0.0.0.0-255.255.255.255",
        "denied-peer-ip=::-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
        f"allowed-peer-ip={low}-{high}",
        "log-file=stdout",
        "simple-log",
        "pidfile=/tmp/turnserver.pid",
    ]
    return "\n".join(lines) + "\n"


def render_nftables() -> str:
    low, high = peer_range()
    return (
        "# Written by the MDD control plane. Loaded in coturn's network namespace before\n"
        "# turnserver starts: relayed traffic may only be UDP to an engine's RTP port.\n"
        "flush ruleset\n"
        "table inet mdd_turn {\n"
        "  chain output {\n"
        "    type filter hook output priority filter; policy drop;\n"
        '    oifname "lo" accept\n'
        # Replies to browsers ride the flows they opened on the listening port.
        "    ct state established,related accept\n"
        f"    ip daddr {low}-{high} udp dport {RTP_PORTS[0]}-{RTP_PORTS[1]} accept\n"
        "  }\n"
        "}\n")


def _client():
    from . import engine
    return engine._client()


def _ensure_network(client, name: str, subnet: ipaddress.IPv4Network, internal: bool):
    try:
        network = client.networks.get(name)
    except docker.errors.NotFound:
        network = None
    if network is None:
        ipam = docker.types.IPAMConfig(pool_configs=[
            docker.types.IPAMPool(subnet=str(subnet), gateway=gateway(subnet))])
        try:
            return client.networks.create(
                name, driver="bridge", internal=internal, ipam=ipam,
                labels={MANAGED_LABEL: "true", "io.mdd-sim-gateway.component": "network"})
        except docker.errors.APIError as exc:
            raise TurnError(f"cannot create Docker network {name} ({subnet}): {exc}. "
                            f"Another network or route may already use it; set "
                            f"MDD_ENGINE_SUBNET / MDD_MEDIA_SUBNET and reload.") from exc
    attrs = network.attrs or {}
    labels = attrs.get("Labels") or {}
    if labels.get(MANAGED_LABEL) != "true":
        raise TurnError(f"Docker network {name} exists but was not created by MDD")
    subnets = [c.get("Subnet") for c in ((attrs.get("IPAM") or {}).get("Config") or [])]
    if str(subnet) not in subnets:
        raise TurnError(f"Docker network {name} uses {subnets}, expected {subnet}; remove it or "
                        f"set the matching MDD_*_SUBNET")
    if bool(attrs.get("Internal")) != internal:
        raise TurnError(f"Docker network {name} internal={attrs.get('Internal')}, expected {internal}")
    return network


def ensure_networks(client=None):
    client = client or _client()
    return (_ensure_network(client, ENGINE_NETWORK, ENGINE_SUBNET, internal=False),
            _ensure_network(client, MEDIA_NETWORK, MEDIA_SUBNET, internal=True))


def _config_dir() -> tuple[str, str]:
    """(path the control plane writes, path Docker bind-mounts on the host)."""
    from . import engine
    return (os.path.join(cfg.DATA_DIR, "turn"), os.path.join(engine.HOST_DATA_DIR, "turn"))


def _write_private(path: str, text: str):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def ensure(client=None) -> dict:
    """Make the relay match the current configuration: networks, config files, container.

    The container is replaced only when what it runs would change (config, ruleset, image or
    published port), so calling this on every start is cheap and never drops a live call."""
    client = client or _client()
    _, media = ensure_networks(client)
    conf = render_config(secret())
    rules = render_nftables()
    local_dir, host_dir = _config_dir()
    os.makedirs(local_dir, mode=0o700, exist_ok=True)
    _write_private(os.path.join(local_dir, "turnserver.conf"), conf)
    _write_private(os.path.join(local_dir, "nftables.conf"), rules)
    try:
        image_id = client.images.get(IMAGE).id
    except docker.errors.ImageNotFound as exc:
        raise TurnError(f"media relay image {IMAGE} is missing; run install.sh reload") from exc
    fingerprint = hashlib.sha256("\0".join(
        [conf, rules, image_id, str(PORT), BIND]).encode()).hexdigest()[:32]

    try:
        current = client.containers.get(CONTAINER)
    except docker.errors.NotFound:
        current = None
    if current is not None:
        labels = (current.attrs.get("Config") or {}).get("Labels") or {}
        if labels.get(MANAGED_LABEL) != "true":
            raise TurnError(f"refusing to replace foreign container {CONTAINER}")
        if labels.get(CONFIG_LABEL) == fingerprint:
            if current.status != "running":
                current.start()
            return {"running": True, "changed": False}
        current.remove(force=True)

    publish = (BIND, PORT) if BIND else PORT
    container = client.containers.create(
        IMAGE,
        name=CONTAINER,
        # Only to load the nftables ruleset; turnserver drops it with root (proc-user).
        cap_add=["NET_ADMIN"],
        security_opt=["no-new-privileges"],
        network="bridge",
        ports={f"{LISTEN_PORT}/udp": publish, f"{LISTEN_PORT}/tcp": publish},
        volumes={host_dir: {"bind": "/etc/mdd-turn", "mode": "ro"}},
        restart_policy={"Name": "unless-stopped"},
        labels={MANAGED_LABEL: "true", "io.mdd-sim-gateway.component": "turn",
                CONFIG_LABEL: fingerprint},
    )
    media.connect(container, ipv4_address=turn_media_address())
    container.start()
    log.info("started media relay %s on port %s", CONTAINER, PORT)
    return {"running": True, "changed": True}


def status(client=None) -> dict:
    try:
        container = (client or _client()).containers.get(CONTAINER)
        return {"running": container.status == "running"}
    except Exception:  # noqa: BLE001 - absent, Docker down: the relay is not usable either way
        return {"running": False}
