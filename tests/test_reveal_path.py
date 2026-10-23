# -*- coding: utf-8 -*-
"""扩展 /api/reveal 的路径白名单（Api.reveal_registered_path）。

只构造 Api.__new__ 的进程内状态 + 临时文件；不启动桥、不真的打开资源管理器。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import webui


class RevealPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.file = self.root / "models" / "demo.safetensors"
        self.file.parent.mkdir(parents=True)
        self.file.write_bytes(b"weights")
        self.api = webui.Api.__new__(webui.Api)
        self.api.model_rows = []
        self.api.rp_rows = []
        self.api.dl = SimpleNamespace(tasks=[], get_history=lambda: [])
        self.opened = []
        self.api.open_in_folder = lambda p: (self.opened.append(p) or {"ok": True})

    def tearDown(self):
        self.temp.cleanup()

    def _reveal(self, path):
        return self.api.reveal_registered_path(path)

    def test_library_file_is_opened_and_selected(self):
        self.api.model_rows = [{"path": str(self.file)}]
        r = self._reveal(str(self.file))
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.opened, [str(self.file)])
        self.assertEqual(r["path"], os.path.abspath(str(self.file)))

    def test_history_file_path_and_dest_dir(self):
        self.api.dl.get_history = lambda: [{"file_path": str(self.file)}]
        self.assertTrue(self._reveal(str(self.file))["ok"])
        self.opened.clear()
        self.api.dl.get_history = lambda: [{"dest_dir": str(self.file.parent),
                                           "filename": self.file.name}]
        self.assertTrue(self._reveal(str(self.file.parent / self.file.name))["ok"])

    def test_task_dest_dir_folder(self):
        self.api.dl.tasks = [SimpleNamespace(dest_dir=str(self.file.parent),
                                            filename=self.file.name)]
        r = self._reveal(str(self.file.parent))
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.opened, [str(self.file.parent)])

    def test_unregistered_path_is_refused(self):
        self.api.model_rows = [{"path": str(self.file)}]
        other = self.root / "elsewhere" / "other.bin"
        other.parent.mkdir()
        other.write_bytes(b"x")
        r = self._reveal(str(other))
        self.assertFalse(r["ok"])
        self.assertIn("未登记", r["msg"])
        self.assertEqual(self.opened, [])
        # 已登记目录里的另一个文件也不算登记（精确匹配，不做前缀放行）
        r2 = self._reveal(str(self.file.parent / "sibling.bin"))
        self.assertFalse(r2["ok"])
        self.assertEqual(self.opened, [])

    def test_registered_but_missing_is_not_opened(self):
        gone = self.root / "gone.safetensors"
        self.api.model_rows = [{"path": str(gone)}]
        r = self._reveal(str(gone))
        self.assertFalse(r["ok"])
        self.assertIn("不存在", r["msg"])
        self.assertEqual(self.opened, [])

    def test_normalization_ignores_case_and_trailing_sep(self):
        self.api.dl.tasks = [SimpleNamespace(dest_dir=str(self.file.parent), filename="")]
        variants = [str(self.file.parent), str(self.file.parent) + os.sep]
        if os.name == "nt":
            variants.append(str(self.file.parent).upper())
        for v in variants:
            with self.subTest(v=v):
                self.opened.clear()
                self.assertTrue(self._reveal(v)["ok"], v)
                self.assertEqual(len(self.opened), 1)

    def test_invalid_input_is_refused(self):
        for bad in (None, "", "   ", "a\x00b", "x" * 4097):
            with self.subTest(bad=repr(bad)[:30]):
                r = self._reveal(bad)
                self.assertFalse(r["ok"])

    def test_open_failure_is_propagated(self):
        self.api.model_rows = [{"path": str(self.file)}]
        self.api.open_in_folder = lambda p: {"ok": False, "msg": "无法打开"}
        r = self._reveal(str(self.file))
        self.assertFalse(r["ok"])
        self.assertEqual(r["msg"], "无法打开")
        self.assertNotIn("path", r)


if __name__ == "__main__":
    unittest.main()
