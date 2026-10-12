"""资源库 RPC 层（webui.Api lib_*）单测：不依赖 A1/A2 交付物。
假库经 patch.object(webui.Api, "_media_lib") 注入（契约 §12），假 media_library 模块经 sys.modules 注入，
全程无网络。"""
import base64, importlib, json, os, sys, threading, time, types, unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import browser_bridge, config, posix_compat, webui

IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
VID_EXTS = (".mp4", ".mov", ".webm", ".mkv")


class FakeLib:
    """记录 A3 对库的全部调用；scan 可替换为可控钩子。"""
    def __init__(self):
        self.calls = []
        self.scan_impl = None
        self.items = []
        self.total = 0
        self.facets_val = {"total": 0}
        self.detail_val = None
        self.item_val = None
        self.star_val = True
        self.tags_val = []
        self.new_tag_id = 7
        self.delete_tag_val = True
        self.registered_val = {"id": 1}          # get_item_by_path：默认已入库
        self.poster_val = {"ok": True, "msg": "已保存海报"}

    def scan(self, roots, exclude=(), progress=None, cancel=None):
        self.calls.append(("scan", roots, exclude, progress, cancel))
        if self.scan_impl:
            return self.scan_impl(roots, exclude, progress, cancel) or {}
        return {"roots": list(roots), "total": 0, "added": 0, "updated": 0,
                "unchanged": 0, "missing": 0, "errors": []}

    def list_items(self, flt):
        self.calls.append(("list_items", flt))
        return {"items": self.items, "total": self.total}

    def facets(self, root_ids=None):
        self.calls.append(("facets", root_ids))
        return self.facets_val

    def detail(self, item_id):
        self.calls.append(("detail", item_id))
        return self.detail_val

    def get_item(self, item_id):
        self.calls.append(("get_item", item_id))
        return self.item_val

    def set_star(self, item_id, on):
        self.calls.append(("set_star", item_id, on))
        return self.star_val

    def set_tags(self, item_id, ids):
        self.calls.append(("set_tags", item_id, ids))
        return self.tags_val

    def list_tags(self):
        self.calls.append(("list_tags",))
        return self.tags_val

    def save_tag(self, tag_id, name, color, parent_id):
        self.calls.append(("save_tag", tag_id, name, color, parent_id))
        return self.new_tag_id

    def delete_tag(self, tag_id):
        self.calls.append(("delete_tag", tag_id))
        return self.delete_tag_val

    def mark_gone(self, item_id):
        self.calls.append(("mark_gone", item_id))
        return True

    def get_item_by_path(self, path):
        self.calls.append(("get_item_by_path", path))
        return self.registered_val

    def set_poster(self, path, data=None):
        """海报落盘与 2MB 上限、has_poster 同步都在库层（A2 用例覆盖），这里只记录委托与透出判定。"""
        self.calls.append(("set_poster", path, data))
        return self.poster_val


def make_fake_media_module(poster_dir=None):
    """假 media_library：只提供 A3 用到的 db_path/poster_path/扩展名表/MediaLibrary。"""
    m = types.ModuleType("media_library")
    m.IMAGE_EXTS = IMG_EXTS
    m.VIDEO_EXTS = VID_EXTS
    m.db = ["db1"]
    m.db_path = lambda: m.db[0]
    m.poster_path = lambda p: os.path.join(poster_dir or "_no_posters_", os.path.basename(p) + ".poster.jpg")

    class MediaLibrary:
        def __init__(self, path=None):
            self.path = path

    m.MediaLibrary = MediaLibrary
    return m


def make_png(path):
    from PIL import Image
    Image.new("RGB", (8, 8), "red").save(path)


