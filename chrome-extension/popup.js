const ext = typeof browser !== 'undefined' ? browser : chrome;
const $ = id => document.getElementById(id);
// tabs-common.js 以 <script> 先于本文件加载，全局为 CftTabs；文件缺失时兜底成空对象，别让整窗白屏
const CT = globalThis.CftTabs || {};
// 依赖顺序 tabs-common → lib/tabrows.js → lib/enrich.js（见 lib/enrich.test.cjs 顶部契约）。
// 这两个模块是纯逻辑/纯取数，缺任一个都只该让"筛选"或"补全"降级，不该让列表消失。
const ROWS = globalThis.CftRows || {};
const ENRICH = (globalThis.CFT && globalThis.CFT.enrich) || {};
const BRIDGE = 'http://127.0.0.1:47531';
const TASK_KEY = 'cftExtTasks';   // 「发送到 CFT 之后」的跟踪快照：必须过 storage，小窗关掉再开还在
let tabs = [], rows = [], selected = new Set(), seenKeys = new Set(), firstRows = true, active = null, job = null, online = false, busy = false, refreshSerial = 0;
let built = { rows: [], others: [], summary: null }, enrichSummary = null, enriching = false;
let taskState = { at: 0, items: [] };
// cftState.reason: connecting | ok | offline | not-cft | too-old —— 三种「不在线」要给用户不同动作
let cftState = {ok:false, reason:'connecting', version:''};
const blockedSet = j => new Set([...(j?.completedKeys || []),...(j?.items || []).filter(r => ['sent','sending','waiting','uncertain'].includes(r.status)).map(r => r.key)]);
const blocked = () => blockedSet(job);
const getActiveTab = async () => (await ext.tabs.query({ active: true, currentWindow: true }))[0];
const STATUS_TEXT = {waiting:'等待提交',sending:'正在提交',sent:'已提交',failed:'未提交',uncertain:'待核对'};
// 桌面桥缺 Key 应答：webui.requires_civitai_key → {ok:false, requires_api_key:true, msg:'请先在设置 → 目录与账号填写并保存 Civitai API Key'}。
// bg 的 post() 目前只透传 msg（requires_api_key 字段被丢掉），所以文案匹配是当前主路径，字段判定是前向兼容。
const needsApiKey = r => !!(r && (r.requiresApiKey === true || r.requires_api_key === true || (typeof r.msg === 'string' && /api\s*key/i.test(r.msg) && /(请先|填写|设置|保存)/.test(r.msg))));
const jobNeedsKey = () => !!job && (job.items || []).some(needsApiKey);
const OFFLINE_HINT = '未连接 CFT：请打开桌面版 CivitaiFreeTool，打开后这里会自动重连（也可以点上方「刷新」）。「一键归档／批量归档」不依赖连接，离线会降级到浏览器下载目录，可照常使用。';
const KEY_HINT = '已连接 CFT，但软件里还没保存 Civitai API Key：请在 CFT「设置 → 目录与账号」填写并保存后再发送。归档功能不受影响，可以先归档。';
const KEY_ROW = '需要 API Key · 请在 CFT「设置 → 目录与账号」填写并保存 Civitai API Key';

// ---- storage 助手：chrome.storage 缺失/写失败都不能让界面崩（归档与发送本身不依赖它） ----
async function storeGet(key) {
  try {
    const s = ext.storage && ext.storage.local;
    if (!s || typeof s.get !== 'function') return null;
    const raw = await s.get(key);
    return raw ? raw[key] : null;
  } catch (_) { return null; }
}
async function storeSet(key, value) {
  try {
    const s = ext.storage && ext.storage.local;
    if (!s || typeof s.set !== 'function') return false;
    await s.set({ [key]: value });
    return true;
  } catch (_) { return false; }
}

function updateButtons() {
  const lock = busy || !!job?.running;
  const cur = $('sendCurrent'), batch = $('sendBatch');
  cur.disabled = lock || !online || !active || blocked().has(active.key);
  const count = rows.filter(r => selected.has(r.key) && !blocked().has(r.key)).length;
  batch.disabled = lock || !online || !count;
  batch.textContent = `发送选中 ${count} 项到 CFT`;
  $('selectionCount').textContent = `已选 ${count} / ${rows.length}`;
  // 一键归档：不依赖 CFT 在线，只要有当前模型页即可；批量归档同样不依赖在线（离线走浏览器下载降级）
  $('btnArchive').disabled = lock || !active;
  $('btnArchiveBatch').disabled = lock || !count;
  $('btnArchiveBatch').textContent = `🗂 批量归档选中 ${count} 页`;
  // 按钮被禁用时要说清「为什么」和「怎么办」；缺 Key 是提示态，不做成红色失败
  const needsKey = online && jobNeedsKey();
  const hint = !online ? OFFLINE_HINT : needsKey ? KEY_HINT : '';
  cur.title = batch.title = lock ? '上一批正在提交，请稍候' : hint;
  // #sendHint 在 popup.html 里；元素缺失时只跳过提示，别把整窗按钮逻辑带崩
  const el = $('sendHint');
  if (el) { el.textContent = hint; el.hidden = !hint; el.classList.toggle('warn', !online && !!hint); }
}

