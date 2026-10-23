# 扩展「模型下载状态」设计与契约（v1）

**English summary.** The desktop app becomes the single source of truth for “do I already have this
model version, is it downloading, and is there a newer same-base version?”. The Chrome/Firefox
extension queries a new local endpoint `POST /api/model/status` in batch, caches the answer in
`chrome.storage.local`, and uses it in four places: a page-level status chip on Civitai model pages,
per-tab toolbar badges, download-state badges/filters in the popup, and an explicit “download the
newer version” action. The desktop remains the only component that touches the library, update
cache and download queue; the extension never guesses local state, never reads cookies/API keys,
and never talks to any host other than the hard-coded `127.0.0.1:47531`.

## 1. 目标与非目标

### 目标

1. **记录已下载**：以桌面端为准，回答「这个模型/版本在本机有没有、在哪、是哪个版本」。
   来源合并模型库扫描、下载历史、活动任务队列，而不是只看扩展自己发过什么。
2. **读取页面状态**：插件读取当前浏览器实例里打开的 C 站模型页，给每一页标出
   已下载 / 有更新 / 下载中 / 未下载 / 已下载其他版本 / 状态未知。
3. **明显提示**：页面内胶囊、每标签工具栏徽章、弹层行徽章+筛选+汇总，三处一致。
4. **闭合动作**：已下载的行发送前明确提示；有更新的行可以一键把**新版**（而不是页面里的旧版）
   发给 CFT。

### 非目标（本版不做）

- 不把扩展变成第二个模型库：不写模型文件、不扫描硬盘、不缓存模型元数据之外的状态。
- 不在扩展里重新实现「更新判定」规则。更新只看桌面端 `model_updates.json` 的结果；
  没检查过就如实显示「更新状态未检查」，不猜「无更新」。
- 不做跨设备/账号同步；状态只存在本机扩展 storage 与桌面端进程。
- 不因为「已下载」静默阻止用户重下；只提示并让用户确认。

## 2. 状态词表

| state | 弹层/页面文案 | 工具栏徽章 | 含义 | 可信来源 |
|---|---|---|---|---|
| `downloaded` | ✓ 已下载 | `✓` 绿 | 请求的**这个版本**已在本机，且没有同底模新版 | 模型库扫描 或 已完成下载历史（文件仍在） |
| `update_available` | ⬆ 有更新 | `↑` 橙 | 请求的这个版本已在本机，桌面端更新缓存判定有同底模新版 | 模型库/历史 + `model_updates.json` |
| `downloading` | ⏳ 下载中 42% | `…` 蓝 | 队列里有这个版本（或该模型其他版本，`version_matched=false`）的活动任务 | `Downloader.tasks` |
| `other_version` | ✓ 已下载其他版本 | `◐` 灰 | 本机有这个模型的其他版本，但没有页面正在看的版本 | 模型库/历史 |
| `not_downloaded` | ↓ 未下载 | `↓` 灰 | 模型库已扫描完成，且库/历史/任务都没有命中 | 三者全空 + `library.ready=true` |
| `unknown` | ? 状态未知 | `?` 灰 | 无法判定：桌面端离线、库未扫描、缺模型/版本 ID、扫描根不可达 | 扩展层/接口 `reason` |

**诚实口径**

- 桌面端离线 ≠ 未下载。扩展保留最后一次缓存状态并标「离线缓存 · 检查于 HH:MM」。
- 模型库未扫描完成 ≠ 未下载。此时没有正面命中返回 `unknown`，有历史/任务命中照常返回正面状态。
- `update` 为 `null` 只表示「没有这条更新记录」，不等于「已是最新」。只有
  `updates.checked_at > 0` 且该模型有记录时才能显示「已是最新」。
- `other_base`（最新版换了底模）不算更新，和桌面端既有规则一致；只在详情里说明。

## 3. 桌面端数据源与优先级

| 来源 | 字段 | 用途 | 备注 |
|---|---|---|---|
| `Api.model_rows`（模型库扫描） | `path/modelId/verId/ver/base/info` | 精确版本命中、其他版本、本地路径 | 只在配置的模型根内；`verId` 缺失时读 `info.versionId` / `info.version.id` |
| `Downloader.tasks` | `info.model_id/version_id/status/progress` | 下载中 | 仅 pending/downloading/retrying/paused |
| `Downloader.history` | `status=done`、`file_path` | 文件已移出模型库根、或改过名时的历史命中 | 本版给历史行补 `model_id/version_id/model_url`；旧行从 `history_assets/<id>.json` 快照回填 |
| `model_updates.json` | `items[path].has_update/latest_version/...` | 有更新 | 按 `(model_id, local_version)` 建索引；不重新联网判定 |
| `mm_scan_state` / `_models_roots` | `running/msg/roots` | `library.ready/scanning` | 未就绪时缺席不判“未下载” |

