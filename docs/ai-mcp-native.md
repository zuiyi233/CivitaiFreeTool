# AI 原生接入（MCP 常驻 · 落点可判定 · 扩展免打包）

> 状态：设计先行，随后按 P1→P4 落地。唯一事实源仍是 `ai_contract.py`；
> MCP / HTTP / OpenAPI 都只是它的投影，本文不新增第二套工具表。

## 目标

让 AI 只用本机回环接口就能完成「在已打开的浏览器里找模型 → 下载 → 落到正确目录 → 校验」，
并把扩展开发从「改一次、打一次包、再导入一次」变成「改源码、自动重载」。

三条可验收的结果：

1. 桌面端一启动，MCP 就已经可用（HTTP + Bearer），不需要用户另开控制台窗口；
   AI 侧有 `scripts/mcp_setup.py` 生成/合并客户端配置。
2. 每次下载的结果都带 `placement`：最终目录、是否在模型根内、按类型分到哪一层、
   没分流时的可执行修复建议。**只报告，不自动搬文件。**
3. 扩展只加载一次；之后改 `chrome-extension/` 源码，`dev_extension.py watch`
   让已加载的扩展在下次心跳内自行重载，不再压缩包、不再重新导入。

## 现状审计（避免重复造）

| 能力 | 现状 | 位置 |
| --- | --- | --- |
| MCP 工具面 | 已存在 29 个工具（core 19 / vision 5 / admin 5），stdio + Streamable HTTP + Bearer | `ai_contract.py`、`mcp_server.py` |
| 已打开标签页 | 扩展只读上报，`cft_browser_tabs` 可搜索；HTTP-only 已验证 | `browser_bridge.py`、`lib/tab-index.js` |
| 下载分流 | 按 Civitai 类型 → ComfyUI 核心目录；不存在的类型目录绝不新建；失败回落 | `model_routing.py`、`webui._route_dest_dir` |
| 落点元数据 | 任务/历史已有 `routed_dir`、`route_reason`、`route_source` | `downloader.py`、`webui.py` |
| MCP 客户端片段 | `assets/mcp-claude.json` / `mcp-cursor.json` / `mcp-qoder.json` | `skills/civitai-free-tool/assets/` |
| 扩展开发 | 目前靠复制/解压到 Release 目录后手动重载 | 本次改造 |

因此本次不是「从零 MCP 化」，而是补齐三处断点：**常驻入口、落点可判定、开发回路**。

## 差距与决定

### 1. 扩展本身不做 MCP Server

MV3/Firefox 扩展没有监听本地端口的能力；把扩展做成独立 MCP Server 只能引入
native messaging 或外部中继，等于第二套契约、第二个安全边界。

**决定**：扩展是**数据面/执行面**，桌面端是唯一 MCP 面。扩展的数据（标签页、下载状态、
归档通道、版本）经回环桥进入同一个契约；AI 只看见一套工具。这样不会出现
「MCP 和扩展各有一份工具表」的漂移。

后续如果要把「一键归档」等纯扩展动作也交给 AI，走**命令队列**（桥下发、扩展轮询执行、
结果回传），仍然不新增第二套 MCP Server；本次不实现，列为 P5 候选。

### 2. MCP 常驻而不是让用户手开侧车

现在要用户自己运行 `CivitaiFreeToolMCP.exe --http`；对「让 AI 方便调用」来说这一步太多。

**决定**：
- 冻结版桌面端启动后，若同目录存在 `CivitaiFreeToolMCP.exe` 且 47532 未被占用，
  由 `mcp_autostart.py` 以 `--http --toolsets all --exit-with-bridge` 启动；
  失败只写诊断状态，不阻塞、不弹窗、不写日志中的 token。
- `--exit-with-bridge` 是侧车自带的看门狗：桥连续不可达约 30s 就退出，避免桌面端崩溃后
  留下孤儿进程占端口。
