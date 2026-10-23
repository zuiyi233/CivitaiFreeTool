# 浏览器标签页索引桥（Open model tabs index）

## 目标

让本地 Agent / 脚本在**不模拟键鼠、不打开扩展弹窗**的前提下，查询已打开浏览器里的
Civitai 模型页，并把找到的页面交给现有下载自动化工具。

一句话链路：

```
扩展 background（已有 tabs 权限）
  → POST /api/ext/tabs（只推模型页标题/URL/ID）
  → 桌面桥内存索引（不落盘）
  → cft_browser_tabs（只读工具）
  → cft_download_model（已有下载工具）
```

## 非目标

- 不读取网页正文、Cookie、API Key、浏览器历史文件或非 Civitai 页面。
- 不新增下载器；下载仍走 `POST /api/download`（扩展 UI）或 `cft_download_model`（自动化面）。
- 不提供远程访问；索引只在 `127.0.0.1` 回环桥上。
- 不替代扩展已有的「批量标签页」UI；UI 仍然可用，桥只是给 Agent 增加一条只读数据面。

## 数据流与新鲜度

1. 扩展在 `tabs.onCreated / onUpdated / onRemoved / onMoved / onActivated` 后去抖 1.2s 汇总一次。
2. 扩展每分钟一次心跳推送（`chrome.alarms`），保证索引不会因为长时间无事件而过期。
3. 桌面桥把最后一次推送保存在内存；`GET /api/ext/tabs` 返回 `at / age_s / stale`。
4. `stale=true` 表示超过 150s 未收到推送：扩展可能被关闭、重载或桌面端刚启动。
   这不是“没有标签页”，Agent 必须把两种状态分开处理。

## 扩展 → 桥：POST /api/ext/tabs

只允许扩展来源（`chrome-extension://` / `moz-extension://`）或无 Origin 的本机调用；
普通网页来源 403。请求体上限 512KiB。

```json
{
  "v": 1,
  "source": "chrome-extension",
  "ext_version": "2.6.1",
  "at": 1792700000000,
  "counts": {"tabs": 678, "civitai": 123, "models": 119, "windows": 3, "discarded": 12},
  "tabs": [
    {
      "tabId": 123,
      "windowId": 45,
      "title": "Live Wallpaper Style",
      "url": "https://civitai.red/models/1264662/live-wallpaper-style?modelVersionId=3351608",
      "modelId": "1264662",
      "versionId": "3351608",
      "active": false,
      "pinned": false,
      "discarded": false,
      "copies": 1
    }
  ]
}
```

桥侧校验：

- host 只允许 `civitai.com / civitai.red`（含 `www.`）；
- path 只允许 `/models/<id>` 或 `/api/download/models/<id>`；
- 查询串只保留数字 `modelVersionId` / `fileId`，其余参数丢弃；
- 标题去控制字符、限长 300；最多 2000 行；非法行丢弃并在响应里计数；
- 不写磁盘、不写日志正文；`ext_version` 只接受 32 字符内的版本形状，用于诊断版本不匹配。

响应：`{"ok":true,"received_at":...,"accepted":N,"rejected":M}`。
开发态若桥被 `POST /api/ext/dev/reload` 置过标记，响应额外带 `dev_reload_at`；
扩展只在新时间戳时 `runtime.reload()`，详见 [extension-dev-mode.md](extension-dev-mode.md)。

## 桥 → Agent：GET /api/ext/tabs

参数：

- `q`：大小写不敏感，按空白分词，所有词都需命中（标题 / URL / modelId / versionId）；
- `limit`：1–200，默认 50；
- `include_discarded`：默认 false；
- `window_id`：可选，只看某个浏览器窗口；
- `active`：可选，只看当前激活标签页。

响应：

