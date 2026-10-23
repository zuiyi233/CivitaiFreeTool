# 桌面桥自动化接口（AI / 脚本调试用）

> 目的：把「必须点界面/看截图/自己轮询」的事情变成一条 HTTP 调用，减少人工与 token 消耗。
> 全部接口只监听 `127.0.0.1:47531`；无 `Origin` 的本机脚本放行，浏览器来源仍按原白名单校验。
> 实现分两层：语义化端点（本文）与长尾方法（`POST /api/rpc`，160+ 个，见 `/api/rpc/methods`）。

## 0. 先读能力清单

```bash
curl http://127.0.0.1:47531/api/automation
```

返回端点表、可写配置键、任务状态词表、数据目录、RPC 方法数量。**这是给 AI 的自描述入口**，
字段与本文一致；新增端点会同步出现在这里。

### 一次调用做多件事：`POST /api/automation/batch`

```json
{"calls": [
  {"method": "GET", "path": "/api/automation/tasks",
   "query": {"limit": 5, "fields": "id,status,progress"}},
  {"method": "POST", "path": "/api/automation/plan", "body": {"url": "..."}}
 ],
 "stop_on_error": false}
```

- 最多 50 条，**按顺序**执行；只允许本层端点（不能嵌套 batch，也不能改走 `/api/rpc`）。
- **batch 本身永远返回 200**：成败看 `results[].ok` 与 `results[].http_status`。
  这样一条失败不会让整个请求看起来「请求都没到」。
- `stop_on_error=true` 时遇到第一个 `http_status >= 400` 就停，`executed < requested`。
- 典型用法：`state + tasks + storage` 合成一次请求；或「plan → download → verify」串成一条。

### 只回你要的字段：`fields=`

所有列表/详情端点都接受 `fields=id,status,info.routed_dir`（逗号分隔，支持点号路径）：

- 列表端点（`tasks` / `items` / `rows` / `images` / `files`）只裁剪条目，
  `ok` / `count` / `waited_s` / `timed_out` 等元信息永远保留；
- 非列表端点保留 `ok` + 你点名的字段（`model` 详情可用 `detail.name` 这种路径）；
- `fields=*` 或不传 = 不裁剪。

存在的理由是省 token：`/library?limit=200` 的完整行里有大量 AI 用不上的键。

### 失败一定带下一步：`hint`

自动化层的错误响应统一为 `{ok:false, msg, hint?}`：`msg` 说清哪里不对，
`hint` 说清下一步该做什么（换参数 / 看日志 / 先跑预览）。AI 不需要靠猜来决定重试还是放弃。

### `GET /api/automation/schema`（自描述）

AI 不必猜配置值形状、也不必读源码：

- `config`：每个可写键的 `type`（bool/int/str/path/enum）、`options`、`default`、`current`
  （密钥显示 `***`）、`desc`。
- `task_states` / `task_actions` / `caps`：词表。
- `library_ops`：模型管理动作清单（见 §5）。
- `routing`：`core_folders` 与 `type_to_folder`（Civitai 类型 → ComfyUI 目录，**唯一事实源**
  是 `model_routing`，这里是它的投影）。
- `sidecars`：落点校验期望的附属文件与「算封面的后缀集合」。

### AI 工具层（MCP / HTTP 共用）

唯一契约在 `ai_contract.py`，完整说明见 [`ai-integration.md`](ai-integration.md)。

- `GET /api/automation/ai`：版本、toolsets、限额、guide、MCP 提示；`caps` 里同时给出
  `openapi` / `sse` 与事件名，客户端不必猜。
- `GET /api/automation/tools?format=openai|anthropic|mcp&profile=text|vision|admin|all`：
  从契约生成工具 schema；`text` 19 个、`vision` 24 个、`admin` 24 个、`all` 29 个。
- `GET /api/automation/openapi.json?profile=text|vision|admin|all`：由契约运行时生成的
  OpenAPI 3.1 文档；默认 `text`（19 个操作），每个工具对应
  `POST /api/automation/tool/{name}`。可直接导入 Dify / n8n / LangChain / Open WebUI 等
  本地 Agent 平台；`servers` 只接受回环 Host，不生成指向任意主机的地址。
- `POST /api/automation/tool/{name}`：OpenAPI 导入器使用的每工具入口，body 直接是
  arguments 对象（不是 `{name,arguments}` 包装），内部与 `/tool` 走同一条执行链；
  `?content_format=` 控制图片内容块。未知工具 404，GET 返回 405。
- `GET /api/ext/tabs?q=...&limit=...`：浏览器扩展上报的已打开 C 站模型页只读索引
  （只存内存，只含标题/URL/模型版本 ID）；`cft_browser_tabs` 工具封装此接口。
  命中后用 `cft_download_model` 下载。契约与安全边界见
  [`browser-tabs-bridge.md`](browser-tabs-bridge.md)。
- `POST /api/automation/tool`：`{"name":"cft_*","arguments":{...}}` 统一执行；
  `content_format=neutral|openai|anthropic|mcp` 控制视觉内容块。
