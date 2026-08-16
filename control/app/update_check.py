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
from urllib.parse import quote

import requests

from .version import VERSION

DEFAULT_REPOSITORY = "MddIdd/mdd-sim-gateway"
_cache: tuple[float, dict] | None = None
_stars_cache: int | None = None


class UpdateNetworkError(RuntimeError):
    pass


def validate_network_settings(value: dict | None) -> dict:
    """Validate and normalize the persisted update networking selection."""
    value = value or {}
    mode = str(value.get("proxy_mode") or "auto").strip().lower()
    if mode in {"manual", "country"}:
        mode = "auto"
    if mode not in {"auto", "direct", "library"}:
        raise UpdateNetworkError("update proxy mode must be auto, direct or library")
    result = {"proxy_mode": mode, "proxy_profile_id": ""}
    if mode == "library":
        profile_id = str(value.get("proxy_profile_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", profile_id):
            raise UpdateNetworkError("select a proxy from the proxy library for software updates")
        result["proxy_profile_id"] = profile_id
    return result


def _network_selection() -> dict:
    from . import config as cfg
    settings = cfg.get_settings()
    selection = validate_network_settings(settings.get("updates"))
    if selection["proxy_mode"] == "library":
        profiles = (settings.get("proxy") or {}).get("profiles") or {}
        if selection["proxy_profile_id"] not in profiles:
            raise UpdateNetworkError("selected update proxy is no longer in the proxy library")
    return selection


def _network_candidates() -> list[dict]:
    selection = _network_selection()
    if selection["proxy_mode"] != "auto":
        return [selection]
    from . import config as cfg
    profiles = ((cfg.get_settings().get("proxy") or {}).get("profiles") or {})
    return [{"proxy_mode": "direct", "proxy_profile_id": ""}] + [
        {"proxy_mode": "library", "proxy_profile_id": str(profile_id)}
        for profile_id in profiles
        if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", str(profile_id))
    ]


def _session(selection: dict) -> requests.Session:
    proxy = _proxy_url(selection)
    session = requests.Session()
    session.trust_env = False
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def _socks5_profile_url(profile: dict) -> str:
    host = str(profile.get("server") or "").strip()
    try:
        port = int(profile.get("port") or 1080)
    except (TypeError, ValueError):
        port = 0
    if not host or not 1 <= port <= 65535 or any(ch in host for ch in "\r\n/@"):
        raise UpdateNetworkError("selected SOCKS5 proxy is invalid")
    username = str(profile.get("username") or "")
    password = str(profile.get("password") or "")
    auth = f"{quote(username, safe='')}:{quote(password, safe='')}@" \
        if username or password else ""
    return f"socks5h://{auth}{host}:{port}"


def _proxy_url(selection: dict) -> str:
    mode = selection["proxy_mode"]
    if mode == "direct":
        return ""
    from . import config as cfg, egress
    settings = cfg.get_settings()
    profile_id = selection["proxy_profile_id"]
    profile = ((settings.get("proxy") or {}).get("profiles") or {}).get(profile_id) or {}
    if profile.get("type") == "socks5":
        return _socks5_profile_url(profile)
    exits = (settings.get("proxy") or {}).get("exits") or {}
    live = egress.status().get("exits") or {}
    candidates = [live.get(country) or {} for country, exit_cfg in exits.items()
                  if isinstance(exit_cfg, dict) and exit_cfg.get("enabled")
                  and exit_cfg.get("profile_id") == profile_id]
    state = next((item for item in candidates if item.get("ready")), {})
    try:
        port = int(state.get("proxy_port") or 0)
    except (TypeError, ValueError):
        port = 0
    host = str(state.get("proxy_host") or "").strip()
    if not state.get("ready") or not host or not 1 <= port <= 65535:
        raise UpdateNetworkError("selected proxy library entry has no ready country exit")
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
    global _stars_cache
    try:
        response = session.get(f"https://api.github.com/repos/{repository_name}",
                               headers=headers, timeout=8)
        response.raise_for_status()
        count = int(response.json().get("stargazers_count"))
    except (requests.RequestException, OSError, ValueError, TypeError):
        return _stars_cache
    if count >= 0:
        _stars_cache = count
    return _stars_cache


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
    last_error: Exception | None = None
    try:
        candidates = _network_candidates()
    except UpdateNetworkError as exc:
        candidates, last_error = [], exc
    for selection in candidates:
        try:
            session = _session(selection)
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
                "network": selection,
                "stars": _stargazers(session, headers, repository_name),
            })
            last_error = None
            break
        except requests.HTTPError as exc:
            last_error = exc
            code = exc.response.status_code if exc.response is not None else 0
            if code in {401, 404}:
                break
        except (requests.RequestException, UpdateNetworkError, OSError, ValueError, TypeError) as exc:
            last_error = exc
    if isinstance(last_error, requests.HTTPError):
        exc = last_error
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
    elif isinstance(last_error, UpdateNetworkError):
        result["error"] = str(last_error)
        result["error_code"] = "update.error.proxy"
    elif last_error is not None:
        result["error"] = f"Update service unavailable: {type(last_error).__name__}"
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
    network = info.get("network") or _network_selection()
    # Reset the visible status first so a stale success/failure from a previous run cannot be
    # mistaken for this run's outcome while the orchestrator picks the request up.
    _write_private_json(status_path, {"state": "running", "phase": "requested",
                                      "target": info["latest"], "updated_at": now})
    _write_private_json(request_path, {"version": info["latest"], "repository": repository(),
                                       "requested_at": now,
                                       "network": network})
    return {"ok": True, "version": info["latest"]}
