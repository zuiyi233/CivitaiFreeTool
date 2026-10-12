// offscreen/archive-worker.js —— 归档编排器（P1+P2+P3+P4）
// 运行环境：offscreen document（有 DOM/fetch，无 showDirectoryPicker，不能调 chrome.downloads）。
// 写盘一律经 sendBg('writeFile') 发给 background；进度/弹窗一律经 sendContent 发给 content。
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
function askContent(tabId, type, payload) {
  if (tabId == null) return Promise.resolve(null);
  const cid = newCid();
  const p = waitCid(cid, ASK_TIMEOUT_MS);
  sendContent(tabId, type, Object.assign({}, payload, { cid })).catch(() => {
    resolveCid(cid, null);
  });
  return p;
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
async function collectPromptList(verItem) {
  const rawImages = Array.isArray(verItem.images) ? verItem.images : [];
  const authImages = rawImages.filter((it) => it && (it.type === 'image' || it.type === 'video'));
  const imageIds = authImages.map((img) => {
    if (img.id != null) return String(img.id);
    const seg = String(img.url || '').split('/').pop() || '';
    return String(seg).split('.')[0];
  }).filter((id) => /^\d+$/.test(id));
  const promptList = [];
  for (const imageId of imageIds.slice(0, 20)) {
    try {
      const data = await CFT.api.getImageGenerationData(imageId);
      const core = data && data.result && data.result.data && data.result.data.json;
      const meta = core && core.meta;
      if (meta && (core.type === 'image' || core.type === 'video')) {
        promptList.push({
          prompt: CFT.util.flattenPromptText(meta.prompt),
          negativePrompt: CFT.util.flattenPromptText(meta.negativePrompt),
          sampler: meta.sampler,
          cfgScale: meta.cfgScale,
          steps: meta.steps,
          Size: meta.Size,
        });
      }
    } catch (_) { /* 单个样图失败不影响主流程 */ }
  }
  return { authImages, promptList };
}

// 评论 + 评价（trpc 主通道 + DOM 兜底，失败不中断；返回拼接前的两段文本）
async function fetchCommentsText(modelId, versionId, tabId) {
  const domFallbackFn = async () => {
    const ans = await askContent(tabId, 'requestDomComments', {});
    // content 侧回传 {cid, results:[{user,content,createdAt,reply,fromDom}]}
    if (ans && Array.isArray(ans.results)) return ans.results;
    return [];
  };
  const settled = await Promise.allSettled([
    CFT.api.fetchCivitaiComments(modelId, domFallbackFn),
    CFT.api.fetchCivitaiReviews(versionId),
  ]);
  return {
    commentsText: settled[0].status === 'fulfilled' ? (settled[0].value || '') : '',
    reviewsText: settled[1].status === 'fulfilled' ? (settled[1].value || '') : '',
  };
}

function countCommentLines(commentsText) {
  if (!commentsText) return 0;
  return String(commentsText).split('\n').filter((l) => /^(\[页面抓取\]\s*)?【|^  ↳ 回复 【/.test(l)).length;
}

// ---- 归档主流程 ----
// opts.batch=true：批量归档媒体阶段调用（契约 8.2）——
//   短路点：复用 payload.modelData 免重复 fetchModel；跳过 askArchiveConfirm / askFileSelection；
//   移除逐页 notifyArchiveDone（由批量结束统一重扫）；opts.skipComments 跳过 评论.txt（由批量阶段三统一写）。
//   返回 {ok, msg, dirName, errors}（单页流程原不取返回值，行为不变）。
async function runArchive(payload, opts) {
  const o = opts || {};
  const isBatch = o.batch === true;
  const skipComments = o.skipComments === true;
  const p = payload || {};
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
        const resp = await CFT.api.fetchJson(
          CFT.api.civitaiBaseUrl() + '/api/v1/models/' + encodeURIComponent(modelId),
          { headers: { Accept: 'application/json' }, timeoutMs: FETCH_TIMEOUT_MS });
        if (!resp.ok) { done(false, '[错误：访问模型信息接口失败] ' + resp.status); return lastResult; }
        modelData = await resp.json();
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
      const rawCalc = CFT.util.sanitizeFilename(modelNameVer).trim() || ('model_' + (modelVersionId || Date.now()));
      dirName = CFT.util.safeFsName(rawCalc);
    }
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
  const collected = await collectPromptList(bag.verItem);
  bag.authImages = collected.authImages;
  bag.promptList = collected.promptList;
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
  await writeInfoText(bag);
  await writeExampleJson(bag);
  await writeCommentsFile(bag);
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
    const primaryFile = bag.files.find((f) => f && f.primary === true) || bag.files[0];
    if (primaryFile && primaryFile.downloadUrl) {
      bag.scheduler.enqueue({
        type: 'model', media: false,
        run: async () => {
          bag.setP(0, '发送模型文件到软件: ' + bag.modelFile);
          const res = await sendBg('sendModelToSoftware', { downloadUrl: primaryFile.downloadUrl, pageUrl: bag.pageUrl, destSubdir: bag.dirName });
          if (!res || res.ok !== true) throw new Error((res && res.msg) || '软件下载通道失败');
          bag.setP(1, '模型文件已发送');
        },
      });
      return;
    }
    clog('warn', '模型文件无下载地址，已跳过（进度计入）');
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
  try {
    const got = await fetchCommentsText(bag.modelId, bag.modelVersionId, bag.tabId);
    commentsText = got.commentsText;
    reviewsText = got.reviewsText;
  } catch (e) {
    clog('warn', '评论拉取失败(已跳过)', e);
    bag.errors.push('[comments] 拉取失败: ' + ((e && e.message) || e));
  }
  bag.st.commentCount = countCommentLines(commentsText);
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
async function runSingleImage(payload) {
  const p = payload || {};
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
  try {
    const data = await CFT.api.getImageGenerationData(imageId);
    const core = data && data.result && data.result.data && data.result.data.json;
    if (!core) throw new Error('API 返回结构异常');
    if (Array.isArray(core.resources)) {
      const res = core.resources[0];
      if (res && res.modelId) currentMid = res.modelId;
      if (res && res.versionId) currentVid = res.versionId;
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
  } catch (e) {
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
  return { ok: true, msg: '图片已保存' };
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
async function prepareBatchItem(res, cfg) {
  const modelId = String(res.modelId || '');
  const payloadVersionId = String(res.modelVersionId || '');
  const resp = await CFT.api.fetchJson(
    CFT.api.civitaiBaseUrl() + '/api/v1/models/' + encodeURIComponent(modelId),
    { headers: { Accept: 'application/json' }, timeoutMs: FETCH_TIMEOUT_MS });
  if (!resp.ok) throw new Error('[错误：访问模型信息接口失败] ' + resp.status);
  const modelData = await resp.json();
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
  const dirName = CFT.util.safeFsName(modelName + '_' + modelVer);
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
      // 注意：不带 tabId —— batch 无 content 交互，askContent/reportProgress 自动 no-op
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

// 阶段 comments（串行）：对 done 页拉评论→写 评论.txt；DOM 兜底禁用（页面可能已关，tabId 传 null）
async function runCommentsStage(b) {
  for (const res of b.results) {
    if (b.cancel) return;
    if (!res || res.status !== 'done') continue;
    try {
      const got = await fetchCommentsText(res.modelId, res.modelVersionId, null);
      const text = [got.commentsText, got.reviewsText]
        .filter((s) => s && String(s).length > 0)
        .join('\n\n');
      if (text) await writeFileToBg(res.dirName, '评论.txt', utf8ToB64(text));
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
async function runBatch(b) {
  try {
    b.phase = 'prepare';
    await runPrepareStage(b);
    if (b.cancel) { await finishBatch(b); return; }
    b.phase = 'media';
    await runMediaStage(b);
    if (b.cancel) { await finishBatch(b); return; }
    b.phase = 'comments';
    await runCommentsStage(b);
    await finishBatch(b);
  } catch (e) {
    clog('error', '批量归档流程异常', e, { jobId: b.jobId });
    b.phase = 'done';
    try { await sendBg('archiveBadge', { text: '' }); } catch (_) { /* 忽略 */ }
  }
}

// 阶段分发（测试钩子用：runBatchStage('prepare'|'media'|'comments', b)）
async function runBatchStage(stage, b) {
  const tar = b || batch;
  if (!tar) return null;
  if (stage === 'prepare') return runPrepareStage(tar);
  if (stage === 'media') return runMediaStage(tar);
  if (stage === 'comments') return runCommentsStage(tar);
  if (stage === 'finish') return finishBatch(tar);
  return null;
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
    })),
  };
  return st;
}
function handleMessage(msg, sender, sendResponse) {
  if (!msg || msg.__cft !== true || msg.to !== 'offscreen') return false;
  const type = msg.type;
  const payload = (msg.payload && typeof msg.payload === 'object') ? msg.payload : {};
  if (type === 'userSelection' || type === 'archiveConfirm' || type === 'domComments') {
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
    computeTotalUnits, buildExamplePayload, selectMedia,
    newCid, waitCid, resolveCid,
    getBatchState, runBatchStage, runPool,
    prepareBatchItem, runPrepareStage, runMediaStage, runCommentsStage, finishBatch,
  };
} catch (_) { /* 忽略 */ }
