# -*- coding: utf-8 -*-
r"""分流决策层：输入「类型 / safetensors 头 / 目录现状」，输出模型本体的落点，不做下载、不写盘、不碰 UI。

为什么要它：downloader.py 的 _resolve_dest 只看全局目标目录，模型本体全堆在同一处，ComfyUI 侧
folder_paths 扫不到，用户下完还得手工搬。目标落点形状（用户决策，嵌套一层模型名目录）：

    <ComfyUI>\models\<类型key>\<模型名>\<模型名>.safetensors

本模块与网络/磁盘完全解耦：嗅探头部靠调用方注入的 fetch(start, end) 闭包（Bearer、代理、重试都在调用方），
落点校验只读 os.path.isdir / os.access，只有 create_missing=True 才可能 mkdir。

三条硬约束（来源：Civitai 官方 enums + 本机 ComfyUI folder_paths.py 实测），改表之前先读完：
1. Civitai model.type 的权威 23 值是 Controlnet（小写 n）/ Workflows（复数），**不存在** Workflow /
   TriggerWords / LoRA / LYCORIS 这些拼写；但历史数据里混存旧拼写（ControlNet、LoRA、Model、
   Textual Inversion），所以查表一律走 normalize_type，表里新旧拼写都留键。
2. 落点只允许是核心 key（CORE_FOLDERS）。animatediff_models / animatediff_motion_lora / ipadapter /
   pulid / instantid 这些不是核心 key，只有自定义节点注册才生效；而且节点用
   folder_paths.add_model_folder_path 注册未知 key 时扩展名集合是空集，会把该目录下**任意文件**
   （含预览图、txt）列进节点下拉框。所以 MotionModule 这类一律不分流，别为了"看起来分流了"扩表。
3. 落点目录名 == key 名（models/checkpoints、models/diffusion_models…）。注意 folder_paths 是
   「同一 key 挂多个路径」的别名机制：本机实测 text_encoders 第一路径是 models/text_encoders（别名
   models/clip），controlnet 第一路径是 models/controlnet（别名 models/t2i_adapter），但
   diffusion_models 的第一路径是 models/unet、models/diffusion_models 是第二路径 —— 和方案里
   「写入必须用第一路径」的说法对不上，所以这里按 key 名落点（两个路径 ComfyUI 都会扫到，
   而 models/unet 在多数整合包里压根不存在）。resolve_dest_dir 要求 key 名目录已存在，否则返回
   None 让调用方退回旧行为 —— 宁可不分流，也不要造出扫不到的目录。
4. 「Civitai 类型 → ComfyUI 目录」的**唯一事实源就是本模块**：model_manager 的 KNOWN_TYPES /
   ENV_DIRS["comfyui"] 由 COMFYUI_TYPE_DIRS / KNOWN_MODEL_TYPES 派生，两边逐键一致。
   （旧模型管理器自带过一份只覆盖 6 个目录的 comfyui 表，而 webui.mm_restore_organize 会用
   `set(ENV_DIRS["comfyui"].values())` 反推「标准目录」，把不在集合里的顶层目录当成非标准把模型
   搬回去 —— 分流写进 diffusion_models 的权重会被「误整理恢复」搬回 checkpoints，自己打自己。）
   files[].type 的语义（17 值枚举 / 权重白名单 / 归一化）唯一事实源在 civitai_api，本模块只 import
   不复制；本模块独有的只有「分流专属」的 FILE_TYPE_TO_FOLDER / NON_WEIGHT_FILE_TYPES 与展示拼写表。
"""
import json
import os
import re
import unicodedata
from collections import namedtuple

from civitai_api import (ATTACHMENT_FILE_TYPES, CIVITAI_FILE_TYPES, DOWNLOADABLE_FILE_TYPES,
                         WEIGHT_FILE_TYPES as WEIGHT_FILE_KINDS, normalize_file_type)
"""files[].type 的判定语义一律复用 civitai_api（纯标准库、import 时不发网络请求）：
WEIGHT_FILE_KINDS / ATTACHMENT_FILE_TYPES / DOWNLOADABLE_FILE_TYPES 都是**归一化后**的集合，
比对前统一走 normalize_file_type，避免「同名不同义」在跨模块 import 时静默错配。"""

