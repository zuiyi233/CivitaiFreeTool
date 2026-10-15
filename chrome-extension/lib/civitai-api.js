// CFT.api —— Civitai trpc 接口层（搬运自油猴脚本 v1.7.8，逻辑原样保留）
// 适配改动（相对脚本原码）：
//   1) civitaiBaseUrl：脚本读 window.location.hostname；扩展 SW/offscreen 的 location 是
//      chrome-extension://<id>，永远判不出镜像站，改为接受可选参数 pageUrlOrHost（归档时
//      由调用方传页面 URL 或裸 host）。入参只认 CIVITAI_HOST_RE 白名单，window 分支保持
//      脚本原判定；两者都不命中才回落 https://civitai.com（无参调用与原行为一致）。
//   2) fetchCivitaiComments：删除脚本 1510-1522 的 DOM 兜底段，改为签名
//      fetchCivitaiComments(modelId, domFallbackFn, pageUrlOrHost)；trpc 无结果且 domFallbackFn
//      为函数时 results = await domFallbackFn()（与原 results 同构数组），再走同样的格式化。
//   3) 新增 fetchJson(url, options)：带超时的 fetch 封装（默认 15s，AbortController），
//      返回 Response；支持 options.timeoutMs 覆盖与外层 options.signal 联动取消。
//   4) trpc helper（buildTrpcUrl / fetchCivitaiComments / fetchCivitaiReviews /
//      getImageGenerationData）末位加可选 pageUrlOrHost，把请求打到页面所在的站点
//      （civitai.red 镜像上的模型在 civitai.com 可能不存在）。
//   5) [2026-10-15 站点变更 / 2026-10-16 更正] 内部 trpc 关闭的是**没有请求上下文的匿名探测**：
//      裸 GET（不带 Sec-Fetch-Site / Origin / Referer）一律 HTTP 401 +
//      {"message":"Please use the public API instead: https://developer.civitai.com/"}；一旦带上
//      这三个同源特征头（页面通道由浏览器自动加），同一 procedure 就回 200/400——400 只是入参不
//      合 Zod schema，不是鉴权。实测（2026-10-16，civitai.com 模型 4201/133005/43331）：
//      resourceReview.getPaged 连登录态都不需要就能拿到真实数据（modelId+modelVersionId → totalItems
//      12706），而 commentv2.getInfinite 回 200 但 comments 恒为空。所以「接口已被站点硬关闭」这个
//      旧结论作废：关的是匿名上下文，带登录态的同源请求仍是 200（见下面 7) 与 8)）。
//      据此（口径按「匿名被拒」保留，但只适用于裸请求）：
//        a. 样图生成数据改走公开 API GET /api/v1/images?imageId=&withMeta=true&limit=1
//           （与桌面端 civitai_api.py:103 get_image_metadata 同一调用，实测 200 +
//           access-control-allow-origin:*，无需密钥）；getImageGenerationData 因此不再
//           返回 tRPC 信封 {result:{data:{json}}}，改为返回归一化 core（见函数注释）。
//        b. 评论/评价没有公开替代端点（/api/v1/models/{id}/comments、/api/v1/comments、
//           /api/v1/model-versions/{id}/reviews 实测全部 404 HTML），只能降级到 DOM 抓取；
//           同时加「按 procedure + 通道 粒度」的 401 熔断，避免一堆注定失败的候选入参 × 翻页
//           在 110 页批量里打成上千次请求（限流风险 + 拖慢归档）。
//   6) 公开 API 无密钥时不返回 NSFW 图的元数据：GET /api/v1/images?imageId=145224191
//      （nsfwLevel 4）实测 200 但 body 为 {"items":[],"metadata":{}}，而 SFW 的
//      imageId=145676249 返回完整 meta。扩展没有也不该去搞 API key，所以**匿名直连**时
//      NSFW 样图必然拿不到提示词——这个原因必须原样体现在产物文本里，不能写成「该图没有提示词」。
//      带上登录态的页面通道是否会放开没有实测依据，所以文本还要交代当次实际走的通道（见 7)）。
//   7) [2026-10-15 传输层] 5) 的 401 和 6) 的空 items 只证明了「匿名请求」的结果，
//      没有证明接口死了。油猴脚本时代这些 fetch 发生在 civitai 页面里（同源，浏览器自动
//      带上登录 Cookie），移植到 offscreen 之后源变成 chrome-extension://（跨站、不带
//      Cookie），同一套接口在登录用户那里照常可用、在扩展里却只剩 401/空 items——丢的是
//      登录上下文，不是功能。据此加可插拔传输层：
//        a. setFetcher(fn)：调用方（offscreen 归档任务）注入「页面通道」fetcher，由
//           content script 在页面里代发同源带 Cookie 的 GET（信封：发
//           pageFetch{url,cid,timeoutMs} / 收 pageFetchResult{cid,ok,status,body,err,truncated}）。
//           fn(url, opts) → {ok, status, text}（可以抛错）；setFetcher(null) 还原默认。
//        b. requestData(url, opts)：**所有** Civitai 数据请求的唯一出口，也是唯一决定
//           transport 的地方。装了 fetcher 先走页面，页面「无标签页 / 不应答 / 超时 /
//           ok:false / body 被截断」时回落直连；没装 fetcher 时直接走直连
//           （fetchJson + credentials:'include'——真 Chrome 里单靠这一条也可能恢复部分
//           会话数据，但它是兜底通道，不是主通道；带凭据的直连若被「通配 ACAO + credentials」
//           规则拦下，会自动降级 credentials:'omit' 重试一次，最坏只等于改造前的行为）。
//        c. 401 熔断按 **procedure + transport** 记：匿名直连看到的 401 绝不能把带登录态的
//           页面通道一起判死（反之亦然）。这正是 5) 那条结论的适用范围修正。
//        d. 文案跟着事实走：数据是页面通道拿到的，就绝不写「接口已被站点关闭」。
//        e. 只有「数据类」请求走 requestData；封面/配图/模型文件是二进制媒体，仍走
//           fetchArrayBuffer→fetchJson（页面通道只回传文本且有 3MB 上限，不适合媒体）。
//   8) [2026-10-16 入参/响应实测校正] 过了 7) 的同源门之后，脚本搬来的入参本身就被服务端拒了，
//      而且解析器读的字段名也不存在——所以评论「一直为空」有两个独立原因。逐条实测（%TEMP% 探针）：
//      a. commentv2.getInfinite 的 entityType 枚举是**小写** model（'Model' 直接 400，Zod 把 16 个
//         合法值原样吐回来）。commentConnectorSchema 只有 entityId/entityType(+parentThreadId/
//         excludedUserIds)，所以旧候选里那三组 {modelId,…} 写法是必然 400 的死参数，已删。
//      b. cursor: z.number().optional()——第一页**必须整个省略**（null 会被判
//         "expected number, received null"），翻页用的 nextCursor 是**上一页最后一条评论的 id
//         （数字）**，不是字符串 token（服务端只在 regularComments.length === limit 时才给）。
//      c. resourceReview.getPaged 的 modelId 只是 Zod **必填校验**（numericString），SQL 里
//         根本不用它：WHERE 只有 rr."modelVersionId" = ${modelVersionId}。所以「只给 modelId」会
//         拿到 200 + totalItems:0（假「这个模型没人评价」），「只给 modelVersionId」会 400。
//         实测 {modelId, modelVersionId} 同时给 → 200 + 真实数据。
//      d. resourceReview.getInfinite 可用作**兜底**：入参 {modelId | modelVersionId, cursor?, limit}
//         且服务端固定 `details IS NOT NULL`（只要写了字的评测），实测匿名 200 拿到真实正文
//         （模型 4384：details="Спасибо!" / "This model is good for just about anything…"），
//         nextCursor 同样是数字 id。它按 modelId 聚合、丢掉了纯打分条目，所以不能顶替版本级全量：
//         主通道仍是 getPaged（见 9)a.），getInfinite 只在版本级一条都没落地时才发。
//      e. 评论响应 result.data.json = {comments, nextCursor, targetComment, replyThreads,
//         childlessCommentIds}；旧解析读 data / data.items → 200 也恒定 0 条。回复在
//         **replyThreads[].comments[]**（ReplyThread={id,commentId,locked,depth,commentCount,
//         comments[],nextCursor?}），旧代码读的 it.children[] 任何形状下都不存在。
//      f. replyThreads 只有在入参带 **repliesDepth** 时才会填（服务端：
//         repliesDepth ? getReplyThreads(…) : {threads:[],childlessCommentIds:[]}）→ 不传就永远空。
//      g. 评论对象字段是 {id, createdAt, content, nsfw, hidden, threadId, pinnedAt, reactionCount,
//         user:{id,username,image,profilePicture,cosmetics}, reactions[]}：顶层**没有 userId**，
//         正文是**消毒后的 HTML**（<p>…</p>），必须转纯文本再落盘。
//      h. 匿名看不到评论内容：同一模型 /api/v1/models 的 stats.commentCount=483，而
//         commentv2.getInfinite 与 commentv2.getCount 匿名都返回空（未登录的模型页 DOM 里连评论区
//         都不渲染）。仓库源码的 getInfinite 是 publicProcedure、SQL 也没按登录态过滤，说明线上
//         还有一层按会话剥离——结论：评论必须靠 7) 的页面通道（带登录 Cookie）才有数据。这次修的
//         是「参数被拒 + 字段名读错」这两条**与登录无关**的硬伤，它们会把本来有数据的页面通道也打成 0 条。
//      i. 超时：评论/评价请求显式带 timeoutMs=12000，页面侧 fetch 上限也钳到 12000（见 offscreen
//         PT_PAGE_FETCH_TIMEOUT_MS），保证页面先放弃并回话，而不是调用方的请求-答复先超时、
//         把随后到达的 pageFetchResult 丢掉。
//   9) [2026-10-16 候选入参与空结果文案] 按 8) 的契约重写这两个函数时定下的两条规矩：
//      a. 候选入参只留「能测出不同事实」的组合：主候选 = 站点当前 schema 的正解；评论额外留一组
//         **去掉可选键**的最小形状，且只有主候选拿到 HTTP 400（Zod 把入参拒了）时才会用到它——
//         0 条是站点对这组参数的真实回答，换参数不会变多，所以不再像旧代码那样对同一模型把 5 组
//         玄学入参各打 5 页。authed 不在 schema 里（Zod 直接丢弃），已删。
//      b. 「HTTP 200 + 空列表」不是一种结论，必须结合当次通道说话（见 requestData 的 transport）：
//         页面通道（带登录 Cookie）的空 = 可以说「没有评论/评价」；匿名直连的空 = 只能说
//         「匿名拿不到受限内容，请在 C 站页面发起归档以借用登录态」（实测：1 万+ 评价的模型，评论
//         接口对匿名一样回 comments:[]）。
//      c. 评论的 result.data.json 有两种空，含义不同：null（服务端没有该模型的 Thread 行，
//         实测不存在/无评论区的模型就返回 {"result":{"data":{"json":null}}}）= 「这个模型没有
//         评论区」；对象但 comments:[] = 本次通道看不到评论。前者绝不能写进抓取失败那套文案。
globalThis.CFT = globalThis.CFT || {};

