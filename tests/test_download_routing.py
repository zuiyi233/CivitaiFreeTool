# -*- coding: utf-8 -*-
"""下载分流接入层回归网：webui._route_dest_dir / _enqueue_one 接线 / downloader._resolve_dest 优先级。

零真实网络：safetensors 头部嗅探靠 patch civitai_api.build_opener 注入假 Range 服务（fetch 的
start/end 是**闭区间**，与 HTTP Range / probe_header 一致）；落点全部在 tmp_path 下，
不碰用户配置、不碰真实模型库、不写仓库 error.log / download_tasks.json。api_key 一律合成假值。
风格照抄 tests/test_images_autofill.py 与 tests/test_download_selection.py。"""
import json
import os
import re
import struct
import sys
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import civitai_api
import config
import downloader
import model_routing
import webui

KEY = "synthetic-test-key"
IMG = "https://image.civitai.com/x/demo/original=true/a.png"


def tensor(name):
    return {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}


def header_blob(*keys):
    """按 safetensors 真格式造假文件头：前 8 字节 LE u64 = 头 JSON 长度 N，随后 N 字节 UTF-8 JSON。"""
    raw = json.dumps({k: tensor(k) for k in keys}, ensure_ascii=False).encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


# 全量 checkpoint：有扩散主干 + VAE 塔 + 文本编码器塔
FULL_FT = header_blob("model.diffusion_model.input_blocks.0.0.weight",
                      "first_stage_model.encoder.conv_in.weight",
                      "conditioner.embedders.0.transformer.text_model.encoder.layers.0.self_attn.q_proj.weight")
# Flux 系 DiT 单挑模型：只有主干，没有塔 —— 同一种 C 站类型，落点必须不同
DIT = header_blob("double_blocks.0.img_attn.qkv.weight", "single_blocks.3.linear.weight",
                  "final_layer.linear.weight")

CKPT_FILE = {"id": 1, "name": "demo.safetensors", "type": "Model", "sizeKB": 200000,
             "downloadUrl": "https://civitai.com/api/download/models/123?fileId=1",
             "hashes": {"SHA256": "1" * 64}}
LORA_FILE = {"id": 2, "name": "demo_lora.safetensors", "type": "LoRA", "sizeKB": 300000,
             "downloadUrl": "https://civitai.com/api/download/models/123?fileId=2",
             "hashes": {"SHA256": "2" * 64}}


