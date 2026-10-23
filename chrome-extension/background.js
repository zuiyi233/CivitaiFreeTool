// background.js —— SW 消息路由 + 批量提交 + offscreen 管理 + 写盘通道 + bridge 客户端（MV3 module）
// 上游 v1.3.0 能力：批量标签页提交（cft-batch-start/status/clear）+ tabs-common.js 抽取，完整保留；
// 归档助手 2.0 能力：CFT_* 信封路由 / ensureOffscreen / writeFile 降级 / bridge 客户端 / 一键归档入口，并接入新消息总线。
import './lib/util.js';
import './lib/civitai-api.js';
import './lib/scheduler.js';
import './lib/idb.js';
import './lib/history.js';
import './lib/config.js';
import './lib/log.js';
import './lib/model-status.js';
import './tabs-common.js';
import './lib/tab-index.js';

const ext = typeof browser !== 'undefined' ? browser : chrome;
const MS = CFT.modelStatus;
const STATUS_KEY = MS.CACHE_KEY;
const STATUS_MAX_ITEMS = 500;      // 与桌面端 /api/model/status 的上限一致
const STATUS_DEDUPE_MS = 1500;     // popup 与多个页面同时刷新时合并成本机桥一次请求
const HISTORY_KEY = 'cftPageHistoryV1';
const HISTORY_MAX = 500;
const NOTIFIED_KEY = 'cftNotifiedTerminalsV1';
const NOTIFY_DEST_KEY = 'cftNotifyDestV1';
const TASK_WATCH_ALARM = 'cftTaskWatch';
const TASK_WATCH_DONE_KEY = 'cftTaskWatchDoneV1';
const TASK_WATCH_MAX_MS = 2 * 60 * 60 * 1000;   // 守望上限 2 小时，避免永久每分钟轮询
const TAB_INDEX_ALARM = 'cftTabIndex';
const TAB_INDEX_DEBOUNCE_MS = 1200;
const DEV_RELOAD_SEEN_KEY = 'cftDevReloadSeenV1';
const BRIDGE = 'http://127.0.0.1:47531';
const CIVITAI_RE = /^https?:\/\/(www\.)?civitai\.(red|com)\/(api\/download\/)?models\//i;
const OFFSCREEN_URL = 'offscreen/archive-worker.html';
const RAW_SIZE_SOFTWARE_MAX = 32 * 1024 * 1024; // 软件通道原始大小上限
const RAW_SIZE_DOWNLOADS_MAX = 20 * 1024 * 1024; // downloads 降级上限
const ARCHIVE_CONFIG_TTL_MS = 30 * 1000; // getArchiveConfig 缓存 30s
const ARCHIVE_BATCH_MAX_ROWS = 500; // 批量归档上限（契约 8.1）
const ARCHIVE_BATCH_COLLECT_MS = 5000; // 逐页 collectPage 超时（契约 8.3）
// 软件写盘超时随体积放大：固定 15s 会把「慢盘上的合法大封面」误判成失败（桥侧 body 上限 128MB /
// 解码后 64MB，browser_bridge.py:36-37，比本扩展 32MB 原始上限宽松，放大超时不与服务器冲突）。
const SW_WRITE_TIMEOUT_BASE_MS = 15000;
const SW_WRITE_TIMEOUT_PER_MB_MS = 1500; // 每 1MB 原始数据追加，桥要解码 + 落盘
const SW_WRITE_TIMEOUT_MAX_MS = 60000; // 32MB 原始 → 15+48=63s 收口到 60s；sendBg 无宿主超时
const STORE = 'lastBatch';

// 批量提交状态（上游 v1.3.0）
let job = null, starting = false;
// 下载状态：批量归档运行期间让出工具栏徽章（归档进度优先），结束后从缓存补回
let _statusBatchRunning = false;
const _statusInflight = new Map();

function bgLog(lvl, msg, err) {
  try { CFT.log.log(lvl, 'bg', msg, err); } catch (_) {}
}
CFT.config.load().then(() => bgLog('info', 'background 启动，配置已加载')).catch(() => {});

// ---- 批量提交：上次批次恢复（上游 v1.3.0） ----
const ready = (async () => {
  const old = (await ext.storage.local.get(STORE))[STORE];
  if (old) {
    job = old;
    if (job.running) {
      job.running = false;
      for (const row of job.items) {
        if (row.status === 'sending') {row.status = 'uncertain'; row.msg = '提交中断，请先在 CFT 核对，避免重复下载';}
        else if (row.status === 'waiting') {row.status = 'failed'; row.msg = '尚未提交，可重新勾选';}
      }
      await save();
    }
  }
})();

// badge：ms 可选。缺省持久显示（批量 job 常驻徽标）；传 ms 则到时自动清除（归档反馈）
function badge(text, color, ms) {
  ext.action.setBadgeText({ text }).catch(() => {});
  ext.action.setBadgeBackgroundColor({ color }).catch(() => {});
  if (ms) setTimeout(() => { ext.action.setBadgeText({ text: '' }).catch(() => {}); }, ms);
}
async function save() { await ext.storage.local.set({ [STORE]: job }); }
async function post(url) {
  const ctrl = new AbortController(), timer = setTimeout(() => ctrl.abort(), 12000);
  try {
    const res = await fetch(BRIDGE + '/api/download', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url, caps: ['bundle'] }), signal: ctrl.signal });
    const data = await res.json();
    if (!res.ok || !data.ok) return { status: 'failed', msg: data.msg || 'CFT 未接受请求' };
    // 已提交即标下载中：页面胶囊 / 弹层徽章不必等 CFT 解析入队
    await markSubmitted(url);
    return { status: 'sent', msg: submitResultMsg(data) };
  } catch (_) {
    // The server may have accepted it before the response was lost. Never retry implicitly.
    return { status: 'uncertain', msg: '未收到确认，请在 CFT 核对后再发送' };
  } finally { clearTimeout(timer); }
}
// 落点回显（新桌面端 /api/download 会带 dest_dir + routed）：老版本没有这两个字段时
// 文案逐字不变，避免"扩展升到新协议、桌面还是旧版"时列表行冒出看不出所以然的话。
// 注意：**绝不把 bundle 令牌写进 job/存储** —— 令牌是一次性落点凭据，行状态会被持久化到
// storage.local 并在重启后恢复，把令牌显示出来只会诱导用户复制一个已经无意义的字符串。
function submitResultMsg(data) {
  const d = data || {};
  const dest = String(d.dest_dir || '').trim();
  if (!dest) return '已提交，在 CFT 查看解析结果';
  return (d.routed === true ? '已提交，将按类型保存到 ' : '已提交，将保存到 ') + dest;
}
async function run() {
  try {
    for (const row of job.items) {
      row.status = 'sending'; await save();
      Object.assign(row, await post(row.url)); await save();
      badge(String(job.items.filter(r => r.status === 'sent').length), '#0a84ff');
    }
  } catch (_) {
    for (const row of job.items) {
      if (row.status === 'sending') { row.status = 'uncertain'; row.msg = '提交中断，请在 CFT 核对'; }
      else if (row.status === 'waiting') { row.status = 'failed'; row.msg = '尚未提交'; }
    }
  } finally {
    job.running = false; await save();
    scheduleStatusBadges(300);
    const sent = job.items.filter(r => r.status === 'sent').length;
    badge(sent === job.items.length ? '✓' : '!', sent === job.items.length ? '#0a84ff' : '#c23d50');
    ext.action.setTitle({ title: `已提交 ${sent}/${job.items.length}，点击图标查看结果` }).catch(() => {});
  }
}
async function start(urls) {
  await ready;
  if (starting || job?.running) return { ok: false, msg: '上一批正在提交，请稍候', job };
  starting = true;
  try {
    if (!Array.isArray(urls) || !urls.length || urls.length > 500) return { ok: false, msg: '请勾选 1–500 个模型页' };
    const links = urls.map(CftTabs.modelLink);
    if (links.some(v => !v)) return { ok: false, msg: '含无效模型链接，未发送任何页面' };
    const completedKeys = [...new Set([...(job?.completedKeys || []), ...(job?.items || []).filter(r => ['sent', 'uncertain'].includes(r.status)).map(r => r.key)])];
    const items = [...new Map(links.map(v => [v.key, { ...v, status: 'waiting', msg: '' }])).values()].filter(r => !completedKeys.includes(r.key));
    if (!items.length) return { ok: false, msg: '这些页面已提交或待核对；如确需再次发送，请先清除提交记录' };
    job = { id: Date.now().toString(), running: true, completedKeys, items };
    await save();
    ensureTaskWatch(true);
    void run();
    return { ok: true, job };
  } finally { starting = false; }
}

