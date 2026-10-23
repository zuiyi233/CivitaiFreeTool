# CivitaiFreeTool tool reference

<!-- GENERATED FILE. Do not edit by hand. -->
<!-- Source: ai_contract.py; regenerate with: python scripts/gen_skill_refs.py -->

This is the catalog shared by the MCP sidecar and the HTTP automation API. Tool names and argument names are identical on both transports.

- Contract API: `1.0`
- MCP server: `civitai-free-tool` `1.0.0`
- Minimum app version: `2.7.0`
- Tools: **29** (core 19, vision 5, admin 5)
- Toolsets are opt-in: `core` is the default; multimodal hosts add `vision`; `admin` is explicit only.
- Profiles: text=core, vision=core+vision, admin=core+admin, all=core+vision+admin.
- `additionalProperties: false` on every schema: unknown arguments are rejected.

## Safety labels

- `read` / `write`: `readOnlyHint` true/false.
- `destructive`: `destructiveHint` true. Requires explicit user confirmation and, where supported, a preview/dry-run.
- `idem`: `idempotentHint` true.
- `net` / `local`: `openWorldHint` true/false (network access expected).
- `returns`: `text` = text only; `mixed` = text plus optional MCP image content (`delivery=inline`). `delivery=paths` returns local paths only, never inline bytes.

## Quick index

| Tool | Toolset | Safety | Returns |
| --- | --- | --- | --- |
| `cft_capabilities` | core | read · idem · local | text |
| `cft_get_state` | core | read · idem · local | text |
| `cft_search_models` | core | read · idem · net | text |
| `cft_resolve_model` | core | read · idem · net | text |
| `cft_download_model` | core | write · net | text |
| `cft_list_tasks` | core | read · idem · local | text |
| `cft_control_tasks` | core | write · destructive · local | text |
| `cft_answer_file_choice` | core | write · local | text |
| `cft_dismiss_ui` | core | write · idem · local | text |
| `cft_verify_model` | core | read · idem · local | text |
| `cft_repair_model` | core | write · idem · local | text |
| `cft_list_models` | core | read · idem · local | text |
| `cft_manage_library` | core | write · destructive · local | text |
| `cft_browse_gallery` | core | read · idem · local | text |
| `cft_annotate_gallery` | core | write · local | text |
| `cft_analyze_workflow` | core | read · idem · local | text |
| `cft_diagnostics` | core | read · idem · local | text |
| `cft_browser_tabs` | core | read · idem · local | text |
| `cft_describe_model` | core | read · idem · local | text |
| `cft_view_model` | vision | read · idem · local | mixed |
| `cft_view_gallery` | vision | read · idem · local | mixed |
| `cft_view_workflow` | vision | read · idem · local | mixed |
| `cft_contact_sheet` | vision | read · idem · local | mixed |
| `cft_inspect_image` | vision | read · idem · local | mixed |
| `cft_manage_files` | admin | write · local | text |
| `cft_configure` | admin | write · local | text |
| `cft_history` | admin | write · local | text |
| `cft_migrate_storage` | admin | write · destructive · local | text |
| `cft_delete_gallery` | admin | write · destructive · local | text |

## core (19 tools)

Text-only tools (no image bytes). A text-only host can complete the download, verify, repair, library and gallery-metadata workflows with these.

### `cft_capabilities`

Return app/MCP versions, bridge readiness, enabled toolsets, limits and guide URIs. Call this before planning a multi-step workflow.

- Safety: read · idem · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `include_tools` | boolean | no | Include the full tool catalog; default: false |

### `cft_get_state`

Return compact app state: config, recent tasks, pending file choice, UI state and blockers. Use compact=true for text-only models. Check blockers before writing.

- Safety: read · idem · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `compact` | boolean | no | Omit full config and long task lists; default: true |
| `task_limit` | integer | no | Recent task count; default: 5; min: 0; max: 50 |
| `fields` | array<string> | no | Dot-path fields to keep, e.g. ['id','info.routed_dir'] |

### `cft_search_models`

Search Civitai's public model catalog. Returns model ids, types, stats and versions. Use this before resolve/download when the user gives a name instead of a URL.

- Safety: read · idem · net
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `query` | string | yes | Search text; minLength: 1 |
| `types` | string | no | Comma-separated types, e.g. LORA,Checkpoint |
| `base_models` | string | no | Comma-separated base models |
| `sort` | string | no | Sort order; enum: "Highest Rated", "Most Downloaded", "Newest" |
| `period` | string | no | Time period; enum: "Day", "Week", "Month", "Year", "AllTime" |
| `nsfw` | boolean | no | Include NSFW results; default: false |
| `limit` | integer | no | Result count; default: 10; min: 1; max: 20 |

