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

let _running = false;           // 防重入锁
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
async function runArchive(payload) {
  const p = payload || {};
  const modelId = String(p.modelId || '');
  let modelVersionId = String(p.modelVersionId || '');
  const aboutText = String(p.aboutText || '');
  const pageUrl = String(p.pageUrl || '');
  const tabId = p.tabId;
  const errors = [];
  let dirName = '';
  let finished = false;
  const done = (ok, msg) => {
    if (finished) return;
    finished = true;
    clog(ok ? 'info' : 'error', '归档结束: ' + msg, null, { modelId, modelVersionId, dirName });
    reportProgress(tabId, 100, msg, ok ? 'info' : 'error');
    notifyDone(tabId, { ok, dirName, msg, errors });
  };
  if (!modelId) { done(false, '缺少模型ID'); return; }
  let cfg = CFT.config.get();
  try { await CFT.config.load(); cfg = CFT.config.get(); }
  catch (e) { clog('warn', '配置加载失败，使用默认值(继续)', e); }
  try {
    if (modelVersionId) {
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
        if (!ans || ans.proceed !== true) { done(false, '用户取消重复归档'); return; }
        clog('info', '用户确认重复归档', null, { modelId, modelVersionId });
      }
    }
    let modelData = null;
    try {
      const resp = await CFT.api.fetchJson(
        CFT.api.civitaiBaseUrl() + '/api/v1/models/' + encodeURIComponent(modelId),
        { headers: { Accept: 'application/json' }, timeoutMs: FETCH_TIMEOUT_MS });
      if (!resp.ok) { done(false, '[错误：访问模型信息接口失败] ' + resp.status); return; }
      modelData = await resp.json();
    } catch (e) { done(false, '模型信息请求失败: ' + ((e && e.message) || e)); return; }
    if (!modelData) { done(false, '模型信息为空'); return; }
    if (modelData.message || modelData.error) {
      done(false, '模型接口返回错误：' + (modelData.message || modelData.error));
      return;
    }
    const modelName = String(modelData.name || ('model_' + modelId)).replace(/[/\\?%*:|"<>~]/g, '-');
    const modelTypeName = MODEL_TYPE_MAP[modelData.modelType] || modelData.type || '未分类';
    const versions = Array.isArray(modelData.modelVersions) ? modelData.modelVersions : [];
    if (!modelVersionId && versions.length === 1 && versions[0] && versions[0].id != null) {
      modelVersionId = String(versions[0].id); // 单版本兜底（脚本 2202）
    }
    if (!modelVersionId) { done(false, '未能识别当前模型版本ID'); return; }
    const verItem = versions.find((v) => String(v && v.id) === String(modelVersionId));
    if (!verItem) { done(false, '[未匹配] 未能匹配到版本 ' + modelVersionId + ' 的数据（可能已下架或ID变更）'); return; }
    const modelVer = verItem.name || '';
    const files = Array.isArray(verItem.files) ? verItem.files : [];
    let modelFile = '';
    let modelNameVer = '';
    if (files.length === 1) {
      modelFile = files[0].name || '';
      modelNameVer = CFT.util.splitFilename(modelFile).name.trimEnd();
    } else if (files.length > 1) {
      const sel = await askContent(tabId, 'askFileSelection', {
        files: files.map((f) => ({ name: f.name, size: f.size, type: f.type })),
      });
      const chosen = sel && sel.fileName ? files.find((f) => f.name === sel.fileName) : null;
      if (!chosen) { done(false, '用户取消文件选择'); return; }
      modelFile = chosen.name || '';
      modelNameVer = CFT.util.splitFilename(modelFile).name;
    } else {
      modelNameVer = modelName + '_' + modelVer;
      if (modelNameVer.slice(-1) === '.') modelNameVer = modelNameVer.substring(0, modelNameVer.length - 1);
    }
    const raw = CFT.util.sanitizeFilename(modelNameVer).trim() || ('model_' + (modelVersionId || Date.now()));
    dirName = CFT.util.safeFsName(raw);
    clog('info', '归档开始', null, { modelId, modelVersionId, dirName, modelFile });
    reportProgress(tabId, 0, '开始归档…', 'info');
    await runArchiveVersion({
      modelId, modelVersionId, modelName, modelVer, modelTypeName, modelFile,
      verItem, files, raw, dirName, aboutText, pageUrl, tabId, cfg, errors, done, modelData,
    });
  } catch (e) {
    clog('error', '归档流程异常', e, { modelId });
    done(false, '归档失败：' + ((e && e.message) || e));
  }
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
  const sendModel = bag.cfg.sendModelToSoftware !== false;
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
  try { await sendBg('notifyArchiveDone', { dirName: bag.dirName }); }
  catch (e) { clog('warn', '通知重扫失败(尽力而为)', e); }
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

// ---- 消息分发（to==='offscreen' 才处理） ----
function handleMessage(msg, sender, sendResponse) {
  if (!msg || msg.__cft !== true || msg.to !== 'offscreen') return false;
  const type = msg.type;
  const payload = (msg.payload && typeof msg.payload === 'object') ? msg.payload : {};
  if (type === 'userSelection' || type === 'archiveConfirm' || type === 'domComments') {
    if (payload.cid != null) resolveCid(payload.cid, payload);
    return false;
  }
  if (type === 'archive') {
    if (_running) {
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
  };
} catch (_) { /* 忽略 */ }