RouteDecision = namedtuple("RouteDecision", "folder reason source")
"""folder = 核心 key（None = 不分流）；reason = 给人看的一句话；source ∈ {"type","header","base_model","none"}。"""

MAX_HEADER_SIZE = 100_000_000
"""safetensors 头 JSON 长度上限。实测 Pony SDXL 头 365,152 B、kohya LoRA 头 79,669 B，
所以固定 4K/64K 预取必然不够 —— 头部长度要从文件前 8 字节读，见 probe_header。"""

CORE_FOLDERS = {
    "checkpoints", "diffusion_models", "loras", "vae", "text_encoders",
    "controlnet", "clip_vision", "embeddings", "hypernetworks", "upscale_models",
}
"""允许作为落点的 ComfyUI 核心 key（第一路径名）。取值 == MODEL_TYPE_TO_FOLDER 的值域，一个都不多。
被排除的核心 key：configs 只收 .yaml、diffusers 的白名单是 "folder"（整仓目录）、
classifiers 白名单是空串、custom_nodes/datasets 不是模型目录、gligen/photomaker/style_models/
vae_approx/latent_upscale_models/model_patches 等没有任何 Civitai 类型对得上。"""

MODEL_TYPE_TO_FOLDER = {
    # Civitai ModelType 权威 23 值中「有核心目录可落」的那些
    "Checkpoint": "checkpoints",
    "TextualInversion": "embeddings",
    "Hypernetwork": "hypernetworks",
    "LORA": "loras",
    "LoCon": "loras",
    "DoRA": "loras",
    "Controlnet": "controlnet",
    "Upscaler": "upscale_models",
    "VAE": "vae",
    "TextEncoder": "text_encoders",
    "UNet": "diffusion_models",
    "CLIPVision": "clip_vision",
    # Civitai 的 CLIP 类型历史上就是文本编码器模型（clip 是 text_encoders 的旧目录名/别名）
    "CLIP": "text_encoders",
    # ---- 历史混存的旧拼写 / 社区叫法：normalize 后与上面同键，或补上权威表没有的形状 ----
    "ControlNet": "controlnet",            # 旧拼写（权威是 Controlnet）
    "LoRA": "loras",                       # 权威值是 LORA
    "LYCORIS": "loras",
    "LOHA": "loras",
    "LOKR": "loras",
    "Model": "checkpoints",                # 早期 Civitai 把模型本体叫 Model
    "Textual Inversion": "embeddings",      # 带空格的旧写法
    "Diffusion Model": "diffusion_models",
    # 旧 model_manager 表里一直在用的社区叫法（权威值是 TextualInversion）：
    # 历史 .civitai.info / .info.json 里真写过 "Embedding"，漏了它就等于老数据不再被认成已知类型。
    "Embedding": "embeddings",
}
"""键是 Civitai 显示拼写（只为可读性）；查表走 _TYPE_INDEX，比较用 normalize_type。
这份 dict 同时是 model_manager.ENV_DIRS["comfyui"] 的唯一来源（见 COMFYUI_TYPE_DIRS），
所以「分流落到哪」和「旧整理/误整理恢复认为哪个目录标准」不可能再对不上。"""

NON_MODEL_TYPES = {
    "AestheticGradient", "MotionModule", "Poses", "Wildcards", "Workflows",
    "ComfyWorkflows", "Detection", "VisionLanguage", "LLM", "Other",
    "Workflow", "TriggerWords",  # 权威 23 值里没有这两个拼写，收着只当历史脏值的防御
}
"""不分流的类型：工作流/姿势/词表/数据集类不是权重，且没有核心目录；
MotionModule 有 animatediff_models 目录但那不是核心 key（约束 2）；
Detection 虽有核心 detection key，但 Civitai 那类资源是数据集/检测器包，按方案一律不分流；
AestheticGradient 落到哪由 aesthetic gradient 专用节点决定，同样不是核心 key。"""