class LibRPCTestBase(unittest.TestCase):
    def make_api(self, fake_lib=True, media_roots=(), poster_dir=None):
        a = webui.Api.__new__(webui.Api)
        a.cfg = {"media_roots": list(media_roots), "media_exclude_dirs": [], "models_dirs": [],
                 "media_page_size": 200}
        a.mm_scan_state = {"running": False, "rows": [], "msg": ""}
        a.media_lib = None
        a._lib_scan_st = {"running": False, "phase": "", "done": 0, "total": 0,
                          "msg": "", "roots": [], "errors": []}
        a._lib_cancel = threading.Event()
        a._lib_thumb_sem = threading.Lock()
        a._lib_scan_lock = threading.Lock()
        a._lib_last_scan_at = 0.0
        a._cover_cache = {}
        lib = FakeLib() if fake_lib is True else (None if fake_lib is False else fake_lib)
        if lib is not None:
            p = patch.object(webui.Api, "_media_lib", return_value=lib)
            p.start()
            self.addCleanup(p.stop)
        mod = make_fake_media_module(poster_dir)
        p2 = patch.dict(sys.modules, {"media_library": mod})
        p2.start()
        self.addCleanup(p2.stop)
        return a, lib, mod

    def wait_scan_done(self, a, timeout=5):
        deadline = time.time() + timeout
        while a._lib_scan_st["running"]:
            if time.time() > deadline:
                self.fail("扫描线程未退出（finally 未复位 running）")
            time.sleep(0.01)


class EnvelopeTests(LibRPCTestBase):
    """§5.2：每个方法的返回信封形状（dict vs JSON 字符串）与键集。"""
    def test_dict_envelopes(self):
        a, lib, mod = self.make_api(media_roots=("C:/fakeout",))
        lib.item_val = {"id": 1, "path": "C:/fakeout/missing.png"}
        checks = [
            ("lib_scan", (), {"started", "msg"}),
            ("lib_scan_state", (), {"running", "phase", "done", "total", "msg", "roots", "errors"}),
            ("lib_cancel_scan", (), {"ok"}),
            ("lib_rescan_if_stale", (), {"started", "reason"}),
            ("lib_set_star", (1, True), {"ok", "starred"}),
            ("lib_set_tags", (1, "[2]"), {"ok", "tags"}),
            ("lib_tag_save", (0, "精选", "#888", -1), {"ok", "id", "msg"}),
            ("lib_tag_delete", (1,), {"ok", "removed"}),
            ("lib_delete", (1,), {"ok", "msg"}),
            ("lib_autodetect_roots", (), {"ok", "roots"}),
        ]
        for name, args, keys in checks:
            with self.subTest(name=name):
                r = getattr(a, name)(*args)
                self.assertIsInstance(r, dict)
                self.assertTrue(keys.issubset(set(r)), "%s 缺键 %s" % (name, keys - set(r)))
        self.wait_scan_done(a)  # lib_scan 真起了线程，收尾等它退出

    def test_json_string_envelopes(self):
        with TemporaryDirectory() as d:
            a, lib, mod = self.make_api(media_roots=(d,), poster_dir=os.path.join(d, "posters"))
            png = os.path.join(d, "a.png")
            make_png(png)
            lib.items = [{"id": 1, "path": png, "kind": "image", "prompt": "x"}]
            lib.total = 1
            lib.detail_val = {"id": 1, "path": png, "kind": "image", "prompt": "x"}
            lib.tags_val = [{"id": 1, "name": "精选", "color": "", "parent_id": None, "count": 12}]
            str_checks = [
                ("lib_list", (json.dumps({"kind": ""}),)),
                ("lib_detail", (1,)),
                ("lib_get_thumbs", (json.dumps([png]),)),
                ("lib_tags", ()),
            ]
            for name, args in str_checks:
                with self.subTest(name=name):
                    r = getattr(a, name)(*args)
                    self.assertIsInstance(r, str, "%s 必须以 JSON 字符串返回（规避 js_api 大 list）" % name)
                    self.assertIsInstance(json.loads(r), (dict, list))
            listed = json.loads(a.lib_list(json.dumps({"offset": 40, "limit": 99999})))
            self.assertEqual(set(listed), {"ok", "items", "total", "offset", "limit", "facets"})
            self.assertTrue(listed["ok"])
            self.assertEqual((listed["offset"], listed["limit"]), (40, 99999))
            self.assertEqual(listed["total"], 1)
            saved = a.lib_save_poster(os.path.join(d, "v.mp4"), base64.b64encode(b"jpgdata").decode())
            self.assertIsInstance(saved, dict)
            self.assertTrue(saved["ok"])
            self.assertTrue(set(saved) >= {"ok", "msg"})

    def test_star_coercion_and_tag_sentinels_passthrough(self):
        a, lib, mod = self.make_api()
        a.lib_set_star(3, "yes")
        self.assertEqual(lib.calls[-1], ("set_star", 3, True))
        a.lib_tag_save(0, "精选", "#fff", -1)
        # §5.2 哨兵值（tag_id=0 新建 / parent_id=-1 无父）原样交给 A2 解释
        self.assertEqual(lib.calls[-1], ("save_tag", 0, "精选", "#fff", -1))
        self.assertEqual(a.lib_tag_save(0, "  ", "", -1), {"ok": False, "id": 0, "msg": "标签名不能为空"})
        lib.new_tag_id = None
        self.assertEqual(a.lib_tag_save(5, "x", "", 2)["ok"], False)


