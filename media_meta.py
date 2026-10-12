"""资源库元数据提取：单个媒体文件 → 归一化 dict（纯函数，只读，绝不抛异常）。

三条读取路径都是流式的：ComfyUI 产出的 PNG 常带几 MB 的 workflow/prompt 块，生成的视频
可达几百 MB，任何 `handle.read()` 整文件读法都不适用于资源库扫描线程。解析逻辑一律复用
comfy_metadata / image_gallery 的既有实现，本模块只负责「载体读取 + 字段归一」。
"""
import json
import math
import os
import re
import struct
import zlib

from comfy_metadata import extract_prompts, extract_model_references, WEIGHT_SUFFIXES
from image_gallery import comfy_generation

# 契约 §7 规格数值：照抄执行，不得自行平衡。
MAX_CHUNK_BYTES = 8 * 1024 * 1024        # PNG 单个文本块上限
MAX_PNG_CHUNKS = 64                      # PNG 文本块数量上限
MAX_WINDOW_BYTES = 16 * 1024 * 1024      # mp4 整读上限，超出走首/尾两段窗口
MAX_SIDECAR_BYTES = 8 * 1024 * 1024      # sidecar JSON 上限
MAX_EXTRA_BYTES = 64 * 1024              # extra 摘要上限
MAX_BOXES = 20000                        # mp4 单窗口盒数量防失控
MAX_DEPTH = 8

IMAGE_EXTS = (".png", ".webp", ".jpg", ".jpeg", ".gif")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_TEXT_TYPES = ("tEXt", "zTXt", "iTXt")

RESULT_KEYS = ("has_meta", "source", "prompt", "negative", "note", "checkpoint", "loras",
               "models", "sampler", "scheduler", "seed", "steps", "cfg",
               "width", "height", "duration", "extra")

# mp4 的盒类型只有 4 字节，写不出 comment/prompt 全称：QuickTime/iTunes 用 ©cmt/©des，
# ffmpeg 的自由元数据键只保留前 4 个字符。故统一归一到四个逻辑标签名，再按
# ©cmt → comment → ©des → description 的取值顺序消费。
_MP4_CONTAINERS = (b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"ilst", b"meta")
_MP4_TEXT_ALIAS = {
    "\xa9cmt": "comment", "cmt": "comment", "comm": "comment", "comment": "comment",
    "\xa9des": "description", "desc": "description", "description": "description",
    "prom": "prompt", "prompt": "prompt",
    "wflo": "workflow", "work": "workflow", "workflow": "workflow",
}
_MP4_BINARY_TYPES = (10, 11, 12, 13, 14, 21, 22, 23)   # ilst data 盒里的封面图/缩略图类型
_MP4_PREFIX_JUNK = (b"\x00\x00\x00\x00", b"apple\x00", b"apple")
_PLIST_PAIR = re.compile(rb"<key>(.{0,64}?)</key>\s*<(?:string|data)>(.{0,4096}?)</(?:string|data)>", re.S)

_GEN_KEYS = ("prompt", "negativePrompt", "promptExtractionNote", "seed", "steps", "cfgScale",
             "sampler", "scheduler", "width", "height", "resources")

# 字段名回退（仅 media_meta 内的加法层）：现代工作流用自定义条件节点接到 BasicGuider 上，
# comfy_generation 的采样器链条走不到文本/底模，但值就放在标准 ComfyUI 字段名下。
# 只按字段名收集字符串，不执行、不猜测未知节点语义——与 comfy_metadata 的规则一致。
_FIELD_CHECKPOINT = ("ckpt_name", "unet_name", "diffusion_model")
_FIELD_PROMPT = ("prompt", "text", "caption", "positive_text")
# 纯文本容器节点的 text/prompt 是文档内容而不是生成输入，必须排除。
_FIELD_PROMPT_SKIP = ("note", "markdown", "text panel", "comfyui-textpanel",
                      "reroute", "anythingindex")
MAX_FALLBACK_PROMPT_CHARS = 4000


