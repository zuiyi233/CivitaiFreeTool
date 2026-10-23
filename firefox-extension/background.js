/* Requests run in the background, not in the short-lived popup. */
if (typeof importScripts === 'function') {
  importScripts('tabs-common.js');
  importScripts('lib/tab-index.js');
  importScripts('lib/model-status.js');
}
const ext = typeof browser !== 'undefined' ? browser : chrome;
const BRIDGE = 'http://127.0.0.1:47531';
const TAB_INDEX_ALARM = 'cftTabIndex';
const TAB_INDEX_DEBOUNCE_MS = 1200;
const DEV_RELOAD_SEEN_KEY = 'cftDevReloadSeenV1';

// ---- 打开的模型页索引：只读上报给本机桥（契约见 docs/browser-tabs-bridge.md） ----
let tabIndexTimer = null, tabIndexInFlight = false, tabIndexAgain = false;
function scheduleTabIndexPush(delay) {
  if (tabIndexTimer) clearTimeout(tabIndexTimer);
  tabIndexTimer = setTimeout(() => {
    tabIndexTimer = null;
    void pushTabIndex();
  }, Math.max(150, Number(delay) || TAB_INDEX_DEBOUNCE_MS));
}
async function pushTabIndex() {
  const index = globalThis.CftTabIndex;
  if (!index || typeof index.buildPayload !== 'function') return;
  if (tabIndexInFlight) { tabIndexAgain = true; return; }
  tabIndexInFlight = true;
  try {
    const tabs = await ext.tabs.query({});
    const payload = index.buildPayload(tabs, {
      source: 'firefox-extension', extVersion: extensionVersion(),
    });
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 5000);
    try {
      const res = await fetch(BRIDGE + '/api/ext/tabs', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload), signal: ctrl.signal,
      });
      const data = await res.json().catch(() => ({}));
      await maybeDevReload(data);
    } finally {
      clearTimeout(timer);
    }
  } catch (_) {
    // 桌面端未启动/端口未开：下次标签页事件或心跳再试；不要把 URL 写进日志
  } finally {
    tabIndexInFlight = false;
    if (tabIndexAgain) { tabIndexAgain = false; scheduleTabIndexPush(300); }
  }
}
function extensionVersion() {
  try {
    const manifest = ext.runtime && typeof ext.runtime.getManifest === 'function'
      ? ext.runtime.getManifest() : null;
    const version = manifest && manifest.version ? String(manifest.version) : '';
    return /^[0-9A-Za-z][0-9A-Za-z._-]{0,31}$/.test(version) ? version : '';
  } catch (_) { return ''; }
}

// 开发态重载：只认桥回传的新 dev_reload_at；先写 storage 再 reload，避免循环。
async function maybeDevReload(data) {
  const at = Number(data && data.dev_reload_at) || 0;
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

// ---- 免点击自动显示下载状态（与 Chrome 版共用同一份 lib/model-status.js） ----
// 缺了这一步，状态只有"打开小窗"才会填：用户看到的就是"徽章是空的、必须点图标"。
// Firefox 版不注入网页（manifest 里没有 content_scripts，也不申请 scripting），
// 所以这里只做到逐标签徽章；页面胶囊在 Firefox 确实做不到，README 里如实写明。
const MS = (globalThis.CFT && globalThis.CFT.modelStatus) || null;
const STATUS_KEY = MS ? MS.CACHE_KEY : 'cftModelStatusV1';
const STATUS_HEARTBEAT_ALARM = 'cft-status-heartbeat';
const STATUS_TAB_LIMIT = 60;   // 一次最多查这么多标签；桥侧上限 500，这里先按实际用量收口
let _statusRefreshInFlight = null;

function ensureStatusAlarm() {
  if (!ext.alarms) return;
  try { ext.alarms.create(STATUS_HEARTBEAT_ALARM, {periodInMinutes: 2}); } catch (_) {}
}

function collectOpenModelTabs() {
  if (!MS) return Promise.resolve([]);
  return ext.tabs.query({}).then((tabs) => {
    const out = [];
    for (const tab of tabs || []) {
      if (!tab || tab.discarded || !Number.isFinite(Number(tab.id))) continue;
      const link = CftTabs.modelLink(tab.url || '');
      if (!link || !link.id) continue;
      out.push({
        tabId: tab.id,
        modelId: link.id,
        versionId: link.version || '',
        key: MS.keyOf(link.id, link.version || ''),
        // 发清洗过的链接，不发标签页原始 URL：原始地址里可能带着 token 之类的追踪参数
        url: link.url || '',
      });
      if (out.length >= STATUS_TAB_LIMIT) break;
    }
    return out;
  }).catch(() => []);
}

async function readStatusCache() {
  if (!MS) return null;
  try {
    const raw = await ext.storage.local.get(STATUS_KEY);
    return MS.sanitizeCache(raw ? raw[STATUS_KEY] : null);
  } catch (_) { return MS.sanitizeCache(null); }
}

// 本机桥失败要区分「没开 CFT」和「CFT 太旧没有这个端点」：两者动作不同。
// 请求体只有模型/版本 ID 与清洗过的页面 URL，没有 Cookie、没有 API Key、没有正文。
async function queryBridgeStatus(items) {
  let r;
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => { try { ctrl.abort(); } catch (_) {} }, 6000);
    try {
      const res = await fetch(BRIDGE + '/api/model/status', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          items: items.map((i) => ({model_id: i.modelId, version_id: i.versionId, url: i.url})),
          include_updates: true,
        }),
        signal: ctrl.signal,
      });
      r = {httpOk: res.ok, status: res.status, data: await res.json().catch(() => ({}) )};
    } finally { clearTimeout(timer); }
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

