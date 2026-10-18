# -*- coding: utf-8 -*-
"""清理下载历史记录：只删记录与历史缓存，绝不删模型文件。

钉住三件事：
1) 只删 history 字典是不够的——队列里的同 id 终态任务会在下一次 save_tasks 时把记录写回来，
   所以必须同时摘掉队列行（而下载中的任务要跳过，不做半截删除）。
2) 物理文件（模型、.part）一律不动：清理记录与删除模型是两件正交的事。
3) 幂等：未知 id 进 missing，重复调用不报错；preview 不落盘。
"""
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import automation_api, config, downloader, webui


def make_downloader(root, history_rows=None):
    with patch.object(config, "load_history", return_value=list(history_rows or [])):
        dl = downloader.Downloader({"download_dir": str(root), "max_concurrent_downloads": 1})
    dl.save_tasks = Mock(return_value=True)
    dl._ensure_workers = Mock()
    return dl


def row(task_id, filename="a.safetensors", status="done"):
    return {"id": task_id, "filename": filename, "status": status,
            "dest_dir": "", "file_path": "", "finished_at": 1}


class DropHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_removes_record_and_terminal_task_keeps_file(self):
        model = self.root / "a.safetensors"
        model.write_bytes(b"weights")
        dl = make_downloader(self.root, [row("h1")])
        task = downloader.DownloadTask("https://example.invalid/f", str(self.root), "a.safetensors")
        task.status = downloader.ST_DONE
        dl.tasks.append(task)
        dl.history[task.id] = row(task.id)

        out = dl.drop_history([task.id])

        self.assertTrue(out["ok"], out)
        self.assertEqual(out["removed"], [task.id])
        self.assertEqual(out["tasks_removed"], [task.id])
        self.assertNotIn(task.id, dl.history)
        self.assertEqual(dl.tasks, [], "队列行必须一并摘掉，否则下次保存会把记录写回来")
        self.assertTrue(model.exists(), "清理记录绝不能删模型文件")
        self.assertTrue(dl.save_tasks.called)

    def test_history_only_row_without_task(self):
        dl = make_downloader(self.root, [row("h1"), row("h2")])
        out = dl.drop_history(["h1"])
        self.assertEqual(out["removed"], ["h1"])
        self.assertEqual(out["tasks_removed"], [])
        self.assertIn("h2", dl.history)

    def test_active_task_is_skipped(self):
        dl = make_downloader(self.root)
        task = downloader.DownloadTask("https://example.invalid/f", str(self.root), "a.safetensors")
        task.status = downloader.ST_DOWNLOADING
        dl.tasks.append(task)
        dl.history[task.id] = row(task.id)
        out = dl.drop_history([task.id])
        self.assertFalse(out["ok"])
        self.assertEqual(out["skipped"][0]["id"], task.id)
        self.assertIn(task.id, dl.history)
        self.assertEqual(len(dl.tasks), 1)

    def test_unknown_id_is_reported_not_raised(self):
        dl = make_downloader(self.root, [row("h1")])
        out = dl.drop_history(["nope", "h1", "h1"])
        self.assertEqual(out["removed"], ["h1"], "重复 id 只处理一次")
        self.assertEqual(out["missing"], ["nope"])
        self.assertEqual(dl.save_tasks.call_count, 1, "有删除才落盘一次")

    def test_empty_ids_rejected(self):
        dl = make_downloader(self.root)
        out = dl.drop_history([])
        self.assertFalse(out["ok"])
        self.assertIn("msg", out)

    def test_failed_task_partial_is_cleaned_with_its_record(self):
        """删除一条失败记录 = 放弃续传上下文：它的 .part 不该变成无人引用的孤儿。"""
        dl = make_downloader(self.root)
        task = downloader.DownloadTask("https://example.invalid/f", str(self.root), "a.safetensors")
        task.status = downloader.ST_ERROR
        part = Path(str(self.root / "a.safetensors") + ".part")
        part.write_bytes(b"half")
        task._partial_path = str(part.resolve())
        dl.tasks.append(task)
        dl.history[task.id] = row(task.id, status="error")

        out = dl.drop_history([task.id])

        self.assertEqual(out["parts_cleaned"], 1, out)
        self.assertFalse(part.exists())
        self.assertFalse((self.root / "a.safetensors").exists(), "清理记录不能凭空造出或删掉本体")
        self.assertNotIn(task.id, dl.history)

    def test_partial_cleanup_pending_row_is_honoured(self):
        """历史行里登记的待清理 .part：删记录时必须顺手清掉，否则兜底义务随记录一起消失。"""
        part = self.root / "a.safetensors.part"
        part.write_bytes(b"half")
        pending = row("h1", status="canceled")
        pending.update(dest_dir=str(self.root), file_path=str(self.root / "a.safetensors"),
                       partial_cleanup_pending=True, removed_partial_path=str(part.resolve()))
        dl = make_downloader(self.root, [pending])

        out = dl.drop_history(["h1"])

        self.assertEqual(out["parts_cleaned"], 1, out)
        self.assertFalse(part.exists())

    def test_parts_cleaned_counts_real_deletions_only(self):
        """多条记录可能指向同一个 .part（同一模型反复取消）：计数不能按调用次数虚报。"""
        shared = str(self.root / "a.safetensors.part")
        rows = []
        for key in ("h1", "h2"):
            r = row(key, status="canceled")
            r.update(dest_dir=str(self.root), file_path=str(self.root / "a.safetensors"),
                     partial_cleanup_pending=True, removed_partial_path=shared)
            rows.append(r)
        dl = make_downloader(self.root, rows)

        out = dl.drop_history(["h1", "h2"])

        self.assertEqual(out["removed"], ["h1", "h2"])
        self.assertEqual(out["parts_cleaned"], 0, "文件本来就不存在，不能报成清理了 2 个")

    def test_late_notify_cannot_resurrect_deleted_record(self):
        """记录已删，但该任务的 _notify 还在路上：_record_history 必须跳过它。"""
        dl = make_downloader(self.root)
        task = downloader.DownloadTask("https://example.invalid/f", str(self.root), "a.safetensors")
        task.status = downloader.ST_DONE
        dl.tasks.append(task)
        dl.history[task.id] = row(task.id)
        dl.drop_history([task.id])

        dl._record_history(task)          # 模拟 in-flight _notify 的写入
        self.assertNotIn(task.id, dl.history)

    def test_persisted_deletion_does_not_come_back(self):
        """真实落盘往返：删除后重新加载，记录不能复活（不 Mock save_tasks）。"""
        data_dir = self.root / "appdata"
        data_dir.mkdir()
        old = (config.APP_DIR, config.HISTORY_PATH, config.TASKS_PATH, config.CONFIG_PATH)
        self.addCleanup(lambda: config.use_data_directory(old[0]))
        config.use_data_directory(str(data_dir))
        try:
            config.save_history([row("h1"), row("h2")])
            dl = downloader.Downloader({"download_dir": str(self.root), "max_concurrent_downloads": 1})
            dl._ensure_workers = Mock()
            out = dl.drop_history(["h1"])
            self.assertEqual(out["removed"], ["h1"])
            reloaded = {r["id"] for r in config.load_history()}
            self.assertEqual(reloaded, {"h2"})
        finally:
            config.use_data_directory(old[0])


class ApiHistoryCleanupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.assets = self.root / "history_assets"
        self.assets.mkdir()
        self.api = webui.Api.__new__(webui.Api)
        self.api.cfg = {}
        self.api.log_error = Mock()
        self.api.dl = make_downloader(self.root, [row("h1"), row("h2")])
        self.api._history_asset = lambda tid, suffix: str(self.assets / (tid + suffix))
        self.api._history_migration_started = True   # 别让测试触发后台迁移线程

    def touch_assets(self, tid):
        for suffix in (".json", ".jpg"):
            (self.assets / (tid + suffix)).write_bytes(b"x")

    def test_removes_records_and_assets_keeps_model(self):
        model = self.root / "a.safetensors"
        model.write_bytes(b"weights")
        self.touch_assets("h1")
        self.touch_assets("h2")
        out = self.api.remove_download_history("h1")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["assets_removed"], 2)
        self.assertIn("模型文件保留", out["msg"])
        self.assertFalse((self.assets / "h1.json").exists())
        self.assertTrue((self.assets / "h2.jpg").exists())
        self.assertTrue(model.exists())

    def test_preview_does_not_delete(self):
        self.touch_assets("h1")
        out = self.api.remove_download_history(ids=["h1", "ghost"], preview=True)
        self.assertTrue(out["preview"])
        self.assertEqual(out["ids"], ["h1"])
        self.assertEqual(out["unknown"], ["ghost"])
        self.assertIn("h1", self.api.dl.history)
        self.assertTrue((self.assets / "h1.jpg").exists())

    def test_clear_all(self):
        out = self.api.remove_download_history(all=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(sorted(out["removed"]), ["h1", "h2"])
        self.assertEqual(self.api.dl.history, {})

    def test_empty_request_is_rejected(self):
        out = self.api.remove_download_history()
        self.assertFalse(out["ok"])
        self.assertIn("hint", out)

    def test_clear_all_on_empty_history_is_success(self):
        """已经空了不是错误：否则 automation 的 clear 会回一个自相矛盾的失败。"""
        self.api.dl.history.clear()
        out = self.api.remove_download_history(all=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["removed"], [])
        self.assertIn("已经是空", out["msg"])

    def test_preview_excludes_active_tasks(self):
        """仍在下载的任务不算历史：preview 不能承诺一条实际删不掉的记录。"""
        task = downloader.DownloadTask("https://example.invalid/f", str(self.root), "b.safetensors")
        task.status = downloader.ST_DOWNLOADING
        self.api.dl.tasks.append(task)
        self.api.dl.history[task.id] = row(task.id)
        out = self.api.remove_download_history(ids=[task.id], preview=True)
        self.assertEqual(out["ids"], [], out)
        self.assertEqual(out["unknown"], [task.id])

    def test_asset_delete_failure_is_logged_not_fatal(self):
        self.touch_assets("h1")
        with patch("os.unlink", side_effect=OSError("locked")):
            out = self.api.remove_download_history("h1")
        self.assertTrue(out["ok"])
        self.assertEqual(out["assets_removed"], 0)
        self.assertTrue(self.api.log_error.called)


class HistoryActionTest(unittest.TestCase):
    """自动化层：action 白名单、ids 校验、preview 透传。"""

    def setUp(self):
        self.api = Mock()
        self.api.remove_download_history = Mock(return_value={"ok": True, "removed": ["h1"]})
        self.ctx = {"version": "test"}

    def call(self, body):
        return automation_api.dispatch("/api/automation/history", "POST", {}, body,
                                       self.api, self.ctx)

    def test_remove_passes_ids(self):
        status, out = self.call({"action": "remove", "ids": ["h1"]})
        self.assertEqual(status, 200, out)
        self.api.remove_download_history.assert_called_once_with(ids=["h1"], preview=False)

    def test_clear_maps_to_all(self):
        status, out = self.call({"action": "clear"})
        self.assertEqual(status, 200, out)
        self.api.remove_download_history.assert_called_once_with(all=True, preview=False)

    def test_remove_without_ids_400(self):
        status, out = self.call({"action": "remove"})
        self.assertEqual(status, 400)
        self.assertIn("hint", out)

    def test_bad_action_and_bad_ids(self):
        self.assertEqual(self.call({"action": "drop"})[0], 400)
        self.assertEqual(self.call({"action": "remove", "ids": "h1"})[0], 400)

    def test_preview_flag_forwarded(self):
        self.call({"action": "remove", "ids": ["h1"], "preview": True})
        self.api.remove_download_history.assert_called_once_with(ids=["h1"], preview=True)

    def test_missing_capability_501(self):
        del self.api.remove_download_history
        self.api.remove_download_history = None
        status, out = self.call({"action": "clear"})
        self.assertEqual(status, 501, out)


if __name__ == "__main__":
    unittest.main()
