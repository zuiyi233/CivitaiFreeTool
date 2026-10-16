# -*- coding: utf-8 -*-
"""browser_bridge 单测：扩展 POST /api/download → handler 入队下载 全链路（临时 handler，不碰真实下载）

bundle 令牌层（分流落点凭据 + /api/archive/file 新形态 + 能力协商）同样全部离线：
假 api 只回本地字典，dl/scan 用 Mock 顶掉，所有落点都在临时目录，绝不发一个真实请求。"""
import json
import os
import tempfile
import time
import unittest
import urllib.request

import browser_bridge

PORT = 47539  # 兜底端口；实际端口由 boot_bridge() 向 OS 要（见下）


def _free_port():
    """让 OS 挑一个空闲端口（bind 到 0 再读回来），只为避开「固定端口被人占住」"""
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def boot_bridge():
    """启动桥服务并把真实端口写回模块级 PORT（全文件共用一个服务实例）

    为什么不再直接用固定 47539：本机并排跑测试的另一个进程（或上一轮没退干净的服务器）
    占住该端口时，本文件的 start() 会静默返回 0，而请求却打到「别人的服务器」上——
    handler 不是本文件注册的那个，用例会莫名其妙地红（实测过：/api/download 全 200）。
    """
    global PORT
    if browser_bridge._server is not None:
        PORT = browser_bridge.port() or PORT
        return PORT
    for _ in range(3):
        p = browser_bridge.start(version="test", port=_free_port())
        if p:
            PORT = p
            return p
    raise RuntimeError("桥服务起不来（端口不可用）")


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


