# -*- coding: utf-8 -*-
"""OpenAPI 3.1 投影：把 ai_contract 的工具表变成可导入的 HTTP 操作。

只做文档生成，不执行工具。每个 operation 对应
`POST /api/automation/tool/{tool_name}`，执行仍由 automation_api._tool_call
复用 ai_tools.execute_tool，避免出现第二套工具逻辑。
"""
from __future__ import annotations

import re

import ai_contract

OPENAPI_VERSION = "3.1.0"

_LOOPBACK_HOST_RE = re.compile(r"^(127\.0\.0\.1|localhost)(?::(\d{1,5}))?$", re.I)


def server_url(host_header="", fallback=None):
    """只信任回环 Host，防止生成指向任意主机的 servers 条目。"""
    host = str(host_header or "").strip()
    match = _LOOPBACK_HOST_RE.match(host)
    if match:
        try:
            port = int(match.group(2) or 0)
        except (TypeError, ValueError):
            port = 0
        if not port:
            port = 47531
        if 1 <= port <= 65535:
            return "http://%s:%d" % (match.group(1), port)
    return str(fallback or ai_contract.DEFAULT_BRIDGE)


def _tool_path(tool):
    return "/api/automation/tool/%s" % tool["name"]


def _requires_confirm(tool):
    props = ((tool.get("inputSchema") or {}).get("properties") or {})
    return "confirm" in props


def _error_response(description):
    return {
        "description": description,
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}},
    }


def _operation(tool):
    schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
    props = schema.get("properties") or {}
    annotations = tool.get("annotations") or {}
    destructive = bool(annotations.get("destructiveHint"))
    operation = {
        "operationId": tool["name"],
        "summary": tool.get("title") or tool["name"],
        "description": tool.get("description") or "",
        "tags": [tool.get("toolset") or "core"],
        "parameters": [{
            "name": "content_format",
            "in": "query",
            "required": False,
            "description": "图片内容块格式；无图片结果时不影响。",
            "schema": {"type": "string", "enum": ["neutral", "openai", "anthropic", "mcp"],
                       "default": "neutral"},
        }],
        "requestBody": {
            "required": bool(schema.get("required")),
            "content": {"application/json": {"schema": schema}},
        },
        "responses": {
            "200": {
                "description": "工具结果信封（ok/data/content/next_actions/meta）",
                "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/ToolResult"}}},
            },
            "400": _error_response("参数或状态不合法"),
            "404": _error_response("工具不存在或目标对象不存在"),
            "405": _error_response("方法不允许；工具操作必须使用 POST"),
            "413": _error_response("请求体超过该路由限制"),
            "500": _error_response("执行失败"),
            "501": _error_response("当前桌面版本不支持该能力"),
            "503": _error_response("RPC/桥服务尚未就绪"),
        },
        "x-cft-toolset": tool.get("toolset") or "core",
        "x-cft-returns": tool.get("returns") or "text",
        "x-cft-annotations": dict(annotations),
        "x-cft-destructive": destructive,
        "x-cft-requires-confirm": _requires_confirm(tool),
        "x-cft-waitable": "wait" in props,
        "x-cft-idempotent": bool(annotations.get("idempotentHint")),
        "x-cft-open-world": bool(annotations.get("openWorldHint")),
    }
    return operation


def _event_catalog():
    return [
        {"type": item["type"], "group": item["group"],
         "description": item["description"], "dataSchema": item["data"]}
        for item in ai_contract.EVENTS
    ]