CIVITAI_MODEL_TYPES = (
    "Checkpoint", "TextualInversion", "Hypernetwork", "AestheticGradient", "LORA", "LoCon", "DoRA",
    "Controlnet", "Upscaler", "MotionModule", "VAE", "TextEncoder", "UNet", "CLIPVision", "Poses",
    "Wildcards", "Workflows", "ComfyWorkflows", "Detection", "VisionLanguage", "CLIP", "LLM", "Other",
)
"""Civitai ModelType 的权威 23 值（精确大小写）。表驱动测试拿它逐个核对：
每个值要么在 MODEL_TYPE_TO_FOLDER 有落点，要么在 NON_MODEL_TYPES 显式不分流，不许有第三种。"""

# ---- 对外派生表：旧 model_manager 的 KNOWN_TYPES / ENV_DIRS["comfyui"] 直接拿这两个 ----

COMFYUI_TYPE_DIRS = dict(MODEL_TYPE_TO_FOLDER)
"""「Civitai 类型显示拼写 → ComfyUI 核心 key」的对外视图（model_manager.ENV_DIRS["comfyui"] == 这份）。
保持**精确键匹配**（不做 normalize）是有意的：webui.mm_restore_organize 用 `env_map.get(mtype)`
反推标准目录，键形状必须一致，否则它查不到 → 把分流产物当非标准目录搬走。
值集合就是「ComfyUI 侧的标准顶层目录」全集：分流写进去的目录，整理侧一定认。"""

KNOWN_MODEL_TYPES = frozenset(MODEL_TYPE_TO_FOLDER) | frozenset(NON_MODEL_TYPES)
"""「认识的 C 站类型」全集（model_manager.KNOWN_TYPES == 这份）：有落点的 + 显式不分流的 + 旧拼写。
不分流的类型留在集合里是旧整理流程的需要（Workflows/Poses/Wildcards… 由用户显式发起、走 TYPE_DIRS），
分流对它们一律返回 folder=None，两条链路不会互相触发。"""

# ---- files[].type：判定语义只有一份，就在 civitai_api --------------------------------
# 权威 17 值 + 已废弃旧值（CIVITAI_FILE_TYPES）与权重白名单（WEIGHT_FILE_KINDS）都是归一化形式；
# 本模块以前自带一份 {"Model", "Pruned Model", "Diffusion Model", "UNet"} 原始大小写集合，
# 同名不同义，跨模块 import 会静默错配 —— 删掉了，只留下面这张「纯展示拼写」映射。

FILE_TYPE_KINDS = CIVITAI_FILE_TYPES
"""files[].type 的合法取值全集（归一化形式），转发 civitai_api 的枚举，本模块不另立权威。"""

FILE_TYPE_LABELS = {
    "model": "Model", "textencoder": "Text Encoder", "visionencoder": "Vision Encoder",
    "prunedmodel": "Pruned Model", "negative": "Negative", "trainingdata": "Training Data",
    "traindata": "Training Data", "vae": "VAE", "config": "Config", "archive": "Archive",
    "unet": "UNet", "diffusionmodel": "Diffusion Model", "clipvision": "CLIPVision",
    "controlnet": "ControlNet", "workflow": "Workflow", "upscaler": "Upscaler",
    "enhancementlora": "Enhancement LoRA", "other": "Other",
    "inpaintmask": "InpaintMask", "template": "Template", "image": "Image",
}
"""归一化 → Civitai 页面显示拼写。**纯展示**，不含任何判定语义；判定永远走 file_type_kind + 归一化集合。"""

WEIGHT_FILE_TYPES = frozenset(FILE_TYPE_LABELS.get(kind, kind) for kind in WEIGHT_FILE_KINDS)
"""显示拼写形式的权重白名单，**由 civitai_api.WEIGHT_FILE_TYPES 派生**（本模块不再复制字面量）。
留这个名字只是因为 webui._needs_header_probe 拿原始 files[].type 字符串直接 `in` 它；
新代码请一律用 is_weight_file_type()，别再自己比对大小写敏感的显示名。"""

