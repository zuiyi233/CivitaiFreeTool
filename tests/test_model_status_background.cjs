/* background 下载状态：查询/缓存/离线回退/提交即标下载中/逐标签徽章。
   全离线：fetch 由本文件路由，绝不发真实请求。跑法：node tests/test_model_status_background.cjs */
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert/strict');
const root = path.resolve(__dirname, '..');
let passed = 0; const ok = (s) => { passed++; console.log('OK ' + s); };
const clone = (v) => JSON.parse(JSON.stringify(v));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const IMPORT_RE = /^import\s+['"]([^'"]+)['"]\s*;?\s*$/gm;
function loadScript(file, ctx, seen) {
  const abs = path.resolve(file); if (seen.has(abs)) return; seen.add(abs);
  const src = fs.readFileSync(abs, 'utf8');
  for (const m of src.matchAll(IMPORT_RE)) loadScript(path.resolve(path.dirname(abs), m[1]), ctx, seen);
  vm.runInContext('(function(){\n' + src.replace(IMPORT_RE, '') + '\n})();', ctx, { filename: path.relative(root, abs) });
}
const loadIn = (ctx, rel) => loadScript(path.join(root, rel), ctx, new Set());

function worker(initial) {
  const stored = clone(initial || {});
  const senders = {};
  const actionCalls = [];
  const fetchCalls = [];
  const notifyCalls = [], alarmCalls = [];
  const tabs = [{ id: 11, windowId: 1, url: 'https://civitai.red/models/101?modelVersionId=1001' }];
  let statusPayload = null, statusStatus = 200, failStatus = false;
  let revealStatus = 200, revealBody = null, notifyFail = false;
  let rpcState = { running: false, done: 2, total: 2, newer: 1, msg: '检查完成：1 个模型有更新' };
  let rpcTasks = [];
  const mkNs = (name, over = {}) => {
    const t = Object.assign({}, over);
    return new Proxy(t, {
      get(o, k) {
        const key = String(k);
        if (key in o) return o[key];
        if (/^on[A-Z]/.test(key)) {
          o[key] = { addListener: (f) => { (senders[name + '.' + key] = senders[name + '.' + key] || []).push(f); return true; }, removeListener: () => {} };
          return o[key];
        }
        o[key] = () => Promise.resolve(undefined); return o[key];
      },
      set(o, k, v) { o[k] = v; return true; },
    });
  };
  const ext = {
    runtime: mkNs('runtime', {
      id: 'test-id', lastError: undefined,
      sendMessage: async () => undefined,
      getContexts: async () => [], getURL: (p) => 'chrome-extension://test-id/' + p,
    }),
    storage: { local: {
      get: async () => clone(stored),
      set: async (v) => { Object.assign(stored, clone(v)); return true; },
      remove: async (k) => { delete stored[k]; return true; },
    } },
    action: mkNs('action', {
      setBadgeText: async (o) => { actionCalls.push(['text', clone(o)]); },
      setBadgeBackgroundColor: async (o) => { actionCalls.push(['bg', clone(o)]); },
      setTitle: async (o) => { actionCalls.push(['title', clone(o)]); },
    }),
    tabs: mkNs('tabs', {
      query: async () => clone(tabs),
      sendMessage: async () => { throw new Error('no receiver'); },
    }),
    offscreen: mkNs('offscreen'), downloads: mkNs('downloads'), contextMenus: mkNs('contextMenus'),
    windows: mkNs('windows'), sidePanel: mkNs('sidePanel'),
    alarms: mkNs('alarms', {
      create: async (name, info) => { alarmCalls.push(['create', name, clone(info || {})]); },
      clear: async (name) => { alarmCalls.push(['clear', name]); },
    }),
    notifications: mkNs('notifications', {
      create: async (id, opts) => {
        if (notifyFail) throw new Error('通知被浏览器拒绝');
        notifyCalls.push([id, clone(opts)]); return id;
      },
      clear: async () => {},
    }),
  };
  const ctx = vm.createContext({
    chrome: ext, URL, URLSearchParams, AbortController, setTimeout, clearTimeout,
    setInterval, clearInterval, queueMicrotask, console, TextEncoder, TextDecoder,
    structuredClone: clone,
    crypto: { randomUUID: () => '00000000-0000-4000-8000-000000000000' },
    fetch: async (url, o) => {
      const u = String(url);
      let body = null;
      try { body = o && o.body ? JSON.parse(o.body) : null; } catch (_) {}
      fetchCalls.push({ url: u, body });
      if (u.includes('/api/model/status')) {
        if (failStatus) throw new Error('Failed to fetch');
        if (statusStatus !== 200) return { ok: false, status: statusStatus, json: async () => ({ ok: false, msg: 'HTTP ' + statusStatus }) };
        return { ok: true, status: 200, json: async () => clone(statusPayload) };
      }
      if (u.includes('/api/download')) return { ok: true, status: 200, json: async () => ({ ok: true, msg: '已开始下载' }) };
      if (u.includes('/api/archive')) return { ok: true, status: 200, json: async () => ({ ok: true }) };
      if (u.includes('/api/reveal')) {
        return { ok: revealStatus === 200, status: revealStatus,
          json: async () => clone(revealBody || { ok: revealStatus === 200, path: body && body.path }) };
      }
      if (u.includes('/api/rpc')) {
        const method = body && body.method;
        if (method === 'mm_check_updates') return { ok: true, status: 200,
          json: async () => ({ ok: true, result: { started: true, msg: '已开始检查 1 个模型' } }) };
        if (method === 'get_mm_update_state') return { ok: true, status: 200, json: async () => ({ ok: true, result: clone(rpcState) }) };
        if (method === 'get_tasks') return { ok: true, status: 200, json: async () => ({ ok: true, result: clone(rpcTasks) }) };
        return { ok: false, status: 404, json: async () => ({ ok: false, msg: '没有这个 RPC' }) };
      }
      return { ok: false, status: 404, json: async () => ({ ok: false }) };
    },
  });
  vm.runInContext(fs.readFileSync(path.join(root, 'chrome-extension/tabs-common.js'), 'utf8'), ctx);
  loadIn(ctx, 'chrome-extension/background.js');
  const message = (type, payload = {}, sender = { id: 'test-id' }) => new Promise((resolve, reject) => {
    const env = { __cft: true, to: 'bg', type, payload };
    let done = false;
    const finish = (v) => { if (!done) { done = true; clearTimeout(timer); resolve(v); } };
    const timer = setTimeout(() => { if (!done) { done = true; reject(new Error('消息无答复: ' + type)); } }, 3000);
    let ret;
    for (const f of (senders['runtime.onMessage'] || [])) {
      ret = f(env, sender, finish);
      if (ret !== false && ret !== undefined) break;
    }
    if (ret && typeof ret.then === 'function') ret.then(finish, () => finish(null));
    else if (ret !== true) finish(ret === false ? null : (ret || null));
  });
  const rawMessage = (msg, sender = { id: 'test-id' }) => new Promise((resolve, reject) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; clearTimeout(timer); resolve(v); } };
    const timer = setTimeout(() => { if (!done) { done = true; reject(new Error('原始消息无答复: ' + msg.type)); } }, 3000);
    let ret;
    for (const f of (senders['runtime.onMessage'] || [])) {
      ret = f(msg, sender, finish);
      if (ret !== false && ret !== undefined) break;
    }
    if (ret && typeof ret.then === 'function') ret.then(finish, () => finish(null));
    else if (ret !== false && ret !== true) finish(ret || null);
  });
  return {
    ctx, stored, actionCalls, fetchCalls, notifyCalls, alarmCalls, senders, message, rawMessage,
    setStatus: (p) => { statusPayload = p; statusStatus = 200; failStatus = false; },
    setStatus404: () => { statusPayload = null; statusStatus = 404; failStatus = false; },
    setOffline: () => { failStatus = true; },
    setReveal: (st, body) => { revealStatus = st; revealBody = body || null; },
    setRpcState: (s) => { rpcState = Object.assign({}, rpcState, s); },
    setRpcTasks: (list) => { rpcTasks = list; },
    setNotifyFail: (v) => { notifyFail = !!v; },
  };
}

