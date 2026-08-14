"""Release checker + one-click update request publisher.

The control plane never applies files itself: ``request_apply`` publishes a request document
that the root host orchestrator picks up and hands to a detached ``systemd-run`` unit
(``host/mdd_update.py``), which downloads the tagged release, overlays the checkout and runs
``install.sh reload``. Progress comes back through ``update-status.json``.
"""
from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import urlsplit

import requests

from .version import VERSION

DEFAULT_REPOSITORY = "MddIdd/mdd-sim-gateway"
_cache: tuple[float, dict] | None = None


class UpdateNetworkError(RuntimeError):
    pass


def validate_network_settings(value: dict | None) -> dict:
    """Validate and normalize the persisted update networking selection."""
    value = value or {}
    mode = str(value.get("proxy_mode") or "direct").strip().lower()
    if mode not in {"direct", "manual", "country"}:
        raise UpdateNetworkError("update proxy mode must be direct, manual or country")
    result = {"proxy_mode": mode, "proxy_url": "", "proxy_country": ""}
    if mode == "manual":
        proxy = str(value.get("proxy_url") or "").strip()
        parsed = urlsplit(proxy)
        if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"} \
                or not parsed.hostname or any(ch in proxy for ch in "\r\n"):
            raise UpdateNetworkError("update proxy must be an HTTP(S) or SOCKS5 URL")
        result["proxy_url"] = proxy
    elif mode == "country":
        country = str(value.get("proxy_country") or "").strip().lower()
        if not re.fullmatch(r"[a-z]{2}", country):
            raise UpdateNetworkError("select a country exit for software updates")
        result["proxy_country"] = country
    return result


def _network_selection() -> dict:
    from . import config as cfg
    return validate_network_settings(cfg.get_settings().get("updates"))


def _proxy_url(selection: dict) -> str:
    mode = selection["proxy_mode"]
    if mode == "direct":
        return ""
    if mode == "manual":
        return selection["proxy_url"]
    from . import egress
    state = (egress.status().get("exits") or {}).get(selection["proxy_country"]) or {}
    try:
        port = int(state.get("proxy_port") or 0)
    except (TypeError, ValueError):
        port = 0
    host = str(state.get("proxy_host") or "").strip()
    if not state.get("ready") or not host or not 1 <= port <= 65535:
        raise UpdateNetworkError("selected country exit is not ready")
    return f"socks5h://{host}:{port}"


def repository() -> str:
    return os.environ.get("MDD_UPDATE_REPOSITORY", DEFAULT_REPOSITORY).strip()


def _version_tuple(value: str) -> tuple[int, ...]:
    core = str(value).strip().removeprefix("v").split("-", 1)[0]
    try:
        return tuple(int(part) for part in core.split("."))
    except ValueError:
        return (0,)


def _stargazers(session, headers: dict, repository_name: str) -> int | None:
    """Star count for the console's repository link, or None if it cannot be read.

    Deliberately folded into the release check rather than served from the status endpoint:
    that endpoint answers every page load and must not wait on GitHub. Failure is silent —
    a decorative count must never turn a working update check into a visible error.
    """
    try:
        response = session.get(f"https://api.github.com/repos/{repository_name}",
                               headers=headers, timeout=8)
        response.raise_for_status()
        count = int(response.json().get("stargazers_count"))
    except (requests.RequestException, OSError, ValueError, TypeError):
        return None
    return count if count >= 0 else None


def check(force: bool = False) -> dict:
    global _cache
    now = time.time()
    if not force and _cache and now - _cache[0] < 300:
        return dict(_cache[1])
    repository_name = repository()
    url = f"https://api.github.com/repos/{repository_name}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"mdd-sim-gateway/{VERSION}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    result = {"ok": False, "current": VERSION, "repository": repository_name,
              "update_available": False, "checked_at": int(now)}
    try:
        selection = _network_selection()
        proxy = _proxy_url(selection)
        session = requests.Session()
        session.trust_env = False  # "direct" must really mean direct, regardless of service env.
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
        response = session.get(url, headers=headers, timeout=12)
        response.raise_for_status()
        payload = response.json()
        latest = str(payload.get("tag_name") or "").removeprefix("v")
        result.update({
            "ok": bool(latest),
            "latest": latest,
            "update_available": _version_tuple(latest) > _version_tuple(VERSION),
            "release_url": str(payload.get("html_url") or ""),
            "published_at": str(payload.get("published_at") or ""),
            "notes": str(payload.get("body") or "")[:4000],
        })
        result["stars"] = _stargazers(session, headers, repository_name)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 0
        if code in {401, 404}:
            # Release checks are intentionally unauthenticated and never send a GitHub token.
            result["error"] = "No release is available from the configured repository"
            result["error_code"] = "update.error.no_release"
        elif code == 403:
            result["error"] = "GitHub update check was rate-limited"
            result["error_code"] = "update.error.rate_limited"
        else:
            result["error"] = f"GitHub returned HTTP {code}"
            result["error_code"] = "update.error.github"
    except UpdateNetworkError as exc:
        result["error"] = str(exc)
        result["error_code"] = "update.error.proxy"
    except (requests.RequestException, OSError, ValueError, TypeError) as exc:
        result["error"] = f"Update service unavailable: {type(exc).__name__}"
        result["error_code"] = "update.error.unavailable"
    _cache = (now, result)
    return dict(result)


def _apply_paths() -> tuple[str, str]:
    from . import config as cfg
    root = os.path.join(cfg.DATA_DIR, "orchestrator")
    return os.path.join(root, "update-request.json"), os.path.join(root, "update-status.json")


def _write_private_json(path: str, value: dict):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def apply_status() -> dict:
    """Current self-update progress as published by the host-side updater."""
    request_path, status_path = _apply_paths()
    try:
        with open(status_path, encoding="utf-8") as handle:
            status = json.load(handle)
        if not isinstance(status, dict):
            status = {}
    except (OSError, ValueError):
        status = {}
    status.setdefault("state", "idle")
    try:
        with open(request_path, encoding="utf-8") as handle:
            requested_at = int((json.load(handle) or {}).get("requested_at") or 0)
        status["requested"] = True
        # An unconsumed request means the orchestrator is not picking work up (stopped or
        # never installed) — surface that instead of letting the UI spin forever.
        if time.time() - requested_at > 120:
            status["state"] = "stalled"
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return status


def request_apply() -> dict:
    """Publish a one-click update request for the host orchestrator."""
    status = apply_status()
    if status.get("state") == "running" and time.time() - int(status.get("updated_at") or 0) < 3600:
        return {"ok": False, "error": "An update is already in progress",
                "error_code": "update.error.in_progress", "status": status}
    info = check(True)
    if not info.get("update_available"):
        return {"ok": False, "error": info.get("error") or "No update is available",
                "error_code": info.get("error_code") or "update.error.not_available"}
    request_path, status_path = _apply_paths()
    now = int(time.time())
    network = _network_selection()
    # Reset the visible status first so a stale success/failure from a previous run cannot be
    # mistaken for this run's outcome while the orchestrator picks the request up.
    _write_private_json(status_path, {"state": "running", "phase": "requested",
                                      "target": info["latest"], "updated_at": now})
    _write_private_json(request_path, {"version": info["latest"], "repository": repository(),
                                       "requested_at": now,
                                       "network": network})
    return {"ok": True, "version": info["latest"]}
