'use strict';
/* CFT content/image-badge.js —— images/posts 页 SAVE 徽章（P3）+ 页内同源 GET（pageFetch）
 * 搬运自油猴脚本 v1.7.8：loadSavedImages(2616-2628，localStorage→CFT.idb 只读)、
 * addImageBadges(2638-2691，originalUrl 构造/SAVED-SAVE 状态/rAF 防重入)、
 * 徽章点击后的 saveSingleImage 请求封装（downloadSingleImage 2694-2800 的意图，
 * 实际下载在 offscreen；filePrefix 由 offscreen 处理，本文件不传 prefix）。
 * 不搬：persistData（只读不写）、downloadSingleImage/downloadBlob 本体、
 * fetchAndCacheModelData/scanModelVersionId（调用点确认仅服务徽章文件名前缀，
 * 前缀已移交 offscreen，content 侧无用）、enqueueDownload 串行队列（单图请求
 * 走 bg/offscreen 侧排队，content 只发消息）。
 * classic content script：样式全部走 content.css（cft- 前缀类），本文件无 inline 样式。
 * 徽章挂载为事件驱动（mutation 批次只入队新增节点）+ 20s 可见性兜底；路由离开 images/posts
 * 即断开观察者，路由复查阶段只比字符串、不碰 DOM。
 */
