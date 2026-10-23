# -*- coding: utf-8 -*-
"""AI 工具执行层：把 ai_contract 的工具映射到现有 automation dispatch / vision_api。

MCP 侧车与 HTTP `/api/automation/tool` 都走这里；不允许各自复制下载、库管理或图片逻辑。
"""
from __future__ import annotations

import copy
import json
import os
import time
from urllib.parse import quote

import ai_contract
import webhook_dispatch


# cft_list_tasks 的默认投影：够判断「下没下完、落在哪、校验过没有」，
# 又不必把 postprocess/url/speed 这些每行几百字符的字段全发出去。
TASKS_COMPACT_FIELDS = ",".join((
    "id", "filename", "status", "progress", "error", "downloaded", "total",
    "dest_dir", "routed_dir", "route_reason", "hash_mismatch",
    "expected_sha256", "actual_sha256", "verification_status",
    "postprocess_errors", "model_id", "version_id", "modelName", "versionName",
    "created_at", "finished_at",
))

# MCP 文本块预算，与 _text() 的默认上限一致
TEXT_BUDGET = 4000


def _trim_tasks_to_fit(payload, budget=TEXT_BUDGET):
    """任务行是按时间追加的：超预算时从**最旧**开始丢，保住最新那几条。

    通用收缩器只会丢列表尾部，对这种有序数组正好反了（Agent 问「刚才那个下完没」
    结果看到的全是历史）。这里按已知语义裁，并如实标 dropped_rows/oldest_dropped。
    """
    if not isinstance(payload, dict):
        return payload
    rows = payload.get("tasks")
    if not isinstance(rows, list) or len(rows) < 2:
        return payload
    dropped = []

    def note():
        keys = ("truncated", "dropped_rows", "oldest_dropped_ids", "hint")
        if not dropped:
            for k in keys:
                payload.pop(k, None)
            return
        payload["truncated"] = True
        payload["dropped_rows"] = {"$.tasks": len(dropped)}
        payload["oldest_dropped_ids"] = [d for d in dropped if d]
        payload["hint"] = ("任务按时间追加，超预算时已丢最旧的 %d 条、保留最新的 %d 条；"
                           "要更全用 limit/fields 收窄再取，count 仍是总数"
                           % (len(dropped), len(rows)))

    while len(rows) > 1:
        note()          # 标记本身也占预算，先带上再量
        try:
            # 不能用 compact_json 量：它自己就会收缩，永远"放得下"，裁剪就从不触发
            size = len(json.dumps(payload, ensure_ascii=False,
                                  separators=(",", ":"), default=str))
        except Exception:
            return payload
        if size <= budget:
            break
        oldest = rows.pop(0)
        dropped.append(oldest.get("id") if isinstance(oldest, dict) else None)
    note()
    return payload


def _now_ms(t0):
    return round((time.time() - t0) * 1000.0, 1)


def _text(value, limit=TEXT_BUDGET):
    try:
        import ai_content
        return ai_content.compact_json(value, max_chars=limit)
    except Exception:
        try:
            s = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            s = str(value)
        return s[:limit] + ("…" if len(s) > limit else "")


def _content_text(text):
    try:
        import ai_content
        return ai_content.text_content(text)
    except Exception:
        return {"type": "text", "text": str(text)}


def _content_image(b64, mime, width=None, height=None, label=""):
    try:
        import ai_content
        return ai_content.image_content(b64, mime, width, height, label)
    except Exception:
        out = {"type": "image", "mime": mime, "b64": b64}
        if width is not None:
            out["width"] = width
        if height is not None:
            out["height"] = height
        if label:
            out["label"] = label
        return out


def _ok(tool, data, content=None, next_actions=None, t0=None, meta=None):
    payload = {
        "ok": True, "tool": tool, "data": data,
        "content": content if content is not None else [_content_text(_text(data))],
        "next_actions": list(next_actions or []),
        "meta": dict(meta or {}),
    }
    if t0 is not None:
        payload["meta"].setdefault("ms", _now_ms(t0))
    return 200, payload


def _err(tool, status, payload, t0=None):
    out = ai_contract.normalize_error(status, payload)
    out["tool"] = tool
    out["content"] = [_content_text(
        (out.get("msg") or "工具调用失败") + (("\n" + out["hint"]) if out.get("hint") else ""))]
    if t0 is not None:
        out.setdefault("meta", {})["ms"] = _now_ms(t0)
    return status, out


def _dispatch(dispatch_fn, api, ctx, method, path, query=None, body=None):
    return dispatch_fn(path, method, query or {}, body, api, ctx)


def _unwrap(tool, result, t0, next_actions=None):
    status, payload = result if isinstance(result, tuple) else (500, result)
    if status >= 400 or not isinstance(payload, dict) or payload.get("ok") is False:
        return _err(tool, status, payload if isinstance(payload, dict) else {"msg": str(payload)}, t0)
    return _ok(tool, payload, next_actions=next_actions, t0=t0)


def _q1(value, default=""):
    return [str(value if value is not None else default)]


