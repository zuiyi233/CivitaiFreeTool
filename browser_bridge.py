# -*- coding: utf-8 -*-
"""浏览器桥：本地 HTTP 服务（127.0.0.1），供 Chrome 扩展一键下载 + **浏览器模式的前端界面与 RPC**。

设计要点
- 仅绑定 127.0.0.1，外部网络不可达；
- CORS 白名单：只允许 chrome-extension://* 与本机页面（file://、null、127.0.0.1/localhost）；
- POST /api/download：校验 URL 后调用注册的下载 handler（webui.Api.dl_submit_url_from_bridge，
  老 handler 是 dl_submit_url：解析并入队下载均为后台线程，handler 同步快速返回）；
  **handler 至少收得住 (url, dest_subdir) 两参**——归档桥的 sendModelToSoftware 靠第二个参数
  把模型文件落进 <归档根>/<目录名>/。handler 自己额外声明了 `caps` 关键字时，桥把请求体里的
  caps 数组原样带进去（按签名探测，绝不给两参老 handler 硬塞第三参——v2.6.30 就是这么整条链 500 的）。
- 能力协商：请求体可选 `caps: ["bundle"]`。**只有扩展声明了 bundle 能力，桌面端才允许按类型分流**
  （分流后模型本体进 ComfyUI 目录，附属文件必须靠 bundle 令牌才能跟着走；老扩展只会把附属文件
  按 destSubdir 写回 <归档根>/<目录名>，届时本体与归档分家——正是用户投诉的「没归档」现场）。
  桌面自身 UI 的下载由桌面自己写附属文件，不受这个门限约束。
- POST /api/archive/file：两种形态（旧→新，逐条保留，缺字段一律 400）
  · `{rel_path, data_b64}`     落点 = 归档根 / rel_path（历史行为）
  · `{bundle, filename, data_b64}` 落点 = bundle 令牌登记的绝对目录，filename 必须是**纯文件名**
- POST /api/archive/done：`{dir_name}`，尽力而为触发模型库重扫；分流后 dir_name 不再代表路径，
  因此允许空参（扩展的批量结束恒传空串），请求体带 `bundle` 时顺手释放该令牌。
- POST /api/model/status：扩展「模型下载状态」批量查询（模型库/历史/任务/更新缓存），
  只读本机数据、不联网；契约见 docs/extension-model-status.md。
- POST /api/ext/tabs：扩展只读上报「打开的 C 站模型页索引」（标题/URL/模型版本 ID，仅内存）；
  GET /api/ext/tabs 供本机 Agent 搜索；契约见 docs/browser-tabs-bridge.md。
- POST /api/rpc：浏览器模式下的通用方法调用（前端 shim 把 window.pywebview.api.xxx 映射到这里）；
- GET /、/app.js 等：浏览器模式下直接由本服务提供 web/ 目录里的界面文件；
- GET /api/heartbeat：页面心跳（用于「关页面即退出」）；
- GET /api/media：资源库媒体文件流式读取（支持 Range/206，浏览器模式视频播放唯一可行路径）；
- 端口冲突/启动失败静默（不影响主程序）。
"""
import base64
import inspect
import json
import mimetypes
import os
import queue
import re
import secrets
import select
import socket
import threading
import time
import urllib.parse
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

DEFAULT_PORT = 47531

# 合法来源（C 站模型页 / API 下载链接）
_URL_HOST_RE = re.compile(r"^(www\.)?civitai\.(red|com)$", re.I)
_URL_PATH_RE = re.compile(r"^/(api/download/)?models/", re.I)
# 浏览器扩展来源：Chrome/Edge 系用 chrome-extension://，Firefox 用 moz-extension://<uuid>
_EXT_ORIGIN_RE = re.compile(r"^(chrome|moz)-extension://", re.I)
_LOCAL_ORIGIN_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$", re.I)

# 归档写盘的 body 上限：仅 /api/archive/file 放宽（base64 膨胀，按原始 64MB 设计）
_ARCHIVE_FILE_MAX = 128 * 1024 * 1024
_ARCHIVE_RAW_MAX = 64 * 1024 * 1024
_NOT_READY = {"ok": False, "msg": "归档服务未就绪"}

_lock = threading.Lock()
_server = None
_version = ""
_handler = None        # def download_url(url, dest_subdir="") -> dict（同步返回，如 {"started": True}）
_api = None            # webui.Api 实例：浏览器模式 RPC 用
_web_root = ""         # 界面文件根目录（浏览器模式）
_last_seen = 0.0       # 页面最后一次请求时间（心跳）
# 界面状态（看门狗与诊断用）：mode=window/browser；window=窗口是否已可见
_ui_state = {"mode": "window", "window": False, "pid": os.getpid(), "started_at": int(time.time())}
# 归档注册制：webui.Api 启动时注册 config/write/done/check 四个入口
_archive_config_fn = None  # def config() -> dict
_archive_write_fn = None   # def write(rel_path, data: bytes) -> str/False
_archive_done_fn = None    # def done(dir_name) -> 任意（尽力而为）
_archive_check_fn = None   # def check(version_id) -> dict {found, paths}
_model_status_fn = None    # def status(items, include_updates=True) -> dict
_reveal_fn = None          # def reveal(path) -> dict（扩展「打开所在目录」；只认桌面端已登记路径）
# 资源库媒体流（GET /api/media）：可读根目录由 webui.Api 注入，桥层不 import webui
_media_roots_fn = None     # def media_roots() -> [绝对根目录]

# ---------------- bundle 落点令牌注册表（模块级，与 _handler 同风格） ----------------
# 为什么要有令牌：扩展算不出分流落点（看不到 safetensors 头、不知道 ComfyUI 目录现状），
# 而封面/配图/提示词/example.json/评论必须写进「模型本体真正所在的那个目录」。
# 桌面端分流成功时把绝对落点登记成一个不透明随机串，随 /api/download 回给扩展；
# 扩展之后只带令牌写文件，绝不自己拼路径 —— 令牌同时充当「这个目录是桌面自己算出来的」凭据，
# 所以只有桌面登记的目录能用，扩展拿任意路径来写不认。
_BUNDLE_TTL = 30 * 60.0
"""滑动 TTL（秒）。批量归档从 prepare 阶段（提交本体）到阶段三（写评论）可能隔好几分钟，
每次写入都续期；令牌过早失效 → 附属文件降级到浏览器下载目录 → 归档分家（用户投诉的现场）。"""

_BUNDLE_MAX = 256
"""容量上限：一屏批量也就几十项，256 足够宽；超出按「最久没写入」淘汰，避免长跑进程无界增长。"""

_bundles = OrderedDict()   # token -> (绝对落点, 到期时刻)；插入序 == 「最近写入序」（resolve 时 move_to_end）
_bundle_lock = threading.Lock()

# 能力名：扩展声明「收得住 bundle 令牌」
CAP_BUNDLE = "bundle"

# 媒体扩展名白名单——镜像 media_library.IMAGE_EXTS / VIDEO_EXTS（契约 §3.1）。
# 为什么在本模块重复定义：这是「越界读文件」的第一道安全闸，必须在 media_library
# 尚未导入/导入失败时也照样生效；两边任一改动都要同步另一侧。
MEDIA_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
MEDIA_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv")
MEDIA_EXTS = MEDIA_IMAGE_EXTS + MEDIA_VIDEO_EXTS
# mimetypes 在部分 Windows/Python 环境里缺条目（实测 .webp 返回 None），而回落值按契约
# 是 video/mp4 —— 给图片用 video/mp4 会让前端 <img> 直接空白，故仅补齐图片侧。
_MEDIA_TYPES = {".webp": "image/webp"}
_MEDIA_CHUNK = 64 * 1024       # 分块写：绝不整读（2GB mp4 不得进内存）
_MEDIA_MAX_PATH = 4096         # 超长输入直接拒，避免无谓的 realpath 开销
_MEDIA_DENY = "文件不存在或无权访问"  # 越界与缺失共用文案：不泄露根目录里有什么
_MEDIA_MSG_MAX = 120


def set_media_roots(fn):
    """注册资源库媒体根目录提供者：webui.Api 启动时把 _media_roots_for_bridge 注册进来。

    fn() -> 绝对根目录列表（可为空）。与 set_archive_handlers 同风格（注入解耦，桥层
    不认识 webui）；未注入时 /api/media 一律 403 且不碰磁盘。
    """
    global _media_roots_fn
    _media_roots_fn = fn


def _media_shape_ok(p):
    """形状校验（不碰磁盘）：非空、绝对路径、无 NUL、无 '..' 段、扩展名在媒体白名单内"""
    if not p or len(p) > _MEDIA_MAX_PATH:
        return False
    if "\x00" in p:
        return False
    try:
        if not os.path.isabs(p):      # 相对路径一律拒（前端只会传绝对路径，出现即异常请求）
            return False
    except Exception:
        return False
    # 逐段判 '..'（而不是子串），免得把 "a..b.png" 这类合法文件名误杀
    for seg in re.split(r"[\\/]", p):
        if seg == "..":
            return False
    ext = os.path.splitext(p)[1].lower()
    return ext in MEDIA_EXTS