class LoraFakeAPI(FakeAPI):
    """LoRA 类型：model_routing.classify 只看 model.type 就能定落点，不读文件头（零网络）"""
    def get_model(self, mid):
        return {"name": "Test Model", "type": "LORA", "modelVersions": [{"id": 456}]}

    def get_model_version(self, vid):
        return {"id": vid, "name": "v1", "baseModel": "PonyXL",
                "files": [{"name": "test.safetensors", "type": "Model", "primary": True,
                           "downloadUrl": "https://civitai.com/api/download/models/456",
                           "hashes": {"SHA256": "abc123"}}]}


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
        boot_bridge()

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
        # 幂等启动：unittest 按字母序先跑本类时服务可能尚未启动，boot_bridge 已幂等
        boot_bridge()

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
        browser_bridge.clear_bundles()
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

    def test_archive_done_allows_empty_dir_name(self):
        """分流后 dir_name 不再代表路径，空参也必须接受并触发重扫。

        回归修复：老实现对空 dir_name 返回 400，而扩展的 finishBatch 恒传 {dirName:''}，
        于是批量归档结束后的模型库重扫从未真正触发（模型管理里看不见刚归档完的模型）。
        """
        import threading
        done = threading.Event()
        seen = []
        orig = type(self.app).scan_models

        def fake_scan(self_app):
            seen.append(self_app)
            done.set()

        type(self.app).scan_models = fake_scan
        try:
            for body in ({}, {"dir_name": ""}, {"dir_name": "real/老客户端仍传真实目录名"}):
                done.clear()
                st, d = req("/api/archive/done", "POST", body, origin="chrome-extension://abc")
                self.assertEqual(st, 200, str(body))
                self.assertTrue(d["ok"], str(body))
                self.assertTrue(done.wait(5), "空 dir_name 没触发重扫: %s" % body)
                self.assertIs(seen[-1], self.app)
        finally:
            type(self.app).scan_models = orig

    def test_archive_done_releases_bundle(self):
        """带 bundle 的结束信号释放该令牌（不带就交给 TTL/容量回收）"""
        import base64
        d = os.path.join(self.tmp, "bundle_done")
        os.makedirs(d, exist_ok=True)
        token = browser_bridge.register_bundle(d)
        b64 = base64.b64encode(b"x").decode("ascii")
        st, _ = req("/api/archive/file", "POST",
                    {"bundle": token, "filename": "keep.txt", "data_b64": b64},
                    origin="chrome-extension://abc")
        self.assertEqual(st, 200)
        st, r = req("/api/archive/done", "POST", {"dir_name": "", "bundle": token},
                    origin="chrome-extension://abc")
        self.assertEqual(st, 200, r)
        self.assertTrue(r["ok"])
        self.assertIsNone(browser_bridge.resolve_bundle(token), "done 之后令牌应已释放")
        st, r = req("/api/archive/file", "POST",
                    {"bundle": token, "filename": "late.txt", "data_b64": b64},
                    origin="chrome-extension://abc")
        self.assertEqual(st, 400, r)
        self.assertFalse(os.path.exists(os.path.join(d, "late.txt")))

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

    # ---------------- bundle 形态（新协议：{bundle, filename, data_b64}） ----------------
    def _mk_bundle_dir(self, name):
        """造一个「桌面分流出来的落点」并登记令牌（真实链路里这一步由 _enqueue_one 完成）"""
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        return d, browser_bridge.register_bundle(d)

    def test_archive_file_bundle_ok(self):
        import base64
        d, token = self._mk_bundle_dir("bundle_ok")
        blob = "封面二进制".encode("utf-8")
        st, r = req("/api/archive/file", "POST",
                    {"bundle": token, "filename": "封面.png", "data_b64": base64.b64encode(blob).decode("ascii")},
                    origin="chrome-extension://abc")
        self.assertEqual(st, 200, r)
        self.assertTrue(r["ok"])
        self.assertEqual(os.path.normpath(os.path.dirname(r["path"])), os.path.normpath(d))
        with open(os.path.join(d, "封面.png"), "rb") as f:
            self.assertEqual(f.read(), blob)

    def test_archive_file_bundle_writes_into_models_root_outside_archive(self):
        """允许根扩展：分流落点在 models_dirs/comfyui_models_root 里、归档根之外，不能一律被拒"""
        import base64
        import shutil
        import tempfile
        extra = tempfile.mkdtemp(prefix="cft_bundle_root_")
        old_dirs = self.app.cfg["models_dirs"]
        old_root = self.app.cfg["archive_root"]
        try:
            # 归档根挪走，让 extra 只在 models_dirs 里 → 不扩根就会被拒
            self.app.cfg["models_dirs"] = [extra]
            self.app.cfg["archive_root"] = os.path.join(self.tmp, "another_archive")
            os.makedirs(self.app.cfg["archive_root"], exist_ok=True)
            st, r = req("/api/archive/file", "POST",
                        {"bundle": browser_bridge.register_bundle(extra),
                         "filename": "example.json",
                         "data_b64": base64.b64encode(b"{}").decode("ascii")},
                        origin="chrome-extension://abc")
            self.assertEqual(st, 200, r)
            self.assertTrue(os.path.isfile(os.path.join(extra, "example.json")))
        finally:
            self.app.cfg["models_dirs"] = old_dirs
            self.app.cfg["archive_root"] = old_root
            shutil.rmtree(extra, ignore_errors=True)

    def test_archive_file_bundle_token_missing_expired_released(self):
        import base64
        b64 = base64.b64encode(b"x").decode("ascii")
        d, token = self._mk_bundle_dir("bundle_lifecycle")
        # 1) 令牌压根没登记过
        st, r = req("/api/archive/file", "POST",
                    {"bundle": "not-a-real-token", "filename": "a.txt", "data_b64": b64},
                    origin="chrome-extension://abc")
        self.assertEqual(st, 400, r)
        self.assertFalse(r["ok"])
        # 2) 已过期（把 TTL 压成负数模拟「30 分钟早过了」）
        old_ttl = browser_bridge._BUNDLE_TTL
        browser_bridge._BUNDLE_TTL = -1.0
        try:
            expired = browser_bridge.register_bundle(d)
        finally:
            browser_bridge._BUNDLE_TTL = old_ttl
        st, r = req("/api/archive/file", "POST",
                    {"bundle": expired, "filename": "b.txt", "data_b64": b64},
                    origin="chrome-extension://abc")
        self.assertEqual(st, 400, r)
        self.assertIn("令牌", r["msg"])
        self.assertIsNone(browser_bridge.resolve_bundle(expired), "过期令牌不该还能解析")
        # 3) 已释放
        browser_bridge.release_bundle(token)
        st, r = req("/api/archive/file", "POST",
                    {"bundle": token, "filename": "c.txt", "data_b64": b64},
                    origin="chrome-extension://abc")
        self.assertEqual(st, 400, r)
        self.assertEqual(sorted(os.listdir(d)), [], "被拒的请求绝不能落盘: %s" % os.listdir(d))

    def test_archive_file_bundle_rejects_dirty_filename(self):
        """filename 必须是纯基名：桌面侧自己判，绝不只信扩展已经过滤过"""
        import base64
        b64 = base64.b64encode(b"x").decode("ascii")
        d, token = self._mk_bundle_dir("bundle_names")
        bad = ["../up.txt", "sub/inside.txt", "a\\b.txt", "C:\\evil.txt", "C:evil.txt",
               "\\\\srv\\share\\x.txt", "/abs/x.txt", "..", ".", "bad\x00name.txt",
               "ctrl\x07bell.txt", "  spaced  ", ""]
        for name in bad:
            st, r = req("/api/archive/file", "POST",
                        {"bundle": token, "filename": name, "data_b64": b64},
                        origin="chrome-extension://abc")
            self.assertEqual(st, 400, repr(name))
            self.assertFalse(r["ok"], repr(name))
        self.assertEqual(sorted(os.listdir(d)), [], "非法 filename 一律不许落盘: %s" % os.listdir(d))
        # 令牌仍然可用（非法名字不该把令牌吃掉）
        st, r = req("/api/archive/file", "POST",
                    {"bundle": token, "filename": "ok.txt", "data_b64": b64},
                    origin="chrome-extension://abc")
        self.assertEqual(st, 200, r)

    def test_archive_file_bundle_rejects_dir_outside_allowed_roots(self):
        """令牌目录被换成允许根之外的路径（用户中途改设置/盘上被动过）→ 拒写"""
        import base64
        import shutil
        import tempfile
        b64 = base64.b64encode(b"x").decode("ascii")
        outside = tempfile.mkdtemp(prefix="cft_bundle_outside_")
        try:
            st, r = req("/api/archive/file", "POST",
                        {"bundle": browser_bridge.register_bundle(outside),
                         "filename": "evil.txt", "data_b64": b64},
                        origin="chrome-extension://abc")
            self.assertEqual(st, 400, r)
            self.assertEqual(os.listdir(outside), [])
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_archive_file_rel_path_form_never_touches_models_root(self):
        """老形态逐字不变：rel_path 只认归档根，不得借 models_dirs 里的兄弟目录逃逸"""
        import base64
        import shutil
        import tempfile
        b64 = base64.b64encode(b"x").decode("ascii")
        sibling = tempfile.mkdtemp(prefix="cft_sibling_")
        old_dirs = self.app.cfg["models_dirs"]
        try:
            self.app.cfg["models_dirs"] = [sibling]
            rel = os.path.join(os.path.basename(sibling), "escaped.txt")
            st, r = req("/api/archive/file", "POST",
                        {"rel_path": "../" + rel, "data_b64": b64},
                        origin="chrome-extension://abc")
            self.assertEqual(st, 400, r)
            self.assertFalse(os.path.exists(os.path.join(sibling, "escaped.txt")),
                             "老形态被扩根了：rel_path 不该能写进 models_dirs")
        finally:
            self.app.cfg["models_dirs"] = old_dirs
            shutil.rmtree(sibling, ignore_errors=True)

    def test_archive_file_bundle_requires_filename(self):
        """两种形态各自缺字段 → 400 + 明确 msg（bundle 形态缺 filename 不会退化成写归档根）"""
        import base64
        b64 = base64.b64encode(b"x").decode("ascii")
        st, r = req("/api/archive/file", "POST",
                    {"bundle": "whatever", "data_b64": b64}, origin="chrome-extension://abc")
        self.assertEqual(st, 400, r)
        self.assertIn("filename", r["msg"])
        st, r = req("/api/archive/file", "POST",
                    {"data_b64": b64}, origin="chrome-extension://abc")
        self.assertEqual(st, 400, r)
        st, r = req("/api/archive/file", "POST", {"bundle": "whatever"},
                    origin="chrome-extension://abc")
        self.assertEqual(st, 400, r)

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


