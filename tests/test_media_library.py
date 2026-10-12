# -*- coding: utf-8 -*-
"""media_library 单测：TemporaryDirectory + patch config.APP_DIR，全程无网络。

元数据提取一律通过 patch `media_library._extract` 注入（契约 §12），因此本文件不依赖 media_meta 是否已存在。
"""
import contextlib
import json
import os
import shutil
import sqlite3
import sys
import time
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import media_library

DEFAULT_META = {
    "has_meta": True, "source": "png_text", "prompt": "1girl, solo, kitten", "negative": "lowres, bad",
    "note": "", "checkpoint": "ckA.safetensors", "loras": ["loraA.safetensors"],
    "models": ["ckA.safetensors", "loraA.safetensors"], "sampler": "DPM++ 2M", "scheduler": "karras",
    "seed": 123, "steps": 20, "cfg": 4.5, "width": 1024, "height": 1024, "duration": None,
    "fps": None,                                 # 只有视频有（media_meta §15.1.5），图片恒为 None
    "extra": {"format": "png"},
}

REAL_EXTRACT = media_library._extract      # 夹具会替换模块属性，这里留住原函数用于直测


class ReadOnlyDb(object):
    """只读直连：断言落库真相，绝不经过被测类的连接封装。"""

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.conn = sqlite3.connect(self.path, timeout=15)
        self.conn.row_factory = sqlite3.Row
        return self.conn

    def __exit__(self, *exc):
        self.conn.commit()
        self.conn.close()
        return False


