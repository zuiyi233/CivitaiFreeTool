'use strict';
/* CFT content/image-badge.js —— images/posts 页 SAVE 徽章（P3）
 * 搬运自油猴脚本 v1.7.8：loadSavedImages(2616-2628，localStorage→CFT.idb 只读)、
 * addImageBadges(2638-2691，originalUrl 构造/SAVED-SAVE 状态/rAF 防重入)、
 * 徽章点击后的 saveSingleImage 请求封装（downloadSingleImage 2694-2800 的意图，
 * 实际下载在 offscreen；filePrefix 由 offscreen 处理，本文件不传 prefix）。
 * 不搬：persistData（只读不写）、downloadSingleImage/downloadBlob 本体、
 * fetchAndCacheModelData/scanModelVersionId（调用点确认仅服务徽章文件名前缀，
 * 前缀已移交 offscreen，content 侧无用）、enqueueDownload 串行队列（单图请求
 * 走 bg/offscreen 侧排队，content 只发消息）。
 * classic content script：样式全部走 content.css（cft- 前缀类），本文件无 inline 样式。
 */
(function () {
  var SAVE_KEY = 'savedImages';
  var RESCAN_INTERVAL_MS = 2000;
  var FAIL_REVERT_MS = 2000;

  var savedSet = {}; // {[imageId]: true}
  var badgeState = {}; // {[imageId]: 'saving'|'saved'|'error'}

  /* ================= 纯逻辑函数（无 DOM，可在 node 下单测） ================= */

  // href → imageId（脚本 311-322 原样：同站校验 + /images/(\d+) + 裸正则兜底）
  function getImageIdFromHref(href, loc) {
    if (!href) return null;
    var s = String(href);
    try {
      var origin = (loc && loc.origin) || '';
      var u = new URL(s, origin || undefined);
      var hosts = ['civitai.com', 'civitai.red', 'www.civitai.com', 'www.civitai.red'];
      if (hosts.indexOf(u.hostname) < 0) return null;
      var m = u.pathname.match(/^\/images\/(\d+)/);
      return m ? m[1] : null;
    } catch (_) {
      var m2 = s.match(/\/images\/(\d+)/);
      return m2 ? m2[1] : null;
    }
  }

  // img src → {ext, originalUrl}（脚本 2651-2658 原样）
  function buildOriginalUrl(src, imageId) {
    try {
      var urlObj = new URL(String(src));
      var parts = urlObj.pathname.split('/');
      var filename = parts[parts.length - 1] || '';
      var extMatch = filename.match(/\.([a-zA-Z0-9]+)$/);
      var ext = extMatch ? extMatch[1] : 'jpeg';
      var basePath = parts.slice(0, parts.length - 2).join('/');
      var originalUrl = urlObj.origin + basePath + '/original=true/' + imageId + '.' + ext;
      return { ext: ext, originalUrl: originalUrl };
    } catch (_) {
      return null;
    }
  }

  // idb 原始值 → id 数组（兼容新格式 {[id]:{time}} 与脚本旧格式 [{id,...}]）
  function normalizeSavedIds(raw) {
    var ids = [];
    try {
      if (!raw) return ids;
      if (Array.isArray(raw)) {
        raw.forEach(function (it) {
          var id = it && (it.id ?? it.imageId);
          if (id != null && String(id) !== '') ids.push(String(id));
        });
      } else if (typeof raw === 'object') {
        Object.keys(raw).forEach(function (k) { ids.push(String(k)); });
      }
    } catch (_) { /* 忽略 */ }
    return ids;
  }

  /* ================= 环境小助手（防御性） ================= */

  function doc() {
    try { return (typeof document !== 'undefined') ? document : null; }
    catch (_) { return null; }
  }

  function warn() {
    try { console.warn.apply(console, ['[cft:image-badge]'].concat([].slice.call(arguments))); }
    catch (_) { /* 忽略 */ }
  }

  function scheduleFrame(fn) {
    try {
      if (typeof requestAnimationFrame === 'function') { requestAnimationFrame(fn); return; }
    } catch (_) { /* 走兜底 */ }
    try { setTimeout(fn, 0); } catch (_) {}
  }

  /* ================= savedImages 只读加载 ================= */

  function loadSavedImages() {
    try {
      var idb = (typeof globalThis !== 'undefined' && globalThis.CFT && globalThis.CFT.idb) || null;
      if (!idb || typeof idb.idbGet !== 'function') return Promise.resolve(false);
      return idb.idbGet(SAVE_KEY).then(function (raw) {
        try {
          savedSet = {};
          normalizeSavedIds(raw).forEach(function (id) { savedSet[id] = true; });
          return true;
        } catch (e) { warn('normalize savedImages failed', e); return false; }
      }).catch(function (e) { warn('loadSavedImages failed', e); return false; });
    } catch (e) { warn('loadSavedImages failed', e); return Promise.resolve(false); }
  }

  /* ================= 徽章 DOM ================= */

  function findBadge(anchor) {
    try {
      if (!anchor || !anchor.querySelector) return null;
      return anchor.querySelector('[data-cft-badge="true"]');
    } catch (_) { return null; }
  }

  function paintBadge(badge, imageId) {
    try {
      if (!badge) return;
      var saved = !!savedSet[String(imageId)] || badgeState[String(imageId)] === 'saved';
      var busy = badgeState[String(imageId)] === 'saving';
      var failed = badgeState[String(imageId)] === 'error';
      badge.classList.remove('cft-badge-saved', 'cft-badge-busy', 'cft-badge-error');
      if (saved) {
        badge.classList.add('cft-badge-saved');
        badge.textContent = 'SAVED';
        badge.title = '已归档';
      } else if (busy) {
        badge.classList.add('cft-badge-busy');
        badge.textContent = '…';
        badge.title = '归档中…';
      } else if (failed) {
        badge.classList.add('cft-badge-error');
        badge.textContent = 'SAVE';
      } else {
        badge.textContent = 'SAVE';
        badge.title = '归档这张图';
      }
    } catch (e) { warn('paintBadge failed', e); }
  }

  function markSaved(imageId) {
    try {
      savedSet[String(imageId)] = true;
      badgeState[String(imageId)] = 'saved';
      var d = doc();
      if (!d) return;
      var anchors = d.querySelectorAll('a[data-cft-badge-anchor="true"]');
      for (var i = 0; i < anchors.length; i++) {
        try {
          var b = findBadge(anchors[i]);
          if (!b) continue;
          if (String(b.getAttribute('data-cft-image-id') || '') === String(imageId)) paintBadge(b, imageId);
        } catch (_) { /* 单个跳过 */ }
      }
    } catch (e) { warn('markSaved failed', e); }
  }

  function onBadgeClick(ev, ctx) {
    try {
      if (ev && ev.preventDefault) ev.preventDefault();
      if (ev && ev.stopPropagation) ev.stopPropagation();
    } catch (_) { /* 忽略 */ }
    try {
      var imageId = String(ctx.imageId);
      if (savedSet[imageId] || badgeState[imageId] === 'saving') return;
      badgeState[imageId] = 'saving';
      paintBadge(ctx.badge, imageId);
      if (typeof chrome === 'undefined' || !chrome.runtime) {
        return onSaveResult(ctx, { ok: false, msg: '扩展运行时不可用' });
      }
      chrome.runtime.sendMessage(
        { __cft: true, to: 'bg', type: 'saveSingleImage', payload: { imageId: imageId, originalUrl: ctx.originalUrl, ext: ctx.ext } },
        function (resp) {
          try {
            if (chrome.runtime.lastError) {
              onSaveResult(ctx, { ok: false, msg: '后台无响应' });
              return;
            }
          } catch (_) { /* 忽略 */ }
          onSaveResult(ctx, resp);
        }
      );
    } catch (e) { warn('onBadgeClick failed', e); }
  }

  // saveSingleImage 响应 {ok, msg}：成功→SAVED；失败→短暂红 + title=msg 后恢复 SAVE
  function onSaveResult(ctx, resp) {
    try {
      var imageId = String(ctx.imageId);
      var p = (resp && resp.payload) || resp || {};
      var ok = !!(p.ok || (resp && resp.ok));
      var msg = String((p && p.msg) || (resp && resp.msg) || '');
      if (ok) {
        markSaved(imageId);
        return;
      }
      badgeState[imageId] = 'error';
      try {
        ctx.badge.classList.add('cft-badge-error');
        ctx.badge.textContent = 'SAVE';
        ctx.badge.title = msg || '归档失败，点击重试';
      } catch (_) { /* 忽略 */ }
      setTimeout(function () {
        try {
          if (badgeState[imageId] !== 'error') return;
          delete badgeState[imageId];
          paintBadge(ctx.badge, imageId);
        } catch (e) { warn('fail revert failed', e); }
      }, FAIL_REVERT_MS);
    } catch (e) { warn('onSaveResult failed', e); }
  }

  function attachBadge(anchor, imageId, built, loc) {
    try {
      var d = doc();
      if (!d || !anchor || anchor.getAttribute('data-cft-badge-anchor') === 'true') return;
      var img = anchor.querySelector ? anchor.querySelector('img') : null;
      if (!img) return;
      anchor.classList.add('cft-badge-anchor');
      anchor.setAttribute('data-cft-badge-anchor', 'true');
      var badge = d.createElement('div');
      badge.className = 'cft-badge';
      badge.setAttribute('data-cft-badge', 'true');
      badge.setAttribute('data-cft-image-id', String(imageId));
      paintBadge(badge, imageId);
      var snap = { imageId: String(imageId), originalUrl: built.originalUrl, ext: built.ext, badge: badge };
      badge.addEventListener('click', function (ev) { onBadgeClick(ev, snap); });
      anchor.appendChild(badge);
      void loc; // 保留参数位（站点判断已在 getImageIdFromHref 内完成）
    } catch (e) { warn('attachBadge failed', e); }
  }

  // addImageBadges（脚本 2638-2691 适配 + rAF 防重入保留）
  function addImageBadges() {
    if (addImageBadges.isPending) return;
    addImageBadges.isPending = true;
    scheduleFrame(function () {
      try {
        addImageBadges.isPending = false;
        var d = doc();
        if (!d) return;
        var loc = null;
        try { loc = (typeof window !== 'undefined' && window.location) || null; } catch (_) { loc = null; }
        var anchors = d.querySelectorAll('a[href*="/images/"]');
        for (var i = 0; i < anchors.length; i++) {
          try {
            var a = anchors[i];
            if (a.getAttribute('data-cft-badge-anchor') === 'true') continue;
            var img = a.querySelector ? a.querySelector('img') : null;
            if (!img) continue;
            var imageId = getImageIdFromHref(a.getAttribute('href'), loc);
            if (!imageId) continue;
            var built = buildOriginalUrl(img.src, imageId);
            if (!built) { warn('解析图片URL失败', img.src); continue; }
            attachBadge(a, imageId, built, loc);
          } catch (e) { warn('scan anchor failed', e); }
        }
      } catch (e) { warn('addImageBadges failed', e); }
    });
  }
  addImageBadges.isPending = false;

  /* ================= 消息处理 ================= */

  function onRuntimeMessage(msg) {
    try {
      if (!msg || msg.__cft !== true || msg.to !== 'content') return; // 非本模块消息
      if (msg.type === 'savedImageDone') {
        try {
          var p = (msg && msg.payload) || {};
          if (p.ok && p.imageId != null && String(p.imageId) !== '') markSaved(p.imageId);
          else if (p.imageId != null && String(p.imageId) !== '') {
            delete badgeState[String(p.imageId)];
            var d = doc();
            if (d) {
              var b = d.querySelector('[data-cft-image-id="' + String(p.imageId) + '"]');
              if (b) paintBadge(b, p.imageId);
            }
          }
        } catch (e) { warn('savedImageDone failed', e); }
      }
      // 其余类型（progress/archiveDone/askFileSelection 等）归 model-page.js 处理
    } catch (e) { warn('onRuntimeMessage failed', e); }
  }

  /* ================= 挂载 ================= */

  function init() {
    try {
      var d = doc();
      if (!d) return;
      loadSavedImages().then(function () { addImageBadges(); });
      setInterval(function () {
        try { addImageBadges(); } catch (e) { warn('rescan tick failed', e); }
      }, RESCAN_INTERVAL_MS);
      try {
        if (typeof MutationObserver !== 'undefined' && d.body) {
          var mo = new MutationObserver(function () { addImageBadges(); });
          mo.observe(d.body, { childList: true, subtree: true });
        }
      } catch (e) { warn('observer failed', e); }
      if (typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.onMessage) {
        chrome.runtime.onMessage.addListener(onRuntimeMessage);
      }
    } catch (e) { warn('init failed', e); }
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      getImageIdFromHref: getImageIdFromHref,
      buildOriginalUrl: buildOriginalUrl,
      normalizeSavedIds: normalizeSavedIds,
      handleMessage: onRuntimeMessage
    };
  } else {
    init();
  }
})();
