# -*- coding: utf-8 -*-
"""SSE 事件总线与传输：快照差分、重放、心跳、背压、真实 HTTP 流。"""
import http.client
import json
import queue
import socket
import struct
import threading
import time
import unittest

import automation_api
import automation_events
import browser_bridge


def _drain(sub, timeout=0.2):
    rows = []
    while True:
        try:
            rows.append(sub.queue.get(timeout=timeout))
        except queue.Empty:
            return rows


def _state(tasks=None, config=None, config_rev=1, choice=None,
           dismiss_rev=0, dismiss_targets=None, overlays=None):
    return {
        "config": dict(config or {"download_dir": "D:/out"}),
        "config_rev": config_rev,
        "pending_file_choice": choice,
        "tasks": list(tasks or []),
        "ui": {"guides": {
            "dismiss_rev": dismiss_rev,
            "dismiss_targets": list(dismiss_targets or []),
            "overlays": list(overlays or []),
        }},
    }


class EventHubTest(unittest.TestCase):
    def make_hub(self, **kwargs):
        self.state = _state()
        self.tasks = []
        self.model_scan = {"running": False, "phase": "", "done": 0, "total": 0, "msg": ""}
        self.library_scan = {"running": False, "phase": "", "done": 0, "total": 0, "msg": ""}
        options = {
            "snapshot_provider": lambda: self.state,
            "tasks_provider": lambda: self.tasks,
            "model_scan_provider": lambda: self.model_scan,
            "library_scan_provider": lambda: self.library_scan,
            "interval": 10.0,
            "idle_stop": 10.0,
        }
        options.update(kwargs)
        self.hub = automation_events.EventHub(**options)
        return self.hub

    def subscribe(self, **kwargs):
        result = self.hub.subscribe(**kwargs)
        self.assertIsNotNone(result)
        return result

    def tearDown(self):
        hub = getattr(self, "hub", None)
        if hub is not None:
            hub.shutdown()
        automation_events.shutdown()

    def test_task_lifecycle_events(self):
        self.make_hub()
        sub, _ = self.subscribe()
        self.tasks.append({"id": "t1", "filename": "a.safetensors",
                           "status": "downloading", "progress": 10})
        self.hub.sample_once()
        events = _drain(sub)
        self.assertEqual([e["type"] for e in events], ["task.created"])

        self.tasks[0].update({"progress": 40})
        self.hub.sample_once()
        self.assertEqual([e["type"] for e in _drain(sub)], ["task.updated"])

        self.tasks[0].update({"status": "done", "progress": 100})
        self.hub.sample_once()
        events = _drain(sub)
        self.assertEqual([e["type"] for e in events], ["task.finished"])
        self.assertEqual(events[0]["data"]["status"], "done")

        self.tasks.clear()
        self.hub.sample_once()
        self.assertEqual([e["type"] for e in _drain(sub)], ["task.removed"])

    def test_file_choice_events(self):
        self.make_hub()
        sub, _ = self.subscribe()
        self.state["pending_file_choice"] = {"id": "choice-1", "files": [{}, {}]}
        self.hub.sample_once()
        events = _drain(sub)
        self.assertEqual(events[0]["type"], "file_choice.pending")
        self.assertEqual(events[0]["data"]["files_count"], 2)
        self.state["pending_file_choice"] = None
        self.hub.sample_once()
        events = _drain(sub)
        self.assertEqual(events[0]["type"], "file_choice.resolved")
        self.assertEqual(events[0]["data"]["id"], "choice-1")

    def test_scan_and_ui_and_config_events(self):
        self.make_hub()
        sub, _ = self.subscribe()
        self.model_scan.update({"running": True, "phase": "walk"})
        self.hub.sample_once()
        self.assertEqual([e["type"] for e in _drain(sub)], ["model_scan.started"])

        self.library_scan.update({"running": True, "phase": "walk"})
        self.hub.sample_once()
        self.assertEqual([e["type"] for e in _drain(sub)], ["library.scan.started"])
        self.library_scan.update({"done": 5, "total": 10})
        self.hub.sample_once()
        self.assertEqual([e["type"] for e in _drain(sub)], ["library.scan.progress"])
        self.library_scan.update({"running": False, "phase": "done"})
        self.hub.sample_once()
        self.assertEqual([e["type"] for e in _drain(sub)], ["library.scan.finished"])

        self.state["ui"]["guides"]["overlays"] = [
            {"name": "onboarding", "group": "guides", "kind": "guide",
             "title": "新手引导", "blocking": True}]
        self.hub.sample_once()
        events = _drain(sub)
        self.assertEqual(events[0]["type"], "ui.overlays.changed")
        self.assertEqual(events[0]["data"]["overlays"][0]["name"], "onboarding")

        self.state["ui"]["guides"].update({"dismiss_rev": 2,
                                           "dismiss_targets": ["onboarding"]})
        self.hub.sample_once()
        events = _drain(sub)
        self.assertEqual(events[0]["type"], "ui.dismissed")
        self.assertEqual(events[0]["data"]["targets"], ["onboarding"])

        self.state.update({"config_rev": 2,
                           "config": {"download_dir": "D:/new"}})
        self.hub.sample_once()
        events = _drain(sub)
        self.assertEqual(events[0]["type"], "config.changed")
        self.assertEqual(events[0]["data"]["keys"], ["download_dir"])

    def test_replay_and_reset(self):
        self.make_hub(ring_max=2)
        self.hub.publish("task.updated", {"id": "t1", "status": "downloading"})
        self.hub.publish("task.updated", {"id": "t1", "status": "downloading"})
        self.hub.publish("task.updated", {"id": "t1", "status": "downloading"})
        sub, info = self.subscribe(since=1, types={"task"})
        self.assertFalse(info["reset"])
        self.assertEqual([e["seq"] for e in info["replay"]], [2, 3])
        hub2 = self.hub
        sub2, info2 = hub2.subscribe(since=0, types={"task"})
        self.assertTrue(info2["reset"])
        self.assertEqual(info2["replay"], [])
        hub2.unsubscribe(sub2)

    def test_unsubscribe_to_resubscribe_forces_replay_reset(self):
        self.make_hub(interval=0.05, idle_stop=0.2)
        sub, _ = self.subscribe()
        event = self.hub.publish("task.updated", {"id": "t1", "status": "downloading"})
        self.hub.unsubscribe(sub)
        time.sleep(0.45)
        result = self.hub.subscribe(since=event["seq"] - 1, types={"task"})
        self.assertIsNotNone(result)
        _, info = result
        self.assertTrue(info["reset"])
        self.assertEqual(info["replay"], [])
        self.hub.unsubscribe(result[0])

    def test_replay_respects_type_filter(self):
        self.make_hub()
        self.hub.publish("ui.dismissed", {"rev": 1, "targets": ["all"]}, group="ui")
        self.hub.publish("task.updated", {"id": "t1", "status": "downloading"}, group="task")
        _, info = self.subscribe(since=0, types={"task"})
        self.assertEqual([e["type"] for e in info["replay"]], ["task.updated"])

    def test_task_that_appears_terminal_emits_created_and_finished(self):
        self.make_hub()
        sub, _ = self.subscribe()
        self.tasks.append({"id": "fast", "filename": "done.safetensors",
                           "status": "done", "progress": 100})
        self.hub.sample_once()
        events = _drain(sub)
        self.assertEqual([e["type"] for e in events], ["task.created", "task.finished"])

    def test_file_choice_log_catches_fast_transition(self):
        self.make_hub()
        sub, _ = self.subscribe()
        self.state["file_choice_rev"] = 1
        self.state["file_choice_transitions"] = [
            {"rev": 1, "event": "resolved", "id": "choice-1",
             "selected_count": 1, "cancelled": False}]
        self.hub.sample_once()
        events = _drain(sub)
        self.assertEqual([e["type"] for e in events], ["file_choice.resolved"])
        self.assertEqual(events[0]["data"]["selected_count"], 1)

    def test_scan_completed_between_samples_emits_finished(self):
        self.make_hub()
        sub, _ = self.subscribe()
        self.model_scan.update({"running": False, "phase": "done",
                                "total": 7, "msg": "扫描完成：7 个模型"})
        self.hub.sample_once()
        events = _drain(sub)
        self.assertEqual([e["type"] for e in events], ["model_scan.finished"])

    def test_shutdown_prevents_sampler_restart(self):
        self.make_hub()
        self.hub.shutdown()
        self.assertIsNone(self.hub.subscribe())
        self.assertFalse(self.hub._sampler is not None and self.hub._sampler.is_alive())

    def test_type_filter_and_overflow(self):
        self.make_hub(queue_size=1)
        sub, _ = self.subscribe(types={"task"})
        self.hub.publish("ui.dismissed", {"rev": 1, "targets": ["all"]})
        self.hub.publish("task.updated", {"id": "t1", "status": "downloading"})
        events = _drain(sub)
        self.assertEqual([e["type"] for e in events], ["task.updated"])

        self.hub.publish("task.updated", {"id": "t2", "status": "downloading"})
        self.hub.publish("task.updated", {"id": "t3", "status": "downloading"})
        self.assertEqual(sub.take_dropped(), 1)

        other, _ = self.subscribe(types={"ui"})
        self.hub.publish("api.call", {"path": "/x"}, group="api", replay=False)
        self.assertEqual(_drain(other), [])

    def test_max_clients(self):
        self.make_hub(max_clients=1)
        first = self.hub.subscribe()
        self.assertIsNotNone(first)
        self.assertIsNone(self.hub.subscribe())
        self.hub.unsubscribe(first[0])

    def test_record_event_reaches_live_subscriber(self):
        hub = automation_events.configure(
            snapshot_provider=lambda: _state(),
            interval=10.0, idle_stop=10.0)
        sub = hub.subscribe(types={"api"})
        self.assertIsNotNone(sub)
        automation_api.record_event("GET", "/api/automation/state", 200, 12.5, "ok")
        events = _drain(sub[0])
        self.assertEqual(events[0]["type"], "api.call")
        self.assertEqual(events[0]["data"]["path"], "/api/automation/state")
        self.assertFalse(any(e["type"] == "api.call" for e in hub._ring))

    def test_shutdown_publishes_stopping(self):
        self.make_hub()
        sub, _ = self.subscribe(types={"app"})
        self.hub.shutdown()
        events = _drain(sub, timeout=0.5)
        self.assertTrue(any(e["type"] == "app.stopping" for e in events))


