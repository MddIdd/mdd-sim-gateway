import unittest
import tempfile
import os
import base64
import hashlib
import hmac
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import yaml

from control.app import config, notify_push

from control.app.notify_push import (
    EV_INCOMING_CALL,
    EV_INCOMING_SMS,
    build_payload,
    build_notification_message,
    build_webhook_request,
    send_pushplus,
    send_feishu,
    feishu_signature,
    telegram_session,
    _deliver_with_retry,
    delivery_status,
)


class NotificationChannelTests(unittest.TestCase):
    def test_message_templates_are_scoped_to_one_event(self):
        sms = build_payload(EV_INCOMING_SMS, {"id": 1, "name": "UK SIM"}, "+44700", "hello")
        cfg = {"message_templates": {EV_INCOMING_SMS: {
            "title": "{{sim_name}} 收到 {{event}}",
            "content": "{{from}}: {{text}}",
        }}}
        self.assertEqual(build_notification_message(sms, cfg), {
            "title": "UK SIM 收到 incoming_sms", "content": "+44700: hello"})
        call = build_payload(EV_INCOMING_CALL, {"id": 1, "name": "UK SIM"}, "+44700", None)
        self.assertIn("来电", build_notification_message(call, cfg)["title"])

    def test_message_templates_can_wrap_the_default_title_and_content(self):
        payload = build_payload(EV_INCOMING_SMS, {"id": 1, "name": "UK SIM"}, "+44700", "hello")
        message = build_notification_message(payload, {"message_templates": {EV_INCOMING_SMS: {
            "title": "[Home] {{title}}", "content": "---\n{{content}}",
        }}})
        self.assertTrue(message["title"].startswith("[Home] MDD"))
        self.assertIn("hello", message["content"])

    def test_unknown_message_template_fields_are_rejected(self):
        payload = build_payload(EV_INCOMING_SMS, {"id": 1}, "+100", "hello")
        with self.assertRaisesRegex(ValueError, "unknown notification template field"):
            build_notification_message(payload, {"message_templates": {EV_INCOMING_SMS: {
                "content": "{{arbitrary_code}}",
            }}})

    def test_message_template_configuration_is_validated_before_save(self):
        notify_push.validate_message_templates({"message_templates": {EV_INCOMING_SMS: {
            "title": "{{title}}", "content": "{{text}}",
        }}})
        with self.assertRaisesRegex(ValueError, "channel configuration must be an object"):
            notify_push.validate_message_templates([])
        with self.assertRaisesRegex(ValueError, "unknown notification template event"):
            notify_push.validate_message_templates({"message_templates": {"shell": {
                "content": "no",
            }}})
        with self.assertRaisesRegex(ValueError, "unknown notification template property"):
            notify_push.validate_message_templates({"message_templates": {EV_INCOMING_SMS: {
                "execute": "no",
            }}})

    def test_standard_webhook_includes_rendered_human_message(self):
        payload = build_payload(EV_INCOMING_SMS, {"id": 1, "name": "UK SIM"}, "+100", "hello")
        _method, _url, kwargs = build_webhook_request({
            "url": "https://example.test/hook",
            "message_templates": {EV_INCOMING_SMS: {"title": "SMS · {{sim_name}}"}},
        }, payload)
        self.assertEqual(kwargs["json"]["title"], "SMS · UK SIM")
        self.assertIn("hello", kwargs["json"]["content"])

    def test_custom_webhook_payload_uses_rendered_message_fields(self):
        payload = build_payload(EV_INCOMING_SMS, {"id": 1, "name": "UK SIM"}, "+100", "hello")
        _method, _url, kwargs = build_webhook_request({
            "format": "custom", "url": "https://example.test/hook",
            "payload_template": '{"subject":"{{title}}","body":"{{content}}"}',
            "message_templates": {EV_INCOMING_SMS: {
                "title": "Custom {{sim_name}}", "content": "Body {{text}}",
            }},
        }, payload)
        self.assertEqual(kwargs["json"], {"subject": "Custom UK SIM", "body": "Body hello"})

    def test_telegram_uses_the_selected_event_template(self):
        payload = build_payload(EV_INCOMING_SMS, {"id": 1, "name": "UK SIM"}, "+100", "hello")
        rendered = notify_push._telegram_text(payload, {"message_templates": {EV_INCOMING_SMS: {
            "title": "SMS from {{from}}", "content": "{{text}}",
        }}})
        self.assertEqual(rendered, "SMS from +100\n\nhello")

    def test_telegram_keeps_its_own_default_content_for_a_blank_override(self):
        payload = build_payload(EV_INCOMING_SMS, {"id": 1, "name": "UK SIM"}, "+100", "hello")
        rendered = notify_push._telegram_text(payload, {"message_templates": {EV_INCOMING_SMS: {
            "title": "Custom title", "content": "",
        }}})
        self.assertTrue(rendered.startswith("Custom title\n\nSIM: UK SIM"))
        self.assertIn("From: +100", rendered)
        self.assertTrue(rendered.endswith("hello"))

    def test_legacy_private_preset_migrates_to_standard_custom_webhook(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "DATA_DIR", temp), patch.object(
                config, "CONFIG_PATH", os.path.join(temp, "config.yaml")):
            with open(config.CONFIG_PATH, "w", encoding="utf-8") as handle:
                yaml.safe_dump({"settings": {"webhook": {
                    "enabled": True, "format": "universal_push", "url": "https://example.test",
                    "source": "gateway", "token": "secret"}}}, handle)
            webhook = config.get_settings()["webhook"]
        self.assertEqual(webhook["format"], "custom")
        self.assertNotIn("source", webhook)
        self.assertNotIn("token", webhook)
        self.assertIn("X-App-Token", webhook["headers_json"])
        self.assertIn('"source": "gateway"', webhook["payload_template"])

    def test_sms_message_contains_title_content_and_sender(self):
        canonical = build_payload(
            EV_INCOMING_SMS,
            {"id": 1, "name": "UK SIM", "msisdn": "+44123"},
            "+44700",
            "hello",
        )
        actual = build_notification_message(canonical)
        self.assertIn("短信", actual["title"])
        self.assertIn("hello", actual["content"])
        self.assertIn("+44700", actual["content"])

    def test_call_payload_does_not_include_sms_text(self):
        canonical = build_payload(EV_INCOMING_CALL, {"id": 2}, "+86150", None)
        actual = build_notification_message(canonical)
        self.assertIn("来电", actual["title"])
        self.assertNotIn("None", actual["content"])

    def test_private_adapter_is_an_ordinary_custom_webhook(self):
        canonical = build_payload(EV_INCOMING_SMS, {"id": 1}, "+100", "hello")
        method, url, kwargs = build_webhook_request({
            "format": "custom", "method": "POST", "body_mode": "json",
            "url": "https://example.test/hook",
            "headers_json": '{"X-App-Token":"private"}',
            "payload_template": '{"source":"gateway","title":"{{title}}","content":"{{content}}"}',
        }, canonical)
        self.assertEqual((method, url), ("POST", "https://example.test/hook"))
        self.assertEqual(kwargs["headers"]["X-App-Token"], "private")
        self.assertEqual(kwargs["json"]["source"], "gateway")
        self.assertIn("hello", kwargs["json"]["content"])

    @patch("control.app.notify_push.requests.post")
    def test_pushplus_uses_official_json_contract(self, post):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"code": 200, "msg": "success"}
        post.return_value = response
        result = send_pushplus({"token": "secret", "topic": "family", "template": "html",
                                "channel": "wechat", "message_templates": {
                                    EV_INCOMING_CALL: {"title": "Call · {{from}}",
                                                       "content": "Line {{instance}}"}}},
                               build_payload(EV_INCOMING_CALL, {"id": 1}, "+100", None))
        self.assertTrue(result["ok"])
        request = post.call_args.kwargs["json"]
        self.assertEqual(request["token"], "secret")
        self.assertEqual(request["topic"], "family")
        self.assertEqual(request["title"], "Call · +100")
        self.assertEqual(request["content"], "Line 1")

    def test_feishu_signature_uses_the_documented_empty_message_hmac(self):
        timestamp = 1700000000
        secret = "test-secret"
        expected = base64.b64encode(hmac.new(
            f"{timestamp}\n{secret}".encode("utf-8"), b"", hashlib.sha256).digest()
        ).decode("ascii")
        self.assertEqual(feishu_signature(timestamp, secret), expected)

    @patch("control.app.notify_push.requests.post")
    def test_feishu_uses_text_contract_and_optional_signature(self, post):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"code": 0, "msg": "success"}
        post.return_value = response
        with patch("control.app.notify_push.time.time", return_value=1700000000):
            result = send_feishu({
                "url": "https://open.feishu.cn/open-apis/bot/v2/hook/test-token",
                "secret": "test-secret",
                "message_templates": {EV_INCOMING_SMS: {
                    "title": "SMS · {{sim_name}}", "content": "{{from}}: {{text}}",
                }},
            }, build_payload(EV_INCOMING_SMS, {"id": 1, "name": "UK SIM"}, "+100", "hello"))
        self.assertTrue(result["ok"])
        request = post.call_args.kwargs["json"]
        self.assertEqual(request["msg_type"], "text")
        self.assertEqual(request["content"]["text"], "SMS · UK SIM\n\n+100: hello")
        self.assertEqual(request["timestamp"], "1700000000")
        self.assertEqual(request["sign"], feishu_signature(1700000000, "test-secret"))

    @patch("control.app.notify_push.requests.post")
    def test_feishu_omits_signature_without_a_secret(self, post):
        response = MagicMock(status_code=200)
        response.json.return_value = {"StatusCode": 0, "StatusMessage": "success"}
        post.return_value = response
        send_feishu({
            "url": "https://open.larksuite.com/open-apis/bot/v2/hook/test-token",
        }, build_payload(EV_INCOMING_CALL, {"id": 1}, "+100", None))
        request = post.call_args.kwargs["json"]
        self.assertNotIn("timestamp", request)
        self.assertNotIn("sign", request)

    @patch("control.app.notify_push.requests.post")
    def test_feishu_rejects_http_200_application_errors(self, post):
        response = MagicMock(status_code=200)
        response.json.return_value = {"code": 19024, "msg": "Key Words Not Found"}
        post.return_value = response
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            send_feishu({
                "url": "https://open.feishu.cn/open-apis/bot/v2/hook/test-token",
            }, build_payload(EV_INCOMING_SMS, {"id": 1}, "+100", "hello"))

    def test_feishu_requires_an_official_custom_bot_url(self):
        payload = build_payload(EV_INCOMING_SMS, {"id": 1}, "+100", "hello")
        with self.assertRaisesRegex(ValueError, "URL is required"):
            send_feishu({}, payload)
        with self.assertRaisesRegex(ValueError, "URL is invalid"):
            send_feishu({"url": "https://example.test/hook"}, payload)
        with self.assertRaisesRegex(ValueError, "URL is invalid"):
            send_feishu({
                "url": "https://open.feishu.cn/open-apis/bot/v2/hook/",
            }, payload)

    def test_feishu_counts_as_an_enabled_channel(self):
        settings = {"feishu": {"enabled": True, "events": {
            EV_INCOMING_SMS: True, EV_INCOMING_CALL: False,
        }}}
        self.assertTrue(notify_push.has_enabled_channel(settings, EV_INCOMING_SMS))
        self.assertFalse(notify_push.has_enabled_channel(settings, EV_INCOMING_CALL))

    def test_legacy_feishu_config_is_exposed_as_one_channel(self):
        channels = notify_push.feishu_channels({
            "enabled": True,
            "url": "https://open.feishu.cn/open-apis/bot/v2/hook/test-token",
        })
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]["id"], "legacy")

    def test_explicit_empty_feishu_channels_disable_legacy_fallback(self):
        self.assertEqual(notify_push.feishu_channels({
            "enabled": True,
            "url": "https://open.feishu.cn/open-apis/bot/v2/hook/test-token",
            "channels": [],
        }), [])

    def test_feishu_instance_filter_matches_only_selected_lines(self):
        self.assertTrue(notify_push.feishu_channel_matches({"instances": []}, "9"))
        self.assertTrue(notify_push.feishu_channel_matches({"instances": ["2", "9"]}, "9"))
        self.assertFalse(notify_push.feishu_channel_matches({"instances": ["2"]}, "9"))
        self.assertFalse(notify_push.feishu_channel_matches({"instances": ["2"]}, ""))

    def test_multi_feishu_validation_rejects_duplicate_ids_and_bad_filters(self):
        with self.assertRaisesRegex(ValueError, "IDs must be unique"):
            notify_push.validate_feishu_channels({"channels": [
                {"id": "ops", "enabled": False}, {"id": "ops", "enabled": False},
            ]})
        with self.assertRaisesRegex(ValueError, "instance filters"):
            notify_push.validate_feishu_channels({"channels": [
                {"id": "ops", "enabled": False, "instances": ["bad/id"]},
            ]})

    def test_disabled_feishu_channel_can_be_saved_before_url_is_filled(self):
        channels = notify_push.validate_feishu_channels({"channels": [{
            "id": "draft", "name": "Draft", "enabled": False, "url": "",
        }]})
        self.assertEqual(channels[0]["id"], "draft")

    @patch("control.app.notify_push._deliver_with_retry")
    def test_dispatch_delivers_through_feishu_without_other_channels(self, deliver):
        settings = {"feishu": {
            "enabled": True,
            "url": "https://open.feishu.cn/open-apis/bot/v2/hook/test-token",
            "events": {EV_INCOMING_SMS: True},
        }}
        for future in notify_push.dispatch(settings, EV_INCOMING_SMS, {"id": 1}, "+100", "hello"):
            future.result(timeout=5)
        deliver.assert_called_once()
        self.assertEqual(deliver.call_args.args[0], "feishu")
        self.assertIs(deliver.call_args.args[1], send_feishu)

    @patch("control.app.notify_push._deliver_with_retry")
    def test_dispatch_fans_out_to_matching_feishu_channels(self, deliver):
        settings = {"feishu": {"channels": [
            {"id": "all", "enabled": True, "instances": [],
             "events": {EV_INCOMING_SMS: True}},
            {"id": "line-2", "enabled": True, "instances": ["2"],
             "events": {EV_INCOMING_SMS: True}},
            {"id": "line-3", "enabled": True, "instances": ["3"],
             "events": {EV_INCOMING_SMS: True}},
        ]}}
        for future in notify_push.dispatch(settings, EV_INCOMING_SMS, {"id": "2"}, "+100", "hello"):
            future.result(timeout=5)
        self.assertEqual(deliver.call_count, 2)
        self.assertCountEqual([call.args[0] for call in deliver.call_args_list],
                              ["feishu:all", "feishu:line-2"])

    @patch("control.app.notify_push._deliver_with_retry")
    def test_line_filtered_feishu_channels_skip_gateway_events(self, deliver):
        settings = {"feishu": {"channels": [
            {"id": "lines", "enabled": True, "instances": ["2"],
             "events": {notify_push.EV_SOFTWARE_UPDATE: True}},
            {"id": "global", "enabled": True, "instances": [],
             "events": {notify_push.EV_SOFTWARE_UPDATE: True}},
        ]}}
        for future in notify_push.dispatch(settings, notify_push.EV_SOFTWARE_UPDATE,
                                            {}, "1.8.0", "update"):
            future.result(timeout=5)
        deliver.assert_called_once()
        self.assertEqual(deliver.call_args.args[0], "feishu:global")

    def test_slow_channels_do_not_hold_the_caller_or_delay_feishu(self):
        release = threading.Event()
        started = {channel: threading.Event()
                   for channel in ("webhook", "telegram", "pushplus", "feishu")}
        workers = []

        def deliver(channel, sender, channel_cfg, payload):
            workers.append(threading.current_thread().name)
            started[channel].set()
            if not release.wait(5):
                raise TimeoutError("delivery test was not released")

        settings = {channel: {"enabled": True, "events": {EV_INCOMING_SMS: True}}
                    for channel in started}
        with ThreadPoolExecutor(max_workers=1) as caller, \
                patch.object(notify_push, "_deliver_with_retry", side_effect=deliver):
            try:
                queued = caller.submit(notify_push.dispatch, settings, EV_INCOMING_SMS,
                                       {"id": "1"}, "+100", "hello")
                futures = queued.result(timeout=2)
                self.assertEqual(caller.submit(lambda: "free").result(timeout=2), "free")
                for event in started.values():
                    self.assertTrue(event.wait(2))
                self.assertTrue(all(name.startswith("mdd-notify") for name in workers))
            finally:
                release.set()
            for future in futures:
                future.result(timeout=2)

    def test_manual_telegram_proxy_is_applied_without_environment_proxy(self):
        session = telegram_session({"proxy_mode": "manual",
                                     "proxy_url": "socks5h://127.0.0.1:1080"})
        try:
            self.assertFalse(session.trust_env)
            self.assertEqual(session.proxies["https"], "socks5h://127.0.0.1:1080")
        finally:
            session.close()

    def test_telegram_uses_a_socks5_entry_from_the_shared_proxy_library(self):
        settings = {"proxy": {"profiles": {"primary": {
            "name": "Primary", "type": "socks5", "server": "proxy.example",
            "port": 1081, "username": "a@b", "password": "p:/w",
        }}}}
        with patch.object(config, "get_settings", return_value=settings):
            session = telegram_session({"proxy_mode": "library",
                                        "proxy_profile_id": "primary"})
        try:
            expected = "socks5h://a%40b:p%3A%2Fw@proxy.example:1081"
            self.assertEqual(session.proxies["http"], expected)
            self.assertEqual(session.proxies["https"], expected)
        finally:
            session.close()

    def test_telegram_library_node_reuses_its_ready_country_exit(self):
        settings = {"proxy": {
            "profiles": {"primary": {"name": "Primary", "type": "node"}},
            "exits": {"gb": {"enabled": True, "profile_id": "primary"}},
        }}
        live = {"exits": {"gb": {"ready": True, "proxy_host": "172.17.0.1",
                                     "proxy_port": 22027}}}
        with patch.object(config, "get_settings", return_value=settings), \
                patch("control.app.notify_push.egress.status", return_value=live):
            session = telegram_session({"proxy_mode": "library",
                                        "proxy_profile_id": "primary"})
        try:
            self.assertEqual(session.proxies["https"], "socks5h://172.17.0.1:22027")
        finally:
            session.close()

    def test_country_telegram_proxy_uses_remote_dns_through_verified_exit(self):
        with patch("control.app.notify_push.egress.status", return_value={"exits": {
                "gb": {"ready": True, "interface": "mdd-gb",
                       "proxy_host": "172.17.0.1", "proxy_port": 22027}}}):
            session = telegram_session({"proxy_mode": "country", "proxy_country": "GB"})
            try:
                self.assertEqual(session.proxies["http"], "socks5h://172.17.0.1:22027")
                self.assertEqual(session.proxies["https"], "socks5h://172.17.0.1:22027")
            finally:
                session.close()
        with patch("control.app.notify_push.egress.status", return_value={"exits": {}}):
            with self.assertRaisesRegex(RuntimeError, "not ready"):
                telegram_session({"proxy_mode": "country", "proxy_country": "gb"})

    def test_delivery_history_retries_and_never_stores_message_body(self):
        calls = []
        def sender(_cfg, _payload):
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("contains a secret response")
            return {"ok": True, "status_code": 204}
        with tempfile.TemporaryDirectory() as temp, patch.dict(
                "os.environ", {"MDD_DATA": temp}), patch(
                    "control.app.notify_push.time.sleep", return_value=None):
            _deliver_with_retry("webhook", sender, {}, {
                "event": EV_INCOMING_SMS, "instance": "line", "text": "private text"})
            history = delivery_status()["history"]
        self.assertEqual(len(calls), 3)
        self.assertEqual(history[0]["status"], "delivered")
        self.assertEqual(history[0]["attempts"], 3)
        self.assertNotIn("private text", str(history))


