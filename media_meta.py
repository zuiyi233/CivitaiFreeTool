"""资源库元数据提取：单个媒体文件 → 归一化 dict（纯函数，只读，绝不抛异常）。

各条读取路径都是流式的：ComfyUI 产出的 PNG 常带几 MB 的 workflow/prompt 块，生成的视频
可达几百 MB，任何 `handle.read()` 整文件读法都不适用于资源库扫描线程。解析逻辑一律复用
comfy_metadata / image_gallery 的既有实现，本模块只负责「载体读取 + 字段归一」。
"""
import io
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
# stts 的 entry 数来自文件声明：几百 MB 的视频也只该有几十条，异常声明必须封顶，
# 否则一个伪造的 4 字节计数会让解析循环把扫描线程吃掉。
MAX_STTS_ENTRIES = 100000
# EBML 的封顶思路与 MAX_STTS_ENTRIES 一样：元素数与「真正读进内存的字节数」都要有上限。
# Cluster 按声明长度 seek 跳过（不读），所以扫描几百 MB 的视频也只消耗几万次 seek。
MAX_EBML_ELEMENTS = 20000
MAX_EBML_READ_BYTES = 8 * 1024 * 1024
MAX_EBML_LEAF_BYTES = 4 * 1024 * 1024    # 单个叶子元素体上限：更大的（附件/字幕）跳过不读

# 提取语义版本：这层一变，老行就必须重解析才能拿到新字段（media_library 据此决定要不要
# 把 meta_mtime 清零重扫）。v2 = §15.1 的参数按字段名回收 + stts 帧率。
# v3 = EBML（webm/mkv）：Info 时长 + 视频轨 DefaultDuration 帧率与宽高 + Tags 里的 comment JSON。
EXTRACT_VERSION = 3

IMAGE_EXTS = (".png", ".webp", ".jpg", ".jpeg", ".gif")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_TEXT_TYPES = ("tEXt", "zTXt", "iTXt")

RESULT_KEYS = ("has_meta", "source", "prompt", "negative", "note", "checkpoint", "loras",
               "models", "sampler", "scheduler", "seed", "steps", "cfg",
               "width", "height", "duration", "fps", "extra")

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

# 参数回收（契约 §15.1）：候选字段名 + 值域照抄 §15.1.1。链式已给出值的字段一律不动，
# 这一层只在字段为空时填；同名字段有两个以上不同有效值时不选边（§15.1.3）。
PARAM_FIELDS = ("seed", "steps", "cfg", "sampler", "scheduler")
_PARAM_SPEC = (
    ("seed", ("seed", "noise_seed", "rand_seed", "random_seed"), "int", 0, 2 ** 64 - 1),
    ("steps", ("steps", "num_steps", "sampling_steps", "step_count"), "int", 1, 10000),
    ("cfg", ("cfg", "cfg_scale", "guidance_scale", "true_cfg_scale"), "float", 0.0, 100.0),
    ("sampler", ("sampler", "sampler_name", "sample_type", "solver"), "sampler", None, None),
    ("scheduler", ("scheduler", "noise_schedule", "schedule"), "scheduler", None, None),
)
_PARAM_BY_NAME = {candidate: (field, kind, lo, hi)
                  for field, candidates, kind, lo, hi in _PARAM_SPEC for candidate in candidates}

# 表外值一律不采用（§15.1.2）：真实库实测出现值 ∪ ComfyUI 官方枚举。
# 实测（2792 条在场、1676 条有空参数）：sampler 只有 euler(790) res_multistep(364) er_sde(180)
# euler_ancestral(138)，scheduler 只有 simple(1172) beta57(152) beta(65) sgm_uniform(14)，
# 全部落在下面的表里；官方枚举取自安装的 ComfyUI：comfy/samplers.py 的
# SAMPLER_NAMES = KSAMPLER_NAMES + ["ddim","uni_pc","uni_pc_bh2"] 与 SCHEDULER_NAMES =
# list(SCHEDULER_HANDLERS)。beta57 是已安装的 comfyui-minimax-h3-audio-T8 注册的调度器
# （h3_t8/sampling.py: BETA57_SCHEDULER_NAME = "beta57"，语义是 beta + alpha0.5/beta0.7）。
KNOWN_SAMPLERS = frozenset((
    "euler", "euler_cfg_pp", "euler_ancestral", "euler_ancestral_cfg_pp", "heun", "heunpp2",
    "exp_heun_2_x0", "exp_heun_2_x0_sde", "dpm_2", "dpm_2_ancestral", "lms", "dpm_fast",
    "dpm_adaptive", "dpmpp_2s_ancestral", "dpmpp_2s_ancestral_cfg_pp", "dpmpp_sde",
    "dpmpp_sde_gpu", "dpmpp_2m", "dpmpp_2m_cfg_pp", "dpmpp_2m_sde", "dpmpp_2m_sde_gpu",
    "dpmpp_2m_sde_heun", "dpmpp_2m_sde_heun_gpu", "dpmpp_3m_sde", "dpmpp_3m_sde_gpu", "ddpm",
    "lcm", "ipndm", "ipndm_v", "deis", "cfgpp_ud10_ab", "res_multistep", "res_multistep_cfg_pp",
    "res_multistep_ancestral", "res_multistep_ancestral_cfg_pp", "gradient_estimation",
    "gradient_estimation_cfg_pp", "er_sde", "seeds_2", "seeds_3", "sa_solver", "sa_solver_pece",
    "ddim", "uni_pc", "uni_pc_bh2",
))
KNOWN_SCHEDULERS = frozenset((
    "simple", "sgm_uniform", "karras", "exponential", "ddim_uniform", "beta", "normal",
    "linear_quadratic", "kl_optimal", "beta57",
))


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


