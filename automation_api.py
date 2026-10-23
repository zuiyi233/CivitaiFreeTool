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
from urllib.parse import parse_qs, unquote

import ai_contract
import ai_tools
import openapi_projection

MAX_WAIT_S = 1800
DEFAULT_WAIT_S = 300
# 传输结束后的收尾预算：sidecar/封面/视频都在 done 之后写，verify 必须等它（见 _wait_postprocess）
POST_WAIT_S = 30.0
TASK_TERMINAL = ("done", "error", "canceled")
SIDECAR_SUFFIXES = (".civitai.info", ".json", "_评论.txt")
# 封面：图片封面由桌面端写 <stem>.preview.png；封面是视频时桌面端写不出 png，
# 由扩展按 coverSaveMode=video 落 <stem>.mp4。少一个都不能算缺失。
PREVIEW_SUFFIXES = (".preview.png", ".preview.jpg", ".preview.webp",
                    ".cover.png", ".cover.jpg", ".preview.mp4", ".preview.webm",
                    ".mp4", ".webm")
ARCHIVE_ARTIFACTS = ("评论.txt", "example.json")

# 资源库（ComfyUI output 媒体库）写操作白名单 + 单次批量上限（与前端分片一致）
GALLERY_OPS = ("star", "tag-add", "tag-remove", "tag-save", "tag-delete", "delete")
GALLERY_BATCH_MAX = 500

# 本层调用事件（AI 自检：我的请求到底到没到、耗时多少、返回了什么状态）
_EVENTS = deque(maxlen=200)
_EVENT_SEQ = 0


def record_event(method, path, status, ms, summary=""):
    global _EVENT_SEQ
    _EVENT_SEQ += 1
    _EVENTS.append({"seq": _EVENT_SEQ, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "method": method, "path": path, "status": status,
                    "ms": round(ms, 1), "summary": str(summary or "")[:200]})
    try:
        import automation_events
        automation_events.publish_api_call(method, path, status, ms, summary)
    except Exception:
        pass


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