```json
{
  "ok": true,
  "at": 1792700000000,
  "age_s": 3.2,
  "stale": false,
  "source": "chrome-extension",
  "ext_version": "2.6.1",
  "counts": {"tabs": 678, "civitai": 123, "models": 119, "windows": 3, "discarded": 12},
  "total": 119,
  "matched": 1,
  "items": [
    {"tabId": 123, "windowId": 45, "title": "Live Wallpaper Style",
     "url": "https://civitai.red/models/1264662/live-wallpaper-style?modelVersionId=3351608",
     "modelId": "1264662", "versionId": "3351608",
     "active": false, "pinned": false, "discarded": false, "copies": 1}
  ]
}
```

扩展从未上报时：`ok=true, at=0, stale=true, total=0, items=[]`，并带
`hint` 提示“打开/重载扩展或确认桌面端在运行”，而不是报成“没有标签页”。

## 自动化工具

新增只读工具 `cft_browser_tabs`（core toolset）：

```json
{"name":"cft_browser_tabs","arguments":{"query":"wallpaper","limit":20}}
```

工具返回同样的索引结果，并在有命中时给出 `next_actions`：
`cft_download_model {url, wait:true, verify:true}`。

典型 Agent 流程：

1. `cft_browser_tabs {"query":"wallpaper"}` → 得到目标 URL/versionId；
2. `cft_download_model {"url":"...","wait":true,"verify":true}`；
3. `cft_list_tasks` / `cft_verify_model` 核对落盘与校验。

## 开发态重载

- `POST /api/ext/dev/reload`：只把 `reload_at` 置新；无 Origin 的本机工具或扩展来源可用。
- `GET /api/ext/dev/state`：返回 `reload_at / pushed_at / pending / extension`，不包含任何 URL。
- 扩展在下一次索引上报的响应里看到 `dev_reload_at` 后 `runtime.reload()`；已见时间戳存
  `storage.local`，不会因心跳循环重载。
- 完整用法与限制（内容脚本仍需刷新页面）见 [extension-dev-mode.md](extension-dev-mode.md)。

## 安全边界

- 数据最小化：只读 `tabs` 的 `title / url / id`；只保留 Civitai 模型页；不读 DOM、Cookie、storage。
- 来源最小化：POST 只接受扩展来源；GET 只在本机回环，CORS 白名单沿用现有桥。
- 生命周期：索引只在内存，桌面端退出即清空；扩展重载后自动重推。
- 不改变现有权限模型：扩展已有的 `tabs` 权限是唯一新增数据来源；不需要 `<all_urls>` 或远程主机权限。
- 不把 URL 写进日志；诊断只暴露数量与时间戳。
- 下载仍由桌面端校验 URL 并走现有 API Key / 代理 / 分流逻辑。

## 分阶段（P0 → P3）

- **P0 契约与桥**：`docs/browser-tabs-bridge.md`、`POST/GET /api/ext/tabs`、内存索引、来源/大小/URL 校验、桥测试。
- **P1 扩展上报**：Chrome/Firefox 共用 `lib/tab-index.js`，background 去抖 + 每分钟心跳推送，Node 纯逻辑测试；不改变现有批处理 UI。
- **P2 Agent 面与交付**：`cft_browser_tabs` 契约/执行器、OpenAPI/文档/CHANGELOG、完整回归、打包 EXE 与扩展原地重载说明、HTTP-only 真机验收。
- **P3 开发态**：索引带 `ext_version`；桥 dev 重载标记；扩展按新时间戳自重载；`scripts/dev_extension.py`
  提供 link/watch/reload/restore（详见 [extension-dev-mode.md](extension-dev-mode.md)）。

## English summary

The browser extension already enumerates Civitai model tabs for its batch UI. This
feature adds a read-only, loopback-only tab index: the extension pushes sanitized
model-tab metadata to the desktop bridge, and a new `cft_browser_tabs` automation
tool exposes search results to local agents. Downloads continue through the existing
`cft_download_model` / `POST /api/download` path. No page content, cookies, API keys,
non-Civitai tabs, or remote access are involved; the index lives in memory only.

Roadmap: P0 bridge contract and validation → P1 extension push + heartbeat + tests →
P2 automation tool, docs, full regression, packaged EXE and in-place extension reload →
P3 extension version diagnostics and a bridge-driven dev reload loop.