def _param_value(kind, value, lo, hi):
    """字段名候选值 → 归一化值；不落在值域（含表外采样器/调度器名）就返回 None。"""
    if value is None or isinstance(value, bool) or isinstance(value, (list, dict)):
        return None
    if kind == "int":
        number = _as_int(value)
        return number if number is not None and lo <= number <= hi else None
    if kind == "float":
        number = _as_float(value)
        return number if number is not None and lo <= number <= hi else None
    text = _text(value).strip()
    table = KNOWN_SAMPLERS if kind == "sampler" else KNOWN_SCHEDULERS
    return text if text and text.lower() in table else None


def _params_by_field(api):
    """遍历 API 图 nodes[].inputs 的参数字段名 → (唯一值, 命中字段名, 多值冲突数)。"""
    found = {field: [] for field in PARAM_FIELDS}     # 每字段：[(比较键, 原值)]
    names = {field: [] for field in PARAM_FIELDS}
    for node in api.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for raw_name, raw_value in inputs.items():
            spec = _PARAM_BY_NAME.get(_text(raw_name).strip().lower())
            if spec is None:
                continue
            field, kind, lo, hi = spec
            value = _param_value(kind, raw_value, lo, hi)
            if value is None:
                continue
            compare = value.lower() if isinstance(value, str) else value
            if any(key == compare for key, _old in found[field]):
                continue
            found[field].append((compare, value))
            name = _text(raw_name).strip().lower()
            if name not in names[field]:
                names[field].append(name)
    values = {}
    conflicts = {}
    for field, pairs in found.items():
        if len(pairs) == 1:
            values[field] = pairs[0][1]
        elif len(pairs) > 1:
            conflicts[field] = len(pairs)     # §15.1.3：多个不同值时不选边
    return values, names, conflicts


def _apply_param_fallback(out, api, notes, extra):
    """链式优先：只填仍为空的参数位，冲突如实留空并写 note（§15.1.3/§15.1.4）。"""
    values, names, conflicts = _params_by_field(api)
    filled = []
    chain_had = False
    for field in PARAM_FIELDS:
        if out[field] not in (None, ""):
            chain_had = True
            continue
        if field in values:
            out[field] = values[field]
            filled.append(field)
        elif field in conflicts:
            notes.append("工作流里有 %d 个不同的 %s，未按字段名猜测。" % (conflicts[field], field))
    if filled or chain_had:
        extra["params_source"] = "field_name" if filled else "chain"
    if filled:
        hit = []
        for field in filled:
            hit.extend(names[field])
        extra["params_fields"] = sorted(set(hit))


def blank_result():
    """键恒定存在的空壳；缺值为 ''/[]/None。"""
    return {"has_meta": False, "source": "none", "prompt": "", "negative": "", "note": "",
            "checkpoint": "", "loras": [], "models": [], "sampler": "", "scheduler": "",
            "seed": None, "steps": None, "cfg": None, "width": None, "height": None,
            "duration": None, "fps": None, "extra": {}}


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


def _mdhd_timescale(data):
    """mdhd 的时间基：v0 在 creation(4)+modification(4) 之后，v1 两个时间字段改 64 位。"""
    if not data:
        return None
    if data[0] == 1:
        timescale = _u32(data, 20) if len(data) >= 24 else 0
    else:
        timescale = _u32(data, 12) if len(data) >= 16 else 0
    return timescale if timescale > 0 else None


