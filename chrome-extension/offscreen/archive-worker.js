// offscreen/archive-worker.js —— 归档编排器（P1+P2+P3+P4）
// 运行环境：offscreen document（有 DOM/fetch，无 showDirectoryPicker，不能调 chrome.downloads）。
// 写盘一律经 sendBg('writeFile') 发给 background；进度/弹窗一律经 sendContent 发给 content。
// 数据请求（模型详情/评论/评价/样图元数据）一律经 CFT.api.requestData，并由 withPageTransport
// 给当前任务装好「页面通道」fetcher：请求先在用户的模型页里同源发出（带登录 Cookie），页面不在/
// 不应答/超时/返回 ok:false 时才回落扩展直连（带 credentials:'include'）。油猴脚本时代这些请求本来就
// 在页面里发，移植后变成跨站匿名请求才出现 tRPC 401 与公开 API 空 items —— 见 lib 文件头 7)。
// 媒体二进制（封面/配图/模型文件）不走页面通道：只回文本且有 3MB 上限，且不需要登录态。
// 参考源：油猴脚本 saveCivitaiModelInfo（2098-2570）、downloadSingleImage（2694-2800）。
import '../lib/util.js';
import '../lib/civitai-api.js';
import '../lib/scheduler.js';
import '../lib/idb.js';
import '../lib/history.js';
import '../lib/config.js';
import '../lib/log.js';

const ASK_TIMEOUT_MS = 15000;   // 请求-答复超时：按用户取消处理
const FETCH_TIMEOUT_MS = 15000; // 单次 fetch 超时
const FETCH_RETRIES = 2;        // 媒体下载重试次数（不含首次）
const B64_CHUNK = 0x8000;       // arrayBuffer → base64 分段
const MODEL_TYPE_MAP = { 1: 'CheckPoint', 2: 'embedding', 3: 'HYPERNETWORK', 4: 'AESTHETIC GRADIENT', 5: 'Lora', 6: 'LyCORIS', 9: 'WILDCARDS' };

let _running = false;           // 防重入锁（单页归档）
let batch = null;               // 批量归档状态机（契约 8.2）；空闲为 null
const _pending = new Map();     // cid -> resolve（请求-答复等待表）

function newCid() {
  return String(Date.now()) + '.' + Math.random().toString(36).slice(2);
}

function clog(lvl, msg, err, ctx) {
  try { CFT.log.log(lvl, 'archive-worker', msg, err, ctx); } catch (_) { /* 日志失败不影响主流程 */ }
}

// ---- 消息收发 ----
function waitCid(cid, ms) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => { _pending.delete(cid); resolve(null); }, ms);
    _pending.set(cid, (v) => { clearTimeout(timer); resolve(v); });
  });
}

function resolveCid(cid, value) {
  const fn = _pending.get(cid);
  if (fn) {
    _pending.delete(cid);
    try { fn(value); } catch (_) { /* 忽略回调异常 */ }
    return true;
  }
  return false;
}

function sendBg(type, payload) {
  return chrome.runtime.sendMessage({ __cft: true, to: 'bg', type, payload: payload || {} });
}

function sendContent(tabId, type, payload) {
  return chrome.tabs.sendMessage(tabId, { __cft: true, to: 'content', type, payload: payload || {} });
}

// 请求-答复：payload 自动带 cid；超时或 content 不可达时 resolve(null)（按取消处理）
// timeoutMs 可覆盖等待上限（页面数据通道用的就是这一套请求-答复机制，只是超时跟着请求走）
function askContent(tabId, type, payload, timeoutMs) {
  if (tabId == null) return Promise.resolve(null);
  const cid = newCid();
  const ms = (Number(timeoutMs) > 0) ? Number(timeoutMs) : ASK_TIMEOUT_MS;
  const p = waitCid(cid, ms);
  sendContent(tabId, type, Object.assign({}, payload, { cid })).catch(() => {
    resolveCid(cid, null);
  });
  return p;
}

// ---- 页面数据通道（pageFetch）----
// 为什么必须有：油猴脚本时代所有 Civitai 数据请求都发生在页面里（同源 + 浏览器自动带登录
// Cookie），移植到 offscreen 后源变成 chrome-extension://、请求变匿名 → tRPC 401、公开 API
// 把受限结果清成 {"items":[]}。content 侧（model-page.js / image-badge.js）已实现 pageFetch
// 处理器（白名单 https + civitai.com|civitai.red + /api/v1/|/api/trpc/，credentials:'same-origin'，
// 3MB 上限），这里负责把它接进 lib 的传输层：
//   · 装 fetcher 的唯一入口是 CFT.api.setFetcher，lib 的 requestData 是唯一选通道的地方；
//   · withPageTransport(entries, label, fn) 给「当前任务」安装，fn 结束 finally 还原；
//     引用计数 + 共享 scope，所以批量阶段并发 5 个 item 时不会互相踩掉别人的 fetcher；
//   · 每个 item 用自己的 tabId（按页面 URL / 站点 host 匹配，见 ptPickTab）；
//   · 哪条通道真的拿到了数据记进 scope.stats + 每条产物的 [通道] 说明，不粉饰也不冤枉接口。
// 媒体（封面/配图/模型文件）不走这里：页面通道只回文本且有 3MB 上限，二进制仍走 fetchJson。
const PT_REPLY_SLACK_MS = 3000;      // 页面侧超时 + 消息往返余量；再不到就判「标签页不应答」
const PT_MAX_BODY_HINT = '3MB';      // 与 content 侧 PAGE_FETCH_MAX_BODY_CHARS 一致

let _ptScope = null;                 // 当前任务作用域（引用计数；最外层负责还原 fetcher）

// 只认白名单站点（复用 lib 的判定，避免这里再写一套 host 正则）；www. 前缀不参与比较
function ptHostOf(u) {
  const h = String((CFT.api && typeof CFT.api.civitaiHostFrom === 'function') ? CFT.api.civitaiHostFrom(u) : '');
  return h.replace(/^www\./i, '').toLowerCase();
}

function ptMakeEntry(entry) {
  const e = entry || {};
  const tabId = (e.tabId == null || e.tabId === '') ? null : Number(e.tabId);
  if (!Number.isFinite(tabId) || tabId < 0) return null;
  const pageUrl = String(e.pageUrl || '');
  return { tabId, pageUrl, host: ptHostOf(pageUrl) }; // host=='' → 站点未知的 tab（单图流），只能兜底借道
}

function ptAddEntries(scope, entries) {
  if (!scope) return;
  const list = Array.isArray(entries) ? entries : (entries ? [entries] : []);
  for (const raw of list) {
    const e = ptMakeEntry(raw);
    if (!e) continue;
    const hit = scope.entries.find((x) => x.tabId === e.tabId);
    if (hit) { if (!hit.pageUrl && e.pageUrl) { hit.pageUrl = e.pageUrl; hit.host = e.host; } continue; }
    scope.entries.push(e);
  }
}

// 选标签页：① 页面 URL 完全一致（批量里每个 item 用自己的 tab）② 同站点 host ③ 站点未知的 tab 兜底。
// host 对不上时宁可不借：从 civitai.red 的页面里发 civitai.com 的请求是跨站，页面侧
// credentials:'same-origin' 反而不带 Cookie，借了等于又退回匿名。
function ptPickTab(scope, url, pageUrlOrHost) {
  if (!scope) return null;
  const alive = scope.entries.filter((e) => e && e.tabId != null && !scope.dead.has(e.tabId));
  if (!alive.length) return null;
  const want = ptHostOf(pageUrlOrHost) || ptHostOf(url);
  if (!want) return alive[0].tabId;
  if (pageUrlOrHost) {
    const exact = alive.find((e) => e.pageUrl && String(e.pageUrl) === String(pageUrlOrHost) && ptHostOf(e.pageUrl) === want);
    if (exact) return exact.tabId;
  }
  const same = alive.find((e) => e.host === want);
  if (same) return same.tabId;
  const wild = alive.find((e) => !e.host);
  return wild ? wild.tabId : null;
}

// 交给 CFT.api.setFetcher 的 fetcher：发 pageFetch → 等同 cid 的 pageFetchResult。
// 契约返回形状 {ok, status, text}；不可用（无 tab / 不应答 / 结构异常）时抛错，由 lib 回落直连。
// 注意：ok:false 也照样把 status 带回去（401 必须由 lib 记到 page 这一路，不能被当成"没跑过"）。
function makePageFetcher(scope) {
  return async function pageFetcher(url, opts) {
    const o = opts || {};
    const timeoutMs = Math.max(1000, Number(o.timeoutMs) || FETCH_TIMEOUT_MS);
    const tabId = ptPickTab(scope, url, o.pageUrlOrHost);
    if (tabId == null) {
      // 区分「本来就没有 tab」和「tab 都在短路名单里」——后者是这次任务的页面通道挂过，不是没接
      const deadList = Array.from(scope.dead);
      scope.lastPageError = (scope.entries.length && deadList.length >= scope.entries.length)
        ? '已短路：标签页 ' + deadList.join(', ') + ' ' + (scope.deadReason.get(deadList[0]) || '之前未应答')
        : '不可用：没有与该请求站点匹配的标签页（页面通道未安装或站点不同）';
      throw new Error(scope.lastPageError);
    }
    if (scope.dead.has(tabId)) {
      throw new Error('已短路：标签页 ' + tabId + ' ' + (scope.deadReason.get(tabId) || '之前未应答'));
    }
    const ans = await askContent(tabId, 'pageFetch', { url, timeoutMs }, timeoutMs + PT_REPLY_SLACK_MS);
    if (!ans || typeof ans !== 'object') {
      // 标签页已关 / content 未接入：同一个 tab 不会第二次应答，本任务内直接短路，
      // 免得 5 组候选入参各等一次超时（批量 110 页会把时间全耗在等人不回的消息上）
      scope.dead.add(tabId);
      scope.deadReason.set(tabId, '超时未应答');
      scope.stats.noAnswer += 1;
      scope.lastPageError = '无应答（标签页 ' + tabId + ' 可能已关闭或页面未接入助手，' + timeoutMs + 'ms 超时）';
      throw new Error(scope.lastPageError);
    }
    const status = Number(ans.status) || 0;
    const body = typeof ans.body === 'string' ? ans.body : '';
    const truncated = ans.truncated === true;
    if (ans.ok === true && !truncated) {
      scope.stats.pageOk += 1;
      scope.stats.lastPageTransport = CFT.api.TRANSPORT_PAGE;
      return { ok: true, status: status || 200, text: body };
    }
    scope.stats.pageFail += 1;
    scope.lastPageError = ans.err ? String(ans.err)
      : (truncated ? '返回体超过 ' + PT_MAX_BODY_HINT + ' 上限被截断（JSON 不可用）'
        : '返回 ok=false（status ' + status + '）');
    // 状态原样回传：401 必须由 lib 记到 page 这一路（不能让它以为"页面通道没跑过"）
    return { ok: false, status, text: body, err: scope.lastPageError, truncated };
  };
}

