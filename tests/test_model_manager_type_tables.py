# -*- coding: utf-8 -*-
"""model_manager 的映射表必须派生自 model_routing —— 防止「分流写好的落点被整理搬回去」。

背景（已实测确认的结构性问题）：
· 分流（model_routing）按 ComfyUI 核心 key 落点：models/diffusion_models、models/text_encoders、
  models/clip_vision、models/upscale_models…
· 旧整理（webui.mm_restore_organize）用 `set(model_manager.ENV_DIRS["comfyui"].values())` 反推
  「标准目录」，不在集合里的顶层目录一律当非标准，再按 `env_map.get(model_type)` 把模型搬回去。
  旧 ENV_DIRS 只有 6 个目录 → 分流产物全被判成非标准 → 一个 model.type=Checkpoint 而权重是
  Diffusion Model/UNet 的 FLUX 文件会被从 diffusion_models\\ 搬回 checkpoints\\，和分流直接对打。
这里表驱动钉住：派生关系、逐键一致、webui 表原样、TYPE_DIRS 兜底边界、本体文件名挑选、info 双读。
全部用构造数据，不碰真实模型库，不发任何网络请求。
"""
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import civitai_api
import model_manager as mm
import model_routing as routing

# 权威 23 值（精确大小写）
CIVITAI_TYPES = list(routing.CIVITAI_MODEL_TYPES)
# 历史混存的旧拼写（含旧 KNOWN_TYPES 一直在用的 Embedding）
LEGACY_TYPE_DIRS = {
    "ControlNet": "controlnet", "LoRA": "loras", "LYCORIS": "loras", "LOHA": "loras",
    "LOKR": "loras", "Model": "checkpoints", "Textual Inversion": "embeddings",
    "Diffusion Model": "diffusion_models", "Embedding": "embeddings",
}
# 旧 KNOWN_TYPES 的 16 项：派生后必须一项不落地继续被认识（否则历史数据的整理会退化成「未知类型」）
OLD_KNOWN_TYPES = {
    "LORA", "LoRA", "Checkpoint", "VAE", "TextualInversion", "Embedding",
    "ControlNet", "Controlnet", "LoCon", "DoRA", "Hypernetwork",
    "AestheticGradient", "Poses", "Wildcards", "Workflows", "MotionModule",
}
# webui/Forge 那份目录名不是分流落点，必须逐键原样保留（ENV_DIRS["webui"] 的旧形状）
OLD_WEBUI_DIRS = {
    "LORA": "Lora", "LoRA": "Lora", "LoCon": "Lora", "DoRA": "Lora",
    "Checkpoint": "Stable-diffusion",
    "VAE": "VAE",
    "TextualInversion": "embeddings", "Embedding": "embeddings",
    "ControlNet": "ControlNet", "Controlnet": "ControlNet",
    "Hypernetwork": "hypernetworks",
}


def info_files(*entries):
    """造 files[]：entries 为 (name, type) 或 (name, type, primary)。"""
    files = []
    for e in entries:
        item = {"name": e[0], "sizeKB": 100}
        if len(e) > 1 and e[1] is not None:
            item["type"] = e[1]
        if len(e) > 2 and e[2]:
            item["primary"] = True
        files.append(item)
    return files


def write_bundle(root, folder, bundle, filename, meta):
    """分流落点形状：<root>/<类型key>/<模型名>/<模型名>.safetensors(+ .civitai.info)。"""
    d = Path(root) / folder / bundle
    d.mkdir(parents=True, exist_ok=True)
    model = d / filename
    model.write_bytes(b"synthetic weight contents")
    (d / (os.path.splitext(filename)[0] + ".civitai.info")).write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return str(model)