def _is_document_node(class_type):
    low = _text(class_type).lower()
    if any(pattern in low for pattern in _FIELD_PROMPT_SKIP):
        return True
    return low.startswith("primitive") or ("comfy" in low and "expression" in low)


def _field_fallback(api):
    """遍历 API 图 nodes[].inputs 的标准字段名 → (checkpoint, loras, prompt)。"""
    checkpoint = ""
    loras = []
    texts = []
    for node in api.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        if not checkpoint:
            for field in _FIELD_CHECKPOINT:
                value = inputs.get(field)
                if isinstance(value, str) and value.strip():
                    checkpoint = _base(value)      # 与 comfy_generation 同样取最后一段
                    break
        for field, value in inputs.items():
            # 权重后缀是硬门槛：lora_strength 之类数值字段自然被过滤掉。
            if not isinstance(value, str) or not value.strip() or \
                    not value.lower().endswith(WEIGHT_SUFFIXES):
                continue
            if field == "lora_name" or field.startswith("lora_") or field.endswith("_lora"):
                name = _base(value)
                if name and name not in loras:
                    loras.append(name)
        if _is_document_node(node.get("class_type")):
            continue
        for field in _FIELD_PROMPT:
            value = inputs.get(field)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                if text not in texts:
                    texts.append(text)
    return checkpoint, loras, " | ".join(texts)[:MAX_FALLBACK_PROMPT_CHARS]


def blank_result():
    """键恒定存在的空壳；缺值为 ''/[]/None。"""
    return {"has_meta": False, "source": "none", "prompt": "", "negative": "", "note": "",
            "checkpoint": "", "loras": [], "models": [], "sampler": "", "scheduler": "",
            "seed": None, "steps": None, "cfg": None, "width": None, "height": None,
            "duration": None, "extra": {}}


def _text(value):
    if value is None or isinstance(value, bool):
        return ""
    return value if isinstance(value, str) else str(value)


def _base(value):
    return _text(value).replace("\\", "/").split("/")[-1].strip()


