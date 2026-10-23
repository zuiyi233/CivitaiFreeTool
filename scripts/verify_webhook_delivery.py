# -*- coding: utf-8 -*-
"""用一次真实下载验证出站 webhook 的 task.* 链路（只连回环，不碰浏览器）。

为什么单独一个脚本：`webhook.test` 合成探针只证明「签名与连通」，不证明下载器真的把
`task.created / task.updated / task.finished` 推进了队列。这个脚本把整条链跑完并如实报告
收到了哪些事件、每条签名校验是否通过，配置在结束时按原值还原。

跑法：
    python scripts/verify_webhook_delivery.py --model-url "https://civitai.red/models/..."
可选：
    --probe-only        只发合成探针，不做下载
    --dest-dir DIR      临时下载目录（写进 webhook 期间的 download_target_dir，退时还原）
    --keep-task         结束后不删任务行（默认会 archive 掉本次产生的任务）
"""
import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_contract          # noqa: E402

WEBHOOK_KEYS = ("webhook_enabled", "webhook_url", "webhook_events",
                "webhook_secret", "webhook_allow_remote")
NEEDED = ("task.created", "task.updated", "task.finished")
PROBE = "webhook.test"          # 合成探针，不在 ai_contract.EVENTS 的 21 类里


def _request(bridge, path, body=None, method=None, timeout=30):
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(bridge.rstrip("/") + path, data=data, headers=headers,
                                 method=method or ("POST" if data else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


class Receiver:
    """本机接收端：记录每条投递的头与原始 body，并校验 HMAC-SHA256 签名。"""

    def __init__(self, secret):
        self.secret = secret
        self.rows = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                headers = {k.lower(): v for k, v in self.headers.items()}
                receiver.record(headers, raw)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d/cft-hook" % self.server.server_port

    def record(self, headers, raw):
        signature = str(headers.get("x-cft-signature") or "")
        timestamp = str(headers.get("x-cft-timestamp") or "")
        expected = "sha256=" + hmac.new(self.secret.encode("utf-8"),
                                        ("%s." % timestamp).encode("utf-8") + raw,
                                        hashlib.sha256).hexdigest()
        try:
            skew = abs(time.time() - float(timestamp))
        except ValueError:
            skew = None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            payload = {}
        self.rows.append({
            "event": str(headers.get("x-cft-event") or ""),
            "group": str(headers.get("x-cft-group") or ""),
            "seq": int(payload.get("seq") or 0),
            "delivery": str(headers.get("x-cft-delivery") or ""),
            "synthetic": headers.get("x-cft-synthetic") == "1",
            "signature_ok": hmac.compare_digest(signature, expected),
            "clock_skew_s": round(skew, 1) if skew is not None else None,
            "status_same_as_body": payload.get("type") == headers.get("x-cft-event"),
            "data": payload.get("data") if isinstance(payload.get("data"), dict) else {},
        })

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def wait_for(bridge, seconds, want_status=("sent", "failed")):
    """等投递线程把队列清空，最多 seconds 秒。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        _, doc = _request(bridge, "/api/automation/webhooks")
        stats = (doc or {}).get("stats") or {}
        if not stats.get("pending") and not stats.get("in_flight"):
            return True
        time.sleep(0.5)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", default=os.environ.get("CFT_BRIDGE", "http://127.0.0.1:47531"))
    parser.add_argument("--model-url", default="", help="C 站模型页 URL；留空则只发合成探针")
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--dest-dir", default="", help="临时下载目录（覆盖 download_target_dir）")
    parser.add_argument("--timeout-s", type=float, default=900.0, help="等下载收尾的上限秒数")
    parser.add_argument("--keep-task", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    host = urllib.parse.urlsplit(args.bridge).hostname or ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit("只允许回环地址的桥，当前是 %r" % host)

    _, health = _request(args.bridge, "/api/health")
    app_version = str(health.get("version") or "")

    _, baseline = _request(args.bridge, "/api/automation/config")
    saved = {}
    for key in WEBHOOK_KEYS + ("download_target_dir", "route_by_type"):
        saved[key] = (baseline.get("config") or {}).get(key)

    receiver = Receiver(secrets.token_hex(24))
    secret = receiver.secret
    patch = dict(zip(WEBHOOK_KEYS, (
        True, receiver.url, ["all"], secret, False)))
    if args.dest_dir:
        patch["download_target_dir"] = os.path.abspath(args.dest_dir)
        patch["route_by_type"] = False
    task_ids = []
    download_result = None
    exit_code = 0
    try:
        _request(args.bridge, "/api/automation/config", {"config": patch})
        if args.probe_only or not args.model_url:
            status, probe = _request(args.bridge, "/api/automation/webhooks",
                                     {"action": "test", "timeout": 15})
            probe_ok = bool(probe.get("ok"))
        else:
            probe_ok = True
            status, download_result = _request(
                args.bridge, "/api/automation/tool/cft_download_model",
                {"url": args.model_url, "wait": True, "verify": True,
                 "timeout_s": int(args.timeout_s)},
                timeout=args.timeout_s + 120)
            # 工具端点返回 {ok, data:{...}}；下载结论在 data 里。
            if isinstance((download_result or {}).get("data"), dict):
                download_result = download_result["data"]
            for row in ((download_result or {}).get("items") or []):
                task = (row or {}).get("task") or {}
                if task.get("id"):
                    task_ids.append(str(task["id"]))
            if (download_result.get("task") or {}).get("id"):
                task_ids.append(str(download_result["task"]["id"]))
        wait_for(args.bridge, 30)
        events = Counter(row["event"] for row in receiver.rows)
        got = set(events)
        needed = NEEDED if download_result else (PROBE,)
        report = {
            "ok": bool(probe_ok) and all(name in got for name in needed),
            "app_version": app_version,
            "probe_ok": probe_ok,
            "delivered_total": len(receiver.rows),
            "events": dict(events),
            "needed_present": {name: name in got for name in needed},
            "signature_all_valid": all(row["signature_ok"] for row in receiver.rows),
            "header_body_consistent": all(row["status_same_as_body"] for row in receiver.rows),
            "max_clock_skew_s": max([row["clock_skew_s"] or 0 for row in receiver.rows], default=0),
            "synthetic_rows": sum(1 for row in receiver.rows if row["synthetic"]),
            "download": {
                "ok": bool((download_result or {}).get("ok")),
                "task_id": ((download_result or {}).get("task") or {}).get("id"),
                "status": ((download_result or {}).get("task") or {}).get("status"),
                "waited_s": (download_result or {}).get("waited_s"),
                "verify": (download_result or {}).get("verify"),
                "placement": (download_result or {}).get("placement") or {},
            } if download_result else None,
        }
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print("webhook 收到 %d 条投递，签名全部有效=%s" %
                  (report["delivered_total"], report["signature_all_valid"]))
            for name in needed:
                print("  %-14s %s x%d" % (name, "有" if name in got else "缺", events.get(name, 0)))
            if download_result:
                print("  下载 ok=%s task=%s status=%s waited=%s" %
                      (report["download"]["ok"], report["download"]["task_id"],
                       report["download"]["status"], report["download"]["waited_s"]))
            print("结论：%s" % ("链路完整" if report["ok"] else "链路不完整（见缺失项）"))
        exit_code = 0 if report["ok"] else 1
    finally:
        if task_ids and not args.keep_task:
            try:
                _request(args.bridge, "/api/automation/tool/cft_control_tasks",
                         {"action": "archive", "ids": task_ids})
            except Exception:
                pass
        restore = {key: (saved.get(key) if saved.get(key) is not None
                         else ai_contract.DEFAULTS.get(key))
                   for key in patch}
        try:
            _request(args.bridge, "/api/automation/config", {"config": restore})
        except Exception as exc:
            print("警告：webhook 配置未能还原（%s）" % type(exc).__name__, file=sys.stderr)
        receiver.stop()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
