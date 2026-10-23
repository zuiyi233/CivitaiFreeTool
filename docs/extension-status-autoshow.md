# 扩展状态免点击自动显示（设计）

> 状态：已落地（Chrome 2.7.0 / Firefox 1.8.0）。唯一事实源仍是桌面端 `POST /api/model/status`，
> 扩展只负责"把它显示出来"，不自己判定状态。

## 1. 现象与根因

用户反馈：C 站页面上看不到"这个模型本机有没有"，图标也没徽章，**必须点扩展图标打开弹层**才看得到。

三条根因（逐条对着代码确认，不是猜）：

1. **没人主动查询**。`reapplyStatusBadges()` 只从 `cftModelStatusV1` 缓存补徽章，注释就写着
   "徽章只从缓存补/清，不发网络"（`background.js`）；而这份缓存只有在弹层里跑一次批量查询才会被填。
   桌面端已经明确回答 `state=downloaded`，扩展却因为"没去问"而显示不出来。
2. **重载后旧标签页里没有内容脚本**。MV3 的扩展重载/更新会杀掉 service worker，同时已注入的
   content script 也随之消失；不刷新页面就没人画 `#cft-status-chip`。所以"点图标能看到、页面上没有"。
3. **心跳只推标签页索引**。`cft-tab-index` 每分钟上报一次索引，但没有任何周期性状态刷新，
   下载完成后徽章不会自己变绿。

## 2. 方案

### 2.1 后台主动批量查询

新增 `refreshOpenTabsStatus(reason)`：

- 用 `chrome.tabs.query({})` 收集 URL 命中 `CftTabs.modelLink()` 的标签，按 `key/modelId/versionId/tabId`
  组装 items；**上限 60 个标签**（桥侧单次上限 500，这里先按实际打开数收口，避免 686 个标签时打爆请求体）。
- 复用既有的 `queryStatusOnce(items)` → `applyBadgesForItems` + `pushStatusToTabs`：
  前者点亮逐标签徽章，后者把状态推给已在页面里的脚本，页面不用再自己多打一轮本机桥。
- 触发点三处：`runtime.onStartup`、`runtime.onInstalled`（安装/更新/重载都算）、
  新增 alarm `cft-status-heartbeat`（`periodInMinutes: 2`，与扩展缓存"下载中 2 分钟"的 TTL 对齐）。
- 并发保护：同一时刻只允许一轮（`_statusRefreshInFlight`），加上与 Chrome 同一份 1.5 秒去重窗口
  （启动 + 更新 + 心跳常挤在同几秒里），桌面端离线时按既有口径降级为"上次缓存 / 需升级"，
  **不把没查到说成未下载**。
- **发出去的 URL 是清洗过的**：items 取 `CftTabs.modelLink(tab.url).url`，不是标签页原始 URL——
  原始地址里可能带着 `token=` 之类的追踪参数，它们既不该进请求体，也不该进桌面端日志。
- **降级不许盖掉已知状态**：桥没连着（或桌面端太旧）时，`entryForItem` 用缓存里那一版结论并打上
  `offline` 标记，徽章仍是 `✓` 而不是 `?`；只有"从来没查过"才是空徽章。

### 2.2 缺失脚本的重注入（ping 一下再决定）

新增 `reinjectContentScripts()`：

- 注入清单**直接读 `chrome.runtime.getManifest().content_scripts`**，不手写第二份文件列表，
  避免清单与 manifest 漂移。
- 判定方式：先 `chrome.tabs.sendMessage(tabId, {__cft:true,to:'content',type:'ping'})`；
  有回音说明脚本还在，**不重复注入**；抛错说明接收端不存在，才
  `chrome.scripting.executeScript({target:{tabId}, files:[...]})` + `insertCSS`。
