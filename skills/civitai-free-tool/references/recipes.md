# Recipes

端到端调用序列。所有工具名在 MCP 与 HTTP 上一致；HTTP 回退时把每个调用写成
`POST /api/automation/tool {"name":"<tool>","arguments":{...}}`。每条 recipe 的“不要做”是硬约束。

## 0. 界面被弹层挡住

```text
cft_get_state {"compact": true}
# blockers[].code 为 onboarding_guide 或 ui_overlay 时：
cft_dismiss_ui {"targets": ["all"], "permanent": true}
# 只想关引导、保留业务确认框：
cft_dismiss_ui {"targets": ["guides"], "permanent": true}
```

预期：当前弹层立即收起；`onboarding_done=true`、`auto_guides=false`，刷新后不再自动弹。
关闭确认框等于取消，不会执行危险操作；下载文件选择关闭等于取消本次下载；设置未保存关闭等于
继续编辑。不要尝试点 X、填假 Key 或逼用户完成引导；人类需要引导，AI 不需要。

## 1. URL 下载（已存在则校验，不重下）

目标：给定 Civitai URL，落到配置目录并确认可用。

```text
cft_resolve_model {"url": "<civitai-url>"}
```

预期：`ok=true`，返回 model/version、`files[]`（含 `exists`、`queued`、`in_library`、落点/分流原因）。只读，不下载。

```text
# 已存在的文件逐个校验（path = files[].routed_dir + files[].name）
cft_verify_model {"path": "<absolute-model-path>", "hash": false}

# 不存在的文件才下载；多文件版本用 file_ids 只下缺的
cft_download_model {"url": "<civitai-url>", "file_ids": [<missing-file-id>], "wait": true, "verify": true}

# 已在队列
cft_list_tasks {"status": ["pending", "downloading"], "wait": true, "timeout_s": 300}
```

预期：下载返回 `submit`/`task`/`verify`；`verify.ok=true` 且 `verify.bundle_ok=true` 即完成。
返回 `skipped="exists"` 表示没有重下（`task` 为 null）。`timed_out=true` 不是失败，继续用
`cft_list_tasks {"ids": ["<task-id>"], "wait": true}`。

**不要做**：不要因为“想确保最新”就加 `force=true`；不要在 `exists=true` 时重下；不要把
`timed_out=true` 当失败；不要在未看 `bundle_ok` 时宣布“完全成功”。

## 1b. 从已打开标签页下载（不截图、不模拟键鼠）

目标：用户说“那个页面已经打开了”，先搜浏览器扩展上报的 C 站模型页索引。

```text
cft_browser_tabs {"query": "wallpaper", "limit": 20}
```

预期：`items[]` 每行有 `title/url/modelId/versionId/tabId/windowId`；`stale=false` 表示索引在
150s 内更新。`stale=true` 时不要把“没搜到”当成“页面不存在”，提示用户切换/重载扩展后再试。

```text
cft_download_model {"url": "<items[0].url>", "wait": true, "verify": true}
```

**不要做**：不要把 `stale=true` 当“没有标签页”；不要用截图/模拟键鼠逐个切标签；不要读取
网页正文/Cookie。索引只覆盖 C 站模型详情页，图片页、帖子页、列表页不在其中。

## 1c. 落点不是 routed：按 hint 修，不搬文件

`cft_resolve_model` / `cft_download_model` 的每个文件都有 `placement`：

```text
placement.status = routed | under_root | outside_root | pending
placement.hint   = 可直接执行的方向
```

- `routed`：已在 `<comfyui_models_root>/<类型key>/...`，正常。
- `under_root`：在模型根但没进类型目录。先 `cft_diagnostics {"source":"storage"}` 看
  `suggested_root`，用户确认后 `cft_configure {"action":"set","config":{"comfyui_models_root":"...","route_by_type":true}}`；
  不想改全局配置时，下次下载传 `dest_subdir`。
- `outside_root`：不在任何已配置模型根内；先核对 `models_dir` / `comfyui_models_root`，再决定。
- `pending`：任务还没解析落点；用 `cft_list_tasks` 等终态后再看。

**不要做**：不要用 `cft_manage_files` / 自己搬动已下载文件来“修正”落点；不要新建未被
ComfyUI 注册的类型目录。placement 只读，修复配置用 `cft_configure`。

## 1d. MCP 接入（客户端侧）

冻结版桌面端会在 47532 自动起 MCP HTTP；客户端也可用 stdio 自己拉起侧车：