def _csv(value):
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value if str(v).strip())
    return "" if value is None else str(value)


def _ids(value):
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return "" if value is None else str(value)


def _zip_next(tool, args, reason):
    return [{"tool": tool, "arguments": args, "why": reason}]


def _bridge_port(ctx):
    """本机桥端口：HTTP 自动化上下文会显式注入；MCP/测试默认 47531。"""
    if isinstance(ctx, dict) and ctx.get("bridge_port"):
        try:
            return int(ctx["bridge_port"])
        except (TypeError, ValueError):
            pass
    try:
        return int(os.environ.get("CFT_BRIDGE_PORT") or 47531)
    except (TypeError, ValueError):
        return 47531


def _browser_tabs_payload(tool, args, ctx, t0):
    """读取桌面桥里扩展上报的模型标签页索引（只读、本机回环）。"""
    import urllib.error
    import urllib.parse
    import urllib.request

    params = {}
    query = str(args.get("query") or "").strip()
    if query:
        params["q"] = query
    if args.get("limit") is not None:
        try:
            params["limit"] = max(1, min(200, int(args["limit"])))
        except (TypeError, ValueError):
            pass
    if args.get("include_discarded"):
        params["include_discarded"] = "1"
    if args.get("window_id") is not None:
        params["window_id"] = str(args["window_id"])
    if args.get("active"):
        params["active"] = "1"
    url = "http://127.0.0.1:%d/api/ext/tabs" % _bridge_port(ctx)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=4) as resp:
            raw = resp.read(1 << 20)
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception as e:
        return _err(tool, 503, {
            "msg": "读取浏览器标签页索引失败: %s" % str(e)[:160],
            "hint": "确认 CivitaiFreeTool 桌面端和浏览器扩展都在运行；扩展需重载到包含标签页索引上报的版本",
        }, t0)
    if not isinstance(data, dict) or data.get("ok") is not True:
        return _err(tool, 502, {"msg": "标签页索引返回格式异常"}, t0)
    items = [item for item in (data.get("items") or []) if isinstance(item, dict)]
    data["items"] = items
    next_actions = []
    if items and items[0].get("url"):
        next_actions = _zip_next(
            "cft_download_model",
            {"url": items[0]["url"], "wait": True, "verify": True},
            "下载选中的模型页")
    return _ok(tool, data, next_actions=next_actions, t0=t0)


def _state_payload(api, ctx, compact=True, task_limit=5, fields=None):
    try:
        data = ctx["state"]()
    except Exception as e:
        return _err("cft_get_state", 500, {"msg": "读取状态失败: %s" % str(e)[:200]})
    if not isinstance(data, dict):
        return _err("cft_get_state", 500, {"msg": "状态返回格式异常"})
    out = dict(data)
    tasks = list(out.get("tasks") or [])
    try:
        limit = max(0, min(50, int(task_limit)))
    except (TypeError, ValueError):
        limit = 5
    out["tasks"] = tasks[-limit:] if limit else []
    if compact:
        out["config"] = {k: out["config"].get(k) for k in (
            "download_dir", "download_target_dir", "models_dir", "comfyui_models_root",
            "route_by_type", "ask_move_after_download", "site_domain", "proxy_enabled",
            "ssl_verify", "target_env", "organize_mode",
            "onboarding_done", "auto_guides") if k in (out.get("config") or {})}
    blockers = []
    next_actions = []
    guides = (out.get("ui") or {}).get("guides") if isinstance(out.get("ui"), dict) else None
    if isinstance(guides, dict) and guides.get("onboarding_pending"):
        blockers.append({
            "code": "onboarding_guide", "severity": "warning",
            "msg": "界面下次打开会弹出人类新手引导；AI 不需要，可直接关闭",
            "fix": {"tool": "cft_dismiss_ui", "arguments": {"permanent": True}},
        })
        next_actions = _zip_next("cft_dismiss_ui", {"permanent": True},
                                 "关闭新手引导并禁止以后自动弹出")
    overlays = guides.get("overlays") if isinstance(guides, dict) else None
    blocking_overlays = []
    for item in overlays or []:
        if not isinstance(item, dict) or not item.get("blocking"):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name == "onboarding":
            continue
        blocking_overlays.append(item)
    if blocking_overlays:
        names = []
        for item in blocking_overlays:
            label = item.get("title") or item.get("name")
            if label and label not in names:
                names.append(str(label))
        blockers.append({
            "code": "ui_overlay", "severity": "blocking",
            "msg": "界面有弹层挡路: %s；关闭确认框=取消，不会执行危险操作" % "、".join(names),
            "fix": {"tool": "cft_dismiss_ui", "arguments": {"targets": ["all"], "permanent": False}},
        })
        if not next_actions:
            next_actions = _zip_next("cft_dismiss_ui", {"targets": ["all"], "permanent": False},
                                     "关闭挡路弹层（确认框按取消处理）")
    pending = out.get("pending_file_choice")
    if pending:
        blockers.append({
            "code": "pending_file_choice", "severity": "blocking",
            "msg": "有下载在等待多文件选择",
            "fix": {"tool": "cft_answer_file_choice",
                    "arguments": {"identity": pending.get("id") if isinstance(pending, dict) else pending}},
        })
    for t in out.get("tasks") or []:
        if isinstance(t, dict) and t.get("status") == "error":
            blockers.append({"code": "task_error", "severity": "warning",
                             "msg": "最近任务失败: %s" % (t.get("error") or t.get("filename") or t.get("id")),
                             "task_id": t.get("id"),
                             "fix": {"tool": "cft_list_tasks", "arguments": {"ids": [t.get("id")]}}})
            break
    out["blockers"] = blockers
    if fields:
        names = [s.strip() for s in _csv(fields).split(",") if s.strip()]
        if names and isinstance(out.get("tasks"), list):
            try:
                import automation_api
                out["tasks"] = [automation_api._project_item(t, names) if isinstance(t, dict) else t
                                for t in out["tasks"]]
            except Exception:
                pass
    return _ok("cft_get_state", out, next_actions=next_actions)