function ptBegin(entries, label) {
  if (_ptScope) {
    _ptScope.depth += 1;
    ptAddEntries(_ptScope, entries);
    if (!_ptScope.installed && _ptScope.entries.length) {
      _ptScope.prevFetcher = (typeof CFT.api.setFetcher === 'function') ? CFT.api.setFetcher(makePageFetcher(_ptScope)) : null;
      _ptScope.installed = true;
    }
    return;
  }
  const scope = {
    label: String(label || ''), depth: 1, entries: [], dead: new Set(), deadReason: new Map(),
    lastPageError: '', installed: false, prevFetcher: null,
    stats: { pageOk: 0, pageFail: 0, noAnswer: 0, lastPageTransport: '' },
  };
  ptAddEntries(scope, entries);
  if (scope.entries.length && typeof CFT.api.setFetcher === 'function') {
    scope.prevFetcher = CFT.api.setFetcher(makePageFetcher(scope));
    scope.installed = true;
  } else {
    scope.lastPageError = scope.entries.length ? 'lib 未提供 setFetcher（版本不匹配），只能用扩展直连'
      : '任务没有可用标签页，页面通道未安装（全部请求走扩展直连）';
  }
  _ptScope = scope;
}

function ptEnd() {
  const scope = _ptScope;
  if (!scope) return;
  scope.depth -= 1;
  if (scope.depth > 0) return;
  if (scope.installed) {
    try { CFT.api.setFetcher(scope.prevFetcher || null); } // 还原默认（= 只剩带 Cookie 的直连）
    catch (e) { clog('warn', '还原默认 fetcher 失败', e); }
  }
  if (_ptScope === scope) _ptScope = null;
}

// 任务级安装：fn 全程可用页面通道，结束时（含抛错）必还原
async function withPageTransport(entries, label, fn) {
  ptBegin(entries, label);
  try { return await fn(); } finally { ptEnd(); }
}

// 页面通道的实话（供产物文本引用）
function pageTransportSummary() {
  const scope = _ptScope;
  if (!scope) {
    return { installed: false, active: false, pageOk: 0, pageFail: 0, noAnswer: 0, tabs: 0, lastPageError: '', deadTabs: [] };
  }
  return {
    installed: !!scope.installed, active: true, tabs: scope.entries.length,
    pageOk: scope.stats.pageOk, pageFail: scope.stats.pageFail, noAnswer: scope.stats.noAnswer,
    lastPageError: scope.lastPageError || '', deadTabs: Array.from(scope.dead), label: scope.label,
  };
}

function reportProgress(tabId, percent, text, level) {
  if (tabId == null) return Promise.resolve();
  const v = Math.max(0, Math.min(100, Number(percent) || 0));
  return sendContent(tabId, 'progress', { percent: v, text: text || '', level: level || 'info' }).catch(() => {});
}

function notifyDone(tabId, result) {
  if (tabId == null) return Promise.resolve();
  return sendContent(tabId, 'archiveDone', result).catch(() => {});
}

// ---- 二进制/文本编码 ----
function arrayBufferToB64(buf) {
  const bytes = new Uint8Array(buf);
  let s = '';
  for (let i = 0; i < bytes.length; i += B64_CHUNK) {
    s += String.fromCharCode.apply(null, bytes.subarray(i, i + B64_CHUNK));
  }
  return btoa(s);
}

function utf8ToB64(str) {
  return btoa(unescape(encodeURIComponent(String(str == null ? '' : str))));
}

// 媒体二进制下载（封面/配图/单图）：仍走扩展直连 fetchJson —— 页面通道只回文本且有 3MB 上限，
// 而且 CDN 资源不需要登录态（数据类接口才需要，见上面 PT 段）。
async function fetchArrayBuffer(url) {
  let lastErr = null;
  for (let attempt = 0; attempt <= FETCH_RETRIES; attempt += 1) {
    try {
      const resp = await CFT.api.fetchJson(url, { timeoutMs: FETCH_TIMEOUT_MS });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      return await resp.arrayBuffer();
    } catch (e) { lastErr = e; }
  }
  throw lastErr || new Error('fetch failed: ' + url);
}

async function fetchMediaB64(url) {
  return arrayBufferToB64(await fetchArrayBuffer(url));
}

async function writeFileToBg(dirName, fileName, dataB64) {
  const res = await sendBg('writeFile', { relPath: dirName + '/' + fileName, dataB64, overwrite: true });
  if (!res || res.ok !== true) throw new Error((res && res.msg) || 'writeFile 失败');
  return res;
}

// ---- 纯逻辑（脚本同语义，可单测） ----
function computeTotalUnits(hasCover, additionalCount, sendModel) {
  return (hasCover ? 1 : 0) + (Number(additionalCount) || 0) + 3 + (sendModel ? 1 : 0);
}

function buildExamplePayload(trainedWords, promptList) {
  return {
    triggerWord: '触发词：' + (trainedWords || '无'),
    samples: Array.isArray(promptList) ? promptList : [],
  };
}

// 归档目录名唯一规则（契约 §3：<safeFsName(模型名_版本)>）：单页与批量共用。
// 曾经单页用「所选文件名前缀」，同一模型两个入口落两个目录（重复目录 + 历史记录里的
// dirName 随入口漂移）；软件侧查重按 version_id，所以这只是目录一致性问题。
function archiveDirName(modelName, modelVer, modelVersionId) {
  const base = String(modelName || '').trim() || ('model_' + (modelVersionId || Date.now()));
  return CFT.util.safeFsName(base + '_' + String(modelVer || '').trim());
}

// 媒体候选选择（脚本 2285-2316：封面按 coverSaveMode；配图图片限 maxPics、视频全下）
function selectMedia(verItem, cfg, authImages) {
  const list = Array.isArray(authImages) ? authImages : [];
  const candidates = list.filter((it) => it && it.url);
  const coverImages = candidates.filter((it) => it.type === 'image');
  const coverVideo = candidates.find((it) => it.type === 'video') || null;
  const coverItem = (cfg.coverSaveMode === 'video')
    ? (coverVideo || coverImages[0] || candidates[0] || null)
    : (coverImages[0] || candidates[0] || null);
  const wantImage = cfg.downloadImages !== false;
  const wantVideo = cfg.downloadVideos !== false;
  const maxPics = Math.max(0, Number(cfg.maxPics) || 0);
  let additional = [];
  if (wantImage || wantVideo) {
    additional = candidates.filter((it) => it && it !== coverItem
      && ((it.type === 'image' && wantImage) || (it.type === 'video' && wantVideo)));
    let seen = 0;
    for (let i = 0; i < additional.length;) {
      if (additional[i].type === 'image') {
        seen += 1;
        if (seen > maxPics) { additional.splice(i, 1); continue; }
      }
      i += 1;
    }
  }
  return { candidates, coverItem, additional };
}

// 样图 generation data（脚本 2258-2280：image/video 类型、slice(0,20)、单图失败跳过）
// pageUrl 决定请求打到 civitai.com 还是 civitai.red（镜像站数据独立）——站点判定统一由
// CFT.api.civitaiBaseUrl(pageUrl) 负责（lib 里已改为公开 API，见 lib 文件头 5)。）。
// getImageGenerationData 返回的是「归一化 core」（不再是 tRPC 信封）：
//   {found, type, meta:<扁平生成数据>, reason?, resources, civitaiResources, modelVersionIds}
// example.json 的每条样本形状不变（prompt/negativePrompt/sampler/cfgScale/steps/Size）。
// 拿不到元数据的图（无密钥时公开 API 对 NSFW/受限图返回空 items）不静默丢弃，记进 gaps，
// 由归档 errors / 模型信息 txt 如实说明原因。
async function collectPromptList(verItem, pageUrl) {
  const rawImages = rawImagesOf(verItem);
  const authImages = rawImages.filter((it) => it && (it.type === 'image' || it.type === 'video'));
  const imageIds = authImages.map((img) => {
    if (img.id != null) return String(img.id);
    const seg = String(img.url || '').split('/').pop() || '';
    return String(seg).split('.')[0];
  }).filter((id) => /^\d+$/.test(id));
  const promptList = [];
  // transports/lastPageError：把「这条数据是哪条通道拿的」原样带出来（页面登录态 vs 扩展匿名直连），
  // 因为「空 items」在两种上下文里的含义完全不同，产物文本必须按当次实际情况说话
  const gaps = { noMeta: [], failed: 0, lastReason: '', lastErr: '', transports: {}, pageTried: 0, lastPageError: '' };
  const tally = (t) => { if (t) gaps.transports[t] = (gaps.transports[t] || 0) + 1; };
  for (const imageId of imageIds.slice(0, 20)) {
    try {
      const core = await CFT.api.getImageGenerationData(imageId, pageUrl);
      tally(core && core.transport);
      if (core && core.pageError) { gaps.pageTried += 1; gaps.lastPageError = String(core.pageError); }
      const meta = core && core.meta;
      if (core && core.found === true && meta && (core.type === 'image' || core.type === 'video')) {
        promptList.push({
          prompt: CFT.util.flattenPromptText(meta.prompt),
          negativePrompt: CFT.util.flattenPromptText(meta.negativePrompt),
          sampler: meta.sampler,
          cfgScale: meta.cfgScale,
          steps: meta.steps,
          Size: meta.Size,
        });
      } else {
        gaps.noMeta.push(String(imageId));
        gaps.lastReason = (core && core.reason) || 'no-meta';
      }
    } catch (e) {
      /* 单个样图失败不影响主流程 */
      gaps.failed += 1;
      gaps.lastErr = (e && e.message) || String(e);
    }
  }
  gaps.lastPageError = gaps.lastPageError || pageTransportSummary().lastPageError;
  return { authImages, promptList, gaps };
}

