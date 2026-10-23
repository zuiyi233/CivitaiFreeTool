#!/usr/bin/env python3
"""Generate or merge the CivitaiFreeTool MCP client config.

Examples (run from the repository root):

    python scripts/mcp_setup.py --client cursor --apply
    python scripts/mcp_setup.py --client claude --transport http --apply
    python scripts/mcp_setup.py --print --transport stdio

The token is never printed: dry-run HTTP output uses a placeholder, and
``--apply`` writes the real token only into the target client config file.
"""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE = "http://127.0.0.1:47531"
MCP_HTTP = "http://127.0.0.1:47532/mcp"
DEFAULT_TOKEN_FILE = ((Path(os.environ["LOCALAPPDATA"]) / "CivitaiFreeToolWeb" / "mcp_http_token.txt")
                      if os.environ.get("LOCALAPPDATA")
                      else Path.home() / ".civitiaifreetoolweb" / "mcp_http_token.txt")
CLIENT_PATHS = {
    "cursor": Path(os.environ.get("USERPROFILE") or Path.home()) / ".cursor" / "mcp.json",
    "claude": Path(os.environ.get("APPDATA") or (Path.home() / ".config"))
              / "Claude" / "claude_desktop_config.json",
}


def find_exe(explicit=""):
    if explicit:
        return Path(explicit).expanduser()
    for candidate in (REPO / "dist" / "CivitaiFreeToolMCP.exe",
                      REPO / "CivitaiFreeToolMCP.exe"):
        if candidate.is_file():
            return candidate
    return None


def read_token(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def server_entry(exe, transport, toolsets, token, client):
    if transport == "stdio":
        return {
            "command": str(exe),
            "args": ["--stdio", "--toolsets", str(toolsets)],
            "env": {"CFT_BRIDGE_URL": BRIDGE},
        }
    entry = {"url": MCP_HTTP, "headers": {"Authorization": "Bearer " + token}}
    if client == "claude":
        entry = {"type": "http", **entry}
    return entry


def merge_config(path, name, entry):
    """Merge one server into an existing mcpServers JSON, with a timestamped backup."""
    path = Path(path)
    data = {}
    backup = None
    if path.exists():
        raw = path.read_text(encoding="utf-8-sig")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError as exc:
            raise ValueError("现有配置不是合法 JSON，未覆盖：%s" % exc)
        if not isinstance(data, dict):
            raise ValueError("现有配置顶层必须是对象，未覆盖")
        backup = path.with_name(path.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
    servers = data.get("mcpServers")
    if servers is None:
        servers = data["mcpServers"] = {}
    if not isinstance(servers, dict):
        raise ValueError("现有的 mcpServers 不是对象，未覆盖")
    servers[name] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client", choices=sorted(CLIENT_PATHS), default="cursor",
                        help="Target client (default cursor).")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                        help="stdio launches its own sidecar; http uses the always-on one.")
    parser.add_argument("--exe", default="", help="Path to CivitaiFreeToolMCP.exe.")
    parser.add_argument("--toolsets", default="core,vision",
                        help="stdio/direct toolsets (default core,vision).")
    parser.add_argument("--name", default="civitai-free-tool", help="Server name in the client config.")
    parser.add_argument("--path", default="", help="Override the client config path.")
    parser.add_argument("--token-file", default="", help="Override the MCP HTTP token file path.")
    parser.add_argument("--apply", action="store_true", help="Write the config (default: print only).")
    parser.add_argument("--print", dest="print_only", action="store_true",
                        help="Print the snippet even when --apply is given.")
    args = parser.parse_args(argv)

    exe = find_exe(args.exe)
    if args.transport == "stdio" and (exe is None or not exe.is_file()):
        print("找不到 CivitaiFreeToolMCP.exe：先运行 pyinstaller 构建，或用 --exe 指定。",
              file=sys.stderr)
        return 2

    token_file = Path(args.token_file).expanduser() if args.token_file else DEFAULT_TOKEN_FILE
    token = read_token(token_file)
    if args.transport == "http" and not token:
        print("没有找到 HTTP Bearer token：先启动一次桌面端/MCP HTTP（自动生成），"
              "或 --token-file 指定；token 不在终端回显。\n路径：%s" % token_file,
              file=sys.stderr)
        if args.apply:
            return 2

    entry_token = token or "<TOKEN_FROM_FILE>"
    entry = server_entry(exe, args.transport, args.toolsets, entry_token, args.client)
    print_entry = entry
    if args.transport == "http":
        print_entry = dict(entry)
        print_entry["headers"] = {"Authorization": "Bearer <TOKEN_FROM_FILE>"}
    snippet = {"mcpServers": {args.name: print_entry}}

    if not args.apply or args.print_only:
        print(json.dumps(snippet, ensure_ascii=False, indent=2))
        if args.transport == "http" and not token:
            print("\n# 把 <TOKEN_FROM_FILE> 换成 %s 的内容；不要粘贴到聊天/提交到仓库。" % token_file,
                  file=sys.stderr)

    if not args.apply:
        print("\n# 未写入。加 --apply 写入客户端配置（会先备份）。", file=sys.stderr)
        return 0

    target = Path(args.path).expanduser() if args.path else CLIENT_PATHS[args.client]
    try:
        backup = merge_config(target, args.name, entry)
    except (OSError, ValueError) as exc:
        print("写入失败：%s" % exc, file=sys.stderr)
        return 1
    print("已写入：%s" % target)
    if backup:
        print("备份：%s" % backup)
    if args.transport == "http":
        print("Bearer token 已写入该配置文件（未回显）；token 文件：%s" % token_file)
    print("重启客户端后使用；stdio 无需先启动桌面端（会自行拉起侧车），http 需要桌面端在运行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
