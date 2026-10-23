#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenAPI + 每工具别名 + SSE 冒烟（只读，不启动下载）。"""
import argparse
import hashlib
import hmac
import http.client
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def get_json(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url, body, timeout=30):
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def read_sse_frame(response, timeout=12):
    """读取一个非注释 SSE 帧；返回 (event_type, id, payload)。"""
    deadline = time.time() + timeout
    event_type = event_id = None
    data_lines = []
    while time.time() < deadline:
        line = response.readline()
        if not line:
            raise RuntimeError("SSE 连接已关闭")
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        if text.startswith(":"):
            continue
        if text == "":
            if not data_lines:
                continue
            payload = json.loads("\n".join(data_lines))
            payload.setdefault("type", event_type)
            return event_type or payload.get("type"), event_id, payload
        if text.startswith("event:"):
            event_type = text.split(":", 1)[1].strip()
        elif text.startswith("id:"):
            event_id = text.split(":", 1)[1].strip()
        elif text.startswith("data:"):
            data_lines.append(text.split(":", 1)[1].strip())
    raise TimeoutError("等待 SSE 帧超时")


def try_frame(response, timeout=2.0):
    """读一帧，超时返回 None——用于「缓冲区里可能根本没有可重放事件」的场合。"""
    try:
        return read_sse_frame(response, timeout=timeout)
    except TimeoutError:
        return None


def run_cli(base, *args):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, "cft_cli.py")
    command = [sys.executable, script, *args, "--bridge", base, "--json"]
    proc = subprocess.run(command, capture_output=True, text=True,
                          encoding="utf-8", timeout=60)
    if proc.returncode != 0:
        raise RuntimeError("CLI %s 退出 %d: %s"
                           % (" ".join(args), proc.returncode, proc.stderr.strip()[:500]))
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError("CLI 输出不是 JSON: %s" % proc.stdout[:300]) from exc


def check_cli(base):
    doctor = run_cli(base, "doctor")
    if not doctor.get("ok"):
        raise RuntimeError("CLI doctor 失败: %s" % json.dumps(doctor, ensure_ascii=False)[:500])
    caps = run_cli(base, "call", "cft_capabilities")
    if not caps.get("ok"):
        raise RuntimeError("CLI call cft_capabilities 失败: %s" % caps)
    if not caps.get("data", {}).get("caps", {}).get("cli", {}).get("available"):
        raise RuntimeError("caps.cli.available 不为真")
    return {"doctor": True, "call": True, "version": caps.get("data", {}).get("version")}


