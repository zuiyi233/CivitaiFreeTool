'use strict';
/* CFT content/model-page.js —— 模型页交互（悬浮归档按钮 / 页面采集 / 弹窗应答 / DOM 评论兜底 / 页内同源 GET）
 * 搬运自油猴脚本 v1.7.8：getCivitaiIdsFromPage(1406-1434)、extractAboutThisVersionText(1437-1448)、
 * DOM 评论兜底(1510-1522)、showObjectSelectionDialog(2572-2607)、createButtonProgressController(1354-1405)、
 * askArchiveConfirm 文案(2155-2157)。样式全部走 content.css（cft- 前缀类），本文件无 inline 样式。
 * classic content script：依赖 manifest 按序注入的 globalThis.CFT.util，只读使用。
 * 按钮挂载为事件驱动（路由事件 + body childList 观察）+ 15s 可见性兜底，不做前台轮询。
 */
(function () {
  var BTN_ID = 'cft-archive-btn';
  var STATUS_ID = 'cft-status-chip';
  var MS = (typeof globalThis !== 'undefined' && globalThis.CFT && globalThis.CFT.modelStatus) || null;
  var LABEL_DEFAULT = '🗂 一键归档';
  var RESTORE_DELAY_MS = 3000;
  var ROUTE_SETTLE_MS = 250;     // 路由事件后等 SPA 本轮渲染结束再处理一次
  var ROUTE_WATCH_MS = 5000;     // 纯字符串复查（完全不碰 DOM），补 pushState 拦不到的路由切换
  var ENSURE_BACKSTOP_MS = 15000; // 慢速兜底：漏网的 SPA 路由切换靠它自愈（后台页跳过）

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
        '\n保存目录：' + (r.destDir || r.dirName || '（未知）') +
        '\n\n【确定】= 仍然重新下载（会覆盖原目录内同名文件）\n【取消】= 放弃本次下载';
    } catch (_) { return '该模型版本已归档过，确定重新下载？'; }
  }

  /* ============ 自动化开关（供 AI/无头流程免交互） ============ */

  // 读 cft_config_v1.autoConfirmDuplicate；storage 不可用/读失败一律按 false（保持原生确认）
  function readAutoConfirm() {
    return new Promise(function (resolve) {
      try {
        var st = (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.local) || null;
        if (!st || typeof st.get !== 'function') { resolve(false); return; }
        st.get('cft_config_v1').then(function (raw) {
          var cfg = raw && raw.cft_config_v1;
          resolve(!!(cfg && cfg.autoConfirmDuplicate === true));
        }).catch(function () { resolve(false); });
      } catch (_) { resolve(false); }
    });
  }

  /* ============ pageFetch 纯校验（无 DOM，可在 node 下单测） ============ */

  var PAGE_FETCH_HOSTS = ['civitai.com', 'civitai.red'];
  var PAGE_FETCH_PATHS = ['/api/v1/', '/api/trpc/'];
  var PAGE_FETCH_REJECT = 'url 不在白名单';
  // 两种拒绝必须分开说：白名单拒绝是扩展一个字节都没发出去；重定向拒发是站点确实改了跳转目标、
  // 页面通道不跟发（跟发就可能拿登录 Cookie 打到白名单外）。调用方文案与排错方向完全不同。
  var PAGE_FETCH_REDIRECT_REJECT = '站点返回重定向，页面通道不跟随（重定向目标未经白名单复核）';
  var PAGE_FETCH_DEFAULT_TIMEOUT_MS = 20000;
  var PAGE_FETCH_MAX_BODY_CHARS = 3 * 1024 * 1024; // 模型详情约 16KB，上限只为防跑飞响应打爆消息通道
  var PAGE_FETCH_CLAIM_CAP = 128;                  // 已应答 cid 记忆表容量（防无界增长）

  // 白名单：https + 无端口/账号 + 域名剥掉前导 www. 后精确等于 civitai.com/.red + 路径前缀受限。
  // 该校验必须落在本文件里做——请求会带上用户登录 Cookie，绝不能拿它去取调用方随手给的任意 URL。
  // 返回 '' 表示放行；否则返回统一拒绝原因。
  function pageFetchRejectReason(rawUrl) {
    try {
      var u = new URL(String(rawUrl == null ? '' : rawUrl));
      if (u.protocol !== 'https:') return PAGE_FETCH_REJECT;
      var host = String(u.hostname || '').toLowerCase();
      if (host.indexOf('www.') === 0) host = host.slice(4);
      if (PAGE_FETCH_HOSTS.indexOf(host) < 0) return PAGE_FETCH_REJECT;
      if (u.port) return PAGE_FETCH_REJECT;
      if (u.username || u.password) return PAGE_FETCH_REJECT;
      var path = String(u.pathname || '');
      for (var i = 0; i < PAGE_FETCH_PATHS.length; i++) {
        if (path.indexOf(PAGE_FETCH_PATHS[i]) === 0) return '';
      }
      return PAGE_FETCH_REJECT;
    } catch (_) { return PAGE_FETCH_REJECT; }
  }

  // 失败原因压成短字符串（不外抛、不把整个 Error 对象塞进消息通道）
  function shortErrText(e) {
    try {
      var s = String((e && (e.message || e.name)) || e || 'unknown').slice(0, 80);
      return 'fetch 失败: ' + s;
    } catch (_) { return 'fetch 异常'; }
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
  var busyWatch = 0;        // 首帧看门狗：进忙态后迟迟没有 progress/archiveDone 就自己解套（见 armBusyWatch）
  var btnMounted = false;   // 按钮当前是否在 DOM 里（由 body childList 观察者维护）
  var handledPath = null;   // 上次处理完（挂载或确认卸载）的 pathname：路由没变则 ensure 直接早退

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

  function clearBusyWatch() {
    try { if (busyWatch) clearTimeout(busyWatch); } catch (_) {}
    busyWatch = 0;
  }

  // 首帧看门狗：消息链路一旦断在 offscreen/BG 之间，页面会永远停在「准备归档…」——
  // 用户看到的只是一个死掉的按钮，没有任何原因。这里在进入忙态时上表，收到首条 progress
  // 即撤表（长下载期间进度可能稀疏，所以只盯首帧，不用固定总时长）。
  var BUSY_FIRST_FRAME_MS = 45000;
  function armBusyWatch() {
    clearBusyWatch();
    busyWatch = setTimeout(function () {
      busyWatch = 0;
      if (!btnBusy) return;
      try { warn('archive first frame timeout'); } catch (_) {}
      try {
        var btn = getButton();
        if (btn) { btn.classList.add('cft-error'); setLabel('✗ 无响应：请重试'); }
        showToast('归档没有开始：扩展离屏文档未响应，请重试一次', true);
      } catch (_) {}
      resetButton();
    }, BUSY_FIRST_FRAME_MS);
  }

  function resetButton() {
    try {
      clearRestoreTimer();
      clearBusyWatch();
      btnBusy = false;
      var btn = getButton();
      if (!btn) { setBtnRefs(null); btnMounted = false; handledPath = null; return; } // 按钮被页面摘掉：复位，等下一次路由事件/兜底重挂
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
      armBusyWatch();
    } catch (e) { warn('enterBusyState failed', e); }
  }

  // progress {percent, text, level}（level==='error' 时按钮标红）
  function applyProgress(p) {
    try {
      clearBusyWatch();   // 首帧已到，之后按阶段推进，不再用固定时长判死
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

  // archiveDone {ok, dirName, destDir, msg, errors}：展示结果 3s 后恢复可点击
  // destDir 是桌面端按类型分流后的真实绝对落点（老软件不回传 → 空，继续显示 dirName）
  function applyArchiveDone(r) {
    try {
      var btn = getButton();
      if (!btn) return;
      setBtnRefs(btn);
      clearRestoreTimer();
      clearBusyWatch();
      btn.classList.remove('cft-archiving');
      try { btn.disabled = true; } catch (_) {}
      if (r && r.ok) {
        btn.classList.remove('cft-error');
        btn.classList.add('cft-done');
        setFill(100);
        setLabel('✓ 已归档 ' + (r.dirName || ''));
        showToast('归档完成：' + (r.destDir || r.dirName || ''), false);
        setTimeout(function () { requestStatus('force'); }, 900);
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
            var err = chrome.runtime.lastError;
            if (err) {
              warn('startArchive rejected:', err.message || 'unknown'); // 真实原因进控制台，别只留「后台无响应」
              applyArchiveDone({ ok: false, msg: '后台无响应' });
            }
            // bg 侧按契约无答复：保持进度态，等待 progress/archiveDone 事件驱动
          } catch (e) { warn('startArchive ack failed', e); }
        }
      );
    } catch (e) { warn('onArchiveClick failed', e); }
  }

  /* ================= 页面下载状态胶囊 =================
   * 位置在归档按钮正上方。先读扩展缓存立即渲染，超过 TTL 才让 bg 去问本机桥；
   * 点击 = 强制刷新。失败只改胶囊文案，绝不弹错误对话框、不阻断归档/下载。
   */
  var statusReqAt = 0, statusReqBusy = false, statusHandledPath = null, lastStatusVersion = '';

  function getStatusChip() {
    try {
      var d = doc();
      return (d && d.getElementById) ? d.getElementById(STATUS_ID) : null;
    } catch (_) { return null; }
  }

  function sendBgStatus(payload, cb) {
    var done = false;
    var timer = setTimeout(function () {
      if (done) return;
      done = true;
      try { cb(null); } catch (_) {}
    }, 8000);
    try {
      if (typeof chrome === 'undefined' || !chrome.runtime || !chrome.runtime.sendMessage) {
        clearTimeout(timer); done = true; cb(null); return;
      }
      chrome.runtime.sendMessage(
        { __cft: true, to: 'bg', type: 'modelStatusQuery', payload: payload },
        function (r) {
          if (done) return;
          done = true;
          clearTimeout(timer);
          try { void chrome.runtime.lastError; } catch (_) {}
          cb(r || null);
        }
      );
    } catch (e) {
      if (done) return;
      done = true;
      clearTimeout(timer);
      cb(null);
    }
  }

  function renderStatus(entry, opts) {
    try {
      var chip = getStatusChip();
      if (!chip) return;
      var lab = (MS && MS.labelOf) ? MS.labelOf(entry, opts || {}) : null;
      if (!lab) {
        chip.textContent = '? 状态未知';
        chip.className = 'cft-status-chip cft-status-unknown';
        chip.title = '下载状态模块未加载';
        return;
      }
      var offline = !!(entry && entry.offline);
      chip.textContent = lab.badge + ' ' + lab.text + (offline && entry && entry.state !== 'unknown' ? ' · 离线' : '');
      chip.className = 'cft-status-chip cft-status-' + lab.state + (offline ? ' cft-status-offline' : '');
      chip.title = (lab.title || lab.text) + '\n点击强制刷新';
      chip.dataset.state = lab.state;
    } catch (e) { warn('renderStatus failed', e); }
  }

  function requestStatus(mode, opts) {
    try {
      var o = opts || {};
      if (!MS) return;
      var ids = getCivitaiIdsFromPage();
      if (!ids.modelId) {
        renderStatus({ state: 'unknown', reason: '未识别到模型 ID' });
        return;
      }
      var now = Date.now();
      if (!o.versionChanged && mode !== 'force' && now - statusReqAt < 60000) return;
      if (statusReqBusy) return;
      // 后台页/隐藏标签不做联网刷新，但允许读缓存（refresh:false）
      var effective = (mode !== 'force' && isPageHidden()) ? false : mode;
      if (effective === false && !MS.getEntry) return;
      statusReqBusy = true;
      statusReqAt = now;
      var vid = ids.modelVersionId || '';
      var key = vid ? 'version:' + vid : 'model:' + ids.modelId;
      sendBgStatus({
        items: [{ key: key, modelId: ids.modelId, versionId: vid, url: safeLocationHref() }],
        refresh: effective,
      }, function (r) {
        statusReqBusy = false;
        if (!r || r.ok !== true) {
          renderStatus({ state: 'unknown', reason: (r && (r.error || r.msg)) || '扩展后台无响应' },
            { offline: !!(r && r.offline) });
          return;
        }
        var entry = (r.items && r.items[0])
          || (r.byKey && (r.byKey[key] || r.byKey[Object.keys(r.byKey)[0]]));
        if (!entry) {
          renderStatus({ state: 'unknown', reason: 'CFT 没有返回这个模型的状态' });
          return;
        }
        lastStatusVersion = vid;
        renderStatus(entry);
      });
    } catch (e) { warn('requestStatus failed', e); }
  }

  function ensureStatusChip(force) {
    var path = safePathname();
    if (!force && path === statusHandledPath) return;
    try {
      var d = doc();
      if (!d || !d.body) return;
      if (!isModelPath(path)) {
        var stale = getStatusChip();
        if (stale) { try { stale.remove(); } catch (_) {} }
        statusHandledPath = path;
        return;
      }
      var existing = getStatusChip();
      if (existing) { statusHandledPath = path; return; }
      var chip = d.createElement('button');
      chip.id = STATUS_ID;
      chip.type = 'button';
      chip.className = 'cft-status-chip cft-status-loading';
      chip.textContent = '… 查询下载状态';
      chip.title = '查询这个模型在本机的下载状态';
      chip.addEventListener('click', function () {
        requestStatus('force');
        showToast('正在查询下载状态…', false);
      });
      d.body.appendChild(chip);
      statusHandledPath = path;
      requestStatus('auto');
      // 版本 ID 常在首屏后才从下载链接/代码块出现：两次有界复查，避免一直按模型级显示
      [1500, 4000].forEach(function (ms) {
        setTimeout(function () {
          try {
            if (isPageHidden()) return;
            var ids = getCivitaiIdsFromPage();
            if (ids.modelId && ids.modelVersionId && ids.modelVersionId !== lastStatusVersion) {
              requestStatus('auto', { versionChanged: true });
            }
          } catch (_) {}
        }, ms);
      });
    } catch (e) { warn('ensureStatusChip failed', e); statusHandledPath = null; }
  }

  function ensureArchiveButton(force) {
    var path = safePathname();
    // 快速早退：路由没变且状态已稳定（按钮在位 / 已确认该页无按钮）时一次 DOM 查询都不做
    if (!force && path === handledPath) return;
    try {
      var d = doc();
      if (!d || !d.body) return;
      if (!isModelPath(path)) {
        var stale = getButton();
        if (stale) { try { stale.remove(); } catch (_) {} setBtnRefs(null); btnBusy = false; }
        btnMounted = false;
        handledPath = path;
        return;
      }
      var existing = getButton();
      if (existing) {
        if (!btnEls.btn) setBtnRefs(existing); // 进度态引用曾丢失：重新收养，别再插一个
        btnMounted = true;
        handledPath = path;
        return;
      }
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
      btnMounted = true;
      handledPath = path;
    } catch (e) { warn('ensureArchiveButton failed', e); handledPath = null; }
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

  /* ================= pageFetch：借页面同源 + 会话 Cookie 发一次 GET =================
   * 油猴脚本时代所有 Civitai API 调用都在页面里 fetch()，同源、带 Cookie，所以评论/测评/详情图都能拿到。
   * 移植后这些请求换到 offscreen（chrome-extension:// 源、跨站、匿名），tRPC 回 401
   * {"message":"Please use the public API instead"}，公开 API 的 NSFW 结果被清成 {"items":[]}。
   * 本处理器就是给其它上下文一个"回到页面里发这个请求"的通道。
   */

  // 同一文档里若两个 content script 都收到同一条请求：先认领 cid 者应答，另一个完全不碰通道。
  // 共享表挂在 globalThis（与 manifest 按序注入的 CFT 同一隔离世界，跨文件可见）。
  function claimPageFetchCid(cid) {
    try {
      if (!cid) return true; // 无 cid 无从去重，照常应答（契约要求调用方必带 cid）
      if (typeof globalThis === 'undefined') return true;
      if (!globalThis.__cftPageFetchCids || typeof globalThis.__cftPageFetchCids.set !== 'function') {
        globalThis.__cftPageFetchCids = new Map();
      }
      var m = globalThis.__cftPageFetchCids;
      if (m.has(cid)) return false;
      m.set(cid, Date.now());
      if (m.size > PAGE_FETCH_CLAIM_CAP) {
        var drop = m.size - Math.floor(PAGE_FETCH_CLAIM_CAP / 2);
        var n = 0;
        m.forEach(function (_v, k) { if (n++ < drop) m.delete(k); }); // 丢最早的半数，够用即可
      }
      return true;
    } catch (_) { return true; }
  }

  // 无论白名单拒绝、超时、还是 fetch 抛异常，都必须且只应答一次 pageFetchResult。
  function runPageFetch(payload, sendResponse) {
    var p = (payload && typeof payload === 'object') ? payload : {};
    var cid = String(p.cid == null ? '' : p.cid);
    var replied = false;
    var timer = 0;
    var settle = function (r) {
      if (replied) return;
      replied = true;
      try { if (timer) clearTimeout(timer); } catch (_) { /* 忽略 */ }
      var out = r || {};
      answer('offscreen', 'pageFetchResult', {
        cid: cid,
        ok: !!out.ok,
        status: (typeof out.status === 'number' && isFinite(out.status)) ? out.status : 0,
        body: typeof out.body === 'string' ? out.body : '',
        err: out.err ? String(out.err) : null,
        truncated: !!out.truncated
      }, sendResponse);
    };
    try {
      if (!cid) { settle({ err: '缺少 cid' }); return; }
      var reject = pageFetchRejectReason(p.url);
      if (reject) { settle({ err: reject }); return; } // 白名单不通过：一个字节都不请求
      var timeoutMs = Number(p.timeoutMs);
      if (!isFinite(timeoutMs) || timeoutMs <= 0) timeoutMs = PAGE_FETCH_DEFAULT_TIMEOUT_MS;
      var controller = null;
      try { controller = (typeof AbortController === 'function') ? new AbortController() : null; } catch (_) { controller = null; }
      if (controller) {
        try {
          timer = setTimeout(function () { try { controller.abort(); } catch (_) { /* 忽略 */ } }, timeoutMs);
        } catch (_) { timer = 0; }
      }
      // 不跟随重定向，且用 redirect:'manual' 而不是 'error'：Chromium 实测（headless Chromium +
      // 本地 302/307 服务）'error' 收到 3xx 抛的是 TypeError("Failed to fetch")，与 CORS/网络失败
      // 同一句话，应答里只能说"可能是重定向"；'manual' 把 3xx 变成 opaqueredirect(status 0)，
      // 既不向跳转目标发出第二个请求（实测目标站命中 0 次、200 响应不受影响），又能把
      // "被重定向拒发"与"白名单拒绝"分开写清楚。
      fetch(String(p.url), {
        method: 'GET',
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
        redirect: 'manual',
        signal: controller ? controller.signal : undefined
      }).then(function (res) {
        var status = (res && typeof res.status === 'number') ? res.status : 0;
        var ok = !!(res && res.ok);
        // 站点把请求重定向了（307 预签名跳转就是这个形状）：目标地址可能已跳出白名单，
        // 所以这里绝不跟发，也绝不把 status 0 的空体当成"成功但没数据"
        if (res && res.type === 'opaqueredirect') {
          settle({ ok: false, status: 0, err: PAGE_FETCH_REDIRECT_REJECT });
          return;
        }
        return res.text().then(function (raw) {
          var text = String(raw == null ? '' : raw);
          var truncated = text.length > PAGE_FETCH_MAX_BODY_CHARS;
          settle({
            ok: ok, status: status,
            body: truncated ? text.slice(0, PAGE_FETCH_MAX_BODY_CHARS) : text,
            err: null, truncated: truncated
          });
        });
      }).catch(function (e) {
        var aborted = !!(controller && controller.signal && controller.signal.aborted) ||
          !!(e && String(e.name) === 'AbortError');
        settle({ err: aborted ? '请求超时 ' + timeoutMs + 'ms' : shortErrText(e) });
      });
    } catch (e) {
      settle({ err: shortErrText(e) }); // 处理器绝不把异常抛给 onMessage
    }
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
        case 'ping': {
          // 后台重注入前的探针：有回音说明脚本还在，别再注一次
          answer('bg', 'pong', { ok: true, injected: !!window.__cftModelPageInjected }, sendResponse);
          return;
        }
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
          var cid0 = payload.cid;
          var record = payload.record;
          // 自动化开关（cft_config_v1.autoConfirmDuplicate）：原生 confirm 会卡住任何无人值守
          // 流程（自动化/无头环境默认 dismiss），开启后直接按「继续」处理，其余行为不变。
          readAutoConfirm().then(function (auto) {
            var proceed = auto === true;
            if (!proceed) {
              try {
                if (typeof confirm === 'function') proceed = confirm(buildArchiveConfirmText(record));
              } catch (e) { warn('confirm failed', e); }
            }
            answer('offscreen', 'archiveConfirm', { cid: cid0, proceed: !!proceed }, sendResponse);
          });
          return true; // 异步响应（要等 storage 读回来）
        }
        case 'requestDomComments': {
          var results = collectDomComments();
          answer('offscreen', 'domComments', { cid: payload.cid, results: results }, sendResponse);
          return;
        }
        case 'pageFetch': {
          if (!claimPageFetchCid(String(payload.cid == null ? '' : payload.cid))) {
            return; // 同文档已有应答方：不重复 answer，也不占用消息通道
          }
          runPageFetch(payload, sendResponse);
          return true; // 异步响应（与 askFileSelection 同款：fetch 完成前保持通道打开）
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
        case 'modelStatusPush': {
          renderStatus(payload.entry);   // popup/后台刷新后即时更新胶囊，不再多打一轮本机桥
          return;
        }
        default: return; // 未知类型：不占用 sendResponse
      }
    } catch (e) { warn('onRuntimeMessage failed', e); }
  }

  /* ========== 挂载：事件驱动为主 + 慢速兜底（不再前台轮询） ========== */
  // 即时：pushState/replaceState 包装 + popstate/hashchange + body 直接子节点增删
  //（整页重渲染、portal、toast）+ visibilitychange；
  // 复查：5s 只比 pathname 字符串（零 DOM 访问），15s 才做一次 getElementById 在位校验。
  // 已知局限：隔离世界里改写 history.pushState 拦不到页面主世界的调用，漏网的路由切换靠复查。

  var routeTimer = 0;
  var backstopTimer = 0;
  var routeWatchTimer = 0;
  var bodyObserver = null;
  var eventsArmed = false;

  function wrapHistory(type) {
    try {
      if (typeof history === 'undefined' || !history[type]) return;
      var original = history[type];
      history[type] = function () {
        var r = original.apply(this, arguments);
        scheduleEnsure();
        return r;
      };
    } catch (e) { warn('wrapHistory failed', e); }
  }

  function isPageHidden() {
    try { return !!(typeof document !== 'undefined' && document.hidden); } catch (_) { return false; }
  }

  function buttonDetached() {
    try { return !!(btnMounted && btnEls.btn && btnEls.btn.isConnected === false); } catch (_) { return false; }
  }

  function runEnsure() {
    try { ensureArchiveButton(); } catch (e) { warn('ensure failed', e); }
    try { ensureStatusChip(); } catch (e) { warn('status ensure failed', e); }
  }

  // 弹层/后台写缓存后同步页面胶囊：只读 storage，不联网
  function onStatusStorage(changes, area) {
    try {
      if (area !== 'local' || !MS || !MS.getEntry || !changes || !changes[MS.CACHE_KEY]) return;
      var ids = getCivitaiIdsFromPage();
      var entry = MS.getEntry(changes[MS.CACHE_KEY].newValue, ids.modelId, ids.modelVersionId);
      if (entry) renderStatus(entry);
    } catch (e) { warn('status storage sync failed', e); }
  }

  // 同一轮 SPA 渲染里的连续路由事件合并成一次处理
  function scheduleEnsure() {
    try {
      if (routeTimer) return;
      routeTimer = setTimeout(function () { routeTimer = 0; runEnsure(); }, ROUTE_SETTLE_MS);
    } catch (e) { warn('scheduleEnsure failed', e); }
  }

  function onBodyMutations() {
    if (buttonDetached()) { btnMounted = false; handledPath = null; } // 页面把按钮摘掉了：立即重挂
    runEnsure();
  }

  function armBodyObserver() {
    try {
      var d = doc();
      if (bodyObserver || !d || !d.body || typeof MutationObserver === 'undefined') return;
      bodyObserver = new MutationObserver(onBodyMutations);
      bodyObserver.observe(d.body, { childList: true }); // 只观察 body 直接子节点，不观察子树
    } catch (e) { warn('armBodyObserver failed', e); bodyObserver = null; }
  }

  function armBackstop() {
    try {
      if (backstopTimer) return;
      backstopTimer = setInterval(function () {
        try {
          if (isPageHidden()) return;      // 后台标签页零工作
          armBodyObserver();               // body 曾缺失的异常时序在这里补挂观察者（幂等）
          ensureArchiveButton(true);       // 强制校验：一次 getElementById
        } catch (e) { warn('ensure backstop failed', e); }
      }, ENSURE_BACKSTOP_MS);
    } catch (e) { warn('armBackstop failed', e); }
  }

  // 5s 复查只做 pathname 字符串比较（不查询 DOM），补上主世界 pushState 的拦不到的缺口
  function armRouteWatch() {
    try {
      if (routeWatchTimer) return;
      routeWatchTimer = setInterval(function () {
        try {
          if (isPageHidden() || safePathname() === handledPath) return;
          ensureArchiveButton();
        } catch (e) { warn('route watch failed', e); }
      }, ROUTE_WATCH_MS);
    } catch (e) { warn('armRouteWatch failed', e); }
  }

  function arm() {
    armBodyObserver();
    armBackstop();
    armRouteWatch();
  }

  function disarm() {
    try { if (backstopTimer) clearInterval(backstopTimer); } catch (_) {}
    backstopTimer = 0;
    try { if (routeWatchTimer) clearInterval(routeWatchTimer); } catch (_) {}
    routeWatchTimer = 0;
    try { if (bodyObserver) bodyObserver.disconnect(); } catch (_) {}
    bodyObserver = null;
  }

  function revalidate() {
    handledPath = null; // 允许隐藏期间漏掉的路由切换在下一次可见时立即纠正
    runEnsure();
    arm();
  }

  function armWindowEvents() {
    if (eventsArmed) return;
    eventsArmed = true;
    try {
      window.addEventListener('popstate', scheduleEnsure);
      window.addEventListener('hashchange', scheduleEnsure);
      window.addEventListener('pageshow', revalidate);      // bfcache 恢复：重新挂观察者/兜底
      window.addEventListener('pagehide', disarm);          // 页面离开：清定时器 + 断观察者
    } catch (_) { /* 忽略 */ }
    try {
      var d = doc();
      if (d) d.addEventListener('visibilitychange', function () {
        if (isPageHidden()) return;
        revalidate();
        if (Date.now() - statusReqAt > 60000) requestStatus('auto');
      });
    } catch (_) { /* 忽略 */ }
  }

  function init() {
    try {
      var d = doc();
      if (!d) return;
      runEnsure();
      wrapHistory('pushState');
      wrapHistory('replaceState');
      armWindowEvents();
      arm();
      if (typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.onMessage) {
        chrome.runtime.onMessage.addListener(onRuntimeMessage);
      }
      if (typeof chrome !== 'undefined' && chrome.storage && chrome.storage.onChanged) {
        chrome.storage.onChanged.addListener(onStatusStorage);
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
      pageFetchRejectReason: pageFetchRejectReason,
      handleMessage: onRuntimeMessage
    };
  } else {
    // 后台可能重注入同一份脚本（扩展重载/更新后旧脚本已被杀）：只允许一个胶囊
    if (window.__cftModelPageInjected) { /* 已注入过，跳过 */ }
    else { window.__cftModelPageInjected = true; init(); }
  }
})();
