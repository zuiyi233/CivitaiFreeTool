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
| offscreen → bg | `writeFile` | `{relPath, dataB64, overwrite}` 或 `{relPath, dataB64, overwrite, bundle, filename}` → `{ok, saved, channel, msg}`（有 `bundle` 走令牌通道写进分流落点，`relPath` 只作降级命名；无令牌退回 rel_path 老形态） | P1 |
| offscreen → bg | `getArchiveConfig` | → `{softwareOnline, archiveRoot, channel}` | P1 |
| offscreen → bg | `sendModelToSoftware` | `{downloadUrl, pageUrl, destSubdir}` → `{ok, msg, bundle, destDir, routed}`（POST 软件桥 /api/download，body `{url, dest_subdir, caps:['bundle']}`；桌面端分流时本体落 `<comfyui_models_root>\<类型key>\<模型名>\` 并回令牌，后续附属文件同目录；未声明 caps 或分流判不成则回落 `<archive_root>/<dest_subdir>/`） | P1 |
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
| content → bg | `saveSingleImage` | `{imageId, originalUrl, ext, pageUrl}` → `{ok}`；`pageUrl` 是发起文档 URL（content 必带，bg 缺省时用 `sender.tab.url` 兜底），offscreen 用它定站点并登记页面通道，见 §2.1 硬约束 1 | P3 |
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
- 选标签页（`ptPickTab`）：只有「这条请求的站点 host」与「这个 tab 所在页面的站点 host」**双方都能
  确定且相等**时才借，匹配顺序是「页面 URL 完全一致 → 同站点 host」，除此之外一律返回 `null` 并
  回落扩展直连。**判不出站点就绝不借道**（`ptMakeEntry` 连登记都不登记）：跨站借页面时 content 侧
  是 `credentials:'same-origin'`，跨站等于不带 Cookie，拿回来的是匿名数据，却被 `makePageFetcher`
  记成 `pageOk` / `transport=page`，产物就会写"数据来自页面通道（带你的登录状态）"——那是把匿名
  跨站请求粉饰成登录态取数。civitai.red 上的模型/图在 civitai.com 可能 404 或另一个结果，借错
  页面还会把 404 读成"这个模型没有评论"。
  （旧实现里 `alive[0]` 与「entry 无 host 信息」两条兜底正是这么骗人的，已删；不要再加回来。）
- 硬约束（三条，实现与测试 tests/test_page_transport.cjs 都要钉住）：
  1. **单图链路必须带发起页 URL**：`image-badge.js` 的 `saveSingleImage` payload 必带当前文档 URL
     （`currentPageUrl()`）；`background.js` 的 `forwardSaveSingleImage` 在 payload 缺 `pageUrl` 时用
     `sender.tab.url` 兜底（content 自己报的优先，不覆盖）；`runSingleImage` 消费它。它同时决定
     元数据请求打到 `civitai.com` 还是 `civitai.red`（`civitaiBaseUrl(pageUrl)`，镜像站数据独立），
     以及能不能借这个 tab。两处都拿不到时 `pageUrl` 保持空串 → 页面通道不安装 → 走直连并在产物里
     如实写"扩展直连（多半按匿名处理）"。
  2. **host 判不出即不借**：见上一条。拿不到可用 tab 时走"不可用：没有与该请求站点匹配的标签页"
     分支，让调用方如实回落直连，而不是借一个错的 tab。
  3. **重定向不跟随**：content 侧 `runPageFetch` 用 `redirect:'manual'`，收到 3xx 时应答
     `ok:false` + 专门文案（"站点返回重定向，页面通道不跟随…"），绝不向跳转目标发第二个请求。
     选 `'manual'` 而不是 `'error'` 是实测结论：Chromium 在 `redirect:'error'` 下抛
     `TypeError("Failed to fetch")`，与 CORS/网络失败同一句话，文案只能说"可能是重定向"；
     `manual` 给出 `type:'opaqueredirect'` + `status:0` + 空体，跳转目标命中 0 次（真浏览器断言见
     tests/test_page_transport.cjs），且 200 响应完全不受影响。"白名单拒绝"与"被重定向拒发"两种
     文案必须分开——前者是扩展一个字节都没发出去，后者是站点确实改了跳转目标。
- 白名单（content 侧 `pageFetchRejectReason`）：`https:` + host 属于
  `civitai.com|civitai.red`（含 `www.`）+ path 前缀 `/api/v1/` 或 `/api/trpc/`，
  `credentials:'same-origin'`，响应文本 ≤3MB，超限 `truncated:true` 且调用方按失败处理。
- 超时：`timeoutMs` 由调用方给（默认 `FETCH_TIMEOUT_MS=15000`），content 用它做 abort；
  offscreen 等 `timeoutMs + PT_REPLY_SLACK_MS(3000)`。content 侧默认 20000 仅在调用方没传时兜底。
  content 的 `pageFetchResult` 走 sendResponse 与 runtime 广播**两条路**，`askContent` 必须两边都消费
  （`resolveCid` 先到先得、天然去重）：只认广播那条路时，广播一断一条合法 200 会白等满超时并把该 tab
  永久短路。
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

三种请求形态实测对照（同一 procedure、同一模型 2989579 / 版本 3389879，2026-10-16 复测）：

| 请求形态 | `resourceReview.getPaged`（两 ID 同传） | `commentv2.getInfinite`（entityType=model） |
| --- | --- | --- |
| 裸 GET（无浏览器头、无 Key） | **401** `Please use the public API instead` | **401** |
| 带 `Sec-Fetch-Site: same-origin` + `Origin` + `Referer`，无 Cookie | **200 + 67 条真实评价** | 200，但 `comments:[]` |
| 带 `Authorization: Bearer <API Key>`，无浏览器头 | **200 + 67 条真实评价** | 200，该模型 `json:null`（无 Thread 行） |

结论要分开说，别混成"接口关了"：
- **评价不需要登录态**——只要有请求上下文（浏览器同源）或带 Key 就给真数据。之前记成"匿名拿到空"
  是因为只传了 `modelId` 没传 `modelVersionId`，那是**假空**（200 + `totalItems:0`）。
- **评论需要真会话**：带同源头但无 Cookie 仍 `comments:[]`；而且**API Key 也不行**——带 Key 对
  `model/image/post/article/review` 各种 entityType 的 `commentv2.getCount` 一律返回 0，v1 的
  `comment.getInfinite` 线上 404。所以评论正文只有"用户已登录的标签页代发"这一条路。
- `resourceReview.getInfinite` 三种入参都返回 200 但 `items:[]`（同一版本 `getPaged` 有 67 条），
  **不能当评价的主通道**，只能在版本级一条都没落地时兜底，且要标明它是模型级、只含写了文字的评价。
- 桌面端**不要靠伪造 `Sec-Fetch-*` 绕过** 401：那是绕过站点"请用公开 API"的门禁。带用户自己的
  API Key 是官方 token 路径（`commentv2Router.getInfinite` 的 `.meta({requiredScope: TokenScope.MediaRead})`
  说明 tRPC 本来就认 token），也是扩展与桌面端共用的正当口径。
- 匿名请求"成功的空"和"真的没有"必须靠通道 + 参数区分，产物文案不能把前者写成"该模型没有评论/评价"。

登录态实测补记（2026-10-16，在本机 Chrome 打开的 civitai.com 模型页里做同源只读请求）：
- 那次浏览器**未登录**（`GET /api/v1/me` → 401），所以只验证到"契约形状正确"：
  `commentv2.getInfinite` 200 返回 `{comments,nextCursor,targetComment,replyThreads,childlessCommentIds}`；
  存在的模型 → `comments:[]`，不存在的模型（123456）→ `json:null`。**这两个分支正是代码判定的实测依据。**
- 未登录时模型页 DOM **连 Comments 标签都不渲染**（整页文本里没有 "comments"），
  与"评论要登录会话"一致，而不是"这些热门模型恰好没评论"。
- **仍未被任何路径证实**：带真实登录会话时页面通道能否拿到非空评论正文。要证它需要一个已登录的
  C 站标签页。在此之前，评论功能一律按"通道已接通、非空结果未证实"交付，产物文案也不得写
  "已拿到全部评论"。

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
1. 软件在线且拿到 **落点令牌** → `POST http://127.0.0.1:47531/api/archive/file`
   `{bundle, filename, data_b64}`（原始大小 ≤ 32MB）。`filename` 必须是**纯基名**，不含任何路径
   分隔符；目标目录完全由桌面端按令牌解析，扩展不再传路径。
