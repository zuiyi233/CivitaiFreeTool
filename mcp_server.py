# -*- coding: utf-8 -*-
"""CivitaiFreeTool MCP sidecar.

A thin MCP (stdio / Streamable HTTP) adapter over the local HTTP bridge at
127.0.0.1:47531.  It never imports webui/automation_api and never starts the GUI
unless ``--launch-app`` is given explicitly.

stdout is reserved for MCP JSON-RPC when serving over stdio; every diagnostic
goes to stderr.
"""
from __future__ import annotations

import argparse
import dataclasses
import hmac
import importlib.metadata
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import anyio
import jsonschema
from anyio import to_thread
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents

import ai_contract as C

LOG = logging.getLogger("cft.mcp")


# ---------------------------------------------------------------------------
# Logging / small helpers
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    level = os.environ.get("CFT_MCP_LOG_LEVEL", "WARNING").upper()
    root = logging.getLogger()
    # stdout belongs to MCP JSON-RPC in stdio mode: never let a logger use it.
    for handler in list(root.handlers):
        if getattr(handler, "stream", None) is sys.stdout:
            root.removeHandler(handler)
    if not root.handlers:
        logging.basicConfig(
            level=getattr(logging, level, logging.WARNING),
            stream=sys.stderr,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    else:
        root.setLevel(getattr(logging, level, logging.WARNING))


def _stderr(message: str) -> None:
    try:
        print(message, file=sys.stderr, flush=True)
    except Exception:
        pass


def _version_tuple(value: Any):
    """Best-effort numeric version parsing: 'v2.7.0-beta' -> (2, 7, 0)."""
    text = str(value or "").strip().lower().lstrip("v")
    if not text:
        return None
    numbers = []
    current = ""
    for ch in text:
        if ch.isdigit():
            current += ch
        elif current:
            numbers.append(int(current))
            current = ""
            if len(numbers) >= 4:
                break
    if current and len(numbers) < 4:
        numbers.append(int(current))
    if not numbers:
        return None
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers[:3])


def _version_lt(current: Any, required: Any) -> bool:
    a, b = _version_tuple(current), _version_tuple(required)
    if a is None or b is None:
        return False
    return a < b


