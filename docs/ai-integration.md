# AI 接入契约（MCP / Skill / HTTP）

> 状态：v2.7.0 契约（MCP / OpenAPI / SSE / Skill / HTTP / CLI / webhook 均已落地）。
> 唯一事实源是 `ai_contract.py`；本文解释结构与边界，不重复维护工具表。

## 1. 角色

| 层 | 职责 | 不做什么 |
| --- | --- | --- |
| MCP 侧车 `CivitaiFreeToolMCP.exe` | 唯一工具协议；stdio + Streamable HTTP；tools/resources/prompts；图片内容块 | 不复制下载/分流/库管理逻辑 |
| Skill `skills/civitai-free-tool` | 安装、操作手册、recipes、安全边界、排障 | 不承载业务逻辑，不替代 MCP |
| HTTP API `/api/automation/*` | 脚本与不支持 MCP 的客户端；同一契约的投影 | 不做过渡层；不另写一套 schema |
| OpenAPI 3.1 `/api/automation/openapi.json` | Dify / n8n / LangChain / Open WebUI 等函数调用平台的导入面 | 不手写工具表；不暴露管理员工具为默认 |
| SSE `/api/automation/events/stream` | 任务、弹层、扫描、配置变化的服务端推送 | 不新增工具；不做入站回调 / 公网服务 |
| CLI `CivitaiFreeToolCLI.exe` | 本机脚本/Shell Agent 的命令行客户端；`call` / `events` / `batch` / `doctor` | 不直连 C 站；不复制工具逻辑；只连回环，不自动启动桌面端 |
| Webhook `/api/automation/webhooks` | 把同一批契约事件出站推送到用户配置的 HTTP 端点 | 不新增事件；不做入站 webhook；不持久化；默认关闭且默认只允许非公网目标 |

```text
MCP Host
  ├─ stdio ─────────────┐
  ├─ Streamable HTTP ───┤ CivitaiFreeToolMCP.exe
  │                       └─ POST /api/automation/tool
  ├─ HTTP fallback ─────►       │
  │                             ▼
OpenAPI importer ──► GET /api/automation/openapi.json
                 └─► POST /api/automation/tool/{name} ─► ai_tools.execute_tool
CivitaiFreeToolCLI.exe ─► POST /api/automation/tool/{name} / GET /events/stream
Local agent ───────► GET /api/automation/events/stream ◄─ EventHub（1 秒快照差分）
                                                     ├─ sink ─► webhook_dispatch ─► 用户端点
                                                     │
                                                     ▼
                                  automation_api.dispatch / Api / downloader
```

CLI 与 webhook 的完整契约（命令、退出码、SSE 重连、HMAC、重试、SSRF/重定向策略）见
[CLI / webhook 传输层](ai-cli-webhook.md)。两者都只消费上面的同一份工具/事件目录。

## 2. 契约层

`ai_contract.py` 定义：

- `TOOLS`：29 个工具，按 `core` / `vision` / `admin` 分组。
- `PROFILES`：`text = core`、`vision = core+vision`、`admin = core+admin`、`all`。
- `TOOLSETS`：MCP 侧车用 `--toolsets` 选择；默认只开 `core`。
- JSON Schema：所有工具参数都在契约里，HTTP 与 MCP 共用。
- MCP 安全注解：`readOnlyHint`、`destructiveHint`、`idempotentHint`、`openWorldHint`。
- `LIMITS`：图片数、边长、字节、上传、批量、等待上限。
- `ERROR_CODES` 与 `normalize_error()`：稳定 `code/retryable/hint/next_actions`。
- `EVENTS`：SSE 领域事件目录（类型、分组、说明、payload schema）；OpenAPI 与
  `cft_capabilities` 的 `caps.events` 都从这里生成。
- `CLI_VERSION` / `CLI_EXIT_CODES`：CLI 传输语义；只映射退出码，不新增工具。
- `WEBHOOK_LIMITS` / `WEBHOOK_DEFAULT_EVENT_GROUPS`：出站 webhook 的超时、重试、
  队列、日志与默认事件组；仍投影同一份 `EVENTS`，默认排除高频 `api.call`。
- `RESOURCES` / `PROMPTS`：MCP 资源与用户可调用的 recipe prompt。
- `TEXT_GUIDE` / `VISION_GUIDE`：`/guide` 与 `cft://guide/*` 的同一份内容。

