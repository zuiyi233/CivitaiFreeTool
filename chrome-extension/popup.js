const ext = typeof browser !== 'undefined' ? browser : chrome;
const $ = id => document.getElementById(id);
// tabs-common.js 以 <script> 先于本文件加载，全局为 CftTabs；文件缺失时兜底成空对象，别让整窗白屏
const CT = globalThis.CftTabs || {};
let tabs = [], rows = [], selected = new Set(), seenKeys = new Set(), firstRows = true, active = null, job = null, online = false, busy = false, refreshSerial = 0;
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
function visibleRows() {const q = $('search').value.trim().toLowerCase();return rows.filter(r => !q || (r.title+' '+r.url).toLowerCase().includes(q));}
// 列表 DOM 只取决于：可见行集合 + 搜索词 + 被占用（blocked）的 key + 是否有批次在跑。
// 轮询期间逐项 status 变化（waiting→sending→sent）不改变这个集合，所以用它做签名，避免每秒重建 500 行。
function listSignature() {
  return [job?.running ? 1 : 0, [...blocked()].sort().join(','), visibleRows().map(r => r.key).join(','), $('search').value].join('#');
}
let seenListSig = '';
function render() {
  const list = $('tabList'), blockedKeys = blocked(); list.replaceChildren();
  const shown = visibleRows();
  for (const row of shown) {
    const label = document.createElement('label'); label.className = 'tab-row';
    const input = document.createElement('input'); input.type = 'checkbox'; input.dataset.key = row.key; input.checked = selected.has(row.key) && !blockedKeys.has(row.key); input.disabled = blockedKeys.has(row.key) || !!job?.running;
    input.addEventListener('change', () => {if(input.checked)selected.add(row.key);else selected.delete(row.key);seenKeys.add(row.key);syncSelection();});
    const text = document.createElement('span'), name = document.createElement('strong'), meta = document.createElement('small');
    name.textContent = row.title;meta.textContent = `ID ${row.id}` + (row.version ? ` · 版本 ${row.version}` : '') + (row.copies > 1 ? ` · 合并 ${row.copies} 个标签` : '') + (blockedKeys.has(row.key) ? ' · 查看下方提交结果' : '');
    label.title = row.url;text.append(name, meta);label.append(input,text);list.append(label);
  }
  if (!shown.length) {const p=document.createElement('p');p.className='empty';p.textContent=rows.length?'没有匹配的模型标签页':'没有打开的 C 站模型页';list.append(p);}
  $('tabSummary').textContent = `${rows.length} 个不同模型 / 版本；只读取模型页标题和链接，不读取网页内容。`;
  seenListSig = listSignature();
  syncSelection();
}
// 轮询路径用：数据真变了才重建列表，否则只刷新按钮态
function renderListIfChanged() { if (listSignature() !== seenListSig) render(); else syncSelection(); }
function syncSelection() {
  const enabled = visibleRows().filter(r => !blocked().has(r.key)), count=enabled.filter(r=>selected.has(r.key)).length;
  $('selectAll').checked = enabled.length > 0 && count===enabled.length;$('selectAll').indeterminate=count>0&&count<enabled.length;$('selectAll').disabled=!enabled.length||!!job?.running;
  updateButtons();
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
    const r = await fetch('http://127.0.0.1:47531/api/health', {signal: c.signal});
    const json = r.ok ? await r.json().catch(() => null) : null;
    st = typeof CT.cftHealth === 'function' ? CT.cftHealth(json, CT.MIN_APP_VERSION)
      : (json && json.ok === true ? {ok: true, reason: 'ok', version: String(json.version || '')} : {ok: false, reason: 'offline', version: ''});
  } catch (_) { st = {ok: false, reason: 'offline', version: ''}; }
  finally { clearTimeout(timer); }
  // too-old 依然是真 CFT，/api/download 可用 → 允许发送；not-cft / offline 一律视为未连接
  online = st.reason === 'ok' || st.reason === 'too-old';
  cftState = st;
  showConnection(); updateButtons();
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
async function refresh() {
  const serial=++refreshSerial;
  try {
    const queried=await ext.tabs.query($('scope').value==='current'?{currentWindow:true}:{});
    if(serial!==refreshSerial)return;
    tabs=queried; applyRows(CftTabs.candidates(tabs)); render();
  }catch(_){$('feedback').textContent='读取标签页失败，请重新加载扩展';}
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
  return [j.id ?? '', j.running ? 1 : 0, items.length, items.map(r => (r.key || r.id || '') + ':' + r.status).join('|')].join('#');
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
    $('feedback').textContent = keyless ? `${keyless} 项没提交是因为软件缺 API Key（不是提交失败）：请在 CFT「设置 → 目录与账号」填写并保存 Civitai API Key，再点「刷新」重新发送。归档不受影响。` : '“已提交”仅表示 CFT 接收链接，解析/文件选择结果请在软件内查看。';
  }
}
function renderJob(next) {
  const sig = panelSignature(next), panelChanged = sig !== seenPanelSig;
  job = next; seenPanelSig = sig;
  if (panelChanged) renderResultsPanel();
  renderListIfChanged();
}
async function status() {try {const r=await ext.runtime.sendMessage({type:'cft-batch-status'});if(r.ok)renderJob(r.job);}catch(_) {}}
async function send(urls) {
  if(busy||job?.running||!urls.length)return;busy=true;updateButtons();$('feedback').textContent='正在提交…';
  try {const r=await ext.runtime.sendMessage({type:'cft-batch-start',urls});if(!r.ok){$('feedback').textContent=needsApiKey(r)?KEY_HINT:r.msg;return;}renderJob(r.job);$('results').open=true;}
  catch(_){$('feedback').textContent='未收到扩展确认，请重新打开小窗核对提交结果，勿重复点击。';}
  finally{busy=false;updateButtons();}
}
// ===== 批量归档：勾选页 → 模型文件立即批量入队 → 逐页图片/提示词归档 → 最后统一抓评论 =====
// 轮询调度集中在下面这组函数：小窗不可见（被切走/正在关闭）时全停，回来再续，别在后台空转
let archTimer = null, statusTimer = null, healthTimer = null, polling = false, archRunning = false;
const HEALTH_RECHECK_MS = 5000; // 只在「未连接」时低频复探：用户打开 CFT 后指示会自动变绿，不用重开小窗
function startArchPolling() { if (!archTimer && !document.hidden) archTimer = setInterval(pollArchBatch, 1000); }
function stopArchPolling() { if (archTimer) { clearInterval(archTimer); archTimer = null; } }
function startPolling() {
  if (polling || document.hidden) return;
  polling = true;
  statusTimer = setInterval(status, 750);
  healthTimer = setInterval(() => { if (!online) health(); }, HEALTH_RECHECK_MS);
  if (archRunning) startArchPolling();
}
function stopPolling() {
  polling = false;
  if (statusTimer) { clearInterval(statusTimer); statusTimer = null; }
  if (healthTimer) { clearInterval(healthTimer); healthTimer = null; }
  stopArchPolling();
}

