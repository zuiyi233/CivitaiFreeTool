# 扩展阶段二：浏览即状态、侧栏、更新、通知与历史（v1，已实现）

**状态：2026-10-22 已实现并通过离线/真 Chromium 回归。** 下面“验收”一节写的是当初设计的
检查项；实际落地的测试文件见 §9（部分能力合并进既有的阶段一测试，而非另起一个文件）。

**English summary.** Phase 2 turns the extension from a popup-only status viewer into a browsing
workbench: inline status badges on Civitai list/image pages, a persistent Chrome side panel, a
one-click “check updates for these local models” action, terminal-task notifications with a
safe “open folder” path allowlist, SHA256 identity fallback for renamed/moved files, and a local
page-visit history that can list “seen but not downloaded” models. The desktop remains the only
component that reads the library, history, update cache and filesystem. The extension never reads
cookies/API keys/webpage text and only talks to `127.0.0.1:47531`.

## 1. 目标与非目标

### 目标

1. **浏览时就能看到状态**：C 站 `/models` 列表页和 `/images/*` 页里的模型链接直接标
   ✓ 已下载 / ↑ 有更新 / … 下载中 / ◐ 其他版本 / ↓ 未下载，复用 `cftModelStatusV1`。
2. **不反复开关小窗**：Chrome 侧边栏常驻同一个工作台；弹层保留，二者共享逻辑与缓存。
3. **更新闭环**：对当前列表里本机已有（模型库命中）的模型批量触发桌面端 `mm_check_updates`，
   完成后自动刷新状态，不静默下载。
4. **完成可感知**：跟踪中的任务到终态时发 Chrome 通知；通知按钮与任务行都能“打开所在目录”，
   桌面端只接受已登记的路径。
5. **身份不只靠版本 ID**：请求可带 C 站侧车/接口里的 SHA256；当版本 ID 缺失或文件被改名时，
   按哈希命中本机文件，减少“其实已下载但显示未知”。
6. **看过什么**：扩展本地记录访问过的模型页，提供“看过但未下载”清单，可一键清除。

### 非目标

- 不做第二个模型库：不扫描磁盘、不写模型文件；哈希只读侧车/历史里已有的值，不主动全盘算哈希。
- 不在扩展里重新判定更新规则；只触发桌面端既有 `mm_check_updates`。
- 不做跨设备同步；页面历史与状态缓存都只在本机扩展存储。
- 不把“打开目录”做成任意路径浏览器；必须命中桌面端已登记的模型/历史/任务落点。
- 不在 Firefox 里伪造侧栏、通知或页面注入能力；Firefox 仍只做弹层状态与手动“打开目录”。

## 2. 列表页 / 图片页状态徽章（Chrome）

### 注入范围

新增 content script `content/list-status.js`（classic，依赖 `lib/util.js`、`lib/model-status.js`）：

| 页面 | 匹配 | 行为 |
|---|---|---|
| `/models` 列表页 | `https://civitai.com/models*` 等两站 + www | 扫描模型链接，在卡片右上角贴状态徽章 |
| `/images/*` | 已有 image-badge 注入范围 | 在图片页的模型资源链接旁贴徽章 |
| `/posts/*` | 已有注入范围 | 同上（帖子里的模型链接） |

脚本在运行时再判 `location.pathname`：只有列表页/图片页/帖子页挂徽章；`/models/<id>` 详情页
仍用现有页面胶囊，不重复注入徽章。

### 链接识别与挂载

- 只用 `a[href]` 里同站 `/models/<数字>` + 可选 `?modelVersionId=`；忽略站外、下载链接、图片页链接。
- 每个链接规范化成 `{modelId, versionId, key}`；同一 key 只挂一枚徽章。
- 挂载点：最近的 `article` / `[class*="mantine-Card"]` / `[data-testid*="card"]`；都没有就挂在
  链接的父元素。容器 `position` 为 `static` 时才补 `relative`；徽章 `position:absolute; top/right`。