class ScanThreadTests(LibRPCTestBase):
    def test_reentrancy_progress_cancel_wiring_and_finally_reset(self):
        with TemporaryDirectory() as d:
            a, lib, mod = self.make_api(media_roots=(d,))
            a.cfg["media_exclude_dirs"] = ["sub/dir"]
            seen = {}
            gate = threading.Event()

            def impl(roots, exclude, progress, cancel):
                seen["callable"] = (callable(progress), callable(cancel))
                seen["cancel_before"] = cancel()
                progress({"phase": "parse", "done": 7, "total": 21, "msg": "解析中"})
                gate.wait(5)
                seen["cancel_after"] = cancel()
                return {"roots": [{"path": d, "status": "ok", "count": 3}],
                        "total": 3, "added": 2, "updated": 1, "unchanged": 0, "missing": 0,
                        "errors": ["boom"]}

            lib.scan_impl = impl
            r = a.lib_scan()
            self.assertTrue(r["started"])
            deadline = time.time() + 5
            while a._lib_scan_st["phase"] != "parse" and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(a.lib_scan_state()["done"], 7)  # progress 回调写进状态
            self.assertEqual(a.lib_scan(), {"started": False, "msg": "资源库扫描已在进行"})  # 重入拒绝
            a.lib_cancel_scan()
            gate.set()
            self.wait_scan_done(a)
            self.assertEqual(seen["callable"], (True, True))
            self.assertFalse(seen["cancel_before"])
            self.assertTrue(seen["cancel_after"])  # cancel 钩子 = _lib_cancel.is_set
            st = a.lib_scan_state()
            self.assertNotIn("rows", st)  # 轻量状态不含行数据
            self.assertEqual(st["roots"], [{"path": d, "status": "ok", "count": 3}])
            self.assertEqual(st["errors"], ["boom"])
            self.assertFalse(st["running"])  # finally 复位
            self.assertEqual(lib.calls[0][1], [d])
            self.assertEqual(lib.calls[0][2], ["sub/dir"])
            self.assertGreater(a._lib_last_scan_at, 0)

    def test_refuses_while_model_scan_running(self):
        a, lib, mod = self.make_api(media_roots=("C:/out",))
        a.mm_scan_state = {"running": True, "rows": [], "msg": ""}
        r = a.lib_scan()
        self.assertFalse(r["started"])
        self.assertIn("模型扫描", r["msg"])
        self.assertEqual([c for c in lib.calls if c[0] == "scan"], [])

    def test_scan_error_still_resets_running(self):
        a, lib, mod = self.make_api(media_roots=("C:/out",))
        def boom(roots, exclude, progress, cancel):
            raise RuntimeError("库炸了")
        lib.scan_impl = boom
        self.assertTrue(a.lib_scan()["started"])
        self.wait_scan_done(a)
        self.assertFalse(a.lib_scan_state()["running"])
        self.assertIn("扫描失败", a.lib_scan_state()["msg"])

    def test_concurrent_lib_scan_starts_exactly_one_thread(self):
        """两个调用方同时进 lib_scan（两标签页 / lib_rescan_if_stale 撞 lib_scan）：
        只允许起一个 walk 线程，且败者不得把赢者的 running 清掉。"""
        with TemporaryDirectory() as d:
            a, lib, mod = self.make_api(media_roots=(d,))
            in_scan = threading.Event()
            gate = threading.Event()

            def impl(roots, exclude, progress, cancel):
                in_scan.set()
                gate.wait(5)
                return {"roots": [], "total": 0, "added": 0, "updated": 0,
                        "unchanged": 0, "missing": 0, "errors": []}

            lib.scan_impl = impl
            real_thread = threading.Thread
            spawned = []

            class CountingThread(real_thread):
                def __init__(self, *args, **kwargs):
                    target = kwargs.get("target")
                    # 只数 lib_scan 自己那个工作闭包，别的环境线程（桥/其他扫描）不算
                    if getattr(target, "__name__", "") == "work" and getattr(target, "__module__", "") == "webui":
                        spawned.append(target)
                    real_thread.__init__(self, *args, **kwargs)

            st_before = a._lib_scan_st
            barrier = threading.Barrier(2)
            results = []

            def caller():
                barrier.wait()
                results.append(a.lib_scan())

            with patch.object(webui.threading, "Thread", CountingThread):
                pair = [real_thread(target=caller) for _ in range(2)]
                for t in pair:
                    t.start()
                for t in pair:
                    t.join(5)
            self.assertEqual(sorted(r["started"] for r in results), [False, True])
            self.assertEqual(len(spawned), 1, "并发 lib_scan 起了两个全量 walk 线程")
            self.assertTrue(in_scan.wait(5))
            self.assertTrue(a._lib_scan_st["running"])          # 败者的 finally 没清掉赢者的标志
            self.assertTrue(a.lib_scan_state()["running"])      # 前端必须还在轮询这个正在跑的扫描
            self.assertFalse(a.lib_scan()["started"])           # 第三次仍被拒
            self.assertIs(a._lib_scan_st, st_before)            # 状态字典原地复位，从不重新绑定
            gate.set()
            self.wait_scan_done(a)
            self.assertFalse(a.lib_scan_state()["running"])
            self.assertEqual(len([c for c in lib.calls if c[0] == "scan"]), 1)

    def test_scan_state_keeps_error_fold_line(self):
        """库侧已按 ERRORS_MAX 排好「明细 + 1 条折叠说明」，RPC 层再切一次会把提示挤掉、UI 少报。"""
        ERRORS_MAX = 200                                        # §7 / media_library.ERRORS_MAX
        with TemporaryDirectory() as d:
            a, lib, mod = self.make_api(media_roots=(d,))
            errs = ["元数据解析异常 e%03d" % i for i in range(ERRORS_MAX)] + ["另有 50 条错误已折叠"]

            def impl(roots, exclude, progress, cancel):
                return {"roots": [], "total": 0, "added": 0, "updated": 0,
                        "unchanged": 0, "missing": 0, "errors": list(errs)}

            lib.scan_impl = impl
            self.assertTrue(a.lib_scan()["started"])
            self.wait_scan_done(a)
            st = a.lib_scan_state()
            self.assertEqual(len(st["errors"]), ERRORS_MAX + 1)
            self.assertIn("折叠", st["errors"][-1])

    def test_scan_state_msg_mentions_skipped_roots(self):
        with TemporaryDirectory() as d:
            a, lib, mod = self.make_api(media_roots=(d,))
            inner = os.path.join(d, "sub")

            def impl(roots, exclude, progress, cancel):
                return {"roots": [{"path": d, "status": "ok", "count": 1}], "total": 1, "added": 1,
                        "updated": 0, "unchanged": 0, "missing": 0, "errors": [],
                        "skipped_roots": [{"path": inner, "inside": d}]}

            lib.scan_impl = impl
            self.assertTrue(a.lib_scan()["started"])
            self.wait_scan_done(a)
            st = a.lib_scan_state()
            self.assertIn("跳过", st["msg"])
            self.assertIn(inner, st["msg"])
            self.assertEqual(st["roots"], [{"path": d, "status": "ok", "count": 1}])

    def test_no_roots_does_not_start_thread(self):
        a, lib, mod = self.make_api(media_roots=())
        self.assertFalse(a.lib_scan()["started"])
        self.assertEqual(lib.calls, [])
        self.assertFalse(a._lib_scan_st["running"])

    def test_rescan_if_stale(self):
        import time as _t
        a, lib, mod = self.make_api(media_roots=())
        self.assertEqual(a.lib_rescan_if_stale(), {"started": False, "reason": "no_roots"})
        a2, lib2, _ = self.make_api(media_roots=("C:/out",))
        r = a2.lib_rescan_if_stale()  # 会话内从未扫过 → 起扫
        self.assertTrue(r["started"])
        self.wait_scan_done(a2)
        a3, lib3, _ = self.make_api(media_roots=("C:/out",))
        a3._lib_last_scan_at = _t.time()
        self.assertEqual(a3.lib_rescan_if_stale(), {"started": False, "reason": "fresh"})
        a4, lib4, _ = self.make_api(media_roots=("C:/out",))
        lib4.last_scan = None
        def ls():
            return _t.time()
        lib4.last_scan_at = ls  # A2 若提供 last_scan_at()，跨会话判定优先
        self.assertEqual(a4.lib_rescan_if_stale(), {"started": False, "reason": "fresh"})


