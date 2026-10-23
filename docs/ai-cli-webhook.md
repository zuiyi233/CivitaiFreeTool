# AI 调用面的 CLI / webhook 传输层

> 状态：已实现，随 **v2.8.0** 发布。契约仍是 `ai_contract.py` 的 **29 个工具 / 21 类事件**；
> 本文只增加传输层，不新增工具、不改执行链、不改下载器状态机。
>
> 前置：MCP（stdio / Streamable HTTP）是唯一工具协议；OpenAPI 3.1、每工具别名、SSE 是同一契约的
> 投影（见 `docs/ai-openapi-sse.md`）。CLI 与 webhook 是同一契约的另外两个传输方向：
> **CLI 是入站客户端，webhook 是出站推送**。

## 0. 一句话

- **CLI**：一个 stdlib-only 的本机命令行客户端。它只把 `/api/automation/*` 包成可脚本化的子命令，
  执行仍走 `POST /api/automation/tool/{name}` → `ai_tools.execute_tool`，不复制任何业务逻辑。
- **webhook**：桌面端把契约事件（`ai_contract.EVENTS`）以 HTTP POST 推到用户配置的本机/局域网
  接收端，带 HMAC-SHA256 签名、有限重试与可观测投递日志；默认关闭，默认只允许非公网地址。

## 1. 角色与边界

| 层 | 职责 | 不做什么 |
| --- | --- | --- |
| `cft_cli.py` / `CivitaiFreeToolCLI.exe` | 本机脚本/Shell Agent 的命令行入口；发现、调用、订阅事件 | 不直连 C 站、不读下载器内部状态、不复制工具逻辑、不做远程连接 |
| `webhook_dispatch.py` | 把 EventHub 事件推给用户配置的 HTTP 端点；签名、重试、日志 | 不新增事件类型、不持久化事件、不监听入站请求、不跟随重定向 |
| `EventHub` sink | 给 webhook 一个非阻塞的事件出口 | 不占 SSE 的 8 个客户端名额；不改变事件 seq/重放语义 |
| `ai_contract.py` | 工具/事件/限额/退出码的唯一事实源 | 不因传输层增加工具或事件 |

```text
Shell / CI / 本地 Agent
  └─ CivitaiFreeToolCLI.exe ──HTTP──┐
                                    ▼
MCP Host ──stdio/HTTP──► CivitaiFreeToolMCP.exe ──► POST /api/automation/tool
OpenAPI importer ──────► POST /api/automation/tool/{name}
                          │
                          ▼
                   ai_tools.execute_tool
                          │
                   automation_api.dispatch / Api / downloader
                          │
                    EventHub（1s 快照差分）
                     ├─ SSE 订阅者
                     └─ sink ─► webhook_dispatch ──HTTP POST──► 用户端点
```

## 2. 契约新增（不改变 29 / 21）

只增加传输层常量，`TOOLS` 与 `EVENTS` 数量不变：

```python
CLI_VERSION = "1.0.0"
CLI_EXIT_CODES = {
    "ok": 0, "internal_error": 1, "usage": 2, "app_not_running": 3,
    "invalid_argument": 4, "not_found": 5, "destructive_requires_confirm": 6,
    "rate_limited": 7, "timeout": 8, "version_mismatch": 9, "unsupported": 9,
}
WEBHOOK_LIMITS = {
    "timeout_s": 5.0, "max_attempts": 4, "backoff_s": [1.0, 5.0, 15.0],
    "queue_size": 256, "log_size": 200, "max_body_bytes": 64 * 1024,
    "max_response_bytes": 64 * 1024, "min_secret_length": 16, "test_timeout_s": 10.0,
}
WEBHOOK_DEFAULT_EVENT_GROUPS = ("task", "file_choice", "scan", "ui", "config", "app")
```

