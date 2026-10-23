# -*- coding: utf-8 -*-
"""AI 工具契约：MCP、HTTP API、Skill 的唯一事实源。

这里只放**数据**：工具名、描述、JSON Schema、安全注解、toolset、限额、错误码、
资源与 prompt。执行逻辑在 ai_tools.py / vision_api.py / mcp_server.py，三者不得各自
手写一份工具表。
"""
from __future__ import annotations

API_VERSION = "1.0"
MIN_APP_VERSION = "2.7.0"
MCP_SERVER_NAME = "civitai-free-tool"
MCP_SERVER_VERSION = "1.0.0"
DEFAULT_BRIDGE = "http://127.0.0.1:47531"
DEFAULT_MCP_HTTP_PORT = 47532

LIMITS = {
    "max_images": 8,
    "max_image_side": 1536,
    "max_image_bytes": 3 * 1024 * 1024,
    "max_total_image_bytes": 8 * 1024 * 1024,
    "max_upload_bytes": 8 * 1024 * 1024,
    "max_gallery_ids": 500,
    "max_contact_items": 24,
    "max_batch": 50,
    "default_wait_s": 300,
    "max_wait_s": 1800,
}

# 只有接受 base64 图片的工具才需要把桥请求体从 1MB 放宽到 8MB。
LARGE_BODY_TOOLS = frozenset({"cft_inspect_image"})

AGENT_TRANSPORT_LIMITS = {
    "sse_max_clients": 8,
    "sse_queue_size": 256,
    "sse_ring_size": 500,
    "sse_heartbeat_s": 15.0,
    "sse_idle_stop_s": 30.0,
    "sse_sample_interval_s": 1.0,
    "event_data_max_bytes": 64 * 1024,
}

# CLI 是桥的客户端，不是第二个执行层：退出码只做传输语义映射。
CLI_VERSION = "1.0.0"
CLI_EXIT_CODES = {
    "ok": 0,
    "internal_error": 1,
    "usage": 2,
    "app_not_running": 3,
    "invalid_argument": 4,
    "not_found": 5,
    "destructive_requires_confirm": 6,
    "rate_limited": 7,
    "timeout": 8,
    "version_mismatch": 9,
    "unsupported": 9,
}

# webhook 是出站尽力推送：有界队列 + 有限重试，不做持久化/跨重启重放。
WEBHOOK_LIMITS = {
    "timeout_s": 5.0,
    "max_attempts": 4,
    "backoff_s": [1.0, 5.0, 15.0],
    "queue_size": 256,
    "log_size": 200,
    "max_body_bytes": 64 * 1024,
    "max_response_bytes": 64 * 1024,
    "min_secret_length": 16,
    "test_timeout_s": 10.0,
}
# api.call 是高频自检事件，默认不推；要收就显式写 "api" 或 "all"。
WEBHOOK_DEFAULT_EVENT_GROUPS = ("task", "file_choice", "scan", "ui", "config", "app")


def _obj(props=None, required=None, **extra):
    out = {"type": "object", "properties": props or {},
           "required": list(required or []), "additionalProperties": False}
    out.update(extra)
    return out


def _any_obj(description=""):
    out = {"type": "object", "additionalProperties": True}
    if description:
        out["description"] = description
    return out


def _arr(items, default=None, description=""):
    out = {"type": "array", "items": items}
    if default is not None:
        out["default"] = default
    if description:
        out["description"] = description
    return out


def _str(description="", default=None, enum=None, min_length=None, max_length=None):
    out = {"type": "string"}
    if description:
        out["description"] = description
    if default is not None:
        out["default"] = default
    if enum:
        out["enum"] = list(enum)
    if min_length is not None:
        out["minLength"] = min_length
    if max_length is not None:
        out["maxLength"] = max_length
    return out


def _int(description="", default=None, minimum=None, maximum=None):
    out = {"type": "integer"}
    if description:
        out["description"] = description
    if default is not None:
        out["default"] = default
    if minimum is not None:
        out["minimum"] = minimum
    if maximum is not None:
        out["maximum"] = maximum
    return out


def _bool(description="", default=None):
    out = {"type": "boolean"}
    if description:
        out["description"] = description
    if default is not None:
        out["default"] = default
    return out


def _num(description="", default=None, minimum=None, maximum=None):
    out = {"type": "number"}
    if description:
        out["description"] = description
    if default is not None:
        out["default"] = default
    if minimum is not None:
        out["minimum"] = minimum
    if maximum is not None:
        out["maximum"] = maximum
    return out


FIELDS = _arr(_str(), description="Dot-path fields to keep, e.g. ['id','info.routed_dir'].")
PATHS = _arr(_str(), description="Absolute model file paths.")
IDS = _arr(_int(), description="Positive integer ids.")
TAG_IDS = _arr(_int(), description="Positive integer tag ids.")


def _tool(name, title, description, toolset, handler, *, annotations,
          input_schema, returns="text"):
    return {
        "name": name, "title": title, "description": description,
        "toolset": toolset, "handler": handler,
        "annotations": dict(annotations),
        "inputSchema": input_schema or _obj(),
        "returns": returns,
    }


_READ = {"readOnlyHint": True, "destructiveHint": False,
         "idempotentHint": True, "openWorldHint": False}
