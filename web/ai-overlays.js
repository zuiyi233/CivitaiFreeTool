/* AI 弹层登记表：每个会挡住界面的弹窗都登记一个「安全关闭」回调。
 *
 * 为什么单独一层：人知道怎么点 X、知道确认框该选什么；AI 只能靠 DOM 猜。
 * 这里把「关哪个弹层、关闭等于取消还是保留」变成可枚举的契约，由
 * POST /api/automation/ui/dismiss / MCP cft_dismiss_ui 一次调用完成。
 *
 * 安全口径：
 * - 确认框关闭 = 取消，绝不代替用户确认危险操作；
 * - 下载文件选择关闭 = 取消本次下载；
 * - 设置未保存关闭 = 继续编辑，不丢修改；
 * - 其余信息/结果/详情/图片查看器关闭 = 仅收起。
 */
(function () {
  "use strict";

  const overlays = new Map();
  let reportTimer = null;
  let lastReport = "";
  let scanTimer = null;

  function connected(el) { return !!(el && el.isConnected); }
  function visible(el) {
    if (!connected(el)) return false;
    const css = getComputedStyle(el);
    return css.display !== "none" && css.visibility !== "hidden";
  }
  function callBridge(method, ...args) {
    try {
      const bridge = window.pywebview && window.pywebview.api;
      if (bridge && typeof bridge[method] === "function") return bridge[method](...args);
    } catch (_) { /* 页面可能还没接上桥 */ }
    return Promise.resolve(null);
  }
  function snapshot() {
    const rows = [];
    for (const [key, entry] of [...overlays.entries()]) {
      if (entry.el && !connected(entry.el)) { overlays.delete(key); continue; }
      rows.push({
        name: entry.name,
        group: entry.group,
        kind: entry.kind,
        title: entry.title || "",
        blocking: entry.blocking !== false,
      });
    }
    return rows;
  }
  function report() {
    if (reportTimer) return;
    reportTimer = setTimeout(() => {
      reportTimer = null;
      const rows = snapshot();
      const sig = JSON.stringify(rows);
      if (sig === lastReport) return;
      lastReport = sig;
      callBridge("set_ui_overlays", rows);
    }, 60);
  }
  function uniqueKey(name) {
    return name + "-" + Math.random().toString(36).slice(2, 9);
  }
  function register(name, close, meta) {
    const opts = meta || {};
    const key = opts.key || uniqueKey(name);
    const entry = {
      key: key,
      name: name,
      close: typeof close === "function" ? close : function () {},
      group: opts.group || "dialogs",
      kind: opts.kind || "overlay",
      title: opts.title || "",
      blocking: opts.blocking !== false,
      el: opts.el || null,
    };
    overlays.set(key, entry);
    report();
    return function unregister() {
      if (overlays.get(key) === entry) { overlays.delete(key); report(); }
    };
  }
  function unregister(key) {
    if (overlays.delete(key)) report();
  }
  function matches(entry, targets) {
    return targets.some((target) =>
      target === "all" || target === "*" ||
      target === entry.name || target === entry.group || target === entry.kind);
  }
  function closeTransient() {
    // 右键菜单/文件夹面板/标签浮层不拦截接口，但会挡住浏览器自动化点击。
    document.querySelectorAll(".ctx-menu").forEach((el) => { el.style.display = "none"; });
    document.querySelectorAll(".mm-menu.open").forEach((el) => el.classList.remove("open"));
    document.querySelectorAll(".folders-panel.open").forEach((el) => el.classList.remove("open"));
    document.querySelectorAll(".lib-tagpop:not([hidden])").forEach((el) => { el.hidden = true; });
    document.querySelectorAll(".cover-hover-preview").forEach((el) => el.remove());
  }
  function dismiss(targets) {
    const list = (Array.isArray(targets) && targets.length)
      ? targets.map((x) => String(x)) : ["all"];
    if (list.includes("all") || list.includes("*") || list.includes("dialogs")) closeTransient();
    const closed = [];
    // 后登记的在上层，先关上层，避免拆掉底层后上层悬空。
    for (const entry of [...overlays.values()].reverse()) {
      if (!matches(entry, list)) continue;
      if (entry.el && !connected(entry.el)) { overlays.delete(entry.key); continue; }
      closed.push(entry.name);
      try { entry.close(); } catch (_) { /* 单个弹层关闭失败不能拖垮整批 */ }
      if (overlays.get(entry.key) === entry) overlays.delete(entry.key);
    }
    report();
    return { closed: closed, remaining: snapshot().map((row) => row.name) };
  }

  function scopeFor(mask) {
    const scope = [mask];
    const next = mask.nextElementSibling;
    if (next && next.classList && next.classList.contains("rename-dialog")) scope.push(next);
    return scope;
  }
  function genericClose(mask) {
    const scope = scopeFor(mask);
    for (const sel of ["#cfCancel", "#dfCancel", "#settingsStay", "[data-ai-cancel]"]) {
      const button = scope.map((root) => root.querySelector(sel)).find(Boolean);
      if (button) { button.click(); return; }
    }
    for (const root of scope) {
      const button = [...root.querySelectorAll("button")].find((btn) =>
        /^(取消|关闭|知道了|继续编辑|返回|暂不|否)$/.test((btn.textContent || "").replace(/\s+/g, "")));
      if (button) { button.click(); return; }
    }
    // 没有可点的安全按钮：直接收起。对纯提示框是关闭，对确认框等同于取消。
    scope.forEach((el) => el.remove());
  }
  function scan() {
    document.querySelectorAll(".rd-mask, .df-mask, .settings-exit-mask").forEach((mask) => {
      if (!visible(mask) || mask.dataset.aiOverlay) return;
      mask.dataset.aiOverlay = "1";
      const scope = scopeFor(mask);
      const titleEl = scope.map((root) => root.querySelector(".rd-title, h2")).find(Boolean);
      const title = (titleEl ? titleEl.textContent : "").trim().slice(0, 80);
      const kind = mask.classList.contains("df-mask") ? "file_choice"
        : mask.classList.contains("settings-exit-mask") ? "settings_unsaved" : "dialog";
      register("dialogs", () => genericClose(mask), {
        key: uniqueKey("auto-" + kind),
        group: "dialogs",
        kind: kind,
        title: title,
        blocking: true,
        el: mask,
      });
    });
  }
  function scheduleScan() {
    if (scanTimer) return;
    scanTimer = setTimeout(() => { scanTimer = null; scan(); report(); }, 0);
  }
  function init() {
    scan();
    new MutationObserver(scheduleScan).observe(document.body, { childList: true, subtree: true });
    // 只兜底清理已经脱离文档的登记项，不主动猜弹层。
    setInterval(() => {
      const before = overlays.size;
      snapshot();
      if (overlays.size !== before) report();
    }, 1500);
    window.addEventListener("pagehide", () => callBridge("set_ui_overlays", []));
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }

  window.CftOverlays = {
    register: register,
    unregister: unregister,
    dismiss: dismiss,
    list: snapshot,
    scan: scan,
  };
  window.cftAiOverlays = window.CftOverlays;
  window.cftDismissOverlays = (targets) => dismiss(targets);
})();