class LibraryCase(unittest.TestCase):
    """公共夹具：临时 app 目录 + 临时扫描根 + 可控 _extract（记录调用、按文件名覆盖元数据）。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        base = Path(self._tmp.name)
        self.app_dir = base / "app"
        self.app_dir.mkdir()
        self.root = base / "output"
        self.root.mkdir()
        self.roots = [str(self.root)]
        self.meta_map = {}
        self.calls = []
        self.extract_hook = None
        self.use_patch(patch.object(config, "APP_DIR", str(self.app_dir)))
        self.use_patch(patch.object(media_library, "_extract", side_effect=self._fake_extract))
        self.addCleanup(self._tmp.cleanup)
        self.lib = media_library.MediaLibrary()

    def use_patch(self, mocker):
        mocker.start()
        self.addCleanup(mocker.stop)
        return mocker

    def _fake_extract(self, path, ext=None):
        if self.extract_hook:
            self.extract_hook(path, ext)              # 测试用：抛异常模拟解析崩溃
        self.calls.append(os.path.normcase(os.path.abspath(str(path))))
        meta = dict(DEFAULT_META)
        meta.update(self.meta_map.get(os.path.basename(str(path)), {}))
        return meta

    def make(self, rel, content=b"payload", mtime=None):
        path = self.root / rel.replace("/", os.sep)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if mtime is None:
            mtime = time.time() - 5000                # 明确早于解析时刻，避免"未来 mtime"引起误重解析
        os.utime(path, (mtime, mtime))
        return path

    def scan(self, exclude=(), **kw):
        return self.lib.scan(self.roots, exclude=exclude, **kw)

    def raw(self, relpath):
        with ReadOnlyDb(self.lib.path) as conn:
            row = conn.execute("SELECT * FROM items WHERE relpath = ?", (relpath,)).fetchone()
            return dict(row) if row is not None else None

    def rel(self, flt):
        return sorted(i["relpath"] for i in self.lib.list_items(flt)["items"])

    def ids(self, flt):
        return [i["id"] for i in self.lib.list_items(flt)["items"]]

    def by_rel(self, flt=None):
        flt = dict({"limit": 500}, **(flt or {}))
        return {i["relpath"]: i for i in self.lib.list_items(flt)["items"]}

    def poster_flag(self, path):
        """白盒读 has_poster 列：库侧标记（能否直出缩略图由文件系统上的海报缓存决定，见 A3 的 _lib_enrich）。"""
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.lib.path)) as conn:   # 必须关连接，Windows 上残留句柄会锁住临时目录删除
            row = conn.execute("SELECT has_poster FROM items WHERE path = ?", (path,)).fetchone()
        return bool(row and row[0])


class SchemaTests(LibraryCase):
    def test_db_path_and_poster_path_follow_patched_app_dir(self):
        self.assertEqual(media_library.db_path(), str(self.app_dir / "media_library.db"))
        self.assertEqual(self.lib.path, media_library.db_path())
        poster = media_library.poster_path(str(self.root / "a.mp4"))
        self.assertTrue(poster.startswith(str(self.app_dir)))
        self.assertEqual(poster, media_library.poster_path(os.path.abspath(str(self.root / "a.mp4"))))
        self.assertNotEqual(poster, media_library.poster_path(str(self.root / "b.mp4")))
        self.assertFalse(os.path.exists(poster))

    def test_schema_version_wal_indexes_and_kind_check(self):
        with ReadOnlyDb(self.lib.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
            columns = {r["name"]: (r["type"], bool(r["notnull"]), r["dflt_value"])
                       for r in conn.execute("PRAGMA table_info(items)")}
            index_sql = {r["name"]: (r["sql"] or "") for r in conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'index'")}
        for name in ("items", "tags", "item_tags", "meta", "ix_items_kind", "ix_items_ck", "ix_items_dir",
                     "ix_items_star", "ix_items_mtime", "ix_items_missing", "ix_items_loras",
                     "ix_items_sampler", "ix_items_scheduler", "ix_item_tags_tag"):
            self.assertIn(name, names)
        # §15.2.1 钉死的确切类型与默认值（照抄，不自行平衡）
        for name in ("seed", "steps"):
            self.assertEqual(columns[name], ("INTEGER", False, None))
        for name in ("cfg", "fps"):
            self.assertEqual(columns[name], ("REAL", False, None))
        for name in ("sampler", "scheduler"):
            self.assertEqual(columns[name], ("TEXT", True, "''"))
        if self.lib._fts_ok:
            self.assertIn("items_fts", names)
        # §15.2.3：两个新索引都是 partial（只覆盖在场文件）
        for name in ("ix_items_sampler", "ix_items_scheduler"):
            self.assertIn("WHERE missing_since IS NULL", index_sql[name])
        self.make("a.png")
        self.scan()
        with ReadOnlyDb(self.lib.path) as conn:
            with self.assertRaises(sqlite3.IntegrityError):        # CHECK (kind IN ('image','video'))
                conn.execute("INSERT INTO items(path, root_id, relpath, dir_path, filename, ext, kind, size, "
                             "mtime, meta_mtime, added_at) VALUES('p','r','p','','p','.wav','audio',1,1,1,1)")

    def test_fts_flag_is_detected_at_schema_creation(self):
        self.assertIsInstance(media_library.FTS_AVAILABLE, bool)
        self.assertEqual(media_library.FTS_AVAILABLE, self.lib._fts_ok)
        self.assertEqual(self.lib.meta_value("fts"), "1" if self.lib._fts_ok else "0")

    def test_media_meta_is_not_imported_at_module_import_time(self):
        source = Path(media_library.__file__).read_text(encoding="utf-8")
        self.assertIn("import media_meta", source)                  # _extract 函数体内延迟 import
        self.assertNotIn("\nimport media_meta", source)             # 但绝不在模块顶层

    def test_extract_indirection_swallows_every_failure(self):
        broken = types.ModuleType("media_meta")                     # 没有 extract → AttributeError
        with patch.dict(sys.modules, {"media_meta": broken}):
            self.assertEqual(REAL_EXTRACT("x.png", ".png"), {})

        def boom(*args, **kwargs):
            raise RuntimeError("解析器炸了")

        exploding = types.ModuleType("media_meta")
        exploding.extract = boom
        with patch.dict(sys.modules, {"media_meta": exploding}):
            self.assertEqual(REAL_EXTRACT("x.png", ".png"), {})

        returning_none = types.ModuleType("media_meta")
        returning_none.extract = lambda *a, **kw: None
        with patch.dict(sys.modules, {"media_meta": returning_none}):
            self.assertEqual(REAL_EXTRACT("x.png", ".png"), {})

        # 抛 KeyError/自定义异常也必须被吞掉（不只是 ImportError）
        class Evil(types.ModuleType):
            def __getattr__(self, item):
                raise KeyError(item)

        with patch.dict(sys.modules, {"media_meta": Evil("media_meta")}):
            self.assertEqual(REAL_EXTRACT("x.png", ".png"), {})

    def test_extract_returns_empty_dict_when_media_meta_absent(self):
        # A1 可能还没交付：media_meta 不可导入时同样不得把异常抛给调用方
        with patch("builtins.__import__", side_effect=ImportError("没有 media_meta")):
            self.assertEqual(REAL_EXTRACT(str(self.root / "missing.png"), ".png"), {})

    def test_spec_numbers_are_written_as_contracted(self):
        self.assertEqual(media_library.FACET_LIMIT, 300)
        self.assertEqual(media_library.SCHEMA_VERSION, 2)                 # §15.2.1 PRAGMA user_version = 2
        self.assertEqual(media_library.PAGE_SIZE_DEFAULT, 200)
        self.assertEqual(media_library.PAGE_SIZE_MAX, 500)
        self.assertEqual(media_library.THUMB_BATCH_MAX, 40)
        self.assertEqual(media_library.MISSING_PURGE_DAYS, 30)
        self.assertEqual(media_library.SNIPPET_LEN, 120)
        self.assertEqual(media_library.ERRORS_MAX, 200)
        self.assertEqual(media_library.DB_NAME, "media_library.db")
        self.assertEqual(media_library.IMAGE_EXTS, (".png", ".jpg", ".jpeg", ".webp", ".gif"))
        self.assertEqual(media_library.VIDEO_EXTS, (".mp4", ".mov", ".webm", ".mkv"))
        self.assertEqual(sorted(media_library.SORT_SQL.keys()),
                         ["added_desc", "name_asc", "size_desc", "time_asc", "time_desc"])
        self.assertEqual(sorted(media_library.KINDS.keys()), ["image", "video"])


class ScanTests(LibraryCase):
    def test_first_scan_stores_normalized_relpath_and_dir_path(self):
        self.make("a.png")
        self.make("sub/deep/b.mp4")
        result = self.scan()
        self.assertEqual(result["added"], 2)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["roots"][0]["status"], "ok")
        self.assertEqual(result["roots"][0]["count"], 2)
        self.assertEqual(len(self.calls), 2)
        row = self.raw("sub/deep/b.mp4")
        self.assertEqual(row["dir_path"], "sub/deep")
        self.assertEqual(row["relpath"], "sub/deep/b.mp4")
        self.assertEqual(row["filename"], "b.mp4")
        self.assertEqual(row["kind"], "video")
        self.assertEqual(row["ext"], ".mp4")
        self.assertEqual(row["root_id"], media_library._root_id(str(self.root)))
        self.assertNotIn("\\", row["relpath"])
        self.assertNotIn("\\", row["dir_path"])
        self.assertTrue(os.path.isabs(row["path"]))
        root_row = self.raw("a.png")
        self.assertEqual(root_row["dir_path"], "")                   # 根目录 = 空串（§3.2 注释）
        self.assertEqual(root_row["kind"], "image")
        self.assertIsNone(root_row["missing_since"])
        self.assertGreaterEqual(root_row["meta_mtime"], root_row["mtime"])
        self.assertGreater(root_row["added_at"], 0)
        self.assertEqual(root_row["prompt"], DEFAULT_META["prompt"])
        self.assertEqual(json.loads(root_row["loras_json"]), ["loraA.safetensors"])

    def test_second_scan_of_unchanged_files_reparses_nothing(self):
        for name in ("a.png", "b.png", "sub/c.mp4"):
            self.make(name)
        self.scan()
        self.assertEqual(len(self.calls), 3)
        result = self.scan()
        self.assertEqual(len(self.calls), 3)                          # 一个字节都没多解析
        self.assertEqual((result["added"], result["updated"], result["missing"]), (0, 0, 0))
        self.assertEqual(result["unchanged"], 3)

    def test_mtime_and_size_change_trigger_reparse(self):
        path = self.make("a.png")
        self.scan()
        self.assertEqual(len(self.calls), 1)
        os.utime(path, (time.time() + 300, time.time() + 300))
        result = self.scan()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual((result["updated"], result["added"]), (1, 0))
        path.write_bytes(b"much bigger content")                      # 尺寸变化
        result = self.scan()
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(self.raw("a.png")["size"], len(b"much bigger content"))

    def test_reparse_preserves_added_at_and_starred(self):
        self.meta_map["a.png"] = {"prompt": "old prompt"}
        path = self.make("a.png")
        self.scan()
        first = self.raw("a.png")
        self.lib.set_star(first["id"], True)
        self.meta_map["a.png"] = {"prompt": "new prompt", "checkpoint": "ckB.safetensors"}
        os.utime(path, (time.time() + 400, time.time() + 400))
        self.scan()
        after = self.raw("a.png")
        self.assertEqual(after["prompt"], "new prompt")
        self.assertEqual(after["checkpoint"], "ckB.safetensors")
        self.assertEqual(after["starred"], 1)                         # 收藏不被重解析冲掉
        self.assertEqual(after["added_at"], first["added_at"])

    def test_deleted_then_reappeared_file_unhides_without_reparse(self):
        path = self.make("a.png")
        original_mtime = os.stat(path).st_mtime
        self.scan()
        calls_after_first = len(self.calls)
        os.remove(path)
        result = self.scan()
        self.assertEqual(result["missing"], 1)
        self.assertEqual(result["unchanged"], 0)
        self.assertIsNotNone(self.raw("a.png")["missing_since"])
        self.assertEqual(self.lib.list_items({})["total"], 0)          # 缺失项不进网格
        self.assertEqual(self.lib.facets()["missing"], 1)
        hidden_added_at = self.raw("a.png")["added_at"]
        path.write_bytes(b"payload")
        os.utime(path, (original_mtime, original_mtime))
        result = self.scan()
        self.assertEqual(len(self.calls), calls_after_first)           # 重新出现绝不重解析
        row = self.raw("a.png")
        self.assertIsNone(row["missing_since"])
        self.assertEqual(row["prompt"], DEFAULT_META["prompt"])        # 元数据原样保留
        self.assertEqual(row["added_at"], hidden_added_at)
        self.assertEqual(result["missing"], 0)
        self.assertEqual(self.lib.list_items({})["total"], 1)

    def test_unreadable_directory_leaves_its_rows_untouched(self):
        locked = self.root / "locked"
        self.make("locked/x.png")
        self.make("other/y.png")
        self.scan()
        os.remove(self.root / "other" / "y.png")
        real_scandir = os.scandir

        def guarded(path, *args, **kwargs):
            if os.path.normcase(str(path)) == os.path.normcase(str(locked)):
                raise PermissionError(13, "denied")
            return real_scandir(path, *args, **kwargs)

        with patch.object(media_library.os, "scandir", side_effect=guarded):
            result = self.scan()
        self.assertIsNone(self.raw("locked/x.png")["missing_since"])   # 没成功枚举就不许判缺失
        self.assertIsNotNone(self.raw("other/y.png")["missing_since"])
        self.assertEqual(result["missing"], 1)
        self.assertTrue(any("无法读取目录" in e for e in result["errors"]))

    def test_deleted_directory_rows_are_marked_missing(self):
        self.make("gone/a.png")
        self.scan()
        os.remove(self.root / "gone" / "a.png")
        os.rmdir(self.root / "gone")
        result = self.scan()
        self.assertEqual(result["missing"], 1)
        self.assertIsNotNone(self.raw("gone/a.png")["missing_since"])

    def test_skip_hidden_pycache_images_node_modules_and_excluded_dirs(self):
        self.make("keep/a.png")
        self.make(".hidden/b.png")
        self.make("__pycache__/c.png")
        self.make("sidecar.images/d.png")
        self.make("skipme/e.png")
        self.make("skipme/deep/f.png")
        self.make("node_modules/g.png")
        self.make("keep/notmedia.txt")
        result = self.scan(exclude=["skipme"])
        self.assertEqual(self.rel({"limit": 500}), ["keep/a.png"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(self.lib.list_items({})["total"], 1)

    def test_newly_excluded_dir_marks_existing_rows_missing(self):
        self.make("skipme/a.png")
        self.scan()
        self.assertEqual(self.lib.list_items({})["total"], 1)
        self.scan(exclude=["skipme"])
        self.assertEqual(self.lib.facets()["missing"], 1)              # 目录被排除 = 不再属于库

    def test_cancel_stops_at_file_boundary_and_keeps_inserted_rows(self):
        for index in range(8):
            self.make("f%02d.png" % index)

        def cancel():
            return len(self.calls) >= 3

        result = self.scan(cancel=cancel)
        self.assertEqual(len(self.calls), 3)                            # 到文件边界即退出
        self.assertEqual(result["added"], 3)
        self.assertEqual(self.lib.list_items({})["total"], 3)           # 已入库内容保留
        self.assertEqual(result["missing"], 0)                           # 部分扫描绝不判缺失
        self.assertEqual(self.lib.last_scan_at(), 0.0)                   # 被取消的扫描不算成功

    def test_cancel_during_walk_marks_nothing_and_keeps_rows(self):
        self.make("a.png")
        self.scan()
        stamped_at = self.lib.last_scan_at()
        os.remove(self.root / "a.png")
        result = self.scan(cancel=lambda: True)
        self.assertEqual(result["missing"], 0)
        self.assertEqual(result["roots"][0]["status"], "cancelled")
        self.assertIsNotNone(self.raw("a.png"))
        self.assertIsNone(self.raw("a.png")["missing_since"])
        self.assertEqual(self.lib.last_scan_at(), stamped_at)                  # 取消的扫描不刷新扫描时间

    def test_progress_phases_and_callback_failure(self):
        self.make("a.png")
        events = []

        def progress(evt):
            events.append(evt)
            raise RuntimeError("回调自己炸了")                            # 回调异常不得拖垮扫描

        result = self.scan(progress=progress)
        self.assertEqual(result["added"], 1)
        phases = [e["phase"] for e in events]
        self.assertTrue(set(phases) <= {"walk", "parse", "reconcile", "done"})
        self.assertIn("parse", phases)
        self.assertIn("done", phases)
        for evt in events:
            self.assertEqual(sorted(evt.keys()), ["done", "msg", "phase", "total"])

    def test_extract_failure_is_recorded_and_scan_continues(self):
        def hook(path, ext):
            raise RuntimeError("坏文件")

        self.extract_hook = hook
        self.make("bad.png")
        self.make("good.png")
        result = self.scan()
        self.assertEqual(result["added"], 2)                              # 解析失败也要入库（只是没元数据）
        self.assertTrue(any("元数据解析异常" in e for e in result["errors"]))
        self.assertEqual(self.raw("bad.png")["has_meta"], 0)
        self.assertEqual(self.lib.facets()["no_meta"], 2)

    def test_non_dict_extract_result_is_recorded_as_error(self):
        self.make("weird.png")
        with patch.object(media_library, "_extract", return_value="不是 dict"):
            result = self.scan()
        self.assertTrue(any("返回类型不是 dict" in e for e in result["errors"]))
        self.assertEqual(result["added"], 1)

    def test_unreachable_root_is_reported_and_rows_untouched(self):
        self.make("a.png")
        self.scan()
        missing_root = str(self.root / "does-not-exist")
        result = self.lib.scan([str(self.root), missing_root])
        statuses = {r["path"]: r["status"] for r in result["roots"]}
        self.assertEqual(statuses[missing_root], "unreachable")
        self.assertTrue(any("unreachable" in e for e in result["errors"]))
        self.assertIsNone(self.raw("a.png")["missing_since"])
        self.assertEqual(self.lib.list_items({})["total"], 1)

    def test_multiple_roots_are_indexed_separately(self):
        second = self.root.parent / "second"
        second.mkdir()
        (second / "x.png").write_bytes(b"payload")
        self.make("a.png")
        result = self.lib.scan([str(self.root), str(second)])
        self.assertEqual(result["added"], 2)
        self.assertEqual(len(result["roots"]), 2)
        self.assertEqual(self.rel({"limit": 500}), ["a.png", "x.png"])
        self.assertEqual({r["root_id"] for r in
                          [self.raw("a.png"), self.raw("x.png")]},
                         {media_library._root_id(str(self.root)), media_library._root_id(str(second))})

    def test_errors_are_capped_and_folded(self):
        sink = []
        errors = media_library._ErrorSink(sink)
        for index in range(media_library.ERRORS_MAX + 50):
            errors.add("错误 %d" % index)
        errors.finish()
        self.assertEqual(len(sink), media_library.ERRORS_MAX + 1)
        self.assertIn("折叠", sink[-1])

    def test_scan_fold_notice_survives_the_cap(self):
        """250 条解析错误：明细被折到 ERRORS_MAX 条，折叠说明必须是最后一条且不被截掉（§4.5）。"""
        def hook(path, ext):
            raise RuntimeError("坏文件")

        self.extract_hook = hook
        for index in range(250):
            self.make("e%03d.png" % index)
        result = self.scan()
        errs = result["errors"]
        self.assertEqual(result["added"], 250)                              # 出错不影响入库
        self.assertLessEqual(len(errs), media_library.ERRORS_MAX + 1)
        self.assertEqual(len(errs), media_library.ERRORS_MAX + 1)
        self.assertIn("折叠", errs[-1])
        self.assertNotIn("折叠", errs[0])

    def test_no_fold_notice_when_errors_fit_the_cap(self):
        def hook(path, ext):
            if os.path.basename(str(path)) == "bad.png":
                raise RuntimeError("坏文件")

        self.extract_hook = hook
        self.make("bad.png")
        self.make("ok.png")
        result = self.scan()
        self.assertEqual(len(result["errors"]), 1)
        self.assertNotIn("折叠", " ".join(result["errors"]))

    def test_nested_duplicate_root_is_skipped_and_never_clobbers_rows(self):
        """items.path 是 UNIQUE：内层根对同一路径走 INSERT OR REPLACE，会把外层根写入的
        added_at/starred 清零（§3.2 的「重解析不动收藏」只管 UPDATE 路径）→ 剪掉重叠根。"""
        self.make("a.png")
        self.make("sub/deep.png")
        self.scan()
        outer = self.lib.scan([str(self.root)])                             # 单根扫描：无剪枝
        self.assertEqual(outer["skipped_roots"], [])
        row = self.raw("sub/deep.png")
        self.lib.set_star(row["id"], True)
        added_at = self.raw("sub/deep.png")["added_at"]
        inner = str(self.root / "sub")
        result = self.lib.scan([str(self.root), inner])
        self.assertEqual([s["path"] for s in result["skipped_roots"]], [inner])
        self.assertEqual(result["skipped_roots"][0]["inside"], str(self.root))
        self.assertEqual([r["path"] for r in result["roots"]], [str(self.root)])   # 被剪的根不再出现在根清单
        self.assertEqual(result["added"], 0)
        after = self.raw("sub/deep.png")
        self.assertEqual(after["starred"], 1)                                # 收藏没被内层根冲掉
        self.assertEqual(after["added_at"], added_at)
        self.assertEqual(self.raw("a.png")["starred"], 0)
        # 反过来传（内层在前）也剪内层：外层根始终保留
        result2 = self.lib.scan([inner, str(self.root)])
        self.assertEqual([s["path"] for s in result2["skipped_roots"]], [inner])
        self.assertEqual(self.raw("sub/deep.png")["starred"], 1)

    def test_equal_and_unreachable_roots_are_not_pruned_wrongly(self):
        self.make("a.png")
        dup = str(self.root)
        trailing = media_library._norm_root(str(self.root) + os.sep)          # 同一目录的另一种写法
        result = self.lib.scan([dup, trailing])
        self.assertEqual([s["path"] for s in result["skipped_roots"]], [trailing])
        self.assertEqual(result["skipped_roots"][0]["inside"], dup)
        self.assertEqual(len(result["roots"]), 1)
        self.assertEqual(result["roots"][0]["count"], 1)                     # 只 walk 一次，行没被重复计数
        missing = str(self.root / "nope")
        result2 = self.lib.scan([str(self.root), missing])                   # 不存在的内层根不剪枝
        self.assertEqual(result2["skipped_roots"], [])
        statuses = {r["path"]: r["status"] for r in result2["roots"]}
        self.assertEqual(statuses[missing], "unreachable")                   # 继续按 §4.3 上报
        sibling = self.root.parent / "second"
        sibling.mkdir()
        (sibling / "x.png").write_bytes(b"payload")
        result3 = self.lib.scan([str(self.root), str(sibling)])              # 平行根不受影响
        self.assertEqual(result3["skipped_roots"], [])
        self.assertEqual(result3["added"], 1)

    def test_purge_missing_only_removes_old_enough_rows(self):
        self.make("old.png")
        self.make("new.png")
        self.make("kept.png")
        self.scan()
        os.remove(self.root / "old.png")
        os.remove(self.root / "new.png")
        self.scan()
        with ReadOnlyDb(self.lib.path) as conn:
            conn.execute("UPDATE items SET missing_since = ? WHERE relpath = 'old.png'",
                         (time.time() - 45 * 86400,))
        self.assertEqual(self.lib.purge_missing(30), 1)
        self.assertIsNone(self.raw("old.png"))
        self.assertIsNotNone(self.raw("new.png"))                          # 30 天内只隐藏不删行
        self.assertIsNotNone(self.raw("kept.png"))
        self.assertEqual(self.lib.purge_missing(365), 0)                   # 未超期的不动
        self.assertEqual(self.lib.purge_missing(0), 1)                     # 0 天 = 立即清
        self.assertIsNotNone(self.raw("kept.png"))

    def test_scan_records_meta_rows(self):
        self.make("a.png")
        self.scan()
        self.assertGreater(self.lib.last_scan_at(), 0)
        self.assertEqual(json.loads(self.lib.meta_value("scan_stats"))["added"], 1)
        self.assertEqual(self.lib.meta_value("db_version"), "2")       # 与 user_version 同一口径
        self.assertEqual(self.lib.meta_value("不存在的键"), "")


class QueryTests(LibraryCase):
    def setUp(self):
        LibraryCase.setUp(self)
        self.meta_map = {
            "img_a.png": {"prompt": "1girl, kitten, signature-abc"},
            "img_b.png": {"prompt": "1girl, dog", "checkpoint": "ckB.safetensors",
                          "loras": ["loraB.safetensors"]},
            "img_c.png": {"prompt": "landscape 100% silk", "checkpoint": "ckA.safetensors", "loras": []},
            "img_d.png": {"prompt": "a_b underscore test", "has_meta": False, "source": "none",
                          "checkpoint": "", "loras": []},
            "vid_a.mp4": {"prompt": "kitten video", "duration": 5.5, "source": "mp4_udta"},
            "vid_b.mp4": {"prompt": "", "has_meta": False, "source": "none", "checkpoint": "", "loras": []},
        }
        for name in self.meta_map:
            self.make(("sub/" if name == "img_c.png" else "") + name)
        self.scan()
        self.all_rels = sorted(["img_a.png", "img_b.png", "sub/img_c.png", "img_d.png", "vid_a.mp4", "vid_b.mp4"])
        self.img_c = "sub/img_c.png"

    def test_items_row_shape_matches_contract_5_1(self):
        page = self.lib.list_items({"q": "signature"})
        self.assertEqual(sorted(page.keys()), ["items", "total"])         # §3.1：只含 items/total
        row = page["items"][0]
        for key in ("id", "path", "relpath", "filename", "kind", "ext", "size", "mtime", "added_at", "checkpoint",
                    "loras", "width", "height", "duration", "has_meta", "meta_source", "starred", "tags",
                    "snippet", "seed", "steps", "cfg", "sampler", "scheduler", "fps"):
            self.assertIn(key, row)
        # §15.2.6：新列的值直接随行给出（列即真相，不用前端再解析 extra_json）
        self.assertEqual((row["seed"], row["steps"], row["cfg"]), (123, 20, 4.5))
        self.assertEqual((row["sampler"], row["scheduler"], row["fps"]), ("DPM++ 2M", "karras", None))
        # thumb / url_local / url_http 属传输层（webui._lib_enrich），数据层不拼播放 URL、不引 browser_bridge
        for key in ("thumb", "url_local", "url_http"):
            self.assertNotIn(key, row)
        self.assertEqual(row["relpath"], "img_a.png")
        self.assertEqual(row["checkpoint"], "ckA.safetensors")
        self.assertEqual(row["loras"], ["loraA.safetensors"])
        self.assertEqual(row["width"], 1024)
        self.assertIsNone(row["duration"])
        self.assertIsInstance(row["has_meta"], bool)
        self.assertIsInstance(row["starred"], bool)
        self.assertEqual(row["meta_source"], "png_text")
        self.assertEqual(row["snippet"], "1girl, kitten, signature-abc")
        self.assertEqual(row["tags"], [])
        videos = self.by_rel({"kind": "video"})
        self.assertEqual(videos["vid_a.mp4"]["duration"], 5.5)
        self.assertEqual(videos["vid_a.mp4"]["meta_source"], "mp4_udta")
        self.assertFalse(videos["vid_b.mp4"]["has_meta"])

    def test_snippet_is_first_120_chars_of_prompt(self):
        long_prompt = ", ".join("word%02d" % i for i in range(30))         # 238 字，远超 snippet
        self.meta_map["img_long.png"] = {"prompt": long_prompt}
        self.make("img_long.png")
        self.scan()
        row = self.by_rel({"q": "word29"})["img_long.png"]
        self.assertEqual(row["snippet"], long_prompt[:media_library.SNIPPET_LEN])
        self.assertEqual(len(row["snippet"]), 120)

    def test_detail_adds_scalar_fields_and_exists(self):
        item_id = self.lib.list_items({"q": "signature"})["items"][0]["id"]
        detail = self.lib.detail(item_id)
        for key in ("negative", "models", "seed", "steps", "cfg", "sampler", "scheduler", "fps",
                    "note", "extra", "exists"):
            self.assertIn(key, detail)
        self.assertEqual(detail["negative"], "lowres, bad")
        self.assertEqual(detail["seed"], 123)
        self.assertEqual(detail["steps"], 20)
        self.assertEqual(detail["cfg"], 4.5)
        self.assertEqual(detail["sampler"], "DPM++ 2M")
        self.assertEqual(detail["scheduler"], "karras")
        self.assertIsNone(detail["fps"])                     # 图片没有帧率，也不许伪造 30
        self.assertEqual(detail["extra"], {"format": "png"})
        self.assertEqual(detail["models"], ["ckA.safetensors", "loraA.safetensors"])
        self.assertTrue(detail["exists"])
        self.assertEqual(detail["path"], str(self.root / "img_a.png"))
        self.assertIsNone(self.lib.detail(999999))
        self.assertIsNone(self.lib.get_item(999999))
        moved = self.lib.detail(item_id)
        self.assertTrue(moved["exists"])
        os.remove(self.root / "img_a.png")
        self.lib.mark_gone(item_id)
        self.assertFalse(self.lib.detail(item_id)["exists"])               # exists 以文件系统为准

    def test_paging_total_reflects_filtered_count_not_page_size(self):
        flt = {"limit": 2, "checkpoints": ["ckA.safetensors", "ckB.safetensors"]}
        one = self.lib.list_items(flt)
        self.assertEqual(one["total"], 4)                                  # img_a/img_b/img_c/vid_a
        self.assertEqual(len(one["items"]), 2)
        two = self.lib.list_items(dict(flt, offset=2))
        self.assertEqual(two["total"], 4)
        self.assertEqual(len(two["items"]), 2)
        three = self.lib.list_items(dict(flt, offset=4))
        self.assertEqual(three["total"], 4)
        self.assertEqual(three["items"], [])
        collected = self.ids(flt) + self.ids(dict(flt, offset=2))
        self.assertEqual(len(collected), 4)
        self.assertEqual(len(set(collected)), 4)

    def test_limit_is_clamped_to_contract_bounds(self):
        self.assertEqual(len(self.lib.list_items({"limit": 10000})["items"]), 6)
        self.assertEqual(len(self.lib.list_items({"limit": 500})["items"]), 6)
        self.assertEqual(len(self.lib.list_items({})["items"]), 6)         # 默认 200
        self.assertEqual(len(self.lib.list_items({"limit": 0})["items"]), 6)
        self.assertEqual(len(self.lib.list_items({"limit": -5})["items"]), 6)
        self.assertEqual(self.lib.list_items({"offset": -10})["total"], 6)

    def test_filter_dimensions_compose_and_or_within(self):
        self.assertEqual(self.rel({"kind": "image"}),
                         ["img_a.png", "img_b.png", "img_d.png", "sub/img_c.png"])
        self.assertEqual(self.rel({"checkpoints": ["ckA.safetensors", "ckB.safetensors"]}),
                         ["img_a.png", "img_b.png", "sub/img_c.png", "vid_a.mp4"])
        self.assertEqual(self.rel({"kind": "image", "checkpoints": ["ckB.safetensors"]}), ["img_b.png"])
        self.assertEqual(self.rel({"loras": ["loraA.safetensors", "loraB.safetensors"]}),
                         ["img_a.png", "img_b.png", "vid_a.mp4"])
        self.assertEqual(self.rel({"dirs": ["sub"]}), ["sub/img_c.png"])
        self.assertEqual(self.rel({"dirs": ["", "sub"]}), self.all_rels)
        self.assertEqual(self.rel({"no_meta": True}), ["img_d.png", "vid_b.mp4"])
        self.assertEqual(self.rel({"starred": True}), [])
        self.assertEqual(self.rel({"kind": "video", "no_meta": True}), ["vid_b.mp4"])
        self.assertEqual(self.rel({"limit": 500}), self.all_rels)

    def test_starred_composes_with_other_dimensions(self):
        items = self.by_rel()
        self.assertTrue(self.lib.set_star(items["img_a.png"]["id"], True))
        self.assertTrue(self.lib.set_star(items["img_b.png"]["id"], True))
        self.assertFalse(self.lib.set_star(items["img_a.png"]["id"], False))
        self.assertEqual(self.rel({"starred": True}), ["img_b.png"])
        self.assertEqual(self.rel({"starred": True, "checkpoints": ["ckB.safetensors"]}), ["img_b.png"])
        self.assertEqual(self.rel({"starred": True, "checkpoints": ["ckA.safetensors"]}), [])
        self.assertEqual(self.lib.facets()["starred"], 1)
        self.assertEqual(self.lib.stats()["starred"], 1)
        self.assertFalse(self.lib.set_star(424242, True))                  # 不存在的行
        self.assertEqual(self.by_rel({"starred": True})["img_b.png"]["starred"], True)

    def test_tag_filter_composes(self):
        items = self.by_rel()
        tag = self.lib.save_tag(0, "精选", "#888", -1)
        self.lib.set_tags(items["img_a.png"]["id"], [tag])
        self.lib.set_tags(items["vid_a.mp4"]["id"], [tag])
        self.assertEqual(self.rel({"tag_ids": [tag]}), ["img_a.png", "vid_a.mp4"])
        self.assertEqual(self.rel({"tag_ids": [tag], "kind": "image"}), ["img_a.png"])
        self.assertEqual(self.rel({"tag_ids": [tag], "q": "kitten"}), ["img_a.png", "vid_a.mp4"])
        self.assertEqual(self.rel({"tag_ids": [tag, 9999]}), ["img_a.png", "vid_a.mp4"])
        self.assertEqual(self.rel({"tag_ids": ["非数字垃圾"]}), self.all_rels)   # 非法 id 忽略，不扩大结果

    def test_group_by_whitelist(self):
        self.assertEqual({i["group"] for i in self.lib.list_items({"group_by": "dir"})["items"]}, {"", "sub"})
        self.assertEqual({i["group"] for i in self.lib.list_items({"group_by": "checkpoint"})["items"]},
                         {"", "ckA.safetensors", "ckB.safetensors"})
        dated = {i["group"] for i in self.lib.list_items({"group_by": "date"})["items"]}
        for value in dated:
            self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}$")
        for evil in ("dir) FROM items--", "date'; DROP TABLE tags;--", "unknown"):
            items = self.lib.list_items({"group_by": evil})["items"]
            self.assertNotIn("group", items[0])                             # 白名单外一律不加 group 键
        self.assertEqual(self.lib.list_items({"group_by": ""})["total"], 6)

    def test_sort_whitelist_falls_back_for_injection(self):
        base = self.lib.list_items({"limit": 500})["items"]
        for evil in ("mtime ASC; DROP TABLE items;--", "(SELECT 1)", "1=1", "path", "", None, 123):
            page = self.lib.list_items({"sort": evil, "limit": 500})
            self.assertEqual([r["id"] for r in page["items"]], [r["id"] for r in base],
                             "非法 sort 必须回落 time_desc：%r" % (evil,))
            self.assertEqual(page["total"], len(base))
        self.assertEqual(self.lib.list_items({})["total"], len(base))        # 表还在，没被拼接干掉
        asc = self.lib.list_items({"sort": "time_asc", "limit": 500})["items"]
        self.assertEqual([r["id"] for r in asc],
                         [r["id"] for r in sorted(base, key=lambda r: (r["mtime"], r["id"]))])
        names = self.lib.list_items({"sort": "name_asc", "limit": 500})["items"]
        self.assertEqual([r["filename"] for r in names], sorted(r["filename"] for r in base))
        sizes = self.lib.list_items({"sort": "size_desc", "limit": 500})["items"]
        self.assertEqual([r["size"] for r in sizes], sorted((r["size"] for r in base), reverse=True))
        added = self.lib.list_items({"sort": "added_desc", "limit": 500})["items"]
        self.assertEqual([r["added_at"] for r in added], sorted((r["added_at"] for r in base), reverse=True))

    def test_filter_values_are_bound_not_interpolated(self):
        evil = "ckA.safetensors' OR '1'='1"
        self.assertEqual(self.rel({"checkpoints": [evil]}), [])
        self.assertEqual(self.rel({"dirs": ["sub' OR 1=1--"]}), [])
        self.assertEqual(self.rel({"loras": ['loraA.safetensors") OR 1=1 --']}), [])
        # §15.2.4 新维度同样只进 bind 参数
        self.assertEqual(self.rel({"samplers": ["DPM++ 2M' OR '1'='1"]}), [])
        self.assertEqual(self.rel({"schedulers": ["karras'); DROP TABLE tags;--"]}), [])
        self.assertEqual(self.rel({"sizes": ["1024x1024' OR 1=1--"]}), self.all_rels)   # 非法 → 忽略该维
        self.assertEqual(self.rel({"duration_min": "1 OR 1=1"}), self.all_rels)
        self.assertEqual(self.rel({"duration_max": "5.5; DELETE FROM items"}), self.all_rels)
        self.assertEqual(self.lib.list_items({"samplers": ["DPM++ 2M' OR '1'='1"]})["total"], 0)
        self.assertEqual(self.rel({"kind": "image' --"}), self.all_rels)     # 白名单外当无筛选
        for evil_q in ('kitten" OR checkpoint:', "kitten) OR (1=1", 'cat" AND "dog', "AND", '"""'):
            page = self.lib.list_items({"q": evil_q})                         # 引号/括号不得改写成逻辑
            self.assertIsInstance(page["total"], int)
            self.assertLess(page["total"], 6)
        self.assertEqual(self.lib.list_items({"limit": "两百年", "offset": "x"})["total"], 6)
        self.assertEqual(self.lib.list_items(None)["total"], 6)
        self.assertEqual(self.lib.list_items({"kind": "IMAGE"})["total"], 4)  # 大小写归一后走白名单

    def test_q_search_uses_fts_when_available(self):
        self.assertTrue(self.lib._fts_ok)
        self.assertTrue(media_library.FTS_AVAILABLE)
        self.assertEqual(self.rel({"q": "kitten"}), ["img_a.png", "vid_a.mp4"])
        self.assertEqual(self.rel({"q": "sig"}), ["img_a.png"])                # 前缀 "sig"* 命中 signature-abc
        self.assertEqual(self.rel({"q": "signature-abc"}), ["img_a.png"])
        self.assertEqual(self.rel({"q": "kitten landscape"}), [])              # 跨词 AND
        self.assertEqual(self.rel({"q": "kitten", "kind": "video"}), ["vid_a.mp4"])
        self.assertEqual(self.rel({"q": "不存在的东西"}), [])
        self.assertEqual(self.lib.list_items({"q": "dog"})["total"], 1)
        self.assertEqual(self.lib.list_items({"q": "ckB.safetensors"})["total"], 1)   # 分面字段可搜

    def test_q_search_like_fallback_escapes_wildcards(self):
        self.lib._fts_ok = False
        self.assertEqual(self.rel({"q": "kitten"}), ["img_a.png", "vid_a.mp4"])
        self.assertEqual(self.rel({"q": "100%"}), ["sub/img_c.png"])         # % 必须按字面量
        self.assertEqual(self.rel({"q": "a_b"}), ["img_d.png"])                # _ 不得当通配符
        self.assertEqual(self.rel({"q": "100% silk"}), ["sub/img_c.png"])
        self.assertEqual(self.rel({"q": "img_c"}), ["sub/img_c.png"])          # 文件名也在 search_text
        self.assertEqual(self.rel({"q": "kitten dog"}), [])                     # AND 语义
        self.assertEqual(self.lib.list_items({"q": "kitten"})["total"], 2)
        self.assertEqual(self.rel({"q": "kitten", "kind": "image"}), ["img_a.png"])

    def test_fts_failure_falls_back_to_like_path(self):
        # FTS 表被外部破坏（精简 sqlite 建的老库）时 MATCH 抛错，必须整条查询回落 LIKE 而不是报错
        with ReadOnlyDb(self.lib.path) as conn:
            conn.execute("DROP TABLE IF EXISTS items_fts")
        self.lib._fts_ok = True
        self.assertEqual(self.rel({"q": "kitten"}), ["img_a.png", "vid_a.mp4"])
        self.assertFalse(self.lib._fts_ok)
        self.assertEqual(self.lib.list_items({"q": "100%"})["total"], 1)

    def test_missing_rows_hidden_but_counted_everywhere(self):
        os.remove(self.root / "img_b.png")
        self.scan()
        self.assertEqual(self.lib.list_items({})["total"], 5)
        facets = self.lib.facets()
        self.assertEqual(facets["missing"], 1)
        self.assertEqual(facets["total"], 5)
        self.assertEqual(self.lib.stats()["missing"], 1)
        self.assertNotIn("img_b.png", self.rel({"limit": 500}))
        self.assertEqual(facets["checkpoints"], [{"name": "ckA.safetensors", "count": 3}])  # ckB 只剩缺失行

    def test_mark_gone(self):
        item_id = self.lib.list_items({"q": "landscape"})["items"][0]["id"]
        self.assertTrue(self.lib.mark_gone(item_id))
        self.assertFalse(self.lib.mark_gone(item_id))                           # 已隐藏不重复标记
        self.assertFalse(self.lib.mark_gone(999999))
        self.assertEqual(self.lib.facets()["missing"], 1)
        self.assertIsNotNone(self.raw("sub/img_c.png")["missing_since"])

    def test_get_item_by_path_gates_library_membership(self):
        vid = self.by_rel({"kind": "video"})["vid_a.mp4"]
        got = self.lib.get_item_by_path(vid["path"])
        self.assertEqual(got["id"], vid["id"])
        self.assertEqual(got["relpath"], "vid_a.mp4")
        self.assertIsNone(self.lib.get_item_by_path(str(self.root / "unlisted.mp4")))   # 库外路径不认
        self.assertIsNone(self.lib.get_item_by_path(""))
        self.assertIsNone(self.lib.get_item_by_path(None))

    def test_lookups_and_writes_tolerate_junk_ids(self):
        valid = self.by_rel()[self.all_rels[0]]["id"]
        self.assertIsNotNone(self.lib.get_item(str(valid)))                       # RPC 层可能传字符串
        for junk in (None, "", "abc", "1=1", {"a": 1}, [1], -1):
            self.assertIsNone(self.lib.get_item(junk))
            self.assertIsNone(self.lib.detail(junk))
            self.assertFalse(self.lib.set_star(junk, True))
            self.assertFalse(self.lib.mark_gone(junk))
            self.assertFalse(self.lib.delete_tag(junk))
            self.assertEqual(self.lib.set_tags(junk, [1]), [])
        self.assertEqual(self.lib.purge_missing("不是天数"), 0)                    # 回落 30 天默认值

    def test_facets_shape_matches_contract_5_3(self):
        facets = self.lib.facets()
        self.assertEqual(set(facets.keys()),
                         {"checkpoints", "loras", "dirs", "tags", "samplers", "schedulers", "sizes",
                          "kinds", "starred", "no_meta", "missing", "total"})
        self.assertEqual(set(facets["kinds"].keys()), {"image", "video"})
        self.assertEqual(facets["kinds"], {"image": 4, "video": 2})
        self.assertEqual(facets["total"], 6)
        self.assertEqual(facets["missing"], 0)
        self.assertEqual(facets["no_meta"], 2)
        self.assertEqual(facets["starred"], 0)
        self.assertEqual(set(facets["checkpoints"][0].keys()), {"name", "count"})
        self.assertEqual([c["name"] for c in facets["checkpoints"]][0], "ckA.safetensors")
        self.assertEqual([c["count"] for c in facets["checkpoints"]],
                         sorted([c["count"] for c in facets["checkpoints"]], reverse=True))
        self.assertNotIn("", [c["name"] for c in facets["checkpoints"]])          # 空底模不进分面
        self.assertEqual(facets["checkpoints"], [{"name": "ckA.safetensors", "count": 3},
                                                 {"name": "ckB.safetensors", "count": 1}])
        for entry in facets["loras"]:
            self.assertEqual(set(entry.keys()), {"name", "count"})
        self.assertEqual({l["name"] for l in facets["loras"]},
                         {"loraA.safetensors", "loraB.safetensors"})
        self.assertEqual([l["count"] for l in facets["loras"]], [2, 1])
        for entry in facets["dirs"]:
            self.assertEqual(set(entry.keys()), {"name", "count", "path"})
        self.assertIn({"name": "sub", "count": 1, "path": "sub"}, facets["dirs"])
        self.assertIn({"name": "", "count": 5, "path": ""}, facets["dirs"])
        for tag in facets["tags"]:
            self.assertEqual(set(tag.keys()), {"id", "name", "color", "count"})
        self.assertEqual(facets["tags"], [])
        # §15.2.5：三个新分面与 checkpoints 完全同形（夹具里 6 条共用同一组参数）
        for key in ("samplers", "schedulers", "sizes"):
            self.assertEqual(facets[key], [{"name": {"samplers": "DPM++ 2M", "schedulers": "karras",
                                                     "sizes": "1024x1024"}[key], "count": 6}], key)
            for entry in facets[key]:
                self.assertEqual(set(entry.keys()), {"name", "count"})

    def test_facet_lists_are_capped_at_300(self):
        with ReadOnlyDb(self.lib.path) as conn:
            for index in range(320):
                name = "bulk%03d.png" % index
                path = self.make(name)
                st = os.stat(path)
                conn.execute("INSERT INTO items(path, root_id, relpath, dir_path, filename, ext, kind, size, mtime, "
                             "meta_mtime, added_at, checkpoint) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (os.path.abspath(path), "otherroot", name, "", name, ".png", "image", st.st_size,
                              st.st_mtime, st.st_mtime, time.time(), "bulk%03d.safetensors" % index))
        facets = self.lib.facets()
        self.assertEqual(len(facets["checkpoints"]), 300)
        self.assertTrue(all(c["count"] >= 1 for c in facets["checkpoints"]))

    def test_stats_shape_matches_contract(self):
        stats = self.lib.stats()
        self.assertEqual(set(stats.keys()),
                         {"total", "image", "video", "starred", "tagged", "missing", "no_meta", "db_bytes",
                          "checkpoints", "tags"})
        self.assertEqual(stats["total"], 6)
        self.assertEqual(stats["image"], 4)
        self.assertEqual(stats["video"], 2)
        self.assertEqual(stats["no_meta"], 2)
        self.assertEqual(stats["missing"], 0)
        self.assertEqual(stats["tagged"], 0)
        self.assertEqual(stats["checkpoints"], 2)
        self.assertEqual(stats["tags"], 0)
        self.assertGreater(stats["db_bytes"], 0)
        tag = self.lib.save_tag(0, "统计", "", -1)
        self.lib.set_tags(self.by_rel()[self.img_c]["id"], [tag])
        self.assertEqual(self.lib.stats()["tagged"], 1)
        self.assertEqual(self.lib.stats()["tags"], 1)

    def test_set_poster_writes_cache_and_syncs_flag(self):
        videos = [i["path"] for i in self.lib.list_items({"kind": "video", "limit": 500})["items"]]
        vid = videos[0]
        saved = self.lib.set_poster(vid, b"\xff\xd8fakejpegdata")
        self.assertTrue(saved["ok"])
        self.assertTrue(os.path.exists(media_library.poster_path(vid)))
        self.assertTrue(self.poster_flag(vid))                             # 库侧标记同步跟上
        row = [i for i in self.lib.list_items({"kind": "video", "limit": 500})["items"] if i["path"] == vid][0]
        self.assertTrue(self.poster_flag(row["path"]))                     # 视频有海报即可直出缩略
        self.assertFalse(self.lib.set_poster(str(self.root / "outside.mp4"), b"x")["ok"])   # 库外路径不落行
        self.assertFalse(os.path.exists(media_library.poster_path(str(self.root / "outside.mp4"))))  # 也不得造文件
        self.assertEqual(self.lib.set_poster(vid, "不是字节")["ok"], False)

    def test_set_poster_without_data_resyncs_flag(self):
        vid = self.by_rel({"kind": "video"})["vid_a.mp4"]
        target = media_library.poster_path(vid["path"])
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(b"\xff\xd8jpeg")                          # A3 自行落盘海报的场景（lib_save_poster）
        self.assertFalse(self.poster_flag(vid["path"]))        # 文件在、标记还没跟上
        self.assertTrue(self.lib.set_poster(vid["path"])["ok"])  # 只同步标记，不重写文件
        self.assertTrue(self.poster_flag(vid["path"]))
        self.assertTrue(os.path.getsize(target) > 0)           # 同步标记不得清空海报

    def test_has_poster_is_persisted_for_rescan(self):
        vid = self.by_rel({"kind": "video"})["vid_a.mp4"]
        self.lib.set_poster(vid["path"], b"\xff\xd8jpeg")
        self.scan()                                                    # 重新扫描不重解析、海报标记不丢
        self.assertTrue(self.poster_flag(vid["path"]))
        self.assertTrue(os.path.exists(media_library.poster_path(vid["path"])))


