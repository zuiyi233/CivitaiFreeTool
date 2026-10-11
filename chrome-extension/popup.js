// CivitaiFreeTool 一键下载 + 一键归档 —— 确认小窗
// 原有函数名与行为保留：getActiveTab / readPageTitle / checkBridge / cleanTitle / startDownload。
const BRIDGE = "http://127.0.0.1:47531";
const CIVITAI_MODEL_RE = /^https?:\/\/(www\.)?civitai\.(red|com)\/(api\/download\/)?models\//i;

const $ = (id) => document.getElementById(id);
const views = {
  loading: $("view-loading"),
  bad: $("view-bad"),
  offline: $("view-offline"),
  ready: $("view-ready"),
  done: $("view-done"),
  error: $("view-error"),
};
function show(name) {
  Object.entries(views).forEach(([k, el]) => el.classList.toggle("hidden", k !== name));
}
function cleanTitle(t) {
  // C 站标题形如 "模型名 | Civitai" / "Civitai | 模型名"
  let s = String(t || "").trim();
  s = s.replace(/\s*[|\|]\s*Civitai\s*$/i, "").trim();
  s = s.replace(/^Civitai\s*[|\|]\s*/i, "").trim();
  return s || "Civitai 模型";
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function readPageTitle(tabId) {
  try {
    const [r] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => document.title,
    });
    return r && r.result ? cleanTitle(r.result) : "";
  } catch (e) {
    return ""; // 无 host 权限（非模型页等）时忽略
  }
}

async function checkBridge() {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 2500);
  try {
    const res = await fetch(BRIDGE + "/api/health", { signal: ctrl.signal });
    const data = await res.json().catch(() => ({}));
    return !!(data && data.ok);
  } catch (e) {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

// 设置区：经 bg 读取归档通道（带缓存，失败则显示降级）
async function loadArchiveStatus() {
  const el = $("archiveStatus");
  try {
    const r = await chrome.runtime.sendMessage({ __cft: true, to: "bg", type: "getArchiveConfig", payload: {} });
    if (r && r.channel === "software" && r.archiveRoot) {
      el.textContent = "软件通道：" + r.archiveRoot;
    } else if (r && r.softwareOnline) {
      el.textContent = "软件在线但未设置归档根目录，本次走浏览器下载目录（降级）";
    } else {
      el.textContent = "浏览器下载目录（降级，软件未在线）";
    }
  } catch (e) {
    el.textContent = "归档通道读取失败，本次走浏览器下载目录（降级）";
  }
}

// 离线降级态：仍进 view-ready，禁用发软件下载按钮，归档按钮保持可用
async function showOfflineReady(tab) {
  $("modelName").textContent = await readPageTitle(tab.id);
  $("modelUrl").textContent = tab && tab.url ? tab.url : "";
  $("modelMeta").textContent = "软件未在线，直接下载不可用；归档仍可继续";
  const dlBtn = $("btnDownload");
  dlBtn.disabled = true;
  dlBtn.textContent = "⬇️ 开始下载（需先打开软件）";
  dlBtn.title = "请先打开 CivitaiFreeTool 软件后再直接下载";
  $("archiveNote").textContent = "软件未在线，归档将保存到浏览器下载目录 CivitaiArchive/";
  $("archiveNote").classList.remove("hidden");
  $("btnArchive").disabled = false;
  $("btnArchive").textContent = "🗂 一键归档到本地";
  show("ready");
}

async function init() {
  show("loading");
  loadArchiveStatus();
  const tab = await getActiveTab();
  const url = tab && tab.url ? tab.url : "";
  if (!CIVITAI_MODEL_RE.test(url)) {
    show("bad");
    return;
  }
  const online = await checkBridge();
  if (!online) {
    await showOfflineReady(tab);
    return;
  }
  $("modelName").textContent = await readPageTitle(tab.id);
  $("modelUrl").textContent = url;
  $("modelMeta").textContent = "确认后将在 CivitaiFreeTool 中直接下载";
  $("btnDownload").disabled = false;
  $("btnDownload").textContent = "⬇️ 开始下载";
  $("archiveNote").classList.add("hidden");
  $("btnArchive").disabled = false;
  $("btnArchive").textContent = "🗂 一键归档到本地";
  show("ready");
}

async function startDownload() {
  const btn = $("btnDownload");
  btn.disabled = true;
  btn.textContent = "正在解析并下载…";
  let offlineAbort = false;
  try {
    const url = $("modelUrl").textContent;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 15000);
    let res;
    try {
      res = await fetch(BRIDGE + "/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
        signal: ctrl.signal,
      });
    } catch (e) {
      // 下载时才发现软件离线：同样进降级 ready 态，归档仍可用
      const t = await getActiveTab().catch(() => null);
      if (t) await showOfflineReady(t);
      else show("offline");
      offlineAbort = true;
      return;
    } finally {
      clearTimeout(timer);
    }
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      show("done");
    } else {
      $("errMsg").textContent = data.msg || "未知错误";
      show("error");
    }
  } finally {
    // 离线 abort 时 showOfflineReady 已设置好按钮状态，此处不再覆盖
    if (!offlineAbort) {
      btn.disabled = false;
      btn.textContent = "⬇️ 开始下载";
    }
  }
}

// 一键归档：bg 接管采集+下发 offscreen；真实进度由页面内按钮展示，
// popup 只做内联提示并 1s 后自动关闭。
async function startArchive() {
  const btn = $("btnArchive");
  const note = $("archiveNote");
  btn.disabled = true;
  btn.textContent = "正在启动归档…";
  try {
    const tab = await getActiveTab();
    const r = await chrome.runtime.sendMessage({
      __cft: true, to: "bg", type: "startArchive", payload: { tabId: tab && tab.id },
    });
    if (r && r.ok) {
      note.textContent = "归档已在页面内进行，请查看页面右下角按钮进度";
      note.classList.remove("hidden");
      setTimeout(() => window.close(), 1000);
    } else {
      note.textContent = (r && r.msg) || "归档启动失败";
      note.classList.remove("hidden");
      btn.disabled = false;
      btn.textContent = "🗂 一键归档到本地";
    }
  } catch (e) {
    note.textContent = "归档启动失败，请重试";
    note.classList.remove("hidden");
    btn.disabled = false;
    btn.textContent = "🗂 一键归档到本地";
  }
}

$("btnDownload").addEventListener("click", startDownload);
$("btnArchive").addEventListener("click", startArchive);
$("btnClose").addEventListener("click", () => window.close());
$("btnRetry").addEventListener("click", () => { show("loading"); init(); });
document.addEventListener("DOMContentLoaded", init);
