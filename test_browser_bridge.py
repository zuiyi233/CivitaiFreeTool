# -*- coding: utf-8 -*-
"""browser_bridge 单测：扩展 POST /api/download → handler 入队下载 全链路（临时 handler，不碰真实下载）"""
import json
import os
import unittest
import urllib.request

import browser_bridge

PORT = 47539  # 测试端口，避开默认 47531


class FakeAPI(__import__("civitai_api").CivitaiAPI):
    """mock CivitaiAPI：resolve/get_model/get_model_version/pick_file 全部本地返回"""
    def resolve_url(self, url):
        return ("123", None)

    def get_model(self, mid):
        return {"name": "Test Model", "modelVersions": [{"id": 456}]}

    def get_model_version(self, vid):
        return {"id": vid, "name": "v1", "files": [{"name": "test.safetensors", "primary": True,
                                         "downloadUrl": "https://civitai.com/api/download/models/456",
                                         "hashes": {"SHA256": "abc123"}}]}

    def pick_file(self, version):
        return version["files"][0], 0

    def build_download_url(self, version_id, file_id=None):
        return "https://civitai.com/api/download/models/%s" % version_id


def req(path, method="GET", body=None, origin=None):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if origin:
        r.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(r, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.received = []
        browser_bridge.set_download_handler(cls._fake_dl)
        browser_bridge.start(version="test", port=PORT)

    @classmethod
    def _fake_dl(cls, url, dest_subdir=""):
        """模拟 dl_enqueue_url：记录 URL 并同步返回（两参兼容归档桥 dest_subdir 透传）"""
        cls.received.append(url)
        if url.startswith("https://civitai.red/models/bad"):
            return {"ok": False, "msg": "模型没有可用版本"}
        return {"started": True}

    def setUp(self):
        type(self).received.clear()

    def test_health(self):
        st, d = req("/api/health")
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(d["version"], "test")

    def test_ext_download_model_url(self):
        st, d = req("/api/download", "POST", {"url": "https://civitai.red/models/1290339/miaomiao-blocks"},
                    origin="chrome-extension://abcdef123456")
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(self.received, ["https://civitai.red/models/1290339/miaomiao-blocks"])

    def test_ext_download_version_url(self):
        st, d = req("/api/download", "POST",
                    {"url": "https://www.civitai.com/models/123?modelVersionId=456"},
                    origin="chrome-extension://abcdef123456")
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertIn("https://www.civitai.com/models/123?modelVersionId=456", self.received)

    def test_ext_download_api_url(self):
        st, d = req("/api/download", "POST", {"url": "https://civitai.red/api/download/models/789"},
                    origin="chrome-extension://abcdef123456")
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])

    def test_handler_rejects(self):
        st, d = req("/api/download", "POST", {"url": "https://civitai.red/models/bad/xx"},
                    origin="chrome-extension://abc")
        self.assertEqual(st, 400)
        self.assertFalse(d["ok"])

    def test_reject_non_model_url(self):
        for bad in ("https://civitai.red/images/12345",
                    "https://civitai.red/posts/1",
                    "https://civitai.red/",
                    "https://example.com/models/1",
                    "https://civitai.red/models"):
            st, d = req("/api/download", "POST", {"url": bad}, origin="chrome-extension://abc")
            self.assertEqual(st, 400, bad)
            self.assertFalse(d["ok"], bad)
        self.assertEqual(self.received, [])

    def test_reject_foreign_origin(self):
        st, d = req("/api/download", "POST", {"url": "https://civitai.red/models/1"},
                    origin="https://evil.example.com")
        self.assertEqual(st, 403)
        self.assertEqual(self.received, [])

    def test_reject_empty_and_bad_json(self):
        st, d = req("/api/download", "POST", {}, origin="chrome-extension://abc")
        self.assertEqual(st, 400)
        st, d = req("/api/download", "POST", {"nourl": 1}, origin="chrome-extension://abc")
        self.assertEqual(st, 400)

    def test_local_origin_allowed(self):
        st, d = req("/api/download", "POST", {"url": "https://civitai.red/models/9/z"},
                    origin="http://127.0.0.1:47531")
        self.assertEqual(st, 200)

    def test_no_handler(self):
        browser_bridge.set_download_handler(None)
        try:
            st, d = req("/api/download", "POST", {"url": "https://civitai.red/models/1/a"},
                        origin="chrome-extension://abc")
            self.assertEqual(st, 500)
            self.assertFalse(d["ok"])
        finally:
            browser_bridge.set_download_handler(self._fake_dl)


