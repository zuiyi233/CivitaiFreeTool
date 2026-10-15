# -*- coding: utf-8 -*-
"""分流决策层单测：合成 safetensors 头字节 + 临时空目录；无网络、无真实模型、不写仓库。"""
import json
import os
import struct
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_routing as routing

# Civitai ModelType 权威 23 值（精确大小写：Controlnet 小写 n、Workflows 复数）
CIVITAI_TYPES = [
    "Checkpoint", "TextualInversion", "Hypernetwork", "AestheticGradient", "LORA", "LoCon", "DoRA",
    "Controlnet", "Upscaler", "MotionModule", "VAE", "TextEncoder", "UNet", "CLIPVision", "Poses",
    "Wildcards", "Workflows", "ComfyWorkflows", "Detection", "VisionLanguage", "CLIP", "LLM", "Other",
]
# 权威表里没有、但历史数据混存的旧拼写
LEGACY_TYPES = [
    ("ControlNet", "controlnet"), ("LoRA", "loras"), ("LYCORIS", "loras"), ("LOHA", "loras"),
    ("LOKR", "loras"), ("Model", "checkpoints"), ("Textual Inversion", "embeddings"),
    ("Diffusion Model", "diffusion_models"),
]


def tensor(name):
    return {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}


def header_bytes(*keys, **meta):
    """按 safetensors 真格式造头：前 8 字节 LE u64 = JSON 长度，随后是 JSON 本体。"""
    doc = {k: tensor(k) for k in keys}
    if meta:
        doc["__metadata__"] = meta
    raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


class FakeFetch:
    """注入式 Range 读取：fetch(start, end) 的 end 是闭区间，和 HTTP Range / downloader 一致。"""

    def __init__(self, blob, fail_after=None):
        self.blob = blob
        self.calls = []
        self.fail_after = fail_after  # 第 N 次之后返回 None，模拟断流/403

    def __call__(self, start, end):
        self.calls.append((start, end))
        if self.fail_after is not None and len(self.calls) > self.fail_after:
            return None
        if start < 0 or end < start or end >= len(self.blob):
            return None
        return self.blob[start:end + 1]


class TypeTableTests(unittest.TestCase):
    def test_all_23_authoritative_types_have_a_decision(self):
        self.assertEqual(len(CIVITAI_TYPES), 23)
        self.assertEqual(len(set(CIVITAI_TYPES)), 23)
        for raw in CIVITAI_TYPES:
            with self.subTest(model_type=raw):
                mapped = routing.MODEL_TYPE_TO_FOLDER.get(raw)
                if mapped is None:
                    self.assertIn(raw, routing.NON_MODEL_TYPES, "23 值必须要么有落点要么显式不分流")
                    self.assertIsNone(routing.classify(raw).folder)
                else:
                    self.assertIn(mapped, routing.CORE_FOLDERS, "落点只允许核心 key")
                    d = routing.classify(raw)
                    self.assertEqual((d.folder, d.source), (mapped, "type"))

    def test_expected_routing_per_type(self):
        want = {
            "Checkpoint": "checkpoints", "TextualInversion": "embeddings", "Hypernetwork": "hypernetworks",
            "LORA": "loras", "LoCon": "loras", "DoRA": "loras", "Controlnet": "controlnet",
            "Upscaler": "upscale_models", "VAE": "vae", "TextEncoder": "text_encoders",
            "UNet": "diffusion_models", "CLIPVision": "clip_vision", "CLIP": "text_encoders",
        }
        self.assertEqual({k: v for k, v in routing.MODEL_TYPE_TO_FOLDER.items() if k in want}, want)
        for raw in ["AestheticGradient", "MotionModule", "Poses", "Wildcards", "Workflows",
                    "ComfyWorkflows", "Detection", "VisionLanguage", "LLM", "Other"]:
            with self.subTest(model_type=raw):
                self.assertIn(raw, routing.NON_MODEL_TYPES)
                self.assertIsNone(routing.classify(raw).folder)
                self.assertEqual(routing.classify(raw).source, "none")

    def test_legacy_spellings_alias_into_the_same_folder(self):
        for raw, folder in LEGACY_TYPES:
            with self.subTest(model_type=raw):
                self.assertEqual(routing.MODEL_TYPE_TO_FOLDER[raw], folder)
                d = routing.classify(raw)
                self.assertEqual((d.folder, d.source), (folder, "type"))

    def test_normalize_type_folds_case_space_hyphen_underscore(self):
        for raw in ["ControlNet", "controlnet", " CONTROLNET ", "Control-Net", "control_net"]:
            self.assertEqual(routing.normalize_type(raw), "controlnet")
        self.assertEqual(routing.normalize_type("Textual Inversion"), routing.normalize_type("TextualInversion"))
        self.assertEqual(routing.normalize_type(None), "")
        self.assertEqual(routing.classify("control net").folder, "controlnet")
        self.assertEqual(routing.classify("LYCORIS").folder, "loras")

    def test_weight_and_file_type_constants_match_enums(self):
        self.assertEqual(routing.WEIGHT_FILE_TYPES, {"Model", "Pruned Model", "Diffusion Model", "UNet"})
        self.assertTrue(set(routing.MODEL_TYPE_TO_FOLDER.values()) <= routing.CORE_FOLDERS)
        # 非权重附件类型必须与权重类型互斥
        self.assertFalse({k for k in routing.NON_WEIGHT_FILE_TYPES if k != "Other"}
                         & (routing.WEIGHT_FILE_TYPES | set(routing.FILE_TYPE_TO_FOLDER)))