_READ_NET = {"readOnlyHint": True, "destructiveHint": False,
             "idempotentHint": True, "openWorldHint": True}
_WRITE = {"readOnlyHint": False, "destructiveHint": False,
          "idempotentHint": False, "openWorldHint": False}
_WRITE_NET = {"readOnlyHint": False, "destructiveHint": False,
              "idempotentHint": False, "openWorldHint": True}
_WRITE_IDEM = {"readOnlyHint": False, "destructiveHint": False,
               "idempotentHint": True, "openWorldHint": False}
_DESTRUCTIVE = {"readOnlyHint": False, "destructiveHint": True,
                "idempotentHint": False, "openWorldHint": False}


def _build_tools():
    out = {}

    def add(t):
        out[t["name"]] = t

    add(_tool(
        "cft_capabilities", "Capabilities",
        "Return app/MCP versions, bridge readiness, enabled toolsets, limits and guide URIs. "
        "Call this before planning a multi-step workflow.",
        "core", "capabilities", annotations=_READ,
        input_schema=_obj({
            "include_tools": _bool("Include the full tool catalog.", False),
        })))

    add(_tool(
        "cft_get_state", "Get state",
        "Return compact app state: config, recent tasks, pending file choice, UI state and blockers. "
        "Use compact=true for text-only models. Check blockers before writing.",
        "core", "state", annotations=_READ,
        input_schema=_obj({
            "compact": _bool("Omit full config and long task lists.", True),
            "task_limit": _int("Recent task count.", 5, 0, 50),
            "fields": FIELDS,
        })))

    add(_tool(
        "cft_search_models", "Search Civitai models",
        "Search Civitai's public model catalog. Returns model ids, types, stats and versions. "
        "Use this before resolve/download when the user gives a name instead of a URL.",
        "core", "search", annotations=_READ_NET,
        input_schema=_obj({
            "query": _str("Search text.", min_length=1),
            "types": _str("Comma-separated types, e.g. LORA,Checkpoint."),
            "base_models": _str("Comma-separated base models."),
            "sort": _str("Sort order.", enum=["Highest Rated", "Most Downloaded", "Newest"]),
            "period": _str("Time period.", enum=["Day", "Week", "Month", "Year", "AllTime"]),
            "nsfw": _bool("Include NSFW results.", False),
            "limit": _int("Result count.", 10, 1, 20),
        }, required=["query"])))

    add(_tool(
        "cft_resolve_model", "Resolve model",
        "Resolve a Civitai URL or model/version id into model metadata, all versions, files, "
        "routing destinations, existing-file state and queue state. Each file carries a read-only "
        "placement verdict (routed / under_root / outside_root) plus a fix hint. "
        "Read-only: does not download.",
        "core", "resolve", annotations=_READ_NET,
        input_schema=_obj({
            "url": _str("Civitai model URL."),
            "model_id": _int("Civitai model id.", minimum=1),
            "version_id": _int("Civitai model version id.", minimum=1),
            "file_id": _int("Only inspect one file id.", minimum=1),
            "probe": _bool("Probe safetensors headers (two Range requests).", False),
        }, description="Provide url, or model_id (optionally with version_id).")))

    add(_tool(
        "cft_download_model", "Download model",
        "Resolve, plan, submit and optionally wait for a Civitai download. By default waits for "
        "completion, runs postprocess, and verifies the destination. Returns placement "
        "(dest_dir / routed_dir / status / hint) so the caller can confirm the file landed in a "
        "model type directory. Existing files return skipped=exists instead of re-downloading; "
        "set force=true only when the user asks.",
        "core", "download", annotations=_WRITE_NET,
        input_schema=_obj({
            "url": _str("Civitai model URL."),
            "urls": _arr(_str(), description="Batch of Civitai URLs."),
            "model_id": _int("Civitai model id.", minimum=1),
            "version_id": _int("Civitai model version id.", minimum=1),
            "file_ids": _arr(_int(), description="Download only these file ids from one version."),
            "dest_subdir": _str("Optional subdirectory under the configured destination."),
            "caps": _arr(_str(), description="Capabilities; default ['bundle'] enables type routing."),
            "wait": _bool("Block until terminal state.", True),
            "timeout_s": _int("Wait budget in seconds.", 300, 1, 1800),
            "verify": _bool("Verify file and sidecars after completion.", True),
            "force": _bool("Re-download even if the target file already exists.", False),
        }, description="Provide url, urls, or model_id (optionally with version_id/file_ids).")))

    add(_tool(
        "cft_list_tasks", "List or wait for tasks",
        "List download/scan tasks. With wait=true and ids, block until those tasks are terminal. "
        "timed_out=true is not a failure; poll again or inspect the task. "
        "MCP text results are size-capped, so rows default to a compact projection "
        "(status, placement, expected/actual sha256, verification_status); pass fields=[\"*\"] "
        "for every field or fields=[\"id\",\"status\"] to choose.",
        "core", "tasks_list", annotations=_READ,
        input_schema=_obj({
            "ids": _arr(_str(), description="Task ids."),
            "status": _arr(_str(enum=["pending", "downloading", "paused", "done", "error", "canceled"])),
            "limit": _int("Maximum rows.", 50, 1, 500),
            "wait": _bool("Block until terminal/queue empty.", False),
            "timeout_s": _int("Wait budget in seconds.", 300, 1, 1800),
            "since": _int("Only tasks updated since this epoch ms.", minimum=0),
            "fields": FIELDS,
        })))

    add(_tool(
        "cft_control_tasks", "Control tasks",
        "Pause, resume, retry, start, remove or archive tasks. Removing/archiving tasks is "
        "destructive for queue state but never deletes model files.",
        "core", "tasks_control", annotations=_DESTRUCTIVE,
        input_schema=_obj({
            "action": _str("Action.", enum=["pause", "resume", "retry", "start", "remove", "archive"]),
            "ids": _arr(_str(), description="Task ids."),
            "all": _bool("Apply pause/resume/start/remove to all tasks.", False),
        }, required=["action"])))

    add(_tool(
        "cft_answer_file_choice", "Answer file choice",
        "Answer a pending multi-file download selection. Get identity from cft_get_state "
        "(pending_file_choice.id). indices refer to files[].index. ack=true marks the prompt as seen.",
        "core", "file_choice", annotations=_WRITE,
        input_schema=_obj({
            "identity": _str("Pending choice identity.", min_length=1),
            "indices": _arr(_int(), description="Selected file indices."),
            "pause": _bool("Keep the task paused after answering.", False),
            "ack": _bool("Only acknowledge that the prompt was shown.", False),
        }, required=["identity"])))

    add(_tool(
        "cft_dismiss_ui", "Dismiss UI popups",
        "Close any popup blocking the UI: onboarding, API-key prompt, tutorial, confirmations, "
        "download file choice, post-download move, unsaved-settings, result/info dialogs, detail "
        "drawer and image viewer. Closing a confirmation means CANCEL, never confirm; closing a "
        "file choice cancels that download. Humans need guides; AI does not. permanent=true "
        "(default, affects guides only) also stops future automatic guides.",
        "core", "dismiss_ui", annotations=_WRITE_IDEM,
        input_schema=_obj({
            "targets": _arr(_str(enum=[
                "all", "guides", "dialogs", "onboarding", "api_key", "reverse_tutorial",
                "confirm", "file_choice", "move_after_download", "settings_unsaved",
                "restricted", "info", "result", "detail", "about", "image_viewer",
            ]), description="Popup groups/names to close; omit for all."),
            "permanent": _bool("Persist onboarding_done and stop automatic guides.", True),
        })))

    add(_tool(
        "cft_verify_model", "Verify model",
        "Verify a downloaded model file, optional SHA256, sidecars, preview and image directory. "
        "ok means the model file itself is usable; bundle_ok means configured sidecars are complete. "
        "With task_id the download-time verdict is passed through (expected_sha256 / actual_sha256 / "
        "verification_status), so an unchecked hash is never mistaken for a checked one; "
        "hash=true recomputes it.",
        "core", "verify", annotations=_READ,
        input_schema=_obj({
            "task_id": _str("Download task id."),
            "path": _str("Absolute model file or directory path."),
            "hash": _bool("Recompute SHA256.", False),
        })))

    add(_tool(
        "cft_repair_model", "Repair model postprocess",
        "Re-run postprocess stages for a completed download (metadata/comments/cover/video/old cleanup). "
        "Idempotent; returns postprocess_errors when finished.",
        "core", "repair", annotations=_WRITE_IDEM,
        input_schema=_obj({
            "task_id": _str("Completed task id.", min_length=1),
            "stages": _arr(_str(enum=["metadata", "comments", "cover", "video", "replace_old"])),
            "wait": _bool("Wait until repair finishes.", True),
            "timeout_s": _int("Wait budget in seconds.", 180, 1, 1800),
        }, required=["task_id"])))

    add(_tool(
        "cft_list_models", "List local models",
        "List scanned local model rows with path/name/type/version/model ids. "
        "Use fields to keep text-only payloads small.",
        "core", "library_list", annotations=_READ,
        input_schema=_obj({
            "q": _str("Search text."),
            "type": _str("Model type filter."),
            "limit": _int("Rows.", 50, 1, 500),
            "offset": _int("Offset.", 0, 0, 1000000),
            "fields": FIELDS,
        })))

    add(_tool(
        "cft_manage_library", "Manage model library",
        "Run a model-library maintenance operation. Destructive ops (dedupe-delete, cleanup) require "
        "confirm=true and explicit paths; run the read-only preview first.",
        "core", "library_op", annotations=_DESTRUCTIVE,
        input_schema=_obj({
            "op": _str("Operation.", enum=[
                "verify", "dedupe", "dedupe-delete", "localize", "gen-json",
                "download-covers", "check-updates", "update", "sync-local",
                "organize", "rename-c", "rename-preview", "translate-descs", "cleanup"]),
            "paths": PATHS,
            "wait": _bool("Wait for completion.", True),
            "timeout_s": _int("Wait budget in seconds.", 300, 1, 1800),
            "overwrite": _bool("Overwrite existing generated files.", False),
            "force": _bool("Bypass caches (check-updates).", False),
            "confirm": _bool("Explicit confirmation for destructive ops.", False),
        }, required=["op"])))

    add(_tool(
        "cft_browse_gallery", "Browse output gallery",
        "Query the ComfyUI output media library. Returns metadata rows (no image bytes). "
        "Use cft_view_gallery for images and cft_annotate_gallery to star/tag.",
        "core", "gallery_list", annotations=_READ,
        input_schema=_obj({
            "kind": _str("Media kind.", enum=["image", "video"]),
            "starred": _bool("Only starred items."),
            "q": _str("Full-text search."),
            "sort": _str("Sort order.", enum=["time_desc", "time_asc", "name_asc", "name_desc"]),
            "dirs": _arr(_str(), description="Directory filters."),
            "tag_ids": TAG_IDS,
            "checkpoints": _arr(_str(), description="Checkpoint filters."),
            "loras": _arr(_str(), description="LoRA filters."),
            "limit": _int("Rows.", 50, 1, 500),
            "offset": _int("Offset.", 0, 0, 1000000),
            "with_facets": _bool("Include facets.", False),
            "fields": FIELDS,
        })))

    add(_tool(
        "cft_annotate_gallery", "Star or tag gallery items",
        "Star/unstar items, add/remove tags, or save/delete a tag definition. "
        "Does not delete media; use cft_delete_gallery (admin) for that.",
        "core", "gallery_op", annotations=_WRITE,
        input_schema=_obj({
            "op": _str("Operation.", enum=["star", "tag-add", "tag-remove", "tag-save", "tag-delete"]),
            "ids": IDS,
            "on": _bool("Star on/off.", True),
            "tag_ids": TAG_IDS,
            "id": _int("Tag id for tag-save/tag-delete (0 creates).", 0, 0, 2 ** 53 - 1),
            "name": _str("Tag name for tag-save."),
            "color": _str("Tag color, e.g. #888888."),
            "parent_id": _int("Parent tag id; -1 means no parent.", -1),
        }, required=["op"])))

    add(_tool(
        "cft_analyze_workflow", "Analyze workflow or generated image",
        "Parse a ComfyUI workflow (.json or embedded PNG/WebP) or Forge/A1111 parameters. "
        "Returns prompts, models, generation params and local model matches. "
        "Set include_image=true to also return a capped JPEG preview.",
        "core", "workflow", annotations=_READ,
        input_schema=_obj({
            "path": _str("Absolute workflow/image path.", min_length=1),
            "full": _bool("Include edges and full node table.", False),
            "include_image": _bool("Include a resized preview image.", False),
            "max_side": _int("Preview longest side.", 1024, 256, 1536),
        }, required=["path"])))

    add(_tool(
        "cft_diagnostics", "Diagnostics",
        "Read error events, automation call events, connectivity, storage and browser-extension status. "
        "storage also lists ComfyUI models-root candidates (suggested_root) when routing is off. "
        "Use when a download fails, lands outside a model root, or the app looks unreachable.",
        "core", "diagnostics", annotations=_READ,
        input_schema=_obj({
            "source": _str("Source.", enum=["logs", "events", "connectivity", "storage", "extension", "all"]),
            "tail": _int("Log tail lines.", 80, 1, 500),
        })))

    add(_tool(
        "cft_browser_tabs", "Search open browser tabs",
        "Search Civitai model pages open in the connected browser extension. Returns title, "
        "sanitized URL, model/version ids, copy count and freshness. Read-only: only model-page "
        "metadata, never page content, cookies or API keys. Use cft_download_model on a hit.",
        "core", "browser_tabs", annotations=_READ,
        input_schema=_obj({
            "query": _str("Case-insensitive text match across title, URL, model id and version id."),
            "limit": _int("Max rows.", 50, 1, 200),
            "include_discarded": _bool("Include sleeping/discarded tabs.", False),
            "window_id": _int("Only tabs from this browser window.", minimum=0, maximum=1000000000),
            "active": _bool("Only the active tab.", False),
        })))

    add(_tool(
        "cft_describe_model", "Describe model (text only)",
        "Return a text summary of a model: name/type/base model/trigger words, cover/sample image paths, "
        "prompts and missing-sidecar notes. Never returns image bytes; use cft_view_model for vision.",
        "core", "describe_model", annotations=_READ,
        input_schema=_obj({
            "path": _str("Absolute model file path.", min_length=1),
            "limit_images": _int("Sample image summaries.", 4, 0, 20),
            "include_prompts": _bool("Include prompt/negative text.", True),
            "fields": FIELDS,
        }, required=["path"])))

    add(_tool(
        "cft_view_model", "View model images",
        "Return model cover/sample images as MCP image content (or paths) plus prompts, generation params "
        "and dimensions. Videos return a JPEG first frame, never mp4 bytes.",
        "vision", "view_model", annotations=_READ, returns="mixed",
        input_schema=_obj({
            "path": _str("Absolute model file path.", min_length=1),
            "limit_images": _int("Images to return.", 4, 1, 8),
            "max_side": _int("Longest side in pixels.", 768, 256, 1536),
            "delivery": _str("inline returns image content; paths returns local paths only.",
                             enum=["inline", "paths"], default="inline"),
            "include_prompts": _bool("Attach prompts/params to each image.", True),
            "safe": _bool("Skip entries marked NSFW when the flag is known.", False),
        }, required=["path"])))

    add(_tool(
        "cft_view_gallery", "View gallery items",
        "Return selected gallery items as MCP image content (or paths) plus generation metadata, "
        "tags and starred state. Use cft_contact_sheet for more than 4 items.",
        "vision", "view_gallery", annotations=_READ, returns="mixed",
        input_schema=_obj({
            "ids": _arr(_int(), description="Gallery item ids (1-8)."),
            "max_side": _int("Longest side in pixels.", 800, 256, 1536),
            "delivery": _str("inline or paths.", enum=["inline", "paths"], default="inline"),
            "include_meta": _bool("Include prompt/model/tags metadata.", True),
        }, required=["ids"])))

    add(_tool(
        "cft_view_workflow", "View workflow preview",
        "Return a workflow/image preview as MCP image content (or path) together with parsed prompts and "
        "model references.",
        "vision", "view_workflow", annotations=_READ, returns="mixed",
        input_schema=_obj({
            "path": _str("Absolute workflow/image path.", min_length=1),
            "max_side": _int("Longest side in pixels.", 1024, 256, 1536),
            "delivery": _str("inline or paths.", enum=["inline", "paths"], default="inline"),
        }, required=["path"])))

    add(_tool(
        "cft_contact_sheet", "Build contact sheet",
        "Compose up to 24 gallery items or model sample images into one labeled JPEG grid plus an "
        "index-to-id/path mapping. Use it to triage many images cheaply, then call cft_view_gallery "
        "for the selected ids.",
        "vision", "contact_sheet", annotations=_READ, returns="mixed",
        input_schema=_obj({
            "source": _str("Source.", enum=["gallery", "model"]),
            "ids": _arr(_int(), description="Gallery ids (gallery source)."),
            "path": _str("Model file path (model source)."),
            "limit": _int("Maximum cells.", 16, 1, 24),
            "cols": _int("Grid columns.", 4, 1, 6),
            "cell": _int("Cell size in pixels.", 256, 96, 512),
            "delivery": _str("inline or paths.", enum=["inline", "paths"], default="inline"),
        }, required=["source"])))

    add(_tool(
        "cft_inspect_image", "Inspect image bytes",
        "Inspect a local image path or base64 image upload: dimensions, format, embedded prompts, generation "
        "params and model references, with an optional preview image. Read-only and in-memory.",
        "vision", "inspect_image", annotations=_READ, returns="mixed",
        input_schema=_obj({
            "path": _str("Absolute image path."),
            "data_b64": _str("Base64 image bytes; do not prefix with data:. Max 8MB."),
            "filename": _str("Original filename for format hints."),
            "max_side": _int("Preview longest side.", 768, 256, 1536),
            "delivery": _str("inline or paths.", enum=["inline", "paths"], default="inline"),
            "include_image": _bool("Include a preview image.", True),
        })))

    add(_tool(
        "cft_manage_files", "Manage files",
        "Move a model/task/history destination, rename a model file with sidecars, or browse a directory. "
        "Destinations must already exist; preview rename with preview=true.",
        "admin", "manage_files", annotations=_WRITE,
        input_schema=_obj({
            "action": _str("Action.", enum=["move", "rename", "list"]),
            "path": _str("Model file path."),
            "dest_dir": _str("Existing absolute destination directory."),
            "new_name": _str("New file name without changing sidecars manually."),
            "paths": PATHS,
            "preview": _bool("Only compute new names.", False),
            "task_id": _str("Download task id for move."),
            "history_id": _str("History id for move."),
            "dirs_only": _bool("Directory listing only (default true).", True),
        }, required=["action"])))

    add(_tool(
        "cft_configure", "Read or write config",
        "Read the full config (secrets redacted) or write whitelisted keys. Writes are atomic per call; "
        "unknown keys reject the whole patch.",
        "admin", "configure", annotations=_WRITE,
        input_schema=_obj({
            "action": _str("Action.", enum=["get", "set"]),
            "config": _obj(description="Whitelisted config patch for action=set."),
        }, required=["action"])))

    add(_tool(
        "cft_history", "Download history",
        "List, remove or clear download history records. This never deletes model files; remove/clear also "
        "drops terminal queue rows and stale .part files.",
        "admin", "history", annotations=_WRITE,
        input_schema=_obj({
            "action": _str("Action.", enum=["list", "remove", "clear"]),
            "ids": _arr(_str(), description="History ids."),
            "limit": _int("Rows.", 50, 1, 500),
            "status": _str("Status filter."),
            "detail": _bool("Return raw history rows.", False),
            "preview": _bool("Only report what would be removed.", False),
        }, required=["action"])))

    add(_tool(
        "cft_migrate_storage", "Migrate data storage",
        "Move the app data directory (config/tasks/history/cache). Requires confirm=true. "
        "Models are not moved. Refused while downloads/scans are running.",
        "admin", "migrate_storage", annotations=_DESTRUCTIVE,
        input_schema=_obj({
            "directory": _str("Existing absolute target directory.", min_length=1),
            "replace_existing": _bool("Allow overwriting target data.", False),
            "confirm": _bool("Explicit confirmation.", False),
        }, required=["directory"])))

    add(_tool(
        "cft_delete_gallery", "Delete gallery media",
        "Move gallery media files to the system recycle bin. Requires confirm=true. "
        "Sidecar files are never deleted with the media.",
        "admin", "delete_gallery", annotations=_DESTRUCTIVE,
        input_schema=_obj({
            "ids": IDS,
            "confirm": _bool("Explicit confirmation.", False),
        }, required=["ids"])))

    return out


