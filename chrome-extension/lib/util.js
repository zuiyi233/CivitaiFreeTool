// CFT.util —— 纯逻辑工具函数（搬运自油猴脚本 v1.7.8，逻辑与注释原样保留）
// 加载方式：MV3 SW / offscreen 以 module 副作用 import，content script 按序注入。
// 约束：顶层不得触碰 document/window（函数体内按需使用，调用方保证环境）。
globalThis.CFT = globalThis.CFT || {};

// 清洗 URL：从可能带反引号/空格的字符串中提取并规范化为可请求的 http(s) URL
function normalizeUrl(url) {
    const rawUrl = String(url || '').trim();
    const extractedUrl = rawUrl.match(/https?:\/\/[^\s"'`<>]+/i)?.[0] ?? '';
    const cleanUrl = extractedUrl.replace(/[\u0060\u00B4\u2018\u2019\u201C\u201D\uFF40]/g, '').trim();
    if (!/^https?:\/\//i.test(cleanUrl)) {
        throw new Error(`invalid url: ${url}`);
    }
    return cleanUrl;
}

// 处理文件名中的非法字符，避免 Windows 写文件失败
function sanitizeFilename(name) {
    let out = String(name || '');
    out = out.replace(/[<>:\"/\\|?*\x00-\x1F]/g, '_');
    out = out.replace(/\s+/g, ' ').trim();
    out = out.replace(/[. ]+$/g, '');
    return out;
}

// ---- [补丁01] 安全文件系统名：修复 getFileHandle/getDirectoryHandle "Name is not allowed" ----
// 增强（v1.6.0）：跨平台规则——保留设备名按「首段」判定（覆盖 aux.tar.gz/con.foo）、
// `.`/`..` 兜底、NFC 归一化、去除不可见/方向控制字符、按 UTF-8 字节（非 UTF-16 码元）
// 截断 + 字素簇安全 + 截断哈希保唯一（避免多个长名截成同一名字）。
const _utf8Len = (function () {
    try { const _enc = new TextEncoder(); return (s) => _enc.encode(s).length; }
    catch (_) { return (s) => s.length; } // 极老环境退化：按码元计
})();
function _cheapHash8(str) {
    let h = 0x811c9dc5;
    for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = (h * 0x01000193) >>> 0; }
    return h.toString(16).padStart(8, '0');
}
function _truncateUtf8ByGrapheme(str, maxBytes) {
    if (_utf8Len(str) <= maxBytes) return str;
    let segs;
    try {
        if (typeof Intl !== 'undefined' && Intl.Segmenter) {
            const it = new Intl.Segmenter(undefined, { granularity: 'grapheme' }).segment(str)[Symbol.iterator]();
            segs = []; let r; while (!(r = it.next()).done) segs.push(r.value.segment);
        } else { segs = Array.from(str); }
    } catch (_) { segs = Array.from(str); }
    let out = '', bytes = 0;
    for (const g of segs) { const b = _utf8Len(g); if (bytes + b > maxBytes) break; out += g; bytes += b; }
    return out;
}
function safeFsName(name, opts) {
    const o = opts || {};
    const maxBytes = (typeof o.maxBytes === 'number' && o.maxBytes > 0) ? o.maxBytes : 220; // ext4/APFS 硬上限 255 字节，留余量

    let s = String(name == null ? '' : name);
    if (typeof s.normalize === 'function') s = s.normalize('NFC'); // 跨 mac/Linux/Windows 可复现
    s = s.replace(/[\u200B-\u200F\u202A-\u202E\u2028\u2029\u2060\uFEFF]/g, ''); // 不可见/方向控制/行分隔符
    s = s.replace(/[<>:\"/\\|?*\x00-\x1F\x7F]/g, '_'); // 平台非法字符（含 macOS 的 ':'）
    s = s.replace(/[. ]+$/g, '');       // 尾部点/空格
    s = s.replace(/^\.+/, '_');          // 前导点（Unix 隐藏文件 / CON. 等）
    s = s.replace(/\s+/g, ' ').trim();
    if (!s || s === '.' || s === '..') return 'file_' + Date.now();

    let base = s, ext = '';
    const dot = s.lastIndexOf('.');
    if (dot > 0 && (s.length - dot - 1) <= 12) { base = s.slice(0, dot); ext = s.slice(dot); }

    // 保留设备名：按「首段」（第一个点之前）判定，覆盖 aux.tar.gz / con.foo 这类情形
    const RESERVED = new Set([
        'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$', 'CLOCK$',
        'COM0', 'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
        'LPT0', 'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'
    ]);
    const firstDot = base.indexOf('.');
    const head = firstDot > 0 ? base.slice(0, firstDot) : base;
    if (RESERVED.has(head.toUpperCase())) base = head + '_' + (firstDot > 0 ? base.slice(firstDot) : '');

    // 按 UTF-8 字节 + 字素簇边界截断；过长则截断哈希保唯一（避免多个长名截断成同一名字）
    const extBytes = _utf8Len(ext);
    const budget = Math.max(8, maxBytes - extBytes);
    if (_utf8Len(base) > budget) {
        base = _truncateUtf8ByGrapheme(base, Math.max(1, budget - 9)) + '_' + _cheapHash8(base);
    }
    base = base.replace(/[. ]+$/g, '') || 'file';
    const result = (base + ext) || ('file_' + Date.now());
    return (result === '.' || result === '..') ? ('file_' + Date.now()) : result;
}

// 生成媒体文件名：优先使用 URL 末段文件名，取不到时用 fallbackBase + 时间戳兜底
function buildMediaFilenameFromUrl(url, fallbackBase, fallbackExt) {
    let cleanUrl;
    try {
        cleanUrl = normalizeUrl(url);
    } catch (_) {
        cleanUrl = '';
    }
    if (cleanUrl) {
        try {
            const u = new URL(cleanUrl);
            const lastSeg = u.pathname.split('/').filter(Boolean).pop() || '';
            const decoded = decodeURIComponent(lastSeg);
            const candidate = sanitizeFilename(decoded);
            if (candidate) {
                if (candidate.includes('.')) return candidate;
                const ext = sanitizeFilename(fallbackExt || '');
                return ext ? `${candidate}.${ext}` : candidate;
            }
        } catch (_) { /* 走兜底 */ }
    }
    const base = sanitizeFilename(fallbackBase || 'file') || 'file';
    const ext = sanitizeFilename(fallbackExt || '');
    const ts = Date.now();
    return ext ? `${base}_${ts}.${ext}` : `${base}_${ts}`;
}

// 从 URL 安全推断扩展名（处理 query 与编码）
function getSafeExtensionFromUrl(url, fallbackExt) {
    let ext = '';
    try {
        const cleanUrl = normalizeUrl(url);
        const u = new URL(cleanUrl);
        const lastSeg = decodeURIComponent(u.pathname.split('/').filter(Boolean).pop() || '');
        const dotIndex = lastSeg.lastIndexOf('.');
        ext = dotIndex >= 0 ? lastSeg.substring(dotIndex + 1) : '';
    } catch (_) {
        ext = '';
    }
    ext = sanitizeFilename(ext).replace(/^\.+/, '');
    return ext || sanitizeFilename(fallbackExt || 'jpg') || 'jpg';
}

function parseHeaderValue(responseHeaders, headerName) {
    const match = String(responseHeaders || '').match(new RegExp(`^\\s*${headerName}\\s*:\\s*([^\\r\\n;]+)`, 'im'));
    return match?.[1]?.trim() || '';
}

// 判断是否为视频扩展名（用于选择 Range 分片下载策略）
function isVideoExt(ext) {
    return /^(mp4|webm|mov|m4v)$/i.test(String(ext || '').trim());
}

// image.civitai.com 原图候选链：原图 + 常见宽度降级，提高下载成功率
function getCivitaiImageUrlCandidates(url) {
    const urls = [];
    try {
        const cleanUrl = normalizeUrl(url);
        urls.push(cleanUrl);
        const u = new URL(cleanUrl);
        if (u.hostname.includes('image.civitai.com') && u.pathname.includes('/original=true/')) {
            for (const width of ['width=1024', 'width=768', 'width=450']) {
                const candidate = new URL(cleanUrl);
                candidate.pathname = candidate.pathname.replace('/original=true/', `/${width}/`);
                urls.push(candidate.toString());
            }
        }
    } catch (_) {
        urls.push(url);
    }
    return Array.from(new Set(urls));
}

// 多行文本压平成单行（用于 JSON 元数据导出）
function flattenPromptText(text) {
    if (typeof text !== 'string') return text;
    return text
        .replace(/\r?\n+/g, ' ')
        .replace(/[ \t]{2,}/g, ' ')
        .trim();
}

// 规范化 HTML → 纯文本（保留段落/换行）
function htmlToPlainTextFormatted(htmlString) {
    if (typeof htmlString !== 'string' || !htmlString.includes('<')) {
        return String(htmlString);
    }
    if (typeof DOMParser !== 'undefined') {
        try {
            const doc = new DOMParser().parseFromString(htmlString, 'text/html');
            const clone = doc.body.cloneNode(true);
            clone.querySelectorAll('br').forEach(br => br.replaceWith('\n'));
            clone.querySelectorAll('p, div, li, h1, h2, h3, h4, h5, h6').forEach(el => {
                el.append('\n');
            });
            return clone.textContent.replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();
        } catch (_) { /* 走 textContent 兜底 */ }
    }
    const div = document.createElement('div');
    div.innerHTML = String(htmlString);
    return div.textContent || '';
}

function normalizeMultilineText(text) {
    return String(text || '')
        .replace(/\r\n/g, '\n')
        .replace(/[ \t]+\n/g, '\n')
        .replace(/\n[ \t]+/g, '\n')
        .replace(/\n{3,}/g, '\n\n')
        .trim();
}

// 对象 → 可读纯文本（HTML 转文本，嵌套对象 JSON 化）
function flattenObjectToPlainTextWithHtmlHandling(obj, separator = ": ") {
    if (typeof obj !== 'object' || obj === null) return String(obj);
    const lines = Object.entries(obj).map(([key, value]) => {
        let formattedValue;
        if (typeof value === 'string' && value.includes('<') && value.includes('>')) {
            formattedValue = htmlToPlainTextFormatted(value);
        } else if (typeof value === 'object' && value !== null) {
            formattedValue = JSON.stringify(value, null, 2);
        } else {
            formattedValue = String(value);
        }
        return `${key}${separator}${formattedValue}`;
    });
    return lines.join('\n');
}

// 注意：纯函数，只是读传入 element 的 textContent —— 仅 content script 调用
function getTextContentNormalized(element) {
    return String(element?.textContent || '').replace(/\s+/g, ' ').trim();
}

// 文件名 → { name, extension }
function splitFilename(filename) {
    if (!filename || typeof filename !== 'string') {
        return { name: '', extension: null };
    }
    const lastDotIndex = filename.lastIndexOf('.');
    if (lastDotIndex === -1) return { name: filename, extension: null };
    return {
        name: filename.substring(0, lastDotIndex),
        extension: filename.substring(lastDotIndex + 1)
    };
}

// HTML 转义（渲染历史列表时防止模型名/URL 注入）
function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// 补零 ISO 时间（YYYY-MM-DD HH:mm:ss），保证字符串字典序 = 时间序，双通道排序一致
function formatDateTime(d) {
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

globalThis.CFT.util = {
    normalizeUrl,
    sanitizeFilename,
    safeFsName,
    buildMediaFilenameFromUrl,
    getSafeExtensionFromUrl,
    parseHeaderValue,
    isVideoExt,
    getCivitaiImageUrlCandidates,
    flattenPromptText,
    htmlToPlainTextFormatted,
    normalizeMultilineText,
    flattenObjectToPlainTextWithHtmlHandling,
    getTextContentNormalized,
    splitFilename,
    escapeHtml,
    formatDateTime,
};
