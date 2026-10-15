/* popup 接入 CftRows + CFT.enrich + 「发送到 CFT 之后」任务区的集成回归。
   两层：
   A) node vm（复用 tests/test_extension_batch.cjs 的 loadScript/mkNs 基座）——钉住"列表不能因为
      补全失败而变空"这条契约依赖：needs-login / rate-limited / error 的行 submittable 仍是 true，
      只有本站 404 才翻成 false。整个弹层的"仅可提交"筛选就是建立在这条不变量上的。
   B) 真 Chromium 跑 popup.html —— 汇总行报数、三种降级态可见、精确勾选发送、任务区跨"关掉再开"恢复。
   全程不碰真实网络：C 站接口与本机桥都由 Node 侧路由应答，也不发任何真实提交。 */
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const http = require('http');
const root = path.resolve(__dirname, '..');
let passed = 0; const ok = (s) => { passed++; console.log('OK ' + s); };
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
// 跨 realm（vm 里造出来的数组）不能用 assert/strict 的 deepEqual：它等价 deepStrictEqual，会因构造函数
// 不同判"不同"。按值比，别按原型比（同 lib/enrich.test.cjs 的 same()）。
const same = (actual, expected, msg) => assert.equal(JSON.stringify(actual ?? null), JSON.stringify(expected ?? null), msg);

// ---- A) 依赖序加载基座（与 test_extension_batch.cjs / test_page_transport.cjs 同一份） ----
const IMPORT_RE = /^import\s+['"]([^'"]+)['"]\s*;?\s*$/gm;
function loadScript(file, ctx, seen) {
  const abs = path.resolve(file); if (seen.has(abs)) return; seen.add(abs);
  const src = fs.readFileSync(abs, 'utf8');
  for (const m of src.matchAll(IMPORT_RE)) loadScript(path.resolve(path.dirname(abs), m[1]), ctx, seen);
  vm.runInContext('(function(){\n' + src.replace(IMPORT_RE, '') + '\n})();', ctx, { filename: path.relative(root, abs) });
}
const loadIn = (ctx, rel) => loadScript(path.join(root, rel), ctx, new Set());
const mkNs = (name, over = {}) => {
  const t = Object.assign({}, over);
  return new Proxy(t, {
    get(o, k) {
      const key = String(k);
      if (key in o) return o[key];
      if (/^on[A-Z]/.test(key)) { o[key] = { addListener: () => true, removeListener: () => {} }; return o[key]; }
      o[key] = () => Promise.resolve(undefined); return o[key];
    },
    set(o, k, v) { o[k] = v; return true; },
  });
};
const clone = (v) => JSON.parse(JSON.stringify(v));
const vmSetInterval = (fn, ms) => { const t = setInterval(fn, ms); if (t && t.unref) t.unref(); return t; };

// 混合标签页：4 个 C 站模型页（red/com 同版本会并成一行）、1 个非 C 站、1 个图片页、1 个 CDN、1 个列表页
const TABS = [
  { id: 11, windowId: 1, title: 'Alpha | Civitai', url: 'https://civitai.red/models/101/a?modelVersionId=1001' },
  { id: 12, windowId: 1, title: 'Alpha 重复页', url: 'https://civitai.com/models/101/b?modelVersionId=1001' },
  { id: 13, windowId: 1, title: 'Beta | Civitai', url: 'https://civitai.com/models/202?modelVersionId=2002' },
  { id: 14, windowId: 2, title: 'Gamma | Civitai', url: 'https://civitai.red/models/303' },
  { id: 15, windowId: 1, title: 'GitHub', url: 'https://github.com/x/y' },
  { id: 16, windowId: 1, title: 'Civitai image', url: 'https://civitai.com/images/9001' },
  { id: 17, windowId: 1, title: 'CDN', url: 'https://image.civitai.com/x/9001.png' },
  { id: 18, windowId: 1, title: '列表页', url: 'https://civitai.com/models?types=Lora' },
];
// /api/v1/models/{mid} 的返回体（版本 ID 要和标签页 URL 对上，否则补全只会拿到模型级字段）
const modelDoc = (id, name, type, base, sizeKB, versionId) => ({
  id: Number(id), name, modelType: type,
  creator: { username: 'maker' }, stats: { downloadCount: 100 }, nsfw: false, nsfwLevel: 1,
  tags: ['style'],
  modelVersions: [{ id: versionId != null ? versionId : 1000 + Number(id), name: 'v1', baseModel: base,
    publishedAt: '2026-01-05T00:00:00Z', trainedWords: ['word'],
    images: [{ url: 'https://image.civitai.com/x/original=true/' + id + '.png' }],
    files: [{ id: 1, name: name + '.safetensors', sizeKB, type: 'Model' }] }],
});