TOOLS = _build_tools()

TOOLSETS = {
    "core": [n for n, t in TOOLS.items() if t["toolset"] == "core"],
    "vision": [n for n, t in TOOLS.items() if t["toolset"] == "vision"],
    "admin": [n for n, t in TOOLS.items() if t["toolset"] == "admin"],
}

PROFILES = {
    "text": ["core"],
    "vision": ["core", "vision"],
    "admin": ["core", "admin"],
    "all": ["core", "vision", "admin"],
}


def tools_for_profile(profile="text"):
    """Return tools enabled by a profile, preserving registry order."""
    sets = PROFILES.get(str(profile or "text").strip().lower())
    if sets is None:
        sets = PROFILES["text"]
    wanted = []
    for name in TOOLSETS.keys():
        if name in sets:
            wanted.extend(TOOLSETS[name])
    return [TOOLS[n] for n in wanted]


def tools_for_toolsets(names):
    wanted = []
    for name in ("core", "vision", "admin"):
        if name in (names or []):
            wanted.extend(TOOLSETS[name])
    return [TOOLS[n] for n in dict.fromkeys(wanted)]


# SSE 领域事件目录：同样属于 AI 契约，避免文档、推送实现、测试各写一份。
EVENTS = [
    {"type": "stream.ready", "group": "stream",
     "description": "连接已建立；携带实例 id、当前 seq 与心跳间隔。",
     "data": _obj({"instance_id": _str(), "last_seq": _int(), "oldest_seq": _int(),
                   "heartbeat_s": _num(), "types": _arr(_str())})},
    {"type": "stream.reset", "group": "stream",
     "description": "请求的 since/Last-Event-ID 早于环形缓冲，后续事件不再连续。",
     "data": _obj({"instance_id": _str(), "oldest_seq": _int(), "last_seq": _int()})},
    {"type": "stream.overflow", "group": "stream",
     "description": "该客户端队列曾满，已有事件被丢弃；dropped 是累计丢失数。",
     "data": _obj({"dropped": _int()})},
    {"type": "state.snapshot", "group": "stream",
     "description": "新连接的首个紧凑状态快照；重连重放时默认不发。",
     "data": _any_obj()},
    {"type": "task.created", "group": "task",
     "description": "出现新的下载任务。",
     "data": _obj({"id": _str(), "filename": _str(), "status": _str(), "dest_dir": _str()})},
    {"type": "task.updated", "group": "task",
     "description": "任务状态或进度变化；每个任务最多约 1 秒一条。",
     "data": _any_obj()},
    {"type": "task.finished", "group": "task",
     "description": "任务进入 done/error/canceled 终态。",
     "data": _any_obj()},
    {"type": "task.removed", "group": "task",
     "description": "任务从队列中消失（移除或清理）。",
     "data": _obj({"id": _str()})},
    {"type": "file_choice.pending", "group": "file_choice",
     "description": "下载文件选择弹窗出现，等待输入。",
     "data": _any_obj()},
    {"type": "file_choice.resolved", "group": "file_choice",
     "description": "待选文件已处理或取消。",
     "data": _any_obj()},
    {"type": "model_scan.started", "group": "scan",
     "description": "模型库扫描开始。",
     "data": _any_obj()},
    {"type": "model_scan.progress", "group": "scan",
     "description": "模型库扫描状态变化（最多约 1 秒一条）。",
     "data": _any_obj()},
    {"type": "model_scan.finished", "group": "scan",
     "description": "模型库扫描结束（成功或失败）。",
     "data": _any_obj()},
    {"type": "library.scan.started", "group": "scan",
     "description": "资源库扫描开始。",
     "data": _any_obj()},
    {"type": "library.scan.progress", "group": "scan",
     "description": "资源库扫描进度（最多约 1 秒一条）。",
     "data": _any_obj()},
    {"type": "library.scan.finished", "group": "scan",
     "description": "资源库扫描结束（成功或失败）。",
     "data": _any_obj()},
    {"type": "ui.overlays.changed", "group": "ui",
     "description": "可见弹层集合变化；AI 可据此调用 cft_dismiss_ui。",
     "data": _obj({"overlays": _arr(_any_obj())})},
    {"type": "ui.dismissed", "group": "ui",
     "description": "弹层关闭命令已生效。",
     "data": _obj({"rev": _int(), "targets": _arr(_str())})},
    {"type": "config.changed", "group": "config",
     "description": "配置版本变化；keys 只给键名，不给值。",
     "data": _obj({"rev": _int(), "keys": _arr(_str())})},
    {"type": "api.call", "group": "api",
     "description": "本层调用事件；仅 types=all 订阅，且不参与历史重放。",
     "data": _any_obj()},
    {"type": "app.stopping", "group": "app",
     "description": "正常退出时尽力发送；进程被强杀时可能缺席，客户端应以连接关闭为准。",
     "data": _obj({"pid": _int()})},
]