FILE_TYPE_TO_FOLDER = {
    "Text Encoder": "text_encoders",
    "Vision Encoder": "clip_vision",
    "CLIPVision": "clip_vision",
    "VAE": "vae",
    "UNet": "diffusion_models",
    "Diffusion Model": "diffusion_models",
    "ControlNet": "controlnet",
    "Upscaler": "upscale_models",
    "Enhancement LoRA": "loras",
}
"""附件自身的类型 → 落点。Model / Pruned Model 是泛称（可能是 checkpoint 也可能是 unet），
故意不在这里给提示，交给 model_type 或头部判。优先级低于 model_type。"""

NON_WEIGHT_FILE_TYPES = {"Negative", "Training Data", "Config", "Archive", "Workflow", "Other"}
"""明确不是模型本体的附件类型：拿到这些一律不分流，别把 Negative.txt 或 training data 归档塞进模型目录。
分流专属表，故意不直接复用 civitai_api.ATTACHMENT_FILE_TYPES：那份集合为了「能不能下载」还收了
image/traindata/inpaintmask/template，而 Negative 在 civitai_api 侧被单独排除（它可下载）；
这里的口径只看「能不能当模型本体落点」，比那份更严，所以另立一张表（值仍按归一化比对）。"""

# normalize 后的索引：新旧拼写、大小写、空格/连字符/下划线差异全部折叠掉，在 normalize_type 之后构建
_TYPE_INDEX = {}
_FILE_INDEX = {}
_NON_TYPE_INDEX = set()
_NON_FILE_INDEX = set()

_BASE_MODEL_FALLBACK_PREFIXES = (
    "sd1", "sd2", "sdxl", "sd3", "pony", "illustrious", "noobai", "flux",
    "kolors", "hunyuan", "hidream", "lumina", "chroma", "neta", "wan", "mochi",
    "stablecascade", "novelai",
)
"""只有明确的 SD / Flux 系底座才值得兜底猜 checkpoints（这些底座上的资源绝大多数是 checkpoint）；
冷门底座（SVD、音频、视频专用）宁可不猜，返回 None 让调用方保持现状。"""


def normalize_type(s):
    """去空格 / 连字符 / 下划线并小写，用于兼容历史拼写（"Textual Inversion" ≡ "textualinversion"）。"""
    return re.sub(r"[\s\-_]+", "", str(s if s is not None else "")).casefold()


def file_type_kind(value):
    """files[].type → 归一化形式，**口径与 civitai_api.normalize_file_type 完全一致**。

    接受 dict（Civitai files[] 的一项：缺 type 键按 "Model"，和 civitai_api 一样）或裸类型字符串。
    裸字符串为空/None 时返回 ""（= 没有文件类型信息），不许被默认成 Model 而混进权重白名单。
    """
    if isinstance(value, dict):
        return normalize_file_type(value)
    if value is None or not str(value).strip():
        return ""
    return normalize_file_type({"type": str(value)})


def is_weight_file_type(value):
    """这个 files[].type（dict 或裸字符串）是不是「模型本体权重」。

    判定语义唯一来自 civitai_api.WEIGHT_FILE_TYPES（Model / Pruned Model / Diffusion Model / UNet）；
    Civitai 对 Config / Archive / Workflow / Training Data 也打 primary，所以任何「挑模型本体」的场合
    都必须先过这里（civitai_api.pick_file、model_manager.choose_body_file 同一个事实源）。
    """
    return file_type_kind(value) in WEIGHT_FILE_KINDS


for _k, _v in MODEL_TYPE_TO_FOLDER.items():
    _TYPE_INDEX.setdefault(normalize_type(_k), _v)
for _k, _v in FILE_TYPE_TO_FOLDER.items():
    _FILE_INDEX.setdefault(file_type_kind(_k), _v)
for _k in NON_MODEL_TYPES:
    _NON_TYPE_INDEX.add(normalize_type(_k))
for _k in NON_WEIGHT_FILE_TYPES:
    _NON_FILE_INDEX.add(file_type_kind(_k))


