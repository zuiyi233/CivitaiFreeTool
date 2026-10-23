# -*- coding: utf-8 -*-
"""ai_tools：工具映射、危险操作闸门、next_actions。全部用假 dispatch，离线。"""
import json
import unittest
import urllib.error
from unittest.mock import patch

import ai_tools


class FakeDispatch:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def __call__(self, path, method, query, body, api, ctx):
        self.calls.append((method, path, query, body))
        return self.responses.get((method, path), (200, {"ok": True, "echo": path}))


class AiToolsTest(unittest.TestCase):
    def test_capabilities_has_mcp_and_limits(self):
        st, out = ai_tools.execute_tool("cft_capabilities", {}, None, {"version": "2.7.0"}, FakeDispatch())
        self.assertEqual(st, 200)
        self.assertTrue(out["data"]["mcp"]["stdio"])
        self.assertEqual(out["data"]["toolsets"]["core"]["count"], 19)

    def test_capabilities_reports_extension_and_mcp_http(self):
        def ext():
            return {"download_requests": 2, "tab_index": {
                "at": 1.0, "source": "chrome-extension", "ext_version": "2.6.1",
                "accepted": 3, "counts": {"models": 3}, "age_s": 2.0, "stale": False}}

        runtime = {"autostart": True, "running": True, "port": 47532,
                   "token_file": "C:/t.txt", "token_present": True}
        with patch.object(ai_tools, "_mcp_runtime", return_value=runtime):
            st, out = ai_tools.execute_tool(
                "cft_capabilities", {}, None,
                {"version": "2.7.0", "extension": ext}, FakeDispatch())
        self.assertEqual(st, 200)
        data = out["data"]
        self.assertTrue(data["extension"]["online"])
        self.assertEqual(data["extension"]["version"], "2.6.1")
        self.assertEqual(data["extension"]["tabs_indexed"], 3)
        self.assertEqual(data["mcp"]["http_endpoint"], "http://127.0.0.1:47532/mcp")
        self.assertTrue(data["mcp"]["running"])
        self.assertEqual(data["mcp"]["token_file"], "C:/t.txt")

    def test_browser_tabs_reads_bridge_and_suggests_download(self):
        calls = []

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n=-1):
                return json.dumps({
                    "ok": True, "at": 123.0, "age_s": 1.2, "stale": False,
                    "source": "chrome-extension", "counts": {"models": 1},
                    "total": 1, "matched": 1,
                    "items": [{
                        "title": "Live Wallpaper Style",
                        "url": "https://civitai.red/models/1264662/live-wallpaper-style"
                               "?modelVersionId=3351608",
                        "modelId": "1264662", "versionId": "3351608",
                    }],
                }).encode("utf-8")

        def fake_urlopen(url, timeout=None):
            calls.append(url)
            return Resp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            st, out = ai_tools.execute_tool(
                "cft_browser_tabs", {"query": "wallpaper", "limit": 5},
                None, {"bridge_port": 49999}, FakeDispatch())
        self.assertEqual(st, 200)
        self.assertEqual(out["data"]["items"][0]["versionId"], "3351608")
        self.assertEqual(out["next_actions"][0]["tool"], "cft_download_model")
        self.assertIn("127.0.0.1:49999/api/ext/tabs?", calls[0])
        self.assertIn("q=wallpaper", calls[0])
        self.assertIn("limit=5", calls[0])

    def test_browser_tabs_reports_bridge_offline(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            st, out = ai_tools.execute_tool("cft_browser_tabs", {}, None, {}, FakeDispatch())
        self.assertEqual(st, 503)
        self.assertTrue(out["hint"])
        self.assertIn("扩展", out["hint"])

    def test_unknown_tool(self):
        st, out = ai_tools.execute_tool("nope", {}, None, {}, FakeDispatch())
        self.assertEqual(st, 404)
        self.assertEqual(out["code"], "not_found")
        self.assertTrue(out["hint"])

    def test_dismiss_ui_maps_to_endpoint(self):
        fake = FakeDispatch({
            ("POST", "/api/automation/ui/dismiss"): (200, {
                "ok": True, "dismissed": ["onboarding"], "permanent": True}),
        })
        st, out = ai_tools.execute_tool("cft_dismiss_ui", {"permanent": True},
                                        None, {}, fake)
        self.assertEqual(st, 200)
        self.assertEqual(fake.calls[-1][1], "/api/automation/ui/dismiss")
        self.assertEqual(fake.calls[-1][3], {"permanent": True})
        self.assertEqual(out["data"]["dismissed"], ["onboarding"])

    def test_state_points_to_dismiss_ui(self):
        ctx = {"state": lambda: {
            "ok": True, "version": "2.7.0", "config": {}, "tasks": [],
            "pending_file_choice": None,
            "ui": {"guides": {"onboarding_pending": True}},
        }}
        st, out = ai_tools.execute_tool("cft_get_state", {}, None, ctx, FakeDispatch())
        self.assertEqual(st, 200)
        self.assertEqual(out["next_actions"][0]["tool"], "cft_dismiss_ui")
        self.assertEqual(out["data"]["blockers"][0]["code"], "onboarding_guide")

    def test_state_reports_blocking_overlay(self):
        ctx = {"state": lambda: {
            "ok": True, "version": "2.7.0", "config": {}, "tasks": [],
            "pending_file_choice": None,
            "ui": {"guides": {"onboarding_pending": False, "overlays": [
                {"name": "confirm", "group": "confirm", "kind": "confirmation",
                 "title": "确认操作", "blocking": True},
            ]}},
        }}
        st, out = ai_tools.execute_tool("cft_get_state", {}, None, ctx, FakeDispatch())
        self.assertEqual(st, 200)
        codes = [b["code"] for b in out["data"]["blockers"]]
        self.assertIn("ui_overlay", codes)
        self.assertEqual(out["next_actions"][0]["tool"], "cft_dismiss_ui")
        self.assertIn("取消", out["data"]["blockers"][0]["msg"])

    def test_library_destructive_gate(self):
        st, out = ai_tools.execute_tool("cft_manage_library",
                                        {"op": "cleanup", "paths": ["D:/m/a.safetensors"]},
                                        None, {}, FakeDispatch())
        self.assertEqual(st, 400)
        self.assertEqual(out["code"], "destructive_requires_confirm")
        st, out = ai_tools.execute_tool("cft_manage_library",
                                        {"op": "cleanup", "paths": ["D:/m/a.safetensors"],
                                         "confirm": True},
                                        None, {}, FakeDispatch())
        self.assertEqual(st, 200)

    def test_download_skipped_returns_verify_next(self):
        fake = FakeDispatch({
            ("POST", "/api/automation/download"): (200, {
                "ok": True, "skipped": "exists", "task": None,
                "files": [{"routed_dir": "D:/m/loras/demo", "name": "demo.safetensors"}]}),
        })
        st, out = ai_tools.execute_tool("cft_download_model", {"url": "https://x/models/1"},
                                        None, {}, fake)
        self.assertEqual(st, 200)
        self.assertEqual(out["next_actions"][0]["tool"], "cft_verify_model")
        self.assertIn("demo.safetensors", out["next_actions"][0]["arguments"]["path"])

    def test_download_timed_out_returns_task_wait(self):
        fake = FakeDispatch({
            ("POST", "/api/automation/download"): (200, {
                "ok": True, "timed_out": True, "task": {"id": "t1"},
                "items": [{"task": {"id": "t2"}}]}),
        })
        st, out = ai_tools.execute_tool("cft_download_model", {"url": "https://x/models/1"},
                                        None, {}, fake)
        self.assertEqual(st, 200)
        self.assertEqual(out["next_actions"][0]["tool"], "cft_list_tasks")
        self.assertEqual(out["next_actions"][0]["arguments"]["ids"], ["t2", "t1"])

    def test_task_wait_true_is_waited(self):
        fake = FakeDispatch()
        st, _out = ai_tools.execute_tool("cft_list_tasks", {"ids": ["a", "b"], "wait": True,
                                                            "timeout_s": 5}, None, {}, fake)
        self.assertEqual(st, 200)
        method, path, query, _body = fake.calls[-1]
        self.assertEqual((method, path), ("GET", "/api/automation/tasks"))
        self.assertEqual(query["ids"], ["a,b"])
        self.assertEqual(query["wait"], ["1"])
        self.assertEqual(query["timeout"], ["5"])

    def test_tasks_default_to_compact_projection(self):
        """不传 fields 时也必须能装进 4000 字符预算：默认投影，别把最新任务整行丢到。"""
        fake = FakeDispatch()
        st, _out = ai_tools.execute_tool("cft_list_tasks", {"limit": 50}, None, {}, fake)
        self.assertEqual(st, 200)
        _m, _p, query, _b = fake.calls[-1]
        fields = query["fields"][0]
        self.assertIn("verification_status", fields)
        self.assertIn("expected_sha256", fields)
        self.assertIn("routed_dir", fields)
        for heavy in ("postprocess", "postprocess_repair", "url", "speed"):
            self.assertNotIn(heavy, fields.split(","), "%s 不该进默认投影" % heavy)

    def test_tasks_fields_array_passes_through(self):
        fake = FakeDispatch()
        ai_tools.execute_tool("cft_list_tasks", {"fields": ["*"]}, None, {}, fake)
        self.assertEqual(fake.calls[-1][2]["fields"], ["*"])

    def test_tasks_over_budget_keeps_newest_rows(self):
        """有序数组超预算要丢最旧的：丢尾部会把 Agent 刚下的那条任务抹掉。"""
        rows = [{"id": "t%d" % i, "filename": "m%d.safetensors" % i, "status": "done",
                 "route_reason": "按 C 站模型类型 LORA 落 loras" * 6,
                 "expected_sha256": "AB" * 32, "actual_sha256": "ab" * 32,
                 "verification_status": "verified"} for i in range(12)]
        fake = FakeDispatch({("GET", "/api/automation/tasks"): (200, {
            "ok": True, "tasks": rows, "count": len(rows)})})
        st, out = ai_tools.execute_tool("cft_list_tasks", {"limit": 50}, None, {}, fake)
        self.assertEqual(st, 200)
        text = out["content"][0]["text"]
        self.assertLessEqual(len(text), 4000)
        data = json.loads(text)
        kept = data["tasks"]
        self.assertLess(len(kept), 12, "该触发的收缩没触发")
        self.assertEqual(kept[-1]["id"], "t11", "必须保住最新那条")
        self.assertTrue(data["truncated"])
        self.assertEqual(data["dropped_rows"]["$.tasks"], 12 - len(kept))
        self.assertIn("t0", data["oldest_dropped_ids"])
        self.assertEqual(data["count"], 12, "count 仍是总数，不能谎报只剩这些")

    def test_workflow_include_image_maps(self):
        fake = FakeDispatch({
            ("POST", "/api/automation/workflow/analyze"): (200, {
                "ok": True, "node_count": 1, "preview_available": True}),
        })
        st, out = ai_tools.execute_tool("cft_analyze_workflow",
                                        {"path": "D:/w.json", "include_image": True},
                                        None, {}, fake)
        self.assertEqual(st, 200)
        body = fake.calls[-1][3]
        self.assertTrue(body["include_image"])
        self.assertEqual(body["max_side"], 1024)

    def test_migrate_storage_gate(self):
        st, out = ai_tools.execute_tool("cft_migrate_storage", {"directory": "D:/data"},
                                        None, {}, FakeDispatch())
        self.assertEqual(st, 400)
        self.assertEqual(out["code"], "destructive_requires_confirm")

    def test_delete_gallery_gate(self):
        st, out = ai_tools.execute_tool("cft_delete_gallery", {"ids": [1]}, None, {}, FakeDispatch())
        self.assertEqual(st, 400)
        self.assertEqual(out["code"], "destructive_requires_confirm")


if __name__ == "__main__":
    unittest.main()