EVENT_TYPES = {item["type"]: item for item in EVENTS}

DOMAIN_EVENT_TYPES = tuple(item["type"] for item in EVENTS if item["group"] != "api")


RESOURCES = [
    {"uri": "cft://guide/text", "name": "CivitaiFreeTool text guide",
     "description": "Compact text-model runbook: discovery, core loop, errors, token discipline.",
     "mimeType": "text/markdown", "kind": "guide", "profile": "text"},
    {"uri": "cft://guide/vision", "name": "CivitaiFreeTool vision guide",
     "description": "Vision-model runbook: image delivery, contact sheets, metadata, safety.",
     "mimeType": "text/markdown", "kind": "guide", "profile": "vision"},
    {"uri": "cft://capabilities", "name": "CivitaiFreeTool capabilities",
     "description": "Live app/MCP capabilities, enabled toolsets, limits and blockers.",
     "mimeType": "application/json", "kind": "capabilities"},
    {"uri": "cft://tools", "name": "CivitaiFreeTool tool catalog",
     "description": "Full tool catalog with JSON Schemas and safety annotations.",
     "mimeType": "application/json", "kind": "tools"},
    {"uri": "cft://schema", "name": "CivitaiFreeTool app schema",
     "description": "Config key types, enums, task states and routing map.",
     "mimeType": "application/json", "kind": "schema"},
    {"uri": "cft://state", "name": "CivitaiFreeTool compact state",
     "description": "Live compact state with tasks, pending choice and blockers.",
     "mimeType": "application/json", "kind": "state"},
]