2. 无令牌（老桌面端，或桌面端判定不分流）→ 同一端点退回 `{rel_path, data_b64}`，
   落 `<archive_root>/<rel_path>`，行为与令牌协议引入前逐字一致。
3. 两者都不可用 → 降级 `chrome.downloads.download({url: dataUrl, filename: 'CivitaiArchive/'+relPath,
   saveAs:false, conflictAction:'overwrite'})`；原始大小 > 20MB 拒绝，返回
   `{ok:false, msg:'文件过大且软件未在线'}`。
4. 响应统一 `{ok, saved: 'software'|'downloads'|'', channel, msg}`（`saved:''` 仅出现在失败响应；无 'skipped' 状态）。
5. 模型文件本体不经 writeFile：offscreen 发 `sendModelToSoftware {downloadUrl, pageUrl, destSubdir}`，
   bg POST `/api/download` body `{url, dest_subdir, caps:['bundle']}`。桌面端**按类型分流**时本体落
   `<comfyui_models_root>\<类型key>\<模型名>\`，并回 `{bundle, dest_dir, routed:true}`；本次归档的
   封面/配图/视频/模型信息 txt/example.json/评论全部改走该令牌的 `bundle` 通道，与本体同目录。
   分流判不成才回落 `<archive_root>/<dest_subdir>/`。
6. **顺序硬约束**：`sendModelToSoftware` 必须先于任何附属文件写入完成——令牌是随下载提交回的，
   并发跑就会在令牌到手前按 rel_path 写盘，本体与附属文件分家（这正是本次修的缺陷）。
7. `bag.dirName` 语义保持不变（仍是 `<归档根>` 下的相对目录名，用于历史与降级命名）；分流后的
   真实绝对落点另存 `bag.destDir`，写进历史记录的 `destDir`/`routed` 字段，不覆写 `dirName`。

## 4. Python 侧（browser_bridge.py + webui.py + web/app.js）

- `GET /api/archive/config` → `{ok, archive_root, download_dir}`（archive_root = cfg 的
  `archive_root`，未设置则回落 `download_dir`；两者都空则 `archive_root=""`）。
- `POST /api/archive/file`：
  - `{bundle, filename, data_b64}` → `{ok, path}`。`bundle` 是桌面端 `secrets.token_urlsafe(16)`
    生成的不透明令牌，注册表在 `browser_bridge.py` 模块级（30 分钟**滑动** TTL、每次写入续期、
    容量 256 按最久未写入淘汰、`stop()` 时清空）。`filename` 桌面端自己也要拒 `/`、`\`、`..`、
    盘符、UNC、控制字符、空串——不能只信扩展过滤过。令牌无效/过期返回 400 与「请重新发送模型」。
  - `{rel_path, data_b64}` → `{ok, path}`。安全：rel_path 用 os.path.normpath 校验不得逃逸
    （拒绝 `..`、绝对路径、盘符）；根目录未配置返回 `{ok:false, msg:'归档根目录未设置'}`。
    **该形态的合法根只有归档根**，不随下面的扩根放开：多根匹配会让 `../<兄弟 models 目录>/x`
    被第二个根接受，等于给旧通道新开一面写盘。
  - `bundle` 形态的允许根 = 归档根 + `comfyui_models_root` + `models_dirs`（分流落点在 ComfyUI
    目录下、归档根之外），并拒绝 reparse/junction 目录。
- `POST /api/download` body `{url, dest_subdir, caps}`：`caps` 含 `"bundle"` 才允许分流。
  老扩展只发 `{url, dest_subdir}`，若照样分流，本体会进 ComfyUI 而附属文件被它写回
  `<archive_root>/<dirName>`，造成分裂归档。桌面自身 UI 的下载由桌面自己写附属文件，不受该门限约束。
  响应 `{ok, msg, bundle?, dest_dir?, routed?}`。
- `POST /api/archive/done` body `{dir_name, bundle?}` → `{ok}`：起 daemon 线程触发模型库后台重扫
  （立即返回，不阻塞请求线程；尽力而为）。**空 `dir_name` 不再返回 400**——扩展批量结束恒传空值，
  此前重扫从未真正触发；分流后 `dir_name` 已不代表路径，结束即重扫。带 `bundle` 时顺带释放令牌。
- `GET /api/library/check?version_id=` → `{ok, found, paths:[...]}`：在模型库扫描缓存/目录中
  匹配 `.civitai.info`/`.json` 的 `version_id` 字段。
- browser_bridge.py 保持既有风格：handler 注册制（新增
  `set_archive_handlers(config_fn, write_fn, done_fn, check_fn)`），bridge 自身不 import webui；
  静默失败、CORS 白名单、origin 校验不变；Content-Length 上限放宽到 128MB（仅
  /api/archive/file）。桥 handler 入参用签名探测兼容（`_handler_wants_caps`），老的两参
  handler 绝不硬塞第三参。
- 设置页新增 `route_by_type`（按类型分流，默认关）与 `comfyui_models_root`（ComfyUI 的 models
  根目录）两行；`data-dirpick`/`data-dirclear` 改为按 key 分派，只有 `download_target_dir`
  走服务端校验的 `set_download_target`。
- 不改动：downloader.py 的断点续传语义 / civitai_api.py 的下载端点白名单 / gui.py / 模型管理其余功能。

## 5. 归档历史（Phase 3，lib/history.js）

- IndexedDB：库名 `civitai_archive_assistant`，store `history`，keyPath 无（key =
  `"${modelId}@${modelVersionId}"` 手动指定）。记录字段：
  `{modelId, modelVersionId, modelName, modelVer, dirName, archiveTime(ISO),
  modelFile, commentCount, imgCount, destDir, routed}`（`dirName` 仍是归档根下的相对目录名，
  分流后的真实绝对落点另存 `destDir`，完成消息与批量状态优先报 `destDir`）。
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

- **阶段 prepare（并发 5）**：逐页 fetchModel → dirName=safeFsName(模型名_版本)；多文件自动选主文件（primary 优先，否则 files[0]，记 `autoChosenFile`）；已归档预检（`CFT.history.get(mid,ver)` 命中 → status='skip'）；命中后逐页 `sendModelToSoftware {downloadUrl, destSubdir:dirName}` 立即入队（POST /api/download，带 `caps:['bundle']`），并把回包的 `bundle`/`destDir`/`routed` 存进该页结果。每页结果存 results 并带 `modelData`（阶段 1 复用，避免重复 fetch）。
- **阶段 media（串行）**：逐页 `runArchive(payload,{batch:true,skipComments:true})`（batch 模式：跳过 askArchiveConfirm/askFileSelection、跳过逐页 notifyArchiveDone、支持 presetModelData 短路 fetchModel）；每页 try/catch 隔离。
- **阶段 comments（串行）**：对 status='done' 页逐页 fetchCommentsText（**纯 tRPC，跳过 DOM 兜底**——页面可能已关）→ writeFileToBg(dirName/'评论.txt')，该页 prepare 阶段拿到过 `bundle` 就走令牌通道写进分流落点（令牌 30 分钟滑动 TTL，跨阶段仍有效）；失败记 errors 不阻断。
- **结束**：phase='done'；发一次 `notifyArchiveDone`（统一重扫）；badge 清空。
- 单页归档（`archive` 消息）行为不变；batch 与单页互斥（一方在跑另一方拒绝）。
- **单图（`saveSingleImage`）同样占这把锁**：`archive` / `archiveBatch` / `saveSingleImage` 三个入口
  由同一个 `anyTaskActive()` 互斥。理由是页面通道的 fetcher 是 lib 的模块级单例（`CFT.api.setFetcher`）
  且 `_ptScope` 只有一份——两个任务并发时后装的会盖掉前一个、entries 还会被 `ptBegin` 并到同一个 scope，
  「谁的请求打到谁的 tab」就失控了（旧实现里单图那个 host='' 的 tab 正是这么被并进批量 scope 的）。
  给 scope 加 taskKey 只挡得住"合并 entries"，挡不住"共用一个全局 fetcher"，所以选锁不选 key。

### 8.3 bg 职责

- `archiveBatchStart`：校验 rows → 逐页 `chrome.tabs.sendMessage(collectPage)`（无应答/页关 → 用 URL 解析 modelId/versionId 兜底，aboutText 置空）→ ensureOffscreen → 发 `archiveBatch`；badge 初始 `0/N`。
- `archiveBatchStatus/Cancel`：透传 offscreen。
- 收到 `archiveBadge`：setBadgeText（批结束空文本清除）。

### 8.4 popup UI

- 批量列表旁「批量归档」按钮（与「发送到下载」并列）：对 selected rows 发 `archiveBatchStart`。
- 状态区轮询 1s：phase/进度 N/M/当前页/skip/failed 计数 + autoChosen 提示（"多文件版本已自动选择主文件 xxx"）+ 取消按钮；popup 关闭不影响任务（队列在 offscreen）。
- 单页「一键归档」保持原行为。