### `cft_resolve_model`

Resolve a Civitai URL or model/version id into model metadata, all versions, files, routing destinations, existing-file state and queue state. Each file carries a read-only placement verdict (routed / under_root / outside_root) plus a fix hint. Read-only: does not download.

- Safety: read · idem · net
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `url` | string | no | Civitai model URL |
| `model_id` | integer | no | Civitai model id; min: 1 |
| `version_id` | integer | no | Civitai model version id; min: 1 |
| `file_id` | integer | no | Only inspect one file id; min: 1 |
| `probe` | boolean | no | Probe safetensors headers (two Range requests); default: false |

### `cft_download_model`

Resolve, plan, submit and optionally wait for a Civitai download. By default waits for completion, runs postprocess, and verifies the destination. Returns placement (dest_dir / routed_dir / status / hint) so the caller can confirm the file landed in a model type directory. Existing files return skipped=exists instead of re-downloading; set force=true only when the user asks.

- Safety: write · net
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `url` | string | no | Civitai model URL |
| `urls` | array<string> | no | Batch of Civitai URLs |
| `model_id` | integer | no | Civitai model id; min: 1 |
| `version_id` | integer | no | Civitai model version id; min: 1 |
| `file_ids` | array<integer> | no | Download only these file ids from one version |
| `dest_subdir` | string | no | Optional subdirectory under the configured destination |
| `caps` | array<string> | no | Capabilities; default ['bundle'] enables type routing |
| `wait` | boolean | no | Block until terminal state; default: true |
| `timeout_s` | integer | no | Wait budget in seconds; default: 300; min: 1; max: 1800 |
| `verify` | boolean | no | Verify file and sidecars after completion; default: true |
| `force` | boolean | no | Re-download even if the target file already exists; default: false |

### `cft_list_tasks`

List download/scan tasks. With wait=true and ids, block until those tasks are terminal. timed_out=true is not a failure; poll again or inspect the task. MCP text results are size-capped, so rows default to a compact projection (status, placement, expected/actual sha256, verification_status); pass fields=["*"] for every field or fields=["id","status"] to choose.

- Safety: read · idem · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `ids` | array<string> | no | Task ids |
| `status` | array<string> | no | enum: "pending", "downloading", "paused", "done", "error", "canceled" |
| `limit` | integer | no | Maximum rows; default: 50; min: 1; max: 500 |
| `wait` | boolean | no | Block until terminal/queue empty; default: false |
| `timeout_s` | integer | no | Wait budget in seconds; default: 300; min: 1; max: 1800 |
| `since` | integer | no | Only tasks updated since this epoch ms; min: 0 |
| `fields` | array<string> | no | Dot-path fields to keep, e.g. ['id','info.routed_dir'] |

### `cft_control_tasks`

Pause, resume, retry, start, remove or archive tasks. Removing/archiving tasks is destructive for queue state but never deletes model files.

- Safety: write · destructive · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `action` | string | yes | Action; enum: "pause", "resume", "retry", "start", "remove", "archive" |
| `ids` | array<string> | no | Task ids |
| `all` | boolean | no | Apply pause/resume/start/remove to all tasks; default: false |

### `cft_answer_file_choice`

Answer a pending multi-file download selection. Get identity from cft_get_state (pending_file_choice.id). indices refer to files[].index. ack=true marks the prompt as seen.

- Safety: write · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `identity` | string | yes | Pending choice identity; minLength: 1 |
| `indices` | array<integer> | no | Selected file indices |
| `pause` | boolean | no | Keep the task paused after answering; default: false |
| `ack` | boolean | no | Only acknowledge that the prompt was shown; default: false |

### `cft_dismiss_ui`

Close any popup blocking the UI: onboarding, API-key prompt, tutorial, confirmations, download file choice, post-download move, unsaved-settings, result/info dialogs, detail drawer and image viewer. Closing a confirmation means CANCEL, never confirm; closing a file choice cancels that download. Humans need guides; AI does not. permanent=true (default, affects guides only) also stops future automatic guides.

- Safety: write · idem · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `targets` | array<string> | no | Popup groups/names to close; omit for all; enum: "all", "guides", "dialogs", "onboarding", "api_key", "reverse_tutorial", "confirm", "file_choice", "move_after_download", "settings_unsaved", "restricted", "info", "result", "detail", "about", "image_viewer" |
| `permanent` | boolean | no | Persist onboarding_done and stop automatic guides; default: true |