class TagCrudTests(LibraryCase):
    def setUp(self):
        LibraryCase.setUp(self)
        self.make("a.png")
        self.make("b.png")
        self.scan()
        self.items = self.by_rel()

    def item_id(self, rel):
        return self.items[rel]["id"]

    def test_save_rename_parent_and_delete_cascade(self):
        parent = self.lib.save_tag(0, "精选", "#888888", -1)
        self.assertIsInstance(parent, int)
        self.assertGreater(parent, 0)
        child = self.lib.save_tag(0, "人物", "", parent)
        tags = {t["name"]: t for t in self.lib.list_tags()}
        self.assertEqual(sorted(tags.keys()), ["人物", "精选"])
        self.assertEqual(tags["人物"]["parent_id"], parent)
        self.assertIsNone(tags["精选"]["parent_id"])                    # parent_id=-1 表示无父
        self.assertEqual({t["color"] for t in self.lib.list_tags()}, {"#888888", ""})

        item_id = self.item_id("a.png")
        result = self.lib.set_tags(item_id, [parent, child])
        self.assertEqual({t["name"] for t in result}, {"人物", "精选"})
        self.assertEqual(sorted(result[0].keys()), ["color", "id", "name"])    # §5.1 tags 形状
        self.assertEqual(len(self.lib.get_item(item_id)["tags"]), 2)
        self.assertEqual(self.rel({"tag_ids": [child]}), ["a.png"])

        renamed = self.lib.save_tag(child, "人物角色", "#fff000", parent)
        self.assertEqual(renamed, child)
        self.assertEqual({t["name"] for t in self.lib.get_item(item_id)["tags"]}, {"人物角色", "精选"})
        self.assertIn("人物角色", [t["name"] for t in self.lib.list_tags()])

        self.assertTrue(self.lib.delete_tag(parent))
        self.assertFalse(self.lib.delete_tag(parent))
        self.assertEqual([t["name"] for t in self.lib.get_item(item_id)["tags"]], ["人物角色"])   # item_tags 级联
        with ReadOnlyDb(self.lib.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM item_tags WHERE tag_id = ?", (parent,)).fetchone()[0], 0)
        left = self.lib.list_tags()
        self.assertEqual(len(left), 1)
        self.assertIsNone(left[0]["parent_id"])                          # 子标签上提，不留孤儿

    def test_set_tags_replaces_and_drops_unknown_ids(self):
        one = self.lib.save_tag(0, "甲", "", -1)
        two = self.lib.save_tag(0, "乙", "", -1)
        item_id = self.item_id("a.png")
        self.assertEqual(len(self.lib.set_tags(item_id, [one, two])), 2)
        self.assertEqual(len(self.lib.set_tags(item_id, [two])), 1)      # 整组替换
        self.assertEqual(self.lib.set_tags(item_id, [one, one]), [t for t in self.lib.get_item(item_id)["tags"]])
        self.assertEqual(len(self.lib.set_tags(item_id, [two, 999999])), 1)   # 库里没有的 tag_id 忽略
        self.assertEqual(self.lib.set_tags(item_id, []), [])
        self.assertEqual(self.lib.get_item(item_id)["tags"], [])

    def test_duplicate_name_returns_existing_tag_instead_of_raising(self):
        first = self.lib.save_tag(0, "同名", "", -1)
        self.assertEqual(self.lib.save_tag(0, "同名", "#123456", -1), first)
        self.assertEqual(len(self.lib.list_tags()), 1)

    def test_rename_to_existing_name_merges_and_keeps_items(self):
        keep = self.lib.save_tag(0, "保留", "", -1)
        drop = self.lib.save_tag(0, "多余", "", -1)
        self.lib.set_tags(self.item_id("a.png"), [drop])
        self.lib.set_tags(self.item_id("b.png"), [keep, drop])
        self.assertEqual(self.lib.save_tag(drop, "保留", "", -1), keep)
        self.assertEqual([t["name"] for t in self.lib.get_item(self.item_id("a.png"))["tags"]], ["保留"])
        self.assertEqual(len(self.lib.get_item(self.item_id("b.png"))["tags"]), 1)
        self.assertEqual(len(self.lib.list_tags()), 1)

    def test_parent_cycle_is_rejected(self):
        top = self.lib.save_tag(0, "A", "", -1)
        mid = self.lib.save_tag(0, "B", "", top)
        low = self.lib.save_tag(0, "C", "", mid)
        self.lib.save_tag(top, "A", "", low)                              # A 挂到子孙 C 之下 → 拒绝
        tags = {t["name"]: t for t in self.lib.list_tags()}
        self.assertIsNone(tags["A"]["parent_id"])
        self.assertEqual(tags["B"]["parent_id"], top)
        self.assertEqual(tags["C"]["parent_id"], mid)
        self.lib.save_tag(mid, "B", "", mid)                               # 自环同样拒绝
        self.assertIsNone({t["name"]: t for t in self.lib.list_tags()}["B"]["parent_id"])

    def test_empty_name_and_unknown_item_are_noops(self):
        self.assertEqual(self.lib.save_tag(0, "   ", "", -1), 0)
        self.assertEqual(self.lib.set_tags(987654, [1]), [])
        self.assertEqual(self.lib.list_tags(), [])

    def test_tag_counts_only_cover_present_files(self):
        tag = self.lib.save_tag(0, "计数", "", -1)
        self.lib.set_tags(self.item_id("a.png"), [tag])
        self.lib.set_tags(self.item_id("b.png"), [tag])
        self.assertEqual(self.lib.facets()["tags"][0]["count"], 2)
        self.assertEqual(self.lib.list_tags()[0]["count"], 2)
        os.remove(self.root / "b.png")
        self.scan()
        self.assertEqual(self.lib.facets()["tags"][0]["count"], 1)         # 缺失文件不再计入分面
        self.assertEqual(self.lib.list_tags()[0]["count"], 1)


class ConnectionPolicyTests(LibraryCase):
    def test_no_long_lived_connection_between_operations(self):
        self.make("a.png")
        self.scan()
        self.assertFalse(hasattr(self.lib, "_conn"))                       # 实例不持有连接对象
        for _ in range(3):
            self.lib.list_items({})
            self.lib.facets()
            self.lib.stats()
        probe = sqlite3.connect(self.lib.path, timeout=5, isolation_level=None)
        try:
            probe.execute("BEGIN EXCLUSIVE")                               # 残留连接会让这里 database is locked
            probe.execute("COMMIT")
        finally:
            probe.close()

    def test_db_file_removed_between_operations_is_recreated(self):
        self.make("a.png")
        self.scan()
        self.assertEqual(self.lib.list_items({})["total"], 1)
        for name in list(os.listdir(self.app_dir)):
            if name.startswith(media_library.DB_NAME):
                os.remove(os.path.join(self.app_dir, name))
        self.assertEqual(self.lib.list_items({})["total"], 0)              # 自动重建空库
        self.assertEqual(self.lib.scan(self.roots)["added"], 1)

    def test_two_instances_share_the_same_db_file(self):
        self.make("a.png")
        self.scan()
        other = media_library.MediaLibrary()                               # 模拟 RPC 线程各自建实例
        self.assertEqual(other.list_items({})["total"], 1)
        self.assertEqual(other.set_star(other.list_items({})["items"][0]["id"], True), True)
        self.assertEqual(self.lib.list_items({"starred": True})["total"], 1)


class RootScopeTests(LibraryCase):
    """移出配置的根：行保留但不得出现在列表/分面里（重新加回即恢复，标签收藏不丢）。"""

    def setUp(self):
        LibraryCase.setUp(self)
        self.root2 = Path(self._tmp.name) / "other"
        self.root2.mkdir()
        self.make("a.png")
        other = self.root2 / "b.png"
        other.write_bytes(b"payload")
        stamp = time.time() - 5000
        os.utime(other, (stamp, stamp))
        self.lib.scan([str(self.root), str(self.root2)])
        self.id1 = media_library.root_id(str(self.root))
        self.id2 = media_library.root_id(str(self.root2))

    def test_root_ids_restricts_list_and_total(self):
        self.assertEqual(self.lib.list_items({})["total"], 2)               # 不传 = 不限定（旧行为不变）
        one = self.lib.list_items({"root_ids": [self.id1]})
        self.assertEqual(one["total"], 1)
        self.assertEqual([i["filename"] for i in one["items"]], ["a.png"])
        self.assertEqual(self.lib.list_items({"root_ids": []})["total"], 0)  # 一个根都没配 → 空视图

    def test_facets_share_the_same_scope(self):
        star = self.lib.list_items({"root_ids": [self.id1]})["items"][0]
        self.lib.set_star(star["id"], True)
        f_all = self.lib.facets()
        f_one = self.lib.facets(root_ids=[self.id1])
        f_none = self.lib.facets(root_ids=[])
        self.assertEqual((f_all["total"], f_one["total"]), (2, 1))
        self.assertEqual((f_one["starred"], f_one["image"] if "image" in f_one else f_one["kinds"]["image"]), (1, 1))
        self.assertEqual([d["path"] for d in f_one["dirs"]], [""])
        self.assertEqual((f_none["total"], f_none["starred"], f_none["dirs"]), (0, 0, []))

    def test_unconfigured_root_rows_survive_and_come_back(self):
        self.lib.scan([str(self.root)])                     # 只扫 root1：root2 的行不得被判缺失（§4.3）
        hidden = self.lib.list_items({"root_ids": [self.id2]})
        self.assertEqual(hidden["total"], 1)
        row = hidden["items"][0]
        self.assertIsNone(row["missing_since"])
        self.lib.set_star(row["id"], True)
        tid = self.lib.save_tag(0, "留着", "", -1)
        self.lib.set_tags(row["id"], [tid])
        back = self.lib.list_items({"root_ids": [self.id1, self.id2], "starred": True})
        self.assertEqual([i["filename"] for i in back["items"]], ["b.png"])
        self.assertEqual([t["name"] for t in back["items"][0]["tags"]], ["留着"])


class ParamDimensionTests(LibraryCase):
    """§15.2：参数列写库 + samplers/schedulers/sizes/duration 筛选维度 + 三个新分面。"""

    ALL = ["p_a.png", "p_b.png", "p_c.png", "p_d.png", "vid_a.mp4", "vid_b.mp4"]

    def setUp(self):
        LibraryCase.setUp(self)
        self.meta_map = {
            "p_a.png": {"sampler": "DPM++ 2M", "scheduler": "karras", "width": 768, "height": 1280,
                        "seed": 11, "steps": 20, "cfg": 4.5, "checkpoint": "ckA.safetensors"},
            "p_b.png": {"sampler": "DPM++ 2M", "scheduler": "karras", "width": 768, "height": 1280,
                        "seed": 12, "steps": 30, "cfg": 7.0, "checkpoint": "ckB.safetensors"},
            "p_c.png": {"sampler": "Euler a", "scheduler": "simple", "width": 1024, "height": 1024,
                        "seed": 13, "steps": 25, "cfg": 1.0},
            "p_d.png": {"sampler": "", "scheduler": "", "width": 512, "height": 512,
                        "seed": None, "steps": None, "cfg": None, "has_meta": False, "source": "none"},
            "vid_a.mp4": {"sampler": "DPM++ 3M SDE", "scheduler": "Normal", "width": 480, "height": 864,
                          "duration": 4.5, "fps": 24.0, "steps": 8, "seed": None, "source": "mp4_udta"},
            "vid_b.mp4": {"sampler": "Euler a", "scheduler": "karras", "width": 768, "height": 1280,
                          "duration": 30.5, "fps": 8.0, "seed": 9, "source": "mp4_udta"},
        }
        for name in self.ALL:
            self.make(name)
        self.scan()

    def columns(self, rel):
        row = self.raw(rel)
        return {key: row[key] for key in ("seed", "steps", "cfg", "sampler", "scheduler", "fps")}

    def test_params_land_in_columns_and_are_mirrored_in_extra_json(self):
        self.assertEqual(self.columns("p_a.png"), {"seed": 11, "steps": 20, "cfg": 4.5,
                                                   "sampler": "DPM++ 2M", "scheduler": "karras", "fps": None})
        self.assertEqual(self.columns("vid_a.mp4"), {"seed": None, "steps": 8, "cfg": 4.5,
                                                    "sampler": "DPM++ 3M SDE", "scheduler": "Normal",
                                                    "fps": 24.0})
        # 没解析出参数的行：文本列按 §15.2.1 的 DEFAULT ''，数值列为 NULL（不是 0）
        self.assertEqual(self.columns("p_d.png"), {"seed": None, "steps": None, "cfg": None,
                                                  "sampler": "", "scheduler": "", "fps": None})
        extra = json.loads(self.raw("vid_b.mp4")["extra_json"])
        self.assertEqual((extra["seed"], extra["sampler"], extra["fps"]), (9, "Euler a", 8.0))
        row = self.by_rel()["vid_b.mp4"]
        self.assertEqual((row["seed"], row["sampler"], row["scheduler"], row["fps"], row["cfg"]),
                         (9, "Euler a", "karras", 8.0, 4.5))

    def test_oversized_seed_stays_readable_through_extra_json(self):
        """sqlite INTEGER 只有 64 位，而 seed 契约允许到 2**64：列落 NULL，详情靠回落不丢展示。"""
        self.meta_map["big.png"] = {"seed": 2 ** 64 - 1, "sampler": "DPM++ 2M"}
        self.make("big.png")
        self.scan()                                        # 绝不允许 OverflowError 把整批入库干掉
        self.assertIsNone(self.raw("big.png")["seed"])
        self.assertEqual(json.loads(self.raw("big.png")["extra_json"])["seed"], 2 ** 64 - 1)
        item_id = self.by_rel()["big.png"]["id"]
        self.assertEqual(self.lib.detail(item_id)["seed"], 2 ** 64 - 1)
        self.assertEqual(self.rel({"samplers": ["DPM++ 2M"]}), ["big.png", "p_a.png", "p_b.png"])
        self.assertEqual(self.lib.detail(item_id)["sampler"], "DPM++ 2M")   # 文本列照样可筛

    def test_samplers_and_schedulers_or_within_dimension_and_across(self):
        self.assertEqual(self.rel({"samplers": ["DPM++ 2M", "Euler a"]}),
                         ["p_a.png", "p_b.png", "p_c.png", "vid_b.mp4"])           # 同维度 OR
        self.assertEqual(self.rel({"schedulers": ["karras", "simple"]}),
                         ["p_a.png", "p_b.png", "p_c.png", "vid_b.mp4"])
        self.assertEqual(self.rel({"samplers": ["Euler a"], "schedulers": ["karras"]}), ["vid_b.mp4"])
        self.assertEqual(self.rel({"samplers": ["Euler a"], "schedulers": ["simple"]}), ["p_c.png"])
        self.assertEqual(self.rel({"samplers": ["Euler a", "DPM++ 3M SDE"], "kind": "video"}),
                         ["vid_a.mp4", "vid_b.mp4"])                                # 与既有维度 AND
        self.assertEqual(self.rel({"samplers": ["DPM++ 2M"], "checkpoints": ["ckB.safetensors"]}), ["p_b.png"])
        self.assertEqual(self.rel({"samplers": ["不存在的采样器"]}), [])
        self.assertEqual(self.lib.list_items({"samplers": ["DPM++ 2M"]})["total"], 2)

    def test_sizes_dimension_matches_exact_pairs(self):
        self.assertEqual(self.rel({"sizes": ["768x1280"]}), ["p_a.png", "p_b.png", "vid_b.mp4"])
        self.assertEqual(self.rel({"sizes": ["768x1280", "1024x1024"]}),
                         ["p_a.png", "p_b.png", "p_c.png", "vid_b.mp4"])            # 同维度 OR
        self.assertEqual(self.rel({"sizes": ["1280x768"]}), [])                     # 有序对，不许颠倒命中
        self.assertEqual(self.rel({"sizes": ["1024x1024"], "samplers": ["Euler a"]}), ["p_c.png"])
        self.assertEqual(self.rel({"sizes": ["480x864"], "kind": "video"}), ["vid_a.mp4"])
        self.assertEqual(self.rel({"sizes": [" 768x1280 "]}), ["p_a.png", "p_b.png", "vid_b.mp4"])

    def test_illegal_sizes_are_ignored_without_error(self):
        for bad in ("768", "768x", "x1280", "768x1280x2", "abc", "768*1280", "-768x1280", "768 x 1280",
                    "768x1280' OR '1'='1", "768x1280); DROP TABLE items;--", "", None, 12.5, "9" * 30 + "x1"):
            self.assertEqual(self.rel({"sizes": [bad], "limit": 500}), self.ALL,
                             "非法 sizes 必须整维忽略：%r" % (bad,))
            self.assertEqual(self.lib.list_items({})["total"], 6)                   # 表没被干掉
        # 混在一起：只按合法的那几项筛，非法项既不放宽也不报错
        self.assertEqual(self.rel({"sizes": ["abc", "512x512", "1x1x1"]}), ["p_d.png"])

    def test_duration_bounds_are_inclusive_on_both_sides(self):
        # duration_min = 「至少这么长」，duration_max = 「最多这么长」；等于边界必须命中
        self.assertEqual(self.rel({"duration_min": 4.5}), ["vid_a.mp4", "vid_b.mp4"])
        self.assertEqual(self.rel({"duration_min": 4.5, "duration_max": 4.5}), ["vid_a.mp4"])
        self.assertEqual(self.rel({"duration_max": 30.5}), ["vid_a.mp4", "vid_b.mp4"])
        self.assertEqual(self.rel({"duration_min": 30.5, "duration_max": 30.5}), ["vid_b.mp4"])
        self.assertEqual(self.rel({"duration_min": 4.6}), ["vid_b.mp4"])          # 刚过下界就只剩长的
        self.assertEqual(self.rel({"duration_max": 4.4}), [])
        self.assertEqual(self.rel({"duration_min": 5, "duration_max": 30}), [])
        self.assertEqual(self.rel({"duration_min": 0, "kind": "video"}), ["vid_a.mp4", "vid_b.mp4"])
        self.assertEqual(self.rel({"duration_min": "4.5"}), ["vid_a.mp4", "vid_b.mp4"])   # 字符串数字可用
        self.assertEqual(self.rel({"duration_min": 4.5, "samplers": ["DPM++ 3M SDE"]}), ["vid_a.mp4"])
        self.assertEqual(self.rel({"duration_max": 100}), ["vid_a.mp4", "vid_b.mp4"])      # 图片 duration 为 NULL
        self.assertEqual(self.lib.list_items({"duration_min": 4.5, "duration_max": 4.5})["total"], 1)

    def test_illegal_duration_bounds_are_ignored(self):
        for bad in ("很多", "", None, [1], {}, "4.5.6", "1 OR 1=1"):
            self.assertEqual(self.rel({"duration_min": bad, "limit": 500}), self.ALL,
                             "非法 duration_min 必须忽略该维度：%r" % (bad,))
            self.assertEqual(self.rel({"duration_max": bad, "limit": 500}), self.ALL)
        self.assertEqual(self.lib.list_items({"duration_min": "很多", "duration_max": "坏"})["total"], 6)

    def test_new_facets_shape_order_and_present_only(self):
        facets = self.lib.facets()
        for key in ("samplers", "schedulers", "sizes"):
            for entry in facets[key]:
                self.assertEqual(set(entry.keys()), {"name", "count"}, key)
        self.assertEqual(facets["samplers"], [{"name": "DPM++ 2M", "count": 2},
                                              {"name": "Euler a", "count": 2},
                                              {"name": "DPM++ 3M SDE", "count": 1}])
        self.assertEqual(facets["schedulers"], [{"name": "karras", "count": 3},
                                                {"name": "Normal", "count": 1},
                                                {"name": "simple", "count": 1}])
        # count DESC, name ASC（name 是文本，"1024x1024" 排在 "480x864" 前）；空值/NULL 不进分面
        self.assertEqual(facets["sizes"], [{"name": "768x1280", "count": 3},
                                           {"name": "1024x1024", "count": 1},
                                           {"name": "480x864", "count": 1},
                                           {"name": "512x512", "count": 1}])
        self.assertNotIn("", [s["name"] for s in facets["samplers"]])               # p_d 没采样器
        for counts in ([f["count"] for f in facets[key]] for key in
                      ("samplers", "schedulers", "sizes")):
            self.assertEqual(counts, sorted(counts, reverse=True))
        os.remove(self.root / "p_b.png")
        os.remove(self.root / "vid_a.mp4")
        self.scan()
        facets = self.lib.facets()
        self.assertEqual(facets["total"], 4)                                        # 只统计在场文件
        self.assertEqual(facets["samplers"], [{"name": "Euler a", "count": 2},
                                              {"name": "DPM++ 2M", "count": 1}])
        self.assertEqual(facets["schedulers"], [{"name": "karras", "count": 2},
                                                {"name": "simple", "count": 1}])
        self.assertEqual(facets["sizes"], [{"name": "768x1280", "count": 2},
                                           {"name": "1024x1024", "count": 1},
                                           {"name": "512x512", "count": 1}])
        self.assertEqual(self.lib.list_items({"samplers": ["DPM++ 3M SDE"]})["total"], 0)

    def test_new_facets_honour_root_ids_scope(self):
        """上一轮刚修的语义（§13.14）：三个新分面也必须跟着 root 作用域走，别把幽灵根算进来。"""
        second = self.root.parent / "second"
        second.mkdir()
        self.meta_map["q_a.png"] = {"sampler": "Restart", "scheduler": "SGMUniform",
                                    "width": 256, "height": 256}
        (second / "q_a.png").write_bytes(b"payload")
        stamp = time.time() - 5000
        os.utime(second / "q_a.png", (stamp, stamp))
        self.lib.scan([str(self.root), str(second)])
        id1 = media_library.root_id(str(self.root))
        id2 = media_library.root_id(str(second))
        full = self.lib.facets()
        scoped = self.lib.facets(root_ids=[id1])
        empty = self.lib.facets(root_ids=[])
        self.assertEqual(full["total"], 7)
        for key, absent in (("samplers", "Restart"), ("schedulers", "SGMUniform"), ("sizes", "256x256")):
            self.assertIn(absent, [f["name"] for f in full[key]], key)
            self.assertNotIn(absent, [f["name"] for f in scoped[key]], key)
            self.assertEqual(scoped[key], [f for f in full[key] if f["name"] != absent], key)
            self.assertEqual(empty[key], [], key)
        self.assertEqual(scoped["total"], 6)
        self.assertEqual(self.lib.list_items({"root_ids": [id1]})["total"], 6)      # 与 list_items 同口径
        self.assertEqual(self.rel({"root_ids": [id2], "samplers": ["Restart"]}), ["q_a.png"])

    def test_new_facets_are_capped_at_facet_limit(self):
        with ReadOnlyDb(self.lib.path) as conn:
            for index in range(320):
                name = "bulk%03d.png" % index
                path = self.make(name)
                st = os.stat(path)
                conn.execute("INSERT INTO items(path, root_id, relpath, dir_path, filename, ext, kind, size, "
                             "mtime, meta_mtime, added_at, sampler, scheduler, width, height) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (os.path.abspath(path), "otherroot", name, "", name, ".png", "image",
                              st.st_size, st.st_mtime, st.st_mtime, time.time(),
                              "bulk%03d" % index, "sched%03d" % index, 640 + index, 512))
        facets = self.lib.facets()
        for key in ("samplers", "schedulers", "sizes"):
            self.assertEqual(len(facets[key]), media_library.FACET_LIMIT, key)
            self.assertTrue(all(f["count"] >= 1 for f in facets[key]))

    def test_detail_reads_columns_first_and_falls_back_to_extra_json(self):
        item_id = self.by_rel()["p_a.png"]["id"]
        self.assertEqual(self.lib.detail(item_id)["sampler"], "DPM++ 2M")           # 列即真相
        # 列为空（老库回填不到 / 超大值装不下）→ 回落 extra_json，既有展示不退化
        with ReadOnlyDb(self.lib.path) as conn:
            conn.execute("UPDATE items SET seed = NULL, steps = NULL, cfg = NULL, sampler = '', "
                         "scheduler = '', fps = NULL WHERE id = ?", (item_id,))
        detail = self.lib.detail(item_id)
        self.assertEqual((detail["seed"], detail["steps"], detail["cfg"]), (11, 20, 4.5))
        self.assertEqual((detail["sampler"], detail["scheduler"]), ("DPM++ 2M", "karras"))
        self.assertEqual(detail["negative"], "lowres, bad")                         # 老字段的口径不变
        self.assertEqual(detail["extra"], {"format": "png"})
        # 列有值时以列为准：extra_json 里的副本被外部改脏也不得影响详情
        with ReadOnlyDb(self.lib.path) as conn:
            conn.execute("UPDATE items SET seed = 77, steps = 21, cfg = 8.5, sampler = 'Restart', "
                         "scheduler = 'SGMUniform', fps = 30.0, extra_json = ? WHERE id = ?",
                         (json.dumps({"seed": 1, "steps": 2, "cfg": 3.0, "sampler": "脏",
                                      "scheduler": "脏", "fps": 1.0, "note": "备注"}, ensure_ascii=False),
                          item_id))
        detail = self.lib.detail(item_id)
        self.assertEqual((detail["seed"], detail["steps"], detail["cfg"]), (77, 21, 8.5))
        self.assertEqual((detail["sampler"], detail["scheduler"], detail["fps"]),
                         ("Restart", "SGMUniform", 30.0))
        self.assertEqual(detail["note"], "备注")                                    # note 只有 extra_json 里有