class SyncEnqueueTest(unittest.TestCase):
    """dl_enqueue_url_sync：同步解析 + 真实入队（FakeAPI，不碰网络与磁盘）"""

    def setUp(self):
        import webui
        import config, downloader, tempfile
        from unittest.mock import patch
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = self.temp.name
        cfg = dict(config.DEFAULTS, api_key="synthetic-test-key", download_dir=root, models_dir=root, models_dirs=[root],
                   download_target_dir="", download_cover=False, metadata_format="both")
        for item in (patch.object(config,"APP_DIR",root),patch.object(config,"TASKS_PATH",os.path.join(root,"tasks.json")),
                     patch.object(config,"HISTORY_PATH",os.path.join(root,"history.json")),patch.object(config,"load",return_value=cfg),
                     patch.object(downloader.Downloader,"_ensure_workers"),patch.object(browser_bridge,"start")):
            item.start();self.addCleanup(item.stop)
        self.app = webui.Api()
        self.app.api = FakeAPI()

    def test_sync_enqueue_ok(self):
        r = self.app.dl_enqueue_url_sync("https://civitai.red/models/123/abc")
        self.assertTrue(r["ok"], r)
        self.assertTrue(r.get("task_id"))
        # 任务真实入队（文件名 = 模型名 + 空格 + 版本名 + 扩展名）
        tasks = [t for t in self.app.dl.tasks if t.info and t.info.get("model_id") == "123"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].filename, "test.safetensors")
        # 关键：info.meta 必须存在（下载完成后生成 json + 封面缩略图的前提）
        meta = (tasks[0].info or {}).get("meta") or {}
        self.assertIn("info", meta)
        self.assertIn("sd", meta)
        self.assertTrue(meta["info"] and meta["sd"])
        # 下载链接用 build_download_url 生成
        self.assertTrue(tasks[0].url.startswith("https://"))

    def test_handle_dl_done_generates_metadata(self):
        """插件入队的任务下载完成后 → 自动生成 SD json（下载管理/模型管理缩略图依赖此链路）"""
        import os
        from downloader import ST_DONE
        r = self.app.dl_enqueue_url_sync("https://civitai.red/models/123/abc")
        self.assertTrue(r["ok"])
        task = next(t for t in self.app.dl.tasks if t.id == r["task_id"])
        # 把任务指向工作区内临时目录并放置占位文件，模拟下载完成
        d = self.temp.name
        os.makedirs(d, exist_ok=True)
        task.dest_dir = d
        task.info["dest_explicit"] = True
        dest = os.path.join(d, task.filename)
        with open(dest, "w") as f:
            f.write("placeholder")
        task.status = ST_DONE
        self.app._handle_dl_done(task)
        base = os.path.splitext(dest)[0]
        self.assertTrue(os.path.exists(base + ".json"), "SD json 未生成")
        import json as _json
        with open(base + ".json", encoding="utf-8") as f:
            sd = _json.load(f)
        self.assertEqual(sd.get("name"), "Test Model")
        # 清理
        for s in (".json", ".civitai.info", ".preview.png"):
            p = base + s
            if os.path.exists(p):
                os.remove(p)
        os.remove(dest)

    def test_sync_enqueue_failure(self):
        class BadAPI(FakeAPI):
            def resolve_url(self, url):
                raise __import__("civitai_api").CivitaiError("timed out")
        self.app.api = BadAPI()
        r = self.app.dl_enqueue_url_sync("https://civitai.red/models/123/abc")
        self.assertFalse(r["ok"])
        self.assertIn("代理", r["msg"])  # 友好错误提示含代理指引
    def test_sync_enqueue_with_dest_subdir(self):
        import tempfile
        archive_root = tempfile.mkdtemp(prefix="cft_dlsub_")
        self.app.cfg = dict(self.app.cfg, archive_root=archive_root, download_dir=archive_root,
                            site_domain="civitai.red")
        try:
            r = self.app.dl_enqueue_url_sync("https://civitai.red/models/123/abc",
                                             "mymodel/v123")
            self.assertTrue(r["ok"], r)
            task = next(t for t in self.app.dl.tasks if t.id == r["task_id"])
            self.assertEqual(os.path.normpath(task.dest_dir),
                             os.path.normpath(os.path.join(archive_root, "mymodel", "v123")))
        finally:
            import shutil
            shutil.rmtree(archive_root, ignore_errors=True)

    def test_sync_enqueue_escape_dest_subdir(self):
        import tempfile
        archive_root = tempfile.mkdtemp(prefix="cft_dlesc_")
        self.app.cfg = dict(self.app.cfg, archive_root=archive_root, download_dir=archive_root,
                            site_domain="civitai.red")
        try:
            for bad in ("..\\evil", "C:\\x", "\\\\srv\\share"):
                r = self.app.dl_enqueue_url_sync("https://civitai.red/models/123/abc", bad)
                self.assertFalse(r["ok"], bad)
                self.assertIn("非法路径", r["msg"], bad)
        finally:
            import shutil
            shutil.rmtree(archive_root, ignore_errors=True)