PROMPTS = [
    {
        "name": "cft_download",
        "title": "Download a Civitai model",
        "description": "Resolve/plan/download/verify a Civitai model from a URL.",
        "arguments": [{"name": "url", "description": "Civitai model URL", "required": True},
                      {"name": "dest_subdir", "description": "Optional destination subdirectory",
                       "required": False}],
        "template": "Download this Civitai model: {url}\n"
                    "First call cft_resolve_model to inspect versions/files and whether it already exists. "
                    "If it exists, verify it instead of downloading. Otherwise call cft_download_model with "
                    "wait=true and verify=true. Destination subdirectory: {dest_subdir}.",
    },
    {
        "name": "cft_find_and_download",
        "title": "Find and download a model",
        "description": "Search by name, choose a version, then download it.",
        "arguments": [{"name": "query", "description": "Model name or keywords", "required": True},
                      {"name": "type", "description": "Optional Civitai type", "required": False}],
        "template": "Find a Civitai model matching '{query}' (type: {type}). "
                    "Use cft_search_models, show the top candidates with id/type/base model, "
                    "then use cft_resolve_model and cft_download_model for the chosen version.",
    },
    {
        "name": "cft_repair_download",
        "title": "Diagnose and repair a download",
        "description": "Inspect a failed/incomplete task, then repair its postprocess stages.",
        "arguments": [{"name": "task_id", "description": "Download task id", "required": True}],
        "template": "Diagnose task {task_id}: call cft_list_tasks with ids=[task_id], then "
                    "cft_verify_model with task_id. If the model file is usable but sidecars are missing, "
                    "call cft_repair_model with the missing stages. Never delete model files.",
    },
    {
        "name": "cft_audit_library",
        "title": "Audit the local model library",
        "description": "Check metadata, duplicates and updates, then propose safe actions.",
        "arguments": [],
        "template": "Audit the local model library. Use cft_list_models with a small limit first, "
                    "then cft_manage_library with read-only ops (verify, dedupe, check-updates). "
                    "Never run dedupe-delete or cleanup without explicit user confirmation and a preview.",
    },
    {
        "name": "cft_curate_gallery",
        "title": "Curate ComfyUI output images",
        "description": "Review output images and star/tag selected items.",
        "arguments": [{"name": "filter", "description": "Optional text description of what to curate",
                       "required": False}],
        "template": "Curate the ComfyUI output gallery. Search with cft_browse_gallery, "
                    "use cft_contact_sheet for a quick overview, then cft_view_gallery for selected ids. "
                    "Suggest a shortlist and ask before applying tags/stars with cft_annotate_gallery. "
                    "User intent: {filter}.",
    },
]