def _extension_snapshot(ctx):
    """Compact extension state for cft_capabilities (no URLs, no page data)."""
    fn = (ctx or {}).get("extension")
    try:
        data = fn() if callable(fn) else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    ti = data.get("tab_index") if isinstance(data.get("tab_index"), dict) else {}
    online = bool(ti.get("at"))
    return {
        "online": online,
        "source": ti.get("source") or "",
        "version": ti.get("ext_version") or "",
        "index_age_s": ti.get("age_s"),
        "index_stale": ti.get("stale"),
        "tabs_indexed": ti.get("accepted", 0),
        "tabs_counts": ti.get("counts") or {},
        "download_requests": data.get("download_requests", 0),
        "archive_files_written": data.get("archive_files_written", 0),
        "active_bundle_tokens": data.get("active_bundle_tokens", 0),
        "hint": "" if online else "扩展尚未上报标签页索引：确认扩展已安装并重载，且桌面端正在运行",
    }


def _mcp_runtime(cfg):
    try:
        import mcp_autostart
        return mcp_autostart.status(cfg)
    except Exception:
        return {}


def capabilities_payload(api, ctx, include_tools=False):
    version = str((ctx or {}).get("version") or "")
    cfg = getattr(api, "cfg", None) or {}
    webhook_url = str(cfg.get("webhook_url") or "")
    raw_events = cfg.get("webhook_events")
    webhook_events = ([str(x) for x in raw_events if str(x).strip()]
                      if isinstance(raw_events, (list, tuple))
                      else list(ai_contract.WEBHOOK_DEFAULT_EVENT_GROUPS))
    if not webhook_events:
        webhook_events = list(ai_contract.WEBHOOK_DEFAULT_EVENT_GROUPS)
    webhook_enabled = cfg.get("webhook_enabled") is True
    data = {
        "ok": True, "app": "CivitaiFreeTool", "version": version,
        "api_version": ai_contract.API_VERSION,
        "mcp": {
            "name": ai_contract.MCP_SERVER_NAME,
            "server_version": ai_contract.MCP_SERVER_VERSION,
            "min_app_version": ai_contract.MIN_APP_VERSION,
            "stdio": "CivitaiFreeToolMCP.exe --stdio --toolsets core,vision",
            "http": "CivitaiFreeToolMCP.exe --http --port %d" % ai_contract.DEFAULT_MCP_HTTP_PORT,
            "http_endpoint": "http://127.0.0.1:%d/mcp" % ai_contract.DEFAULT_MCP_HTTP_PORT,
            **_mcp_runtime(getattr(api, "cfg", None)),
        },
        "bridge": {"url": ai_contract.DEFAULT_BRIDGE, "reachable": True},
        "extension": _extension_snapshot(ctx),
        "toolsets": {k: {"count": len(v), "tools": list(v)} for k, v in ai_contract.TOOLSETS.items()},
        "profiles": {k: {"toolsets": v, "count": len(ai_contract.tools_for_profile(k))}
                     for k, v in ai_contract.PROFILES.items()},
        "caps": {
            "openapi": True,
            "sse": True,
            "events": [e["type"] for e in ai_contract.EVENTS],
            "webhook": {
                "available": True,
                "enabled": webhook_enabled,
                "configured": bool(webhook_enabled and webhook_url
                                   and str(cfg.get("webhook_secret") or "")),
                "url_host": webhook_dispatch.url_info(webhook_url)["host"],
                "events": webhook_events,
                "signature": "HMAC-SHA256",
                "endpoint": "/api/automation/webhooks",
            },
            "cli": {
                "available": True,
                "command": "CivitaiFreeToolCLI.exe",
                "source": "cft_cli.py",
                "transport": "http-bridge",
                "bridge": ai_contract.DEFAULT_BRIDGE,
                "commands": ["health", "capabilities", "tools", "guide", "openapi",
                             "call", "batch", "events", "doctor"],
                "exit_codes": dict(ai_contract.CLI_EXIT_CODES),
            },
            "transports": {
                "openapi": "/api/automation/openapi.json?profile=text|vision|admin|all",
                "sse": "/api/automation/events/stream?since=&types=&snapshot=1",
                "webhook": "/api/automation/webhooks",
                "cli": "CivitaiFreeToolCLI.exe --bridge %s doctor" % ai_contract.DEFAULT_BRIDGE,
            },
        },
        "limits": dict(ai_contract.LIMITS),
        "guide": {
            "text": "/api/automation/guide?profile=text",
            "vision": "/api/automation/guide?profile=vision",
        },
        "resources": [r["uri"] for r in ai_contract.RESOURCES],
        "prompts": [p["name"] for p in ai_contract.PROMPTS],
    }
    if include_tools:
        data["tools"] = [ai_contract.public_tool(t, format="mcp") for t in ai_contract.TOOLS.values()]
    return data


