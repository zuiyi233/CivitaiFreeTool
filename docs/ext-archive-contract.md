# Chrome 扩展「归档助手」跨模块契约（v1）

> 所有参与开发的代理必须遵守本契约。 Phase 1-4（脚本功能吸收进扩展）统一按此实现。
> 参考源：`D:\油猴脚本\Civitai一键下载模型并归档.user.js`（下称"脚本"）。

## 0. 目录结构

```
chrome-extension/
  manifest.json            # D 负责：权限 MV3
  background.js            # D 负责：SW 消息路由 + offscreen 管理 + 写盘通道 + bridge 客户端
  popup.html/.js/.css      # D 负责：保留现有"发软件下载" + 新增一键归档入口
  offscreen/
    archive-worker.html    # D 负责（壳）
    archive-worker.js      # B 负责：归档编排主流程
  content/
    model-page.js          # C 负责：模型页交互（按钮/弹窗/DOM 提取）
    image-badge.js         # C 负责：图片流/帖子页 SAVE 徽章（Phase 3）
    content.css            # C 负责：徽章/按钮样式
  lib/                     # A 负责：纯逻辑，从脚本搬运适配
    util.js  civitai-api.js  scheduler.js  idb.js  history.js  config.js  log.js
```

## 1. 模块加载方式（无构建，硬性约束）