class MigrationTests(LibraryCase):
    """§15.2.2：v1 库原地迁移（加列 + 从 extra_json 回填），**绝不**要求删库重扫。"""

    # 老库的裸 SQL 建表照抄契约 §3.2（一个参数列都没有），迁移完全由被测代码负责
    V1_DDL = """CREATE TABLE items(
  id            INTEGER PRIMARY KEY,
  path          TEXT NOT NULL UNIQUE,
  root_id       TEXT NOT NULL,
  relpath       TEXT NOT NULL,
  dir_path      TEXT NOT NULL,
  filename      TEXT NOT NULL,
  ext           TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('image','video')),
  size          INTEGER NOT NULL,
  mtime         REAL NOT NULL,
  meta_mtime    REAL NOT NULL DEFAULT 0,
  added_at      REAL NOT NULL,
  missing_since REAL,
  checkpoint    TEXT NOT NULL DEFAULT '',
  loras_json    TEXT NOT NULL DEFAULT '[]',
  models_json   TEXT NOT NULL DEFAULT '[]',
  width         INTEGER, height INTEGER, duration REAL,
  prompt        TEXT NOT NULL DEFAULT '',
  negative      TEXT NOT NULL DEFAULT '',
  search_text   TEXT NOT NULL DEFAULT '',
  has_meta      INTEGER NOT NULL DEFAULT 0,
  meta_source   TEXT NOT NULL DEFAULT '',
  starred       INTEGER NOT NULL DEFAULT 0,
  has_poster    INTEGER NOT NULL DEFAULT 0,
  extra_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE tags(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, color TEXT NOT NULL DEFAULT '',
                  parent_id INTEGER);
CREATE TABLE item_tags(item_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, PRIMARY KEY(item_id, tag_id));
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"""

    V1_COLUMNS = ("path", "root_id", "relpath", "dir_path", "filename", "ext", "kind", "size", "mtime",
                  "meta_mtime", "added_at", "missing_since", "checkpoint", "loras_json", "models_json",
                  "width", "height", "duration", "prompt", "negative", "search_text", "has_meta",
                  "meta_source", "starred", "has_poster", "extra_json")

    def build_v1(self, rows, version=1):
        path = str(self.app_dir / "v1_media_library.db")
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.executescript(self.V1_DDL)
            for row in rows:
                values = dict({"root_id": media_library._root_id(str(self.root)), "dir_path": "",
                               "ext": ".png", "kind": "image", "meta_mtime": time.time(),
                               "added_at": 1728000000.0, "missing_since": None, "checkpoint": "",
                               "loras_json": "[]", "models_json": "[]", "width": None, "height": None,
                               "duration": None, "prompt": "", "negative": "", "search_text": "",
                               "has_meta": 1, "meta_source": "png_text", "starred": 0, "has_poster": 0,
                               "extra_json": "{}"}, **row)
                conn.execute("INSERT INTO items(%s) VALUES(%s)" % (
                    ",".join(self.V1_COLUMNS), ",".join("?" * len(self.V1_COLUMNS))),
                    [values[key] for key in self.V1_COLUMNS])
            conn.execute("INSERT INTO tags(id, name, color) VALUES(11, '精选', '#888')")
            conn.execute("INSERT INTO item_tags(item_id, tag_id) VALUES(1, 11)")
            conn.execute("PRAGMA user_version = %d" % int(version))
            conn.commit()
        return path

    def file_row(self, rel, extra, **kw):
        """按磁盘真实 size/mtime 造一行 v1 记录，让迁移后的增量扫描认得出「同一个文件」。"""
        path = self.root / rel.replace("/", os.sep)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"payload")
        stamp = time.time() - 5000
        os.utime(path, (stamp, stamp))
        st = os.stat(path)
        row = {"path": os.path.abspath(str(path)), "relpath": rel, "filename": rel.rsplit("/", 1)[-1],
               "size": st.st_size, "mtime": st.st_mtime, "extra_json": json.dumps(extra, ensure_ascii=False)}
        if "/" in rel:
            row["dir_path"] = rel.rsplit("/", 1)[0]
        if rel.endswith(".mp4"):
            row.update({"ext": ".mp4", "kind": "video"})
        row.update(kw)
        return row

    def params_of(self, path, rel):
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT seed, steps, cfg, sampler, scheduler, fps, meta_mtime, starred, "
                               "added_at, prompt FROM items WHERE relpath = ?", (rel,)).fetchone()
            return dict(row) if row is not None else None

    def test_v1_database_gains_columns_and_backfills_from_extra_json(self):
        path = self.build_v1([
            {"path": "F:/v1/a.png", "relpath": "a.png", "filename": "a.png",
             "root_id": media_library._root_id(str(self.root)), "size": 100, "mtime": 1728000000.0,
             "width": 768, "height": 1280, "prompt": "1girl, old", "starred": 1,
             "search_text": "a.png 1girl old signature-xyz",
             "extra_json": json.dumps({"seed": 1234, "steps": 28, "cfg": 6.5, "sampler": "DPM++ 2M Karras",
                                       "scheduler": "exponential", "fps": 24.0, "note": "", "extra": {}})},
            {"path": "F:/v1/b.png", "relpath": "sub/b.png", "filename": "b.png", "dir_path": "sub",
             "root_id": media_library._root_id(str(self.root)), "size": 200, "mtime": 1728000001.0,
             "extra_json": json.dumps({"seed": None, "sampler": "Euler a", "scheduler": ""})},
            {"path": "F:/v1/c.mp4", "relpath": "c.mp4", "filename": "c.mp4", "ext": ".mp4", "kind": "video",
             "root_id": media_library._root_id(str(self.root)), "size": 300, "mtime": 1728000002.0,
             "extra_json": "{}"},
        ])
        lib = media_library.MediaLibrary(path)                          # 打开即迁移，不碰扫描
        self.assertEqual(self.calls, [])                                # 没有偷偷要求重扫（§15.2.2）
        with contextlib.closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
            idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        for name in ("seed", "steps", "cfg", "sampler", "scheduler", "fps"):
            self.assertIn(name, cols)
        self.assertIn("ix_items_sampler", idx)
        self.assertIn("ix_items_scheduler", idx)
        a = self.params_of(path, "a.png")
        self.assertEqual((a["seed"], a["steps"], a["cfg"], a["sampler"], a["scheduler"], a["fps"]),
                         (1234, 28, 6.5, "DPM++ 2M Karras", "exponential", 24.0))
        b = self.params_of(path, "sub/b.png")
        self.assertEqual((b["seed"], b["cfg"], b["sampler"], b["scheduler"], b["fps"]),
                         (None, None, "Euler a", "", None))             # 键缺失/JSON null → NULL，不伪造 0
        c = self.params_of(path, "c.mp4")
        self.assertEqual((c["seed"], c["sampler"], c["scheduler"]), (None, "", ""))
        # 既有数据一个都不许丢：收藏、提示词、加入时间、标签
        self.assertEqual((a["starred"], a["prompt"], a["added_at"]), (1, "1girl, old", 1728000000.0))
        by_rel = {i["relpath"]: i for i in lib.list_items({"limit": 500})["items"]}
        self.assertEqual(len(by_rel), 3)
        self.assertEqual([t["name"] for t in by_rel["a.png"]["tags"]], ["精选"])
        self.assertEqual(lib.list_items({})["total"], 3)
        self.assertEqual(lib.facets()["tags"], [{"id": 11, "name": "精选", "color": "#888", "count": 1}])
        self.assertEqual(lib.facets()["samplers"], [{"name": "DPM++ 2M Karras", "count": 1},
                                                    {"name": "Euler a", "count": 1}])
        self.assertEqual([i["relpath"] for i in lib.list_items({"samplers": ["Euler a"]})["items"]],
                         ["sub/b.png"])                                   # 迁移完立即可筛
        self.assertEqual([i["relpath"] for i in lib.list_items({"sizes": ["768x1280"]})["items"]],
                         ["a.png"])                                       # 尺寸来自既有 width/height 列
        self.assertEqual(lib.detail(1)["seed"], 1234)                      # 详情走列，不用再解析文件
        self.assertEqual(lib.stats()["total"], 3)
        # 老库没有 items_fts：迁移后建 FTS 必须 rebuild，否则搜索空命中、重解析还会撞 malformed
        self.assertEqual([i["relpath"] for i in lib.list_items({"q": "signature"})["items"]], ["a.png"])

    def test_migration_is_idempotent_on_reopen(self):
        path = self.build_v1([
            {"path": "F:/v1/a.png", "relpath": "a.png", "filename": "a.png",
             "root_id": media_library._root_id(str(self.root)), "size": 1, "mtime": 1728000000.0,
             "extra_json": json.dumps({"seed": 1234, "sampler": "DPM++ 2M Karras"})},
        ])
        media_library.MediaLibrary(path)
        first = self.params_of(path, "a.png")
        with contextlib.closing(sqlite3.connect(path)) as conn:          # 模拟迁移后又写进库的新数据
            conn.execute("UPDATE items SET seed = 4242, sampler = 'Restart' WHERE relpath = 'a.png'")
            conn.commit()
        for _ in range(2):                                               # 二次/三次打开都不许回滚数据
            reopened = media_library.MediaLibrary(path)
            after = self.params_of(path, "a.png")
            self.assertEqual((after["seed"], after["sampler"]), (4242, "Restart"))
            self.assertEqual(after["starred"], first["starred"])
            self.assertEqual(reopened.list_items({})["total"], 1)
            self.assertEqual(reopened.facets()["samplers"], [{"name": "Restart", "count": 1}])
        with contextlib.closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key = 'db_version'").fetchone()[0], "2")

    def test_migrated_rows_reparse_once_then_become_no_op(self):
        """迁移把 extra_json 的旧值提到列上；参数回收/帧率是新提取器才有的字段，
        所以迁移后的第一轮扫描必须重解析一次（§15.1 版本门控），之后才是 keep。"""
        row = self.file_row("m.png", {"seed": 5, "steps": 6, "cfg": 7.5, "sampler": "Euler a",
                                      "scheduler": "normal", "fps": None})
        path = self.build_v1([row])
        lib = media_library.MediaLibrary(path)
        result = lib.scan([str(self.root)])
        self.assertEqual(result["reparse"], 1)                          # 老行被标记补解析
        self.assertEqual((result["added"], result["updated"], result["unchanged"]), (0, 1, 0))
        self.assertEqual(len(self.calls), 1)
        self.calls.clear()
        self.assertEqual(lib.scan([str(self.root)])["reparse"], 0)       # 版本已落盘，不再重复补
        self.assertEqual(self.calls, [])                                 # 未变文件保持 keep
        after = self.params_of(path, "m.png")                            # 夹具 DEFAULT_META 覆盖列值
        self.assertEqual((after["seed"], after["sampler"]), (123, "DPM++ 2M"))
        os.utime(self.root / "m.png", (time.time() + 600, time.time() + 600))
        result = lib.scan([str(self.root)])
        self.assertEqual(result["updated"], 1)
        self.assertEqual(len(self.calls), 1)                             # 文件真变了才重解析

    def test_without_json1_columns_stay_empty_and_scan_refills_them(self):
        """没编 JSON1 的 sqlite：json_extract 报 OperationalError → 整库 meta_mtime=0 交给增量扫描（§15.2.2）。

        这里用一条必然抛 OperationalError 的语句代替回填 SQL，模拟的正是「回填不可用」这一可观测症状，
        而不是去改实现；迁移本身必须照常完成（加列 + user_version=2）。
        """
        rows = [self.file_row("j1.png", {"seed": 1, "sampler": "Omni", "scheduler": "simple"}),
                self.file_row("sub/j2.mp4", {"seed": 2, "sampler": "Omni", "scheduler": "simple"})]
        path = self.build_v1(rows)
        with patch.object(media_library, "_V2_BACKFILL_SQL",
                          "UPDATE items SET seed = json_extract_missing(extra_json, '$.seed')"):
            lib = media_library.MediaLibrary(path)                       # 不许抛
        self.assertEqual(self.calls, [])                                 # 迁移自己不做任何解析
        with contextlib.closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIn("seed", {r[1] for r in conn.execute("PRAGMA table_info(items)")})
        cols = {r["relpath"]: (r["seed"], r["sampler"], r["meta_mtime"]) for r in self._read_params(path)}
        self.assertEqual(cols["j1.png"], (None, "", 0.0))                 # 回填没跑成 → 列保持空
        self.assertEqual(cols["sub/j2.mp4"], (None, "", 0.0))             # meta_mtime=0 = 待重解析
        result = lib.scan([str(self.root)])
        self.assertEqual(result["updated"], 2)                            # 增量扫描逐行重解析补回
        cols = {r["relpath"]: (r["seed"], r["sampler"], r["scheduler"], r["meta_mtime"])
                for r in self._read_params(path)}
        self.assertEqual(cols["j1.png"][:3], (123, "DPM++ 2M", "karras"))  # 夹具 DEFAULT_META 的值
        self.assertEqual(cols["sub/j2.mp4"][:3], (123, "DPM++ 2M", "karras"))
        self.assertGreater(cols["j1.png"][3], 0.0)                        # 重解析后回到正常对账时刻
        facets = lib.facets()
        self.assertEqual(facets["samplers"], [{"name": "DPM++ 2M", "count": 2}])   # 分面重新有内容
        self.assertEqual(sorted(i["relpath"] for i in lib.list_items({"samplers": ["DPM++ 2M"]})["items"]),
                         ["j1.png", "sub/j2.mp4"])

    def _read_params(self, path):
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT relpath, seed, steps, cfg, sampler, scheduler, fps, meta_mtime FROM items "
                "ORDER BY relpath")]

    def test_malformed_extra_json_row_is_deferred_not_fatal(self):
        rows = [self.file_row("ok.png", {"seed": 1, "sampler": "Omnice"}),
                self.file_row("bad.png", {})]
        rows[1]["extra_json"] = "{坏 json"                                # 老库里被人手改坏的一行
        path = self.build_v1(rows)
        with contextlib.closing(sqlite3.connect(path)) as conn:      # 迁移前只有老列，按老列读对账时刻
            stamp_before = conn.execute("SELECT meta_mtime FROM items WHERE relpath = 'ok.png'").fetchone()[0]
        lib = media_library.MediaLibrary(path)                            # 一行坏值不得拖垮整库回填
        ok = self.params_of(path, "ok.png")
        self.assertEqual((ok["seed"], ok["sampler"], ok["scheduler"]), (1, "Omnice", ""))
        self.assertEqual(ok["meta_mtime"], stamp_before)                   # 好行不动
        bad = self.params_of(path, "bad.png")
        self.assertEqual((bad["seed"], bad["sampler"]), (None, ""))
        self.assertEqual(bad["meta_mtime"], 0.0)                           # 坏行留给增量扫描
        with contextlib.closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(lib.list_items({})["total"], 2)
        # 隔离版本门控（§15.1 会把老行整体标一次补解析）：这里只验「坏行」这一条路径
        with contextlib.closing(sqlite3.connect(path)) as conn:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('extract_version', ?)",
                         (str(media_library.MediaLibrary._extract_version()),))
            conn.commit()
        self.assertEqual(lib.scan([str(self.root)])["updated"], 1)         # 只有坏行被重解析
        self.assertEqual(self.params_of(path, "bad.png")["sampler"], "DPM++ 2M")


