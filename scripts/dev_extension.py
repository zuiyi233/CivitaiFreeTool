#!/usr/bin/env python3
"""Development loop for the unpacked browser extension — no repacking.

One-time:
    python scripts/dev_extension.py link

Then:
    python scripts/dev_extension.py watch      # source edits -> bridge reload flag
    python scripts/dev_extension.py reload     # ask the loaded extension to reload now

The browser still needs one manual reload after a content-script change (it does not
inject new content scripts into already-open tabs). Background/popup changes are
picked up by ``runtime.reload()`` without touching the extensions page.

Only repository paths under ``.tools/`` are modified. The manifest ``key`` is kept,
so the extension ID does not change.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE = "http://127.0.0.1:47531"
DEFAULT_LOADED = REPO / ".tools" / "release2632" / "CivitaiFreeTool-ChromeExtension"
QUIET_DIRS = {".git", "node_modules"}


def _is_link(path: Path) -> bool:
    try:
        if hasattr(os.path, "isjunction") and os.path.isjunction(path):
            return True
    except OSError:
        pass
    return path.is_symlink()


def _abs_no_follow(path) -> Path:
    """Absolute path without resolving junctions/symlinks (resolve() would hide the link)."""
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _link_target(path: Path) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return ""


def _make_link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # Direct argv (no shell): mklink is a cmd builtin, so invoke cmd.exe explicitly.
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise OSError((result.stderr or result.stdout or "mklink failed").strip())
    else:
        os.symlink(str(target), str(link), target_is_directory=True)


def _remove_link(link: Path) -> None:
    if os.name == "nt":
        # rmdir removes a junction itself, never the target's contents.
        subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True, text=True)
    else:
        link.unlink()


def _backups(loaded: Path):
    return sorted(loaded.parent.glob(loaded.name + ".release-backup-*"))


def link(args):
    source = Path(args.source).expanduser().resolve()
    loaded = _abs_no_follow(args.loaded)
    if not (source / "manifest.json").is_file():
        print("源目录没有 manifest.json：%s" % source, file=sys.stderr)
        return 2
    if _is_link(loaded):
        if os.path.normcase(_link_target(loaded)) == os.path.normcase(str(source)):
            print("已是指向源码的链接：%s -> %s" % (loaded, source))
            return 0
        print("目标已是链接但指向别处：%s -> %s（先 restore 或换 --loaded）"
              % (loaded, _link_target(loaded)), file=sys.stderr)
        return 1
    backup = None
    if loaded.exists():
        backup = loaded.with_name(loaded.name + ".release-backup-" + time.strftime("%Y%m%d-%H%M%S"))
        try:
            os.replace(loaded, backup)
        except OSError as exc:
            print("无法移开已加载目录（浏览器可能占用）：%s" % exc, file=sys.stderr)
            return 1
    try:
        _make_link(loaded, source)
    except OSError as exc:
        if backup and backup.exists() and not loaded.exists():
            os.replace(backup, loaded)
        print("创建链接失败：%s" % exc, file=sys.stderr)
        return 1
    print("开发链接已建立：%s -> %s" % (loaded, source))
    if backup:
        print("旧目录备份：%s" % backup)
    print("在浏览器扩展页对 CivitaiFreeTool 点一次「重新加载」；之后运行 watch 即可。")
    return 0


def restore(args):
    loaded = _abs_no_follow(args.loaded)
    if not _is_link(loaded):
        print("目标不是链接，未改动：%s" % loaded, file=sys.stderr)
        return 1
    _remove_link(loaded)
    backups = _backups(loaded)
    if not backups:
        print("已移除链接；没有找到 release-backup，目录现在是空的：%s" % loaded)
        return 0
    latest = backups[-1]
    try:
        os.replace(latest, loaded)
    except OSError as exc:
        print("链接已移除，但恢复备份失败：%s（备份在 %s）" % (exc, latest), file=sys.stderr)
        return 1
    print("已恢复：%s -> %s" % (loaded, latest.name))
    return 0


def _state(bridge):
    try:
        with urllib.request.urlopen(bridge.rstrip("/") + "/api/ext/dev/state", timeout=3) as resp:
            return json.loads(resp.read(1 << 20).decode("utf-8", "replace"))
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _request_reload(bridge):
    req = urllib.request.Request(bridge.rstrip("/") + "/api/ext/dev/reload", data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read(1 << 20).decode("utf-8", "replace"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return {"ok": False, "msg": str(exc)}


def reload_cmd(args):
    out = _request_reload(args.bridge)
    if not out.get("ok"):
        print("请求重载失败：%s（桌面端在运行吗？）" % out.get("msg"), file=sys.stderr)
        return 1
    print("已请求重载 reload_at=%.3f" % float(out.get("reload_at") or 0))
    if not args.wait:
        return 0
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        state = _state(args.bridge) or {}
        if float(state.get("pushed_at") or 0) >= float(out.get("reload_at") or 0):
            print("扩展已重载并重新上报（pushed_at=%.3f）" % float(state.get("pushed_at") or 0))
            return 0
        time.sleep(1)
    print("等待超时：扩展还没上报；切一个标签页或等下次心跳（≤60s）。", file=sys.stderr)
    return 2


def status(args):
    loaded = _abs_no_follow(args.loaded)
    source = Path(args.source).expanduser().resolve()
    data = {
        "source": str(source),
        "source_manifest": _manifest_version(source),
        "loaded": str(loaded),
        "loaded_manifest": _manifest_version(loaded),
        "is_link": _is_link(loaded),
        "link_target": _link_target(loaded) if _is_link(loaded) else "",
        "bridge": args.bridge,
        "bridge_state": _state(args.bridge),
    }
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0 if data["source_manifest"] else 1


def _manifest_version(path: Path):
    try:
        return json.loads((path / "manifest.json").read_text(encoding="utf-8")).get("version") or ""
    except (OSError, ValueError):
        return ""


def _snapshot(root: Path):
    out = {}
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in QUIET_DIRS]
        for name in names:
            if name.endswith(".test.cjs"):
                continue
            path = Path(base) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            out[str(path)] = (stat.st_mtime_ns, stat.st_size)
    return out


def watch(args):
    source = Path(args.source).expanduser().resolve()
    if not (source / "manifest.json").is_file():
        print("源目录没有 manifest.json：%s" % source, file=sys.stderr)
        return 2
    previous = _snapshot(source)
    print("监听 %s（%d 个文件）；Ctrl+C 停止。改动后自动请求扩展重载。" % (source, len(previous)))
    try:
        while True:
            time.sleep(max(0.2, args.interval))
            current = _snapshot(source)
            changed = [path for path, sig in current.items() if previous.get(path) != sig]
            changed += [path for path in previous if path not in current]
            if not changed:
                continue
            time.sleep(0.8)
            current = _snapshot(source)
            changed = [path for path, sig in current.items() if previous.get(path) != sig]
            changed += [path for path in previous if path not in current]
            previous = current
            out = _request_reload(args.bridge)
            if out.get("ok"):
                print("改动 %d 个文件 → 已请求重载（%s）" % (len(changed), ", ".join(
                    sorted(os.path.basename(p) for p in changed)[:6])))
            else:
                print("改动 %d 个文件，但请求重载失败：%s" % (len(changed), out.get("msg")))
    except KeyboardInterrupt:
        print("\n已停止监听。")
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bridge", default=BRIDGE, help="Desktop bridge URL.")
    parser.add_argument("--source", default=str(REPO / "chrome-extension"),
                        help="Extension source directory.")
    parser.add_argument("--loaded", default=str(DEFAULT_LOADED),
                        help="Directory the browser loaded (under .tools/).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("link", help="Replace the loaded copy with a link to source (backup kept).")
    sub.add_parser("restore", help="Remove the link and restore the newest backup.")
    sub.add_parser("status", help="Show link/version/bridge state.")
    p_reload = sub.add_parser("reload", help="Ask the extension to reload now.")
    p_reload.add_argument("--wait", action="store_true", help="Wait for the next index push.")
    p_reload.add_argument("--timeout", type=float, default=75.0)
    p_watch = sub.add_parser("watch", help="Reload on source changes.")
    p_watch.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)

    if args.cmd == "link":
        return link(args)
    if args.cmd == "restore":
        return restore(args)
    if args.cmd == "status":
        return status(args)
    if args.cmd == "reload":
        return reload_cmd(args)
    if args.cmd == "watch":
        return watch(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