- 源码运行不自动拉起（测试/开发不产生后台进程）；开发者可显式 `python mcp_server.py --http`。
- stdio 仍然是「客户端自己拉起侧车」的标准方式，与常驻 HTTP 并存，两个入口共用同一契约。

### 3. 下载「正确位置」必须可解释、可验证

分流已经实现了，但 AI 看不到「这次到底分没分、为什么、在不在模型根里」，
于是只能猜；用户看到的就是「文件又落在大目录根部」。

**决定**：
- 保持既有硬约束：类型目录不存在/不可写 → 不新建、不分流、如实回落；
  下载完成后**绝不自动移动**模型文件（移动会破坏任务、历史、ComfyUI 引用）。
- 在计划与结果里增加只读的 `placement`：
  `dest_dir / routed_dir / folder / under_models_root / status / hint`；
  `status ∈ routed | under_root | outside_root | pending`。
- `cft_diagnostics(source=storage)` 增加 `candidates`：从已配置的 `models_dir` /
  `download_dir` / `download_target_dir` 自身或上级目录里，找出包含多个 ComfyUI
  核心类型目录的路径，作为 `comfyui_models_root` 的建议值。**只建议，不自动写配置。**
- 修正建议统一指向已有工具：`cft_configure`（开分流 / 设根目录）或
  `cft_download_model(dest_subdir=...)`，不新增「设置落点」工具。

### 4. 开发态免打包的最小闭环

浏览器安全模型决定了「加载未打包扩展」必须人工做一次；能消除的是之后每次的
复制、压缩、重新导入。

**决定**：
- `scripts/dev_extension.py link` 把已加载目录换成指向源码的 junction（Windows）/
  symlink（POSIX），manifest `key` 不变 → 扩展 ID 不变；旧目录先备份，可 `restore`。
- `link` 之后，源码改动已经在浏览器看到的目录里；剩下只需要「重载」。
- 重载自动化：桥在 `/api/ext/tabs` 的上报响应里携带 `dev_reload_at`；扩展只有发现
  新时间戳才 `runtime.reload()`，已见时间戳存 `storage.local`，重载后不会循环。
  `dev_extension.py watch` 监测源码 mtime 并 POST `/api/ext/dev/reload`。
- 扩展重载后 `pushed_at` 会更新；`watch` 用它确认「新代码已经跑起来」。
- 内容脚本仍要求刷新对应页面（浏览器不会把新 content script 注入已打开页面）；
  这条限制写进文档，不假装解决。

## 架构

```
AI Client ──stdio──> CivitaiFreeToolMCP.exe ─┐
          └─HTTP───> 127.0.0.1:47532 (Bearer) ─┤ 同一 ai_contract
                                               ↓
                                   POST /api/automation/tool
                                               ↓
桌面端 Web EXE ── 本机桥 127.0.0.1:47531 ── 下载/分流/校验/库
        ↑
        │ /api/ext/tabs（只读索引） / api/ext/dev/*（开发态）
        │
浏览器扩展（tabs 权限；不上报正文/Cookie/API Key）

开发回路：dev_extension.py watch → POST /api/ext/dev/reload
        → 扩展下次心跳收到 dev_reload_at → runtime.reload()
```

## 分阶段与验收

- **P1 MCP 常驻 + 一键接入**
  - 冻结版启动后 47532 可连（未带 token 返回 401）；桌面端退出后侧车自杀式退出
    （看门狗 10s×3 次，实测停止后 45s 内端口已无监听）。
  - `scripts/mcp_setup.py --client cursor --apply` 合并配置且保留其他 server；token 不出现在 stdout。
  - `cft_capabilities` 返回 `extension`（在线/版本/索引新鲜度）与 `mcp.http_running`、`mcp.token_file`。
- **P2 落点可判定**
  - 三种 placement 各有单测；不改变 `route_by_type` 默认值（仍为 false）与「不建类型目录」约束。
  - `cft_diagnostics(source=storage)` 能在临时目录里识别候选根。