class RenameTest(unittest.TestCase):
    """rename_file：自定义改名必须保留扩展名（前缀含点不误判）"""

    def setUp(self):
        import webui
        self.dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".test_tmp", "rename")
        os.makedirs(self.dir, exist_ok=True)
        self.app = webui.Api()
        self.app.api = FakeAPI()

    def _mk(self, name):
        p = os.path.join(self.dir, name)
        with open(p, "wb") as f:
            f.write(b"x")
        return p

    def test_prefix_with_dot_keeps_ext(self):
        # 前缀含点（如 "v1.8 前缀 Kisaki"）→ 扩展名必须保留 .safetensors
        p = self._mk("Kisaki v1.0.safetensors")
        r = self.app.rename_file(p, "v1.8 前缀 Kisaki")
        self.assertTrue(r["ok"], r)
        new_path = os.path.join(self.dir, "v1.8 前缀 Kisaki.safetensors")
        self.assertTrue(os.path.exists(new_path), "扩展名丢失: " + str(os.listdir(self.dir)))
        self.assertFalse(os.path.exists(p))

    def test_plain_prefix_keeps_ext(self):
        p = self._mk("Kisaki.safetensors")
        r = self.app.rename_file(p, "我的前缀 Kisaki")
        self.assertTrue(r["ok"], r)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "我的前缀 Kisaki.safetensors")))

    def test_input_with_explicit_ext(self):
        p = self._mk("Kisaki.safetensors")
        r = self.app.rename_file(p, "新的名字.ckpt")
        self.assertTrue(r["ok"], r)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "新的名字.ckpt")))

    def test_sides_renamed(self):
        p = self._mk("Kisaki.safetensors")
        with open(os.path.join(self.dir, "Kisaki.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        r = self.app.rename_file(p, "新名")
        self.assertTrue(r["ok"], r)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "新名.safetensors")))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "新名.json")), "附属 json 未同步改名")

    def tearDown(self):
        import shutil
        if os.path.exists(self.dir):
            shutil.rmtree(self.dir, ignore_errors=True)


