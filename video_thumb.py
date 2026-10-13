# -*- coding: utf-8 -*-
"""视频首帧出盘：让 Windows 自己的解码器出像素，程序把 JPEG 写在视频旁边。

为什么必须由程序生成（而不是浏览器抓帧）
------------------------------------
桌面端默认从 `file://` 加载界面（main_web.py 用 `url=INDEX` 起窗口），Chromium 对「file:// 页面上的
file:// 视频」一律把 canvas 标脏：实测 `toDataURL()` 抛 `SecurityError: tainted canvas`。也就是说
"纯 stdlib 解不了 H.264，能拿出首帧像素的只有浏览器"这句话只在浏览器模式 + 同源 http 下发成立，
默认模式下 107 个视频帧的 `imageUrl` 全空，海报就只能是空白直到播起来。所以首帧必须在磁盘上生成。

为什么走外壳而不是 Media Foundation
------------------------------------
`SHCreateItemFromParsingName` + `IShellItemImageFactory::GetImage`（就是资源管理器出视频缩略图那条路）
实测可用，本机 6 个真实 mp4 全成功，含中文目录名与 66MB / 1568x2272 的原片：进程内首次 817ms
（外壳 + 解码器冷启动），之后单张 218~309ms，同一文件重复取 31~49ms；就地读 D 盘原片对源目录零副作用
（条目数 5→5、无 Thumbs.db、源 mtime 不变）。Media Foundation `IMFSourceReader::ReadSample` 因此没启用。
失败面也干净：非视频 / 截断 / 空文件 → `GetImage 0x8004b200`，本模块一律回 False 并留原因在 `last_error()`。

约定
----
- 只写调用方给的那一个 `out_jpg`（外加同目录同名的 `.tmp`），目录不存在不创建、不跟着链接写到别处；
- 写盘复用 `image_gallery.open_exclusive_tmp`（O(CREATE|EXCL)）与 `image_gallery.is_reparse_path`
  （`os.path.islink()` 对 Windows junction 恒为 False，必须看 FILE_ATTRIBUTE_REPARSE_POINT）；
- 非 Windows、DLL 未编译、pythonnet 缺失、字节解不出图 —— 全部安静返回 False，绝不抛异常，
  调用方据此回落到浏览器抓帧那条路。
"""
import io
import os
import sys
import threading
from pathlib import Path

import image_gallery

QUALITY = 82                # 与前端抓帧回传（save_model_video_poster）同一档，避免两种海报观感不一致
MIN_JPEG_BYTES = 256        # 小于这个数不可能是真首帧（外壳失败时给过 0 字节位图）
MAX_JPEG_BYTES = 4 * 1024 * 1024
MAX_EDGE = 4096             # 解出来的海报长边超过它一定是哪里错了，直接判失败
DEFAULT_MAX_SIZE = 640

_probe = {"done": False, "helper": None}
_lock = threading.RLock()
# 失败原因按线程存：补帧是后台线程在跑，前端诊断接口可能同时在另一个线程上问「为什么没出图」，
# 用全局字段会把两条链路的理由互相冲掉。
_tls = threading.local()


def _set_error(msg):
    _tls.msg = str(msg or "")[:240]


def last_error():
    """本线程最近一次失败的原因（只给人看，不参与判定；成功时为空串）。"""
    return getattr(_tls, "msg", "") or ""


def _dll_path():
    """原生 DLL 位置：冻结包内是 `native/CftChrome.dll`，源码运行是 `build/native/CftChrome.dll`。

    与 native_chrome.IntegratedChrome 同一套定位口径，两处不一致就会出现「窗口壳能用、首帧不能用」。"""
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    if getattr(sys, "frozen", False):
        return root / "native" / "CftChrome.dll"
    return root / "build" / "native" / "CftChrome.dll"


