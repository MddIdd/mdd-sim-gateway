"""Safe local administration helpers for the unified WebUI.

Backups stay on the gateway (they contain credentials). Support bundles are downloadable but
strictly redacted and contain only configuration shape, status and bounded log tails.
"""
from __future__ import annotations

import io
import json
import os
from collections import deque
from pathlib import Path
import re
import shutil
import tarfile
import time
import zipfile

import docker
import yaml

from . import config as cfg
from . import store


_SECRET_KEYS = re.compile(
    r"pin|puk|password|secret|token|credential|imsi|iccid|imei|msisdn|eid|"
    r"carrier_identity|gid1|gid2|\bspn\b|subscription|proxy_url|webhook_url|headers?|"
    r"activation|matching_id|confirmation|smdp",
    re.I,
)
_SAFE_DIAGNOSTIC_KEYS = {"imei_valid", "iccid_valid", "imei_source_matches"}
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


def _path_secret(path: tuple[str, ...]) -> bool:
    """Fields whose generic names become private only in a specific document context."""
    if not path:
        return False
    key = path[-1]
    if _SECRET_KEYS.search(key) or key in {"profile_id", "proxy_profile_id"}:
        return True
    if "egress" in path and key in {"node", "pinned_node", "candidates"}:
        return True
    if "network" in path and "addresses" in path and key == "address":
        return True
    if len(path) >= 3 and path[0:2] == ("proxy", "profiles") \
            and key in {"name", "value", "server", "username", "outbound_tag"}:
        return True
    if "feishu" in path and "channels" in path and key in {"name", "url", "secret"}:
        return True
    return False


def redact(value, key: str = "", path: tuple[str, ...] = ()):
    current = path + ((key,) if key else ())
    # These closed-schema booleans describe whether evidence exists without carrying the
    # identity itself. A malformed/non-boolean value remains secret by key name.
    if current and current[-1] in _SAFE_DIAGNOSTIC_KEYS and isinstance(value, bool):
        return value
    if _path_secret(current):
        return "<redacted>" if value not in (None, "", [], {}) else value
    if isinstance(value, dict):
        # Profile ids are operator-controlled labels and can themselves name a provider or
        # account. Keep the number and shape of entries without exporting those labels.
        if current == ("proxy", "profiles"):
            return {f"profile-{index}": redact(v, path=current + ("[]",))
                    for index, v in enumerate(value.values(), 1)}
        return {str(k): redact(v, str(k), current) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, path=current + ("[]",)) for v in value]
    if isinstance(value, str):
        value = _ACTIVATION_CODE.sub("<redacted-activation-code>", value)
        value = _URL.sub("<redacted-url>", value)
        value = _LONG_DIGITS.sub("<redacted-number>", value)
        return _HEX_BLOB.sub("<redacted-secret>", value)
    return value


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.") or "gateway"


def redact_log(text: str) -> str:
    """Redact a plain log file line by line.

    Blanking the whole line is right here: an unlabelled key row carries nothing else, and a
    log line that mentions key material is not worth reconstructing. ``redact_jsonl`` exists
    because that trade is wrong for a structured record.
    """
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


def _redact_record(value, key: str = "", path: tuple[str, ...] = ()):
    """Redact inside a diagnostic record, blanking only the strings that carry key material.

    Same rules as ``redact_log``, applied per string rather than per line. A captured log tail
    is a list of log lines, so each element gets exactly the treatment it would have got in
    its own file, and the registration/SIP/host evidence beside it survives.
    """
    current = path + ((key,) if key else ())
    if current and current[-1] in _SAFE_DIAGNOSTIC_KEYS and isinstance(value, bool):
        return value
    if _path_secret(current):
        return "<redacted>" if value not in (None, "", [], {}) else value
    if isinstance(value, dict):
        return {str(k): _redact_record(v, str(k), current) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_record(item, path=current + ("[]",)) for item in value]
    if isinstance(value, str):
        if _MULTILINE_SECRET.search(value) or _KEY_MATERIAL.search(value):
            return "<redacted cryptographic material>"
        return redact(value, path=current)
    return value


def redact_jsonl(text: str) -> str:
    """Redact a JSON-lines diagnostic file record by record.

    One record is one line, so ``redact_log``'s line rules blank an entire record — and, via
    the two-line lookahead, the two records after it — as soon as any captured log tail inside
    it mentions key material. The engine's own tunnel log says ``received decoded message`` on
    every fragmented exchange, so in practice that wiped every record of the one file written
    specifically to outlive a rebuild loop (see ``engine.capture_diagnostics``). Redact the
    parsed structure instead; a record that will not parse still falls back to the line rules.
    """
    lines = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            lines.append(redact_log(line))
            continue
        lines.append(json.dumps(_redact_record(record), ensure_ascii=False, sort_keys=True))
    return "\n".join(lines)


