#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CivitaiFreeTool AI 调用面的命令行客户端（stdlib only）。

它不是第二个执行层：所有工具调用都走本机桥的
``POST /api/automation/tool/{name}``，工具表/事件目录/退出码来自 ``ai_contract``。
只连接回环地址；不自动启动桌面端，不复制下载/分流/校验逻辑。
"""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import ai_contract as C

APP_NAME = "CivitaiFreeToolCLI"
DEFAULT_BRIDGE = C.DEFAULT_BRIDGE
DEFAULT_TIMEOUT = 15.0
TOOL_TIMEOUT = 60.0
SSE_TIMEOUT = 60.0
MAX_SSE_IDLE = 1800.0
_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
_MIME_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/gif": ".gif",
}


class UsageError(ValueError):
    pass


class BridgeError(Exception):
    def __init__(self, code, msg, hint="", status=0, payload=None):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.hint = hint
        self.status = int(status or 0)
        self.payload = payload or {}

    def envelope(self):
        out = dict(self.payload)
        out.update({"ok": False, "code": self.code, "msg": self.msg})
        if self.hint:
            out.setdefault("hint", self.hint)
        return out


def _bridge_base(value):
    text = str(value or DEFAULT_BRIDGE).strip()
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError as exc:
        raise UsageError("--bridge 格式不合法: %s" % str(exc)[:120]) from exc
    if parts.scheme not in ("http", "https"):
        raise UsageError("--bridge 只支持 http/https")
    if parts.username is not None or parts.password is not None:
        raise UsageError("--bridge 不允许带用户名/密码")
    host = (parts.hostname or "").lower()
    if host not in _LOOPBACK_HOSTS:
        raise UsageError("CLI 只允许连接回环地址（127.0.0.1 / localhost / ::1）")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise UsageError("--bridge 只接受 scheme://host:port，不要带路径/query")
    try:
        port = parts.port
    except ValueError as exc:
        raise UsageError("--bridge 端口不合法") from exc
    if host == "localhost":
        host = "127.0.0.1"
    host_text = "[%s]" % host if ":" in host else host
    default_port = 443 if parts.scheme == "https" else 80
    return "%s://%s:%d" % (parts.scheme, host_text, int(port or default_port))


def _split_base(base):
    parts = urllib.parse.urlsplit(base)
    return parts.scheme, parts.hostname, int(parts.port or (443 if parts.scheme == "https" else 80))


class Bridge:
    def __init__(self, base):
        self.base = _bridge_base(base)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _port_open(self):
        """区分“端口没监听”和“进程在但 HTTP 卡住”：Windows 上关闭端口也可能表现为超时。"""
        try:
            _, host, port = _split_base(self.base)
            with socket.create_connection((host, port), timeout=0.25):
                return True
        except OSError:
            return False

    def _timeout_error(self, exc):
        if self._port_open():
            return BridgeError("timeout", "请求超时: %s" % str(exc)[:160],
                               "确认桌面端没有卡住；工具执行可加 --timeout")
        return BridgeError(
            "app_not_running",
            "连不上 CivitaiFreeTool 桥 %s: %s" % (self.base, str(exc)[:160]),
            "先打开桌面端；源码运行时用 python main_web.py 或对应启动方式",
        )

    def request(self, method, path, body=None, timeout=DEFAULT_TIMEOUT):
        url = self.base + path
        data = None
        headers = {"Accept": "application/json",
                   "User-Agent": "%s/%s" % (APP_NAME, C.CLI_VERSION)}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=str(method).upper())
        try:
            with self._opener.open(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 0) or response.getcode())
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                raw = exc.read()
            except Exception:
                raw = b""
        except (TimeoutError, socket.timeout) as exc:
            raise self._timeout_error(exc) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise self._timeout_error(reason) from exc
            raise BridgeError(
                "app_not_running",
                "连不上 CivitaiFreeTool 桥 %s: %s" % (self.base, str(reason)[:160]),
                "先打开桌面端；源码运行时用 python main_web.py 或对应启动方式",
            ) from exc
        except OSError as exc:
            raise BridgeError(
                "app_not_running",
                "连不上 CivitaiFreeTool 桥 %s: %s" % (self.base, str(exc)[:160]),
                "先打开桌面端",
            ) from exc
        text = raw.decode("utf-8", "replace")
        try:
            payload = json.loads(text) if text.strip() else {}
        except ValueError:
            payload = {"ok": False, "code": "internal_error",
                       "msg": "桥返回非 JSON（HTTP %d）" % status,
                       "raw": text[:300]}
        if not isinstance(payload, dict):
            payload = {"ok": True, "result": payload}
        return status, payload


def _exit_code(payload, status=0, default=1):
    code = str((payload or {}).get("code") or "")
    if not code:
        code = C.error_code_for_status(status, payload if isinstance(payload, dict) else {})
    return int(C.CLI_EXIT_CODES.get(code, default))


def _print_json(value, compact=False):
    print(json.dumps(value, ensure_ascii=False, indent=None if compact else 2,
                     separators=(",", ":") if compact else None))


def _emit(args, payload):
    if getattr(args, "quiet", False):
        return
    _print_json(payload, compact=bool(getattr(args, "json", False)))


def _error(args, payload, status=0):
    if getattr(args, "json", False):
        sys.stderr.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    else:
        msg = payload.get("msg") or "调用失败"
        code = payload.get("code") or ""
        hint = payload.get("hint") or ""
        line = "错误: %s" % msg
        if code:
            line += " [%s]" % code
        if hint:
            line += " | %s" % hint
        sys.stderr.write(line + "\n")
    return _exit_code(payload, status)


def _timeout(args, default):
    value = getattr(args, "timeout", None)
    return float(value) if value else float(default)


def _parse_value(raw):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _collect_arguments(args):
    base = {}
    raw_args = getattr(args, "args", None)
    if raw_args is not None:
        text = sys.stdin.read() if raw_args == "-" else raw_args
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise UsageError("--args 不是合法 JSON: %s" % str(exc)[:120]) from exc
        if not isinstance(parsed, dict):
            raise UsageError("--args 必须是 JSON 对象")
        base.update(parsed)
    repeated = {}
    for item in getattr(args, "arg", None) or []:
        if "=" not in item:
            raise UsageError("--arg 需要 key=value，例如 --arg limit=5")
        key, raw = item.split("=", 1)
        key = key.strip()
        if not key:
            raise UsageError("--arg 的 key 不能为空")
        value = _parse_value(raw)
        if key in repeated:
            if not isinstance(repeated[key], list):
                repeated[key] = [repeated[key]]
            repeated[key].append(value)
        else:
            repeated[key] = value
    base.update(repeated)
    return base


def _safe_label(value):
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._")
    return text[:40] or "image"


def _save_content(payload, directory):
    """把图片内容块写到目录；返回 (新 payload, saved[])；不改工具 ok。"""
    if not isinstance(payload, dict):
        return payload, []
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        return payload, [{"error": "创建目录失败: %s" % str(exc)[:160]}]
    out = dict(payload)
    content = []
    saved = []
    for index, block in enumerate(payload.get("content") or []):
        if not isinstance(block, dict):
            content.append(block)
            continue
        clean = dict(block)
        b64 = ""
        mime = "image/jpeg"
        target = ""
        if block.get("type") == "image":
            b64 = block.get("b64") or block.get("data") or ""
            mime = block.get("mime") or block.get("mimeType") or mime
            if not b64 and isinstance(block.get("source"), dict):
                b64 = block["source"].get("data") or ""
                mime = block["source"].get("media_type") or mime
            target = "b64" if block.get("b64") is not None else (
                "data" if block.get("data") is not None else "")
        elif block.get("type") == "image_url":
            image_url = block.get("image_url") if isinstance(block.get("image_url"), dict) else {}
            url = str(image_url.get("url") or "")
            if url.startswith("data:image/") and ";base64," in url:
                head, b64 = url.split(",", 1)
                mime = head[5:].split(";", 1)[0] or mime
                target = "image_url"
        if not b64:
            content.append(clean)
            continue
        ext = _MIME_EXT.get(str(mime).lower())
        if not ext:
            ext = ".bin"
        label = _safe_label(block.get("label") or ("#%d" % (index + 1)))
        path = os.path.join(directory, "%04d-%s%s" % (index + 1, label, ext))
        try:
            raw = base64.b64decode("".join(str(b64).split()), validate=True)
            with open(path, "wb") as handle:
                handle.write(raw)
        except (OSError, ValueError, base64.binascii.Error) as exc:
            saved.append({"index": index + 1, "error": str(exc)[:160]})
            content.append(clean)
            continue
        saved.append({"index": index + 1, "path": os.path.abspath(path),
                      "mime": mime, "bytes": len(raw), "label": label})
        if target == "b64":
            clean["b64"] = "<saved>"
        elif target == "data":
            clean["data"] = "<saved>"
        elif target == "image_url":
            clean["image_url"] = {"url": "<saved>"}
        elif isinstance(clean.get("source"), dict):
            clean["source"] = dict(clean["source"], data="<saved>")
        content.append(clean)
    out["content"] = content
    out["saved_content"] = saved
    return out, saved


# ---- 子命令 ----
def cmd_health(args):
    status, payload = Bridge(args.bridge).request("GET", "/api/health",
                                                 timeout=_timeout(args, 5.0))
    _emit(args, payload)
    if status == 200 and payload.get("ok") is not False:
        return 0
    return _error(args, payload, status)


def cmd_capabilities(args):
    query = "?include_tools=1" if getattr(args, "tools", False) else ""
    status, payload = Bridge(args.bridge).request("GET", "/api/automation/ai" + query,
                                                 timeout=_timeout(args, DEFAULT_TIMEOUT))
    _emit(args, payload)
    if status == 200 and payload.get("ok") is not False:
        return 0
    return _error(args, payload, status)


def cmd_tools(args):
    profile = args.profile
    if profile not in C.PROFILES:
        raise UsageError("--profile 必须是 %s 之一" % "|".join(C.PROFILES))
    fmt = args.format
    if fmt not in ("mcp", "openai", "anthropic"):
        raise UsageError("--format 必须是 mcp|openai|anthropic")
    status, payload = Bridge(args.bridge).request(
        "GET", "/api/automation/tools?profile=%s&format=%s" % (profile, fmt),
        timeout=_timeout(args, DEFAULT_TIMEOUT))
    if status != 200 or payload.get("ok") is False:
        _error(args, payload, status)
        return _exit_code(payload, status)
    if getattr(args, "json", False):
        _emit(args, payload)
    elif not getattr(args, "quiet", False):
        for tool in payload.get("tools") or []:
            name = tool.get("name") or (tool.get("function") or {}).get("name") or ""
            title = tool.get("title") or (tool.get("function") or {}).get("description") or ""
            print("%s\t%s" % (name, title))
    return 0


def cmd_guide(args):
    profile = args.profile
    if profile not in ("text", "vision"):
        raise UsageError("--profile 必须是 text|vision")
    markdown = bool(getattr(args, "markdown", False))
    suffix = "&format=markdown" if markdown else ""
    status, payload = Bridge(args.bridge).request(
        "GET", "/api/automation/guide?profile=%s%s" % (profile, suffix),
        timeout=_timeout(args, DEFAULT_TIMEOUT))
    if status != 200 or payload.get("ok") is False:
        _error(args, payload, status)
        return _exit_code(payload, status)
    if markdown and not getattr(args, "json", False):
        if not getattr(args, "quiet", False):
            print(payload.get("markdown") or "")
    else:
        _emit(args, payload)
    return 0


def cmd_openapi(args):
    profile = args.profile
    if profile not in C.PROFILES:
        raise UsageError("--profile 必须是 %s 之一" % "|".join(C.PROFILES))
    status, payload = Bridge(args.bridge).request(
        "GET", "/api/automation/openapi.json?profile=%s" % profile,
        timeout=_timeout(args, DEFAULT_TIMEOUT))
    if status != 200 or payload.get("ok") is False:
        _error(args, payload, status)
        return _exit_code(payload, status)
    _emit(args, payload)
    return 0


def cmd_call(args):
    name = str(args.tool or "").strip()
    if not name or "/" in name:
        raise UsageError("工具名不能为空且不能含 /")
    arguments = _collect_arguments(args)
    query = ""
    if getattr(args, "format", None):
        query = "?content_format=" + urllib.parse.quote(args.format)
    status, payload = Bridge(args.bridge).request(
        "POST", "/api/automation/tool/%s%s" % (urllib.parse.quote(name, safe=""), query),
        body=arguments, timeout=_timeout(args, TOOL_TIMEOUT))
    if getattr(args, "save_content", None):
        payload, _saved = _save_content(payload, args.save_content)
    _emit(args, payload)
    if 200 <= status < 300 and payload.get("ok") is not False:
        return 0
    return _error(args, payload, status)


def _read_batch(args):
    path = getattr(args, "file", None)
    if path in (None, "-"):
        text = sys.stdin.read()
    else:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            raise UsageError("读取 batch 文件失败: %s" % str(exc)[:160]) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise UsageError("batch 文件不是合法 JSON: %s" % str(exc)[:120]) from exc
    if isinstance(data, list):
        data = {"calls": data}
    if not isinstance(data, dict) or not isinstance(data.get("calls"), list):
        raise UsageError('batch 需要 {"calls":[...]} 或 [...]')
    data["stop_on_error"] = bool(getattr(args, "stop_on_error", False))
    return data


def cmd_batch(args):
    body = _read_batch(args)
    status, payload = Bridge(args.bridge).request(
        "POST", "/api/automation/batch", body=body, timeout=_timeout(args, TOOL_TIMEOUT))
    _emit(args, payload)
    if status != 200:
        return _error(args, payload, status)
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    for result in results:
        if not isinstance(result, dict):
            continue
        if result.get("ok") is False:
            nested = result.get("result") if isinstance(result.get("result"), dict) else {}
            code = (nested.get("code") or result.get("code")
                    or C.error_code_for_status(int(result.get("http_status") or 0), nested))
            return int(C.CLI_EXIT_CODES.get(code, 1))
    return 0


def _iter_sse(response):
    event_type = event_id = None
    data_lines = []
    while True:
        raw = response.readline()
        if not raw:
            if data_lines:
                yield event_type, event_id, _decode_sse(data_lines)
            return
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data_lines:
                yield event_type, event_id, _decode_sse(data_lines)
            event_type = event_id = None
            data_lines = []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event_type = line.split(":", 1)[1].strip()
        elif line.startswith("id:"):
            event_id = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())


def _decode_sse(lines):
    text = "\n".join(lines)
    try:
        payload = json.loads(text)
    except ValueError:
        payload = {"raw": text}
    if not isinstance(payload, dict):
        payload = {"data": payload}
    return payload


def _print_event(args, event_type, event_id, payload):
    if getattr(args, "quiet", False):
        return
    if getattr(args, "json", False):
        if event_type and not payload.get("type"):
            payload = dict(payload, type=event_type)
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return
    kind = payload.get("type") or event_type or "?"
    seq = payload.get("seq") or event_id or "-"
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    print("#%s %s %s" % (seq, kind, json.dumps(data, ensure_ascii=False,
                                               separators=(",", ":"))))


def _open_sse(args, base, last_id, snapshot):
    scheme, host, port = _split_base(base)
    conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    timeout = float(getattr(args, "idle_timeout", None) or _timeout(args, SSE_TIMEOUT))
    timeout = max(1.0, min(MAX_SSE_IDLE, timeout))
    conn = conn_cls(host, port, timeout=timeout)
    query = {
        "types": args.types or "all",
        "snapshot": "1" if snapshot else "0",
    }
    if last_id is not None:
        query["since"] = str(last_id)
    elif getattr(args, "since", None) is not None:
        query["since"] = str(args.since)
    path = "/api/automation/events/stream?" + urllib.parse.urlencode(query)
    headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache",
               "User-Agent": "%s/%s" % (APP_NAME, C.CLI_VERSION)}
    if last_id is not None:
        headers["Last-Event-ID"] = str(last_id)
    conn.request("GET", path, headers=headers)
    response = conn.getresponse()
    if response.status != 200:
        raw = response.read()
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            payload = {"ok": False, "code": "internal_error",
                       "msg": "SSE 返回 HTTP %d" % response.status}
        conn.close()
        raise BridgeError(payload.get("code") or C.error_code_for_status(response.status, payload),
                          payload.get("msg") or "SSE 连接失败",
                          payload.get("hint") or "", response.status, payload)
    return conn, response


def cmd_events(args):
    base = _bridge_base(args.bridge)
    if args.since is not None and args.since < 0:
        raise UsageError("--since 必须是非负整数")
    if args.max is not None and args.max < 1:
        raise UsageError("--max 必须 >= 1")
    if args.idle_timeout is not None and args.idle_timeout <= 0:
        raise UsageError("--idle-timeout 必须 > 0")
    last_id = None
    snapshot = not bool(getattr(args, "no_snapshot", False))
    count = 0
    connected_once = False
    initial_failures = 0
    try:
        while True:
            try:
                conn, response = _open_sse(args, base, last_id, snapshot)
                connected_once = True
            except BridgeError as exc:
                # 4xx（未知事件名/请求非法）重试也不会变好；只有总线未就绪/连接层错误才重连。
                if exc.status and exc.status != 503:
                    return _error(args, exc.envelope(), exc.status)
                if not connected_once:
                    initial_failures += 1
                    if initial_failures >= 3:
                        return _error(args, exc.envelope(), exc.status)
                if not getattr(args, "no_reconnect", False):
                    time.sleep(1.0)
                    snapshot = False
                    continue
                return _error(args, exc.envelope(), exc.status)
            except (OSError, http.client.HTTPException, socket.timeout) as exc:
                if not connected_once:
                    initial_failures += 1
                    if initial_failures >= 3:
                        return _error(args, {
                            "ok": False, "code": "app_not_running",
                            "msg": "连不上 SSE 事件流 %s: %s" % (base, str(exc)[:160]),
                            "hint": "先打开桌面端；源码运行用 python main_web.py --browser",
                        })
                if getattr(args, "no_reconnect", False):
                    return _error(args, {"ok": False, "code": "app_not_running",
                                         "msg": "SSE 连接失败: %s" % str(exc)[:160]})
                time.sleep(1.0)
                snapshot = False
                continue
            try:
                for event_type, event_id, payload in _iter_sse(response):
                    _print_event(args, event_type, event_id, payload)
                    seq = payload.get("seq")
                    if seq is not None:
                        last_id = int(seq)
                    elif event_id:
                        try:
                            last_id = int(event_id)
                        except ValueError:
                            pass
                    if payload.get("type") == "stream.reset":
                        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                        if data.get("last_seq") is not None:
                            last_id = int(data["last_seq"])
                    count += 1
                    if args.max is not None and count >= args.max:
                        return 0
            except (TimeoutError, socket.timeout):
                if getattr(args, "idle_timeout", None):
                    return 0
                if getattr(args, "no_reconnect", False):
                    return _error(args, {"ok": False, "code": "timeout",
                                         "msg": "SSE 读取超时"})
            except (OSError, http.client.HTTPException) as exc:
                if getattr(args, "no_reconnect", False):
                    return _error(args, {"ok": False, "code": "app_not_running",
                                         "msg": "SSE 连接中断: %s" % str(exc)[:160]})
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            if getattr(args, "no_reconnect", False):
                return 0
            snapshot = False
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 130


def cmd_doctor(args):
    bridge = Bridge(args.bridge)
    report = {"ok": False, "bridge": bridge.base, "checks": [], "health": None,
              "capabilities": None}
    status, health = bridge.request("GET", "/api/health", timeout=_timeout(args, 5.0))
    report["health"] = health
    if status != 200 or health.get("ok") is False:
        report["checks"].append({"name": "health", "ok": False,
                                 "detail": health.get("msg") or "HTTP %d" % status})
        _emit(args, report)
        return _error(args, health, status)
    report["checks"].append({"name": "health", "ok": True,
                             "detail": health.get("version") or ""})
    status, caps = bridge.request("GET", "/api/automation/ai",
                                 timeout=_timeout(args, DEFAULT_TIMEOUT))
    report["capabilities"] = caps
    if status != 200 or caps.get("ok") is False:
        report["checks"].append({"name": "capabilities", "ok": False,
                                 "detail": caps.get("msg") or "HTTP %d" % status})
        _emit(args, report)
        return _error(args, caps, status)
    caps_caps = caps.get("caps") if isinstance(caps.get("caps"), dict) else {}
    transports = caps_caps.get("transports") if isinstance(caps_caps.get("transports"), dict) else {}
    profiles = caps.get("profiles") if isinstance(caps.get("profiles"), dict) else {}
    webhook_cap = caps_caps.get("webhook") if isinstance(caps_caps.get("webhook"), dict) else {}
    cli_cap = caps_caps.get("cli") if isinstance(caps_caps.get("cli"), dict) else {}
    expected = {key: len(C.tools_for_profile(key)) for key in C.PROFILES}
    counts_ok = all(int((profiles.get(k) or {}).get("count") or 0) == v
                    for k, v in expected.items())
    events_ok = len(caps_caps.get("events") or []) == len(C.EVENTS)
    required = {
        "openapi": bool(caps_caps.get("openapi")) and bool(transports.get("openapi")),
        "sse": bool(caps_caps.get("sse")) and bool(transports.get("sse")),
        "webhook": bool(webhook_cap.get("available")) and bool(transports.get("webhook")),
        "cli": bool(cli_cap.get("available")) and bool(transports.get("cli")),
        "tools %d" % len(C.TOOLS): counts_ok,
        "events %d" % len(C.EVENTS): events_ok,
    }
    for name, ok in required.items():
        report["checks"].append({"name": name, "ok": ok})
    mcp = caps.get("mcp") if isinstance(caps.get("mcp"), dict) else {}
    report["checks"].append({"name": "mcp", "ok": True,
                             "detail": "running" if mcp.get("running") else "not running",
                             "optional": True})
    report["ok"] = all(item["ok"] for item in report["checks"] if not item.get("optional"))
    _emit(args, report)
    if not report["ok"]:
        sys.stderr.write("doctor: 桌面端能力不完整；升级桌面端/CLI 到同一版本后重试\n")
        return 1
    return 0


def _build_parser():
    # 子命令上的同名选项用 SUPPRESS：写在子命令前（全局）或后（局部）都生效，
    # 且不会用子命令默认值覆盖前面已经给出的 --bridge。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--bridge", default=argparse.SUPPRESS,
                        help="本机桥地址（默认 %s；只允许回环）" % DEFAULT_BRIDGE)
    common.add_argument("--timeout", type=float, default=argparse.SUPPRESS,
                        help="HTTP 超时秒数（按子命令有默认值）")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="紧凑 JSON 输出")
    common.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS,
                        help="不输出成功结果，只返回退出码")

    parser = argparse.ArgumentParser(prog=APP_NAME, description=__doc__)
    parser.add_argument("--version", action="store_true", help="打印 CLI/契约版本")
    parser.add_argument("--bridge", default=os.environ.get("CFT_BRIDGE") or DEFAULT_BRIDGE,
                        help="本机桥地址（默认 %s；只允许回环）" % DEFAULT_BRIDGE)
    parser.add_argument("--timeout", type=float, default=None,
                        help="HTTP 超时秒数（按子命令有默认值）")
    parser.add_argument("--json", action="store_true", help="紧凑 JSON 输出")
    parser.add_argument("--quiet", action="store_true", help="不输出成功结果，只返回退出码")
    sub = parser.add_subparsers(dest="command")

    health = sub.add_parser("health", parents=[common], help="探活")
    health.set_defaults(func=cmd_health)

    caps = sub.add_parser("capabilities", parents=[common], help="能力协商")
    caps.add_argument("--tools", action="store_true", help="附带完整工具 schema")
    caps.set_defaults(func=cmd_capabilities)

    tools = sub.add_parser("tools", parents=[common], help="列出契约工具")
    tools.add_argument("--profile", default="text", choices=list(C.PROFILES))
    tools.add_argument("--format", default="mcp", choices=("mcp", "openai", "anthropic"))
    tools.set_defaults(func=cmd_tools)

    guide = sub.add_parser("guide", parents=[common], help="模型 runbook")
    guide.add_argument("--profile", default="text", choices=("text", "vision"))
    guide.add_argument("--markdown", action="store_true")
    guide.set_defaults(func=cmd_guide)

    openapi = sub.add_parser("openapi", parents=[common], help="输出 OpenAPI 3.1 文档")
    openapi.add_argument("--profile", default="text", choices=list(C.PROFILES))
    openapi.set_defaults(func=cmd_openapi)

    call = sub.add_parser("call", parents=[common], help="调用一个契约工具")
    call.add_argument("tool", help="cft_* 工具名")
    call.add_argument("--args", default=None, help="JSON 对象；- 从 stdin 读")
    call.add_argument("--arg", action="append", default=[], metavar="KEY=VALUE",
                      help="可重复；值按 JSON 解析，失败按字符串")
    call.add_argument("--format", default=None,
                      choices=("neutral", "openai", "anthropic", "mcp"))
    call.add_argument("--save-content", default=None, metavar="DIR",
                      help="把图片内容块写到目录，并在输出里用 <saved> 代替 base64")
    call.set_defaults(func=cmd_call)

    batch = sub.add_parser("batch", parents=[common], help="一次执行多条自动化调用")
    batch.add_argument("--file", default="-", help="JSON 文件；- 或省略 = stdin")
    batch.add_argument("--stop-on-error", action="store_true")
    batch.set_defaults(func=cmd_batch)

    events = sub.add_parser("events", parents=[common], help="订阅 SSE 领域事件")
    events.add_argument("--types", default="all", help="逗号分隔的事件名/组名，或 all")
    events.add_argument("--since", type=int, default=None, help="从该 seq 之后开始")
    events.add_argument("--no-snapshot", action="store_true", help="不要求首个状态快照")
    events.add_argument("--max", type=int, default=None, help="收到 N 帧后退出")
    events.add_argument("--idle-timeout", type=float, default=None,
                        help="N 秒无数据就退出 0")
    events.add_argument("--no-reconnect", action="store_true", help="断线不重连")
    events.set_defaults(func=cmd_events)

    doctor = sub.add_parser("doctor", parents=[common], help="一次性预检")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def _utf8_pipes():
    """管道按 UTF-8 输出：Windows 默认用 ANSI 代码页，被 Agent/脚本捕获时中文会乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            pass


def main(argv=None):
    _utf8_pipes()
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if getattr(args, "version", False):
        print("%s %s (contract %s; %d tools / %d events)"
              % (APP_NAME, C.CLI_VERSION, C.API_VERSION, len(C.TOOLS), len(C.EVENTS)))
        return 0
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except UsageError as exc:
        return _error(args, {"ok": False, "code": "usage", "msg": str(exc),
                             "hint": "运行 %s --help 查看用法" % APP_NAME})
    except BridgeError as exc:
        return _error(args, exc.envelope(), exc.status)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
