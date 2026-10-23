#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenAPI + 每工具别名 + SSE 冒烟（只读，不启动下载）。"""
import argparse
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge", default=os.environ.get("CFT_BRIDGE", "http://127.0.0.1:47531"))
    parser.add_argument("--json", action="store_true")
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
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))


if __name__ == "__main__":
    main()
