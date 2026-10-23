#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CivitaiFreeTool MCP doctor (stdlib only).

Checks, in order:
  1. GET <bridge>/api/health
  2. GET <bridge>/api/automation/ai
  3. optional --mcp <path>: run --version (or at least verify the file exists)
  4. optional --token-env: report whether the env var is set (never its value)

Exit code 0 when the bridge and AI endpoints are OK; 1 otherwise. It does not
read the app config, never prints API keys or tokens, and only talks to loopback.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

DEFAULT_BRIDGE = "http://127.0.0.1:47531"
DEFAULT_TOKEN_ENV = "CFT_MCP_TOKEN"
MIN_APP_VERSION = "2.7.0"
HTTP_MCP_PORT = 47532
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
MAX_BODY = 2 * 1024 * 1024


class Report:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0
        self.next_steps: list[str] = []

    def ok(self, label: str, message: str) -> None:
        print("[OK]   %s: %s" % (label, message))

    def info(self, label: str, message: str) -> None:
        print("[INFO] %s: %s" % (label, message))

    def skip(self, label: str, message: str) -> None:
        print("[SKIP] %s: %s" % (label, message))

    def warn(self, label: str, message: str, next_step: str | None = None) -> None:
        self.warnings += 1
        print("[WARN] %s: %s" % (label, message))
        if next_step:
            self._add_next(next_step)

    def fail(self, label: str, message: str, next_step: str | None = None) -> None:
        self.failures += 1
        print("[FAIL] %s: %s" % (label, message))
        if next_step:
            self._add_next(next_step)

    def _add_next(self, step: str) -> None:
        if step not in self.next_steps:
            self.next_steps.append(step)


def normalize_bridge(value: str) -> str:
    raw = (value or "").strip().rstrip("/")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("expected an http(s) URL, e.g. http://127.0.0.1:47531")
    if parts.hostname.lower() not in LOOPBACK_HOSTS:
        raise ValueError("refusing a non-loopback bridge; this skill is local-only")
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def fetch_json(url: str, timeout: float):
    """Return (status, payload, error). Local requests bypass system proxies."""
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "cft-doctor/1.0"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_BODY)
            status = response.status
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(MAX_BODY)
        except Exception:
            raw = b""
        status = exc.code
    except urllib.error.URLError as exc:
        return None, None, _error_text(exc.reason or exc)
    except (TimeoutError, OSError) as exc:
        return None, None, _error_text(exc)
    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        payload = None
    return status, payload, ""


def version_tuple(value: str):
    bits = []
    for chunk in re.split(r"[.\-+_]", str(value or "")):
        match = re.match(r"(\d+)", chunk)
        if match:
            bits.append(int(match.group(1)))
    return tuple(bits)


def version_lt(current: str, minimum: str) -> bool:
    left = version_tuple(current)
    right = version_tuple(minimum)
    if not left or not right:
        return False
    size = max(len(left), len(right))
    return left + (0,) * (size - len(left)) < right + (0,) * (size - len(right))


