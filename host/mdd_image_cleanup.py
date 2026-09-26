#!/usr/bin/env python3
"""Retain only the current and one rollback generation of MDD Docker images."""

from __future__ import annotations

import argparse
import re
import subprocess


MANAGED_VERSION_REPOSITORIES = {
    "mdd-sim-gateway/control",
    "ghcr.io/mddidd/mdd-sim-gateway-engine",
    "ghcr.io/mddidd/mdd-sim-gateway-relay",
}
MANAGED_LABEL = "io.mdd-sim-gateway.managed=true"
VERSION_TAG = re.compile(r"v\d+(?:\.\d+)*(?:-[0-9A-Za-z.]+)?\Z")
VERSION = re.compile(r"\d+(?:\.\d+)*(?:-[0-9A-Za-z.]+)?\Z")


def _managed_version_ref(reference: str) -> tuple[str, str] | None:
    """Return ``(repository, tag)`` for an exact MDD release-image reference."""
    repository, separator, tag = reference.rpartition(":")
    if not separator or repository not in MANAGED_VERSION_REPOSITORIES:
        return None
    if not VERSION_TAG.fullmatch(tag):
        return None
    return repository, tag


def prune_superseded_images(version: str, *, prune_build_cache: bool = False) -> bool:
    """Drop obsolete MDD version tags, then remove images left dangling by the rotation.

    The stable aliases are deliberately outside ``MANAGED_VERSION_REPOSITORIES`` or do not use
    version tags: ``control:latest``, ``control:previous``, ``engine:latest``,
    ``engine:previous`` and ``engine-base:trusted`` therefore survive.  Docker also refuses to
    delete an image still referenced by any container.  Removing only old release tags makes a
    generation older than ``:previous`` eligible for the final, narrowly scoped dangling prune.
    """
    if not VERSION.fullmatch(version):
        return False

    listed = subprocess.run(
        ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if listed.returncode:
        return False

    current_tag = f"v{version}"
    success = True
    stale_refs = set()
    for raw_reference in listed.stdout.splitlines():
        reference = raw_reference.strip()
        parsed = _managed_version_ref(reference)
        if parsed and parsed[1] != current_tag:
            stale_refs.add(reference)

    for reference in sorted(stale_refs):
        removed = subprocess.run(
            ["docker", "image", "rm", reference],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        success = removed.returncode == 0 and success

    pruned = subprocess.run(
        ["docker", "image", "prune", "--force", "--filter", f"label={MANAGED_LABEL}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    success = pruned.returncode == 0 and success
    if prune_build_cache:
        # Legacy installs used Docker's shared default builder, whose records carry no project
        # label. Docker cannot retrospectively identify only MDD records. The conservative prune
        # (without --all) removes dangling build cache only; it never removes images, containers,
        # volumes, or cache records still in use.
        cache_pruned = subprocess.run(
            ["docker", "builder", "prune", "--force"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        success = cache_pruned.returncode == 0 and success
    return success


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--prune-build-cache", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if prune_superseded_images(
        args.version, prune_build_cache=args.prune_build_cache) else 1)


if __name__ == "__main__":
    main()
