"""Every API request and every WebSocket handshake passes one gate.

Driven as raw ASGI: starlette.testclient needs httpx, which is not a runtime dependency, and
the gate is only a function of the scope and the first messages anyway.
"""
import asyncio
import unittest
from unittest.mock import patch

from control.app import gate, main

SESSION = "valid-session-token"
CSRF = "csrf-of-the-session"
ENGINE_TOKEN = "per-install-engine-token"
HOST = "gateway.example.net"
ORIGIN = f"https://{HOST}"


def _headers(pairs):
    return [(name.encode("latin-1"), value.encode("latin-1")) for name, value in pairs]


def http(path, method="GET", *, cookie=SESSION, headers=()):
    pairs = [("host", HOST)]
    if cookie:
        pairs.append(("cookie", f"{main.auth.SESSION_COOKIE}={cookie}"))
    return {"type": "http", "path": path, "method": method, "client": ("192.0.2.10", 50000),
            "headers": _headers(pairs + list(headers))}


def websocket(path="/ws", *, cookie=SESSION, origin=ORIGIN, host=HOST, subprotocols=(),
              peer="192.0.2.10", headers=()):
    pairs = [("host", host)] if host else []
    if origin is not None:
        pairs.append(("origin", origin))
    if cookie:
        pairs.append(("cookie", f"{main.auth.SESSION_COOKIE}={cookie}"))
    return {"type": "websocket", "path": path, "client": (peer, 50000),
            "subprotocols": list(subprotocols), "headers": _headers(pairs + list(headers))}


class _Result:
    def __init__(self):
        self.sent = []
        self.reached = None     # the scope the application saw, if it was reached

    @property
    def status(self):
        return next(m["status"] for m in self.sent if m["type"] == "http.response.start")

    @property
    def accepted(self):
        return any(m["type"] == "websocket.accept" for m in self.sent)

    @property
    def close_code(self):
        return next((m.get("code", 1000) for m in self.sent if m["type"] == "websocket.close"),
                    None)

    @property
    def principal(self):
        return self.reached["state"]["principal"] if self.reached else None


async def _run(scope):
    result = _Result()

    async def application(scope, receive, send):
        result.reached = scope
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
        else:
            await receive()
            await send({"type": "websocket.accept"})

    inbox = [{"type": "http.request", "body": b"", "more_body": False}
             if scope["type"] == "http" else {"type": "websocket.connect"}]

    async def receive():
        if inbox:
            return inbox.pop(0)
        await asyncio.sleep(3600)

    async def send(message):
        result.sent.append(message)

    await asyncio.wait_for(gate.Gate(application)(scope, receive, send), 5)
    return result


def run(scope):
    return asyncio.run(_run(scope))


class GateTestCase(unittest.TestCase):
    def setUp(self):
        sessions = {SESSION: {"csrf": CSRF}}
        settings = {"security": {"trusted_proxies": ["10.0.0.0/8"]}}
        for p in (
            patch.object(gate.auth, "session", side_effect=lambda token: sessions.get(token)),
            patch.object(gate.cfg, "internal_event_token", return_value=ENGINE_TOKEN),
            patch.object(gate.cfg, "get_settings", return_value=settings),
        ):
            p.start()
            self.addCleanup(p.stop)