def _vision_package(tool, result, t0):
    """Vision result -> common envelope; image bytes only in content, not data."""
    if not isinstance(result, dict) or result.get("ok") is False:
        status = 400
        if isinstance(result, dict) and result.get("code") in ("invalid_media", "not_found"):
            status = 404 if result.get("code") == "not_found" else 400
        return _err(tool, status, result if isinstance(result, dict) else {"msg": str(result)}, t0)
    data = copy.deepcopy(result)
    items = data.get("items") if isinstance(data.get("items"), list) else []
    # inspect_image 的返回是单图顶层结构（image/meta/width...），统一成 items 再打包。
    if not items and isinstance(data.get("image"), dict):
        subject = data.get("subject") if isinstance(data.get("subject"), dict) else {}
        items = [{
            "id": subject.get("filename") or data.get("filename") or "image",
            "label": "#1",
            "text": (data.get("filename") or "image") + (
                " %sx%s" % (data.get("width"), data.get("height")) if data.get("width") else ""),
            "meta": data.get("meta") if isinstance(data.get("meta"), dict) else {},
            "image": data["image"],
            "path": data.get("path") or subject.get("path") or "",
            "source": data.get("source") or "upload",
            "is_video": False,
        }]
        data["items"] = items
        data.pop("image", None)
    content = []
    clean_items = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        label = item.get("label") or "#%d" % (i + 1)
        text = item.get("text") or item.get("path") or item.get("id") or label
        content.append(_content_text("%s %s" % (label, text)))
        img = item.get("image") if isinstance(item.get("image"), dict) else None
        clean = {k: v for k, v in item.items() if k != "image"}
        if img and img.get("b64"):
            content.append(_content_image(img.get("b64"), img.get("mime") or "image/jpeg",
                                          img.get("width"), img.get("height"), label))
            clean["image_meta"] = {k: img.get(k) for k in ("mime", "width", "height", "bytes")
                                   if img.get(k) is not None}
        elif item.get("path"):
            clean["image_path"] = item.get("path")
        clean_items.append(clean)
    data["items"] = clean_items
    if not content:
        content.append(_content_text(data.get("note") or data.get("hint") or "没有可返回的图片"))
    next_actions = []
    if tool == "cft_contact_sheet" and data.get("mapping"):
        ids = [m.get("id") for m in data["mapping"] if isinstance(m, dict) and m.get("id")]
        if ids:
            next_actions = _zip_next("cft_view_gallery", {"ids": ids[:8]}, "查看选中的全尺寸图片")
    elif tool == "cft_view_gallery":
        ids = [it.get("id") for it in clean_items if it.get("id")]
        if ids:
            next_actions = _zip_next("cft_annotate_gallery", {"op": "star", "ids": ids},
                                     "用户确认后可收藏这些条目")
    return _ok(tool, data, content=content, next_actions=next_actions, t0=t0)


def _describe_model(dispatch_fn, api, ctx, args, t0):
    path = str(args.get("path") or "").strip()
    if not path:
        return _err("cft_describe_model", 400, {"msg": "path 不能为空"}, t0)
    result = _dispatch(dispatch_fn, api, ctx, "GET", "/api/automation/model",
                       {"path": _q1(path), "mode": ["text"]})
    status, payload = result
    if status >= 400 or not isinstance(payload, dict) or payload.get("ok") is False:
        return _err("cft_describe_model", status, payload if isinstance(payload, dict) else {"msg": str(payload)}, t0)
    detail = payload.get("detail") or {}
    info = detail.get("info") if isinstance(detail, dict) else {}
    info = info if isinstance(info, dict) else {}
    covers = detail.get("covers") if isinstance(detail, dict) else []
    covers = covers if isinstance(covers, list) else []
    top = [c for c in covers if isinstance(c, dict)][:max(0, min(20, int(args.get("limit_images") or 4)))]
    lines = ["模型: %s" % (info.get("name") or detail.get("name") or path),
             "类型: %s | 底模: %s" % (info.get("type") or "", info.get("baseModel") or "")]
    words = info.get("trainedWords") or info.get("trained_words") or info.get("trigger_words") or []
    if words:
        lines.append("触发词: " + ", ".join(str(w) for w in words[:20]))
    for i, c in enumerate(top):
        bits = [str(c.get("local_path") or c.get("url") or c.get("orig_url") or "")]
        if args.get("include_prompts", True) and c.get("prompt"):
            bits.append("prompt=" + str(c["prompt"])[:400])
        lines.append("#%d %s" % (i + 1, " | ".join(bits)))
    if not covers:
        lines.append("没有本地封面或示例图；可先 cft_verify_model 或 cft_repair_model。")
    data = {"path": path, "model": info.get("name") or "", "type": info.get("type") or "",
            "base_model": info.get("baseModel") or "", "trigger_words": words[:20],
            "images": [{"label": "#%d" % (i + 1), "path": c.get("local_path") or "",
                        "url": c.get("url") or c.get("orig_url") or "",
                        "prompt": (c.get("prompt") or "")[:800] if args.get("include_prompts", True) else ""}
                       for i, c in enumerate(top)],
            "media_omitted": payload.get("media_omitted", 0)}
    return _ok("cft_describe_model", data,
               content=[_content_text("\n".join(lines))],
               next_actions=_zip_next("cft_view_model", {"path": path}, "需要看图时使用视觉工具"),
               t0=t0)


