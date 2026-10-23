/* ============================== 集成契约：CFT.enrich → popup / 工作台 ==============================
 * 【这个文件是什么】
 *   node vm 自测（不发任何真实网络请求：全程用注入的假 fetcher，且 vm 里根本没有 fetch）。
 *   跑法：node chrome-extension/lib/enrich.test.cjs
 *
 * 【调用顺序（接进 popup.js / 工作台页面时照这个来）】
 *   1. 加载顺序：tabs-common.js → lib/tabrows.js → lib/enrich.js（enrich 不依赖 tabrows，
 *      但两边共享 globalThis.CFT；popup.html 里 <script src="lib/enrich.js"> 放在 tabrows 之后）。
 *      MV3 SW 用 `import './lib/enrich.js';`（IIFE 挂 globalThis，副作用导入即可）。
 *   2. 先分类再富化：`const built = CftRows.build(chrome.tabs.query 的结果)` → built.rows（可提交模型行）
 *      与 built.others（图片页/列表页等）。rows 上有 `enriched:null` 占位，就是等这里填的。
 *   3. 首屏立即出画面：`await CFT.enrich.peek(rows.concat(others))` → 只读缓存（0 请求，network:false
 *      且不写库），拿到的 entry 直接 applyToRows 渲染；缺数据的行 state='loading'，先渲染标题也够用。
 *   4. 再联网补全：`const result = await CFT.enrich.enrich(rows.concat(others), { openTabIds })`
 *      → `CFT.enrich.applyToRows(rows, result)` → `CftRows.applyFilters/ sortRows` → 渲染。
 *      想要渐进渲染就传 `onProgress({done, planned, ok, failed})`，或在 enrich 之外分片调用。
 *      popup 打开时调一次即可（10 分钟 TTL 内再开小窗都是缓存命中，0 请求）。
 *   5. 富化之后才决定按钮与筛选项：`entry.submittable` 才是"能不能提交归档"的最终答案（tabrows 的
 *      submittable 只说明 URL 形状对，enrich 会把"这个 ID 在本站 404"改成 false）。applyToRows 会写：
 *        row.meta   ← entry.meta（类型/底模/NSFW/文件数/大小/下载量/发布时间/标签/作者/缩略图）
 *        row.state  ← entry.state（'ready'|'loading'|'needs-login'|'missing'|'rate-limited'|'error'|'skipped'，
 *                     给 CftRows.applyFilters 的 filters.states 用）
 *        row.enriched ← entry.status（细粒度态，渲染"这行为什么是灰的"时用）
 *        row.thumb / row.enrichReason / row.enrichFrom / row.enrichCk / row.enrichModelId / row.enrichVersionId
 *      ⚠ applyToRows 不改 row.key / row.url —— 提交与归档仍用 tabrows 给的那两份值。
 *   6. 标签页关闭后回收：chrome.tabs.onRemoved（或定期）调 `CFT.enrich.pruneCache({ openTabIds })`。
 *      设置里"清空扩展缓存"→ `CFT.enrich.clearCache()`。诊断 → `CFT.enrich.readCache()`。
 *
 * 【必传入参】
 *   enrich(items, options)
 *     items：数组，每项至少要有 `url`。直接吃 CftRows.build() 的 rows/others、CftTabs.candidates()
 *           的输出、或原生 chrome.tabs.Tab 都行。识别的字段：
 *             url     必填，标签页原始地址（决定站点与请求目标，见下面"站点"一节）
 *             key     选填，行唯一键（CftRows 的 key，形如 version:3389879:file:choose）。缺省时
 *                     退化成 `tab:<tabId>`，再缺省用 url 本身——它只用于结果回填，不参与取数。
 *             tabId   选填（也接受 chrome.tabs.Tab 的 id），用于 entry.tabIds / byTabId / 缓存回收
 *             windowId、title 选填，本模块不消费（过滤与标题清洗归 tabrows）
 *     options（全部选填）：
 *             network:false      只读缓存，不发请求（peek 就是这么实现的）
 *             force:true         忽略缓存重新取（配合"刷新"按钮；429 门禁仍然生效）
 *             openTabIds:[...]   本轮还开着的 tabId；传了才会淘汰"过期且标签页已全关"的条目
 *             fetcher:(url,o)=>{ok,status,json|text}  注入取数（单测/由 background 代发用）
 *             concurrency/ttlMs/maxEntries/maxBytes/imageIdsMax/timeoutMs/thumbWidth/maxTasks
 *                                覆盖默认值（默认：4 并发 / 10 分钟 TTL / 160 条 / 200KB / 20 个 id / 12s）
 *             persist:false      不写 chrome.storage.local
 *             now:number|Date    注入当前时间（测试用；缺省 Date.now()）
 *             credentials:'omit' 强制匿名直连（默认 'include'，被 CORS 挡下会自动降级匿名重试一次）
 *             onProgress({done,planned,ok,failed})  每完成一条请求回调一次
 *   绝不接受的入参：API Key、Cookie、Authorization 头。本模块从不读取设置里的 key，
 *   也不会把任何凭据拼进请求（测试里逐条断言请求头只有 Accept）。
 *
 * 【产出字段】
 *   返回值：{ entries:[entry...], byKey:{key→entry}, byCk:{cacheKey→entry}, byTabId:{tabId→entry}, summary }
 *   entry（每个取数单元一条；同一模型的多个标签页/多个 fileId 会共用一条，keys 里列出所有行键）：
 *     key/keys/ck/tabIds/url/site/kind/modelId/versionId/imageId   身份；ck 是缓存键（site|kind:id）
 *     status   'ok'|'partial'|'stale'|'needs-login'|'missing'|'rate-limited'|'error'|'skipped'|'pending'
 *     state    行状态，直接写进 row.state 供 CftRows.applyFilters 的 filters.states 用：
 *              'ready'|'loading'|'needs-login'|'missing'|'rate-limited'|'error'|'skipped'
 *     submittable  能不能提交归档（kind 是 model/version 且本站没判 404）
 *     reason   人类可读的降级真因（中文，必带"是哪种失败"），渲染成行尾灰字或 tooltip
 *     retryAt  限流/缺数据时建议重试的毫秒时间戳（配合 chrome.alarms；没有就 null）
 *     cached/from('cache'|'network'|'cache-stale')/borrowed(借了同模型的缓存文档)/staleUsed/channel
 *              ('page'|'direct'|'direct-anon'|'injected')/at
 *   entry.meta —— 就是 CftRows.applyFilters/sortRows 吃的 r.meta，字段一一对应：
 *     name          模型名（tabrows 的 q 搜索会读）
 *     type          'CheckPoint'/'Lora'/…（filters.types；数字 modelType 已按归档链路那份表映射过）
 *     baseModel     'SD 1.5'/'SDXL'/…（filters.baseModels）
 *     nsfw          true/false/null（filters.nsfw 的 'hide'/'only'；未知绝不当成 false）
 *     fileCount     版本文件数（filters.onlyMultiFile：>1 才算多文件）
 *     totalMB       版本文件总大小 MB（sortRows('size')）
 *     downloadCount 下载量（sortRows('downloads')）
 *     publishedAt   版本发布时间 ISO 串（sortRows('published')）
 *     tags[]/trainedWords[]/creator  q 搜索与展示
 *     thumb         缩略图 URL：image.civitai.com 的 original=true 已改写成 width=256（列表行别拿原图）
 *     另有 largestFileMB/files[{name,sizeMB,type}]/mediaCount/versionCount/versionIds/versionName/
 *         updatedAt/ratingCount/likedCount/collectedCount/nsfwLevel/earlyAccess/imageId/url/type
 *         kind/site/modelId/versionId/submittable/exists/needsLogin/staleUsed/anonymous/enrichedAt（身份与降级标记）
 *     图片行（/images/{id}、/posts/{id}）的 meta：type='image'|'video'、thumb、url、nsfw、nsfwLevel、
 *         creator、publishedAt、hasPrompt（站点有没有给生成参数）、linkedVersionIds[]（能定位到模型版本，
 *         工作台可据此给"跳到模型页提交"的入口）、baseModel。
 *     ⚠ meta 是"每个取数单元共享的只读对象"（同一模型的多个标签页/多个 fileId 指向同一个对象），
 *       渲染时只读不改；要改就浅拷贝。
 *
 * 【失败时的降级形状（消费方必须能画出来，别当异常抛）】
 *   enrich 不抛错。任何一行取不到数据都是：entry.meta 为 null（或 staleUsed 的旧值）+ entry.reason 说清真因。
 *     404            status:'missing'、state:'missing'、submittable:false、exists:false（entry 上；meta 通常是 null），
 *                    reason 会写明"按该标签页自己的站点请求、没拿另一站的结果冒充"
 *     匿名拿不到     status:'needs-login'、state:'needs-login'、entry.needsLogin:true、entry.exists:null
 *                    （null＝"不知道存不存在"，绝不伪装成"没有数据"）
 *     429            status:'rate-limited'、retryAt、summary.blockedUntil[site]；本轮不重试（SW 会被回收），
 *                    到点后由调用方再调一次 enrich
 *     超时/5xx/坏JSON status:'error'（有旧缓存时 meta 仍是旧值 + staleUsed:true）
 *     非详情页/站外  status:'skipped'、state:'skipped'、meta:null、submittable:false（列表页、搜索页、
 *                    个人主页、非 C 站地址：不请求、不读页面内容、不取 Cookie）
 *     只读到旧缓存   status:'stale'、state:'ready'、staleUsed:true、reason 里有旧数据的时间
 *   summaryText(CFT.enrich.summaryText(result.summary)) 可直接显示成"信息补全 8 · 需登录态 2 · 本站点不存在 1"，
 *   和 CftRows.summaryText（"共 N 个标签页 → 模型页 X …"）是互补的两行。
 * ============================================================================================== */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');       // chrome-extension/
