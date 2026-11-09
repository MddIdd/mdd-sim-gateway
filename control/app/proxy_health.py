"""Advisory UDP monitoring. Never restart an engine, select a node or alter registration."""
from __future__ import annotations

import asyncio
import json
import time

from . import egress

INTERVAL = 600
CONFIRM_INTERVAL = 30
FAILURE_INTERVAL = 60
MIN_RECHECK = 10
MAX_AGE = INTERVAL + 120
UNHEALTHY_MAX_AGE = 180
FAILURES = 2
LABEL = "Proxy UDP connectivity problem"
REASON = ("The current proxy repeatedly failed UDP connectivity checks. Incoming and outgoing "
          "calls may fail. Check or change the proxy.")


class Monitor:
    def __init__(self):
        self.results = {}
        self.tasks = {}
        self.limit = asyncio.Semaphore(2)

    @staticmethod
    def key(context):
        return json.dumps(context, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def max_age(result):
        return MAX_AGE if result.get("state") == "passed" else UNHEALTHY_MAX_AGE

    def snapshot(self, context):
        if context is None:
            return None
        key = self.key(context)
        result = self.results.get(key)
        value = {"state": "unknown", "checked_at": None}
        if result:
            value = {name: result[name] for name in ("state", "checked_at")}
            if time.monotonic() - result["completed"] > self.max_age(result):
                value["state"] = "unknown"
        # Preserve an established warning while its recheck runs.
        if key in self.tasks and value["state"] == "unknown":
            value["state"] = "checking"
        return {**value, "checking": key in self.tasks}

    async def refresh(self, context, current_context, *, force=False):
        if context is None or not context.get("proxy_host"):
            return self.snapshot(context)
        key = self.key(context)
        if key in self.tasks:
            return await asyncio.shield(self.tasks[key])
        previous = self.results.get(key)
        state = (previous or {}).get("state")
        interval = (INTERVAL if state == "passed" else
                    CONFIRM_INTERVAL if state == "suspect" else FAILURE_INTERVAL)
        if previous and time.monotonic() - previous["completed"] < (
                MIN_RECHECK if force else interval):
            return self.snapshot(context)
        task = asyncio.create_task(self._probe(context, current_context))
        self.tasks[key] = task
        task.add_done_callback(lambda done: self.tasks.pop(key, None)
                               if self.tasks.get(key) is done else None)
        return await asyncio.shield(task)

    async def _probe(self, context, current_context):
        key = self.key(context)
        async with self.limit:
            # Selection can change while waiting for a worker as well as during I/O.
            if current_context() != context:
                return None
            try:
                await asyncio.to_thread(egress.test_udp_proxy,
                                        context["proxy_host"], context["proxy_port"], 8.0)
                verdict = "passed"
            except (egress.EgressError, OSError):
                verdict = "failed"
            except Exception:
                # A local collector error is not evidence against the proxy. Raw errors
                # may include endpoints/credentials and never leave this collector.
                verdict = "unknown"
            if current_context() != context:
                return None
            previous = self.results.get(key) or {}
            recent = time.monotonic() - previous.get("completed", float("-inf")) <= self.max_age(previous)
            failures = (previous.get("failures", 0) if recent else 0) + 1 if verdict == "failed" else 0
            state = ("failed" if failures >= FAILURES else "suspect") if failures else verdict
            self.results[key] = {"state": state, "failures": failures,
                                 "checked_at": int(time.time()), "completed": time.monotonic()}
            return {"state": state, "checked_at": self.results[key]["checked_at"], "checking": False}

    def prune(self, contexts):
        active = {self.key(value) for value in contexts if value is not None}
        self.results = {key: value for key, value in self.results.items() if key in active}

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()


def annotate(status, health):
    """Keep the registration state authoritative; expose the independent warning to UIs.

    Existing recovery, failover, outage notifications and call routing continue to consume
    the registration state. A failed public UDP probe is not a proven carrier outage.
    """
    result = {**status}
    result.pop("proxy_health", None)
    if health is not None:
        result["proxy_health"] = health
    if result.get("state") == "OK":
        result.update(label="IMS registered", reason_code="ok", reason="IMS registration is active.")
        if health and health["state"] == "failed":
            result.update(label=LABEL, reason_code="proxy_udp_failed", reason=REASON)
    return result