**状态优先级**（从高到低）

1. 该版本有活动任务 → `downloading`（`version_matched=true`）。
2. 该版本在模型库/历史中且文件存在：
   - 更新记录 `has_update=true` → `update_available`
   - 否则 → `downloaded`
3. 该模型有其他活动任务 → `downloading`（`version_matched=false`）。
4. 该模型有其他本地版本 → `other_version`。
5. 缺失且 `library.ready=false` → `unknown`。
6. → `not_downloaded`。

> 历史状态为 `done` 但 `file_path` 已不存在：不算已下载，附 `history.file_exists=false` 与
> `reason`，让界面能说「有下载历史，但文件已不在」。

## 4. 桥接口 `POST /api/model/status`

随 CFT **v2.7.0** 提供。更旧的桌面端没有这个路由，扩展会显示「桌面端版本过旧」并继续允许普通发送，不会把 404 当成“未下载”。

- 只注册给本机扩展/本机页面；CORS 与现有 `/api/*` 相同（`chrome-extension://`、`moz-extension://`、
  `127.0.0.1/localhost`、`file://`/`null`）。不新增鉴权，也不暴露到公网。
- 请求体上限 64KB；`items` 最多 500 项，超出 400。
- 不联网、不触发 Civitai API；只读进程内缓存与本地文件存在性。模型库未扫描时最多触发一次
  后台 `scan_models()`（60 秒节流），并如实返回 `library.scanning=true`。

请求：

```json
{
  "items": [
    {"model_id": "101", "version_id": "1001", "url": "https://civitai.red/models/101"},
    {"model_id": "202"}
  ],
  "include_updates": true
}
```

- `model_id` / `version_id` 接受数字或数字字符串；`url` 可选，服务端按
  `/models/<mid>`、`?modelVersionId=<vid>`、`/api/download/models/<vid>` 回退解析。
- 两项 ID 都缺 → 该条 `state=unknown, reason="缺少模型 ID"`，不影响其他条目。

响应（节选）：

```json
{
  "ok": true,
  "generated_at": 1760000000,
  "library": {
    "ready": true, "scanning": false, "count": 42,
    "roots": [{"path": "D:\\C站", "status": "ok", "error": ""}]
  },
  "updates": {"checked_at": 1760000000, "available": true, "records": 12},
  "items": [
    {
      "model_id": "101", "version_id": "1001",
      "state": "update_available", "version_matched": true,
      "local": {
        "source": "library", "version_id": "1001", "version_name": "v1.0",
        "model_name": "Alpha LoRA", "base_model": "SDXL",
        "path": "D:\\C站\\loras\\alpha\\alpha.safetensors",
        "paths": ["D:\\C站\\loras\\alpha\\alpha.safetensors"],
        "file_count": 1, "size": 153600000, "exists": true
      },
      "local_versions": [
        {"version_id": "1001", "version_name": "v1.0", "source": "library",
         "path": "D:\\C站\\loras\\alpha\\alpha.safetensors", "exists": true}
      ],
      "download": null,
      "history": null,
      "update": {
        "known": true, "has_update": true, "other_base": false, "unknown_local": false,
        "local_version": "1001", "local_name": "v1.0",
        "latest_version": "1002", "latest_name": "v1.2",
        "latest_url": "https://civitai.red/models/101?modelVersionId=1002",
        "behind": 1, "checked_at": 1760000000,
        "newer": [{"id": "1002", "name": "v1.2", "date": "2026-10-01",
                   "url": "https://civitai.red/models/101?modelVersionId=1002"}]
      },
      "reason": ""
    }
  ]
}
```

补充字段：

- `download`: `{status, progress, task_id, filename, dest_dir, version_matched, task_count}`。
- `history`: `{task_id, finished_at, file_path, file_exists}`。
- `update` 为 `null` 时：该条没有更新记录；界面用 `updates.checked_at` 决定说
  「还没在 CFT 里检查更新」还是「已检查，但没有这个模型的记录」。
- `library.ready=false` 且没有正面命中：`state=unknown`，`reason` 说明是扫描中/未配置根/根不可达。

错误：400（JSON/参数非法、超 500 项）、500（`状态服务未就绪`）、200 + 逐条 `unknown`（可预期缺数据）。

## 5. 扩展架构

```
chrome-extension/
  lib/model-status.js        # 纯逻辑：归一化/标签/徽章/汇总/缓存合并/请求项构造（新增）
  background.js              # 查询桥、缓存、提交即标下载中、每标签徽章（改）
  content/model-page.js      # 页面状态胶囊 + 刷新（改）
  content/content.css        # 胶囊样式（改）
  popup.html/.js/.css        # 行徽章、筛选、汇总、只选未下载、下载新版（改）
```