新增工具的顺序：先改 `ai_contract.py`，再补 `ai_tools.py` 执行映射，最后跑
`tests/test_ai_contract.py`；MCP 与 HTTP 不允许各自加名字。完整跨层流程（含状态/事件登记、
弹层登记、数量同步与打包验证，以及每一步撞哪个检查）见 §11。

## 3. 工具面

### core（19，纯文本安全）

`cft_capabilities`、`cft_get_state`、`cft_search_models`、`cft_resolve_model`、
`cft_download_model`、`cft_list_tasks`、`cft_control_tasks`、`cft_answer_file_choice`、
`cft_dismiss_ui`、`cft_verify_model`、`cft_repair_model`、`cft_list_models`、
`cft_manage_library`、`cft_browse_gallery`、`cft_annotate_gallery`、`cft_analyze_workflow`、
`cft_describe_model`、`cft_diagnostics`。

`cft_dismiss_ui` 是「人需要引导、AI 不需要」的统一入口，关闭任何挡路弹层：新手引导 /
API Key 提示 / 反向解析教程 / 确认框 / 下载文件选择 / 下载完成移动询问 / 设置未保存 /
受限提示 / 结果窗 / 详情抽屉 / 图片查看器。语义是安全的：**确认框关闭=取消，绝不代替
用户确认**；下载文件选择关闭=取消本次下载；设置未保存关闭=继续编辑。`permanent=true`
只对引导生效（写 `onboarding_done=true` 与 `auto_guides=false`），设置页手动入口仍可打开。

### vision（5）

`cft_view_model`、`cft_view_gallery`、`cft_view_workflow`、`cft_contact_sheet`、
`cft_inspect_image`。

### admin（5，显式开启）

`cft_manage_files`、`cft_configure`、`cft_history`、`cft_migrate_storage`、
`cft_delete_gallery`。

## 4. 统一结果包

HTTP `/tool`、MCP `tools/call` 共用：

```json
{
  "ok": true,
  "tool": "cft_view_gallery",
  "data": {"items": [{"id": 12, "prompt": "...", "width": 1024}]},
  "content": [
    {"type": "text", "text": "#1 ..."},
    {"type": "image", "mime": "image/jpeg", "b64": "...", "width": 768, "height": 768}
  ],
  "next_actions": [
    {"tool": "cft_annotate_gallery", "arguments": {"op": "star", "ids": [12]}}
  ],
  "meta": {"ms": 42, "truncated": false}
}
```

- `data`：结构化结果；视觉工具不重复放 base64。
- `content`：中性内容块；MCP 转 `TextContent`/`ImageContent`，HTTP 可转
  OpenAI/Anthropic。
- `next_actions`：服务端确认过前置条件的下一条工具调用。
- 错误：`{ok:false, code, retryable, msg, hint, next_actions}`；MCP 用 `isError:true`。
- **文本块不返残串**：MCP 的 `content[].text` 是 JSON 文本，超过 4000 字符时按**结构**收缩——先夹逼超长
  字符串（`...(截断)`），再整行丢弃有序列表的尾部，并补 `truncated / dropped_rows / hint`，
  保证调用方 `json.loads` 一定成功（旧实现直接切字符串，`cft_list_tasks` 一超限就返回
  `Unterminated string` 的废文本）。要更全的结果用 `fields`/`limit` 收窄再取，或走不截断的 HTTP 投影。
  结果可能被拆成多个 `content` 片段，客户端应把所有 `text` 拼接后再解析。
- **列表工具默认给紧凑投影**：`cft_list_tasks` 不传 `fields` 时只发判断所需的字段（状态、落点、
  `expected_sha256`/`actual_sha256`/`verification_status`、`postprocess_errors` 等），因为全字段 6 行就会
  撑爆文本预算、逼收缩器整行丢弃——丢的恰好是最新任务。任务行是时间序，所以这一层超预算时**从最旧的丢**、
  保留最新几条，并给 `oldest_dropped_ids`（`count` 仍是总数）。要全量传 `fields=["*"]`，自选传
  `fields=["id","status"]`；`fields` 是**数组**，写成字符串会被 inputSchema 拒掉（`'*' is not of type 'array'`）。
  HTTP 投影不受此默认影响。