```bash
python scripts/mcp_setup.py --client cursor --apply                 # stdio
python scripts/mcp_setup.py --client claude --transport http --apply
```

脚本先备份、保留其它 server；HTTP token 只写客户端配置，不回显。连不上时先看
`cft_capabilities` 的 `mcp.running` / `extension.online`，再跑 `cft_doctor.py`。

## 2. 搜索、选版本、再下载

目标：用户只给名字时，先展示候选，再下用户选中的版本。

```text
cft_search_models {"query": "<name>", "types": "LORA", "base_models": "<optional>", "limit": 10}
```

预期：候选列表（id/type/base model/统计/版本）。把候选和差异展示给用户；不要替用户猜。

```text
cft_resolve_model {"model_id": <chosen-model-id>}
cft_resolve_model {"model_id": <model-id>, "version_id": <chosen-version-id>}
cft_download_model {"model_id": <model-id>, "version_id": <version-id>, "wait": true, "verify": true}
```

预期：第一个调用列出全部版本；第二个列出该版本文件、落点、`exists`/`queued`。若该版本有多个文件，
先展示 `file_id`/文件名/体积，再用 `file_ids` 只下需要的文件，或让用户走 recipe 6 选择：

```text
cft_download_model {"model_id": <id>, "version_id": <id>, "file_ids": [<file-id>], "wait": true, "verify": true}
```

**不要做**：不要默认选第一个版本；不要在未询问时打开 `nsfw=true`；不要一次下载所有版本；
不要用 `urls` 与 `file_ids` 混用来绕过版本选择。

## 3. 失败 / 收尾不完整：先 verify，再 repair

目标：下载任务显示完成但 sidecar/封面缺失，或任务报错；修好而不是重下。

```text
cft_list_tasks {"ids": ["<task-id>"], "fields": ["id", "status", "postprocess", "postprocess_errors", "error", "info.routed_dir"]}
cft_verify_model {"task_id": "<task-id>", "hash": false}
```

预期：任务行给出 `status`、`postprocess.done`、`postprocess_errors[]`；verify 给出 `ok`（模型本体可用）、
`bundle_ok`（配置要求的附属文件齐全）、`sidecars.expected_missing`。

```text
cft_repair_model {"task_id": "<task-id>", "stages": ["metadata", "comments", "cover"], "wait": true}
cft_verify_model {"task_id": "<task-id>", "hash": false}
```

预期：repair 只对“已完成且文件仍在”的任务生效，幂等；返回 `postprocess_errors=[]`，第二次 verify
`bundle_ok=true`。若 `postprocess_errors` 仍非空，读 `cft_diagnostics {"source":"logs","tail":120}` 再决定。

**不要做**：不要用 `force=true` 重下代替 repair；不要删模型文件或 `.part`；不要在任务未完成时 repair；
不要把缺少评论文件（未配 API Key）误判成模型损坏；不要对 `task_failed` 立刻盲目 retry。

## 4. 模型库审计（先只读，破坏性另行确认）

目标：盘点本地库、查重、查更新，输出可执行建议。

```text
cft_list_models {"limit": 50, "fields": ["path", "name", "type", "ver", "verId", "modelId"]}
cft_manage_library {"op": "verify", "wait": true, "timeout_s": 300}
cft_manage_library {"op": "dedupe", "wait": true, "timeout_s": 300}
cft_manage_library {"op": "check-updates", "wait": true, "timeout_s": 300}
```

预期：`verify` 校验哈希并补全 C 站元数据（会发网络请求，可能写 sidecar，但不删文件）；`dedupe` 只计算
重复，结果在 `progress.result`，不删除；`check-updates` 只查询更新（`force=true` 才绕过 24h 缓存）。
把重复组、更新列表、缺失 sidecar 汇总给用户。

只有用户明确确认后才可继续：

```text
# 先展示 dedupe 的重复组与将删除的精确路径
cft_manage_library {"op": "dedupe-delete", "paths": ["<duplicate-path>"], "confirm": true, "wait": true}

# cleanup 不可恢复：先列出会丢的元数据/封面/示例图，再逐路径确认
cft_manage_library {"op": "cleanup", "paths": ["<model-path>"], "confirm": true, "wait": true}
```

**不要做**：不要自动执行 `dedupe-delete` 或 `cleanup`；不要用“全部路径”做批量删除；不要把 `update`
（会入下载队列）、`organize`/`rename-c`（会改文件位置/名字）、`localize`/`translate-descs`（会改文件名/简介）
当成只读；任何一项都要先说明影响并取得确认。