class ThumbTests(LibRPCTestBase):
    def test_cap_40_and_image_data_urls(self):
        a, lib, mod = self.make_api()
        with TemporaryDirectory() as d:
            pd = os.path.join(d, "posters")
            os.makedirs(pd)
            td = os.path.join(d, "thumbs")
            a, lib, mod = self.make_api(poster_dir=pd)
            pngs = []
            for i in range(45):
                p = os.path.join(d, "i%d.png" % i)
                make_png(p)
                pngs.append(p)
            vids = [os.path.join(d, "v%d.mp4" % i) for i in (1, 2)]
            with open(mod.poster_path(vids[0]), "wb") as f:
                f.write(b"jpgbytes")
            with patch.object(posix_compat, "thumb_cache_dir", return_value=td):
                capped = json.loads(a.lib_get_thumbs(json.dumps(pngs + vids)))
                self.assertEqual(len(capped), 40)  # 单次 40 上限，超出截断
                self.assertEqual(list(capped)[:5], pngs[:5])
                self.assertTrue(capped[pngs[0]].startswith("data:image/jpeg;base64,"))
                small = json.loads(a.lib_get_thumbs(json.dumps([pngs[0]]), 96))
                self.assertTrue(small[pngs[0]].startswith("data:image/jpeg;base64,"))
                mix = json.loads(a.lib_get_thumbs(json.dumps(vids + [pngs[0]])))
                self.assertEqual(mix[vids[1]], "")  # 视频无海报缓存 → ""
                self.assertEqual(mix[vids[0]], "data:image/jpeg;base64," + base64.b64encode(b"jpgbytes").decode())
                self.assertEqual(json.loads(a.lib_get_thumbs("坏JSON")), {})
            # 复用 _cover_thumb 的内存缓存与磁盘缓存（与模型封面共 thumb_cache_dir）
            self.assertTrue(any(k[3] == 96 for k in a._cover_cache))
            self.assertTrue(os.listdir(td))

    def test_thumbs_serialized_by_sem(self):
        a, lib, mod = self.make_api()
        class SpyLock:  # threading.Lock 实例无法打补丁，换成可记录的假锁验证批间串行
            def __init__(self):
                self.entered = 0
            def __enter__(self):
                self.entered += 1
                return self
            def __exit__(self, *exc):
                return False
        a._lib_thumb_sem = SpyLock()
        a.lib_get_thumbs("[]")
        self.assertEqual(a._lib_thumb_sem.entered, 1)