- 跳过 `discarded`（休眠）标签：它们本来就没在跑，唤醒时走正常注入链路。
- 内容脚本侧再加幂等守卫 `window.__cftModelPageInjected`，即使被注入两次也只有一个胶囊。
- **绝不自动刷新用户页面**，也不往非 C 站页面注入。

### 2.3 徽章口径（回答"图标亮了是不是就下载了"）

| 徽章 | 含义 |
| --- | --- |
| `✓` 绿 | 已下载（本机命中该版本文件） |
| `↑` 橙 | 有更新（同模型有更新版本可下） |
| `…` 蓝 | 下载中 |
| `◐` 灰 | 其他版本（本机有同模型别的版本） |
| `↓` 灰 | 未下载 |
| `?` 灰 | 状态未知（库没扫完 / 桌面端没答上来） |
| **无徽章** | **没判定过**：桌面端没连上、缓存为空，或这个标签还没进过状态链路 |

悬停标题给完整文案（`lab.label · lab.detail`），列表页 / 图片页查询不会污染工具栏徽章。

### 2.4 唤醒不能只挂在 onInstalled/onStartup（真机踩出来的）

`chrome.runtime.reload()`（开发态重载就是它）之后，Chrome **不保证再把 `onInstalled` 交给新的 SW**：
新后台只等一个事件把它叫醒，醒了只跑模块级代码。所以唤醒挂在 `onInstalled`/`onStartup` 上会一条都不跑，
"重载后补回脚本"这条链永远不触发。现在模块级再加 `bootWake()`：

- 每次 SW 醒来先 `ensureStatusAlarm()`（闹钟被重载清掉时也能补回来）。
- 再判断这是不是冷启动：`alarms.getAll()` 里**没有** `cft-status-heartbeat`（刚被重载/更新过），
  或 storage 里的 `cftBootWakeAtV1` 超过 90 秒 —— 满足任一条才 `wakeOpenTabs('boot')`。
- 两个条件都要：只看时间戳会漏掉"重载发生在 90 秒内"；只看闹钟会在闹钟被保留的宿主上漏掉重启。
- 反面也要成立：**空闲重启不许再打一轮**（闹钟还在 + 刚补过 → 安静跳过），
  否则 SW 每 30 秒被唤醒一次就会把本机桥按分钟级重打。

### 2.5 重复注入的真机陷阱：注入文件顶层不能有 const/let

`chrome.scripting.executeScript({files})` 把文件注进**同一个隔离世界**，而扩展重载后这是第二次注入。
`lib/util.js` 顶层的 `const _utf8Len = …` 第二次声明直接抛 `SyntaxError: Identifier '_utf8Len' has
already been declared`，整批脚本静默不执行——胶囊再也不回来，而 vm 里的假 `executeScript` 永远测不出来。
现在把这些文件的顶层声明改成 `var`（`function` 本来就可重复声明），并加了两道闸：

- 静态不变量：manifest `content_scripts` 里列出的每个文件，顶层不得出现 `const`/`let`/`class`
  （`tests/test_extension_batch.cjs`）。
- 真加载回归：真 Chrome 里把 manifest 声明的文件再注一遍，页面上必须仍只有一个 `#cft-status-chip`
  且 **0 条页面脚本错误**（`tests/test_extension_real_load.cjs`，`CFT_EXT_REAL=1` 才跑）。

> 为什么 `runtime.reload()` 本身不在真加载回归里跑：实测在这套 Playwright 临时 profile 中，
> reload 之后扩展 SW 再也不回来（40 秒内 SW 列表与 `serviceworker` 事件都是空的，新开标签页也叫不醒），
> 复现不了"真浏览器重载那一刻"。重载那一轮由 §2.4 的 vm 冷启动用例钉，注入与推送两条腿由真 Chrome 钉。

## 3. 权限与安全边界