class HeaderClassifyTests(unittest.TestCase):
    def assert_keys(self, keys, expected):
        self.assertEqual(routing.classify_safetensors_keys(keys), expected)

    def test_rule1_text_encoder_beats_everything(self):
        self.assert_keys(["text_encoders.weight"], "text_encoders")
        self.assert_keys(["token_embd.weight"], "text_encoders")
        self.assert_keys(["transformer.text_model.embeddings.weight"], "text_encoders")
        self.assert_keys(["tokenizer.bos_token_id"], "text_encoders")
        # 没有主干权重时，rule1 优先于 LoRA / DiT；且文本编码器内部的 blocks. 不会反过来污染判定
        self.assert_keys(["token_embd.lora_unet.weight"], "text_encoders")
        self.assert_keys(["transformer.text_model.encoder.blocks.0.weight"], "text_encoders")
        self.assert_keys(["tokenizer.bos_token_id", "text_encoders.clip_l.encoder.blocks.1.weight"], "text_encoders")

    def test_rule1_text_encoder_section_of_a_checkpoint_does_not_hijack_it(self):
        # Flux / SD3 的完整 checkpoint 顶层就有 text_encoders.clip_l.* 与 text_encoders.t5xx.*，
        # 判定必须看「有没有主干权重」，否则每个 checkpoint 都会被投进 models/text_encoders
        flux = ["model.diffusion_model.double_blocks.0.attn.to_q.weight",
                "text_encoders.clip_l.transformer.text_model.encoder.blocks.0.weight",
                "text_encoders.t5xx.decoder.block.0.layer.0.SelfAttention.q.weight",
                "first_stage_model.decoder.conv_in.weight"]
        self.assertEqual(routing.classify_safetensors_keys(flux), "checkpoints")
        self.assertEqual(routing.classify_safetensors_keys(flux[:3]), "diffusion_models")
        # 纯文本编码器（CLIPLoader 的那类文件）仍然走 rule1
        self.assertEqual(routing.classify_safetensors_keys(["text_encoders.t5xx.decoder.block.0.weight"]),
                         "text_encoders")

    def test_rule2_motion_module_is_not_routed(self):
        self.assert_keys(["motion_modules.0.blocks.weight"], None)
        self.assert_keys(["unet.zero_g_conv.weight"], None)

    def test_rule3_lora_shapes(self):
        for keys in [["lora_unet_down_blocks_0_weight"], ["lora_te_text_model.weight"],
                     ["base_model.model.transformer.blocks.0.qkv.lora_A.weight"],
                     ["network.loras.0.weight"], ["single_blocks.0.attn.qkv.lora_B.weight"]]:
            with self.subTest(keys=keys):
                self.assert_keys(keys, "loras")

    def test_rule4_dit_backbone_is_diffusion_models(self):
        for keys in [["double_blocks.0.attn.qkv.weight"], ["single_blocks.0.linear.weight"],
                     ["txtmerge.2.mlp.weight"], ["imgmerge.0.proj.weight"], ["blocks.3.norm.weight"]]:
            with self.subTest(keys=keys):
                self.assert_keys(keys, "diffusion_models")

    def test_rule5_full_checkpoint_versus_picked_out_diffusion(self):
        diffusion = "model.diffusion_model.input_blocks.0.ops.weight"
        self.assert_keys([diffusion, "first_stage_model.encoder.conv_in.weight"], "checkpoints")
        self.assert_keys([diffusion, "conditioner.embedders.0.transformer.text_model.embeddings.weight"], "checkpoints")
        self.assert_keys([diffusion], "diffusion_models")

    def test_rule4_does_not_steal_checkpoint_block_names(self):
        # SD1.5 checkpoint 的 input_blocks./output_blocks. 里就含 "blocks."，判定必须按顶层前缀
        keys = ["model.diffusion_model.input_blocks.4.0.in_layers.0.weight",
                "model.diffusion_model.output_blocks.5.0.conv.weight",
                "first_stage_model.decoder.conv_in.weight", "cond_stage_model.transformer.text_model.final_layer_norm.weight"]
        self.assertEqual(routing.classify_safetensors_keys(keys), "checkpoints")

    def test_rule6_standalone_vae(self):
        self.assert_keys(["encoder.down.0.block.0.conv1.weight", "decoder.up.0.conv.weight"], "vae")
        self.assert_keys(["first_stage_model.encoder.conv_in.weight"], "vae")
        # 有扩散权重时轮不到 VAE 规则（已在 rule5 返回）
        self.assert_keys(["encoder.x.weight", "model.diffusion_model.x.weight"], "diffusion_models")

    def test_rule7_ignored_keys_and_unrecognisable(self):
        self.assert_keys(["__metadata__", "layer.min", "layer.max", "layer.scale_weight"], None)
        self.assert_keys([], None)
        self.assert_keys(["whatever.onnx.weight"], None)
        # __metadata__ 里塞了 lora 字样也算不出结构 → None，交给类型映射
        self.assertEqual(routing.classify_safetensors_keys({"__metadata__": {"ssmd_model": "lora_unet_x"}}), None)

    def test_rule_order_is_strict(self):
        # LoRA 头同时含 DiT 与扩散前缀：rule3 必须先于 rule4/rule5，否则全量 FT 会被认成 LoRA 的反面
        self.assert_keys(["lora_unet_x", "model.diffusion_model.y", "double_blocks.0.y"], "loras")
        # 文本编码器优先于 LoRA
        self.assert_keys(["lora_te_x", "token_embd.weight"], "text_encoders")

    def test_header_keys_accepts_parsed_dict(self):
        blob = header_bytes("lora_unet_a", "lora_te_b", ssmd_model_type="LORA")
        doc = routing.parse_safetensors_header(blob)
        self.assertIn("__metadata__", doc)
        self.assertEqual(routing.classify_safetensors_keys(routing.header_keys(doc)), "loras")

    def test_every_rule_also_holds_through_real_header_bytes(self):
        """每条规则都从真字节（8 字节 LE 长度 + JSON）走一遍 parse → classify，而不是只喂键名。"""
        samples = [
            (["token_embd.weight"], "text_encoders"),
            (["motion_modules.0.blocks.weight", "model.diffusion_model.x"], None),
            (["lora_unet_down_blocks_0_q.weight"], "loras"),
            (["double_blocks.0.attn.qkv.weight"], "diffusion_models"),
            (["model.diffusion_model.x", "first_stage_model.encoder.y"], "checkpoints"),
            (["model.diffusion_model.x"], "diffusion_models"),
            (["encoder.down.0.block.0.conv1.weight", "decoder.up.0.conv.weight"], "vae"),
            (["nothing.recognisable.weight"], None),
        ]
        for keys, expected in samples:
            with self.subTest(keys=keys[0]):
                blob = header_bytes(*keys, ssmd_name="demo")
                n = struct.unpack("<Q", blob[:8])[0]
                self.assertEqual(n, len(blob) - 8, "头长度字段要和真字节数一致")
                doc = routing.parse_safetensors_header(blob)
                self.assertEqual(routing.classify_safetensors_keys(doc), expected)
                self.assertEqual(routing.classify("SomethingUnknown", header=doc).folder, expected)


