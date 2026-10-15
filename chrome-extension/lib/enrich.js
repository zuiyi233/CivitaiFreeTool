/* CFT.enrich —— 标签页元数据富化：lib/tabrows.js 的上游数据源
 *
 * 它解决的是用户这句抱怨的后半段：「加载的是当前打开浏览器标签页的所有页面，没有对 civitai 站或
 * 特定站点进行筛选」。分类/过滤已由 lib/tabrows.js 做掉，但行上仍然是空的——没有类型、没有底模、
 * 没有大小、没有缩略图，也就没有「这一页值不值得提交」的判断依据。本模块只补数据，不做展示、不做过滤。
 *
 * 设计要点（为什么长这样）：
 *  1) 顶层不触碰 chrome.* / document / window（只在函数体内引用）：`node --check` 与 node vm 单测
 *     都要能直接加载本文件（与 lib/tabrows.js、tabs-common.js 同样的约束）。
 *  2) 缓存按「站点 + 模型/版本/图片身份」存，不按 tabId 各存一份：同一个模型在两个标签页里是同一份
 *     元数据，按 tabId 复制会让缓存随标签页开关无界增长。tabId 记在条目上只做**生命周期回收**
 *     （条目过期 且 它的标签页都已关闭 → 淘汰），另有 条目数上限 + 字节上限 + LRU + 硬性年龄上限。
 *     对调用方仍等价于「tabId+url → meta」：给 url 能定位条目，给 tabId 也能（返回值的 byTabId）。
 *  3) MV3 SW 30 秒空闲回收：缓存与 429 退避门禁都落在 chrome.storage.local，任何"等待"都不依赖内存
 *     变量活过调用间隙。落盘是**增量的**（每完成 4 个单元或每 1.5 秒刷一次，429 门禁立刻刷），
 *     所以一批 50 个标签页跑到一半被回收也不会整批白跑。**遇到 429 不 sleep 重试**（等待会被回收打断、
 *     还把整批拖长），而是把 gate.until 写进存储、本轮直接降级成 rate-limited，调用方按 summary.retryAt
 *     之后再调一次 enrich。
 *  4) 请求一定打到**该标签页自己的站点**：civitai.com 与 civitai.red 各自独立（实测模型 2989579 在
 *     .com 是 404、在 .red 存在）。绝不写死域名，也绝不拿另一站的结果冒充——404 就如实说 404，
 *     reason 里写明"这个 ID 可能只在镜像站上"。缓存 key 也带站点，两站永不互相污染。
 *  5) 只用公开 API：/api/v1/model-versions/mini/{vid}（实测 942B，版本→模型反查最省的一条）、
 *     /api/v1/models/{mid}（实测 15.6KB，字段全）、/api/v1/images?ids=1,2,3（实测 ≤20 个一批可用，
 *     带 ids 时 items 为空、必须读 metadata）。不碰 /api/trpc/*（匿名 401，已由 lib/civitai-api.js
 *     专门处理）；不假设存在批量 models 接口（多个 id 实测 400）。
 *     请求顺序也按省流量排：URL 里 modelId 已知 → 只打 models/{mid}；只有 versionId（例如
 *     /api/download/models/{vid}）→ 先 mini 反查、再按需要补 models；同一模型的多个版本共用一次
 *     models 请求；models 已缓存时版本行**借**它的模型级字段，一次请求都不发。
 *  6) 鉴权：本模块**从不读取也从不转发用户的 Civitai API Key**（不设 Authorization 头、不碰设置里的
 *     key）。扩展自身发请求是跨站匿名，NSFW/受限内容的元数据会被站点清成空（实测
 *     /api/v1/images?imageId=145224191 匿名 200 但 {"items":[],"metadata":{}}）。这种"空"和"这个模型
 *     真没数据"是两件事，所以空结果一律标 status:'needs-login' + meta.needsLogin=true，
 *     绝不写成 missing（那等于谎报"没有数据"）。
 *  7) 并发上限按 **HTTP 请求** 计（默认 4）：图片按 id 批量成一条请求，同模型多版本共用一条 models
 *     请求——标签页再多也不会打爆站点；队列跑动中还能追加任务（mini→models 依赖链由同一批 worker 接走）。
 *  8) 逐条降级：一个标签页失败（404/401/429/超时/JSON 坏）只影响它自己那条 entry，整批照常返回；
 *     有陈旧缓存时宁可给旧值并标 staleUsed=true，也不把列表渲染成空白。
 *  9) 缩略图：image.civitai.com 的 original=true 返回原图，列表行只要按宽度重写的缩略图
 *     （thumbUrl 去掉 original=true / 改写 width= 段），非该 CDN 的地址一律原样返回。
 *
 * 消费方（popup / 工作台）请先看 lib/enrich.test.cjs 顶部的「集成契约」。
 */