## 5. 视觉层

来源优先级：

1. 本地 `<模型>.images/` 图片或视频首帧。
2. 本地封面（`.preview.*` / `.cover.*`）。
3. C站缩略图缓存。
4. 视频海报缓存。

规则：

- 视频永远返回 JPEG 首帧，不返回 mp4。
- 每张图带 `mime/b64/width/height/bytes` 与 `prompt/negative/steps/sampler/seed/models`。
- `delivery=paths` 只回本地路径；`inline` 回图片内容。
- `max_images<=8`、`max_side<=1536`、单图/总字节双上限；超出返回 `omitted`。
- 只读配置的模型根/媒体根，`realpath` 校验。
- 上传分析上限 8MB，只在内存解析，不落盘。

## 6. MCP 侧车

- 入口：`mcp_server.py`，依赖 `requirements-mcp.txt`，打包为控制台程序 `CivitaiFreeToolMCP.exe`
  （`pyinstaller -y CivitaiFreeToolMCP.spec`）。
- stdio：stdout 只写 JSON-RPC；日志走 stderr。
- Streamable HTTP：默认 `127.0.0.1:47532`，必须带 Bearer token。
- 默认 toolsets：`core`；多模态 Host 配 `core,vision`；管理操作配 `admin`。
- **常驻 HTTP（冻结版）**：桌面端启动后自动拉起同目录侧车
  `--http --port 47532 --toolsets core,vision,admin --exit-with-bridge`；47532 被占用时跳过，
  侧车在桥连续不可达约 30s 后自行退出，避免孤儿进程。源码运行/测试不自动拉起。
  配置 `mcp_http_autostart=false` 或环境变量 `CFT_NO_MCP_HTTP=1` 可关闭。
- **客户端接入**：`python scripts/mcp_setup.py --client cursor|claude --transport stdio|http --apply`
  生成/合并 `mcpServers`（先备份，保留其它 server）；HTTP 的 Bearer token
  写入客户端配置但不回显。`--print` 只打印片段。
- token 文件：`%LOCALAPPDATA%\CivitaiFreeToolWeb\mcp_http_token.txt`；日志与终端都不打印 token。
- 握手：`GET /api/health` + `/api/automation/ai`；版本不匹配返回 `version_mismatch`。
- `cft_capabilities` 额外返回 `extension`（在线/版本/索引新鲜度）与 `mcp.running/token_file`，
  AI 不必猜扩展有没有连上。
- 长任务：MCP progress notification；取消只停止等待，不擅自取消下载。
- 文件选择：返回 `needs_input`，用 `cft_answer_file_choice` 跟进，不依赖 elicitation。
- 不自动启动 GUI；`--launch-app` 显式开启后才尝试。

### 下载落点（placement）

- `cft_resolve_model` / `POST /api/automation/plan` 的每个文件带只读 `placement`：
  `dest_dir / routed_dir / folder / under_models_root / status / hint`；
  `status ∈ routed | under_root | outside_root | pending`。
- `cft_download_model` / `POST /api/automation/download` 在单条结果与每个 item 上返回同一结构；
  `skipped=exists` 时也带落点。AI 不需要从 `routed_dir` 是否为空猜「到底分没分」。
- `cft_diagnostics {source:"storage"}` 增加 `candidates` / `suggested_root`：只从已配置的
  `models_dir` / `download_dir` / `download_target_dir` 及其直接上级里，找包含多个 ComfyUI
  核心类型目录的路径；不扫描整盘，不自动写配置。
- 修复建议固定指向已有工具：`cft_configure`（设 `comfyui_models_root` / 开 `route_by_type`）
  或 `cft_download_model(dest_subdir=...)`。**绝不自动移动已下载文件，绝不新建未注册的类型目录。**

### 界面弹层与原生对话框

- 所有应用内弹层登记在 `web/ai-overlays.js`：新手引导、API Key 提示、反向解析教程、确认框、
  下载文件选择、下载完成移动询问、设置未保存、受限提示、结果窗、更新结果窗、详情抽屉、
  关于窗、图片查看器。此外 MutationObserver 兜底扫描 `.rd-mask` / `.df-mask` /
  `.settings-exit-mask`，新加的对话框即使没显式登记也能被 `dialogs` 一键收起。
  前端把当前可见弹层上报到 `state.ui.guides.overlays`，AI 不需要截图猜。
