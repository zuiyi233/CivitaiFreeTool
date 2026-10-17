# -*- coding: utf-8 -*-
"""下载收尾失败的可见性：不再 `except: pass`，而是写 error.log + 挂到任务行上。

为什么值得单独钉：收尾（元数据/封面/视频/旧版清理）失败时下载本体是好的，界面显示「已完成」，
但模型在库里没有名字/类型/海报 —— 没有留痕就只能靠猜。这里锁住三件事：
1) 失败被记录且带阶段名；2) 记录有上限，不会无限膨胀；3) get_tasks 把记录暴露给列表与自动化接口。
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import downloader, webui


class PostprocessVisibilityTest(unittest.TestCase):
    def setUp(self):
        self.api = webui.Api.__new__(webui.Api)
        self.api.cfg = {}
        self.api.log_error = Mock()
        self.api.dl = Mock()
        self.api.dl._lock = threading.RLock()
        self.task = downloader.DownloadTask("https://example.invalid/f", "D:/models", "a.safetensors")
        self.task.status = downloader.ST_DONE

    def test_failure_is_logged_and_attached_with_stage(self):
        self.api._note_postprocess_error(self.task, "metadata", PermissionError("denied"))
        errs = self.task.info["postprocess_errors"]
        self.assertEqual(errs[0]["stage"], "metadata")
        self.assertIn("denied", errs[0]["error"])
        self.assertTrue(self.api.log_error.called)
        self.assertIn("metadata", str(self.api.log_error.call_args))

    def test_records_are_capped(self):
        for i in range(30):
            self.api._note_postprocess_error(self.task, "cover", "e%d" % i)
        self.assertEqual(len(self.task.info["postprocess_errors"]), 20)

    def test_bad_info_does_not_raise(self):
        self.task.info = None
        self.api._note_postprocess_error(self.task, "video", RuntimeError("boom"))
        self.assertIsInstance(self.task.info, dict)
        self.assertEqual(self.task.info["postprocess_errors"][0]["stage"], "video")

    def test_get_tasks_exposes_postprocess_errors(self):
        self.task.info["postprocess_errors"] = [{"stage": "cover", "error": "x"}]
        self.api.dl.tasks = [self.task]
        rows = self.api.get_tasks()
        self.assertEqual(rows[0]["postprocess_errors"], [{"stage": "cover", "error": "x"}])
        self.assertEqual(rows[0]["id"], self.task.id)

    def test_get_tasks_defaults_to_empty_list(self):
        self.api.dl.tasks = [self.task]
        self.assertEqual(self.api.get_tasks()[0]["postprocess_errors"], [])


if __name__ == "__main__":
    unittest.main()