// ---- 筛选/排序：全部交给 CftRows（纯函数），popup 只负责把控件值翻译成 filters ----
function currentFilters() {
  const f = {};
  const q = $('search') ? $('search').value.trim() : '';
  if (q) f.q = q;
  const t = $('fType') ? $('fType').value : '';
  if (t) f.types = [t];
  const b = $('fBase') ? $('fBase').value : '';
  if (b) f.baseModels = [b];
  const s = $('fState') ? $('fState').value : '';
  if (s) f.states = [s];
  return f;
}
function visibleRows() {
  const f = currentFilters();
  let list = typeof ROWS.applyFilters === 'function' ? ROWS.applyFilters(rows, f) : rows.filter(r => !f.q || (r.title + ' ' + r.url).toLowerCase().includes(f.q.toLowerCase()));
  // 「是否可提交」不在 CftRows 的词表里（它是 tabrows+enrich 两段的结论），在这里补；缺省按可提交对待，
  // 否则联网还没回来时会整列表被"仅可提交"筛空 —— 那正是"看起来没数据"的旧毛病。
  const sub = $('fSub') ? $('fSub').value : '';
  if (sub === 'yes') list = list.filter(r => r.submittable !== false);
  else if (sub === 'no') list = list.filter(r => r.submittable === false);
  return typeof ROWS.sortRows === 'function' ? ROWS.sortRows(list, $('sort') ? $('sort').value : 'tab') : list;
}
// 列表 DOM 只取决于：可见行集合 + 筛选/排序控件 + 被占用（blocked）的 key + 是否有批次在跑。
// 轮询期间逐项 status 变化（waiting→sending→sent）不改变这个集合，所以用它做签名，避免每秒重建 500 行。
function listSignature() {
  const f = currentFilters();
  return [job?.running ? 1 : 0, [...blocked()].sort().join(','), visibleRows().map(r => r.key + ':' + (r.state || '') + ':' + (r.meta ? 1 : 0)).join(','),
    f.q || '', f.types || '', f.baseModels || '', f.states || '', ($('fSub') || {}).value || '', ($('sort') || {}).value || ''].join('#');
}
let seenListSig = '';

const MB = n => { const v = Number(n); return Number.isFinite(v) ? (Math.round(v / 104857.6) / 10) : null; };
const PCT = n => { const v = Number(n); return Number.isFinite(v) ? Math.max(0, Math.min(100, Math.round(v))) : 0; };
const CLOCK = ts => { try { return new Date(ts).toLocaleTimeString(); } catch (_) { return ''; } };