- 徽章类名 `cft-list-status`，文案复用 `MS.labelOf` 的 `badge + label`；`unknown` 不显示
  （避免未查询时满屏问号），查询回来有确定状态再出现。
- 动态列表：`MutationObserver`（childList/subtree）合并到 300ms 一次。贴徽章本身是一次 childList
  变更，所以**会再触发一轮收尾扫描**；`render()` 在文本没变化时不写 `textContent`，那一轮之后
  DOM 不再变动，扫描就停下来（改动前实测每 300ms 自激一次）。这不是"我们的插入不进观察面"，
  而是"插入收敛到一次"。
- SPA 路由离开列表/图片页：断开观察者并移除所有徽章；回来重新扫描。

### 数据流

1. 先从 `chrome.storage.local` 读 `cftModelStatusV1`，命中且不陈旧 → 立即画。
2. 对缺失/陈旧项发 `modelStatusQuery`（`refresh:'auto'`, `badge:false`）。`badge:false` 让
   background 不把列表页的某个模型状态写到工具栏徽章（列表页没有单一模型身份）。
3. background 写缓存后，`chrome.storage.onChanged` 触发重绘；不额外轮询。
4. 一次最多 200 个不同模型/版本；超出的链接留到下一轮滚动/变更再查。

### 验收

- 合成 DOM（Playwright）：列表卡片出现 `✓ 已下载` / `↑ 有更新` / `↓ 未下载`；unknown 不出现。
- 真 Chromium：徽章写完后连续两个 1.2s 窗口里，扫描次数最多 +1 且第二个窗口 0 增长（挡住自激循环）。
- 动态插入新卡片后 1s 内出现徽章；离开 `/models` 后徽章清空。
- 不请求 C 站正文，只发本机桥状态查询（`badge:false`）。

## 3. Chrome 侧边栏工作台

- manifest 增加 `sidePanel` 权限与 `side_panel.default_path: "sidepanel.html"`。
- `sidepanel.html` 是 `popup.html` 的同构页面（同一组脚本/CSS/控件），只多一个标记文件
  `sidepanel.js`（`window.__CFT_SIDE_PANEL__ = true`，在 `popup.js` 之前加载）；两侧共享
  `popup.js`，避免逻辑分叉。
  **标记必须是外部脚本**：MV3 扩展页面的默认 CSP 是 `script-src 'self'`，内联 `<script>` 会被静默
  拦掉 —— 写成内联时 `SIDE` 永远是 `undefined`，侧栏看起来"打开了但样式没变、还会自己关"，
  而把扩展目录当 HTTP 站点伺服的测试完全发现不了（真扩展回归已钉住这一点）。
- 弹层页头新增「侧栏」按钮：`chrome.sidePanel.open({windowId})`（用户手势内）。
  浏览器不支持/非 Chrome 时不显示按钮。
- side 模式：
  - 隐藏「在侧边栏打开」；`body` 加 `side-panel` 类，宽度 100%、列表最大高度按视口放大。
  - 归档成功后的 `window.close()` 不执行（侧栏关闭会打断用户）；改为提示。
  - 轮询策略不变：页面可见才跑，隐藏即停。
- 不改变 `action.default_popup`，工具栏图标仍打开弹层。

### 验收

- Playwright（HTTP 伺服）：`popup.html` 有侧栏按钮且 `sidePanel` API 存在时调用一次；`sidepanel.html`
  加载后按钮隐藏、`body.side-panel` 生效、列表与发送逻辑照常。
- 真扩展（`tests/test_extension_real_load.cjs`，需 `CFT_EXT_REAL=1` 的 headed Chrome）：在
  `chrome-extension://` 原点下 `__CFT_SIDE_PANEL__` 为 true、`side-panel` 生效、弹层页
  `chrome.sidePanel.open` 可用且入口可见、0 脚本错误；无头 Chrome 装载不了扩展时明确 SKIP。