- 关闭语义是安全的：**确认框=取消**；文件选择=取消本次下载；设置未保存=继续编辑；
  其余只收起。绝不因为「关闭」而执行删除、移动或确认。
- `web/` 里已经**没有任何原生 `alert` / `confirm` / `prompt` 调用点**（0 处）：浏览器原生对话框
  会卡死整条 JS，`cft_dismiss_ui` 也接不到。剩下的两处「确认」在应用内确认框不可用时直接按
  取消处理，不再退回原生对话框。原生 `pick_dir` / `pick_file` 选择器仍只属于人：AI 若要选目录，
  用 `GET /api/automation/fs` / `cft_manage_files` / `cft_configure`，不要点界面上的「浏览」。

## 7. HTTP 投影

新增：

- `GET /api/automation/ai`：能力、工具集、限额、guide 地址、MCP 提示，以及
  `caps.openapi` / `caps.sse` / `caps.events`。
- `GET /api/automation/tools?format=openai|anthropic|mcp&profile=text|vision|admin|all`
- `GET /api/automation/openapi.json?profile=text|vision|admin|all`：运行时从 `ai_contract`
  生成 OpenAPI 3.1；默认 text 19 个操作，`servers` 只回显回环 Host。
- `POST /api/automation/tool`：`{name, arguments, content_format?}`，MCP 也调它。
- `POST /api/automation/tool/{name}`：每工具一个操作，body 直接是 arguments；与 `/tool`
  共用 `_tool_call`，供 OpenAPI 导入器使用。GET 返回 405，未知工具 404。
- 只读工具 `cft_browser_tabs`：搜索浏览器扩展上报的已打开 C 站模型页（标题/URL/模型/版本 ID），
  不读页面正文或 Cookie；命中后用 `cft_download_model` 下载。链路见
  [`browser-tabs-bridge.md`](browser-tabs-bridge.md)。
- `GET /api/automation/events/stream`：SSE 领域事件流；支持 `Last-Event-ID`、
  `since`、`types`、`snapshot`。详见 [`docs/ai-openapi-sse.md`](ai-openapi-sse.md)。
- 请求体上限：自动化面默认 1MB；`/tool`、`/vision/analyze` 与 `LARGE_BODY_TOOLS`
  （当前是 `cft_inspect_image`）的别名放宽到 8MB，超限返回 413 + `limit_bytes`。
- `POST /api/automation/ui/dismiss`：关闭任何挡路弹层；目标可按 `all` / `guides` /
  `dialogs` 或具体名字（`confirm`、`file_choice`、`move_after_download`、
  `settings_unsaved`、`detail`、`image_viewer` …）。确认框按取消处理，绝不自动确认。
  `permanent=true` 只对引导写入 `onboarding_done` / `auto_guides=false`。
- `GET /api/automation/state` 的 `ui.guides`：`onboarding_pending` / `auto_guides` /
  `dismiss_rev` / `dismiss_targets` / `overlays[]`（当前可见弹层及标题、是否阻塞、上报时间），
  顶层 `config_rev` 给出配置版本；AI 无需靠截图判断界面上有什么。
- `GET /api/automation/guide?profile=text|vision&format=markdown|json`
- `/api/automation/vision/*`：model / gallery / workflow / contact-sheet / analyze。
- 现有详情端点：`mode=text`（剥 base64）、`include_images`、`include_image`。

现有端点默认行为不变；新能力全部是加法。

### 非 MCP 平台：OpenAPI + SSE

1. 桌面端启动后，把 `http://127.0.0.1:47531/api/automation/openapi.json?profile=text`
   导入 Dify / n8n / LangChain / Open WebUI 等本地函数调用平台。
   每个工具是一个独立 operation：`POST /api/automation/tool/cft_*`，body 直接填参数。
2. 需要感知任务、下载文件选择或弹层时订阅 SSE，而不是每秒轮询：

   ```bash
   curl -N "http://127.0.0.1:47531/api/automation/events/stream?types=task,file_choice,ui"
   ```

   客户端记录事件 `id`（seq）；断线后用 `Last-Event-ID` 自动重放。收到 `stream.reset`
   说明环形缓冲已过期，应重新读 `cft_get_state` 对齐。