# ---- safetensors 头 ---------------------------------------------------------------

def _quant_companion(key):
    """量化伴生张量（gguf/scaled-fp8 之类）：只给 scale/min/max，不带结构信息，参与判定会误判。"""
    return key.endswith((".scale_weight", ".min", ".max"))


def header_keys(header):
    """把「头」归一成张量名集合。接受 parse_safetensors_header 的 dict、张量名可迭代对象或 None。"""
    if not header:
        return ()
    if isinstance(header, dict):
        items = list(header.keys())
    else:
        items = [str(k) for k in header if k is not None]
    return tuple(k for k in items if isinstance(k, str) and k != "__metadata__" and not _quant_companion(k))


def classify_safetensors_keys(keys):
    """按张量名判定核心 key，判不出返回 None。

    顺序是硬性的（错一步就把全量 FT 认成 LoRA、把 checkpoint 认成 diffusion_models）：
    1 文本编码器 → 2 MotionModule（不分流）→ 3 LoRA → 4 DiT/UNet 主干 → 5 扩散权重 → 6 VAE。
    所有 marker 一律用 **顶层前缀**（startswith）而不是「包含」，两处必须这样：
    · checkpoint 的 `model.diffusion_model.input_blocks.0...` 里就含 `blocks.`，用包含会把
      SD1.5 全家桶认成 flux DiT；
    · SDXL/Pony checkpoint 含 `cond_stage_model.transformer.text_model.*`、Flux/SD3 checkpoint 含
      `text_encoders.t5xx.*` 与 `text_encoders.clip_l.*` 这些**内部塔**，按包含（或按顶层前缀但不看
      主干）会把每一个完整 checkpoint 都误投进 text_encoders。所以 rule1 额外要求「没有主干权重」。
    """
    folder, kind = _classify_keys(header_keys(keys))
    return folder if kind == "folder" else None


# 顶层前缀集合（判定只用 startswith，见 classify_safetensors_keys 的注释）
_TEXT_ENCODER_PREFIXES = ("text_encoders", "token_embd", "transformer.text_model.", "tokenizer.")
_DIT_PREFIXES = ("double_blocks.", "single_blocks.", "txtmerge.", "imgmerge.", "blocks.")
_DIFFUSION_PREFIX = "model.diffusion_model."
_LORA_PREFIXES = ("lora_unet_", "lora_te_", "base_model.model.", "network.loras.")
_VAE_PREFIXES = ("first_stage_model.encoder", "encoder.", "decoder.")


def _has_backbone(keys):
    """有没有扩散主干（DiT 或 UNet）：有主干的文件是 checkpoint/单挑模型，不是纯文本编码器。"""
    return any(key.startswith(_DIT_PREFIXES) or key.startswith(_DIFFUSION_PREFIX) for key in keys)


def _classify_keys(keys):
    """内部实现：返回 (key 或 None, kind)，kind ∈ {"folder","motion","unknown"}。
    motion 与 unknown 都是 None，但 classify 需要区分「头部明说不分流」和「头部看不出东西」。

    本机抽查 295 个真实文件（models + C 站下载目录）的实测，写死在这儿省得后人重新踩：
    · `loras` / `vae` / 经典 `model.diffusion_model.` checkpoint 都能稳定判出；
    · 顶层平铺 `net.blocks.*`（Anima / SimpleTuner 导出）与 LLM/VLM/GGUF-in-safetensors 认不出 →
      kind="unknown"，classify 会继续按 model_type 走，这是设计好的退化路径；
    · 新型 FP8/int8 全家桶（`model.diffusion_model.*` + `text_encoders.*` + `vae.*`）按 rule5 会落
      diffusion_models 而不是 checkpoints —— 只有 `first_stage_model.` / `conditioner.embedders.`
      才算「带塔的完整 checkpoint」。这类文件要不要算 checkpoint 由接入方（webui/downloader）定口径，
      本模块只按 marker 说话，别看到用户把 AIO 放 checkpoints 就当成这里的 bug 来改表。
    """
    if any(key.startswith(_TEXT_ENCODER_PREFIXES) for key in keys) and not _has_backbone(keys):
        return "text_encoders", "folder"
    for key in keys:
        if key.startswith("motion_modules.") or "zero_g" in key:
            return None, "motion"
    for key in keys:
        if key.startswith(_LORA_PREFIXES) or ".lora_" in key:
            return "loras", "folder"
    if any(key.startswith(_DIT_PREFIXES) for key in keys):
        return "diffusion_models", "folder"
    if any(key.startswith(_DIFFUSION_PREFIX) for key in keys):
        # 带 VAE 塔或文本编码器塔 = 完整 checkpoint，只有扩散部分 = 单挑出来的 unet/diffusion model
        if any(key.startswith(("first_stage_model.", "conditioner.embedders.")) for key in keys):
            return "checkpoints", "folder"
        return "diffusion_models", "folder"
    if any(key.startswith(_VAE_PREFIXES) for key in keys):
        return "vae", "folder"
    return None, "unknown"