- 静态：`popup.html` / `sidepanel.html` 不得有内联脚本；manifest 引用的文件（含 `sidepanel.js`）必须存在。
- manifest 权限审计只增加 `sidePanel`；Firefox manifest 不变。

## 4. 一键检查这些模型的更新

- 入口：弹层/侧栏「下载状态」汇总下方「⫫ 检查这些模型的更新」；本地模型路径来自
  `row.dl.local_versions[].source == 'library'` 的 `path`，去重、最多 50 条。
- 无路径：提示「当前列表没有模型库命中的本地模型」；缺 API Key：沿用 `KEY_HINT` 文案。
- Chrome：`runtime.sendMessage {type:'checkModelUpdates', paths}` → background POST
  `/api/rpc` `{method:'mm_check_updates', args:[false, paths]}`（显式 paths 会强制重查）。
  启动后每 2s 查 `get_mm_update_state`，最多 5 分钟；完成后返回
  `{ok, running:false, newer, msg}`。弹层显示“检查中 x/y”，结束后 `refreshStatuses({force:true})`。
- Firefox：弹层直接 `POST /api/rpc`，同样的轮询逻辑（无 background 代理）。
- 桌面端返回 `requires_api_key` 时显示缺 Key 指引；`started:false` 原样显示 `msg`（白名单/正在跑）。
- 不自动下载任何模型；只更新 `model_updates.json` 并刷新徽章。

### 验收

- 假桥断言 RPC 方法名/参数/轮询；有更新后状态从 `downloaded` 变 `update_available`。
- `running` 超时、HTTP 失败、缺 Key 三条路径都有可读文案，不误报“已是最新”。

## 5. 完成通知与打开所在目录

### 桌面桥 `POST /api/reveal`

- 入参 `{path}`，只接受字符串；桥层 64KB body 上限。未注册 handler 返回 503；
  形状错误 400；白名单拒绝 403（`{ok:false,msg:'路径未登记，拒绝打开'}`）。
- `Api.reveal_registered_path(path)` 先归一化 `normcase(abspath)`，只允许：
  - `model_rows[*].path` 的文件（打开其所在文件夹并选中）；
  - `Downloader.history` 的 `file_path` / `dest_dir/filename`；
  - `Downloader.tasks` 的 `dest_dir/filename` 或 `dest_dir`。
- 未命中返回 `{ok:false,msg:'路径未登记，拒绝打开'}`；不提供任意路径浏览/执行。
- 命中后调用既有 `open_in_folder`（Windows Shell API，跨平台走 posix_compat）。

### 扩展

- manifest 增加 `notifications`、`alarms` 权限（仅 Chrome；Firefox 不加）。
- `pollTasks` 检测到新的终态（done/failed/library）时发 `notifyTaskDone`
  `{key, phase, filename, dest_dir, title}`；background 去重（`cftNotifiedTerminalsV1`，上限 200）
  后创建通知：
  - 标题：`下载完成` / `下载失败` / `已在模型库中`；
  - 正文：文件名 + 落点；
  - 按钮：`打开所在目录`（有 dest_dir 时）。
- 通知按钮 → background 读回该通知登记的 `dest_dir` → POST `/api/reveal`；点击通知正文 →
  打开 `http://127.0.0.1:47531/`（CFT 工作台）。
- **任务归属按版本认**：桌面端 `get_tasks` 本次补出 `model_id` / `version_id`（取自任务
  `info`）。弹层与后台守望都先用 `version_id`、没有再退任务 URL 里的 `modelVersionId`，
  两者都没有才退回"按模型 ID"的老口径。这样"同一个模型重下新版本"不会把上一次那条
  `done` 任务当成本次完成，也不会提前清掉守望 alarm。
- **通知去重键是 `key:phase[:task_id]`**，并且只在 `notifications.create` 成功后才写记账：
  同一版本重新下载（新 task_id）会再通知一次；浏览器拒绝创建时不记账，下次仍可重试。