def _helper():
    """取 CftNative.VideoThumb 类型；拿不到就返回 None（只探一次，别每帧都付一次 AddReference）。"""
    if _probe["done"]:
        return _probe["helper"]
    with _lock:
        if _probe["done"]:
            return _probe["helper"]
        helper = None
        if sys.platform != "win32":
            _set_error("非 Windows，外壳出不了首帧，交给浏览器抓帧兜底")
        else:
            dll = _dll_path()
            if not os.path.isfile(str(dll)):
                _set_error("原生 DLL 未编译：%s（python scripts/build_native.py）" % dll)
            else:
                try:
                    import clr  # pythonnet，仅 Windows 需要，requirements 里已带
                    clr.AddReference(str(dll))
                    from CftNative import VideoThumb
                    helper = VideoThumb
                except Exception as e:  # DLL 与运行时不匹配、pythonnet 缺失等
                    _set_error("原生模块不可用：%s: %s" % (type(e).__name__, e))
        _probe["helper"] = helper
        _probe["done"] = True
    return helper


def available():
    """本机是否具备原生出首帧的能力（调用方据此决定要不要还去走浏览器抓帧）。"""
    return _helper() is not None


def _extract(video_path, max_size):
    """向外壳要首帧 JPEG 字节；失败返回 None。单测替掉的就是这一层。"""
    helper = _helper()
    if helper is None:
        return None
    try:
        raw = helper.FirstFrameJpeg(str(video_path), int(max_size), QUALITY)
        if raw is None:
            _set_error("外壳未出图：%s" % str(helper.LastError or "").strip())
            return None
        # pythonnet 的 System.Byte[] 不走 buffer 协议（实测直接 f.write() 抛
        # "not implemented for 'Byte'' iterator"），必须先 bytes() 过一遍。
        data = bytes(raw)
    except Exception as e:  # GDI+/外壳的任何 .NET 异常都在这层收口
        _set_error("原生调用异常：%s: %s" % (type(e).__name__, e))
        return None
    if not data:
        _set_error("外壳返回空字节")
        return None
    return data


def _jpeg_size(data):
    """纯 stdlib 从 JPEG 头里读尺寸（SOF0/1/2…）。Pillow 缺席时的兜底校验，也用来复核 Pillow 的读数。"""
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return (int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big"))
        if marker == 0xDD:                      # DNL 之后没有段长度
            i += 4
            continue
        seg = int.from_bytes(data[i + 2:i + 4], "big")
        if seg < 2:
            return None
        i += 2 + seg
    return None


def _decode(data):
    """字节必须真能解码成 JPEG 才算首帧，返回 (宽, 高)；不合格返回 None。"""
    if not isinstance(data, (bytes, bytearray)):
        _set_error("首帧数据类型不对")
        return None
    data = bytes(data)
    if len(data) < MIN_JPEG_BYTES:
        _set_error("首帧不足 %dB，判为无效" % MIN_JPEG_BYTES)
        return None
    if len(data) > MAX_JPEG_BYTES:
        _set_error("首帧超过 %dB" % MAX_JPEG_BYTES)
        return None
    if data[:2] != b"\xff\xd8" or data[2:3] != b"\xff":
        _set_error("首帧不是 JPEG")
        return None
    size = None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            if im.format != "JPEG":
                _set_error("首帧解成了 %s，不是 JPEG" % im.format)
                return None
            size = (im.width, im.height)
    except ImportError:
        size = _jpeg_size(data)                 # 没有 Pillow 就只认 JPEG 头，尺寸靠段解析
    except Exception as e:
        _set_error("首帧解码失败：%s: %s" % (type(e).__name__, e))
        return None
    if not size:
        _set_error("首帧解不出尺寸")
        return None
    w, h = size
    if w < 2 or h < 2 or max(w, h) > MAX_EDGE:
        _set_error("首帧尺寸异常 %dx%d" % (w, h))
        return None
    return (w, h)


def _write_atomic(data, out, tmp):
    """独占创建同目录 .tmp → 写完 → 原子替换。失败自己清掉自己建的临时文件，绝不动 out。"""
    fd = None
    ok = False
    try:
        try:
            fd = image_gallery.open_exclusive_tmp(tmp)
        except Exception as e:
            _set_error("临时文件独占创建失败：%s: %s" % (type(e).__name__, e))
            return False
        with os.fdopen(fd, "wb") as f:
            fd = None
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())            # 海报是「一次生成长期复用」的资产，掉电不该留半张
            except OSError:
                pass
        os.replace(tmp, out)
        ok = True
        return True
    except Exception as e:
        _set_error("写盘失败：%s: %s" % (type(e).__name__, e))
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if not ok:
            try:
                os.unlink(tmp)                  # 只有独占创建成功才走到这里，删的一定是自己建的那个
            except OSError:
                pass


