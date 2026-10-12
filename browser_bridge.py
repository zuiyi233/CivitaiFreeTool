# -*- coding: utf-8 -*-
"""浏览器桥：本地 HTTP 服务（127.0.0.1），供 Chrome 扩展一键下载 + **浏览器模式的前端界面与 RPC**。

设计要点
- 仅绑定 127.0.0.1，外部网络不可达；
- CORS 白名单：只允许 chrome-extension://* 与本机页面（file://、null、127.0.0.1/localhost）；
- POST /api/download：校验 URL 后调用注册的下载 handler（webui.Api.dl_enqueue_url，
  解析并入队下载均为后台线程，handler 同步快速返回）；
- POST /api/rpc：浏览器模式下的通用方法调用（前端 shim 把 window.pywebview.api.xxx 映射到这里）；
- GET /、/app.js 等：浏览器模式下直接由本服务提供 web/ 目录里的界面文件；
- GET /api/heartbeat：页面心跳（用于「关页面即退出」）；
- GET /api/media：资源库媒体文件流式读取（支持 Range/206，浏览器模式视频播放唯一可行路径）；
- 端口冲突/启动失败静默（不影响主程序）。
"""
import base64
import json
import mimetypes
import os
import re
import threading
import time
import urllib.parse
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
# 资源库媒体流（GET /api/media）：可读根目录由 webui.Api 注入，桥层不 import webui
_media_roots_fn = None     # def media_roots() -> [绝对根目录]

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
    """注册下载入口：webui.Api 启动时把 dl_enqueue_url 注册进来"""
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


def port():
    """桥服务端口（未启动返回 0）"""
    return _server.server_port if _server is not None else 0


def touch():
    global _last_seen
    _last_seen = time.time()


def set_archive_handlers(config_fn=None, write_fn=None, done_fn=None, check_fn=None):
    """注册归档入口：webui.Api 启动时把 config/write/done/check 注册进来"""
    global _archive_config_fn, _archive_write_fn, _archive_done_fn, _archive_check_fn
    _archive_config_fn = config_fn
    _archive_write_fn = write_fn
    _archive_done_fn = done_fn
    _archive_check_fn = check_fn


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
        """POST /api/archive/file：body {rel_path, data_b64}，上限 128MB"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, _ARCHIVE_FILE_MAX)) if length else b""
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
            rel_path = data.get("rel_path") or ""
            data_b64 = data.get("data_b64") or ""
        except Exception:
            return self._json(400, {"ok": False, "msg": "请求格式错误"}, origin)
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
        try:
            saved = fn(rel_path, blob)
        except Exception as e:
            return self._json(400, {"ok": False, "msg": str(e)[:200]}, origin)
        if saved is False or saved is None:
            return self._json(400, {"ok": False, "msg": "写入失败"}, origin)
        return self._json(200, {"ok": True, "path": str(saved)}, origin)

    def _handle_archive_done(self, origin):
        """POST /api/archive/done：body {dir_name}，尽力而为语义"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 65536)) if length else b""
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
            dir_name = data.get("dir_name") or ""
        except Exception:
            return self._json(400, {"ok": False, "msg": "请求格式错误"}, origin)
        fn = _archive_done_fn
        if fn is None:
            return self._json(500, dict(_NOT_READY), origin)
        if not dir_name:
            return self._json(400, {"ok": False, "msg": "dir_name 不能为空"}, origin)
        try:
            fn(dir_name)
        except Exception as e:
            # 尽力而为：done 失败也返回 ok:true + msg
            return self._json(200, {"ok": True, "msg": "重扫触发失败: %s" % str(e)[:120]}, origin)
        return self._json(200, {"ok": True}, origin)

    def do_POST(self):
        origin = self._origin()
        if not self._check_origin(origin):
            return
        path = urlsplit(self.path).path
        touch()
        if path == "/api/rpc":
            return self._rpc(origin)
        if path == "/api/archive/file":
            return self._handle_archive_file(origin)
        if path == "/api/archive/done":
            return self._handle_archive_done(origin)
        if path != "/api/download":
            return self._json(404, {"ok": False, "msg": "Not Found"}, origin)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(length, 65536)) if length else b""
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
            url = data.get("url") or ""
            dest_subdir = data.get("dest_subdir") or ""
        except Exception:
            return self._json(400, {"ok": False, "msg": "请求格式错误"}, origin)
        url = (url or "").strip()
        if not url:
            return self._json(400, {"ok": False, "msg": "链接不能为空"}, origin)
        if not is_model_url(url):
            return self._json(400, {"ok": False, "msg": "仅支持 Civitai 模型页链接"}, origin)
        fn = _handler
        if fn is None:
            return self._json(500, {"ok": False, "msg": "下载服务未就绪"}, origin)
        try:
            result = fn(url, dest_subdir) or {}
        except Exception as e:
            return self._json(500, {"ok": False, "msg": "下载启动失败: %s" % str(e)[:120]}, origin)
        if isinstance(result, dict) and result.get("ok") is False:
            return self._json(400, result, origin)
        return self._json(200, {"ok": True, "msg": "已开始下载", **result}, origin)


def start(version="", port=None):
    """启动桥服务（幂等，端口冲突/异常静默）。返回端口号或 0。"""
    global _server, _version
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
