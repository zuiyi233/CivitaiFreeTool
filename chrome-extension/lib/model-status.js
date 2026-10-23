// CFT.modelStatus —— 「这个模型/版本在本机到底有没有、是不是在下载、有没有新版」的纯逻辑层。
//
// 数据源只有一个：桌面端 POST /api/model/status（见 docs/extension-model-status.md）。
// 本模块不碰 chrome.* / document / window，负责：
//   1) 把桌面返回的条目归一化成扩展内部 entry（含 checkedAt / seenUrls / offline）；
//   2) 标签、徽章、汇总、筛选口径（页面胶囊 / popup / Firefox 共用同一份文案）；
//   3) chrome.storage 缓存的合并与过期判定（storage 读写在调用方，本模块只算数据）；
//   4) tab 行 → 批量请求项的构造。
//
// 约束：顶层不调用任何浏览器 API；node 下可直接 require/`vm` 加载。
globalThis.CFT = globalThis.CFT || {};

(function (root) {
  'use strict';

  const CACHE_KEY = 'cftModelStatusV1';
  const CACHE_VERSION = 1;
  const MAX_ENTRIES = 1000;
  const MAX_SEEN_URLS = 5;
  const TTL_DOWNLOADING_MS = 2 * 60 * 1000;
  const TTL_DEFAULT_MS = 5 * 60 * 1000;

  const STATES = {
    DOWNLOADED: 'downloaded',
    UPDATE: 'update_available',
    DOWNLOADING: 'downloading',
    OTHER: 'other_version',
    NOT: 'not_downloaded',
    UNKNOWN: 'unknown',
  };
  const STATE_LIST = [STATES.DOWNLOADED, STATES.UPDATE, STATES.DOWNLOADING,
    STATES.OTHER, STATES.NOT, STATES.UNKNOWN];

  const INFO = {
    downloaded: { label: '已下载', badge: '✓', color: '#1a7f37', tone: 'ok' },
    update_available: { label: '有更新', badge: '↑', color: '#b26a00', tone: 'warn' },
    downloading: { label: '下载中', badge: '…', color: '#1a73e8', tone: 'busy' },
    other_version: { label: '其他版本', badge: '◐', color: '#6b7280', tone: 'muted' },
    not_downloaded: { label: '未下载', badge: '↓', color: '#6b7280', tone: 'muted' },
    unknown: { label: '未知', badge: '?', color: '#6b7280', tone: 'muted' },
  };

  function str(v) { return v == null ? '' : String(v); }
  function num(v) { const n = Number(v); return Number.isFinite(n) ? n : 0; }
  function idOf(v) { const s = str(v).trim(); return /^[1-9]\d*$/.test(s) ? s : ''; }

  function keyOf(modelId, versionId) {
    const mid = idOf(modelId), vid = idOf(versionId);
    if (vid) return 'version:' + vid;
    if (mid) return 'model:' + mid;
    return '';
  }

  // 缓存/响应统一用「模型/版本」身份键，不用 popup 行的 file 段：
  // 同一版本的多文件行应共享一个状态，页面胶囊与弹层也能互相对上。
  function statusKeyOf(req) {
    const r = req || {};
    return keyOf(r.modelId, r.versionId) || str(r.key) || ('url:' + str(r.url));
  }

  function stateOf(value) {
    return STATE_LIST.indexOf(value) >= 0 ? value : STATES.UNKNOWN;
  }

  function clock(ts) {
    const n = num(ts);
    if (!n) return '';
    try {
      return new Date(n).toLocaleString('zh-CN', { hour12: false });
    } catch (_) { return ''; }
  }

  // 后端条目 → 内部 entry。keyHint 来自请求项（无 ID 的页面靠它保持稳定）。
  function normalizeItem(raw, opts) {
    const o = opts || {};
    const r = (raw && typeof raw === 'object') ? raw : {};
    const now = num(o.now) || Date.now();
    const modelId = idOf(r.model_id || r.modelId);
    const versionId = idOf(r.version_id || r.versionId);
    const key = keyOf(modelId, versionId) || str(o.keyHint) || '';
    const state = stateOf(r.state);
    return {
      key,
      modelId,
      versionId,
      state,
      matched: str(r.matched || 'none'),
      versionMatched: r.version_matched === true,
      local: r.local && typeof r.local === 'object' ? r.local : null,
      localVersions: Array.isArray(r.local_versions) ? r.local_versions : [],
      download: r.download && typeof r.download === 'object' ? r.download : null,
      history: r.history && typeof r.history === 'object' ? r.history : null,
      update: r.update && typeof r.update === 'object' ? r.update : null,
      reason: str(r.reason || ''),
      checkedAt: num(o.cacheAt) || now,
      seenAt: now,
      seenUrls: Array.isArray(o.seenUrls) ? o.seenUrls.slice(0, MAX_SEEN_URLS) : [],
      offline: o.offline === true,
      libraryReady: o.libraryReady !== false,
      libraryScanning: o.libraryScanning === true,
      updatesCheckedAt: num(o.updatesCheckedAt),
    };
  }

  // 内容身份：C 站文件哈希（SHA256）。桌面端在版本 ID 缺失/文件改名时用它回退匹配；
  // 只接受 64 位十六进制，去重并限 8 个，避免把任意字符串带进请求体。
  function hashesOf(input) {
    const r = (input && typeof input === 'object') ? input : {};
    const meta = (r.meta && typeof r.meta === 'object') ? r.meta : {};
    const out = [], seen = new Set();
    const add = (v) => {
      const s = str(v).trim().toLowerCase();
      if (!/^[a-f0-9]{64}$/.test(s) || seen.has(s) || out.length >= 8) return;
      seen.add(s); out.push(s);
    };
    if (Array.isArray(r.hashes)) r.hashes.forEach(add);
    if (Array.isArray(meta.sha256s)) meta.sha256s.forEach(add);
    if (Array.isArray(meta.files)) meta.files.forEach((f) => add(f && f.sha256));
    return out;
  }

  // 请求项：popup 行 / content 页面信息 → 后端入参 + 扩展 key。
  function buildItem(input, tabId) {
    const r = (input && typeof input === 'object') ? input : {};
    const modelId = idOf(r.modelId || r.model_id || r.id);
    let versionId = idOf(r.versionId || r.version_id || r.version);
    if (!versionId) versionId = idOf(r.enrichVersionId || (r.meta && r.meta.versionId));
    const url = str(r.url || '');
    const key = str(r.key || '') || keyOf(modelId, versionId) || ('url:' + url);
    const out = { key, modelId, versionId, url };
    const hashes = hashesOf(r);
    if (hashes.length) out.hashes = hashes;
    if (Number.isFinite(Number(tabId))) out.tabId = Number(tabId);
    return out;
  }

  function buildItems(rows) {
    const out = [];
    const seen = new Set();
    for (const row of Array.isArray(rows) ? rows : []) {
      if (!row || typeof row !== 'object') continue;
      const item = buildItem(row, row.tabId);
      if (!item.modelId && !item.versionId && !item.url) continue;
      // 同一 key 的多个标签页都要保留 tabId（徽章逐标签设置），但请求体只发一份
      const dedupe = item.key + '#' + (item.tabId == null ? '' : item.tabId);
      if (seen.has(dedupe)) continue;
      seen.add(dedupe);
      out.push(item);
    }
    return out;
  }

  // 后端响应 → byKey。响应按请求顺序返回，无 ID 的条目靠同位置请求项的 keyHint 稳定。
  function normalizeResponse(payload, opts) {
    const o = opts || {};
    const p = (payload && typeof payload === 'object') ? payload : {};
    const reqs = Array.isArray(o.items) ? o.items : [];
    const now = num(o.now) || Date.now();
    const cacheAt = num(p.generated_at) ? num(p.generated_at) * 1000 : now;
    const library = p.library && typeof p.library === 'object' ? p.library : {};
    const updates = p.updates && typeof p.updates === 'object' ? p.updates : {};
    const items = [];
    const byKey = {};
    const raws = Array.isArray(p.items) ? p.items : [];
    raws.forEach((raw, i) => {
      const req = reqs[i] || {};
      const rawIds = (raw && typeof raw === 'object') ? raw : {};
      const mid = idOf(rawIds.model_id || rawIds.modelId);
      const vid = idOf(rawIds.version_id || rawIds.versionId);
      // 身份键优先用模型/版本 ID（多文件行共享）；没有 ID 的页面才退回请求项 key。
      const key = keyOf(mid, vid) || keyOf(req.modelId, req.versionId) || str(req.key) || ('url:' + str(req.url));
      const entry = normalizeItem(raw, {
        now, keyHint: key, cacheAt,
        offline: o.offline === true,
        libraryReady: library.ready !== false,
        libraryScanning: library.scanning === true,
        updatesCheckedAt: num(updates.checked_at) * 1000,
        seenUrls: req.url ? [req.url] : [],
      });
      entry.key = key;
      items.push(entry);
      byKey[key] = entry;
    });
    return { ok: p.ok !== false, items, byKey, cacheAt, library, updates,
      offline: o.offline === true, error: str(o.error || '') };
  }

  // 离线/失败时的兜底：请求里每一项都要有可展示的 entry，不能把“没查”显示成“没下载”。
  function offlineResponse(items, opts) {
    const o = opts || {};
    const now = num(o.now) || Date.now();
    const isOffline = o.offline !== false;
    const reason = str(o.reason) || (isOffline
      ? '未连接 CFT，且扩展里还没有这个模型的状态缓存'
      : '桌面端没有返回可用的下载状态');
    const out = [];
    const byKey = {};
    for (const req of Array.isArray(items) ? items : []) {
      const key = statusKeyOf(req);
      const entry = normalizeItem({ state: STATES.UNKNOWN, reason }, {
        now, keyHint: key, cacheAt: 0, offline: isOffline, seenUrls: req.url ? [req.url] : [],
      });
      entry.key = key;
      out.push(entry);
      byKey[key] = entry;
    }
    return { ok: false, items: out, byKey, cacheAt: 0, library: {}, updates: {},
      offline: isOffline, error: str(o.error || '') };
  }

  function sanitizeCache(raw) {
    const cache = (raw && typeof raw === 'object') ? raw : {};
    const entries = {};
    const src = cache.entries && typeof cache.entries === 'object' ? cache.entries : {};
    for (const key of Object.keys(src)) {
      const e = src[key];
      if (!e || typeof e !== 'object') continue;
      if (!e.key) e.key = key;
      e.state = stateOf(e.state);
      e.checkedAt = num(e.checkedAt);
      e.seenAt = num(e.seenAt) || e.checkedAt;
      e.seenUrls = Array.isArray(e.seenUrls) ? e.seenUrls.slice(0, MAX_SEEN_URLS) : [];
      entries[key] = e;
    }
    return { version: CACHE_VERSION, savedAt: num(cache.savedAt), entries };
  }

  function prune(cache) {
    const keys = Object.keys(cache.entries);
    if (keys.length <= MAX_ENTRIES) return cache;
    keys.sort((a, b) => num(cache.entries[b].seenAt) - num(cache.entries[a].seenAt));
    for (const key of keys.slice(MAX_ENTRIES)) delete cache.entries[key];
    return cache;
  }

  // 把一次响应合并进缓存：状态/详情以新响应为准，seenUrls 累加；离线失败保留旧状态。
  function mergeCache(rawCache, result, opts) {
    const o = opts || {};
    const cache = sanitizeCache(rawCache);
    const now = num(o.now) || Date.now();
    const r = result || {};
    const reqItems = Array.isArray(o.items) ? o.items : (r.items || []);
    const urlsByKey = o.urlsByKey || {};
    const resByKey = r.byKey || {};
    const failed = r.ok === false || r.offline === true;
    const touched = new Set();
    for (const req of reqItems) {
      const key = statusKeyOf(req);
      const fresh = resByKey[key];
      const prev = cache.entries[key] || null;
      // 整批失败（离线/超时）时后端没有给出任何事实：绝不能拿 offlineResponse 的占位
      // unknown 覆盖上一次的真实状态，只能标 offline。
      if (fresh && !(failed && prev)) {
        const entry = Object.assign({}, prev || {}, fresh);
        entry.key = key;
        entry.seenAt = now;
        const urls = new Set(Array.isArray(prev && prev.seenUrls) ? prev.seenUrls : []);
        if (req.url) urls.add(req.url);
        for (const u of (urlsByKey[key] || [])) if (u) urls.add(u);
        entry.seenUrls = Array.from(urls).slice(-MAX_SEEN_URLS);
        cache.entries[key] = entry;
        touched.add(key);
      } else if (prev) {
        // 请求没有回包（部分失败/条目被裁掉）：保留旧状态，只标离线/触达时间
        prev.offline = r.offline === true;
        prev.seenAt = now;
        if (req.url) {
          const urls = new Set(prev.seenUrls || []);
          urls.add(req.url);
          prev.seenUrls = Array.from(urls).slice(-MAX_SEEN_URLS);
        }
        touched.add(key);
      }
    }
    cache.savedAt = now;
    return { cache: prune(cache), touched: Array.from(touched) };
  }

  function getEntry(cache, modelId, versionId) {
    const c = sanitizeCache(cache);
    const vid = idOf(versionId), mid = idOf(modelId);
    if (vid && c.entries['version:' + vid]) return c.entries['version:' + vid];
    if (mid && c.entries['model:' + mid]) return c.entries['model:' + mid];
    return null;
  }

  function ttlOf(entry) {
    return entry && entry.state === STATES.DOWNLOADING ? TTL_DOWNLOADING_MS : TTL_DEFAULT_MS;
  }

  function isStale(entry, now) {
    if (!entry) return true;
    const t = num(now) || Date.now();
    return t - num(entry.checkedAt) > ttlOf(entry);
  }

  // 行状态回填：popup 的 rows 用 version key；页面胶囊用单条 entry。
  function applyToRows(rows, cacheOrResult) {
    const src = cacheOrResult || {};
    const byKey = src.byKey || (src.entries ? null : src);
    const cache = src.entries ? sanitizeCache(src) : null;
    for (const row of Array.isArray(rows) ? rows : []) {
      if (!row) continue;
      const item = buildItem(row, row.tabId);
      row.dlKey = item.key;
      let entry = null;
      const canonical = keyOf(item.modelId, item.versionId);
      if (byKey && (byKey[item.key] || (canonical && byKey[canonical]))) {
        entry = byKey[item.key] || byKey[canonical];
      } else if (cache) entry = getEntry(cache, item.modelId, item.versionId);
      if (entry) row.dl = entry;
    }
    return rows;
  }

  // ---- 文案 ----
  function versionText(ver) {
    if (!ver) return '';
    return str(ver.version_name) || (ver.version_id ? '版本 ' + ver.version_id : '');
  }

  function localText(entry) {
    const local = entry && entry.local;
    if (!local) return '';
    return versionText(local) || (local.version_id ? '版本 ' + local.version_id : '');
  }

  function detailOf(entry) {
    if (!entry) return '';
    switch (entry.state) {
      case STATES.DOWNLOADED: {
        const v = localText(entry);
        if (entry.update && entry.update.unknown_local) return (v ? v + ' · ' : '') + '更新状态未知';
        return v;
      }
      case STATES.UPDATE: {
        const from = (entry.update && entry.update.local_name) || localText(entry);
        const to = (entry.update && entry.update.latest_name) || '新版';
        return (from ? from + ' → ' : '→ ') + to;
      }
      case STATES.DOWNLOADING: {
        const d = entry.download || {};
        const p = Math.max(0, Math.min(100, Math.round(num(d.progress))));
        return (d.version_matched === false ? '其他版本 · ' : '') + p + '%';
      }
      case STATES.OTHER:
        return localText(entry) + (entry.localVersions && entry.localVersions.length > 1
          ? ' 等 ' + entry.localVersions.length + ' 个版本' : '');
      case STATES.NOT:
        return entry.history && entry.history.file_exists === false ? '历史文件已不在' : '';
      case STATES.UNKNOWN:
        return shortReason(entry.reason);
      default: return '';
    }
  }

  function shortReason(reason) {
    const s = str(reason).split(/[；\n]/)[0] || '';
    return s.length > 42 ? s.slice(0, 42) + '…' : s;
  }

  function labelOf(entry, opts) {
    const o = opts || {};
    const isOffline = !!(entry && entry.offline) || o.offline === true;
    const info = INFO[stateOf(entry && entry.state)] || INFO.unknown;
    const detail = detailOf(entry);
    const text = info.label + (detail ? ' · ' + detail : '');
    const short = info.label + (entry && entry.state === STATES.DOWNLOADING
      ? ' ' + Math.max(0, Math.min(100, Math.round(num(entry.download && entry.download.progress)))) + '%' : '');
    const lines = [];
    if (entry) {
      if (entry.local && entry.local.model_name) lines.push('模型：' + entry.local.model_name);
      lines.push('状态：' + text);
      const local = entry.local;
      if (local) {
        const v = localText(entry) || '版本未知';
        lines.push('本地：' + v + (local.path ? ' · ' + local.path : ''));
        if (local.exists === false) lines.push('本地记录存在，但文件已不在');
      }
      if (entry.history && entry.history.file_exists === false) lines.push('下载历史：文件已不在原路径');
      if (entry.update) {
        if (entry.update.has_update) {
          lines.push('最新：' + ((entry.update.latest_name || entry.update.latest_version || '') +
            (entry.update.behind > 1 ? '（落后 ' + entry.update.behind + ' 个版本）' : '')));
          lines.push('更新方式：在扩展弹层勾选后点「下载选中项的新版」');
        } else if (entry.update.unknown_local) {
          lines.push('更新：本地版本不在 C 站列表，无法判定是否有新版');
        } else if (entry.update.other_base) {
          lines.push('更新：最新版换了底模，按 CFT 规则不算更新');
        }
        if (entry.update.checked_at) lines.push('更新检查于：' + clock(entry.update.checked_at));
      } else if (entry.state === STATES.DOWNLOADED || entry.state === STATES.OTHER) {
        lines.push(entry.updatesCheckedAt ? '更新：已检查，但没有这个模型的记录' : '更新：还没在 CFT 里检查过这个模型');
      }
      if (entry.download) {
        lines.push('CFT：' + str(entry.download.status) + ' ' +
          Math.round(num(entry.download.progress)) + '%' +
          (entry.download.filename ? ' · ' + entry.download.filename : ''));
      }
      if (entry.reason) lines.push('原因：' + entry.reason);
      if (entry.checkedAt) lines.push('状态检查于：' + clock(entry.checkedAt));
      if (isOffline) lines.push('CFT 未连接：显示的是上次缓存状态');
    } else {
      lines.push('状态：未知（没有状态缓存）');
    }
    return {
      state: stateOf(entry && entry.state),
      label: info.label,
      text,
      short,
      detail,
      badge: info.badge,
      color: info.color,
      tone: info.tone,
      cls: 'dl-' + stateOf(entry && entry.state),
      offline: isOffline,
      title: lines.join('\n'),
    };
  }

  function categoryOf(entry) {
    const s = stateOf(entry && entry.state);
    if (s === STATES.UPDATE) return 'update';
    if (s === STATES.DOWNLOADED) return 'downloaded';
    if (s === STATES.DOWNLOADING) return 'downloading';
    if (s === STATES.OTHER) return 'other';
    if (s === STATES.NOT) return 'not';
    return 'unknown';
  }

  function filterRows(rows, category) {
    if (!category) return Array.isArray(rows) ? rows.slice() : [];
    return (Array.isArray(rows) ? rows : []).filter((r) => categoryOf(r && r.dl) === category);
  }

  function summaryOf(entries, opts) {
    const o = opts || {};
    const counts = { downloaded: 0, update: 0, downloading: 0, other: 0, not: 0, unknown: 0 };
    const list = Array.isArray(entries) ? entries : [];
    for (const e of list) counts[categoryOf(e)]++;
    const parts = [];
    if (counts.downloaded) parts.push('已下载 ' + counts.downloaded);
    if (counts.update) parts.push('有更新 ' + counts.update);
    if (counts.downloading) parts.push('下载中 ' + counts.downloading);
    if (counts.other) parts.push('其他版本 ' + counts.other);
    if (counts.not) parts.push('未下载 ' + counts.not);
    if (counts.unknown) parts.push('未知 ' + counts.unknown);
    const when = o.cacheAt ? ' · 检查于 ' + clock(o.cacheAt) : '';
    const offline = o.offline ? ' · CFT 未连接，显示上次缓存（不是“没有下载”）' : '';
    return {
      counts,
      text: list.length ? '下载状态：' + parts.join(' · ') + when + offline : '',
      offline: !!o.offline,
    };
  }

  function keyFor(modelId, versionId) { return keyOf(modelId, versionId); }

  root.CFT.modelStatus = {
    CACHE_KEY, CACHE_VERSION, MAX_ENTRIES, TTL_DOWNLOADING_MS, TTL_DEFAULT_MS,
    STATES, STATE_LIST, INFO,
    keyOf, stateOf, normalizeItem, normalizeResponse, offlineResponse,
    buildItem, buildItems, hashesOf, sanitizeCache, mergeCache, getEntry,
    isStale, ttlOf, applyToRows,
    labelOf, categoryOf, filterRows, summaryOf, clock, keyFor,
  };
})(globalThis);