def _guarded(video_path, out_jpg, max_size, overwrite):
    _set_error("")
    if sys.platform != "win32":
        _set_error("非 Windows，不生成首帧")
        return False
    cap = max(16, min(MAX_EDGE, int(max_size)))
    src = os.path.abspath(str(video_path or ""))
    if not os.path.isfile(src):
        _set_error("视频文件不存在：%s" % src)
        return False
    out = os.path.abspath(str(out_jpg or ""))
    out_dir = os.path.dirname(out)
    name = os.path.basename(out)
    if not name or ":" in name or name in (".", ".."):
        # 文件名里带冒号 = NTFS 备用数据流，写进去就是往别的文件里塞字节；本模块只认普通文件名。
        _set_error("首帧目标文件名无效：%s" % name)
        return False
    if not out_dir or not os.path.isdir(out_dir):
        _set_error("首帧目标目录不存在（本模块不创建目录）：%s" % out_dir)
        return False
    # 目录是指向别处的链接/junction → 写出去就跑到模型目录之外，必须拒（与 _fill_dir_writable 同口径）
    if image_gallery.is_reparse_path(out_dir) or os.path.islink(out_dir):
        _set_error("首帧目标目录是链接/junction，已拒绝")
        return False
    if image_gallery.is_reparse_path(out) or os.path.islink(out):
        _set_error("首帧目标本身是指向别处的链接，已拒绝")
        return False
    if os.path.isdir(out):
        _set_error("首帧目标是目录，已拒绝")
        return False
    if not overwrite:
        try:
            if os.path.isfile(out) and os.path.getsize(out) > 0:
                _set_error("本地已有首帧，未覆盖")
                return False
        except OSError:
            _set_error("无法确认已有首帧，未覆盖")
            return False
    data = _extract(src, cap)
    if data is None:
        if not last_error():
            _set_error("外壳未给出首帧字节")
        return False
    size = _decode(data)
    if size is None:
        return False
    if max(size) > cap:
        _set_error("首帧 %dx%d 超出 max_size=%d，判为无效" % (size[0], size[1], cap))
        return False
    tmp = out + ".tmp"                       # 同目录同名 + .tmp：由构造保证跑不出去
    if image_gallery.is_reparse_path(tmp) or os.path.islink(tmp):
        _set_error("残留的 .tmp 是链接，已拒绝")
        return False
    return _write_atomic(bytes(data), out, tmp)


def first_frame(video_path, out_jpg, max_size=DEFAULT_MAX_SIZE, overwrite=False):
    """把 `video_path` 的首帧写成 `out_jpg`（长边 ≤ max_size 的 JPEG）。

    成功返回 True；任何失败（非 Windows、DLL 没编译、外壳解不出、字节不是图、目标是指向别处的链接、
    磁盘异常）都返回 False 并只把原因留在 `last_error()`，绝不抛异常。
    写入范围只有一处：`os.path.dirname(out_jpg)` 里的 `out_jpg` 本身（外加同名的 `.tmp`）——目录不存在
    不创建、目标或目录是指向别处的链接就拒、文件名带 NTFS 备用数据流冒号就拒。
    `overwrite=False`（默认）时目标已存在且有内容也算 False——本地已有首帧不覆盖，与
    `save_model_video_poster` 同一条口径，因此对 107 帧盲调是安全的。"""
    try:
        return _guarded(video_path, out_jpg, max_size, overwrite)
    except Exception as e:  # 最后一道闸：连 abspath 抛 ValueError 这种都不许冒到调用方
        _set_error("未预期异常：%s: %s" % (type(e).__name__, e))
        return False