class WebhookReceiver:
    def __init__(self):
        self.captured = []
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                receiver.captured.append(({k.lower(): v for k, v in self.headers.items()}, body))
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

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def check_webhook(base, force=False):
    before = get_json(base + "/api/automation/webhooks")
    occupied = bool(before.get("enabled") or before.get("secret_set")
                    or (before.get("url") or {}).get("host"))
    if occupied and not force:
        raise RuntimeError("检测到已有 webhook 配置；为避免覆盖真实 secret，"
                           "加 --force-webhook 才继续")
    receiver = WebhookReceiver()
    secret = secrets.token_hex(24)
    patch = {
        "webhook_enabled": True, "webhook_url": receiver.url,
        "webhook_events": ["all"], "webhook_secret": secret,
        "webhook_allow_remote": False,
    }
    try:
        status, saved = post_json(base + "/api/automation/config", {"config": patch})
        if status != 200 or not saved.get("ok"):
            raise RuntimeError("配置 webhook 失败: %s" % saved)
        if secret in json.dumps(saved, ensure_ascii=False):
            raise RuntimeError("配置保存响应泄露了 webhook secret")
        status, tested = post_json(base + "/api/automation/webhooks",
                                   {"action": "test", "timeout": 10})
        if status != 200 or not tested.get("ok"):
            raise RuntimeError("webhook test 失败: %s" % tested)
        deadline = time.time() + 5
        while time.time() < deadline:
            if any(headers.get("x-cft-event") == "webhook.test"
                   for headers, _body in receiver.captured):
                break
            time.sleep(0.05)
        probes = [(headers, body) for headers, body in receiver.captured
                  if headers.get("x-cft-event") == "webhook.test"]
        if not probes:
            raise RuntimeError("webhook test 没有到达本机接收端（收到的事件: %r）"
                               % [h.get("x-cft-event") for h, _ in receiver.captured])
        headers, body = probes[0]
        if tested.get("delivery") and headers.get("x-cft-delivery") != tested["delivery"]:
            raise RuntimeError("webhook test 的 delivery id 对不上")
        if headers.get("x-cft-synthetic") != "1":
            raise RuntimeError("webhook test 缺少 X-CFT-Synthetic")
        ts = headers.get("x-cft-timestamp") or ""
        signature = headers.get("x-cft-signature") or ""
        expected = "sha256=" + hmac.new(secret.encode(), ("%s." % ts).encode() + body,
                                        hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise RuntimeError("webhook 签名校验失败")
        view = get_json(base + "/api/automation/webhooks")
        blob = json.dumps(view, ensure_ascii=False)
        if secret in blob or "/cft-hook" in blob:
            raise RuntimeError("webhook 状态响应泄露 secret 或 URL 路径")
        return {"delivery": tested.get("delivery"), "http_status": tested.get("http_status"),
                "signature": True, "redacted": True,
                "event": tested.get("event")}
    finally:
        receiver.stop()
        try:
            post_json(base + "/api/automation/config", {"config": {
                "webhook_enabled": False, "webhook_url": "", "webhook_events": [],
                "webhook_secret": "", "webhook_allow_remote": False}})
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", default=os.environ.get("CFT_BRIDGE", "http://127.0.0.1:47531"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--with-webhook", action="store_true",
                        help="临时配置本机接收端验证出站 webhook；结束时会关闭/清空 webhook 配置")
    parser.add_argument("--force-webhook", action="store_true",
                        help="已有 webhook 配置时仍覆盖（无法恢复旧 secret，慎用）")
    args = parser.parse_args()
    parsed = urllib.parse.urlsplit(args.bridge)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 47531
    base = "http://%s:%d" % (host, port)
    result = {"bridge": base}

    result["health"] = bool(get_json(base + "/api/health")["ok"])
    doc = get_json(base + "/api/automation/openapi.json?profile=text")
    server = doc["servers"][0]["url"]
    result["openapi"] = {"version": doc["openapi"], "operations": len(doc["paths"]),
                         "server": server}
    if server not in ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port):
        raise RuntimeError("OpenAPI servers 不是回环地址")

    st, alias = post_json(base + "/api/automation/tool/cft_capabilities", {})
    caps = alias["data"]["caps"]
    result["tool_alias"] = {"status": st, "ok": alias["ok"],
                            "openapi": caps["openapi"], "sse": caps["sse"],
                            "webhook": bool((caps.get("webhook") or {}).get("available")),
                            "cli": bool((caps.get("cli") or {}).get("available")),
                            "events": len(caps["events"])}
    try:
        get_json(base + "/api/automation/tool/cft_capabilities")
    except urllib.error.HTTPError as error:
        result["tool_alias_get"] = error.code
        if error.code != 405:
            raise RuntimeError("GET 每工具别名应返回 405，实际 %s" % error.code)
    else:
        raise RuntimeError("GET 每工具别名没有返回 405")

    conn = http.client.HTTPConnection(host, port, timeout=20)
    conn.request("GET", "/api/automation/events/stream?snapshot=1&types=all",
                 headers={"Accept": "text/event-stream"})
    response = conn.getresponse()
    if response.status != 200:
        raise RuntimeError("SSE status=%s" % response.status)
    ready_type, _, ready = read_sse_frame(response)
    snapshot_type, _, snapshot = read_sse_frame(response)
    result["sse"] = {"ready": ready_type, "snapshot": snapshot_type}

    # 只读工具发现调用应由 record_event 实时推回 api.call。
    trigger = threading.Thread(target=lambda: get_json(base + "/api/automation/tools?profile=text"))
    trigger.start()
    _, event_id, event = read_sse_frame(response)
    trigger.join(timeout=5)
    result["sse_live"] = {"type": event["type"], "path": event["data"]["path"], "id": event_id}
    if event["type"] != "api.call":
        raise RuntimeError("SSE 未收到 api.call：%r" % event)
    conn.close()

    replay = http.client.HTTPConnection(host, port, timeout=20)
    replay.request("GET", "/api/automation/events/stream?snapshot=0&types=all",
                   headers={"Accept": "text/event-stream",
                            "Last-Event-ID": str(int(event_id) - 1)})
    replay_response = replay.getresponse()
    if replay_response.status != 200:
        raise RuntimeError("SSE 重连 status=%s" % replay_response.status)
    _, _, _ = read_sse_frame(replay_response)
    first = try_frame(replay_response)
    replay.close()
    if first is None:
        # 刚启动且没有领域事件时，环形缓冲本来就空的——不是断线重放的锅。
        result["sse_replay"] = {"outcome": "buffer-empty"}
    else:
        ftype, fid, _ = first
        if fid is not None and int(fid) < int(event_id):
            if ftype == "api.call":
                raise RuntimeError("api.call 不参与历史重放，却出现在补发里")
            result["sse_replay"] = {"outcome": "replayed", "type": ftype, "id": fid}
        else:
            result["sse_replay"] = {"outcome": "live", "type": ftype, "id": fid}
    result["cli"] = check_cli(base)
    if args.with_webhook:
        result["webhook"] = check_webhook(base, force=args.force_webhook)
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))


if __name__ == "__main__":
    main()