function enrichCtx() {
  const stored = {};
  const ext = {
    runtime: mkNs('runtime', { id: 'test-id' }),
    storage: { local: {
      get: async (k) => { const out = {}; if (k && stored[k] !== undefined) out[k] = clone(stored[k]); return out; },
      set: async (v) => Object.assign(stored, clone(v)),
    } },
    tabs: mkNs('tabs', { query: async () => [] }),
  };
  // 不给 fetch：默认取数路径必须自己降级成 error 条目 —— 那正是"补全失败"那条分支要验的
  const ctx = vm.createContext({ chrome: ext, URL, URLSearchParams, AbortController, setTimeout, clearTimeout,
    setInterval: vmSetInterval, clearInterval, queueMicrotask, console, TextEncoder, TextDecoder, structuredClone: clone });
  loadIn(ctx, 'chrome-extension/tabs-common.js');
  loadIn(ctx, 'chrome-extension/lib/tabrows.js');
  loadIn(ctx, 'chrome-extension/lib/enrich.js');
  return ctx;
}
const fetcherPlan = (statuses, docs) => async (url) => {
  const id = (/\/api\/v1\/models\/(\d+)/.exec(url) || [])[1] || '';
  const status = statuses[id] || 200;
  if (status !== 200) return { ok: false, status, json: null };
  return { ok: true, status: 200, json: docs[id] };
};

