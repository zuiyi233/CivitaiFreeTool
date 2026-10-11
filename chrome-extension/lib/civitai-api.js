// CFT.api —— Civitai trpc 接口层（搬运自油猴脚本 v1.7.8，逻辑原样保留）
// 适配改动（相对脚本原码）：
//   1) civitaiBaseUrl：脚本读 window.location.hostname；扩展 SW/offscreen 无 window，
//      改为 window 存在时按原逻辑判定，否则默认 https://civitai.com。
//   2) fetchCivitaiComments：删除脚本 1510-1522 的 DOM 兜底段，改为签名
//      fetchCivitaiComments(modelId, domFallbackFn)；trpc 无结果且 domFallbackFn 为函数时
//      results = await domFallbackFn()（与原 results 同构数组），再走同样的格式化。
//   3) 新增 fetchJson(url, options)：带超时的 fetch 封装（默认 15s，AbortController），
//      返回 Response；支持 options.timeoutMs 覆盖与外层 options.signal 联动取消。
// 约束：顶层不触碰 document/window/chrome（civitaiBaseUrl 只在函数体内读 window）。
globalThis.CFT = globalThis.CFT || {};

// 当前站点判断（内容脚本环境按页面 hostname 判定；SW/offscreen 无 window 时默认 civitai.com）
const civitaiBaseUrl = () => {
    try {
        const host = (typeof window !== 'undefined' && window.location && window.location.hostname) || '';
        if (String(host).includes('civitai.red')) return 'https://civitai.red';
    } catch (_) { /* 非页面环境走默认 */ }
    return 'https://civitai.com';
};

// ---- trpc 工具（评论/评价/样图元数据）----
function buildTrpcUrl(procedureName, inputJson) {
    return civitaiBaseUrl() + '/api/trpc/' + procedureName + '?input=' + encodeURIComponent(JSON.stringify({ json: inputJson }));
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

// 评论：trpc 主通道（多组参数容错 + cursor 翻页拿全量，最多 5 页）→ domFallbackFn 兜底；失败不抛错
async function fetchCivitaiComments(modelId, domFallbackFn) {
    const results = [];
    const inputCandidates = [
        { entityType: 'Model', entityId: Number(modelId), sort: 'Newest', cursor: null, limit: 50, authed: true },
        { entityType: 'Model', entityId: Number(modelId), sort: 'Newest', cursor: null, limit: 50 },
        { modelId: Number(modelId), sort: 'Newest', cursor: null, limit: 50 },
        { modelId: Number(modelId), cursor: null, limit: 50, authed: true },
        { modelId: Number(modelId), limit: 50 },
    ];
    const MAX_PAGES = 5;
    for (const baseInput of inputCandidates) {
        let cursor = null;
        for (let page = 0; page < MAX_PAGES; page++) {
            const pageInput = cursor ? Object.assign({}, baseInput, { cursor }) : baseInput;
            try {
                const resp = await fetch(buildTrpcUrl('commentv2.getInfinite', pageInput));
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

    // DOM 兜底改为由调用方注入（content script 实现并回传同构数组；SW/offscreen 传空即可）
    if (!results.length && typeof domFallbackFn === 'function') {
        try {
            const fb = await domFallbackFn();
            if (Array.isArray(fb) && fb.length) results.push(...fb);
        } catch (_) { /* 忽略 DOM 兜底错误 */ }
    }

    if (!results.length) return '';
    const header = `===== 评论 Comments（模型ID: ${modelId}，抓取时间: ${new Date().toLocaleString()}）=====\n`;
    return header + results.map(c => {
        const prefix = c.fromDom ? '[页面抓取] ' : (c.reply ? '  ↳ 回复 ' : '');
        return `${prefix}【${c.user}】(${c.createdAt || '时间未知'})\n${String(c.content).replace(/\s+$/g, '')}\n`;
    }).join('\n');
}

// 评价：trpc resourceReview.getPaged，多组参数容错；失败返回空串
async function fetchCivitaiReviews(modelVersionId) {
    const inputCandidates = [
        { modelVersionId: Number(modelVersionId), cursor: null, limit: 50 },
        { modelVersionId: Number(modelVersionId), limit: 50 },
        { id: Number(modelVersionId), cursor: null, limit: 50 },
    ];
    const MAX_PAGES = 5;
    for (const baseInput of inputCandidates) {
        const allLines = [];
        let cursor = null;
        for (let page = 0; page < MAX_PAGES; page++) {
            const pageInput = cursor ? Object.assign({}, baseInput, { cursor }) : baseInput;
            try {
                const resp = await fetch(buildTrpcUrl('resourceReview.getPaged', pageInput));
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
            return `===== 评价 Reviews（版本ID: ${modelVersionId}，抓取时间: ${new Date().toLocaleString()}）=====\n` + allLines.join('\n');
        }
    }
    return '';
}

// 样图 generation data（用于 example.txt 的提示词与单图元数据）
// [v1.2.0] 加 15s 超时，避免单张挂起卡死整条归档
async function getImageGenerationData(imageId) {
    const url = buildTrpcUrl('image.getGenerationData', { id: parseInt(imageId, 10), authed: true });
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 15000);
    try {
        const resp = await fetch(url, { signal: ctrl.signal });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        return await resp.json();
    } finally {
        clearTimeout(timer);
    }
}

globalThis.CFT.api = {
    civitaiBaseUrl,
    buildTrpcUrl,
    extractTrpcData,
    commentUserLabel,
    fetchJson,
    fetchCivitaiComments,
    fetchCivitaiReviews,
    getImageGenerationData,
};