class BundleRegistryTest(unittest.TestCase):
    """bundle 令牌注册表：滑动 TTL / 写入续期 / 释放 / 容量上限（纯内存，不碰磁盘与网络）"""

    def setUp(self):
        browser_bridge.clear_bundles()
        self.addCleanup(browser_bridge.clear_bundles)

    def test_register_and_resolve(self):
        token = browser_bridge.register_bundle(r"D:\ComfyUI\models\loras\Demo_v1")
        self.assertTrue(token)
        self.assertEqual(len(token), 22, "secrets.token_urlsafe(16) 恒 22 字符")
        self.assertEqual(browser_bridge.resolve_bundle(token), r"D:\ComfyUI\models\loras\Demo_v1")

    def test_register_rejects_empty_and_tokens_are_unique(self):
        self.assertEqual(browser_bridge.register_bundle(""), "")
        self.assertEqual(browser_bridge.register_bundle(None), "")
        self.assertEqual(browser_bridge.register_bundle("   "), "")
        self.assertEqual(browser_bridge.bundle_count(), 0)
        a = browser_bridge.register_bundle(r"D:\a")
        b = browser_bridge.register_bundle(r"D:\a")
        self.assertNotEqual(a, b, "同一目录反复归档要各拿一个令牌，不能互相覆盖")

    def test_ttl_is_sliding_and_renews_on_every_write(self):
        """每次解析（= 每次写盘）都把到期时刻往后推——批量归档从 prepare 到写评论能隔几分钟"""
        old = browser_bridge._BUNDLE_TTL
        browser_bridge._BUNDLE_TTL = 5.0
        try:
            token = browser_bridge.register_bundle(r"D:\slide")
            deadline0 = browser_bridge._bundles[token][1]
            time.sleep(0.05)
            self.assertEqual(browser_bridge.resolve_bundle(token), r"D:\slide")
            self.assertGreater(browser_bridge._bundles[token][1], deadline0, "解析（写入）没续期")
        finally:
            browser_bridge._BUNDLE_TTL = old
        self.assertEqual(browser_bridge._BUNDLE_TTL, 1800.0, "TTL 默认必须是 30 分钟")
        self.assertEqual(browser_bridge.resolve_bundle(token), r"D:\slide", "恢复 TTL 后仍有效")

    def test_expired_token_resolves_to_none_and_is_dropped(self):
        old = browser_bridge._BUNDLE_TTL
        browser_bridge._BUNDLE_TTL = -1.0
        try:
            token = browser_bridge.register_bundle(r"D:\stale")
        finally:
            browser_bridge._BUNDLE_TTL = old
        self.assertIsNone(browser_bridge.resolve_bundle(token))
        self.assertIsNone(browser_bridge.resolve_bundle(token), "已过期令牌不得复活")
        self.assertEqual(browser_bridge.bundle_count(), 0, "过期项应在解析时就从表里带走")

    def test_release(self):
        token = browser_bridge.register_bundle(r"D:\rel")
        self.assertTrue(browser_bridge.release_bundle(token))
        self.assertFalse(browser_bridge.release_bundle(token), "重复释放是正常路径，但返回 False")
        self.assertFalse(browser_bridge.release_bundle(""))
        self.assertFalse(browser_bridge.release_bundle("unknown"))
        self.assertIsNone(browser_bridge.resolve_bundle(token))
        self.assertIsNone(browser_bridge.resolve_bundle(""))
        self.assertIsNone(browser_bridge.resolve_bundle(None))

    def test_capacity_evicts_least_recently_written(self):
        """表满按「最久没写入」淘汰：正在写附属文件的批次不能被同批后来的登记挤掉"""
        old_max = browser_bridge._BUNDLE_MAX
        browser_bridge._BUNDLE_MAX = 4
        try:
            a = browser_bridge.register_bundle(r"D:\a")
            b = browser_bridge.register_bundle(r"D:\b")
            c = browser_bridge.register_bundle(r"D:\c")
            d = browser_bridge.register_bundle(r"D:\d")
            self.assertEqual(browser_bridge.resolve_bundle(a), r"D:\a")   # a 变成「最近写过」
            e = browser_bridge.register_bundle(r"D:\e")                   # 于是挤掉 b
            self.assertEqual(browser_bridge.bundle_count(), 4)
            self.assertIsNone(browser_bridge.resolve_bundle(b))
            for t, p in ((a, r"D:\a"), (c, r"D:\c"), (d, r"D:\d"), (e, r"D:\e")):
                self.assertEqual(browser_bridge.resolve_bundle(t), p)
        finally:
            browser_bridge._BUNDLE_MAX = old_max

    def test_caps_helpers(self):
        self.assertTrue(browser_bridge.caps_allow_bundle(["bundle"]))
        self.assertTrue(browser_bridge.caps_allow_bundle(["bundle", "future"]))
        self.assertTrue(browser_bridge.caps_has(("bundle",), "bundle"))
        # 老扩展不发这个字段；脏值一律当「没声明」——判错方向的代价是归档分家，宁可少放行
        for no in (None, [], {}, "", "bundle", 12, [None], ["other"], [" Bundle "], [{"bundle": 1}], ()):
            self.assertFalse(browser_bridge.caps_allow_bundle(no), repr(no))

    def test_download_handler_caps_detection_keeps_two_arg_handlers(self):
        """老 handler 只声明 (url, dest_subdir)：桥绝不硬塞第三参（v2.6.30 整条链 500 的回归网）"""
        seen = {}
        unset = object()

        def two_arg(url, dest_subdir=""):
            seen["args"] = (url, dest_subdir)
            return {"ok": True}

        def three_arg(url, dest_subdir="", caps=unset):
            # 用哨兵区分「没传 caps」与「传了 None」：桥只在 caps 非空时才带这个关键字
            seen["args"] = (url, dest_subdir) if caps is unset else (url, dest_subdir, caps)
            return {"ok": True}

        browser_bridge._call_download_handler(two_arg, "u", "d", ["bundle"])
        self.assertEqual(seen["args"], ("u", "d"))
        browser_bridge._call_download_handler(three_arg, "u", "d", ["bundle"])
        self.assertEqual(seen["args"], ("u", "d", ["bundle"]))
        browser_bridge._call_download_handler(three_arg, "u", "d", None)   # 没 caps 就不带关键字
        self.assertEqual(seen["args"], ("u", "d"))
        # **kwargs 兜底的 handler 也算收得住 caps
        def var_kw(url, dest_subdir="", **kw):
            seen["args"] = (url, dest_subdir, kw.get("caps"))
            return {"ok": True}
        browser_bridge._call_download_handler(var_kw, "u", "d", ["bundle"])
        self.assertEqual(seen["args"], ("u", "d", ["bundle"]))


