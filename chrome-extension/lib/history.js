// CFT.history —— 归档历史（IndexedDB 存储 + 导入导出 + 写盘镜像）
// 搬运：findArchivedRecord（脚本 1713-1719，原样）；filterHistoryByKeyword
//   （脚本 2007-2016，原读模块级 historyRecordsCache —— 适配为纯函数
//   filterHistoryByKeyword(records, keyword)，调用方传入全量数组）。
// 基于 CFT.idb 新实现：all/get/put/delete/search/exportJson/importJson/mirrorFile。
// IndexedDB 库名/store：以契约第 5 节为准 —— 库 'civitai_archive_assistant'、store 'history'，
//   key = `${modelId}@${modelVersionId}` 手动指定（脚本 idbOpen 的默认库名不同，不沿用）。
// 记录字段（契约第 5 节 + 脚本 2515-2528 的实际字段）：
//   {modelId, modelVersionId, modelName, modelVer, dirName, archiveTime(ISO),
//    modelFile, commentCount, imgCount}（另保留脚本的 modelType/modelUrl/picCount/
//    videoCount/hasModelFile 等扩展字段，put 时透传不丢）。
// 约束：顶层不调用 chrome；在 node 下 CFT.idb 缺失时各函数返回空/假值而不抛错。
globalThis.CFT = globalThis.CFT || {};

const HISTORY_DB = 'civitai_archive_assistant';
const HISTORY_STORE = 'history';
const HISTORY_FILENAME = '_civitai_archive_history.json';

function _historyIdb() {
    try {
        if (globalThis.CFT && globalThis.CFT.idb) return globalThis.CFT.idb;
    } catch (_) {}
    return null;
}
function _recordKey(modelId, versionId) {
    return String(modelId) + '@' + String(versionId);
}

// 查询某模型版本是否已归档（用于页面提示）
function findArchivedRecord(records, modelId, modelVersionId) {
    return (records || []).find(r => String(r.modelId) === String(modelId)
        && (!modelVersionId || String(r.modelVersionId) === String(modelVersionId))) || null;
}

// 按关键字过滤历史（脚本原读模块级缓存；此处改为纯函数，调用方传入全量）
// 保持脚本原匹配字段：modelName / modelType / dirName / modelVer（大小写不敏感）
function filterHistoryByKeyword(records, keyword) {
    const kw = String(keyword || '').trim().toLowerCase();
    if (!kw) return records || [];
    return (records || []).filter(r =>
        String(r.modelName || '').toLowerCase().includes(kw) ||
        String(r.modelType || '').toLowerCase().includes(kw) ||
        String(r.dirName || '').toLowerCase().includes(kw) ||
        String(r.modelVer || '').toLowerCase().includes(kw)
    );
}

// store 全量（按 archiveTime 倒序，字符串字典序 = 时间序）
async function all() {
    const idb = _historyIdb();
    if (!idb) return [];
    const rows = await idb.idbGetAll(HISTORY_DB, HISTORY_STORE);
    return (Array.isArray(rows) ? rows : []).sort((a, b) =>
        String(b.archiveTime || '').localeCompare(String(a.archiveTime || '')));
}

async function get(modelId, versionId) {
    const idb = _historyIdb();
    if (!idb) return null;
    return await idb.idbGet(_recordKey(modelId, versionId), HISTORY_DB, HISTORY_STORE);
}

// put(record)：archiveTime 缺失时自动补 ISO；同 key 覆盖（upsert）
async function put(record) {
    const idb = _historyIdb();
    if (!idb) return false;
    const r = Object.assign({}, record);
    if (r.modelId == null || r.modelVersionId == null) return false;
    if (!r.archiveTime) {
        try {
            r.archiveTime = new Date().toISOString();
        } catch (_) {
            r.archiveTime = String(Date.now());
        }
    }
    await idb.idbSet(_recordKey(r.modelId, r.modelVersionId), r, HISTORY_DB, HISTORY_STORE);
    return true;
}

async function del(modelId, versionId) {
    const idb = _historyIdb();
    if (!idb) return false;
    const key = (versionId == null && typeof modelId === 'string' && modelId.includes('@'))
        ? modelId : _recordKey(modelId, versionId);
    await idb.idbDelete(key, HISTORY_DB, HISTORY_STORE);
    return true;
}

async function search(keyword) {
    return filterHistoryByKeyword(await all(), keyword);
}

async function exportJson() {
    return JSON.stringify(await all(), null, 2);
}

// importJson(json, merge)：merge=true 合并（同 key 以 archiveTime 最新为准），
//   merge=false 清空后写入；返回 {added, updated, total}
async function importJson(json, merge) {
    const idb = _historyIdb();
    if (!idb) return { added: 0, updated: 0, total: 0 };
    let arr;
    try {
        arr = typeof json === 'string' ? JSON.parse(json) : json;
    } catch (_) {
        throw new Error('invalid history json');
    }
    if (!Array.isArray(arr)) throw new Error('invalid history json: expected array');
    if (!merge) {
        try { await idb.idbClear(HISTORY_DB, HISTORY_STORE); } catch (_) {}
    }
    const existing = merge ? await all() : [];
    const byKey = new Map();
    for (const r of existing) {
        if (r && r.modelId != null) byKey.set(_recordKey(r.modelId, r.modelVersionId), r);
    }
    let added = 0, updated = 0;
    for (const item of arr) {
        if (!item || item.modelId == null || item.modelVersionId == null) continue;
        const k = _recordKey(item.modelId, item.modelVersionId);
        const ex = byKey.get(k);
        if (!ex) { byKey.set(k, item); added++; }
        else if (String(item.archiveTime || '') >= String(ex.archiveTime || '')) { byKey.set(k, item); updated++; }
    }
    for (const [, r] of byKey) {
        await idb.idbSet(_recordKey(r.modelId, r.modelVersionId), r, HISTORY_DB, HISTORY_STORE);
    }
    return { added, updated, total: byKey.size };
}

// mirrorFile(writeFileFn)：把全量数组 JSON.stringify 后调用
//   writeFileFn({relPath:'_civitai_archive_history.json', ...})，失败不抛错
async function mirrorFile(writeFileFn) {
    if (typeof writeFileFn !== 'function') return false;
    try {
        const text = await exportJson();
        let blobSize = 0;
        try { blobSize = new Blob([text]).size; } catch (_) { blobSize = text.length; }
        const res = await writeFileFn({
            relPath: HISTORY_FILENAME,
            dataB64: (typeof btoa !== 'undefined')
                ? btoa(unescape(encodeURIComponent(text)))
                : Buffer.from(text, 'utf8').toString('base64'),
            overwrite: true,
            size: blobSize,
        });
        return !!(res && res.ok !== false);
    } catch (_) {
        return false; // 失败不抛错
    }
}

globalThis.CFT.history = {
    HISTORY_DB,
    HISTORY_STORE,
    HISTORY_FILENAME,
    findArchivedRecord,
    filterHistoryByKeyword,
    all,
    get,
    put,
    delete: del,
    search,
    exportJson,
    importJson,
    mirrorFile,
};