- `WEBHOOK_DEFAULT_EVENT_GROUPS` 刻意排除 `api`：`api.call` 是自检高频事件，默认推送会淹没接收端。
- `caps` 增加两个**发现**字段（不影响执行权限）：

```json
{
  "webhook": {
    "available": true, "enabled": false, "configured": false,
    "url_host": "", "events": ["task", "file_choice", "scan", "ui", "config", "app"],
    "signature": "HMAC-SHA256", "endpoint": "/api/automation/webhooks"
  },
  "cli": {
    "available": true, "command": "CivitaiFreeToolCLI.exe", "source": "cft_cli.py",
    "transport": "http-bridge", "commands": ["health", "capabilities", "tools", "guide",
      "openapi", "call", "batch", "events", "doctor"], "exit_codes": { "...": 0 }
  },
  "transports": {
    "openapi": "…", "sse": "…",
    "webhook": "/api/automation/webhooks", "cli": "CivitaiFreeToolCLI.exe --bridge …"
  }
}
```

OpenAPI 文档增加 `x-cft-transports`（同样从契约生成），不增加 `paths`。

## 3. CLI 传输层

### 3.1 安装与连接

- 发行包：`CivitaiFreeTool-Windows-v<version>.zip` 内与 `CivitaiFreeToolWeb.exe` 同目录的
  `CivitaiFreeToolCLI.exe`（本次并入便携 ZIP，不新增顶层发布资产）。
- 源码：`python cft_cli.py`。只依赖 Python 标准库 + `ai_contract.py`。
- 连接目标：默认 `http://127.0.0.1:47531`；`--bridge` 或环境变量 `CFT_BRIDGE` 覆盖。
- **不自动启动桌面端**。未运行时退出码 3，并给出启动提示；需要自动拉起时用 MCP 侧车的
  `--launch-app`，不在 CLI 里复制进程管理。

### 3.2 子命令

| 命令 | 对应传输面 | 说明 |
| --- | --- | --- |
| `health` | `GET /api/health` | 只探活 |
| `capabilities [--tools]` | `GET /api/automation/ai` | 能力、版本、toolsets、MCP/webhook/CLI 运行态 |
| `tools [--profile] [--format]` | `GET /api/automation/tools` | 默认 `profile=text`、`format=mcp`；不手写工具表 |
| `guide [--profile] [--markdown]` | `GET /api/automation/guide` | 与 MCP resources 同源 |
| `openapi [--profile]` | `GET /api/automation/openapi.json` | 原样输出 JSON |
| `call <tool>` | `POST /api/automation/tool/{name}` | 唯一执行入口；`--args` / `--arg` / `--format` / `--save-content` |
| `batch` | `POST /api/automation/batch` | 从文件或 stdin 读 `{calls:[…]}`；`--stop-on-error` |
| `events` | `GET /api/automation/events/stream` | SSE 跟随；`Last-Event-ID` 重连；`--max` / `--idle-timeout` |
| `doctor` | health + ai | 一次性预检：版本、契约、传输能力、MCP 运行态 |

全局：`--bridge URL`、`--timeout S`（默认 15s；工具执行默认 60s）、`--json`、`--quiet`、
`--version`。全局选项可写在子命令**前或后**（例如 `CivitaiFreeToolCLI.exe --bridge URL doctor`
与 `... doctor --bridge URL` 等价）。

### 3.3 `call` 的参数规则

- `--args '{"url":"…","wait":true}'`：JSON 对象；`--args -` 从 stdin 读。
- `--arg key=value`：可重复。值按 JSON 解析（`5`→int、`true`→bool、`["a"]`→array），
  解析失败按字符串；同一 key 出现多次合成数组。**数组/对象优先用 `--args`**，避免逗号歧义。
- `--format neutral|openai|anthropic|mcp`：映射为查询参数 `content_format`，不改变执行。
- `--save-content DIR`：把返回的图片内容块写到 `DIR/0001-<label>.<ext>`，在输出里追加
  `saved_content[]`；写盘失败只标记该条，不改变工具调用结果。