def _media_within_roots(real_path, roots):
    """realpath 后的候选是否落在某个 media_root 的 realpath 之内（含根自身）

    两侧都必须 realpath：只规范化候选会被「根内的符号链接指向根外」绕过；
    比较时补 os.sep，避免 /data 前缀误匹配 /dataset。normcase 仅为兼容 Windows
    大小写差异（POSIX 下恒等），不会放宽真实目录边界。
    去尾分隔符时保留 '/'、'\\'、'C:\\' 这类根（否则前缀比较会退化）。
    """
    cand = os.path.normcase(real_path)
    for root in roots or ():
        try:
            if not root or not isinstance(root, str):
                continue
            rr = os.path.normcase(os.path.realpath(root))
            if not rr:
                continue
            while len(rr) > 1 and rr.endswith(("/", "\\")) and rr not in ("\\", "/"):
                rr = rr[:-1]
            prefix = rr if rr.endswith(("/", "\\")) else rr + os.sep
            if cand == rr or cand.startswith(prefix):
                return True
        except Exception:
            continue
    return False


def parse_media_range(header, size):
    """解析单子范围 Range 头（RFC 7233）→ (start, end)；不可满足/非法返回 None（调用方给 416）。

    支持 bytes=a-b / bytes=a- / bytes=-N（后缀范围）；b 越界按 size-1 收口。
    多范围（含逗号）按非法处理：multipart/byteranges 视频播放用不上，实现它只会扩大解析面。
    size=0 时任何范围都不可满足（同时避开负长度/除零）。
    """
    if not header or "," in header:
        return None
    m = re.match(r"^bytes=(\d*)-(\d*)$", header.strip())
    if not m:
        return None
    first, last = m.group(1), m.group(2)
    if not first and not last:
        return None
    try:
        if not first:                     # bytes=-N：末尾 N 字节
            n = int(last)
            if n <= 0 or size <= 0:
                return None
            return (max(size - n, 0), size - 1)
        start = int(first)
        if size <= 0 or start >= size:     # 空文件 / 起点越界 → 416
            return None
        end = int(last) if last else size - 1
        if end < start:
            return None
        return (start, min(end, size - 1))
    except (ValueError, OverflowError):
        return None


def _media_is_abort(exc):
    """是否「客户端提前断开」：视频拖动/关页面是常态，必须静默（Windows 可能是裸 OSError）"""
    if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
        return True
    return getattr(exc, "winerror", None) in (10053, 10054, 10058) or \
        getattr(exc, "errno", None) in (32, 104, 10053, 10054)


def _media_log(exc):
    """非断开类异常交给主程序日志面（与 _serve_static 同风格），桥层自身不 print"""
    try:
        if _api is not None and hasattr(_api, "log_error"):
            import traceback as _tb
            _api.log_error("media", "%s\n%s" % (str(exc)[:200], _tb.format_exc()))
    except Exception:
        pass


def _stream_media(handler, fh, start, length):
    """分块（64KB）写出 fh 的 [start, start+length)；任何写入失败都静默返回。

    为什么分块：本端点存在的理由就是 GB 级视频不能整读进内存（见契约 §11.6）。
    """
    try:
        fh.seek(start)
    except OSError:
        return False
    remaining = length
    while remaining > 0:
        try:
            chunk = fh.read(min(_MEDIA_CHUNK, remaining))
        except OSError:
            return False
        if not chunk:
            # 文件被外部截断：Content-Length 已发出，只能提前收尾（媒体按不可变设计）
            return False
        try:
            handler.wfile.write(chunk)
        except Exception as e:
            if not _media_is_abort(e):
                _media_log(e)
            return False
        remaining -= len(chunk)
    return True


def set_ui_state(**kw):
    """主程序报告界面状态（/api/health 会带上，看门狗用它判定「界面到底出来没有」）"""
    try:
        _ui_state.update(kw)
        _ui_state["reported_at"] = int(time.time())
    except Exception:
        pass


def set_download_handler(fn):
    """注册下载入口：webui.Api 启动时把 dl_submit_url 注册进来（必须接受 (url, dest_subdir) 两参）

    想收能力协商（caps）的 handler 自己声明 `caps=` 关键字参数即可，桥按签名决定要不要传
    —— 老的两参 handler 不会因为多一个参数而 TypeError（v2.6.30 就是这么整条链 500 的）。
    """
    global _handler
    _handler = fn


def set_api(obj, web_root=""):
    """注册 RPC 目标（webui.Api 实例）与界面文件目录（浏览器模式）"""
    global _api, _web_root
    _api = obj
    if web_root:
        _web_root = web_root


def page_alive(max_silence=8.0):
    """页面是否还活着（浏览器模式「关页面即退出」用）：最近 max_silence 秒内有过请求"""
    return (time.time() - _last_seen) < max_silence


# ---- 自动化接口（AI / 脚本调试用，只读 + 白名单写配置）----
# 设计目标：把「人肉点界面才能拿到/改到的东西」变成一条 HTTP 调用，省掉截图-点击-轮询。
# 不新增写盘/下载能力：下载仍走 /api/download，归档仍走 /api/archive/*，这里只做观测与配置。
AUTOMATION_CONFIG_KEYS = (
    "route_by_type", "comfyui_models_root", "ask_move_after_download",
    "multi_file_download", "download_target_dir", "download_dir",
    "models_dirs", "site_domain", "max_images_per_model", "download_cover",
    "proxy_enabled", "proxy_address", "ssl_verify",
    # 影响文件名/目录名与批量吞吐：自动化脚本常需要显式固定这些
    "download_name_mode", "filename_include_version", "rename_clean_rules",
    "max_concurrent_downloads", "download_timeout", "download_retry",
    "gen_metadata", "save_raw_json", "save_translated_json", "auto_translate",
    "target_env", "organize_mode", "archive_root",
    # 首次引导：AI 不需要人类的新手引导，可直接关闭并禁止自动弹出
    "onboarding_done", "auto_guides",
    # 出站 webhook：默认关闭；secret/URL 在状态与保存响应里打码
    "webhook_enabled", "webhook_url", "webhook_events",
    "webhook_secret", "webhook_allow_remote",
)


_PROXY_USERINFO_RE = re.compile(r"^(\w+://)([^/@]+):([^/@]+)@(.*)$")


def _redact_proxy_address(value):
    """代理地址可能写成 http://user:pass@host:port；自动化状态只保留可诊断的主机/端口。"""
    text = str(value or "")
    match = _PROXY_USERINFO_RE.match(text)
    if match:
        return "%s***:***@%s" % (match.group(1), match.group(4))
    if "@" in text and "://" not in text:
        return "***:***@" + text.rsplit("@", 1)[-1]
    return text


def automation_state():
    """GET /api/automation/state：一条请求拿到「配置 + 任务 + 待选文件 + 界面状态」"""
    api = _api
    if api is None:
        return {"ok": False, "msg": "RPC 未就绪"}
    cfg = getattr(api, "cfg", None) or {}
    tasks = []
    for t in list(getattr(getattr(api, "dl", None), "tasks", []) or [])[-20:]:
        try:
            d = t.to_dict()
        except Exception:
            continue
        info = d.get("info") or {}
        tasks.append({
            "id": d.get("id"), "filename": d.get("filename"), "status": d.get("status"),
            "dest_dir": d.get("dest_dir") or "", "routed_dir": info.get("routed_dir") or "",
            "route_reason": info.get("route_reason") or "",
            "progress": d.get("progress"), "downloaded": d.get("downloaded"),
            "total": d.get("total"), "error": d.get("error") or "",
            "postprocess": d.get("postprocess") if isinstance(d.get("postprocess"), dict) else None,
            "postprocess_errors": list(d.get("postprocess_errors") or [])[:20],
        })
    try:
        pending = api.get_download_file_choice()
    except Exception:
        pending = None
    try:
        choice_rev = int(api.get_download_file_choice_revision())
    except Exception:
        choice_rev = 0
    try:
        choice_log = list(api.get_download_file_choice_transitions(0) or [])[-20:]
    except Exception:
        choice_log = []
    import automation_api
    config = automation_api.redact_config_values(
        {k: cfg.get(k) for k in AUTOMATION_CONFIG_KEYS})
    config["proxy_address"] = _redact_proxy_address(config.get("proxy_address"))
    ui = dict(_ui_state)
    done = cfg.get("onboarding_done") is True
    auto_guides = cfg.get("auto_guides", True) is not False
    has_key = bool(str(cfg.get("api_key") or "").strip())
    ui["guides"] = {
        "onboarding_done": done,
        "auto_guides": auto_guides,
        "api_key_present": has_key,
        # 与前端 init 条件一致：没 Key、没完成、未关闭自动引导时，下次打开会弹
        "onboarding_pending": bool(auto_guides and not done and not has_key),
        "dismiss_rev": int(getattr(api, "_ui_dismiss_rev", 0)),
        "dismiss_targets": list(getattr(api, "_ui_dismiss_targets", []) or []),
        "overlays": list(getattr(api, "_ui_overlays", []) or []),
    }
    overlays_at = float(getattr(api, "_ui_overlays_at", 0) or 0)
    ui["guides"]["overlays_age_s"] = round(max(0.0, time.time() - overlays_at), 1) if overlays_at else None
    return {
        "ok": True, "version": _version,
        "config": config,
        "config_rev": int(getattr(api, "_cfg_rev", 0) or 0),
        "pending_file_choice": pending,
        "file_choice_rev": choice_rev,
        "file_choice_transitions": choice_log,
        "tasks": tasks,
        "ui": ui,
    }