class DerivedTableTests(unittest.TestCase):
    def test_comfyui_table_is_a_copy_of_the_routing_table(self):
        self.assertEqual(mm.ENV_DIRS["comfyui"], routing.COMFYUI_TYPE_DIRS)
        self.assertEqual(mm.ENV_DIRS["comfyui"], routing.MODEL_TYPE_TO_FOLDER)
        # 是派生副本而不是同一个对象：调用方改 ENV_DIRS 不许污染分流决策表
        self.assertIsNot(mm.ENV_DIRS["comfyui"], routing.MODEL_TYPE_TO_FOLDER)

    def test_known_types_is_a_copy_of_the_routing_universe(self):
        self.assertEqual(mm.KNOWN_TYPES, set(routing.KNOWN_MODEL_TYPES))
        self.assertIsNot(mm.KNOWN_TYPES, routing.KNOWN_MODEL_TYPES)
        self.assertTrue(set(routing.CIVITAI_MODEL_TYPES) <= mm.KNOWN_TYPES)
        self.assertTrue(OLD_KNOWN_TYPES <= mm.KNOWN_TYPES, "旧 16 项必须全部继续被认识")

    def test_every_authoritative_type_agrees_with_the_routing_decision(self):
        """表驱动：23 权威值 + 旧拼写，逐键比对「整理侧的目录」== 「分流的落点」。"""
        std_dirs = set(mm.ENV_DIRS["comfyui"].values())
        for raw in CIVITAI_TYPES + list(LEGACY_TYPE_DIRS):
            with self.subTest(model_type=raw):
                folder = routing.classify(raw).folder
                self.assertIn(raw, mm.KNOWN_TYPES, "权威类型必须被整理侧认识")
                if folder is None:
                    self.assertIn(raw, routing.NON_MODEL_TYPES)
                    self.assertNotIn(raw, mm.ENV_DIRS["comfyui"])
                    continue
                self.assertEqual(mm.ENV_DIRS["comfyui"][raw], folder)
                self.assertIn(folder, std_dirs, "分流落点必须在标准目录集合里")
                self.assertIn(folder, routing.CORE_FOLDERS)

    def test_routing_landing_dirs_are_all_standard_dirs(self):
        """反向核对：分流可能写出的每一个目录，整理侧都必须认成标准目录（含曾被点名缺的四个）。"""
        std_dirs = set(mm.ENV_DIRS["comfyui"].values())
        self.assertEqual(std_dirs, set(routing.MODEL_TYPE_TO_FOLDER.values()))
        self.assertTrue({"diffusion_models", "text_encoders", "clip_vision", "upscale_models"} <= std_dirs)
        # 分流侧本来就不会写这三个非核心 key（不是标准目录是预期，别误以为需要补进表里）
        self.assertEqual(std_dirs & {"style_models", "vae_approx", "latent_upscale_models"}, set())
        self.assertEqual(routing.CORE_FOLDERS & {"style_models", "vae_approx", "latent_upscale_models"}, set())

    def test_webui_table_kept_verbatim(self):
        self.assertEqual(mm.ENV_DIRS["webui"], OLD_WEBUI_DIRS)
        self.assertEqual(set(mm.ENV_DIRS), {"webui", "comfyui"})

    def test_restore_organize_reverse_inference_agrees_with_routing(self):
        """逐字复刻 webui.mm_restore_organize 的判据（webui.py 不由本任务修改，但数据源必须对它正确）：
            std_dirs = set(ENV_DIRS["comfyui"].values());  top = rel.split(os.sep)[0]
            if top in std_dirs: 整棵子树跳过        # 分流产物一律不碰
            tdir = env_map.get(model_type)          # 反推「标准目录」
        两条都必须与分流结论一致，否则就是「误整理恢复把分流结果搬走」。"""
        env_map = mm.ENV_DIRS["comfyui"]
        std_dirs = set(env_map.values())
        for raw, folder in sorted(routing.MODEL_TYPE_TO_FOLDER.items()):
            top = ("%(k)s/%(b)s/%(f)s" % {"k": folder, "b": "Flux Demo", "f": "flux.safetensors"}).split("/")[0]
            with self.subTest(model_type=raw):
                self.assertIn(top, std_dirs, "分流产物所在目录必须被认成标准目录，否则整站会被搬走")
                self.assertEqual(env_map.get(raw), folder, "按类型反推的标准目录必须就是分流落点")
                self.assertEqual(routing.classify(raw).folder, folder)

    def test_type_dirs_fallback_only_covers_non_routed_types(self):
        """TYPE_DIRS 兜底表的边界：分流会落的类型一律由 ENV_DIRS 先命中；
        只剩分流明确不分流的类型才落到这张旧表（用户显式整理的搬运，和分流方向相反但互不触发）。"""
        for tdir_key in mm.TYPE_DIRS:
            with self.subTest(model_type=tdir_key):
                self.assertIn(tdir_key, mm.KNOWN_TYPES, "兜底表里的类型必须是已知类型")
                if tdir_key in mm.ENV_DIRS["comfyui"]:
                    self.assertNotIn(tdir_key, routing.NON_MODEL_TYPES)
                else:
                    self.assertIn(tdir_key, routing.NON_MODEL_TYPES,
                                  "没有分流落点的类型必须显式不分流，否则整理和分流会抢同一个模型")
        # BASE_DIRS 全仓库无调用方（含 js / tests），作为死代码删除
        self.assertFalse(hasattr(mm, "BASE_DIRS"))

    def test_organize_never_moves_a_routed_bundle_back_to_another_folder(self):
        """真实形状回归：模型已在 <root>/<分流目录>/<模型名>/ 下，整理只许留在同一个分流目录里。"""
        with TemporaryDirectory() as d:
            root = Path(d)
            for raw, folder in sorted(mm.ENV_DIRS["comfyui"].items()):
                model = write_bundle(
                    root, folder, "Demo Bundle", "demo.safetensors",
                    {"name": "Demo Bundle", "type": raw, "baseModel": "SDXL",
                     "tags": [{"name": "style"}],
                     "files": info_files(("demo.safetensors", "Model", True))})
                dest, msgs = mm.organize_model(model, str(root), dry_run=True,
                                               env="comfyui", mode="civitai")
                rel = os.path.relpath(dest, str(root)).replace(os.sep, "/")
                with self.subTest(model_type=raw):
                    self.assertEqual(rel.split("/")[0], folder,
                                     "整理不能把 %s 搬出分流目录（消息 %s）" % (raw, msgs))


