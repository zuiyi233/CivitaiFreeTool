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
import './tabs-common.js';

const ext = typeof browser !== 'undefined' ? browser : chrome;
const BRIDGE = 'http://127.0.0.1:47531';
const CIVITAI_RE = /^https?:\/\/(www\.)?civitai\.(red|com)\/(api\/download\/)?models\//i;
const OFFSCREEN_URL = 'offscreen/archive-worker.html';
const RAW_SIZE_SOFTWARE_MAX = 32 * 1024 * 1024; // 软件通道原始大小上限
const RAW_SIZE_DOWNLOADS_MAX = 20 * 1024 * 1024; // downloads 降级上限
const ARCHIVE_CONFIG_TTL_MS = 30 * 1000; // getArchiveConfig 缓存 30s
const ARCHIVE_BATCH_MAX_ROWS = 500; // 批量归档上限（契约 8.1）
const ARCHIVE_BATCH_COLLECT_MS = 5000; // 逐页 collectPage 超时（契约 8.3）
const STORE = 'lastBatch';

// 批量提交状态（上游 v1.3.0）
let job = null, starting = false;

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
    const res = await fetch(BRIDGE + '/api/download', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }), signal: ctrl.signal });
    const data = await res.json();
    return res.ok && data.ok ? { status: 'sent', msg: '已提交，在 CFT 查看解析结果' } : { status: 'failed', msg: data.msg || 'CFT 未接受请求' };
  } catch (_) {
    // The server may have accepted it before the response was lost. Never retry implicitly.
    return { status: 'uncertain', msg: '未收到确认，请在 CFT 核对后再发送' };
  } finally { clearTimeout(timer); }
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

// 右键菜单：页面 / 链接 → 批量提交入口（上游 v1.3.0）
ext.runtime.onInstalled.addListener(() => {
  ext.contextMenus.removeAll(() => ext.contextMenus.create({ id: 'cft-dl', title: '发送到 CivitaiFreeTool', contexts: ['page', 'link'] }));
});
ext.contextMenus.onClicked.addListener(info => {
  if (info.menuItemId !== 'cft-dl') return;
  start([info.linkUrl || info.pageUrl]).then(r => { if (!r.ok) { badge('!', '#c23d50'); ext.action.setTitle({ title: r.msg }); } }).catch(() => badge('!', '#c23d50'));
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
      if (await hasOffscreen()) return true;
      try {
        await chrome.offscreen.createDocument({
          url: OFFSCREEN_URL,
          reasons: ['DOM_PARSER'],
          justification: '归档长任务需要 DOM 解析与持续网络/写盘，SW 无 DOM 且会被休眠',
        });
      } catch (e) {
        if (await hasOffscreen()) return true; // 已存在竞态（并发/残留）：视为成功
        throw e;
      }
      return true;
    } finally {
      _offscreenPromise = null;
    }
  })();
  return _offscreenPromise;
}

// ---- getArchiveConfig（探活 2.5s + 读软件归档配置，缓存 30s） ----
let _archiveCfgCache = null;
async function handleGetArchiveConfig() {
  const now = Date.now();
  if (_archiveCfgCache && now - _archiveCfgCache.at < ARCHIVE_CONFIG_TTL_MS) {
    return _archiveCfgCache.value;
  }
  const out = { softwareOnline: false, archiveRoot: '', channel: 'downloads' };
  try {
    const h = await bridgeFetch('/api/health', { timeoutMs: 2500 });
    if (h.httpOk && h.data && h.data.ok) {
      out.softwareOnline = true;
      try {
        const c = await bridgeFetch('/api/archive/config', { timeoutMs: 5000 });
        if (c.httpOk && c.data && c.data.ok) {
          out.archiveRoot = c.data.archive_root || c.data.download_dir || '';
        }
      } catch (e) {
        bgLog('warn', '读取软件归档配置失败', e);
      }
    }
  } catch (_) {
    // 软件离线：保持 downloads 降级
  }
  if (out.softwareOnline && out.archiveRoot) out.channel = 'software';
  _archiveCfgCache = { at: now, value: out };
  return out;
}