class ListDetailTests(LibRPCTestBase):
    def test_evil_filter_passthrough_and_row_enrichment(self):
        a, lib, mod = self.make_api(poster_dir=os.getcwd())
        with TemporaryDirectory() as d:
            png = os.path.join(d, "a.png")
            make_png(png)
            vid = os.path.join(d, "v.mp4")
            evil = {"kind": "image", "sort": "time_desc; DROP TABLE items;--", "group_by": "1=1",
                    "zzz": {"嵌套": [1]}, "limit": 99999, "offset": 0, "q": "%_'evil"}
            lib.items = [{"id": 1, "path": png, "kind": "image", "ext": ".png", "prompt": "x" * 200},
                         {"id": 2, "path": vid, "kind": "video", "ext": ".mp4"}]
            lib.total = 2
            out = json.loads(a.lib_list(json.dumps(evil, ensure_ascii=False)))
            # 校验在 A2 层：flt 解析后客户端键必须原样透传（未知键/evil sort 一字不改）
            sent = [c for c in lib.calls if c[0] == "list_items"][-1][1]
            self.assertEqual({k: v for k, v in sent.items() if k != "root_ids"}, evil)
            # root_ids 由服务端注入并覆盖客户端值（前端不得自行放宽作用域）
            self.assertIn("root_ids", sent)
            evil.pop("root_ids", None)
            evil_with_roots = dict(evil)
            evil_with_roots["root_ids"] = ["客户端伪造"]
            a.lib_list(json.dumps(evil_with_roots, ensure_ascii=False))
            self.assertNotIn("客户端伪造", [c for c in lib.calls if c[0] == "list_items"][-1][1]["root_ids"])
            row = out["items"][0]
            self.assertEqual(row["snippet"], "x" * 120)
            self.assertTrue(row["thumb"])
            self.assertFalse(out["items"][1]["thumb"])  # 视频无海报
            local, http = out["items"][1]["url_local"], out["items"][1]["url_http"]
            import pathlib, urllib.parse
            self.assertEqual(local, pathlib.Path(os.path.abspath(vid)).as_uri())
            # 桥端口为 0 → url_http 置空；显式 patch，不依赖"本进程恰好没人起桥"（其它用例会真起服务）
            with patch.object(browser_bridge, "port", return_value=0):
                out0 = json.loads(a.lib_list("{}"))
            self.assertEqual(out0["items"][1]["url_http"], "")
            if http:   # 桥在跑时的形态（端口号随环境，只校验结构）
                self.assertIn("http://127.0.0.1:", http)
                self.assertIn("/api/media?p=", http)
            with patch.object(browser_bridge, "port", return_value=47531):
                out2 = json.loads(a.lib_list("{}"))
            self.assertEqual(out2["items"][1]["url_http"],
                             "http://127.0.0.1:47531/api/media?p=%s" % urllib.parse.quote(os.path.abspath(vid)))
            self.assertEqual(out["facets"], lib.facets_val)  # list_items 没带 facets → 回落 facets()

    def test_detail_shape_preview_and_missing(self):
        a, lib, mod = self.make_api()
        with TemporaryDirectory() as d:
            png = os.path.join(d, "a.png")
            make_png(png)
            lib.detail_val = {"id": 1, "path": png, "kind": "image", "negative": "bad", "seed": 7}
            td = os.path.join(d, "thumbs")
            with patch.object(posix_compat, "thumb_cache_dir", return_value=td):
                det = json.loads(a.lib_detail(1))
            self.assertTrue(det["ok"])
            self.assertTrue(det["exists"])
            self.assertTrue(det["preview"].startswith("data:image/jpeg;base64,"))  # §7 详情 800 预览
            self.assertEqual(det["seed"], 7)
            self.assertIn("url_local", det)
            lib.detail_val = None
            self.assertEqual(json.loads(a.lib_detail(99)), {"ok": False, "msg": "条目不存在"})

    def test_bad_flt_json_envelope(self):
        a, lib, mod = self.make_api()
        out = json.loads(a.lib_list("{坏json"))
        self.assertFalse(out["ok"])
        self.assertEqual((out["items"], out["total"], out["offset"], out["limit"]), ([], 0, 0, 0))