let passed = 0;
const ok = (s) => { passed++; console.log('OK ' + s); };
const clone = (v) => JSON.parse(JSON.stringify(v));
const tick = (ms) => new Promise((r) => setTimeout(r, ms || 1));
// 跨 realm（vm 里造出来的数组/对象）不能用 assert/strict 的 deepEqual——它等价于 deepStrictEqual，
// 会因为构造函数不同而判"不同"。按值比，别按原型比。
const same = (actual, expected, msg) => assert.equal(JSON.stringify(actual) ?? 'null', JSON.stringify(expected) ?? 'null', msg);

// ---- 假 chrome：storage.local 按 key 读写；没有 window/document；故意不给 fetch ----
function fakeChrome(initial) {
  const data = initial ? clone(initial) : {};
  const writes = [];
  return {
    data,
    writes,
    storage: {
      local: {
        async get(key) {
          const out = {};
          const list = Array.isArray(key) ? key : [key];
          for (const k of list) if (typeof k === 'string' && data[k] !== undefined) out[k] = clone(data[k]);
          if (key && typeof key === 'object' && !Array.isArray(key)) {
            for (const k of Object.keys(key)) if (data[k] !== undefined) out[k] = clone(data[k]);
          }
          return out;
        },
        async set(obj) { for (const k of Object.keys(obj)) { data[k] = clone(obj[k]); writes.push(k); } },
        async remove(key) { delete data[key]; },
      },
    },
  };
}

function loadLib(deps) {
  const chrome = fakeChrome();
  const netLog = [];
  const ctx = vm.createContext({
    chrome, URL, URLSearchParams, AbortController, setTimeout, clearTimeout, console, Promise,
    // 顶层不触碰 chrome/document/window —— 这里连 window/document 都不给，加载成功即证明这一点。
    // fetch 只服务于"没注入 opts.fetcher、也没装 CFT.api"那条兜底分支（第 16 段）；
    // 其它用例都显式传假 fetcher，任何走到这里的全局 fetch 都会反映在 netLog 里，藏不住。
    fetch: async (url, o) => {
      netLog.push({ url, credentials: o && o.credentials, headers: (o && o.headers) || {} });
      return { ok: true, status: 200, text: async () => JSON.stringify(modelDoc(123)) };
    },
    structuredClone: clone, Math, Number, JSON, Date, Object, Array, String, Boolean, Error, RegExp, Set, Map, Symbol, parseInt, parseFloat, isNaN,
  });
  for (const f of deps || []) vm.runInContext(fs.readFileSync(path.join(root, f), 'utf8'), ctx, { filename: f });
  return { ctx, chrome, netLog };
}
const E = () => loadEnv.ctx.CFT.enrich;
const R = () => loadEnv.ctx.CftRows;
const T = () => loadEnv.ctx.CftTabs;
const DEPS = ['tabs-common.js', 'lib/tabrows.js', 'lib/enrich.js'];
const loadEnv = loadLib(DEPS);
const hasRows = typeof R() === 'object' && R() !== null;

// ---- 假 fetcher：站点路由表 + 调用台账（URL、请求头、并发峰值）----
function makeLog() { return { calls: [], active: 0, maxActive: 0, headersSeen: new Set() }; }
function makeFetcher(table, log) {
  return async (url, o) => {
    const u = new URL(url);
    log.calls.push(url);
    for (const k of Object.keys(o.headers || {})) log.headersSeen.add(k.toLowerCase());
    log.active += 1;
    if (log.active > log.maxActive) log.maxActive = log.active;
    await tick(2);                       // 让出事件循环，真实暴露并发窗口
    log.active -= 1;
    const hit = table[u.hostname + u.pathname] || table[u.pathname];
    if (hit === undefined) return { ok: false, status: 404, json: null, error: 'no route ' + url };
    if (hit instanceof Error) throw hit;
    // 路由值可以是：响应对象 / 生成响应的函数 / 直接给 JSON 体。统一在这里归一，测试里就不用到处写 ok:true。
    let r = (typeof hit === 'function') ? await hit(u, o) : hit;
    if (!r || typeof r !== 'object') r = { json: r };
    const status = r.status || 200;
    const good = r.ok !== undefined ? r.ok === true : (status >= 200 && status < 300);
    return { ok: good, status, json: r.json !== undefined ? r.json : (good ? r : null), error: r.error || '' };
  };
}