def build_document(profile="text", *, host_header="", app_version="", fallback_server=None):
    """生成 OpenAPI 文档；profile 只决定暴露哪些工具，不改变执行权限。"""
    profile = str(profile or "text").strip().lower()
    if profile not in ai_contract.PROFILES:
        raise ValueError("unknown profile: %s" % profile)
    tools = ai_contract.tools_for_profile(profile)
    paths = {_tool_path(tool): {"post": _operation(tool)} for tool in tools}
    toolset_desc = {
        "core": "发现、状态、搜索、下载、任务、文件选择、弹层关闭、校验、库/资源库查询",
        "vision": "图片/视频帧与 contact sheet（返回图片内容块或路径）",
        "admin": "文件移动/改名、配置写入、历史清理、存储迁移、资源库删除",
    }
    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": "CivitaiFreeTool Local Agent API",
            "version": str(app_version or ai_contract.MIN_APP_VERSION),
            "description": (
                "Local-only function-calling projection of CivitaiFreeTool. "
                "Each operation is POST /api/automation/tool/{tool_name}; request body is the "
                "tool's arguments object. The bridge listens on 127.0.0.1 only and is not "
                "reachable from cloud or containerized agents without host networking. "
                "Confirmation dialogs are canceled, never confirmed. "
                "Profile controls discovery only; destructive tools still require confirm=true."
            ),
            "x-cft-api-version": ai_contract.API_VERSION,
            "x-cft-min-app-version": ai_contract.MIN_APP_VERSION,
        },
        "servers": [{
            "url": server_url(host_header, fallback_server),
            "description": "CivitaiFreeTool local bridge (loopback only)",
        }],
        "tags": [
            {"name": name, "description": toolset_desc.get(name, "")}
            for name in ai_contract.TOOLSETS
        ],
        "paths": paths,
        "components": {
            "schemas": {
                "ToolResult": {
                    "type": "object",
                    "description": "工具结果；data 的结构随工具而异，content 可能包含图片内容块。",
                    "additionalProperties": True,
                    "properties": {
                        "ok": {"type": "boolean"},
                        "tool": {"type": "string"},
                        "data": {"type": "object", "additionalProperties": True},
                        "content": {"type": "array", "items": {"type": "object",
                                                               "additionalProperties": True}},
                        "next_actions": {"type": "array", "items": {"additionalProperties": True}},
                        "meta": {"type": "object", "additionalProperties": True},
                    },
                },
                "Error": {
                    "type": "object",
                    "description": "统一错误信封；不要重试 retryable=false 的错误。",
                    "additionalProperties": True,
                    "required": ["ok", "msg"],
                    "properties": {
                        "ok": {"type": "boolean", "const": False},
                        "code": {"type": "string"},
                        "retryable": {"type": "boolean"},
                        "msg": {"type": "string"},
                        "hint": {"type": "string"},
                        "next_actions": {"type": "array", "items": {"additionalProperties": True}},
                    },
                },
            },
        },
        "x-cft-profile": profile,
        "x-cft-toolsets": {
            name: {"count": len(names), "tools": list(names)}
            for name, names in ai_contract.TOOLSETS.items()
        },
        "x-cft-limits": dict(ai_contract.LIMITS),
        "x-cft-agent-transport-limits": dict(ai_contract.AGENT_TRANSPORT_LIMITS),
        "x-cft-large-body-tools": sorted(ai_contract.LARGE_BODY_TOOLS),
        "x-cft-events": _event_catalog(),
        "x-cft-transports": {
            "mcp": {"command": "CivitaiFreeToolMCP.exe --stdio",
                    "http": "http://127.0.0.1:%d/mcp" % ai_contract.DEFAULT_MCP_HTTP_PORT},
            "openapi": {"endpoint": "/api/automation/openapi.json",
                        "tool_alias": "/api/automation/tool/{name}"},
            "sse": {"endpoint": "/api/automation/events/stream",
                    "replay_header": "Last-Event-ID"},
            "webhook": {"endpoint": "/api/automation/webhooks",
                        "mode": "outbound", "signature": "HMAC-SHA256",
                        "configure": "/api/automation/config",
                        "events": list(ai_contract.WEBHOOK_DEFAULT_EVENT_GROUPS),
                        "limits": dict(ai_contract.WEBHOOK_LIMITS)},
            "cli": {"command": "CivitaiFreeToolCLI.exe",
                    "source": "cft_cli.py",
                    "bridge": ai_contract.DEFAULT_BRIDGE,
                    "exit_codes": dict(ai_contract.CLI_EXIT_CODES)},
        },
    }