def rpc_methods():
    """GET /api/rpc/methods：列出 /api/rpc 可调用方法及参数名（AI 不必猜方法名）"""
    return {"ok": True, "rpc": "POST /api/rpc {method, args:[...]}", "methods": _rpc_method_list()}


def _event_tasks_snapshot():
    """SSE 采样用任务快照：活动任务全量（最多 100），只读，不碰磁盘。"""
    api = _api
    dl = getattr(api, "dl", None) if api is not None else None
    rows = []
    for task in list(getattr(dl, "tasks", []) or [])[:100]:
        try:
            row = task.to_dict()
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _event_model_scan():
    api = _api
    fn = getattr(api, "get_scan_state", None) if api is not None else None
    return fn() if callable(fn) else {}


def _event_library_scan():
    api = _api
    fn = getattr(api, "lib_scan_state", None) if api is not None else None
    return fn() if callable(fn) else {}


def _rpc_method_list():
    api = _api
    if api is None:
        return []
    out = []
    for name in dir(api):
        if name.startswith("_") or name in ("pick_dir", "pick_file", "pick_files"):
            continue
        fn = getattr(api, name, None)
        if not callable(fn):
            continue
        try:
            args = list(inspect.signature(fn).parameters)
        except Exception:
            args = []
        out.append({"method": name, "args": args})
    return out


# 浏览器扩展通道的观测（AI 验证「插件到底连没连上、有没有真的提交」）
_EXT_STATE = {"download_count": 0, "archive_file_count": 0, "archive_done_count": 0,
              "last_download_at": 0.0, "last_download_origin": "", "last_download_caps": [],
              "last_download_url": ""}


def note_extension_download(origin, caps, url):
    _EXT_STATE["download_count"] += 1
    _EXT_STATE["last_download_at"] = time.time()
    _EXT_STATE["last_download_origin"] = str(origin or "")
    _EXT_STATE["last_download_caps"] = list(caps or [])
    _EXT_STATE["last_download_url"] = str(url or "")


def note_extension_archive(kind):
    if kind == "file":
        _EXT_STATE["archive_file_count"] += 1
    else:
        _EXT_STATE["archive_done_count"] += 1


def extension_status():
    last = _EXT_STATE["last_download_at"]
    return {
        "download_requests": _EXT_STATE["download_count"],
        "archive_files_written": _EXT_STATE["archive_file_count"],
        "archive_done_calls": _EXT_STATE["archive_done_count"],
        "last_download": ({
            "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last)),
            "age_s": round(time.time() - last, 1),
            "origin": _EXT_STATE["last_download_origin"],
            "caps": _EXT_STATE["last_download_caps"],
            "url": _EXT_STATE["last_download_url"],
        } if last else None),
        "active_bundle_tokens": len(_bundles),
        "tab_index": tab_index_status(),
        "dev": _EXT_DEV.copy(),
    }


def port():
    """桥服务端口（未启动返回 0）"""
    return _server.server_port if _server is not None else 0


def touch():
    global _last_seen
    _last_seen = time.time()


def set_model_status_handler(fn):
    """注册扩展下载状态查询入口（webui.Api.model_status）。

    与归档注册同一风格：桥层不 import webui。fn(items, include_updates=True) 必须是纯读，
    不得联网或阻塞长扫描；返回体直接透给扩展，额外包一层 {ok:true}。
    """
    global _model_status_fn
    _model_status_fn = fn


def set_reveal_handler(fn):
    """注册「打开已登记路径」入口（webui.Api.reveal_registered_path）。

    fn(path) 必须自己做白名单与归一化；桥层只做形状校验，不 import webui。
    与 state 查询不同，这是个动作端点：未注入时一律 503，绝不猜测路径。
    """
    global _reveal_fn
    _reveal_fn = fn


def set_archive_handlers(config_fn=None, write_fn=None, done_fn=None, check_fn=None):
    """注册归档入口：webui.Api 启动时把 config/write/done/check 注册进来

    write_fn 的调用约定：老形态 `write_fn(rel_path, data)`（两参，落归档根）；
    新形态 `write_fn(filename, data, target_dir)`（桥解析出 bundle 令牌后带绝对目录，
    根校验与纯文件名复核由 webui 侧做——令牌只证明「这是桌面自己算出来的落点」）。
    """
    global _archive_config_fn, _archive_write_fn, _archive_done_fn, _archive_check_fn
    _archive_config_fn = config_fn
    _archive_write_fn = write_fn
    _archive_done_fn = done_fn
    _archive_check_fn = check_fn


# ---------------- bundle 令牌：注册 / 解析 / 释放 ----------------

def register_bundle(abs_dir):
    """登记一个分流落点 → 返回不透明令牌；入参为空返回 ""（调用方据此不发令牌）

    只存字符串、不校验目录存在性：登记时刻目录刚被 _route_dest_dir 建好，
    真正的「还在不在允许根内」由写盘那一刻的 webui 侧复核（用户中途改设置要能生效）。
    """
    d = str(abs_dir or "").strip()
    if not d:
        return ""
    token = secrets.token_urlsafe(16)
    now = time.time()
    with _bundle_lock:
        # 惰性清扫：过期项在登记时顺手带走，省一个后台线程（表最多 256 条，遍历代价可忽略）
        for stale in [k for k, (_, dl) in _bundles.items() if dl <= now]:
            _bundles.pop(stale, None)
        _bundles[token] = (d, now + _BUNDLE_TTL)
        while len(_bundles) > _BUNDLE_MAX:
            _bundles.popitem(last=False)   # 淘汰最久没写入的那一条
    return token


def resolve_bundle(token):
    """令牌 → 绝对落点（**命中即滑动续期**）；不存在/已过期返回 None

    续期是这条链路的要害：附属文件是一路写过来的，只要还在写就不能失效。
    同时 move_to_end，让容量淘汰按「最久没写入」而不是「最早登记」——
    正在写附属文件的批次绝不会被同批后来的登记挤掉。
    """
    t = str(token or "")
    if not t:
        return None
    now = time.time()
    with _bundle_lock:
        ent = _bundles.get(t)
        if ent is None:
            return None
        d, deadline = ent
        if deadline <= now:
            _bundles.pop(t, None)          # 过期就地删，别再占容量
            return None
        _bundles[t] = (d, now + _BUNDLE_TTL)
        _bundles.move_to_end(t)
        return d


def release_bundle(token):
    """主动释放（归档结束）。未知/已过期令牌返回 False 且静默——重复释放是正常路径不是错误"""
    t = str(token or "")
    if not t:
        return False
    with _bundle_lock:
        return _bundles.pop(t, None) is not None


def clear_bundles():
    """清空注册表（测试与进程收尾用）"""
    with _bundle_lock:
        _bundles.clear()


def bundle_count():
    """当前在册令牌数（测试与诊断用，不做断言依据）"""
    with _bundle_lock:
        return len(_bundles)


# ---------------- 能力协商 ----------------

def caps_has(caps, name):
    """caps 是扩展声明的能力字符串数组（老扩展根本不发这个字段）。

    只认 list/tuple/set 里的字符串元素；脏值（裸字符串、dict、None）一律当「没声明」——
    这条门限只管一件事：放行分流。判错方向的代价是归档分家，所以宁可少放行。
    """
    if not isinstance(caps, (list, tuple, set)):
        return False
    return any(isinstance(c, str) and c.strip() == name for c in caps)


def caps_allow_bundle(caps):
    """扩展是否声明「能按 bundle 令牌写附属文件」——只有声明了，桌面端才允许分流（理由见模块头）"""
    return caps_has(caps, CAP_BUNDLE)


def is_model_url(url):
    """校验是否为 Civitai 模型页/下载链接（hostname + path 双重校验）"""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return False
    m = re.match(r"^https?://([^/]+)(/.*)?$", url)
    if not m:
        return False
    if not _URL_HOST_RE.match(m.group(1)):
        return False
    path = m.group(2) or "/"
    return bool(_URL_PATH_RE.match(path))