function rawImagesOf(verItem) {
  return Array.isArray(verItem && verItem.images) ? verItem.images : [];
}

// 样图元数据请求实际走的通道（不留空白让人猜"是没登录还是真没有"）
function describePromptTransports(gaps) {
  const g = gaps || {};
  const t = g.transports || {};
  const page = Number(t[CFT.api.TRANSPORT_PAGE]) || 0;
  const direct = Number(t[CFT.api.TRANSPORT_DIRECT]) || 0;
  if (!page && !direct) return '';
  const parts = [];
  if (page) parts.push('页面通道（借模型页同源、带你的登录状态）' + page + ' 次');
  if (direct) parts.push('扩展直连（chrome-extension 源，站点多半按匿名处理）' + direct + ' 次');
  let s = '；本次请求通道：' + parts.join(' + ');
  if (!page) s += '；页面通道未完成：' + (g.lastPageError || pageTransportSummary().lastPageError || '页面通道未安装');
  return s;
}

// example.json 少提示词的真实原因（绝不写成「该图没有提示词」）：
// 站点公开 API 在不带 API key 时对 NSFW/受限图直接返回 {"items":[]}（实测 imageId=145224191,
// nsfwLevel 4 → 空；SFW 的 145676249 → 完整 meta）。扩展没有也不申请 key，所以这部分样图的
// 提示词/参数注定拿不到，必须把原因摆到产物里，而不是留一个空 samples 让用户误判。
// 加了页面登录态通道之后这句话要说清「当次到底走的哪条通道」：带登录态仍空 items 和
// 压根没登录态（匿名直连）是两个不同的结论，不能都写成"公开 API 不给 NSFW"。
function promptGapNote(gaps, gotCount) {
  const g = gaps || { noMeta: [], failed: 0 };
  const noMeta = (g.noMeta || []).length;
  const failed = Number(g.failed) || 0;
  if (!noMeta && !failed) return '';
  const total = noMeta + failed + (Number(gotCount) || 0);
  const t = g.transports || {};
  const pageUsed = (Number(t[CFT.api.TRANSPORT_PAGE]) || 0) > 0;
  const parts = [];
  if (noMeta) {
    parts.push(noMeta + '/' + total + ' 张样图站点接口未返回生成数据'
      + '（' + (pageUsed
        ? '本次已借道页面通道、带你的登录状态，公开 API 仍返回空 items —— 说明这些图在登录态下也不公开元数据'
        : '公开 API 在不带 API key / 不带登录态时不返回 NSFW/受限图的元数据，实测返回 {"items":[]}'
          + (g.lastReason ? '，reason=' + g.lastReason : '')
          // 这句只对本会话实测过的匿名 curl/直连成立；带登录态时是否放开没有实测依据，所以不写"必然"
          + '；本扩展无 API key 且不做申请，且本次页面通道没有参与这些请求'
          + (g.lastPageError ? '（' + String(g.lastPageError) + '）' : ''))
      + '），因此这些样图的提示词/参数无法保存——这不代表这些图没有提示词');
  }
  if (failed) {
    parts.push(failed + '/' + total + ' 张样图元数据请求失败' + (g.lastErr ? '：' + g.lastErr : ''));
  }
  return parts.join('；') + '。已保存 ' + (Number(gotCount) || 0) + ' 条提示词样本'
    + describePromptTransports(g) + '。';
}

// 评论 + 评价（trpc 主通道 + DOM 兜底，失败不中断；返回拼接前的两段文本）
// 通道：lib 侧 requestData 先走页面（本页 tab，带登录态）、不成回落扩展直连；
// 只有确实被 401 判死的那一路会被熔断，所以这里报出的 gated 明细是「procedure + transport」。
async function fetchCommentsText(modelId, versionId, tabId, pageUrl) {
  const domFallbackFn = async () => {
    const ans = await askContent(tabId, 'requestDomComments', {});
    // content 侧回传 {cid, results:[{user,content,createdAt,reply,fromDom}]}
    if (ans && Array.isArray(ans.results)) return ans.results;
    return [];
  };
  const ptBefore = pageTransportSummary();
  const settled = await Promise.allSettled([
    CFT.api.fetchCivitaiComments(modelId, domFallbackFn, pageUrl),
    CFT.api.fetchCivitaiReviews(versionId, pageUrl),
  ]);
  // 熔断状态直接问 lib（别靠匹配文案）：哪条 procedure 的哪条通道已经因 401 停调，一目了然
  const gateState = typeof CFT.api.trpcUnauthorizedState === 'function' ? CFT.api.trpcUnauthorizedState() : {};
  const gates = Object.keys(gateState).map((k) => {
    const v = gateState[k] || {};
    return {
      key: k,
      procedure: v.procedure || String(k).split('|')[0],
      transport: v.transport || (String(k).indexOf('|page') > 0 ? CFT.api.TRANSPORT_PAGE : CFT.api.TRANSPORT_DIRECT),
      httpStatus: v.httpStatus,
    };
  });
  const ptAfter = pageTransportSummary();
  const commentsText = settled[0].status === 'fulfilled' ? (settled[0].value || '') : '';
  return {
    commentsText,
    reviewsText: settled[1].status === 'fulfilled' ? (settled[1].value || '') : '',
    gates,
    // 兼容旧字段名（值现在是 {procedure,transport} 明细）
    gatedProcedures: gates,
    dataTransport: CFT.api.textDataChannel ? CFT.api.textDataChannel(commentsText) : '',
    domCount: countDomCommentLines(commentsText),
    page: {
      pageOk: Math.max(ptAfter.pageOk - ptBefore.pageOk, 0),
      pageFail: Math.max(ptAfter.pageFail - ptBefore.pageFail, 0),
      noAnswer: ptAfter.noAnswer,
      installed: ptAfter.installed,
      tabs: ptAfter.tabs,
      lastPageError: ptAfter.lastPageError || '',
    },
    failures: [settled[0].reason, settled[1].reason]
      .filter((e) => e)
      .map((e) => ((e && e.message) || String(e))),
  };
}

// 降级说明（单页进 bag.errors，批量进 batch.errors；评论.txt 里的同名单已由 lib 写好）
// savedCount = 文本里真正落盘的评论条数；domCount = 其中来自页面抓取的部分。
// info.dataTransport 是「评论.txt 里实际写明的数据通道」——页面通道拿到数据时这里绝不能再
// 说"接口已被站点关闭"（那是匿名直连的结论），也没有降级可言，直接返回空串。
function commentsDegradeNote(gatedEntries, savedCount, info) {
  const api = CFT.api;
  const known = [api.COMMENTS_PROCEDURE, api.REVIEWS_PROCEDURE];
  // 允许传旧的 procedure 字符串数组（无通道信息时按「匿名直连被判死」处理）
  const gates = (Array.isArray(gatedEntries) ? gatedEntries : []).map((g) => {
    if (typeof g === 'string') return { procedure: g, transport: api.TRANSPORT_DIRECT };
    return { procedure: String((g && g.procedure) || ''), transport: (g && g.transport) || api.TRANSPORT_DIRECT };
  }).filter((g) => known.indexOf(g.procedure) >= 0);
  if (!gates.length) return '';
  const i = info || {};
  const n = Number(savedCount) || 0;
  const domOnly = n > 0 && Number(i.domCount || 0) === n;
  const viaPage = i.dataTransport === api.TRANSPORT_PAGE;
  if (viaPage && !domOnly) return ''; // 页面登录态通道把数据拿到了：没有降级，通道说明已写进 评论.txt
  const byProc = {};
  gates.forEach((g) => { (byProc[g.procedure] = byProc[g.procedure] || {})[g.transport] = true; });
  const detail = Object.keys(byProc).map((p) => {
    const chans = [];
    if (byProc[p][api.TRANSPORT_DIRECT]) chans.push('扩展匿名直连');
    if (byProc[p][api.TRANSPORT_PAGE]) chans.push('页面登录态通道');
    return p + '（' + chans.join(' + ') + ' 返回 401）';
  }).join('；');
  // 真因文案交给 lib：两路都 401 才允许说"接口已被站点关闭"
  const reason = api.trpcReasonText({
    directGated: gates.some((g) => g.transport === api.TRANSPORT_DIRECT),
    pageGated: gates.some((g) => g.transport === api.TRANSPORT_PAGE),
    pageTried: !!((i.page && (i.page.pageOk || i.page.pageFail || i.page.noAnswer)) || i.pageError),
    pageError: i.pageError || (i.page && i.page.lastPageError) || '',
    pageAvailable: !!(i.page && i.page.installed),
  });
  const tail = n > 0
    ? (domOnly
      ? '，已保存 ' + n + ' 条页面上已渲染的评论（每条带 [页面抓取] 标记），未加载出来的历史评论无法保存'
      : '，已保存 ' + n + ' 条评论（数据通道: ' + (i.dataTransport === api.TRANSPORT_DIRECT ? '扩展直连' : '未知') + '）')
    : '，本次未能保存任何评论（评价也没有页面抓取通道，页面抓取只覆盖评论区）';
  const advice = (n > 0 && !domOnly) ? '' : '归档前先在模型页展开评论区并等待加载完成，可多保存一些评论。';
  return reason + tail + '；已停调的接口: ' + detail
    + (i.pageError || (i.page && i.page.lastPageError) ? '；页面通道情况: ' + (i.pageError || i.page.lastPageError) : '')
    + '。' + advice;
}