- **P3 扩展免打包**
  - `link` 后加载路径是 junction 且 manifest 版本/`key` 不变；`restore` 能还原。
  - 桥 dev 接口：扩展来源/本机工具可触发，网页来源 403；响应只带时间戳，不带数据。
  - 扩展开启 dev 重载后，同一 `dev_reload_at` 只触发一次 `runtime.reload()`。
- **P4 回归与交付**
  - 全量 pytest、扩展 CJS 测试、两个 EXE 重打；隔离启动的打包验收若被用户正在使用的
    实例占用端口，则如实报告未验项，不用「测试通过」代替。
  - 本轮 2.7.0 的这条缺口已在桌面实例停止、47531 空闲后补做并转为真机项：最终包隔离启动、
    MCP 自启动与自杀、5 个前端资源字节一致、413/429、配置往返、只读冒烟、dev 接口来源校验
    逐项复验，明细见 `docs/releases/v2.7.0.md`。

## 安全边界

- 所有新接口只监听 `127.0.0.1`；MCP HTTP 继续要求 Bearer，token 只落在用户数据目录，
  不写日志、不进仓库、不在对话里回显。
- 扩展仍然只上报模型页 `title/url/id` 与 `ext_version`；不读正文、Cookie、storage。
- 开发接口只改变「扩展何时重载」，不执行任意代码、不读写页面数据；网页来源一律拒绝。
- `link` 只操作仓库内 `.tools/` 下的加载副本，先备份、可恢复；不碰用户正式安装目录。
- 下载落点诊断只读；不自动改配置、不自动搬文件、不新建未注册的类型目录。
- `mcp_autostart` 只启动同目录的官方侧车，不拼 shell、不接受外部命令；端口被占用时直接放弃。

## 已知问题与优化（本次处理）

| 问题 | 处理 |
| --- | --- |
| MCP 侧车需手动启动 | 冻结版自启动 + 桥看门狗，失败静默 |
| 双开/端口被占 | 先探测 47532，占用则跳过；侧车绑定失败即退出 |
| 桌面端崩溃留孤儿 | `--exit-with-bridge` 连续 3 次健康检查失败退出 |
| token 泄露面 | 只从文件读，不打印；setup 写客户端配置时不回显 |
| AI 不知道扩展是否在 | capabilities 带扩展在线/版本/索引新鲜度 |
| 分流失败无声 | placement.hint + storage candidates，指向 `cft_configure`/`dest_subdir` |
| 误搬模型 | 明确不自动移动；只报告 |
| 扩展每次重导入 | junction + dev 重载；扩展 ID 由 manifest key 固定 |
| dev 重载死循环 | storage 记录已见时间戳 |
| 内容脚本未更新 | 文档明确要求刷新页面 |
| 契约漂移 | 不新增工具，只扩展现有返回字段；测试仍以 `ai_contract` 为准 |

## 非目标

- 不做远程访问、公网 MCP、云同步。
- 不做扩展的独立 MCP Server / native messaging 桥。
- 不自动启用类型分流、不自动改用户配置、不自动移动已下载文件。
- 不做扩展热更新到 release ZIP；发布仍走既有打包。

## English summary

This change closes three gaps rather than rebuilding the AI surface: (1) the packaged
desktop app now auto-starts the MCP sidecar over loopback HTTP with a bearer token and
a bridge-liveness watchdog, plus a setup script that merges client configs; (2) download
results expose a read-only `placement` block and storage diagnostics suggest a ComfyUI
models root — the app never moves files or creates unregistered type directories;
(3) a dev junction plus a bridge-driven reload flag let an already-loaded extension pick
up source edits without repacking, using a persisted timestamp to avoid reload loops.
The extension stays a data plane; `ai_contract.py` remains the only tool contract.