def _extract_version(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("version", "app_version", "appVersion"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("app", "application"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            value = _extract_version(nested)
            if value:
                return value
    return ""


def _extract_api_version(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("api_version", "apiVersion"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("contract", "mcp"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            value = _extract_api_version(nested)
            if value:
                return value
    return ""


def _app_not_running(bridge_base: str, detail: str = "") -> dict:
    msg = "CivitaiFreeTool is not reachable at %s" % bridge_base
    if detail:
        msg += " (%s)" % detail
    return {
        "ok": False,
        "code": "app_not_running",
        "retryable": True,
        "msg": msg,
        "hint": (
            "Start CivitaiFreeToolWeb.exe and wait for the UI, then retry. "
            "The bridge must listen on 127.0.0.1:47531; --launch-app can start it automatically."
        ),
        "next_actions": [],
    }


def _version_mismatch(msg: str, hint: str) -> dict:
    return {
        "ok": False,
        "code": "version_mismatch",
        "retryable": False,
        "msg": msg,
        "hint": hint,
        "next_actions": [],
    }


def _error_text(err: dict) -> str:
    code = str(err.get("code") or "error")
    msg = str(err.get("msg") or err.get("message") or "")
    lines = ["%s: %s" % (code, msg) if msg else code]
    hint = err.get("hint")
    if hint:
        lines.append("hint: %s" % hint)
    actions = err.get("next_actions")
    if actions:
        try:
            lines.append("next_actions: %s" % json.dumps(actions, ensure_ascii=False, default=str))
        except Exception:
            pass
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Local bridge client (stdlib only, no import of the app)
# ---------------------------------------------------------------------------

class BridgeUnavailable(Exception):
    pass


class BridgeTimeout(BridgeUnavailable):
    pass


class Bridge:
    def __init__(self, base_url: str):
        self.base = (base_url or C.DEFAULT_BRIDGE).rstrip("/")
        self._cache_lock = threading.Lock()
        self._cached_status = None
        self._cached_at = 0.0
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # -- HTTP ---------------------------------------------------------------
    def request(self, method: str, path: str, body: Any = None,
                timeout: float = 10.0):
        url = self.base + path
        data = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "%s/%s" % (C.MCP_SERVER_NAME, C.MCP_SERVER_VERSION),
        }
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                status = int(getattr(resp, "status", 0) or resp.getcode())
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                raw = exc.read()
            except Exception:
                raw = b""
        except TimeoutError as exc:
            raise BridgeTimeout(str(exc) or "request timed out") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise BridgeTimeout(str(reason)) from exc
            raise BridgeUnavailable(str(reason) or "connection failed") from exc
        except OSError as exc:
            raise BridgeUnavailable(str(exc) or "connection failed") from exc

        text = raw.decode("utf-8", "replace")
        try:
            payload = json.loads(text) if text.strip() else {}
        except ValueError:
            payload = {
                "ok": False,
                "code": "internal_error",
                "retryable": True,
                "msg": "Bridge returned non-JSON HTTP %d" % status,
                "raw": text[:300],
            }
        if not isinstance(payload, dict):
            payload = {"ok": True, "result": payload}
        return status, payload

    def call_tool(self, name: str, arguments: dict, timeout: float = 60.0):
        return self.request(
            "POST", "/api/automation/tool",
            {"name": name, "arguments": arguments or {}},
            timeout=timeout,
        )

    # -- App status ---------------------------------------------------------
    def invalidate(self) -> None:
        with self._cache_lock:
            self._cached_status = None
            self._cached_at = 0.0

    def check_app(self, force: bool = False):
        if not force:
            with self._cache_lock:
                cached = self._cached_status
                fresh = (time.monotonic() - self._cached_at) < 30.0
            if cached is not None and fresh:
                return cached

        try:
            health_status, health = self.request("GET", "/api/health", timeout=2.5)
        except BridgeUnavailable as exc:
            self.invalidate()
            return AppStatus(False, error=_app_not_running(self.base, str(exc)))

        if health_status != 200:
            err = C.normalize_error(
                health_status, health,
                hint="The HTTP bridge answered, but /api/health failed. Restart CivitaiFreeToolWeb.",
            )
            self.invalidate()
            return AppStatus(False, error=err)

        version = _extract_version(health)
        if version and _version_lt(version, C.MIN_APP_VERSION):
            self.invalidate()
            return AppStatus(False, version=version, health=health, error=_version_mismatch(
                "App version %s is older than the required %s" % (version, C.MIN_APP_VERSION),
                "Upgrade CivitaiFreeToolWeb to >= %s so /api/automation/tool and "
                "/api/automation/ai are available." % C.MIN_APP_VERSION,
            ))

        try:
            ai_status, ai = self.request("GET", "/api/automation/ai", timeout=2.5)
        except BridgeUnavailable as exc:
            self.invalidate()
            return AppStatus(False, version=version, health=health,
                             error=_app_not_running(self.base, str(exc)))

        if ai_status in (404, 405, 501):
            self.invalidate()
            return AppStatus(False, version=version, health=health, error=_version_mismatch(
                "The app does not expose /api/automation/ai",
                "Upgrade CivitaiFreeToolWeb to >= %s (the MCP sidecar needs the AI "
                "automation endpoints)." % C.MIN_APP_VERSION,
            ))
        if ai_status != 200:
            self.invalidate()
            err = C.normalize_error(ai_status, ai,
                                    hint="GET /api/automation/ai failed; check the app log.")
            return AppStatus(False, version=version, health=health, error=err)
        if ai.get("ok") is False:
            self.invalidate()
            err = C.normalize_error(ai_status, ai,
                                    hint="The app reported that AI automation is unavailable.")
            return AppStatus(False, version=version, health=health, error=err)

        api_version = _extract_api_version(ai)
        if api_version and _version_lt(api_version, C.API_VERSION):
            self.invalidate()
            return AppStatus(False, version=version, health=health, ai=ai, error=_version_mismatch(
                "App automation contract %s is older than required %s"
                % (api_version, C.API_VERSION),
                "Upgrade CivitaiFreeToolWeb to >= %s." % C.MIN_APP_VERSION,
            ))

        if not version:
            version = _extract_version(ai)
        status = AppStatus(True, version=version, health=health, ai=ai)
        with self._cache_lock:
            self._cached_status = status
            self._cached_at = time.monotonic()
        return status


@dataclasses.dataclass
class AppStatus:
    ok: bool
    version: str = ""
    health: dict | None = None
    ai: dict | None = None
    error: dict | None = None


class AppLauncher:
    """Optionally starts CivitaiFreeToolWeb.exe next to this executable."""

    LAUNCH_WAIT_S = 20.0

    def __init__(self, bridge: Bridge, enabled: bool):
        self.bridge = bridge
        self.enabled = bool(enabled)
        self._lock = threading.Lock()
        self._attempted = False

    def exe_path(self) -> Path:
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).resolve().parent
        else:
            base = Path(__file__).resolve().parent
        return base / "CivitaiFreeToolWeb.exe"

    def ensure(self) -> AppStatus:
        status = self.bridge.check_app()
        if status.ok or not self.enabled:
            return status
        code = (status.error or {}).get("code")
        if code != "app_not_running":
            return status

        with self._lock:
            if self._attempted:
                return status
            self._attempted = True

        exe = self.exe_path()
        if not exe.is_file():
            LOG.warning("--launch-app: %s not found", exe)
            err = dict(status.error or _app_not_running(self.bridge.base))
            err["hint"] = (str(err.get("hint") or "") +
                           " --launch-app was requested but CivitaiFreeToolWeb.exe was not "
                           "found next to the MCP executable.").strip()
            return AppStatus(False, error=err)

        try:
            kwargs = {}
            if os.name == "nt":
                # DETACHED_PROCESS: the app must outlive the MCP sidecar.
                kwargs["creationflags"] = 0x00000008
            subprocess.Popen(
                [str(exe)],
                cwd=str(exe.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                **kwargs,
            )
            _stderr("--launch-app: started %s" % exe)
        except Exception as exc:
            LOG.warning("--launch-app failed: %s", exc)
            err = dict(status.error or _app_not_running(self.bridge.base))
            err["hint"] = (str(err.get("hint") or "") +
                           " Failed to start CivitaiFreeToolWeb.exe: %s" % exc).strip()
            return AppStatus(False, error=err)

        deadline = time.monotonic() + self.LAUNCH_WAIT_S
        while time.monotonic() < deadline:
            time.sleep(0.5)
            status = self.bridge.check_app(force=True)
            if status.ok:
                _stderr("--launch-app: app is ready (version %s)" % (status.version or "?"))
                return status
        err = dict(status.error or _app_not_running(self.bridge.base))
        err["hint"] = (str(err.get("hint") or "") +
                       " CivitaiFreeToolWeb.exe was started but was not ready within %ds."
                       % int(self.LAUNCH_WAIT_S)).strip()
        return AppStatus(False, error=err)


# ---------------------------------------------------------------------------
# MCP value mapping
# ---------------------------------------------------------------------------

_B64_KEYS = {"b64", "base64", "b64_json", "image_b64", "data_b64"}


def _sanitize(value: Any, depth: int = 0) -> Any:
    """Drop bulky inline base64 from structuredContent (images live in content)."""
    if depth > 12:
        return "<max depth>"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in _B64_KEYS and isinstance(item, str) and len(item) > 512:
                out[key] = "<omitted %d base64 chars; see content[]>" % len(item)
            else:
                out[key] = _sanitize(item, depth + 1)
        return out
    if isinstance(value, list):
        return [_sanitize(item, depth + 1) for item in value]
    return value


def _content_blocks(payload: dict) -> list:
    blocks = []
    raw = payload.get("content")
    if isinstance(raw, str) and raw:
        raw = [{"type": "text", "text": raw}]
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                blocks.append(types.TextContent(type="text", text=item))
                continue
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "").lower()
            if kind == "text":
                text = item.get("text")
                if text is None:
                    text = json.dumps(item, ensure_ascii=False, default=str)
                blocks.append(types.TextContent(type="text", text=str(text)))
            elif kind == "image":
                data = item.get("b64") or item.get("data") or item.get("base64")
                if isinstance(data, str) and data:
                    mime = item.get("mime") or item.get("mimeType") or "image/jpeg"
                    blocks.append(types.ImageContent(
                        type="image", data=data, mimeType=str(mime)))
                else:
                    blocks.append(types.TextContent(
                        type="text",
                        text="[image content omitted: no inline data; use delivery=\"paths\"]",
                    ))
            else:
                blocks.append(types.TextContent(
                    type="text",
                    text=json.dumps(item, ensure_ascii=False, default=str),
                ))
    if not blocks:
        data = payload.get("data")
        text = payload.get("msg")
        if not text:
            try:
                text = json.dumps(data if data is not None else payload,
                                  ensure_ascii=False, default=str)
            except Exception:
                text = str(data)
        blocks.append(types.TextContent(type="text", text=str(text)))
    return blocks


def _success_result(name: str, payload: dict) -> types.CallToolResult:
    data = payload.get("data")
    if data is None:
        data = {}
    elif not isinstance(data, dict):
        data = {"result": data}
    next_actions = payload.get("next_actions")
    if not isinstance(next_actions, list):
        next_actions = []
    meta = payload.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    structured = {
        "ok": True,
        "tool": str(payload.get("tool") or name),
        "data": _sanitize(data),
        "next_actions": _sanitize(next_actions),
        "meta": _sanitize(meta),
    }
    return types.CallToolResult(
        content=_content_blocks(payload),
        structuredContent=structured,
        isError=False,
    )


def _error_result(err: dict) -> types.CallToolResult:
    body = dict(err) if isinstance(err, dict) else {"ok": False, "msg": str(err)}
    body["ok"] = False
    if "retryable" not in body:
        body["retryable"] = bool(body.get("code") in ("app_not_running", "timeout", "network_error"))
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=_error_text(body))],
        structuredContent=body,
        isError=True,
    )


def _tool_timeout(name: str, arguments: dict) -> float:
    waits = name in ("cft_download_model", "cft_manage_library",
                     "cft_list_tasks", "cft_repair_model")
    if waits:
        try:
            budget = int(arguments.get("timeout_s") or C.LIMITS.get("default_wait_s", 300))
        except (TypeError, ValueError):
            budget = C.LIMITS.get("default_wait_s", 300)
        budget = max(1, min(budget, int(C.LIMITS.get("max_wait_s", 1800))))
        return float(budget + 30)
    if name in ("cft_view_model", "cft_view_gallery", "cft_view_workflow",
                "cft_contact_sheet", "cft_inspect_image"):
        return 120.0
    return 60.0


def _will_wait(name: str, arguments: dict) -> bool:
    if name in ("cft_download_model", "cft_manage_library"):
        return arguments.get("wait", True) is not False
    if name == "cft_list_tasks":
        return bool(arguments.get("wait"))
    return False


# ---------------------------------------------------------------------------
# Progress polling for long-running tools
# ---------------------------------------------------------------------------

_TERMINAL = {"done", "error", "canceled"}
_ACTIVE = {"pending", "downloading", "paused"}


def _tasks_from_payload(payload: Any) -> list:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("tasks"), list):
        return data["tasks"]
    if isinstance(payload.get("tasks"), list):
        return payload["tasks"]
    return []


