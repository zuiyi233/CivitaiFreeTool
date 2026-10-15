# -*- coding: utf-8 -*-
"""files[].type 权威 17 值的表驱动测试：每种类型都断言「能否下载」与「能否被选为主文件」。

来源：官方仓库 civitai/civitai 的 prisma 模型文件类型常量 + 本机实测 API。
覆盖任务 #7（pick_file 会把附件当主模型）与任务 #8（model_files 白名单缺值）。
纯离线：不发网络请求。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import civitai_api
from civitai_api import CivitaiAPI, CivitaiError

# type → (可下载, 会被选为主文件)
EXPECTED = {
    "Model": (True, True),
    "Text Encoder": (True, True),
    "Vision Encoder": (True, True),
    "Pruned Model": (True, True),
    "Negative": (True, False),          # 负面嵌入可以下载，但绝不能当主模型
    "Training Data": (False, False),
    "VAE": (True, True),
    "Config": (False, False),
    "Archive": (False, False),
    "UNet": (True, True),
    "Diffusion Model": (True, True),
    "CLIPVision": (True, True),
    "ControlNet": (True, True),
    "Workflow": (False, False),
    "Upscaler": (True, True),
    "Enhancement LoRA": (True, True),
    "Other": (False, False),
}
# 已废弃但历史数据里仍会出现的旧拼写，语义与对应的现值一致
LEGACY = {
    "TrainData": "Training Data",
    "InpaintMask": "Other",
    "Template": "Other",
    "Controlnet": "ControlNet",
    "Control Net": "ControlNet",
}
ATTACHMENT_NAMES = {"Training Data": "set.zip", "Archive": "pack.zip", "Config": "cfg.json",
                    "Workflow": "flow.json", "Other": "misc.zip", "Image": "preview.png"}
# 兜底扩展名集合之外的后缀：用来证明「已知类型不再靠扩展名巧合」
ODD_EXT = ".pkl"


def f(name, type_=None, primary=False, **extra):
    d = {"id": 1, "name": name, "sizeKB": 100}
    if type_ is not None:
        d["type"] = type_
    if primary:
        d["primary"] = True
    d.update(extra)
    return d


class FileTypeTableTests(unittest.TestCase):
    def listable(self, type_, name):
        """model_files 是否放行该类型（异常 = 不放行）"""
        try:
            return any(x.get("name") == name for x in CivitaiAPI().model_files({"files": [f(name, type_, primary=True)]}))
        except CivitaiError:
            return False

    def pickable(self, type_, name):
        """pick_file 是否会把该类型选成主文件（异常 = 不会）"""
        try:
            got, _ = CivitaiAPI().pick_file({"files": [f(name, type_, primary=True)]})
            return got.get("type") == type_
        except CivitaiError:
            return False

    def test_every_authoritative_file_type_has_one_verdict(self):
        self.assertEqual(17, len(EXPECTED))
        self.assertEqual({civitai_api.normalize_file_type({"type": t}) for t in EXPECTED},
                         set(civitai_api.CIVITAI_FILE_TYPES[:17]))
        for type_, (want_dl, want_pick) in EXPECTED.items():
            weight = ATTACHMENT_NAMES.get(type_, "demo.safetensors")
            with self.subTest(type=type_, ext=weight):
                self.assertEqual(self.listable(type_, weight), want_dl)
                self.assertEqual(self.pickable(type_, weight), want_pick)
            odd = "demo" + ODD_EXT
            with self.subTest(type=type_, ext=ODD_EXT):
                # 已知类型的判定只看类型白名单，不再依赖扩展名恰好在 weights 集合里
                self.assertEqual(self.listable(type_, odd), want_dl)
                self.assertEqual(self.pickable(type_, odd), want_pick)

    def test_legacy_spellings_map_to_their_current_meaning(self):
        for legacy, current in LEGACY.items():
            want_dl, want_pick = EXPECTED[current]
            with self.subTest(legacy=legacy):
                self.assertEqual(self.listable(legacy, "legacy.safetensors"), want_dl)
                self.assertEqual(self.pickable(legacy, "legacy.safetensors"), want_pick)

    def test_type_spelling_variants_normalize(self):
        for spell in ("ControlNet", "controlnet", "Control Net", "CONTROL_NET", " control-net "):
            with self.subTest(controlnet=spell):
                self.assertTrue(self.listable(spell, "cn.safetensors"))
                self.assertTrue(self.pickable(spell, "cn.safetensors"))
        for spell in ("TRAINING_DATA", "Training Data", "training data", "TrainData", "work-flow"):
            with self.subTest(attachment=spell):
                self.assertFalse(self.listable(spell, "s.zip"))
                self.assertFalse(self.pickable(spell, "s.zip"))

    def test_newly_whitelisted_types_survive_cold_extensions(self):
        """任务 #8 的实际漏网：这些类型在旧白名单里没有，后缀又不在兜底集合里 → 被静默丢掉。"""
        for type_, name in [("Pruned Model", "m.torchscript"), ("Vision Encoder", "ve.pkl"),
                            ("CLIPVision", "cv.bin.bak"), ("UNet", "unet.pkc"),
                            ("Upscaler", "4x.pkf"), ("Enhancement LoRA", "enh.pkh")]:
            with self.subTest(type=type_):
                self.assertTrue(self.listable(type_, name))

    def test_missing_type_key_still_defaults_to_model(self):
        self.assertEqual(CivitaiAPI().model_files({"files": [f("no_type.safetensors", None, True)]})[0]["name"],
                         "no_type.safetensors")
        self.assertEqual(CivitaiAPI().pick_file({"files": [f("no_type.safetensors", None, True)]})[0]["name"],
                         "no_type.safetensors")

    def test_explicit_null_type_does_not_default_to_model(self):
        """type 显式为 null（键存在）时不能当成 Model 放行一个 zip"""
        version = {"files": [{"id": 1, "name": "pack.zip", "type": None, "primary": True}]}
        with self.assertRaises(CivitaiError):
            CivitaiAPI().model_files(version)