def _as_int(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value == int(value) else None
    if isinstance(value, str):
        text = value.strip()
        return int(text) if text.lstrip("-").isdigit() else None
    return None


def _as_float(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        out = float(value)
        return out if math.isfinite(out) else None
    if isinstance(value, str):
        try:
            out = float(value.strip())
        except (TypeError, ValueError):
            return None
        return out if math.isfinite(out) else None
    return None


def _load_json(value):
    """NaN/Infinity 落成 None：否则 extra 用 allow_nan=False 序列化时会再炸。"""
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text[0] not in "{[":
        return None
    try:
        return json.loads(text, parse_constant=lambda _: None)
    except (ValueError, TypeError, RecursionError):
        return None


def is_api_graph(data):
    """契约 §2.2 的判定式，原样实现。"""
    return isinstance(data, dict) and any(isinstance(v, dict) and v.get("class_type") for v in data.values())


# --------------------------------------------------------------------------- PNG

def _inflate(payload):
    """zip bomb 也只解压到块上限，解压失败不算致命错误。"""
    try:
        decompressed = zlib.decompress(payload)
    except zlib.error:
        try:
            decompressed = zlib.decompressobj().decompress(payload, MAX_CHUNK_BYTES)
        except zlib.error:
            return None
    if not decompressed:
        return None
    try:
        return decompressed.decode("utf-8")
    except UnicodeDecodeError:
        return decompressed.decode("latin-1", "replace")


def _decode_text(data):
    """tEXt：latin-1 的 keyword、一个 0 字节、latin-1 文本。"""
    head, sep, rest = data.partition(b"\x00")
    if not head or not sep or not rest:
        return None, None
    return head.decode("latin-1", "replace"), rest.decode("latin-1", "replace")


def _decode_ztext(data):
    """zTXt：keyword\\0 method(1) zlib 流。"""
    head, sep, rest = data.partition(b"\x00")
    if not head or not sep or len(rest) < 2:
        return None, None
    return head.decode("latin-1", "replace"), _inflate(rest[1:])


def _decode_itext(data):
    """iTXt：keyword\\0 flags(1) method(1) lang\\0 translated keyword\\0 text（UTF-8）。"""
    head, sep, rest = data.partition(b"\x00")
    if not head or not sep or len(rest) < 2:
        return None, None
    flags, after = rest[0], rest[2:]
    _lang, sep2, tail = after.partition(b"\x00")
    if not sep2:
        return None, None
    _keyword, sep3, payload = tail.partition(b"\x00")
    if not sep3:
        return None, None
    return head.decode("latin-1", "replace"), (_inflate(payload) if flags == 1 else payload.decode("utf-8", "replace"))


def read_image_text_chunks(path, keys=("prompt", "workflow"), max_chunks=MAX_PNG_CHUNKS,
                           max_chunk_bytes=MAX_CHUNK_BYTES):
    """流式遍历 PNG 块取 tEXt/zTXt/iTXt；命中全部 keys 或遇 IEND 即停。返回 {key: str}。

    只按块头 read(8) + seek：单个文本块可达 8MB，整文件读会拖垮扫描线程。超长块跳过而不中断，
    因为 ComfyUI 把 workflow 写在 IDAT 之后，前面的块不合法不代表后面没有元数据。
    """
    found = {}
    wanted = set(k for k in (keys or ()) if k)
    try:
        with open(path, "rb") as f:
            if f.read(8) != PNG_SIGNATURE:
                return {}
            seen = 0
            while True:
                header = f.read(8)
                if len(header) < 8:
                    break
                length, raw_type = struct.unpack(">I4s", header)
                name = raw_type.decode("latin-1", "replace")
                if name in PNG_TEXT_TYPES:
                    if length > max_chunk_bytes:
                        f.seek(length + 4, os.SEEK_CUR)      # 超长块跳过，继续找下一个
                    else:
                        data = f.read(length)
                        f.seek(4, os.SEEK_CUR)
                        if len(data) < length:
                            break
                        seen += 1
                        keyword, value = _PNG_DECODERS[name](data)
                        if keyword and value is not None and keyword not in found:
                            found[keyword] = value
                        if wanted and wanted.issubset(found):
                            break
                        if seen >= max_chunks:
                            break
                elif name == "IEND":
                    break
                else:
                    f.seek(length + 4, os.SEEK_CUR)
    except Exception:
        return found
    return found


_PNG_DECODERS = {"tEXt": _decode_text, "zTXt": _decode_ztext, "iTXt": _decode_itext}


# ----------------------------------------------------------------------- WebP/JPEG

def _read_pil_info(path):
    """WebP/JPEG 回落 PIL：.info + EXIF UserComment（对齐 image_gallery 的取法）。"""
    from PIL import Image
    info, width, height = {}, None, None
    with Image.open(path) as im:
        info = dict(im.info)
        width, height = im.size
        try:
            comment = im.getexif().get(0x9286)
        except Exception:
            comment = None
    if comment is not None:
        info["UserComment"] = comment
    return info, width, height


_EXIF_PREFIXES = ("ASCII\x00\x00\x00", "UNICODE\x00\x00\x00", "UNICODE\x00", "JIS\x00\x00\x00\x00\x00",
                  "BIG5\x00\x00\x00\x00", "None\x00\x00\x00\x00\x00")


def _comment_text(value):
    """EXIF UserComment 带字符集标记前缀，要去掉才是正文（契约 §2.3）。"""
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else _text(value)
    for prefix in _EXIF_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):]
    if text.startswith("ENCODING="):
        return text.partition("\x00")[2]
    return text


# --------------------------------------------------------------------------- mp4

def _u32(buf, off):
    return struct.unpack(">I", buf[off:off + 4])[0] if off + 4 <= len(buf) else 0


def _u64(buf, off):
    return struct.unpack(">Q", buf[off:off + 8])[0] if off + 8 <= len(buf) else 0


def _logical_key(ctype):
    try:
        name = ctype.decode("latin-1", "replace").strip().strip("\x00").lower()
    except (TypeError, ValueError):
        return None
    return _MP4_TEXT_ALIAS.get(name)