// 站点白名单（与 tabs-common.js 的 modelLink 同形）：civitai.com / civitai.red，可带 www.
const CIVITAI_HOST_RE = /^(?:www\.)?civitai\.(?:com|red)$/i;

// 页面 URL / 裸 host → 允许的站点 host；不合法或不在白名单返回 ''（绝不让页面串当请求目标）
// 只取 hostname：URL 上的端口、用户名密码在解析时自然丢弃，无法拼进请求地址
function civitaiHostFrom(pageUrlOrHost) {
    const raw = String(pageUrlOrHost || '').trim();
    if (!raw) return '';
    let hostname = '';
    if (/^https?:\/\//i.test(raw)) {
        try { hostname = new URL(raw).hostname; } catch (_) { return ''; }
    } else if (/^[a-z0-9.-]+$/i.test(raw)) {
        hostname = raw;
    } else {
        return '';
    }
    return CIVITAI_HOST_RE.test(hostname) ? hostname : '';
}

// 当前站点判断：入参（offscreen/SW 无页面 location，只能调用方给）→ window → 默认 civitai.com
const civitaiBaseUrl = (pageUrlOrHost) => {
    const fromArg = civitaiHostFrom(pageUrlOrHost);
    if (fromArg) return 'https://' + fromArg;
    try {
        const host = (typeof window !== 'undefined' && window.location && window.location.hostname) || '';
        if (String(host).includes('civitai.red')) return 'https://civitai.red';
    } catch (_) { /* 非页面环境走默认 */ }
    return 'https://civitai.com';
};

// ---- trpc 工具（评论/评价；样图元数据已迁到公开 API，见 getImageGenerationData）----
function buildTrpcUrl(procedureName, inputJson, pageUrlOrHost) {
    return civitaiBaseUrl(pageUrlOrHost) + '/api/trpc/' + procedureName + '?input=' + encodeURIComponent(JSON.stringify({ json: inputJson }));
}
function extractTrpcData(jsonBody) {
    try {
        return jsonBody?.result?.data?.json ?? null;
    } catch (_) {
        return null;
    }
}
function commentUserLabel(node) {
    try {
        // 实测/源码：评论对象是 {id, createdAt, content, user:{id, username, image, …}, reactions[]}
        // —— 顶层**没有 userId**，所以兜底必须先看 user.id，否则每一行都会写成「用户#?」。
        return (node?.user?.username || node?.user?.name || '用户#' + (node?.user?.id ?? node?.userId ?? '?')) + '';
    } catch (_) {
        return '匿名';
    }
}

// ---- tRPC 响应解析（2026-10-16 按站点源码 + live 探针校准，见文件头 8)）----
const HTML_ENTITY_MAP = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#039;': "'", '&apos;': "'", '&nbsp;': ' ' };

