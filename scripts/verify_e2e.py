#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一条命令验证「提交 → 分流落盘 → 附属文件」，输出紧凑 JSON。

现在全部走桌面端的语义化接口（/api/automation/*），不再自己轮询与拼校验：
    python scripts/verify_e2e.py --url "<C站模型页>"            # 下载并等待完成
    python scripts/verify_e2e.py --url "..." --plan             # 只干跑，不入队
    python scripts/verify_e2e.py --task <task_id>               # 只校验已有任务
    python scripts/verify_e2e.py --url "..." --json             # 单行 JSON

退出码：0 = 成功；1 = 失败/超时。契约见 docs/automation-api.md。
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
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
        except Exception:
            return e.code, {"ok": False, "msg": "HTTP %s" % e.code}


def set_config(bridge, pairs):
    patch = {}
    for item in pairs or []:
        if "=" not in item:
            raise SystemExit("--set 需要 k=v 形式：%r" % item)
        k, v = item.split("=", 1)
        low = v.strip().lower()
        patch[k.strip()] = (low == "true") if low in ("true", "false") else v
    st, d = http(bridge, "/api/automation/config", "POST", {"config": patch})
    if st != 200 or not d.get("ok"):
        raise SystemExit("配置写入失败: %s %s" % (st, d))
    return d.get("config")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="C 站模型页/版本页 URL")
    ap.add_argument("--task", help="只校验这个 task_id")
    ap.add_argument("--plan", action="store_true", help="干跑：只解析文件与落点，不入队")
    ap.add_argument("--hash", action="store_true", help="校验时重算 SHA256（大文件较慢）")
    ap.add_argument("--wait", type=int, default=900, help="等待完成的秒数（默认 900）")
    ap.add_argument("--bridge", default=DEFAULT_BRIDGE)
    ap.add_argument("--set", action="append", metavar="KEY=VALUE", help="提交前写配置，可重复")
    ap.add_argument("--json", action="store_true", help="只输出一行 JSON")
    args = ap.parse_args()

    report = {"ok": False, "bridge": args.bridge}
    st, health = http(args.bridge, "/api/health", timeout=5)
    report["health"] = health
    if st != 200 or not health.get("ok"):
        report["error"] = "桌面程序未启动或桥不可用"
        print(json.dumps(report, ensure_ascii=False))
        return 1
    if args.set:
        report["config"] = set_config(args.bridge, args.set)

    if args.task:
        st, verify = http(args.bridge, "/api/automation/verify?task_id=%s&hash=%d"
                          % (urllib.parse.quote(args.task), 1 if args.hash else 0))
        report.update({"ok": bool(verify.get("ok")), "verify": verify})
        if st != 200:
            report["error"] = verify.get("msg") or st
        print(json.dumps(report, ensure_ascii=False, indent=0 if args.json else 1))
        return 0 if report["ok"] else 1

    if not args.url:
        raise SystemExit("需要 --url（或 --task）")

    if args.plan:
        st, plan = http(args.bridge, "/api/automation/plan", "POST", {"url": args.url})
        report.update({"ok": st == 200 and bool(plan.get("ok")), "plan": plan})
        if not report["ok"]:
            report["error"] = plan.get("msg") or st
        print(json.dumps(report, ensure_ascii=False, indent=0 if args.json else 1))
        return 0 if report["ok"] else 1

    st, dl = http(args.bridge, "/api/automation/download", "POST",
                  {"url": args.url, "wait": True, "timeout": args.wait, "verify": True},
                  timeout=args.wait + 60)
    report.update({"ok": bool(dl.get("ok")), "download": dl})
    if not report["ok"]:
        report["error"] = dl.get("msg") or (dl.get("task") or {}).get("error") or st
    print(json.dumps(report, ensure_ascii=False, indent=0 if args.json else 1))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
