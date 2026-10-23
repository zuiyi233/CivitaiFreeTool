# -*- coding: utf-8 -*-
"""Manual smoke test for the MCP sidecar over stdio.

Examples:
    python scripts/smoke_mcp.py
    python scripts/smoke_mcp.py --bridge http://127.0.0.1:47531 --toolsets core,vision
    python scripts/smoke_mcp.py --exe dist/CivitaiFreeToolMCP.exe

It starts the sidecar, performs initialize -> tools/list -> resources/read guide
-> cft_capabilities, and prints a short summary.  This script is for humans; the
automated coverage lives in tests/test_mcp_server.py and tests/test_mcp_http.py.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import AnyUrl

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ai_contract as C  # noqa: E402


async def _run(args) -> int:
    if args.exe:
        command = str(Path(args.exe).resolve())
        base = [command]
    else:
        command = sys.executable
        base = [command, str(ROOT / "mcp_server.py")]
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    params = StdioServerParameters(
        command=command,
        args=base[1:] + ["--stdio", "--bridge", args.bridge,
                         "--toolsets", args.toolsets],
        cwd=str(ROOT),
        env=env,
    )
    with anyio.fail_after(args.timeout):
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                init = await session.initialize()
                print("server: %s %s" % (init.serverInfo.name, init.serverInfo.version))

                tools = await session.list_tools()
                print("tools: %d (%s)" % (
                    len(tools.tools), ", ".join(sorted(t.name for t in tools.tools))))

                guide = await session.read_resource(AnyUrl("cft://guide/text"))
                print("guide: %d chars" % len(guide.contents[0].text))

                result = await session.call_tool("cft_capabilities", {})
                if result.isError:
                    print("cft_capabilities failed:")
                    print(result.structuredContent or
                          (result.content[0].text if result.content else ""))
                    return 1
                structured = result.structuredContent or {}
                print("app: ok=%s tool=%s" % (
                    structured.get("ok"),
                    structured.get("tool") or "cft_capabilities"))
                print(json.dumps(structured.get("data"), ensure_ascii=False,
                                 indent=2, default=str)[:4000])
                return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test CivitaiFreeToolMCP over stdio.")
    parser.add_argument("--bridge", default=C.DEFAULT_BRIDGE)
    parser.add_argument("--toolsets", default="core")
    parser.add_argument("--exe", default="", help="Use a packaged CivitaiFreeToolMCP.exe.")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    try:
        return anyio.run(_run, args)
    except Exception as exc:
        print("smoke failed: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
