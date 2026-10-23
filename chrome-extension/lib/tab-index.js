/* CftTabIndex — 只读的「已打开 C 站模型页」索引载荷。
 * 纯逻辑：不碰 DOM、不读页面正文/Cookie/API Key，只使用 tabs 的 title/url/id。
 * 依赖先加载 tabs-common.js（CftTabs.modelLink / candidates / cleanTitle）。
 * 契约见 docs/browser-tabs-bridge.md；URL 与标题在桥侧还会再校验一次。
 */
(function (root) {
  const MAX_ROWS = 2000;
  const CIVITAI_RE = /^(www\.)?civitai\.(com|red)$/i;
  const CDN_RE = /^(image|delivery|media)\.civitai\.(com|red)$/i;
  const EXT_VERSION_RE = /^[0-9A-Za-z][0-9A-Za-z._-]{0,31}$/;

  function isCivitaiPage(raw) {
    try {
      const u = new URL(raw);
      if (!['http:', 'https:'].includes(u.protocol) || u.port || u.username || u.password) return false;
      return CIVITAI_RE.test(u.hostname) || CDN_RE.test(u.hostname);
    } catch (_) {
      return false;
    }
  }

  function buildPayload(tabs, opts) {
    const list = Array.isArray(tabs) ? tabs : [];
    const byTab = new Map();
    for (const tab of list) {
      if (tab && typeof tab.id === 'number') byTab.set(tab.id, tab);
    }
    const rows = root.CftTabs && typeof root.CftTabs.candidates === 'function'
      ? root.CftTabs.candidates(list) : [];
    const items = [];
    for (const row of rows) {
      if (items.length >= MAX_ROWS) break;
      const tab = byTab.get(row.tabId) || {};
      items.push({
        tabId: row.tabId,
        windowId: row.windowId,
        title: row.title,
        url: row.url,
        modelId: row.id,
        versionId: row.version || '',
        active: !!tab.active,
        pinned: !!tab.pinned,
        discarded: !!tab.discarded,
        copies: row.copies || 1,
      });
    }
    const windowIds = new Set();
    let civitai = 0, discarded = 0;
    for (const tab of list) {
      if (!tab) continue;
      if (typeof tab.windowId === 'number') windowIds.add(tab.windowId);
      if (isCivitaiPage(tab.url)) civitai++;
      if (tab.discarded) discarded++;
    }
    return {
      v: 1,
      source: (opts && opts.source) || 'chrome-extension',
      ext_version: (() => {
        const value = String((opts && opts.extVersion) || '').slice(0, 32);
        return EXT_VERSION_RE.test(value) ? value : '';
      })(),
      at: Date.now(),
      counts: {
        tabs: list.length,
        civitai,
        models: rows.length,
        windows: windowIds.size,
        discarded,
      },
      tabs: items,
    };
  }

  root.CftTabIndex = { buildPayload, isCivitaiPage, MAX_ROWS };
})(globalThis);