// 行尾那枚小徽章：三种"取不到数据"必须各说各话，绝不可塌成"没有数据"
function rowBadge(row) {
  const st = String(row.state || ''), en = String(row.enriched || '');
  if (st === 'needs-login') return { text: '需登录态', cls: 'needs-login' };
  if (st === 'rate-limited') return { text: '限流退避', cls: 'rate-limited' };
  if (st === 'missing') return { text: '本站不存在', cls: 'missing' };
  if (st === 'error') return { text: '取数失败', cls: 'error' };
  if (en === 'stale' || (row.meta && row.meta.staleUsed)) return { text: '旧缓存数据', cls: 'stale' };
  if (st === 'skipped') return { text: '未取数', cls: 'skipped' };
  if (st === 'loading' || !row.state) return { text: '待补全', cls: 'loading' };
  return null;
}
function rowMetaText(row) {
  const m = row.meta || {};
  const bits = ['ID ' + row.id];
  if (row.version) bits.push('版本 ' + row.version);
  if (m.versionName) bits.push('版本名 ' + m.versionName);
  if (m.type) bits.push(m.type);
  if (m.baseModel) bits.push(m.baseModel);
  if (m.totalMB != null) bits.push(m.totalMB + ' MB');
  else if (m.fileCount != null) bits.push(m.fileCount + ' 个文件');
  if (m.downloadCount != null) bits.push('下载 ' + m.downloadCount);
  if (row.copies > 1) bits.push('合并 ' + row.copies + ' 个标签');
  if (row.submittable === false) bits.push('不可提交');
  if (blocked().has(row.key)) bits.push('查看下方提交结果');
  return bits.join(' · ');
}
// enrich 的 reason 是给"人看真因"写的，整段很长；行内只放第一句（去掉 ISO 时间戳），
// 完整原因留在 title 里 —— 否则一行 reason 能把整列表挤走。
function shortReason(s) {
  const head = String(s || '').split(/[；\n]/)[0] || '';
  const clean = head.replace(/\s*到\s*20\d\d-\d\d-\d\dT[\d:.]+Z?/g, '').trim();
  return clean.length > 72 ? clean.slice(0, 72) + '…' : clean;
}
function render() {
  const list = $('tabList'), blockedKeys = blocked(); list.replaceChildren();
  const shown = visibleRows();
  for (const row of shown) {
    const label = document.createElement('label'); label.className = 'tab-row' + (row.submittable === false ? ' nosub' : '');
    const input = document.createElement('input'); input.type = 'checkbox'; input.dataset.key = row.key; input.checked = selected.has(row.key) && !blockedKeys.has(row.key); input.disabled = blockedKeys.has(row.key) || !!job?.running;
    input.addEventListener('change', () => {if(input.checked)selected.add(row.key);else selected.delete(row.key);seenKeys.add(row.key);syncSelection();});
    label.append(input);
    // 缩略图来自公开 API 的 image.civitai.com（enrich 已把 original 改写成 width=256）；
    // 取不到就把图摘掉，只留文字行 —— 留个空盒子会让人以为数据没加载出来
    if (row.meta && row.meta.thumb) {
      const img = document.createElement('img'); img.className = 'thumb'; img.alt = ''; img.loading = 'lazy'; img.src = row.meta.thumb;
      img.addEventListener('error', () => img.remove());
      label.append(img);
    }
    const text = document.createElement('span'), name = document.createElement('strong'), meta = document.createElement('small');
    name.textContent = (row.meta && row.meta.name) || row.title || ('ID ' + row.id);
    const badge = rowBadge(row);
    if (badge) { const b = document.createElement('span'); b.className = 'row-state ' + badge.cls; b.textContent = badge.text; name.append(b); }
    meta.textContent = rowMetaText(row);
    const why = shortReason(row.enrichReason);
    if (why) meta.textContent += ' · ' + why;
    label.title = row.url + (row.enrichReason ? '\n' + row.enrichReason : '');
    text.append(name, meta); label.append(text);
    list.append(label);
  }
  if (!shown.length) {
    const p = document.createElement('p'); p.className = 'empty';
    p.textContent = rows.length
      ? `当前筛选下没有匹配的模型页（列表里共 ${rows.length} 行，换一下类型/底模/补全状态或清空搜索）`
      : '没有打开的 C 站模型页：下面那行列出了各标签页被筛掉的原因和数量';
    list.append(p);
  }
  renderSummaries(shown.length);
  updateCurrentMeta();
  seenListSig = listSignature();
  syncSelection();
}
// 两行汇总互补：第一行说"筛选掉了什么、筛掉多少"，第二行说"补全为什么没成功"
function renderSummaries(shownCount) {
  const summaryEl = $('tabSummary');
  if (typeof ROWS.summaryText === 'function' && built.summary) {
    const base = ROWS.summaryText(built.summary);
    summaryEl.textContent = rows.length
      ? `${base} · 列表里 ${rows.length} 行，当前筛选显示 ${shownCount} 行`
      : `${base} · 没有可提交的模型页`;
  } else {
    summaryEl.textContent = `${rows.length} 个不同模型 / 版本；只读取模型页标题和链接，不读取网页内容。`;
  }
  const en = $('enrichSummary');
  if (!en) return;
  if (typeof ENRICH.summaryText !== 'function') { en.textContent = '信息补全模块未加载（lib/enrich.js 缺失），列表仍可用，只有标题和 ID。'; }
  else if (enrichSummary) en.textContent = ENRICH.summaryText(enrichSummary);
  else en.textContent = enriching ? '正在联网补全模型信息…（列表已按标题先显示，未补全的行标「待补全」）' : '';
  // 限流退避必须单独说清"这轮不再打站点"，否则用户会以为是我们卡住了
  const note = $('rateNote');
  if (note) {
    const retryAt = Number(enrichSummary && enrichSummary.retryAt) || 0;
    const sites = enrichSummary && enrichSummary.blockedUntil ? Object.keys(enrichSummary.blockedUntil) : [];
    if (retryAt && sites.length) {
      note.hidden = false;
      note.textContent = `站点 ${sites.join('、')} 触发限流，本轮不再请求（退避到 ${CLOCK(retryAt)}）。到时点「刷新」即可，列表里的旧数据仍照常显示。`;
    } else note.hidden = true;
  }
}
// 轮询路径用：数据真变了才重建列表，否则只刷新按钮态
function renderListIfChanged() { if (listSignature() !== seenListSig) render(); else syncSelection(); }
function syncSelection() {
  const enabled = visibleRows().filter(r => !blocked().has(r.key)), count=enabled.filter(r=>selected.has(r.key)).length;
  $('selectAll').checked = enabled.length > 0 && count===enabled.length;$('selectAll').indeterminate=count>0&&count<enabled.length;$('selectAll').disabled=!enabled.length||!!job?.running;
  updateButtons();
}
// 筛选项是从富化结果里长出来的：没富化时只有"全部"，补全回来后自动出现类型/底模
function syncFilterOptions() {
  for (const [id, pick] of [['fType', r => (r.meta || {}).type], ['fBase', r => (r.meta || {}).baseModel]]) {
    const el = $(id); if (!el) continue;
    const values = [...new Set(rows.map(pick).filter(v => v != null && v !== '').map(String))].sort((a, b) => a.localeCompare(b, 'zh-Hans-CN'));
    const keep = el.value;
    el.replaceChildren();
    const all = document.createElement('option'); all.value = '';
    all.textContent = id === 'fType' ? '全部类型' : '全部底模';
    el.append(all);
    for (const v of values) { const o = document.createElement('option'); o.value = v; o.textContent = v; el.append(o); }
    el.value = values.indexOf(keep) >= 0 ? keep : '';
    el.disabled = !values.length;
  }
}
function updateCurrentMeta() {
  if (!active || !$('modelUrl')) return;
  const row = rows.find(r => r.key === active.key);
  if (!row) return;
  const m = row.meta || {};
  const bits = [row.url];
  if (m.type) bits.push(m.type);
  if (m.baseModel) bits.push(m.baseModel);
  if (m.totalMB != null) bits.push(m.totalMB + ' MB');
  if (!row.meta) bits.push('信息待联网补全');
  $('modelUrl').textContent = bits.join(' · ');
}
// 连接指示只认真正的 CFT：CftTabs.cftHealth 校验 ok + app 标识 + 最低版本，别把端口上的别的服务当成本机软件
function showConnection() {
  const el = $('connection'), st = cftState, v = st.version ? 'v' + st.version : '';
  const min = CT.MIN_APP_VERSION || '2.6.25';
  const text = st.reason === 'ok' ? `CFT 已连接${v ? ' · ' + v : ''}`
    : st.reason === 'too-old' ? `CFT 版本过旧${v ? ' · ' + v : ''}（需 ≥ v${min}）`
    : st.reason === 'not-cft' ? `127.0.0.1:47531 上是别的服务${v ? ' · ' + v : ''}`
    : st.reason === 'connecting' ? '连接中…' : '未检测到 CFT';
  const advice = st.reason === 'ok' ? '发送与归档均可用'
    : st.reason === 'too-old' ? '请升级 CivitaiFreeTool：发送仍可用，但归档写入软件目录的通道需要新版本，旧版会降级到浏览器下载目录'
    : st.reason === 'not-cft' ? '本机 47531 端口被别的程序占用，或未启动 CFT：请关闭冲突程序并打开桌面版 CFT'
    : st.reason === 'connecting' ? '正在探测本机 CFT' : '请打开桌面版 CivitaiFreeTool；没打开时归档功能仍可用（浏览器下载降级）';
  el.textContent = text; el.title = advice;
  el.classList.toggle('connected', st.reason === 'ok');
  el.classList.toggle('warn', st.reason === 'too-old' || st.reason === 'not-cft');
}
async function health() {
  const c = new AbortController(), timer = setTimeout(() => c.abort(), 2500);
  let st = {ok: false, reason: 'offline', version: ''};
  try {
    const r = await fetch(BRIDGE + '/api/health', {signal: c.signal});
    const json = r.ok ? await r.json().catch(() => null) : null;
    st = typeof CT.cftHealth === 'function' ? CT.cftHealth(json, CT.MIN_APP_VERSION)
      : (json && json.ok === true ? {ok: true, reason: 'ok', version: String(json.version || '')} : {ok: false, reason: 'offline', version: ''});
  } catch (_) { st = {ok: false, reason: 'offline', version: ''}; }
  finally { clearTimeout(timer); }
  // too-old 依然是真 CFT，/api/download 可用 → 允许发送；not-cft / offline 一律视为未连接
  const wasOnline = online;
  online = st.reason === 'ok' || st.reason === 'too-old';
  cftState = st;
  showConnection(); updateButtons(); renderTasks();
  if (online && !wasOnline) pollTasks();
}
// 刷新保留用户的勾选决定：已决定过（选中或取消）的 key 原样保留，新出现的页默认选中，已关闭的页丢弃
function applyRows(next) {
  rows = next;
  const present = new Set(rows.map(r => r.key));
  if (firstRows) { selected = new Set(present); firstRows = false; }  // 首次加载：全选，与旧行为一致
  else {
    const kept = new Set();
    for (const k of selected) if (present.has(k)) kept.add(k);          // 丢弃已关闭标签页
    for (const r of rows) if (!seenKeys.has(r.key)) kept.add(r.key);     // 新页面默认选中
    selected = kept;
  }
  for (const r of rows) seenKeys.add(r.key);
  if (seenKeys.size > 4000) seenKeys = new Set(present);                 // 长期开关标签页的兜底上限
}
function buildRows(list) {
  // tabrows 缺失（脚本没加载成功）时退回 tabs-common 的旧口径，列表照常可用，只是没有汇总
  if (typeof ROWS.build === 'function') return ROWS.build(list);
  return { rows: typeof CT.candidates === 'function' ? CT.candidates(list) : [], others: [], summary: null };
}
// 首屏 → 缓存 → 联网三段：慢网络时先出标题（state=loading 的行留在列表里），补全回来再刷元数据
async function runEnrich(items, openTabIds, network) {
  if (typeof ENRICH.enrich !== 'function' || !items.length) return null;
  const options = { openTabIds };
  if (network) options.onProgress = (p) => {
    // 渐进反馈只在联网阶段做：一次文本更新，不重建列表，免得边取数边抖动
    if (enriching) {
      const el = $('enrichSummary');
      if (el && p && p.planned) el.textContent = `联网补全中 ${p.done}/${p.planned}（成功 ${p.ok}、失败 ${p.failed}）…`;
    }
  };
  try {
    const result = await (network ? ENRICH.enrich(items, options) : ENRICH.peek(items, options));
    return result || null;
  } catch (_) { return null; }   // 契约：enrich 不抛错；真抛了也只是没元数据，行必须还在
}
async function refresh() {
  const serial=++refreshSerial;
  try {
    const queried=await ext.tabs.query($('scope').value==='current'?{currentWindow:true}:{});
    if(serial!==refreshSerial)return;
    tabs=queried;
    built = buildRows(queried);
    applyRows(built.rows);
    render();
    const openTabIds = queried.map(t => t && t.id).filter(id => typeof id === 'number');
    const items = (built.rows || []).concat(built.others || []);
    // 先只读缓存（0 请求、不写库）：重开小窗时绝大多数行立刻就有元数据
    const peeked = await runEnrich(items, openTabIds, false);
    if(serial!==refreshSerial)return;
    if (peeked) {
      if (typeof ENRICH.applyToRows === 'function') { ENRICH.applyToRows(built.rows, peeked); ENRICH.applyToRows(built.others || [], peeked); }
      enrichSummary = peeked.summary || null;
      syncFilterOptions(); render();
    }
    if (typeof ENRICH.enrich !== 'function' || !items.length) return;
    enriching = true; render();
    const result = await runEnrich(items, openTabIds, true);
    if(serial!==refreshSerial){enriching=false;return;}
    enriching = false;
    if (!result) { render(); return; }
    if (typeof ENRICH.applyToRows === 'function') { ENRICH.applyToRows(built.rows, result); ENRICH.applyToRows(built.others || [], result); }
    enrichSummary = result.summary || null;
    syncFilterOptions(); render();
  }catch(_){$('feedback').textContent='读取标签页失败，请重新加载扩展';enriching=false;}
}
function mode(batch) {
  $('currentPanel').hidden=batch;$('batchPanel').hidden=!batch;
  for (const [id,on] of [['modeBatch',batch],['modeCurrent',!batch]]) {$(id).classList.toggle('active',on);$(id).setAttribute('aria-pressed',String(on));}
}
// 归档助手 2.0：设置区读取归档通道（带缓存，失败显示降级）—— 异步显示，不阻塞批量 UI
async function loadArchiveStatus() {
  const el = $('archiveStatus');
  try {
    const arch = await ext.runtime.sendMessage({ __cft: true, to: 'bg', type: 'getArchiveConfig', payload: {} });
    if (arch && arch.channel === 'software' && arch.archiveRoot) el.textContent = `归档通道（软件）：${arch.archiveRoot}`;
    else if (arch && arch.softwareOnline) el.textContent = '归档通道：软件在线但未设根目录，走浏览器下载目录（降级）';
    else el.textContent = '归档通道：浏览器下载目录（降级，软件未在线）';
  } catch (_) {
    el.textContent = '归档通道读取失败，本次走浏览器下载目录（降级）';
  }
  el.hidden = false;
}
// 一键归档：bg 接管采集+下发 offscreen；真实进度由页面内按钮展示，popup 只内联提示并 1s 后自动关闭
async function startArchive() {
  const btn = $('btnArchive'), note = $('archiveNote');
  btn.disabled = true;
  try {
    const tab = await getActiveTab();
    const r = await ext.runtime.sendMessage({
      __cft: true, to: 'bg', type: 'startArchive', payload: { tabId: tab && tab.id },
    });
    if (r && r.ok) {
      note.textContent = '归档已在页面内进行，请查看页面右下角按钮进度';
      note.hidden = false;
      setTimeout(() => window.close(), 1000);
    } else {
      note.textContent = (r && r.msg) || '归档启动失败';
      note.hidden = false;
      updateButtons();
    }
  } catch (e) {
    note.textContent = '归档启动失败，请重试';
    note.hidden = false;
    updateButtons();
  }
}
// 结果面板签名：job id + 是否在跑 + 每项状态。只有面板内容真变了才重建它的 DOM
function panelSignature(j) {
  if (!j) return '';
  const items = j.items || [];
  return [j.id ?? '', j.running ? 1: 0, items.length, items.map(r => (r.key || r.id || '') + ':' + r.status).join('|')].join('#');
}
let seenPanelSig = '';
function renderResultsPanel() {
  $('results').hidden = !job; $('clearResults').hidden = !job; $('clearResults').disabled = !!job?.running;
  if (!job) return;
  const items = job.items || [];
  const sent = items.filter(r=>r.status==='sent').length, uncertain = items.filter(r=>r.status==='uncertain').length, keyless = items.filter(needsApiKey).length;
  $('resultSummary').textContent = `${job.running?'正在提交':'提交结果'}：已提交 ${sent}/${items.length}` + (uncertain?` · ${uncertain} 项待核对`:'') + (keyless?` · ${keyless} 项缺 API Key`:'');
  $('resultList').replaceChildren();
  for (const r of items) {
    const p = document.createElement('p');
    const noKey = needsApiKey(r);
    p.className = 'result ' + (noKey ? 'needskey' : r.status);
    p.textContent = `ID ${r.id}：` + (noKey ? KEY_ROW : `${STATUS_TEXT[r.status] || r.status} · ${r.msg || ''}`);
    $('resultList').append(p);
  }
  if (!job.running) {
    $('results').open = true;
    $('feedback').textContent = keyless ? `${keyless} 项没提交是因为软件缺 API Key（不是提交失败）：请在 CFT「设置 → 目录与账号」填写并保存 Civitai API Key，再点「刷新」重新发送。归档不受影响。` : '“已提交”仅表示 CFT 接收链接，下面的“发送到 CFT 之后”才是软件里的解析/下载状态。';
  }
}
function renderJob(next) {
  const sig = panelSignature(next), panelChanged = sig !== seenPanelSig;
  job = next; seenPanelSig = sig;
  if (panelChanged) renderResultsPanel();
  renderListIfChanged();
}
async function status() {try {const r=await ext.runtime.sendMessage({type:'cft-batch-status'});if(r.ok)renderJob(r.job);}catch(_) {}}