class ParseHeaderTests(unittest.TestCase):
    def test_valid_roundtrip(self):
        blob = header_bytes("model.diffusion_model.input_blocks.0.ops.weight", title="demo")
        doc = routing.parse_safetensors_header(blob)
        self.assertEqual(sorted(doc), ["__metadata__", "model.diffusion_model.input_blocks.0.ops.weight"])
        self.assertEqual(doc["__metadata__"], {"title": "demo"})

    def test_garbage_returns_none_never_raises(self):
        cases = [
            b"", b"not a safetensors file at all", b"\x00" * 8,
            struct.pack("<Q", 1) + b"{}",                       # 长度说 1 字节，实际 2 → 头读全才认
            struct.pack("<Q", 10) + b"{bad json",               # JSON 非法
            struct.pack("<Q", 5) + b"[1,2]",                    # 顶层不是 dict
            struct.pack("<Q", routing.MAX_HEADER_SIZE + 1) + b"{}" * 8,  # N 超上限
            struct.pack("<Q", 0) + b"{}",                       # N=0
            b"GGUF" + b"\x00" * 8,                              # GGUF 不是 safetensors
            struct.pack("<Q", 4) + b'{"a":1}',                  # 长度对不上（声明 4 实际 7）
        ]
        for blob in cases:
            with self.subTest(blob=blob[:12]):
                self.assertIsNone(routing.parse_safetensors_header(blob))
        self.assertIsNone(routing.parse_safetensors_header(None))
        self.assertIsNone(routing.parse_safetensors_header("a string"))
        # 结构合法但没有张量名：返回 {}，让 classify 继续按类型判
        self.assertEqual(routing.parse_safetensors_header(struct.pack("<Q", 2) + b"{}"), {})


