---
name: civitai-free-tool
description: "Control the local CivitaiFreeTool desktop app via MCP; search, download, verify and repair Civitai or HuggingFace models; manage the local model library; browse and tag the ComfyUI output gallery; inspect model covers and sample images with vision. Use for text-only hosts (core tools, metadata only), multimodal hosts (core+vision, inline images), OpenAPI/Dify/n8n/LangChain fallbacks, SSE event monitoring, the CLI client, outbound webhook delivery, MCP setup or download-failure triage."
---

# CivitaiFreeTool

控制本机 CivitaiFreeTool 桌面端：模型搜索/下载/校验/修复、模型库维护、ComfyUI 输出画廊浏览与打标、封面/示例图查看。**MCP 是主入口，HTTP 只是回退**。不要编造图片内容、下载结果、任务状态或哈希。

## 何时使用

- 用户给出 Civitai/HuggingFace 模型链接或名字，要下载、续传、校验、修复、查重、查更新。
- 要查看本地模型库、分流目录、下载队列/历史、待选文件，或回答“为什么没下成功”。
- 要浏览/筛选/打标 ComfyUI output 画廊，或查看模型封面、示例图、工作流预览。
- 要安装/诊断 MCP 侧车、端口、代理/DNS、版本不匹配。
- 不适用：与 CivitaiFreeTool 无关的通用文件操作；不要用它读写任意文件或执行命令。

## 1. 连接与探测

1. **优先 MCP**：先调 `cft_capabilities`，确认 `ok`、`bridge.reachable`、版本与限额；
   同时看 `extension.online/version`（扩展是否连上）与 `mcp.running`（HTTP 侧车是否在跑）。
   实际可用的工具以 Host 的 `tools/list` 为准（MCP 只暴露 `--toolsets` 启用的工具）。多步任务前必做。
2. **MCP 不可用才走 HTTP 回退**（无工具、连接失败、Host 不支持 MCP）：
   - `GET http://127.0.0.1:47531/api/health` 确认 app 在线。
   - `GET /api/automation/ai` 取能力、工具集、限额、MCP 提示与 `caps.openapi` /
     `caps.sse`；`GET /api/automation/tools?profile=text|vision` 取工具表。
   - 函数调用平台：`GET /api/automation/openapi.json?profile=text` 导入；调用
     `POST /api/automation/tool/{cft_name}`，body 直接填 arguments。脚本也可直接用
     `POST /api/automation/tool`（`{name,arguments}` 包装）。
   - 需要持续感知任务、待选文件或弹层：订阅
     `GET /api/automation/events/stream`，保存每条事件的 `id`，断线用 `Last-Event-ID`
     重放；不要每秒轮询 state。
   - **Shell/CI 场景**可用 `CivitaiFreeToolCLI.exe`（`call` / `batch` / `events` / `doctor`），
     它只连本机桥，退出码映射契约；完整命令见 [docs/ai-cli-webhook.md](../../docs/ai-cli-webhook.md)。
   - **需要事件出站推送**时，经用户明确同意后用 `cft_configure` 设置 `webhook_*`
     （默认关闭、默认只允许非公网目标）；`GET/POST /api/automation/webhooks` 可看状态/测试，
     不要把 secret 写进回答或日志。
   - HTTP MCP 默认监听 `http://127.0.0.1:47532/mcp`（Streamable HTTP），必须带 `Authorization: Bearer <token>`；token 只从本地环境/客户端注入，绝不写进仓库或回答。
   - **冻结版桌面端会自动拉起 MCP HTTP**（47532，桥断即退）；不需要用户另开控制台。源码运行需手动 `python mcp_server.py --http`。
   - 配置片段见 `assets/mcp-qoder.json`、`assets/mcp-claude.json`、`assets/mcp-cursor.json`；也可在仓库根运行
     `python scripts/mcp_setup.py --client cursor|claude --transport stdio|http --apply` 自动合并（先备份、token 不回显）。
3. stdio 启动示例：`CivitaiFreeToolMCP.exe --stdio --toolsets core,vision`（纯文本用 `core`）；HTTP 用 `--http --port 47532`。MCP 不会自动启动 GUI，先打开桌面端。
4. 全都不通：运行 `python scripts/cft_doctor.py`（可加 `--mcp <exe>`、`--bridge`、`--token-env`），按输出的下一步处理。

## 2. 文本模型（text-only）

- 只启用/使用 `core`（19 个工具）。`cft_describe_model` 在 core 里，只返回文本、绝无图片字节。**绝不请求 inline 图片**，不调用 `cft_view_model`、`cft_view_gallery`、`cft_view_workflow`、`cft_contact_sheet`、`cft_inspect_image`。
- 看图需求改为文本：`cft_describe_model`（封面/示例图路径、提示词、缺失 sidecar 说明）、`cft_browse_gallery`（元数据行）、`cft_analyze_workflow`（`include_image=false`，返回提示词/模型/参数/本地匹配）。
- 保持 payload 小：`limit`、`fields`、`compact=true`；不要 `full=true`。

## 3. 多模态模型（multimodal）

- 启用 `core,vision`（MCP `--toolsets core,vision`，或 profile `vision`）。
- 先 `cft_contact_sheet` 粗看（最多 24 格，`source=gallery|model`），按返回的 index→id/path 映射选 1–8 个，再 `cft_view_gallery` / `cft_view_model` 取选中项；`delivery=inline`。
- 图片参数：`max_side` 768–1024 起步（上限 1536），`limit_images` ≤8；视频只返回 JPEG 首帧，不返回 mp4。
- NSFW 标记可能未知；`safe=true` 只跳过已知 NSFW，未知不等于安全。看不到或被省略的图片要如实说明。

