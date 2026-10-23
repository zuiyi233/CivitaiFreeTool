# AI 接入扩展方案：OpenAPI 3.1 + SSE 事件流

状态：已实现（P0 OpenAPI / P1 SSE / P2 验收与文档）  
日期：2026-10-22  
目标版本：v2.7.0  
关联文档：[AI 接入契约](ai-integration.md)、[自动化 API](automation-api.md)

## 0. 落地结果

- OpenAPI：`GET /api/automation/openapi.json?profile=text|vision|admin|all`；
  每工具执行路径 `POST /api/automation/tool/{name}`。
- SSE：`GET /api/automation/events/stream`，由 `automation_events.EventHub`
  做 1 秒快照差分；支持首帧 `state.snapshot`、心跳、`Last-Event-ID` 重放、reset、
  overflow、429。
- 事件目录在 `ai_contract.EVENTS`（21 类，其中 20 类属于默认领域订阅），与
  `cft_capabilities` 的 `caps.events`、OpenAPI 的 `x-cft-events` 同源。
- 测试：`tests/test_ai_openapi.py`、`tests/test_automation_sse.py`；打包真机验收见发布说明。

## 1. 结论

MCP 继续作为 Agent 的首选入口，不替代、不改协议。新增两条 **同一契约的旁路投影**：

1. **OpenAPI 3.1**：把 29 个工具生成为可被 Dify、n8n、LangChain、Open WebUI、自研 Agent
   直接导入的 HTTP 操作。解决「支持函数调用、但不支持 MCP」的平台接入问题。
2. **SSE 事件流**：新增 `GET /api/automation/events/stream`，在任务完成/失败、扫描结束、
   弹层出现、配置变化时主动推送。解决「AI 只能每秒轮询」的问题。

两条旁路都只做传输与格式适配：工具执行仍走
`MCP 侧车 / OpenAPI 客户端 → /api/automation/tool* → ai_tools.execute_tool → 既有 Api 方法`，
不复制下载、分流、校验、资源库逻辑。

## 2. 为什么是这两项

| 现状 | 缺口 | 本方案 |
|---|---|---|
| MCP stdio / Streamable HTTP 已可用 | 宿主必须支持 MCP；终端型 Agent、工作流平台、自研脚本未必支持 | OpenAPI 3.1 + 每工具一个 HTTP 操作 |
| `/tools` 已能给出 OpenAI / Anthropic 函数 schema | 只给 schema，不导入平台就无法发现和调用 | 生成完整 OpenAPI 文档与实际执行路径 |
| `/events?since=` 只能轮询，缓冲区 200 条 | 下载/扫描/弹层变化没有推送，AI 只能高频查 | 独立 SSE 事件总线 + 断线重放 |
| 前端每秒轮询 `get_tasks` | 外部 AI 没有等价的低成本订阅方式 | 服务端做快照差分，一次采样服务所有订阅者 |

明确不做的事：

- 不做 webhook、A2A、CLI、WebSocket；这些留到确有需求再加。
- 不新增工具、不改变工具语义、不改变 MCP 返回结构。
- 不把桥暴露到局域网或公网；OpenAPI 只面向 **同一台机器上的本地 Agent**。
- 不为 OpenAPI 再手写一份工具表；文档必须由 `ai_contract.py` 生成。

## 3. 总体架构

```
ai_contract.py（唯一事实源：工具 / Schema / 事件目录 / 限额）
   ├── MCP 侧车 ──────────────┐
   ├── OpenAPI 文档生成 ──┐    │
   └── SSE 事件目录 ────┐ │    │
                        │ │    ▼
外部 Agent ──OpenAPI──> GET /api/automation/openapi.json
外部 Agent ──HTTP─────> POST /api/automation/tool/{tool}
MCP 宿主 ─────────────> POST /api/automation/tool
外部 Agent ──SSE──────> GET /api/automation/events/stream
                              │
                     automation_events.EventHub
                              │ 1s 快照差分（仅在有订阅者时启动）
                     automation_state() + Api 状态
```

