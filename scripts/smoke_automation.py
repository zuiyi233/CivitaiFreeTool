#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动化接口冒烟：只调用只读端点 + 预期的 400/404 分支，不下载、不写配置。

用途：改完桥/自动化层后，用一条命令确认「接口面没断、返回形状没变」。
    python scripts/smoke_automation.py [--bridge http://127.0.0.1:47531]

退出码：0 = 全部符合预期；1 = 有失败项。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BRIDGE = "http://127.0.0.1:47531"


def http(bridge, path, method="GET", body=None, timeout=30):
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
            return e.code, {}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bridge", default=DEFAULT_BRIDGE)
    ap.add_argument("--url", default="https://civitai.red/models/2949009?modelVersionId=3398520",
                    help="plan/search 用的样例模型页")
    args = ap.parse_args()
    B = args.bridge
    checks = []

    def check(name, cond, detail=""):
        checks.append({"name": name, "ok": bool(cond), "detail": str(detail)[:200]})

    st, health = http(B, "/api/health", timeout=5)
    check("health", st == 200 and health.get("ok"), health)
    if not checks[0]["ok"]:
        print(json.dumps({"ok": False, "checks": checks}, ensure_ascii=False))
        return 1

    st, man = http(B, "/api/automation")
    paths = {e["path"] for e in (man.get("endpoints") or [])}
    check("manifest", st == 200 and len(paths) >= 12, sorted(paths))
    check("manifest-config-keys", bool(man.get("config_keys_writable")))

    st, state = http(B, "/api/automation/state")
    check("state", st == 200 and state.get("ok") and "config" in state and "tasks" in state)

    st, tasks = http(B, "/api/automation/tasks?limit=5")
    check("tasks", st == 200 and isinstance(tasks.get("tasks"), list))

    st, trimmed = http(B, "/api/automation/tasks?limit=5&fields=id,status")
    rows = trimmed.get("tasks") or []
    check("tasks-fields", st == 200 and all(set(r) <= {"id", "status"} for r in rows)
          and "count" in trimmed, rows[:1])

    st, bat = http(B, "/api/automation/batch", "POST", {"calls": [
        {"method": "GET", "path": "/api/automation/state"},
        {"method": "GET", "path": "/api/automation/storage",
         "query": {"fields": "routing"}}]})
    check("batch", st == 200 and bat.get("ok") and bat.get("executed") == 2
          and bat["results"][1]["result"].get("routing") is not None, bat.get("results"))

    st, schema = http(B, "/api/automation/schema")
    check("schema", st == 200 and "config" in schema and "library_ops" in schema)
    check("schema-routing", bool((schema.get("routing") or {}).get("type_to_folder")),
          (schema.get("routing") or {}).get("core_folders"))

    st, events = http(B, "/api/automation/events?since=0&limit=5")
    check("events", st == 200 and isinstance(events.get("events"), list)
          and "next_since" in events)

    st, hist = http(B, "/api/automation/history?limit=3")
    check("history", st == 200 and isinstance(hist.get("items"), list))

    st, store = http(B, "/api/automation/storage")
    check("storage", st == 200 and isinstance(store.get("dirs"), list)
          and isinstance(store.get("type_dirs"), list))

    st, fs = http(B, "/api/automation/fs")
    check("fs-drives", st == 200 and bool(fs.get("entries"))
          and all(e.get("dir") for e in fs["entries"]), fs.get("entries", [])[:2])
    st, fs404 = http(B, "/api/automation/fs?path=Z%3A%2Fdefinitely%2Fnot%2Fhere")
    check("fs-404", st == 404 and fs404.get("hint"), fs404)

    st, mmstate = http(B, "/api/automation/library/op?op=verify")
    check("library-op-state", st == 200 and "running" in mmstate)

    st, imgstate = http(B, "/api/automation/images/state")
    check("images-state", st == 200 and "download" in imgstate)

    st, cfg = http(B, "/api/automation/config")
    leaked = [k for k, v in (cfg.get("config") or {}).items()
              if k in ("api_key", "baidu_key") and v not in ("", "***")]
    check("config", st == 200 and cfg.get("ok"))
    check("config-redacted", not leaked, leaked)

    st, logs = http(B, "/api/automation/logs?source=both&tail=5")
    check("logs", st == 200 and "error_log" in logs and "events" in logs)

    st, lib = http(B, "/api/automation/library?limit=3")
    check("library", st == 200 and isinstance(lib.get("rows"), list))
    st, gal = http(B, "/api/automation/gallery?limit=2")
    check("gallery", st == 200 and isinstance(gal.get("items"), list) and "count" in gal)
    st, gtags = http(B, "/api/automation/gallery/tags")
    check("gallery-tags", st == 200 and isinstance(gtags.get("tags"), list))
    st, gscan = http(B, "/api/automation/gallery/scan")
    check("gallery-scan-state", st == 200 and "running" in (gscan.get("state") or {}))
    st, gitem = http(B, "/api/automation/gallery/item?id=999999999")
    check("gallery-item-404", st == 404 and gitem.get("hint"), gitem)
    row = (lib.get("rows") or [{}])[0]
    if row.get("path") and os.path.isfile(row["path"]):
        st, detail = http(B, "/api/automation/model?path=" + urllib.parse.quote(row["path"]))
        check("model-detail", st == 200 and isinstance(detail.get("detail"), dict))
        st, ver = http(B, "/api/automation/verify?path=" + urllib.parse.quote(row["path"]))
        check("verify", st == 200 and ver.get("exists") is True)
        st, pr = http(B, "/api/automation/prompts?limit=3&path=" + urllib.parse.quote(row["path"]))
        check("prompts", st == 200 and isinstance(pr.get("images"), list))

    st, ext = http(B, "/api/automation/extension")
    check("extension", st == 200 and "download_requests" in ext)

    st, conn = http(B, "/api/automation/connectivity")
    check("connectivity", st == 200 and "dns" in conn and "api" in conn)

    st, plan = http(B, "/api/automation/plan", "POST", {"url": args.url})
    check("plan", st == 200 and plan.get("ok") and plan.get("files"), plan.get("msg"))

    st, res = http(B, "/api/automation/resolve", "POST", {"url": args.url})
    check("resolve", st == 200 and res.get("ok") and res.get("versions"), res.get("msg"))

    st, search = http(B, "/api/automation/search", "POST", {"query": "rezero", "limit": 3})
    check("search", st == 200 and search.get("ok"), search.get("msg"))

    # 预期失败分支：形状要稳定、说清原因
    st, fc = http(B, "/api/automation/file-choice", "POST", {})
    check("file-choice-400", st == 400 and "identity" in (fc.get("msg") or ""), fc)
    st, bad = http(B, "/api/automation/tasks", "POST", {"action": "nope", "ids": []})
    check("tasks-400", st == 400, bad)
    st, nf = http(B, "/api/automation/verify?task_id=__missing__")
    check("verify-404", st == 404, nf)
    st, wf = http(B, "/api/automation/workflow/analyze", "POST", {"path": "Z:/nope.json"})
    check("workflow-404", st == 404, wf)
    st, mm = http(B, "/api/automation/library/op", "POST", {"op": "nope"})
    check("library-op-400", st == 400, mm)
    st, gop = http(B, "/api/automation/gallery/op", "POST", {"op": "nope", "ids": [1]})
    check("gallery-op-400", st == 400 and gop.get("hint"), gop)
    st, mv = http(B, "/api/automation/move", "POST", {"path": "Z:/nope.safetensors"})
    check("move-400", st == 400 and mv.get("hint"), mv)
    st, rn = http(B, "/api/automation/rename", "POST", {})
    check("rename-400", st == 400 and rn.get("hint"), rn)
    st, bad = http(B, "/api/automation/batch", "POST", {"calls": [{"path": "/api/rpc"}]})
    check("batch-foreign-400", st == 200 and bad.get("results", [{}])[0].get("http_status") == 400, bad)
    st, rep = http(B, "/api/automation/tasks/repair", "POST", {})
    check("repair-400", st == 400 and rep.get("hint"), rep)

    ok = all(c["ok"] for c in checks)
    print(json.dumps({"ok": ok, "checks": checks}, ensure_ascii=False, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
