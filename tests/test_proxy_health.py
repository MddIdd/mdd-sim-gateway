"""Runtime UDP warnings must describe the selected exit without changing call lifecycle."""
import asyncio
import copy
import time
import unittest
from unittest.mock import AsyncMock, patch

from control.app import authz, device_state, egress, gate, main, proxy_health
from control.app.egress_contract import proxy_fingerprint


class RuntimeProbeContextTests(unittest.TestCase):
    def setUp(self):
        self.inst = {"id": "1", "mcc": "234"}
        self.settings = {"proxy": {"enabled": True, "exits": {
            "gb": {"enabled": True, "mode": "subscription", "pinned_node": "one"}}}}
        self.document = {"version": 1, "transport": "socks5", "enabled": True,
                         "updated_at": time.time(),
                         "config_fingerprint": proxy_fingerprint(self.settings["proxy"]),
                         "exits": {"gb": {"ready": True, "transport": "socks5",
                             "mode": "subscription", "selection": "manual", "node": "one",
                             "candidates": ["one"], "proxy_host": "mdd-egress", "proxy_port": 1080}}}

    def context(self, transport="socks5"):
        with patch.object(egress, "transport", return_value=transport), \
                patch.object(egress, "status", return_value=self.document):
            return egress.runtime_probe_context(self.inst, self.settings)

    def test_manual_single_node_is_monitored_without_becoming_failover_eligible(self):
        self.assertEqual(self.context()["proxy_host"], "mdd-egress")
        with patch.object(egress, "transport", return_value="socks5"), \
                patch.object(egress, "status", return_value=self.document):
            self.assertIsNone(egress.exit_probe_context(self.inst, self.settings))

    def test_all_existing_proxy_modes_can_be_monitored(self):
        for mode in ("subscription", "profile", "existing", "manual"):
            self.document["exits"]["gb"]["mode"] = mode
            self.assertIn("proxy_host", self.context(), mode)

    def test_disabled_and_direct_routes_do_not_probe(self):
        self.settings["proxy"]["enabled"] = False
        self.assertIsNone(self.context())
        self.settings["proxy"]["enabled"] = True
        self.settings["proxy"]["exits"]["gb"]["mode"] = "direct"
        self.assertIsNone(self.context())

    def test_stale_mismatched_unready_and_malformed_routes_are_unknown(self):
        for mutate in (
            lambda doc: doc.update(updated_at=time.time() - 100),
            lambda doc: doc.update(config_fingerprint="old"),
            lambda doc: doc["exits"]["gb"].update(ready=False),
            lambda doc: doc["exits"]["gb"].update(proxy_host="socks5://user:secret@host"),
            lambda doc: doc["exits"]["gb"].update(node=""),
        ):
            saved = copy.deepcopy(self.document)
            mutate(self.document)
            self.assertNotIn("proxy_host", self.context())
            self.document = saved

    def test_node_and_configuration_changes_invalidate_identity(self):
        before = self.context()
        self.document["exits"]["gb"]["node"] = "replacement"
        self.assertNotEqual(before, self.context())
        self.settings["proxy"]["exits"]["gb"]["pinned_node"] = "replacement"
        self.assertNotIn("proxy_host", self.context())

    def test_host_transport_requires_a_fresh_matching_configuration(self):
        self.assertIn("proxy_host", self.context("host"))
        self.document.pop("config_fingerprint")
        self.assertNotIn("proxy_host", self.context("host"))


class ProxyMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.monitor = proxy_health.Monitor()
        self.context = {"country": "gb", "proxy_host": "mdd-egress", "proxy_port": 1080,
                        "node": "one", "config_fingerprint": "current"}
        self.current = dict(self.context)

    async def asyncTearDown(self):
        await self.monitor.close()

    def age(self, seconds):
        self.monitor.results[self.monitor.key(self.context)]["completed"] -= seconds

    async def refresh(self, **kw):
        return await self.monitor.refresh(self.context, lambda: self.current, **kw)

    async def test_two_separate_failed_rounds_warn_and_a_success_clears(self):
        with patch.object(egress, "test_udp_proxy", side_effect=egress.EgressError("secret")) as probe:
            first = await self.refresh()
            self.assertEqual(first["state"], "suspect")
            self.assertEqual((await self.refresh(force=True))["state"], "suspect")
            self.assertEqual(probe.call_count, 1)
            self.age(31)
            self.assertEqual((await self.refresh())["state"], "failed")
        self.age(11)
        with patch.object(egress, "test_udp_proxy", return_value=12):
            self.assertEqual((await self.refresh(force=True))["state"], "passed")
        self.assertEqual(self.monitor.results[self.monitor.key(self.context)]["failures"], 0)

    async def test_concurrent_lines_and_manual_checks_share_one_probe(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocking(*args):
            entered.set()
            await release.wait()
        with patch.object(proxy_health.asyncio, "to_thread", side_effect=blocking) as probe:
            first = asyncio.create_task(self.refresh())
            await entered.wait()
            second = asyncio.create_task(self.refresh(force=True))
            await asyncio.sleep(0)
            self.assertEqual(self.monitor.snapshot(self.context)["state"], "checking")
            release.set()
            a, b = await asyncio.gather(first, second)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(a["state"], "passed")
        self.assertEqual(a, b)

    async def test_successful_exits_wait_ten_minutes_and_stay_fresh_between_checks(self):
        with patch.object(egress, "test_udp_proxy", return_value=12) as probe:
            await self.refresh()
            self.age(599)
            self.assertEqual(self.monitor.snapshot(self.context)["state"], "passed")
            await self.refresh()
            self.assertEqual(probe.call_count, 1)
            self.age(2)
            self.assertEqual(self.monitor.snapshot(self.context)["state"], "passed")
            await self.refresh()
            self.assertEqual(probe.call_count, 2)
            self.age(721)
            self.assertEqual(self.monitor.snapshot(self.context)["state"], "unknown")

    async def test_first_failure_confirms_in_thirty_seconds_then_checks_recovery_each_minute(self):
        with patch.object(egress, "test_udp_proxy", side_effect=TimeoutError()) as probe:
            self.assertEqual((await self.refresh())["state"], "suspect")
            self.age(29)
            await self.refresh()
            self.assertEqual(probe.call_count, 1)
            self.age(2)
            self.assertEqual((await self.refresh())["state"], "failed")
            self.assertEqual(probe.call_count, 2)
            self.age(59)
            await self.refresh()
            self.assertEqual(probe.call_count, 2)
        self.age(2)
        with patch.object(egress, "test_udp_proxy", return_value=12) as probe:
            self.assertEqual((await self.refresh())["state"], "passed")
            self.age(61)
            await self.refresh()
            self.assertEqual(probe.call_count, 1)

    async def test_switch_during_probe_discards_both_success_and_failure(self):
        for failure in (False, True):
            async def switch(*args):
                self.current = {**self.context, "node": "new"}
                if failure:
                    raise egress.EgressError("private endpoint")
            self.current = self.context
            with patch.object(proxy_health.asyncio, "to_thread", side_effect=switch):
                self.assertIsNone(await self.refresh())
            self.assertEqual(self.monitor.snapshot(self.current)["state"], "unknown")
            self.assertFalse(self.monitor.results)

    async def test_old_results_expire_and_do_not_count_as_consecutive_failures(self):
        with patch.object(egress, "test_udp_proxy", side_effect=TimeoutError()):
            await self.refresh()
            self.age(181)
            self.assertEqual(self.monitor.snapshot(self.context)["state"], "unknown")
            self.assertEqual((await self.refresh())["state"], "suspect")

    async def test_collector_errors_and_missing_endpoints_are_not_proxy_faults(self):
        with patch.object(egress, "test_udp_proxy", side_effect=RuntimeError("secret")):
            result = await self.refresh()
        self.assertEqual(result["state"], "unknown")
        self.assertNotIn("secret", str(result))
        with patch.object(egress, "test_udp_proxy") as probe:
            self.assertIsNone(await self.monitor.refresh(None, lambda: None))
            self.assertEqual((await self.monitor.refresh({"country": "gb"}, lambda: {}))["state"], "unknown")
        probe.assert_not_called()

    async def test_cancelling_a_request_does_not_cancel_shared_probe_or_leave_it_cached(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocking(*args):
            entered.set()
            await release.wait()
        with patch.object(proxy_health.asyncio, "to_thread", side_effect=blocking):
            request = asyncio.create_task(self.refresh())
            await entered.wait()
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            release.set()
            await asyncio.gather(*self.monitor.tasks.values())
            await asyncio.sleep(0)
        self.assertFalse(self.monitor.tasks)
        self.assertEqual(self.monitor.snapshot(self.context)["state"], "passed")


class ProxyWarningIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.inst = {"id": "proxy-warning-test", "enabled": True}
        self.base = {"state": "OK", "label": "IMS registered", "reason_code": "ok",
                     "detail": {"registration": "Registered"}}
        self.warning = {"state": "failed", "checked_at": 123, "checking": False}

    def tearDown(self):
        main.hub.reset_health(self.inst["id"])

    def test_warning_preserves_registration_and_does_not_schedule_recovery(self):
        st = proxy_health.annotate(self.base, self.warning)
        self.assertEqual(st["state"], "OK")
        self.assertEqual(st["detail"]["registration"], "Registered")
        with patch.object(main.engine, "capture_and_stop") as stop, \
                patch.object(main.egress, "request_reselect") as reselect:
            result = main.apply_health(self.inst["id"], self.inst, st, "generation-1")
        self.assertEqual(result["retry"]["count"], 0)
        stop.assert_not_called()
        reselect.assert_not_called()
        activity = main._with_status_activity(self.inst["id"], st)
        self.assertIn("proxy", activity["activity"]["next"])

    def test_device_snapshots_and_warnings_agree_and_recovery_clears_text(self):
        st = proxy_health.annotate(self.base, self.warning)
        native = device_state.native_vowifi_capability(True, True, st)
        modem = main._vowifi_capability(True, {"actual": {"vowifi_bridge_active": True}}, True, st)
        for view in (native, modem):
            self.assertEqual(view["actual"], "degraded")
            self.assertEqual(view["reason"], proxy_health.REASON)
        cleared = proxy_health.annotate(st, {"state": "passed", "checked_at": 124})
        self.assertEqual(cleared["label"], "IMS registered")
        self.assertEqual(cleared["reason_code"], "ok")
        self.assertEqual(device_state.native_vowifi_capability(True, True, cleared)["actual"], "on")

    def test_proxy_warning_does_not_hide_sim_registration_or_stopped_errors(self):
        for state in ("STOPPED", "NO_CARD", "REGISTERING", "ERROR"):
            original = {**self.base, "state": state, "label": "original", "reason_code": "original"}
            annotated = proxy_health.annotate(original, self.warning)
            self.assertEqual(annotated["label"], "original")
            self.assertEqual(annotated["reason_code"], "original")

    async def test_manual_recheck_is_admin_only_and_uses_current_line_exit(self):
        path = "/api/instances/1/proxy-health/check"
        self.assertFalse(authz.allowed(gate.Principal("client"), "POST", path))
        self.assertTrue(authz.allowed(gate.Principal("admin"), "POST", path))
        context = {"country": "gb", "proxy_host": "mdd-egress", "proxy_port": 1080}
        with patch.object(main.cfg, "get_instance", return_value=self.inst), \
                patch.object(main, "_cached_line_status", return_value=self.base), \
                patch.object(main, "_proxy_context", return_value=context), \
                patch.object(main.hub.proxy_health, "refresh", new=AsyncMock()) as refresh, \
                patch.object(main.hub.proxy_health, "snapshot", return_value=self.warning):
            result = await main.api_proxy_health_check(self.inst["id"])
        self.assertEqual(result["proxy_health"], self.warning)
        self.assertTrue(refresh.call_args.kwargs["force"])

    async def test_background_monitor_probes_registered_lines_and_deduplicates_exits(self):
        lines = [self.inst, {"id": "second", "enabled": True},
                 {"id": "disabled", "enabled": False}, {"id": "stopped", "enabled": True}]
        context = {"country": "gb", "proxy_host": "mdd-egress", "proxy_port": 1080}
        cache = {line["id"]: self.base for line in lines}
        cache["stopped"] = {"state": "STOPPED"}
        with patch.object(main.cfg, "list_instances", return_value=lines), \
                patch.object(main.hub, "status_cache", cache), \
                patch.object(main, "_proxy_context", return_value=context), \
                patch.object(main.hub.proxy_health, "prune"), \
                patch.object(main.hub.proxy_health, "refresh", new=AsyncMock()) as refresh, \
                patch.object(main.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await main.proxy_health_poller()
        refresh.assert_awaited_once()
        self.assertEqual(refresh.call_args.args[0], context)

    def test_cached_http_status_uses_new_exit_identity_without_network_io(self):
        old = proxy_health.annotate(self.base, self.warning)
        monitor = proxy_health.Monitor()
        with patch.object(main.hub, "status_cache", {self.inst["id"]: old}), \
                patch.object(main.hub, "proxy_health", monitor), \
                patch.object(main, "_proxy_context", return_value={"country": "gb", "node": "new"}), \
                patch.object(egress, "test_udp_proxy") as probe:
            result = main._cached_line_status(self.inst)
        self.assertEqual(result["proxy_health"]["state"], "unknown")
        self.assertEqual(result["label"], "IMS registered")
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