// 与 Chrome 版同一条口径：桥没开着（或桌面端太旧）时，离线占位不盖掉上次已知状态，
// 徽章继续显示缓存里那一版结论，只额外带上「离线」说明。
function entryForItem(it, result, cache) {
  const key = MS.keyOf(it.modelId, it.versionId) || it.key;
  const fresh = (result && result.byKey && (result.byKey[key] || result.byKey[it.key])) || null;
  const cached = MS.getEntry(cache, it.modelId, it.versionId);
  const degraded = !result || result.ok === false || result.offline === true;
  if (cached && degraded && fresh) return Object.assign({}, cached, {offline: true});
  return fresh || cached || null;
}

async function applyBadge(tabId, entry, offline) {
  if (!MS || !Number.isFinite(Number(tabId))) return;
  const lab = entry ? MS.labelOf(entry, {offline: offline === true || entry.offline === true}) : null;
  try {
    await ext.action.setBadgeText({tabId, text: lab ? lab.badge : ''});
    if (lab) await ext.action.setBadgeBackgroundColor({tabId, color: lab.color});
    // Firefox 才有 setBadgeTitle：徽章悬浮说明只挂在对应标签上，不改全局按钮文案
    if (lab && ext.action.setBadgeTitle) {
      await ext.action.setBadgeTitle({tabId, title: lab.label + (lab.detail ? ' · ' + lab.detail : '') + '\n' + lab.title});
    }
  } catch (_) { /* 标签已关 / 该宿主不支持逐标签徽章：跳过，不影响其它标签 */ }
}

// 启动、更新、心跳、标签页抖动常常挤在同几秒里：同一批标签只打一次桥（与 Chrome 同一个窗口）。
const STATUS_DEDUPE_MS = 1500;
const _statusInflight = new Map();
function queryStatusOnce(items) {
  const sig = items.map((i) => i.key + ':' + i.modelId + ':' + i.versionId).sort().join('|');
  const now = Date.now();
  const hit = _statusInflight.get(sig);
  if (hit && now - hit.at < STATUS_DEDUPE_MS) return hit.promise;
  const promise = queryBridgeStatus(items).finally(() => {
    setTimeout(() => {
      const cur = _statusInflight.get(sig);
      if (cur && cur.promise === promise) _statusInflight.delete(sig);
    }, STATUS_DEDUPE_MS);
  });
  _statusInflight.set(sig, {at: now, promise});
  return promise;
}

function refreshOpenTabsStatus(reason) {
  if (!MS) return Promise.resolve({ ok: false, count: 0, reason: reason || '', error: '状态模块未加载' });
  if (_statusRefreshInFlight) return _statusRefreshInFlight;
  return collectOpenModelTabs().then((items) => {
    if (!items.length) return { ok: true, count: 0, reason: reason || '' };
    const run = readStatusCache().then((cache) => queryStatusOnce(items).then((result) => {
      const offline = !!(result && result.offline);
      return Promise.all(items.map((it) => applyBadge(it.tabId, entryForItem(it, result, cache), offline)))
        .then(() => ({ ok: true, count: items.length, reason: reason || '' }));
    }));
    _statusRefreshInFlight = run.catch(() => ({ ok: false, count: items.length, reason: reason || '' }))
      .then((out) => { _statusRefreshInFlight = null; return out; });
    return _statusRefreshInFlight;
  });
}

// 新开的模型页不必等下一次心跳才有徽章：先用缓存里那一版结论点上，不打网络。
// 缓存里没有就保持空徽章（"没查过"不等于"未下载"），心跳会把它补上。
function applyCachedBadge(tabId, url) {
  if (!MS || !Number.isFinite(Number(tabId))) return Promise.resolve();
  const link = CftTabs.modelLink(url || '');
  if (!link || !link.id) return Promise.resolve();
  return readStatusCache().then((cache) => applyBadge(tabId, MS.getEntry(cache, link.id, link.version || '')))
    .catch(() => {});
}