- 工具不存在：服务端 404 → 退出码 5；破坏性工具缺 `confirm:true` → 退出码 6。
- CLI 不缓存工具表，也不在本地做参数校验；`ai_contract` 只用于补全提示与 `--help`。

### 3.4 `events`

- 默认 `types=all`、`snapshot=1`、持续跟随；`--once` 不存在，用 `--max N` 或
  `--idle-timeout S` 收口。
- 断线自动重连，携带最后一条事件的 `id` 作为 `Last-Event-ID`；`--no-reconnect` 关闭。
- 输入是 NDJSON 友好的流：默认每行一条人类可读文本；`--json` 每行一个完整事件 JSON。
- `stream.reset` 原样输出；CLI 不伪造“没有事件”的结论。

### 3.5 输出与退出码

- 默认：`call`/`batch`/`openapi`/`capabilities` 输出格式化 JSON；`tools`/`events` 输出紧凑行。
- `--json`：单条命令输出紧凑 JSON；`events` 输出 NDJSON。
- `--quiet`：不写 stdout，只靠退出码；错误仍写 stderr。
- 管道/重定向时 stdout/stderr 强制 UTF-8（Windows 默认 ANSI 代码页会让 Agent 捕获到的中文乱码）；
  交互式控制台仍由 Python 使用控制台编码。
- 退出码来自 `ai_contract.CLI_EXIT_CODES`；服务端 `{ok:false,code}` 优先，HTTP 状态兜底，
  未知 code 回落 1。连接被拒/端口无监听 → 3，读超时 → 8。

### 3.6 安全边界

- 只连回环地址；CLI 不提供 `--host`/转发/代理选项，避免把本机桥变成远程入口。
- 不带 token：桥沿用现有本机信任模型；MCP HTTP 的 Bearer token 只属于 47532，不在这里复用。
- 不把工具参数或结果写进日志/历史文件；`--save-content` 是唯一显式落盘动作，由用户给目录。

## 4. webhook 出站传输层

### 4.1 配置键（走现有白名单配置面）

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `webhook_enabled` | bool | `false` | 总开关；默认关闭 |
| `webhook_url` | str | `""` | http(s) 端点；视为凭据（很多平台的 URL 路径含 token），状态/配置响应只露 scheme+host+port |
| `webhook_events` | list[str] | `[]` | 事件类型/组/`all`；空 = `WEBHOOK_DEFAULT_EVENT_GROUPS` |
| `webhook_secret` | str | `""` | HMAC 密钥；启用时必填，至少 16 字符；所有响应打码 |
| `webhook_allow_remote` | bool | `false` | 允许公网目标；默认只允许非公网地址 |

写入路径：

```bash
CivitaiFreeToolCLI.exe call cft_configure --args '{"action":"set","config":{
  "webhook_enabled":true,
  "webhook_url":"http://127.0.0.1:8799/cft-hook",
  "webhook_secret":"replace-with-a-long-random-secret",
  "webhook_events":["task","ui","config"],
  "webhook_allow_remote":false
}}'
```

`cft_configure` 是 admin 工具（29 个之一）；不新增工具。配置保存前做整条校验：非法键、非法 URL、
短密钥、未知事件名都整条拒绝（不做部分生效）。

### 4.2 投递契约

```http
POST /cft-hook HTTP/1.1
Content-Type: application/json; charset=utf-8
User-Agent: CivitaiFreeTool-Webhook/1.0
X-CFT-Delivery: 3f1c9d2e7a4b4c0e9f6a1b2c3d4e5f60
X-CFT-Event: task.finished
X-CFT-Group: task
X-CFT-Seq: 42
X-CFT-Instance: 1a2b3c4d
X-CFT-Timestamp: 1760000000
X-CFT-Signature: sha256=<hex>
X-CFT-Synthetic: 1        # 仅 POST /webhooks {"action":"test"}

{"seq":42,"instance_id":"1a2b3c4d","type":"task.finished","group":"task",
 "at":"2026-10-24T12:00:00+08:00","data":{…}}
```

