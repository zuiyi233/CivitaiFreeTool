/* CivitaiFreeTool 前端逻辑（完整版） */
"use strict";

// ---------- js_api 封装 ----------
// ============ 浏览器模式适配（A/C 方案）：把 window.pywebview.api 映射到本地 HTTP RPC ============
// 后端 browser_bridge 提供 /api/rpc 与界面文件服务；pywebview 窗口模式下这段不生效
// （那时 window.pywebview 已由 pywebview 注入）。界面出问题不会拖死下载任务——
// 页面只是"显示器"，后端（exe）独立常驻。
(function () {
  if (window.pywebview && window.pywebview.api) return;
  if (!(location.protocol === "http:" || location.protocol === "https:")) return;
  window.__browserMode = true;
  window.pywebview = {
    api: new Proxy({}, {
      get: function (_t, name) {
        if (typeof name !== "string") return undefined;
        return async function () {
          const args = Array.prototype.slice.call(arguments);
          const res = await fetch("/api/rpc", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ method: name, args: args }),
          });
          const data = await res.json().catch(function () { return {}; });
          if (data && data.error) throw new Error(data.error);
          return data ? data.result : null;
        };
      },
    }),
  };
  // 心跳：让后端知道页面还在（设置里的「关页面后自动退出」依赖它）
  setInterval(function () { fetch("/api/heartbeat").catch(function () {}); }, 2000);
})();

// 前端错误也写进后端日志（error.log），出问题时可以查
window.addEventListener("error", (e) => {
  try { api.call("log_ui_error", String(e.message || e.error || "?"), String(e.filename || ""), String(e.lineno || ""), String((e.error && e.error.stack) || "")).catch(() => {}); } catch (err) { /* 忽略 */ }
});
window.addEventListener("unhandledrejection", (e) => {
  const r = e.reason || {};
  try { api.call("log_ui_error", "未处理的 Promise 拒绝: " + String(r.message || r), "", "", String(r.stack || "")).catch(() => {}); } catch (err) { /* 忽略 */ }
});

const api = {
  call(method, ...args) {
    if(typeof guardCivitaiAction === "function" && !guardCivitaiAction(method,args))return Promise.resolve(["rp_start","dl_action","rp_add_paths"].includes(method)?false:{ok:false,started:false,requires_api_key:true,msg:"请先填写并保存 Civitai API Key"});
    const result=window.pywebview.api[method](...args);
    if(method==='save_config')return Promise.resolve(result).then(ok=>{if(ok)window.dispatchEvent(new CustomEvent('cft:config-saved',{detail:args[0]}));return ok;});
    return result;
  },
};

// ---------- 工具 ----------
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function fmtSize(n) {
  n = Number(n || 0);
  if (!n) return "-";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return i === 0 ? n + " B" : n.toFixed(1) + " " + u[i];
}

function fmtTime(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function short(s, n = 40) {
  s = String(s == null ? "" : s);
  return s.length > n ? s.slice(0, n) + "…" : s;
}

// 界面缩放（实时应用 + 防抖保存）
// 用 html 根元素 zoom：Chromium 对根缩放会重排视口逻辑尺寸，
// 全宽布局（无 max-width 居中）随之自适应，无水平偏移、无空白
// 注意：root zoom 不改变 vh/vw（布局视口恒定），.content 高度需反向补偿，
// 否则缩放后高度不足（下半空白）或溢出（放大裁切）
let zoomTimer = null;
function applyZoom(v) {
  v = Math.max(60, Math.min(200, Math.round(v)));
  const z = v / 100;
  const content = document.querySelector(".content");
  if (z === 1) {
    document.documentElement.style.zoom = "";
    if (content) content.style.height = "";
  } else {
    document.documentElement.style.zoom = z;
    if (content) content.style.height = "calc(100vh / " + z + ")";
  }
  document.documentElement.style.setProperty("--ui-zoom-factor", String(z));
  document.documentElement.style.setProperty("--app-height", "calc(100vh / " + z + ")");
  document.documentElement.dataset.compactLayout = window.innerWidth / z < 1080 ? "true" : "false";
  document.documentElement.dataset.narrowLayout = window.innerWidth / z < 760 ? "true" : "false";
  if (state && state.cfg) state.cfg.ui_zoom = v;
  window.dispatchEvent(new Event("cft:zoom"));
}
document.addEventListener("wheel", (e) => {
  if (e.altKey && !e.ctrlKey && !e.metaKey && state.mmView === "list" && e.target.closest && e.target.closest("#mmTableWrap, #mmListSizeControl")) {
    e.preventDefault(); setModelListSize(Number(state.cfg.model_list_size || 3) + (e.deltaY < 0 ? 1 : -1), true); return;
  }
  if (e.altKey && !e.ctrlKey && e.target.closest && e.target.closest("#mmMasonry, #mmImageSizeControl")) {
    e.preventDefault(); setMasonrySize(Number(state.cfg.masonry_card_width || 220) + (e.deltaY < 0 ? 20 : -20), true); return;
  }
  if (!e.ctrlKey) return;
  e.preventDefault();
  const cur = Number((state && state.cfg && state.cfg.ui_zoom) || 100);
  applyZoom(cur + (e.deltaY < 0 ? 5 : -5));
  clearTimeout(zoomTimer);
  zoomTimer = setTimeout(async () => {
    try { if (state && state.cfg) await api.call("save_config", state.cfg); } catch (err) {}
  }, 600);
}, { passive: false });

function setStatus(t) {
  const text = String(t ?? "");
  $("#statusText").textContent = text;
  if (text && text !== toastLastStatus) { toastLastStatus = text; showToast(text); }
  if (!text) toastLastStatus = "";
}

// 底部中心胶囊与左下角状态同步；重复轮询不延长提示，不拦截鼠标。
let toastTimer = null, toastHideTimer = null, toastFrame = 0, toastLastStatus = "";
function positionToast() {
  const t = document.getElementById("toast"); if (!t) return;
  const actions = document.getElementById("settingsActions"), z = Number(document.documentElement.style.zoom) || 1;
  const rect = actions?.getBoundingClientRect();
  const bottom = rect && actions.getClientRects().length && rect.top < innerHeight ? Math.max(24, innerHeight - rect.top + 12) : 24;
  t.style.bottom = bottom / z + "px";
}
window.addEventListener("resize", positionToast);
window.addEventListener("cft:zoom", positionToast);
function showToast(msg) {
  const text = String(msg ?? "");
  if (!text) return;
  let t = document.getElementById("toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "toast";
    t.setAttribute("aria-hidden", "true"); // 状态栏保留文本，避免读屏重复播报。
    document.body.appendChild(t);
  }
  clearTimeout(toastTimer); clearTimeout(toastHideTimer); cancelAnimationFrame(toastFrame);
  t.textContent = text;
  t.style.display = "block";
  positionToast();
  toastFrame = requestAnimationFrame(() => { t.style.opacity = "1"; });
  toastTimer = setTimeout(() => {
    t.style.opacity = "0";
    toastHideTimer = setTimeout(() => { t.style.display = "none"; }, 300);
  }, 2600);
}

function confirmBox(msg) {
  // 自定义确认弹窗（原生 confirm 显示地址栏太丑）
  return new Promise((resolve) => {
    const mask = document.createElement("div");
    mask.className = "rd-mask";
    const dlg = document.createElement("div");
    dlg.className = "rename-dialog";
    dlg.style.width = "420px";
    dlg.innerHTML =
      '<div class="rd-title">确认操作</div>' +
      '<div style="font-size:calc(13px * var(--type-scale, 1));color:var(--text);line-height:1.7;word-break:break-all">' + esc(msg).replace(/\n/g, "<br>") + "</div>" +
      '<div class="rd-actions">' +
      ((state.cfg && state.cfg.confirm_buttons_flip)
        ? '<button class="btn" id="cfCancel">取消</button><button class="btn btn-danger" id="cfOk">确定</button>'
        : '<button class="btn btn-danger" id="cfOk">确定</button><button class="btn" id="cfCancel">取消</button>') +
      "</div>";
    document.body.appendChild(mask);
    document.body.appendChild(dlg);
    mask.dataset.aiOverlay = "1";
    // 双击触发的第二次点击会落在刚出现的遮罩上，等于「弹窗一闪就取消」；开窗瞬间的遮罩点击忽略
    const openedAt = Date.now();
    const close = () => { mask.remove(); dlg.remove(); };
    const aiOff = window.CftOverlays
      ? window.CftOverlays.register("confirm", () => { close(); resolve(false); },
          { group: "dialogs", kind: "confirmation", title: "确认操作", el: mask })
      : null;
    const done = () => { if (aiOff) aiOff(); };
    $("#cfCancel", dlg).addEventListener("click", () => { close(); done(); resolve(false); });
    mask.addEventListener("click", () => {
      if (Date.now() - openedAt < 250) return;
      close(); done(); resolve(false);
    });
    $("#cfOk", dlg).addEventListener("click", () => { close(); done(); resolve(true); });
  });
}

// 与 confirmBox 相同，但**不转义**（传已拼好的富文本；会 resolve {ok:true, root} 便于往里面塞缩略图）
function confirmBoxRaw(html, title) {
  return new Promise((resolve) => {
    const mask = document.createElement("div");
    mask.className = "rd-mask";
    const dlg = document.createElement("div");
    dlg.className = "rename-dialog";
    dlg.style.width = "560px";
    dlg.innerHTML =
      '<div class="rd-title">' + (title || "确认操作") + "</div>" +
      '<div style="font-size:calc(13px * var(--type-scale, 1));color:var(--text);line-height:1.7">' + html + "</div>" +
      '<div class="rd-actions">' +
      ((state.cfg && state.cfg.confirm_buttons_flip)
        ? '<button class="btn" id="cfCancel">取消</button><button class="btn btn-danger" id="cfOk">确定</button>'
        : '<button class="btn btn-danger" id="cfOk">确定</button><button class="btn" id="cfCancel">取消</button>') +
      "</div>";
    document.body.appendChild(mask);
    document.body.appendChild(dlg);
    mask.dataset.aiOverlay = "1";
    const openedAt = Date.now();
    const close = () => { mask.remove(); dlg.remove(); };
    const aiOff = window.CftOverlays
      ? window.CftOverlays.register("confirm", () => { close(); resolve(false); },
          { group: "dialogs", kind: "confirmation", title: title || "确认操作", el: mask })
      : null;
    const done = () => { if (aiOff) aiOff(); };
    $("#cfCancel", dlg).addEventListener("click", () => { close(); done(); resolve(false); });
    mask.addEventListener("click", () => {
      if (Date.now() - openedAt < 250) return;
      close(); done(); resolve(false);
    });
    $("#cfOk", dlg).addEventListener("click", () => { done(); mask.remove(); resolve({ ok: true, root: dlg }); });
  });
}

// 批量把封面塞进某个容器里的 img.dd-thumb（查重/确认弹窗用；失败静默）
async function loadDdThumbs(rootEl, paths, size) {
  try {
    const list = (paths || []).filter(Boolean);
    if (!list.length) return;
    const json = await api.call("get_covers", list, size || 96);
    const covers = JSON.parse(json || "{}");
    rootEl.querySelectorAll("img.dd-thumb").forEach((img) => {
      const b64 = covers[img.dataset.path];
      if (b64) img.src = "data:image/jpeg;base64," + b64;
    });
  } catch (e) { /* 缩略图失败不影响功能 */ }
}

// 复制到剪贴板（走后端 Win32；WebView2 file:// 下 navigator.clipboard 不可用）
window.__copyText = async function (t) {
  try { await api.call("copy_text", String(t == null ? "" : t)); return true; }
  catch (e) { return false; }
};

// 报错一键复制：点下载管理里标了 .err-copy 的报错单元格、或点批量下载的解析日志区，直接复制去搜
document.addEventListener("click", function (e) {
  let txt = "";
  const cell = e.target && e.target.closest ? e.target.closest("td.err-copy") : null;
  if (cell) {
    txt = (cell.textContent || "").trim();
  } else if (e.target && e.target.id === "parseLog") {
    txt = (e.target.textContent || "").trim();
  } else {
    return;
  }
  if (!txt) return;
  window.__copyText(txt).then(function (ok) {
    setStatus(ok ? "已复制报错到剪贴板，可直接搜索" : "复制失败（可手动选中复制）");
  });
});

// ---------- 状态 ----------
const state = {
  cfg: null,
  models: [],
  display: [],
  mmChecked: new Set(),
  coverCache: new Map(),   // 前端封面缓存（path → dataURI）：重绘时同步填充，删除/刷新不再闪屏
  coverCacheMulti: new Map(), // 瀑布流轮播帧缓存（path → [[src,isVideo],...]，视频帧多一格首帧静图 [mp4Src,true,posterSrc]）：get_covers_multi 批量预取，不影响单图链路
  mmSel: new Set(),
  mmSort: { col: "name", rev: false },
  mmTypeF: "", mmBaseF: "", mmStF: "", mmFolderF: "", mmAuthorF: "", mmCoverF: "",   // Metro 筛选：底模 / 更新状态 / 文件夹 / 作者 / 封面
  mmUpdOnly: false,          // 只看有更新的模型（更新检测筛选）
  mmUpdItems: null,          // 更新检测结果（path → 记录），「更新」页面用
  mmUpdCheckedAt: 0,
  updSel: new Set(),         //「更新」页面勾选的文件
  updPick: {},               //「更新」页面每行下拉选中的目标版本 {path: versionId}
  updQ: "", updBase: "", updState: "", updSortKey: "date",
  updPer: 50, updPage: 1,
  updSortRev: true,          // 表头排序方向（点击切换）
  updCtxPath: "",            //「更新」页面右键/··· 的目标
  updBatchBusy: false,       // 批量更新进行中（防重复点击）
  mmLastSel: -1,
  mmView: "list",
  rpRunning: false,
  dlTimer: null,
  dlTasks: [],
  dlThumbs: {},   // 下载管理列表缩略图缓存（filename → base64）
};

// ---------- 页面切换 ----------
function switchPage(name) {
  if (name !== "settings" && typeof settingsHaveChanges === "function" && settingsHaveChanges()) {requestSettingsLeave(()=>switchPage(name));return;}
  if(typeof refreshApiKeyGates === "function")refreshApiKeyGates();
  if (name !== "models") fmClose();
  if (name !== "models" && $("#detailMask").style.display === "flex") closeDetail();
  $$(".nav-tab").forEach((t) => t.classList.toggle("active", t.dataset.page === name));
  $$(".page").forEach((p) => p.classList.toggle("active", p.id === "page-" + name));
  if (typeof syncModelInspector === "function") syncModelInspector(name === "models");
  if (typeof applyTextAndWindowAppearance === "function") applyTextAndWindowAppearance();
  if (name === "models") mmScanIfNeeded();
  if (name === "library") libScanIfNeeded();
  if (name === "download") refreshDlTarget();
  if (name === "updates") renderUpdatesPage();
  if (name === "dlmanager" && typeof refreshDownloadHistory === "function") refreshDownloadHistory();
}
let navWheelDelta=0,navWheelLast=0,navWheelDirection=0,navWheelEventAt=0;
$('#navTabs').title='滚轮上下切换页面；Ctrl + 滚轮仍调整界面缩放';
$('#navTabs').addEventListener('wheel',e=>{
  if(e.ctrlKey || e.metaKey || e.altKey || !e.deltaY || Math.abs(e.deltaX)>Math.abs(e.deltaY) || !window.__ready)return;
  e.preventDefault();
  const now=performance.now(),direction=Math.sign(e.deltaY),tabs=[...document.querySelectorAll('#navTabs .nav-tab')];
  if(now-navWheelEventAt>240 || direction!==navWheelDirection)navWheelDelta=0;
  navWheelEventAt=now;navWheelDirection=direction;navWheelDelta+=e.deltaY*(e.deltaMode===1?16:e.deltaMode===2?innerHeight:1);
  if(Math.abs(navWheelDelta)<48 || now-navWheelLast<180)return;
  const active=tabs.findIndex(t=>t.classList.contains('active')),next=Math.max(0,Math.min(tabs.length-1,active+direction));
  navWheelDelta=0;navWheelLast=now;
  if(next!==active){switchPage(tabs[next].dataset.page);tabs[next].scrollIntoView({block:'nearest'});}
},{passive:false});
$("#navTabs").addEventListener("click", (e) => {
  const b = e.target.closest(".nav-tab");
  if (b) switchPage(b.dataset.page);
});

// ================= 批量下载 =================
function addUrlRow(url) {
  const row = document.createElement("div");
  row.className = "url-row";
  row.innerHTML =
    '<input class="input" placeholder="https://civitai.red/models/12345"/>' +
    '<button class="icon-btn" title="新建">＋</button>' +
    '<button class="icon-btn" title="删除">×</button>';
  const inp = row.querySelector("input");
  if (url) inp.value = url;
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addUrlRow(); } });
  // 粘贴多行链接自动拆分为多条（剪贴板常见多个 URL）
  inp.addEventListener("paste", (e) => {
    const text = ((e.clipboardData || window.clipboardData) || {}).getData
      ? (e.clipboardData || window.clipboardData).getData("text") : "";
    if (!text || text.indexOf("\n") < 0) return;  // 单行走默认粘贴
    e.preventDefault();
    const lines = text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    if (!lines.length) return;
    inp.value = lines[0];
    for (let i = 1; i < lines.length; i++) addUrlRow(lines[i]);
  });
  row.querySelectorAll(".icon-btn")[0].addEventListener("click", () => addUrlRow());
  row.querySelectorAll(".icon-btn")[1].addEventListener("click", () => {
    row.remove();
    if (!$("#urlRows").children.length) addUrlRow();
  });
  $("#urlRows").appendChild(row);
  inp.focus();
}

$("#btnAddUrl").addEventListener("click", () => addUrlRow());
$("#btnPaste").addEventListener("click", async () => {
  const txt = await api.call("get_clipboard");
  const lines = String(txt || "").split(/[\r\n]+/).map((x) => x.trim()).filter((x) => x.startsWith("http"));
  if (!lines.length) { setStatus("剪贴板中没有链接"); return; }
  addUrlRow();
  const last = document.querySelector("#urlRows .url-row:last-child input");
  if (last) last.value = lines[0];
  for (let i = 1; i < lines.length; i++) addUrlRow(lines[i]);
  setStatus("已从剪贴板粘贴 " + lines.length + " 条链接");
});
$("#btnTodo").addEventListener("click", () => openTodoDialog());
function openTodoDialog() {
  const mask = document.createElement("div");
  mask.className = "rd-mask";
  const dlg = document.createElement("div");
  dlg.className = "rename-dialog";
  dlg.style.width = "520px";
  dlg.innerHTML =
    '<div class="rd-title">到期提醒清单（到期打开软件时提醒）</div>' +
    '<div class="form-grid" style="grid-template-columns:90px 1fr">' +
    '<label>链接</label><input class="input" id="tdUrl" placeholder="https://civitai.red/models/..." />' +
    "</div>" +
    '<div style="font-size:calc(12px * var(--type-scale, 1));color:var(--text-dim);margin:4px 0 6px 90px">时间自动选择：Early Access 模型按其免费到期时间提醒；其他模型默认 7 天后提醒</div>' +
    '<div class="rd-actions"><button class="btn btn-primary" id="tdAdd">添加</button></div>' +
    '<div style="font-size:calc(13px * var(--type-scale, 1));font-weight:600;margin:10px 0 6px">清单：</div>' +
    '<div id="tdList" style="max-height:200px;overflow:auto;font-size:calc(12px * var(--type-scale, 1));line-height:1.9"></div>' +
    '<div class="rd-actions"><button class="btn" id="tdClose">关闭</button></div>';
  document.body.appendChild(mask);
  document.body.appendChild(dlg);
  const close = () => { mask.remove(); dlg.remove(); };
  mask.addEventListener("click", close);
  $("#tdClose").addEventListener("click", close);
  $("#tdAdd").addEventListener("click", async () => {
    const url = $("#tdUrl").value.trim();
    if (!url) { setStatus("请输入链接"); return; }
    setStatus("检测到期时间 …");
    const res = await api.call("todo_add", url);
    setStatus(res && res.msg ? res.msg : "已添加");
    $("#tdUrl").value = "";
    renderTodoList();
  });
  async function renderTodoList() {
    const r = await api.call("todo_list");
    const box = $("#tdList");
    if (!r || !r.todos || !r.todos.length) { box.innerHTML = "（空）"; return; }
    box.innerHTML = r.todos.map((t) =>
      '<div style="display:flex;gap:6px;align-items:center">' +
      '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + esc(t.url) + '">' + esc(t.label || t.url) + "</span>" +
      (t.due ? '<span style="color:var(--danger)">已到期</span>' : "<span>" + t.remain_days + " 天后</span>") +
      '<button class="btn btn-tiny" data-dl="' + esc(t.url) + '" title="立即解析并下载这个模型">下载</button>' +
      '<button class="btn btn-tiny" data-del="' + esc(t.url) + '" title="从清单移除"></button></div>').join("");
    box.querySelectorAll("button[data-dl]").forEach((b) => b.addEventListener("click", async () => {
      setStatus("正在解析并加入下载队列 …");
      const res = await api.call("todo_download", b.dataset.dl);
      setStatus(res && res.msg ? res.msg : "已开始下载");
      renderTodoList();
    }));
    box.querySelectorAll("button[data-del]").forEach((b) => b.addEventListener("click", async () => {
      await api.call("todo_remove", b.dataset.del);
      renderTodoList();
    }));
  }
  renderTodoList();
}
// 启动时检查到期待办：直接给「一键下载 / 全部下载」，不用再自己去翻链接
(async function checkTodoDue() {
  try {
    const r = await api.call("todo_due");
    if (!r || !r.due || !r.due.length) return;
    const mask = document.createElement("div");
    mask.className = "rd-mask";
    const dlg = document.createElement("div");
    dlg.className = "rename-dialog";
    dlg.style.width = "470px";
    const rows = r.due.map((t) =>
      '<div style="display:flex;gap:8px;align-items:center;margin:5px 0">' +
      '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:calc(13px * var(--type-scale, 1))" title="' + esc(t.url) + '">' + esc(t.label || t.url) + "</span>" +
      '<button class="btn btn-tiny td-dl" data-url="' + esc(t.url) + '">一键下载</button></div>').join("");
    dlg.innerHTML =
      '<div style="font-size:calc(15px * var(--type-scale, 1));font-weight:600;margin-bottom:6px">到期待办：可以下载了</div>' +
      '<div style="font-size:calc(12px * var(--type-scale, 1));color:var(--text-dim);margin-bottom:8px">这些模型已到免费/可下载时间（下载成功后会自动从清单里移除）：</div>' +
      '<div style="max-height:240px;overflow:auto">' + rows + "</div>" +
      '<div class="rd-actions"><button class="btn btn-primary" id="tdAll">全部下载</button><button class="btn" id="tdGoDl">去下载页</button><button class="btn" id="tdLater">稍后再说</button></div>';
    document.body.appendChild(mask);
    document.body.appendChild(dlg);
    const close = () => { mask.remove(); dlg.remove(); };
    mask.addEventListener("click", close);
    $("#tdLater").addEventListener("click", close);
    $("#tdGoDl").addEventListener("click", () => { switchPage("download"); close(); });
    $("#tdAll").addEventListener("click", async () => {
      setStatus("正在把到期待办加入下载队列 …");
      const res = await api.call("todo_download_all");
      setStatus(res && res.msg ? res.msg : "已开始下载");
      close();
    });
    dlg.querySelectorAll(".td-dl").forEach((b) => b.addEventListener("click", async () => {
      setStatus("正在解析并加入下载队列 …");
      const res = await api.call("todo_download", b.dataset.url);
      setStatus(res && res.msg ? res.msg : "已开始下载");
      b.disabled = true;
      b.textContent = "已加入";
      const row = b.parentElement;
      if (row) row.style.opacity = ".5";
    }));
  } catch (e) { /* 忽略 */ }
})();
$("#btnClearUrls").addEventListener("click", () => {
  $("#urlRows").innerHTML = "";
  addUrlRow();
});
// 批量下载跳转提示：告诉用户任务已排队，等待即可（4 秒自动关闭，也可点按钮）
function showDlNotice() {
  const mask = document.createElement("div");
  mask.className = "rd-mask";
  const dlg = document.createElement("div");
  dlg.className = "rename-dialog";
  dlg.style.width = "380px";
  dlg.innerHTML =
    '<div class="rd-title">已在下载队列中</div>' +
    '<div style="font-size:calc(13px * var(--type-scale, 1));color:var(--text-dim);line-height:1.8">请求已提交解析；多文件版本将在本页弹窗选择，请等待结果，无需重复点击「解析」。</div>' +
    '<div class="rd-actions"><button class="btn btn-primary" id="dnOk">知道了</button></div>';
  document.body.appendChild(mask);
  document.body.appendChild(dlg);
  const timer = setTimeout(close, 4000);
  function close() { clearTimeout(timer); mask.remove(); dlg.remove(); }
  $("#dnOk", dlg).addEventListener("click", close);
  mask.addEventListener("click", close);
}

// Chrome 扩展一键下载成功 → 后端推送：自动切到下载管理页并刷新
window.__extDownloadStarted = function () {
  try {
    switchPage("dlmanager");
    dlRefresh();
    showDlNotice();
  } catch (e) { /* 忽略 */ }
};

$("#btnParse").addEventListener("click", async () => {  const urls = $$("#urlRows .url-row input").map((i) => i.value.trim()).filter(Boolean);
  if (!urls.length) { setStatus("请先输入链接"); return; }
  // 立即跳转到下载管理页，并禁用按钮防止重复点击（用户反馈：等待期重复点击导致重复下载同一模型）
  switchPage("dlmanager");
  showDlNotice();
  const btn = $("#btnParse");
  btn.disabled = true;
  const oldText = btn.textContent;
  btn.textContent = "解析中...";
  try {
  // 分流：HuggingFace 走文件选择；C 站图片页走批量模型下载；其余走 civitai 解析
  const imgUrls = urls.filter((u) => /civitai\.(red|com)\/images\//i.test(u));
  const hfUrls = urls.filter((u) => /huggingface\.co/i.test(u));
  const cvUrls = urls.filter((u) => !/huggingface\.co/i.test(u) && !/civitai\.(red|com)\/images\//i.test(u));
  if (cvUrls.length) {
    const r = await api.call("parse_urls", cvUrls);
    if (r && r.started) {
      setStatus("解析中 0/" + cvUrls.length);
      pollParse();
    }
  }
  for (const u of hfUrls) {
    try {
      const res = JSON.parse((await api.call("hf_list_files", u)) || "{}");
      if (!res.ok) { setStatus("HF 解析失败: " + (res.msg || "")); continue; }
      showHfDialog(res);
    } catch (e) { setStatus("HF 解析失败: " + e); }
  }
  for (const u of imgUrls) {
    try {
      setStatus("图片页解析中...");
      const res = JSON.parse((await api.call("download_image_models", u)) || "{}");
      if (!res.ok) { setStatus("图片解析失败: " + (res.msg || "")); continue; }
      showImgDlResult(res);
      dlRefresh();
    } catch (e) { setStatus("图片解析失败: " + e); }
  }
  } finally {
    btn.disabled = false;
    btn.textContent = oldText;
  }
});

// ===== 图片页模型批量下载结果弹窗 =====
function showImgDlResult(r) {
  const mask = document.createElement("div");
  mask.className = "rd-mask";
  const dlg = document.createElement("div");
  dlg.className = "rename-dialog";
  dlg.style.width = "520px";
  dlg.style.maxHeight = "80vh";
  dlg.style.overflow = "auto";
  const detail = (r.detail || []).map((d) =>
    '<div class="wf-model"><span class="wf-model-ref">' + esc(d.name) + '（' + esc(d.type || "-") + "）</span>" +
    (d.status === "add"
      ? '<span class="wf-model-path">已加入下载队列</span>'
      : d.status === "fail"
        ? '<span class="wf-model-miss">失败：' + esc(d.msg || "未知原因") + "</span>"
        : '<span class="wf-model-miss">本地已存在，跳过</span>') +
    "</div>").join("");
  const failed = (r.detail || []).filter((d) => d.status === "fail").length;
  dlg.innerHTML =
    '<div class="rd-title">图片页模型批量下载</div>' +
    '<div class="wf-info">图片共使用 ' + r.total + ' 个模型 · 新增 ' + r.added + ' · 已存在跳过 ' + r.skipped +
    (failed ? ' · 失败 ' + failed : "") + "</div>" +
    '<div class="wf-nodes" style="margin-top:8px">' + (detail || '<div class="wf-empty">无可用模型</div>') + "</div>" +
    '<div class="rd-actions"><button class="btn btn-primary" id="imgDlOk">知道了</button></div>';
  document.body.appendChild(mask);
  document.body.appendChild(dlg);
  const close = () => { mask.remove(); dlg.remove(); };
  $("#imgDlOk", dlg).addEventListener("click", close);
  mask.addEventListener("click", close);
}

// ===== HuggingFace 文件选择弹窗 =====
let hfCtx = null;
function showHfDialog(info) {
  const mask = document.createElement("div");
  mask.className = "rd-mask";
  const dlg = document.createElement("div");
  dlg.className = "rename-dialog";
  dlg.style.width = "560px";
  dlg.style.maxHeight = "80vh";
  dlg.style.overflow = "auto";
  const files = info.files || [];
  dlg.innerHTML =
    '<div class="rd-title">' + esc(info.repo) + "（" + files.length + " 个文件）</div>" +
    '<div class="hf-list">' + files.slice(0, 300).map((f, i) =>
      '<label class="hf-item"><input type="checkbox" class="hf-cb" data-i="' + i + '" ' +
      (/\.(safetensors|ckpt|pt|pth|bin|onnx|gguf|sft)$/i.test(f.path) ? "checked" : "") + "/> " +
      '<span class="hf-path">' + esc(f.path) + "</span>" +
      '<span class="hf-size">' + (f.size ? fmtSize(f.size) : "") + "</span></label>"
    ).join("") + "</div>" +
    '<div class="rd-actions">' +
    '<button class="btn" id="hfAll">全选</button>' +
    '<button class="btn" id="hfNone">全不选</button>' +
    '<button class="btn btn-primary" id="hfOk">下载所选</button></div>';
  document.body.appendChild(mask);
  document.body.appendChild(dlg);
  hfCtx = info;
  $("#hfAll").addEventListener("click", () => document.querySelectorAll(".hf-cb").forEach((c) => { c.checked = true; }));
  $("#hfNone").addEventListener("click", () => document.querySelectorAll(".hf-cb").forEach((c) => { c.checked = false; }));
  $("#hfOk").addEventListener("click", async () => {
    const sel = [];
    document.querySelectorAll(".hf-cb:checked").forEach((c) => sel.push(files[Number(c.dataset.i)].path));
    const close = () => { mask.remove(); dlg.remove(); hfCtx = null; };
    close();
    if (!sel.length) { setStatus("未选择任何文件"); return; }
    const r = await api.call("hf_enqueue", info.repo, info.rev, sel);
    setStatus("已加入下载队列 " + (r && r.added ? r.added : 0) + " 个文件");
    dlRefresh();
  });
  mask.addEventListener("click", () => { mask.remove(); dlg.remove(); hfCtx = null; });
}

function pollParse() {
  const t = setInterval(async () => {
    const s = await api.call("get_parse_state");
    if (s && s.items) {
      const log = $("#parseLog");
      log.textContent = s.items.map((i) => (i.ok ? "[OK] " : "[失败] ") + (i.url || "") + " " + (i.msg || "")).join("\n");
      setStatus("解析中 " + s.done + "/" + s.total);
      if (s.finished || !s.running) {
        clearInterval(t);
        const paid = (s.items || []).filter((i) => i.paid);
        const ok = (s.items || []).filter((i) => i.ok).length;
        setStatus("解析完成，共加入 " + ok + " 个任务" + (paid.length ? "；" + paid.length + " 个需付费" : ""));
        if (paid.length) showPaidDialog(paid);
        dlRefresh();
        if (ok > 0) switchPage("dlmanager");
      }
    }
  }, 800);
}

// 付费/Early Access 模型处理弹窗
function showPaidDialog(items) {
  const mask = document.createElement("div");
  mask.className = "rd-mask";
  const dlg = document.createElement("div");
  dlg.className = "rename-dialog";
  dlg.style.width = "540px";
  const rows = items.map((it) => {
    const remain = it.deadline ? Math.max(1, Math.ceil((it.deadline - Date.now() / 1000) / 86400)) : 7;
    return '<div class="paid-row" data-url="' + esc(it.url) + '" data-deadline="' + (it.deadline || 0) + '">' +
      '<div style="font-weight:600">需付费（Early Access）</div>' +
      '<div style="font-size:calc(12px * var(--type-scale, 1));color:var(--text-dim);word-break:break-all">' + esc(it.url) + "</div>" +
      '<div style="font-size:calc(12px * var(--type-scale, 1));margin:4px 0 8px">约 ' + remain + ' 天后免费 —— <button class="btn btn-tiny paid-todo">加入待办（自动到期提醒）</button> <button class="btn btn-tiny paid-dl">仍要下载</button></div></div>';
  }).join("");
  dlg.innerHTML =
    '<div class="rd-title">以下模型需要付费或尚未公开</div>' +
    '<div style="max-height:260px;overflow:auto;font-size:calc(13px * var(--type-scale, 1));line-height:1.8">' + rows + "</div>" +
    '<div class="rd-actions"><button class="btn" id="paidClose">知道了</button></div>';
  document.body.appendChild(mask);
  document.body.appendChild(dlg);
  const close = () => { mask.remove(); dlg.remove(); };
  mask.addEventListener("click", close);
  $("#paidClose").addEventListener("click", close);
  dlg.querySelectorAll(".paid-todo").forEach((b) => b.addEventListener("click", async () => {
    const row = b.closest(".paid-row");
    const res = await api.call("todo_add", row.dataset.url, null, Number(row.dataset.deadline) || null);
    setStatus(res && res.msg ? res.msg : "已加入待办");
    b.textContent = "已加入待办";
    b.disabled = true;
  }));
  dlg.querySelectorAll(".paid-dl").forEach((b) => b.addEventListener("click", async () => {
    const row = b.closest(".paid-row");
    setStatus("正在加入下载队列 …");
    const r = await api.call("dl_enqueue_url", row.dataset.url);
    b.textContent = "已提交";
    b.disabled = true;
    if (r && r.started) pollParse();
  }));
}

// ================= 下载目标文件夹（下载页选择 / 设置里预设） =================
async function refreshDlTarget() {
  const el = $("#dlTargetPath");
  if (!el) return;
  try {
    const t = JSON.parse((await api.call("get_download_target")) || "{}");
    if (t.target) {
      el.textContent = t.target;
      el.classList.add("dl-target-set");
    } else {
      el.textContent = "默认下载目录：" + (t.default || "（未设置）");
      el.classList.remove("dl-target-set");
    }
  } catch (e) { /* 忽略 */ }
}

// 从模型目录树里选文件夹（数据源与模型管理页同源：get_folders）
async function pickFolderModal(options = {}) {
  if(document.querySelector(".folder-picker"))return "";
  let data = {};
  try { data = JSON.parse((await api.call("get_folders")) || "{}"); }
  catch (e) { setStatus("读取分类目录失败，请检查模型目录设置后重试"); return ""; }
  const makeRows = info => {
    const root=String(info.root||""),sep=root.includes("\\")||/^[a-z]:/i.test(root)?"\\":"/";
    const out=root && info.exists!==false?[{path:root,label:"模型目录根目录（直接放根下）",depth:0}]:[];
    const walk=(nodes,depth)=>(nodes||[]).forEach(n=>{if(out.length)out.push({path:root.replace(/[\\/]$/,"")+sep+String(n.path||"").replace(/[\\/]/g,sep),label:n.name,depth:depth+1});walk(n.children,depth+1);});
    walk(info.tree||[],0);return out;
  };
  let rows=makeRows(data);
  const key=path=>normalizedModelPath(path).replace(/\/$/,"");
  const inside=(path,parent)=>key(path)===key(parent)||key(path).startsWith(key(parent)+"/");
  const statuses=new Map();
  const checkFolder=async path=>{
    const result=await api.call("folder_picker_status",[path]);
    if(Array.isArray(result))result.forEach(r=>statuses.set(key(r.path),r));
    return statuses.get(key(path))||{exists:rows.some(r=>key(r.path)===key(path)),valid:rows.some(r=>key(r.path)===key(path))};
  };

  const mask = document.createElement("div");
  mask.className = "rd-mask";
  const dlg = document.createElement("div");
  dlg.className = "rename-dialog folder-picker";
  dlg.setAttribute("role", "dialog");
  dlg.setAttribute("aria-modal", "true");
  dlg.setAttribute("aria-labelledby", "fpTitle");
  dlg.innerHTML =
    '<div class="fp-heading"><div class="rd-title" id="fpTitle">' + esc(options.title||"选择下载落地的文件夹") + '</div><button class="btn btn-tiny" id="fpReset">重置尺寸</button></div>' +
    '<p class="fp-hint">模型与 json / 封面一起保存。窗口大小自动适配，也可拖动右下角调整。</p>' +
    '<div class="fp-toolbar"><input class="input" type="search" id="fpSearch" aria-label="搜索分类名称或路径" placeholder="搜索分类名称或路径…"/><span id="fpCount" aria-live="polite"></span></div>' +
    '<div class="fp-view-options"><label><input type="checkbox" id="fpShowPaths"/>显示详细路径</label><span>星标置顶；中键取消收藏，右键管理记录</span></div>' +
    '<div id="fpFavorites" class="fp-favorites" aria-label="收藏文件夹"></div>' +
    '<div id="fpList" role="listbox" aria-label="分类文件夹" tabindex="0"></div>' +
    '<div class="fp-footer"><div class="fp-selected"><span>' + (options.anyFolder?'移动到':'保存到') + '</span><div id="fpSelected"></div></div>' +
    '<div class="rd-actions">' + (options.anyFolder?'<button class="btn" id="fpBrowse">其他文件夹…</button>':'') + '<button class="btn" id="fpCancel">取消</button><button class="btn btn-primary" id="fpOk">确定</button></div></div>';
  document.body.appendChild(mask);
  document.body.appendChild(dlg);
  const previousFocus = document.activeElement;
  const currentTarget = options.target || (state.cfg && state.cfg.download_target_dir);
  let sel = (rows.find((r) => r.path === currentTarget) || rows[0] || {}).path || "";
  const list = $("#fpList", dlg);
  const folded = new Set(Array.isArray(state.cfg.folder_picker_folded) ? state.cfg.folder_picker_folded : []);
  const favorites = new Set(Array.isArray(state.cfg.folder_picker_favorites) ? state.cfg.folder_picker_favorites : []);
  let showPaths = state.cfg.folder_picker_show_paths === true;
  dlg.dataset.showPaths = String(showPaths);
  const savePicker = async values => {
    Object.assign(state.cfg, values);
    if('folder_picker_show_paths' in values){const input=$('[data-key="folder_picker_show_paths"]');if(input)input.checked=values.folder_picker_show_paths;}
    try{if(!await api.call("save_config",values))throw Error("save");return true;}catch(_){setStatus("文件夹偏好保存失败，请重试");return false;}
  };
  const folderName = path => String(path).replace(/[\\/]$/,"").split(/[\\/]/).pop() || "模型目录";
  const favoritesBar = $("#fpFavorites",dlg);
  const renderFavorites = () => {
    favoritesBar.innerHTML = Array.from(favorites).map(path => {
      const status=statuses.get(key(path)),missing=status?!status.exists:!rows.some(r=>key(r.path)===key(path));
      return '<button type="button" class="fp-favorite-chip'+(path===sel?' selected':'')+(missing?' is-missing':'')+'" data-favorite-pick="'+esc(path)+'" data-missing="'+missing+'" title="'+esc(path+(missing?' · 目录失效；中键取消收藏 / 右键删除记录':' · 中键取消收藏 / 右键管理'))+'">'+esc(folderName(path))+'</button>';
    }).join('') || '<span class="fp-favorites-empty">点击目录后的星标，收藏常用角色文件夹</span>';
  };
  const parents = new Map(), branches = new Set(), stack = [];
  const buildParents=()=>{parents.clear();branches.clear();stack.length=0;rows.forEach(row=>{while(stack.length&&stack.at(-1).depth>=row.depth)stack.pop();if(stack.length){parents.set(row.path,stack.at(-1).path);branches.add(stack.at(-1).path);}stack.push(row);});};
  buildParents();
  let visible = rows;
  const mark = () => {
    list.querySelectorAll(".fp-item").forEach((d) => {
      const on = d.dataset.path === sel;
      d.classList.toggle("selected", on);
      d.setAttribute("aria-selected", String(on));
    });
    $("#fpSelected", dlg).textContent = (showPaths ? sel : folderName(sel)) || "请先在设置中配置模型目录";
    $("#fpSelected", dlg).title = sel;
    renderFavorites();
    const status=statuses.get(key(sel));$("#fpOk", dlg).disabled = !sel || !!(status && (!status.exists || (!options.anyFolder && !status.valid)));
  };
  const render = () => {
    const q = $("#fpSearch", dlg).value.trim().toLocaleLowerCase();
    visible = rows.filter((r) => {
      if (q) return (r.label + " " + r.path).toLocaleLowerCase().includes(q);
      let parent = parents.get(r.path);
      while (parent) { if (folded.has(parent)) return false; parent = parents.get(parent); }
      return true;
    });
    list.innerHTML = visible.map((r) =>
      '<div class="fp-item" role="option" data-path="' + esc(r.path) + '" title="' + esc(r.path) + '" style="--fp-depth:' + (q ? 0 : Math.min(r.depth, 10)) + '">' +
      '<div class="fp-name">' + (branches.has(r.path) ? '<button type="button" class="fp-fold" data-fold="' + esc(r.path) + '" aria-label="折叠或展开 ' + esc(r.label) + '" aria-expanded="' + (!folded.has(r.path)) + '">' + _icon('chevron-down') + '</button>' : '<span class="fp-fold-space"></span>') + _icon("folder") + '<span class="fp-folder-label">' + esc(r.label) + '</span><button type="button" class="fp-star" data-favorite="' + esc(r.path) + '" aria-label="' + (favorites.has(r.path)?'取消收藏':'收藏') + ' ' + esc(r.label) + '" aria-pressed="' + favorites.has(r.path) + '">' + _icon('star') + '</button></div>' +
      '<div class="fp-path" title="' + esc(r.path) + '">' + esc(r.path) + "</div></div>").join("") ||
      '<div class="fp-empty">' + (rows.length ? "没有匹配的分类；清空搜索可查看全部" : "尚未配置模型目录，请前往设置添加") + "</div>";
    $("#fpCount", dlg).textContent = visible.length + " / " + rows.length + " 个目录";
    // 搜索仅过滤显示，保留已选目标，并在底部完整展示，防止误改保存位置。
    mark();
  };
  $("#fpShowPaths",dlg).checked = showPaths;
  $("#fpShowPaths",dlg).addEventListener("change",e=>{
    showPaths=e.target.checked;dlg.dataset.showPaths=String(showPaths);savePicker({folder_picker_show_paths:showPaths});mark();
  });
  const refreshRows=async()=>{try{data=JSON.parse((await api.call("get_folders"))||"{}");rows=makeRows(data);buildParents();if(dlg.isConnected)render();}catch(_){setStatus("目录刷新失败，请重试");}};
  const removeRecord=async path=>{
    try{
      const result=await api.call("forget_folder_record",path);if(!result?.ok){setStatus(result?.msg||"记录删除失败，请重试");return;}
      Object.assign(state.cfg,result.preferences||{});
      if('download_target_dir' in (result.preferences||{})){const field=document.querySelector('[data-key="download_target_dir"]');if(field){field.value=state.cfg.download_target_dir;markSettingSaved('download_target_dir');}}
      favorites.clear();(state.cfg.folder_picker_favorites||[]).forEach(p=>favorites.add(p));folded.clear();(state.cfg.folder_picker_folded||[]).forEach(p=>folded.add(p));
      if(result.missing){rows=rows.filter(r=>!inside(r.path,path));buildParents();if(inside(sel,path))sel=rows[0]?.path||"";}
      if(dlg.isConnected)render();refreshDlTarget();setStatus(result.msg||"已移除记录");
    }catch(_){setStatus("记录删除失败，请重试");}
  };
  const unFavorite=async path=>{
    const before=Array.from(favorites),scroll=list.scrollTop;favorites.delete(path);render();list.scrollTop=scroll;
    if(!await savePicker({folder_picker_favorites:Array.from(favorites)})){favorites.clear();before.forEach(p=>favorites.add(p));state.cfg.folder_picker_favorites=before;if(dlg.isConnected)render();}
  };
  favoritesBar.addEventListener("pointerdown",e=>{if(e.button===1&&e.target.closest('[data-favorite-pick]'))e.preventDefault();});
  favoritesBar.addEventListener("auxclick",e=>{const chip=e.target.closest('[data-favorite-pick]');if(e.button!==1||!chip)return;e.preventDefault();unFavorite(chip.dataset.favoritePick);});
  favoritesBar.addEventListener("click",async e=>{
    const chip=e.target.closest("[data-favorite-pick]");if(!chip)return;
    try{const status=await checkFolder(chip.dataset.favoritePick);if(!dlg.isConnected)return;if(!status.exists||(!options.anyFolder&&!status.valid)){renderFavorites();setStatus("目录已失效；可以中键取消收藏，或右键删除记录");return;}
      sel=chip.dataset.favoritePick;mark();list.querySelector('[data-path="'+CSS.escape(sel)+'"]')?.scrollIntoView({block:'nearest'});
    }catch(_){setStatus("目录状态读取失败，请重试");}
  });
  const menu=document.createElement('div');menu.id='fpContext';menu.className='ctx-menu fp-context';menu.setAttribute('role','menu');menu.style.display='none';document.body.appendChild(menu);
  const hideMenu=()=>menu.style.display='none';
  const outsideMenu=e=>{if(!e.target.closest('#fpContext'))hideMenu();};document.addEventListener('pointerdown',outsideMenu);
  const context=async e=>{
    const target=e.target.closest('[data-favorite-pick],.fp-item');if(!target)return;e.preventDefault();const path=target.dataset.favoritePick||target.dataset.path;
    try{const status=await checkFolder(path);if(!dlg.isConnected)return;
      menu.innerHTML='<button class="ctx-item" role="menuitem" data-fp-act="open">'+_icon('folder')+'打开文件夹所在位置</button><button class="ctx-item ctx-danger" role="menuitem" data-fp-act="remove" '+(!favorites.has(path)&&status.exists?'disabled':'')+'>'+_icon('trash')+'删除'+(status.exists?'收藏':'失效记录')+'</button><div class="fp-context-note">只清理记录，不删除磁盘目录或模型</div>';
      menu.dataset.path=path;menu.style.display='block';const z=Number(document.documentElement.style.zoom)||1;menu.style.left=e.clientX/z+'px';menu.style.top=e.clientY/z+'px';const rect=menu.getBoundingClientRect();menu.style.left=Math.max(8,Math.min(e.clientX,innerWidth-rect.width-8))/z+'px';menu.style.top=Math.max(8,Math.min(e.clientY,innerHeight-rect.height-8))/z+'px';
    }catch(_){setStatus("目录状态读取失败，请重试");}
  };
  favoritesBar.addEventListener('contextmenu',context);list.addEventListener('contextmenu',context);
  menu.addEventListener('click',async e=>{const action=e.target.closest('[data-fp-act]');if(!action||action.disabled)return;const path=menu.dataset.path;hideMenu();if(action.dataset.fpAct==='remove')return removeRecord(path);try{const result=await api.call('open_folder_location',path);setStatus(result?.ok?'已打开目录所在位置':result?.msg||'目录打开失败');}catch(_){setStatus('目录打开失败，请重试');}});
  window.addEventListener('resize',hideMenu);window.addEventListener('cft:zoom',hideMenu);
  api.call('folder_picker_status',Array.from(new Set([...favorites,sel].filter(Boolean)))).then(result=>{if(Array.isArray(result)){result.forEach(r=>statuses.set(key(r.path),r));if(dlg.isConnected)render();}}).catch(()=>{});
  list.addEventListener("click", (e) => {
    const star=e.target.closest("[data-favorite]");
    if(star){
      const path=star.dataset.favorite;favorites.has(path)?favorites.delete(path):favorites.add(path);
      savePicker({folder_picker_favorites:Array.from(favorites)});
      const scroll=list.scrollTop;render();list.scrollTop=scroll;
      list.querySelector('[data-favorite="'+CSS.escape(path)+'"]')?.focus();return;
    }
    const toggle = e.target.closest("[data-fold]");
    if (toggle) {
      const path = toggle.dataset.fold;
      folded.has(path) ? folded.delete(path) : folded.add(path);
      savePicker({folder_picker_folded:Array.from(folded)});
      render(); list.querySelector('[data-fold="' + CSS.escape(path) + '"]')?.focus();
      return;
    }
    const it = e.target.closest(".fp-item");
    if (!it) return;
    sel = it.dataset.path;
    mark();
  });
  $("#fpSearch", dlg).addEventListener("input", render);
  render();
  const clampRatio = (v, fallback) => Number.isFinite(Number(v)) ? Math.max(0.2, Math.min(0.96, Number(v))) : fallback;
  const savedSize = (state.cfg && state.cfg.folder_picker_size) || {};
  let preferred = { width: clampRatio(savedSize.width, 0.72), height: clampRatio(savedSize.height, 0.78) };
  const fit = () => {
    const z = Number(document.documentElement.style.zoom) || 1;
    const maxW = Math.max(1, (window.innerWidth - 24) / z);
    const maxH = Math.max(1, (window.innerHeight - 24) / z);
    dlg.style.minWidth = Math.min(420, maxW) + "px";
    dlg.style.minHeight = Math.min(300, maxH) + "px";
    dlg.style.maxWidth = maxW + "px";
    dlg.style.maxHeight = maxH + "px";
    dlg.style.width = Math.min(maxW, Math.max(420, window.innerWidth * preferred.width / z)) + "px";
    dlg.style.height = Math.min(maxH, Math.max(300, window.innerHeight * preferred.height / z)) + "px";
    dlg.dataset.small = maxH < 390 || maxW < 540 ? "true" : "false";
  };
  const remember = () => {
    const rect = dlg.getBoundingClientRect();
    preferred = { width: rect.width / window.innerWidth, height: rect.height / window.innerHeight };
  };
  dlg.addEventListener("pointerup", remember);
  $("#fpReset", dlg).addEventListener("click", () => { preferred = { width: 0.72, height: 0.78 }; fit(); });
  window.addEventListener("resize", fit);
  window.addEventListener("cft:zoom", fit);
  fit();
  $("#fpSearch", dlg).focus();
  return new Promise((resolve) => {
    let closed = false;
    const close = (value = "") => {
      if (closed) return;
      closed = true;
      remember();
      if (state.cfg) state.cfg.folder_picker_size = preferred;
      // 只保存尺寸字段，不顺带提交设置页尚未保存的改动。
      api.call("save_config", { folder_picker_size: preferred }).catch(() => {});
      window.removeEventListener("resize", fit);
      window.removeEventListener("cft:zoom", fit);
      document.removeEventListener("keydown", keydown, true);
      document.removeEventListener("pointerdown",outsideMenu);window.removeEventListener("resize",hideMenu);window.removeEventListener("cft:zoom",hideMenu);menu.remove();
      mask.remove(); dlg.remove();
      if (previousFocus && previousFocus.isConnected) previousFocus.focus();
      resolve(value);
    };
    const keydown = (e) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); if(menu.style.display!=="none")hideMenu();else close(); }
      else if (e.key === "Tab") {
        const buttons = [...dlg.querySelectorAll("button:not(:disabled), input, [tabindex='0']"),...menu.querySelectorAll("button:not(:disabled)")].filter(el=>el.getClientRects().length);
        const first = buttons[0], last = buttons[buttons.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      } else if ((e.key === "ArrowDown" || e.key === "ArrowUp") && document.activeElement === list && visible.length) {
        e.preventDefault();
        const i = visible.findIndex((r) => r.path === sel);
        sel = visible[Math.max(0, Math.min(visible.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))].path;
        mark();
        const selected = list.querySelector(".selected");
        if (selected) selected.scrollIntoView({ block: "nearest" });
      } else if (e.key === "Enter" && document.activeElement === list && sel) { e.preventDefault(); finishSelection(); }
    };
    document.addEventListener("keydown", keydown, true);
    mask.addEventListener("click", () => close());
    $("#fpCancel", dlg).addEventListener("click", () => close());
    let confirming=false;
    const finishSelection=async()=>{if(!sel||confirming)return;confirming=true;$('#fpOk',dlg).disabled=true;try{const status=await checkFolder(sel);if(closed)return;if(!status.exists||(!options.anyFolder&&!status.valid)){setStatus('目标目录已失效，请重新选择；收藏仍可中键取消或右键删除');await refreshRows();return;}close(sel);}catch(_){setStatus('目录状态读取失败，请重试');}finally{confirming=false;if(!closed)mark();}};
    $("#fpOk", dlg).addEventListener("click",finishSelection);
    $('#fpBrowse',dlg)?.addEventListener('click',async()=>{const path=await api.call('pick_dir');if(path){sel=path;mark();}});
  });
}

async function applyDownloadTarget(p) {
  const r = JSON.parse((await api.call("set_download_target", p)) || "{}");
  if (r.ok) {
    state.cfg.download_target_dir = p || "";
    setStatus(r.msg || (p ? "下载将直接保存到：" + p : "已恢复为默认下载目录"));
    await refreshDlTarget();
  } else {
    setStatus(r.msg || "设置失败");
  }
  return r.ok;
}

if ($("#btnDlTarget")) $("#btnDlTarget").addEventListener("click", async () => {
  const p = await pickFolderModal();
  if (p) await applyDownloadTarget(p);
});
if ($("#btnDlTargetReset")) $("#btnDlTargetReset").addEventListener("click", () => applyDownloadTarget(""));

// ================= SHA256 查重 =================
function fmtBytes(n) {
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  n = Number(n) || 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n + " B" : n.toFixed(1) + " " + u[i]);
}

async function mmDedupeFlow() {
  const r = await api.call("mm_dedupe", null);
  if (!r || !r.started) { setStatus((r && r.msg) || "查重未开始"); return; }
  setStatus("查重中：正在计算文件哈希并整理新旧版本（大库需要一会儿）…");
  const timer = setInterval(async () => {
    const p = await api.call("get_mm_progress");
    if (!p) return;
    if (p.running) { setStatus("查重中 " + (p.done || 0) + "/" + (p.total || 0) + (p.msg ? " · " + p.msg : "")); return; }
    clearInterval(timer);
    setStatus(p.msg || "查重完成");
    const dupGroups = Array.isArray(p.result) ? p.result : [];
    const modelGroups = Array.isArray(p.model_groups) ? p.model_groups : [];
    if (dupGroups.length || modelGroups.length) showDedupeDialog(dupGroups, modelGroups);
  }, 800);
}

function fmtDate(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
}

async function showDedupeDialog(groups, modelGroups) {
  groups = groups || [];
  modelGroups = modelGroups || [];
  const totalDups = groups.reduce((s, g) => s + g.dups.length, 0);
  const sizeA = groups.reduce((s, g) => s + g.dups.reduce((x, d) => x + (d.size || 0), 0), 0);
  const totalOlds = modelGroups.reduce((s, g) => s + (g.olds || []).length, 0);
  const sizeB = modelGroups.reduce((s, g) => s + (g.olds || []).reduce((x, o) => x + (o.size || 0), 0), 0);
  const thumb = (p) => '<img class="dd-thumb" data-path="' + esc(p) + '" alt=""/>';

  const secA = groups.length ? (
    '<div class="dedup-sec">完全相同（同哈希 · ' + groups.length + " 组 · " + totalDups + " 个副本）</div>" +
    '<div class="dd-hint">同名同内容只留一份：默认勾选的是「多余的副本」，会移入回收站（可还原）。</div>' +
    groups.map((g, gi) =>
      '<div class="dedup-group">' +
      '<div class="dedup-keep">' + thumb(g.keep) + '<div class="dd-text">保留 <b>' + esc(g.keep_name) + "</b>" +
      '<div class="dedup-dir">' + esc(g.keep_dir) + "</div></div></div>" +
      g.dups.map((d, di) =>
        '<label class="dedup-dup">' + thumb(d.path) +
        '<input type="checkbox" data-g="' + gi + '" data-d="' + di + '" checked/> ' +
        '<div class="dd-text"><span>' + esc(d.name) + "</span>" +
        '<div class="dedup-dir">' + esc(d.dir) + " · " + fmtBytes(d.size) + "</div></div></label>").join("") +
      "</div>").join("")) : "";

  const secB = modelGroups.length ? (
    '<div class="dedup-sec">同模型多版本（' + modelGroups.length + " 组 · 旧版 " + totalOlds + " 个 · 约 " + fmtBytes(sizeB) + "）</div>" +
    '<div class="dd-hint">同一个模型存了多个版本：<b>默认已帮你勾上「旧版」</b>（点下面「旧版共存」就能全部不删）；' +
    "每条都能看缩略图 + 点 去 C 站核对到底是哪一版。</div>" +
    modelGroups.map((g, gi) =>
      '<div class="dedup-group">' +
      '<div class="dedup-keep">' + thumb(g.keep.path) + '<div class="dd-text"><b>' + esc(g.model_name || g.model_id) + "</b> " +
      '<a href="#" class="dd-link" data-url="' + esc(g.url) + '">模型 C 站页面</a>' +
      (g.count > 2 ? '<span style="color:var(--text-dim)"> · 共 ' + g.count + " 个版本</span>" : "") +
      '<div class="dedup-dir">保留（最新） ' + esc(g.keep.ver || g.keep.name) + " · " + esc(g.keep.base || "-") +
      " · " + fmtBytes(g.keep.size) + (g.keep.mtime ? " · " + fmtDate(g.keep.mtime) : "") +
      ' <a href="#" class="dd-link" data-url="' + esc(g.keep.url) + '">这一版</a></div></div></div>' +
      g.olds.map((o, oi) =>
        '<label class="dedup-dup">' + thumb(o.path) +
        '<input type="checkbox" data-mg="' + gi + '" data-o="' + oi + '" checked/> ' +
        '<div class="dd-text"><span>旧版 ' + esc(o.ver || o.name) + "</span>" +
        '<div class="dedup-dir">' + esc(o.base || "-") + " · " + fmtBytes(o.size) + (o.mtime ? " · " + fmtDate(o.mtime) : "") +
        (o.copies > 1 ? " · 另有同名副本 " + o.copies + " 份（在上一节里）" : "") + "</div>" +
        '<div class="dedup-dir">' + esc(o.dir) + ' · <a href="#" class="dd-link" data-url="' + esc(o.url) + '">这一版</a></div></div></label>').join("") +
      "</div>").join("")) : "";

  const mask = document.createElement("div");
  mask.className = "rd-mask";
  const dlg = document.createElement("div");
  dlg.className = "rename-dialog";
  dlg.style.width = "720px";
  dlg.innerHTML =
    '<div class="rd-title">查重结果：' + groups.length + " 组完全相同 + " + modelGroups.length + " 组同模型多版本（可清理约 " + fmtBytes(sizeA + sizeB) + "）</div>" +
    '<div class="dd-hint" style="border:1px solid var(--border);border-radius:8px;padding:6px 8px;margin-bottom:8px">' +
    "<b>勾选 = 移入回收站</b>（可在回收站还原，不会真删）。「<b>删旧留新</b>」= 把所有旧版都勾上；「<b>旧版共存</b>」= 全部取消勾选、什么都不删。</div>" +
    '<div style="max-height:400px;overflow:auto">' + secA + secB + "</div>" +
    '<div class="rd-actions"><button class="btn" id="ddSelAll">全选</button><button class="btn" id="ddSelNone">全不选</button>' +
    '<button class="btn" id="ddOldsAll" title="把所有旧版都勾上（只保留最新版）">删旧留新（勾选旧版）</button>' +
    '<button class="btn" id="ddCoexist" title="取消勾选全部旧版 = 新旧版本都留着，什么都不删">旧版共存（不删）</button>' +
    '<button class="btn" id="ddClose">关闭</button>' +
    '<button class="btn btn-danger" id="ddDel">移入回收站</button></div>';
  document.body.appendChild(mask);
  document.body.appendChild(dlg);
  const close = () => { mask.remove(); dlg.remove(); };
  mask.addEventListener("click", close);
  $("#ddClose", dlg).addEventListener("click", close);
  dlg.querySelectorAll(".dd-link").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    api.call("open_url", a.dataset.url);
  }));
  function updCount() {
    let n = 0;
    dlg.querySelectorAll("input[type=checkbox]:checked").forEach((c) => {
      if (c.dataset.mg !== undefined || c.dataset.g !== undefined) n++;
    });
    $("#ddDel", dlg).textContent = n ? "移入回收站（" + n + " 个）" : "移入回收站";
  }
  dlg.addEventListener("change", updCount);
  $("#ddSelAll", dlg).addEventListener("click", () => { dlg.querySelectorAll("input[type=checkbox]").forEach((c) => (c.checked = true)); updCount(); });
  $("#ddSelNone", dlg).addEventListener("click", () => { dlg.querySelectorAll("input[type=checkbox]").forEach((c) => (c.checked = false)); updCount(); });
  $("#ddOldsAll", dlg).addEventListener("click", () => { dlg.querySelectorAll("input[data-mg]").forEach((c) => (c.checked = true)); updCount(); setStatus("已勾选全部旧版：确认后只保留每个模型的最新版"); });
  $("#ddCoexist", dlg).addEventListener("click", () => { dlg.querySelectorAll("input[data-mg]").forEach((c) => (c.checked = false)); updCount(); setStatus("已取消全部旧版勾选：新旧版本都保留"); });
  updCount();
  // 缩略图（有封面的才显示，失败不影响）
  const allPaths = [];
  groups.forEach((g) => { allPaths.push(g.keep); (g.dups || []).forEach((d) => allPaths.push(d.path)); });
  modelGroups.forEach((g) => { allPaths.push(g.keep.path); (g.olds || []).forEach((o) => allPaths.push(o.path)); });
  loadDdThumbs(dlg, allPaths);

  $("#ddDel", dlg).addEventListener("click", async () => {
    const picked = [];
    dlg.querySelectorAll("input[type=checkbox]:checked").forEach((c) => {
      if (c.dataset.mg !== undefined && c.dataset.mg !== "") {
        const g = modelGroups[Number(c.dataset.mg)];
        const o = g && g.olds[Number(c.dataset.o)];
        if (o) picked.push({ path: o.path, name: o.name, dir: o.dir, ver: o.ver, size: o.size });
      } else if (c.dataset.g !== undefined && c.dataset.g !== "") {
        const g = groups[Number(c.dataset.g)];
        const d = g && g.dups[Number(c.dataset.d)];
        if (d) picked.push({ path: d.path, name: d.name, dir: d.dir, size: d.size });
      }
    });
    if (!picked.length) { setStatus("没有勾选任何要清理的文件"); return; }
    // 带缩略图 + 排版的确认框（confirmBox 会转义 HTML，所以这里用不转义的版本）
    const totalSize = picked.reduce((s, d) => s + (d.size || 0), 0);
    const rows = picked.map((d) =>
      '<div class="cf-row">' + thumb(d.path) + '<div class="dd-text">' +
      "<b>" + esc(d.name) + "</b>" + (d.ver ? ' <span style="color:var(--warn,#d29922)">旧版 ' + esc(d.ver) + "</span>" : " <span style='color:var(--text-dim)'>重复副本</span>") +
      '<div class="dedup-dir">' + esc(d.dir) + (d.size ? " · " + fmtBytes(d.size) : "") + "</div></div></div>").join("");
    const ok = await confirmBoxRaw(
      '<div style="font-size:calc(12px * var(--type-scale, 1));color:var(--text-dim);margin-bottom:6px">以下 <b>' + picked.length +
      "</b> 个文件将移入回收站（可还原，不会真正删除）：</div>" +
      '<div style="max-height:300px;overflow:auto">' + rows + "</div>",
      "清理重复 / 旧版模型（" + picked.length + " 个" + (totalSize ? " · 约 " + fmtBytes(totalSize) : "") + "）");
    if (!ok) return;
    if (ok.root) loadDdThumbs(ok.root, picked.map((d) => d.path));
    if (ok.root) ok.root.remove();          // 关掉确认框（否则清理完它还挂在屏幕上）
    await close();
    const r0 = await api.call("mm_dedupe_delete", picked.map((d) => d.path));
    if (!r0 || !r0.ok) { setStatus((r0 && r0.msg) || "清理失败"); return; }
    setStatus(r0.msg || "开始清理…");
    const timer = setInterval(async () => {
      const p = await api.call("get_mm_progress");
      if (!p) return;
      if (p.running) { setStatus(p.msg || "正在清理…"); return; }
      clearInterval(timer);
      setStatus(p.msg || "清理完成");
      if (Array.isArray(p.result) && p.result.length) {
        infoBox('<div style="font-size:calc(12px * var(--type-scale, 1));line-height:1.8">' +
          p.result.map((f) => "· <b>" + esc(f.file) + "</b>：" + esc(f.msg)).join("<br/>") +
          "</div>", "这些没能清理（可在资源管理器里手动删除）");
      }
      await api.call("scan_models");
      pollMmScan();
    }, 700);
  });
}

if ($("#mmDedupe")) $("#mmDedupe").addEventListener("click", mmDedupeFlow);

// ================= 下载管理 =================
let dlDragging=false;
// 配置版本号轮询：后端（包括 AI / 自动化接口）改了配置后，前端不必等刷新页面才生效。
// 每 2 拍问一次轻量 get_ui_flags：版本号变了拉全量 get_config；无论如何都用 flags 里的
// 几个判定键覆盖 state.cfg —— 这样「页面刚加载、首次轮询之前改了配置」的竞态也能收敛。
let _cfgRev=null,_cfgTick=0,_uiDismissRev=null;
async function pollCfgRev() {
  // 首个周期立即取一次 baseline：否则「页面刚打开 + AI 立刻 dismiss」会把新 rev 当基线吞掉。
  if (++_cfgTick % 2 !== 0 && _cfgRev !== null) return;
  try {
    const flags = await api.call("get_ui_flags");
    if (!flags || typeof flags.cfg_rev !== "number") return;
    if (_cfgRev !== null && flags.cfg_rev !== _cfgRev) {
      _cfgRev = flags.cfg_rev;
      state.cfg = await api.call("get_config");
      const settingsPage = $("#page-settings");
      const dirty = typeof settingsHaveChanges === "function" && settingsHaveChanges();
      if (settingsPage && settingsPage.classList.contains("active") && !dirty
          && typeof buildSettingsForm === "function") buildSettingsForm();
    } else if (_cfgRev === null) {
      _cfgRev = flags.cfg_rev;
    }
    if (!state.cfg) return;          // 全量配置还没加载：下个周期再覆盖
    state.cfg.ask_move_after_download = flags.ask_move_after_download;
    state.cfg.download_target_dir = flags.download_target_dir;
    state.cfg.multi_file_download = flags.multi_file_download;
    state.cfg.route_by_type = flags.route_by_type;
    state.cfg.onboarding_done = flags.onboarding_done;
    state.cfg.auto_guides = flags.auto_guides;
    // 后端（AI/MCP）要求关闭弹层：版本号一变就立即收起，不等页面刷新。
    if (typeof flags.ui_dismiss_rev === "number") {
      const rev = flags.ui_dismiss_rev;
      const applyDismiss = () => {
        const targets = Array.isArray(flags.ui_dismiss_targets) && flags.ui_dismiss_targets.length
          ? flags.ui_dismiss_targets : ["all"];
        if (window.CftOverlays) window.CftOverlays.dismiss(targets);
        else dismissGuideOverlays();
      };
      if (_uiDismissRev === null) {
        _uiDismissRev = rev;
        // 首次轮询晚于 AI 调用时，不能把刚发出的命令当 baseline 吞掉；
        // 只认 10 秒内的新命令，避免重开页面时重放很久以前的关闭目标。
        const at = Number(flags.ui_dismiss_at || 0);
        if (rev > 0 && at > 0 && (Date.now() / 1000 - at) < 10) applyDismiss();
      } else if (rev !== _uiDismissRev) {
        _uiDismissRev = rev;
        applyDismiss();
      }
    }
    applyGuideFlags(flags);
  } catch (_) { /* 桥未就绪：下个周期再试 */ }
}
async function dlRefresh() {
  if(dlDragging)return;
  pollCfgRev();
  try {
    const tasks = await api.call("get_tasks");
    state.dlTasks = tasks || [];
    $("#dlTable").dataset.hasErrors=state.dlTasks.some(t=>t.error||(t.postprocess_errors||[]).length)?"true":"false";
    $("#dlRemoveCompleted").disabled=!state.dlTasks.some(t=>t.status==="done");
    const tbody = $("#dlTable tbody");
    const selPaths = new Set(Array.from(tbody.querySelectorAll("tr.sel-row")).map((tr) => tr.dataset.taskId));
    tbody.innerHTML = state.dlTasks.map((t) => {
      const st = t.hash_mismatch ? (t.manual_binding ? "已关联 · 校验不一致" : "已下载 · 校验不一致") : { pending: "等待中", downloading: "下载中", retrying: "等待重试", done: "已完成", paused: "已暂停", error: "失败", canceled: "已取消" }[t.status] || t.status;
      const prog = t.status === "downloading" ? (Number(t.progress)||0).toFixed(1) + "%" : st;
      const speed = t.speed ? (t.speed / 1048576).toFixed(1) + " MB/s" : "";
      const size = t.total ? fmtSize(t.downloaded) + " / " + fmtSize(t.total) : fmtSize(t.downloaded);
      const thumb = (state.dlThumbs[t.id] || state.dlThumbs[t.filename]) ? '<img class="thumb" src="data:image/jpeg;base64,' + (state.dlThumbs[t.id] || state.dlThumbs[t.filename]) + '" alt=""/>' : '<span class="thumb thumb-empty"></span>';
      const errCell = t.error
        ? "<td class='c-err err-copy' title='点击复制完整报错'>" + esc(t.error) + "</td>"
        : "<td class='c-err'></td>";
      // 收尾失败（元数据/封面/视频/旧版清理）：下载本体没问题，但库里可能缺信息，必须显式提示
      const pp = Array.isArray(t.postprocess_errors) ? t.postprocess_errors : [];
      const ppBadge = pp.length
        ? '<span class="pp-warn" title="' + esc(pp.map(e => (e.stage || "") + ": " + (e.error || "")).join("\n")) + '">收尾未完成</span>'
        : "";
      // 「保存到」列：显示该任务会落在哪（未单独指定时显示全局目标）；点一下可单独换
      // 分流任务必须优先看 routed_dir：dest_dir 要到下载真正开始那一刻才写入，
      // 否则等待中的任务一直显示「默认」，用户看不出它会落进哪个 ComfyUI 类型目录。
      const routed = t.routed_dir || "";
      const destFull = routed || t.dest_dir || "";
      const effFull = destFull || (state.cfg ? (state.cfg.download_target_dir || state.cfg.download_dir || "") : "");
      const md = (state.cfg && state.cfg.models_dir) || "";
      let destShow = effFull;
      if (md && effFull && effFull.toLowerCase().indexOf(md.toLowerCase()) === 0) {
        destShow = effFull.slice(md.length).replace(/^[\\/]+/, "");
      }
      const destTip = (destFull ? destFull : (effFull + "\n（全局目标）"))
        + (routed ? "\n按类型分流：" + (t.route_reason || "") : "") + "\n点击选择该文件的保存文件夹";
      const destLeaf = String(destShow || effFull || "未设置").replace(/[\\/]$/, "").split(/[\\/]/).pop();
      const destCell = "<td class='c-dest cell-dest' data-task='" + esc(t.id) + "' title='" + esc(destTip) + "'><button type='button' class='dest-picker' aria-label='保存到 " + esc(destLeaf) + "'>" + _icon("folder") + "<span>" + esc(destLeaf) + "</span>" + _icon("chevron-down") + "</button>" + (destFull ? (routed ? " <span class='dest-def'>分流</span>" : "") : " <span class='dest-def'>默认</span>") + "</td>";
      return '<tr data-task-id="'+esc(t.id)+'" data-fn="' + esc(t.filename) + '" class="' + (selPaths.has(t.id) ? "sel-row" : "") + '">' +
        '<td class="dl-check-col"><span class="dl-check-controls"><button type="button" class="dl-drag-handle" draggable="true" aria-label="拖动调整下载顺序" data-tip="拖动排序：前面的等待任务优先，不中断当前下载">'+ '<svg class="ic dl-grip" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="8" cy="6" r="1.7"/><circle cx="16" cy="6" r="1.7"/><circle cx="8" cy="12" r="1.7"/><circle cx="16" cy="12" r="1.7"/><circle cx="8" cy="18" r="1.7"/><circle cx="16" cy="18" r="1.7"/></svg>' +'</button><input type="checkbox" class="dl-task-check" aria-label="选择下载任务 ' + esc(t.filename) + '"'+(selPaths.has(t.id)?' checked':'')+'/></span></td>' + "<td class='c-thumb'>" + thumb + "</td><td class='c-file'><div>" + esc(t.filename) + "</div>" +
        (effFull ? '<div class="file-subpath" title="' + esc(effFull + (/[/\\]$/.test(effFull) ? "" : (effFull.includes("\\") || /^[a-z]:/i.test(effFull) ? "\\" : "/")) + t.filename) + '">' +
          esc(effFull + (/[/\\]$/.test(effFull) ? "" : (effFull.includes("\\") || /^[a-z]:/i.test(effFull) ? "\\" : "/")) + t.filename) + "</div>" : "") +
        "</td>" + destCell + "<td class='dl-task-status'>" + esc(st) + ppBadge + (t.hash_mismatch && !t.manual_binding ? '<button type="button" class="btn btn-tiny" data-bind-download="' + esc(t.id) + '" data-tip="仅关联已保存的 C站信息，不修改文件、不代表通过校验">' + _icon('file') + '关联模型信息</button>' : '') + (["done", "error", "canceled"].includes(t.status) ? '<button class="btn btn-tiny task-archive" data-archive-task="' + esc(t.id) + '">' + _icon("clock") + '移入历史</button>' : "") + "</td><td>" + esc(prog) + "</td>" +
        "<td>" + esc(speed) + "</td><td>" + esc(size) + "</td>" + errCell + "</tr>";
    }).join("");
    syncDlSelection();
    loadDlThumbs(state.dlTasks || []);
    if (typeof updateDownloadQueueCount === "function") updateDownloadQueueCount();
    // 下载受限（Early Access/付费）→ 弹窗选择
    maybeAskRestricted(tasks || []);
    // 下载完成 → 询问移动分类（ask_move_after_download 开启且本次会话未询问过）
    maybeAskMove(tasks || []);
  } catch (e) { /* 未就绪 */ }
}

// 下载管理列表缩略图：按文件名缓存，已加载的不重复请求（get_covers 传模型文件路径自动找同目录封面）
const _dlThumbLoading = new Set();
async function loadDlThumbs(tasks) {
  const todo = tasks.filter(t=>!state.dlThumbs[t.id] && !_dlThumbLoading.has(t.id)).slice(0,40);
  if (!todo.length) return;
  todo.forEach(t=>_dlThumbLoading.add(t.id));
  try {
    const result=await api.call("get_task_thumbnails",todo.map(t=>t.id));
    const covers=typeof result==='string'?JSON.parse(result):result||{};
    const done=todo.filter(t=>!covers[t.id] && t.status==='done' && t.dest_dir);
    if(done.length){
      const cached=JSON.parse(await api.call('get_covers',done.map(t=>t.dest_dir+'\\'+t.filename),512)||'{}');
      done.forEach(t=>{const b=cached[t.dest_dir+'\\'+t.filename];if(b)covers[t.id]=b;});
    }
    for(const task of todo)if(covers[task.id]){
      state.dlThumbs[task.id]=covers[task.id];
      const row=$('#dlTable tbody tr[data-task-id="'+CSS.escape(task.id)+'"]');
      if(row){const img=document.createElement('img');img.className='thumb';img.alt='';img.src='data:image/jpeg;base64,'+covers[task.id];row.querySelector('.c-thumb').replaceChildren(img);}
    }
  }catch(_){}finally{todo.forEach(t=>_dlThumbLoading.delete(t.id));}
}

// 下载受限（C 站限制）弹窗选择：花费积分重试 / 加入待办并移除 / 浏览器打开
const _restrictAsked = new Set();
function maybeAskRestricted(tasks) {
  if (_restrictAsking) return;
  const t = (tasks || []).find((x) =>
    x.status === "error" && (x.error || "").includes("暂不可下载") && !_restrictAsked.has(x.id));
  if (!t) return;
  _restrictAsked.add(t.id);
  _restrictAsking = true;
  const mask = document.createElement("div");
  mask.className = "rd-mask";
  const dlg = document.createElement("div");
  dlg.className = "rename-dialog";
  dlg.style.width = "480px";
  dlg.innerHTML =
    '<div class="rd-title">模型下载受限（Early Access / 付费）</div>' +
    '<div style="font-size:calc(12px * var(--type-scale, 1));color:var(--text-dim);word-break:break-all">' + esc(t.filename || "") + "</div>" +
    '<div style="font-size:calc(12px * var(--type-scale, 1));color:var(--text-dim);margin:4px 0 10px">该模型在 C 站暂不可直接下载。若有积分可先在浏览器购买解锁，或加入待办等免费开放。</div>' +
    '<div class="rd-actions">' +
    '<button class="btn btn-primary" id="rkRetry">花费积分/重试下载</button>' +
    '<button class="btn" id="rkTodo">加入待办并移除</button>' +
    '<button class="btn" id="rkSite">浏览器打开</button></div>';
  document.body.appendChild(mask);
  document.body.appendChild(dlg);
  mask.dataset.aiOverlay = "1";
  let aiOff = null;
  const close = () => {
    if (aiOff) aiOff();
    mask.remove(); dlg.remove(); _restrictAsking = false;
  };
  aiOff = window.CftOverlays
    ? window.CftOverlays.register("restricted", close,
        { group: "dialogs", kind: "restricted", title: "模型下载受限", el: mask })
    : null;
  mask.addEventListener("click", close);
  $("#rkRetry").addEventListener("click", async () => {
    close();
    await api.call("dl_action", "retry", [t.filename]);
    dlRefresh();
  });
  $("#rkSite").addEventListener("click", () => {
    const url = t.url || ("https://" + (state.cfg.site_domain || "civitai.red") + "/models/" + (t.filename || ""));
    api.call("open_url", url);
  });
  $("#rkTodo").addEventListener("click", async () => {
    close();
    if (t.url) await api.call("todo_add", t.url);
    await api.call("dl_action", "remove", [t.filename]);
    setStatus("已加入待办并从下载管理移除");
    dlRefresh();
  });
}
let _restrictAsking = false;

// 下载完成移动询问（每个任务仅询问一次）
const _moveAsked = new Set();
let _moveAsking = false;
function maybeAskMove(tasks) {
  if (_moveAsking || !(state.cfg && state.cfg.ask_move_after_download)) return;
  // 已在下载页/设置里选了「下载目标文件夹」→ 下载完成会自动归位，不再弹窗询问
  if (state.cfg && (state.cfg.download_target_dir || "").trim()) return;
  // 已按类型分流的任务也不再问：它已经在 ComfyUI 的正确目录里，弹窗反而会把本体搬出去
  const done = tasks.find((t) => t.status === "done" && t.dest_dir && !t.routed_dir && !_moveAsked.has(t.id));
  if (!done) return;
  _moveAsked.add(done.id);
  _moveAsking = true;
  const mask = document.createElement("div");
  mask.className = "rd-mask";
  const dlg = document.createElement("div");
  dlg.className = "rename-dialog";
  dlg.innerHTML =
    '<div class="rd-title">下载完成：' + esc(done.filename) + "</div>" +
    '<div style="display:flex;gap:14px;align-items:flex-start">' +
    '<div id="mvThumb" style="width:112px;height:112px;border-radius:12px;background:var(--surface2);display:flex;align-items:center;justify-content:center;overflow:hidden;flex-shrink:0;border:1px solid var(--border)"><span style="font-size:calc(30px * var(--type-scale, 1))"></span></div>' +
    '<div style="flex:1;min-width:0">' +
    '<div style="font-size:calc(13px * var(--type-scale, 1));color:var(--text-dim);line-height:1.8">是否移动到分类文件夹？（主文件与 json/封面等附属一起移动）</div>' +
    '<div class="rd-actions" style="margin-top:10px">' +
    '<button class="btn" id="mvNo">不移动</button>' +
    '<button class="btn btn-primary" id="mvYes">选择文件夹</button></div>' +
    "</div></div>";
  document.body.appendChild(mask);
  document.body.appendChild(dlg);
  mask.dataset.aiOverlay = "1";
  let aiOff = null;
  // 关闭 = 不移动（保持下载落点），绝不替用户选目录。
  const close = () => {
    if (aiOff) aiOff();
    clearTimeout(mvTimer); mask.remove(); dlg.remove(); _moveAsking = false;
  };
  aiOff = window.CftOverlays
    ? window.CftOverlays.register("move_after_download", close,
        { group: "dialogs", kind: "move_after_download", title: "下载完成：是否移动", el: mask })
    : null;
  // 左侧显示模型缩略图（同目录 preview.png 封面；封面下载是后台任务，可能稍后才落盘，最多重试 4 次）
  let mvTries = 0;
  let mvTimer = null;
  (function loadMvThumb() {
    api.call("get_covers", [done.dest_dir + "\\" + done.filename], 112).then((json) => {
      try {
        const covers = JSON.parse(json || "{}");
        const b64 = Object.values(covers)[0];
        if (b64) {
          $("#mvThumb", dlg).innerHTML = '<img src="data:image/jpeg;base64,' + b64 + '" style="width:100%;height:100%;object-fit:cover"/>';
          return;
        }
      } catch (e) { /* 忽略 */ }
      // 封面还没落盘：等 2.5s 后重试（最多 4 次，约 10s 内追上封面下载；弹窗关闭则停止）
      if (mvTries < 4 && document.body.contains(dlg)) {
        mvTries++;
        mvTimer = setTimeout(loadMvThumb, 2500);
      }
    }).catch(() => { /* 无封面保持占位 */ });
  })();
  $("#mvNo", dlg).addEventListener("click", close);
  mask.addEventListener("click", close);
  $("#mvYes", dlg).addEventListener("click", async () => {
    const dir = await pickFolderModal();   // 应用内文件夹树（与模型管理同源），不用系统弹窗
    close();
    if (!dir) return;
    const res = await api.call("move_file_to", done.dest_dir + "\\" + done.filename, dir);
    setStatus(res && res.msg ? res.msg : "移动完成");
    dlRefresh();
  });
}

async function bindDownloadInfo(taskId) {
  setStatus('正在读取本地文件并核对哈希…');
  const report = await api.call('inspect_download_binding',taskId);
  if(!report?.ok){setStatus(report?.msg||'无法关联该文件');return;}
  const html = '<p>将本地文件关联到 <b>'+esc(report.model_name)+'</b> · '+esc(report.version_name)+'</p>' +
    '<p class="binding-warning">文件未通过官方 SHA256 校验。此操作仅保存名称、版本和封面等来源信息，不能保证它与官方模型一致，也不改变文件。</p>' +
    '<dl class="binding-hashes"><dt>官方 SHA256</dt><dd>'+esc(report.expected_sha256)+'</dd><dt>本地实际 SHA256</dt><dd>'+esc(report.actual_sha256)+'</dd></dl>';
  document.querySelectorAll('.tip-float').forEach(tip=>{tip.style.display='none';});
  const accepted = await confirmBoxRaw(html,'仅关联信息（校验仍未通过）');
  if(!accepted)return;
  accepted.root.remove();
  const result = await api.call('bind_download_metadata',taskId,report.actual_sha256,true);
  setStatus(result?.msg||'关联失败');
  await dlRefresh();await refreshDownloadHistory();
  if(result?.ok)mmScan();
}

$("#dlTable tbody").addEventListener("click", async (e) => {
  const binding = e.target.closest("[data-bind-download]");
  if(binding){e.stopPropagation();try{await bindDownloadInfo(binding.dataset.bindDownload);}catch(_){setStatus('关联失败，请重新打开窗口检查；未更改校验结果');}return;}
  const archive = e.target.closest("[data-archive-task]");
  if (archive) {
    e.stopPropagation();
    const result = await api.call("archive_download_task", archive.dataset.archiveTask);
    setStatus(result?.msg || "已移入历史");
    await dlRefresh(); await refreshDownloadHistory(); return;
  }
  // 「保存到」列：给这一个任务单独选文件夹（应用内文件夹树，不用系统弹窗）
  const destCell = e.target.closest(".cell-dest");
  if (destCell) {
    const tid = destCell.dataset.task;
    const p = await pickFolderModal();
    if (!p) return;
    const r = await api.call("set_task_target", tid, p);
    setStatus((r && r.msg) || "已更新保存位置");
    dlRefresh();
    return;
  }
  const tr = e.target.closest("tr");
  if (!tr) return;
  const ctrl = e.ctrlKey || e.metaKey;
  if(e.target.matches(".dl-task-check"))tr.classList.toggle("sel-row",e.target.checked);
  else if (ctrl) tr.classList.toggle("sel-row");
  else {
    $$("#dlTable tbody tr").forEach((r) => r.classList.remove("sel-row"));
    tr.classList.add("sel-row");
  }
  syncDlSelection();
});

function dlSel() {
  return Array.from($$("#dlTable tbody tr.sel-row")).map((r) => r.dataset.fn);
}

function dlSelectedIds(){return Array.from(document.querySelectorAll('#dlTable tbody tr.sel-row')).map(r=>r.dataset.taskId);}
function syncDlSelection(){const rows=Array.from(document.querySelectorAll('#dlTable tbody tr'));rows.forEach(r=>{const input=r.querySelector('.dl-task-check');if(input)input.checked=r.classList.contains('sel-row');});const count=rows.filter(r=>r.classList.contains('sel-row')).length;$('#dlSelectAll').checked=rows.length>0&&count===rows.length;$('#dlSelectAll').indeterminate=count>0&&count<rows.length;}
$('#dlSelectAll').addEventListener('change',e=>{document.querySelectorAll('#dlTable tbody tr').forEach(r=>r.classList.toggle('sel-row',e.target.checked));syncDlSelection();});
$('#dlPauseAll').addEventListener('click',()=>dlAct('pause_all'));
$('#dlRemoveAll').addEventListener('click',async()=>{if(await confirmBox('移除全部下载任务？取消未完成下载并清理其 .part 临时文件；不删除模型文件或历史。'))dlAct('remove_all');});
async function dlAct(action) {
  if (["start","pause","retry","remove"].includes(action) && !dlSelectedIds().length) {setStatus("请先勾选下载任务");return;}
  if(action==="remove" && !(await confirmBox("移除选中任务并清理未完成的 .part？已完成模型和历史保留。")))return;
  const result = await api.call("dl_action", action, null, ["start_all","pause_all","remove_all","clear_done","remove_completed","save"].includes(action) ? null : dlSelectedIds());
  if (action === "save") setStatus(result ? "任务及历史已保存" : "记录保存失败，请检查目录写入权限");
  dlRefresh();
}
$("#dlStartSel").addEventListener("click", () => dlAct("start"));
$("#dlStartAll").addEventListener("click", () => dlAct("start_all"));
$("#dlPauseSel").addEventListener("click", () => dlAct("pause"));
$("#dlRetrySel").addEventListener("click", () => dlAct("retry"));
$("#dlRemoveSel").addEventListener("click", () => dlAct("remove"));
// 批量/单选：给任务指定保存文件夹（应用内文件夹树；未勾选=全部任务）
$("#dlSetTarget").addEventListener("click", async () => {
  const sel = dlSel();
  const ids=dlSelectedIds();const tasks = (state.dlTasks || []).filter((t) => !ids.length || ids.includes(t.id));
  if (!tasks.length) { setStatus(sel.length ? "选中的任务里没有可设置的" : "任务列表为空"); return; }
  const p = await pickFolderModal();
  if (!p) return;
  let ok = 0, fail = 0, msg = "";
  for (const t of tasks) {
    const r = await api.call("set_task_target", t.id, p);
    if (r && r.ok) ok++; else { fail++; msg = (r && r.msg) || msg; }
  }
  setStatus("已设置 " + ok + " 个任务的保存位置" + (fail ? ("，失败 " + fail + (msg ? "：" + msg : "")) : ""));
  dlRefresh();
});
$('#dlRemoveCompleted').addEventListener('click',()=>dlAct('remove_completed'));
$("#dlClearDone").addEventListener("click", () => dlAct("clear_done"));
$("#dlSave").addEventListener("click", () => dlAct("save"));

// Download context actions target the right-clicked task ID, never the previous selection.
let dlContextTaskId = null;
$("#dlTable tbody").addEventListener("contextmenu", (e) => {
  const tr = e.target.closest("tr");
  if (!tr) return;
  const t = (state.dlTasks || []).find((x) => x.id === tr.dataset.taskId);
  if (!t) return;
  e.preventDefault();
  dlContextTaskId = t.id;
  const menu = $("#ctxMenu");
  menu.innerHTML =
    '<div class="ctx-item" data-act="dl_folder" data-tip="打开资源管理器并选中该文件">打开所在文件夹</div>' +
    '<div class="ctx-item" data-act="dl_copy" data-tip="复制当前文件名">复制文件名</div>' +
    '<div class="ctx-item" data-act="dl_site" data-tip="在浏览器打开该模型在 C 站的主页">打开C站</div>' +
    '<hr class="ctx-sep"/><button class="ctx-item danger" data-act="dl_remove" data-tip="取消此任务，停止写入后清理未完成 .part；已完成模型与历史保留">' + _icon("trash") + '移除此任务</button>';
  menu.style.display = "block";
  const zf = parseFloat(getComputedStyle(document.documentElement).zoom) || 1;
  menu.style.left = (e.clientX / zf) + "px";
  menu.style.top = (e.clientY / zf) + "px";
  const mr = menu.getBoundingClientRect();
  if (mr.right > window.innerWidth) menu.style.left = Math.max(0, window.innerWidth - mr.width) + "px";
  if (mr.bottom > window.innerHeight) menu.style.top = Math.max(0, window.innerHeight - mr.height) + "px";
});
$("#ctxMenu").addEventListener("click", async (e) => {
  const item = e.target.closest("[data-act^=dl_]");
  if (!item) return;
  const t = (state.dlTasks || []).find((x) => x.id === dlContextTaskId);
  $("#ctxMenu").style.display = "none";
  const act = item.dataset.act;
  if (!t) return;
  if (act === "dl_remove") {
    if(!(await confirmBox("移除此任务："+t.filename+"？未完成的 .part 会在写入停止后清理；已完成模型与历史保留。")))return;
    const result=await api.call("remove_download_task",t.id);
    setStatus(result?.msg||"任务已移除");await dlRefresh();await refreshDownloadHistory();
  } else if (act === "dl_copy") {
    await window.__copyText(t.filename);
    setStatus("文件名已复制: " + t.filename);
  } else if (act === "dl_folder") {
    const full = (t.dest_dir ? t.dest_dir + "\\" : "") + t.filename;
    const r = JSON.parse(await api.call("open_in_folder", full) || "{}");
    setStatus((r && r.ok) ? "已打开所在文件夹" : ((r && r.msg) || "文件不存在或已被移动"));
  } else if (act === "dl_site") {
    const url = t.url || ("https://" + (state.cfg.site_domain || "civitai.red") + "/models/" + t.filename);
    api.call("open_url", url);
  }
});

// ================= 模型管理 =================
function mmScanIfNeeded() {
  if (!state.models.length && !$("#mmScan").disabled) mmScan();
}

async function mmScan() {
  if (!window.__ready) { setStatus("初始化中，请稍候 ..."); await new Promise((r) => setTimeout(r, 600)); return mmScan(); }
  setStatus("正在扫描 ...");
  // fire-and-forget + 独立轮询：不依赖 js_api Promise 完成来启动轮询
  api.call("scan_models").catch(() => {});
  pollMmScan();
}

function pollMmScan() {
  const t = setInterval(async () => {
    const s = await api.call("get_scan_state");
    if (s && !s.running) {
      clearInterval(t);
      try {
        state.models = JSON.parse((await api.call("get_scan_rows")) || "[]");
        const cfg=await api.call("get_config");if(cfg?.model_favorites)state.cfg.model_favorites=cfg.model_favorites;
      } catch (e) {
        state.models = [];
        setStatus("模型列表加载失败：" + ((e && e.message) || e) + "（可重试；持续失败请到设置页打开日志文件夹）");
      }
      // 保留用户的排序与筛选（删除/刷新后不再重置）；勾选只清掉已不存在的模型
      try {
        const alive = new Set(state.models.map((r) => r.path));
        Array.from(state.mmChecked).forEach((p) => { if (!alive.has(p)) state.mmChecked.delete(p); });
      } catch (e) { state.mmChecked.clear(); }
      try {
        const u = await api.call("get_model_updates");   // 缓存的更新检测结果 → 卡片
        if (u && u.items) applyUpdatesToRows(u.items);
      } catch (e) { /* 忽略 */ }
      applyMmFilter();
      // 局域网增强：扫描根状态上报——不可达目录明确警告，不用"0 个模型"误导
      const badRoots = Array.isArray(s.roots) ? s.roots.filter((r) => r && r.status !== "ok") : [];
      if (badRoots.length) {
        const shown = badRoots.slice(0, 3).map((r) => r.path + "（" + (r.error || "不可达") + "）").join("；");
        setStatus("⚠ 以下模型目录不可达：" + shown + (badRoots.length > 3 ? " 等 " + badRoots.length + " 个" : ""));
      } else {
        setStatus(s.msg || "扫描完成：" + state.models.length + " 个模型");
      }
      $("#mmCount").textContent = state.models.length + " 个";
    } else if (s && s.msg) setStatus(s.msg);
  }, 700);
}

function renderMm() {
  setMasonrySize((state.cfg || {}).masonry_card_width, false);
  setModelListSize((state.cfg || {}).model_list_size, false);
  const _scMV=$("#mmMasonryViewport")?.scrollTop||0;
  const _scM = ($("#mmMasonry") || {}).scrollTop || 0;
  const _scT = ($("#mmTableWrap") || {}).scrollTop || 0;
  const rows = state.display.slice();
  if (state.mmSort.col) {
    const c = state.mmSort.col;
    rows.sort((a, b) => {
      let x, y;
      if (c === "size") { x = a.size || 0; y = b.size || 0; }
      else if (c === "mtime") { x = a.mtime || 0; y = b.mtime || 0; }
      else if (c === "name") { x = a.name.toLowerCase(); y = b.name.toLowerCase(); }
      else { x = String(a[c] || "").toLowerCase(); y = String(b[c] || "").toLowerCase(); }
      return x < y ? -1 : x > y ? 1 : 0;
    });
    if (state.mmSort.rev) rows.reverse();
  }
  rows.sort((a,b)=>Number(isModelFavorite(b.path))-Number(isModelFavorite(a.path)));
  applyModelToolbarLock();
  state.display = rows.slice();  // 同步排序后的显示顺序（shift 区间 / data-idx 依赖）
  if (state.mmView === "masonry") {
    renderMasonry(rows);
    if($("#mmMasonryViewport"))$("#mmMasonryViewport").scrollTop=_scMV;
    if ($("#mmMasonry")) $("#mmMasonry").scrollTop = _scM;
    if ($("#mmTableWrap")) $("#mmTableWrap").scrollTop = _scT;
    return;
  }
  // 切回列表时恢复容器显示
  $("#mmTableWrap").style.display = "block";
  $("#mmMasonry").style.display = "none";
  const root = (state.cfg && state.cfg.models_dir) || "";
  const tbody = $("#mmTable tbody");
  tbody.innerHTML = rows.map((r, i) => {
    const rel = root ? r.path.replace(root.replace(/\\/g, "/"), "").replace(/^\//, "") : r.path;
    return '<tr data-idx="' + i + '" data-path="' + esc(r.path) + '" class="' + (state.mmSel.has(r.path) ? "sel-row" : "") + (normalizedModelPath(state.historyFocusPath)===normalizedModelPath(r.path)?" history-focus":"") + '">' +
      '<td class="cell-sel" data-col="sel">' + (state.mmChecked.has(r.path) ? '<span class="cbox on"></span>' : '<span class="cbox"></span>') + "</td>" +
      "<td class='c-name' data-col='name'><div class='ml-wrap'>" +
        '<img data-idx="' + i + '" data-path="' + esc(r.path) + '" class="thumb ml-thumb" alt=""/>' +
        (r.upd && r.upd.has_update ? '<a href="#" class="mm-upd" data-url="' + esc(r.upd.url || "") + '" title="' + esc(updTip(r.upd)) + '">' + _icon("alert") + '</a>' : "") +
        '<div class="ml-txt">' +
          '<div class="ml-1" data-tip="' + esc((r.civitai_name || r.name) + "\n" + String(r.name || "")) + '">' + (isModelFavorite(r.path)?_icon("star","ic model-star"):"")+esc(r.civitai_name || r.name) + "</div>" +
          ((r.civitai_name && r.name && String(r.civitai_name) !== String(r.name)) ? '<div class="ml-2" data-tip="本地文件名">' + esc(r.name) + "</div>" : "") +
          ((r.author || (r.info && r.info.creator)) ? '<div class="ml-3">作者 ' + esc(r.author || (r.info && r.info.creator)) + "</div>" : "") +
        "</div></div></td>" +
      "<td class='c-name' data-col='cname' data-tip='' >" + esc(r.civitai_name || "-") + "</td>" +
      "<td data-col='type'>" + esc(r.type || "-") + "</td><td data-col='base'>" + esc(r.base || "-") + "</td>" +
      "<td class='c-ver' data-col='ver'>" + esc(r.ver || "-") + "</td>" +
      "<td data-col='update'>" + ((r.upd && (r.upd.has_update || r.upd.other_base)) ? '<span class="st-mini upd">' + _icon("alert") + '有更新</span>' : (r.upd ? '<span class="st-mini ok">' + _icon("check") + '已最新</span>' : '<span class="st-mini none">未检查</span>')) + "</td>" +
      "<td data-col='hash'>" + esc(r.hash || "-") + "</td>" +
      "<td data-col='size'>" + fmtSize(r.size) + "</td><td class='c-time' data-col='mtime'>" + fmtTime(r.mtime) + "</td>" +
      "<td class='c-path' data-col='path' data-full='" + esc(rel.replace(/[^\\/]+$/, "")) + "' data-tip='" + esc(r.path) + "'>" + esc(short(rel.replace(/[^\\/]+$/, ""), 26) || "\\") + "</td></tr>";
  }).join("");
  tbody.querySelectorAll("td[data-col]:not([data-col=sel]):not([data-col=name]):not([data-col=path])").forEach(td=>td.title=td.textContent.trim());
  $("#mmCheckLabel").textContent = "已勾选 " + state.mmChecked.size + " 个";
  _applyCoverCache($("#mmTable"));
  loadThumbs(0);
  mmApplyCols();
  if ($("#mmMasonry")) $("#mmMasonry").scrollTop = _scM;
  if ($("#mmTableWrap")) $("#mmTableWrap").scrollTop = _scT;
}

// 分批加载封面缩略图（每次 40 个，避免大传输卡顿）
async function loadUpdThumbs() {
  const imgs = Array.from(document.querySelectorAll("#updTable img.upd-cov[data-path]"));
  const paths = imgs.map((i) => i.dataset.path).filter(Boolean);
  if (!paths.length) return;
  try {
    const json = await api.call("get_covers", Array.from(new Set(paths)));
    const covers = JSON.parse(json || "{}");
    imgs.forEach((i) => { const b = covers[i.dataset.path]; if (b) i.src = "data:image/jpeg;base64," + b; });
  } catch (e) { /* 缩略图失败不影响 */ }
}

function _applyCoverCache(root) {
  // 重绘后立刻用缓存同步填充已加载过的封面（同一 JS 任务内完成 → 不闪屏）
  const scope = root || document;
  scope.querySelectorAll("img.thumb, img.ms-img").forEach((img) => {
    const p = img.dataset.path;
    if (!p || !state.coverCache) return;
    const du = state.coverCache.get(p);
    if (du && !String(img.src || "").startsWith("data:")) {
      img.src = du;
      const w = img.closest(".ms-img-wrap");
      if (w && w.dataset.ph === "loading") w.dataset.ph = "ok";
    }
  });
}
async function loadThumbs(start) {
  if (state.mmView === "list" && Number(state.cfg.model_list_size || 3) < 3) return;
  const batch = state.display.slice(start, start + 40);
  if (!batch.length) return;
  const miss = batch.filter((r) => !state.coverCache.has(r.path));
  if (miss.length) {
    try {
      const json = await api.call("get_covers", miss.map((r) => r.path));
      const covers = JSON.parse(json || "{}");
      if (state.coverCache.size > 4000) state.coverCache.clear();
      for (const [p, b64] of Object.entries(covers)) {
        const du = "data:image/jpeg;base64," + b64;
        state.coverCache.set(p, du);
        document.querySelectorAll(".thumb").forEach((img) => {
          if (img.dataset.path === p) img.src = du;
        });
      }
    } catch (e) { /* 缩略图失败不影响列表 */ }
  }
  loadThumbs(start + 40);
}

// ===== 表头列宽拖拽（v2.1.74 重做）=====
// 规则：拖动某列右缘的分隔线 → 调整【该列】；若该列是"模型信息"（弹性 auto 列），则调整它右边那一列。
// 这样：拖「基础」左边的线 = 调「基础」（分隔线归属直觉一致）；宽度增减由弹性列自动吸收 → 表格永远铺满、右侧不留空。
// 宽度按列名存 localStorage（mm_col_w2），mmApplyCols 重建 colgroup 后自动恢复；双击分隔线 = 重置该列。
(function () {
  let drag = null;
  const KEY = "mm_col_w2";
  const flexName = () => $("#mmTable").dataset.mmFlex || "name"; // 路径优先弹性，路径隐藏时名称兜底
  function thOf(colName) { return document.querySelector('#mmTable th[data-col="' + colName + '"]'); }
  function colOf(colName) { return document.querySelector('#mmTable col.c-' + colName); }
  function visibleNext(th) {
    let el = th.nextElementSibling;
    while (el && el.style.display === "none") el = el.nextElementSibling;
    return (el && el.dataset && el.dataset.col) ? el : null;
  }
  function targetOf(th) {                  // 分隔线（th 右缘）对应目标列：默认左列；左列是弹性列时取右列
    if (th.dataset.col === "sel") return null; // 选择格不是信息列，不能拖成宽列。
    if (th.dataset.col !== flexName()) return th;
    const nx = visibleNext(th);
    return (nx && nx.dataset.col !== flexName()) ? nx : null;
  }
  function applyWidth(colName, px) {
    const v = Math.round(px) + "px";
    const col = colOf(colName); if (col) col.style.width = v;
    const th = thOf(colName); if (th) th.style.width = v;
  }
  function saveWidths() {
    let out = {}; try { out = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (_) {}
    delete out.sel;
    document.querySelectorAll("#mmTable col[class^='c-']").forEach((c) => {
      const name = c.className.replace(/^c-/, "");
      if (name === "sel" || name === flexName()) return;
      if (c.style.width) out[name] = c.style.width; else delete out[name];
    });
    try { localStorage.setItem(KEY, JSON.stringify(out)); } catch (e) { }
    window.mmApplyCols();
  }
  function restoreWidths() {
    let w = {};
    try { w = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { }
    Object.keys(w).forEach((k) => { const px=parseFloat(w[k]); if (k !== "sel" && k !== flexName() && Number.isFinite(px) && px >= 50) applyWidth(k, px); });
  }
  document.addEventListener("mousemove", (e) => {
    if (!drag) return;
    const zf = parseFloat(getComputedStyle(document.documentElement).zoom) || 1;
    const w = Math.max(50, drag.startW + (e.clientX - drag.startX) / zf);
    applyWidth(drag.col, w);
  });
  document.addEventListener("mouseup", () => {
    if (!drag) return;
    const th = thOf(drag.col); if (th) th.classList.remove("resizing");
    saveWidths();
    drag = null;
  });
  function bindResize() {
    document.querySelectorAll("#mmTable th[data-col]").forEach((th) => {
      if (th.querySelector(".col-resize")) return;
      const hd = document.createElement("div");
      hd.className = "col-resize";
      hd.addEventListener("mousedown", (e) => {
        const tg = targetOf(th);
        if (!tg) return;                      // 弹性列两侧（无法定宽）→ 不响应
        e.preventDefault(); e.stopPropagation();
        const targetTh = thOf(tg.dataset.col);
        drag = { col: tg.dataset.col, startX: e.clientX,
                 startW: targetTh ? targetTh.getBoundingClientRect().width / (parseFloat(getComputedStyle(document.documentElement).zoom)||1) : 100 };
        tg.classList.add("resizing");
      });
      hd.addEventListener("dblclick", () => { // 双击 = 重置该列宽度
        const tg = targetOf(th);
        if (!tg) return;
        const col = colOf(tg.dataset.col); if (col) col.style.width = "";
        if (tg) tg.style.width = "";
        saveWidths();
      });
      th.appendChild(hd);
    });
    restoreWidths();
  }
  // 表格渲染后重新绑定（renderMm 调用 mmApplyCols 后）
  const origApply = window.mmApplyCols;
  window.mmApplyCols = function () {
    if (origApply) origApply();
    bindResize();
  };
})();

// 表头 点击 = 全选/取消全选
$("#mmTable thead").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-col=sel]");
  if (!th) return;
  const allSel = state.display.length > 0 && state.display.every((r) => state.mmChecked.has(r.path));
  if (allSel) state.mmChecked.clear();
  else state.display.forEach((r) => state.mmChecked.add(r.path));
  th.innerHTML = '<span class="cbox' + (allSel ? " on" : "") + '"></span> 全选';
  $("#mmCheckLabel").textContent = "已勾选 " + state.mmChecked.size + " 个";
  state.display.forEach((r) => {
    const tr2 = document.querySelector('#mmTable tbody tr[data-path="' + CSS.escape(r.path) + '"]');
    const c2 = tr2 && tr2.querySelector(".cell-sel");
    if (c2) c2.innerHTML = state.mmChecked.has(r.path) ? '<span class="cbox on"></span>' : '<span class="cbox"></span>';
  });
});

// ===== 模型列表列显隐（右键表头） =====
const MM_COLS = [["sel", "勾选"], ["thumb", "缩略图"], ["name", "模型信息"], ["cname", "C站模型名"],
                 ["type", "类型"], ["base", "基础模型"], ["ver", "版本"], ["update", "更新"],
                 ["hash", "哈希"], ["size", "大小"], ["mtime", "下载时间"], ["path", "路径"]];
// 列宽以内容用途为单位；只让一个可见列弹性，避免 fixed 布局把空白均摊给勾选格。
function mmSizeColumns(hidden) {
  const table = $("#mmTable"), scale = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--type-scale")) || 1;
  const widths = {sel:40,name:({1:160,2:220,3:280})[Number(state.cfg.model_list_size)||3]||280,cname:180,type:56,base:88,ver:72,update:80,hash:140,size:80,mtime:136,path:120};
  const flex = hidden.has("path") ? "name" : "path"; table.dataset.mmFlex = flex;
  let saved = {}; try { saved = JSON.parse(localStorage.getItem("mm_col_w2") || "{}"); } catch (_) {}
  let minimum = 0;
  for (const [key, width] of Object.entries(widths)) table.style.setProperty("--mm-col-"+key, width * scale + "px");
  document.querySelectorAll("#mmTable thead th[data-col]").forEach(th => {
    const key=th.dataset.col; if(key===flex || key==="sel") th.style.width="";
    if(hidden.has(key)) return;
    const custom=parseFloat(saved[key]);
    minimum += key!==flex && key!=="sel" && Number.isFinite(custom) && custom>=50 ? custom : (widths[key]||80)*scale;
  });
  table.style.minWidth = Math.ceil(minimum) + "px";
}
function mmApplyCols() {
  let hidden = [];
  const DEFAULT_HIDDEN = ["cname", "hash"];   // 只收起「C站模型名」「哈希」；大小/时间/路径都保留（信息密度优先）
  try {
    // v2 迁移：早期版本默认藏得太多（fixed 布局下会留下空槽 → 右侧一片空白），升级时重置一次
    if (localStorage.getItem("mm_hidden_cols_v") !== "3") {
      hidden = DEFAULT_HIDDEN.slice();
      localStorage.setItem("mm_hidden_cols", JSON.stringify(hidden));
      localStorage.setItem("mm_hidden_cols_v", "3");
    } else {
      hidden = JSON.parse(localStorage.getItem("mm_hidden_cols") || "[]");
    }
  } catch (e) { hidden = DEFAULT_HIDDEN.slice(); }
  const hs = new Set(hidden);
  // 档位只调整模型信息单元格；类型/基础/版本/更新/大小/时间/路径依用户列设置保留。
  // 表头与数据行必须用同一份清单、同步隐藏（否则表头 A 位置/数据 B 位置）
  document.querySelectorAll("#mmTable thead [data-col], #mmTable tbody [data-col]").forEach((el) => {
    el.style.display = hs.has(el.dataset.col) ? "none" : "";
  });
  // ★ 关键根因修复（2026-09 用无头浏览器实测量出来的，勿改回）：
  //   CSS 表格里 display:none 的单元格会让后续单元格"左移"，错误占用 <colgroup> 的列槽
  //   → 列宽整体错位、表格尾部无列覆盖 → 右侧出现固定空白（实测 1280 宽下 237px）。
  //   正确做法：<colgroup> 只重建"可见列"的 <col>（class 与 CSS 的 col.c-* 规则同源），
  //   让可见单元格与 <col> 一一对应；弹性列（c-name = auto）随之吃掉全部剩余宽度 → 永远铺满。
  const cg = document.querySelector("#mmTable colgroup");
  const ths = Array.from(document.querySelectorAll("#mmTable thead th"));
  if (cg) {
    cg.innerHTML = ths.filter((th) => !hs.has(th.dataset.col))
      .map((th) => '<col class="c-' + th.dataset.col + '"/>').join("");
  }
  // 「缩略图」是伪列（缩略图在模型信息单元格里，没有独立 <col>）：用表上的 class 控制
  const tbl = document.getElementById("mmTable");
  if (tbl) {tbl.classList.toggle("no-thumb", hs.has("thumb")); mmSizeColumns(hs);}
}
$("#mmTable thead").addEventListener("contextmenu", (e) => {
  e.preventDefault();
  let hidden = [];
  try { hidden = JSON.parse(localStorage.getItem("mm_hidden_cols") || "[]"); } catch (e) {}
  const hs = new Set(hidden);
  const menu = $("#ctxMenu");
  menu.innerHTML = '<div class="ctx-item" style="font-weight:600;cursor:default">显示列</div>' +
    MM_COLS.map(([c, label]) =>
      '<div class="ctx-item" data-col="' + c + '">' + (hs.has(c) ? '<span class="cbox"></span> ' : '<span class="cbox on"></span> ') + esc(label) + "</div>").join("");
  menu.style.display = "block";
  const zf2 = parseFloat(getComputedStyle(document.documentElement).zoom) || 1;
  menu.style.left = (e.clientX / zf2) + "px";
  menu.style.top = (e.clientY / zf2) + "px";
  for (let k = 0; k < 4; k++) {
    const got = menu.getBoundingClientRect();
    const dx = e.clientX - got.left;
    const dy = e.clientY - got.top;
    if (Math.abs(dx) < 2 && Math.abs(dy) < 2) break;
    menu.style.left = (parseFloat(menu.style.left) + dx) + "px";
    menu.style.top = (parseFloat(menu.style.top) + dy) + "px";
  }
  const mr = menu.getBoundingClientRect();
  if (mr.right > window.innerWidth) menu.style.left = Math.max(0, window.innerWidth - mr.width) + "px";
  if (mr.bottom > window.innerHeight) menu.style.top = Math.max(0, window.innerHeight - mr.height) + "px";
});
$("#ctxMenu").addEventListener("click", (e) => {
  const item = e.target.closest("[data-col]");
  if (!item || !item.dataset.col) return;
  let hidden = [];
  try { hidden = JSON.parse(localStorage.getItem("mm_hidden_cols") || "[]"); } catch (e) {}
  const hs = new Set(hidden);
  if (hs.has(item.dataset.col)) hs.delete(item.dataset.col); else hs.add(item.dataset.col);
  localStorage.setItem("mm_hidden_cols", JSON.stringify(Array.from(hs)));
  mmApplyCols();
  $("#ctxMenu").style.display = "none";
});
// 「显示列」等右键面板：点击面板外或按 Esc 关闭（此前只能靠再次切换列才会关，用户反馈"关不掉"）
document.addEventListener("click", (e) => {
  const m = $("#ctxMenu");
  if (m && m.style.display === "block" && e.target && !(e.target.closest && e.target.closest("#ctxMenu"))) m.style.display = "none";
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { const m = $("#ctxMenu"); if (m) m.style.display = "none"; }
});
// 渲染后应用列显隐（在 renderMm 列表分支后调用）

// ===== 瀑布流视图 =====
function renderMasonry(rows) {
  document.querySelectorAll("#mmMasonry .ms-card").forEach((c) => _stopCarousel(c));  // 重建前清理轮播定时器
  $("#mmTableWrap").style.display = "none";
  $("#mmMasonry").style.display = "block";
  $("#mmMasonry").innerHTML = rows.map((r, i) => {
    const checked = state.mmChecked.has(r.path) ? "checked" : "";
    const _cn = String(r.civitai_name || "").trim();
    const _disp = _cn || String(r.name || "");
    const _sub = (_cn && _cn !== r.name) ? String(r.name || "") : "";
    const _meta = [r.base, r.type].filter(Boolean).map((x) => short(String(x), 16)).join(" · ");
    const _meta2 = [r.ver ? short(String(r.ver), 16) : "", fmtSize(r.size)].filter(Boolean).join(" · ");
    return '<div class="ms-card' + (checked ? " checked" : "") + (normalizedModelPath(state.historyFocusPath)===normalizedModelPath(r.path)?' history-focus':'') + '" tabindex="0" data-idx="' + i + '" data-path="' + esc(r.path) + '" title="' + esc(String(r.name || "") + " · " + String(r.path || "")) + '">' +
      '<span class="ms-check">' + (checked ? '<span class="cbox on"></span>' : '<span class="cbox"></span>') + "</span>" +
      _msTag(r) +
      (r.upd && r.upd.has_update ? '<a href="#" class="ms-upd" data-url="' + esc(r.upd.url || "") + '" title="' + esc(updTip(r.upd)) + '">' + _icon("alert") + '</a>' : "") +
      '<div class="ms-img-wrap" data-ph="loading"><img class="ms-img" data-idx="' + i + '" data-path="' + esc(r.path) + '" alt=""/></div>' +
      '<div class="ms-info"><div class="ms-titles"><div class="ms-name" data-civitai-name="' + (_cn ? 'true' : 'false') + '">' + (isModelFavorite(r.path)?_icon('star','ic model-star'):'') + esc(_disp) + "</div>" +
      (_sub ? '<div class="ms-sub">' + esc(_sub) + "</div>" : "") +
      '</div><div class="ms-footer">' +
      (r.author ? '<div class="ms-author" title="' + esc(r.author) + '">作者：' + esc(r.author) + '</div>' : '') +
      '<div class="ms-meta">' + esc(_meta || (r.type || "-")) + "</div>" +
      '<div class="ms-size">' + esc(_meta2 || fmtSize(r.size)) + "</div></div></div></div>";
  }).join("");
  _applyCoverCache($("#mmMasonry"));
  $("#mmCheckLabel").textContent = "已勾选 " + state.mmChecked.size + " 个";
  loadMasonryThumbs(0);
}
async function loadMasonryThumbs(start) {
  const batch = state.display.slice(start, start + 40);
  if (!batch.length) {
    // 所有批次跑完：仍处 loading 的标记为"暂无封面"（保证高度稳定、不再是白块）
    document.querySelectorAll('.ms-img-wrap[data-ph="loading"]').forEach((w) => { w.dataset.ph = "none"; });
    return;
  }
  const _markBatch = (paths, ph) => {
    paths.forEach((pp) => {
      document.querySelectorAll(".ms-img[data-path]").forEach((img) => {
        if (img.dataset.path === pp) {
          const w = img.closest(".ms-img-wrap");
          if (w && w.dataset.ph === "loading") w.dataset.ph = ph;
        }
      });
    });
  };
  const miss = batch.filter((r) => !state.coverCache.has(r.path));
  if (miss.length) {
    const _dpr = Math.max(1, window.devicePixelRatio || 1);
    const _edge = Math.min(960, Math.round(320 * _dpr));   // 高缩放屏按物理像素取图，避免放大发糊
    try {
      const json = await api.call("get_covers", miss.map((r) => r.path), _edge);
      const covers = JSON.parse(json || "{}");
      if (state.coverCache.size > 4000) state.coverCache.clear();
      for (const [p, b64] of Object.entries(covers)) {
        const du = "data:image/jpeg;base64," + b64;
        state.coverCache.set(p, du);
        document.querySelectorAll(".ms-img").forEach((img) => {
          if (img.dataset.path === p) {
            const w = img.closest(".ms-img-wrap");
            img.onload = () => { if (w) w.dataset.ph = "ok"; };
            img.onerror = () => { if (w) w.dataset.ph = "fail"; };
            img.src = du;
          }
        });
      }
      _markBatch(miss.filter((r) => !covers[r.path]).map((r) => r.path), "none");
    } catch (e) {
      _markBatch(miss.map((r) => r.path), "fail");     // 拉取失败：统一显示"封面加载失败"
    }
  }
  // 多图 hover 轮播预取：视口可见的未缓存模型批量请求 1-3 张；滚动时由 _onMmScroll 节流补充
  prefetchMultiForCards(_visibleMasonryCards());
  loadMasonryThumbs(start + 40);
}
// ===== 瀑布流多图预取（渲染批 + 滚动节流共用）与卡片 hover 轮播：仅用已缓存 dataURI 叠层淡入（不发新请求），列表视图不参与 =====
let _multiPrefetching = false, _multiScrollTimer = null, _multiPending = null;
const _multiTried = new Set();   // 本轮会话已请求过的路径：没有多图可给的模型也不该被反复问（滚动一次问一次就是刷屏）
function _visibleMasonryCards() {
  const vh = window.innerHeight || document.documentElement.clientHeight;
  return Array.prototype.filter.call(
    document.querySelectorAll("#mmMasonry .ms-card"),
    (c) => { const b = c.getBoundingClientRect(); return b.top < vh && b.bottom > 0; });
}
async function prefetchMultiForCards(cards) {
  if (!cards || !cards.length) return;
  if (_multiPrefetching) { _multiPending = cards; return; }   // 在途时合并成最后一次请求，不丢弃（丢弃会让 hover 永远等不到帧）
  const need = cards.map((c) => c.dataset.path).filter((p) => p && !state.coverCacheMulti.has(p) && !_multiTried.has(p));
  if (!need.length) return;
  need.forEach((p) => _multiTried.add(p));
  _multiPrefetching = true;
  try {
    const raw = await api.call("get_covers_multi", need);
    const multi = (typeof raw === "string") ? JSON.parse(raw || "{}") : (raw || {});
    if (multi && typeof multi === "object") {
      if (state.coverCacheMulti.size > 4000) { state.coverCacheMulti.clear(); _multiTried.clear(); }   // 两个集合必须一起清：只清缓存会让 _multiTried 把老路径永久挡在门外
      for (const [p, list] of Object.entries(multi)) {
        if (!Array.isArray(list) || !list.length) continue;
        // 帧格式 [[src, isVideo], [mp4Src, true, posterSrc], ...]：src 为 dataURI（本地）或 https/file URL（远程/落盘视频），原样使用
        state.coverCacheMulti.set(p, list
          .filter((f) => Array.isArray(f) && f[0])
          .map((f) => {
            const src = String(f[0]), isVideo = !!f[1];
            // 视频帧的 src 只能是可播直链（后端给 mp4 URL 或本地 file URI），绝不能再套 data:image/jpeg 前缀：
            // 那会把 mp4 地址伪装成 jpeg base64，交回 <img> 又是一次白费带宽且永远解不出图。
            // 第三格是这一帧自己的首帧静图（本地 data: 或 CDN 缩略档），起播前画面全靠它，不能在这里丢掉。
            if (isVideo) return [src, true, String(f[2] || ""), Number.isFinite(Number(f[3])) ? Number(f[3]) : -1];
            return [/^data:/i.test(src) || /^https?:/i.test(src) || /^file:/i.test(src) ? src : "data:image/jpeg;base64," + src, false];
          }));
        if (typeof _startCarousel !== "function") continue;
        // 正在 hover 该卡片且刚拿到多图 → 立即启动轮播
        document.querySelectorAll("#mmMasonry .ms-card:hover").forEach((cd) => { if (cd.dataset.path === p) _startCarousel(cd, p); });
      }
    }
  } catch (e) { /* 多图预取失败静默：无 hover 轮播，单图链路不受影响；失败的路径放回待请求，下次滚动/hover 还能补上 */
    need.forEach((p) => _multiTried.delete(p));
  }
  finally {
    _multiPrefetching = false;
    if (_multiPending) { const again = _multiPending; _multiPending = null; prefetchMultiForCards(again); }
  }
}
function _onMmScroll() {
  if (_multiScrollTimer) return;
  _multiScrollTimer = setTimeout(() => { _multiScrollTimer = null; prefetchMultiForCards(_visibleMasonryCards()); }, 300);
}
// 必须用 capture：真正滚动的是 main.content（html/body 与 #mmMasonry 自己都不滚，scrollHeight==clientHeight），
// 而元素上的 scroll 事件不冒泡——挂在 window 的冒泡监听和挂在 #mmMasonry 的监听历史上一次都没触发过，
// 于是首屏之后多图帧再也不会预取，hover 无帧可轮。捕获阶段能收到任意滚动容器的 scroll。
window.addEventListener("scroll", _onMmScroll, { passive: true, capture: true });
const _masonryCarousel = new WeakMap(); // card → 播放器状态（帧列表、游标、在途预载、归属）
const _autoPlayers = new Set();         // 自动轮播中的卡片（WeakMap 不可遍历，另存一份可迭代的登记）
const _MM_CAROUSEL_MS = 3000;   // 悬停轮播：约 3 秒/帧（用户约定）
const _MM_AUTO_MS = 3000;       // 自动轮播：每拍只推进一张卡片，整屏不会同时闪
const _MM_FADE_MS = 350;        // 淡入时长，与 CSS .ms-img-alt 的 transition 保持一致
function _reducedMotion() { try { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { return false; } }
function _masonryShown() { const el = document.getElementById("mmMasonry"); return !!el && el.offsetParent !== null; }
function _carouselPlayer(card, path, auto) {
  const old = _masonryCarousel.get(card);
  if (old) return old;
  const list = state.coverCacheMulti && state.coverCacheMulti.get(path);
  if (!Array.isArray(list) || !_carouselHasFrames(list)) return null;   // 只有一张静帧就没必要轮
  const wrap = card.querySelector(".ms-img-wrap");
  if (!wrap) return null;
  const plan = _carouselFramePlan(list);
  const st = { card, wrap, list, idx: plan.start, start: plan.start, videoOnly: plan.videoOnly, cur: null, loading: false, seq: 0, dead: new Set(), timer: null, auto: !!auto };
  _masonryCarousel.set(card, st);
  return st;
}
// 帧计划：正常列表第 1 张是本地静帧封面，轮播从第 2 张起；首帧就是视频说明这模型本地没有静帧封面
// （卡片主位只剩占位图），此时从第 0 帧起，否则主位一直空着。
// videoOnly = 封面之后没有任何静帧：全视频模型，自动轮播也必须能播，不然整卡在瀑布流里哑火。
function _carouselFramePlan(frames) {
  const start = frames[0] && frames[0][1] ? 0 : 1;
  const rest = frames.slice(start).filter((f) => f && f[0]);
  return { start, videoOnly: rest.length > 0 && rest.every((f) => f[1]) };
}
function _carouselHasFrames(frames) {
  if (!Array.isArray(frames) || !frames.length) return false;
  const plan = _carouselFramePlan(frames);
  return frames.length >= 2 || plan.videoOnly;
}
// ---------- 视频首帧抓帧回传（详情主视频与瀑布流 hover 共用） ----------
// 后端是纯 stdlib + PIL，解不了 H.264，能拿出首帧像素的只有浏览器：视频真正播到 readyState>=2 时离屏 canvas
// 抓一帧回传 save_model_video_poster，落进「模型名.images/image_XX.jpg」，之后详情与瀑布流都按本地静帧读。
// 手法与资源库 libCapturePoster 同一条（长边 640 / JPEG q0.82 / 失败静默），只是落点与 RPC 不同。
const _modelPosterTried = new Set();   // key = path#index：抓不到（跨域画布被污染）也算试过，绝不逐帧重试刷爆 RPC
function _canvasFlat(ctx, w, h) {
  // 画布还没解出画面时 drawImage 不报错，端出来是一张纯黑：这种图既不该回传也不该落盘。
  // 判据与后端 image_gallery.is_flat_image 同一条（≤4 色或三通道标准差都 <3），两边改动都要同步另一侧。
  let d;
  try { d = ctx.getImageData(0, 0, w, h).data; } catch (e) { return false; }   // 跨域取样被拒：不判，交给 toDataURL 那条路走
  const uniq = new Set(); const s = [0, 0, 0], sq = [0, 0, 0]; let n = 0;
  const step = 4 * Math.max(1, Math.floor(w * h / 4096));   // 最多取 4096 点，大帧不必逐像素
  for (let i = 0; i + 2 < d.length; i += step) {
    uniq.add((d[i] << 16) | (d[i + 1] << 8) | d[i + 2]);
    s[0] += d[i]; s[1] += d[i + 1]; s[2] += d[i + 2];
    sq[0] += d[i] * d[i]; sq[1] += d[i + 1] * d[i + 1]; sq[2] += d[i + 2] * d[i + 2];
    n++;
  }
  if (uniq.size <= 4) return true;    // 采样点凑不出几种颜色：没有画面
  let maxStd = 0;
  for (let k = 0; k < 3; k++) {
    const m = s[k] / n;
    maxStd = Math.max(maxStd, Math.sqrt(Math.max(0, sq[k] / n - m * m)));
  }
  return maxStd < 3;
}
function captureModelVideoPoster(video, path, infoIndex) {
  const idx = Number(infoIndex);
  if (!video || !path || !(idx >= 0)) return Promise.resolve("");   // 序号认不出就不抓：落错 image_XX 会把首帧写进别的帧
  const key = path + "#" + idx;
  if (_modelPosterTried.has(key)) return Promise.resolve("");
  if (_modelPosterTried.size > 4000) _modelPosterTried.clear();   // 与 lib.posterTried 同一把尺子，长会话不至于无限堆
  _modelPosterTried.add(key);
  return new Promise((resolve) => {
    let settled = false;
    const wantSrc = String(video.src || "");   // src 属性解析后的绝对地址，不随重定向变化，正适合判「这个元素还在播我要的那一帧吗」
    const finish = (value, retriable) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      video.removeEventListener("loadeddata", onData);
      video.removeEventListener("error", onErr);
      // 被换走 src 而放弃的要退回「未试过」：详情主视频与瀑布流都会复用同一个 <video> 元素改 src
      if (retriable) _modelPosterTried.delete(key);
      resolve(value);
    };
    const grab = () => {
      // 此刻这个元素播的已经不是这一帧了（切缩略图、换卡片）：再抓就是把别的帧的首帧写成 image_XX.jpg
      if (String(video.src || "") !== wantSrc) { finish("", true); return; }
      let du = "";
      try {
        const vw = Number(video.videoWidth || 0), vh = Number(video.videoHeight || 0);
        if (!vw || !vh) { finish(""); return; }
        const scale = Math.min(1, 640 / Math.max(vw, vh));
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round(vw * scale));
        canvas.height = Math.max(1, Math.round(vh * scale));
        const ctx = canvas.getContext("2d");
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        if (_canvasFlat(ctx, canvas.width, canvas.height)) { finish("", true); return; }
        // 解出画面之前 drawImage 给的是一张纯黑：回传它等于把「首帧空白」换成「首帧一块黑」，
        // 还会被后端当成「本地已有首帧」而不再让外壳取帧。退回「未试过」，播起来之后还有机会抓对的。
        du = canvas.toDataURL("image/jpeg", 0.82);   // 跨域视频到这里会抛 SecurityError，画布脏了就抓不到
      } catch (e) { finish(""); return; }           // 静默：正在播的视频不受影响，下次打开也不该重试
      if (!du || du.length * 0.75 > 2 * 1024 * 1024) { finish(""); return; }
      api.call("save_model_video_poster", path, idx, du)
        .then((r) => finish(r && r.ok ? du : ""))
        .catch(() => finish(""));
    };
    const onData = () => grab();
    const onErr = () => finish("");
    const timer = setTimeout(() => finish(""), 8000);   // 解码超时：这一帧就放弃
    video.addEventListener("loadeddata", onData);
    video.addEventListener("error", onErr);
    if (Number(video.readyState || 0) >= 2) grab();   // 挂监听前首帧已解好（本地 file:// 常发生）
  });
}
// 视频帧的 info.images[] 序号：后端在第四格直接给（get_covers_multi 的 [mp4, true, poster, infoIndex]）。
// 老帧/详情 cover 没有这一格时，只能按约定落盘的本地名 video_XX.mp4 反推——远程 mp4 直链里不含序号，
// 猜不出来就别猜：抓错序号会把别帧的首帧写成这个文件名，宁可继续空白。
function _frameInfoIndex(frame) {
  const n = Number(Array.isArray(frame) ? frame[3] : NaN);
  if (Number.isInteger(n) && n >= 0) return n;
  return _videoFrameInfoIndex(Array.isArray(frame) ? frame[0] : frame);
}
function _videoFrameInfoIndex(src) {
  const m = /[\\/]video_(\d+)\.[a-z0-9]+(?:[?#].*)?$/i.exec(String(src || ""));
  return m ? Number(m[1]) - 1 : -1;
}
// 本地落盘的视频/图片要交给 media 元素时用这一条：浏览器兜底模式的页面是 http origin，
// Chromium 直接拒绝 file:/// 子资源（实测控制台 "Not allowed to load local resource"），
// 于是 v2.6.29 的「本地优先」在这条模式下等于视频根本不播。同源 /api/media 流式端点才是
// 本地文件的读盘通道（只放行模型根目录内的绝对路径，且支持 Range）。窗口模式（pywebview 的
// file:// 页面）里 file:/// 同源可用，原样返回不动它。
function _playableMediaUri(src) {
  const s = String(src || "");
  if (!window.__browserMode || !/^file:/i.test(s)) return s;
  try {
    const u = new URL(s);
    let p = decodeURIComponent(u.pathname || "");
    p = (u.hostname && u.hostname !== "localhost") ? "//" + u.hostname + p : p.replace(/^\/([A-Za-z]:)/, "$1");
    return p ? "/api/media?p=" + encodeURIComponent(p) : s;
  } catch (e) { return s; }   // 解析不了就保持原值，最多退回改前那一步，绝不静默换成空 src
}
// 详情 cover → info.images[] 序号：save_model_video_poster 按这个序号落 image_XX.jpg，错一位就把首帧写进别的帧。
// covers 里没有这个字段，只能回认：本地已落盘的视频名字里带着序号，否则用 orig_url 对回 info.images[].url。
function _coverVideoInfoIndex(info, c) {
  const local = _videoFrameInfoIndex((c || {}).url);
  if (local >= 0) return local;
  const u = String((c || {}).orig_url || (c || {}).url || "");
  const images = Array.isArray((info || {}).images) ? info.images : [];
  return u ? images.findIndex((im) => String((im || {}).url || "") === u) : -1;
}
function _nextFrame(fn) {
  let done = false;
  const run = () => { if (done) return; done = true; fn(); };
  try { window.requestAnimationFrame(() => run()); } catch (e) { /* 没有 rAF 就只靠超时 */ }
  setTimeout(run, 48);   // rAF 不跑时（后台标签页、最小化窗口）也要把帧显出来，不能永远停在 opacity:0
}
function _carouselMount(st, el) {
  // 归属检查：本轮已经停播（鼠标移开 / 卡片滚出视口）而预载才回来时，绝不能把帧挂上去——
  // 那会在卡片上留下一张没人负责清理的孤儿叠图，直到下次重渲染才消失。
  if (_masonryCarousel.get(st.card) !== st) { if (el.tagName === "VIDEO") { try { el.pause(); } catch (_) {} } return false; }
  const prev = st.cur;
  st.wrap.append(el);           // 新帧压在旧帧之上（同为 absolute inset:0，后插入的在上层）
  st.cur = el;
  // 关键：先按 CSS 的 opacity:0 插入，下一帧再抬到 1，过渡才会真的跑。
  // 以前这里在 append 之前就把 style.opacity 写成 "1"，等于自己把淡入废掉了，所以切换很生硬。
  // 但只分两步还不够：刚插入的元素在这一帧之前没有被算过样式，浏览器没有"变化前样式"可比，
  // rAF 里改 opacity 会被当成初始值直接跳到 1（实测 computed opacity 从 1 起步、过渡根本没跑）。
  // 读一次 getComputedStyle 强制这一次样式计算，把 opacity:0 钉成变化前的值，淡入才真的存在。
  try { getComputedStyle(el).opacity; } catch (e) { /* 取不到就算了，最多退回无淡入的硬切 */ }
  _nextFrame(() => { el.style.opacity = "1"; });   // 超时兜底：后台标签页/最小化窗口里 rAF 不跑，否则叠层会永远停在 opacity:0
  if (prev) {
    // 旧帧保持不透明直到新帧淡到位再摘：交叉期间两张都半透明会透出底下的主封面，反而闪一下
    let dropped = false;
    const drop = () => {
      if (dropped) return;
      dropped = true;
      if (prev.tagName === "VIDEO") { try { prev.pause(); } catch (_) {} }
      prev.remove();
    };
    el.addEventListener("transitionend", drop, { once: true });
    setTimeout(drop, _MM_FADE_MS + 260);   // 隐藏标签页里 transition 可能不派发，超时兜底
  }
  return true;
}
function _carouselRearm(st) {
  if (_masonryCarousel.get(st.card) !== st) return;   // 停播后回来的帧不再排下一拍，否则留下无人回收的定时器
  if (st.auto) return;                                // 自动模式的节拍归全局调度器，卡片自己不挂定时器
  clearInterval(st.timer);
  st.timer = setInterval(() => { if (!st.card.isConnected) { _stopCarousel(st.card); return; } _carouselAdvance(st); }, _MM_CAROUSEL_MS);
}
function _carouselStillFrame(st, src, my) {
  // 静帧挂载（含视频帧的首帧静图）：data: 立即可用；远程档先预载，失败记 dead 并继续排拍，不能停摆
  if (/^data:/i.test(src)) {
    const alt = document.createElement("img");
    alt.className = "ms-img ms-img-alt"; alt.alt = ""; alt.decoding = "async";
    alt.src = src;
    _carouselMount(st, alt);
    st.idx = st.idx + 1; _carouselRearm(st);
    return;
  }
  const pre = new Image();
  let done = false;
  const finish = (ok) => {
    if (done) return;
    done = true;
    if (my !== st.seq) return;   // 期间已另起一帧请求：这一份整体作废，也不去动它的 loading
    st.loading = false;
    if (!ok) { st.dead.add(src); st.idx = st.idx + 1; _carouselRearm(st); return; }   // 失败也要继续排拍，不能停摆
    const alt = document.createElement("img");
    alt.className = "ms-img ms-img-alt"; alt.alt = ""; alt.decoding = "async";
    alt.src = src;
    _carouselMount(st, alt);
    st.idx = st.idx + 1;
    _carouselRearm(st);
  };
  pre.onload = () => finish(true);
  pre.onerror = () => finish(false);
  st.loading = true;
  pre.src = src;
  setTimeout(() => finish(false), 6000);   // 挂起帧 6s 超时跳过
}
function _carouselAdvance(st) {
  if (_masonryCarousel.get(st.card) !== st || st.loading) return;
  const list = st.list;
  const my = ++st.seq;    // 每次推进递增：预载回来时若已不是最新请求就丢弃
  for (let tries = 0; tries < list.length; tries++) {
    if (st.idx >= list.length) st.idx = (Number.isFinite(st.start) ? st.start : 1);   // 主封面固定第 1 张，轮播从第 2 张起并在此回卷（全视频模型从第 0 帧起）
    const frame = list[st.idx] || [];
    const src = String(frame[0] || ""), isVideo = !!frame[1], poster = String(frame[2] || "");
    if (isVideo) {
      // 自动轮播默认不播视频帧：一屏多路解码不划算，悬停时才播。但视频帧现在自带首帧静图，
      // 有静图就以静帧身份参与轮播——否则视频多的模型在自动模式下整帧被跳过，卡片看着就是呆的。
      if (st.auto && !st.videoOnly) {
        if (!poster || st.dead.has(poster)) { st.idx = st.idx + 1; continue; }   // 静图也走同一份 dead 名单：拉挂过的档别再每拍重试
        _carouselStillFrame(st, poster, my); return;
      }
      // 视频帧只交给 <video>：src 是 mp4 直链或本地 file URI。协议不对就直接跳过这一帧，
      // 绝不落到下面的 new Image()——真实库 479 条 images[] 里 107 条（22.3%）是 .mp4，
      // 图片预载拿到的是 content-type: video/mp4 的 1.28–2.31MB 视频字节，图片永远解码不出来却已经花掉带宽。
      if (!/^(https?:|file:)/i.test(src)) { st.idx = st.idx + 1; continue; }
      const v = document.createElement("video");
      v.className = "ms-img ms-img-alt"; v.muted = true; v.loop = true; v.autoplay = true;
      v.playsInline = true; v.preload = "metadata";
      // 首帧静图写进 poster：preload="metadata" 的 <video> 在播起来之前就是一块空白（用户报的主症状），
      // 有后端给的第一帧就先垫上，鼠标移过去才动画面也不会闪白。
      if (poster) v.poster = poster;
      v.onerror = () => { if (st.cur === v) { try { v.pause(); } catch (_) {} v.remove(); st.cur = null; } };
      v.src = _playableMediaUri(src);
      if (_carouselMount(st, v)) { const p = v.play && v.play(); if (p && p.catch) p.catch(() => {}); }
      // 连首帧静图都没有的老帧：hover 真播起来时抓一帧回传落盘（认不出 info 序号的远程帧内部会自己放弃）
      if (!poster) { captureModelVideoPoster(v, st.card.dataset.path, _frameInfoIndex(frame)).then((du) => { if (du) frame[2] = du; }); }
      st.idx = st.idx + 1; _carouselRearm(st); return;
    }
    if (!src) { st.idx = st.idx + 1; continue; }   // 空 src 不喂给 new Image()（空串既不发 load 也不发 error，会把这一拍拖到 6s 超时）
    if (!/^data:/i.test(src) && st.dead.has(src)) { st.idx = st.idx + 1; continue; }
    _carouselStillFrame(st, src, my);
    return;
  }
  st.idx = Number.isFinite(st.start) ? st.start : 1; _carouselRearm(st);   // 所有帧都不可用：继续排拍，别把这一轮卡死
}
function _startCarousel(card, path) {
  const st = _carouselPlayer(card, path, false);
  if (!st) return;
  st.auto = false;                       // 悬停接管：改由这张卡自己的节拍驱动
  if (!st.timer) _carouselRearm(st);
  _carouselAdvance(st);                  // 立刻推进一帧，第一拍不等 3 秒
}
function _stopCarousel(card) {
  const st = _masonryCarousel.get(card);
  if (!st) return;
  clearInterval(st.timer);
  _masonryCarousel.delete(card);   // 先摘登记：在途的预载回来时挂载点会发现归属已变而丢弃这一帧
  _autoPlayers.delete(card);
  const wrap = card.querySelector(".ms-img-wrap") || card;
  wrap.querySelectorAll(".ms-img-alt").forEach((n) => { if (n.tagName === "VIDEO") { try { n.pause(); } catch (_) {} } n.remove(); });  // 按 DOM 清：淡入中的新帧与还没摘的旧帧一起收
  st.cur = null;
}
// 自动轮播：不悬停也换帧。每拍只挑一张在视口内、没被悬停、有多图帧的卡片推进一帧，
// 整屏因此是"隔几秒动一处"而不是同时闪；挑中的顺序按游标轮转，转一圈每张卡片都会被换一次。
let _autoCursor = 0, _autoTimer = null;
function _mmAutoSweep() {
  _autoPlayers.forEach((card) => {
    if (!_masonryCarousel.get(card)) { _autoPlayers.delete(card); return; }
    const b = card.getBoundingClientRect();
    const inView = b.top < (window.innerHeight || 0) && b.bottom > 0;
    if (!card.isConnected || !inView || card.matches(":hover")) { _stopCarousel(card); _autoPlayers.delete(card); }
  });
}
function _mmAutoTick() {
  if (!state.cfg || state.cfg.masonry_autoplay === false) return;
  if (_reducedMotion() || document.hidden || !_masonryShown()) return;   // 后台标签页不偷偷拉图
  _mmAutoSweep();
  const cards = _visibleMasonryCards().filter((c) => {
    if (c.matches(":hover")) return false;
    const st = _masonryCarousel.get(c);
    if (st && st.timer) return false;      // 悬停中的卡片归它自己的节拍
    const frames = state.coverCacheMulti && state.coverCacheMulti.get(c.dataset.path || "");
    // 只有一张的卡片挑中也无事发生，节拍就白空转；但全视频模型（哪怕只有 1 帧）必须进自动节拍，
    // 否则它在瀑布流里依旧是哑火的空卡。
    return _carouselHasFrames(frames);
  });
  if (!cards.length) {
    const any = _visibleMasonryCards();    // 一帧都没预取到时顺手催一次，下一拍就有得换
    if (any.length) prefetchMultiForCards(any);
    return;
  }
  if (_autoCursor >= cards.length) _autoCursor = 0;
  const card = cards[_autoCursor++];
  const path = card.dataset.path || "";
  if (path && !state.coverCacheMulti.has(path)) prefetchMultiForCards([card]);
  const st = _carouselPlayer(card, path, true);
  if (!st) return;
  st.auto = true;
  _autoPlayers.add(card);
  _carouselAdvance(st);
}
function _mmAutoSetInterval(on) {
  if (on && !_autoTimer) _autoTimer = setInterval(_mmAutoTick, _MM_AUTO_MS);
  else if (!on && _autoTimer) {
    clearInterval(_autoTimer); _autoTimer = null;
    // 关掉自动轮播时把自动播放器请下来的叠帧一起收掉。只停节拍不清 DOM 的话，卡片会永远停在
    // 最后一帧（不hover、不重渲染就不消失）——实测关设置后 2 张卡各剩 1 层叠帧。
    [..._autoPlayers].forEach((card) => { _stopCarousel(card); });
    _autoPlayers.clear();
  }
}
_mmAutoSetInterval(true);
document.addEventListener("mouseover", (e) => {
  const card = e.target && e.target.closest ? e.target.closest("#mmMasonry .ms-card") : null;
  if (!card) return;
  const st = _masonryCarousel.get(card);
  if (st && st.timer) return;      // 已经是悬停节拍在跑，不重复起
  const path = card.dataset.path || "";
  // 首屏预取只覆盖当时可见的卡片；滚下来之前先 hover 到那张卡时就地补一次请求，
  // 拿到帧后 prefetchMultiForCards 会对仍在 :hover 的卡片自动起播。
  if (path && !state.coverCacheMulti.has(path)) prefetchMultiForCards([card]);
  _startCarousel(card, path);      // 悬停接管：自动轮播留下的播放器直接升级成每 3 秒一帧的专属节拍
});
document.addEventListener("mouseout", (e) => {
  const card = e.target && e.target.closest ? e.target.closest("#mmMasonry .ms-card") : null;
  if (!card) return;
  if (e.relatedTarget && card.contains(e.relatedTarget)) return;   // 仍在卡片内移动
  _stopCarousel(card);
});
$("#mmViewToggle").addEventListener("click", () => {
  state.mmView = state.mmView === "masonry" ? "list" : "masonry";
  $("#mmViewToggle").textContent = state.mmView === "masonry" ? "列表视图" : "瀑布流";
  try { syncViewSeg(); } catch (e) { /* 忽略 */ }
  renderMm();
});
// 瀑布流：单击卡片打开详情；勾选走卡片角标按钮
$("#mmMasonry").addEventListener("click", (e) => {
  const chk = e.target.closest(".ms-check");
  if (chk) {
    const card = chk.closest(".ms-card");
    const r = card && state.display[Number(card.dataset.idx)];
    if (!r) return;
    if (state.mmChecked.has(r.path)) state.mmChecked.delete(r.path);
    else state.mmChecked.add(r.path);
    chk.innerHTML = state.mmChecked.has(r.path) ? '<span class="cbox on"></span>' : '<span class="cbox"></span>';
    card.classList.toggle("checked", state.mmChecked.has(r.path));
    $("#mmCheckLabel").textContent = "已勾选 " + state.mmChecked.size + " 个";
    return;
  }
  const card = e.target.closest(".ms-card");
  if (!card) return;
  if(e.detail>1)return;
  const p = card.dataset.path;
  const cardChk = card.querySelector(".ms-check");
  if (state.mmChecked.has(p)) state.mmChecked.delete(p);
  else state.mmChecked.add(p);
  card.classList.toggle("checked", state.mmChecked.has(p));
  if (cardChk) cardChk.innerHTML = state.mmChecked.has(p) ? '<span class="cbox on"></span>' : '<span class="cbox"></span>';
  $("#mmCheckLabel").textContent = "已勾选 " + state.mmChecked.size + " 个";
  showModelDetail(p);
});
$("#mmMasonry").addEventListener("contextmenu",e=>{
  const card=e.target.closest('.ms-card');if(!card)return;
  showModelContextMenu(e,state.display.find(r=>r.path===card.dataset.path));
});

// 列表：单击主体勾选并更新详情；勾选格 / Ctrl / Shift 保留独立批量选择
$("#mmTable tbody").addEventListener("click", (e) => {
  const tr = e.target.closest("tr");
  if (!tr) return;
  const path = tr.dataset.path;
  const checkboxClick=!!e.target.closest(".cell-sel");
  if(e.detail>1 && !checkboxClick)return;
  const idx = Number(tr.dataset.idx);
  let targets = [path];
  if (e.shiftKey) {
    const base = state.mmLastSel >= 0 ? state.mmLastSel : 0;
    const a = Math.min(base, idx), b = Math.max(base, idx);
    targets = state.display.slice(a, b + 1).map((r) => r.path);
  } else if (e.ctrlKey || e.metaKey) {
    // ctrl 单击：单独切换当前行
    const cur = state.mmChecked.has(path);
    cur ? state.mmChecked.delete(path) : state.mmChecked.add(path);
    state.mmLastSel = idx;
    targets.forEach((p) => {
      const tr2 = document.querySelector('#mmTable tbody tr[data-path="' + CSS.escape(p) + '"]');
      if (tr2) tr2.classList.toggle("sel-row", state.mmChecked.has(p));
    });
    state.mmSel = new Set(state.mmChecked);
    $("#mmCheckLabel").textContent = "已勾选 " + state.mmChecked.size + " 个";
    return;
  }
  const allChecked = targets.every((p) => state.mmChecked.has(p));
  targets.forEach((p) => allChecked ? state.mmChecked.delete(p) : state.mmChecked.add(p));
  state.mmLastSel = idx;
  targets.forEach((p) => {
    const tr2 = document.querySelector('#mmTable tbody tr[data-path="' + CSS.escape(p) + '"]');
    if (tr2) {
      tr2.classList.toggle("sel-row", state.mmChecked.has(p));
      const cell2 = tr2.querySelector(".cell-sel");
      if (cell2) cell2.innerHTML = state.mmChecked.has(p) ? '<span class="cbox on"></span>' : '<span class="cbox"></span>';
    }
  });
  state.mmSel = new Set(state.mmChecked);
  $("#mmCheckLabel").textContent = "已勾选 " + state.mmChecked.size + " 个";
  if(!e.shiftKey && !checkboxClick)showModelDetail(path);
});

// 筛选
let mmFilterTimer = null;
// 统一过滤：关键词 + 「只看有更新」
function applyMmFilter() {
  const kw = $("#mmFilter").value.trim().toLowerCase();
  let rows = state.models;
  if (state.mmUpdOnly) rows = rows.filter((r) => r.upd && r.upd.has_update);
  if (kw) {
    rows = rows.filter((r) =>
      (r.name || "").toLowerCase().includes(kw) ||
      (r.civitai_name || "").toLowerCase().includes(kw) ||
      (r.path || "").toLowerCase().includes(kw));
  }
  if (state.mmFolderF) {
    // 相对文件夹路径（Lora/风格）对绝对模型路径做"目录段"匹配；统一斜杠与大小写
    const _fp = String(state.mmFolderF).toLowerCase().replace(/^[\/]+|[\/]+$/g, "");
    rows = rows.filter((r) => {
      const p2 = String(r.path || "").replace(/\\/g, "/").toLowerCase();
      return p2.indexOf("/" + _fp + "/") >= 0 || p2.indexOf("/" + _fp) === p2.length - _fp.length - 1;
    });
  }
  if (state.mmTypeF) rows = rows.filter(r => String(r.type_filter || r.type || "Unknown").toLowerCase() === state.mmTypeF.toLowerCase());
  if (state.mmBaseF) rows = rows.filter((r) => String(r.base || "") === state.mmBaseF);
  if (state.mmAuthorF) rows = rows.filter((r) => String(r.author || "") === state.mmAuthorF);
  if (state.mmStF) {
    rows = rows.filter((r) => {
      const u = r.upd || {};
      const st = u.has_update ? "upd" : (r.upd ? (u.other_base ? "other" : "latest") : "none");
      return st === state.mmStF;
    });
  }
  if (state.mmCoverF) {
    // 封面筛选：有图（含视频封面）/无图。coverCache 兜底自愈「扫描后才补下封面」的行级字段滞后
    rows = rows.filter((r) => {
      const has = !!r.has_cover || (state.coverCache && state.coverCache.has(r.path));
      return state.mmCoverF === "cover" ? has : !has;
    });
  }
  state.display = rows.slice();
  // Selection belongs to the current visible result, never hidden rows.
  const visible=new Set(state.display.map(r=>r.path));
  for(const selected of [state.mmChecked,state.mmSel])for(const path of selected)if(!visible.has(path))selected.delete(path);
  state.mmLastSel=null;
  renderMm();
  fillMmBaseOptions();
  fillMmFolderOptions();
  fillMmAuthorOptions();
  if (state.mmUpdOnly) setStatus("筛选：有更新的模型 " + state.display.length + " 个（点「有更新」可取消）");
}
$("#mmFilter").addEventListener("input", () => {
  clearTimeout(mmFilterTimer);
  mmFilterTimer = setTimeout(applyMmFilter, 200);
});
$("#mmFilterClear").addEventListener("click", () => { $("#mmFilter").value = ""; applyMmFilter(); });
if ($("#mmUpdOnly")) $("#mmUpdOnly").addEventListener("click", () => {
  state.mmUpdOnly = !state.mmUpdOnly;
  $("#mmUpdOnly").classList.toggle("active", !!state.mmUpdOnly);
  applyMmFilter();
});

// ===== 更新检测：按钮 + 徽标 =====
function updTip(u) {
  if (!u) return "";
  const parts = ["有新版"];
  if (u.latest_name) parts.push(u.latest_name);
  if (u.latest_base) parts.push("（" + u.latest_base + "）");
  if (u.behind > 1) parts.push("· 落后 " + u.behind + " 个版本");
  parts.push("· 点击去 C 站看新版（不会自动下载）");
  return parts.join(" ");
}
function applyUpdatesToRows(items) {
  if (!items) return;
  state.mmUpdItems = items;          // 供「更新」页面使用
  for (const r of state.models) {
    const it = items[r.path];
    r.upd = it || null;
  }
}
async function mmCheckUpdatesFlow(force, paths) {
  const r = await api.call("mm_check_updates", !!force, (paths && paths.length) ? paths : null);
  if (!r || !r.started) {
    if (r && r.recent) {
      const ok = await confirmBox(r.msg + "<br/><br/>要现在强制重新检查一遍吗？<br/>（约 200 个模型，需要 1~2 分钟）");
      if (ok) return mmCheckUpdatesFlow(true);
    } else {
      setStatus((r && r.msg) || "检查更新未开始");
    }
    return;
  }
  setStatus("检查更新中 0/" + (r.total || "?") + " …");
  const timer = setInterval(async () => {
    const p = await api.call("get_mm_update_state");
    if (!p) return;
    if (p.running) {
      setStatus("检查更新中 " + (p.done || 0) + "/" + (p.total || 0) + " · 已发现 " + (p.newer || 0) + " 个有更新（再点一次按钮可停止）");
      ["#updCheck", "#mmCheckUpd"].forEach((sel) => {
        const b = $(sel);
        if (b) { b.textContent = "停止检查（" + (p.done || 0) + "/" + (p.total || 0) + "）"; b.classList.add("is-busy"); }
      });
      return;
    }
    clearInterval(timer);
    ["#updCheck", "#mmCheckUpd"].forEach((sel) => {
      const b = $(sel);
      if (b) { b.textContent = "检查更新"; b.classList.remove("is-busy"); }
    });
    const wlSkip = p.wl_skipped ? "（已按白名单跳过 " + p.wl_skipped + " 个）" : "";
    setStatus((p.msg || "检查完成") + wlSkip);
    try {
      const u = await api.call("get_model_updates");
      if (u && u.items) {
        applyUpdatesToRows(u.items);
        state.mmUpdCheckedAt = u.checked_at || 0;
        if (state.models.length) applyMmFilter();
        if ($("#page-updates") && $("#page-updates").classList.contains("active")) {
          renderUpdatesPage();                    // 正在「更新」页面 → 直接刷新列表
        } else {
          showUpdateResults(u.items);             // 其它页面 → 弹结果窗
        }
      }
    } catch (e) { /* 忽略 */ }
  }, 800);
}

// 检查更新结果窗口：列出「有同底模新版」的模型，每条可去 C 站
// ================= 更新页面（紧凑数据表 + 版本下拉 / 批量更新 / 白名单） =================
function _updBase(p) { return String(p || "").replace(/\\/g, "/").split("/").pop() || ""; }
function _updDir(p) { const a = String(p || "").replace(/\\/g, "/").split("/"); a.pop(); return a.join("/"); }

// 语义化版本比较：v1 < v1.1 < v1.2 < v1.10 < v2（绝不按字符串序排）
function _verKey(name) {
  const parts = String(name || "").match(/\d+|[A-Za-z]+/g) || [];
  return parts.slice(0, 10).map((p) => (/^\d+$/.test(p) ? [0, Number(p), ""] : [1, 0, p.toLowerCase()]));
}
function _verCmp(a, b) {
  const ka = _verKey(a), kb = _verKey(b);
  const n = Math.max(ka.length, kb.length);
  for (let i = 0; i < n; i++) {
    const x = ka[i] || [-1, 0, ""], y = kb[i] || [-1, 0, ""];   // 段数少的（v1）排在前（v1 < v1.1）
    if (x[0] !== y[0]) return x[0] - y[0];
    if (x[1] !== y[1]) return x[1] - y[1];
    if (x[2] !== y[2]) return x[2] < y[2] ? -1 : 1;
  }
  return 0;
}

function _updState(it) {
  if (it && it.has_update) return { k: "upd", t: _icon("alert") + "有更新", cls: "green" };
  if (it && it.other_base) return { k: "other", t: _icon("layers") + "仅换底模", cls: "mut" };
  if (it && it.unknown) return { k: "unknown", t: _icon("info") + "无法判定", cls: "gray" };
  if (it && !it.checked_at) return { k: "todo", t: _icon("info") + "未检查", cls: "gray" };
  return { k: "latest", t: _icon("check") + "已是最新", cls: "blue" };
}

// 更新页状态选项卡：与列表徽章共用同一套状态色令牌；再点当前项 = 取消筛选
const UPD_ST_DEF = [["", "全部"], ["upd", "有更新"], ["latest", "已是最新"], ["other", "仅换底模"], ["unknown", "无法判定"], ["todo", "未检查"]];
function renderUpdTabs(baseRows) {
  const host = $("#updTabs");
  if (!host) return;
  const cnt = { "": (baseRows || []).length };
  (baseRows || []).forEach((x) => { const k = _updState(x.it).k; cnt[k] = (cnt[k] || 0) + 1; });
  host.innerHTML = UPD_ST_DEF.map(([k, t]) => {
    const on = state.updState === k ? " on" : "";
    const dot = k ? '<span class="dot ' + k + '"></span>' : "";
    return '<button type="button" class="upd-tab' + on + '" data-st="' + k + '" role="tab" aria-selected="' + (on ? "true" : "false") + '">' +
      dot + t + ' <span class="n">' + (cnt[k] || 0) + "</span></button>";
  }).join("");
  host.querySelectorAll(".upd-tab").forEach((b) => b.addEventListener("click", () => {
    const k = b.dataset.st || "";
    state.updState = (state.updState === k) ? "" : k;   // 再点当前筛选 = 取消
    state.updPage = 1;
    renderUpdatesPage();
  }));
}

// 该行的「可选版本」清单：优先用后端 ver_list（含当前/推荐），老缓存退化到只知最新版
function _updVers(x) {
  const it = x.it || {};
  let vl = (it.ver_list || []).slice();
  if (!vl.length && it.has_update) {
    vl = [{ id: String(it.latest_version || ""), name: it.latest_name || ("ID " + (it.latest_version || "?")),
            date: String(it.latest_date || "").slice(0, 10), current: false }];
  }
  vl.sort((a, b) => _verCmp(b.name, a.name));          // 语义化倒序：v3, v1.1, v1, v0.9
  const rec = it.has_update ? String(it.latest_version || "") : "";
  return { list: vl, rec };
}

// 版本选择控件：Metro 直角按钮 + 门户菜单（列出全部同底模版本）
function vselHtml(x, list, rec, sel, selEntry) {
  const cur = String(x.path);
  const label = selEntry ? ((String(selEntry.id) === String(rec) ? '<span class="v-star">★</span> ' : "") + esc(selEntry.name || sel)) : "选择版本";
  const items = list.map((v) => {
    const isRec = String(v.id) === String(rec);
    const isCur = !!v.current;
    return '<div class="vitem' + (String(v.id) === String(sel) ? " on" : "") + '" data-vpath="' + esc(cur) + '" data-vid="' + esc(v.id) + '">' +
      '<span class="v-name">' + (isRec ? '<span class="v-star">★</span> ' : "") + esc(v.name || ("ID " + v.id)) +
      (isCur ? '<span class="v-cur">当前版本</span>' : "") + "</span>" +
      '<span class="v-date">' + esc(String(v.date || "").slice(0, 10)) + "</span></div>";
  }).join("");
  const site = x.it && x.it.url
    ? '<div class="mm-sep-h"></div><div class="vitem vopen" data-vsite="' + esc(x.it.url) + '">打开 C 站查看全部版本 →</div>'
    : "";
  return '<span class="btn-group vsel-wrap">' +
    '<button class="btn vsel-btn" data-vsel="' + esc(cur) + '" data-tip="选择要更新到的版本（推荐版本默认选中）">' +
    '<span class="vsel-label">' + label + '</span>' + _icon("chevron-down") + "</button>" +
    '<div class="mm-menu vmenu">' + items + site + "</div></span>";
}

function _updRowHtml(x) {
  const it = x.it || {};
  const st = _updState(it);
  const isUpd = !!it.has_update;
  const { list, rec } = _updVers(x);
  const sel = state.updPick[x.path] || rec || (list.length ? String(list[0].id) : "");
  const selEntry = list.find((v) => String(v.id) === String(sel)) || null;
  const curName = it.local_name || "";
  const date = String(it.latest_date || it.local_date || "").slice(0, 10);

  // 版本清单直接由 vselHtml() 渲染（自定义门户菜单，列出全部版本）

  // 降级判断：选中的版本比当前版本旧（按发布日期；没有日期就用语义化版本名兜底）
  const curEntry = list.find((v) => v.current) || null;
  const selIsCurrent = !!(selEntry && selEntry.current);
  let isDown = false;
  if (selEntry && !selIsCurrent) {
    const sd = selEntry.date || "", cd = (curEntry && curEntry.date) || "";
    if (sd && cd) isDown = sd < cd;
    else if (selEntry.name && curName) isDown = _verCmp(selEntry.name, curName) < 0;
  }
  const actLabel = isDown ? "降级到 " + esc((selEntry && selEntry.name) || sel) : "更新到 " + esc((selEntry && selEntry.name) || "最新");
  const actTip = isDown
    ? "回退到这一版（比你当前用的旧）：新版会下到旧版所在文件夹；当前这份文件保留或删除由设置决定"
    : "下载这一版到旧版所在文件夹；旧版保留或删除由设置决定";
  return '<tr class="' + (state.updSel.has(x.path) ? "is-sel" : "") + (isUpd ? "" : " is-flat") + '" data-path="' + esc(x.path) + '" data-mid="' + esc(it.model_id || "") +
    '" data-mname="' + esc(it.model_name || "") + '">' +
    '<td class="c-ck"><input type="checkbox" class="upd-cb" data-path="' + esc(x.path) + '"' + (state.updSel.has(x.path) ? " checked" : "") + "/></td>" +
    '<td class="c-info"><div class="upd-info">' +
      '<img class="dd-thumb upd-thumb" data-path="' + esc(x.path) + '" alt=""/>' +
      '<div class="upd-meta">' +
        '<div class="upd-nm" data-tip="' + esc(it.model_name || _updBase(x.path)) + '">' + esc(it.model_name || _updBase(x.path)) + "</div>" +
        '<div class="upd-sub2">' + (it.author ? "作者：" + esc(it.author) + " ｜ " : "") + (it.local_base ? "底模：" + esc(it.local_base) : "") + (it.model_type ? " ｜ " + esc(it.model_type) : "") + "</div>" +
        '<div class="upd-sub2 upd-dim file-subpath" data-tip="' + esc(x.path) + '">' + esc(_updBase(x.path)) + " ｜ " + esc(_updDir(x.path)) + "</div>" +
      "</div></div></td>" +
    '<td class="c-cur"><span class="ver-badge" data-tip="' + esc(it.local_name || "") + '">' + esc(curName || "未知") + "</span>" +
      (!curName && it.local_version ? '<div class="upd-dim upd-id">ID ' + esc(it.local_version) + "</div>" : "") + "</td>" +
    '<td class="c-ver">' + (list.length ? vselHtml(x, list, rec, sel, selEntry) : '<span class="upd-dim">—</span>') + "</td>" +
    '<td class="c-date">' + esc(date || "—") + "</td>" +
    '<td class="c-st"><span class="st-badge ' + st.cls + '">' + st.t + "</span></td>" +
    '<td class="c-act"><div class="upd-acts2">' +
      ((!selIsCurrent && sel) ? '<button class="btn btn-tiny upd-go' + (isDown ? " is-down" : " btn-primary") + '" data-path="' + esc(x.path) + '" data-vid="' + esc(sel) + '" data-tip="' + actTip + '">' + actLabel + "</button>" : "") +
      (it.url ? '<a href="#" class="upd-site" data-url="' + esc(it.url) + '" data-tip="在浏览器打开 C 站页面"><img class="thumb upd-cov" data-path="' + esc(x.path) + '" alt=""/></a>' : "") +
      '<button class="icon-btn upd-more" data-path="' + esc(x.path) + '" data-tip="更多：打开文件夹 / 复制路径 / 不再提醒">···</button>' +
    "</div></td></tr>";
}

function _updLibraryRows() {
  const items = state.mmUpdItems || {};
  const union = Object.assign({}, items);
  for (const r of (state.models || [])) {         // 扫到、有 C 站信息、但还没检查过的 → 补一条「未检查」行
    if (r && r.path && r.modelId && !union[r.path]) {
      union[r.path] = { model_name: r.civitai_name || "", local_name: r.versionName || "",
                        local_base: r.base || "", model_type: r.type || "", author: r.author || "" };
    }
  }
  return Object.entries(union).map(([path, it]) => ({ path, it: it || {} }));
}
function _updFilterRows() {
  let rows = _updLibraryRows();
  const q = String(state.updQ || "").trim().toLowerCase();
  if (state.updBase) rows = rows.filter((x) => String(x.it.local_base || "") === state.updBase);
  if (q) rows = rows.filter((x) => ((x.it.model_name || "") + " " + x.path + " " + (x.it.local_name || "") + " " + (x.it.author || "")).toLowerCase().includes(q));
  renderUpdTabs(rows);                                                    // 计数：底模/搜索过滤后、状态过滤前的行
  if (state.updState) rows = rows.filter((x) => _updState(x.it).k === state.updState);
  const sk = state.updSortKey || "date";
  const rev = !!state.updSortRev;
  // 各列默认方向：date/ver 默认"新的多者在前"(降序)，其余默认升序
  // 说明：比较器一律按升序写，rev=true 时才 reverse()
  const ST_RANK = { upd: 0, other: 1, unknown: 2, todo: 3, latest: 4 };   // 有更新在前
  const cmp = (a, b) => {
    if (sk === "name") {
      const x = String(a.it.model_name || _updBase(a.path)), y = String(b.it.model_name || _updBase(b.path));
      return x.localeCompare(y, "zh") || String(a.path).localeCompare(b.path);
    }
    if (sk === "cur") {
      return _verCmp(String(a.it.local_name || ""), String(b.it.local_name || "")) || String(a.path).localeCompare(b.path);
    }
    if (sk === "ver") {
      const x = (a.it.ver_list || []).length, y = (b.it.ver_list || []).length;
      return (x - y) || String(a.path).localeCompare(b.path);          // 升序；"多的在前"是默认方向(rev)
    }
    if (sk === "state") {
      return (ST_RANK[_updState(a.it).k] ?? 9) - (ST_RANK[_updState(b.it).k] ?? 9)
        || String(a.path).localeCompare(b.path);
    }
    const x = String(a.it.latest_date || a.it.local_date || ""), y = String(b.it.latest_date || b.it.local_date || "");
    return x.localeCompare(y) || String(a.path).localeCompare(b.path);  // 升序；"新的在前"是默认方向(rev)
  };
  rows.sort(cmp);
  if (rev) rows.reverse();
  return rows;
}

function _updPagerHtml(pages, page) {
  if (pages <= 1) return "";
  const btn = (p, label, dis) => '<button class="pg' + (p === page ? " on" : "") + '" data-p="' + p + '"' + (dis ? " disabled" : "") + ">" + label + "</button>";
  const out = [btn(page - 1, "‹", page <= 1)];
  const win = [];
  for (let p = 1; p <= pages; p++) {
    if (p === 1 || p === pages || Math.abs(p - page) <= 1) win.push(p);
    else if (win[win.length - 1] !== "…") win.push("…");
  }
  win.forEach((p) => { out.push(p === "…" ? '<span class="pg-dot">…</span>' : btn(p, p, false)); });
  out.push(btn(page + 1, "›", page >= pages));
  return out.join("");
}

async function renderUpdatesPage() {
  const tbody = $("#updTbody");
  if (!tbody) return;
  if (!state.mmUpdItems) {
    try {
      const u = await api.call("get_model_updates");
      if (u && u.items) { applyUpdatesToRows(u.items); state.mmUpdCheckedAt = u.checked_at || 0; }
    } catch (e) { /* 忽略 */ }
  }
  const items = state.mmUpdItems || {};
  const all = _updFilterRows();
  const per = Number(state.updPer || 50);
  const pages = Math.max(1, Math.ceil(all.length / per));
  state.updPage = Math.min(Math.max(1, state.updPage || 1), pages);
  const pageRows = all.slice((state.updPage - 1) * per, state.updPage * per);

  // 底模下拉
  const bases = Array.from(new Set(Object.values(items).map((it) => String((it || {}).local_base || "")).filter(Boolean))).sort();
  const bsel = $("#updBase");
  if (bsel) bsel.innerHTML = '<option value="">全部底模</option>' + bases.map((b) => '<option value="' + esc(b) + '"' + (b === state.updBase ? " selected" : "") + ">" + esc(b) + "</option>").join("");

  // 顶部统计
  const nUpd = Object.values(items).filter((it) => it && it.has_update).length;
  const ca = state.mmUpdCheckedAt ? new Date(state.mmUpdCheckedAt * 1000).toLocaleString() : "还没检查过";
  const libraryTotal=_updLibraryRows().length;
  const cnt = $("#updCount");
  if (cnt) cnt.innerHTML = "可更新 <b class=\"warn\">" + nUpd + "</b> ｜ 共 " + libraryTotal + " 个 ｜ 上次检查 " + esc(ca);

  // 表体 + 空态
  const empty = $("#updEmpty");
  if (!all.length) {
    tbody.innerHTML = "";
    if (empty) {
      empty.style.display = "block";
      empty.innerHTML = Object.keys(items).length
        ? "当前筛选/搜索下没有条目（清空搜索框或把筛选改回「全部」）。"
        : "还没有检查结果：点右上角「检查更新」开始（首次约 1~2 分钟，结果缓存 24 小时）。";
    }
  } else {
    if (empty) empty.style.display = "none";
    tbody.innerHTML = pageRows.map(_updRowHtml).join("");
  loadUpdThumbs();
    loadDdThumbs(tbody, pageRows.map((x) => x.path), 64);
  }

  // 表头排序指示
  try {
    const sk2 = state.updSortKey || "date", rv = !!state.updSortRev;
    document.querySelectorAll("#updTable thead th.sortable").forEach((th) => {
      const on = th.dataset.key === sk2;
      th.classList.toggle("on", on);
      const sp = th.querySelector(".th-sort");
      if (sp) sp.textContent = on ? (rv ? " ▼" : " ▲") : "";
    });
    const sel = $("#updSort");
    if (sel) {
      const map = { date: "date", name: "name", state: "state" };
      if (map[sk2]) sel.value = map[sk2];
    }
  } catch (e) { /* 忽略 */ }

  // 底栏
  const st1 = $("#updStat");
  if (st1) st1.textContent = "共 " + all.length + " 个模型" + (all.length !== libraryTotal ? "（筛选中，总 " + libraryTotal + "）" : "");
  _updSyncSel();
  const pg = $("#updPager");
  if (pg) pg.innerHTML = _updPagerHtml(pages, state.updPage);
  _updBind(tbody);
}

function _updSyncSel() {
  const n = state.updSel.size;
  const el = $("#updSelInfo");
  if (el) el.innerHTML = "已选 <b>" + n + "</b> 个";
  const b = $("#updDlSel");
  if (b) { b.textContent = n ? "批量更新（" + n + "）" : "批量更新"; b.disabled = !n; }
  const allCk = $("#updAll");
  if (allCk) {
    const boxes = Array.from(document.querySelectorAll("#updTbody .upd-cb"));
    const on = boxes.filter((c) => c.checked).length;
    allCk.checked = boxes.length > 0 && on === boxes.length;
    allCk.indeterminate = on > 0 && on < boxes.length;
  }
}

// 下拉换版本时实时重算按钮：更新到 X / 降级到 X；选回当前版本则隐藏按钮
function _updSyncRowButton(tr, path, vid) {
  const it = (state.mmUpdItems || {})[path] || {};
  const { list } = _updVers({ path, it });
  const e = list.find((x) => String(x.id) === String(vid)) || null;
  const cur = list.find((x) => x.current) || null;
  const isCur = !!(e && e.current);
  let isDown = false;
  if (e && !isCur) {
    const sd = e.date || "", cd = (cur && cur.date) || "";
    if (sd && cd) isDown = sd < cd;
    else if (e.name && it.local_name) isDown = _verCmp(e.name, it.local_name) < 0;
  }
  let btn = tr.querySelector(".upd-go");
  if (isCur || !e) { if (btn) btn.remove(); return; }
  if (!btn) {
    const acts = tr.querySelector(".upd-acts2");
    if (!acts) return;
    btn = document.createElement("button");
    btn.className = "btn btn-tiny upd-go";
    btn.dataset.path = path;
    acts.insertBefore(btn, acts.firstChild);
    btn.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const oldTxt = btn.textContent;
      btn.disabled = true;
      btn.textContent = "加入队列…";
      const r = await api.call("mm_download_version", btn.dataset.path, btn.dataset.vid);
      btn.textContent = (r && r.ok) ? "已加入" : ((r && r.msg) || "失败");
      setStatus((r && r.msg) || "");
      setTimeout(() => { btn.disabled = false; btn.textContent = oldTxt; }, 1800);
    });
  }
  btn.dataset.vid = vid;
  btn.textContent = isDown ? "降级到 " + ((e.name) || vid) : "更新到 " + ((e.name) || vid);
  btn.classList.toggle("is-down", isDown);
  btn.classList.toggle("btn-primary", !isDown);
}

function _updBind(tbody) {
  tbody.querySelectorAll(".upd-cb").forEach((cb) => cb.addEventListener("change", () => {
    const p = cb.dataset.path;
    if (cb.checked) state.updSel.add(p); else state.updSel.delete(p);
    const tr = cb.closest("tr");
    if (tr) tr.classList.toggle("is-sel", cb.checked);
    _updSyncSel();
  }));
  tbody.querySelectorAll(".upd-vsel").forEach((sel) => sel.addEventListener("change", () => {
    const p = sel.dataset.path;
    const it = (state.mmUpdItems || {})[p] || {};
    const v = sel.value;
    if (v === "__site") {
      api.call("open_url", it.url || "");
      sel.value = state.updPick[p] || String(it.latest_version || "");
      return;
    }
    state.updPick[p] = v;
    const tr2 = sel.closest("tr");
    if (tr2) _updSyncRowButton(tr2, p, v);
  }));
  tbody.querySelectorAll(".upd-go").forEach((b) => b.addEventListener("click", async (e) => {
    e.stopPropagation();
    const old = b.textContent;
    b.disabled = true;
    b.textContent = "加入队列…";
    const r = await api.call("mm_download_version", b.dataset.path, b.dataset.vid);
    b.textContent = (r && r.ok) ? "已加入" : ((r && r.msg) || "失败");
    setStatus((r && r.msg) || "");
    setTimeout(() => { b.disabled = false; b.textContent = old; }, 1800);
  }));
  tbody.querySelectorAll(".chip-go, .upd-site").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    if (a.dataset.url) api.call("open_url", a.dataset.url);
  }));
  tbody.querySelectorAll(".upd-more").forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    _updCtxMenu(b.dataset.path, b);
  }));
  tbody.querySelectorAll("tr").forEach((tr) => tr.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    _updCtxMenu(tr.dataset.path, null, e.clientX, e.clientY);
  }));
}

function _updCtxMenu(path, anchor, cx, cy) {
  const it = (state.mmUpdItems || {})[path] || {};
  state.updCtxPath = path;
  const menu = $("#ctxMenu");
  menu.innerHTML =
    (it.has_update ? '<div class="ctx-item" data-act="upd_dl" data-tip="把下拉里选中的版本加入下载队列">更新到选中版本</div>' : "") +
    '<div class="ctx-item" data-act="upd_wl" data-tip="以后不再提示这个模型的更新（按模型记入白名单；对已最新的模型也有效）">不再提醒更新（加入白名单）</div>' +
    (it.url ? '<div class="ctx-item" data-act="upd_site2" data-tip="在浏览器打开 C 站页面">打开 C 站页面</div>' : "") +
    '<div class="ctx-item" data-act="upd_folder" data-tip="打开资源管理器并选中该文件">打开所在文件夹</div>' +
    '<div class="ctx-item" data-act="upd_copy" data-tip="复制文件完整路径">复制文件路径</div>';
  menu.style.display = "block";
  const zf = parseFloat(getComputedStyle(document.documentElement).zoom) || 1;
  if (anchor) {
    const r = anchor.getBoundingClientRect();
    menu.style.left = (r.left / zf) + "px";
    menu.style.top = ((r.bottom + 4) / zf) + "px";
  } else {
    menu.style.left = ((cx || 0) / zf) + "px";
    menu.style.top = ((cy || 0) / zf) + "px";
  }
  const mr = menu.getBoundingClientRect();
  if (mr.right > window.innerWidth) menu.style.left = Math.max(0, window.innerWidth - mr.width) + "px";
  if (mr.bottom > window.innerHeight) menu.style.top = Math.max(0, window.innerHeight - mr.height) + "px";
}

$("#ctxMenu").addEventListener("click", async (e) => {
  const item = e.target.closest("[data-act^=upd_]");
  if (!item) return;
  $("#ctxMenu").style.display = "none";
  const p = state.updCtxPath || "";
  const it = (state.mmUpdItems || {})[p] || {};
  const act = item.dataset.act;
  if (act === "upd_dl") {
    const { rec } = _updVers({ path: p, it });
    const vid = state.updPick[p] || rec;
    const r = await api.call("mm_download_version", p, vid);
    setStatus((r && r.msg) || "");
  } else if (act === "upd_wl") {
    const r = await api.call("add_update_whitelist", it.model_id || "", it.model_name || "");
    setStatus((r && r.msg) || "已加入白名单");
    if (state.mmUpdItems) delete state.mmUpdItems[p];
    state.updSel.delete(p);
    for (const rw of state.models) if (rw.path === p) rw.upd = null;
    renderUpdatesPage();
    renderMm();
  } else if (act === "upd_site2") { if (it.url) api.call("open_url", it.url); }
  else if (act === "upd_folder") {
    const r = JSON.parse(await api.call("open_in_folder", p) || "{}");
    setStatus((r && r.ok) ? "已打开所在文件夹" : ((r && r.msg) || "文件不存在或已被移动"));
  } else if (act === "upd_copy") {
    await window.__copyText(p);
    setStatus("路径已复制: " + p);
  }
});

// 批量更新：逐条按各自下拉选中的版本入队（每条几百毫秒，带进度）
async function updBatchDownload(paths) {
  const items = state.mmUpdItems || {};
  const picks = [];
  (paths || []).forEach((p) => {
    const it = items[p] || {};
    if (!it.has_update) return;
    const { rec } = _updVers({ path: p, it });
    picks.push({ path: p, vid: state.updPick[p] || rec, name: it.model_name || _updBase(p) });
  });
  if (!picks.length) { setStatus("勾选的条目里没有「有更新」的（先点「检查更新」）"); return; }
  const keepTxt = (state.cfg && state.cfg.update_keep_old === "delete") ? "旧版将移入回收站" : "旧版保留";
  const ok = await confirmBoxRaw(
    "将把以下 <b>" + picks.length + "</b> 个模型更新到各自选择的版本（新版下到旧版所在文件夹）：<br/>" +
    '<div class="dedup-dir" style="max-height:200px;overflow:auto">' +
    picks.slice(0, 40).map((x) => "· " + esc(x.name) + " → " + esc(((_updVers({ path: x.path, it: items[x.path] }).list.find((v) => String(v.id) === String(x.vid)) || {}).name) || x.vid)).join("<br/>") +
    (picks.length > 40 ? "<br/>… 等 " + picks.length + " 个" : "") + "</div>" +
    '<div class="dedup-dir">' + keepTxt + "（设置里可改）。</div>",
    "确认批量更新");
  if (!ok) return;
  if (ok.root) ok.root.remove();          // 关掉确认框（confirmBoxRaw 只收遮罩，内容框要调用方自己收）
  const btn = $("#updDlSel");
  if (state.updBatchBusy) return;
  state.updBatchBusy = true;
  if (btn) btn.disabled = true;
  let done = 0, skip = 0, fail = 0;
  for (const x of picks) {
    setStatus("批量更新 " + (done + skip + fail + 1) + "/" + picks.length + "：" + x.name);
    try {
      const r = await api.call("mm_download_version", x.path, x.vid);
      if (r && r.ok) done++;
      else if (r && r.skipped) skip++;
      else fail++;
    } catch (e) { fail++; }
  }
  state.updBatchBusy = false;
  if (btn) btn.disabled = false;
  setStatus("批量更新完成：入队 " + done + " 个" + (skip ? "，跳过 " + skip + " 个（已在队列或已存在）" : "") +
            (fail ? "，失败 " + fail + " 个" : "") + " —— 已跳到「下载管理」");
  renderUpdatesPage();
  switchPage("dlmanager");                // 自动跳到下载管理页看进度
  if (typeof dlRefresh === "function") dlRefresh();
}

// 批量忽略（加入更新白名单）
async function updBatchIgnore(paths) {
  const items = state.mmUpdItems || {};
  const list = (paths || []).filter((p) => (items[p] || {}).model_id);
  if (!list.length) { setStatus("没有可忽略的条目"); return; }
  const ok = await confirmBoxRaw("把选中的 <b>" + list.length + "</b> 个模型加入<b>更新白名单</b>？<br/>以后检查更新会直接跳过它们（可在「白名单」里移出）。", "不再提醒更新");
  if (!ok) return;
  if (ok.root) ok.root.remove();          // 关掉确认框
  let n = 0;
  for (const p of list) {
    const it = items[p] || {};
    try { await api.call("add_update_whitelist", it.model_id || "", it.model_name || ""); n++; } catch (e) { /* 单个失败继续 */ }
    if (state.mmUpdItems) delete state.mmUpdItems[p];
    state.updSel.delete(p);
  }
  setStatus("已忽略 " + n + " 个模型的更新提醒");
  renderUpdatesPage();
  renderMm();
}

async function updWhitelistDialog() {
  let items = [];
  try {
    const r = await api.call("get_update_whitelist");
    items = ((r && r.items) || []).slice();
  } catch (e) {
    infoBox("<div class='dt-dim'>白名单读取失败：" + esc(String(e)) + "</div>", "更新白名单");
    return;
  }
  const dlg = document.createElement("div");
  dlg.className = "rd-mask";
  const box = document.createElement("div");
  box.className = "rename-dialog";
  box.style.width = "620px";
  const onKey = (e) => { if (e.key === "Escape") closeAll(); };
  const closeAll = () => { document.removeEventListener("keydown", onKey); dlg.remove(); box.remove(); };
  document.addEventListener("keydown", onKey);
  const render = () => {
    box.innerHTML =
      '<div class="rd-title">更新白名单 <span class="dt-dim">（' + items.length + ' 个模型）</span><span class="dlg-x" id="wlX" title="关闭" data-tip="关闭（Esc）">' + _icon("x") + "</span></div>" +
      '<input class="input" id="wlQ" placeholder="搜索模型名称 / modelId…" style="width:100%;margin:6px 0 8px" />' +
      '<div class="dd-hint">名单里的模型<b>不再提示更新</b>（检查时直接跳过）。加入方式：行内「···」→「不再提醒更新」。</div>' +
      '<div style="max-height:320px;overflow:auto">' +
      (items.length ? items.map((x) =>
        '<div class="cf-row" data-name="' + esc(((x.name || "") + " " + x.model_id).toLowerCase()) + '"><div class="dd-text"><b>' + esc(x.name || x.model_id) + "</b>" +
        '<div class="dedup-dir">modelId ' + esc(x.model_id) + '</div></div>' +
        '<button class="btn btn-tiny wl-del" data-mid="' + esc(x.model_id) + '">移出</button></div>').join("")
        : '<div class="upd-empty">白名单是空的。</div>') +
      "</div>" +
      '<div class="rd-actions"><button class="btn" id="wlClose">关闭</button></div>';
    $("#wlClose", box).addEventListener("click", closeAll);
    $("#wlX", box).addEventListener("click", closeAll);
    const qEl = $("#wlQ", box);
    if (qEl) qEl.addEventListener("input", () => {
      const q = qEl.value.trim().toLowerCase();
      box.querySelectorAll('.cf-row[data-name]').forEach((row) => {
        row.style.display = (!q || row.dataset.name.includes(q)) ? "" : "none";
      });
    });
    box.querySelectorAll(".wl-del").forEach((b) => b.addEventListener("click", async () => {
      const rr = await api.call("remove_update_whitelist", b.dataset.mid);
      setStatus((rr && rr.msg) || "已移出");
      const i = items.findIndex((x) => String(x.model_id) === String(b.dataset.mid));
      if (i >= 0) items.splice(i, 1);
      render();
    }));
  };
  render();
  document.body.appendChild(dlg);
  document.body.appendChild(box);
  dlg.addEventListener("click", closeAll);
}

// 工具栏 / 底栏交互（一次性绑定）
$('#updSync').addEventListener('click',async()=>{
 const button=$('#updSync');button.disabled=true;setStatus('正在同步本地模型与更新记录…');
 try{
  const r=await api.call('sync_local_model_updates');
  if(r?.ok){state.models=JSON.parse(await api.call('get_scan_rows')||'[]');const u=await api.call('get_model_updates');state.mmUpdItems=u?.items||{};applyUpdatesToRows(state.mmUpdItems);applyMmFilter();state.updPage=1;await renderUpdatesPage();}
  setStatus(r?.msg||'同步未返回结果');
 }catch(_){setStatus('同步失败，原更新记录保留');}finally{button.disabled=false;}
});
if ($("#updCheck")) $("#updCheck").addEventListener("click", async () => {
  const st = await api.call("get_mm_update_state").catch(() => null);
  if (st && st.running) {                       // 正在检查 → 再点一次 = 停止
    const r = await api.call("cancel_mm_op");
    setStatus((r && r.msg) || "已请求停止检查");
    return;
  }
  const sel = [...state.updSel];                // 有勾选 → 只检查勾选的这几个
  if (sel.length) { setStatus("只检查勾选的 " + sel.length + " 个模型…"); mmCheckUpdatesFlow(true, sel); return; }
  mmCheckUpdatesFlow(false);
});
if ($("#updWl")) $("#updWl").addEventListener("click", updWhitelistDialog);
if ($("#updDlSel")) $("#updDlSel").addEventListener("click", () => updBatchDownload([...state.updSel]));
if ($("#updAll")) $("#updAll").addEventListener("change", (e) => {
  const on = e.target.checked;
  document.querySelectorAll("#updTbody .upd-cb").forEach((cb) => {
    cb.checked = on;
    const p = cb.dataset.path;
    if (on) state.updSel.add(p); else state.updSel.delete(p);
    const tr = cb.closest("tr");
    if (tr) tr.classList.toggle("is-sel", on);
  });
  _updSyncSel();
});
if ($("#updSearch")) {
  let tmr = null;
  $("#updSearch").addEventListener("input", (e) => {
    clearTimeout(tmr);
    const v = e.target.value;
    tmr = setTimeout(() => { state.updQ = v; state.updPage = 1; renderUpdatesPage(); }, 200);
  });
}
if ($("#updBase")) $("#updBase").addEventListener("change", (e) => { state.updBase = e.target.value; state.updPage = 1; renderUpdatesPage(); });
// 状态筛选已改为 #updTabs 选项卡（点击逻辑在 renderUpdTabs 内绑定）
if ($("#updSort")) $("#updSort").addEventListener("change", (e) => { state.updSortKey = e.target.value; state.updSortRev = (e.target.value === "date"); state.updPage = 1; renderUpdatesPage(); });   // 下拉只有 时间/名称/状态，方向与表头一致
if ($("#updPer")) $("#updPer").addEventListener("change", (e) => { state.updPer = Number(e.target.value) || 50; state.updPage = 1; renderUpdatesPage(); });
// 表头点击排序：同一列再点一次切换升/降序
(function bindUpdHeaderSort() {
  const thead = document.querySelector("#updTable thead");
  if (!thead) return;
  thead.addEventListener("click", (e) => {
    const th = e.target.closest("th.sortable");
    if (!th) return;
    const key = th.dataset.key || "";
    if (!key) return;
    const DEF_REV = { date: true, ver: true };    // 这两列默认"新的/多者在前"
    if (state.updSortKey === key) state.updSortRev = !state.updSortRev;
    else {
      state.updSortKey = key;
      state.updSortRev = !!DEF_REV[key];
    }
    state.updPage = 1;
    renderUpdatesPage();
  });
})();

if ($("#updPager")) $("#updPager").addEventListener("click", (e) => {
  const b = e.target.closest("button.pg");
  if (!b || b.disabled) return;
  state.updPage = Number(b.dataset.p) || 1;
  renderUpdatesPage();
});

// 纯提示弹窗：只有一个「知道了」，点它就关（修"关不掉的弹窗"）
function infoBox(html, title) {
  const mask = document.createElement("div");
  mask.className = "rd-mask";
  const dlg = document.createElement("div");
  dlg.className = "rename-dialog";
  dlg.style.width = "560px";
  dlg.innerHTML =
    '<div class="rd-title">' + (title || "提示") + '<span class="dlg-x" id="ibX" title="关闭（Esc）">' + _icon("x") + "</span></div>" +
    '<div class="ib-body" style="font-size:calc(13px * var(--type-scale, 1));color:var(--text);line-height:1.7">' + html + "</div>" +
    '<div class="rd-actions"><button class="btn btn-primary" id="ibOk">知道了</button></div>';
  const onKey = (e) => { if (e.key === "Escape") close(); };
  let aiOff = null;
  const close = () => {
    if (aiOff) aiOff();
    document.removeEventListener("keydown", onKey); mask.remove(); dlg.remove();
  };
  document.body.appendChild(mask);
  document.body.appendChild(dlg);
  mask.dataset.aiOverlay = "1";
  aiOff = window.CftOverlays
    ? window.CftOverlays.register("info", close,
        { group: "dialogs", kind: "info", title: title || "提示", blocking: false, el: mask })
    : null;
  document.addEventListener("keydown", onKey);
  mask.addEventListener("click", close);
  const ok = dlg.querySelector("#ibOk");
  if (ok) ok.addEventListener("click", close);
  const bx = dlg.querySelector("#ibX");
  if (bx) bx.addEventListener("click", close);
  mask.addEventListener("click", close);
  return close;
}

async function showUpdateResults(items) {
  const rows = Object.entries(items || {}).filter(([, it]) => it && it.has_update);
  const others = Object.entries(items || {}).filter(([, it]) => it && it.other_base);
  if (!rows.length && !others.length) { setStatus("检查完成：没有发现有更新的模型 "); return; }
  const dlg = document.createElement("div");
  dlg.className = "rd-mask";
  const box = document.createElement("div");
  box.className = "rename-dialog";
  box.style.width = "700px";
  const rowHtml = (r) => {
    const it = r[1];
    const nm = r[0].replace(/\\/g, "/").split("/").pop();
    return '<div class="cf-row"><img class="dd-thumb" data-path="' + esc(r[0]) + '" alt=""/>' +
      '<div class="dd-text"><b>' + esc(nm) + "</b>" +
      (it.has_update
        ? ' <span style="color:var(--warn,#d29922)">有新版</span>'
        : ' <span style="color:var(--text-dim)">换底模（未计入）</span>') +
      '<div class="dedup-dir">你本地：' + esc(it.local_base || "-") + " ｜ " +
      (it.has_update ? "同底模最新：" : "最新版：") + esc(it.latest_base || "-") +
      (it.latest_name ? " · " + esc(it.latest_name) : "") +
      (it.latest_date ? " · " + esc(String(it.latest_date).slice(0, 10)) : "") +
      (it.behind > 1 ? " · 落后 " + it.behind + " 个版本" : "") +
      (it.unknown ? esc(it.msg || "") : "") + "</div>" +
      (it.has_update && (it.newer_list || []).length
        ? '<div class="upd-t3">更新后可用：' + it.newer_list.map((v) =>
            '<span class="upd-chip"><a href="#" class="chip-go" data-url="' + esc(v.url || "") + '">' + esc(v.name || v.id) +
            (v.date ? ' <i>' + esc(v.date) + "</i>" : "") + '</a><a href="#" class="chip-dl" data-path="' + esc(r[0]) +
            '" data-vid="' + esc(v.id) + '" data-tip="只下载这一版"></a></span>').join(" ") + "</div>"
        : "") +
      (it.url ? '<a href="#" class="dd-link" data-url="' + esc(it.url) + '">去 C 站看新版</a>' : "") +
      (it.has_update ? ' <button class="btn btn-tiny upd-one" data-path="' + esc(r[0]) + '">更新</button>' : "") +
      "</div></div>";
  };
  box.innerHTML =
    '<div class="rd-title">检查更新：' + rows.length + " 个模型有同底模新版" + (others.length ? "（另有 " + others.length + " 个只换了底模）" : "") + "</div>" +
    '<div class="dd-hint">只列出「<b>和你所用底模相同</b>」的新版本；换了底模的（如 Anima→Krea）单列在下面，<b>不算更新</b>。' +
    "点 去 C 站看新版，或直接点「更新」把新版加入下载队列；模型列表里这些条目已标 ，可用「有更新」筛选。</div>" +
    '<div style="max-height:360px;overflow:auto">' +
    rows.map(rowHtml).join("") +
    (others.length ? '<div class="dedup-sec">最新版换了底模（仅供参考，未计入更新）</div>' + others.map(rowHtml).join("") : "") +
    "</div>" +
    '<div class="rd-actions">' +
    (rows.length ? '<button class="btn btn-primary" id="updAll">全部更新（' + rows.length + "）</button>" : "") +
    '<button class="btn" id="updClose">关闭</button>' +
    '<button class="btn" id="updFilter">只看这些模型</button></div>';
  document.body.appendChild(dlg);
  document.body.appendChild(box);
  dlg.dataset.aiOverlay = "1";
  let aiOff = null;
  const close = () => {
    if (aiOff) aiOff();
    dlg.remove(); box.remove();
  };
  aiOff = window.CftOverlays
    ? window.CftOverlays.register("update_results", close,
        { group: "dialogs", kind: "result", title: "检查更新结果", blocking: false, el: dlg })
    : null;
  dlg.addEventListener("click", close);
  $("#updClose", box).addEventListener("click", close);
  $("#updFilter", box).addEventListener("click", () => {
    close();
    state.mmUpdOnly = true;
    const b = $("#mmUpdOnly");
    if (b) b.classList.add("active");
    applyMmFilter();
    setStatus("已筛选出有更新的模型");
  });
  box.querySelectorAll(".dd-link").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    api.call("open_url", a.dataset.url);
  }));
  box.querySelectorAll(".chip-go").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (a.dataset.url) api.call("open_url", a.dataset.url);
  }));
  box.querySelectorAll(".chip-dl").forEach((a) => a.addEventListener("click", async (e) => {
    e.preventDefault();
    setStatus("正在解析这一版并加入下载队列…");
    const r = await api.call("mm_download_version", a.dataset.path, a.dataset.vid);
    setStatus((r && r.msg) || "完成");
  }));
  const allBtn = $("#updAll", box);
  if (allBtn) allBtn.addEventListener("click", async () => { close(); await mmUpdateFlow(null); });
  box.querySelectorAll(".upd-one").forEach((b) => b.addEventListener("click", async () => {
    b.disabled = true;
    b.textContent = "…";
    await mmUpdateFlow([b.dataset.path]);
    b.textContent = "已加入";
    b.disabled = false;
  }));
  loadDdThumbs(box, rows.concat(others).map((r) => r[0]));
}
if ($("#mmCheckUpd")) $("#mmCheckUpd").addEventListener("click", async () => {
  const st = await api.call("get_mm_update_state").catch(() => null);
  if (st && st.running) {
    const r = await api.call("cancel_mm_op");
    setStatus((r && r.msg) || "已请求停止检查");
    return;
  }
  const sel = [...state.mmChecked];             // 勾了模型就只查勾选的
  if (sel.length) { setStatus("只检查勾选的 " + sel.length + " 个模型…"); mmCheckUpdatesFlow(true, sel); return; }
  mmCheckUpdatesFlow(false);
});

// ===== 更新选中：把勾选（或结果窗里指定）的「有新版」模型加入下载队列 =====
async function mmUpdateFlow(paths) {
  const r = await api.call("mm_update_models", paths && paths.length ? paths : null);
  if (!r || !r.ok) { setStatus((r && r.msg) || "没有可更新的模型"); return false; }
  setStatus(r.msg || "开始更新…");
  const timer = setInterval(async () => {
    const p = await api.call("get_mm_progress");
    if (!p) return;
    if (p.running) { setStatus("更新中 " + (p.done || 0) + "/" + (p.total || 0) + (p.msg ? " · " + p.msg : "")); return; }
    clearInterval(timer);
    setStatus(p.msg || "更新完成");
    if (Array.isArray(p.result) && p.result.length) {
      infoBox("<div style='font-size:calc(12px * var(--type-scale, 1));line-height:1.9'>" +
        p.result.map((f) => "· <b>" + esc(f.file) + "</b>：" + esc(f.msg)).join("<br/>") +
        "</div>", "这些没能加入下载队列");
    }
  }, 800);
  return true;
}
async function mmUpdateSelectedFlow() {
  const paths = state.display.filter((r) => state.mmChecked.has(r.path) && r.upd && r.upd.has_update).map((r) => r.path);
  if (!paths.length) {
    setStatus("没有勾选「有新版」的模型：先点「检查更新」，再用「有更新」筛出来并全选");
    return;
  }
  const ok = await confirmBoxRaw(
    "<div style='font-size:calc(13px * var(--type-scale, 1));line-height:1.8'>将下载以下 <b>" + paths.length + "</b> 个模型的<b>新版</b>：" +
    "<div style='max-height:200px;overflow:auto;margin-top:6px'>" +
    paths.map((p) => "· " + esc(p.replace(/\\/g, "/").split("/").pop())).join("<br/>") + "</div>" +
    "<div style='font-size:calc(12px * var(--type-scale, 1));color:var(--text-dim);margin-top:6px'>新版会下到旧版所在文件夹；<b>旧版文件不会被动</b>（要清理可用「查重」的删旧留新）。</div></div>",
    "更新选中的 " + paths.length + " 个模型");
  if (!ok) return;
  if (ok.root) ok.root.remove();
  await mmUpdateFlow(paths);
}
if ($("#openLogs")) $("#openLogs").addEventListener("click", async () => {
  const r = await api.call("open_logs_dir");
  setStatus((r && r.msg) || "已打开日志文件夹");
});

if ($("#mmGoUpdates")) $("#mmGoUpdates").addEventListener("click", () => {
  state.updState = "upd";                 // 默认只看「有更新」
  state.updQ = "";
  state.updPage = 1;
  switchPage("updates");                  // switchPage 内部会调 renderUpdatesPage（含 tabs 刷新）                  // switchPage 内部会调 renderUpdatesPage
});

if ($("#mmUpdDl")) $("#mmUpdDl").addEventListener("click", () => {
  // 模型管理页的「更新选中」：把勾选里有新版的交给新的批量更新流程（版本按更新页每行的下拉选择）
  const paths = state.display.filter((r) => state.mmChecked.has(r.path) && r.upd && r.upd.has_update).map((r) => r.path);
  if (!paths.length) { setStatus("没有勾选「有新版」的模型：先点「检查更新」，再勾选带 的条目"); return; }
  updBatchDownload(paths);
});
// 点卡片/列表里的 → 打开 C 站新版页面（不会下载）
document.addEventListener("click", (e) => {
  const a = e.target.closest && e.target.closest(".ms-upd, .mm-upd");
  if (!a) return;
  e.preventDefault();
  e.stopPropagation();
  if (a.dataset.url) {
    api.call("open_url", a.dataset.url);
    setStatus("已打开 C 站新版页面（下载需自己决定）");
  }
}, true);

// 排序（点击表头）
$$("#mmTable th[data-sort]").forEach((th) => {
  th.addEventListener("click", () => {
    const c = th.dataset.sort;
    if (state.mmSort.col === c) state.mmSort.rev = !state.mmSort.rev;
    else state.mmSort = { col: c, rev: false };
    renderMm();
  });
});

let mmMoving=false;
async function moveCheckedModels(){
  if(mmMoving)return;
  const paths=state.display.filter(r=>state.mmChecked.has(r.path)).map(r=>r.path);if(!paths.length){setStatus('请先勾选要批量移动的模型');return;}
  mmMoving=true;$('#mmMoveTo').disabled=true;
  try{
    const operation=await api.call('get_mm_progress');if(operation?.running){setStatus('已有模型操作正在进行，请稍后批量移动');return;}
    const dir=await pickFolderModal({title:'选择批量移动目标文件夹',anyFolder:true});if(!dir)return;
    const confirmation=await confirmBoxRaw('<div>将 <b>'+paths.length+'</b> 个勾选模型移动到：</div><p>'+esc(dir)+'</p><p>模型及 JSON、封面、示例图一起移动；同名冲突会跳过，不覆盖。不会修改全局下载保存位置。</p>','批量移动模型');if(!confirmation)return;if(confirmation.root)confirmation.root.remove();
    let done=0;const failed=[];let detailPath=detailRow?.path;
    for(let i=0;i<paths.length;i++){
      const path=paths[i];setStatus('批量移动 '+(i+1)+' / '+paths.length+'：'+path.replace(/\\/g,'/').split('/').pop());
      try{const result=await api.call('move_file_to',path,dir);if(!result?.ok){failed.push({path,msg:result?.msg||'移动失败'});continue;}
        const next=result.path||path;if(next!==path){for(const set of [state.mmChecked,state.mmSel])if(set.delete(path))set.add(next);const row=state.models.find(r=>r.path===path);if(row)row.path=next;if(state.coverCache.has(path)){state.coverCache.set(next,state.coverCache.get(path));state.coverCache.delete(path);}if(detailPath===path)detailPath=next;}done++;
      }catch(_){failed.push({path,msg:'移动未完成，请检查文件位置后重试'});}
    }
    try{const cfg=await api.call('get_config');if(cfg.model_favorites)state.cfg.model_favorites=cfg.model_favorites;}catch(_){}
    _mmFolderTree=null;renderMm();mmScan();if(detailPath && detailRow?.path!==detailPath)await showModelDetail(detailPath);
    setStatus('批量移动完成：成功 '+done+'，失败/跳过 '+failed.length+'（共 '+paths.length+'）');
    if(failed.length)infoBox(failed.map(r=>'<p>'+esc(r.path)+'<br/>'+esc(r.msg)+'</p>').join(''),'未移动的模型');
  }catch(_){setStatus('批量移动未完成，请检查文件位置后重试');}
  finally{mmMoving=false;$('#mmMoveTo').disabled=false;}
}
$('#mmMoveTo').addEventListener('click',moveCheckedModels);

function mmCheckedPaths() {
  const checked = state.display.filter((r) => state.mmChecked.has(r.path)).map((r) => r.path);
  const sel = state.display.filter(r=>state.mmSel.has(r.path)).map(r=>r.path);
  return checked.length ? checked : (sel.length ? sel : null);
}

$("#mmSelAll").addEventListener("click", () => { state.display.forEach((r) => state.mmChecked.add(r.path)); renderMm(); });
$("#mmSelNone").addEventListener("click", () => { state.mmChecked.clear(); renderMm(); });
$("#mmSelInv").addEventListener("click", () => {
  state.display.forEach((r) => state.mmChecked.has(r.path) ? state.mmChecked.delete(r.path) : state.mmChecked.add(r.path));
  renderMm();
});

function mmOp(name, fn) {
  $("#" + name).addEventListener("click", async () => {
    const paths = mmCheckedPaths();
    if (name !== "mmScan" && !paths && !["mmScan"].includes(name)) { setStatus("请先勾选或选中模型"); return; }
    setStatus(name + " 开始 ...");
    fn(paths).then(result=>{
      if(!result?.started){setStatus(result?.msg||"操作未开始");return;}
      pollMmProgress();
    }).catch(()=>setStatus("操作未开始，请重试"));
  });
}

// ===== 文件夹显示：竖排二级菜单 =====
let foldersState = null;
function fmDirectoryVisible(path) {
  let key=String(path).replace(/\\/g,'/').replace(/^\/+|\/+$/g,'').toLowerCase();
  const choices=foldersState.visibility || {};
  if(foldersState.include_subfolders===false)return Object.hasOwn(choices,key)?choices[key]:!key.includes('/');
  while(key){if(Object.hasOwn(choices,key))return choices[key];key=key.includes('/')?key.slice(0,key.lastIndexOf('/')):'';}
  return true;
}
// Parent clicks are explicit subtree operations, not merely inherited defaults.
function fmSubtreeChoices(path, visible) {
  const normal=p=>String(p).replace(/\\/g,'/').replace(/^\/+|\/+$/g,'').toLowerCase();
  const key=normal(path),choices={...(foldersState.visibility||{})};
  if(!key)return choices;
  const inside=p=>normal(p)===key||normal(p).startsWith(key+'/');
  // Reset old descendant overrides too, even if that folder is no longer in the tree.
  for(const p of Object.keys(choices))if(inside(p))choices[p]=visible;
  choices[key]=visible;
  (function walk(nodes){for(const node of nodes||[]){if(inside(node.path))choices[normal(node.path)]=visible;walk(node.children);}})(foldersState.tree);
  return choices;
}
function fmTreeHtml(tree, depth) {
  return (tree||[]).map(n=>{
    const visible=fmDirectoryVisible(n.path);
    return '<div class="fm-item" role="checkbox" tabindex="0" aria-checked="'+visible+'" data-path="'+esc(n.path)+'" style="padding-left:'+(12+depth*18)+'px">'+
      '<input type="checkbox" tabindex="-1" aria-hidden="true"'+(visible?' checked':'')+'/>'+_icon('folder')+'<span class="fm-name">'+esc(n.name)+'</span>'+
      '<span class="fm-state '+(visible?'on':'off')+'">'+(visible?'显示':'排除')+'</span></div>'+fmTreeHtml(n.children,depth+1);
  }).join('');
}
function fmRender() {
  const panel=$('#mmFoldersPanel');if(!foldersState)return;
  const showRoot=foldersState.show_root;
  panel.innerHTML='<div class="fm-title">模型文件夹显示</div><div class="fm-title">'+
    (foldersState.include_subfolders===false?'直属模式：点击父文件夹也会同步勾选或排除已列出的子文件夹。':'默认全部显示。点击父文件夹同步切换整个子树，再单独调整子文件夹。想只显示 A/B：先排除 A，再显示 A/B。显示 A、排除 B：仅排除 B 子树。')+
    ' 不移动或删除文件。</div><div class="fm-toolbar"><button class="btn btn-tiny" id="fmAll">全部显示</button><button class="btn btn-tiny" id="fmNone">全部排除</button></div>'+
    '<div class="fm-item fm-top" role="checkbox" tabindex="0" aria-checked="'+showRoot+'" data-path="__root__"><input type="checkbox" tabindex="-1" aria-hidden="true"'+(showRoot?' checked':'')+'/>'+
    '<span class="fm-name">根目录下的模型</span><span class="fm-state '+(showRoot?'on':'off')+'">'+(showRoot?'显示':'排除')+'</span></div><hr class="fp-sep"/>'+fmTreeHtml(foldersState.tree||[],0);
}
// 独立弹层：锁定工具栏的 overflow 不再裁切文件夹菜单。
let fmAnchor = null, fmRequest = 0, fmSaving = false;
function fmClose(restoreFocus = false) {
  ++fmRequest;
  $("#mmFoldersPanel").classList.remove("open");
  $("#mmFolders").setAttribute("aria-expanded", "false");
  if (restoreFocus && fmAnchor) fmAnchor.focus();
}
function fmPosition() {
  const panel = $("#mmFoldersPanel");
  if (!panel.classList.contains("open") || !fmAnchor) return;
  if (!fmAnchor.getClientRects().length || !$("#page-models").classList.contains("active")) { fmClose(); return; }
  const z = Number(document.documentElement.style.zoom) || 1;
  const rect = fmAnchor.getBoundingClientRect(), gap = 8;
  panel.style.minWidth = "0";
  panel.style.width = Math.min(340 * z, innerWidth - 2 * gap) / z + "px";
  const below = innerHeight - rect.bottom - 2 * gap, above = rect.top - 2 * gap;
  const upwards = below < 180 * z && above > below;
  panel.style.maxHeight = Math.max(60, Math.min(440 * z, upwards ? above : below, innerHeight - 2 * gap)) / z + "px";
  const box = panel.getBoundingClientRect();
  panel.style.left = Math.max(gap, Math.min(rect.left, innerWidth - box.width - gap)) / z + "px";
  panel.style.top = Math.max(gap, Math.min(upwards ? rect.top - box.height - gap : rect.bottom + gap, innerHeight - box.height - gap)) / z + "px";
}
async function fmSave(visibility, showRoot) {
  if (!foldersState || fmSaving) return;
  fmSaving = true;
  const panel = $("#mmFoldersPanel");
  const focusedPath=panel.contains(document.activeElement)?document.activeElement.closest(".fm-item")?.dataset.path:null;
  panel.setAttribute("aria-busy", "true");
  try {
    const nextHidden=Object.keys(visibility).filter(p=>visibility[p]===false);
    const saved = await api.call("save_folders", nextHidden, showRoot, visibility);
    if (saved !== true) throw new Error("save_folders failed");
    foldersState.hidden = nextHidden; foldersState.visibility=visibility; foldersState.show_root = showRoot;
    Object.assign(state.cfg,{hidden_model_folders:nextHidden,model_folder_visibility:{...visibility},show_root_models:showRoot});
    // A changed folder scope must not leave hidden models selected while the scan refreshes.
    state.mmChecked.clear();state.mmSel.clear();state.mmLastSel=-1;renderMm();
    fmRender(); fmPosition();
    if(focusedPath)panel.querySelector('.fm-item[data-path="'+CSS.escape(focusedPath)+'"]')?.focus();
    setStatus("文件夹显示已保存（仅筛选显示，不移动或删除模型）");
    mmScan();
  } catch (err) { setStatus("文件夹显示保存失败，请重试"); }
  finally { fmSaving = false; panel.removeAttribute("aria-busy"); }
}
$("#mmFoldersPanel").addEventListener("click", (e) => {
  e.stopPropagation();
  const btn = e.target.closest("#fmAll, #fmNone");
  if (btn && foldersState) {
    const all = [];
    (function walk(nodes) { (nodes || []).forEach(n => { all.push(n.path); walk(n.children); }); })(foldersState.tree);
    const visible=btn.id==='fmAll';
    fmSave(Object.fromEntries(all.map(p=>[p.replace(/\\/g,'/').toLowerCase(),visible])),visible);
    return;
  }
  const item = e.target.closest(".fm-item");
  if (!item || !foldersState) return;
  e.preventDefault();
  const path=item.dataset.path, choices=path==="__root__"?{...(foldersState.visibility||{})}:fmSubtreeChoices(path,!fmDirectoryVisible(path));
  fmSave(choices,path==="__root__"?!foldersState.show_root:foldersState.show_root);
});
$("#mmFoldersPanel").addEventListener("keydown",e=>{if((e.key===' '||e.key==='Enter')&&e.target.matches('.fm-item')){e.preventDefault();e.target.click();}});
$("#mmFolders").addEventListener("click", async (e) => {
  e.stopPropagation();
  const panel = $("#mmFoldersPanel");
  if (panel.classList.contains("open")) { fmClose(); return; }
  fmAnchor = e.currentTarget.getClientRects().length ? e.currentTarget : $("[data-menu='mmMoreMenu']");
  if (panel.parentElement !== document.body) document.body.appendChild(panel);
  panel.classList.add("open");
  $("#mmFolders").setAttribute("aria-expanded", "true");
  panel.innerHTML = '<div class="fm-title">正在读取文件夹…</div>';
  fmPosition();
  const request = ++fmRequest;
  try {
    const json = await api.call("get_folders");
    if (request !== fmRequest) return;
    const data = typeof json === "string" ? JSON.parse(json || "{}") : json;
    if (!data || !data.root || !Array.isArray(data.tree)) {
      panel.innerHTML = '<div class="fm-title">请先在设置中配置模型管理目录</div>'; return;
    }
    foldersState = {...data, hidden: data.hidden || [], visibility:data.visibility||Object.fromEntries((data.hidden||[]).map(p=>[p.replace(/\\/g,'/').toLowerCase(),false])), show_root: data.show_root !== false};
    _mmFolderTree = data.tree;
    fmRender();
    if (!data.tree.length) panel.insertAdjacentHTML("beforeend", '<div class="fm-title">暂无子文件夹，根目录模型仍可单独切换</div>');
  } catch (err) {
    if (request === fmRequest) { panel.innerHTML = '<div class="fm-title">文件夹读取失败，请关闭后重试</div>'; setStatus("文件夹读取失败，请重试"); }
  } finally { if (request === fmRequest) fmPosition(); }
});
document.addEventListener("click", e => {
  if (!e.target.closest("#mmFoldersWrap, #mmFoldersPanel, [data-proxy='mmFolders']")) fmClose();
});
document.addEventListener("keydown", e => { if (e.key === "Escape" && $("#mmFoldersPanel").classList.contains("open")) fmClose(true); });
window.addEventListener("resize", fmPosition);
window.addEventListener("cft:zoom", fmPosition);
document.addEventListener("scroll", e => { if (!$("#mmFoldersPanel").contains(e.target)) fmPosition(); }, true);

// 详情单模型立即反查：不切页，不启动批量队列里的其它模型。
let detailRpPath=null;
async function identifyDetailModel(path){
  if(detailRpPath){setStatus('当前已有模型正在识别，请稍候');return false;}
  detailRpPath=path;let failures=0;
  const updateButton=()=>{if($('#dRp'))$('#dRp').disabled=!!detailRpPath;};updateButton();
  try{
    const start=await api.call('rp_identify_model',path);
    if(!start?.started){setStatus(start?.msg||'识别未启动');return false;}
    setStatus('正在识别当前模型：'+String(path).split(/[\\/]/).pop());
    while(true){
      await new Promise(r=>setTimeout(r,600));
      let progress,rows;
      try{[progress,rows]=await Promise.all([api.call('rp_state'),api.call('rp_get_rows')]);failures=0;}
      catch(_){if(++failures<3)continue;setStatus('暂时无法读取进度；后端识别仍在继续，请稍后检查');return false;}
      const row=(rows||[]).find(r=>normalizedModelPath(r.path)===normalizedModelPath(path));
      if(progress?.running){setStatus((progress.msg||'正在反查')+' · '+(progress.progress||0)+'% · '+String(path).split(/[\\/]/).pop());continue;}
      if(row?.status==='成功'){
        const parsed=JSON.parse((await api.call('get_scan_rows'))||'[]');if(Array.isArray(parsed)){state.models=parsed;applyMmFilter();}
        if(normalizedModelPath(detailRow?.path)===normalizedModelPath(path))await showModelDetail(path);
        setStatus('模型识别完成：'+(row.model||String(path).split(/[\\/]/).pop()));return true;
      }
      setStatus('模型识别'+(row?.status||'未完成')+(row?.model?'：'+row.model:''));return false;
    }
  }catch(_){setStatus('当前模型识别启动失败，请检查日志或网络后重试');return false;}
  finally{detailRpPath=null;updateButton();}
}

// 模型详情二级界面（C 站风格）
let detailRow = null;
let detailGeneration = 0;
let _detailAiOff = null;
// 详情面板全局状态（document 级图片右键委托需要访问）
let detailImgIdx = 0;
let detailImgLocalPath = null;
// ===== 简介富文本：白名单 sanitize + 纯文本转换（零依赖；不直接 innerHTML 注入 C 站原始 HTML） =====
const DESC_OK_TAGS = { P: 1, BR: 1, STRONG: 1, B: 1, EM: 1, I: 1, U: 1, S: 1, DEL: 1, INS: 1, UL: 1, OL: 1, LI: 1, H1: 1, H2: 1, H3: 1, H4: 1, BLOCKQUOTE: 1, CODE: 1, PRE: 1, A: 1, IMG: 1, HR: 1, SPAN: 1, DIV: 1, FONT: 1, SUB: 1, SUP: 1 };
const DESC_DROP_TAGS = { SCRIPT: 1, STYLE: 1, IFRAME: 1, OBJECT: 1, EMBED: 1, FORM: 1, INPUT: 1, BUTTON: 1, TEXTAREA: 1, SELECT: 1, LINK: 1, META: 1, SVG: 1, MATH: 1, VIDEO: 1, AUDIO: 1, SOURCE: 1 };
function sanitizeDescHtml(html) {
  if (!html) return "";
  let doc;
  try { doc = new DOMParser().parseFromString(String(html), "text/html"); } catch (e) { return ""; }
  const body = doc.body;
  if (!body) return "";
  const walk = (node) => {
    Array.from(node.childNodes).forEach((ch) => {
      if (ch.nodeType === 3) return;                       // 纯文本：保留
      if (ch.nodeType !== 1) { ch.remove(); return; }      // 注释/处理指令：丢
      const tag = ch.tagName;
      if (DESC_DROP_TAGS[tag]) { ch.remove(); return; }    // script/iframe/style…：连内容一起丢
      if (!DESC_OK_TAGS[tag]) {
        walk(ch);                                          // 未知标签（如 <section>、<figure>）：原样展开、保留文字
        while (ch.firstChild) node.insertBefore(ch.firstChild, ch);
        ch.remove();
        return;
      }
      Array.from(ch.attributes).forEach((at) => {          // 属性白名单：其余全删（含 style/class/on*）
        const n = at.name.toLowerCase();
        const keep = (tag === "A" && n === "href") || (tag === "IMG" && (n === "src" || n === "alt"));
        if (!keep) ch.removeAttribute(at.name);
      });
      if (tag === "A") {
        const href = ch.getAttribute("href") || "";
        if (/^https?:\/\//i.test(href)) { ch.setAttribute("target", "_blank"); ch.setAttribute("rel", "noopener noreferrer"); }
        else ch.removeAttribute("href");                   // javascript:/data: 等一律拆掉链接
      }
      if (tag === "IMG") {
        const src = ch.getAttribute("src") || "";
        if (/^https?:\/\//i.test(src) || /^data:image\//i.test(src)) ch.setAttribute("loading", "lazy");
        else ch.remove();
      }
      walk(ch);
    });
  };
  walk(body);
  return body.innerHTML;
}
// 富文本 → 人类可读纯文本（段落保留换行、列表加 •，复制用）
function descToPlain(html) {
  if (!html) return "";
  const BLOCK = { P: 1, DIV: 1, H1: 1, H2: 1, H3: 1, H4: 1, LI: 1, BLOCKQUOTE: 1, PRE: 1 };
  let doc;
  try { doc = new DOMParser().parseFromString("<div id='__r'>" + sanitizeDescHtml(html) + "</div>", "text/html"); }
  catch (e) { return String(html).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim(); }
  const root = doc.getElementById("__r");
  if (!root) return "";
  const out = [];
  const rec = (n) => {
    if (n.nodeType === 3) { out.push(n.nodeValue.replace(/\s+/g, " ")); return; }
    if (n.nodeType !== 1) return;
    const t = n.tagName;
    if (t === "BR") { out.push("\n"); return; }
    if (BLOCK[t]) out.push("\n");
    if (t === "LI") out.push("• ");
    Array.from(n.childNodes).forEach(rec);
    if (BLOCK[t]) out.push("\n\n");
  };
  Array.from(root.childNodes).forEach(rec);
  return out.join("").replace(/[ \t]+\n/g, "\n").replace(/\n{3,}/g, "\n\n").replace(/[ \t]{2,}/g, " ").trim();
}

// ===== 从 C 站同步：Metro 对话框（四态状态机 + 实际更新字段 diff） =====
function syncDialogClose(d) {
  try { document.removeEventListener("keydown", d.onKey); } catch (e) { }
  try { d.mask.remove(); } catch (e) { }
  try { d.dlg.remove(); } catch (e) { }
}
function syncDialogOpen() {
  const mask = document.createElement("div");
  mask.className = "rd-mask";
  const dlg = document.createElement("div");
  dlg.className = "rename-dialog m-dlg";
  dlg.style.width = "520px";
  const d = { mask: mask, dlg: dlg, onKey: null };
  d.onKey = (e) => { if (e.key === "Escape") syncDialogClose(d); };
  document.addEventListener("keydown", d.onKey);
  mask.addEventListener("click", () => syncDialogClose(d));
  document.body.appendChild(mask);
  document.body.appendChild(dlg);
  return d;
}
function syncDialogRender(d, st) {
  const dlg = d.dlg; if (!dlg) return;
  const ICON = { run: "refresh", ok: "check", fail: "x", notfound: "search", net: "alert" };
  const TXT = { run: "正在从 C 站同步 …", ok: "同步成功", fail: "同步失败", notfound: "未找到对应 C 站模型", net: "无法连接 C 站" };
  const cls = st.state === "ok" ? "ok" : (st.state === "run" ? "run" : (st.state === "notfound" ? "warn" : "fail"));
  let html = '<div class="rd-title">从 C 站同步<span class="dlg-x" id="sdX" title="关闭（Esc）">' + _icon("x") + "</span></div>" +
    '<div class="sb-status ' + cls + '">' + _icon(ICON[st.state] || "info") + "<span>" + (TXT[st.state] || "") +
    (st.note ? "（" + esc(String(st.note)) + "）" : "") + "</span></div>";
  if (st.model) html += '<div class="sb-kv"><span class="k">模型</span><span class="v">' + esc(String(st.model)) + "</span></div>";
  if (st.version) html += '<div class="sb-kv"><span class="k">版本</span><span class="v">' + esc(String(st.version)) + "</span></div>";
  if (st.state === "ok") {
    if (st.changed && st.changed.length) {
      html += '<div class="sb-fields"><div class="sb-fields-h">已更新字段（' + st.changed.length + " 项）</div><ul>" +
        st.changed.map((x) => "<li>" + esc(x) + "</li>").join("") + "</ul></div>";
    } else {
      html += '<div class="sb-fields"><div class="sb-dim">信息已是最新，无需变更。</div></div>';
    }
  } else if (st.state === "notfound") {
    html += '<div class="sb-dim">C 站没有匹配到该模型；可到「反向解析」页用哈希排查，或确认该模型是否已从 C 站删除。</div>';
  } else if (st.err) {
    html += '<div class="sb-err">' + esc(String(st.err).slice(0, 200)) + "</div>";
  }
  if (st.state !== "run") html += '<div class="rd-actions"><button class="btn btn-primary" id="sdOk">知道了</button></div>';
  dlg.innerHTML = html;
  const bx = dlg.querySelector("#sdX"); if (bx) bx.addEventListener("click", () => syncDialogClose(d));
  const okb = dlg.querySelector("#sdOk"); if (okb) okb.addEventListener("click", () => syncDialogClose(d));
}

// ===== 详情后台补帧进度 =====
// 后端 get_model_detail 结尾已经自动排了一次补帧任务（受 config.images_dir_autofill 控制），
// 这里只负责轮询展示与取消：绝不自己再起任务、绝不每次轮询重建 DOM（重排会把详情里的视频/滚动位置打断）。
const _AUTOFILL_POLL_MS = 1500;
const _AUTOFILL_EMPTY_TICKS = 3;   // 连续 3 次「没有任务」就停：本地已齐/开关关闭时不该一直问后端
let _autofillTimer = null, _autofillSeq = 0;
function _autofillStop() { _autofillSeq++; if (_autofillTimer) { clearInterval(_autofillTimer); _autofillTimer = null; } }
function _autofillJson(x) {
  if (x && typeof x === "object") return x;
  try { return JSON.parse(String(x || "{}")); } catch (_) { return {}; }
}
function _autofillRow(panel, path) {
  let row = panel.querySelector("#dtAutofill");
  if (row) return row;
  const wrap = panel.querySelector(".detail-left") || panel.querySelector(".dt-body");
  if (!wrap) return null;
  wrap.insertAdjacentHTML("beforeend",
    '<p class="dt-autofill" id="dtAutofill" hidden role="status"><span class="dt-af-text"></span>' +
    '<button type="button" class="btn btn-tiny dt-af-cancel" title="取消补帧：剩余帧本轮不再下载，已落盘的保留">取消</button></p>');
  row = panel.querySelector("#dtAutofill");
  row.querySelector(".dt-af-cancel").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      const r = _autofillJson(await api.call("cancel_images_autofill", path));
      _autofillText(row, (r && (r.msg || r.canceled)) ? (r.msg || "已请求取消") : "取消请求失败");
    } catch (_) {
      _autofillText(row, "取消请求失败");
    } finally { btn.disabled = false; }
  });
  return row;
}
function _autofillText(row, text, running) {
  const el = row.querySelector(".dt-af-text");
  if (el) el.textContent = text || "";   // 只换文本：不重排、不重建详情 DOM
  row.hidden = !text;
  const btn = row.querySelector(".dt-af-cancel");
  if (btn) btn.hidden = running !== true;
}
function startAutofillWatch(d, generation, panel) {
  _autofillStop();
  const seq = _autofillSeq;
  const path = String(d.path || "");
  if (!path || d.cached_history) return;
  let emptyTicks = 0;
  const alive = () => seq === _autofillSeq && generation === detailGeneration && detailRow === d &&
    $("#detailMask") && $("#detailMask").style.display === "flex" && $("#page-models").classList.contains("active");
  const tick = async () => {
    if (!alive()) { _autofillStop(); return; }
    let st;
    try { st = _autofillJson(await api.call("get_images_autofill_state", path)); }
    catch (_) { _autofillStop(); return; }
    if (!alive()) return;   // 轮询期间已离开详情：直接放弃这一帧
    if (!st || st.ok === false) { _autofillStop(); return; }
    const total = Number(st.total) || 0, done = Number(st.done) || 0;
    const failed = Number(st.failed) || 0, skipped = Number(st.skipped) || 0;
    const downloaded = Number(st.downloaded) || 0;
    const msg = String(st.msg || "");
    if (!st.running) {
      if (!total) {   // 该模型没有补帧任务（本地已齐 / 开关关闭 / 全局闸门被占）：静默退出
        if (++emptyTicks >= _AUTOFILL_EMPTY_TICKS) _autofillStop();
        return;
      }
      const row = _autofillRow(panel, path);
      if (row) _autofillText(row, "补帧结束 " + done + "/" + total +
        (downloaded ? "，新增 " + downloaded : "") + (failed ? "，失败 " + failed : "") +
        (skipped ? "，跳过 " + skipped : "") + (msg ? "：" + msg : ""), false);
      // 刚落地的首帧要立刻出现在当前详情里：以前得关掉重开才有画面，而"打开就是空白"正是用户报的现象。
      // 只在"本来就没东西可看"时重渲染——主图已有内容、或用户已经选过某一帧，都不打扰他正在看的东西。
      if (downloaded > 0 && detailRow === d && generation === detailGeneration &&
          $("#detailMask") && $("#detailMask").style.display === "flex") {
        const main = panel.querySelector("#dMain");
        const blankish = main && (main.tagName === "VIDEO"
          ? !String(main.getAttribute("poster") || "")
          : main.dataset && main.dataset.empty === "true");
        if (blankish) showModelDetail(path);
      }
      _autofillStop();
      return;
    }
    emptyTicks = 0;
    const row = _autofillRow(panel, path);
    if (row) _autofillText(row, "正在补齐示例图 " + done + "/" + total +
      (failed ? "，失败 " + failed : "") + (skipped ? "，跳过 " + skipped : "") +
      (msg ? "：" + msg : ""), true);
  };
  _autofillTimer = setInterval(tick, _AUTOFILL_POLL_MS);
  tick();
}

async function showModelDetail(path, historyId = "") {
  const generation = ++detailGeneration;
  let json;try{json = await api.call(historyId ? "get_history_detail" : "get_model_detail", historyId || path);}catch(_){if(generation===detailGeneration)setStatus('详情读取异常，请检查元数据或刷新后重试');return;}
  let d;
  try { d = typeof json==='object' && json!==null?json:JSON.parse(json || "{}"); } catch (e) { d = {}; }
  if (generation !== detailGeneration || !$("#page-models").classList.contains("active")) return;
  if (!d.ok) { setStatus(d.msg||"详情获取失败：元数据格式异常或文件不可读取"); return; }
  detailRow = d;
  detailRow.history_id = historyId;
  document.body.classList.add("models-workspace");
  const info = d.info || {};
  const v = info.version || {};
  const creator = (info.creator && info.creator.username) || info.creator || "";
  const trained = Array.isArray(info.trainedWords) ? info.trainedWords : [];
  const descOrig = String(info.description || "");            // C 站原始 description（HTML 富文本，保持原样）
  const descZh = String(info.description_zh || "");           // 中文翻译（纯文本）
  const descIsRich = /<[a-z][\s\S]*>/i.test(descOrig);       // 含标签 → 富文本
  const descRich = descIsRich ? sanitizeDescHtml(descOrig) : "";
  const descPlain = descIsRich ? descToPlain(descOrig) : descOrig;   // 复制用：纯文本、保留段落
  const descZhIsRich = /<[a-z][\s\S]*>/i.test(descZh);      // 中文翻译里带标签（旧数据）→ 按富文本渲染
  const descRaw = descPlain;                                  // 兼容旧引用
  const desc = descPlain;
  const covers = d.covers || [];
  detailImgIdx = 0;
  detailImgLocalPath = null;
  const mainB64 = covers.find((c) => c.b64);
  const firstVideo = (covers.length && covers[0] && covers[0].type === "video") ? covers[0] : null;   // 首个示例是视频时主图直接渲染 <video>
  detailImgLocalPath = (mainB64 && mainB64.local && d.path) ? d.path : null;
  detailImgIdx = 0;
  const panel = $("#detailPanel");
  panel.innerHTML =
    '<div class="dt-head">' +
      '<div class="dt-head-t">模型详情</div>' +
      '<div class="dt-head-r">' +
        (creator ? '<span class="dt-author author-chip" data-author="' + esc(creator) + '" data-tip="点击复制作者链接">' + esc(creator) + '</span>' : "") +
        '<button class="icon-btn dt-close" id="dFavorite" aria-pressed="'+isModelFavorite(d.path)+'" title="收藏并置顶 / 取消收藏">' + _icon('star') + '</button>' +
      '</div>' +
    '</div>' +
    '<div class="dt-body">' +
    '<div class="detail-left">' +
    (firstVideo
      ? '<video class="detail-main-img" id="dMain" controls preload="metadata" title="放大观看视频（播放器自带全屏）" src="' + esc(_playableMediaUri(firstVideo.url)) + '" poster="' + esc(firstVideo.poster || "") + '"></video>'
      : mainB64
        ? '<img class="detail-main-img" id="dMain" src="data:image/jpeg;base64,' + mainB64.b64 + '"/>'
        : covers.length?'<img class="detail-main-img" id="dMain" data-empty="true" alt="示例图片"/>':'<div class="detail-main-img dt-empty" id="dMain">' + _icon("image", "ic-lg") + '<span>暂无封面</span></div>') +
    '<div class="detail-thumbs">' + covers.map((c, i) =>
      c.type === "video"
        ? '<span class="detail-thumb-box"><img class="detail-thumb' + (i === 0 ? " on" : "") + '" data-i="' + i + '" data-url="' + esc(c.poster || "") + '" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"/><i class="thumb-video-badge" aria-hidden="true">▶</i></span>'
        : (c.b64
            ? '<img class="detail-thumb' + (i === 0 ? " on" : "") + '" data-i="' + i + '" src="data:image/jpeg;base64,' + c.b64 + '"/>'
            : '<img class="detail-thumb" data-i="' + i + '" data-url="' + esc(c.url || "") + '" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"/>')
    ).join("") + "</div></div>" +
    '<div class="detail-right">' +
    '<div class="detail-title">' + esc(info.name || d.name || "-") + "</div>" +
    (info.association?.hash_verified === false ? '<p class="binding-warning">手动关联来源信息 · SHA256 校验不一致，不能据此确认文件与官方模型相同。</p>' : '') +
    (info.modelName && info.modelName !== info.name ? '<div class="detail-cname">' + esc(info.modelName) + "</div>" : "") +
    '<div class="dt-mrs">' +
      '<div class="dt-mr"><span class="k">类型</span><span class="v">' + esc(info.type || "-") + "</span></div>" +
      '<div class="dt-mr"><span class="k">底模</span><span class="v">' + esc(info.baseModel || d.base || "-") + "</span></div>" +
      '<div class="dt-mr"><span class="k">版本</span><span class="v">' + esc(v.name || d.ver || "-") + "</span></div>" +
      '<div class="dt-mr"><span class="k">文件</span><span class="v dt-mono" data-tip="' + esc(d.path || "") + '">' + esc(d.name || "-") + "</span></div>" +
      (info.nsfw ? '<div class="dt-mr"><span class="k">分级</span><span class="v dt-warn">NSFW</span></div>' : "") +
    "</div>" +
    '<div class="dt-sec"><div class="dt-sec-h">' + _icon("tag") + '触发词<span class="dt-sec-hint">点击复制（英文原文）</span></div>' +
      '<div class="detail-tags">' + (trained.length ? trained.map((t, ti) => {
        return '<div class="detail-tag copy-tag" data-orig="' + esc(t) + '" data-tip="点击复制这一套">' +
          (trained.length > 1 ? '<span class="detail-tag-idx">' + (ti + 1) + "</span>" : "") +
          '<span class="detail-tag-txt">' + esc(t) + "</span>" +
          '<span class="dt-copy">' + _icon("copy") + "</span></div>";
      }).join("") : '<span class="dt-dim">无触发词信息（可先「识别模型信息」）</span>') + "</div></div>" +
    '<div class="dt-sec"><div class="dt-sec-h">' + _icon("file") + '简介' +
      (descPlain || descZh ? '<button class="btn dt-copy" id="dCopyDesc" data-tip="复制纯文本简介（保留段落与列表）">' + _icon("copy") + '复制</button>' : "") + '</div>' +
      '<div class="detail-desc' + (descIsRich ? " rich" : "") + '">' + (descIsRich
        ? descRich
        : (descPlain || descZh ? esc(descPlain || descZh) : '<span class="dt-dim">暂无简介</span>')) + '</div>' +
      (descZh ? '<div class="detail-desc-zh' + (descZhIsRich ? "" : " plain") + '"><span class="dz-label">' + _icon("globe") + '中文翻译</span>' +
        (descZhIsRich ? sanitizeDescHtml(descZh) : esc(descZh)) + "</div>" : "") + "</div>" +
    '<div class="dt-sec"><div class="dt-sec-h">' + _icon("settings") + '操作</div>' +
      '<div class="dt-ag"><div class="dt-ag-h">主要操作</div><div class="dt-ag-b">' +
        '<button class="btn btn-primary" id="dEditInfo">' + _icon("pencil") + '编辑信息</button>' +
        '<button class="btn" id="dSite" data-tip="在浏览器打开该模型在 C 站的主页">' + _icon("external") + '打开 C 站</button>' +
      "</div></div>" +
      '<div class="dt-ag"><div class="dt-ag-h">文件</div><div class="dt-ag-b">' +
        '<button class="btn" id="dRename" data-tip="自定义改名（保留扩展名）">' + _icon("pencil") + '改名</button>' +
        '<button class="btn" id="dRenameC" data-tip="先预览，确认后仅改这个模型及附属文件">' + _icon('file') + '文件名 → C站名称</button>' +
        '<button class="btn" id="dCover" data-tip="用本地图片替换封面">' + _icon("image") + '设置封面</button>' +
      "</div></div>" +
      '<div class="dt-ag"><div class="dt-ag-h">信息处理</div><div class="dt-ag-b">' +
        '<button class="btn" id="dRp" data-tip="从 C 站匹配该模型的名字/触发词/封面">' + _icon("upload") + '识别模型信息</button>' +
        '<button class="btn" id="dSync" data-tip="立即从 C 站重新匹配并写回本模型信息（模型名/触发词/版本/封面，沿用现有识别逻辑）">' + _icon("refresh") + '从 C 站同步</button>' +
        '<button class="btn" id="dTranslate" data-tip="把简介翻译成中文（需在设置配置百度翻译）">' + _icon("globe") + '翻译成中文</button>' +
        '<button class="btn" id="dLocalize" data-tip="把本地文件名翻译成中文">' + _icon("globe") + '文件名翻中文</button>' +
        '<button class="btn" id="dJson" data-tip="生成 WebUI 能识别的元数据文件">' + _icon("file") + '生成 SD 可读 JSON</button>' +
      "</div></div>" +
      '<div class="dt-ag"><div class="dt-ag-h">图片</div><div class="dt-ag-b">' +
        '<button class="btn" id="dAllImgs" data-tip="把 C 站该模型的示例图（最多 20 张，视频取首帧）下载到「模型名.images」文件夹">' + _icon("download") + '下载示例图</button>' +
      "</div></div>" +
    "</div>" +
    "</div></div>";
  $("#detailMask").style.display = "flex";
  if (window.CftOverlays) {
    _detailAiOff = window.CftOverlays.register("detail", closeDetail,
      { group: "dialogs", kind: "detail", title: "模型详情", blocking: false, el: $("#detailMask") });
  }
  if (d.cached_history) {
    panel.querySelector('.detail-title').insertAdjacentHTML('afterend', '<p class="history-model">历史信息缓存 · 本地文件已移走或不存在，仅供查看</p>');
    panel.querySelectorAll('#dEditInfo,#dRename,#dRenameC,#dCover,#dRp,#dSync,#dTranslate,#dLocalize,#dJson,#dAllImgs').forEach(button => button.disabled = true);
  }
  // 自动加载所有 URL 缩略图（避免空占位）；视频项取首帧图（poster），绝不把 mp4 直链当图片请求
  covers.forEach((c, i) => {
    const thumbUrl = c.type === "video" ? (c.poster || "") : (c.url || "");
    // poster 现在可能是 data:（磁盘已有首帧，后端直接给帧字节）。get_cover_b64 只认 http(s)、对 data: 返回空串，
    // 走下面的 RPC 会把这张本来有图的缩略图整个 remove 掉——本地已有帧就直接当 src 用，不发 RPC。
    if (!c.b64 && /^data:/i.test(thumbUrl)) {
      const th = panel.querySelector('.detail-thumb[data-i="' + i + '"]');
      if (th) th.src = thumbUrl;
      return;
    }
    if (!c.b64 && thumbUrl) {
      api.call("get_cover_b64", thumbUrl).then((b64) => {
        if (generation !== detailGeneration) return;
        if (!b64) {
          // 加载失败：移除该占位缩略图
          const th = panel.querySelector('.detail-thumb[data-i="' + i + '"]');
          if (th) th.remove();
          return;
        }
        c.b64 = b64;
        const th = panel.querySelector('.detail-thumb[data-i="' + i + '"]');
        if (th) th.src = "data:image/jpeg;base64," + b64;
        const main = $("#dMain");
        if (main?.tagName==="IMG" && detailImgIdx===i && main.dataset.hdIndex!==String(i) && (!main.src || main.dataset.empty)) {
          main.src = "data:image/jpeg;base64," + b64;
          main.style.opacity = "1";delete main.dataset.empty;
        }
      });
    }
  });
  // 首帧静图缺失的老视频：主视频播起来时抓一帧回传落盘，下次打开详情/瀑布流就是本地静帧。
  // 已有 poster（本地帧或 info 的 imageUrl）就不抓——抓一次是白烧一路解码 + 一次 RPC。
  if (firstVideo && !firstVideo.poster) captureModelVideoPoster($("#dMain", panel), d.path, _coverVideoInfoIndex(info, firstVideo))
    .then((du) => { if (du) firstVideo.poster = du; });
  const bindMainImage=()=>{   // 图片主图绑定“点击放大查看”（同一 img 只绑一次）；视频主图有原生控制条，不抢点击
    const m=$("#dMain",panel);if(!m||m.tagName!=="IMG"||m.dataset.mainBound)return;
    m.dataset.mainBound="1";
    m.addEventListener("click",()=>openImageViewer(detailRow,detailImgIdx));
    m.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();openImageViewer(detailRow,detailImgIdx);}});
    m.setAttribute("title", "点击放大图片并查看生成数据");m.tabIndex=0;
  };
  bindMainImage();
  // 画廊切换
  let imageChoiceGeneration=0;
  const loadB64=async i=>{
    const c=covers[i];if(!c)return;
    const choice=++imageChoiceGeneration;detailImgIdx=i;detailImgLocalPath=c.local?d.path:null;
    panel.querySelectorAll('.detail-thumb').forEach(t=>t.classList.toggle('on',Number(t.dataset.i)===i));
    const current=()=>generation===detailGeneration && detailImgIdx===i && choice===imageChoiceGeneration;
    const prevMain=$("#dMain",panel);
    // 视频项：主图换 <video controls>，mp4 直连远程（不走 b64），poster 用首帧图；切走前先停旧视频
    if(c.type==='video'&&c.url){
      if(prevMain&&prevMain.tagName==='VIDEO')prevMain.pause();
      let video=$("#dMain",panel);
      if(!video||video.tagName!=='VIDEO'){
        video=document.createElement('video');video.className='detail-main-img';video.id='dMain';
        if(prevMain)prevMain.replaceWith(video);
        video=$("#dMain",panel)||video;
      }
      video.controls=true;video.preload='metadata';video.autoplay=true;
      video.src=_playableMediaUri(c.url);video.poster=c.poster||'';
      video.setAttribute('title','放大观看视频（播放器自带全屏）');video.tabIndex=0;
      if(video.onclick)video.onclick=null;
      delete video.dataset.empty;delete video.dataset.hdIndex;
      if(!c.poster)captureModelVideoPoster(video,d.path,_coverVideoInfoIndex(info,c)).then(du=>{if(du)c.poster=du;});
      return;
    }
    // 图片项：先停掉并移除主图区的视频
    if(prevMain&&prevMain.tagName==='VIDEO'){
      prevMain.pause();
      const img=document.createElement('img');img.className='detail-main-img';img.id='dMain';
      prevMain.replaceWith(img);
    }
    const image=$("#dMain",panel);delete image.dataset.hdIndex;
    if(image.tagName==='IMG')bindMainImage();
    const warm=typeof warmViewedGallery==='function'?await warmViewedGallery(detailRow,i):null;
    if(!current())return;
    if(warm){image.src=warm.url;image.dataset.hdIndex=String(i);image.style.opacity='1';delete image.dataset.empty;return;}
    if(c.b64){image.src='data:image/jpeg;base64,'+c.b64;delete image.dataset.empty;}
    // data: 直接当 src：get_cover_b64 只认 http(s)，把 data: 喂进去只会拿到空串
    else if(/^data:/i.test(c.url||'')){image.src=c.url;delete image.dataset.empty;}
    else if(c.url){
      image.style.opacity='.5';const b64=await api.call('get_cover_b64',c.url);
      if(!current())return;
      if(b64){image.src='data:image/jpeg;base64,'+b64;c.b64=b64;delete image.dataset.empty;}image.style.opacity='1';
    }
  };
  panel.querySelectorAll(".detail-thumb").forEach((t) => {
    t.addEventListener("click", () => loadB64(Number(t.dataset.i)));
  });
  // 操作
  $("#dSite", panel).addEventListener("click", () => {
    const mid = info.modelId || info.id || info.model_id;
    api.call("open_url", "https://" + (state.cfg.site_domain || "civitai.red") + "/models/" + (mid || ""));
  });

  panel.querySelectorAll(".author-chip").forEach((el) => {
    el.addEventListener("click", async () => {
      const uname = el.dataset.author || "";
      const url = "https://" + (state.cfg.site_domain || "civitai.red") + "/user/" + uname;
      try { await window.__copyText(url); setStatus("作者链接已复制: " + url); }
      catch (e) { setStatus("复制失败"); }
    });
  });
  panel.querySelectorAll(".copy-tag").forEach((tg) => {
    tg.addEventListener("click", async () => {
      // 优先复制英文原文（data-orig）——WebUI 触发词必须用英文；没有翻译时回退显示文本
      const orig = tg.dataset.orig;
      const el = tg.querySelector(".detail-tag-txt") || tg;
      const ok = await window.__copyText(orig || el.textContent.trim());
      setStatus(ok ? "已复制该套触发词（英文原文）" : "复制失败");
    });
  });
  $("#dCover").addEventListener("click", () => {
    const inp = document.createElement("input");
    inp.type = "file";
    inp.accept = "image/*";
    inp.addEventListener("change", async () => {
      const f = inp.files && inp.files[0];
      if (!f) return;
      const reader = new FileReader();
      reader.onload = async () => {
        const b64 = String(reader.result).split(",").pop();
        setStatus("上传封面中 …");
        const res = await api.call("set_custom_cover", path, b64);
        setStatus(res && res.msg ? res.msg : "封面已更新");
        showModelDetail(path);
      };
      reader.readAsDataURL(f);
    });
    inp.click();
  });
  $("#dEditInfo").addEventListener("click", async () => {
    const mask = document.createElement("div");
    mask.className = "rd-mask";
    const dlg = document.createElement("div");
    dlg.className = "rename-dialog";
    dlg.style.width = "520px";
    const v = info.version || {};
    dlg.innerHTML =
      '<div class="rd-title">编辑模型信息</div>' +
      '<div class="form-grid" style="grid-template-columns:120px 1fr">' +
      '<label>模型名</label><input class="input" id="eiName" value="' + esc(info.modelName || "") + '"/>' +
      '<label>触发词</label><textarea class="rules" id="eiTags" rows="5" placeholder="每行一套触发词（一套内用英文逗号分隔），与 C 站展示一致">' + esc(trained.join("\n")) + "</textarea>" +
      '<label>类型</label><input class="input" id="eiType" value="' + esc(info.type || "") + '"/>' +
      '<label>基础模型</label><input class="input" id="eiBase" value="' + esc(info.baseModel || "") + '"/>' +
      '<label>版本</label><input class="input" id="eiVer" value="' + esc(v.name || "") + '"/>' +
      '<label>简介</label><textarea class="rules" id="eiDesc" rows="4">' + esc(desc || "") + "</textarea>" +
      "</div>" +
      '<div class="rd-actions"><button class="btn btn-primary" id="eiOk">保存</button><button class="btn" id="eiNo">取消</button></div>';
    document.body.appendChild(mask);
    document.body.appendChild(dlg);
    const close = () => { mask.remove(); dlg.remove(); };
    mask.addEventListener("click", close);
    $("#eiNo").addEventListener("click", close);
    $("#eiOk").addEventListener("click", async () => {
      const res = await api.call("save_model_info", path, {
        name: $("#eiName").value,
        trained_words: $("#eiTags").value.split(/\r?\n/).map((x) => x.trim()).filter((x) => x),
        type: $("#eiType").value,
        base_model: $("#eiBase").value,
        version: $("#eiVer").value,
        description: $("#eiDesc").value,
      });
      setStatus(res && res.msg ? res.msg : "已保存");
      close();
      showModelDetail(path);
    });
  });
  $("#dTranslate", panel).addEventListener("click", async () => {
    setStatus("翻译成中文中...");
    const r = await api.call("mm_translate_descs", [d.path]);
    setStatus(r && r.msg ? r.msg : "翻译完成");
    showModelDetail(d.path);  // 保持面板，刷新内容
  });
  $("#dLocalize", panel).addEventListener("click", async () => {
    setStatus("文件名翻中文中...");
    const r = await api.call("mm_localize", [d.path]);
    setStatus(r && r.msg ? r.msg : "汉化完成");
    showModelDetail(d.path);
  });
  $("#dAllImgs", panel).addEventListener("click", async () => {
    setStatus("下载所有示例图中 0%...");
    const timer = setInterval(async () => {
      try {
        const st = JSON.parse((await api.call("get_img_dl_state")) || "{}");
        if (st.total > 0) {
          const pct = Math.round((st.done / st.total) * 100);
          setStatus("下载所有示例图中 " + pct + "% (" + st.done + "/" + st.total + ")");
        }
      } catch (e) {}
    }, 400);
    const r = JSON.parse((await api.call("download_all_images", d.path)) || "{}");
    clearInterval(timer);
    setStatus(r.ok ? ("示例图：新下载 " + (r.downloaded || 0) + " 张" + (r.existing ? "，已存在 " + r.existing + " 张" : "")) : "失败: " + (r.msg || ""));
    if (r.ok) { closeDetail(); showModelDetail(d.path); }
  });
  $("#dRenameC", panel).addEventListener("click", () => renameDetailToCivitai(d));
  $("#dRename", panel).addEventListener("click", () => { closeDetail(); showRenameDialog(d.path, d.name); });
  $("#dRp", panel).disabled=!!detailRpPath;
  $("#dRp", panel).addEventListener("click", () => identifyDetailModel(d.path));
  $("#dSync", panel).addEventListener("click", async () => {
    const btn = $("#dSync", panel);
    if (btn) btn.disabled = true;
    setStatus("正在从 C 站同步 …");
    const pick = (x) => {
      const it = (x && x.info) || {};
      return {
        name: String(it.name || ""),
        author: String((it.creator && it.creator.username) || it.creator || ""),
        version: String((it.version && it.version.name) || ""),
        words: (Array.isArray(it.trainedWords) ? it.trainedWords.join("|") : ""),
        desc: String(it.description || ""),
        type: String(it.type || ""),
        base: String(it.baseModel || ""),
        imgs: (Array.isArray(it.images) ? it.images.length : 0)
      };
    };
    let before = null;
    try { before = pick(await api.call("get_model_detail", d.path)); } catch (e) { before = null; }
    const dlg = syncDialogOpen();
    syncDialogRender(dlg, { state: "run" });
    let last = null, errText = "";
    try {
      await api.call("rp_add_paths", [d.path]);
      await api.call("rp_start");
      for (let i = 0; i < 90; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        const rows = (await api.call("rp_get_rows").catch(() => null)) || [];
        last = rows.find((x) => x.path === d.path) || null;
        const st0 = last ? String(last.status || "") : "";
        if (/完成|成功|失败|错误|未匹配|未收录|已跳过|已取消/.test(st0)) break;
        if (i === 4) {
          syncDialogRender(dlg, { state: "run", note: st0 || "排队中" });
          setStatus("正在从 C 站同步 …（" + (st0 || "排队中") + "）");
        }
      }
    } catch (e) { errText = String(e || ""); }
    if (btn) btn.disabled = false;
    const st = last ? String(last.status || "") : "";
    let state, model = (last && last.model) || "", version = (last && last.version) || "", err = "";
    if (/完成|成功/.test(st)) state = "ok";
    else if (/未匹配|未收录|404/.test(st)) state = "notfound";
    else if (errText && /timeout|timed out|connect|network|SSL|ECONN|HTTP|proxy/i.test(errText)) { state = "net"; err = errText; }
    else if (/失败|错误|已取消/.test(st)) { state = "fail"; err = String((last && last.model) || ""); }
    else { state = "fail"; err = st || errText || "未收到完成状态"; }
    let changed = [];
    if (state === "ok") {
      await new Promise((r) => setTimeout(r, 700));
      let after = null;
      try { after = pick(await api.call("get_model_detail", d.path)); } catch (e) { after = null; }
      const LABELS = [["name", "模型名称"], ["author", "作者"], ["version", "版本"], ["words", "触发词"], ["desc", "简介"], ["type", "类型"], ["base", "基础模型"], ["imgs", "示例图片"]];
      if (before && after) LABELS.forEach((p2) => { if (before[p2[0]] !== after[p2[0]]) changed.push(p2[1]); });
      if (after && after.name) model = after.name;
      if (after && after.version) version = after.version;
      try { showModelDetail(d.path); } catch (e) { }
    }
    syncDialogRender(dlg, { state: state, model: model, version: version, changed: changed, err: err });
    setStatus(state === "ok"
      ? ("同步完成：" + (changed.length ? "更新 " + changed.length + " 项" : "信息已是最新"))
      : ("同步未完成：" + (st || err || "无结果")));
  });
  $("#dJson", panel).addEventListener("click", async () => {
    await api.call("mm_gen_json", [d.path], true);
    setStatus("SD json 已生成");
    closeDetail();
  });
  const dCopyBtn = $("#dCopyDesc", panel);
  if (dCopyBtn) dCopyBtn.addEventListener("click", async () => {   // 复制：纯文本（段落/列表保留，无 HTML 标签）
    const full = [descPlain, descZh ? "—— 中文翻译 ——\n" + descZh : ""].filter(Boolean).join("\n\n");
    let ok = false;
    try { ok = await window.__copyText(full); } catch (e) { ok = false; }
    if (!ok) {
      try {
        const ta = document.createElement("textarea");
        ta.value = full; ta.style.position = "fixed"; ta.style.opacity = "0";
        document.body.appendChild(ta); ta.select(); ok = document.execCommand("copy"); ta.remove();
      } catch (e2) { ok = false; }
    }
    setStatus(ok ? "简介已复制" : "复制失败（可手动选中复制）");
  });
  $("#dFavorite", panel).addEventListener("click", () => toggleModelFavorite(d.path));
  // 只准备本地已有缓存的在线图；未知在线图不会后台下载。串行、空闲调度，避免同时解码整组图片。
  if(typeof warmViewedGallery==='function'){
    const prepare=async i=>{
      if(generation!==detailGeneration || detailRow!==d || i>=covers.length)return;
      const entry=await warmViewedGallery(d,i);
      if(generation!==detailGeneration || detailRow!==d)return;
      if(entry && detailImgIdx===i){const image=panel.querySelector('#dMain');if(image?.tagName==='IMG'){image.src=entry.url;image.dataset.hdIndex=String(i);image.style.opacity='1';delete image.dataset.empty;}}
      const next=()=>prepare(i+1);if(window.requestIdleCallback)requestIdleCallback(next,{timeout:500});else setTimeout(next,40);
    };
    prepare(0);
  }
  startAutofillWatch(d, generation, panel);   // 只轮询后端已起的补帧任务，不阻塞详情渲染
}
// 详情图片右键：复制/打开文件夹/复制提示词/打开原图（document 级委托）
let detailImgCtx = null;
document.addEventListener("contextmenu", (e) => {
  const img = e.target && e.target.closest ? e.target.closest(".detail-main-img, .detail-thumb") : null;
  if (!img || !detailRow) return;
  e.preventDefault();
  const idx = img.classList.contains("detail-main-img") ? (detailImgIdx || 0) : Number(img.dataset.i);
  const c = detailRow.covers ? detailRow.covers[idx] : null;
  if (!c) return;
  detailImgCtx = { c, idx };
  const menu = $("#ctxMenu");
  menu.innerHTML =
    '<div class="ctx-item" data-act="img_copy" data-tip="把图片本身复制到剪贴板（可直接粘贴）">复制图片到剪贴板</div>' +
    '<div class="ctx-item" data-act="img_prompt" data-tip="复制该图片的正面提示词（本地 PNG 元数据 / C 站 info）">复制正面提示词</div>' +
    '<div class="ctx-item" data-act="img_neg" data-tip="复制该图片的负面提示词">复制负面提示词</div>' +
    '<div class="ctx-item" data-act="img_tags" data-tip="复制模型级触发词（与图片提示词分开）">复制触发词（模型 tags）</div>' +
    '<div class="ctx-item" data-act="img_ascover" data-tip="把这张图设为该模型在列表/瀑布流里显示的缩略图（覆盖当前封面文件）">设为模型缩略图</div>' +
    '<div class="ctx-item" data-act="img_folder" data-tip="打开资源管理器并选中该图片">打开图片所在文件夹</div>' +
    '<div class="ctx-item" data-act="img_orig" data-tip="在浏览器打开该图片在 C 站的原页">打开原图片网站</div>';
  menu.style.display = "block";
  const zf = parseFloat(getComputedStyle(document.documentElement).zoom) || 1;
  menu.style.left = (e.clientX / zf) + "px";
  menu.style.top = (e.clientY / zf) + "px";
  for (let k = 0; k < 4; k++) {
    const got = menu.getBoundingClientRect();
    const dx = e.clientX - got.left;
    const dy = e.clientY - got.top;
    if (Math.abs(dx) < 2 && Math.abs(dy) < 2) break;
    menu.style.left = (parseFloat(menu.style.left) + dx) + "px";
    menu.style.top = (parseFloat(menu.style.top) + dy) + "px";
  }
  const mr = menu.getBoundingClientRect();
  if (mr.right > window.innerWidth) menu.style.left = Math.max(0, window.innerWidth - mr.width) + "px";
  if (mr.bottom > window.innerHeight) menu.style.top = Math.max(0, window.innerHeight - mr.height) + "px";
});
$("#ctxMenu").addEventListener("click", async (e) => {
  const item = e.target.closest("[data-act^=img_]");
  if (!item || !detailImgCtx) return;
  const { c, idx } = detailImgCtx;
  const act = item.dataset.act;
  $("#ctxMenu").style.display = "none";
  try {
    // 本地图文件路径：优先图自身 local_path（右键哪张就用哪张），本地图无路径时用模型文件兜底
    const localImgPath = c.local_path || (c.local ? detailRow.path : null);
    if (act === "img_copy") {
      try {const result=await api.call('copy_gallery_image',detailRow.path,idx,detailRow.history_id||'');if(result?.ok){showToast(result.msg);return;}} catch(_){}

      // 复制图片本身到剪贴板（本地图读原图；远程图用已加载的 b64；都没有才复制路径/链接）
      let b64 = null;
      if (c.local_path) {
        try {
          const r = JSON.parse(await api.call("get_local_img_b64", c.local_path) || "{}");
          if (r && r.ok && r.b64) b64 = r.b64;
        } catch (e) { b64 = null; }
      }
      if (!b64 && c.b64) b64 = c.b64;
      if (b64) {
        try {
          const blob = await (await fetch("data:image/jpeg;base64," + b64)).blob();
          await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
          showToast("图片已复制到剪贴板，可直接粘贴");
        } catch (e2) {
          await window.__copyText(c.local_path || c.orig_url || c.url || "");
          showToast("剪贴板写入失败，已复制路径/链接");
        }
      } else {
        await window.__copyText(c.local_path || c.orig_url || c.url || "");
        showToast("已复制路径/链接");
      }
    } else if (act === "img_ascover") {
      // 设为模型缩略图：本地图直接设；只有在线地址的先自动下载到本地再设
      const srcCov = c.local_path || (c.local ? detailRow.path : null);
      const urlCov = c.orig_url || c.url || "";
      if (!srcCov && !urlCov) { showToast("这张图既没有本地文件也没有在线地址，无法设置"); }
      else {
        if (!srcCov) showToast("该图尚未下载到本地，正在在线获取后设为缩略图…");
        const rCov = await api.call("set_model_cover", detailRow.path, srcCov || "", urlCov);
        if (rCov && rCov.ok) {
          showToast("已把这张图设为模型缩略图");
          setStatus("已设为模型缩略图：" + (detailRow.name || detailRow.path || ""));
          try { state.coverCache.delete(detailRow.path); } catch (e) { }
          try { await loadThumbs(0); } catch (e) { }
          try { if (state.mmView === "masonry") await loadMasonryThumbs(0); } catch (e) { }
        } else {
          showToast((rCov && rCov.msg) || "设为缩略图失败");
        }
      }
    } else if (act === "img_folder") {
      // 只打开本地文件夹；远程图没有本地文件时提示，不打开网站（打开网站走"打开原图片网站"）
      if (localImgPath) {
        const r = await api.call("open_in_folder", localImgPath);
        if (r && r.ok) showToast("已打开文件夹：" + localImgPath);
        else showToast(r && r.msg ? r.msg + "：" + localImgPath : "打开失败");
      } else {
        showToast("该图没有本地文件（可点「下载封面图」或详情页下载所有示例图）");
      }
    } else if (act === "img_tags") {
      const tw = (detailRow && detailRow.trainedWords) ? detailRow.trainedWords : [];
      if (tw.length) {
        await window.__copyText(tw.join(", "));
        setStatus("触发词已复制（" + tw.length + " 套）");
      } else {
        setStatus("该模型没有触发词信息（可先反向解析）");
      }
    } else if (act === "img_prompt") {
      // 复制图片的正面提示词（本地识别 PNG 元数据 / C 站 info 兜底）
      const p = c.prompt || "";
      if (p) {
        await window.__copyText(p);
        showToast("正面提示词已复制");
      } else {
        showToast("该图片没有正面提示词信息");
      }
    } else if (act === "img_neg") {
      // 复制图片的负面提示词
      const n = c.negative || "";
      if (n) {
        await window.__copyText(n);
        showToast("负面提示词已复制");
      } else {
        showToast("该图片没有负面提示词信息");
      }
    } else if (act === "img_orig") {
      if (c.orig_url) await api.call("open_url", c.orig_url);
      else setStatus("无原图链接");
    }
  } catch (err) { setStatus("操作失败: " + err); }
  detailImgCtx = null;
});

// 伪 C 站详情面板：ESC 退出
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if ($("#detailMask") && $("#detailMask").style.display === "flex") closeDetail();
  }
});
function closeDetail() {
  detailGeneration++;
  _autofillStop();   // 离开详情页就停轮询：后端任务继续跑，进度不再打扰
  const dvd=$("#detailPanel video");if(dvd&&dvd.pause)dvd.pause();
  $("#detailMask").style.display = "none";
  $("#detailPanel").innerHTML = "";
  if (_detailAiOff) { _detailAiOff(); _detailAiOff = null; }
  detailRow = null;
  if (typeof syncModelInspector === "function") syncModelInspector($("#page-models").classList.contains("active"));
}
$("#detailMask").addEventListener("click", (e) => {
  if (e.target.id === "detailMask") closeDetail();
});
// 模型详情在单击选中后更新，不再另发双击请求；下载历史的双击入口保持独立。

// ===== 工作流分析（独立页面：拖入/选择 → 解析 → 节点 + 模型哈希匹配） =====
function wfSetStatus(t, k) {
  const el = $("#wfStatus");
  if (!el) return;
  el.textContent = t || "";
  el.className = "wf-status" + (k ? " " + k : "");
}
let wfLastPath = "";
async function wfAnalyze(path) {
  if (!path) return;
  wfLastPath = path;
  setStatus("工作流解析中...");
  wfSetStatus("分析中…", "on");
  const json = await api.call("analyze_workflow", path);
  let r = {};
  try { r = JSON.parse(json || "{}"); } catch (e) {}
  if (!r.ok) { setStatus("解析失败: " + (r.msg || "")); wfSetStatus("分析失败", "err"); return; }
  setStatus("工作流分析完成");
  wfSetStatus("分析完成", "done");
  wfRenderResult(r);
}
async function wfRenderResult(r) {
  if (r.source_path) wfLastPath = r.source_path;
  return window.CftWorkflow.render(r);
}
const wfDrop = $(".wf-workspace");
wfDrop.addEventListener("dragover", (e) => { e.preventDefault(); wfDrop.classList.add("over"); });
wfDrop.addEventListener("dragleave", () => wfDrop.classList.remove("over"));
wfDrop.addEventListener("drop", (e) => {
  e.preventDefault();
  wfDrop.classList.remove("over");
  const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if (!f) { setStatus("无法读取拖入文件，请点击选择"); return; }
  if (f.path) { wfAnalyze(f.path); return; }
  setStatus("读取文件中...");
  wfSetStatus("读取文件中…", "on");
  const rd = new FileReader();
  rd.onload = async () => {
    try {
      const bytes = new Uint8Array(rd.result);
      let bin = "";
      for (let i = 0; i < bytes.length; i += 0x8000) {
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      }
      const b64 = btoa(bin);
      const json = await api.call("wf_analyze_data", f.name, b64);
      let r = {};
      try { r = JSON.parse(json || "{}"); } catch (err) {}
      if (!r.ok) { setStatus("解析失败: " + (r.msg || "")); wfSetStatus("分析失败", "err"); return; }
      wfSetStatus("分析完成", "done");
      wfRenderResult(r);
    } catch (err) { setStatus("读取失败: " + err); wfSetStatus("读取失败", "err"); }
  };
  rd.onerror = () => setStatus("文件读取失败");
  rd.readAsArrayBuffer(f);
});
let wfLastPromptText = "";
$("#wfPick").addEventListener("click", async () => {
  const p = await api.call("pick_file");
  if (p) wfAnalyze(p);
});
{ const wfCopyBtn = $("#wfCopy");
  if (wfCopyBtn) wfCopyBtn.addEventListener("click", async () => {
    if (!wfLastPromptText) return;
    const ok = await window.__copyText(wfLastPromptText);
    setStatus(ok ? "提示词已复制到剪贴板" : "复制失败（可手动选中复制）");
  }); }
{ const rl = $("#wfResultTitle");
  if (rl) rl.addEventListener("click", async (e) => {
    if (e.target.closest("#wfRechoose")) {
      const p2 = await api.call("pick_file");
      if (p2) wfAnalyze(p2);
    } else if (e.target.closest("#wfReanalyze")) {
      if (wfLastPath) wfAnalyze(wfLastPath);
      else setStatus("没有可重新分析的文件");
    }
  }); }
// ===== 关于弹窗（右下角「关于」/左上角 logo） =====
async function showAbout() {
  const mask = document.createElement("div");
  mask.className = "rd-mask";
  const dlg = document.createElement("div");
  dlg.className = "rename-dialog";
  dlg.style.width = "480px";
  let ver = "v?";
  try {
    const r = JSON.parse(await api.call("get_version") || "{}");
    if (r && r.ok) ver = "v" + r.version;
  } catch (e) { /* 版本获取失败时显示占位 */ }
  dlg.innerHTML =
    '<div class="about-head">' +
    '<img class="about-logo" src="bili_face.png" alt=""/>' +
    '<div><div class="about-name">CivitaiFreeTool <span class="about-ver">' + esc(ver) + '</span></div>' +
    '<div style="font-size:calc(12px * var(--type-scale, 1));color:var(--text-dim)">Civitai / HuggingFace 模型下载、管理、反向解析工具（免费全功能）</div></div></div>' +
    '<div class="about-updates">' +
    '<div class="about-up-title">最近更新（' + esc(ver) + '）</div>' +
    '<div class="about-up-body">' +
    '<div class="about-up-item"><b>左右工作台</b>：左侧导航可收起，模型详情改为右侧抽屉，统一线性图标。</div>' +
    '<div class="about-up-item"><b>设置重构</b>：八个分类、跨分类搜索、常用与高级分开，功能说明直接展示。</div>' +
    '<div class="about-up-item"><b>分类规则</b>：使用流程与示例放在旁边，格式错误明确提示，插入示例不覆盖已有内容。</div>' +
    '<div class="about-up-item"><b>下载历史</b>：自动保存并恢复任务，收起队列不删除历史。旧版未保存的记录无法补回。</div>' +
    '</div></div>' +
    '<div class="about-thanks">' +
    '<div class="about-up-title">感谢 Contributors</div>' +
    '<div class="about-contribs" id="aboutContribs">加载中…</div></div>' +
    '<div class="about-author">' +
    '<div class="rd-home" id="rdHome">作者：爱德怀斯official —— 点击打开 B 站主页</div>' +
    '<div class="rd-group" id="rdGroup">粉丝群：909810278 —— 点击加入</div></div>' +
    '<div class="rd-actions">' +
    '<button class="btn" id="aboutGithub">GitHub 仓库</button>' +
    '<button class="btn btn-primary" id="aboutOk">知道了</button></div>';
  document.body.appendChild(mask);
  document.body.appendChild(dlg);
  mask.dataset.aiOverlay = "1";
  let aiOff = null;
  const close = () => {
    if (aiOff) aiOff();
    mask.remove(); dlg.remove();
  };
  aiOff = window.CftOverlays
    ? window.CftOverlays.register("about", close,
        { group: "dialogs", kind: "info", title: "关于", blocking: false, el: mask })
    : null;
  $("#aboutOk").addEventListener("click", close);
  mask.addEventListener("click", close);
  $("#aboutGithub").addEventListener("click", () => api.call("open_url", "https://github.com/ADVICEsama/CivitaiFreeTool"));
  $("#rdHome").addEventListener("click", () => api.call("open_url", "https://space.bilibili.com/273101122"));
  $("#rdGroup").addEventListener("click", () => api.call("open_url", "https://qm.qq.com/q/EbnuVZB4wE"));
  // 贡献者：动态拉取 GitHub contributors（排除作者本人），失败回退静态致谢
  const box = $("#aboutContribs", dlg);
  const fallback = '<div class="about-contrib">感谢 <a href="#" data-gh="guanhaisen">@guanhaisen</a>、<a href="#" data-gh="LckHot">@LckHot</a> 的社区贡献 </div>';
  fetch("https://api.github.com/repos/ADVICEsama/CivitaiFreeTool/contributors?per_page=10")
    .then((r) => (r.ok ? r.json() : Promise.reject()))
    .then((list) => {
      const others = (list || []).filter((c) => c.login !== "ADVICEsama");
      if (!others.length) { box.innerHTML = fallback; return; }
      box.innerHTML = others.map((c) =>
        '<a class="about-contrib" href="#" data-gh="' + esc(c.login) + '" title="' + c.contributions + ' 次提交">' +
        (c.avatar_url ? '<img class="about-contrib-avatar" src="' + esc(c.avatar_url) + '&s=48" alt=""/>' : "") +
        '<span>@' + esc(c.login) + '<em>' + c.contributions + " 次提交</em></span></a>").join("");
    })
    .catch(() => { box.innerHTML = fallback; });
  box.addEventListener("click", (e) => {
    const a = e.target.closest("[data-gh]");
    if (a) { e.preventDefault(); api.call("open_url", "https://github.com/" + a.dataset.gh); }
  });
}
$("#aboutFloat").addEventListener("click", showAbout);
// 左上角 logo 点击 = 关于页
const _logoEl = document.querySelector(".nav-logo");
if (_logoEl) _logoEl.addEventListener("click", showAbout);

// ===== 全局悬浮提示（带动画，鼠标跟随） =====
(function () {
  const tip = document.createElement("div");
  tip.className = "tip-float";
  tip.style.display = "none";
  document.body.appendChild(tip);
  document.addEventListener("mousemove", (e) => {
    const t = e.target && e.target.closest ? e.target.closest("[data-tip], td") : null;
    if (!t) { tip.style.display = "none"; return; }
    let text = (t.dataset && t.dataset.tip) || "";
    if (!text && t.tagName === "TD") {
      const full = t.getAttribute("data-full");
      const cur = (t.textContent || "").trim();
      const truncated = full != null || (t.scrollWidth > t.clientWidth + 4) || cur.length >= 20;
      if (!truncated) { tip.style.display = "none"; return; }
      text = (full != null ? full : cur) || "";
    }
    if (!text || !text.trim()) { tip.style.display = "none"; return; }
    tip.textContent = text.trim();
    tip.style.display = "block";
    tip.classList.add("show");
    const zf = parseFloat(document.documentElement.style.zoom) || 1;
    tip.style.maxWidth=Math.min(360,(innerWidth-16)/zf)+"px";
    const width=tip.offsetWidth*zf,height=tip.offsetHeight*zf;
    const x=e.clientX+14+width>innerWidth-8?e.clientX-width-14:e.clientX+14;
    const y=e.clientY+14+height>innerHeight-8?e.clientY-height-14:e.clientY+14;
    tip.style.left=Math.max(8,Math.min(x,innerWidth-width-8))/zf+"px";
    tip.style.top=Math.max(8,Math.min(y,innerHeight-height-8))/zf+"px";
    clearTimeout(tip._h);
    tip._h = setTimeout(() => { tip.style.display = "none"; }, 3000);
  });
})();

// ===== 模型管理右键菜单 =====
let ctxRow = null;
function placeContextMenu(menu,x,y){
  const z=Number(document.documentElement.style.zoom)||1;
  menu.style.display='block';menu.style.maxHeight=Math.max(100,(innerHeight-16)/z)+'px';
  const rect=menu.getBoundingClientRect();
  menu.style.left=Math.max(8,Math.min(x,innerWidth-rect.width-8))/z+'px';
  menu.style.top=Math.max(8,Math.min(y,innerHeight-rect.height-8))/z+'px';
}
function contextAction(act,label,icon,tip=''){
  return '<button type="button" class="ctx-item'+(act==='del'?' danger':'')+'" data-act="'+esc(act)+'" title="'+esc(tip||label)+'" data-tip="'+esc(tip||label)+'">'+_icon(icon)+'<span>'+esc(label)+'</span></button>';
}
function showModelContextMenu(e,row){
  if(!row)return;e.preventDefault();ctxRow=row;detailImgCtx=null;
  const menu=$('#ctxMenu');
  const action=(act,label,icon,tip)=>contextAction(act,label,icon,tip);
  const group=(label,icon,items)=>'<details class="ctx-group"><summary class="ctx-group-label" data-tip="展开'+esc(label)+'功能；菜单项可悬停查看用途">'+_icon(icon)+label+_icon('chevron-down','ic ctx-chevron')+'</summary><div>'+items+'</div></details>';
  menu.innerHTML='<div class="ctx-caption">'+esc(row.civitai_name||row.name||'模型操作')+'</div>'+
    action('detail','查看模型信息','info')+action('favorite',isModelFavorite(row.path)?'取消收藏置顶':'收藏并置顶','star')+action('folder','打开所在文件夹','folder')+action('move','移动到…文件夹','folder','只移动当前右键模型及附属文件、图片文件夹，不包含其它勾选模型')+action('site','打开 C站','external')+
    '<hr class="ctx-sep"/>'+group('复制','copy',action('copy_name','复制文件名','copy')+action('copy_cname','复制 C站模型名','copy'))+
    group('改名与整理','pencil',action('rename','自定义改名','pencil')+action('rename_c','文件名 → C站名称','pencil')+action('localize','文件名翻译为中文','file')+action('organize','按分类规则整理','folder','按设置里的分类规则移动此模型及附属文件；不是刷新或扫描，需先配置目标环境'))+
    group('元数据与更新','settings',action('rp','识别模型信息','search')+action('sdjson','生成 SD 元数据 JSON','file')+action('wl','不再提醒此模型更新','refresh'))+
    '<hr class="ctx-sep"/>'+action('del','移入回收站','trash','可从系统回收站恢复');
  menu.querySelectorAll('.ctx-group').forEach(group=>{
    group.querySelector('summary').addEventListener('click',()=>{if(!group.open)menu.querySelectorAll('.ctx-group').forEach(other=>{if(other!==group)other.open=false;});});
    group.addEventListener('toggle',()=>{
    if(group.open)menu.querySelectorAll('.ctx-group').forEach(other=>{if(other!==group)other.open=false;});
    const box=menu.getBoundingClientRect();placeContextMenu(menu,box.left,box.top);
  });});
  placeContextMenu(menu,e.clientX,e.clientY);
}
$("#mmTable tbody").addEventListener("contextmenu",e=>{
  const tr=e.target.closest('tr[data-path]');if(tr)showModelContextMenu(e,state.display.find(r=>r.path===tr.dataset.path));
});
document.addEventListener("click", (e) => {
  const menu = $("#ctxMenu");
  if (menu.style.display !== "none" && !e.target.closest("#ctxMenu")) {
    menu.style.display = "none";
  }
});
$("#ctxMenu").addEventListener("click", async (e) => {
  const item = e.target.closest(".ctx-item[data-act]");
  if (!item || /^(img_|dl_)/.test(item.dataset.act) || !ctxRow) return;
  const act = item.dataset.act;
  $("#ctxMenu").style.display = "none";
  const path = ctxRow.path;
  try {
    if (act === "favorite") {await toggleModelFavorite(path);return;}
    if (act === "detail") {await showModelDetail(path);return;}
    if (act === "folder") { api.call("open_in_folder", path); return; }
    if (act === "copy_name") {
      try { await window.__copyText(ctxRow.name || ""); setStatus("已复制文件名: " + ctxRow.name); }
      catch (e) { setStatus("复制失败"); }
    } else if (act === "copy_cname") {
      try { await window.__copyText(ctxRow.civitai_name || "-"); setStatus("已复制C站模型名: " + (ctxRow.civitai_name || "-")); }
      catch (e) { setStatus("复制失败"); }
    } else if (act === "site") {
      const url = ctxRow.url || ("https://" + (state.cfg.site_domain || "civitai.red") + "/models/" + (ctxRow.modelId || ""));
      api.call("open_url", url);
    } else if (act === "wl") {
      const mid = ctxRow.modelId || "";
      if (!mid) { setStatus("该模型没有 C 站信息，无法加入白名单"); return; }
      const r = await api.call("add_update_whitelist", mid, ctxRow.civitai_name || ctxRow.name || "");
      setStatus((r && r.msg) || "已加入白名单");
    } else if (act === "rename") {
      showRenameDialog(path, ctxRow.name);
    } else if (act === "rename_c") {
      await api.call("mm_rename", [path]);
      setStatus("改名完成，刷新中...");
      await api.call("scan_models");
      pollMmScan();
    } else if (act === "sdjson") {
      await api.call("mm_gen_json", [path], true);
      setStatus("SD json 已生成");
      await api.call("scan_models");
      pollMmScan();
    } else if (act === "localize") {
      await api.call("mm_localize", [path]);
      setStatus("汉化完成，刷新中...");
      await api.call("scan_models");
      pollMmScan();
    } else if (act === "rp") {
      await api.call("rp_add_paths", [path]);
      setStatus("已发送去识别模型信息");
      document.querySelector('.nav-tab[data-page="reverse"]').click();
    } else if (act === "organize") {
      await api.call("mm_organize", [path]);
      setStatus("整理完成，刷新中...");
      await api.call("scan_models");
      pollMmScan();
    } else if (act === "move") {
      await showMoveDialog(path);
    } else if (act === "del") {
      if (!(await confirmBox("确定将「" + (ctxRow.name || path) + "」移入回收站？"))) return;
      const res = await api.call("rm_file", path);
      setStatus(res && res.msg ? res.msg : "已删除");
      await api.call("scan_models");
      pollMmScan();
    }
  } catch (err) {
    setStatus("操作失败: " + err);
  }
});

// 改名弹窗
async function showRenameDialog(path, oldName) {
  try { const suggested=await api.call("get_rename_default",path);if(typeof suggested === "string" && suggested)oldName=suggested; }catch(_){}
  const mask = document.createElement("div");
  mask.className = "rd-mask";
  const dlg = document.createElement("div");
  dlg.className = "rename-dialog";
  const oldBase = String(oldName || "").replace(/\.(safetensors|ckpt|pt|pth|bin|onnx|gguf|sft)$/i, "");
  dlg.innerHTML =
    '<div class="rd-title">改名（保留扩展名）</div>' +
    '<input class="input rd-input" id="rdInput" value="' + esc(oldBase) + '" placeholder="输入新文件名"/>' +
    '<div class="rd-actions">' +
    '<button class="btn" id="rdCancel">取消</button>' +
    '<button class="btn btn-primary" id="rdOk">确定</button></div>';
  document.body.appendChild(mask);
  document.body.appendChild(dlg);
  const inp = $("#rdInput");
  inp.focus();
  inp.select();
  const close = () => { mask.remove(); dlg.remove(); };
  $("#rdCancel").addEventListener("click", close);
  mask.addEventListener("click", close);
  $("#rdOk").addEventListener("click", async () => {
    const name = inp.value.trim();
    close();
    if (!name) return;
    const res = await api.call("rename_file", path, name);
    setStatus(res && res.msg ? res.msg : "改名完成");
    await api.call("scan_models");
    pollMmScan();
  });
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") $("#rdOk").click(); });
}

async function showMoveDialog(path) {
  if(mmMoving){setStatus('已有移动任务正在进行');return;}
  mmMoving=true;
  try{
    const progress=await api.call('get_mm_progress');if(progress?.running){setStatus('已有模型操作正在进行');return;}
    const destination=await pickFolderModal({title:'移动当前模型到…文件夹',anyFolder:true});if(!destination)return;
    const name=String(path).split(/[\\/]/).pop();
    const confirmation=await confirmBoxRaw('<p>只移动当前模型：<b>'+esc(name)+'</b></p><p>目标：'+esc(destination)+'</p><p>附属文件、同名 .images 图片目录一起移动。同名冲突不覆盖，不包含其它勾选模型。</p>','移动单个模型');
    if(!confirmation)return;if(confirmation.root)confirmation.root.remove();
    const result=await api.call('move_file_to',path,destination);setStatus(result?.msg||'移动未完成');
    if(result?.ok){_mmFolderTree=null;mmScan();if(detailRow?.path===path&&result.path)showModelDetail(result.path);}
  }catch(_){setStatus('移动未完成，请检查原目录与目标目录');}
  finally{mmMoving=false;}
}

function pollMmProgress() {
  const t = setInterval(async () => {
    const p = await api.call("get_mm_progress");
    if (p && p.running) {
      setStatus((p.msg || "处理中") + " " + p.done + "/" + p.total);
      $("#mmCount").textContent = "";
    } else if (p && !p.running && p.total > 0) {
      clearInterval(t);
      setStatus(p.msg || "完成");
      if (p.result && typeof p.result === "object" && !Array.isArray(p.result) && p.result.ok !== undefined) {
        setStatus(p.msg + "（成功 " + p.result.ok + "，跳过 " + (p.result.skip || 0) + "）");
      }
      await api.call("scan_models");
      pollMmScan();
    }
  }, 700);
}

mmOp("mmVerify", (p) => api.call("mm_verify", p));
mmOp("mmJson", (p) => api.call("mm_gen_json", p, true));
mmOp("mmCovers", (p) => api.call("mm_download_covers", p));
mmOp("mmTranslate", (p) => api.call("mm_translate_descs", p));
$("#mmOrganize").addEventListener("click", async () => {
  if (!(state.cfg && state.cfg.target_env)) {
    confirmBox("请先在 设置 → 分类规则 选择 目标环境（WebUI / ComfyUI），才能整理模型");
    return;
  }
  if ((state.cfg.organize_mode || "manual") === "manual") {
    const paths = Array.from(state.mmChecked);
    const rows = state.display.filter((r) => paths.includes(r.path));
    if (!rows.length) { setStatus("请先勾选要整理的模型"); return; }
    setStatus("手动整理：共 " + rows.length + " 个，逐个选择目标文件夹 …");
    for (const r of rows) {
      const dir = await api.call("pick_dir");
      if (!dir) break;
      const res = await api.call("move_file_to", r.path, dir);
      setStatus(res && res.msg ? res.msg : "已移动 " + r.name);
    }
    mmScan();
    return;
  }
  const paths=mmCheckedPaths();if(!paths){setStatus("请先勾选当前筛选结果中的模型");return;}
  const result=await api.call("mm_organize",paths);if(!result?.started){setStatus(result?.msg||"整理未开始");return;}pollMmProgress();
});
mmOp("mmCleanup", (p) => api.call("mm_cleanup", p));
$("#mmRestore").addEventListener("click", async () => {
  if (!(state.cfg && state.cfg.target_env)) {
    confirmBox("请先在 设置 → 分类规则 选择 目标环境（WebUI / ComfyUI）");
    return;
  }
  setStatus("正在扫描可恢复的模型 …");
  const prev = await api.call("mm_restore_organize", true);
  if (!prev || !prev.ok) { setStatus(prev ? prev.msg : "扫描失败"); return; }
  if (!prev.count) { setStatus("没有发现需要恢复的模型 "); return; }
  const root = (state.cfg.models_dir || "").replace(/\\/g, "/");
  const lines = prev.items.slice(0, 30).map((it) =>
    "· " + esc(it.src.split(/[\\/]/).pop()) + " → " + esc(it.dest.replace(/\\/g, "/").replace(root, "")) + "（" + esc(it.why) + "）").join("<br/>");
  const more = prev.count > 30 ? "<br/>… 共 " + prev.count + " 个" : "";
  const ok = await confirmBox(
    "<div style='font-size:calc(12px * var(--type-scale, 1));color:var(--text-dim);line-height:1.8'>以下模型将被移回标准目录（只移动不删除，json/封面随行）：</div><div style='font-size:calc(12px * var(--type-scale, 1));line-height:1.9;max-height:240px;overflow:auto;margin-top:6px'>" + lines + more + "</div>",
    "恢复误整理");
  if (!ok) return;
  setStatus("正在恢复 …");
  const res = await api.call("mm_restore_organize", false);
  setStatus(res && res.msg ? res.msg : "恢复完成");
  mmScan();
});
function updateOrganizeBtns() {
  const env = state.cfg && state.cfg.target_env;
  const mode = (state.cfg && state.cfg.organize_mode) || "manual";
  const canAuto = env && mode !== "manual";
  const el = $("#mmRestore");
  if (el) el.style.display = canAuto ? "" : "none";
}
updateOrganizeBtns();
// 扫描走独立的 scan_state 轮询（不走 mm_progress）
$("#mmRefresh").addEventListener("click", () => {
  setStatus("刷新中 ...");
  api.call("scan_models").catch(() => {});
  pollMmScan();
});
$("#mmScan").addEventListener("click", () => mmScan());

// 改名：主按钮执行默认动作（设置可改），hover 显示二级菜单
function mmRenameRun(act) {
  const p = mmCheckedPaths();
  if (!p) { setStatus("请先勾选或选中模型"); return; }
  if (act === "custom") {
    const first = state.display.find((r) => r.path === p[0]);
    if (first) { showRenameDialog(first.path, first.name); return; }
    setStatus("未找到选中模型"); return;
  }
  mmRenamePreview(act, p);      // 批量改名：先试算预览，确认后才执行
}

// 批量改名预览（只读试算 → 用户确认 → 才真正执行）
async function mmRenamePreview(act, paths) {
  const label = act === "rename_c" ? "文件名 → C站名称" : "文件名 → 中文";
  setStatus("试算中（未修改任何文件）…");
  let r = null;
  try { r = await api.call("mm_rename_preview", act, paths); } catch (e) { setStatus("试算失败：" + e); return; }
  const items = ((r && r.items) || []).filter((x) => x && x.old);
  if (!items.length) { setStatus("没有可改名的模型（先勾选）"); return; }
  const willChange = items.filter((x) => x.new && !x.same);
  const rows = items.map((it) => {
    const tag = it.new && !it.same ? "" : ' <span class="rp-skip">' + (it.new ? "名字已相同" : "跳过") + "</span>";
    return "<tr><td class=\"rp-old\" data-tip=\"" + esc(it.old) + "\">" + esc(short(it.old, 46)) + "</td>" +
      '<td class="rp-arrow">→</td>' +
      '<td class="rp-new" data-tip="' + esc(it.new || "") + '">' + esc(short(it.new || "—", 46)) + tag + "</td></tr>";
  }).join("");
  const mask = document.createElement("div");
  mask.className = "rd-mask";
  const dlg = document.createElement("div");
  dlg.className = "rename-dialog rp-dlg";
  dlg.innerHTML =
    '<div class="rd-title">批量改名预览 · ' + esc(label) + "</div>" +
    '<div class="rp-hint">以下为<b>试算结果，尚未修改任何文件</b>；点「应用」才会执行。</div>' +
    '<div class="rp-list"><table class="rp-tb"><thead><tr><th>原文件名</th><th></th><th>新文件名</th></tr></thead><tbody>' + rows + "</tbody></table></div>" +
    '<div class="rd-actions"><span class="rp-count">共 ' + items.length + " 项，将改动 " + willChange.length + " 项</span><span style=\"flex:1\"></span>" +
    '<button class="btn" id="rpCancel">取消</button>' +
    '<button class="btn btn-primary" id="rpApply"' + (willChange.length ? "" : " disabled") + ">应用 " + willChange.length + " 项</button></div>";
  const close = () => { try { mask.remove(); dlg.remove(); } catch (e) { /* 忽略 */ } };
  document.body.appendChild(mask);
  document.body.appendChild(dlg);
  mask.addEventListener("click", close);
  dlg.querySelector("#rpCancel").addEventListener("click", close);
  dlg.querySelector("#rpApply").addEventListener("click", () => {
    close();
    setStatus(label + " 开始 …");
    api.call(act === "rename_c" ? "mm_rename" : "mm_localize", paths).then(result => {
      if(!result?.started){setStatus(result?.msg||"改名未开始");return;}
      // These APIs start a background worker; do not scan until it finishes.
      pollMmProgress();
    }).catch(() => setStatus("改名未开始，请重试"));
  });
}
$("#mmRenameMain").addEventListener("click", () => {
  const def = (state.cfg && state.cfg.rename_menu_default) || "custom";
  mmRenameRun(def);
});
// 子菜单显示：JS 延迟隐藏（200ms 缓冲，穿过空隙不消失）+ CSS hover 双保险
let renameHideTimer = null;
$("#mmRenameGroup").addEventListener("mouseenter", () => {
  clearTimeout(renameHideTimer);
  $("#mmRenameSub").style.display = "block";
});
$("#mmRenameGroup").addEventListener("mouseleave", () => {
  renameHideTimer = setTimeout(() => { $("#mmRenameSub").style.display = "none"; }, 200);
});
$("#mmRenameSub").addEventListener("mouseenter", () => {
  clearTimeout(renameHideTimer);
  $("#mmRenameSub").style.display = "block";
});
$("#mmRenameSub").addEventListener("click", (e) => {
  const item = e.target.closest(".submenu-item");
  if (!item) return;
  $("#mmRenameSub").style.display = "none";
  mmRenameRun(item.dataset.act);
});
$("#mmSendRp").addEventListener("click", async () => {
  const p = mmCheckedPaths();
  if (!p) { setStatus("请先勾选或选中模型"); return; }
  if(!await api.call("rp_add_paths", p))return;
  switchPage("reverse");await rpRefresh();
  setStatus("已发送 " + p.length + " 个到反向解析，请点击开始反查");
});

// 批量从 C 站同步：勾选模型 → 加入反向解析队列并立即开始 → 进度 → 汇总
let _batchSyncBusy = false;
async function mmSyncBatchRun() {
  if (_batchSyncBusy) { setStatus("批量同步正在进行中…"); return; }
  const paths = mmCheckedPaths();
  if (!paths) { setStatus("请先勾选要同步的模型（可先「全选」）"); return; }
  const ok = await confirmBox(
    "将从 C 站获取这 " + paths.length + " 个模型的最新信息（名称 / 简介 / 触发词 / 版本等），并更新本地记录。" +
    "\n\n每模型约 1~4 秒，数据量大时需要较长时间。开始吗？");
  if (!ok) return;
  _batchSyncBusy = true;
  try {
    await api.call("rp_add_paths", paths);
    if(!await api.call("rp_start", paths)){setStatus("反查未开始，请检查 API Key 或已有任务");return;}
    const t0 = Date.now();
    let st = null;
    while (true) {
      await new Promise((r) => setTimeout(r, 1500));
      st = await api.call("rp_state").catch(() => null);
      if (st && st.total) setStatus("批量同步中… " + (st.done || 0) + " / " + st.total);
      if (!st || st.running === false) break;
      if (Date.now() - t0 > 3 * 3600 * 1000) break;
    }
    const rows = (await api.call("rp_get_rows").catch(() => null)) || [];
    const failN = rows.filter((r) => /失败|未收录|错误/.test(String(r.status || ""))).length;
    const okN = Math.max(0, rows.length - failN);
    setStatus("批量同步完成：成功 " + okN + " 个" + (failN ? "，失败/未收录 " + failN + " 个" : "") + "（列表显示如需刷新请点「刷新」）");
    try { if (typeof window.toast === "function") window.toast("批量同步完成：成功 " + okN + " 个" + (failN ? "，失败 " + failN + " 个" : ""), 5000); } catch (e) { }
  } catch (e) {
    setStatus("批量同步失败：" + (e && e.message || e));
  } finally {
    _batchSyncBusy = false;
  }
}
document.querySelectorAll(".mm-metro [data-act='syncbatch']").forEach((it) => {
  if (it._sb) return; it._sb = 1;
  it.addEventListener("click", () => { _closeMmMenus(); mmSyncBatchRun(); });
});

// 打开入口：行双击 / 瀑布流卡片双击（单击统一为选中）

// ================= 反向解析 =================
function rpStatusCls(st) {
  const t = String(st || "");
  if (/成功|完成|匹配到|已收录/.test(t)) return "ok";
  if (/反查中|进行|匹配中|处理中/.test(t)) return "run";
  if (/失败|错误|未收录|文件不存在/.test(t)) return "err";
  return "wait";
}
function rpStatusGlyph(c) { return c === "ok" ? "\u2713 " : c === "run" ? "\u25cc " : c === "err" ? "\u00d7 " : ""; }
function rpProgRender(s2) {
  const el = $("#rpProg");
  if (!el) return;
  if (s2 && s2.running) {
    el.style.display = "";
    el.classList.toggle("paused", !!s2.paused);
    $("#rpProgTxt").textContent = s2.paused ? "已暂停" : "正在反查";
    $("#rpProgN").textContent = (s2.done || 0) + " / " + (s2.total || 0);
  } else if (s2 && s2.total > 0) {
    el.style.display = "";
    el.classList.remove("paused");
    $("#rpProgTxt").textContent = "本轮完成";
    $("#rpProgN").textContent = (s2.done || 0) + " / " + s2.total;
  } else {
    el.style.display = "none";
  }
}
const rpChecked = new Set();
let rpShown = [], rpRenderSignature = "";
function rpVisibleRows(rows) {
  const kw = $("#rpFilter").value.trim().toLowerCase(), failed = $("#rpOnlyFailed").checked;
  return rows.filter(r => (!failed || rpStatusCls(r.status) === "err") && (!kw || [r.path,r.model,r.status].some(v => String(v || "").toLowerCase().includes(kw))));
}
function rpSelectionSync() {
  const visible = new Set(rpShown.map(r => r.path));
  for(const path of rpChecked)if(!visible.has(path))rpChecked.delete(path);
  const count = rpChecked.size, all = $("#rpSelectAll");
  all.checked = rpShown.length > 0 && count === rpShown.length;all.indeterminate = count > 0 && count < rpShown.length;
  $("#rpSelectedCount").textContent = "已选 " + count + " 项";
  $("#rpRemoveSel").disabled = !count;$("#rpStartSelected").disabled = !count || !!state.rpRunning;
}
async function rpRefresh() {
  const rows = await api.call("rp_get_rows") || [];state.rpRows = rows;
  rpShown = rpVisibleRows(rows);rpSelectionSync();
  const signature = JSON.stringify([rpShown,Array.from(rpChecked)]);
  if(signature === rpRenderSignature)return;rpRenderSignature = signature;
  $("#rpTable tbody").innerHTML = rpShown.map(r => {
    const p=String(r.path||""),base=p.replace(/[\\/]+$/,"").split(/[\\/]/).pop()||p;
    const dir=base.length<p.length?p.slice(0,p.length-base.length):"",st=rpStatusCls(r.status),sha=String(r.sha||"");
    return '<tr data-path="'+esc(p)+'"'+(rpChecked.has(p)?' class="sel-row"':'')+'><td class="rp-check-col"><input type="checkbox" class="rp-row-check" aria-label="勾选 '+esc(base)+'"'+(rpChecked.has(p)?' checked':'')+'/></td>'+
      '<td class="c-file" title="'+esc(p)+'"><div class="rp-fname">'+esc(base)+'</div><div class="rp-fdir file-subpath">'+esc(dir)+'</div></td>'+
      '<td class="c-sha"><span class="rp-sha" title="'+esc(sha)+'">'+esc(sha.length>18?sha.slice(0,18)+"…":sha)+'</span></td>'+
      '<td><span class="rp-st '+st+'">'+rpStatusGlyph(st)+esc(r.status||"")+'</span></td>'+
      '<td class="c-name"><div class="rp-model" title="'+esc(r.model||"")+'">'+esc(r.model||"")+'</div></td><td>'+esc(r.version||"")+'</td></tr>';
  }).join("") || '<tr class="rp-empty"><td colspan="6">'+(rows.length?"没有符合筛选的项目":"暂无待反查任务；点击上方逐步教程")+'</td></tr>';
}
$("#rpFilter").addEventListener("input",rpRefresh);
$("#rpOnlyFailed").addEventListener("change",rpRefresh);
$("#rpFilterClear").addEventListener("click",()=>{$("#rpFilter").value="";$("#rpOnlyFailed").checked=false;rpRefresh();});
$("#rpSelectAll").addEventListener("change",()=>{for(const r of rpShown){if($("#rpSelectAll").checked)rpChecked.add(r.path);else rpChecked.delete(r.path);}rpRefresh();});
$("#rpTable tbody").addEventListener("click",e=>{
  const row=e.target.closest('tr[data-path]');if(!row)return;
  if(e.target.closest('button,a'))return;
  if(e.target.matches('.rp-row-check')){if(e.target.checked)rpChecked.add(row.dataset.path);else rpChecked.delete(row.dataset.path);}
  else{if(rpChecked.has(row.dataset.path))rpChecked.delete(row.dataset.path);else rpChecked.add(row.dataset.path);}
  rpRefresh();
});
$("#rpRemoveSel").addEventListener("click",async()=>{
  const paths=rpShown.filter(r=>rpChecked.has(r.path)).map(r=>r.path);if(!paths.length)return;
  if(!await confirmBox("移除这 "+paths.length+" 条反查列表记录？不会删除模型文件；进行中的任务会保留。"))return;
  const r=await api.call("rp_remove_selected",paths);setStatus(r?.msg||"列表移除未完成");await rpRefresh();
});
$("#rpClearAll").addEventListener("click",async()=>{
  if(!await confirmBox("确定清空反向解析列表？只清列表记录，不删除模型文件。"))return;
  if(!await api.call("rp_clear")){setStatus("反查进行中，请先停止后再清空");return;}rpChecked.clear();await rpRefresh();
});
async function rpStartScope(selected=false) {
  if(state.rpRunning){setStatus("已有反查正在运行");return;}
  const paths=rpShown.filter(r=>!selected||rpChecked.has(r.path)).map(r=>r.path);
  if(!paths.length){setStatus(selected?"请先勾选要反查的模型":"没有符合筛选的待反查模型");return;}
  const started=await api.call("rp_start",paths,$("#rpOnlyFailed").checked);
  if(!started){setStatus("反查未开始：请检查 API Key、失败筛选或已有任务");return;}
  state.rpRunning=true;$("#rpStart").disabled=true;$("#rpStartSelected").disabled=true;$("#rpPause").disabled=false;$("#rpStop").disabled=false;pollRp();
}
$("#rpStart").addEventListener("click",()=>rpStartScope(false));
$("#rpStartSelected").addEventListener("click",()=>rpStartScope(true));
$("#rpTutorial").addEventListener("click",startReverseTutorial);

$("#rpPause").addEventListener("click", async () => {
  const paused = await api.call("rp_pause");
  $("#rpPause").textContent = paused ? "继续" : "暂停";
});
$("#rpStop").addEventListener("click", async () => {
  await api.call("rp_stop");
  $("#rpPause").disabled = true;
  $("#rpStop").disabled = true;
});

function pollRp() {
  const t = setInterval(async () => {
    let s = null;
    try {
      s = await api.call("rp_state");
      await rpRefresh();
    } catch (e) { s = null; }
    state.rpRunning=!!s?.running;rpSelectionSync();rpProgRender(s);
    if (s && s.running) setStatus("反向解析 " + s.done + "/" + s.total + (s.paused ? "（已暂停）" : ""));
    else if (s && !s.running && s.total > 0) {
      clearInterval(t);
      $("#rpStart").disabled = false;
      $("#rpPause").disabled = true;
      $("#rpStop").disabled = true;
      $("#rpPause").textContent = "暂停";
      rpProgRender(s);
      setStatus("反向解析完成");
    }
  }, 800);
}

// ================= 设置 =================
// [分组, key, 标签, 类型, 选项]
const SETTING_FIELDS = [
  ["基本", "api_key", "Civitai API Key", "password"],
  ["基本", "download_dir", "下载目录", "text"],
  ["基本", "models_dirs", "模型管理目录（每行一个）", "dirs"],
  ["归档", "archive_root", "归档根目录", "text"],
  ["基本", "site_domain", "站点域名", "select", ["civitai.red", "civitai.com"]],
  ["网络", "proxy_enabled", "启用代理", "bool"],
  ["网络", "ssl_verify", "启用证书验证", "bool"],
  ["网络", "proxy_address", "代理地址", "text"],
  ["下载", "max_concurrent_downloads", "同时下载数量", "number"],
  ["网络", "download_timeout", "下载超时(秒)", "number"],
  ["网络", "download_retry", "断流自动重试次数", "number"],
  ["网络", "hash_threads", "哈希线程数", "number"],
  ["下载", "multi_file_download", "多文件版本下载方式", "select", [["ask", "弹窗多选（无人操作 10 秒选第一个）"], ["first", "直接下载第一个"], ["all", "直接下载全部模型文件"]]],
  ["下载", "gen_metadata", "完成后自动生成 json/info", "bool"],
  ["下载", "download_cover", "完成后自动下载封面", "bool"],
  ["下载", "ask_move_after_download", "完成后询问移动分类", "bool"],
  ["下载", "download_target_dir", "下载目标文件夹（预设）", "dir"],
  ["下载", "route_by_type", "按类型自动分流到 ComfyUI", "bool"],
  ["下载", "comfyui_models_root", "ComfyUI 模型根目录（models）", "dir"],
  ["下载", "rename_clean_rules", "文件名清理符号", "select", [["", "不清理"], ["comma", "去逗号（推荐）"], ["comma,paren", "去逗号 + 括号"], ["comma,paren,dash", "去逗号+括号，横线/下划线→空格"]]],
  ["下载", "metadata_format", "metadata 格式", "select", ["sd", "civitai", "both"]],
  ["翻译", "baidu_appid", "百度翻译 APP ID", "text"],
  ["翻译", "baidu_key", "百度翻译密钥", "password"],
  ["翻译", "auto_translate", "反向解析自动翻译", "bool"],
  ["下载", "download_name_mode", "下载文件命名方式", "select", [["original","原文件名"],["civitai","C站模型名"],["chinese","中文模型名（需要翻译服务）"]]],
  ["下载", "filename_include_version", "命名附加模型版本号", "bool"],
  ["界面", "theme", "界面主题", "select", [
    ["dark", "深色"],
    ["dark_purple", "暮紫（暗）"],
    ["dark_blue", "深海（暗）"],
    ["dark_green", "森林（暗）"],
    ["dark_red", "熔岩（暗）"],
    ["dark_graphite", "石墨（暗 · 中性灰蓝）"],
    ["dark_pink", "夜樱（暗 · 柔和粉色）"],
    ["dark_rose", "绯夜（暗 · 浓郁玫红）"],
    ["light", "浅色"],
    ["light_blue", "晴空（亮）"],
    ["light_pink", "樱粉（亮）"],
    ["light_green", "薄荷（亮）"],
    ["metro", "Metro 磁贴（Win10 风 · 直角扁平）"],
    ["modern", "现代浅色"],
  ]],
  ["界面", "ui_zoom", "界面缩放", "select", ["80", "90", "100", "110", "125", "150"]],
  ["界面", "ui_scheme", "Metro 亮暗", "select", [["light", "亮色"], ["dark", "暗色"], ["auto", "跟随系统"]]],
  ["界面", "metro_accent", "Metro 主题色", "select", [["#0078D4", "Windows 蓝（默认）"], ["#107C10", "翡翠绿"], ["#7A3FF2", "紫罗兰"], ["#B26A00", "琥珀橙"], ["#C42B1C", "绛红"], ["#006E8C", "青碧"], ["system", "跟随系统主题色"]]],
  ["界面", "show_file_paths", "名称下显示完整路径", "bool"],
  ["界面", "folder_picker_show_paths", "分类选择器显示详细路径", "bool"],
  ["界面", "ui_font", "默认字体（本机已安装）", "font"],
  ["界面", "ui_text_size", "全局字号（不缩放界面）", "select", [["small", "小 · 90%"], ["standard", "标准 · 100%"], ["large", "大 · 110%"], ["xlarge", "较大 · 120%"], ["huge", "最大 · 130%"]]],
  ["界面", "integrated_titlebar", "隐藏标题栏，仅保留窗口按钮", "bool"],
  ["界面", "window_appearance", "原生标题栏与材质", "select", [["theme", "跟随软件主题（推荐）"], ["mica", "Mica 标题栏 · Windows 11"], ["mica_alt", "Mica Alt 标题栏 · Windows 11"], ["system", "Windows 系统外观"], ["external", "外部工具控制（需另装 MFE）"]]],
  ["界面", "shortcuts_enabled", "启用快捷键（默认关闭）", "bool"],
  ["界面", "model_folder_include_subfolders", "勾选文件夹包含子目录", "bool"],
  ["界面", "model_list_size", "模型列表大小", "select", [["3","完整 · 最大（当前样式）"],["2","紧凑 · 无封面/作者"],["1","极简 · 名称精简（信息列保留）"]]],
  ["界面", "masonry_info_mode", "瀑布流卡片介绍", "select", [["always","常驻介绍（当前样式）"],["slide","只看封面 · 悬停滑出介绍"],["fade","只看封面 · 悬停淡入介绍"]]],
  ["界面", "masonry_overlay_size", "悬浮介绍覆盖范围", "select", [["partial","卡片下方部分"],["full","整个卡片"]]],
  ["界面", "masonry_autoplay", "瀑布流自动轮播：不悬停也换帧（每 3 秒只动一张卡片，会按需拉取 C 站缩略图；关闭后仅悬停时轮播）", "bool"],
  ["界面", "name_scroll_speed", "名称滚动速度（像素/秒）", "number"],
  ["界面", "name_scroll_start_pause", "名称滚动：开头停顿（毫秒）", "number"],
  ["界面", "name_scroll_end_pause", "名称滚动：末尾停顿（毫秒）", "number"],
  ["界面", "masonry_card_width", "瀑布流图片大小", "range", [140,420,10]],
  ["界面", "custom_accent_enabled", "自定义强调色（所有主题）", "bool"],
  ["界面", "custom_accent", "自定义强调色", "color"],
  ["界面", "ui_density", "列表信息密度", "select", [["compact", "紧凑"], ["standard", "标准"], ["comfortable", "宽松"]]],
  ["界面", "ui_corners", "控件圆角", "select", [["theme", "跟随主题"], ["square", "直角"], ["soft", "轻圆角"], ["rounded", "柔和圆角"]]],
  ["界面", "rename_menu_default", "改名默认动作", "select", [["custom", "自定义改名"], ["rename_c", "文件名改成C站名"], ["localize", "文件名翻中文"]]],
  ["界面", "confirm_buttons_flip", "确认弹窗按钮翻转", "bool"],
  ["界面", "default_page", "启动默认页", "select", [["models", "模型管理"], ["library", "资源库"], ["download", "批量下载"], ["dlmanager", "下载管理"], ["reverse", "反向解析"], ["workflow", "工作流分析"], ["settings", "设置"]]],
  ["界面", "default_view", "模型默认视图", "select", [["waterfall", "瀑布流"], ["list", "列表"]]],
  ["界面", "zebra_rows", "模型列表斑马纹", "bool"],
  ["界面", "ambient_bg", "顶部氛围动态背景", "bool"],
  ["界面", "ui_mode", "启动界面", "select", [["window", "软件窗口（默认；失败可浏览器兜底）"], ["browser", "浏览器启动（以后不尝试软件窗口）"]]],
  ["界面", "window_wait_seconds", "窗口模式等待秒数", "number"],
  ["界面", "browser_fallback_enabled", "软件窗口失败时自动浏览器兜底", "bool"],
  ["界面", "cache_detail_images", "缓存详情与瀑布流的在线缩略图", "bool"],
  ["界面", "pointer_effects", "鼠标交互特效", "select", [["off", "关闭"], ["click", "仅点击波纹"], ["trail", "拖尾 + 点击"]]],
  ["界面", "pointer_effect_quality", "交互特效性能档", "select", [["low", "轻量 · 少量粒子"], ["high", "精细 · 更多粒子"]]],
  ["界面", "effects_fps_limit", "动画帧率上限（0 = 跟随屏幕）", "number"],
  ["界面", "cache_original_images", "缓存已查看原图和生成数据", "bool"],
  ["界面", "gallery_cache_mb", "原图缓存容量（MB）", "number"],
  ["界面", "gallery_display_width", "查看器显示档最长边（像素，超出才降级；保存与复制仍是原图）", "number"],
  ["界面", "gallery_display_quality", "查看器显示档 JPEG 质量（转码会剥掉原图内嵌生成参数）", "number"],
  ["界面", "image_max_mb", "单张静帧体积上限（MB，超限整张丢弃不留半张）", "number"],
  ["界面", "video_max_mb", "单个视频体积上限（MB，超限不落盘）", "number"],
  ["界面", "fetch_deadline_s", "单帧在线读取总时限（秒，超时该帧留到下次打开）", "number"],
  ["界面", "images_dir_autofill", "打开详情后自动补齐「模型名.images」缺的示例图（会往模型同目录写文件，详情里可取消）", "bool"],
  ["界面", "close_action", "点窗口关闭按钮时", "select", [["exit", "退出软件（默认）"], ["minimize", "最小化到任务栏（不退出）"]]],
  ["下载", "update_keep_old", "更新后如何处理旧版本", "select", [["keep", "保留旧版文件（默认）"], ["delete", "删除旧版（移入回收站，可还原）"]]],
  ["界面", "webview_disable_gpu", "禁用 GPU 加速（软件渲染）", "bool"],
  ["界面", "tray_icon", "浏览器模式：托盘图标", "bool"],
  ["界面", "exit_when_page_closed", "浏览器模式：关页面后自动退出", "bool"],
];

// ===== Metro 外观：亮暗（可跟随系统）+ 主题色（可跟随系统主题色）=====
let _schemeMQ = null;
// 以 WCAG 相对亮度选择黑/白前景，亮色强调色不再出现白字看不清的问题。
function accentForeground(hex) {
  const channels = hex.slice(1).match(/../g).map((s) => parseInt(s, 16) / 255);
  const linear = channels.map((v) => v <= 0.04045 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
  const luminance = linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
  return (luminance + 0.05) / 0.05 >= 1.05 / (luminance + 0.05) ? "#000000" : "#ffffff";
}
function applyCustomUi() {
  const cfg = state.cfg || {};
  const root = document.documentElement;
  const custom = cfg.custom_accent_enabled && /^#[0-9a-f]{6}$/i.test(cfg.custom_accent || "");
  root.dataset.customAccent = custom ? "true" : "false";
  if (custom) {
    root.style.setProperty("--primary", cfg.custom_accent);
    root.style.setProperty("--primary-hover", cfg.custom_accent);
    root.style.setProperty("--primary-fg", accentForeground(cfg.custom_accent));
    root.style.setProperty("--metro-accent", cfg.custom_accent);
  } else {
    root.style.removeProperty("--primary-hover");
    root.style.removeProperty("--primary-fg");
    if (root.dataset.theme !== "metro") root.style.removeProperty("--primary");
  }
  const primary = getComputedStyle(root).getPropertyValue("--primary").trim();
  if (/^#[0-9a-f]{6}$/i.test(primary)) {
    const values = [1,3,5].map(i => parseInt(primary.slice(i,i+2),16)/255).map(v => v <= .04045 ? v/12.92 : Math.pow((v+.055)/1.055,2.4));
    const lum = .2126*values[0]+.7152*values[1]+.0722*values[2];
    root.style.setProperty("--primary-fg", lum > .179 ? "#000000" : "#ffffff");
  }
  const font = String(cfg.ui_font || "").trim();
  if (font) root.style.setProperty("--font", JSON.stringify(font) + ', "Microsoft YaHei UI", system-ui, sans-serif');
  else root.style.removeProperty("--font");
  const fontCaption = $("#fontPreviewName"); if(fontCaption)fontCaption.textContent = font || "软件默认字体";
  applyTextAndWindowAppearance();
  setMasonrySize(cfg.masonry_card_width,false);
  root.dataset.masonryInfo=["slide","fade"].includes(cfg.masonry_info_mode)?cfg.masonry_info_mode:"always";
  root.dataset.masonryOverlay=cfg.masonry_overlay_size==="full"?"full":"partial";
  window.dispatchEvent(new Event("cft:name-scroll-options"));
  root.dataset.showPaths = cfg.show_file_paths === false ? "false" : "true";
  root.dataset.density = ["compact", "comfortable"].includes(cfg.ui_density) ? cfg.ui_density : "standard";
  root.dataset.corners = ["square", "soft", "rounded"].includes(cfg.ui_corners) ? cfg.ui_corners : "theme";
  root.dataset.sidebar = cfg.sidebar_collapsed ? "collapsed" : "expanded";
  root.dataset.zebra = cfg.zebra_rows !== false ? "true" : "false";
  if (typeof applyPointerEffects === "function") applyPointerEffects(cfg);
  const sidebarButton = $("#toggleSidebar");
  if (sidebarButton) sidebarButton.setAttribute("aria-expanded", String(!cfg.sidebar_collapsed));
  const preview = $("#appearancePreview");
  if (preview) preview.textContent = "实时预览 · " + (custom ? cfg.custom_accent.toUpperCase() : "主题默认强调色");
  const bg = $("#bg");
  if (bg) bg.style.display = cfg.ambient_bg === false ? "none" : "";
}
function applyUiAppearance() {
  const scheme = String((state.cfg && state.cfg.ui_scheme) || "light");
  const acc = String((state.cfg && state.cfg.metro_accent) || "#0078D4");
  const root = document.documentElement;
  if (scheme === "auto") {
    if (!_schemeMQ) {
      try {
        _schemeMQ = window.matchMedia("(prefers-color-scheme: dark)");
        _schemeMQ.addEventListener("change", () => applyUiAppearance());
      } catch (e) { _schemeMQ = null; }
    }
    root.dataset.scheme = (_schemeMQ && _schemeMQ.matches) ? "dark" : "light";
  } else {
    root.dataset.scheme = scheme === "dark" ? "dark" : "light";
  }
  const _setAcc = (c) => {
    root.style.setProperty("--metro-accent", c);
    // --primary 只在 Metro 主题下跟随主题色；经典主题必须保持自己的主题主色（否则主按钮变黑底黑字）
    if (root.dataset.theme === "metro") root.style.setProperty("--primary", c);
    else root.style.removeProperty("--primary");
    applyCustomUi();
  };
  if (/^#([0-9a-f]{6})$/i.test(acc)) {
    _setAcc(acc);
  } else if (acc === "system") {
    _setAcc("#0078D4");  // 读取系统色期间也清除前一个主题的内联覆盖。
    api.call("get_system_accent").then((c) => {
      if (state.cfg && state.cfg.metro_accent === "system" && c && /^#([0-9a-fA-F]{6})$/.test(c)) _setAcc(c);
    }).catch(() => { });
  } else _setAcc("#0078D4");
  applyCustomUi();
  // 主题切换后：代理按钮标签需要按主题重刷（经典带 emoji / Metro 不带）
  try { (window._mmLabelSync || []).forEach((f) => f()); } catch (e) { }
  try { _applyMetroOpts(); } catch (e) { }
}

// 设置项 hover 说明（鼠标移到标签上显示功能作用）
const SETTING_TIPS = {
  "show_file_paths": "显示/隐藏下载文件、分类文件夹、反向解析和更新列表名称下的路径；不影响搜索、保存位置列、底部选中目标、详情及右键复制路径",
  "custom_accent_enabled": "启用后覆盖当前主题的强调色，按钮/选中项/进度条跟随颜色；关闭恢复当前主题默认配色",
  "custom_accent": "点击色块自由选色，实时预览。黑白文字自动适配；需先开启自定义强调色",
  "ui_density": "调整下载、模型、更新等列表以及分类窗口的行间距，紧凑模式显示更多内容",
  "ui_corners": "统一面板、弹窗、输入框和按钮的圆角；跟随主题可恢复原样式",
  "api_key": "Civitai 账号免费生成的 API Key，用于查询模型信息、下载与反向解析。在 civitai.red/user/account 登录后点「New API Key」生成",
  "download_dir": "模型下载后存放的位置，可填任意文件夹（如 D:\\models）",
  "models_dirs": "本地模型管理目录：软件从这里扫描模型并显示封面/触发词。WebUI 与 ComfyUI 分开存放时每行填一个（如 D:\\sd-webui-forge-neo\\webui\\models 和 D:\\ComfyUI\\models），扫描会合并显示",
  "site_domain": "打开 C 站页面用的域名：网络异常时可在 civitai.red 与 civitai.com 之间切换",
  "proxy_enabled": "开启后所有请求走代理（科学上网工具），解析/下载失败时可尝试开启",
  "ssl_verify": "关闭后跳过 TLS 证书验证：代理软件开了 HTTPS 解密（MITM）导致报证书错误时取消勾选即可恢复",
  "proxy_address": "代理软件地址，如 127.0.0.1:7897（Clash 默认端口）",
  "max_concurrent_downloads": "1–32 个。前面的等待任务优先；降低数量不强行中断当前下载，只限制后续任务启动。",
  "download_timeout": "单个文件下载无响应超过该秒数判定失败并重试",
  "download_retry": "连接被掐断（SSL EOF / 超时，代理节点不稳定时常见）自动重试次数：每次从已下载的断点续传，不重下。0 = 不重试",
  "rename_clean_rules": "下载命名与「改成 C 站名」一键改名时清理符号：ComfyUI 会把文件名里的逗号当成提示词分隔符，导致找不到 lora（如 py,ill,xl 这种名字）。推荐「去逗号」",
  "hash_threads": "计算文件哈希（校验/反向解析用）的线程数",
  "gen_metadata": "下载完成后自动生成 <模型名>.civitai.info / .json 元数据；没有它，模型管理里看不到名称/触发词",
  "download_cover": "下载完成后自动把 C 站预览图保存到模型目录（模型管理显示缩略图用）",
  "ask_move_after_download": "下载完成后询问是否把文件移动到指定文件夹（适合按类型归档；设了「下载目标文件夹」后本项自动不弹）",
  "window_wait_seconds": "窗口启动默认至少等待 30 秒（范围 30–600 秒），仍有启动活动时继续宽限。旧版过短设置自动按 30 秒处理；真正无法显示时使用浏览器兜底。想直接用浏览器可修改运行方式",
  "update_keep_old": "「更新页面」下载新版完成后的旧版处理：默认【保留旧版文件】（新版和旧版并存，要清理可用「查重 → 删旧留新」）；选【删除旧版】则下载完成后把旧版文件移入回收站（含预览图/元数据，可还原）。只影响「更新下载」，不影响普通批量下载",
  "download_target_dir": "预设下载落地文件夹（在模型目录里选）：下载的模型连 json/封面直接放进它，不再弹窗询问。也可在「批量下载」页临时选择",
  "metadata_format": "sd = WebUI 能直接识别的扁平 json；civitai = C 站原始 info 结构；both = 两个都生成（.civitai.info 始终生成）",
  "baidu_appid": "百度翻译开放平台 APP ID（免费申请），用于反向解析自动翻译模型名/简介",
  "baidu_key": "百度翻译开放平台密钥，与 APP ID 配套",
  "auto_translate": "反向解析时自动把模型名/简介翻译成中文",
  "translate_filename": "下载时把模型名翻译成中文作为文件名（需要配置百度翻译）",
  "theme": "界面主题：深色 / 浅色 / 现代浅色；选 Metro 磁贴 = Win10 直角扁平风（顶部按钮分组 + 默认瀑布流）",
  "ui_zoom": "界面整体缩放比例（百分比）",
  "ui_scheme": "Metro 主题的亮暗模式：亮色 / 暗色 / 跟随系统（Windows 的浅色/深色设置）",
  "metro_accent": "Metro 主题的强调色：影响按钮、选中、链接等；「跟随系统主题色」读取 Windows 个性化里的主题色",
  "rename_menu_default": "点「改名」默认执行的动作：自定义 / 改成 C 站模型名 / 文件名翻中文",
  "confirm_buttons_flip": "交换确认弹窗中「确定/取消」按钮位置（防误点）",
  "default_page": "启动软件后默认打开的页面",
  "default_view": "模型管理默认展示方式：瀑布流（大图卡片）或列表（表格）",
  "zebra_rows": "模型列表行间斑马纹，便于横向对齐查看",
  "ambient_bg": "顶部氛围动态背景（渐变光晕）",
  "ui_mode": "界面显示方式：窗口 = 原生窗口（默认）；浏览器 = 软件在后台跑、界面用系统浏览器打开 —— 界面卡死/崩溃不会拖死下载任务，刷新页面即可恢复（推荐受「启动卡死」困扰时使用）。改完重启软件生效",
  "close_action": "点窗口右上角 X 的行为：退出软件（默认）/ 最小化到任务栏（软件继续在后台跑，点任务栏图标可还原）。想彻底退出可改用托盘菜单或任务管理器",
  "webview_disable_gpu": "禁用 WebView2 的 GPU 加速（强制软件渲染）。默认关闭——软件渲染会让「顶部氛围动态背景」等动画吃满 CPU、笔记本/台式机都会明显升温。只有在遇到「WebView2 建窗卡死」且关掉氛围背景仍无法解决时才建议勾选",
  "tray_icon": "浏览器模式：在托盘显示图标（左键打开界面，右键菜单里有「退出软件」）",
  "exit_when_page_closed": "浏览器模式：关掉页面且没有任务在下载时，自动退出软件；有下载任务时会继续在后台跑",
  "target_env": "你的模型最终要放进哪个部署环境：WebUI/Forge 用 Lora、Stable-diffusion 目录；ComfyUI 用 loras、checkpoints 目录。整理前必须选择",
  "organize_mode": "整理方式：手动 = 每个模型弹窗让你选文件夹；C 站 tags = 按 C 站分类自动归档（需先反向解析生成 info）；自定义规则 = 按你填的关键词规则归档",
  "organize_rules": "每行一条：关键词1, 关键词2 -> 目标文件夹（如：NoobAI, noob -> NoobAI）",
};

// 设置分组折叠
$("#settingsForm").addEventListener("click", (e) => {
  const lg = e.target.closest("fieldset.form-section > legend");
  if (!lg) return;
  const fs = lg.parentElement;
  const body = fs.querySelector(".form-grid, .bd-links, .form-item, textarea, label, div");
  const grid = fs.querySelector(".form-grid");
  if (!grid) return;
  const collapsed = fs.dataset.collapsed === "1";
  fs.dataset.collapsed = collapsed ? "0" : "1";
  grid.style.display = collapsed ? "" : "none";
  const extra = fs.querySelector(".bd-links");
  if (extra) extra.style.display = collapsed ? "" : "none";
  const rules = fs.querySelector("textarea.rules");
  if (rules) rules.closest(".form-grid") && (rules.closest(".form-grid").style.display = collapsed ? "" : "none");
  if (!lg.dataset.arrow) lg.dataset.arrow = lg.textContent.replace(/[▾▸]\s*$/, "").trim();
  lg.innerHTML = lg.dataset.arrow + (collapsed ? " ▸" : " ▾");
  lg.style.cursor = "pointer";
});
// 百度翻译申请链接（desc 备注已说明性质，无需 [official]/【中文】前缀）
const BAIDU_LINKS = [
  { label: "百度翻译开放平台", url: "https://fanyi-api.baidu.com/product/11", desc: "官方申请入口（免费额度）" },
  { label: "申请教程 by TTime", url: "https://ttime.v1.timerecord.cn/pages/4596af/", desc: "图文教程" },
  { label: "申请教程 by BobTranslate", url: "https://bobtranslate.com/service/translate/baidu.html", desc: "图文教程" },
];

function buildSettingsForm() {
  buildWorkbenchSettings();
}


// Metro 主题的选项联动：亮暗/主题色仅 Metro 显示；斑马纹仅 Metro 下强制关闭
function _applyMetroOpts() {
  applyWorkbenchThemeOptions();
}

// 密码框小眼睛（切换明文显示）
$("#settingsForm").addEventListener("click", (e) => {
  const eye = e.target.closest(".pwd-eye");
  if (!eye) return;
  const key = eye.dataset.eye;
  const inp = document.querySelector('.pwd-wrap input[data-key="' + key + '"]');
  if (!inp) return;
  inp.type = inp.type === "password" ? "text" : "password";
  eye.textContent = inp.type === "password" ? "显示" : "隐藏";
});
// 工作流缺失模型搜索下载（Civitai + HuggingFace）
$("#wfModels").addEventListener("click", (e) => {
  const btn = e.target.closest(".wf-search");
  if (!btn) return;
  const q = encodeURIComponent(btn.dataset.search || "");
  api.call("open_url", "https://civitai.red/search/models?query=" + q);
});

// ===== 设置 =====
$("#settingsForm").addEventListener("click", (e) => {
  const link = e.target.closest(".bd-link");
  if (link) api.call("open_url", link.dataset.url);
});

async function saveSettings() {
  const cfg = {};
  Object.assign(cfg, state.cfg);
  $$("#settingsForm [data-key]").forEach((el) => {
    const k = el.dataset.key;
    if (el.type === "checkbox") cfg[k] = el.checked;
    else if (el.type === "number") cfg[k] = Number(el.value);
    else if (el.tagName === "TEXTAREA") cfg[k] = el.value.split("\n").map((s) => s.trim()).filter(Boolean);
    else cfg[k] = el.value;
  });
  updateOrganizeBtns();
  const parsedRules = parseOrganizeRules($("#organizeRules").value);
  if (parsedRules.errors.length || (cfg.organize_mode === "rules" && !parsedRules.rules.length)) {
    showSettingsCategory("organize");
    const msg = parsedRules.errors.length ? parsedRules.errors.join("；") : "自定义规则模式至少需要一条规则";
    $("#ruleValidation").textContent = msg;
    setStatus("未保存：" + msg);
    $("#organizeRules").focus();
    return;
  }
  cfg.organize_rules = parsedRules.rules;
  const folderModeChanged=(state.cfg.model_folder_include_subfolders!==false)!==(cfg.model_folder_include_subfolders!==false);
  if (!(await api.call("save_config", cfg))) { setStatus("设置保存失败，请检查目录写入权限"); return; }
  state.cfg = await api.call("get_config");
  setStatus("设置已保存");
  updateOrganizeBtns();
  // 不刷新页面：直接应用主题/缩放/表单（避免白屏闪烁）
  applyZoom(Number(state.cfg.ui_zoom) || 100);
  document.documentElement.dataset.theme = state.cfg.theme || "modern";
  applyUiAppearance();
  buildSettingsForm();
  if(folderModeChanged){foldersState=null;mmScan();}
  return true;
}
$("#btnSaveSettings").addEventListener("click",()=>saveSettings().catch(()=>setStatus("设置保存失败，请重试")));

// 设置里「下载目标文件夹」的选择/清除（点击即时保存，不依赖底部「保存设置」）
$("#settingsForm").addEventListener("click", async (e) => {
  if (e.target.closest("#resetAppearance")) {
    const defaults = { custom_accent_enabled: false, custom_accent: "#60A5FA", ui_density: "standard", ui_corners: "theme", ui_font: "", ui_text_size: "standard", window_appearance: "theme", masonry_card_width:220, model_list_size:3 };
    Object.assign(state.cfg, defaults);
    setModelListSize(3,false);
    for (const [key, value] of Object.entries(defaults)) {
      const input = $('#settingsForm [data-key="' + key + '"]');
      if (input) { if (input.type === "checkbox") input.checked = value; else input.value = value; }
    }
    applyUiAppearance();
    try { const ok = await api.call("save_config", defaults); setStatus(ok ? "已恢复当前主题默认外观" : "恢复已预览，保存失败，请重试"); }
    catch (err) { setStatus("恢复已预览，保存失败，请重试"); }
    return;
  }
  const dirtest = e.target.closest("[data-dirtest]");
  if (dirtest) {
    const key = dirtest.dataset.dirtest;
    const inp = $('#settingsForm [data-key="' + key + '"]');
    if (!inp) return;
    const paths = (inp.tagName === "TEXTAREA" ? inp.value.split("\n") : [inp.value])
      .map((s) => s.trim()).filter(Boolean);
    if (!paths.length) { showToast("请先填写目录路径"); return; }
    dirtest.disabled = true;
    try {
      const results = [];
      for (const p of paths) {
        try { results.push({ path: p, ...(await api.call("check_dir", p)) }); }
        catch (err) { results.push({ path: p, ok: false, error: String(err) }); }
      }
      const bad = results.filter((r) => !r.ok);
      if (!bad.length) {
        const remote = results.some((r) => r.is_remote);
        showToast("全部可达（" + results.length + " 个" + (remote ? "，含网络路径" : "") +
          (results.every((r) => r.writable) ? "，均可写）" : "；部分只读）"));
      } else {
        showToast("不可达 " + bad.length + "/" + results.length + "：" +
          bad.map((r) => r.path + "（" + (r.error || "未知原因") + "）").join("；"));
      }
    } finally { dirtest.disabled = false; }
    return;
  }
  const pick = e.target.closest("[data-dirpick]");
  if (pick) {
    const key = pick.dataset.dirpick;
    const p = await pickFolderModal();
    if (!p) return;
    // 只有「下载目标文件夹」需要服务端校验并立即生效；其余目录项走普通保存。
    // 写成无条件 applyDownloadTarget 的话，给分流用的「ComfyUI 模型根目录」点选择会改到下载目标上。
    if (key === "download_target_dir" && !(await applyDownloadTarget(p))) return;
    const inp = $('#settingsForm [data-key="' + key + '"]');
    if (inp) {inp.value = p;markSettingSaved(key);}
    return;
  }
  const clr = e.target.closest("[data-dirclear]");
  if (clr) {
    const key = clr.dataset.dirclear;
    if (key === "download_target_dir" && !(await applyDownloadTarget(""))) return;
    const inp = $('#settingsForm [data-key="' + key + '"]');
    if (inp) {inp.value = "";markSettingSaved(key);}
  }
});

// 设置项即时生效：主题/缩放等改完立即应用，不必等「保存设置」（也不触发整页刷新）
$("#settingsForm").addEventListener("change", (e) => {
  const el = e.target.closest("[data-key]");
  if (!el) return;
  const key = el.dataset.key;
  let val = el.type === "checkbox" ? el.checked : (el.type === "number" ? Number(el.value) : el.value);
  if(key.startsWith("shortcuts_")){applyShortcutPreference(key,val);return;}
  if(key==="model_list_size"){setModelListSize(val,true);return;}
  if(key==="name_scroll_speed"){val=Math.max(10,Math.min(240,Math.round(Number.isFinite(Number(val))?Number(val):70)));el.value=val;}
  if(["name_scroll_start_pause","name_scroll_end_pause"].includes(key)){val=Math.max(0,Math.min(5000,Math.round(Number(val)||0)));el.value=val;}
  if(key==="effects_fps_limit"){val=val===0?0:Math.max(15,Math.min(360,Math.round(Number(val)||60)));el.value=val;}
  if(key==="gallery_cache_mb"){val=Math.max(64,Math.min(65536,Math.round(Number(val)||8192)));el.value=val;}
  // 新增的图片相关数值：钳制范围与后端 config.normalize_ui_preferences 完全一致，前端不再另立一套口径
  if(key==="image_max_mb"){val=Math.max(1,Math.min(2000,Math.round(Number(val)||20)));el.value=val;}
  if(key==="video_max_mb"){val=Math.max(1,Math.min(4096,Math.round(Number(val)||100)));el.value=val;}
  if(key==="fetch_deadline_s"){val=Math.max(5,Math.min(3600,Math.round(Number(val)||120)));el.value=val;}
  if(key==="gallery_display_width"){val=Math.max(256,Math.min(4096,Math.round(Number(val)||1440)));el.value=val;}
  if(key==="gallery_display_quality"){val=Math.max(40,Math.min(100,Math.round(Number(val)||80)));el.value=val;}
  if(key==='ui_mode' || key==='browser_fallback_enabled'){
    const previous=state.cfg[key];state.cfg[key]=val;
    api.call('save_config',{[key]:val}).then(ok=>{
      if(!ok){state.cfg[key]=previous;if(el.type==='checkbox')el.checked=previous;else el.value=previous;setStatus('启动方式保存失败，请重试');return;}
      setStatus(key==='ui_mode'?(val==='browser'?'已保存：以后直接用浏览器启动，不尝试软件窗口；重启生效':'已保存：以后先启动软件窗口；失败时按兜底开关处理；重启生效'):'浏览器兜底开关已保存；不会改变正常启动方式');
    }).catch(()=>{state.cfg[key]=previous;if(el.type==='checkbox')el.checked=previous;else el.value=previous;setStatus('启动方式保存失败，请重试');});
  } else if (key === "theme") {
    state.cfg.theme = val;
    document.documentElement.dataset.theme = val || "modern";
    applyUiAppearance();   // 主题切换：重算 --primary/主题色 + 重刷代理按钮标签（经典 emoji / Metro 无）
  } else if (["show_file_paths", "custom_accent_enabled", "custom_accent", "ui_density", "ui_corners", "ambient_bg"].includes(key)) {
    if (state.cfg) state.cfg[key] = val;
    applyUiAppearance();
    api.call("save_config", { [key]: val }).then((ok) => {
      if (!ok) setStatus("外观已预览，保存失败，请重试");
    }).catch(() => setStatus("外观已预览，保存失败，请重试"));
  } else if (["effects_fps_limit", "cache_original_images", "gallery_cache_mb", "image_max_mb", "video_max_mb", "fetch_deadline_s", "gallery_display_width", "gallery_display_quality", "images_dir_autofill", "zebra_rows", "masonry_info_mode", "masonry_overlay_size", "masonry_autoplay", "name_scroll_speed", "name_scroll_start_pause", "name_scroll_end_pause", "pointer_effects", "pointer_effect_quality", "cache_detail_images", "ui_font", "ui_text_size", "window_appearance", "integrated_titlebar", "folder_picker_show_paths"].includes(key)) {
    state.cfg[key] = val;
    if(key==="cache_original_images" && val===false && typeof clearViewerImageCache==="function")clearViewerImageCache();
    if(key==="masonry_autoplay")_mmAutoSetInterval(val!==false);   // 关的时候要连已挂的自动叠帧一起收回，不只是停下一次心跳
    applyUiAppearance();
    if(["image_max_mb","video_max_mb","fetch_deadline_s","gallery_display_width","gallery_display_quality","images_dir_autofill"].includes(key))setStatus(key==="images_dir_autofill"?(val?"已保存：打开详情后会往「模型名.images」写缺的示例图":"已保存并关闭：不再往模型同目录补帧"):"已保存：下一次读取图片与补帧时生效");
    api.call("save_config", { [key]: val }).catch(() => setStatus("设置保存失败"));
  } else if (key === "masonry_card_width") {
    setMasonrySize(val,true);
  } else if (key === "ui_scheme" || key === "metro_accent") {
    if (state.cfg) state.cfg[key] = val;
    applyUiAppearance();
  } else if (key === "ui_zoom") {
    applyZoom(Number(val) || 100);
  }
});
$("#settingsForm").addEventListener("input", (e) => {
  if(e.target.dataset.key === "masonry_card_width") {setMasonrySize(e.target.value,true);return;}
  if (e.target.dataset.key === "custom_accent") {
    state.cfg.custom_accent = e.target.value;
    applyUiAppearance();
  }
});

$("#btnTestApi").addEventListener("click", async () => {
  const r = await api.call("test_api");
  infoBox("<p>" + esc(r.msg || "") + "</p>", r.ok ? "API 连接正常" : "API 连接失败");
  setStatus(r.ok ? "API 正常" : "API 失败");
});
$("#btnTestBaidu").addEventListener("click", async () => {
  const r = await api.call("test_baidu");
  infoBox("<p>" + esc(r.msg || "") + "</p>", r.ok ? "百度翻译正常" : "百度翻译失败");
});
$("#btnOnboarding").addEventListener("click", () => showOnboarding());

// 清理图片缓存（删除 *.images 文件夹）
$("#settingsForm").addEventListener("click", async (e) => {
  const btn = e.target.closest("#btnCleanImgCache");
  if (!btn) return;
  const r = JSON.parse(await api.call("cleanup_img_cache", true) || "{}");
  if (!r.ok || !r.count) {
    showToast(r.msg || "没有图片缓存文件夹（未下载过全部图片）");
    return;
  }
  const mb = (r.size / 1048576).toFixed(1);
  const list = r.dirs.slice(0, 5).map((d) => "· " + d.path).join("\n");
  const more = r.dirs.length > 5 ? "\n… 等 " + r.dirs.length + " 个" : "";
  const ok = await confirmBox(
    "将删除 <b>" + r.count + "</b> 个图片缓存文件夹（共 <b>" + mb + " MB</b>）：<br/>" +
    esc(list).replace(/\n/g, "<br/>") + esc(more).replace(/\n/g, "<br/>") +
    "<br/><br/>删除后详情面板的本地示例图会清空（封面缩略图不受影响）。确定删除？");
  if (!ok) return;
  const r2 = JSON.parse(await api.call("cleanup_img_cache", false) || "{}");
  if (r2 && r2.ok) {
    showToast("已删除 " + (r2.removed || 0) + " 个图片缓存文件夹");
    // 刷新模型管理（缩略图可能变化）
    api.call("scan_models");
  } else {
    showToast("清理失败");
  }
});

// ================= 启动 =================
async function init() {
  state.cfg = await api.call("get_config");
  refreshApiKeyGates();
  window.__ready = true;
  refreshDlTarget();
  // 应用主题（dark / light / modern）
  const theme = state.cfg.theme || "modern";
  document.documentElement.dataset.theme = theme;
  // 主题既已确定：立即重算 亮暗/主题色/设置页联动，并重刷代理按钮标签
  // （启动时 bindMmMetro 早于 init 运行，那一刻主题还是空的 → 代理标签会走经典分支；这里补一次重刷）
  try { applyUiAppearance(); } catch (e) { }
  // 界面缩放
  applyZoom(Number(state.cfg.ui_zoom) || 100);
  // 启动默认页
  const defPage = state.cfg.default_page || "models";
  const tab = document.querySelector('.nav-tab[data-page="' + defPage + '"]');
  if (tab) tab.click();
  // 应用模型管理默认视图（设置项 default_view）
  state.mmView = state.cfg.default_view === "waterfall" ? "masonry" : "list";
  try { syncViewSeg(); } catch (e) { /* 忽略 */ }
  const vtb = $("#mmViewToggle");
  if (vtb) vtb.textContent = state.mmView === "masonry" ? "列表视图" : "瀑布流";
  buildSettingsForm();
  addUrlRow();
  dlRefresh();
  setInterval(dlRefresh, 1000);
  setInterval(rpRefresh, 2000);
  setStatus("就绪");
  // 首次使用引导（未配置 API key 时自动弹出）；AI 可用 cft_dismiss_ui 关闭自动引导
  if (!state.cfg.api_key && !state.cfg.onboarding_done && state.cfg.auto_guides !== false) {
    showOnboarding();
  }
}

// ===== 首次使用引导（主题 / 下载目录 / API key / 模型目录 / 反向解析） =====
const OB_STEPS = ["申请 API Key", "页面介绍", "主题", "下载目录", "模型目录", "反向解析"];
let obStep = 0;
let obTheme = "dark";
let obDirVal = "";   // 跨步骤保存（输入框只在对应步骤渲染）
let obKeyVal = "";
let obModelDirs = []; // 模型管理目录（多目录）
let obStartPage = "models";
let obMini = false;   // 迷你模式：引导缩小悬浮右侧，不中断

// 迷你模式切换：全屏 ⇄ 右下角小窗
function setObMini(mini) {
  obMini = mini;
  const mask = $("#obMask");
  mask.classList.toggle("mini", mini);
  if (mini) {
    $("#obMiniTitle").textContent = OB_STEPS[obStep] + "（第 " + (obStep + 1) + "/" + OB_STEPS.length + " 步）";
  }
}
$("#obMiniContinue").addEventListener("click", () => setObMini(false));
$("#obMiniClose").addEventListener("click", async () => {
  setObMini(false);
  await finishOnboarding();
});
// 引导类弹窗统一收起：AI 不需要人类的新手引导/教程，后端一条命令即可关闭。
let _obAiOff = null;
function hideOnboardingOverlay() {
  const mask = $("#obMask");
  if (mask && mask.style.display !== "none") {
    mask.style.display = "none";
    setObMini(false);
  }
  if (_obAiOff) { _obAiOff(); _obAiOff = null; }
}
function dismissGuideOverlays() {
  hideOnboardingOverlay();
  if (typeof closeApiKeyPrompt === "function") closeApiKeyPrompt();
  else document.getElementById("apiKeyPrompt")?.remove();
  if (typeof closeReverseTutorial === "function") closeReverseTutorial();
}
function applyGuideFlags(flags) {
  if (!flags) return false;
  const disabled = flags.onboarding_done === true || flags.auto_guides === false;
  if (!disabled) return false;
  const mask = $("#obMask");
  if (mask && mask.style.display !== "none") dismissGuideOverlays();
  return true;
}
// 浏览器自动化/控制台的一行入口；后端 MCP 工具 cft_dismiss_ui 走同一实现。
window.cftDismissUi = async function (targets, permanent) {
  try {
    const picked = Array.isArray(targets) && targets.length ? targets : ["all"];
    const r = await api.call("dismiss_ui", picked, permanent !== false);
    if (window.CftOverlays) window.CftOverlays.dismiss(picked);
    else dismissGuideOverlays();
    return r;
  } catch (e) {
    return { ok: false, msg: String(e) };
  }
};
window.cftDismissGuides = window.cftDismissUi;   // 旧名保留，行为已扩展到所有弹层
window.addEventListener("cft:config-saved", (e) => applyGuideFlags(e.detail || {}));
function showOnboarding() {
  if(typeof settingsHaveChanges==="function"&&settingsHaveChanges()){requestSettingsLeave(showOnboarding);return;}
  obStep = 0;obStartPage=document.querySelector(".nav-tab.active")?.dataset.page||"models";
  obTheme = state.cfg.theme || "dark";
  obDirVal = state.cfg.download_dir || "";
  obKeyVal = state.cfg.api_key || "";
  const md = state.cfg.models_dirs;
  obModelDirs = (Array.isArray(md) && md.length) ? md.slice() : (state.cfg.models_dir ? [state.cfg.models_dir] : []);
  obMini = false;
  $("#obMask").classList.remove("mini");
  $("#obMask").style.display = "flex";
  if (window.CftOverlays) {
    if (_obAiOff) _obAiOff();
    _obAiOff = window.CftOverlays.register("onboarding", hideOnboardingOverlay,
      { group: "guides", kind: "guide", title: "新手引导", el: $("#obMask") });
  }
  renderOnboarding();
}
function renderOnboarding() {
  $("#obSteps").innerHTML = OB_STEPS.map((s, i) =>
    '<div class="ob-step ' + (i === obStep ? "active" : i < obStep ? "done" : "") + '">' + s + "</div>").join("");
  $("#obPrev").style.display = obStep === 0 ? "none" : "inline-block";
  $("#obNext").disabled=obStep===0&&!obKeyVal.trim();
  $("#obNext").textContent = obStep === OB_STEPS.length - 1 ? "完成 " : "下一步";
  const body = $("#obBody");
  if (obStep === 1) {
    const pages=[
      ['download','download','批量下载','粘贴模型链接，解析后选择文件和保存位置，再开始下载。'],
      ['dlmanager','list','下载管理','查看进度，暂停或重试；已结束任务可收进历史，打开实际文件位置。'],
      ['models','layers','模型管理','扫描本地目录；单击选中并更新右侧详情，星标收藏置顶。'],
      ['library','image','资源库','索引 ComfyUI 产出的图片与视频；按底模、文件夹、标签筛选，单击看提示词与参数。'],
      ['updates','refresh','检查更新','检查已有模型的新版本，选择需要更新的文件，不会自动删除旧版。'],
      ['reverse','search','反向解析','已有文件但缺少封面、名称或触发词？从这里识别 C站模型信息。'],
      ['workflow','workflow','工作流分析','打开 ComfyUI 的 JSON / PNG，查看节点、参数与缺少的模型。'],
      ['settings','settings','设置','目录、账号、字体、主题、启动方式与分类规则；关键词可搜索。']
    ];
    body.innerHTML='<p class="ob-label">先认识这些页面。点击任意页面直接体验，引导会缩到右下角，随时可以继续。</p><div class="ob-page-grid">'+pages.map(([page,icon,title,text])=>'<button class="ob-page-card" data-page="'+page+'">'+_icon(icon)+'<span><b>'+title+'</b><small>'+text+'</small></span></button>').join('')+'</div>';
    body.querySelectorAll('[data-page]').forEach(button=>button.onclick=()=>{obStartPage=button.dataset.page;switchPage(obStartPage);setObMini(true);});
  } else if (obStep === 2) {
    const options=SETTING_FIELDS.find(field=>field[1]==='theme')[4];
    body.innerHTML='<p class="ob-label">选择喜欢的主题，设置里还能调整字体、字号和强调色。</p><div class="ob-themes">'+options.map(([value,label])=>'<button class="btn ob-theme" data-t="'+esc(value)+'">'+esc(label)+'</button>').join('')+'</div>';
    body.querySelectorAll('[data-t]').forEach(button=>{button.classList.toggle('sel',button.dataset.t===obTheme);button.onclick=()=>{obTheme=button.dataset.t;state.cfg.theme=obTheme;document.documentElement.dataset.theme=obTheme;applyUiAppearance();body.querySelectorAll('[data-t]').forEach(b=>b.classList.toggle('sel',b===button));};});
  } else if (obStep === 3) {
    body.innerHTML =
      '<div class="ob-label">下载目录（模型下载后存放位置，可修改）</div>' +
      '<div style="display:flex;gap:8px"><input class="input" id="obDir" style="flex:1" value="' + esc(obDirVal) + '"/>' +
      '<button class="btn" id="obBrowse">浏览</button></div>' +
      '<div style="color:var(--text-dim);font-size:calc(12px * var(--type-scale, 1));margin-top:6px">默认：软件根目录下的 downloads/models 文件夹</div>';
    $("#obDir").addEventListener("input", () => { obDirVal = $("#obDir").value; });
    $("#obBrowse").addEventListener("click", async () => {
      const picked = await api.call("pick_dir");
      if (picked) { obDirVal = picked; $("#obDir").value = picked; }
    });
    $("#obBrowse2") && null;
  } else if (obStep === 0) {
    body.innerHTML =
      '<div class="ob-label">第一步：先申请 Civitai API Key（免费申请，用于查询模型信息与下载）</div>' +
      '<input class="input" id="obKey" type="password" value="' + esc(obKeyVal) + '" placeholder="粘贴你的 API Key"/>' +
      '<div class="ob-guide" id="obGuide">' +
      '<div style="font-weight:600;margin-bottom:6px">如何注册 API Key：</div>' +
      '<div>1. 打开 <a class="ob-link" id="obApiPage">civitai.red/user/account</a>（登录后点 Account Settings 生成 API Keys）</div>' +
      '<div>2. 登录账号后点击「New API Key」生成</div>' +
      '<div>3. 复制生成的 Key 粘贴到上方输入框即可</div></div>' +
      '<div class="ob-actions2"><button class="btn" id="obToggleGuide">如何注册 API？</button>' +
      '<button class="btn" id="obOpenApi">打开注册页</button></div>' +
      '<div style="color:var(--text-dim);font-size:calc(12px * var(--type-scale, 1));margin-top:6px">请先完成申请并填写 API Key，再进入页面介绍；Key 仅保存到你的本地配置。</div>';
    $("#obKey").addEventListener("input", () => { obKeyVal = $("#obKey").value;$("#obNext").disabled=!obKeyVal.trim();$("#obSkipAll").disabled=!obKeyVal.trim(); });
    $("#obToggleGuide").addEventListener("click", () => {
      const g = $("#obGuide");
      g.style.display = g.style.display === "none" ? "block" : "none";
    });
    $("#obOpenApi").addEventListener("click", () => api.call("open_url", "https://civitai.red/user/account"));
    $("#obApiPage").addEventListener("click", () => api.call("open_url", "https://civitai.red/user/account"));
  } else if (obStep === 4) {
    // 模型管理目录：手把手选择（可多目录）
    body.innerHTML =
      '<div class="ob-label">模型管理目录 —— 你本地存放模型的地方</div>' +
      '<div class="ob-hint">软件从这里扫描模型、显示封面和触发词。WebUI 与 ComfyUI 分开存放的，把两个目录都填上（每行一个）：</div>' +
      '<textarea class="input" id="obModelDirs" rows="3" style="width:100%;box-sizing:border-box">' + esc(obModelDirs.join("\n")) + '</textarea>' +
      '<div class="ob-actions2"><button class="btn" id="obBrowseModels">选择文件夹</button>' +
      '<button class="btn" id="obBrowseModels2">再添加一个</button></div>' +
      '<div style="color:var(--text-dim);font-size:calc(12px * var(--type-scale, 1));margin-top:6px">常见路径：D:\\sd-webui-forge-neo\\webui\\models（WebUI）、D:\\ComfyUI\\models（ComfyUI）</div>';
    const sync = () => { obModelDirs = $("#obModelDirs").value.split("\n").map((s) => s.trim()).filter(Boolean); };
    $("#obModelDirs").addEventListener("input", sync);
    const addDir = async () => {
      const picked = await api.call("pick_dir");
      if (!picked) return;
      sync();
      if (!obModelDirs.includes(picked)) obModelDirs.push(picked);
      $("#obModelDirs").value = obModelDirs.join("\n");
    };
    $("#obBrowseModels").addEventListener("click", async () => {
      const picked = await api.call("pick_dir");
      if (!picked) return;
      obModelDirs = [picked];
      $("#obModelDirs").value = picked;
    });
    $("#obBrowseModels2").addEventListener("click", addDir);
  } else {
    // 反向解析：推荐但可跳过
    body.innerHTML =
      '<div class="ob-label">反向解析（强烈推荐，也可跳过）</div>' +
      '<div class="ob-hint">把你已下载的模型文件识别出 C 站信息：自动匹配模型名、触发词（tags）、类型和基础模型，并生成封面。</div>' +
      '<div class="ob-hint" style="margin-top:4px">做完后，模型管理里每个模型才有名字和触发词可复制；<b>跳过也不影响其他功能</b>。</div>' +
      '<div class="ob-actions2"><button class="btn btn-primary" id="obGoRp">立即体验（推荐）</button>' +
      '<button class="btn" id="obSkipRp">跳过（以后在 反向解析 页随时可用）</button></div>';
    $("#obGoRp").addEventListener("click", async () => {
      if(await finishOnboarding()!==true)return;
      startReverseTutorial();
    });
    $("#obSkipRp").addEventListener("click", async () => {
      await finishOnboarding();
    });
  }
  body.scrollTop=0;
  $('#obSkipAll')?.remove();$('#obNext').insertAdjacentHTML('beforebegin','<button class="btn" id="obSkipAll">跳过引导，直接使用</button>');
  $('#obSkipAll').disabled=!obKeyVal.trim();$('#obSkipAll').title=obKeyVal.trim()?'跳过后续介绍':'请先申请并填写 API Key';
  $('#obSkipAll').onclick=finishOnboarding;
}

// 引导完成：保存全部配置
async function finishOnboarding() {
  if(!obKeyVal.trim()){obStep=0;renderOnboarding();showToast("请先申请并填写 Civitai API Key");return false;}
  state.cfg.onboarding_done = true;
  state.cfg.api_key = (obKeyVal || "").trim();
  state.cfg.download_dir = (obDirVal || state.cfg.download_dir || "").trim();
  state.cfg.theme = obTheme;
  const dirs = obModelDirs.filter(Boolean);
  state.cfg.models_dirs = dirs;
  if (dirs.length) state.cfg.models_dir = dirs[0];
  document.documentElement.dataset.theme = obTheme;
        applyUiAppearance();
  if(!await api.call("save_config", state.cfg)){showToast("设置保存失败，请重试");return;}
  state.cfg = await api.call("get_config");
  buildSettingsForm();
  applyZoom(Number(state.cfg.ui_zoom) || 100);
  document.documentElement.dataset.theme = state.cfg.theme || "modern";
  hideOnboardingOverlay();
  switchPage(obStartPage);
  setStatus('引导已完成。可在设置里随时重新打开。');
  return true;
}
$("#obNext").addEventListener("click",async()=>{
  if(obStep===0){
    if(!obKeyVal.trim()){showToast('请先申请并填写 API Key');return;}
    try{if(!await api.call('save_config',{api_key:obKeyVal.trim(),site_domain:state.cfg.site_domain||'civitai.red'})){showToast('API Key 保存失败，请重试');return;}state.cfg=await api.call('get_config');buildSettingsForm();}
    catch(_){showToast('API Key 保存失败，请重试');return;}
  }
  if(obStep===OB_STEPS.length-1){await finishOnboarding();return;}
  obStep++;renderOnboarding();
});
$("#obPrev").addEventListener("click", () => { obStep--; renderOnboarding(); });

window.addEventListener("pywebviewready", init);
if (window.pywebview) init();
// 就绪守卫：init 未完成前禁止依赖 js_api 的操作（首次桥接可能慢）
window.__ready = false;

// 批量下载页 hint 仓库域名外链：仅处理 .ob-link（定向委托，避免误伤其他 data-url 元素）
document.addEventListener("click", (e) => {
  const el = e.target.closest("#page-download .card .ob-link[data-url]");
  if (!el) return;
  e.preventDefault();
  api.call("open_url", el.dataset.url);
});

/* ============================================================================
   Metro 工具区（仅 Metro 主题可见）——
   按钮全部是"代理"：点击转交给下面老按钮的 click()，业务逻辑零改动；
   标签/禁用/显隐用 MutationObserver 跟随原按钮（如「停止检查（N/M）」、恢复误整理的显隐）。
   ========================================================================== */

// SVG 图标（内联 sprite，见 index.html 的 #i-* symbols）
function _icon(name, cls) {
  return '<svg class="ic' + (cls ? " " + cls : "") + '" viewBox="0 0 24 24" fill="none" stroke="currentColor"' +
    ' stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<use href="#i-' + name + '"/></svg>';
}

function _msTag(r) {
  // 卡片上不再显示「已最新」等文字标签（无更新默认不显示任何标记）；
  // 有更新的模型用右上角 ❗ 角标（.ms-upd，点击去 C 站）
  return "";
}

function syncViewSeg() {
  const l = document.getElementById("mmViewList"), m = document.getElementById("mmViewMasonry");
  if (l) l.classList.toggle("on", state.mmView === "list");
  if (m) m.classList.toggle("on", state.mmView === "masonry");
}

function mmSetView(v) {
  state.mmView = (v === "masonry") ? "masonry" : "list";
  const t = document.getElementById("mmViewToggle");
  if (t) t.textContent = state.mmView === "masonry" ? "\ud83d\udccb \u5217\u8868\u89c6\u56fe" : "\ud83d\uddbc\ufe0f \u7011\u5e03\u6d41";
  syncViewSeg();
  renderMm();
}

function syncSortDir() {
  const sd = document.getElementById("mmSortDir");
  if (sd) {
    sd.innerHTML = _icon("sort") + (state.mmSort.rev ? "降序" : "升序");
    sd.dataset.tip = state.mmSort.rev ? "当前降序，点击切换为升序" : "当前升序，点击切换为降序";
  }
}

let _mmFolderTree = null;
async function fillMmFolderOptions() {
  // 复用现有 get_folders（与「文件夹显隐」面板同源），只做"拉平 + 填充下拉"
  const sel = document.getElementById("mmFolderF");
  if (!sel) return;
  if (!_mmFolderTree) {
    try {
      const j = JSON.parse((await api.call("get_folders")) || "{}");
      _mmFolderTree = Array.isArray(j) ? j : ((j && j.tree) || []);   // ★ get_folders 返回 {root, tree, hidden, show_root}
    } catch (e) { _mmFolderTree = []; }
  }
  const flat = [];
  const walk = (nodes, depth) => (nodes || []).forEach((n) => {
    if (n && n.path) flat.push({ path: n.path, name: (depth ? "　".repeat(depth) : "") + (n.name || n.path) });
    if (n && n.children) walk(n.children, depth + 1);
  });
  walk(_mmFolderTree, 0);
  const sig = flat.map((f) => f.path).join("|");
  if (sel._sig === sig) return;
  sel._sig = sig;
  const cur = sel.value;
  sel.innerHTML = '<option value="">全部文件夹</option>' + flat.map((f) =>
    '<option value="' + esc(f.path) + '">' + esc(f.name) + "</option>").join("");
  sel.value = cur;
}
function fillMmAuthorOptions() {
  const af = document.getElementById("mmAuthorF");
  if (!af) return;
  const authors = [...new Set(state.models.map((r) => String(r.author || "").trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b, "zh"));
  const sig = authors.join("|");
  if (af._sig === sig) return;
  af._sig = sig;
  const cur = af.value;
  af.innerHTML = '<option value="">全部作者</option>' + authors.map((a) =>
    '<option value="' + esc(a) + '">' + esc(a) + "</option>").join("");
  af.value = cur;
  if (af.value !== cur) { state.mmAuthorF = ""; }
}
function fillMmBaseOptions() {
  const bf = document.getElementById("mmBaseF");
  if (!bf) return;
  const bases = [...new Set(state.models.map((r) => String(r.base || "").trim()).filter(Boolean))].sort();
  const sig = bases.join("|");
  if (bf._sig === sig) return;
  bf._sig = sig;
  const cur = bf.value;
  bf.innerHTML = '<option value="">全部底模</option>' +
    bases.map((b) => '<option value="' + esc(b) + '">' + esc(b) + "</option>").join("");
  bf.value = cur;
}

function _closeMmMenus() {
  document.querySelectorAll(".btn-group.open").forEach((g) => {
    g.classList.remove("open");
    const m = g._menu;
    if (m) m.classList.remove("open");
  });
}

// 打开菜单：把菜单移到 body（portal），用 fixed 定位 + 完全不透明背景 —— 任何祖先的
// overflow / transform / stacking context 都影响不到它
function _mmOpenMenu(grp, menu, btn) {
  if (menu.parentElement !== document.body) document.body.appendChild(menu);
  menu.style.position = "fixed";
  menu.style.background = "var(--surface, #fff)";   // 暗色模式下跟随主题面
  menu.style.zIndex = "var(--z-dropdown, 340)";
  const r = btn.getBoundingClientRect();
  const w = menu.offsetWidth || 220;
  menu.style.left = Math.max(8, Math.min(r.left, (window.innerWidth || 1920) - w - 8)) + "px";
  menu.style.top = (r.bottom + 2) + "px";
  menu.classList.add("open");
  grp.classList.add("open");
  grp._menu = menu;
  menu._owner = grp;
}

function bindMmMetro() {
  const _sec = (name, fn) => { try { fn(); } catch (e) { console.error("[bindMmMetro]", name, e); try { api.call("log_ui_error", "bindMmMetro/" + name + ": " + (e && e.message || e)).catch(() => {}); } catch (e2) { } } };
  // 1) 代理按钮：点击 → 原按钮 click()
  document.querySelectorAll(".mm-metro [data-proxy]").forEach((el) => {
    if (el._px) return; el._px = 1;
    el.addEventListener("click", (e) => {
      if (el.dataset.proxy === "mmFolders") e.stopPropagation();
      _closeMmMenus();
      const src = document.getElementById(el.dataset.proxy);
      if (src && !src.disabled) src.click();
    });
  });
  // 2) 菜单开合（点击式；再点收起；外点/Esc 关闭）
  _sec("menus", () => {
  document.querySelectorAll(".mm-metro [data-menu]").forEach((btn) => {
    if (btn._mn) return; btn._mn = 1;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const grp = btn.closest(".btn-group");
      if (!grp) return;
      // 菜单首次打开后会 portal 到 body，此时 grp.querySelector 找不到它 → 用记住的引用兜底（否则第二次点不开）
      const menu = grp.querySelector(".mm-menu") || grp._menu || null;
      const wasOpen = grp.classList.contains("open");
      _closeMmMenus();
      if (!wasOpen && menu) _mmOpenMenu(grp, menu, btn);   // 箭头翻转由 CSS 负责（.btn-group.open .car svg）
    });
  });
  });
  // 3) 改名菜单（复用 mmRenameRun，功能与老菜单完全一致）
  _sec("rename", () => {
  document.querySelectorAll(".mm-metro [data-ract]").forEach((it) => {
    if (it._ra) return; it._ra = 1;
    it.addEventListener("click", () => { _closeMmMenus(); mmRenameRun(it.dataset.ract); });
  });
  });
  // 4) 跳设置 / 打开日志
  _sec("goto", () => {
  document.querySelectorAll(".mm-metro [data-goto='settings']").forEach((it) => {
    if (it._gt) return; it._gt = 1;
    it.addEventListener("click", () => { _closeMmMenus(); switchPage("settings"); });
  });
  document.querySelectorAll(".mm-metro [data-act='logs']").forEach((it) => {
    if (it._lg) return; it._lg = 1;
    it.addEventListener("click", async () => {
      _closeMmMenus();
      const r = await api.call("open_logs_dir");
      setStatus((r && r.msg) || "已打开日志文件夹");
    });
  });
  });
  _sec("outside", () => {
  document.addEventListener("click", (e) => {
    if (e.target && e.target.closest && e.target.closest(".mm-menu")) return;   // 点在菜单里不关
    _closeMmMenus();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") _closeMmMenus(); });
  // 窗口尺寸变化时，已打开的菜单重新定位（避免飘走）
  window.addEventListener("resize", () => {
    document.querySelectorAll(".btn-group.open").forEach((g) => {
      const b = g.querySelector(".btn[data-menu]");
      if (b && g._menu) _mmOpenMenu(g, g._menu, b);
    });
  });
  });
  // 5) Metro 筛选（底模 / 状态 / 排序），复用既有 state 与排序机制
  _sec("filters", () => {
  const bf = document.getElementById("mmBaseF"), sf = document.getElementById("mmStF"),
        so = document.getElementById("mmSortF"), sd = document.getElementById("mmSortDir");
  const tf = document.getElementById("mmTypeF");
  if (tf) tf.addEventListener("change", () => { state.mmTypeF = tf.value; applyMmFilter(); });
  const ff = document.getElementById("mmFolderF");
  if (ff) ff.addEventListener("change", () => { state.mmFolderF = ff.value; applyMmFilter(); });
  if (bf) bf.addEventListener("change", () => { state.mmBaseF = bf.value; applyMmFilter(); });
  const af2 = document.getElementById("mmAuthorF");
  if (af2) af2.addEventListener("change", () => { state.mmAuthorF = af2.value; applyMmFilter(); });
  const cf = document.getElementById("mmCoverF");
  if (cf) cf.addEventListener("change", () => { state.mmCoverF = cf.value; applyMmFilter(); });
  if (sf) sf.addEventListener("change", () => { state.mmStF = sf.value; applyMmFilter(); });
  if (so) so.addEventListener("change", () => {
    if (!so.value) state.mmSort = { col: null, rev: false };
    else state.mmSort = { col: so.value, rev: state.mmSort.col === so.value ? state.mmSort.rev : false };
    syncSortDir(); applyMmFilter();
  });
  if (sd) sd.addEventListener("click", () => {
    state.mmSort = { col: state.mmSort.col || "name", rev: !state.mmSort.rev };
    if (so) so.value = state.mmSort.col;
    syncSortDir(); applyMmFilter();
  });
  syncSortDir();
  // 5b) 分段视图切换
  const _vl = document.getElementById("mmViewList"), _vm = document.getElementById("mmViewMasonry");
  if (_vl) _vl.addEventListener("click", () => mmSetView("list"));
  if (_vm) _vm.addEventListener("click", () => mmSetView("masonry"));
  syncViewSeg();
  });
  _sec("appearance", () => { applyUiAppearance(); });
  // 6) 代理跟随原按钮（文字/禁用/显隐）
  document.querySelectorAll(".mm-metro [data-proxy]").forEach((el) => {
    const src = document.getElementById(el.dataset.proxy);
    if (!src) return;
    const _clean = (t) => String(t || "").replace(/^[\u{1F300}-\u{1FAFF}\u{2300}-\u{27BF}\u{2B00}-\u{2BFF}\uFE0F\u200D\s]+/u, "");
    const _isMetro = () => document.documentElement.dataset.theme === "metro";
    const sync = () => {
      const raw = String(src.textContent || "");
      const lbl = _clean(raw);       // 经典主题：保留 emoji（原样）
      const ic = el.dataset.icon ? _icon(el.dataset.icon) : "";
      if (el.innerHTML !== (ic + lbl)) el.innerHTML = ic + lbl;
      el.disabled = !!src.disabled;
      el.style.display = (src.style.display === "none") ? "none" : "";
    };
    (window._mmLabelSync = window._mmLabelSync || []).push(sync);
    try {
      new MutationObserver(sync).observe(src, { childList: true, characterData: true, subtree: true, attributes: true, attributeFilter: ["style", "disabled"] });
    } catch (e) { /* 忽略 */ }
    sync();
  });
}
try { bindMmMetro(); } catch (e) { /* Metro 未启用时也不报错 */ }

/* ===== 更新页「版本选择」控件的事件（委托；与门户菜单同一套逻辑） ===== */
document.addEventListener("click", (e) => {
  const t = e.target;
  if (!t || !t.closest) return;
  // 打开/关闭
  const btn = t.closest(".vsel-btn");
  if (btn) {
    e.stopPropagation();
    const wrap = btn.closest(".btn-group");
    const menu = wrap && wrap.querySelector(".mm-menu");
    const wasOpen = wrap && wrap.classList.contains("open");
    _closeMmMenus();
    if (!wasOpen && menu) _mmOpenMenu(wrap, menu, btn);
    return;
  }
  // 选择版本
  const item = t.closest(".vitem[data-vid]");
  if (item) {
    const pth = item.dataset.vpath, vid = item.dataset.vid;
    state.updPick[pth] = vid;
    _closeMmMenus();
    renderUpdatesPage();
    return;
  }
  // 去 C 站
  const site = t.closest(".vopen[data-vsite]");
  if (site) {
    _closeMmMenus();
    api.call("open_url", site.dataset.vsite);
  }
}, true);

// 门户菜单挂在 body 后仍可点（portal 出去的菜单也走上面的委托）

// Escape 先关闭右键菜单，不同时关闭背后的模型详情。
document.addEventListener('keydown',e=>{const menu=$('#ctxMenu');if(e.key==='Escape' && menu.style.display==='block'){menu.style.display='none';e.preventDefault();e.stopImmediatePropagation();}},true);
window.addEventListener('resize',()=>{const menu=$('#ctxMenu');if(menu)menu.style.display='none';});

// ===== 资源库 =====
// ComfyUI 产物（图片/视频）索引页。数据一律走 lib_* RPC，与模型库的
// state.models / state.display / state.coverCache 完全隔离，互不读写。
const lib = {
  rows: [], total: 0, offset: 0, limit: 200,
  facets: null, tags: [], tagsLoaded: false,
  // samplers / schedulers / sizes / duration_min / duration_max 的键名与服务端 §15.2.4 严格一致，
  // durations 用 null 表达「不限」（libBuildFlt 只在有值时才带上，别把 null 当 0 发出去）。
  filters: { kind: "", checkpoints: [], loras: [], dirs: [], tag_ids: [], starred: false, no_meta: false, q: "", sort: "time_desc", group_by: "", samplers: [], schedulers: [], sizes: [], duration_min: null, duration_max: null },
  loading: false, thumbCache: new Map(), sel: new Set(), detail: null,
  // ---- 多选与批量操作（sel 里的键一律存 String(id)，跟 data-id 同一口径）----
  // selectMode：#libSelectMode 开关；current：非多选态下「详情对应的那张卡」；
  // selAnchor：shift 范围选的锚点（lib.rows 下标，换筛选/重置即作废）；
  // byId：id → {path, kind, hasMeta}，列表加载与「选中筛选结果全部」逐批登记时顺手记下来，
  //        批量送逆向／送工作流要靠它拿路径（跨页选中的条目本地并没有行数据）。
  // selTagIds/selTagMode：#libSelTagPop 里勾选的标签与本次是加还是摘；
  // selFetching/selFetchToken：#libSelAllFilter 的逐批取 id 作业；batchRunning：一次只允许一个批量动作。
  selectMode: false, current: null, selAnchor: null, byId: new Map(),
  selTagIds: new Set(), selTagMode: "", selFetching: false, selFetchToken: 0, batchRunning: false,
  prog: "",                                // 批量/取 id 进行中的播报文本（操作条与选择行共用，空串＝空闲）
  // 内部状态：请求令牌 + 定时器（切页时全部清理，避免后台继续请求、继续解码视频）
  request: 0, detailRequest: 0, scanTimer: null, scrollTimer: null, qTimer: null, durTimer: null,
  thumbBusy: false, scanRunning: false, noRoots: false, rootsState: [],
  posterTried: new Set(), posterTimers: [], vObs: null,      // vObs：全页共用的一个视频挂载观察器
  popKind: "", popAnchor: null, foldersOpen: false,
  // 详情长文本（提示词/负面提示词）的展开态：按 String(id) 存，换条目天然回折叠态。
  // descOpenId 记下这份状态属于哪一条：对不上就整份清空（既是重置也防止越翻越多）。
  descOpen: {}, descOpenId: "",
  _dirsFromPanel: [], _durError: "",
};

function libEl(id) { return document.getElementById(id); }
function libPageActive() { const p = libEl("page-library"); return !!p && p.classList.contains("active"); }
function libRowById(id) { const k = String(id); return lib.rows.find((r) => String(r.id) === k) || null; }
// 每批条数取设置（§7：默认 200，config 归一化区间 20..500），启动早期 state.cfg 还没到位时回落 200
function libLimit() { const n = Number((state.cfg || {}).media_page_size || 200) || 200; return Math.max(20, Math.min(500, n)); }
// pywebview 用 file:/// 直读；浏览器模式下页面本身是 http origin，file:/// 不可用，走 /api/media 流式端点
function libMediaUrl(it) { if (!it) return ""; return window.__browserMode ? String(it.url_http || "") : String(it.url_local || it.url_http || ""); }
function libVal(id) { const el = libEl(id); return el ? String(el.value || "") : ""; }
function libFlag(id) { const el = libEl(id); return !!(el && el.checked); }
// 后端缩略图/预览可能回 dataURL（A3 现状）或裸 base64：统一成可直接赋给 src 的形式
function libThumbUri(v) {
  const s = String(v || "");
  if (!s) return "";
  return /^(data:|blob:|https?:|file:)/i.test(s) ? s : "data:image/jpeg;base64," + s;
}
function libDay(ts) { const d = new Date(Number(ts || 0) * 1000); const p = (n) => String(n).padStart(2, "0"); return p(d.getMonth() + 1) + "-" + p(d.getDate()); }
function libDur(sec) {
  const s = Number(sec || 0); if (!s) return "";
  return s < 60 ? s.toFixed(1) + " 秒" : Math.floor(s / 60) + " 分" + String(Math.round(s % 60)).padStart(2, "0") + " 秒";
}
// 帧率：后端读得到才显示（§15.1.5 明确「拿不到就是 None，不得默认 30」），这里只做两位小数格式化
function libFps(v) {
  if (v == null || v === "") return "";
  const n = Number(v);
  return isFinite(n) && n > 0 ? n.toFixed(2) + " fps" : "";
}
// 参数来源标注：只有 extra.params_source 确实是 'field_name' 才标（§15.2 的 provenance），
// 文案与 class 沿用提示词那一款（.dt-sec-hint「按字段名提取」），不另起一套样式；
// params_fields 若有就放进 title，如实说明命中了哪些字段名。
function libParamsSourceHint(extra) {
  if (!extra || typeof extra !== "object" || extra.params_source !== "field_name") return "";
  const fields = (Array.isArray(extra.params_fields) ? extra.params_fields : [])
    .map((x) => String(x == null ? "" : x).trim()).filter(Boolean);
  const tip = fields.length ? "参数按工作流里的字段名回收：" + fields.join("、") : "参数按工作流里的字段名回收，不是链条解析结果";
  return '<span class="dt-sec-hint" title="' + esc(tip) + '">按字段名提取</span>';
}
// 任何我们创建过的 <video> 都要清理：只 pause 不够（本仓库修过一次"关掉还在播"的 bug），
// 但摘掉 src 只用于"节点即将被重建"的场合；网格卡片走 libReleaseVideo，顺带撤销未完成的抓帧尝试
function libPauseVideos(root) {
  if (!root) return;
  root.querySelectorAll("video").forEach((v) => { try { v.pause(); } catch (e) { } });
}
function libTeardownVideos(root) {
  libPauseVideos(root);
  if (!root) return;
  root.querySelectorAll("video").forEach((v) => {
    if (v.dataset.src) { libReleaseVideo(v); return; }
    v.removeAttribute("src"); try { v.load(); } catch (e) { }
  });
}
function libPopShow(el, anchor) {
  if (!el) return;
  if (el.classList.contains("lib-tagpop") && el.parentElement !== document.body) document.body.appendChild(el);  // 浮层 portal：祖先的 transform/overflow 都裁不到它
  el.hidden = false;
  lib.popAnchor = anchor || null;
  libPopPosition(el, anchor);
}
function libPopHide(el) { if (el) el.hidden = true; }
function libPopPosition(pop, anchor) {
  if (!pop || !anchor || !anchor.getClientRects().length) return;
  const z = Number(document.documentElement.style.zoom) || 1, gap = 8;
  const rect = anchor.getBoundingClientRect();
  pop.style.position = "fixed";
  pop.style.width = Math.min(320 * z, innerWidth - 2 * gap) / z + "px";
  const below = innerHeight - rect.bottom - 2 * gap, above = rect.top - 2 * gap;
  const upwards = below < 180 * z && above > below;
  pop.style.maxHeight = Math.max(60, Math.min(420 * z, upwards ? above : below, innerHeight - 2 * gap)) / z + "px";
  const box = pop.getBoundingClientRect();
  pop.style.left = Math.max(gap, Math.min(rect.left, innerWidth - box.width - gap)) / z + "px";
  pop.style.top = Math.max(gap, Math.min(upwards ? rect.top - box.height - gap : rect.bottom + gap, innerHeight - box.height - gap)) / z + "px";
}
function libHidePopovers() {
  libPopHide(libEl("libTagPop"));
  libPopHide(libEl("libSelTagPop"));            // 批量标签浮层与它同属一套 portal，Esc/外点一起收
  const folders = libEl("libFoldersPanel");
  if (folders) folders.hidden = true;          // 目录面板靠 hidden 开关（A4 的 .folders-panel 绝对定位在 #libFoldersWrap 内）
  const btn = libEl("libFoldersBtn");
  if (btn) btn.setAttribute("aria-expanded", "false");
  lib.popKind = ""; lib.popAnchor = null; lib.foldersOpen = false;
}

// ---------- 扫描 ----------
const LIB_PHASE = { walk: "遍历目录", parse: "解析元数据", reconcile: "对账", done: "完成" };
function libSetScanHint(t) { const el = libEl("libScanHint"); if (el) el.textContent = t || ""; }
function libSetScanBtns(running) {
  // A4 的标记用 disabled 表达「能不能点」（停止按钮默认存在但禁用）
  const s = libEl("libScanBtn"), stop = libEl("libStopScanBtn");
  if (s) s.disabled = !!running;
  if (stop) stop.disabled = !running;
}
function libSetRootHint() {
  const el = libEl("libRootHint"); if (!el) return;
  // A4 在这里写了固定的扩展名说明：保留原文，只在末尾追加当前目录状态。
  // 基准文本读写必须用同一个属性：以前存 _baseText、读 dataset.base（没人写过），
  // 每次轮询都会把说明文字冲成 "undefined …"，这里统一用 _baseText。
  if (el._baseText === undefined) el._baseText = (el.textContent || "").trim();
  const roots = (state.cfg || {}).media_roots || [];
  let extra = "";
  if (!roots.length) extra = "当前未配置资源目录（设置 · 资源库扫描目录）。";
  else {
    const bad = (lib.rootsState || []).filter((r) => r && r.status !== "ok");
    extra = "当前 " + roots.length + " 个目录" + (bad.length ? "，其中 " + bad.length + " 个不可达（" + short(bad[0].path || "", 24) + "）" : "") + "。";
  }
  el.textContent = el._baseText + (extra ? " " + extra : "");
}
function libScanIfNeeded() {
  // 进页只做一次「是否该增量扫描」的判断（后端按 last_scan_at ≥60s 或有新文件迹象决定）
  libHookCardVideos(libEl("libGrid"));      // 回页先把在场的卡片交回观察器，哪怕本轮不重绘网格
  libFacetRemeasure();                      // 页面刚转为可见：分面组溢出与否要按当前布局重量一次
  api.call("lib_rescan_if_stale").then((r) => {
    lib.noRoots = !!(r && r.reason === "no_roots");
    libSetRootHint();
    if (r && r.started) { libSetScanHint("增量扫描中…"); libStartScanPoll(); return; }
    // started=false 可能是「另一个扫描还在跑」：探一次状态，别拿着旧数据当新数据
    return api.call("lib_scan_state").catch(() => null).then((s) => {
      if (s && s.running) { libStartScanPoll(); return; }
      if (s) { lib.rootsState = Array.isArray(s.roots) ? s.roots : lib.rootsState; libSetRootHint(); }
      libLoad(true);
    });
  }).catch(() => { libSetScanHint("资源库后端未就绪（请确认程序已更新到含资源库的版本）"); libUpdateEmpty(); });
}
function libStartScan() {
  api.call("lib_scan").then((r) => {
    if (r && r.started) { libSetScanHint("扫描启动中…"); libStartScanPoll(); return; }
    // 后端拒绝扫描的两种原因都要让用户看见：没配目录 / 模型扫描占用中
    if (!(((state.cfg || {}).media_roots) || []).length) lib.noRoots = true;
    libUpdateEmpty();
    setStatus((r && r.msg) || "扫描已在进行中");
  }).catch(() => setStatus("扫描启动失败：资源库后端未就绪"));
}
function libStopScan() {
  api.call("lib_cancel_scan").then(() => setStatus("已请求停止扫描（已入库的部分保留）"))
    .catch(() => setStatus("停止请求失败，请稍后再试"));
}
function libStartScanPoll() {
  if (lib.scanTimer) return;                       // 防重复轮询：同一时刻只允许一个 interval
  lib.scanRunning = true; libSetScanBtns(true);
  lib.scanTimer = setInterval(async () => {
    if (!libPageActive()) { libStopScanPoll(); return; }   // 离开页面即停表（扫描仍在后台跑）
    let s = null;
    try { s = await api.call("lib_scan_state"); } catch (e) { }
    if (!s) return;
    lib.rootsState = Array.isArray(s.roots) ? s.roots : lib.rootsState;
    libSetRootHint();
    if (s.running) {
      const p = LIB_PHASE[s.phase] || s.phase || "";
      libSetScanHint("扫描中" + (p ? " · " + p : "") + (s.total ? " · " + (s.done || 0) + "/" + s.total : "") + (s.msg ? " · " + s.msg : ""));
      return;
    }
    libStopScanPoll();
    const errs = Array.isArray(s.errors) ? s.errors.length : 0;
    libSetScanHint((s.msg || "扫描完成") + (errs ? " · " + errs + " 条解析异常" : ""));
    await libLoad(true);
  }, 1000);                                        // §7：扫描进度 1s 轮询（对齐模型库）
}
function libStopScanPoll() {
  if (lib.scanTimer) { clearInterval(lib.scanTimer); lib.scanTimer = null; }
  lib.scanRunning = false; libSetScanBtns(false);
}

// ---------- 列表加载 ----------
// 时长输入：留空＝不限（不发给服务端）；非负数才有效；两端都填时下限不得大于上限。
// 返回值 {min,max,error}——error 非空时调用方必须当场提示并且**不发请求**（§15.3.1）。
function libParseDur(raw) {
  const s = String(raw == null ? "" : raw).trim();
  if (!s) return null;                        // 留空 = 这一端不限
  const n = Number(s);
  if (!isFinite(n) || n < 0) return NaN;      // 非数字 / 负数 = 非法
  return n;
}
function libReadDurations() {
  const min = libParseDur(libVal("libDurMin")), max = libParseDur(libVal("libDurMax"));
  if (typeof min === "number" && isNaN(min)) return { min: null, max: null, error: "时长下限要是不小于 0 的秒数，留空表示不限" };
  if (typeof max === "number" && isNaN(max)) return { min: null, max: null, error: "时长上限要是不小于 0 的秒数，留空表示不限" };
  if (min != null && max != null && min > max) return { min: min, max: max, error: "时长下限不能大于上限（" + min + " 秒 > " + max + " 秒）" };
  return { min: min, max: max, error: "" };
}
function libReadControls() {
  const f = lib.filters;
  f.kind = libVal("libKindF");
  f.sort = libVal("libSort") || "time_desc";
  f.group_by = libVal("libGroup");
  f.checkpoints = libVal("libCkF") ? [libVal("libCkF")] : [];
  f.loras = libVal("libLoraF") ? [libVal("libLoraF")] : [];
  f.tag_ids = libVal("libTagF") ? [Number(libVal("libTagF"))] : [];
  f.starred = libFlag("libStarOnly");
  f.no_meta = libFlag("libNoMeta");
  const dir = libVal("libDirF");
  // 目录有两个入口：下拉（单选）与面板（多选）。下拉有值时以它为准，否则沿用面板的多选集合。
  if (dir) f.dirs = [dir]; else f.dirs = lib._dirsFromPanel.slice();
  f.q = libVal("libQ").trim();
  // samplers / schedulers / sizes 的真相在 lib.filters（chip 组由 lib.filters 反着渲染，DOM 里没有独立状态），
  // 这里不动它们；时长区间是输入框，照其它控件一样从 DOM 回收，非法值保留上一份有效值并记下 error。
  const dur = libReadDurations();
  lib._durError = dur.error;
  if (!dur.error) { f.duration_min = dur.min; f.duration_max = dur.max; }
}
function libBuildFlt(offset) {
  const f = lib.filters;
  const flt = {
    kind: f.kind, checkpoints: f.checkpoints, loras: f.loras, dirs: f.dirs, tag_ids: f.tag_ids,
    starred: f.starred, no_meta: f.no_meta, q: f.q, sort: f.sort, group_by: f.group_by,
    samplers: f.samplers, schedulers: f.schedulers, sizes: f.sizes,
    offset: offset, limit: libLimit(),
  };
  // 留空就不传（§15.2.4 的口径：缺键＝该维度不限），别把 null 发过去让服务端按 0 处理
  if (f.duration_min != null) flt.duration_min = f.duration_min;
  if (f.duration_max != null) flt.duration_max = f.duration_max;
  return flt;
}
function libHasFilter() {
  const f = lib.filters;
  return !!(f.kind || f.q || f.starred || f.no_meta || f.checkpoints.length || f.loras.length || f.dirs.length || f.tag_ids.length ||
    f.samplers.length || f.schedulers.length || f.sizes.length || f.duration_min != null || f.duration_max != null);
}
async function libLoad(reset) {
  const grid = libEl("libGrid"); if (!grid) return;
  // 先读控件再作废当前列表：时长区间非法时只提示、直接返回，屏幕上的列表保持原样（不发请求）。
  if (reset) {
    libReadControls();
    if (lib._durError) { setStatus(lib._durError); return; }
    lib.request++; lib.rows = []; lib.offset = 0;
  }
  const flt = libBuildFlt(reset ? 0 : lib.offset);
  // 续拉分页不重算分面：真实库实测 facets ~130ms，是整页耗时的主要来源，翻页时用不上
  flt.with_facets = reset ? 1 : 0;
  const token = lib.request;
  lib.loading = true; libSetLoading(true); libUpdateEmpty();
  let data = null;
  try {
    const raw = await api.call("lib_list", JSON.stringify(flt));   // 大批量负载后端以 JSON 字符串返回
    data = typeof raw === "string" ? JSON.parse(raw || "{}") : (raw || null);
  } catch (e) { data = null; }
  if (token !== lib.request) return;                               // 迟到响应（已换筛选/已重置）直接丢弃
  lib.loading = false; libSetLoading(false);
  if (!data || data.ok === false) {
    libSetScanHint("资源库读取失败" + (data && data.msg ? "：" + data.msg : ""));
    libRenderGrid(); libUpdateStats(); libUpdateEmpty(); libUpdateSelUi(); return;
  }
  const items = Array.isArray(data.items) ? data.items : [];
  lib.total = Number(data.total || 0);
  if (Number(data.limit)) lib.limit = Number(data.limit);
  lib.offset = Number(flt.offset) + items.length;                  // 续拉偏移按实际返回条数推进，不受服务端回显影响
  if (reset) lib.rows = items; else lib.rows = lib.rows.concat(items);
  if (data.facets && !data.facets_skipped) { lib.facets = data.facets; libFillFacets(); }
  // #libScanHint 在空闲时改读「库里有多少」，别停在几轮前的"扫描中…"上
  if (!lib.scanRunning) {
    const cur = (libEl("libScanHint") || {}).textContent || "";
    if (!cur || /扫描中|未扫描|读取失败/.test(cur)) libSetScanHint(lib.noRoots ? "未配置资源目录" : "库内 " + lib.total + " 项");
  }
  // 已选集合跨页、跨筛选保留（直到用户点「清除选择」或批量删除），所以这里**不再** lib.sel.clear()。
  // 换筛选/重置时列表换了天地，shift 范围选的锚点下标必须先作废，否则会圈进不相干的条目。
  if (reset) lib.selAnchor = null;
  libRememberRows(items);
  if (reset) libRenderGrid(); else libAppendCards(items);
  libUpdateStats(); libUpdateEmpty(); libUpdateMore(); libUpdateSelUi(); libPrefetchThumbs();
}
function libGroupHtml(group, last) {
  if (!group || group === last) return "";
  return '<div class="lib-group-title">' + esc(group) + "</div>";
}
function libCardHtml(it) {
  const isVid = it.kind === "video";
  const selected = lib.sel.has(String(it.id));
  const dims = isVid ? libDur(it.duration) : (it.width && it.height ? it.width + "×" + it.height : "");
  const sub = [it.checkpoint ? short(String(it.checkpoint), 20) : "", dims].filter(Boolean).join(" · ");
  // 后端已经把 prompt 前 120 字算成 snippet：卡片没地方全文展示，就挂在文件名 tooltip 上（悬停即读）。
  // snippet 来自文件内嵌元数据，属于外部可控文本，一律过 esc。
  const snip = String(it.snippet || "").trim();
  const nameTip = [String(it.filename || ""), snip ? "提示词：" + snip : ""].filter(Boolean).join(" · ");
  // 视频且还没有海报缓存时放 <video>：src 只写进 data-src，等观察器确认卡片进了视口附近再挂上。
  // 渲染 200 张卡就直接挂 200 个 preload="metadata"，等于同时开 200 路 /api/media 流（Chromium 每个
  // origin 只排队 6 条连接，多余的只能干等，每个解码器还各占一份内存），所以这里按需挂载并按时释放。
  const media = (isVid && !it.thumb)
    ? '<video class="ms-img lib-vid" muted loop playsinline preload="metadata" data-path="' + esc(it.path) +
      '" data-src="' + esc(libMediaUrl(it) + "#t=0.1") + '" data-lib-live="0"></video>'
    : '<img class="ms-img" decoding="async" alt="" data-path="' + esc(it.path) + '"/>';
  const chips = (Array.isArray(it.tags) ? it.tags : []).slice(0, 3)
    .map((t) => '<span class="lib-chip"' + (t.color ? ' style="border-color:' + esc(t.color) + '"' : "") + ">" + esc(t.name) + "</span>").join("");
  return '<div class="ms-card lib-card' + (selected ? " checked" : "") + (String(it.id) === String(lib.current) ? " is-current" : "") + '" tabindex="0" data-id="' + esc(String(it.id)) + '" data-path="' + esc(it.path) + '" data-kind="' + esc(it.kind) + '"' +
    ' title="' + esc(String(it.filename || "") + " · " + String(it.path || "")) + '">' +
    // 多选态才可见的勾选框（可见性由 #libGrid.lib-selecting 管，见 style.css）：
    // 结构、类名与模型库瀑布流完全一致（.ms-check + .cbox），卡片本身仍是单击切换的地方，
    // 所以这里只当指示器，不给它自己的交互与语义（aria-hidden）。
    '<span class="ms-check" aria-hidden="true">' + (selected ? '<span class="cbox on"></span>' : '<span class="cbox"></span>') + "</span>" +
    '<div class="ms-img-wrap" data-ph="' + ((isVid && !it.thumb) ? "ok" : "loading") + '">' + media +
    (isVid ? '<span class="thumb-video-badge">▶</span>' : "") +
    (it.has_meta ? "" : '<span class="lib-badge-meta" title="无内嵌元数据">?</span>') +
    "</div>" +
    '<div class="ms-info"><div class="ms-titles">' +
    '<div class="ms-name" title="' + esc(nameTip) + '">' + esc(it.filename || "") + "</div>" +
    '<div class="ms-sub">' + esc(sub || (isVid ? "视频" : "图片")) + "</div>" +
    (chips ? '<div class="lib-chips">' + chips + "</div>" : "") +
    '</div><div class="ms-footer"><span class="ms-size">' + esc(fmtSize(it.size)) + '</span><span class="lib-time">' + esc(libDay(it.mtime)) + "</span></div></div></div>";
}
function libRenderGrid() {
  const grid = libEl("libGrid"); if (!grid) return;
  const vp = libEl("libViewport"), keepTop = vp ? vp.scrollTop : 0;   // 重绘不回顶部（收藏/删标签都会重绘）
  if (lib.vObs) lib.vObs.disconnect();                                 // 旧节点全部作废：先解绑再重挂，避免观察器攥着已丢弃的元素
  libTeardownVideos(grid);
  // 多选态是网格上的一个类，重绘（含切页回来）都要照着 lib.selectMode 补回来，
  // 不靠「切开关时重绘一次」——卡片 HTML 里的勾选框一直渲着，只由它决定可见与否。
  grid.classList.toggle("lib-selecting", !!lib.selectMode);
  let last = "", html = "";
  for (const it of lib.rows) { const g = libGroupHtml(it.group, last); if (g) { last = it.group; html += g; } html += libCardHtml(it); }
  grid.innerHTML = html;
  if (vp) vp.scrollTop = keepTop;
  libApplyCachedThumbs(); libHookCardVideos(grid); libUpdateMore();
}
function libAppendCards(items) {
  const grid = libEl("libGrid"); if (!grid) return;
  let last = "", html = "";
  for (const it of items) { const g = libGroupHtml(it.group, last); if (g) { last = it.group; html += g; } html += libCardHtml(it); }
  if (html) grid.insertAdjacentHTML("beforeend", html);
  libApplyCachedThumbs(); libHookCardVideos(grid);
}
function libSetLoading(on) {
  const el = libEl("libLoading"); if (!el) return;
  el.hidden = !(on && !lib.rows.length);
}
function libUpdateMore() {
  const more = libEl("libMore"); if (!more) return;
  const show = !lib.scanRunning && lib.rows.length < lib.total;
  more.hidden = !show;
  if (show) more.textContent = "加载更多（已加载 " + lib.rows.length + " / " + lib.total + "）";
}
function libUpdateStats() {
  const bar = libEl("libStatsBar"); if (!bar) return;
  const f = lib.facets || {}, kinds = f.kinds || {};
  // facets.total 是库内全局数（不受筛选影响），lib.total 是本次 lib_list 的过滤后总数：
  // 有筛选时两个都要说清楚，否则用户会拿着「共 2792 项」去理解只有 863 项的结果。
  const globalTotal = f.total != null ? Number(f.total) : null;
  const parts = [];
  if (libHasFilter()) parts.push("筛选 " + lib.total + " 项" + (globalTotal != null ? " / 共 " + globalTotal + " 项" : ""));
  else parts.push("共 " + (globalTotal != null ? globalTotal : lib.total) + " 项");
  if (kinds.image != null) parts.push("图片 " + kinds.image);
  if (kinds.video != null) parts.push("视频 " + kinds.video);
  if (f.starred != null) parts.push("收藏 " + f.starred);
  if (f.no_meta != null) parts.push("无元数据 " + f.no_meta);
  if (f.missing) parts.push("文件已删 " + f.missing);
  parts.push("已加载 " + lib.rows.length);
  if (lib.sel.size) parts.push("选中 " + lib.sel.size);
  bar.innerHTML = parts.map((p) => "<span>" + esc(p) + "</span>").join("");   // .lib-stats 是 flex+gap，一项一个 span
}
function libUpdateEmpty() {
  const empty = libEl("libEmpty"); if (!empty) return;
  if (lib.rows.length || lib.loading || lib.scanRunning) { empty.hidden = true; return; }
  empty.hidden = false;
  const title = lib.noRoots ? "还没有配置资源目录" : (libHasFilter() ? "无匹配项" : "资源库还没有内容");
  const hint = lib.noRoots
    ? "去设置页添加 ComfyUI 输出目录（可填多个），再回这里点「扫描资源」。"
    : (libHasFilter() ? "当前筛选条件下没有文件；清掉关键词或分面再看。" : "点上面的「扫描资源」索引本地 ComfyUI 产物。");
  // A4 的空态是 <p class="lib-empty-title"> + <p class="hint">：有结构就只改文字，没结构再回落纯文本
  const t = empty.querySelector(".lib-empty-title"), h = empty.querySelector(".hint");
  if (t || h) { if (t) t.textContent = title; if (h) h.textContent = hint; }
  else empty.textContent = title + "：" + hint;
}

// ---------- 多选与批量操作 ----------
// 分片口径：服务端 _LIB_BATCH_MAX = 2000 个 id 一次（webui.py），前端按 500 一片提交，
// 每片各自累计成功数；某片抛错/返回 ok=false 只把该片 id 记成失败，后面的片照发（不因一条坏数据废掉整批）。
const LIB_BATCH_CHUNK = 500;
const LIB_SWEEP_LIMIT = 500;          // #libSelAllFilter 逐批取 id 的 limit（服务端硬上限也是 500）
const LIB_SWEEP_MAX_BATCHES = 200;    // 单次最多发多少批，兜住 total 回显异常时的死循环
const LIB_SWEEP_MAX_IDS = 50000;      // 单次最多取回多少个 id；到顶就如实说「其余未选」
const LIB_BYID_MAX = 30000;           // id→路径 台账的会话上限（超了先丢掉用不上的）

function libChunk(arr) {
  const out = [];
  for (let i = 0; i < arr.length; i += LIB_BATCH_CHUNK) out.push(arr.slice(i, i + LIB_BATCH_CHUNK));
  return out;
}
function libSelIds() { return Array.from(lib.sel).map((s) => Number(s)).filter((n) => n > 0); }
// id → {path, kind, hasMeta}：列表加载与「选中筛选全部」都会登记。跨页选中的条目本地没有行数据，
// 送逆向／送工作流要靠这本账把 id 还原成文件路径；拿不到就如实跳过并报数，绝不猜。
function libRememberRows(items) {
  if (lib.byId.size > LIB_BYID_MAX) libPruneById();
  for (const it of items) {
    if (!it || it.id == null) continue;
    lib.byId.set(String(it.id), { path: String(it.path || ""), kind: String(it.kind || ""), hasMeta: !!it.has_meta });
  }
}
function libPruneById() {
  const keep = new Set(lib.sel);
  for (const r of lib.rows) keep.add(String(r.id));
  for (const k of Array.from(lib.byId.keys())) if (!keep.has(k)) lib.byId.delete(k);
}
function libByIdOf(id) {
  const k = String(id);
  const rec = lib.byId.get(k);
  if (rec && rec.path) return rec;
  const row = libRowById(k);
  if (row) return { path: String(row.path || ""), kind: String(row.kind || ""), hasMeta: !!row.has_meta };
  return rec || null;
}
// 已选里哪些真能送出去：逆向沿用单条详情的口径（只有图片送），工作流沿用「有内嵌元数据才按」
function libSelTargets() {
  const out = { reverse: [], workflow: [], noPath: 0, video: 0 };
  for (const k of lib.sel) {
    const rec = libByIdOf(k);
    if (!rec || !rec.path) { out.noPath++; continue; }
    if (rec.kind === "video") { out.video++; continue; }
    out.reverse.push(rec.path);
    if (rec.hasMeta) out.workflow.push(rec.path);
  }
  return out;
}

function libCardEl(id) {
  const grid = libEl("libGrid"); if (!grid) return null;
  return grid.querySelector('.lib-card[data-id="' + CSS.escape(String(id)) + '"]');
}
function libPaintCard(card) {
  if (!card) return;
  const on = lib.sel.has(card.dataset.id);
  card.classList.toggle("checked", on);
  const chk = card.querySelector(".ms-check");
  if (chk) chk.innerHTML = on ? '<span class="cbox on"></span>' : '<span class="cbox"></span>';
}
function libPaintAllCards() {
  const grid = libEl("libGrid"); if (!grid) return;
  grid.querySelectorAll(".lib-card").forEach(libPaintCard);
}
// 开关多选：只切网格上的一个类 + 显示选择行。不重绘网格（勾选框本来就在卡片里，靠 CSS 决定显隐），
// 这样缩略图缓存、视频懒挂载、滚动位置都不受开关影响；已选集合也不动。
function libSetSelectMode(on) {
  lib.selectMode = !!on;
  const grid = libEl("libGrid");
  if (grid) grid.classList.toggle("lib-selecting", lib.selectMode);
  const row = libEl("libSelRow");
  if (row) row.hidden = !lib.selectMode;
  libUpdateSelUi();
}
function libActivateCard(card, shift) {
  if (!card) return;
  const id = card.dataset.id;
  if (lib.selectMode) { libSelectClick(id, shift); return; }
  // 非多选态：单击行为与改动前一致（打开详情）。只是高亮类分了家——.checked 归批量已选、
  // 当前详情对应的那张卡用 .is-current，批量选择不会再被「顺手点一张卡」冲掉。
  lib.current = String(id);
  const grid = libEl("libGrid");
  if (grid) grid.querySelectorAll(".lib-card.is-current").forEach((c) => { if (c !== card) c.classList.remove("is-current"); });
  card.classList.add("is-current");
  libShowDetail(id);
}
// 多选态单击＝切换选中；shift 点选在「当页」（= lib.rows 已加载的这段）里做范围选
function libSelectClick(id, shift) {
  const k = String(id);
  const idx = lib.rows.findIndex((r) => String(r.id) === k);
  if (shift && lib.selAnchor != null && idx >= 0) {
    const a = Math.min(lib.selAnchor, idx), b = Math.max(lib.selAnchor, idx);
    for (let i = a; i <= b; i++) { const r = lib.rows[i]; if (r && r.id != null) lib.sel.add(String(r.id)); }
    libPaintAllCards();                     // 范围可能横跨几十张卡，整网格刷一遍比逐张 querySelector 便宜
  } else {
    if (lib.sel.has(k)) lib.sel.delete(k); else lib.sel.add(k);
    lib.selAnchor = idx >= 0 ? idx : null;
    libPaintCard(libCardEl(k));
  }
  libUpdateStats(); libUpdateSelUi();
}
function libSelAllPage() {
  if (lib.selFetching || lib.batchRunning) { setStatus("有作业在跑，请等它结束"); return; }
  if (!lib.rows.length) { setStatus("当页还没加载出条目：先滚动加载更多，或回上面重新扫描"); return; }
  libRememberRows(lib.rows);
  let added = 0;
  for (const r of lib.rows) { if (r && r.id != null && !lib.sel.has(String(r.id))) { lib.sel.add(String(r.id)); added++; } }
  libPaintAllCards(); libUpdateStats(); libUpdateSelUi();
  setStatus("已选中当页已加载的 " + lib.rows.length + " 项（本次新增 " + added + " 项，累计已选 " + lib.sel.size + " 项）");
}
function libSelClear() {
  if (lib.batchRunning) { setStatus("批量动作还在跑，请等它结束"); return; }
  if (!lib.sel.size) { setStatus("已选集合本来就是空的"); return; }
  const n = lib.sel.size;
  lib.sel.clear(); lib.selTagIds.clear(); lib.selAnchor = null; libSetProg("");
  libPaintAllCards(); libUpdateStats(); libUpdateSelUi();
  setStatus("已清除 " + n + " 项选择（只清选择，没动任何文件与库里的行）");
}
// 「选中当前筛选条件下的全部结果」：服务端分页，本地只有 lib.rows，所以用同一份 flt
// 以 limit=500 逐批把 id 拉全再并入。这里不碰 lib.request／lib.rows——列表本身不重绘，
// 只在已选集合上累加，避免把用户正在看的滚动位置打断。
async function libSelAllFilter() {
  if (lib.batchRunning) { setStatus("批量动作还在跑，请等它结束"); return; }
  if (lib.selFetching) {                     // 再点一次＝停：令牌作废，已并入的部分留着
    lib.selFetchToken++; lib.selFetching = false; libSetProg(""); libUpdateSelUi();
    setStatus("已停止取 id，已并入的 " + lib.sel.size + " 项保留");
    return;
  }
  libReadControls();
  if (lib._durError) { setStatus(lib._durError); return; }   // 非法区间当场拦住，不发查询
  const token = ++lib.selFetchToken;
  lib.selFetching = true; libUpdateSelUi();
  let offset = 0, fetched = 0, total = 0, added = 0, calls = 0, stop = "";
  while (true) {
    if (token !== lib.selFetchToken || !libPageActive()) { stop = stop || "已离开资源库页"; break; }
    const flt = libBuildFlt(offset);
    flt.limit = LIB_SWEEP_LIMIT;
    let data = null;
    try {
      const raw = await api.call("lib_list", JSON.stringify(flt));
      data = typeof raw === "string" ? JSON.parse(raw || "{}") : (raw || null);
    } catch (e) { data = null; }
    calls++;
    if (!data || data.ok === false) { stop = "第 " + calls + " 批读取失败" + (data && data.msg ? "（" + short(String(data.msg), 40) + "）" : ""); break; }
    const items = Array.isArray(data.items) ? data.items : [];
    total = Number(data.total || 0);
    libRememberRows(items);
    for (const it of items) { if (it && it.id != null && !lib.sel.has(String(it.id))) { lib.sel.add(String(it.id)); added++; } }
    fetched += items.length;
    offset = Number(flt.offset) + items.length;
    libSetProg("正在按当前筛选取 id：" + fetched + " / " + total + "（第 " + calls + " 批 ×" + LIB_SWEEP_LIMIT + "）");
    if (calls % 10 === 0) setStatus("取 id 中：已取 " + fetched + "/" + total + "（" + calls + " 批）");
    if (!items.length || offset >= total) break;
    if (fetched >= LIB_SWEEP_MAX_IDS) { stop = "已达单次上限 " + LIB_SWEEP_MAX_IDS + " 项"; break; }
    if (calls >= LIB_SWEEP_MAX_BATCHES) { stop = "已达单次上限 " + LIB_SWEEP_MAX_BATCHES + " 批"; break; }
  }
  lib.selFetching = false; libSetProg("");
  libPaintAllCards(); libUpdateStats(); libUpdateSelUi();
  setStatus("已按当前筛选并入 " + added + " 项，累计已选 " + lib.sel.size + " 项（取 id " + calls + " 批 ×" + LIB_SWEEP_LIMIT + "）" + (stop ? "，" + stop + "，其余未选" : ""));
}

// ---------- 批量动作的公共骨架 ----------
function libSetProg(text) {
  lib.prog = String(text || "");
  const p = libEl("libSelProg");
  if (p) { p.textContent = lib.prog; p.hidden = !lib.prog; }
  const cnt = libEl("libSelBarCount");
  if (cnt) cnt.textContent = lib.prog || ("已选 " + lib.sel.size + " 项");
}
function libUpdateSelUi() {
  const n = lib.sel.size, busy = !!(lib.batchRunning || lib.selFetching);
  const bar = libEl("libSelBar");
  if (bar) bar.hidden = !n;                                   // 空集合不浮操作条，免得一屋子禁用按钮
  libSetProg(lib.prog);
  const st = libEl("libSelState");
  if (st) st.textContent = lib.prog || (n ? "已选 " + n + " 项" : (lib.selectMode ? "未选中任何项" : "多选已关闭"));
  const set = (id, on, tip) => {
    const el = libEl(id); if (!el) return;
    el.disabled = busy || !on;
    // 动态说明写 data-tip（本仓库自己的悬浮提示读它），不要写 title：
    // 写了就会同时冒出浏览器原生气泡 + 软件气泡，两层提示叠着看很乱。
    if (tip !== undefined) el.dataset.tip = tip;
  };
  const allPage = libEl("libSelAllPage");
  if (allPage) { allPage.disabled = busy; allPage.textContent = lib.rows.length ? ("选中当页已加载 " + lib.rows.length + " 项") : "选中当页已加载"; }
  const allFilter = libEl("libSelAllFilter");
  if (allFilter) { allFilter.disabled = lib.batchRunning; allFilter.textContent = lib.selFetching ? "停止取 id" : "选中筛选结果全部"; }
  const clear = libEl("libSelClear"); if (clear) clear.disabled = busy || !n;
  const t = libSelTargets();
  set("libSelStar", n > 0, "批量收藏已选的 " + n + " 项：只改库里的标记，不动文件");
  set("libSelUnstar", n > 0, "批量取消收藏已选的 " + n + " 项");
  set("libSelTagAdd", n > 0, "给已选的 " + n + " 项加标签（并集，不动原有标签）");
  set("libSelTagRemove", n > 0, "从已选的 " + n + " 项摘标签");
  set("libSelDelete", n > 0, "删除已选的 " + n + " 项：文件进回收站，同名 .json 不删");
  // 送分析两个按钮要能把 id 还原成文件路径才有意义：能送的数写在 title 里，
  // 一个都还原不出来时说明原因，而不是摆个「点了没反应」的按钮。
  const revTip = t.reverse.length
    ? ("把这 " + t.reverse.length + " 张图片按文件哈希加入反向解析列表" +
       ((t.video || t.noPath) ? "（已跳过 " + t.video + " 个视频、" + t.noPath + " 个本地取不到路径的）" : ""))
    : ("已选里没有能送去逆向解析的图片（只有图片能送，且本地要取到文件路径；共 " + n + " 项已选）");
  set("libSelReverse", t.reverse.length > 0, revTip);
  const wfTip = t.workflow.length
    ? ("送工作流分析：分析页一次只展示一个结果，会送这 " + t.workflow.length + " 个里的第 1 个")
    : "已选里没有能分析的文件（要带内嵌元数据，且本地能取到文件路径）";
  set("libSelWorkflow", t.workflow.length > 0, wfTip);
}
async function libRunChunks(label, chunks, run) {
  const totalIds = chunks.reduce((s, c) => s + c.length, 0);
  let processed = 0, done = 0, failed = 0, lastMsg = "";
  for (const chunk of chunks) {
    let r = null;
    try { r = await run(chunk); } catch (e) { r = null; }
    processed += chunk.length;
    if (r && r.ok) done += Number(r.count != null ? r.count : chunk.length);
    else { failed += chunk.length; lastMsg = (r && r.msg) || "请求失败"; }
    const prog = label + "：已处理 " + processed + "/" + totalIds + (failed ? "（失败 " + failed + "）" : "");
    libSetProg(prog); setStatus(prog);                       // §7：过程用 setStatus 播报
  }
  return { done, failed, processed: totalIds, chunks: chunks.length, lastMsg };
}
function libBatchSummary(label, res, extra) {
  const bits = [label + "完成：" + res.done + " 项生效"];
  if (res.failed) bits.push(res.failed + " 项失败" + (res.lastMsg ? "（" + short(String(res.lastMsg), 40) + "）" : ""));
  const ignored = res.processed - res.done - res.failed;      // 服务端 count 只算库里真存在的行，未知 id 自动忽略
  if (ignored > 0) bits.push(ignored + " 项库里已查不到");
  if (extra) bits.push(extra);
  bits.push("共 " + res.chunks + " 片 ×" + LIB_BATCH_CHUNK);
  return bits.join("，");
}
async function libBatchAction(label, run) {
  if (lib.batchRunning) { setStatus("上一个批量动作还在跑，请等它结束"); return null; }
  if (lib.selFetching) { setStatus("「选中筛选结果全部」还在取 id，请等它结束或再点一次停止"); return null; }
  const ids = libSelIds();
  if (!ids.length) { setStatus("先勾选要批量操作的资源：筛选行里打开「多选」，单击卡片即选中"); return null; }
  const chunks = libChunk(ids);
  lib.batchRunning = true; libUpdateSelUi();
  let res = null;
  try { res = await libRunChunks(label, chunks, run); }
  finally { lib.batchRunning = false; libSetProg(""); libUpdateSelUi(); }
  return res;
}
// 批量改完数据后的统一收尾：走既有的重载路径（libLoad(true) 顺带把 facets 计数与统计条一起刷新）
function libAfterBatchMutate() {
  libUpdateStats(); libUpdateSelUi();
  return libLoad(true);
}
function libBatchStar(on) {
  const label = on ? "批量收藏" : "批量取消收藏";
  return libBatchAction(label, (c) => api.call("lib_set_star_batch", JSON.stringify(c), on))
    .then((res) => { if (!res) return; setStatus(libBatchSummary(label, res)); return libAfterBatchMutate(); });
}
function libMergeRowTags(byId) {
  for (const k of Object.keys(byId || {})) {
    const row = libRowById(k);
    if (row) row.tags = Array.isArray(byId[k]) ? byId[k] : [];   // 服务端回的是结果态（并集/已摘），直接落到行数据
  }
}
function libBatchTags(mode, tagIds) {
  const method = mode === "add" ? "lib_tag_add_batch" : "lib_tag_remove_batch";
  const label = mode === "add" ? "批量加标签" : "批量摘标签";
  return libBatchAction(label, (c) => api.call(method, JSON.stringify(c), JSON.stringify(tagIds)).then((r) => {
    if (r && r.ok && r.tags) libMergeRowTags(r.tags);
    return r;
  })).then((res) => {
    if (!res) return;
    setStatus(libBatchSummary(label, res, "标签 " + tagIds.length + " 个"));
    lib.selTagIds.clear();
    // 标签计数变了：#libTagF 与浮层要跟着换（libEnsureTags→libAfterTagChange），再走列表重载
    return libEnsureTags().then(() => { libAfterTagChange(); return libAfterBatchMutate(); });
  });
}
function libDeleteConfirmText(n) {
  return "删除已选的 " + n + " 项？文件会被移进系统回收站（不是彻底删除，可从回收站还原），库里的行同时标记为已删除。" +
    "同名 .json 工作流文件不会删除：ComfyUI 用它同时存放本媒体与同名兄弟文件（图片／视频共用一份生成数据）的元数据，删掉会连带毁掉兄弟文件的元数据。" +
    "这一步只碰本地文件，不访问 C 站，也不动模型库。";
}
async function libBatchDelete() {
  if (lib.batchRunning || lib.selFetching) { setStatus("有作业在跑，请等它结束"); return; }
  const ids = libSelIds();
  if (!ids.length) { setStatus("先勾选要批量删除的资源"); return; }
  if (!(await confirmBox(libDeleteConfirmText(ids.length)))) { setStatus("已取消删除，什么都没动"); return; }
  const chunks = libChunk(ids);
  lib.batchRunning = true; libUpdateSelUi();
  let processed = 0, deleted = 0, missing = 0, failed = 0, lastMsg = "";
  try {
    for (const chunk of chunks) {
      let r = null;
      try { r = await api.call("lib_delete_batch", JSON.stringify(chunk)); } catch (e) { r = null; }
      processed += chunk.length;
      if (r && r.ok) { deleted += Number(r.deleted || 0); missing += Number(r.missing || 0); }
      else { failed += chunk.length; lastMsg = (r && r.msg) || "请求失败"; }
      const prog = "批量删除：已处理 " + processed + "/" + ids.length + (deleted ? "（已进回收站 " + deleted + "）" : "");
      libSetProg(prog); setStatus(prog);
    }
  } finally {
    lib.batchRunning = false; libSetProg("");
  }
  // 删除后这些 id 必须从已选集合摘掉（否则操作条攥着一堆已经不存在的行），顺带清掉它们的本地台账与缩略图缓存
  const paths = [];
  for (const id of ids) {
    const k = String(id), rec = lib.byId.get(k);
    if (rec && rec.path) paths.push(rec.path);
    lib.sel.delete(k); lib.byId.delete(k);
  }
  for (const p of paths) { lib.thumbCache.delete(p); lib.posterTried.delete(p); }
  if (lib.current != null && ids.some((id) => String(id) === String(lib.current))) lib.current = null;
  const bits = ["批量删除完成：" + deleted + " 个文件已进回收站"];
  if (missing) bits.push(missing + " 项文件本来就不在磁盘上（只标记，没报错）");
  if (failed) bits.push(failed + " 项失败" + (lastMsg ? "（" + short(lastMsg, 40) + "）" : ""));
  setStatus(bits.join("，") + "，共 " + chunks.length + " 片 ×" + LIB_BATCH_CHUNK + "；同名 .json 侧车有意保留");
  await libAfterBatchMutate();
}
function libBatchReverse() {
  if (lib.batchRunning || lib.selFetching) { setStatus("有作业在跑，请等它结束"); return; }
  const t = libSelTargets();
  if (!t.reverse.length) {
    setStatus("已选里没有能送去逆向解析的图片" + (t.noPath ? "（" + t.noPath + " 项本地取不到文件路径，先用「选中筛选结果全部」把 id 与路径取回来）" : "（只有图片能送，视频按单条详情的同一口径不送）"));
    return;
  }
  const chunks = libChunk(t.reverse);
  let sent = 0, failed = 0, blocked = false;
  const step = (i) => {
    if (i >= chunks.length) return Promise.resolve();
    return api.call("rp_add_paths", chunks[i]).then((ok) => {
      if (ok) { sent += chunks[i].length; setStatus("送逆向解析：已处理 " + Math.min(i * LIB_BATCH_CHUNK + chunks[i].length, t.reverse.length) + "/" + t.reverse.length); return step(i + 1); }
      // 返回 false＝api 包装层拦下了（缺 Civitai Key：它已经提示并弹引导），
      // 这里不再叠一条状态、也不再往后发，与单条详情「if (!ok) return」同一处理。
      blocked = true;
      return Promise.resolve();
    }).catch(() => { failed += chunks[i].length; return step(i + 1); });     // 单片抛错不中断整批
  };
  return step(0).then(() => {
    if (blocked) return;
    if (!sent) { setStatus("送去反向解析失败：列表一个文件都没收到（可以再去反向解析页确认）"); return; }
    libLeavePage();
    switchPage("reverse");
    if (typeof rpRefresh === "function") rpRefresh();
    const skip = [];
    if (t.video) skip.push(t.video + " 个视频");
    if (t.noPath) skip.push(t.noPath + " 个取不到路径的");
    setStatus("已送去逆向解析 " + sent + " 张图片" + (failed ? "，" + failed + " 个提交失败" : "") + (skip.length ? "，跳过 " + skip.join("、") : "") + "：在反向解析页点「开始反查」才会联网查询");
  });
}
function libBatchWorkflow() {
  if (lib.batchRunning || lib.selFetching) { setStatus("有作业在跑，请等它结束"); return; }
  const t = libSelTargets();
  if (!t.workflow.length) { setStatus("已选里没有能分析的文件：要带内嵌元数据、且本地能取到文件路径"); return; }
  const first = String(t.workflow[0] || "");
  libCloseDetail(); libLeavePage();
  switchPage("workflow");
  if (typeof wfAnalyze === "function") wfAnalyze(first); else api.call("analyze_workflow", first).catch(() => { });
  setStatus(t.workflow.length > 1
    ? "工作流分析一次只展示一个结果：已送已选里第 1 个（" + short(first.split(/[\\/]/).pop() || "", 24) + "），另有 " + (t.workflow.length - 1) + " 个可分析"
    : "已送工作流分析：" + short(first.split(/[\\/]/).pop() || "", 24));
}

// ---------- 批量标签浮层（先勾标签，再一次应用） ----------
function libSelTagSlot(pop) { return pop.querySelector("[data-lib-slot='sel-chips']") || pop; }
function libToggleSelTagPop(anchor, mode) {
  if (lib.popKind === "seltags" && lib.selTagMode === mode) { libHidePopovers(); return; }   // 同一个按钮再点＝收起
  libOpenSelTagPop(anchor, mode);
}
function libOpenSelTagPop(anchor, mode) {
  const pop = libEl("libSelTagPop");
  if (!pop) return;
  if (!lib.sel.size) { setStatus("先勾选要批量打标签的资源"); return; }
  lib.popKind = "seltags"; lib.selTagMode = mode === "remove" ? "remove" : "add";
  lib.selTagIds.clear();                                       // 每次开浮层都从空勾选开始，避免「上次没点的标签被带进这次」
  libPopShow(pop, anchor);
  libSelTagSlot(pop).innerHTML = '<span class="dt-dim">读取标签中…</span>';
  libRenderSelTagPop(pop);                                     // 说明与「应用」按钮的数是本地已知数，先给出来
  libEnsureTags().then(() => { if (lib.popKind === "seltags" && !pop.hidden) libRenderSelTagPop(pop); });
}
function libRenderSelTagPop(pop) {
  if (!pop) return;
  const mode = lib.selTagMode === "remove" ? "remove" : "add", n = lib.sel.size;
  const hint = libEl("libSelTagHint");
  if (hint) hint.textContent = mode === "add"
    ? "点一下选中要添加的标签，可多选。加上去是并集，不会动这些条目原有的标签。"
    : "点一下选中要摘掉的标签，可多选。只从已选的 " + n + " 项上摘，其它条目不受影响。";
  libSelTagSlot(pop).innerHTML = (lib.tags && lib.tags.length)
    ? lib.tags.map((tg) => {
        const on = lib.selTagIds.has(Number(tg.id));
        return '<span class="lib-chip" role="button" tabindex="0" aria-pressed="' + (on ? "true" : "false") + '" data-lib-sel-tag="' + esc(String(tg.id)) + '"' +
          (tg.color ? ' style="border-color:' + esc(tg.color) + '"' : "") + ' title="点击' + (on ? "取消勾选" : "勾选") + '">' +
          esc(tg.name) + (tg.count != null ? " · " + tg.count : "") + "</span>";
      }).join("")
    : '<span class="dt-dim">还没有标签：先在单条详情的「打标签」里新建一个</span>';
  const apply = libEl("libSelTagApply");
  if (apply) { apply.textContent = mode === "add" ? ("添加到已选 " + n + " 项") : ("从已选 " + n + " 项摘除"); apply.disabled = !lib.selTagIds.size; }
  const cnt = libEl("libSelTagCount");
  if (cnt) cnt.textContent = lib.selTagIds.size ? ("已勾 " + lib.selTagIds.size + " 个标签") : "未勾标签";
}
function libRunSelTagBatch() {
  const tagIds = Array.from(lib.selTagIds).map(Number).filter((n) => n > 0);
  if (!tagIds.length) { setStatus("先在浮层里勾一个标签"); return; }
  const mode = lib.selTagMode === "remove" ? "remove" : "add";
  libHidePopovers();
  return libBatchTags(mode, tagIds);
}
// 选择行 / 操作条 / 标签浮层上所有 data-lib-act^="sel-" 的按钮都汇到这里
function libSelBarAction(act, btn) {
  if (act === "sel-all-page") return libSelAllPage();
  if (act === "sel-all-filter") return libSelAllFilter();
  if (act === "sel-clear") return libSelClear();
  if (act === "sel-star") return libBatchStar(true);
  if (act === "sel-unstar") return libBatchStar(false);
  if (act === "sel-tag-add") return libToggleSelTagPop(btn, "add");
  if (act === "sel-tag-remove") return libToggleSelTagPop(btn, "remove");
  if (act === "sel-tag-apply") return libRunSelTagBatch();
  if (act === "sel-reverse") return libBatchReverse();
  if (act === "sel-workflow") return libBatchWorkflow();
  if (act === "sel-delete") return libBatchDelete();
}

// ---------- 标注导出 / 导入（§18）----------
// 导出的档案只含「人工做过的事」：收藏 + 标签。身份键是库内相对路径 relpath（＋大小），
// 换盘符/改目录名后仍认得回来；机器解析出来的提示词与参数不进档案（它们跟着文件走，重扫即得）。
// 写文件这一步完全在前端做（Blob + a.download，与 image-viewer.js 的 #ivExport 同一条已验证写法，
// 两种界面模式都可用），不新增任何后端写文件通路。
const LIB_ANN_KIND = "cft-media-annotations";        // 后端 export_annotations() 里的 kind 字面量

// pywebview 直接给对象、浏览器模式可能给 JSON 字符串：与 lib_detail/lib_tags 同一口径兜一层
function libAnnObj(raw) {
  if (typeof raw === "string") { try { return JSON.parse(raw || "null"); } catch (e) { return null; } }
  return raw && typeof raw === "object" ? raw : null;
}
function libAnnNum(v) { const n = Number(v || 0); return isFinite(n) ? n : 0; }
// 文件名时间戳：资源库标注-20261012-2030.json（本地时区，精确到分：同一分钟里重复导出会撞名，
// 但浏览器自己会给第二个同名文件加 (1)，比在名字里塞秒更好读）
function libAnnStamp() {
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  return String(d.getFullYear()) + p(d.getMonth() + 1) + p(d.getDate()) + "-" + p(d.getHours()) + p(d.getMinutes());
}
// 两个入口按钮互斥：一次只允许一个作业在跑（导出 23ms／导入试算 38ms，但选文件与确认框会挂着人）
function libAnnBusy(busy) {
  const a = libEl("libExportAnn"), b = libEl("libImportAnn");
  if (a) a.disabled = !!busy;
  if (b) b.disabled = !!busy;
}
async function libExportAnnotations() {
  if (libEl("libExportAnn") && libEl("libExportAnn").disabled) return;
  libAnnBusy(true);
  let res = null;
  try { res = libAnnObj(await api.call("lib_export_annotations")); } catch (e) { res = null; }
  finally { libAnnBusy(false); }
  if (!res) { setStatus("导出标注失败：后端没有返回结果（请确认程序已更新到含标注导出的版本）"); return; }
  if (!res.ok) { setStatus("导出标注失败：" + ((res && res.msg) || "后端返回 ok=false")); return; }
  const count = libAnnNum(res.count);
  if (count <= 0) { setStatus("还没有收藏或标签可导出：先在卡片或详情里点「收藏」、打标签，再来导出"); return; }
  const data = libAnnObj(res.data);        // data 是完整档案对象；异常就当没拿到，不发空文件
  if (!data || !Array.isArray(data.items)) { setStatus("导出标注失败：后端没有回档案内容（data 缺失）"); return; }
  const name = "资源库标注-" + libAnnStamp() + ".json";
  try {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  } catch (e) { setStatus("导出标注失败：浏览器不允许下载（" + ((e && e.message) || "Blob 或下载被拦截") + "）"); return; }
  setStatus("已导出 " + count + " 条标注 → " + name + "（只含收藏与标签，不含机器解析的元数据）");
}
// 坏档案当场报错：不是 JSON / 顶层不是对象 / kind 不对 / 缺 items，都说清是哪一步、为什么。
// 后端 import_annotations 也会再判一遍（两条口径一致），这里先拦是为了不把一个明显不能用的文件发过去。
function libAnnCheck(text) {
  let payload = null;
  try { payload = JSON.parse(String(text || "")); }
  catch (e) { return "这个文件不是合法的 JSON（" + ((e && e.message) || "解析失败") + "），多半选错了文件或文件被截断"; }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return "档案顶层不是对象（正常导出的最外层是 {kind, version, items…}）";
  if (payload.kind !== LIB_ANN_KIND) return "档案 kind 是「" + (payload.kind == null ? "（缺失）" : String(payload.kind)) + "」，不是资源库标注导出（" + LIB_ANN_KIND + "）";
  if (!Array.isArray(payload.items)) return "档案缺少 items 条目列表，读不出要导入的标注";
  if (!payload.items.length) return "档案里 0 条标注（items 是空列表），没有可导入的东西";
  return "";
}
function libAnnReadFile(file) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result == null ? "" : fr.result));
    fr.onerror = () => reject(new Error("文件读不出来（可能被别的程序占用，或没有读取权限）"));
    fr.readAsText(file, "utf-8");
  });
}
function libPickImportFile() {
  if (libEl("libImportAnn") && libEl("libImportAnn").disabled) return;
  const input = libEl("libImportFile");
  if (!input) { setStatus("导入标注不可用：页面上没有选文件控件（#libImportFile 未就位）"); return; }
  input.value = "";                       // 允许连着两次选同一个文件：change 只在值变化时发
  input.click();
}
function libAnnCountRow(label, value, hint) {
  return '<div class="lib-ann-row"><span class="lib-ann-k">' + esc(label) + '</span><b class="lib-ann-v">' + libAnnNum(value) +
    '</b><span class="lib-ann-h">' + esc(hint) + "</span></div>";
}
// 六个计数原样摆出来（含认不出的数量），外加「只补不删」这条口径 —— 全部是后端回的真实数字，不编统计。
function libAnnPreviewHtml(fileName, s) {
  const rows = [
    libAnnCountRow("匹配到", s.matched, "档案里的条目在当前库里认得了（按相对路径＋文件大小）"),
    libAnnCountRow("要收藏", s.starred, "其中会标成收藏的条目数"),
    libAnnCountRow("标签关联", s.tags_applied, "要往条目上挂的标签次数（一个条目可带多个标签）"),
    libAnnCountRow("新建标签", s.tags_created, "库里还没有、导入时会按名字创建的标签数"),
    libAnnCountRow("认不出", s.unmatched, "当前库里没有这些相对路径：文件被移走/改名，或档案来自另一套目录"),
    libAnnCountRow("有歧义", s.ambiguous, "同一个相对路径对上多个条目（重名或改过大小），这次不动它们"),
  ].join("");
  const skipped = libAnnNum(s.unmatched) + libAnnNum(s.ambiguous);
  return '<p class="lib-ann-file">档案：<b>' + esc(String(fileName || "")) + "</b>　<span class=\"lib-ann-note\">下面是试算结果，还没有写进库</span></p>" +
    '<div class="lib-ann-rows">' + rows + "</div>" +
    '<p class="lib-ann-rule">导入只新增收藏与标签，不会取消或删除你已有的任何标注；同名标签按名字合并。</p>' +
    (skipped > 0 ? '<p class="lib-ann-skip">其中 ' + skipped + " 项认不出或有歧义，会被原样跳过（只计数、不改动），档案里其余部分照常导入。</p>" : "") +
    '<p class="lib-ann-skip">点「确定」才会真写进库；想先看看就点「取消」，库里一切不动。</p>';
}
function libAnnSummary(prefix, r) {
  return prefix + "：匹配 " + libAnnNum(r.matched) + " 项，收藏 " + libAnnNum(r.starred) + " 项，标签关联 " + libAnnNum(r.tags_applied) +
    " 次，新建标签 " + libAnnNum(r.tags_created) + " 个，认不出 " + libAnnNum(r.unmatched) + " 项，有歧义 " + libAnnNum(r.ambiguous) + " 项";
}
async function libOnImportFileChosen(ev) {
  const input = ev && ev.target ? ev.target : null;
  const file = input && input.files && input.files[0] ? input.files[0] : null;
  if (!file) return;                      // 系统文件框里按取消：change 带着空列表过来，什么都不做
  if (input) input.value = "";            // 先释放选择，后面无论走到哪一步都不会留着上次挑的文件
  libAnnBusy(true);
  let text = "", readErr = "";
  try { text = await libAnnReadFile(file); } catch (e) { readErr = (e && e.message) || "文件读不出来"; }
  if (readErr) { libAnnBusy(false); setStatus("导入标注失败：" + readErr); return; }
  const bad = libAnnCheck(text);
  if (bad) { libAnnBusy(false); setStatus("导入标注失败：" + bad + "。库里的收藏与标签没有被动过"); return; }
  // 第一趟：dry_run 试算。后端的试算计数与真写口径一致（实测两边数字相同），所以预览不是猜。
  let trial = null;
  try { trial = libAnnObj(await api.call("lib_import_annotations", text, true)); } catch (e) { trial = null; }
  if (!trial) { libAnnBusy(false); setStatus("导入标注失败：试算没有返回结果（后端未就绪），没有写入任何数据"); return; }
  libAnnBusy(false);
  if (!trial.ok) { setStatus("导入标注失败：" + ((trial && trial.msg) || "后端不认这份档案") + "。没有写入任何数据"); return; }
  if (trial.dry_run === false) { setStatus("导入标注中止：后端回的不是试算结果（dry_run=false），没有继续"); return; }
  const ok = await confirmBoxRaw(libAnnPreviewHtml(file.name, trial), "导入资源库标注（共 " + libAnnNum(trial.matched) + " 项匹配）");
  if (!ok || !ok.root) { setStatus("已取消导入，库里一切未改（刚才只是试算）"); return; }
  ok.root.remove();                       // confirmBoxRaw 确定时只摘遮罩，内容框要调用方自己收
  libAnnBusy(true);
  let res = null;
  try { res = libAnnObj(await api.call("lib_import_annotations", text, false)); } catch (e) { res = null; }
  finally { libAnnBusy(false); }
  if (!res) { setStatus("导入标注失败：写入没有返回结果，请刷新列表后核对（可能已部分写入）"); return; }
  if (!res.ok) { setStatus("导入标注失败：" + ((res && res.msg) || "后端返回 ok=false")); return; }
  setStatus(libAnnSummary("导入完成", res) + "；只新增收藏与标签，已有标注未取消");
  // 收尾走既有路径：列表 + 分面 + 统计（libLoad(true)）、标签下拉（libEnsureTags → libAfterTagChange）
  return libEnsureTags().then(() => { libAfterTagChange(); return libLoad(true); });
}

// ---------- 缩略图与视频海报 ----------
function libApplyCachedThumbs() {
  const grid = libEl("libGrid"); if (!grid) return;
  grid.querySelectorAll('img.ms-img[data-path]').forEach((img) => {
    const du = lib.thumbCache.get(img.dataset.path);
    if (!du || img.src === du) return;
    const w = img.closest(".ms-img-wrap");
    img.onload = () => { if (w) w.dataset.ph = "ok"; };
    img.onerror = () => { if (w) w.dataset.ph = "fail"; };
    img.src = du;
  });
}
function libPrefetchThumbs() { setTimeout(libThumbPass, 0); }
async function libThumbPass() {
  if (lib.thumbBusy) return;
  const grid = libEl("libGrid"); if (!grid) return;
  const token = lib.request;
  const pending = Array.from(grid.querySelectorAll('img.ms-img[data-path]')).filter((img) => {
    const w = img.closest(".ms-img-wrap");
    return w && w.dataset.ph === "loading" && !lib.thumbCache.has(img.dataset.path);
  });
  if (!pending.length) {
    // 收尾：真正没图的标"暂无封面"（保住卡片高度），只是 onload 还没回来的不动，别把刚要显示的图标成空
    grid.querySelectorAll('.ms-img-wrap[data-ph="loading"]').forEach((w) => {
      const img = w.querySelector("img.ms-img");
      if (img && img.getAttribute("src")) return;
      w.dataset.ph = "none";
    });
    return;
  }
  lib.thumbBusy = true;
  const paths = Array.from(new Set(pending.map((i) => i.dataset.path))).slice(0, 40);   // §7：缩略图批 40
  let ok = true, map = null;
  try {
    const _dpr = Math.max(1, window.devicePixelRatio || 1);
    const raw = await api.call("lib_get_thumbs", JSON.stringify(paths), Math.min(960, Math.round(320 * _dpr)));
    map = typeof raw === "string" ? JSON.parse(raw || "{}") : (raw || {});
  } catch (e) { ok = false; }
  lib.thumbBusy = false;
  if (token !== lib.request) return;                 // 已换筛选/已重置：这一批的结果不要了
  if (ok && map) {
    if (lib.thumbCache.size > 4000) lib.thumbCache.clear();
    // A3 的 lib_get_thumbs 已带 data: 前缀（图片走 _cover_thumb、视频读海报缓存）；仍兼容裸 base64
    for (const [p, b64] of Object.entries(map)) { const du = libThumbUri(b64); if (du) lib.thumbCache.set(p, du); }
    libApplyCachedThumbs();
    paths.filter((p) => !map[p]).forEach((p) => libMarkThumbWrap(p, "none"));
  } else {
    paths.forEach((p) => libMarkThumbWrap(p, "fail"));   // 拉取失败：与模型库同一「加载失败」占位语义
    return;
  }
  setTimeout(libThumbPass, 30);                      // 继续下一批（分页后卡片可能还很多）
}
function libMarkThumbWrap(path, ph) {
  const grid = libEl("libGrid"); if (!grid) return;
  grid.querySelectorAll('img.ms-img[data-path="' + CSS.escape(path) + '"]').forEach((img) => {
    const w = img.closest(".ms-img-wrap"); if (w && w.dataset.ph === "loading") w.dataset.ph = ph;
  });
}
// ---------- 视频懒挂载与海报抓帧 ----------
// 单 exe 打包不内置 ffmpeg，后端无法解码视频首帧，因此由前端 <video> 解码后离屏 canvas 抓一帧
// 回传 lib_save_poster 缓存；失败（画布被跨域污染 / 解码不出来）就静默留着 <video>。
// 挂载与抓帧绑在一起：卡片不在近区就既不挂 src 也不记「已试过」，回到近区再补。
const LIB_VID_MARGIN = "150% 0px";     // 视口上下各 1.5 屏：滚到哪解到哪，不会一次积压几百路
const LIB_VID_FALLBACK = 12;           // 引擎没有 IntersectionObserver 时只挂前 12 个，宁少不炸
function libVidObserver() {
  if (lib.vObs) return lib.vObs;                    // 全页共用一个观察器：切页只 disconnect，回来复用同一实例
  if (typeof IntersectionObserver !== "function") return null;
  const root = libEl("libViewport");
  lib.vObs = new IntersectionObserver((entries) => {
    for (const en of entries) { if (en.isIntersecting) libHydrateVideo(en.target); else libReleaseVideo(en.target); }
  }, { root: root || null, rootMargin: LIB_VID_MARGIN, threshold: 0 });
  return lib.vObs;
}
// 渲染 / 追加卡片后唯一的登记入口：只交给观察器判断，不在这里挂 src（observe 会立刻回调一次）
function libHookCardVideos(root) {
  if (!root) return;
  const vids = Array.from(root.querySelectorAll('video.lib-vid[data-src]'));
  if (!vids.length) return;
  const obs = libVidObserver();
  if (!obs) { vids.slice(0, LIB_VID_FALLBACK).forEach(libHydrateVideo); return; }
  vids.forEach((v) => obs.observe(v));
}
function libHydrateVideo(v) {
  if (!v || !v.dataset.src || v.dataset.libLive === "1") return;
  v.dataset.libLive = "1";
  v.src = v.dataset.src;                            // 地址建卡时写进 data-src，这里只赋值，不再解析标记
  const card = v.closest(".lib-card");
  const item = card ? libRowById(card.dataset.id) : null;
  // 播放/暂停交给文档级 mouseover/mouseout 委托（与模型库瀑布流同一手法），这里只管挂流和抓帧
  if (item && item.kind === "video" && !item.thumb) v._libPosterCancel = libCapturePoster(v, item);
}
// 离开近区：停解码、摘 src，把解码器和那一路 /api/media 流交出去；滚回来观察器会再挂上
function libReleaseVideo(v) {
  if (!v || v.dataset.libLive !== "1") return;
  v.dataset.libLive = "0";
  if (v._libPosterCancel) {
    const cancel = v._libPosterCancel; v._libPosterCancel = null;
    cancel();                                       // 本次抓帧随卡片一起作废：path 不能记成「已试过」
  }
  try { v.pause(); } catch (e) { }
  v.removeAttribute("src");
  try { v.load(); } catch (e) { }
}
// 视频海报抓帧：readyState 到 2（首帧可用）才画 canvas，长边 640 / JPEG q=0.82 回传 lib_save_poster
// 会话级上限：thumbCache 在 libThumbPass 里也是超 4000 就整桶换水，这里同一把尺子，
// 免得一次不切页的长滚动把 posterTried 堆到 10 万条（换水只会让旧 path 允许再试一次）。
function libPosterTriedAdd(path) {
  if (lib.posterTried.size > 4000) lib.posterTried.clear();
  lib.posterTried.add(path);
}
function libCapturePoster(video, item) {
  if (lib.posterTried.has(item.path)) return null;
  libPosterTriedAdd(item.path);                       // 同一 path 会话内只回传一次
  let done = false, timer = 0;
  const finish = (keepTried) => {
    if (done) return;
    done = true;
    clearTimeout(timer);
    const idx = lib.posterTimers.indexOf(timer);
    if (idx >= 0) lib.posterTimers.splice(idx, 1);
    video.removeEventListener("loadeddata", onData);
    video.removeEventListener("error", onError);
    if (keepTried === false) lib.posterTried.delete(item.path);   // 只有「卡片被卸载」才退回未试过
  };
  const grab = () => {
    if (done) return;
    finish(true);
    try {
      const vw = Number(video.videoWidth || 0), vh = Number(video.videoHeight || 0);
      if (!vw || !vh) return;
      const scale = Math.min(1, 640 / Math.max(vw, vh));             // §7：回传最长边 640
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(vw * scale));
      canvas.height = Math.max(1, Math.round(vh * scale));
      const ctx = canvas.getContext("2d");
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      if (_canvasFlat(ctx, canvas.width, canvas.height)) { lib.posterTried.delete(item.path); return; }
      // 与模型库抓帧同一判据：解出画面之前的纯黑画布不回传，退回「未试过」等播起来再抓
      const du = canvas.toDataURL("image/jpeg", 0.82);               // §7：JPEG q=0.82
      const b64 = du.slice(du.indexOf(",") + 1);
      if (!b64 || b64.length * 0.75 > 2 * 1024 * 1024) return;       // §7：≤2MB
      api.call("lib_save_poster", item.path, b64).then((r) => {
        if (r && r.ok) { lib.thumbCache.set(item.path, du); item.thumb = true; }
      }).catch(() => { });
    } catch (e) { /* 跨域画布被污染或解码失败：静默跳过，卡片仍是 <video> */ }
  };
  function onData() { grab(); }
  function onError() { finish(true); }
  timer = setTimeout(() => finish(true), 8000);      // 解码超时：放弃抓帧
  lib.posterTimers.push(timer);
  video.addEventListener("loadeddata", onData);
  video.addEventListener("error", onError);
  if (Number(video.readyState || 0) >= 2) grab();    // 挂监听前首帧已解好（本地 file:// 常发生）
  return () => finish(false);                        // 交给 libReleaseVideo 的撤销句柄
}

// ---------- 分面下拉 ----------
function libFillSelect(id, rows, allLabel, valueOf, labelOf) {
  const el = libEl(id); if (!el) return;
  const sig = rows.map((r) => valueOf(r)).join("|");
  if (el._sig === sig) return;            // 与模型库同一手法：签名不变就不重填（重填会把选中项冲掉）
  el._sig = sig;
  const cur = el.value;
  el.innerHTML = '<option value="">' + esc(allLabel) + "</option>" +
    rows.map((r) => '<option value="' + esc(String(valueOf(r))) + '">' + esc(String(labelOf(r))) + "</option>").join("");
  el.value = cur;
  if (cur && el.value !== cur && lib.filters) libPruneFacet(id);   // 选项已消失：同步清掉对应筛选，别筛出"莫名空列表"
}
function libPruneFacet(id) {
  const f = lib.filters;
  if (id === "libCkF") f.checkpoints = [];
  else if (id === "libLoraF") f.loras = [];
  else if (id === "libTagF") f.tag_ids = [];
  else if (id === "libDirF" && f.dirs.length === 1) f.dirs = [];
}
function libFillFacets() {
  const f = lib.facets || {};
  libFillSelect("libCkF", Array.isArray(f.checkpoints) ? f.checkpoints.slice(0, 300) : [], "全部底模", (r) => r.name, (r) => r.name + "（" + r.count + "）");
  libFillSelect("libLoraF", Array.isArray(f.loras) ? f.loras.slice(0, 300) : [], "全部 LoRA", (r) => r.name, (r) => r.name + "（" + r.count + "）");
  libFillSelect("libDirF", Array.isArray(f.dirs) ? f.dirs.slice(0, 300) : [], "全部文件夹", (r) => String(r.path != null ? r.path : r.name), (r) => short(String(r.name || r.path || ""), 26) + "（" + r.count + "）");
  libFillSelect("libTagF", Array.isArray(f.tags) ? f.tags.slice(0, 300) : [], "全部标签", (r) => String(r.id), (r) => r.name + "（" + r.count + "）");
  libFillChipFacets();
  if (lib.foldersOpen) libRenderFolders();
}

// 展开控件要不要挂靠真实布局测量决定，前提是「测量时这一组是可见且有高度的」：
// 进页（刚从隐藏切成可见）和窗口改宽度（chip 换行位置变了）都要重量一次，否则判断会停在旧布局上。
function libFacetRemeasure() {
  for (const g of LIB_CHIP_FACETS) { const s = libChipFacetSlot(g.id); if (s) s._sig = null; }
  libFillChipFacets();
}
let libFacetResizeTimer = 0;
window.addEventListener("resize", () => {
  clearTimeout(libFacetResizeTimer);
  libFacetResizeTimer = setTimeout(() => {
    const p = document.getElementById("page-library");
    if (p && p.classList.contains("active")) libFacetRemeasure();
  }, 220);
});

// ---------- 分面 chip 组（采样器 / 调度器 / 尺寸：同维度多选＝OR，跨维度＝AND）----------
// 三组的 facets 形状与 checkpoints 完全一致（§15.2.5：[{"name","count"}]，count DESC、上限 300），
// 所以只认 name；key 必须与服务端 flt 的键名逐字相同（lib.filters 用的就是这些键，改名要一起改）。
const LIB_CHIP_FACETS = [
  { id: "libSamplerFacet", key: "samplers", title: "采样器" },
  { id: "libSchedulerFacet", key: "schedulers", title: "调度器" },
  { id: "libSizeFacet", key: "sizes", title: "尺寸" },
];
const LIB_FACET_LIMIT = 300;                    // §7：facet 上限 300 项，超出按 count 截断（服务端已经截，这里再兜一层）
function libChipFacetSlot(id) {
  const box = libEl(id); if (!box) return null;
  return box.querySelector("[data-lib-slot='facet-opts']") || box;
}
function libFacetChipHtml(g, name, count, ghost) {
  const picked = (lib.filters[g.key] || []).indexOf(name) >= 0;
  const label = esc(name) + (count == null ? "" : "（" + esc(String(count)) + "）");
  const tip = ghost ? name + "：这一项已经不在当前分面计数里（多半是重扫后没有文件再带它），点 × 取消筛选"
    : (picked ? "已选中，点一下取消" + g.title + "筛选" : "点一下按" + g.title + "筛选");
  return '<span class="lib-chip lib-facet-chip' + (ghost ? " is-ghost" : "") + '" role="button" tabindex="0" aria-pressed="' + (picked ? "true" : "false") + '"' +
    ' data-lib-facet="' + esc(g.key) + '" data-lib-facetval="' + esc(name) + '" title="' + esc(tip) + '">' + label +
    (picked ? '<button class="lib-chip-x" type="button" data-lib-facetdel="' + esc(g.key) + '" data-lib-facetval="' + esc(name) + '" aria-label="移除' + esc(g.title + "筛选：" + name) + '">×</button>' : "") +
    "</span>";
}
function libFacetMoreEl(g, total, open) {
  const el = document.createElement("span");
  el.className = "lib-chip lib-facet-more";
  el.setAttribute("role", "button");
  el.tabIndex = 0;
  el.dataset.libFacetmore = g.key;
  el.textContent = open ? "收起" : "展开（共 " + total + " 项）";
  el.title = open ? "已经展开（高度按窗口给上限，没看全就在组里滚动），点一下收回两行"
    : "共 " + total + " 个" + g.title + "值没露全，展开后一次能多看到一批（也可以直接在组里滚动）";
  return el;
}
function libToggleFacetMore(key) {
  const g = LIB_CHIP_FACETS.find((x) => x.key === key); if (!g) return;
  const slot = libChipFacetSlot(g.id); if (!slot) return;
  slot._open = !slot._open;
  slot._sig = null;                                     // 展开态变了必须重画，否则点一下没反应
  slot.scrollTop = 0;
  libFillChipFacets();
}
function libFillChipFacets() {
  const f = lib.facets || {};
  for (const g of LIB_CHIP_FACETS) {
    const slot = libChipFacetSlot(g.id); if (!slot) continue;
    const host = slot.parentElement || slot;
    const act = document.activeElement;
    // 重画会把焦点丢掉（元素被整个替换），键盘用户按一次「展开」就得从头 Tab。记下焦点落在哪个控件上，画完接回去。
    let focusKey = "";
    if (act && host.contains(act)) {
      if (act.dataset && act.dataset.libFacetmore === g.key) focusKey = "more";
      else if (act.dataset && act.dataset.libFacetval) focusKey = "v" + act.dataset.libFacetval;
    }
    const rows = Array.isArray(f[g.key]) ? f[g.key].slice(0, LIB_FACET_LIMIT) : [];
    const names = [], counts = Object.create(null);
    for (const r of rows) {
      const n = String((r && r.name != null) ? r.name : "");
      if (!n || counts[n] != null) continue;              // 后端已按 count 降序，重名只留第一条
      counts[n] = Number(r.count || 0); names.push(n);
    }
    const chosen = Array.isArray(lib.filters[g.key]) ? lib.filters[g.key] : [];
    // 选中但本轮分面里已经没有的值要继续画出来（标成 ghost）：否则用户会卡在「筛出 0 项、又没地方取消」的死角。
    // 下拉那套可以靠 libPruneFacet 静默清掉（它有「全部」这个天然回落值），chip 组没有，所以这里不裁剪。
    const ghosts = chosen.filter((v) => names.indexOf(v) < 0);
    const total = names.length + ghosts.length;
    const sig = names.map((n) => n + ":" + counts[n]).join("|") + "#" + chosen.slice().sort().join("|") +
      "#" + (slot._open ? "1" : "0");
    if (slot._sig === sig) continue;                       // 签名不变不重画：重画会丢滚动位置和键盘焦点
    slot._sig = sig;
    const keepTop = slot._open ? 0 : slot.scrollTop;
    slot.classList.toggle("is-open", !!slot._open);
    slot.innerHTML = total
      ? names.map((n) => libFacetChipHtml(g, n, counts[n], false)).join("") + ghosts.map((n) => libFacetChipHtml(g, n, null, true)).join("")
      // 空维度只占位、不隐藏整组：D1/D2 落地前后布局不跳（§15.3.1）
      : '<span class="lib-facet-empty" title="' + esc("扫到带" + g.title + "的条目后就能点选筛选") + '">暂无数据</span>';
    slot.scrollTop = keepTop;
    // 展开/收起控件挂在「组」上而不是滚动容器里：塞在容器内它自己就正好落在折行以下，等于看不见。
    // 两行封顶时组内滚动条也很难被察觉（真实窗口 1266px 实测：尺寸组一屏只露 4 个值），
    // 所以先量一次真实溢出，只有真的没露全才挂这个控件。
    const oldMore = host.querySelector(":scope > [data-lib-facetmore]");
    if (oldMore) oldMore.remove();
    if (slot._open || (total && slot.scrollHeight > slot.clientHeight + 2)) host.appendChild(libFacetMoreEl(g, total, !!slot._open));
    if (focusKey === "more") { const m = host.querySelector(":scope > [data-lib-facetmore]"); if (m) m.focus(); }
    else if (focusKey) { const c = slot.querySelector('[data-lib-facetval="' + CSS.escape(focusKey.slice(1)) + '"]'); if (c) c.focus(); }
  }
}
function libToggleFacetChip(key, name, on) {
  const arr = lib.filters[key];
  if (!Array.isArray(arr) || !name) return;
  const i = arr.indexOf(name);
  const want = on === undefined ? i < 0 : !!on;           // chip 本体＝切换，× ＝强制取消选中
  if (want) { if (i >= 0) return; arr.push(name); }
  else { if (i < 0) return; arr.splice(i, 1); }
  libFillChipFacets();                                     // 先按新状态重画（含 × 的出现/消失）
  libOnFilterChange();                                     // 再走既有的「筛选变化后刷新」路径：libLoad(true) → 统计条「筛选 N 项 / 共 M 项」
}
// 标签增删改之后筛选下拉必须自己跟上：facets 只在 lib_list 时回来，不重开页面就一直是旧列表。
// lib_tags 是权威全量（含 count），读过就用它，没读过拿 facets.tags 兜底；libFillSelect 会保住当前选中项。
function libRefreshTagFilter() {
  const el = libEl("libTagF"); if (!el) return;
  const facetTags = (lib.facets && Array.isArray(lib.facets.tags)) ? lib.facets.tags : [];
  const rows = lib.tagsLoaded ? lib.tags : facetTags;   // 删掉最后一个标签时 lib.tags 是空数组，不能当「没读过」
  el._sig = null;                                  // 强制重填：id 没变但名称/计数可能已经变了
  libFillSelect("libTagF", rows.slice(0, 300), "全部标签", (r) => String(r.id), (r) => String(r.name || "") + (r.count != null ? "（" + r.count + "）" : ""));
}
// 标签数据变更后的统一收尾（新建 / 增删绑定 / 将来删除标签都走这里）：刷新下拉 + 重画浮层
function libAfterTagChange() {
  libRefreshTagFilter();
  const pop = libEl("libTagPop");
  if (pop && !pop.hidden) libRenderTagPop(pop);
}

// ---------- 目录面板（无独立 RPC，分面 dirs 已在 lib_list 里返回，按路径段拼树） ----------
function libDirTree() {
  const dirs = (lib.facets && Array.isArray(lib.facets.dirs)) ? lib.facets.dirs : [];
  const root = { children: {} };
  for (const d of dirs) {
    const p = String(d.path != null ? d.path : (d.name || "")).replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
    let node = root, acc = "";
    if (!p) { root.count = Number(d.count || 0); continue; }
    for (const seg of p.split("/")) {
      acc = acc ? acc + "/" + seg : seg;
      node.children[seg] = node.children[seg] || { name: seg, path: acc, children: {}, count: 0 };
      node = node.children[seg];
    }
    node.count = Number(d.count || 0);
  }
  const out = [];
  (function walk(node, depth) {
    Object.keys(node.children).sort((a, b) => a.localeCompare(b, "zh")).forEach((k) => {
      const n = node.children[k]; n.depth = depth; out.push(n); walk(n, depth + 1);
    });
  })(root, 0);
  return out;
}
function libRenderFolders() {
  const panel = libEl("libFoldersPanel"); if (!panel) return;
  const rows = libDirTree();
  if (!rows.length) { panel.innerHTML = '<div class="fm-title">暂无文件夹：先扫描，或检查资源目录配置</div>'; return; }
  panel.innerHTML = '<div class="fm-title">资源库文件夹（点击筛选，不移动也不删除文件）</div>' +
    '<div class="fm-toolbar"><button class="btn btn-tiny" data-lib-act="dirs-none" type="button">全部文件夹</button></div>' +
    rows.map((n) => {
      const on = !lib.filters.dirs.length || lib.filters.dirs.indexOf(n.path) >= 0;
      return '<div class="fm-item" role="checkbox" tabindex="0" aria-checked="' + (lib.filters.dirs.indexOf(n.path) >= 0) + '" data-lib-dir="' + esc(n.path) + '" style="padding-left:' + (12 + n.depth * 18) + 'px">' +
        '<input type="checkbox" tabindex="-1" aria-hidden="true"' + (lib.filters.dirs.indexOf(n.path) >= 0 ? " checked" : "") + "/>" + _icon("folder") +
        '<span class="fm-name">' + esc(n.name) + "</span>" +
        '<span class="fm-state ' + (on ? "on" : "off") + '">' + (n.count > 0 ? n.count + " 项" : "—") + "</span></div>";
    }).join("");
}
function libToggleDir(path) {
  const cur = lib.filters.dirs;
  const i = cur.indexOf(path);
  if (i >= 0) cur.splice(i, 1); else cur.push(path);
  lib._dirsFromPanel = cur.slice();
  const sel = libEl("libDirF");
  if (sel) { sel.value = cur.length === 1 ? cur[0] : ""; }
  libRenderFolders();
  libOnFilterChange();
}

// ---------- 标签浮层 ----------
function libEnsureTags() {
  return api.call("lib_tags").then((raw) => {
    const list = typeof raw === "string" ? JSON.parse(raw || "[]") : (raw || []);
    lib.tags = Array.isArray(list) ? list : [];
    lib.tagsLoaded = true;
  }).catch(() => { lib.tags = []; lib.tagsLoaded = false; });
}
function libOpenTagPop(anchor) {
  const pop = libEl("libTagPop");
  if (!pop || !lib.detail) return;
  lib.popKind = "tags";
  libPopShow(pop, anchor);
  libTagSlot(pop).innerHTML = '<span class="dt-dim">读取中…</span>';
  libEnsureTags().then(() => { if (lib.popKind === "tags" && !pop.hidden) libRenderTagPop(pop); });
}
function libTagSlot(pop) { return pop.querySelector("[data-lib-slot='chips']") || pop; }   // A4 的浮层留了 chip 插槽
function libRenderTagPop(pop) {
  const d = lib.detail; if (!d) return;
  const chosen = new Set((Array.isArray(d.tags) ? d.tags : []).map((t) => Number(t.id)));
  libTagSlot(pop).innerHTML =
    (lib.tags.length ? lib.tags.map((t) => {
      const on = chosen.has(Number(t.id));
      return '<span class="lib-chip" role="button" tabindex="0" aria-pressed="' + (on ? "true" : "false") + '" data-lib-tag="' + esc(String(t.id)) + '"' +
        (t.color ? ' style="border-color:' + esc(t.color) + '"' : "") + ' title="点击' + (on ? "移除" : "添加") + '">' + esc(t.name) + (t.count != null ? " · " + t.count : "") +
        '<button class="lib-chip-x" type="button" data-lib-tagdel="' + esc(String(t.id)) + '" data-lib-tagname="' + esc(t.name) + '" data-lib-tagcount="' + esc(String(t.count == null ? 0 : t.count)) + '" aria-label="删除标签 ' + esc(t.name) + '">×</button></span>';
    }).join("") : '<span class="dt-dim">还没有标签，先新建一个</span>') +
    '<div class="lib-chips" style="width:100%"><input class="input" data-lib-newtag placeholder="新标签名" style="flex:1;min-width:0"/>' +
    '<button class="btn btn-tiny" data-lib-act="tag-add" type="button">新建</button></div>';
}
function libSaveTags(ids) {
  const d = lib.detail; if (!d) return Promise.resolve();
  return api.call("lib_set_tags", Number(d.id), JSON.stringify(ids)).then((r) => {
    if (!r || !r.ok) { setStatus((r && r.msg) || "标签保存失败"); return; }
    d.tags = Array.isArray(r.tags) ? r.tags : [];
    const row = libRowById(d.id); if (row) row.tags = d.tags;
    libRenderGrid(); libUpdateStats();
    // 打完标签后 #libTagF 不能还停在「全部标签」：重读标签列表（带新计数）再刷新下拉与浮层
    libEnsureTags().then(libAfterTagChange);
  }).catch(() => setStatus("标签保存失败"));
}
function libAddTag(name) {
  return api.call("lib_tag_save", 0, name, "", -1).then((r) => {
    if (!r || !r.ok) { setStatus((r && r.msg) || "新建标签失败"); return; }
    return libEnsureTags();
  });
}

async function libDeleteTag(id, name, count) {
  if (!id) return;
  const used = count ? "它会从 " + count + " 个条目上移除。" : "它还没被用到。";
  if (typeof confirmBox !== "function") { setStatus("确认框不可用，已取消删除标签"); return; }  // 不退回原生 confirm：那会卡死 AI
  const ok = await confirmBox("删除标签「" + (name || "") + "」？" + used + "（不动文件本身）");
  if (!ok) return;
  const wasFiltered = String(id) === String(libVal("libTagF"));
  try {
    const r = await api.call("lib_tag_delete", id);
    if (!r || !r.ok) { setStatus((r && r.msg) || "删除标签失败"); return; }
    setStatus("已删除标签");
    if (wasFiltered) { const s = libEl("libTagF"); if (s) s.value = ""; lib.filters.tag_ids = []; }
    await libEnsureTags();
    libAfterTagChange();
    if (wasFiltered) libLoad(true);
  } catch (_) {
    setStatus("删除标签失败");
  }
}

// ---------- 详情 ----------
// 详情内翻页（← / → 与面板上的 #libDetailPrev / #libDetailNext）：
// 顺序按「当前已加载的这一批 lib.rows」走（服务端是分页的，lib.rows 就是当页已加载的段），
// 到头就停：不循环、不自动换页、也不偷偷去拉下一页——跨页翻页不在本期范围，提示里如实说明。
// 明确不接模型库的 openImageViewer：那是 Civitai 示例图语义（covers 缓存键跟 api_key 绑定、还有
// 「打开 C站原图页」），把本地资源硬塞进去只会污染它。资源库详情本身就有大图与视频内联播放，
// 翻页只做「换一件」，图片与视频都支持（libShowDetail 换内容前会 teardown 上一个 <video>）。
function libDetailIndex() {
  const d = lib.detail; if (!d) return -1;
  const k = String(d.id);
  return lib.rows.findIndex((r) => r && String(r.id) === k);
}
function libDetailNavHtml() {
  const idx = libDetailIndex(), n = lib.rows.length, has = idx >= 0;
  const pos = (has ? (idx + 1) : "—") + " / " + n;
  const tip = "这是当前页内的序号（第 " + (has ? idx + 1 : "?") + " 张 / 本批已加载 " + n + " 项，库内共 " + lib.total +
    " 项）。← → 只在这一批里前后换一件，到头就停；列表是服务端分页的，跨页翻页不在本期范围，要往后走得先滚动加载更多。";
  return '<span class="lib-dt-pos" id="libDetailPos" role="status" aria-live="polite" title="' + esc(tip) + '">' + esc(pos) + "</span>" +
    '<button class="icon-btn lib-dt-nav" id="libDetailPrev" type="button" data-lib-act="prev" aria-label="上一件资源（当前页内）" title="上一件（←）"' + (has && idx > 0 ? "" : " disabled") + ">&#8249;</button>" +
    '<button class="icon-btn lib-dt-nav" id="libDetailNext" type="button" data-lib-act="next" aria-label="下一件资源（当前页内）" title="下一件（→）"' + (has && idx < n - 1 ? "" : " disabled") + ">&#8250;</button>";
}
function libDetailNav(delta) {
  if (!lib.detail) return;
  const idx = libDetailIndex(), n = lib.rows.length;
  if (idx < 0) { setStatus("详情对应的条目已不在当前列表里（列表重新加载过了）：先关掉详情，再点一次卡片"); return; }
  const to = idx + delta;
  if (to < 0) { setStatus("已经是这一批的第一张（本批 " + n + " 项 / 库内共 " + lib.total + " 项）：到头就停，不循环"); return; }
  if (to >= n) { setStatus("已经是这一批的最后一张（本批 " + n + " 项 / 库内共 " + lib.total + " 项）：跨页翻页不在本期范围，往下滚动加载更多才会有新的"); return; }
  const row = lib.rows[to];
  if (!row || row.id == null) { setStatus("翻页失败：这一条读不到 id（列表数据异常），没有改动任何东西"); return; }
  const id = String(row.id);
  // 高亮跟着详情走（lib.current 是「当前详情对应的那张卡」）。这里绝不碰 lib.sel：
  // 多选态下翻页只是换一件看，选中集合不许因此增减。
  lib.current = id;
  const grid = libEl("libGrid");
  if (grid) {
    grid.querySelectorAll(".lib-card.is-current").forEach((c) => { if (c.dataset.id !== id) c.classList.remove("is-current"); });
    const card = libCardEl(id);
    if (card) { card.classList.add("is-current"); try { card.scrollIntoView({ block: "nearest" }); } catch (e) { } }
  }
  libShowDetail(id);
}
// ← / → 的否决顺序就是事件纪律，任何一条命中就完全不接管（顺序有意：先让在跑的快捷键系统知道）
function libDetailKeyNav(e) {
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  if (e.defaultPrevented || e.ctrlKey || e.altKey || e.shiftKey || e.metaKey) return;   // 已被处理过 / 带修饰的组合键不抢
  if (!libPageActive()) return;                       // 只有资源库页活着才谈得上
  const mask = libEl("detailMask");
  if (!lib.detail || !mask || mask.style.display !== "flex") return;   // 详情没开（含 Esc 已关掉）就不接管，也不许把它重新拉开
  if (document.getElementById("imageViewer")) return;                  // 全屏查看器在＝完全不接管（它的 ←→ 是示例图语义）
  if (document.querySelector(".rd-mask, .df-mask, .settings-exit-mask")) return;  // 确认框/浮在详情之上的弹窗打开时，方向键归它
  const t = e.target;
  if (t && t.closest && t.closest('input, textarea, select, [contenteditable="true"], [contenteditable=""]')) return;  // 输入焦点不抢
  e.preventDefault();
  libDetailNav(e.key === "ArrowLeft" ? -1 : 1);
}
async function libShowDetail(id) {
  const row = libRowById(id);
  const request = ++lib.detailRequest;
  let d = null;
  try {
    const raw = await api.call("lib_detail", Number(id));
    d = typeof raw === "string" ? JSON.parse(raw || "{}") : (raw || null);
  } catch (e) { d = null; }
  if (request !== lib.detailRequest || !libPageActive()) return;
  if (!d || typeof d !== "object") { setStatus("资源详情读取失败"); return; }
  if (d.ok === false) { setStatus(d.msg || "资源详情读取失败"); return; }
  d.id = d.id || Number(id);
  if (row) {   // 详情缺播放地址时用行数据兜底，避免右侧空白
    if (!d.url_local) d.url_local = row.url_local;
    if (!d.url_http) d.url_http = row.url_http;
    if (!d.path) d.path = row.path;
  }
  lib.detail = d;
  const panel = libEl("detailPanel"), mask = libEl("detailMask");
  if (!panel || !mask) return;
  // 换内容前先把标签浮层收掉：浮层 portal 在 body 上，不主动收就会攥着上一个「打标签」按钮的
  // 锚点飘在新详情上面（翻页时按键盘最容易撞见）。单击路径里文档级委托已经收过一遍，这里是补键盘路径。
  libHidePopovers();
  libTeardownVideos(panel);
  panel.innerHTML = libDetailHtml(d);
  mask.style.display = "flex";
  // 长文本要不要挂「展开全文」得等 DOM 落地后量真实溢出，所以渲染完立刻测一次（同 libFillChipFacets 的纪律）。
  // 必须在 mask 显示之后测：详情浮层是 display:none 的，隐藏状态下 scrollHeight/clientHeight 都是 0，会误判成「没溢出」。
  libFillDescMore();
  // 正文用的是内嵌字体（@font-face），字体替换前后行高会变，临界长度的条目可能「该挂没挂」。
  // 字体就绪后按同一套判定重测一次；令牌变了说明用户已经翻页/关掉详情，就不动现在的 DOM。
  const descReq = lib.detailRequest;
  if (document.fonts && document.fonts.ready && document.fonts.ready.then) {
    document.fonts.ready.then(() => {
      if (descReq === lib.detailRequest && lib.detail && libPageActive()) libFillDescMore();
    }).catch(() => { });
  }
}
function libDetailRow(k, v, mono) {
  if (v === "" || v == null) return "";
  return '<div class="dt-mr"><span class="k">' + esc(k) + '</span><span class="v' + (mono ? " dt-mono" : "") + '" data-tip="' + esc(String(v)) + '">' + esc(String(v)) + "</span></div>";
}
// 详情里的长文本正文块（正面/负面提示词走同一个 helper）。
// .detail-desc 是模型库详情也在用的共享类（固定 180px 滚动框），不能动它的全局行为，
// 所以这里只加资源库自己的 lib-desc：折叠态约 3 行、溢出判定与控件都由 libFillDescMore 负责。
// text 来自文件内嵌元数据（外部可控），一律过 esc；emptyHtml 是本函数里写死的占位文案。
function libDetailDescHtml(field, text, emptyHtml) {
  const t = String(text == null ? "" : text);
  return '<div class="detail-desc lib-desc" data-lib-desc="' + esc(field) + '">' +
    (t ? esc(t) : (emptyHtml || "")) + "</div>";
}
function libDetailHtml(d) {
  const isVid = d.kind === "video";
  const url = libMediaUrl(d);
  // 后端 lib_detail 已带 §7 详情边长 800 的 preview（视频则是海报缓存）：先显示它，点开才加载原文件
  const preview = libThumbUri(d.preview) || lib.thumbCache.get(d.path) || "";
  const media = isVid
    ? '<video class="detail-main-img" id="dMain" controls preload="metadata" playsinline' + (preview ? ' poster="' + esc(preview) + '"' : "") + ' src="' + esc(url) + '"></video>'
    : (url ? '<img class="detail-main-img" id="dMain" alt="' + esc(d.filename || "") + '" src="' + esc(preview || url) + '" data-lib-full="' + esc(url) + '" title="点击看原图 / 还原"/>'
      : '<div class="detail-main-img dt-empty" id="dMain">' + _icon("image", "ic-lg") + "<span>文件不可读</span></div>");
  const refs = []
    .concat(Array.isArray(d.models) ? d.models : [])
    .concat(Array.isArray(d.loras) ? d.loras : [])
    .filter((v, i, arr) => v && arr.indexOf(v) === i);
  const extra = d.extra && typeof d.extra === "object" ? JSON.stringify(d.extra, null, 2) : "";
  // 提示词来源要诚实：name_fallback 是后端按字段名（prompt/text 等）回退捞出来的，
  // 不是链条解析结果。extra 里没这个字段就不标注，不编造来源说明。
  const srcHint = (d.extra && d.extra.prompt_source === "name_fallback") ? '<span class="dt-sec-hint">按字段名提取</span>' : "";
  // 生成参数独立成组：只在有值时渲染（libDetailRow 空值回 ""），七个字段全空就整组不出现——
  // 实测 532 条视频这五项全为空（§15.0），不能摆一排「—」假装有数据。
  const paramRows = [
    libDetailRow("种子", d.seed), libDetailRow("步数", d.steps), libDetailRow("CFG", d.cfg),
    libDetailRow("采样器", d.sampler), libDetailRow("调度器", d.scheduler),
    libDetailRow("帧率", libFps(d.fps)), libDetailRow("时长", libDur(d.duration)),
  ].filter(Boolean).join("");
  const paramsHint = libParamsSourceHint(d.extra);
  return '<div class="dt-head"><div class="dt-head-t">资源详情</div><div class="dt-head-r">' + libDetailNavHtml() +
    '<button class="icon-btn dt-close" data-lib-act="close" aria-label="关闭详情" title="关闭">' + _icon("x") + "</button></div></div>" +
    '<div class="dt-body"><div class="detail-left">' + media +
    (d.exists === false ? '<p class="binding-warning">文件已不在磁盘上（索引保留 30 天后清除）</p>' : "") +
    '</div><div class="detail-right">' +
    '<div class="detail-title">' + esc(d.filename || "-") + "</div>" +
    '<div class="dt-mrs">' +
      libDetailRow("类型", isVid ? "视频 " + String(d.ext || "") : "图片 " + String(d.ext || "")) +
      libDetailRow("底模", d.checkpoint) +
      libDetailRow("尺寸", d.width && d.height ? d.width + " × " + d.height : "") +
      libDetailRow("大小", fmtSize(d.size)) +
      libDetailRow("生成时间", d.mtime ? fmtTime(d.mtime) : "") +
      libDetailRow("入库时间", d.added_at ? fmtTime(d.added_at) : "") +
      libDetailRow("元数据来源", d.meta_source || (d.has_meta ? "" : "无内嵌元数据")) +
      libDetailRow("路径", d.path, true) +
    "</div>" +
    (paramRows ? '<div class="dt-sec"><div class="dt-sec-h">' + _icon("settings") + '生成参数' + paramsHint + '</div><div class="dt-mrs">' + paramRows + "</div></div>" : "") +
    (d.note ? '<div class="dt-sec"><div class="dt-sec-h">' + _icon("info") + '解析备注</div><div class="detail-desc">' + esc(d.note) + "</div></div>" : "") +
    '<div class="dt-sec"><div class="dt-sec-h">' + _icon("file") + '正面提示词' + srcHint +
      (d.prompt ? '<button class="btn dt-copy" data-lib-act="copy-prompt" type="button">' + _icon("copy") + "复制</button>" : "") +
      '</div>' + libDetailDescHtml("prompt", d.prompt, '<span class="dt-dim">无提示词（文件未内嵌元数据，或同名 .json 侧车也没有）</span>') + "</div></div>" +
    (d.negative ? '<div class="dt-sec"><div class="dt-sec-h">' + _icon("file") + '负面提示词' +
      '<button class="btn dt-copy" data-lib-act="copy-negative" type="button">' + _icon("copy") + "复制</button>" +
      '</div>' + libDetailDescHtml("negative", d.negative, "") + "</div></div>" : "") +
    (refs.length ? '<div class="dt-sec"><div class="dt-sec-h">' + _icon("layers") + '模型引用<span class="dt-sec-hint">点击跳模型库</span></div><div class="lib-chips">' +
      refs.map((m) => '<span class="lib-chip" data-lib-model="' + esc(m) + '" title="在本地模型库里定位">' + esc(m) + "</span>").join("") + "</div></div>" : "") +
    (extra ? '<div class="dt-sec"><details><summary class="dt-sec-h">' + _icon("settings") + "原始解析摘要</summary><pre class=\"detail-desc\">" + esc(extra) + "</pre></details></div>" : "") +
    '<div class="dt-sec"><div class="dt-sec-h">' + _icon("tag") + '标签</div><div class="lib-chips">' +
      ((d.tags && d.tags.length) ? d.tags.map((t) => '<span class="lib-chip"' + (t.color ? ' style="border-color:' + esc(t.color) + '"' : "") + ">" + esc(t.name) + "</span>").join("") : '<span class="dt-dim">未打标签</span>') +
      "</div>" +
      '<div class="dt-ag"><div class="dt-ag-h">操作</div><div class="dt-ag-b">' +
        '<button class="btn" data-lib-act="star" aria-pressed="' + (!!d.starred) + '" type="button">' + _icon("star") + (d.starred ? "取消收藏" : "收藏") + "</button>" +
        '<button class="btn" data-lib-act="tags" type="button">' + _icon("tag") + "打标签</button>" +
        '<button class="btn" data-lib-act="folder" type="button">' + _icon("folder") + "打开所在文件夹</button>" +
        '<button class="btn" data-lib-act="copy-path" type="button">' + _icon("copy") + "复制路径</button>" +
      "</div></div>" +
      '<div class="dt-ag"><div class="dt-ag-h">分析</div><div class="dt-ag-b">' +
        '<button class="btn" data-lib-act="workflow" type="button"' + (d.has_meta ? "" : " disabled") + ' title="' + (d.has_meta ? "把内嵌工作流送去分析" : "该文件没有内嵌元数据，无工作流可分析") + '">' + _icon("workflow") + "送工作流分析</button>" +
        (d.kind === "image" ? '<button class="btn" data-lib-act="reverse" type="button" title="按文件哈希加入反向解析列表；到反向解析页点「开始反查」才会联网查询">' + _icon("search") + "送逆向解析</button>" : "") +
      "</div></div>" +
      '<div class="dt-ag"><div class="dt-ag-h">删除</div><div class="dt-ag-b">' +
        '<button class="btn btn-danger" data-lib-act="delete" type="button"' + (d.exists === false ? " disabled" : "") + ">" + _icon("trash") + "删除文件</button>" +
      "</div></div>" +
    "</div></div></div>";
}
function libDescMoreEl(field, count, open) {
  const el = document.createElement("span");
  el.className = "lib-chip lib-desc-more";
  el.setAttribute("role", "button");
  el.tabIndex = 0;
  el.dataset.libDescmore = field;
  el.setAttribute("aria-pressed", open ? "true" : "false");
  el.textContent = open ? "收起" : "展开全文（共 " + count + " 字）";
  // 展开态有高度上限（CSS 里 min(60vh, 560px)），不保证一次全露出，title 就如实说「没看全就滚动」，
  // 不能写成「展开即全文」这种做不到的承诺。
  el.title = open ? "已展开：高度按窗口给上限，没看全就在这个块里往下滚动；点一下收回三行"
    : "共 " + count + " 字，现在只显示开头几行；展开后高度按窗口给上限，没看全就在这个块里滚动";
  return el;
}
// 量一次真实溢出再决定挂不挂控件（照 libFillChipFacets 的纪律：折行以下看不见的东西，
// 不给出「能看全」的入口就等于没数据）。控件挂在正文块的兄弟位置而不是正文块内部：
// 塞在滚动框里它自己就落在折叠线以下，等于看不见。
function libFillDescMore() {
  const panel = libEl("detailPanel"), d = lib.detail;
  if (!panel || !d) return;
  const key = String(d.id);
  // 换条目（←/→ 翻页、重开另一条）先把展开态清掉：展开只属于当前这一条，不许跟着串下去
  if (lib.descOpenId !== key) { lib.descOpen = {}; lib.descOpenId = key; }
  const openMap = lib.descOpen;
  const act = document.activeElement;
  // 重画会整个替换控件元素，键盘用户按一次「展开」就会从头 Tab；记下焦点在哪个字段上，画完接回去
  const focusKey = (act && panel.contains(act) && act.dataset && act.dataset.libDescmore) ? act.dataset.libDescmore : "";
  panel.querySelectorAll("[data-lib-desc]").forEach((el) => {
    const field = el.dataset.libDesc;
    const host = el.parentElement || el;
    const old = host.querySelector(":scope > [data-lib-descmore]");
    if (old) old.remove();
    const text = String(d[field] == null ? "" : d[field]);
    const open = !!openMap[field];
    el.classList.toggle("is-expanded", open);
    if (!text) return;                                        // 空正文（只有占位文案）不挂控件
    if (open || el.scrollHeight > el.clientHeight + 2) {
      host.appendChild(libDescMoreEl(field, text.length, open));
    }
  });
  if (focusKey) {
    const m = panel.querySelector('[data-lib-descmore="' + CSS.escape(focusKey) + '"]');
    if (m) m.focus();
  }
}
function libToggleDescMore(field) {
  const d = lib.detail; const panel = libEl("detailPanel");
  if (!d || !panel || !field) return;
  if (lib.descOpenId !== String(d.id)) { lib.descOpen = {}; lib.descOpenId = String(d.id); }
  const el = panel.querySelector('[data-lib-desc="' + CSS.escape(field) + '"]');
  if (!el) return;
  const on = !lib.descOpen[field];
  lib.descOpen[field] = on;
  if (on) el.scrollTop = 0;                                    // 展开是从头读，不该留着折叠态的滚动位置
  libFillDescMore();
}
function libCloseDetail() {
  const panel = libEl("detailPanel"), mask = libEl("detailMask");
  libTeardownVideos(panel);
  if (mask) mask.style.display = "none";
  if (panel) panel.innerHTML = "";
  lib.detail = null;
  // 关掉再重开同一条也要回到折叠态（展开是「这一次看」的临时视图，不是条目属性）
  lib.descOpen = {}; lib.descOpenId = "";
  // 详情关掉后「当前卡」的高亮也就没意义了；批量已选的 .checked 不受影响（它跨页保留）
  lib.current = null;
  const grid = libEl("libGrid");
  if (grid) grid.querySelectorAll(".lib-card.is-current").forEach((c) => c.classList.remove("is-current"));
  libHidePopovers();
}
async function libGotoModel(name) {
  let list = [];
  try {
    const raw = await api.call("workflow_model_matches", [name]);   // 后端按可迭代对象处理，必须传数组
    list = typeof raw === "string" ? JSON.parse(raw || "[]") : (raw || []);
  } catch (e) { list = []; }
  const hit = Array.isArray(list) ? list.find((x) => x && x.local && x.path) : null;
  if (!hit) { setStatus("本地模型库未匹配到 " + name + "（不在设置的模型目录里）"); return; }
  libCloseDetail(); libLeavePage();
  switchPage("models");
  try { await showModelDetail(hit.path); } catch (e) { setStatus("模型详情打开失败"); }
}
function libSetStar(on) {
  const d = lib.detail; if (!d) return Promise.resolve();
  return api.call("lib_set_star", Number(d.id), !!on).then((r) => {
    if (!r || !r.ok) { setStatus((r && r.msg) || "收藏状态保存失败"); return; }
    d.starred = !!r.starred;
    const row = libRowById(d.id); if (row) row.starred = d.starred;
    const btn = document.querySelector('#detailPanel [data-lib-act="star"]');
    if (btn) { btn.setAttribute("aria-pressed", String(d.starred)); btn.innerHTML = _icon("star") + (d.starred ? "取消收藏" : "收藏"); }
    libRenderGrid(); libUpdateStats();
  }).catch(() => setStatus("收藏状态保存失败"));
}
function libDeleteItem() {
  const d = lib.detail; if (!d) return Promise.resolve();
  // 只回收媒体文件本体：同名 .json 侧车是 ComfyUI 给同名的图片/视频共享写的，删了会连带毁掉兄弟文件的元数据
  const msg = "删除 " + (d.filename || "") + " ？文件会被移进系统回收站（不是彻底删除，可从回收站还原）。同名 .json 侧车文件有意保留：ComfyUI 用它同时存放本媒体与同名兄弟文件（图片／视频共用一份生成数据）的元数据。";
  return confirmBox(msg).then((ok) => {
    if (!ok) return;
    return api.call("lib_delete", Number(d.id)).then((r) => {
      if (!r || !r.ok) { setStatus((r && r.msg) || "删除失败：文件可能被其它程序占用"); return; }
      lib.thumbCache.delete(d.path); lib.posterTried.delete(d.path);
      // 单条删除也要把这一项从批量已选里摘掉：留着它，下一次批量动作就会打到一个已不存在的行上
      lib.sel.delete(String(d.id)); lib.byId.delete(String(d.id));
      const idx = lib.rows.findIndex((x) => String(x.id) === String(d.id));
      if (idx >= 0) lib.rows.splice(idx, 1);
      lib.total = Math.max(0, lib.total - 1);
      libCloseDetail();
      libRenderGrid(); libUpdateStats(); libUpdateEmpty(); libUpdateMore(); libUpdateSelUi();
      setStatus("已删除（文件在回收站里）");
    });
  }).catch(() => setStatus("删除失败"));
}
function libDetailAction(act, btn) {
  const d = lib.detail; if (!d) return;
  if (act === "close") { libCloseDetail(); return; }
  // 详情头的翻页钮与 ←/→ 同一条路（libDetailKeyNav 只做判定，真正的换一件在这里）
  if (act === "prev") { libDetailNav(-1); return; }
  if (act === "next") { libDetailNav(1); return; }
  if (act === "star") return libSetStar(!d.starred);
  if (act === "tags") { libOpenTagPop(btn); return; }
  if (act === "delete") return libDeleteItem();
  if (act === "copy-path") { window.__copyText(d.path || "").then((ok) => setStatus(ok ? "路径已复制" : "复制失败")); return; }
  if (act === "copy-prompt") { window.__copyText(d.prompt || "").then((ok) => setStatus(ok ? "正面提示词已复制" : "复制失败")); return; }
  if (act === "copy-negative") { window.__copyText(d.negative || "").then((ok) => setStatus(ok ? "负面提示词已复制" : "复制失败")); return; }
  if (act === "folder") {
    return api.call("open_in_folder", d.path).then((raw) => {
      const r = typeof raw === "string" ? JSON.parse(raw || "{}") : (raw || {});
      setStatus(r && r.ok ? "已打开所在文件夹" : ((r && r.msg) || "文件不存在或已被移动"));
    }).catch(() => setStatus("打开文件夹失败"));
  }
  if (act === "workflow") {
    const p = String(d.path || "");
    libCloseDetail(); libLeavePage();
    switchPage("workflow");
    if (typeof wfAnalyze === "function") wfAnalyze(p); else api.call("analyze_workflow", p).catch(() => { });
    return;
  }
  if (act === "reverse") {
    // 与模型库「发送去识别」同一条既有链路：rp_add_paths 只把路径塞进反查列表，反向解析页用
    // rp_get_rows 渲染、点「开始反查」再 rp_start 才联网；不另起后端方法，也不在这里抢跑队列。
    // 缺 API Key 时 api 包装层已经提示并弹账号引导（返回 false），这里不再叠一条状态。
    const p = String(d.path || "");
    return api.call("rp_add_paths", [p]).then((ok) => {
      if (!ok) return;
      libCloseDetail(); libLeavePage();
      switchPage("reverse");
      if (typeof rpRefresh === "function") rpRefresh();
      setStatus("已送去逆向解析 " + short(p.split(/[\\/]/).pop() || "", 24) + "：在反向解析页点「开始反查」");
    }).catch(() => setStatus("送去反向解析失败"));
  }
}

// ---------- 分页 / 筛选 ----------
function libOnFilterChange() { libLoad(true); }
// 时长筛选的唯一入口（防抖 input 与 change 都收敛到这里）：非法就当场提示、不发请求，
// 合法才走 libOnFilterChange → libLoad(true) → libReadControls/libBuildFlt 的既有路径。
function libApplyDurationFilter() {
  libReadControls();
  if (lib._durError) { setStatus(lib._durError); return; }
  libOnFilterChange();
}
function libMaybeMore() {
  if (lib.loading || lib.scanRunning) return;
  if (lib.rows.length >= lib.total) return;
  const vp = libEl("libViewport");
  if (vp && vp.scrollHeight - vp.scrollTop - vp.clientHeight > 400) return;
  lib.offset = lib.rows.length;
  libLoad(false);
}
function libOnScroll() {
  if (lib.scrollTimer) return;      // 300ms 节流，与模型库滚动同一参数
  lib.scrollTimer = setTimeout(() => { lib.scrollTimer = null; libMaybeMore(); }, 300);
}

// ---------- 设置页（media_* 三键，控件由 index.html 提供） ----------
function libSyncSettings() {
  const cfg = state.cfg || {};
  const roots = libEl("setMediaRoots"); if (roots) roots.value = (cfg.media_roots || []).join("\n");
  const exc = libEl("setMediaExclude"); if (exc) exc.value = (cfg.media_exclude_dirs || []).join("\n");
  const size = libEl("setMediaPageSize"); if (size) size.value = String(cfg.media_page_size || 200);
}
function libCfgSet(key, value) { if (state.cfg) state.cfg[key] = value; }
function libDetectRoots() {
  api.call("lib_autodetect_roots").then((r) => {
    if (!r || !r.ok) { setStatus((r && r.msg) || "没有探测到 ComfyUI output 目录"); return; }
    const found = Array.isArray(r.roots) ? r.roots : [];
    if (!found.length) { setStatus("没有探测到 ComfyUI output 目录，请手动填写"); return; }
    const box = libEl("setMediaRoots");
    // A4 的按钮说明是「不覆盖手写路径」：所以是并入而不是替换
    const curText = box ? String(box.value || "") : ((state.cfg || {}).media_roots || []).join("\n");
    const lines = curText.split("\n").map((s) => String(s).trim()).filter(Boolean);
    const norm = (s) => String(s).replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
    const seen = new Set(lines.map(norm));
    const added = found.filter((p) => p && !seen.has(norm(p)));
    if (box) box.value = lines.concat(added).join("\n");
    libCfgSet("media_roots", lines.concat(added));
    setStatus(added.length ? "探测到并并入 " + added.length + " 个目录（已存在的没重复添加），记得点保存" : "探测到的目录都已在列表里，无需改动");
  }).catch(() => setStatus("自动探测失败"));
}

// ---------- 切页清理 ----------
function libReleaseGridVideos() {
  const grid = libEl("libGrid"); if (!grid) return;
  grid.querySelectorAll("video").forEach((v) => libReleaseVideo(v));
}
function libLeavePage() {
  libStopScanPoll();
  if (lib.scrollTimer) { clearTimeout(lib.scrollTimer); lib.scrollTimer = null; }
  if (lib.qTimer) { clearTimeout(lib.qTimer); lib.qTimer = null; }
  if (lib.durTimer) { clearTimeout(lib.durTimer); lib.durTimer = null; }
  lib.request++;                    // 让在途的列表/缩略图响应作废
  lib.thumbBusy = false;
  lib.posterTimers.forEach((t) => clearTimeout(t)); lib.posterTimers = [];
  if (lib.vObs) lib.vObs.disconnect();          // 观察器只解绑、实例留着：回页时 libHookCardVideos 重新登记
  libReleaseGridVideos();                       // 离页把解码器和 /api/media 流全部交出去，隐藏页不该继续占
  lib.posterTried.clear(); lib.thumbCache.clear();   // 会话级缓存到此为止：10 万视频的滚动会话不能让它们只涨不落
  libHidePopovers();
}

// ---------- 事件绑定（一律文档级委托：A4 的标记若有出入也不会因缺节点抛错） ----------
const LIB_FILTER_IDS = ["libKindF", "libSort", "libGroup", "libCkF", "libLoraF", "libTagF", "libDirF", "libStarOnly", "libNoMeta", "libDurMin", "libDurMax"];
document.addEventListener("click", (e) => {
  const t = e.target;
  if (!t || !t.closest) return;
  const id = t.id || "";
  // 切页清理：switchPage 只允许 1 行钩子（进 library），离开由这里兜住（#navTabs 的委托先执行）
  const navTab = t.closest(".nav-tab");
  if (navTab && navTab.dataset.page !== "library") libLeavePage();
  if (id === "libScanBtn") { libStartScan(); return; }
  if (id === "libStopScanBtn") { libStopScan(); return; }
  // 标注导出／导入：按 closest 而不是 t.id——按钮里包着 <svg>/<use>，点在图标上时 target 自己没有 id
  if (t.closest("#libExportAnn")) { libExportAnnotations(); return; }
  if (t.closest("#libImportAnn")) { libPickImportFile(); return; }
  if (id === "libMore") { libMaybeMore(); return; }
  if (id === "libRootDetect") { libDetectRoots(); return; }
  // 多选：选择行三个控件 + 操作条七个动作 + 标签浮层的应用按钮。
  // 走 [data-lib-act^="sel-"] 的 closest 而不是 t.id：带图标的按钮里 target 常常是 <svg>/<use>，
  // 它自己没有 id，按 id 判会「点到图标就没反应」。disabled 的按钮浏览器不发 click，这里不用再判可点性。
  const selBtn = t.closest('[data-lib-act^="sel-"]');
  if (selBtn) { libSelBarAction(selBtn.dataset.libAct, selBtn); return; }
  if (id === "libFoldersBtn") {
    const panel = libEl("libFoldersPanel");
    if (!panel) return;
    const open = !lib.foldersOpen;
    libHidePopovers();
    lib.foldersOpen = open;
    panel.hidden = !open;
    t.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) { lib.popKind = "folders"; lib.popAnchor = t; libRenderFolders(); }
    return;
  }
  const tab = t.closest('.nav-tab[data-page="settings"]');
  if (tab) { setTimeout(libSyncSettings, 0); return; }
  const dirItem = t.closest('[data-lib-dir]');
  if (dirItem) { libToggleDir(dirItem.dataset.libDir); return; }
  if (t.closest('[data-lib-act="dirs-none"]')) { lib.filters.dirs = []; lib._dirsFromPanel = []; const s = libEl("libDirF"); if (s) s.value = ""; libRenderFolders(); libOnFilterChange(); return; }
  // 提示词「展开全文 / 收起」：属性名与分面那套 data-lib-facetmore 分开写，两条 closest 互不误伤
  const descMore = t.closest("[data-lib-descmore]");
  if (descMore) { libToggleDescMore(descMore.dataset.libDescmore); return; }
  // 分面 chip：× 必须先判（它自己也带 data-lib-facetval，落到下一条就会「取消又选中」＝点了没反应）
  const facetMore = t.closest("[data-lib-facetmore]");
  if (facetMore) { libToggleFacetMore(facetMore.dataset.libFacetmore); return; }
  const facetDel = t.closest("[data-lib-facetdel]");
  if (facetDel) { libToggleFacetChip(facetDel.dataset.libFacetdel, facetDel.dataset.libFacetval, false); return; }
  const facetChip = t.closest(".lib-facet-chip[data-lib-facetval]");
  if (facetChip) { libToggleFacetChip(facetChip.dataset.libFacet, facetChip.dataset.libFacetval); return; }
  // 批量标签浮层里的 chip：只勾/取消勾，真正的增删等点「应用」（属性名与单条浮层的 data-lib-tag 分开，互不误伤）
  const selTagChip = t.closest("[data-lib-sel-tag]");
  if (selTagChip) {
    const tid = Number(selTagChip.dataset.libSelTag);
    if (lib.selTagIds.has(tid)) lib.selTagIds.delete(tid); else lib.selTagIds.add(tid);
    libRenderSelTagPop(libEl("libSelTagPop"));
    return;
  }
  const delTag = t.closest("[data-lib-tagdel]");
  if (delTag) { libDeleteTag(Number(delTag.dataset.libTagdel), delTag.dataset.libTagname, Number(delTag.dataset.libTagcount)); return; }
  const tagItem = t.closest("[data-lib-tag]");
  if (tagItem && lib.detail) {
    const tid = Number(tagItem.dataset.libTag);
    const ids = (lib.detail.tags || []).map((x) => Number(x.id));
    const i = ids.indexOf(tid);
    if (i >= 0) ids.splice(i, 1); else ids.push(tid);
    libSaveTags(ids);
    return;
  }
  if (t.closest('[data-lib-act="tag-add"]')) {
    const pop = libEl("libTagPop");
    const inp = pop ? pop.querySelector("[data-lib-newtag]") : null;
    const name = inp ? String(inp.value || "").trim() : "";
    if (!name) { setStatus("先写标签名"); return; }
    libAddTag(name).then(libAfterTagChange);
    return;
  }
  const model = t.closest("[data-lib-model]");
  if (model) { libGotoModel(model.dataset.libModel); return; }
  const act = t.closest("#detailPanel [data-lib-act]");
  if (act) { libDetailAction(act.dataset.libAct, act); return; }
  const card = t.closest("#libGrid .lib-card");
  if (card) {
    if (e.detail > 1) return;
    // 多选态＝切换选中；非多选态＝保持原样打开详情（高亮类的分工见 libActivateCard）
    libActivateCard(card, e.shiftKey);
    return;
  }
  const main = t.closest("#detailPanel #dMain");
  if (main && main.tagName === "IMG") {   // 详情图片就地放大：库内文件是原图，无需再走在线画廊查看器
    const big = main.dataset.libBig === "1";
    main.dataset.libBig = big ? "0" : "1";
    if (!big) {
      if (main.dataset.libFull && main.src !== main.dataset.libFull) { main.dataset.libPreview = main.src; main.src = main.dataset.libFull; }
      main.style.maxHeight = "calc(100vh - 160px)";
    } else {
      if (main.dataset.libPreview) main.src = main.dataset.libPreview;
      main.style.maxHeight = "";
    }
    main.style.objectFit = "contain";
    return;
  }
  // 浮层外点击即关（浮层已 portal 到 body，只判断自身与锚点）
  if (lib.popKind && !t.closest("#libTagPop") && !t.closest("#libSelTagPop") && !t.closest("#libFoldersPanel") && !(lib.popAnchor && lib.popAnchor.contains(t))) libHidePopovers();
});
document.addEventListener("mouseover", (e) => {
  const v = e.target && e.target.closest ? e.target.closest("#libGrid video.lib-vid") : null;
  if (!v) return;
  libHydrateVideo(v);                 // 悬停可能赶在观察器回调之前：没挂 src 的 play() 只会 reject
  if (v.paused) { const p = v.play && v.play(); if (p && p.catch) p.catch(() => { }); }
});
document.addEventListener("mouseout", (e) => {
  const card = e.target && e.target.closest ? e.target.closest("#libGrid .lib-card") : null;
  if (!card || (e.relatedTarget && card.contains(e.relatedTarget))) return;
  const v = card.querySelector("video.lib-vid"); if (v) { try { v.pause(); } catch (err) { } }
});
document.addEventListener("input", (e) => {
  const t = e.target; if (!t || !t.id) return;
  if (t.id === "libQ") {
    clearTimeout(lib.qTimer);
    lib.qTimer = setTimeout(() => { lib.filters.q = String(t.value || "").trim(); libOnFilterChange(); }, 300);
    return;
  }
  // 时长区间：与 #libQ 同一条 300ms 防抖节奏，非法值在 libApplyDurationFilter 里当场拦掉
  if (t.id === "libDurMin" || t.id === "libDurMax") {
    clearTimeout(lib.durTimer);
    lib.durTimer = setTimeout(() => { lib.durTimer = null; libApplyDurationFilter(); }, 300);
    return;
  }
  if (t.id === "setMediaRoots") { t.dataset.touched = "1"; libCfgSet("media_roots", String(t.value || "").split("\n").map((s) => s.trim()).filter(Boolean)); return; }
  if (t.id === "setMediaExclude") { t.dataset.touched = "1"; libCfgSet("media_exclude_dirs", String(t.value || "").split("\n").map((s) => s.trim()).filter(Boolean)); return; }
  if (t.id === "setMediaPageSize") { t.dataset.touched = "1"; libCfgSet("media_page_size", Number(t.value) || 200); }
});
// A4 的媒体控件不在 SETTING_FIELDS 里（saveSettings 只回收 [data-key]），值靠 state.cfg 桥接：
// 进设置页要回填，用户没动过的控件聚焦时再补一次，避免框里是空的让人以为没配置
document.addEventListener("focusin", (e) => {
  const t = e.target;
  if (!t || !t.id || t.dataset.touched) return;
  if (["setMediaRoots", "setMediaExclude", "setMediaPageSize"].indexOf(t.id) >= 0) libSyncSettings();
});
document.addEventListener("change", (e) => {
  const t = e.target; if (!t || !t.id) return;
  // 多选开关不是筛选：不重发 lib_list、不清已选（切换只改交互语义，见 libSetSelectMode）
  if (t.id === "libSelectMode") {
    libSetSelectMode(t.checked);
    setStatus(lib.selectMode
      ? "多选已打开：单击卡片切换选中，Shift 点选在当页里做范围选；已选 " + lib.sel.size + " 项"
      : "多选已关闭：单击卡片回到「打开详情」。已选的 " + lib.sel.size + " 项仍然保留（跨页、跨筛选），要清掉点选择行里的「清除选择」");
    return;
  }
  if (LIB_FILTER_IDS.indexOf(t.id) < 0) return;
  if (t.id === "libDirF") { lib.filters.dirs = t.value ? [t.value] : []; lib._dirsFromPanel = []; if (lib.foldersOpen) libRenderFolders(); }
  // 时长输入：失焦 / 回车 / 步进箭头触发的 change 清掉在等的防抖，避免同一次改动发两个请求
  if (t.id === "libDurMin" || t.id === "libDurMax") { if (lib.durTimer) { clearTimeout(lib.durTimer); lib.durTimer = null; } libApplyDurationFilter(); return; }
  libReadControls();
  libOnFilterChange();
});
// 详情打开时 ← / → 换一件（判定与互斥全在 libDetailKeyNav 里，含多选态/Esc/全屏查看器的纪律）。
// 挂在冒泡阶段的末尾：app.js 在 commands.js 之后加载，所以用户自绑的快捷键先跑，它们 preventDefault 过
// 我就让开（libDetailKeyNav 第一件事就是查 e.defaultPrevented）。
document.addEventListener("keydown", libDetailKeyNav);
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!lib.popKind) return;                 // 浮层打开时 Esc 只关浮层，不同时关掉详情（捕获阶段抢在既有 Esc 处理之前）
  libHidePopovers();
  e.preventDefault(); e.stopImmediatePropagation();
}, true);
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  if (!e.target || !e.target.closest) return;
  // 分面 chip 是 role=button 的 span，键盘要能按；焦点落在里面的 × 时交给浏览器原生按钮激活，
  // 这里再 click 一次就成了「取消 + 重新选中」＝没反应（Space 的原生 click 在 keyup，还会再触发一次）。
  const fchip = e.target.closest(".lib-facet-chip[data-lib-facetval]");
  if (fchip) { if (e.target.closest("button")) return; e.preventDefault(); fchip.click(); return; }
  const fmore = e.target.closest("[data-lib-facetmore]");
  if (fmore) { e.preventDefault(); fmore.click(); return; }        // 展开/收起也是 role=button 的 span
  // 提示词的「展开全文 / 收起」同样是 role=button 的 span：Enter / Space 与鼠标同权。
  // 判定放在 chip 之后、且用独立属性名，不会和上面两条互相吃命中。
  const dmore = e.target.closest("[data-lib-descmore]");
  if (dmore) { e.preventDefault(); dmore.click(); return; }
  const chip = e.target.closest("#libTagPop [data-lib-tag]");
  if (chip) { e.preventDefault(); chip.click(); return; }      // chip 是 role=button 的 span，键盘要能按
  const selChip = e.target.closest("#libSelTagPop [data-lib-sel-tag]");
  if (selChip) { e.preventDefault(); selChip.click(); return; }
  const card = e.target.closest("#libGrid .lib-card");
  if (!card) return;
  e.preventDefault();
  libActivateCard(card, e.shiftKey);      // 多选态下 Enter/Space 同样是切换选中（shift 做范围选）
});
window.addEventListener("resize", () => {
  if (lib.popKind === "tags") libPopPosition(libEl("libTagPop"), lib.popAnchor);
  else if (lib.popKind === "seltags") libPopPosition(libEl("libSelTagPop"), lib.popAnchor);
});

function libInit() {
  const vp = libEl("libViewport");
  if (vp) vp.addEventListener("scroll", libOnScroll, { passive: true });
  window.addEventListener("scroll", libOnScroll, { passive: true });
  libSetScanBtns(false);
  libSetRootHint();
  // 多选相关控件的初始态：选择行与操作条都藏着、动作按钮全禁用（没有已选就没有能点的动作）
  libSetSelectMode(false);
  // 导入标注：选完文件才走「读文本 → 试算 → 确认 → 写入」。在系统文件框里按取消不会发 change，
  // 发了空列表也在 handler 里当场返回，不会有半开状态。
  const importFile = libEl("libImportFile");
  if (importFile) importFile.addEventListener("change", libOnImportFileChosen);
}
try { libInit(); } catch (e) { /* 资源库标记未就位时不影响其它页面 */ }
