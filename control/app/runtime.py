"""Event-backed cache of engine container runtime state.

The status poller used to inspect each container every four seconds. Docker already publishes
authoritative lifecycle events, so keep a small cache hot from those events and retain a slow
inspect as a self-healing fallback when the event stream disconnects or an event is missed.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable

import docker

from . import engine

log = logging.getLogger("mdd.runtime")

EVENT_CACHE_SECONDS = 60.0
FALLBACK_CACHE_SECONDS = 4.0
_STOP_ACTIONS = {"die", "destroy", "kill", "stop"}
_START_ACTIONS = {"start", "restart", "unpause"}


class RuntimeRegistry:
    def __init__(self, inspector=engine.container_runtime):
        self._inspector = inspector
        self._cache: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._processor: asyncio.Task | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._stream_lock = threading.Lock()
        self._event_client = None
        self._event_stream = None
        self._on_change: Callable[[str, dict, str], Awaitable[None]] | None = None
        self.events_connected = False

    async def start(self, on_change=None) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._on_change = on_change
        self._stop.clear()
        loop = asyncio.get_running_loop()
        self._processor = asyncio.create_task(self._process_events())
        self._thread = threading.Thread(
            target=self._watch_events, args=(loop,), name="mdd-docker-events", daemon=True)
        self._thread.start()

    async def close(self) -> None:
        self._stop.set()
        with self._stream_lock:
            stream, client = self._event_stream, self._event_client
        for value in (stream, client):
            if value is not None:
                try:
                    await asyncio.to_thread(value.close)
                except Exception:
                    pass
        if self._processor:
            self._processor.cancel()
            await asyncio.gather(self._processor, return_exceptions=True)
            self._processor = None
        if self._thread:
            await asyncio.to_thread(self._thread.join, 2.0)
            self._thread = None
        self.events_connected = False

    async def get(self, iid: str, force: bool = False) -> dict:
        iid = str(iid)
        max_age = EVENT_CACHE_SECONDS if self.events_connected else FALLBACK_CACHE_SECONDS
        cached = self._cache.get(iid)
        if not force and cached and time.monotonic() - cached["observed_at"] < max_age:
            return dict(cached)
        lock = self._locks.setdefault(iid, asyncio.Lock())
        async with lock:
            cached = self._cache.get(iid)
            if not force and cached and time.monotonic() - cached["observed_at"] < max_age:
                return dict(cached)
            runtime = await asyncio.to_thread(self._inspector, iid)
            runtime = {**runtime, "observed_at": time.monotonic()}
            self._cache[iid] = runtime
            return dict(runtime)

    def invalidate(self, iid: str) -> None:
        self._cache.pop(str(iid), None)

    async def handle_event(self, event: dict) -> None:
        """Apply one decoded Docker event. Public for deterministic unit testing."""
        actor = event.get("Actor") or {}
        attrs = actor.get("Attributes") or {}
        name = str(attrs.get("name") or "")
        prefix = "mdd-sim-gateway-engine-"
        if not name.startswith(prefix):
            return
        iid = name[len(prefix):]
        action = str(event.get("Action") or event.get("status") or "").lower()
        if action in _STOP_ACTIONS:
            # Keep the same keys an inspect returns; callers read restart_count unconditionally.
            runtime = {"running": False, "ip": None,
                       "container_id": event.get("id") or actor.get("ID"),
                       "restart_count": int((self._cache.get(iid) or {}).get("restart_count") or 0),
                       "started_at": str((self._cache.get(iid) or {}).get("started_at") or ""),
                       "observed_at": time.monotonic()}
            self._cache[iid] = runtime
        elif action in _START_ACTIONS:
            runtime = await self.get(iid, force=True)
        else:
            return
        if self._on_change:
            await self._on_change(iid, dict(runtime), action)

    async def _process_events(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self.handle_event(event)
            except Exception as exc:  # noqa
                log.warning("Docker event handling failed: %s", exc)

    def _watch_events(self, loop: asyncio.AbstractEventLoop) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            client = stream = None
            connected_at = time.monotonic()
            try:
                # This is a deliberately idle streaming request; a finite HTTP read timeout
                # would tear down a perfectly healthy connection whenever no container changed.
                client = docker.from_env(timeout=None)
                stream = client.events(decode=True, filters={
                    "type": "container", "label": f"{engine.MANAGED_LABEL}=true"})
                with self._stream_lock:
                    self._event_client, self._event_stream = client, stream
                self.events_connected = True
                for event in stream:
                    if self._stop.is_set():
                        break
                    loop.call_soon_threadsafe(self._queue.put_nowait, event)
            except Exception as exc:  # noqa
                if not self._stop.is_set():
                    log.warning("Docker event stream disconnected: %s", exc)
            finally:
                self.events_connected = False
                with self._stream_lock:
                    self._event_client = self._event_stream = None
                for value in (stream, client):
                    if value is not None:
                        try:
                            value.close()
                        except Exception:
                            pass
            if time.monotonic() - connected_at > 30:
                backoff = 2.0
            elif not self._stop.is_set():
                backoff = min(30.0, backoff * 2)
            self._stop.wait(backoff)