def _progress_snapshot(bridge: Bridge, name: str, arguments: dict,
                       elapsed: float, budget: float):
    try:
        if name == "cft_list_tasks":
            ids = [str(item) for item in (arguments.get("ids") or [])]
            query = {"wait": False, "limit": max(1, min(len(ids) or 20, 100))}
            if ids:
                query["ids"] = ids
            status, payload = bridge.call_tool("cft_list_tasks", query, timeout=10)
            rows = _tasks_from_payload(payload)
            if ids:
                wanted = set(ids)
                rows = [r for r in rows if str(r.get("id")) in wanted]
            if not rows and ids:
                return elapsed, budget, "waiting for %d task(s)" % len(ids)
            terminal = sum(1 for r in rows if str(r.get("status")) in _TERMINAL)
            total = max(len(ids) or len(rows), terminal, 1)
            return float(terminal), float(total), "%d/%d task(s) terminal" % (terminal, total)

        if name == "cft_download_model":
            status, payload = bridge.call_tool("cft_list_tasks", {"limit": 20}, timeout=10)
            rows = _tasks_from_payload(payload)
            active = sum(1 for r in rows if str(r.get("status")) in _ACTIVE)
            done = sum(1 for r in rows if str(r.get("status")) in _TERMINAL)
            if rows:
                return float(done), float(max(len(rows), 1)), (
                    "download: %d active, %d terminal (recent tasks)" % (active, done))
            return elapsed, budget, "waiting for the download to appear in the task list"

        if name == "cft_manage_library":
            status, payload = bridge.call_tool("cft_get_state", {"compact": True}, timeout=10)
            rows = _tasks_from_payload(payload)
            active = sum(1 for r in rows if str(r.get("status")) in _ACTIVE)
            message = "library operation running"
            if active:
                message += " (%d active task(s))" % active
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            blockers = data.get("blockers") if isinstance(data, dict) else None
            if blockers:
                message += "; blockers: %s" % json.dumps(blockers, ensure_ascii=False)[:160]
            return elapsed, budget, message
    except Exception:
        pass
    return elapsed, budget, "waiting for the app"