class PickFileRegressionTests(unittest.TestCase):
    def test_primary_config_attachment_is_not_downloaded_as_model(self):
        """任务 #7：Civitai 对附件也打 primary → 修复前 pick_file 直接返回它"""
        cfg, mdl = f("readme.json", "Config", True), f("real.safetensors", "Model")
        version = {"files": [cfg, mdl]}
        got, index = CivitaiAPI().pick_file(version)
        self.assertIs(got, mdl)
        self.assertEqual(index, 1)

    def test_primary_workflow_archive_traindata_all_yield_to_weight(self):
        for attach_type in ("Workflow", "Archive", "Training Data"):
            with self.subTest(attach=attach_type):
                weight = f("w.safetensors", "Pruned Model")
                got, index = CivitaiAPI().pick_file({"files": [f("a.zip", attach_type, True), weight]})
                self.assertIs(got, weight)
                self.assertEqual(index, 1)

    def test_negative_never_primary_even_when_it_is_the_only_primary(self):
        emb, mdl = f("bad.safetensors", "Negative", True), f("good.safetensors", "Model")
        got, _ = CivitaiAPI().pick_file({"files": [emb, mdl]})
        self.assertIs(got, mdl)
        with self.assertRaises(CivitaiError):
            CivitaiAPI().pick_file({"files": [emb]})

    def test_weight_primary_wins_over_weight_non_primary(self):
        second, first = f("b.safetensors", "Diffusion Model"), f("a.safetensors", "Diffusion Model", True)
        got, index = CivitaiAPI().pick_file({"files": [second, first]})
        self.assertIs(got, first)
        self.assertEqual(index, 1)

    def test_non_weight_only_version_still_picks_the_real_file(self):
        """VAE/Text Encoder/Upscaler 这类模型本身只有非权重类型文件 —— 不能因为白名单而报错"""
        for type_, name in [("VAE", "vae.safetensors"), ("Text Encoder", "te.gguf"),
                            ("Upscaler", "up.pth"), ("ControlNet", "cn.safetensors"),
                            ("CLIPVision", "cv.safetensors")]:
            with self.subTest(type=type_):
                got, _ = CivitaiAPI().pick_file({"files": [f(name, type_, True)]})
                self.assertEqual(got["type"], type_)

    def test_attachments_only_raises_instead_of_returning_the_attachment(self):
        version = {"files": [f("flow.json", "Workflow", True), f("set.zip", "Training Data")]}
        with self.assertRaises(CivitaiError):
            CivitaiAPI().pick_file(version)

    def test_empty_and_malformed_versions(self):
        api = CivitaiAPI()
        for version in ({"files": []}, {}, {"files": ["not-a-dict"]}):
            with self.subTest(version=version):
                with self.assertRaises(CivitaiError):
                    api.pick_file(version)

    def test_unknown_future_weight_type_is_not_dropped(self):
        """前向兼容：未知类型 + 权重后缀仍然可下载/可挑选（对齐 test_download_selection 的既有约定）"""
        version = {"files": [f("future.gguf", "Future Weight Type", True)]}
        self.assertEqual(len(CivitaiAPI().model_files(version)), 1)
        self.assertEqual(CivitaiAPI().pick_file(version)[0]["type"], "Future Weight Type")

    def test_return_contract_is_still_file_and_index(self):
        version = {"files": [f("x.json", "Config"), f("y.safetensors", "Model")]}
        got = CivitaiAPI().pick_file(version)
        self.assertIsInstance(got, tuple)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0], version["files"][1])
        self.assertEqual(got[1], 1)


class ModelFilesSelectionTests(unittest.TestCase):
    def test_attachments_filtered_but_all_weight_variants_kept(self):
        files = [f("m.safetensors", "Model", True), f("m.sft", "Pruned Model"),
                 f("m-q4.gguf", "UNet"), f("diff.safetensors", "Diffusion Model"),
                 f("te.safetensors", "Text Encoder"), f("ve.onnx", "Vision Encoder"),
                 f("cv.safetensors", "CLIPVision"), f("up.pth", "Upscaler"),
                 f("e.safetensors", "Enhancement LoRA"), f("neg.safetensors", "Negative"),
                 f("cfg.json", "Config", True), f("train.zip", "Training Data"),
                 f("pack.zip", "Archive"), f("flow.json", "Workflow"), f("misc.zip", "Other")]
        kept = CivitaiAPI().model_files({"files": files})
        self.assertEqual([x["type"] for x in kept], [x["type"] for x in files[:10]])
        self.assertFalse(any(x["type"] in ("Config", "Training Data", "Archive", "Workflow", "Other")
                             for x in kept))

    def test_only_attachments_fail_closed(self):
        for attach in ("Config", "Archive", "Workflow", "Training Data", "Other", "TrainData"):
            with self.subTest(type=attach):
                with self.assertRaises(CivitaiError):
                    CivitaiAPI().model_files({"files": [f("only.zip", attach, True)]})


if __name__ == "__main__":
    unittest.main()
