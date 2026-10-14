/* Model-tab metadata only. No page injection, cookies, API keys or unrelated URLs. */
(function (root) {
  function modelLink(raw) {
    try {
      const u = new URL(raw);
      if (!['http:', 'https:'].includes(u.protocol) || !/^(www\.)?civitai\.(red|com)$/i.test(u.hostname) || u.port || u.username || u.password) return null;
      const m = u.pathname.match(/^\/(api\/download\/)?models\/([1-9]\d*)(?:\/[^?#]*)?$/i);
      if (!m) return null;
      const query = new URLSearchParams();
      for (const key of ['modelVersionId', 'fileId']) {
        const values = u.searchParams.getAll(key);
        if (values.length > 1 || (values.length && !/^[1-9]\d*$/.test(values[0]))) return null;
        if (values.length) query.set(key, values[0]);
      }
      const version = m[1] ? m[2] : query.get('modelVersionId');
      const key = (version ? 'version:' + version : 'model:' + m[2]) + ':file:' + (query.get('fileId') || 'choose');
      u.search = query.toString(); u.hash = '';
      return {url:u.href, key, id:m[2], version:version || ''};
    } catch (_) { return null; }
  }
  // /api/health 认人：只判「应答方是不是 CFT、版本够不够」，不是安全边界（本机任意进程
  // 都能仿冒 app 字段），它挡的是端口上挂着别的服务/旧版 exe 这类错认。
  const APP_ID = 'CivitaiFreeTool';
  const MIN_APP_VERSION = '2.6.25'; // 归档桥 /api/archive/* 与 /api/library/check 的最低版本
  function versionAtLeast(v, min) {
    const a = String(v || '').split('.').map(n => parseInt(n, 10) || 0);
    const b = String(min || '').split('.').map(n => parseInt(n, 10) || 0);
    for (let i = 0; i < Math.max(a.length, b.length); i++) {
      const x = a[i] || 0, y = b[i] || 0;
      if (x !== y) return x > y;
    }
    return true;
  }
  function cftHealth(json, minVersion) {
    const j = (json && typeof json === 'object') ? json : null;
    const version = String((j && j.version) || '');
    if (!j || j.ok !== true) return { ok: false, reason: 'offline', version: '' };
    if (j.app !== APP_ID) return { ok: false, reason: 'not-cft', version };
    if (!versionAtLeast(version, minVersion || MIN_APP_VERSION)) return { ok: false, reason: 'too-old', version };
    return { ok: true, reason: 'ok', version, mode: String(j.mode || ''), window: !!j.window };
  }
  function cleanTitle(s) { return String(s || '').replace(/\s*\|\s*Civitai\s*$/i, '').replace(/^Civitai\s*\|\s*/i, '').trim() || 'Civitai 模型'; }
  function candidates(tabs) {
    const rows = [], seen = new Map();
    for (const tab of tabs) {
      const link = modelLink(tab.url);
      if (!link) continue;
      if (seen.has(link.key)) {seen.get(link.key).copies++; continue;}
      const row = {...link, title:cleanTitle(tab.title), tabId:tab.id, windowId:tab.windowId, copies:1};
      seen.set(link.key, row); rows.push(row);
    }
    return rows;
  }
  root.CftTabs = {modelLink, cleanTitle, candidates, cftHealth, versionAtLeast, APP_ID, MIN_APP_VERSION};
})(globalThis);