class ProbeHeaderTests(unittest.TestCase):
    def test_two_stage_ranges(self):
        blob = header_bytes("lora_unet_a")
        n = len(blob) - 8
        fetch = FakeFetch(blob)
        doc = routing.probe_header(fetch)
        self.assertEqual(sorted(doc), ["lora_unet_a"])
        self.assertEqual(fetch.calls, [(0, 7), (8, 8 + n - 1)], "闭区间 Range：先 0-7 拿长度，再取 8..8+N-1")

    def test_large_header_beyond_4k_is_still_read(self):
        # 实测 Pony SDXL 头 365KB、kohya LoRA 头 80KB，固定 4K/64K 预取一定会截断
        keys = ["model.diffusion_model.down_blocks_%d_weight" % i for i in range(400)]
        blob = header_bytes(*keys)
        self.assertGreater(len(blob) - 8, 20_000)
        doc = routing.probe_header(FakeFetch(blob))
        self.assertEqual(len(doc), len(keys))

    def test_fetch_returning_none_is_not_fatal(self):
        blob = header_bytes("lora_unet_a")
        self.assertIsNone(routing.probe_header(FakeFetch(b"")))
        self.assertIsNone(routing.probe_header(FakeFetch(blob, fail_after=1)))   # 第二段拿不到
        self.assertIsNone(routing.probe_header(lambda s, e: None))
        self.assertIsNone(routing.probe_header(lambda s, e: (_ for _ in ()).throw(RuntimeError("403"))))

    def test_declared_length_over_limit_is_refused_without_second_fetch(self):
        blob = struct.pack("<Q", 500_000_000) + b"{}"
        fetch = FakeFetch(blob)
        self.assertIsNone(routing.probe_header(fetch))
        self.assertEqual(fetch.calls, [(0, 7)])
        small = FakeFetch(header_bytes("lora_unet_a"))
        self.assertIsNone(routing.probe_header(small, max_bytes=8))  # 调用方可以更严格

    def test_body_not_starting_with_brace_is_rejected(self):
        n = 2
        blob = struct.pack("<Q", n) + b"{}"
        fetch = FakeFetch(blob)
        self.assertEqual(routing.probe_header(fetch), {})
        bad = struct.pack("<Q", n) + b"PK"          # zip/其他容器的魔数
        self.assertIsNone(routing.probe_header(FakeFetch(bad)))
        self.assertEqual(routing.probe_header(FakeFetch(struct.pack("<Q", n) + b"  ")), None)

    def test_truncated_header_is_rejected(self):
        fetch = FakeFetch(header_bytes("lora_unet_a"))
        fetch.blob = fetch.blob[:20]                # 头只回了一半
        self.assertIsNone(routing.probe_header(fetch))