- body 与 SSE 的 `data` **逐字段相同**（同一事件对象），不额外包 `event` 外壳。
- `X-CFT-Event`/`Group`/`Seq`/`Delivery` 给接收端做路由与去重；body 仍是唯一事实。
- 签名：`HMAC-SHA256(secret_utf8, f"{X-CFT-Timestamp}.".encode() + raw_body_bytes)`，
  常量时间比较；接收端应拒绝时钟偏差 > 300s 的请求，并按 `X-CFT-Delivery` 去重。
- `POST /webhooks {"action":"test"}` 发送一条**合成探针**：`type=webhook.test`、
  `group=webhook`、`seq=0`、`data={"probe":true}`，带 `X-CFT-Synthetic: 1`。
  它**不在** `ai_contract.EVENTS` 里（事件目录仍是 21 类），也不会进入 EventHub/环形缓冲；
  接收端可据此单独识别并忽略。测试探针不做事件过滤。

Python 校验示例：

```python
import hashlib, hmac
expected = hmac.new(secret.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(expected, signature_header.split("=", 1)[1])
```

### 4.3 过滤

- `webhook_events=[]` → 默认 `task` / `file_choice` / `scan` / `ui` / `config` / `app` 六组，
  不含 `api`。
- `["task.finished","ui"]` → 精确类型 + 组名都可；`["all"]` → 含 `api.call` 与 `app.stopping`。
- `stream.*` 只在 SSE 连接内生成，不经过 EventHub，因此 webhook 永远不会收到它们。
- 过滤在入队前做；被过滤的事件不占用队列、不计入 `dropped`。

### 4.4 重试与背压

| 情况 | 行为 |
| --- | --- |
| 2xx | 成功；记录 `http_status`、`ms` |
| 网络错误 / 超时 / 408 / 425 / 429 / 5xx | 重试，最多 4 次；间隔 1s / 5s / 15s |
| 3xx | **不跟随重定向**，直接失败（防止绕过 URL 校验） |
| 其他 4xx | 不重试，直接失败 |
| 队列满（256） | 丢新事件，`dropped++`，不阻塞事件总线 |
| body > 64KB | 丢弃并计入 `dropped`，`last_error` 记 body_too_large；契约事件当前远小于此 |
| 进程退出/重启 | 未投递事件丢失；webhook 不持久化 |

- **不重排 SSE 的 seq**：webhook 复用 EventHub 的 `seq`，接收端可据此判断丢失。
- **不持久化**：需要可靠重放时用 SSE + `Last-Event-ID` 或订阅 `/api/automation/events`，
  webhook 只做“尽力实时推送”。这一点必须写进用户文档，不能宣称 at-least-once。
- 投递日志环形 200 条，只记 id/type/seq/attempts/status/http_status/ms/error，**不记 body、
  不记 URL、不记 secret**；`error` 里的 URL 被替换成 `[webhook-url]`。

### 4.5 状态与测试接口

`GET /api/automation/webhooks`：

```json
{
  "ok": true, "transport": "webhook", "enabled": true, "configured": true,
  "url": {"scheme": "http", "host": "127.0.0.1", "port": 8799, "path_set": true},
  "events": ["task", "ui", "config"], "allow_remote": false, "secret_set": true,
  "attached": true,
  "stats": {"accepted": 12, "sent": 11, "failed": 1, "dropped": 0, "retried": 2,
            "pending": 0, "in_flight": 0, "last_error": "", "last_success_at": "…"},
  "signature": {"algorithm": "HMAC-SHA256", "header": "X-CFT-Signature",
                "signed": "<timestamp>.<raw body>"},
  "limits": {…}, "deliveries": [{"id": "…", "event": "task.finished", "seq": 42,
                "status": "sent", "attempts": 1, "http_status": 200, "ms": 12, "at": "…"}],
  "config_error": ""
}
```

