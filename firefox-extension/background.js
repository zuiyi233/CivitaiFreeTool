/* Requests run in the background, not in the short-lived popup. */
if (typeof importScripts === 'function') {
  importScripts('tabs-common.js');
  importScripts('lib/tab-index.js');
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

function ensureTabIndexAlarm() {
  if (!ext.alarms) return;
  try { ext.alarms.create(TAB_INDEX_ALARM, {periodInMinutes: 1}); } catch (_) {}
}
if (ext.alarms && ext.alarms.onAlarm) {
  ext.alarms.onAlarm.addListener((alarm) => {
    if (alarm && alarm.name === TAB_INDEX_ALARM) void pushTabIndex();
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
  }
});
for (const name of ['onCreated', 'onActivated', 'onMoved', 'onAttached', 'onDetached']) {
  try { ext.tabs[name].addListener(() => scheduleTabIndexPush(TAB_INDEX_DEBOUNCE_MS)); } catch (_) {}
}
ext.tabs.onRemoved.addListener(() => scheduleTabIndexPush(300));
ensureTabIndexAlarm();
scheduleTabIndexPush(1200);
ext.runtime.onStartup.addListener(() => {
  void rememberOpenTabs();
  ensureTabIndexAlarm();
  scheduleTabIndexPush(800);
});
ext.runtime.onInstalled.addListener(() => {
  void rememberOpenTabs();
  ensureTabIndexAlarm();
  scheduleTabIndexPush(800);
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