def _strip_junk_prefix(data):
    changed = True
    while data and changed:
        changed = False
        for junk in _MP4_PREFIX_JUNK:
            if data.startswith(junk):
                data = data[len(junk):]
                changed = True
    return data


def _box_text(data):
    """ilst 里的文本藏在 data 子盒中，头部 16 字节 = size(4)+'data'(4)+version/flags(4)+locale(4)。"""
    if len(data) >= 16 and data[4:8] == b"data":
        if (_u32(data, 8) >> 24) in _MP4_BINARY_TYPES:
            return None
        data = data[16:]
    else:
        data = _strip_junk_prefix(data)
    data = data.strip(b"\x00")
    if not data or b"\x00" in data:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if text.strip() else None


def _plist_pairs(data):
    """有些写法把键名放进 udta/META 的 plist：只取短字符串值。"""
    out = []
    for key, value in _PLIST_PAIR.findall(data):
        logical = _MP4_TEXT_ALIAS.get(key.decode("latin-1", "replace").strip().lower())
        if not logical:
            continue
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if text.strip():
            out.append((logical, text))
    return out


def _mvhd_duration(data):
    """version 0 的 timescale/duration 在 12/16；version 1 的时间字段改 64 位，整体后移。"""
    if len(data) < 20:
        return None
    if data[0] == 1:
        if len(data) < 32:
            return None
        timescale, duration = _u32(data, 20), _u64(data, 24)
    else:
        timescale, duration = _u32(data, 12), _u32(data, 16)
    if timescale <= 0 or duration <= 0:
        return None
    return duration / float(timescale)


def _tkhd_size(data):
    """tkhd 宽高是 16.16 定点；version 1 的时间字段 64 位，偏移整体后移 16 字节。"""
    if data[:1] == b"\x01":
        if len(data) < 96:
            return None
        width, height = _u32(data, 88), _u32(data, 92)
    else:
        if len(data) < 84:
            return None
        width, height = _u32(data, 76), _u32(data, 80)
    width, height = width >> 16, height >> 16
    if width <= 0 or height <= 0:
        return None
    return int(width), int(height)


def _handle_box(ctype, body, acc, in_trak, depth):
    bucket = "container" if not in_trak else "track"
    logical = _logical_key(ctype)
    if logical:
        text = _box_text(body)
        if text:
            acc[bucket].append((logical, text))
        return
    if ctype in _MP4_CONTAINERS:
        if b"<key>" in body and ctype not in (b"udta", b"ilst", b"trak", b"mdia", b"minf", b"stbl"):
            for pair in _plist_pairs(body):
                acc[bucket].append(pair)
            return
        _walk_boxes(body, 4 if ctype == b"meta" else 0, len(body), acc,
                    in_trak or ctype == b"trak", depth + 1)
        return
    stripped = body.lstrip(b"\x00")[:5]
    if stripped == b"<?xml" or b"<key>" in body:
        for pair in _plist_pairs(body):
            acc[bucket].append(pair)
        return
    if ctype == b"mvhd" and acc["duration"] is None:
        acc["duration"] = _mvhd_duration(body)
    elif ctype == b"tkhd" and acc["width"] is None:
        size = _tkhd_size(body)
        if size:
            acc["width"], acc["height"] = size


def _walk_boxes(buf, pos, end, acc, in_trak, depth):
    if depth > MAX_DEPTH:
        return
    while pos + 8 <= end and acc["boxes"] < MAX_BOXES:
        size = _u32(buf, pos)
        ctype = buf[pos + 4:pos + 8]
        header = 8
        if size == 1:
            size = _u64(buf, pos + 8)
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            return          # 声明尺寸越界：窗口被截断或字节本是垃圾，本窗口到此为止
        acc["boxes"] += 1
        _handle_box(ctype, buf[pos + header:pos + size], acc, in_trak, depth)
        pos += size