## 4. OpenAPI 3.1 设计

### 4.1 发现端点

```
GET /api/automation/openapi.json?profile=text|vision|admin|all
```

- 默认 `profile=text`，与现有 `/tools` 默认一致。
- `admin` 只有显式请求才出现；`text` 绝不包含 admin 工具。
- 返回 `application/json`，`Cache-Control: no-store`。文档在请求时从契约生成，不落盘。
- `servers[0].url` 优先使用请求的 `Host`，但只接受 `127.0.0.1:<port>` / `localhost:<port>`；
  其他 Host 回落到 `http://127.0.0.1:47531`。
- 同时在 `GET /api/automation` 的端点清单和 `GET /api/automation/ai` 的能力响应中登记
  `openapi` 与 `sse`，避免 AI 靠猜。

### 4.2 每个工具一个实际执行路径

OpenAPI 导入器通常把「一个 operation = 一个工具」。因此新增：

```
POST /api/automation/tool/{tool_name}
Content-Type: application/json
Body: 直接是该工具的 arguments 对象（不再是 {name, arguments} 包装）
```

示例：

```http
POST /api/automation/tool/cft_search_models
Content-Type: application/json

{"query":"sdxl","type":"Checkpoint","limit":5}
```

服务端行为：

- `tool_name` 必须精确命中 `ai_contract.TOOLS`；未知工具返回现有错误信封（404）。
- 请求体必须是 JSON 对象；空体按 `{}` 处理。
- 内部转换为 `{"name": tool_name, "arguments": body, "content_format": <query>}`，
  直接复用 `_tool_call`，不新增执行分支。
- 支持 `?content_format=neutral|openai|anthropic|mcp`，默认 `neutral`。
- `GET` 返回 405；`POST` 的 body 上限沿用 8MB（图片工具需要）。
- 既有 `POST /api/automation/tool` 保持原样，MCP 侧车继续走它。

操作命名：

- `operationId` 固定等于工具名，例如 `cft_search_models`，保证跨版本稳定。
- `tags` 使用工具集：`core` / `vision` / `admin`。
- `summary` 用工具 title；`description` 用契约里的 description。
- `requestBody.schema` 直接使用 `TOOLS[name]["inputSchema"]`，不二次加工。
  现有 Schema 已是 OpenAPI 3.1 兼容的 JSON Schema 2020-12 子集。

### 4.3 Profile 与安全注解

| profile | 工具数 | 用途 |
|---|---|---|
| `text` | 19 | 默认；纯文本模型、工作流平台 |
| `vision` | 24 | core + vision；多模态模型 |
| `admin` | 24 | core + admin；文件/配置/删除 |
| `all` | 29 | 全量 |

每个 operation 附带扩展字段，供平台展示风险：

```json
{
  "x-cft-toolset": "core",
  "x-cft-annotations": {
    "readOnlyHint": true,
    "destructiveHint": false,
    "idempotentHint": true,
    "openWorldHint": false
  },
  "x-cft-destructive": false,
  "x-cft-requires-confirm": false,
  "x-cft-returns": "text"
}
```

`profile` 只控制 **文档里出现哪些工具**，不改变执行权限；这与现有 `/tools` 一致。
真正的破坏性操作仍要求 `confirm:true`，OpenAPI 只是把风险提前告诉平台。

### 4.4 响应与错误

成功响应沿用现有工具信封：

```json
{
  "ok": true,
  "tool": "cft_get_state",
  "data": {},
  "content": [],
  "next_actions": [],
  "meta": {}
}
```

OpenAPI 里用一个宽松的 `ToolResult` schema（`additionalProperties: true`）描述，
不把 29 种返回结构硬编码进文档；否则契约一改文档就漂移。错误沿用统一结构：

