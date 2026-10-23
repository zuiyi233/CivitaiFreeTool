"""Shared loopback paths for the MCP sidecar and the desktop bridge.

Kept dependency-free so both the Web EXE and the MCP EXE can import it without
pulling in the MCP SDK (the Web EXE does not ship ``mcp``).
"""
import os
import socket
from pathlib import Path

TOKEN_DIR = "CivitaiFreeToolWeb"
TOKEN_FILE = "mcp_http_token.txt"


def token_path() -> Path:
    """Bearer token location; never inside the repository."""
    base = os.environ.get("LOCALAPPDATA") or ""
    if base:
        path = Path(base) / TOKEN_DIR / TOKEN_FILE
    else:
        path = Path.home() / ("." + TOKEN_DIR.lower()) / TOKEN_FILE
    try:
        repo = Path(__file__).resolve().parent
        if path.resolve().is_relative_to(repo):
            path = Path.home() / ("." + TOKEN_DIR.lower()) / TOKEN_FILE
    except (OSError, ValueError):
        pass
    return path


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False