def execute_tool(name, arguments, api, ctx, dispatch_fn):
    """Execute one contract tool. Returns (http_status, common_envelope)."""
    t0 = time.time()
    name = str(name or "").strip()
    args = arguments if isinstance(arguments, dict) else {}
    tool = ai_contract.TOOLS.get(name)
    if not tool:
        return _err(name, 404, {"msg": "未知工具: %s" % name,
                                "hint": "调用 GET /api/automation/tools 获取工具表"}, t0)

    handler = tool["handler"]
    try:
        if handler == "capabilities":
            return _ok(name, capabilities_payload(api, ctx, bool(args.get("include_tools"))), t0=t0)

        if handler == "state":
            return _state_payload(api, ctx, args.get("compact", True), args.get("task_limit", 5),
                                  args.get("fields"))

        if handler == "search":
            body = {k: args.get(k) for k in ("query", "types", "base_models", "sort", "period", "nsfw", "limit")
                    if args.get(k) is not None}
            status, payload = _dispatch(dispatch_fn, api, ctx, "POST", "/api/automation/search", body=body)
            if status >= 400 or not isinstance(payload, dict) or payload.get("ok") is False:
                return _err(name, status, payload if isinstance(payload, dict) else {"msg": str(payload)}, t0)
            items = payload.get("items") or []
            next_actions = []
            if items and isinstance(items[0], dict) and items[0].get("id"):
                next_actions = _zip_next("cft_resolve_model", {"model_id": items[0]["id"]},
                                         "检查版本/文件与落点")
            return _ok(name, payload, next_actions=next_actions, t0=t0)

        if handler == "resolve":
            body = {k: args.get(k) for k in ("url", "model_id", "version_id", "file_id", "probe")
                    if args.get(k) is not None}
            status, payload = _dispatch(dispatch_fn, api, ctx, "POST", "/api/automation/resolve", body=body)
            if status >= 400 or not isinstance(payload, dict) or payload.get("ok") is False:
                return _err(name, status, payload if isinstance(payload, dict) else {"msg": str(payload)}, t0)
            files = [f for f in (payload.get("files") or []) if isinstance(f, dict)]
            missing = [f for f in files if not f.get("exists")]
            next_actions = []
            if missing:
                dl = {"model_id": payload.get("model", {}).get("id"),
                      "version_id": payload.get("version", {}).get("id"),
                      "file_ids": [f.get("file_id") for f in missing if f.get("file_id")],
                      "wait": True, "verify": True}
                next_actions = _zip_next("cft_download_model", dl, "下载不存在的文件")
            elif files:
                f0 = files[0]
                path = os.path.join(f0.get("routed_dir") or "", f0.get("name") or "") if f0.get("routed_dir") else ""
                next_actions = _zip_next("cft_verify_model", {"path": path} if path else {},
                                         "文件已在盘上，校验附属文件")
            placement = next((f.get("placement") for f in files
                              if isinstance(f.get("placement"), dict)), {}) or {}
            if placement.get("status") in ("under_root", "outside_root"):
                next_actions += _zip_next(
                    "cft_diagnostics", {"source": "storage"},
                    "落点未进类型目录：查看候选模型根与分流配置")
            return _ok(name, payload, next_actions=next_actions, t0=t0)

        if handler == "download":
            body = {k: args.get(k) for k in (
                "url", "urls", "model_id", "version_id", "file_ids", "dest_subdir", "caps",
                "wait", "timeout_s", "verify", "force") if args.get(k) is not None}
            if "timeout_s" in body:
                body["timeout"] = body.pop("timeout_s")
            status, payload = _dispatch(dispatch_fn, api, ctx, "POST", "/api/automation/download", body=body)
            if status >= 400 or not isinstance(payload, dict) or payload.get("ok") is False:
                return _err(name, status, payload if isinstance(payload, dict) else {"msg": str(payload)}, t0)
            next_actions = []
            if payload.get("skipped") == "exists":
                f0 = next((f for f in (payload.get("files") or []) if isinstance(f, dict)), {})
                path = ""
                if f0.get("routed_dir") and f0.get("name"):
                    path = os.path.join(f0["routed_dir"], f0["name"])
                elif f0.get("path"):
                    path = f0["path"]
                next_actions = _zip_next("cft_verify_model", {"path": path} if path else {},
                                         "文件已存在，校验而不是重下")
            elif payload.get("timed_out"):
                ids = []
                for item in payload.get("items") or []:
                    if isinstance(item, dict):
                        tid = item.get("id") or ((item.get("task") or {}).get("id")
                                                 if isinstance(item.get("task"), dict) else None)
                        if tid:
                            ids.append(tid)
                if payload.get("task") and payload["task"].get("id"):
                    ids.append(payload["task"]["id"])
                next_actions = _zip_next("cft_list_tasks", {"ids": ids, "wait": True},
                                         "等待剩余下载完成")
            else:
                ids = []
                for item in payload.get("items") or []:
                    if isinstance(item, dict):
                        tid = item.get("id") or ((item.get("task") or {}).get("id")
                                                 if isinstance(item.get("task"), dict) else None)
                        if tid:
                            ids.append(tid)
                if payload.get("task") and payload["task"].get("id"):
                    ids.append(payload["task"]["id"])
                if ids:
                    next_actions = _zip_next("cft_verify_model", {"task_id": ids[0]},
                                             "核对文件与附属文件")
            return _ok(name, payload, next_actions=next_actions, t0=t0)

        if handler == "tasks_list":
            q = {}
            for k in ("ids", "status"):
                if args.get(k):
                    q[k] = [_ids(args[k])]
            for k in ("limit", "since"):
                if args.get(k) is not None:
                    q[k] = _q1(args[k])
            if args.get("wait") is not None:
                q["wait"] = ["1" if args.get("wait") else "0"]
            if args.get("timeout_s") is not None:
                q["timeout"] = _q1(args["timeout_s"])
            if args.get("fields"):
                q["fields"] = [_csv(args["fields"])]
            else:
                # MCP 文本块有 4000 字符预算：任务行 26 个字段全发时 6 行就超限，
                # 收缩只能整行丢弃、会把最新的任务丢到。默认给紧凑投影，fields="*" 仍要全量。
                q["fields"] = [_csv(TASKS_COMPACT_FIELDS)]
            status, payload = _dispatch(dispatch_fn, api, ctx, "GET", "/api/automation/tasks", q)
            if status >= 400 or not isinstance(payload, dict) or payload.get("ok") is False:
                return _err(name, status, payload if isinstance(payload, dict) else {"msg": str(payload)}, t0)
            payload = _trim_tasks_to_fit(payload)
            next_actions = []
            if payload.get("timed_out"):
                next_actions = _zip_next("cft_list_tasks",
                                         {"ids": args.get("ids") or [], "wait": True},
                                         "继续等待剩余任务")
            elif any(t.get("status") == "error" for t in (payload.get("tasks") or []) if isinstance(t, dict)):
                next_actions = _zip_next("cft_diagnostics", {"source": "logs"}, "读取失败原因")
            return _ok(name, payload, next_actions=next_actions, t0=t0)

        if handler == "tasks_control":
            body = {"action": args.get("action"), "ids": args.get("ids") or [], "all": bool(args.get("all"))}
            return _unwrap(name, _dispatch(dispatch_fn, api, ctx, "POST", "/api/automation/tasks", body=body), t0)

        if handler == "file_choice":
            body = {k: args.get(k) for k in ("identity", "indices", "pause", "ack") if args.get(k) is not None}
            return _unwrap(name, _dispatch(dispatch_fn, api, ctx, "POST", "/api/automation/file-choice", body=body), t0)

        if handler == "dismiss_ui":
            body = {}
            if args.get("targets") is not None:
                body["targets"] = args.get("targets")
            if args.get("permanent") is not None:
                body["permanent"] = args.get("permanent")
            return _unwrap(name, _dispatch(dispatch_fn, api, ctx, "POST", "/api/automation/ui/dismiss", body=body), t0)

        if handler == "verify":
            q = {}
            for k in ("task_id", "path"):
                if args.get(k):
                    q[k] = _q1(args[k])
            q["hash"] = ["1" if args.get("hash") else "0"]
            status, payload = _dispatch(dispatch_fn, api, ctx, "GET", "/api/automation/verify", q)
            if status >= 400 or not isinstance(payload, dict) or payload.get("ok") is False:
                return _err(name, status, payload if isinstance(payload, dict) else {"msg": str(payload)}, t0)
            next_actions = []
            if payload.get("ok") and not payload.get("bundle_ok") and args.get("task_id"):
                next_actions = _zip_next("cft_repair_model", {"task_id": args["task_id"], "wait": True},
                                         "模型文件可用，但收尾附属文件不齐")
            return _ok(name, payload, next_actions=next_actions, t0=t0)

        if handler == "repair":
            body = {"task_id": args.get("task_id")}
            if args.get("stages"):
                body["stages"] = args["stages"]
            body["wait"] = True if args.get("wait") is None else bool(args.get("wait"))
            if args.get("timeout_s") is not None:
                body["timeout"] = args["timeout_s"]
            return _unwrap(name, _dispatch(dispatch_fn, api, ctx, "POST", "/api/automation/tasks/repair", body=body), t0)

        if handler == "library_list":
            q = {}
            for k in ("q", "type"):
                if args.get(k) is not None:
                    q[k] = _q1(args[k])
            for k in ("limit", "offset"):
                if args.get(k) is not None:
                    q[k] = _q1(args[k])
            if args.get("fields"):
                q["fields"] = [_csv(args["fields"])]
            return _unwrap(name, _dispatch(dispatch_fn, api, ctx, "GET", "/api/automation/library", q), t0)

        if handler == "library_op":
            op = str(args.get("op") or "")
            if op in ("dedupe-delete", "cleanup") and not args.get("confirm"):
                return _err(name, 400, {"code": "destructive_requires_confirm",
                                        "msg": "%s 会删除/移动文件，必须显式 confirm=true" % op,
                                        "hint": "先跑 dedupe / rename-preview 预览，再带 confirm=true"}, t0)
            body = {k: args.get(k) for k in (
                "op", "paths", "wait", "overwrite", "force", "confirm") if args.get(k) is not None}
            if args.get("timeout_s") is not None:
                body["timeout"] = args["timeout_s"]
            return _unwrap(name, _dispatch(dispatch_fn, api, ctx, "POST", "/api/automation/library/op", body=body), t0)

        if handler == "gallery_list":
            flt = {}
            for key in ("kind", "starred", "q", "sort", "dirs", "tag_ids", "checkpoints", "loras"):
                if args.get(key) is not None:
                    flt[key] = args[key]
            q = {"filter": [json.dumps(flt, ensure_ascii=False)],
                 "limit": _q1(args.get("limit", 50)), "offset": _q1(args.get("offset", 0)),
                 "with_facets": ["1" if args.get("with_facets") else "0"]}
            if args.get("fields"):
                q["fields"] = [_csv(args["fields"])]
            return _unwrap(name, _dispatch(dispatch_fn, api, ctx, "GET", "/api/automation/gallery", q), t0)

        if handler == "gallery_op":
            body = {k: args.get(k) for k in ("op", "ids", "on", "tag_ids", "id", "name", "color", "parent_id")
                    if args.get(k) is not None}
            return _unwrap(name, _dispatch(dispatch_fn, api, ctx, "POST", "/api/automation/gallery/op", body=body), t0)

        if handler == "workflow":
            body = {"path": args.get("path"), "full": bool(args.get("full"))}
            if args.get("include_image"):
                body["include_image"] = True
                body["max_side"] = args.get("max_side", 1024)
            status, payload = _dispatch(dispatch_fn, api, ctx, "POST", "/api/automation/workflow/analyze", body=body)
            if status >= 400 or not isinstance(payload, dict) or payload.get("ok") is False:
                return _err(name, status, payload if isinstance(payload, dict) else {"msg": str(payload)}, t0)
            return _ok(name, payload, t0=t0)

        if handler == "diagnostics":
            source = str(args.get("source") or "all")
            tail = max(1, min(500, int(args.get("tail") or 80)))
            data = {}
            if source in ("logs", "all"):
                data["logs"] = _dispatch(dispatch_fn, api, ctx, "GET", "/api/automation/logs",
                                          {"source": ["both"], "tail": _q1(tail)})[1]
            if source in ("events", "all"):
                data["events"] = _dispatch(dispatch_fn, api, ctx, "GET", "/api/automation/events",
                                           {"limit": _q1(min(200, tail))})[1]
            if source in ("connectivity", "all"):
                data["connectivity"] = _dispatch(dispatch_fn, api, ctx, "GET", "/api/automation/connectivity")[1]
            if source in ("storage", "all"):
                data["storage"] = _dispatch(dispatch_fn, api, ctx, "GET", "/api/automation/storage")[1]
            if source in ("extension", "all"):
                data["extension"] = _dispatch(dispatch_fn, api, ctx, "GET", "/api/automation/extension")[1]
            return _ok(name, data, t0=t0)

        if handler == "browser_tabs":
            return _browser_tabs_payload(name, args, ctx, t0)

        if handler == "describe_model":
            return _describe_model(dispatch_fn, api, ctx, args, t0)

        if handler in ("view_model", "view_gallery", "view_workflow", "contact_sheet", "inspect_image"):
            import vision_api
            if handler == "view_model":
                r = vision_api.model_vision(api, args.get("path"), args.get("limit_images", 4),
                                            args.get("max_side", 768), args.get("delivery", "inline"),
                                            args.get("include_prompts", True), bool(args.get("safe")))
            elif handler == "view_gallery":
                r = vision_api.gallery_vision(api, args.get("ids"), args.get("max_side", 800),
                                              args.get("delivery", "inline"), args.get("include_meta", True))
            elif handler == "view_workflow":
                r = vision_api.workflow_vision(api, args.get("path"), args.get("max_side", 1024),
                                               args.get("delivery", "inline"))
            elif handler == "contact_sheet":
                r = vision_api.contact_sheet(api, args.get("source"), args.get("ids"), args.get("path"),
                                             args.get("limit", 16), args.get("cols", 4),
                                             args.get("cell", 256), args.get("delivery", "inline"))
            else:
                r = vision_api.inspect_image(api, args.get("path"), args.get("data_b64"),
                                             args.get("filename", ""), args.get("max_side", 768),
                                             args.get("delivery", "inline"), args.get("include_image", True))
            return _vision_package(name, r, t0)

        if handler == "manage_files":
            action = str(args.get("action") or "")
            if action == "move":
                body = {k: args.get(k) for k in ("path", "dest_dir", "task_id", "history_id") if args.get(k)}
                return _unwrap(name, _dispatch(dispatch_fn, api, ctx, "POST", "/api/automation/move", body=body), t0)
            if action == "rename":
                body = {k: args.get(k) for k in ("path", "new_name", "paths", "preview") if args.get(k) is not None}
                return _unwrap(name, _dispatch(dispatch_fn, api, ctx, "POST", "/api/automation/rename", body=body), t0)
            if action == "list":
                q = {"dirs_only": ["1" if args.get("dirs_only", True) else "0"]}
                if args.get("path"):
                    q["path"] = _q1(args["path"])
                return _unwrap(name, _dispatch(dispatch_fn, api, ctx, "GET", "/api/automation/fs", q), t0)
            return _err(name, 400, {"msg": "action 必须是 move|rename|list"}, t0)

        if handler == "configure":
            action = str(args.get("action") or "")
            if action == "get":
                return _unwrap(name, _dispatch(dispatch_fn, api, ctx, "GET", "/api/automation/config"), t0)
            if action == "set":
                cfg = args.get("config")
                if not isinstance(cfg, dict) or not cfg:
                    return _err(name, 400, {"msg": "config 必须是非空对象"}, t0)
                return _unwrap(name, _dispatch(dispatch_fn, api, ctx, "POST", "/api/automation/config",
                                               body={"config": cfg}), t0)
            return _err(name, 400, {"msg": "action 必须是 get|set"}, t0)

        if handler == "history":
            action = str(args.get("action") or "list")
            if action == "list":
                q = {}
                for k in ("limit", "status"):
                    if args.get(k) is not None:
                        q[k] = _q1(args[k])
                q["detail"] = ["1" if args.get("detail") else "0"]
                return _unwrap(name, _dispatch(dispatch_fn, api, ctx, "GET", "/api/automation/history", q), t0)
            if action in ("remove", "clear"):
                body = {"action": action}
                if args.get("ids"):
                    body["ids"] = args["ids"]
                if args.get("preview"):
                    body["preview"] = True
                return _unwrap(name, _dispatch(dispatch_fn, api, ctx, "POST", "/api/automation/history", body=body), t0)
            return _err(name, 400, {"msg": "action 必须是 list|remove|clear"}, t0)

        if handler == "migrate_storage":
            if not args.get("confirm"):
                return _err(name, 400, {"code": "destructive_requires_confirm",
                                        "msg": "迁移数据目录必须显式 confirm=true",
                                        "hint": "先确认目录与 replace_existing，再重试"}, t0)
            body = {"directory": args.get("directory"), "replace_existing": bool(args.get("replace_existing")),
                    "confirm": True}
            return _unwrap(name, _dispatch(dispatch_fn, api, ctx, "POST", "/api/automation/storage/migrate", body=body), t0)

        if handler == "delete_gallery":
            if not args.get("confirm"):
                return _err(name, 400, {"code": "destructive_requires_confirm",
                                        "msg": "删除媒体会移入回收站，必须显式 confirm=true",
                                        "hint": "先 cft_browse_gallery 确认 ids，再重试"}, t0)
            body = {"op": "delete", "ids": args.get("ids"), "confirm": True}
            return _unwrap(name, _dispatch(dispatch_fn, api, ctx, "POST", "/api/automation/gallery/op", body=body), t0)

        return _err(name, 501, {"msg": "工具执行器未实现: %s" % handler}, t0)
    except Exception as e:
        return _err(name, 500, {"msg": "工具执行异常: %s" % str(e)[:200]}, t0)


def format_content(payload, fmt="neutral"):
    """Convert a common envelope's neutral content for HTTP clients."""
    if not isinstance(payload, dict) or fmt in ("", "neutral", None):
        return payload
    try:
        import ai_content
        if fmt == "openai":
            content = ai_content.to_openai(payload.get("content") or [])
        elif fmt == "anthropic":
            content = ai_content.to_anthropic(payload.get("content") or [])
        elif fmt == "mcp":
            content = ai_content.to_mcp(payload.get("content") or [])
        else:
            return payload
    except Exception:
        return payload
    out = dict(payload)
    out["content"] = content
    return out