# ---- 浏览器标签页索引（扩展上报，只读，仅内存）----
# 只收「Civitai 模型详情页」的标题/URL/ID：不读网页正文、Cookie、API Key 或非 C 站页面。
# 契约见 docs/browser-tabs-bridge.md；索引不落盘，桌面端退出即清空。
TAB_INDEX_VERSION = 1
TAB_INDEX_MAX_BYTES = 512 * 1024
TAB_INDEX_MAX_ROWS = 2000
TAB_INDEX_MAX_INPUT_ROWS = 5000
TAB_INDEX_STALE_S = 150.0

_TAB_MODEL_PATH_RE = re.compile(r"^/models/([1-9]\d*)(?:/[^/?#]*)?$", re.I)
_TAB_BAD_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_TAB_TITLE_SUFFIX_RE = re.compile(r"\s*\|\s*Civitai\s*$", re.I)
_TAB_TITLE_PREFIX_RE = re.compile(r"^Civitai\s*\|\s*", re.I)
_EXT_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,31}$")

_TAB_INDEX = {"at": 0.0, "source": "", "ext_version": "", "counts": {}, "rows": []}
_TAB_INDEX_LOCK = threading.Lock()

# 开发态重载：桌面端置 reload_at，扩展在下次索引上报的响应里看到新时间戳后 runtime.reload()。
# 只影响「扩展何时重载」，不传页面数据、不执行任意代码；pushed_at 用于确认新代码已跑起来。
_EXT_DEV = {"reload_at": 0.0, "pushed_at": 0.0}
_EXT_DEV_LOCK = threading.Lock()


def _tab_int(value, minimum=0, maximum=None):
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    if value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _tab_clean_title(raw):
    text = _TAB_BAD_CHARS_RE.sub(" ", str(raw or ""))
    text = _TAB_TITLE_SUFFIX_RE.sub("", text).strip()
    text = _TAB_TITLE_PREFIX_RE.sub("", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:300] or "Civitai 模型"


def _tab_clean_url(raw, model_id, version_id):
    """只保留 C 站模型页需要的查询参数，返回 (干净 URL, 规范 modelId, versionId, fileId) 或 None。"""
    try:
        u = urlsplit(str(raw or "").strip())
        port = u.port
    except (TypeError, ValueError):
        return None
    if u.scheme not in ("http", "https") or port is not None:
        return None
    if not u.hostname or not _URL_HOST_RE.match(u.hostname):
        return None
    if u.username or u.password:
        return None
    match = _TAB_MODEL_PATH_RE.match(u.path or "")
    if not match:
        return None
    path_model_id = _tab_int(match.group(1), minimum=1)
    if path_model_id is None:
        return None
    if model_id is not None and model_id != path_model_id:
        return None
    try:
        query = urllib.parse.parse_qs(u.query, keep_blank_values=False)
    except ValueError:
        return None
    versions = query.get("modelVersionId") or []
    files = query.get("fileId") or []
    if len(versions) > 1 or len(files) > 1:
        return None
    query_version = _tab_int(versions[0], minimum=1) if versions else None
    if versions and query_version is None:
        return None
    if version_id is not None and query_version is not None and version_id != query_version:
        return None
    version_id = version_id if version_id is not None else query_version
    file_id = _tab_int(files[0], minimum=1) if files else None
    if files and file_id is None:
        return None
    clean_query = []
    if version_id is not None:
        clean_query.append(("modelVersionId", str(version_id)))
    if file_id is not None:
        clean_query.append(("fileId", str(file_id)))
    clean_url = urllib.parse.urlunsplit((
        u.scheme, u.netloc.lower(), u.path,
        urllib.parse.urlencode(clean_query), "",
    ))
    return clean_url, path_model_id, version_id, file_id


def _sanitize_tab_row(row):
    if not isinstance(row, dict):
        return None
    model_id = _tab_int(row.get("modelId"), minimum=1)
    version_id = _tab_int(row.get("versionId"), minimum=1)
    cleaned = _tab_clean_url(row.get("url"), model_id, version_id)
    if cleaned is None:
        return None
    url, model_id, version_id, file_id = cleaned
    out = {
        "tabId": _tab_int(row.get("tabId"), minimum=0),
        "windowId": _tab_int(row.get("windowId"), minimum=0),
        "title": _tab_clean_title(row.get("title")),
        "url": url,
        "modelId": str(model_id),
        "versionId": str(version_id) if version_id is not None else "",
        "fileId": str(file_id) if file_id is not None else "",
        "active": row.get("active") is True,
        "pinned": row.get("pinned") is True,
        "discarded": row.get("discarded") is True,
        "copies": _tab_int(row.get("copies"), minimum=1, maximum=999) or 1,
    }
    return out


def _tab_index_counts(raw, accepted):
    raw = raw if isinstance(raw, dict) else {}

    def num(key, fallback=0):
        value = _tab_int(raw.get(key), minimum=0, maximum=10 ** 9)
        return value if value is not None else fallback

    return {
        "tabs": num("tabs"),
        "civitai": num("civitai"),
        "models": len(accepted),
        "windows": num("windows"),
        "discarded": num("discarded"),
    }


def store_tab_index(payload, origin=""):
    """校验并替换内存索引；返回 {ok, accepted, rejected, received_at} 或抛 ValueError。"""
    if not isinstance(payload, dict):
        raise ValueError("body 必须是 JSON 对象")
    version = payload.get("v", TAB_INDEX_VERSION)
    if _tab_int(version, minimum=1) != TAB_INDEX_VERSION:
        raise ValueError("不支持的索引版本")
    rows = payload.get("tabs")
    if not isinstance(rows, list):
        raise ValueError("tabs 必须是数组")
    if len(rows) > TAB_INDEX_MAX_INPUT_ROWS:
        raise ValueError("tabs 超过上限 %d" % TAB_INDEX_MAX_INPUT_ROWS)
    accepted, rejected, seen = [], 0, set()
    for row in rows:
        clean = _sanitize_tab_row(row)
        if clean is None:
            rejected += 1
            continue
        key = (clean["url"], clean["tabId"])
        if key in seen:
            rejected += 1
            continue
        seen.add(key)
        accepted.append(clean)
        if len(accepted) >= TAB_INDEX_MAX_ROWS:
            break
    if rows and not accepted:
        raise ValueError("没有合法的 Civitai 模型页")
    source = str(payload.get("source") or "").strip().lower()[:32]
    if source not in ("chrome-extension", "firefox-extension"):
        source = "firefox-extension" if origin.startswith("moz-extension://") else "chrome-extension"
    ext_version = str(payload.get("ext_version") or "").strip()
    if not _EXT_VERSION_RE.match(ext_version):
        ext_version = ""
    at = time.time()
    with _TAB_INDEX_LOCK:
        _TAB_INDEX.update({
            "at": at,
            "source": source,
            "ext_version": ext_version,
            "counts": _tab_index_counts(payload.get("counts"), accepted),
            "rows": accepted,
        })
    return {"ok": True, "received_at": at, "accepted": len(accepted), "rejected": rejected}


def tab_index_snapshot():
    with _TAB_INDEX_LOCK:
        return {
            "at": _TAB_INDEX["at"],
            "source": _TAB_INDEX["source"],
            "ext_version": _TAB_INDEX.get("ext_version", ""),
            "counts": dict(_TAB_INDEX["counts"]),
            "rows": [dict(row) for row in _TAB_INDEX["rows"]],
        }


def search_tab_index(query="", limit=50, include_discarded=False, window_id=None, active_only=False):
    """只读搜索内存索引；返回 Agent 可直接消费的结构（URL 已清洗）。"""
    snap = tab_index_snapshot()
    rows = snap["rows"]
    if not include_discarded:
        rows = [row for row in rows if not row["discarded"]]
    if window_id is not None:
        rows = [row for row in rows if row["windowId"] == window_id]
    if active_only:
        rows = [row for row in rows if row["active"]]
    tokens = [token for token in str(query or "").lower().split() if token]
    if tokens:
        def hit(row):
            hay = " ".join((row["title"], row["url"], row["modelId"], row["versionId"])).lower()
            return all(token in hay for token in tokens)
        rows = [row for row in rows if hit(row)]
    try:
        limit = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        limit = 50
    age_s = max(0.0, time.time() - snap["at"]) if snap["at"] else None
    stale = snap["at"] == 0 or (age_s is not None and age_s > TAB_INDEX_STALE_S)
    out = {
        "ok": True,
        "at": snap["at"],
        "age_s": round(age_s, 1) if age_s is not None else None,
        "stale": stale,
        "source": snap["source"],
        "ext_version": snap["ext_version"],
        "counts": snap["counts"],
        "total": len(rows),
        "matched": min(len(rows), limit),
        "items": rows[:limit],
    }
    if not snap["at"]:
        out["hint"] = "扩展尚未上报标签页索引：确认浏览器扩展已安装并启用，且桌面端正在运行"
    elif stale:
        out["hint"] = "索引已过期：切换/打开浏览器标签页或重载扩展后会重新上报"
    return out