function backend(over) {
  return Object.assign({
    model_id: '101', version_id: '1001', state: 'downloaded', matched: 'version', version_matched: true,
    local: { source: 'library', version_id: '1001', version_name: 'v1.0', model_name: 'Demo',
      base_model: 'SDXL', path: 'D:\\models\\demo.safetensors', paths: ['D:\\models\\demo.safetensors'],
      file_count: 1, size: 1, exists: true },
    local_versions: [{ version_id: '1001', version_name: 'v1.0', source: 'library', exists: true }],
    download: null, history: null, update: null, reason: '',
  }, over || {});
}
const payload = (items) => ({
  ok: true, generated_at: Math.floor(Date.now() / 1000), library: { ready: true, scanning: false, count: 1 },
  updates: { checked_at: Math.floor(Date.now() / 1000), available: false, records: 0 }, items,
});
const req = [{ key: 'version:1001:file:choose', modelId: '101', versionId: '1001',
  url: 'https://civitai.red/models/101?modelVersionId=1001', tabId: 11 }];

(async () => {
  // 1. 在线查询：返回状态、写缓存、给发起标签设置徽章；新鲜缓存不再打桥
  let w = worker();
  w.setStatus(payload([backend()]));
  let r = await w.message('modelStatusQuery', { items: req, refresh: true });
  assert.equal(r.ok, true);
  assert.equal(r.items[0].state, 'downloaded');
  assert.equal(r.items[0].key, 'version:1001');
  assert.equal(w.stored.cftModelStatusV1.entries['version:1001'].state, 'downloaded');
  assert(w.actionCalls.some(([kind, o]) => kind === 'text' && o.tabId === 11 && o.text === '✓'),
    JSON.stringify(w.actionCalls));
  assert.equal(w.fetchCalls.length, 1);
  assert.deepEqual(w.fetchCalls[0].body.items, [{ model_id: '101', version_id: '1001', url: req[0].url }]);
  r = await w.message('modelStatusQuery', { items: req, refresh: 'auto' });
  assert.equal(r.cachedOnly, true);
  assert.equal(w.fetchCalls.length, 1, '新鲜缓存不应重复打桥');
  ok('在线查询：状态入缓存、逐标签 ✓ 徽章、请求体只含 ID/URL；新鲜缓存零请求');

  // 2. 离线：保留上次真实状态，只标 offline
  w.setOffline();
  const before = w.stored.cftModelStatusV1.entries['version:1001'];
  r = await w.message('modelStatusQuery', { items: req, refresh: true });
  assert.equal(r.offline, true);
  assert.equal(r.items[0].state, 'downloaded', '离线不能把已下载抹成未知');
  assert.equal(r.items[0].offline, true);
  assert.equal(w.stored.cftModelStatusV1.entries['version:1001'].state, before.state);
  assert.equal(w.stored.cftModelStatusV1.entries['version:1001'].offline, true);
  ok('离线回退：保留上次已下载状态并标 offline，绝不显示成未下载');

  // 3. 旧桌面端 404：给出升级提示，不当成离线
  w = worker();
  w.setStatus404();
  r = await w.message('modelStatusQuery', { items: req, refresh: true });
  assert.equal(r.offline, false);
  assert.equal(r.items[0].state, 'unknown');
  assert(r.items[0].reason.includes('版本过旧'), r.items[0].reason);
  ok('旧桌面端 404 与离线分开：提示升级 CFT，而不是“没开软件/未下载”');

  // 4. 缓存只读消息
  w = worker({ cftModelStatusV1: { version: 1, savedAt: 123, entries: {
    'version:1001': { key: 'version:1001', state: 'downloaded', checkedAt: 1, seenAt: 1 },
  } } });
  r = await w.message('modelStatusCache', { modelId: '101', versionId: '1001' });
  assert.equal(r.at, 123);
  assert.equal(r.entries['version:1001'].state, 'downloaded');
  ok('modelStatusCache 按模型/版本取缓存，不发网络');

  // 5. 提交成功立即标下载中（弹层批量发送链路）：等 post → markSubmitted 落缓存
  w = worker();
  const startResp = await w.rawMessage({ type: 'cft-batch-start', urls: [req[0].url] });
  assert.equal(startResp && startResp.ok, true, JSON.stringify(startResp));
  for (let i = 0; i < 100; i++) {
    if (w.stored.cftModelStatusV1 && w.stored.cftModelStatusV1.entries
      && w.stored.cftModelStatusV1.entries['version:1001']
      && w.stored.cftModelStatusV1.entries['version:1001'].state === 'downloading') break;
    await sleep(20);
  }
  const submitted = w.stored.cftModelStatusV1 && w.stored.cftModelStatusV1.entries
    && w.stored.cftModelStatusV1.entries['version:1001'];
  assert(submitted, '提交后应有 version:1001 的状态缓存');
  assert.equal(submitted.state, 'downloading');
  assert.equal(submitted.download.status, 'pending');
  const modelSubmitted = w.stored.cftModelStatusV1.entries['model:101'];
  assert(modelSubmitted, '模型级 key 也要同步，页面没有版本参数时才有提示');
  assert.equal(modelSubmitted.state, 'downloading');
  assert.equal(w.stored.lastBatch.items[0].status, 'sent');
  ok('提交到 CFT 成功即写 downloading（版本级+模型级），不必等解析入队');

  const until = async (fn, ms = 3000) => {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) { if (fn()) return true; await sleep(20); }
    return false;
  };

  // 6. 页面级历史：只记模型页；同标签 30s 内去重；非模型页不写
  w = worker();
  const onUpdated = (w.senders['tabs.onUpdated'] || [])[0];
  assert(onUpdated, 'tabs.onUpdated 监听应已注册');
  const pageUrl = 'https://civitai.red/models/101?modelVersionId=1001';
  onUpdated(11, { url: pageUrl }, { id: 11, title: 'Demo | Civitai', url: pageUrl });
  assert(await until(() => w.stored.cftPageHistoryV1 && w.stored.cftPageHistoryV1.items['version:1001']),
    '模型页应写入页面历史');
  const hist = w.stored.cftPageHistoryV1.items['version:1001'];
  assert.equal(hist.modelId, '101');
  assert.equal(hist.versionId, '1001');
  assert.equal(hist.url, pageUrl);
  assert.equal(hist.title, 'Demo');
  assert.equal(hist.count, 1);
  onUpdated(11, { status: 'complete' }, { id: 11, title: 'Demo | Civitai', url: pageUrl });
  await sleep(60);
  assert.equal(w.stored.cftPageHistoryV1.items['version:1001'].count, 1, '30s 内同标签同一页不重复计数');
  onUpdated(12, { status: 'complete' }, { id: 12, title: 'img', url: 'https://civitai.red/images/123' });
  onUpdated(13, { status: 'complete' }, { id: 13, title: 'download', url: 'https://civitai.red/api/download/models/777' });
  await sleep(60);
  assert.deepEqual(Object.keys(w.stored.cftPageHistoryV1.items), ['version:1001'],
    '图片页与下载直链都不该进页面历史');
  ok('页面历史：只记模型页、标题清洗、同标签 30s 去重，图片页/下载直链不写');

  // 7. badge:false（列表页脚本）只取状态，不动工具栏徽章
  w = worker();
  w.setStatus(payload([backend()]));
  const badgeBefore = w.actionCalls.length;
  r = await w.message('modelStatusQuery', { items: req, refresh: true, badge: false });
  assert.equal(r.ok, true);
  assert.equal(w.actionCalls.length, badgeBefore, 'badge:false 不应产生任何工具栏徽章调用');
  assert(w.stored.cftModelStatusV1.entries['version:1001'], '状态仍应入缓存供列表徽章复用');
  ok('列表页查询 badge:false：缓存照写、工具栏徽章不动');

  // 8. 一键检查更新：同路径去重后交给既有 mm_check_updates；查询进度；来源校验
  w = worker();
  let r2 = await w.message('checkModelUpdates', {
    paths: ['D:\\models\\a.safetensors', 'D:\\models\\a.safetensors', ''],
  });
  assert.equal(r2.ok, true, JSON.stringify(r2));
  assert.equal(r2.started, true);
  const checkCall = w.fetchCalls.find((c) => c.url.endsWith('/api/rpc') && c.body && c.body.method === 'mm_check_updates');
  assert(checkCall, '应调用 mm_check_updates');
  assert.deepEqual(checkCall.body.args, [false, ['D:\\models\\a.safetensors']]);
  const st = await w.message('modelUpdateState');
  assert.equal(st.ok, true);
  assert.equal(st.state.newer, 1);
  assert.equal(st.state.running, false);
  const denied = await w.message('checkModelUpdates', { paths: ['D:\\x'] }, { id: 'other' });
  assert.equal(denied.ok, false);
  ok('检查更新：路径去重+上限、参数 [false,paths]、进度透传、非本扩展来源拒绝');

  // 9. 打开目录：只转发路径；未登记/离线把真实原因带回来；空路径不发请求
  w = worker();
  let rv = await w.message('revealPath', { path: 'D:\\models' });
  assert.equal(rv.ok, true);
  const rvCall = w.fetchCalls.find((c) => c.url.endsWith('/api/reveal'));
  assert.deepEqual(rvCall.body, { path: 'D:\\models' });
  w.setReveal(403, { ok: false, msg: '路径未登记，拒绝打开' });
  rv = await w.message('revealPath', { path: 'C:\\Windows' });
  assert.equal(rv.ok, false);
  assert(rv.msg.includes('未登记'), rv.msg);
  const callsBefore = w.fetchCalls.filter((c) => c.url.endsWith('/api/reveal')).length;
  rv = await w.message('revealPath', { path: '   ' });
  assert.equal(rv.ok, false);
  assert.equal(w.fetchCalls.filter((c) => c.url.endsWith('/api/reveal')).length, callsBefore, '空路径不该发请求');
  w.setReveal(404, { ok: false, msg: 'Not Found' });
  rv = await w.message('revealPath', { path: 'D:\\models' });
  assert.equal(rv.ok, false);
  assert(rv.msg.includes('版本过旧'), '旧桌面端 404 要说升级，不是“未连接”：' + rv.msg);
  ok('打开目录：路径原样转发、拒绝原因透传、404 提示升级、空路径零请求');

  // 10. 完成通知：按 key+phase 去重、落点登记、按钮回调走 reveal
  w = worker();
  let n = await w.message('notifyTaskDone', {
    key: 'version:1001', phase: 'done', name: 'Demo',
    filename: 'demo.safetensors', dest_dir: 'D:\\models',
  });
  assert.equal(n.ok, true, JSON.stringify(n));
  assert.equal(w.notifyCalls.length, 1);
  const nid = w.notifyCalls[0][0], nopts = w.notifyCalls[0][1];
  assert.equal(nopts.title, '下载完成');
  assert(nopts.message.includes('demo.safetensors') && nopts.message.includes('D:\\models'), nopts.message);
  assert.deepEqual(nopts.buttons, [{ title: '打开所在目录' }]);
  assert.equal(w.stored.cftNotifyDestV1[nid], 'D:\\models');
  n = await w.message('notifyTaskDone', { key: 'version:1001', phase: 'done', dest_dir: 'D:\\models' });
  assert.equal(n.skipped, true, '同一 key+phase 不应重复弹');
  assert.equal(w.notifyCalls.length, 1);
  n = await w.message('notifyTaskDone', { key: 'version:1001', phase: 'failed' });
  assert.equal(n.ok, true, '阶段变化应重新通知');
  assert.equal(w.notifyCalls.length, 2);
  const onBtn = (w.senders['notifications.onButtonClicked'] || [])[0];
  assert(onBtn, '通知按钮监听应已注册');
  onBtn(nid);
  assert(await until(() => w.fetchCalls.some((c) => c.url.endsWith('/api/reveal'))), '按钮应打开登记落点');
  assert.deepEqual(w.fetchCalls.find((c) => c.url.endsWith('/api/reveal')).body, { path: 'D:\\models' });
  ok('完成通知：终态去重、按钮只在有落点时出现、按钮回调复用 /api/reveal');

  // 11. 后台守望：任务落地即通知，全部终态后自动清 alarm
  w = worker({ cftExtTasks: { at: Date.now(), items: [{
    key: 'version:1001', url: pageUrl, modelId: '101', versionId: '1001', name: 'Demo', cft: null,
  }] } });
  w.setRpcTasks([{ url: pageUrl, status: 'done', filename: 'demo.safetensors', dest_dir: 'D:\\models' }]);
  const onAlarm = (w.senders['alarms.onAlarm'] || [])[0];
  assert(onAlarm, 'alarms.onAlarm 应已注册');
  onAlarm({ name: 'cftTaskWatch' });
  assert(await until(() => w.notifyCalls.length === 1), '守望应给落地任务发通知');
  assert.equal(w.notifyCalls[0][1].title, '下载完成');
  assert(await until(() => w.alarmCalls.some(([op, name]) => op === 'clear' && name === 'cftTaskWatch')),
    '全部终态应清除守望 alarm：' + JSON.stringify(w.alarmCalls));
  ok('后台守望：关掉小窗后仍通知，全部落地即停表');

  // 12. 同模型的旧 done 任务不能冒充这次下载完成：版本不同必须继续等
  w = worker({ cftExtTasks: { at: Date.now(), items: [{
    key: 'version:1001', url: pageUrl, modelId: '101', versionId: '1001', name: 'Demo', cft: null,
  }] } });
  w.setRpcTasks([
    { id: 'old', url: 'https://civitai.red/models/101', version_id: '1000', status: 'done',
      filename: 'old.safetensors', dest_dir: 'D:\\old' },
    { id: 'new', url: 'https://civitai.red/models/101', version_id: '1001', status: 'downloading',
      progress: 50, dest_dir: 'D:\\models' },
  ]);
  const onAlarm2 = (w.senders['alarms.onAlarm'] || [])[0];
  onAlarm2({ name: 'cftTaskWatch' });
  await sleep(250);
  assert.equal(w.notifyCalls.length, 0, '旧版本的 done 任务不能触发“下载完成”');
  assert(!w.alarmCalls.some(([op, name]) => op === 'clear' && name === 'cftTaskWatch'),
    '新任务还在下载，守望不能提前清掉');
  w.setRpcTasks([{ id: 'new', url: 'https://civitai.red/models/101', version_id: '1001', status: 'done',
    filename: 'demo.safetensors', dest_dir: 'D:\\models' }]);
  onAlarm2({ name: 'cftTaskWatch' });
  assert(await until(() => w.notifyCalls.length === 1), '本版本落地后应通知');
  assert.equal(w.stored.cftNotifiedTerminalsV1[0], 'version:1001:done:new',
    '去重键要带 task_id：同版本重下才会再通知');
  ok('任务按版本匹配：旧 done 不冒充、新任务落地才通知、去重键含 task_id');

  // 13. 通知创建失败不能提前写去重（否则这条永远不再提醒）
  w = worker();
  w.setNotifyFail(true);
  let nf = await w.message('notifyTaskDone', { key: 'version:1002', phase: 'done', task_id: 't9' });
  assert.equal(nf.ok, false);
  assert.deepEqual(w.stored.cftNotifiedTerminalsV1 || [], [], '创建失败不应写入已通知集合');
  w.setNotifyFail(false);
  nf = await w.message('notifyTaskDone', { key: 'version:1002', phase: 'done', task_id: 't9' });
  assert.equal(nf.ok, true, JSON.stringify(nf));
  assert.equal(w.notifyCalls.length, 1, '重试成功且只弹一次');
  nf = await w.message('notifyTaskDone', { key: 'version:1002', phase: 'done', task_id: 't9' });
  assert.equal(nf.skipped, true);
  ok('通知失败不写去重：浏览器拒绝后可重试，成功后才记账');

  console.log(`\n${passed} passed`);
})().catch((e) => { console.error(e); process.exit(1); });