class OrganizeFilenameTests(unittest.TestCase):
    def test_attachment_primary_does_not_rename_the_weight(self):
        """:352 的缺陷本体：Civitai 对 Config/Archive/Workflow 也打 primary。"""
        with TemporaryDirectory() as d:
            root = Path(d) / "models"
            attachments = [("readme.json", "Config"), ("pack.zip", "Archive"),
                           ("flow.json", "Workflow"), ("set.zip", "Training Data")]
            for name, atype in attachments:
                with self.subTest(attachment=atype):
                    model = write_bundle(root, "checkpoints", "Demo", "demo.safetensors",
                                         {"name": "", "type": "Checkpoint", "baseModel": "SDXL",
                                          "tags": [{"name": "style"}],
                                          "files": info_files((name, atype, True),
                                                               ("demo.safetensors", "Model"))})
                    dest, _ = mm.organize_model(model, str(root), dry_run=True,
                                                env="comfyui", mode="civitai")
                    self.assertEqual(os.path.basename(dest), "demo.safetensors")

    def test_weight_primary_beats_weight_non_primary_and_keeps_stem(self):
        model = None
        with TemporaryDirectory() as d:
            root = Path(d) / "models"
            model = write_bundle(root, "checkpoints", "Demo", "raw.safetensors",
                                 {"name": "", "type": "Checkpoint", "baseModel": "SDXL",
                                  "tags": [{"name": "style"}],
                                  "files": info_files(("pruned.safetensors", "Pruned Model"),
                                                       ("full.safetensors", "Model", True),
                                                       ("cfg.json", "Config"))})
            dest, _ = mm.organize_model(model, str(root), dry_run=True, env="comfyui", mode="civitai")
            self.assertEqual(os.path.basename(dest), "full.safetensors")

    def test_no_weight_type_falls_back_to_the_old_choice(self):
        """一个权重类型都没有 → 原样退回旧逻辑（primary 且非 Negative，否则 files[0]）。"""
        only_attach = info_files(("pack.zip", "Archive", True), ("cfg.json", "Config"))
        self.assertEqual(mm.choose_body_file(only_attach)["name"], "pack.zip")
        negative_only = info_files(("neg.safetensors", "Negative", True))
        self.assertEqual(mm.choose_body_file(negative_only)["name"], "neg.safetensors")
        # 没 primary 也没权重类型：旧逻辑同样是 files[0]
        self.assertEqual(mm.choose_body_file(info_files(("a.zip", "Archive"),
                                                        ("b.json", "Config")))["name"], "a.zip")
        self.assertIsNone(mm.choose_body_file([]))
        self.assertIsNone(mm.choose_body_file(None))
        self.assertIsNone(mm.choose_body_file(["not-a-dict"]))

    def test_missing_type_key_defaults_to_weight_exactly_like_civitai_api(self):
        """历史 info 常不写 files[].type：缺 type 按 Model 算权重（与 civitai_api 同口径）。"""
        files = info_files(("legacy.zip", None, True), ("legacy.safetensors", None))
        self.assertTrue(routing.is_weight_file_type(files[0]))
        self.assertEqual(mm.choose_body_file(files)["name"], "legacy.zip")

    def test_choose_body_file_uses_the_civitai_api_whitelist(self):
        for type_ in civitai_api.WEIGHT_FILE_TYPES:
            label = routing.FILE_TYPE_LABELS[type_]
            self.assertEqual(mm.choose_body_file(info_files(("cfg.json", "Config", True),
                                                             ("w.safetensors", label)))["name"],
                             "w.safetensors")


