"""Who is calling the control surface, answered once for HTTP and WebSocket alike.

Every management route lives under ``/api/`` and every WebSocket needs a signed-in browser, so
one ASGI middleware sits in front of both. It used to be an HTTP middleware, which never sees a
WebSocket handshake: each socket endpoint had to repeat the session check itself, and a socket
added later without that check would have been open to anyone.

The middleware resolves the caller to a :class:`Principal`, refuses what needs a credential
the caller does not have, and leaves the answer in ``scope["state"]["principal"]`` for handlers
that need to know more than "allowed".

A socket authenticated by the session cookie must also come from the gateway's own page. The
cookie is SameSite=Strict, but "site" means the registrable domain: a page on a sibling host
(``other.example.net`` beside ``gateway.example.net``) still gets the cookie attached. Browsers
always send ``Origin`` on a WebSocket handshake and a page cannot change it, so the handshake is
accepted only when that origin is the host the browser asked for.
"""
from __future__ import annotations

import hmac
import ipaddress
import logging
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from starlette.requests import cookie_parser
from starlette.responses import JSONResponse
from starlette.websockets import WebSocket

from . import auth
from . import config as cfg

log = logging.getLogger("mdd.gate")

PUBLIC_PATHS = frozenset({"/api/auth/status", "/api/auth/setup", "/api/auth/login"})
ENGINE_EVENT_PATH = "/api/engine/event"
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_HEADER = "x-mdd-csrf-token"
ENGINE_TOKEN_HEADER = "x-mdd-engine-token"

# WebSocket close codes. 4401 tells the WebUI its session is gone (show the login screen);
# 4403 tells it the page is not one the gateway serves, which signing in again would not fix.
WS_UNAUTHENTICATED = 4401
WS_FORBIDDEN_ORIGIN = 4403

# The control surface is served only over TLS (run.py) and its cookie is Secure, so the page a
# signed-in socket comes from is always https; a Host without a port means 443.
HTTPS_PORT = 443


@dataclass(frozen=True)
class Principal:
    """The caller of one request or socket."""

    kind: str           # "admin" | "engine" | "anonymous"
    csrf: str = ""      # the session's CSRF token; only a cookie session has one

    @property
    def authenticated(self) -> bool:
        return self.kind != "anonymous"


ANONYMOUS = Principal("anonymous")
ENGINE = Principal("engine")


def _headers(scope) -> dict[str, str]:
    """Request headers, lower-cased; a repeated header keeps its first value."""
    headers: dict[str, str] = {}
    for name, value in scope.get("headers") or ():
        headers.setdefault(name.decode("latin-1").lower(), value.decode("latin-1"))
    return headers


def _cookie(headers: dict[str, str], name: str) -> str:
    """One cookie's value, read the way browsers send them rather than the way RFC 6265 asks.

    A browser sends every cookie of the host name, whatever the port, so other services on the
    same host add theirs: values with spaces or quotes are common there. http.cookies drops such
    a cookie and every one after it without a word, which would lose the session and leave the
    administrator signed in yet refused. Starlette's parser, which handled this before the gate
    existed, splits on ";" and keeps going.
    """
    return cookie_parser(headers.get("cookie", "")).get(name, "")


def _session(headers: dict[str, str]) -> Principal | None:
    current = auth.session(_cookie(headers, auth.SESSION_COOKIE) or None)
    return Principal("admin", csrf=str(current.get("csrf") or "")) if current else None


def _engine(headers: dict[str, str]) -> Principal | None:
    expected = cfg.internal_event_token()
    supplied = headers.get(ENGINE_TOKEN_HEADER, "")
    return ENGINE if expected and hmac.compare_digest(supplied, expected) else None


def _nobody(headers: dict[str, str]) -> Principal | None:
    return None


def _api(scope) -> bool:
    return (scope.get("path") or "").startswith("/api/")


@dataclass(frozen=True)
class Source:
    """Where one kind of caller's credential comes from, and what else it must prove.

    ``resolve`` reads the credential from the headers and returns None when there is none or it
    is not valid. ``required`` refuses the request then; otherwise it goes on as anonymous.
    ``csrf`` asks state-changing requests for the session's CSRF token, which a browser does not
    attach by itself. ``origin`` asks a WebSocket handshake for the gateway's own page.
    """

    name: str
    transport: str                          # "http" | "websocket"
    matches: Callable[[dict], bool]
    resolve: Callable[[dict[str, str]], Principal | None]
    required: bool = True
    refusal: str = "authentication required"
    csrf: bool = False
    origin: bool = False


# Checked in order; the first source whose ``matches`` accepts the scope decides alone, so a
# credential is honoured only where its own row says so -- the engine token nowhere but the
# engine callback, the session cookie nowhere on it.
SOURCES: tuple[Source, ...] = (
    # Static assets stay public so the browser can render the login screen.
    Source("static", "http", lambda scope: not _api(scope), _nobody, required=False),
    # Reachable signed out; a session, if there is one, is still reported to the handler.
    Source("public", "http", lambda scope: scope.get("path") in PUBLIC_PATHS, _session,
           required=False),
    Source("engine", "http", lambda scope: scope.get("path") == ENGINE_EVENT_PATH, _engine,
           refusal="invalid engine token"),
    Source("session", "http", _api, _session, csrf=True),
    Source("socket", "websocket", lambda scope: True, _session, origin=True),
)