// ---- 通用 bridge 请求（GET/POST，超时抛错，调用方处理） ----
async function bridgeFetch(path, opts) {
  const o = opts || {};
  const ctrl = new AbortController();
  const timer = setTimeout(() => { try { ctrl.abort(); } catch (_) {} }, o.timeoutMs || 10000);
  try {
    const res = await fetch(BRIDGE + path, {
      method: o.method || 'GET',
      headers: o.body ? { 'Content-Type': 'application/json' } : undefined,
      body: o.body ? JSON.stringify(o.body) : undefined,
      signal: ctrl.signal,
    });
    const data = await res.json().catch(() => ({}));
    return { httpOk: res.ok, status: res.status, data };
  } finally {
    clearTimeout(timer);
  }
}

// ---- 打开的模型页索引：只读上报给本机桥（契约见 docs/browser-tabs-bridge.md） ----
// 只使用 tabs 的 title/url/id；不读页面正文、Cookie、API Key，也不上报非 C 站页面。
let tabIndexTimer = null, tabIndexInFlight = false, tabIndexAgain = false;
function scheduleTabIndexPush(delay) {
  if (tabIndexTimer) clearTimeout(tabIndexTimer);
  tabIndexTimer = setTimeout(() => {
    tabIndexTimer = null;
    void pushTabIndex();
  }, Math.max(150, Number(delay) || TAB_INDEX_DEBOUNCE_MS));
}
async function pushTabIndex() {
  if (!CftTabIndex || typeof CftTabIndex.buildPayload !== 'function') return;
  if (tabIndexInFlight) { tabIndexAgain = true; return; }
  tabIndexInFlight = true;
  try {
    const tabs = await ext.tabs.query({});
    const source = 'chrome-extension';
    const payload = CftTabIndex.buildPayload(tabs, { source, extVersion: extensionVersion() });
    const res = await bridgeFetch('/api/ext/tabs', { method: 'POST', body: payload, timeoutMs: 5000 });
    await maybeDevReload(res);
  } catch (_) {
    // 桌面端未启动/端口未开：下次标签页事件或心跳再试；不要把 URL 写进日志
  } finally {
    tabIndexInFlight = false;
    if (tabIndexAgain) { tabIndexAgain = false; scheduleTabIndexPush(300); }
  }
}
function ensureTabIndexAlarm() {
  if (!ext.alarms) return;
  try { ext.alarms.create(TAB_INDEX_ALARM, { periodInMinutes: 1 }); } catch (_) {}
}

function extensionVersion() {
  try {
    const manifest = ext.runtime && typeof ext.runtime.getManifest === 'function'
      ? ext.runtime.getManifest() : null;
    const version = manifest && manifest.version ? String(manifest.version) : '';
    return /^[0-9A-Za-z][0-9A-Za-z._-]{0,31}$/.test(version) ? version : '';
  } catch (_) { return ''; }
}

// 开发态重载：桥在索引上报的响应里带 dev_reload_at；只认新时间戳，存 storage 防重载循环。
async function maybeDevReload(resp) {
  const at = Number(resp && resp.data && resp.data.dev_reload_at) || 0;
  if (!at) return;
  let seen = 0;
  try {
    const saved = await ext.storage.local.get(DEV_RELOAD_SEEN_KEY);
    seen = Number(saved && saved[DEV_RELOAD_SEEN_KEY]) || 0;
  } catch (_) { return; }
  if (at <= seen) return;
  try { await ext.storage.local.set({ [DEV_RELOAD_SEEN_KEY]: at }); } catch (_) { return; }
  try { ext.runtime.reload(); } catch (_) {}
}