## 4. 写操作与确认

- 写之前先 `cft_get_state`（`compact=true`）看 `blockers`、`pending_file_choice`、最近任务；有阻塞先解决。
- 界面被任何弹层挡住时，直接 `cft_dismiss_ui {"targets":["all"],"permanent":true}`：
  新手引导 / API Key 提示 / 反向解析教程 / 确认框 / 下载文件选择 / 下载完成移动询问 /
  设置未保存 / 受限提示 / 结果窗 / 详情抽屉 / 图片查看器。**关闭确认框=取消，绝不会代替用户确认**；
  关闭下载文件选择=取消本次下载；关闭设置未保存=继续编辑。人需要引导才会发现功能，AI 不需要；
  `blockers` 里出现 `onboarding_guide` 或 `ui_overlay` 时按 `fix` 执行即可。
  设置页的手动「新手引导」仍保留给人用。
- 破坏性操作先 preview/dry-run（如 `cft_manage_library` 的 `dedupe` 预览、`cft_manage_files` 的 `preview=true`），列出精确 ids/paths/数量，取得用户明确的“执行”确认，再带 `confirm=true` 调用。
- 明确破坏性：`cft_control_tasks`（remove/archive）、`cft_manage_library`（dedupe-delete/cleanup）、`cft_migrate_storage`、`cft_delete_gallery`（回收站）。`force=true` 重下、move/rename、`cft_history` remove/clear、`cft_configure` 写入也必须先确认。
- 一次只做一个可解释的变更；完成后用 `cft_verify_model` / `cft_list_tasks` / `cft_get_state` 核对，不要假设成功。

## 5. 错误与重试

- 先读 `code` / `retryable` / `hint`；有 `next_actions` 优先按它执行（它们是可执行的工具调用，不是建议）。但 text-only 下忽略任何指向 `cft_view_*` / `cft_contact_sheet` 的图片调用，改用文本替代。
- 不要重试 `invalid_argument`、`not_found`、`unsupported`、`version_mismatch`、`destructive_requires_confirm`；也不要盲重试 `forbidden`、`permission_denied`、`path_outside_roots`、`verification_failed`、`task_failed`、`invalid_media`。
- `conflict`、`busy`、`rate_limited`、`timeout`、`app_not_running`、`network_error`、`internal_error` 可按退避重试；`timed_out=true` 不是失败，用 `cft_list_tasks(wait=true)` 或再查。
- 版本不匹配：升级桌面端/MCP，不要改契约或硬编码工具名。

## 6. 下载与修复

- `cft_download_model` 默认 `wait=true`、`verify=true`；返回 `skipped="exists"` 表示文件已在，**不重下**。
- 结果里的 `placement.status`：`routed` 才算进了类型目录；`under_root` / `outside_root` 按 `hint`
  处理——用 `cft_diagnostics {source:"storage"}` 看 `suggested_root`，再用 `cft_configure`
  设置 `comfyui_models_root` / `route_by_type`，或在下载时传 `dest_subdir`。
  **不要自己移动已下载文件**（会破坏任务、历史与 ComfyUI 引用）。
- 失败或收尾不完整：`cft_list_tasks` 看 `status`/`postprocess`/`postprocess_errors` → `cft_verify_model` 看 `ok`/`bundle_ok` → `cft_repair_model` 补缺失 stage → 再 verify。不要用 `force=true` 代替修复，不要删模型文件。
- 只有用户明确要求替换，或确认文件损坏且同意覆盖时，才用 `force=true`；`verify.ok=false` 先诊断，不要直接重下。

## 7. 按需加载

| 需要 | 读 |
| --- | --- |
| 29 个工具的参数/安全注解/返回类型 | [references/tools.md](references/tools.md) |
| 端到端调用序列与预期结果 | [references/recipes.md](references/recipes.md) |
| OpenAPI 导入、每工具别名、SSE 订阅与重连 | [references/recipes.md](references/recipes.md) §8 |
| CLI 子命令/退出码、webhook 配置/签名/测试 | [references/recipes.md](references/recipes.md) §9；[docs/ai-cli-webhook.md](../../docs/ai-cli-webhook.md) |
| 证明真实下载把 `task.*` 推到了 webhook | [references/recipes.md](references/recipes.md) §9.3 + `scripts/verify_webhook_delivery.py` |
| 视觉工具、delivery、contact sheet、视频首帧 | [references/vision.md](references/vision.md) |
| 本机边界、token、破坏性确认、路径越界 | [references/safety.md](references/safety.md) |
| 连通性/代理/DNS 诊断脚本 | [scripts/cft_doctor.py](scripts/cft_doctor.py) |
| 从 ai_contract 重新生成工具表 | [scripts/gen_skill_refs.py](scripts/gen_skill_refs.py) |
| MCP 客户端配置片段 | [assets/](assets/) |

## 底线

- 只操作本机 app；不暴露 47531/47532，不转发端口，不做公网代理。
- 不伪造图片、提示词、哈希、下载完成、任务状态；工具没返回就说明没拿到。
- 29 个 `cft_*` 工具只覆盖 Civitai；HuggingFace 链接走桌面端自身流程，不要编造 `cft_*` HF 调用。
- 只读默认；`admin` 工具集显式开启；不确定时先问用户。