- 永不返回 `webhook_url` 的 path/query、`webhook_secret`。
- `config_error`：配置文件被手工改坏时的最近一次校验错误；此时投递自动停用，接口仍可用。

`POST /api/automation/webhooks`：

| action | 作用 |
| --- | --- |
| `test` | 发送 `webhook.test` 合成探针；默认等待投递结束，`timeout`（0.5–30s）可收口 |
| `clear-log` | 清空投递日志（不改变统计累计值） |
| `refresh` | 从当前配置重新加载（手工改配置文件/长尾 RPC 改配置后使用） |

示例：

```bash
CivitaiFreeToolCLI.exe call cft_capabilities            # 看 caps.webhook.enabled
curl -s http://127.0.0.1:47531/api/automation/webhooks
curl -s -X POST http://127.0.0.1:47531/api/automation/webhooks \
  -H 'Content-Type: application/json' -d '{"action":"test"}'
```

### 4.6 URL / SSRF / 重定向策略

- 只接受 `http`/`https`；必须有 host；端口 1–65535；**拒绝 URL userinfo**
  （`http://user:pass@host`）与 fragment。
- `webhook_allow_remote=false`（默认）：DNS 解析出的**每个**地址都必须是
  `ipaddress.is_global == False` 的非公网地址（回环、RFC1918、链路本地、CGNAT、ULA）。
  解析失败直接拒绝。这样本机 n8n/Dify 能用，公网 SaaS 默认不能用。
- `webhook_allow_remote=true`：允许公网目标，属于显式数据出境选择；文档必须提示事件里可能含
  本地文件名/落点路径。
- 不跟随重定向；端点必须是最终地址。
- 不使用系统代理（`ProxyHandler({})`），避免“本机 webhook”被代理带到外网。
- 已知限制：预检 DNS + 直连原 URL 仍存在 DNS rebinding 窗口；本项目信任本机配置者，
  不把 webhook 目标当作不可信输入。需要更强保证时用固定 IP + Host 头/自签证书，另立项。

### 4.7 生命周期

- `browser_bridge.start()`：先配置 EventHub，再把 `_api.cfg` 交给 webhook 刷新；
  webhook 作为 **sink** 挂到 hub，sink 也能维持 1 秒采样线程（没有 SSE 订阅者时仍能推送）。
- `POST /api/automation/config`（含 `cft_configure`）：先整条校验，成功保存后立即刷新；
  关闭时 detach sink，不留下空转线程。
- 手工改配置文件/长尾 RPC 改配置后，用 `POST /webhooks {"action":"refresh"}` 显式重载。
  **`GET /webhooks` 只读**：不在读取时重做 DNS/配置校验，也不丢队列。
- `browser_bridge.stop()`：`automation_events.shutdown()` 先发布 `app.stopping`，
  再 `webhook_dispatch.shutdown(flush_s=1.0)` 尽力把最后一条送出去（慢端点仍可能错过，以尽力而为，
  不宣称可靠）。
- sink 在内存锁内做 O(1) 入队并立即返回，异常被隔离；webhook 故障绝不影响下载/UI/SSE。

## 5. 测试与验收