### 5.1 storage

`chrome.storage.local` key：`cftModelStatusV1`

```json
{
  "version": 1, "savedAt": 1760000000,
  "entries": {
    "version:1001": {
      "key": "version:1001", "modelId": "101", "versionId": "1001",
      "state": "downloaded", "checkedAt": 1760000000, "seenAt": 1760000000,
      "seenUrls": ["https://civitai.red/models/101?modelVersionId=1001"],
      "local": {}, "local_versions": [], "download": null, "history": null, "update": null,
      "reason": "", "offline": false
    }
  }
}
```

- 同一模型/版本只有一个 key；有版本用 `version:<vid>`，没有版本用 `model:<mid>`。
- 最多 1000 条，按 `seenAt` LRU；`seenUrls` 最多 5 条（记录“哪些页面看过这个模型”，仅本地）。
- `state=downloading` 的缓存 2 分钟内有效，其余 5 分钟；超时仍显示，但加「状态可能过期」。
- 离线失败**不清空**缓存，只把 `offline=true` 写回本次返回。

### 5.2 消息

| from → to | type | payload → 响应 |
|---|---|---|
| popup/content → bg | `modelStatusQuery` | `{items:[{modelId,versionId,url,tabId?,key?}], refresh:true\|false\|'auto'}` → `{ok, items, cacheAt, offline, library, updates, error}` |
| popup/content → bg | `modelStatusCache` | `{keys?}` → `{ok, at, entries}`（只读，不发网络） |
| bg → content | `modelStatusPush` | `{entry}`（后台刷新后即时更新页面胶囊） |

- `refresh=true` 强制打桥；`false` 只读缓存；`'auto'`（默认）在缓存缺失/过期时打桥。
- bg 对同一批请求做 1.5 秒内的合并，避免 popup 与多个页面同时刷新打爆本机桥。
- `items` 里的 `tabId` 用于设置该标签徽章；content 消息缺省用 `sender.tab.id`。

### 5.3 工具栏徽章

- `chrome.action.setBadgeText({tabId, text})`：`✓`（绿）/`↑`（橙）/`…`（蓝）/`◐`、`↓`（灰）/`?`（灰）。
- `setTitle({tabId})` 写完整解释（本地版本、最新版本、路径、检查时间）。
- **不覆盖批量归档进度**：`archiveBatchStart` 成功后清空各标签状态徽章并挂起状态徽章；
  批量状态变为非 running 后从缓存重新挂。
- `tabs.onUpdated`：URL 变成/离开 C 站模型页时，从缓存补/清徽章（不发网络）。
- `runtime.onStartup`：枚举所有模型标签，从缓存补徽章。
- 提交成功（popup 批量发送、归档 `sendModelToSoftware`）立即写一条
  `state=downloading, download.status=pending`，让用户在 CFT 解析前就看到反馈。

### 5.4 页面胶囊（`/models/*`）

位置：右下角归档按钮正上方（`bottom:68px; right:16px`），与按钮同宽视觉体系，不遮挡站点内容。

| 状态 | 文案 | 点击 |
|---|---|---|
| downloaded | `✓ 已下载 · v1.0` | 强制刷新 |
| update_available | `⬆ 有更新 · v1.0 → v1.2` | 强制刷新；title 提示去弹层「下载新版」 |
| downloading | `⏳ 下载中 42%` | 强制刷新 |
| other_version | `✓ 已下载其他版本 · 2 个` | 强制刷新 |
| not_downloaded | `↓ 未下载` | 强制刷新 |
| unknown 离线 | `? 离线 · 上次 ✓ 已下载` | 强制刷新 |
| unknown 无 ID | `? 未识别模型 ID` | 无 |

- 页面加载时先读缓存立即渲染；缓存过期才联网。
- SPA 路由切换后重挂并重新查询；版本 ID 先从 URL，再从下载链接/评价链接/代码块提取，
  仍取不到就按模型级查询。
- `archiveDone` 成功后 1 秒强制刷新。
- `chrome.storage.onChanged` 监听到 `cftModelStatusV1` 变化时，用缓存更新胶囊（不联网）。

### 5.5 弹层

- 行徽章：`✓ 已下载` / `⬆ 有更新` / `⏳ 下载中 42%` / `◐ 其他版本` / `↓ 未下载` / `? 未知`；
  small 行补充 `本地 v1.0` / `最新 v1.2`。