def source_for(scope) -> Source | None:
    """The one credential source that decides this request, if the gate handles it at all."""
    for source in SOURCES:
        if source.transport == scope.get("type") and source.matches(scope):
            return source
    return None


def principal(scope) -> Principal:
    """Who is calling, from the credential its source accepts.

    Only says who; whether that is enough for the path is the middleware's decision.
    """
    source = source_for(scope)
    return (source.resolve(_headers(scope)) if source else None) or ANONYMOUS


def current(connection) -> Principal:
    """The principal the middleware resolved, for a handler's Request or WebSocket."""
    found = connection.scope.get("state", {}).get("principal")
    return found if isinstance(found, Principal) else ANONYMOUS


def trusted_proxy(peer: str, settings: dict | None = None) -> bool:
    """Whether the direct peer is a reverse proxy whose forwarding headers are believed."""
    settings = settings if settings is not None else cfg.get_settings()
    trusted = (settings.get("security") or {}).get("trusted_proxies") or []
    try:
        address = ipaddress.ip_address(peer)
        return any(address in ipaddress.ip_network(str(item), strict=False) for item in trusted)
    except ValueError:
        return False


def _host_port(authority: str) -> tuple[str, int] | None:
    """``host[:port]`` normalised for comparison; the port defaults to HTTPS's."""
    try:
        parts = urlsplit("//" + authority.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host:
        return None
    return host.lower(), port or HTTPS_PORT


def origin_allowed(scope, headers: dict[str, str] | None = None,
                   settings: dict | None = None) -> bool:
    """Whether a WebSocket handshake comes from a page the gateway itself served.

    A browser always sends ``Origin`` on a handshake, so a missing one, or the opaque ``null``
    of a sandboxed frame or a local file, is refused: nothing that uses the session cookie
    legitimately arrives without it. The gateway's own page is always https, so an http origin
    on the same host name is another page, not this one. Behind a reverse proxy that rewrites ``Host``, the name the
    browser used is in ``X-Forwarded-Host``, which is believed only from a configured trusted
    proxy -- the same rule the audit log applies to ``X-Forwarded-For``.
    """
    headers = headers if headers is not None else _headers(scope)
    origin = headers.get("origin", "").strip()
    if not origin or origin == "null":
        return False
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme != "https" or parts.path not in ("", "/"):
        return False
    wanted = _host_port(parts.netloc)
    if wanted is None:
        return False
    if _host_port(headers.get("host", "")) == wanted:
        return True
    forwarded = headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    peer = (scope.get("client") or ("",))[0] or ""
    return bool(forwarded) and trusted_proxy(peer, settings) and \
        _host_port(forwarded) == wanted


class Gate:
    """ASGI middleware: dispatch every request and handshake to its credential source."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        source = source_for(scope)
        if source is None:
            return await self.app(scope, receive, send)
        headers = _headers(scope)
        who = source.resolve(headers)
        if who is None and source.required:
            return await self._deny(scope, receive, send, 401, source.refusal,
                                    WS_UNAUTHENTICATED)
        who = who or ANONYMOUS
        if source.csrf and scope.get("method", "GET").upper() in MUTATING_METHODS:
            if not hmac.compare_digest(headers.get(CSRF_HEADER, ""), who.csrf):
                return await self._deny(scope, receive, send, 403, "invalid CSRF token",
                                        WS_FORBIDDEN_ORIGIN)
        if source.origin and not origin_allowed(scope, headers):
            log.warning("refused WebSocket %s from origin %r (host %r, forwarded host %r)",
                        scope.get("path"), headers.get("origin", ""), headers.get("host", ""),
                        headers.get("x-forwarded-host", ""))
            return await self._deny(scope, receive, send, 403, "origin not allowed",
                                    WS_FORBIDDEN_ORIGIN)
        scope.setdefault("state", {})["principal"] = who
        await self.app(scope, receive, send)

    async def _deny(self, scope, receive, send, status: int, detail: str, close_code: int):
        if scope["type"] == "websocket":
            return await self._refuse(scope, receive, send, close_code)
        await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)

    @staticmethod
    async def _refuse(scope, receive, send, code: int) -> None:
        """Close a handshake the way its client can read.

        A browser that asked for a subprotocol (the softphone's ``sip``) fails any handshake whose
        answer does not name one, so that socket is closed before it is accepted. Anything else is
        accepted first, because a browser reports the close code only for an open socket: the
        WebUI's event socket relies on seeing 4401 to know the session has ended.
        """
        ws = WebSocket(scope, receive, send)
        if not scope.get("subprotocols"):
            await ws.accept()
        await ws.close(code=code)