- `GET /api/automation/guide?profile=text|vision`：给模型的紧凑 runbook。
- `POST /api/automation/ui/dismiss`：AI 一键关闭所有挡路弹层——新手引导、API Key 提示、
  反向解析教程、确认框、下载文件选择、下载完成移动询问、设置未保存、受限提示、结果窗、
  详情抽屉、图片查看器。`targets` 可写 `all` / `guides` / `dialogs` 或具体名字。
  **关闭确认框=取消，绝不自动确认危险操作**；关闭下载文件选择=取消本次下载；
  关闭设置未保存=继续编辑。默认 `permanent=true` 只对引导生效（写 `onboarding_done` /
  `auto_guides=false`），设置页手动入口仍可用。
  MCP 对应工具 `cft_dismiss_ui`；`state.ui.guides.overlays[]` 会列出当前可见弹层，
  有阻塞项时直接调用即可。
- 视觉端点：`/vision/model`、`/vision/gallery`、`/vision/workflow`、
  `/vision/contact-sheet`、`/vision/analyze`。视频只回首帧 JPEG；`delivery=paths` 只回路径，
  `inline` 回图片内容；默认 `max_side=768–1024`、最多 8 张。
- 现有详情端点加 `mode=text`：`/model` 与 `/gallery/item` 剥掉内联 base64，
  纯文本模型不会拿到图片 token；`/workflow/analyze` 加 `include_image=1` 才回缩略预览。
- 所有错误现在都带 `code`、`retryable`、`hint`；核心工具成功响应带 `next_actions`。

MCP 侧车是同一个契约的 stdio / Streamable HTTP 入口：`CivitaiFreeToolMCP.exe`。
Skill 在 `skills/civitai-free-tool/`，负责安装、recipes 和安全边界。

## 1. 观测

### `GET /api/automation/state`
版本、关键配置、`config_rev`、最近 20 条任务（含 `routed_dir` / `route_reason` /
`postprocess` / `postprocess_errors`）、待选文件、界面状态（`ui.guides` 给出新手引导是否会弹、
`auto_guides`、`dismiss_rev` / `dismiss_targets`，以及前端上报的 `overlays[]`：
名称/标题/是否阻塞/上报时间）。

### `GET /api/automation/tasks`
参数：`ids`(csv)、`status`(csv)、`limit`、`since`(ms)、`wait=1`、`timeout`(秒，默认 300，上限 1800)。

- 不传 `wait`：立即返回快照。
- `wait=1` + `ids`：阻塞到这些任务全部进入终态（done/error/canceled）。
- `wait=1` 无 `ids`：阻塞到队列里没有 pending/downloading。
- 返回 `{ok, tasks, count, waited_s, timed_out}` —— `timed_out=true` 表示到点仍未终态，
  不是失败，按需再查。
- 每行带完整性结论 `expected_sha256 / actual_sha256 / verification_status`
  （`verified` / `mismatch` / `unverified`）：只有 `hash_mismatch` 分不清「校验通过」与「根本没校验」，
  而 `unverified` 是如实状态，不是失败。

### `GET /api/automation/extension`
浏览器扩展通道状态：`download_requests`、`archive_files_written`、`archive_done_calls`、
最近一次提交的 `origin/caps/url/age_s`、当前有效 `active_bundle_tokens`。
用来回答「插件到底连没连上、有没有真的提交」。

### `GET /api/automation/config`
全量配置（`api_key` / `baidu_key` / `baidu_appid` 打码）+ 数据目录信息 + 可写键列表。

### `GET /api/automation/logs`
参数：`source=error|events|both`（默认 both）、`tail`（默认 80）。
`error_log` 给 `error.log` 尾部；`events` 是本层最近 200 次调用（方法/路径/状态码/耗时），
用来确认「我的请求到底到没到、耗时多少」。

### `GET /api/automation/events`
增量事件流：`since`（上次返回的 `next_since`）、`limit`。每条事件带自增 `seq`。
比 `logs?source=events` 更适合轮询——只拿新增的那几条，不重复读全量。

### `GET /api/automation/events/stream`（SSE）
领域事件推送，把「AI 每秒轮询 state」变成「弹层/任务/扫描一有变化就收到」：

- 响应：`text/event-stream`，标准 `event:` / `id:` / `data:`，每 15 秒一条 `: keep-alive` 注释心跳。
- 参数：`since=<seq>`（或标准 `Last-Event-ID` header）、`types=<csv 事件名/组名|all>`、
  `snapshot=0|1`（默认 1：新连接先发一条 `state.snapshot` 作为基线；重连且无缺口时不重发）。
- 事件目录由 `ai_contract.EVENTS` 生成，可通过 `/api/automation/ai` 的 `caps.events` 或
  `/api/automation/openapi.json` 的 `x-cft-events` 读到；默认订阅全部领域事件，
  `api.call` 仅在 `types=all` 时实时推送，不参与历史重放。
- 事件源是 1 秒快照差分（任务、待选文件、模型/资源库扫描、可见弹层、配置版本），
  不向下载器埋点；因此事件最多延迟约 1 秒，与前端轮询同一量级。
  待选文件按变更日志（`state.file_choice_transitions` + `file_choice_rev`）读取，
  一闪而过的 pending→resolved 也不会漏；瞬时完成的补 `created`+`finished`，
  两次采样之间跑完的扫描补 `*.finished`。
