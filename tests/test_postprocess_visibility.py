# -*- coding: utf-8 -*-
"""下载收尾失败的可见性：不再 `except: pass`，而是写 error.log + 挂到任务行上。

为什么值得单独钉：收尾（元数据/封面/视频/旧版清理）失败时下载本体是好的，界面显示「已完成」，
但模型在库里没有名字/类型/海报 —— 没有留痕就只能靠猜。这里锁住三件事：
1) 失败被记录且带阶段名；2) 记录有上限，不会无限膨胀；3) get_tasks 把记录暴露给列表与自动化接口。
"""
import sys
import tempfile
import threading
import time
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
        self.task.info["postprocess"] = {"done": True, "stages": ["metadata"]}
        self.api.dl.tasks = [self.task]
        rows = self.api.get_tasks()
        self.assertEqual(rows[0]["postprocess_errors"], [{"stage": "cover", "error": "x"}])
        self.assertEqual(rows[0]["postprocess"], {"done": True, "stages": ["metadata"]})
        self.assertEqual(rows[0]["id"], self.task.id)

    def test_get_tasks_defaults_to_empty_list(self):
        self.api.dl.tasks = [self.task]
        self.assertEqual(self.api.get_tasks()[0]["postprocess_errors"], [])
        self.assertIsNone(self.api.get_tasks()[0]["postprocess"])



class RepairPostprocessTest(unittest.TestCase):
    """收尾一键重跑：幂等启动、按 stage 清理错误、有完成标记（自动化据此等待）"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "a.safetensors").write_bytes(b"weights")
        self.api = webui.Api.__new__(webui.Api)
        self.api.cfg = {"gen_metadata": False, "download_cover": False,
                        "archive_comments": False, "update_keep_old": "keep"}
        self.api.log_error = Mock()
        self.api.dl = Mock()
        self.api.dl.history = {}
        self.api.dl.save_tasks = Mock()
        self.api._cache_history_task = Mock()
        self.task = downloader.DownloadTask("https://example.invalid/f", str(self.root),
                                            "a.safetensors", info={"meta": {"info": {"id": 1}}})
        self.task.status = downloader.ST_DONE
        self.api.dl.tasks = [self.task]

    def wait_done(self, timeout=5.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            marker = (self.task.info or {}).get("postprocess_repair") or {}
            if marker.get("done"):
                return marker
            time.sleep(0.02)
        return {}

    def test_repair_runs_and_marks_done(self):
        self.task.info["postprocess_errors"] = [{"stage": "metadata", "error": "old"}]
        out = self.api.repair_download_postprocess(self.task.id, ["metadata"])
        self.assertTrue(out["ok"], out)
        marker = self.wait_done()
        self.assertTrue(marker.get("done"), marker)
        self.assertEqual(self.task.info["postprocess_errors"], [])
        self.assertEqual(marker["stages"], ["metadata"])

    def test_repair_rejects_unknown_task_and_stage(self):
        self.assertFalse(self.api.repair_download_postprocess("nope")["ok"])
        out = self.api.repair_download_postprocess(self.task.id, ["nope"])
        self.assertFalse(out["ok"])
        self.assertIn("metadata", out["hint"])

    def test_repair_rejects_not_done_and_missing_meta(self):
        self.task.status = downloader.ST_ERROR
        self.assertFalse(self.api.repair_download_postprocess(self.task.id)["ok"])
        self.task.status = downloader.ST_DONE
        self.task.info = {}
        out = self.api.repair_download_postprocess(self.task.id)
        self.assertFalse(out["ok"])
        self.assertIn("verify", out["hint"])

    def test_repair_accepts_history_row(self):
        row = self.task.to_dict()
        self.api.dl.tasks = []
        self.api.dl.history = {self.task.id: row}
        out = self.api.repair_download_postprocess(self.task.id, ["metadata"])
        self.assertTrue(out["ok"], out)

    def test_full_postprocess_marks_done_for_callers(self):
        """自动化层靠 postprocess.done 判断「传输完成 + 收尾完成」，没有它就必然假失败。"""
        self.api._handle_dl_done(self.task)
        pp = self.task.info.get("postprocess") or {}
        self.assertTrue(pp.get("done"), pp)
        self.assertIn("finished_at", pp)
        self.assertEqual(pp["stages"], ["metadata", "comments", "cover", "video", "replace_old"])


class UiFlagsTest(unittest.TestCase):
    """配置版本号：前端靠它感知「外部（AI/桥）改了配置」，不必刷新页面"""

    def test_flags_include_rev_and_dialog_keys(self):
        api = webui.Api.__new__(webui.Api)
        api.cfg = {"ask_move_after_download": False, "download_target_dir": "D:/out",
                   "multi_file_download": "first", "route_by_type": True}
        flags = api.get_ui_flags()
        self.assertEqual(flags["cfg_rev"], 0)
        self.assertFalse(flags["ask_move_after_download"])
        self.assertEqual(flags["download_target_dir"], "D:/out")
        self.assertEqual(flags["multi_file_download"], "first")
        self.assertTrue(flags["route_by_type"])

    def test_save_config_bumps_rev(self):
        from unittest.mock import patch
        api = webui.Api.__new__(webui.Api)
        api.cfg = {"max_concurrent_downloads": 2, "models_dirs": []}
        api.dl = Mock()
        with patch.object(webui.config, "save", return_value=True),              patch.object(webui.config, "normalize_ui_preferences"),              patch.object(webui.Api, "_new_api", return_value=Mock()):
            self.assertTrue(api.save_config({"ask_move_after_download": False}))
        self.assertEqual(api.get_ui_flags()["cfg_rev"], 1)
        with patch.object(webui.config, "save", return_value=True),              patch.object(webui.config, "normalize_ui_preferences"),              patch.object(webui.Api, "_new_api", return_value=Mock()):
            api.save_config({"ask_move_after_download": True})
        self.assertEqual(api.get_ui_flags()["cfg_rev"], 2)


if __name__ == "__main__":
    unittest.main()