// ---- 下载状态：缓存 / 查询 / 徽章（契约见 docs/extension-model-status.md） ----
async function readStatusCache() {
  try {
    const raw = await ext.storage.local.get(STATUS_KEY);
    return MS.sanitizeCache(raw ? raw[STATUS_KEY] : null);
  } catch (_) { return MS.sanitizeCache(null); }
}
async function writeStatusCache(cache) {
  try { await ext.storage.local.set({ [STATUS_KEY]: cache }); } catch (_) { /* 存储满/权限变化都不影响下载 */ }
}
function uniqueStatusItems(items) {
  const map = new Map();
  for (const raw of items || []) {
    const it = MS.buildItem(raw);
    if (!it.modelId && !it.versionId && !it.url) continue;
    const key = MS.keyOf(it.modelId, it.versionId) || it.key;
    if (!key) continue;
    if (!map.has(key)) map.set(key, Object.assign({}, it, { key }));
  }
  return Array.from(map.values()).slice(0, STATUS_MAX_ITEMS);
}
// 本机桥失败要区分「没开 CFT」和「CFT 太旧没有这个端点」：两者动作不同
async function queryBridgeStatus(items) {
  let r;
  try {
    r = await bridgeFetch('/api/model/status', {
      method: 'POST', timeoutMs: 6000,
      body: { items: items.map((i) => ({
        model_id: i.modelId, version_id: i.versionId, url: i.url,
        ...(Array.isArray(i.hashes) && i.hashes.length ? { hashes: i.hashes } : {}),
      })), include_updates: true },
    });
  } catch (_) {
    return MS.offlineResponse(items, { error: '未连接 CFT' });
  }
  if (r.httpOk && r.data && r.data.ok) return MS.normalizeResponse(r.data, { items });
  if (r.status === 404) {
    return MS.offlineResponse(items, {
      offline: false,
      error: '桌面端版本过旧，不支持下载状态查询（需 CFT 2.7.0 及以上）',
      reason: '桌面端版本过旧，不支持下载状态查询（需 CFT 2.7.0 及以上）',
    });
  }
  return MS.offlineResponse(items, { error: (r.data && (r.data.msg || r.data.error)) || ('HTTP ' + r.status) });
}
// 只读缓存：没有条目也要给出 unknown，不能因为“没查过”就显示成未下载
function statusFromCache(items, cache, now) {
  const out = [], byKey = {};
  for (const req of items) {
    const key = MS.keyOf(req.modelId, req.versionId) || req.key;
    const prev = MS.getEntry(cache, req.modelId, req.versionId);
    const entry = prev || MS.normalizeItem(
      { state: 'unknown', reason: '扩展里还没有这个模型的状态缓存，点「刷新」查询 CFT' },
      { now, keyHint: key, seenUrls: req.url ? [req.url] : [] });
    entry.key = entry.key || key;
    out.push(entry); byKey[entry.key] = entry;
  }
  return { ok: true, items: out, byKey, cacheAt: cache.savedAt || 0, library: {}, updates: {}, offline: false, cachedOnly: true };
}
function queryStatusOnce(items, force) {
  const sig = items.map((i) => i.key + ':' + i.modelId + ':' + i.versionId).sort().join('|');
  const now = Date.now();
  const hit = _statusInflight.get(sig);
  // 主动刷新（用户点刷新/离线后重试）必须真的再打一次桥；dedupe 只服务并发的自动查询
  if (!force && hit && now - hit.at < STATUS_DEDUPE_MS) return hit.promise;
  const promise = queryBridgeStatus(items).finally(() => {
    setTimeout(() => {
      const cur = _statusInflight.get(sig);
      if (cur && cur.promise === promise) _statusInflight.delete(sig);
    }, STATUS_DEDUPE_MS);
  });
  _statusInflight.set(sig, { at: now, promise });
  return promise;
}
async function applyBadge(tabId, entry) {
  if (_statusBatchRunning || !Number.isFinite(Number(tabId))) return;
  const lab = entry ? MS.labelOf(entry) : null;
  try {
    await ext.action.setBadgeText({ tabId, text: lab ? lab.badge : '' });
    if (lab) await ext.action.setBadgeBackgroundColor({ tabId, color: lab.color });
    await ext.action.setTitle({
      tabId,
      title: lab ? (lab.label + (lab.detail ? ' · ' + lab.detail : '') + '\n' + lab.title) : 'CivitaiFreeTool 一键下载',
    });
  } catch (_) { /* 标签已关 / 浏览器不支持 tabId 徽章：跳过 */ }
}
async function applyBadgesForItems(items, result, cache) {
  const byTab = new Map();
  for (const it of items) {
    const tid = Number(it.tabId);
    if (!Number.isFinite(tid) || byTab.has(tid)) continue;
    const entry = (result.byKey && result.byKey[it.key]) || MS.getEntry(cache, it.modelId, it.versionId);
    byTab.set(tid, entry || null);
  }
  await Promise.all(Array.from(byTab.entries()).map(([tid, entry]) => applyBadge(tid, entry)));
}
// popup 批量刷新后把状态推给页面胶囊：页面不用再多打一轮本机桥
async function pushStatusToTabs(items, result, cache) {
  const byTab = new Map();
  for (const it of items) {
    const tid = Number(it.tabId);
    if (!Number.isFinite(tid) || byTab.has(tid)) continue;
    const key = MS.keyOf(it.modelId, it.versionId);
    const entry = (result.byKey && (result.byKey[key] || result.byKey[it.key]))
      || MS.getEntry(cache, it.modelId, it.versionId);
    if (entry) byTab.set(tid, entry);
  }
  await Promise.all(Array.from(byTab.entries()).map(async ([tabId, entry]) => {
    try {
      await chrome.tabs.sendMessage(tabId, { __cft: true, to: 'content', type: 'modelStatusPush', payload: { entry } });
    } catch (_) { /* 页面没有 content script（非模型页/未刷新） */ }
  }));
}
async function reapplyStatusBadges() {
  if (_statusBatchRunning) return;
  const cache = await readStatusCache();
  let tabs = [];
  try { tabs = await ext.tabs.query({}); } catch (_) { return; }
  for (const tab of tabs) {
    const link = CftTabs.modelLink(tab && tab.url);
    if (!link) continue;
    const entry = MS.getEntry(cache, link.id, link.version);
    if (entry) await applyBadge(tab.id, entry);
  }
}
async function clearStatusBadges() {
  let tabs = [];
  try { tabs = await ext.tabs.query({}); } catch (_) { return; }
  await Promise.all(tabs.map((tab) => {
    if (!CftTabs.modelLink(tab && tab.url)) return Promise.resolve();
    return ext.action.setBadgeText({ tabId: tab.id, text: '' }).catch(() => {});
  }));
}
// 批量提交/归档会连续写很多条状态：把徽章重算合并成一次，别每条都遍历全部标签
let _statusBadgeTimer = null;
function scheduleStatusBadges(delayMs) {
  if (_statusBadgeTimer) return;
  _statusBadgeTimer = setTimeout(() => {
    _statusBadgeTimer = null;
    void reapplyStatusBadges();
  }, Math.max(50, Number(delayMs) || 300));
}
function idsFromUrl(url, pageUrl) {
  const link = CftTabs.modelLink(pageUrl || url || '');
  let modelId = link ? link.id : '';
  let versionId = link ? (link.version || '') : '';
  const m = /\/api\/download\/models\/([1-9]\d*)/i.exec(String(url || ''));
  if (m) versionId = m[1];
  if (!modelId) {
    const mm = /\/models\/([1-9]\d*)/i.exec(String(url || ''));
    if (mm) modelId = mm[1];
  }
  return { modelId, versionId };
}
// 提交成功即标「下载中」：CFT 解析/入队前的这几十秒也要有反馈，而不是仍显示“未下载”
async function markSubmitted(url, pageUrl) {
  const ids = idsFromUrl(url, pageUrl);
  if (!ids.modelId && !ids.versionId) return;
  const cache = await readStatusCache();
  const now = Date.now();
  const seen = new Set();
  if (pageUrl) seen.add(pageUrl);
  const make = (key, versionId) => {
    const prev = cache.entries[key] || {};
    const urls = new Set([...(Array.isArray(prev.seenUrls) ? prev.seenUrls : []), ...seen]);
    const entry = Object.assign({}, prev, {
      key, modelId: ids.modelId, versionId,
      state: 'downloading', matched: versionId ? 'version' : 'model',
      versionMatched: !!versionId,
      download: { status: 'pending', progress: 0, version_matched: !!versionId, task_count: 1, filename: '', dest_dir: '' },
      local: prev.local || null, localVersions: prev.localVersions || [],
      update: prev.update || null, history: prev.history || null,
      reason: '已提交到 CFT，等待解析/入队',
      checkedAt: now, seenAt: now, seenUrls: Array.from(urls).slice(-5),
      offline: false, libraryReady: true, libraryScanning: false,
      updatesCheckedAt: prev.updatesCheckedAt || 0,
    });
    cache.entries[key] = entry;
    return entry;
  };
  make(MS.keyOf(ids.modelId, ids.versionId) || ('model:' + ids.modelId), ids.versionId);
  if (ids.modelId) make('model:' + ids.modelId, '');
  await writeStatusCache(cache);
  scheduleStatusBadges(300);
}
async function handleModelStatusQuery(payload, sender) {
  const p = (payload && typeof payload === 'object') ? payload : {};
  const senderTab = sender && sender.tab && Number.isFinite(Number(sender.tab.id)) ? Number(sender.tab.id) : null;
  const raw = Array.isArray(p.items) ? p.items : [];
  const items = [];
  for (const row of raw) {
    const item = MS.buildItem(row, row && row.tabId != null ? row.tabId : senderTab);
    if (!item.modelId && !item.versionId && !item.url) continue;
    items.push(item);
  }
  const uniq = uniqueStatusItems(items);
  if (!uniq.length) {
    return { ok: true, items: [], byKey: {}, cacheAt: 0, offline: false, library: {}, updates: {}, error: '' };
  }
  const cache = await readStatusCache();
  const now = Date.now();
  const mode = p.refresh === true ? 'force' : (p.refresh === false ? 'cache' : 'auto');
  const need = mode === 'force' || (mode === 'auto' && uniq.some((it) => MS.isStale(MS.getEntry(cache, it.modelId, it.versionId), now)));
  let result;
  if (need) {
    result = await queryStatusOnce(uniq, mode === 'force');
  } else {
    result = statusFromCache(uniq, cache, now);
  }
  let nextCache = cache;
  if (!result.cachedOnly) {
    const merged = MS.mergeCache(cache, result, { items: uniq, now });
    nextCache = merged.cache;
    await writeStatusCache(nextCache);
    if (result.ok === false) {
      // 离线/旧桌面端：返回给界面的应是“上次已知状态 + offline”，不是离线占位的 unknown
      const items2 = uniq.map((req) => nextCache.entries[MS.keyOf(req.modelId, req.versionId) || req.key]
        || MS.getEntry(nextCache, req.modelId, req.versionId) || result.byKey[req.key]).filter(Boolean);
      const byKey2 = {};
      for (const e of items2) byKey2[e.key] = e;
      result = Object.assign({}, result, { items: items2, byKey: byKey2 });
    }
  }
  // 列表页一次查多个模型，没有单一模型身份：不写工具栏徽章、不推页面胶囊
  const wantBadge = p.badge !== false;
  if (wantBadge) {
    await applyBadgesForItems(items, result, nextCache);
    if (!result.cachedOnly) await pushStatusToTabs(items, result, nextCache);
  }
  return {    ok: true, items: result.items, byKey: result.byKey,
    cacheAt: result.cacheAt || nextCache.savedAt || 0,
    cachedOnly: !!result.cachedOnly, offline: !!result.offline,
    library: result.library || {}, updates: result.updates || {},
    error: result.error || '',
  };
}
async function handleModelStatusCache(payload) {
  const p = (payload && typeof payload === 'object') ? payload : {};
  const cache = await readStatusCache();
  if (p.modelId != null || p.versionId != null) {
    const entry = MS.getEntry(cache, p.modelId, p.versionId);
    return { ok: true, at: cache.savedAt || 0, entries: entry ? { [entry.key]: entry } : {} };
  }
  const keys = Array.isArray(p.keys) ? new Set(p.keys.map(String)) : null;
  if (!keys) return { ok: true, at: cache.savedAt || 0, entries: cache.entries };
  const entries = {};
  for (const key of keys) if (cache.entries[key]) entries[key] = cache.entries[key];
  return { ok: true, at: cache.savedAt || 0, entries };
}

