"""Start the packaged MCP sidecar in HTTP mode alongside the desktop app.

Frozen builds only: source runs and tests must not spawn background processes.
Every failure is non-fatal — MCP is an add-on transport, not a startup
dependency of the UI or downloads.
"""
import atexit
import os
import subprocess
import sys

import ai_contract
import mcp_paths

PROC = None
CREATE_NO_WINDOW = 0x08000000


def sidecar_path(app_dir):
    name = "CivitaiFreeToolMCP.exe" if os.name == "nt" else "CivitaiFreeToolMCP"
    return os.path.join(str(app_dir or ""), name)


def build_args(exe, port=None, toolsets="core,vision,admin"):
    return [str(exe), "--http", "--port", str(int(port or ai_contract.DEFAULT_MCP_HTTP_PORT)),
            "--toolsets", str(toolsets or "core,vision,admin"), "--exit-with-bridge"]


def enabled(cfg=None):
    if os.environ.get("CFT_NO_MCP_HTTP"):
        return False
    if not getattr(sys, "frozen", False):
        return False
    cfg = cfg if isinstance(cfg, dict) else {}
    return cfg.get("mcp_http_autostart", True) is not False


def maybe_start(app_dir, cfg=None):
    """Return the Popen handle, or None when skipped/unavailable."""
    global PROC
    if not enabled(cfg):
        return None
    exe = sidecar_path(app_dir)
    if not os.path.isfile(exe):
        return None
    port = int(ai_contract.DEFAULT_MCP_HTTP_PORT)
    if mcp_paths.port_open(port):
        return None
    if PROC is not None and PROC.poll() is None:
        return PROC
    kwargs = {
        "cwd": str(app_dir), "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(build_args(exe, port), **kwargs)
    except OSError:
        return None
    PROC = proc
    atexit.register(_stop, proc)
    return proc


def _stop(proc):
    try:
        if proc is not None and proc.poll() is None:
            proc.terminate()
    except OSError:
        pass


def status(cfg=None):
    proc = PROC
    running = bool(proc is not None and proc.poll() is None) or mcp_paths.port_open(
        int(ai_contract.DEFAULT_MCP_HTTP_PORT))
    path = mcp_paths.token_path()
    return {
        "autostart": bool(enabled(cfg)),
        "running": running,
        "pid": proc.pid if (proc is not None and proc.poll() is None) else None,
        "port": int(ai_contract.DEFAULT_MCP_HTTP_PORT),
        "token_file": str(path),
        "token_present": bool(path.is_file()),
    }