// ===== 发送到 CFT 之后：读 CFT 自己的解析/下载状态（复用桥已有的 /api/rpc 反射，不新增端点） =====
const CFT_ST = { pending: '排队中', downloading: '下载中', retrying: '重试中', done: '已完成', paused: '已暂停', error: '失败', canceled: '已取消' };
const TERMINAL = new Set(['done', 'failed', 'library']);
const MID_RE = /\/models\/([1-9]\d*)/i;
const modelIdOf = u => { const m = MID_RE.exec(String(u || '')); return m ? m[1] : ''; };
const sameTarget = (a, b) => {
  const x = String(a || ''), y = String(b || '');
  if (!x || !y) return false;
  if (x === y) return true;
  const mx = modelIdOf(x), my = modelIdOf(y);
  return !!mx && mx === my;
};
// 只发送用户勾选过的链接；这里记的是"发出去了哪些"，用于之后问 CFT 下得怎么样
function recordSubmission(items) {
  const list = (items || []).filter(Boolean);
  if (!list.length) return;
  const now = Date.now();
  const byKey = new Map((taskState.items || []).map(it => [it.key, it]));
  for (const raw of list) {
    const row = rows.find(r => r.key === raw.key) || {};
    const key = raw.key || row.key || raw.url;
    const prev = byKey.get(key);
    const versionId = String(raw.version || row.enrichVersionId || (row.meta && row.meta.versionId) || '');
    byKey.set(key, {
      key, url: raw.url || row.url || '', modelId: String(raw.id || row.id || modelIdOf(raw.url || '') || ''),
      versionId, name: (row.meta && row.meta.name) || raw.title || row.title || ('ID ' + (raw.id || '')),
      sent: raw.status === 'sent', msg: raw.msg || '',
      // 已经跑完的跟踪结论保留（CFT 侧后来居上也不该把"已完成"抹成"未知"），未完成的清空重来
      cft: prev && prev.cft && TERMINAL.has(prev.cft.phase) ? prev.cft : null,
      checkedAt: 0,
    });
  }
  taskState = { at: now, items: [...byKey.values()].slice(-60) };
  renderTasks();
  persistTasks();
}
async function persistTasks() { await storeSet(TASK_KEY, taskState); }
async function loadTasks() {
  const saved = await storeGet(TASK_KEY);
  if (saved && Array.isArray(saved.items)) {
    taskState = { at: Number(saved.at) || 0, items: saved.items.filter(it => it && it.url).slice(-60) };
  }
  renderTasks();
}
async function clearTasks() {
  taskState = { at: 0, items: [] };
  await storeSet(TASK_KEY, taskState);
  renderTasks();
}
async function rpc(method, args) {
  const ctrl = new AbortController(), timer = setTimeout(() => ctrl.abort(), 6000);
  try {
    const r = await fetch(BRIDGE + '/api/rpc', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ method, args: args || [] }), signal: ctrl.signal,
    });
    if (!r.ok) return { ok: false, error: 'CFT 返回 HTTP ' + r.status };
    const json = await r.json().catch(() => null);
    if (!json || typeof json !== 'object') return { ok: false, error: 'CFT 应答格式异常' };
    if (json.error) return { ok: false, error: String(json.error) };
    return { ok: true, result: json.result };
  } catch (_) {
    return { ok: false, error: '查询失败：CFT 未应答（可能刚被关闭）' };
  } finally { clearTimeout(timer); }
}
function tasksOfItem(item, list) {
  const mid = String(item.modelId || '') || modelIdOf(item.url);
  if (!mid) return [];
  return (list || []).filter(t => t && t.url && modelIdOf(t.url) === mid);
}
function parseOfItem(item, state) {
  const items = (state && state.items) || [];
  for (let i = items.length - 1; i >= 0; i--) if (sameTarget(items[i].url, item.url)) return items[i];
  return null;
}
// 一个提交项 → 现在到底到哪一步了。注意 CFT 的 _parse_state 只保留"最近一次解析"，
// 多链接提交时后面的会覆盖前面的，所以解析信息只在这条确实是最近一次时才当证据用。
function cftOfItem(item, list, state) {
  const mine = tasksOfItem(item, list);
  const p = parseOfItem(item, state);
  if (p && p.ok === false) {
    return { phase: 'failed', terminal: true, text: 'CFT 解析后没有入队：' + (p.msg || '未知原因') + (p.paid ? '（付费/抢先体验，需要时在软件里选「仍要下载」）' : '') };
  }
  if (mine.length) {
    const fin = mine.filter(t => ['done', 'error', 'canceled'].includes(t.status));
    const live = mine.filter(t => !['done', 'error', 'canceled'].includes(t.status));
    const done = mine.filter(t => t.status === 'done');
    const head = mine.length > 1 ? `${mine.length} 个文件任务：` : '';
    const parts = [];
    if (done.length) parts.push(`已完成 ${done.length}`);
    if (live.length) parts.push(live.map(t => `${CFT_ST[t.status] || t.status}${t.status === 'downloading' ? ' ' + PCT(t.progress) + '%' : ''}`).join('、'));
    const err = mine.find(t => t.status === 'error');
    const bad = mine.filter(t => t.status === 'error').length;
    if (bad) parts.push(`失败 ${bad}`);
    const run = live[0] || done[0] || mine[0];
    let text = head + parts.join(' · ');
    if (run && run.status === 'downloading') {
      // speed / downloaded / total 都是字节口径（web/app.js 的 t.speed/1048576 同源），别再乘一次
      const a = MB(run.downloaded), b = MB(run.total);
      text += ` · ${a != null && b != null ? a + '/' + b + ' MB' : ''}${run.speed ? ' · ' + MB(run.speed) + ' MB/s' : ''}`;
    }
    if (mine.some(t => t.hash_mismatch)) text += ' · 注意：SHA256 校验不一致，去 CFT 里核对';
    if (err) text += ' · 失败原因：' + String(err.error || '').slice(0, 90);
    if (run) text += ` · 文件：${run.filename || ''}`;
    if (run && run.dest_dir) text += ` · 落盘：${run.dest_dir}`;
    else if (run) text += ' · 落盘目录在开始下载时才解析';
    const terminal = fin.length === mine.length && mine.length > 0;
    return { phase: terminal ? (done.length ? 'done' : 'failed') : 'downloading', terminal, text };
  }
  if (item.cft && item.cft.phase === 'library') return item.cft;
  if (p && p.ok) return { phase: 'queued', terminal: false, text: 'CFT 已解析并回报入队：' + (p.msg || '已加入队列'), waiting: true };
  return { phase: 'waiting', terminal: false, text: 'CFT 的下载队列里还没有这个模型的任务：可能仍在等文件选择（多文件版本要在软件里挑），或提交还没落到软件', waiting: true };
}
function summarizeTasks(list, queried, queryError) {
  const el = $('taskSummary'); if (!el) return;
  if (!list.length) { el.textContent = ''; return; }
  const count = phase => list.filter(it => (it.cft || {}).phase === phase).length;
  const head = `跟踪 ${list.length} 个提交：已完成 ${count('done')} · 下载中 ${count('downloading')} · 等待解析 ${count('waiting') + count('queued')} · 本地库已有 ${count('library')} · 失败 ${count('failed')}`;
  const when = queried ? `状态取自 ${CLOCK(queried)}` : '';
  const offline = !online ? '未连接 CFT，下面是上次查到的状态（不是"没有任务"）：' : queryError ? `问不到 CFT 的状态：${queryError}；下面是上次查到的结果。` : '';
  el.textContent = [offline + head, when].filter(Boolean).join(' · ');
}
let taskQueriedAt = 0, taskError = '';
function renderTasks() {
  const panel = $('taskPanel'), list = taskState.items || [];
  if (!panel) return;
  panel.hidden = !list.length;
  if (!list.length) { $('taskList') && $('taskList').replaceChildren(); summarizeTasks(list, 0, ''); return; }
  const box = $('taskList'); if (!box) return;
  box.replaceChildren();
  for (const it of list) {
    const cft = it.cft || { phase: 'unknown', text: '' };
    const div = document.createElement('div');
    div.className = 'task-item ' + (cft.phase || 'unknown');
    div.dataset.key = it.key || it.url;
    const dot = document.createElement('span'); dot.className = 'dot';
    const grow = document.createElement('div'); grow.className = 'grow';
    const b = document.createElement('b'); b.textContent = it.name || ('ID ' + (it.modelId || ''));
    const small = document.createElement('small');
    const line = [`扩展：${it.sent ? '已提交' : '未提交/待核对'}`, `CFT：${cft.text || '还没查到状态'}`];
    if (it.versionId) line.push(`版本 ${it.versionId}`);
    small.textContent = line.filter(Boolean).join(' · ');
    small.title = it.url || '';
    grow.append(b, small); div.append(dot, grow); box.append(div);
  }
  summarizeTasks(list, taskQueriedAt, taskError);
}
async function pollTasks() {
  const list = taskState.items || [];
  if (!list.length) return;
  if (!online) { renderTasks(); return; }
  const [tasksRes, parseRes] = await Promise.all([rpc('get_tasks', []), rpc('get_parse_state', [])]);
  if (!tasksRes.ok && !parseRes.ok) {
    taskError = (tasksRes.error || parseRes.error || '');
    renderTasks();
    return;
  }
  taskError = '';
  const taskList = Array.isArray(tasksRes.result) ? tasksRes.result : [];
  const parseState = (parseRes.ok && parseRes.result && typeof parseRes.result === 'object') ? parseRes.result : null;
  taskQueriedAt = Date.now();
  for (const it of list) {
    const next = cftOfItem(it, taskList, parseState);
    if (next.phase === 'waiting' && it.versionId && !it.checkedAt) {
      // 队列里没有、又拿到版本 ID：很可能是早就下过（或在别的目录里），问一次模型库
      const chk = await rpc('library_check', [it.versionId]);
      it.checkedAt = taskQueriedAt;
      if (chk.ok && chk.result && chk.result.found) {
        const path = (chk.result.paths || [])[0] || '';
        it.cft = { phase: 'library', terminal: true, text: '已在本机模型库里：' + path };
        continue;
      }
    }
    // 已经终态就别再被"这轮没查到"覆盖回等待态（CFT 完成任务后会把它移出活动列表）
    if (it.cft && it.cft.terminal && next.phase === 'waiting') continue;
    it.cft = next;
  }
  renderTasks();
  persistTasks();
  // 全部到终态就自己停表：轮询只在"还有事没落地"时才有意义，别让小窗开着也一直问
  if (!tasksNeedPolling()) stopTaskPolling();
}
const tasksNeedPolling = () => (taskState.items || []).some(it => !it.cft || !it.cft.terminal);

