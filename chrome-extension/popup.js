const ext = typeof browser !== 'undefined' ? browser : chrome;
const $ = id => document.getElementById(id);
let tabs = [], rows = [], selected = new Set(), active = null, job = null, online = false, busy = false, seenJob = '', refreshSerial = 0;
const blocked = () => new Set([...(job?.completedKeys || []),...(job?.items || []).filter(r => ['sent','sending','waiting','uncertain'].includes(r.status)).map(r => r.key)]);
const getActiveTab = async () => (await ext.tabs.query({ active: true, currentWindow: true }))[0];
function updateButtons() {
  const lock = busy || !!job?.running;
  $('sendCurrent').disabled = lock || !online || !active || blocked().has(active.key);
  const count = rows.filter(r => selected.has(r.key) && !blocked().has(r.key)).length;
  $('sendBatch').disabled = lock || !online || !count;
  $('sendBatch').textContent = `发送选中 ${count} 项到 CFT`;
  $('selectionCount').textContent = `已选 ${count} / ${rows.length}`;
  // 一键归档：不依赖 CFT 在线，只要有当前模型页即可；批量归档同样不依赖在线（离线走浏览器下载降级）
  $('btnArchive').disabled = lock || !active;
  $('btnArchiveBatch').disabled = lock || !count;
  $('btnArchiveBatch').textContent = `🗂 批量归档选中 ${count} 页`;
}
function visibleRows() {const q = $('search').value.trim().toLowerCase();return rows.filter(r => !q || (r.title+' '+r.url).toLowerCase().includes(q));}
function render() {
  const list = $('tabList'), blockedKeys = blocked(); list.replaceChildren();
  const shown = visibleRows();
  for (const row of shown) {
    const label = document.createElement('label'); label.className = 'tab-row';
    const input = document.createElement('input'); input.type = 'checkbox'; input.dataset.key = row.key; input.checked = selected.has(row.key) && !blockedKeys.has(row.key); input.disabled = blockedKeys.has(row.key) || !!job?.running;
    input.addEventListener('change', () => {if(input.checked)selected.add(row.key);else selected.delete(row.key);syncSelection();});
    const text = document.createElement('span'), name = document.createElement('strong'), meta = document.createElement('small');
    name.textContent = row.title;meta.textContent = `ID ${row.id}` + (row.version ? ` · 版本 ${row.version}` : '') + (row.copies > 1 ? ` · 合并 ${row.copies} 个标签` : '') + (blockedKeys.has(row.key) ? ' · 查看下方提交结果' : '');
    label.title = row.url;text.append(name, meta);label.append(input,text);list.append(label);
  }
  if (!shown.length) {const p=document.createElement('p');p.className='empty';p.textContent=rows.length?'没有匹配的模型标签页':'没有打开的 C 站模型页';list.append(p);}
  $('tabSummary').textContent = `${rows.length} 个不同模型 / 版本；只读取模型页标题和链接，不读取网页内容。`;
  syncSelection();
}
function syncSelection() {
  const enabled = visibleRows().filter(r => !blocked().has(r.key)), count=enabled.filter(r=>selected.has(r.key)).length;
  $('selectAll').checked = enabled.length > 0 && count===enabled.length;$('selectAll').indeterminate=count>0&&count<enabled.length;$('selectAll').disabled=!enabled.length||!!job?.running;
  updateButtons();
}
async function health() {
  const c=new AbortController(),timer=setTimeout(()=>c.abort(),2500);
  try {const r=await fetch('http://127.0.0.1:47531/api/health',{signal:c.signal});online=r.ok&&!!(await r.json()).ok;}catch(_){online=false;}finally{clearTimeout(timer);}
  $('connection').textContent=online?'CFT 已连接':'请先打开 CFT';$('connection').classList.toggle('connected',online);updateButtons();
}
async function refresh() {
  const serial=++refreshSerial;
  try {
    const queried=await ext.tabs.query($('scope').value==='current'?{currentWindow:true}:{});
    if(serial!==refreshSerial)return;
    tabs=queried;rows=CftTabs.candidates(tabs);selected=new Set(rows.map(r=>r.key));render();
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
function renderJob(next) {
  const change = seenJob !== (next?.id||'') || JSON.stringify(job)!==JSON.stringify(next);
  job=next;
  if(!change){updateButtons();return;}
  seenJob=job?.id||'';$('results').hidden=!job;$('clearResults').hidden=!job;$('clearResults').disabled=!!job?.running;
  if(job){
    const sent=job.items.filter(r=>r.status==='sent').length, uncertain=job.items.filter(r=>r.status==='uncertain').length;
    $('resultSummary').textContent=`${job.running?'正在提交':'提交结果'}：已提交 ${sent}/${job.items.length}`+(uncertain?` · ${uncertain} 项待核对`:'');
    $('resultList').replaceChildren();
    for(const row of job.items){const p=document.createElement('p');p.className='result '+row.status;p.textContent=`ID ${row.id}：${({waiting:'等待提交',sending:'正在提交',sent:'已提交',failed:'未提交',uncertain:'待核对'})[row.status]} · ${row.msg||''}`;$('resultList').append(p);}
    if(!job.running){$('results').open=true;$('feedback').textContent='“已提交”仅表示 CFT 接收链接，解析/文件选择结果请在软件内查看。';}
  }
  render();
}
async function status() {try {const r=await ext.runtime.sendMessage({type:'cft-batch-status'});if(r.ok)renderJob(r.job);}catch(_) {}}
async function send(urls) {
  if(busy||job?.running||!urls.length)return;busy=true;updateButtons();$('feedback').textContent='正在提交…';
  try {const r=await ext.runtime.sendMessage({type:'cft-batch-start',urls});if(!r.ok){$('feedback').textContent=r.msg;return;}renderJob(r.job);$('results').open=true;}
  catch(_){$('feedback').textContent='未收到扩展确认，请重新打开小窗核对提交结果，勿重复点击。';}
  finally{busy=false;updateButtons();}
}
// ===== 批量归档：勾选页 → 模型文件立即批量入队 → 逐页图片/提示词归档 → 最后统一抓评论 =====
let archTimer = null;
const ARCH_PHASE_TEXT = { prepare: '阶段 1/3：解析模型并批量提交文件到 CFT 下载…', media: '阶段 2/3：逐页归档封面/配图/提示词…', comments: '阶段 3/3：统一抓取评论与评价…', done: '批量归档完成' };
function renderArchBatch(s) {
  const panel = $('archBatchPanel');
  if (!s || (!s.running && s.total === 0)) { panel.hidden = true; return; }
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
  if (s.running && !archTimer) archTimer = setInterval(pollArchBatch, 1000);
  if (!s.running && archTimer) { clearInterval(archTimer); archTimer = null; updateButtons(); }
}
async function pollArchBatch() {
  try {
    const s = await ext.runtime.sendMessage({ __cft: true, to: 'bg', type: 'archiveBatchStatus', payload: {} });
    if (s) renderArchBatch(s);
  } catch (_) { /* SW 瞬时不可达，下轮再取 */ }
}
async function startArchiveBatch() {
  const btn = $('btnArchiveBatch');
  const rows = rows.filter(r => selected.has(r.key)).map(r => ({ tabId: r.tabId, url: r.url, title: r.title }));
  if (!rows.length) return;
  btn.disabled = true; busy = true; updateButtons();
  $('feedback').textContent = `正在启动批量归档（${rows.length} 页）…`;
  try {
    const r = await ext.runtime.sendMessage({ __cft: true, to: 'bg', type: 'archiveBatchStart', payload: { rows } });
    if (r && r.ok) {
      $('feedback').textContent = `批量归档已启动：${r.total} 页` + (r.skipped ? `（${r.skipped} 页无法识别已跳过）` : '') + '。可关闭小窗，后台继续。';
      renderArchBatch({ running: true, phase: 'prepare', total: r.total, results: [], skipped: r.skipped, failed: 0 });
      if (!archTimer) archTimer = setInterval(pollArchBatch, 1000);
      pollArchBatch();
    } else {
      $('feedback').textContent = (r && r.msg) || '批量归档启动失败';
    }
  } catch (_) { $('feedback').textContent = '批量归档启动失败，请重试'; }
  finally { busy = false; updateButtons(); }
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
  setInterval(status,750);
  pollArchBatch(); // 重开小窗时恢复正在进行的批量归档状态显示
});