async def _progress_loop(bridge: Bridge, name: str, arguments: dict,
                         session, token, request_id, budget: float) -> None:
    started = time.monotonic()
    try:
        await session.send_progress_notification(
            token, 0.0, max(1.0, budget), "started",
            related_request_id=request_id,
        )
    except Exception:
        return

    while True:
        try:
            await anyio.sleep(2.0)
        except BaseException:
            return
        elapsed = time.monotonic() - started
        try:
            progress, total, message = await to_thread.run_sync(
                lambda: _progress_snapshot(bridge, name, arguments, elapsed, budget),
                abandon_on_cancel=True,
            )
        except BaseException:
            raise
        try:
            await session.send_progress_notification(
                token, float(progress), float(total) if total else None, message,
                related_request_id=request_id,
            )
        except Exception:
            return


# ---------------------------------------------------------------------------
# MCP server construction
# ---------------------------------------------------------------------------

def _public_tools(enabled: list[dict]) -> list[types.Tool]:
    out = []
    for item in enabled:
        out.append(types.Tool(
            name=item["name"],
            title=item.get("title"),
            description=item.get("description"),
            inputSchema=item["inputSchema"],
            annotations=types.ToolAnnotations(**item.get("annotations", {})),
        ))
    return out


def _public_resources() -> list[types.Resource]:
    out = []
    for item in C.RESOURCES:
        out.append(types.Resource(
            uri=item["uri"],
            name=item["name"],
            description=item.get("description"),
            mimeType=item.get("mimeType"),
        ))
    return out


