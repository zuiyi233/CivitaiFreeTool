'use strict';
/* CFT content/list-status.js —— C 站列表页 / 图片页 / 帖子页的模型链接状态徽章。
 *
 * 只做一件事：找到页面里的 /models/<id> 链接，在所属卡片右上角贴一枚
 * ✓ 已下载 / ↑ 有更新 / … 下载中 / ◐ 其他版本 / ↓ 未下载。
 * 数据只来自扩展状态缓存与本机桥；unknown 不显示，避免“还没查”被看成结论。
 * 不读网页正文、不点按钮、不改站点数据；样式见 content.css 的 .cft-list-status。
 */
(function () {
  var MS = (typeof globalThis !== 'undefined' && globalThis.CFT && globalThis.CFT.modelStatus) || null;
  var CACHE_KEY = MS ? MS.CACHE_KEY : 'cftModelStatusV1';
  var BADGE_CLASS = 'cft-list-status';
  var MAX_QUERY = 200;
  var SCAN_DEBOUNCE_MS = 300;
  var ROUTE_CHECK_MS = 1000;

  var observer = null, scanTimer = 0, routeTimer = 0, armed = false, querying = false, handledPath = '';
  var lastQueryAt = 0;

  function warn() {
    try { console.warn.apply(console, ['[cft:list-status]'].concat([].slice.call(arguments))); } catch (_) {}
  }

  function pathname() {
    try { return String(location.pathname || ''); } catch (_) { return ''; }
  }

  function pageKind(path) {
    var p = String(path || '');
    if (p === '/models' || p === '/models/') return 'list';
    if (/^\/images\/\d+/.test(p) || /^\/posts\/\d+/.test(p)) return 'media';
    return '';
  }

  function isTarget() { return !!pageKind(pathname()); }

  // 同站模型链接 → {modelId,versionId,key,url}；只认 C 站两站，忽略下载链接与站外。
  function parseModelLink(raw) {
    try {
      var u = new URL(String(raw || ''), location.origin);
      if (!/^https?:$/.test(u.protocol)) return null;
      if (!/^(www\.)?civitai\.(red|com)$/i.test(u.hostname) || u.port) return null;
      var m = u.pathname.match(/^\/models\/(\d+)(?:\/|$)/);
      if (!m) return null;
      var vid = u.searchParams.get('modelVersionId') || '';
      if (vid && !/^[1-9]\d*$/.test(vid)) return null;
      var key = (vid ? 'version:' + vid : 'model:' + m[1]);
      return { modelId: m[1], versionId: vid, key: key, url: u.href };
    } catch (_) { return null; }
  }

  function cardOf(anchor) {
    try {
      return anchor.closest('article')
        || anchor.closest('[class*="mantine-Card"]')
        || anchor.closest('[data-testid*="card"]')
        || anchor.parentElement;
    } catch (_) { return anchor.parentElement; }
  }

  function badgeOf(card) {
    try {
      if (!card) return null;
      if (card.__cftListBadge && card.__cftListBadge.isConnected) return card.__cftListBadge;
      return card.querySelector('.' + BADGE_CLASS);
    } catch (_) { return null; }
  }

  function removeBadge(card) {
    var b = badgeOf(card);
    if (b) { try { b.remove(); } catch (_) {} }
    try {
      if (card && card.__cftListBadge) card.__cftListBadge = null;
      if (card && card.dataset && card.dataset.cftListPos === '1') {
        card.style.position = '';
        delete card.dataset.cftListPos;
      }
    } catch (_) {}
  }

  function render(card, entry) {
    if (!card || !MS) return;
    var lab = MS.labelOf(entry, { offline: !!(entry && entry.offline) });
    // unknown 不贴：还没查到不是结论；缓存里明确未知时也不占卡片位置
    if (!entry || !lab || lab.state === 'unknown') { removeBadge(card); return; }
    var b = badgeOf(card);
    if (!b) {
      b = document.createElement('span');
      b.className = BADGE_CLASS;
      b.dataset.cftListStatus = '1';
      try {
        if (getComputedStyle(card).position === 'static') {
          card.style.position = 'relative';
          card.dataset.cftListPos = '1';
        }
      } catch (_) {}
      card.appendChild(b);
      card.__cftListBadge = b;
    }
    // 文本没变就不要写：textContent 会替换子节点，MutationObserver 会把它当成页面变化，
    // 每轮扫描再触发下一轮（实测 2.5s 内自激 8 次）。class/title 是属性，本来就不在监听面里。
    var text = lab.badge + ' ' + lab.short;
    if (b.textContent !== text) b.textContent = text;
    b.title = lab.title || lab.text;
    b.className = BADGE_CLASS + ' dl-' + lab.state + (lab.offline ? ' offline' : '');
  }

  function collectAnchors() {
    var out = [];
    try {
      var anchors = document.querySelectorAll('a[href*="/models/"]');
      for (var i = 0; i < anchors.length; i++) {
        var info = parseModelLink(anchors[i].getAttribute('href'));
        if (!info) continue;
        out.push({ anchor: anchors[i], info: info, card: cardOf(anchors[i]) });
      }
    } catch (e) { warn('collectAnchors failed', e); }
    return out;
  }

  function clearAll() {
    try {
      var nodes = document.querySelectorAll('.' + BADGE_CLASS);
      for (var i = 0; i < nodes.length; i++) {
        var card = nodes[i].parentElement;
        removeBadge(card);
      }
    } catch (_) {}
  }

  function applyEntries(anchors, entries) {
    for (var i = 0; i < anchors.length; i++) {
      var a = anchors[i];
      var e = entries && entries[a.info.key];
      if (e) render(a.card, e);
    }
  }

  function storageGet(key) {
    return new Promise(function (resolve) {
      try {
        if (typeof chrome === 'undefined' || !chrome.storage || !chrome.storage.local) { resolve(null); return; }
        chrome.storage.local.get(key, function (raw) {
          try { void chrome.runtime.lastError; } catch (_) {}
          resolve(raw && raw[key] ? raw[key] : null);
        });
      } catch (_) { resolve(null); }
    });
  }

  function bgQuery(items) {
    return new Promise(function (resolve) {
      var done = false;
      var timer = setTimeout(function () { if (!done) { done = true; resolve(null); } }, 8000);
      try {
        if (typeof chrome === 'undefined' || !chrome.runtime || !chrome.runtime.sendMessage) {
          clearTimeout(timer); resolve(null); return;
        }
        chrome.runtime.sendMessage({
          __cft: true, to: 'bg', type: 'modelStatusQuery',
          // badge:false —— 列表页没有单一模型身份，不能把某个模型状态写到工具栏徽章
          payload: { items: items, refresh: 'auto', badge: false },
        }, function (r) {
          if (done) return;
          done = true; clearTimeout(timer);
          try { void chrome.runtime.lastError; } catch (_) {}
          resolve(r || null);
        });
      } catch (_) {
        if (!done) { done = true; clearTimeout(timer); resolve(null); }
      }
    });
  }

  async function scan() {
    if (!isTarget()) { clearAll(); return; }
    var anchors = collectAnchors();
    if (!anchors.length) return;
    var cache = MS ? MS.sanitizeCache(await storageGet(CACHE_KEY)) : null;
    var now = Date.now();
    var entries = {}, unique = {}, missing = [];
    for (var i = 0; i < anchors.length; i++) {
      var key = anchors[i].info.key;
      if (!entries[key]) {
        var e = MS ? MS.getEntry(cache, anchors[i].info.modelId, anchors[i].info.versionId) : null;
        entries[key] = e || null;
      }
      if (!unique[key]) {
        unique[key] = anchors[i].info;
        if (MS && MS.isStale(entries[key], now)) missing.push(unique[key]);
      }
    }
    applyEntries(anchors, entries);
    if (!missing.length || querying || document.hidden) return;
    if (Date.now() - lastQueryAt < 15000) return;   // 同一页滚动/变更不反复打桥
    lastQueryAt = Date.now();
    querying = true;
    try {
      var result = await bgQuery(missing.slice(0, MAX_QUERY).map(function (m) {
        return { key: m.key, modelId: m.modelId, versionId: m.versionId, url: m.url };
      }));
      if (result && result.ok === true && result.byKey) {
        for (var k in result.byKey) if (result.byKey[k]) entries[k] = result.byKey[k];
        applyEntries(anchors, entries);
      }
    } catch (e) { warn('status query failed', e); }
    finally { querying = false; }
  }

  function scheduleScan() {
    if (scanTimer) return;
    scanTimer = setTimeout(function () { scanTimer = 0; void scan(); }, SCAN_DEBOUNCE_MS);
  }

  function arm() {
    if (armed || !isTarget()) return;
    armed = true;
    try {
      observer = new MutationObserver(scheduleScan);
      observer.observe(document.body, { childList: true, subtree: true });
    } catch (e) { warn('observe failed', e); }
    scheduleScan();
  }

  function disarm() {
    armed = false;
    if (observer) { try { observer.disconnect(); } catch (_) {} observer = null; }
    clearAll();
  }

  function routeCheck() {
    var p = pathname();
    if (p === handledPath) return;
    handledPath = p;
    if (isTarget()) arm(); else disarm();
  }

  function init() {
    try {
      handledPath = pathname();
      if (isTarget()) arm();
      try {
        if (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.onChanged) {
          chrome.storage.onChanged.addListener(function (changes, area) {
            if (area === 'local' && changes && changes[CACHE_KEY]) scheduleScan();
          });
        }
      } catch (_) {}
      try {
        document.addEventListener('visibilitychange', function () {
          if (!document.hidden) scheduleScan();
        });
      } catch (_) {}
      routeTimer = setInterval(routeCheck, ROUTE_CHECK_MS);
      try { if (routeTimer && routeTimer.unref) routeTimer.unref(); } catch (_) {}
    } catch (e) { warn('init failed', e); }
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { parseModelLink: parseModelLink, pageKind: pageKind };
  } else {
    init();
  }
})();