class DeleteTests(LibRPCTestBase):
    def test_refuses_missing_file_and_unknown_item(self):
        a, lib, mod = self.make_api()
        lib.item_val = None
        self.assertFalse(a.lib_delete(1)["ok"])
        lib.item_val = {"id": 1, "path": os.path.join("C:/nope", "a.png")}
        with patch.object(webui, "_recycle_to_trash") as rec:
            r = a.lib_delete(1)
        self.assertFalse(r["ok"])
        self.assertEqual(r["msg"], "文件不存在")
        rec.assert_not_called()
        self.assertEqual([c for c in lib.calls if c[0] == "mark_gone"], [])

    def test_recycles_media_only_never_sidecar(self):
        a, lib, mod = self.make_api()
        with TemporaryDirectory() as d:
            png = os.path.join(d, "out.png")
            make_png(png)
            sidecar = os.path.join(d, "out.json")  # ComfyUI 图/视频共享同名 sidecar，绝不可连带删
            with open(sidecar, "w", encoding="utf-8") as f:
                f.write("{}")
            lib.item_val = {"id": 9, "path": png}
            with patch.object(posix_compat, "IS_WINDOWS", True), \
                 patch.object(webui, "_recycle_to_trash", return_value=True) as rec:
                r = a.lib_delete(9)
            self.assertTrue(r["ok"])
            rec.assert_called_once_with([png])  # 只回收媒体文件本体
            self.assertTrue(os.path.isfile(sidecar))
            self.assertIn(("mark_gone", 9), lib.calls)

    def test_posix_trash_branch_and_failure(self):
        a, lib, mod = self.make_api()
        with TemporaryDirectory() as d:
            png = os.path.join(d, "b.png")
            make_png(png)
            lib.item_val = {"id": 2, "path": png}
            with patch.object(posix_compat, "IS_WINDOWS", False), \
                 patch.object(posix_compat, "trash", return_value=False) as tr:
                r = a.lib_delete(2)
            self.assertFalse(r["ok"])
            tr.assert_called_once_with([png])
            self.assertEqual([c for c in lib.calls if c[0] == "mark_gone"], [])


