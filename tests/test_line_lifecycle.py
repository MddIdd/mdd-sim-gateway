import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from control.app import config, engine, main


_lifecycle_patcher = None


def setUpModule():
    # Lifecycle persistence itself is covered against a TemporaryDirectory below. Other tests
    # exercise policy decisions and must never write their fictional line ids into repo data.
    global _lifecycle_patcher
    _lifecycle_patcher = patch.object(main, "_record_lifecycle")
    _lifecycle_patcher.start()


def tearDownModule():
    if _lifecycle_patcher is not None:
        _lifecycle_patcher.stop()


class DeletedCardSuppressionTests(unittest.TestCase):
    def test_deleted_inserted_card_stays_suppressed_until_physical_removal(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = str(Path(temp) / "config.yaml")
            with patch.multiple(config, DATA_DIR=temp, CONFIG_PATH=config_path):
                config.suppress_card_until_removal("test-iccid")
                self.assertTrue(config.card_auto_create_suppressed("test-iccid"))
                self.assertNotIn("test-iccid", Path(config_path).read_text())
                config.unsuppress_card("test-iccid")
                self.assertFalse(config.card_auto_create_suppressed("test-iccid"))

    def test_engine_instance_data_delete_is_scoped_to_one_line(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "instances" / "line-1" / "run"
            other = Path(temp) / "instances" / "line-2" / "run"
            target.mkdir(parents=True)
            other.mkdir(parents=True)
            (target / "instance.json").write_text("secret")
            (other / "keep").write_text("keep")
            with patch.object(engine, "DATA_DIR", temp):
                self.assertTrue(engine.delete_instance_data("line-1"))
            self.assertFalse(target.parent.exists())
            self.assertTrue((other / "keep").exists())

    def test_lifecycle_records_are_bounded_and_reject_free_text(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(engine, "DATA_DIR", temp), \
                patch.object(engine, "HOST_DATA_DIR", temp), \
                patch.object(engine, "LIFECYCLE_RECORDS", 3):
            Path(temp, "instances", "line-1", "logs").mkdir(parents=True)
            for index in range(5):
                engine.record_lifecycle(
                    "line-1", "recovery_failed", reason_code="engine_start_failed",
                    retry_count=index, card_present=True)
            path = Path(temp, "instances", "line-1", "logs", "lifecycle.jsonl")
            records = [json.loads(line) for line in path.read_text().splitlines()]
            with self.assertRaises(ValueError):
                engine.record_lifecycle(
                    "line-1", "recovery_failed", reason_code="path=/private/secret")
            with self.assertRaises(ValueError):
                engine.record_lifecycle(
                    "../outside", "recovery_failed", reason_code="engine_start_failed")

        self.assertEqual(len(records), 3)
        self.assertEqual([item["retry_count"] for item in records], [2, 3, 4])
        self.assertEqual(set(records[-1]), {
            "ts", "instance", "event", "reason_code", "retry_count", "card_present"})

    def test_lifecycle_keeps_the_sip_status_that_condemned_the_line(self):
        # Issue #33: a reg_rejected freeze reached the support bundle without the SIP code
        # that caused it, because every log holding it had already rotated.
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(engine, "DATA_DIR", temp), \
                patch.object(engine, "HOST_DATA_DIR", temp):
            Path(temp, "instances", "line-1", "logs").mkdir(parents=True)
            engine.record_lifecycle(
                "line-1", "recovery_scheduled", reason_code="reg_rejected",
                delay_seconds=120, sip_status=403)
            engine.record_lifecycle(
                "line-1", "recovery_scheduled", reason_code="reg_rejected",
                delay_seconds=120, sip_status=None)
            engine.record_lifecycle(
                "line-1", "recovery_scheduled", reason_code="reg_rejected",
                delay_seconds=120, sip_status=12345)
            path = Path(temp, "instances", "line-1", "logs", "lifecycle.jsonl")
            records = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(records[0]["sip_status"], 403)
        self.assertNotIn("sip_status", records[1])
        self.assertEqual(records[2]["sip_status"], 999)

    def test_lifecycle_never_recreates_a_deleted_instance_directory(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(engine, "DATA_DIR", temp), \
                patch.object(engine, "HOST_DATA_DIR", temp):
            engine.record_lifecycle(
                "deleted", "recovery_cancelled", reason_code="line_deleted")
            self.assertFalse(Path(temp, "instances", "deleted").exists())


class LineDeleteApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_line_can_delete_history_and_pause_inserted_card(self):
        inst = {"id": "1", "iccid": "test-iccid"}
        card = {"present": True, "matched": "1", "iccid": "test-iccid"}
        with patch.object(main.cfg, "get_instance", return_value=inst), \
                patch.object(main.hub, "cards_list", return_value=[card]), \
                patch.object(main.cfg, "suppress_card_until_removal") as suppress, \
                patch.object(main.engine, "stop"), \
                patch.object(main.hub, "drop_ami", new=AsyncMock()), \
                patch.object(main.cfg, "delete_instance") as delete_config, \
                patch.object(main.engine, "delete_instance_data") as delete_data, \
                patch.object(main.store, "clear_messages", return_value=12) as clear_messages, \
                patch.object(main.store, "clear_calls", return_value=3) as clear_calls, \
                patch.object(main.store, "clear_line_states", return_value=7) as clear_states, \
                patch.object(main, "_refresh_card_matches"), \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            result = await main.api_instance_delete("1", delete_history=True, confirm_id="1")

        suppress.assert_called_once_with("test-iccid")
        delete_config.assert_called_once_with("1")
        delete_data.assert_called_once_with("1")
        clear_messages.assert_called_once_with("1")
        clear_calls.assert_called_once_with("1")
        clear_states.assert_called_once_with("1")
        self.assertTrue(result["history_deleted"])

    async def test_delete_line_can_preserve_history(self):
        inst = {"id": "1", "iccid": ""}
        with patch.object(main.cfg, "get_instance", return_value=inst), \
                patch.object(main.hub, "cards_list", return_value=[]), \
                patch.object(main.engine, "stop"), \
                patch.object(main.hub, "drop_ami", new=AsyncMock()), \
                patch.object(main.cfg, "delete_instance"), \
                patch.object(main.engine, "delete_instance_data"), \
                patch.object(main.store, "clear_messages") as clear_messages, \
                patch.object(main.store, "clear_calls") as clear_calls, \
                patch.object(main.store, "clear_line_states") as clear_states, \
                patch.object(main, "_refresh_card_matches"), \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            result = await main.api_instance_delete("1", delete_history=False, confirm_id="1")

        clear_messages.assert_not_called()
        clear_calls.assert_not_called()
        self.assertFalse(result["history_deleted"])

    async def test_delete_requires_exact_line_id_confirmation(self):
        with self.assertRaises(main.HTTPException) as raised:
            await main.api_instance_delete("line-2", confirm_id="line-1")
        self.assertEqual(raised.exception.status_code, 400)

    async def test_delete_duplicate_line_starts_surviving_owner(self):
        inst = {"id": "old", "iccid": "same-card"}
        replacement = {"id": "2", "iccid": "same-card", "enabled": True}
        card = {"present": True, "matched": "old", "iccid": "same-card"}
        with patch.object(main.cfg, "get_instance", return_value=inst), \
                patch.object(main.cfg, "list_instances", return_value=[inst, replacement]), \
                patch.object(main.hub, "cards_list", return_value=[card]), \
                patch.object(main.cfg, "suppress_card_until_removal") as suppress, \
                patch.object(main.engine, "stop"), \
                patch.object(main.hub, "drop_ami", new=AsyncMock()), \
                patch.object(main.cfg, "delete_instance"), \
                patch.object(main.engine, "delete_instance_data"), \
                patch.object(main.store, "clear_line_states"), \
                patch.object(main, "_refresh_card_matches"), \
                patch.object(main, "_auto_start_hotplugged_line", new=AsyncMock()) as start, \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            await main.api_instance_delete("old", delete_history=False, confirm_id="old")
            await __import__('asyncio').sleep(0)

        suppress.assert_not_called()
        start.assert_awaited_once_with("2")


class BackgroundStartGuardTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        for iid in ("1", "2", "offline", "removed"):
            main.hub.reset_health(iid)

    def test_saved_line_without_a_live_card_is_not_eligible(self):
        inst = {"id": "offline", "iccid": "saved-card", "enabled": True}
        with patch.object(main.hub, "cards_list", return_value=[]):
            self.assertEqual(main._line_auto_start_allowed(inst), (False, "no_card"))

    def test_device_vowifi_switch_blocks_background_start(self):
        inst = {"id": "offline", "iccid": "saved-card", "enabled": True}
        card = {"present": True, "iccid": "saved-card", "hardware_id": "device-1",
                "hardware_kind": "modem"}
        desired = {"defaults": {"vowifi_enabled": True},
                   "devices": {"device-1": {"vowifi_enabled": False}}}
        with patch.object(main.hub, "cards_list", return_value=[card]), \
                patch.object(main.device_state, "desired", return_value=desired):
            self.assertEqual(
                main._line_auto_start_allowed(inst), (False, "vowifi_disabled"))

    def test_new_device_default_does_not_disable_an_enabled_native_reader_line(self):
        inst = {"id": "offline", "iccid": "saved-card", "enabled": True}
        card = {"present": True, "iccid": "saved-card", "name": "USB reader",
                "reader_port": "usb:1-2", "hardware_kind": "reader"}
        desired = {"defaults": {"vowifi_enabled": False}, "devices": {}}
        with patch.object(main.hub, "cards_list", return_value=[card]), \
                patch.object(main, "_device_for_card", return_value=("reader-1", "reader")), \
                patch.object(main.device_state, "desired", return_value=desired) as read_desired:
            self.assertEqual(main._line_auto_start_allowed(inst), (True, ""))
        read_desired.assert_not_called()

    async def test_auto_recovery_rechecks_a_transiently_absent_card(self):
        inst = {"id": "offline", "iccid": "saved-card", "enabled": True}
        main.hub.health_for("offline").update({
            "frozen_code": "tunnel_sim_auth", "frozen_reason": "failed",
            "auto_retrying": True, "retry_count": 3,
        })
        started = main.time.monotonic()
        with patch.object(main, "_line_auto_start_allowed",
                          return_value=(False, "no_card")), \
                patch.object(main, "_start_engine_checked") as start, \
                patch.object(main, "_record_lifecycle") as lifecycle, \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            await main._auto_recover_instance("offline", inst, 60)

        start.assert_not_called()
        self.assertEqual(main.hub.status_cache["offline"]["state"], "NO_CARD")
        health = main.hub.health["offline"]
        self.assertEqual(health["frozen_code"], "tunnel_sim_auth")
        self.assertFalse(health["auto_retrying"])
        self.assertGreaterEqual(health["next_retry_at"], started + 60)
        lifecycle.assert_called_once_with(
            "offline", "recovery_blocked", "no_card", retry_count=3,
            delay_seconds=60, card_present=False)

        # The sampled cache can remain absent for hours. Rechecks re-arm recovery but must not
        # fill the bounded audit with an identical blocked record every cooldown.
        health["auto_retrying"] = True
        await main._auto_recover_instance("offline", inst, 60)
        lifecycle.assert_called_once()

    async def test_disabled_vowifi_still_cancels_pending_recovery(self):
        inst = {"id": "offline", "iccid": "saved-card", "enabled": True}
        main.hub.health_for("offline").update({
            "frozen_code": "tunnel_network", "frozen_reason": "failed",
            "auto_retrying": True, "next_retry_at": 12345,
        })
        with patch.object(main, "_line_auto_start_allowed",
                          return_value=(False, "vowifi_disabled")), \
                patch.object(main, "_start_engine_checked") as start, \
                patch.object(main, "_record_lifecycle") as lifecycle, \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            await main._auto_recover_instance("offline", inst, 60)

        start.assert_not_called()
        health = main.hub.health["offline"]
        self.assertIsNone(health["frozen_code"])
        self.assertIsNone(health["next_retry_at"])
        self.assertEqual(main.hub.status_cache["offline"]["state"], "STOPPED")
        lifecycle.assert_called_once_with(
            "offline", "recovery_cancelled", "vowifi_disabled",
            retry_count=0, card_present=True)

    async def test_hardware_imei_recovery_failure_is_structured_without_exception_text(self):
        inst = {"id": "offline", "iccid": "saved-card", "enabled": True,
                "imei_source_device_id": "modem-a"}
        main.hub.health_for("offline").update({
            "frozen_code": "tunnel_network", "frozen_reason": "failed",
            "auto_retrying": True, "retry_count": 3,
        })
        failure = main.HTTPException(409, {
            "code": "hardware_imei_required",
            "message": "private path /dev/ttyUSB2 and 123456789012345",
            "device_id": "modem-a",
        })
        with patch.object(main, "_line_auto_start_allowed", return_value=(True, "")), \
                patch.object(main, "_start_engine_checked", side_effect=failure), \
                patch.object(main, "_record_lifecycle") as lifecycle, \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            await main._auto_recover_instance("offline", inst, 60)

        self.assertEqual(lifecycle.call_count, 2)
        failed = lifecycle.call_args_list[-1]
        self.assertEqual(failed.args, (
            "offline", "recovery_failed", "hardware_imei_required"))
        self.assertEqual(failed.kwargs["imei_valid"], False)
        self.assertEqual(failed.kwargs["imei_source_matches"], True)
        self.assertNotIn("private", str(failed))

    async def test_card_removal_cancels_a_pending_recovery_without_a_container(self):
        inst = {"id": "removed", "iccid": "saved-card"}
        main.hub.health_for("removed").update({
            "frozen_code": "tunnel_sim_auth", "next_retry_at": 12345,
        })
        entry = {"name": "reader", "index": 2, "matched": "removed",
                 "iccid": "saved-card"}
        with patch.object(main.cfg, "unsuppress_card"), \
                patch.object(main.cfg, "get_instance", return_value=inst), \
                patch.object(main, "_record_lifecycle") as lifecycle, \
                patch.object(main.engine, "is_running", return_value=False):
            stopped = await main._on_card_remove(entry)

        self.assertFalse(stopped)
        health = main.hub.health["removed"]
        self.assertIsNone(health["frozen_code"])
        self.assertIsNone(health["next_retry_at"])
        lifecycle.assert_called_once_with(
            "removed", "recovery_cancelled", "no_card",
            retry_count=0, card_present=False)

    async def test_maintenance_restart_only_recreates_the_running_snapshot(self):
        running = {"id": "1", "enabled": True}
        offline = {"id": "2", "enabled": True}

        def is_running(iid):
            return str(iid) == "1"

        with patch.object(main.cfg, "list_instances", return_value=[running, offline]), \
                patch.object(main.cfg, "get_settings", return_value={}), \
                patch.object(main.engine, "is_running", side_effect=is_running), \
                patch.object(main.engine, "stop") as stop, \
                patch.object(main, "_start_engine_checked") as start, \
                patch.object(main.hub, "drop_ami", new=AsyncMock()):
            result = await main.api_system_maintenance({"action": "restart_lines"})

        self.assertEqual(result["restarted"], ["1"])
        stop.assert_called_once_with("1")
        start.assert_called_once_with(running, {}, dev_mounts=False)

    async def test_maintenance_can_prune_only_dangling_build_cache(self):
        main.hub.host_snapshot = {"disk": {"used_percent": 99.0}}
        main.hub.host_alerts = [{"code": "disk_critical"}]
        with patch.object(main.operations, "prune_dangling_build_cache",
                          return_value={"ok": True, "space_reclaimed_bytes": 4096}) as prune, \
                patch.object(main.sysinfo, "collect",
                             return_value={"disk": {"used_percent": 70.0}}):
            result = await main.api_system_maintenance({"action": "prune_build_cache"})
        self.assertEqual(result["space_reclaimed_bytes"], 4096)
        self.assertEqual(result["action"], "prune_build_cache")
        self.assertEqual(main.hub.host_snapshot, {"disk": {"used_percent": 70.0}})
        self.assertEqual(main.hub.host_alerts, [])
        prune.assert_called_once_with()

    async def test_build_cache_prune_failure_is_an_actionable_http_error(self):
        with patch.object(main.operations, "prune_dangling_build_cache",
                          side_effect=RuntimeError("Docker refused builder prune")):
            with self.assertRaises(main.HTTPException) as raised:
                await main.api_system_maintenance({"action": "prune_build_cache"})
        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("Docker refused", raised.exception.detail)

    async def test_maintenance_can_drop_old_mdd_images_on_explicit_request(self):
        with patch.object(main.operations, "prune_old_mdd_images", return_value={
                "ok": True, "removed_images": 2, "space_reclaimed_bytes": 8192}) as prune, \
                patch.object(main.sysinfo, "collect", return_value={"disk": {}}):
            result = await main.api_system_maintenance({"action": "prune_old_images"})
        self.assertEqual(result["action"], "prune_old_images")
        self.assertEqual(result["removed_images"], 2)
        prune.assert_called_once_with()


class StatusActivityTests(unittest.TestCase):
    def test_frozen_status_explains_countdown_and_next_action(self):
        main.hub.health["activity-test"] = {
            "auto_retrying": False, "fail_start": None, "retry_count": 3,
            "frozen_code": "registering", "frozen_reason": "IMS unavailable",
            "next_retry_at": None, "last_state": None,
        }
        status = main._with_status_activity("activity-test", {
            "state": "ERROR", "label": "Failed", "reason": "IMS unavailable",
            "detail": {}, "frozen": True, "automatic_retry_in": 18,
            "retry": {"count": 3, "max": 3},
        })
        self.assertEqual(status["activity"]["seconds"], 18)
        self.assertIn("rebuilt", status["activity"]["next"])
        main.hub.health.pop("activity-test", None)

    def test_permanent_pin_freeze_explains_required_manual_action(self):
        main.hub.health["pin-test"] = {
            "auto_retrying": False, "fail_start": None, "retry_count": 3,
            "frozen_code": "pin_wrong", "frozen_reason": "SIM PIN is incorrect.",
            "next_retry_at": None, "last_state": None,
        }
        status = main._with_status_activity("pin-test", {
            "state": "ERROR", "label": "Failed", "reason_code": "pin_wrong",
            "reason": "SIM PIN is incorrect.", "detail": {}, "frozen": True,
            "automatic_retry_in": None, "retry": {"count": 3, "max": 3},
        })
        self.assertFalse(status["activity"]["automatic"])
        self.assertIn("PIN", status["activity"]["next"])
        main.hub.health.pop("pin-test", None)


class OfflineDeviceStatusTests(unittest.IsolatedAsyncioTestCase):
    def test_quiet_stopped_lines_do_not_hold_gateway_on_fast_polling(self):
        instances = [{"id": "ok", "enabled": True}, {"id": "away", "enabled": True}]
        main.hub.status_cache["ok"] = {"state": "OK"}
        main.hub.status_cache["away"] = {"state": "STOPPED"}
        self.assertEqual(main._status_poll_delay(instances), main.STATUS_POLL_HEALTHY_SECONDS)
        main.hub.status_cache.pop("ok", None)
        main.hub.status_cache.pop("away", None)

    def test_registering_line_keeps_fast_polling(self):
        instances = [{"id": "starting", "enabled": True}]
        main.hub.status_cache["starting"] = {"state": "REGISTERING"}
        self.assertEqual(main._status_poll_delay(instances), main.STATUS_POLL_FAST_SECONDS)
        main.hub.status_cache.pop("starting", None)

    async def test_ims_rejection_uses_retry_budget_before_cooldown_rebuild(self):
        main.hub.health.pop("3", None)

    async def test_unanswered_ims_with_no_call_uses_generation_safe_fast_recovery(self):
        iid = "fast-unanswered"
        main.hub.reset_health(iid)
        main.hub.reg_unanswered_recovery_at.pop(iid, None)
        unanswered = {
            "state": "REGISTERING", "label": "Registering to IMS",
            "reason_code": "reg_unanswered", "reason": "Carrier IMS did not answer.",
            "detail": {"registration": "Rejected", "active_channels": 0},
        }
        inst = {"id": iid, "enabled": True, "retry": {"max": 3, "interval": 30}}
        started = main.time.monotonic()
        with patch.object(main.engine, "capture_and_stop") as capture_and_stop, \
                patch.object(main, "_judge_exit_failure", return_value=main.failover.HOLD), \
                patch.object(main.hub, "drop_ami", new=AsyncMock()) as drop_ami:
            result = main.apply_health(iid, inst, unanswered, "generation-1")
            for _ in range(20):
                await __import__("asyncio").sleep(0.01)
                if capture_and_stop.called and drop_ami.await_count:
                    break

        self.assertTrue(result["frozen"])
        self.assertEqual(result["reason_code"], "reg_unanswered")
        self.assertGreaterEqual(result["automatic_retry_in"], 9)
        self.assertLessEqual(result["automatic_retry_in"], 10)
        capture_and_stop.assert_called_once_with(
            iid, inst, "health-freeze:reg_unanswered", "generation-1")
        drop_ami.assert_awaited_once_with(iid)
        self.assertGreaterEqual(main.hub.reg_unanswered_recovery_at[iid], started)
        main.hub.reset_health(iid)
        main.hub.reg_unanswered_recovery_at.pop(iid, None)

    async def test_unanswered_ims_with_active_or_unknown_calls_keeps_slow_path(self):
        inst = {"id": "guarded", "enabled": True,
                "retry": {"max": 3, "interval": 30}}
        for channels in (1, None):
            with self.subTest(active_channels=channels):
                main.hub.reset_health("guarded")
                main.hub.reg_unanswered_recovery_at.pop("guarded", None)
                st = {
                    "state": "REGISTERING", "label": "Registering to IMS",
                    "reason_code": "reg_unanswered", "reason": "No response.",
                    "detail": {"registration": "Rejected", "active_channels": channels},
                }
                with patch.object(main.engine, "capture_and_stop") as capture:
                    result = main.apply_health("guarded", inst, st, "generation-1")
                self.assertNotIn("frozen", result)
                self.assertEqual(result["retry"], {"count": 1, "max": 3})
                capture.assert_not_called()
        main.hub.reset_health("guarded")

    async def test_unanswered_fast_recovery_is_rate_limited_per_line(self):
        main._record_lifecycle.reset_mock()
        iid = "rate-limited"
        main.hub.reset_health(iid)
        main.hub.reg_unanswered_recovery_at[iid] = main.time.monotonic()
        st = {
            "state": "REGISTERING", "label": "Registering to IMS",
            "reason_code": "reg_unanswered", "reason": "No response.",
            "detail": {"registration": "Rejected", "active_channels": 0},
        }
        inst = {"id": iid, "enabled": True, "retry": {"max": 3, "interval": 30}}
        with patch.object(main.engine, "capture_and_stop") as capture:
            result = main.apply_health(iid, inst, st, "generation-2")
        self.assertNotIn("frozen", result)
        capture.assert_not_called()
        main.hub.reset_health(iid)
        main.hub.reg_unanswered_recovery_at.pop(iid, None)
        main.hub.status_cache.pop("3", None)
        rejected = {"state": "REGISTERING", "label": "Registering to IMS",
                    "reason_code": "reg_rejected", "reason": "Carrier rejected IMS.",
                    "detail": {"registration": "Rejected"}}
        started = main.time.monotonic()
        # Freezing now snapshots the container before removing it: the evidence of why the
        # line failed is destroyed by the removal, and a rebuild loop would otherwise erase
        # it every couple of minutes. capture_and_stop() does both, off the event loop.
        with patch.object(main.engine, "capture_and_stop") as capture_and_stop, \
                patch.object(main.egress, "request_reselect", return_value="") as reselect, \
                patch.object(main.egress, "status", return_value={"exits": {"us": {
                    "node": "n1", "candidates": ["n1", "n2"], "selection": "auto"}}}), \
                patch.object(main.egress, "line_country", return_value="us"), \
                patch.object(main.engine, "read_run_json",
                             return_value={"state": "CONNECTED"}), \
                patch.object(main.engine, "ike_evidence", return_value={"retransmits": 0}), \
                patch.object(main, "_save_exit_ledgers"), \
                patch.object(main.hub, "drop_ami", new=AsyncMock()) as drop_ami:
            inst = {"id": "3", "enabled": True,
                    "retry": {"max": 3, "interval": 30}}
            first = main.apply_health("3", inst, rejected)
            main.hub.health["3"]["fail_start"] = started - 91
            exhausted = main.apply_health("3", inst, rejected)
            for _ in range(20):
                await __import__("asyncio").sleep(0.01)
                if capture_and_stop.called:
                    break

        self.assertNotIn("frozen", first)
        self.assertEqual(first["retry"], {"count": 1, "max": 3})
        self.assertTrue(exhausted["frozen"])
        self.assertEqual(exhausted["reason_code"], "reg_rejected")
        self.assertGreaterEqual(exhausted["automatic_retry_in"], 119)
        self.assertLessEqual(exhausted["automatic_retry_in"], 120)
        self.assertGreaterEqual(main.hub.health["3"]["next_retry_at"] - started, 119)
        capture_and_stop.assert_called_once()
        self.assertEqual(capture_and_stop.call_args.args[0], "3")
        self.assertIn("reg_rejected", capture_and_stop.call_args.args[2])
        self.assertIsNone(capture_and_stop.call_args.args[3])
        main._record_lifecycle.assert_called_once()
        lifecycle_call = main._record_lifecycle.call_args
        self.assertEqual(
            lifecycle_call.args,
            ("3", "recovery_scheduled", "reg_rejected"))
        self.assertEqual(lifecycle_call.kwargs["retry_count"], 3)
        self.assertGreaterEqual(lifecycle_call.kwargs["delay_seconds"], 119)
        self.assertLessEqual(lifecycle_call.kwargs["delay_seconds"], 120)
        # The exit is NOT asked to move. A carrier that answers registration with a rejection
        # says nothing about the path its packets took, and moving on that evidence is what
        # made a healthy pool churn: measured over fifty freezes, the node blamed most often
        # went on to carry this same line for eight uninterrupted hours.
        reselect.assert_not_called()
        drop_ami.assert_awaited_once_with("3")
        main.hub.exit_ledgers.pop("3", None)
        main.hub.health.pop("3", None)

    async def test_repeated_vowifi_on_request_restarts_stopped_modem_line(self):
        line = {"id": "3", "name": "Giff", "enabled": False}
        device = {"id": "modem-a", "device_type": "modem", "instance_id": "3"}
        desired = {"devices": {"modem-a": {
            "cellular_enabled": True, "vowifi_enabled": True, "flight_mode": False}}}
        observed = {"devices": {"modem-a": {"present": True}}}
        with patch.object(main, "_unified_devices", new=AsyncMock(return_value=[device])), \
                patch.object(main, "_device_sources", return_value=(desired, observed, {})), \
                patch.object(main, "_device_identities", return_value={}), \
                patch.object(main.hub, "cards_list", return_value=[]), \
                patch.object(main, "_instance_for_device", return_value=line), \
                patch.object(main.engine, "is_running", return_value=False), \
                patch.object(main.cfg, "upsert_instance", return_value={**line, "enabled": True}) as save, \
                patch.object(main.device_state, "set_desired") as set_desired, \
                patch.object(main.egress, "publish"), \
                patch.object(main, "_wait_for_device_request", new=AsyncMock()), \
                patch.object(main, "_resume_instances", new=AsyncMock(return_value={})) as resume, \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            result = await main.api_device_capabilities(
                "modem-a", {"vowifi_enabled": True})

        self.assertEqual(result["id"], "modem-a")
        save.assert_called_once_with({"id": "3", "enabled": True})
        set_desired.assert_called_once()
        resume.assert_awaited_once_with({"3"}, set())

    async def test_disabled_line_stops_stale_container_without_auto_recovery(self):
        inst = {"id": "3", "enabled": False}
        with patch.object(main.cfg, "get_instance", return_value=inst), \
                patch.object(main.hub.runtime, "get", new=AsyncMock(return_value={
                    "running": True, "ip": "172.17.0.2", "container_id": "c3"})), \
                patch.object(main.engine, "stop") as stop, \
                patch.object(main.hub, "drop_ami", new=AsyncMock()) as drop_ami, \
                patch.object(main.hub, "broadcast", new=AsyncMock()) as broadcast, \
                patch.object(main.status_mod, "compute", new=AsyncMock()) as compute:
            await main._poll_instance_status(inst)

        stop.assert_called_once_with("3")
        drop_ami.assert_awaited_once_with("3")
        compute.assert_not_awaited()
        self.assertEqual(main.hub.status_cache["3"]["state"], "STOPPED")
        broadcast.assert_awaited_once()
        main.hub.status_cache.pop("3", None)
        main.hub.status_sampled_at.pop("3", None)
        main.hub.health.pop("3", None)

    async def test_stale_disabled_snapshot_does_not_stop_newly_enabled_line(self):
        stale = {"id": "race", "enabled": False}
        current = {"id": "race", "enabled": True}
        sampled = {"state": "REGISTERING", "label": "Registering",
                   "reason_code": "registering", "reason": "Registering.",
                   "detail": {"registration": "unknown"}}
        with patch.object(main.cfg, "get_instance", return_value=current), \
                patch.object(main.hub.runtime, "get", new=AsyncMock(return_value={
                    "running": True, "ip": "172.17.0.2", "container_id": "race"})), \
                patch.object(main.engine, "stop") as stop, \
                patch.object(main.hub, "ami_for", new=AsyncMock(return_value=object())), \
                patch.object(main.status_mod, "compute", new=AsyncMock(return_value=sampled)), \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            await main._poll_instance_status(stale)

        stop.assert_not_called()
        self.assertEqual(main.hub.status_cache["race"]["state"], "REGISTERING")
        main.hub.reset_health("race")

    async def test_reader_enable_is_persisted_before_engine_start(self):
        device = {"id": "reader-a", "device_type": "reader", "instance_id": "7"}
        line = {"id": "7", "enabled": False}
        order = []

        def save(update):
            order.append("save")
            return {**line, **update}

        async def start(_iid):
            order.append("start")
            return {"ok": True}

        with patch.object(main, "_unified_devices", new=AsyncMock(return_value=[device])), \
                patch.object(main.cfg, "get_instance", return_value=line), \
                patch.object(main.engine, "is_running", return_value=False), \
                patch.object(main.cfg, "upsert_instance", side_effect=save), \
                patch.object(main, "api_instance_start", new=AsyncMock(side_effect=start)):
            await main.api_device_capabilities("reader-a", {"vowifi_enabled": True})

        self.assertEqual(order, ["save", "start"])

    async def test_manual_stop_clears_pending_automatic_recovery(self):
        main.hub.health["stop-test"] = {
            "auto_retrying": False, "fail_start": 1, "retry_count": 3,
            "frozen_code": "registering", "frozen_reason": "IMS unavailable",
            "next_retry_at": main.time.monotonic() + 1, "last_state": "REGISTERING",
        }
        main._record_lifecycle.reset_mock()
        with patch.object(main.engine, "stop") as stop, \
                patch.object(main.hub, "drop_ami", new=AsyncMock()) as drop_ami:
            await main.api_instance_stop("stop-test")

        self.assertIsNone(main.hub.health["stop-test"]["frozen_code"])
        self.assertIsNone(main.hub.health["stop-test"]["next_retry_at"])
        self.assertEqual(main.hub.status_cache["stop-test"]["state"], "STOPPED")
        stop.assert_called_once_with("stop-test")
        drop_ami.assert_awaited_once_with("stop-test")
        main._record_lifecycle.assert_called_once_with(
            "stop-test", "recovery_cancelled", "user_requested",
            retry_count=3, card_present=None)
        main.hub.reset_health("stop-test")

    async def test_unknown_registration_only_holds_ok_for_bounded_grace(self):
        iid = "status-grace"
        previous = {"state": "OK", "label": "Working", "reason_code": "ok",
                    "reason": "Registered.", "detail": {"registration": "Registered"}}
        unknown = {"state": "REGISTERING", "label": "Registering",
                   "reason_code": "registering", "reason": "Checking.",
                   "detail": {"registration": "unknown"}}
        sampled_at = main.time.monotonic()
        main.hub.status_cache[iid] = previous
        main.hub.status_sampled_at[iid] = sampled_at
        inst = {"id": iid, "enabled": True}
        with patch.object(main.hub, "ami_for", new=AsyncMock(return_value=object())), \
                patch.object(main.status_mod, "compute", new=AsyncMock(return_value=unknown)), \
                patch.object(main.hub.runtime, "get", new=AsyncMock(return_value={
                    "running": True, "ip": "172.17.0.2", "container_id": "grace"})), \
                patch.object(main.hub, "broadcast", new=AsyncMock()):
            await main._poll_instance_status(inst)
            self.assertEqual(main.hub.status_cache[iid]["state"], "OK")
            self.assertEqual(main.hub.status_sampled_at[iid], sampled_at)

            main.hub.status_sampled_at[iid] = (
                main.time.monotonic() - main.STATUS_OK_GRACE_SECONDS - 1)
            await main._poll_instance_status(inst)

        self.assertEqual(main.hub.status_cache[iid]["state"], "REGISTERING")
        main.hub.reset_health(iid)

    async def test_live_modem_sim_is_present_without_vowifi_bridge_card(self):
        desired = {"devices": {"modem-a": {
            "cellular_enabled": True, "vowifi_enabled": False, "flight_mode": False}}}
        observed = {"devices": {"modem-a": {
            "present": True,
            "actual": {"cellular_radio_enabled": True, "vowifi_bridge_active": False},
            "cellular": {"available": True, "sim_iccid": "live-card",
                         "registration": "roaming", "operator": "Visited Network",
                         "radio_enabled": True, "data_active": True}}}}
        line = {"id": "3", "name": "Home SIM", "iccid": "live-card",
                "mcc": "234", "mnc": "10", "enabled": False}
        with patch.object(main, "_device_sources", return_value=(desired, observed, {})), \
                patch.object(main, "_device_identities", return_value={}), \
                patch.object(main.hub, "cards_list", return_value=[]), \
                patch.object(main.cfg, "list_instances", return_value=[line]), \
                patch.object(main.device_state, "native_reader_devices", return_value={}), \
                patch.object(main.device_state, "hardware", return_value={}), \
                patch.object(main.cfg, "get_settings", return_value={
                    "proxy": {"exits": {}}, "rekey": {"minutes": 30}}), \
                patch.object(main, "_cached_line_status", return_value=None), \
                patch.object(main.egress, "status", return_value={"lines": {}}), \
                patch.object(main.egress, "line_country", return_value="GB"), \
                patch.object(main.egress, "country_for_mcc", return_value="GB"):
            devices = await main._unified_devices()

        self.assertEqual(len(devices), 1)
        device = devices[0]
        self.assertTrue(device["sim"]["present"])
        self.assertEqual(device["sim"]["name"], "Home SIM")
        self.assertEqual(device["sim"]["carrier"]["name"], "O2")
        self.assertEqual(device["sim"]["carrier"]["plmn"], "234-10")
        self.assertEqual(device["instance_id"], "3")
        self.assertEqual(device["capabilities"]["cellular"]["actual"], "on")

    async def test_saved_unplugged_modem_never_looks_like_it_is_transitioning(self):
        desired = {"devices": {"modem-a": {
            "cellular_enabled": False, "vowifi_enabled": True, "flight_mode": False}}}
        observed = {"devices": {"modem-a": {
            "present": False, "transitioning": True,
            "actual": {"cellular_radio_enabled": False, "vowifi_bridge_active": False}}}}
        assignments = {"modem-a": {"name": "Saved modem"}}
        with patch.object(main, "_device_sources", return_value=(desired, observed, assignments)), \
                patch.object(main, "_device_identities", return_value={}), \
                patch.object(main.hub, "cards_list", return_value=[]), \
                patch.object(main.device_state, "native_reader_devices", return_value={}), \
                patch.object(main.device_state, "hardware", return_value={
                    "modem-a": {"device_type": "modem", "name": "Saved modem"}}), \
                patch.object(main.cfg, "get_settings", return_value={
                    "proxy": {"exits": {}}, "rekey": {"minutes": 30}}), \
                patch.object(main.egress, "status", return_value={"lines": {}}), \
                patch.object(main.egress, "line_country", return_value=""), \
                patch.object(main.egress, "country_for_mcc", return_value=""):
            devices = await main._unified_devices()

        self.assertEqual(len(devices), 1)
        device = devices[0]
        self.assertFalse(device["present"])
        self.assertEqual(device["capabilities"]["cellular"]["actual"], "off")
        self.assertEqual(device["capabilities"]["flight"]["actual"], "off")
        self.assertEqual(device["capabilities"]["vowifi"]["actual"], "off")
        self.assertFalse(device["capabilities"]["vowifi"]["available"])


if __name__ == "__main__":
    unittest.main()


class ConnectivityTimelineEvidenceTests(unittest.TestCase):
    """The timeline must chart what was observed, not what could not be read.

    compute() only reaches REGISTERING once the tunnel is installed, so a registration of
    "unknown" there means the read itself failed — the management timeout this codebase
    already refuses to treat as a carrier failure. Charting it as a disconnect made a line
    that never dropped its tunnel show 16 outages in a day.
    """

    def test_a_registered_line_is_up(self):
        self.assertEqual(main._line_state_kind(
            {"state": "OK", "detail": {"registration": "Registered"}}), "up")

    def test_a_stopped_line_is_off(self):
        self.assertEqual(main._line_state_kind({"state": "STOPPED", "detail": {}}), "off")

    def test_an_unreadable_registration_is_not_evidence_of_a_disconnect(self):
        for detail in ({"registration": "unknown"}, {"registration": ""}, {}):
            self.assertIsNone(main._line_state_kind({"state": "REGISTERING", "detail": detail}))

    def test_a_carrier_answer_is_still_recorded_as_down(self):
        # These are real observations of a line that is not registered.
        for registration in ("Unregistered", "Rejected"):
            self.assertEqual(main._line_state_kind(
                {"state": "REGISTERING", "detail": {"registration": registration}}), "down")

    def test_failures_before_registration_are_recorded_as_down(self):
        for state in ("TUNNEL_DOWN", "EPDG_UNRESOLVED", "NO_CARD", "PIN_PROBLEM", "ERROR"):
            self.assertEqual(main._line_state_kind({"state": state, "detail": {}}), "down")


class HostAlertSuppressionTests(unittest.TestCase):
    """Suppression is measured in hours, so it has to outlive a manager restart: an appliance
    is restarted for upgrades far more often than a brown-out or a full disk changes."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self._patch = patch.object(main.cfg, "DATA_DIR", self._temp.name)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._temp.cleanup()

    def test_state_survives_a_restart(self):
        main._save_host_alert_state({"undervoltage_seen": {"at": 1000.0}})
        self.assertEqual(main._load_host_alert_state(),
                         {"undervoltage_seen": {"at": 1000.0}})

    def test_a_missing_state_file_is_not_an_error(self):
        self.assertEqual(main._load_host_alert_state(), {})

    def test_a_corrupt_state_file_does_not_break_the_poller(self):
        with open(main._host_alert_state_path(), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(main._load_host_alert_state(), {})

    def test_acknowledged_host_alert_stays_hidden_while_condition_persists(self):
        alerts = [{"code": "undervoltage_seen", "severity": "warning"}]
        self.assertEqual(main._visible_host_alerts(alerts, {}), alerts)
        self.assertEqual(main._visible_host_alerts(
            alerts, {"undervoltage_seen": {"acknowledged": True}}), [])

    def test_clear_host_alerts_persists_acknowledgement(self):
        old_alerts, old_state = main.hub.host_alerts, main.hub.host_alert_state
        try:
            main.hub.host_alerts = [{"code": "undervoltage_seen", "severity": "warning"}]
            main.hub.host_alert_state = {}

            result = main.api_host_alerts_clear()

            self.assertEqual(result["cleared"], ["undervoltage_seen"])
            self.assertEqual(main.hub.host_alerts, [])
            saved = main._load_host_alert_state()
            self.assertTrue(saved["undervoltage_seen"]["acknowledged"])
        finally:
            main.hub.host_alerts, main.hub.host_alert_state = old_alerts, old_state

    def test_the_summary_explains_each_condition_rather_than_naming_it(self):
        text = main._host_alert_summary([
            {"code": "undervoltage_now", "severity": "critical", "detail": {"events": 96}}])
        self.assertIn("供电", text)
        self.assertIn("critical", text)
        self.assertIn("96", text)
        self.assertNotIn("undervoltage_now", text)


class PortedNumberTests(unittest.IsolatedAsyncioTestCase):
    """A ported number is the same SIM registering normally and being answered with a
    different public identity. Treating the first IMS answer as permanent left the line
    presenting its previous number as caller identity indefinitely."""

    def setUp(self):
        main.hub._msisdn_checked.pop("5", None)

    async def _verify(self, stored, observed, source="ims"):
        inst = {"id": "5", "name": "voxi", "msisdn": stored, "msisdn_source": source}
        with patch.object(main, "extract_msisdn", return_value=observed), \
                patch.object(main.engine, "exec_cli") as exec_cli, \
                patch.object(main.cfg, "upsert_instance",
                             side_effect=lambda x: {**inst, **x}) as upsert, \
                patch.object(main.cfg, "get_settings", return_value={}), \
                patch.object(main, "_start_engine_checked") as restart, \
                patch.object(main.hub, "drop_ami", new=AsyncMock()), \
                patch.object(main.hub, "broadcast", new=AsyncMock()), \
                patch.object(main.notify_push, "dispatch") as dispatch:
            await main._verify_ims_msisdn("5", inst)
            for _ in range(20):
                await __import__("asyncio").sleep(0.01)
                if dispatch.called:
                    break
        return upsert, restart, dispatch, exec_cli

    async def test_a_new_carrier_number_is_adopted_and_announced(self):
        # The first check must run even when the host has been up for less than the
        # verification interval (CI runners and newly booted gateways commonly have).
        with patch.object(main, "MSISDN_VERIFY_INTERVAL_SECONDS", float("inf")):
            upsert, restart, dispatch, exec_cli = await self._verify(
                "+447700900456", "+447700900123")
        self.assertEqual(upsert.call_args.args[0]["msisdn"], "+447700900123")
        # The dialplan is a snapshot from container start, so the line must be rebuilt or it
        # keeps presenting the old number as caller identity.
        restart.assert_called_once()
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[1], main.notify_push.EV_NUMBER_CHANGED)
        self.assertIn("+447700900456", dispatch.call_args.args[4])
        # Nothing is asked of the carrier. Forcing a REGISTER to produce a readable identity
        # is what issue #8 reported: some IMS cores answer it with 503, Asterisk reports a
        # rejected registration, and the health policy takes a working line down.
        exec_cli.assert_not_called()

    async def test_an_unchanged_number_does_nothing(self):
        upsert, restart, dispatch, _ = await self._verify(
            "+447700900123", "+447700900123")
        upsert.assert_not_called()
        restart.assert_not_called()
        dispatch.assert_not_called()

    async def test_a_manually_entered_number_is_never_overridden(self):
        upsert, restart, _, exec_cli = await self._verify(
            "+440000000000", "+447700900123", source="manual")
        upsert.assert_not_called()
        restart.assert_not_called()
        exec_cli.assert_not_called()

    async def test_an_unreadable_registration_is_not_treated_as_a_change(self):
        upsert, restart, _, _ = await self._verify("+447700900456", None)
        upsert.assert_not_called()
        restart.assert_not_called()

    async def test_a_failed_rebuild_does_not_commit_the_new_number(self):
        inst = {"id": "5", "name": "voxi", "msisdn": "+447700900456",
                "msisdn_source": "ims"}
        with patch.object(main, "extract_msisdn", return_value="+447700900123"), \
                patch.object(main.engine, "exec_cli"), \
                patch.object(main.cfg, "upsert_instance") as upsert, \
                patch.object(main.cfg, "get_settings", return_value={}), \
                patch.object(main, "_start_engine_checked", side_effect=RuntimeError("docker")), \
                patch.object(main.hub, "drop_ami", new=AsyncMock()):
            with self.assertLogs("vowifi.main", level="WARNING") as captured:
                await main._verify_ims_msisdn("5", inst)
        upsert.assert_not_called()
        self.assertIn("will retry", "\n".join(captured.output))

    def test_ported_number_check_uses_a_slow_default_cadence(self):
        self.assertGreaterEqual(main.MSISDN_VERIFY_INTERVAL_SECONDS, 6 * 60 * 60)

    def test_latest_associated_identity_wins(self):
        with patch.object(main.engine, "logs", return_value=(
                "P-Associated-Uri: <tel:+447700000001>\n"
                "P-Associated-Uri: <sip:+447700000002@example.invalid>\n")):
            self.assertEqual(main.extract_msisdn("5"), "+447700000002")

    def test_the_number_is_read_from_an_ordinary_registration(self):
        """The patched engine announces the identity of every REGISTER it makes, so no SIP
        packet logger and no extra REGISTER are needed to see it."""
        with patch.object(main.engine, "logs", return_value=(
                "[Aug 21 09:00:00] NOTICE[123] res_pjsip_outbound_registration.c: "
                "IMS public identity: <sip:+447700000001@ims.example.invalid>\n")):
            self.assertEqual(main.extract_msisdn("5"), "+447700000001")

    def test_an_imsi_derived_identity_does_not_hide_the_dialable_number(self):
        """Carriers list every public identity. The IMSI-derived IMPU comes first and carries
        no number; the line's number is in a later entry of the same header."""
        with patch.object(main.engine, "logs", return_value=(
                "NOTICE[123]: IMS public identity: <sip:234100000000001@ims.example.invalid>,"
                "<tel:+447700000003>\n")):
            self.assertEqual(main.extract_msisdn("5"), "+447700000003")

    def test_an_engine_that_never_announced_one_reports_nothing(self):
        with patch.object(main.engine, "logs", return_value="Registration successful\n"):
            self.assertIsNone(main.extract_msisdn("5"))


class SustainedAlertTests(unittest.TestCase):
    """Starting a container on a memory-tight box pages a batch back in. That burst is real
    but is the cost of the operation, not something an operator can act on — and reporting it
    is how an indicator earns the reputation that makes people ignore a genuine outage."""

    def test_a_one_sample_spike_is_not_reported(self):
        streaks = {}
        spike = [{"code": "swap_pressure", "severity": "warning", "detail": {}}]
        self.assertEqual(main._sustained_alerts(spike, streaks), [])

    def test_a_rate_that_holds_is_reported(self):
        streaks = {}
        spike = [{"code": "swap_pressure", "severity": "warning", "detail": {}}]
        for _ in range(main.SUSTAINED_ALERT_SAMPLES - 1):
            self.assertEqual(main._sustained_alerts(spike, streaks), [])
        kept = main._sustained_alerts(spike, streaks)
        self.assertEqual([x["code"] for x in kept], ["swap_pressure"])
        self.assertEqual(kept[0]["detail"]["samples"], main.SUSTAINED_ALERT_SAMPLES)

    def test_a_gap_restarts_the_count(self):
        streaks = {}
        spike = [{"code": "swap_pressure", "severity": "warning", "detail": {}}]
        main._sustained_alerts(spike, streaks)
        main._sustained_alerts([], streaks)          # subsided
        self.assertEqual(main._sustained_alerts(spike, streaks), [])

    def test_conditions_that_are_instantaneous_are_reported_at_once(self):
        # A brown-out lasts seconds; waiting three minutes would simply miss it.
        streaks = {}
        alerts = [{"code": "undervoltage_now", "severity": "critical", "detail": {}}]
        self.assertEqual(main._sustained_alerts(alerts, streaks), alerts)


class ImeiSourceFollowsReaderTests(unittest.TestCase):
    """A reader that moves to a new USB port derives a new id; lines naming the old one as
    their IMEI source are left pointing at a device that no longer exists."""

    def _run(self, instances):
        saved = []
        with patch.object(main.cfg, "list_instances", return_value=instances), \
                patch.object(main.cfg, "upsert_instance", side_effect=saved.append):
            followed = main._follow_imei_source("reader-old", "reader-new")
        return followed, saved

    def test_the_line_naming_the_retired_id_is_repointed(self):
        followed, saved = self._run([{"id": "5", "imei_source_device_id": "reader-old"}])
        self.assertEqual(followed, ["5"])
        self.assertEqual(saved, [{"id": "5", "imei_source_device_id": "reader-new"}])

    def test_lines_naming_another_device_are_untouched(self):
        followed, saved = self._run([
            {"id": "1", "imei_source_device_id": "2c7c-0125-1-1.4.4"},
            {"id": "2", "imei_source_device_id": ""},
            {"id": "3"},
        ])
        self.assertEqual(followed, [])
        self.assertEqual(saved, [])

    def test_every_line_sharing_the_reader_follows_it(self):
        followed, _ = self._run([{"id": "3", "imei_source_device_id": "reader-old"},
                                 {"id": "4", "imei_source_device_id": "reader-old"}])
        self.assertEqual(followed, ["3", "4"])

    def test_the_marker_is_never_cleared(self):
        # An empty marker would let the one-time legacy migration run a second time and
        # overwrite the reader record from whichever SIM happens to be inserted.
        _, saved = self._run([{"id": "5", "imei_source_device_id": "reader-old"}])
        self.assertTrue(all(item["imei_source_device_id"] for item in saved))


class HardwareImeiRecoveryTests(unittest.TestCase):
    CARD = {"iccid": "8900000000000000001"}

    def _resolve(self, inst):
        with patch.object(main, "_device_for_card", return_value=("modem-a", "modem")), \
                patch.object(main, "_device_identities",
                             return_value={"modem-a": {"imei": ""}}), \
                patch.object(main, "_match_instance_by_iccid", return_value=inst):
            return main._hardware_imei_for_card(self.CARD, [self.CARD])

    def test_rebuild_does_not_reuse_a_modem_imei_from_a_port_based_id(self):
        resolved = self._resolve({
            "imei": "123456789012345", "imei_source_device_id": "modem-a"})
        self.assertEqual(resolved, ("", "modem-a", "modem"))

    def test_rebuild_never_borrows_an_imei_from_another_modem(self):
        resolved = self._resolve({
            "imei": "123456789012345", "imei_source_device_id": "modem-b"})
        self.assertEqual(resolved, ("", "modem-a", "modem"))

    def test_reader_rebuild_also_reuses_its_verified_line_snapshot(self):
        inst = {"imei": "123456789012345", "imei_source_device_id": "reader-a"}
        with patch.object(main, "_device_for_card", return_value=("reader-a", "reader")), \
                patch.object(main.device_state, "hardware", return_value={}), \
                patch.object(main, "_match_instance_by_iccid", return_value=inst):
            resolved = main._hardware_imei_for_card(self.CARD, [self.CARD])
        self.assertEqual(resolved, ("123456789012345", "reader-a", "reader"))


class OutageDetailTests(unittest.TestCase):
    """The outage record must name the evidence, not just the verdict."""

    def test_a_dns_failure_names_the_domain_and_the_resolvers(self):
        st = {"reason_code": "epdg_unresolved",
              "detail": {"epdg_fqdn": "epdg.epc.mnc260.mcc310.pub.3gppnetwork.org",
                         "nameservers": ["223.5.5.5", "119.29.29.29"]}}
        evidence = __import__("json").loads(main._outage_detail(st))
        self.assertEqual(evidence, {
            "code": "client_dns_unresolved",
            "peer": "epdg.epc.mnc260.mcc310.pub.3gppnetwork.org",
            "servers": ["223.5.5.5", "119.29.29.29"],
        })

    def test_a_tunnel_failure_names_the_epdg(self):
        st = {"reason_code": "tunnel_network",
              "detail": {"epdg_fqdn": "epdg.epc.mnc010.mcc234.pub.3gppnetwork.org"}}
        evidence = __import__("json").loads(main._outage_detail(st))
        self.assertEqual(evidence["code"], "server_epdg_ike_unanswered")
        self.assertEqual(evidence["peer"], "epdg.epc.mnc010.mcc234.pub.3gppnetwork.org")

    def test_a_registration_failure_names_the_pcscf(self):
        st = {"reason_code": "reg_rejected",
              "detail": {"pcscf": "fd00:976a:2:153::5", "registration": "Rejected",
                         "sip_status": 403}}
        evidence = __import__("json").loads(main._outage_detail(st))
        self.assertEqual(evidence, {"code": "server_pcscf_sip_rejected",
                                    "peer": "fd00:976a:2:153::5", "status": 403})

    def test_child_rekey_timeout_names_server_request_and_peer(self):
        st = {"reason_code": "tunnel_child_rekey_timeout",
              "detail": {"epdg_fqdn": "epdg.example"}}
        evidence = __import__("json").loads(main._outage_detail(st))
        self.assertEqual(evidence, {"code": "server_epdg_child_rekey_unanswered",
                                    "peer": "epdg.example"})

    def test_tunnel_setup_does_not_call_recovery_the_outage_cause(self):
        st = {"reason_code": "tunnel_setup",
              "detail": {"epdg_fqdn": "epdg.example"}}
        evidence = __import__("json").loads(main._outage_detail(st))
        self.assertEqual(evidence, {"code": "tunnel_cause_not_captured",
                                    "peer": "epdg.example"})

    def test_codes_without_useful_evidence_stay_quiet(self):
        self.assertEqual(main._outage_detail({"reason_code": "no_card", "detail": {}}), "")


class ExitFailoverWiringTests(unittest.IsolatedAsyncioTestCase):
    """The policy is only worth its tests if it is actually consulted on the freeze path,
    and if giving up really stops the rebuild instead of merely saying so."""

    EXITS = {"exits": {"us": {"node": "node-a", "candidates": ["node-a", "node-b"],
                              "selection": "auto", "mode": "subscription"}}}
    INST = {"id": "9", "enabled": True, "mcc": "310", "mnc": "240", "name": "test"}

    def setUp(self):
        main.hub.exit_ledgers.pop("9", None)
        main.hub.reset_health("9")
        settings = patch.object(main.cfg, "get_settings", return_value={"proxy": {"enabled": True}})
        settings.start()
        self.addCleanup(settings.stop)
        stalled = patch.object(main.egress, "report_stalled_exit")
        self.stalled = stalled.start()
        self.addCleanup(stalled.stop)

    def tearDown(self):
        main.hub.exit_ledgers.pop("9", None)
        main.hub.reset_health("9")

    def _judge(self, swu, retransmits, exits=None, stable_for=0.0, peers=(),
               reason="tunnel_network", evidence_error=None):
        st = {"reason_code": reason, "reason": "x"}
        with patch.object(main.egress, "line_country", return_value="us"), \
                patch.object(main.egress, "status", return_value=self.EXITS if exits is None else exits), \
                patch.object(main.cfg, "list_instances", return_value=list(peers)), \
                patch.object(main.engine, "read_run_json", return_value={"state": swu}), \
                patch.object(main.engine, "ike_evidence",
                             return_value={"retransmits": retransmits}, side_effect=evidence_error), \
                patch.object(main, "_save_exit_ledgers"), \
                patch.object(main.egress, "request_reselect") as reselect, \
                patch.object(main.asyncio, "to_thread", new=AsyncMock()) as to_thread:
            action = main._judge_exit_failure("9", self.INST, st, stable_for)
        # dispatch is handed to to_thread(), which is called synchronously to build the
        # awaitable — so its arguments are visible without waiting for the task to run.
        return action, reselect, to_thread

    async def test_missing_tunnel_evidence_never_switches_or_cleans_sessions(self):
        for attempt in range(main.failover.FAILURES_BEFORE_REPORT):
            action, reselect, _ = self._judge(None, 0)
            self.assertIn(action, (main.failover.HOLD, main.failover.REPORT))
            reselect.assert_not_called()
        self.stalled.assert_not_called()
        self.assertEqual(main.hub.exit_ledgers["9"]["strikes"], 0)

    async def test_retransmit_read_failure_preserves_connected_evidence(self):
        for attempt in range(3):
            action, reselect, _ = self._judge("CONNECTED", 0, evidence_error=OSError("unreadable"))
            self.assertEqual(action, main.failover.HOLD)
            reselect.assert_not_called()
        self.stalled.assert_not_called()
        self.assertEqual(main.hub.exit_ledgers["9"]["strikes"], 0)

    async def test_dns_failure_never_strikes_or_cleans_an_exit(self):
        for attempt in range(3):
            action, reselect, _ = self._judge("DOWN", 10, reason="epdg_unresolved")
            self.assertEqual(action, main.failover.HOLD)
            reselect.assert_not_called()
        self.stalled.assert_not_called()

    async def test_direct_missing_and_nonselectable_exits_do_not_accumulate_ledgers(self):
        for exits in ({}, {"exits": {}}, {"exits": {"us": {"mode": "direct"}}},
                      {"exits": {"us": {"mode": "manual", "node": "fixed"}}}):
            main.hub.exit_ledgers["9"] = {"exhausted": True, "given_up": True}
            for attempt in range(4):
                action, reselect, notify = self._judge("DOWN", 0, exits=exits)
                self.assertEqual(action, main.failover.HOLD)
                reselect.assert_not_called()
                notify.assert_not_called()
                self.assertNotIn("9", main.hub.exit_ledgers)
        self.stalled.assert_not_called()

    async def test_disabled_proxy_ignores_stale_subscription_status(self):
        with patch.object(main.cfg, "get_settings", return_value={"proxy": {"enabled": False}}):
            action, reselect, _ = self._judge("DOWN", 0)
        self.assertEqual(action, main.failover.HOLD)
        self.assertNotIn("9", main.hub.exit_ledgers)
        reselect.assert_not_called()
        self.stalled.assert_not_called()

    async def test_direct_freezes_keep_normal_retry_cadence(self):
        st = {"state": "TUNNEL_DOWN", "reason_code": "tunnel_network", "reason": "x"}
        with patch.object(main.egress, "status", return_value={"exits": {"us": {"mode": "direct"}}}), \
                patch.object(main, "_local_card_fault", return_value=""), \
                patch.object(main, "_save_exit_ledgers"), \
                patch.object(main.asyncio, "to_thread", new=AsyncMock()), \
                patch.object(main.hub, "drop_ami", new=AsyncMock()):
            for attempt in range(4):
                main.hub.reset_health("9")
                health = main.hub.health_for("9")
                health["fail_start"] = main.time.monotonic() - 10000
                before = main.time.monotonic()
                main.apply_health("9", self.INST, dict(st))
                self.assertGreaterEqual(health["next_retry_at"], before + 160)
                self.assertLess(health["next_retry_at"], before + 170)
                self.assertNotIn("9", main.hub.exit_ledgers)

    def test_empty_or_missing_ike_logs_are_explicitly_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertFalse(main.engine._charon_evidence(temp)["available"])
            run = Path(temp) / "run"
            run.mkdir()
            log = run / "charon.log"
            log.write_text("")
            self.assertFalse(main.engine._charon_evidence(temp)["available"])
            log.write_text("tunnel CONNECTED\n")
            evidence = main.engine._charon_evidence(temp)
            self.assertTrue(evidence["available"])
            self.assertEqual(evidence["retransmits"], 0)

    async def test_a_healthy_tunnel_neither_moves_the_exit_nor_notifies(self):
        action, reselect, to_thread = self._judge("CONNECTED", 0)
        self.assertEqual(action, main.failover.HOLD)
        reselect.assert_not_called()
        to_thread.assert_not_called()

    async def test_the_exit_moves_once_the_node_has_had_its_chances(self):
        for _ in range(main.failover.STRIKES_PER_NODE - 1):
            action, reselect, _ = self._judge("CONNECTING", 14)
            self.assertEqual(action, main.failover.HOLD)
            reselect.assert_not_called()
        action, reselect, _ = self._judge("CONNECTING", 14)
        self.assertEqual(action, main.failover.SWITCH)
        reselect.assert_called_once()

    async def test_an_exhausted_pool_notifies_once_and_backs_off(self):
        seen = []
        for node in ("node-a", "node-b"):
            exits = {"exits": {"us": {"node": node, "candidates": ["node-a", "node-b"],
                                      "selection": "auto", "mode": "subscription"}}}
            for _ in range(main.failover.STRIKES_PER_NODE):
                action, _reselect, to_thread = self._judge("DOWN", 0, exits)
                seen.append((action, to_thread))
        action, to_thread = seen[-1]
        self.assertEqual(action, main.failover.BACK_OFF)
        self.assertEqual(to_thread.call_args[0][0], main.notify_push.dispatch)
        self.assertEqual(to_thread.call_args[0][2], main.notify_push.EV_LINE_UNRECOVERABLE)
        self.assertTrue(main.hub.exit_ledgers["9"]["exhausted"])
        # The slow retries that follow keep backing off without announcing again.
        action, _reselect, to_thread = self._judge("DOWN", 0)
        self.assertEqual(action, main.failover.BACK_OFF)
        to_thread.assert_not_called()

    async def test_a_registered_sibling_keeps_the_exit_where_it_is(self):
        peer = {"id": "7", "enabled": True, "mcc": "310", "mnc": "260", "name": "peer"}
        main.hub.status_cache["7"] = {"state": "OK"}
        actions = []
        try:
            for _ in range(main.failover.FAILURES_BEFORE_REPORT):
                action, reselect, _ = self._judge("DOWN", 0, peers=[peer])
                actions.append(action)
                reselect.assert_not_called()
        finally:
            main.hub.status_cache.pop("7", None)
        # Never a switch — but the operator is still told, once, that the line is stuck.
        self.assertNotIn(main.failover.SWITCH, actions)
        self.assertEqual(actions[-1], main.failover.REPORT)

    async def test_giving_up_stops_the_automatic_rebuild(self):
        h = main.hub.health_for("9")
        h["fail_start"] = main.time.monotonic() - 10_000    # past the retry budget
        st = {"state": "TUNNEL_DOWN", "label": "x", "reason_code": "tunnel_network",
              "reason": "x", "detail": {}}
        with patch.object(main, "_judge_exit_failure",
                          return_value=main.failover.GIVE_UP), \
                patch.object(main.engine, "capture_and_stop"), \
                patch.object(main.cfg, "get_settings", return_value={}):
            main.apply_health("9", self.INST, st)
        # None reads as "never" in the recovery check, the same mechanism a blocked PIN uses.
        self.assertIsNone(h["next_retry_at"])
        self.assertEqual(h["frozen_code"], "tunnel_network")

    async def test_a_failed_manual_retry_stays_stopped_while_pin_is_still_locked(self):
        h = main.hub.health_for("9")
        h["fail_start"] = main.time.monotonic() - 10_000
        main.hub.exit_ledgers["9"] = {
            "node": "node-a", "strikes": 3, "tried": ["node-a"],
            "failures": 3, "given_up": True, "reported": True,
        }
        st = {"state": "TUNNEL_DOWN", "label": "x", "reason_code": "tunnel_network",
              "reason": "x", "detail": {}}
        with patch.object(main, "_judge_exit_failure", return_value=main.failover.HOLD), \
                patch.object(main.engine, "capture_and_stop"), \
                patch.object(main.cfg, "get_settings", return_value={}):
            main.apply_health("9", self.INST, st)
        self.assertIsNone(h["next_retry_at"])
        main.hub.exit_ledgers.pop("9", None)

    async def test_backing_off_slows_the_rebuild_instead_of_stopping_it(self):
        h = main.hub.health_for("9")
        h["fail_start"] = main.time.monotonic() - 10_000
        st = {"state": "TUNNEL_DOWN", "label": "x", "reason_code": "tunnel_network",
              "reason": "x", "detail": {}}
        with patch.object(main, "_judge_exit_failure",
                          return_value=main.failover.BACK_OFF), \
                patch.object(main.engine, "capture_and_stop"), \
                patch.object(main.cfg, "get_settings", return_value={}):
            main.apply_health("9", self.INST, st)
        # An hour, not the ordinary cooldown: the line still retries by itself, but at a
        # pace that stops the churn while whatever broke every exit at once passes.
        remaining = h["next_retry_at"] - main.time.monotonic()
        self.assertGreater(remaining, main.failover.EXHAUSTED_RETRY_SECONDS * 0.9)

    async def test_a_freeze_that_still_has_options_keeps_its_retry_time(self):
        h = main.hub.health_for("9")
        h["fail_start"] = main.time.monotonic() - 10_000
        st = {"state": "TUNNEL_DOWN", "label": "x", "reason_code": "tunnel_network",
              "reason": "x", "detail": {}}
        with patch.object(main, "_judge_exit_failure", return_value=main.failover.HOLD), \
                patch.object(main.engine, "capture_and_stop"), \
                patch.object(main.cfg, "get_settings", return_value={}):
            main.apply_health("9", self.INST, st)
        self.assertIsNotNone(h["next_retry_at"])

    async def test_registering_clears_what_the_ledger_held_against_the_exit(self):
        main.hub.exit_ledgers["9"] = {"node": "node-a", "strikes": 2, "tried": ["node-a"],
                                      "failures": 2, "given_up": True, "reported": True}
        st = {"state": "OK", "label": "Working", "reason_code": "ok", "reason": "",
              "detail": {}}
        with patch.object(main, "_save_exit_ledgers"), \
                patch.object(main.cfg, "get_settings", return_value={}):
            main.apply_health("9", self.INST, st)
        self.assertNotIn("9", main.hub.exit_ledgers)


class StaleStatusCacheTests(unittest.TestCase):
    """Switching VoWiFi off stops the line but the last sample taken while it ran stayed
    authoritative until the next poll — so a device kept reporting the problem of a line
    the operator had just switched off, and a module reported "no SIM card" for a SIM
    sitting in its reader."""

    STALE = {"state": "NO_CARD", "label": "No SIM card", "reason_code": "no_card",
             "reason": "No SIM card detected in the reader.", "detail": {}}

    def setUp(self):
        main.hub.status_cache["line-stale"] = dict(self.STALE)
        self.addCleanup(main.hub.status_cache.pop, "line-stale", None)

    def test_a_disabled_line_is_not_reported_from_the_cache_it_left_behind(self):
        status = main._cached_line_status({"id": "line-stale", "enabled": False})
        self.assertEqual(status["state"], "STOPPED")

    def test_a_running_line_still_reports_its_last_observation(self):
        status = main._cached_line_status({"id": "line-stale", "enabled": True})
        self.assertEqual(status["state"], "NO_CARD")