if __name__ == "__main__":
    unittest.main()


class BrandPrefixTests(unittest.TestCase):
    """A push arrives out of context — a lock screen, a Telegram list beside other bots — and
    the event alone does not say which machine is talking."""

    EVENTS = ("incoming_sms", "incoming_call", "missed_call", "voicemail_received",
              "host_alert", "number_changed", "line_unrecoverable", "keepalive_result",
              "balance_low", "software_update")

    def _payload(self, event):
        return notify_push.build_payload(
            event, {"id": "1", "name": "UK SIM", "msisdn": "+447700900123"},
            "+447700900321", "detail")

    def test_every_title_leads_with_the_brand(self):
        for event in self.EVENTS:
            title = notify_push.build_notification_message(self._payload(event))["title"]
            self.assertTrue(title.startswith(notify_push.BRAND), f"{event}: {title}")

    def test_every_telegram_message_leads_with_the_brand(self):
        for event in self.EVENTS:
            first = notify_push._telegram_text(self._payload(event)).splitlines()[0]
            self.assertIn(notify_push.BRAND, first, f"{event}: {first}")

    def test_the_brand_is_not_repeated_when_the_text_already_carries_it(self):
        # The software-update wording names the product, so a blind prefix would read
        # "MDD · MDD Sim Gateway 新版本".
        title = notify_push.build_notification_message(
            self._payload("software_update"))["title"]
        self.assertEqual(title.count(notify_push.BRAND), 1, title)
        first = notify_push._telegram_text(
            self._payload("software_update")).splitlines()[0]
        self.assertEqual(first.count(notify_push.BRAND), 1, first)

    def test_the_icon_stays_leftmost_in_telegram(self):
        # It is what makes the event type scannable in a chat list.
        first = notify_push._telegram_text(self._payload("missed_call")).splitlines()[0]
        self.assertFalse(first.startswith(notify_push.BRAND), first)
        self.assertLess(first.index("📵"), first.index(notify_push.BRAND))


