"""The offline notification: announced once past the threshold, closed by an all-clear.

A line that the gateway keeps rebuilding used to be silent for as long as the rebuilding took,
which was hours for the outages nobody could blame on an exit.
"""
import asyncio
import unittest
from unittest.mock import patch

from control.app import line_offline, main, notify_push

DOWN, UP, IGNORE = line_offline.DOWN, line_offline.UP, line_offline.IGNORE
T = 600.0


def run(state, observations, wall, mono):
    return line_offline.evaluate(state, observations, T, wall, mono)


class EvaluateTests(unittest.TestCase):
    def test_a_short_outage_is_left_to_automatic_recovery(self):
        state = {}
        run(state, {"1": (DOWN, "x")}, 1000, 0)
        offline, recovered, _ = run(state, {"1": (DOWN, "x")}, 1500, 500)
        self.assertEqual(offline, [])
        offline, recovered, changed = run(state, {"1": (UP, "")}, 1550, 550)
        self.assertEqual((offline, recovered, changed), ([], [], False))
        self.assertEqual(state, {})

    def test_announced_once_then_closed_by_an_all_clear(self):
        state = {}
        run(state, {"1": (DOWN, "no card")}, 1000, 0)
        offline, _, changed = run(state, {"1": (DOWN, "no card")}, 1600, T)
        self.assertEqual([e["instance"] for e in offline], ["1"])
        self.assertEqual(offline[0]["reason"], "no card")
        self.assertTrue(changed)
        offline, _, changed = run(state, {"1": (DOWN, "no card")}, 2000, 1000)
        self.assertEqual((offline, changed), ([], False))
        _, recovered, changed = run(state, {"1": (UP, "")}, 4600, 3600)
        self.assertEqual(recovered[0]["duration"], 3600)
        self.assertTrue(changed)
        self.assertEqual(state, {})

    def test_samples_without_evidence_neither_start_nor_end_an_outage(self):
        state = {}
        run(state, {"1": (None, "")}, 1000, 0)
        self.assertEqual(state, {})
        run(state, {"1": (DOWN, "")}, 1000, 0)
        run(state, {"1": (None, "")}, 1300, 300)
        offline, _, _ = run(state, {"1": (DOWN, "")}, 1600, T)
        self.assertEqual(len(offline), 1)

    def test_switching_a_line_off_withdraws_the_alert_without_an_all_clear(self):
        state = {}
        run(state, {"1": (DOWN, "")}, 1000, 0)
        run(state, {"1": (DOWN, "")}, 1600, T)
        _, recovered, changed = run(state, {"1": (IGNORE, "")}, 1700, 700)
        self.assertEqual(recovered, [])
        self.assertTrue(changed)
        self.assertEqual(state, {})

    def test_a_deleted_line_is_forgotten(self):
        state = {}
        run(state, {"1": (DOWN, "")}, 1000, 0)
        run(state, {"1": (DOWN, "")}, 1600, T)
        _, recovered, changed = run(state, {}, 1700, 700)
        self.assertEqual((recovered, changed, state), ([], True, {}))

    def test_a_wall_clock_jump_is_not_counted_as_outage(self):
        """A Pi boots without an RTC; NTP then moves the wall clock forward by days."""
        state = {}
        run(state, {"1": (DOWN, "")}, 1000, 0)
        offline, _, _ = run(state, {"1": (DOWN, "")}, 1000 + 5 * 86400, 30)
        self.assertEqual(offline, [])

    def test_an_announced_outage_survives_a_restart(self):
        state = {}
        run(state, {"1": (DOWN, "r")}, 1000, 0)
        run(state, {"1": (DOWN, "r")}, 1600, T)
        restored = line_offline.restore(line_offline.persistable(state))
        # A fresh process has a fresh monotonic clock; the alert must not repeat.
        offline, _, _ = run(restored, {"1": (DOWN, "r")}, 1700, 5)
        self.assertEqual(offline, [])
        _, recovered, _ = run(restored, {"1": (UP, "")}, 4600, 10)
        self.assertEqual(recovered[0]["duration"], 3600)

    def test_unannounced_outages_are_not_persisted(self):
        state = {}
        run(state, {"1": (DOWN, "")}, 1000, 0)
        self.assertEqual(line_offline.persistable(state), {})

    def test_threshold_is_clamped(self):
        self.assertEqual(line_offline.threshold_seconds({}), 600)
        self.assertEqual(line_offline.threshold_seconds({"line_offline_notify_minutes": 0}), 60)
        self.assertEqual(line_offline.threshold_seconds({"line_offline_notify_minutes": "x"}), 600)
        self.assertEqual(
            line_offline.threshold_seconds({"line_offline_notify_minutes": 99999}), 1440 * 60)

    def test_duration_reads_naturally(self):
        self.assertEqual(line_offline.format_duration(30), "1 分钟")
        self.assertEqual(line_offline.format_duration(720), "12 分钟")
        self.assertEqual(line_offline.format_duration(3600), "1 小时")
        self.assertEqual(line_offline.format_duration(5400), "1 小时 30 分钟")
        self.assertEqual(line_offline.format_duration(90000), "1 天 1 小时")