- **只有 Chrome 清单新增 `scripting`**（用户确认后的选择：免刷新补回页面胶囊值得换一个权限）。
  用途仅限把本扩展自己声明过的 content script 重新注入到已经匹配的 C 站页面；
  不注入任意代码（只允许 `files`，禁 `func`/`code`）、不注入到站点之外。测试把这三条一起钉住。
- **Firefox 不申请 `scripting`**：它本来就没有 `content_scripts`，也没有页面通道，
  申请了就是拿一个用不上的权限换不到任何功能。见 §4。
- 扩展仍然只发模型/版本 ID 与清洗过的 URL，**不读 Cookie、API Key、网页正文**；
  只连硬编码 `http://127.0.0.1:47531`。
- 心跳是 2 分钟一次的本机请求，桌面端不在跑时直接失败返回，不重试轰炸、不落盘任何 URL 日志。

## 4. Firefox 口径（徽章这一层镜像过去，注入那一层不镜像）

Firefox 版没有 `content_scripts`，也拿不到页面里的接收者，所以只移植**不需要注入的那一半**：

- 同一套 `collectOpenModelTabs`（≤60 个未休眠的 C 站模型页）→ 批量 `POST /api/model/status`
  → 逐标签 `browser.action.setBadgeText({tabId})` + `setBadgeBackgroundColor({tabId})`；
  悬浮说明用 Firefox 才有的 `setBadgeTitle({tabId})`，不改全局按钮文案。
- 触发点与 Chrome 相同：`onStartup`、`onInstalled`、`cft-status-heartbeat`（2 分钟）。
  离线降级与 1.5 秒去重窗口也同口径。
- 新开的模型页先按缓存点徽章（只读 `storage`，不打网络），不必等下一次心跳；
  缓存里没有就保持空徽章——"没查过"不等于"未下载"。
- **页面胶囊在 Firefox 做不到，就如实写着做不到**（README 差异表里那一行是"没有"），
  不为了"看起来平价"去申请 `scripting` 或伪造注入。

## 5. 验收判据

1. 桌面端在线、扩展刚被 dev 重载：不点图标、不刷新页面，已打开的 C 站模型页出现 `#cft-status-chip`，
   且状态与 `POST /api/model/status` 的判定逐字一致（本轮真机基线：`[MMH3] Anime Shift` → `downloaded` → `✓ 已下载`）。
2. 逐标签徽章同上表；`?` 与"无徽章"能被区分开（前者是"问过但没结论"，后者是"还没问过"）。
3. 下载完成后 ≤2 分钟内徽章自己从 `…` 变 `✓`，无需任何人工动作。
4. 已有测试无退化：`tests/test_tab_index.cjs`、`tests/test_extension_batch.cjs`、
   `tests/test_firefox_parity.cjs`，以及新增的批量查询/重注入/幂等守卫用例。
5. 重复注入不产生第二个胶囊；非 C 站页面零注入；休眠标签不被唤醒。
6. Firefox 侧：后台一轮批量查询就点亮逐标签徽章，跳过休眠与站外标签；桥没开着时徽章仍是上次
   已知状态（`✓`）而不是 `?`；新开标签页按缓存秒出徽章且**零网络请求**；80 个标签也只发 60 条。
   以上四条各自有断言，写在 `tests/test_firefox_parity.cjs` §3.7。
7. 两边的状态请求体都只含 `model_id`/`version_id`/清洗过的 `url` 与 `include_updates`，
   带 `token=` 的原始地址不会出现在请求体里（Chrome 与 Firefox 各钉一条）。
8. 冷启动那一轮（SW 醒来没有状态心跳）必须自己补脚本 + 查状态 + 点亮徽章，不依赖任何生命周期事件；
   反过来"闹钟还在 + 90 秒内刚补过"必须安静跳过（`tests/test_extension_batch.cjs` 两组）。

### 实测结果（真 Chrome + 真桌面端，`CFT_EXT_REAL=1 node tests/test_extension_real_load.cjs`）