// ===== 批量归档：勾选页 → 模型文件立即批量入队 → 逐页图片/提示词归档 → 最后统一抓评论 =====
// 轮询调度集中在下面这组函数：小窗不可见（被切走/正在关闭）时全停，回来再续，别在后台空转
let archTimer = null, statusTimer = null, healthTimer = null, taskTimer = null, polling = false, archRunning = false;
const HEALTH_RECHECK_MS = 5000; // 只在「未连接」时低频复探：用户打开 CFT 后指示会自动变绿，不用重开小窗
const TASK_POLL_MS = 2500;
function startArchPolling() { if (!archTimer && !document.hidden) archTimer = setInterval(pollArchBatch, 1000); }
function stopArchPolling() { if (archTimer) { clearInterval(archTimer); archTimer = null; } }
function startTaskPolling() { if (!taskTimer && !document.hidden && tasksNeedPolling()) taskTimer = setInterval(pollTasks, TASK_POLL_MS); }
function stopTaskPolling() { if (taskTimer) { clearInterval(taskTimer); taskTimer = null; } }
function startPolling() {
  if (polling || document.hidden) return;
  polling = true;
  statusTimer = setInterval(status, 750);
  healthTimer = setInterval(() => { if (!online) health(); }, HEALTH_RECHECK_MS);
  if (archRunning) startArchPolling();
  if ((taskState.items || []).length) startTaskPolling();
}
function stopPolling() {
  polling = false;
  if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
  if (healthTimer) { clearInterval(healthTimer); healthTimer = null; }
  stopArchPolling(); stopTaskPolling();
}

