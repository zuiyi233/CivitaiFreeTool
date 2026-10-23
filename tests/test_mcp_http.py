# -*- coding: utf-8 -*-
"""Streamable HTTP transport tests for mcp_server.py.

A fake local bridge keeps the test independent from the desktop app; the MCP
HTTP server binds 127.0.0.1 on a free port and is always terminated.
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import AnyUrl

import ai_contract as C

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "mcp_server.py"
TOKEN = "testtoken"


class FakeBridge:
    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:
                    pass

            def do_GET(self):
                path = urlsplit(self.path).path
                if path == "/api/health":
                    return self._send(200, {
                        "ok": True, "app": "CivitaiFreeTool", "version": "2.7.5"})
                if path == "/api/automation/ai":
                    return self._send(200, {
                        "ok": True, "api_version": C.API_VERSION,
                        "app_version": "2.7.5"})
                if path == "/api/automation/schema":
                    return self._send(200, {"ok": True})
                if path == "/api/automation/state":
                    return self._send(200, {"ok": True, "tasks": []})
                return self._send(404, {"ok": False, "code": "not_found",
                                        "retryable": False, "msg": "no route"})

            def do_POST(self):
                path = urlsplit(self.path).path
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                if path != "/api/automation/tool":
                    return self._send(404, {"ok": False, "code": "not_found",
                                            "retryable": False, "msg": "no route"})
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = {}
                name = body.get("name")
                if name == "cft_get_state":
                    return self._send(200, {
                        "ok": True, "tool": name,
                        "data": {"version": "2.7.5", "tasks": [], "blockers": []},
                        "content": [{"type": "text", "text": "state ok"}],
                        "next_actions": [], "meta": {"ms": 1},
                    })
                if name == "cft_download_model":
                    time.sleep(3.0)
                    return self._send(200, {
                        "ok": True, "tool": name,
                        "data": {"downloaded": True},
                        "content": [{"type": "text", "text": "download done"}],
                        "next_actions": [], "meta": {"ms": 3000},
                    })
                return self._send(404, {"ok": False, "code": "not_found",
                                        "retryable": False, "msg": "unknown tool"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self.httpd.server_port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
            self.thread.join(timeout=5)


@pytest.fixture()
def fake_bridge():
    bridge = FakeBridge()
    try:
        yield bridge
    finally:
        bridge.close()


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _start_server(bridge_url: str, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(
        [sys.executable, str(SERVER), "--http", "--port", str(port),
         "--token", TOKEN, "--bridge", bridge_url, "--toolsets", "core"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_ready(proc: subprocess.Popen, port: int, timeout: float = 25.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate(timeout=5)
            raise AssertionError(
                "mcp_server exited early (%s)\nstdout=%s\nstderr=%s"
                % (proc.returncode, out.decode("utf-8", "replace"),
                   err.decode("utf-8", "replace")))
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", "/mcp")
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            if resp.status == 401:
                return
            last_error = "unexpected status %s: %s" % (
                resp.status, body.decode("utf-8", "replace")[:200])
        except OSError as exc:
            last_error = str(exc)
        time.sleep(0.1)
    raise AssertionError("mcp_server HTTP not ready: %s" % last_error)


def _stop_server(proc: subprocess.Popen):
    if proc.poll() is None:
        proc.terminate()
    try:
        out, err = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate(timeout=5)
    return out, err


def _bare_request(port: int, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(
            "POST", "/mcp",
            body=json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": "bare", "version": "1"}},
            }),
            headers=headers or {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        resp = conn.getresponse()
        return resp.status, resp.read(), dict(resp.getheaders())
    finally:
        conn.close()


async def _http_client_roundtrip(port: int) -> None:
    url = "http://127.0.0.1:%d/mcp" % port
    async with streamablehttp_client(
        url,
        headers={"Authorization": "Bearer %s" % TOKEN},
        timeout=10,
        sse_read_timeout=30,
    ) as (read_stream, write_stream, _get_session_id):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            assert init.serverInfo.name == C.MCP_SERVER_NAME
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            expected = {tool["name"] for tool in C.tools_for_toolsets(["core"])}
            assert names == expected
            assert "cft_view_gallery" not in names  # vision disabled

            result = await session.call_tool("cft_get_state", {"compact": True})
            assert result.isError is False
            assert result.structuredContent["ok"] is True

            guide = await session.read_resource(AnyUrl("cft://guide/text"))
            assert "CivitaiFreeTool" in guide.contents[0].text

            progress = []

            async def on_progress(value, total, message):
                progress.append((value, total, message))

            downloaded = await session.call_tool(
                "cft_download_model", {"url": "https://civitai.com/models/1"},
                progress_callback=on_progress)
            assert downloaded.isError is False
            assert progress, "expected progress over the POST SSE stream"


async def _http_roundtrip_with_deadline(port: int) -> None:
    with anyio.fail_after(60):
        await _http_client_roundtrip(port)


def test_http_transport_and_auth(fake_bridge):
    port = _free_port()
    proc = _start_server(fake_bridge.url, port)
    try:
        _wait_ready(proc, port)

        # No token -> 401 JSON, before MCP processing.
        status, body, headers = _bare_request(port)
        assert status == 401
        payload = json.loads(body.decode("utf-8"))
        assert payload["ok"] is False
        assert payload["code"] == "unauthorized"
        lower_headers = {key.lower(): value for key, value in headers.items()}
        assert lower_headers.get("www-authenticate") == "Bearer"

        # Wrong token -> 401 as well.
        status, body, _ = _bare_request(port, headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer wrong-token",
        })
        assert status == 401

        anyio.run(_http_roundtrip_with_deadline, port)
    finally:
        out, err = _stop_server(proc)
        # HTTP mode must not write protocol data to stdout.
        assert out.strip() == b"", out.decode("utf-8", "replace")[:500]
        assert b"Traceback" not in err, err.decode("utf-8", "replace")[-2000:]