class ExtractVersionTests(LibraryCase):
    """§15.1 提取语义版本：老库必须自己补解析。

    增量扫描按 mtime+size 对账，文件没改就永不重解析；v1 迁移又只是把 extra_json 的旧值提到列上。
    没有这层版本标记，按字段名回收的参数与 fps 在已建库的用户机上永远补不上。
    """

    NEW_VER = str(media_library.MediaLibrary._extract_version())

    def _force_old_version(self, value=1):
        with contextlib.closing(sqlite3.connect(self.lib.path)) as conn:
            conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('extract_version', ?)",
                         (str(value),))
            conn.commit()                                    # 不 commit 的话关闭即回滚，测试等于没写

    def _version(self):
        return self.lib.meta_value("extract_version")

    def test_stale_version_reparses_and_stamps_after_success(self):
        self.make("a.png")
        self.make("b.mp4")
        self.scan()
        self.calls.clear()
        self._force_old_version()
        result = self.scan()
        self.assertEqual(result["reparse"], 2)                       # 两行都被标记补解析
        self.assertEqual(sorted(os.path.basename(p) for p in self.calls), ["a.png", "b.mp4"])
        self.assertEqual(result["updated"], 2)                       # 已有行重解析算 updated
        self.assertEqual(self._version(), self.NEW_VER)

    def test_current_version_does_not_reparse(self):
        self.make("a.png")
        first = self.scan()
        self.assertEqual(first["reparse"], 0)                        # 新建库直接就是当前版本
        self.calls.clear()
        self.assertEqual(self.scan()["reparse"], 0)
        self.assertEqual(self.calls, [])                             # 未变化的文件不许被重扫

    def test_image_without_meta_is_skipped(self):
        self.meta_map["plain.jpg"] = {"has_meta": False, "source": "none", "prompt": "",
                                      "negative": "", "checkpoint": "", "loras": [], "models": [],
                                      "sampler": "", "scheduler": "", "seed": None, "steps": None,
                                      "cfg": None, "extra": {"ext": ".jpg"}}
        self.make("plain.jpg")
        self.make("v.mp4")
        self.scan()
        self.calls.clear()
        self._force_old_version()
        result = self.scan()
        self.assertEqual(result["reparse"], 1)                       # 无元数据的图片不值得重解析
        self.assertEqual([os.path.basename(p) for p in self.calls], ["v.mp4"])

    def test_cancelled_scan_leaves_version_for_next_round(self):
        for index in range(8):
            self.make("f%02d.png" % index)
        self.scan()
        self._force_old_version()
        self.calls.clear()
        result = self.scan(cancel=lambda: len(self.calls) >= 2)
        self.assertEqual(result["reparse"], 8)
        self.assertEqual(self._version(), "1")                       # 没跑完就宣称已补解析是撒谎
        self.calls.clear()
        again = self.scan()
        # 被打断的那轮把版本标记留在旧值，下一轮整批重标：已解析的 2 行会被再解析一次，
        # 这是「宁可重做也不留永久空白」的取舍（解析幂等，代价有界）。
        self.assertEqual(again["reparse"], 8)
        self.assertEqual(self._version(), self.NEW_VER)

    def test_extension_only_version_reparses_only_those_files(self):
        """只改了 webm/mkv 解析的那一版不许拖着整库重扫：真实库 2792 行、这类文件 0 个，白扫 31 秒。"""
        self.make("a.png")
        self.make("b.mp4")
        self.make("c.webm")
        self.make("d.mkv")
        self.scan()
        self.calls.clear()
        self._force_old_version(int(self.NEW_VER) - 1)              # 只跨过「定向」那一版
        result = self.scan()
        self.assertEqual(result["reparse"], 2)
        self.assertEqual(sorted(os.path.basename(p) for p in self.calls), ["c.webm", "d.mkv"])
        self.assertEqual(self._version(), self.NEW_VER)

    def test_stale_stmt_falls_back_to_whole_library_across_unscoped_version(self):
        """跨过整库级版本（哪怕同时跨过定向版本）必须回到整库口径：宁可多标不可漏标。"""
        whole, scoped = media_library._EXTRACT_STALE_REPARSE_SQL, media_library._EXTRACT_SCOPED_REPARSE
        self.assertTrue(scoped, "定向表被清空后这条用例就失去意义了")
        only = min(scoped)
        sql, params = media_library._stale_reparse_stmt(only - 1, only)
        self.assertIn("ext IN", sql)
        self.assertEqual(params, tuple(sorted(scoped[only])))
        for past in range(1, only - 1):                             # 从更早的整库级版本跨过来
            self.assertEqual(media_library._stale_reparse_stmt(past, only), (whole, ()))
        self.assertEqual(media_library._stale_reparse_stmt(only, only), (whole, ()))   # 无需补解析


