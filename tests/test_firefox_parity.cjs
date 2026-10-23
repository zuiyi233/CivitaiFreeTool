/* Firefox 侧与 Chrome 侧的平价回归（任务：把 tabrows / enrich / 任务区接进 Firefox 弹层）。
 *
 * 为什么要有这个文件：Firefox 版和 Chrome 版共用同一份纯逻辑模块，"复制过去"这件事本身
 * 极易悄悄分叉（改一个字段、顺手洗一下格式），分叉后的表现是两边列表行为不一样，而不是报错。
 * 所以这里钉三件事：
 *   1) firefox-extension 里的 tabrows.js / enrich.js / tabs-common.js 与 chrome 版**逐字节相同**；
 *   2) Firefox 的能力边界写在 manifest/README/popup 里且互相一致（没注入、没 offscreen/downloads/
 *      scripting/<all_urls>，也没有"点了没反应"或"显示成已归档"的假归档入口）；
 *   3) popup.js 在假 chrome + 假 DOM 下真的跑通筛选 / 汇总 / 富化徽章 / 任务区，
 *      并且依赖缺失（lib 没加载成功）时退回 CftTabs.candidates 的兜底路径仍然可用。
 *
 * 全程不联网：C 站公开接口与本机桥都由假 fetch 应答。假 Response 必须同时给 json() 和 text()
 * ——lib/enrich.js 的取数层读的是 resp.text()，只给 json() 会让整批富化"取数失败"，
 * 测出来的就是假故障现场（同一份坑写在 tests/test_extension_batch.cjs 的注释里）。
 *
 * 跑法：node tests/test_firefox_parity.cjs
 */
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const http = require('http');
const root = path.resolve(__dirname, '..');
let passed = 0; const ok = (s) => { passed++; console.log('OK ' + s); };
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const clone = (v) => JSON.parse(JSON.stringify(v));
const same = (actual, expected, msg) => assert.equal(JSON.stringify(actual ?? null), JSON.stringify(expected ?? null), msg);
const read = (rel) => fs.readFileSync(path.join(root, rel));
// 静态伺服：把扩展目录当普通网页跑起来（Playwright 加载 MV3 弹层的既有做法）
async function serveDir(dir) {
  const server = http.createServer((req, res) => {
    const p = path.join(root, dir, new URL(req.url, 'http://localhost').pathname);
    fs.readFile(p, (e, b) => {
      if (e) { res.writeHead(404).end(); return; }
      res.setHeader('Content-Type', p.endsWith('.js') ? 'application/javascript' : p.endsWith('.css') ? 'text/css' : 'text/html');
      res.end(b);
    });
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  return { server, port: server.address().port };
}

// ---- 依赖序加载基座（与 tests/test_extension_batch.cjs / test_popup_integration.cjs 同一份） ----
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
const vmSetInterval = (fn, ms) => { const t = setInterval(fn, ms); if (t && t.unref) t.unref(); return t; };

// ---- 假 DOM：只实现 popup.js 真正用到的那几件事（id 取元素 / textContent / children / 事件） ----
function makeEl(tag) {
  const el = {
    tagName: String(tag || 'div').toUpperCase(), id: '', children: [], parent: null,
    _text: '', _classes: new Set(), _handlers: {}, dataset: {},
    hidden: false, disabled: false, checked: false, indeterminate: false, open: false,
    value: '', title: '', src: '', alt: '', loading: '', type: '',
  };
  Object.defineProperty(el, 'className', {
    get() { return [...el._classes].join(' '); },
    set(v) { el._classes = new Set(String(v || '').split(/\s+/).filter(Boolean)); },
  });
  el.classList = {
    toggle(n, force) { const on = force === undefined ? !el._classes.has(n) : !!force; if (on) el._classes.add(n); else el._classes.delete(n); return on; },
    add(...n) { for (const x of n) el._classes.add(x); },
    remove(...n) { for (const x of n) el._classes.delete(x); },
    contains(n) { return el._classes.has(n); },
  };
  Object.defineProperty(el, 'textContent', {
    // 真实 DOM：先赋 textContent 再 append 子节点，文本是"自身 + 子节点"拼接（行徽章就是靠这个叠在名字后面）
    get() { return el._text + el.children.map((c) => c.textContent).join(''); },
    set(v) { el._text = String(v); el.children = []; },
  });
  el.append = (...nodes) => { for (const n of nodes) { if (n == null) continue; n.parent = el; el.children.push(n); } };
  el.replaceChildren = (...nodes) => { el.children = []; el.append(...nodes); };
  el.remove = () => { if (el.parent) el.parent.children = el.parent.children.filter((c) => c !== el); el.parent = null; };
  el.addEventListener = (t, f) => { (el._handlers[t] = el._handlers[t] || []).push(f); };
  el.dispatch = (t) => { for (const f of (el._handlers[t] || [])) f({ type: t, target: el }); };
  el.find = (pred) => (pred(el) ? [el] : el.children.flatMap((c) => c.find(pred)));
  return el;
}
// 元素表直接从 popup.html 里解析：这样"HTML 里有没有这个控件"和"popup.js 取的是不是同一个 id"
// 被同一条断言管住 —— 浏览器里 getElementById 返回 null 的地方，这里也返回 null。
function makeDocument(html) {
  const byId = new Map();
  for (const m of html.matchAll(/<([a-z0-9]+)[^>]*?\bid="([^"]+)"/gi)) {
    const el = makeEl(m[1]); el.id = m[2];
    // hidden 是属性而不是 JS 赋值时，也必须按 HTML 的初值来（否则"面板已收起"这种断言会假通过）
    el.hidden = /\bhidden\b/i.test(m[0]);
    byId.set(m[2], el);
  }
  // <select> 的初始 value = 第一个 option 的 value（浏览器就是这个语义）
  for (const m of html.matchAll(/<select[^>]*?\bid="([^"]+)"[^>]*>([\s\S]*?)<\/select>/gi)) {
    const el = byId.get(m[1]); if (!el) continue;
    const first = /<option[^>]*?\bvalue="([^"]*)"/i.exec(m[2]);
    el.value = first ? first[1] : '';
  }
  const doc = {
    hidden: false, _handlers: {}, byId,
    getElementById: (id) => byId.get(id) || null,
    createElement: (tag) => makeEl(tag),
    addEventListener: (t, f) => { (doc._handlers[t] = doc._handlers[t] || []).push(f); },
    fire: (t) => { for (const f of (doc._handlers[t] || [])) f({ type: t }); },
  };
  return doc;
}

// ---- 世界：混合标签页 + 编排好的假接口应答 ----
// 5 个模型页（101 的 red/com 同版本并成一行 → 4 行）+ github + 图片页 + CDN + 列表页 = 9 个标签页。
// 404 那行是"补全成功"的对照组：类型/底模筛选项只能从有 meta 的行里长出来，
// 而 needs-login / rate-limited 两行没有 meta —— 少了这个对照组就测不出"筛选项自动生成"。
const TABS = [
  { id: 11, windowId: 1, title: 'Alpha | Civitai', url: 'https://civitai.red/models/101/a?modelVersionId=1001&token=SECRET1&utm_source=chatgpt.com' },
  { id: 12, windowId: 1, title: 'Alpha 重复页', url: 'https://civitai.com/models/101/b?modelVersionId=1001' },
  { id: 13, windowId: 1, title: 'Beta | Civitai', url: 'https://civitai.com/models/202?modelVersionId=2002&fbclid=abc' },
  { id: 14, windowId: 2, title: 'Gamma | Civitai', url: 'https://civitai.red/models/303?token=SECRET2' },
  { id: 19, windowId: 2, title: 'Delta | Civitai', url: 'https://civitai.com/models/404?modelVersionId=4004' },
  { id: 15, windowId: 1, title: 'GitHub', url: 'https://github.com/x/y?token=OUTSIDE' },
  { id: 16, windowId: 1, title: 'Civitai image', url: 'https://civitai.com/images/9001' },
  { id: 17, windowId: 1, title: 'CDN', url: 'https://image.civitai.com/x/9001.png' },
  { id: 18, windowId: 1, title: '列表页', url: 'https://civitai.com/models?types=Lora' },
];
const ACTIVE = { id: 11, windowId: 1, title: 'Alpha | Civitai', url: TABS[0].url };
const modelDoc = (id, name, type, base, sizeKB, versionId) => ({
  id: Number(id), name, modelType: type, creator: { username: 'maker' },
  stats: { downloadCount: 100, ratingCount: 7 }, nsfw: false, nsfwLevel: 1, tags: ['style'],
  modelVersions: [{ id: versionId != null ? versionId : 1000 + Number(id), name: 'v1', baseModel: base,
    publishedAt: '2026-01-05T00:00:00Z', trainedWords: ['word'],
    images: [{ url: 'https://image.civitai.com/x/original=true/' + id + '.png', width: 1024, height: 1024, nsfwLevel: 1 }],
    files: [{ id: 1, name: name + '.safetensors', sizeKB, type: 'Model' }] }],
});
const DOCS = {
  101: modelDoc(101, 'Alpha LoRA', 5, 'SDXL', 150000, 1001),
  202: modelDoc(202, 'Beta Checkpoint', 1, 'SD 1.5', 6000000, 2002),
  303: modelDoc(303, 'Gamma Flux', 5, 'Flux.1 d', 240000),
  404: modelDoc(404, 'Delta Lora', 5, 'SDXL', 120000, 4004),
};

