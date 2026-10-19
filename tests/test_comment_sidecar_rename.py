# -*- coding: utf-8 -*-
"""<主干>_评论.txt 必须跟模型本体一起改名/移动。

背景 bug：下载收尾写出的评论/评价文本是 `<主干>_评论.txt`（完整后缀，不是扩展名），
model_manager._SIDE_EXTS 原先只列了 ".txt"，于是 rename_to_civitai 之后评论文件
留在旧名前缀下，rename_model_bundle / organize_model / mm_restore_organize 等
复用 _SIDE_EXTS 的路径同样漏掉它。这里钉住 _SIDE_EXTS 的契约与改名行为。

只在 tempfile 临时目录里造合成文件，不碰真实模型库 / 配置 / error.log。
"""
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model_manager

COMMENT = "用户甲：好用\n用户乙：风格很像\n"


class CommentSidecarRenameTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def make_bundle(self, stem):
        """造一个完整模型包：本体 + 评论 + info + 预览图 + .images 目录。"""
        (self.dir / (stem + ".safetensors")).write_bytes(b"synthetic-weights")
        (self.dir / (stem + "_评论.txt")).write_text(COMMENT, encoding="utf-8")
        (self.dir / (stem + ".civitai.info")).write_text('{"name": "old"}', encoding="utf-8")
        (self.dir / (stem + ".preview.png")).write_bytes(b"png")
        imgs = self.dir / (stem + ".images")
        imgs.mkdir()
        (imgs / "1.png").write_bytes(b"png")
        return str(self.dir / (stem + ".safetensors"))

    def test_renames_all_sidecars_including_comment(self):
        src = self.make_bundle("old name")
        new_path, msgs = model_manager.rename_to_civitai(src, {"name": "New Model"})

        self.assertEqual(new_path, str(self.dir / "New Model.safetensors"), msgs)
        for suffix in (".safetensors", "_评论.txt", ".civitai.info", ".preview.png"):
            self.assertTrue((self.dir / ("New Model" + suffix)).is_file(), suffix)
            self.assertFalse((self.dir / ("old name" + suffix)).exists(), suffix)
        self.assertTrue((self.dir / "New Model.images" / "1.png").is_file())
        self.assertFalse((self.dir / "old name.images").exists())
        # 评论内容一个字节都不能变
        self.assertEqual((self.dir / "New Model_评论.txt").read_text(encoding="utf-8"), COMMENT)
        self.assertTrue(any("已重命名" in m for m in msgs), msgs)

    def test_dry_run_reports_target_without_touching_disk(self):
        src = self.make_bundle("old name")
        before = sorted(p.name for p in self.dir.iterdir())

        new_path, msgs = model_manager.rename_to_civitai(src, {"name": "New Model"}, dry_run=True)

        self.assertEqual(new_path, str(self.dir / "New Model.safetensors"), msgs)
        self.assertTrue(any("将重命名" in m for m in msgs), msgs)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), before)   # 盘上零变化
        self.assertTrue(os.path.isfile(src))
        self.assertEqual((self.dir / "old name_评论.txt").read_text(encoding="utf-8"), COMMENT)
        self.assertFalse((self.dir / "New Model.safetensors").exists())
        self.assertFalse((self.dir / "New Model_评论.txt").exists())

    def test_existing_target_comment_refuses_and_keeps_source(self):
        src = self.make_bundle("old name")
        (self.dir / "New Model_评论.txt").write_text("目标处既有评论", encoding="utf-8")

        new_path, msgs = model_manager.rename_to_civitai(src, {"name": "New Model"})

        self.assertEqual(new_path, src, msgs)
        self.assertTrue(any("目标存在同名附属文件/图片文件夹，未改名" in m for m in msgs), msgs)
        # 源模型与源评论都原样保留（拒绝必须发生在任何 rename 之前）
        self.assertEqual((self.dir / "old name.safetensors").read_bytes(), b"synthetic-weights")
        self.assertEqual((self.dir / "old name_评论.txt").read_text(encoding="utf-8"), COMMENT)
        self.assertEqual((self.dir / "New Model_评论.txt").read_text(encoding="utf-8"),
                         "目标处既有评论")
        self.assertFalse((self.dir / "New Model.safetensors").exists())

    def test_side_exts_contains_comment_suffix(self):
        # 防回退：rename_to_civitai / rename_model_bundle / organize_model / mm_move /
        # mm_restore_organize / gui 移动全部复用这张表，少这一项评论就会掉队。
        self.assertIn("_评论.txt", model_manager._SIDE_EXTS)


if __name__ == "__main__":
    unittest.main()
