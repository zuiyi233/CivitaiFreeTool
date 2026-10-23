# -*- coding: utf-8 -*-
"""出站 webhook 传输层：EventHub sink → 有界队列 → 带 HMAC 的 HTTP POST。

边界：
- 只做传输，不产生事件、不改事件 seq、不持久化、不接收入站请求。
- 默认关闭；启用时必须显式给 URL 与 >=16 字符的 secret。
- 默认只允许非公网目标（本机 / 局域网），公网目标必须显式 ``webhook_allow_remote=true``。
- 不跟随重定向、不使用系统代理；投递日志与状态接口不返回 URL 路径/query 或 secret。
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from datetime import datetime

import ai_contract

WEBHOOK_KEYS = (
    "webhook_enabled", "webhook_url", "webhook_events",
    "webhook_secret", "webhook_allow_remote",
)
PROBE_TYPE = "webhook.test"
PROBE_GROUP = "webhook"

_EVENT_TYPES = frozenset(ai_contract.EVENT_TYPES)
_EVENT_GROUPS = frozenset(item["group"] for item in ai_contract.EVENTS)
_DEFAULT_EVENTS = tuple(ai_contract.WEBHOOK_DEFAULT_EVENT_GROUPS)


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class WebhookConfigError(ValueError):
    """配置不合法；消息直接给用户看，不包含 secret。"""


class _BodyTooLarge(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """3xx 一律失败：重定向可以绕过启用时的 URL/SSRF 校验。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener():
    # 本机 webhook 不应被系统代理带到外网；与 MCP 侧车的 Bridge 客户端一致。
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _host_port(parts):
    host = parts.hostname or ""
    if not host:
        return ""
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    try:
        port = parts.port
    except ValueError:
        return ""
    return "%s:%d" % (host, port) if port else host


def redact_url(value):
    """状态/文档里只露 scheme+host+port；路径和 query 可能含平台 token。"""
    text = str(value or "").strip()
    if not text:
        return "***"
    try:
        parts = urllib.parse.urlsplit(text)
        netloc = _host_port(parts)
    except ValueError:
        return "***"
    if not netloc or not parts.scheme:
        return "***"
    path_set = bool((parts.path or "").strip("/")) or bool(parts.query) or bool(parts.fragment)
    return "%s://%s%s" % (parts.scheme, netloc, "/***" if path_set else "")


def url_info(value):
    text = str(value or "").strip()
    if not text:
        return {"scheme": "", "host": "", "port": None, "path_set": False}
    try:
        parts = urllib.parse.urlsplit(text)
        host = parts.hostname or ""
        try:
            port = parts.port
        except ValueError:
            port = None
    except ValueError:
        return {"scheme": "", "host": "", "port": None, "path_set": False}
    return {
        "scheme": parts.scheme.lower(),
        "host": host,
        "port": port,
        "path_set": bool((parts.path or "").strip("/")) or bool(parts.query),
    }


def validate_url(url, *, allow_remote=False):
    """校验 URL；默认拒绝会解析到公网地址的目标（SSRF/数据出境默认关闭）。"""
    text = str(url or "").strip()
    if not text:
        raise WebhookConfigError("webhook_url 不能为空")
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError as exc:
        raise WebhookConfigError("webhook_url 格式不合法: %s" % str(exc)[:120]) from exc
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise WebhookConfigError("webhook_url 只支持 http/https")
    if parts.username is not None or parts.password is not None:
        raise WebhookConfigError("webhook_url 不允许带用户名/密码，请把凭据放进 secret 或接收端")
    if parts.fragment:
        raise WebhookConfigError("webhook_url 不允许 fragment")
    host = parts.hostname or ""
    if not host:
        raise WebhookConfigError("webhook_url 缺少主机名")
    try:
        port = parts.port
    except ValueError as exc:
        raise WebhookConfigError("webhook_url 端口不合法") from exc
    if port is not None and not (1 <= int(port) <= 65535):
        raise WebhookConfigError("webhook_url 端口必须在 1-65535")
    if not allow_remote:
        try:
            infos = socket.getaddrinfo(host, port or (443 if scheme == "https" else 80),
                                       type=socket.SOCK_STREAM)
        except OSError as exc:
            raise WebhookConfigError("无法解析 webhook 主机 %s: %s" % (host, str(exc)[:120])) from exc
        if not infos:
            raise WebhookConfigError("无法解析 webhook 主机 %s" % host)
        for info in infos:
            addr = str(info[4][0]).split("%", 1)[0]
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError as exc:
                raise WebhookConfigError("webhook 主机解析出无法识别的地址: %s" % addr) from exc
            if ip.is_global:
                raise WebhookConfigError(
                    "默认只允许非公网 webhook 目标；%s 解析到公网地址 %s。"
                    "若确需公网投递，请显式设置 webhook_allow_remote=true" % (host, addr))
    return text