class ClassifyPrecedenceTests(unittest.TestCase):
    def test_header_overrides_model_type(self):
        d = routing.classify("Checkpoint", header={"lora_unet_blocks_0_weight": tensor("x")})
        self.assertEqual((d.folder, d.source), ("loras", "header"))
        self.assertIn("头", d.reason)
        # 反向：类型说是 LoRA，头里是完整 checkpoint → 以头为准
        d = routing.classify("LORA", header={"model.diffusion_model.x": tensor("x"),
                                             "first_stage_model.encoder.x": tensor("y")})
        self.assertEqual((d.folder, d.source), ("checkpoints", "header"))

    def test_motion_header_blocks_routing_even_if_type_is_routable(self):
        d = routing.classify("Checkpoint", header={"motion_modules.0.blocks.weight": tensor("x")})
        self.assertEqual((d.folder, d.source), (None, "none"))

    def test_header_may_be_a_bare_key_iterable(self):
        d = routing.classify("Checkpoint", header=["decoder.up.0.conv_in.weight"])
        self.assertEqual((d.folder, d.source), ("vae", "header"))
        self.assertEqual(routing.classify("Checkpoint", header=()).source, "type")
        self.assertEqual(routing.classify("Checkpoint", header=None).source, "type")
        self.assertEqual(routing.classify("Checkpoint", header={"__metadata__": {"a": 1}}).source, "type")

    def test_unrecognisable_header_falls_back_to_type_then_base_model(self):
        d = routing.classify("VAE", header={"whatever.onnx.weight": tensor("x")})
        self.assertEqual((d.folder, d.source), ("vae", "type"))
        d = routing.classify("UnknownFutureType", header={"whatever": tensor("x")}, base_model="SD 1.5")
        self.assertEqual((d.folder, d.source), ("checkpoints", "base_model"))

    def test_file_type_gate_and_hint(self):
        d = routing.classify("LORA", file_type="Negative")
        self.assertEqual((d.folder, d.source), (None, "none"))
        d = routing.classify("LORA", file_type="Workflow")
        self.assertIsNone(d.folder)
        d = routing.classify(None, file_type="Text Encoder")
        self.assertEqual((d.folder, d.source), ("text_encoders", "type"))
        d = routing.classify(None, file_type="Model")     # 泛称不给落点提示
        self.assertEqual((d.folder, d.source), (None, "none"))

    def test_base_model_fallback_only_for_known_families(self):
        for base in ["Pony", "SDXL 1.0", "FLUX.1 K", "NoobAI"]:
            self.assertEqual(routing.classify(None, base_model=base).folder, "checkpoints")
        for base in ["SVD", "Other", "", None, "Kandinsky"]:
            self.assertIsNone(routing.classify(None, base_model=base).folder)
        d = routing.classify(None, base_model="SD 1.5")
        self.assertEqual(d.source, "base_model")
        self.assertIn("低置信", d.reason)

    def test_no_input_at_all_is_none(self):
        d = routing.classify(None)
        self.assertEqual((d.folder, d.source), (None, "none"))
        d = routing.classify("")
        self.assertIsNone(d.folder)


class ResolveDestDirTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        for key in routing.CORE_FOLDERS:
            (self.root / key).mkdir(parents=True, exist_ok=True)

    def test_nested_layout_and_no_creation_by_default(self):
        dest = routing.resolve_dest_dir(str(self.root), "loras", "PonyRealism_v6")
        self.assertEqual(dest, os.path.join(str(self.root), "loras", "PonyRealism_v6"))
        self.assertFalse(os.path.exists(dest), "默认只算路径，建目录是调用方的事")
        made = routing.resolve_dest_dir(str(self.root), "loras", "PonyRealism_v6", create_missing=True)
        self.assertTrue(os.path.isdir(made))

    def test_missing_type_dir_is_refused_never_created(self):
        with TemporaryDirectory() as d:
            self.assertIsNone(routing.resolve_dest_dir(d, "checkpoints", "Demo"))
            self.assertIsNone(routing.resolve_dest_dir(d, "checkpoints", "Demo", create_missing=True),
                              "create_missing 也不许新建类型目录")

    def test_non_core_key_is_refused(self):
        for key in ["ipadapter", "animatediff_models", "pulid", "instantid", "configs", "unet", "clip",
                    "t2i_adapter", "", None, "Checkpoints", "../checkpoints"]:
            with self.subTest(key=key):
                self.assertIsNone(routing.resolve_dest_dir(str(self.root), key, "Demo"))

    def test_path_escape_and_absolute_names_refused(self):
        bad = ["..", ".", "...", "C:", "C:\\models\\evil", "C:/models/evil",
               r"\\server\share", "/etc/passwd", "loras/../checkpoints", "a/b", "a\\b",
               "nul", "CON", "com1", "aux.txt", "x" * 300, " ", "name ", "name.", ""]
        for name in bad:
            with self.subTest(bundle_name=name):
                self.assertIsNone(routing.resolve_dest_dir(str(self.root), "checkpoints", name))
                self.assertFalse(self.root.joinpath("checkpoints", "evil").exists())
        # 单层名字里的点是合法的，不许连带拒掉
        self.assertIsNotNone(routing.resolve_dest_dir(str(self.root), "checkpoints", "a..b"))

    def test_existing_file_with_same_name_is_left_alone(self):
        (self.root / "checkpoints" / "Demo").write_bytes(b"occupied")
        self.assertIsNone(routing.resolve_dest_dir(str(self.root), "checkpoints", "Demo"))
        self.assertTrue((self.root / "checkpoints" / "Demo").is_file())

    def test_empty_or_bad_models_root(self):
        for root in ["", None, str(self.root / "nowhere")]:
            with self.subTest(models_root=root):
                self.assertIsNone(routing.resolve_dest_dir(root, "checkpoints", "Demo"))

    def test_bundle_name_from_helper_is_accepted(self):
        name = routing.bundle_name_for("Pony Realism: 史诗级写真 (v6.0)", "6.0")
        dest = routing.resolve_dest_dir(str(self.root), "checkpoints", name)
        self.assertEqual(dest, os.path.join(str(self.root), "checkpoints", name))


