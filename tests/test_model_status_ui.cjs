/* Chrome 侧「下载状态」界面回归：
 *   1) 弹层：行徽章 / 下载状态筛选 / 只选未下载 / 下载选中项的新版（发 latest_url）
 *   2) 模型页胶囊：自动查询、点击强制刷新、storage 变更同步、bg 推送同步、离开模型页即移除
 *
 * 为什么用真 Chromium：假 DOM 测不出 hidden 面板里的控件点不到、徽章挂错节点、胶囊被 toast 盖住
 * 这类问题。状态数据由假 runtime/假桥提供，不联网、不读用户真实浏览器数据。
 *
 * 跑法：node tests/test_model_status_ui.cjs
 * （Playwright 路径默认取仓库 .tools/，可用 PLAYWRIGHT_PATH / PLAYWRIGHT_BROWSERS_PATH 覆盖）
 */
const fs = require('fs'), path = require('path'), assert = require('assert/strict'), http = require('http');
const root = path.resolve(__dirname, '..');
let passed = 0; const ok = (s) => { passed++; console.log('OK ' + s); };

function serve() {
  const server = http.createServer((req, res) => {
    const u = new URL(req.url, 'http://localhost');
    const p = u.pathname;
    let file = '';
    if (p === '/popup.html' || p === '/sidepanel.html' || p === '/popup.js' || p === '/sidepanel.js'
      || p === '/popup.css' || p === '/tabs-common.js' || p.startsWith('/lib/')
      || p.startsWith('/icons/')) file = path.join(root, 'chrome-extension', p);
    else if (p.startsWith('/ext/')) file = path.join(root, 'chrome-extension', p.slice(5));
    if (file) {
      fs.readFile(file, (e, b) => {
        if (e) { res.writeHead(404).end(); return; }
        res.setHeader('Content-Type', file.endsWith('.js') ? 'application/javascript'
          : file.endsWith('.css') ? 'text/css' : 'text/html');
        res.end(b);
      });
      return;
    }
    // 列表页：真实卡片结构（article + 绝对模型链接），钉住徽章贴在卡片上而不是链接里
    if (p === '/models') {
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.end('<!doctype html><html><head><meta charset="utf-8"/></head><body><div id="grid">'
        + '<article class="card" id="c101"><a href="https://civitai.red/models/101?modelVersionId=1001">Alpha</a>'
        + '<a href="https://civitai.red/models/101">Alpha no version</a></article>'
        + '<article class="card" id="c202"><a href="https://civitai.red/models/202">Beta</a></article>'
        + '<article class="card" id="c303"><a href="https://civitai.red/models/303?modelVersionId=3003">Gamma</a></article>'
        + '<article class="card" id="c404"><a href="https://civitai.red/models/404?modelVersionId=4004">Delta</a></article>'
        + '<article class="card" id="cx"><a href="https://example.com/models/505?modelVersionId=5005">External</a></article>'
        + '<article class="card" id="ci"><a href="/images/999">Image</a></article>'
        + '</div><script src="/ext/lib/util.js"></script>'
        + '<script src="/ext/lib/model-status.js"></script>'
        + '<script src="/ext/content/list-status.js"></script></body></html>');
      return;
    }
    // 任意 /models/… /images/… 路径都回同一张壳页：胶囊只看 location.pathname，不依赖站点渲染
    if (/^\/(models|images)\//.test(p)) {
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.end('<!doctype html><html><head><meta charset="utf-8"/></head><body><div id="root">page</div>'
        + '<script src="/ext/lib/util.js"></script>'
        + '<script src="/ext/lib/model-status.js"></script>'
        + '<script src="/ext/content/model-page.js"></script></body></html>');
      return;
    }
    res.writeHead(404).end();
  });
  return new Promise((r) => server.listen(0, '127.0.0.1', () => r({ server, base: 'http://127.0.0.1:' + server.address().port })));
}