def _normalize_event_names(raw, *, validate_names):
    if raw is None:
        raw = []
    if isinstance(raw, str):
        raise WebhookConfigError("webhook_events 必须是数组，例如 [\"task\",\"ui\"]")
    if not isinstance(raw, (list, tuple)):
        raise WebhookConfigError("webhook_events 必须是数组")
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, str):
            raise WebhookConfigError("webhook_events 的每一项必须是事件名/组名/\"all\"")
        name = item.strip()
        if not name or name in seen:
            continue
        if validate_names and name != "all" and name not in _EVENT_TYPES and name not in _EVENT_GROUPS:
            raise WebhookConfigError(
                "未知 webhook 事件名 %s；可用事件组: %s；也可用事件类型或 all"
                % (name, ", ".join(sorted(_EVENT_GROUPS))))
        seen.add(name)
        out.append(name)
    return tuple(out) if out else _DEFAULT_EVENTS


def normalize_config(cfg):
    """把 user_config 里的 webhook_* 变成投递用的规范化配置；不合法就抛错。"""
    cfg = cfg or {}
    enabled = cfg.get("webhook_enabled", False)
    if not isinstance(enabled, bool):
        raise WebhookConfigError("webhook_enabled 必须是布尔值")
    allow_remote = cfg.get("webhook_allow_remote", False)
    if not isinstance(allow_remote, bool):
        raise WebhookConfigError("webhook_allow_remote 必须是布尔值")
    url = cfg.get("webhook_url", "")
    if url is None:
        url = ""
    if not isinstance(url, str):
        raise WebhookConfigError("webhook_url 必须是字符串")
    url = url.strip()
    secret = cfg.get("webhook_secret", "")
    if secret is None:
        secret = ""
    if not isinstance(secret, str):
        raise WebhookConfigError("webhook_secret 必须是字符串")
    events = _normalize_event_names(cfg.get("webhook_events", []),
                                    validate_names=bool(enabled))
    if enabled:
        if not url:
            raise WebhookConfigError("启用 webhook 前必须设置 webhook_url")
        if len(secret) < int(ai_contract.WEBHOOK_LIMITS["min_secret_length"]):
            raise WebhookConfigError(
                "启用 webhook 前必须设置 webhook_secret（至少 %d 字符）"
                % ai_contract.WEBHOOK_LIMITS["min_secret_length"])
        validate_url(url, allow_remote=allow_remote)
    return {
        "enabled": enabled,
        "url": url,
        "secret": secret,
        "events": events,
        "event_set": frozenset(events),
        "allow_remote": allow_remote,
    }


def validate_patch(patch, current):
    """只在校验补丁碰到 webhook_* 时执行；返回规范化后的合并配置或抛错。"""
    patch = patch or {}
    if not any(key in patch for key in WEBHOOK_KEYS):
        return None
    current = current or {}
    merged = {key: patch[key] if key in patch else current.get(key) for key in WEBHOOK_KEYS}
    return normalize_config(merged)


def _safe_error(exc, url="", secret=""):
    text = "%s: %s" % (type(exc).__name__, exc)
    if url:
        text = text.replace(url, "[webhook-url]")
    if secret:
        text = text.replace(secret, "[webhook-secret]")
    return text[:300]


