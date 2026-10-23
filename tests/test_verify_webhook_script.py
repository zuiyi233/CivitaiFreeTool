# -*- coding: utf-8 -*-
"""真机 webhook 验收脚本自身的测试（scripts/verify_webhook_delivery.py）。

这条脚本是「task.* 推送已真机证实」这一结论的唯一证据来源，所以它自己的签名校验必须可信：
签名对不上、body 被改过一个字节、时间戳缺失，都必须被标成失败，否则验收结论是假的。
"""
import hashlib
import hmac
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import ai_contract                      # noqa: E402
import verify_webhook_delivery as vwd  # noqa: E402

SECRET = "0123456789abcdef0123456789abcdef"


def sign(secret, timestamp, raw):
    return "sha256=" + hmac.new(secret.encode("utf-8"),
                                 ("%s." % timestamp).encode("utf-8") + raw,
                                 hashlib.sha256).hexdigest()


def headers(event, raw, timestamp, signature):
    return {"x-cft-event": event, "x-cft-group": "task", "x-cft-delivery": "d-1",
            "x-cft-timestamp": timestamp, "x-cft-signature": signature}


def receiver():
    # Receiver.__init__ 会起真实端口，这里只用它的记录逻辑，不起服务。
    obj = vwd.Receiver.__new__(vwd.Receiver)
    obj.secret = SECRET
    obj.rows = []
    return obj


class SignatureCheckTest(unittest.TestCase):
    def test_valid_signature_is_accepted(self):
        raw = json.dumps({"seq": 7, "type": "task.finished", "group": "task",
                          "data": {"id": "t1", "status": "done"}}).encode("utf-8")
        ts = str(int(time.time()))
        r = receiver()
        r.record(headers("task.finished", raw, ts, sign(SECRET, ts, raw)), raw)
        row = r.rows[0]
        self.assertTrue(row["signature_ok"], row)
        self.assertTrue(row["status_same_as_body"])
        self.assertLess(row["clock_skew_s"], 5)
        self.assertEqual(row["event"], "task.finished")
        self.assertEqual(row["seq"], 7)
        self.assertEqual(row["data"]["status"], "done")

    def test_tampered_body_fails_signature(self):
        raw = json.dumps({"seq": 7, "type": "task.finished",
                          "data": {"status": "done"}}).encode("utf-8")
        ts = str(int(time.time()))
        signature = sign(SECRET, ts, raw)
        tampered = raw.replace(b"done", b"error")
        self.assertNotEqual(tampered, raw)
        r = receiver()
        r.record(headers("task.finished", tampered, ts, signature), tampered)
        self.assertFalse(r.rows[0]["signature_ok"])

    def test_wrong_secret_fails_signature(self):
        raw = json.dumps({"seq": 1, "type": "task.created", "data": {}}).encode("utf-8")
        ts = str(int(time.time()))
        r = receiver()
        r.record(headers("task.created", raw, ts, sign("deadbeef" * 4, ts, raw)), raw)
        self.assertFalse(r.rows[0]["signature_ok"])

    def test_missing_timestamp_is_not_silently_ok(self):
        raw = json.dumps({"seq": 2, "type": "task.updated", "data": {}}).encode("utf-8")
        r = receiver()
        r.record(headers("task.updated", raw, "", "sha256=whatever"), raw)
        row = r.rows[0]
        self.assertFalse(row["signature_ok"])
        self.assertIsNone(row["clock_skew_s"])

    def test_probe_and_download_requirements_differ(self):
        """探针模式只要求 webhook.test；下载模式必须三种 task.* 都在场。"""
        self.assertEqual(vwd.PROBE, "webhook.test")
        self.assertNotIn(vwd.PROBE, ai_contract.EVENT_TYPES)
        self.assertEqual(vwd.NEEDED, ("task.created", "task.updated", "task.finished"))


if __name__ == "__main__":
    unittest.main()