- 只用 `--load-extension` 装载扩展、**没点过图标、没开过弹层**，打开
  `https://civitai.red/models/2998224/mmh3-anime-shift?modelVersionId=3400711` 后页面自己出现
  `✓ 已下载 · v1.0`，与桌面端 `POST /api/model/status` 的 `state=downloaded` /
  `local.version_name=v1.0` / `path=D:\ComfyUI_Mie_2026_V8.0\ComfyUI\models\loras\MMH3-AnimeShift-V1\…` 一致。
- 后台向这一页 `sendMessage(modelStatusPush)`，页面不刷新就把陈旧的胶囊文本改成 `✓ 已下载 · v1.0`
  （主世界标记仍在 → 证明文档没被重新加载）。
- 把 manifest 声明的文件再注一遍：页面仍只有 1 个胶囊、0 条页面脚本错误。
  修这条之前它是 `SyntaxError: Identifier '_utf8Len' has already been declared`（见 §2.5）。
- `chrome.alarms.getAll()` 里确实有 `cft-status-heartbeat`（2 分钟），storage 里有 `cftBootWakeAtV1`。
- 用户本机实例经开发态重载后上报 `ext_version=2.7.0`、`source=chrome-extension`，索引里 685 个标签 /
  116 个模型页，`pushed_at > reload_at`（重载真的生效了）。

## English summary

Problem: status was visible only inside the popup. Three causes: badges are filled from cache only and
nothing ever queried the bridge unless the popup opened; after an extension reload, MV3 kills the
content scripts of already-open tabs, so page chips cannot exist; the 1-minute heartbeat only pushed
the tab index.

Design: add a background batch status refresh over open Civitai model tabs (capped at 60 tabs), triggered
on startup, on install/update, and by a new 2-minute `cft-status-heartbeat` alarm, reusing the existing
badge/push helpers. Re-attach content scripts by probing each tab with a message first — only tabs whose
receiver is gone get `chrome.scripting.executeScript` with the file list read from the manifest itself,
skipping discarded tabs, plus an idempotency guard inside the script so a chip is never duplicated.
Never auto-refresh user pages, never inject outside matched Civitai URLs.

Trade-off: Chrome gains exactly one permission, `scripting`, used solely to re-inject this extension's own
declared scripts into pages it already matches (files only — never `func`/`code`, never outside the matched
Civitai URLs). Firefox does **not** ask for it: it has no content scripts, so it mirrors only the half that
needs no injection — the same background batch query, now lighting up per-tab toolbar badges on startup,
on install/update and on the 2-minute heartbeat, with a cache-only badge for freshly opened tabs. Page chips
simply do not exist in Firefox, and the README says so instead of faking parity. All other boundaries stay:
IDs and sanitized URLs only (raw tab URLs may carry a `token=`, so they are never sent), no cookies, no page
bodies, loopback `127.0.0.1:47531` only. When the bridge is unreachable the badge keeps the last known state
and is marked offline rather than degrading to `?`. Badge vocabulary is documented so "no badge" means
"never evaluated", distinct from `?` ("asked, no answer yet").

Two things only real Chrome taught us. First, `chrome.runtime.reload()` does not reliably hand
`onInstalled` to the new service worker — it simply waits for an event — so the wake-up now also lives at
module scope: every worker start re-registers the heartbeat and runs one re-attach + status pass if the
heartbeat alarm is gone (we were just reloaded/updated) or the stored `cftBootWakeAtV1` timestamp is older
than 90 seconds; a warm idle restart stays silent so the loopback bridge never gets poked for nothing.
Second, `executeScript({files})` evaluates into the *same* isolated world, so re-injecting a file that
declares a top-level `const` throws `Identifier '_utf8Len' has already been declared` and the whole batch
dies silently — the injected files now declare top-level values with `var`, guarded both by a static
manifest-content-scripts invariant and by a real-load check that re-injects the manifest's own file list
and demands exactly one chip plus zero page errors.