def _short(value, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")


def _error_text(reason) -> str:
    """Avoid localized Windows error text (and encoding noise) in the report."""
    if isinstance(reason, BaseException):
        winerror = getattr(reason, "winerror", None)
        errno = getattr(reason, "errno", None)
        if winerror == 10061 or errno in (61, 111):
            return "connection refused (app not running or port closed)"
        if winerror == 10060 or errno == 110:
            return "connection timed out"
        if winerror == 10054 or errno == 104:
            return "connection reset by peer"
        text = str(reason)
        if any(ord(char) > 127 for char in text):
            return reason.__class__.__name__
        return text or reason.__class__.__name__
    text = str(reason or "")
    if any(ord(char) > 127 for char in text):
        return "connection failed (localized OS error)"
    return text or "connection failed"


def _payload_message(payload) -> str:
    if isinstance(payload, dict):
        code = payload.get("code")
        msg = payload.get("msg") or payload.get("message") or payload.get("error")
        hint = payload.get("hint")
        bits = []
        if code:
            bits.append("code=%s" % code)
        if msg:
            bits.append(_short(msg))
        if hint:
            bits.append("hint: %s" % _short(hint, 160))
        return "; ".join(bits) or "unexpected response shape"
    return "unexpected response shape"


def check_health(bridge: str, timeout: float, report: Report) -> bool:
    url = bridge + "/api/health"
    status, payload, error = fetch_json(url, timeout)
    if error:
        report.fail(
            "/api/health",
            "cannot reach %s: %s" % (bridge, _short(error)),
            "start CivitaiFreeTool and retry; if the app uses another port, pass --bridge",
        )
        return False
    if status != 200 or not isinstance(payload, dict) or payload.get("ok") is not True:
        report.fail(
            "/api/health",
            "HTTP %s: %s" % (status, _payload_message(payload)),
            "check that 47531 is CivitaiFreeTool and the app is fully started",
        )
        return False
    app = str(payload.get("app") or "")
    version = str(payload.get("version") or "")
    if app != "CivitaiFreeTool":
        report.fail(
            "/api/health",
            "port answered by %r, not CivitaiFreeTool" % (app or "unknown app"),
            "stop the other process or pass the correct --bridge",
        )
        return False

    extras = []
    for key in ("mode", "window", "pid"):
        if payload.get(key) not in (None, ""):
            extras.append("%s=%s" % (key, payload[key]))
    suffix = " (%s)" % ", ".join(extras) if extras else ""
    report.ok("/api/health", "%s %s%s" % (app, version or "(unknown version)", suffix))

    if version and version_tuple(version):
        if version_lt(version, MIN_APP_VERSION):
            report.warn(
                "app version",
                "%s is older than %s" % (version, MIN_APP_VERSION),
                "upgrade CivitaiFreeTool to %s+ for /api/automation/ai and cft_* tools" % MIN_APP_VERSION,
            )
    return True


def check_ai(bridge: str, timeout: float, report: Report) -> bool:
    url = bridge + "/api/automation/ai"
    status, payload, error = fetch_json(url, timeout)
    if error:
        report.fail(
            "/api/automation/ai",
            "cannot reach %s: %s" % (url, _short(error)),
            "start the app and retry; the bridge may have stopped after /api/health",
        )
        return False
    if status == 404:
        report.fail(
            "/api/automation/ai",
            "HTTP 404 (AI automation endpoint is not available)",
            "upgrade CivitaiFreeTool to %s+; older builds only expose legacy /api/automation endpoints"
            % MIN_APP_VERSION,
        )
        return False
    if status != 200 or not isinstance(payload, dict):
        report.fail(
            "/api/automation/ai",
            "HTTP %s: %s" % (status, _payload_message(payload)),
            "read the error code/hint and retry after fixing it",
        )
        return False
    if payload.get("ok") is False:
        next_actions = payload.get("next_actions")
        step = "follow the response hint/next_actions"
        if isinstance(next_actions, list) and next_actions:
            step = "follow next_actions: %s" % _short(next_actions[0], 160)
        report.fail(
            "/api/automation/ai",
            _payload_message(payload),
            step,
        )
        return False

    toolsets = payload.get("toolsets")
    if isinstance(toolsets, dict):
        toolset_text = ",".join(str(key) for key in toolsets)
    elif isinstance(toolsets, list):
        toolset_text = ",".join(str(item) for item in toolsets)
    else:
        toolset_text = str(toolsets or "")

    tool_count = payload.get("tool_count")
    tools = payload.get("tools")
    if tool_count is None and isinstance(tools, list):
        tool_count = len(tools)
    if tool_count is None and isinstance(toolsets, dict):
        counts = [value.get("count") for value in toolsets.values() if isinstance(value, dict)]
        counts = [count for count in counts if isinstance(count, int)]
        if counts:
            tool_count = sum(counts)

    bits = []
    if toolset_text:
        bits.append("toolsets=%s" % toolset_text)
    if tool_count is not None:
        bits.append("tools=%s" % tool_count)
    if payload.get("api_version"):
        bits.append("api=%s" % payload["api_version"])
    if payload.get("min_app_version"):
        bits.append("min_app=%s" % payload["min_app_version"])
    mcp = payload.get("mcp")
    if isinstance(mcp, dict) and mcp.get("min_app_version"):
        bits.append("min_app=%s" % mcp["min_app_version"])
    caps = payload.get("caps")
    if isinstance(caps, dict):
        if caps.get("openapi"):
            bits.append("openapi=ok")
        if caps.get("sse"):
            bits.append("sse=ok/%d events" % len(caps.get("events") or []))
    report.ok("/api/automation/ai", ", ".join(bits) or "reachable")

    if isinstance(mcp, dict):
        port = mcp.get("http_port") or mcp.get("port")
        requires = mcp.get("requires_token")
        if requires is None:
            requires = mcp.get("auth_required")
        if port or requires:
            report.info(
                "HTTP MCP",
                "127.0.0.1:%s requires a Bearer token; use stdio if no token is configured"
                % (port or HTTP_MCP_PORT),
            )
    return True


def check_mcp(path_text: str, report: Report) -> None:
    path = Path(path_text).expanduser()
    if not path.exists():
        report.fail(
            "MCP binary",
            "not found: %s" % path,
            "install CivitaiFreeToolMCP.exe or pass the correct --mcp path",
        )
        return
    if path.is_dir():
        report.fail(
            "MCP binary",
            "is a directory: %s" % path,
            "pass the CivitaiFreeToolMCP.exe file, not its directory",
        )
        return
    report.ok("MCP binary", str(path))

    try:
        proc = subprocess.run(
            [str(path), "--version"],
            cwd=str(path.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.warn(
            "MCP --version",
            "could not run (%s); the file exists, continuing" % exc.__class__.__name__,
            "run the MCP binary manually with --version if setup still fails",
        )
        return

    output = (proc.stdout or proc.stderr or "").strip().splitlines()
    if proc.returncode == 0:
        report.ok("MCP --version", _short(output[0]) if output else "exit 0")
    else:
        report.warn(
            "MCP --version",
            "exit %s; the file exists, continuing" % proc.returncode,
            "confirm the sidecar supports --version (older builds may not)",
        )


def check_token(env_name: str, report: Report) -> None:
    if not env_name:
        report.skip("token", "no --token-env given")
        return
    value = os.environ.get(env_name)
    if value and value.strip():
        report.ok("token", "%s is set (value hidden)" % env_name)
    else:
        report.info(
            "token",
            "%s is not set; stdio does not need it, HTTP MCP on 127.0.0.1:%s uses --token, %s, "
            "or its local token file" % (env_name, HTTP_MCP_PORT, env_name),
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the local CivitaiFreeTool bridge and MCP sidecar (stdlib only)."
    )
    parser.add_argument("--bridge", default=DEFAULT_BRIDGE, help="bridge base URL (loopback only)")
    parser.add_argument("--token-env", default=DEFAULT_TOKEN_ENV,
                        help="env var to report as set/unset; never printed")
    parser.add_argument("--mcp", default="", help="optional path to CivitaiFreeToolMCP.exe")
    parser.add_argument("--timeout", type=float, default=3.0, help="HTTP timeout in seconds (default 3)")
    args = parser.parse_args(argv)

    try:
        bridge = normalize_bridge(args.bridge)
    except ValueError as exc:
        print("[FAIL] bridge: %s" % exc)
        print("\nResult: FAILED")
        return 1
    if args.timeout <= 0:
        print("[FAIL] timeout: must be greater than 0")
        print("\nResult: FAILED")
        return 1

    print("CivitaiFreeTool MCP doctor")
    print("bridge: %s" % bridge)
    print("timeout: %ss" % args.timeout)
    print("")

    report = Report()
    health_ok = check_health(bridge, args.timeout, report)
    if health_ok:
        check_ai(bridge, args.timeout, report)
    else:
        report.skip("/api/automation/ai", "skipped because /api/health failed")

    if args.mcp:
        check_mcp(args.mcp, report)
    else:
        report.skip("MCP binary", "not checked; pass --mcp <path> to verify the sidecar")

    check_token(args.token_env, report)

    print("")
    if report.failures:
        print("Result: FAILED (%d failed, %d warning%s)"
              % (report.failures, report.warnings, "" if report.warnings == 1 else "s"))
        if report.next_steps:
            print("Next steps:")
            for step in report.next_steps:
                print("  - %s" % step)
        return 1

    print("Result: OK (%d warning%s)"
          % (report.warnings, "" if report.warnings == 1 else "s"))
    if report.next_steps:
        print("Notes:")
        for step in report.next_steps:
            print("  - %s" % step)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