def _stts_totals(data):
    """stts：ver/flags(4) + entry_count(4) + (sample_count(4) sample_delta(4))*。

    返回 (采样数合计, tick 合计)。tick 合计按 count*delta 累加 —— 一条 entry 描述的是
    count 个间隔为 delta 的样本，只加 delta 会把多帧合成一帧，帧率就假高。
    """
    if len(data) < 8:
        return None
    entries = _u32(data, 4)
    if entries <= 0 or entries > MAX_STTS_ENTRIES:
        return None
    samples = ticks = 0
    pos = 8
    for _ in range(entries):
        if pos + 8 > len(data):
            break
        samples += _u32(data, pos)
        ticks += _u32(data, pos) * _u32(data, pos + 4)
        pos += 8
    if samples <= 0 or ticks <= 0:
        return None
    return samples, ticks


def _track_fps(track):
    """fps = 采样数合计 / (delta 合计 / timescale)，保留 3 位；拿不到就 None（不默认 30）。"""
    if not track or not track.get("video"):
        return None
    timescale = track.get("timescale")
    totals = track.get("stts")
    if not timescale or not totals:
        return None
    samples, ticks = totals
    fps = samples * float(timescale) / float(ticks)
    if not math.isfinite(fps) or fps <= 0:
        return None
    return round(fps, 3)


def _read_trak(body, acc, depth):
    """逐 trak 建临时上下文：视频轨判定与 stts/mdhd 必须同轨才配对。"""
    track = {"video": False, "timescale": None, "stts": None}
    _walk_boxes(body, 0, len(body), acc, True, depth + 1, track)
    fps = _track_fps(track)
    if fps is not None and acc["fps"] is None:
        acc["fps"] = fps


def _handle_box(ctype, body, acc, in_trak, depth, track=None):
    bucket = "container" if not in_trak else "track"
    logical = _logical_key(ctype)
    if logical:
        text = _box_text(body)
        if text:
            acc[bucket].append((logical, text))
        return
    if ctype == b"trak":
        _read_trak(body, acc, depth)
        return
    if ctype in _MP4_CONTAINERS:
        if b"<key>" in body and ctype not in (b"udta", b"ilst", b"trak", b"mdia", b"minf", b"stbl"):
            for pair in _plist_pairs(body):
                acc[bucket].append(pair)
            return
        _walk_boxes(body, 4 if ctype == b"meta" else 0, len(body), acc, in_trak, depth + 1, track)
        return
    stripped = body.lstrip(b"\x00")[:5]
    if stripped == b"<?xml" or b"<key>" in body:
        for pair in _plist_pairs(body):
            acc[bucket].append(pair)
        return
    if ctype == b"mvhd" and acc["duration"] is None:
        acc["duration"] = _mvhd_duration(body)
    elif ctype == b"tkhd":
        size = _tkhd_size(body)
        if size:
            if track is not None:
                track["video"] = True          # 有画面尺寸就是视频轨，不依赖 hdlr 写法
            if acc["width"] is None:
                acc["width"], acc["height"] = size
    elif ctype == b"hdlr" and track is not None:
        if body[8:12] == b"vide":
            track["video"] = True
    elif ctype == b"mdhd" and track is not None:
        timescale = _mdhd_timescale(body)
        if timescale:
            track["timescale"] = timescale
    elif ctype == b"stts" and track is not None:
        totals = _stts_totals(body)
        if totals:
            track["stts"] = totals