ERROR_CODES = {
    "invalid_argument": {"status": 400, "retryable": False},
    "not_found": {"status": 404, "retryable": False},
    "unsupported": {"status": 501, "retryable": False},
    "forbidden": {"status": 403, "retryable": False},
    "conflict": {"status": 409, "retryable": True},
    "busy": {"status": 409, "retryable": True},
    "rate_limited": {"status": 429, "retryable": True},
    "timeout": {"status": 408, "retryable": True},
    "app_not_running": {"status": 503, "retryable": True},
    "version_mismatch": {"status": 409, "retryable": False},
    "destructive_requires_confirm": {"status": 400, "retryable": False},
    "permission_denied": {"status": 403, "retryable": False},
    "path_outside_roots": {"status": 403, "retryable": False},
    "verification_failed": {"status": 200, "retryable": False},
    "task_failed": {"status": 200, "retryable": False},
    "network_error": {"status": 502, "retryable": True},
    "webhook_failed": {"status": 502, "retryable": False},
    "invalid_media": {"status": 400, "retryable": False},
    "internal_error": {"status": 500, "retryable": True},
}


def error_code_for_status(status, payload=None):
    payload = payload if isinstance(payload, dict) else {}
    code = str(payload.get("code") or "").strip()
    if code in ERROR_CODES:
        return code
    if status == 400:
        return "invalid_argument"
    if status == 401:
        return "forbidden"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 408:
        return "timeout"
    if status == 409:
        return "conflict"
    if status == 429:
        return "rate_limited"
    if status == 501:
        return "unsupported"
    if status == 503:
        return "app_not_running"
    if status >= 500:
        return "internal_error"
    return "invalid_argument"