class BatchOperationsTests(LibraryCase):
    """§16 批量层：未知 id 一律忽略、加标签是并集、摘标签只摘指定的那几个。"""

    def _ids(self):
        return [i["id"] for i in self.lib.list_items({})["items"]]

    def test_starred_batch_counts_only_existing_rows(self):
        for index in range(4):
            self.make("s%d.png" % index)
        self.scan()
        ids = self._ids()
        self.assertEqual(len(ids), 4)
        self.assertEqual(self.lib.set_starred(ids[:2] + [99999], True), 2)   # 库里没有的 id 不算数
        self.assertEqual(self.lib.facets()["starred"], 2)
        self.assertEqual(self.lib.set_starred(ids, False), 4)
        self.assertEqual(self.lib.facets()["starred"], 0)

    def test_add_tags_is_union_and_remove_is_targeted(self):
        self.make("t1.png")
        self.make("t2.png")
        self.scan()
        ids = self._ids()
        ta = self.lib.save_tag(0, "A", "", -1)
        tb = self.lib.save_tag(0, "B", "", -1)
        tc = self.lib.save_tag(0, "C", "", -1)
        self.lib.set_tags(ids[0], [ta])                        # t1 已经有 A
        got = self.lib.add_tags(ids, [tb, 99999])              # 不存在的 tag_id 不写脏关联
        self.assertEqual({i["id"] for i in got[ids[0]]}, {ta, tb})   # 原有 A 保留 = 并集
        self.assertEqual({i["id"] for i in got[ids[1]]}, {tb})
        after = self.lib.remove_tags([ids[0]], [tb])
        self.assertEqual({i["id"] for i in after[ids[0]]}, {ta})     # 只摘掉 B
        self.assertEqual({i["id"] for i in self.lib.add_tags([ids[0]], []).get(ids[0], [])}, {ta})

    def test_empty_and_garbage_batches_write_nothing(self):
        self.make("e.png")
        self.scan()
        tid = self.lib.save_tag(0, "A", "", -1)
        self.assertEqual(self.lib.set_starred([], True), 0)
        self.assertEqual(self.lib.add_tags([], [tid]), {})
        self.assertEqual(self.lib.add_tags([99999], [tid]), {})       # 条目不存在 → 不建关联
        self.assertEqual(self.lib.list_tags()[0]["count"], 0)