- 可靠性：环形缓冲 500 条；`Last-Event-ID` 在缓冲内按序补发（补发同样只给 `types` 订阅的类别），
  超出先发 `stream.reset` 再转实时；每客户端队列 256，满时丢最旧并补一条 `stream.overflow`；
  最多 8 个并发订阅，超出返回 429 + `hint`。
  最后一个订阅者断开后采样线程保留 30 秒宽限期，宽限期内重连可继续重放；服务退出走
  `shutdown()`，`app.stopping` 尽力发送，被强杀时可能缺席——以连接关闭为准。
- 安全：只监听 `127.0.0.1`；事件只带配置键名，绝不带 `api_key` / 翻译密钥 / token 值；
  `config.proxy_address` 里的 `user:pass@` 在状态层就脱敏。

```bash
curl -N "http://127.0.0.1:47531/api/automation/events/stream?types=task,ui,file_choice"
```

### 出站 webhook：`GET/POST /api/automation/webhooks`

把同一批契约事件以 HTTP POST 推给用户配置的端点，适合 n8n/zapier/自建接收器。
**默认关闭**；不新增事件类型，仍投影 `ai_contract.EVENTS`（默认排除高频 `api.call`）。

配置键（与其它白名单键一样走 `POST /api/automation/config` 或 `cft_configure`）：

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `webhook_enabled` | bool | false | 总开关 |
| `webhook_url` | str | "" | http(s) 端点；回显只露 scheme+host+port（路径可能含平台 token） |
| `webhook_events` | list | [] | 事件名/组名/`all`；空 = `task/file_choice/scan/ui/config/app` |
| `webhook_secret` | str | "" | HMAC-SHA256 密钥；启用时至少 16 字符，回显恒为 `***` |
| `webhook_allow_remote` | bool | false | 允许公网目标；默认只允许本机/局域网 |

`GET /api/automation/webhooks` 返回启停、脱敏目标、事件过滤、统计（accepted/sent/failed/
dropped/retried）、最近 50 条投递与 `config_error`。`POST` 的 `action`：

- `test`：发一条 `webhook.test` 合成探针（带 `X-CFT-Synthetic: 1`），默认等到投递结束；
- `clear-log`：清空投递日志，累计统计保留；
- `refresh`：从当前配置重新加载（手工改配置文件/长尾 RPC 后使用）。

`GET /api/automation/webhooks` 只读：不会重新做 DNS/配置校验，也不会停用投递或丢队列；
配置保存（`POST /api/automation/config`）会立即刷新，手工改文件后显式调 `refresh`。

投递请求带 `X-CFT-Delivery/Event/Group/Seq/Instance/Timestamp/Signature`；签名是
`HMAC-SHA256(secret, "<timestamp>.<raw body>")`，接收端应校验时间窗（建议 300 秒）并按
`X-CFT-Delivery` 去重。重试 4 次（1s/5s/15s），只对网络错误、408/425/429/5xx 重试；
**不跟随 3xx**；队列 256，满时丢新事件并计入 `dropped`；不持久化、不跨重启重放。
完整契约与 Python/Node 校验示例见 [ai-cli-webhook.md](ai-cli-webhook.md)。

```bash
curl -s http://127.0.0.1:47531/api/automation/webhooks
curl -s -X POST http://127.0.0.1:47531/api/automation/webhooks \
  -H 'Content-Type: application/json' -d '{"action":"test"}'
```

### `GET /api/automation/history`
下载历史（已完成/失败/取消，倒序）。参数：`limit`、`status`、`detail=1`。
默认返回精简字段（id/filename/status/dest_dir/routed_dir/file_path/file_exists/total/error/finished_at）；
`detail=1` 返回原始行。用于回答「上次那个下完了吗、文件还在不在」。

### `POST /api/automation/history`
清理历史记录。**只删记录与历史缓存（缩略图/信息快照），绝不删模型文件**——与「删除模型」正交。

```json
{"action": "remove", "ids": ["1792..."]}
{"action": "clear"}                     // 清空全部
{"action": "remove", "ids": ["..."], "preview": true}   // 只回报将删哪些，不落盘
```

- 返回 `{ok, removed:[id], missing:[id], skipped:[{id,reason}], tasks_removed:[id],
  assets_removed:int, parts_cleaned:int, msg}`。
- **会同时摘掉队列里同 id 的终态任务**：历史行在每次保存时由队列里的终态任务重写
  （`_record_history`），只删记录不摘队列行，下一次保存就会「复活」。`tasks_removed` 如实告知；
  `_record_history` 另有墓碑集合，拦住「删除瞬间还在路上的 `_notify`」把同一行写回。
- 删除失败/中断记录的 `.part`（续传数据）会一并清理（`parts_cleaned`）：记录没了，续传上下文
  也就没了，留着只是磁盘垃圾。清理沿用既有的归属校验，不会碰其他任务正在写的临时文件。
- 仍在下载/等待中的任务不算历史记录（`GET /history` 本来就过滤掉），遇到会进 `skipped` 并说明原因。
- 幂等：不存在的 id 进 `missing`、不报错；`ids` 为空或 `action` 非法返回 400 + `hint`；
  `clear` 在历史本来就为空时返回 `ok:true, removed:[]`（不是错误）。