## 5. 画廊看图打标（文本模型走元数据，多模态走图片）

目标：筛选 ComfyUI 输出，选图，打星/打标。

```text
cft_browse_gallery {"kind": "image", "starred": false, "limit": 50, "with_facets": true, "fields": ["id", "filename", "width", "height", "starred"]}
```

预期：`items[]`（id/元数据/标签/星标）与 `facets`。文本模型到此为止：只根据元数据与用户描述筛选，
需要提示词时用 `cft_analyze_workflow {"path": "<item-path>", "include_image": false}`。

多模态模型继续：

```text
cft_contact_sheet {"source": "gallery", "ids": [<ids>], "limit": 16, "cols": 4, "cell": 256, "delivery": "inline"}
cft_view_gallery {"ids": [<selected-1-8>], "delivery": "inline", "include_meta": true, "max_side": 800}
```

预期：contact sheet 返回一张带标签的网格和 index→id/path 映射；据此选 1–8 个 id 再看大图。
用户确认后打标：

```text
cft_annotate_gallery {"op": "star", "ids": [<ids>], "on": true}
cft_annotate_gallery {"op": "tag-add", "ids": [<ids>], "tag_ids": [<tag-id>]}
# 需要新标签时先建：cft_annotate_gallery {"op":"tag-save","id":0,"name":"精选","color":"#888888","parent_id":-1}
```

**不要做**：不要在未展示/未确认时批量打标；不要根据文件名断言画面内容；文本模型不要请求 inline 图片；
不要用 `cft_delete_gallery`（admin，回收站）代替取消星标；不要删除媒体同名的 `.json` sidecar。

## 6. 文件选择阻塞：get_state → answer_file_choice

目标：多文件版本暂停等待选择时，恢复下载。

```text
cft_get_state {"compact": true}
```

预期：`pending_file_choice` 含 `id`（identity）与 `files[]`（每项 `index`/名称/体积）。把文件与体积展示给用户；
如果用户没明确选，先问，不要猜。

```text
cft_answer_file_choice {"identity": "<pending_file_choice.id>", "indices": [<index>, ...]}
cft_list_tasks {"ids": ["<task-id>"], "wait": true, "timeout_s": 300}
```

预期：选择被接受，任务继续/恢复；`pause=true` 保持暂停，`ack=true` 仅表示“已展示”，不选文件。
若没有待选，工具返回 `invalid_argument`/`not_found` 类错误，按 `hint` 处理。

**不要做**：不要伪造或复用旧 `identity`；不要在用户没看到选项时发 `ack=true`；不要默认勾选全部大文件；
不要把 `pause=true` 当成取消任务。

## 7. 诊断 app 不通 / 代理 / DNS

目标：区分“app 没起”“MCP 没配”“网络/代理问题”“版本不匹配”。

```text
cft_capabilities {"include_tools": false}
```

MCP 调用失败时，走 HTTP：

```text
GET http://127.0.0.1:47531/api/health
GET http://127.0.0.1:47531/api/automation/ai
python scripts/cft_doctor.py --mcp "<path-to-CivitaiFreeToolMCP.exe>" --token-env CFT_MCP_TOKEN
```

MCP 可用时：

```text
cft_diagnostics {"source": "all", "tail": 120}
```

预期：`connectivity.dns`（含 `suspicious`）、`api` 耗时、`proxy` 与 `system_proxy` 对比、`warnings`、存储/扩展状态。
按 warning 行动：系统代理开着但 app 没开 → 在 app 里启用同地址代理（`cft_configure` 属 admin，先确认）；
DNS 污染 → 换 DNS/网络；`app_not_running` → 启动桌面端；`version_mismatch` → 升级 app/MCP。

**不要做**：不要把 47531/47532 暴露到局域网或公网；不要关闭防火墙/改系统代理来“试试”；不要在日志、
配置片段或回答里写 API Key/token；不要用 `cft_configure` 改路径/代理而未取得确认。

## 8. OpenAPI 导入与 SSE 订阅（MCP 之外的本地 Agent 面）

### 8.1 OpenAPI

```text
GET http://127.0.0.1:47531/api/automation/openapi.json?profile=text
POST http://127.0.0.1:47531/api/automation/tool/cft_get_state
Body: {"compact": true}
```

- `profile`：`text` 19 / `vision` 24 / `admin` 24 / `all` 29；默认 text。
- 每个 operation 是一个具体 path：`POST /api/automation/tool/{cft_name}`；body 只放该工具的
  arguments，不要包 `{name,arguments}`。