function popupWorld(opt = {}) {
  const html = read('firefox-extension/popup.html').toString('utf8');
  const document = makeDocument(html);
  const stored = opt.stored || {};
  const world = {
    stored, document, netLog: [], runtimeCalls: [], sent: [], job: null, statusLog: [],
    revealLog: [],
    modelStatus: opt.modelStatus || {},
    bridgeOnline: opt.bridgeOnline !== false, apiStatus: opt.apiStatus || {},
    rpc: { tasks: [], parse: { running: false, items: [], finished: true } },
  };
  const storage = {
    async get(k) { const out = {}; if (k && stored[k] !== undefined) out[k] = clone(stored[k]); return out; },
    async set(v) { Object.assign(stored, clone(v)); return true; },
    async remove(k) { delete stored[k]; return true; },
  };
  // callbackStyle:true 模拟"chrome.* 命名空间不返 Promise"的那类宿主：get/set 只在传回调时才交付，
  // 返回值是 undefined。lib 的缓存层遇到这种宿主必须静默退化，界面要如实说明（不能假装缓存生效）。
  const rawStorage = opt.callbackStyle
    ? { local: {
        // 只在调用方传了回调时才交付结果，返回值恒为 undefined —— 这就是那类宿主的语义
        get(k, cb) { if (typeof cb === 'function') cb({ [k]: clone(stored[k]) }); return undefined; },
        set(obj, cb) { if (typeof cb === 'function') storage.set(obj).then(() => cb()); return undefined; },
      } }
    : { local: storage };
  const ext = {
    runtime: mkNs('runtime', {
      id: 'test-id', getURL: (p) => 'moz-extension://test-id/' + p,
      sendMessage: async (m) => {
        world.runtimeCalls.push(m);
        if (m && m.__cft) throw new Error('Firefox 版不该发归档消息：' + m.type);
        if (m.type === 'cft-batch-start') {
          world.sent.push(m.urls.slice());
          world.job = { id: 'job-' + world.sent.length, running: false, completedKeys: [],
            items: m.urls.map((u) => Object.assign({}, ctx.CftTabs.modelLink(u), { status: 'sent', msg: '已接受' })) };
          return { ok: true, job: world.job };
        }
        if (m.type === 'cft-batch-clear') { world.job = null; return { ok: true, job: null }; }
        return { ok: true, job: world.job };
      },
    }),
    storage: rawStorage,
    tabs: mkNs('tabs', { query: async (q) => {
      if (q && q.active) return [Object.assign({}, ACTIVE)];
      if (q && q.currentWindow) return TABS.filter((t) => t.windowId === 1).map(clone);
      return TABS.map(clone);
    } }),
    contextMenus: mkNs('contextMenus'), action: mkNs('action'),
  };
  const body = (u, o) => {
    if (String(u).indexOf('127.0.0.1:47531') < 0) {
      const id = (/\/api\/v1\/(?:models|model-versions\/mini)\/(\d+)/.exec(String(u)) || [])[1] || '';
      const st = world.apiStatus[id] || 200;
      return st === 200 ? { ok: true, status: 200, body: DOCS[id] || null } : { ok: false, status: st, body: null };
    }
    const p = new URL(String(u)).pathname;
    if (!world.bridgeOnline) throw new Error('Failed to fetch');
    if (p === '/api/health') return { ok: true, status: 200, body: { ok: true, app: 'CivitaiFreeTool', version: '2.6.30', mode: 'window', window: true } };
    if (p === '/api/model/status') {
      const req = JSON.parse((o && o.body) || '{}');
      world.statusLog.push(req);
      return { ok: true, status: 200, body: {
        ok: true, generated_at: Math.floor(Date.now() / 1000),
        library: { ready: true, scanning: false, count: 4 },
        updates: { checked_at: Math.floor(Date.now() / 1000), count: 1 },
        items: (req.items || []).map((it) => Object.assign(
          { model_id: it.model_id, version_id: it.version_id },
          world.modelStatus[String(it.version_id)] || world.modelStatus[String(it.model_id)]
            || { state: 'not_downloaded', reason: '测试里没给这个版本配置状态' })),
      } };
    }
    if (p === '/api/reveal') {
      const req = JSON.parse((o && o.body) || '{}');
      world.revealLog.push(req);
      return { ok: true, status: 200, body: { ok: true, path: req.path } };
    }
    if (p === '/api/rpc') {
      const req = JSON.parse((o && o.body) || '{}');
      if (req.method === 'get_tasks') return { ok: true, status: 200, body: { result: world.rpc.tasks } };
      if (req.method === 'get_parse_state') return { ok: true, status: 200, body: { result: world.rpc.parse } };
      if (req.method === 'library_check') return { ok: true, status: 200, body: { result: { ok: true, found: false, paths: [] } } };
      if (req.method === 'mm_check_updates') {
        world.rpc.check = req.args;
        return { ok: true, status: 200, body: { result: { started: true, msg: '已开始检查' } } };
      }
      if (req.method === 'get_mm_update_state') {
        return { ok: true, status: 200, body: { result: { running: false, done: 1, total: 1,
          newer: 1, msg: '检查完成：1 个模型有更新' } } };
      }
      return { ok: false, status: 400, body: { error: '测试桥不支持：' + req.method } };
    }
    return { ok: false, status: 404, body: { ok: false } };
  };
  const ctx = vm.createContext({
    chrome: ext, URL, URLSearchParams, AbortController, setTimeout, clearTimeout,
    setInterval: vmSetInterval, clearInterval, queueMicrotask, console, TextEncoder, TextDecoder,
    structuredClone: clone, Promise, document, confirm: () => true,
    window: { addEventListener: () => {}, close: () => {}, chrome: ext },
    fetch: async (u, o) => {
      world.netLog.push({ url: String(u), headers: (o && o.headers) || {}, credentials: o && o.credentials, body: o && o.body });
      const r = body(u, o);
      // 两个方法都给：enrich 的取数层读 text()，popup 的 health/rpc 读 json()
      return { ok: r.ok, status: r.status, json: async () => r.body, text: async () => JSON.stringify(r.body ?? null) };
    },
  });
  // 加载顺序由 popup.html 里的 <script src> 决定：测试照着它加载，等于把"顺序"也钉住了
  const srcs = [...html.matchAll(/<script src="([^"]+)"/g)].map((m) => m[1]);
  world.scriptSrcs = srcs;
  const wanted = opt.skipLibs ? srcs.filter((s) => !/^lib\//.test(s)) : srcs;
  for (const s of wanted) loadIn(ctx, 'firefox-extension/' + s);
  world.ctx = ctx;
  world.el = (id) => document.getElementById(id);
  world.rowsText = () => world.el('tabList').children.map((c) => c.textContent);
  world.rowInputs = () => world.el('tabList').children.map((c) => c.find((x) => x.tagName === 'INPUT')[0]);
  world.wait = async (fn, label, ms = 6000) => {
    for (let i = 0; i < Math.max(1, ms / 15); i++) { if (await fn()) return; await sleep(15); }
    throw new Error('等待条件未成立: ' + label + ' || tabSummary=' + world.el('tabSummary').textContent
      + ' || enrichSummary=' + world.el('enrichSummary').textContent + ' || rows=' + world.rowsText().join('\n'));
  };
  return world;
}

(async () => {
  // ============ 1. 共用的纯逻辑模块必须与 chrome 版逐字节相同 ============
  for (const rel of ['tabs-common.js', 'lib/tabrows.js', 'lib/enrich.js', 'lib/model-status.js', 'lib/tab-index.js']) {
    const a = read('chrome-extension/' + rel), b = read('firefox-extension/' + rel);
    assert.equal(b.length, a.length, rel + ' 字节数不同');
    assert.equal(Buffer.compare(a, b), 0, rel + ' 与 chrome 版不是逐字节相同（两份分叉就是下一个 bug 的来源）');
  }
  ok('firefox-extension 的 tabs-common.js / lib/tabrows.js / lib/enrich.js / lib/model-status.js / lib/tab-index.js 与 chrome 版逐字节相同');
  // Firefox 侧不该把归档链路那套（civitai-api/pageFetch/util）拖进来：能力集不同，拖进来就是假功能
  same(fs.readdirSync(path.join(root, 'firefox-extension', 'lib')).sort(), ['enrich.js', 'model-status.js', 'tab-index.js', 'tabrows.js'],
    'firefox-extension/lib 只该有这四个共用模块');
  for (const f of ['content', 'offscreen', 'sidepanel']) {
    assert(!fs.existsSync(path.join(root, 'firefox-extension', f)), 'Firefox 侧不该有 ' + f + ' 目录');
  }
  ok('Firefox 侧没有 content/offscreen/sidepanel：归档与页面通道确实不存在，不是藏着没接');

  // ============ 2. 权限面 + 版本一致性 ============
  const manifest = JSON.parse(read('firefox-extension/manifest.json').toString('utf8'));
  const blob = JSON.stringify(manifest);
  assert(!manifest.content_scripts || manifest.content_scripts.length === 0, '不得注入网页');
  assert(!/<all_urls>/.test(blob), '不得申请 <all_urls>');
  for (const perm of ['offscreen', 'downloads', 'scripting', 'tabs', 'clipboardRead', 'nativeMessaging', 'bookmarks', 'history']) {
    if (perm === 'tabs') continue;
    assert(!manifest.permissions.includes(perm), '不得申请 ' + perm);
  }
  assert(manifest.permissions.includes('storage'), '需要 storage（提交记录 / 任务快照 / 补全缓存）');
  for (const host of manifest.host_permissions) {
    assert(/^https:\/\/(www\.)?civitai\.(com|red)\/\*$/.test(host) || /^http:\/\/(127\.0\.0\.1|localhost)\/\*$/.test(host),
      'host_permissions 只含 C 站两站(含 www) 与本机桥：' + host);
  }
  ok('manifest：不注入网页、无 <all_urls>/offscreen/downloads/scripting，host 只有 C 站两站与本机桥');
  const readme = read('firefox-extension/README.md').toString('utf8');
  const firstLine = readme.split('\n')[0];
  assert(firstLine.includes(manifest.version), 'README 标题版本号必须与 manifest 一致（当前 ' + firstLine + ' / ' + manifest.version + '）');
  assert(/^\d+\.\d+\.\d+$/.test(manifest.version), '版本号是三段式：' + manifest.version);
  assert(firstLine.includes('Firefox'), 'README 标题要点明这是 Firefox 版：' + firstLine);
  // 差异清单是这次交付的核心：缺了就等于让用户以为 Firefox 也能归档
  assert(readme.includes('与 Chrome / Edge 版的能力差异'), 'README 必须有"与 Chrome 版能力差异"一节');
  for (const kw of ['一键归档', '评论', 'offscreen', '不注入网页', 'downloads', 'sidePanel']) {
    assert(readme.includes(kw), 'README 的差异/隐私一节要提到「' + kw + '」');
  }
  assert(readme.includes('不注入网页') && readme.includes('没有 `content_scripts`'), 'README 要如实写 Firefox 侧不注入网页');
  assert(/评论[^.\n]*(Chrome|已登录)/.test(readme), 'README 要写明评论/详情图归档只在 Chrome 侧、且需要已登录的 C 站标签页');
  ok('README ' + manifest.version + '：版本与 manifest 一致，能力差异与隐私面按 Firefox 真实行为写');

  // 入口文件里不许留"点了没反应"的归档入口，也不许把状态显示成"已归档"。
  // 注释里出现 offscreen/content_scripts 是在解释"为什么没有"，所以要先把注释剥掉再看代码。
  const popupJs = read('firefox-extension/popup.js').toString('utf8');
  const popupHtml = read('firefox-extension/popup.html').toString('utf8');
  const bgJs = read('firefox-extension/background.js').toString('utf8');
  const stripComments = (src) => src
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');
  for (const [name, src] of [['popup.js', popupJs], ['popup.html', popupHtml], ['background.js', bgJs]]) {
    assert(!/btnArchive|archiveBatch|startArchive|getArchiveConfig|offscreen|downloads\.|sidePanel|content_scripts|pageFetch/i
      .test(stripComments(src)), name + ' 的代码里不该出现依赖归档/页面通道的入口（Firefox 做不到，出现就是假功能）');
  }
  assert(!popupJs.includes('已归档'), 'popup.js 不许把任何状态显示成"已归档"');
  assert(bgJs.includes("const HISTORY_KEY = 'cftPageHistoryV1'") && bgJs.includes('rememberVisit('),
    'Firefox 后台也要记录页面历史（弹层「看过但未下载」的数据源）');
  assert(!/notifications\./.test(stripComments(bgJs)), 'Firefox 版没有 notifications 权限，代码里也不该调用');
  assert(/id="capNote"/.test(popupHtml) && /id="capNoteBatch"/.test(popupHtml) && popupHtml.includes('Chrome'),
    'popup.html 要明说归档只在 Chrome/Edge 版有');
  ok('弹层没有假归档入口：入口不存在、文案直说本浏览器不支持并指向 Chrome/Edge');
  // "接了一半"最常见的形态是控件没上：popup.js 里 $('id') 取到的东西必须都在 popup.html 里存在
  const htmlIds = new Set([...popupHtml.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
  const jsIds = [...new Set([...popupJs.matchAll(/\$\('([^']+)'\)/g)].map((m) => m[1]))];
  same(jsIds.filter((i) => !htmlIds.has(i)), [], 'popup.js 引用的控件都在 popup.html 里（共 ' + jsIds.length + ' 个）');

  // ============ 3. 弹层跑通：筛选 / 汇总 / 富化徽章 ============
  same(popupWorld().scriptSrcs, ['tabs-common.js', 'lib/tabrows.js', 'lib/enrich.js', 'lib/model-status.js', 'popup.js'],
    'popup.html 的加载顺序必须是 tabs-common → lib/tabrows → lib/enrich → lib/model-status → popup');

  const w = popupWorld({ apiStatus: { 101: 401, 303: 429 } });   // 101 匿名被拒、303 限流、202/404 正常
  w.document.fire('DOMContentLoaded');
  await w.wait(() => w.el('tabList').children.length === 4, '列表出现 4 行模型页');
  await w.wait(() => w.rowsText().join('').includes('Beta Checkpoint'), '富化结果回来');
  const sum = w.el('tabSummary').textContent;
  assert(sum.includes('共 9 个标签页'), sum);
  assert(sum.includes('可提交模型页 5'), sum);
  assert(sum.includes('重复合并 1'), sum);
  assert(sum.includes('非 C 站已忽略 1'), sum);
  assert(sum.includes('未纳入提交'), sum);
  assert(sum.includes('当前筛选显示 4 行'), sum);
  ok('筛选是看得见的：9 个标签页里 5 个模型页并成 4 行，图片页/CDN/列表页/站外各自计数（与 Chrome 版同一份 CftRows.summaryText）');

  const texts = w.rowsText();
  assert.equal(texts.length, 4, 'github/图片页/CDN/列表页都不该进列表：' + texts.join('\n'));
  assert(texts.some((t) => t.includes('需登录态')), texts.join('\n'));
  assert(texts.some((t) => t.includes('限流退避')), texts.join('\n'));
  const alpha = texts.find((t) => t.includes('Alpha'));
  assert(alpha.includes('需要登录态'), '行内要直接说出真因，不能只给徽章：' + alpha);
  // 徽章词表必须只有"这轮没取到数据的具体原因"，不许出现把三种失败塌成一个的"没有数据"
  const VOCAB = ['待补全', '限流退避', '需登录态', '本站不存在', '取数失败', '旧缓存数据', '未取数'];
  const badges = w.el('tabList').find((x) => x._classes.has('row-state')).map((x) => x.textContent);
  assert(badges.includes('需登录态') && badges.includes('限流退避'), '徽章：' + badges.join(','));
  assert(badges.every((b) => VOCAB.includes(b)), '徽章只能来自主徽章词表：' + badges.join(','));
  const en = w.el('enrichSummary').textContent;
  assert(en.includes('信息补全'), en);
  // 图片页那条也算一个取数单元（它也在 enrich 的 items 里），所以只钉"每一类都报了出来"
  assert(/需登录态 \d+/.test(en) && en.includes('被限流 1'), en);
  assert.equal(w.el('rateNote').hidden, false, '限流退避要单独说明本轮不再打站点');
  assert(w.el('rateNote').textContent.includes('退避'), w.el('rateNote').textContent);
  ok('徽章与降级真因在 Firefox 弹层里同样可见：需登录态 / 限流退避 / 退避说明各说各话');

  // 缩略图与富化字段
  const thumbs = w.el('tabList').find((x) => x.tagName === 'IMG' && x._classes.has('thumb'));
  assert(thumbs.length >= 1, '富化回来的缩略图要画进列表');
  assert(thumbs.every((t) => /^https:\/\/image\.civitai\.com/.test(t.src)), '缩略图只来自 C 站 CDN：' + thumbs.map((t) => t.src).join(','));
  const beta = texts.find((t) => t.includes('Beta Checkpoint'));
  assert(beta.includes('5859.4 MB') && beta.includes('SD 1.5'), '大小/底模来自按各自站点取到的公开接口：' + beta);
  const typeOpts = w.el('fType').children.map((c) => c.value).filter(Boolean);
  assert(typeOpts.includes('CheckPoint') && typeOpts.includes('Lora'), '筛选项从富化结果里长出来：' + typeOpts.join(','));
  const baseOpts = w.el('fBase').children.map((c) => c.value).filter(Boolean);
  assert(baseOpts.includes('SD 1.5'), '底模筛选项同理：' + baseOpts.join(','));
  ok('富化字段进了界面：类型/底模/大小可见，缩略图只走 image.civitai.com，类型与底模筛选项自动生成');

  // 筛选与排序（CftRows 那两条纯函数在 Firefox 里同样生效）。
  // 注意 applyFilters 的语义是"缺字段不排斥"（信息还没回来时不该把列表筛空），
  // 所以按类型筛 CheckPoint 会留下没有 meta 的两行，只把明确是别的类型的 Delta 筛掉。
  w.el('fType').value = 'CheckPoint'; w.el('fType').dispatch('change');
  await w.wait(() => w.el('tabList').children.length === 3, '按类型筛掉明确不同类型的行');
  assert(w.rowsText().some((t) => t.includes('Beta Checkpoint')), w.rowsText().join('\n'));
  assert(!w.rowsText().some((t) => t.includes('Delta')), '类型不符的行必须被筛掉：' + w.rowsText().join('\n'));
  w.el('fType').value = ''; w.el('fType').dispatch('change');
  await w.wait(() => w.el('tabList').children.length === 4, '清空类型筛选恢复 4 行');
  w.el('fState').value = 'needs-login'; w.el('fState').dispatch('change');
  await w.wait(() => w.el('tabList').children.length === 1, '按补全状态筛选只剩 1 行');
  assert(w.rowsText()[0].includes('Alpha'), w.rowsText()[0]);
  w.el('fState').value = 'ready'; w.el('fState').dispatch('change');
  await w.wait(() => w.el('tabList').children.length === 2, '「已补全」筛选命中 2 行');
  w.el('fState').value = ''; w.el('fState').dispatch('change');
  w.el('sort').value = 'size'; w.el('sort').dispatch('change');
  await w.wait(() => w.rowsText()[0].includes('Beta Checkpoint'), '按文件大小排序');
  w.el('sort').value = 'tab'; w.el('sort').dispatch('change');
  w.el('search').value = 'gamma'; w.el('search').dispatch('input');
  await w.wait(() => w.el('tabList').children.length === 1, '搜索按标题命中');
  w.el('search').value = ''; w.el('search').dispatch('input');
  await w.wait(() => w.el('tabList').children.length === 4, '清空搜索恢复 4 行');
  ok('筛选/排序在 Firefox 弹层生效：类型、补全状态、按大小排序、搜索都能收敛列表（同一份 CftRows.applyFilters/sortRows）');

  // 联网请求面：只打 C 站公开接口，不带 Authorization/API Key，不打桥外的端口
  const hosts = [...new Set(w.netLog.map((n) => new URL(n.url).host))].sort();
  same(hosts.filter((h) => h !== '127.0.0.1:47531'), ['civitai.com', 'civitai.red'], '富化只请求标签页自己的那两个站点');
  for (const n of w.netLog) {
    if (n.url.indexOf('127.0.0.1') >= 0) continue;
    assert(!/authorization|x-civitai-api-token|api[-_]?key/i.test(JSON.stringify(n.headers)), '请求头不该带凭据：' + JSON.stringify(n.headers));
    assert(!/token=|key=/i.test(n.url), '请求 URL 里不带 token/api key：' + n.url);
  }
  assert(w.runtimeCalls.every((m) => !m.__cft), '不该有归档消息发往 background');
  ok('隐私面实测：富化只连 civitai.com / civitai.red 与本机桥，请求头没有凭据，没有一条归档消息');

  // ============ 3.5 下载状态：徽章 / 筛选 / 只选未下载 / 下载新版 / 离线缓存 ============
  // 状态机与文案全部来自共用的 lib/model-status.js；这里验证 Firefox 弹层真的把它接到了行、筛选和按钮上。
  const sw = popupWorld({ modelStatus: {
    '1001': { state: 'downloaded', matched: 'version', version_matched: true,
      local: { source: 'library', path: 'D:\\models\\alpha.safetensors',
        version_id: 1001, version_name: 'v1', model_name: 'Alpha LoRA', exists: true } },
    '2002': { state: 'update_available', matched: 'version', version_matched: true,
      local: { version_id: 2002, version_name: 'v1', model_name: 'Beta Checkpoint', exists: true },
      update: { has_update: true, behind: 1, latest_version: 2099, latest_name: 'v2',
        latest_url: 'https://civitai.com/models/202?modelVersionId=2099' } },
    '4004': { state: 'not_downloaded', history: { file_exists: false } },
    '1303': { state: 'unknown', reason: '模型库还没扫描到这个版本' },
  } });
  sw.document.fire('DOMContentLoaded');
  await sw.wait(() => sw.el('tabList').children.length === 4, '状态世界里列表出现 4 行');
  const dlLine = () => sw.el('dlSummary').textContent;
  await sw.wait(() => /已下载 1/.test(dlLine()) && /有更新 1/.test(dlLine()), '状态汇总回来：' + dlLine());
  assert(dlLine().includes('未下载 1') && dlLine().includes('未知 1'), dlLine());
  const dlRows = sw.rowsText();
  assert(dlRows.find((t) => t.includes('Alpha')).includes('✓ 已下载'), dlRows.join('\n'));
  assert(dlRows.find((t) => t.includes('Beta')).includes('↑ 有更新'), dlRows.join('\n'));
  assert(dlRows.find((t) => t.includes('Delta')).includes('↓ 未下载'), dlRows.join('\n'));
  assert(dlRows.find((t) => t.includes('Gamma')).includes('? 未知'), dlRows.join('\n'));
  const betaRow = dlRows.find((t) => t.includes('Beta'));
  assert(betaRow.includes('本地 v1') && betaRow.includes('最新 v2'), '行内要说清本地与最新版本：' + betaRow);
  assert(sw.statusLog.length >= 1 && sw.statusLog.every((r) => Array.isArray(r.items) && r.include_updates === true),
    '状态查询走本机桥 /api/model/status，且带 include_updates：' + JSON.stringify(sw.statusLog));
  assert(sw.stored.cftModelStatusV1 && sw.stored.cftModelStatusV1.entries['version:1001'],
    '状态结果要落扩展缓存，关掉小窗再开还能先显示');
  ok('下载状态进了 Firefox 弹层：已下载/有更新/未下载/未知各有徽章与汇总，本地与最新版本可见，结果落缓存');

  sw.el('fDl').value = 'downloaded'; sw.el('fDl').dispatch('change');
  await sw.wait(() => sw.el('tabList').children.length === 1 && sw.rowsText()[0].includes('Alpha'), '按「已下载」筛选');
  sw.el('fDl').value = 'update'; sw.el('fDl').dispatch('change');
  await sw.wait(() => sw.el('tabList').children.length === 1 && sw.rowsText()[0].includes('Beta'), '按「有更新」筛选');
  sw.el('fDl').value = ''; sw.el('fDl').dispatch('change');
  await sw.wait(() => sw.el('tabList').children.length === 4, '清空下载状态筛选');
  await sw.el('selectUndownloaded').onclick();
  assert.equal(sw.el('selectionCount').textContent, '已选 1 / 4', '只勾选确认未下载的页面：' + sw.el('selectionCount').textContent);
  assert(sw.rowsText()[sw.rowInputs().findIndex((i) => i.checked)].includes('Delta'), '被选中的应只有未下载的 Delta');
  ok('「按下载状态筛选」与「只选未下载」在 Firefox 弹层生效（状态未知的不会被误选）');

  // 选中 Beta（有更新）→「下载选中项的新版」发的是最新版 URL，而不是页面里的旧版本
  for (let i = 0; i < 4; i++) {
    const input = sw.rowInputs()[i];
    const want = sw.rowsText()[i].includes('Beta');
    if (input.checked !== want) { input.checked = want; input.dispatch('change'); }
  }
  await sw.wait(() => !sw.el('updateBar').hidden, '有更新的选中项要显示「下载选中项的新版」');
  assert(sw.el('btnSendUpdates').textContent.includes('（1）'), sw.el('btnSendUpdates').textContent);
  await sw.el('btnSendUpdates').onclick();
  await sw.wait(() => sw.sent.length === 1, '新版下载请求发出');
  same(sw.sent[0], ['https://civitai.com/models/202?modelVersionId=2099'], '下载新版必须发 update.latest_url，不是页面里的旧版本');
  await sw.wait(() => (sw.stored.cftExtTasks && sw.stored.cftExtTasks.items || []).some((it) => it.modelId === '202' && it.versionId === '2099'),
    '提交跟踪落 storage');
  const updTask = sw.stored.cftExtTasks.items.find((it) => it.modelId === '202');
  assert(updTask && updTask.versionId === '2099', '提交跟踪要记下最新版本号：' + JSON.stringify(updTask));
  ok('「下载选中项的新版」只发最新版 URL，提交跟踪记录的是新版本号（普通发送不会静默换版本）');

  // 离线重开：缓存里的状态必须原样显示，并明确标「CFT 未连接」，绝不能塌成未知/未下载
  const off = popupWorld({ stored: sw.stored, bridgeOnline: false });
  off.document.fire('DOMContentLoaded');
  await off.wait(() => off.el('tabList').children.length === 4, '离线世界里列表仍出现 4 行');
  await off.wait(() => off.el('dlSummary').textContent.includes('CFT 未连接'), '离线汇总要说明是缓存：' + off.el('dlSummary').textContent);
  const offRows = off.rowsText();
  assert(offRows.find((t) => t.includes('Alpha')).includes('✓ 已下载'), '离线时缓存状态必须保留：' + offRows.join('\n'));
  assert(offRows.find((t) => t.includes('Beta')).includes('↑ 有更新'), '离线时更新状态也要保留：' + offRows.join('\n'));
  assert(off.el('dlSummary').textContent.includes('上次缓存'), off.el('dlSummary').textContent);
  off.document.hidden = true; off.document.fire('visibilitychange');
  sw.document.hidden = true; sw.document.fire('visibilitychange');
  ok('CFT 离线重开：弹层显示上次缓存（已下载/有更新），明确标「未连接 · 上次缓存」，不把没查到说成未下载');

  // ============ 3.6 阶段二：页面历史 / 检查更新 / 打开目录 ============
  const ph = popupWorld({
    stored: {
      cftPageHistoryV1: { version: 1, savedAt: Date.now(), items: {
        'version:4004': { key: 'version:4004', modelId: '404', versionId: '4004',
          firstAt: Date.now() - 5000, lastAt: Date.now() - 1000, count: 2,
          url: 'https://civitai.com/models/404?modelVersionId=4004', title: 'Delta seen' },
        'version:1001': { key: 'version:1001', modelId: '101', versionId: '1001',
          firstAt: Date.now() - 5000, lastAt: Date.now() - 500, count: 1,
          url: 'https://civitai.red/models/101?modelVersionId=1001', title: 'Alpha seen' },
      } },
      cftExtTasks: { at: Date.now(), items: [{ key: 'version:1001', url: TABS[0].url, modelId: '101',
        versionId: '1001', name: 'Alpha LoRA', sent: true, checkedAt: 0,
        cft: { phase: 'done', terminal: true, text: '已完成 1',
          filename: 'Alpha LoRA_v1.safetensors', dest_dir: 'D:\\models' } }] },
    },
    modelStatus: {
      '1001': { state: 'downloaded', matched: 'version', version_matched: true,
        local: { source: 'library', path: 'D:\\models\\alpha.safetensors',
          version_id: 1001, version_name: 'v1', model_name: 'Alpha LoRA', exists: true } },
      '2002': { state: 'not_downloaded' },
      '4004': { state: 'not_downloaded' },
      '1303': { state: 'unknown', reason: '模型库还没扫描到这个版本' },
    },
  });
  ph.document.fire('DOMContentLoaded');
  await ph.wait(() => ph.el('tabList').children.length === 4
    && /已下载 1/.test(ph.el('dlSummary').textContent), '阶段二世界状态加载');
  await ph.wait(() => !ph.el('pageHistoryPanel').hidden
    && ph.el('pageHistoryList').children.length === 1, '页面历史只剩未下载的一条');
  const histText = ph.el('pageHistoryList').textContent;
  assert(histText.includes('Delta seen') && !histText.includes('Alpha seen'),
    '已下载的页面不该留在「看过但未下载」：' + histText);
  await ph.el('clearPageHistory').onclick();
  same(ph.stored.cftPageHistoryV1.items, {}, '清除页面历史只清扩展记录');
  assert.equal(ph.el('pageHistoryPanel').hidden, true);
  ok('Firefox 页面历史：只列看过未下载、已下载被过滤、一键清除');

  await ph.wait(() => !ph.el('btnCheckUpdates').disabled, '检查更新按钮可用');
  await ph.el('btnCheckUpdates').onclick();
  same(ph.rpc.check, [false, ['D:\\models\\alpha.safetensors']], '只把模型库命中的本地路径交给 mm_check_updates');
  assert(/检查完成/.test(ph.el('feedback').textContent), ph.el('feedback').textContent);
  ok('Firefox 一键检查更新：直连桥 mm_check_updates + 轮询进度，完成后刷新状态');

  await ph.wait(() => ph.el('taskList').find((c) => c.tagName === 'BUTTON').length === 1,
    '任务行出现「打开目录」按钮');
  ph.el('taskList').find((c) => c.tagName === 'BUTTON')[0].dispatch('click');
  await ph.wait(() => ph.revealLog.length === 1, '打开目录请求应发到 /api/reveal');
  same(ph.revealLog[0], { path: 'D:\\models' }, '只发桌面登记的 dest_dir');
  await ph.wait(() => ph.el('feedback').textContent.includes('已在文件管理器中打开'), '打开目录的成功文案');
  ph.document.hidden = true; ph.document.fire('visibilitychange');
  ok('Firefox 任务行「打开目录」：直连 /api/reveal，路径来自 CFT 任务落点');

  // ============ 4. 任务区：精确发送 + CFT 侧状态 + 跨窗口恢复 ============
  // speed/downloaded/total 一律字节口径（与 downloader.DownloadTask 一致）
  w.rpc.tasks = [
    { id: 't1', url: 'https://civitai.red/models/101', filename: 'Alpha LoRA_v1.safetensors', status: 'downloading', progress: 42.4,
      downloaded: 63000000, total: 148000000, speed: 3355443, error: '', dest_dir: 'D:\\Civitai\\loras' },
    { id: 't2', url: 'https://civitai.com/models/202', filename: 'Beta Checkpoint_v1.safetensors', status: 'done', progress: 100,
      downloaded: 6000000000, total: 6000000000, speed: 0, error: '', dest_dir: 'D:\\Civitai\\checkpoints' },
  ];
  const gammaInput = w.rowInputs()[w.rowsText().findIndex((t) => t.includes('Gamma'))];
  gammaInput.checked = false; gammaInput.dispatch('change');
  const batch = w.el('sendBatch');
  assert.equal(batch.disabled, false, '有勾选项且在线时发送按钮该可用（title=' + batch.title + '）');
  assert(batch.textContent.includes('发送选中 3 项'), batch.textContent);
  await batch.onclick();
  await w.wait(() => !w.el('taskPanel').hidden && w.el('taskList').children.length === 3, '任务区出现 3 条跟踪');
  same(w.sent, [['https://civitai.red/models/101/a?modelVersionId=1001', 'https://civitai.com/models/202?modelVersionId=2002',
    'https://civitai.com/models/404?modelVersionId=4004']], '发送集合 = 勾选集合，token/追踪参数被剥掉，未勾选的 Gamma 一条没发');
  await w.wait(() => {
    const t = w.el('taskList').children.map((c) => c.textContent);
    return t.some((x) => x.includes('下载中 42')) && t.some((x) => x.includes('已完成'));
  }, '任务区拿到 CFT 的下载状态');
  const taskTexts = w.el('taskList').children.map((c) => c.textContent);
  const tA = taskTexts.find((t) => t.includes('Alpha'));
  assert(tA.includes('3.2 MB/s') && tA.includes('D:\\Civitai\\loras'), '字节口径的速度与落盘目录：' + tA);
  const tB = taskTexts.find((t) => t.includes('Beta'));
  assert(tB.includes('已完成') && tB.includes('checkpoints'), tB);
  const tD = taskTexts.find((t) => t.includes('Delta'));
  assert(tD.includes('队列里还没有这个模型的任务'), 'CFT 还没入队时要说"还没查到"，不能塌成"已完成"：' + tD);
  const ts = w.el('taskSummary').textContent;
  assert(ts.includes('已完成 1') && ts.includes('下载中 1') && ts.includes('等待解析 1'), ts);
  // 任务区的解释句是静态文案（DOM shim 不解析 HTML 正文），所以直接钉 popup.html 的源码
  assert(/「已提交」只代表扩展把链接交给了 CFT/.test(popupHtml) && /只有显示「已完成」才算下到本地/.test(popupHtml),
    '任务区必须自带"已提交 ≠ 已下载完成"的说明');
  assert(!taskTexts.some((t) => /已归档/.test(t)), '任务区不许出现"已归档"这种本浏览器做不到的状态');
  await w.wait(() => Array.isArray(w.stored.cftExtTasks?.items) && w.stored.cftExtTasks.items.length === 3, '任务快照写进 storage');
  same(w.stored.cftExtTasks.items.map((i) => i.url), w.sent[0], '快照记的就是这次提交的那几条 URL');
  ok('任务区接通：读 /api/rpc 的 get_tasks/get_parse_state，显示扩展已提交与 CFT 实际进度，快照落 storage');

  // 关掉小窗再开：从 storage 恢复；CFT 没开着时如实说明而不是清空
  const again = popupWorld({ stored: w.stored, bridgeOnline: false });
  again.document.fire('DOMContentLoaded');
  await again.wait(() => !again.el('taskPanel').hidden && again.el('taskList').children.length === 3, '重开小窗任务区从 storage 恢复');
  const restored = again.el('taskList').children.map((c) => c.textContent);
  assert(restored.some((t) => t.includes('下载中 42') && t.includes('D:\\Civitai\\loras')), restored.join('\n'));
  const rsum = again.el('taskSummary').textContent;
  assert(rsum.includes('未连接 CFT') && rsum.includes('不是"没有任务"'), rsum);
  assert.equal(again.el('sendBatch').disabled, true, '桥没开着时发送按钮保持禁用，不假装能发');
  assert(again.el('sendHint').textContent.includes('未连接'), again.el('sendHint').textContent);
  again.document.hidden = true; again.document.fire('visibilitychange');   // 停表，别让定时器挂在测试进程上
  w.document.hidden = true; w.document.fire('visibilitychange');
  ok('提交后关闭再开仍能看到跟踪状态（进度/落盘/失败原因）；CFT 离线时如实说明且发送按钮保持禁用');

  // ============ 5. 宿主 storage 不返 Promise：缓存层退化必须如实说明 ============
  const cb = popupWorld({ callbackStyle: true });
  cb.document.fire('DOMContentLoaded');
  await cb.wait(() => cb.el('tabList').children.length === 4, 'callback 风格 storage 下列表照常 4 行');
  await cb.wait(() => cb.el('enrichSummary').textContent.includes('缓存'), cb.el('enrichSummary').textContent);
  assert(cb.el('enrichSummary').textContent.includes('没生效') || cb.el('enrichSummary').textContent.includes('没能写进'),
    cb.el('enrichSummary').textContent);
  assert(!cb.rowsText().some((t) => /已归档/.test(t)), cb.rowsText().join('\n'));
  // 这条说明不是"永远亮着的假警报"：另一边的正常世界（promise 风格 storage）里缓存键确实写进去了
  assert.equal(cb.stored[cb.ctx.CFT.enrich.STORE_KEY], undefined, '这类宿主下 lib 的缓存键根本没落进 storage');
  assert.notEqual(w.stored[w.ctx.CFT.enrich.STORE_KEY], undefined, 'promise 风格宿主下同一份 lib 会正常写缓存');
  assert(!w.el('enrichSummary').textContent.includes('没生效'), '缓存正常工作时不许亮这条说明');
  ok('chrome.* 不返 Promise 的宿主里，富化缓存退化成"每次重新联网"，界面直说而不是假装缓存生效（列表本身照常）');

  // ============ 6. 依赖缺失兜底：tabrows / enrich 没加载成功也要能用 ============
  const fb = popupWorld({ skipLibs: true });
  fb.document.fire('DOMContentLoaded');
  await fb.wait(() => fb.el('tabList').children.length === 4, '退回 CftTabs.candidates 后仍列出 4 行');
  assert(fb.el('tabSummary').textContent.includes('4 个不同模型 / 版本'), fb.el('tabSummary').textContent);
  assert(fb.el('enrichSummary').textContent.includes('信息补全模块未加载'), fb.el('enrichSummary').textContent);
  assert.equal(fb.netLog.filter((n) => n.url.indexOf('127.0.0.1') < 0).length, 0, '没有 enrich 就不该打 C 站');
  fb.el('search').value = 'beta'; fb.el('search').dispatch('input');
  await fb.wait(() => fb.el('tabList').children.length === 1, '兜底路径下搜索仍可用');
  fb.el('search').value = ''; fb.el('search').dispatch('input');
  await fb.wait(() => fb.el('tabList').children.length === 4, '清空搜索回到 4 行');
  await fb.el('sendBatch').onclick();
  same(fb.sent, [['https://civitai.red/models/101/a?modelVersionId=1001', 'https://civitai.com/models/202?modelVersionId=2002',
    'https://civitai.red/models/303', 'https://civitai.com/models/404?modelVersionId=4004']], '兜底路径下发送集合仍然正确（token 被剥）');
  assert.equal(fb.el('taskList').children.length, 4, '任务区不依赖 lib，兜底时照样能跟踪；feedback=' + fb.el('feedback').textContent);
  fb.document.hidden = true; fb.document.fire('visibilitychange');
  cb.document.hidden = true; cb.document.fire('visibilitychange');
  ok('lib 没加载成功时退回 CftTabs.candidates：列表、搜索、发送、任务区全部照常，只是没有汇总与富化');

  // ============ 7. 真 Chromium 跑 firefox-extension/popup.html ============
  // vm 里的假 DOM 不解析 HTML：标签写歪、select 没有默认 option、CSS 类挂错位置，它都发现不了。
  // 这里用真浏览器把同一份 popup.html + popup.js 跑一遍，只验证"接上了、没有脚本错误、
  // 该出现的都出现"，不发任何真实网络（C 站接口与桥由 Node 侧应答，CDN 图片用本地 1px PNG 顶掉）。
  const pwPath = process.env.PLAYWRIGHT_PATH || path.join(root, '.tools', 'node_modules', 'playwright');
  process.env.PLAYWRIGHT_BROWSERS_PATH = process.env.PLAYWRIGHT_BROWSERS_PATH || path.join(root, '.tools', 'ms-playwright');
  let chromium = null;
  try { ({ chromium } = require(pwPath)); } catch (e) { console.log('SKIP 真浏览器部分：playwright 不可用（' + pwPath + '）：' + e.message); }
  if (chromium) {
    const { server, port } = await serveDir('firefox-extension');
    const browser = await chromium.launch({ headless: true });
    try {
      const context = await browser.newContext();
      const store = new Map();
      await context.exposeBinding('__cftStoreGet', (s, k) => (store.has(k) ? store.get(k) : undefined));
      await context.exposeBinding('__cftStoreSet', (s, k, v) => { store.set(k, v); return true; });
      await context.route('**://image.civitai.com/**', (route) => route.fulfill({
        status: 200, contentType: 'image/png',
        body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==', 'base64'),
      }));
      await context.addInitScript(() => {
        const st = {
          async get(k) { const v = await window.__cftStoreGet(k); return v === undefined ? {} : { [k]: v }; },
          async set(o) { for (const key of Object.keys(o)) await window.__cftStoreSet(key, o[key]); return true; },
          async remove(k) { await window.__cftStoreSet(k, undefined); return true; },
        };
        window.sent = [];
        window.__job = null;
        window.chrome = {
          storage: { local: st, session: { local: st } },
          runtime: {
            id: 'test-id', getURL: (p) => 'chrome-extension://test-id/' + p,
            sendMessage: async (m) => {
              if (m && m.__cft) throw new Error('Firefox 版不该发归档消息');
              if (m.type === 'cft-batch-start') {
                window.sent.push(m.urls.slice());
                window.__job = { id: 'job-1', running: false, completedKeys: [],
                  items: m.urls.map((u) => Object.assign({}, CftTabs.modelLink(u), { status: 'sent', msg: '已接受' })) };
              }
              if (m.type === 'cft-batch-clear') window.__job = null;
              return { ok: true, job: window.__job };
            },
          },
          tabs: { query: async (q) => {
            if (q && q.active) return [{ id: 11, windowId: 1, title: 'Alpha | Civitai', url: window.__tabs[0].url }];
            return (q && q.currentWindow) ? window.__tabs.filter((t) => t.windowId === 1) : window.__tabs;
          }, onRemoved: { addListener: () => {} } },
          action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {}, setTitle: async () => {} },
        };
        window.__tabs = [
          { id: 11, windowId: 1, title: 'Alpha | Civitai', url: 'https://civitai.red/models/101/a?modelVersionId=1001&token=SECRET1&utm_source=chatgpt.com' },
          { id: 12, windowId: 1, title: 'Alpha 重复页', url: 'https://civitai.com/models/101/b?modelVersionId=1001' },
          { id: 13, windowId: 1, title: 'Beta | Civitai', url: 'https://civitai.com/models/202?modelVersionId=2002&fbclid=abc' },
          { id: 14, windowId: 2, title: 'Gamma | Civitai', url: 'https://civitai.red/models/303?token=SECRET2' },
          { id: 19, windowId: 2, title: 'Delta | Civitai', url: 'https://civitai.com/models/404?modelVersionId=4004' },
          { id: 15, windowId: 1, title: 'GitHub', url: 'https://github.com/x/y?token=OUTSIDE' },
          { id: 16, windowId: 1, title: 'Civitai image', url: 'https://civitai.com/images/9001' },
          { id: 17, windowId: 1, title: 'CDN', url: 'https://image.civitai.com/x/9001.png' },
          { id: 18, windowId: 1, title: '列表页', url: 'https://civitai.com/models?types=Lora' },
        ];
        const DOC = {
          101: { name: 'Alpha LoRA', modelType: 5, base: 'SDXL', sizeKB: 150000, versionId: 1001 },
          202: { name: 'Beta Checkpoint', modelType: 1, base: 'SD 1.5', sizeKB: 6000000, versionId: 2002 },
          404: { name: 'Delta Lora', modelType: 5, base: 'SDXL', sizeKB: 120000, versionId: 4004 },
        };
        // 同一个模型既可能按 /models/{mid} 取，也可能按 /model-versions/mini/{vid} 取，
        // 两种 URL 都要回同一份文档（否则版本级请求会被当成"另一个 ID"，界面上就少了字段）
        const BY_ID = {};
        for (const mid of Object.keys(DOC)) { BY_ID[mid] = DOC[mid]; BY_ID[String(DOC[mid].versionId)] = DOC[mid]; }
        window.fetch = async (u, o) => {
          const url = String(u);
          if (url.indexOf('127.0.0.1:47531') >= 0) {
            const p = new URL(url).pathname;
            if (p === '/api/health') return { ok: true, status: 200, json: async () => ({ ok: true, app: 'CivitaiFreeTool', version: '2.6.30', mode: 'window', window: true }) };
            if (p === '/api/model/status') {
              const req = JSON.parse((o && o.body) || '{}');
              const MAP = {
                1001: { state: 'downloaded', local: { version_id: 1001, version_name: 'v1', model_name: 'Alpha LoRA', exists: true } },
                2002: { state: 'update_available', local: { version_id: 2002, version_name: 'v1', model_name: 'Beta Checkpoint', exists: true },
                  update: { has_update: true, behind: 1, latest_version: 2099, latest_name: 'v2', latest_url: 'https://civitai.com/models/202?modelVersionId=2099' } },
                4004: { state: 'not_downloaded' },
              };
              return { ok: true, status: 200, json: async () => ({
                ok: true, generated_at: Math.floor(Date.now() / 1000),
                library: { ready: true, scanning: false, count: 3 },
                updates: { checked_at: Math.floor(Date.now() / 1000), count: 1 },
                items: (req.items || []).map((it) => Object.assign({ model_id: it.model_id, version_id: it.version_id },
                  MAP[String(it.version_id)] || { state: 'unknown', reason: '测试里的 Gamma 没有状态' })),
              }) };
            }
            if (p === '/api/rpc') {
              const req = JSON.parse((o && o.body) || '{}');
              if (req.method === 'get_tasks') return { ok: true, status: 200, json: async () => ({ result: window.__tasks || [] }) };
              if (req.method === 'get_parse_state') return { ok: true, status: 200, json: async () => ({ result: { running: false, items: [], finished: true } }) };
              if (req.method === 'library_check') return { ok: true, status: 200, json: async () => ({ result: { ok: true, found: false, paths: [] } }) };
              return { ok: false, status: 400, json: async () => ({ error: '测试桥不支持' + req.method }) };
            }
            return { ok: false, status: 404, json: async () => ({ ok: false }) };
          }
          const id = (/\/api\/v1\/(?:models|model-versions\/mini)\/(\d+)/.exec(url) || [])[1]
            || ((/\/api\/v1\/images\/(\d+)/.exec(url) || [])[1] || '');
          if (id === '101' || id === '9001') return { ok: false, status: 401, json: async () => null, text: async () => 'null' };
          if (id === '303') return { ok: false, status: 429, json: async () => null, text: async () => 'null' };
          const d = BY_ID[id];
          if (!d) return { ok: false, status: 404, json: async () => null, text: async () => 'null' };
          const body = { id: Number(id), name: d.name, modelType: d.modelType, creator: { username: 'maker' },
            stats: { downloadCount: 100, ratingCount: 7 }, nsfw: false, nsfwLevel: 1, tags: ['style'],
            modelVersions: [{ id: d.versionId, name: 'v1', baseModel: d.base,
              publishedAt: '2026-01-05T00:00:00Z', trainedWords: ['word'],
              images: [{ url: 'https://image.civitai.com/x/original=true/' + id + '.png', width: 1024, height: 1024, nsfwLevel: 1 }],
              files: [{ id: 1, name: d.name + '.safetensors', sizeKB: d.sizeKB, type: 'Model' }] }] };
          return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) };
        };
      });
      const page = await context.newPage();
      const errors = [], dialogs = [];
      page.on('pageerror', (e) => errors.push(e.message));
      page.on('dialog', async (d) => { dialogs.push(d.message()); await d.accept(); });
      await page.goto('http://127.0.0.1:' + port + '/popup.html');
      await page.waitForFunction(() => document.querySelectorAll('.tab-row').length === 4);
      // 富化是异步的：先读缓存（peek）也会出现「信息补全 x/y」，必须等联网这一轮真结束
      // （进度文本是「联网补全中…」，完成后的汇总才有「本次联网」/「缓存命中」等明细）
      await page.waitForFunction(() => {
        const t = document.querySelector('#enrichSummary').textContent;
        return /信息补全 \d+\/\d+/.test(t) && !t.includes('联网补全中')
          && (t.includes('本次联网') || t.includes('缓存命中'));
      });
      const sum = await page.textContent('#tabSummary');
      assert(sum.includes('共 9 个标签页') && sum.includes('可提交模型页 5') && sum.includes('非 C 站已忽略 1'), sum);
      const enLine = await page.textContent('#enrichSummary');
      assert(enLine.includes('信息补全'), enLine);
      const rowTexts = await page.$$eval('.tab-row', (els) => els.map((e) => e.textContent));
      assert(rowTexts.some((t) => t.includes('需登录态')), rowTexts.join('\n'));
      assert(rowTexts.some((t) => t.includes('限流退避')), rowTexts.join('\n'));
      assert(rowTexts.some((t) => t.includes('5859.4 MB') && t.includes('SD 1.5')), '富化字段要真渲染出来：' + rowTexts.join('\n'));
      assert.equal(await page.locator('.tab-row img.thumb').count(), 2, '两个补全成功的模型页要画出缩略图');
      const typeOpts = await page.$$eval('#fType option', (els) => els.map((e) => e.value).filter(Boolean));
      same(typeOpts, ['CheckPoint', 'Lora'], '类型筛选项由富化结果生成：' + typeOpts.join(','));
      // 下载状态（真 DOM）：徽章要画在行里、汇总要出数、筛选要真的收敛列表
      await page.waitForFunction(() => document.querySelectorAll('.tab-row .dl-state').length === 4
        && /已下载 1/.test(document.querySelector('#dlSummary').textContent));
      const dlSum = await page.textContent('#dlSummary');
      assert(dlSum.includes('有更新 1') && dlSum.includes('未下载 1') && dlSum.includes('未知 1'), dlSum);
      const badges = await page.$$eval('.tab-row .dl-state', (els) => els.map((e) => e.textContent));
      assert(badges.some((t) => t.includes('已下载')) && badges.some((t) => t.includes('有更新'))
        && badges.some((t) => t.includes('未下载')), '下载状态徽章：' + badges.join(','));
      await page.locator('#modeBatch').click();   // 下载筛选控件在批量面板里
      await page.selectOption('#fDl', 'downloaded');
      await page.waitForFunction(() => document.querySelectorAll('.tab-row').length === 1);
      assert((await page.textContent('.tab-row')).includes('Alpha'), '按「已下载」筛选只剩 Alpha');
      await page.selectOption('#fDl', '');
      await page.waitForFunction(() => document.querySelectorAll('.tab-row').length === 4);
      assert.equal(await page.locator('#updateBar').isVisible(), true, '选中「有更新」的行时应出现新版按钮');
      assert((await page.textContent('#btnSendUpdates')).includes('（1）'), await page.textContent('#btnSendUpdates'));
      await page.locator('#modeCurrent').click();
      // 「本浏览器做不到」的说明必须真的可见，而不是藏在 DOM 里（批量列表默认在"批量标签页"面板里）
      await page.locator('#modeBatch').click();
      assert(await page.locator('#batchPanel').isVisible(), '批量面板应可切到');
      assert(await page.locator('#capNoteBatch').isVisible(), '批量面板要说清批量归档只在 Chrome/Edge 有');
      assert.equal(await page.locator('#btnArchiveBatch').count(), 0, 'Firefox 弹层里不该有「批量归档」按钮');
      await page.locator('#modeCurrent').click();
      assert(await page.locator('#capNote').isVisible(), '当前页面板也要说明没有一键归档');
      assert.equal(await page.locator('#btnArchive').count(), 0, 'Firefox 弹层里不该有「一键归档」按钮');
      await page.locator('#modeBatch').click();
      await page.waitForFunction(() => document.querySelectorAll('.tab-row').length === 4);
      assert.equal(errors.length, 0, '界面脚本错误：' + errors.join('\n'));
      ok('真 Chromium：popup.html 结构/加载顺序/汇总行/徽章/缩略图/筛选项/不支持说明全部到位，0 脚本错误');

      // 发送 + 任务区（真 DOM 语义：取消勾选一行，按钮计数与发送集合都要跟着变）
      await page.evaluate(() => {
        window.__tasks = [
          { id: 't1', url: 'https://civitai.red/models/101', filename: 'Alpha LoRA_v1.safetensors', status: 'downloading', progress: 42.4,
            downloaded: 63000000, total: 148000000, speed: 3355443, error: '', dest_dir: 'D:\\Civitai\\loras' },
          { id: 't2', url: 'https://civitai.com/models/202', filename: 'Beta Checkpoint_v1.safetensors', status: 'done', progress: 100,
            downloaded: 6000000000, total: 6000000000, speed: 0, error: '', dest_dir: 'D:\\Civitai\\checkpoints' },
        ];
      });
      await page.locator('.tab-row').filter({ hasText: 'Gamma' }).locator('input').uncheck();
      assert((await page.textContent('#sendBatch')).includes('发送选中 3 项'), await page.textContent('#sendBatch'));
      await page.locator('#sendBatch').click();
      await page.waitForFunction(() => window.sent.length === 1);
      assert(dialogs.some((m) => m.includes('有更新') && m.includes('下载选中项的新版')),
        '普通发送命中「有更新」的行时要先确认并指出新版按钮：' + JSON.stringify(dialogs));
      same(await page.evaluate(() => window.sent[0]),
        ['https://civitai.red/models/101/a?modelVersionId=1001', 'https://civitai.com/models/202?modelVersionId=2002', 'https://civitai.com/models/404?modelVersionId=4004'],
        '真浏览器里发送集合 = 勾选集合，token/追踪参数被剥掉');
      await page.waitForFunction(() => !document.querySelector('#taskPanel').hidden);
      await page.waitForFunction(() => document.querySelectorAll('#taskList .task-item').length === 3);
      const tTexts = await page.$$eval('#taskList .task-item', (els) => els.map((e) => e.textContent));
      const ta = tTexts.find((t) => t.includes('Alpha'));
      assert(ta.includes('下载中 42') && ta.includes('3.2 MB/s') && ta.includes('D:\\Civitai\\loras'), ta);
      const tb = tTexts.find((t) => t.includes('Beta'));
      assert(tb.includes('已完成') && tb.includes('checkpoints'), tb);
      const tsum = await page.textContent('#taskSummary');
      assert(tsum.includes('已完成 1') && tsum.includes('下载中 1') && tsum.includes('等待解析 1'), tsum);
      await page.waitForFunction(() => !!document.querySelector('#taskList .task-item.done'));
      const snap = store.get('cftExtTasks');
      assert(snap && Array.isArray(snap.items) && snap.items.length === 3, '任务快照必须过 storage：' + JSON.stringify(snap));
      same(snap.items.map((i) => i.url), await page.evaluate(() => window.sent[0]), '快照记的就是这次提交的那几条 URL');
      const ENRICH_STORE_KEY = /STORE_KEY = '([^']+)'/.exec(read('firefox-extension/lib/enrich.js').toString('utf8'))[1];
      assert(store.has(ENRICH_STORE_KEY), '富化缓存也要落 storage（键名取自 lib：' + ENRICH_STORE_KEY + '）；实际键：' + [...store.keys()].join(','));
      assert.equal(errors.length, 0, '界面脚本错误：' + errors.join('\n'));
      ok('真 Chromium：勾选→发送→任务区显示 CFT 的下载中/已完成/等待解析，快照进 storage（与 Chrome 版同一份逻辑）');
      await page.close();
    } finally {
      await browser.close();
      await new Promise((r) => server.close(r));
    }
  }

  console.log('PASSED ' + passed);
})().catch((e) => { console.error(e); process.exitCode = 1; });