| 层 | 用例 |
| --- | --- |
| 契约 | 仍是 29 工具 / 21 事件；`CLI_EXIT_CODES` / `WEBHOOK_LIMITS` / 默认组形状稳定 |
| EventHub | sink 收到事件；sink 单独存在时采样不退出；sink 异常不影响 publish；移除后按 idle 停采样 |
| webhook 单元 | URL/密钥/事件名校验；userinfo/fragment/非 http 拒绝；公网默认拒绝、显式允许；HMAC 正确；过滤；队列满丢弃；重定向不跟随；日志/状态脱敏 |
| webhook 集成 | 本机接收端 500→500→200 重试成功；400 不重试；`test` 合成事件头/签名/body schema；关闭后不再投递 |
| CLI 单元 | 本机 fake bridge：参数类型、`--args` 合并、工具别名、错误退出码、app 不在线、SSE `--max`、NDJSON、batch、doctor |
| 真机 | 源码桥：CLI `call cft_capabilities`、`events --max 1`、webhook `test` 收到并通过 HMAC 校验；打包：CLI EXE 对打包 Web EXE 重复上述链路 |
| 文档 | 本文与 `ai-integration.md` / `automation-api.md` / `README` / Skill recipes 同步；未验证项明确标注 |

### 5.1 本轮真机验收（2026-10-24，复核修复后重跑）

隔离数据目录，避开用户正在运行的 47531；源码实例与打包实例都监听 **47541**：

| 项 | 源码实例 | 打包 Web EXE |
| --- | --- | --- |
| `CivitaiFreeToolCLI.exe doctor` | ✅ 全绿（health/openapi/sse/webhook/cli/29 工具/21 事件） | ✅ 同样全绿 |
| `call cft_capabilities` | ✅ `caps.webhook/cli.available=true` | ✅ 同样为真 |
| `events --max 2` | ✅ `stream.ready` + `state.snapshot` | ✅ 同样两帧 |
| `POST /webhooks {"action":"test"}` | ✅ 本机接收端收到 `webhook.test`，HMAC 校验通过，状态响应无 secret/URL 路径 | ✅ 同样通过 |
| SSE / OpenAPI 回归 | ✅ 19 operations、`api.call` 实时、别名 GET 405 | ✅ 同样通过 |
| 脱敏与配置差分 | ✅ `GET /config`、保存响应、`GET /webhooks` 均无 secret/URL 路径；`config.changed.keys` 含 4 个 `webhook_*` 键 | ✅ 同样通过 |

- 本节是**手工验收**（非 CI）：打包 Web EXE 在隔离副本中构建，差异只有两处隔离改动——
  `browser_bridge.DEFAULT_PORT` 改为 47541、`main_web.py` 的单实例互斥体改名，以避开用户正在
  运行的 47531 实例；没有替换/停止用户的 47531 实例。隔离副本与构建产物是临时验证物，不随仓库发布。
- `CivitaiFreeToolCLI.exe` 为 PyInstaller onefile（约 8.8MB），分别对源码桥与打包 Web EXE 跑过
  `doctor` / `call` / `events`。
- **未制造**真实下载/多文件待选来触发 `task.*` / `file_choice.*` webhook；这两类由事件总线
  单测与 `webhook.test` 探针覆盖，不声称已做真机下载推送。
- Dify/n8n 等第三方平台仍不做导入验收；本机接收端只验证传输契约（头、签名、状态码、重试）。

## 6. 非目标（本阶段不做）

- A2A、入站 webhook（外部回调驱动桌面端）、WebSocket、公网隧道。
- webhook 的 GUI 设置页（本期走配置白名单 + CLI/HTTP；需要时单独立项）。
- 持久化投递队列 / 跨重启重放 / exactly-once。
- 统一 token 鉴权（沿用现有本机信任模型）。
- 在 CLI 里自动启动/停止桌面端或 MCP 侧车。

## 7. English summary

This phase adds the last two transports on the AI surface without touching the contract:
a stdlib-only **CLI** that speaks the existing local bridge (`/api/automation/tool/{name}`,
`/events/stream`), and an opt-in **outbound webhook** that pushes the same 21 contract
events to a user-configured endpoint with HMAC-SHA256 signatures, bounded retries,
non-blocking delivery and a redacted delivery log. The CLI is a client only; the webhook
is a sink on the existing EventHub. Neither adds tools, events, or a second execution
path. Webhooks are disabled by default, reject redirects, and only allow non-public
destinations unless `webhook_allow_remote=true` is explicitly set.