// 评论/评测正文是**消毒后的 HTML**（服务端 allowedTags: p/div/strong/em/u/s/a/br/span/code/pre…），
// 直接落盘会把 <p>great</p> 写进 评论.txt。优先复用 CFT.util（content/offscreen 都按契约先注入
// util.js），拿不到时用等价的本地剥离，绝不因为缺工具而回吐原始 HTML。
function commentPlainText(v) {
    if (v == null) return '';
    if (typeof v !== 'string') return String(v);
    if (v.indexOf('<') < 0) return v.replace(/\s+$/g, '');
    try {
        const U = globalThis.CFT && globalThis.CFT.util;
        if (U && typeof U.htmlToPlainTextFormatted === 'function') return String(U.htmlToPlainTextFormatted(v));
    } catch (_) { /* 走本地兜底 */ }
    return v
        .replace(/<br\s*\/?>/gi, '\n')
        .replace(/<\/(p|div|li|h[1-6])>/gi, '\n')
        .replace(/<\/?[a-zA-Z!][^>]*>/g, ' ')
        .replace(/&(?:amp|lt|gt|quot|#039|apos|nbsp);/gi, (m) => HTML_ENTITY_MAP[m.toLowerCase()] || m)
        .replace(/[ \t]+/g, ' ')
        .replace(/\n{3,}/g, '\n\n')
        .trim();
}

function asList(v) { return Array.isArray(v) ? v : []; }

// 主列表：新版 comments[]（评论）/ items[]（评测 getPaged 与 getInfinite）/ 裸数组（历史形状）
function trpcListFromData(data, prefer) {
    if (Array.isArray(data)) return data;
    if (!data || typeof data !== 'object') return [];
    const keys = prefer === 'items' ? ['items', 'comments'] : ['comments', 'items'];
    for (const k of keys) if (Array.isArray(data[k])) return data[k];
    return [];
}

// 回复：ReplyThread = {id, commentId, locked, depth, commentCount, comments[], nextCursor?}
// 旧代码读 it.children[]——站点从来没给过这个字段（文件头 8)e.）。保留 children 作为容错。
function repliesFromThread(t) {
    if (!t || typeof t !== 'object') return [];
    if (Array.isArray(t.comments)) return t.comments;
    if (Array.isArray(t.children)) return t.children;
    return [];
}

// cursor 是 z.number().optional()，nextCursor 是「上一页最后一条的 id」（数字）；
// superjson 把 undefined 序列化成 null，所以这里统一：数字→用，纯数字字符串→转数字，其余→停止翻页。
function normalizeTrpcCursor(v) {
    if (typeof v === 'number' && Number.isFinite(v)) return v;
    if (typeof v === 'string' && /^\d+$/.test(v.trim())) return Number(v.trim());
    return null;
}

// 区分「站点明确回答 null」和「这根本不是 tRPC 信封」：
// commentsv2.service.ts 的 getCommentsInfinite 在 `if (!mainThread) return null` 时，HTTP 仍是 200，
// 信封是 {"result":{"data":{"json":null}}}（实测不存在的 entityId 就是这个形状）。
// 这一句的含义是「该模型没有评论区（服务端没有对应 Thread 行）」，属于**事实**，
// 不能和「解析不出来 / 不是 JSON」那种抓取失败混在一起。
function trpcJsonIsNull(body) {
    return !!(body && typeof body === 'object' && body.result && body.result.data
        && Object.prototype.hasOwnProperty.call(body.result.data, 'json')
        && body.result.data.json === null);
}

// HTTP 400 + error 信封 = Zod 把**入参**拒了（接口契约又变了），既不是没数据也不是鉴权失败。
// 返回可读短语，供文案与「改用最小入参重试」这条降级路径判断使用。
function trpcInputRejected(res, body) {
    if (Number(res && res.status) !== 400) return '';
    const raw = body && body.error && body.error.json && body.error.json.message;
    const s = String(raw || '').replace(/\s+/g, ' ').trim();
    return (s || 'HTTP 400（入参被 Zod 拒绝）').slice(0, 200);
}

// 评价接口（resourceReview.getPaged）的 modelId 是 Zod 必填校验项，而调用方未必给得到
// （历史调用只传 versionId）。按代价从低到高补齐，绝不编造：
//   1) 显式入参；2) 页面 URL 的 /models/<id>（归档时 pageUrl 就是模型页，零请求）；
//   3) 公开 API GET /api/v1/model-versions/<id>（实测返回 modelId、无需密钥，只在缺前两条时打一次）。
// 三条都拿不到就**不发** trpc 请求：缺 modelId 的 getPaged 必然 400，白打一次请求还写不出真因。
function modelIdFromPageUrl(pageUrlOrHost) {
    const m = String(pageUrlOrHost || '').match(/\/models\/(\d+)/i);
    return m ? Number(m[1]) : NaN;
}
async function resolveReviewModelId(versionId, pageUrlOrHost, modelIdHint) {
    const hint = Number(modelIdHint);
    if (Number.isFinite(hint) && hint > 0) return hint;
    const fromUrl = modelIdFromPageUrl(pageUrlOrHost);
    if (Number.isFinite(fromUrl) && fromUrl > 0) return fromUrl;
    const info = await getModelVersionInfo(versionId, pageUrlOrHost);
    const fromApi = Number(info && info.modelId);
    return (Number.isFinite(fromApi) && fromApi > 0) ? fromApi : NaN;
}

// 评测行：ResourceReview 的真实字段是 {id, modelId, modelVersionId, rating, recommended, details,
// createdAt, nsfw, exclude, metadata, commentCount, imageCount, quality, user:{id, username, …}}。
// 旧代码读的 r.recommend / r.content 都不存在（8)g. 同源），所以推荐与否和正文都拿不到。
function reviewLine(r) {
    const user = commentUserLabel(r);
    const bits = [];
    if (r && r.rating != null && r.rating !== '') bits.push('评分:' + r.rating);
    if (r && typeof r.recommended === 'boolean') bits.push('推荐:' + (r.recommended ? '是' : '否'));
    else if (r && typeof r.recommend === 'boolean') bits.push('推荐:' + (r.recommend ? '是' : '否'));
    const detail = commentPlainText(r && (r.details != null ? r.details : r.content));
    const tail = detail ? detail : '(只打了分，没有写文字评价)';
    return `【${user}】${bits.length ? ' ' + bits.join(' ') : ''} ${(r && r.createdAt) || ''}\n${tail}\n`;
}

// 带超时的 fetch 封装（默认 15s）：返回 Response；失败（超时/网络错）直接抛错，由调用方处理
// 注意：这里**不**默认加 credentials —— 媒体下载（封面/配图/模型文件，见 offscreen 的
// fetchArrayBuffer）也复用本函数，把 CDN 请求变成带 Cookie 的请求没有收益只有风险。
// 「带登录态的直连」由下面的 directFetcher（requestData 的兜底通道）显式加 credentials:'include'。
async function fetchJson(url, options) {
    const opts = options || {};
    const timeoutMs = (typeof opts.timeoutMs === 'number' && opts.timeoutMs > 0) ? opts.timeoutMs : 15000;
    const ctrl = new AbortController();
    const timer = setTimeout(() => { try { ctrl.abort(); } catch (_) {} }, timeoutMs);
    let onOuterAbort = null;
    try {
        if (opts.signal) {
            if (opts.signal.aborted) { try { ctrl.abort(); } catch (_) {} }
            else {
                onOuterAbort = () => { try { ctrl.abort(); } catch (_) {} };
                try { opts.signal.addEventListener('abort', onOuterAbort, { once: true }); } catch (_) {}
            }
        }
        const { timeoutMs: _ignored, ...fetchOpts } = opts;
        return await fetch(url, { ...fetchOpts, signal: ctrl.signal });
    } finally {
        clearTimeout(timer);
        try { if (opts.signal && onOuterAbort) opts.signal.removeEventListener('abort', onOuterAbort); } catch (_) {}
    }
}

// ---- tRPC 401 熔断（按 procedure + transport 粒度）----
// 任何一次 401 之后，本会话内不再用**同一条通道**调同一个 procedure，直接走兜底/空结果。
// 要点：
//   · per-procedure：resourceReview.getPaged 熔断不影响 commentv2.getInfinite；
//   · per-transport：匿名直连(direct)的 401 只判死 direct；页面(page)的 401 只判死 page。
//     两者是不同的认证上下文——「匿名被拒」不是「带登录态也被拒」的证据，把一路的 401
//     记到另一路上就会 permanently 关掉本来能拿到数据的通道（正是本次修的 bug）；
//   · 只认 HTTP 401：404/500/解析错误/超时/网络错一律不进熔断（否则偶发故障会被升级成
//     「永久放弃」，而评论接口其实只是临时抖动）；页面通道「无标签页/不应答」这类可用性问题
//     由调用方（offscreen）自己按任务短路，不写进这张表；
//   · 模块级 Map，扩展重载 / SW 与 offscreen 重新 import 即自动清空 → 重启后自然再试一次；
//   · 粒度不拆 host：civitai.com 与 civitai.red 是同一套后端（匿名都返回 401），
//     按 host 再拆一层只会让熔断表虚胖、多打一轮注定失败的探测。
const COMMENTS_PROCEDURE = 'commentv2.getInfinite';
const REVIEWS_PROCEDURE = 'resourceReview.getPaged';
// 评价主通道：按 **modelId** 聚合、只返回写了字的评测（服务端固定 details IS NOT NULL）、
// cursor 数字翻页；实测匿名 200 有真实正文，而 getPaged 只给版本级且多为 details:null（文件头 8)c./d.）
const REVIEWS_INFINITE_PROCEDURE = 'resourceReview.getInfinite';
// 评论/评价单次请求预算：必须明显小于 offscreen 的请求-答复等待（PT_REPLY 侧钳到同一个值），
// 否则页面通道还没回话调用方就先判「无应答」，迟到的 pageFetchResult 被丢弃（文件头 8)i.）
const TRPC_REQUEST_TIMEOUT_MS = 12000;
const TRANSPORT_PAGE = 'page';     // content script 在页面里代发（同源 + 登录 Cookie）
const TRANSPORT_DIRECT = 'direct'; // 扩展自身直连（有 host_permissions，带 Cookie 但仍是跨站源）
const _trpcUnauthorized = new Map(); // 'procedure|transport' -> {procedure, transport, at, httpStatus}

function normalizeTransport(t) {
    return String(t || '') === TRANSPORT_PAGE ? TRANSPORT_PAGE : TRANSPORT_DIRECT;
}
function gateKey(procedureName, transport) {
    return String(procedureName) + '|' + normalizeTransport(transport);
}
function trpcUnauthorizedMark(procedureName, resp, transport) {
    const t = normalizeTransport(transport);
    _trpcUnauthorized.set(gateKey(procedureName, t), {
        procedure: String(procedureName),
        transport: t,
        at: Date.now(),
        httpStatus: Number(resp && resp.status) || 401,
    });
    return true;
}
// 传 transport = 只查这一路；省略 transport = 「任一路已被判死」（兼容旧的调用写法）
function trpcUnauthorizedTest(procedureName, transport) {
    const p = String(procedureName);
    if (transport) return _trpcUnauthorized.has(gateKey(p, transport));
    return _trpcUnauthorized.has(gateKey(p, TRANSPORT_PAGE)) || _trpcUnauthorized.has(gateKey(p, TRANSPORT_DIRECT));
}
// 某 procedure 两路通道的熔断明细（文案据此说话，别猜）
function trpcGateSnapshot(procedureName) {
    return {
        page: _trpcUnauthorized.get(gateKey(procedureName, TRANSPORT_PAGE)) || null,
        direct: _trpcUnauthorized.get(gateKey(procedureName, TRANSPORT_DIRECT)) || null,
    };
}
// 还能不能发：装了页面 fetcher 时要求两路都判死才算打光；没装时只看直连那一路
function trpcTransportsExhausted(procedureName) {
    const p = String(procedureName);
    const directGated = trpcUnauthorizedTest(p, TRANSPORT_DIRECT);
    if (typeof _pageFetcher !== 'function') return directGated;
    return directGated && trpcUnauthorizedTest(p, TRANSPORT_PAGE);
}
// 只读快照（自检/调试用；返回拷贝，外部改不动内部状态）
// key 现在是 'procedure|transport'，值里带 procedure / transport 字段，消费方读字段别解析 key
function trpcUnauthorizedState() {
    const out = {};
    _trpcUnauthorized.forEach((v, k) => { out[k] = Object.assign({}, v, { key: k }); });
    return out;
}
function trpcUnauthorizedReset() {
    const before = trpcUnauthorizedState();
    _trpcUnauthorized.clear();
    return before;
}

// ---- 可插拔传输层：所有 Civitai 数据请求的唯一出口 ----
// 页面通道 fetcher 由调用方注入（offscreen 归档任务安装、任务结束还原）：
//   fn(url, opts) → {ok, status, text}｜可抛错；opts = {headers, timeoutMs, pageUrlOrHost, signal, transport}
// setFetcher(null) 还原默认（只剩直连）。返回值是上一次 fetcher，方便调用方成对还原。
let _pageFetcher = null;
function setFetcher(fn) {
    const prev = _pageFetcher;
    _pageFetcher = (typeof fn === 'function') ? fn : null;
    return prev;
}
function getFetcher() { return _pageFetcher; }

// pageError 文案统一加一次「页面通道」前缀：fetcher 抛的短语（"不可用：…"、"已短路：…"、
// "无应答（…）"）拼进「页面通道这次没能完成请求（X）」这类句子时不会叠成"页面通道页面通道…"
function pageErrText(msg) {
    const s = String(msg || '').trim();
    if (!s) return '页面通道未完成请求';
    const body = s.replace(/^页面通道[：:；;]?/, '');
    // 我们的短语是"续句"（不可用/已短路/无应答/返回…/请求…/已被…）直接接在前缀后，
    // content 侧原样转来的错误（如 "url 不在白名单"）用冒号隔开，别拼成病句
    return /^(不可用|已短路|无应答|返回|请求|已被|未安装)/.test(body) ? '页面通道' + body : '页面通道：' + body;
}

// fetcher 返回值宽松归一：{ok, status, text/body}，字段缺失按失败处理（绝不当成"空结果成功"）
function normalizeFetcherResult(r) {
    if (!r || typeof r !== 'object') return { ok: false, status: 0, text: '', err: '返回结构异常', truncated: false };
    const text = typeof r.text === 'string' ? r.text : (typeof r.body === 'string' ? r.body : '');
    const status = Number(r.status) || 0;
    return {
        ok: r.ok === true,
        status,
        text,
        err: r.err ? String(r.err) : '',
        truncated: r.truncated === true,
    };
}

// 兜底通道：扩展直连 + credentials:'include'（见文件头 7)b.）
// 带 Cookie 的跨源请求有一个硬坑：若站点响应头是 Access-Control-Allow-Origin: *（公开 API 实测就是），
// 浏览器规则「通配 ACAO 不允许携带凭据」会让 fetch 直接 reject，反而把改造前能用的匿名直连也弄坏。
// 所以这里在**非超时**的失败（CORS 拦截表现为 TypeError）上自动降级重试一次 credentials:'omit'，
// 保证这次改动最坏也只是"回到改动前的行为"，不会更差。真 Chrome 里若这条重试生效，说明直连
// 拿不到会话数据，那正是页面通道存在的理由。
async function directFetcher(url, opts) {
    const o = opts || {};
    const cred = o.credentials || 'include';
    const run = async (credentials) => {
        const resp = await fetchJson(url, { headers: o.headers, credentials, timeoutMs: o.timeoutMs, signal: o.signal });
        return { ok: !!resp.ok, status: Number(resp.status) || 0, text: await resp.text() };
    };
    try {
        return await run(cred);
    } catch (e) {
        if (cred !== 'include' || (e && e.name === 'AbortError')) throw e; // 超时/主动取消不重试（重试等于把超时翻倍）
        try { return await run('omit'); } catch (_) { throw e; }
    }
}

// requestData(url, opts) —— 唯一决定 transport 的地方。
// opts: {headers, timeoutMs, signal, pageUrlOrHost, procedure, transport, credentials, allowPage}
// 返回（永不抛错，调用方按字段判断）：
//   { ok, status, text, transport:'page'|'direct'|'', gated, pageError, directError, truncated }
// · 有页面 fetcher 且该 procedure 的 page 路未熔断 → 先发页面；页面「抛错 / ok:false /
//   被截断」→ 回落直连（文件头 7)b.）。
// · 401 一律按 (procedure, 实际用的那条通道) 记熔断；两路都判死时 gated:true，调用方立刻收手。
async function requestData(url, opts) {
    const o = opts || {};
    const procedure = o.procedure ? String(o.procedure) : '';
    const out = { ok: false, status: 0, text: '', transport: '', gated: false, pageError: '', directError: '', truncated: false, pageStatus: 0, directStatus: 0 };
    const pageAllowed = typeof _pageFetcher === 'function' && o.transport !== TRANSPORT_DIRECT
        && o.allowPage !== false
        && (!procedure || !trpcUnauthorizedTest(procedure, TRANSPORT_PAGE));
    const directAllowed = !procedure || !trpcUnauthorizedTest(procedure, TRANSPORT_DIRECT);
    if (pageAllowed) {
        try {
            const raw = await _pageFetcher(url, {
                headers: o.headers, timeoutMs: o.timeoutMs, pageUrlOrHost: o.pageUrlOrHost,
                signal: o.signal, transport: TRANSPORT_PAGE, procedure,
            });
            const r = normalizeFetcherResult(raw);
            if (r.status === 401 && procedure) trpcUnauthorizedMark(procedure, r, TRANSPORT_PAGE);
            if (r.ok && r.truncated !== true && typeof r.text === 'string') {
                out.ok = true; out.status = r.status || 200; out.text = r.text;
                out.transport = TRANSPORT_PAGE;
                return out;
            }
            out.truncated = r.truncated === true;
            out.pageError = pageErrText(r.err
                || (r.truncated ? '返回体超过页面上限被截断（JSON 不可用）'
                    : '返回 ok=false（status ' + r.status + '）'));
            out.pageStatus = r.status || 0;
        } catch (e) {
            const m = String((e && e.message) || e || '');
            // fetcher 自己抛的中文短语直接续在前缀后；异常对象/英文栈信息才加「请求失败」
            out.pageError = pageErrText(/^[\u4e00-\u9fa5]/.test(m) ? m : ('请求失败: ' + m));
        }
    } else if (typeof _pageFetcher === 'function' && procedure) {
        out.pageError = pageErrText('已被 401 熔断（' + procedure + '）');
    }
    if (!directAllowed) {
        out.gated = true;
        out.status = 401;
        out.directStatus = 401;
        out.directError = '直连通道已被 401 熔断（' + (procedure || '-') + '）';
        return out;
    }
    try {
        const r = await directFetcher(url, o);
        out.transport = TRANSPORT_DIRECT;
        out.status = r.status;
        out.directStatus = r.status;
        out.ok = r.ok === true;
        out.text = typeof r.text === 'string' ? r.text : '';
        if (r.status === 401 && procedure) trpcUnauthorizedMark(procedure, r, TRANSPORT_DIRECT);
    } catch (e) {
        out.transport = TRANSPORT_DIRECT;
        out.directError = '直连请求失败: ' + ((e && e.message) || String(e));
    }
    return out;
}

// 文本 → JSON（解析失败返回 null；调用方按"这次没数据"处理，语义与旧的 resp.json() 抛错一致）
function parseJsonSafe(text) {
    try { return JSON.parse(String(text)); } catch (_) { return null; }
}

// 一次调用的通道台账：文案要说"实际发生了什么"，就得先把它记下来
function newTransportTracker() {
    return { pageTried: 0, pageOk: 0, pageError: '', directTried: 0, dataTransport: '', stopReason: '', pageStatus: 0, directStatus: 0 };
}
function observeTransportOutcome(tx, res) {
    if (!tx || !res) return tx;
    if (res.pageError) { tx.pageTried += 1; tx.pageError = String(res.pageError); }
    if (res.transport === TRANSPORT_PAGE && res.ok) tx.pageOk += 1;
    if (res.transport === TRANSPORT_DIRECT) tx.directTried += 1;
    if (res.pageStatus) tx.pageStatus = res.pageStatus;
    if (res.directStatus) tx.directStatus = res.directStatus;
    if (res.ok && res.transport) tx.dataTransport = res.transport;
    return tx;
}

// 降级说明文案（写进 评论.txt / 批量 errors，务必说清真因，别让用户以为模型没评论）
// 这句是「匿名直连 + 页面登录态两条通道都拿到 401」时才成立的结论；只有一路 401 时
// 必须换用 trpcReasonText() 的分通道说法，否则就是把未验证的结论写给用户。
const TRPC_CLOSED_REASON = '评论/评价接口已被站点关闭（返回 401，官方要求改用公开 API，而公开 API 未提供评论/评价端点）';

// 按「哪一路拿到了 401、页面通道这次到底跑没跑」组织真因文案（文件头 7)c./7)d.）
function trpcReasonText(ctx) {
    const c = ctx || {};
    const dg = c.directGated === true;
    const pg = c.pageGated === true;
    if (dg && pg) return TRPC_CLOSED_REASON; // 两路同判 401：与原口径一致（这次是真的关了）
    if (pg && !dg) {
        return '评论/评价接口在带登录态的页面通道里返回 401（官方要求改用公开 API，而公开 API 未提供评论/评价端点）；'
            + '扩展直连那一路本次没有被判死，稍后仍会重试';
    }
    if (dg && !pg) {
        const why = c.pageTried
            ? '页面通道这次没能完成请求（' + (c.pageError || '页面未应答') + '），所以只拿到扩展的匿名结果'
            : (c.pageAvailable
                ? '页面通道这次没有被用于该请求（标签页与请求站点不匹配）'
                : '本次没有可用的页面通道（没有标签页 / 页面还没接入助手），无法借用你的登录状态重试');
        return '扩展匿名直连评论/评价接口时返回 401（官方要求改用公开 API，而公开 API 未提供评论/评价端点）；' + why
            + '。请注意：这只能证明匿名请求被拒，不能证明接口对登录用户也关闭';
    }
    return TRPC_CLOSED_REASON;
}
// 某 procedure 的真因（读熔断表 + 本次通道台账，调用方不用自己拼）
function trpcReasonFor(procedureName, tx) {
    const t = tx || {};
    return trpcReasonText({
        directGated: trpcUnauthorizedTest(procedureName, TRANSPORT_DIRECT),
        pageGated: trpcUnauthorizedTest(procedureName, TRANSPORT_PAGE),
        pageTried: (Number(t.pageTried) || 0) > 0,
        pageError: t.pageError || '',
        pageAvailable: typeof _pageFetcher === 'function',
    });
}
// 数据确实拿到了：只交代它从哪条通道来的，绝不写"接口已关闭"
// 文案里 '[通道] 数据来自X通道' 是机器可读标记（archive-worker 用 textDataChannel 反读产物，
// 从而保证「文件里写的通道」和「errors 里说的通道」是同一个来源，不会各说各话）
function dataChannelNote(transport) {
    if (transport === TRANSPORT_PAGE) {
        return '[通道] 数据来自页面通道（在模型页里同源发起，带你的登录状态）。\n';
    }
    if (transport === TRANSPORT_DIRECT) {
        return '[通道] 数据来自扩展直连通道（chrome-extension 源；已带 Cookie，但站点仍可能按匿名请求处理）。\n';
    }
    return '';
}
// 从已生成的文本里读出数据通道：'page' | 'direct' | ''（没写=没走接口，DOM 抓取或空）
function textDataChannel(text) {
    const s = String(text || '');
    if (s.indexOf('[通道] 数据来自页面通道') >= 0) return TRANSPORT_PAGE;
    if (s.indexOf('[通道] 数据来自扩展直连通道') >= 0) return TRANSPORT_DIRECT;
    return '';
}

function commentsHeader(modelId) {
    return `===== 评论 Comments（模型ID: ${modelId}，抓取时间: ${new Date().toLocaleString()}）=====\n`;
}
function reviewsHeader(modelVersionId, modelId, scope) {
    // 标题必须说清**实际取数口径**：主通道 getInfinite 按 modelId 聚合整个模型的评价（跨版本），
    // 只有兜底的 getPaged 才按版本过滤（lib 文件头 8)c./d.）。写成「按版本过滤」会把
    // 模型级结果谎报成版本级，用户会以为其它版本的评价丢了。
    const hasModel = Number(modelId) > 0;
    const label = scope === 'version' ? '（按版本过滤）' : (hasModel ? '（按模型聚合，含其它版本）' : '（按版本过滤）');
    const scope2 = hasModel ? `版本ID: ${modelVersionId}，模型ID: ${modelId}${label}` : `版本ID: ${modelVersionId}（按版本过滤）`;
    return `===== 评价 Reviews（${scope2}，抓取时间: ${new Date().toLocaleString()}）=====\n`;
}

// hasDom: 'none' = 什么都没拿到；'dom' = 只有页面抓取的评论；'partial' = 有数据但翻页被打断
// reason 由 trpcReasonFor() 给出（说清是哪条通道被拒、页面通道为什么没起作用）
function commentsDeadNote(modelId, hasDom, reason) {
    const why = reason || TRPC_CLOSED_REASON;
    if (hasDom === 'dom') {
        return `[说明] ${why}，本次保存的评论全部来自页面上已渲染的内容（每条带 [页面抓取] 标记）；未加载出来的历史评论无法保存。模型ID: ${modelId}。\n`;
    }
    if (hasDom === 'partial') {
        return `[说明] ${why}，评论翻页在 401 处中断，以下结果可能不完整。模型ID: ${modelId}。\n`;
    }
    return `[说明] ${why}，本次仅能保存页面上已渲染的评论，而页面上没有已渲染的评论（评论区未展开或未加载），所以本文件没有任何评论。\n`
        + `[建议] 归档前先在模型页展开评论区并等待加载完成，再重新归档；走页面抓取通道时每条会带 [页面抓取] 标记。模型ID: ${modelId}。\n`;
}

function reviewsDeadNote(modelVersionId, hasData, reason) {
    const why = reason || TRPC_CLOSED_REASON;
    if (hasData) {
        return `[说明] ${why}，评价翻页在 401 处中断，以下结果可能不完整。版本ID: ${modelVersionId}。\n`;
    }
    return `[说明] ${why}；评价也没有页面抓取通道（页面抓取只覆盖评论区），本次未能保存任何评价。版本ID: ${modelVersionId}。\n`;
}

// 「HTTP 200 + 空列表」不是结论，是**一次带通道的观测**（文件头 9)b.）：页面通道（同源带 Cookie）
// 的空与匿名直连的空含义完全不同，所以两套文案必须分开写，且都要交代当次实际走的通道。
function commentsEmptyNote(transport) {
    if (transport === TRANSPORT_PAGE) {
        return '[说明] 已在模型页里同源调用 commentv2.getInfinite（带你的登录状态），站点返回的 comments 是空列表：'
            + '这个模型目前没有可保存的评论。\n';
    }
    if (transport === TRANSPORT_DIRECT) {
        return '[说明] 本次走的是扩展匿名直连，commentv2.getInfinite 返回 HTTP 200 但 comments 是空列表。'
            + '实测该接口对未登录请求一律返回空 comments（有上万条评价的模型也一样），所以这不是「这个模型没有评论」。'
            + '[建议] 在 C 站模型页里发起归档以借用你的登录状态；页面已渲染的评论会由页面抓取通道兜底（每条带 [页面抓取] 标记）。\n';
    }
    return '[说明] 本次没有可用通道发出 commentv2.getInfinite 请求，未保存任何评论。\n';
}

function reviewsEmptyNote(transport) {
    if (transport === TRANSPORT_PAGE) {
        return '[说明] 已在模型页里同源调用 resourceReview.getPaged（modelId + modelVersionId + page），'
            + '站点返回 items 空列表且 totalItems=0：这个版本没有评价。\n';
    }
    if (transport === TRANSPORT_DIRECT) {
        return '[说明] 本次走的是扩展匿名直连，resourceReview.getPaged 返回 HTTP 200 但 items 空列表（totalItems=0）。'
            + '实测这个接口对匿名请求同样会返回真实评价（模型 4201/133005/43331 都拿到 totalItems>0），'
            + '所以这里的 0 条更接近「这个版本确实没人评价」；但如果模型页上明明挂着评价，请在 C 站页面里发起归档（带登录状态）后重试。\n';
    }
    return '[说明] 本次没有可用通道发出 resourceReview.getPaged 请求，未保存任何评价。\n';
}

// 站点明确回答 json:null：这是「没有评论区」这个事实，不是抓取失败（文件头 9)c.）
function noCommentThreadNote(modelId, transport) {
    return '[说明] 站点返回 result.data.json = null，即服务端没有这个模型的 Thread 行：这个模型没有评论区。'
        + '这是接口的正常回答，不是抓取失败，也不是 401（本次通道：'
        + (transport === TRANSPORT_PAGE ? '页面登录态' : transport === TRANSPORT_DIRECT ? '扩展匿名直连' : '未知')
        + `）。模型ID: ${modelId}。\n`;
}

// 入参被 Zod 拒绝（HTTP 400）：说清「是契约变了」，别写成「没有数据」
function trpcInputRejectedNote(procedureName, detail) {
    return `[说明] ${procedureName} 的入参被站点拒绝（HTTP 400）：${detail || '（无明细）'}。`
        + '这说明接口契约又变了，不是这个模型没有内容；需要按当前 schema 重新核对入参。\n';
}

// 评论：commentv2.getInfinite 主通道（数字 cursor 翻页，最多 5 页）→ domFallbackFn 兜底；失败不抛错
// 通道：装了页面 fetcher 时每个请求先走页面（带登录态），页面不成再走直连（文件头 7)）。
// 熔断按 procedure+transport 记，所以「匿名直连 401」不会挡住后面的页面尝试，反之亦然。
// 入参（文件头 8)a./b./f. + 9)a.，全部 live 校过）——只留两组，各自测一件不同的事：
//   A 当前 schema 的正解：entityType 小写 'model'（'Model' = 400 invalid_value）、entityId 是数字
//     （不是 {modelId}）、第一页**整个省略 cursor**（cursor:null 会被 z.number().optional() 判
//     "expected number, received null"）、repliesDepth 必给（服务端 repliesDepth ? … : {threads:[]}，
//     不给就一条回复都拿不到）。authed 不在 schema 里，Zod 直接丢弃，已删。
//   B 去掉所有可选键的最小形状：只有 A 拿到 HTTP 400（入参被拒 = 契约又漂移）时才用，
//     用来在那种情况下至少保住主列表。A 拿到 200 + 空列表**不会**换参数重试——空是站点对这组
//     参数的真实回答，换键不会变多（旧代码 5 组 × 5 页 = 每模型最多 25 次注定重复的请求）。
// 评论条目 → 产物文本。行首必须是 '  ↳ 回复 【' / '[页面抓取] 【'：offscreen 的
// countCommentLines / countDomCommentLines 按这两个行首数条数（跨文件契约），只能往后加信息。
function formatCommentLines(results) {
    return (results || []).map(c => {
        const prefix = c.fromDom ? '[页面抓取] ' : (c.reply ? '  ↳ 回复 ' : '');
        const from = (c.reply && c.replyTo) ? ` 〔回复 @${c.replyTo}〕` : '';
        return `${prefix}【${c.user}】(${c.createdAt || '时间未知'})${from}\n${String(c.content).replace(/\s+$/g, '')}\n`;
    }).join('\n');
}

async function fetchCivitaiComments(modelId, domFallbackFn, pageUrlOrHost) {
    const results = [];
    const entityId = Number(modelId);
    const MAX_PAGES = 5;
    const inputCandidates = (Number.isFinite(entityId) && entityId > 0)
        ? [
            { entityType: 'model', entityId, sort: 'Newest', limit: 50, repliesDepth: 2, repliesLimit: 20 },
            { entityType: 'model', entityId, limit: 50 },
        ]
        : [];
    const tx = newTransportTracker();
    const seen = new Set(); // 翻页/兜底可能重叠，按评论 id 去重，绝不把同一条写两遍
    const st = { threadMissing: false, sawThread: false, pages: 0, hasMore: false, replyUnknown: new Set(), badInput: '' };
    const pushComment = (node, parent) => {
        if (!node || typeof node !== 'object') return;
        const text = commentPlainText(node.content);
        if (!text) return; // 只剩 <p></p> 的占位不配占一行
        const key = (node.id != null) ? 'c' + node.id : 'x' + commentUserLabel(node) + '|' + (node.createdAt || '') + '|' + text;
        if (seen.has(key)) return;
        seen.add(key);
        results.push({
            user: commentUserLabel(node), content: text, createdAt: node.createdAt || '',
            reply: !!parent,
            // 回复挂在**哪条父评论**下：站点把回复放在 replyThreads[].comments[]，父评论 id = thread.commentId。
            // 标记写成行尾的〔回复 @某人〕，行首仍是 '  ↳ 回复 【'——offscreen 的 countCommentLines
            // 按行首数条数，那个格式是跨文件契约，不能动。
            replyTo: parent ? String(parent.label || '') : '',
        });
    };
    const collectFromPage = (data, withReplies) => {
        const mains = trpcListFromData(data, 'comments'); // 8)e.：主列表是 data.comments，旧代码读 data.items
        const threads = asList(data && data.replyThreads);
        const idOf = (c) => (c && c.id != null ? String(c.id) : '');
        const labelOf = new Map();
        mains.forEach((c) => { if (idOf(c)) labelOf.set(idOf(c), commentUserLabel(c)); });
        for (const t of threads) {
            for (const c of repliesFromThread(t)) if (idOf(c) && !labelOf.has(idOf(c))) labelOf.set(idOf(c), commentUserLabel(c));
        }
        // 服务端按 (depth,id) + 预算挑线程，replyThreads 的数组顺序和页面阅读顺序无关。
        // 归档文本要能顺着读，所以按「父评论在本页 comments 里的位置」把线程挂回父评论下面。
        const byParent = new Map();
        threads.forEach((t, idx) => {
            const k = String(t && t.commentId);
            const g = byParent.get(k) || { idx, list: [] };
            g.list.push(t);
            byParent.set(k, g);
        });
        const emitThread = (t) => {
            const parentId = t && t.commentId;
            const label = labelOf.get(String(parentId)) || ('评论#' + (parentId == null ? '?' : parentId));
            for (const rep of repliesFromThread(t)) pushComment(rep, { label });
        };
        if (data && data.targetComment) pushComment(data.targetComment, null); // 深链锚点：排在本页最前
        for (const it of mains) {
            pushComment(it, null);
            const g = byParent.get(idOf(it));
            if (g) g.list.forEach(emitThread);
        }
        // 父评论不在本页的线程（更深层级、或父评论已被上一页翻走）挂不到父评论下面，按原顺序排到最后
        for (const t of threads) if (!mains.some((c) => idOf(c) === String(t && t.commentId))) emitThread(t);
        // childlessCommentIds 是服务端明确回答「这条没有回复」的名单；既没有线程又不在名单里
        // = 线程预算没排到它，回复状态未知，不能假装本页拿到了全量。
        // 但只有服务端**这一页真的挑过线程**（threads 或 childless 非空）时这个推断才成立：
        // 整页没有任何回复时 rows 为空 → completedDepth=0 → childless 也是空，
        // 那种「两个都空」是「没人回复」而不是「没查」，据此报缺口就是假警报。
        if (!withReplies) return;
        if (!threads.length && !asList(data && data.childlessCommentIds).length) return;
        const threadParents = new Set(threads.map((t) => String(t && t.commentId)));
        const childless = new Set(asList(data && data.childlessCommentIds).map(String));
        for (const c of mains) {
            if (idOf(c) && !threadParents.has(idOf(c)) && !childless.has(idOf(c))) st.replyUnknown.add(idOf(c));
        }
    };
    let gateTripped = trpcUnauthorizedTest(COMMENTS_PROCEDURE);
    if (!trpcTransportsExhausted(COMMENTS_PROCEDURE)) {
        for (const baseInput of inputCandidates) {
            if (trpcTransportsExhausted(COMMENTS_PROCEDURE)) { gateTripped = true; break; }
            const withReplies = baseInput.repliesDepth != null;
            let cursor = null;
            let rejectedInput = false;
            for (let page = 0; page < MAX_PAGES; page++) {
                // 只有拿到数字 cursor 才加 cursor 键：第一页带 cursor:null = 400（8)b.）
                const pageInput = (typeof cursor === 'number') ? Object.assign({}, baseInput, { cursor }) : baseInput;
                const res = await requestData(
                    buildTrpcUrl(COMMENTS_PROCEDURE, pageInput, pageUrlOrHost),
                    { pageUrlOrHost, procedure: COMMENTS_PROCEDURE, timeoutMs: TRPC_REQUEST_TIMEOUT_MS });
                observeTransportOutcome(tx, res);
                if (res.gated) { gateTripped = true; tx.stopReason = 'gated'; break; } // 两路都判死：别再发
                if (res.status === 401) { gateTripped = true; tx.stopReason = '401'; break; } // requestData 已按通道记好熔断
                const body = parseJsonSafe(res.text);
                const rejected = trpcInputRejected(res, body);
                if (rejected) { rejectedInput = true; st.badInput = rejected; tx.stopReason = 'bad-input'; break; }
                if (!res.ok) { tx.stopReason = 'http'; break; }
                if (body === null) { tx.stopReason = 'parse'; break; }
                // json:null ≠ 空列表：这是"该模型没有评论区"这个事实（9)c.），单独一条分支
                if (trpcJsonIsNull(body)) { st.threadMissing = true; tx.stopReason = 'no-thread'; break; }
                const data = extractTrpcData(body);
                if (!data || typeof data !== 'object') { tx.stopReason = 'shape'; break; }
                st.sawThread = true;
                st.pages += 1;
                collectFromPage(data, withReplies);
                const nextCursor = normalizeTrpcCursor(data.nextCursor);
                if (!nextCursor) { st.hasMore = false; tx.stopReason = 'eof'; break; }
                st.hasMore = true;
                cursor = nextCursor;
            }
            // 只有「入参被 400 拒了」才值得换最小形状再试一轮；拿到 200（哪怕 0 条）就是答案本身
            if (results.length || !rejectedInput) break;
        }
    }

    // DOM 兜底改为由调用方注入（content script 实现并回传同构数组；SW/offscreen 传空即可）
    if (!results.length && typeof domFallbackFn === 'function') {
        try {
            const fb = await domFallbackFn();
            if (Array.isArray(fb) && fb.length) {
                // content 侧给的是 textContent（已是纯文本），这里再过一遍 commentPlainText 只是防御
                // 版本不一致时混进 HTML；并补上去重，避免同一条被写两遍（seen 里已有接口条目）
                for (const item of fb) {
                    if (!item || typeof item !== 'object') continue;
                    const text = commentPlainText(item.content);
                    if (!text) continue;
                    const key = 'd' + String(item.user || '') + '|' + String(item.createdAt || '') + '|' + text;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    results.push(Object.assign({}, item, { content: text }));
                }
            }
        } catch (_) { /* 忽略 DOM 兜底错误 */ }
    }

    if (!results.length) {
        // 顺序即优先级：先说"没有评论区"（事实），再说 401（被拒），再说空列表（按通道区分含义），
        // 最后才是什么都没发生（如 entityId 非法 / 网络失败）——保持原行为：空串，不产生文件。
        if (st.threadMissing) {
            return commentsHeader(modelId) + dataChannelNote(tx.dataTransport) + noCommentThreadNote(modelId, tx.dataTransport);
        }
        if (gateTripped) {
            return commentsHeader(modelId) + commentsDeadNote(modelId, 'none', trpcReasonFor(COMMENTS_PROCEDURE, tx));
        }
        if (st.sawThread) {
            return commentsHeader(modelId) + dataChannelNote(tx.dataTransport) + commentsEmptyNote(tx.dataTransport)
                + (st.badInput ? trpcInputRejectedNote(COMMENTS_PROCEDURE, st.badInput) : '');
        }
        if (st.badInput) {
            return commentsHeader(modelId) + dataChannelNote(tx.dataTransport) + trpcInputRejectedNote(COMMENTS_PROCEDURE, st.badInput);
        }
        return '';
    }
    const domOnly = results.every((r) => r && r.fromDom);
    if (domOnly) {
        // 只有页面抓取的评论：接口那一路的结论照事实写（哪条通道被拒 / 页面通道为什么没成），
        // 然后**必须把抓到的评论本体写出来**——commentsDeadNote('dom') 明知道承诺了
        // 「本次保存的评论全部来自页面上已渲染的内容（每条带 [页面抓取] 标记）」，
        // 只写说明不写正文等于当着用户的面说谎（这条是本次实测中发现的第 6 个硬伤）。
        return commentsHeader(modelId)
            + (gateTripped ? commentsDeadNote(modelId, 'dom', trpcReasonFor(COMMENTS_PROCEDURE, tx)) : '')
            + dataChannelNote('')
            + '[说明] 以下评论来自页面已渲染内容（DOM 抓取），不含未加载出来的历史评论。\n'
            + formatCommentLines(results);
    }
    // 拿到了接口数据：绝不再写"接口已关闭"，只交代数据来自哪条通道；
    // 翻页确实被 401/熔断打断过才补一句"可能不完整"
    const cutShort = gateTripped && (tx.stopReason === '401' || tx.stopReason === 'gated');
    let limitNote = '';
    if (st.hasMore && !cutShort) {
        limitNote = `[说明] 评论翻页到本文件上限 ${MAX_PAGES} 页为止，站点仍给了 nextCursor（还有更多评论），以下结果不完整。模型ID: ${modelId}。\n`;
    }
    if (st.replyUnknown.size > 0) {
        limitNote += `[说明] 有 ${st.replyUnknown.size} 条顶层评论服务端既没返回回复线程、也没列入「无回复」名单（站点按预算挑线程），它们的回复可能没保存全。模型ID: ${modelId}。\n`;
    }
    return commentsHeader(modelId)
        + dataChannelNote(tx.dataTransport)
        + (cutShort ? commentsDeadNote(modelId, 'partial', trpcReasonFor(COMMENTS_PROCEDURE, tx)) : '')
        + limitNote
        + (st.badInput ? trpcInputRejectedNote(COMMENTS_PROCEDURE, st.badInput) : '')
        + formatCommentLines(results);
}

// 评价：resourceReview.getPaged，按 page 翻页，入参 = modelId + modelVersionId + page（文件头 9)a.）
// 通道与熔断口径同 fetchCivitaiComments。
// 【为什么这两项 ID 一个都不能少】8)c.：getPaged 的 Zod 只有 modelId 是必填、SQL 只用 modelVersionId
//   过滤，所以「只给 modelId」= 200 + totalItems:0 的空壳（会被读成"这个模型没人评价"），
//   「只给 modelVersionId」= 400。归档的对象是某个**版本**，所以两项同时给：modelId 过校验、
//   modelVersionId 定范围。
// 【为什么分页用 page 不用 cursor】paginationSchema = {limit, page}，没有 cursor 这个键；
//   多发一个 cursor 只会被 Zod 静默丢弃 —— 旧代码的 cursor 翻页因此永远停在第 1 页。
//   收口只看响应里的 currentPage/totalPages。
// 【getPaged 与 getInfinite 的分工】主通道是版本级 getPaged（全量条目，含只打分不写字的）；
//   resourceReview.getInfinite 只在版本级一条都没落地时才发：
//   · 入参 {modelId, limit, cursor?}，服务端固定 details IS NOT NULL → 只返回**写了字**的评价；
//     nextCursor 是数字（上一页末条 id）。实测匿名 200 就能拿到真实正文（模型 4384）。
//   · 它是另一个 procedure，不受 getPaged 的 401 熔断影响，所以能救回「getPaged 被判死/入参被拒」
//     的情况；但它按模型聚合且丢掉了纯打分条目，绝不能顶替版本级全量，只能当兜底。
//   · 已知代价：热门版本 5 页窗口（50×5）可能全被纯打分占满，写了字的评价排在更后面的页里进不了
//     文件——此时产物会写明「只保存了前 5 页 / totalItems=N」，不会假装全量。
// tx 由 fetchReviewsInfinite 共享，这样两个 procedure 的台账（实际拿到数据的通道 / 页面错误）合并成一份事实。
async function fetchReviewsInfinite(modelId, pageUrlOrHost, tx) {
    const out = { lines: [], cutShort: false, truncated: false, sawData: false, badInput: '', eof: false };
    const baseInput = { modelId: Number(modelId), limit: 50 };
    const MAX_PAGES = 5;
    let cursor = null;
    for (let pageNo = 1; pageNo <= MAX_PAGES; pageNo++) {
        const pageInput = (typeof cursor === 'number') ? Object.assign({}, baseInput, { cursor }) : baseInput;
        const res = await requestData(
            buildTrpcUrl(REVIEWS_INFINITE_PROCEDURE, pageInput, pageUrlOrHost),
            { pageUrlOrHost, procedure: REVIEWS_INFINITE_PROCEDURE, timeoutMs: TRPC_REQUEST_TIMEOUT_MS });
        observeTransportOutcome(tx, res);
        if (res.gated || res.status === 401) { out.cutShort = true; break; }
        if (!res.ok) break;
        const body = parseJsonSafe(res.text);
        if (body === null) break;
        const rejected = trpcInputRejected(res, body);
        if (rejected) { out.badInput = rejected; break; }
        const data = extractTrpcData(body);
        if (!data || typeof data !== 'object') break;
        out.sawData = true;
        const items = trpcListFromData(data, 'items');
        for (const r of items) out.lines.push(reviewLine(r));
        const nextCursor = normalizeTrpcCursor(data.nextCursor);
        if (!nextCursor) { out.eof = true; break; }
        cursor = nextCursor;
        if (pageNo === MAX_PAGES) out.truncated = true;
    }
    return out;
}

async function fetchCivitaiReviews(modelVersionId, pageUrlOrHost, modelIdHint) {
    const vid = Number(modelVersionId);
    const MAX_PAGES = 5;
    const tx = newTransportTracker();
    const st = { pages: 0, got: 0, totalItems: 0, totalPages: 0, eof: false, badInput: '', sawData: false };
    if (!(Number.isFinite(vid) && vid > 0)) return '';
    const mid = await resolveReviewModelId(vid, pageUrlOrHost, modelIdHint);
    if (!(Number.isFinite(mid) && mid > 0)) {
        // 缺 modelId 就必然 400，所以不发请求，直接把"为什么没发"写清楚
        return reviewsHeader(vid) + '[说明] 没能确定这个版本的 modelId，而 resourceReview.getPaged 把它当必填校验项'
            + `（缺失时 HTTP 400 "expected number, received undefined"），所以本次没有发起评价请求。版本ID: ${vid}。\n`;
    }
    const baseInput = { modelId: mid, modelVersionId: vid, limit: 50 };
    const allLines = [];
    const seen = new Set();
    let gateTripped = trpcUnauthorizedTest(REVIEWS_PROCEDURE);
    let cutShort = false;
    if (!trpcTransportsExhausted(REVIEWS_PROCEDURE)) {
        for (let pageNo = 1; pageNo <= MAX_PAGES; pageNo++) {
            const pageInput = Object.assign({}, baseInput, { page: pageNo });
            const res = await requestData(
                buildTrpcUrl(REVIEWS_PROCEDURE, pageInput, pageUrlOrHost),
                { pageUrlOrHost, procedure: REVIEWS_PROCEDURE, timeoutMs: TRPC_REQUEST_TIMEOUT_MS });
            observeTransportOutcome(tx, res);
            if (res.gated) { gateTripped = true; cutShort = true; tx.stopReason = 'gated'; break; }
            if (res.status === 401) { gateTripped = true; cutShort = true; tx.stopReason = '401'; break; }
            const body = parseJsonSafe(res.text);
            const rejected = trpcInputRejected(res, body);
            if (rejected) { st.badInput = rejected; tx.stopReason = 'bad-input'; break; }
            if (!res.ok) { tx.stopReason = 'http'; break; }
            if (body === null) { tx.stopReason = 'parse'; break; }
            const data = extractTrpcData(body);
            if (!data || typeof data !== 'object') { tx.stopReason = 'shape'; break; }
            st.sawData = true;
            st.pages += 1;
            st.totalItems = Number(data.totalItems) || 0;
            st.totalPages = Number(data.totalPages) || 0;
            for (const r of asList(data.items)) {
                const key = (r && r.id != null) ? String(r.id) : 'x' + allLines.length;
                if (seen.has(key)) continue; // 页与页会重叠：站点按 id 排序，期间有人新发评价就会移位
                seen.add(key);
                allLines.push(reviewLine(r));
            }
            // 收口只看响应事实：currentPage 到 totalPages 就是到底
            const currentPage = Number(data.currentPage) || pageNo;
            const items = asList(data.items);
            if (st.totalPages >= 1) {
                if (currentPage >= st.totalPages) { st.eof = true; tx.stopReason = 'eof'; break; }
            } else if (items.length < baseInput.limit) {
                st.eof = true; tx.stopReason = 'eof'; break; // 没有 totalPages 的老/异常形状：本页不满 limit 即到底
            }
        }
    } else {
        cutShort = true;
    }
    st.got = allLines.length;

    if (allLines.length) {
        // 数据是接口给的：说清通道；只有翻页真被 401 打断时才说"可能不完整"
        const truncated = !st.eof && !cutShort;
        return reviewsHeader(vid, mid, 'version')
            + dataChannelNote(tx.dataTransport)
            + (cutShort ? reviewsDeadNote(vid, true, trpcReasonFor(REVIEWS_PROCEDURE, tx)) : '')
            + (truncated ? `[说明] 评价翻页到本文件上限 ${MAX_PAGES} 页为止（站点 totalPages=${st.totalPages}、totalItems=${st.totalItems}），本次只保存 ${st.got} 条，其余未保存。版本ID: ${vid}。\n` : '')
            + (st.badInput ? trpcInputRejectedNote(REVIEWS_PROCEDURE, st.badInput) : '')
            + allLines.join('\n');
    }
    // 版本级一条都没落地时，才用模型级 getInfinite 兜底（它是**另一个 procedure**，
    // 不受 REVIEWS_PROCEDURE 那条熔断影响；且服务端固定 details IS NOT NULL，只回写了字的评价）。
    // 它只可能补回内容、不可能把版本级的全量打分换成残缺文本，所以放在 getPaged 之后而不是之前。
    if (!allLines.length && !trpcTransportsExhausted(REVIEWS_INFINITE_PROCEDURE)) {
        const inf = await fetchReviewsInfinite(mid, pageUrlOrHost, tx);
        if (inf.lines.length) {
            return reviewsHeader(vid, mid, 'model')
                + dataChannelNote(tx.dataTransport)
                + '[说明] 版本级 resourceReview.getPaged 没有返回条目，以下评价来自模型级 resourceReview.getInfinite'
                + '（跨版本，且站点固定只返回**写了文字**的评价）。\n'
                + (inf.cutShort ? reviewsDeadNote(vid, true, trpcReasonFor(REVIEWS_INFINITE_PROCEDURE, tx)) : '')
                + (inf.truncated ? `[说明] 评价翻页到本文件上限 ${MAX_PAGES} 页为止，站点仍给了 nextCursor（还有更多评价），本次只保存 ${inf.lines.length} 条。模型ID: ${mid}。\n` : '')
                + (inf.badInput ? trpcInputRejectedNote(REVIEWS_INFINITE_PROCEDURE, inf.badInput) : '')
                + inf.lines.join('\n');
        }
        if (inf.badInput) st.badInput = st.badInput || inf.badInput;
        if (inf.cutShort) { gateTripped = true; cutShort = true; }
    }
    // 与评论同一优先级：被 401 判死 ≠ 空列表；空列表的含义跟着当次通道走（文件头 9)b.）
    if (gateTripped) {
        return reviewsHeader(vid, mid, 'version') + reviewsDeadNote(vid, false, trpcReasonFor(REVIEWS_PROCEDURE, tx));
    }
    if (st.sawData) {
        return reviewsHeader(vid, mid, 'version') + dataChannelNote(tx.dataTransport) + reviewsEmptyNote(tx.dataTransport)
            + (st.badInput ? trpcInputRejectedNote(REVIEWS_PROCEDURE, st.badInput) : '');
    }
    if (st.badInput) {
        return reviewsHeader(vid, mid, 'version') + dataChannelNote(tx.dataTransport) + trpcInputRejectedNote(REVIEWS_PROCEDURE, st.badInput);
    }
    return '';
}

// 样图元数据：公开 API GET /api/v1/images?imageId=&withMeta=true&limit=1
// （旧 image.getGenerationData trpc 已 401；本调用与桌面端 civitai_api.py:103
//  get_image_metadata 完全同参，实测 200 / ACAO:* / 无需密钥）
//
// 【返回形状变了】不再返回 tRPC 信封 {result:{data:{json}}}，而是返回归一化后的 core：
//   有元数据：{ found:true,  id, type:'image'|'video', url, nsfwLevel,
//               meta:{prompt,negativePrompt,sampler,cfgScale,steps,Size,seed,...},
//               resources:[], civitaiResources:[{type,weight,modelVersionId}], modelVersionIds:[],
//               transport:'page'|'direct', pageError:'' }
//   无元数据：{ found:false, id, reason:'empty-items'|'no-meta'|'id-mismatch', type:null,
//               meta:null, resources:[], civitaiResources:[], modelVersionIds:[], nsfwLevel:null,
//               transport, pageError }
//   meta 与旧 core.meta 同构同层，调用方读 prompt/sampler/… 的写法不用变（type 也从
//   item.type 带上，旧信封里同样是 json.type）。
// 为什么要"显式归一化"：公开 API 的 items[0].meta 是 {id, meta:{…}} 双层包裹（实测
// imageId 145676249 / 12097475 / 64399178 全都如此），而不带 imageId 的列表查询返回的
// 是扁平 meta；直接当扁平用就会读出 prompt=undefined，把 example.json 静默写成空样本。
// HTTP/网络错误照旧抛（调用方按图跳过，语义与原 tRPC 版一致）。
//
// 通道：装了页面 fetcher 时这个请求会先在页面里发（带登录状态）。返回的 core 里带
// transport / pageError 两个字段，调用方（archive-worker 的 promptGapNote）必须把它写进
// 产物说明——「空 items」在匿名直连和带登录态两种上下文里的含义完全不同，别再只留一个 reason。
async function getImageGenerationData(imageId, pageUrlOrHost) {
    const id = parseInt(imageId, 10);
    if (!Number.isFinite(id) || id <= 0) throw new Error('无效的图片ID: ' + imageId);
    const empty = (reason) => ({
        found: false, id, reason, type: null, url: null, nsfwLevel: null,
        meta: null, resources: [], civitaiResources: [], modelVersionIds: [],
        transport: '', pageError: '',
    });
    const url = civitaiBaseUrl(pageUrlOrHost) + '/api/v1/images?imageId=' + encodeURIComponent(id) + '&withMeta=true&limit=1';
    const res = await requestData(url, { headers: { Accept: 'application/json' }, pageUrlOrHost });
    const channel = { transport: res.transport, pageError: res.pageError || '' };
    if (!res.ok) throw new Error(`HTTP ${res.status || 0}` + (res.directError ? '（' + res.directError + '）' : ''));
    const body = parseJsonSafe(res.text);
    if (!body || typeof body !== 'object') throw new Error('样图元数据返回不是合法 JSON');
    const items = (Array.isArray(body.items)) ? body.items : [];
    if (!items.length) return Object.assign(empty('empty-items'), channel); // 无登录态/无密钥时 NSFW 或受限图就是空 items（实测 145224191）
    // 与桌面端同款校验：只认 id 对得上的那条，避免把别的图的提示词写进产物
    const item = items.find((it) => it && String(it.id) === String(id)) || null;
    if (!item) return Object.assign(empty('id-mismatch'), channel);
    const flat = unwrapImageMeta(item.meta);
    const hasMeta = !!(flat && typeof flat === 'object' && Object.keys(flat).length > 0);
    const resources = Array.isArray(flat?.resources) ? flat.resources
        : (Array.isArray(item.resources) ? item.resources : []);
    const civitaiResources = Array.isArray(flat?.civitaiResources) ? flat.civitaiResources : [];
    const modelVersionIds = Array.isArray(item.modelVersionIds) ? item.modelVersionIds : [];
    const core = {
        found: hasMeta,
        id: item.id,
        type: item.type || null,
        url: item.url || null,
        nsfwLevel: item.nsfwLevel != null ? item.nsfwLevel : null,
        meta: hasMeta ? flat : null,
        resources,
        civitaiResources,
        modelVersionIds,
        transport: channel.transport,
        pageError: channel.pageError,
    };
    if (!hasMeta) core.reason = 'no-meta';
    return core;
}

// items[0].meta 双层包裹 → 扁平生成数据（见 getImageGenerationData 注释）
function unwrapImageMeta(rawMeta) {
    if (!rawMeta || typeof rawMeta !== 'object' || Array.isArray(rawMeta)) return null;
    const inner = rawMeta.meta;
    if (inner && typeof inner === 'object' && !Array.isArray(inner)) return inner;
    if (typeof inner === 'string') {
        try {
            const parsed = JSON.parse(inner);
            if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed;
        } catch (_) { /* 不是 JSON 就按扁平 meta 处理 */ }
    }
    return rawMeta;
}

// 从归一化 core 里取版本ID/模型ID：公开 API 的 meta.resources 只有 {name,type,weight,hash}
// （不含 modelId/versionId），模型关联在 civitaiResources[].modelVersionId 与 item.modelVersionIds。
// 旧 tRPC 的 resources[] 带 modelId/versionId，这里按「先 version 后 model」的顺序尽力还原，
// 取不到就返回 null（绝不编造 ID）。
function imageGenerationRefs(core) {
    const out = { versionId: null, modelId: null };
    if (!core || typeof core !== 'object') return out;
    const resList = Array.isArray(core.resources) ? core.resources : [];
    const civList = Array.isArray(core.civitaiResources) ? core.civitaiResources : [];
    for (const r of resList) {
        if (!r || typeof r !== 'object') continue;
        if (out.versionId == null) out.versionId = r.versionId ?? r.modelVersionId ?? r.id ?? null;
        if (out.modelId == null) out.modelId = r.modelId ?? null;
    }
    for (const r of civList) {
        if (!r || typeof r !== 'object') continue;
        if (out.versionId == null) out.versionId = r.versionId ?? r.modelVersionId ?? null;
        if (out.modelId == null) out.modelId = r.modelId ?? null;
    }
    if (out.versionId == null && Array.isArray(core.modelVersionIds) && core.modelVersionIds.length) {
        out.versionId = core.modelVersionIds[0];
    }
    return out;
}

// 版本 → 模型ID：GET /api/v1/model-versions/{id}（公开、无需密钥，实测返回 modelId）
// 只在单图归档里用（一次用户动作一次请求）；批量/样图循环绝不碰，免得白打请求。
// 失败静默返回 null：单图流本来就允许"只存原图不存 JSON"。
// 返回体上挂 __transport/__pageError（不改动原字段），方便单图产物如实交代数据是从哪条通道拿的。
async function getModelVersionInfo(versionId, pageUrlOrHost) {
    const id = parseInt(versionId, 10);
    if (!Number.isFinite(id) || id <= 0) return null;
    const res = await requestData(
        civitaiBaseUrl(pageUrlOrHost) + '/api/v1/model-versions/' + encodeURIComponent(id),
        { headers: { Accept: 'application/json' }, pageUrlOrHost });
    if (!res.ok) return null;
    const data = parseJsonSafe(res.text);
    if (!data || typeof data !== 'object') return null;
    try {
        Object.defineProperty(data, '__transport', { value: res.transport, enumerable: false, configurable: true });
        Object.defineProperty(data, '__pageError', { value: res.pageError || '', enumerable: false, configurable: true });
    } catch (_) { /* 冻结的返回体就不挂了，不影响取 modelId */ }
    return data;
}

globalThis.CFT.api = {
    civitaiHostFrom,
    civitaiBaseUrl,
    buildTrpcUrl,
    extractTrpcData,
    commentUserLabel,
    fetchJson,
    // 传输层（文件头 7)）：页面通道可插拔，requestData 是唯一决定 transport 的出口
    TRANSPORT_PAGE,
    TRANSPORT_DIRECT,
    setFetcher,
    getFetcher,
    directFetcher,
    requestData,
    parseJsonSafe,
    COMMENTS_PROCEDURE,
    REVIEWS_PROCEDURE,
    REVIEWS_INFINITE_PROCEDURE,
    TRPC_REQUEST_TIMEOUT_MS,
    commentPlainText,
    asList,
    trpcListFromData,
    repliesFromThread,
    normalizeTrpcCursor,
    // trpc 契约细节（入参校验/空结果含义）——测试与自检都要能问 lib 而不是靠猜文案
    trpcJsonIsNull,
    trpcInputRejected,
    modelIdFromPageUrl,
    resolveReviewModelId,
    commentsEmptyNote,
    reviewsEmptyNote,
    noCommentThreadNote,
    trpcInputRejectedNote,
    reviewLine,
    formatCommentLines,
    trpcUnauthorizedMark,
    trpcUnauthorizedTest,
    trpcGateSnapshot,
    trpcTransportsExhausted,
    trpcUnauthorizedState,
    trpcUnauthorizedReset,
    TRPC_CLOSED_REASON,
    trpcReasonText,
    trpcReasonFor,
    dataChannelNote,
    textDataChannel,
    newTransportTracker,
    observeTransportOutcome,
    fetchCivitaiComments,
    fetchCivitaiReviews,
    getImageGenerationData,
    unwrapImageMeta,
    imageGenerationRefs,
    getModelVersionInfo,
};
