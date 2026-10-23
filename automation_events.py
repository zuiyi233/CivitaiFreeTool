# -*- coding: utf-8 -*-
"""SSE 领域事件总线：快照差分、环形缓冲、订阅队列与重放。

设计约束：
- 不向 downloader/webui 的任务状态机埋点；只在有订阅者（或刚断开后的宽限期）时，
  每 interval 秒读一次现有状态并做差分。
- 与 /api/automation/events 的调用日志分开：SSE 有自己的 seq，不改变既有 since 语义。
- 所有对外事件类型都必须出现在 ai_contract.EVENTS 中，避免文档与实现漂移。
"""
from __future__ import annotations

import copy
import json
import os
import queue
import threading
import time
import uuid
from collections import deque
from datetime import datetime

import ai_contract

TERMINAL = ("done", "error", "canceled")
DEFAULT_INTERVAL_S = 1.0
DEFAULT_IDLE_STOP_S = 30.0
RING_MAX = 500
QUEUE_MAX = 256
MAX_CLIENTS = 8
HEARTBEAT_S = 15.0


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def now_iso():
    return _now()


def _short(value, limit=500):
    text = str(value if value is not None else "")
    return text[:limit]


def _json_key(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return str(value)


def _task_public(row):
    if not isinstance(row, dict):
        return None
    task_id = row.get("id")
    if task_id is None:
        return None
    progress = row.get("progress")
    try:
        progress = round(float(progress), 1)
    except (TypeError, ValueError):
        progress = None
    out = {
        "id": _short(task_id, 80),
        "filename": _short(row.get("filename"), 240),
        "status": _short(row.get("status"), 40),
        "dest_dir": _short(row.get("dest_dir"), 500),
        "progress": progress,
        "downloaded": row.get("downloaded"),
        "total": row.get("total"),
        "error": _short(row.get("error"), 800),
    }
    if row.get("postprocess") is not None:
        out["postprocess"] = row.get("postprocess")
    if row.get("postprocess_errors"):
        out["postprocess_errors"] = [_short(e, 300) for e in
                                     (row.get("postprocess_errors") or [])[:20]]
    return out


def _choice_public(value):
    if not isinstance(value, dict):
        return None
    files = value.get("files")
    count = len(files) if isinstance(files, list) else value.get("files_count")
    try:
        count = int(count or 0)
    except (TypeError, ValueError):
        count = 0
    return {"id": _short(value.get("id"), 120), "files_count": max(0, count)}


def _scan_public(value):
    if not isinstance(value, dict):
        return {}

    def number(key):
        try:
            return max(0, int(value.get(key) or 0))
        except (TypeError, ValueError, OverflowError):
            return 0

    return {
        "running": bool(value.get("running")),
        "phase": _short(value.get("phase"), 80),
        "done": number("done"),
        "total": number("total"),
        "msg": _short(value.get("msg"), 500),
    }


def _overlays_public(rows):
    out = []
    for row in (rows or [])[:24]:
        if not isinstance(row, dict):
            continue
        out.append({
            "name": _short(row.get("name"), 80),
            "group": _short(row.get("group"), 40),
            "kind": _short(row.get("kind"), 60),
            "title": _short(row.get("title"), 120),
            "blocking": row.get("blocking") is not False,
        })
    return out


def _tasks_map(rows):
    out = {}
    for row in rows or []:
        item = _task_public(row)
        if item:
            out[str(item["id"])] = item
    return out


def _task_changed(prev, curr):
    for key in ("status", "progress", "downloaded", "total", "error", "postprocess",
                "postprocess_errors"):
        if _json_key(prev.get(key)) != _json_key(curr.get(key)):
            return True
    return False


class Subscription:
    def __init__(self, types=None, queue_size=QUEUE_MAX):
        self.types = types
        self.queue = queue.Queue(maxsize=queue_size)
        self.closed = threading.Event()
        self._lock = threading.Lock()
        self._dropped = 0

    def wants(self, event):
        if self.types is None:
            return True
        return event.get("type") in self.types or event.get("group") in self.types

    def offer(self, event):
        if self.closed.is_set() or not self.wants(event):
            return
        while True:
            try:
                self.queue.put_nowait(event)
                return
            except queue.Full:
                dropped = False
                try:
                    self.queue.get_nowait()
                    dropped = True
                except queue.Empty:
                    pass
                if dropped:
                    with self._lock:
                        self._dropped += 1

    def take_dropped(self):
        with self._lock:
            value, self._dropped = self._dropped, 0
        return value

    def close(self, clear=True):
        self.closed.set()
        if clear:
            try:
                while True:
                    self.queue.get_nowait()
            except queue.Empty:
                pass


class EventHub:
    def __init__(self, *, snapshot_provider=None, tasks_provider=None,
                 model_scan_provider=None, library_scan_provider=None,
                 config_provider=None,
                 interval=DEFAULT_INTERVAL_S, idle_stop=DEFAULT_IDLE_STOP_S,
                 max_clients=MAX_CLIENTS, queue_size=QUEUE_MAX, ring_max=RING_MAX):
        limits = getattr(ai_contract, "AGENT_TRANSPORT_LIMITS", {})
        self.instance_id = uuid.uuid4().hex[:8]
        self.interval = max(0.05, float(interval or limits.get("sse_sample_interval_s", DEFAULT_INTERVAL_S)))
        self.idle_stop = max(self.interval, float(idle_stop or limits.get("sse_idle_stop_s", DEFAULT_IDLE_STOP_S)))
        self.max_clients = int(max_clients or limits.get("sse_max_clients", MAX_CLIENTS))
        self.queue_size = int(queue_size or limits.get("sse_queue_size", QUEUE_MAX))
        self.ring_max = max(1, int(ring_max or limits.get("sse_ring_size", RING_MAX)))
        self.heartbeat_s = float(limits.get("sse_heartbeat_s", HEARTBEAT_S))
        self._snapshot_provider = snapshot_provider
        self._tasks_provider = tasks_provider
        self._model_scan_provider = model_scan_provider
        self._library_scan_provider = library_scan_provider
        # 原始配置只用于算 config.changed.keys；对外快照仍是脱敏后的 state.config。
        self._config_provider = config_provider
        self._lock = threading.RLock()
        self._ring = deque(maxlen=self.ring_max)
        self._subs = set()
        self._sinks = set()
        self._seq = 0
        self._prev = None
        self._sampler = None
        self._stop = threading.Event()
        self._last_sub_at = 0.0
        self._needs_resync = False
        self._dead = False

    # ---- 生命周期 ----
    def _safe_call(self, fn, default):
        if not callable(fn):
            return default
        try:
            value = fn()
            return value if value is not None else default
        except Exception:
            return default

    def _collect(self):
        state = self._safe_call(self._snapshot_provider, {})
        if not isinstance(state, dict):
            state = {}
        tasks = self._safe_call(self._tasks_provider, None)
        if not isinstance(tasks, list):
            tasks = state.get("tasks") if isinstance(state.get("tasks"), list) else []
        ui = state.get("ui") if isinstance(state.get("ui"), dict) else {}
        guides = ui.get("guides") if isinstance(ui.get("guides"), dict) else {}
        config = state.get("config") if isinstance(state.get("config"), dict) else {}
        raw_config = self._safe_call(self._config_provider, None)
        if not isinstance(raw_config, dict):
            raw_config = config
        else:
            # 配置是原地更新的同一个 dict；必须快照，否则上一帧也会被改成新值。
            raw_config = copy.deepcopy(raw_config)
        choice_log = state.get("file_choice_transitions")
        if not isinstance(choice_log, list):
            choice_log = []
        return {
            "tasks": _tasks_map(tasks),
            "choice": _choice_public(state.get("pending_file_choice")),
            "choice_rev": int(state.get("file_choice_rev") or 0),
            "choice_log": [dict(row) for row in choice_log[-20:] if isinstance(row, dict)],
            "model_scan": _scan_public(self._safe_call(self._model_scan_provider, {})),
            "library_scan": _scan_public(self._safe_call(self._library_scan_provider, {})),
            "overlays": _overlays_public(guides.get("overlays") if isinstance(guides, dict) else []),
            "dismiss_rev": int(guides.get("dismiss_rev") or 0) if isinstance(guides, dict) else 0,
            "dismiss_targets": list(guides.get("dismiss_targets") or []) if isinstance(guides, dict) else [],
            "config_rev": int(state.get("config_rev") or 0),
            "config": config,
            "config_raw": raw_config,
        }

    def _ensure_sampler(self):
        now = time.time()
        self._last_sub_at = now
        with self._lock:
            if self._sampler is not None and self._sampler.is_alive():
                return False
            resync = bool(self._needs_resync)
            self._needs_resync = False
            self._prev = self._collect()
            self._stop.clear()
            self._sampler = threading.Thread(target=self._run, daemon=True,
                                             name="cft-sse-events")
            self._sampler.start()
            return resync

    def _run(self):
        while not self._stop.wait(self.interval):
            with self._lock:
                if not self._subs and not self._sinks:
                    idle = time.time() - self._last_sub_at
                    if idle >= self.idle_stop:
                        self._sampler = None
                        self._stop.set()
                        self._needs_resync = True
                        return
            try:
                self.sample_once()
            except Exception:
                # 采样失败不能让 SSE 线程退出；下个周期继续。
                continue

    def sample_once(self):
        current = self._collect()
        with self._lock:
            prev = self._prev or current
            self._prev = current
        for event_type, data, group in self._diff(prev, current):
            self.publish(event_type, data, group=group)
        return current

    def _diff(self, prev, curr):
        out = []
        prev_tasks, curr_tasks = prev.get("tasks") or {}, curr.get("tasks") or {}
        for key, task in curr_tasks.items():
            old = prev_tasks.get(key)
            if old is None:
                out.append(("task.created", {k: task.get(k) for k in
                                             ("id", "filename", "status", "dest_dir")}, "task"))
                if task.get("status") in TERMINAL:
                    out.append(("task.finished", task, "task"))
            elif task.get("status") != old.get("status") and task.get("status") in TERMINAL:
                out.append(("task.finished", task, "task"))
            elif _task_changed(old, task):
                out.append(("task.updated", task, "task"))
        for key, task in prev_tasks.items():
            if key not in curr_tasks:
                out.append(("task.removed", {"id": task.get("id")}, "task"))

        prev_rev = int(prev.get("choice_rev") or 0)
        curr_rev = int(curr.get("choice_rev") or 0)
        if curr_rev > prev_rev:
            for row in curr.get("choice_log") or []:
                if int(row.get("rev") or 0) <= prev_rev:
                    continue
                event = str(row.get("event") or "")
                choice_id = _short(row.get("id"), 120)
                if event == "pending":
                    out.append(("file_choice.pending", {
                        "id": choice_id,
                        "files_count": int(row.get("files_count") or 0),
                    }, "file_choice"))
                elif event == "resolved":
                    out.append(("file_choice.resolved", {
                        "id": choice_id,
                        "selected_count": int(row.get("selected_count") or 0),
                        "cancelled": bool(row.get("cancelled")),
                    }, "file_choice"))
        else:
            # 兼容不暴露 broker revision 的旧状态源：保留当前快照边沿检测。
            prev_choice, curr_choice = prev.get("choice"), curr.get("choice")
            prev_choice_id = prev_choice.get("id") if isinstance(prev_choice, dict) else None
            curr_choice_id = curr_choice.get("id") if isinstance(curr_choice, dict) else None
            if curr_choice_id and curr_choice_id != prev_choice_id:
                out.append(("file_choice.pending", curr_choice, "file_choice"))
            elif prev_choice_id and not curr_choice_id:
                out.append(("file_choice.resolved", {"id": prev_choice_id}, "file_choice"))

        for prefix, key, can_progress in (
            ("model_scan", "model_scan", True),
            ("library.scan", "library_scan", True),
        ):
            old = prev.get(key) or {}
            new = curr.get(key) or {}
            scan_fields = {k: new.get(k) for k in ("phase", "done", "total", "msg")}
            old_fields = {k: old.get(k) for k in ("phase", "done", "total", "msg")}
            if not old.get("running") and new.get("running"):
                out.append((prefix + ".started", new, "scan"))
            elif old.get("running") and not new.get("running"):
                out.append((prefix + ".finished", new, "scan"))
            elif not old.get("running") and not new.get("running") and \
                    scan_fields.get("msg") and scan_fields != old_fields:
                # 整个扫描发生在两次采样之间时，至少如实报告一个新的完成/失败结果。
                out.append((prefix + ".finished", new, "scan"))
            elif can_progress and new.get("running") and scan_fields != old_fields:
                out.append((prefix + ".progress", new, "scan"))

        old_overlays = prev.get("overlays") or []
        new_overlays = curr.get("overlays") or []
        if _json_key(old_overlays) != _json_key(new_overlays):
            out.append(("ui.overlays.changed", {"overlays": new_overlays}, "ui"))
        if int(curr.get("dismiss_rev") or 0) != int(prev.get("dismiss_rev") or 0):
            out.append(("ui.dismissed", {"rev": int(curr.get("dismiss_rev") or 0),
                                         "targets": list(curr.get("dismiss_targets") or [])}, "ui"))

        old_cfg = prev.get("config_raw") if isinstance(prev.get("config_raw"), dict) \
            else (prev.get("config") or {})
        new_cfg = curr.get("config_raw") if isinstance(curr.get("config_raw"), dict) \
            else (curr.get("config") or {})
        if int(curr.get("config_rev") or 0) != int(prev.get("config_rev") or 0):
            keys = sorted(k for k in set(old_cfg) | set(new_cfg)
                          if _json_key(old_cfg.get(k)) != _json_key(new_cfg.get(k)))
            out.append(("config.changed", {"rev": int(curr.get("config_rev") or 0), "keys": keys},
                        "config"))
        return out

    # ---- 发布与订阅 ----
    def publish(self, event_type, data=None, *, group=None, replay=True):
        if event_type not in ai_contract.EVENT_TYPES:
            return None
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "instance_id": self.instance_id,
                "type": event_type,
                "group": group or ai_contract.EVENT_TYPES[event_type]["group"],
                "at": _now(),
                "data": data if isinstance(data, dict) else {},
            }
            if replay:
                self._ring.append(event)
            subs = list(self._subs)
            sinks = list(self._sinks)
        for sub in subs:
            sub.offer(event)
        for sink in sinks:
            try:
                sink(event)
            except Exception:
                # sink 是旁路：webhook 故障不能影响 SSE / 下载 / 其它订阅者。
                continue
        return event

    def add_sink(self, sink):
        """注册旁路消费者（webhook）：不占 SSE 客户端名额，但同样维持采样线程。"""
        if not callable(sink):
            return False
        with self._lock:
            if self._dead:
                return False
            self._sinks.add(sink)
            self._ensure_sampler()
        return True

    def remove_sink(self, sink):
        with self._lock:
            self._sinks.discard(sink)
            self._last_sub_at = time.time()

    def active_sinks(self):
        with self._lock:
            return len(self._sinks)

    def subscribe(self, since=None, types=None, queue_size=None):
        with self._lock:
            if self._dead or len(self._subs) >= self.max_clients:
                return None
            resync = self._ensure_sampler()
            sub = Subscription(types=types, queue_size=queue_size or self.queue_size)
            self._subs.add(sub)
            last_seq = self._seq
            ring = list(self._ring)
        if since is None:
            return sub, {"replay": [], "reset": False, "last_seq": last_seq,
                         "oldest_seq": ring[0]["seq"] if ring else last_seq + 1}
        reset = bool(resync or (ring and since < ring[0]["seq"] - 1))
        replay = [event for event in ring if event["seq"] > since and sub.wants(event)] \
            if not reset else []
        return sub, {"replay": replay, "reset": reset, "last_seq": last_seq,
                     "oldest_seq": ring[0]["seq"] if ring else last_seq + 1}

    def unsubscribe(self, sub):
        with self._lock:
            self._subs.discard(sub)
            self._last_sub_at = time.time()
        sub.close()

    def last_seq(self):
        with self._lock:
            return self._seq, (self._ring[0]["seq"] if self._ring else self._seq + 1)

    def active_clients(self):
        with self._lock:
            return len(self._subs)

    def shutdown(self):
        with self._lock:
            if self._dead:
                return
            self._dead = True
        if self._subs or self._sinks:
            self.publish("app.stopping", {"pid": os.getpid()}, group="app")
        with self._lock:
            subs = list(self._subs)
            self._subs.clear()
            self._sinks.clear()
            self._stop.set()
        for sub in subs:
            sub.close(clear=False)
        sampler = self._sampler
        if sampler is not None and sampler is not threading.current_thread():
            sampler.join(timeout=0.2)


# ---- 模块级单例：bridge 配置，automation_api 记录调用事件 ----
_hub = None
_hub_lock = threading.Lock()


def configure(**kwargs):
    global _hub
    with _hub_lock:
        if _hub is not None:
            _hub.shutdown()
        _hub = EventHub(**kwargs)
    return _hub


def get_hub():
    return _hub


def publish_event(event_type, data=None, *, group=None, replay=True):
    hub = _hub
    if hub is not None:
        return hub.publish(event_type, data, group=group, replay=replay)
    return None


def publish_api_call(method, path, status, ms, summary=""):
    hub = _hub
    if hub is None:
        return None
    return hub.publish("api.call", {
        "method": str(method or ""), "path": _short(path, 300),
        "status": int(status or 0), "ms": round(float(ms or 0), 1),
        "summary": _short(summary, 300),
    }, group="api", replay=False)


def shutdown():
    global _hub
    with _hub_lock:
        hub, _hub = _hub, None
    if hub is not None:
        hub.shutdown()
