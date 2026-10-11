// background.js —— SW 消息路由 + offscreen 管理 + 写盘通道 + bridge 客户端（MV3 module）
// 保留旧逻辑：右键菜单 "cft-dl" → downloadUrl（badge 反馈）原样不动。
import './lib/util.js';
import './lib/civitai-api.js';
import './lib/scheduler.js';
import './lib/idb.js';
import './lib/history.js';
import './lib/config.js';
import './lib/log.js';

const BRIDGE = 'http://127.0.0.1:47531';
const CIVITAI_RE = /^https?:\/\/(www\.)?civitai\.(red|com)\/(api\/download\/)?models\//i;
const OFFSCREEN_URL = 'offscreen/archive-worker.html';
const RAW_SIZE_SOFTWARE_MAX = 32 * 1024 * 1024; // 软件通道原始大小上限
const RAW_SIZE_DOWNLOADS_MAX = 20 * 1024 * 1024; // downloads 降级上限
const ARCHIVE_CONFIG_TTL_MS = 30 * 1000; // getArchiveConfig 缓存 30s

function bgLog(lvl, msg, err) {
  try { CFT.log.log(lvl, 'bg', msg, err); } catch (_) {}
}
CFT.config.load().then(() => bgLog('info', 'background 启动，配置已加载')).catch(() => {});

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

// ---- 旧逻辑保留：badge / postDownload / downloadUrl ----
function badge(text, color, ms) {
  try {
    chrome.action.setBadgeText({ text });
    chrome.action.setBadgeBackgroundColor({ color });
    setTimeout(() => chrome.action.setBadgeText({ text: '' }), ms || 3000);
  } catch (_) {}
}

async function postDownload(url) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 6000);
  try {
    const res = await fetch(BRIDGE + '/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
      signal: ctrl.signal,
    });
    const data = await res.json().catch(() => ({}));
    return { httpOk: res.ok, ...data };
  } catch (e) {
    return { httpOk: false, msg: '无法连接 CivitaiFreeTool（请先打开软件）' };
  } finally {
    clearTimeout(timer);
  }
}

async function downloadUrl(url) {
  if (!url || !CIVITAI_RE.test(url)) {
    badge('!', '#d93025', 3000);
    chrome.action.setTitle({ title: '当前页面不是 Civitai 模型页' });
    return;
  }
  const r = await postDownload(url);
  if (r.httpOk && r.ok) {
    badge('✓', '#1a73e8', 3000);
    chrome.action.setTitle({ title: '已开始下载，可在 CivitaiFreeTool 下载管理查看' });
  } else if (r.httpOk && !r.ok) {
    badge('!', '#d93025', 3000);
    chrome.action.setTitle({ title: r.msg || '下载启动失败' });
  } else {
    badge('!', '#d93025', 3000);
    chrome.action.setTitle({ title: r.msg || '无法连接 CivitaiFreeTool' });
  }
}

// 右键菜单：页面 / 链接上「一键下载到 CivitaiFreeTool」（旧逻辑保留）
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: 'cft-dl',
    title: '一键下载到 CivitaiFreeTool',
    contexts: ['page', 'link'],
  });
});
chrome.contextMenus.onClicked.addListener((info) => {
  downloadUrl(info.linkUrl || info.pageUrl);
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

// ---- onMessage 总线：只处理 to==='bg'，其余放行 ----
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.__cft !== true) return false;
  if (msg.to !== 'bg') return false;
  const type = msg.type;
  const payload = msg.payload || {};
  (async () => {
    switch (type) {
      case 'startArchive':
        await handleStartArchive(payload, sendResponse);
        break;
      case 'getArchiveConfig':
        sendResponse(await handleGetArchiveConfig());
        break;
      case 'writeFile':
        sendResponse(await handleWriteFile(payload));
        break;
      case 'sendModelToSoftware':
        sendResponse(await handleSendModelToSoftware(payload));
        break;
      case 'checkLibrary':
        sendResponse(await handleCheckLibrary(payload));
        break;
      case 'notifyArchiveDone':
        sendResponse(await handleNotifyArchiveDone(payload));
        break;
      case 'saveSingleImage':
        forwardSaveSingleImage(payload, sender, sendResponse);
        break;
      default:
        sendResponse({ ok: false, msg: '未知消息类型: ' + type });
    }
  })().catch((e) => {
    bgLog('error', 'bg 消息处理异常: ' + type, e);
    try {
      sendResponse({ ok: false, msg: String((e && e.message) || e) });
    } catch (_) {}
  });
  return true;
});