const ARCH_PHASE_TEXT = { prepare: '阶段 1/3：解析模型并批量提交文件到 CFT 下载…', media: '阶段 2/3：逐页归档封面/配图/提示词…', comments: '阶段 3/3：统一抓取评论与评价…', done: '批量归档完成' };
function renderArchBatch(s) {
  const panel = $('archBatchPanel');
  if (!s || (!s.running && s.total === 0)) { panel.hidden = true; archRunning = !!(s && s.running); if (!archRunning) stopArchPolling(); return; }
  panel.hidden = false;
  $('archPhase').textContent = ARCH_PHASE_TEXT[s.phase] || s.phase || '';
  const done = (s.results || []).filter(r => ['done','skip','failed'].includes(r.status)).length;
  $('archProgress').textContent = s.running
    ? `进度 ${done}/${s.total}` + (s.current ? ` · 当前：${s.current}` : '')
    : `共 ${s.total} 页：完成 ${(s.results||[]).filter(r=>r.status==='done').length} · 跳过(已归档) ${s.skipped||0} · 失败 ${s.failed||0}`;
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
$('selectAll').onchange=()=>{for(const r of visibleRows().filter(r=>!blocked().has(r.key))){if($('selectAll').checked)selected.add(r.key);else selected.delete(r.key);}render();};
$('sendCurrent').onclick=()=>send(active?[active.url]:[]);
$('sendBatch').onclick=()=>send(rows.filter(r=>selected.has(r.key)&&!blocked().has(r.key)).map(r=>r.url));
document.addEventListener('DOMContentLoaded',async()=>{
  try {const [tab]=await ext.tabs.query({active:true,currentWindow:true});active=CftTabs.modelLink(tab?.url);$('modelName').textContent=active?CftTabs.cleanTitle(tab.title):'当前页面不是 C 站模型页';$('modelUrl').textContent=active?active.url:'可以切换到“批量标签页”，发送其它已打开的模型页。';if(!active)mode(true);if(active)loadArchiveStatus();await status();await refresh();await health();}
  catch(_){$('feedback').textContent='扩展初始化失败，请重新加载';}
  startPolling();
  pollArchBatch(); // 重开小窗时恢复正在进行的批量归档状态显示
});
// 小窗被切走/关闭就停止轮询：批量任务在 bg+offscreen 里跑，不靠 popup 定时器
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopPolling();
  else { startPolling(); status(); if (!online) health(); pollArchBatch(); }
});
window.addEventListener('pagehide', stopPolling);