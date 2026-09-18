#!/usr/bin/env python3
"""Check the engines' Docker subnets before install.sh creates them.

Engines sit on two fixed subnets: mdd-engine (eth0) and the internal mdd-media (eth1) the TURN
relay reaches them on. A subnet that overlaps a LAN, a VPN, another Docker network or the
orchestrator's country tunnels would silently misroute traffic, so installation stops and
says which one instead.

Exit status: 0 = usable, 1 = conflict or invalid (message on stderr), 2 = could not inspect.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
import sys

# The orchestrator numbers its country TUNs 172.29.{20 + n}.1/30 (mdd_orchestrator.py), so the
# rest of 172.29.0.0/16 from .20.0 up is spoken for even while no tunnel is up yet.
COUNTRY_TUNNELS = list(ipaddress.summarize_address_range(
    ipaddress.ip_address("172.29.20.0"), ipaddress.ip_address("172.29.255.255")))
# Engines take host .10 to .29 of each subnet (control/app/turn.py FIRST_LINE_HOST, LINE_SLOTS).
MIN_PREFIX = 27


def conflicts(name: str, subnet: str, docker_networks: dict[str, list[str]],
              routes: list[tuple[str, str]], own: set[str], own_interfaces: set[str],
              other_own: str | None = None) -> list[str]:
    """Why ``subnet`` cannot be used for the MDD network ``name`` (empty when it can).

    docker_networks maps network name -> subnets; routes are (destination, device) pairs.
    ``own`` names the MDD networks (whose existing subnets are expected), ``own_interfaces``
    the bridges carrying them, and ``other_own`` the other MDD subnet, which must not overlap."""
    try:
        network = ipaddress.ip_network(subnet, strict=True)
    except ValueError as exc:
        return [f"{subnet} is not a valid IPv4 subnet ({exc})"]
    if network.version != 4:
        return [f"{subnet} must be an IPv4 subnet"]
    problems = []
    if network.prefixlen > MIN_PREFIX:
        problems.append(f"{subnet} is too small: use /{MIN_PREFIX} or larger (engines take .10-.29)")
    if other_own:
        other = ipaddress.ip_network(other_own, strict=False)
        if network.overlaps(other):
            problems.append(f"{subnet} overlaps the other MDD network {other}")
    for reserved in COUNTRY_TUNNELS:
        if network.overlaps(reserved):
            problems.append(f"{subnet} overlaps the country tunnel range 172.29.20.0-172.29.255.255")
            break
    for other_name, subnets in sorted(docker_networks.items()):
        if other_name in own:
            continue
        for other_subnet in subnets:
            try:
                other = ipaddress.ip_network(other_subnet, strict=False)
            except ValueError:
                continue
            if other.version == 4 and network.overlaps(other):
                problems.append(f"{subnet} overlaps Docker network '{other_name}' ({other})")
    for destination, device in routes:
        if device in own_interfaces or destination == "default":
            continue
        try:
            other = ipaddress.ip_network(destination, strict=False)
        except ValueError:
            continue
        if network.overlaps(other):
            problems.append(f"{subnet} overlaps the host route {other} on {device}")
    return problems


def _docker_networks() -> tuple[dict[str, list[str]], dict[str, str]]:
    ids = subprocess.run(["docker", "network", "ls", "-q"], check=True, text=True,
                         stdout=subprocess.PIPE).stdout.split()
    if not ids:
        return {}, {}
    inspected = json.loads(subprocess.run(["docker", "network", "inspect", *ids], check=True,
                                          text=True, stdout=subprocess.PIPE).stdout)
    subnets, bridges = {}, {}
    for network in inspected:
        name = network.get("Name", "")
        subnets[name] = [c.get("Subnet", "") for c in ((network.get("IPAM") or {}).get("Config") or [])]
        options = network.get("Options") or {}
        bridges[name] = options.get("com.docker.network.bridge.name") or f"br-{network.get('Id', '')[:12]}"
    return subnets, bridges


def _routes() -> list[tuple[str, str]]:
    out = subprocess.run(["ip", "-j", "-4", "route", "show"], check=True, text=True,
                         stdout=subprocess.PIPE).stdout
    return [(r.get("dst", ""), r.get("dev", "")) for r in json.loads(out or "[]")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine-name", required=True)
    parser.add_argument("--engine-subnet", required=True)
    parser.add_argument("--media-name", required=True)
    parser.add_argument("--media-subnet", required=True)
    args = parser.parse_args()
    try:
        docker_networks, bridges = _docker_networks()
        routes = _routes()
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"could not inspect Docker networks or host routes: {exc}", file=sys.stderr)
        return 2
    own = {args.engine_name, args.media_name}
    own_interfaces = {bridges[name] for name in own if name in bridges}
    problems = []
    for name, subnet, other in ((args.engine_name, args.engine_subnet, args.media_subnet),
                                (args.media_name, args.media_subnet, None)):
        problems += conflicts(name, subnet, docker_networks, routes, own, own_interfaces, other)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