const statusPopupInit = () => {
  window.__sent = []; window.__job = null; window.__statusQueries = []; window.__checkUpdates = null;
  window.__rpcTasks = [];
  window.__store = {
    // 页面历史：一条未下载（应显示在清单里）、一条已下载（应被过滤掉）
    cftPageHistoryV1: { version: 1, savedAt: Date.now(), items: {
      'model:999': { key: 'model:999', modelId: '999', versionId: '', firstAt: Date.now() - 60000,
        lastAt: Date.now() - 1000, count: 2, url: 'https://civitai.red/models/999', title: 'Seen But Not Downloaded' },
      'version:1001': { key: 'version:1001', modelId: '101', versionId: '1001', firstAt: Date.now() - 60000,
        lastAt: Date.now() - 2000, count: 1, url: 'https://civitai.red/models/101?modelVersionId=1001', title: 'Alpha seen' },
    } },
  };
  window.__statusMap = {
    1001: { state: 'downloaded', matched: 'version', version_matched: true,
      local: { source: 'library', path: 'D:\\models\\alpha.safetensors', version_id: 1001,
        version_name: 'v1', model_name: 'Alpha', exists: true } },
    2002: { state: 'update_available', matched: 'version', version_matched: true,
      local: { version_id: 2002, version_name: 'v1', model_name: 'Beta', exists: true },
      update: { has_update: true, behind: 1, latest_version: 2099, latest_name: 'v2',
        latest_url: 'https://civitai.com/models/202?modelVersionId=2099' } },
    4004: { state: 'not_downloaded' },
  };
  window.__tabs = [
    { id: 1, windowId: 1, title: 'Alpha | Civitai', url: 'https://civitai.red/models/101/a?modelVersionId=1001' },
    { id: 2, windowId: 1, title: 'Beta | Civitai', url: 'https://civitai.com/models/202?modelVersionId=2002' },
    { id: 3, windowId: 1, title: 'Delta | Civitai', url: 'https://civitai.com/models/404?modelVersionId=4004' },
  ];
  const keyOf = (mid, vid) => vid ? 'version:' + vid : 'model:' + mid;
  const storage = {
    async get(k) { return k in window.__store ? { [k]: window.__store[k] } : {}; },
    async set(o) { Object.assign(window.__store, JSON.parse(JSON.stringify(o))); return true; },
    async remove(k) { delete window.__store[k]; return true; },
  };
  window.chrome = {
    storage: { local: storage },
    runtime: {
      id: 'test-id', lastError: undefined,
      getURL: (p) => 'chrome-extension://test-id/' + p,
      sendMessage: async (m) => {
        if (m && m.type === 'cft-batch-status') return { ok: true, job: window.__job };
        if (m && m.type === 'cft-batch-start') {
          window.__sent.push(m.urls.slice());
          window.__job = { id: 'job-' + window.__sent.length, running: false, completedKeys: [],
            items: m.urls.map((u) => Object.assign({}, CftTabs.modelLink(u), { status: 'sent', msg: '已接受' })) };
          return { ok: true, job: window.__job };
        }
        if (m && m.type === 'cft-batch-clear') { window.__job = null; return { ok: true }; }
        if (m && m.__cft && m.type === 'modelStatusQuery') {
          const p = (m.payload && m.payload.items) || [];
          window.__statusQueries.push(p);
          const items = p.map((it) => {
            const spec = window.__statusMap[String(it.versionId)] || window.__statusMap[String(it.modelId)]
              || { state: 'unknown', reason: '测试没有配置这个版本' };
            return Object.assign({ key: keyOf(it.modelId, it.versionId), modelId: String(it.modelId || ''),
              versionId: String(it.versionId || ''), checkedAt: Date.now() }, spec);
          });
          const byKey = {}; for (const e of items) byKey[e.key] = e;
          return { ok: true, items, byKey, cacheAt: Date.now(),
            library: { ready: true, scanning: false, count: 3 },
            updates: { checked_at: Math.floor(Date.now() / 1000), count: 1 }, offline: false };
        }
        if (m && m.__cft && m.type === 'checkModelUpdates') {
          window.__checkUpdates = m.payload;
          return { ok: true, started: true, msg: '已开始检查更新' };
        }
        if (m && m.__cft && m.type === 'modelUpdateState') {
          return { ok: true, state: { running: false, done: 1, total: 1, newer: 1,
            msg: '检查完成：1 个模型有更新' } };
        }
        if (m && m.__cft && m.type === 'getArchiveConfig') return { channel: 'browser' };
        if (m && m.__cft && m.type === 'archiveBatchStatus') return { running: false, total: 0 };
        return { ok: true };
      },
    },
    tabs: { query: async (q) => (q && q.active) ? [window.__tabs[0]]
      : (q && q.currentWindow) ? window.__tabs.filter((t) => t.windowId === 1) : window.__tabs },
    action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {}, setTitle: async () => {} },
  };
  window.fetch = async (u, o) => {
    const url = String(u);
    if (url.indexOf('/api/health') >= 0) {
      return { ok: true, status: 200, json: async () => ({ ok: true, app: 'CivitaiFreeTool', version: '2.6.33', mode: 'window', window: true }) };
    }
    if (url.indexOf('/api/rpc') >= 0) {
      let req = {};
      try { req = JSON.parse((o && o.body) || '{}'); } catch (_) {}
      if (req.method === 'get_tasks') return { ok: true, status: 200, json: async () => ({ result: window.__rpcTasks }) };
      if (req.method === 'get_parse_state') {
        return { ok: true, status: 200, json: async () => ({ result: { running: false, items: [], finished: true } }) };
      }
      if (req.method === 'library_check') return { ok: true, status: 200, json: async () => ({ result: { ok: true, found: false, paths: [] } }) };
      return { ok: true, status: 200, json: async () => ({ result: [] }) };
    }
    return { ok: false, status: 404, json: async () => null, text: async () => 'null' };
  };
};

