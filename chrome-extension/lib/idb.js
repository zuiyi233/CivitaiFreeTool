// CFT.idb —— IndexedDB 键值封装（搬运自油猴脚本 v1.7.8，逻辑原样保留）
// 搬运：idbOpen(1720-1734)、idbSet(1735-1745)、idbGet(1746-1759)、
//       idbUpdate(1760-1775)、idbDelete（脚本 1817 行附近）。
// 脚本默认值：库名 'civitai_archive_db'，store 'kv' —— 原样保留为默认参数。
// 适配改动：各函数新增可选的 (dbName/storeName) 覆盖参数（默认即脚本原值），
//   供 history.js 使用契约库名 'civitai_archive_assistant' / store 'history'；
//   不传参时行为与脚本完全一致。新增 idbGetAll / idbClear 两个小助手
//   （供 history.all / importJson 全量读写，逻辑与上同风格）。
// 约束：顶层不触碰 indexedDB/chrome（只在函数体内使用 indexedDB）。
globalThis.CFT = globalThis.CFT || {};

const IDB_DB_NAME = 'civitai_archive_db';
const IDB_STORE = 'kv';

// [v1.6.0] 连接复用（OPT-05）：避免每次读写都重新 open（按 库名@store 缓存）
const _idbPromises = new Map(); // cacheKey -> Promise<IDBDatabase>
function _cacheKey(dbName, storeName) { return String(dbName) + '@' + String(storeName); }
function _resolveDbTarget(dbName, storeName, opts) {
    const o = opts || {};
    return {
        dbName: dbName || o.db || o.dbName || IDB_DB_NAME,
        storeName: storeName || o.store || o.storeName || IDB_STORE,
    };
}
function _openDbRaw(dbName, version, storeName) {
    return new Promise((resolve, reject) => {
        const req = indexedDB.open(dbName, version);
        req.onupgradeneeded = () => {
            try {
                if (!req.result.objectStoreNames.contains(storeName)) {
                    req.result.createObjectStore(storeName);
                }
            } catch (_) { /* 已存在则忽略 */ }
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
    });
}
function _watchDb(ck, db) {
    db.onclose = () => { _idbPromises.delete(ck); };
    db.onversionchange = () => { try { db.close(); } catch (_) {} _idbPromises.delete(ck); };
}
function idbOpen(dbName, storeName) {
    const t = _resolveDbTarget(dbName, storeName);
    const ck = _cacheKey(t.dbName, t.storeName);
    const cached = _idbPromises.get(ck);
    if (cached) return cached;
    const p = _openDbRaw(t.dbName, 1, t.storeName).then((db) => {
        // 脏库兜底：库已存在但缺 store 时 onupgradeneeded 不触发，version+1 重开补建
        if (!db.objectStoreNames.contains(t.storeName)) {
            const v = db.version + 1;
            try { db.close(); } catch (_) {}
            return _openDbRaw(t.dbName, v, t.storeName);
        }
        return db;
    }).then((db) => {
        _watchDb(ck, db);
        return db;
    }, (err) => {
        _idbPromises.delete(ck);
        throw err;
    });
    _idbPromises.set(ck, p);
    return p;
}
async function idbSet(key, value, dbName, storeName) {
    try {
        const t = _resolveDbTarget(dbName, storeName);
        const db = await idbOpen(t.dbName, t.storeName);
        return new Promise((resolve, reject) => {
            const tx = db.transaction(t.storeName, 'readwrite');
            tx.objectStore(t.storeName).put(value, key);
            tx.oncomplete = () => resolve();
            tx.onerror = () => reject(tx.error);
        });
    } catch (_) { /* IndexedDB 不可用时静默 */ }
}
async function idbGet(key, dbName, storeName) {
    try {
        const t = _resolveDbTarget(dbName, storeName);
        const db = await idbOpen(t.dbName, t.storeName);
        return new Promise((resolve) => {
            const tx = db.transaction(t.storeName, 'readonly');
            const req = tx.objectStore(t.storeName).get(key);
            req.onsuccess = () => resolve(req.result || null);
            req.onerror = () => resolve(null);
        });
    } catch (_) {
        return null;
    }
}
// [v1.6.0] 单事务原子 read-modify-write（OPT-05）：修复多片同时完成时 completedChunks 丢更新
async function idbUpdate(key, mutate, dbName, storeName) {
    try {
        const t = _resolveDbTarget(dbName, storeName);
        const db = await idbOpen(t.dbName, t.storeName);
        return new Promise((resolve, reject) => {
            const tx = db.transaction(t.storeName, 'readwrite');
            const store = tx.objectStore(t.storeName);
            const req = store.get(key);
            req.onsuccess = () => {
                const next = mutate(req.result || null);
                if (next !== undefined && next !== null) store.put(next, key);
            };
            tx.oncomplete = () => resolve();
            tx.onerror = () => reject(tx.error);
        });
    } catch (_) { /* IndexedDB 不可用时静默 */ }
}
async function idbDelete(key, dbName, storeName) {
    try {
        const t = _resolveDbTarget(dbName, storeName);
        const db = await idbOpen(t.dbName, t.storeName);
        await new Promise((resolve, reject) => {
            const tx = db.transaction(t.storeName, 'readwrite');
            tx.objectStore(t.storeName).delete(key);
            tx.oncomplete = () => resolve();
            tx.onerror = () => reject(tx.error);
        });
    } catch (_) { /* 忽略 */ }
}
// 全量读取（history.all 用；脚本无此函数，按同风格新增）
async function idbGetAll(dbName, storeName) {
    try {
        const t = _resolveDbTarget(dbName, storeName);
        const db = await idbOpen(t.dbName, t.storeName);
        return await new Promise((resolve) => {
            const tx = db.transaction(t.storeName, 'readonly');
            const store = tx.objectStore(t.storeName);
            if (typeof store.getAll === 'function') {
                const req = store.getAll();
                req.onsuccess = () => resolve(Array.isArray(req.result) ? req.result : []);
                req.onerror = () => resolve([]);
            } else {
                const out = [];
                const cursorReq = store.openCursor();
                cursorReq.onsuccess = () => {
                    const cur = cursorReq.result;
                    if (cur) { out.push(cur.value); try { cur.continue(); } catch (_) {} }
                    else resolve(out);
                };
                cursorReq.onerror = () => resolve([]);
            }
        });
    } catch (_) {
        return [];
    }
}
// 清空 store（history.importJson merge=false 用；按同风格新增）
async function idbClear(dbName, storeName) {
    try {
        const t = _resolveDbTarget(dbName, storeName);
        const db = await idbOpen(t.dbName, t.storeName);
        await new Promise((resolve, reject) => {
            const tx = db.transaction(t.storeName, 'readwrite');
            tx.objectStore(t.storeName).clear();
            tx.oncomplete = () => resolve();
            tx.onerror = () => reject(tx.error);
        });
    } catch (_) { /* 忽略 */ }
}

globalThis.CFT.idb = {
    IDB_DB_NAME,
    IDB_STORE,
    idbOpen,
    idbSet,
    idbGet,
    idbUpdate,
    idbDelete,
    idbGetAll,
    idbClear,
};