const ARCH_PHASE_TEXT = { prepare: '阶段 1/3：解析模型并批量提交文件到 CFT 下载…', media: '阶段 2/3：逐页归档封面/配图/提示词…', comments: '阶段 3/3：统一抓取评论与评价…', done: '批量归档完成' };
function renderArchBatch(s) {
  const panel = $('archBatchPanel');
  // 拿不到状态、或拿到的是错误信封（{ok:false,msg} / {ok:true,started}）时，total 是 undefined：
  // 那种情况就是"当前没有批次"，面板必须收起。以前只判 total===0，于是这些回复会被渲染成
  // "共 undefined 页 · 完成 0 · 跳过(已归档) 0"，把一次通信故障显示成了用户的归档结果。
  const total = Number(s && s.total);
  if (!s || !Number.isFinite(total) || (!s.running && total === 0)) { panel.hidden = true; archRunning = !!(s && s.running); if (!archRunning) stopArchPolling(); return; }
  panel.hidden = false;
  $('archPhase').textContent = ARCH_PHASE_TEXT[s.phase] || s.phase || '';
  const done = (s.results || []).filter(r => ['done','skip','failed'].includes(r.status)).length;
  $('archProgress').textContent = s.running
    ? `进度 ${done}/${total}` + (s.current ? ` · 当前：${s.current}` : '')
    : `共 ${total} 页：完成 ${(s.results||[]).filter(r=>r.status==='done').length} · 跳过(已归档) ${s.skipped||0} · 失败 ${s.failed||0}`;
  const auto = (s.results || []).filter(r => r.autoChosenFile);
  $('archCounts').textContent = auto.length ? `多文件版本已自动选择主文件 ${auto.length} 页（其他精度可在 CFT 中单独下载）` : '';
  $('archAuto').replaceChildren();
  for (const r of auto.slice(0, 5)) {
    const p = document.createElement('p'); p.className = 'muted';
    p.textContent = `· ${r.dirName || r.url}：已自动选择 ${r.autoChosenFile}`;
    $('archAuto').append(p);
  }
  if (auto.length > 5) { const p=document.createElement('p');p.className='muted';p.textContent=`… 等 ${auto.length} 页`; $('archAuto').append(p); }
  $('btnArchCancel').hidden = !s.running;
  archRunning = !!s.running;
  if (s.running) startArchPolling();
  else if (archTimer) { stopArchPolling(); updateButtons(); }
}
async function pollArchBatch() {
  try {
    const s = await ext.runtime.sendMessage({ __cft: true, to: 'bg', type: 'archiveBatchStatus', payload: {} });
    if (s) renderArchBatch(s);
  } catch (_) { /* SW 瞬时不可达，下轮再取 */ }
}
async function startArchiveBatch() {
  const btn = $('btnArchiveBatch');
  // 局部名必须是 picked：写成 `const rows = rows.filter(...)` 会让右侧命中同名局部绑定的 TDZ，
  // 每次点击都抛 ReferenceError，按钮在禁用前就炸掉，用户看到的是「完全没反应」。
  const picked = rows.filter(r => selected.has(r.key)).map(r => ({ tabId: r.tabId, url: r.url, title: r.title }));
  if (!picked.length) { $('feedback').textContent = '请先在列表里勾选要批量归档的模型标签页'; updateButtons(); return; }
  btn.disabled = true; busy = true; updateButtons();
  $('feedback').textContent = `正在启动批量归档（${picked.length} 页）…`;
  try {
    const r = await ext.runtime.sendMessage({ __cft: true, to: 'bg', type: 'archiveBatchStart', payload: { rows: picked } });
    if (r && r.ok) {
      $('feedback').textContent = `批量归档已启动：${r.total} 页` + (r.skipped ? `（${r.skipped} 页无法识别已跳过）` : '') + '。可关闭小窗，后台继续。';
      renderArchBatch({ running: true, phase: 'prepare', total: r.total, results: [], skipped: r.skipped, failed: 0 });
      startArchPolling();
      pollArchBatch();
    } else {
      $('feedback').textContent = ((r && r.msg) || '批量归档启动失败') + '（可改用每页的「一键归档」）';
    }
  } catch (_) { $('feedback').textContent = '批量归档启动失败，请重试；离线时它会降级走浏览器下载目录，不是连接问题。'; }
  finally { busy = false; updateButtons(); } // archRunning 由 renderArchBatch 统一维护
}
$('btnArchiveBatch').onclick = startArchiveBatch;
$('btnArchCancel').onclick = async () => {
  try { await ext.runtime.sendMessage({ __cft: true, to: 'bg', type: 'archiveBatchCancel', payload: {} }); $('feedback').textContent = '已请求取消；正在进行的页面完成后停止，已入队的模型文件不会撤回。'; }
  catch (_) { $('feedback').textContent = '取消请求失败，请重试'; }
};
$('btnArchive').onclick = startArchive;
$('clearResults').onclick=async()=>{if(!confirm('清除扩展提交记录后，这些页面可再次发送。请先核对 CFT，避免重复任务；不会删除 CFT 中的下载记录。'))return;try{const r=await ext.runtime.sendMessage({type:'cft-batch-clear'});if(r.ok){renderJob(null);$('feedback').textContent='扩展提交记录已清除；CFT 中的任务未改动';}else $('feedback').textContent=r.msg;}catch(_){$('feedback').textContent='清除失败，请重试';}};
$('modeCurrent').onclick=()=>mode(false);$('modeBatch').onclick=()=>mode(true);
$('scope').onchange=refresh;$('refresh').onclick=()=>{refresh();health();};$('search').oninput=render;
for (const id of ['fType', 'fBase', 'fState', 'fSub', 'sort']) { const el = $(id); if (el) el.onchange = render; }
const taskRefresh = $('taskRefresh');
if (taskRefresh) taskRefresh.onclick = () => { taskState.items.forEach(it => { if (it.cft && !it.cft.terminal) it.cft = null; }); renderTasks(); pollTasks(); };
const clearTasksBtn = $('clearTasks');
if (clearTasksBtn) clearTasksBtn.onclick = async () => { await clearTasks(); stopTaskPolling(); $('feedback').textContent = '已清除扩展里的下载状态跟踪；CFT 中的任务与文件没有改动。'; };
$('selectAll').onchange=()=>{for(const r of visibleRows().filter(r=>!blocked().has(r.key))){if($('selectAll').checked)selected.add(r.key);else selected.delete(r.key);}render();};
$('sendCurrent').onclick=()=>send(active?[active.url]:[]);
$('sendBatch').onclick=()=>send(rows.filter(r=>selected.has(r.key)&&!blocked().has(r.key)).map(r=>r.url));
async function send(urls) {
  if(busy||job?.running||!urls.length)return;busy=true;updateButtons();$('feedback').textContent='正在提交…';
  try {const r=await ext.runtime.sendMessage({type:'cft-batch-start',urls});if(!r.ok){$('feedback').textContent=needsApiKey(r)?KEY_HINT:r.msg;return;}renderJob(r.job);$('results').open=true;
    // 提交完立刻挂上跟踪：job.items 里就是"用户勾的那些 URL"，一条不多一条不少
    recordSubmission((r.job && r.job.items) || urls.map(u => ({ url: u, status: 'waiting' })));
    pollTasks(); startTaskPolling();}
  catch(_){$('feedback').textContent='未收到扩展确认，请重新打开小窗核对提交结果，勿重复点击。';}
  finally{busy=false;updateButtons();}
}
document.addEventListener('DOMContentLoaded',async()=>{
  try {
    await loadTasks();   // 先恢复"发送到 CFT 之后"的跟踪，再读标签页：重开小窗也不能空屏
    const [tab]=await ext.tabs.query({active:true,currentWindow:true});active=CftTabs.modelLink(tab?.url);$('modelName').textContent=active?CftTabs.cleanTitle(tab.title):'当前页面不是 C 站模型页';$('modelUrl').textContent=active?active.url:'可以切换到“批量标签页”，发送其它已打开的模型页。';if(!active)mode(true);if(active)loadArchiveStatus();await status();await refresh();await health();
  }
  catch(_){$('feedback').textContent='扩展初始化失败，请重新加载';}
  startPolling();
  pollArchBatch(); // 重开小窗时恢复正在进行的批量归档状态显示
  if ((taskState.items || []).length) pollTasks();
});
// 小窗被切走/关闭就停止轮询：批量任务在 bg+offscreen 里跑，不靠 popup 定时器
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopPolling();
  else { startPolling(); status(); if (!online) health(); pollArchBatch(); if ((taskState.items || []).length) { pollTasks(); startTaskPolling(); } }
});
window.addEventListener('pagehide', stopPolling);