// ---- 页面级历史：只记 C 站模型页 URL/标题/时间，仅本地；与下载历史无关 ----
const _visitSeen = new Map();   // tabId -> 30s 去重签名
function historyKeyOf(link) {
  return MS.keyOf(link && link.id, link && link.version) || (link && link.key) || '';
}
async function readPageHistory() {
  try {
    const raw = (await ext.storage.local.get(HISTORY_KEY))[HISTORY_KEY];
    const items = raw && raw.items && typeof raw.items === 'object' ? raw.items : {};
    return { version: 1, savedAt: Number(raw && raw.savedAt) || 0, items: Object.assign({}, items) };
  } catch (_) { return { version: 1, savedAt: 0, items: {} }; }
}
async function rememberVisit(tab) {
  try {
    if (!tab || !tab.url) return;
    // 下载直链不是“看过的模型页”：modelLink 会把 /api/download/models/<vid> 的版本号当模型 ID
    try {
      if (/^\/api\/download\//i.test(new URL(tab.url).pathname)) return;
    } catch (_) { return; }
    const link = CftTabs.modelLink(tab.url);
    if (!link) return;
    const key = historyKeyOf(link);
    if (!key) return;
    const now = Date.now();
    const sig = key + '@' + Math.floor(now / 30000);
    if (_visitSeen.get(tab.id) === sig) return;
    _visitSeen.set(tab.id, sig);
    if (_visitSeen.size > 400) _visitSeen.clear();
    const h = await readPageHistory();
    const prev = h.items[key] || {};
    h.items[key] = {
      key, modelId: link.id, versionId: link.version || '',
      url: link.url, title: CftTabs.cleanTitle(tab.title),
      firstAt: Number(prev.firstAt) || now, lastAt: now,
      count: (Number(prev.count) || 0) + 1,
    };
    const keys = Object.keys(h.items);
    if (keys.length > HISTORY_MAX) {
      keys.sort((a, b) => (Number(h.items[b].lastAt) || 0) - (Number(h.items[a].lastAt) || 0));
      for (const k of keys.slice(HISTORY_MAX)) delete h.items[k];
    }
    h.savedAt = now;
    await ext.storage.local.set({ [HISTORY_KEY]: h });
  } catch (_) { /* 历史记录失败不影响标签/徽章 */ }
}
async function rememberOpenTabs() {
  try {
    const tabs = await ext.tabs.query({});
    for (const tab of tabs) await rememberVisit(tab);
  } catch (_) { /* ignore */ }
}

// ---- 本机桥 RPC / 更新检查 / 打开目录 ----
async function bridgeRpc(method, args, timeoutMs) {
  try {
    const r = await bridgeFetch('/api/rpc', {
      method: 'POST', timeoutMs: timeoutMs || 12000,
      body: { method, args: args || [] },
    });
    const d = r.data || {};
    if (r.httpOk && d.ok !== false && !d.error) return { ok: true, result: d.result };
    return { ok: false, msg: d.error || d.msg || ('HTTP ' + r.status), requiresApiKey: /api\s*key/i.test(String(d.error || d.msg || '')) };
  } catch (e) {
    return { ok: false, msg: 'CFT 未连接' };
  }
}
async function handleCheckModelUpdates(payload) {
  const paths = [...new Set((payload && Array.isArray(payload.paths) ? payload.paths : [])
    .map((p) => String(p || '')).filter(Boolean))].slice(0, 50);
  if (!paths.length) return { ok: false, msg: '当前列表没有模型库命中的本地模型路径' };
  const r = await bridgeRpc('mm_check_updates', [false, paths], 20000);
  if (!r.ok) return { ok: false, msg: r.msg, requiresApiKey: r.requiresApiKey };
  const res = r.result || {};
  return { ok: true, started: res.started === true, recent: res.recent === true,
    msg: res.msg || (res.started ? '已开始检查更新' : '检查没有启动') };
}
async function handleModelUpdateState() {
  const r = await bridgeRpc('get_mm_update_state', [], 8000);
  return r.ok ? { ok: true, state: r.result || {} } : { ok: false, msg: r.msg };
}
async function handleRevealPath(payload) {
  const path = String((payload && payload.path) || '').trim();
  if (!path) return { ok: false, msg: '路径无效' };
  try {
    const r = await bridgeFetch('/api/reveal', { method: 'POST', timeoutMs: 6000, body: { path } });
    const d = r.data || {};
    if (r.status === 404) return { ok: false, msg: '桌面端版本过旧，不支持打开目录（需 CFT 2.7.0 及以上）' };
    if (r.status === 503) return { ok: false, msg: d.msg || '桌面端打开目录服务未就绪，请升级 CFT' };
    return { ok: !!(r.httpOk && d.ok), msg: d.msg || (r.httpOk ? '' : 'CFT 未应答'), path: d.path || '' };
  } catch (_) { return { ok: false, msg: 'CFT 未连接，无法打开目录' }; }
}

// ---- 完成通知：按 key+phase 去重，持久化避免 SW 重启后重复弹 ----
async function readJson(key, fallback) {
  try { const raw = (await ext.storage.local.get(key))[key]; return raw == null ? fallback : raw; }
  catch (_) { return fallback; }
}
async function writeJson(key, value) {
  try { await ext.storage.local.set({ [key]: value }); } catch (_) {}
}
const _notifying = new Set();
async function notifyTaskDone(payload) {
  const p = payload || {};
  if (!p.key || !p.phase) return { ok: false, msg: '缺少任务标识' };
  // 同一版本重下会产生新的 task_id：去重键必须带上它，否则第二次永远静默
  const key = String(p.key) + ':' + String(p.phase) + (p.task_id ? ':' + String(p.task_id) : '');
  if (_notifying.has(key)) return { ok: true, skipped: true };
  _notifying.add(key);
  try {
    const raw = await readJson(NOTIFIED_KEY, []);
    const list = Array.isArray(raw) ? raw.map(String) : [];
    if (list.indexOf(key) >= 0) return { ok: true, skipped: true };
    if (!ext.notifications || typeof ext.notifications.create !== 'function') {
      return { ok: false, msg: '此浏览器不支持通知' };
    }
    const title = p.phase === 'done' ? '下载完成' : p.phase === 'failed' ? '下载失败'
      : p.phase === 'library' ? '已在模型库中' : '任务状态更新';
    const dest = String(p.dest_dir || '');
    const message = [String(p.name || p.filename || '模型'),
      p.filename ? '文件：' + p.filename : '', dest ? '落点：' + dest : ''].filter(Boolean).join('\n');
    const id = 'cft-task-' + Date.now() + '-' + Math.random().toString(36).slice(2, 6);
    const opts = { type: 'basic', iconUrl: ext.runtime.getURL('icons/icon128.png'),
      title, message, contextMessage: 'CivitaiFreeTool', priority: 1 };
    if (dest) opts.buttons = [{ title: '打开所在目录' }];
    // 先 create 再写去重：创建失败/浏览器拒绝时不能把这条永久标记成“已通知”
    try { await ext.notifications.create(id, opts); } catch (e) { return { ok: false, msg: String((e && e.message) || e) }; }
    list.push(key);
    await writeJson(NOTIFIED_KEY, list.slice(-200));
    if (dest) {
      const map = await readJson(NOTIFY_DEST_KEY, {});
      map[id] = dest;
      const keys = Object.keys(map).slice(-100);
      const trimmed = {}; for (const k of keys) trimmed[k] = map[k];
      await writeJson(NOTIFY_DEST_KEY, trimmed);
    }
    return { ok: true, id };
  } finally { _notifying.delete(key); }
}
async function revealNotification(id) {
  const map = await readJson(NOTIFY_DEST_KEY, {});
  const dest = map[id] || '';
  if (dest) return handleRevealPath({ path: dest });
  return { ok: false, msg: '这条通知没有登记落点' };
}
if (ext.notifications && ext.notifications.onButtonClicked) {
  ext.notifications.onButtonClicked.addListener((id) => { void revealNotification(id); });
}
if (ext.notifications && ext.notifications.onClicked) {
  ext.notifications.onClicked.addListener((id) => {
    void (async () => {
      try { await ext.tabs.create({ url: BRIDGE + '/' }); } catch (_) {}
      try { await ext.notifications.clear(id); } catch (_) {}
    })();
  });
}

// ---- 后台守望：弹层/侧栏关掉后仍能在任务落地时发通知（最多 2 小时） ----
function ensureTaskWatch(fresh) {
  try {
    if (!ext.alarms) return;
    ext.alarms.create(TASK_WATCH_ALARM, { periodInMinutes: 1 });
    void ext.storage.local.set({ cftTaskWatchSince: Date.now() });
    if (fresh) void ext.storage.local.set({ [TASK_WATCH_DONE_KEY]: [] });
  } catch (_) {}
}
function clearTaskWatch() {
  try { if (ext.alarms) ext.alarms.clear(TASK_WATCH_ALARM); } catch (_) {}
  try { void ext.storage.local.remove(['cftTaskWatchSince', TASK_WATCH_DONE_KEY]); } catch (_) {}
}
async function trackedPendingItems() {
  await ready;
  const raw = await readJson('cftExtTasks', null);
  const fromPopup = raw && Array.isArray(raw.items) ? raw.items.filter((it) => it && it.url && (!it.cft || !it.cft.terminal)) : [];
  if (fromPopup.length) return fromPopup;
  return (job?.items || []).filter((r) => ['sent', 'waiting', 'sending', 'uncertain'].includes(r.status))
    .map((r) => ({ key: r.key, url: r.url, modelId: r.id, versionId: r.version, name: r.title, cft: null }));
}
// get_tasks 的 version_id 是新增字段；旧桌面端没有时从任务 URL 兜底取。
function taskVersionId(t) {
  const direct = String((t && (t.version_id || t.versionId)) || '');
  if (/^[1-9]\d*$/.test(direct)) return direct;
  const s = String((t && t.url) || '');
  const m = /[?&]modelVersionId=([1-9]\d*)/i.exec(s) || /\/api\/download\/models\/([1-9]\d*)/i.exec(s);
  return m ? m[1] : '';
}
async function watchTasks() {
  const since = Number(await readJson('cftTaskWatchSince', 0)) || 0;
  if (since && Date.now() - since > TASK_WATCH_MAX_MS) { clearTaskWatch(); return; }
  const items = await trackedPendingItems();
  if (!items.length) { clearTaskWatch(); return; }
  const [tr, pr] = await Promise.all([bridgeRpc('get_tasks', [], 8000), bridgeRpc('get_parse_state', [], 8000)]);
  if (!tr.ok && !pr.ok) return;
  const tasks = Array.isArray(tr.result) ? tr.result : [];
  const rawDone = await readJson(TASK_WATCH_DONE_KEY, []);
  const doneSet = new Set(Array.isArray(rawDone) ? rawDone.map(String) : []);
  let anyActive = false;
  for (const it of items) {
    const itemKey = String(it.key || it.url || '');
    const link = CftTabs.modelLink(it.url || '');
    const mid = String(it.modelId || '') || (link ? link.id : '');
    const wantedVid = String(it.versionId || '');
    const mine = tasks.filter((t) => {
      if (!t || !t.url) return false;
      const m = /\/models\/([1-9]\d*)/i.exec(String(t.url));
      if (!m || m[1] !== mid) return false;
      const tv = taskVersionId(t);
      // 有版本信息就只认这个版本；旧桌面端没有版本时仍按模型级匹配
      return !wantedVid || !tv || tv === wantedVid;
    });
    if (mine.some((t) => !['done', 'error', 'canceled'].includes(t.status))) anyActive = true;
    const done = mine.find((t) => t.status === 'done');
    const bad = mine.find((t) => ['error', 'canceled'].includes(t.status));
    let phase = '', dest = '', filename = '';
    if (done) { phase = 'done'; dest = done.dest_dir || ''; filename = done.filename || ''; }
    else if (bad) { phase = 'failed'; dest = bad.dest_dir || ''; filename = bad.filename || ''; }
    else if (!mine.length && it.versionId) {
      const chk = await bridgeRpc('library_check', [it.versionId], 6000);
      if (chk.ok && chk.result && chk.result.found) {
        phase = 'library'; dest = (chk.result.paths || [])[0] || '';
      }
    }
    if (phase) {
      await notifyTaskDone({ key: itemKey, phase, name: it.name || '', task_id: (done && done.id) || (bad && bad.id) || '',
        filename, dest_dir: dest });
      doneSet.add(itemKey);
    }
  }
  await writeJson(TASK_WATCH_DONE_KEY, Array.from(doneSet).slice(-200));
  // 所有跟踪项都有落地结论且没有活动任务 → 停止守望；否则最多跑到 2 小时上限
  if (!anyActive && items.every((it) => doneSet.has(String(it.key || it.url || '')))) {
    clearTaskWatch();
  }
}
if (ext.alarms && ext.alarms.onAlarm) {
  ext.alarms.onAlarm.addListener((alarm) => {
    if (!alarm) return;
    if (alarm.name === TASK_WATCH_ALARM) void watchTasks();
    else if (alarm.name === TAB_INDEX_ALARM) void pushTabIndex();
  });
}


ext.runtime.onInstalled.addListener(() => {
  ext.contextMenus.removeAll(() => ext.contextMenus.create({ id: 'cft-dl', title: '发送到 CivitaiFreeTool', contexts: ['page', 'link'] }));
  ensureTabIndexAlarm();
  scheduleTabIndexPush(800);
});
// 标签页跳转：记录页面历史（仅 C 站模型页）；徽章只从缓存补/清，不发网络
ext.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (!changeInfo) return;
  scheduleTabIndexPush(TAB_INDEX_DEBOUNCE_MS);
  if (changeInfo.url !== undefined || changeInfo.status === 'complete') {
    void rememberVisit(tab || { id: tabId, url: changeInfo.url, title: '' });
  }
  if (changeInfo.url === undefined) return;
  (async () => {
    if (_statusBatchRunning) return;
    const link = CftTabs.modelLink(changeInfo.url || '');
    if (!link) {
      await ext.action.setBadgeText({ tabId, text: '' }).catch(() => {});
      return;
    }
    const cache = await readStatusCache();
    const entry = MS.getEntry(cache, link.id, link.version);
    if (entry) await applyBadge(tabId, entry);
  })().catch(() => {});
});
// 打开/关闭/移动/切换标签页都会改变索引；去抖合并成一次上报。
for (const name of ['onCreated', 'onActivated', 'onMoved', 'onAttached', 'onDetached']) {
  try { ext.tabs[name].addListener(() => scheduleTabIndexPush(TAB_INDEX_DEBOUNCE_MS)); } catch (_) {}
}
ext.tabs.onRemoved.addListener(() => scheduleTabIndexPush(300));
ensureTabIndexAlarm();
scheduleTabIndexPush(1200);
ext.runtime.onStartup.addListener(() => {
  scheduleStatusBadges(500);
  void rememberOpenTabs();
  ensureTabIndexAlarm();
  scheduleTabIndexPush(800);
  // 上次关浏览器时还有未落地的提交：恢复后台守望
  void (async () => {
    const raw = await readJson('cftExtTasks', null);
    const pending = (raw && Array.isArray(raw.items) ? raw.items : []).some((it) => it && (!it.cft || !it.cft.terminal));
    if (pending) ensureTaskWatch();
  })();
});

