# 扩展开发态：免打包、可自动重载

## 目标

改扩展源码后不再「压缩 → 重新选择目录 → 导入」，只保留浏览器安全模型要求的一次性加载。

```
chrome-extension/（源码）
      ↑ junction
.tools/release2632/CivitaiFreeTool-ChromeExtension（浏览器已加载的路径）
      ↑
scripts/dev_extension.py watch → 桥 /api/ext/dev/reload → 扩展下次心跳自重载
```

## 一次性准备

```bash
# 仓库根；会把已加载副本移到 .release-backup-*，再建指向源码的 junction
python scripts/dev_extension.py link

# 浏览器扩展页点一次「重新加载」；扩展 ID 由 manifest.key 固定，不会变
```

之后：

```bash
python scripts/dev_extension.py watch        # 监听源码，改动即请求重载
python scripts/dev_extension.py reload --wait  # 手动请求，并等扩展重新上报
python scripts/dev_extension.py status       # 链接、版本、桥状态
python scripts/dev_extension.py restore      # 删除链接并恢复最近的备份
```

Firefox：

```bash
python scripts/dev_extension.py --source firefox-extension --loaded <临时载入目录> link
```

## 自动重载怎么发生

1. `watch` 发现源码文件 mtime/大小变化（忽略 `*.test.cjs`），POST `http://127.0.0.1:47531/api/ext/dev/reload`。
2. 桥只把 `reload_at` 时间戳置新，不推代码、不读页面数据。
3. 扩展下一次 `POST /api/ext/tabs`（标签页事件或每分钟心跳）的响应里带 `dev_reload_at`。
4. 扩展把它与 `chrome.storage.local` 里的 `cftDevReloadSeenV1` 比较；只有更新的时间戳才
   `runtime.reload()`，先写 storage 再重载，所以不会循环。
5. 重载后下一次索引上报更新 `pushed_at`；`reload --wait` 用它确认新代码已经跑起来。

`GET /api/ext/dev/state` 返回 `{reload_at, pushed_at, pending, extension}`；
`extension.ext_version` 来自索引载荷，可用于确认浏览器里跑的是哪一版。

## 必须知道的限制

- **内容脚本改动也不用刷新页面（Chrome 2.7.0 起）**：重载后后台先 `ping` 探测每个已匹配页面里还有没有接收者，
  确认没了才用 `scripting` 把 manifest 自己声明的那几个文件注回去（只 `files`，不执行任意代码，不注站外）。
  浏览器本身确实不会自动重注入，这一步是扩展自己补的；Firefox 侧没有内容脚本，不适用。
- 重载最迟等一次心跳（≤60s）；想立刻生效就切一下标签页，或在扩展页点一次「重新加载」。
- Firefox 的临时附加组件在浏览器重启后仍要手动临时载入一次（签名策略）。
- 桌面端必须是含 `/api/ext/dev/*` 的 2.7.0+；旧版会 404，`watch` 会如实报失败。
- 宿主扩展页 CSP 仍禁止内联脚本；本开发回路不改变该边界。

## 安全与可恢复

- `link` 只操作仓库内 `.tools/` 下的加载副本，先改名为 `.release-backup-<时间>`，再建链接。
- `restore` 只移除链接（junction 用 `rmdir`，不会删目标内容），再把最近备份改回来。
- dev 接口只影响「扩展何时重载」，不执行任意代码、不传页面数据；网页来源被桥的 Origin 白名单拒绝。
- 发行流程不变：用户仍下载 ZIP、解压到固定目录；`key` 不变，升级只点重新加载。

## 排障

| 现象 | 原因 / 处理 |
| --- | --- |
| `link` 报「无法移开已加载目录」 | 浏览器占用；关闭扩展页/重启浏览器后重试，或手动关掉占用进程 |
| `reload` 返回 404 | 桌面端是旧版；升级到含 dev 接口的 2.7.0+ |
| 请求成功但扩展没重载 | 等一次心跳（≤60s）或切标签页；确认浏览器里加载的是 `.tools/.../CivitaiFreeTool-ChromeExtension` 这个路径 |
| 扩展 ID 变了 | 检查 manifest `key` 是否被删；`link` 不改 manifest |
| 页面脚本还是旧逻辑 | 注回只发生在扩展重载/更新时；页面里脚本还有回音就不重复注入（防两个胶囊），此时仍是旧逻辑 → 刷新该页面 |
| storage 里 `cftDevReloadSeenV1` 太大 | 正常；它只保存最后一次已处理的时间戳 |

## English summary

`dev_extension.py link` replaces the loaded unpacked copy with a junction to the
repository source (backup kept; `manifest.key` unchanged so the extension ID is
stable). `watch` posts to the bridge's dev-only `/api/ext/dev/reload`; the
extension reloads itself when its next tab-index push returns a newer
`dev_reload_at`, with the timestamp persisted in `storage.local` to prevent loops.
Content-script changes still require a page refresh, and Firefox temporary add-ons
still need one manual load per browser session. No packaging or re-import is
required for background/popup development.