def _moov_spans(buf, tail_window):
    spans = []
    if not tail_window:
        pos = 0
        while pos + 8 <= len(buf):
            size = _u32(buf, pos)
            header = 8
            if size == 1:
                size = _u64(buf, pos + 8)
                header = 16
            elif size == 0:
                size = len(buf) - pos
            if size < header:
                break
            if buf[pos + 4:pos + 8] == b"moov" and pos + size <= len(buf):
                spans.append((pos + header, pos + size))
            pos += size
        return spans
    # moov 在尾窗口里时，它的顶层盒起点在窗口之外，只能用 'moov' 四字反推盒头；mdat 内部
    # 完全可能出现同样的字节，所以要校验声明尺寸与首个子盒类型。
    start = 0
    while True:
        idx = buf.find(b"moov", start)
        if idx < 0:
            break
        start = idx + 4
        if idx < 4:
            continue
        begin = idx - 4
        size = _u32(buf, begin)
        if size < 16 or begin + size > len(buf):
            continue
        if buf[idx + 8:idx + 12] not in (b"mvhd", b"trak", b"udta", b"meta", b"Meta"):
            continue
        spans.append((idx + 4, begin + size))
    return spans


def read_mp4_meta(path, max_window_bytes=MAX_WINDOW_BYTES, info=None):
    """返回 {"comment":str|None,"description":str|None,"prompt":str|None,"workflow":str|None,
             "duration":float|None,"width":int|None,"height":int|None}。

    info 是可选的诊断字典（调用方传空 dict 即可），用来记录 moov 落在哪个窗口，
    不参与契约返回值形状。
    """
    out = {"comment": None, "description": None, "prompt": None, "workflow": None,
           "duration": None, "width": None, "height": None}
    if info is None:
        info = {}
    info.setdefault("moov_found", False)
    info.setdefault("window", "whole")
    info.setdefault("tags", [])
    size = os.path.getsize(path)
    if size <= max_window_bytes:
        with open(path, "rb") as f:
            windows = [(f.read(), False)]
    else:
        # 为什么必须读尾部：ffmpeg 默认把 moov 写在 mdat 之后，ComfyUI 的 SaveVideo 与
        # VHS_VideoCombine 都没开 faststart —— 只扫文件头 16MB 永远取不到元数据。
        with open(path, "rb") as f:
            f.seek(0)
            head = f.read(max_window_bytes)
            f.seek(size - max_window_bytes)
            tail = f.read(max_window_bytes)
        windows = [(head, False), (tail, True)]
        info["window"] = "head+tail"
    acc = {"container": [], "track": [], "duration": None, "width": None, "height": None, "boxes": 0}
    for buf, is_tail in windows:
        for start, stop in _moov_spans(buf, is_tail):
            info["moov_found"] = True
            info.setdefault("moov_at", "tail" if is_tail else "head")
            _walk_boxes(buf, start, stop, acc, False, 1)
    tags = {}
    for group in (acc["container"], acc["track"]):   # 容器 udta 优先，再逐 trak
        for key, text in group:
            tags.setdefault(key, text)
    for key in ("comment", "description", "prompt", "workflow"):
        if tags.get(key):
            out[key] = tags[key]
    out["duration"] = acc["duration"]
    out["width"] = acc["width"]
    out["height"] = acc["height"]
    info["boxes"] = acc["boxes"]
    info["tags"] = sorted(k for k, v in tags.items() if v)
    return out


# --------------------------------------------------------------------- 归一化拼装

def _classify(value, origin, sink, notes, depth=0):
    """把 prompt/workflow/comment/description 的各种载体归到 api / ui / 纯文本三个桶。"""
    if value is None or depth > 3 or isinstance(value, (int, float, bool)):
        return
    if isinstance(value, str):
        parsed = _load_json(value)
        if parsed is None:
            text = value.strip()
            if not text:
                return
            if origin == "prompt":
                sink["text"].append(text)
            elif origin in ("comment", "description"):
                notes.append("视频的 %s 标签不是 JSON，未当作提示词。" % origin)
            else:
                notes.append("workflow 不是 JSON，已忽略。")
            return
        _classify(parsed, origin, sink, notes, depth + 1)
        return
    if isinstance(value, list):
        notes.append("元数据不是对象（不是 ComfyUI 图），已忽略。")
        return
    if not isinstance(value, dict):
        return
    if is_api_graph(value):
        if sink["api"] is None:
            sink["api"] = value
        return
    if isinstance(value.get("nodes"), list):
        if sink["ui"] is None:
            sink["ui"] = value
        return
    for key in ("prompt", "workflow", "comment", "description"):
        if key in value:
            _classify(value[key], key, sink, notes, depth + 1)