3. OpenAPI 与 SSE 都是本地面：桥只监听回环地址。云端 GPT Actions 或远程容器默认连不到，
   不要为此把端口暴露到局域网/公网；Docker 内 Agent 需要宿主网络或运行在宿主上。

## 8. Skill

```text
skills/civitai-free-tool/
  SKILL.md
  references/tools.md      # 由 ai_contract 生成
  references/recipes.md
  references/vision.md
  references/safety.md
  scripts/cft_doctor.py
  assets/mcp-qoder.json
  assets/mcp-claude.json
  assets/mcp-cursor.json
```

Skill 先调 `cft_capabilities`；文本模型只用 core 与 describe/browse；多模态模型加 vision
并把 `delivery=inline`；破坏性操作必须先 preview/dry-run 并取得用户确认。

## 9. 安全

- MCP HTTP 只绑 `127.0.0.1`，Bearer token 本地生成，不入库、不进 release。
- 自动化端点仍是本机信任模型；不暴露到公网。
- OpenAPI 的 `profile` 只控制文档可见性：默认 `text` 不含 admin；每工具别名仍复用统一
  执行与 `confirm:true` 门控。
- SSE 只允许回环/白名单浏览器来源；事件只带配置键名，不带 `api_key`、翻译密钥或 token；
  限制为 8 个订阅、500 条重放缓冲、单客户端 256 队列。
- `GET /api/automation/state` 的 `config.proxy_address` 若含 `user:pass@` 一律脱敏成
  `***:***@`，状态、快照与事件都不会回显代理凭据。
- 图片只读模型根/媒体根；上传只解析不写盘。
- 破坏性工具在 schema 标 `destructiveHint`，执行层仍强制 `confirm:true`。
- 不提供任意文件读取、命令执行或 UI 截图能力。

## 10. 验收

1. SDK 客户端完成 initialize → tools/list → tools/call → resources/read → prompts/get。
2. 纯文本模型仅用 core 工具完成 resolve → download → verify，不需要读 27KB 文档。
3. 多模态模型收到 MCP image content；contact sheet 可一次浏览多张。
4. HTTP `/tools` 与 MCP `tools/list` 来自同一契约，测试比对无漂移。
5. app 未启动、版本不匹配、路径越界、端口占用都有稳定 code/retryable/hint。
6. 打包后的 MCP EXE stdio stdout 干净，HTTP 无 token 被拒。
7. 全量 pytest、现有 smoke、AI 审计、两个 EXE 隔离目录验收通过。
8. OpenAPI 文档与契约数量一致，每工具别名和 `/tool` 等价；SSE 真机完成首帧、心跳、
   `Last-Event-ID` 重放与 429 上限验证。

非 MCP 平台（Dify / n8n / LangChain 等）的接入面见
[OpenAPI 3.1 + SSE 设计稿与验收](ai-openapi-sse.md)（已实现，含打包真机验收清单）。

## 11. 新增 / 修改一个契约工具的跨层流程

顺序不能颠倒：契约先行，MCP、HTTP、OpenAPI、Skill 都只是它的投影。每一步都标了
「漏掉会撞哪个检查」，照着走就不需要靠跑全量测试来发现层没接通。

**1）契约定义** — [`ai_contract.py`](../ai_contract.py) 的 `TOOLS[name]`

- 键：`name` / `title` / `description` / `toolset` / `inputSchema` / `handler` / `returns` /
  `annotations`，一个都不能少（`handler` 是执行映射的字符串名，不是函数引用）。
- `toolset` 决定可见性：`core` 进默认 text profile，纯文本模型也会用它；破坏性动作放 `admin`
  并标 `destructiveHint`。
- `inputSchema` 里出现 `confirm` 属性 = 执行层强制 `confirm:true`；出现 `wait` = 该工具可阻塞等待。
  参数口径沿用既有名字（`ids` / `fields` / `limit`），`additionalProperties: false`。
- 漏了会撞：`tests/test_ai_contract.py`（键完整性、名字唯一、四个 profile 数量）。

**2）执行映射** — [`ai_tools.py`](../ai_tools.py) 的 `execute_tool` handler 分支

