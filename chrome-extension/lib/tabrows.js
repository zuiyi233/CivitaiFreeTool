/* CftRows —— 标签页分类 / 汇总 / 过滤 / 排序（纯逻辑，无 DOM、无 chrome，可在 node 下直接单测）
 * 存在的理由：过滤一直在做（tabs-common.modelLink 只认 civitai 的 /models/），但**看不见**——
 * 用户看到的就是"一长串标签页"，于是判断成"没筛选"。本模块把筛选过程本身变成可展示的数据：
 * 每个标签页归入一个 kind，汇总出「共 N 个标签页 → 模型页 X / 图片页 Y / 其它 C 站页 Z / 已忽略 W」，
 * 让"过滤了多少、过滤掉了什么"直接出现在界面上。
 * 只读 URL 与标题，不读页面内容、不取 Cookie。
 */
(function (root) {
  const CIVITAI_HOSTS = ['civitai.com', 'civitai.red'];
  // C 站自身的分区（导航栏口径：3D Models / Articles / Comics / Crucibles / Shop 等）
  const SECTION_OF = [
    [/^\/models(\/|$)/i, 'model'],
    [/^\/images(\/|$)/i, 'image'],
    [/^\/posts(\/|$)/i, 'post'],
    [/^\/articles(\/|$)/i, 'article'],
    [/^\/comics(\/|$)/i, 'comic'],
    [/^\/crucibles(\/|$)/i, 'crucible'],
    [/^\/resources(\/|$)/i, 'resource'],
    [/^\/search(\?|\/|$)/i, 'search'],
    [/^\/user(\/|$)/i, 'user'],
  ];

  function hostOf(raw) {
    try {
      const u = new URL(raw);
      if (!['http:', 'https:'].includes(u.protocol)) return '';
      return String(u.hostname || '').toLowerCase().replace(/^www\./, '');
    } catch (_) { return ''; }
  }
  function isCivitai(host) {
    // CDN（image/delivery/media）也算 C 站页面：它们不是可提交对象，但要让用户看见"为什么被排除"
    return CIVITAI_HOSTS.includes(host) || /^(image|delivery|media)\.civitai\.(com|red)$/.test(host);
  }
  function sectionOf(pathname) {
    for (const [re, name] of SECTION_OF) if (re.test(pathname)) return name;
    return 'other';
  }

  // 单个标签页 → 行描述。kind 为 'model' 时带 modelLink 解析出的 id/version/key。
  function classify(tab) {
    const raw = (tab && tab.url) || '';
    const host = hostOf(raw);
    const out = {
      tabId: (tab && typeof tab.id === 'number') ? tab.id : null,
      windowId: (tab && typeof tab.windowId === 'number') ? tab.windowId : null,
      url: raw,
      title: String((tab && tab.title) || ''),
      host,
      civitai: isCivitai(host),
      section: 'offsite',
      kind: 'ignored',       // model | image | post | list | civitai-other | ignored
      detail: false,         // 是否详情页（带数字 ID）
      discarded: !!(tab && tab.discarded),
      pinned: !!(tab && tab.pinned),
      active: !!(tab && tab.active),
      id: '', version: '', key: '', groupId: '', submittable: false,
    };
    if (!out.civitai) return out;
    let u;
    try { u = new URL(raw); } catch (_) { return out; }
    out.section = sectionOf(u.pathname);
    const detailId = (u.pathname.match(/^\/[a-z-]+\/([1-9]\d*)(?:\/|$)/i) || [])[1] || '';
    out.detail = !!detailId;
    out.id = detailId;
    if (out.section === 'model') {
      const link = root.CftTabs ? root.CftTabs.modelLink(raw) : null;
      if (link) {
        out.kind = 'model';
        out.url = link.url; out.key = link.key; out.id = link.id; out.version = link.version || '';
        out.groupId = link.id; out.submittable = true;
        out.title = root.CftTabs.cleanTitle(tab.title);
        return out;
      }
      // /models/{id} 但 modelLink 拒了（非法 query、带端口等）：仍算模型页，只是不可提交
      if (detailId) { out.kind = 'model'; out.key = 'model:' + detailId; out.groupId = detailId; return out; }
      out.kind = 'list';   // /models?types=... 之类的列表页
      return out;
    }
    if (out.section === 'image' || out.section === 'post') { out.kind = 'image'; return out; }
    if (out.section === 'search' || out.section === 'user') { out.kind = 'list'; return out; }
    if (out.section !== 'other') { out.kind = 'civitai-other'; return out; }
    out.kind = 'civitai-other';
    return out;
  }

  // 全量标签页 → {rows(可提交模型行，已按 key 去重合并), summary(过滤可见性), others(其它 C 站页)}
  function build(tabs) {
    const rows = [], seen = new Map(), others = [];
    const summary = {
      total: (tabs || []).length, civitai: 0, model: 0, modelDetail: 0, image: 0,
      list: 0, civitaiOther: 0, offsite: 0, discarded: 0, merged: 0, windows: 0,
    };
    const windowIds = new Set();
    for (const tab of tabs || []) {
      const c = classify(tab);
      if (!c.civitai) { summary.offsite++; continue; }
      summary.civitai++;
      if (c.discarded) summary.discarded++;
      if (typeof c.windowId === 'number') { summary.windows++; windowIds.add(c.windowId); }
      if (c.kind === 'model') {
        summary.model++;
        if (c.detail) summary.modelDetail++;
        if (seen.has(c.key)) { seen.get(c.key).copies++; summary.merged++; continue; }
        const row = Object.assign({}, c, { copies: 1, enriched: null });
        seen.set(c.key, row); rows.push(row); continue;
      }
      if (c.kind === 'image') summary.image++;
      else if (c.kind === 'list') summary.list++;
      else summary.civitaiOther++;
      others.push(c);
    }
    summary.windows = windowIds.size;
    return { rows, others, summary };
  }

  // 过滤：filters = {sections:[], types:[], baseModels:[], states:[], windows:[], nsfw:'hide'|'show'|'only', q:'', onlyMultiFile:false}
  // 行上的 meta（富化结果）可能还没到——缺字段时按「不排斥」处理，避免信息未回来时列表看起来是空的。
  function applyFilters(rows, filters) {
    const f = filters || {};
    const q = String(f.q || '').trim().toLowerCase();
    const list = Array.isArray(f.sections) && f.sections.length ? new Set(f.sections) : null;
    const types = Array.isArray(f.types) && f.types.length ? new Set(f.types) : null;
    const bases = Array.isArray(f.baseModels) && f.baseModels.length ? new Set(f.baseModels) : null;
    const states = Array.isArray(f.states) && f.states.length ? new Set(f.states) : null;
    const wins = Array.isArray(f.windows) && f.windows.length ? new Set(f.windows) : null;
    return (rows || []).filter((r) => {
      const m = r.meta || {};
      if (list && !list.has(r.section || 'model')) return false;
      if (wins && !wins.has(r.windowId)) return false;
      if (types && m.type) { if (!types.has(String(m.type))) return false; }
      if (bases && m.baseModel) { if (!bases.has(String(m.baseModel))) return false; }
      if (states && r.state) { if (!states.has(String(r.state))) return false; }
      if (f.nsfw === 'hide' && m.nsfw === true) return false;
      if (f.nsfw === 'only' && m.nsfw !== true) return false;
      if (f.onlyMultiFile && m.fileCount != null && !(m.fileCount > 1)) return false;
      if (q) {
        const hay = [r.title, r.id, r.version, m.name, m.creator, m.type, m.baseModel,
          (m.tags || []).join(' '), (m.trainedWords || []).join(' '), r.url].join(' ').toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }

  const SORTS = {
    tab: (a, b) => (a.tabId || 0) - (b.tabId || 0),
    title: (a, b) => String(a.title).localeCompare(String(b.title), 'zh-Hans-CN'),
    type: (a, b) => String((a.meta || {}).type || '').localeCompare(String((b.meta || {}).type || '')),
    size: (a, b) => ((b.meta || {}).totalMB || 0) - ((a.meta || {}).totalMB || 0),
    downloads: (a, b) => ((b.meta || {}).downloadCount || 0) - ((a.meta || {}).downloadCount || 0),
    published: (a, b) => String((b.meta || {}).publishedAt || '').localeCompare(String((a.meta || {}).publishedAt || '')),
    window: (a, b) => (a.windowId || 0) - (b.windowId || 0) || (a.tabId || 0) - (b.tabId || 0),
  };
  function sortRows(rows, key) {
    const fn = SORTS[key] || SORTS.tab;
    return (rows || []).slice().sort(fn);
  }

  // 「已选 N / 共 M」之外还要能说出过滤掉了多少，否则用户仍然不知道系统做了什么
  function summaryText(s) {
    if (!s || !s.total) return '没有打开的标签页';
    const parts = [`共 ${s.total} 个标签页`];
    parts.push(`C 站 ${s.civitai}`);
    parts.push(`可提交模型页 ${s.model}`);
    if (s.merged) parts.push(`重复合并 ${s.merged}`);
    if (s.image || s.list || s.civitaiOther) {
      const o = [];
      if (s.image) o.push(`图片/帖子 ${s.image}`);
      if (s.list) o.push(`列表页 ${s.list}`);
      if (s.civitaiOther) o.push(`其它 C 站页 ${s.civitaiOther}`);
      parts.push(`未纳入提交 ${o.join('、')}`);
    }
    if (s.offsite) parts.push(`非 C 站已忽略 ${s.offsite}`);
    if (s.discarded) parts.push(`休眠标签 ${s.discarded}（信息需联网补全）`);
    if (s.windows > 1) parts.push(`跨 ${s.windows} 个窗口`);
    return parts.join(' · ');
  }

  root.CftRows = { classify, build, applyFilters, sortRows, summaryText, CIVITAI_HOSTS, SORT_KEYS: Object.keys(SORTS) };
})(globalThis);
