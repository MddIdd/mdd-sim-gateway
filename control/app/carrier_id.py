"""Offline carrier identification using Android's AOSP Carrier ID table.

Only the resolved, non-sensitive carrier description is returned to API callers.  SIM
matching attributes (IMSI/ICCID prefixes, SPN and GIDs) stay in the local control plane.
The vendored textproto is parsed directly so the gateway does not need protobuf at runtime.
"""
from __future__ import annotations

from functools import lru_cache
import json
import os
import re
from typing import Any


DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "carrier_list.textpb")
_FIELD = re.compile(r"^([a-z_][a-z_0-9]*):\s*(.+)$")

# Android CarrierResolver uses descending bit scores.  Reusing those weights makes a rule
# with an IMSI prefix beat any combination of less-specific attributes, and so on.
_SCORES = {
    "mccmnc_tuple": 1 << 8,
    "imsi_prefix_xpattern": 1 << 7,
    "iccid_prefix": 1 << 6,
    "gid1": 1 << 5,
    "gid2": 1 << 4,
    "spn": 1 << 1,
    "preferred_apn": 1,
}


def _value(text: str) -> str | int:
    text = text.strip()
    if text.startswith('"'):
        return json.loads(text)
    return int(text)


@lru_cache(maxsize=1)
def _database() -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], int]:
    carriers: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    attribute: dict[str, list[str]] | None = None
    version = 0
    try:
        handle = open(DATA_PATH, encoding="utf-8")
    except OSError:
        return carriers, {}, version
    with handle:
        for original in handle:
            line = original.strip()
            if not line:
                continue
            if line == "carrier_id {":
                current = {"attributes": []}
                continue
            if line == "carrier_attribute {" and current is not None:
                attribute = {}
                continue
            if line == "}":
                if attribute is not None and current is not None:
                    current["attributes"].append(attribute)
                    attribute = None
                elif current is not None:
                    carriers.append(current)
                    current = None
                continue
            match = _FIELD.match(line)
            if not match:
                continue
            key, raw = match.groups()
            value = _value(raw)
            if current is None:
                if key == "version":
                    version = int(value)
            elif attribute is not None:
                attribute.setdefault(key, []).append(str(value))
            else:
                current[key] = value
    by_id = {int(item["canonical_id"]): item for item in carriers
             if item.get("canonical_id") is not None}
    return carriers, by_id, version


def database_version() -> int:
    return _database()[2]


def _plmns(identity: dict) -> list[str]:
    mcc = re.sub(r"\D", "", str(identity.get("mcc") or ""))
    mnc = re.sub(r"\D", "", str(identity.get("mnc") or ""))
    if len(mcc) != 3 or not mnc:
        return []
    values = [mcc + mnc]
    # Older MDD WebUI versions padded every MNC to three digits.  Prefer the exact value,
    # then recover a two-digit MNC only if the table has no exact record.
    if len(mnc) == 3 and mnc.startswith("0"):
        values.append(mcc + mnc[1:])
    return list(dict.fromkeys(values))


def _imsi_matches(value: str | None, patterns: list[str]) -> bool:
    if not value:
        return False
    for pattern in patterns:
        if len(value) < len(pattern):
            continue
        if all(expected in "xX" or expected == actual
               for expected, actual in zip(pattern, value)):
            return True
    return False


def _prefix_matches(value: str | None, prefixes: list[str], *, fold: bool = False) -> bool:
    if value is None:
        return False
    left = value.casefold() if fold else value
    return any(left.startswith(prefix.casefold() if fold else prefix) for prefix in prefixes)


