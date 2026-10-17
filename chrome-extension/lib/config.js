// CFT.config —— 插件配置（契约第 6 节，按 CONFIG_DEFAULTS）
// 脚本 localStorage 版 config/loadConfig（脚本 46-65）仅作参考，不搬运；
// 此处存取走 chrome.storage.local，SW/offscreen/content 均可用。
// 内存缓存 + load() 缺失键用默认值补齐 + save(patch) 合并写回 + get() 读缓存。
// 约束：chrome.* 只在函数体内引用，顶层可被 node --check 与非浏览器环境加载。
globalThis.CFT = globalThis.CFT || {};

const CONFIG_DEFAULTS = {
    coverSaveMode: 'video',     // 'image' | 'video'
    downloadImages: true,
    downloadVideos: true,
    sendModelToSoftware: true,  // 模型文件本体转发软件下载（永远建议 true）
    maxPics: 100,
    badgeEnabled: true,
    globalConcurrency: 4,       // 写盘通道并发（chrome.downloads / 软件写盘）
    fallbackDirPrefix: 'CivitaiArchive',  // 降级 downloads 时的子目录前缀
    // 自动化：重复归档不再弹原生 confirm（无人值守/无头环境默认 dismiss，会直接取消归档）。
    // 默认 false 保持人工确认；AI 调试时置 true 即可全程免交互。
    autoConfirmDuplicate: false,
};
const CONFIG_STORAGE_KEY = 'cft_config_v1';

let _cache = null; // 内存缓存（null = 未加载）

function _storage() {
    try {
        if (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local) {
            return chrome.storage.local;
        }
    } catch (_) {}
    return null;
}

function _withDefaults(saved) {
    return Object.assign({}, CONFIG_DEFAULTS, (saved && typeof saved === 'object') ? saved : {});
}

// load()：chrome.storage.local 读，缺失键用默认值补齐；无 chrome 环境时返回默认拷贝
async function load() {
    const st = _storage();
    if (!st) {
        _cache = _withDefaults(null);
        return Object.assign({}, _cache);
    }
    try {
        const raw = await st.get(CONFIG_STORAGE_KEY);
        const saved = raw ? raw[CONFIG_STORAGE_KEY] : null;
        _cache = _withDefaults(saved);
    } catch (_) {
        if (!_cache) _cache = _withDefaults(null);
    }
    return Object.assign({}, _cache);
}

// save(patch)：merge 写回（读当前→合并→写→刷新缓存），返回合并后拷贝
async function save(patch) {
    if (!_cache) await load();
    const next = Object.assign({}, _cache, (patch && typeof patch === 'object') ? patch : {});
    _cache = next;
    const st = _storage();
    if (st) {
        try { await st.set({ [CONFIG_STORAGE_KEY]: next }); } catch (_) { /* 存储失败仅保留内存 */ }
    }
    return Object.assign({}, _cache);
}

// get()：返回内存缓存（未加载时返回默认拷贝，不触发异步读）
function get() {
    if (!_cache) _cache = _withDefaults(null);
    return Object.assign({}, _cache);
}

globalThis.CFT.config = {
    CONFIG_DEFAULTS,
    CONFIG_STORAGE_KEY,
    load,
    save,
    get,
};