const listStatusInit = () => {
  const now = Date.now();
  window.__queries = []; window.__storageListener = null; window.__scans = 0;
  window.__store = { cftModelStatusV1: { version: 1, savedAt: now, entries: {
    'version:1001': { key: 'version:1001', modelId: '101', versionId: '1001', state: 'downloaded',
      checkedAt: now, local: { version_id: 1001, version_name: 'v1', exists: true } },
    'model:202': { key: 'model:202', modelId: '202', versionId: '', state: 'not_downloaded', checkedAt: now },
    // checkedAt=0：明确陈旧，列表页应只对这一条问后台
    'version:3003': { key: 'version:3003', modelId: '303', versionId: '3003', state: 'unknown',
      checkedAt: 0, reason: '还没查' },
    'version:4004': { key: 'version:4004', modelId: '404', versionId: '4004', state: 'update_available',
      checkedAt: now, local: { version_id: 4004, version_name: 'v1', exists: true },
      update: { has_update: true, latest_version: 4099, latest_name: 'v2' } },
  } } };
  window.chrome = {
    runtime: {
      id: 'test-id', lastError: undefined,
      sendMessage: (m, cb) => {
        window.__queries.push(m);
        if (typeof cb === 'function') setTimeout(() => cb(null), 0);
      },
    },
    storage: {
      local: { get: (k, cb) => { window.__scans++; return setTimeout(() => cb({ [k]: window.__store[k] }), 0); } },
      onChanged: { addListener: (f) => { window.__storageListener = f; } },
    },
  };
};