### `cft_verify_model`

Verify a downloaded model file, optional SHA256, sidecars, preview and image directory. ok means the model file itself is usable; bundle_ok means configured sidecars are complete. With task_id the download-time verdict is passed through (expected_sha256 / actual_sha256 / verification_status), so an unchecked hash is never mistaken for a checked one; hash=true recomputes it.

- Safety: read · idem · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `task_id` | string | no | Download task id |
| `path` | string | no | Absolute model file or directory path |
| `hash` | boolean | no | Recompute SHA256; default: false |

### `cft_repair_model`

Re-run postprocess stages for a completed download (metadata/comments/cover/video/old cleanup). Idempotent; returns postprocess_errors when finished.

- Safety: write · idem · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `task_id` | string | yes | Completed task id; minLength: 1 |
| `stages` | array<string> | no | enum: "metadata", "comments", "cover", "video", "replace_old" |
| `wait` | boolean | no | Wait until repair finishes; default: true |
| `timeout_s` | integer | no | Wait budget in seconds; default: 180; min: 1; max: 1800 |

### `cft_list_models`

List scanned local model rows with path/name/type/version/model ids. Use fields to keep text-only payloads small.

- Safety: read · idem · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `q` | string | no | Search text |
| `type` | string | no | Model type filter |
| `limit` | integer | no | Rows; default: 50; min: 1; max: 500 |
| `offset` | integer | no | Offset; default: 0; min: 0; max: 1000000 |
| `fields` | array<string> | no | Dot-path fields to keep, e.g. ['id','info.routed_dir'] |

### `cft_manage_library`

Run a model-library maintenance operation. Destructive ops (dedupe-delete, cleanup) require confirm=true and explicit paths; run the read-only preview first.

- Safety: write · destructive · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `op` | string | yes | Operation; enum: "verify", "dedupe", "dedupe-delete", "localize", "gen-json", "download-covers", "check-updates", "update", "sync-local", "organize", "rename-c", "rename-preview", "translate-descs", "cleanup" |
| `paths` | array<string> | no | Absolute model file paths |
| `wait` | boolean | no | Wait for completion; default: true |
| `timeout_s` | integer | no | Wait budget in seconds; default: 300; min: 1; max: 1800 |
| `overwrite` | boolean | no | Overwrite existing generated files; default: false |
| `force` | boolean | no | Bypass caches (check-updates); default: false |
| `confirm` | boolean | no | Explicit confirmation for destructive ops; default: false |

### `cft_browse_gallery`

Query the ComfyUI output media library. Returns metadata rows (no image bytes). Use cft_view_gallery for images and cft_annotate_gallery to star/tag.

- Safety: read · idem · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `kind` | string | no | Media kind; enum: "image", "video" |
| `starred` | boolean | no | Only starred items |
| `q` | string | no | Full-text search |
| `sort` | string | no | Sort order; enum: "time_desc", "time_asc", "name_asc", "name_desc" |
| `dirs` | array<string> | no | Directory filters |
| `tag_ids` | array<integer> | no | Positive integer tag ids |
| `checkpoints` | array<string> | no | Checkpoint filters |
| `loras` | array<string> | no | LoRA filters |
| `limit` | integer | no | Rows; default: 50; min: 1; max: 500 |
| `offset` | integer | no | Offset; default: 0; min: 0; max: 1000000 |
| `with_facets` | boolean | no | Include facets; default: false |
| `fields` | array<string> | no | Dot-path fields to keep, e.g. ['id','info.routed_dir'] |

### `cft_annotate_gallery`

Star/unstar items, add/remove tags, or save/delete a tag definition. Does not delete media; use cft_delete_gallery (admin) for that.

- Safety: write · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `op` | string | yes | Operation; enum: "star", "tag-add", "tag-remove", "tag-save", "tag-delete" |
| `ids` | array<integer> | no | Positive integer ids |
| `on` | boolean | no | Star on/off; default: true |
| `tag_ids` | array<integer> | no | Positive integer tag ids |
| `id` | integer | no | Tag id for tag-save/tag-delete (0 creates); default: 0; min: 0; max: 9007199254740991 |
| `name` | string | no | Tag name for tag-save |
| `color` | string | no | Tag color, e.g. #888888 |
| `parent_id` | integer | no | Parent tag id; -1 means no parent; default: -1 |

### `cft_analyze_workflow`

Parse a ComfyUI workflow (.json or embedded PNG/WebP) or Forge/A1111 parameters. Returns prompts, models, generation params and local model matches. Set include_image=true to also return a capped JPEG preview.