def tab_index_status():
    snap = tab_index_snapshot()
    age_s = max(0.0, time.time() - snap["at"]) if snap["at"] else None
    return {
        "at": snap["at"],
        "age_s": round(age_s, 1) if age_s is not None else None,
        "stale": snap["at"] == 0 or (age_s is not None and age_s > TAB_INDEX_STALE_S),
        "source": snap["source"],
        "ext_version": snap["ext_version"],
        "counts": snap["counts"],
        "accepted": len(snap["rows"]),
        "pushed_at": _EXT_DEV["pushed_at"],
    }


# bundle 形态的 filename 语法闸（纯文件名）。为什么桥层也要判一次：
# webui 侧判的是「落在哪个根里」（策略），这里的「协议形状」即使注册方漏判也拦住；
# 两边都不信扩展过滤过（扩展改了/被人改了不能变成写盘逃逸面）。
_FILENAME_BAD_MSG = "filename 必须是纯文件名（不能含路径分隔符、盘符、UNC、.. 或控制字符）"


def bundle_filename_error(name):
    """纯文件名校验：合法返回 None，非法返回给扩展看的一句话（400 的 msg）"""
    if not isinstance(name, str):
        return _FILENAME_BAD_MSG
    s = name.strip()
    if not s:
        return "filename 不能为空"
    if s in (".", "..") or s.startswith(".."):
        return _FILENAME_BAD_MSG
    if "/" in s or "\\" in s:
        return _FILENAME_BAD_MSG
    # 盘符（"C:x" 在 POSIX 上是合法文件名，却是 Windows 逃逸面）与 UNC
    if re.match(r"^[A-Za-z]:", s) or s.startswith("//"):
        return _FILENAME_BAD_MSG
    # 控制字符（含 NUL）：Windows 会静默截断/拒绝，宁可不写
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in s):
        return _FILENAME_BAD_MSG
    # 首尾空白：Windows 会静默去掉尾部空格，两边算出不同文件名就等于重复归档
    if s != name:
        return _FILENAME_BAD_MSG
    return None


def _handler_wants_caps(fn):
    """handler 自己是否声明了 caps（关键字或位置参数皆可）。老 handler 只收两参，绝不硬塞第三参。"""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    p = params.get("caps")
    if p is not None and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
        return True
    return any(x.kind == x.VAR_KEYWORD for x in params.values())


def _call_download_handler(fn, url, dest_subdir, caps):
    """按 handler 的声明调用：caps 非空且 handler 收得住才带关键字传进去。

    门限的判定权交给 webui（它才知道分流与令牌怎么发），桥只负责「把扩展声明的能力原样送到」，
    老 handler（含测试里的假实现）不会因为多一个参数而 TypeError。
    """
    if caps and _handler_wants_caps(fn):
        return fn(url, dest_subdir, caps=caps) or {}
    return fn(url, dest_subdir) or {}