class ObservationTests(unittest.TestCase):
    def observe(self, inst, status, allowed=(True, "")):
        with patch.dict(main.hub.status_cache, {"1": status} if status else {}, clear=True), \
                patch.object(main, "_line_auto_start_allowed", return_value=allowed):
            return main._line_offline_observation({"id": "1", **inst})

    def test_an_enabled_line_whose_engine_is_not_running_is_offline(self):
        kind, reason = self.observe({}, {"state": "STOPPED", "reason_code": "stopped"})
        self.assertEqual(kind, DOWN)
        self.assertIn("stopped", reason)

    def test_a_missing_card_is_offline(self):
        kind, reason = self.observe({}, {"state": "NO_CARD", "reason_code": "no_card"},
                                    allowed=(False, "no_card"))
        self.assertEqual(kind, DOWN)
        self.assertIn("读卡器", reason)

    def test_switched_off_lines_and_drafts_are_not_watched(self):
        self.assertEqual(self.observe({"enabled": False}, {"state": "STOPPED"})[0], IGNORE)
        self.assertEqual(self.observe({"provisioning_state": "draft"}, {"state": "NO_CARD"})[0],
                         IGNORE)
        self.assertEqual(self.observe({}, {"state": "STOPPED"},
                                      allowed=(False, "vowifi_disabled"))[0], IGNORE)

    def test_a_failed_registration_read_says_nothing(self):
        status = {"state": "REGISTERING", "detail": {"registration": "unknown"}}
        self.assertEqual(self.observe({}, status)[0], None)
        self.assertEqual(self.observe({}, None)[0], None)
        self.assertEqual(self.observe({}, {"state": "OK"})[0], UP)


class AnnouncementTests(unittest.TestCase):
    def check(self, instances, observations, state):
        async def go():
            await main._check_line_offline(instances)
            await asyncio.sleep(0.05)   # let the fire-and-forget dispatch run

        with patch.object(main.hub, "line_offline_state", state), \
                patch.object(main, "_line_offline_observation",
                             side_effect=lambda inst: observations[str(inst["id"])]), \
                patch.object(main, "_save_line_offline_state"), \
                patch.object(main.cfg, "get_settings", return_value={}), \
                patch.object(main.notify_push, "dispatch") as dispatch:
            asyncio.run(go())
        return [call.args for call in dispatch.call_args_list]

    def test_lines_that_drop_together_are_announced_in_one_message(self):
        state = {iid: {"since": 1000.0, "mono": -10_000.0, "notified": False, "reason": "r"}
                 for iid in ("1", "2")}
        instances = [{"id": "1", "name": "UK"}, {"id": "2", "name": "US"}]
        sent = self.check(instances, {"1": (DOWN, "r"), "2": (DOWN, "r")}, state)
        self.assertEqual(len(sent), 1)
        settings, event, target, source, text, match = sent[0]
        self.assertEqual(event, notify_push.EV_LINE_OFFLINE)
        self.assertEqual(target["name"], "2 条线路")
        self.assertEqual(match, ["1", "2"])
        self.assertIn("UK", text)
        self.assertIn("US", text)

    def test_a_single_line_is_announced_as_itself(self):
        state = {"1": {"since": 1000.0, "mono": -10_000.0, "notified": False, "reason": "r"}}
        sent = self.check([{"id": "1", "name": "UK", "msisdn": "+44"}], {"1": (DOWN, "r")}, state)
        settings, event, target, source, text, match = sent[0]
        self.assertEqual((target["name"], source, match), ("UK", "+44", None))
        self.assertIn("恢复后会再通知", text)


class FeishuRoutingTests(unittest.TestCase):
    def test_a_combined_message_reaches_bots_routed_to_any_of_its_lines(self):
        settings = {"feishu": {"channels": [
            {"id": "a", "enabled": True, "url": "https://open.feishu.cn/x", "instances": ["2"]},
            {"id": "b", "enabled": True, "url": "https://open.feishu.cn/y", "instances": ["9"]},
        ]}}
        with patch.object(notify_push, "_DELIVERY_EXECUTOR") as executor:
            notify_push.dispatch(settings, notify_push.EV_LINE_OFFLINE,
                                 {"id": "", "name": "2 条线路"}, "", "t", ["1", "2"])
        channels = [call.args[1] for call in executor.submit.call_args_list]
        self.assertEqual(channels, ["feishu:a"])


if __name__ == "__main__":
    unittest.main()