class HttpTests(GateTestCase):
    def test_static_assets_are_public(self):
        result = run(http("/assets/index.js", cookie=None))
        self.assertEqual(result.status, 200)
        self.assertEqual(result.principal, gate.ANONYMOUS)

    def test_public_paths_are_reachable_signed_out_and_still_see_a_session(self):
        for path in sorted(gate.PUBLIC_PATHS):
            with self.subTest(path=path):
                signed_out = run(http(path, "POST", cookie=None))
                self.assertEqual(signed_out.status, 200)
                self.assertEqual(signed_out.principal, gate.ANONYMOUS)
                signed_in = run(http(path, "GET"))
                self.assertEqual(signed_in.principal, gate.Principal("admin", csrf=CSRF))

    def test_api_needs_a_live_session(self):
        for cookie in (None, "expired-or-unknown"):
            with self.subTest(cookie=cookie):
                result = run(http("/api/instances", cookie=cookie))
                self.assertEqual(result.status, 401)
                self.assertIsNone(result.reached)
        result = run(http("/api/instances"))
        self.assertEqual(result.status, 200)
        self.assertEqual(result.principal.kind, "admin")

    def test_other_services_cookies_do_not_hide_the_session(self):
        # Browsers send every cookie of the host name, whatever the port; another service on
        # the same host may write values RFC 6265 does not allow.
        for other in ('other_app={"x": 1}', "theme=dark mode", 'quoted="a b"', "flag",
                      "=nameless"):
            with self.subTest(other=other):
                scope = http("/api/instances", cookie=None,
                             headers=[("cookie", f"{other}; {main.auth.SESSION_COOKIE}={SESSION}")])
                result = run(scope)
                self.assertEqual(result.status, 200)
                self.assertEqual(result.principal.kind, "admin")

    def test_state_changes_need_the_sessions_csrf_token(self):
        for method in sorted(gate.MUTATING_METHODS):
            for supplied, expected in ((None, 403), ("wrong", 403), (CSRF, 200)):
                with self.subTest(method=method, supplied=supplied):
                    extra = [(gate.CSRF_HEADER, supplied)] if supplied else []
                    result = run(http("/api/instances/sim1", method, headers=extra))
                    self.assertEqual(result.status, expected)

    def test_reads_need_no_csrf_token(self):
        self.assertEqual(run(http("/api/instances", "GET")).status, 200)

    def test_engine_callback_takes_only_the_engine_token(self):
        path = gate.ENGINE_EVENT_PATH
        good = [(gate.ENGINE_TOKEN_HEADER, ENGINE_TOKEN)]
        result = run(http(path, "POST", cookie=None, headers=good))
        self.assertEqual(result.status, 200)
        self.assertEqual(result.principal, gate.ENGINE)
        for label, scope in (
            ("wrong token", http(path, "POST", cookie=None,
                                 headers=[(gate.ENGINE_TOKEN_HEADER, "wrong")])),
            ("no token", http(path, "POST", cookie=None)),
            # An administrator's browser session is not the engine.
            ("session only", http(path, "POST", headers=[(gate.CSRF_HEADER, CSRF)])),
        ):
            with self.subTest(label):
                self.assertEqual(run(scope).status, 401)

    def test_engine_token_is_not_a_session_elsewhere(self):
        scope = http("/api/instances", cookie=None, headers=[(gate.ENGINE_TOKEN_HEADER,
                                                              ENGINE_TOKEN)])
        self.assertEqual(run(scope).status, 401)

    def test_unset_engine_token_refuses_every_callback(self):
        with patch.object(gate.cfg, "internal_event_token", return_value=""):
            scope = http(gate.ENGINE_EVENT_PATH, "POST", cookie=None,
                         headers=[(gate.ENGINE_TOKEN_HEADER, "")])
            self.assertEqual(run(scope).status, 401)


