/* 页面数据通道（pageFetch）回归：三条 P1 + 两个 P2。
   契约与证据链见 docs/ext-archive-contract.md §2.1。这里只钉住「绝不跨站借页面」这一件事：
   发起页 URL 必须一路带到 offscreen；host 判不出或对不上就不借；借不到就如实回落直连 ——
   绝不能把匿名跨站请求记成「页面通道（带你的登录状态）」。
   加载扩展 JS 复用 tests/test_extension_batch.cjs 的 loadScript/mkNs 基座（按依赖序加载
   side-effect import，并把未列出的 chrome.* 自动桩成返回 Promise 的函数）。 */
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const root = path.resolve(__dirname, '..');
let passed = 0; const ok = (s) => { passed++; console.log('OK ' + s); };

const IMPORT_RE = /^import\s+['"]([^'"]+)['"]\s*;?\s*$/gm;
function loadScript(file, ctx, seen) {
  const abs = path.resolve(file); if (seen.has(abs)) return; seen.add(abs);
  const src = fs.readFileSync(abs, 'utf8');
  for (const m of src.matchAll(IMPORT_RE)) loadScript(path.resolve(path.dirname(abs), m[1]), ctx, seen);
  vm.runInContext('(function(){\n' + src.replace(IMPORT_RE, '') + '\n})();', ctx, { filename: path.relative(root, abs) });
}
const loadIn = (ctx, rel) => loadScript(path.join(root, rel), ctx, new Set());

// mkNs 把 onXxx 监听器登记到「当前环境」那张表里：每个 harness 先 bindSink 再建 chrome。
let SINK = {};
const sendersOf = (k) => (SINK[k] = SINK[k] || []);
const bindSink = () => (SINK = {});
const mkNs = (name, over = {}) => {
  const t = Object.assign({}, over);
  return new Proxy(t, {
    get(o, k) {
      const key = String(k);
      if (key in o) return o[key];
      if (/^on[A-Z]/.test(key)) {
        o[key] = { addListener: (f) => { sendersOf(name + '.' + key).push(f); return true; }, removeListener: () => {} };
        return o[key];
      }
      o[key] = () => Promise.resolve(undefined); return o[key];
    },
    set(o, k, v) { o[k] = v; return true; },
  });
};
const clone = (v) => JSON.parse(JSON.stringify(v));
// vm 里的 setInterval（lib/log.js 的 2s 刷盘）是常驻定时器：不 unref 的话 node 跑完断言也不退出
const vmSetInterval = (fn, ms) => { const t = setInterval(fn, ms); if (t && t.unref) t.unref(); return t; };
const wait = async (f, ms = 3000) => {
  for (let i = 0; i < Math.max(1, ms / 10); i++) { if (await f()) return; await new Promise((r) => setTimeout(r, 10)); }
  throw new Error('timeout 等待条件未成立');
};

const IMG_META_BODY = JSON.stringify({
  items: [{ id: 123, type: 'image', url: 'https://image.civitai.com/x/123.png', meta: { prompt: 'hello', sampler: 'DPM++' } }],
});
const RED_PAGE = 'https://civitai.red/images/123';
const COM_PAGE = 'https://civitai.com/models/4201?modelVersionId=501240';
const SINGLE = { imageId: '123', originalUrl: 'https://image.civitai.com/x/original=true/123.png', ext: 'png' };