const statusContentInit = () => {
  window.__statusCalls = [];
  window.__runtimeListeners = [];
  window.__storageListener = null;
  window.__statusEntry = { state: 'downloaded', matched: 'version', version_matched: true,
    local: { version_id: 456, version_name: 'v1', model_name: 'Demo', exists: true } };
  window.chrome = {
    runtime: {
      id: 'test-id', lastError: undefined,
      sendMessage: (m, cb) => {
        if (m && m.type === 'modelStatusQuery') {
          window.__statusCalls.push(m.payload);
          const entry = Object.assign({ key: 'version:456', modelId: '123', versionId: '456' }, window.__statusEntry);
          if (typeof cb === 'function') setTimeout(() => cb({ ok: true, items: [entry], cacheAt: Date.now(),
            library: { ready: true, scanning: false } }), 0);
        }
      },
      onMessage: { addListener: (f) => window.__runtimeListeners.push(f) },
    },
    storage: {
      local: { get: async () => ({}) },
      onChanged: { addListener: (f) => { window.__storageListener = f; } },
    },
  };
};

(async () => {
  const pwPath = process.env.PLAYWRIGHT_PATH || path.join(root, '.tools', 'node_modules', 'playwright');
  process.env.PLAYWRIGHT_BROWSERS_PATH = process.env.PLAYWRIGHT_BROWSERS_PATH || path.join(root, '.tools', 'ms-playwright');
  let chromium = null;
  try { ({ chromium } = require(pwPath)); }
  catch (e) { console.log('SKIP 真浏览器部分：playwright 不可用（' + pwPath + '）：' + e.message); process.exit(0); }
  const { server, base } = await serve();
  const browser = await chromium.launch({ headless: true });
  try {
    // ================= 1. 弹层 =================
    {
      const context = await browser.newContext();
      await context.addInitScript(statusPopupInit);
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', (e) => errors.push(e.message));
      await page.goto(base + '/popup.html');
      await page.waitForFunction(() => document.querySelectorAll('.tab-row').length === 3);
      await page.waitForFunction(() => /已下载 1/.test(document.querySelector('#dlSummary').textContent));
      const sum = await page.textContent('#dlSummary');
      assert(sum.includes('有更新 1') && sum.includes('未下载 1'), sum);
      assert.equal(await page.locator('.tab-row .dl-state').count(), 3, '每行都要有下载状态徽章');
      const badges = await page.$$eval('.tab-row .dl-state', (els) => els.map((e) => e.textContent));
      assert(badges.some((t) => t.includes('已下载')) && badges.some((t) => t.includes('有更新'))
        && badges.some((t) => t.includes('未下载')), badges.join(','));
      await page.locator('#modeBatch').click();
      await page.selectOption('#fDl', 'downloaded');
      await page.waitForFunction(() => document.querySelectorAll('.tab-row').length === 1);
      assert((await page.textContent('.tab-row')).includes('Alpha'), '按「已下载」筛选只剩 Alpha');
      await page.selectOption('#fDl', '');
      await page.waitForFunction(() => document.querySelectorAll('.tab-row').length === 3);
      ok('Chrome 弹层：行徽章与「已下载/有更新/未下载」汇总可见，下载状态筛选能收敛列表');

      await page.locator('#selectUndownloaded').click();
      assert.equal(await page.textContent('#selectionCount'), '已选 1 / 3', await page.textContent('#selectionCount'));
      const checkedTexts = await page.$$eval('.tab-row', (rows) => rows.filter((r) => r.querySelector('input').checked).map((r) => r.textContent));
      assert.equal(checkedTexts.length, 1, checkedTexts.join('\n'));
      assert(checkedTexts[0].includes('Delta'), '只选未下载：状态未知的不能被误选：' + checkedTexts[0]);
      ok('Chrome 弹层：「只选未下载」只勾确认未下载的页面（已下载/有更新/未知都不动）');

      await page.locator('.tab-row').filter({ hasText: 'Delta' }).locator('input').uncheck();
      await page.locator('.tab-row').filter({ hasText: 'Beta' }).locator('input').check();
      await page.waitForFunction(() => !document.querySelector('#updateBar').hidden);
      assert((await page.textContent('#btnSendUpdates')).includes('（1）'), await page.textContent('#btnSendUpdates'));
      await page.locator('#btnSendUpdates').click();
      await page.waitForFunction(() => window.__sent.length === 1);
      assert.deepEqual(await page.evaluate(() => window.__sent[0]),
        ['https://civitai.com/models/202?modelVersionId=2099'],
        '「下载选中项的新版」必须发 update.latest_url，不是页面里的旧版本');
      await page.waitForFunction(() => (window.__store.cftExtTasks?.items || [])
        .some((it) => it.modelId === '202' && it.versionId === '2099'));
      ok('Chrome 弹层：「下载选中项的新版」只发最新版 URL，提交跟踪记录新版本号');

      // 旧版本的 done 任务不能冒充这次下载完成（同模型多版本是常态）
      await page.evaluate(() => {
        window.__rpcTasks = [{ id: 'old', url: 'https://civitai.com/models/202', version_id: '2002',
          status: 'done', filename: 'old.safetensors', dest_dir: 'D:\\old' }];
      });
      await page.locator('#taskRefresh').click();
      await page.waitForFunction(() => {
        const t = document.querySelector('#taskList .task-item');
        return t && !t.classList.contains('done');
      });
      const staleText = await page.textContent('#taskList');
      assert(!staleText.includes('已完成'), '旧版本任务不能显示成这次已完成：' + staleText);
      await page.evaluate(() => {
        window.__rpcTasks = [{ id: 'new', url: 'https://civitai.com/models/202', version_id: '2099',
          status: 'done', filename: 'new.safetensors', dest_dir: 'D:\\models' }];
      });
      await page.locator('#taskRefresh').click();
      await page.waitForFunction(() => document.querySelector('#taskList .task-item.done')?.textContent.includes('new.safetensors'));
      ok('Chrome 弹层任务跟踪按版本认任务：旧 done 不冒充，本版本落地才显示已完成');

      // 页面历史：已下载的被过滤，未下载的留一条；一键清空只清历史
      await page.waitForFunction(() => !document.querySelector('#pageHistoryPanel').hidden
        && document.querySelectorAll('#pageHistoryList .hist-item').length === 1);
      const histText = await page.textContent('#pageHistoryList');
      assert(histText.includes('Seen But Not Downloaded') && !histText.includes('Alpha seen'), histText);
      page.on('dialog', (d) => d.accept());
      await page.locator('#pageHistoryPanel').evaluate((el) => { el.open = true; });
      await page.locator('#clearPageHistory').click();
      await page.waitForFunction(() => window.__store.cftPageHistoryV1
        && Object.keys(window.__store.cftPageHistoryV1.items).length === 0);
      ok('Chrome 弹层：页面历史只列「看过未下载」，一键清除不动别的记录');

      // 一键检查更新：只把模型库命中的本地路径交给后台，进度来自桌面端
      await page.waitForFunction(() => !document.querySelector('#btnCheckUpdates').disabled);
      await page.locator('#btnCheckUpdates').click();
      await page.waitForFunction(() => (window.__checkUpdates?.paths || []).length > 0);
      const checkPaths = await page.evaluate(() => window.__checkUpdates.paths);
      assert(checkPaths.includes('D:\\models\\alpha.safetensors'), checkPaths.join(','));
      await page.waitForFunction(() => /检查完成/.test(document.querySelector('#feedback').textContent),
        null, { timeout: 10000 });
      ok('Chrome 弹层：一键检查更新只发模型库本地路径，完成文案来自桌面端进度');

      assert.equal(errors.length, 0, '弹层脚本错误：' + errors.join('\n'));
      await context.close();
    }

    // ================= 2. 模型页胶囊 =================
    {
      const context = await browser.newContext();
      await context.addInitScript(statusContentInit);
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', (e) => errors.push(e.message));
      await page.goto(base + '/models/123/alpha?modelVersionId=456');
      await page.waitForFunction(() => document.querySelector('#cft-status-chip')?.dataset.state === 'downloaded');
      const chipText = await page.textContent('#cft-status-chip');
      assert(chipText.includes('已下载'), '胶囊要显示缓存里的已下载：' + chipText);
      assert.equal(await page.getAttribute('#cft-status-chip', 'class'),
        'cft-status-chip cft-status-downloaded', '胶囊类名要带状态，样式才有颜色');
      const calls = await page.evaluate(() => window.__statusCalls);
      assert.equal(calls.length, 1, '首屏只应自动查询一次：' + calls.length);
      assert.equal(calls[0].items[0].versionId, '456', '查询要用页面里的版本 ID：' + JSON.stringify(calls[0]));
      assert.equal(calls[0].refresh, 'auto');

      await page.evaluate(() => {
        window.__statusEntry = { state: 'update_available', update: { has_update: true, latest_name: 'v2' } };
      });
      await page.locator('#cft-status-chip').click();
      await page.waitForFunction(() => document.querySelector('#cft-status-chip').dataset.state === 'update_available');
      assert((await page.textContent('#cft-status-chip')).includes('有更新'), await page.textContent('#cft-status-chip'));
      assert.equal((await page.evaluate(() => window.__statusCalls)).length, 2, '点击胶囊要强制刷新一次');
      ok('模型页胶囊：首屏自动查询并显示已下载，点击强制刷新后有更新状态即时变化');

      // 弹层/后台写缓存 → storage.onChanged 只读同步，不再打桥
      const before = await page.evaluate(() => window.__statusCalls.length);
      await page.evaluate(() => {
        window.__storageListener({ cftModelStatusV1: { newValue: { entries: {
          'version:456': { key: 'version:456', modelId: '123', versionId: '456', state: 'other_version',
            local: { version_id: 499, version_name: 'v0', exists: true } },
        } } } }, 'local');
      });
      await page.waitForFunction(() => document.querySelector('#cft-status-chip').dataset.state === 'other_version');
      assert.equal(await page.evaluate(() => window.__statusCalls.length), before, 'storage 同步不该再请求后台');
      assert((await page.textContent('#cft-status-chip')).includes('其他版本'), await page.textContent('#cft-status-chip'));

      // 后台推送 → 直接渲染，也不打桥
      await page.evaluate(() => {
        for (const f of window.__runtimeListeners) {
          f({ __cft: true, to: 'content', type: 'modelStatusPush',
            payload: { entry: { state: 'downloading', download: { status: 'downloading', progress: 42 } } } }, {}, () => {});
        }
      });
      await page.waitForFunction(() => document.querySelector('#cft-status-chip').dataset.state === 'downloading');
      const dlText = await page.textContent('#cft-status-chip');
      assert(dlText.includes('下载中') && dlText.includes('42%'), '推送后胶囊要显示进度：' + dlText);
      assert.equal(await page.evaluate(() => window.__statusCalls.length), before, '推送不该再请求后台');
      ok('模型页胶囊：storage 变更与 background 推送都能即时更新，且不额外请求本机桥');

      await page.evaluate(() => history.pushState({}, '', '/images/789'));
      await page.waitForFunction(() => !document.querySelector('#cft-status-chip') && !document.querySelector('.cft-archive-btn'));
      assert.equal(errors.length, 0, '内容脚本错误：' + errors.join('\n'));
      await context.close();
      ok('离开模型页（SPA 路由）后胶囊与归档按钮都被移除，脚本 0 错误');
    }

    // ================= 3. C 站列表页卡片徽章 =================
    {
      const context = await browser.newContext();
      await context.addInitScript(listStatusInit);
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', (e) => errors.push(e.message));
      await page.goto(base + '/models');
      await page.waitForFunction(() => document.querySelectorAll('.cft-list-status').length === 3);
      const cards = await page.evaluate(() => {
        const out = {};
        for (const card of document.querySelectorAll('article.card')) {
          const b = card.querySelector('.cft-list-status');
          out[card.id] = b ? { text: b.textContent, cls: b.className } : null;
        }
        return out;
      });
      assert(cards.c101 && cards.c101.text.includes('已下载'), JSON.stringify(cards));
      assert(cards.c202 && cards.c202.text.includes('未下载'), JSON.stringify(cards));
      assert(cards.c404 && cards.c404.text.includes('有更新'), JSON.stringify(cards));
      assert.equal(cards.c303, null, 'unknown 不贴徽章（还没查不是结论）');
      assert.equal(cards.cx, null, '站外链接不贴');
      assert.equal(cards.ci, null, '图片链接不贴');
      assert(cards.c101.cls.includes('dl-downloaded'), cards.c101.cls);
      assert.equal(await page.locator('#c101 .cft-list-status').count(), 1, '同一卡片多链接只贴一枚');
      const queries = await page.evaluate(() => window.__queries);
      assert.equal(queries.length, 1, '只查陈旧/缺失项一次：' + JSON.stringify(queries));
      assert.equal(queries[0].payload.badge, false, '列表页不能写工具栏徽章');
      assert.deepEqual(queries[0].payload.items.map((i) => i.key), ['model:101', 'version:3003'],
        '缺失的模型级链接与陈旧版本各查一次；新鲜缓存不再问');
      // 徽章写 DOM 不能无限自激：插入徽章会带来一次收尾扫描，但之后必须彻底停下来
      const scansA = await page.evaluate(() => window.__scans);
      await page.waitForTimeout(1200);
      const scansB = await page.evaluate(() => window.__scans);
      await page.waitForTimeout(1200);
      const scansC = await page.evaluate(() => window.__scans);
      assert(scansB - scansA <= 1, '徽章渲染后还在反复重扫：1.2s 内扫了 ' + (scansB - scansA) + ' 次');
      assert.equal(scansC, scansB, '扫描没有收敛：第二个 1.2s 又多扫了 ' + (scansC - scansB) + ' 次');
      // storage 变更只重绘、不再打桥
      await page.evaluate(() => {
        // 真实 chrome.storage 会先落新值再广播；假环境要把 store 与事件一起更新
        window.__store.cftModelStatusV1 = { version: 1, savedAt: Date.now(), entries: {
          'version:3003': { key: 'version:3003', modelId: '303', versionId: '3003', state: 'downloaded',
            checkedAt: Date.now(), local: { version_id: 3003, version_name: 'v1', exists: true } },
        } };
        window.__storageListener({ cftModelStatusV1: { newValue: window.__store.cftModelStatusV1 } }, 'local');
      });
      await page.waitForFunction(() => document.querySelector('#c303 .cft-list-status')?.textContent.includes('已下载'));
      assert.equal(await page.evaluate(() => window.__queries.length), 1, 'storage 同步不该再请求后台');
      assert.equal(errors.length, 0, '列表页脚本错误：' + errors.join('\n'));
      await context.close();
      ok('C 站列表页：✓/↓/↑ 贴在卡片上、unknown/站外/图片不贴、陈旧项单次查询且 badge:false');
    }

    // ================= 4. Chrome 侧边栏 =================
    {
      const context = await browser.newContext();
      await context.addInitScript(statusPopupInit);
      await context.addInitScript(() => { window.__closed = 0; window.close = () => { window.__closed++; }; });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', (e) => errors.push(e.message));
      await page.goto(base + '/sidepanel.html');
      await page.waitForFunction(() => document.querySelectorAll('.tab-row').length === 3);
      assert.equal(await page.evaluate(() => document.body.classList.contains('side-panel')), true);
      assert.equal(await page.locator('#openSidePanel').isHidden(), true);
      assert.equal(await page.evaluate(() => window.__closed), 0, '侧栏页不该自动关自己');
      assert.equal(errors.length, 0, '侧栏脚本错误：' + errors.join('\n'));
      await context.close();
      ok('Chrome 侧边栏：复用同一套弹层脚本，side-panel 样式生效、入口隐藏且不自动关闭');
    }
  } finally {
    await browser.close();
    await new Promise((r) => server.close(r));
  }
  console.log('PASSED ' + passed);
})().catch((e) => { console.error(e); process.exitCode = 1; });
