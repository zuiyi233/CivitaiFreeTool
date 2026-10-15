# -*- coding: utf-8 -*-
"""桌面端 tRPC 取评论/评价（合成数据 + mock HTTP）。

Synthetic model files / metadata only. Never commit personal keys or workflows.
契约（入参形状 / 出参字段 / 三种空结果的措辞）见 docs/ext-archive-contract.md §2.2。
"""
import json
import sys
import unittest
import urllib.parse
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import civitai_api
import webui

KEY = "synthetic-test-key"


def envelope(payload):
    """tRPC 的真实信封：数据在 result.data.json"""
    return {"result": {"data": {"json": payload}}}


def inputs_of(urls):
    """从 ?input=<urlencoded {"json":{…}}> 还原成 dict 列表"""
    out = []
    for url in urls:
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        out.append(json.loads(qs["input"][0])["json"])
    return out


class FakeHTTP:
    """按调用顺序吐预设响应；list 项是 dict → 200，CivitaiError → 指定 HTTP 状态"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def __call__(self, url, raw=False, retries=3):
        self.urls.append(url)
        if not self.responses:
            raise AssertionError("没有更多预设响应，但请求还在发: %s" % url)
        item = self.responses.pop(0)
        if isinstance(item, civitai_api.CivitaiError):
            raise item
        return item

    @property
    def inputs(self):
        return inputs_of(self.urls)


def comment(id_, user, content, **kw):
    node = {"id": id_, "createdAt": "2026-10-0%dT00:00:00.000Z" % (id_ % 9 + 1),
            "content": content, "nsfw": False, "hidden": False, "threadId": 1,
            "reactionCount": 0, "user": {"id": id_ * 10, "username": user}}
    node.update(kw)
    return node


class TrpcUrlTests(unittest.TestCase):
    def test_input_is_urlencoded_json_wrapper(self):
        url = civitai_api.build_trpc_url(civitai_api.COMMENTS_PROCEDURE,
                                         {"entityType": "model", "entityId": 7})
        self.assertTrue(url.startswith("https://civitai.com/api/trpc/commentv2.getInfinite?input="))
        self.assertEqual(json.loads(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["input"][0]),
                         {"json": {"entityType": "model", "entityId": 7}})

    def test_site_base_only_accepts_https_bare_origin(self):
        # Key 只会发给站点根；带 path/账号/非 https 的配置一律回落到默认 host（绝不把 Bearer 发去别处）
        self.assertEqual(civitai_api.CivitaiAPI(KEY, site_base="civitai.red").trpc_base,
                         "https://civitai.red/api/trpc")
        self.assertEqual(civitai_api.CivitaiAPI(KEY, site_base="http://civitai.com").trpc_base,
                         "https://civitai.com/api/trpc")
        self.assertEqual(civitai_api.CivitaiAPI(KEY, site_base="https://evil.example/x").trpc_base,
                         "https://civitai.com/api/trpc")

    def test_opener_strips_authorization_on_redirect(self):
        # tRPC 与下载共用 build_opener：307 预签名重定向必须剥掉 Bearer，否则 R2 报签名错
        handlers = [type(h) for h in civitai_api.CivitaiAPI(KEY).opener.handlers]
        self.assertIn(civitai_api._NoAuthRedirect, handlers)


class CommentsInputShapeTests(unittest.TestCase):
    def test_first_page_shape(self):
        api = civitai_api.CivitaiAPI(KEY)
        fake = FakeHTTP([envelope({"comments": [comment(1, "甲", "<p>好</p>")],
                                   "nextCursor": None, "replyThreads": [],
                                   "childlessCommentIds": [1]})])
        api._request = fake
        result = api.fetch_comments(2989579)
        sent = fake.inputs[0]
        self.assertEqual(sent["entityType"], "model")          # 必须小写；'Model' → 400 invalid_value
        self.assertEqual(sent["entityId"], 2989579)
        self.assertNotIn("cursor", sent)                       # 第一页整个省略；cursor:null → 400
        self.assertEqual(sent["repliesDepth"], 2)              # 不显式给 → 回复恒为 0 条
        self.assertEqual(sent["repliesLimit"], 20)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["items"]), 1)

    def test_cursor翻页用nextCursor数字(self):
        api = civitai_api.CivitaiAPI(KEY)
        fake = FakeHTTP([
            envelope({"comments": [comment(1, "甲", "<p>一</p>")], "nextCursor": 100,
                      "replyThreads": [], "childlessCommentIds": [1]}),
            envelope({"comments": [comment(101, "乙", "<p>二</p>")], "nextCursor": None,
                      "replyThreads": [], "childlessCommentIds": [101]}),
        ])
        api._request = fake
        result = api.fetch_comments(42)
        self.assertEqual(len(fake.inputs), 2)
        self.assertEqual(fake.inputs[1]["cursor"], 100)
        self.assertNotIn("cursor", fake.inputs[0])
        self.assertEqual([it["content"] for it in result["items"]], ["一", "二"])
        self.assertEqual(result["pages"], 2)
        self.assertFalse(result["truncated"])

    def test_truncated_when_page_limit_reached(self):
        api = civitai_api.CivitaiAPI(KEY)
        fake = FakeHTTP([envelope({"comments": [comment(i, "u%d" % i, "<p>t%d</p>" % i)],
                                   "nextCursor": i, "replyThreads": [],
                                   "childlessCommentIds": [i]}) for i in (1, 2)])
        api._request = fake
        result = api.fetch_comments(42, max_pages=2)
        self.assertTrue(result["truncated"])                   # 站点还在给 cursor：不能谎称全量

    def test_bad_id_does_not_go_online(self):
        api = civitai_api.CivitaiAPI(KEY)
        api._request = Mock(side_effect=AssertionError("不该发请求"))
        for bad in (None, "", "0", "abc", True, -3):
            self.assertEqual(api.fetch_comments(bad)["status"], "bad_id")

    def test_no_key_never_touches_network(self):
        api = civitai_api.CivitaiAPI("")          # 没 Key：匿名 tRPC 只会「成功地拿到空结果」
        api._request = Mock(side_effect=AssertionError("不该发请求"))
        self.assertEqual(api.fetch_comments(42)["status"], "no_key")
        self.assertEqual(api.fetch_reviews(42, 7)["status"], "no_key")
        api._request.assert_not_called()


class ReplyThreadTests(unittest.TestCase):
    def test_reply_threads_expand_with_parent(self):
        api = civitai_api.CivitaiAPI(KEY)
        data = {"comments": [comment(1, "楼主", "<p>主评论</p>"), comment(2, "二楼", "<p>另一条</p>")],
                "nextCursor": None,
                "replyThreads": [{"id": 900, "commentId": 1, "depth": 1, "commentCount": 2,
                                  "comments": [comment(3, "路人", "<p>回复一号</p>"),
                                               comment(4, "楼主", "<p>自回</p>")]}],
                "childlessCommentIds": [2]}
        fake = FakeHTTP([envelope(data)])
        api._request = fake
        result = api.fetch_comments(7)
        self.assertEqual(result["reply_unknown"], 0)     # 1 有线程、2 在 childless 名单里
        items = result["items"]
        self.assertEqual([it["id"] for it in items], [1, 2, 3, 4])
        self.assertFalse(items[0]["reply"])
        self.assertTrue(items[2]["reply"])
        self.assertEqual(items[2]["reply_to"], "楼主")    # 父评论 id=1 → 用户名，不是裸 id
        text = civitai_api.render_comments_section(result, 7, stamp="2026-10-16 00:00:00")
        self.assertIn("  ↳ 回复 【路人】", text)
        self.assertIn("〔回复 @楼主〕", text)
        self.assertIn("主评论", text)
        self.assertNotIn("<p>", text)                     # 正文是消毒过的 HTML，必须转纯文本

    def test_reply_unknown_when_thread_budget_skipped(self):
        api = civitai_api.CivitaiAPI(KEY)
        fake = FakeHTTP([envelope({"comments": [comment(1, "甲", "<p>a</p>"),
                                                comment(2, "乙", "<p>b</p>")],
                                   "nextCursor": None, "replyThreads": [],
                                   "childlessCommentIds": []})])
        api._request = fake
        result = api.fetch_comments(7)
        self.assertEqual(result["reply_unknown"], 2)      # 既没线程也没进「无回复」名单 = 状态未知
        self.assertIn("回复可能没保存全",
                      civitai_api.render_comments_section(result, 7, stamp="t"))

    def test_duplicate_across_pages_deduped(self):
        api = civitai_api.CivitaiAPI(KEY)
        shared = comment(1, "甲", "<p>a</p>")
        fake = FakeHTTP([envelope({"comments": [shared], "nextCursor": 5, "replyThreads": [],
                                    "childlessCommentIds": [1]}),
                         envelope({"comments": [shared], "nextCursor": None, "replyThreads": [],
                                    "childlessCommentIds": [1]})])
        api._request = fake
        self.assertEqual(len(api.fetch_comments(7)["items"]), 1)


class NullVsEmptyTests(unittest.TestCase):
    def test_json_null_is_no_comment_section(self):
        api = civitai_api.CivitaiAPI(KEY)
        fake = FakeHTTP([{"result": {"data": {"json": None}}}])
        api._request = fake
        result = api.fetch_comments(2989579)
        self.assertEqual(result["status"], "no_thread")
        text = civitai_api.render_comments_section(result, 2989579, stamp="2026-10-16 00:00:00")
        self.assertIn("返回 null", text)
        self.assertIn("这个模型没有评论区", text)
        self.assertIn("不是抓取失败", text)
        self.assertNotIn("0 条 ≠", text)

    def test_empty_list_is_not_reported_as_no_comments(self):
        api = civitai_api.CivitaiAPI(KEY)
        fake = FakeHTTP([envelope({"comments": [], "nextCursor": None, "replyThreads": [],
                                    "childlessCommentIds": []})])
        api._request = fake
        text = civitai_api.render_comments_section(api.fetch_comments(7), 7, stamp="t")
        self.assertIn("返回 0 条", text)
        self.assertIn("不是一回事", text)
        self.assertNotIn("这个模型没有评论区", text)


class ReviewTests(unittest.TestCase):
    def test_payload_carries_both_ids_and_page(self):
        api = civitai_api.CivitaiAPI(KEY)
        page1 = {"items": [{"id": 11, "modelId": 2989579, "modelVersionId": 3389879,
                            "createdAt": "2026-10-01T00:00:00.000Z", "rating": 5,
                            "recommended": True, "details": "<p>很好用</p>", "nsfw": False,
                            "imageCount": 1, "commentCount": 0,
                            "user": {"id": 1, "username": "甲"}}],
                 "totalItems": 67, "currentPage": 1, "pageSize": 50, "totalPages": 2}
        page2 = dict(page1, items=[dict(page1["items"][0], id=12, details="")],
                     currentPage=2)
        fake = FakeHTTP([envelope(page1), envelope(page2)])
        api._request = fake
        result = api.fetch_reviews(2989579, 3389879)
        for sent in fake.inputs:
            self.assertEqual(sent["modelId"], 2989579)       # 只给 modelVersionId → 400
            self.assertEqual(sent["modelVersionId"], 3389879)  # 只给 modelId → 成功的空结果（假空）
            self.assertNotIn("cursor", sent)                 # 分页字段是 page，cursor 会被静默忽略
        self.assertEqual([s["page"] for s in fake.inputs], [1, 2])
        self.assertEqual(result["pages"], 2)                 # 在 totalPages=2 处停，不多打第 3 页
        self.assertEqual(result["total_items"], 67)
        self.assertEqual(len(result["items"]), 2)
        text = civitai_api.render_reviews_section(result, 2989579, 3389879, stamp="2026-10-16 00:00:00")
        self.assertIn("拿到 2 条评价", text)
        self.assertIn("totalItems=67", text)
        self.assertIn("【甲】 评分:5 推荐:是", text)
        self.assertIn("(只打了分，没有写文字评价)", text)

    def test_stops_at_total_pages_not_at_max_pages(self):
        api = civitai_api.CivitaiAPI(KEY)
        pages = [envelope({"items": [{"id": i, "details": "<p>p%d</p>" % i, "user": {"username": "u"}}],
                            "totalItems": 150, "currentPage": p, "pageSize": 50, "totalPages": 3})
                 for p, i in ((1, 1), (2, 2), (3, 3))]
        fake = FakeHTTP(pages)
        api._request = fake
        result = api.fetch_reviews(1, 2, max_pages=5)
        self.assertEqual(len(fake.inputs), 3)
        self.assertFalse(result["truncated"])

    def test_missing_version_id_refuses_fake_empty_call(self):
        api = civitai_api.CivitaiAPI(KEY)
        api._request = Mock(side_effect=AssertionError("不该发请求"))
        result = api.fetch_reviews(2989579, None)
        self.assertEqual(result["status"], "bad_id")
        self.assertIn("空结果", result["error"])
        api._request.assert_not_called()


class DegradationTests(unittest.TestCase):
    def test_401_and_429_do_not_raise(self):
        for status, expected in ((401, "unauthorized"), (403, "unauthorized"),
                                 (429, "rate_limited"), (400, "rejected"), (500, "http_error")):
            api = civitai_api.CivitaiAPI(KEY)
            api._request = Mock(side_effect=civitai_api.CivitaiError("HTTP %d: boom" % status,
                                                                    status=status))
            self.assertEqual(api.fetch_comments(7)["status"], expected, status)
            self.assertEqual(api.fetch_reviews(7, 8)["status"], expected, status)

    def test_network_error_does_not_raise(self):
        api = civitai_api.CivitaiAPI(KEY)
        api._request = Mock(side_effect=civitai_api.CivitaiError("timed out"))
        self.assertEqual(api.fetch_comments(7)["status"], "http_error")

    def test_degraded_text_never_claims_no_comments(self):
        api = civitai_api.CivitaiAPI(KEY)
        api._request = Mock(side_effect=civitai_api.CivitaiError("HTTP 401: Unauthorized", status=401))
        text = civitai_api.render_comments_section(api.fetch_comments(7), 7, stamp="t")
        self.assertIn("HTTP 401", text)
        self.assertIn("0 条 ≠", text)
        self.assertNotIn("这个模型没有评论区", text)

    def test_document_marks_channel_for_every_section(self):
        api = civitai_api.CivitaiAPI(KEY)
        comments = {"procedure": civitai_api.COMMENTS_PROCEDURE, "status": "no_thread",
                    "items": [], "pages": 0, "truncated": False, "reply_unknown": 0, "error": ""}
        reviews = {"procedure": civitai_api.REVIEWS_PROCEDURE, "status": "no_key",
                   "items": [], "pages": 0, "truncated": False, "error": "", "max_pages": 5}
        doc = civitai_api.build_comments_document(comments, reviews, 2989579, 3389879,
                                                  stamp="2026-10-16 00:00:00")
        self.assertIn("===== 评论 Comments", doc)
        self.assertIn("===== 评价 Reviews", doc)
        self.assertIn("这个模型没有评论区", doc)
        self.assertIn("未配置 Civitai API Key", doc)
        self.assertIn("模型ID: 2989579", doc)


class WebuiSidecarTests(unittest.TestCase):
    def api(self, cfg, client):
        a = webui.Api.__new__(webui.Api)
        a.cfg = cfg
        a._new_api = Mock(return_value=client)
        a.log_error = Mock()
        return a

    def task(self, model_id="2989579", version_id="3389879"):
        t = Mock()
        t.info = {"model_id": model_id, "version_id": version_id,
                  "meta": {"info": {"modelId": model_id, "versionId": version_id}}}
        t.filename = "demo.safetensors"
        return t

    def test_writes_comments_file_with_provenance(self):
        client = civitai_api.CivitaiAPI(KEY)
        client._request = FakeHTTP([
            envelope({"comments": [comment(1, "楼主", "<p>顶</p>")], "nextCursor": None,
                      "replyThreads": [], "childlessCommentIds": [1]}),
            envelope({"items": [{"id": 5, "rating": 5, "recommended": True, "details": "<p>好用</p>",
                                 "createdAt": "2026-10-01T00:00:00.000Z",
                                 "user": {"username": "甲"}}],
                      "totalItems": 1, "currentPage": 1, "totalPages": 1}),
        ])
        with TemporaryDirectory() as d:
            a = self.api({"api_key": KEY}, client)
            base = str(Path(d) / "demo")          # 与 _handle_dl_done 同口径：去扩展名的前缀
            status = a._archive_comments_sidecar(self.task(), base)
            path = Path(status["path"])
            self.assertTrue(status["ok"])
            self.assertEqual(path.name, "demo_评论.txt")      # 与扩展的 评论.txt 同名口径
            text = path.read_text(encoding="utf-8")
            self.assertIn("带 Key 的 tRPC 拿到 1 条", text)
            self.assertIn("拿到 1 条评价", text)
            self.assertIn("【楼主】", text)
            self.assertIn("好用", text)

    def test_without_key_no_online_request_and_no_file(self):
        with TemporaryDirectory() as d:
            client = civitai_api.CivitaiAPI(KEY)
            client._request = Mock(side_effect=AssertionError("不该发请求"))
            a = self.api({"api_key": ""}, client)
            status = a._archive_comments_sidecar(self.task(), str(Path(d) / "demo"))
            self.assertEqual(status["skipped"], "no_api_key")
            a._new_api.assert_not_called()
            self.assertEqual(list(Path(d).iterdir()), [])
            self.assertIn("API Key", a.log_error.call_args_list[0][0][1])

    def test_401_still_writes_explanatory_file_and_download_survives(self):
        client = civitai_api.CivitaiAPI(KEY)
        client._request = Mock(side_effect=civitai_api.CivitaiError("HTTP 401: Unauthorized",
                                                                   status=401))
        with TemporaryDirectory() as d:
            a = self.api({"api_key": KEY}, client)
            status = a._archive_comments_sidecar(self.task(), str(Path(d) / "demo"))
            text = (Path(d) / "demo_评论.txt").read_text(encoding="utf-8")
            self.assertEqual(status["comment_status"], "unauthorized")
            self.assertEqual(status["reviews"], 0)
            self.assertIn("HTTP 401", text)
            self.assertIn("0 条 ≠", text)
            self.assertTrue(a.log_error.called)          # 降级原因进日志

    def test_blowup_inside_fetch_is_only_logged(self):
        client = civitai_api.CivitaiAPI(KEY)
        client.fetch_comments = Mock(side_effect=RuntimeError("炸了"))
        with TemporaryDirectory() as d:
            a = self.api({"api_key": KEY}, client)
            status = a._archive_comments_sidecar(self.task(), str(Path(d) / "demo"))
            self.assertEqual(status["skipped"], "error")
            self.assertFalse((Path(d) / "demo_评论.txt").exists())

    def test_handle_dl_done_keeps_running_without_key(self):
        """下载主流程不能被评论归档拖住：没 Key 时照旧写 .civitai.info，且不发在线请求"""
        with TemporaryDirectory() as d:
            a = webui.Api.__new__(webui.Api)
            a.cfg = {"gen_metadata": True, "metadata_format": "sd", "download_cover": False,
                     "archive_comments": True, "api_key": ""}
            a.log_error = Mock()
            a._new_api = Mock(side_effect=AssertionError("不该发请求"))
            a._cache_history_task = Mock()
            base = Path(d) / "demo.safetensors"
            base.write_bytes(b"synthetic")
            t = self.task()
            t.info["meta"] = {"info": {"name": "demo"}, "sd": {"prompt": "x"}}
            t.dest_dir, t.filename = d, "demo.safetensors"
            t.status = webui.downloader.ST_DONE
            t._metadata_started = False
            t.verification_status = "verified"
            a._handle_dl_done(t)
            self.assertTrue((Path(d) / "demo.civitai.info").exists())
            self.assertFalse((Path(d) / "demo_评论.txt").exists())
            a._new_api.assert_not_called()

    def test_handle_dl_done_writes_comments_sidecar_with_key(self):
        """点「发送到桌面端」下载完成后，磁盘上就要有评论/评价文本（不再依赖扩展）"""
        client = civitai_api.CivitaiAPI(KEY)
        client._request = FakeHTTP([
            envelope({"comments": [comment(1, "楼主", "<p>顶</p>")], "nextCursor": None,
                      "replyThreads": [{"id": 9, "commentId": 1, "depth": 1, "commentCount": 1,
                                        "comments": [comment(2, "路人", "<p>回复</p>")]}],
                      "childlessCommentIds": []}),
            envelope({"items": [{"id": 5, "rating": 4, "recommended": True,
                                 "details": "<p>好用</p>", "createdAt": "2026-10-01T00:00:00.000Z",
                                 "user": {"username": "甲"}}],
                      "totalItems": 1, "currentPage": 1, "totalPages": 1}),
        ])
        with TemporaryDirectory() as d:
            a = webui.Api.__new__(webui.Api)
            a.cfg = {"gen_metadata": True, "metadata_format": "sd", "download_cover": False,
                     "archive_comments": True, "api_key": KEY}
            a.log_error = Mock()
            a._new_api = Mock(return_value=client)
            a._cache_history_task = Mock()
            (Path(d) / "demo.safetensors").write_bytes(b"synthetic")
            t = self.task()
            t.info["meta"] = {"info": {"name": "demo"}, "sd": {"prompt": "x"}}
            t.dest_dir, t.filename = d, "demo.safetensors"
            t.status = webui.downloader.ST_DONE
            t._metadata_started = False
            t.verification_status = "verified"
            a._handle_dl_done(t)
            text = (Path(d) / "demo_评论.txt").read_text(encoding="utf-8")
            self.assertIn("带 Key 的 tRPC 拿到 2 条（顶层 1 条 / 回复 1 条）", text)
            self.assertIn("  ↳ 回复 【路人】(2026-10-03T00:00:00.000Z) 〔回复 @楼主〕", text)
            self.assertIn("拿到 1 条评价", text)
            self.assertEqual(t.info["comments_archive"]["comments"], 2)

    def test_comments_sidecar_moves_and_trashes_with_model(self):
        """改名/移动/回收必须带上评论 sidecar，否则模型走了、评论留在原地"""
        with TemporaryDirectory() as d:
            root = Path(d)
            src, dest = root / "m.safetensors", root / "dest"
            src.write_bytes(b"synthetic")
            (root / ("m" + webui._COMMENTS_SIDECAR_SUFFIX)).write_text("评论", encoding="utf-8")
            a = webui.Api.__new__(webui.Api)
            a.cfg = {}
            a.model_rows = []
            a._relocate_model = Mock()
            a.dl = Mock()
            a.dl.tasks = []
            dest.mkdir()
            self.assertTrue(a.move_file_to(str(src), str(dest))["ok"])
            self.assertTrue((dest / ("m" + webui._COMMENTS_SIDECAR_SUFFIX)).exists())


class BridgeRpcTests(unittest.TestCase):
    def test_comments_rpc_exposed_and_key_gated(self):
        """扩展/前端可以显式要一次评论文本，但门禁与下载路径同口径：没 Key 不出网。"""
        a = webui.Api.__new__(webui.Api)
        a.cfg = {"api_key": ""}
        a._new_api = Mock(side_effect=AssertionError("不该发请求"))
        out = json.loads(a.fetch_model_comments("2989579"))
        self.assertFalse(out["ok"])
        self.assertTrue(out["requires_api_key"])
        a._new_api.assert_not_called()

    def test_comments_rpc_returns_text_and_status(self):
        client = civitai_api.CivitaiAPI(KEY)
        client._request = FakeHTTP([
            envelope({"comments": [comment(1, "楼主", "<p>顶</p>")], "nextCursor": None,
                      "replyThreads": [], "childlessCommentIds": [1]}),
            envelope({"items": [], "totalItems": 0, "currentPage": 1, "totalPages": 1}),
        ])
        a = webui.Api.__new__(webui.Api)
        a.cfg = {"api_key": KEY}
        a._new_api = Mock(return_value=client)
        out = json.loads(a.fetch_model_comments("2989579", "3389879"))
        self.assertTrue(out["ok"])
        self.assertEqual(out["comments"], 1)
        self.assertEqual(out["comment_status"], "ok")
        self.assertEqual(out["review_status"], "ok")
        self.assertIn("带 Key 的 tRPC 拿到 1 条", out["text"])


if __name__ == "__main__":
    unittest.main()
