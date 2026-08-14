"""Safe local administration helpers for the unified WebUI.

Backups stay on the gateway (they contain credentials). Support bundles are downloadable but
strictly redacted and contain only configuration shape, status and bounded log tails.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import tarfile
import time
import zipfile

import yaml

from . import config as cfg


_SECRET_KEYS = re.compile(
    r"pin|puk|password|secret|token|credential|imsi|iccid|imei|msisdn|eid|"
    r"carrier_identity|gid1|gid2|\bspn\b|subscription|proxy_url|webhook_url|headers?|"
    r"activation|matching_id|confirmation|smdp",
    re.I,
)
_LONG_DIGITS = re.compile(r"(?<!\d)\+?\d{10,40}(?!\d)")
_URL = re.compile(r"\b(?:https?|socks5h?)://[^\s\"'<>]+", re.I)
_ACTIVATION_CODE = re.compile(r"\bLPA:1\$[^\s\"'<>]+", re.I)
_HEX_BLOB = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{24,}(?![0-9A-Fa-f])")
_KEY_MATERIAL = re.compile(
    r"(?:\b(?:CK|IK|MK|XKEY|KENCR|KAUT|MSK|EMSK|SKEYSEED|SK_[A-Z_]+|"
    r"NONCE|RAND|AUTN|AUTS|RES)\b\s*(?::|=|\s)\s*(?:0x)?[0-9A-Fa-f]{8,}\b|"
    r"\bDIFFIE[- ]HELLMAN KEY\b)",
    re.I,
)
_MULTILINE_SECRET = re.compile(
    r"(?:IKEv2 DECRYPTION TABLE|ESP SA INFO|received decoded message|DECRYPTED DATA)", re.I
)


def redact(value, key: str = ""):
    if _SECRET_KEYS.search(key):
        return "<redacted>" if value not in (None, "", [], {}) else value
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if isinstance(value, str):
        value = _ACTIVATION_CODE.sub("<redacted-activation-code>", value)
        value = _URL.sub("<redacted-url>", value)
        value = _LONG_DIGITS.sub("<redacted-number>", value)
        return _HEX_BLOB.sub("<redacted-secret>", value)
    return value


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.") or "gateway"


def redact_log(text: str) -> str:
    lines = []
    redact_following = 0
    for line in text.splitlines():
        if redact_following:
            lines.append("<redacted cryptographic material>")
            redact_following -= 1
        elif _MULTILINE_SECRET.search(line):
            lines.append("<redacted cryptographic material>")
            # Wireshark IKE/ESP tables emit two unlabelled key rows after the heading.
            redact_following = 2
        elif _KEY_MATERIAL.search(line):
            lines.append("<redacted cryptographic material>")
        else:
            lines.append(str(redact(line)))
    return "\n".join(lines)


def create_local_backup(system_name: str = "gateway") -> dict:
    """Create a root-local recovery archive. It is intentionally not returned over HTTP."""
    root = Path(cfg.DATA_DIR).resolve()
    target_dir = root / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"{_safe_name(system_name)}-{stamp}.tar.gz"
    with tarfile.open(target, "w:gz") as archive:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or target_dir in path.parents:
                continue
            archive.add(path, arcname=str(path.relative_to(root)), recursive=False)
    os.chmod(target, 0o600)
    return {"ok": True, "name": target.name, "created_at": int(time.time()),
            "size": target.stat().st_size, "location": "gateway-local"}


def list_local_backups() -> list[dict]:
    root = Path(cfg.DATA_DIR) / "backups"
    result = []
    for path in sorted(root.glob("*.tar.gz"), reverse=True) if root.exists() else []:
        stat = path.stat()
        result.append({"name": path.name, "created_at": int(stat.st_mtime),
                       "size": stat.st_size, "location": "gateway-local"})
    return result[:50]


def host_diagnostics() -> dict:
    """Read the host orchestrator's published view, or say why it is absent.

    An empty section would read as "nothing wrong on the host"; a stopped or outdated
    orchestrator is itself a finding, so the absence is reported rather than omitted.
    """
    path = Path(cfg.DATA_DIR) / "orchestrator" / "host-diagnostics.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False,
                "note": "the host orchestrator has not published diagnostics; it may be "
                        "stopped, or older than the release that introduced this file"}
    if not isinstance(document, dict):
        return {"available": False, "note": "host diagnostics document is malformed"}
    return {"available": True, **document}


def support_bundle(status_documents: dict, log_lines: int = 500) -> bytes:
    log_lines = max(50, min(2000, int(log_lines)))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        settings = redact(cfg.get_settings())
        archive.writestr("settings-redacted.yaml", yaml.safe_dump(settings, allow_unicode=True))
        archive.writestr("status-redacted.json", json.dumps(
            redact(status_documents), ensure_ascii=False, indent=2))
        # The host orchestrator owns systemd, the USB tree and the bridge children; this
        # process runs in a container that can observe none of them. Without its published
        # view a modem fault is only diagnosable by asking the operator to run commands.
        archive.writestr("host-diagnostics-redacted.json", json.dumps(
            redact(host_diagnostics()), ensure_ascii=False, indent=2))
        base = Path(cfg.DATA_DIR) / "instances"
        # Include the current logs, compact rebuild snapshots and retained IKE segments. Every
        # source goes through the same redaction and contributes only its configured tail.
        paths = [*base.glob("*/run/*.log"), *base.glob("*/logs/diagnostics.jsonl"),
                 *base.glob("*/logs/ike/charon-*.log")]
        for path in sorted(paths):
            try:
                lines = path.read_text(errors="replace").splitlines()[-log_lines:]
            except OSError:
                continue
            text = redact_log("\n".join(lines))
            iid = path.parents[2].name if path.parent.name == "ike" else path.parent.parent.name
            archive.writestr(f"logs/{iid}-{path.name}", text)
        archive.writestr("manifest.json", json.dumps({
            "created_at": int(time.time()), "redacted": True,
            "contains_credentials": False, "review_before_sharing": True,
            "log_lines_per_file": log_lines,
        }, indent=2))
    return output.getvalue()