function countCommentLines(commentsText) {
  if (!commentsText) return 0;
  return String(commentsText).split('\n').filter((l) => /^(\[页面抓取\]\s*)?【|^  ↳ 回复 【/.test(l)).length;
}

// 其中来自页面抓取（DOM 兜底）的条数——用来区分「存到了接口评论」和「只有页面渲染出来的评论」
function countDomCommentLines(commentsText) {
  if (!commentsText) return 0;
  return String(commentsText).split('\n').filter((l) => /^\[页面抓取\]\s*【/.test(l)).length;
}

// ---- 归档主流程 ----
// opts.batch=true：批量归档媒体阶段调用（契约 8.2）——
//   短路点：复用 payload.modelData 免重复 fetchModel；跳过 askArchiveConfirm / askFileSelection；
//   移除逐页 notifyArchiveDone（由批量结束统一重扫）；opts.skipComments 跳过 评论.txt（由批量阶段三统一写）。
//   返回 {ok, msg, dirName, errors}（单页流程原不取返回值，行为不变）。
// payload.dataTabId：只用于「数据请求借哪个标签页发」（页面通道），与 payload.tabId（弹窗/进度/DOM
// 兜底等 UI 通道）分开——批量里每个 item 都有自己的 tabId 可以借道拿登录态数据，但绝不该因此
// 给用户的页面弹进度或"归档完成"（契约 8.2：批量阶段无 content 交互）。
async function runArchive(payload, opts) {
  const p = payload || {};
  const raw = (p.dataTabId != null && p.dataTabId !== '') ? p.dataTabId
    : ((p.tabId != null && p.tabId !== '') ? p.tabId : null);
  const entries = [];
  if (raw != null && Number.isFinite(Number(raw))) entries.push({ tabId: Number(raw), pageUrl: String(p.pageUrl || '') });
  return withPageTransport(entries, 'archive:' + String(p.modelId || ''), () => runArchiveCore(p, opts));
}