class AutodetectTests(LibRPCTestBase):
    def test_candidate_order_existence_and_dedup(self):
        a, lib, mod = self.make_api()
        with TemporaryDirectory() as d:
            install = os.path.join(d, "install")   # 不创建 ComfyUI/output → 应被跳过
            home = os.path.join(d, "home")
            mroot = os.path.join(d, "models", "stable")
            home_out = os.path.join(home, "ComfyUI", "output")
            models_out = os.path.join(d, "models", "ComfyUI", "output")
            for p in (home_out, models_out):
                os.makedirs(p)
            a.cfg["models_dirs"] = [mroot, os.path.join(d, "models", "extra")]  # 同父 → 去重
            with patch.object(config, "INSTALL_DIR", install), \
                 patch.object(os.path, "expanduser", return_value=home):
                r = a.lib_autodetect_roots()
            self.assertTrue(r["ok"])
            self.assertEqual(r["roots"], [home_out, models_out])  # §6 顺序：安装目录→主目录→模型目录父级


class SavePosterTests(LibRPCTestBase):
    def test_writes_only_registered_video_paths(self):
        with TemporaryDirectory() as d:
            a, lib, mod = self.make_api(media_roots=(d,), poster_dir=os.path.join(d, "posters"))
            vid = os.path.join(d, "v.mp4")
            payload = "data:image/jpeg;base64," + base64.b64encode(b"poster-bytes").decode()
            self.assertTrue(a.lib_save_poster(vid, payload)["ok"])
            # A3 这层只做守卫与转发：剥 data: 前缀后的字节必须原样交给库层
            # （2MB 上限、原子落盘、has_poster 同步都在 media_library.set_poster，A2 用例覆盖）
            self.assertEqual(lib.calls[-1], ("set_poster", vid, b"poster-bytes"))
            self.assertFalse(a.lib_save_poster(vid, "")["ok"])                 # 空负载
            self.assertFalse(a.lib_save_poster(os.path.join(d, "a.png"), payload)["ok"])  # 仅视频
            self.assertFalse(a.lib_save_poster(os.path.join(d, "..", "evil_root", "x.mp4"), payload)["ok"])
            lib.registered_val = None                                           # 未入库
            self.assertEqual(a.lib_save_poster(vid, payload)["msg"], "路径未入库")
            self.assertEqual([c for c in lib.calls if c[0] == "set_poster"],
                             [("set_poster", vid, b"poster-bytes")])            # 被拒的三次不得委托
            lib.registered_val = {"id": 1}
            lib.poster_val = {"ok": False, "msg": "海报超过 2MB 上限"}
            self.assertFalse(a.lib_save_poster(vid, payload)["ok"])             # 库层判定原样透出

    def test_case_mismatched_root_still_accepts_poster(self):
        """Windows 大小写不敏感：配置根 D:\\Out 与扫到的 D:\\out\\a.mp4 只差大小写。
        比较两侧都 normcase 才不会被误判「不在资源目录内」（与 browser_bridge._media_within_roots 一致）。
        用 normcase/realpath 打桩固定跨平台语义：POSIX 的 normcase 是恒等函数，真实目录也造不出大小写差异。"""
        root = os.path.join("D:", os.sep + "Media", "Out")
        vid = os.path.join("D:", os.sep + "media", "out", "v.mp4")
        payload = base64.b64encode(b"poster-bytes").decode()
        with patch.object(os.path, "normcase", lambda s: str(s).lower()), \
             patch.object(os.path, "realpath", lambda s: os.path.abspath(s)):
            a, lib, mod = self.make_api(media_roots=(root,))
            self.assertTrue(a.lib_save_poster(vid, payload)["ok"],
                            "大小写不一致的根不得拒绝合法海报写入")
            self.assertEqual(lib.calls[-1][0], "set_poster")
            # 真越界仍然被拒：normcase 只放宽大小写，不放宽目录边界
            outside = os.path.join("E:", os.sep + "media", "out", "v.mp4")
            r = a.lib_save_poster(outside, payload)
            self.assertFalse(r["ok"])
            self.assertEqual(r["msg"], "路径不在资源目录内")
            prefix_trap = os.path.join("D:", os.sep + "media", "outbackup", "v.mp4")
            self.assertEqual(a.lib_save_poster(prefix_trap, payload)["msg"], "路径不在资源目录内")
            self.assertEqual([c for c in lib.calls if c[0] == "set_poster"],
                             [("set_poster", vid, b"poster-bytes")])