class BundleCapsTest(unittest.TestCase):
    """能力协商全链路：/api/download 的 caps 决定要不要分流，分流就回令牌，令牌把附属文件写进同一目录。

    零真实网络：假 api 只回本地字典（LoRA 走「按类型映射」，压根不读 safetensors 头），
    dl 用 Mock 收任务，全部落点在临时目录。
    """

    @classmethod
    def setUpClass(cls):
        import config
        import webui
        from unittest.mock import Mock
        cls.tmp = tempfile.mkdtemp(prefix="cft_caps_")
        cls.archive = os.path.join(cls.tmp, "archive")       # 归档根（附属文件的旧通道）
        cls.comfy = os.path.join(cls.tmp, "models")          # comfyui_models_root（分流落点）
        os.makedirs(cls.archive, exist_ok=True)
        os.makedirs(os.path.join(cls.comfy, "loras"), exist_ok=True)
        app = webui.Api.__new__(webui.Api)
        app.cfg = dict(config.DEFAULTS,
                       api_key="synthetic-test-key", site_domain="civitai.red",
                       download_dir=cls.archive, archive_root=cls.archive,
                       models_dir=cls.archive, models_dirs=[cls.archive],
                       download_target_dir="", download_cover=False, gen_metadata=False,
                       multi_file_download="first",
                       route_by_type=True, comfyui_models_root=cls.comfy)
        app.log_error = Mock()                # 绝不把测试噪声写进仓库 error.log
        app.api = LoraFakeAPI()
        app.dl = Mock()
        app.dl.tasks = []
        app.model_rows = []
        app.mm_scan_state = {"running": False, "rows": [], "msg": ""}
        app.scan_models = Mock()
        cls.app = app
        cls._prev = (browser_bridge._handler, browser_bridge._archive_config_fn,
                     browser_bridge._archive_write_fn, browser_bridge._archive_done_fn,
                     browser_bridge._archive_check_fn)
        browser_bridge.set_download_handler(app.dl_submit_url_from_bridge)
        browser_bridge.set_archive_handlers(config_fn=app.archive_config, write_fn=app.archive_write,
                                            done_fn=app.archive_done, check_fn=app.library_check)
        browser_bridge.clear_bundles()
        boot_bridge()

    @classmethod
    def tearDownClass(cls):
        import shutil
        h, cf, w, dn, ck = cls._prev
        browser_bridge._handler = h
        browser_bridge.set_archive_handlers(config_fn=cf, write_fn=w, done_fn=dn, check_fn=ck)
        browser_bridge.clear_bundles()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.app.dl.reset_mock()
        self.app.dl.tasks = []
        browser_bridge.clear_bundles()

    def _wait_task(self):
        """等后台入队线程把任务交给 Mock dl（caps 缺失那条分支是异步应答 + 后台解析）"""
        for _ in range(150):
            if self.app.dl.add_task.call_args:
                return self.app.dl.add_task.call_args.args[0]
            time.sleep(0.02)
        self.fail("后台线程没入队")

    def _body(self, with_caps, dest_subdir="Demo Model_v1"):
        body = {"url": "https://civitai.red/models/123/abc", "dest_subdir": dest_subdir}
        if with_caps:
            body["caps"] = ["bundle"]
        return body

    def test_caps_absent_keeps_old_flow_and_returns_no_bundle(self):
        """老扩展归档流（只发 {url, dest_subdir}）：不分流、响应里没有 bundle，本体仍落 <归档根>/<目录名>

        这条历史上就被 dest_subdir 的「指定落点优先」挡住了分流，等于双保险；
        响应里没有 bundle 才是关键——扩展看见没有令牌就退回 rel_path 形态，两边同目录。
        """
        st, d = req("/api/download", "POST", self._body(False), origin="chrome-extension://abc")
        self.assertEqual(st, 200, d)
        self.assertTrue(d["ok"])
        self.assertNotIn("bundle", d, d)
        self.assertFalse(d.get("routed"), d)
        task = self._wait_task()
        self.assertEqual(os.path.normpath(task.dest_dir),
                         os.path.normpath(os.path.join(self.archive, "Demo Model_v1")))
        self.assertFalse((task.info or {}).get("routed_dir"), "老扩展被分流了 → 归档必然分家")
        self.assertTrue(task.info.get("dest_explicit"), "回落归档子目录后要标记，才不被全局目标搬走")
        self.assertEqual(browser_bridge.bundle_count(), 0)

    def test_caps_absent_without_dest_subdir_is_blocked_by_the_gate(self):
        """列表批量提交（老扩展，无 dest_subdir）：只剩门限这一道能挡住分流"""
        body = {"url": "https://civitai.red/models/123/abc"}
        st, d = req("/api/download", "POST", body, origin="chrome-extension://abc")
        self.assertEqual(st, 200, d)
        self.assertNotIn("bundle", d, d)
        task = self._wait_task()
        self.assertFalse((task.info or {}).get("routed_dir"),
                         "LORA 类型 + route_by_type=True，没有门限就一定分流")
        self.assertIn("bundle 能力", (task.info or {}).get("route_reason", ""),
                      "不分流的原因要写进 route_reason，用户在下载列表看得见")

    def test_caps_present_without_dest_subdir_routes_but_acks_fast(self):
        """列表批量提交 + 新扩展（caps、无 dest_subdir）：分流照常，但这条链路没有令牌可消费

        为什么不改成同步：这条路径的扩展只发链接，附属文件由桌面自己在下载完成后生成，
        本体与附属文件必然同目录；同步解析会让每条链接都排队等一次解析（多文件版本还要等
        文件选择弹窗），批量提交会被拖成 12s 超时。
        """
        st, d = req("/api/download", "POST",
                    {"url": "https://civitai.red/models/123/abc", "caps": ["bundle"]},
                    origin="chrome-extension://abc")
        self.assertEqual(st, 200, d)
        self.assertTrue(d["started"], d)
        self.assertNotIn("bundle", d, d)
        task = self._wait_task()
        self.assertEqual(os.path.normpath(task.info.get("routed_dir") or ""),
                         os.path.normpath(os.path.join(self.comfy, "loras", "test")))
        self.assertEqual(browser_bridge.bundle_count(), 1,
                         "分流出落点就登记令牌（扩展用不上，交给 TTL/容量回收）")

    def test_caps_present_routes_and_returns_bundle_token(self):
        """声明 caps:["bundle"] → 分流到 <comfy>\\loras\\<文件名主干>，令牌随响应回给扩展"""
        st, d = req("/api/download", "POST", self._body(True), origin="chrome-extension://abc")
        self.assertEqual(st, 200, d)
        self.assertTrue(d["ok"], d)
        self.assertTrue(d.get("routed"), d)
        token = d.get("bundle") or ""
        self.assertTrue(token, "分流成功必须回 bundle 令牌：%s" % d)
        expect = os.path.normpath(os.path.join(self.comfy, "loras", "test"))
        self.assertEqual(os.path.normpath(d["dest_dir"]), expect)
        self.assertEqual(browser_bridge.resolve_bundle(token), expect)
        task = self._wait_task()
        self.assertEqual(os.path.normpath(task.info["routed_dir"]), expect)
        # 附属文件用令牌写：必须进本体所在的那个目录，而不是 <归档根>/<目录名>
        import base64
        blob = "评论正文".encode("utf-8")
        st, w = req("/api/archive/file", "POST",
                    {"bundle": token, "filename": "评论.txt",
                     "data_b64": base64.b64encode(blob).decode("ascii")},
                    origin="chrome-extension://abc")
        self.assertEqual(st, 200, w)
        self.assertTrue(os.path.isfile(os.path.join(expect, "评论.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.archive, "Demo Model_v1", "评论.txt")),
                         "分流后附属文件不该再回归档根（用户投诉的「没归档」就是这么来的）")
        # 令牌目录在 comfyui_models_root 里、归档根之外——允许根扩展就是为这一步
        self.assertIn(os.path.normpath(self.comfy),
                      [os.path.normpath(r) for r in self.app._archive_allowed_roots()])

    def test_caps_present_but_unroutable_falls_back_to_dest_subdir(self):
        """分流判不成（类型判不出落点）时 dest_subdir 仍是落点，且不发令牌"""
        old_api = self.app.api
        self.app.api = FakeAPI()          # 没有 model.type / baseModel → classify 判不出，绝不嗅探
        try:
            st, d = req("/api/download", "POST", self._body(True, dest_subdir="另一个归档目录"),
                        origin="chrome-extension://abc")
        finally:
            self.app.api = old_api
        self.assertEqual(st, 200, d)
        task = self._wait_task()
        self.assertEqual(os.path.normpath(task.dest_dir),
                         os.path.normpath(os.path.join(self.archive, "另一个归档目录")))
        self.assertTrue(task.info.get("dest_explicit"), "回落归档子目录后要标记，才不被全局目标搬走")
        self.assertFalse(d.get("routed"), d)
        self.assertNotIn("bundle", d, d)
        self.assertEqual(browser_bridge.bundle_count(), 0)

    def test_caps_garbage_is_treated_as_no_caps(self):
        st, d = req("/api/download", "POST",
                    {"url": "https://civitai.red/models/123/abc", "dest_subdir": "x",
                     "caps": "bundle"}, origin="chrome-extension://abc")
        self.assertEqual(st, 200, d)
        self.assertNotIn("bundle", d, "裸字符串是脏值：按「没声明」处理，宁可少分流")

    def test_desktop_own_ui_entries_still_route_without_caps(self):
        """桌面自身的下载入口（dl_submit_url / dl_enqueue_url_sync）不受门限约束，照常分流"""
        r = self.app.dl_submit_url("https://civitai.red/models/123/abc")
        self.assertTrue(r["ok"], r)
        task = self._wait_task()
        self.assertEqual(os.path.normpath(task.info.get("routed_dir") or ""),
                         os.path.normpath(os.path.join(self.comfy, "loras", "test")))
        self.app.dl.reset_mock()
        item = self.app.dl_enqueue_url_sync("https://civitai.red/models/123/abc")
        self.assertTrue(item["ok"], item)
        self.assertTrue(item.get("bundle"), item)

    def test_archive_done_releases_token_and_rescans_with_empty_dir_name(self):
        """批量结束（dirName 恒空）：不再 400，令牌释放，重扫真的被触发"""
        import base64
        st, d = req("/api/download", "POST", self._body(True), origin="chrome-extension://abc")
        self.assertEqual(st, 200, d)
        token = d["bundle"]
        st, r = req("/api/archive/file", "POST",
                    {"bundle": token, "filename": "example.json",
                     "data_b64": base64.b64encode(b"{}").decode("ascii")},
                    origin="chrome-extension://abc")
        self.assertEqual(st, 200, r)
        self.app.scan_models.reset_mock()
        for _ in range(150):
            if self.app.scan_models.call_count:
                break
            time.sleep(0.02)     # 前面入队/写盘都可能已触发重扫：先把它耗掉再断言这一次
        self.app.scan_models.reset_mock()
        st, r = req("/api/archive/done", "POST", {"dir_name": "", "bundle": token},
                    origin="chrome-extension://abc")
        self.assertEqual(st, 200, r)
        self.assertTrue(r["ok"], r)
        for _ in range(150):
            if self.app.scan_models.call_count:
                break
            time.sleep(0.02)
        self.assertEqual(self.app.scan_models.call_count, 1, "空 dir_name 的结束信号必须触发模型库重扫")
        self.assertIsNone(browser_bridge.resolve_bundle(token), "done 后令牌应释放")


if __name__ == "__main__":
    unittest.main(verbosity=2)