// ---- offscreen（archive-worker.js）运行环境：假 chrome + 可编排的标签页/直连应答 ----
function offscreen() {
  const senders = bindSink();
  const log = { pageFetch: [], direct: [], bg: [] };
  let onTabMessage = null;   // (tabId, msg) => Promise<答复>；null = 该 tab 完全不应答
  let onDirect = null;       // (url) => 假 Response，或抛错
  const ext = {
    runtime: mkNs('runtime', {
      id: 'test-id', lastError: undefined,
      getContexts: async () => [{ url: 'offscreen' }],
      getURL: (p) => 'chrome-extension://test-id/' + p,
      sendMessage: async (m) => {
        log.bg.push(m);
        if (m && m.__cft === true && m.to === 'offscreen') { // content 的 runtime 广播（备用应答路）
          for (const f of senders['runtime.onMessage'] || []) f(m, { id: 'test-id' }, () => {});
          return undefined;
        }
        const t = String((m && m.type) || '');
        if (t === 'checkLibrary') return { found: false, paths: [] };
        if (t === 'writeFile') return { ok: true, saved: 'software', channel: 'software' };
        return { ok: true };
      },
    }),
    storage: { local: { get: async () => ({}), set: async () => {}, remove: async () => {} } },
    tabs: mkNs('tabs', {
      query: async () => [],
      sendMessage: async (tabId, msg) => {
        log.pageFetch.push({ tabId, msg });
        const h = onTabMessage;
        if (!h) return new Promise(() => {});   // 不应答：复现「两条应答路全断」
        return h(tabId, msg);
      },
    }),
    action: mkNs('action'), offscreen: mkNs('offscreen'), downloads: mkNs('downloads'),
    contextMenus: mkNs('contextMenus'), windows: mkNs('windows'), sidePanel: mkNs('sidePanel'),
    alarms: mkNs('alarms'), notifications: mkNs('notifications'),
  };
  const ctx = vm.createContext({
    chrome: ext, URL, URLSearchParams, AbortController, setTimeout, clearTimeout,
    setInterval: vmSetInterval, clearInterval, queueMicrotask, console, TextEncoder, TextDecoder,
    btoa, atob, structuredClone: clone, indexedDB: undefined,
    fetch: async (url) => {
      log.direct.push(String(url));
      if (/image\.civitai\.com/.test(String(url))) return { ok: true, status: 200, arrayBuffer: async () => new Uint8Array([1, 2, 3, 4]).buffer };
      const h = onDirect;
      if (h) return h(String(url));
      throw new Error('测试桩：这条直连请求不该发生 ' + url);
    },
  });
  loadIn(ctx, 'chrome-extension/offscreen/archive-worker.js');
  const H = ctx.__cftArchiveTest;
  const envFor = (msg, r) => ({
    __cft: true, to: 'offscreen', type: 'pageFetchResult',
    payload: Object.assign({ cid: msg.payload.cid, ok: true, status: 200, body: '', err: null, truncated: false }, r || {}),
  });
  // 驱动 offscreen 的 onMessage（契约 §2：{__cft,to:'offscreen',type,payload}），兼容同步/异步 sendResponse
  const ask = (type, payload) => new Promise((resolve, reject) => {
    let done = false, ret;
    const finish = (v) => { if (!done) { done = true; clearTimeout(timer); resolve(v); } };
    const timer = setTimeout(() => { if (!done) { done = true; reject(new Error('offscreen 无答复: ' + type)); } }, 5000);
    for (const f of senders['runtime.onMessage'] || []) {
      ret = f({ __cft: true, to: 'offscreen', type, payload: payload || {} }, { id: 'test-id' }, finish);
      if (ret !== false && ret !== undefined) break;
    }
    if (ret && typeof ret.then === 'function') ret.then(finish, () => finish(null));
    else if (ret !== true) finish(ret === false ? null : (ret || null));
  });
  return {
    ctx, H, log,
    // 页面侧答复：sendResponse（resolve 值）或 runtime 广播，两条路各测一次
    answer(msg, r, via) {
      const env = envFor(msg, r);
      if (via === 'broadcast') { ext.runtime.sendMessage(env); return new Promise(() => {}); }
      return Promise.resolve(env);
    },
    ask,
    setTabHandler: (fn) => { onTabMessage = fn; },
    setDirect: (fn) => { onDirect = fn; },
  };
}

