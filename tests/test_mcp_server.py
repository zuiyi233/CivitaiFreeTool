# -*- coding: utf-8 -*-
"""End-to-end tests for mcp_server.py over stdio.

The fake bridge is a stdlib ThreadingHTTPServer; no real desktop app, no
network access, and nothing is written outside the temp test process.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import ImageContent, TextContent
from pydantic import AnyUrl

import ai_contract as C

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "mcp_server.py"

# 1x1 JPEG (valid enough for an MCP ImageContent; bytes are opaque to the SDK).
JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAP//////////////////////////////////////////"
    "////////////////////////////////////////////////////2wBDAf//////////////////"
    "////////////////////////////////////////////////////////////wAARCAABAAEDASIA"
    "AhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oA"
    "DAMBAAIQAxAAAAF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAA"
    "AAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEB"
    "PwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAA"
    "AAAAAP/aAAgBAQABPyF//9oADAMBAAIAAwAAABD/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oA"
    "CAEDAQE/EH//xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAECAQE/EH//xAAUEAEAAAAAAAAA"
    "AAAAAAAAAAAA/9oACAEBAAE/EH//2Q=="
)


class FakeBridge:
    def __init__(self):
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep pytest output clean
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
                owner.requests.append(("GET", path, None))
                if path == "/api/health":
                    return self._send(200, {
                        "ok": True, "app": "CivitaiFreeTool", "version": "2.7.5",
                        "mode": "web",
                    })
                if path == "/api/automation/ai":
                    return self._send(200, {
                        "ok": True,
                        "api_version": C.API_VERSION,
                        "app_version": "2.7.5",
                        "toolsets": ["core", "vision", "admin"],
                        "limits": C.LIMITS,
                        "mcp": {"http_port": 47532},
                    })
                if path == "/api/automation/schema":
                    return self._send(200, {"ok": True, "task_states": ["done"], "routing": {}})
                if path == "/api/automation/state":
                    return self._send(200, {"ok": True, "tasks": [], "blockers": []})
                return self._send(404, {"ok": False, "code": "not_found",
                                        "retryable": False, "msg": "no route"})

            def do_POST(self):
                path = urlsplit(self.path).path
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = {}
                owner.requests.append(("POST", path, body))
                if path != "/api/automation/tool":
                    return self._send(404, {"ok": False, "code": "not_found",
                                            "retryable": False, "msg": "no route"})

                name = body.get("name")
                args = body.get("arguments") or {}
                if name == "cft_get_state":
                    return self._send(200, {
                        "ok": True, "tool": name,
                        "data": {"version": "2.7.5", "tasks": [], "blockers": []},
                        "content": [{"type": "text", "text": "state: no blockers"}],
                        "next_actions": [{"tool": "cft_capabilities", "arguments": {}}],
                        "meta": {"ms": 4},
                    })
                if name == "cft_capabilities":
                    return self._send(200, {
                        "ok": True, "tool": name,
                        "data": {"app": "CivitaiFreeTool", "version": "2.7.5"},
                        "content": [{"type": "text", "text": "capabilities ok"}],
                        "next_actions": [], "meta": {"ms": 2},
                    })
                if name == "cft_view_gallery":
                    return self._send(200, {
                        "ok": True, "tool": name,
                        "data": {
                            "items": [{"id": 12, "width": 768, "height": 768,
                                       "prompt": "a cat"}],
                            # deliberately duplicate base64 to verify that the
                            # sidecar strips it from structuredContent
                            "b64": JPEG_B64,
                        },
                        "content": [
                            {"type": "text", "text": "#12 a cat"},
                            {"type": "image", "mime": "image/jpeg", "b64": JPEG_B64,
                             "width": 768, "height": 768},
                        ],
                        "next_actions": [],
                        "meta": {"ms": 9},
                    })
                if name == "cft_search_models":
                    if args.get("query") == "invalid":
                        return self._send(400, {
                            "ok": False, "code": "invalid_argument", "retryable": False,
                            "msg": "query rejected by fake bridge",
                            "hint": "Use a different query.",
                            "next_actions": [{"tool": "cft_capabilities", "arguments": {}}],
                        })
                    return self._send(200, {
                        "ok": True, "tool": name,
                        "data": {"items": []},
                        "content": [{"type": "text", "text": "0 results"}],
                        "next_actions": [], "meta": {},
                    })
                if name == "cft_list_tasks":
                    return self._send(200, {
                        "ok": True, "tool": name,
                        "data": {"tasks": [], "count": 0},
                        "content": [{"type": "text", "text": "no tasks"}],
                        "next_actions": [], "meta": {},
                    })
                if name == "cft_download_model":
                    # Slow enough for the sidecar to poll and emit progress.
                    time.sleep(3.0)
                    return self._send(200, {
                        "ok": True, "tool": name,
                        "data": {"downloaded": True, "skipped": None},
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


async def _exercises(url: str) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER), "--stdio", "--bridge", url,
              "--toolsets", "core,vision"],
        cwd=str(ROOT),
        env=env,
    )

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            assert init.serverInfo.name == C.MCP_SERVER_NAME
            assert init.capabilities.tools is not None
            assert init.capabilities.resources is not None
            assert init.capabilities.prompts is not None

            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            expected = {tool["name"]
                        for tool in C.tools_for_toolsets(["core", "vision"])}
            assert names == expected
            assert "cft_view_gallery" in names
            assert "cft_configure" not in names  # admin stays off
            view = next(tool for tool in listed.tools if tool.name == "cft_view_gallery")
            assert view.annotations is not None
            assert view.annotations.readOnlyHint is True

            listed_resources = await session.list_resources()
            uris = {str(item.uri) for item in listed_resources.resources}
            assert "cft://guide/text" in uris
            assert "cft://capabilities" in uris

            listed_prompts = await session.list_prompts()
            assert {item.name for item in listed_prompts.prompts} >= {
                "cft_download", "cft_find_and_download"}

            # --- text tool -------------------------------------------------
            result = await session.call_tool("cft_get_state", {"compact": True})
            assert result.isError is False
            assert result.structuredContent["ok"] is True
            assert result.structuredContent["tool"] == "cft_get_state"
            assert result.structuredContent["data"]["version"] == "2.7.5"
            assert any(isinstance(block, TextContent) for block in result.content)
            assert result.structuredContent["next_actions"][0]["tool"] == "cft_capabilities"

            # --- image tool ------------------------------------------------
            result = await session.call_tool("cft_view_gallery", {"ids": [12]})
            assert result.isError is False
            images = [block for block in result.content if isinstance(block, ImageContent)]
            assert len(images) == 1
            assert images[0].mimeType == "image/jpeg"
            assert images[0].data == JPEG_B64
            data = result.structuredContent["data"]
            assert data["items"][0]["id"] == 12
            assert str(data.get("b64", "")).startswith("<omitted")
            assert JPEG_B64 not in json.dumps(result.structuredContent)

            # --- resources -------------------------------------------------
            guide = await session.read_resource(AnyUrl("cft://guide/text"))
            assert guide.contents
            assert "CivitaiFreeTool" in guide.contents[0].text
            caps = await session.read_resource(AnyUrl("cft://capabilities"))
            caps_payload = json.loads(caps.contents[0].text)
            assert caps_payload["ok"] is True
            assert caps_payload["api_version"] == C.API_VERSION
            tools_res = await session.read_resource(AnyUrl("cft://tools"))
            tools_payload = json.loads(tools_res.contents[0].text)
            assert tools_payload["count"] == len(expected)
            state_res = await session.read_resource(AnyUrl("cft://state"))
            state_payload = json.loads(state_res.contents[0].text)
            assert state_payload["version"] == "2.7.5"
            schema_res = await session.read_resource(AnyUrl("cft://schema"))
            schema_payload = json.loads(schema_res.contents[0].text)
            assert schema_payload["ok"] is True

            # --- prompts ---------------------------------------------------
            prompt = await session.get_prompt(
                "cft_download", {"url": "https://civitai.com/models/123"})
            assert prompt.messages
            assert prompt.messages[0].role == "user"
            text = prompt.messages[0].content.text
            assert "https://civitai.com/models/123" in text
            assert "(not specified)" in text  # optional dest_subdir default

            # --- tool-level error -----------------------------------------
            failure = await session.call_tool("cft_search_models", {"query": "invalid"})
            assert failure.isError is True
            assert failure.structuredContent["code"] == "invalid_argument"
            assert failure.structuredContent["retryable"] is False
            assert "invalid_argument" in failure.content[0].text
            assert failure.structuredContent["hint"] == "Use a different query."

            invalid = await session.call_tool("cft_get_state", {"task_limit": 999})
            assert invalid.isError is True
            assert invalid.structuredContent["code"] == "invalid_argument"
            assert "task_limit" in invalid.structuredContent["msg"]

            # --- long-task progress ---------------------------------------
            progress = []

            async def on_progress(value, total, message):
                progress.append((value, total, message))

            downloaded = await session.call_tool(
                "cft_download_model",
                {"url": "https://civitai.com/models/123"},
                progress_callback=on_progress,
            )
            assert downloaded.isError is False
            assert progress, "expected at least one progress notification"
            assert any((value or 0) > 0 or total for value, total, _ in progress)


def test_stdio_contract_roundtrip(fake_bridge):
    anyio.run(_run_with_deadline, fake_bridge.url)


async def _run_with_deadline(url: str) -> None:
    with anyio.fail_after(90):
        await _exercises(url)
