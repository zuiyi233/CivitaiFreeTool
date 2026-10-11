// CFT.log —— 诊断日志（参考脚本 dlLog/dlReadLogs/dlClearLogs/_dlScheduleFlush，脚本 350-378 简化）
// 简化点：
//   1) 内存环形缓冲上限 500 条（脚本 DL_LOG_MAX=300，这里按任务要求取 500），最新在前。
//   2) 每 2s 批量持久化到 chrome.storage.local（key: 'cft_dl_logs'），localStorage 改为 chrome.storage。
//   3) 去掉本地服务上报段（_dlFlushLogsToService 不搬运）。
// API：log(lvl, mod, msg, err, ctx)、read(limit)、clear()。
// 约束：chrome.* 只在函数体内引用；顶层可被 node --check 与非浏览器环境加载。
globalThis.CFT = globalThis.CFT || {};

const LOG_STORAGE_KEY = 'cft_dl_logs';
const LOG_MAX = 500;            // 内存环形缓冲上限
const LOG_FLUSH_MS = 2000;      // 批量持久化间隔 2s

const _buf = [];                // 内存环形缓冲（最新在前）
let _flushTimer = null;
let _dirty = false;

function _fmtTime(d) {
    try {
        if (globalThis.CFT && globalThis.CFT.util && typeof globalThis.CFT.util.formatDateTime === 'function') {
            return globalThis.CFT.util.formatDateTime(d);
        }
    } catch (_) {}
    // util 未加载时的最小兜底（与 formatDateTime 同格式）
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function _storage() {
    try {
        if (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local) {
            return chrome.storage.local;
        }
    } catch (_) {}
    return null;
}

function _scheduleFlush() {
    if (_flushTimer) return;
    _flushTimer = setTimeout(async () => {
        _flushTimer = null;
        if (!_dirty) return;
        _dirty = false;
        const st = _storage();
        if (!st) return; // 非扩展环境：仅保留内存
        try {
            await st.set({ [LOG_STORAGE_KEY]: _buf.slice(0, LOG_MAX) });
        } catch (_) { /* 存储失败仅保留内存 */ }
    }, LOG_FLUSH_MS);
}

function log(lvl, mod, msg, err, ctx) {
    const entry = {
        t: _fmtTime(new Date()),
        lvl: lvl || 'info',
        mod: String(mod || ''),
        msg: String(msg || ''),
        err: err instanceof Error ? (err.stack || err.message || String(err)) : (err ? String(err) : undefined),
        ctx: ctx || undefined,
    };
    _buf.unshift(entry);
    if (_buf.length > LOG_MAX) _buf.length = LOG_MAX;
    _dirty = true;
    if (lvl === 'error' && typeof console !== 'undefined') {
        try { console.error(`[cft:${mod}]`, msg, err || ''); } catch (_) {}
    }
    _scheduleFlush();
    return entry;
}

function read(limit) {
    return _buf.slice(0, limit || 200);
}

async function clear() {
    _buf.length = 0;
    _dirty = false;
    const st = _storage();
    if (st) {
        try { await st.remove(LOG_STORAGE_KEY); } catch (_) {}
    }
}

globalThis.CFT.log = {
    LOG_STORAGE_KEY,
    LOG_MAX,
    log,
    read,
    clear,
};