- 新筛选项「下载状态」：全部 / 已下载 / 有更新 / 下载中 / 已下载其他版本 / 未下载 / 未知。
- 汇总行：`下载状态：已下载 3 · 有更新 1 · 下载中 1 · 未下载 4 · 未知 2 · 检查于 12:34`
  或「未连接 CFT，显示上次缓存（不是“没有下载”）」。
- 操作：
  - 「只选未下载」：只选 `not_downloaded` 的行（`unknown` 不动）。
  - 「⬆ 下载选中项的新版（N）」：仅对选中有 `update.latest_url` 的行发送**新版 URL**。
  - 普通发送前若选中项含 `downloaded`（无更新）或 `update_available`，弹确认说明：
    前者会重下同一版本；后者普通发送仍会发页面里的旧版本，建议用「下载新版」。
    确认后照常发送，不静默改 URL、不静默跳过。
- 打开弹层：先渲染缓存，再对可见模型行批量 `modelStatusQuery`（auto），有 `library.scanning`
  或过期时最多重试 3 次（2.5s 间隔）。
- 「发送到 CFT 之后」轮询到终态（done/library/failed）后，触发一次状态刷新。

## 6. 隐私与安全

- 发往桌面端的只有：模型 ID、版本 ID、可选的页面 URL（已是扩展清洗过的无 token 形态）。
- **不发送** Cookie、API Key、浏览历史；不读取页面正文；C 站接口请求仍由现有归档通道按需发起。
- 扩展只连接硬编码 `http://127.0.0.1:47531`；桥仍只绑定 127.0.0.1，CORS 白名单不变。
- 状态缓存只存模型 ID/版本 ID/状态/本地路径展示文本/见过的页面 URL（最多 5 条），可在
  `chrome://extensions` 清除扩展存储时一并删除。
- 桌面端接口不返回 API Key、任务签名下载 URL；`latest_url` 是公开模型页 URL。

## 7. Firefox 范围

Firefox 版没有 content script / offscreen，本版只做弹层状态：
`lib/model-status.js` 与 Chrome 逐字节相同；popup 直接 `POST /api/model/status`，缓存同一 key，
提供行徽章、下载状态筛选、汇总、「只选未下载」与「下载新版」。不提供页面胶囊与工具栏徽章，
README 明确这条能力边界。

## 8. 验收与测试

- Python：`tests/test_model_status.py`（状态优先级/历史/更新/未扫描/URL 解析）、
  `test_browser_bridge.py`（端点/CORS/参数）、`test_download_history.py`（历史字段持久化）。
- JS：`chrome-extension/lib/model-status.test.cjs`（标签/汇总/缓存/过期）、
  `tests/test_model_status_background.cjs`（bg 在线/离线/旧桌面端/缓存消息/提交即标下载中）、
  `tests/test_model_status_ui.cjs`（真 Chromium：弹层徽章/筛选/只选未下载/新版发送 + content 胶囊/点击刷新/storage 推送/路由移除）、
  `tests/test_firefox_parity.cjs`（共享模块逐字节 + Firefox 弹层状态/离线缓存）、
  `tests/test_popup_integration.cjs` 与 `tests/test_extension_batch.cjs`（原有列表/发送/任务区不退化）。
- 手工/真浏览器：模型页胶囊可见不遮挡；popup 打开后 3 秒内出现状态；关闭 CFT 后显示离线缓存
  而不是“未下载”；有更新的行能发出 `modelVersionId=<新版本>`。

## 9. 后续方向（本版不做）

按价值/成本排序，供下一阶段选择：

1. **C 站列表页/图片页覆盖**：在 `/models` 列表卡片和 `/images/*` 的资源模型列表上直接标
   ✓/↓/↑，浏览时就能筛掉已下载的模型；复用同一状态缓存。
2. **Side Panel 常驻工作台**：用 `chrome.sidePanel` 把当前弹层变成侧栏，标签页状态实时刷新，
   适合一次开几十个模型页的批量场景。
3. **一键检查这些模型的更新**：对打开页面涉及的本地模型批量触发桌面端 `mm_check_updates`
   （需要 API Key），完成后自动刷新状态；比用户去 CFT 更新页逐个点更顺。
4. **下载/归档完成通知 + 打开所在目录**：`chrome.notifications` + 桌面桥新增只读的
   “打开已登记路径”入口（仅扩展来源、仅限已登记落点），完成后直接跳到文件夹。
5. **哈希级去重**：模型库按 SHA256 识别“改名/换目录后的同一个文件”，状态从版本 ID 命中
   升级为内容命中，减少“版本 ID 丢失但文件其实在”的未知。
6. **页面级历史**：记录访问过的模型页（仅本地、可一键清除），提供“看过但没下载”清单；
   与下载历史相互独立，清理一边不影响另一边。