// ---- 站点数据样本（字段形状取自归档链路的真实消费点：modelType/modelVersions/files[].sizeKB/stats）----
const CDN = 'https://image.civitai.com/xgdotergstresis/abc';
// ver：覆盖那唯一一条版本记录（真实站点上 modelVersionId 是全局唯一主键，
// 所以测试里的版本 ID 也必须互不相同，否则会被当成同一个模型富化一次——那不是被测行为）
function modelDoc(id, over, ver) {
  const base = {
    id, name: 'Demo Model ' + id, modelType: 5, nsfw: false,
    tags: ['fantasy', 'character'], creator: { username: 'maker', name: 'Maker' },
    stats: { downloadCount: 1234, ratingCount: 56, likedCount: 78, collectedCount: 9 },
    updatedAt: '2026-01-02T03:04:05Z',
    modelVersions: [{
      id: 11, name: 'v1.0', baseModel: 'SD 1.5', trainedWords: ['ohwx', 'woman'],
      publishedAt: '2026-01-01T00:00:00Z', nsfw: false,
      files: [
        { id: 91, name: 'demoV1.safetensors', type: 'Model', sizeKB: 1048576 },        // 1024 MB
        { id: 92, name: 'demoV1.yaml', type: 'Config', sizeKB: 2048 },
      ],
      images: [{ url: CDN + '/original=true/145676249.jpeg', nsfwLevel: 0 }],
    }],
  };
  if (ver) base.modelVersions = [Object.assign({}, base.modelVersions[0], ver)];
  return Object.assign(base, over || {});
}
function miniDoc(over) {
  return Object.assign({
    id: 13, modelId: 300, name: 'v3.0', baseModel: 'SDXL', trainedWords: ['dw'],
    publishedAt: '2026-02-02T00:00:00Z',
    files: [{ id: 99, name: 'big.safetensors', type: 'Model', sizeKB: 2097152 }],
    images: [{ url: CDN + '/original=true/77.png' }],
  }, over || {});
}