(function () {
  var SAVE_KEY = 'savedImages';
  var SCAN_BACKSTOP_MS = 20000;  // 慢速兜底全表扫描（仅可见标签页），漏网的 SPA 转换靠它自愈
  var ROUTE_WATCH_MS = 5000;     // 仅「路由已离开 images/posts、观察者已断开」时运行：纯字符串复查
  var DIRTY_CAP = 64;            // 单帧待查锚点上限，超出退化成一次全表扫描
  var DIRTY_ROOT_CAP = 24;       // 单帧待查容器上限，同上
  var FAIL_REVERT_MS = 2000;

  var savedSet = {}; // {[imageId]: true}
  var badgeState = {}; // {[imageId]: 'saving'|'saved'|'error'}

  var observer = null;      // body childList(subtree) 观察者：只负责把新增节点入队
  var scanTimer = 0;        // 慢速全表扫描兜底
  var routeTimer = 0;       // 路由复查（无 DOM 访问）
  var dirtyAnchors = [];    // 本帧待查的 <a>
  var dirtyRoots = [];      // 本帧待查的新增容器
  var needFullScan = true;  // 首帧 / 路由变化 / 兜底 → 全表扫描
  var scanScheduled = false;
  var handledPath = '';     // 上次扫描时的 pathname
  var eventsArmed = false;  // window/document 事件只挂一次

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

  // 当前文档 URL：单图归档必须带上它（契约 §2.1 硬约束）。offscreen 用它决定
  // ① 元数据请求打到 civitai.com 还是 civitai.red（镜像站数据独立，选错站就是拿另一个站的图）
  // ② 能不能借本标签页发同源请求（host 判不出就不借，回落匿名直连）。
  // bg 拿得到 sender.tab.url 会兜底，但那是"请求来自哪个 tab"的近似值；发起文档自己报最准。
  function currentPageUrl() {
    try {
      if (typeof window !== 'undefined' && window.location && window.location.href) return String(window.location.href);
    } catch (_) { /* 忽略 */ }
    try {
      var d = doc();
      if (d && (d.URL || d.documentURI)) return String(d.URL || d.documentURI);
    } catch (_) { /* 忽略 */ }
    return '';
  }

  // 徽章点击 → bg 的 saveSingleImage 信封（纯函数，便于在 node 下钉住 payload 形状）
  function buildSaveSingleImageRequest(ctx, pageUrl) {
    var c = ctx || {};
    return {
      __cft: true,
      to: 'bg',
      type: 'saveSingleImage',
      payload: {
        imageId: String(c.imageId == null ? '' : c.imageId),
        originalUrl: String(c.originalUrl == null ? '' : c.originalUrl),
        ext: String(c.ext == null ? '' : c.ext),
        pageUrl: String(pageUrl == null ? '' : pageUrl),
      },
    };
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

  /* ============ pageFetch 纯校验（无 DOM，可在 node 下单测） ============ */
  // 与 model-page.js 中的同名实现保持逐字一致：两个 content script 各自服务不同路径，
  // 都必须能独立应答 pageFetch，而 lib/ 归他人所有，不能把公共实现搬出去。

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

  function isPageHidden() {
    try { return !!(typeof document !== 'undefined' && document.hidden); } catch (_) { return false; }
  }

  // 本脚本只在 images/posts 页有意义（manifest matches）：SPA 路由离开后就没有待挂目标了
  function isBadgePath(pathname) {
    return /^\/(images|posts)(\/|$)/.test(String(pathname || ''));
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
        buildSaveSingleImageRequest(ctx, currentPageUrl()),
        function (resp) {
          var err = null;
          try { err = chrome.runtime.lastError; } catch (_) { /* 忽略 */ }
          if (err) {
            warn('saveSingleImage rejected:', err.message || 'unknown'); // 真实原因进控制台，别只留「后台无响应」
            onSaveResult(ctx, { ok: false, msg: '后台无响应' });
            return;
          }
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
  // 扫描分两种：全表扫描（首帧 / 路由变化 / 慢速兜底）与增量扫描（mutation 批次里的新增节点）。
  function addImageBadges() {
    needFullScan = true;
    scheduleScan();
  }

  function scheduleScan() {
    if (scanScheduled) return;
    scanScheduled = true;
    scheduleFrame(function () {
      try { runScan(); } catch (e) { warn('scan failed', e); }
      finally { scanScheduled = false; }
    });
  }

  function currentLoc() {
    try { return (typeof window !== 'undefined' && window.location) || null; } catch (_) { return null; }
  }

  function safePathname() {
    var loc = currentLoc();
    return (loc && loc.pathname) ? String(loc.pathname) : '';
  }

  // 单个锚点：与旧版循环体等价，命中就挂徽章（已挂过的靠 data-cft-badge-anchor 早退）
  function scanAnchor(a, loc) {
    try {
      if (!a || a.nodeType !== 1) return;
      if (a.getAttribute('data-cft-badge-anchor') === 'true') return;
      var img = a.querySelector ? a.querySelector('img') : null;
      if (!img) return;
      var imageId = getImageIdFromHref(a.getAttribute('href'), loc);
      if (!imageId) return;
      var built = buildOriginalUrl(img.src, imageId);
      if (!built) { warn('解析图片URL失败', img.src); return; }
      attachBadge(a, imageId, built, loc);
    } catch (e) { warn('scan anchor failed', e); }
  }

  // 在 root（document 或新增容器）子树内查锚点
  function scanWithin(root, loc) {
    try {
      if (!root || !root.querySelectorAll) return;
      var anchors = root.querySelectorAll('a[href*="/images/"]');
      for (var i = 0; i < anchors.length; i++) scanAnchor(anchors[i], loc);
    } catch (e) { warn('scan within failed', e); }
  }

  function runScan() {
    var d = doc();
    if (!d) { dirtyAnchors.length = 0; dirtyRoots.length = 0; return; }
    var path = safePathname();
    if (path !== handledPath) { handledPath = path; needFullScan = true; }
    if (!isBadgePath(path)) { // SPA 把路由带走了：本帧起本页再无相关目标
      dirtyAnchors.length = 0;
      dirtyRoots.length = 0;
      needFullScan = true;
      stopObserving();
      return;
    }
    startObserving(); // 路由回到 images/posts：幂等重挂观察者
    var loc = currentLoc();
    if (needFullScan) {
      needFullScan = false;
      dirtyAnchors.length = 0;
      dirtyRoots.length = 0;
      scanWithin(d, loc);
      return;
    }
    var anchors = dirtyAnchors; dirtyAnchors = [];
    var roots = dirtyRoots; dirtyRoots = [];
    for (var i = 0; i < anchors.length; i++) {
      if (anchors[i].isConnected !== false) scanAnchor(anchors[i], loc);
    }
    for (var j = 0; j < roots.length; j++) {
      if (roots[j].isConnected !== false) scanWithin(roots[j], loc);
    }
  }

  /* ================= mutation → 增量队列（每批只做常数次判断） ================= */

  function pushDirtyAnchor(a) {
    if (dirtyAnchors.length >= DIRTY_CAP) { needFullScan = true; dirtyAnchors.length = 0; return; }
    if (dirtyAnchors.indexOf(a) < 0) dirtyAnchors.push(a);
  }

  function pushDirtyRoot(n) {
    if (dirtyRoots.length >= DIRTY_ROOT_CAP) { needFullScan = true; dirtyRoots.length = 0; return; }
    dirtyRoots.push(n);
  }

  function queueAdded(node) {
    if (!node || node.nodeType !== 1) return; // 文本/注释节点：零成本跳过
    var tag = String(node.tagName || '').toUpperCase();
    if (tag === 'DIV' && String(node.className || '').indexOf('cft-badge') >= 0) return; // 自己插的徽章
    if (tag === 'A') { pushDirtyAnchor(node); return; }
    if (tag === 'IMG') { // <img> 在既有 <a> 内延迟挂载：定位到所在锚点
      var host = node.closest ? node.closest('a[href*="/images/"]') : null;
      if (host) pushDirtyAnchor(host);
      return;
    }
    pushDirtyRoot(node); // 容器插入：只扫这棵新增子树
  }

  function onMutations(records) {
    try {
      if (isPageHidden()) { needFullScan = true; return; } // 后台页不攒队列，等可见时一次全表扫
      for (var i = 0; i < records.length; i++) {
        var added = records[i].addedNodes;
        for (var j = 0; j < added.length; j++) queueAdded(added[j]);
      }
      if (needFullScan || dirtyAnchors.length || dirtyRoots.length) scheduleScan();
    } catch (e) { warn('onMutations failed', e); }
  }

  /* ================= 观察者 / 兜底 ================= */

  function startObserving() {
    try {
      var d = doc();
      if (observer || !d || !d.body || typeof MutationObserver === 'undefined') return;
      observer = new MutationObserver(onMutations);
      // 只关心「有新节点插入」：虚拟列表加图必然走 childList，属性/文本变动不再触发全表扫描
      observer.observe(d.body, { childList: true, subtree: true });
    } catch (e) { warn('observer failed', e); observer = null; }
  }

  function stopObserving() {
    try { if (observer) observer.disconnect(); } catch (_) {}
    observer = null;
    clearScanTimer();
    startRouteWatch(); // 路由回到 images/posts 时靠它（纯字符串比较）复活
  }

  function clearScanTimer() {
    try { if (scanTimer) clearInterval(scanTimer); } catch (_) {}
    scanTimer = 0;
  }

  function armScanTimer() {
    try {
      if (scanTimer) return;
      scanTimer = setInterval(function () {
        try {
          if (isPageHidden()) return;                     // 后台标签页零工作
          if (!isBadgePath(safePathname())) { stopObserving(); return; } // 漏网的路由离开：停观察
          addImageBadges();                               // 漏网的 SPA 新图靠这一次全表扫自愈
        } catch (e) { warn('scan backstop failed', e); }
      }, SCAN_BACKSTOP_MS);
    } catch (e) { warn('armScanTimer failed', e); }
  }

  function startRouteWatch() {
    try {
      if (routeTimer) return;
      routeTimer = setInterval(function () {
        try {
          if (!isBadgePath(safePathname())) return; // 只比字符串，不碰 DOM
          stopRouteWatch();
          armActive();
        } catch (_) { /* 忽略 */ }
      }, ROUTE_WATCH_MS);
    } catch (e) { warn('startRouteWatch failed', e); }
  }

  function stopRouteWatch() {
    try { if (routeTimer) clearInterval(routeTimer); } catch (_) {}
    routeTimer = 0;
  }

  // 路由处于 images/posts：观察者 + 慢速兜底 + 立即重扫
  function armActive() {
    stopRouteWatch();
    startObserving();
    armScanTimer();
    addImageBadges();
  }

  function onVisibilityChange() {
    try {
      if (isPageHidden()) return;
      if (isBadgePath(safePathname())) { needFullScan = true; scheduleScan(); }
      else stopObserving();
    } catch (e) { warn('visibility change failed', e); }
  }

  function teardown() { // 页面离开：定时器与观察者全部释放
    stopRouteWatch();
    clearScanTimer();
    try { if (observer) observer.disconnect(); } catch (_) {}
    observer = null;
    handledPath = '';
  }

  function armWindowEvents() {
    if (eventsArmed) return;
    eventsArmed = true;
    try {
      window.addEventListener('popstate', onRouteEvent);
      window.addEventListener('hashchange', onRouteEvent);
      window.addEventListener('pageshow', onRouteEvent); // bfcache 恢复：重新武装
      window.addEventListener('pagehide', teardown);
    } catch (_) { /* 忽略 */ }
    try {
      var d = doc();
      if (d) d.addEventListener('visibilitychange', onVisibilityChange);
    } catch (_) { /* 忽略 */ }
  }

  // 注意：隔离世界里改写 history.pushState 拦不到页面主世界的调用，漏网的路由切换靠 20s 兜底
  function onRouteEvent() {
    try {
      handledPath = '';
      if (isBadgePath(safePathname())) armActive();
      else stopObserving();
    } catch (e) { warn('route event failed', e); }
  }

  /* ================= pageFetch：借页面同源 + 会话 Cookie 发一次 GET =================
   * 油猴脚本时代所有 Civitai API 调用都在页面里 fetch()，同源、带 Cookie。移植后请求换到
   * offscreen（chrome-extension:// 源、跨站、匿名），tRPC 回 401「Please use the public API
   * instead」，公开 API 的 NSFW 结果被清成 {"items":[]}。images/posts 页同样能给其它上下文
   * 提供"回到页面里发这个请求"的通道，实现与 model-page.js 一致。
   */

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
    runtimeSend(env);
  }

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

  function onRuntimeMessage(msg, sender, sendResponse) {
    try {
      if (!msg || msg.__cft !== true || msg.to !== 'content') return; // 非本模块消息
      if (msg.type === 'pageFetch') {
        var payload = (msg && msg.payload) || {};
        if (!claimPageFetchCid(String(payload.cid == null ? '' : payload.cid))) {
          return; // 同文档已有应答方（model-page.js 抢先认领）：不重复 answer，也不占用通道
        }
        runPageFetch(payload, sendResponse);
        return true; // 异步响应（与 model-page.js 的 askFileSelection 同款：fetch 完成前保持通道打开）
      }
      if (msg.type === 'savedImageDone') {
        try {
          var p = (msg && msg.payload) || {};
          if (p.ok && p.imageId != null && String(p.imageId) !== '') markSaved(p.imageId);
          else if (p.imageId != null && String(p.imageId) !== '') {
            delete badgeState[String(p.imageId)];
            repaintBadgeById(p.imageId);
          }
        } catch (e) { warn('savedImageDone failed', e); }
      }
      // 其余类型（progress/archiveDone/askFileSelection 等）归 model-page.js 处理
    } catch (e) { warn('onRuntimeMessage failed', e); }
  }

  // 属性选择器前先校验 imageId 形状：值虽来自本扩展，拼进选择器仍需防注入
  function repaintBadgeById(imageId) {
    var id = String(imageId == null ? '' : imageId);
    if (!/^\d+$/.test(id)) { warn('savedImageDone 非法 imageId', id); return; }
    var d = doc();
    if (!d) return;
    try {
      var b = d.querySelector('[data-cft-image-id="' + id + '"]');
      if (b) paintBadge(b, id);
    } catch (e) { warn('repaintBadgeById failed', e); }
  }

  /* ================= 挂载（事件驱动为主 + 慢速兜底，不做前台轮询） ================= */

  function init() {
    try {
      var d = doc();
      if (!d) return;
      loadSavedImages().then(function () { addImageBadges(); });
      armWindowEvents();
      if (isBadgePath(safePathname())) armActive();
      else startRouteWatch();
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
      currentPageUrl: currentPageUrl,
      buildSaveSingleImageRequest: buildSaveSingleImageRequest,
      pageFetchRejectReason: pageFetchRejectReason,
      handleMessage: onRuntimeMessage
    };
  } else {
    init();
  }
})();