def _walk_boxes(buf, pos, end, acc, in_trak, depth, track=None):
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
        _handle_box(ctype, buf[pos + header:pos + size], acc, in_trak, depth, track)
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
             "duration":float|None,"width":int|None,"height":int|None,"fps":float|None}。

    info 是可选的诊断字典（调用方传空 dict 即可），用来记录 moov 落在哪个窗口，
    不参与契约返回值形状。
    """
    out = {"comment": None, "description": None, "prompt": None, "workflow": None,
           "duration": None, "width": None, "height": None, "fps": None}
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
    acc = {"container": [], "track": [], "duration": None, "width": None, "height": None,
           "fps": None, "boxes": 0}
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
    out["fps"] = acc["fps"]
    info["boxes"] = acc["boxes"]
    info["tags"] = sorted(k for k, v in tags.items() if v)
    return out


# ----------------------------------------------------------------- EBML（webm/mkv）

# EBML 元素 = 变长 ID + 变长数据长度 + 内容。两者共用一套「前导零个数 + 1 = 字节数」的编码，
# 区别是 ID 把标记位算进值里（所以 0x83 是 1 字节、0x4489 是 2 字节、0x2AD7B1 是 3 字节、
# 0x18538067 是 4 字节），而长度要去掉标记位；长度值全 1（0xFF、0x01FF…FF）表示未知长度。
# 下面只登记本轮真正要读的元素与判断结构所需的兄弟 ID，其它未知 ID 一律按声明长度跳过。
_EBML_ID_EBML = 0x1A45DFA3
_EBML_ID_DOCTYPE = 0x4282
_EBML_ID_SEGMENT = 0x18538067
_EBML_ID_SEEKHEAD = 0x114D9B74
_EBML_ID_INFO = 0x1549A966
_EBML_ID_TIMECODE_SCALE = 0x2AD7B1
_EBML_ID_DURATION = 0x4489
_EBML_ID_TRACKS = 0x1654AE6B
_EBML_ID_TRACK_ENTRY = 0xAE
_EBML_ID_TRACK_NUMBER = 0xD7
_EBML_ID_TRACK_TYPE = 0x83
_EBML_ID_DEFAULT_DURATION = 0x23E383
_EBML_ID_VIDEO = 0xE0
_EBML_ID_PIXEL_WIDTH = 0xB0
_EBML_ID_PIXEL_HEIGHT = 0xBA
_EBML_ID_DISPLAY_WIDTH = 0xB5
_EBML_ID_DISPLAY_HEIGHT = 0xB6
_EBML_ID_TAGS = 0x1254C367
_EBML_ID_TAG = 0x7373
_EBML_ID_SIMPLE_TAG = 0x67C8
_EBML_ID_TAG_NAME = 0x45A3
_EBML_ID_TAG_STRING = 0x4487
_EBML_ID_CLUSTER = 0x1F43B675
_EBML_ID_BLOCK_GROUP = 0xA0
_EBML_ID_SIMPLE_BLOCK = 0xA3
_EBML_ID_BLOCK_TIMECODE = 0xE3
_EBML_ID_CUES = 0x1C53BB6B
_EBML_ID_CHAPTERS = 0x1043A770
_EBML_ID_VOID = 0xEC
_EBML_ID_CRC32 = 0xBF

_EBML_NAMES = {
    _EBML_ID_EBML: "EBML", _EBML_ID_DOCTYPE: "DocType", _EBML_ID_SEGMENT: "Segment",
    _EBML_ID_SEEKHEAD: "SeekHead", _EBML_ID_INFO: "Info", _EBML_ID_TIMECODE_SCALE: "TimecodeScale",
    _EBML_ID_DURATION: "Duration", _EBML_ID_TRACKS: "Tracks", _EBML_ID_TRACK_ENTRY: "TrackEntry",
    _EBML_ID_TRACK_NUMBER: "TrackNumber", _EBML_ID_TRACK_TYPE: "TrackType",
    _EBML_ID_DEFAULT_DURATION: "DefaultDuration", _EBML_ID_VIDEO: "Video",
    _EBML_ID_PIXEL_WIDTH: "PixelWidth", _EBML_ID_PIXEL_HEIGHT: "PixelHeight",
    _EBML_ID_DISPLAY_WIDTH: "DisplayWidth", _EBML_ID_DISPLAY_HEIGHT: "DisplayHeight",
    _EBML_ID_TAGS: "Tags", _EBML_ID_TAG: "Tag", _EBML_ID_SIMPLE_TAG: "SimpleTag",
    _EBML_ID_TAG_NAME: "TagName", _EBML_ID_TAG_STRING: "TagString", _EBML_ID_CLUSTER: "Cluster",
    _EBML_ID_BLOCK_GROUP: "BlockGroup", _EBML_ID_SIMPLE_BLOCK: "SimpleBlock",
    _EBML_ID_BLOCK_TIMECODE: "Timecode", _EBML_ID_CUES: "Cues", _EBML_ID_CHAPTERS: "Chapters",
    _EBML_ID_VOID: "Void", _EBML_ID_CRC32: "CRC32",
}

# 取值叶子：(解码方式, 落到本层收集袋的键)。收集袋按容器划分，所以块数据里巧合出现的
# Duration 字节不会污染 Info 的值（Video 例外：它把尺寸直接写进所属 TrackEntry 的袋子）。
_EBML_VALUES = {
    _EBML_ID_DOCTYPE: ("text", "doctype"),
    _EBML_ID_TIMECODE_SCALE: ("uint", "timecode_scale"),
    _EBML_ID_DURATION: ("float", "duration"),
    _EBML_ID_TRACK_TYPE: ("uint", "track_type"),
    _EBML_ID_DEFAULT_DURATION: ("uint", "default_duration"),
    _EBML_ID_PIXEL_WIDTH: ("uint", "pixel_width"),
    _EBML_ID_PIXEL_HEIGHT: ("uint", "pixel_height"),
    _EBML_ID_DISPLAY_WIDTH: ("uint", "display_width"),
    _EBML_ID_DISPLAY_HEIGHT: ("uint", "display_height"),
    _EBML_ID_TAG_NAME: ("text", "tag_name"),
    _EBML_ID_TAG_STRING: ("text", "tag_string"),
}

_EBML_CONTAINERS = ("EBML", "Segment", "Info", "Tracks", "TrackEntry", "Video", "Tags", "Tag",
                    "SimpleTag")
# 这些元素允许未知长度（直播式写入 / 尾部追加封装）；其余未知长度一律就地收摊。
_EBML_UNKNOWN_SIZE = frozenset((_EBML_ID_SEGMENT, _EBML_ID_CLUSTER, _EBML_ID_TRACKS, _EBML_ID_TAGS))
_EBML_SEGMENT_SIBLINGS = frozenset((_EBML_ID_EBML, _EBML_ID_SEGMENT, _EBML_ID_SEEKHEAD,
                                    _EBML_ID_INFO, _EBML_ID_TRACKS, _EBML_ID_CHAPTERS,
                                    _EBML_ID_CLUSTER, _EBML_ID_CUES, _EBML_ID_TAGS,
                                    _EBML_ID_VOID, _EBML_ID_CRC32))
# 未知长度容器靠「合法子元素 / 兄弟 ID」判断边界：别把子元素当顶层，也别把顶层当子元素。
# Cluster 不在这张表里：它的内容由 _ebml_skip_cluster 整段按声明长度跳过（块数据里的巧合字节
# 一律不解析），边界只看 Segment 级兄弟 ID。
_EBML_CHILDREN = {
    "top": _EBML_SEGMENT_SIBLINGS,
    "Segment": _EBML_SEGMENT_SIBLINGS,
    "Tracks": frozenset((_EBML_ID_TRACK_ENTRY,)),
    "Tags": frozenset((_EBML_ID_TAG,)),
}
# Matroska 的 TimecodeScale 缺省是 1000000 纳秒 = 1 毫秒，规范允许文件里不写这个元素。
_EBML_DEFAULT_TIMECODE_SCALE = 1000000
# ffmpeg/VHS 写 `-metadata comment=...` 就落在 Tags 的 SimpleTag 里；title 是 comment 的回落，
# prompt/workflow 是少数封装器直接写自由键的情况。大小写不敏感，取值顺序与 mp4 那条路一致。
_EBML_TAG_KEYS = ("comment", "description", "title", "prompt", "workflow")


def _ebml_state(stream, size, limit, acc, window):
    """一趟扫描的可变状态：流、文件大小、本窗口允许走到的位置、元素/字节计数、待回头元素。"""
    return {"f": stream, "size": size, "limit": limit, "acc": acc, "window": window,
            "pos": 0, "read": 0, "elements": 0, "clusters": 0, "pending": None, "stopped": ""}


def _ebml_stop(st, reason):
    """第一个停止原因最接近根因，后面的只记账不覆盖。"""
    if not st["stopped"]:
        st["stopped"] = reason
    st["acc"]["stopped"].add(reason)


def _ebml_read(st, count):
    """顺序读取，受字节上限约束：超限或 EOF 返回空，调用方按「读不满」处理。"""
    if count <= 0:
        return b""
    if st["read"] + count > MAX_EBML_READ_BYTES:
        _ebml_stop(st, "read_limit")
        return b""
    data = st["f"].read(count)
    st["read"] += len(data)
    st["pos"] += len(data)
    return data


def _ebml_seek_to(st, offset):
    st["pos"] = offset
    try:
        st["f"].seek(offset)
    except Exception:
        _ebml_stop(st, "seek")
        return False
    return True


def _ebml_skip(st, count):
    """按声明长度跳过内容（不读）：Cluster 的块数据就是这么越过去的。"""
    if count <= 0:
        return True
    if st["pos"] + count > st["size"]:
        _ebml_stop(st, "declared_beyond_eof")
        return False
    return _ebml_seek_to(st, st["pos"] + count)


def _ebml_vint(st, is_id):
    """变长整数：is_id=True 保留前导标记位（ID 的值含标记位），False 时去掉。

    返回 (值, 字节数)；长度值全 1 时值为 None（未知长度），起始字节不合法时返回 None。
    """
    first = _ebml_read(st, 1)
    if not first:
        return None
    byte = first[0]
    if byte == 0:
        return None                                     # 0x00 不是合法的起始字节
    mask, zeros = 0x80, 0
    while mask and not byte & mask:
        mask >>= 1
        zeros += 1
    length = zeros + 1
    if length > (4 if is_id else 8):
        return None                                     # 元素 ID 最长 4 字节
    rest = _ebml_read(st, length - 1) if length > 1 else b""
    if len(rest) != length - 1:
        return None
    raw = int.from_bytes(bytes([byte]) + rest, "big")
    if is_id:
        return raw, length
    low = (1 << (7 * length)) - 1                        # 标记位之外剩下的 value 位
    value = raw & low
    return (None if value == low else value), length


def _ebml_header(st):
    """下一个元素的 (ID, 声明长度)；长度 None 表示未知长度。读不到返回 None。"""
    ident = _ebml_vint(st, True)
    if ident is None:
        return None
    size = _ebml_vint(st, False)
    if size is None:
        return None
    return ident[0], size[0]


def _ebml_next(st):
    """取下一个元素头；上一层退回的元素先消化退回项（不重复读字节）。"""
    if st["pending"] is not None:
        item, st["pending"] = st["pending"], None
        return item
    return _ebml_header(st)


def _ebml_uint(data):
    """uINT：1-8 字节大端无符号。宽度 0 或超过 8 字节都是畸形声明，不猜。"""
    if not data or len(data) > 8:
        return None
    return int.from_bytes(data, "big")


def _ebml_float(data):
    """FLOAT：规范只有 4/8 字节，但实测有 0/1/2 字节的畸形写法 —— 高位补零按 double 解。"""
    if not data or len(data) > 8:
        return None
    try:
        if len(data) == 8:
            value = struct.unpack(">d", data)[0]
        elif len(data) == 4:
            value = struct.unpack(">f", data)[0]
        else:
            value = struct.unpack(">d", b"\x00" * (8 - len(data)) + data)[0]
    except (struct.error, IndexError):
        return None
    return value if math.isfinite(value) else None


def _ebml_text(data):
    """STRING/UTF-8：去掉尾部 NUL，坏字节按 replace 解，绝不抛。"""
    if not data:
        return ""
    return data.rstrip(b"\x00").decode("utf-8", "replace")


def _ebml_leaf(st, size):
    """读叶子元素内容：过大就跳过（返回 None 当缺失），读不满就当截断。"""
    if not size:
        return None
    if size > MAX_EBML_LEAF_BYTES:
        _ebml_skip(st, size)
        st["acc"]["big_leaves"] += 1
        return None
    data = _ebml_read(st, size)
    return data if len(data) == size else None


def _ebml_value(st, spec, data, ctx):
    kind, key = spec
    if kind == "uint":
        value = _ebml_uint(data)
    elif kind == "float":
        value = _ebml_float(data)
    else:
        value = _ebml_text(data) or None
    if value is None:
        return
    if key == "doctype":                          # 诊断字段，属于整趟扫描而不某一层
        st["acc"]["doctype"] = value
        return
    ctx.setdefault(key, value)                    # 同层重复元素按规范顺序取第一个


def _ebml_settle(st, name, ctx):
    """容器退出时结算：时长走 Info，帧率/宽高走视频轨，键值走 SimpleTag。"""
    acc = st["acc"]
    if name == "Info":
        raw = ctx.get("duration")
        if acc["duration"] is None and raw is not None and raw > 0:
            # TimecodeScale 可以缺席（缺省 1000000 纳秒 = 1 毫秒），用时如实区分是否真的读到过。
            scale = ctx.get("timecode_scale")
            seconds = round(float(scale or _EBML_DEFAULT_TIMECODE_SCALE) * float(raw) / 1e9, 6)
            if 0 < seconds < 1e9:                 # 畸形宽度的 FLOAT 解出次正范数也会被这里挡掉
                acc["duration"] = seconds
                acc["used"].update(("Info", "Duration"))
                if scale:
                    acc["used"].add("TimecodeScale")
        return
    if name == "TrackEntry":
        if ctx.get("track_type") != 1:            # 1 = 视频轨；声音/字幕轨不算帧率
            return
        acc["used"].add("TrackType")
        default_duration = ctx.get("default_duration")
        if acc["fps"] is None and default_duration:
            # DefaultDuration 的单位是纳秒/帧（规范原文：帧率写作 1000000000/duration）。
            # 实测 ffmpeg libvpx-vp9 与 libx264 都按 ns 写：r=8→125000000、24→41666666、
            # 25→40000000、30→33333333、23.976→41708375，1e9/DD 正好还原帧率。
            fps = 1000000000.0 / float(default_duration)
            if 0.01 <= fps <= 10000:              # 越界的畸形声明不写进 fps
                acc["fps"] = round(fps, 3)        # 与 mp4 同口径：3 位小数，拿不到留 None
                acc["used"].add("DefaultDuration")
        if acc["width"] is None:
            size = (ctx.get("pixel_width"), ctx.get("pixel_height"))
            if size[0] and size[1]:
                acc["used"].update(("Video", "PixelWidth", "PixelHeight"))
            else:
                size = (ctx.get("display_width"), ctx.get("display_height"))
                if size[0] and size[1]:
                    acc["sizing"] = "display"     # 只有显示尺寸：宽高带比例，如实标出来
                    acc["used"].update(("Video", "DisplayWidth", "DisplayHeight"))
                else:
                    return
            acc["width"], acc["height"] = size
        return
    if name == "SimpleTag":
        tag = _text(ctx.get("tag_name")).strip().lower()
        value = ctx.get("tag_string")
        if tag in _EBML_TAG_KEYS and value:
            acc["tags"].setdefault(tag, value)
            acc["tags_at"] = st["window"]
            acc["used"].update(("Tags", "Tag", "SimpleTag", "TagName", "TagString"))


def _ebml_walk(st, end, scope, ctx, depth):
    """流式遍历一个容器的体。end 是体末尾；None 表示本层未知长度，靠读到兄弟 ID 收尾。"""
    if depth > MAX_DEPTH:
        _ebml_stop(st, "depth")
        return
    while st["elements"] < MAX_EBML_ELEMENTS and not st["stopped"]:
        head = _ebml_next(st)
        if head is None:
            return
        eid, size = head
        body = st["pos"]
        if body > st["limit"]:
            _ebml_stop(st, "window")              # 超出首窗口的位置边界（尾部另有窗口兜底）
            return
        if end is not None and (body > end or (size is not None and body + size > end)):
            st["pending"] = head                  # 这个元素不属于本层，退回给上一层
            return
        if end is None and scope != "top" and eid not in _EBML_CHILDREN.get(scope, ()):
            st["pending"] = head                  # 未知长度容器的第一个「不是子元素」就是兄弟
            return
        if size is None and (eid not in _EBML_UNKNOWN_SIZE
                             or eid not in _EBML_CHILDREN.get(scope, ())):
            st["pending"] = head
            _ebml_stop(st, "unknown_size")        # 没有长度又没有兄弟边界可依，就地收摊
            return
        name = _EBML_NAMES.get(eid)
        st["elements"] += 1
        if name == "Cluster":                     # 块数据里没有我们要的字段：整段跳过
            st["clusters"] += 1
            if size is None:
                _ebml_skip_cluster(st, end, depth + 1)
            elif not _ebml_skip(st, size):
                return
            continue
        if name == "Segment":
            st["acc"]["segment"] = True
        spec = _EBML_VALUES.get(eid)
        if spec is not None:
            _ebml_value(st, spec, _ebml_leaf(st, size), ctx)
            continue
        if name in _EBML_CONTAINERS:
            child_ctx = ctx if name == "Video" else {}
            _ebml_walk(st, body + size if size is not None else None, name, child_ctx, depth + 1)
            if name != "Video":
                _ebml_settle(st, name, child_ctx)
            continue
        if size is None:
            _ebml_stop(st, "unknown_leaf")
            return
        if not _ebml_skip(st, size):              # 未知 ID / 不关心的叶子：按声明长度跳过
            return


def _ebml_skip_cluster(st, end, depth):
    """未知长度的 Cluster：只读元素头、按声明长度跳过内容，读到下一个 Segment 级兄弟 ID 就停。

    这里绝不把子元素当取值元素解析（块数据里巧合出现的 0xB0 会被读成 PixelWidth），也不允许
    无限下钻：子元素再是未知长度就收摊，元素数与字节数另有封顶。
    """
    if depth > MAX_DEPTH:
        _ebml_stop(st, "depth")
        return
    while st["elements"] < MAX_EBML_ELEMENTS and not st["stopped"]:
        head = _ebml_next(st)
        if head is None:
            return
        eid, size = head
        body = st["pos"]
        if body > st["limit"]:
            _ebml_stop(st, "window")
            return
        if eid in _EBML_SEGMENT_SIBLINGS or (end is not None and size is not None
                                            and body + size > end):
            st["pending"] = head                  # 已经是下一个兄弟（或另一个 Cluster）
            return
        if size is None:
            st["pending"] = head
            _ebml_stop(st, "unknown_block")       # 块必须有长度，没有就无法定位下一个
            return
        st["elements"] += 1
        if not _ebml_skip(st, size):
            return


def _ebml_acc():
    return {"segment": False, "duration": None, "fps": None,
            "width": None, "height": None, "sizing": None, "tags": {}, "tags_at": None,
            "doctype": None, "used": set(), "stopped": set(), "big_leaves": 0}


def _ebml_scan_tail(buf, acc):
    """尾窗口反推 Tags 起点：'Tags' 的四字节 ID 在块数据里也可能巧合出现，靠首个子元素是
    Tag 来校验（与 mp4 的 _moov_spans 同一个思路）。找到就解析，找不到就什么也不做。"""
    st = _ebml_state(io.BytesIO(buf), len(buf), len(buf), acc, "tail")
    needle = _EBML_ID_TAGS.to_bytes(4, "big")
    start = 0
    while start < len(buf) and st["elements"] < MAX_EBML_ELEMENTS:
        index = buf.find(needle, start)
        if index < 0:
            break
        start = index + 4
        st["stopped"] = ""                        # 一个候选的失败不该挡住后面的候选
        if not _ebml_seek_to(st, index):
            break
        head = _ebml_header(st)
        if head is None or head[0] != _EBML_ID_TAGS:
            continue
        size = head[1]
        body = st["pos"]
        if size is None or size < 8 or body + size > len(buf) or size > MAX_EBML_LEAF_BYTES:
            continue
        child = _ebml_header(st)                  # 结构校验：Tags 的第一个子元素必须是 Tag
        if child is None or child[0] != _EBML_ID_TAG:
            continue
        st["pending"] = child
        st["elements"] += 1
        _ebml_walk(st, body + size, "Tags", {}, 1)
    return st["elements"]


def read_ebml_meta(path, max_window_bytes=MAX_WINDOW_BYTES, info=None):
    """返回 {"comment","description","prompt","workflow","duration","width","height","fps"}，
    形状与 read_mp4_meta 一致，所以四个标签位走同一个后处理入口（_absorb）。

    不依赖 SeekHead/Cues：从头顺扫，Cluster 按声明长度跳过；文件超过窗口大小时再加一段尾部
    窗口兜底（有的封装器把 Tags 写在最后一个 Cluster 之后）。任何畸形输入只降级不抛异常。
    info 是可选诊断袋（记录实际用到的元素名），不参与契约返回值形状。
    """
    out = {"comment": None, "description": None, "prompt": None, "workflow": None,
           "duration": None, "width": None, "height": None, "fps": None}
    if info is None:
        info = {}
    size = os.path.getsize(path)
    acc = _ebml_acc()
    elements = 0
    clusters = 0
    read_bytes = 0
    window = "whole" if size <= max_window_bytes else "head+tail"
    with open(path, "rb") as f:
        limit = size if size <= max_window_bytes else max_window_bytes
        st = _ebml_state(f, size, limit, acc, window if window == "whole" else "head")
        _ebml_walk(st, None, "top", {}, 0)
        elements += st["elements"]
        clusters += st["clusters"]
        read_bytes += st["read"]
        if size > max_window_bytes:
            # 为什么还要读尾部：有的封装器把 Tags 写在最后一个 Cluster 之后，只扫文件头取不到。
            f.seek(size - max_window_bytes)
            tail = f.read(max_window_bytes)
            read_bytes += len(tail)
            elements += _ebml_scan_tail(tail, acc)
    tags = acc["tags"]
    out["comment"] = tags.get("comment") or tags.get("title")   # comment 优先，title 回落
    out["description"] = tags.get("description")
    out["prompt"] = tags.get("prompt")
    out["workflow"] = tags.get("workflow")
    out["duration"] = acc["duration"]
    out["width"] = acc["width"]
    out["height"] = acc["height"]
    out["fps"] = acc["fps"]
    info["segment_found"] = acc["segment"]
    info["window"] = window
    info["elements"] = elements
    info["clusters"] = clusters
    info["read_bytes"] = read_bytes
    info["used"] = sorted(acc["used"])
    info["tags"] = sorted(tags)
    info["tags_at"] = acc["tags_at"]
    if acc["doctype"]:
        info["doctype"] = acc["doctype"]
    if acc["sizing"]:
        info["sizing"] = acc["sizing"]
    if acc["big_leaves"]:
        info["big_leaves"] = acc["big_leaves"]
    if acc["stopped"]:
        info["stopped"] = sorted(acc["stopped"])
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
        _apply_param_fallback(out, api, notes, extra)
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


def _container_payload(meta):
    """容器里读出的四个标签位喂统一后处理入口：mp4 的 udta 与 EBML 的 Tags 走同一个 _absorb。"""
    return {k: v for k, v in meta.items()
            if k in ("comment", "description", "prompt", "workflow") and v}


def _merge_stream_fields(out, meta):
    """时长/宽高/帧率属于容器，即使没有元数据也要回填。"""
    if out["width"] is None:
        out["width"] = meta["width"]
    if out["height"] is None:
        out["height"] = meta["height"]
    if meta.get("duration") is not None:
        out["duration"] = meta["duration"]
    if meta.get("fps") is not None:
        out["fps"] = meta["fps"]


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
        payloads = _container_payload(meta)
        if payloads:
            source = "mp4_udta"
        if not mp4_info.get("moov_found"):
            notes.append("未找到 moov（可能超出首尾各 16MB 窗口）。")
        _merge_stream_fields(out, meta)
    elif ext in (".webm", ".mkv"):
        ebml_info = {}
        meta = read_ebml_meta(target, max_window_bytes, ebml_info)
        extra["ebml"] = ebml_info
        payloads = _container_payload(meta)
        if payloads:
            # 值来自 Segment → Tags → Tag → SimpleTag → TagString，与 mp4 的 udta 平级但来源分开
            source = "ebml_tag"
        if not ebml_info.get("segment_found"):
            notes.append("未找到 Matroska Segment（文件不是 EBML，或结构超出首尾各 16MB 窗口）。")
        _merge_stream_fields(out, meta)

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
