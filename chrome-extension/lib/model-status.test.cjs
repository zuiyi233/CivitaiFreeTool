/* CFT.modelStatus 纯逻辑自测：node chrome-extension/lib/model-status.test.cjs
 * 不联网、不碰 chrome/DOM；把桌面端契约里的响应原样喂进来，钉住扩展侧的口径。
 */
const assert = require('assert/strict');
const path = require('path');
require(path.join(__dirname, 'model-status.js'));

const MS = globalThis.CFT.modelStatus;
let passed = 0;
const ok = (s) => { passed++; console.log('OK ' + s); };
const clone = (v) => JSON.parse(JSON.stringify(v));

function backend(over) {
  return Object.assign({
    model_id: '101', version_id: '1001', state: 'downloaded', matched: 'version',
    version_matched: true,
    local: { source: 'library', version_id: '1001', version_name: 'v1.0', model_name: 'Demo',
      base_model: 'SDXL', path: 'D:\\models\\demo.safetensors', paths: ['D:\\models\\demo.safetensors'],
      file_count: 1, size: 123, exists: true },
    local_versions: [{ version_id: '1001', version_name: 'v1.0', source: 'library',
      path: 'D:\\models\\demo.safetensors', exists: true }],
    download: null, history: null, update: null, reason: '',
  }, over || {});
}

// ---- key / 请求项 ----
assert.equal(MS.keyOf('101', '1001'), 'version:1001');
assert.equal(MS.keyOf('101', ''), 'model:101');
assert.equal(MS.keyOf('', ''), '');
assert.equal(MS.keyOf('abc', '0'), '');
const items = MS.buildItems([
  { key: 'version:1001:file:choose', id: '101', version: '1001', url: 'https://civitai.red/models/101', tabId: 11 },
  { key: 'version:1001:file:choose', id: '101', enrichVersionId: '1001', url: 'https://civitai.com/models/101', tabId: 12 },
  { key: 'model:202:file:choose', id: '202', url: 'https://civitai.com/models/202', tabId: 13 },
]);
assert.equal(items.length, 3);
assert.equal(items[0].key, 'version:1001:file:choose');
assert.equal(items[0].versionId, '1001');
assert.equal(items[2].versionId, '');
assert.equal(MS.buildItem({ id: '303', meta: { versionId: '3003' } }).versionId, '3003');
ok('buildItems 复用行 key、保留逐标签 tabId、版本 ID 按 URL→enrich→meta 取值');

// ---- 哈希级身份：C 站 SHA256 透传（只认 64 位十六进制、去重、限 8） ----
const H = (c) => c.repeat(64);
const hashed = MS.buildItem({ id: '101', version: '1001', meta: {
  sha256s: [H('a'), H('a'), 'not-a-hash', H('B')],
  files: [{ sha256: H('c') }, { sha256: '' }, {}],
} });
assert.equal(JSON.stringify(hashed.hashes), JSON.stringify([H('a'), H('b'), H('c')]),
  '去重、小写、同时收 sha256s 与 files[].sha256');
assert.equal(MS.buildItem({ id: '101' }).hashes, undefined, '没有哈希时不要空数组（请求体保持旧形状）');
const many = MS.buildItem({ id: '101', hashes: Array.from({ length: 12 }, (_, i) => H((i % 10).toString())) });
assert.equal(many.hashes.length, 8, '每项最多带 8 个哈希');
assert.equal(MS.hashesOf({ meta: { files: [{ sha256: H('f').toUpperCase() }] } }).length, 1);
assert.equal(JSON.stringify(MS.hashesOf(null)), '[]');
ok('哈希透传：sha256s/files 合并去重限 8，非法值不进请求体');

// ---- 响应归一化 ----
const req = [
  { key: 'version:1001:file:choose', modelId: '101', versionId: '1001', url: 'https://civitai.red/models/101?a=1' },
  { key: 'url:https://civitai.com/models/bad', modelId: '', versionId: '', url: 'https://civitai.com/models/bad' },
];
const res = MS.normalizeResponse({
  ok: true, generated_at: 1700000000,
  library: { ready: true, scanning: false, count: 2 },
  updates: { checked_at: 1700000000, available: true, records: 1 },
  items: [backend(), backend({ model_id: '', version_id: '', state: 'unknown', reason: '缺少模型 ID',
    local: null, local_versions: [], matched: 'none', version_matched: false })],
}, { items: req, now: 1700000100000 });
assert.equal(res.byKey['version:1001'].state, 'downloaded');
assert.equal(res.byKey['url:https://civitai.com/models/bad'].state, 'unknown');
assert.equal(res.byKey['url:https://civitai.com/models/bad'].key, 'url:https://civitai.com/models/bad');
assert.equal(res.cacheAt, 1700000000000);
assert.equal(res.byKey['version:1001'].updatesCheckedAt, 1700000000000);
const row = { key: 'version:1001:file:choose', id: '101', version: '1001', url: 'https://civitai.red/models/101' };
MS.applyToRows([row], res);
assert.equal(row.dl.state, 'downloaded', '行 key 带 file 段时也要能按版本身份回填');
ok('normalizeResponse 按请求顺序落 key，无 ID 的页面用 url key 保持稳定；行按版本身份回填');

