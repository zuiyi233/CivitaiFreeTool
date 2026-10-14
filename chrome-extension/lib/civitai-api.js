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
//   5) [2026-10-15 站点变更] Civitai 已硬关闭内部 trpc：commentv2.getInfinite /
//      resourceReview.getPaged / image.getGenerationData 一律 HTTP 401 +
//      {"message":"Please use the public API instead: https://developer.civitai.com/"}
//      （civitai.com 与 civitai.red 实测均 401）。据此：
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
//      imageId=145676249 返回完整 meta。扩展没有也不该去搞 API key，所以 NSFW 样图
//      必然拿不到提示词——这个原因必须原样体现在产物文本里，不能写成「该图没有提示词」。
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

// ---- tRPC 401 熔断（按 procedure 粒度）----
// 站点已关闭内部 trpc（见文件头 5)，任何一次 401 之后本会话内不再调用同一个 procedure，
// 直接走 DOM/空结果分支。要点：
//   · per-procedure：resourceReview.getPaged 熔断不影响 commentv2.getInfinite；
//   · 只认 HTTP 401：404/500/解析错误/超时/网络错一律不进熔断（否则偶发故障会被升级成
//     「永久放弃」，而评论接口其实只是临时抖动）；
//   · 模块级 Map，扩展重载 / SW 与 offscreen 重新 import 即自动清空 → 重启后自然再试一次；
//   · 粒度是 procedure 而不是 procedure+host：civitai.com 与 civitai.red 实测都返回 401
//     （同一套后端），按 host 再拆一层只会让熔断表虚胖、多打一轮注定失败的探测。
const COMMENTS_PROCEDURE = 'commentv2.getInfinite';
const REVIEWS_PROCEDURE = 'resourceReview.getPaged';
const _trpcUnauthorized = new Map(); // procedure -> {at, httpStatus}

function trpcUnauthorizedMark(procedureName, resp) {
    _trpcUnauthorized.set(procedureName, {
        at: Date.now(),
        httpStatus: Number(resp && resp.status) || 401,
    });
    return true;
}
function trpcUnauthorizedTest(procedureName) {
    return _trpcUnauthorized.has(String(procedureName));
}
// 只读快照（自检/调试用；返回拷贝，外部改不动内部状态）
function trpcUnauthorizedState() {
    const out = {};
    _trpcUnauthorized.forEach((v, k) => { out[k] = Object.assign({ procedure: k }, v); });
    return out;
}
function trpcUnauthorizedReset() {
    const before = trpcUnauthorizedState();
    _trpcUnauthorized.clear();
    return before;
}

// 降级说明文案（写进 评论.txt / 批量 errors，务必说清真因，别让用户以为模型没评论）
const TRPC_CLOSED_REASON = '评论/评价接口已被站点关闭（返回 401，官方要求改用公开 API，而公开 API 未提供评论/评价端点）';

function commentsHeader(modelId) {
    return `===== 评论 Comments（模型ID: ${modelId}，抓取时间: ${new Date().toLocaleString()}）=====\n`;
}
function reviewsHeader(modelVersionId) {
    return `===== 评价 Reviews（版本ID: ${modelVersionId}，抓取时间: ${new Date().toLocaleString()}）=====\n`;
}

// hasDom: 'none' = 什么都没拿到；'dom' = 只有页面抓取的评论；'partial' = 有数据但翻页被 401 打断
function commentsDeadNote(modelId, hasDom) {
    if (hasDom === 'dom') {
        return `[说明] ${TRPC_CLOSED_REASON}，本次保存的评论全部来自页面上已渲染的内容（每条带 [页面抓取] 标记）；未加载出来的历史评论无法保存。模型ID: ${modelId}。\n`;
    }
    if (hasDom === 'partial') {
        return `[说明] ${TRPC_CLOSED_REASON}，评论翻页在 401 处中断，以下结果可能不完整。模型ID: ${modelId}。\n`;
    }
    return `[说明] ${TRPC_CLOSED_REASON}，本次仅能保存页面上已渲染的评论，而页面上没有已渲染的评论（评论区未展开或未加载），所以本文件没有任何评论。\n`
        + `[建议] 归档前先在模型页展开评论区并等待加载完成，再重新归档；走页面抓取通道时每条会带 [页面抓取] 标记。模型ID: ${modelId}。\n`;
}