def parse_safetensors_header(blob):
    """safetensors 头部解析：前 8 字节 LE u64 = 头 JSON 长度 N，随后 N 字节 UTF-8 JSON。

    结构非法 / JSON 解析失败一律返回 None，绝不抛异常 —— 嗅探失败要退回落全局目录，不能让下载崩掉。
    空对象 `{}` 算结构合法（返回 {}，只是张量名为空帮不上判定）。
    GGUF 不是 safetensors（magic 是 "GGUF"），按扩展名先分流出去，不在这里嗅探。
    """
    try:
        if not isinstance(blob, (bytes, bytearray)) or len(blob) < 9:
            return None
        n = int.from_bytes(bytes(blob[:8]), "little")
        if n <= 0 or n > MAX_HEADER_SIZE:
            return None
        if bytes(blob[8:9]) != b"{":
            return None
        if len(blob) < 8 + n:
            return None  # 头没读全，别拿半截 JSON 去 loads
        data = json.loads(bytes(blob[8:8 + n]).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if any(not isinstance(k, str) for k in data):
        return None
    return data


def probe_header(fetch, max_bytes=MAX_HEADER_SIZE):
    """两段式嗅探：fetch(0, 7) 拿长度 N，再 fetch(8, 8+N-1) 拿头 JSON（end 是闭区间，跟 HTTP Range 一致）。

    fetch 由调用方注入（闭包里带 Bearer / 代理 / 重试），本模块不 import urllib、不发网络请求，
    这样单测可以完全 fake。上限按 max_bytes 夹一次（None = 用 MAX_HEADER_SIZE，更小 = 调用方自己更严，
    0 或负数就等于不接受任何头），防止 Civitai 上被塞了个离谱的长度字段。
    """
    cap = MAX_HEADER_SIZE if max_bytes is None else min(MAX_HEADER_SIZE, max(0, int(max_bytes)))
    try:
        prefix = fetch(0, 7)
    except Exception:
        return None
    if prefix is None or len(prefix) < 8:
        return None
    try:
        n = int.from_bytes(bytes(prefix[:8]), "little")
    except Exception:
        return None
    if n <= 0 or n > cap:
        return None
    try:
        body = fetch(8, 8 + n - 1)
    except Exception:
        return None
    if not body:
        return None
    return parse_safetensors_header(bytes(prefix) + bytes(body))


# ---- 决策 --------------------------------------------------------------------------

def classify(model_type, file_type=None, base_model=None, header=None):
    """给出落点决策。优先级：safetensors 头 > Civitai model.type > （文件类型提示）> base_model 兜底。

    判不出就返回 folder=None，调用方保持现有「全局目标目录」行为；这里绝不返回猜测性的目录。
    """
    keys = header_keys(header)
    t = normalize_type(model_type)
    if keys:
        folder, kind = _classify_keys(keys)
        if kind == "folder":
            if _TYPE_INDEX.get(t) == folder:
                reason = "头部与 C 站类型 %s 一致，落 %s" % (model_type, folder)
            else:
                reason = "以 safetensors 头为准（C 站类型标的是 %s）" % (model_type or "空")
            return RouteDecision(folder, reason, "header")
        if kind == "motion":
            return RouteDecision(None, "头部是 MotionModule，animatediff 目录不是核心 key，不分流", "none")
    ft = file_type_kind(file_type)
    if ft in _NON_FILE_INDEX:
        return RouteDecision(None, "文件类型 %s 不是模型权重，不分流" % (file_type or "空"), "none")
    folder = _TYPE_INDEX.get(t)
    if folder:
        return RouteDecision(folder, "按 C 站模型类型 %s 落 %s" % (model_type, folder), "type")
    if t in _NON_TYPE_INDEX:
        return RouteDecision(None, "类型 %s 没有对应核心目录，不分流" % (model_type or "空"), "none")
    folder = _FILE_INDEX.get(ft)
    if folder:
        return RouteDecision(folder, "模型类型未知，按文件类型 %s 落 %s" % (file_type, folder), "type")
    b = normalize_type(base_model)
    if b and any(b.startswith(p) for p in _BASE_MODEL_FALLBACK_PREFIXES):
        return RouteDecision("checkpoints",
                             "类型与头部都判不出，按底座 %s 兜底放 checkpoints（低置信，可手工调整）" % base_model,
                             "base_model")
    return RouteDecision(None, "类型 / 文件头 / 底座都判不出，交给默认目录", "none")


# ---- 落点目录 -----------------------------------------------------------------------

# Windows 保留设备名（与扩展侧 safeFsName 同一张表）
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"} | \
            {"COM%d" % i for i in range(10)} | {"LPT%d" % i for i in range(10)}
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1F\x7F]')
_INVISIBLE = re.compile(r"[\u200B-\u200F\u202A-\u202E\u2028\u2029\u2060\uFEFF]")
_MAX_NAME_BYTES = 255  # NTFS/ext4 单段目录名硬上限 255 字节