def _apply_graph(out, api, ui, notes, extra):
    """API 图权威；只有 UI 图时才走 workflow 回落。"""
    if api is not None:
        gen = comfy_generation(api) or {}
        refs = extract_model_references(graph=api) or {}
        extra["graph"] = "api"
    else:
        gen = extract_prompts(workflow=ui) or {}
        refs = extract_model_references(workflow=ui) or {}
        extra["graph"] = "ui"
    extra["graph_nodes"] = len(api if api is not None else (ui or {}))
    if api is not None and isinstance(ui, dict):
        extra["ui_nodes"] = len(ui.get("nodes") or [])
    if not out["prompt"]:
        out["prompt"] = _text(gen.get("prompt"))
    if not out["negative"]:
        out["negative"] = _text(gen.get("negativePrompt"))
    note = _text(gen.get("promptExtractionNote"))
    if note:
        out["note"] = note
    for field, value in (("seed", _as_int(gen.get("seed", gen.get("noise_seed")))),
                         ("steps", _as_int(gen.get("steps"))),
                         ("cfg", _as_float(gen.get("cfgScale", gen.get("cfg")))),
                         ("width", _as_int(gen.get("width"))),
                         ("height", _as_int(gen.get("height")))):
        if value is not None:
            out[field] = value
    for field, key in (("sampler", "sampler"), ("scheduler", "scheduler")):
        value = _text(gen.get(key, gen.get("sampler_name" if key == "sampler" else key)))
        if value and not out[field]:
            out[field] = value
    resources = gen.get("resources")
    if isinstance(resources, list):
        for item in resources:
            if not isinstance(item, dict):
                continue
            name = _base(item.get("name"))
            kind = _text(item.get("type")).upper()
            if not name:
                continue
            if kind in ("CHECKPOINT", "DIFFUSION_MODEL"):
                if not out["checkpoint"]:
                    out["checkpoint"] = name
            elif kind == "LORA" and name not in out["loras"]:
                out["loras"].append(name)
    models = refs.get("models") if isinstance(refs, dict) else None
    if isinstance(models, list):
        seen = []
        for name in models:
            if isinstance(name, str) and name and name not in seen:
                seen.append(name)
        out["models"] = seen
    disabled = refs.get("disabled_models") if isinstance(refs, dict) else None
    if isinstance(disabled, list) and disabled:
        extra["disabled_models"] = [d for d in disabled if isinstance(d, str)][:200]
    leftover = {k: v for k, v in gen.items() if k not in _GEN_KEYS}
    if leftover:
        extra["generation"] = leftover
    if api is not None:
        # 链条结果更精确（只报告真正接进被采样模型的权重），有则原样保留、不合并字段名结果。
        fb_ckpt, fb_loras, fb_prompt = _field_fallback(api)
        if out["checkpoint"] or out["loras"]:
            extra["classification_source"] = "chain"
        elif fb_ckpt or fb_loras:
            out["checkpoint"] = fb_ckpt
            out["loras"] = fb_loras
            extra["classification_source"] = "field_name"
        if out["prompt"]:
            extra["prompt_source"] = "chain"
        elif fb_prompt:
            out["prompt"] = fb_prompt
            extra["prompt_source"] = "name_fallback"
            notes.append("提示词按字段名（prompt/text 等）回退还原：工作流使用自定义条件节点，"
                         "无法区分正负向分组，多条文本以 | 连接，可能包含非生成内容。")
    if not out["checkpoint"] and out["models"]:
        # 纯 UI 图里没有 CHECKPOINT 语义，不靠文件名猜底模。
        notes.append("仅有 UI 工作流，底模/LoRA 未记录（不猜测）。")