function reviewsDeadNote(modelVersionId, hasData) {
    if (hasData) {
        return `[说明] ${TRPC_CLOSED_REASON}，评价翻页在 401 处中断，以下结果可能不完整。版本ID: ${modelVersionId}。\n`;
    }
    return `[说明] ${TRPC_CLOSED_REASON}；评价也没有页面抓取通道（页面抓取只覆盖评论区），本次未能保存任何评价。版本ID: ${modelVersionId}。\n`;
}

// 评论：trpc 主通道（多组参数容错 + cursor 翻页拿全量，最多 5 页）→ domFallbackFn 兜底；失败不抛错
// 401 熔断命中时（本会话已判死 or 本次首探就 401）不再发请求，并把原因写进文本
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
    let gateTripped = trpcUnauthorizedTest(COMMENTS_PROCEDURE);
    if (!gateTripped) {
        for (const baseInput of inputCandidates) {
            if (trpcUnauthorizedTest(COMMENTS_PROCEDURE)) { gateTripped = true; break; }
            let cursor = null;
            for (let page = 0; page < MAX_PAGES; page++) {
                const pageInput = cursor ? Object.assign({}, baseInput, { cursor }) : baseInput;
                try {
                    const resp = await fetchJson(buildTrpcUrl(COMMENTS_PROCEDURE, pageInput, pageUrlOrHost));
                    if (resp.status === 401) { trpcUnauthorizedMark(COMMENTS_PROCEDURE, resp); gateTripped = true; break; }
                    if (!resp.ok) break;
                    const data = extractTrpcData(await resp.json());
                    const items = (Array.isArray(data) ? data : (data?.items || []));
                    if (!items.length) break;
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
                    if (!nextCursor) break;
                    cursor = nextCursor;
                } catch (_) { break; /* 换下一组参数重试 */ }
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
        // 被熔断却空 = 站点关了接口，必须把原因写进文件（含"先展开评论区"的提示）
        return gateTripped ? commentsHeader(modelId) + commentsDeadNote(modelId, 'none') : '';
    }
    const domOnly = results.every((r) => r && r.fromDom);
    const header = commentsHeader(modelId) + (gateTripped ? commentsDeadNote(modelId, domOnly ? 'dom' : 'partial') : '');
    return header + results.map(c => {
        const prefix = c.fromDom ? '[页面抓取] ' : (c.reply ? '  ↳ 回复 ' : '');
        return `${prefix}【${c.user}】(${c.createdAt || '时间未知'})\n${String(c.content).replace(/\s+$/g, '')}\n`;
    }).join('\n');
}

// 评价：trpc resourceReview.getPaged，多组参数容错；401 熔断命中则写清原因；其余失败返回空串
async function fetchCivitaiReviews(modelVersionId, pageUrlOrHost) {
    const inputCandidates = [
        { modelVersionId: Number(modelVersionId), cursor: null, limit: 50 },
        { modelVersionId: Number(modelVersionId), limit: 50 },
        { id: Number(modelVersionId), cursor: null, limit: 50 },
    ];
    const MAX_PAGES = 5;
    let gateTripped = trpcUnauthorizedTest(REVIEWS_PROCEDURE);
    for (const baseInput of inputCandidates) {
        if (trpcUnauthorizedTest(REVIEWS_PROCEDURE)) { gateTripped = true; break; }
        const allLines = [];
        let cursor = null;
        for (let page = 0; page < MAX_PAGES; page++) {
            const pageInput = cursor ? Object.assign({}, baseInput, { cursor }) : baseInput;
            try {
                const resp = await fetchJson(buildTrpcUrl(REVIEWS_PROCEDURE, pageInput, pageUrlOrHost));
                if (resp.status === 401) { trpcUnauthorizedMark(REVIEWS_PROCEDURE, resp); gateTripped = true; break; }
                if (!resp.ok) break;
                const data = extractTrpcData(await resp.json());
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
            } catch (_) { break; /* 换下一组参数重试 */ }
        }
        if (allLines.length) {
            return reviewsHeader(modelVersionId) + (gateTripped ? reviewsDeadNote(modelVersionId, true) : '') + allLines.join('\n');
        }
    }
    // 同评论：没熔断却空 = 真的没有评价，返回空串（不写"接口关闭"的假原因）
    return gateTripped ? reviewsHeader(modelVersionId) + reviewsDeadNote(modelVersionId, false) : '';
}

// 样图元数据：公开 API GET /api/v1/images?imageId=&withMeta=true&limit=1
// （旧 image.getGenerationData trpc 已 401；本调用与桌面端 civitai_api.py:103
//  get_image_metadata 完全同参，实测 200 / ACAO:* / 无需密钥）
//
// 【返回形状变了】不再返回 tRPC 信封 {result:{data:{json}}}，而是返回归一化后的 core：
//   有元数据：{ found:true,  id, type:'image'|'video', url, nsfwLevel,
//               meta:{prompt,negativePrompt,sampler,cfgScale,steps,Size,seed,...},
//               resources:[], civitaiResources:[{type,weight,modelVersionId}], modelVersionIds:[] }
//   无元数据：{ found:false, id, reason:'empty-items'|'no-meta'|'id-mismatch', type:null,
//               meta:null, resources:[], civitaiResources:[], modelVersionIds:[], nsfwLevel:null }
//   meta 与旧 core.meta 同构同层，调用方读 prompt/sampler/… 的写法不用变（type 也从
//   item.type 带上，旧信封里同样是 json.type）。
// 为什么要"显式归一化"：公开 API 的 items[0].meta 是 {id, meta:{…}} 双层包裹（实测
// imageId 145676249 / 12097475 / 64399178 全都如此），而不带 imageId 的列表查询返回的
// 是扁平 meta；直接当扁平用就会读出 prompt=undefined，把 example.json 静默写成空样本。
// HTTP/网络错误照旧抛（调用方按图跳过，语义与原 tRPC 版一致）。
async function getImageGenerationData(imageId, pageUrlOrHost) {
    const id = parseInt(imageId, 10);
    if (!Number.isFinite(id) || id <= 0) throw new Error('无效的图片ID: ' + imageId);
    const empty = (reason) => ({
        found: false, id, reason, type: null, url: null, nsfwLevel: null,
        meta: null, resources: [], civitaiResources: [], modelVersionIds: [],
    });
    const url = civitaiBaseUrl(pageUrlOrHost) + '/api/v1/images?imageId=' + encodeURIComponent(id) + '&withMeta=true&limit=1';
    const resp = await fetchJson(url, { headers: { Accept: 'application/json' } });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const body = await resp.json();
    const items = (body && Array.isArray(body.items)) ? body.items : [];
    if (!items.length) return empty('empty-items'); // 无密钥时 NSFW/受限图就是空 items（实测 145224191）
    // 与桌面端同款校验：只认 id 对得上的那条，避免把别的图的提示词写进产物
    const item = items.find((it) => it && String(it.id) === String(id)) || null;
    if (!item) return empty('id-mismatch');
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
async function getModelVersionInfo(versionId, pageUrlOrHost) {
    const id = parseInt(versionId, 10);
    if (!Number.isFinite(id) || id <= 0) return null;
    try {
        const resp = await fetchJson(
            civitaiBaseUrl(pageUrlOrHost) + '/api/v1/model-versions/' + encodeURIComponent(id),
            { headers: { Accept: 'application/json' } });
        if (!resp.ok) return null;
        const data = await resp.json();
        return (data && typeof data === 'object') ? data : null;
    } catch (_) {
        return null;
    }
}

globalThis.CFT.api = {
    civitaiHostFrom,
    civitaiBaseUrl,
    buildTrpcUrl,
    extractTrpcData,
    commentUserLabel,
    fetchJson,
    COMMENTS_PROCEDURE,
    REVIEWS_PROCEDURE,
    trpcUnauthorizedMark,
    trpcUnauthorizedTest,
    trpcUnauthorizedState,
    trpcUnauthorizedReset,
    TRPC_CLOSED_REASON,
    fetchCivitaiComments,
    fetchCivitaiReviews,
    getImageGenerationData,
    unwrapImageMeta,
    imageGenerationRefs,
    getModelVersionInfo,
};