ext.contextMenus.onClicked.addListener(info => {
  if (info.menuItemId !== 'cft-dl') return;
  // 失败必须给出可读原因：徽标只说明「没成功」，标题才说明为什么
  start([info.linkUrl || info.pageUrl]).then(r => {
    if (!r || !r.ok) { badge('!', '#c23d50'); ext.action.setTitle({ title: (r && r.msg) || '发送失败' }).catch(() => {}); }
  }, () => {
    badge('!', '#c23d50');
    ext.action.setTitle({ title: '发送失败，请检查扩展状态后重试' }).catch(() => {});
  }).catch(() => {}); // setTitle 本身异常也不该冒成 unhandled rejection
});

// ---- offscreen 管理（并发防抖 + 已存在竞态兜底） ----
let _offscreenPromise = null;
async function hasOffscreen() {
  try {
    const ctxs = await chrome.runtime.getContexts({ contextTypes: ['OFFSCREEN_DOCUMENT'] });
    return !!(ctxs && ctxs.length);
  } catch (_) {
    return false;
  }
}
async function ensureOffscreen() {
  // 同步临界区：检查+赋值同一同步段完成，并发调用共享同一 promise，无 unhandled rejection
  if (_offscreenPromise) return _offscreenPromise;
  _offscreenPromise = (async () => {
    try {
      if (await hasOffscreen()) return await waitOffscreenReady();
      try {
        await chrome.offscreen.createDocument({
          url: OFFSCREEN_URL,
          reasons: ['DOM_PARSER'],
          justification: '归档长任务需要 DOM 解析与持续网络/写盘，SW 无 DOM 且会被休眠',
        });
      } catch (e) {
        if (!(await hasOffscreen())) throw e; // 已存在竞态（并发/残留）：继续等就绪
      }
      // createDocument 只保证文档已创建，模块里的 onMessage 监听可能还没注册。
      // 不握手就直接发 archive，消息会被静默丢弃（sendMessage 的 .catch 吞掉），
      // 页面按钮永远停在「准备归档…」—— 实测复现过。
      return await waitOffscreenReady();
    } finally {
      _offscreenPromise = null;
    }
  })();
  return _offscreenPromise;
}

// 等 offscreen 模块真正能应答 ping；超时抛错，让上层把「离屏文档未就绪」明确报给用户
async function waitOffscreenReady(timeoutMs = 4000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      const r = await chrome.runtime.sendMessage({ __cft: true, to: 'offscreen', type: 'ping', payload: {} });
      if (r && r.ready) return true;
    } catch (_) { /* 监听还没注册：继续等 */ }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error('离屏文档未就绪（模块加载超时）');
}