class _Resp:
    """假 HTTP 响应：支持 .status / .getcode() / .headers.get() / .read(n) 与 with 语句。"""

    def __init__(self, status, ctype, data):
        self.status = status
        self.getcode = lambda: status
        self.headers = {"Content-Type": ctype, "Content-Length": str(len(data))}
        self._data = data
        self._pos = 0

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self._data) - self._pos
        chunk = self._data[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def range_opener(blob, status=206, ctype="application/octet-stream", seen=None):
    """假 R2：严格按闭区间 Range 切片。blob=None 模拟「一个字节都不给」（403 / 断流）。"""
    def _open(req, timeout=None):
        if seen is not None:
            seen.append(req)
        if blob is None:
            return _Resp(403, "text/plain; charset=utf-8", b"forbidden")
        m = re.match(r"bytes=(\d+)-(\d+)$", req.get_header("Range") or "")
        if not m or start_out_of_range(m, blob):
            return _Resp(200, ctype, blob if m else b"")
        start, end = int(m.group(1)), int(m.group(2))
        return _Resp(status, ctype, blob[start:end + 1])
    opener = Mock()
    opener.open = _open
    return opener


def start_out_of_range(m, blob):
    return int(m.group(1)) >= len(blob)


def comfy_root(tmp_path, *folders):
    """<tmp>/models 当 ComfyUI 的 models 根；只有列出来的类型目录才预先存在。

    resolve_dest_dir 要求「类型 key 目录已存在」，没建的那些就是「不分流」用例。"""
    root = Path(tmp_path) / "models"
    root.mkdir(parents=True, exist_ok=True)
    for f in folders:
        (root / f).mkdir(parents=True, exist_ok=True)
    return str(root)


def version_for(files, base_model="PonyXL"):
    return {"id": 123, "name": "v1", "baseModel": base_model, "files": copy_files(files)}


def copy_files(files):
    return json.loads(json.dumps(files))


def make_app(tmp_path, **over):
    """最小 Api 实例（照抄相邻用例的 __new__ 造法）：只装分流链路用到的属性。"""
    a = webui.Api.__new__(webui.Api)
    cfg = {**config.DEFAULTS,
           "api_key": KEY,
           "download_dir": str(tmp_path / "downloads"),
           "download_target_dir": str(tmp_path / "global"),
           "models_dir": str(tmp_path / "downloads"),
           "models_dirs": [str(tmp_path / "downloads")],
           "download_cover": False, "gen_metadata": False, "archive_comments": False,
           "translate_filename": False, "multi_file_download": "first",
           "comfyui_models_root": comfy_root(tmp_path)}
    cfg.update(over)
    a.cfg = cfg
    a.log_error = Mock()          # 绝不把测试噪声写进仓库 error.log
    a.api = civitai_api.CivitaiAPI()
    a.api.resolve_url = Mock(return_value=(456, 123))
    a.api.get_model = Mock(return_value={"id": 456, "name": "Demo Model", "type": "Checkpoint",
                                         "modelVersions": [{"id": 123}]})
    a.api.get_model_version = Mock(return_value=version_for([CKPT_FILE]))
    a.dl = Mock()
    a.dl.tasks = []
    return a


def enqueue(a, version=None, file_dict=None, **kw):
    """走真实 _enqueue_one（命名 / meta / 防重复分支都不跳过），返回 (item, task)。"""
    version = version or a.api.get_model_version.return_value
    version = dict(version, files=copy_files(version.get("files") or [CKPT_FILE]))
    target = file_dict or version["files"][0]
    item = a._enqueue_one("https://civitai.red/models/456?modelVersionId=123",
                          _version=version, _file=target, _model_id=456, **kw)
    calls = a.dl.add_task.call_args_list
    return item, (calls[-1].args[0] if calls else None)


def make_downloader(cfg_over=None):
    cfg = {**config.DEFAULTS, "download_dir": "", "download_target_dir": "", **(cfg_over or {})}
    return downloader.Downloader(cfg)


@pytest.fixture(autouse=True)
def _no_persistence(monkeypatch):
    """整个模块零落盘：不读写仓库的 download_tasks.json / download_history.json。"""
    monkeypatch.setattr(config, "load_history", lambda: [])
    monkeypatch.setattr(config, "save_tasks", lambda *a, **k: True)
    monkeypatch.setattr(config, "save_history", lambda *a, **k: True)
    monkeypatch.setattr(config, "load_tasks", lambda: [])


# ---------------------------------------------------------------- 配置面

def test_config_defaults_are_conservative():
    assert config.DEFAULTS["route_by_type"] is False
    assert config.DEFAULTS["comfyui_models_root"] == ""


def test_normalize_rejects_dirty_route_values():
    cfg = {"route_by_type": "true", "comfyui_models_root": "  D:\\ComfyUI\\models  "}
    config.normalize_ui_preferences(cfg)
    assert cfg["route_by_type"] is False            # 只有显式 true 才开：脏值一律当关
    assert cfg["comfyui_models_root"] == "D:\\ComfyUI\\models"


# ---------------------------------------------------------------- 关闭态＝回归护栏

def test_disabled_routing_keeps_pre_change_destination(tmp_path):
    (tmp_path / "global").mkdir(parents=True, exist_ok=True)
    a = make_app(tmp_path)
    dest, reason, source = a._route_dest_dir({"type": "Checkpoint", "name": "Demo Model"},
                                             a.api.get_model_version.return_value, CKPT_FILE, "demo")
    assert (dest, reason, source) == (None, "未开启分流", "none")
    item, task = enqueue(a)
    assert item["ok"] and task is not None
    assert "routed_dir" not in task.info
    assert task.info["route_reason"] == "未开启分流"
    assert task.dest_dir == ""                       # 入队时不落定，与改动前完全一致
    assert make_downloader(a.cfg)._resolve_dest(task) == str(tmp_path / "global")


def test_missing_or_bogus_models_root_counts_as_disabled(tmp_path):
    for bad in ("", "   ", str(tmp_path / "does-not-exist")):
        a = make_app(tmp_path, route_by_type=True, comfyui_models_root=bad)
        dest, reason, source = a._route_dest_dir({"type": "Checkpoint", "name": "n"},
                                                 version_for([CKPT_FILE]), CKPT_FILE, "demo")
        assert dest is None and source == "none"
        assert "未开启分流" in reason                 # 一眼看出是「没开」而不是「判定失败」


# ---------------------------------------------------------------- 类型目录缺失＝不分流

def test_absent_type_folder_is_not_created_and_explains_why(tmp_path):
    root = comfy_root(tmp_path)                      # 一个类型目录都没建
    a = make_app(tmp_path, route_by_type=True, comfyui_models_root=root)
    dest, reason, source = a._route_dest_dir({"type": "LORA", "name": "Demo Model"},
                                             version_for([LORA_FILE]), LORA_FILE, "demo")
    assert dest is None and source == "type"
    assert "不存在" in reason and "不新建类型目录" in reason
    assert not os.path.isdir(os.path.join(root, "loras"))      # 绝不 mkdir 类型目录（用户明确要求）


def test_same_name_file_blocking_type_dir_is_left_alone(tmp_path):
    root = comfy_root(tmp_path, "loras")
    blocker = Path(root) / "loras" / "demo"
    blocker.write_text("有人把模型名当文件名用了", encoding="utf-8")
    a = make_app(tmp_path, route_by_type=True, comfyui_models_root=root)
    dest, reason, _ = a._route_dest_dir({"type": "LORA", "name": "Demo Model"},
                                        version_for([LORA_FILE]), LORA_FILE, "demo")
    assert dest is None and "同名文件占用" in reason
    assert blocker.read_text(encoding="utf-8") == "有人把模型名当文件名用了"   # 原文件一个字节都没动


# ---------------------------------------------------------------- 正常分流

def test_lora_routes_into_nested_bundle_dir(tmp_path):
    root = comfy_root(tmp_path, "loras")
    (tmp_path / "global").mkdir(parents=True, exist_ok=True)
    a = make_app(tmp_path, route_by_type=True, comfyui_models_root=root)
    a.api.get_model = Mock(return_value={"id": 456, "name": "Demo Model", "type": "LORA"})
    a.api.get_model_version = Mock(return_value=version_for([LORA_FILE]))
    item, task = enqueue(a)
    assert item["ok"] and task is not None
    # 目录名 = 模型文件名主干（kurachi_mizuki\kurachi_mizuki.safetensors），不是「模型名_版本名」：
    # 后者会得到 "Kurachi Mizuki - style [Anima Base] (Commission)_Anima Base v1.0" 这种带括号长名。
    expect = os.path.join(root, "loras", os.path.splitext(task.filename)[0])
    assert task.info["routed_dir"] == expect
    assert task.info["route_source"] == "type"
    assert os.path.isdir(expect)                     # 只有分流成功才建模型名目录
    assert item["dest_dir"] == expect                # 下载列表能直接显示落点
    assert "loras" in item["route_reason"]
    assert make_downloader(a.cfg)._resolve_dest(task) == expect     # 压过 download_target_dir


def test_checkpoint_header_splits_checkpoints_and_diffusion_models(tmp_path):
    root = comfy_root(tmp_path, "checkpoints", "diffusion_models")
    a = make_app(tmp_path, route_by_type=True, comfyui_models_root=root)
    version = version_for([CKPT_FILE], base_model="SD 1.5")
    model_obj = {"type": "Checkpoint", "name": "Demo Model"}
    for blob, folder in ((FULL_FT, "checkpoints"), (DIT, "diffusion_models")):
        with patch.object(civitai_api, "build_opener", return_value=range_opener(blob)):
            dest, reason, source = a._route_dest_dir(model_obj, version, CKPT_FILE, "demo")
        assert dest == os.path.join(root, folder, "demo"), "%s -> %s" % (folder, reason)
        assert source == "header" and "头" in reason      # 原因要说清是「按文件头」判的
        assert os.path.isdir(dest)
    assert os.path.isdir(os.path.join(root, "checkpoints", "demo"))
    assert os.path.isdir(os.path.join(root, "diffusion_models", "demo"))


def test_probe_only_for_ambiguous_safetensors(tmp_path):
    root = comfy_root(tmp_path, "loras", "vae", "checkpoints")
    a = make_app(tmp_path, route_by_type=True, comfyui_models_root=root)
    seen = []
    opener = range_opener(FULL_FT, seen=seen)
    with patch.object(civitai_api, "build_opener", return_value=opener):
        # 类型已经确定的 LoRA / VAE 不该浪费两次 Range 请求
        assert a._route_dest_dir({"type": "LORA", "name": "n"}, version_for([LORA_FILE]),
                                 LORA_FILE, "demo")[0]
        vae = dict(LORA_FILE, name="v.safetensors", type="VAE")
        assert a._route_dest_dir({"type": "VAE", "name": "n"}, version_for([vae]), vae, "v")[0]
        # GGUF 不是 safetensors：只按类型映射，绝不读头
        gguf = dict(CKPT_FILE, id=9, name="demo-Q4_K_M.gguf",
                   downloadUrl="https://civitai.com/api/download/models/123?fileId=9")
        dest, _, source = a._route_dest_dir({"type": "Checkpoint", "name": "Demo Model"},
                                            version_for([gguf]), gguf, "demo")
        assert dest == os.path.join(root, "checkpoints", "demo") and source == "type"
    assert seen == []                                   # 一条请求都没发


def test_sniff_request_carries_bearer_only_with_key(tmp_path):
    root = comfy_root(tmp_path, "checkpoints")
    version = version_for([CKPT_FILE])
    seen = []
    a = make_app(tmp_path, route_by_type=True, comfyui_models_root=root)
    with patch.object(civitai_api, "build_opener", return_value=range_opener(FULL_FT, seen=seen)):
        assert a._route_dest_dir({"type": "Checkpoint", "name": "Demo Model"}, version,
                                 CKPT_FILE, "demo")[0]
    assert seen and all(r.get_header("Authorization") == "Bearer " + KEY for r in seen)
    assert [r.get_header("Range") for r in seen] == ["bytes=0-7", "bytes=8-%d" % (len(FULL_FT) - 1)]
    # 无 Key（受限资源之外的情形）不能发空 Bearer
    seen2 = []
    b = make_app(tmp_path, route_by_type=True, comfyui_models_root=root, api_key="")
    with patch.object(civitai_api, "build_opener", return_value=range_opener(FULL_FT, seen=seen2)):
        b._route_dest_dir({"type": "Checkpoint", "name": "Demo Model"}, version, CKPT_FILE, "demo")
    assert seen2 and all(r.get_header("Authorization") is None for r in seen2)


# ---------------------------------------------------------------- 嗅探降级

SNIFF_FAILURES = {
    "拿不到字节(403)": range_opener(None),
    "307 跳登录页回 HTML": range_opener(b"<html>login</html>" * 200, status=200,
                                     ctype="text/html; charset=utf-8"),
    "Range 被忽略回 200": range_opener(FULL_FT, status=200),
    "长度字段超上限": range_opener(struct.pack("<Q", model_routing.MAX_HEADER_SIZE + 1) + b"{"),
    "半截头（正文被截断）": range_opener(FULL_FT[:40]),
    "content-type 是 json": range_opener(FULL_FT, ctype="application/json"),
    "opener 直接抛错": Mock(open=Mock(side_effect=OSError("连接被掐断"))),
}


@pytest.mark.parametrize("label", sorted(SNIFF_FAILURES))
def test_sniff_failures_never_break_and_still_route_by_type(tmp_path, label):
    root = comfy_root(tmp_path, "checkpoints")
    a = make_app(tmp_path, route_by_type=True, comfyui_models_root=root)
    version = version_for([CKPT_FILE])
    with patch.object(civitai_api, "build_opener", return_value=SNIFF_FAILURES[label]):
        dest, reason, source = a._route_dest_dir({"type": "Checkpoint", "name": "Demo Model"},
                                                 version, CKPT_FILE, "demo")
    assert dest == os.path.join(root, "checkpoints", "demo"), "%s -> %s" % (label, reason)
    assert source == "type", label               # 降级为「只按类型映射」，不是报错
    assert "按 C 站模型类型 Checkpoint 落 checkpoints" in reason, label


def test_download_url_unavailable_degrades_to_type_only(tmp_path):
    """file_download_url 自己抛错（受限/无链接）也不能连累分流判定。"""
    root = comfy_root(tmp_path, "checkpoints")
    a = make_app(tmp_path, route_by_type=True, comfyui_models_root=root)
    with patch.object(a.api, "file_download_url",
                      side_effect=civitai_api.CivitaiError("无下载链接")):
        dest, reason, source = a._route_dest_dir({"type": "Checkpoint", "name": "Demo Model"},
                                                 version_for([CKPT_FILE]), CKPT_FILE, "demo")
    assert dest == os.path.join(root, "checkpoints", "demo") and source == "type"
    assert "checkpoints" in reason


def test_probe_budget_exhausted_degrades_without_network(tmp_path):
    """总时限（_SNIFF_BUDGET_S）用完就放弃嗅探：同步入队链路（归档桥）不能被网络拖住。"""
    root = comfy_root(tmp_path, "checkpoints")
    a = make_app(tmp_path, route_by_type=True, comfyui_models_root=root)
    a._SNIFF_BUDGET_S = -1                      # 预算已经用完
    seen = []
    with patch.object(civitai_api, "build_opener", return_value=range_opener(FULL_FT, seen=seen)):
        dest, reason, source = a._route_dest_dir({"type": "Checkpoint", "name": "Demo Model"},
                                                 version_for([CKPT_FILE]), CKPT_FILE, "demo")
    assert dest == os.path.join(root, "checkpoints", "demo") and source == "type"
    assert seen == []


def test_download_still_enqueues_when_sniff_explodes(tmp_path):
    """嗅探炸了也必须正常入队：分流只是锦上添花。"""
    root = comfy_root(tmp_path, "checkpoints")
    (tmp_path / "global").mkdir(parents=True, exist_ok=True)
    a = make_app(tmp_path, route_by_type=True, comfyui_models_root=root)
    with patch.object(civitai_api, "build_opener",
                      return_value=Mock(open=Mock(side_effect=RuntimeError("代理炸了")))):
        item, task = enqueue(a)
    assert item["ok"] is True and task is not None
    assert item["route_source"] == "type"
    assert make_downloader(a.cfg)._resolve_dest(task) == task.info["routed_dir"]


# ---------------------------------------------------------------- 不分流的类型

def test_unclassified_type_keeps_current_behaviour(tmp_path):
    root = comfy_root(tmp_path, "checkpoints")
    a = make_app(tmp_path, route_by_type=True, comfyui_models_root=root)
    flow = {"id": 7, "name": "flow.json", "type": "Workflow",
            "downloadUrl": "https://civitai.com/api/download/models/123?fileId=7", "hashes": {}}
    dest, reason, source = a._route_dest_dir({"type": "Workflows", "name": "Demo Model"},
                                             version_for([flow]), flow, "flow")
    assert dest is None and source == "none"
    assert "不分流" in reason and "保持现有落盘" in reason   # 文案给人看，不暴露内部配置键名
    assert os.listdir(os.path.join(root, "checkpoints")) == []   # 一个目录都没多


def test_motion_module_header_is_never_routed(tmp_path):
    root = comfy_root(tmp_path, "checkpoints")
    a = make_app(tmp_path, route_by_type=True, comfyui_models_root=root)
    blob = header_blob("model.diffusion_model.input_blocks.0.0.weight",
                       "motion_modules.04.zero_conv.weight")
    with patch.object(civitai_api, "build_opener", return_value=range_opener(blob)):
        dest, reason, source = a._route_dest_dir({"type": "MotionModule", "name": "Demo Model"},
                                                 version_for([CKPT_FILE]), CKPT_FILE, "demo")
    assert dest is None and source == "none" and "不分流" in reason


# ---------------------------------------------------------------- 优先级与回落

def test_dest_explicit_beats_routed_dir(tmp_path):
    routed = tmp_path / "models" / "loras" / "Demo"
    routed.mkdir(parents=True)
    explicit = str(tmp_path / "更新下载目标")
    task = downloader.DownloadTask("https://civitai.invalid/f", explicit, "demo.safetensors",
                                   info={"dest_explicit": True, "routed_dir": str(routed)})
    d = make_downloader({"download_target_dir": str(tmp_path / "global")})
    assert d._resolve_dest(task) == explicit          # 用户显式指定永远最优先
    task.info.pop("dest_explicit")
    assert d._resolve_dest(task) == str(routed)       # 去掉显式标记才轮到分流


def test_routed_dir_wins_over_global_target_and_task_dest(tmp_path):
    routed = tmp_path / "models" / "vae" / "Demo"
    routed.mkdir(parents=True)
    task = downloader.DownloadTask("https://civitai.invalid/f", str(tmp_path / "任务自带"),
                                   "demo.safetensors", info={"routed_dir": str(routed)})
    d = make_downloader({"download_target_dir": str(tmp_path / "global"),
                         "download_dir": str(tmp_path / "default")})
    assert d._resolve_dest(task) == str(routed)


def test_deleted_routed_dir_falls_back_instead_of_raising(tmp_path):
    routed = tmp_path / "models" / "loras" / "Demo"
    routed.mkdir(parents=True)
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    d = make_downloader({"download_target_dir": str(global_dir),
                         "download_dir": str(global_dir)})
    task = downloader.DownloadTask("https://civitai.invalid/f", "", "demo.safetensors",
                                   info={"routed_dir": str(routed)})
    assert d._resolve_dest(task) == str(routed)
    routed.rmdir()                                    # 用户手删 / 换盘
    assert d._resolve_dest(task) == str(global_dir)    # 回落现有落点，不报错也不重建
    assert not routed.exists()


def test_enqueue_with_explicit_dest_skips_routing(tmp_path):
    root = comfy_root(tmp_path, "checkpoints")
    explicit = tmp_path / "old_version_dir"
    a = make_app(tmp_path, route_by_type=True, comfyui_models_root=root)
    item, task = enqueue(a, dest_dir=str(explicit), skip_if_exists=True)
    assert item["ok"], item
    assert "routed_dir" not in task.info               # 更新下载不被分流搬走
    assert "指定落点优先" in task.info["route_reason"]
    assert make_downloader(a.cfg)._resolve_dest(task) == str(explicit)
    assert not os.path.isdir(os.path.join(root, "checkpoints", "demo"))


def test_hf_relative_subdir_semantics_unchanged(tmp_path):
    """hf_rel 分支不能被 routed_dir 抢走语义（分流任务永远不带 hf_rel，这里钉住原行为）。"""
    base = tmp_path / "global"
    base.mkdir()
    task = downloader.DownloadTask("https://huggingface.co/x", "", "m.bin", info={"hf_rel": "a/b"})
    d = make_downloader({"download_target_dir": str(base)})
    assert d._resolve_dest(task) == os.path.join(str(base), "a", "b")


def test_routing_priority_survives_task_round_trip(tmp_path):
    """持久化往返（dest_dir 已被写成落点）后落点仍稳定：to_dict/from_dict 不丢 routed_dir。"""
    routed = tmp_path / "models" / "loras" / "Demo"
    routed.mkdir(parents=True)
    task = downloader.DownloadTask("https://civitai.invalid/f", str(routed), "demo.safetensors",
                                   info={"routed_dir": str(routed), "route_reason": "按类型落 loras"})
    restored = downloader.DownloadTask.from_dict(task.to_dict())
    assert restored.info["routed_dir"] == str(routed)
    assert restored.info["route_reason"] == "按类型落 loras"
    assert make_downloader({"download_target_dir": str(tmp_path / "g")})._resolve_dest(restored) == str(routed)


# ---------------------------------------------------------------- 防重入

def test_two_tasks_routed_to_same_bundle_do_not_write_concurrently(tmp_path):
    routed = tmp_path / "models" / "loras" / "demo"
    routed.mkdir(parents=True)
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    d = downloader.Downloader({**config.DEFAULTS, "max_concurrent_downloads": 2,
                               "download_target_dir": str(global_dir)})
    mk = lambda: downloader.DownloadTask("https://civitai.invalid/f", "", "demo.safetensors",
                                         info={"routed_dir": str(routed)})
    a, b = mk(), mk()
    expect = os.path.normcase(os.path.abspath(os.path.join(str(routed), "demo.safetensors")))
    assert d._resolve_dest(a) == d._resolve_dest(b) == str(routed)   # 同一落点 + 同一文件名
    started, release = threading.Event(), threading.Event()
    finished = []

    def fake_impl(task):
        # 真实 _download_impl 的第一步同样是取 _claimed_dest；这里同口径读 dest_dir，
        # 只模拟「正在往这个路径写」，然后把状态收尾，交给 finally 释放占位。
        started.set()
        release.wait(5)
        finished.append((task.id, task.dest_dir))
        task.status = downloader.ST_DONE

    d._download_impl = fake_impl
    d.add_task(a)
    d.add_task(b)
    try:
        assert started.wait(5), "worker 未认领任务"
        time.sleep(0.3)
        assert a.id in d._active_ids
        # ★ 同路径防重入：第二个任务必须还在排队，不能同时往同一目录写同一个文件
        assert b.id not in d._active_ids
        assert b.status == downloader.ST_PENDING
        assert list(d._active_paths.values()) == [expect]
        release.set()
        for _ in range(150):
            if len(finished) == 2:
                break
            time.sleep(0.05)
    finally:
        d._stop = True
        release.set()
    assert len(finished) == 2, finished                # 前一个让位后第二个才继续
    assert {p for _, p in finished} == {str(routed)}   # 两者落点同源（都来自 routed_dir）


def test_routed_tasks_with_different_names_share_bundle_dir(tmp_path):
    """同一流向目录里不同文件名不互相阻塞（防止把防重入写成「同目录串行」）。"""
    bundle = tmp_path / "models" / "loras" / "Demo"
    bundle.mkdir(parents=True)
    d = downloader.Downloader({**config.DEFAULTS, "max_concurrent_downloads": 2,
                               "download_target_dir": str(tmp_path / "g")})
    (tmp_path / "g").mkdir()
    mk = lambda name: downloader.DownloadTask("https://civitai.invalid/f", "", name,
                                             info={"routed_dir": str(bundle)})
    a, b = mk("one.safetensors"), mk("two.safetensors")
    done, release = [], threading.Event()
    d._download_impl = lambda t: (done.append(t.filename), release.wait(5),
                                  setattr(t, "status", downloader.ST_DONE))
    d.add_task(a)
    d.add_task(b)
    try:
        for _ in range(100):
            if len(done) == 2:
                break
            time.sleep(0.05)
        assert len(done) == 2, done                    # 两个不同文件可以同时开跑
    finally:
        d._stop = True
        release.set()


# ---------------------------------------------------------------- 路径安全

HOSTILE = ["../../evil", "..", ".", "C:\\Windows", "\\\\server\\share", "/etc", "C:/x",
           "NUL", "con.txt", "  ", "a" * 400, "emoji🙂/../../x", "laser<|>?*", "\u202e..\\win"]


@pytest.mark.parametrize("name", HOSTILE)
def test_hostile_model_names_cannot_escape_models_root(tmp_path, name):
    root = comfy_root(tmp_path, "loras")
    a = make_app(tmp_path, route_by_type=True, comfyui_models_root=root)
    dest, reason, _ = a._route_dest_dir({"type": "LORA", "name": name},
                                        version_for([LORA_FILE]), LORA_FILE, "demo")
    ar = os.path.abspath(root)
    if dest is None:
        # 允许的不分流原因只有这三种（都保持现有落盘）；超短名/路径过长在 Windows 上会创建失败
        assert any(k in reason for k in ("不存在", "同名文件占用", "创建失败")), (name, reason)
        assert not (Path(root) / "loras" / "evil").exists()
        return
    seg = os.path.basename(dest)
    assert os.path.dirname(os.path.abspath(dest)) == os.path.join(ar, "loras"), (name, dest)
    assert os.path.commonpath([ar, os.path.abspath(dest)]) == ar, (name, dest)
    assert os.sep not in seg and "/" not in seg and "\x00" not in seg, (name, dest)
    assert len(seg.encode("utf-8")) <= 140 + 9 + 8, (name, dest)
    assert model_routing.is_safe_segment(seg), (name, seg)


def test_resolve_dest_dir_rejects_raw_traversal_bundles(tmp_path):
    root = comfy_root(tmp_path, "loras")
    for bad in ("..", "../evil", "a\\b", "a/b", "C:x", "\\\\server\\share", "."):
        assert model_routing.resolve_dest_dir(root, "loras", bad) is None, bad
    assert sorted(p.name for p in (Path(root) / "loras").iterdir()) == []


# ---------------------------------------------------------------- 附属文件跟着进嵌套目录

def test_sidecars_and_preview_follow_the_routed_dir(tmp_path):
    root = comfy_root(tmp_path, "checkpoints")
    a = make_app(tmp_path, route_by_type=True, comfyui_models_root=root,
                 gen_metadata=True, metadata_format="both", download_cover=True,
                 archive_comments=True, models_dir=root, models_dirs=[root],
                 download_target_dir=root)
    bundle = os.path.join(root, "checkpoints", "demo")
    os.makedirs(bundle)
    model = os.path.join(bundle, "demo.safetensors")
    Path(model).write_bytes(b"fake-weights")
    task = downloader.DownloadTask("https://civitai.invalid/f", bundle, "demo.safetensors",
                                   info={"model_id": "456", "version_id": "123",
                                         "modelName": "Demo Model", "versionName": "v1",
                                         "routed_dir": bundle,
                                         "meta": {"info": {"name": "Demo Model",
                                                           "images": [{"url": IMG}]},
                                                  "sd": {"prompt": "demo"}}})
    client = Mock()
    client.fetch_comments = Mock(return_value={"status": "ok", "pages": 1,
                                               "items": [{"user": "阿强", "content": "好用"}]})
    client.fetch_reviews = Mock(return_value={"status": "ok", "items": []})

    def fake_image(url, dest, **kw):
        Path(dest).write_bytes(b"png")
        return True

    a._new_api = Mock(return_value=client)
    a._cache_history_task = Mock()
    a.move_file_to = Mock()
    a.dl.tasks = [task]
    with patch.object(webui, "_download_image", side_effect=fake_image):
        a._handle_dl_done(task)
    base = os.path.join(bundle, "demo")
    for suffix in (".civitai.info", ".json", ".preview.png", "_评论.txt"):
        assert os.path.isfile(base + suffix), suffix       # 本体与全部附属文件在同一嵌套目录
    assert [str(p) for p in Path(root).rglob("*.civitai.info")] == [base + ".civitai.info"]
    assert [str(p) for p in Path(root).rglob("*_评论.txt")] == [base + "_评论.txt"]
    # <名>.images/ 必须与模型同层（同 tests/test_images_autofill.py 的同层校验口径）
    img_base = os.path.splitext(model)[0]
    assert webui._fill_dir_writable(img_base + ".images", model) is True
    targets = webui._images_fill_targets(img_base, [{"url": IMG}])
    assert targets and all(os.path.dirname(dest) == img_base + ".images" for _, _, dest in targets)
    assert os.path.dirname(img_base) == bundle


def test_images_dir_is_created_inside_the_bundle(tmp_path):
    """补帧的 <名>.images/ 必须落在嵌套目录里与模型同层（真实 _start_images_autofill 链路）。"""
    root = comfy_root(tmp_path, "checkpoints")
    bundle = os.path.join(root, "checkpoints", "demo")
    os.makedirs(bundle)
    model = os.path.join(bundle, "demo.safetensors")
    Path(model).write_bytes(b"fake-weights")
    (Path(bundle) / "demo.civitai.info").write_text(json.dumps({"images": [{"url": IMG}]}),
                                                    encoding="utf-8")
    a = make_app(tmp_path, route_by_type=True, comfyui_models_root=root)
    written = []

    def fake_run(path, pending, cfg, state=None, cancel=None, deadline_s=None):
        written.extend(dest for _, _, dest in pending)
        return {"msg": "", "downloaded": len(pending)}

    with patch.object(webui, "_images_fill_run", side_effect=fake_run):
        r = a._start_images_autofill(model, "test")
        assert r.get("started") is True, r
        assert r["dir"] == os.path.join(bundle, "demo.images")
        for _ in range(100):
            if written:
                break
            time.sleep(0.05)
    assert written, written
    assert all(w.startswith(os.path.join(bundle, "demo.images")) for w in written), written
    assert not list(Path(root).glob("demo.images"))         # 绝不在 models 根或类型目录旁边


# ---------------------------------------------------------------- 可见性（roots / _target_allowed）

def test_models_roots_gains_comfy_root_only_when_routing(tmp_path):
    root = comfy_root(tmp_path, "loras")
    off = make_app(tmp_path)                          # route_by_type 默认 False
    assert root not in off._models_roots()
    assert off._target_allowed(os.path.join(root, "loras", "Demo")) is False
    on = make_app(tmp_path, route_by_type=True, comfyui_models_root=root)
    assert root in on._models_roots()
    assert on._target_allowed(os.path.join(root, "loras", "Demo")) is True
    assert len(on._models_roots()) == len(set(on._models_roots()))          # 不重复并入
    assert on._root_of(os.path.join(root, "loras", "Demo", "m.safetensors")) == root
    gone = make_app(tmp_path, route_by_type=True, comfyui_models_root=str(tmp_path / "不存在"))
    assert str(tmp_path / "不存在") not in gone._models_roots()


def test_history_row_carries_route_fields(tmp_path):
    routed = tmp_path / "models" / "loras" / "Demo"
    routed.mkdir(parents=True)
    d = make_downloader()
    task = downloader.DownloadTask("https://civitai.invalid/f", str(routed), "demo.safetensors",
                                   info={"routed_dir": str(routed), "route_reason": "按类型落 loras"})
    task.dest_dir = str(routed)
    task.status = downloader.ST_DONE
    d._record_history(task)
    row = d.get_history()[0]
    assert row["route_reason"] == "按类型落 loras"
    assert row["routed_dir"] == str(routed)
    assert row["dest_dir"] == str(routed)              # 列表/历史落点字段齐全
    assert row["file_path"] == os.path.join(str(routed), "demo.safetensors")


# ---------------------------------------------------------------- 入口一致性（图片页批量 / parse_urls）

def _image_result(app, resources, url="https://civitai.red/images/1"):
    app.parse_image_models = Mock(return_value=json.dumps({"ok": True, "resources": resources}))
    return json.loads(app.download_image_models(url))


def test_batch_dedup_index_follows_routing(tmp_path):
    """防重复索引必须跟着分流走：分流后本体落在 <models_root>\\<类型>\\<名>\\ 里，
    只 walk download_dir 永远看不见它们 → 每点一次「批量下载全部模型」就重复下一遍。"""
    root = comfy_root(tmp_path, "loras")
    bundle = Path(root) / "loras" / "demo_lora"
    (bundle / "demo_lora.images").mkdir(parents=True)   # 图片缓存目录不算模型
    (bundle / "demo_lora.safetensors").write_bytes(b"x")
    (bundle / "demo_lora.images" / "shot.png").write_bytes(b"x")
    off = make_app(tmp_path, comfyui_models_root=root)   # route_by_type 默认 False
    assert "demo_lora.safetensors" not in off._batch_existing_model_names()
    on = make_app(tmp_path, route_by_type=True, comfyui_models_root=root)
    assert "demo_lora.safetensors" in on._batch_existing_model_names()
    assert "shot.png" not in on._batch_existing_model_names()   # .images 里的图不当成模型


def test_batch_download_routes_then_skips_when_already_routed(tmp_path):
    """一次改两件事：① 这条链路要按类型分流 ② 分流后的文件要能被下一次认出来并跳过。
    只做①不做②就会「分流越正常、重复下载越多」。"""
    root = comfy_root(tmp_path, "loras")
    a = make_app(tmp_path, route_by_type=True, comfyui_models_root=root)
    a.api.get_model = Mock(return_value={"id": 456, "name": "Demo LoRA", "type": "LORA",
                                         "modelVersions": [{"id": 123}]})
    a.api.get_model_version = Mock(return_value=version_for([LORA_FILE]))
    res = [{"modelId": 456, "versionId": 123, "name": "Demo LoRA", "type": "LORA"}]
    first = _image_result(a, res)
    assert first["added"] == 1 and first["skipped"] == 0
    task = a.dl.add_task.call_args_list[-1].args[0]
    expect = os.path.join(root, "loras", os.path.splitext(task.filename)[0])
    assert task.info["routed_dir"] == expect
    assert first["detail"][0]["dest_dir"] == expect          # 弹窗能显示落点
    assert "loras" in first["detail"][0]["route_reason"]
    Path(expect).mkdir(parents=True, exist_ok=True)
    (Path(expect) / task.filename).write_bytes(b"x")
    second = _image_result(a, res)
    assert second["added"] == 0 and second["skipped"] == 1   # 已经分流到位的文件不再重复下载


def test_parse_urls_records_routed_dir(tmp_path):
    """前端「解析链接」走的是 parse_urls 而不是 _enqueue_one，两条入口都要接上分流。"""
    root = comfy_root(tmp_path, "loras")
    a = make_app(tmp_path, route_by_type=True, comfyui_models_root=root)
    a.api.get_model = Mock(return_value={"id": 456, "name": "Demo LoRA", "type": "LORA",
                                         "modelVersions": [{"id": 123}]})
    a.api.get_model_version = Mock(return_value=version_for([LORA_FILE]))
    a.parse_urls(["https://civitai.red/models/456?modelVersionId=123"])
    for _ in range(300):
        if getattr(a, "_parse_state", {}).get("finished"):
            break
        time.sleep(0.01)
    task = a.dl.add_task.call_args_list[-1].args[0]
    assert task.info["routed_dir"] == os.path.join(root, "loras", os.path.splitext(task.filename)[0])
