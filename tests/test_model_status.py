# -*- coding: utf-8 -*-
"""扩展下载状态聚合（Api.model_status）离线测试。

只构造 Api.__new__ 的进程内状态与临时文件；不启动桥、不联网、不触发真实扫描。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import downloader
import webui


def make_task(mid="", vid="", status=downloader.ST_PENDING, progress=0.0,
              filename="demo.safetensors", dest_dir="D:\\models", url=""):
    info = {}
    if mid:
        info["model_id"] = mid
    if vid:
        info["version_id"] = vid
    return SimpleNamespace(id="t-" + str(vid or mid or "x"), info=info, status=status,
                           progress=progress, filename=filename, dest_dir=dest_dir, url=url)


class FakeDownloader:
    def __init__(self, history=None, tasks=None):
        self._history = list(history or [])
        self.tasks = list(tasks or [])

    def get_history(self):
        return [dict(r) for r in self._history]


class ModelStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = self.root / "app"
        self.app.mkdir()
        self.models = self.root / "models"
        (self.models / "loras").mkdir(parents=True)
        self.patches = [patch.object(config, "APP_DIR", str(self.app))]
        for p in self.patches:
            p.start()
        self.api = webui.Api.__new__(webui.Api)
        self.api.cfg = {"models_dirs": [str(self.models)], "site_domain": "civitai.red"}
        self.api.model_rows = []
        self.api.mm_scan_state = {"running": False, "rows": [], "msg": "扫描完成：0 个模型"}
        self.api._updates = {"checked_at": 0, "items": {}}
        self.api._status_hist_meta = {}
        self.api._status_scan_started_at = time.time()
        self.api.dl = FakeDownloader()
        self.file = self.models / "loras" / "demo.safetensors"
        self.file.write_bytes(b"weights")

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def _row(self, mid="101", vid="1001", path=None):
        return {"path": str(path or self.file), "size": 7, "modelId": mid, "verId": vid,
                "ver": "v1.0", "civitai_name": "Demo", "base": "SDXL", "info": {}}

    def _status(self, items, **kw):
        return self.api.model_status(items, **kw)

    def _one(self, items, **kw):
        result = self._status(items, **kw)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(result["items"]), 1)
        return result["items"][0], result

    def test_exact_library_hit_is_downloaded(self):
        self.api.model_rows = [self._row()]
        item, result = self._one([{"model_id": "101", "version_id": "1001"}])
        self.assertEqual(item["state"], "downloaded")
        self.assertTrue(item["version_matched"])
        self.assertEqual(item["matched"], "version")
        self.assertEqual(item["local"]["path"], str(self.file))
        self.assertTrue(item["local"]["exists"])
        self.assertEqual(item["local"]["version_name"], "v1.0")
        self.assertTrue(result["library"]["ready"])
        self.assertEqual(result["library"]["count"], 1)
        self.assertIsNone(item["update"])

    def test_update_record_turns_downloaded_into_update_available(self):
        self.api.model_rows = [self._row()]
        self.api._updates = {"checked_at": 123, "items": {
            str(self.file): {"model_id": "101", "local_version": "1001", "local_name": "v1.0",
                             "has_update": True, "latest_version": "1002", "latest_name": "v2.0",
                             "url": "https://civitai.red/models/101?modelVersionId=1002",
                             "behind": 1, "checked_at": 120,
                             "newer_list": [{"id": "1002", "name": "v2.0", "date": "2026-10-01",
                                             "url": "https://civitai.red/models/101?modelVersionId=1002"}]}}}
        item, result = self._one([{"model_id": "101", "version_id": "1001"}])
        self.assertEqual(item["state"], "update_available")
        self.assertTrue(item["update"]["has_update"])
        self.assertEqual(item["update"]["latest_version"], "1002")
        self.assertEqual(item["update"]["latest_url"], "https://civitai.red/models/101?modelVersionId=1002")
        self.assertEqual(item["update"]["newer"][0]["id"], "1002")
        self.assertEqual(result["updates"]["checked_at"], 123)
        self.assertEqual(result["updates"]["records"], 1)

    def test_other_local_version_is_reported_honestly(self):
        other = self.models / "loras" / "old.safetensors"
        other.write_bytes(b"old")
        self.api.model_rows = [self._row(vid="1000", path=other)]
        item, _ = self._one([{"model_id": "101", "version_id": "1001"}])
        self.assertEqual(item["state"], "other_version")
        self.assertFalse(item["version_matched"])
        self.assertEqual(item["local_versions"][0]["version_id"], "1000")
        self.assertIn("1001", item["reason"])

    def test_active_exact_task_is_downloading(self):
        self.api.model_rows = []
        self.api.dl = FakeDownloader(tasks=[make_task("101", "1001", downloader.ST_DOWNLOADING, 42.4)])
        item, _ = self._one([{"model_id": "101", "version_id": "1001"}])
        self.assertEqual(item["state"], "downloading")
        self.assertTrue(item["download"]["version_matched"])
        self.assertEqual(item["download"]["progress"], 42.4)

    def test_active_other_version_is_downloading_but_not_matched(self):
        self.api.dl = FakeDownloader(tasks=[make_task("101", "1000", downloader.ST_PENDING, 0)])
        item, _ = self._one([{"model_id": "101", "version_id": "1001"}])
        self.assertEqual(item["state"], "downloading")
        self.assertFalse(item["download"]["version_matched"])
        self.assertEqual(item["matched"], "model")
        self.assertIn("其他版本", item["reason"])

    def test_done_history_with_existing_file_counts_as_downloaded(self):
        self.api.model_rows = []
        self.api.dl = FakeDownloader(history=[{
            "id": "h1", "status": downloader.ST_DONE, "file_path": str(self.file),
            "model_id": "101", "version_id": "1001", "modelName": "Demo", "versionName": "v1.0",
            "finished_at": 111, "total": 7}])
        item, _ = self._one([{"model_id": "101", "version_id": "1001"}])
        self.assertEqual(item["state"], "downloaded")
        self.assertEqual(item["local"]["source"], "history")
        self.assertTrue(item["history"]["file_exists"])

    def test_done_history_missing_file_is_not_downloaded(self):
        gone = self.models / "gone.safetensors"
        self.api.dl = FakeDownloader(history=[{
            "id": "h1", "status": downloader.ST_DONE, "file_path": str(gone),
            "model_id": "101", "version_id": "1001", "finished_at": 111}])
        item, _ = self._one([{"model_id": "101", "version_id": "1001"}])
        self.assertEqual(item["state"], "not_downloaded")
        self.assertIn("文件已不在", item["reason"])
        self.assertFalse(item["history"]["file_exists"])

    def test_old_history_row_backfills_ids_from_snapshot(self):
        self.api.model_rows = []
        self.api.dl = FakeDownloader(history=[{
            "id": "legacy-1", "status": downloader.ST_DONE, "file_path": str(self.file),
            "finished_at": 111}])
        snapshot = Path(self.api._history_asset("legacy-1", ".json"))
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(json.dumps({"id": 101, "name": "Demo", "versionId": 1001,
                                        "version": {"id": 1001, "name": "v1.0", "baseModel": "SDXL"}}),
                            encoding="utf-8")
        item, _ = self._one([{"model_id": "101", "version_id": "1001"}])
        self.assertEqual(item["state"], "downloaded")
        self.assertEqual(item["local"]["version_name"], "v1.0")
        self.assertEqual(item["history"]["task_id"], "legacy-1")

    def test_unscanned_library_reports_unknown_not_not_downloaded(self):
        self.api.model_rows = []
        self.api.mm_scan_state = {"running": False, "rows": [], "msg": ""}
        self.api._status_scan_started_at = time.time()  # 阻止自动扫描，保持“未扫描”场景
        item, result = self._one([{"model_id": "999", "version_id": "9999"}])
        self.assertEqual(item["state"], "unknown")
        self.assertFalse(result["library"]["ready"])
        self.assertIn("未扫描", item["reason"])

    def test_unscanned_library_keeps_positive_history_match(self):
        self.api.model_rows = []
        self.api.mm_scan_state = {"running": False, "rows": [], "msg": ""}
        self.api._status_scan_started_at = time.time()
        self.api.dl = FakeDownloader(history=[{
            "id": "h1", "status": downloader.ST_DONE, "file_path": str(self.file),
            "model_id": "101", "version_id": "1001", "finished_at": 111}])
        item, _ = self._one([{"model_id": "101", "version_id": "1001"}])
        self.assertEqual(item["state"], "downloaded")

    def test_url_forms_are_parsed(self):
        self.api.model_rows = [self._row()]
        item, _ = self._one([{"url": "https://civitai.red/models/101?modelVersionId=1001"}])
        self.assertEqual(item["model_id"], "101")
        self.assertEqual(item["version_id"], "1001")
        self.assertEqual(item["state"], "downloaded")
        item2, _ = self._one([{"url": "https://civitai.red/api/download/models/1001"}])
        self.assertEqual(item2["model_id"], "")
        self.assertEqual(item2["version_id"], "1001")
        self.assertEqual(item2["state"], "downloaded")

    def test_missing_ids_is_unknown_and_does_not_break_batch(self):
        self.api.model_rows = [self._row()]
        result = self._status([{"model_id": "101", "version_id": "1001"}, {"url": "https://example.invalid/x"}])
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"][0]["state"], "downloaded")
        self.assertEqual(result["items"][1]["state"], "unknown")
        self.assertIn("缺少模型 ID", result["items"][1]["reason"])

    def test_model_level_query_aggregates_local_versions(self):
        self.api.model_rows = [self._row()]
        item, _ = self._one([{"model_id": "101"}])
        self.assertEqual(item["state"], "downloaded")
        self.assertEqual(item["matched"], "model")
        self.assertFalse(item["version_matched"])
        self.assertEqual(item["local"]["version_id"], "1001")

    def test_update_unknown_local_does_not_become_update_available(self):
        self.api.model_rows = [self._row()]
        self.api._updates = {"checked_at": 123, "items": {
            str(self.file): {"model_id": "101", "local_version": "1001", "unknown": True,
                             "has_update": False, "latest_version": "1002", "latest_name": "v9",
                             "url": "https://civitai.red/models/101?modelVersionId=1002"}}}
        item, _ = self._one([{"model_id": "101", "version_id": "1001"}])
        self.assertEqual(item["state"], "downloaded")
        self.assertTrue(item["update"]["unknown_local"])
        self.assertFalse(item["update"]["has_update"])

    def test_hash_fallback_hits_library_without_version_ids(self):
        """版本 ID 缺失的侧车：请求带 SHA256 时按内容认出已下载，而不是 unknown/未下载。"""
        sha = "a" * 64
        self.api.model_rows = [{"path": str(self.file), "size": 7, "name": self.file.name,
                                "modelId": "", "verId": "", "ver": "", "civitai_name": "",
                                "base": "", "info": {"files": [{"hashes": {"SHA256": sha.upper()}}]}}]
        item, _ = self._one([{"url": "https://civitai.red/models/101?modelVersionId=1001",
                              "hashes": [sha]}])
        self.assertEqual(item["state"], "downloaded")
        self.assertEqual(item["matched"], "hash")
        self.assertFalse(item["version_matched"])
        self.assertEqual(item["local"]["source"], "library")
        self.assertEqual(item["local"]["path"], str(self.file))
        self.assertIn("SHA256", item["reason"])

    def test_hash_fallback_hits_history_and_reports_existing_file(self):
        sha = "b" * 64
        self.api.dl = FakeDownloader(history=[{
            "id": "hist-1", "status": downloader.ST_DONE, "filename": "renamed.safetensors",
            "dest_dir": str(self.models / "loras"), "file_path": str(self.file),
            "total": 7, "finished_at": 123, "actual_sha256": sha,
            "model_id": "", "version_id": "", "modelName": "Demo", "versionName": "v1.0",
        }])
        item, _ = self._one([{"url": "https://civitai.red/models/101?modelVersionId=1001",
                              "hashes": [sha]}])
        self.assertEqual(item["state"], "downloaded")
        self.assertEqual(item["matched"], "hash")
        self.assertEqual(item["local"]["source"], "history")
        self.assertTrue(item["history"]["file_exists"])

    def test_hash_fallback_requires_existing_file_and_valid_hash(self):
        sha = "c" * 64
        self.api.model_rows = [{"path": str(self.file), "size": 7, "name": self.file.name,
                                "modelId": "", "verId": "", "ver": "", "civitai_name": "",
                                "base": "", "info": {"files": [{"hashes": {"SHA256": sha}}]}}]
        self.file.unlink()
        item, _ = self._one([{"model_id": "101", "version_id": "1001", "hashes": [sha]}])
        self.assertNotEqual(item["matched"], "hash")
        self.assertIn(item["state"], ("not_downloaded", "unknown"))
        item2, _ = self._one([{"model_id": "101", "version_id": "1001", "hashes": ["not-a-sha"]}])
        self.assertNotEqual(item2["matched"], "hash")
        self.assertIn(item2["state"], ("not_downloaded", "unknown"))

    def test_hash_fallback_path_prefers_live_hash_file_over_dead_exact(self):
        """精确版本只剩历史记录（文件已不在）时，local 必须指向真正命中的哈希文件。"""
        sha = "f" * 64
        gone = self.models / "loras" / "gone.safetensors"
        self.api.dl = FakeDownloader(history=[{
            "id": "hist-dead", "status": downloader.ST_DONE, "filename": "gone.safetensors",
            "dest_dir": str(gone.parent), "file_path": str(gone),
            "total": 7, "finished_at": 1, "actual_sha256": "e" * 64,
            "model_id": "101", "version_id": "1001", "modelName": "Demo", "versionName": "v1.0",
        }])
        self.api.model_rows = [{"path": str(self.file), "size": 7, "name": self.file.name,
                                "modelId": "", "verId": "", "ver": "", "civitai_name": "",
                                "base": "", "info": {"files": [{"hashes": {"SHA256": sha}}]}}]
        item, _ = self._one([{"model_id": "101", "version_id": "1001", "hashes": [sha]}])
        self.assertEqual(item["state"], "downloaded")
        self.assertEqual(item["matched"], "hash")
        self.assertEqual(item["local"]["path"], str(self.file))
        self.assertTrue(item["local"]["exists"])

    def test_hash_fallback_does_not_borrow_other_file_in_multifile_sidecar(self):
        """多文件版本：另一个文件的 SHA256 不能证明这条路径已下载。"""
        mine, other = "a" * 64, "b" * 64
        self.api.model_rows = [{"path": str(self.file), "size": 7, "name": self.file.name,
                                "modelId": "", "verId": "", "ver": "", "civitai_name": "",
                                "base": "", "info": {"files": [
                                    {"name": self.file.name, "hashes": {"SHA256": mine}},
                                    {"name": "other.safetensors", "hashes": {"SHA256": other}},
                                ]}}]
        item, _ = self._one([{"model_id": "101", "version_id": "1001", "hashes": [other]}])
        self.assertNotEqual(item["matched"], "hash", item)
        item2, _ = self._one([{"model_id": "101", "version_id": "1001", "hashes": [mine]}])
        self.assertEqual(item2["matched"], "hash")
        self.assertEqual(item2["local"]["path"], str(self.file))

    def test_hash_does_not_override_exact_version_hit(self):
        other = self.models / "loras" / "other.safetensors"
        other.write_bytes(b"other")
        self.api.model_rows = [
            self._row(),
            {"path": str(other), "size": 5, "name": other.name, "modelId": "", "verId": "",
             "ver": "", "civitai_name": "", "base": "",
             "info": {"files": [{"hashes": {"SHA256": "d" * 64}}]}},
        ]
        item, _ = self._one([{"model_id": "101", "version_id": "1001", "hashes": ["d" * 64]}])
        self.assertEqual(item["matched"], "version")
        self.assertTrue(item["version_matched"])
        self.assertEqual(item["local"]["path"], str(self.file))


if __name__ == "__main__":
    unittest.main()