- 界面等价入口：下载管理 → 下载历史 → 每行「删除」/ 工具栏「清空历史」。

### `GET /api/automation/prompts`
参数：`path`（模型文件绝对路径）、`limit`。直接读 `.civitai.info` 的 `images[].meta`，返回
`{index, type, url, width, height, nsfw, prompt, negative_prompt, params:{steps, sampler, seed, ...}}`。
AI 复现示例图时不用再手撸元数据解析。没有 info 时给 `hint` 指向 `library/op {op:"verify"}`。

### `GET /api/automation/connectivity`
连通性体检，一条请求区分「网络问题」与「代码问题」：

- `dns`：`civitai.red`、`b2.civitai.com`（模型文件 CDN）、`image.civitai.com` 的解析结果，
  带 `suspicious`（命中 Facebook 网段 / `face:b00c` 等污染特征）。
- `api`：公开 API 基址与实际请求耗时（跟随 `site_domain`）。
- `proxy`：应用配置的代理；`system_proxy`：Windows 系统代理（WinINET）。
- `warnings`：**系统代理开着但应用没开**、疑似 DNS 污染等可直接行动的提示。

> 实测教训：浏览器走系统代理、Python/urllib 默认不走。系统代理开着而应用没开时，
> 表现是「浏览器能下、桌面端一律 10060 超时」，很容易被误判成代码 bug。

### `GET /api/automation/library`
参数：`q`、`type`、`limit`、`offset`。返回已解析成对象的模型行（path/name/type/ver/verId/modelId…）
与当前扫描状态。底层 `get_scan_rows()` 原本返回 JSON 字符串，这里已解析。

### `GET /api/automation/model?path=…`
单个模型的完整详情（info + 画廊，底层 `get_model_detail` 的 JSON 字符串已解析）+
该文件/目录的附属文件清单（`.civitai.info`/`.json`/`.preview.png`/`_评论.txt`/`.images`/
扩展产物）。文件不存在返回 404。

### 资源库（ComfyUI output 媒体库）

把 ComfyUI 输出目录里的图片/视频当成可检索、可打标、可回收的媒体库。底层即 `webui.py` 的
`lib_*` 方法；筛选字段、行形状、分面与写操作语义的完整定义见
[`media-library-contract.md`](media-library-contract.md) **§5.2**。

#### `GET /api/automation/gallery`
参数：`filter`（JSON 对象字符串，默认 `{}`）、`limit`（默认 50，合法 1–500，越界 400）、
`offset`（默认 0，>=0）、`with_facets=0|1`（默认 0）、`fields`。

`filter` 的字段（`kind`/`starred`/`q`/`checkpoints`/`loras`/`dirs`/`tag_ids`/`sort`/
`group_by` 等）见 media-library-contract §5.2。**查询参数会覆盖 filter 里的
`limit`/`offset`/`with_facets`**；`root_ids` 由底层按配置的媒体根强制注入，filter 里写也越不了权。
`with_facets=1` 才返回 `facets`，底层给 `None` 时归一成 `{}`。

```bash
curl -G "http://127.0.0.1:47531/api/automation/gallery" \
  --data-urlencode 'filter={"kind":"image","starred":true,"sort":"time_desc"}' \
  --data-urlencode 'limit=20' --data-urlencode 'offset=0' --data-urlencode 'with_facets=1'
```

返回 `{ok,count,total,offset,limit,items,facets?}`。`fields` 只裁剪 `items`，
`count`/`total`/`offset`/`limit` 等元信息永远保留。filter 不是 JSON 对象 → 400 + `hint`。

#### `GET /api/automation/gallery/item`
参数：`id`（正整数，来自列表 `items[].id`）、`fields`（详情字段用 `item.width` 这种路径）。
返回 `{ok,id,item}`。条目不存在或底层 `ok:false` → 404 + `hint`「先 GET /gallery 取 id」。

```bash
curl "http://127.0.0.1:47531/api/automation/gallery/item?id=12&fields=id,item.filename,item.width"
```

#### `GET /api/automation/gallery/tags`
返回 `{ok,count,tags}`，标签形状为 `{id,name,color,parent_id,count}`（§5.2）。
`fields` 裁剪 `tags` 列表项，例如 `fields=id,name`。

#### `POST /api/automation/gallery/op`
统一写入口，`op` 取 `star` / `tag-add` / `tag-remove` / `tag-save` / `tag-delete` / `delete`，
未知值 400 并列出合法值。`ids` 与 `tag_ids` 必须是正整数 JSON 数组，服务端去重，
单次 1–500（与前端分片一致）。

```json
{"op": "star", "ids": [1, 2], "on": true}
{"op": "tag-add", "ids": [1, 2], "tag_ids": [3]}
{"op": "tag-remove", "ids": [1], "tag_ids": [3]}
{"op": "tag-save", "id": 0, "name": "精选", "color": "#888", "parent_id": -1}
{"op": "tag-delete", "id": 3}
{"op": "delete", "ids": [1, 2], "confirm": true}
```