```json
{
  "ok": false,
  "code": "invalid_arguments",
  "retryable": false,
  "msg": "...",
  "hint": "...",
  "next_actions": []
}
```

文档中给出 `cft_get_state`、`cft_search_models`、`cft_download_model` 三个示例，
其余工具靠 schema 自描述。

### 4.5 本地 Agent 平台注意事项

- Dify / n8n / LangChain / Open WebUI 若与本软件在同一台机器，可直接导入。
- 云端平台（ChatGPT GPT Actions、云端 Coze 等）无法访问 `127.0.0.1`；本方案不通过隧道
  或公网映射解决这个问题，这是刻意的安全边界。
- Docker 内的 Agent 默认也到不了宿主回环地址；需要 `--network host` 或把 Agent 跑在宿主上。
  文档必须如实写明，不能让用户导入后才发现连不上。

## 5. SSE 事件流设计

### 5.1 端点与参数

```
GET /api/automation/events/stream
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `since` | 空 | 从该 seq 之后重放；空 = 只收新事件 |
| `types` | 全部领域事件 | 逗号分隔的事件名或组名；`all` 含 `api.call` |
| `snapshot` | `1` | 新连接先发一条 `state.snapshot`；重连重放时默认不重发 |
| `Last-Event-ID` | 空 | 标准 SSE 断线续传；有 header 时优先于 `since` |

响应头：

```
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache, no-transform
Connection: keep-alive
X-Accel-Buffering: no
```

### 5.2 帧格式

```
id: 42
event: task.finished
data: {"seq":42,"instance_id":"a1b2c3","type":"task.finished","at":"2026-10-22T12:00:00+08:00","data":{"id":"t-1","status":"done"}}