- 任务行增加「打开目录」按钮（有 dest_dir 时），Chrome 走 background `revealPath`，
  Firefox 直接 POST `/api/reveal`。
- 后台守望：普通提交批次（`lastBatch` / `cftExtTasks`）后
  `chrome.alarms.create('cftTaskWatch', {periodInMinutes:1})`；`onAlarm` 只查仍非终态的任务，
  全部终态或超过 2 小时即清 alarm。通知只在“第一次看到终态”时发，重启后按已通知集合去重。
- 边界：**批量归档暂不逐模型发系统通知**。它有自己的进度面板与全局徽章，且离屏任务的
  结果不回写到 `cftExtTasks`；硬接会让“已归档跳过”的页面误发完成通知。
- 离线/旧桌面端（404）时按钮给出“需升级 CFT”的提示，不假装打开成功。

### 验收

- 桌面：`reveal_registered_path` 命中模型/历史/任务三类路径；未登记路径拒绝；归一化忽略大小写与
  尾分隔符；已登记但文件已消失时拒绝并不打开；桥 400/403/503 分支。
- 扩展：终态按 `key:phase[:task_id]` 只通知一次；同版本重新下载（新 task_id）会再通知；
  `create` 失败不写记账、可重试；按钮消息带登记路径；404 文案说"需升级 CFT"；
  alarm 在全部终态后清除；同模型的旧 done 任务不会被认成本次完成（弹层与后台守望各一条回归）。
- Firefox：无通知权限，任务行「打开目录」直连桥可用。

## 6. 哈希级身份回退

- C 站数据里有 `files[].hashes.SHA256`。扩展补全时把版本内各文件的 SHA256 保留为
  `meta.files[].sha256` 与 `meta.sha256s`；状态请求带 `hashes:[...]`（每项最多 8 个，去重小写）。
- 桌面端本地索引（只读已有数据，不扫盘算哈希）：
  - 模型库行的侧车 `info.files[].hashes.SHA256`（扫描时已在 `row.info` 里）；
  - 历史 `actual_sha256` / `expected_sha256`。
  - 多文件版本只认**文件名与本行文件一致**的那条哈希：侧车里的 `files[]` 是整个版本的清单，
    拿另一个文件的 SHA256 命中会把 `local.path` 指到内容并不匹配的文件；单文件版本没有歧义。
    改名后的多文件版本宁可退回 unknown，也不猜。
  - 活动任务**不参与**哈希命中：排队/下载中的任务不能证明文件已经存在，拿它的
    `expected_sha256` 判“已下载”会把“准备下载”说成“已下载”。
- 优先级：精确版本 + 文件存在 → 原逻辑；否则若请求带哈希且本地索引命中且文件存在 →
  `state=downloaded`（若有该版本更新记录仍可 `update_available`），`matched='hash'`,
  `version_matched=false`，`reason='按 SHA256 命中本地文件（版本 ID 缺失或文件已改名）'`，
  `local.source` 标 `library`/`history`，并返回命中路径。
- 哈希不存在/文件已不在 → 维持原来的 `unknown`/`not_downloaded`，不猜。
- 扩展缓存 key 仍用模型/版本 ID；哈希只影响后端匹配，不新增缓存键。

### 验收

- Python：侧车哈希命中、历史哈希命中、文件缺失不算命中、无哈希不改变原状态、已有精确命中不被哈希覆盖。
- 扩展：enrich 投影保留哈希；请求体带 `hashes`；Firefox 复制逐字节相同。

## 7. 页面级历史与“看过未下载”

- storage key `cftPageHistoryV1`：
  ```json
  {"version":1,"savedAt":0,"items":{"version:1001":{"key":"version:1001","modelId":"101",
   "versionId":"1001","url":"https://civitai.red/models/101?modelVersionId=1001",
   "title":"Alpha | Civitai","firstAt":0,"lastAt":0,"count":1}}}
  ```