- `star` 返回 `{ok,op,count,requested}`：`count` 是底层实际更新数，`requested` 是去重后的 id 数。
- `tag-add` / `tag-remove` 原样返回底层结果（`count`/`tags`）并补 `op`。
- `tag-save`：`id=0` 新建，`parent_id=-1` 无父；`name` 为空 → 400 + `hint`。
- `tag-delete`：`{id}` 必填，返回底层 `{ok,removed}`。
- `delete` **必须 `confirm:true`**（缺失/假值 → 400 + `hint`）；逐条调用底层删除，
  返回 `{ok,op:"delete",deleted,failed,results:[{id,ok,msg}]}`——`ok` 表示是否全部成功，
  单条失败不会中断整批。删除是**系统回收站语义**（Windows 回收站 / 其它平台 trash），
  且**只回收媒体文件本体，绝不连带同名 `.json` 等 sidecar**：ComfyUI 的 `.json` 被同批
  图片/视频共享，连删会毁掉另一件的元数据（该保护在底层 `lib_delete`，本层不复制逻辑）。

#### `GET /api/automation/gallery/scan`
返回 `{ok,state}`。`state` 是轻量的扫描进度
（`running`/`phase`/`done`/`total`/`msg`/`roots`/`errors`，不含行数据），形状见 §5.2 的
`lib_scan_state`。

#### `POST /api/automation/gallery/scan`
参数：`wait=0|1`（默认 0）、`timeout`（秒，默认 300，夹在 1–1800）。

```json
{"wait": true, "timeout": 300}
```

调用 `lib_scan()` 后返回 `{ok,started,msg,state,waited_s,timed_out}`。`wait=1` 时每 0.25s
轮询 `lib_scan_state()`，直到 `running=false` 或超时。**已在扫描中时 `started:false`**
（`msg` 说明原因），`wait=1` 仍会继续等本轮结束——调用方要的是「扫完没有」，不是「谁启动的」。
`timed_out=true` 不是失败，按需再查 `GET /gallery/scan`。

### `POST /api/automation/search`
C 站公开模型搜索（只读，`limit` 夹在 1–20，站点根跟随 `site_domain`）。

```json
{"query": "rezero", "types": "LORA", "base_models": "PonyXL", "sort": "Highest Rated",
 "period": "Month", "nsfw": false, "limit": 10}
```

返回 `{ok, total, items:[{id,name,type,nsfw,stats,versions:[{id,name,baseModel}]}]}`。
用于「先找到模型再决定下哪个」，不必让 AI 自己拼 C 站 URL。

## 2. 计划（干跑，不写入）

### `POST /api/automation/plan`

```json
{"url": "https://civitai.red/models/2949009?modelVersionId=3398520", "file_id": null, "probe": false}
```

回答：模型/版本信息、每个文件的名称与体积、**会落进哪个目录**、分流原因、
`exists`（目标文件已在盘上，分流关闭时按 `download_target_dir`/`download_dir` 预判）、
`queued`（已在下载队列）、`in_library`（模型库已有该版本）。每个文件还有只读的
`placement`：

```json
{"dest_dir": "D:/models/loras/demo", "routed_dir": "D:/models/loras/demo",
 "folder": "loras", "expected_folder": "loras",
 "under_models_root": true, "under_configured_root": true,
 "status": "routed", "hint": ""}
```

`status`：`routed`（在 `comfyui_models_root/<类型>/` 下）、`under_root`（在模型根但没进类型目录）、
`outside_root`（不在任何已配置模型根内）、`pending`（下载开始才解析）。
`hint` 给出可执行的修复方向；本接口仍**不建目录、不入队、不下载**。
`probe=true` 才做 safetensors 头嗅探（两次 Range 请求），用于 Checkpoint/Model 这类
`checkpoints` 与 `diffusion_models` 分不清的情况。

### `POST /api/automation/resolve`
参数：`url` 或 `model_id`(+`version_id`)。比 `plan` 多返回 `versions`（全部版本的 id/名称/底模/
发布时间/文件数/主文件体积），用来**先挑版本再下载**。其余字段与 `plan` 相同，同样只读。

## 3. 执行与等待

### `POST /api/automation/download`

```json
{"url": "...", "dest_subdir": "", "caps": ["bundle"], "wait": true, "timeout": 900, "verify": true}
```

也接受这些等价写法：

- `{"model_id": 2949009, "version_id": 3398520}`：由 `site_domain` 拼出模型页链接。
- `{"url": "...", "file_ids": [1, 2]}`：同一版本只下指定文件（每个文件一条 URL，带 `fileId=`）。
- `{"urls": ["...", "..."]}`：批量提交（`file_ids` 不能与 `urls` 同用）。

- `caps` 缺省就是 `["bundle"]`：自动化默认开启类型分流，本体与附属文件才会进同一个目录。
- **目标文件已存在时不会重下**：返回 `{ok:true, skipped:"exists", files:[...], placement:{...}}`
  （`task` 为 null），`hint` 指向 `force:true`。这是给 AI 兜底的——它看不到界面，直接对已下载过的
  模型调 `/download` 会白传几百 MB 并覆盖同名文件（实测 284MB）。`force:true` 显式要求重下。
  预判只看单条 `url`/`model_id`；`urls`/`file_ids` 批量不做预判（避免 N 次解析）。
  分流关闭时按 `download_target_dir` / `download_dir` 预判；两者都为空才退回旧行为。