def _absorb(out, payloads, source, notes, extra):
    """True = 拿到了 prompt/workflow（has_meta 与 source 随之落定）。"""
    sink = {"api": None, "ui": None, "text": []}
    for key in ("comment", "description", "prompt", "workflow"):   # ©cmt→comment→©des→description 同序
        raw = payloads.get(key)
        if raw is not None:
            _classify(raw, key, sink, notes)
    if sink["api"] is not None or sink["ui"] is not None:
        _apply_graph(out, sink["api"], sink["ui"], notes, extra)
        out["has_meta"] = True
        out["source"] = source or "png_text"
        return True
    if sink["text"]:
        out["prompt"] = "\n".join(sink["text"])
        out["has_meta"] = True
        out["source"] = source or "png_text"
        notes.append("元数据不是 ComfyUI 图，按纯文本记录。")
        return True
    return False


def sidecar_paths(path):
    """ComfyUI 会把 out.png 与 out.mp4 的元数据写进同一个 out.json，所以 sidecar 以媒体文件名为准。"""
    base = os.path.splitext(path)[0]
    return [base + ".json", base + ".workflow.json"]


def _read_sidecar(out, target, notes, extra):
    for candidate in sidecar_paths(target):
        try:
            if os.path.abspath(candidate) == target or not os.path.isfile(candidate):
                continue
            size = os.path.getsize(candidate)
        except OSError:
            continue
        if size > MAX_SIDECAR_BYTES:
            notes.append("sidecar 超过 8MB，已跳过。")
            continue
        try:
            with open(candidate, "r", encoding="utf-8", errors="replace") as f:
                data = json.loads(f.read(), parse_constant=lambda _: None)
        except Exception:
            continue
        if not isinstance(data, dict):
            notes.append("sidecar 不是 JSON 对象，已跳过。")
            continue
        if is_api_graph(data):
            payloads = {"prompt": data}
        elif isinstance(data.get("nodes"), list):
            payloads = {"workflow": data}       # .workflow.json 的顶层就是 UI 图
        else:
            payloads = {k: data[k] for k in ("prompt", "workflow", "comment", "description") if k in data}
        if not payloads:
            continue
        extra["sidecar"] = _base(candidate)
        if _absorb(out, payloads, "sidecar", notes, extra):
            return True
    return False


def _cap_extra(extra, limit=None):
    """extra 只允许 64KB：先裁长字符串/长列表，仍超限就退化成类型摘要并置 truncated。"""
    if limit is None:
        limit = MAX_EXTRA_BYTES
    def shrink(value, depth=0):
        if isinstance(value, str):
            return value if len(value) <= 512 else value[:512] + "…"
        if isinstance(value, bytes):
            return "bytes:%d" % len(value)
        if depth >= 4:
            return None
        if isinstance(value, dict):
            return {str(k): shrink(v, depth + 1) for k, v in list(value.items())[:64]}
        if isinstance(value, (list, tuple)):
            return [shrink(v, depth + 1) for v in list(value)[:64]]
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        if isinstance(value, (int, bool)) or value is None:
            return value
        return str(value)[:512]

    trimmed = shrink(extra if isinstance(extra, dict) else {"raw": extra})
    try:
        if len(json.dumps(trimmed, ensure_ascii=False, allow_nan=False).encode("utf-8")) <= limit:
            return trimmed
    except (TypeError, ValueError, RecursionError):
        pass
    summary = {}
    for key, value in trimmed.items():
        if isinstance(value, (str, list, dict)):
            summary[key] = "%s:%d" % (type(value).__name__, len(value))
        else:
            summary[key] = value
    summary["truncated"] = True
    try:
        if len(json.dumps(summary, ensure_ascii=False, allow_nan=False).encode("utf-8")) > limit:
            summary = {"truncated": True, "keys": sorted(summary)[:64]}
    except (TypeError, ValueError):
        summary = {"truncated": True}
    return summary


def _norm_ext(ext, path):
    text = _text(ext or os.path.splitext(path)[1]).strip().lower()
    if not text:
        return ""
    return text if text.startswith(".") else "." + text