def origin_allowed(origin):
    """CORS 来源白名单：无 Origin（curl/本地工具）、浏览器扩展（Chrome/Firefox）、本机页面允许"""
    if not origin or origin == "null" or origin.startswith("file:"):
        return True
    if _EXT_ORIGIN_RE.match(origin):
        return True
    if _LOCAL_ORIGIN_RE.match(origin):
        return True
    return False


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静默日志，避免刷控制台

    def _cors(self, origin):
        if origin and origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
        else:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")

    def _json(self, code, obj, origin):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors(origin)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _origin(self):
        return self.headers.get("Origin") or ""

    def _check_origin(self, origin):
        if origin and not origin_allowed(origin):
            self._json(403, {"ok": False, "msg": "来源被拒绝"}, origin)
            return False
        return True

    def do_OPTIONS(self):
        origin = self._origin()
        if origin and not origin_allowed(origin):
            return self._json(403, {"ok": False, "msg": "来源被拒绝"}, origin)
        self.send_response(204)
        self._cors(origin)
        self.end_headers()

    def do_GET(self):
        origin = self._origin()
        if not self._check_origin(origin):
            return
        path = urlsplit(self.path).path
        touch()
        if path == "/api/health":
            d = {"ok": True, "app": "CivitaiFreeTool", "version": _version}
            try:
                d.update(_ui_state)          # mode / window / pid：看门狗与诊断用
            except Exception:
                pass
            return self._json(200, d, origin)
        if path == "/api/heartbeat":
            return self._json(200, {"ok": True}, origin)
        if path == "/api/rpc/methods":
            return self._json(200, rpc_methods(), origin)
        if path == "/api/automation/events/stream":
            return self._automation_sse(origin)
        if path == "/api/ext/tabs":
            return self._handle_ext_tabs_get(origin)
        if path == "/api/ext/dev/state":
            return self._handle_ext_dev_state(origin)
        if path.startswith("/api/automation"):
            return self._automation(origin, "GET")
        if path == "/api/archive/config":
            fn = _archive_config_fn
            if fn is None:
                return self._json(500, dict(_NOT_READY), origin)
            try:
                cfg = fn() or {}
            except Exception as e:
                return self._json(500, {"ok": False, "msg": "归档配置读取失败: %s" % str(e)[:120]}, origin)
            return self._json(200, {"ok": True, "archive_root": cfg.get("archive_root") or "",
                                    "download_dir": cfg.get("download_dir") or ""}, origin)
        if path == "/api/library/check":
            fn = _archive_check_fn
            if fn is None:
                return self._json(500, dict(_NOT_READY), origin)
            # query 参数解析（self.path 含 query string，先切 path 再解析）
            try:
                qs = urllib.parse.urlparse(self.path).query
                vid = (urllib.parse.parse_qs(qs).get("version_id") or [""])[0]
            except Exception:
                vid = ""
            try:
                result = fn(vid) or {}
            except Exception as e:
                return self._json(500, {"ok": False, "msg": "模型库检查失败: %s" % str(e)[:120]}, origin)
            if isinstance(result, dict) and result.get("ok") is False and "found" not in result:
                return self._json(400, result, origin)
            out = {"ok": True, "found": bool(result.get("found")),
                   "paths": list(result.get("paths") or [])}
            return self._json(200, out, origin)
        if path == "/api/media":
            # 显式排在 _serve_static 之前：/api/ 前缀本就不会走静态，排序只为可读与更稳
            return self._media_stream(origin)
        # 浏览器模式：直接由本服务提供界面文件（只在注册了 web 根目录时启用）
        if _web_root and not path.startswith("/api/"):
            return self._serve_static(path)
        return self._json(404, {"ok": False, "msg": "Not Found"}, origin)

    def _serve_static(self, path):
        """提供 web/ 目录里的界面文件（防目录穿越；只服务界面自身，不外泄其他路径）"""
        rel = unquote(path).lstrip("/") or "index.html"
        root = os.path.normpath(_web_root)
        full = os.path.normpath(os.path.join(root, rel))
        if not (full == root or full.startswith(root + os.sep)):
            return self._json(403, {"ok": False, "msg": "禁止访问"}, "")
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            return self._json(404, {"ok": False, "msg": "文件不存在"}, "")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        try:
            with open(full, "rb") as f:
                body = f.read()
        except Exception as e:
            try:
                if _api is not None and hasattr(_api, "log_error"):
                    import traceback as _tb
                    _api.log_error("rpc", "%s\n%s" % (str(e)[:200], _tb.format_exc()))
            except Exception:
                pass
            return self._json(500, {"ok": False, "msg": str(e)[:120]}, "")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _media_error(self, code, msg, origin, content_range=None):
        """媒体端点的错误体：中文、截断、绝不带堆栈（响应可能在流中途之前发出）"""
        body = json.dumps({"ok": False, "msg": str(msg)[:_MEDIA_MSG_MAX]}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if content_range:
            self.send_header("Content-Range", content_range)
        self._cors(origin)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _media_head(self, code, full_path, length, content_range):
        """成功响应头：不加任何 CORS（媒体走 <img>/<video src>，与 _serve_static 一致）"""
        ext = os.path.splitext(full_path)[1].lower()
        ctype = _MEDIA_TYPES.get(ext) or mimetypes.guess_type(full_path)[0] or "video/mp4"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        # 媒体文件按不可变设计（同名覆盖也换 mtime），与界面资源的 no-store 有意不同：
        # 视频拖动依赖可缓存 + Range，no-store 会让每次 seek 都回源读盘。
        self.send_header("Cache-Control", "private, max-age=3600")
        if content_range:
            self.send_header("Content-Range", content_range)
        self.end_headers()

    def _media_stream(self, origin):
        """GET /api/media?p=<urlencode(绝对路径)>：资源库图片/视频只读流式（支持 Range）

        校验一律在打开文件之前完成；越界与「文件不存在」同码同文案（不泄露根目录内容）。
        """
        fn = _media_roots_fn
        if fn is None:
            # A3 未注入根提供者＝资源库没配置：直接拒，且不碰磁盘（契约 §8 只允许 403/404）
            return self._media_error(403, "资源库未配置", origin)
        try:
            qs = urllib.parse.urlparse(self.path).query
            p = (urllib.parse.parse_qs(qs, keep_blank_values=True).get("p") or [""])[0]
        except Exception:
            p = ""
        if not _media_shape_ok(p):
            return self._media_error(403, "非法路径", origin)
        try:
            real = os.path.realpath(p)          # 解析整条符号链接链，防根内软链逃逸
        except (OSError, ValueError):
            real = ""
        try:
            roots = list(fn() or [])
        except Exception as e:
            roots = []
            _media_log(e)                       # 注入方异常是服务端问题：进日志面，不喷控制台
        if not real or not _media_within_roots(real, roots):
            return self._media_error(404, _MEDIA_DENY, origin)
        try:
            if not os.path.isfile(real):
                return self._media_error(404, _MEDIA_DENY, origin)
            fh = open(real, "rb")
        except (OSError, ValueError):
            return self._media_error(404, _MEDIA_DENY, origin)
        # 从这里起响应头可能已发出，任何异常都只能静默收尾（否则 socketserver 会喷堆栈）
        try:
            with fh:
                try:
                    size = os.fstat(fh.fileno()).st_size  # 用已打开的 fd 取尺寸，避开 stat/open 间的竞态
                except OSError:
                    return self._media_error(404, _MEDIA_DENY, origin)
                rng = self.headers.get("Range")
                if not rng:
                    self._media_head(200, real, size, None)
                    _stream_media(self, fh, 0, size)
                    return
                span = parse_media_range(rng, size)
                if span is None:
                    self._media_error(416, "范围无效", origin, "bytes */%d" % size)
                    return
                start, end = span
                self._media_head(206, real, end - start + 1, "bytes %d-%d/%d" % (start, end, size))
                _stream_media(self, fh, start, end - start + 1)
        except Exception as e:
            if not _media_is_abort(e):
                _media_log(e)
            try:
                self.close_connection = True    # 半截响应不能再复用这条连接
            except Exception:
                pass

    def _automation_sse(self, origin):
        """SSE 领域事件流：标准 event/id/data + 心跳 + Last-Event-ID 重放。

        传输层细节（chunked、断开、写超时）留在桥；事件语义在 automation_events。
        """
        import ai_contract
        import automation_events

        split = urlsplit(self.path)
        query = urllib.parse.parse_qs(split.query)
        raw_since = self.headers.get("Last-Event-ID")
        if raw_since is None or not str(raw_since).strip():
            raw_since = (query.get("since") or [""])[0]
        since = None
        if str(raw_since or "").strip():
            try:
                since = int(str(raw_since).strip())
                if since < 0:
                    raise ValueError
            except (TypeError, ValueError):
                return self._json(400, {
                    "ok": False, "msg": "since / Last-Event-ID 必须是非负整数",
                    "hint": "用上一条事件的 id（seq）作为 Last-Event-ID；首次连接可省略",
                }, origin)

        raw_types = str((query.get("types") or [""])[0] or "").strip()
        types = set(ai_contract.DOMAIN_EVENT_TYPES)
        if raw_types:
            if raw_types.lower() in ("all", "*"):
                types = None
            else:
                parts = {part for part in re.split(r"[,\s]+", raw_types) if part}
                known = set(ai_contract.EVENT_TYPES) | {
                    item["group"] for item in ai_contract.EVENTS}
                unknown = sorted(parts - known)
                if unknown:
                    return self._json(400, {
                        "ok": False, "msg": "未知事件类型: " + ", ".join(unknown),
                        "hint": "types 可写事件名、组名（task/scan/ui/config/...）或 all",
                        "known": sorted(known),
                    }, origin)
                types = parts or None

        snapshot = str((query.get("snapshot") or ["1"])[0] or "1").lower() not in (
            "0", "false", "no", "off")
        hub = automation_events.get_hub()
        if hub is None:
            return self._json(503, {
                "ok": False, "msg": "SSE 事件总线未就绪",
                "hint": "升级桌面端到 >= %s 后重试" % ai_contract.MIN_APP_VERSION,
            }, origin)
        if str(getattr(self, "request_version", "")).upper() != "HTTP/1.1":
            return self._json(400, {
                "ok": False, "msg": "SSE 需要 HTTP/1.1",
                "hint": "使用支持 chunked SSE 的本地 Agent 客户端；HTTP/1.0 不能可靠分帧",
            }, origin)
        if hub.active_clients() >= hub.max_clients:
            return self._json(429, {
                "ok": False, "msg": "SSE 连接数已达上限",
                "hint": "关闭空闲的 SSE 客户端后重试", "limit": hub.max_clients,
            }, origin)

        # 先把 header 刷出去再订阅：客户端在 header 前断开时不会留下占槽位的 Subscription。
        self.protocol_version = "HTTP/1.1"
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Accel-Buffering", "no")
            self._cors(origin)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

        subscribed = hub.subscribe(since=since, types=types)
        if subscribed is None:
            try:
                msg = b": SSE connection limit reached\n\n"
                self.wfile.write(("%X\r\n" % len(msg)).encode("ascii") + msg + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass
            self.close_connection = True
            return
        sub, info = subscribed

        try:
            self.connection.settimeout(max(5.0, hub.heartbeat_s * 2))
        except Exception:
            pass

        last_write = [time.time()]

        def write_chunk(data):
            body = data.encode("utf-8") if isinstance(data, str) else data
            self.wfile.write(("%X\r\n" % len(body)).encode("ascii") + body + b"\r\n")
            self.wfile.flush()
            last_write[0] = time.time()

        def emit(event):
            payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            write_chunk("id: %s\nevent: %s\ndata: %s\n\n" % (
                event.get("seq", 0), event.get("type", "message"), payload))

        def client_alive():
            """SSE 客户端不再发请求；用 peek 检测 FIN，及时释放订阅名额。"""
            try:
                readable, _, _ = select.select([self.connection], [], [], 0)
                if not readable:
                    return True
                return bool(self.connection.recv(1, socket.MSG_PEEK))
            except (OSError, ValueError):
                return False

        def control(event_type, data, seq=None):
            return {
                "seq": int(info.get("last_seq", 0) if seq is None else seq),
                "instance_id": hub.instance_id,
                "type": event_type,
                "group": "stream",
                "at": automation_events.now_iso(),
                "data": data if isinstance(data, dict) else {},
            }

        try:
            emit(control("stream.ready", {
                "instance_id": hub.instance_id,
                "last_seq": int(info.get("last_seq", 0)),
                "oldest_seq": int(info.get("oldest_seq", 0)),
                "heartbeat_s": hub.heartbeat_s,
                "types": sorted(types) if types else ["all"],
            }))
            if info.get("reset"):
                emit(control("stream.reset", {
                    "instance_id": hub.instance_id,
                    "oldest_seq": int(info.get("oldest_seq", 0)),
                    "last_seq": int(info.get("last_seq", 0)),
                }))
            if snapshot and (since is None or info.get("reset")):
                snap = automation_state()
                emit(control("state.snapshot", snap))
            for event in info.get("replay") or []:
                emit(event)

            while True:
                if sub.closed.is_set() and sub.queue.empty():
                    break
                try:
                    event = sub.queue.get(timeout=0.5)
                except queue.Empty:
                    if sub.closed.is_set():
                        break
                    if not client_alive():
                        break
                    if time.time() - last_write[0] >= hub.heartbeat_s:
                        write_chunk(": keep-alive %s\n\n" % automation_events.now_iso())
                    continue
                dropped = sub.take_dropped()
                if dropped:
                    emit(control("stream.overflow", {"dropped": int(dropped)},
                                 seq=event.get("seq")))
                emit(event)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        except Exception:
            pass
        finally:
            hub.unsubscribe(sub)
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass
            self.close_connection = True

    def _drain_rejected_body(self, length, cap):
        """回 413 前排空已发送的字节，否则客户端只看到连接中断、拿不到错误信封。

        Windows 上若在接收缓冲仍有未读数据时关闭 socket，会直接 RST，httplib/requests
        读响应头就失败。上限只排到 2 倍 cap，更大的请求放弃排空——不给对端免费的带宽消耗。
        """
        if length > 2 * cap:
            return
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _automation(self, origin, method):
        """所有 /api/automation* 请求的统一入口：解析 query/body → automation_api.dispatch

        每次调用记一条事件（AI 自检：请求到没到、耗时多少、状态码），可用
        GET /api/automation/logs?source=events 取回。
        """
        import automation_api
        split = urlsplit(self.path)
        path = split.path
        query = urllib.parse.parse_qs(split.query)
        body = None
        if method == "POST":
            # 图片上传放宽到 8MB（/tool 是 MCP inspect_image 的入口）；其余自动化请求仍守 1MB，
            # 避免把桥变成任意大文件入口。
            import ai_contract
            tool_name = ""
            if path.startswith("/api/automation/tool/"):
                tool_name = unquote(path[len("/api/automation/tool/"):]).strip("/")
            large_body = (path in ("/api/automation/vision/analyze", "/api/automation/tool") or
                          tool_name in ai_contract.LARGE_BODY_TOOLS)
            cap = (8 << 20) if large_body else (1 << 20)
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > cap:
                    self._drain_rejected_body(length, cap)
                    return self._json(413, {"ok": False, "msg": "请求体过大",
                                            "limit_bytes": cap,
                                            "hint": "图片分析请压缩到 8MB 以内或改用 path"}, origin)
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8", "replace") or "{}")
            except Exception:
                return self._json(400, {"ok": False, "msg": "请求格式错误"}, origin)
            if not isinstance(body, dict):
                return self._json(400, {"ok": False, "msg": "body 必须是 JSON 对象"}, origin)
        t0 = time.time()
        try:
            result = automation_api.dispatch(path, method, query, body, _api, self._automation_ctx())
        except Exception as e:
            result = (500, {"ok": False, "msg": "自动化接口异常: %s" % str(e)[:200]})
        if result is None:
            result = (404, {"ok": False, "msg": "Not Found"})
        status, payload = result
        payload = payload if isinstance(payload, dict) else {"ok": bool(status == 200), "result": payload}
        automation_api.record_event(method, path, status, (time.time() - t0) * 1000.0,
                                    payload.get("msg") or ("ok" if payload.get("ok") else ""))
        return self._json(status, payload, origin)

    def _automation_ctx(self):
        import config
        return {
            "version": _version,
            "host": self.headers.get("Host") or "",
            "state": automation_state,
            "extension": extension_status,
            "config_keys": AUTOMATION_CONFIG_KEYS,
            "rpc_methods": _rpc_method_list(),
            "bridge_port": port(),
            "data_dir": getattr(config, "APP_DIR", ""),
            "error_log": os.path.join(getattr(config, "APP_DIR", ""), "error.log"),
            "save_config": self._automation_save_config,
        }

    def _automation_save_config(self, body):
        """POST /api/automation/config 的实现：只改白名单键，走 Api.save_config

        非法键整条拒绝，不做部分生效——自动化脚本最怕「改了一半」，静默半成功会让后续断言
        落在错误前提上。body 两种形态都收：{"config": {...}} 或直接 {...}。
        """
        api = _api
        if api is None:
            return 500, dict(_NOT_READY)
        b = body or {}
        patch = b.get("config") if isinstance(b.get("config"), dict) else b
        if not isinstance(patch, dict) or not patch:
            return 400, {"ok": False, "msg": "config 必须是非空对象"}
        bad = sorted(k for k in patch if k not in AUTOMATION_CONFIG_KEYS)
        if bad:
            return 400, {"ok": False, "msg": "不允许修改的配置项: " + ", ".join(bad),
                         "allowed": list(AUTOMATION_CONFIG_KEYS)}
        try:
            import webhook_dispatch
            webhook_dispatch.validate_patch(patch, getattr(api, "cfg", None) or {})
        except webhook_dispatch.WebhookConfigError as e:
            return 400, {"ok": False, "msg": str(e),
                         "hint": "webhook 配置整条校验：非法键/URL/密钥/事件名都会拒绝，不做部分生效"}
        try:
            ok = api.save_config(dict(patch))
        except Exception as e:
            return 500, {"ok": False, "msg": "保存失败: %s" % str(e)[:120]}
        if ok is False:
            return 500, {"ok": False, "msg": "保存失败（配置未变更）"}
        try:
            import automation_events
            import webhook_dispatch
            webhook_dispatch.refresh_from_config(getattr(api, "cfg", None) or {},
                                                automation_events.get_hub())
        except Exception:
            pass
        import automation_api
        cfg = getattr(api, "cfg", None) or {}
        return 200, {"ok": True,
                     "config": automation_api.redact_config_values(
                         {k: cfg.get(k) for k in AUTOMATION_CONFIG_KEYS})}

    def _rpc(self, origin):
        """浏览器模式通用方法调用：{method, args:[...]} → {result} / {error}"""
        if _api is None:
            return self._json(500, {"error": "RPC 未就绪"}, origin)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 16 * 1024 * 1024)) if length else b"{}"
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
            method = str(data.get("method") or "")
            args = data.get("args") or []
            if not isinstance(args, list):
                args = [args]
        except Exception:
            return self._json(400, {"error": "请求格式错误"}, origin)
        if (not method) or method.startswith("_") or method in ("pick_dir", "pick_file", "pick_files"):
            return self._json(400, {"error": "不允许的方法: %s" % method[:40]}, origin)
        fn = getattr(_api, method, None)
        if not callable(fn):
            return self._json(400, {"error": "方法不存在: %s" % method[:40]}, origin)
        try:
            result = fn(*args)
        except Exception as e:
            return self._json(500, {"error": "%s: %s" % (type(e).__name__, str(e)[:200])}, origin)
        return self._json(200, {"result": result}, origin)

    def _handle_archive_file(self, origin):
        """POST /api/archive/file，body 上限 128MB，两种形态（详见模块头）：
           旧 `{rel_path, data_b64}`            → 相对归档根，行为与历史逐字一致
           新 `{bundle, filename, data_b64}`    → 落点 = bundle 令牌登记的绝对目录
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, _ARCHIVE_FILE_MAX)) if length else b""
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
            rel_path = data.get("rel_path") or ""
            data_b64 = data.get("data_b64") or ""
            bundle = data.get("bundle") or ""
            filename = data.get("filename") or ""
        except Exception:
            return self._json(400, {"ok": False, "msg": "请求格式错误"}, origin)
        use_bundle = bool(bundle)
        target_dir = ""
        if use_bundle:
            # bundle 形态自己的一套必填校验：缺 filename 就是协议错，绝不退化成写归档根
            if not filename or not data_b64:
                return self._json(400, {"ok": False, "msg": "bundle 形态需要 filename 与 data_b64"}, origin)
            bad = bundle_filename_error(filename)
            if bad:
                return self._json(400, {"ok": False, "msg": bad}, origin)
        else:
            if not rel_path or not data_b64:
                return self._json(400, {"ok": False, "msg": "rel_path 与 data_b64 不能为空"}, origin)
        try:
            blob = base64.b64decode(data_b64, validate=True)
        except Exception:
            return self._json(400, {"ok": False, "msg": "data_b64 解码失败"}, origin)
        if len(blob) > _ARCHIVE_RAW_MAX:
            return self._json(400, {"ok": False, "msg": "文件过大（解码后超过 64MB）"}, origin)
        fn = _archive_write_fn
        if fn is None:
            return self._json(500, dict(_NOT_READY), origin)
        if use_bundle:
            # 解析放在解码/尺寸校验之后：坏请求不该把令牌寿命续满
            # 令牌不存在/已过期/已释放 → 400 + 明说原因（扩展会带着这条 msg 降级到浏览器下载目录，
            # 用户至少能在归档日志里看到「令牌没了」而不是莫名其妙）
            target_dir = resolve_bundle(bundle)
            if not target_dir:
                return self._json(400, {"ok": False, "msg": "落点令牌无效或已过期，请重新发送模型"}, origin)
            rel_path = filename
        try:
            saved = fn(rel_path, blob, target_dir) if target_dir else fn(rel_path, blob)
        except Exception as e:
            return self._json(400, {"ok": False, "msg": str(e)[:200]}, origin)
        if saved is False or saved is None:
            return self._json(400, {"ok": False, "msg": "写入失败"}, origin)
        note_extension_archive("file")
        return self._json(200, {"ok": True, "path": str(saved)}, origin)

    def _handle_archive_done(self, origin):
        """POST /api/archive/done：body {dir_name, bundle?}，尽力而为语义。

        dir_name 允许为空：分流后它不再代表路径（本体已在 ComfyUI 类型目录里），
        「结束」这件事本身就是触发模型库重扫的信号。扩展的批量结束恒传 {dir_name:''}，
        旧实现在这里返回 400，导致批量归档后的重扫从未真正触发（本次一并修掉）。
        老客户端传真实 dirName 行为不变。带 bundle 时顺手释放该令牌（不带就交给 TTL/容量回收）。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 65536)) if length else b""
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
            dir_name = data.get("dir_name") or ""
            bundle = data.get("bundle") or ""
        except Exception:
            return self._json(400, {"ok": False, "msg": "请求格式错误"}, origin)
        fn = _archive_done_fn
        if fn is None:
            return self._json(500, dict(_NOT_READY), origin)
        if bundle:
            release_bundle(bundle)
        note_extension_archive("done")
        try:
            fn(dir_name)
        except Exception as e:
            # 尽力而为：done 失败也返回 ok:true + msg
            return self._json(200, {"ok": True, "msg": "重扫触发失败: %s" % str(e)[:120]}, origin)
        return self._json(200, {"ok": True}, origin)

    def _handle_model_status(self, origin):
        """POST /api/model/status：扩展批量查询本地下载状态（只读、不联网）。

        入参 {items:[{model_id,version_id,url}], include_updates?:bool}，最多 500 项。
        单条缺 ID 走 200 + 逐条 unknown，不因一条坏数据整批失败。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 65536)) if length else b""
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            return self._json(400, {"ok": False, "msg": "请求格式错误"}, origin)
        if not isinstance(data, dict):
            return self._json(400, {"ok": False, "msg": "请求格式错误"}, origin)
        items = data.get("items")
        if not isinstance(items, list):
            return self._json(400, {"ok": False, "msg": "items 必须是数组"}, origin)
        if len(items) > 500:
            return self._json(400, {"ok": False, "msg": "一次最多查询 500 个模型"}, origin)
        fn = _model_status_fn
        if fn is None:
            return self._json(500, {"ok": False, "msg": "状态服务未就绪"}, origin)
        include_updates = data.get("include_updates", True) is not False
        try:
            result = fn(items, include_updates=include_updates) or {}
        except Exception as e:
            return self._json(500, {"ok": False, "msg": "状态查询失败: %s" % str(e)[:120]}, origin)
        if not isinstance(result, dict):
            return self._json(500, {"ok": False, "msg": "状态查询返回格式异常"}, origin)
        return self._json(200, {"ok": True, **result} if result.get("ok") is not False else result, origin)

    def _handle_ext_tabs(self, origin):
        """POST /api/ext/tabs：扩展上报打开的 C 站模型页索引（只读、仅内存）。

        只接受浏览器扩展来源；普通网页即使能打到回环端口也不能写索引。
        """
        if origin and not _EXT_ORIGIN_RE.match(origin):
            return self._json(403, {"ok": False, "msg": "只接受浏览器扩展上报"}, origin)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > TAB_INDEX_MAX_BYTES:
            self._drain_rejected_body(length, TAB_INDEX_MAX_BYTES)
            return self._json(413, {"ok": False, "msg": "标签页索引过大",
                                    "limit_bytes": TAB_INDEX_MAX_BYTES}, origin)
        try:
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            return self._json(400, {"ok": False, "msg": "请求格式错误"}, origin)
        try:
            result = store_tab_index(data, origin)
        except ValueError as e:
            return self._json(400, {"ok": False, "msg": str(e)}, origin)
        with _EXT_DEV_LOCK:
            _EXT_DEV["pushed_at"] = time.time()
            reload_at = _EXT_DEV["reload_at"]
        if reload_at:
            result["dev_reload_at"] = reload_at
        return self._json(200, result, origin)

    def _handle_ext_dev_state(self, origin):
        """开发态：扩展与重载标记的只读状态。"""
        with _EXT_DEV_LOCK:
            reload_at = _EXT_DEV["reload_at"]
            pushed_at = _EXT_DEV["pushed_at"]
        return self._json(200, {
            "ok": True, "reload_at": reload_at, "pushed_at": pushed_at,
            "pending": bool(reload_at > pushed_at),
            "extension": tab_index_status(),
        }, origin)

    def _handle_ext_dev_reload(self, origin):
        """开发态：请求已加载的扩展重载（只改时间戳，不传代码/数据）。

        全局 Origin 白名单已在本层之前执行；网页来源进不来。
        """
        at = time.time()
        with _EXT_DEV_LOCK:
            _EXT_DEV["reload_at"] = at
        return self._json(200, {
            "ok": True, "reload_at": at,
            "hint": "扩展下一次索引上报后会 runtime.reload()；内容脚本仍需刷新对应页面",
        }, origin)

    def _handle_ext_tabs_get(self, origin):
        """GET /api/ext/tabs：本机 Agent 只读查询索引。"""
        try:
            query = urllib.parse.parse_qs(urlsplit(self.path).query)
        except Exception:
            query = {}

        def one(key, default=""):
            values = query.get(key) or [default]
            return values[0]

        limit = _tab_int(one("limit", "50"), minimum=1, maximum=200) or 50
        window_raw = one("window_id", "")
        window_id = _tab_int(window_raw, minimum=0) if window_raw != "" else None
        truthy = ("1", "true", "yes", "on")
        out = search_tab_index(
            one("q", ""), limit=limit,
            include_discarded=one("include_discarded", "").lower() in truthy,
            window_id=window_id,
            active_only=one("active", "").lower() in truthy,
        )
        return self._json(200, out, origin)

    def _handle_reveal(self, origin):
        """POST /api/reveal：打开桌面端已登记的落点。

        只做形状校验并把 path 交给 Api.reveal_registered_path；白名单、存在性、
        跨平台打开全在 webui 侧。未登记一律 403 —— 这不是任意路径浏览器。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 65536)) if length else b""
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            return self._json(400, {"ok": False, "msg": "请求格式错误"}, origin)
        if not isinstance(data, dict):
            return self._json(400, {"ok": False, "msg": "请求格式错误"}, origin)
        path = data.get("path")
        if not isinstance(path, str) or not path.strip():
            return self._json(400, {"ok": False, "msg": "path 必须是非空字符串"}, origin)
        if len(path) > 4096:
            return self._json(400, {"ok": False, "msg": "路径过长"}, origin)
        fn = _reveal_fn
        if fn is None:
            return self._json(503, {"ok": False, "msg": "打开目录服务未就绪，请升级 CFT"}, origin)
        try:
            result = fn(path.strip()) or {}
        except Exception as e:
            return self._json(500, {"ok": False, "msg": "打开目录失败: %s" % str(e)[:120]}, origin)
        if not isinstance(result, dict):
            return self._json(500, {"ok": False, "msg": "打开目录返回格式异常"}, origin)
        return self._json(200 if result.get("ok") is True else 403, result, origin)

    def do_POST(self):
        origin = self._origin()
        if not self._check_origin(origin):
            return
        path = urlsplit(self.path).path
        touch()
        if path == "/api/rpc":
            return self._rpc(origin)
        if path == "/api/reveal":
            return self._handle_reveal(origin)
        if path == "/api/archive/file":
            return self._handle_archive_file(origin)
        if path == "/api/archive/done":
            return self._handle_archive_done(origin)
        if path.startswith("/api/automation"):
            return self._automation(origin, "POST")
        if path == "/api/ext/tabs":
            return self._handle_ext_tabs(origin)
        if path == "/api/ext/dev/reload":
            return self._handle_ext_dev_reload(origin)
        if path == "/api/model/status":
            return self._handle_model_status(origin)
        if path != "/api/download":
            return self._json(404, {"ok": False, "msg": "Not Found"}, origin)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 65536)) if length else b""
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
            url = data.get("url") or ""
            dest_subdir = data.get("dest_subdir") or ""
            caps = data.get("caps") or []
        except Exception:
            return self._json(400, {"ok": False, "msg": "请求格式错误"}, origin)
        url = (url or "").strip()
        if not url:
            return self._json(400, {"ok": False, "msg": "链接不能为空"}, origin)
        if not is_model_url(url):
            return self._json(400, {"ok": False, "msg": "仅支持 Civitai 模型页链接"}, origin)
        note_extension_download(origin, caps, url)
        fn = _handler
        if fn is None:
            return self._json(500, {"ok": False, "msg": "下载服务未就绪"}, origin)
        try:
            result = _call_download_handler(fn, url, dest_subdir, caps)
        except Exception as e:
            return self._json(500, {"ok": False, "msg": "下载启动失败: %s" % str(e)[:120]}, origin)
        if isinstance(result, dict) and result.get("ok") is False:
            return self._json(400, result, origin)
        # **result 在 "msg" 之后合并：桌面端的真实一句话（"已加入队列: xxx"）压过这里的兜底文案，
        # 分流三件套 bundle/dest_dir/routed 也在 result 里，靠这个合并透出去（老扩展没有这些键，
        # 合并结果与改动前逐字一致）
        return self._json(200, {"ok": True, "msg": "已开始下载", **result}, origin)


