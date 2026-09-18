"""Relay the browser softphone's SIP-over-WebSocket to its line's engine.

The browser only ever talks to the control surface origin (HTTPS, or a reverse proxy in front
of it) and picks a line by path. Each engine's Asterisk serves plain WS on its docker-bridge
address, which is never published to the host, so this relay is the only way in. That keeps a
single TLS hop and a single certificate (the WebUI's), and one path per line instead of one
host port per line.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from starlette.websockets import WebSocket
from websockets.asyncio.client import connect

log = logging.getLogger("mdd.softphone_ws")

SUBPROTOCOL = "sip"
# Asterisk's plain HTTP server inside the engine (http.conf bindport). Container-internal, so
# every line uses the same port; the container address is what tells lines apart.
ENGINE_WS_PORT = 8088
# One WebSocket message is one SIP message. A WebRTC INVITE with its SDP and ICE candidates is
# a few KB, so this only bounds a misbehaving peer.
MAX_MESSAGE = 256 * 1024
OPEN_TIMEOUT = 5


def path(iid: str) -> str:
    return f"/api/instances/{quote(str(iid), safe='')}/softphone/ws"


def engine_url(ip: str) -> str:
    return f"ws://{ip}:{ENGINE_WS_PORT}/ws"


def offers_sip(header: str | None) -> bool:
    return SUBPROTOCOL in {p.strip().lower() for p in (header or "").split(",")}


async def relay(browser: WebSocket, url: str) -> None:
    """Open the engine side first, then accept the browser, then pump both ways until either
    side closes. A line whose Asterisk is down fails the browser handshake instead of accepting
    a socket that can never register; JsSIP retries with its own backoff."""
    try:
        engine = await connect(url, subprotocols=[SUBPROTOCOL], open_timeout=OPEN_TIMEOUT,
                               max_size=MAX_MESSAGE, compression=None, proxy=None)
    except Exception as e:  # noqa: BLE001 - any failure means the same thing to the browser
        log.info("softphone relay: engine %s unreachable: %s", url, e)
        await browser.close(code=1011)
        return

    await browser.accept(subprotocol=SUBPROTOCOL)

    async def browser_to_engine():
        while True:
            message = await browser.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("text")
            if data is None:
                data = message.get("bytes")
            if data is not None:
                await engine.send(data)

    async def engine_to_browser():
        async for data in engine:
            if isinstance(data, str):
                await browser.send_text(data)
            else:
                await browser.send_bytes(data)

    tasks = [asyncio.create_task(browser_to_engine()), asyncio.create_task(engine_to_browser())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                log.debug("softphone relay %s ended: %r", url, task.exception())
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.close()
        try:
            await browser.close()
        except Exception:  # noqa: BLE001 - already closed by the browser
            pass