(async () => {
  // ---------- 1. URL → modelId/versionId 解析 ----------
  const parse = E().parseRef;
  const a = parse('https://civitai.red/models/2989579/anima-x?modelVersionId=3389879&fileId=44');
  same([a.kind, a.modelId, a.versionId, a.site], ['version', '2989579', '3389879', 'civitai.red']);
  ok('查询参数里的 ?modelVersionId=3389879 被认成版本，且 modelId 同时已知');
  const b = parse('https://www.civitai.com/models/123/some-slug');
  same([b.kind, b.modelId, b.versionId, b.site], ['model', '123', '', 'civitai.com']);
  ok('www.civitai.com 归一到 civitai.com，无版本参数时 kind=model');
  const c = parse('https://civitai.com/api/download/models/555');
  same([c.kind, c.modelId, c.versionId], ['version', '', '555']);
  ok('/api/download/models/{n} 里的 n 按版本ID处理（与 tabs-common.modelLink 同口径，不会当成模型ID）');
  const d = parse('https://civitai.red/images/145224191');
  same([d.kind, d.imageId, d.site], ['image', '145224191', 'civitai.red']);
  assert.equal(parse('https://civitai.red/posts/998').kind, 'image');
  ok('图片页/帖子页识别为 image 单元');
  assert.equal(parse('https://civitai.red/models?type=Checkpoint'), null);
  assert.equal(parse('https://civitai.com/search?q=lora'), null);
  assert.equal(parse('https://civitai.com/user/maker'), null);
  assert.equal(parse('https://evil.example/models/1'), null);
  assert.equal(parse('https://civitai.red.evil.example/models/1'), null);
  assert.equal(parse('https://image.civitai.com/x/original=true/1.jpg'), null);
  assert.equal(parse('file:///C:/secret/models/1'), null);
  ok('列表页/搜索页/个人主页/CDN/站外/非 http 一律 null（不请求、不猜）');
  assert.equal(parse('https://civitai.com/models/1?modelVersionId=11&modelVersionId=12').versionId, '');
  ok('重复的 modelVersionId 视为歧义，退回 model 单元（和 tabs-common 一样保守）');
  same([E().ckOf(a), E().ckOf(b)], ['civitai.red|version:3389879', 'civitai.com|model:123']);
  ok('缓存键带站点：.com 与 .red 的同 ID 条目不可能互相污染');

  // ---------- 2. 缩略图 / 分块 ----------
  const thumb = E().thumbUrl(CDN + '/original=true/145676249.jpeg', 256);
  assert.equal(thumb, CDN + '/width=256/145676249.jpeg');
  assert.ok(!thumb.includes('original=true'));
  assert.equal(E().thumbUrl('https://example.invalid/a.jpg'), 'https://example.invalid/a.jpg');
  assert.ok(!E().thumbUrl('https://image.civitai.com/q/?original=true&w=1', 128).includes('original='));
  ok('列表行只拿按宽度重写的缩略图；非 image.civitai.com 的地址原样保留');
  // 注意：vm 里造出来的数组/对象与主 realm 的原型不同，assert/strict 的 deepEqual（=deepStrictEqual）
  // 会因构造函数不同而失败 —— 跨 realm 一律按值比（JSON 或逐字段）。
  assert.equal(JSON.stringify(E().chunk([1, 2, 3, 4, 5], 2).map((x) => x.length)), JSON.stringify([2, 2, 1]));
  ok('批量分块可用（图片 ids 就靠它限长）');

  // ---------- 3. 纯投影 ----------
  const doc = E().projectModelDoc(modelDoc(123), 256);
  assert.equal(doc.type, 'Lora', 'modelType 数字要按归档链路那份表映射');
  assert.equal(doc.versions[0].totalMB, 1026);
  assert.equal(doc.versions[0].thumb, CDN + '/width=256/145676249.jpeg');
  assert.equal(doc.downloadCount, 1234);
  same(doc.versionIds, ['11']);
  assert.equal(E().projectModelDoc({ modelType: 1, type: 'X' }, 256).type, 'CheckPoint');
  assert.equal(E().projectModelDoc({ type: { name: 'Checkpoint' } }, 256).type, 'Checkpoint');
  assert.equal(E().projectVersionMeta({ files: [{ size: 2147483648 }] }, 256).totalMB, 2048, 'size 大值按字节解释');
  assert.equal(E().projectVersionMeta({ files: [{ size: 2048 }] }, 256).totalMB, 2, 'size 小值按 KB 解释');
  ok('字段投影：类型映射 / sizeKB→MB / 缩略图重写 / 版本名录都稳定');

  // C 站 files[].hashes.SHA256 → meta.sha256s / files[].sha256（桌面端哈希回退的唯一来源）
  const SHA_A = 'a'.repeat(64), SHA_B = 'B'.repeat(64);
  const hp = E().projectVersionMeta({ files: [
    { id: 1, name: 'a.safetensors', sizeKB: 1024, hashes: { SHA256: SHA_A } },
    { id: 2, name: 'b.safetensors', sizeKB: 512, hashes: { sha256: SHA_B } },
    { id: 3, name: 'c.txt', sizeKB: 2, hashes: { SHA256: 'nope' } },
  ] }, 256);
  same(hp.sha256s, [SHA_A, SHA_B.toLowerCase()], 'sha256s 收全部合法哈希并小写');
  same(hp.files.map((f) => f.sha256), [SHA_A, SHA_B.toLowerCase(), ''], '逐文件保留哈希；非法值留空');
  same(E().projectVersionMeta({ files: [{ name: 'x' }] }, 256).sha256s, [], '没有哈希时是空数组');
  ok('哈希投影：files[].hashes.SHA256 保留为 sha256s/files[].sha256（供富化结果直接传给状态请求）');

  // ---------- 4. 整批富化：并发、分块、去重、tabrows 直接消费 ----------
  if (!hasRows) { console.log('!! lib/tabrows.js 不在，跳过与 tabrows 的集成断言'); }
  if (hasRows) {
    const log = makeLog();
    const table = {
      'civitai.com/api/v1/models/123': { json: modelDoc(123) },
      'civitai.com/api/v1/models/124': { json: modelDoc(124, { modelVersions: [{ id: 21, name: 'only', baseModel: 'SDXL', files: [{ id: 5, name: 'x.safetensors', sizeKB: 1024 }] }] }) },
      'civitai.com/api/v1/model-versions/mini/13': { json: miniDoc() },
      // mini 反查出 modelId=300 之后要补的那次 models/{mid}（版本 13 的文件在这里给全）
      'civitai.com/api/v1/models/300': { json: modelDoc(300, null, { id: 13, baseModel: 'SDXL', files: [{ id: 99, name: 'big.safetensors', type: 'Model', sizeKB: 2097152 }] }) },
      // 10 个不同模型（版本 ID 各不相同）：并发峰值必须被压住
      ...Object.fromEntries(Array.from({ length: 10 }, (_, i) => ['civitai.com/api/v1/models/9' + i, { json: modelDoc(90 + i, null, { id: 900 + i }) }])),
      'civitai.com/api/v1/images': (u) => {
        const ids = (u.searchParams.get('ids') || '').split(',').filter(Boolean);
        const metadata = {};
        for (const id of ids) metadata[id] = { id: Number(id), type: 'image', url: CDN + '/original=true/' + id + '.jpg', nsfwLevel: 0, modelVersionIds: [11], user: { username: 'maker' } };
        return { ok: true, status: 200, json: { metadata, items: [] } };   // 实测：带 ids 时 items 是空数组
      },
    };
    const now = Date.parse('2026-10-16T00:00:00Z');
    const tabs = [];
    for (let i = 0; i < 10; i++) tabs.push({ id: 100 + i, windowId: 1, title: 'M9' + i + ' | Civitai', url: 'https://civitai.com/models/9' + i + '/slug?modelVersionId=' + (900 + i) });
    // 同一模型同一版本的第二个标签页（不同 fileId → tabrows 会分成两行，但取数只该发生一次）
    tabs.push({ id: 110, windowId: 1, title: 'dup', url: 'https://civitai.com/models/90/slug?modelVersionId=900&fileId=77' });
    // 只有版本ID 的下载页 → 走 mini 反查
    tabs.push({ id: 111, windowId: 2, title: 'dl', url: 'https://civitai.com/api/download/models/13' });
    // 图片页 ×3 → 一条批量请求
    tabs.push({ id: 112, windowId: 2, title: 'img', url: 'https://civitai.com/images/500' });
    tabs.push({ id: 113, windowId: 2, title: 'img', url: 'https://civitai.com/images/501' });
    tabs.push({ id: 114, windowId: 2, title: 'post', url: 'https://civitai.com/posts/502' });
    // 噪音行：列表页 + 站外页
    tabs.push({ id: 115, windowId: 2, title: 'list', url: 'https://civitai.com/models?types=Checkpoint' });
    tabs.push({ id: 116, windowId: 2, title: 'off', url: 'https://example.invalid/private' });

    const built = R().build(tabs);
    assert.ok(built.rows.length > 0);
    const items = built.rows.concat(built.others);
    const res = await E().enrich(items, { fetcher: makeFetcher(table, log), now, openTabIds: tabs.map((t) => t.id) });
    assert.equal(log.maxActive, 4, '并发峰值应正好顶到上限 4，不超');
    assert.equal(res.summary.requested, 13, '10 个模型 + 1 次 mini + 1 次 models(300) + 1 次图片批量 = 13 条请求');
    ok('并发上限 4 生效；同模型多行共用一次请求；3 个图片页合成 1 条 ids= 批量请求');
    assert.ok(log.calls.some((u) => /\/api\/v1\/images\?ids=500,501,502/.test(u)));
    ok('图片走 /api/v1/images?ids= 批量，且读的是 metadata（items 为空也能取到）');
    same([...log.headersSeen].sort(), ['accept'], '绝不发 Authorization / Cookie：只允许 Accept 头');
    const dl = res.entries.find((e) => e.kind === 'version' && e.versionId === '13');
    assert.equal(dl.site, 'civitai.com');
    assert.equal(dl.meta.modelId, '300', 'mini 反查到的 modelId 要落到 meta 上');
    assert.equal(dl.meta.totalMB, 2048);
    assert.equal(dl.meta.type, 'Lora', 'mini 之后还要补上 models/300 的模型级字段');
    assert.equal(dl.status, 'ok');
    ok('只有版本ID 的行：mini(942B) 反查 modelId → 再补 models/{mid} 的模型级字段');
    const img = res.byTabId[112];
    assert.equal(img.status, 'ok');
    assert.equal(img.meta.linkedVersionIds.join(''), '11');
    assert.equal(img.meta.thumb, CDN + '/width=256/500.jpg');
    assert.equal(img.submittable, false, '图片页不是模型页，不能提交归档');
    const skipped = res.byTabId[115];
    same([skipped.status, skipped.state, skipped.meta, skipped.submittable], ['skipped', 'skipped', null, false]);
    assert.match(skipped.reason, /模型列表页/);
    // 站外页：tabrows 根本不会把它交给富化，所以单独喂一条（顺带证明站外地址零请求）
    const offLog = makeLog();
    const off = await E().enrich([{ key: 'off', tabId: 116, url: 'https://example.invalid/private' }],
      { fetcher: makeFetcher({}, offLog), now, persist: false, openTabIds: [116] });
    assert.equal(offLog.calls.length, 0, '站外地址一个请求都不发');
    assert.equal(off.entries[0].reason.indexOf('非 C 站地址'), 0);
    ok('列表页/站外页如实标 skipped，meta 为 null，不替它们编造数据');

    E().applyToRows(built.rows, res);
    E().applyToRows(built.others, res);
    assert.ok(built.rows.every((r) => r.enriched && r.state));
    const withType = built.rows.find((r) => r.meta && r.meta.type);
    assert.ok(withType, '行上要能被 tabrows 读到 type');
    const dlRow = built.others.find((r) => r.url.includes('/api/download/'));
    assert.ok(dlRow && dlRow.meta && dlRow.meta.totalMB === 2048, 'tabrows 把下载链接页归到 others，富化也要能把它填上');
    const filtered = R().applyFilters(built.rows, { types: ['Lora'] });
    assert.equal(filtered.length, built.rows.filter((r) => !r.meta || r.meta.type === 'Lora').length);
    const sized = R().sortRows(built.rows.concat(built.others), 'size');
    assert.equal(sized[0].meta.totalMB, 2048, 'sortRows(size) 直接吃 meta.totalMB');
    const multiRows = R().applyFilters(built.rows, { onlyMultiFile: true });
    assert.ok(multiRows.length >= 1 && multiRows.every((r) => r.meta && r.meta.fileCount > 1));
    const q = R().applyFilters(built.rows, { q: 'maker' });
    assert.ok(q.length >= 1, 'q 搜索要能命中 creator（tabrows 拼的是 meta.creator）');
    ok('tabrows 的 applyFilters(types/onlyMultiFile/q) 与 sortRows(size) 能直接消费 meta，无需适配层');
    const s = res.summary;
    assert.equal(R().build(tabs).summary.total, s.total + 1, 'tabrows 统计 16 个 C 站页 + 1 个站外页，富化只收到前者');
    assert.equal(s.total, 16, 'rows + others 全部进台账');
    assert.equal(s.units, 14, '重复行（同 site+version）合并成同一个取数单元');
    assert.equal(s.skipped, 1, '列表页只标不取（站外页压根不会进来）');
    assert.equal(s.ready, 14, '14 个单元全部就绪（10 模型版本 + 1 mini + 3 图片）');
    ok('summary.ready 统计的是"信息已可用"的行数，可直接渲染');

    // ---------- 5. 缓存命中 / 过期 / 陈旧兜底 ----------
    const log2 = makeLog();
    const res2 = await E().enrich(items, { fetcher: makeFetcher(table, log2), now: now + 60000, openTabIds: tabs.map((t) => t.id) });
    assert.equal(log2.calls.length, 0, 'TTL 内第二次调用应零请求');
    assert.equal(res2.summary.cached, res2.summary.units, '每个单元都应命中缓存');
    assert.ok(res2.entries.every((e) => e.cached === true || e.status === 'skipped'));
    ok('chrome.storage.local 缓存命中：重复打开小窗不再打站点（0 请求）');
    const peeked = await E().peek(items, { fetcher: makeFetcher(table, makeLog()), now: now + 60000 });
    assert.equal(peeked.summary.requested, 0);
    const peekOne = peeked.entries.find((e) => e.kind === 'version' && e.versionId === '900');
    assert.equal(peekOne.meta.type, 'Lora', 'peek() 只读缓存也要能给出类型');
    assert.equal(peekOne.cached, true);
    ok('peek() 首屏走缓存：0 请求就画出类型/大小/缩略图');

    const log3 = makeLog();
    const res3 = await E().enrich(items, { fetcher: makeFetcher(table, log3), now: now + 11 * 60 * 1000, openTabIds: tabs.map((t) => t.id) });
    assert.ok(log3.calls.length >= 12, 'TTL(10 分钟) 过期后要重新取数');
    assert.equal(res3.summary.cached, 0);
    ok('TTL 过期（>10 分钟）后自动重新联网补全');

    const log4 = makeLog();
    const dead = new Proxy({}, { get: () => () => ({ ok: false, status: 500, json: null, error: '站点 500' }) });
    const res4 = await E().enrich(items, { fetcher: makeFetcher(dead, log4), now: now + 60 * 60 * 1000, openTabIds: tabs.map((t) => t.id) });
    const errEntry = res4.entries.find((e) => e.status === 'error');
    assert.ok(errEntry, '取数失败要标 error');
    assert.equal(errEntry.staleUsed, true, '失败时要把旧缓存端出来，别把列表渲染成空白');
    assert.ok(errEntry.meta && errEntry.meta.type, '陈旧值仍在 meta 里');
    assert.match(errEntry.reason, /HTTP 500/);
    ok('整批全失败时：status=error + staleUsed + 旧 meta，界面还能显示上次的数据而不是空白');

    // ---------- 6. 单条失败不拖垮整批 ----------
    const log5 = makeLog();
    const mixed = {
      'civitai.com/api/v1/models/123': { json: modelDoc(123) },
      'civitai.com/api/v1/models/124': () => { throw new Error('这个标签页的取数炸了'); },
      'civitai.com/api/v1/model-versions/mini/13': { ok: false, status: 404, json: null },
      'civitai.com/api/v1/images': { ok: false, status: 401, json: null },
    };
    const rows5 = [
      { key: 'k1', tabId: 1, url: 'https://civitai.com/models/123/x' },
      { key: 'k2', tabId: 2, url: 'https://civitai.com/models/124/x' },
      { key: 'k3', tabId: 3, url: 'https://civitai.com/api/download/models/13' },
      { key: 'k4', tabId: 4, url: 'https://civitai.com/images/777' },
    ];
    const res5 = await E().enrich(rows5, { fetcher: makeFetcher(mixed, log5), now: now + 2 * 60 * 60 * 1000, persist: false, openTabIds: [1, 2, 3, 4] });
    const byTab = res5.byTabId;
    assert.equal(byTab[1].status, 'ok');
    assert.equal(byTab[2].status, 'error');
    assert.match(byTab[2].reason, /这个标签页的取数炸了/);
    assert.equal(byTab[3].status, 'missing');
    assert.equal(byTab[4].status, 'needs-login');
    assert.equal(res5.entries.length, 4, '四行都要有结果，一行都不许掉');
    ok('单条失败（抛错/404/401）只降级它自己那条，整批 4 条结果齐全');

    // ---------- 7. 站点选择：只打该标签页自己的站点，不跨站冒充 ----------
    const log6 = makeLog();
    const table6 = {
      // 实测：2989579 在 .com 是 404，在 .red 存在
      'civitai.com/api/v1/models/2989579': { ok: false, status: 404, json: null },
      'civitai.red/api/v1/models/2989579': { json: modelDoc(2989579, { name: '镜像上才有的模型' }) },
    };
    const rows6 = [
      { key: 'com', tabId: 1, url: 'https://civitai.com/models/2989579/slug?modelVersionId=11' },
      { key: 'red', tabId: 2, url: 'https://civitai.red/models/2989579/slug?modelVersionId=11' },
    ];
    const res6 = await E().enrich(rows6, { fetcher: makeFetcher(table6, log6), now: now + 3 * 60 * 60 * 1000, persist: false, openTabIds: [1, 2] });
    same(log6.calls.map((u) => new URL(u).hostname).sort(), ['civitai.com', 'civitai.red']);
    assert.equal(res6.byKey.com.status, 'missing');
    assert.equal(res6.byKey.com.submittable, false, '本站 404 的行不能再当"可提交"');
    assert.match(res6.byKey.com.reason, /镜像/);
    assert.equal(res6.byKey.red.status, 'ok');
    assert.equal(res6.byKey.red.meta.name, '镜像上才有的模型');
    assert.equal(res6.byKey.red.meta.exists, true);
    ok('同一个模型 ID 分别打到两个标签页自己的站点：.com 判 404 就如实 404，绝不拿 .red 的数据顶替');

    // ---------- 8. NSFW / 匿名空结果如实降级 ----------
    const log7 = makeLog();
    const table7 = {
      // 实测：匿名请求 NSFW 图 → 200 但 {"items":[],"metadata":{}}
      'civitai.com/api/v1/images': { json: { items: [], metadata: {} } },
    };
    const rows7 = [
      { key: 'i1', tabId: 1, url: 'https://civitai.com/images/145224191' },
      { key: 'i2', tabId: 2, url: 'https://civitai.com/images/145676249' },
    ];
    const res7 = await E().enrich(rows7, { fetcher: makeFetcher(table7, log7), now: now + 4 * 60 * 60 * 1000, persist: false, openTabIds: [1, 2] });
    assert.equal(log7.calls.length, 1, '两张图合成一条 ids= 请求');
    for (const e of res7.entries) {
      assert.equal(e.status, 'needs-login', '空结果必须标"需要登录态"，不能标 missing');
      assert.equal(e.state, 'needs-login');
      assert.equal(e.meta, null, '没有数据就如实留空，不伪装');
      assert.match(e.reason, /需要登录态/);
      assert.match(e.reason, /不读取也不转发/);
      assert.notEqual(e.status, 'missing');
    }
    ok('NSFW 匿名空 metadata → needs-login（不是 missing），reason 说明不读取也不转发 API Key');
    // 部分命中：一条 ids 批量里只有 SFW 那条给数据
    const log8 = makeLog();
    const table8 = {
      'civitai.com/api/v1/images': (u) => {
        const ids = (u.searchParams.get('ids') || '').split(',').filter(Boolean);
        const metadata = {};
        if (ids.indexOf('145676249') >= 0) metadata['145676249'] = { id: 145676249, type: 'image', url: CDN + '/original=true/145676249.jpg', nsfwLevel: 0 };
        return { ok: true, status: 200, json: { metadata, items: [] } };
      },
    };
    const res8 = await E().enrich(rows7, { fetcher: makeFetcher(table8, log8), now: now + 5 * 60 * 60 * 1000, persist: false, openTabIds: [1, 2] });
    assert.equal(res8.byKey.i1.status, 'needs-login');
    assert.equal(res8.byKey.i2.status, 'ok');
    assert.equal(res8.byKey.i1.meta, null, 'NSFW 匿名取不到 → meta 如实留空，不编造');
    assert.equal(res8.byKey.i1.needsLogin, true, '降级标记落在 entry 上（meta 是 null）');
    assert.equal(res8.byKey.i2.status, 'ok');
    assert.equal(res8.byKey.i2.meta.needsLogin, false);
    assert.equal(res8.byKey.i2.needsLogin, false);
    ok('同批里 SFW 拿到数据、NSFW 标需登录态：逐条降级，不整批抹平');

    // ---------- 9. 429 退避要真生效（并且扛得住"重新调用"） ----------
    const log9 = makeLog();
    const table9 = {
      '/api/v1/models/1': () => table9.__next(),
      '/api/v1/models/2': () => table9.__next(),
      __next: () => ({ ok: false, status: 429, json: null, error: '限流' }),
    };
    const env9 = loadLib(DEPS);
    const enrich9 = env9.ctx.CFT.enrich;
    // 同站的两个不同模型：两条请求并发发出、都吃到 429 → 第二轮退避必须指数放大
    const rows9 = [
      { key: 'r1', tabId: 1, url: 'https://civitai.red/models/1/slug?modelVersionId=11' },
      { key: 'r2', tabId: 2, url: 'https://civitai.red/models/2/slug?modelVersionId=12' },
    ];
    const t9 = Date.parse('2026-10-16T06:00:00Z');
    const r9a = await enrich9.enrich(rows9, { fetcher: makeFetcher(table9, log9), now: t9, persist: true, openTabIds: [1, 2] });
    same(r9a.entries.map((e) => e.status), ['rate-limited', 'rate-limited']);
    assert.equal(log9.calls.length, 2);
    assert.ok(r9a.summary.retryAt > t9, 'retryAt 要说清什么时候可以再试');
    assert.ok(r9a.summary.blockedUntil['civitai.red'] > t9);
    const gate9 = env9.chrome.data[enrich9.STORE_KEY].gates['civitai.red'];
    assert.equal(gate9.tries, 2, '同一轮里再吃到 429 → 退避时间翻倍（30s→60s）');
    assert.equal(gate9.until, t9 + 60000);
    // 换一个新调用（等价于 SW 被回收后重新起来）：门禁从 storage 读出来，期内零请求
    const r9b = await enrich9.enrich(rows9, { fetcher: makeFetcher(table9, log9), now: t9 + 5000, persist: true, openTabIds: [1, 2] });
    assert.equal(log9.calls.length, 2, '退避期内再次调用必须一个请求都不发（门禁写在 chrome.storage.local，不靠内存变量）');
    same(r9b.entries.map((e) => e.status), ['rate-limited', 'rate-limited']);
    assert.equal(r9b.entries[0].retryAt, t9 + 60000);
    table9.__next = () => ({ json: modelDoc(1) });
    const r9c = await enrich9.enrich(rows9, { fetcher: makeFetcher(table9, log9), now: t9 + 61000, persist: true, openTabIds: [1, 2] });
    assert.equal(log9.calls.length, 4, '退避到期后才允许再试（两条链路各补一次）');
    assert.equal(r9c.entries[0].status, 'ok');
    assert.equal(r9c.entries[1].status, 'partial', '拿得到模型级、但这个版本不在文档的版本表里 → 如实 partial');
    assert.match(r9c.entries[1].reason, /不在模型/);
    ok('429：按站点退避（写进 storage，扛 SW 30 秒回收），期内零请求，同轮重复限流指数放大，到期自动恢复');

    // ---------- 10. 缓存体积上限与淘汰（标签页不断开关不能无界增长） ----------
    const env10 = loadLib(DEPS);
    const enrich10 = env10.ctx.CFT.enrich;
    const log10 = makeLog();
    const table10 = {};
    for (let i = 0; i < 30; i++) table10['civitai.com/api/v1/models/7' + i] = { json: modelDoc(70 + i, null, { id: 700 + i }) };
    const rows10 = [];
    for (let i = 0; i < 30; i++) rows10.push({ key: 'k' + i, tabId: 200 + i, url: 'https://civitai.com/models/7' + i + '/x?modelVersionId=' + (700 + i) });
    const t10 = Date.parse('2026-10-16T08:00:00Z');
    const r10 = await enrich10.enrich(rows10, { fetcher: makeFetcher(table10, log10), now: t10, maxEntries: 8, openTabIds: rows10.map((r) => r.tabId) });
    assert.equal(r10.summary.ready, 30, '淘汰不能影响本轮返回的行数');
    const st10 = await enrich10.readCache();
    assert.ok(st10.count <= 8, '条目数上限要压住：' + st10.count);
    assert.ok(st10.bytes <= enrich10.DEFAULTS.maxBytes, '字节上限要压住：' + st10.bytes);
    ok('缓存有体积/条目上限并做 LRU 淘汰（30 行只留 ' + st10.count + ' 条，' + st10.bytes + 'B）');
    const closed = await enrich10.pruneCache({ now: t10 + 60 * 60 * 1000, openTabIds: [] });
    const st10b = await enrich10.readCache();
    assert.equal(st10b.count, 0, '过期且标签页全关 → 全部回收');
    assert.ok(closed.dropped > 0);
    ok('pruneCache({openTabIds}) 把"关掉的标签页留下的过期条目"清干净');

    // ---------- 11. 多版本共用一次 models 请求 + 借缓存省请求 ----------
    const env11 = loadLib(DEPS);
    const enrich11 = env11.ctx.CFT.enrich;
    const log11 = makeLog();
    const multi = modelDoc(500, {
      modelVersions: [
        { id: 501, name: 'V2', baseModel: 'SDXL', files: [{ id: 1, name: 'v2.safetensors', sizeKB: 5120 }], images: [{ url: CDN + '/original=true/501.jpg' }], publishedAt: '2026-03-03T00:00:00Z' },
        { id: 502, name: 'V1', baseModel: 'SD 1.5', files: [{ id: 2, name: 'v1.safetensors', sizeKB: 1024 }, { id: 3, name: 'v1.yaml', sizeKB: 4 }], images: [{ url: CDN + '/original=true/502.jpg' }], publishedAt: '2026-03-01T00:00:00Z' },
        { id: 503, name: 'V0', baseModel: 'SD 1.5', files: [{ id: 4, name: 'v0.safetensors', sizeKB: 1024 }], images: [{ url: CDN + '/original=true/503.jpg' }], publishedAt: '2026-02-01T00:00:00Z' },
      ],
    });
    const table11 = { 'civitai.com/api/v1/models/500': { json: multi } };
    const rows11 = [
      { key: 'a', tabId: 1, url: 'https://civitai.com/models/500/x?modelVersionId=501' },
      { key: 'b', tabId: 2, url: 'https://civitai.com/models/500/x?modelVersionId=502' },
      { key: 'c', tabId: 3, url: 'https://civitai.com/models/500/x' },
    ];
    const t11 = Date.parse('2026-10-16T09:00:00Z');
    const r11a = await enrich11.enrich(rows11, { fetcher: makeFetcher(table11, log11), now: t11, persist: true, openTabIds: [1, 2, 3] });
    assert.equal(log11.calls.length, 1, '同一模型的三个版本行只打一次 models/{id}');
    assert.equal(r11a.byKey.a.meta.totalMB, 5);
    assert.equal(r11a.byKey.b.meta.totalMB, 1);
    assert.ok(r11a.byKey.b.meta.fileCount === 2 && r11a.byKey.b.meta.baseModel === 'SD 1.5');
    assert.equal(r11a.byKey.c.meta.versionId, '501', '没有版本参数的行取文档里的第一个版本');
    ok('同模型多版本共用一次请求，且各行拿到各自版本的文件大小/底模');
    // 新开的第三个版本行（503）：它自己没有单元缓存，但同模型的 model-doc 是新鲜的 → 借字段，0 请求
    const table12 = { 'civitai.com/api/v1/models/500': () => { throw new Error('不该再请求 models/500'); } };
    const log12 = makeLog();
    const r12 = await enrich11.enrich([{ key: 'd', tabId: 4, url: 'https://civitai.com/models/500/x?modelVersionId=503' }],
      { fetcher: makeFetcher(table12, log12), now: t11 + 60000, persist: true, openTabIds: [1, 2, 3, 4] });
    assert.equal(log12.calls.length, 0, '模型文档已缓存：新开的版本行一次请求都不发（借 model-doc）');
    assert.equal(r12.byKey.d.meta.totalMB, 1, '借来的文档要能定位到具体版本的体积');
    assert.equal(r12.byKey.d.meta.name, 'Demo Model 500');
    assert.equal(r12.byKey.d.meta.creator, 'maker');
    assert.equal(r12.byKey.d.borrowed, true);
    ok('借同模型的缓存文档补模型级字段：省掉 15.6KB 的那一次请求');

    // ---------- 12. 不联网也不许发请求；行状态词表对齐 tabrows ----------
    const env13 = loadLib(DEPS);
    const enrich13 = env13.ctx.CFT.enrich;
    const r13 = await enrich13.enrich([{ key: 'x', tabId: 1, url: 'https://civitai.com/models/1?a=1' }], { network: false, now: t11 });
    same([r13.entries[0].status, r13.entries[0].state, r13.entries[0].meta], ['pending', 'loading', null]);
    assert.match(r13.entries[0].reason, /别当成/);
    const rowsA = [{ key: 'x', id: '1', kind: 'model', url: 'https://civitai.com/models/1?a=1', tabId: 1 }];
    enrich13.applyToRows(rowsA, r13);
    assert.equal(rowsA[0].state, 'loading');
    const kept = R().applyFilters(rowsA, { types: ['Lora'] });
    assert.equal(kept.length, 1, '信息没回来时不能把行过滤掉（tabrows 对缺字段按不排斥处理，这条依赖 meta.type 缺失）');
    ok('未取数行：state=loading、meta=null，过滤时不被误删（列表不会因为联网慢而变空）');
    assert.ok(enrich13.ROW_STATES.includes('needs-login') && enrich13.ROW_STATES.includes('rate-limited'), 'state 词表要和契约里写的一致');
    const summary = { total: 3, civitai: 3, model: 1, modelDetail: 1, image: 1, list: 0, civitaiOther: 1, offsite: 0, discarded: 0, merged: 0, windows: 1 };
    assert.match(R().summaryText(summary), /可提交模型页 1/);
    assert.match(enrich13.summaryText(r13.summary), /待补全 1/);
    ok('两行汇总互补：CftRows.summaryText 说筛选，CFT.enrich.summaryText 说补全/需登录/限流');

    // ---------- 13. tabs-common 的 key 与行合并 ----------
    const cand = T().candidates([{ id: 1, windowId: 1, title: 'Demo | Civitai', url: 'https://civitai.red/models/1/a?modelVersionId=11' }]);
    const env14 = loadLib(DEPS);
    const r14 = await env14.ctx.CFT.enrich.enrich(cand, { network: false, now: t11 });
    assert.equal(r14.entries[0].key, cand[0].key, 'CftTabs.candidates() 的行键要能原样回填');
    ok('也吃得下 tabs-common.candidates() 的输出（老 popup 路径不用改结构）');

    // ---------- 14. 环境约束：没有 DOM 也能加载；storage 读坏了也不能整批失败 ----------
    assert.equal(typeof loadEnv.ctx.document, 'undefined');
    assert.equal(typeof loadEnv.ctx.window, 'undefined');
    ok('enrich.js 在没有 document/window 的 vm 里就能加载：顶层确实不触碰 DOM');
    const env15 = loadLib(DEPS);
    env15.chrome.storage.local.get = async () => { throw new Error('storage 读不了'); };
    env15.chrome.storage.local.set = async () => { throw new Error('storage 写不了'); };
    const r15 = await env15.ctx.CFT.enrich.enrich(
      [{ key: 'x', tabId: 1, url: 'https://civitai.com/models/123/x?modelVersionId=11' }],
      { fetcher: makeFetcher({ '/api/v1/models/123': { json: modelDoc(123) } }, makeLog()), now: t11 });
    assert.equal(r15.entries[0].status, 'ok', '存储坏了只该失去缓存，不该失去富化');
    assert.equal(r15.entries[0].meta.totalMB, 1026);
    assert.equal(r15.summary.persisted, false);
    ok('chrome.storage.local 读写异常：照常返回结果，仅本次不缓存');

    // ---------- 15. 增量落盘：批次没跑完也得已经在写缓存（SW 随时可能被回收） ----------
    const env16 = loadLib(DEPS);
    const slowTable = {};
    for (let i = 0; i < 8; i++) slowTable['/api/v1/models/8' + i] = async () => { await tick(60); return { json: modelDoc(80 + i, null, { id: 800 + i }) }; };
    const rows16 = [];
    for (let i = 0; i < 8; i++) rows16.push({ key: 's' + i, tabId: 300 + i, url: 'https://civitai.com/models/8' + i + '/x?modelVersionId=' + (800 + i) });
    const inflight = env16.ctx.CFT.enrich.enrich(rows16, { fetcher: makeFetcher(slowTable, makeLog()), now: t11, openTabIds: rows16.map((r) => r.tabId) });
    await tick(90);                                  // 批次仍在跑
    const mid = await env16.ctx.CFT.enrich.readCache();
    assert.ok(mid.count > 0, '批次进行中就该已经落盘，不能只在结尾统一写库');
    const done16 = await inflight;
    assert.equal(done16.summary.ready, 8);
    const after16 = await env16.ctx.CFT.enrich.readCache();
    assert.ok(after16.count >= mid.count && after16.count <= env16.ctx.CFT.enrich.DEFAULTS.maxEntries);
    ok('增量刷盘生效：跑一半时缓存里已有 ' + mid.count + ' 条，结束时 ' + after16.count + ' 条');

    // ---------- 16. 真实默认通道：装了 CFT.api 时走 requestData，并且带上"这一页自己的站点" ----------
    // enrich.js 自己会创建 globalThis.CFT，所以"先加载 enrich、再往 CFT 上挂 api"就是 SW 里的真实组合顺序。
    const env17 = loadLib(['tabs-common.js', 'lib/tabrows.js']);
    vm.runInContext(fs.readFileSync(path.join(root, 'lib', 'enrich.js'), 'utf8'), env17.ctx, { filename: 'lib/enrich.js' });
    const seen = [];
    env17.ctx.CFT.api = {
      // 只 stub 出 enrich 会用到的那一小块接口（requestData）。不加载真的 civitai-api.js：
      // 它会把请求发给不存在的桌面桥/真站点，而本测试要求一次真实网络都不发。
      requestData: async (url, o) => {
        seen.push({ url, pageUrlOrHost: o && o.pageUrlOrHost, headers: (o && o.headers) || {} });
        return { ok: true, status: 200, text: JSON.stringify(modelDoc(123)), transport: 'direct', pageError: '', directError: '' };
      },
    };
    const r17 = await env17.ctx.CFT.enrich.enrich(
      [{ key: 'x', tabId: 1, url: 'https://civitai.red/models/123/x?modelVersionId=11' }], { now: t11, persist: false });
    assert.equal(r17.entries[0].status, 'ok', '注入 fetcher 之外，默认的 CFT.api.requestData 路径也要能富化');
    assert.equal(seen[0].pageUrlOrHost, 'civitai.red', 'requestData 必须收到这一页自己的站点，否则请求会被打到默认的 .com');
    assert.equal(r17.entries[0].channel, 'direct');
    assert.equal(r17.entries[0].meta.anonymous, false);
    same(Object.keys(seen[0].headers).map((h) => h.toLowerCase()), ['accept'], '这条路上同样只有 Accept，没有 Authorization');
    env17.ctx.CFT.api.requestData = async (url, o) => {
      seen.push({ url, pageUrlOrHost: o && o.pageUrlOrHost });
      const body = /\/model-versions\/mini\//.test(url) ? miniDoc() : modelDoc(300, null, { id: 13 });
      return { ok: true, status: 200, text: JSON.stringify(body), transport: 'page' };
    };
    const r17b = await env17.ctx.CFT.enrich.enrich(
      [{ key: 'y', tabId: 2, url: 'https://civitai.com/api/download/models/13' }], { now: t11 + 1, persist: false });
    assert.equal(seen[seen.length - 2].url.indexOf('/model-versions/mini/13') > 0, true, '只有版本ID 时先走 942B 的 mini');
    assert.equal(seen[seen.length - 1].url.indexOf('/api/v1/models/300') > 0, true, 'mini 反查出 modelId 后再补 models/{mid}');
    assert.equal(seen[seen.length - 1].pageUrlOrHost, 'civitai.com', 'mini 反查之后的补全请求也要打这一页自己的站点');
    assert.equal(r17b.entries[0].channel, 'page', '归档任务装了页面通道时，富化跟着借登录态（NSFW 才有机会拿到东西）');
    assert.equal(r17b.entries[0].meta.anonymous, false);
    ok('默认通道接线正确：requestData 拿到的是标签页自己的站点，且 channel/anonymous 如实交代');

    // 兜底分支：既没注入 fetcher、也没装 CFT.api → 用全局 fetch（还是假的那一个，不发真实请求）
    const env18 = loadLib(DEPS);
    const r18 = await env18.ctx.CFT.enrich.enrich(
      [{ key: 'z', tabId: 3, url: 'https://civitai.com/models/123/x?modelVersionId=11' }], { now: t11, persist: false });
    assert.equal(env18.netLog.length, 1, '兜底路径确实调用全局 fetch（假的，测试里没有一次真实网络）');
    assert.equal(r18.entries[0].status, 'ok');
    assert.equal(r18.entries[0].channel, 'direct');
    assert.equal(env18.netLog[0].credentials, 'include');
    same(Object.keys(env18.netLog[0].headers).map((h) => h.toLowerCase()), ['accept'], '兜底路径也只带 Accept，不自造 Authorization');
    // 带凭据被「通配 ACAO + credentials」挡下 → 自动降级匿名重试一次（与 civitai-api.directFetcher 同策略）
    const env19 = loadLib(DEPS);
    let omitTries = 0;
    env19.ctx.fetch = async (url, o) => {
      if (o.credentials === 'include') throw new TypeError('Failed to fetch');
      omitTries += 1;
      return { ok: true, status: 200, text: async () => JSON.stringify(modelDoc(123)) };
    };
    const r19 = await env19.ctx.CFT.enrich.enrich(
      [{ key: 'z', tabId: 3, url: 'https://civitai.com/models/123/x?modelVersionId=11' }], { now: t11, persist: false });
    assert.equal(omitTries, 1);
    assert.equal(r19.entries[0].channel, 'direct-anon');
    assert.equal(r19.entries[0].status, 'ok');
    assert.equal(r19.entries[0].meta.anonymous, true, '匿名拿到的数据要标 anonymous，NSFW 空结果才解释得通');
    ok('兜底取数路径：默认带 Cookie、被 CORS 挡下自动降级匿名一次，并如实标 anonymous');
  }

  console.log('PASSED ' + passed + ' enrich checks（全程假 fetcher，未发起任何真实网络请求）');
})().catch((e) => { console.error('FAILED', e && e.stack || e); process.exitCode = 1; });