class MovedRootTests(LibraryCase):
    """§17 搬家重连：根目录改名/换盘符后，收藏与标签不许整片留在旧行上陪葬。"""

    def setUp(self):
        super().setUp()
        self.root2 = Path(self._tmp.name) / "output2"
        self.root2.mkdir()
        self.root3 = Path(self._tmp.name) / "output3"
        self.root3.mkdir()

    def _place(self, root, rel, content=b"payload"):
        path = root / rel.replace("/", os.sep)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        os.utime(path, (time.time() - 5000, time.time() - 5000))
        return path

    def _rid(self, root):
        return media_library.root_id(str(root))

    def _ids(self, flt=None):
        return {i["relpath"]: i["id"] for i in
                self.lib.list_items(dict(flt or {}))["items"]}

    def test_moved_directory_carries_star_and_tags(self):
        self._place(self.root, "a/1.png")
        self._place(self.root, "2.png")
        self.lib.scan([str(self.root)])
        old = self._ids()
        tid = self.lib.save_tag(0, "精选", "", -1)
        self.lib.set_star(old["a/1.png"], True)
        self.lib.set_tags(old["2.png"], [tid])

        for rel in ("a/1.png", "2.png"):                       # 整目录搬家，旧位置消失
            src = self.root / rel.replace("/", os.sep)
            dst = self.root2 / rel.replace("/", os.sep)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
        shutil.rmtree(str(self.root))

        result = self.lib.scan([str(self.root2)])
        self.assertEqual(result["relinked"], 2)
        new = self._ids({"root_ids": [self._rid(self.root2)]})
        self.assertEqual(self.lib.detail(new["a/1.png"])["starred"], 1)
        self.assertEqual([t["name"] for t in self.lib.detail(new["2.png"])["tags"]], ["精选"])
        # 旧行不删（§13.14 的「加回即恢复」不许被破坏）：库里 4 行，作用域内只有 2 行
        self.assertEqual(self.lib.list_items({})["total"], 4)
        self.assertEqual(self.lib.list_items({"root_ids": [self._rid(self.root2)]})["total"], 2)

    def test_ambiguous_destination_is_not_guessed(self):
        self._place(self.root, "x.png")
        self.lib.scan([str(self.root)])
        old = self._ids()
        self.lib.set_star(old["x.png"], True)
        shutil.copy2(str(self.root / "x.png"), str(self.root2 / "x.png"))
        shutil.copy2(str(self.root / "x.png"), str(self.root3 / "x.png"))   # 两个在管根都有同一份
        shutil.rmtree(str(self.root))
        result = self.lib.scan([str(self.root2), str(self.root3)])
        self.assertEqual(result["relinked"], 0)                # 一对二：不猜
        scoped = self.lib.facets(root_ids=[self._rid(self.root2), self._rid(self.root3)])
        self.assertEqual(scoped["starred"], 0)                 # 新行谁都没收到收藏
        self.assertEqual(self.lib.facets()["starred"], 1)      # 旧行原样保留（§13.14）

    def test_two_live_libraries_are_not_treated_as_a_move(self):
        self._place(self.root, "y.png")
        self.lib.scan([str(self.root)])
        self.lib.set_star(self._ids()["y.png"], True)
        self._place(self.root2, "y.png")                       # 另一套库里有同名同尺寸文件
        result = self.lib.scan([str(self.root), str(self.root2)])
        self.assertEqual(result["relinked"], 0)                # 旧文件还在原地 → 不是搬家
        self.assertEqual(self.lib.list_items({})["total"], 2)
        self.assertEqual(self.lib.facets()["starred"], 1)      # 只有原来那一行是收藏

    def test_relink_is_idempotent_and_ignores_missing_scope(self):
        self._place(self.root, "z.png")
        self.lib.scan([str(self.root)])
        self.lib.set_star(self._ids()["z.png"], True)
        shutil.copy2(str(self.root / "z.png"), str(self.root2 / "z.png"))
        shutil.rmtree(str(self.root))
        self.assertEqual(self.lib.scan([str(self.root2)])["relinked"], 1)
        again = self.lib.scan([str(self.root2)])               # 旧行仍在库里但已复制过
        self.assertEqual(again["relinked"], 1)                 # 复制是幂等的（INSERT OR IGNORE）
        self.assertEqual(self.lib.detail(self._ids({"root_ids": [self._rid(self.root2)]})["z.png"])["starred"], 1)