def is_safe_segment(name):
    """判定「可直接当作一级目录名」：不接受分隔符 / `.` / `..` / 盘符 / UNC / 设备名 / 首尾点空格。

    这里是**拒绝**而不是就地清洗：落点目录名要和扩展侧写的目录名一模一样，
    悄悄改写字符会让两边算出不同目录（重复目录 bug 的老根因，见 archive-worker.js 的 archiveDirName 注释）。
    """
    s = str(name if name is not None else "")
    if not s or s in (".", "..") or s.strip("\x00 ") != s:
        return False
    if s.startswith(".") or s.endswith((".", " ")):
        return False
    # _ILLEGAL 已经盖住 / 与 \，所以 "a/b"、"C:\\x"、"\\\\server\\share" 全在这一行挡掉；
    # 盘符单独判一次，避免 POSIX 侧只校验冒号的实现把 "C:x" 当合法名
    if _ILLEGAL.search(s) or _INVISIBLE.search(s) or re.match(r"^[A-Za-z]:", s):
        return False
    head = s.split(".")[0]
    if head.upper() in _RESERVED:
        return False
    return len(s.encode("utf-8", "surrogatepass")) <= _MAX_NAME_BYTES


def resolve_dest_dir(models_root, folder_key, bundle_name, create_missing=False):
    r"""落点 = <models_root>\<folder_key>\<bundle_name>；不合法或目录不齐就返回 None。

    用户决策：folder_key 目录必须**已存在且可写**，create_missing=False（默认）时缺目录就返回 None，
    绝不新建类型目录 —— 新建出来的 models/foo 没人注册，只会变成垃圾目录；调用方拿到 None 走旧行为。
    bundle 目录本身允许还不存在（正要往里下载），由调用方创建。
    """
    root = str(models_root if models_root is not None else "").strip()
    if not root or folder_key not in CORE_FOLDERS:
        return None
    if not is_safe_segment(bundle_name):
        return None
    parent = os.path.join(root, folder_key)
    if not os.path.isdir(parent) or not os.access(parent, os.W_OK):
        return None
    dest = os.path.normpath(os.path.join(parent, str(bundle_name)))
    if os.path.lexists(dest) and not os.path.isdir(dest):
        return None  # 同名文件占位（有人把模型名当文件名用了），不动它
    if not os.path.isdir(dest) and create_missing:
        try:
            os.makedirs(dest)
        except OSError:
            return None
    # bundle 目录不存在也照常返回：正要往里下载，父目录可写即可，建目录是调用方的事。
    # 兜底 containment：清洗过输入，这里再确认一次没跳出 models_root（盘符/大小写/软链都算）
    try:
        ar, ad = os.path.abspath(root), os.path.abspath(dest)
        if os.path.commonpath([ar, ad]) != ar or os.path.dirname(ad) != os.path.abspath(parent):
            return None
    except (ValueError, OSError):
        return None
    return dest