// ---- getArchiveConfig（探活 2.5s + 认人 + 读软件归档配置，缓存 30s） ----
let _archiveCfgCache = null;
// 显式失效：改目录/写盘失败后 30s TTL 会让扩展继续按旧目标写，光靠 TTL 不够。
// 消费方（popup 设置区 / offscreen）改完软件端配置后发 invalidateArchiveConfig 即可立刻生效。
function invalidateArchiveConfigCache(reason) {
  if (!_archiveCfgCache) return;
  _archiveCfgCache = null;
  if (reason) bgLog('info', '归档配置缓存失效: ' + reason);
}
async function handleGetArchiveConfig() {
  const now = Date.now();
  if (_archiveCfgCache && now - _archiveCfgCache.at < ARCHIVE_CONFIG_TTL_MS) {
    return _archiveCfgCache.value;
  }
  // archiveRoot=软件归档根（旧语义：落盘实际位置）。分流落地后模型本体其实在
  // <ComfyUI>\models\<类型>\ 下，这里只是附属文件与历史镜像所在的根；弹层把它当"实际落点"
  // 展示已经不再准确，真实落点要看 /api/download 回包的 dest_dir（见 submitResultMsg）。
  // downloadDir=软件下载目录（仅展示，二者可不同）
  const out = { softwareOnline: false, archiveRoot: '', channel: 'downloads', downloadDir: '', version: '', reason: 'offline' };
  try {
    const h = await bridgeFetch('/api/health', { timeoutMs: 2500 });
    // 只认「应答方确实是 CFT 且版本够」：端口上挂着别的服务或旧版 exe 一律算离线
    const health = CftTabs.cftHealth(h.httpOk ? h.data : null);
    out.version = health.version;
    out.reason = health.reason;
    if (health.ok) {
      out.softwareOnline = true;
      try {
        const c = await bridgeFetch('/api/archive/config', { timeoutMs: 5000 });
        if (c.httpOk && c.data && c.data.ok) {
          // webui.archive_config 服务端已把 archive_root 回落过 download_dir（webui.py:2916-2919
          // 与写盘用的 _archive_root() 同规则），archive_root 就是权威落盘根；
          // download_dir 只是原始下载目录，绝不能再当根目录用。
          out.archiveRoot = String(c.data.archive_root || '');
          out.downloadDir = String(c.data.download_dir || '');
        }
      } catch (e) {
        bgLog('warn', '读取软件归档配置失败', e);
      }
    } else if (health.reason !== 'offline') {
      bgLog('warn', '端口应答方不可用作 CFT: ' + health.reason + ' ' + health.version);
    }
  } catch (_) {
    // 软件离线：保持 downloads 降级
  }
  if (out.softwareOnline && out.archiveRoot) out.channel = 'software';
  _archiveCfgCache = { at: now, value: out };
  return out;
}

// ---- writeFile（通道顺序：软件 POST → downloads data:URL 降级，降级必须可见） ----
// 两种请求形态（与桌面端 browser_bridge.py 约定，两端都要向后兼容）：
//   新：{bundle, filename, data_b64} —— bundle 是 /api/download 返回的不透明落点令牌，
//       filename 必须是**纯文件名**（不含路径分隔符），目录由桌面端解析令牌决定，
//       于是图片/提示词/example.json/评论能跟模型本体进同一个嵌套目录。
//   旧：{rel_path, data_b64} —— 老桌面端、以及"桌面端判定不分流"（没有 bundle）时继续走这条，
//       行为与历史完全一致（rel_path 相对软件归档根）。
// 判定权在桌面端：扩展只负责「有令牌就用令牌」，绝不自己算分流目录（扩展看不到文件头/类型）。
function pureBundleFilename(name) {
  const s = String(name || '');
  if (!s || s === '.' || s === '..') return '';
  // 含路径分隔符就不是"纯文件名"：不送进 bundle 通道（否则桌面端 4xx，或更糟——写到令牌目录之外）
  if (/[\\/]/.test(s)) return '';
  return s;
}
async function trySoftwareWrite(target, dataB64) {
  const rawSize = Math.floor(dataB64.length * 3 / 4);
  const mb = Math.max(1, Math.ceil(rawSize / 1048576));
  const body = target.bundle
    ? { bundle: target.bundle, filename: target.filename, data_b64: dataB64 }
    : { rel_path: target.relPath, data_b64: dataB64 };
  const r = await bridgeFetch('/api/archive/file', {
    method: 'POST',
    body,
    timeoutMs: Math.min(SW_WRITE_TIMEOUT_MAX_MS, SW_WRITE_TIMEOUT_BASE_MS + mb * SW_WRITE_TIMEOUT_PER_MB_MS),
  });
  const ok = !!(r.httpOk && r.data && r.data.ok);
  // 失败原因要带出来（令牌过期/文件名非法这类 4xx 只有桌面端知道），否则降级文案只能说"写盘失败"
  const msg = ok ? '' : String((r.data && (r.data.msg || r.data.error)) || ('软件写盘失败（HTTP ' + r.status + '）'));
  return { ok, msg };
}
async function tryDownloadsWrite(relPath, dataB64) {
  const cfg = CFT.config.get() || {};
  const prefix = cfg.fallbackDirPrefix || 'CivitaiArchive';
  const filename = prefix + '/' + String(relPath).replace(/^\/+/, '');
  await chrome.downloads.download({
    url: 'data:application/octet-stream;base64,' + dataB64,
    filename,
    saveAs: false,
    conflictAction: 'overwrite',
  });
  return filename;
}
const DEGRADED_SOFTWARE_MSG = '软件归档通道写盘失败，本文件改落到浏览器下载目录，归档可能不完整';
async function handleWriteFile(payload) {
  const p = payload || {};
  const relPath = String(p.relPath || p.rel_path || '');
  const dataB64 = String(p.dataB64 || p.data_b64 || '');
  const bundle = String(p.bundle || '');
  const filename = pureBundleFilename(p.filename);
  const useBundle = !!(bundle && filename);
  // bundle 形态缺 filename（或 filename 带路径分隔符）时退回 rel_path：调用方传了 relPath 就还有
  // 老通道可用；两者都没有才是真的参数错误。
  if (useBundle ? !dataB64 : (!relPath || !dataB64)) {
    return { ok: false, saved: '', channel: 'downloads', msg: useBundle ? 'dataB64 不能为空' : 'relPath 与 dataB64 不能为空' };
  }
  const target = useBundle ? { bundle, filename, relPath } : { relPath };
  // downloads 降级时的落点名：bundle 形态理论上不依赖 relPath，但调用方一般两者都带；
  // 只带了 bundle 就用纯文件名兜底，绝不因为走了新形态就把文件丢掉。
  const downloadsRelPath = relPath || filename;
  const rawSize = Math.floor(dataB64.length * 3 / 4);
  if (rawSize > RAW_SIZE_SOFTWARE_MAX) {
    return { ok: false, saved: '', channel: 'downloads', msg: '文件过大（约 ' + (rawSize / 1048576).toFixed(1) + 'MB，超过 32MB 上限）' };
  }
  const cfg = await handleGetArchiveConfig();
  // 软件通道本该可用 → 后面任何 downloads 落盘/放弃都算「降级」，要在响应里显式暴露
  const degraded = cfg.channel === 'software';
  let softwareErrMsg = '';
  if (degraded) {
    try {
      const w = await trySoftwareWrite(target, dataB64);
      if (w.ok) {
        return { ok: true, saved: 'software', channel: 'software', msg: '' };
      }
      softwareErrMsg = w.msg;
      invalidateArchiveConfigCache('软件写盘返回失败，缓存的软件通道已不可信');
      bgLog('warn', '软件写盘返回失败，降级 downloads: ' + (useBundle ? 'bundle:' + filename : relPath) + ' — ' + w.msg);
    } catch (e) {
      softwareErrMsg = String((e && e.message) || e);
      invalidateArchiveConfigCache('软件写盘异常，缓存的软件通道已不可信');
      bgLog('warn', '软件写盘异常，降级 downloads: ' + (useBundle ? 'bundle:' + filename : relPath), e);
    }
  }
  if (rawSize > RAW_SIZE_DOWNLOADS_MAX) {
    return {
      ok: false, saved: '', channel: cfg.channel, degraded,
      msg: (degraded ? '软件写盘失败，且文件过大无法降级（约 ' : '文件过大且软件未在线（约 ') + (rawSize / 1048576).toFixed(1) + 'MB，降级上限 20MB）',
      degradedMsg: degraded ? DEGRADED_SOFTWARE_MSG + '（该文件未落任何目录）' + (softwareErrMsg ? '：' + softwareErrMsg : '') : '',
    };
  }
  try {
    const savedName = await tryDownloadsWrite(downloadsRelPath, dataB64);
    return {
      ok: true, saved: 'downloads', channel: 'downloads', msg: '', degraded,
      degradedMsg: degraded ? DEGRADED_SOFTWARE_MSG + '：' + savedName + (softwareErrMsg ? '（软件侧原因: ' + softwareErrMsg + '）' : '') : '',
    };
  } catch (e) {
    return {
      ok: false, saved: '', channel: 'downloads', degraded,
      msg: '浏览器下载失败: ' + String((e && e.message) || e),
      degradedMsg: degraded ? DEGRADED_SOFTWARE_MSG + '（浏览器下载也失败）' + (softwareErrMsg ? '：' + softwareErrMsg : '') : '',
    };
  }
}