def normalize_error(status, payload, hint=None, next_actions=None):
    """Add stable code/retryable/hint/next_actions to every error envelope."""
    out = dict(payload) if isinstance(payload, dict) else {"msg": str(payload)}
    out["ok"] = False
    code = error_code_for_status(status, out)
    out["code"] = code
    spec = ERROR_CODES.get(code) or {}
    out.setdefault("retryable", bool(spec.get("retryable", status >= 500)))
    if hint and not out.get("hint"):
        out["hint"] = hint
    if next_actions and not out.get("next_actions"):
        out["next_actions"] = next_actions
    return out


TEXT_GUIDE = """# CivitaiFreeTool · text model guide

You control a local CivitaiFreeTool desktop app. It downloads Civitai/HuggingFace models,
routes them into ComfyUI folders, manages the local model library, and indexes ComfyUI output media.

## Core loop
1. `cft_capabilities` — verify the app and enabled toolsets.
2. `cft_get_state` — read blockers and recent tasks. If any popup blocks the UI, call
   `cft_dismiss_ui` once; AI sessions do not need the human onboarding. Confirmation dialogs
   are canceled (not confirmed), and a file-choice popup is canceled (download not started).
3. `cft_search_models` / `cft_resolve_model` — find the model and inspect versions/files.
4. `cft_download_model` — default waits, postprocesses and verifies; existing files return `skipped=exists`.
5. `cft_list_tasks` / `cft_verify_model` — poll or verify after a timeout.
6. `cft_repair_model` — fix missing metadata/cover/comments instead of re-downloading.

## Rules
- Use `next_actions` when present; they are executable tool calls, not prose.
- Read `code` and `retryable` on errors. Do not retry `invalid_argument`, `not_found`,
  `unsupported`, `version_mismatch` or `destructive_requires_confirm`.
- Never call a destructive tool without explicit user confirmation and a preview/dry-run.
  Destructive tools: `cft_manage_library` (dedupe-delete/cleanup), `cft_control_tasks` (remove),
  `cft_migrate_storage`, `cft_delete_gallery` (admin).
- `cft_download_model` returns `skipped=exists` instead of re-downloading; use `force=true` only
  when the user explicitly asks to replace the file.
- Do not request inline images with text-only models. Use `cft_describe_model`,
  `cft_browse_gallery` and `cft_analyze_workflow` for metadata instead.
- Keep payloads small: use `limit`, `fields`, `compact=true`, and avoid `full=true`.

## Recipes
- URL download: resolve → check `exists` → download(wait=true, verify=true) → verify.
- Search and choose: search → show candidates → resolve chosen version → download.
- Failed download: list task → verify → repair missing stages → verify again.
- Library audit: list models → verify/dedupe/check-updates (read-only) → propose actions.
- Gallery metadata: browse gallery → inspect prompts → annotate only after user approval.
"""