class HostAlertNotificationTests(unittest.TestCase):
    """The host alert is not a SIM event. Rendering it through the call/SMS path produced
    "📞 Incoming call — SIM: Raspberry Pi 3 Model B, From: Raspberry Pi 3 Model B"."""

    def _payload(self):
        return notify_push.build_payload(
            notify_push.EV_HOST_ALERT,
            {"id": "host", "name": "Raspberry Pi 3 Model B"},
            "Raspberry Pi 3 Model B",
            "[warning] 检测到历史欠压事件。")

    def test_telegram_does_not_render_it_as_a_call(self):
        text = notify_push._telegram_text(self._payload())
        self.assertNotIn("Incoming call", text)
        self.assertNotIn("SIM:", text)
        self.assertIn("网关主机异常", text)
        self.assertIn("欠压", text)

    def test_the_shared_message_builder_uses_host_wording(self):
        message = notify_push.build_notification_message(self._payload())
        self.assertIn("主机", message["title"])
        self.assertNotIn("来电", message["title"])
        self.assertIn("欠压", message["content"])

    def test_a_channel_can_switch_host_alerts_off_independently(self):
        enabled = notify_push._events_enabled({"events": {"host_alert": False}})
        self.assertFalse(enabled[notify_push.EV_HOST_ALERT])
        # The other categories are unaffected by that choice.
        self.assertTrue(enabled[notify_push.EV_INCOMING_SMS])
        self.assertTrue(enabled[notify_push.EV_INCOMING_CALL])

    def test_a_freshly_enabled_channel_gets_host_alerts(self):
        self.assertTrue(notify_push._events_enabled({})[notify_push.EV_HOST_ALERT])