// ---- sendModelToSoftware（模型本体转发软件下载，10s 超时 + badge） ----
// 响应除 {ok, msg} 外，还要把桌面端的落点三件套原样回传给 offscreen：
//   bundle   —— 本次模型的不透明落点令牌，后续附属文件（封面/配图/提示词/example.json/评论）
//                用它写进"模型本体真正所在的那个目录"；老桌面端没有 → 空串
//   dest_dir —— 人类可读的绝对落点（只用于展示，扩展绝不解析它来拼路径）
//   routed   —— 桌面端是否真的按类型分流了
// 三者缺失时形状保持不变（空串/false），worker 会完整退回旧的 destSubdir/dirName 行为。
function softwareRouteInfo(data) {
  const d = data || {};
  return {
    bundle: typeof d.bundle === 'string' ? d.bundle : '',
    destDir: typeof d.dest_dir === 'string' ? d.dest_dir : '',
    routed: d.routed === true,
  };
}
async function handleSendModelToSoftware(payload) {
  const url = String((payload && (payload.downloadUrl || payload.url)) || '');
  if (!url || !CIVITAI_RE.test(url)) return { ok: false, msg: '仅支持 Civitai 模型页链接', ...softwareRouteInfo(null) };
  try {
    // caps:["bundle"] 是能力协商：告诉桌面端「本扩展收得住落点令牌」。
    // 桌面端只有看到这个才允许按类型分流 —— 分流后本体进 ComfyUI 目录，附属文件必须靠令牌才
    // 能跟着走；不带 caps 的老桌面/老扩展组合下，桌面端会保持 <归档根>/<目录名> 的旧落点。
    const r = await bridgeFetch('/api/download', {
      method: 'POST', body: { url, dest_subdir: String((payload && payload.destSubdir) || ''), caps: ['bundle'] }, timeoutMs: 10000,
    });
    const route = softwareRouteInfo(r.data);
    if (r.httpOk && r.data && r.data.ok) {
      badge('✓', '#1a73e8', 3000);
      await markSubmitted(url, String((payload && payload.pageUrl) || ''));
      return { ok: true, msg: r.data.msg || '已开始下载', ...route };
    }
    badge('!', '#d93025', 3000);
    // 失败也带回 dest_dir：桌面端多半已经把任务落到某个目录了，用户看得到位置比只看"失败"有用
    return { ok: false, msg: (r.data && r.data.msg) || '下载启动失败', ...route };
  } catch (_) {
    badge('!', '#d93025', 3000);
    return { ok: false, msg: '无法连接 CivitaiFreeTool（请先打开软件）', ...softwareRouteInfo(null) };
  }
}

// ---- checkLibrary（软件离线一律 {found:false}） ----
async function handleCheckLibrary(payload) {
  const vid = String((payload && (payload.versionId || payload.version_id)) || '');
  try {
    const r = await bridgeFetch('/api/library/check?version_id=' + encodeURIComponent(vid), { timeoutMs: 8000 });
    if (r.httpOk && r.data && r.data.ok) {
      return { ok: true, found: !!r.data.found, paths: r.data.paths || [] };
    }
  } catch (_) {
    // 离线走默认
  }
  return { ok: true, found: false, paths: [] };
}

// ---- notifyArchiveDone（尽力而为，失败静默 {ok:false}） ----
async function handleNotifyArchiveDone(payload) {
  try {
    const r = await bridgeFetch('/api/archive/done', {
      method: 'POST', body: { dir_name: String((payload && payload.dirName) || '') }, timeoutMs: 8000,
    });
    return { ok: !!(r.httpOk && r.data && r.data.ok) };
  } catch (_) {
    return { ok: false };
  }
}

// ---- startArchive：取目标 tab → content 采集 → 建 offscreen → 下发 archive ----
async function resolveTargetTab(tabId) {
  if (tabId) {
    try {
      const t = await chrome.tabs.get(tabId);
      if (t) return t;
    } catch (_) {}
  }
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return (tabs && tabs[0]) || null;
}
async function handleStartArchive(payload, sendResponse) {
  let tab = null;
  try {
    tab = await resolveTargetTab(payload && payload.tabId);
  } catch (_) {
    tab = null;
  }
  if (!tab || !tab.id) {
    sendResponse({ ok: false, msg: '请先打开 Civitai 模型页' });
    return;
  }
  let collected = null;
  try {
    // content 侧 answer() 回传完整信封 {__cft,to,type,payload}，解包 payload
    const env = await chrome.tabs.sendMessage(tab.id, { __cft: true, to: 'content', type: 'collectPage', payload: {} });
    collected = (env && env.payload) ? env.payload : env;
  } catch (_) {
    collected = null;
  }
  if (!collected || !collected.modelId) {
    // 三种情形分开说，否则「已装扩展但页面早于安装」也会被同一句「请先打开模型页」误导
    let msg = '请先打开 Civitai 模型页';
    if (collected) msg = '未能从页面识别出模型 ID，请刷新该页面后重试';
    else if (CftTabs.modelLink(String(tab.url || ''))) msg = '当前模型页还未接入助手，请刷新该页面后重试';
    sendResponse({ ok: false, msg });
    return;
  }
  try {
    await ensureOffscreen();
  } catch (e) {
    bgLog('error', 'offscreen 创建失败', e);
    sendResponse({ ok: false, msg: '离屏文档创建失败' });
    return;
  }
  chrome.runtime.sendMessage({
    __cft: true, to: 'offscreen', type: 'archive',
    // pageUrl 与单图链路同一条硬约束（契约 §2.1）：offscreen 靠它定站点、决定能不能借这个 tab。
    // content 报空时用 tab.url 兜底 —— bg 手里就有这个 tab，留空只会把登录态取数降级成匿名直连。
    payload: { ...collected, tabId: tab.id, pageUrl: String(collected.pageUrl || tab.url || '') },
  }).catch(() => {});
  sendResponse({ ok: true, msg: '归档已开始' });
}

