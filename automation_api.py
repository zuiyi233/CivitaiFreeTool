# -*- coding: utf-8 -*-
"""AI / 脚本自动化接口（语义化 HTTP 面）

为什么单独一层：`/api/rpc` 已经能调到 Api 的 160+ 个方法，但对面（AI/脚本）需要自己
知道方法名、参数顺序、返回的是 JSON 字符串还是对象、什么时候该轮询。这一层只做四件事：

1. **发现**：`GET /api/automation` 给出端点清单、可写配置键、路径、任务状态词表。
2. **计划**：`POST /api/automation/plan` 干跑一次，回答「会下哪些文件、落哪个目录、
   已经存在吗、已经在队列里吗」，不产生任何写入。
3. **执行与等待**：`POST /api/automation/download` 提交并可阻塞到终态，省掉外部轮询；
   `GET /api/automation/tasks?wait=1` 同理。
4. **校验与排障**：`GET /api/automation/verify` 校验落点/哈希/附属文件；
   `GET /api/automation/logs` 给 error.log 尾部与本层调用事件。

底层动作一律复用既有 Api 方法与桥接口，不复制下载/分流逻辑。所有等待都有上限
（默认 300s，夹在 1–1800s），所有写操作显式且参数校验失败整条拒绝。
"""
import hashlib
import json
import os
import time
from collections import deque
from urllib.parse import parse_qs

MAX_WAIT_S = 1800
DEFAULT_WAIT_S = 300
TASK_TERMINAL = ("done", "error", "canceled")
SIDECAR_SUFFIXES = (".civitai.info", ".json", "_评论.txt")
# 封面：图片封面由桌面端写 <stem>.preview.png；封面是视频时桌面端写不出 png，
# 由扩展按 coverSaveMode=video 落 <stem>.mp4。少一个都不能算缺失。
PREVIEW_SUFFIXES = (".preview.png", ".preview.jpg", ".preview.webp",
                    ".cover.png", ".cover.jpg", ".preview.mp4", ".preview.webm",
                    ".mp4", ".webm")
ARCHIVE_ARTIFACTS = ("评论.txt", "example.json")

# 本层调用事件（AI 自检：我的请求到底到没到、耗时多少、返回了什么状态）
_EVENTS = deque(maxlen=200)
_EVENT_SEQ = 0


def record_event(method, path, status, ms, summary=""):
    global _EVENT_SEQ
    _EVENT_SEQ += 1
    _EVENTS.append({"seq": _EVENT_SEQ, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "method": method, "path": path, "status": status,
                    "ms": round(ms, 1), "summary": str(summary or "")[:200]})


