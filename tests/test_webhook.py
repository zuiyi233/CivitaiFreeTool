# -*- coding: utf-8 -*-
"""webhook 出站传输：URL 校验、HMAC、重试、背压、脱敏与测试探针。全部本机。"""
import hashlib
import hmac
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ai_contract
import automation_api
import automation_events
import webhook_dispatch

SECRET = "0123456789abcdef0123456789abcdef"


def event(seq=1, event_type="task.updated", group="task", data=None):
    return {
        "seq": seq, "instance_id": "inst0001", "type": event_type, "group": group,
        "at": "2026-10-24T12:00:00+08:00", "data": data if data is not None else {"progress": 10},
    }


def wait_until(predicate, timeout=3.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


class Receiver:
    """本机 HTTP 接收端；statuses 依次出队，空 = 200。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []
        self.statuses = []
        self.started = threading.Event()
        self.block = threading.Event()
        self.release = threading.Event()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                headers = {k.lower(): v for k, v in self.headers.items()}
                with receiver.lock:
                    receiver.requests.append({"path": self.path, "headers": headers, "body": body})
                    status = receiver.statuses.pop(0) if receiver.statuses else 200
                receiver.started.set()
                if receiver.block.is_set():
                    receiver.release.wait(5.0)
                self.send_response(status)
                if status in (301, 302, 303, 307, 308):
                    self.send_header("Location", "/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d/hook" % self.server.server_port

    def stop(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


class WebhookTest(unittest.TestCase):
    def setUp(self):
        self.receiver = Receiver()
        self.addCleanup(self.receiver.stop)
        self.dispatchers = []

    def tearDown(self):
        for dispatcher in self.dispatchers:
            dispatcher.stop(flush_s=0.1)

    def make(self, url=None, *, enabled=True, events=("all",), secret=SECRET,
             allow_remote=False, **limits):
        options = {"timeout_s": 2.0, "max_attempts": 3,
                   "backoff_s": [0.01, 0.02], "test_timeout_s": 2.0}
        options.update(limits)
        dispatcher = webhook_dispatch.WebhookDispatcher(limits=options)
        dispatcher.apply_config({
            "webhook_enabled": enabled,
            "webhook_url": url if url is not None else self.receiver.url,
            "webhook_events": list(events),
            "webhook_secret": secret,
            "webhook_allow_remote": allow_remote,
        })
        self.dispatchers.append(dispatcher)
        return dispatcher

    def wait_sent(self, dispatcher, count=1, timeout=3.0):
        self.assertTrue(wait_until(lambda: dispatcher.status()["stats"]["sent"] >= count,
                                   timeout=timeout),
                        dispatcher.status())

    # ---- 基础投递 ----
    def test_offer_delivers_signed_event(self):
        dispatcher = self.make()
        self.assertTrue(dispatcher.offer(event(seq=7, data={"filename": "a.safetensors"})))
        self.wait_sent(dispatcher)
        request = self.receiver.requests[0]
        headers = request["headers"]
        body = request["body"]
        self.assertEqual(headers["x-cft-event"], "task.updated")
        self.assertEqual(headers["x-cft-group"], "task")
        self.assertEqual(headers["x-cft-seq"], "7")
        self.assertEqual(headers["x-cft-instance"], "inst0001")
        self.assertTrue(headers["x-cft-delivery"])
        self.assertNotIn("x-cft-synthetic", headers)
        ts = headers["x-cft-timestamp"]
        expected = hmac.new(SECRET.encode(), ("%s." % ts).encode() + body,
                            hashlib.sha256).hexdigest()
        self.assertEqual(headers["x-cft-signature"], "sha256=" + expected)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["type"], "task.updated")
        self.assertEqual(payload["seq"], 7)
        self.assertEqual(payload["data"], {"filename": "a.safetensors"})

    def test_default_filter_excludes_api(self):
        dispatcher = self.make(events=())
        self.assertFalse(dispatcher.offer(event(seq=1, event_type="api.call", group="api")))
        self.assertTrue(dispatcher.offer(event(seq=2)))
        self.wait_sent(dispatcher)
        self.assertEqual(len(self.receiver.requests), 1)
        self.assertEqual(dispatcher.status()["events"],
                         list(ai_contract.WEBHOOK_DEFAULT_EVENT_GROUPS))

    def test_filter_accepts_event_type_and_group(self):
        dispatcher = self.make(events=("task.finished", "ui"))
        self.assertTrue(dispatcher.offer(event(seq=1, event_type="task.finished")))
        self.assertFalse(dispatcher.offer(event(seq=2, event_type="task.updated")))
        self.assertTrue(dispatcher.offer(event(seq=3, event_type="ui.dismissed", group="ui")))
        self.wait_sent(dispatcher, 2)
        self.assertEqual([json.loads(r["body"])["seq"] for r in self.receiver.requests], [1, 3])

    def test_disabled_does_not_deliver(self):
        dispatcher = self.make(enabled=False)
        self.assertFalse(dispatcher.offer(event()))
        self.assertFalse(dispatcher.status()["enabled"])
        self.assertEqual(self.receiver.requests, [])

    # ---- 重试 / 失败 ----
    def test_retry_then_success(self):
        self.receiver.statuses = [500, 500, 200]
        dispatcher = self.make()
        self.assertTrue(dispatcher.offer(event()))
        self.wait_sent(dispatcher)
        status = dispatcher.status()
        self.assertEqual(status["stats"]["sent"], 1)
        self.assertEqual(status["stats"]["retried"], 2)
        self.assertEqual(status["stats"]["failed"], 0)
        self.assertEqual(len(self.receiver.requests), 3)
        row = status["deliveries"][0]
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["attempts"], 3)

    def test_no_retry_on_400(self):
        self.receiver.statuses = [400]
        dispatcher = self.make()
        self.assertTrue(dispatcher.offer(event()))
        self.assertTrue(wait_until(
            lambda: dispatcher.status()["stats"]["failed"] == 1, timeout=2.0))
        self.assertEqual(len(self.receiver.requests), 1)
        row = dispatcher.status()["deliveries"][0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["http_status"], 400)

    def test_redirect_is_not_followed(self):
        self.receiver.statuses = [302]
        dispatcher = self.make()
        self.assertTrue(dispatcher.offer(event()))
        self.assertTrue(wait_until(
            lambda: dispatcher.status()["stats"]["failed"] == 1, timeout=2.0))
        self.assertEqual(len(self.receiver.requests), 1)
        self.assertEqual(dispatcher.status()["deliveries"][0]["http_status"], 302)

    def test_queue_full_drops_new_event(self):
        self.receiver.block.set()
        dispatcher = self.make(queue_size=1)
        self.assertTrue(dispatcher.offer(event(seq=1)))
        self.assertTrue(self.receiver.started.wait(2.0))
        self.assertTrue(dispatcher.offer(event(seq=2)))
        self.assertFalse(dispatcher.offer(event(seq=3)))
        self.assertEqual(dispatcher.status()["stats"]["dropped"], 1)
        self.receiver.release.set()
        self.wait_sent(dispatcher, 2)

    # ---- 配置校验 ----
    def test_public_target_rejected_by_default(self):
        with self.assertRaises(webhook_dispatch.WebhookConfigError):
            webhook_dispatch.validate_url("http://1.1.1.1/hook", allow_remote=False)
        self.assertEqual(webhook_dispatch.validate_url("http://1.1.1.1/hook", allow_remote=True),
                         "http://1.1.1.1/hook")

    def test_url_validation(self):
        bad = [
            "ftp://127.0.0.1/hook",
            "http://user:pass@127.0.0.1:1/hook",
            "http://127.0.0.1:1/hook#frag",
            "http://127.0.0.1:99999/hook",
            "",
        ]
        for url in bad:
            with self.assertRaises(webhook_dispatch.WebhookConfigError, msg=url):
                webhook_dispatch.validate_url(url, allow_remote=True)

    def test_config_keys_are_writable_and_defaulted(self):
        import browser_bridge
        import config
        for key in webhook_dispatch.WEBHOOK_KEYS:
            self.assertIn(key, browser_bridge.AUTOMATION_CONFIG_KEYS)
            self.assertIn(key, config.DEFAULTS)

    def test_secret_required_and_events_validated(self):
        with self.assertRaises(webhook_dispatch.WebhookConfigError):
            webhook_dispatch.normalize_config({
                "webhook_enabled": True, "webhook_url": self.receiver.url,
                "webhook_secret": "short", "webhook_events": ["all"],
            })
        with self.assertRaises(webhook_dispatch.WebhookConfigError):
            webhook_dispatch.normalize_config({
                "webhook_enabled": True, "webhook_url": self.receiver.url,
                "webhook_secret": SECRET, "webhook_events": ["nope"],
            })
        self.assertIsNone(webhook_dispatch.validate_patch({"download_dir": "D:/x"}, {}))

    def test_status_redacts_url_path_and_secret(self):
        url = self.receiver.url + "?token=super-secret-token"
        dispatcher = self.make(url=url)
        status = dispatcher.status()
        blob = json.dumps(status, ensure_ascii=False)
        self.assertNotIn("super-secret-token", blob)
        self.assertNotIn(SECRET, blob)
        self.assertTrue(status["secret_set"])
        self.assertEqual(status["url"]["host"], "127.0.0.1")
        self.assertTrue(status["url"]["path_set"])
        self.assertNotIn("path", status["url"])

    # ---- 测试探针 / 日志 ----
    def test_probe_is_synthetic_and_signed(self):
        dispatcher = self.make()
        result = dispatcher.test_delivery(timeout=2.0)
        self.assertTrue(result["ok"], result)
        request = self.receiver.requests[0]
        self.assertEqual(request["headers"]["x-cft-event"], "webhook.test")
        self.assertEqual(request["headers"]["x-cft-synthetic"], "1")
        payload = json.loads(request["body"].decode("utf-8"))
        self.assertEqual(payload["type"], "webhook.test")
        self.assertEqual(payload["group"], "webhook")
        self.assertTrue(payload["data"]["probe"])
        ts = request["headers"]["x-cft-timestamp"]
        expected = hmac.new(SECRET.encode(), ("%s." % ts).encode() + request["body"],
                            hashlib.sha256).hexdigest()
        self.assertEqual(request["headers"]["x-cft-signature"], "sha256=" + expected)

    def test_clear_log_keeps_stats(self):
        dispatcher = self.make()
        self.assertTrue(dispatcher.offer(event()))
        self.wait_sent(dispatcher)
        self.assertGreaterEqual(len(dispatcher.status()["deliveries"]), 1)
        self.assertGreaterEqual(dispatcher.clear_log(), 1)
        self.assertEqual(dispatcher.status()["deliveries"], [])
        self.assertGreaterEqual(dispatcher.status()["stats"]["sent"], 1)


class FakeApi:
    def __init__(self, cfg):
        self.cfg = cfg


def http_ctx():
    return {
        "version": "2.7.0",
        "config_keys": ["webhook_enabled", "webhook_url", "webhook_events",
                        "webhook_secret", "webhook_allow_remote"],
        "state": lambda: {"ok": True, "config": {}, "tasks": [], "ui": {}},
    }


class WebhookHttpTest(unittest.TestCase):
    def setUp(self):
        automation_events.shutdown()
        self.receiver = Receiver()
        self.addCleanup(self.receiver.stop)

    def tearDown(self):
        webhook_dispatch.shutdown(flush_s=0.1)
        automation_events.shutdown()

    def cfg(self, enabled=True):
        return {
            "webhook_enabled": enabled, "webhook_url": self.receiver.url,
            "webhook_events": ["all"], "webhook_secret": SECRET,
            "webhook_allow_remote": False,
        }

    def test_status_redacts_and_probe_delivers(self):
        cfg = self.cfg()
        api = FakeApi(cfg)
        webhook_dispatch.refresh_from_config(cfg, None)
        status, body = automation_api.dispatch("/api/automation/webhooks", "GET", {},
                                               None, api, http_ctx())
        self.assertEqual(status, 200, body)
        self.assertTrue(body["enabled"])
        blob = json.dumps(body, ensure_ascii=False)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("/hook", blob)
        status, body = automation_api.dispatch("/api/automation/webhooks", "POST", {},
                                               {"action": "test"}, api, http_ctx())
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertEqual(self.receiver.requests[0]["headers"]["x-cft-event"], "webhook.test")

    def test_test_action_rejects_when_disabled(self):
        cfg = self.cfg(enabled=False)
        api = FakeApi(cfg)
        webhook_dispatch.refresh_from_config(cfg, None)
        status, body = automation_api.dispatch("/api/automation/webhooks", "POST", {},
                                               {"action": "test"}, api, http_ctx())
        self.assertEqual(status, 409, body)
        self.assertEqual(body["code"], "conflict")
        self.assertIn("webhook_enabled", body["hint"])

    def test_bad_action_is_rejected(self):
        api = FakeApi(self.cfg())
        status, body = automation_api.dispatch("/api/automation/webhooks", "POST", {},
                                               {"action": "nope"}, api, http_ctx())
        self.assertEqual(status, 400, body)
        self.assertEqual(body["code"], "invalid_argument")

    def test_get_is_read_only_and_does_not_requeue_validation(self):
        cfg = self.cfg()
        webhook_dispatch.refresh_from_config(cfg, None)
        calls = []
        original = webhook_dispatch.refresh_from_config

        def spy(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        webhook_dispatch.refresh_from_config = spy
        try:
            status, body = automation_api.dispatch("/api/automation/webhooks", "GET", {},
                                                   None, FakeApi(cfg), http_ctx())
        finally:
            webhook_dispatch.refresh_from_config = original
        self.assertEqual(status, 200, body)
        self.assertTrue(body["enabled"])
        self.assertEqual(calls, [], "GET /webhooks 必须只读，不能重新做 DNS/配置校验")

    def test_config_get_masks_webhook_url_and_secret(self):
        cfg = self.cfg()
        status, body = automation_api.dispatch("/api/automation/config", "GET", {},
                                               None, FakeApi(cfg), http_ctx())
        self.assertEqual(status, 200, body)
        blob = json.dumps(body, ensure_ascii=False)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("/hook", blob)
        self.assertTrue(body["config"]["webhook_url"].endswith("/***"))
        self.assertEqual(body["config"]["webhook_secret"], "***")

    def test_schema_masks_webhook_secret_and_url(self):
        cfg = self.cfg()
        api = FakeApi(cfg)
        status, body = automation_api.dispatch("/api/automation/schema", "GET", {},
                                               None, api, http_ctx())
        self.assertEqual(status, 200, body)
        keys = body["config"]
        self.assertEqual(keys["webhook_secret"]["current"], "***")
        self.assertEqual(keys["webhook_url"]["current"], "http://127.0.0.1:%d/***"
                         % self.receiver.server.server_port)
        self.assertEqual(keys["webhook_secret"]["type"], "secret")


if __name__ == "__main__":
    unittest.main()