(async () => {
  // ===================== A. 纯逻辑层：过滤事实可见 + 降级不塌成空 =====================
  const ctx = enrichCtx();
  const R = ctx.CftRows, E = ctx.CFT.enrich;
  const built = R.build(TABS);
  assert.equal(built.rows.length, 3, 'red/com 同一个版本只该留一行');
  same(built.rows.map(r => r.key), ['version:1001:file:choose', 'version:2002:file:choose', 'model:303:file:choose']);
  assert.equal(built.summary.offsite, 1);
  assert.equal(built.summary.merged, 1);
  const summaryText = R.summaryText(built.summary);
  assert(summaryText.includes('非 C 站已忽略 1'), summaryText);
  assert(summaryText.includes('可提交模型页 4'), summaryText);
  assert(summaryText.includes('重复合并 1'), summaryText);
  assert(summaryText.includes('未纳入提交'), summaryText);
  ok('汇总行把"筛掉了多少"说出来（非 C 站／图片页／CDN／列表页各自计数，重复合并可见）');

  const rows = built.rows;
  const peeked = await E.peek(rows, {});
  E.applyToRows(rows, peeked);
  same(rows.map(r => r.state), ['loading', 'loading', 'loading']);
  same(R.applyFilters(rows, {}).map(r => r.key), rows.map(r => r.key), '没联网时一行都不该被筛掉');
  assert.equal(R.applyFilters(rows, { states: ['loading'] }).length, 3);
  assert.equal(rows.filter(r => r.submittable !== false).length, 3, '「仅可提交」不能把待补全的行筛空');
  assert(E.summaryText(peeked.summary).includes('待补全 3'), E.summaryText(peeked.summary));
  ok('联网还没回来时 3 行全留在列表里（state=loading 保留，缩略图缺失也不影响行存在）');

  const degraded = await E.enrich(rows, {
    fetcher: fetcherPlan({ 101: 401, 303: 429 }, { 101: modelDoc(101, 'Alpha', 5, 'SDXL', 150000, 1001), 202: modelDoc(202, 'Beta Real', 1, 'SD 1.5', 6000000, 2002), 303: modelDoc(303, 'Gamma', 5, 'Flux.1 D', 240000) }),
    openTabIds: TABS.map(t => t.id), force: true,
  });
  E.applyToRows(rows, degraded);
  const byKey = k => rows.find(r => r.key === k);
  assert.equal(byKey('version:1001:file:choose').state, 'needs-login');
  assert.equal(byKey('version:1001:file:choose').submittable, true, '需登录态绝不能被判成不可提交');
  assert.equal(byKey('model:303:file:choose').state, 'rate-limited');
  assert.equal(byKey('model:303:file:choose').submittable, true, '限流退避也还是可提交');
  assert.equal(byKey('version:2002:file:choose').meta.name, 'Beta Real');
  assert.equal(rows.filter(r => r.submittable !== false).length, 3, '需登录/限流的行必须留在列表里');
  const degradedText = E.summaryText(degraded.summary);
  assert(degradedText.includes('需登录态') && degradedText.includes('被限流'), degradedText);
  assert(degraded.entries.some(e => e.retryAt > Date.now()), '限流条目必须带退避时间点：' + JSON.stringify(degraded.entries.map(e => e.retryAt)));
  ok('补全失败/需登录/限流的行仍可见且仍可提交，原因与退避时间进 summaryText');

  // 404 单独一套存储：上一轮的 429 已给 civitai.red 装上退避门禁，同一套存储里测 404 会先被门禁拒掉
  const ctx2 = enrichCtx();
  const R2 = ctx2.CftRows, E2 = ctx2.CFT.enrich;
  const fresh = R2.build(TABS).rows;
  const gone = await E2.enrich(fresh, {
    fetcher: fetcherPlan({ 303: 404 }, { 101: modelDoc(101, 'Alpha', 5, 'SDXL', 150000, 1001), 202: modelDoc(202, 'Beta', 1, 'SD 1.5', 6000000, 2002), 303: null }),
    openTabIds: TABS.map(t => t.id),
  });
  E2.applyToRows(fresh, gone);
  const missing = fresh.find(r => r.key === 'model:303:file:choose');
  assert.equal(missing.state, 'missing');
  assert.equal(missing.submittable, false, '只有本站 404 才该翻掉可提交');
  assert.equal(fresh.filter(r => r.submittable !== false).length, 2);
  assert(String(missing.enrichReason).includes('没有拿另一站的结果冒充它'), missing.enrichReason);
  ok('404 才翻掉可提交标记（按标签页自己的站点判定，不拿另一站冒充），"仅可提交"据此筛掉它');

  // ===================== B. 真 Chromium：弹层行为 =====================
  const pwPath = process.env.PLAYWRIGHT_PATH || path.join(root, '.tools', 'node_modules', 'playwright');
  process.env.PLAYWRIGHT_BROWSERS_PATH = process.env.PLAYWRIGHT_BROWSERS_PATH || path.join(root, '.tools', 'ms-playwright');
  let chromium = null;
  try { ({ chromium } = require(pwPath)); } catch (e) { console.log('SKIP 浏览器部分：playwright 不可用（' + pwPath + '）：' + e.message); }
  if (chromium) {
  const server = http.createServer((req, res) => {
    const p = path.join(root, 'chrome-extension', new URL(req.url, 'http://localhost').pathname);
    fs.readFile(p, (e, b) => { if (e) { res.writeHead(404).end(); return; }
      res.setHeader('Content-Type', p.endsWith('.js') ? 'application/javascript' : p.endsWith('.css') ? 'text/css' : 'text/html');
      res.end(b); });
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const port = server.address().port;
  const browser = await chromium.launch({ headless: true });

  // Node 侧可控世界：storage 跨页面存活（任务区"关掉再开"就靠它）、C 站接口与桥按脚本编排
  const world = { store: new Map(), tabs: TABS, bridgeOnline: true, api: {}, rpc: {} };
  const deferred = () => { let release; const promise = new Promise(r => { release = r; }); return { promise, release }; };
  const blank = () => { world.store.clear(); world.bridgeOnline = true; world.tabs = TABS.slice();
    world.api = { hold: null, status: {}, docs: {}, delay: {} };
    world.rpc = { tasks: [], parse: { running: false, items: [], finished: true } }; };

  const context = await browser.newContext();
  await context.exposeBinding('nodeStoreGet', (source, key) => (world.store.has(key) ? world.store.get(key) : undefined));
  await context.exposeBinding('nodeStoreSet', (source, key, value) => { world.store.set(key, value); return true; });
  await context.exposeBinding('nodeTabs', async (source, query) => {
    if (query && query.active) return [{ id: 99, windowId: 1, title: 'Civitai 首页', url: 'https://civitai.com/' }];
    return (query && query.currentWindow) ? world.tabs.filter(t => t.windowId === 1) : world.tabs.slice();
  });
  await context.exposeBinding('nodeApi', async (source, url) => {
    if (world.api.hold) await world.api.hold.promise;
    const id = (/\/api\/v1\/models\/(\d+)/.exec(url) || [])[1] || '';
    if (world.api.delay && world.api.delay[id]) await sleep(world.api.delay[id]);
    const status = (world.api.status || {})[id] || 200;
    if (status !== 200) return { status, body: null };
    if (/\/api\/v1\/images/.test(url)) {
      return { status: 200, body: { items: [{ id: 9001, type: 'image', url: 'https://image.civitai.com/x/9001.png', nsfw: false, nsfwLevel: 1, createdAt: '2026-01-01T00:00:00Z', meta: { prompt: 'p' } }], metadata: {} } };
    }
    return { status: 200, body: (world.api.docs || {})[id] };
  });
  await context.exposeBinding('nodeBridge', async (source, pathName, body) => {
    if (!world.bridgeOnline) throw new Error('Failed to fetch');
    if (pathName === '/api/health') return { status: 200, body: { ok: true, app: 'CivitaiFreeTool', version: '2.6.30', mode: 'window', window: true } };
    if (pathName === '/api/rpc') {
      const req = JSON.parse(body || '{}');
      if (req.method === 'get_tasks') return { status: 200, body: { result: world.rpc.tasks } };
      if (req.method === 'get_parse_state') return { status: 200, body: { result: world.rpc.parse } };
      if (req.method === 'library_check') return { status: 200, body: { result: { ok: true, found: false, paths: [] } } };
      return { status: 400, body: { error: '测试桥不支持：' + req.method } };
    }
    return { status: 404, body: { ok: false } };
  });
  await context.addInitScript(() => {
    window.sent = [];
    window.__job = null;
    const storage = {
      async get(key) { const v = await window.nodeStoreGet(key); return v === undefined ? {} : { [key]: v }; },
      async set(obj) { for (const k of Object.keys(obj)) await window.nodeStoreSet(k, obj[k]); return true; },
      async remove(key) { await window.nodeStoreSet(key, undefined); return true; },
    };
    window.chrome = {
      id: 'test-id',
      storage: { local: storage, session: { local: storage } },
      tabs: { query: (q) => window.nodeTabs(q || {}), onRemoved: { addListener: () => {} },
        sendMessage: async () => { throw new Error('Could not establish connection. Receiving end does not exist.'); } },
      runtime: {
        id: 'test-id', getURL: (p) => 'chrome-extension://test-id/' + p,
        sendMessage: async (m) => {
          if (m && m.__cft) return m.type === 'archiveBatchStatus' ? { ok: true, started: true } : { ok: true };
          if (m.type === 'cft-batch-start') {
            window.sent.push(m.urls.slice());
            window.__job = { id: 'job-1', running: false, completedKeys: [],
              items: m.urls.map(u => Object.assign({}, CftTabs.modelLink(u), { status: 'sent', msg: 'accepted' })) };
            return { ok: true, job: window.__job };
          }
          if (m.type === 'cft-batch-clear') { window.__job = null; return { ok: true }; }
          return { ok: true, job: window.__job };
        },
      },
    };
    window.fetch = async (url, opts) => {
      const u = String(url);
      const r = u.indexOf('127.0.0.1:47531') >= 0
        ? await window.nodeBridge(new URL(u).pathname, (opts && opts.body) || '')
        : await window.nodeApi(u);
      return { ok: r.status >= 200 && r.status < 300, status: r.status, json: async () => r.body, text: async () => JSON.stringify(r.body) };
    };
  });
  const openPopup = async () => {
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto('http://127.0.0.1:' + port + '/popup.html');
    return { page, errors };
  };
  const rows = (page) => page.$$eval('.tab-row', els => els.map(e => e.textContent));
  const waitUntil = async (f, label, ms = 8000) => {
    for (let i = 0; i < Math.max(1, ms / 20); i++) { if (await f()) return; await sleep(20); }
    throw new Error('等待条件未成立: ' + label);
  };

  try {
    // ---- B1. 默认只列模型页 + 汇总行报数 + 类型/底模筛选 + 按大小排序 ----
    blank();
    world.api.docs = { 101: modelDoc(101, 'Alpha LoRA', 5, 'SDXL', 150000, 1001),
      202: modelDoc(202, 'Beta Checkpoint', 1, 'SD 1.5', 6000000, 2002),
      303: modelDoc(303, 'Gamma Flux', 5, 'Flux.1 D', 240000) };
    let { page, errors } = await openPopup();
    await page.waitForFunction(() => document.querySelectorAll('.tab-row').length === 3);
    const sum1 = await page.textContent('#tabSummary');
    assert(sum1.includes('共 8 个标签页'), sum1);
    assert(sum1.includes('可提交模型页 4'), sum1);
    assert(sum1.includes('非 C 站已忽略 1'), sum1);
    assert(sum1.includes('未纳入提交'), sum1);
    assert.equal(await page.locator('.tab-row').count(), 3, '列表只该有 3 行模型页：github/图片页/CDN/列表页都不进列表');
    assert.equal(await page.locator('#tabList .empty').count(), 0);
    await page.waitForFunction(() => document.querySelector('.tab-row small').textContent.includes('SDXL'));
    const b1rows = await rows(page);
    assert(b1rows[0].includes('Alpha LoRA') && b1rows[0].includes('146.5 MB'), b1rows[0]);
    assert(b1rows[0].includes('Lora') && b1rows[0].includes('ID 101'), b1rows[0]);
    assert.equal(await page.locator('.tab-row img.thumb').count(), 3, '每行都要画出富化出来的缩略图');
    await page.selectOption('#fType', 'CheckPoint');
    await page.waitForFunction(() => document.querySelectorAll('.tab-row').length === 1);
    assert((await page.textContent('.tab-row')).includes('Beta Checkpoint'));
    await page.selectOption('#fType', '');
    await page.selectOption('#fBase', 'SDXL');
    await page.waitForFunction(() => document.querySelectorAll('.tab-row').length === 1);
    await page.selectOption('#fBase', '');
    await page.selectOption('#sort', 'size');
    await page.waitForFunction(() => document.querySelectorAll('.tab-row').length === 3);
    assert((await page.textContent('.tab-row')).includes('Beta Checkpoint'), '按文件大小排序后最大的在前');
    assert.equal(errors.length, 0, '界面脚本错误: ' + errors.join('\n'));
    ok('默认只列 C 站模型页，被筛掉的数量在汇总行里；类型/底模筛选与按大小排序按富化结果生效');
    await page.close();

    // ---- B2. 三种降级态可见 + 联网未完成时列表不空 ----
    blank();
    const now = Date.now();
    world.tabs = [TABS[0], TABS[2], TABS[3], TABS[4], TABS[5]];
    world.store.set('cftEnrichV1', { version: 1, savedAt: now, gates: {}, entries: {
      // 过期条目：这轮联网失败也要把旧值给出去，界面标"旧缓存数据"而不是空白
      'civitai.red|version:1001': { ck: 'civitai.red|version:1001', site: 'civitai.red', kind: 'version', refId: '1001',
        url: 'https://civitai.red/models/101', at: now - 20 * 60 * 1000, last: now - 20 * 60 * 1000, ttl: 10 * 60 * 1000,
        status: 'ok', reason: '', channel: 'direct', tabIds: [11],
        meta: { name: '旧缓存里的 Alpha', type: 'Lora', baseModel: 'SDXL', versionId: '1001', totalMB: 146.5, submittable: true, exists: true } },
      // 新鲜条目：命中缓存，一次请求都不发
      'civitai.com|version:2002': { ck: 'civitai.com|version:2002', site: 'civitai.com', kind: 'version', refId: '2002',
        url: 'https://civitai.com/models/202', at: now - 60 * 1000, last: now - 60 * 1000, ttl: 10 * 60 * 1000,
        status: 'ok', reason: '', channel: 'direct', tabIds: [13],
        meta: { name: '缓存里的 Beta', type: 'CheckPoint', baseModel: 'SD 1.5', versionId: '2002', totalMB: 5859.4, submittable: true, exists: true } },
    } });
    world.api.hold = deferred();
    ({ page, errors } = await openPopup());
    await page.waitForFunction(() => document.querySelectorAll('.tab-row').length === 3);
    await page.waitForFunction(() => [...document.querySelectorAll('.tab-row')].some(r => r.textContent.includes('旧缓存里的 Alpha')));
    const held = await rows(page);
    assert.equal(held.length, 3, '请求还挂着时列表必须是满的 3 行，不能空');
    assert(held.some(t => t.includes('旧缓存数据') && t.includes('旧缓存里的 Alpha')), held.join('\n'));
    assert(held.some(t => t.includes('待补全')), held.join('\n'));
    assert.equal(await page.locator('#tabList .empty').count(), 0);
    assert((await page.textContent('#enrichSummary')).includes('缓存命中 1'), await page.textContent('#enrichSummary'));
    // 放行：101 匿名被拒（401）、303 限流（429，故意延后一点，别让退避门禁抢在 401 之前），202 全程走缓存
    world.api.status = { 101: 401, 303: 429 };
    world.api.delay = { 303: 400 };
    world.api.hold.release();
    await page.waitForFunction(() => {
      const t = [...document.querySelectorAll('.tab-row')].map(r => r.textContent);
      return t.some(x => x.includes('需登录态')) && t.some(x => x.includes('限流退避'));
    });
    const held2 = await rows(page);
    assert.equal(held2.length, 3);
    const aRow = held2.find(t => t.includes('Alpha'));
    assert(aRow && aRow.includes('需登录态'), held2.join('\n'));
    assert(aRow.includes('旧缓存里的 Alpha'), '需登录态这一行还要给出旧值，不能空着：' + aRow);
    assert(aRow.includes('需要登录态（不是"没有数据"）'), '行内要直接说出这是登录态问题，不是没数据：' + aRow);
    const gRow = held2.find(t => t.includes('Gamma'));
    assert(gRow && gRow.includes('限流退避'), held2.join('\n'));
    const en2 = await page.textContent('#enrichSummary');
    assert(en2.includes('需登录态 1') && en2.includes('被限流 1'), en2);
    assert.equal(await page.locator('#rateNote').isVisible(), true);
    const rate = await page.textContent('#rateNote');
    assert(rate.includes('限流') && rate.includes('退避'), rate);
    assert.equal(errors.length, 0, '界面脚本错误: ' + errors.join('\n'));
    ok('需登录态 / 旧缓存 / 限流退避三种状态在界面上各说各话，没有一个塌成"没有数据"');
    await page.close();

    // ---- B3. 只发送勾选的 URL：集合精确相等、token 被剥、未勾选与站外页绝不出现 ----
    blank();
    world.tabs = [
      { id: 11, windowId: 1, title: 'Alpha | Civitai', url: 'https://civitai.red/models/101/a?modelVersionId=1001&token=SECRET1&utm_source=chatgpt.com' },
      { id: 13, windowId: 1, title: 'Beta | Civitai', url: 'https://civitai.com/models/202?modelVersionId=2002&fbclid=abc' },
      { id: 14, windowId: 2, title: 'Gamma | Civitai', url: 'https://civitai.red/models/303?token=SECRET2' },
      { id: 15, windowId: 1, title: 'GitHub', url: 'https://github.com/x/y?token=OUTSIDE' },
      { id: 16, windowId: 1, title: 'Civitai image', url: 'https://civitai.com/images/9001' },
    ];
    world.api.docs = { 101: modelDoc(101, 'Alpha LoRA', 5, 'SDXL', 150000, 1001),
      202: modelDoc(202, 'Beta Checkpoint', 1, 'SD 1.5', 6000000, 2002),
      303: modelDoc(303, 'Gamma Flux', 5, 'Flux.1 D', 240000) };
    world.rpc.tasks = [
      // speed/downloaded/total 一律字节口径（与 downloader.DownloadTask 一致）：3.2 MiB/s = 3355443
      { id: 't1', url: 'https://civitai.red/models/101', filename: 'Alpha LoRA_v1.safetensors', status: 'downloading', progress: 42.4,
        downloaded: 63000000, total: 148000000, speed: 3355443, error: '', dest_dir: 'D:\\Civitai\\loras' },
      { id: 't2', url: 'https://civitai.com/models/202', filename: 'Beta Checkpoint_v1.safetensors', status: 'done', progress: 100,
        downloaded: 6000000000, total: 6000000000, speed: 0, error: '', dest_dir: 'D:\\Civitai\\checkpoints' },
    ];
    ({ page, errors } = await openPopup());
    await page.waitForFunction(() => document.querySelectorAll('.tab-row').length === 3);
    assert.equal(await page.locator('.tab-row input:checked').count(), 3);
    await page.locator('.tab-row').filter({ hasText: 'Gamma' }).locator('input').uncheck();
    await page.locator('#sendBatch').click();
    await page.waitForFunction(() => window.sent.length === 1);
    const sent = await page.evaluate(() => window.sent[0]);
    same(sent, ['https://civitai.red/models/101/a?modelVersionId=1001', 'https://civitai.com/models/202?modelVersionId=2002'],
      '发送集合必须与勾选项精确相等：' + JSON.stringify(sent));
    same(await page.evaluate(() => window.sent.length), 1, '一次点击只发一批');
    const blob = JSON.stringify(await page.evaluate(() => window.sent));
    assert(!/SECRET1|SECRET2|OUTSIDE|utm_source|fbclid|github\.com|images\/9001/.test(blob), blob);
    ok('发送集合 = 勾选集合（2 项），token/追踪参数被剥掉，未勾选的模型页与站外/图片页一条都没发');

    // ---- B4. 任务区：显示 CFT 侧的解析/下载状态、进度与落盘位置 ----
    await page.waitForFunction(() => !document.querySelector('#taskPanel').hidden);
    await page.waitForFunction(() => document.querySelectorAll('#taskList .task-item').length === 2);
    const taskTexts = await page.$$eval('#taskList .task-item', els => els.map(e => e.textContent));
    assert.equal(taskTexts.length, 2, '任务区只跟踪本次提交的那几个模型，不是所有标签页');
    const tA = taskTexts.find(t => t.includes('Alpha LoRA'));
    assert(tA && tA.includes('下载中 42') && tA.includes('3.2 MB/s') && tA.includes('D:\\Civitai\\loras'), JSON.stringify(taskTexts));
    const tB = taskTexts.find(t => t.includes('Beta Checkpoint'));
    assert(tB && tB.includes('已完成') && tB.includes('checkpoints'), JSON.stringify(taskTexts));
    assert((await page.textContent('#taskHint')).includes('已提交'), '必须说清"已提交 ≠ 已下载完成"');
    const ts4 = await page.textContent('#taskSummary');
    assert(ts4.includes('已完成 1') && ts4.includes('下载中 1'), ts4);
    assert(world.store.has('cftExtTasks'), '任务状态必须落在 chrome.storage，不能只在内存里');
    await waitUntil(() => {
      const s = world.store.get('cftExtTasks');
      return !!(s && s.items && s.items.length === 2 && s.items.some(i => i.cft && i.cft.phase === 'done'));
    }, '任务快照写回 storage（含已完成项）');
    const snap = world.store.get('cftExtTasks');
    same(snap.items.map(i => i.url), sent, '快照记的就是这次提交的那几条 URL');
    assert(snap.items.some(i => i.cft && i.cft.phase === 'done'), JSON.stringify(snap.items));
    ok('任务区显示每个模型的 CFT 状态（下载中 42% / 已完成）、字节口径的速度与落盘目录');

    // 「已提交但 CFT 没入队」也要看得见：改桥的应答后点「查询最新状态」
    world.rpc.tasks = world.rpc.tasks.filter(t => t.id !== 't2');
    world.rpc.parse = { running: false, total: 1, done: 1, finished: true,
      items: [{ url: 'https://civitai.com/models/202?modelVersionId=2002', ok: false, paid: true, msg: '需付费（Early Access）' }] };
    await page.locator('#taskRefresh').click();
    await page.waitForFunction(() => [...document.querySelectorAll('#taskList .task-item')].some(e => e.textContent.includes('需付费')));
    const after = await page.$$eval('#taskList .task-item', els => els.map(e => e.textContent));
    const b2 = after.find(t => t.includes('Beta Checkpoint'));
    assert(b2.includes('CFT 解析后没有入队') && b2.includes('仍要下载'), b2);
    assert((await page.textContent('#taskSummary')).includes('失败 1'), await page.textContent('#taskSummary'));
    ok('CFT 解析后没入队（付费/无版本）如实标成失败并给出下一步，不会被"已提交"掩盖');

    // ---- B5. 关掉小窗再开：任务区从 storage 恢复；CFT 不在线时不塌成"没有任务" ----
    await waitUntil(() => {
      const s = world.store.get('cftExtTasks');
      return !!(s && s.items && s.items.some(i => i.cft && i.cft.phase === 'failed'));
    }, '失败态也写回 storage');
    await page.close();
    world.bridgeOnline = false;              // 用户关了 CFT 再点开拓展小窗
    world.tabs = world.tabs.filter(t => t.id !== 11);   // 连标签页都关了，更能说明它不靠内存
    const again = await openPopup();
    await again.page.waitForFunction(() => !document.querySelector('#taskPanel').hidden);
    await again.page.waitForFunction(() => document.querySelectorAll('#taskList .task-item').length === 2);
    const restored = await again.page.$$eval('#taskList .task-item', els => els.map(e => e.textContent));
    assert.equal(restored.length, 2, JSON.stringify(restored));
    assert(restored.some(t => t.includes('下载中 42') && t.includes('D:\\Civitai\\loras')), restored.join('\n'));
    assert(restored.some(t => t.includes('CFT 解析后没有入队')), restored.join('\n'));
    const rsum = await again.page.textContent('#taskSummary');
    assert(rsum.includes('未连接 CFT') && rsum.includes('不是"没有任务"'), rsum);
    assert.equal(again.errors.length, 0, '界面脚本错误: ' + again.errors.join('\n'));
    ok('提交后关闭小窗再打开仍能看到跟踪状态（进度/落盘/失败原因都在），CFT 离线时如实说明而不是清空');
    await again.page.close();
  } finally {
    await browser.close();
    await new Promise(r => server.close(r));
  }
  }
  console.log('PASSED ' + passed);
})().catch(e => { console.error(e); process.exitCode = 1; });