- Safety: read · idem · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `path` | string | yes | Absolute workflow/image path; minLength: 1 |
| `full` | boolean | no | Include edges and full node table; default: false |
| `include_image` | boolean | no | Include a resized preview image; default: false |
| `max_side` | integer | no | Preview longest side; default: 1024; min: 256; max: 1536 |

### `cft_diagnostics`

Read error events, automation call events, connectivity, storage and browser-extension status. storage also lists ComfyUI models-root candidates (suggested_root) when routing is off. Use when a download fails, lands outside a model root, or the app looks unreachable.

- Safety: read · idem · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `source` | string | no | Source; enum: "logs", "events", "connectivity", "storage", "extension", "all" |
| `tail` | integer | no | Log tail lines; default: 80; min: 1; max: 500 |

### `cft_browser_tabs`

Search Civitai model pages open in the connected browser extension. Returns title, sanitized URL, model/version ids, copy count and freshness. Read-only: only model-page metadata, never page content, cookies or API keys. Use cft_download_model on a hit.

- Safety: read · idem · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `query` | string | no | Case-insensitive text match across title, URL, model id and version id |
| `limit` | integer | no | Max rows; default: 50; min: 1; max: 200 |
| `include_discarded` | boolean | no | Include sleeping/discarded tabs; default: false |
| `window_id` | integer | no | Only tabs from this browser window; min: 0; max: 1000000000 |
| `active` | boolean | no | Only the active tab; default: false |

### `cft_describe_model`

Return a text summary of a model: name/type/base model/trigger words, cover/sample image paths, prompts and missing-sidecar notes. Never returns image bytes; use cft_view_model for vision.

- Safety: read · idem · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `path` | string | yes | Absolute model file path; minLength: 1 |
| `limit_images` | integer | no | Sample image summaries; default: 4; min: 0; max: 20 |
| `include_prompts` | boolean | no | Include prompt/negative text; default: true |
| `fields` | array<string> | no | Dot-path fields to keep, e.g. ['id','info.routed_dir'] |

## vision (5 tools)

Image-aware tools. These return image content with `delivery=inline` or paths with `delivery=paths`. Text-only hosts should use the core `cft_describe_model` / `cft_browse_gallery` instead.

### `cft_view_model`

Return model cover/sample images as MCP image content (or paths) plus prompts, generation params and dimensions. Videos return a JPEG first frame, never mp4 bytes.

- Safety: read · idem · local
- Returns: mixed

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `path` | string | yes | Absolute model file path; minLength: 1 |
| `limit_images` | integer | no | Images to return; default: 4; min: 1; max: 8 |
| `max_side` | integer | no | Longest side in pixels; default: 768; min: 256; max: 1536 |
| `delivery` | string | no | inline returns image content; paths returns local paths only; enum: "inline", "paths"; default: "inline" |
| `include_prompts` | boolean | no | Attach prompts/params to each image; default: true |
| `safe` | boolean | no | Skip entries marked NSFW when the flag is known; default: false |

### `cft_view_gallery`

Return selected gallery items as MCP image content (or paths) plus generation metadata, tags and starred state. Use cft_contact_sheet for more than 4 items.

- Safety: read · idem · local
- Returns: mixed

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `ids` | array<integer> | yes | Gallery item ids (1-8) |
| `max_side` | integer | no | Longest side in pixels; default: 800; min: 256; max: 1536 |
| `delivery` | string | no | inline or paths; enum: "inline", "paths"; default: "inline" |
| `include_meta` | boolean | no | Include prompt/model/tags metadata; default: true |

### `cft_view_workflow`

Return a workflow/image preview as MCP image content (or path) together with parsed prompts and model references.

- Safety: read · idem · local
- Returns: mixed

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `path` | string | yes | Absolute workflow/image path; minLength: 1 |
| `max_side` | integer | no | Longest side in pixels; default: 1024; min: 256; max: 1536 |
| `delivery` | string | no | inline or paths; enum: "inline", "paths"; default: "inline" |

### `cft_contact_sheet`

Compose up to 24 gallery items or model sample images into one labeled JPEG grid plus an index-to-id/path mapping. Use it to triage many images cheaply, then call cft_view_gallery for the selected ids.