def _truthy(value, default=False):
    """布尔参数归一：兼容 true/1/yes 与字符串写法（"false" 不能因为非空就算真）"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() not in ("", "0", "false", "no", "off")


def _tasks_raw(api):
    """当前任务列表（含 info 里的 routed_dir / route_reason）"""
    try:
        return list(api.get_tasks() or [])
    except Exception:
        return []


# ---------------------------------------------------------------- 通用工具
_MISSING = object()


def _fail(status, msg, hint="", code="", retryable=None, next_actions=None):
    """失败响应：msg 说清「哪里不对」，hint 说清「下一步做什么」——AI 靠这两句决定重试还是改参数。"""
    out = {"ok": False, "msg": str(msg)}
    if hint:
        out["hint"] = str(hint)
    if code:
        out["code"] = str(code)
    if retryable is not None:
        out["retryable"] = bool(retryable)
    if next_actions:
        out["next_actions"] = list(next_actions)
    return status, out


def _pick_path(obj, path):
    """按点号路径取值："info.routed_dir" → obj["info"]["routed_dir"]；取不到返回 _MISSING"""
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


def _set_path(dst, path, value):
    parts = str(path).split(".")
    cur = dst
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _project_item(item, names):
    out = {}
    for n in names:
        v = _pick_path(item, n)
        if v is not _MISSING:
            _set_path(out, n, v)
    return out


def _apply_fields(payload, fields, list_key=None):
    """字段投影：`fields=id,status,progress,info.routed_dir` 只回这些字段。

    存在的理由是省 token：`/library?limit=200` 与 `/tasks` 的完整行里有大量 AI 用不上的键。
    列表端点（list_key）保留全部元信息（ok/count/waited_s…），只裁剪条目；
    非列表端点保留 ok + 请求的字段。给 `*` 表示不裁剪。
    """
    names = [s.strip() for s in str(fields or "").split(",") if s.strip()]
    if not names or "*" in names or not isinstance(payload, dict):
        return payload
    if list_key and isinstance(payload.get(list_key), list):
        out = {k: v for k, v in payload.items() if k != list_key}
        out[list_key] = [_project_item(it, names) if isinstance(it, dict) else it
                         for it in payload[list_key]]
        return out
    projected = _project_item(payload, names)
    if "ok" in payload:
        projected.setdefault("ok", payload["ok"])
    return projected


def _fields_from(query, body=None):
    if isinstance(body, dict) and body.get("fields"):
        return str(body.get("fields"))
    return ((query or {}).get("fields", [""])[0] or "")


def _strip_media(value):
    """mode=text：剥掉内联 base64/图片 data URL，保留路径与元数据。"""
    try:
        import ai_content
        return ai_content.strip_inline_media(value)
    except Exception:
        count = 0

        def walk(node):
            nonlocal count
            if isinstance(node, dict):
                out = {}
                for k, v in node.items():
                    if k in ("b64", "preview_b64", "poster_b64"):
                        count += 1
                        continue
                    out[k] = walk(v)
                return out
            if isinstance(node, list):
                return [walk(v) for v in node]
            if isinstance(node, str) and node.startswith("data:image/") and ";base64," in node:
                count += 1
                return ""
            return node

        return walk(value), count


def _norm_query(q):
    """batch 里 query 允许 {k: 标量} 或 {k: [..]}，统一成 parse_qs 的形状（dict[str, list[str]]）"""
    out = {}
    for k, v in (q or {}).items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            out[str(k)] = [str(x) for x in v]
        else:
            out[str(k)] = [str(v)]
    return out


def _find_task(api, task_id):
    for t in _tasks_raw(api):
        if t.get("id") == task_id:
            return t
    return None


def _verify_dir(path, filename, expected_sha256="", do_hash=False, expect=None):
    """校验落点：文件存在/大小/哈希 + 附属文件清单（只读，不移动任何东西）。

    `ok` 只回答「这个模型文件本身可用」；`bundle_ok` 才回答「配置要求写的附属文件都齐了」。
    分开的原因（实测踩到）：没配 API Key 时评论文件本就不会写、metadata_format=civitai 时
    不会写 .json，把它们算进 `ok` 会让每一次 verify 都假失败，AI 只能靠猜。
    `expect` 是调用方按当前配置/任务算出的「应当存在」的后缀列表。
    """
    expected_missing = []
    out = {"ok": False, "bundle_ok": False, "dir": path or "", "filename": filename or "",
           "exists": False, "size": 0, "expected_sha256": expected_sha256 or "",
           "sha256": "", "sha256_ok": None, "preview": "",
           "sidecars": {"found": [], "missing": [], "expected_missing": expected_missing},
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
                else:
                    out["sha256_hint"] = "没有期望 SHA256 可比对，这里只给实算值"
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
        expected_missing.extend(stem + s for s in (expect or []) if stem + s not in names)
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

    out["ok"] = bool(out["exists"] and out["size"] > 0 and (out["sha256_ok"] is not False))
    out["bundle_ok"] = bool(out["ok"] and not expected_missing)
    return out


def _expect_sidecars(api):
    """按当前配置算「应当存在」的 sidecar 后缀。

    只列配置真的会让它写出来的：`archive_comments` 有 Key 才有评论文件；
    `metadata_format=civitai` 不写 .json。把这些算成缺失就是假失败。
    """
    cfg = getattr(api, "cfg", None) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    expect = []
    if cfg.get("gen_metadata", True):
        expect.append(".civitai.info")
        if str(cfg.get("metadata_format") or "sd").strip().lower() in ("sd", "both"):
            expect.append(".json")
    if cfg.get("archive_comments", True) and str(cfg.get("api_key") or "").strip():
        expect.append("_评论.txt")
    return expect


# ---------------------------------------------------------------- 各端点

def _manifest(api, ctx):
    return 200, {
        "ok": True, "app": "CivitaiFreeTool", "version": ctx.get("version", ""),
        "rpc": "POST /api/rpc {method, args:[...]}（长尾方法走这里，见 rpc_methods）",
        "endpoints": [
            {"method": "GET", "path": "/api/automation", "desc": "能力清单（本表）"},
            {"method": "POST", "path": "/api/automation/batch",
             "params": {"calls": "[{method,path,query?,body?}]", "stop_on_error": "bool"},
             "desc": "一次 HTTP 跑多条本层调用（最多 50 条），逐条返回 http_status；batch 本身恒 200"},
            {"method": "GET", "path": "/api/automation/schema",
             "desc": "自描述：配置键类型/默认值/可选值 + 词表 + 类型目录映射"},
            {"method": "GET", "path": "/api/automation/ai",
             "params": {"include_tools": "0|1"},
             "desc": "AI 能力协商：版本、toolsets、限额、guide、MCP 提示"},
            {"method": "GET", "path": "/api/automation/tools",
             "params": {"format": "mcp|openai|anthropic", "profile": "text|vision|admin|all"},
             "desc": "从 ai_contract 生成的工具 schema（同一份契约给 MCP/HTTP）"},
            {"method": "GET", "path": "/api/automation/openapi.json",
             "params": {"profile": "text|vision|admin|all"},
             "desc": "OpenAPI 3.1 文档：每个工具一个 POST /api/automation/tool/{name} 操作；"
                     "默认 text（19 个），admin 需显式选择"},
            {"method": "POST", "path": "/api/automation/tool/{name}",
             "params": {"body": "工具 arguments 对象", "content_format": "neutral|openai|anthropic|mcp"},
             "desc": "OpenAPI 导入器使用的每工具执行入口；与 POST /api/automation/tool 等价"},
            {"method": "POST", "path": "/api/automation/tool",
             "params": {"name": "cft_*", "arguments": "object", "content_format": "neutral|openai|anthropic|mcp"},
             "desc": "统一工具执行入口；MCP 侧车也调用它"},
            {"method": "GET", "path": "/api/automation/guide",
             "params": {"profile": "text|vision", "format": "json|markdown"},
             "desc": "给模型的紧凑 runbook（与 MCP resources 同源）"},
            {"method": "GET", "path": "/api/automation/vision/model",
             "params": {"path": "str", "limit_images": "1-8", "max_side": "256-1536",
                        "delivery": "inline|paths", "include_prompts": "0|1", "safe": "0|1"},
             "desc": "模型封面/示例图 + 提示词/参数（视觉内容块）"},
            {"method": "GET", "path": "/api/automation/vision/gallery",
             "params": {"ids": "csv int", "max_side": "256-1536", "delivery": "inline|paths"},
             "desc": "资源库条目的图片/视频首帧 + 生成元数据"},
            {"method": "GET", "path": "/api/automation/vision/workflow",
             "params": {"path": "str", "max_side": "256-1536", "delivery": "inline|paths"},
             "desc": "工作流/图片预览 + 提示词与模型引用"},
            {"method": "GET", "path": "/api/automation/vision/contact-sheet",
             "params": {"source": "gallery|model", "ids": "csv int?" , "path": "str?",
                        "limit": "1-24", "cols": "1-6", "cell": "96-512"},
             "desc": "多图拼一张带编号的 contact sheet + id 映射"},
            {"method": "POST", "path": "/api/automation/vision/analyze",
             "params": {"path": "str?", "data_b64": "str?", "filename": "str?", "include_image": "0|1"},
             "desc": "解析本地/上传图片的生成参数与模型引用（只读、内存内）"},
            {"method": "GET", "path": "/api/automation/state",
             "params": {"compact": "0|1", "task_limit": "0-50", "fields": "csv"},
             "desc": "配置 + 最近任务 + 待选文件；compact=1 给文本模型"},
            {"method": "POST", "path": "/api/automation/ui/dismiss",
             "params": {"targets": "[all|guides|dialogs|onboarding|api_key|reverse_tutorial|"
                                    "confirm|file_choice|move_after_download|settings_unsaved|"
                                    "restricted|info|result|detail|about|image_viewer]",
                        "permanent": "bool（默认 true，仅对引导生效）"},
             "desc": "AI 一键关闭所有挡路弹层；确认框关闭=取消，file_choice=取消本次下载；"
                     "permanent=true 只把新手引导设为不再自动弹"},
            {"method": "GET", "path": "/api/automation/tasks",
             "params": {"ids": "csv", "status": "csv", "limit": "int", "wait": "0|1", "timeout": "s",
                        "since": "ms", "fields": "csv"},
             "desc": "任务列表；wait=1 阻塞到终态；fields= 裁剪字段（含 info.routed_dir 这类点号路径）"},
            {"method": "POST", "path": "/api/automation/tasks",
             "params": {"action": "pause|resume|retry|start|remove|archive", "ids": "[str]", "all": "bool"},
             "desc": "批量任务控制，逐项返回结果"},
            {"method": "POST", "path": "/api/automation/tasks/repair",
             "params": {"task_id": "str", "stages": "[metadata|comments|cover|video|replace_old]?",
                        "wait": "0|1", "timeout": "s"},
             "desc": "重跑下载收尾（幂等），默认阻塞到完成并回报 postprocess_errors"},
            {"method": "GET", "path": "/api/automation/events",
             "params": {"since": "int", "limit": "int"},
             "desc": "本层调用事件流（自检：请求到没到、多久、什么状态）"},
            {"method": "GET", "path": "/api/automation/events/stream",
             "params": {"since": "int?", "types": "csv 事件名/组 或 all",
                        "snapshot": "0|1（默认 1）"},
             "desc": "SSE 领域事件流：任务/文件选择/扫描/弹层/配置；支持 Last-Event-ID 重放"},
            {"method": "GET", "path": "/api/automation/history",
             "params": {"limit": "int", "status": "str", "detail": "0|1", "fields": "csv"},
             "desc": "已完成/失败/取消的下载历史（倒序）"},
            {"method": "POST", "path": "/api/automation/history",
             "params": {"action": "remove|clear", "ids": "[str]?", "all": "bool?",
                        "preview": "0|1"},
             "desc": "清理下载历史记录：只删记录与缩略图/信息快照，绝不删模型文件；"
                     "同时摘掉队列里同 id 的终态任务（否则下次保存会把记录写回来）；"
                     "preview=1 只回报将删的 id；不存在的 id 进 missing，可重复调用"},
            {"method": "GET", "path": "/api/automation/prompts",
             "params": {"path": "str", "limit": "int", "fields": "csv"},
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
                        "wait": "0|1", "timeout": "s", "verify": "0|1", "force": "0|1"},
             "desc": "提交下载（默认 caps=[bundle] 以启用分流），支持批量，可阻塞到终态并顺带校验；"
                     "目标文件已存在时返回 skipped=exists（不重复传几百 MB），force=1 才重下"},
            {"method": "GET", "path": "/api/automation/verify",
             "params": {"task_id": "str?", "path": "str?", "hash": "0|1"},
             "desc": "校验模型文件/哈希/附属文件（视频封面也算封面）；ok=模型文件本身可用，"
                     "bundle_ok=配置要求写的附属文件齐全，sidecars.expected_missing 列出缺哪些"},
            {"method": "GET", "path": "/api/automation/config", "desc": "全量配置（密钥打码）+ 数据目录"},
            {"method": "POST", "path": "/api/automation/config", "params": {"config": "object"},
             "desc": "白名单配置写入"},
            {"method": "GET", "path": "/api/automation/storage",
             "desc": "落点目录存在/可写/剩余空间 + ComfyUI 类型目录清单"},
            {"method": "GET", "path": "/api/automation/fs",
             "params": {"path": "str?", "dirs_only": "0|1"},
             "desc": "只读目录浏览：不带 path 列盘符，带 path 列一层（不读文件内容）"},
            {"method": "POST", "path": "/api/automation/storage/migrate",
             "params": {"directory": "str", "replace_existing": "bool?", "confirm": "true"},
             "desc": "迁移数据目录（配置/任务/历史/缓存），需 confirm:true"},
            {"method": "GET", "path": "/api/automation/logs",
             "params": {"source": "error|events|both", "tail": "int"},
             "desc": "error.log 尾部 + 本层调用事件"},
            {"method": "GET", "path": "/api/automation/library",
             "params": {"q": "str", "type": "str", "limit": "int", "offset": "int", "fields": "csv"},
             "desc": "模型库行数据（已解析成对象）"},
            {"method": "POST", "path": "/api/automation/library/op",
             "params": {"op": "|".join(_MM_OPS), "paths": "[str]?", "wait": "0|1",
                        "timeout": "s", "overwrite": "bool?", "force": "bool?", "confirm": "bool?"},
             "desc": "模型管理动作（校验/查重/汉化/生成 json/检查更新/更新/封面…）可等待完成"},
            {"method": "GET", "path": "/api/automation/library/op", "params": {"op": "str"},
             "desc": "上述后台动作的进度"},
            {"method": "GET", "path": "/api/automation/gallery",
             "params": {"filter": "JSON 对象字符串（media-library-contract §5.2）",
                        "limit": "1-500（默认 50）", "offset": ">=0（默认 0）",
                        "with_facets": "0|1（默认 0）", "fields": "csv"},
             "desc": "资源库（ComfyUI output 媒体库）列表：筛选/分页/分面；"
                     "filter 里的 limit/offset/with_facets 会被查询参数覆盖"},
            {"method": "GET", "path": "/api/automation/gallery/item",
             "params": {"id": "int", "fields": "csv", "mode": "full|text"},
             "desc": "资源库条目详情；mode=text 剥掉 preview base64，给纯文本模型"},
            {"method": "GET", "path": "/api/automation/gallery/tags", "params": {"fields": "csv"},
             "desc": "资源库标签列表"},
            {"method": "POST", "path": "/api/automation/gallery/op",
             "params": {"op": "|".join(GALLERY_OPS), "ids": "[int]?", "on": "bool?",
                        "tag_ids": "[int]?", "id": "int?", "name": "str?", "color": "str?",
                        "parent_id": "int?", "confirm": "true（delete 必填）"},
             "desc": "资源库写操作：收藏/标签/删除；ids 与 tag_ids 单次 1-500；"
                     "delete 需 confirm:true，只把媒体本体移入回收站，不连带 sidecar"},
            {"method": "GET", "path": "/api/automation/gallery/scan",
             "desc": "资源库扫描进度（轻量，不含行数据）"},
            {"method": "POST", "path": "/api/automation/gallery/scan",
             "params": {"wait": "0|1（默认 0）", "timeout": "1-1800s（默认 300）"},
             "desc": "触发资源库扫描；wait=1 轮询到 running=false 或超时"},
            {"method": "POST", "path": "/api/automation/images/download",
             "params": {"path": "str", "wait": "0|1", "timeout": "s"},
             "desc": "批量下载示例图到 <名>.images/（异步启动，可等待）"},
            {"method": "GET", "path": "/api/automation/images/state", "params": {"path": "str?"},
             "desc": "示例图下载 / 补帧进度"},
            {"method": "POST", "path": "/api/automation/workflow/analyze",
             "params": {"path": "str", "full": "0|1", "include_image": "0|1", "max_side": "256-1536"},
             "desc": "解析 ComfyUI 工作流/生成参数 + 本地模型匹配；"
                     "include_image=1 时附缩略预览（默认不带 base64）"},
            {"method": "POST", "path": "/api/automation/move",
             "params": {"path": "str?", "task_id": "str?", "history_id": "str?", "dest_dir": "str"},
             "desc": "移动模型 / 下载任务落点 / 历史记录到已存在的目录"},
            {"method": "POST", "path": "/api/automation/rename",
             "params": {"path": "str?", "new_name": "str?", "paths": "[str]?", "preview": "bool?"},
             "desc": "单文件改名或批量改名（preview=true 只算不改）"},
            {"method": "GET", "path": "/api/automation/model",
             "params": {"path": "str", "fields": "csv", "mode": "full|text"},
             "desc": "单个模型详情（info + 画廊）+ 附属文件清单；"
                     "mode=text 剥掉 covers base64，给纯文本模型"},
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


def _state(api, ctx, query=None):
    """状态：compact=1 时只回关键配置与少量任务，fields 只裁剪 tasks。"""
    q = query or {}
    data = ctx["state"]()
    if not isinstance(data, dict):
        return 200, data
    out = dict(data)
    compact = _truthy(q.get("compact", ["0"])[0])
    if compact:
        limit = _int(q.get("task_limit", ["5"])[0], 5, 0, 50)
        out["tasks"] = list(out.get("tasks") or [])[-limit:] if limit else []
        cfg = out.get("config") or {}
        out["config"] = {k: cfg.get(k) for k in (
            "download_dir", "download_target_dir", "models_dir", "comfyui_models_root",
            "route_by_type", "ask_move_after_download", "site_domain", "proxy_enabled",
            "ssl_verify", "target_env", "organize_mode",
            "onboarding_done", "auto_guides") if k in cfg}
    fields = _fields_from(q)
    if fields:
        names = [s.strip() for s in fields.split(",") if s.strip()]
        if names and isinstance(out.get("tasks"), list):
            out["tasks"] = [_project_item(t, names) if isinstance(t, dict) else t
                            for t in out["tasks"]]
    return 200, out


def _ui_dismiss(api, ctx, body):
    """关闭引导类弹窗：人需要新手引导，AI 不需要，也不该为此研究怎么点 X。

    只处理引导/教程（onboarding、API Key 提示、反向解析教程），不碰模型详情等业务弹层。
    `permanent=true` 写入 onboarding_done + auto_guides=false，下次打开不再自动弹出。
    """
    b = body or {}
    targets = b.get("targets")
    if targets is None:
        targets = ["all"]
    if isinstance(targets, str):
        targets = [targets]
    if not isinstance(targets, list):
        return _fail(400, "targets 必须是数组",
                     '例如 {"targets":["all"],"permanent":true}')
    allowed = tuple(getattr(api, "_UI_DISMISS_TARGETS", None) or (
        "all", "guides", "dialogs", "onboarding", "api_key", "reverse_tutorial",
        "confirm", "file_choice", "move_after_download", "settings_unsaved",
        "restricted", "info", "result", "detail", "media", "about", "image_viewer"))
    picked, seen = [], set()
    for item in targets:
        name = str(item or "").strip()
        if name in allowed and name not in seen:
            seen.add(name)
            picked.append(name)
    if not picked:
        return _fail(400, "targets 必须是 %s 之一" % "|".join(allowed),
                     "不传 targets = 关闭全部弹层；确认框关闭=取消，不会执行危险操作；"
                     "AI 建议 permanent=true 只对引导生效")
    permanent = b.get("permanent", True)
    if not isinstance(permanent, bool):
        return _fail(400, "permanent 必须是布尔值", "permanent=true 表示以后不再自动弹引导")
    fn = getattr(api, "dismiss_ui", None)
    if not callable(fn):
        return _fail(501, "当前版本不支持关闭引导",
                     "升级桌面端到 >= %s" % ai_contract.MIN_APP_VERSION)
    try:
        result = fn(targets=picked, permanent=permanent)
    except Exception as e:
        return _fail(500, "关闭引导失败: %s" % str(e)[:120], "查看 GET /api/automation/logs")
    if not isinstance(result, dict) or result.get("ok") is False:
        return _fail(500, (result or {}).get("msg") or "关闭引导失败",
                     "检查配置是否可写（GET /api/automation/config）")
    return 200, result


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
    return 200, _apply_fields({"ok": True, "tasks": rows, "count": len(rows),
                               "waited_s": waited, "timed_out": timed_out},
                              _fields_from(query), "tasks")


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


_API_CACHE = {}
_API_CACHE_TTL = 30.0
_API_CACHE_MAX = 64


def _cached_call(civ, kind, ident, fetch):
    """短缓存 get_model / get_model_version（30s）。

    为什么需要：单次请求经代理到镜像站实测 3.4~18s（首次含 TLS 建连），而 plan→resolve→download
    常常在几秒内问同一个版本。缓存只存成功结果、30 秒过期、上限 64 条；失败一律不缓存。
    """
    key = (getattr(civ, "api_base", ""), kind, str(ident))
    now = time.time()
    hit = _API_CACHE.get(key)
    if hit and now - hit[0] < _API_CACHE_TTL:
        return hit[1]
    value = fetch()
    if len(_API_CACHE) >= _API_CACHE_MAX:
        for k, v in list(_API_CACHE.items()):
            if now - v[0] >= _API_CACHE_TTL:
                _API_CACHE.pop(k, None)
        if len(_API_CACHE) >= _API_CACHE_MAX:
            _API_CACHE.clear()
    _API_CACHE[key] = (now, value)
    return value


def _filter_files(files, file_id):
    """按 file_id 过滤；不存在时返回 (None, 错误说明)"""
    if file_id is None:
        return files, ""
    picked = [f for f in files if str(f.get("id")) == str(file_id)]
    return (picked, "") if picked else (None, "指定文件不属于这个版本")


def _resolve_target(civ, body, allow_first_version=True, need_versions=False):
    """url 或 model_id(+version_id) → (model_id, version_id, version, model, files, error)

    只读：不下载、不建目录。url 里的 modelVersionId 优先，其次显式 version_id，
    最后才退回该模型的第一个版本（allow_first_version=False 时直接报错）。

    `need_versions=True` 才额外拉一次模型详情（要列全部版本时）。
    `/v1/model-versions/<id>` 自带 `model{name,type}`，只做命名/分流的话它就是全部所需，
    以前无条件再拉一次 `/v1/models/<id>`，白白多花一次网络往返（实测 2.3s）。
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
            versions = (_cached_call(civ, "model", model_id,
                                     lambda: civ.get_model(model_id)) or {}).get("modelVersions") or []
            if not versions:
                return None, None, None, None, [], "模型没有可用版本"
            if not allow_first_version:
                return None, None, None, None, [], "必须指定 version_id"
            version_id = versions[0]["id"]
        version = _cached_call(civ, "version", version_id,
                               lambda: civ.get_model_version(version_id))
        if model_id is None:
            mid = version.get("modelId")
            if mid is not None and str(mid).strip().isdigit():
                model_id = str(mid).strip()
        embedded = version.get("model") if isinstance(version.get("model"), dict) else {}
        if (not need_versions and model_id is not None
                and embedded.get("name") and embedded.get("type")):
            model = {"id": model_id, "name": embedded["name"], "type": embedded["type"]}
        else:
            model = (_cached_call(civ, "model", model_id, lambda: civ.get_model(model_id))
                     if model_id is not None else {})
        return model_id, version_id, version, model, civ.model_files(version), ""
    except Exception as e:
        return None, None, None, None, [], "解析失败: %s" % str(e)[:200]