def start(version="", port=None):
    """启动桥服务（幂等，端口冲突/异常静默）。返回端口号或 0。"""
    global _server, _version
    try:
        import automation_events
        if automation_events.get_hub() is None:
            automation_events.configure(
                snapshot_provider=automation_state,
                tasks_provider=_event_tasks_snapshot,
                model_scan_provider=_event_model_scan,
                library_scan_provider=_event_library_scan,
                # 只用原始配置算 keys；SSE 快照仍走 automation_state 的脱敏值。
                config_provider=lambda: getattr(_api, "cfg", None) or {},
            )
        import webhook_dispatch
        webhook_dispatch.refresh_from_config(getattr(_api, "cfg", None) or {},
                                            automation_events.get_hub())
    except Exception:
        pass
    if _server is not None:
        return _server.server_port
    _version = version or _version
    port = int(port or DEFAULT_PORT)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    except OSError:
        return 0
    srv.daemon_threads = True
    _server = srv
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
    return srv.server_port


def stop():
    global _server
    try:
        import automation_events
        automation_events.shutdown()
    except Exception:
        pass
    try:
        import webhook_dispatch
        webhook_dispatch.shutdown(flush_s=1.0)
    except Exception:
        pass
    if _server is not None:
        try:
            _server.shutdown()
        except Exception:
            pass
        try:
            _server.server_close()
        except Exception:
            pass
        _server = None
    clear_bundles()   # 服务没了令牌就没意义（重启后 webui 会重新登记）；失败也不影响关服务