- Safety: read · idem · local
- Returns: mixed

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `source` | string | yes | Source; enum: "gallery", "model" |
| `ids` | array<integer> | no | Gallery ids (gallery source) |
| `path` | string | no | Model file path (model source) |
| `limit` | integer | no | Maximum cells; default: 16; min: 1; max: 24 |
| `cols` | integer | no | Grid columns; default: 4; min: 1; max: 6 |
| `cell` | integer | no | Cell size in pixels; default: 256; min: 96; max: 512 |
| `delivery` | string | no | inline or paths; enum: "inline", "paths"; default: "inline" |

### `cft_inspect_image`

Inspect a local image path or base64 image upload: dimensions, format, embedded prompts, generation params and model references, with an optional preview image. Read-only and in-memory.

- Safety: read · idem · local
- Returns: mixed

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `path` | string | no | Absolute image path |
| `data_b64` | string | no | Base64 image bytes; do not prefix with data:. Max 8MB |
| `filename` | string | no | Original filename for format hints |
| `max_side` | integer | no | Preview longest side; default: 768; min: 256; max: 1536 |
| `delivery` | string | no | inline or paths; enum: "inline", "paths"; default: "inline" |
| `include_image` | boolean | no | Include a preview image; default: true |

## admin (5 tools)

Administrative tools. Enable explicitly; never default to `admin`.

### `cft_manage_files`

Move a model/task/history destination, rename a model file with sidecars, or browse a directory. Destinations must already exist; preview rename with preview=true.

- Safety: write · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `action` | string | yes | Action; enum: "move", "rename", "list" |
| `path` | string | no | Model file path |
| `dest_dir` | string | no | Existing absolute destination directory |
| `new_name` | string | no | New file name without changing sidecars manually |
| `paths` | array<string> | no | Absolute model file paths |
| `preview` | boolean | no | Only compute new names; default: false |
| `task_id` | string | no | Download task id for move |
| `history_id` | string | no | History id for move |
| `dirs_only` | boolean | no | Directory listing only (default true); default: true |

### `cft_configure`

Read the full config (secrets redacted) or write whitelisted keys. Writes are atomic per call; unknown keys reject the whole patch.

- Safety: write · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `action` | string | yes | Action; enum: "get", "set" |
| `config` | object | no | Whitelisted config patch for action=set; free-form object |

### `cft_history`

List, remove or clear download history records. This never deletes model files; remove/clear also drops terminal queue rows and stale .part files.

- Safety: write · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `action` | string | yes | Action; enum: "list", "remove", "clear" |
| `ids` | array<string> | no | History ids |
| `limit` | integer | no | Rows; default: 50; min: 1; max: 500 |
| `status` | string | no | Status filter |
| `detail` | boolean | no | Return raw history rows; default: false |
| `preview` | boolean | no | Only report what would be removed; default: false |

### `cft_migrate_storage`

Move the app data directory (config/tasks/history/cache). Requires confirm=true. Models are not moved. Refused while downloads/scans are running.

- Safety: write · destructive · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `directory` | string | yes | Existing absolute target directory; minLength: 1 |
| `replace_existing` | boolean | no | Allow overwriting target data; default: false |
| `confirm` | boolean | no | Explicit confirmation; default: false |

### `cft_delete_gallery`

Move gallery media files to the system recycle bin. Requires confirm=true. Sidecar files are never deleted with the media.

- Safety: write · destructive · local
- Returns: text

| Parameter | Type | Required | Notes |
| --- | --- | --- | --- |
| `ids` | array<integer> | yes | Positive integer ids |
| `confirm` | boolean | no | Explicit confirmation; default: false |

## Limits

- `max_images`: `8`
- `max_image_side`: `1536`
- `max_image_bytes`: `3145728`
- `max_total_image_bytes`: `8388608`
- `max_upload_bytes`: `8388608`
- `max_gallery_ids`: `500`
- `max_contact_items`: `24`
- `max_batch`: `50`
- `default_wait_s`: `300`
- `max_wait_s`: `1800`

## Error codes

| Code | HTTP | Retryable |
| --- | --- | --- |
| `invalid_argument` | 400 | no |
| `not_found` | 404 | no |
| `unsupported` | 501 | no |
| `forbidden` | 403 | no |
| `conflict` | 409 | yes |
| `busy` | 409 | yes |
| `rate_limited` | 429 | yes |
| `timeout` | 408 | yes |
| `app_not_running` | 503 | yes |
| `version_mismatch` | 409 | no |
| `destructive_requires_confirm` | 400 | no |
| `permission_denied` | 403 | no |
| `path_outside_roots` | 403 | no |
| `verification_failed` | 200 | no |
| `task_failed` | 200 | no |
| `network_error` | 502 | yes |
| `invalid_media` | 400 | no |
| `internal_error` | 500 | yes |