// ---- archiveBatch：批量归档（契约 8.3）。collectPage 成功用 {modelId,modelVersionId,aboutText}；
// 失败/超时/页面已关 → CftTabs.modelLink(url) 解析 {id,version} 兜底，aboutText 置空。 ----
async function collectBatchRow(tabId) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (v) => { if (!settled) { settled = true; resolve(v); } };
    const timer = setTimeout(() => done(null), ARCHIVE_BATCH_COLLECT_MS);
    chrome.tabs.sendMessage(tabId, { __cft: true, to: 'content', type: 'collectPage', payload: {} })
      .then((env) => { clearTimeout(timer); done((env && env.payload) ? env.payload : env); })
      .catch(() => { clearTimeout(timer); done(null); });
  });
}
function parseBatchRowByUrl(url) {
  const link = CftTabs.modelLink(String(url || ''));
  if (!link) return null;
  return { modelId: link.id, modelVersionId: link.version || '', aboutText: '' };
}
async function handleArchiveBatchStart(payload) {
  const rows = (payload && Array.isArray(payload.rows)) ? payload.rows : [];
  if (!rows.length || rows.length > ARCHIVE_BATCH_MAX_ROWS) {
    return { ok: false, msg: '请勾选 1–' + ARCHIVE_BATCH_MAX_ROWS + ' 个模型页' };
  }
  const items = [];
  let skipped = 0;
  for (const row of rows) {
    const tabId = row && row.tabId;
    const url = row && row.url;
    if (!tabId || !url) { skipped++; continue; }
    let collected = null;
    try { collected = await collectBatchRow(tabId); } catch (_) { collected = null; }
    let modelId = collected && collected.modelId;
    let modelVersionId = collected ? String(collected.modelVersionId || '') : '';
    let aboutText = collected ? String(collected.aboutText || '') : '';
    if (!modelId) {
      const fb = parseBatchRowByUrl(url);
      if (!fb) { skipped++; continue; }
      modelId = fb.modelId; modelVersionId = fb.modelVersionId; aboutText = '';
    }
    items.push({ tabId, url, modelId, modelVersionId, pageUrl: url, aboutText });
  }
  if (!items.length) return { ok: false, msg: '未能从勾选页面解析出有效模型页', skipped };
  try {
    await ensureOffscreen();
  } catch (e) {
    bgLog('error', 'offscreen 创建失败', e);
    return { ok: false, msg: '离屏文档创建失败', skipped };
  }
  const jobId = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  let resp = null;
  try {
    resp = await chrome.runtime.sendMessage({
      __cft: true, to: 'offscreen', type: 'archiveBatch', payload: { jobId, items },
    });
  } catch (e) {
    bgLog('error', 'archiveBatch 下发离屏失败', e);
    return { ok: false, msg: '归档批启动失败: ' + String((e && e.message) || e), skipped };
  }
  const r = (resp && typeof resp === 'object') ? resp : {};
  if (r.ok && r.started) {
    // 归档进度用全局徽章；逐标签状态徽章会盖住它，先收起，批结束再从缓存补回
    _statusBatchRunning = true;
    void clearStatusBadges();
    badge('0/' + items.length, '#1a73e8'); // 初始 badge 0/N，蓝色
  }
  return { ok: true, jobId, total: items.length, skipped, ...r };
}
// status/cancel 纯转发 offscreen，响应原样透传（契约 8.1/8.3）
async function forwardBatchControl(type, payload) {
  let resp = null;
  try {
    resp = await chrome.runtime.sendMessage({ __cft: true, to: 'offscreen', type, payload: payload || {} });
  } catch (e) {
    return { ok: false, msg: '转发离屏失败: ' + String((e && e.message) || e) };
  }
  if (type === 'archiveBatchStatus' && resp && resp.running === false && _statusBatchRunning) {
    _statusBatchRunning = false;
    scheduleStatusBadges(250);
  }
  return (resp && typeof resp === 'object') ? resp : { ok: true };
}
function handleArchiveBatchStatus(payload) { return forwardBatchControl('archiveBatchStatus', payload); }
function handleArchiveBatchCancel(payload) { return forwardBatchControl('archiveBatchCancel', payload); }
// offscreen 进度 badge：批结束发空文本，同时把背景色清回默认
function handleArchiveBadge(payload) {
  const text = String((payload && payload.text) || '');
  chrome.action.setBadgeText({ text }).catch(() => {});
  if (!text) {
    chrome.action.setBadgeBackgroundColor({ color: '#d93025' }).catch(() => {}); // 清回 Chrome 默认色
    if (_statusBatchRunning) {
      _statusBatchRunning = false;
      scheduleStatusBadges(250);       // 归档结束：把逐标签下载状态徽章补回来
    }
  }
  return { ok: true };
}

// ---- saveSingleImage：ensureOffscreen 后原样重发给 offscreen，再把应答 relay 回 content ----
// 说明：B 侧 offscreen 可能直接应答 content 的原消息；此处 relay 保证即使它只应答
// 转发消息，content 也能拿到结果（重复 sendResponse 会被浏览器忽略，无害）。
// pageUrl 是单图链路的硬约束（契约 §2.1）：offscreen 靠它判站点、决定借哪个标签页发数据请求。
// content 应当自带发起文档的 URL；缺失时用 sender.tab.url 兜底（bg 拿得到），绝不留空 ——
// 空 pageUrl 会让 offscreen 把一个站点未知的 tab 当候选借出去，跨站请求被记成"带登录态"。
function forwardSaveSingleImage(payload, sender, sendResponse) {
  const p = (payload && typeof payload === 'object') ? payload : {};
  const tab = (sender && sender.tab) || null;
  const tabId = p.tabId || (tab && tab.id) || 0;
  const pageUrl = String(p.pageUrl || (tab && tab.url) || '');
  ensureOffscreen().then(() => chrome.runtime.sendMessage({
    __cft: true, to: 'offscreen', type: 'saveSingleImage', payload: { ...p, tabId, pageUrl },
  })).then((r) => {
    try {
      sendResponse(r && typeof r === 'object' ? r : { ok: true });
    } catch (_) {}
  }).catch((e) => {
    bgLog('error', 'saveSingleImage 转发失败', e);
    try {
      sendResponse({ ok: false, msg: '图片转存失败: ' + String((e && e.message) || e) });
    } catch (_) {}
  });
}

// ---- onMessage 总线：归档信封 {__cft:true,to:'bg'} + 上游批量 cft-batch-*，互不覆盖 ----
ext.runtime.onMessage.addListener((msg, sender, respond) => {
  if (!msg) return false;
  // 归档助手信封
  if (msg.__cft === true && msg.to === 'bg') {
    const type = msg.type;
    const payload = msg.payload || {};
    (async () => {
      switch (type) {
        case 'startArchive':
          await handleStartArchive(payload, respond);
          break;
        case 'getArchiveConfig':
          respond(await handleGetArchiveConfig());
          break;
        case 'invalidateArchiveConfig': // 用户在 CFT 改了归档目录后由 UI 侧调用，立刻重探活
          invalidateArchiveConfigCache(String((payload && payload.reason) || '调用方要求失效'));
          respond({ ok: true });
          break;
        case 'writeFile':
          respond(await handleWriteFile(payload));
          break;
        case 'sendModelToSoftware':
          respond(await handleSendModelToSoftware(payload));
          break;
        case 'checkLibrary':
          respond(await handleCheckLibrary(payload));
          break;
        case 'sendContent': {
          // offscreen 文档没有 chrome.tabs，content 消息统一由这里代发（见 archive-worker.sendContent）
          const tabId = Number(payload && payload.tabId);
          if (!Number.isFinite(tabId)) { respond({ ok: false, msg: '无效的标签页' }); break; }
          try {
            respond(await chrome.tabs.sendMessage(tabId, {
              __cft: true, to: 'content',
              type: (payload && payload.type) || '', payload: (payload && payload.payload) || {},
            }));
          } catch (e) {
            respond({ ok: false, msg: String((e && e.message) || e) });
          }
          break;
        }
        case 'log':
          // offscreen 无 chrome.storage，日志由这里落到扩展存储（诊断用）
          bgLog((payload && payload.lvl) || 'info',
                '[offscreen] ' + String((payload && payload.msg) || ''),
                (payload && payload.err) || undefined);
          respond({ ok: true });
          break;
        case 'notifyArchiveDone':
          respond(await handleNotifyArchiveDone(payload));
          break;
        case 'saveSingleImage':
          forwardSaveSingleImage(payload, sender, respond);
          break;
        case 'archiveBatchStart':
          respond(await handleArchiveBatchStart(payload));
          break;
        case 'archiveBatchStatus':
          respond(await handleArchiveBatchStatus(payload));
          break;
        case 'archiveBatchCancel':
          respond(await handleArchiveBatchCancel(payload));
          break;
        case 'archiveBadge':
          respond(handleArchiveBadge(payload));
          break;
        case 'modelStatusQuery':
          respond(await handleModelStatusQuery(payload, sender));
          break;
        case 'modelStatusCache':
          respond(await handleModelStatusCache(payload));
          break;
        case 'checkModelUpdates':
          respond(sender && sender.id === ext.runtime.id
            ? await handleCheckModelUpdates(payload)
            : { ok: false, msg: '拒绝来源不明的检查请求' });
          break;
        case 'modelUpdateState':
          respond(sender && sender.id === ext.runtime.id
            ? await handleModelUpdateState()
            : { ok: false, msg: '拒绝来源不明的查询' });
          break;
        case 'revealPath':
          respond(sender && sender.id === ext.runtime.id
            ? await handleRevealPath(payload)
            : { ok: false, msg: '拒绝来源不明的打开请求' });
          break;
        case 'notifyTaskDone':
          respond(sender && sender.id === ext.runtime.id
            ? await notifyTaskDone(payload)
            : { ok: false, msg: '拒绝来源不明的通知请求' });
          break;
        default:
          respond({ ok: false, msg: '未知消息类型: ' + type });
      }
    })().catch((e) => {
      bgLog('error', 'bg 消息处理异常: ' + type, e);
      try {
        respond({ ok: false, msg: String((e && e.message) || e) });
      } catch (_) {}
    });
    return true;
  }
  // 上游 v1.3.0 批量提交消息
  if (sender.id === ext.runtime.id && ['cft-batch-start', 'cft-batch-status', 'cft-batch-clear'].includes(msg.type)) {
    (async () => {
      await ready;
      if (msg.type === 'cft-batch-clear') {
        if (starting || job?.running) return { ok: false, msg: '提交中不能清除记录' };
        job = null; await save(); badge('', '#0a84ff');
        return { ok: true, job };
      }
      return msg.type === 'cft-batch-start' ? start(msg.urls) : { ok: true, job };
    })()
      .then(respond, () => respond({ ok: false, msg: '扩展状态读取失败，请重新加载扩展' }));
    return true;
  }
  return false;
});