def _public_prompts() -> list[types.Prompt]:
    out = []
    for item in C.PROMPTS:
        args = []
        for arg in item.get("arguments") or []:
            args.append(types.PromptArgument(
                name=arg["name"],
                description=arg.get("description"),
                required=bool(arg.get("required")),
            ))
        out.append(types.Prompt(name=item["name"], title=item.get("title"),
                                description=item.get("description"), arguments=args))
    return out


def _prompt_default(name: str) -> str:
    return {
        "dest_subdir": "(not specified)",
        "type": "(any)",
        "filter": "(not specified)",
    }.get(name, "(not specified)")


class _PromptValues(dict):
    def __missing__(self, key):  # keep unknown placeholders visible
        return "{%s}" % key


def _json_resource(payload: Any) -> list[ReadResourceContents]:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return [ReadResourceContents(content=text, mime_type="application/json")]


def _build_server(bridge: Bridge, launcher: AppLauncher,
                  enabled: list[dict]) -> Server:
    enabled_names = {item["name"] for item in enabled}

    server = Server(
        C.MCP_SERVER_NAME,
        version=C.MCP_SERVER_VERSION,
        instructions=(
            "Local CivitaiFreeTool control plane. Read cft://guide/text (or cft://guide/vision) "
            "and call cft_capabilities before multi-step work. All tool execution is proxied to "
            "the desktop app over 127.0.0.1:47531; cft://state gives a compact status snapshot."
        ),
    )

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        return _public_tools(enabled)

    @server.list_resources()
    async def handle_list_resources() -> list[types.Resource]:
        return _public_resources()

    @server.list_prompts()
    async def handle_list_prompts() -> list[types.Prompt]:
        return _public_prompts()

    @server.get_prompt()
    async def handle_get_prompt(name: str, arguments: dict | None):
        prompt = next((p for p in C.PROMPTS if p["name"] == name), None)
        if prompt is None:
            raise ValueError("Unknown prompt: %s" % name)
        arguments = arguments or {}
        values = {}
        missing = []
        for arg in prompt.get("arguments") or []:
            value = arguments.get(arg["name"])
            if value is None or str(value).strip() == "":
                if arg.get("required"):
                    missing.append(arg["name"])
                value = _prompt_default(arg["name"])
            values[arg["name"]] = str(value)
        if missing:
            raise ValueError("Missing required prompt argument(s): %s" % ", ".join(missing))
        text = prompt["template"].format_map(_PromptValues(values))
        return types.GetPromptResult(
            description=prompt.get("description"),
            messages=[types.PromptMessage(
                role="user",
                content=types.TextContent(type="text", text=text),
            )],
        )

    @server.read_resource()
    async def handle_read_resource(uri):
        target = str(uri)
        if target == "cft://guide/text":
            return [ReadResourceContents(content=C.TEXT_GUIDE, mime_type="text/markdown")]
        if target == "cft://guide/vision":
            return [ReadResourceContents(content=C.VISION_GUIDE, mime_type="text/markdown")]

        if target == "cft://tools":
            payload = {
                "ok": True,
                "tools": [C.public_tool(item, format="mcp") for item in enabled],
                "count": len(enabled),
                "toolsets": sorted({item["toolset"] for item in enabled}),
            }
            return _json_resource(payload)

        if target not in ("cft://capabilities", "cft://schema", "cft://state"):
            raise ValueError("Unknown resource URI: %s" % target)

        status = await to_thread.run_sync(launcher.ensure, abandon_on_cancel=True)
        if not status.ok:
            return _json_resource(status.error or _app_not_running(bridge.base))

        try:
            if target == "cft://capabilities":
                code, payload = await to_thread.run_sync(
                    lambda: bridge.request("GET", "/api/automation/ai", timeout=10),
                    abandon_on_cancel=True,
                )
                if code != 200:
                    payload = C.normalize_error(
                        code, payload, hint="GET /api/automation/ai failed.")
                return _json_resource(payload)

            if target == "cft://schema":
                code, payload = await to_thread.run_sync(
                    lambda: bridge.request("GET", "/api/automation/schema", timeout=10),
                    abandon_on_cancel=True,
                )
                if code != 200:
                    payload = C.normalize_error(
                        code, payload, hint="GET /api/automation/schema failed; upgrade the app.")
                return _json_resource(payload)

            # cft://state: prefer POST /tool, fall back to the legacy GET endpoint.
            code, payload = await to_thread.run_sync(
                lambda: bridge.call_tool("cft_get_state", {"compact": True}, timeout=15),
                abandon_on_cancel=True,
            )
            if code == 200 and payload.get("ok") is not False:
                data = payload.get("data")
                return _json_resource(data if isinstance(data, dict) else payload)
            fallback_code, fallback = await to_thread.run_sync(
                lambda: bridge.request("GET", "/api/automation/state?compact=1", timeout=15),
                abandon_on_cancel=True,
            )
            if fallback_code == 200 and fallback.get("ok") is not False:
                return _json_resource(fallback)
            err = C.normalize_error(
                code, payload,
                hint="Start the app and retry; the MCP sidecar could not read app state.",
            )
            return _json_resource(err)
        except BridgeTimeout as exc:
            return _json_resource({
                "ok": False, "code": "timeout", "retryable": True,
                "msg": "The app did not respond in time: %s" % exc,
                "hint": "Retry, or check the app with cft_diagnostics.",
            })
        except BridgeUnavailable as exc:
            bridge.invalidate()
            return _json_resource(_app_not_running(bridge.base, str(exc)))

    @server.call_tool(validate_input=False)
    async def handle_call_tool(name: str, arguments: dict | None):
        arguments = arguments if isinstance(arguments, dict) else {}

        if name not in C.TOOLS:
            return _error_result({
                "ok": False, "code": "not_found", "retryable": False,
                "msg": "Unknown tool: %s" % name,
                "hint": "Call tools/list to see the tools enabled for this MCP server.",
            })
        if name not in enabled_names:
            toolset = C.TOOLS[name].get("toolset") or "?"
            return _error_result({
                "ok": False, "code": "permission_denied", "retryable": False,
                "msg": "Tool %s is disabled (toolset: %s)" % (name, toolset),
                "hint": "Restart the MCP server with --toolsets including '%s'." % toolset,
            })

        # Validate ourselves instead of letting the SDK do it: the SDK's
        # validation error is a text-only CallToolResult without structuredContent.
        schema = C.TOOLS[name].get("inputSchema") or {}
        try:
            jsonschema.validate(instance=arguments, schema=schema)
        except jsonschema.ValidationError as exc:
            path = ".".join(str(part) for part in exc.absolute_path) or "arguments"
            return _error_result({
                "ok": False, "code": "invalid_argument", "retryable": False,
                "msg": "Invalid arguments for %s at %s: %s" % (name, path, exc.message),
                "hint": "Use the inputSchema from tools/list.",
            })
        except jsonschema.SchemaError:
            # A broken contract schema is a server bug, not a caller error.
            pass

        try:
            status = await to_thread.run_sync(launcher.ensure, abandon_on_cancel=True)
            if not status.ok:
                return _error_result(status.error or _app_not_running(bridge.base))

            timeout = _tool_timeout(name, arguments)
            ctx = server.request_context
            token = None
            if ctx.meta is not None:
                token = getattr(ctx.meta, "progressToken", None)

            def invoke():
                return bridge.call_tool(name, arguments, timeout=timeout)

            if token is None or not _will_wait(name, arguments):
                code, payload = await to_thread.run_sync(invoke, abandon_on_cancel=True)
            else:
                async with anyio.create_task_group() as tg:
                    tg.start_soon(
                        _progress_loop, bridge, name, arguments, ctx.session,
                        token, ctx.request_id, timeout,
                    )
                    try:
                        code, payload = await to_thread.run_sync(invoke, abandon_on_cancel=True)
                    finally:
                        tg.cancel_scope.cancel()

            if code == 200 and payload.get("ok") is not False:
                return _success_result(name, payload)
            if code == 200:
                err = C.normalize_error(code, payload)
            else:
                err = C.normalize_error(code, payload)
            if err.get("code") == "app_not_running":
                bridge.invalidate()
            return _error_result(err)
        except BridgeTimeout as exc:
            return _error_result({
                "ok": False, "code": "timeout", "retryable": True,
                "msg": "The app did not answer %s within %.0fs: %s" % (name, timeout, exc),
                "hint": "The operation may still be running. Poll cft_list_tasks, "
                        "or narrow the wait budget with timeout_s.",
            })
        except BridgeUnavailable as exc:
            bridge.invalidate()
            return _error_result(_app_not_running(bridge.base, str(exc)))
        except Exception as exc:
            LOG.exception("tool %s failed", name)
            return _error_result({
                "ok": False, "code": "internal_error", "retryable": True,
                "msg": "MCP sidecar failed while calling %s: %s" % (name, str(exc)[:240]),
                "hint": "Retry; if it persists, check the MCP server stderr log.",
            })

    return server