CALL_EVENTS = ("call_out", "call_result", "ussd")
CALL_EVENT_SCAN_LINES = 20_000
SUPPORT_BUNDLE_MAX_BYTES = 10 * 1024 * 1024
# Leave space for ZIP metadata, the manifest and compression overhead. The final archive is
# still measured and pruned below; this budget keeps that fallback exceptional.
SUPPORT_BUNDLE_CONTENT_BYTES = 8 * 1024 * 1024

_MDD_IMAGE_PREFIXES = ("mdd-sim-gateway/", "ghcr.io/mddidd/mdd-sim-gateway-")
_CURRENT_IMAGE_TAGS = {
    "mdd-sim-gateway/engine:latest",
    "mdd-sim-gateway/control:latest",
    "mdd-sim-gateway/engine-base:trusted",
}


def _managed_image(image) -> bool:
    tags = [str(tag) for tag in (getattr(image, "tags", None) or [])]
    attrs = getattr(image, "attrs", None) or {}
    labels = ((attrs.get("Config") or {}).get("Labels") or {})
    return labels.get("io.mdd-sim-gateway.managed") == "true" or any(
        tag.startswith(_MDD_IMAGE_PREFIXES) for tag in tags)


def _image_layer_bytes(report: dict) -> int:
    usage = report.get("ImageUsage") or {}
    return max(0, int(usage.get("TotalSize") or report.get("LayersSize") or 0))


def prune_old_mdd_images() -> dict:
    """Delete unused MDD history while preserving current/base and every live container.

    This is deliberately separate from automatic one-generation rollback retention: an
    administrator invokes it when free space matters more than one-click rollback. Removing by
    image ID with ``force=True`` drops historical aliases together, but only after the ID has
    been excluded from every container and every stable current/base tag.
    """
    client = docker.from_env(timeout=30)
    try:
        before = _image_layer_bytes(client.df())
        protected_ids = {
            str(container.image.id) for container in client.containers.list(all=True)
            if getattr(container, "image", None) is not None
        }
        images = client.images.list(all=True)
        for image in images:
            if any(tag in _CURRENT_IMAGE_TAGS for tag in (image.tags or [])):
                protected_ids.add(str(image.id))
        candidates = [image for image in images
                      if _managed_image(image) and str(image.id) not in protected_ids]
        removed = 0
        for image in candidates:
            client.images.remove(str(image.id), force=True, noprune=False)
            removed += 1
        after = _image_layer_bytes(client.df())
    except docker.errors.DockerException as exc:
        raise RuntimeError(f"could not remove old MDD images: {exc}") from exc
    finally:
        try:
            client.close()
        except docker.errors.DockerException:
            pass
    return {"ok": True, "removed_images": removed,
            "space_reclaimed_bytes": max(0, before - after)}


def prune_dangling_build_cache() -> dict:
    """Remove only dangling Docker builder records and report the reclaimed bytes.

    Legacy builder records have no MDD ownership labels. This deliberately mirrors
    ``docker builder prune`` without ``--all``: images, containers, volumes, and reusable
    cache records remain untouched.
    """
    client = docker.from_env(timeout=30)
    try:
        # ``all=False`` is Docker's dangling-only builder prune. Unlike image prune, the
        # build/prune API does not accept a ``dangling`` filter (Docker 28 returns HTTP 400).
        result = client.api.prune_builds(all=False) or {}
    except docker.errors.DockerException as exc:
        raise RuntimeError(f"could not prune Docker build cache: {exc}") from exc
    finally:
        try:
            client.close()
        except docker.errors.DockerException:
            pass
    return {"ok": True, "space_reclaimed_bytes": max(
        0, int(result.get("SpaceReclaimed") or 0))}


