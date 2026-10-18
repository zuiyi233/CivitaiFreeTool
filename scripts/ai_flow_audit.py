#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 调用卡点审计：只用自动化接口，把一条完整工作流实际跑一遍并计时。

它回答的不是「接口存在吗」，而是三个更难的问题：
  1. 某一步是不是**必须人工介入**（原生弹窗、待选文件、模态确认）；
  2. 某一步是不是**没有完成信号**（只能盲轮询，或者根本不知道结束没）；
  3. 某一步是不是**要多跳几次**才能完成（例如收尾失败后没有重跑入口）。

用法：
    python scripts/ai_flow_audit.py                  # 只读 + 可等待动作（快，默认）
    python scripts/ai_flow_audit.py --e2e            # 额外跑一次真实小文件下载→校验
    python scripts/ai_flow_audit.py --json           # 机器可读
    python scripts/ai_flow_audit.py --bridge http://127.0.0.1:47531

退出码：0 = 没有发现阻塞项；1 = 有阻塞项（steps 里 blocked_by 非空）。
"""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BRIDGE = "http://127.0.0.1:47531"
SAMPLE_URL = "https://civitai.red/models/2949009?modelVersionId=3398520"


def http(bridge, path, method="GET", body=None, timeout=300):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(bridge + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8") or "{}"
            return r.status, json.loads(raw), round((time.time() - t0) * 1000, 1), len(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8") or "{}"
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"ok": False, "msg": raw[:200]}
        return e.code, payload, round((time.time() - t0) * 1000, 1), len(raw)


class Audit:
    def __init__(self, bridge, keep_e2e=False):
        self.bridge = bridge
        self.steps = []
        self.keep_e2e = keep_e2e

    def step(self, name, fn, blocked_by="", note=""):
        """跑一步并记录：ok / 耗时 / 是否被人工步骤或缺失能力挡住"""
        detail = ""
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, "异常: %s" % str(e)[:200]
        if isinstance(detail, dict):
            ms = detail.get("ms", 0)
            shown = detail.get("note") or note
        else:
            ms = 0
            shown = detail or note          # 异常信息必须显示出来，不能被默认 note 盖掉
        self.steps.append({"name": name, "ok": bool(ok), "ms": ms,
                           "blocked_by": "" if ok else blocked_by, "note": shown})
        return ok, detail

    # ---------------------------------------------------------------- 各步
    def discovery(self):
        st, health, ms, _ = http(self.bridge, "/api/health", timeout=10)
        st2, man, ms2, n_man = http(self.bridge, "/api/automation")
        st3, sch, ms3, _ = http(self.bridge, "/api/automation/schema")
        ok = st == 200 and st2 == 200 and st3 == 200 and man.get("endpoints") and sch.get("config")
        return ok, {"ms": ms + ms2 + ms3,
                    "note": "%d 端点 / %d 可写配置 / 清单 %.1fKB"
                            % (len(man.get("endpoints") or []), len(man.get("config_keys_writable") or []),
                               n_man / 1024.0)}

    def plan_and_resolve(self, url):
        st, plan, ms, _ = http(self.bridge, "/api/automation/plan", "POST", {"url": url}, timeout=120)
        st2, res, ms2, _ = http(self.bridge, "/api/automation/resolve", "POST", {"url": url}, timeout=120)
        files = plan.get("files") or []
        ok = st == 200 and plan.get("ok") and files and res.get("versions")
        return ok, {"ms": ms + ms2,
                    "note": "%d 个文件 → %s（已存在=%s）；%d 个版本"
                            % (len(files), files[0].get("routed_dir") or "未分流",
                               files[0].get("exists") if files else None,
                               len(res.get("versions") or []))}

    def tasks_shape(self):
        st, full, ms, n_full = http(self.bridge, "/api/automation/tasks?limit=20")
        st2, slim, ms2, n_slim = http(self.bridge, "/api/automation/tasks?limit=20&fields=id,status,postprocess_errors")
        rows = slim.get("tasks") or []
        ok = st == 200 and st2 == 200 and all(set(r) <= {"id", "status", "postprocess_errors"} for r in rows)
        return ok, {"ms": ms + ms2,
                    "note": "%d 条任务；完整 %.1fKB → 投影 %.1fKB"
                            % (len(rows), n_full / 1024.0, n_slim / 1024.0)}

    def pending_choice(self):
        st, state, ms, _ = http(self.bridge, "/api/automation/state")
        pending = state.get("pending_file_choice")
        # 有待选文件不算失败（有接口可答），但它是「人工卡点」：必须显式记录
        return True, {"ms": ms,
                      "note": ("存在待选文件（可用 /file-choice 回答）: %s" % (pending.get("id") if pending else ""))
                              if pending else "无待选文件"}

    def download_gate(self, url):
        """检查下载入口的契约，**不提交真实下载**。

        以前这里真的提交一次再删任务：每次审计都拉起一条 284MB 传输，而「提交 → 列表 →
        移除」之间有竞态，删不掉时就会留下一条「下载中」和几十上百 MB 的 .part（实测踩到）。
        真实「提交 → 等完 → 校验」由 --e2e 的小模型覆盖，这条只测参数校验与自述。
        """
        st, bad, ms, _ = http(self.bridge, "/api/automation/download", "POST", {}, timeout=60)
        st2, man, ms2, _ = http(self.bridge, "/api/automation")
        entry = next((e for e in (man.get("endpoints") or [])
                      if e.get("path") == "/api/automation/download"), {})
        has_force = "force" in (entry.get("params") or {})
        ok = st == 400 and bool(bad.get("hint")) and has_force
        return ok, {"ms": ms + ms2,
                    "note": "空 body 被拒=%s（带 hint=%s）；入口声明 force=%s（已存在不重下）"
                            % (st == 400, bool(bad.get("hint")), has_force)}

    def live_done_task(self):
        """挑一个「已完成且文件还在」的任务当探针。

        直接取最后一条 done 会把「用户已删文件」的历史任务当探针，报出假卡点
        （本脚本自己 e2e 清理后也踩过），所以这里逐个 verify 到 exists 为止。
        """
        st, tasks, ms, _ = http(self.bridge,
                                "/api/automation/tasks?limit=40&status=done&fields=id,filename,status,routed_dir,dest_dir")
        for row in reversed(tasks.get("tasks") or []):
            tid = row.get("id")
            if not tid:
                continue
            st2, ver, ms2, _ = http(self.bridge,
                                    "/api/automation/verify?task_id=" + urllib.parse.quote(tid))
            if st2 == 200 and ver.get("exists"):
                return tid, row, ver, ms + ms2
        return "", {}, {}, ms

    def verify_and_prompts(self):
        tid, row, ver, ms = self.live_done_task()
        if not tid:
            return True, {"ms": ms, "note": "跳过：没有「文件仍在」的已完成任务"}
        path = os.path.join(row.get("routed_dir") or row.get("dest_dir") or "",
                            row.get("filename") or "")
        st2, pr, ms2, _ = http(self.bridge, "/api/automation/prompts?limit=3&path=" + urllib.parse.quote(path))
        ok = bool(ver.get("ok")) and st2 == 200
        return ok, {"ms": ms + ms2,
                    "note": "ok=%s bundle_ok=%s 封面=%s；prompts=%s 条（%s）"
                            % (ver.get("ok"), ver.get("bundle_ok"), ver.get("preview") or "无",
                               (pr or {}).get("count"),
                               (pr or {}).get("hint") or "有生成参数")}

    def postprocess_health(self):
        st, tasks, ms, _ = http(self.bridge, "/api/automation/tasks?limit=20&fields=id,status,postprocess_errors")
        bad = [t for t in (tasks.get("tasks") or []) if t.get("postprocess_errors")]
        return True, {"ms": ms,
                      "note": ("%d 条任务收尾未完成：%s" % (len(bad), bad[:2])) if bad else "所有任务收尾完整"}

    def repair_probe(self):
        """检查「收尾失败能否重跑」：接口存在且能给出完成信号才算通过。"""
        st, man, ms, _ = http(self.bridge, "/api/automation")
        paths = {e.get("path") for e in (man.get("endpoints") or [])}
        if "/api/automation/tasks/repair" not in paths:
            return False, {"ms": ms, "note": "没有重跑入口：只能 verify/images/cover 分别调"}
        tid, _row, _ver, ms2 = self.live_done_task()
        if not tid:
            return True, {"ms": ms2, "note": "跳过：没有「文件仍在」的已完成任务可试"}
        st3, rep, ms3, _ = http(self.bridge, "/api/automation/tasks/repair", "POST",
                                {"task_id": tid, "stages": ["metadata"], "wait": True, "timeout": 60})
        ok = st3 == 200 and rep.get("ok") and rep.get("finished")
        return ok, {"ms": ms3, "note": "重跑 metadata：%s（%.1fs）"
                                       % ("完成" if ok else rep.get("msg"), (rep.get("waited_s") or 0))}

    def file_ops(self):
        st, lib, ms, _ = http(self.bridge, "/api/automation/library?limit=3&q=REZERO")
        row = (lib.get("rows") or [{}])[0]
        path = row.get("path") or ""
        if not path:
            return False, {"ms": ms, "note": "模型库为空，跳过文件操作"}
        st2, prev, ms2, _ = http(self.bridge, "/api/automation/rename", "POST",
                                 {"paths": [path], "preview": True})
        st3, mv, ms3, _ = http(self.bridge, "/api/automation/move", "POST",
                               {"path": path, "dest_dir": "Z:/definitely/not/here"})
        st4, fs, ms4, _ = http(self.bridge, "/api/automation/fs")
        ok = (st2 == 200 and prev.get("ok") and st3 == 400 and mv.get("hint")
              and st4 == 200 and fs.get("entries"))
        return ok, {"ms": ms + ms2 + ms3 + ms4,
                    "note": "重命名预览=%s；移动坏目录被拒且带 hint=%s；盘符 %d 个"
                            % (bool(prev.get("ok")), bool(mv.get("hint")), len(fs.get("entries") or []))}

    def batch_roundtrip(self):
        st, out, ms, _ = http(self.bridge, "/api/automation/batch", "POST", {"calls": [
            {"method": "GET", "path": "/api/automation/state", "query": {"fields": "version"}},
            {"method": "GET", "path": "/api/automation/storage"},
            {"method": "GET", "path": "/api/automation/extension"}]})
        ok = st == 200 and out.get("ok") and out.get("executed") == 3
        return ok, {"ms": ms, "note": "3 条调用一次往返，%d 条成功" % (out.get("executed") or 0)}

    def connectivity(self):
        st, conn, ms, _ = http(self.bridge, "/api/automation/connectivity", timeout=120)
        return st == 200, {"ms": ms,
                           "note": "API=%s；代理=%s；告警=%d 条"
                                   % (conn.get("api", {}).get("ok"),
                                      conn.get("proxy", {}).get("enabled"),
                                      len(conn.get("warnings") or []))}

    def cleanup_e2e(self, verify):
        """删掉本次 e2e 自己下下来的文件。

        审计脚本不该往用户模型库里留测试垃圾；plan 阶段已确认落点原本不存在，
        所以这里只删「本模型名」开头的产物，不动目录里别的东西。
        """
        d = (verify or {}).get("dir") or ""
        stem = os.path.splitext((verify or {}).get("filename") or "")[0]
        removed = []
        if not d or not stem or not os.path.isdir(d):
            return removed
        for name in os.listdir(d):
            if not (name == stem or name.startswith(stem + ".") or name.startswith(stem + "_")):
                continue
            target = os.path.join(d, name)
            try:
                shutil.rmtree(target) if os.path.isdir(target) else os.remove(target)
                removed.append(name)
            except OSError:
                pass
        try:
            if not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
        return removed

    def e2e_small_download(self):
        """真实下载一个尽量小的模型：搜索 → 逐个 plan → 选最小的 → 下载 → 校验。"""
        best = None
        for query in ("tiny vae", "small lora test", "vae"):
            st, found, _, _ = http(self.bridge, "/api/automation/search", "POST",
                                   {"query": query, "limit": 5}, timeout=60)
            for item in (found.get("items") or []):
                for ver in (item.get("versions") or [])[:1]:
                    url = "https://civitai.red/models/%s?modelVersionId=%s" % (item.get("id"), ver.get("id"))
                    st2, plan, _, _ = http(self.bridge, "/api/automation/plan", "POST", {"url": url}, timeout=90)
                    for f in (plan.get("files") or []):
                        kb = f.get("size_kb") or 0
                        # 只挑本机还没有的：e2e 不该为了自证再下一份已有文件
                        if (f.get("folder") and not f.get("exists")
                                and 1024 <= kb <= 20 * 1024 and (best is None or kb < best[0])):
                            best = (kb, url, f.get("name"), plan.get("model", {}).get("name"))
            if best:
                break
        if not best:
            # 没有合适候选是「本轮没得测」，不是卡点：避免 e2e 把 0 候选算成阻塞
            return True, {"note": "跳过：没找到 1–20MB 且本机未有的候选（不为此白下几百 MB）"}
        kb, url, name, model_name = best
        st, out, ms, _ = http(self.bridge, "/api/automation/download", "POST",
                              {"url": url, "wait": True, "timeout": 600, "verify": True}, timeout=700)
        verify = out.get("verify") or {}
        ok = st == 200 and bool(out.get("ok")) and bool(verify.get("bundle_ok"))
        cleaned = ""
        if not self.keep_e2e:
            removed = self.cleanup_e2e(verify)
            tid = (out.get("task") or {}).get("id")
            task_gone = True
            if tid:
                _st, rm, _, _ = http(self.bridge, "/api/automation/tasks", "POST",
                                     {"action": "remove", "ids": [tid]})
                task_gone = any(r.get("ok") for r in (rm.get("results") or []))
            cleaned = "；已清理 %d 个测试文件%s（下载历史仍会留一条记录）" % (
                len(removed), "" if task_gone else "；⚠ 任务行未移除")
        return ok, {"ms": ms,
                    "note": "%s（%s，%.1fMB）→ %s；ok=%s bundle_ok=%s 等待=%.1fs%s"
                            % (name, model_name, kb / 1024.0,
                               (out.get("task") or {}).get("routed_dir") or "未分流",
                               bool(out.get("ok")), bool(verify.get("bundle_ok")),
                               out.get("waited_s") or 0, cleaned)}

    # ---------------------------------------------------------------- 跑
    def run(self, url, e2e=False):
        self.step("发现能力（health/manifest/schema）", self.discovery)
        self.step("计划与版本解析（plan/resolve）", lambda: self.plan_and_resolve(url),
                  blocked_by="网络/API 不可达")
        self.step("任务列表与字段投影", self.tasks_shape)
        self.step("待选文件状态", self.pending_choice,
                  note="有待选时 AI 必须调用 /file-choice 才能继续")
        self.step("提交入队（不等传输）", lambda: self.download_gate(url),
                  blocked_by="提交被拒（看 msg/hint）")
        self.step("校验与提示词", self.verify_and_prompts,
                  blocked_by="没有已完成任务或落点不完整")
        self.step("收尾健康度", self.postprocess_health)
        self.step("收尾重跑入口", self.repair_probe,
                  blocked_by="缺少 /tasks/repair：失败后只能分项手工重跑")
        self.step("文件操作（预览/移动/浏览）", self.file_ops,
                  blocked_by="模型库为空或接口缺失")
        self.step("批量调用（batch）", self.batch_roundtrip)
        self.step("连通性体检", self.connectivity)
        if e2e:
            self.step("真实下载小文件 → 校验", self.e2e_small_download,
                      blocked_by="下载/校验失败")
        return self.steps


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bridge", default=DEFAULT_BRIDGE)
    ap.add_argument("--url", default=SAMPLE_URL)
    ap.add_argument("--e2e", action="store_true", help="额外真实下载一个 1–20MB 的模型")
    ap.add_argument("--e2e-keep", action="store_true",
                    help="e2e 下载的文件留在模型库（默认跑完即清理）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    steps = Audit(args.bridge, keep_e2e=args.e2e_keep).run(args.url, e2e=args.e2e)
    blockers = [s for s in steps if not s["ok"]]
    result = {"ok": not blockers, "steps": steps, "blockers": [s["name"] for s in blockers],
              "total_ms": round(sum(s["ms"] for s in steps), 1)}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print("AI 调用卡点审计  bridge=%s" % args.bridge)
        print("-" * 78)
        for s in steps:
            mark = "OK  " if s["ok"] else "卡点"
            print("%s %-34s %8.0fms  %s" % (mark, s["name"], s["ms"], s["note"]))
        print("-" * 78)
        print("总计 %.0fms；阻塞项 %d 个%s" % (result["total_ms"], len(blockers),
                                          ("：" + "、".join(result["blockers"])) if blockers else ""))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