- 分支只做「拆参数 → 调既有能力 → 装信封」，能力本体继续走现有 automation 路径或 `api.*` 方法；
  绝不在工具里直接读盘、发网络或复制下载逻辑，否则就有第二条实现。
- 成功走 `_ok`、失败走 `_err`，错误由 `ai_contract.normalize_error` 统一补
  `code` / `retryable` / `hint` / `next_actions`。
- 需要新后端能力时：先加 `webui.py` 的 Api 方法（复用优先），由 `browser_bridge.py` 暴露，
  再在工具里调用。
- 漏了的症状：`/tool` 返回 404 未知工具（契约有名字、执行层没分支）。

**3）HTTP 路由** — 通常零改动

- `/tool`、`/tool/{name}`、`/tools`、`/openapi.json` 都遍历契约，新工具自动出现在四个面上。
- 只有「非工具形态的专用端点」（例如 `/ui/dismiss`）才动 `automation_api.py` 的 dispatch 与
  `MANIFEST` 清单行。
- 请求体可能带图片字节 → 把工具名加进 `ai_contract.LARGE_BODY_TOOLS`，否则 1MB 就 413。
- 漏了会撞：`tests/test_ai_openapi.py::test_document_counts_and_schema_identity`。

**4）MCP 侧车** — 通常零改动

- 工具表来自契约（`mcp_server.py` 里 `import ai_contract as C`），加 `--toolsets` 就能选到。
- 只有属于「可等待」「返回图片」「需要额外 next_actions」这三类，才把名字补进
  `mcp_server.py` 对应的元组里。
- 漏了会撞：`tests/test_mcp_server.py`、`CivitaiFreeToolMCP.exe --selftest` 的工具清单。

**5）状态与事件（按需）**

- 要让 AI 感知新状态：字段加进 `browser_bridge.automation_state()`，事件在
  `automation_events.py` 的 `_collect` / `_diff` 里发出；**事件必须先在
  `ai_contract.EVENTS` 登记**，否则 `publish()` 直接丢弃且不报错。
- 新增会挡路的弹层：必须在 [`web/ai-overlays.js`](../web/ai-overlays.js)
  登记安全关闭回调，并在注释里写明关闭语义（取消 / 继续编辑 / 仅收起）；不登记就只能靠
  `.rd-mask` 兜底扫描，`cft_dismiss_ui` 无法给出准确 `blockers` 动作。

**6）数量同步** — 加/删工具会波及这些写死数字的地方

- 测试：`tests/test_ai_contract.py`（19 / 24 / 24 / 29）、`tests/test_ai_openapi.py` 的 `expected`。
- 文档：`docs/ai-integration.md` §2、`docs/releases/v2.7.0.md` 工具集表、`CHANGELOG.md`、
  `README.md` 的 AI 条目、`skills/civitai-free-tool/SKILL.md`。
- `skills/civitai-free-tool/references/tools.md` 是生成的，不要手改。

**7）Skill 参考生成与打包验证**

```bash
python skills/civitai-free-tool/scripts/gen_skill_refs.py          # 重新生成 tools.md
python skills/civitai-free-tool/scripts/gen_skill_refs.py --check   # 只校验
python -m pytest -q
python -m PyInstaller --noconfirm CivitaiFreeToolWeb.spec
python -m PyInstaller --noconfirm CivitaiFreeToolMCP.spec
```

然后在 `.tools/` 的隔离目录里跑打包 EXE（别在用户数据目录上验）：

- `python scripts/smoke_agent_transports.py`（OpenAPI + 别名 + SSE 只读冒烟）
- `python skills/civitai-free-tool/scripts/cft_doctor.py --mcp <隔离目录>/CivitaiFreeToolMCP.exe`
- 改了前端必查 `GET /app.js` 与 `web/app.js` 逐字节一致，并核对 EXE 的 FileVersion
- Skill 有改动就重打 `dist/civitai-free-tool-skill-*.zip`，排除 `__pycache__`

**已踩过的漏点**：只改契约没补 `execute_tool`；图片工具忘了进 `LARGE_BODY_TOOLS` 被 1MB 拦成 413；
事件没登记进 `EVENTS` 被静默丢弃；改了前端只跑源码测试、没重打包，EXE 仍是旧产物。