def call_event_evidence(text: str) -> str:
    """Extract just enough of the engine's call events to check a service-code verdict.

    The UI's verdict for a dialled service code ("the carrier does not support this code",
    "the carrier refused it") is derived from the Q.850 cause on call_result, and that cause
    is recorded ONLY here. Without it a user's report cannot be checked against what the
    carrier actually said — the bundle showed the conclusion but never the evidence.

    The whole file cannot be shipped: it also carries dialled subscriber numbers and message
    bodies, and the key-name redaction rules do not reach them because they sit inside an
    `args` array. So keep the diagnosis and drop the identity — a service code is a carrier's
    own public number and is precisely what has to be diagnosable, whereas a number a user
    dialled is not, and neither is the text of a reply.
    """
    out = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        event = str(record.get("event") or "")
        if event not in CALL_EVENTS:
            continue
        args = [str(a) for a in (record.get("args") or [])]
        # call_result is "<direction> <peer> <dialstatus> <cause>"; the others lead with peer.
        peer_at = 1 if (event == "call_result" and args and args[0] in ("in", "out")) else 0
        if peer_at < len(args) and not any(ch in args[peer_at] for ch in "*#"):
            args[peer_at] = "<number>"
        if event == "ussd" and len(args) > 1:
            # That a reply arrived, and how big it was, answers the question. Its text can
            # carry account details and answers nothing.
            args[1] = f"<{len(args[1])} bytes>"
        safe = {"instance": record.get("instance"), "event": event, "args": args}
        if isinstance(record.get("ts"), (int, float)):
            safe["ts"] = int(record["ts"])
        out.append(json.dumps(safe, ensure_ascii=False, sort_keys=True))
    return "\n".join(out)


def create_local_backup(system_name: str = "gateway") -> dict:
    """Create a root-local recovery archive. It is intentionally not returned over HTTP.

    The message history goes in as a consistent snapshot rather than the live file, together
    with every MMS attachment it refers to; the archive is then checked to hold each of those
    attachments, so restoring it restores whole messages."""
    root = Path(cfg.DATA_DIR).resolve()
    target_dir = root / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"{_safe_name(system_name)}-{stamp}.tar.gz"
    history = Path(store.DB_PATH).resolve()
    snapshot = history.is_relative_to(root) and history.is_file()
    database_name = str(history.relative_to(root)) if snapshot else ""
    skipped = {database_name + suffix for suffix in ("", "-journal", "-wal", "-shm")}
    mms_root = Path(store.mms_dir()).resolve()
    # Uploads of a message still being composed are working copies, not history.
    staging_root = (root / "mms-staging").resolve()
    staging = target_dir / f".staging-{stamp}"
    for stale in target_dir.glob(".staging-*"):
        shutil.rmtree(stale, ignore_errors=True)
    try:
        referenced = []
        if snapshot:
            store.snapshot_history(str(staging / database_name), str(staging / "mms"))
            referenced = _referenced_parts(staging / database_name)
        with tarfile.open(target, "w:gz") as archive:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or target_dir in path.parents \
                        or staging_root in path.parents:
                    continue
                relative = str(path.relative_to(root))
                if snapshot and (relative in skipped or mms_root in path.parents):
                    continue
                archive.add(path, arcname=relative, recursive=False)
            if snapshot:
                archive.add(staging / database_name, arcname=database_name, recursive=False)
                mms_name = str(mms_root.relative_to(root)) if mms_root.is_relative_to(root) \
                    else "mms"
                for path in sorted((staging / "mms").rglob("*")):
                    if path.is_file():
                        archive.add(path, recursive=False, arcname=str(
                            Path(mms_name) / path.relative_to(staging / "mms")))
        if referenced:
            with tarfile.open(target, "r:gz") as archive:
                names = set(archive.getnames())
            mms_name = str(mms_root.relative_to(root)) if mms_root.is_relative_to(root) \
                else "mms"
            absent = [p for p in referenced if f"{mms_name}/{p}" not in names]
            if absent:
                raise RuntimeError(f"the backup is missing {len(absent)} MMS attachment(s), "
                                   f"e.g. {absent[0]}")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    os.chmod(target, 0o600)
    return {"ok": True, "name": target.name, "created_at": int(time.time()),
            "size": target.stat().st_size, "location": "gateway-local"}


def _referenced_parts(database: Path) -> list[str]:
    """"<message id>/<file>" of every attachment the history snapshot refers to and had."""
    import sqlite3
    with sqlite3.connect(database) as check:
        if not check.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                             "AND name='mms_parts'").fetchone():
            return []
        rows = check.execute("SELECT message_id, path FROM mms_parts WHERE path!=''").fetchall()
    staged = database.parent / "mms"
    return [f"{int(m)}/{p}" for m, p in rows if (staged / str(int(m)) / p).is_file()]