# ---------------------------------------------------------------------------
# Transports / CLI
# ---------------------------------------------------------------------------

import mcp_paths

# Backwards-compatible aliases (tests and older callers referenced the private names).
_TOKEN_FILE = mcp_paths.TOKEN_FILE


def _token_path() -> Path:
    return mcp_paths.token_path()



def _write_token(path: Path, token: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(token + "\n", encoding="utf-8")
        os.replace(tmp, path)
        if os.name == "posix":
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        return True
    except OSError as exc:
        LOG.warning("could not write MCP token file %s: %s", path, exc)
        return False


def _resolve_token(explicit: str | None):
    token = (explicit or "").strip()
    source = "arg"
    path = None
    if not token:
        token = (os.environ.get("CFT_MCP_TOKEN") or "").strip()
        source = "env"
    if not token:
        path = _token_path()
        try:
            saved = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
        except OSError:
            saved = ""
        if saved:
            token = saved
            source = "file"
        else:
            token = secrets.token_urlsafe(32)
            source = "generated"
            if _write_token(path, token):
                _stderr("Generated MCP HTTP bearer token and saved it to %s" % path)
            else:
                _stderr("Generated MCP HTTP bearer token (could not persist it)")
    if source != "arg":
        # Never echo the secret: manual starts print the token file path instead.
        _stderr("MCP HTTP bearer token loaded (%s); source file: %s"
                % (source, path or "<not persisted>"))
    return token, path, source


class _BearerAuthMiddleware:
    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        from starlette.datastructures import Headers
        from starlette.responses import JSONResponse

        header = Headers(scope=scope).get("authorization", "")
        scheme, _, supplied = header.partition(" ")
        supplied = supplied.strip()
        ok = bool(supplied) and scheme.lower() == "bearer" and hmac.compare_digest(
            supplied.encode("utf-8"), self.token.encode("utf-8"))
        if not ok:
            response = JSONResponse(
                {
                    "ok": False,
                    "code": "unauthorized",
                    "retryable": False,
                    "msg": "Missing or invalid bearer token",
                    "hint": "Send Authorization: Bearer <token>.",
                },
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class _MCPEndpoint:
    def __init__(self, manager):
        self.manager = manager

    async def __call__(self, scope, receive, send):
        await self.manager.handle_request(scope, receive, send)


def _build_http_app(bridge: Bridge, launcher: AppLauncher,
                    enabled: list[dict], token: str):
    import contextlib as _contextlib
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Route

    server = _build_server(bridge, launcher, enabled)
    manager = StreamableHTTPSessionManager(
        server,
        json_response=False,
        stateless=False,
        session_idle_timeout=1800.0,
    )

    @_contextlib.asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    app = Starlette(routes=[Route("/mcp", endpoint=_MCPEndpoint(manager))],
                    lifespan=lifespan)
    app.add_middleware(_BearerAuthMiddleware, token=token)
    return app


def _watch_bridge(bridge_base: str, interval: float = 10.0, failures: int = 3) -> None:
    """Exit when the desktop bridge has been unreachable for a while.

    Auto-started sidecars must not outlive the app: if the app crashes, the
    next start would otherwise find 47532 occupied by a stale process.
    """
    url = (bridge_base or C.DEFAULT_BRIDGE).rstrip("/") + "/api/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def work():
        bad = 0
        while True:
            time.sleep(interval)
            try:
                with opener.open(url, timeout=3) as resp:
                    if int(getattr(resp, "status", 0) or resp.getcode()) == 200:
                        bad = 0
                        continue
            except (OSError, urllib.error.URLError):
                pass
            bad += 1
            if bad >= failures:
                _stderr("bridge unreachable for %ds; exiting MCP HTTP sidecar"
                        % int(interval * bad))
                os._exit(0)

    threading.Thread(target=work, daemon=True, name="cft-bridge-watch").start()


def _run_http(bridge: Bridge, launcher: AppLauncher, enabled: list[dict],
              port: int, token: str, exit_with_bridge: bool = False) -> int:
    try:
        import uvicorn
    except ImportError as exc:
        _stderr("Streamable HTTP requires uvicorn/starlette: %s" % exc)
        return 2

    if exit_with_bridge:
        _watch_bridge(bridge.base)
    app = _build_http_app(bridge, launcher, enabled, token)
    _stderr("MCP Streamable HTTP listening on http://127.0.0.1:%d/mcp" % port)
    try:
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=int(port),
            log_level="warning",
            access_log=False,
            lifespan="on",
            timeout_graceful_shutdown=3,
        )
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        _stderr("MCP HTTP server failed to bind 127.0.0.1:%d: %s" % (port, exc))
        return 2
    return 0


async def _run_stdio(server: Server) -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


def _parse_toolsets(value: str, parser: argparse.ArgumentParser) -> list[str]:
    parts = [part.strip().lower() for part in str(value or "").split(",") if part.strip()]
    if not parts:
        parts = ["core"]
    if "all" in parts or "full" in parts:
        return ["core", "vision", "admin"]
    unknown = [part for part in parts if part not in C.TOOLSETS]
    if unknown:
        parser.error("unknown toolset(s): %s (choose from %s or 'all')"
                     % (", ".join(unknown), ", ".join(C.TOOLSETS)))
    return [name for name in ("core", "vision", "admin") if name in parts]


def _mcp_sdk_version() -> str:
    try:
        return importlib.metadata.version("mcp")
    except Exception:
        return "unknown"


def _selftest(args, enabled: list[dict], bridge: Bridge, launcher: AppLauncher) -> int:
    print("%s %s" % (C.MCP_SERVER_NAME, C.MCP_SERVER_VERSION))
    print("contract API %s, min app %s, MCP SDK %s"
          % (C.API_VERSION, C.MIN_APP_VERSION, _mcp_sdk_version()))
    print("bridge: %s" % bridge.base)
    print("toolsets: %s (%d tools)"
          % (",".join(sorted({t["toolset"] for t in enabled})), len(enabled)))
    print("resources: %d, prompts: %d" % (len(C.RESOURCES), len(C.PROMPTS)))
    print("tools: %s" % ", ".join(t["name"] for t in enabled))

    status = launcher.ensure() if args.launch_app else bridge.check_app()
    if status.ok:
        print("app: ok version=%s" % (status.version or "unknown"))
        ai = status.ai or {}
        if ai:
            print("app ai: %s" % json.dumps({
                "api_version": ai.get("api_version") or ai.get("apiVersion"),
                "mcp": ai.get("mcp"),
            }, ensure_ascii=False, default=str))
        return 0
    print("app: NOT READY")
    print(json.dumps(status.error or {}, ensure_ascii=False, indent=2, default=str))
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="CivitaiFreeToolMCP",
        description="CivitaiFreeTool MCP sidecar (stdio by default, optional Streamable HTTP).",
    )
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument("--stdio", action="store_true",
                           help="Serve MCP over stdio (default).")
    transport.add_argument("--http", action="store_true",
                           help="Serve MCP Streamable HTTP on 127.0.0.1.")
    parser.add_argument("--port", type=int, default=C.DEFAULT_MCP_HTTP_PORT,
                        help="HTTP port (default %d)." % C.DEFAULT_MCP_HTTP_PORT)
    parser.add_argument("--token", default=None,
                        help="HTTP bearer token; defaults to CFT_MCP_TOKEN or a generated file token.")
    parser.add_argument("--bridge",
                        default=os.environ.get("CFT_BRIDGE_URL") or C.DEFAULT_BRIDGE,
                        help="Local app bridge URL (default $CFT_BRIDGE_URL or %s)."
                             % C.DEFAULT_BRIDGE)
    parser.add_argument("--toolsets", default="core",
                        help="Comma-separated toolsets: core,vision,admin (default core).")
    parser.add_argument("--exit-with-bridge", action="store_true",
                        help="HTTP mode only: exit when the desktop bridge stays unreachable "
                             "(used by the auto-started sidecar).")
    parser.add_argument("--launch-app", action="store_true",
                        help="If the app is unreachable, start CivitaiFreeToolWeb.exe next to this program.")
    parser.add_argument("--selftest", action="store_true",
                        help="Check the contract, MCP SDK and app bridge, then exit.")
    parser.add_argument("--version", action="version",
                        version="%s %s (contract %s, min app %s)"
                                % (C.MCP_SERVER_NAME, C.MCP_SERVER_VERSION,
                                   C.API_VERSION, C.MIN_APP_VERSION))
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _setup_logging()

    if not (0 < int(args.port) < 65536):
        parser.error("--port must be between 1 and 65535")

    toolsets = _parse_toolsets(args.toolsets, parser)
    enabled = C.tools_for_toolsets(toolsets)
    bridge = Bridge(args.bridge)
    launcher = AppLauncher(bridge, args.launch_app)

    if args.selftest:
        return _selftest(args, enabled, bridge, launcher)

    server = _build_server(bridge, launcher, enabled)

    if args.launch_app:
        threading.Thread(
            target=launcher.ensure, name="cft-app-launch", daemon=True,
        ).start()

    if args.http:
        token, path, source = _resolve_token(args.token)
        if path is not None:
            _stderr("MCP HTTP token file: %s" % path)
        elif source == "arg":
            _stderr("MCP HTTP bearer token: supplied on the command line")
        return _run_http(bridge, launcher, enabled, args.port, token,
                         exit_with_bridge=bool(args.exit_with_bridge))

    _stderr("%s %s | bridge=%s | toolsets=%s | tools=%d"
            % (C.MCP_SERVER_NAME, C.MCP_SERVER_VERSION, bridge.base,
               ",".join(toolsets), len(enabled)))
    try:
        anyio.run(_run_stdio, server)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        LOG.exception("stdio server failed")
        _stderr("MCP stdio server failed: %s" % exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