class _Task:
    def __init__(self, task_id, status="downloading", progress=0):
        self._row = {"id": task_id, "filename": "a.safetensors", "status": status,
                     "dest_dir": "D:/out", "progress": progress,
                     "downloaded": 0, "total": 10, "error": ""}

    def to_dict(self):
        return dict(self._row)


class _DL:
    def __init__(self):
        self.tasks = []


class _SseApi:
    def __init__(self):
        self.cfg = {"download_dir": "D:/out", "site_domain": "civitai.red",
                    "comfyui_models_root": "", "route_by_type": False}
        self.dl = _DL()
        self._cfg_rev = 0
        self._ui_dismiss_rev = 0
        self._ui_dismiss_targets = []
        self._ui_overlays = []
        self.model_scan = {"running": False, "phase": "", "done": 0, "total": 0, "msg": ""}
        self.lib_scan = {"running": False, "phase": "", "done": 0, "total": 0, "msg": ""}

    def get_download_file_choice(self):
        return None

    def get_scan_state(self):
        return dict(self.model_scan)

    def lib_scan_state(self):
        return dict(self.lib_scan)


def _free_port():
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _read_event(resp, timeout=5.0):
    """读取一个 SSE 帧；返回 (event_type, data, raw_lines)，注释帧返回 ('comment', ...)。"""
    deadline = time.time() + timeout
    lines = []
    while time.time() < deadline:
        line = resp.readline()
        if not line:
            return None, None, lines
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        if text == "":
            if not lines:
                continue
            break
        lines.append(text)
    else:
        return None, None, lines
    event_type, data, comments = "message", None, []
    payload = []
    for text in lines:
        if text.startswith(":"):
            comments.append(text)
        elif text.startswith("event:"):
            event_type = text.split(":", 1)[1].strip()
        elif text.startswith("data:"):
            payload.append(text.split(":", 1)[1].strip())
    if comments and not payload:
        return "comment", comments, lines
    if payload:
        try:
            data = json.loads("\n".join(payload))
        except ValueError:
            data = "\n".join(payload)
    return event_type, data, lines


class SseTransportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owned_server = browser_bridge._server is None
        cls.prev_api = browser_bridge._api
        cls.api = _SseApi()
        cls.port = browser_bridge.start(version="test", port=_free_port()) or browser_bridge.port()
        if not cls.port:
            raise RuntimeError("bridge did not start")
        browser_bridge.set_api(cls.api)

    @classmethod
    def tearDownClass(cls):
        browser_bridge._api = cls.prev_api
        if cls.owned_server:
            browser_bridge.stop()

    def connect(self, query="", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/automation/events/stream" + query,
                     headers=headers or {})
        resp = conn.getresponse()
        return conn, resp

    def json_call(self, path, method="GET", body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        conn.request(method, path, body=payload,
                     headers={"Content-Type": "application/json"} if payload else {})
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        return resp.status, json.loads(raw)

    def test_openapi_and_per_tool_alias_over_http(self):
        st, doc = self.json_call("/api/automation/openapi.json?profile=text")
        self.assertEqual(st, 200, doc)
        self.assertEqual(len(doc["paths"]), 19)
        st, body = self.json_call("/api/automation/tool/cft_capabilities", "POST", {})
        self.assertEqual(st, 200, body)
        self.assertTrue(body["ok"])
        self.assertTrue(body["data"]["caps"]["sse"])
        st, body = self.json_call("/api/automation/tool/cft_capabilities")
        self.assertEqual(st, 405, body)

    def test_body_cap_follows_large_body_tools(self):
        pad = "x" * (1 << 20)
        st, body = self.json_call("/api/automation/ui/dismiss", "POST", {"targets": ["all"], "pad": pad})
        self.assertEqual(st, 413, body)
        self.assertEqual(body["limit_bytes"], 1 << 20)
        # 同一份载荷走 8MB 口径：能过闸，剩下的交给工具自己校验。
        st, body = self.json_call("/api/automation/tool/cft_inspect_image", "POST",
                                  {"path": "D:/不存在的图.png", "pad": pad})
        self.assertNotEqual(st, 413, body)
        self.assertEqual(st, 404, body)
        st, body = self.json_call("/api/automation/tool/cft_inspect_image", "POST",
                                  {"pad": "x" * (8 << 20)})
        self.assertEqual(st, 413, body)
        self.assertEqual(body["limit_bytes"], 8 << 20)

    def test_ready_snapshot_and_live_event(self):
        conn, resp = self.connect("?snapshot=1&types=ui")
        try:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.getheader("Content-Type").split(";")[0],
                             "text/event-stream")
            kind, ready, _ = _read_event(resp)
            self.assertEqual(kind, "stream.ready")
            self.assertEqual(ready["data"]["instance_id"],
                             automation_events.get_hub().instance_id)
            kind, snapshot, _ = _read_event(resp)
            self.assertEqual(kind, "state.snapshot")
            self.assertIn("config", snapshot["data"])
            automation_events.publish_event("ui.dismissed",
                                            {"rev": 3, "targets": ["onboarding"]},
                                            group="ui")
            kind, event, _ = _read_event(resp)
            self.assertEqual(kind, "ui.dismissed")
            self.assertEqual(event["data"]["targets"], ["onboarding"])
        finally:
            conn.close()

    def test_replay_with_last_event_id(self):
        hub = automation_events.get_hub()
        hub.publish("ui.dismissed", {"rev": 1, "targets": ["all"]}, group="ui")
        first_seq = hub.last_seq()[0]
        conn, resp = self.connect("?snapshot=0&types=ui")
        try:
            _read_event(resp)  # ready
        finally:
            conn.close()
        conn, resp = self.connect("?snapshot=0&types=ui",
                                  headers={"Last-Event-ID": str(first_seq - 1)})
        try:
            kind, ready, _ = _read_event(resp)
            self.assertEqual(kind, "stream.ready")
            kind, event, _ = _read_event(resp)
            self.assertEqual(kind, "ui.dismissed")
            self.assertEqual(event["seq"], first_seq)
        finally:
            conn.close()

    def test_heartbeat_comment(self):
        hub = automation_events.get_hub()
        old = hub.heartbeat_s
        hub.heartbeat_s = 0.2
        conn, resp = self.connect("?snapshot=0&types=task")
        try:
            kind, _, _ = _read_event(resp)
            self.assertEqual(kind, "stream.ready")
            kind, _, lines = _read_event(resp)
            self.assertEqual(kind, "comment")
            self.assertTrue(any(line.startswith(": keep-alive") for line in lines))
        finally:
            conn.close()
            hub.heartbeat_s = old

    def test_ready_data_matches_contract_schema(self):
        conn, resp = self.connect("?snapshot=0&types=task")
        try:
            kind, ready, _ = _read_event(resp)
            self.assertEqual(kind, "stream.ready")
            data = ready["data"]
            self.assertIsInstance(data["heartbeat_s"], (int, float))
            self.assertIsInstance(data["types"], list)
            self.assertEqual(set(data), {"instance_id", "last_seq", "oldest_seq",
                                         "heartbeat_s", "types"})
        finally:
            conn.close()

    def test_client_abort_before_headers_does_not_leak_subscription(self):
        hub = automation_events.get_hub()
        deadline = time.time() + 3
        while hub.active_clients() and time.time() < deadline:
            time.sleep(0.05)
        before = hub.active_clients()
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            sock.sendall(b"GET /api/automation/events/stream?snapshot=0 HTTP/1.1\r\n"
                         b"Host: 127.0.0.1\r\n\r\n")
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        finally:
            sock.close()
        deadline = time.time() + 3
        while hub.active_clients() > before and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(hub.active_clients(), before)

    def test_max_clients_returns_429(self):
        hub = automation_events.get_hub()
        deadline = time.time() + 3
        while hub.active_clients() and time.time() < deadline:
            time.sleep(0.05)
        old = hub.max_clients
        hub.max_clients = 1
        conn1, resp1 = self.connect("?snapshot=0&types=task")
        try:
            self.assertEqual(resp1.status, 200)
            _read_event(resp1)
            conn2, resp2 = self.connect("?snapshot=0&types=task")
            try:
                self.assertEqual(resp2.status, 429)
                body = json.loads(resp2.read().decode("utf-8"))
                self.assertFalse(body["ok"])
                self.assertEqual(body["limit"], 1)
            finally:
                conn2.close()
        finally:
            conn1.close()
            hub.max_clients = old


if __name__ == "__main__":
    unittest.main()