class WebSocketTests(GateTestCase):
    def test_signed_in_same_origin_socket_reaches_the_endpoint(self):
        result = run(websocket())
        self.assertIsNotNone(result.reached)
        self.assertEqual(result.principal.kind, "admin")

    def test_other_services_cookies_do_not_hide_the_sessions_socket(self):
        scope = websocket(cookie=None, headers=[
            ("cookie", f'other_app={{"x": 1}}; theme=dark mode; '
                       f'{main.auth.SESSION_COOKIE}={SESSION}')])
        self.assertEqual(run(scope).principal.kind, "admin")

    def test_signed_out_socket_is_closed_with_4401_after_accepting(self):
        # The WebUI's event socket reads 4401 as "session ended"; a browser reports a close code
        # only for a socket that was accepted.
        for cookie in (None, "expired-or-unknown"):
            with self.subTest(cookie=cookie):
                result = run(websocket(cookie=cookie))
                self.assertIsNone(result.reached)
                self.assertTrue(result.accepted)
                self.assertEqual(result.close_code, gate.WS_UNAUTHENTICATED)

    def test_signed_out_subprotocol_socket_is_closed_before_accepting(self):
        # The softphone asks for "sip"; accepting without naming it would fail the handshake
        # on the browser's side for the wrong reason.
        result = run(websocket("/api/instances/sim1/softphone/ws", cookie=None,
                               subprotocols=["sip"]))
        self.assertIsNone(result.reached)
        self.assertFalse(result.accepted)
        self.assertEqual(result.close_code, gate.WS_UNAUTHENTICATED)

    def test_any_socket_path_is_gated(self):
        result = run(websocket("/api/some/socket/added/later", cookie=None))
        self.assertIsNone(result.reached)
        self.assertEqual(result.close_code, gate.WS_UNAUTHENTICATED)

    def test_foreign_missing_or_opaque_origin_is_refused(self):
        for origin in ("https://other.example.net", "https://evil.example", None, "null",
                       f"https://{HOST}:8443", f"http://{HOST}", f"http://{HOST}:443",
                       "not a url",
                       f"{ORIGIN}/path"):
            with self.subTest(origin=origin):
                result = run(websocket(origin=origin))
                self.assertIsNone(result.reached)
                self.assertEqual(result.close_code, gate.WS_FORBIDDEN_ORIGIN)

    def test_origin_matches_host_by_name_and_port(self):
        for origin, host in ((ORIGIN, f"{HOST}:443"), (f"https://{HOST.upper()}", HOST),
                             (f"https://{HOST}:8443", f"{HOST}:8443"),
                             ("https://[2001:db8::1]:8443", "[2001:db8::1]:8443")):
            with self.subTest(origin=origin, host=host):
                self.assertIsNotNone(run(websocket(origin=origin, host=host)).reached)

    def test_forwarded_host_is_believed_only_from_a_trusted_proxy(self):
        rewritten = dict(host="127.0.0.1:8443", headers=[("x-forwarded-host", HOST)])
        self.assertIsNotNone(run(websocket(peer="10.1.2.3", **rewritten)).reached)
        refused = run(websocket(peer="192.0.2.10", **rewritten))
        self.assertIsNone(refused.reached)
        self.assertEqual(refused.close_code, gate.WS_FORBIDDEN_ORIGIN)

    def test_origin_is_checked_only_after_authentication(self):
        # A signed-out foreign page learns only that it is signed out.
        result = run(websocket(cookie=None, origin="https://evil.example"))
        self.assertEqual(result.close_code, gate.WS_UNAUTHENTICATED)


class SourceTableTests(unittest.TestCase):
    # One representative request per credential source; the tests above exercise each of them.
    EXAMPLES = {
        "static": http("/assets/index.js", cookie=None),
        "public": http("/api/auth/login", "POST", cookie=None),
        "engine": http(gate.ENGINE_EVENT_PATH, "POST", cookie=None),
        "session": http("/api/instances"),
        "socket": websocket(),
    }

    def test_every_source_is_exercised(self):
        # A row added to the table without an example here -- and tests above -- fails.
        self.assertEqual(set(self.EXAMPLES), {source.name for source in gate.SOURCES})
        for name, scope in self.EXAMPLES.items():
            with self.subTest(name=name):
                self.assertEqual(gate.source_for(scope).name, name)

    def test_source_names_are_unique(self):
        names = [source.name for source in gate.SOURCES]
        self.assertEqual(len(names), len(set(names)))

    def test_other_asgi_traffic_is_passed_through(self):
        self.assertIsNone(gate.source_for({"type": "lifespan"}))


class WiringTests(unittest.TestCase):
    def test_the_application_is_behind_the_gate(self):
        self.assertIn(gate.Gate, [item.cls for item in main.app.user_middleware])

    def test_handlers_read_the_gates_answer(self):
        class _Connection:
            scope = {"state": {"principal": gate.Principal("admin", csrf=CSRF)}}

        self.assertEqual(gate.current(_Connection()).csrf, CSRF)
        _Connection.scope = {}
        self.assertEqual(gate.current(_Connection()), gate.ANONYMOUS)


if __name__ == "__main__":
    unittest.main()