def _cfg_roots(cfg):
    """All directories that can legitimately hold model files (ordered, deduped)."""
    cfg = cfg if isinstance(cfg, dict) else {}
    roots = []
    for key in ("comfyui_models_root", "models_dir", "download_target_dir", "download_dir"):
        value = str(cfg.get(key) or "").strip()
        if value:
            roots.append(value)
    for value in (cfg.get("models_dirs") or []):
        value = str(value or "").strip()
        if value:
            roots.append(value)
    out, seen = [], set()
    for path in roots:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def _under(path, root):
    try:
        child = os.path.normcase(os.path.abspath(str(path)))
        parent = os.path.normcase(os.path.abspath(str(root)))
    except (OSError, ValueError):
        return False
    return bool(child and parent and (child == parent or child.startswith(parent + os.sep)))


def _default_dest_dir(api):
    cfg = getattr(api, "cfg", None) or {}
    target = str(cfg.get("download_target_dir") or "").strip()
    if target and os.path.isdir(target):
        return target
    return str(cfg.get("download_dir") or "").strip()


def _placement(dest, cfg, expected_folder=""):
    """Read-only placement verdict; never moves files or changes config.

    status:
      routed       -> under comfyui_models_root/<type>/...
      under_root   -> inside a configured root but not in a type subdirectory
      outside_root -> not inside any configured model root
      pending      -> destination is resolved only when the download starts
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    dest = str(dest or "").strip()
    root = str(cfg.get("comfyui_models_root") or "").strip()
    roots = _cfg_roots(cfg)
    out = {
        "dest_dir": dest, "routed_dir": "", "folder": "",
        "expected_folder": str(expected_folder or ""),
        "under_models_root": False, "under_configured_root": False,
        "status": "pending", "hint": "",
    }
    if not dest:
        out["hint"] = "落点将在下载开始时解析；先在 cft_diagnostics storage 里确认模型根与分流配置"
        return out
    under_root = bool(root and _under(dest, root))
    out["under_models_root"] = under_root
    out["under_configured_root"] = any(_under(dest, item) for item in roots)
    rel = ""
    if under_root:
        try:
            rel = os.path.relpath(os.path.abspath(dest), os.path.abspath(root))
        except ValueError:
            rel = ""
        if rel and rel != ".":
            out["folder"] = rel.split(os.sep)[0]
            out["routed_dir"] = dest
    if out["folder"]:
        out["status"] = "routed"
    elif under_root:
        out["status"] = "under_root"
        out["hint"] = ("文件落在模型根目录根部，未按类型分目录；开启 route_by_type 并设置 "
                       "comfyui_models_root（候选值见 cft_diagnostics storage），"
                       "或下载时传 dest_subdir")
    elif out["under_configured_root"]:
        out["status"] = "under_root"
        out["hint"] = ("文件在已配置的模型目录内，但不在 comfyui_models_root 下；"
                       "若要让 ComfyUI 直接扫到，设置 comfyui_models_root 并开启 route_by_type")
    else:
        out["status"] = "outside_root"
        out["hint"] = ("文件不在任何已配置的模型根内；检查 models_dir / comfyui_models_root "
                       "或下载时传 dest_subdir")
    if not root and dest:
        out["hint"] = ("未配置 comfyui_models_root，无法按类型分流；先设置根目录并开启 "
                       "route_by_type（候选值见 cft_diagnostics storage）")
    return out


def _model_root_candidates(cfg):
    """Suggest comfyui_models_root from configured dirs that already contain type folders.

    Only looks at the configured paths and their direct parent — never scans drives.
    """
    import model_routing
    cfg = cfg if isinstance(cfg, dict) else {}
    folders = set(model_routing.CORE_FOLDERS)
    out, seen = [], set()

    def inspect(path, reason):
        if not path or not os.path.isdir(path):
            return
        try:
            names = [name for name in os.listdir(path)
                     if os.path.isdir(os.path.join(path, name))]
        except OSError:
            return
        matched = sorted(name for name in names if name.lower() in folders)
        if len(matched) < 2:
            return
        key = os.path.normcase(os.path.realpath(path))
        if key in seen:
            return
        seen.add(key)
        out.append({"path": path, "matched": matched, "reason": reason})

    current = str(cfg.get("comfyui_models_root") or "").strip()
    inspect(current, "当前 comfyui_models_root 已包含类型目录")
    for key in ("models_dir", "download_dir", "download_target_dir"):
        path = str(cfg.get(key) or "").strip()
        if not path:
            continue
        inspect(path, "%s 本身包含多个 ComfyUI 类型目录" % key)
        try:
            inspect(os.path.dirname(os.path.normpath(path)),
                    "%s 的上级目录包含多个 ComfyUI 类型目录" % key)
        except (OSError, ValueError):
            continue
    for path in (cfg.get("models_dirs") or []):
        path = str(path or "").strip()
        if not path:
            continue
        inspect(path, "模型目录本身包含多个 ComfyUI 类型目录")
        try:
            inspect(os.path.dirname(os.path.normpath(path)),
                    "模型目录的上级包含多个 ComfyUI 类型目录")
        except (OSError, ValueError):
            continue
    best = out[0]["path"] if out else ""
    return {"current": current, "suggested": best, "candidates": out}


def _plan_payload(api, body, model_id, version_id, version, model, files):
    """纯计算：文件命名 / 分流决策 / 落点 / 是否已存在或在队列。不写盘、不入队、不发请求
    （probe=1 时才会为了 safetensors 头做两次 Range，这由调用方决定是否开启）。"""
    import model_naming
    import model_routing

    root = str((api.cfg or {}).get("comfyui_models_root") or "").strip()
    route_on = bool((api.cfg or {}).get("route_by_type"))
    probe = bool((body or {}).get("probe"))
    civ = getattr(api, "api", None)
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
        subdir = str((body or {}).get("dest_subdir") or "").strip()
        final_dir = dest or ""
        if not final_dir and subdir:
            # 与 webui.dl_submit_url_from_bridge 的回落一致：未分流时 dest_subdir 落到归档根
            try:
                final_dir = api._safe_archive_path(api._archive_root(), subdir)
            except Exception:
                final_dir = ""
        if not final_dir:
            final_dir = _default_dest_dir(api)
        placement = _placement(final_dir, getattr(api, "cfg", None), folder)
        item = {
            "name": base, "file_id": f.get("id"), "type": f.get("type"),
            "primary": bool(f.get("primary")), "size_kb": f.get("sizeKB"),
            "folder": folder or "", "bundle": bundle,
            "routed_dir": dest or "", "route_reason": decision.reason,
            "route_source": decision.source,
            "would_route": bool(dest),
            "placement": placement,
            "exists": bool(final_dir and os.path.exists(os.path.join(final_dir, base))),
            "queued": False,
        }
        if item["exists"]:
            item["existing_path"] = os.path.join(final_dir, base)
        if civ is None:
            item["download_url"] = ""
            item["download_url_error"] = "桌面端未提供 C 站客户端"
        else:
            try:
                item["download_url"] = civ.file_download_url(version, f)
            except Exception as e:
                item["download_url"] = ""
                item["download_url_error"] = str(e)[:120]
        try:
            item["queued"] = bool(api._dup_in_queue(version_id, (f.get("hashes") or {}).get("SHA256"),
                                                    base, dest or final_dir))
        except Exception:
            pass
        items.append(item)

    try:
        lib = api.library_check(version_id)
    except Exception:
        lib = {}
    return _apply_fields({
        "ok": True, "url": str((body or {}).get("url") or ""),
        "model": {"id": model_id, "name": model.get("name"), "type": model.get("type")},
        "version": {"id": version_id, "name": version.get("name"),
                    "base_model": version.get("baseModel")},
        "routing": {"enabled": route_on, "models_root": root},
        "in_library": bool((lib or {}).get("found")),
        "library_paths": list((lib or {}).get("paths") or []),
        "files": items,
    }, _fields_from(None, body), "files")


def _plan(api, ctx, body):
    """干跑：不写盘、不入队。probe=1 才做 safetensors 头嗅探（两次 Range 网络请求）。"""
    civ = getattr(api, "api", None)
    if civ is None:
        return _fail(500, "API 未就绪", "确认桌面端已启动（GET /api/health）")
    model_id, version_id, version, model, files, err = _resolve_target(civ, body)
    if err:
        return _fail(400, err, "URL 需形如 /models/<id>?modelVersionId=<id>，或直接传 model_id/version_id")
    files, ferr = _filter_files(files, (body or {}).get("file_id"))
    if ferr:
        return _fail(400, ferr, "先用 POST /api/automation/resolve 看该版本有哪些 file_id")
    return 200, _plan_payload(api, body, model_id, version_id, version, model, files)


def _wait_postprocess(api, tasks, timeout=POST_WAIT_S):
    """等已完成任务的收尾（元数据/评论/封面/视频）跑完。

    status=done 只代表传输结束：收尾在独立线程里继续写 sidecar。以前 download(wait=1,verify=1)
    立刻 verify，拿到 sidecars.missing 就把一次成功下载判成失败（实测 9.1MB 模型复现）。
    返回额外等待秒数；旧任务没有 postprocess 标记时最多多等 2s，不做无谓等待。
    """
    ids = {t.get("id") for t in tasks if t.get("status") == "done" and t.get("id")}
    if not ids:
        return 0.0
    t0 = time.time()
    missing_since = {}
    while True:
        now = time.time()
        pending = set()
        for t in _tasks_raw(api):
            if t.get("id") not in ids:
                continue
            pp = t.get("postprocess")
            if isinstance(pp, dict) and pp.get("done") is True:
                continue
            if not isinstance(pp, dict):
                # 没有标记的历史任务：2s 内没出现就认定不会再有，别白等满超时
                if now - missing_since.setdefault(t.get("id"), now) > 2.0:
                    continue
            pending.add(t.get("id"))
        if not pending or now - t0 >= timeout:
            return round(now - t0, 1)
        time.sleep(0.25)


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


def _existing_target(api, b):
    """目标文件已存在时返回 (plan, hits)，否则 None。

    为什么要在自动化层拦：`POST /download` 对已下载过的模型会老老实实再传一遍，
    实测 284MB 白下 + 覆盖同名文件。AI 看不到界面，最容易踩这个坑。
    `force=1` 显式要求重下时才放过；批量（urls/file_ids）不做预判，避免 N 次解析。
    """
    if str(b.get("force", "0")).lower() in ("1", "true", "yes"):
        return None
    if b.get("urls") or b.get("file_ids"):
        return None
    civ = getattr(api, "api", None)
    if civ is None:
        return None
    model_id, version_id, version, model, files, err = _resolve_target(civ, b)
    if err:
        return None
    files, ferr = _filter_files(files, b.get("file_id"))
    if ferr:
        return None
    plan = _plan_payload(api, b, model_id, version_id, version, model, files)
    hits = [f for f in (plan.get("files") or []) if f.get("exists")]
    return (plan, hits) if hits else None


def _download(api, ctx, body):
    b = body or {}
    hit = _existing_target(api, b)
    if hit:
        plan, files = hit
        out = {"ok": True, "skipped": "exists", "task": None, "files": files,
               "model": plan.get("model"), "version": plan.get("version"),
               "waited_s": 0, "timed_out": False,
               "hint": "目标文件已存在，未重复下载；确需重新下载请传 force:true"}
        if files:
            existing = files[0].get("existing_path") or ""
            out["placement"] = files[0].get("placement") or _placement(
                os.path.dirname(existing) if existing else "", getattr(api, "cfg", None))
        return 200, out
    targets, err = _download_targets(api, b)
    if err:
        return 400, {"ok": False, "msg": err,
                     "hint": '最小用法：{"url":"https://<site_domain>/models/<id>?modelVersionId=<vid>",'
                             '"wait":true}；批量用 urls 或 file_ids；已存在的模型要重下才加 force:true'}
    dest_subdir = str(b.get("dest_subdir") or "")
    caps = b.get("caps")
    if caps is None:
        caps = ["bundle"]          # 自动化默认开启分流：本体与附属文件才会进同一个类型目录
    if not isinstance(caps, list):
        return 400, {"ok": False, "msg": "caps 必须是数组",
                     "hint": 'caps 由能力协商决定，手工调用一般省略；要显式关闭分流传 []'}
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
        items.append({"url": t["url"], "ok": accepted, "submit": submit,
                      "placement": _placement(submit.get("dest_dir") or "",
                                              getattr(api, "cfg", None))})

    tasks, waited, timed_out = [], 0.0, False
    if wait and expect_task:
        tasks, waited, timed_out = _wait_new_tasks(api, before, timeout)
        if want_verify and not timed_out:
            # 传输完成 ≠ 收尾完成；不等它，verify 会读到一个还没写 sidecar 的目录
            extra = _wait_postprocess(api, tasks)
            if extra:
                waited = round(waited + extra, 1)
                fresh = {t.get("id"): t for t in _tasks_raw(api)}
                tasks = [fresh.get(t.get("id"), t) for t in tasks]   # 保留提交顺序
    expect = _expect_sidecars(api)
    for idx, task in enumerate(tasks):
        if idx < len(items):
            items[idx]["task"] = task
            dest = task.get("routed_dir") or task.get("dest_dir") or ""
            items[idx]["placement"] = _placement(dest, getattr(api, "cfg", None))
            if task.get("status") == "done" and want_verify:
                items[idx]["verify"] = _verify_dir(
                    task.get("routed_dir") or task.get("dest_dir") or "",
                    task.get("filename") or "", "", False, expect)
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
               "timed_out": timed_out, "task": r.get("task"), "items": items,
               "placement": r.get("placement")}
        placement_hint = (r.get("placement") or {}).get("hint")
        if placement_hint and not out.get("hint"):
            out["hint"] = placement_hint
        if r.get("verify") is not None:
            out["verify"] = r["verify"]
            out["ok"] = bool(out["ok"] and r["verify"].get("ok"))
            if not r["verify"].get("bundle_ok"):
                out["hint"] = ("模型文件可用，但配置要求写出的附属文件不全（收尾可能未完成）："
                               "可 POST /api/automation/tasks/repair {task_id} 重跑收尾")
        return 200, out

    return 200, {"ok": all(i.get("ok") for i in items) if items else False,
                 "items": items, "count": len(items),
                 "waited_s": waited, "timed_out": timed_out}


def _verify(api, ctx, query):
    task_id = (query.get("task_id", [""])[0] or "").strip()
    path = (query.get("path", [""])[0] or "").strip()
    do_hash = str(query.get("hash", ["0"])[0]).lower() in ("1", "true", "yes")
    expect = _expect_sidecars(api)
    if task_id:
        task = _find_task(api, task_id)
        if not task:
            return 404, {"ok": False, "msg": "任务不存在"}
        dest = task.get("routed_dir") or task.get("dest_dir") or ""
        info = (task.get("info") or {}) if isinstance(task.get("info"), dict) else {}
        expected = info.get("expected_sha256") or task.get("expected_sha256") or ""
        out = _verify_dir(dest, task.get("filename") or "", expected, do_hash, expect)
        # 下载那一刻的校验结论直接带出来：不然 hash:false 时 AI 只看到 sha256_ok=null，
        # 分不清「已经校验过」和「根本没校验」
        for k in ("expected_sha256", "actual_sha256", "verification_status"):
            if task.get(k):
                out[k] = task[k]
        if out.get("verification_status") == "verified" and out.get("sha256_ok") is None:
            out["sha256_ok"] = True
        return 200, out
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
            return 200, _verify_dir(path, weights[0][1] if weights else "", "", do_hash, expect)
        return 200, _verify_dir(os.path.dirname(path), os.path.basename(path), "", do_hash, expect)
    return 400, {"ok": False, "msg": "需要 task_id 或 path",
                 "hint": "传 task_id；不知道 id 先 GET /api/automation/tasks"}


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
    return 200, _apply_fields({"ok": True, "total": total, "offset": offset, "limit": limit,
                               "rows": page, "scan": state},
                              _fields_from(query), "rows")


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


# ---------------------------------------------------------------- 资源库（ComfyUI output 媒体库）
# 底层能力在 webui.py 的 lib_* 方法；筛选字段、行形状与分面见
# docs/media-library-contract.md §5.2。本层只做参数校验、调用与结果整形，
# 不复制筛选/扫描/回收站逻辑（尤其 delete 的 sidecar 保护由 lib_delete 保证）。

def _gallery_batch_ids(value, name="ids"):
    """ids / tag_ids 闸门：JSON 数组、正整数、去重、单次 1..500。"""
    if not isinstance(value, list):
        return None, "%s 必须是正整数 JSON 数组" % name
    ids = []
    for x in value:
        if isinstance(x, bool):
            return None, "%s 里必须都是正整数" % name
        if isinstance(x, int):
            n = x
        elif isinstance(x, float) and x.is_integer():
            n = int(x)
        elif isinstance(x, str):
            try:
                n = int(x.strip())
            except ValueError:
                return None, "%s 里必须都是正整数" % name
        else:
            return None, "%s 里必须都是正整数" % name
        if n <= 0:
            return None, "%s 里必须都是正整数" % name
        if n not in ids:
            ids.append(n)
    if not ids:
        return None, "%s 不能为空" % name
    if len(ids) > GALLERY_BATCH_MAX:
        return None, "%s 单次最多 %d 项，请分片提交" % (name, GALLERY_BATCH_MAX)
    return ids, ""


def _gallery_list(api, ctx, query):
    """资源库列表：filter 是 JSON 对象字符串；limit/offset/with_facets 查询参数优先。

    `lib_list` 返回 JSON 字符串，且会在服务端把 root_ids 覆盖成配置的媒体根，
    所以 filter 里写 root_ids 也越不了权。facets 只在 with_facets=1 时返回，
    底层给 None 时归一成 {}（前端跳过重算时就是 None）。
    """
    raw = (query.get("filter", ["{}"])[0] or "{}").strip() or "{}"
    try:
        flt = json.loads(raw)
    except Exception as e:
        return _fail(400, "filter 不是合法 JSON: %s" % str(e)[:120],
                     'filter 是 JSON 对象字符串（字段见 docs/media-library-contract.md §5.2），'
                     '例如 {"kind":"image","starred":true,"q":"1girl"}')
    if not isinstance(flt, dict):
        return _fail(400, "filter 必须是 JSON 对象",
                     '例如 {"kind":"image","limit":20}；数组/标量不是合法筛选条件')
    try:
        limit = int((query.get("limit", ["50"])[0] or "50").strip())
    except (TypeError, ValueError):
        return _fail(400, "limit 必须是整数", "limit 合法范围 1..500（默认 50）")
    if not 1 <= limit <= 500:
        return _fail(400, "limit 必须在 1..500 之间（收到 %d）" % limit,
                     "limit 合法范围 1..500（默认 50）；翻页用 offset")
    try:
        offset = int((query.get("offset", ["0"])[0] or "0").strip())
    except (TypeError, ValueError):
        return _fail(400, "offset 必须是整数", "offset 必须 >= 0（默认 0）")
    if offset < 0:
        return _fail(400, "offset 必须 >= 0（收到 %d）" % offset, "offset 从 0 开始")
    with_facets = _truthy(query.get("with_facets", ["0"])[0])
    fn = getattr(api, "lib_list", None)
    if not callable(fn):
        return _fail(501, "当前版本不支持资源库", "升级桌面端后重试（需要 webui.py 的 lib_list）")
    flt.update({"limit": limit, "offset": offset, "with_facets": 1 if with_facets else 0})
    try:
        data = _jsonish(fn(json.dumps(flt, ensure_ascii=False)))
    except Exception as e:
        return _fail(500, "读取资源库失败: %s" % str(e)[:200],
                     "看 GET /api/automation/logs?source=error")
    if not isinstance(data, dict):
        return _fail(500, "资源库返回格式异常", "升级桌面端后重试")
    if not data.get("ok", True):
        return _fail(400, data.get("msg") or "筛选参数无效",
                     "筛选字段见 docs/media-library-contract.md §5.2")
    items = data.get("items") if isinstance(data.get("items"), list) else []
    got_offset, got_limit = data.get("offset"), data.get("limit")
    out = {"ok": True, "count": len(items), "total": int(data.get("total") or 0),
           "offset": int(got_offset if got_offset is not None else offset),
           "limit": int(got_limit if got_limit is not None else limit),
           "items": items}
    if with_facets:
        facets = data.get("facets")
        out["facets"] = facets if isinstance(facets, dict) else {}
    return 200, _apply_fields(out, _fields_from(query), "items")


def _gallery_item(api, ctx, query):
    raw = (query.get("id", [""])[0] or "").strip()
    try:
        item_id = int(raw)
    except (TypeError, ValueError):
        return _fail(400, "id 必须是正整数", "id 来自 GET /api/automation/gallery 的 items[].id")
    if item_id <= 0:
        return _fail(400, "id 必须是正整数（收到 %d）" % item_id,
                     "id 来自 GET /api/automation/gallery 的 items[].id")
    fn = getattr(api, "lib_detail", None)
    if not callable(fn):
        return _fail(501, "当前版本不支持资源库详情", "升级桌面端后重试（需要 webui.py 的 lib_detail）")
    try:
        item = _jsonish(fn(item_id))
    except Exception as e:
        return _fail(500, "读取条目失败: %s" % str(e)[:200],
                     "看 GET /api/automation/logs?source=error")
    if not isinstance(item, dict) or not item.get("ok"):
        msg = item.get("msg") if isinstance(item, dict) else "条目不存在"
        return _fail(404, msg or "条目不存在",
                     "先 GET /api/automation/gallery 取 id（items[].id），再查详情")
    mode = str((query.get("mode", [""])[0] or "")).strip().lower()
    omitted = 0
    if mode == "text":
        item, omitted = _strip_media(item)
    out = {"ok": True, "id": item_id, "mode": mode or "full", "item": item}
    if omitted:
        out["media_omitted"] = omitted
    return 200, _apply_fields(out, _fields_from(query))


def _gallery_tags(api, ctx, query):
    fn = getattr(api, "lib_tags", None)
    if not callable(fn):
        return _fail(501, "当前版本不支持资源库标签", "升级桌面端后重试（需要 webui.py 的 lib_tags）")
    try:
        data = _jsonish(fn())
    except Exception as e:
        return _fail(500, "读取标签失败: %s" % str(e)[:200],
                     "看 GET /api/automation/logs?source=error")
    if isinstance(data, dict) and data.get("ok") is False:
        return _fail(400, data.get("msg") or "读取标签失败", "升级桌面端后重试")
    tags = data if isinstance(data, list) else []
    return 200, _apply_fields({"ok": True, "count": len(tags), "tags": tags},
                              _fields_from(query), "tags")


def _gallery_op(api, ctx, body):
    """资源库写操作统一入口：star / tag-add / tag-remove / tag-save / tag-delete / delete。

    returns 200 with per-item results for delete even when some fail: 单条失败绝不能
    中断整批，否则 AI 拿不到「哪些成功、哪些要重试」。delete 只把媒体本体移入回收站，
    不连带同名 .json（由 lib_delete 保证，这里不复制该逻辑）。
    """
    b = body or {}
    op = str(b.get("op") or "").strip().lower()
    if op not in GALLERY_OPS:
        return _fail(400, "op 必须是 %s 之一" % "|".join(GALLERY_OPS),
                     '例如 {"op":"star","ids":[1,2]} 或 {"op":"delete","ids":[1],"confirm":true}')

    if op == "tag-save":
        name = str(b.get("name") or "").strip()
        if not name:
            return _fail(400, "name 不能为空",
                         '{"op":"tag-save","id":0,"name":"精选","color":"#888"}（id=0 新建）')
        try:
            tag_id = int(b.get("id", 0) or 0)
        except (TypeError, ValueError):
            return _fail(400, "id 必须是整数（0=新建）", "新建标签传 id:0 或不传")
        if tag_id < 0:
            return _fail(400, "id 必须 >= 0（0=新建）", "新建标签传 id:0 或不传")
        try:
            parent_id = int(b.get("parent_id") if b.get("parent_id") is not None else -1)
        except (TypeError, ValueError):
            return _fail(400, "parent_id 必须是整数（-1=无父）", "无父标签传 parent_id:-1 或不传")
        fn = getattr(api, "lib_tag_save", None)
        if not callable(fn):
            return _fail(501, "当前版本不支持标签保存", "升级桌面端后重试（需要 lib_tag_save）")
        try:
            r = _jsonish(fn(tag_id, name, str(b.get("color") or ""), parent_id))
        except Exception as e:
            return _fail(500, "保存标签失败: %s" % str(e)[:200],
                         "看 GET /api/automation/logs?source=error")
        r = r if isinstance(r, dict) else {"ok": False, "msg": "返回格式异常"}
        return (200 if r.get("ok") else 400), r

    if op == "tag-delete":
        try:
            tag_id = int(b.get("id"))
        except (TypeError, ValueError):
            return _fail(400, "tag-delete 需要正整数 id", '{"op":"tag-delete","id":3}')
        if tag_id <= 0:
            return _fail(400, "id 必须是正整数", '{"op":"tag-delete","id":3}')
        fn = getattr(api, "lib_tag_delete", None)
        if not callable(fn):
            return _fail(501, "当前版本不支持标签删除", "升级桌面端后重试（需要 lib_tag_delete）")
        try:
            r = _jsonish(fn(tag_id))
        except Exception as e:
            return _fail(500, "删除标签失败: %s" % str(e)[:200],
                         "看 GET /api/automation/logs?source=error")
        r = r if isinstance(r, dict) else {"ok": False, "msg": "返回格式异常"}
        return (200 if r.get("ok") else 400), r

    ids, err = _gallery_batch_ids(b.get("ids"), "ids")
    if err:
        return _fail(400, err, "ids 是资源库条目 id 的正整数数组（单次 1..500）")

    if op == "star":
        on = _truthy(b.get("on"), True)
        fn = getattr(api, "lib_set_star_batch", None)
        if not callable(fn):
            return _fail(501, "当前版本不支持收藏", "升级桌面端后重试（需要 lib_set_star_batch）")
        try:
            r = _jsonish(fn(json.dumps(ids), on)) or {}
        except Exception as e:
            return _fail(500, "收藏失败: %s" % str(e)[:200], "看 GET /api/automation/logs?source=error")
        if not isinstance(r, dict):
            r = {"ok": False, "msg": "返回格式异常"}
        out = {"ok": bool(r.get("ok")), "op": op,
               "count": int(r.get("count") or 0), "requested": len(ids)}
        if not out["ok"] and r.get("msg"):
            out["msg"] = str(r.get("msg"))
        return (200 if out["ok"] else 400), out

    if op in ("tag-add", "tag-remove"):
        tag_ids, err = _gallery_batch_ids(b.get("tag_ids"), "tag_ids")
        if err:
            return _fail(400, err, "tag_ids 是标签 id 的正整数数组（单次 1..500）")
        fn = getattr(api, "lib_tag_add_batch" if op == "tag-add" else "lib_tag_remove_batch", None)
        if not callable(fn):
            return _fail(501, "当前版本不支持标签批量操作", "升级桌面端后重试")
        try:
            r = _jsonish(fn(json.dumps(ids), json.dumps(tag_ids)))
        except Exception as e:
            return _fail(500, "标签操作失败: %s" % str(e)[:200],
                         "看 GET /api/automation/logs?source=error")
        out = dict(r) if isinstance(r, dict) else {"ok": False, "msg": "返回格式异常"}
        out["op"] = op
        return (200 if out.get("ok") else 400), out

    # delete：回收站语义，必须 confirm:true；逐条调用，单条失败不中断整批
    if not _truthy(b.get("confirm")):
        return _fail(400, "delete 会把媒体文件移入回收站，必须显式传 confirm:true",
                     "先 GET /api/automation/gallery 确认 ids，再带 confirm:true 执行；"
                     "只回收媒体本体，不连带同名 .json 等 sidecar",
                     code="destructive_requires_confirm")
    fn = getattr(api, "lib_delete", None)
    if not callable(fn):
        return _fail(501, "当前版本不支持删除", "升级桌面端后重试（需要 webui.py 的 lib_delete）")
    results, deleted = [], 0
    for item_id in ids:
        try:
            r = _jsonish(fn(item_id)) or {}
        except Exception as e:
            r = {"ok": False, "msg": "删除失败: %s" % str(e)[:200]}
        if not isinstance(r, dict):
            r = {"ok": bool(r), "msg": ""}
        ok = bool(r.get("ok"))
        deleted += 1 if ok else 0
        results.append({"id": item_id, "ok": ok,
                        "msg": str(r.get("msg") or ("已移入回收站" if ok else "删除失败"))})
    return 200, {"ok": deleted == len(results), "op": op, "deleted": deleted,
                 "failed": len(results) - deleted, "results": results}


def _gallery_scan_state(api, ctx):
    fn = getattr(api, "lib_scan_state", None)
    if not callable(fn):
        return _fail(501, "当前版本不支持资源库扫描", "升级桌面端后重试（需要 lib_scan_state）")
    try:
        state = _jsonish(fn())
    except Exception as e:
        return _fail(500, "读取扫描状态失败: %s" % str(e)[:200],
                     "看 GET /api/automation/logs?source=error")
    return 200, {"ok": True, "state": state if isinstance(state, dict) else {}}


def _gallery_scan(api, ctx, body):
    """触发资源库扫描；wait=1 时轮询到 running=false 或超时。

    已在进行中时 lib_scan 如实返回 started:false（msg 说明原因），但 wait=1 仍继续
    等本轮结束——AI 想要的是「扫完没有」，不是「这次是不是我启动的」。
    """
    b = body or {}
    wait = _truthy(b.get("wait"), False)
    timeout = _int(b.get("timeout", DEFAULT_WAIT_S), DEFAULT_WAIT_S, 1, MAX_WAIT_S)
    start_fn = getattr(api, "lib_scan", None)
    state_fn = getattr(api, "lib_scan_state", None)
    if not callable(start_fn) or not callable(state_fn):
        return _fail(501, "当前版本不支持资源库扫描", "升级桌面端后重试（需要 lib_scan/lib_scan_state）")
    try:
        started = _jsonish(start_fn()) or {}
    except Exception as e:
        return _fail(500, "启动扫描失败: %s" % str(e)[:200],
                     "看 GET /api/automation/logs?source=error")
    if not isinstance(started, dict):
        started = {"started": bool(started), "msg": ""}

    t0 = time.time()
    state, timed_out = {}, False
    if wait:
        while True:
            try:
                st = _jsonish(state_fn())
            except Exception:
                st = {}
            state = st if isinstance(st, dict) else {}
            if not state.get("running"):
                break
            if time.time() - t0 >= timeout:
                timed_out = True
                break
            time.sleep(0.25)
    else:
        try:
            st = _jsonish(state_fn())
        except Exception:
            st = {}
        state = st if isinstance(st, dict) else {}
    return 200, {"ok": True, "started": bool(started.get("started")),
                 "msg": str(started.get("msg") or ""), "state": state,
                 "waited_s": round(time.time() - t0, 1), "timed_out": timed_out}


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
    mode = str((query.get("mode", [""])[0] or "")).strip().lower()
    omitted = 0
    if mode == "text":
        detail, omitted = _strip_media(detail)
    v = _verify_dir(os.path.dirname(path), os.path.basename(path))
    out = {"ok": True, "path": path, "mode": mode or "full", "detail": detail,
           "files": {"exists": v["exists"], "size": v["size"],
                     "sidecars": v["sidecars"], "images": v["images"],
                     "artifacts": v["artifacts"]}}
    if omitted:
        out["media_omitted"] = omitted
    return 200, _apply_fields(out, _fields_from(query))


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
    "onboarding_done": ("bool", "", "首次使用引导是否已完成"),
    "auto_guides": ("bool", "", "是否自动弹出新手引导/教程；AI 可关闭，手动入口仍可用"),
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
    "organize": ("mm_organize", "mm", "按 target_env/organize_mode 把模型整理进分类目录"),
    "rename-c": ("mm_rename", "mm", "批量重命名为 C 站模型名"),
    "rename-preview": ("mm_rename_preview", "sync", "只算不改：预览批量重命名的新旧名"),
    "translate-descs": ("mm_translate_descs", "mm", "把模型简介翻译成中文（需百度翻译密钥）"),
    "cleanup": ("mm_cleanup", "mm", "删除元数据/封面/示例图（不可恢复，需 confirm:true）"),
}
_CONFIRM_OPS = {"dedupe-delete", "cleanup"}

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


def _ai(api, ctx, query):
    include_tools = _truthy((query or {}).get("include_tools", ["0"])[0])
    return 200, ai_tools.capabilities_payload(api, ctx, include_tools)


def _tools(api, ctx, query):
    """工具 schema 投影：同一份 ai_contract 生成 OpenAI/Anthropic/MCP 形状。"""
    fmt = str((query or {}).get("format", ["mcp"])[0] or "mcp").strip().lower()
    profile = str((query or {}).get("profile", ["text"])[0] or "text").strip().lower()
    if profile not in ai_contract.PROFILES:
        return _fail(400, "profile 必须是 %s 之一" % "|".join(ai_contract.PROFILES),
                     "text=纯文本；vision=core+vision；admin=core+admin；all=全部")
    if fmt not in ("mcp", "openai", "anthropic"):
        return _fail(400, "format 必须是 mcp|openai|anthropic",
                     "大多数函数调用客户端用 openai；Claude 用 anthropic；MCP 侧车用 mcp")
    rows = [ai_contract.public_tool(t, format=fmt) for t in ai_contract.tools_for_profile(profile)]
    return 200, {"ok": True, "format": fmt, "profile": profile,
                 "tools": rows, "count": len(rows),
                 "guide": "/api/automation/guide?profile=%s" % ("vision" if profile == "vision" else "text"),
                 "limits": dict(ai_contract.LIMITS)}


def _openapi(api, ctx, query):
    """OpenAPI 3.1 文档：从 ai_contract 运行时生成，不落盘、不手写。"""
    profile = str((query or {}).get("profile", ["text"])[0] or "text").strip().lower()
    if profile not in ai_contract.PROFILES:
        return _fail(400, "profile 必须是 %s 之一" % "|".join(ai_contract.PROFILES),
                     "text=纯文本；vision=core+vision；admin=core+admin；all=全部")
    try:
        doc = openapi_projection.build_document(
            profile,
            host_header=str((ctx or {}).get("host") or ""),
            app_version=str((ctx or {}).get("version") or ""),
        )
    except Exception as e:
        return _fail(500, "生成 OpenAPI 失败: %s" % str(e)[:160],
                     "检查 GET /api/automation/tools?profile=%s 是否正常" % profile)
    return 200, doc


def _tool_call(api, ctx, body):
    """统一工具执行入口：MCP 侧车与 HTTP 客户端共用，不是过渡层。"""
    b = body or {}
    name = str(b.get("name") or "").strip()
    args = b.get("arguments")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _fail(400, "arguments 必须是 JSON 对象",
                     '形如 {"name":"cft_get_state","arguments":{"compact":true}}')
    status, payload = ai_tools.execute_tool(name, args, api, ctx, dispatch)
    fmt = str(b.get("content_format") or b.get("format") or "neutral").strip().lower()
    if fmt not in ("neutral", "openai", "anthropic", "mcp"):
        return _fail(400, "content_format 必须是 neutral|openai|anthropic|mcp",
                     "neutral 给通用客户端；openai/anthropic 直接给消息 content 块")
    return status, ai_tools.format_content(payload, fmt)


def _guide(api, ctx, query):
    profile = str((query or {}).get("profile", ["text"])[0] or "text").strip().lower()
    if profile not in ("text", "vision"):
        return _fail(400, "profile 必须是 text|vision", "text 给纯文本模型；vision 给多模态模型")
    guide = ai_contract.guide_for(profile)
    if str((query or {}).get("format", ["json"])[0] or "json").strip().lower() == "markdown":
        return 200, {"ok": True, "profile": profile, "format": "markdown", "markdown": guide}
    return 200, {"ok": True, "profile": profile, "format": "json", "guide": guide,
                 "tools": [t["name"] for t in ai_contract.tools_for_profile(profile)]}


def _vision_query_args(query, tool):
    """把 GET query 字符串转成工具参数；只保留契约里声明的键。"""
    q = query or {}
    spec = (ai_contract.TOOLS.get(tool) or {}).get("inputSchema") or {}
    props = spec.get("properties") or {}
    args = {}
    for key, schema in props.items():
        if key not in q:
            continue
        raw = (q.get(key) or [""])[0]
        typ = schema.get("type")
        try:
            if typ == "integer":
                args[key] = int(raw)
            elif typ == "number":
                args[key] = float(raw)
            elif typ == "boolean":
                args[key] = _truthy(raw)
            elif typ == "array":
                if raw.strip().startswith("["):
                    args[key] = json.loads(raw)
                else:
                    item_type = (schema.get("items") or {}).get("type")
                    parts = [s.strip() for s in raw.split(",") if s.strip()]
                    args[key] = [int(s) for s in parts] if item_type == "integer" else parts
            else:
                args[key] = raw
        except (TypeError, ValueError):
            return None, "参数 %s 格式不对" % key
    return args, ""


def _vision_get(api, ctx, query, tool):
    args, err = _vision_query_args(query, tool)
    if err:
        return _fail(400, err, "参数类型见 GET /api/automation/tools")
    status, payload = ai_tools.execute_tool(tool, args, api, ctx, dispatch)
    fmt = str((query or {}).get("content_format", ["neutral"])[0] or "neutral").lower()
    if fmt not in ("neutral", "openai", "anthropic", "mcp"):
        return _fail(400, "content_format 必须是 neutral|openai|anthropic|mcp", "默认 neutral")
    return status, ai_tools.format_content(payload, fmt)


def _vision_analyze(api, ctx, body):
    b = body or {}
    args = {k: b.get(k) for k in ("path", "data_b64", "filename", "max_side", "delivery", "include_image")
            if b.get(k) is not None}
    status, payload = ai_tools.execute_tool("cft_inspect_image", args, api, ctx, dispatch)
    fmt = str(b.get("content_format") or "neutral").lower()
    if fmt not in ("neutral", "openai", "anthropic", "mcp"):
        return _fail(400, "content_format 必须是 neutral|openai|anthropic|mcp", "默认 neutral")
    return status, ai_tools.format_content(payload, fmt)


def _resolve(api, ctx, body):
    """只读解析：url 或 model_id → 模型、全部版本、选定版本的文件、落点、库内状态。

    比 /plan 多一层「版本清单」，AI 可以先挑版本再决定下哪个，不用先下载再后悔。
    与 /plan 共用同一次解析（版本 + 模型各一次请求，30 秒内还会命中缓存），
    不再像早期实现那样「调一遍 plan 再拉一次模型」。
    """
    civ = getattr(api, "api", None)
    if civ is None:
        return _fail(500, "API 未就绪", "确认桌面端已启动（GET /api/health）")
    model_id, version_id, version, model, files, err = _resolve_target(civ, body, need_versions=True)
    if err:
        return _fail(400, err, "URL 需形如 /models/<id>?modelVersionId=<id>，或直接传 model_id/version_id")
    files, ferr = _filter_files(files, (body or {}).get("file_id"))
    if ferr:
        return _fail(400, ferr, "先用 POST /api/automation/resolve 看该版本有哪些 file_id")
    plan = _plan_payload(api, body, model_id, version_id, version, model, files)
    versions = []
    try:
        for v in (model.get("modelVersions") or []):
            vfiles = v.get("files") or []
            versions.append({
                "id": v.get("id"), "name": v.get("name"),
                "base_model": v.get("baseModel"),
                "published_at": v.get("publishedAt"),
                "file_count": len(vfiles),
                "primary_size_kb": next((f.get("sizeKB") for f in vfiles if f.get("primary")), None),
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
    return 200, _apply_fields({"ok": True, "count": len(items), "items": items,
                               "error": (data or {}).get("error") if isinstance(data, dict) else None},
                              _fields_from(query), "items")


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
    return 200, _apply_fields(out, _fields_from(query), "images")


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
    paths = b.get("paths")
    if paths is not None and not isinstance(paths, list):
        return _fail(400, "paths 必须是数组（模型文件绝对路径）", "传 [\"D:/models/loras/a.safetensors\"]")
    if op in _CONFIRM_OPS:
        if not b.get("confirm"):
            return _fail(400, "%s 会删除文件/移入回收站，必须显式传 confirm:true" % op,
                         "先跑只读的 dedupe / rename-preview 确认清单，再带 confirm:true 执行",
                         code="destructive_requires_confirm")
        if not paths:
            return _fail(400, "%s 必须指定 paths" % op, "paths 是模型文件的绝对路径数组")
    fn = getattr(api, name, None)
    if not callable(fn):
        return _fail(501, "当前版本不支持 %s" % op, "用 GET /api/automation/schema 看本机实际可用的 library_ops")

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
    out["preview_available"] = bool(data.get("preview_b64"))
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
    if _truthy(b.get("include_image")):
        max_side = _int(b.get("max_side", 1024), 1024, 256, 1536)
        try:
            import vision_api
            v = vision_api.workflow_vision(api, path, max_side=max_side, delivery="inline")
            items = v.get("items") if isinstance(v, dict) else []
            if items and isinstance(items[0], dict):
                out["preview"] = items[0].get("image") or {}
                out["preview_meta"] = {k: items[0].get(k) for k in ("source", "text") if items[0].get(k)}
            if isinstance(v, dict) and v.get("note"):
                out["preview_note"] = v["note"]
        except Exception as e:
            out["preview_error"] = str(e)[:160]
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
    candidates = _model_root_candidates(cfg)
    return 200, {"ok": True,
                 "routing": {"enabled": bool(cfg.get("route_by_type")), "root": root},
                 "dirs": dirs, "type_dirs": type_dirs,
                 "candidates": candidates.get("candidates") or [],
                 "suggested_root": candidates.get("suggested") or "",
                 "hint": ("route_by_type=true 且类型目录存在时才分流；不存在的目录绝不新建。"
                          + (" 候选根：%s" % candidates.get("suggested")
                             if candidates.get("suggested") and not root else ""))}


_BATCH_MAX = 50
_BATCH_BLOCKED = {"/api/automation/batch"}


def _batch(api, ctx, body):
    """一次 HTTP 跑多条自动化调用：AI 省往返、省 token、省心跳等待。

    ```json
    {"calls": [{"method": "GET", "path": "/api/automation/tasks",
                "query": {"limit": 5, "fields": "id,status,progress"}},
               {"method": "POST", "path": "/api/automation/plan", "body": {"url": "..."}}],
     "stop_on_error": false}
    ```
    只允许调用本层端点（不允许嵌套 batch、不允许改走 /api/rpc），逐条按顺序执行，
    每条独立记录自己的 http_status：**batch 本身永远 200**，成败看 `results[].ok`。
    """
    b = body or {}
    calls = b.get("calls")
    if not isinstance(calls, list) or not calls:
        return _fail(400, "calls 必须是非空数组", '形如 {"calls":[{"method":"GET","path":"/api/automation/tasks"}]}')
    if len(calls) > _BATCH_MAX:
        return _fail(400, "calls 最多 %d 条（拆成多次调用）" % _BATCH_MAX, "把只读查询合并，写操作单独发")
    stop_on_error = bool(b.get("stop_on_error"))
    results = []
    for i, c in enumerate(calls):
        if not isinstance(c, dict):
            results.append({"index": i, "http_status": 400, "ok": False,
                            "result": {"ok": False, "msg": "每条 call 必须是对象"}})
            if stop_on_error:
                break
            continue
        method = str(c.get("method") or "GET").upper()
        path = str(c.get("path") or "")
        if not path.startswith("/api/automation"):
            results.append({"index": i, "http_status": 400, "ok": False, "result": {
                "ok": False, "msg": "path 必须是 /api/automation 下的端点",
                "hint": "需要长尾方法请直接 POST /api/rpc（batch 只包自动化层）"}})
            if stop_on_error:
                break
            continue
        if path in _BATCH_BLOCKED:
            results.append({"index": i, "http_status": 400, "ok": False,
                            "result": {"ok": False, "msg": "batch 不能嵌套 batch"}})
            if stop_on_error:
                break
            continue
        try:
            r = dispatch(path, method, _norm_query(c.get("query")), c.get("body"), api, ctx)
        except Exception as e:
            r = (500, {"ok": False, "msg": "调用异常: %s" % str(e)[:200]})
        if r is None:
            r = (404, {"ok": False, "msg": "端点不存在: %s" % path})
        status, payload = r
        if not isinstance(payload, dict):
            payload = {"ok": bool(status == 200), "result": payload}
        results.append({"index": i, "method": method, "path": path,
                        "http_status": status, "ok": bool(payload.get("ok")),
                        "result": payload})
        if stop_on_error and status >= 400:
            break
    return 200, {"ok": all(r["ok"] for r in results), "count": len(results),
                 "executed": len(results), "requested": len(calls), "results": results}


def _move(api, ctx, body):
    """移动模型或其落点：`path`+`dest_dir`（模型文件）/ `task_id`（下载任务）/ `history_id`（历史记录）。

    只接受**已存在**的绝对目录：不代建目录是刻意的——分流与整理都只写已注册的目录，
    凭空造一个目录 ComfyUI 扫不到，用户只会得到一堆看不见的模型。
    """
    b = body or {}
    dest = str(b.get("dest_dir") or "").strip()
    if not dest:
        return _fail(400, "dest_dir 不能为空", "可先用 GET /api/automation/storage 看有哪些目录")
    if not os.path.isdir(dest):
        return _fail(400, "目标目录不存在: %s" % dest, "只接受已存在的绝对目录（不代建）")
    path = str(b.get("path") or "").strip()
    task_id = str(b.get("task_id") or "").strip()
    history_id = str(b.get("history_id") or "").strip()
    try:
        if task_id:
            r = api.set_task_target(task_id, dest)
        elif history_id:
            r = api.history_move_to(history_id, dest)
        elif path:
            r = api.move_file_to(path, dest)
        else:
            return _fail(400, "path / task_id / history_id 至少给一个", "见 GET /api/automation/tasks 与 /history")
    except Exception as e:
        return _fail(500, "移动失败: %s" % str(e)[:200], "看 GET /api/automation/logs?source=error")
    r = _jsonish(r) or {}
    return (200 if isinstance(r, dict) and r.get("ok") else 400), r


def _rename(api, ctx, body):
    """重命名：单文件 `path`+`new_name`；批量 `paths`（`preview:true` 只算不改）。

    批量走 mm_rename，会连附属文件（info/json/封面）一起改名；预览走 mm_rename_preview，
    返回每条的新旧名与 `same`，执行前建议先预览。
    """
    b = body or {}
    paths = b.get("paths")
    preview = bool(b.get("preview"))
    if paths is not None or preview:
        if not isinstance(paths, list) or not paths:
            return _fail(400, "paths 必须是非空数组", '形如 {"paths":["D:/models/loras/a.safetensors"],"preview":true}')
        fn = api.mm_rename_preview if preview else api.mm_rename
        try:
            r = _jsonish(fn(paths=paths)) or {}
        except Exception as e:
            return _fail(500, "重命名失败: %s" % str(e)[:200], "看 GET /api/automation/logs?source=error")
        ok = bool(r.get("ok", r.get("started")))
        if not ok:
            return _fail(400, r.get("msg") or "重命名未开始", "先 GET /api/automation/library 确认模型已扫描")
        if not preview and not r.get("ok"):
            return 200, {"ok": True, "started": r,
                         "hint": "后台执行中，用 GET /api/automation/library/op?op=rename-c 看进度"}
        return 200, r
    path = str(b.get("path") or "").strip()
    new_name = str(b.get("new_name") or "").strip()
    if not path or not new_name:
        return _fail(400, "path + new_name 不能为空", "批量请传 paths（可先 preview:true 预览）")
    try:
        r = _jsonish(api.rename_file(path, new_name)) or {}
    except Exception as e:
        return _fail(500, "重命名失败: %s" % str(e)[:200], "看 GET /api/automation/logs?source=error")
    return (200 if isinstance(r, dict) and r.get("ok") else 400), r


_FS_MAX = 500


def _fs(api, ctx, query):
    """只读目录浏览：给 AI 一个「能选到真实落点」的入口。

    原生 pick_dir 弹窗必须有人点，AI 拿不到任何目录名；只靠 get_folders 又只能看到已登记的
    模型目录，于是「移动到任意目录」这类动作无从下手。这里只做两件事：列盘符、列一层目录名，
    **不读文件内容、不返回文件大小**，条目上限 500。
    """
    path = (query.get("path", [""])[0] or "").strip()
    dirs_only = str(query.get("dirs_only", ["1"])[0]).lower() not in ("0", "false", "no")
    if not path:
        roots = []
        if os.name == "nt":
            for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                d = letter + ":\\"
                if os.path.isdir(d):
                    roots.append({"name": d, "path": d, "dir": True})
        cfg = getattr(api, "cfg", None) or {}
        for extra in (os.path.expanduser("~"), str(cfg.get("download_dir") or "")):
            if extra and os.path.isdir(extra):
                roots.append({"name": extra, "path": extra, "dir": True})
        return 200, {"ok": True, "path": "", "parent": "", "entries": roots[:40],
                     "hint": "带 path=<某个目录> 再列一层；dirs_only=0 才包含文件"}
    if not os.path.isdir(path):
        return _fail(404, "目录不存在: %s" % path, "先用不带 path 的调用列盘符，再逐层进入")
    try:
        names = sorted(os.listdir(path),
                       key=lambda n: (not os.path.isdir(os.path.join(path, n)), n.lower()))
    except OSError as e:
        return _fail(400, "读取目录失败: %s" % str(e)[:120], "检查权限，或换一个可读目录")
    entries, truncated = [], False
    for name in names:
        full = os.path.join(path, name)
        try:
            is_dir = os.path.isdir(full)
        except OSError:
            is_dir = False
        if dirs_only and not is_dir:
            continue
        if len(entries) >= _FS_MAX:
            truncated = True
            break
        entries.append({"name": name, "path": full, "dir": is_dir})
    parent = os.path.dirname(os.path.abspath(path))
    return 200, {"ok": True, "path": path,
                 "parent": "" if os.path.normcase(parent) == os.path.normcase(path) else parent,
                 "entries": entries, "truncated": truncated}


def _storage_migrate(api, ctx, body):
    """把数据目录（配置/任务/历史/缓存）迁移到指定目录。会搬文件，必须显式 confirm:true。"""
    b = body or {}
    directory = str(b.get("directory") or "").strip()
    if not directory:
        return _fail(400, "directory 不能为空", "推荐先用 GET /api/automation/fs 选一个真实存在的目录")
    if not b.get("confirm"):
        return _fail(400, "迁移会移动配置/任务/历史/缓存，必须显式传 confirm:true",
                     "先 GET /api/automation/config 看 storage.current 与 storage.suggested",
                     code="destructive_requires_confirm")
    try:
        r = _jsonish(api.migrate_data_storage(directory, bool(b.get("replace_existing")))) or {}
    except Exception as e:
        return _fail(500, "迁移失败: %s" % str(e)[:200], "看 GET /api/automation/logs?source=error")
    if not isinstance(r, dict):
        r = {"ok": True, "raw": r}
    return (200 if r.get("ok") else 400), r


_REPAIR_STAGES = ("metadata", "comments", "cover", "video", "replace_old")


def _task_repair(api, ctx, body):
    """按 task_id 重跑下载收尾（可只跑失败的 stage），默认阻塞到完成。

    这是「收尾失败」的唯一收口：以前 AI 只能 verify / images/download / cover 分头补，
    既不知道补全没有，也没有一次做完的入口。
    """
    b = body or {}
    tid = str(b.get("task_id") or "").strip()
    if not tid:
        return _fail(400, "task_id 不能为空", "从 GET /api/automation/tasks?fields=id,status,postprocess_errors 取")
    stages = b.get("stages")
    if stages is not None and not isinstance(stages, list):
        return _fail(400, "stages 必须是数组", "可选: " + "|".join(_REPAIR_STAGES))
    try:
        r = _jsonish(api.repair_download_postprocess(tid, stages)) or {}
    except Exception as e:
        return _fail(500, "重跑失败: %s" % str(e)[:200], "看 GET /api/automation/logs?source=error")
    if not isinstance(r, dict):
        r = {"ok": True, "raw": r}
    if not r.get("ok"):
        return _fail(400, r.get("msg") or "重跑未开始", r.get("hint") or "看 GET /api/automation/tasks")
    wait = str(b.get("wait", "1")).lower() not in ("0", "false", "no")
    timeout = _int(b.get("timeout", 180), 180, 1, MAX_WAIT_S)
    if not wait:
        return 202, {**r, "hint": "用 GET /api/automation/tasks?ids=%s&fields=id,postprocess_repair 轮询" % tid}
    t0 = time.time()
    while time.time() - t0 < timeout:
        row = _find_task(api, tid)
        marker = (row or {}).get("postprocess_repair") or {}
        if marker.get("done"):
            return 200, {"ok": True, "finished": True, "task_id": tid,
                         "stages": marker.get("stages") or [],
                         "waited_s": round(time.time() - t0, 1), "timed_out": False,
                         "postprocess_errors": (row or {}).get("postprocess_errors") or []}
        time.sleep(0.3)
    row = _find_task(api, tid)
    return 200, {"ok": True, "finished": False, "timed_out": True, "task_id": tid,
                 "stages": ((row or {}).get("postprocess_repair") or {}).get("stages") or [],
                 "waited_s": round(time.time() - t0, 1),
                 "postprocess_errors": (row or {}).get("postprocess_errors") or [],
                 "hint": "仍在跑（视频可能较大）：再查一次或加大 timeout"}


# ---------------------------------------------------------------- 分发

def _history_action(api, ctx, body):
    """清理下载历史：`{action:"remove", ids:[...]}` / `{action:"clear", all:true}`。

    只删记录与历史缓存（缩略图/信息快照），**绝不删模型文件**——与「删除模型」正交。
    `preview=1` 只回报将删除的 id，不落盘；幂等：不存在的 id 记进 missing 而不报错。
    """
    b = body or {}
    action = str(b.get("action") or "remove").strip().lower()
    preview = str(b.get("preview", "0")).lower() in ("1", "true", "yes")
    if action not in ("remove", "clear"):
        return 400, {"ok": False, "msg": "action 只能是 remove 或 clear",
                     "hint": "remove 传 ids；clear（或 all:true）清空全部"}
    fn = getattr(api, "remove_download_history", None)
    if not callable(fn):
        return 501, {"ok": False, "msg": "当前版本不支持清理历史记录",
                     "hint": "升级桌面端后重试"}
    ids = b.get("ids")
    if ids is not None and not isinstance(ids, list):
        return 400, {"ok": False, "msg": "ids 必须是数组",
                     "hint": "先 GET /api/automation/history?limit=200 取 id"}
    kwargs = {"preview": preview}
    if action == "clear" or b.get("all"):
        kwargs["all"] = True
    else:
        kwargs["ids"] = [str(i) for i in (ids or []) if str(i or "").strip()]
        if not kwargs["ids"]:
            return 400, {"ok": False, "msg": "remove 需要 ids",
                         "hint": "GET /api/automation/history 取 id；清空全部用 {action:'clear'}"}
    try:
        out = fn(**kwargs) or {}
    except Exception as e:
        return 500, {"ok": False, "msg": "清理历史失败: %s" % str(e)[:200]}
    return 200, out


def _dispatch_impl(path, method, query, body, api, ctx):
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
        if path == "/api/automation/ai":
            return _ai(api, ctx, q)
        if path == "/api/automation/tools":
            return _tools(api, ctx, q)
        if path == "/api/automation/openapi.json":
            return _openapi(api, ctx, q)
        if path.startswith("/api/automation/tool/"):
            return _fail(405, "每工具操作必须用 POST",
                         "POST /api/automation/tool/{name}，body 是工具 arguments 对象；"
                         "GET 文档在 /api/automation/openapi.json")
        if path == "/api/automation/guide":
            return _guide(api, ctx, q)
        if path == "/api/automation/state":
            return _state(api, ctx, q)
        if path == "/api/automation/vision/model":
            return _vision_get(api, ctx, q, "cft_view_model")
        if path == "/api/automation/vision/gallery":
            return _vision_get(api, ctx, q, "cft_view_gallery")
        if path == "/api/automation/vision/workflow":
            return _vision_get(api, ctx, q, "cft_view_workflow")
        if path == "/api/automation/vision/contact-sheet":
            return _vision_get(api, ctx, q, "cft_contact_sheet")
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
        if path == "/api/automation/gallery":
            return _gallery_list(api, ctx, q)
        if path == "/api/automation/gallery/item":
            return _gallery_item(api, ctx, q)
        if path == "/api/automation/gallery/tags":
            return _gallery_tags(api, ctx, q)
        if path == "/api/automation/gallery/scan":
            return _gallery_scan_state(api, ctx)
        if path == "/api/automation/images/state":
            return _images_state(api, ctx, q)
        if path == "/api/automation/storage":
            return _storage(api, ctx)
        if path == "/api/automation/fs":
            return _fs(api, ctx, q)
        if path == "/api/automation/model":
            return _model_detail(api, ctx, q)
        if path == "/api/automation/extension":
            return _extension(api, ctx)
        if path == "/api/automation/connectivity":
            return _connectivity(api, ctx, q)
        return None

    if m == "POST":
        if path.startswith("/api/automation/tool/"):
            name = unquote(path[len("/api/automation/tool/"):]).strip("/")
            if not name or "/" in name:
                return _fail(404, "工具不存在",
                             "工具名来自 GET /api/automation/tools?format=mcp")
            fmt = str((q.get("content_format") or q.get("format") or ["neutral"])[0] or "neutral").strip().lower()
            return _tool_call(api, ctx, {"name": name,
                                         "arguments": {} if body is None else body,
                                         "content_format": fmt})
        if path == "/api/automation/tool":
            return _tool_call(api, ctx, body)
        if path == "/api/automation/vision/analyze":
            return _vision_analyze(api, ctx, body)
        if path == "/api/automation/batch":
            return _batch(api, ctx, body)
        if path == "/api/automation/history":
            return _history_action(api, ctx, body)
        if path == "/api/automation/tasks":
            return _tasks_action(api, ctx, body)
        if path == "/api/automation/tasks/repair":
            return _task_repair(api, ctx, body)
        if path == "/api/automation/ui/dismiss":
            return _ui_dismiss(api, ctx, body)
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
        if path == "/api/automation/gallery/op":
            return _gallery_op(api, ctx, body)
        if path == "/api/automation/gallery/scan":
            return _gallery_scan(api, ctx, body)
        if path == "/api/automation/images/download":
            return _images_download(api, ctx, body)
        if path == "/api/automation/workflow/analyze":
            return _workflow(api, ctx, body)
        if path == "/api/automation/move":
            return _move(api, ctx, body)
        if path == "/api/automation/storage/migrate":
            return _storage_migrate(api, ctx, body)
        if path == "/api/automation/rename":
            return _rename(api, ctx, body)
        if path == "/api/automation/config":
            return ctx["save_config"](body)
        return None

    return 405, {"ok": False, "msg": "方法不支持"}


def dispatch(path, method, query, body, api, ctx):
    """统一入口：执行后给所有错误补 code/retryable，调用方不必逐条写。"""
    result = _dispatch_impl(path, method, query, body, api, ctx)
    if result is None:
        return None
    status, payload = result
    if isinstance(payload, dict) and payload.get("ok") is False:
        payload = ai_contract.normalize_error(status, payload)
    return status, payload