- **返回落点**：单条结果与每个 item 都带 `placement`（结构同 `plan`），
  `skipped=exists` 时也带；任务还没解析出落点时 `status=pending`。
  它只报告，不会移动文件、不会改配置。
- `wait=true`（默认）：阻塞到本次提交产生的**全部**新任务进入终态，返回 `task`（单条）或
  `items`（多条，每条含 `submit`/`task`/`verify`）；`timed_out=true` 表示到点未完成。
- `verify=true`（默认）：完成后再跑一次落点校验（文件/附属文件）。**会先等收尾结束**
  （`postprocess.done`，预算 30s）：`status=done` 只代表传输完成，sidecar/封面此时还在写，
  不等就会把成功下载判成失败（实测复现过）。

单条返回 `{ok, submit, task, verify, waited_s, timed_out}`，批量返回
`{ok, items, count, waited_s, timed_out}`。**一次调用覆盖「提交→等完→收尾→校验」。**

`verify.ok` 只回答「模型文件本身可用」，`verify.bundle_ok` 才回答「配置要求写的附属文件都齐了」；
不齐时返回 `hint` 指向 `/tasks/repair`。两者分开是因为没配 API Key 时评论文件本就不会写，
把它算进 `ok` 会让每次校验都假失败。

### `POST /api/automation/tasks`
批量控制：`{"action": "pause|resume|retry|start|remove|archive", "ids": ["..."], "all": false}`。
逐项返回 `{id, ok, result|error}`；`all:true` 只支持 pause/resume/start/remove。

### `POST /api/automation/tasks/repair`
重跑下载收尾（元数据 / 评论 / 封面 / 示例视频 / 旧版清理），默认阻塞到完成：

```json
{"task_id": "1792...", "stages": ["metadata", "cover"], "wait": true, "timeout": 180}
```

- `stages` 省略 = 全部五个阶段；幂等（元数据重写、封面/视频已存在即跳过）。
- 重跑前会清掉这些 stage 的历史错误，结束后返回 `postprocess_errors`（空数组 = 全好了）。
- 只接受**已完成且文件仍在**的任务；历史记录也能重跑（从 `to_dict` 还原任务）。
- 缺少 C 站元数据（`info.meta.info`）时直接拒绝并给 `hint`（先用 `library/op {op:"verify"}` 补）。
- `wait=false` 返回 202，用 `GET /tasks?ids=...&fields=id,postprocess_repair` 轮询 `done`。

这条端点是为「下载完成但库不完整」准备的唯一收口：以前 AI 只能 verify / images/download /
cover 分头补，既不知道补完没有，也没有一次做完的入口。

### `POST /api/automation/file-choice`
回答多文件选择（自动化最常见的阻塞点）：

```json
{"identity": "state.pending_file_choice.id", "indices": [0], "pause": false}
```

`indices` 按 `pending_file_choice.files[].index` 取；传 `{"ack": true}` 表示「界面已经展示过」，
用于启动默认超时。没有待选时返回 400 并说明。

### `POST /api/automation/library/scan`
触发模型扫描，`wait=true`（默认）等到扫描结束，返回扫描状态。

### `POST /api/automation/library/op`
模型管理动作的统一入口，`op` 取以下值（`GET /api/automation/schema` 的 `library_ops` 同源）：

| op | 作用 | 备注 |
| --- | --- | --- |
| `verify` | 校验哈希 + 补全 C 站元数据 | 会发网络请求 |
| `dedupe` | SHA256 查重（只算不删） | 结果在 `progress.result` |
| `dedupe-delete` | 重复文件移入回收站 | **必须 `confirm:true` 且给 `paths`** |
| `localize` | 汉化文件名 | 需要百度翻译密钥 |
| `gen-json` | 生成 WebUI 可读 json | `overwrite:true` 覆盖已有 |
| `download-covers` | 下载缺失封面 | |
| `check-updates` | 检查 C 站更新 | `force:true` 绕过 24h 缓存 |
| `update` | 把「有新版」的模型加入下载队列 | 先跑 `check-updates` |
| `sync-local` | 按本地文件清理失效更新记录 | 同步执行，最长约 2 分钟 |
| `organize` | 按 `target_env` / `organize_mode` 把模型整理进分类目录 | 需要先选目标环境 |
| `rename-c` | 批量重命名为 C 站模型名 | 连附属文件一起改 |
| `rename-preview` | 只算不改：预览批量重命名的新旧名 | 同步返回 `items[]` |
| `translate-descs` | 把模型简介翻译成中文 | 需要百度翻译密钥 |
| `cleanup` | 删除元数据/封面/示例图 | **不可恢复，需 `confirm:true`** |

参数：`paths`（模型文件绝对路径数组，省略=全部）、`wait=1`（默认，阻塞到 `running=false`）、
`timeout`。返回 `{ok, op, start, progress, waited_s, timed_out}`。
`wait=0` 时立即返回，用 `GET /api/automation/library/op?op=verify` 轮询进度。