class ArchiveBridgeTest(unittest.TestCase):
    """归档桥：config/file/done/library/check（真实 Api 方法 + 临时目录，不碰真实下载）"""

    @classmethod
    def setUpClass(cls):
        import tempfile
        import webui
        cls.tmp = tempfile.mkdtemp(prefix="cft_archive_")
        # 跳过 Api.__init__（避免覆盖 BridgeTest 的 fake 下载 handler 与默认端口服务）；
        # 只给实例挂上归档链路依赖的最小属性
        app = webui.Api.__new__(webui.Api)
        app.cfg = {"archive_root": cls.tmp, "download_dir": cls.tmp,
                   "models_dirs": [cls.tmp], "models_dir": cls.tmp}
        app.model_rows = []
        app.mm_scan_state = {"running": False, "rows": [], "msg": ""}
        cls.app = app
        cls._register()
        # 幂等启动：unittest 按字母序先跑本类时服务可能尚未启动，start 已幂等
        browser_bridge.start(version="test", port=PORT)

    @classmethod
    def _register(cls):
        browser_bridge.set_archive_handlers(config_fn=cls.app.archive_config,
                                            write_fn=cls.app.archive_write,
                                            done_fn=cls.app.archive_done,
                                            check_fn=cls.app.library_check)

    @classmethod
    def tearDownClass(cls):
        import shutil
        browser_bridge.set_archive_handlers(None, None, None, None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_archive_config(self):
        st, d = req("/api/archive/config", origin="chrome-extension://abc")
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(d["archive_root"], self.tmp)
        self.assertEqual(d["download_dir"], self.tmp)

    def test_archive_config_fallback(self):
        # archive_root 为空时回落 download_dir
        self.app.cfg["archive_root"] = ""
        try:
            st, d = req("/api/archive/config", origin="chrome-extension://abc")
            self.assertEqual(st, 200)
            self.assertEqual(d["archive_root"], self.tmp)
        finally:
            self.app.cfg["archive_root"] = self.tmp

    def test_archive_file_ok(self):
        import base64
        blob = "归档测试内容".encode("utf-8")
        st, d = req("/api/archive/file", "POST",
                    {"rel_path": "mymodel/模型名.txt",
                     "data_b64": base64.b64encode(blob).decode("ascii")},
                    origin="chrome-extension://abc")
        self.assertEqual(st, 200, d)
        self.assertTrue(d["ok"])
        with open(d["path"], "rb") as f:
            self.assertEqual(f.read(), blob)

    def test_archive_file_escape(self):
        import base64
        b64 = base64.b64encode(b"x").decode("ascii")
        for bad in ("../x", "..\\x", "a/../../x", "C:/evil/x", "/abs/x", "\\\\srv\\x"):
            st, d = req("/api/archive/file", "POST",
                        {"rel_path": bad, "data_b64": b64},
                        origin="chrome-extension://abc")
            self.assertEqual(st, 400, bad)
            self.assertFalse(d["ok"], bad)

    def test_archive_done(self):
        st, d = req("/api/archive/done", "POST", {"dir_name": "mymodel"},
                    origin="chrome-extension://abc")
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])

    def test_archive_done_empty_dir_name(self):
        for bad in ({}, {"dir_name": ""}):
            st, d = req("/api/archive/done", "POST", bad,
                        origin="chrome-extension://abc")
            self.assertEqual(st, 400, str(bad))
            self.assertFalse(d["ok"], str(bad))

    def test_archive_done_async(self):
        """archive_done 必须 daemon 线程执行 scan_models、立即返回（防 hang）"""
        import threading
        import time
        import webui
        app = webui.Api.__new__(webui.Api)
        started = threading.Event()

        def slow_scan():
            started.set()
            time.sleep(5)

        app.scan_models = slow_scan
        t0 = time.time()
        r = app.archive_done("mymodel")
        dt = time.time() - t0
        self.assertTrue(r.get("ok"))
        self.assertLess(dt, 2, "archive_done 同步阻塞 scan_models")
        self.assertTrue(started.wait(timeout=5), "scan_models 未在后台线程执行")
        alive = [t for t in threading.enumerate()
                 if t is not threading.current_thread() and t.daemon and t.is_alive()]
        self.assertTrue(any(getattr(t, "_target", None) is not None for t in alive)
                        or started.is_set())

    def test_archive_file_bad_base64(self):
        st, d = req("/api/archive/file", "POST",
                    {"rel_path": "mymodel/a.txt", "data_b64": "!!!not-base64!!!"},
                    origin="chrome-extension://abc")
        self.assertEqual(st, 400)
        self.assertFalse(d["ok"])

    def test_archive_file_empty_rel_path(self):
        import base64
        b64 = base64.b64encode(b"x").decode("ascii")
        st, d = req("/api/archive/file", "POST",
                    {"rel_path": "", "data_b64": b64},
                    origin="chrome-extension://abc")
        self.assertEqual(st, 400)
        self.assertFalse(d["ok"])

    def test_archive_file_too_large(self):
        import base64
        import browser_bridge as _bb
        old = _bb._ARCHIVE_RAW_MAX
        _bb._ARCHIVE_RAW_MAX = 4  # 收紧上限，仅本用例
        try:
            b64 = base64.b64encode(b"12345").decode("ascii")
            st, d = req("/api/archive/file", "POST",
                        {"rel_path": "mymodel/big.bin", "data_b64": b64},
                        origin="chrome-extension://abc")
            self.assertEqual(st, 400)
            self.assertFalse(d["ok"])
        finally:
            _bb._ARCHIVE_RAW_MAX = old

    def test_safe_archive_path_rejects_escape(self):
        import webui
        root = self.tmp
        for bad in ("..\\evil", "C:\\x", "\\\\srv\\share", "../x", "C:/evil/x", "/abs/x"):
            with self.assertRaises(ValueError, msg=bad):
                webui.Api._safe_archive_path(root, bad)

    def test_check_found(self):
        with open(os.path.join(self.tmp, "hit.civitai.info"), "w", encoding="utf-8") as f:
            json.dump({"versionId": 777888}, f)
        st, d = req("/api/library/check?version_id=777888", origin="chrome-extension://abc")
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertTrue(d["found"])
        self.assertTrue(any(p.endswith("hit.civitai.info") for p in d["paths"]))

    def test_check_not_found(self):
        st, d = req("/api/library/check?version_id=999000111", origin="chrome-extension://abc")
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertFalse(d["found"])
        self.assertEqual(d["paths"], [])

    def test_no_archive_handler(self):
        browser_bridge.set_archive_handlers(None, None, None, None)
        try:
            st, d = req("/api/archive/config", origin="chrome-extension://abc")
            self.assertEqual(st, 500)
            self.assertFalse(d["ok"])
            import base64
            st, d = req("/api/archive/file", "POST",
                        {"rel_path": "a.txt", "data_b64": base64.b64encode(b"x").decode("ascii")},
                        origin="chrome-extension://abc")
            self.assertEqual(st, 500)
            self.assertFalse(d["ok"])
            st, d = req("/api/library/check?version_id=1", origin="chrome-extension://abc")
            self.assertEqual(st, 500)
            self.assertFalse(d["ok"])
        finally:
            self._register()