async function runArchiveCore(p, opts) {
  const o = opts || {};
  const isBatch = o.batch === true;
  const skipComments = o.skipComments === true;
  const modelId = String(p.modelId || '');
  let modelVersionId = String(p.modelVersionId || '');
  const aboutText = String(p.aboutText || '');
  const pageUrl = String(p.pageUrl || '');
  const tabId = p.tabId;
  const errors = [];
  let dirName = '';
  let finished = false;
  let lastResult = null;
  const done = (ok, msg) => {
    if (finished) return;
    finished = true;
    lastResult = { ok, msg, dirName, errors };
    clog(ok ? 'info' : 'error', '归档结束: ' + msg, null, { modelId, modelVersionId, dirName });
    reportProgress(tabId, 100, msg, ok ? 'info' : 'error');
    notifyDone(tabId, { ok, dirName, msg, errors });
  };
  if (!modelId) { done(false, '缺少模型ID'); return lastResult; }
  let cfg = CFT.config.get();
  try { await CFT.config.load(); cfg = CFT.config.get(); }
  catch (e) { clog('warn', '配置加载失败，使用默认值(继续)', e); }
  try {
    if (!isBatch && modelVersionId) {
      let rec = null;
      try { rec = await CFT.history.get(modelId, modelVersionId); }
      catch (e) { clog('warn', '历史查询失败(继续)', e); }
      let lib = null;
      try { lib = await sendBg('checkLibrary', { versionId: modelVersionId }); }
      catch (e) { clog('warn', '模型库检查失败(继续)', e); }
      const hit = (rec && rec.archiveTime) ? rec : null;
      if (hit || (lib && lib.found)) {
        const record = hit || { modelId, modelVersionId, dirName: '', paths: (lib && lib.paths) || [] };
        const ans = await askContent(tabId, 'askArchiveConfirm', { record });
        if (!ans || ans.proceed !== true) { done(false, '用户取消重复归档'); return lastResult; }
        clog('info', '用户确认重复归档', null, { modelId, modelVersionId });
      }
    }
    let modelData = null;
    if (isBatch && p.modelData) {
      modelData = p.modelData; // 批量短路：prepare 阶段已 fetch，避免重复请求
    } else {
      try {
        // 数据类请求统一走 CFT.api.requestData（有页面通道先借页面登录态，回落扩展直连）
        const res = await CFT.api.requestData(
          CFT.api.civitaiBaseUrl(pageUrl) + '/api/v1/models/' + encodeURIComponent(modelId),
          { headers: { Accept: 'application/json' }, timeoutMs: FETCH_TIMEOUT_MS, pageUrlOrHost: pageUrl });
        if (!res.ok) { done(false, '[错误：访问模型信息接口失败] ' + (res.status || 0)); return lastResult; }
        modelData = CFT.api.parseJsonSafe(res.text);
        if (!modelData) { done(false, '模型信息不是合法 JSON（通道: ' + (res.transport || '?') + '）'); return lastResult; }
      } catch (e) { done(false, '模型信息请求失败: ' + ((e && e.message) || e)); return lastResult; }
      if (!modelData) { done(false, '模型信息为空'); return lastResult; }
    }
    if (modelData.message || modelData.error) {
      done(false, '模型接口返回错误：' + (modelData.message || modelData.error));
      return lastResult;
    }
    const modelName = String(modelData.name || ('model_' + modelId)).replace(/[/\\?%*:|"<>~]/g, '-');
    const modelTypeName = MODEL_TYPE_MAP[modelData.modelType] || modelData.type || '未分类';
    const versions = Array.isArray(modelData.modelVersions) ? modelData.modelVersions : [];
    if (!modelVersionId && versions.length === 1 && versions[0] && versions[0].id != null) {
      modelVersionId = String(versions[0].id); // 单版本兜底（脚本 2202）
    }
    if (!modelVersionId) { done(false, '未能识别当前模型版本ID'); return lastResult; }
    let verItem = null;
    if (isBatch && p.verItem) {
      verItem = p.verItem; // 批量短路：prepare 阶段已定位版本
    } else {
      verItem = versions.find((v) => String(v && v.id) === String(modelVersionId));
    }
    if (!verItem) { done(false, '[未匹配] 未能匹配到版本 ' + modelVersionId + ' 的数据（可能已下架或ID变更）'); return lastResult; }
    const modelVer = verItem.name || '';
    const files = Array.isArray(verItem.files) ? verItem.files : [];
    let modelFile = '';
    let modelNameVer = '';
    if (isBatch && p.modelFile) {
      // 批量短路：prepare 已选主文件（primary 优先否则 files[0]），跳过 askFileSelection
      modelFile = p.modelFile;
      modelNameVer = CFT.util.splitFilename(modelFile).name;
    } else if (files.length === 1) {
      modelFile = files[0].name || '';
      modelNameVer = CFT.util.splitFilename(modelFile).name.trimEnd();
    } else if (files.length > 1) {
      const sel = await askContent(tabId, 'askFileSelection', {
        files: files.map((f) => ({ name: f.name, size: f.size, type: f.type })),
      });
      const chosen = sel && sel.fileName ? files.find((f) => f.name === sel.fileName) : null;
      if (!chosen) { done(false, '用户取消文件选择'); return lastResult; }
      modelFile = chosen.name || '';
      modelNameVer = CFT.util.splitFilename(modelFile).name;
    } else {
      modelNameVer = modelName + '_' + modelVer;
      if (modelNameVer.slice(-1) === '.') modelNameVer = modelNameVer.substring(0, modelNameVer.length - 1);
    }
    if (isBatch && p.dirName) {
      dirName = String(p.dirName); // 批量短路：与 prepare 入队模型文件的目标子目录保持一致
    } else {
      dirName = archiveDirName(modelName, modelVer, modelVersionId);
    }
    // raw 仅做封面/配图文件名前缀（保持脚本原语义），目录名统一走 archiveDirName
    const raw = CFT.util.sanitizeFilename(modelNameVer).trim() || ('model_' + (modelVersionId || Date.now()));
    clog('info', '归档开始', null, { modelId, modelVersionId, dirName, modelFile });
    reportProgress(tabId, 0, '开始归档…', 'info');
    await runArchiveVersion({
      modelId, modelVersionId, modelName, modelVer, modelTypeName, modelFile,
      verItem, files, raw, dirName, aboutText, pageUrl, tabId, cfg, errors, done, modelData,
      isBatch, batchSkipComments: skipComments,
    });
  } catch (e) {
    clog('error', '归档流程异常', e, { modelId });
    done(false, '归档失败：' + ((e && e.message) || e));
  }
  return lastResult;
}

async function runArchiveVersion(bag) {
  const U = CFT.util;
  const st = { completed: 0, mediaFail: 0, modelError: null, commentCount: 0 };
  bag.st = st;
  const collected = await collectPromptList(bag.verItem, bag.pageUrl);
  bag.authImages = collected.authImages;
  bag.promptList = collected.promptList;
  // 提示词缺失就把「为什么缺」写进 errors/进度/模型信息 txt（不留空白让用户误判）
  bag.promptGapNote = promptGapNote(collected.gaps, bag.promptList.length);
  if (bag.promptGapNote) {
    bag.errors.push('[example] ' + bag.promptGapNote);
    clog('warn', '样图元数据不完整: ' + bag.promptGapNote, null, {
      modelId: bag.modelId, modelVersionId: bag.modelVersionId,
      noMeta: (collected.gaps && collected.gaps.noMeta) || [],
    });
    reportProgress(bag.tabId, 0, '部分样图无提示词（站点公开 API 未返回其元数据）', 'warn');
  }
  const media = selectMedia(bag.verItem, bag.cfg, collected.authImages);
  bag.coverItem = media.coverItem;
  bag.additional = media.additional;
  bag.candidates = media.candidates;
  // 批量模式下模型文件已由 prepare 阶段 sendModelToSoftware 入队，此处不重复发送（进度单位照旧补齐）
  const sendModel = (bag.cfg.sendModelToSoftware !== false) && !bag.isBatch;
  bag.totalUnits = computeTotalUnits(!!media.coverItem, media.additional.length, sendModel);
  bag.setP = (part, text) => {
    const v = Math.max(0, Math.min(1, Number(part) || 0));
    const overall = bag.totalUnits > 0 ? ((st.completed + v) / bag.totalUnits) * 100 : 0;
    reportProgress(bag.tabId, overall, text || '处理中', 'info');
  };
  const scheduler = CFT.scheduler.createDownloadScheduler({
    order: ['model', 'video', 'image', 'other'],
    concurrencyByType: { model: 1, video: 2, image: 3, other: 2 },
    globalMax: Math.max(1, Number(bag.cfg.globalConcurrency) || 4),
    onTaskDone: (t) => {
      if (t.status === 'error' && t.media) st.mediaFail += 1;
      st.completed += 1;
      bag.setP(0, (t.status === 'error' ? '失败 ' : '下载 ') + st.completed + '/' + bag.totalUnits);
    },
    onError: (type, err) => {
      const msg = (err && err.message) || String(err);
      bag.errors.push('[' + type + '] ' + msg);
      if (type === 'model' && !st.modelError) st.modelError = err;
      clog('error', '「' + type + '」任务失败: ' + msg, err);
    },
  });
  bag.scheduler = scheduler;
  enqueueModelTask(bag, sendModel);
  enqueueCoverTask(bag);
  enqueueMediaTasks(bag);
  // 附属文本文件失败不致命：此时调度器已在写同一目录，若异常冒泡到 runArchive 的 catch，
  // 归档会被判失败（且不记历史）而写入者仍在跑。失败只进 errors，进度单位补上。
  const writeAux = async (label, fn) => {
    try {
      await fn();
    } catch (e) {
      bag.errors.push('[' + label + '] 写入失败: ' + ((e && e.message) || e));
      clog('warn', '「' + label + '」写入失败(不阻断归档)', e);
      bag.st.completed += 1;
    }
  };
  await writeAux('info', () => writeInfoText(bag));
  await writeAux('example', () => writeExampleJson(bag));
  await writeAux('comments', () => writeCommentsFile(bag));
  try { await scheduler.waitAll(); } catch (_) { /* 单任务错误已由调度器计数 */ }
  // 诚实判定：模型文件失败绝不能记为成功（脚本 2499-2507）
  const stats = scheduler.stats();
  const modelFailed = ((stats.byType.model && stats.byType.model.fail) || 0) > 0;
  if (modelFailed) {
    const reason = (st.modelError && st.modelError.message)
      || (bag.errors.find((e) => e.indexOf('[model]') === 0)) || '未知原因';
    bag.done(false, '模型文件发送失败，不记为成功: ' + String(reason).slice(0, 200));
    return;
  }
  await finishArchiveSuccess(bag);
}

// 模型文件任务最先入队（OPT-06）
function enqueueModelTask(bag, sendModel) {
  if (sendModel && bag.modelFile) {
    // 下载用户实际选中的那个文件（按名匹配，与 askFileSelection/prepare 的 modelFile 同源）；
    // 名字没匹配上才回落 primary/首个。命中名字但无下载地址时不替换成别的文件，直接跳过。
    const chosenFile = bag.files.find((f) => f && f.name === bag.modelFile)
      || bag.files.find((f) => f && f.primary === true) || bag.files[0];
    if (chosenFile && chosenFile.downloadUrl) {
      bag.scheduler.enqueue({
        type: 'model', media: false,
        run: async () => {
          bag.setP(0, '发送模型文件到软件: ' + bag.modelFile);
          const res = await sendBg('sendModelToSoftware', { downloadUrl: chosenFile.downloadUrl, pageUrl: bag.pageUrl, destSubdir: bag.dirName });
          if (!res || res.ok !== true) throw new Error((res && res.msg) || '软件下载通道失败');
          bag.setP(1, '模型文件已发送');
        },
      });
      return;
    }
    clog('warn', '模型文件无下载地址，已跳过（进度计入）', null, { modelFile: bag.modelFile });
    bag.st.completed += 1;
    return;
  }
  bag.st.completed += 1; // 未开启或无文件时补进度单位，避免卡进度
}

// 封面任务（脚本 2402-2429 回退链：coverFallbackItems × getCivitaiImageUrlCandidates）
function enqueueCoverTask(bag) {
  const U = CFT.util;
  if (!bag.coverItem) return;
  const coverUrl = bag.coverItem.url;
  const coverExt = U.getSafeExtensionFromUrl(coverUrl, 'jpg');
  const coverType = U.isVideoExt(coverExt) ? 'video' : 'image';
  bag.scheduler.enqueue({
    type: coverType, media: true,
    run: async () => {
      bag.setP(0, '下载封面');
      const fileName = U.safeFsName(bag.raw + '.' + coverExt);
      let lastErr = null;
      const chain = [bag.coverItem].concat(
        bag.candidates.filter((it) => it && it.type === 'image' && it !== bag.coverItem));
      for (const item of chain) {
        for (const u of U.getCivitaiImageUrlCandidates(item.url)) {
          try {
            const b64 = await fetchMediaB64(u);
            await writeFileToBg(bag.dirName, fileName, b64);
            lastErr = null;
            break;
          } catch (e) { lastErr = e; }
        }
        if (!lastErr) break;
      }
      if (lastErr) throw lastErr;
      bag.setP(1, '封面已写入');
      clog('info', '封面已写入: ' + fileName);
    },
  });
}

// 配图/视频任务（脚本 2431-2448）
function enqueueMediaTasks(bag) {
  const U = CFT.util;
  let idx = 0;
  for (const item of bag.additional) {
    idx += 1;
    const n = idx;
    const mExt = U.getSafeExtensionFromUrl(item.url, 'jpg');
    const mType = U.isVideoExt(mExt) ? 'video' : 'image';
    bag.scheduler.enqueue({
      type: mType, media: true,
      run: async () => {
        bag.setP(0, '下载配图' + n);
        const name = U.safeFsName(U.buildMediaFilenameFromUrl(item.url, bag.raw + '_' + Date.now() + '_' + n, mExt));
        const b64 = await fetchMediaB64(item.url);
        await writeFileToBg(bag.dirName, name, b64);
        bag.setP(1, '配图' + n + '已写入');
      },
    });
  }
}

// 模型信息 txt（脚本 2234-2257）
async function writeInfoText(bag) {
  const U = CFT.util;
  const apiDesc = U.normalizeMultilineText(U.htmlToPlainTextFormatted(bag.modelData.description || ''));
  const verDesc = U.normalizeMultilineText(bag.aboutText || U.htmlToPlainTextFormatted(bag.verItem.description || ''));
  const textDesc = '\n\n-----模型访问地址------\n\n' + bag.pageUrl
    + '\n\n-----关于此版本------\n\n' + (verDesc || '无')
    + '\n\n-----模型介绍------\n\n' + (apiDesc || '无')
    + '\n\n-----其他参数------\n\n';
  const modelInfoJson = {
    modelType: bag.modelTypeName,
    description: textDesc,
    modelName: bag.modelName,
    modelVer: bag.modelVer,
    modelId: bag.modelId,
    modelFile: bag.modelFile || '',
    modelVersionId: bag.modelVersionId,
    modelUrl: bag.pageUrl,
  };
  // 样图提示词缺失时把真实原因写在「其他参数」里（example.json 的形状不动，只在信息文件补说明）
  if (bag.promptGapNote) modelInfoJson['examplePromptNote'] = bag.promptGapNote;
  bag.setP(0, '写入模型信息');
  await writeFileToBg(bag.dirName, U.safeFsName(bag.modelName + '.txt'),
    utf8ToB64(U.flattenObjectToPlainTextWithHtmlHandling(modelInfoJson)));
  bag.st.completed += 1;
}

// example.json（契约第 3 节格式）
async function writeExampleJson(bag) {
  bag.setP(0, '写入 example.json');
  const payload = buildExamplePayload(bag.verItem.trainedWords, bag.promptList);
  await writeFileToBg(bag.dirName, 'example.json', utf8ToB64(JSON.stringify(payload, null, 2)));
  bag.st.completed += 1;
}

// 评论.txt（任一段非空即写；拼接照脚本 2480-2488）
async function writeCommentsFile(bag) {
  // 批量模式跳过：评论由批量阶段三统一写（pure tRPC，页面可能已关），进度单位照常补齐
  if (bag.batchSkipComments) { bag.st.completed += 1; return; }
  let commentsText = '';
  let reviewsText = '';
  let gates = [];
  try {
    const got = await fetchCommentsText(bag.modelId, bag.modelVersionId, bag.tabId, bag.pageUrl);
    commentsText = got.commentsText;
    reviewsText = got.reviewsText;
    gates = got.gates || [];
    (got.failures || []).forEach((m) => bag.errors.push('[comments] 拉取异常: ' + m));
    bag.dataChannel = got.dataTransport || '';
    clog('info', '评论数据通道: ' + (bag.dataChannel || '无（未从接口取到数据）')
      + (got.page && got.page.installed ? '，页面通道已装(' + got.page.tabs + ' 个标签页, 成功 ' + got.page.pageOk + ' 次)' : '，本次无页面通道'));
  } catch (e) {
    clog('warn', '评论拉取失败(已跳过)', e);
    bag.errors.push('[comments] 拉取失败: ' + ((e && e.message) || e));
  }
  bag.st.commentCount = countCommentLines(commentsText);
  // 接口被 401 判死 → 除 评论.txt 里的说明外，再进一次 errors/日志，避免"文件没了/文件是空的"
  // 被读成"这个模型没人评论"（页面通道拿到数据时 commentsDegradeNote 自己会闭嘴）
  const degradeNote = commentsDegradeNote(gates, bag.st.commentCount, {
    dataTransport: CFT.api.textDataChannel ? CFT.api.textDataChannel(commentsText) : '',
    domCount: countDomCommentLines(commentsText),
    page: pageTransportSummary(),
    pageError: pageTransportSummary().lastPageError,
  });
  if (degradeNote) { bag.errors.push('[comments] ' + degradeNote); clog('warn', '评论/评价降级: ' + degradeNote); }
  if (commentsText || reviewsText) {
    try {
      bag.setP(0, '写入评论');
      let text = '';
      if (commentsText) text += commentsText;
      if (reviewsText) text += '\n\n' + reviewsText;
      await writeFileToBg(bag.dirName, '评论.txt', utf8ToB64(text));
    } catch (e) {
      clog('warn', '评论写入失败(已跳过)', e);
      bag.errors.push('[comments] 写入失败: ' + ((e && e.message) || e));
    }
  }
  bag.st.completed += 1;
}

async function finishArchiveSuccess(bag) {
  const picCount = bag.authImages.filter((m) => m && m.type === 'image').length;
  const videoCount = bag.authImages.filter((m) => m && m.type === 'video').length;
  const record = {
    modelId: String(bag.modelId),
    modelVersionId: String(bag.modelVersionId),
    modelName: bag.modelName,
    modelVer: bag.modelVer || '',
    modelType: bag.modelTypeName,
    modelUrl: bag.pageUrl,
    dirName: bag.dirName,
    archiveTime: new Date().toISOString(),
    modelFile: bag.modelFile || '',
    commentCount: bag.st.commentCount,
    imgCount: picCount,
    picCount,
    videoCount,
    hasModelFile: !!(bag.cfg.sendModelToSoftware !== false && bag.modelFile),
  };
  try {
    await CFT.history.put(record);
  } catch (e) {
    clog('error', '历史记录写入失败', e);
    bag.errors.push('[history] 写入失败: ' + ((e && e.message) || e));
  }
  try {
    const ok = await CFT.history.mirrorFile((arg) => sendBg('writeFile', {
      relPath: arg.relPath, dataB64: arg.dataB64, overwrite: true,
    }));
    if (!ok) bag.errors.push('[history] 镜像写入失败');
  } catch (e) {
    clog('warn', '历史镜像失败(不影响归档)', e);
    bag.errors.push('[history] 镜像失败: ' + ((e && e.message) || e));
  }
  // 批量模式不发逐页 notifyArchiveDone：由批量结束统一重扫（契约 8.2）
  if (!bag.isBatch) {
    try { await sendBg('notifyArchiveDone', { dirName: bag.dirName }); }
    catch (e) { clog('warn', '通知重扫失败(尽力而为)', e); }
  }
  const failNote = bag.st.mediaFail > 0 ? '（' + bag.st.mediaFail + ' 个媒体失败）' : '';
  bag.done(true, '归档完成：' + bag.dirName + failNote);
}

// ---- 单图流程（P3，适配脚本 downloadSingleImage 2694-2800） ----
// 单图同样需要登录态上下文：bg 的 saveSingleImage 会带上发起动作的 tabId（payload.tabId 或
// sender.tab.id），这里把它登记给页面通道；拿不到 tab 时静默回落扩展直连（行为与改造前一致）。
async function runSingleImage(payload) {
  const p = payload || {};
  const raw = (p.tabId != null && p.tabId !== '') ? Number(p.tabId) : NaN;
  const entries = Number.isFinite(raw) && raw >= 0 ? [{ tabId: raw, pageUrl: String(p.pageUrl || '') }] : [];
  return withPageTransport(entries, 'single-image:' + String(p.imageId || ''), () => runSingleImageCore(p));
}

async function runSingleImageCore(p) {
  const U = CFT.util;
  const imageId = String(p.imageId || '');
  const originalUrl = String(p.originalUrl || '');
  if (!imageId || !originalUrl) return { ok: false, msg: '参数缺失(imageId/originalUrl)' };
  let ext = U.sanitizeFilename(String(p.ext || '')).replace(/^\.+/, '');
  if (!ext) ext = U.getSafeExtensionFromUrl(originalUrl, 'jpg');
  // offscreen 无页面模型缓存，prefix 固定 'civitai_image'（脚本 2699 页缓存分支不适用）
  const prefix = 'civitai_image';
  const base = prefix + '_' + imageId;
  let currentMid = null;
  let currentVid = null;
  let metaToSave = null;
  let metaNote = '';
  try {
    // pageUrl 由 content/bg 附带时才能定站点；单图流缺省按 civitai.com（getCivitaiImageUrl 走 CDN 不受影响）
    // getImageGenerationData 现在返回归一化 core（公开 /api/v1/images），不再是 tRPC 信封
    const core = await CFT.api.getImageGenerationData(imageId, String(p.pageUrl || ''));
    if (!core) throw new Error('API 返回结构异常');
    if (core.found !== true) {
      // 站点公开 API 在不带 API key / 不带登录态时对 NSFW 或受限图返回空 items（实测 imageId=145224191）：
      // 这是"拿不到"不是"没有"，原图照样保存，但绝不编造 meta，也不写成"该图没有提示词"。
      // 通道写进说明：匿名直连拿不到 ≠ 接口没有，用户自己的登录态可能就是差一个同源请求。
      metaNote = '站点接口未返回该图生成数据（reason=' + (core.reason || 'unknown')
        + '；数据通道: ' + (core.transport === CFT.api.TRANSPORT_PAGE ? '页面登录态通道'
          : (core.transport === CFT.api.TRANSPORT_DIRECT ? '扩展直连（多半按匿名处理）' : '无'))
        + (core.pageError ? '；页面通道未完成: ' + core.pageError : '')
        + ')，已仅保存原图';
      clog('warn', '单图无生成数据: ' + metaNote, null, { imageId });
    } else {
      const refs = CFT.api.imageGenerationRefs(core);
      currentVid = refs.versionId;
      currentMid = refs.modelId;
      if (!currentMid && currentVid) {
        // 公开 API 的图片对象只给 modelVersionIds、不给 modelId；补一次 model-versions
        // （单图是用户逐张点的一次动作，多这一个请求可以接受；样图循环绝不做这件事）
        const ver = await CFT.api.getModelVersionInfo(currentVid, String(p.pageUrl || ''));
        if (ver && ver.modelId) currentMid = ver.modelId;
      }
      metaToSave = {
        type: core.type,
        modelId: currentMid,
        versionId: currentVid,
        meta: Object.assign({}, core.meta || {}, {
          prompt: U.flattenPromptText(core.meta && core.meta.prompt),
          negativePrompt: U.flattenPromptText(core.meta && core.meta.negativePrompt),
        }),
      };
    }
  } catch (e) {
    metaNote = '单图元数据获取失败: ' + ((e && e.message) || e);
    clog('warn', '单图元数据获取失败(继续下载原图)', e, { imageId });
  }
  const urls = U.getCivitaiImageUrlCandidates(originalUrl);
  let buf = null;
  let lastErr = null;
  for (const u of urls) {
    try { buf = await fetchArrayBuffer(u); lastErr = null; break; }
    catch (e) { lastErr = e; }
  }
  if (!buf) return { ok: false, msg: '图片下载失败: ' + ((lastErr && lastErr.message) || '未知错误') };
  const safeBase = U.safeFsName(base);
  try {
    await writeFileToBg('Images', safeBase + '.' + ext, arrayBufferToB64(buf));
  } catch (e) {
    clog('error', '单图写盘失败', e, { imageId });
    return { ok: false, msg: '写盘失败: ' + ((e && e.message) || e) };
  }
  if (metaToSave) {
    try {
      await writeFileToBg('Images', safeBase + '.json', utf8ToB64(JSON.stringify(metaToSave, null, 2)));
    } catch (e) {
      // JSON 失败仅记录，原图已保存仍算成功
      clog('warn', '单图 JSON 写入失败(原图已保存)', e, { imageId });
    }
  }
  let savedStateOk = true;
  try {
    const prev = (await CFT.idb.idbGet('savedImages')) || {};
    const map = (prev && typeof prev === 'object') ? prev : {};
    map[String(imageId)] = { time: Date.now() };
    await CFT.idb.idbSet('savedImages', map);
  } catch (e) {
    savedStateOk = false;
    clog('warn', 'SAVED 状态写入失败(图片已保存)', e, { imageId });
  }
  // 多标签页徽章同步：写盘与 idb 写入都成功后广播 savedImageDone，无监听器的 tab 逐个静默忽略
  if (savedStateOk) {
  try {
    const tabs = await chrome.tabs.query({ url: [
      'https://civitai.com/images/*', 'https://www.civitai.com/images/*',
      'https://civitai.red/images/*', 'https://www.civitai.red/images/*',
      'https://civitai.com/posts/*', 'https://www.civitai.com/posts/*',
      'https://civitai.red/posts/*', 'https://www.civitai.red/posts/*',
    ] });
    for (const t of tabs || []) {
      if (t && t.id != null) {
        try {
          await chrome.tabs.sendMessage(t.id,
            { __cft: true, to: 'content', type: 'savedImageDone', payload: { ok: true, imageId } });
        } catch (_) { /* 无监听器则静默 */ }
      }
    }
  } catch (e) {
    clog('warn', 'savedImageDone 广播失败(不影响保存结果)', e, { imageId });
  }
  }
  return { ok: true, msg: '图片已保存', metaSaved: !!metaToSave, metaNote: metaNote };
}

// ---- 批量归档状态机（契约 8.2；模块级 batch，与单页 _running 互斥） ----
// batch = { jobId, cancel, phase, total, results[], errors[], current }
// results 项：{tabId,url,modelId,modelVersionId,status,dirName,autoChosenFile?,msg?}
//   内部附 modelData/verItem/modelName/modelVer/modelFile/aboutText（阶段间复用，快照时剔除）。

function batchActive() {
  // 空闲(null) 或已完成(done) 视为不占用
  return !!(batch && batch.phase !== 'done');
}

// 自写并发池：items 逐个跑 limit 并发，fn(item, index)，fn 抛错仅终止本路 worker
async function runPool(items, limit, fn) {
  const list = Array.isArray(items) ? items : [];
  const concurrency = Math.max(1, Number(limit) || 1);
  let idx = 0;
  const total = list.length;
  const worker = async () => {
    while (idx < total) {
      const i = idx;
      idx += 1;
      await fn(list[i], i);
    }
  };
  const n = Math.min(concurrency, total);
  const workers = [];
  for (let w = 0; w < n; w += 1) workers.push(worker());
  await Promise.all(workers);
}

// 阶段 prepare 单页：fetchModel → 定位版本 → dirName → 多文件选主文件 → 已归档预检 → 模型文件入队
// 每个 item 用自己的 res.tabId 装页面通道（并发 5 个 item 共用同一个引用计数 scope，
// ptPickTab 按页面 URL / 站点 host 精确路由，绝不会 A 的请求借到 B 的 tab）
async function prepareBatchItem(res, cfg) {
  const modelId = String(res.modelId || '');
  const payloadVersionId = String(res.modelVersionId || '');
  const ptEntries = (res.tabId != null && Number.isFinite(Number(res.tabId)))
    ? [{ tabId: Number(res.tabId), pageUrl: String(res.pageUrl || '') }] : [];
  const modelData = await withPageTransport(ptEntries, 'batch-prepare:' + modelId, async () => {
    const resp = await CFT.api.requestData(
      CFT.api.civitaiBaseUrl(res.pageUrl || '') + '/api/v1/models/' + encodeURIComponent(modelId),
      { headers: { Accept: 'application/json' }, timeoutMs: FETCH_TIMEOUT_MS, pageUrlOrHost: res.pageUrl || '' });
    if (!resp.ok) throw new Error('[错误：访问模型信息接口失败] ' + (resp.status || 0)
      + '（通道: ' + (resp.transport || '无') + (resp.pageError ? '；页面通道: ' + resp.pageError : '') + '）');
    const parsed = CFT.api.parseJsonSafe(resp.text);
    if (!parsed) throw new Error('模型信息不是合法 JSON（通道: ' + (resp.transport || '?') + '）');
    return parsed;
  });
  if (!modelData) throw new Error('模型信息为空');
  if (modelData.message || modelData.error) throw new Error('模型接口返回错误：' + (modelData.message || modelData.error));
  const modelName = String(modelData.name || ('model_' + modelId)).replace(/[/\\?%*:|"<>~]/g, '-');
  const versions = Array.isArray(modelData.modelVersions) ? modelData.modelVersions : [];
  let versionId = payloadVersionId;
  if (!versionId && versions.length === 1 && versions[0] && versions[0].id != null) {
    versionId = String(versions[0].id); // 单版本兜底
  }
  if (!versionId) throw new Error('未能识别当前模型版本ID');
  const verItem = versions.find((v) => String(v && v.id) === String(versionId));
  if (!verItem) throw new Error('[未匹配] 未能匹配到版本 ' + versionId + ' 的数据（可能已下架或ID变更）');
  const modelVer = String(verItem.name || '');
  const files = Array.isArray(verItem.files) ? verItem.files : [];
  // 已归档预检：历史命中 → skip（不再入队/归档）
  try {
    const rec = (CFT.history && typeof CFT.history.get === 'function')
      ? await CFT.history.get(modelId, versionId) : null;
    if (rec && rec.archiveTime) {
      res.status = 'skip';
      res.dirName = String(rec.dirName || '');
      res.msg = '已归档（' + String(rec.archiveTime) + '）';
      return;
    }
  } catch (e) { clog('warn', '批量历史预检失败(按未归档继续)', e, { modelId, versionId }); }
  const dirName = archiveDirName(modelName, modelVer, versionId);
  res.dirName = dirName;
  const primaryFile = files.find((f) => f && f.primary === true) || files[0] || null;
  res.modelFile = primaryFile ? String(primaryFile.name || '') : '';
  if (files.length > 1 && primaryFile) res.autoChosenFile = res.modelFile; // 多文件自动选主文件
  // 阶段间复用，避免阶段 media 重复 fetch（契约 8.2）
  res.modelData = modelData;
  res.verItem = verItem;
  res.modelName = modelName;
  res.modelVer = modelVer;
  // 模型文件本体入队（软件侧下载到 <archive_root>/<destSubdir>/）
  if (primaryFile && primaryFile.downloadUrl && cfg.sendModelToSoftware !== false) {
    const sent = await sendBg('sendModelToSoftware', {
      downloadUrl: primaryFile.downloadUrl, pageUrl: res.pageUrl || '', destSubdir: dirName,
    });
    if (!sent || sent.ok !== true) throw new Error('软件下载通道失败: ' + ((sent && sent.msg) || '未知错误'));
  }
}

// 阶段 prepare（并发 5）
async function runPrepareStage(b) {
  let cfg = CFT.config.get();
  try { await CFT.config.load(); cfg = CFT.config.get(); }
  catch (e) { clog('warn', '配置加载失败，使用默认值(继续)', e); }
  let handled = 0;
  await runPool(b.results, 5, async (res) => {
    if (b.cancel) { res.status = 'failed'; res.msg = '已取消'; return; }
    try {
      await prepareBatchItem(res, cfg);
    } catch (e) {
      res.status = 'failed';
      res.msg = (e && e.message) || String(e);
      clog('warn', '批量准备失败', e, { modelId: res.modelId });
    }
    handled += 1;
    b.current = { phase: 'prepare', index: handled, total: b.total };
    try { await sendBg('archiveBadge', { text: handled + '/' + b.total }); } catch (_) { /* 忽略 */ }
  });
}

// 阶段 media（串行）：逐页 runArchive(batch 短路)，按结果更新 status
async function runMediaStage(b) {
  for (const res of b.results) {
    if (b.cancel) return;
    if (!res || res.status !== 'queued') continue;
    const payload = {
      modelId: res.modelId,
      modelVersionId: res.modelVersionId,
      aboutText: res.aboutText || '',
      pageUrl: res.pageUrl || '',
      dirName: res.dirName,
      modelData: res.modelData,
      verItem: res.verItem,
      modelFile: res.modelFile || '',
      // dataTabId：本行自己的标签页，只用来借页面登录态发数据请求（评论/样图元数据）。
      // 依然不带 tabId —— 批量不碰 content 的 UI 通道（askContent/reportProgress/notifyDone 保持 no-op，
      // 契约 8.2），否则 110 页批量会逐页弹进度和"归档完成"。
      dataTabId: (res.tabId != null) ? res.tabId : null,
    };
    try {
      const result = await runArchive(payload, { batch: true, skipComments: true });
      res.status = (result && result.ok) ? 'done' : 'failed';
      if (result && result.msg) res.msg = result.msg;
    } catch (e) {
      res.status = 'failed';
      res.msg = '归档失败：' + ((e && e.message) || e);
    }
    b.current = { phase: 'media', index: b.results.indexOf(res) + 1, total: b.total };
    clog('info', '批量媒体阶段完成: ' + res.status, null, { modelId: res.modelId, dirName: res.dirName });
  }
}

// 阶段 comments（串行）：对 done 页拉评论→写 评论.txt；DOM 兜底仍禁用（页面评论区多半没展开，
// 契约 8.2「纯 tRPC」），但**数据请求用本行的 tabId 借道页面**（runBatch 装的 scope 里
// ptPickTab 按 res.pageUrl 命中该 tab），这样登录用户的历史评论才拿得到。
async function runCommentsStage(b) {
  for (const res of b.results) {
    if (b.cancel) return;
    if (!res || res.status !== 'done') continue;
    const ptEntries = (res.tabId != null && Number.isFinite(Number(res.tabId)))
      ? [{ tabId: Number(res.tabId), pageUrl: String(res.pageUrl || '') }] : [];
    try {
      const got = await withPageTransport(ptEntries, 'batch-comments:' + String(res.modelId || ''),
        () => fetchCommentsText(res.modelId, res.modelVersionId, null, res.pageUrl || ''));
      const text = [got.commentsText, got.reviewsText]
        .filter((s) => s && String(s).length > 0)
        .join('\n\n');
      if (text) await writeFileToBg(res.dirName, '评论.txt', utf8ToB64(text));
      res.dataChannel = got.dataTransport || '';
      // 熔断是全批生效的：批级 errors 只报一次（逐文件的说明已经写在各自 评论.txt 里），
      // 免得 110 页刷出 110 行同义错误把 popup 状态区淹没
      const note = commentsDegradeNote(got.gates, countCommentLines(got.commentsText), {
        dataTransport: got.dataTransport,
        domCount: got.domCount,
        page: got.page,
        pageError: (got.page && got.page.lastPageError) || '',
      });
      if (note && !b.commentsGateNoted) {
        b.commentsGateNoted = true;
        clog('warn', '批量评论降级: ' + note);
        if (Array.isArray(b.errors)) {
          b.errors.push({
            modelId: res.modelId, modelVersionId: res.modelVersionId, dirName: res.dirName,
            msg: '评论/评价降级: ' + note + '（批量阶段不做页面抓取兜底，各模型的 评论.txt 已写明实际通道与真因）',
          });
        }
      }
    } catch (e) {
      b.errors.push({
        modelId: res.modelId, modelVersionId: res.modelVersionId, dirName: res.dirName,
        msg: '评论写入失败: ' + ((e && e.message) || e),
      });
      clog('warn', '批量评论写入失败(不阻断)', e, { modelId: res.modelId });
    }
    b.current = { phase: 'comments', index: b.results.indexOf(res) + 1, total: b.total };
  }
}

// 结束：清 badge + 统一触发一次重扫（bg 的 notifyArchiveDone 忽略 dirName 值，全库重扫）
async function finishBatch(b) {
  b.phase = 'done';
  b.current = null;
  try { await sendBg('archiveBadge', { text: '' }); } catch (_) { /* 忽略 */ }
  try { await sendBg('notifyArchiveDone', { dirName: '' }); } catch (_) { /* 忽略 */ }
}

// 批量主循环：三阶段，随时检查 cancel
// 整个 job 装一次页面通道（把所有 item 各自的 tabId 登记进去），三阶段共用、finally 必还原。
// 阶段内/单页再各自 withPageTransport 只是往同一个 scope 追加绑定（引用计数），不会互相覆盖。
async function runBatch(b) {
  const entries = (b.results || [])
    .filter((r) => r && r.tabId != null && Number.isFinite(Number(r.tabId)))
    .map((r) => ({ tabId: Number(r.tabId), pageUrl: String(r.pageUrl || '') }));
  try {
    await withPageTransport(entries, 'batch:' + String(b.jobId || ''), async () => {
      b.phase = 'prepare';
      await runPrepareStage(b);
      if (b.cancel) return;
      b.phase = 'media';
      await runMediaStage(b);
      if (b.cancel) return;
      b.phase = 'comments';
      await runCommentsStage(b);
    });
    await finishBatch(b);
  } catch (e) {
    clog('error', '批量归档流程异常', e, { jobId: b.jobId });
    b.phase = 'done';
    try { await sendBg('archiveBadge', { text: '' }); } catch (_) { /* 忽略 */ }
  }
}

// 阶段分发（测试钩子用：runBatchStage('prepare'|'media'|'comments', b)）
// 单独跑某个阶段时也要有页面通道（正常流程由 runBatch 统一安装），所以这里同样装一个 scope
async function runBatchStage(stage, b) {
  const tar = b || batch;
  if (!tar) return null;
  const run = () => {
    if (stage === 'prepare') return runPrepareStage(tar);
    if (stage === 'media') return runMediaStage(tar);
    if (stage === 'comments') return runCommentsStage(tar);
    if (stage === 'finish') return finishBatch(tar);
    return null;
  };
  if (stage === 'finish') return run();
  const entries = (tar.results || [])
    .filter((r) => r && r.tabId != null && Number.isFinite(Number(r.tabId)))
    .map((r) => ({ tabId: Number(r.tabId), pageUrl: String(r.pageUrl || '') }));
  return withPageTransport(entries, 'batch-stage:' + stage, run);
}

// 状态快照（契约 8.1/8.2；剔除内部 modelData/verItem 等大字段）
function getBatchState() {
  const empty = { running: false, jobId: '', phase: 'done', total: 0, done: 0, skipped: 0, failed: 0, current: null, results: [], errors: [], cancelRequested: false };
  if (!batch) return empty;
  const st = {
    running: batchActive(),
    jobId: batch.jobId,
    phase: batch.phase,
    total: batch.total,
    done: batch.results.filter((r) => r && r.status === 'done').length,
    skipped: batch.results.filter((r) => r && r.status === 'skip').length,
    failed: batch.results.filter((r) => r && r.status === 'failed').length,
    current: batch.current,
    errors: (batch.errors || []).slice(),
    cancelRequested: batch.cancel === true,
    results: batch.results.map((r) => ({
      tabId: r.tabId, url: r.url, modelId: r.modelId, modelVersionId: r.modelVersionId,
      status: r.status, dirName: r.dirName,
      autoChosenFile: r.autoChosenFile || '', msg: r.msg || '',
      dataChannel: r.dataChannel || '', // 评论实际走的通道（page/direct/''），供状态区如实显示
    })),
  };
  return st;
}
function handleMessage(msg, sender, sendResponse) {
  if (!msg || msg.__cft !== true || msg.to !== 'offscreen') return false;
  const type = msg.type;
  const payload = (msg.payload && typeof msg.payload === 'object') ? msg.payload : {};
  if (type === 'userSelection' || type === 'archiveConfirm' || type === 'domComments' || type === 'pageFetchResult') {
    // pageFetchResult 与 domComments 同一套 cid 请求-答复：content 侧既走 sendResponse（本函数收不到，
    // 由 askContent 的 sendMessage 链丢弃）也走 runtime 广播（这里消费），resolveCid 先到先得、天然去重
    if (payload.cid != null) resolveCid(payload.cid, payload);
    return false;
  }
  if (type === 'archive') {
    if (_running || batchActive()) {
      try { sendResponse({ ok: false, msg: '归档任务进行中' }); } catch (_) { /* 忽略 */ }
      return false;
    }
    _running = true;
    try { sendResponse({ ok: true, started: true }); } catch (_) { /* 忽略 */ }
    runArchive(payload)
      .catch((e) => { clog('error', '归档顶层异常', e); })
      .finally(() => { _running = false; });
    return false;
  }
  if (type === 'archiveBatch') {
    if (_running || batchActive()) {
      try { sendResponse({ ok: false, msg: '批量归档任务进行中' }); } catch (_) { /* 忽略 */ }
      return false;
    }
    const items = (payload.items && Array.isArray(payload.items)) ? payload.items : [];
    const results = [];
    for (const it of items) {
      if (!it) continue;
      results.push({
        tabId: it.tabId, url: String(it.url || ''), modelId: String(it.modelId || ''),
        modelVersionId: String(it.modelVersionId || ''), pageUrl: String(it.pageUrl || String(it.url || '')),
        aboutText: String(it.aboutText || ''), status: 'queued', dirName: '', msg: '',
      });
    }
    const jobId = String(payload.jobId || (Date.now().toString(36) + Math.random().toString(36).slice(2, 8)));
    batch = { jobId, cancel: false, phase: 'prepare', total: results.length, results, errors: [], current: null };
    try { sendResponse({ ok: true, started: true, jobId, total: results.length }); } catch (_) { /* 忽略 */ }
    runBatch(batch)
      .catch((e) => { clog('error', '批量归档顶层异常', e); })
      .finally(() => { /* batch 保留供 archiveBatchStatus 查询 */ });
    return false;
  }
  if (type === 'archiveBatchStatus') {
    let st;
    try { st = getBatchState(); } catch (_) { st = null; }
    try { sendResponse(st || { running: false, msg: '批量状态读取失败' }); } catch (_) { /* 忽略 */ }
    return false;
  }
  if (type === 'archiveBatchCancel') {
    if (batchActive()) {
      batch.cancel = true;
      try { sendResponse({ ok: true, msg: '已请求取消' }); } catch (_) { /* 忽略 */ }
    } else {
      try { sendResponse({ ok: false, msg: '无进行中的批量任务' }); } catch (_) { /* 忽略 */ }
    }
    return false;
  }
  if (type === 'saveSingleImage') {
    runSingleImage(payload).then(
      (res) => { try { sendResponse(res); } catch (_) { /* 忽略 */ } },
      (e) => { try { sendResponse({ ok: false, msg: '单图归档失败：' + ((e && e.message) || e) }); } catch (_) { /* 忽略 */ } });
    return true;
  }
  return false;
}

try {
  if (typeof chrome !== 'undefined' && chrome.runtime && chrome.runtime.onMessage) {
    chrome.runtime.onMessage.addListener(handleMessage);
  }
} catch (_) { /* 非扩展环境（node 自检）跳过监听注册 */ }

// node 冒烟自检钩子（生产无影响）
try {
  globalThis.__cftArchiveTest = {
    handleMessage, runArchive, runSingleImage,
    computeTotalUnits, buildExamplePayload, selectMedia, archiveDirName,
    newCid, waitCid, resolveCid,
    getBatchState, runBatchStage, runPool,
    prepareBatchItem, runPrepareStage, runMediaStage, runCommentsStage, finishBatch,
    // 页面数据通道（pageFetch）与通道文案
    askContent, withPageTransport, pageTransportSummary, ptPickTab, ptHostOf, makePageFetcher,
    fetchCommentsText, commentsDegradeNote, countDomCommentLines, promptGapNote, collectPromptList,
  };
} catch (_) { /* 忽略 */ }