: keep-alive 2026-10-22T12:00:15+08:00
```

- `id` 是单次进程内单调递增的 seq；`instance_id` 每次启动随机生成。
- 心跳用 SSE 注释（`:` 开头），不占 seq，不触发客户端事件回调，默认 15 秒。
- 单条 `data` 上限 64KB；超限截断任务列表/错误文本并带 `truncated:true`。
- 二进制、base64、封面字节绝不进 SSE；图片仍由 vision 工具按需取。

### 5.3 事件目录

| 事件 | 触发 | 主要字段 |
|---|---|---|
| `stream.ready` | 连接建立 | `instance_id`, `last_seq`, `oldest_seq`, `heartbeat_s`, `types` |
| `state.snapshot` | 新连接（默认） | 紧凑 state：`config`、`tasks`、`pending_file_choice`、`ui` |
| `stream.reset` | `since` 早于环形缓冲 | `oldest_seq`, `last_seq`, `instance_id` |
| `stream.overflow` | 客户端队列满 | `dropped` |
| `task.created` | 新任务出现 | `id`, `filename`, `status`, `dest_dir` |
| `task.updated` | 状态/进度变化（每任务最多 1 次/秒） | `id`, `status`, `progress`, `downloaded`, `total`, `error` |
| `task.finished` | 进入 `done/error/canceled` | `id`, `status`, `filename`, `dest_dir`, `error`, `postprocess_errors` |
| `task.removed` | 任务从队列消失 | `id` |
| `file_choice.pending` | 出现待选文件 | `id`, `files_count` |
| `file_choice.resolved` | 待选结束 | `id`, `selected_count`, `cancelled` |
| `model_scan.started/progress/finished` | 模型库扫描 | `running`, `msg`, `total` |
| `library.scan.started/progress/finished` | 资源库扫描 | `done`, `total`, `phase`, `msg` |
| `ui.overlays.changed` | 可见弹层集合变化 | `overlays[]`：`name/group/kind/title/blocking` |
| `ui.dismissed` | AI/前端关闭弹层 | `rev`, `targets` |
| `config.changed` | 配置版本变化 | `rev`, `keys[]`（只给键名，不给值） |
| `api.call` | 仅 `types=all`，且不重放 | 现有调用事件：`method/path/status/ms/summary` |
| `app.stopping` | 正常退出时尽力发送 | `pid` |

目录共 21 条（`ai_contract.EVENTS`），其中 20 条属于领域事件默认订阅集，`api.call` 只在
`types=all` 时实时投递。事件名、分组与 payload schema 的唯一出处是契约，SSE 帧与
OpenAPI 的 `x-cft-events` 都从它生成，不接受第二份清单。

`ui.overlays.changed` 是这次弹层工作的自然延伸：AI 不必轮询 `state.ui.guides.overlays`，
弹层一出现就能收到推送并调用 `cft_dismiss_ui`。

### 5.4 事件源：快照差分，不改下载器

不向 `downloader.py` / `webui.py` 的任务状态机里散落埋点。事件总线在有订阅者时启动一个
1 秒采样线程，读取现有状态并做差分：

```python
state = automation_state()             # 配置 / 待选文件 / UI（不读磁盘）
tasks = api.dl.tasks                   # 活动任务全量，只读，采样上限 100
model_scan = api.get_scan_state()      # 轻量，无行数据
library_scan = api.lib_scan_state()    # 轻量，无行数据
```

差分规则：

- 新任务 id → `task.created`；终态迁移 → `task.finished`；其余变化 → `task.updated`。
- **瞬时任务不会丢**：若某任务在两次采样之间出现并已完成，快照里只见过它的终态，
  则补发 `task.created` + `task.finished` 一对，保证「created → finished」的顺序契约成立。
- 进度按整数百分比 + 1 秒节流，避免刷屏。
- 只比较 `automation_state()` 与任务快照里的只读字段，不读取模型文件、不查网络。
- **待选文件走变更日志，不走电平比较**：`FileChoiceBroker` 维护 `choice_rev` 与
  最近 50 条 `pending/resolved` 记录，采样按游标读取。因此「弹窗一闪而过」（自动选中或
  立即取消）也能被捕获，不会被电平差分漏掉。
- **扫描同理兜住「两次采样之间跑完」**：除了 `running` 的上升/下降沿，还识别 msg 变化但
  已不在运行的情况，补发 `*.finished`；模型库扫描另有 `model_scan.progress`。
- 最后一个订阅者断开后保留 30 秒宽限期，再停止采样；宽限期内重连可继续重放，
  超过宽限期则强制 `stream.reset` 并用新的 `state.snapshot` 对齐。
- `shutdown()` 后 hub 标记为死，采样线程不会被新订阅者重新拉起，退出不会被 SSE 卡住。
- 采样异常只记录一次日志并继续，不能让 SSE 线程拖垮桥。

副作用：事件最多延迟约 1 秒。与当前前端 1 秒轮询同一量级，可接受；需要更低延迟时再考虑
在任务状态机里加显式 publish，而不在本期做。

### 5.5 重放、断线与背压

- 领域事件环形缓冲 `deque(maxlen=500)`，独立于现有 200 条 `api.call` 日志；SSE 使用自己的
  seq，不改变 `/events` 的 `since/next_since` 语义。
- `api.call` 事件只在 hub 活跃时由 `record_event` 实时投递，不进入历史重放；需要调用日志
  历史的客户端继续用 `/events`。
- 客户端带 `Last-Event-ID`（或 `?since=`）重连：缓冲内则按序补发，缓冲外则先发 `stream.reset`，
  再进入实时模式；绝不静默丢事件。
- **重放同样受 `types` 过滤**：订阅者记录自己的过滤条件，环形缓冲是共享的，但每个客户端
  只收到自己订阅的类别。
- 每个客户端一个 `Queue(maxsize=256)`。发布用非阻塞 `put_nowait`；队列满时丢最旧事件，
  累计 `dropped`，下一次成功写入前先发 `stream.overflow`。`dropped` 只统计真实丢弃。
- 最多 8 个并发 SSE 客户端，超出返回 429 + `hint`。连接数在写响应头前预检；若预检通过后
  仍撞上限（并发竞争），则用一帧注释说明并正常收尾，不留下半订阅状态。
- **订阅在响应头写成功之后才登记**：客户端在握手前断开不会留下幽灵订阅，也不会误启采样线程。
- 正常退出时尽力向所有客户端发 `app.stopping` 再关闭连接；进程被强杀时该事件可能缺席，
  客户端应以连接关闭为最终信号。

### 5.6 HTTP 传输细节

当前桥是 `ThreadingHTTPServer`，每个连接一个线程，适合 SSE，但要注意：

- SSE 分支必须在 `_automation()` 之前拦截，不能走返回 JSON 的 dispatch。
- 该响应使用 HTTP/1.1、`Connection: keep-alive`、不设 `Content-Length`，每帧 `flush()`；
  非 HTTP/1.1 的请求直接 400，避免分块语义落到不支持的协议版本上。
- 响应头带 `Cache-Control: no-cache, no-transform` 与 `X-Accel-Buffering: no`，防中间代理缓冲。
- 连接线程设置写超时；心跳写失败即视为断开。断开检测用 `select` + `MSG_PEEK`，
  不需要客户端额外发心跳。
- 请求体超限时先把已发送的字节排空（≤2×cap）再回 413，否则 Windows 会 RST，
  客户端只能看到连接中断而拿不到 `limit_bytes` + `hint`；更大的请求不排空，直接放弃。
- 浏览器模式下关闭页面会自动退出软件的既有策略不变：SSE 连接不冒充页面心跳，
  不阻止 `exit_when_page_closed`。需要常驻的 Agent 应使用窗口模式或自行拉起软件。

## 6. 契约与状态字段变更（均为增量）

`ai_contract.py`：

- 新增 `EVENTS` 目录（事件名、组、说明、payload schema），供 SSE 与 OpenAPI 的
  `x-cft-events` 共用；`EVENT_TYPES` 按名索引，`DOMAIN_EVENT_TYPES` 是默认订阅集
  （不含 `api.call`）。
- 新增 `AGENT_TRANSPORT_LIMITS`（连接数 / 队列 / 环形缓冲 / 心跳 / 空闲宽限 / 采样间隔 /
  单帧上限）与 `LARGE_BODY_TOOLS`（`cft_inspect_image` 等允许 8MB 请求体的工具）。
  两者都出现在 OpenAPI 的 `x-cft-agent-transport-limits` / `x-cft-large-body-tools` 里，
  客户端不必去猜请求体上限。
- `capabilities_payload()` 增加 `caps.openapi` / `caps.sse` / `caps.events` / `caps.transports`
  的具体 URL 模板。
- 工具定义数量随契约更新（当前 29 个）。

`automation_state()`：

- 增加 `config_rev`（当前 `_cfg_rev`）。
- `ui.guides` 增加 `dismiss_targets`（当前 `_ui_dismiss_targets`）。
- 任务项增加 `postprocess` 与 `postprocess_errors`，供 `task.finished` 如实报告收尾失败。
- 增加 `file_choice_rev` 与 `file_choice_transitions`，供事件总线按游标读取待选文件变化。
- `config.proxy_address` 若带 `user:pass@` 一律脱敏为 `***:***@`，状态与事件都不回显凭据。

以上都是新增字段，现有 MCP/HTTP/Skill 客户端不受影响；对应测试只做增量断言。

## 7. 安全边界

- **只监听回环**：不新增端口，不新增公网入口；OpenAPI 与 SSE 都复用 47531。
  OpenAPI 的 `servers` 只从回环 `Host` 推导，非回环 Host 一律回落默认地址，
  不会生成「指向任意外部主机」的文档。
- **沿用现有 Origin 白名单**：无 Origin 的本地进程、扩展 Origin、本机页面；其他浏览器来源 403。
- **不新增鉴权层**：现有自动化面本身就是本机信任模型，单独给新端点加 token 而 `/tool`
  仍开放没有实际意义。若未来要防本机恶意进程，应对整个 `/api/automation/*` 统一加 token，
  那是独立的安全改造，不混在本期。
- **Secret 不进事件**：事件只带配置键名，不带 `api_key` / `baidu_key` / token / Cookie；
  `api.call` 的 path/summary 过滤 `token`、`key`、`api_key`、`authorization` 查询参数；
  代理地址 userinfo 在状态层就已脱敏。
- **Admin 默认不出现**：OpenAPI 默认 `text`；profile 只影响发现，执行仍是 `confirm:true` 门控。
- **资源上限**：SSE 8 连接、队列 256、环形缓冲 500、单帧 64KB、心跳 15 秒、空闲宽限 30 秒。
  请求体默认 1MB，`/vision/analyze`、`/tool` 与 `LARGE_BODY_TOOLS` 放宽到 8MB，超限 413。
- **异常隔离**：单个客户端写失败只注销该客户端；单个工具调用失败仍走现有错误信封。

## 8. 兼容性与回归面

- MCP 侧车、`/tool`、`/tools`、`/guide`、`/state` 行为不变。
- `/events` 保持原样（调用日志轮询），不新增参数、不改变 `since/next_since` 语义；
  SSE 是独立的领域事件订阅，二者不共享 seq。
- 新路径 `POST /tool/{name}` 是 `_tool_call` 的薄别名，不产生第二套执行逻辑。
- 前端不依赖 SSE；AI 弹层关闭仍可用 `ui_dismiss_rev` 轮询兜底。
- Skill 只增加「非 MCP 平台怎么接」的 recipes，不承载业务逻辑。

## 9. 测试与验收

### 9.1 单元/集成测试

`tests/test_ai_openapi.py`（13 条，全离线，直接走 `automation_api.dispatch`）：

- 每个 profile 的 operation 数与契约一致（text 19 / vision 24 / admin 24 / all 29），
  且每个 operation 的 `requestBody` schema 与契约的 `inputSchema` 逐字段相等。
- `text` 与 `vision` 不含 admin 工具；`admin` / `all` 才出现；未知 profile 返回 400。
- `servers` 只信任回环 `Host`：外部 Host 头不会写进文档；文档可 JSON 序列化且不含本机绝对路径。
- `x-cft-events` 与 `ai_contract.EVENTS` 同源；`capabilities` 的 `caps.openapi` / `caps.sse` 为真；
  `/api/automation` 清单列出 `/openapi.json`、`/tool/{name}`、`/events/stream`。
- `/tool/{name}` 与 `/tool` 对同一只读工具返回一致结果；`?content_format=` 生效；
  未知工具 404；非对象 body 400；GET 405。

`tests/test_automation_sse.py`（22 条：14 条总线 + 8 条真实 HTTP 传输）：

- 总线：任务 created/updated/finished/removed；只在快照里见到终态的瞬时任务补发
  created + finished；待选文件走变更日志，快速 pending→resolved 也能捕获；扫描的
  started/progress/finished，含「两次采样之间跑完」补发 finished；
  `ui.overlays.changed` / `ui.dismissed` / `config.changed`（只有键名）。
- 重放：环形缓冲内按序补发，`since` 早于缓冲发 `stream.reset`；最后一个订阅者断开并超过
  宽限期后再连，必须拿到 reset + 新快照；重放同样受 `types` 过滤。
- 背压：队列满时 `stream.overflow` 且 `dropped` 只计真实丢弃；第 9 个连接被拒；
  `shutdown()` 发布 `app.stopping` 且采样线程不会被新订阅者重新拉起。
- 传输：`stream.ready` + `state.snapshot` + 实时事件；`Last-Event-ID` 重放 seq 连续；
  短间隔下心跳注释帧出现；`stream.ready` 的 payload 通过契约 schema 校验；
  客户端在响应头写出前断开不会留下幽灵订阅。
- 请求体口径：1MB 路径超限返回 413 + `limit_bytes`；同一载荷走 `LARGE_BODY_TOOLS`
  的 8MB 口径能过闸（错误来自工具本身而非网关）；超过 8MB 仍 413。

`tests/test_ai_http.py` 增量：

- `/api/automation` 清单与 `/ai` 的 caps 包含 `openapi` / `sse`。
- `/events` 原有轮询仍通过；SSE 的独立 seq 不影响 `since/next_since`。

### 9.2 打包真机验收（已执行）

在隔离目录 `.tools/smoke_all/` 运行刚打包的 `CivitaiFreeToolWeb.exe`（FileVersion 2.7.0，
`--browser`，数据目录与仓库隔离），逐项观测：

| 验收项 | 结果 |
|---|---|
| `GET /api/automation/openapi.json?profile=text\|vision\|admin\|all` | 200；操作数 19 / 24 / 24 / 29；`servers` 只有 `http://127.0.0.1:47531` |
| OpenAPI 与契约同源 | 每个 operation 的 `requestBody` schema 与 `GET /tools?format=mcp` 逐字段相等 |
| `POST /tool/cft_capabilities` | 200，`caps.openapi=true`、`caps.sse=true`、`caps.events=21` |
| `GET /tool/cft_capabilities` | 405 |
| SSE 首帧 | `stream.ready` + `state.snapshot`（`types=config,stream` / `ui,stream` 均按订阅过滤） |
| 真界面弹层 | Playwright 打开确认框 → 收到 `ui.overlays.changed`，`overlays[].name = confirm` |
| AI 一键关闭 | `POST /ui/dismiss {"targets":["confirm"]}` → 收到 `ui.dismissed`（seq 连续），页面 `confirm` 的结果是 `false`：**关闭等于取消** |
| 断线重放 | 带 `Last-Event-ID` 重连，补发 `ui.dismissed` 且 seq 与断开前一致 |
| `config.changed` | 改一个开关 → 事件 `rev=1`、`keys=["save_raw_json"]`（只有键名）；随后恢复原值 |
| 代理脱敏 | 写入 `http://user:pass@127.0.0.1:8899` → `state`/事件读到 `http://***:***@127.0.0.1:8899`，密码不出现在任何响应 |
| 请求体口径 | 1.2MB 打普通端点 → 413 + `limit_bytes=1048576` 且能读到完整错误信封；同样载荷打 `cft_inspect_image` 别名 → 过闸，错误来自工具本身（404 not_found） |
| 订阅上限 | 8 个并发订阅正常，第 9 个 → 429 + `hint` |
| 前端产物 | `GET /app.js`、`/ai-overlays.js`、`/commands.js`、`/download-files.js`、`/image-viewer.js` 与源文件 SHA-256 逐字节一致 |
| MCP 未漂移 | `CivitaiFreeToolMCP.exe --selftest` 对同一打包实例列出 core 19 工具，`app: ok version=2.7.0` |

**未在打包级验证**：`task.*` / `file_choice.*` 的真机触发——需要真实下载或真实多文件待选，
会动用户的磁盘与网络，不在验收里制造。这两类由 `tests/test_automation_sse.py` 的快照差分
用例覆盖（含瞬时任务补发、快速待选过渡）。

第三方平台（Dify/n8n/LangChain）不做虚假验收：本机没有部署就不声称「已导入成功」。
测试至少覆盖 OpenAPI 结构校验与一个真实 HTTP 消费客户端（`scripts/smoke_agent_transports.py`）；
第三方导入步骤写进文档，由有环境的用户按步骤验证。

## 10. 分阶段落地

| 阶段 | 内容 | 退出标准 |
|---|---|---|
| P0 | 契约事件目录、OpenAPI 生成器、`/openapi.json`、`/tool/{name}` | ✅ 文档 19/24/24/29；别名与 `/tool` 结果一致；测试全绿 |
| P1 | `automation_events.py`、快照差分、SSE 端点、重放/心跳/背压 | ✅ SSE 测试通过；无订阅者超过宽限期后停止采样 |
| P2 | 打包真机验收、示例客户端、Dify/n8n/LangChain 接入文档、Skill recipes | ✅ 见本节下方实际验收；文档步骤可复现 |
| P3（后续） | CLI 子命令、webhook、A2A | 单独立项，不阻塞本期 |

预计改动文件：

- 新增：`openapi_projection.py`、`automation_events.py`、`tests/test_ai_openapi.py`、
  `tests/test_automation_sse.py`。
- 修改：`ai_contract.py`（事件目录/caps）、`automation_api.py`（路由/清单）、
  `browser_bridge.py`（SSE 传输/状态字段/8MB 路径）、`docs/ai-integration.md`、
  `docs/automation-api.md`、`docs/releases/*`、`skills/civitai-free-tool/references/recipes.md`。
- 不改：`downloader.py` 的任务状态机、MCP 工具执行链、现有 29 个工具的定义。

## 11. 风险与取舍

| 风险 | 取舍 |
|---|---|
| SSE 每个客户端占一个线程 | 上限 8；心跳/写超时；断开即回收 |
| 快照差分最多延迟 1 秒 | 与现有前端轮询同量级；不为此改下载器 |
| 事件环形缓冲占内存 | 500 条、单帧 64KB 上限；超限 reset 而非静默丢 |
| OpenAPI 平台可能不认识 `x-cft-*` | 标准字段完整；扩展字段可忽略，不影响调用 |
| 云端平台连不上回环地址 | 明确不支持；不为此开公网隧道 |
| 本机恶意进程可调用自动化面 | 维持现有信任模型；统一 token 化单独立项 |
| 多一个投影就多一份回归面 | 文档运行时生成、执行复用 `_tool_call`、测试锁契约数量 |

## 12. 默认决策（可调整）

1. **不做 token**：本期沿用本机信任；如果要把自动化面给非本机 Agent 用，先暂停并单独设计鉴权。
2. **默认 `profile=text`**：避免纯文本模型一导入就看到 29 个工具；vision/admin 显式选择。
3. **默认发 `state.snapshot`**：AI 一连上就有基线，减少一次 `/state` 调用。
4. **默认不推送 `api.call`**：避免客户端看到自己的调用回声；`types=all` 才包含。
5. **事件源用 1 秒快照差分**：先保证不侵入下载器；延迟不够再升级为显式 publish。

## English summary

Keep MCP as the primary agent transport. OpenAPI 3.1 is now generated at runtime from
`ai_contract.py`, and every tool has a real per-operation alias route
(`POST /api/automation/tool/{name}`). SSE uses a separate snapshot-diff event bus with a
30-second reconnect grace, chunked HTTP/1.1 transport, heartbeat, replay, reset/overflow,
and loopback-only security. No download state machine was modified.

All three phases are landed. Acceptance ran against the freshly packaged build in an isolated
directory: 19/24/24/29 operations per profile, request schemas byte-identical to the MCP tool
list, `405` on `GET` alias routes, live `ui.overlays.changed` / `ui.dismissed` from a real
in-app confirm dialog (dismissing resolves the promise as `false`, i.e. cancel), `Last-Event-ID`
replay with a matching sequence, `config.changed` carrying key names only, masked proxy
credentials, a readable `413` envelope on oversized bodies, and `429` for a ninth concurrent
subscriber. `task.*` and `file_choice.*` pushes are covered by the automated suite rather than
a real download, so acceptance never touches the user's disk or network.
