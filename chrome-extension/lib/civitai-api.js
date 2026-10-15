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
//   5) [2026-10-15 站点变更] 内部 trpc 对**匿名请求**硬关闭：commentv2.getInfinite /
//      resourceReview.getPaged / image.getGenerationData 一律 HTTP 401 +
//      {"message":"Please use the public API instead: https://developer.civitai.com/"}
//      （civitai.com 与 civitai.red 实测均 401）。【更正】这组实测全部是匿名 curl/扩展直连
//      的结果，只能证明「不带登录态被拒」，不能证明接口对登录用户也关闭——见下面 7)。
//      据此（结论仍按匿名口径保留）：
//        a. 样图生成数据改走公开 API GET /api/v1/images?imageId=&withMeta=true&limit=1
//           （与桌面端 civitai_api.py:103 get_image_metadata 同一调用，实测 200 +
//           access-control-allow-origin:*，无需密钥）；getImageGenerationData 因此不再
//           返回 tRPC 信封 {result:{data:{json}}}，改为返回归一化 core（见函数注释）。
//        b. 评论/评价没有公开替代端点（/api/v1/models/{id}/comments、/api/v1/comments、
//           /api/v1/model-versions/{id}/reviews 实测全部 404 HTML），只能降级到 DOM 抓取；
//           同时加「按 procedure 粒度」的 401 熔断，避免 5 组候选入参 × 5 页 = 每个模型
//           最多 25 次注定失败的请求（110 页批量会打出上千次，限流风险 + 拖慢归档）。
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
// 约束：顶层不触碰 document/window/chrome（civitaiBaseUrl 只在函数体内读 window）。
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
        return (node?.user?.username || node?.user?.name || '用户#' + (node?.userId ?? '?')) + '';
    } catch (_) {
        return '匿名';
    }
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
function reviewsHeader(modelVersionId) {
    return `===== 评价 Reviews（版本ID: ${modelVersionId}，抓取时间: ${new Date().toLocaleString()}）=====\n`;
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

// 评论：trpc 主通道（多组参数容错 + cursor 翻页拿全量，最多 5 页）→ domFallbackFn 兜底；失败不抛错
// 通道：装了页面 fetcher 时每个请求先走页面（带登录态），页面不成再走直连（文件头 7)）。
// 熔断按 procedure+transport 记，所以「匿名直连 401」不会挡住后面的页面尝试，反之亦然。
async function fetchCivitaiComments(modelId, domFallbackFn, pageUrlOrHost) {
    const results = [];
    const inputCandidates = [
        { entityType: 'Model', entityId: Number(modelId), sort: 'Newest', cursor: null, limit: 50, authed: true },
        { entityType: 'Model', entityId: Number(modelId), sort: 'Newest', cursor: null, limit: 50 },
        { modelId: Number(modelId), sort: 'Newest', cursor: null, limit: 50 },
        { modelId: Number(modelId), cursor: null, limit: 50, authed: true },
        { modelId: Number(modelId), limit: 50 },
    ];
    const MAX_PAGES = 5;
    const tx = newTransportTracker();
    let gateTripped = trpcUnauthorizedTest(COMMENTS_PROCEDURE);
    if (!trpcTransportsExhausted(COMMENTS_PROCEDURE)) {
        for (const baseInput of inputCandidates) {
            if (trpcTransportsExhausted(COMMENTS_PROCEDURE)) { gateTripped = true; break; }
            let cursor = null;
            for (let page = 0; page < MAX_PAGES; page++) {
                const pageInput = cursor ? Object.assign({}, baseInput, { cursor }) : baseInput;
                const res = await requestData(
                    buildTrpcUrl(COMMENTS_PROCEDURE, pageInput, pageUrlOrHost),
                    { pageUrlOrHost, procedure: COMMENTS_PROCEDURE });
                observeTransportOutcome(tx, res);
                if (res.gated) { gateTripped = true; tx.stopReason = 'gated'; break; } // 两路都判死：别再发
                if (res.status === 401) { gateTripped = true; tx.stopReason = '401'; break; } // requestData 已按通道记好熔断
                if (!res.ok) { tx.stopReason = 'http'; break; /* 换下一组参数重试 */ }
                const body = parseJsonSafe(res.text);
                if (body === null) { tx.stopReason = 'parse'; break; }
                const data = extractTrpcData(body);
                const items = (Array.isArray(data) ? data : (data?.items || []));
                if (!items.length) { tx.stopReason = 'empty'; break; }
                for (const it of items) {
                    if (typeof it?.content === 'string') {
                        results.push({ user: commentUserLabel(it), content: it.content, createdAt: it.createdAt || '', reply: false });
                    }
                    if (Array.isArray(it?.children)) {
                        it.children.forEach(c => {
                            if (typeof c?.content === 'string') {
                                results.push({ user: commentUserLabel(c), content: c.content, createdAt: c.createdAt || '', reply: true });
                            }
                        });
                    }
                }
                const nextCursor = (!Array.isArray(data) && data?.nextCursor) ? data.nextCursor : null;
                if (!nextCursor) { tx.stopReason = 'eof'; break; }
                cursor = nextCursor;
            }
            if (results.length) break;
        }
    }

    // DOM 兜底改为由调用方注入（content script 实现并回传同构数组；SW/offscreen 传空即可）
    if (!results.length && typeof domFallbackFn === 'function') {
        try {
            const fb = await domFallbackFn();
            if (Array.isArray(fb) && fb.length) results.push(...fb);
        } catch (_) { /* 忽略 DOM 兜底错误 */ }
    }

    if (!results.length) {
        // 没被熔断却仍空 = 真没有评论（保持原行为：空串，不产生文件、也不写"接口关闭"这种假原因）
        // 被熔断却空 = 请求被拒，必须把真因写进文件（含"先展开评论区"的提示）
        return gateTripped
            ? commentsHeader(modelId) + commentsDeadNote(modelId, 'none', trpcReasonFor(COMMENTS_PROCEDURE, tx))
            : '';
    }
    const domOnly = results.every((r) => r && r.fromDom);
    if (domOnly) {
        // 只有页面抓取的评论：接口那一路的结论照事实写（哪条通道被拒 / 页面通道为什么没成）
        return commentsHeader(modelId)
            + (gateTripped ? commentsDeadNote(modelId, 'dom', trpcReasonFor(COMMENTS_PROCEDURE, tx)) : '');
    }
    // 拿到了接口数据：绝不再写"接口已关闭"，只交代数据来自哪条通道；
    // 翻页确实被 401/熔断打断过才补一句"可能不完整"
    const cutShort = gateTripped && (tx.stopReason === '401' || tx.stopReason === 'gated');
    return commentsHeader(modelId)
        + dataChannelNote(tx.dataTransport)
        + (cutShort ? commentsDeadNote(modelId, 'partial', trpcReasonFor(COMMENTS_PROCEDURE, tx)) : '')
        + results.map(c => {
            const prefix = c.fromDom ? '[页面抓取] ' : (c.reply ? '  ↳ 回复 ' : '');
            return `${prefix}【${c.user}】(${c.createdAt || '时间未知'})\n${String(c.content).replace(/\s+$/g, '')}\n`;
        }).join('\n');
}

// 评价：trpc resourceReview.getPaged，多组参数容错；通道与熔断口径同 fetchCivitaiComments
async function fetchCivitaiReviews(modelVersionId, pageUrlOrHost) {
    const inputCandidates = [
        { modelVersionId: Number(modelVersionId), cursor: null, limit: 50 },
        { modelVersionId: Number(modelVersionId), limit: 50 },
        { id: Number(modelVersionId), cursor: null, limit: 50 },
    ];
    const MAX_PAGES = 5;
    const tx = newTransportTracker();
    let gateTripped = trpcUnauthorizedTest(REVIEWS_PROCEDURE);
    if (!trpcTransportsExhausted(REVIEWS_PROCEDURE)) {
        for (const baseInput of inputCandidates) {
            if (trpcTransportsExhausted(REVIEWS_PROCEDURE)) { gateTripped = true; break; }
            const allLines = [];
            let cursor = null;
            let cutShort = false;
            for (let page = 0; page < MAX_PAGES; page++) {
                const pageInput = cursor ? Object.assign({}, baseInput, { cursor }) : baseInput;
                const res = await requestData(
                    buildTrpcUrl(REVIEWS_PROCEDURE, pageInput, pageUrlOrHost),
                    { pageUrlOrHost, procedure: REVIEWS_PROCEDURE });
                observeTransportOutcome(tx, res);
                if (res.gated || res.status === 401) { gateTripped = true; cutShort = true; break; }
                if (!res.ok) break;
                const body = parseJsonSafe(res.text);
                if (body === null) break;
                const data = extractTrpcData(body);
                const items = (Array.isArray(data) ? data : (data?.items || []));
                if (!items.length) break;
                const lines = items.map(r => {
                    const user = (r?.user?.username || r?.user?.name || '用户#' + (r?.userId ?? '?'));
                    let rating = '';
                    if (r?.rating != null) rating = `评分:${r.rating}`;
                    else if (r?.recommend !== undefined) rating = `推荐:${r.recommend ? '是' : '否'}`;
                    const detail = String(r?.details || r?.content || '').trim();
                    return `【${user}】${rating ? ' ' + rating : ''} ${r?.createdAt || ''}\n${detail}\n`;
                });
                allLines.push(...lines);
                const nextCursor = (!Array.isArray(data) && data?.nextCursor) ? data.nextCursor : null;
                if (!nextCursor) break;
                cursor = nextCursor;
            }
            if (allLines.length) {
                // 数据是接口给的：说清通道，只有翻页真被 401 打断时才说"可能不完整"
                return reviewsHeader(modelVersionId)
                    + dataChannelNote(tx.dataTransport)
                    + (cutShort ? reviewsDeadNote(modelVersionId, true, trpcReasonFor(REVIEWS_PROCEDURE, tx)) : '')
                    + allLines.join('\n');
            }
        }
    }
    // 同评论：没熔断却空 = 真的没有评价，返回空串（不写"接口关闭"的假原因）
    return gateTripped
        ? reviewsHeader(modelVersionId) + reviewsDeadNote(modelVersionId, false, trpcReasonFor(REVIEWS_PROCEDURE, tx))
        : '';
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