VISION_GUIDE = """# CivitaiFreeTool · vision model guide

You can inspect local model covers, sample images, ComfyUI workflow previews, and output gallery items.
The MCP server returns native image content blocks plus text metadata. Use images only when they help
the task; metadata-only calls are cheaper. If a human onboarding/tutorial overlay blocks the UI,
call `cft_dismiss_ui` once instead of trying to click through it.

## Image tools
- `cft_view_model` — cover/sample images for a model file, with prompt and generation params.
- `cft_view_gallery` — selected output gallery items (ids from `cft_browse_gallery`).
- `cft_view_workflow` — workflow or generated-image preview with parsed prompts/models.
- `cft_contact_sheet` — one labeled grid for quick triage of up to 24 items; then request selected ids.
- `cft_inspect_image` — local path or base64 upload; returns metadata plus optional preview.

## Delivery
- `delivery="inline"` returns image content (use this for vision).
- `delivery="paths"` returns local paths only (use when the host has its own file reader).
- `max_side` default 768–1024; raise only when detail matters. `limit_images` max 8.
- Videos are always returned as a JPEG first frame, never as mp4 bytes.

## Rules
- Start broad with a contact sheet; request full-size images only for selected ids.
- Attach metadata (prompt, sampler, seed, model refs) to your reasoning; do not guess unseen details.
- Ask before applying tags/stars; use `cft_annotate_gallery` only after user approval.
- NSFW flags may be unknown. Do not assume an image is safe from the filename alone.
- If an image or metadata is missing, say so and offer `cft_repair_model`, not a fabricated description.
"""


def guide_for(profile="text"):
    return VISION_GUIDE if str(profile or "").strip().lower() == "vision" else TEXT_GUIDE


def public_tool(tool, *, format="mcp"):
    """Project a registry tool into a host-facing shape."""
    base = {
        "name": tool["name"],
        "title": tool["title"],
        "description": tool["description"],
        "inputSchema": tool["inputSchema"],
        "annotations": tool["annotations"],
        "x-cft-toolset": tool["toolset"],
        "x-cft-returns": tool["returns"],
    }
    if format == "anthropic":
        return {"name": base["name"], "description": base["description"],
                "input_schema": base["inputSchema"]}
    if format == "openai":
        return {"type": "function", "function": {
            "name": base["name"], "description": base["description"],
            "parameters": base["inputSchema"]}}
    return base