// ---- 缓存合并 / 离线 ----
let cache = { entries: {} };
let merged = MS.mergeCache(cache, res, { items: req, now: 1700000100000 });
cache = merged.cache;
assert.equal(cache.entries['version:1001'].state, 'downloaded');
assert.deepEqual(cache.entries['version:1001'].seenUrls, ['https://civitai.red/models/101?a=1']);
const updateRes = MS.normalizeResponse({
  ok: true, generated_at: 1700000200, library: { ready: true },
  updates: { checked_at: 1700000200, available: true, records: 1 },
  items: [backend({ state: 'update_available', update: {
    known: true, has_update: true, local_version: '1001', local_name: 'v1.0',
    latest_version: '1002', latest_name: 'v1.2', latest_url: 'https://civitai.red/models/101?modelVersionId=1002',
    behind: 1, checked_at: 1700000200,
  } })],
}, { items: [req[0]], now: 1700000200000 });
cache = MS.mergeCache(cache, updateRes, { items: [req[0]], now: 1700000200000 }).cache;
const entry = cache.entries['version:1001'];
assert.equal(entry.state, 'update_available');
assert.equal(entry.update.latest_name, 'v1.2');
assert.deepEqual(entry.seenUrls, ['https://civitai.red/models/101?a=1']);
const offline = MS.offlineResponse([req[0]], { now: 1700000300000 });
const kept = MS.mergeCache(cache, offline, { items: [req[0]], now: 1700000300000 }).cache;
assert.equal(kept.entries['version:1001'].state, 'update_available', '离线不能把已下载/有更新抹成未知');
assert.equal(kept.entries['version:1001'].offline, true);
assert.equal(offline.byKey['version:1001'].state, 'unknown');
ok('合并缓存：新状态覆盖、seenUrls 累加、离线保留旧状态并标 offline');

// ---- 过期 ----
const t0 = 1700000000000;
assert.equal(MS.isStale({ state: 'downloaded', checkedAt: t0 }, t0 + 4 * 60 * 1000), false);
assert.equal(MS.isStale({ state: 'downloaded', checkedAt: t0 }, t0 + 6 * 60 * 1000), true);
assert.equal(MS.isStale({ state: 'downloading', checkedAt: t0 }, t0 + 3 * 60 * 1000), true);
assert.equal(MS.isStale(null, t0), true);
ok('下载中 2 分钟过期，其余 5 分钟过期；没有条目算过期');

// ---- 文案 ----
const lab = MS.labelOf(entry);
assert.equal(lab.label, '有更新');
assert.equal(lab.badge, '↑');
assert(lab.text.includes('v1.0 → v1.2'), lab.text);
assert(lab.title.includes('下载选中项的新版'), lab.title);
const dl = MS.labelOf(backend({ state: 'downloading', download: { status: 'downloading', progress: 42.4, version_matched: true } }));
assert.equal(dl.short, '下载中 42%');
const other = MS.labelOf(backend({ state: 'other_version', version_matched: false, matched: 'model',
  local_versions: [{ version_id: '1000', version_name: 'v0' }, { version_id: '999', version_name: 'v-1' }] }));
assert.equal(other.label, '其他版本');
const miss = MS.labelOf(backend({ state: 'not_downloaded', local: null, local_versions: [],
  history: { file_exists: false }, reason: '模型库已扫描完成，未找到该版本' }));
assert.equal(miss.label, '未下载');
assert(miss.text.includes('历史文件已不在'), miss.text);
const unknown = MS.labelOf(backend({ state: 'unknown', local: null, local_versions: [], reason: 'CFT 未连接' }), { offline: true });
assert.equal(unknown.label, '未知');
assert.equal(unknown.offline, true);
assert(unknown.title.includes('上次缓存'), unknown.title);
const noUpdateChecked = MS.labelOf(backend({ state: 'downloaded', updatesCheckedAt: 0 }));
assert(noUpdateChecked.title.includes('还没在 CFT 里检查过'), noUpdateChecked.title);
ok('六态文案/徽章/说明齐全，未检查更新不会被说成“已是最新”');

// ---- 汇总 / 筛选 ----
const all = [entry, backend({ state: 'downloaded' }), backend({ state: 'downloading' }),
  backend({ state: 'other_version' }), backend({ state: 'not_downloaded' }), backend({ state: 'unknown' })];
const sum = MS.summaryOf(all, { cacheAt: t0 });
assert.equal(sum.counts.downloaded, 1);
assert.equal(sum.counts.update, 1);
assert.equal(sum.counts.downloading, 1);
assert.equal(sum.counts.other, 1);
assert.equal(sum.counts.not, 1);
assert.equal(sum.counts.unknown, 1);
assert(sum.text.includes('有更新 1') && sum.text.includes('未下载 1'), sum.text);
const rows = all.map((e) => ({ dl: e }));
assert.equal(MS.filterRows(rows, 'update').length, 1);
assert.equal(MS.filterRows(rows, 'not').length, 1);
assert.equal(MS.filterRows(rows, '').length, 6);
ok('汇总按六态计数；筛选口径与标签一致');

// ---- 缓存上限 ----
const big = { entries: {} };
for (let i = 0; i < MS.MAX_ENTRIES + 5; i++) {
  big.entries['version:' + (i + 1)] = { key: 'version:' + (i + 1), state: 'downloaded', checkedAt: i, seenAt: i };
}
const capped = MS.sanitizeCache(big);
assert.equal(Object.keys(capped.entries).length, MS.MAX_ENTRIES + 5, 'sanitize 不做淘汰');
const pruned = MS.mergeCache(big, { byKey: {}, offline: false }, { items: [], now: 999999 }).cache;
assert.equal(Object.keys(pruned.entries).length, MS.MAX_ENTRIES, '合并时按 seenAt 淘汰到上限');
ok('缓存条目有上限，按最近触达淘汰');

console.log(`\n${passed} passed`);