class AnnotationTransferTests(LibraryCase):
    """§18 标注导出/导入：收藏与标签目前只活在 media_library.db 一个文件里，必须有出路。"""

    def setUp(self):
        super().setUp()
        self.root2 = Path(self._tmp.name) / "output2"
        self.root2.mkdir()

    def _curate(self):
        for rel in ("a/1.png", "a/2.png", "3.mp4"):
            self.make(rel)
        self.scan()
        ids = {i["relpath"]: i["id"] for i in self.lib.list_items({})["items"]}
        parent = self.lib.save_tag(0, "场景", "", -1)
        child = self.lib.save_tag(0, "夜景", "#ff8800", parent)
        self.lib.set_star(ids["a/1.png"], True)
        self.lib.set_tags(ids["a/2.png"], [child])
        return ids

    def test_export_only_carries_human_annotations(self):
        self._curate()
        doc = self.lib.export_annotations()
        self.assertEqual(doc["kind"], "cft-media-annotations")
        self.assertEqual(len(doc["items"]), 2)                 # 没被人工动过的 3.mp4 不进档案
        self.assertEqual({i["relpath"] for i in doc["items"]}, {"a/1.png", "a/2.png"})
        night = [t for t in doc["tags"] if t["name"] == "夜景"][0]
        self.assertEqual(night["parent_path"], ["场景"])         # 祖先按列表带走，不按 "父/子" 拼串
        self.assertEqual(night["color"], "#ff8800")
        self.assertNotIn("prompt", doc["items"][0])            # 机器解析出来的元数据不属于标注

    def test_tag_name_containing_slash_survives_roundtrip(self):
        ids = self._curate()
        slash = self.lib.save_tag(0, "A/B", "", -1)             # 名字里带 "/" 是合法的
        self.lib.set_tags(ids["3.mp4"], [slash])
        doc = self.lib.export_annotations()
        self.lib.import_annotations(doc, dry_run=False)
        names = {t["name"] for t in self.lib.list_tags()}
        self.assertIn("A/B", names)
        self.assertNotIn("A", names)                            # 绝不被拆成两层
        self.assertEqual({t["name"] for t in self.lib.detail(ids["3.mp4"])["tags"]}, {"A/B"})

    def test_dry_run_counts_match_the_real_import(self):
        self._curate()
        doc = self.lib.export_annotations()
        with self.lib._db() as conn:
            conn.execute("DELETE FROM item_tags")
            conn.execute("DELETE FROM tags")
            conn.execute("UPDATE items SET starred = 0")
        preview = self.lib.import_annotations(doc, dry_run=True)
        applied = self.lib.import_annotations(doc, dry_run=False)
        for key in ("matched", "starred", "tags_applied", "tags_created", "unmatched", "ambiguous"):
            self.assertEqual(preview[key], applied[key], key)   # 试算说多少，真写就多少

    def test_roundtrip_into_rebuilt_index(self):
        ids = self._curate()
        doc = self.lib.export_annotations()
        # 模拟索引被清空重建（换机 / 删库重扫）
        with self.lib._db() as conn:
            conn.execute("DELETE FROM item_tags")
            conn.execute("DELETE FROM tags")
            conn.execute("UPDATE items SET starred = 0")
        self.assertEqual(self.lib.facets()["starred"], 0)

        preview = self.lib.import_annotations(doc, dry_run=True)
        self.assertEqual((preview["matched"], preview["dry_run"]), (2, True))
        self.assertEqual(self.lib.facets()["starred"], 0)      # 试算绝不写库

        done = self.lib.import_annotations(doc, dry_run=False)
        self.assertEqual(done["matched"], 2)
        self.assertEqual(done["starred"], 1)
        self.assertEqual(self.lib.facets()["starred"], 1)
        tags = {t["name"]: t for t in self.lib.list_tags()}
        self.assertIn("夜景", tags)
        self.assertEqual(tags["夜景"]["color"], "#ff8800")      # 颜色按档案重建
        self.assertEqual(tags["夜景"]["parent_id"], tags["场景"]["id"])
        got = self.lib.detail(ids["a/2.png"])["tags"]
        self.assertEqual([t["name"] for t in got], ["夜景"])

    def test_matching_is_by_relpath_then_size_with_unique_fallback(self):
        self._curate()
        doc = self.lib.export_annotations()
        doc["items"][0]["size"] = 12345                        # 文件被就地改写过：大小对不上
        stats = self.lib.import_annotations(doc, dry_run=False)
        self.assertEqual((stats["matched"], stats["unmatched"]), (2, 0))   # relpath 唯一 → 仍认得

    def test_ambiguous_and_unknown_are_reported_not_guessed(self):
        self._curate()
        doc = self.lib.export_annotations()
        doc["items"].append({"relpath": "nope.png", "filename": "nope.png",
                             "ext": ".png", "size": 7, "starred": 1, "tags": []})
        doc["items"].append({"relpath": "a/1.png", "filename": "1.png",
                             "ext": ".png", "size": 99999, "starred": 1, "tags": []})
        with self.lib._db() as conn:                           # 造一个 relpath 撞车的第二根
            conn.execute("INSERT INTO items(path, root_id, relpath, dir_path, filename, ext, kind,"
                         " size, mtime, meta_mtime, added_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (os.path.join(self.root2, "a/1.png"), "other-root", "a/1.png", "a",
                          "1.png", ".png", "image", 7, 1.0, 1.0, 1.0))
        stats = self.lib.import_annotations(doc, dry_run=True)
        self.assertEqual(stats["unmatched"], 1)                # 库里根本没有的文件
        self.assertGreaterEqual(stats["ambiguous"], 1)         # relpath 对不上 size 且撞车 → 不猜

    def test_import_never_removes_existing_curation(self):
        ids = self._curate()
        other = self.lib.save_tag(0, "别的", "", -1)
        self.lib.set_tags(ids["3.mp4"], [other])
        doc = {"kind": "cft-media-annotations", "version": 1,
               "items": [{"relpath": "a/1.png", "filename": "1.png", "ext": ".png",
                          "size": os.path.getsize(self.root / "a/1.png"), "starred": 0, "tags": []}]}
        self.lib.import_annotations(doc, dry_run=False)
        counts = {t["name"]: t["count"] for t in self.lib.list_tags()}
        self.assertEqual(counts["别的"], 1)                     # 档案没提的标签不受影响
        self.assertEqual(self.lib.detail(ids["a/2.png"])["tags"][0]["name"], "夜景")   # 档案没提的不动

    def test_bad_payload_is_rejected(self):
        self._curate()
        for bad in (None, {}, {"kind": "别的"}, {"kind": "cft-media-annotations", "items": "x"}):
            stats = self.lib.import_annotations(bad, dry_run=False)
            self.assertIn("msg", stats)
            self.assertEqual(stats.get("matched", 0), 0)


if __name__ == "__main__":
    unittest.main()