// SW/事件页每次醒来都把心跳登记好，并判断"这是不是一次冷启动"。
// 只挂在 onInstalled/onStartup 上不够：chrome.runtime.reload()（开发态重载就是它）之后
// 浏览器不一定再把 onInstalled 交给新的后台，只等事件把它唤醒——那条链不跑，徽章就一直空着。
const BOOT_WAKE_KEY = 'cftBootWakeAtV1';
const BOOT_WAKE_MIN_MS = 90 * 1000;   // 90 秒内的重复唤醒不补整轮
function bootWake() {
  if (!MS) return Promise.resolve({ok: false, reason: 'boot', error: '状态模块未加载'});
  ensureStatusAlarm();
  const probeAlarm = ext.alarms && typeof ext.alarms.getAll === 'function'
    ? ext.alarms.getAll().then((all) => (all || []).some((a) => a && a.name === STATUS_HEARTBEAT_ALARM)).catch(() => true)
    : Promise.resolve(true);
  return Promise.all([probeAlarm, ext.storage.local.get(BOOT_WAKE_KEY)]).then(([hasAlarm, saved]) => {
    const now = Date.now();
    const last = Number(saved && saved[BOOT_WAKE_KEY]) || 0;
    if (hasAlarm && now - last < BOOT_WAKE_MIN_MS) return {ok: true, skipped: true};
    try { ext.storage.local.set({[BOOT_WAKE_KEY]: now}); } catch (_) { /* 写不进去也要继续补 */ }
    return refreshOpenTabsStatus('boot');
  }).catch(() => ({ok: false}));
}

function ensureTabIndexAlarm() {
  if (!ext.alarms) return;
  try { ext.alarms.create(TAB_INDEX_ALARM, {periodInMinutes: 1}); } catch (_) {}
}
if (ext.alarms && ext.alarms.onAlarm) {
  ext.alarms.onAlarm.addListener((alarm) => {
    if (!alarm) return;
    if (alarm.name === TAB_INDEX_ALARM) void pushTabIndex();
    else if (alarm.name === STATUS_HEARTBEAT_ALARM) void refreshOpenTabsStatus('heartbeat');
  });
}
const STORE = 'lastBatch';
let job = null, starting = false;
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
function badge(text, color) {
  ext.action.setBadgeText({text}).catch(() => {});
  ext.action.setBadgeBackgroundColor({color}).catch(() => {});
}
async function save() { await ext.storage.local.set({[STORE]:job}); }

// ---- 页面级历史：只记 C 站模型页 URL/标题/时间，仅本地（与 Chrome 版同 key/同去重） ----
const HISTORY_KEY = 'cftPageHistoryV1';
const HISTORY_MAX = 500;
const _visitSeen = new Map();
function historyKeyOf(link) {
  if (!link) return '';
  return link.version ? 'version:' + link.version : (link.id ? 'model:' + link.id : '');
}
async function readPageHistory() {
  try {
    const raw = (await ext.storage.local.get(HISTORY_KEY))[HISTORY_KEY];
    return {version:1, savedAt:Number(raw && raw.savedAt) || 0,
      items:(raw && raw.items && typeof raw.items === 'object') ? Object.assign({}, raw.items) : {}};
  } catch (_) { return {version:1, savedAt:0, items:{}}; }
}
async function rememberVisit(tab) {
  try {
    if (!tab || !tab.url) return;
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
    h.items[key] = {key, modelId:link.id, versionId:link.version || '', url:link.url,
      title:CftTabs.cleanTitle(tab.title), firstAt:Number(prev.firstAt) || now,
      lastAt:now, count:(Number(prev.count) || 0) + 1};
    const keys = Object.keys(h.items);
    if (keys.length > HISTORY_MAX) {
      keys.sort((a,b) => (Number(h.items[b].lastAt) || 0) - (Number(h.items[a].lastAt) || 0));
      for (const k of keys.slice(HISTORY_MAX)) delete h.items[k];
    }
    h.savedAt = now;
    await ext.storage.local.set({[HISTORY_KEY]:h});
  } catch (_) { /* 历史失败不影响发送 */ }
}
async function rememberOpenTabs() {
  try {
    const tabs = await ext.tabs.query({});
    for (const tab of tabs) await rememberVisit(tab);
  } catch (_) {}
}
ext.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (!changeInfo) return;
  scheduleTabIndexPush(TAB_INDEX_DEBOUNCE_MS);
  if (changeInfo.url !== undefined || changeInfo.status === 'complete') {
    void rememberVisit(tab || {id:tabId, url:changeInfo.url, title:''});
    void applyCachedBadge(tabId, (tab && tab.url) || changeInfo.url || '');
  }
});
for (const name of ['onCreated', 'onActivated', 'onMoved', 'onAttached', 'onDetached']) {
  try { ext.tabs[name].addListener(() => scheduleTabIndexPush(TAB_INDEX_DEBOUNCE_MS)); } catch (_) {}
}
ext.tabs.onRemoved.addListener(() => scheduleTabIndexPush(300));
ensureTabIndexAlarm();
scheduleTabIndexPush(1200);
void bootWake();
ext.runtime.onStartup.addListener(() => {
  void rememberOpenTabs();
  ensureTabIndexAlarm();
  scheduleTabIndexPush(800);
  ensureStatusAlarm();
  void refreshOpenTabsStatus('startup');
});
ext.runtime.onInstalled.addListener(() => {
  void rememberOpenTabs();
  ensureTabIndexAlarm();
  scheduleTabIndexPush(800);
  ensureStatusAlarm();
  void refreshOpenTabsStatus('installed');
});