class MissedCallNotificationTests(unittest.TestCase):
    """A missed call is the outcome of a call nobody answered — a different event from the
    ringing announcement, and the one that matters when no browser was open."""

    def test_it_is_enabled_by_default_and_can_be_disabled_per_channel(self):
        self.assertTrue(notify_push._events_enabled({})[notify_push.EV_MISSED_CALL])
        self.assertFalse(notify_push._events_enabled({"events": {
            notify_push.EV_MISSED_CALL: False,
        }})[notify_push.EV_MISSED_CALL])

    def test_it_is_worded_as_a_missed_call_not_a_ringing_one(self):
        payload = notify_push.build_payload(
            notify_push.EV_MISSED_CALL,
            {"id": "1", "name": "UK SIM", "msisdn": "+447700900123"},
            "+447700900321", None)
        built = notify_push.build_notification_message(payload)
        telegram = notify_push._telegram_text(payload)
        self.assertIn("未接来电", built["title"])
        self.assertIn("+447700900321", built["content"])
        self.assertIn("Missed call", telegram)
        self.assertNotIn("Incoming call", telegram)
        self.assertNotIn("Incoming SMS", telegram)

    def test_it_carries_no_message_body(self):
        # Nothing was said into a call that was never answered; a body here could only be a
        # leak from an adjacent SMS event.
        payload = notify_push.build_payload(
            notify_push.EV_MISSED_CALL, {"id": "1"}, "+15550000", "secret sms body")
        self.assertIsNone(payload["text"])

    def test_it_is_toggleable_independently_of_incoming_calls(self):
        settings = {"telegram": {"enabled": True, "events": {
            notify_push.EV_INCOMING_CALL: False, notify_push.EV_MISSED_CALL: True}}}
        self.assertTrue(notify_push.has_enabled_channel(
            settings, notify_push.EV_MISSED_CALL))
        self.assertFalse(notify_push.has_enabled_channel(
            settings, notify_push.EV_INCOMING_CALL))