- `manifest.json`：`"background": {"service_worker": "background.js", "type": "module"}`。
- offscreen：`archive-worker.html` 内 `<script type="module" src="archive-worker.js">`。
- **lib/*.js 一律禁止 `export` / `import` 语句**（content script 无法用 module）。所有 lib
  文件把公开 API 挂到 `globalThis.CFT.<ns>`（如 `CFT.util.safeFsName`）。
  module 环境（SW / offscreen）通过 `import './lib/util.js'` 的副作用获得全局 `CFT`。
- content scripts 在 manifest 中按序注入 `lib/util.js` → `content/xxx.js`，直接使用 `CFT`。

## 2. 消息协议（chrome.runtime.sendMessage）

信封：`{__cft: true, to: 'bg'|'offscreen'|'content', type: string, payload: object}`。
所有 onMessage 监听器必须先检查 `to` 字段，非本模块的消息直接 `return`（不占用 sendResponse）。
异步响应必须 `return true` 保持通道。路由规则：
- `to:'offscreen'` 的消息由 offscreen archive-worker 处理（bg 不转发，runtime 消息本身广播）；
- `to:'bg'` 由 background 处理；`to:'content'` 由 content 处理。

### 消息清单

| from → to | type | payload → 响应 | 阶段 |
|---|---|---|---|
| popup/content → bg | `startArchive` | `{tabId}` → 无（bg 指挥 content 采集） | P1 |
| bg → content | collectPage | bg 经 tabs.sendMessage 主动采集，envelope 应答 `{modelId, modelVersionId, aboutText, pageUrl}` | P1 |
| bg → offscreen | `archive` | `{modelId, modelVersionId, aboutText, pageUrl, tabId}` → 流式进度 | P1 |
| offscreen → bg | `writeFile` | `{relPath, dataB64, overwrite}` → `{ok, saved, channel, msg}` | P1 |
| offscreen → bg | `getArchiveConfig` | → `{softwareOnline, archiveRoot, channel}` | P1 |
| offscreen → bg | `sendModelToSoftware` | `{downloadUrl, pageUrl, destSubdir}` → `{ok, msg}`（POST 软件桥 /api/download，body `{url, dest_subdir}`；dest_subdir=归档子目录，模型文件落 <archive_root>/<destSubdir>/） | P1 |
| offscreen → content | `progress` | `{percent, text, level}` | P1 |
| offscreen → content | `archiveDone` | `{ok, dirName, msg, errors:[]}` | P1 |
| offscreen → content | `askFileSelection` | `{files:[{name,size,type}]}` → `{fileName}` | P2 |
| offscreen → content | `askArchiveConfirm` | `{record}` → `{proceed}` | P3 |
| offscreen → content | `requestDomComments` | → `{results:[{user,content,createdAt,reply,fromDom}]}`（结构化数组，不做文本拼接） | P2 |
| offscreen → bg | `checkLibrary` | `{versionId}` → `{found, paths}` | P4 |
| offscreen → bg | `notifyArchiveDone` | `{dirName}` → `{ok}`（通知软件重扫） | P4 |
| content → offscreen | `userSelection` | `{fileName}`（对 askFileSelection 的答复） | P2 |
| content → offscreen | `archiveConfirm` | `{proceed}` | P3 |
| content → offscreen | `domComments` | `{cid, results:[...]}`（同上结构；offscreen 并入 trpc 结果走统一 formatter） | P2 |
| content → bg | `saveSingleImage` | `{imageId, originalUrl, ext}` → `{ok}` | P3 |
| offscreen → content | `savedImageDone` | `{ok, imageId}`（bg 经 tabs.sendMessage 广播到所有 images/posts 页，刷新徽章 SAVED） |
| offscreen → content | `pageFetch` | `{url, timeoutMs}`（cid 由 askContent 注入）→ content 在页面里**同源代发** GET，见 §2.1 |
| content → offscreen | `pageFetchResult` | `{cid, ok, status, body, err, truncated}`（sendResponse + runtime 广播双路，resolveCid 先到先得） |
| popup → bg | `invalidateArchiveConfig` | → `{ok}`（软件离线或归档目录变化后，丢弃 getArchiveConfig 的缓存通道判定） | P3 |

注意：content 页面可能被刷新/关闭，offscreen 对 content 的每个"请求-答复"消息都要有
超时兜底（15s），超时按用户取消处理。

### 2.1 页面数据通道（pageFetch）与传输层

为什么必须有：油猴脚本时代所有 C 站数据请求都发生在页面里（同源，浏览器自动带登录 Cookie）。
移植到 offscreen 后源变成 `chrome-extension://`、请求变匿名，站点就不给受限数据了：tRPC 一律
401 `Please use the public API instead`，公开 API 则是 200 + 空结果（实测匿名
`GET /api/v1/images?imageId=145676405&withMeta=true` → `{"items":[],"metadata":{}}`，同结构的
SFW 图 145676249 有完整 meta）。**丢的是登录上下文，不是接口**，所以主通道改回页面。

【2026-10-16 更正，两条结论都要带上】
1. 用户的 **API Key 是可以过 tRPC 认证的**：`Authorization: Bearer <key>` 打
   `/api/trpc/commentv2.getInfinite` / `resourceReview.getPaged`，匿名 401 → 带 Key 200，
   且返回真实数据（模型 2989579 版本 3389879 实测 `items=50 totalItems=67 totalPages=2`）。
   所以**桌面端不必依赖浏览器**：有 Key 就能自己取评论/评价（见 §2.3）。页面通道是给
   「没有 Key」或「Key 没有相应 scope」时的兜底，不是唯一通路。
2. 公开 API 的空结果不只是匿名造成的：`/api/v1/images` 的
   `browsingLevel ?? nsfw ?? publicBrowsingLevelsFlag`（官方 `src/pages/api/v1/images/index.ts:218`）
   **对带 Key 的请求同样默认只放行 PG**。实测 `imageId=145676405`（Soft）带 Key 不传
   browsingLevel → `items:[]`；加 `browsingLevel=31` → `items=1`。位掩码
   `NsfwLevel{PG=1,PG13=2,R=4,X=8,XXX=16}`，全放行即 31。区域受限时服务端会强制降回 SFW，
   这是账号/地区策略，不是参数能越过的——产物文本要按实际返回说话。

- 唯一出口：`lib/civitai-api.js` 的 `requestData(url, opts)`，所有**数据类**请求都走它，
  它也是唯一决定 transport 的地方。二进制媒体（封面/配图/模型文件）仍走 `fetchJson`——
  页面通道只回文本且有 3MB 上限，不适合媒体。
- 装/拆：offscreen 用 `CFT.api.setFetcher(makePageFetcher(scope))` 在任务开始时安装，
  `withPageTransport(entries, label, fn)` 在 `finally` 还原；引用计数 + 共享 scope，
  批量并发 5 个 item 时不会互相踩掉别人的 fetcher。
- 选标签页：`ptPickTab` 按「页面 URL 精确匹配 → 同 host → 无 host 信息」三级挑，
  **绝不跨站借页面**（civitai.red 上的模型在 civitai.com 可能 404，借错页面会把 404 读成
  "这个模型没有评论"）。
- 白名单（content 侧 `pageFetchRejectReason`）：`https:` + host 属于
  `civitai.com|civitai.red`（含 `www.`）+ path 前缀 `/api/v1/` 或 `/api/trpc/`，
  `credentials:'same-origin'`，响应文本 ≤3MB，超限 `truncated:true` 且调用方按失败处理。
- 超时：`timeoutMs` 由调用方给（默认 `FETCH_TIMEOUT_MS=15000`），content 用它做 abort；
  offscreen 等 `timeoutMs + PT_REPLY_SLACK_MS(3000)`。content 侧默认 20000 仅在调用方没传时兜底。
- 短路：同一个 tab 一次不应答就进 `scope.dead`，本任务内不再向它发请求——否则批量 110 页时
  每组候选入参都要各等一轮超时。
- 401 熔断按 `procedure + transport` 分路记录（`page` / `direct`）：匿名直连看到的 401
  不得判死带登录态的页面通道，反之亦然。
- 回落与文案：页面通道「抛错 / ok:false / 被截断」才回落直连（直连用 `credentials:'include'`，
  被「通配 ACAO + 凭据」规则拦下时自动降级 `credentials:'omit'` 重试一次，最坏等于改造前行为）。
  产物文本要写清当次实际通道；"匿名直连拿到 0 条"不能写成"这个模型没有评论"。

### 2.2 C 站内部接口的实测入参/出参（2026-10-16 复测）

请求形态：`GET <base>/api/trpc/<procedure>?input=<urlencoded {"json":{...}}>`，带
`Sec-Fetch-Site: same-origin` + `Origin` + `Referer` 时**不再 401，而是正常 200/400**，
所以下面是真实 wire 契约；权威定义另见官方开源仓库 `civitai/civitai`（public, main）的
`src/server/schema/commentv2.schema.ts`、`src/server/schema/resourceReview.schema.ts`、
`src/server/services/commentsv2.service.ts`。

`commentv2.getInfinite`（评论）
- `entityType` **必须小写**：`'model'`。传 `'Model'` → 400 `invalid_value`（合法值含
  `question/answer/image/post/model/comment/review/article/bounty/bountyEntry/challenge/comicChapter/model3d/model3dReview`）。
- `entityId` 必填 number（传 `{modelId}` → 400 `path:["entityId"] expected number received undefined`）。
- `cursor` 只能省略或给 number：**第一页不能带 `cursor:null`** → 400 `expected number, received null`。
- `sort` ∈ `Newest | Oldest | Most Reactions`（默认 Oldest）；`limit` 1..100（默认 20）。
- `repliesDepth`（0..20）**必须显式给**，否则回复一条都拿不到：服务层 `depth < 1` 直接
  `return {threads:[], childlessCommentIds:[]}`。`repliesLimit` 1..50。
- `authed: true` 不在 schema 里，zod 静默丢弃——旧脚本里那几组 `authed` 候选是玄学。
- 出参 `result.data.json`：`null` 表示该模型没有 Thread 行（= 真的没有评论区，不是抓取失败）；
  否则 `{comments, nextCursor, targetComment, replyThreads, childlessCommentIds}`。
  评论节点字段 `id/createdAt/content/nsfw/hidden/threadId/pinnedAt/reactionCount/user/reactions`，
  **没有 `children`**；回复在 `replyThreads[]`，每项
  `{id, commentId, depth, commentCount, comments:[...], nextCursor?}`，`commentId` 是父评论 id。
  翻页用 `nextCursor`（number），为 null 即到底。

`resourceReview.getPaged`（评价）
- `modelId` **必填**；`modelVersionId` 是可选过滤（只传 `modelVersionId` → 400
  `path:["modelId"] expected number received undefined`）。归档单个版本时两者一起传。
- 分页字段是 **`page`** 不是 `cursor`（`paginationSchema = {limit, page}`；传 `cursor`
  被静默忽略，于是循环永远停在第 1 页）。出参
  `{items, totalItems, currentPage, pageSize, totalPages}`，按 `currentPage >= totalPages` 收。
- **`modelVersionId` 在 schema 里是 optional，但实测必须一起传**：同一 Key 下
  `{modelId:2989579}` → `items=0 total=0`，`{modelId:2989579, modelVersionId:3389879}` →
  `items=50 total=67`。只按 modelId 查会拿到"成功的空结果"，别把它当成该版本没有评价。
- 条目含 `rating`、`details`（正文）、`user`、`nsfw`、`metadata`、`thread{commentCount}`、
  `modelVersion{id,name,baseModel}`。

匿名形态（影响文案，不能忽略）：实测 most-downloaded 前四模型（4201/4384/133005/43331）匿名请求
这两个 procedure 全部 **200 但数据为空**。匿名不会报错，只会"成功地拿到 0 条"，所以
`0 条 + direct 通道` 的文案不能写成"这个模型没有评论/评价"。

### 2.3 桌面端取评论/评价（Python，`civitai_api.py`，本 fork 新增）

- 认证：`Authorization: Bearer <api_key>`，走 `build_opener`；`_NoAuthRedirect`
  （civitai_api.py:28）会在 307 预签名重定向时剥掉 Authorization，别绕过它把 Key 带进 CDN。
- 评价：`resourceReview.getPaged {modelId, modelVersionId, limit≤50, page}` 逐页取到
  `currentPage >= totalPages`；正文在 `details`，另有 `rating/recommended/quality/createdAt/user`。
- 评论：`commentv2.getInfinite {entityType:'model', entityId, sort:'Newest', limit, repliesDepth}`，
  第一页不带 cursor，用 `nextCursor`(number) 续页；`json:null` = 该实体没有评论区（不是失败）；
  回复在 `replyThreads[]`，父评论 id 是 `commentId`。实测 `stats.commentCount` 与 v2 线程不一致
  （模型 2989579 报 5，但 `entityType:'model'` 返回 null、67 条评价的 `commentCount` 全为 0），
  所以按实体逐个尝试、空就如实记空，不要把它当抓取失败。
- 产物：`评论.txt`（与扩展侧同名，扩展写在 `offscreen/archive-worker.js:909`），文本里标条数与来源；
  无 Key / 401 / 限流一律跳过，**不许影响下载主流程**。

## 3. 写盘通道（background 统一实现，offscreen 只发 writeFile）

目录结构（相对归档根）：

```
<safeFsName(模型名_版本)>/
  模型名.txt          # 模型信息（访问地址/关于此版本/介绍/其他参数）
  example.json        # {triggerWord, samples:[{prompt,negativePrompt,sampler,cfgScale,steps,Size}]}   [P2]
  评论.txt            # fetchCivitaiComments + fetchCivitaiReviews 拼接                        [P2]
  <模型名_版本>.jpg|mp4   # 封面（视频封面用 .mp4）                                              [P1]
  配图_N.jpg / 视频_N.mp4  #                                                                    [P2]
Images/<文件前缀>_<imageId>.jpg|.json   # SAVE 徽章单图归档                                     [P3]
_civitai_archive_history.json           # 历史镜像（导入导出格式）                               [P3]
```

通道选择（bg 的 writeFile 实现顺序）：
1. 软件在线且 `archive_root` 非空 → `POST http://127.0.0.1:47531/api/archive/file`
   `{rel_path, data_b64}`（原始大小 ≤ 32MB）。
2. 否则降级 `chrome.downloads.download({url: dataUrl, filename: 'CivitaiArchive/'+relPath,
   saveAs:false, conflictAction:'overwrite'})`；原始大小 > 20MB 拒绝，返回
   `{ok:false, msg:'文件过大且软件未在线'}`。
3. 响应统一 `{ok, saved: 'software'|'downloads'|'', channel, msg}`（`saved:''` 仅出现在失败响应；无 'skipped' 状态）。
4. 模型文件本体不经 writeFile：offscreen 发 `sendModelToSoftware {downloadUrl, pageUrl, destSubdir}`，
   bg POST `/api/download` body `{url, dest_subdir}`，由软件下载到 `<archive_root>/<dest_subdir>/`，
   与小文件同一归档目录闭环。

## 4. Python 侧（browser_bridge.py + webui.py + web/app.js）

- `GET /api/archive/config` → `{ok, archive_root, download_dir}`（archive_root = cfg 的
  `archive_root`，未设置则回落 `download_dir`；两者都空则 `archive_root=""`）。
- `POST /api/archive/file` body `{rel_path, data_b64}` → `{ok, path}`。安全：rel_path 用
  os.path.normpath 校验不得逃逸根目录（拒绝 `..`、绝对路径、盘符）；根目录未配置返回
  `{ok:false, msg:'归档根目录未设置'}`。
- `POST /api/archive/done` body `{dir_name}` → `{ok}`：起 daemon 线程触发模型库后台重扫（立即返回，
  不阻塞请求线程；尽力而为）。
- `GET /api/library/check?version_id=` → `{ok, found, paths:[...]}`：在模型库扫描缓存/目录中
  匹配 `.civitai.info`/`.json` 的 `version_id` 字段。
- browser_bridge.py 保持既有风格：handler 注册制（新增
  `set_archive_handlers(config_fn, write_fn, done_fn, check_fn)`），bridge 自身不 import webui；
  静默失败、CORS 白名单、origin 校验不变；Content-Length 上限放宽到 128MB（仅
  /api/archive/file）。
- 设置页（web/app.js 数据驱动设置项，见 2079-2160 区段）新增「归档根目录」行，key=
  `archive_root`，用现有 pick_dir 对话框模式。
- 不改动：downloader.py / civitai_api.py / gui.py / 模型管理其余功能。

## 5. 归档历史（Phase 3，lib/history.js）

- IndexedDB：库名 `civitai_archive_assistant`，store `history`，keyPath 无（key =
  `"${modelId}@${modelVersionId}"` 手动指定）。记录字段：
  `{modelId, modelVersionId, modelName, modelVer, dirName, archiveTime(ISO),
  modelFile, commentCount, imgCount}`。
- API：`CFT.history.all()` / `.get(modelId, versionId)` / `.put(record)` /
  `.delete(key)` / `.search(keyword)` / `.exportJson()` / `.importJson(json, merge)` /
  `.mirrorFile(writeFileFn)`（把全量历史写 `_civitai_archive_history.json`）。
- 已归档检测：`findArchivedRecord(records, modelId, modelVersionId)` 逻辑与脚本一致。

## 6. 插件配置（lib/config.js）

```js
CONFIG_DEFAULTS = {
  coverSaveMode: 'video',     // 'image' | 'video'
  downloadImages: true,
  downloadVideos: true,
  sendModelToSoftware: true,  // 模型文件本体转发软件下载（永远建议 true）
  maxPics: 100,
  badgeEnabled: true,
  globalConcurrency: 4,       // 写盘通道并发（chrome.downloads / 软件写盘）
  fallbackDirPrefix: 'CivitaiArchive'  // 降级 downloads 时的子目录前缀
}
```
存取走 `chrome.storage.local`，API：`CFT.config.load()` / `.save(patch)` / `.get()`。

## 7. 验证基线

- 所有 JS：`node --check <file>` 通过。
- Python：`python -m py_compile` 通过；不改坏现有单测（`python -m pytest` / unittest）。
- 提交前每个代理自检自己文件；跨文件集成问题由主控统一修。

---

## 8. 批量归档协议（多标签页批量扩展）

### 8.1 消息清单（信封 `{__cft:true,to,type,payload}`）

| from → to | type | payload → 响应 |
|---|---|---|
| popup → bg | `archiveBatchStart` | `{rows:[{tabId,url,title}]}` → `{ok, jobId, total}`（立即返回） |
| popup → bg | `archiveBatchStatus` | → `{running, jobId, phase, total, done, skipped, failed, current, results, errors, cancelRequested}` |
| popup → bg | `archiveBatchCancel` | → `{ok}`（设取消标志；已入队的模型文件不撤回） |
| bg → offscreen | `archiveBatch` | `{jobId, items:[{tabId,url,modelId,modelVersionId,pageUrl,aboutText}]}` → `{ok,started}` |
| offscreen → bg | `archiveBadge` | `{text}`（bg setBadgeText，批结束发空清掉） |
| popup/轮询 → offscreen | `archiveBatchStatus` / `archiveBatchCancel` | 同 bg 语义（bg 透传） |

### 8.2 offscreen 批量状态机（模块级 `batch`，与单页 `_running` 互斥）

```
batch = { jobId, cancel:false, phase:'prepare'|'media'|'comments'|'done',
          total, results:[{tabId,url,modelId,modelVersionId,status:'queued'|'done'|'skip'|'failed',
                           dirName,autoChosenFile?,msg?}], errors:[] }
```

- **阶段 prepare（并发 5）**：逐页 fetchModel → dirName=safeFsName(模型名_版本)；多文件自动选主文件（primary 优先，否则 files[0]，记 `autoChosenFile`）；已归档预检（`CFT.history.get(mid,ver)` 命中 → status='skip'）；命中后逐页 `sendModelToSoftware {downloadUrl, destSubdir:dirName}` 立即入队（POST /api/download）。每页结果存 results 并带 `modelData`（阶段 1 复用，避免重复 fetch）。
- **阶段 media（串行）**：逐页 `runArchive(payload,{batch:true,skipComments:true})`（batch 模式：跳过 askArchiveConfirm/askFileSelection、跳过逐页 notifyArchiveDone、支持 presetModelData 短路 fetchModel）；每页 try/catch 隔离。
- **阶段 comments（串行）**：对 status='done' 页逐页 fetchCommentsText（**纯 tRPC，跳过 DOM 兜底**——页面可能已关）→ writeFileToBg(dirName/'评论.txt')；失败记 errors 不阻断。
- **结束**：phase='done'；发一次 `notifyArchiveDone`（统一重扫）；badge 清空。
- 单页归档（`archive` 消息）行为不变；batch 与单页互斥（一方在跑另一方拒绝）。

### 8.3 bg 职责

- `archiveBatchStart`：校验 rows → 逐页 `chrome.tabs.sendMessage(collectPage)`（无应答/页关 → 用 URL 解析 modelId/versionId 兜底，aboutText 置空）→ ensureOffscreen → 发 `archiveBatch`；badge 初始 `0/N`。
- `archiveBatchStatus/Cancel`：透传 offscreen。
- 收到 `archiveBadge`：setBadgeText（批结束空文本清除）。

### 8.4 popup UI

- 批量列表旁「批量归档」按钮（与「发送到下载」并列）：对 selected rows 发 `archiveBatchStart`。
- 状态区轮询 1s：phase/进度 N/M/当前页/skip/failed 计数 + autoChosen 提示（"多文件版本已自动选择主文件 xxx"）+ 取消按钮；popup 关闭不影响任务（队列在 offscreen）。
- 单页「一键归档」保持原行为。