async function post(url) {
  const ctrl = new AbortController(), timer = setTimeout(() => ctrl.abort(), 12000);
  try {
    const res = await fetch(BRIDGE + '/api/download', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url}), signal:ctrl.signal});
    const data = await res.json();
    if (!res.ok || !data.ok) return {status:'failed', msg:data.msg || 'CFT 未接受请求'};
    return {status:'sent', msg:submitResultMsg(data)};
  } catch (_) {
    // The server may have accepted it before the response was lost. Never retry implicitly.
    return {status:'uncertain', msg:'未收到确认，请在 CFT 核对后再发送'};
  } finally { clearTimeout(timer); }
}
// 落点回显（与 Chrome 版同规则）：新桌面端会在 /api/download 响应里带 dest_dir（人类可读绝对
// 落点）与 routed（是否已按模型类型分流），列表行必须把它显示出来，否则用户提交完就找不到文件了。
// 两个字段都没有＝老桌面端，文案逐字保持原样。本扩展（Firefox）没有写盘通道，故只回显路径、
// 不消费桌面端的落点令牌，能力边界不变。
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
      if (row.status === 'sending') {row.status = 'uncertain'; row.msg = '提交中断，请在 CFT 核对';}
      else if (row.status === 'waiting') {row.status = 'failed'; row.msg = '尚未提交';}
    }
  } finally {
    job.running = false; await save();
    const sent = job.items.filter(r => r.status === 'sent').length;
    badge(sent === job.items.length ? '✓' : '!', sent === job.items.length ? '#0a84ff' : '#c23d50');
    ext.action.setTitle({title:`已提交 ${sent}/${job.items.length}，点击图标查看结果`}).catch(() => {});
  }
}
async function start(urls) {
  await ready;
  if (starting || job?.running) return {ok:false, msg:'上一批正在提交，请稍候', job};
  starting = true;
  try {
    if (!Array.isArray(urls) || !urls.length || urls.length > 500) return {ok:false, msg:'请勾选 1–500 个模型页'};
    const links = urls.map(CftTabs.modelLink);
    if (links.some(v => !v)) return {ok:false, msg:'含无效模型链接，未发送任何页面'};
    const completedKeys = [...new Set([...(job?.completedKeys || []),...(job?.items || []).filter(r=>['sent','uncertain'].includes(r.status)).map(r=>r.key)])];
    const items = [...new Map(links.map(v => [v.key, {...v,status:'waiting',msg:''}])).values()].filter(r=>!completedKeys.includes(r.key));
    if(!items.length)return {ok:false,msg:'这些页面已提交或待核对；如确需再次发送，请先清除提交记录'};
    job = {id:Date.now().toString(), running:true, completedKeys, items};
    await save();
    void run();
    return {ok:true,job};
  } finally {starting = false;}
}
ext.runtime.onMessage.addListener((msg, sender, respond) => {
  if (sender.id !== ext.runtime.id || !['cft-batch-start','cft-batch-status','cft-batch-clear'].includes(msg?.type)) return false;
  (async () => {await ready;if(msg.type==='cft-batch-clear'){if(starting||job?.running)return {ok:false,msg:'提交中不能清除记录'};job=null;await save();badge('','#0a84ff');return {ok:true,job};}return msg.type === 'cft-batch-start' ? start(msg.urls) : {ok:true,job};})()
    .then(respond, () => respond({ok:false,msg:'扩展状态读取失败，请重新加载扩展'}));
  return true;
});
ext.runtime.onInstalled.addListener(() => {
  ext.contextMenus.removeAll(() => ext.contextMenus.create({id:'cft-dl', title:'发送到 CivitaiFreeTool', contexts:['page','link']}));
});
ext.contextMenus.onClicked.addListener(info => {
  if (info.menuItemId !== 'cft-dl') return;
  start([info.linkUrl || info.pageUrl]).then(r => {if (!r.ok) {badge('!','#c23d50');ext.action.setTitle({title:r.msg});}}).catch(() => badge('!','#c23d50'));
});