def extract(path, ext=None, max_chunk_bytes=MAX_CHUNK_BYTES, max_window_bytes=MAX_WINDOW_BYTES):
    """返回归一化 dict；任何异常一律吞掉并在 note 里说明，绝不抛给扫描线程。"""
    out = blank_result()
    notes = []
    extra = {}
    try:
        _fill(out, path, ext, max_chunk_bytes, max_window_bytes, notes, extra)
    except Exception as exc:
        notes.append("元数据解析失败：%s" % type(exc).__name__)
    merged = [n for n in (_text(out["note"]),) + tuple(notes) if n]
    out["note"] = "\n".join(merged)
    out["extra"] = _cap_extra(extra)
    for key, default in blank_result().items():
        if key not in out or out[key] is None and default is not None:
            out[key] = default
    return out


def _fill(out, path, ext, max_chunk_bytes, max_window_bytes, notes, extra):
    target = os.path.abspath(_text(path))
    ext = _norm_ext(ext, target)
    extra["ext"] = ext
    if not os.path.isfile(target):
        notes.append("文件不存在或不可读。")
        return

    payloads = {}
    source = ""
    if ext == ".png":
        chunks = read_image_text_chunks(target, keys=("prompt", "workflow"),
                                        max_chunks=MAX_PNG_CHUNKS, max_chunk_bytes=max_chunk_bytes)
        extra["chunk_keys"] = sorted(chunks)
        if chunks:
            payloads = {k: v for k, v in (("prompt", chunks.get("prompt")),
                                          ("workflow", chunks.get("workflow"))) if v}
            source = "png_text"
    elif ext in (".webp", ".jpg", ".jpeg"):
        try:
            info, width, height = _read_pil_info(target)
        except Exception as exc:
            info, width, height = {}, None, None
            notes.append("图片头读取失败：%s" % type(exc).__name__)
        extra["chunk_keys"] = sorted(k for k in info if isinstance(k, str))
        comment = _comment_text(info.get("UserComment")) or _comment_text(info.get("comment"))
        payloads = {k: v for k, v in (("prompt", info.get("prompt")), ("workflow", info.get("workflow")),
                                      ("comment", comment)) if v}
        if payloads:
            # webp/jpeg 走同一 PIL/EXIF 通路，但来源标签分开（详情面板要显示"来自哪一类容器"）
            source = "jpeg_exif" if ext in (".jpg", ".jpeg") else "webp_exif"
        out["width"] = _as_int(width)
        out["height"] = _as_int(height)
    elif ext == ".gif":
        extra["gif"] = "v1 不解析元数据"
    elif ext in (".mp4", ".mov"):
        mp4_info = {}
        meta = read_mp4_meta(target, max_window_bytes, mp4_info)
        extra["mp4"] = mp4_info
        payloads = {k: v for k, v in meta.items()
                    if k in ("comment", "description", "prompt", "workflow") and v}
        if payloads:
            source = "mp4_udta"
        if not mp4_info.get("moov_found"):
            notes.append("未找到 moov（可能超出首尾各 16MB 窗口）。")
        # 时长/宽高属于容器，即使没有元数据也要回填。
        if out["width"] is None:
            out["width"] = meta["width"]
        if out["height"] is None:
            out["height"] = meta["height"]
        if meta.get("duration") is not None:
            out["duration"] = meta["duration"]
    elif ext in (".webm", ".mkv"):
        extra["ebml"] = "v1 不解析 EBML"

    found = _absorb(out, payloads, source, notes, extra) if payloads else False
    if not found:
        _read_sidecar(out, target, notes, extra)
    if ext in IMAGE_EXTS and (out["width"] is None or out["height"] is None):
        try:
            header = _read_image_dimensions(target)
        except Exception:
            header = (None, None)
        out["width"] = out["width"] if out["width"] is not None else _as_int(header[0])
        out["height"] = out["height"] if out["height"] is not None else _as_int(header[1])


def _read_image_dimensions(path):
    from PIL import Image
    with Image.open(path) as im:
        return im.size