class PathUtilTest(unittest.TestCase):
    """path_util 路径工具：norm_root / is_within / check_reachable"""

    def test_norm_root_drive_root_keeps_tail(self):
        """盘符根保尾：'Z:\\' 不得被削成 'Z:'（禁止 rstrip('/\\')）"""
        import path_util
        self.assertEqual(path_util.norm_root("Z:\\"), "Z:\\")

    def test_norm_root_unc_strips_tail(self):
        """UNC 去尾：'\\\\host\\share\\' → '\\\\host\\share'"""
        import path_util
        self.assertEqual(path_util.norm_root("\\\\host\\share\\"), "\\\\host\\share")

    def test_norm_root_normal_unchanged(self):
        """普通路径规范化后指向同一位置（语义不变）"""
        import shutil
        import tempfile
        import path_util
        d = tempfile.mkdtemp(prefix="cft_norm_")
        try:
            self.assertEqual(os.path.normpath(path_util.norm_root(d)), os.path.normpath(d))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_norm_root_empty(self):
        """空串 / None 返回 ''（永不抛异常）"""
        import path_util
        self.assertEqual(path_util.norm_root(""), "")
        self.assertEqual(path_util.norm_root(None), "")

    def test_is_within(self):
        """同路径 True / 大小写 True / 子路径 True / 兄弟 False / 空入参 False"""
        import shutil
        import tempfile
        import path_util
        root = tempfile.mkdtemp(prefix="cft_within_")
        try:
            child = os.path.join(root, "sub", "m.safetensors")
            self.assertTrue(path_util.is_within(root, root))  # 同路径（含相等）
            self.assertTrue(path_util.is_within(root, child))  # 子路径
            self.assertTrue(path_util.is_within(root.lower(), child.upper()))  # 大小写不敏感
            sibling = os.path.join(os.path.dirname(root),
                                   os.path.basename(root) + "_sibling", "x.bin")
            self.assertFalse(path_util.is_within(root, sibling))  # 兄弟目录（前缀陷阱）
            self.assertFalse(path_util.is_within("", child))  # 空 root
            self.assertFalse(path_util.is_within(root, ""))  # 空 child
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_check_reachable(self):
        """临时目录 exists+ok+writable；不存在路径 exists=False；返回 dict 键齐全"""
        import shutil
        import tempfile
        import path_util
        d = tempfile.mkdtemp(prefix="cft_reach_")
        try:
            r = path_util.check_reachable(d)
            for k in ("ok", "exists", "writable", "is_remote", "error"):
                self.assertIn(k, r)
            self.assertTrue(r["exists"])
            self.assertTrue(r["ok"])
            self.assertTrue(r["writable"])
            missing = os.path.join(d, "nonexistent_xyz")
            r2 = path_util.check_reachable(missing)
            for k in ("ok", "exists", "writable", "is_remote", "error"):
                self.assertIn(k, r2)
            self.assertFalse(r2["exists"])
            self.assertFalse(r2["ok"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class ModelRootsStatusTest(unittest.TestCase):
    """扫描根状态：_model_roots_with_status 分组 + _models_roots 只含可达目录"""

    def _make_app(self, ok_dir, missing):
        """仿 ArchiveBridgeTest：Api.__new__(Api) 免 __init__，只挂最小依赖"""
        import webui
        app = webui.Api.__new__(webui.Api)
        app.cfg = {"models_dirs": [ok_dir, missing], "models_dir": ok_dir,
                   "hidden_model_folders": [], "show_root_models": True,
                   "max_images_per_model": 5}
        app.model_rows = []
        app.mm_scan_state = {"running": False, "rows": [], "msg": ""}
        return app

    def test_roots_with_status_groups(self):
        """真实目录 + 不存在路径 → ok 1 项 / unreachable 1 项且 error 非空"""
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="cft_roots_")
        try:
            missing = os.path.join(tmp, "nonexistent_xyz")
            app = self._make_app(tmp, missing)
            st = app._model_roots_with_status()
            self.assertIsInstance(st, list)
            oks = [x for x in st if x.get("status") == "ok"]
            bads = [x for x in st if x.get("status") == "unreachable"]
            self.assertEqual(len(oks), 1, st)
            self.assertEqual(len(bads), 1, st)
            self.assertTrue(bads[0].get("error"), "不可达项 error 为空")
            for x in st:  # 条目结构：path / status / error
                for k in ("path", "status", "error"):
                    self.assertIn(k, x)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_models_roots_excludes_unreachable(self):
        """_models_roots 签名语义不变：只含可达目录"""
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="cft_roots_")
        try:
            missing = os.path.join(tmp, "nonexistent_xyz")
            app = self._make_app(tmp, missing)
            roots = app._models_roots()
            normed = [os.path.normpath(p) for p in roots]
            self.assertIn(os.path.normpath(tmp), normed)
            self.assertNotIn(os.path.normpath(missing), normed)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ScanRootsStateTest(unittest.TestCase):
    """scan_models roots 状态：mm_scan_state['roots'] 含 ok/unreachable，不可达根不中断"""

    def test_scan_roots_state(self):
        import shutil
        import tempfile
        import time
        import webui
        tmp = tempfile.mkdtemp(prefix="cft_scanroots_")
        try:
            missing = os.path.join(tmp, "nonexistent_xyz")
            # scan_models 真实实现依赖的最小字段（仿 ArchiveBridgeTest 免 __init__ 构造）
            app = webui.Api.__new__(webui.Api)
            app.cfg = {"models_dirs": [tmp, missing], "models_dir": tmp,
                       "hidden_model_folders": [], "show_root_models": True,
                       "max_images_per_model": 5}
            app.model_rows = []
            app.mm_scan_state = {"running": False, "rows": [], "msg": ""}
            r = app.scan_models()
            self.assertTrue(r.get("started"))
            deadline = time.time() + 30  # 后台线程扫描，轮询等完成（空目录秒级）
            while app.mm_scan_state.get("running") and time.time() < deadline:
                time.sleep(0.1)
            self.assertFalse(app.mm_scan_state.get("running"), "scan 线程 30s 内未结束")
            roots = app.mm_scan_state.get("roots")
            self.assertIsInstance(roots, list, "mm_scan_state 缺 'roots' 字段")
            self.assertTrue(any(x.get("status") == "ok" for x in roots), roots)
            bads = [x for x in roots if x.get("status") != "ok"]
            self.assertEqual(len(bads), 1, roots)
            self.assertTrue(bads[0].get("error"), "不可达项 error 为空")
            # 汇总 msg 保持现有格式（不可达根不得中断其他根扫描）
            self.assertTrue(app.mm_scan_state.get("msg", "").startswith("扫描完成"),
                            app.mm_scan_state.get("msg"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class CoverCacheTest(unittest.TestCase):
    """get_covers 缩略图缓存：未命中生成 / 二次命中一致 / mtime 变化换新键"""

    def _call(self, app, model, cache):
        """调 get_covers 并兼容返回格式（JSON 字符串或原生结构）"""
        import json
        r = app.get_covers([model], size=64, cache_dir=cache)
        return json.loads(r) if isinstance(r, str) else r

    def _assert_one_item(self, res, model):
        """断言返回恰 1 项封面数据（兼容 dict / list 两种格式）"""
        if isinstance(res, dict):
            self.assertEqual(len(res), 1, res)
            self.assertIn(model, res)
            self.assertTrue(res[model], "封面 b64 为空")
        else:
            self.assertIsInstance(res, list)
            self.assertEqual(len(res), 1, res)
            self.assertEqual(res[0].get("path"), model, res)
            self.assertTrue(res[0].get("b64"), "封面 b64 为空")

    def test_cover_cache_hit_and_rekey_on_mtime(self):
        """首次未命中生成 → 二次命中（出 jpg、结果一致）→ touch 后新键（文件数+1）"""
        import shutil
        import tempfile
        import time
        import webui
        from PIL import Image
        work = tempfile.mkdtemp(prefix="cft_cover_")
        cache = tempfile.mkdtemp(prefix="cft_thumb_")
        try:
            model = os.path.join(work, "m.safetensors")
            with open(model, "wb") as f:
                f.write(b"stub-model")
            cover = os.path.join(work, "m.jpg")  # find_cover 候选之一：同名 .jpg
            Image.new("RGB", (8, 8), (255, 0, 0)).save(cover, "JPEG")  # PIL 造 8x8 红图
            # get_covers 无重型 self 依赖，Api.__new__ 免 __init__ 仅补 cfg 与内存缓存
            app = webui.Api.__new__(webui.Api)
            app.cfg = {"models_dirs": [work], "models_dir": work}
            app._cover_cache = {}  # 上游 v2.2.x get_covers 新增的内存封面缓存

            def jpgs():
                return sorted(f for f in os.listdir(cache) if f.lower().endswith(".jpg"))

            self.assertEqual(jpgs(), [])  # 首次调用前缓存为空→未命中生成
            first = self._call(app, model, cache)
            self._assert_one_item(first, model)
            second = self._call(app, model, cache)
            self._assert_one_item(second, model)
            self.assertTrue(jpgs(), "二次调用后缓存目录无 .jpg")
            self.assertEqual(second, first, "缓存命中结果应与首次一致")
            before = set(os.listdir(cache))
            t = time.time() + 100  # 键含 int(mtime)，+100s 确保跨秒换键
            os.utime(cover, (t, t))
            third = self._call(app, model, cache)
            self._assert_one_item(third, model)
            after = set(os.listdir(cache))
            self.assertGreater(len(after), len(before), "mtime 变化后应生成新缓存键（文件数+1）")
        finally:
            shutil.rmtree(work, ignore_errors=True)
            shutil.rmtree(cache, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