def list_local_backups() -> list[dict]:
    root = Path(cfg.DATA_DIR) / "backups"
    result = []
    for path in sorted(root.glob("*.tar.gz"), reverse=True) if root.exists() else []:
        stat = path.stat()
        result.append({"name": path.name, "created_at": int(stat.st_mtime),
                       "size": stat.st_size, "location": "gateway-local"})
    return result[:50]


def delete_local_backup(name: str) -> dict:
    """Delete one named local backup without allowing paths outside the backup directory."""
    name = str(name or "")
    if (Path(name).name != name or not re.fullmatch(r"[A-Za-z0-9_.-]+\.tar\.gz", name)):
        raise ValueError("invalid backup name")
    root = (Path(cfg.DATA_DIR) / "backups").resolve()
    target = root / name
    if not target.is_file():
        raise FileNotFoundError(name)
    target.unlink()
    return {"ok": True, "name": name}


SERVICE_RESTART_SCOPES = ("control", "services", "host")
# The orchestrator polls a few times a minute; a request still sitting here after this long
# means nothing is consuming it, not that it is slow.
_RESTART_PICKUP_SECONDS = 60


def _service_restart_paths() -> tuple[Path, Path]:
    root = Path(cfg.DATA_DIR) / "orchestrator"
    return root / "service-restart-request.json", root / "service-restart-status.json"


def _write_private_json(path: Path, value: dict):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def request_service_restart(scope: str) -> dict:
    """Publish a service-restart request for the root orchestrator to carry out.

    The control plane can restart neither itself nor the host: it is unprivileged, and in two
    of the three scopes it is one of the processes being restarted. So it only states the
    intent, exactly as it does for self-updates, and the orchestrator detaches whatever would
    otherwise kill the process running it.
    """
    if scope not in SERVICE_RESTART_SCOPES:
        return {"ok": False, "error_code": "restart.error.invalid_scope"}
    request_path, status_path = _service_restart_paths()
    now = int(time.time())
    # Reset the visible status first so the previous restart's outcome cannot be read as this
    # one's while the orchestrator is still picking the request up.
    _write_private_json(status_path, {"state": "requested", "scope": scope, "updated_at": now})
    _write_private_json(request_path, {"scope": scope, "requested_at": now})
    return {"ok": True, "scope": scope}