class ConfigKeysTests(unittest.TestCase):
    def test_defaults_and_clamp(self):
        self.assertEqual(config.DEFAULTS["media_roots"], [])
        self.assertEqual(config.DEFAULTS["media_exclude_dirs"], [])
        self.assertEqual(config.DEFAULTS["media_page_size"], 200)
        for value, expect in ((1, 20), ("1", 20), (9999, 500), (300, 300), (None, 200), ("坏值", 200)):
            cfg = {"media_page_size": value}
            config.normalize_ui_preferences(cfg)
            self.assertEqual(cfg["media_page_size"], expect, "lo=20/hi=500 钳制（§5.4）")

    def test_roundtrip(self):
        with TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "user_config.json")
            saved = {"media_roots": ["D:/ComfyUI/output", "D:/x"],
                     "media_exclude_dirs": ["sub/dir"], "media_page_size": 300}
            with patch.object(config, "APP_DIR", d), patch.object(config, "CONFIG_PATH", cfg_path):
                self.assertTrue(config.save(saved))
                got = config.load()
            self.assertEqual(got["media_roots"], saved["media_roots"])
            self.assertEqual(got["media_exclude_dirs"], saved["media_exclude_dirs"])
            self.assertEqual(got["media_page_size"], 300)


class AccessorTests(LibRPCTestBase):
    def test_media_lib_lazy_rebuild_on_db_path_change(self):
        a, _lib, mod = self.make_api(fake_lib=False)
        first = a._media_lib()
        self.assertIsInstance(first, mod.MediaLibrary)
        self.assertEqual(first.path, "db1")
        self.assertIs(a._media_lib(), first)  # 路径未变 → 复用
        mod.db[0] = "db2"
        second = a._media_lib()
        self.assertIsNot(second, first)  # db_path() 变化即重建（patch config.APP_DIR 依赖这条）
        self.assertEqual(second.path, "db2")

    def test_media_roots_and_bridge_helper(self):
        a, lib, mod = self.make_api(media_roots=(" D:/a ", "", "D:/b"))
        self.assertEqual(a._media_roots(), ["D:/a", "D:/b"])
        a.cfg["media_roots"] = "D:/单字符串"
        self.assertEqual(a._media_roots_for_bridge(), ["D:/单字符串"])

    def test_init_survives_bridge_without_set_media_roots(self):
        """A6 的 setter 未落地时 webui.Api() 仍必须可构造（getattr 守护）。"""
        class StubBridge:
            def set_download_handler(self, fn): pass
            def set_api(self, api, web_root=""): pass
            def set_archive_handlers(self, **kw): pass
            def start(self, version=""): pass
            def port(self): return 0
        self.assertIsNone(getattr(StubBridge, "set_media_roots", None))
        p = patch.dict(sys.modules, {"browser_bridge": StubBridge()})
        p.start()
        # 顺序关键：清理时先恢复 sys.modules（p.stop 后入先出）再重载 webui，避免 webui 长期引用假桥
        self.addCleanup(importlib.reload, webui)
        self.addCleanup(p.stop)
        importlib.reload(webui)
        with TemporaryDirectory() as d:
            with patch.object(config, "APP_DIR", d), \
                 patch.object(config, "CONFIG_PATH", os.path.join(d, "user_config.json")), \
                 patch.object(config, "TASKS_PATH", os.path.join(d, "download_tasks.json")), \
                 patch.object(config, "HISTORY_PATH", os.path.join(d, "download_history.json")):
                api = webui.Api()
            self.assertEqual(api._media_roots_for_bridge(), [])
            self.assertFalse(api.lib_scan()["started"])  # 未配置根 → 不起线程


if __name__ == "__main__":
    unittest.main()