# ---- 目录名清洗（与 chrome-extension/lib/util.js 的 safeFsName 对齐）------------------

def _utf16_units(s):
    """JS 的 charCodeAt 序列（含代理对算两个单元），用来复刻 _cheapHash8 的哈希值。"""
    raw = s.encode("utf-16-le", "surrogatepass")
    return [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]


def _cheap_hash8(s):
    """FNV-1a 32 位（与扩展侧 _cheapHash8 同参数），8 位十六进制。"""
    h = 0x811C9DC5
    for unit in _utf16_units(s):
        h ^= unit
        h = (h * 0x01000193) & 0xFFFFFFFF
    return "%08x" % h


def _graphemes(s):
    """近似字素簇：基字符 + 后续组合/变体标记。够用即可 —— 逐字符截断会把 emoji/组合文字切成半个簇。"""
    out = []
    for ch in s:
        if ch and (unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Mc", "Me")) and out:
            out[-1] += ch
        else:
            out.append(ch)
    return out


def _truncate_utf8(s, max_bytes):
    if len(s.encode("utf-8", "surrogatepass")) <= max_bytes:
        return s
    out, used = [], 0
    for g in _graphemes(s):
        b = len(g.encode("utf-8", "surrogatepass"))
        if used + b > max_bytes:
            break
        out.append(g)
        used += b
    return "".join(out)


def bundle_name_for(model_name, version_name="", max_bytes=140):
    """模型名(+版本) → 目录名，清洗规则与扩展侧 safeFsName 一致，保证桌面与浏览器落进同一个嵌套目录。

    与扩展唯一一处故意不同：safeFsName 空名回退 `file_<Date.now()>`，这里回退固定 "model"
    （决策层要可复现，两边时间戳对不上就等于重复目录）。
    """
    base = str(model_name if model_name is not None else "").strip()
    version = str(version_name if version_name is not None else "").strip()
    raw = base + ("_" + version if version else "")
    s = unicodedata.normalize("NFC", raw)
    s = _INVISIBLE.sub("", s)
    s = _ILLEGAL.sub("_", s)
    s = re.sub(r"[. ]+$", "", s)
    s = re.sub(r"^\.+", "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s or s in (".", ".."):
        return "model"
    limit = max(8, int(max_bytes or 140))
    dot = s.rfind(".")
    stem, ext = s, ""
    if dot > 0 and (len(s) - dot - 1) <= 12:
        stem, ext = s[:dot], s[dot:]
    first = stem.find(".")
    head = stem[:first] if first > 0 else stem
    if head.upper() in _RESERVED:
        stem = head + "_" + (stem[first:] if first > 0 else "")
    budget = max(8, limit - len(ext.encode("utf-8", "surrogatepass")))
    if len(stem.encode("utf-8", "surrogatepass")) > budget:
        stem = _truncate_utf8(stem, max(1, budget - 9)) + "_" + _cheap_hash8(stem)
    stem = re.sub(r"[. ]+$", "", stem) or "model"
    out = stem + ext
    if not is_safe_segment(out):
        out = stem if is_safe_segment(stem) else "model"
    if len(out.encode("utf-8", "surrogatepass")) > limit:
        out = _truncate_utf8(out, max(1, limit - 9)) + "_" + _cheap_hash8(out)
        out = re.sub(r"[. ]+$", "", out) or "model"
    return out