class BundleNameTests(unittest.TestCase):
    def test_illegal_characters_become_underscores(self):
        self.assertEqual(routing.bundle_name_for('a/b\\c:d*e"f?g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")

    def test_joins_version_and_collapses_whitespace(self):
        self.assertEqual(routing.bundle_name_for("Demo  Model", " v2 "), "Demo Model_v2")
        self.assertEqual(routing.bundle_name_for("Demo", ""), "Demo")
        self.assertEqual(routing.bundle_name_for(None, None), "model")
        self.assertEqual(routing.bundle_name_for("..."), "model")
        self.assertEqual(routing.bundle_name_for("\u3000"), "model")   # 全角空格也算空白

    def test_dot_and_space_edges_and_leading_dots(self):
        self.assertEqual(routing.bundle_name_for("name.."), "name")
        self.assertEqual(routing.bundle_name_for("name  "), "name")
        self.assertEqual(routing.bundle_name_for(".hidden"), "_hidden")
        self.assertEqual(routing.bundle_name_for("  中文 模型 "), "中文 模型")

    def test_reserved_device_names(self):
        for raw in ["CON", "nul.txt", "COM1", "aux"]:
            with self.subTest(name=raw):
                out = routing.bundle_name_for(raw)
                self.assertTrue(routing.is_safe_segment(out), out)
                self.assertNotIn(out.split(".")[0].upper(), routing._RESERVED)

    def test_invisible_and_direction_control_chars_dropped(self):
        self.assertEqual(routing.bundle_name_for("demo\u200b\ufeffname\u202e"), "demoname")

    def test_nfc_normalisation_is_reproducible(self):
        self.assertEqual(routing.bundle_name_for("café"), routing.bundle_name_for("café"))

    def test_utf8_byte_budget_with_hash(self):
        for max_bytes in [140, 80, 40]:
            raw = "pony衍生模型请遵守原作者许可协议" * 12
            out = routing.bundle_name_for(raw, "v1.0", max_bytes=max_bytes)
            with self.subTest(max_bytes=max_bytes):
                self.assertLessEqual(len(out.encode("utf-8")), max_bytes)
                self.assertIn("_", out)
                self.assertTrue(routing.is_safe_segment(out), out)
        # 截断带哈希：两个长名不会撞成同一个
        a = routing.bundle_name_for("模型" * 90 + "A", "")
        b = routing.bundle_name_for("模型" * 90 + "B", "")
        self.assertNotEqual(a, b)
        self.assertNotEqual(routing.bundle_name_for("a" * 400), routing.bundle_name_for("b" * 400))

    def test_short_extension_is_kept(self):
        self.assertEqual(routing.bundle_name_for("demo.safetensors"), "demo.safetensors")

    def test_result_always_usable_as_segment(self):
        for raw in ["  ", "..", "\x00\x01", "?" * 30, "。", "……", "CON", "\\", "\\\\", "a" * 300]:
            with self.subTest(name=raw):
                self.assertTrue(routing.is_safe_segment(routing.bundle_name_for(raw)))


if __name__ == "__main__":
    unittest.main()