- 图片工具可用 `?content_format=openai|anthropic|mcp`；默认 neutral。
- OpenAPI 的 profile 只控制“文档里看得见哪些工具”，执行权限和 `confirm:true` 不因 profile 改变；
  admin/destructive 仍必须先向用户确认。

**不要做**：不要把云端平台无法访问的 `127.0.0.1` 当成可公网导入；不要为绕过 Docker 网络限制
把桥暴露到局域网。

### 8.2 SSE 事件流

```bash
curl -N "http://127.0.0.1:47531/api/automation/events/stream?types=task,file_choice,ui"
```

```text
id: 7
event: file_choice.pending
data: {"seq":7,"instance_id":"...","type":"file_choice.pending",...,"data":{"id":"...","files_count":3}}

: keep-alive ...
```

事件目录共 21 类，权威清单读 `GET /api/automation/ai` 的 `caps.events` 或 OpenAPI 的
`x-cft-events`；`types=` 可填事件名或组名（`task` / `file_choice` / `scan` / `ui` / `config`），
重放同样按 `types` 过滤，`api.call` 只在 `types=all` 时实时出现、不重放。

标准流程：

1. 保存每条事件的 `id`（seq）。
2. 收到 `state.snapshot` 后以它为基线；不要重放期间重复发起下载。
3. 任务完成用 `cft_verify_model` 复核；收到 `file_choice.pending` 用 `cft_get_state` 看选项再回答；
   收到 `ui.overlays.changed` 且阻塞时调用 `cft_dismiss_ui`。
4. 断线请求带 `Last-Event-ID: <最后 seq>`。看到 `stream.reset` 表示环形缓冲已过期，重新调用
   `cft_get_state` 对齐。
5. 看到 `stream.overflow` 说明队列曾满，可能有事件丢失，同样重新读 state。

**不要做**：不要为了“更快”开多个订阅；最多 8 个客户端。不要把事件当成完成证明，仍需
`wait/verify`；不要把事件里的路径原样展示给用户前做存在性检查。

## 9. CLI 与 webhook（本机脚本 / 事件外推）

### 9.1 CLI

```bash
CivitaiFreeToolCLI.exe doctor
CivitaiFreeToolCLI.exe call cft_capabilities --json
CivitaiFreeToolCLI.exe call cft_search_models --arg query=seedream --arg limit=5
CivitaiFreeToolCLI.exe call cft_download_model --args '{"url":"...","wait":true}'
CivitaiFreeToolCLI.exe events --types task,file_choice --json
CivitaiFreeToolCLI.exe call cft_view_gallery --args '{"ids":[1,2]}' --save-content .\out
```

- 只连 `127.0.0.1/localhost/::1`，不自动启动桌面端；app 不在线退出码 3。
- 退出码：0 成功 / 2 用法 / 3 app 未运行 / 4 参数 / 5 未找到 / 6 需确认 / 7 限流 / 8 超时 / 9 版本。
- `events` 默认跟随并断线重连；`--max N` 或 `--idle-timeout S` 收口；`--json` 是 NDJSON。
- 需要图片时 `--save-content DIR` 落盘，stdout 不喷 base64。

### 9.2 webhook（先得到用户同意）

webhook 会把事件（文件名、落点、弹层、配置键名）发到用户指定的地址，属于数据出境选择。
**先问用户目标地址是否可信、是否允许公网**，再配置：

```text
cft_configure {"action":"set","config":{
  "webhook_enabled":true,
  "webhook_url":"http://127.0.0.1:8799/cft-hook",
  "webhook_secret":"<至少 16 字符的随机串>",
  "webhook_events":["task","file_choice","ui"],
  "webhook_allow_remote":false
}}
```

然后：

```bash
curl -s http://127.0.0.1:47531/api/automation/webhooks
curl -s -X POST http://127.0.0.1:47531/api/automation/webhooks \
  -H 'Content-Type: application/json' -d '{"action":"test"}'
```

- 接收端校验 `X-CFT-Signature: sha256=HMAC(secret, "<timestamp>.<raw body>")`，拒绝时间偏差
  >300s，并按 `X-CFT-Delivery` 去重；`webhook.test` 探针带 `X-CFT-Synthetic: 1`。
- 重试 4 次（1s/5s/15s），不跟随 3xx；队列满/应用退出会丢事件，`stats.dropped` 如实记录。
- **不要**把 `webhook_secret` 写进回答/日志/仓库；不要在用户未明确同意时设
  `webhook_allow_remote=true`；不要把 webhook 当成可靠队列（需要重放用 SSE）。