class NumberChangeNotificationTests(unittest.TestCase):
    """A ported number changes the line's caller identity, so it is announced rather than
    silently corrected."""

    def _payload(self):
        return notify_push.build_payload(
            notify_push.EV_NUMBER_CHANGED,
            {"id": "5", "name": "voxi", "msisdn": "+447700900123"},
            "+447700900123", "+447700900456 → +447700900123")

    def test_it_is_not_rendered_as_a_call_or_an_sms(self):
        text = notify_push._telegram_text(self._payload())
        self.assertNotIn("Incoming call", text)
        self.assertNotIn("Incoming SMS", text)
        self.assertIn("号码已变更", text)
        self.assertIn("+447700900123", text)

    def test_the_shared_builder_names_the_line(self):
        message = notify_push.build_notification_message(self._payload())
        self.assertIn("voxi", message["title"])
        self.assertIn("+447700900456", message["content"])

    def test_it_has_its_own_per_channel_toggle(self):
        enabled = notify_push._events_enabled({"events": {"number_changed": False}})
        self.assertFalse(enabled[notify_push.EV_NUMBER_CHANGED])
        self.assertTrue(enabled[notify_push.EV_HOST_ALERT])
        self.assertTrue(enabled[notify_push.EV_INCOMING_SMS])


class UnrecoverableLineNotificationTests(unittest.TestCase):
    """A gateway that has stopped trying must say so, and must not be mistaken for a call."""

    PAYLOAD = {"event": notify_push.EV_LINE_UNRECOVERABLE, "sim_name": "voxi",
               "instance": "5", "from": "SD-US", "text": "所有候选出口都试过了。"}

    def test_it_is_not_rendered_as_a_call_or_an_sms(self):
        text = notify_push._telegram_text(self.PAYLOAD)
        self.assertIn("线路无法自动恢复", text)
        self.assertNotIn("Incoming call", text)
        self.assertNotIn("Incoming SMS", text)

    def test_the_shared_builder_names_the_line_and_carries_the_reason(self):
        built = notify_push.build_notification_message(self.PAYLOAD)
        self.assertIn("voxi", built["title"])
        self.assertEqual(built["content"], self.PAYLOAD["text"])

    def test_it_has_its_own_per_channel_toggle(self):
        self.assertFalse(notify_push._events_enabled(
            {"events": {notify_push.EV_LINE_UNRECOVERABLE: False}}
        )[notify_push.EV_LINE_UNRECOVERABLE])
        self.assertTrue(notify_push._events_enabled({})[notify_push.EV_LINE_UNRECOVERABLE])