def _attribute_score(attribute: dict[str, list[str]], identity: dict,
                     plmns: list[str]) -> tuple[int, str, list[str]] | None:
    offered = attribute.get("mccmnc_tuple") or []
    matched_plmn = next((value for value in plmns if value in offered), "")
    if not matched_plmn:
        return None
    score = _SCORES["mccmnc_tuple"]
    matched = ["mccmnc"]
    checks = (
        ("imsi_prefix_xpattern", "imsi", _imsi_matches),
        ("iccid_prefix", "iccid", lambda value, rules: _prefix_matches(value, rules)),
        ("gid1", "gid1", lambda value, rules: _prefix_matches(value, rules, fold=True)),
        ("gid2", "gid2", lambda value, rules: _prefix_matches(value, rules, fold=True)),
        ("spn", "spn", lambda value, rules: value is not None and
         any(value.casefold() == rule.casefold() for rule in rules)),
        ("preferred_apn", "apn", lambda value, rules: value is not None and
         any(value.casefold() == rule.casefold() for rule in rules)),
    )
    carrier_identity = identity.get("carrier_identity") or {}
    for rule_key, identity_key, predicate in checks:
        rules = attribute.get(rule_key) or []
        if not rules:
            continue
        value = (carrier_identity.get(identity_key) if identity_key in {"spn", "gid1", "gid2"}
                 else identity.get(identity_key))
        if not predicate(None if value is None else str(value), rules):
            return None
        score += _SCORES[rule_key]
        matched.append(identity_key)
    # These rules require UICC carrier-privilege certificates, which MDD deliberately does
    # not expose.  Falling back to the parent network is safer than a false MVNO label.
    if attribute.get("privilege_access_rule"):
        return None
    return score, matched_plmn, matched


def lookup(identity: dict | None) -> dict | None:
    """Return a safe carrier description for one SIM, or None when MCC/MNC is unknown."""
    identity = identity or {}
    plmns = _plmns(identity)
    if not plmns:
        return None
    carriers, by_id, version = _database()
    known_plmns = {value for carrier in carriers
                   for attribute in carrier.get("attributes") or []
                   for value in attribute.get("mccmnc_tuple") or []}
    selected_plmn = next((value for value in plmns if value in known_plmns), plmns[0])
    plmns = [selected_plmn]
    best: tuple[int, int, dict, str, list[str]] | None = None
    for order, carrier in enumerate(carriers):
        for attribute in carrier.get("attributes") or []:
            result = _attribute_score(attribute, identity, plmns)
            if result is None:
                continue
            score, matched_plmn, matched = result
            candidate = (score, -order, carrier, matched_plmn, matched)
            if best is None or candidate[:2] > best[:2]:
                best = candidate
    if best is None:
        # Some AOSP parent-network rules also carry an expected (often blank) SPN.  With an
        # older saved MDD line we may not have read SPN yet, but can still provide a conservative
        # host-network fallback: prefer a parentless record with the fewest extra conditions.
        fallbacks = []
        for order, carrier in enumerate(carriers):
            if carrier.get("parent_canonical_id"):
                continue
            for attribute in carrier.get("attributes") or []:
                matched_plmn = next((value for value in plmns
                                     if value in (attribute.get("mccmnc_tuple") or [])), "")
                if matched_plmn:
                    conditions = sum(bool(value) for key, value in attribute.items()
                                     if key != "mccmnc_tuple")
                    fallbacks.append((conditions, order, carrier, matched_plmn))
        if not fallbacks:
            mcc, mnc = plmns[0][:3], plmns[0][3:]
            return {"name": "", "home_network": "", "plmn": f"{mcc}-{mnc}",
                    "match_source": "mccmnc", "database": "none",
                    "database_version": version}
        _conditions, _order, carrier, matched_plmn = min(fallbacks, key=lambda item: item[:2])
        name = str(carrier.get("carrier_name") or "")
        return {"name": name, "home_network": name,
                "plmn": f"{matched_plmn[:3]}-{matched_plmn[3:]}",
                "match_source": "mccmnc", "specific": False,
                "database": "aosp", "database_version": version}
    score, _order, carrier, matched_plmn, matched = best
    parent_id = int(carrier.get("parent_canonical_id") or 0)
    parent = by_id.get(parent_id)
    if parent is None:
        # Many MVNO records in the public table do not carry parent_canonical_id.  A plain
        # MCC/MNC-only rule for the same PLMN is the conservative host network.
        for candidate in carriers:
            if candidate is carrier:
                continue
            if any(matched_plmn in (attribute.get("mccmnc_tuple") or [])
                   and set(attribute) == {"mccmnc_tuple"}
                   for attribute in candidate.get("attributes") or []):
                parent = candidate
                break
    name = str(carrier.get("carrier_name") or "")
    home_network = str((parent or {}).get("carrier_name") or name)
    return {
        "name": name,
        "home_network": home_network,
        "plmn": f"{matched_plmn[:3]}-{matched_plmn[3:]}",
        "match_source": "+".join(matched),
        "specific": score > _SCORES["mccmnc_tuple"],
        "database": "aosp",
        "database_version": version,
    }
