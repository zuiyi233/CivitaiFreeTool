'use strict';
/* CFT content/model-page.js —— 模型页交互（悬浮归档按钮 / 页面采集 / 弹窗应答 / DOM 评论兜底）
 * 搬运自油猴脚本 v1.7.8：getCivitaiIdsFromPage(1406-1434)、extractAboutThisVersionText(1437-1448)、
 * DOM 评论兜底(1510-1522)、showObjectSelectionDialog(2572-2607)、createButtonProgressController(1354-1405)、
 * askArchiveConfirm 文案(2155-2157)。样式全部走 content.css（cft- 前缀类），本文件无 inline 样式。
 * classic content script：依赖 manifest 按序注入的 globalThis.CFT.util，只读使用。
 */
(function () {
  var BTN_ID = 'cft-archive-btn';
  var LABEL_DEFAULT = '🗂 一键归档';
  var RESTORE_DELAY_MS = 3000;
  var ENSURE_INTERVAL_MS = 2000;

  /* ================= 纯逻辑函数（无 DOM，可在 node 下单测） ================= */

  // URL → {modelId, modelVersionId}（脚本 1408-1413）
  function parseCivitaiIdsFromUrl(href) {
    var out = { modelId: '', modelVersionId: '' };
    try {
      var u = new URL(href);
      var m = u.pathname.match(/\/models\/(\d+)/);
      out.modelId = (m && m[1]) || '';
      out.modelVersionId = u.searchParams.get('modelVersionId') || '';
    } catch (_) { /* 忽略 */ }
    return out;
  }

  // 下载链接 href → versionId（脚本 1416-1420）
  function matchDownloadVersionId(href) {
    try {
      var m = String(href || '').match(/\/api\/download\/models\/(\d+)/);
      return (m && m[1]) || '';
    } catch (_) { return ''; }
  }

  // review 链接 href → versionId（脚本 1422-1426）
  function matchReviewVersionId(href) {
    try {
      var m = String(href || '').match(/modelVersionId=(\d+)/);
      return (m && m[1]) || '';
    } catch (_) { return ''; }
  }

  // 全数字代码块文本数组 → versionId（脚本 1427-1432：取第 2 个）
  function pickCodeVersionId(texts) {
    try {
      var nums = (texts || []).filter(function (t) { return /^\d+$/.test(t); });
      return nums.length >= 2 ? nums[1] : '';
    } catch (_) { return ''; }
  }

  function isModelPath(pathname) {
    try { return /\/models\/\d+/.test(String(pathname || '')); }
    catch (_) { return false; }
  }

  // 重复归档确认文案（脚本 2155-2157 原样）
  function buildArchiveConfirmText(rec) {
    try {
      var r = rec || {};
      return '⚠ 该模型版本已归档过！\n\n模型：' + (r.modelName || r.modelId || '') +
        '\n版本：' + (r.modelVer || r.modelVersionId || '') +
        '\n归档时间：' + (r.archiveTime || '') +
        '\n保存目录：' + (r.dirName || '（未知）') +
        '\n\n【确定】= 仍然重新下载（会覆盖原目录内同名文件）\n【取消】= 放弃本次下载';
    } catch (_) { return '该模型版本已归档过，确定重新下载？'; }
  }

  /* ================= 环境小助手（防御性） ================= */

  function doc() {
    try { return (typeof document !== 'undefined') ? document : null; }
    catch (_) { return null; }
  }

  function safeLocationHref() {
    try {
      if (typeof window !== 'undefined' && window.location) return String(window.location.href);
      if (typeof location !== 'undefined' && location.href) return String(location.href);
    } catch (_) { /* 忽略 */ }
    return '';
  }

  function safePathname() {
    try {
      if (typeof window !== 'undefined' && window.location) return String(window.location.pathname);
    } catch (_) { /* 忽略 */ }
    return '';
  }

  // CFT.util.getTextContentNormalized 只读使用；不可用时本地兜底
  function txt(el) {
    try {
      if (typeof globalThis !== 'undefined' && globalThis.CFT && globalThis.CFT.util) {
        return globalThis.CFT.util.getTextContentNormalized(el);
      }
    } catch (_) { /* 走兜底 */ }
    try { return String((el && el.textContent) || '').replace(/\s+/g, ' ').trim(); }
    catch (_) { return ''; }
  }

  function normLines(s) {
    try {
      if (typeof globalThis !== 'undefined' && globalThis.CFT && globalThis.CFT.util) {
        return globalThis.CFT.util.normalizeMultilineText(s);
      }
    } catch (_) { /* 走兜底 */ }
    return String(s || '').replace(/\r\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  }

  function warn() {
    try { console.warn.apply(console, ['[cft:model-page]'].concat([].slice.call(arguments))); }
    catch (_) { /* 忽略 */ }
  }

  /* ================= DOM 提取（脚本逻辑原样搬运） ================= */

  function getCivitaiIdsFromPage() {
    var result = parseCivitaiIdsFromPageUrl();
    try {
      var d = doc();
      if (!d) return result;
      if (!result.modelVersionId) {
        var dl = d.querySelector('a[href*="/api/download/models/"]');
        result.modelVersionId = matchDownloadVersionId(dl && dl.getAttribute('href'));
      }
      if (!result.modelVersionId) {
        var rl = d.querySelector('a[href*="modelVersionId="]');
        result.modelVersionId = matchReviewVersionId(rl && rl.getAttribute('href'));
      }
      if (!result.modelVersionId) {
        var codes = Array.prototype.map.call(
          d.querySelectorAll('.mantine-Code-root'), function (el) { return txt(el); });
        result.modelVersionId = pickCodeVersionId(codes);
      }
    } catch (e) { warn('getCivitaiIdsFromPage failed', e); }
    return result;
  }

  function parseCivitaiIdsFromPageUrl() {
    return parseCivitaiIdsFromUrl(safeLocationHref());
  }

  // 右侧 "About this version" 文本（兼容 CCT 汉化"关于此版本"，脚本 1437-1448）
  function extractAboutThisVersionText() {
    try {
      var d = doc();
      if (!d) return '';
      var keys = ['about this version', '关于此版本'];
      var found = null;
      var cands = d.querySelectorAll('p, span, div, h1, h2, h3, h4');
      for (var i = 0; i < cands.length; i++) {
        if (keys.indexOf(txt(cands[i]).toLowerCase()) >= 0) { found = cands[i]; break; }
      }
      if (!found) return '';
      var box = null;
      try { box = found.closest('[data-with-border="true"]'); } catch (_) { box = null; }
      var container = box || found.parentElement;
      return normLines(txt(container));
    } catch (e) { warn('extractAboutThisVersionText failed', e); return ''; }
  }

  function collectPageData() {
    try {
      var ids = getCivitaiIdsFromPage();
      return {
        modelId: ids.modelId || '',
        modelVersionId: ids.modelVersionId || '',
        aboutText: extractAboutThisVersionText(),
        pageUrl: safeLocationHref()
      };
    } catch (e) { warn('collectPageData failed', e); return { modelId: '', modelVersionId: '', aboutText: '', pageUrl: '' }; }
  }

  // 评论 DOM 兜底（脚本 1510-1522）：只抓叶子节点、去重、长度>=8
  // 返回原始 results 数组 [{user,content,createdAt,reply,fromDom}]，由 offscreen 格式化
  function collectDomComments() {
    var results = [];
    try {
      var d = doc();
      if (!d) return results;
      var seen = {};
      var els = d.querySelectorAll('[class*="comment"], [data-comment]');
      for (var i = 0; i < els.length; i++) {
        var el = els[i];
        try {
          if (el.querySelector('[class*="comment"], [data-comment]')) continue;
          var text = txt(el);
          if (!text || text.length < 8 || seen[text]) continue;
          seen[text] = 1;
          var userEl = null;
          try {
            var host = el.closest ? el.closest('[class*="comment"]') : null;
            userEl = host ? host.querySelector('a[href*="/user/"]') : null;
          } catch (_) { userEl = null; }
          results.push({
            user: userEl ? String(userEl.textContent || '').trim() || '匿名' : '匿名',
            content: text, createdAt: '', reply: false, fromDom: true
          });
        } catch (_) { /* 单条失败跳过 */ }
      }
    } catch (e) { warn('collectDomComments failed', e); }
    return results;
  }

  /* ================= toast（脚本 showToast 2867-2892 简化版，样式走 css） ================= */

  function showToast(msg, isError) {
    try {
      var d = doc();
      if (!d || !d.body) return;
      var t = d.createElement('div');
      t.className = 'cft-toast' + (isError ? ' cft-toast-error' : '');
      t.textContent = String(msg || '');
      d.body.appendChild(t);
      setTimeout(function () { try { t.classList.add('cft-toast-hide'); } catch (_) {} }, 4000);
      setTimeout(function () { try { t.remove(); } catch (_) {} }, 4500);
    } catch (e) { warn('showToast failed', e); }
  }

  /* ================= 悬浮归档按钮 + 进度态（适配脚本 1354-1405） ================= */

  var btnEls = { btn: null, fill: null, label: null };
  var btnBusy = false;
  var restoreTimer = 0;

  function getButton() {
    try {
      var d = doc();
      return (d && d.getElementById) ? d.getElementById(BTN_ID) : null;
    } catch (_) { return null; }
  }

  function setBtnRefs(btn) {
    btnEls.btn = btn;
    btnEls.fill = btn ? btn.querySelector('.cft-archive-fill') : null;
    btnEls.label = btn ? btn.querySelector('.cft-archive-label') : null;
  }

  function setLabel(s) { try { if (btnEls.label) btnEls.label.textContent = String(s); } catch (_) {} }

  function setFill(pct) {
    try {
      var p = Math.max(0, Math.min(100, Math.round(Number(pct) || 0)));
      if (btnEls.fill) btnEls.fill.style.width = p + '%';
      return p;
    } catch (_) { return 0; }
  }

  function clearRestoreTimer() {
    try { if (restoreTimer) clearTimeout(restoreTimer); } catch (_) {}
    restoreTimer = 0;
  }

  function resetButton() {
    try {
      clearRestoreTimer();
      btnBusy = false;
      var btn = getButton();
      if (!btn) { setBtnRefs(null); return; }
      setBtnRefs(btn);
      btn.classList.remove('cft-archiving', 'cft-done', 'cft-error');
      try { btn.disabled = false; } catch (_) {}
      setFill(0);
      setLabel(LABEL_DEFAULT);
      try { btn.title = ''; } catch (_) {}
    } catch (e) { warn('resetButton failed', e); }
  }

  function enterBusyState(text) {
    try {
      clearRestoreTimer();
      btnBusy = true;
      var btn = getButton();
      if (!btn) return;
      setBtnRefs(btn);
      btn.classList.add('cft-archiving');
      btn.classList.remove('cft-done', 'cft-error');
      try { btn.disabled = true; } catch (_) {}
      setFill(0);
      setLabel(text || '准备归档…');
    } catch (e) { warn('enterBusyState failed', e); }
  }

  // progress {percent, text, level}（level==='error' 时按钮标红）
  function applyProgress(p) {
    try {
      var btn = getButton();
      if (!btn) return;
      setBtnRefs(btn);
      btnBusy = true;
      try { btn.disabled = true; } catch (_) {}
      btn.classList.add('cft-archiving');
      var pct = setFill(p && p.percent);
      var text = (p && typeof p.text === 'string' && p.text) ? p.text : '归档中…';
      setLabel(text + ' ' + pct + '%');
      if (p && p.level === 'error') btn.classList.add('cft-error');
      else btn.classList.remove('cft-error');
    } catch (e) { warn('applyProgress failed', e); }
  }

  // archiveDone {ok, dirName, msg, errors}：展示结果 3s 后恢复可点击
  function applyArchiveDone(r) {
    try {
      var btn = getButton();
      if (!btn) return;
      setBtnRefs(btn);
      clearRestoreTimer();
      btn.classList.remove('cft-archiving');
      try { btn.disabled = true; } catch (_) {}
      if (r && r.ok) {
        btn.classList.remove('cft-error');
        btn.classList.add('cft-done');
        setFill(100);
        setLabel('✓ 已归档 ' + (r.dirName || ''));
        showToast('归档完成：' + (r.dirName || ''), false);
      } else {
        btn.classList.remove('cft-done');
        btn.classList.add('cft-error');
        setLabel('✗ ' + ((r && r.msg) || '归档失败'));
        showToast('归档失败：' + ((r && r.msg) || '未知错误'), true);
      }
      restoreTimer = setTimeout(function () { resetButton(); }, RESTORE_DELAY_MS);
    } catch (e) { warn('applyArchiveDone failed', e); }
  }

  function onArchiveClick() {
    try {
      if (btnBusy) return;
      enterBusyState('准备归档…');
      if (typeof chrome === 'undefined' || !chrome.runtime) {
        applyArchiveDone({ ok: false, msg: '扩展运行时不可用' });
        return;
      }
      chrome.runtime.sendMessage(
        { __cft: true, to: 'bg', type: 'startArchive', payload: {} },
        function () {
          try {
            if (chrome.runtime.lastError) {
              applyArchiveDone({ ok: false, msg: '后台无响应' });
            }
            // bg 侧按契约无答复：保持进度态，等待 progress/archiveDone 事件驱动
          } catch (e) { warn('startArchive ack failed', e); }
        }
      );
    } catch (e) { warn('onArchiveClick failed', e); }
  }

  function ensureArchiveButton() {
    try {
      var d = doc();
      if (!d || !d.body) return;
      if (!isModelPath(safePathname())) {
        var stale = getButton();
        if (stale) { try { stale.remove(); } catch (_) {} setBtnRefs(null); btnBusy = false; }
        return;
      }
      if (getButton()) return;
      var btn = d.createElement('button');
      btn.id = BTN_ID;
      btn.type = 'button';
      btn.className = 'cft-archive-btn';
      btn.title = '一键归档当前模型页';
      var fill = d.createElement('span');
      fill.className = 'cft-archive-fill';
      var label = d.createElement('span');
      label.className = 'cft-archive-label';
      label.textContent = LABEL_DEFAULT;
      btn.appendChild(fill);
      btn.appendChild(label);
      btn.addEventListener('click', onArchiveClick);
      d.body.appendChild(btn);
      setBtnRefs(btn);
    } catch (e) { warn('ensureArchiveButton failed', e); }
  }

  /* ============ 文件选择弹窗（脚本 2572-2607 搬运，样式改 css 类） ============ */
  // 解析为 fileName 字符串；取消/关闭/Esc/点击遮罩 → ''
  function showFileSelectionDialog(files) {
    return new Promise(function (resolve) {
      try {
        var d = doc();
        if (!d || !d.body) { resolve(''); return; }
        var settled = false;
        var done = function (name) {
          if (settled) return;
          settled = true;
          try { overlay.remove(); } catch (_) {}
          try { d.removeEventListener('keydown', onKey); } catch (_) {}
          resolve(String(name || ''));
        };
        var overlay = d.createElement('div');
        overlay.className = 'cft-sel-overlay';
        var box = d.createElement('div');
        box.className = 'cft-sel-box';
        var title = d.createElement('h3');
        title.className = 'cft-sel-title';
        title.textContent = '请选择要归档的模型文件';
        box.appendChild(title);
        (files || []).forEach(function (f) {
          var b = d.createElement('button');
          b.type = 'button';
          b.className = 'cft-sel-item';
          var nm = (f && f.name) || JSON.stringify(f);
          b.textContent = nm;
          b.title = nm;
          b.addEventListener('click', function () { done(f && f.name ? f.name : ''); });
          box.appendChild(b);
        });
        var cancel = d.createElement('button');
        cancel.type = 'button';
        cancel.className = 'cft-sel-cancel';
        cancel.textContent = '取消';
        cancel.addEventListener('click', function () { done(''); });
        box.appendChild(cancel);
        overlay.appendChild(box);
        overlay.addEventListener('click', function (ev) { if (ev.target === overlay) done(''); });
        var onKey = function (ev) { if (ev && ev.key === 'Escape') done(''); };
        d.addEventListener('keydown', onKey);
        d.body.appendChild(overlay);
      } catch (e) { warn('showFileSelectionDialog failed', e); resolve(''); }
    });
  }

  /* ================= 消息处理 ================= */

  function runtimeSend(env) {
    try {
      if (typeof chrome !== 'undefined' && chrome.runtime) {
        chrome.runtime.sendMessage(env, function () {
          try { void chrome.runtime.lastError; } catch (_) {}
        });
      }
    } catch (e) { warn('runtimeSend failed', e); }
  }

  // 答复：sendResponse（主通道）+ 同信封广播（备用，offscreen 任选其一消费，以 cid 去重）
  function answer(to, type, payload, sendResponse) {
    var env = { __cft: true, to: to, type: type, payload: payload || {} };
    try { if (typeof sendResponse === 'function') sendResponse(env); }
    catch (e) { warn('sendResponse failed', e); }
    if (type !== 'collectPage') runtimeSend(env); // collectPage 复用请求 type，广播会造成回环，只走 sendResponse
  }

  function onRuntimeMessage(msg, sender, sendResponse) {
    try {
      if (!msg || msg.__cft !== true || msg.to !== 'content') return; // 非本模块消息
      var payload = (msg && msg.payload) || {};
      switch (msg.type) {
        case 'collectPage': {
          var data = collectPageData();
          data.cid = payload.cid;
          answer('bg', 'collectPage', data, sendResponse);
          return;
        }
        case 'askFileSelection': {
          var cid1 = payload.cid;
          showFileSelectionDialog(payload.files).then(function (fileName) {
            answer('offscreen', 'userSelection', { cid: cid1, fileName: fileName }, sendResponse);
          });
          return true; // 异步响应
        }
        case 'askArchiveConfirm': {
          var proceed = false;
          try {
            if (typeof confirm === 'function') proceed = confirm(buildArchiveConfirmText(payload.record));
          } catch (e) { warn('confirm failed', e); }
          answer('offscreen', 'archiveConfirm', { cid: payload.cid, proceed: !!proceed }, sendResponse);
          return;
        }
        case 'requestDomComments': {
          var results = collectDomComments();
          answer('offscreen', 'domComments', { cid: payload.cid, results: results }, sendResponse);
          return;
        }
        case 'progress': {
          applyProgress(payload);
          return;
        }
        case 'archiveDone': {
          applyArchiveDone(payload);
          return;
        }
        case 'savedImageDone': {
          return; // 模型页无需处理，安全跳过
        }
        default: return; // 未知类型：不占用 sendResponse
      }
    } catch (e) { warn('onRuntimeMessage failed', e); }
  }

  /* ================= 挂载（SPA 路由变化时确保按钮存在） ================= */

  function wrapHistory(type) {
    try {
      if (typeof history === 'undefined' || !history[type]) return;
      var original = history[type];
      history[type] = function () {
        var r = original.apply(this, arguments);
        setTimeout(function () { ensureArchiveButton(); }, 600);
        return r;
      };
    } catch (e) { warn('wrapHistory failed', e); }
  }

  function init() {
    try {
      var d = doc();
      if (!d) return;
      ensureArchiveButton();
      setInterval(function () {
        try { ensureArchiveButton(); } catch (e) { warn('ensure tick failed', e); }
      }, ENSURE_INTERVAL_MS);
      wrapHistory('pushState');
      wrapHistory('replaceState');
      try {
        window.addEventListener('popstate', function () {
          setTimeout(function () { ensureArchiveButton(); }, 600);
        });
      } catch (_) { /* 忽略 */ }
      if (typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.onMessage) {
        chrome.runtime.onMessage.addListener(onRuntimeMessage);
      }
    } catch (e) { warn('init failed', e); }
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      parseCivitaiIdsFromUrl: parseCivitaiIdsFromUrl,
      matchDownloadVersionId: matchDownloadVersionId,
      matchReviewVersionId: matchReviewVersionId,
      pickCodeVersionId: pickCodeVersionId,
      isModelPath: isModelPath,
      buildArchiveConfirmText: buildArchiveConfirmText,
      handleMessage: onRuntimeMessage
    };
  } else {
    init();
  }
})();
