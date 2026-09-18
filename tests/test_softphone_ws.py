"""The browser softphone reaches its line's engine through the control surface, by path."""
import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader
from websockets.asyncio.server import serve

from control.app import main, softphone_ws


class _Browser:
    """The parts of starlette's WebSocket the endpoint uses, driven by the test.

    starlette.testclient needs httpx, which is not a runtime dependency; the endpoint is small
    enough to exercise directly."""

    def __init__(self, subprotocols=("sip",)):
        self.cookies = {main.auth.SESSION_COOKIE: "token"}
        self.headers = {"sec-websocket-protocol": ", ".join(subprotocols)} if subprotocols else {}
        self.accepted = None
        self.close_code = None
        self.inbox = asyncio.Queue()    # what the browser sends
        self.outbox = asyncio.Queue()   # what the relay delivers to the browser

    async def accept(self, subprotocol=None):
        self.accepted = subprotocol

    async def close(self, code=1000):
        if self.close_code is None:
            self.close_code = code

    async def receive(self):
        return await self.inbox.get()

    async def send_text(self, data):
        await self.outbox.put(data)

    async def send_bytes(self, data):
        await self.outbox.put(data)

    def say(self, text):
        self.inbox.put_nowait({"type": "websocket.receive", "text": text})

    def hang_up(self):
        self.inbox.put_nowait({"type": "websocket.disconnect", "code": 1000})


class SoftphoneRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine_subprotocols = []
        self.engine_closed = asyncio.Event()

        async def handler(connection):
            self.engine_subprotocols.append(connection.subprotocol)
            async for message in connection:
                await connection.send(f"engine:{message}")
            self.engine_closed.set()

        self.server = await serve(handler, "127.0.0.1", 0, subprotocols=["sip"])
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    def patched(self, *, session=True, instance=None, runtime=None, port=None):
        instance = {"id": "sim1", "sip": {"webrtc": {"enable": True}}} if instance is None else instance
        runtime = runtime or {"running": True, "ip": "127.0.0.1", "container_id": "c1"}
        for p in (
            patch.object(main.auth, "session", return_value={"csrf": "x"} if session else None),
            patch.object(main.cfg, "get_instance", return_value=instance),
            patch.object(main.engine, "container_runtime", return_value=runtime),
            patch.object(softphone_ws, "ENGINE_WS_PORT", self.port if port is None else port),
        ):
            p.start()
            self.addCleanup(p.stop)

    async def test_sip_messages_flow_both_ways_on_the_sip_subprotocol(self):
        self.patched()
        browser = _Browser()
        call = asyncio.create_task(main.ws_softphone(browser, "sim1"))
        browser.say("REGISTER sip:ims SIP/2.0")
        reply = await asyncio.wait_for(browser.outbox.get(), 5)
        self.assertEqual(browser.accepted, "sip")
        self.assertEqual(reply, "engine:REGISTER sip:ims SIP/2.0")
        self.assertEqual(self.engine_subprotocols, ["sip"])
        # Closing the browser side must close the engine side too, or every page reload would
        # leave a registered socket behind in Asterisk.
        browser.hang_up()
        await asyncio.wait_for(call, 5)
        await asyncio.wait_for(self.engine_closed.wait(), 5)

    async def refused(self, browser=None, **patches):
        self.patched(**patches)
        browser = browser or _Browser()
        await asyncio.wait_for(main.ws_softphone(browser, "sim1"), 10)
        self.assertIsNone(browser.accepted)
        return browser.close_code

    async def test_signed_out_browser_is_refused(self):
        self.assertEqual(await self.refused(session=False), 4401)

    async def test_unknown_line_disabled_softphone_or_missing_subprotocol_is_refused(self):
        cases = [
            ({}, ("sip",)),
            ({"id": "sim1", "sip": {"webrtc": {"enable": False}}}, ("sip",)),
            (None, ()),
        ]
        for instance, subprotocols in cases:
            with self.subTest(instance=instance, subprotocols=subprotocols):
                code = await self.refused(_Browser(subprotocols), instance=instance)
                self.assertEqual(code, 1008)

    async def test_stopped_engine_is_refused(self):
        code = await self.refused(runtime={"running": False, "ip": None, "container_id": None})
        self.assertEqual(code, 1013)

    async def test_unreachable_engine_fails_the_handshake(self):
        self.server.close()
        await self.server.wait_closed()
        self.assertEqual(await self.refused(), 1011)

    def test_path_is_per_line_and_escaped(self):
        self.assertEqual(softphone_ws.path("sim1"), "/api/instances/sim1/softphone/ws")
        self.assertEqual(softphone_ws.path("a/b"), "/api/instances/a%2Fb/softphone/ws")
        self.assertTrue(softphone_ws.offers_sip("chat, SIP"))
        self.assertFalse(softphone_ws.offers_sip(None))


class EngineListenerTests(unittest.TestCase):
    """What the relay connects to: plain WS on the bridge address, never on the tunnel."""

    @staticmethod
    def http_conf(**ctx):
        root = Path(__file__).resolve().parents[1]
        env = Environment(loader=FileSystemLoader(str(root / "engine" / "templates")),
                          trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
        return env.get_template("http.conf.j2").render(
            **{"webrtc_enable": True, "local_addr": "172.17.0.3",
               "webrtc_ws_port": softphone_ws.ENGINE_WS_PORT, **ctx})

    def test_listens_in_plain_on_the_bridge_address_at_the_relay_port(self):
        conf = self.http_conf()
        self.assertIn("bindaddr=172.17.0.3\n", conf)
        self.assertIn(f"bindport={softphone_ws.ENGINE_WS_PORT}\n", conf)
        self.assertNotIn("tls", conf)
        self.assertNotIn("0.0.0.0", conf)

    def test_stays_on_loopback_without_a_softphone_or_bridge_address(self):
        for ctx in ({"webrtc_enable": False}, {"local_addr": ""}):
            with self.subTest(**ctx):
                self.assertIn("bindaddr=127.0.0.1\n", self.http_conf(**ctx))

    def test_render_uses_the_relay_port(self):
        render = (Path(__file__).resolve().parents[1] / "engine" / "render.py").read_text()
        self.assertIn(f'"webrtc_ws_port": {softphone_ws.ENGINE_WS_PORT},', render)


if __name__ == "__main__":
    unittest.main()
