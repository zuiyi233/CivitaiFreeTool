#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 调用卡点审计（UI 层）：用真实 Chromium 打开桥提供的界面，验证「配置改了前端会不会跟上」。

为什么要这一层：`POST /api/automation/config` 改的是后端 cfg，而弹窗判定（是否询问移动到分类
文件夹等）发生在前端，读的是页面加载时缓存下来的 `state.cfg`。后端改完前端不知道，就会在
「AI 已经关掉弹窗」之后继续弹 —— 这类卡点只有真的开一次页面才能看见。

用法：
    python scripts/ai_ui_audit.py [--bridge http://127.0.0.1:47531] [--wait 6] [--json]

退出码：0 = 通过；1 = 有卡点。
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BRIDGE = "http://127.0.0.1:47531"


def http(bridge, path, method="GET", body=None, timeout=60):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(bridge + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except ValueError:
            return e.code, {}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bridge", default=DEFAULT_BRIDGE)
    ap.add_argument("--wait", type=float, default=6.0, help="改配置后等多久再读前端（秒）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    checks = []

    def check(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})

    _, cfg0 = http(args.bridge, "/api/automation/config")
    original = (cfg0.get("config") or {}).get("ask_move_after_download")
    if original is None:
        print("无法读取 ask_move_after_download，审计中止", file=sys.stderr)
        return 1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(args.bridge + "/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_function("() => typeof state !== 'undefined' && state.cfg", timeout=30000)
            front = page.evaluate("() => state.cfg.ask_move_after_download")
            check("页面可加载且拿到配置", front is not None, "state.cfg.ask_move_after_download=%s" % front)

            # 1) 后端改配置 → 前端是否在 --wait 内跟上（这是核心卡点）
            want = not bool(original)
            http(args.bridge, "/api/automation/config", "POST",
                 {"config": {"ask_move_after_download": want}})
            t0 = time.time()
            after = front
            while time.time() - t0 < max(0.5, args.wait):
                after = page.evaluate("() => state.cfg.ask_move_after_download")
                if after == want:
                    break
                time.sleep(0.25)
            elapsed = round(time.time() - t0, 2)
            check("外部改配置后前端自动生效", after == want,
                  "后端=%s 前端=%s 生效耗时 %.2fs（上限 %.0fs；需刷新页面才生效即为卡点）"
                  % (want, after, elapsed, args.wait))

            # 2) 弹窗门禁本身是否正确（用真实代码路径，不改 state）
            page.evaluate("() => { _moveAsked.clear(); }")
            shown = page.evaluate("""() => {
                state.cfg.ask_move_after_download = true;
                state.cfg.download_target_dir = '';
                maybeAskMove([{id:'audit-1', filename:'audit.safetensors', status:'done',
                               dest_dir:'D:/audit', routed_dir:''}]);
                const dlg = Array.from(document.querySelectorAll('.rename-dialog'))
                  .some(el => (el.textContent || '').includes('是否移动到分类文件夹'));
                document.querySelectorAll('.rd-mask').forEach(el => el.remove());
                document.querySelectorAll('.rename-dialog').forEach(el => el.remove());
                return dlg;
            }""")
            check("弹窗门禁：开关打开时会询问", shown is True, "dialog=%s" % shown)
            page.evaluate("() => { _moveAsked.clear(); }")
            hidden = page.evaluate("""() => {
                state.cfg.ask_move_after_download = false;
                maybeAskMove([{id:'audit-2', filename:'audit.safetensors', status:'done',
                               dest_dir:'D:/audit', routed_dir:''}]);
                const dlg = Array.from(document.querySelectorAll('.rename-dialog'))
                  .some(el => (el.textContent || '').includes('是否移动到分类文件夹'));
                document.querySelectorAll('.rd-mask').forEach(el => el.remove());
                document.querySelectorAll('.rename-dialog').forEach(el => el.remove());
                return dlg;
            }""")
            check("弹窗门禁：开关关闭时不询问", hidden is False, "dialog=%s" % hidden)

            # 3) 分流任务不该被询问（已经在正确目录里）
            page.evaluate("() => { _moveAsked.clear(); state.cfg.ask_move_after_download = true; }")
            routed = page.evaluate("""() => {
                maybeAskMove([{id:'audit-3', filename:'audit.safetensors', status:'done',
                               dest_dir:'D:/audit', routed_dir:'D:/models/loras/audit'}]);
                const dlg = Array.from(document.querySelectorAll('.rename-dialog'))
                  .some(el => (el.textContent || '').includes('是否移动到分类文件夹'));
                document.querySelectorAll('.rd-mask').forEach(el => el.remove());
                document.querySelectorAll('.rename-dialog').forEach(el => el.remove());
                return dlg;
            }""")
            check("弹窗门禁：分流任务不询问", routed is False, "dialog=%s" % routed)

            check("页面无 JS 异常", not errors, errors[:3])
        finally:
            # 恢复原配置，审计不改变用户设置
            http(args.bridge, "/api/automation/config", "POST",
                 {"config": {"ask_move_after_download": bool(original)}})
            browser.close()

    ok = all(c["ok"] for c in checks)
    if args.json:
        print(json.dumps({"ok": ok, "checks": checks}, ensure_ascii=False, indent=1))
    else:
        print("AI 调用卡点审计（UI 层）  bridge=%s" % args.bridge)
        print("-" * 78)
        for c in checks:
            print("%s %-30s %s" % ("OK  " if c["ok"] else "卡点", c["name"], c["detail"]))
        print("-" * 78)
        print("结论：%s" % ("全部通过" if ok else "存在卡点"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
