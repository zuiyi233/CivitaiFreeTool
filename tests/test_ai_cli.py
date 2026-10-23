# -*- coding: utf-8 -*-
"""CLI 传输层：本机 fake bridge 验证参数、退出码、SSE、doctor 与图片落盘。"""
import base64
import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ai_contract
import cft_cli
import openapi_projection


def sse_frame(event_type, event_id, data):
    return ("event: %s\nid: %s\ndata: %s\n\n"
            % (event_type, event_id, json.dumps(data, ensure_ascii=False))).encode("utf-8")


class FakeBridge:
    def __init__(self):
        self.posts = []
        self.sse_headers = []
        self.sse_paths = []
        self.sse_frames = []
        self.sse_sleep_after = 0.0
        self._free_port = None
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _json(self, status, payload):
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except OSError:
                    pass

            def do_GET(self):
                split = urllib.parse.urlsplit(self.path)
                path, query = split.path, urllib.parse.parse_qs(split.query)
                if path == "/api/health":
                    return self._json(200, {"ok": True, "app": "CivitaiFreeTool",
                                            "version": "2.7.0"})
                if path == "/api/automation/ai":
                    return self._json(200, bridge.caps())
                if path == "/api/automation/tools":
                    profile = (query.get("profile") or ["text"])[0]
                    fmt = (query.get("format") or ["mcp"])[0]
                    rows = [ai_contract.public_tool(tool, format=fmt)
                            for tool in ai_contract.tools_for_profile(profile)]
                    return self._json(200, {"ok": True, "tools": rows, "count": len(rows),
                                            "profile": profile, "format": fmt})
                if path == "/api/automation/guide":
                    profile = (query.get("profile") or ["text"])[0]
                    return self._json(200, {"ok": True, "profile": profile,
                                            "markdown": "# guide\n" + profile,
                                            "guide": "guide text"})
                if path == "/api/automation/openapi.json":
                    profile = (query.get("profile") or ["text"])[0]
                    return self._json(200, openapi_projection.build_document(
                        profile, host_header="127.0.0.1:47531", app_version="2.7.0"))
                if path == "/api/automation/events/stream":
                    with bridge_lock:
                        bridge.sse_headers.append({k.lower(): v for k, v in self.headers.items()})
                        bridge.sse_paths.append(self.path)
                        frames = bridge.sse_frames.pop(0) if bridge.sse_frames else b""
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    try:
                        self.wfile.write(frames)
                        self.wfile.flush()
                    except OSError:
                        pass
                    if bridge.sse_sleep_after:
                        threading.Event().wait(bridge.sse_sleep_after)
                    self.close_connection = True
                    return
                return self._json(404, {"ok": False, "code": "not_found", "msg": "no route"})

            def do_POST(self):
                split = urllib.parse.urlsplit(self.path)
                path = split.path
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    return self._json(400, {"ok": False, "code": "invalid_argument",
                                            "msg": "bad json"})
                if path == "/api/automation/batch":
                    return self._json(200, {
                        "ok": False, "executed": 2, "requested": 2,
                        "results": [
                            {"ok": True, "http_status": 200, "result": {"ok": True}},
                            {"ok": False, "http_status": 400,
                             "result": {"ok": False, "code": "destructive_requires_confirm"}},
                        ],
                    })
                prefix = "/api/automation/tool/"
                if not path.startswith(prefix):
                    return self._json(404, {"ok": False, "code": "not_found", "msg": "no route"})
                name = urllib.parse.unquote(path[len(prefix):])
                with bridge_lock:
                    bridge.posts.append((name, body))
                if name == "cft_fail":
                    return self._json(400, {"ok": False, "code": "destructive_requires_confirm",
                                            "msg": "need confirm"})
                if name == "cft_missing":
                    return self._json(404, {"ok": False, "code": "not_found",
                                            "msg": "tool missing"})
                if name == "cft_image":
                    payload = base64.b64encode(b"png-bytes").decode("ascii")
                    return self._json(200, {
                        "ok": True, "tool": name, "data": {},
                        "content": [{"type": "image", "mime": "image/png",
                                     "b64": payload, "label": "#1"}],
                    })
                return self._json(200, {"ok": True, "tool": name,
                                        "data": {"echo": body}, "content": []})

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.server.server_port

    def caps(self):
        return {
            "ok": True, "app": "CivitaiFreeTool", "version": "2.7.0",
            "api_version": ai_contract.API_VERSION,
            "profiles": {key: {"count": len(ai_contract.tools_for_profile(key))}
                         for key in ai_contract.PROFILES},
            "caps": {
                "openapi": True, "sse": True,
                "events": [item["type"] for item in ai_contract.EVENTS],
                "webhook": {"available": True},
                "cli": {"available": True},
                "transports": {"openapi": "/api/automation/openapi.json",
                               "sse": "/api/automation/events/stream",
                               "webhook": "/api/automation/webhooks",
                               "cli": "CivitaiFreeToolCLI.exe"},
            },
            "mcp": {"running": True},
        }

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