def _jsonish(value):
    """Api 里有的方法返回 JSON 字符串（规避 js_api 大对象转换），统一解析成对象"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _int(value, default, low, high):
    try:
        n = int(value)
    except Exception:
        return default
    return max(low, min(high, n))


def _tasks_raw(api):
    """当前任务列表（含 info 里的 routed_dir / route_reason）"""
    try:
        return list(api.get_tasks() or [])
    except Exception:
        return []


def _find_task(api, task_id):
    for t in _tasks_raw(api):
        if t.get("id") == task_id:
            return t
    return None


def _verify_dir(path, filename, expected_sha256="", do_hash=False):
    """校验落点：文件存在/大小/哈希 + 附属文件清单（只读，不移动任何东西）"""
    out = {"ok": False, "dir": path or "", "filename": filename or "",
           "exists": False, "size": 0, "expected_sha256": expected_sha256 or "",
           "sha256": "", "sha256_ok": None, "preview": "",
           "sidecars": {"found": [], "missing": []},
           "artifacts": {"found": [], "missing": []},
           "images": {"exists": False, "count": 0}}
    if not path or not os.path.isdir(path):
        out["error"] = "落点目录不存在"
        return out
    target = os.path.join(path, filename) if filename else ""
    if target and os.path.isfile(target):
        out["exists"] = True
        out["size"] = os.path.getsize(target)
        if do_hash:
            h = hashlib.sha256()
            try:
                with open(target, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                out["sha256"] = h.hexdigest().upper()
                if expected_sha256:
                    out["sha256_ok"] = out["sha256"].lower() == str(expected_sha256).lower()
            except OSError as e:
                out["error"] = "读取文件失败: %s" % e
    elif target:
        out["error"] = "模型文件不存在"

    stem = os.path.splitext(filename)[0] if filename else ""
    if stem:
        names = set(os.listdir(path))
        for suf in SIDECAR_SUFFIXES:
            key = stem + suf
            (out["sidecars"]["found"] if key in names else out["sidecars"]["missing"]).append(key)
        out["preview"] = next((stem + s for s in PREVIEW_SUFFIXES if stem + s in names), "")
        out["sidecars"]["missing"] += [] if out["preview"] else [stem + ".preview.png|.mp4"]
        img_dir = stem + ".images"
        out["images"]["exists"] = img_dir in names and os.path.isdir(os.path.join(path, img_dir))
        if out["images"]["exists"]:
            try:
                out["images"]["count"] = len([n for n in os.listdir(os.path.join(path, img_dir))
                                              if not n.endswith(".tmp")])
            except OSError:
                pass
    for name in ARCHIVE_ARTIFACTS:
        if os.path.isfile(os.path.join(path, name)):
            out["artifacts"]["found"].append(name)
        else:
            out["artifacts"]["missing"].append(name)

    out["ok"] = bool(out["exists"] and not out["sidecars"]["missing"]
                     and (out["sha256_ok"] is not False))
    return out


# ---------------------------------------------------------------- 各端点

def _manifest(api, ctx):
    return 200, {
        "ok": True, "app": "CivitaiFreeTool", "version": ctx.get("version", ""),
        "rpc": "POST /api/rpc {method, args:[...]}（长尾方法走这里，见 rpc_methods）",
        "endpoints": [
            {"method": "GET", "path": "/api/automation", "desc": "能力清单（本表）"},
            {"method": "GET", "path": "/api/automation/schema",
             "desc": "自描述：配置键类型/默认值/可选值 + 词表 + 类型目录映射"},
            {"method": "GET", "path": "/api/automation/state", "desc": "配置 + 最近任务 + 待选文件"},
            {"method": "GET", "path": "/api/automation/tasks",
             "params": {"ids": "csv", "status": "csv", "limit": "int", "wait": "0|1", "timeout": "s", "since": "ms"},
             "desc": "任务列表；wait=1 阻塞到终态"},
            {"method": "POST", "path": "/api/automation/tasks",
             "params": {"action": "pause|resume|retry|start|remove|archive", "ids": "[str]", "all": "bool"},
             "desc": "批量任务控制，逐项返回结果"},
            {"method": "GET", "path": "/api/automation/events",
             "params": {"since": "int", "limit": "int"},
             "desc": "本层调用事件流（自检：请求到没到、多久、什么状态）"},
            {"method": "GET", "path": "/api/automation/history",
             "params": {"limit": "int", "status": "str", "detail": "0|1"},
             "desc": "已完成/失败/取消的下载历史（倒序）"},
            {"method": "GET", "path": "/api/automation/prompts",
             "params": {"path": "str", "limit": "int"},
             "desc": "示例图提示词与生成参数（复现用）"},
            {"method": "POST", "path": "/api/automation/plan",
             "params": {"url": "str", "model_id": "int?", "version_id": "int?", "file_id": "int?", "probe": "0|1"},
             "desc": "干跑：解析文件/落点/是否已存在或在队列，不写入"},
            {"method": "POST", "path": "/api/automation/resolve",
             "params": {"url": "str?", "model_id": "int?", "version_id": "int?"},
             "desc": "只读解析：模型 + 全部版本 + 选定版本文件 + 落点 + 库内状态"},
            {"method": "POST", "path": "/api/automation/download",
             "params": {"url": "str?", "urls": "[str]?", "model_id": "int?", "version_id": "int?",
                        "file_ids": "[int]?", "dest_subdir": "str?", "caps": "[str]?",
                        "wait": "0|1", "timeout": "s", "verify": "0|1"},
             "desc": "提交下载（默认 caps=[bundle] 以启用分流），支持批量，可阻塞到终态并顺带校验"},
            {"method": "GET", "path": "/api/automation/verify",
             "params": {"task_id": "str?", "path": "str?", "hash": "0|1"},
             "desc": "校验模型文件/哈希/附属文件（视频封面也算封面）"},
            {"method": "GET", "path": "/api/automation/config", "desc": "全量配置（密钥打码）+ 数据目录"},
            {"method": "POST", "path": "/api/automation/config", "params": {"config": "object"},
             "desc": "白名单配置写入"},
            {"method": "GET", "path": "/api/automation/storage",
             "desc": "落点目录存在/可写/剩余空间 + ComfyUI 类型目录清单"},
            {"method": "GET", "path": "/api/automation/logs",
             "params": {"source": "error|events|both", "tail": "int"},
             "desc": "error.log 尾部 + 本层调用事件"},
            {"method": "GET", "path": "/api/automation/library",
             "params": {"q": "str", "type": "str", "limit": "int", "offset": "int"},
             "desc": "模型库行数据（已解析成对象）"},
            {"method": "POST", "path": "/api/automation/library/op",
             "params": {"op": "|".join(_MM_OPS), "paths": "[str]?", "wait": "0|1",
                        "timeout": "s", "overwrite": "bool?", "force": "bool?", "confirm": "bool?"},
             "desc": "模型管理动作（校验/查重/汉化/生成 json/检查更新/更新/封面…）可等待完成"},
            {"method": "GET", "path": "/api/automation/library/op", "params": {"op": "str"},
             "desc": "上述后台动作的进度"},
            {"method": "POST", "path": "/api/automation/images/download",
             "params": {"path": "str", "wait": "0|1", "timeout": "s"},
             "desc": "批量下载示例图到 <名>.images/（异步启动，可等待）"},
            {"method": "GET", "path": "/api/automation/images/state", "params": {"path": "str?"},
             "desc": "示例图下载 / 补帧进度"},
            {"method": "POST", "path": "/api/automation/workflow/analyze",
             "params": {"path": "str", "full": "0|1"},
             "desc": "解析 ComfyUI 工作流/生成参数 + 本地模型匹配（默认不带 base64 预览）"},
            {"method": "GET", "path": "/api/automation/model", "params": {"path": "str"},
             "desc": "单个模型详情（info + 画廊）+ 附属文件清单"},
            {"method": "POST", "path": "/api/automation/file-choice",
             "params": {"identity": "str", "indices": "[int]?", "pause": "bool", "ack": "bool"},
             "desc": "回答/确认待选文件（identity 来自 state.pending_file_choice.id）"},
            {"method": "POST", "path": "/api/automation/search",
             "params": {"query": "str", "types": "str?", "base_models": "str?",
                        "sort": "str?", "period": "str?", "nsfw": "bool?", "limit": "1-20"},
             "desc": "C 站公开模型搜索（只读）"},
            {"method": "POST", "path": "/api/automation/library/scan",
             "params": {"wait": "0|1", "timeout": "s"}, "desc": "触发模型扫描并可等待完成"},
            {"method": "GET", "path": "/api/automation/extension",
             "desc": "浏览器扩展通道状态（最近提交/令牌/归档写盘计数）"},
            {"method": "GET", "path": "/api/automation/connectivity",
             "desc": "连通性体检：DNS（含污染特征）/ 公开 API / 代理配置"},
        ],
        "config_keys_writable": list(ctx.get("config_keys") or []),
        "task_states": ["pending", "downloading", "paused", "done", "error", "canceled"],
        "paths": {"data_dir": ctx.get("data_dir", ""), "error_log": ctx.get("error_log", "")},
        "rpc_methods_count": len(ctx.get("rpc_methods", []) or []),
    }


def _state(api, ctx):
    return 200, ctx["state"]()


def _tasks(api, ctx, query):
    ids = [s for s in (query.get("ids", [""])[0] or "").split(",") if s.strip()]
    statuses = [s for s in (query.get("status", [""])[0] or "").split(",") if s.strip()]
    limit = _int(query.get("limit", ["50"])[0], 50, 1, 500)
    wait = str(query.get("wait", ["0"])[0]).lower() in ("1", "true", "yes")
    timeout = _int(query.get("timeout", [DEFAULT_WAIT_S])[0], DEFAULT_WAIT_S, 1, MAX_WAIT_S)
    since = query.get("since", [""])[0]

    def snapshot():
        rows = _tasks_raw(api)
        if ids:
            rows = [t for t in rows if t.get("id") in set(ids)]
        if statuses:
            rows = [t for t in rows if t.get("status") in set(statuses)]
        if since:
            try:
                rows = [t for t in rows if float(t.get("created_at") or 0) >= float(since)]
            except Exception:
                pass
        return rows[-limit:]

    waited = 0.0
    timed_out = False
    if wait:
        t0 = time.time()
        while True:
            rows = snapshot()
            if ids:
                done = rows and all(t.get("status") in TASK_TERMINAL for t in rows)
            else:
                done = bool(rows) and not any(t.get("status") in ("pending", "downloading") for t in rows)
            if done:
                break
            if time.time() - t0 >= timeout:
                timed_out = True
                break
            time.sleep(0.25)
        waited = round(time.time() - t0, 1)
    rows = snapshot()
    return 200, {"ok": True, "tasks": rows, "count": len(rows),
                 "waited_s": waited, "timed_out": timed_out}


def _tasks_action(api, ctx, body):
    action = str((body or {}).get("action") or "").strip()
    ids = (body or {}).get("ids") or []
    all_flag = bool((body or {}).get("all"))
    if action not in ("pause", "resume", "retry", "start", "remove", "archive"):
        return 400, {"ok": False, "msg": "action 必须是 pause|resume|retry|start|remove|archive"}
    if not all_flag and not isinstance(ids, list):
        return 400, {"ok": False, "msg": "ids 必须是数组（或传 all:true）"}
    ids = [str(i) for i in ids]

    results = []
    if all_flag:
        mapping = {"pause": "pause_all", "start": "start_all", "resume": "start_all", "remove": "remove_all"}
        mapped = mapping.get(action)
        if mapped is None:
            return 400, {"ok": False, "msg": "all:true 仅支持 pause/resume/start/remove"}
        ok = api.dl_action(mapped, None, None)
        results.append({"scope": "all", "ok": bool(ok)})
    else:
        for tid in ids:
            try:
                if action == "remove":
                    r = api.remove_download_task(tid)
                elif action == "archive":
                    r = api.archive_download_task(tid)
                else:
                    r = api.dl_action(action, None, [tid])
                results.append({"id": tid, "ok": bool(r) and r is not False,
                                "result": _jsonish(r) if not isinstance(r, bool) else r})
            except Exception as e:
                results.append({"id": tid, "ok": False, "error": str(e)[:200]})
    return 200, {"ok": all(r.get("ok") for r in results) if results else False, "results": results}


def _resolve_target(civ, body, allow_first_version=True):
    """url 或 model_id(+version_id) → (model_id, version_id, version, model, files, error)

    只读：不下载、不建目录。url 里的 modelVersionId 优先，其次显式 version_id，
    最后才退回该模型的第一个版本（allow_first_version=False 时直接报错）。
    """
    b = body or {}
    url = str(b.get("url") or "").strip()
    model_id = b.get("model_id")
    version_id = b.get("version_id")
    if not url and not model_id and not version_id:
        return None, None, None, None, [], "url 或 model_id/version_id 至少给一个"
    try:
        if url:
            mid, vid = civ.resolve_url(url)
            model_id = model_id if model_id is not None else mid
            version_id = version_id if version_id is not None else vid
        if not version_id:
            if model_id is None:
                return None, None, None, None, [], "URL 里没有版本信息，也没有给 model_id"
            versions = (civ.get_model(model_id) or {}).get("modelVersions") or []
            if not versions:
                return None, None, None, None, [], "模型没有可用版本"
            if not allow_first_version:
                return None, None, None, None, [], "必须指定 version_id"
            version_id = versions[0]["id"]
        version = civ.get_model_version(version_id)
        if model_id is None:
            mid = version.get("modelId")
            if mid is not None and str(mid).strip().isdigit():
                model_id = str(mid).strip()
        model = civ.get_model(model_id) if model_id is not None else {}
        return model_id, version_id, version, model, civ.model_files(version), ""
    except Exception as e:
        return None, None, None, None, [], "解析失败: %s" % str(e)[:200]


def _plan(api, ctx, body):
    """干跑：不写盘、不入队。probe=1 才做 safetensors 头嗅探（两次 Range 网络请求）。"""
    import model_naming
    import model_routing

    civ = getattr(api, "api", None)
    if civ is None:
        return 500, {"ok": False, "msg": "API 未就绪"}
    model_id, version_id, version, model, files, err = _resolve_target(civ, body)
    if err:
        return 400, {"ok": False, "msg": err}
    file_id = (body or {}).get("file_id")
    if file_id is not None:
        files = [f for f in files if str(f.get("id")) == str(file_id)]
        if not files:
            return 400, {"ok": False, "msg": "指定文件不属于这个版本"}

    root = str((api.cfg or {}).get("comfyui_models_root") or "").strip()
    route_on = bool((api.cfg or {}).get("route_by_type"))
    probe = bool((body or {}).get("probe"))
    items = []
    for f in files:
        base = model_naming.download_name(api.cfg, f, model.get("name") or "",
                                          version.get("name", ""), multi=len(files) > 1)
        decision = model_routing.classify(model.get("type"), f.get("type"), version.get("baseModel"))
        if probe and api._needs_header_probe(base, f.get("type"), decision):
            header = api._sniff_safetensors_header(version, f)
            if header:
                decision = model_routing.classify(model.get("type"), f.get("type"),
                                                  version.get("baseModel"), header)
        folder = decision.folder
        bundle = model_routing.bundle_name_for(os.path.splitext(base)[0], "")
        dest = model_routing.resolve_dest_dir(root, folder, bundle) if (route_on and folder) else None
        item = {
            "name": base, "file_id": f.get("id"), "type": f.get("type"),
            "primary": bool(f.get("primary")), "size_kb": f.get("sizeKB"),
            "folder": folder or "", "bundle": bundle,
            "routed_dir": dest or "", "route_reason": decision.reason,
            "route_source": decision.source,
            "would_route": bool(dest),
            "exists": bool(dest and os.path.exists(os.path.join(dest, base))),
            "queued": False,
        }
        try:
            item["download_url"] = civ.file_download_url(version, f)
        except Exception as e:
            item["download_url"] = ""
            item["download_url_error"] = str(e)[:120]
        try:
            item["queued"] = bool(api._dup_in_queue(version_id, (f.get("hashes") or {}).get("SHA256"),
                                                    base, dest or ""))
        except Exception:
            pass
        items.append(item)

    try:
        lib = api.library_check(version_id)
    except Exception:
        lib = {}
    return 200, {
        "ok": True, "url": str((body or {}).get("url") or ""),
        "model": {"id": model_id, "name": model.get("name"), "type": model.get("type")},
        "version": {"id": version_id, "name": version.get("name"),
                    "base_model": version.get("baseModel")},
        "routing": {"enabled": route_on, "models_root": root},
        "in_library": bool((lib or {}).get("found")),
        "library_paths": list((lib or {}).get("paths") or []),
        "files": items,
    }


def _wait_new_tasks(api, before_ids, timeout):
    """等「本次提交产生的全部新任务」到终态；超时返回当前快照（不抛异常）"""
    t0 = time.time()
    while True:
        new = [t for t in _tasks_raw(api) if t.get("id") not in before_ids]
        if new and all(t.get("status") in TASK_TERMINAL for t in new):
            return new, round(time.time() - t0, 1), False
        if time.time() - t0 >= timeout:
            return new, round(time.time() - t0, 1), True
        time.sleep(0.25)


def _wait_new_task(api, before_ids, timeout):
    """等「本次提交产生的新任务」到终态；超时返回当前快照"""
    tasks, waited, timed_out = _wait_new_tasks(api, before_ids, timeout)
    return (tasks[-1] if tasks else None), waited, timed_out


def _download_targets(api, b):
    """把 url / urls / model_id+version_id(+file_ids) 归一成待提交的 URL 列表"""
    urls = b.get("urls")
    if urls is not None:
        if b.get("file_ids"):
            return [], "file_ids 不能和 urls 同时用（文件 ID 只属于某一个版本）"
        if not isinstance(urls, list) or not urls:
            return [], "urls 必须是非空数组"
        clean = [{"url": str(u).strip()} for u in urls if str(u or "").strip()]
        return (clean, "") if clean else ([], "urls 里没有有效链接")
    url = str(b.get("url") or "").strip()
    model_id, version_id = b.get("model_id"), b.get("version_id")
    if not url and model_id is None and version_id is None:
        return [], "url / urls / model_id+version_id 至少给一个"
    if not url:
        try:
            url = api._site_url(model_id, version_id)
        except Exception as e:
            return [], "构造下载链接失败: %s" % str(e)[:120]
    file_ids = b.get("file_ids")
    if not file_ids:
        return [{"url": url}], ""
    if not isinstance(file_ids, list):
        return [], "file_ids 必须是数组"
    sep = "&" if "?" in url else "?"
    try:
        return [{"url": "%s%sfileId=%s" % (url, sep, int(fid))} for fid in file_ids], ""
    except (TypeError, ValueError):
        return [], "file_ids 里必须都是数字"


def _download(api, ctx, body):
    b = body or {}
    targets, err = _download_targets(api, b)
    if err:
        return 400, {"ok": False, "msg": err}
    dest_subdir = str(b.get("dest_subdir") or "")
    caps = b.get("caps")
    if caps is None:
        caps = ["bundle"]          # 自动化默认开启分流：本体与附属文件才会进同一个类型目录
    if not isinstance(caps, list):
        return 400, {"ok": False, "msg": "caps 必须是数组"}
    wait = str(b.get("wait", "1")).lower() not in ("0", "false", "no")
    timeout = _int(b.get("timeout", DEFAULT_WAIT_S), DEFAULT_WAIT_S, 1, MAX_WAIT_S)
    want_verify = str(b.get("verify", "1")).lower() not in ("0", "false", "no")

    before = {t.get("id") for t in _tasks_raw(api)}
    items, expect_task = [], False
    for t in targets:
        try:
            submit = _jsonish(api.dl_submit_url_from_bridge(t["url"], dest_subdir, caps)) or {}
        except Exception as e:
            items.append({"url": t["url"], "ok": False, "error": "提交失败: %s" % str(e)[:200]})
            continue
        if not isinstance(submit, dict):
            submit = {"ok": True, "raw": submit}
        accepted = bool(submit.get("ok")) if "ok" in submit else bool(submit.get("started"))
        expect_task = expect_task or accepted
        items.append({"url": t["url"], "ok": accepted, "submit": submit})

    tasks, waited, timed_out = [], 0.0, False
    if wait and expect_task:
        tasks, waited, timed_out = _wait_new_tasks(api, before, timeout)
    for idx, task in enumerate(tasks):
        if idx < len(items):
            items[idx]["task"] = task
            if task.get("status") == "done" and want_verify:
                items[idx]["verify"] = _verify_dir(
                    task.get("routed_dir") or task.get("dest_dir") or "",
                    task.get("filename") or "", "", False)
                if not items[idx]["verify"].get("ok"):
                    items[idx]["ok"] = False

    single = len(items) == 1
    if single:
        r = items[0]
        if r.get("error"):
            return 500, {"ok": False, "msg": r["error"]}
        if not r.get("submit", {}).get("ok") and not r.get("submit", {}).get("started"):
            return 400, {"ok": False, "submit": r.get("submit"), "items": items,
                         "msg": (r.get("submit") or {}).get("msg") or "提交被拒绝"}
        out = {"ok": bool(r.get("ok")), "submit": r.get("submit"), "waited_s": waited,
               "timed_out": timed_out, "task": r.get("task"), "items": items}
        if r.get("verify") is not None:
            out["verify"] = r["verify"]
            out["ok"] = bool(out["ok"] and r["verify"].get("ok"))
        return 200, out

    return 200, {"ok": all(i.get("ok") for i in items) if items else False,
                 "items": items, "count": len(items),
                 "waited_s": waited, "timed_out": timed_out}


def _verify(api, ctx, query):
    task_id = (query.get("task_id", [""])[0] or "").strip()
    path = (query.get("path", [""])[0] or "").strip()
    do_hash = str(query.get("hash", ["0"])[0]).lower() in ("1", "true", "yes")
    if task_id:
        task = _find_task(api, task_id)
        if not task:
            return 404, {"ok": False, "msg": "任务不存在"}
        dest = task.get("routed_dir") or task.get("dest_dir") or ""
        info = (task.get("info") or {}) if isinstance(task.get("info"), dict) else {}
        expected = info.get("expected_sha256") or task.get("expected_sha256") or ""
        return 200, _verify_dir(dest, task.get("filename") or "", expected, do_hash)
    if path:
        if os.path.isdir(path):
            # 目录形态：自动挑最大的权重文件当本体（多文件版本取主文件，体积最大最稳）
            weights = []
            try:
                for n in os.listdir(path):
                    if n.lower().endswith((".safetensors", ".ckpt", ".pt", ".pth", ".gguf", ".bin")):
                        weights.append((os.path.getsize(os.path.join(path, n)), n))
            except OSError:
                weights = []
            weights.sort(reverse=True)
            return 200, _verify_dir(path, weights[0][1] if weights else "", "", do_hash)
        return 200, _verify_dir(os.path.dirname(path), os.path.basename(path), "", do_hash)
    return 400, {"ok": False, "msg": "需要 task_id 或 path"}


def _config_get(api, ctx):
    cfg = dict(getattr(api, "cfg", None) or {})
    for k in ("api_key", "baidu_key", "baidu_appid"):
        if cfg.get(k):
            cfg[k] = "***"
    try:
        storage = api.get_data_storage_info()
    except Exception:
        storage = {}
    return 200, {"ok": True, "config": cfg, "writable_keys": list(ctx.get("config_keys") or []),
                 "storage": storage}


def _logs(api, ctx, query):
    source = (query.get("source", ["both"])[0] or "both").lower()
    tail = _int(query.get("tail", ["80"])[0], 80, 1, 2000)
    out = {"ok": True}
    if source in ("error", "both"):
        path = ""
        try:
            path = api.error_log_path()
        except Exception:
            pass
        lines = []
        if path and os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    size = f.tell()
                    f.seek(max(0, size - 128 * 1024))
                    lines = f.read().decode("utf-8", "replace").splitlines()[-tail:]
            except OSError as e:
                out["error_log_read_error"] = str(e)
        out["error_log"] = {"path": path, "lines": lines}
    if source in ("events", "both"):
        out["events"] = list(_EVENTS)[-tail:]
    return 200, out


def _library(api, ctx, query):
    rows = _jsonish(api.get_scan_rows())
    if not isinstance(rows, list):
        rows = []
    q = (query.get("q", [""])[0] or "").strip().lower()
    typ = (query.get("type", [""])[0] or "").strip().lower()
    limit = _int(query.get("limit", ["100"])[0], 100, 1, 2000)
    offset = _int(query.get("offset", ["0"])[0], 0, 0, 1000000)
    if q:
        rows = [r for r in rows if q in str(r.get("name", "")).lower()
                or q in str(r.get("path", "")).lower()]
    if typ:
        rows = [r for r in rows if typ == str(r.get("type_filter", "")).lower()
                or typ == str(r.get("type", "")).lower()]
    total = len(rows)
    page = [{"path": r.get("path"), "name": r.get("name"), "type": r.get("type"),
             "type_filter": r.get("type_filter"), "base": r.get("base"),
             "size": r.get("size"), "ver": r.get("ver"), "verId": r.get("verId"),
             "modelId": r.get("modelId")} for r in rows[offset:offset + limit]]
    state = {}
    try:
        state = api.get_scan_state()
    except Exception:
        pass
    return 200, {"ok": True, "total": total, "offset": offset, "limit": limit,
                 "rows": page, "scan": state}


def _library_scan(api, ctx, body):
    b = body or {}
    wait = str(b.get("wait", "1")).lower() not in ("0", "false", "no")
    timeout = _int(b.get("timeout", DEFAULT_WAIT_S), DEFAULT_WAIT_S, 1, MAX_WAIT_S)
    started = _jsonish(api.scan_models()) or {}
    if not wait:
        return 200, {"ok": True, "started": started}
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = api.get_scan_state() or {}
        if not st.get("running"):
            return 200, {"ok": True, "started": started, "scan": st,
                         "waited_s": round(time.time() - t0, 1), "timed_out": False}
        time.sleep(0.25)
    return 200, {"ok": True, "started": started, "scan": api.get_scan_state(),
                 "waited_s": round(time.time() - t0, 1), "timed_out": True}


def _system_proxy():
    """读系统代理（Windows 走 WinINET 注册表，其它平台看环境变量）。

    为什么体检要看它：浏览器走系统代理，Python/urllib 默认**不走**。系统代理开着而应用没开，
    就会出现「浏览器能下载、桌面端一律超时」——本机实测就是这么被误导过的。
    """
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enabled = bool(winreg.QueryValueEx(key, "ProxyEnable")[0])
        server = ""
        if enabled:
            try:
                server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "")
            except OSError:
                server = ""
        return {"enabled": enabled, "server": server}
    except Exception:
        pass
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(var)
        if v:
            return {"enabled": True, "server": v}
    return {"enabled": False, "server": ""}


def _connectivity(api, ctx, query):
    """连通性体检：DNS / 公开 API / 代理（含系统代理对比），一条请求回答「网络还是代码」。

    实测价值：本机 civitai.com 与 b2.civitai.com 被 DNS 污染（解析到 199.59.148.x / …face:b00c…），
    且系统代理开着而应用没开时，浏览器能下、桌面端一律 10060 超时。没有这个体检就只能逐个 curl 比对。
    """
    import socket
    import time as _time
    cfg = getattr(api, "cfg", None) or {}
    site = str(cfg.get("site_domain") or "").strip()
    host = site.replace("https://", "").replace("http://", "").split("/")[0] or "civitai.com"
    out = {"ok": True, "site_domain": site or host,
           "proxy": {"enabled": bool(cfg.get("proxy_enabled")), "address": cfg.get("proxy_address") or ""},
           "system_proxy": _system_proxy(), "dns": {}, "api": {}, "warnings": []}
    for h in (host, "b2.civitai.com", "image.civitai.com"):
        t0 = _time.time()
        try:
            addrs = sorted({ai[4][0] for ai in socket.getaddrinfo(h, 443, proto=socket.IPPROTO_TCP)})
            # 污染特征（只作提示，不直接判失败）：Facebook 网段 / face:b00c
            polluted = any(a.startswith(("199.59.148.", "157.240.", "31.13.", "179.60."))
                           or "face:b00c" in a or a.startswith("2a03:2880:") for a in addrs)
            out["dns"][h] = {"ok": True, "addrs": addrs[:6], "suspicious": polluted,
                             "ms": round((_time.time() - t0) * 1000, 1)}
        except Exception as e:
            out["dns"][h] = {"ok": False, "error": str(e)[:120],
                             "ms": round((_time.time() - t0) * 1000, 1)}
    civ = getattr(api, "api", None)
    if civ is not None:
        t0 = _time.time()
        try:
            civ._get("/models", {"limit": 1}, retries=1)
            out["api"] = {"ok": True, "base": getattr(civ, "api_base", ""),
                          "ms": round((_time.time() - t0) * 1000, 1)}
        except Exception as e:
            out["api"] = {"ok": False, "base": getattr(civ, "api_base", ""),
                          "error": str(e)[:200], "ms": round((_time.time() - t0) * 1000, 1)}
    sysp = out["system_proxy"]
    if sysp.get("enabled") and not out["proxy"]["enabled"]:
        out["warnings"].append(
            "系统代理已开启（%s）但应用未启用代理：浏览器能下载、桌面端会超时。"
            "可用 POST /api/automation/config 设 proxy_enabled=true 且 proxy_address 与系统代理一致。"
            % (sysp.get("server") or "?"))
    if any(v.get("suspicious") for v in out["dns"].values()):
        out["warnings"].append("检测到疑似 DNS 污染的解析结果：请确认代理已开启（远端 DNS），"
                               "否则文件 CDN（b2.civitai.com）会连接超时。")
    out["ok"] = bool(out["api"].get("ok")) and bool(out["dns"].get(host, {}).get("ok"))
    if not out["ok"]:
        out.setdefault("hint", "公开 API 不通：先看 proxy 与 system_proxy 是否一致，再看 warnings")
    return 200, out


def _extension(api, ctx):
    fn = ctx.get("extension")
    data = fn() if callable(fn) else {}
    return 200, {"ok": True, **(data or {})}


def _file_choice(api, ctx, body):
    """回答/确认待选文件：identity 来自 /api/automation/state 的 pending_file_choice"""
    b = body or {}
    identity = str(b.get("identity") or "").strip()
    if not identity:
        return 400, {"ok": False, "msg": "identity 不能为空（见 state.pending_file_choice.id）"}
    try:
        if b.get("ack"):
            r = api.acknowledge_download_file_choice(identity)
        else:
            indices = b.get("indices")
            if indices is not None and not isinstance(indices, list):
                return 400, {"ok": False, "msg": "indices 必须是数组"}
            r = api.respond_download_file_choice(identity, indices, bool(b.get("pause")))
    except Exception as e:
        return 500, {"ok": False, "msg": "处理失败: %s" % str(e)[:200]}
    r = _jsonish(r) or {}
    return (200 if r.get("ok") else 400), r


def _model_detail(api, ctx, query):
    path = (query.get("path", [""])[0] or "").strip()
    if not path:
        return 400, {"ok": False, "msg": "需要 path"}
    if not os.path.isfile(path):
        return 404, {"ok": False, "msg": "文件不存在: %s" % path}
    try:
        detail = _jsonish(api.get_model_detail(path))
    except Exception as e:
        return 500, {"ok": False, "msg": "读取详情失败: %s" % str(e)[:200]}
    if not isinstance(detail, dict):
        detail = {"raw": detail}
    v = _verify_dir(os.path.dirname(path), os.path.basename(path))
    return 200, {"ok": True, "path": path, "detail": detail,
                 "files": {"exists": v["exists"], "size": v["size"],
                           "sidecars": v["sidecars"], "images": v["images"],
                           "artifacts": v["artifacts"]}}


def _search(api, ctx, body):
    """C 站公开模型搜索（只读，limit ≤ 20）。站点根跟随 site_domain。"""
    b = body or {}
    civ = getattr(api, "api", None)
    if civ is None:
        return 500, {"ok": False, "msg": "API 未就绪"}
    limit = _int(b.get("limit", 10), 10, 1, 20)
    params = {"limit": limit}
    q = str(b.get("query") or "").strip()
    if q:
        params["query"] = q
    for key, api_key in (("types", "types"), ("base_models", "baseModels"),
                         ("sort", "sort"), ("period", "period"), ("nsfw", "nsfw")):
        v = b.get(key)
        if v not in (None, "", []):
            params[api_key] = v
    try:
        data = civ._get("/models", params)
    except Exception as e:
        return 400, {"ok": False, "msg": "搜索失败: %s" % str(e)[:200]}
    items = []
    for m in (data.get("items") or [])[:limit]:
        items.append({
            "id": m.get("id"), "name": m.get("name"), "type": m.get("type"),
            "nsfw": m.get("nsfw"), "stats": m.get("stats"),
            "versions": [{"id": v.get("id"), "name": v.get("name"), "baseModel": v.get("baseModel")}
                         for v in (m.get("modelVersions") or [])[:5]],
        })
    meta = data.get("metadata") or {}
    return 200, {"ok": True, "total": meta.get("totalItems"), "items": items}


# ---------------------------------------------------------------- 扩展覆盖
# 下面这些端点不新增底层能力，只把「模型管理 / 资源下载 / 工作流分析」里已有的
# 动作整理成 AI 能一次看懂、一次调完的形状（含可等待的进度与结果）。

_CONFIG_HINTS = {
    "route_by_type": ("bool", "", "按类型分流到 <comfyui_models_root>/<类型key>/<文件名主干>/"),
    "comfyui_models_root": ("path", "", "ComfyUI 的 models 根目录；空或不存在 = 不分流"),
    "download_target_dir": ("path", "", "下载落地目录；空 = 用 download_dir"),
    "download_name_mode": ("enum", ["original", "civitai", "chinese"], "模型文件命名来源"),
    "multi_file_download": ("enum", ["ask", "first", "all"], "一个版本有多个文件时的默认策略"),
    "filename_include_version": ("bool", "", "文件名是否带原始版本号"),
    "max_concurrent_downloads": ("int", [1, 32], "并发下载数"),
    "download_retry": ("int", [0, 50], "断点续传重试次数"),
    "proxy_enabled": ("bool", "", "应用是否走 HTTP 代理"),
    "proxy_address": ("str", "", "代理地址 host:port"),
    "ssl_verify": ("bool", "", "TLS 证书校验（MITM 代理时可关）"),
    "site_domain": ("str", "", "站点根域名（决定 API 与下载链接的 host）"),
    "gen_metadata": ("bool", "", "下载后生成 .civitai.info / .json"),
    "download_cover": ("bool", "", "下载后保存封面"),
    "archive_comments": ("bool", "", "下载后抓取评论"),
    "translate_filename": ("bool", "", "下载时把模型名翻成中文作文件名"),
    "max_images_per_model": ("int", [0, 200], "每个模型最多保存的示例图数"),
    "images_dir_autofill": ("bool", "", "详情打开后后台补齐 <名>.images/"),
    "metadata_format": ("enum", ["sd", "civitai", "both"], "生成的元数据格式"),
    "target_env": ("enum", ["", "webui", "comfyui"], "整理目标环境"),
    "organize_mode": ("enum", ["manual", "civitai", "rules"], "整理分类模式"),
}

_MM_OPS = {
    "verify": ("mm_verify", "mm", "校验哈希 + 补全 C 站元数据"),
    "dedupe": ("mm_dedupe", "mm", "SHA256 查重（只算不删）"),
    "dedupe-delete": ("mm_dedupe_delete", "mm", "把指定重复文件移入回收站（需 confirm:true）"),
    "localize": ("mm_localize", "mm", "汉化文件名（需要百度翻译密钥）"),
    "gen-json": ("mm_gen_json", "mm", "生成 WebUI 可读的 SD json"),
    "download-covers": ("mm_download_covers", "mm", "下载缺失封面"),
    "check-updates": ("mm_check_updates", "upd", "检查 C 站更新（force 可绕过 24h 缓存）"),
    "update": ("mm_update_models", "mm", "把「有新版」的模型加入下载队列"),
    "sync-local": ("sync_local_model_updates", "sync", "按本地文件清理失效的更新记录"),
}

_SECRET_KEYS = ("api_key", "baidu_key", "baidu_appid")


def _schema(api, ctx):
    """自描述：可写配置键的类型/默认值/可选值 + 词表 + 类型目录映射。

    AI 不必猜 `route_by_type` 是布尔还是字符串，也不必去读源码里的枚举。
    """
    import config
    import model_routing
    cfg = getattr(api, "cfg", None) or {}
    keys = {}
    for k in list(ctx.get("config_keys") or []):
        default = config.DEFAULTS.get(k)
        typ, options, desc = _CONFIG_HINTS.get(
            k, (type(default).__name__ if default is not None else "any", "", ""))
        current = "***" if k in _SECRET_KEYS and cfg.get(k) else cfg.get(k)
        keys[k] = {"type": typ, "options": options, "desc": desc,
                   "default": default, "current": current}
    return 200, {
        "ok": True,
        "config": keys,
        "task_states": ["pending", "downloading", "paused", "done", "error", "canceled"],
        "task_actions": ["pause", "resume", "retry", "start", "remove", "archive"],
        "library_ops": {k: {"desc": v[2], "waitable": True} for k, v in _MM_OPS.items()},
        "caps": {"bundle": "客户端能接收落点令牌，允许分流并写附属文件"},
        "routing": {
            "models_root": str(cfg.get("comfyui_models_root") or ""),
            "enabled": bool(cfg.get("route_by_type")),
            "core_folders": sorted(model_routing.CORE_FOLDERS),
            "type_to_folder": dict(model_routing.COMFYUI_TYPE_DIRS),
        },
        "sidecars": {"required": list(SIDECAR_SUFFIXES),
                     "preview_any_of": list(PREVIEW_SUFFIXES),
                     "archive": list(ARCHIVE_ARTIFACTS)},
    }


def _resolve(api, ctx, body):
    """只读解析：url 或 model_id → 模型、全部版本、选定版本的文件、落点、库内状态。

    比 /plan 多一层「版本清单」，AI 可以先挑版本再决定下哪个，不用先下载再后悔。
    """
    civ = getattr(api, "api", None)
    if civ is None:
        return 500, {"ok": False, "msg": "API 未就绪"}
    status, plan = _plan(api, ctx, body)
    if status != 200:
        return status, plan
    model_id = (plan.get("model") or {}).get("id")
    versions = []
    try:
        model = civ.get_model(model_id) if model_id is not None else {}
        for v in (model.get("modelVersions") or []):
            files = v.get("files") or []
            versions.append({
                "id": v.get("id"), "name": v.get("name"),
                "base_model": v.get("baseModel"),
                "published_at": v.get("publishedAt"),
                "file_count": len(files),
                "primary_size_kb": next((f.get("sizeKB") for f in files if f.get("primary")), None),
            })
    except Exception as e:
        plan["versions_error"] = str(e)[:120]
    return 200, {**plan, "ok": True, "versions": versions}


def _events(api, ctx, query):
    """本层调用事件流：带上 since=上次的 next_since，只拿新增的那几条。"""
    since = _int(query.get("since", ["0"])[0], 0, 0, 10 ** 12)
    limit = _int(query.get("limit", ["100"])[0], 100, 1, 200)
    rows = [e for e in list(_EVENTS) if e.get("seq", 0) > since][-limit:]
    return 200, {"ok": True, "events": rows, "last_seq": _EVENT_SEQ,
                 "next_since": rows[-1]["seq"] if rows else since,
                 "hint": "轮询时带 since=next_since；last_seq 是当前最新序号"}


def _history(api, ctx, query):
    limit = _int(query.get("limit", ["50"])[0], 50, 1, 500)
    detail = str(query.get("detail", ["0"])[0]).lower() in ("1", "true", "yes")
    want = (query.get("status", [""])[0] or "").strip()
    try:
        data = _jsonish(api.get_download_history()) or {}
    except Exception as e:
        return 500, {"ok": False, "msg": "读取历史失败: %s" % str(e)[:200]}
    items = data.get("items") if isinstance(data, dict) else data
    items = [r for r in (items or []) if isinstance(r, dict)]
    if want:
        items = [r for r in items if r.get("status") == want]

    def ts(row):
        try:
            return float(row.get("finished_at") or row.get("updated_at") or 0)
        except (TypeError, ValueError):
            return 0.0

    items = sorted(items, key=ts, reverse=True)[:limit]
    if not detail:
        items = [{"id": r.get("id"), "filename": r.get("filename"), "status": r.get("status"),
                  "dest_dir": r.get("dest_dir"), "routed_dir": r.get("routed_dir"),
                  "file_path": r.get("file_path"), "file_exists": r.get("file_exists"),
                  "total": r.get("total"), "error": r.get("error") or "",
                  "finished_at": r.get("finished_at")} for r in items]
    return 200, {"ok": True, "count": len(items), "items": items,
                 "error": (data or {}).get("error") if isinstance(data, dict) else None}


def _prompts(api, ctx, query):
    """模型示例图的提示词与生成参数（AI 复现用）：直接读 .civitai.info 的 images[].meta。"""
    path = (query.get("path", [""])[0] or "").strip()
    if not path:
        return 400, {"ok": False, "msg": "需要 path（模型文件绝对路径）"}
    if not os.path.isfile(path):
        return 404, {"ok": False, "msg": "文件不存在: %s" % path}
    limit = _int(query.get("limit", ["50"])[0], 50, 1, 200)
    try:
        images = api._info_images(path) or []
    except Exception as e:
        return 500, {"ok": False, "msg": "读取 info 失败: %s" % str(e)[:200]}
    rows = []
    for i, im in enumerate(images[:limit]):
        meta = im.get("meta") if isinstance(im.get("meta"), dict) else {}
        rows.append({
            "index": i, "type": im.get("type") or "image",
            "url": im.get("url") or "", "width": im.get("width"), "height": im.get("height"),
            "nsfw": im.get("nsfwLevel") or im.get("nsfw"),
            "prompt": meta.get("prompt") or "",
            "negative_prompt": meta.get("negativePrompt") or "",
            "params": {k: v for k, v in meta.items() if k not in ("prompt", "negativePrompt")},
        })
    out = {"ok": True, "path": path, "count": len(images), "shown": len(rows), "images": rows}
    if not images:
        out["hint"] = "该模型没有 info 或 images 为空；可先 POST /api/automation/library/op {op:'verify'} 补全元数据"
    return 200, out


def _wait_mm(getter, timeout):
    """等后台模型操作结束：running=false 即完成（结果在返回的 progress 里）"""
    t0 = time.time()
    while True:
        try:
            st = getter() or {}
        except Exception:
            st = {}
        if not st.get("running"):
            return st, round(time.time() - t0, 1), False
        if time.time() - t0 >= timeout:
            return st, round(time.time() - t0, 1), True
        time.sleep(0.3)


def _library_op(api, ctx, body):
    """模型管理动作的统一入口：verify / dedupe / localize / gen-json / updates / covers…"""
    import inspect
    b = body or {}
    op = str(b.get("op") or "").strip()
    if op not in _MM_OPS:
        return 400, {"ok": False, "msg": "op 必须是 %s 之一" % "|".join(_MM_OPS)}
    name, kind, desc = _MM_OPS[op]
    fn = getattr(api, name, None)
    if not callable(fn):
        return 501, {"ok": False, "msg": "当前版本不支持 %s" % op}
    paths = b.get("paths")
    if paths is not None and not isinstance(paths, list):
        return 400, {"ok": False, "msg": "paths 必须是数组（模型文件绝对路径）"}
    if op == "dedupe-delete":
        if not b.get("confirm"):
            return 400, {"ok": False, "msg": "dedupe-delete 会移入回收站，必须显式传 confirm:true"}
        if not paths:
            return 400, {"ok": False, "msg": "dedupe-delete 必须指定 paths"}

    kwargs = {}
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    if "paths" in params and paths is not None:
        kwargs["paths"] = paths
    if "overwrite" in params:
        kwargs["overwrite"] = bool(b.get("overwrite"))
    if "force" in params:
        kwargs["force"] = bool(b.get("force"))
    try:
        started = _jsonish(fn(**kwargs)) or {}
    except Exception as e:
        return 500, {"ok": False, "msg": "执行失败: %s" % str(e)[:200]}
    if not isinstance(started, dict):
        started = {"ok": True, "raw": started}
    accepted = bool(started.get("started")) or bool(started.get("ok"))
    if not accepted:
        return 400, {"ok": False, "op": op, "desc": desc, "start": started,
                     "msg": started.get("msg") or "动作未启动"}

    wait = str(b.get("wait", "1")).lower() not in ("0", "false", "no")
    timeout = _int(b.get("timeout", DEFAULT_WAIT_S), DEFAULT_WAIT_S, 1, MAX_WAIT_S)
    if not wait or kind == "sync":
        return 200, {"ok": True, "op": op, "desc": desc, "start": started,
                     "progress": started if kind == "sync" else None,
                     "hint": None if kind == "sync" else "用 GET /api/automation/library/op?op=%s 轮询" % op}
    getter = api.get_mm_update_state if kind == "upd" else api.get_mm_progress
    progress, waited, timed_out = _wait_mm(getter, timeout)
    return 200, {"ok": not timed_out and not progress.get("error"), "op": op, "desc": desc,
                 "start": started, "progress": progress, "waited_s": waited,
                 "timed_out": timed_out}


def _library_op_state(api, ctx, query):
    op = (query.get("op", ["verify"])[0] or "verify").strip()
    kind = (_MM_OPS.get(op) or ("", "mm", ""))[1]
    getter = api.get_mm_update_state if kind == "upd" else api.get_mm_progress
    try:
        st = getter() or {}
    except Exception as e:
        return 500, {"ok": False, "msg": str(e)[:200]}
    return 200, {"ok": True, "op": op, "running": bool(st.get("running")), "progress": st}


def _images_download(api, ctx, body):
    """批量下载示例图到 <名>.images/；默认异步启动 + 轮询，绝不把请求线程挂死。"""
    import threading
    b = body or {}
    path = str(b.get("path") or "").strip()
    if not path:
        return 400, {"ok": False, "msg": "需要 path（模型文件绝对路径）"}
    if not os.path.isfile(path):
        return 404, {"ok": False, "msg": "文件不存在: %s" % path}
    try:
        if not (api._info_images(path) or []):
            return 400, {"ok": False, "msg": "该模型没有示例图信息（info.images 为空）"}
    except Exception:
        pass
    wait = str(b.get("wait", "1")).lower() not in ("0", "false", "no")
    timeout = _int(b.get("timeout", DEFAULT_WAIT_S), DEFAULT_WAIT_S, 1, MAX_WAIT_S)

    def run():
        try:
            api.download_all_images(path)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()
    if not wait:
        return 202, {"ok": True, "started": True, "path": path,
                     "hint": "用 GET /api/automation/images/state?path=... 轮询 total/done"}
    t0 = time.time()
    state = {}
    while time.time() - t0 < timeout:
        try:
            state = _jsonish(api.get_img_dl_state()) or {}
        except Exception:
            state = {}
        total = int(state.get("total") or 0)
        if total and int(state.get("done") or 0) >= total:
            break
        time.sleep(0.5)
    timed_out = not (state.get("total") and state.get("done") >= state.get("total"))
    return 200, {"ok": True, "started": True, "path": path, "state": state,
                 "waited_s": round(time.time() - t0, 1), "timed_out": timed_out}


def _images_state(api, ctx, query):
    path = (query.get("path", [""])[0] or "").strip()
    out = {"ok": True}
    try:
        out["download"] = _jsonish(api.get_img_dl_state()) or {}
    except Exception as e:
        out["download"] = {"error": str(e)[:120]}
    if path:
        try:
            out["autofill"] = _jsonish(api.get_images_autofill_state(path)) or {}
        except Exception as e:
            out["autofill"] = {"error": str(e)[:120]}
    return 200, out


def _workflow(api, ctx, body):
    """ComfyUI 工作流解析 + 本地模型匹配。默认丢掉 preview_b64/边表，只留 AI 用得上的部分。"""
    b = body or {}
    path = str(b.get("path") or "").strip()
    if not path:
        return 400, {"ok": False, "msg": "需要 path（.json / .png / .webp 工作流文件）"}
    if not os.path.isfile(path):
        return 404, {"ok": False, "msg": "文件不存在: %s" % path}
    try:
        data = _jsonish(api.analyze_workflow(path)) or {}
    except Exception as e:
        return 500, {"ok": False, "msg": "解析失败: %s" % str(e)[:200]}
    if not isinstance(data, dict):
        return 500, {"ok": False, "msg": "解析结果不是对象"}
    if not data.get("ok"):
        return 400, data
    full = bool(b.get("full"))
    out = {k: v for k, v in data.items() if k not in ("preview_b64", "nodes", "edges")}
    nodes = data.get("nodes") or []
    out["nodes"] = nodes if full else [
        {"id": n.get("id"), "type": n.get("type"), "title": n.get("title")} for n in nodes]
    out["edges_count"] = len(data.get("edges") or [])
    if full:
        out["edges"] = data.get("edges")
    models = data.get("models") or []
    if models:
        try:
            out["local_matches"] = _jsonish(api.workflow_model_matches(models)) or []
        except Exception as e:
            out["local_matches_error"] = str(e)[:160]
    return 200, out


def _storage(api, ctx):
    """落点与磁盘：哪些目录存在/可写、剩余空间、ComfyUI 类型目录是否齐全。"""
    import shutil
    cfg = getattr(api, "cfg", None) or {}
    root = str(cfg.get("comfyui_models_root") or "").strip()
    seen, dirs = set(), []
    for key in ("download_dir", "download_target_dir", "models_dir", "comfyui_models_root"):
        p = str(cfg.get(key) or "").strip()
        if not p or os.path.normcase(p) in seen:
            continue
        seen.add(os.path.normcase(p))
        exists = os.path.isdir(p)
        row = {"key": key, "path": p, "exists": exists,
               "writable": bool(exists and os.access(p, os.W_OK))}
        if exists:
            try:
                row["free_gb"] = round(shutil.disk_usage(p).free / (1024 ** 3), 1)
            except OSError:
                pass
        dirs.append(row)
    type_dirs = []
    if root and os.path.isdir(root):
        import model_routing
        for folder in sorted(model_routing.CORE_FOLDERS):
            p = os.path.join(root, folder)
            exists = os.path.isdir(p)
            type_dirs.append({"folder": folder, "exists": exists,
                              "writable": bool(exists and os.access(p, os.W_OK))})
    return 200, {"ok": True,
                 "routing": {"enabled": bool(cfg.get("route_by_type")), "root": root},
                 "dirs": dirs, "type_dirs": type_dirs,
                 "hint": "route_by_type=true 且类型目录存在时才分流；不存在的目录绝不新建"}


# ---------------------------------------------------------------- 分发

def dispatch(path, method, query, body, api, ctx):
    """返回 (status, payload)；路径不认识返回 None（交给上层 404）。

    query: dict[str, list[str]]（parse_qs 结果）；body: dict 或 None。
    ctx 由 bridge 注入：version / state() / extension() / config_keys / data_dir / error_log / rpc_methods。
    """
    q = query or {}
    m = (method or "GET").upper()
    if api is None:
        return 500, {"ok": False, "msg": "RPC 未就绪"}

    if m == "GET":
        if path == "/api/automation":
            return _manifest(api, ctx)
        if path == "/api/automation/schema":
            return _schema(api, ctx)
        if path == "/api/automation/state":
            return _state(api, ctx)
        if path == "/api/automation/tasks":
            return _tasks(api, ctx, q)
        if path == "/api/automation/events":
            return _events(api, ctx, q)
        if path == "/api/automation/history":
            return _history(api, ctx, q)
        if path == "/api/automation/prompts":
            return _prompts(api, ctx, q)
        if path == "/api/automation/verify":
            return _verify(api, ctx, q)
        if path == "/api/automation/config":
            return _config_get(api, ctx)
        if path == "/api/automation/logs":
            return _logs(api, ctx, q)
        if path == "/api/automation/library":
            return _library(api, ctx, q)
        if path == "/api/automation/library/op":
            return _library_op_state(api, ctx, q)
        if path == "/api/automation/images/state":
            return _images_state(api, ctx, q)
        if path == "/api/automation/storage":
            return _storage(api, ctx)
        if path == "/api/automation/model":
            return _model_detail(api, ctx, q)
        if path == "/api/automation/extension":
            return _extension(api, ctx)
        if path == "/api/automation/connectivity":
            return _connectivity(api, ctx, q)
        return None

    if m == "POST":
        if path == "/api/automation/tasks":
            return _tasks_action(api, ctx, body)
        if path == "/api/automation/plan":
            return _plan(api, ctx, body)
        if path == "/api/automation/resolve":
            return _resolve(api, ctx, body)
        if path == "/api/automation/download":
            return _download(api, ctx, body)
        if path == "/api/automation/file-choice":
            return _file_choice(api, ctx, body)
        if path == "/api/automation/search":
            return _search(api, ctx, body)
        if path == "/api/automation/library/scan":
            return _library_scan(api, ctx, body)
        if path == "/api/automation/library/op":
            return _library_op(api, ctx, body)
        if path == "/api/automation/images/download":
            return _images_download(api, ctx, body)
        if path == "/api/automation/workflow/analyze":
            return _workflow(api, ctx, body)
        if path == "/api/automation/config":
            return ctx["save_config"](body)
        return None

    return 405, {"ok": False, "msg": "方法不支持"}
