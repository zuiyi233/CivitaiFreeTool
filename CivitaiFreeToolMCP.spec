# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the MCP sidecar (console mode, onefile).

Build with:
    pyinstaller --clean --noconfirm CivitaiFreeToolMCP.spec

Notes:
- ai_contract.py is imported normally by mcp_server.py, so Analysis collects it
  as a pure module.  It is also listed in datas as a belt-and-braces copy for
  frozen/inspection workflows; the copy is harmless.
- The HTTP transport (uvicorn/starlette) is imported lazily, so its protocol
  backends are invisible to static analysis and must be listed explicitly.
"""
from pathlib import Path

hiddenimports = [
    # MCP SDK (low-level server, stdio + Streamable HTTP)
    "mcp",
    "mcp.types",
    "mcp.shared.context",
    "mcp.shared.message",
    "mcp.shared.session",
    "mcp.shared.version",
    "mcp.server",
    "mcp.server.lowlevel",
    "mcp.server.lowlevel.server",
    "mcp.server.lowlevel.helper_types",
    "mcp.server.session",
    "mcp.server.stdio",
    "mcp.server.models",
    "mcp.server.elicitation",
    "mcp.server.streamable_http",
    "mcp.server.streamable_http_manager",
    "mcp.server.transport_security",
    "mcp.server.auth",
    "mcp.server.auth.middleware",
    "mcp.server.auth.middleware.bearer_auth",
    # HTTP stack (uvicorn/starlette imports are lazy and partly dynamic)
    "starlette",
    "starlette.applications",
    "starlette.datastructures",
    "starlette.requests",
    "starlette.responses",
    "starlette.routing",
    "starlette.types",
    "uvicorn",
    "uvicorn.config",
    "uvicorn.server",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.http.flow_control",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "sse_starlette",
    "sse_starlette.sse",
    # Runtime dependencies of mcp / pydantic / validation
    "anyio",
    "anyio._backends._asyncio",
    "anyio.abc",
    "anyio.streams.memory",
    "httpx",
    "httpcore",
    "h11",
    "sniffio",
    "certifi",
    "idna",
    "jsonschema",
    "jsonschema_specifications",
    "referencing",
    "rpds",
    "pydantic",
    "pydantic_core",
    "typing_extensions",
    "typing_inspection",
    "pydantic_settings",
]

a = Analysis(
    ["mcp_server.py"],
    pathex=[str(Path(SPECPATH))],
    binaries=[],
    datas=[("ai_contract.py", ".")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CivitaiFreeToolMCP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["icon.ico"],
)