### `POST /api/automation/images/download`
把模型的示例图批量下载到 `<名>.images/`。参数：`path`、`wait=1`、`timeout`。
后台线程执行（绝不把请求线程挂死），`wait=1` 时轮询进度到 `done>=total` 或超时，
返回 `{ok, started, state:{total,done,downloaded,failed}, waited_s, timed_out}`。
`GET /api/automation/images/state?path=…` 同时给出批量下载与后台补帧的进度。

### `POST /api/automation/workflow/analyze`
解析 ComfyUI 工作流（`.json` / 内嵌 PNG/WebP）或 Forge/A1111 生成参数。
参数：`path`、`full=0|1`。默认**丢掉** `preview_b64` 与边表，只留
`node_count` / `models` / `pos_prompt` / `neg_prompt` / `prompt_notes` / `generation_parameters`
与精简节点表；`full=1` 才返回 `edges`。若解析出模型引用，会自动附 `local_matches`
（本地库是否有同名文件 + 路径 + SHA256 前缀）。

### `GET /api/automation/storage`
落点与磁盘：`download_dir` / `download_target_dir` / `models_dir` / `comfyui_models_root`
的存在性、可写性、剩余空间；`type_dirs` 列出 ComfyUI 核心类型目录哪些已存在。
用于回答「为什么没分流」——分流只写已存在的类型目录，不新建。

`candidates` / `suggested_root`：从这些已配置目录**自身或直接上级**里，找出包含至少两个
ComfyUI 核心类型目录的路径，作为 `comfyui_models_root` 的建议值。只读、不扫整盘、不自动写配置。
配合 `POST /api/automation/config` 写 `{"route_by_type": true, "comfyui_models_root": "<suggested_root>"}`。

### `POST /api/automation/move`
移动模型或其落点，三种目标任选其一：

```json
{"path": "D:/models/loras/a.safetensors", "dest_dir": "D:/ComfyUI/models/loras"}
{"task_id": "<下载任务 id>", "dest_dir": "..."}
{"history_id": "<历史记录 id>", "dest_dir": "..."}
```

`dest_dir` 必须是**已存在的绝对目录**：不代建是刻意的（凭空造目录 ComfyUI 扫不到）。
正在下载的文件会被拒绝（先暂停）；目标有同名模型/附属文件时整条拒绝，绝不覆盖。

### `POST /api/automation/rename`
- 单文件：`{"path": "...", "new_name": "新名字"}` —— 主文件与附属（info/json/封面/评论）同步改名。
- 批量：`{"paths": ["..."], "preview": true}` 先看新旧名（只算不改）；
  去掉 `preview` 后走后台批量改名，用 `GET /api/automation/library/op?op=rename-c` 看进度。

### `GET /api/automation/fs`
只读目录浏览，解决「原生 pick_dir 必须有人点」的缺口：

- 不带 `path`：列盘符（Windows）+ 家目录 + `download_dir`；
- 带 `path`：列该目录一层，默认只回目录（`dirs_only=0` 才含文件），最多 500 条，带 `parent`；
- **只回名字与路径，不读文件内容、不返回大小**；目录不存在返回 404 + `hint`。

### `POST /api/automation/storage/migrate`
把数据目录（配置/任务/历史/缓存）迁到 `directory`，**必须 `confirm:true`**；
`replace_existing:true` 表示允许覆盖目标已有数据。下载/扫描/整理进行中会被拒绝（先停掉）。

### 收尾状态与失败可见：`postprocess` / `postprocess_errors`
`status=done` 只代表传输结束，收尾在独立线程继续。任务行因此带两个字段：

- `postprocess: {stages, started_at, done, finished_at}`：收尾是否真的跑完。
  `download(wait=true, verify=true)` 与 `/tasks/repair` 都靠它判断，不要自己猜文件齐没齐。
- `postprocess_errors: [{stage, error}]`：元数据/封面/视频/旧版清理失败时留痕（同时写 `error.log`）。

下载页状态列显示「收尾未完成」，自动化可以直接据此重跑 `POST /tasks/repair`——**不再出现
「显示已完成、库里却没有信息」的静默状态**。

## 4. 校验

### `GET /api/automation/verify`
参数：`task_id` 或 `path`，可选 `hash=1` 重算 SHA256。

用 `task_id` 时，下载那一刻的结论会原样带出（`expected_sha256 / actual_sha256 /
verification_status`），`verification_status=verified` 时 `sha256_ok` 直接给 `true`，
不必为了确认完整性再花几十秒重算；没校验过就保持 `sha256_ok=null`，并在 `hash=1` 又缺期望值时
附 `sha256_hint` 说明「只给实算值、没有可比对的期望哈希」。

校验项：模型文件存在、体积、SHA256（与任务记录的期望值比对）、
`.civitai.info` / `.json` / `_评论.txt`、封面、`.images/` 目录与帧数、
扩展产物 `评论.txt` / `example.json`。
`path` 是目录时自动挑最大的权重文件当本体。

两个结论字段（分开的理由见 §3 download）：

- `ok`：模型文件本身可用（存在、非空、哈希不是 False）。
- `bundle_ok`：再加上「当前配置要求写出的附属文件都在」。缺哪些看
  `sidecars.expected_missing`；`sidecars.missing` 是全部缺失项（含配置不要求的，只作参考）。