- 记录：Chrome/Firefox background 在 `tabs.onUpdated`（URL 变化/加载完成）与 `onStartup` 扫描
  当前标签；只用 `CftTabs.modelLink` 校验，存清洗后的 URL（无 token）、标题、时间；最多 500 条，
  超出按 `lastAt` 淘汰。仅本地，不发送给桌面。
- 弹层/侧栏新增可折叠「看过但未下载」区：
  - 数据来自页面历史；状态取 `cftModelStatusV1` 的缓存（没有则“状态未知”）。
  - 只列不是 `downloaded`/`update_available`/`downloading` 的条目；每条有“打开”链接。
  - 显示条数；「清除页面历史」需确认，只删扩展记录，不删下载历史/模型文件。
- 隐私文案：只记录 C 站模型页 URL/标题/访问时间，保存在本机扩展存储，可随时清除。

### 验收

- node/Playwright：访问模型页写历史；重复访问去重并累加次数；清理后列表为空；
  已下载的条目不出现在“看过未下载”清单。
- Firefox 平价：同 key、同去重语义。

## 8. 隐私与安全

- 扩展新增权限仅 Chrome：`sidePanel`、`notifications`、`alarms`；不新增 host 权限，不读网页正文。
- 列表/图片页徽章只读页面里的模型链接，不抓取页面文本；请求体仍只有模型/版本 ID、清洗 URL、
  可选 SHA256。
- `/api/reveal` 是唯一新增的“动作”端点：只接受桌面端已登记路径，拒绝任意路径；桥仍只绑
  `127.0.0.1`，CORS 白名单不变。
- 页面历史与状态缓存都可用扩展存储清除；通知正文不含 API Key/下载令牌。

## 9. 测试与发布

- 新增/扩展：
  - `tests/test_model_status_background.cjs`：页面历史（模型页/30s 去重/非模型页不写）、
    列表页 `badge:false`、检查更新参数、`/api/reveal` 转发与拒绝、通知去重/按钮、
    后台守望发通知并清 alarm。
  - `tests/test_model_status_ui.cjs`（真 Chromium）：列表页卡片徽章（unknown/站外/图片不贴、
    陈旧项单次查询）、侧栏页 `side-panel`、页面历史清单与一键清除、检查更新路径。
  - `chrome-extension/content/list-status.test.cjs`：`pageKind` / `parseModelLink` 纯函数。
  - `chrome-extension/lib/model-status.test.cjs` 与 `lib/enrich.test.cjs`：SHA256 投影、请求项
    `hashes` 去重限 8。
  - `tests/test_firefox_parity.cjs`：页面历史、检查更新、任务行打开目录、后台历史静态断言。
  - Python `tests/test_reveal_path.py`（路径白名单）、`tests/test_model_status.py` 哈希回退用例、
    `tests/test_browser_bridge.py` 的 `/api/reveal` 分支。
- 回归：全量 pytest、`test_extension_batch.cjs`、`test_popup_integration.cjs`、
  `test_model_status_ui.cjs`、`test_firefox_parity.cjs`。
- 版本：Chrome 2.5.0、Firefox 1.6.0；桌面端仍为当前未发布版本 2.7.0。扩展 ZIP 资产名不带版本号，
  原地“重新加载”升级（manifest `key` 保留）。

## 10. 后续可继续深化（不在本阶段）

- 列表页按状态过滤/隐藏已下载卡片（需尊重站点分页与虚拟列表）。
- 侧栏内嵌任务详情（队列/历史/更新页签），代替跳 CFT。
- 通知聚合（一次批次一条通知，而不是每个任务一条）。
- 用 Content Script 在模型页显示“同模型其他版本”对比（已有 C 站接口数据）。
- 可选的本地哈希全量索引（后台低优先级、可暂停），让哈希回退覆盖没有侧车的文件。