(async () => {
  // ============ P1-2：ptPickTab 只在「双方 host 都能确定且相等」时借道 ============
  {
    const w = offscreen();
    const mk = (pageUrl, tabId) => w.H.ptMakeEntry({ tabId, pageUrl });
    const scope = (entries) => ({ entries: entries.filter(Boolean), dead: new Set(), deadReason: new Map() });
    const redTab = scope([mk(RED_PAGE, 42)]);
    assert.equal(redTab.entries.length, 1, 'red 页面 URL 的 tab 能登记');
    assert.equal(w.H.ptPickTab(redTab, 'https://civitai.red/api/v1/images?imageId=123', RED_PAGE), 42);
    assert.equal(w.H.ptPickTab(redTab, 'https://civitai.red/api/v1/models/1', 'https://www.civitai.red/images/123'), 42,
      'www. 前缀不参与比较，同站就借');
    // 站点不同：宁可不借（旧实现在这里靠 wild/alive[0] 把 com 的 tab 借给 red 的请求）
    assert.equal(w.H.ptPickTab(scope([mk(COM_PAGE, 9)]), 'https://civitai.red/api/v1/images?imageId=123', RED_PAGE), null);
    // 站点判不出的条目根本不登记（旧实现登记成 host='' 的 wild 候选）
    assert.equal(mk('', 77), null, 'pageUrl 缺失 → 不登记');
    assert.equal(mk('https://evil.example/models/1', 78), null, '白名单外的页面 URL → 不登记');
    assert.equal(mk(RED_PAGE, 0), null, 'tabId 0（bg 的占位值）不是真标签页');
    assert.equal(scope([mk('', 77)]).entries.length, 0);
    // 请求侧站点判不出：旧实现取 alive[0]，现在返回 null
    assert.equal(w.H.ptPickTab(redTab, 'not-a-url', ''), null);
    assert.equal(w.H.ptPickTab(scope([mk(RED_PAGE, 42), mk(COM_PAGE, 9)]), 'https://civitai.com/api/v1/models/1', ''), 9,
      'pageUrlOrHost 缺失时按请求 URL 的 host 匹配同站 tab（仍然同站才借）');
    const dead = scope([mk(RED_PAGE, 42)]); dead.dead.add(42);
    assert.equal(w.H.ptPickTab(dead, 'https://civitai.red/api/v1/models/1', RED_PAGE), null, '已短路的 tab 不再借');
    ok('ptPickTab：host 双方可判且相等才借；判不出/对不上/已短路一律 null');
  }

  // ============ P1-1：单图链路带发起页 URL → 借对 tab，请求打到 civitai.red ============
  {
    const w = offscreen();
    w.setTabHandler((tabId, msg) => {
      assert.equal(msg.type, 'pageFetch');
      assert.equal(tabId, 42, '单图借的是发起动作所在的那个 tab');
      assert.match(msg.payload.url, /^https:\/\/civitai\.red\/api\/v1\/images\?imageId=123/,
        '镜像站的图必须打到 civitai.red（旧实现退化成 civitai.com，取的是另一个站的数据）');
      return w.answer(msg, { body: IMG_META_BODY });
    });
    const res = await w.ask('saveSingleImage', Object.assign({}, SINGLE, { tabId: 42, pageUrl: RED_PAGE }));
    assert.equal(res && res.ok, true, '单图归档成功: ' + JSON.stringify(res));
    assert.equal(res.metaSaved, true, '页面通道拿到了生成数据 → 落 json');
    assert.equal(res.metaNote, '', '拿到数据就不该有降级说明');
    assert.equal(w.log.pageFetch.length, 1, '只发了一次页面请求');
    assert.deepEqual(w.log.direct.filter((u) => /\/api\/v1\//.test(u)), [], '数据请求没有被扩展直连再发一次');
    ok('单图：red 图页 + red tab → 借对 tab、请求打到 civitai.red、产物不带降级说明');
  }

  // ============ P2：askContent 也消费 sendResponse 的 resolve 值 ============
  {
    const w = offscreen();
    // 只走 sendResponse（resolve 值）、完全不广播 —— 修复前这条要等满 timeoutMs+3000 再把 tab 永久短路
    w.setTabHandler((tabId, msg) => w.answer(msg, { body: IMG_META_BODY }));
    const t0 = Date.now();
    const res = await w.ask('saveSingleImage', Object.assign({}, SINGLE, { tabId: 42, pageUrl: RED_PAGE }));
    const dt = Date.now() - t0;
    assert.equal(res && res.ok, true, JSON.stringify(res));
    assert.equal(res.metaSaved, true, 'resolve 值就要把数据接住，不能干等广播');
    assert.ok(dt < 2000, '不该等满超时（实测 ' + dt + 'ms）');
    ok('askContent：sendResponse 的 resolve 值直接喂给 resolveCid（' + dt + 'ms 完成，tab 没被短路）');
  }
  {
    // 广播那一路仍然可用（两条路等价，resolveCid 先到先得、不重复计数）
    const w = offscreen();
    w.setTabHandler((tabId, msg) => w.answer(msg, { body: IMG_META_BODY }, 'broadcast'));
    const res = await w.ask('saveSingleImage', Object.assign({}, SINGLE, { tabId: 42, pageUrl: RED_PAGE }));
    assert.equal(res && res.ok, true, '只广播不应答 sendResponse 也要能成');
    assert.equal(res.metaSaved, true);
    ok('askContent：content 只走 runtime 广播时同样能应答（两条路等价）');
  }

  // ============ P1-1/P1-2：red 请求 + 只有 com tab → 不借、回落直连且不谎报登录态 ============
  {
    const w = offscreen();
    let seen = null;
    w.setDirect((url) => {
      seen = url;
      return { ok: true, status: 200, json: async () => JSON.parse(IMG_META_BODY), text: async () => IMG_META_BODY };
    });
    const r = await w.H.withPageTransport([{ tabId: 9, pageUrl: COM_PAGE }], 'unit:cross-site', async () => {
      const core = await w.ctx.CFT.api.getImageGenerationData('123', RED_PAGE);
      return { core, summary: w.H.pageTransportSummary() };
    });
    assert.equal(w.log.pageFetch.length, 0, '跨站绝不借：一个 pageFetch 都不该发出去');
    assert.match(String(seen), /^https:\/\/civitai\.red\/api\/v1\/images/, '回落扩展直连，且站点仍是 red');
    assert.equal(r.core.transport, 'direct', '通道记成 direct，不能被记成 page');
    assert.equal(r.summary.pageOk, 0, '页面通道一次都没成功');
    assert.match(r.summary.lastPageError, /没有与该请求站点匹配的标签页/);
    // 产物文案只认当次实际通道：direct 就只能写「扩展直连」
    const note = w.H.promptGapNote({ noMeta: ['9'], failed: 0, transports: { direct: 1 }, lastReason: 'empty-items' }, 0);
    assert.match(note, /扩展直连/);
    assert.doesNotMatch(note, /带你的登录状态/, '匿名直连绝不写成登录态取数: ' + note);
    ok('跨站：red 请求 + 只有 com tab → 零 pageFetch、如实回落直连、transport 不是 page');
  }

  // ============ P1-1 的原始入口：pageUrl 缺失 → 不登记 tab、不装页面通道 ============
  {
    const w = offscreen();
    w.setDirect(() => ({ ok: true, status: 200, json: async () => ({ items: [] }), text: async () => '{"items":[]}' }));
    // 老信封：只有 tabId、没有 pageUrl（P1-1 就是这么把一个站点未知的 tab 借出去，还记成登录态）
    const res = await w.ask('saveSingleImage', Object.assign({}, SINGLE, { tabId: 42 }));
    assert.equal(w.log.pageFetch.length, 0, 'pageUrl 缺失时绝不向那个 tab 发页面请求');
    assert.equal(res.ok, true, '原图仍照常保存');
    assert.match(res.metaNote, /扩展直连/, '产物必须写「扩展直连（多半按匿名处理）」: ' + res.metaNote);
    assert.doesNotMatch(res.metaNote, /页面登录态通道/);
    assert.match(w.log.direct.find((u) => /\/api\/v1\//.test(u)) || '', /civitai\.com/,
      'pageUrl 缺失时按 lib 默认站点 civitai.com 直连 —— 这正是发起页 URL 必须带上的理由');
    ok('pageUrl 缺失：页面通道不安装、不借道，产物如实写扩展直连');
  }

  // ============ P1-3：saveSingleImage 参与防重入（两个方向都挡） ============
  {
    const w = offscreen();
    let releaseBatchTab = null;
    w.setTabHandler((tabId, msg) => new Promise((res) => { releaseBatchTab = () => res(w.answer(msg, { ok: false, status: 599, err: '测试：批量收尾' })); }));
    const started = await w.ask('archiveBatch', {
      jobId: 'j1',
      items: [{ tabId: 5, url: COM_PAGE, modelId: '4201', modelVersionId: '501240', pageUrl: COM_PAGE }],
    });
    assert.equal(started.ok, true, '批量已启动: ' + JSON.stringify(started));
    await wait(() => w.log.pageFetch.length === 1);
    const before = w.H.pageTransportSummary();
    assert.match(before.label, /^batch:/, '当前 scope 是批量的');
    assert.equal(before.tabs, 1);
    const dup = await w.ask('saveSingleImage', Object.assign({}, SINGLE, { tabId: 77, pageUrl: RED_PAGE }));
    assert.equal(dup.ok, false, '单图不许叠在批量上');
    assert.match(dup.msg, /归档任务进行中/);
    assert.equal(w.log.pageFetch.length, 1, '被拒的单图没有向任何 tab 发页面请求');
    const after = w.H.pageTransportSummary();
    assert.equal(after.label, before.label, '批量 scope 没被改写');
    assert.equal(after.tabs, 1, '单图那个 host 不同的 tab 没有被并进批量正在用的 scope 当候选');
    assert.equal((await w.ask('archive', { modelId: '4201', pageUrl: COM_PAGE })).ok, false, '单页归档也被同一把锁挡住');
    w.setDirect(() => { throw new Error('测试：直连也不给数据'); });
    releaseBatchTab();                                   // 放行批量：页面失败 → 直连失败 → prepare 失败 → 收尾
    await wait(() => w.H.getBatchState().running === false);
    await wait(() => w.H.anyTaskActive() === false);
    assert.equal(w.H.pageTransportSummary().active, false, '批量结束后 scope 已还原');
    ok('P1-3：单图叠在批量上被拒，且绝不把 host 不同的 tab 并进批量正在用的 scope');
  }
  {
    // 反向：单图在跑时单页归档与批量都被拒；单图结束后锁释放（漏放锁会把整个 offscreen 永久锁死）
    const w = offscreen();
    let releaseSingle = null;
    w.setTabHandler((tabId, msg) => new Promise((res) => { releaseSingle = () => res(w.answer(msg, { ok: false, status: 0, err: '测试：放行' })); }));
    const inflight = w.ask('saveSingleImage', Object.assign({}, SINGLE, { tabId: 42, pageUrl: RED_PAGE }));
    await wait(() => w.log.pageFetch.length === 1);
    assert.equal((await w.ask('archive', { modelId: '1', pageUrl: RED_PAGE })).ok, false, '单图在跑 → 拒绝单页归档');
    assert.equal((await w.ask('archiveBatch', { items: [] })).ok, false, '单图在跑 → 拒绝批量归档');
    w.setDirect(() => { throw new Error('测试：直连失败'); });
    releaseSingle();
    const doneRes = await inflight;
    // 页面通道这轮回 ok:false、直连也失败 → 元数据拿不到，但原图（CDN，不需要登录态）照样存下
    assert.equal(doneRes.ok, true, JSON.stringify(doneRes));
    assert.match(doneRes.metaNote, /元数据获取失败/, '拿不到数据要在产物里如实交代: ' + doneRes.metaNote);
    assert.doesNotMatch(doneRes.metaNote, /页面登录态通道/);
    await wait(() => w.H.anyTaskActive() === false, 2000);
    const accepted = await w.ask('archive', { modelId: '', pageUrl: RED_PAGE });
    assert.equal(accepted.ok, true, '锁释放后单页归档重新被受理: ' + JSON.stringify(accepted));
    await wait(() => w.H.anyTaskActive() === false, 2000);
    ok('P1-3：锁是双向的 —— 单图在跑时单页/批量都被拒，结束后锁自动释放');
  }

  // ============ P2：content 侧不跟随重定向（两个 content script 同一套实现） ============
  for (const rel of ['chrome-extension/content/image-badge.js', 'chrome-extension/content/model-page.js']) {
    const ctx = vm.createContext({
      URL, URLSearchParams, AbortController, setTimeout, clearTimeout, setInterval: vmSetInterval, clearInterval, console,
      module: { exports: {} },
      chrome: { runtime: { sendMessage: () => {}, onMessage: { addListener: () => {} } } },
      window: { location: { href: RED_PAGE } },
    });
    vm.runInContext('(function(){\n' + fs.readFileSync(path.join(root, rel), 'utf8') + '\n})();', ctx);
    const M = ctx.module.exports;
    const calls = [];
    // opaquedirect 是 Chromium 实测形状（type:'opaqueredirect' + status 0 + 空体，且目标站命中 0 次）
    ctx.fetch = async (url, init) => {
      calls.push({ url: String(url), init: init || {} });
      if (init && init.redirect === 'manual') return { type: 'opaqueredirect', status: 0, ok: false, redirected: false, text: async () => '' };
      return { type: 'basic', status: 200, ok: true, text: async () => IMG_META_BODY };
    };
    let resp = null, answered = 0;
    const ret = M.handleMessage(
      { __cft: true, to: 'content', type: 'pageFetch', payload: { cid: 'c-' + rel, url: 'https://civitai.red/api/v1/models/1', timeoutMs: 5000 } },
      {}, (env) => { answered++; resp = env; });
    assert.equal(ret, true, rel + '：pageFetch 是异步应答，必须 return true 保持通道');
    await wait(() => answered === 1, 1000);
    assert.equal(calls.length, 1, rel + '：3xx 之后绝不向重定向目标再发一次请求');
    assert.equal(calls[0].init.redirect, 'manual',
      rel + '：不跟随重定向。实测 redirect:error 抛的是 TypeError("Failed to fetch")，与 CORS/网络失败同文案，无法如实区分');
    assert.equal(calls[0].init.credentials, 'same-origin', rel + '：只在页面里同源带 Cookie');
    assert.equal(resp.payload.ok, false);
    assert.equal(resp.payload.status, 0);
    assert.match(resp.payload.err, /重定向/, rel + '：文案要说「被重定向拒发」: ' + resp.payload.err);
    assert.doesNotMatch(resp.payload.err, /不在白名单/, rel + '：不能把重定向说成白名单拒绝');
    // 白名单拒绝：一个字节都不发，文案保持「url 不在白名单」
    let badAnswered = 0, badResp = null;
    M.handleMessage({ __cft: true, to: 'content', type: 'pageFetch', payload: { cid: 'bad-' + rel, url: 'https://evil.example/api/v1/x' } },
      {}, (env) => { badAnswered++; badResp = env; });
    await wait(() => badAnswered === 1, 1000);
    assert.match(badResp.payload.err, /不在白名单/);
    assert.equal(calls.length, 1, rel + '：白名单外的 URL 完全没有进入 fetch');
    ok('content 页面通道（' + path.basename(rel) + '）：不跟随重定向、只应答一次、两种拒绝文案分开');
  }

  // ============ 单图信封：content 带发起页 URL，bg 用 sender.tab.url 兜底 ============
  {
    const ctx = vm.createContext({
      URL, URLSearchParams, AbortController, setTimeout, clearTimeout, console,
      module: { exports: {} }, chrome: { runtime: { sendMessage: () => {}, onMessage: { addListener: () => {} } } },
      window: { location: { href: RED_PAGE } },
    });
    vm.runInContext('(function(){\n' + fs.readFileSync(path.join(root, 'chrome-extension/content/image-badge.js'), 'utf8') + '\n})();', ctx);
    const M = ctx.module.exports;
    assert.equal(M.currentPageUrl(), RED_PAGE, '发起文档 URL 取自 window.location.href');
    const env = M.buildSaveSingleImageRequest({ imageId: 123, originalUrl: SINGLE.originalUrl, ext: 'png' }, M.currentPageUrl());
    assert.equal(env.to, 'bg');
    assert.equal(env.type, 'saveSingleImage');
    assert.equal(env.payload.pageUrl, RED_PAGE, '单图信封必须带发起页 URL（契约 §2.1）');
    assert.equal(env.payload.imageId, '123');
    const noUrl = M.buildSaveSingleImageRequest({ imageId: 1, originalUrl: 'u', ext: 'jpg' }, undefined);
    assert.equal(noUrl.payload.pageUrl, '', '拿不到就留空，由 bg 用 sender.tab.url 兜底（绝不编造）');
    ok('image-badge：saveSingleImage 信封带当前文档 URL');
  }
  {
    const senders = bindSink();
    const out = [];
    const ext = {
      runtime: mkNs('runtime', {
        id: 'test-id', lastError: undefined,
        getContexts: async () => [{ url: 'offscreen' }],
        getURL: (p) => 'chrome-extension://test-id/' + p,
        sendMessage: async (m) => { out.push(m); return { ok: true, msg: '已转发' }; },
      }),
      storage: { local: { get: async () => ({}), set: async () => {} } },
      action: mkNs('action'), tabs: mkNs('tabs', { query: async () => [] }),
      offscreen: mkNs('offscreen'), downloads: mkNs('downloads'), contextMenus: mkNs('contextMenus'),
      windows: mkNs('windows'), sidePanel: mkNs('sidePanel'), alarms: mkNs('alarms'), notifications: mkNs('notifications'),
    };
    const ctx = vm.createContext({
      chrome: ext, URL, URLSearchParams, AbortController, setTimeout, clearTimeout,
      setInterval: vmSetInterval, clearInterval, queueMicrotask, console, TextEncoder, TextDecoder,
      structuredClone: clone, fetch: async () => ({ ok: true, json: async () => ({ ok: true }) }),
    });
    loadIn(ctx, 'chrome-extension/background.js');
    const drive = async (payload, sender) => {
      const n = out.length;
      for (const f of senders['runtime.onMessage'] || []) {
        f({ __cft: true, to: 'bg', type: 'saveSingleImage', payload }, sender, () => {});
      }
      await wait(() => out.length > n, 1000);   // bg 要过 ensureOffscreen 才转发
      return out.slice(n).find((m) => m && m.type === 'saveSingleImage' && m.to === 'offscreen');
    };
    const fwd = await drive(Object.assign({}, SINGLE), { id: 'test-id', tab: { id: 42, url: RED_PAGE } });
    assert.ok(fwd, 'bg 必须把单图请求转发给 offscreen');
    assert.equal(fwd.payload.tabId, 42);
    assert.equal(fwd.payload.pageUrl, RED_PAGE, 'payload 缺 pageUrl 时 bg 用 sender.tab.url 兜底');
    const fwd2 = await drive(Object.assign({}, SINGLE, { pageUrl: COM_PAGE }), { id: 'test-id', tab: { id: 42, url: RED_PAGE } });
    assert.equal(fwd2.payload.pageUrl, COM_PAGE, 'content 自己报了就不覆盖（发起文档优先于 tab.url）');
    const fwd3 = await drive(Object.assign({}, SINGLE), { id: 'test-id' });
    assert.equal(fwd3.payload.pageUrl, '', 'bg 也拿不到 sender.tab 时保持留空 —— offscreen 就不借道');
    assert.equal(fwd3.payload.tabId, 0, 'tabId 0 是占位值，offscreen 侧不当真标签页登记');
    ok('background：forwardSaveSingleImage 补齐 pageUrl（sender.tab.url 兜底，content 报的优先）');
  }

  // ============ 真 Chromium 复核上面那条假设（vm 里 opaqueredirect 是我编的 fixture） ============
  // content 侧「不跟随重定向」选的是 redirect:'manual'，依赖两个浏览器事实：3xx → opaqueredirect
  // 且**不会向跳转目标发出第二个请求**；200 响应不受影响。上面用假 fetch 只能证明我们读对了字段，
  // 所以这里用本地 302/307 服务在真 Chromium 里再验一次（绝不碰真实站点）。playwright 装在本机
  // .tools/ 里（与 tests/test_extension_batch.cjs 同一套），拿不到就跳过、不算失败。
  const pwPath = process.env.PLAYWRIGHT_PATH || path.join(root, '.tools', 'node_modules', 'playwright');
  process.env.PLAYWRIGHT_BROWSERS_PATH = process.env.PLAYWRIGHT_BROWSERS_PATH || path.join(root, '.tools', 'ms-playwright');
  let chromium = null;
  try { ({ chromium } = require(pwPath)); } catch (e) { console.log('SKIP 真浏览器部分：playwright 不可用（' + pwPath + '）：' + e.message); }
  if (chromium) {
    const http = require('http');
    let targetHits = 0;
    const srvB = http.createServer((req, res) => {
      targetHits += 1;
      res.writeHead(200, { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*' });
      res.end('{"other":1}');
    });
    await new Promise((r) => srvB.listen(0, '127.0.0.1', r));
    const other = 'http://127.0.0.1:' + srvB.address().port + '/x';
    const srvA = http.createServer((req, res) => {
      if (req.url === '/to-302') { res.writeHead(302, { Location: other }); res.end(); return; }
      if (req.url === '/to-307') { res.writeHead(307, { Location: other }); res.end(); return; }
      res.writeHead(200, { 'Content-Type': 'application/json' }); res.end('{"items":[]}');
    });
    await new Promise((r) => srvA.listen(0, '127.0.0.1', r));
    const base = 'http://127.0.0.1:' + srvA.address().port;
    const browser = await chromium.launch({ headless: true });
    try {
      const page = await browser.newPage();
      await page.goto(base + '/');
      for (const p of ['/to-302', '/to-307']) {
        targetHits = 0;
        const r = await page.evaluate(async ([base, p]) => {
          try {
            const res = await fetch(base + p, { method: 'GET', credentials: 'same-origin', redirect: 'manual' });
            let body = ''; try { body = await res.text(); } catch (_) { body = '(不可读)'; }
            return { type: res.type, status: res.status, ok: res.ok, bodyLen: body.length };
          } catch (e) { return { err: String((e && e.message) || e) }; }
        }, [base, p]);
        assert.equal(r.type, 'opaqueredirect', p + ' 的形状：' + JSON.stringify(r));
        assert.equal(r.status, 0, p + ' 不给状态码，绝不能当成功');
        assert.equal(r.ok, false);
        assert.equal(r.bodyLen, 0, p + ' 响应体不可读');
        assert.equal(targetHits, 0, p + '：跳转目标一次都没被请求（这才叫"不产生跨站请求"）');
      }
      targetHits = 0;
      const plain = await page.evaluate(async (base) => {
        const res = await fetch(base + '/', { method: 'GET', credentials: 'same-origin', redirect: 'manual' });
        return { type: res.type, status: res.status, ok: res.ok, body: await res.text() };
      }, base);
      assert.equal(plain.type, 'basic', '非重定向的 200 不受 manual 影响：' + JSON.stringify(plain));
      assert.equal(plain.status, 200);
      assert.match(plain.body, /items/);
      // redirect:'error' 的文案为什么不能用：Chromium 抛的是 "Failed to fetch"，与 CORS/网络失败同形
      const errMode = await page.evaluate(async (base) => {
        try { await fetch(base + '/to-302', { redirect: 'error' }); return { ok: true }; }
        catch (e) { return { name: e.name, message: String(e.message || e) }; }
      }, base);
      assert.equal(errMode.name, 'TypeError');
      assert.doesNotMatch(String(errMode.message), /redirect/i,
        '实测就是 "' + errMode.message + '"：拿它写"被重定向拒发"会把 CORS/网络失败也这么说');
      ok('真 Chromium：manual 下 302/307 均不跟发（跳转目标命中 0 次），200 不受影响；error 文案确实无法区分');
    } finally {
      await browser.close();
      await new Promise((r) => srvA.close(r));
      await new Promise((r) => srvB.close(r));
    }
  }

  console.log('PASSED ' + passed);
})().catch((e) => { console.error(e); process.exitCode = 1; });