期望值随配置变：`gen_metadata=false` 不要求 `.civitai.info`/`.json`；`metadata_format=civitai`
不要求 `.json`；`archive_comments=false` 或没配 API Key 不要求 `_评论.txt`。

封面判定：`<stem>.preview.png|.preview.jpg|.preview.webp|.cover.png|.cover.jpg` 与
`<stem>.preview.mp4|.preview.webm|<stem>.mp4|.webm` **任一存在即算通过**，命中的文件名放在
`preview` 字段。封面本身是视频的模型（如 REZERO H3）桌面端写不出 `.preview.png`，
旧规则会误报缺失——以视频封面为准是正确语义。

## 5. 配置写入（白名单）

### `POST /api/automation/config`

```json
{"config": {"route_by_type": true, "ask_move_after_download": false}}
```

允许键：`route_by_type`、`comfyui_models_root`、`ask_move_after_download`、`multi_file_download`、
`download_target_dir`、`download_dir`、`models_dirs`、`site_domain`、`max_images_per_model`、
`download_cover`、`proxy_enabled`、`proxy_address`、`ssl_verify`、
`download_name_mode`、`filename_include_version`、`rename_clean_rules`、
`max_concurrent_downloads`、`download_timeout`、`download_retry`、
`gen_metadata`、`save_raw_json`、`save_translated_json`、`auto_translate`、
`target_env`、`organize_mode`、`archive_root`、`onboarding_done`、`auto_guides`。

出现白名单外的键 → 整条 400，**不做部分生效**。写入走 `Api.save_config`（与设置页同一条路径，
含多目录规范化与下载器重绑）。

## 6. 长尾方法

`GET /api/rpc/methods` 列出全部可调用方法及参数名；`POST /api/rpc {"method": "...", "args": [...]}`
调用。系统选择框类方法（`pick_dir` / `pick_file` / `pick_files`）永远不暴露。

常用：`respond_download_file_choice`、`get_model_detail`、`mm_organize`、`mm_verify`、
`mm_check_updates`、`lib_list`、`rp_start`、`fetch_model_comments`。

## 7. 一键验证脚本

```bash
python scripts/verify_e2e.py --url "<模型页>" --json      # 提交→等待→校验，一行 JSON
python scripts/verify_e2e.py --url "<模型页>" --plan      # 只干跑
python scripts/verify_e2e.py --task <task_id> --hash      # 只校验已有任务

python scripts/ai_flow_audit.py                            # 卡点审计：API 层走完整流程并计时
python scripts/ai_flow_audit.py --e2e                      # 额外真实下载一个 1–20MB 的小模型
python scripts/ai_ui_audit.py                              # 卡点审计：真 Chromium 打开界面，验配置改动是否即时生效
python scripts/smoke_agent_transports.py --json             # OpenAPI + /tool/{name} + SSE 重放（只读）
```

`ai_flow_audit.py` 只回答三个问题：某一步是不是**必须人工介入**、是不是**没有完成信号**、
是不是**要多跳几次**。退出码非 0 表示有阻塞项。`ai_ui_audit.py` 需要 Playwright（Python 版），
用真实浏览器验证前端是否跟得上后端配置变更（无头模式，不影响正在运行的窗口，结束会恢复原配置）。

退出码 0 = 全部符合预期。

## 8. 免交互开关（浏览器扩展侧）

`chrome.storage.local` 的 `cft_config_v1.autoConfirmDuplicate = true` 时，
「该版本已归档过」不再弹原生 `confirm`（无头/自动化环境会默认 dismiss 并取消归档），
直接按「继续」处理。默认 `false`，保持人工确认。

## 9. 已知边界

- 「是否移动到分类文件夹」是**前端弹窗**，不属于后端接口；自动化请设
  `ask_move_after_download=false`，或让任务走分流（分流任务本就不会弹）。
- 扩展 offscreen 文档只有 `chrome.runtime`（无 `chrome.tabs` / `chrome.storage`），
  所有 content 消息经 background 代发；归档日志由 offscreen 转交 bg 落盘。
- `plan` 的 `probe=true` 会走网络；默认关闭以免干跑变慢。
- 自动化端点与 `/api/rpc` 同源放行：本机任何进程都能调用，**不要在没有本机信任的前提下暴露端口**。
- 请求体上限：`POST /api/automation/{tool,vision/analyze}` 与 `tool/{name}` 中属于
  `ai_contract.LARGE_BODY_TOOLS`（当前只有 `cft_inspect_image`，MCP 图片上传入口）放宽到 8MB，
  其余自动化端点与别名仍是 1MB；超限返回 413 + `limit_bytes`，不把桥变成任意大文件入口。
- 出站 webhook 是尽力实时推送，不是 at-least-once：不落盘、不跨重启重放、不跟随重定向；
  队列满或应用退出时事件会丢，`stats.dropped` / 投递日志会如实记录。需要可靠重放请用 SSE。
- webhook 默认只允许解析到非公网地址的目标；`webhook_allow_remote=true` 是显式数据出境选择，
  事件里可能含本地文件名/落点路径。`webhook_url` 与 `webhook_secret` 不会出现在任何响应/日志里。