def service_restart_status() -> dict:
    """Progress of the requested restart, as published by the host orchestrator."""
    request_path, status_path = _service_restart_paths()
    status = _read_json(status_path)
    status.setdefault("state", "idle")
    requested_at = int(_read_json(request_path).get("requested_at") or 0) \
        if request_path.exists() else 0
    if requested_at:
        status["requested"] = True
        # An unconsumed request means the orchestrator is not picking work up (stopped, or
        # never installed) — say so instead of leaving the page waiting for a restart that is
        # never going to happen.
        if time.time() - requested_at > _RESTART_PICKUP_SECONDS:
            status["state"] = "stalled"
            status["error_code"] = "restart.error.not_picked_up"
    return status


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
    settings = yaml.safe_dump(redact(cfg.get_settings()), allow_unicode=True)
    status = json.dumps(redact(status_documents), ensure_ascii=False, indent=2)
    # The host orchestrator owns systemd, the USB tree and the bridge children; this process
    # can observe none of them. Every value still passes through the same redactor.
    host = json.dumps(redact(host_diagnostics()), ensure_ascii=False, indent=2)
    fixed = {
        "settings-redacted.yaml": settings,
        "status-redacted.json": status,
        "host-diagnostics-redacted.json": host,
    }
    base = Path(cfg.DATA_DIR) / "instances"
    candidates: list[dict] = []

    def jsonl_times(lines: list[str]) -> tuple[int | None, int | None]:
        values = []
        for line in lines:
            try:
                value = int((json.loads(line) or {}).get("ts") or 0)
            except (ValueError, TypeError, AttributeError):
                continue
            if value:
                values.append(value)
        return (min(values), max(values)) if values else (None, None)

    def add_candidate(path: Path, name: str, source: list[str], eligible: list[str],
                      selected: list[str], text: str, priority: int,
                      raw_line_count: int | None = None) -> None:
        archived = text.splitlines()
        first_ts, last_ts = jsonl_times(archived) if path.suffix == ".jsonl" else (None, None)
        stat = path.stat()
        raw_lines = len(source) if raw_line_count is None else raw_line_count
        coverage = {
            "raw_lines": raw_lines,
            "scanned_lines": len(source),
            "unscanned_lines": max(0, raw_lines - len(source)),
            "eligible_lines": len(eligible),
            "filtered_lines": max(0, len(source) - len(eligible)),
            "included_lines": len(archived),
            "truncated": raw_lines > len(source) or len(selected) < len(eligible),
            "mtime": int(stat.st_mtime),
        }
        if first_ts is not None:
            coverage.update({"first_ts": first_ts, "last_ts": last_ts})
        candidates.append({"name": name, "text": text, "priority": priority,
                           "coverage": coverage})

    # Events are filtered rather than generically exported: subscriber calls and message
    # bodies never enter the candidate set at all.
    for path in sorted(base.glob("*/logs/events.jsonl")):
        try:
            tail: deque[str] = deque(maxlen=CALL_EVENT_SCAN_LINES)
            raw_line_count = 0
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    raw_line_count += 1
                    tail.append(line.rstrip("\r\n"))
            source = list(tail)
            eligible = call_event_evidence("\n".join(source)).splitlines()
            selected = eligible[-log_lines:]
            if selected:
                evidence = "\n".join(selected)
                add_candidate(path, f"logs/{path.parent.parent.name}-call-events.jsonl",
                              source, eligible, selected, evidence, 20, raw_line_count)
        except OSError:
            continue

    # Explicit allow-list: voicemail recordings and every unknown future file stay excluded.
    paths = [*base.glob("*/run/*.log"), *base.glob("*/logs/diagnostics.jsonl"),
             *base.glob("*/logs/lifecycle.jsonl"),
             *base.glob("*/logs/ike/charon-*.log")]
    for path in sorted(paths):
        try:
            source = path.read_text(errors="replace").splitlines()
            if path.parent.name == "ike" and len(source) > log_lines:
                head = max(1, log_lines // 5)
                selected = source[:head] + source[-(log_lines - head):]
            else:
                selected = source[-log_lines:]
            joined = "\n".join(selected)
            text = redact_jsonl(joined) if path.suffix == ".jsonl" else redact_log(joined)
            iid = path.parents[2].name if path.parent.name == "ike" else path.parent.parent.name
            priority = 50 if path.name == "lifecycle.jsonl" else 40 \
                if path.name == "diagnostics.jsonl" else 10
            add_candidate(path, f"logs/{iid}-{path.name}", source, source, selected,
                          text, priority)
        except OSError:
            continue

    def mark_omitted(coverage: dict) -> None:
        coverage.update({"included_lines": 0, "included_bytes": 0, "omitted": True})

    files = {}
    included = dict(fixed)
    used = sum(len(value.encode("utf-8")) for value in fixed.values())
    for candidate in sorted(candidates, key=lambda item: (-item["priority"], item["name"])):
        encoded = candidate["text"].encode("utf-8")
        coverage = {**candidate["coverage"], "included_bytes": len(encoded)}
        if used + len(encoded) <= SUPPORT_BUNDLE_CONTENT_BYTES:
            included[candidate["name"]] = candidate["text"]
            used += len(encoded)
            coverage["omitted"] = False
        else:
            mark_omitted(coverage)
        files[candidate["name"]] = coverage

    def build() -> bytes:
        manifest = {
            "created_at": int(time.time()), "redacted": True,
            "contains_credentials": False, "review_before_sharing": True,
            "log_lines_per_file": log_lines,
            "max_archive_bytes": SUPPORT_BUNDLE_MAX_BYTES,
            "files": files,
        }
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, text in included.items():
                archive.writestr(name, text)
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        return output.getvalue()

    content = build()
    # Compression normally makes the 8 MiB raw budget much smaller. If an unusually
    # incompressible bundle exceeds the public upload contract, inspect the already-built ZIP
    # once, choose enough low-priority entries to cover the exact compressed deficit, then
    # rebuild a single time. Recompressing the whole archive after every removed file was
    # quadratic on a Raspberry Pi.
    removable = sorted(
        (item for item in candidates if item["name"] in included),
        key=lambda item: (item["priority"], item["name"]))
    if len(content) > SUPPORT_BUNDLE_MAX_BYTES and removable:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            compressed = {info.filename: info.compress_size + 76 + 2 * len(info.filename)
                          for info in archive.infolist()}
        deficit = len(content) - SUPPORT_BUNDLE_MAX_BYTES + 1024
        reclaimed = 0
        for item in removable:
            included.pop(item["name"], None)
            mark_omitted(files[item["name"]])
            reclaimed += compressed.get(item["name"], 0)
            if reclaimed >= deficit:
                break
        content = build()
    if len(content) > SUPPORT_BUNDLE_MAX_BYTES:
        raise RuntimeError("redacted support bundle exceeds its 10 MiB safety budget")
    return content