// ---- writeFile（通道顺序：软件 POST → downloads data:URL 降级） ----
async function trySoftwareWrite(relPath, dataB64) {
  const r = await bridgeFetch('/api/archive/file', {
    method: 'POST',
    body: { rel_path: relPath, data_b64: dataB64 },
    timeoutMs: 15000,
  });
  return !!(r.httpOk && r.data && r.data.ok);
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
async function handleWriteFile(payload) {
  const p = payload || {};
  const relPath = String(p.relPath || p.rel_path || '');
  const dataB64 = String(p.dataB64 || p.data_b64 || '');
  if (!relPath || !dataB64) return { ok: false, saved: '', channel: 'downloads', msg: 'relPath 与 dataB64 不能为空' };
  const rawSize = Math.floor(dataB64.length * 3 / 4);
  if (rawSize > RAW_SIZE_SOFTWARE_MAX) {
    return { ok: false, saved: '', channel: 'downloads', msg: '文件过大（约 ' + (rawSize / 1048576).toFixed(1) + 'MB，超过 32MB 上限）' };
  }
  const cfg = await handleGetArchiveConfig();
  if (cfg.channel === 'software') {
    try {
      if (await trySoftwareWrite(relPath, dataB64)) {
        return { ok: true, saved: 'software', channel: 'software', msg: '' };
      }
      bgLog('warn', '软件写盘返回失败，降级 downloads: ' + relPath);
    } catch (e) {
      bgLog('warn', '软件写盘异常，降级 downloads: ' + relPath, e);
    }
  }
  if (rawSize > RAW_SIZE_DOWNLOADS_MAX) {
    return { ok: false, saved: '', channel: cfg.channel, msg: '文件过大且软件未在线（约 ' + (rawSize / 1048576).toFixed(1) + 'MB，降级上限 20MB）' };
  }
  try {
    await tryDownloadsWrite(relPath, dataB64);
    return { ok: true, saved: 'downloads', channel: 'downloads', msg: '' };
  } catch (e) {
    return { ok: false, saved: '', channel: 'downloads', msg: '浏览器下载失败: ' + String((e && e.message) || e) };
  }
}

// ---- sendModelToSoftware（模型本体转发软件下载，10s 超时 + badge） ----
async function handleSendModelToSoftware(payload) {
  const url = String((payload && (payload.downloadUrl || payload.url)) || '');
  if (!url || !CIVITAI_RE.test(url)) return { ok: false, msg: '仅支持 Civitai 模型页链接' };
  try {
    const r = await bridgeFetch('/api/download', {
      method: 'POST', body: { url, dest_subdir: String((payload && payload.destSubdir) || '') }, timeoutMs: 10000,
    });
    if (r.httpOk && r.data && r.data.ok) {
      badge('✓', '#1a73e8', 3000);
      return { ok: true, msg: r.data.msg || '已开始下载' };
    }
    badge('!', '#d93025', 3000);
    return { ok: false, msg: (r.data && r.data.msg) || '下载启动失败' };
  } catch (_) {
    badge('!', '#d93025', 3000);
    return { ok: false, msg: '无法连接 CivitaiFreeTool（请先打开软件）' };
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
    sendResponse({ ok: false, msg: '请先打开 Civitai 模型页' });
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
    payload: { ...collected, tabId: tab.id },
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
  if (r.ok && r.started) badge('0/' + items.length, '#1a73e8'); // 初始 badge 0/N，蓝色
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
  return (resp && typeof resp === 'object') ? resp : { ok: true };
}
function handleArchiveBatchStatus(payload) { return forwardBatchControl('archiveBatchStatus', payload); }
function handleArchiveBatchCancel(payload) { return forwardBatchControl('archiveBatchCancel', payload); }
// offscreen 进度 badge：批结束发空文本，同时把背景色清回默认
function handleArchiveBadge(payload) {
  const text = String((payload && payload.text) || '');
  chrome.action.setBadgeText({ text }).catch(() => {});
  if (!text) chrome.action.setBadgeBackgroundColor({ color: '#d93025' }).catch(() => {}); // 清回 Chrome 默认色
  return { ok: true };
}

// ---- saveSingleImage：ensureOffscreen 后原样重发给 offscreen，再把应答 relay 回 content ----
// 说明：B 侧 offscreen 可能直接应答 content 的原消息；此处 relay 保证即使它只应答
// 转发消息，content 也能拿到结果（重复 sendResponse 会被浏览器忽略，无害）。
function forwardSaveSingleImage(payload, sender, sendResponse) {
  const tabId = (payload && payload.tabId) || (sender && sender.tab && sender.tab.id) || 0;
  ensureOffscreen().then(() => chrome.runtime.sendMessage({
    __cft: true, to: 'offscreen', type: 'saveSingleImage', payload: { ...(payload || {}), tabId },
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
        case 'writeFile':
          respond(await handleWriteFile(payload));
          break;
        case 'sendModelToSoftware':
          respond(await handleSendModelToSoftware(payload));
          break;
        case 'checkLibrary':
          respond(await handleCheckLibrary(payload));
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