class RenameToCivitaiFilenameTests(unittest.TestCase):
    def test_files_fallback_skips_attachment_names(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "raw.safetensors"
            p.write_bytes(b"synthetic")
            for name, atype in [("readme.json", "Config"), ("pack.zip", "Archive"),
                                ("flow.json", "Workflow")]:
                with self.subTest(attachment=atype):
                    meta = {"files": info_files((name, atype, True), ("real.safetensors", "Model"))}
                    new, _ = mm.rename_to_civitai(str(p), meta, dry_run=True)
                    self.assertEqual(Path(new).name, "real.safetensors")

    def test_meta_name_still_wins_over_file_entries(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "raw.safetensors"
            p.write_bytes(b"synthetic")
            meta = {"name": "Koikatsu Style",
                    "files": info_files(("flow.json", "Workflow", True), ("real.safetensors", "Model"))}
            new, _ = mm.rename_to_civitai(str(p), meta, dry_run=True)
            self.assertEqual(Path(new).name, "Koikatsu Style.safetensors")


class FindInfoFileTests(unittest.TestCase):
    """find_info_file 必须同时认 .info.json（扩展/反向解析写的）与 .civitai.info（分流新写的）。"""

    def _bundle(self, root, folder="diffusion_models", bundle="Flux Demo", name="flux.safetensors"):
        return write_bundle(root, folder, bundle, name, {"name": "Flux Demo", "type": "UNet"})

    def test_dot_civitai_info_is_found(self):
        with TemporaryDirectory() as d:
            model = self._bundle(Path(d))
            found = mm.find_info_file(model)
            self.assertEqual(os.path.basename(found), "flux.civitai.info")

    def test_dot_info_json_is_found(self):
        with TemporaryDirectory() as d:
            model = self._bundle(Path(d))
            side = Path(model).with_name("flux.info.json")
            side.write_text(json.dumps({"name": "Flux Demo", "modelId": 1}), encoding="utf-8")
            self.assertEqual(mm.find_info_file(model), str(side))

    def test_precedence_info_json_then_civitai_info_then_bare_civitai_info(self):
        with TemporaryDirectory() as d:
            model = self._bundle(Path(d))
            base = Path(model)
            (base.with_name("flux.civitai.info")).write_text('{"name":"A"}', encoding="utf-8")
            (base.with_name("flux.info.json")).write_text('{"name":"B"}', encoding="utf-8")
            self.assertEqual(os.path.basename(mm.find_info_file(model)), "flux.info.json")
            os.remove(base.with_name("flux.info.json"))
            self.assertEqual(os.path.basename(mm.find_info_file(model)), "flux.civitai.info")
            os.remove(base.with_name("flux.civitai.info"))
            bare = base.parent / "civitai.info"
            bare.write_text('{"name":"C"}', encoding="utf-8")
            self.assertEqual(mm.find_info_file(model), str(bare))

    def test_sd_flat_json_needs_model_or_version_id(self):
        with TemporaryDirectory() as d:
            model = self._bundle(Path(d))
            os.remove(os.path.splitext(model)[0] + ".civitai.info")
            flat = Path(model).with_name("flux.json")
            flat.write_text('{"sd_version":"sd1"}', encoding="utf-8")
            self.assertIsNone(mm.find_info_file(model))
            flat.write_text(json.dumps({"model_id": 12345}), encoding="utf-8")
            self.assertEqual(mm.find_info_file(model), str(flat))

    def test_both_suffixes_yield_name_and_trigger_words(self):
        """用户实测：ComfyUI 目录里 154 个 .info.json、0 个 .civitai.info，分流后新写的才是后者。"""
        meta = {"name": "Flux Demo", "type": "UNet", "trainedWords": ["sig2", "flux style"],
                "files": info_files(("flux.safetensors", "Diffusion Model", True))}
        for suffix in (".civitai.info", ".info.json"):
            with self.subTest(suffix=suffix):
                with TemporaryDirectory() as d:
                    model = self._bundle(Path(d))
                    os.remove(os.path.splitext(model)[0] + ".civitai.info")
                    Path(model).with_name("flux" + suffix).write_text(
                        json.dumps(meta, ensure_ascii=False), encoding="utf-8")
                    found = mm.find_info_file(model)
                    self.assertTrue(found)
                    with open(found, encoding="utf-8") as handle:
                        got = json.load(handle)
                    self.assertEqual(got["name"], "Flux Demo")
                    self.assertEqual(got["trainedWords"], ["sig2", "flux style"])
                    self.assertEqual(mm.body_file_name(got["files"]), "flux.safetensors")


if __name__ == "__main__":
    unittest.main()