class WebhookDispatcher:
    def __init__(self, *, limits=None, opener=None, clock=None, wall_clock=None):
        self.limits = dict(ai_contract.WEBHOOK_LIMITS)
        if limits:
            self.limits.update(limits)
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        self._opener = opener or _build_opener()
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._queue = []
        self._in_flight = None
        self._thread = None
        self._stopping = False
        self._drain_until = None
        self._closed = False
        self._config = None
        self._config_error = ""
        self._hub = None
        self._sink_attached = False
        self._log = deque(maxlen=max(1, int(self.limits["log_size"])))
        self._order = 0
        self._stats = {
            "accepted": 0, "sent": 0, "failed": 0, "dropped": 0,
            "retried": 0, "last_error": "", "last_success_at": "",
        }

    # ---- 配置与拓扑 ----
    def apply_config(self, cfg, hub=None):
        try:
            conf = normalize_config(cfg)
        except WebhookConfigError as exc:
            with self._cv:
                self._config_error = str(exc)
                self._config = None
                self._closed = True
                self._stopping = True
                self._drain_until = self._clock()
                self._detach_locked()
                self._drop_queued_locked("配置无效，已停止 webhook")
                self._cv.notify_all()
            return False

        with self._cv:
            changed = bool(self._config) and (
                self._config["url"] != conf["url"] or self._config["secret"] != conf["secret"])
            self._config = conf
            self._config_error = ""
            self._closed = False
            if changed:
                self._drop_queued_locked("webhook URL/secret 已变更，旧签名队列作废")
            if conf["enabled"]:
                self._stopping = False
                self._drain_until = None
                self._ensure_worker_locked()
                if hub is not None:
                    self._attach_locked(hub)
            else:
                self._detach_locked()
                self._stopping = True
                self._drain_until = self._clock()
                self._drop_queued_locked("webhook 已关闭")
            self._cv.notify_all()
        return True

    def _ensure_worker_locked(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, daemon=True,
                                            name="cft-webhook")
            self._thread.start()

    def _attach_locked(self, hub):
        if hub is self._hub and self._sink_attached:
            return
        self._detach_locked()
        self._hub = hub
        try:
            hub.add_sink(self.offer)
            self._sink_attached = True
        except Exception:
            self._sink_attached = False

    def _detach_locked(self):
        hub = self._hub
        if hub is not None:
            try:
                hub.remove_sink(self.offer)
            except Exception:
                pass
        self._hub = None
        self._sink_attached = False

    # ---- 入队 ----
    def _wants_locked(self, conf, event):
        names = conf["event_set"]
        if "all" in names:
            return True
        return event.get("type") in names or event.get("group") in names

    def _make_delivery(self, event, conf, *, synthetic=False):
        body = json.dumps(event, ensure_ascii=False, separators=(",", ":"),
                          default=str).encode("utf-8")
        if len(body) > int(self.limits["max_body_bytes"]):
            raise _BodyTooLarge("body %d bytes > %d" % (len(body), self.limits["max_body_bytes"]))
        return {
            "id": uuid.uuid4().hex,
            "event": str(event.get("type") or ""),
            "group": str(event.get("group") or ""),
            "seq": int(event.get("seq") or 0),
            "instance_id": str(event.get("instance_id") or ""),
            "body": body,
            "url": conf["url"],
            "secret": conf["secret"],
            "synthetic": bool(synthetic),
            "attempts": 0,
            "due": self._clock(),
            "order": 0,
            "done": threading.Event(),
            "row": None,
        }

    def _reserve_locked(self, delivery, conf):
        self._order += 1
        delivery["order"] = self._order
        row = {
            "id": delivery["id"], "event": delivery["event"], "seq": delivery["seq"],
            "synthetic": delivery["synthetic"], "status": "queued", "attempts": 0,
            "http_status": None, "ms": None, "at": _now_iso(), "error": "",
        }
        delivery["row"] = row
        self._log.append(row)
        self._queue.append(delivery)
        self._stats["accepted"] += 1

    def offer(self, event):
        """EventHub sink：必须快速返回，任何异常都不能影响事件总线。"""
        try:
            with self._cv:
                conf = self._config
                if (self._closed or not conf or not conf["enabled"]
                        or not self._wants_locked(conf, event)):
                    return False
            delivery = self._make_delivery(event, conf)
            with self._cv:
                current = self._config
                if (self._closed or not current or not current["enabled"]
                        or current["url"] != conf["url"]
                        or current["secret"] != conf["secret"]
                        or not self._wants_locked(current, event)):
                    return False
                if len(self._queue) >= int(self.limits["queue_size"]):
                    self._stats["dropped"] += 1
                    self._stats["last_error"] = "webhook 队列已满，丢弃 %s" % delivery["event"]
                    return False
                self._reserve_locked(delivery, conf)
                self._cv.notify_all()
            return True
        except _BodyTooLarge as exc:
            with self._cv:
                self._stats["dropped"] += 1
                self._stats["last_error"] = "webhook body 过大: %s" % str(exc)[:160]
            return False
        except Exception:
            return False

    # ---- 投递 ----
    def _take_due_locked(self):
        now = self._clock()
        due = [d for d in self._queue if d["due"] <= now]
        if not due:
            return None
        delivery = min(due, key=lambda d: (d["due"], d["order"]))
        self._queue.remove(delivery)
        return delivery

    def _wait_timeout_locked(self):
        now = self._clock()
        waits = []
        if self._queue:
            waits.append(min(d["due"] for d in self._queue) - now)
        if self._stopping and self._drain_until is not None:
            waits.append(self._drain_until - now)
        return max(0.0, min(waits)) if waits else None

    def _run(self):
        while True:
            item = None
            with self._cv:
                while True:
                    if self._stopping:
                        if ((self._drain_until is not None and self._clock() >= self._drain_until)
                                or (not self._queue and self._in_flight is None)):
                            self._thread = None
                            return
                    if self._in_flight is None:
                        item = self._take_due_locked()
                        if item is not None:
                            self._in_flight = item
                            break
                    self._cv.wait(self._wait_timeout_locked())
            if item is not None:
                try:
                    self._deliver(item)
                finally:
                    with self._cv:
                        self._in_flight = None
                        self._cv.notify_all()

    def _deliver(self, delivery):
        started = self._clock()
        delivery["attempts"] += 1
        try:
            result = self._post(delivery)
        except Exception as exc:
            result = {"ok": False, "retryable": True,
                      "error": _safe_error(exc, delivery.get("url"), delivery.get("secret"))}
        ms = round(max(0.0, self._clock() - started) * 1000.0, 1)
        with self._cv:
            row = delivery["row"]
            row["attempts"] = delivery["attempts"]
            row["ms"] = ms
            row["http_status"] = result.get("http_status")
            row["error"] = result.get("error") or ""
            if result.get("ok"):
                row["status"] = "sent"
                self._stats["sent"] += 1
                self._stats["last_success_at"] = _now_iso()
                delivery["done"].set()
                return
            retryable = bool(result.get("retryable"))
            attempts = delivery["attempts"]
            max_attempts = max(1, int(self.limits["max_attempts"]))
            if retryable and attempts < max_attempts:
                backoff = list(self.limits.get("backoff_s") or [])
                delay = float(backoff[min(attempts - 1, len(backoff) - 1)]) if backoff else 0.0
                delivery["due"] = self._clock() + max(0.0, delay)
                row["status"] = "retrying"
                self._queue.append(delivery)
                self._stats["retried"] += 1
                self._cv.notify_all()
                return
            row["status"] = "failed"
            self._stats["failed"] += 1
            self._stats["last_error"] = row["error"] or "webhook 投递失败"
            delivery["done"].set()

    def _post(self, delivery):
        body = delivery["body"]
        url = delivery["url"]
        secret = delivery["secret"]
        ts = str(int(self._wall_clock()))
        signature = hmac.new(secret.encode("utf-8"),
                             ("%s." % ts).encode("utf-8") + body,
                             hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "CivitaiFreeTool-Webhook/1.0",
            "X-CFT-Delivery": delivery["id"],
            "X-CFT-Event": delivery["event"],
            "X-CFT-Group": delivery["group"],
            "X-CFT-Seq": str(delivery["seq"]),
            "X-CFT-Instance": delivery["instance_id"],
            "X-CFT-Timestamp": ts,
            "X-CFT-Signature": "sha256=" + signature,
        }
        if delivery.get("synthetic"):
            headers["X-CFT-Synthetic"] = "1"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        timeout = float(self.limits["timeout_s"])
        max_read = int(self.limits["max_response_bytes"])
        try:
            with self._opener.open(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 0) or response.getcode())
                try:
                    response.read(max_read)
                except Exception:
                    pass
            if 200 <= status < 300:
                return {"ok": True, "http_status": status}
            return {"ok": False, "http_status": status,
                    "retryable": status in (408, 425, 429) or status >= 500,
                    "error": "HTTP %d" % status}
        except urllib.error.HTTPError as exc:
            try:
                exc.read(max_read)
            except Exception:
                pass
            status = int(getattr(exc, "code", 0) or 0)
            return {"ok": False, "http_status": status,
                    "retryable": status in (408, 425, 429) or status >= 500,
                    "error": "HTTP %d" % status}
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            return {"ok": False, "retryable": True,
                    "error": _safe_error(reason, url, secret)}
        except (TimeoutError, socket.timeout) as exc:
            return {"ok": False, "retryable": True,
                    "error": _safe_error(exc, url, secret)}
        except OSError as exc:
            return {"ok": False, "retryable": True,
                    "error": _safe_error(exc, url, secret)}

    # ---- 观测 / 测试 ----
    def status(self):
        with self._cv:
            conf = self._config
            limited = list(self._log)[-50:]
            deliveries = [dict(row) for row in reversed(limited)]
            stats = dict(self._stats)
            stats["pending"] = len(self._queue)
            stats["in_flight"] = 1 if self._in_flight is not None else 0
            return {
                "ok": True, "transport": "webhook",
                "enabled": bool(conf and conf["enabled"]),
                "configured": bool(conf and conf["enabled"] and conf["url"] and conf["secret"]),
                "url": url_info(conf["url"]) if conf else url_info(""),
                "events": list(conf["events"]) if conf else list(_DEFAULT_EVENTS),
                "allow_remote": bool(conf and conf["allow_remote"]),
                "secret_set": bool(conf and conf["secret"]),
                "attached": bool(self._sink_attached),
                "stats": stats,
                "signature": {"algorithm": "HMAC-SHA256", "header": "X-CFT-Signature",
                              "signed": "<timestamp>.<raw body>"},
                "limits": dict(self.limits),
                "deliveries": deliveries,
                "config_error": self._config_error,
            }

    def test_delivery(self, timeout=None):
        with self._cv:
            conf = self._config
            if not conf or not conf["enabled"]:
                raise WebhookConfigError("webhook 未启用；先设置 webhook_enabled=true")
            if not conf["url"] or not conf["secret"]:
                raise WebhookConfigError("webhook 缺少 URL 或 secret")
        probe = {
            "seq": 0,
            "instance_id": str(getattr(self._hub, "instance_id", "") or ""),
            "type": PROBE_TYPE,
            "group": PROBE_GROUP,
            "at": _now_iso(),
            "data": {"probe": True, "pid": os.getpid()},
        }
        delivery = self._make_delivery(probe, conf, synthetic=True)
        with self._cv:
            if len(self._queue) >= int(self.limits["queue_size"]):
                raise WebhookConfigError("webhook 队列已满；稍后重试")
            self._reserve_locked(delivery, conf)
            self._cv.notify_all()
        wait_s = float(timeout or self.limits["test_timeout_s"])
        delivery["done"].wait(max(0.0, wait_s))
        with self._cv:
            row = dict(delivery["row"])
        return {
            "ok": row["status"] == "sent",
            "delivery": delivery["id"],
            "event": PROBE_TYPE,
            "status": row["status"],
            "attempts": row["attempts"],
            "http_status": row["http_status"],
            "ms": row["ms"],
            "error": row["error"],
        }

    def clear_log(self):
        with self._cv:
            count = len(self._log)
            self._log.clear()
            return count

    def _drop_queued_locked(self, reason):
        count = len(self._queue)
        if not count:
            return 0
        for delivery in self._queue:
            row = delivery.get("row")
            if isinstance(row, dict):
                row["status"] = "dropped"
                row["error"] = reason[:300]
            delivery["done"].set()
        self._queue.clear()
        self._stats["dropped"] += count
        self._stats["last_error"] = reason[:300]
        return count

    def stop(self, flush_s=1.0):
        with self._cv:
            self._closed = True
            self._stopping = True
            self._drain_until = self._clock() + max(0.0, float(flush_s or 0))
            self._detach_locked()
            self._cv.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.1, float(flush_s or 0) + 1.0))
        with self._cv:
            self._drop_queued_locked("服务停止，未投递事件丢弃")
            if thread is None or not thread.is_alive():
                self._thread = None


# ---- 模块级单例：桥启动/停止与配置保存都走这里 ----
_dispatcher = None
_dispatcher_lock = threading.Lock()


def get_dispatcher():
    global _dispatcher
    with _dispatcher_lock:
        if _dispatcher is None:
            _dispatcher = WebhookDispatcher()
        return _dispatcher


def refresh_from_config(cfg, hub=None):
    dispatcher = get_dispatcher()
    dispatcher.apply_config(cfg or {}, hub)
    return dispatcher


def status():
    return get_dispatcher().status()


def offer(event):
    return get_dispatcher().offer(event)


def shutdown(flush_s=1.0):
    global _dispatcher
    with _dispatcher_lock:
        dispatcher, _dispatcher = _dispatcher, None
    if dispatcher is not None:
        dispatcher.stop(flush_s=flush_s)