(function (root) {
  'use strict';

  const CIVITAI_HOSTS = ['civitai.com', 'civitai.red'];
  const STORE_KEY = 'cftEnrichV1';
  const STORE_VERSION = 1;

  const DEFAULTS = {
    ttlMs: 10 * 60 * 1000,          // 模型元数据不常变：10 分钟量级
    missingTtlMs: 5 * 60 * 1000,    // 404 也缓存（省得每次刷新白打），但更短，下架/改名要能较快翻案
    hardAgeMs: 24 * 60 * 60 * 1000, // 超过一天的条目连"陈旧兜底"的价值都没有，直接丢
    maxEntries: 160,                // 条目数上限（超出按 LRU 淘汰）
    maxBytes: 200 * 1024,           // 体积上限：缓存必须有界，标签页会不断开关
    concurrency: 4,                 // 同时在飞的请求上限，超出的排队
    imageIdsMax: 20,                // /api/v1/images?ids= 实测 1..20 可用
    timeoutMs: 12000,
    backoffBaseMs: 30 * 1000,       // 429：30s → 60s → 120s …（按站点分别记，指数退避）
    backoffMaxMs: 10 * 60 * 1000,
    maxTabIdsPerEntry: 8,
    maxVersionsKept: 10,            // 模型文档里保留"详细信息"的版本数
    maxVersionIdsKept: 40,          // 模型文档里保留"版本 ID 名录"的上限
    maxFilesKept: 12,
    maxTagsKept: 12,
    maxWordsKept: 8,
    maxTasks: 400,                  // 单批请求数硬上限（防依赖链跑飞）
    thumbWidth: 256,                // 列表行只要缩略图
  };

  // 与 offscreen/archive-worker.js 的 MODEL_TYPE_MAP 同一份口径，否则两边的类型筛选项对不上
  const MODEL_TYPE_MAP = { 1: 'CheckPoint', 2: 'embedding', 3: 'HYPERNETWORK', 4: 'AESTHETIC GRADIENT', 5: 'Lora', 6: 'LyCORIS', 9: 'WILDCARDS' };

  const ST = {
    OK: 'ok', PARTIAL: 'partial', STALE: 'stale', NEEDS_LOGIN: 'needs-login', MISSING: 'missing',
    RATE_LIMITED: 'rate-limited', ERROR: 'error', SKIPPED: 'skipped', PENDING: 'pending',
  };
  // row.state 的词表 —— tabrows.applyFilters 的 filters.states 就吃这些值
  const STATE_OF = {
    ok: 'ready', partial: 'ready', stale: 'ready', 'needs-login': 'needs-login', missing: 'missing',
    'rate-limited': 'rate-limited', error: 'error', skipped: 'skipped', pending: 'loading',
  };
  const ROW_STATES = ['ready', 'loading', 'needs-login', 'missing', 'rate-limited', 'error', 'skipped'];

  // 「模型级」字段：同一模型的多个版本共享，可从缓存里的模型文档借用（省掉一次 15.6KB 请求）。
  // 刻意不含版本级字段（versionId/totalMB/fileCount/baseModel/files/thumb/publishedAt）——借错会串版本。
  const MODEL_LEVEL_FIELDS = ['modelId', 'name', 'type', 'tags', 'creator', 'nsfw', 'nsfwLevel',
    'downloadCount', 'ratingCount', 'likedCount', 'collectedCount', 'versionCount', 'versionIds', 'updatedAt'];

  const ID_OK = /^[1-9]\d*$/;

  // ---- 小工具（纯函数） ----
  function str(v) { return v == null ? '' : String(v); }
  function num(v) { const n = Number(v); return Number.isFinite(n) ? n : null; }
  function trimArr(v, max) {
    const out = [];
    for (const x of Array.isArray(v) ? v : []) {
      const s = str(x).trim();
      if (s && out.indexOf(s) < 0) out.push(s);
      if (out.length >= max) break;
    }
    return out;
  }
  function browserApi() { try { return root.chrome || root.browser || null; } catch (_) { return null; } }
  function fetchImpl() { try { return typeof root.fetch === 'function' ? root.fetch : null; } catch (_) { return null; } }
  function parseJsonSafe(text) { try { return JSON.parse(String(text)); } catch (_) { return null; } }

  // ---- 站点与 URL 解析（纯函数，node 下可直接单测） ----
  // 标签页 URL → 站点（去 www.）。协议只认 http(s)，host 不在白名单一律返回 ''（绝不让页面串当请求目标）。
  function siteOf(raw) {
    try {
      const u = new URL(String(raw || ''));
      if (u.protocol !== 'https:' && u.protocol !== 'http:') return '';
      const h = String(u.hostname || '').toLowerCase().replace(/^www\./, '');
      return CIVITAI_HOSTS.indexOf(h) >= 0 ? h : '';
    } catch (_) { return ''; }
  }

  // URL → 取数入参。只认这几类（其余返回 null，不猜）：
  //   /models/{mid}                        → model（modelId 已知：只打 models/{mid}）
  //   /models/{mid}?modelVersionId={vid}   → version（mid+vid 都已知：1 次请求就够，不需要 mini）
  //   /models/{mid}/versions/{vid}         → version
  //   /api/download/models/{vid}           → version（**这里的数字是版本ID 不是模型ID**，与 tabs-common.modelLink 同口径）
  //   /images/{iid} | /posts/{iid}         → image
  // ?fileId= 忽略：它不影响"是哪个模型/版本"，而 tabrows 的行 key 已带 file 段，重复合并照常生效。
  function parseRef(raw) {
    const url = String(raw || '');
    let u; try { u = new URL(url); } catch (_) { return null; }
    const site = siteOf(url);
    if (!site) return null;
    const host = String(u.hostname || '').toLowerCase();
    const ref = { kind: '', modelId: '', versionId: '', imageId: '', site, host, origin: 'https://' + host, url: u.href, key: '' };
    let path = u.pathname || '/';
    if (path.length > 1) path = path.replace(/\/+$/, '');
    let m = path.match(/^\/api\/download\/models\/([1-9]\d*)$/i);
    if (m) { ref.kind = 'version'; ref.versionId = m[1]; ref.key = 'version:' + m[1]; return ref; }
    m = path.match(/^\/models\/([1-9]\d*)(?:\/([^/?#]*))?(?:\/([^/?#]*))?$/i);
    if (m) {
      ref.modelId = m[1];
      const q = u.searchParams.getAll('modelVersionId');
      if (q.length === 1 && ID_OK.test(q[0])) ref.versionId = q[0];   // 多个值＝歧义，和 tabs-common 一样放弃
      if (!ref.versionId && /^versions$/i.test(m[2] || '') && ID_OK.test(m[3] || '')) ref.versionId = m[3];
      ref.kind = ref.versionId ? 'version' : 'model';
      ref.key = ref.kind + ':' + (ref.versionId || ref.modelId);
      return ref;
    }
    m = path.match(/^\/(?:images|posts)\/([1-9]\d*)/i);
    if (m) { ref.kind = 'image'; ref.imageId = m[1]; ref.key = 'image:' + m[1]; return ref; }
    return null;
  }

  function ckOf(ref) {
    if (!ref || !ref.kind) return '';
    const id = ref.kind === 'image' ? ref.imageId : (ref.kind === 'version' ? ref.versionId : ref.modelId);
    return str(ref.site) + '|' + ref.kind + ':' + id;
  }
  // 模型文档单独占一个 key 空间：`|model:` 是"某一行的结果"，`|model-doc:` 是"可借用的公共数据"，
  // 两者混在一个 key 上会让 /models/{mid} 那一行和它的文档互相覆盖。
  function docCkOf(site, modelId) { return site + '|model-doc:' + modelId; }

  // image.civitai.com 的 original=true 返回原图；列表行要的是按宽度重写的缩略图 CDN 地址。
  // 已知两种形态：路径段 /original=true/（lib/util.js getCivitaiImageUrlCandidates 同款）与查询参数 ?original=true。
  function thumbUrl(raw, width) {
    const w = num(width) > 0 ? num(width) : DEFAULTS.thumbWidth;
    const s = str(raw);
    if (!s) return '';
    let p; try { p = new URL(s); } catch (_) { return s; }
    if (!/(^|\.)image\.civitai\.com$/i.test(p.hostname)) return s;   // 别人的 URL 不改写
    try {
      if (p.pathname.indexOf('/original=true/') >= 0) p.pathname = p.pathname.replace('/original=true/', '/width=' + w + '/');
      else if (/\/width=\d+\//.test(p.pathname)) p.pathname = p.pathname.replace(/\/width=\d+\//, '/width=' + w + '/');
      if (p.searchParams.has('original')) p.searchParams.delete('original');
      return p.href;
    } catch (_) { return s; }
  }

  function chunk(list, size) {
    const n = num(size) > 0 ? num(size) : 1;
    const src = Array.isArray(list) ? list : [];
    const out = [];
    for (let i = 0; i < src.length; i += n) out.push(src.slice(i, i + n));
    return out;
  }

  // ---- 字段投影（纯函数：站点 JSON → tabrows 能直接消费的 meta） ----
  // 站点在不同接口/时期给的字形不一样（type 可能是数字 modelType、字符串或对象；baseModel 可能是数组），
  // 所以下面全部宽松取值；取不到就留 undefined —— tabrows 的过滤对缺字段按「不排斥」处理，正好。
  function modelTypeOf(model) {
    const byCode = MODEL_TYPE_MAP[Number(model && model.modelType)];
    if (byCode) return byCode;
    const t = model && model.type;
    if (typeof t === 'string' && t) return t;
    if (t && typeof t === 'object') return str(t.name || t.type || t.value);
    return '';
  }
  function baseModelOf(v) {
    if (!v || typeof v !== 'object') return '';
    if (typeof v.baseModel === 'string') return v.baseModel;
    if (Array.isArray(v.baseModels)) return str(v.baseModels[0] || '');
    if (v.baseModel && typeof v.baseModel === 'object') return str(v.baseModel.name || v.baseModel.value);
    return '';
  }
  // 文件大小：公开 API 给 sizeKB（download_selection.py / gui.py / reverse_parse.py 同字段）；页面侧另有
  // size（字节）。只有 sizeKB 缺失时才用 size，并按「≥1e7 一定是字节」判——没有模型有 10TB，KB 单位不可能。
  function fileMB(f) {
    if (!f || typeof f !== 'object') return null;
    const kb = num(f.sizeKB);
    if (kb != null && kb > 0) return Math.round(kb / 102.4) / 10;
    const b = num(f.sizeBytes);
    if (b != null && b > 0) return Math.round(b / 104857.6) / 10;
    const s = num(f.size);
    if (s == null || s <= 0) return null;
    return s >= 1e7 ? Math.round(s / 104857.6) / 10 : Math.round(s / 102.4) / 10;
  }
  function nsfwLevelOf(v) { const n = num(v && v.nsfwLevel); return n == null ? null : n; }
  function pickThumb(images, width) {
    const list = Array.isArray(images) ? images : [];
    for (const im of list) {
      const url = im && typeof im === 'object' ? str(im.url) : str(im);
      if (url) return thumbUrl(url, width);
    }
    return '';
  }

  // 单个版本对象（models 文档里的元素，或 model-versions/mini 的返回体）→ 版本级字段
  function projectVersionMeta(v, width) {
    const ver = v && typeof v === 'object' ? v : {};
    const w = num(width) > 0 ? num(width) : DEFAULTS.thumbWidth;
    const files = Array.isArray(ver.files) ? ver.files : [];
    const sizes = files.map(fileMB).filter((x) => x != null);
    const trainedWords = Array.isArray(ver.trainedWords)
      ? trimArr(ver.trainedWords, DEFAULTS.maxWordsKept)
      : trimArr(str(ver.trainedWords).split(/[,，\s]+/), DEFAULTS.maxWordsKept);
    return {
      versionId: str(ver.id) || '',
      versionName: str(ver.name) || '',
      baseModel: baseModelOf(ver),
      trainedWords,
      fileCount: files.length,
      totalMB: sizes.length ? Math.round(sizes.reduce((a, b) => a + b, 0) * 10) / 10 : null,
      largestFileMB: sizes.length ? Math.max.apply(null, sizes) : null,
      files: files.slice(0, Math.max(1, DEFAULTS.maxFilesKept)).map((f) => ({
        name: str(f && f.name) || '', sizeMB: fileMB(f), type: str(f && f.type) || '',
      })),
      thumb: pickThumb(ver.images, w),
      mediaCount: Array.isArray(ver.images) ? ver.images.length : 0,
      publishedAt: str(ver.publishedAt) || str(ver.createdAt) || '',
      nsfw: typeof ver.nsfw === 'boolean' ? ver.nsfw : null,
      nsfwLevel: nsfwLevelOf(ver),
      earlyAccess: !!ver.earlyAccessPolicy || !!ver.earlyAccessDays,
    };
  }

  // /api/v1/models/{mid} → 模型级字段 + 瘦身后的版本表（15.6KB 原样进缓存会立刻把体积顶满）
  function projectModelDoc(json, width) {
    const model = json && typeof json === 'object' ? json : {};
    const versions = Array.isArray(model.modelVersions) ? model.modelVersions : [];
    const w = num(width) > 0 ? num(width) : DEFAULTS.thumbWidth;
    const stats = model.stats && typeof model.stats === 'object' ? model.stats : {};
    const pick = (a, b) => { const n = num(a != null ? a : b); return n == null ? 0 : n; };
    return {
      modelId: str(model.id) || '',
      name: str(model.name) || '',
      type: modelTypeOf(model),
      tags: trimArr(model.tags, DEFAULTS.maxTagsKept),
      creator: str((model.creator && (model.creator.username || model.creator.name)) || ''),
      nsfw: typeof model.nsfw === 'boolean' ? model.nsfw : null,
      nsfwLevel: nsfwLevelOf(model),
      downloadCount: pick(stats.downloadCount, model.downloadCount),
      ratingCount: pick(stats.ratingCount, model.ratingCount),
      likedCount: pick(stats.likedCount, model.likedCount),
      collectedCount: pick(stats.collectedCount, model.collectedCount),
      updatedAt: str(model.updatedAt) || str(model.createdAt) || '',
      versionCount: versions.length,
      versionIds: trimArr(versions.map((v) => str(v && v.id)), DEFAULTS.maxVersionIdsKept),
      versions: versions.slice(0, Math.max(1, DEFAULTS.maxVersionsKept)).map((v) => projectVersionMeta(v, w)),
    };
  }

  function pickModelFields(doc) {
    const out = {};
    for (const k of MODEL_LEVEL_FIELDS) if (doc && doc[k] !== undefined) out[k] = doc[k];
    return out;
  }
  function findVersionInDoc(docFields, versionId) {
    const list = (docFields && Array.isArray(docFields.versions)) ? docFields.versions : [];
    const vid = str(versionId);
    if (!vid) return null;
    for (const v of list) if (v && str(v.versionId) === vid) return v;
    return null;
  }

  // ---- 图片接口投影：{metadata:{<id>:{...}}, items:[...]}；带 ids= 时 items 为空、必须读 metadata ----
  function unwrapImageMeta(rec) {
    const api = root.CFT && root.CFT.api;
    if (api && typeof api.unwrapImageMeta === 'function') { try { return api.unwrapImageMeta(rec); } catch (_) { /* 落回本地实现 */ } }
    if (!rec || typeof rec !== 'object' || Array.isArray(rec)) return null;
    const inner = rec.meta;
    if (inner && typeof inner === 'object' && !Array.isArray(inner)) return inner;
    if (typeof inner === 'string') { const p = parseJsonSafe(inner); if (p && typeof p === 'object' && !Array.isArray(p)) return p; }
    return rec;
  }
  // 图片→模型的关联：item.modelVersionIds / meta.civitaiResources[].modelVersionId / meta.resources[]
  // （与 civitai-api.imageGenerationRefs 同一取值思路）。取不到就空数组，绝不编造 ID。
  function imageRefs(rec, flat) {
    const out = [];
    const push = (v) => { const s = str(v); if (s && ID_OK.test(s) && out.indexOf(s) < 0) out.push(s); };
    const lists = [rec && rec.modelVersionIds, rec && rec.modelIds, flat && flat.modelVersionIds];
    for (const l of lists) for (const x of Array.isArray(l) ? l : []) push(x);
    for (const key of ['civitaiResources', 'resources']) {
      const arr = Array.isArray(flat && flat[key]) ? flat[key] : [];
      for (const r of arr) { push(r && r.modelVersionId); push(r && r.versionId); }
    }
    return out;
  }
  function projectImage(rec, width) {
    const r = rec && typeof rec === 'object' ? rec : {};
    const flat = unwrapImageMeta(r) || {};
    const w = num(width) > 0 ? num(width) : DEFAULTS.thumbWidth;
    const lvl = nsfwLevelOf(r);
    const keys = flat && typeof flat === 'object' ? Object.keys(flat) : [];
    return {
      imageId: str(r.id) || '',
      type: str(r.type) || '',                       // 'image' | 'video'
      nsfw: typeof r.nsfw === 'boolean' ? r.nsfw : (lvl != null ? lvl > 0 : null),
      nsfwLevel: lvl,
      creator: str((r.user && (r.user.username || r.user.name)) || ''),
      thumb: str(r.url) ? thumbUrl(r.url, w) : '',
      url: str(r.url) || '',
      mediaCount: str(r.url) ? 1 : 0,
      publishedAt: str(r.createdAt) || '',
      hasPrompt: !!(flat && (flat.prompt != null || flat.negativePrompt != null || keys.length > 2)),
      linkedVersionIds: imageRefs(r, flat),
      baseModel: baseModelOf(flat),
    };
  }

  // ---- chrome.storage.local 缓存层（全部只在函数体内引用 chrome） ----
  // 结构：{version, savedAt,
  //        entries:{[ck]:{ck,site,kind,refId,url,at,last,ttl,status,reason,channel,tabIds[],meta{}}},
  //        gates:{[site]:{until,tries,at,lastStatus}}}
  function loadStore() {
    const out = { version: STORE_VERSION, savedAt: 0, entries: {}, gates: {} };
    const api = browserApi();
    if (!api || !api.storage || !api.storage.local || typeof api.storage.local.get !== 'function') return Promise.resolve(out);
    return Promise.resolve()
      .then(() => api.storage.local.get(STORE_KEY))
      .then((raw) => {
        const s = raw && raw[STORE_KEY];
        if (s && typeof s === 'object') {
          if (s.entries && typeof s.entries === 'object') out.entries = s.entries;
          if (s.gates && typeof s.gates === 'object') out.gates = s.gates;
          out.savedAt = num(s.savedAt) || 0;
        }
        return out;
      })
      .catch(() => out);   // 读不到就当作空缓存：富化只会变慢，不能因此整批失败
  }

  function writeStore(store) {
    const api = browserApi();
    if (!api || !api.storage || !api.storage.local || typeof api.storage.local.set !== 'function') return Promise.resolve(false);
    return Promise.resolve()
      .then(() => api.storage.local.set({
        [STORE_KEY]: { version: STORE_VERSION, savedAt: Date.now(), entries: store.entries, gates: store.gates },
      }))
      .then(() => true)
      .catch(() => false); // 配额/写失败由调用方削减后重试一次
  }

  function storeBytes(entries) {
    try { return JSON.stringify(entries || {}).length; } catch (_) { return Number.MAX_SAFE_INTEGER; }
  }

  function ttlFor(status, cfg) {
    const c = cfg || DEFAULTS;
    if (status === ST.MISSING) return Math.min(num(c.missingTtlMs) || DEFAULTS.missingTtlMs, num(c.ttlMs) || DEFAULTS.ttlMs);
    return num(c.ttlMs) || DEFAULTS.ttlMs;
  }

  // 命中返回 {entry, stale}：过期但没超硬性年龄的条目仍返回（stale:true），
  // 用途是"这一轮取数失败了至少还有旧值可显示"，不是"当新数据用"。
  function cacheGet(store, ck, now, cfg) {
    const e = store && store.entries ? store.entries[ck] : null;
    if (!e || typeof e !== 'object' || !e.meta || !(num(e.at) > 0)) return null;
    const hardAge = num(cfg && cfg.hardAgeMs) || DEFAULTS.hardAgeMs;
    const age = now - e.at;
    if (age > hardAge) return null;
    const ttl = num(e.ttl) || (num(cfg && cfg.ttlMs) || DEFAULTS.ttlMs);   // 条目自带写入时的 ttl（尊重调用方的 ttlMs 覆盖）
    return age <= ttl ? { entry: e, stale: false } : { entry: e, stale: true };
  }

  function attachTab(entry, tabId, url) {
    const t = num(tabId);
    if (t == null) return;
    if (!Array.isArray(entry.tabIds)) entry.tabIds = [];
    if (entry.tabIds.indexOf(t) < 0) {
      entry.tabIds.push(t);
      while (entry.tabIds.length > DEFAULTS.maxTabIdsPerEntry) entry.tabIds.shift();
    }
    if (url) entry.url = String(url);
  }

  // 淘汰：① 硬性年龄上限；②「已过期 + 它的标签页全关了」的条目（这正是"标签页不断开关"留下的垃圾）；
  // ③④ 条目数与字节数上限，都按 LRU（last 最旧先删）。limits 可覆盖 maxEntries/maxBytes/hardAgeMs。
  function evict(store, now, openTabIds, limits) {
    const cap = {
      maxEntries: num(limits && limits.maxEntries) || DEFAULTS.maxEntries,
      maxBytes: num(limits && limits.maxBytes) || DEFAULTS.maxBytes,
      hardAgeMs: num(limits && limits.hardAgeMs) || DEFAULTS.hardAgeMs,
    };
    const res = { dropped: 0, kept: 0, bytes: 0, cap: cap };
    const entries = (store && store.entries) || {};
    const knowsOpen = Array.isArray(openTabIds);
    const live = [];
    for (const ck of Object.keys(entries)) {
      const e = entries[ck];
      const at = num(e && e.at);
      if (!e || typeof e !== 'object' || !e.meta || !(at > 0)) { delete entries[ck]; res.dropped++; continue; }
      const last = num(e.last) || at;
      const expired = now - at > (num(e.ttl) || DEFAULTS.ttlMs);
      const aged = now - last > cap.hardAgeMs;
      const tabs = Array.isArray(e.tabIds) ? e.tabIds : [];
      const tabsGone = knowsOpen && tabs.length > 0 && !tabs.some((t) => openTabIds.indexOf(t) >= 0);
      if (aged || (expired && tabsGone)) { delete entries[ck]; res.dropped++; continue; }
      live.push({ ck, last });
    }
    live.sort((a, b) => a.last - b.last);
    while (live.length > cap.maxEntries) { delete entries[live.shift().ck]; res.dropped++; }
    res.bytes = storeBytes(entries);
    while (live.length && res.bytes > cap.maxBytes) {
      const victim = live.shift();
      if (!entries[victim.ck]) continue;
      delete entries[victim.ck]; res.dropped++;
      res.bytes = storeBytes(entries);
    }
    res.kept = Object.keys(entries).length;
    return res;
  }

  // ---- 429 退避门禁（按站点记在存储里 → 扛 SW 回收；到期自然失效，不需要显式清理） ----
  function gateActive(store, site, now) {
    const g = store && store.gates ? store.gates[site] : null;
    const until = g ? num(g.until) : null;
    return until != null && until > now ? g : null;
  }
  function gateMark(store, site, now, cfg) {
    const g = store && store.gates ? store.gates[site] : null;
    const prevUntil = g ? num(g.until) : null;
    // 上一轮退避仍在生效期 → 指数放大；已到期 → tries 归零，重新从 30s 起算（别把一次抖动记成永久）
    const tries = (prevUntil != null && prevUntil > now) ? (num(g.tries) || 0) : 0;
    const ms = Math.min(cfg.backoffBaseMs * Math.pow(2, tries), cfg.backoffMaxMs);
    const gate = { until: now + ms, tries: tries + 1, at: now, lastStatus: 429 };
    store.gates[site] = gate;
    return gate;
  }

  // ---- 取数层：可注入 fetcher（单测/由 bg 代发）；默认复用 CFT.api.requestData，再退到裸 fetch ----
  // 统一形状 {ok, status, json, channel, error}；channel='direct-anon' 说明这次确定是匿名请求。
  // 这里绝不设置 Authorization、绝不读取设置里的 Civitai API Key（约束见文件头 6)）。
  async function getJson(url, ctx, site) {
    const cfg = ctx.cfg;
    const headers = { Accept: 'application/json' };
    if (typeof ctx.fetcher === 'function') {
      try {
        const r = await ctx.fetcher(url, { site, headers, timeoutMs: cfg.timeoutMs, now: ctx.now });
        return normalizeFetcherResult(r);
      } catch (e) {
        return { ok: false, status: 0, json: null, channel: 'injected', error: 'fetcher 抛错: ' + str((e && e.message) || e) };
      }
    }
    const api = root.CFT && root.CFT.api;
    if (api && typeof api.requestData === 'function') {
      // 复用归档链路那套传输层：装了页面 fetcher 就先借登录态，且它自带 credentials 降级重试。
      // pageUrlOrHost 传的是**这个标签页自己的站点**（civitaiBaseUrl 只认白名单 host）。
      try {
        const res = await api.requestData(url, {
          headers, timeoutMs: cfg.timeoutMs, pageUrlOrHost: site, credentials: ctx.opts.credentials || 'include',
        });
        return {
          ok: res.ok === true,
          status: num(res.status) || 0,
          json: res.ok === true ? parseJsonSafe(res.text) : null,
          channel: res.transport === 'page' ? 'page' : 'direct',
          error: res.ok === true ? '' : str(res.directError || res.pageError) || ('HTTP ' + (res.status || 0)),
        };
      } catch (e) {
        return { ok: false, status: 0, json: null, channel: 'direct', error: '请求失败: ' + str((e && e.message) || e) };
      }
    }
    const doFetch = fetchImpl();
    if (!doFetch) return { ok: false, status: 0, json: null, channel: '', error: '当前环境没有 fetch，无法联网补全' };
    const run = async (credentials) => {
      const ctrl = new AbortController();
      const timer = setTimeout(() => { try { ctrl.abort(); } catch (_) {} }, cfg.timeoutMs);
      try { return await doFetch(url, { credentials, headers, signal: ctrl.signal }); }
      finally { clearTimeout(timer); }
    };
    const read = async (resp, channel) => {
      const text = await resp.text();
      const status = num(resp.status) || 0;
      const ok = resp.ok === true;
      return { ok, status, json: ok ? parseJsonSafe(text) : null, channel, error: ok ? '' : 'HTTP ' + status };
    };
    const cred = ctx.opts.credentials || 'include';
    try {
      const resp = await run(cred);
      return await read(resp, cred === 'omit' ? 'direct-anon' : 'direct');
    } catch (e) {
      if (e && e.name === 'AbortError') return { ok: false, status: 0, json: null, channel: 'direct', error: '请求超时（' + cfg.timeoutMs + 'ms）' };
      if (cred === 'omit') return { ok: false, status: 0, json: null, channel: 'direct-anon', error: '请求失败: ' + str((e && e.message) || e) };
      // 带凭据的跨源请求会被「通配 ACAO + credentials」规则直接 reject（公开 API 实测 ACAO:*）。
      // 退回匿名重试一次——最坏等于改造前的行为，不会更差（同 civitai-api.directFetcher 的说明）。
      try {
        const resp = await run('omit');
        return await read(resp, 'direct-anon');
      } catch (e2) {
        return { ok: false, status: 0, json: null, channel: 'direct-anon', error: '请求失败: ' + str((e2 && e2.message) || e2) };
      }
    }
  }
  function normalizeFetcherResult(r) {
    if (!r || typeof r !== 'object') return { ok: false, status: 0, json: null, channel: 'injected', error: 'fetcher 返回结构异常' };
    const status = num(r.status) || 0;
    const ok = r.ok === true;
    let json = r.json !== undefined ? r.json : null;
    if (ok && json == null && typeof r.text === 'string') json = parseJsonSafe(r.text);
    return { ok, status, json, channel: str(r.channel) || 'injected', error: ok ? '' : str(r.error) || ('HTTP ' + status) };
  }

  function statusFromFailure(res) {
    const s = num(res && res.status) || 0;
    if (s === 404 || s === 410) return ST.MISSING;
    if (s === 401 || s === 403) return ST.NEEDS_LOGIN;
    if (s === 429) return ST.RATE_LIMITED;
    return ST.ERROR;
  }

  // ---- 取数单元（unit）与批次上下文 ----
  function newUnit(ctx, ck, ref) {
    const u = {
      ck, ref, kind: ref.kind, site: ref.site, keys: [], tabIds: [], url: str(ref.url),
      status: ST.PENDING, reason: '', retryAt: null, cached: false, from: '', channel: '',
      fields: {}, stale: null, docCk: '', borrowed: false, staleUsed: false, atCache: 0, versionPicked: '',
    };
    ctx.units.set(ck, u);
    return u;
  }
  // 一个取数单元可以对应多行（同模型的重复标签页、只差 fileId 的两行），这里只做有界合并
  function pushTarget(unit, t) {
    if (unit.keys.indexOf(t.key) < 0) unit.keys.push(t.key);
    if (t.tabId != null && unit.tabIds.indexOf(t.tabId) < 0) {
      unit.tabIds.push(t.tabId);
      while (unit.tabIds.length > DEFAULTS.maxTabIdsPerEntry) unit.tabIds.shift();
    }
    if (t.url) unit.url = t.url;
  }

  // meta 的最终形状：unit.fields + 身份/降级标记。缓存里存的与返回给调用方的必须是同一个形状。
  function buildMeta(ctx, unit) {
    const ref = unit.ref || {};
    const m = Object.assign({}, unit.fields);
    m.kind = ref.kind || unit.kind || '';
    m.site = unit.site || ref.site || '';
    if (ref.modelId || m.modelId) m.modelId = str(m.modelId || ref.modelId);
    if (ref.versionId || m.versionId) m.versionId = str(m.versionId || ref.versionId);
    if (ref.imageId) m.imageId = str(ref.imageId);
    m.submittable = (m.kind === 'model' || m.kind === 'version') && unit.status !== ST.MISSING;
    m.exists = unit.status === ST.MISSING ? false
      : (unit.status === ST.NEEDS_LOGIN || unit.status === ST.ERROR || unit.status === ST.RATE_LIMITED || unit.status === ST.PENDING) ? null : true;
    m.needsLogin = unit.status === ST.NEEDS_LOGIN;
    m.staleUsed = !!unit.staleUsed;
    m.anonymous = unit.channel === 'direct-anon';
    m.enrichedAt = num(unit.atCache) || ctx.now;
    return m;
  }

  function finishUnit(ctx, unit, channel) {
    if (channel && !unit.channel) unit.channel = channel;
    if (unit.status !== ST.PENDING) return unit;      // 已经判过（失败或缓存命中）就不要再改它的状态
    const f = unit.fields;
    const hasModel = f.name != null || f.type != null || f.creator != null || f.downloadCount != null;
    const hasVersion = !!f.versionId || f.totalMB != null || !!f.baseModel || f.fileCount != null;
    if (unit.kind === 'image') unit.status = ST.OK;
    else {
      unit.status = hasModel && hasVersion ? ST.OK : ST.PARTIAL;
      if (unit.status === ST.PARTIAL && !unit.reason) {
        unit.reason = hasVersion
          ? '只拿到版本级信息：模型类型/封面/下载量/标签要 /api/v1/models/' + str(unit.ref.modelId || f.modelId || '?') + '，这次没拿到（限流/该站 404/无模型关联）。'
          : '只拿到模型级信息：版本 ' + str(unit.ref.versionId || '?') + ' 不在返回的版本详情里，文件大小/底模未补全。';
      }
    }
    if (!unit.from) unit.from = unit.cached ? 'cache' : 'network';
    persistUnit(ctx, unit);
    scheduleFlush(ctx, false);
    return unit;
  }

  function failUnit(ctx, unit, status, res, site) {
    unit.status = status;
    if (!unit.from) unit.from = 'network';
    const idText = str(unit.ref.modelId || unit.fields.modelId || unit.ref.versionId || unit.ref.imageId || unit.ck);
    if (status === ST.MISSING) {
      unit.reason = 'ID ' + idText + ' 在 ' + site + ' 上返回 404（这个站确实没有它）。同一个 ID 在 civitai.com / civitai.red 上可能只存在于一边'
        + '（实测 2989579 在 .com 是 404、在 .red 镜像站上存在）——本行按标签页自己的站点请求，我没有拿另一站的结果冒充它。';
    } else if (status === ST.NEEDS_LOGIN) {
      unit.reason = '需要登录态（不是"没有数据"）：站点把这次请求当匿名请求处理'
        + (res && res.status && res.status !== 200 ? '（HTTP ' + res.status + '）' : '（返回 200 但 items/metadata 被清空）')
        + '，受限/NSFW 内容的元数据不会给出。本模块不读取也不转发你的 Civitai API Key。';
    } else if (status === ST.RATE_LIMITED) {
      unit.retryAt = ctx.retryAt || null;
      unit.reason = '站点限流（429），已对 ' + site + ' 退避' + (ctx.retryAt ? ' 到 ' + new Date(ctx.retryAt).toISOString() : '')
        + '；本轮不重试（MV3 SW 30 秒空闲回收，sleep 等待不可靠），到点再调一次 enrich 即可。';
    } else {
      unit.reason = '补全失败：' + (str(res && res.error) || '未知错误') + (res && res.status ? '（HTTP ' + res.status + '）' : '');
    }
    if (unit.stale && unit.stale.meta && !Object.keys(unit.fields).length) {
      // 有旧数据就先给旧的，别把列表渲染成空白；但 status 保持失败态，reason 会说清这是陈旧值
      unit.fields = Object.assign({}, unit.stale.meta);
      unit.staleUsed = true;
      unit.atCache = num(unit.stale.at) || 0;
      unit.from = 'cache-stale';
      ctx.summary.staleServed += 1;
    }
    persistUnit(ctx, unit);
    scheduleFlush(ctx, false);
    return unit;
  }

  function markRateLimited(ctx, units, site) {
    const g = gateActive(ctx.store, site, ctx.now);
    if (g) ctx.retryAt = num(g.until) || ctx.retryAt;
    for (const u of units) if (u.status === ST.PENDING) failUnit(ctx, u, ST.RATE_LIMITED, { status: 429, error: '' }, site);
  }

  // ---- 模型文档：同一模型的多个版本共用一次 /api/v1/models/{mid} ----
  function groupOf(ctx, site, modelId) {
    const ck = docCkOf(site, modelId);
    let g = ctx.docGroups.get(ck);
    if (!g) { g = { ck, site, modelId, units: [], queued: false, done: false, doc: null, status: ST.PENDING, channel: '' }; ctx.docGroups.set(ck, g); }
    return g;
  }
  function attachToGroup(ctx, group, unit) {
    if (group.units.indexOf(unit) < 0) group.units.push(unit);
    unit.docCk = group.ck;
    if (group.done) { applyDoc(ctx, group, group.lastRes); return; }   // 迟到的单元：直接用已取到的文档
    if (!group.queued) { group.queued = true; queueTask(ctx, docTask(ctx, group)); }
  }
  function docTask(ctx, group) {
    return async () => {
      if (gateActive(ctx.store, group.site, ctx.now)) { markRateLimited(ctx, group.units.slice(), group.site); return; }
      const res = await getJson('https://' + group.site + '/api/v1/models/' + encodeURIComponent(group.modelId), ctx, group.site);
      applyDoc(ctx, group, res);
    };
  }
  function applyDoc(ctx, group, res) {
    const first = !group.done;
    group.done = true;
    group.lastRes = res;
    if (first) observeRequest(ctx, res, group.site);   // 迟到加入的单元不再重复计数（一次请求就记一次）
    const units = group.units.slice();
    if (!res || !res.ok) {
      const st = statusFromFailure(res);
      group.status = st;
      for (const u of units) if (u.status === ST.PENDING) failUnit(ctx, u, st, res, group.site);
      return;
    }
    const body = res.json;
    if (!body || typeof body !== 'object' || Array.isArray(body)) {
      group.status = ST.ERROR;
      for (const u of units) if (u.status === ST.PENDING) failUnit(ctx, u, ST.ERROR, { status: res.status, error: '模型信息不是合法 JSON' }, group.site);
      return;
    }
    const errText = str(body.error || body.message);
    const notFound = body.notFound === true || /not\s*found|不存在/i.test(errText);
    // 认不出是模型文档（没 id / 没版本表 / 没名字）时按"取到的东西不对"处理，而不是谎报 404
    if (notFound || (num(body.id) == null && !Array.isArray(body.modelVersions) && !body.name)) {
      group.status = notFound ? ST.MISSING : ST.ERROR;
      for (const u of units) {
        if (u.status !== ST.PENDING) continue;
        failUnit(ctx, u, group.status, { status: notFound ? 404 : res.status, error: notFound ? errText : '返回体不是模型信息（' + (errText || '字段缺失') + '）' }, group.site);
      }
      return;
    }
    const doc = projectModelDoc(body, ctx.cfg.thumbWidth);
    group.doc = doc; group.status = ST.OK; group.channel = res.channel;
    persistGroup(ctx, group);
    for (const u of units) {
      if (u.status !== ST.PENDING) continue;
      applyDocToUnit(ctx, u, doc, res.channel);
      finishUnit(ctx, u, res.channel);
    }
  }
  function applyDocToUnit(ctx, unit, doc, channel) {
    Object.assign(unit.fields, pickModelFields(doc));
    unit.docCk = unit.docCk || docCkOf(unit.site, doc.modelId);
    if (channel) unit.channel = channel;
    const vid = str(unit.ref.versionId || '');
    if (!vid) {
      const v = (doc.versions || [])[0];
      if (v) {
        unit.versionPicked = 'first';
        Object.assign(unit.fields, v);
        unit.ref = Object.assign({}, unit.ref, { versionId: str(v.versionId) });
      } else unit.reason = '这个模型没有可展示的版本（modelVersions 为空）。';
      return;
    }
    const v = findVersionInDoc(doc, vid);
    if (v) { Object.assign(unit.fields, v); return; }
    if ((doc.versionIds || []).indexOf(vid) >= 0) unit.reason = '版本 ' + vid + ' 存在，但不在缓存保留的最新 ' + DEFAULTS.maxVersionsKept + ' 个版本详情里，文件大小/底模未补全。';
    else unit.reason = '版本 ' + vid + ' 不在模型 ' + str(doc.modelId) + ' 的版本表里（可能已删除，或不属于这个模型）。';
  }
  // 需要模型级字段：优先借缓存里的模型文档（一次请求都不发），否则并入文档组（同模型只发一次）
  function needDoc(ctx, unit, site, modelId) {
    const group = groupOf(ctx, site, modelId);
    if (group.status === ST.PENDING && !group.queued) {
      const sib = ctx.force ? null : cacheGet(ctx.store, group.ck, ctx.now, ctx.cfg);
      if (sib && !sib.stale && sib.entry.meta) {
        applyDocToUnit(ctx, unit, sib.entry.meta, str(sib.entry.channel));
        unit.borrowed = true;
        unit.cached = true;   // 一次请求都没发：这就是缓存命中
        if (!unit.from) unit.from = 'cache';
        finishUnit(ctx, unit, str(sib.entry.channel));
        return;
      }
    }
    attachToGroup(ctx, group, unit);
  }

  // ---- 429 记录 / 进度回调 ----
  function observeRequest(ctx, res, site) {
    ctx.summary.requested += 1;
    if (res && res.ok) ctx.summary.ok += 1;
    else ctx.summary.failed += 1;
    if (res && num(res.status) === 429 && site) {
      const g = gateMark(ctx.store, site, ctx.now, ctx.cfg);
      ctx.retryAt = num(g.until) || ctx.retryAt;
      ctx.summary.blockedUntil[site] = ctx.retryAt;
      // 门禁必须当场落地：下一轮调用（或重启后的 SW）要是读不到 gate，就会立刻再把站点打一遍
      scheduleFlush(ctx, true);
    }
    if (typeof ctx.opts.onProgress === 'function') {
      try { ctx.opts.onProgress({ done: ctx.summary.requested, planned: ctx.summary.planned, ok: ctx.summary.ok, failed: ctx.summary.failed }); } catch (_) { /* 回调异常不影响取数 */ }
    }
  }

  function queueTask(ctx, task) {
    if (ctx.tasks.length >= ctx.cfg.maxTasks) { ctx.summary.taskCap += 1; return false; }
    ctx.tasks.push(task);
    ctx.summary.planned += 1;
    if (typeof ctx.wake === 'function') { try { ctx.wake(); } catch (_) {} }
    return true;
  }

  // 只有版本ID 的行（典型是 /api/download/models/{vid}）：先打 942B 的 mini 反查 modelId
  function miniTask(ctx, unit) {
    queueTask(ctx, async () => {
      const site = unit.site;
      if (gateActive(ctx.store, site, ctx.now)) { markRateLimited(ctx, [unit], site); return; }
      const res = await getJson('https://' + site + '/api/v1/model-versions/mini/' + encodeURIComponent(unit.ref.versionId), ctx, site);
      observeRequest(ctx, res, site);
      if (!res.ok) { failUnit(ctx, unit, statusFromFailure(res), res, site); return; }
      const body = res.json;
      if (!body || typeof body !== 'object' || Array.isArray(body)) { failUnit(ctx, unit, ST.ERROR, { status: res.status, error: '版本信息不是合法 JSON' }, site); return; }
      unit.channel = res.channel;
      if (!unit.from) unit.from = 'network';
      Object.assign(unit.fields, projectVersionMeta(body, ctx.cfg.thumbWidth));
      if (!unit.fields.versionId) unit.fields.versionId = str(unit.ref.versionId);
      const modelId = str(body.modelId || (body.model && body.model.id) || '');
      if (!modelId) { finishUnit(ctx, unit, res.channel); return; }   // 反查不到 modelId：只有版本级字段 → partial
      unit.fields.modelId = modelId;
      unit.ref = Object.assign({}, unit.ref, { modelId });
      needDoc(ctx, unit, site, modelId);
    });
  }

  // ---- 图片：按站点分组、每 ≤cfg.imageIdsMax 个 id 一条请求（实测 ids= 批量可用） ----
  function queueImages(ctx, units) {
    const bySite = {};
    for (const u of units) {
      if (u.status !== ST.PENDING || u.kind !== 'image') continue;
      (bySite[u.site] = bySite[u.site] || []).push(u);
    }
    for (const site of Object.keys(bySite)) {
      for (const group of chunk(bySite[site], ctx.cfg.imageIdsMax)) {
        queueTask(ctx, async () => {
          if (gateActive(ctx.store, site, ctx.now)) { markRateLimited(ctx, group.slice(), site); return; }
          const ids = group.map((u) => u.ref.imageId);
          // 逗号不编码：实测口径就是 ?ids=1,2,3（encodeURIComponent 整个串会变成 %2C，
          // 站点虽然收，但日志/排查时读不出这批查了哪些 id）
          const url = 'https://' + site + '/api/v1/images?ids=' + ids.map((x) => encodeURIComponent(x)).join(',') + '&withMeta=true';
          const res = await getJson(url, ctx, site);
          observeRequest(ctx, res, site);
          if (!res.ok) { for (const u of group) failUnit(ctx, u, statusFromFailure(res), res, site); return; }
          const body = res.json && typeof res.json === 'object' ? res.json : {};
          const metadata = body.metadata && typeof body.metadata === 'object' ? body.metadata : {};
          const items = Array.isArray(body.items) ? body.items : [];
          for (const u of group) {
            const id = str(u.ref.imageId);
            const rec = metadata[id] || metadata[num(id)] || items.find((x) => x && str(x.id) === id) || null;
            // 记录缺席：匿名请求会把 NSFW/受限图整条清空（实测 imageId=145224191 → items/metadata 全空），
            // 这既可能是"需要登录态"也可能是"这个 id 不存在"，站点没给区分依据 → 按需要登录态标，不谎报"没有数据"。
            if (!rec) { failUnit(ctx, u, ST.NEEDS_LOGIN, { status: 200 }, site); continue; }
            u.channel = res.channel;
            Object.assign(u.fields, projectImage(rec, ctx.cfg.thumbWidth));
            finishUnit(ctx, u, res.channel);
          }
        });
      }
    }
  }

  // 并发上限的队列：任务运行中还能追加（mini→models 依赖链），空闲 worker 会接住新任务。
  async function drainQueue(ctx) {
    const conc = Math.max(1, Math.min(num(ctx.cfg.concurrency) || 1, 64));
    let cursor = 0;
    let active = 0;
    let waiters = [];
    const wake = () => { const w = waiters; waiters = []; for (const f of w) { try { f(); } catch (_) {} } };
    ctx.wake = wake;
    const worker = async () => {
      for (;;) {
        if (cursor >= ctx.tasks.length) {
          if (active === 0) return;                   // 没有待办、也没有在跑的：收工
          await new Promise((r) => { waiters.push(r); });  // 等依赖链追加任务
          continue;
        }
        const task = ctx.tasks[cursor++];
        active++;
        try { await task(); }
        catch (e) {
          ctx.summary.thrown += 1;
          try { if (console && console.warn) console.warn('[enrich] 任务异常（只影响这一条，整批继续）', (e && e.message) || e); } catch (_) {}
        } finally { active--; wake(); }
      }
    };
    const workers = [];
    for (let k = 0; k < conc; k++) workers.push(worker());
    await Promise.all(workers);
    ctx.wake = null;
  }

  function normalizeTargets(items) {
    const out = [];
    for (const it of Array.isArray(items) ? items : []) {
      const url = str(it && it.url);
      const tabId = num(it && (it.tabId != null ? it.tabId : it.id));
      const key = str(it && it.key) || (tabId != null ? 'tab:' + tabId : url);
      out.push({ key, url, tabId, windowId: num(it && it.windowId), title: str(it && it.title), ref: parseRef(url) });
    }
    return out;
  }

  function reasonNotCivitai(url) {
    const site = siteOf(url);
    if (!site) return '非 C 站地址，已跳过（不请求、不读取页面内容、不取 Cookie）：' + (str(url).slice(0, 80) || '空地址');
    return '这是 C 站的' + sectionHint(url) + '，不是模型/图片详情页，没有可补全的元数据（tabrows 已把它归到"未纳入提交"）。';
  }
  function sectionHint(url) {
    try {
      const p = new URL(url).pathname || '/';
      if (/^\/models(\/|$)/i.test(p)) return '模型列表页';
      if (/^\/images(\/|$)/i.test(p)) return '图片列表页';
      if (/^\/(search|user|articles|comics|crucibles|resources|shop)(\/|$)/i.test(p)) return '浏览页';
      return '页面';
    } catch (_) { return '页面'; }
  }

  function pickCfg(opts) {
    const out = {};
    const keys = ['ttlMs', 'missingTtlMs', 'hardAgeMs', 'maxEntries', 'maxBytes', 'concurrency', 'imageIdsMax', 'timeoutMs', 'thumbWidth', 'maxTasks'];
    for (const k of keys) { const v = num(opts[k]); if (v != null && v > 0) out[k] = v; }
    return out;
  }

  // ---- 主入口 ----
  // items：tabrows.build().rows / tabrows.others / CftTabs.candidates() 的输出 / chrome.tabs.Tab 都行（都要有 url）。
  async function enrich(items, options) {
    const opts = options || {};
    const now = num(opts.now) != null ? num(opts.now) : Date.now();
    const cfg = Object.assign({}, DEFAULTS, pickCfg(opts));
    const store = await loadStore();
    const ctx = {
      opts, cfg, now, store,
      force: opts.force === true,
      fetcher: typeof opts.fetcher === 'function' ? opts.fetcher : null,
      units: new Map(), docGroups: new Map(), tasks: [], retryAt: null, wake: null,
      doneUnits: 0, lastFlushCount: 0, lastFlushTs: 0, flushChain: null,
      summary: {
        total: 0, units: 0, skipped: 0, cached: 0, staleServed: 0, planned: 0, requested: 0, ok: 0, failed: 0,
        thrown: 0, taskCap: 0, dropped: 0, entries: 0, bytes: 0, persisted: null, sites: [], blockedUntil: {}, retryAt: null,
      },
    };
    const targets = normalizeTargets(items);
    ctx.summary.total = targets.length;
    const network = opts.network !== false;

    // 1) 归并成取数单元：同一 site+kind:id 只算一次（tabrows 合并出来的重复行天然共用一条）
    for (const t of targets) {
      if (!t.ref) {
        const ck = 'skip|' + (t.key || t.url || '?');
        let unit = ctx.units.get(ck);
        if (!unit) {
          unit = newUnit(ctx, ck, { kind: '', modelId: '', versionId: '', imageId: '', site: siteOf(t.url), origin: '', url: t.url, key: '' });
          unit.kind = '';
          unit.status = ST.SKIPPED;
          unit.reason = reasonNotCivitai(t.url);
        }
        pushTarget(unit, t);
        // skipped 计数交给第 5 步按 entry 累加（这里再 +1 会把"同一页开了两个标签"数成两条）
        continue;
      }
      const ck = ckOf(t.ref);
      let unit = ctx.units.get(ck);
      if (!unit) { unit = newUnit(ctx, ck, t.ref); ctx.summary.units += 1; }
      pushTarget(unit, t);
    }

    // 2) 新鲜命中 → 免请求；过期条目留作"失败时给陈旧值"的兜底
    for (const unit of ctx.units.values()) {
      if (unit.status === ST.SKIPPED) continue;
      const hit = ctx.force ? null : cacheGet(store, unit.ck, now, cfg);
      if (hit && !hit.stale) {
        unit.fields = Object.assign({}, hit.entry.meta);
        unit.status = hit.entry.status === ST.PENDING ? ST.OK : (hit.entry.status || ST.OK);
        unit.reason = str(hit.entry.reason);
        unit.channel = str(hit.entry.channel);
        unit.cached = true;
        unit.from = 'cache';
        unit.atCache = num(hit.entry.at) || 0;
        ctx.summary.cached += 1;
        continue;
      }
      if (hit && hit.stale) unit.stale = hit.entry;
      if (network) continue;                          // 要联网：让第 3 步去取新值，取不成再由 failUnit 回落到 stale
      if (unit.stale) {
        unit.fields = Object.assign({}, unit.stale.meta);
        unit.status = ST.STALE;
        unit.reason = '本轮没有联网（network:false），给的是 ' + new Date(unit.stale.at).toISOString() + ' 的陈旧缓存，可能已过期。';
        unit.from = 'cache-stale'; unit.staleUsed = true; unit.atCache = num(unit.stale.at) || 0;
        ctx.summary.staleServed += 1;
      } else {
        unit.status = ST.PENDING;
        unit.reason = '尚未取数（network:false 且没有缓存）：这一行的类型/大小/缩略图还没有值，别当成"没有数据"。';
      }
    }

    // 3) 排任务：图片批量 / 模型文档（同模型多版本共用一次）/ 只有版本ID 的先走 mini
    if (network) {
      const allUnits = [...ctx.units.values()];
      queueImages(ctx, allUnits);
      for (const unit of allUnits) {
        if (unit.status !== ST.PENDING || unit.kind === 'image') continue;
        const ref = unit.ref;
        if (ref.kind === 'model' || (ref.kind === 'version' && ref.modelId)) { needDoc(ctx, unit, ref.site, ref.modelId); continue; }
        if (ref.kind === 'version') { miniTask(ctx, unit); continue; }   // modelId 未知：942B 反查
      }
      await drainQueue(ctx);
      for (const unit of ctx.units.values()) {
        if (unit.status === ST.PENDING) failUnit(ctx, unit, ST.ERROR, { status: 0, error: '这条没排上任务（单批 ' + ctx.cfg.maxTasks + ' 条请求上限用满）' }, unit.site);
      }
    }

    // 4) 收尾落盘 + 淘汰。单元在取数过程中已由 persistUnit/persistGroup 增量写过了，这里只是补齐
    //    最后一段并等所有刷盘请求排干（避免并发的 set 把淘汰后的结果覆盖回旧内容）。
    for (const unit of ctx.units.values()) persistUnit(ctx, unit);
    for (const [, group] of ctx.docGroups) persistGroup(ctx, group);
    try { await (ctx.flushChain || Promise.resolve()); } catch (_) { /* 刷盘失败不影响本轮出参 */ }
    const openTabs = Array.isArray(opts.openTabIds) ? opts.openTabIds.map(Number).filter((x) => num(x) != null) : null;
    const ev = evict(store, ctx.now, openTabs, cfg);
    ctx.summary.dropped = ev.dropped;
    ctx.summary.entries = ev.kept;
    ctx.summary.bytes = ev.bytes;
    if (opts.persist !== false) {
      let saved = false;
      try { saved = await writeStore(store); } catch (_) { saved = false; }
      if (!saved) {                                 // 配额/写失败：削一半再试一次，缓存缺这一轮不算功能坏
        const keys = Object.keys(store.entries);
        for (let i = 0; i < Math.ceil(keys.length / 2); i++) delete store.entries[keys[i]];
        try { saved = await writeStore(store); } catch (_) { saved = false; }
      }
      ctx.summary.persisted = saved;
    }

    // 5) 出参
    const entries = []; const byKey = {}; const byCk = {}; const byTabId = {};
    for (const unit of ctx.units.values()) {
      const e = entryFromUnit(ctx, unit);
      entries.push(e);
      byCk[e.ck] = e;
      for (const k of e.keys) byKey[k] = e;
      for (const t of e.tabIds) byTabId[t] = e;
      const st = STATE_OF[e.status] || 'error';
      ctx.summary[st] = (ctx.summary[st] || 0) + 1;
      if (e.status === ST.PENDING || e.status === ST.RATE_LIMITED || e.status === ST.NEEDS_LOGIN || e.status === ST.ERROR) {
        const at = e.retryAt || ctx.retryAt || 0;
        if (at > (ctx.summary.retryAt || 0)) ctx.summary.retryAt = at;
      }
    }
    ctx.summary.sites = [...new Set(entries.map((e) => e.site).filter(Boolean))];
    for (const site of CIVITAI_HOSTS) {
      const g = gateActive(store, site, ctx.now);
      const until = num(g && g.until) || num(ctx.summary.blockedUntil[site]) || 0;
      if (until) {
        ctx.summary.blockedUntil[site] = until;
        if (!ctx.summary.retryAt || until > ctx.summary.retryAt) ctx.summary.retryAt = until;
      }
    }
    return { entries, byKey, byCk, byTabId, summary: ctx.summary };
  }

  function entryFromUnit(ctx, unit) {
    const ref = unit.ref || {};
    const hasFields = !!(unit.fields && Object.keys(unit.fields).length);
    const meta = hasFields ? buildMeta(ctx, unit) : null;
    const keys = (unit.keys && unit.keys.length) ? unit.keys.slice() : [unit.ck];
    return {
      key: keys[0],
      keys,
      ck: unit.ck,
      tabIds: unit.tabIds.slice(),
      url: unit.url || str(ref.url),
      site: unit.site || str(ref.site),
      kind: ref.kind || unit.kind || '',
      modelId: str(unit.fields.modelId || ref.modelId),
      versionId: str(unit.fields.versionId || ref.versionId),
      imageId: str(ref.imageId),
      status: unit.status,
      state: STATE_OF[unit.status] || 'error',
      submittable: meta ? meta.submittable : ((ref.kind === 'model' || ref.kind === 'version') && unit.status !== ST.MISSING),
      // meta 为 null（真没数据）时降级标记落在 entry 上，渲染层不用先去判 meta 是否存在
      needsLogin: unit.status === ST.NEEDS_LOGIN,
      exists: meta ? meta.exists : (unit.status === ST.MISSING ? false : null),
      meta,
      reason: unit.reason || '',
      retryAt: unit.retryAt || null,
      cached: !!unit.cached,
      staleUsed: !!unit.staleUsed,
      borrowed: !!unit.borrowed,
      from: unit.from || '',
      channel: unit.channel || '',
      at: ctx.now,
    };
  }

  // ---- 增量落盘：单元一完成就写进 store 并按节流刷到 chrome.storage.local ----
  // 为什么要增量：一批 50 个标签页要跑十几秒，MV3 SW 随时可能被回收；只在结尾统一写库等于"整批白跑"。
  function persistUnit(ctx, unit) {
    if (unit.status === ST.SKIPPED || unit.status === ST.PENDING || unit.status === ST.STALE) return;
    try {
      // 原样命中缓存的单元：只更新 LRU 的 last，不重置 at —— 否则"每 10 分钟打开一次小窗"会把
      // TTL 无限续期，缓存里的数据永远不过期（真实数据早就变了）。
      if (unit.cached && unit.from === 'cache') {
        const keep = ctx.store.entries[unit.ck];
        if (keep) { keep.last = ctx.now; return; }
      }
      const hasFields = !!(unit.fields && Object.keys(unit.fields).length);
      // 404 / 需要登录态 也要落一条：否则每次刷新都白打一遍（省流量，且降级理由稳定）
      if (!hasFields && unit.status !== ST.MISSING && unit.status !== ST.NEEDS_LOGIN) return;
      writeEntry(ctx.store, unit, ctx.now, buildMeta(ctx, unit), ctx.cfg);
    } catch (_) { /* 这条不入库，下次重新取 */ }
  }

  function persistGroup(ctx, group) {
    if (!group.doc) return;                    // 没拿到文档就别写，避免把失败态伪装成模型文档
    try {
      const tabIds = [];
      for (const u of group.units) for (const t of u.tabIds) if (tabIds.indexOf(t) < 0) tabIds.push(t);
      const meta = Object.assign(pickModelFields(group.doc), { versions: group.doc.versions });
      writeEntry(ctx.store, {
        ck: group.ck, site: group.site, kind: 'model-doc', url: 'https://' + group.site + '/models/' + group.modelId,
        tabIds, status: ST.OK, reason: '', channel: group.channel,
        ref: { kind: 'model', modelId: group.modelId, versionId: '', imageId: '', site: group.site, url: '' },
      }, ctx.now, meta, ctx.cfg);
    } catch (_) { /* 同上 */ }
  }

  // 刷盘节流：≥4 个单元完成 或 距上次 ≥1.5 秒才写一次；429 门禁这类"必须立刻落地"的强制写。
  function scheduleFlush(ctx, force) {
    if (ctx.opts.persist === false) return;
    ctx.doneUnits += 1;
    const sinceCount = ctx.doneUnits - (ctx.lastFlushCount || 0);
    const sinceTime = Date.now() - (ctx.lastFlushTs || 0);
    if (!force && sinceCount < 4 && sinceTime < 1500 && ctx.lastFlushTs) return;
    ctx.lastFlushCount = ctx.doneUnits;
    ctx.lastFlushTs = Date.now();
    ctx.flushChain = (ctx.flushChain || Promise.resolve())
      .then(() => writeStore(ctx.store))
      .then((saved) => { ctx.summary.persisted = saved; })
      .catch(() => {});
  }

  function writeEntry(store, unit, now, meta, cfg) {
    const ref = unit.ref || {};
    const prev = store.entries[unit.ck];
    const entry = {
      ck: unit.ck,
      site: unit.site || str(ref.site),
      kind: unit.kind || ref.kind || str(ref.kind),
      refId: ref.kind === 'image' ? ref.imageId : (ref.kind === 'version' ? ref.versionId : ref.modelId),
      url: unit.url || str(ref.url),
      at: now,
      last: now,
      ttl: ttlFor(unit.status, cfg),
      status: unit.status,
      reason: str(unit.reason).slice(0, 300),
      channel: unit.channel || '',
      tabIds: Array.isArray(prev && prev.tabIds) ? prev.tabIds.slice(0, DEFAULTS.maxTabIdsPerEntry) : [],
      meta,
    };
    for (const t of Array.isArray(unit.tabIds) ? unit.tabIds : []) attachTab(entry, t, entry.url);
    store.entries[unit.ck] = entry;
    return entry;
  }

  // ---- 行合并：tabrows.build() 的行上有 enriched:null 占位，就在这里填 ----
  function applyToRows(rows, result) {
    const byKey = (result && result.byKey) || {};
    const byCk = (result && result.byCk) || {};
    const byTabId = (result && result.byTabId) || {};
    for (const r of Array.isArray(rows) ? rows : []) {
      let e = byKey[r.key] || (r.tabId != null ? byTabId[r.tabId] : null) || null;
      if (!e) { const ref = parseRef(r.url); e = ref ? byCk[ckOf(ref)] : null; }
      if (!e) continue;
      r.enriched = e.status;
      r.state = e.state;
      r.meta = e.meta;
      r.thumb = (e.meta && e.meta.thumb) || '';
      r.submittable = r.kind === 'image' ? false : e.submittable;
      r.enrichReason = e.reason;
      r.enrichFrom = e.from;
      r.enrichCk = e.ck;
      if (e.modelId) r.enrichModelId = e.modelId;
      if (e.versionId) r.enrichVersionId = e.versionId;
    }
    return rows;
  }

  // 只读缓存（首屏立即渲染用）：等价 enrich(items, {network:false, persist:false})——
  // 不能写库，否则会把"陈旧"条目的时间刷成 now，等于把过期数据续命成有效数据。
  function peek(items, options) {
    return enrich(items, Object.assign({}, options, { network: false, persist: false }));
  }

  function clearCache() {
    return writeStore({ version: STORE_VERSION, savedAt: 0, entries: {}, gates: {} }).then(() => ({ ok: true }));
  }

  function pruneCache(options) {
    const opts = options || {};
    const now = num(opts.now) != null ? num(opts.now) : Date.now();
    const openTabs = Array.isArray(opts.openTabIds) ? opts.openTabIds.map(Number).filter((x) => num(x) != null) : null;
    return loadStore().then((store) => {
      const ev = evict(store, now, openTabs, Object.assign({}, DEFAULTS, pickCfg(opts)));
      if (opts.persist === false) return ev;
      return writeStore(store).then(() => ev);
    });
  }

  function readCache() {
    const now = Date.now();
    return loadStore().then((store) => {
      const gates = {};
      for (const site of Object.keys(store.gates || {})) {
        const g = store.gates[site];
        gates[site] = { until: num(g && g.until) || 0, tries: num(g && g.tries) || 0, active: gateActive(store, site, now) != null };
      }
      return { entries: store.entries, count: Object.keys(store.entries || {}).length, bytes: storeBytes(store.entries), gates };
    });
  }

  // 「信息补全 8 · 需登录 2 · 本站不存在 1」——过滤要可见，取数为什么失败也要可见
  function summaryText(s) {
    if (!s || !s.total) return '没有需要补全的标签页';
    const parts = ['信息补全 ' + (s.ready || 0) + '/' + s.total];
    if (s['needs-login']) parts.push('需登录态 ' + s['needs-login']);
    if (s.missing) parts.push('本站点不存在 ' + s.missing);
    if (s['rate-limited']) parts.push('被限流 ' + s['rate-limited']);
    if (s.error) parts.push('取数失败 ' + s.error);
    if (s.loading) parts.push('待补全 ' + s.loading);
    if (s.cached) parts.push('缓存命中 ' + s.cached);
    if (s.skipped) parts.push('跳过（非详情页） ' + s.skipped);
    if (s.requested) parts.push('本次联网 ' + s.requested + ' 次');
    if (s.staleServed) parts.push('其中陈旧值 ' + s.staleServed);
    if (s.retryAt) parts.push('可在 ' + new Date(s.retryAt).toLocaleTimeString() + ' 后重试');
    return parts.join(' · ');
  }

  root.CFT = root.CFT || {};
  root.CFT.enrich = {
    // 纯解析 / 投影（不联网，可单测）
    siteOf, parseRef, ckOf, docCkOf, chunk, thumbUrl,
    projectModelDoc, projectVersionMeta, projectImage, pickModelFields, findVersionInDoc, modelTypeOf, baseModelOf, fileMB,
    // 主入口
    enrich, peek, applyToRows,
    // 缓存与门禁
    readCache, pruneCache, clearCache, evict, cacheGet, gateActive, gateMark, DEFAULTS,
    // 词表 / 常量
    summaryText, STATUS: ST, ROW_STATES, STATE_OF, MODEL_LEVEL_FIELDS, STORE_KEY, STORE_VERSION, CIVITAI_HOSTS, MODEL_TYPE_MAP,
  };
})(globalThis);