bridge_lock = threading.Lock()


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class CliTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBridge()
        self.addCleanup(self.fake.stop)

    def run_cli(self, *argv):
        argv = list(argv)
        if argv:
            argv = [argv[0], "--bridge", self.fake.base] + argv[1:]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cft_cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_version_and_help(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cft_cli.main(["--version"]), 0)
        self.assertIn("29 tools / 21 events", out.getvalue())
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cft_cli.main([]), 2)

    def test_health_and_capabilities(self):
        code, out, _ = self.run_cli("health", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["version"], "2.7.0")
        code, out, _ = self.run_cli("capabilities", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["caps"]["events"]), 21)

    def test_tools_profiles_and_format(self):
        code, out, err = self.run_cli("tools", "--profile", "all", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["count"], 29)
        code, out, err = self.run_cli("tools", "--profile", "text")
        self.assertEqual(code, 0)
        self.assertEqual(len([line for line in out.splitlines() if line.strip()]), 19)
        self.assertEqual(err, "")
        code, _, _ = self.run_cli("tools", "--profile", "nope")
        self.assertEqual(code, 2)

    def test_call_argument_typing(self):
        code, out, _ = self.run_cli(
            "call", "cft_test",
            "--arg", "limit=5", "--arg", "force=true", "--arg", "name=abc",
            "--arg", "tag=a", "--arg", "tag=b", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["data"]["echo"], {
            "limit": 5, "force": True, "name": "abc", "tag": ["a", "b"]})

    def test_call_args_json_and_override(self):
        code, out, _ = self.run_cli(
            "call", "cft_test", "--args", '{"a":1,"b":2}', "--arg", "b=3", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["data"]["echo"], {"a": 1, "b": 3})

    def test_call_exit_codes(self):
        code, out, err = self.run_cli("call", "cft_fail", "--json")
        self.assertEqual(code, 6)
        self.assertEqual(json.loads(out)["code"], "destructive_requires_confirm")
        self.assertIn("destructive_requires_confirm", err)
        code, _, _ = self.run_cli("call", "cft_missing")
        self.assertEqual(code, 5)

    def test_call_save_content(self):
        directory = tempfile.mkdtemp(prefix="cft-cli-content-")
        self.addCleanup(shutil.rmtree, directory, True)
        code, out, _ = self.run_cli("call", "cft_image",
                                    "--save-content", directory, "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["content"][0]["b64"], "<saved>")
        saved = payload["saved_content"][0]
        self.assertTrue(os.path.isfile(saved["path"]))
        with open(saved["path"], "rb") as handle:
            self.assertEqual(handle.read(), b"png-bytes")
        self.assertEqual(saved["mime"], "image/png")

    def test_events_json_and_max(self):
        ready = {"seq": 0, "type": "stream.ready", "group": "stream", "data": {}}
        created = {"seq": 1, "type": "task.created", "group": "task",
                   "data": {"id": "t1"}}
        finished = {"seq": 2, "type": "task.finished", "group": "task",
                    "data": {"id": "t1"}}
        frames = (sse_frame("stream.ready", 0, ready)
                  + sse_frame("task.created", 1, created)
                  + sse_frame("task.finished", 2, finished))
        self.fake.sse_frames = [frames]
        code, out, _ = self.run_cli("events", "--max", "2", "--no-reconnect", "--json")
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual([row["type"] for row in rows], ["stream.ready", "task.created"])
        self.fake.sse_frames = [frames]
        code, out, _ = self.run_cli("events", "--max", "2", "--no-reconnect")
        self.assertEqual(code, 0)
        self.assertIn("#1 task.created", out)

    def test_events_reconnects_with_last_event_id(self):
        first = {"seq": 1, "type": "task.created", "group": "task", "data": {"id": "t1"}}
        second = {"seq": 2, "type": "task.finished", "group": "task", "data": {"id": "t1"}}
        self.fake.sse_frames = [sse_frame("task.created", 1, first),
                                sse_frame("task.finished", 2, second)]
        code, out, _ = self.run_cli("events", "--max", "2", "--json")
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual([row["seq"] for row in rows], [1, 2])
        self.assertEqual(len(self.fake.sse_headers), 2)
        self.assertEqual(self.fake.sse_headers[1].get("last-event-id"), "1")
        self.assertIn("snapshot=0", self.fake.sse_paths[1])

    def test_events_idle_timeout_exits_zero(self):
        ready = {"seq": 0, "type": "stream.ready", "group": "stream", "data": {}}
        self.fake.sse_frames = [sse_frame("stream.ready", 0, ready)]
        self.fake.sse_sleep_after = 1.0
        code, out, _ = self.run_cli("events", "--idle-timeout", "0.2",
                                    "--no-reconnect", "--json")
        self.assertEqual(code, 0)
        self.assertIn("stream.ready", out)

    def test_doctor_ok_and_missing_transport(self):
        code, out, _ = self.run_cli("doctor", "--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["ok"])

        original = self.fake.caps

        def old_caps():
            payload = original()
            payload["caps"]["webhook"] = {"available": False}
            return payload

        self.fake.caps = old_caps
        code, out, _ = self.run_cli("doctor", "--json")
        self.assertEqual(code, 1)
        checks = {item["name"]: item["ok"] for item in json.loads(out)["checks"]}
        self.assertFalse(checks["webhook"])

    def test_batch_exit_code_uses_first_failure(self):
        handle, path = tempfile.mkstemp(prefix="cft-batch-", suffix=".json")
        os.close(handle)
        self.addCleanup(os.remove, path)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({"calls": [{"method": "GET", "path": "/api/health"}]}, stream)
        code, out, _ = self.run_cli("batch", "--file", path, "--json")
        self.assertEqual(code, 6)
        self.assertEqual(len(json.loads(out)["results"]), 2)

    def test_guide_and_openapi(self):
        code, out, _ = self.run_cli("guide", "--markdown")
        self.assertEqual(code, 0)
        self.assertIn("# guide", out)
        code, out, _ = self.run_cli("openapi", "--profile", "text", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["paths"]), 19)

    def test_global_options_before_subcommand(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cft_cli.main(["--bridge", self.fake.base, "health", "--json"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["version"], "2.7.0")

    def test_events_app_down_exits_three(self):
        port = free_port()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cft_cli.main(["events", "--bridge", "http://127.0.0.1:%d" % port,
                                 "--timeout", "0.3", "--json"])
        self.assertEqual(code, 3, err.getvalue())
        self.assertIn("app_not_running", err.getvalue())

    def test_save_content_supports_all_block_shapes(self):
        directory = tempfile.mkdtemp(prefix="cft-cli-shapes-")
        self.addCleanup(shutil.rmtree, directory, True)
        b64_png = base64.b64encode(b"neutral").decode("ascii")
        b64_jpg = base64.b64encode(b"mcp").decode("ascii")
        b64_webp = base64.b64encode(b"anthropic").decode("ascii")
        b64_gif = base64.b64encode(b"openai").decode("ascii")
        payload = {"ok": True, "content": [
            {"type": "image", "mime": "image/png", "b64": b64_png, "label": "#1"},
            {"type": "image", "mimeType": "image/jpeg", "data": b64_jpg, "label": "#2"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/webp",
                                         "data": b64_webp}},
            {"type": "image_url", "image_url": {
                "url": "data:image/gif;base64," + b64_gif}},
        ]}
        cleaned, saved = cft_cli._save_content(payload, directory)
        self.assertEqual(len(saved), 4, saved)
        self.assertTrue(all(os.path.isfile(item["path"]) for item in saved))
        self.assertEqual(cleaned["content"][0]["b64"], "<saved>")
        self.assertEqual(cleaned["content"][1]["data"], "<saved>")
        self.assertEqual(cleaned["content"][2]["source"]["data"], "<saved>")
        self.assertEqual(cleaned["content"][3]["image_url"]["url"], "<saved>")

    def test_non_loopback_bridge_rejected(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cft_cli.main(["health", "--bridge", "http://example.com:47531"])
        self.assertEqual(code, 2)
        self.assertIn("回环", err.getvalue())

    def test_bridge_userinfo_rejected(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cft_cli.main(["health", "--bridge", "http://user:pass@127.0.0.1:47531"])
        self.assertEqual(code, 2)
        self.assertIn("用户名/密码", err.getvalue())

    def test_app_not_running_exit_code(self):
        port = free_port()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cft_cli.main(["health", "--bridge", "http://127.0.0.1:%d" % port,
                                 "--timeout", "0.4"])
        self.assertEqual(code, 3)
        self.assertIn("app_not_running", err.getvalue())

    def test_piped_output_is_utf8(self):
        """Windows 管道默认 ANSI 代码页；Agent 捕获时中文必须能按 UTF-8 解码。"""
        port = free_port()
        env = dict(os.environ)
        env.pop("PYTHONIOENCODING", None)
        proc = subprocess.run(
            [sys.executable, cft_cli.__file__, "health",
             "--bridge", "http://127.0.0.1:%d" % port, "--timeout", "0.4"],
            capture_output=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        text = proc.stderr.decode("utf-8")
        self.assertIn("app_not_running", text)
        self.assertIn("连不上", text)


if __name__ == "__main__":
    unittest.main()
