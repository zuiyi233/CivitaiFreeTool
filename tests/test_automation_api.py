# -*- coding: utf-8 -*-
"""automation_api：能力清单 / 干跑 / 阻塞等待 / 落点校验 的离线测试。

全部用假 Api + 临时目录，不发网络请求、不写仓库文件。
"""
import json
import os
import unittest

import automation_api


def make_tree(root, stem="demo", with_sidecars=True):
    """在 root 下造一个模型落点：本体 + 可选附属文件"""
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, stem + ".safetensors")
    with open(path, "wb") as f:
        f.write(b"weights")
    if with_sidecars:
        for suf in (".civitai.info", ".json", ".preview.png", "_评论.txt"):
            with open(os.path.join(root, stem + suf), "w", encoding="utf-8") as f:
                f.write("x")
        os.makedirs(os.path.join(root, stem + ".images"), exist_ok=True)
        with open(os.path.join(root, stem + ".images", "video_01.mp4"), "wb") as f:
            f.write(b"v")
        with open(os.path.join(root, "评论.txt"), "w", encoding="utf-8") as f:
            f.write("c")
        with open(os.path.join(root, "example.json"), "w", encoding="utf-8") as f:
            f.write("{}")
    return path


class FakeCiv:
    def resolve_url(self, url):
        return (456, 123)

    def get_model(self, mid):
        return {"id": 456, "name": "Demo", "type": "LORA", "modelVersions": [{"id": 123}]}

    def get_model_version(self, vid):
        return {"id": 123, "name": "v1", "baseModel": "PonyXL", "modelId": 456,
                "files": [{"id": 1, "name": "demo.safetensors", "type": "Model",
                           "primary": True, "sizeKB": 1000,
                           "hashes": {"SHA256": "ab" * 32}}]}

    def model_files(self, version):
        return version["files"]

    def file_download_url(self, version, f):
        return "https://civitai.red/api/download/models/123?fileId=1"


class FakeApi:
    def __init__(self, tmp):
        self.cfg = {"comfyui_models_root": str(tmp / "models"), "route_by_type": True,
                    "site_domain": "civitai.red", "api_key": "secret"}
        self.api = FakeCiv()
        self.tasks = []
        self.submitted = []
        self.mm_calls = []
        self.moves = []
        self.renames = []

    def get_tasks(self):
        return list(self.tasks)

    def get_download_file_choice(self):
        return None

    def library_check(self, version_id):
        return {"found": False, "paths": []}

    def _dup_in_queue(self, *a):
        return False

    def _needs_header_probe(self, *a):
        return False

    def get_scan_rows(self):
        return json.dumps([{"path": "D:/m/a.safetensors", "name": "a", "type": "LORA",
                            "type_filter": "loras", "base": "PonyXL", "size": 1,
                            "ver": "v1", "verId": "1", "modelId": "2"}])

    def get_scan_state(self):
        return {"running": False, "msg": "", "total": 1, "roots": []}

    def get_data_storage_info(self):
        return {"current": "D:/data"}

    def error_log_path(self):
        return os.path.join(str(self.cfg.get("_tmp", "")), "error.log")

    def dl_submit_url_from_bridge(self, url, dest_subdir="", caps=None):
        self.submitted.append({"url": url, "dest_subdir": dest_subdir, "caps": caps})
        dest = os.path.join(self.cfg["comfyui_models_root"], "loras", "demo")
        make_tree(dest)
        self.tasks.append({"id": "t%d" % (len(self.tasks) + 1), "filename": "demo.safetensors",
                           "status": "done", "dest_dir": dest, "routed_dir": dest,
                           "progress": 100.0, "downloaded": 7, "total": 7, "error": "",
                           "info": {}})
        return {"ok": True, "started": True, "dest_dir": dest, "routed": True, "bundle": "tok"}

    # ---- 扩展面用到的既有能力（都保持离线/无副作用）----
    def _site_url(self, model_id, version_id=None):
        u = "https://civitai.red/models/%s" % model_id
        return u + ("?modelVersionId=%s" % version_id if version_id else "")

    def _info_images(self, path):
        return [{"type": "image", "url": "https://x/1.jpg", "width": 512, "height": 768,
                 "meta": {"prompt": "a cat", "negativePrompt": "bad", "steps": 28, "seed": 7}}]

    def get_download_history(self):
        return {"items": [
            {"id": "h1", "filename": "a.safetensors", "status": "done", "dest_dir": "D:/m",
             "routed_dir": "D:/m/loras/a", "file_path": "D:/m/loras/a/a.safetensors",
             "file_exists": True, "total": 10, "error": "", "finished_at": 100},
            {"id": "h2", "filename": "b.safetensors", "status": "error", "dest_dir": "D:/m",
             "routed_dir": "", "file_path": "D:/m/b.safetensors", "file_exists": False,
             "total": 0, "error": "boom", "finished_at": 200}], "error": None}

    def get_mm_progress(self):
        return {"running": False, "total": 1, "done": 1, "msg": "校验完成", "result": None}

    def get_mm_update_state(self):
        return {"running": False, "total": 1, "done": 1, "newer": 0, "items": {}}

    def mm_verify(self, paths=None):
        self.mm_calls.append(("mm_verify", paths))
        return {"started": True}

    def mm_dedupe_delete(self, paths):
        self.mm_calls.append(("mm_dedupe_delete", paths))
        return {"started": True}

    def download_all_images(self, path):
        return json.dumps({"ok": True, "downloaded": 3, "existing": 0, "dir": path + ".images"})

    def get_img_dl_state(self):
        return json.dumps({"total": 3, "done": 3})

    def get_images_autofill_state(self, path=""):
        return {"ok": True, "running": False, "total": 3, "done": 3}

    def analyze_workflow(self, path):
        return json.dumps({"ok": True, "file": "wf.json", "source_path": path,
                           "has_workflow": True, "source": "comfyui", "node_count": 2,
                           "models": ["demo.safetensors"], "pos_prompt": "a cat",
                           "neg_prompt": "", "prompt_notes": "",
                           "generation_parameters": {}, "disabled_models": [],
                           "preview_b64": "AAAA",
                           "nodes": [{"id": 1, "type": "KSampler", "title": ""},
                                     {"id": 2, "type": "CheckpointLoader", "title": "ck"}],
                           "edges": [{"source": 2, "target": 1, "input": "model", "slot": 0}]})

    def move_file_to(self, path, dest_dir):
        self.moves.append(("file", path, dest_dir))
        return {"ok": True, "path": path}

    def set_task_target(self, task_id, dest_dir):
        self.moves.append(("task", task_id, dest_dir))
        return {"ok": True}

    def history_move_to(self, task_id, dest_dir):
        self.moves.append(("history", task_id, dest_dir))
        return {"ok": True}

    def rename_file(self, path, new_name):
        self.renames.append(("file", path, new_name))
        return {"ok": True, "path": path}

    def mm_rename_preview(self, act="rename_c", paths=None):
        self.renames.append(("preview", paths))
        return {"ok": True, "items": [{"old": "a", "new": "b", "same": False}]}

    def mm_rename(self, paths=None):
        self.renames.append(("batch", paths))
        return {"started": True}

    def mm_organize(self, paths=None):
        self.mm_calls.append(("mm_organize", paths))
        return {"started": True}

    def mm_cleanup(self, paths=None):
        self.mm_calls.append(("mm_cleanup", paths))
        return {"started": True}

    def workflow_model_matches(self, refs):
        return json.dumps([{"ref": r, "local": True, "path": "D:/m/" + r, "sha256": "abc"}
                           for r in refs])


def ctx(api):
    return {"version": "test", "state": lambda: {"ok": True},
            "extension": lambda: {"download_requests": 1},
            "config_keys": list(__import__("browser_bridge").AUTOMATION_CONFIG_KEYS),
            "rpc_methods": [], "data_dir": "", "error_log": "",
            "save_config": lambda body: (200, {"ok": True})}


class VerifyDirTest(unittest.TestCase):
    def test_complete_bundle_passes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            make_tree(d)
            out = automation_api._verify_dir(d, "demo.safetensors")
            self.assertTrue(out["ok"], out)
            self.assertTrue(out["exists"])
            self.assertEqual(out["images"]["count"], 1)
            self.assertEqual(out["sidecars"]["missing"], [])

    def test_missing_sidecar_fails(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            make_tree(d, with_sidecars=False)
            out = automation_api._verify_dir(d, "demo.safetensors")
            self.assertFalse(out["ok"])
            self.assertIn("demo.civitai.info", out["sidecars"]["missing"])

    def test_video_cover_satisfies_preview(self):
        """封面是视频的模型没有 .preview.png：<stem>.mp4 必须能顶上，否则误报缺失。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = make_tree(d)
            os.remove(os.path.join(d, "demo.preview.png"))
            out = automation_api._verify_dir(d, "demo.safetensors")
            self.assertFalse(out["ok"])                     # 两者都没有才判缺失
            open(os.path.join(d, "demo.mp4"), "wb").write(b"v")
            out = automation_api._verify_dir(d, "demo.safetensors")
            self.assertEqual(out["preview"], "demo.mp4")
            self.assertTrue(out["ok"], out)

    def test_hash_checked_when_requested(self):
        import hashlib
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = make_tree(d)
            want = hashlib.sha256(open(p, "rb").read()).hexdigest().upper()
            out = automation_api._verify_dir(d, "demo.safetensors", want, True)
            self.assertTrue(out["sha256_ok"])
            bad = automation_api._verify_dir(d, "demo.safetensors", "00" * 32, True)
            self.assertFalse(bad["sha256_ok"])
            self.assertFalse(bad["ok"])

    def test_missing_dir_reports_error(self):
        out = automation_api._verify_dir("Z:/definitely/not/here", "x.safetensors")
        self.assertFalse(out["ok"])
        self.assertIn("不存在", out["error"])


class DispatchTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.api = FakeApi(__import__("pathlib").Path(self.tmp.name))
        os.makedirs(os.path.join(self.api.cfg["comfyui_models_root"], "loras"), exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unknown_path_returns_none(self):
        self.assertIsNone(automation_api.dispatch("/api/automation/nope", "GET", {}, None,
                                                  self.api, ctx(self.api)))

    def test_manifest_lists_endpoints(self):
        status, body = automation_api.dispatch("/api/automation", "GET", {}, None,
                                               self.api, ctx(self.api))
        self.assertEqual(status, 200)
        paths = {e["path"] for e in body["endpoints"]}
        for p in ("/api/automation/plan", "/api/automation/download",
                  "/api/automation/tasks", "/api/automation/verify",
                  "/api/automation/logs", "/api/automation/library"):
            self.assertIn(p, paths)
        self.assertEqual(body["version"], "test")

    def test_plan_is_dry_run(self):
        status, body = automation_api.dispatch("/api/automation/plan", "POST", {},
                                               {"url": "https://civitai.red/models/456"}, self.api, ctx(self.api))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["model"]["type"], "LORA")
        f = body["files"][0]
        self.assertEqual(f["folder"], "loras")
        self.assertTrue(f["would_route"])
        self.assertTrue(f["routed_dir"].endswith(os.path.join("loras", "demo")))
        self.assertFalse(f["exists"])
        self.assertEqual(self.api.submitted, [], "plan 绝不能入队")
        self.assertFalse(os.path.isdir(f["routed_dir"]), "plan 绝不能建目录")

    def test_plan_rejects_empty_url(self):
        status, body = automation_api.dispatch("/api/automation/plan", "POST", {}, {}, self.api, ctx(self.api))
        self.assertEqual(status, 400)

    def test_download_waits_and_verifies(self):
        status, body = automation_api.dispatch("/api/automation/download", "POST", {},
                                               {"url": "https://civitai.red/models/456",
                                                "wait": True, "timeout": 5}, self.api, ctx(self.api))
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"], body)
        self.assertEqual(self.api.submitted[0]["caps"], ["bundle"], "自动化默认声明 bundle 能力")
        self.assertEqual(body["task"]["status"], "done")
        self.assertTrue(body["verify"]["ok"])
        self.assertFalse(body["timed_out"])

    def test_tasks_wait_returns_terminal(self):
        self.api.tasks.append({"id": "t1", "filename": "a", "status": "done",
                               "created_at": 1, "dest_dir": "", "routed_dir": "",
                               "route_reason": "", "progress": 100.0,
                               "downloaded": 1, "total": 1, "error": ""})
        status, body = automation_api.dispatch("/api/automation/tasks", "GET",
                                               {"ids": ["t1"], "wait": ["1"], "timeout": ["2"]},
                                               None, self.api, ctx(self.api))
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        self.assertFalse(body["timed_out"])

    def test_tasks_action_rejects_bad_action(self):
        status, body = automation_api.dispatch("/api/automation/tasks", "POST", {},
                                               {"action": "explode", "ids": ["t1"]}, self.api, ctx(self.api))
        self.assertEqual(status, 400)

    def test_verify_by_path(self):
        d = os.path.join(self.api.cfg["comfyui_models_root"], "loras", "demo")
        make_tree(d)
        status, body = automation_api.dispatch("/api/automation/verify", "GET",
                                               {"path": [d]}, None, self.api, ctx(self.api))
        self.assertEqual(status, 200)
        self.assertTrue(body["exists"])

    def test_verify_unknown_task_404(self):
        status, body = automation_api.dispatch("/api/automation/verify", "GET",
                                               {"task_id": ["nope"]}, None, self.api, ctx(self.api))
        self.assertEqual(status, 404)

    def test_library_parses_json_string(self):
        status, body = automation_api.dispatch("/api/automation/library", "GET",
                                               {"q": ["a"]}, None, self.api, ctx(self.api))
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["rows"][0]["type_filter"], "loras")

    def test_events_recorded(self):
        automation_api.dispatch("/api/automation", "GET", {}, None, self.api, ctx(self.api))
        status, body = automation_api.dispatch("/api/automation/logs", "GET",
                                               {"source": ["events"], "tail": ["5"]},
                                               None, self.api, ctx(self.api))
        self.assertEqual(status, 200)
        self.assertIn("events", body)

    def test_file_choice_answers_pending(self):
        self.api.answered = []

        def respond(identity, indices=None, pause=False):
            self.api.answered.append((identity, indices, pause))
            return {"ok": True}

        self.api.respond_download_file_choice = respond
        status, body = automation_api.dispatch("/api/automation/file-choice", "POST", {},
                                               {"identity": "c1", "indices": [0]},
                                               self.api, ctx(self.api))
        self.assertEqual(status, 200)
        self.assertEqual(self.api.answered, [("c1", [0], False)])

    def test_file_choice_requires_identity(self):
        status, body = automation_api.dispatch("/api/automation/file-choice", "POST", {},
                                               {"indices": [0]}, self.api, ctx(self.api))
        self.assertEqual(status, 400)

    def test_model_detail_includes_files(self):
        d = os.path.join(self.api.cfg["comfyui_models_root"], "loras", "demo")
        path = make_tree(d)
        self.api.get_model_detail = lambda p: json.dumps({"name": "Demo", "type": "LORA"})
        status, body = automation_api.dispatch("/api/automation/model", "GET",
                                               {"path": [path]}, None, self.api, ctx(self.api))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["detail"]["name"], "Demo")
        self.assertTrue(body["files"]["exists"])
        self.assertEqual(body["files"]["sidecars"]["missing"], [])

    def test_model_detail_missing_file_404(self):
        status, body = automation_api.dispatch("/api/automation/model", "GET",
                                               {"path": ["Z:/nope.safetensors"]},
                                               None, self.api, ctx(self.api))
        self.assertEqual(status, 404)

    def test_search_shapes_items(self):
        def fake_get(path, params=None, raw=False, retries=3):
            self.assertEqual(path, "/models")
            self.assertEqual(params["limit"], 2)
            return {"items": [{"id": 1, "name": "A", "type": "LORA", "nsfw": False,
                               "stats": {"downloadCount": 3},
                               "modelVersions": [{"id": 9, "name": "v1", "baseModel": "PonyXL"}]}],
                    "metadata": {"totalItems": 1}}

        self.api.api._get = fake_get
        status, body = automation_api.dispatch("/api/automation/search", "POST", {},
                                               {"query": "rezero", "limit": 2},
                                               self.api, ctx(self.api))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["items"][0]["versions"][0]["baseModel"], "PonyXL")

    def test_search_bounds_limit(self):
        seen = {}

        def fake_get(path, params=None, raw=False, retries=3):
            seen.update(params or {})
            return {"items": []}

        self.api.api._get = fake_get
        automation_api.dispatch("/api/automation/search", "POST", {},
                                {"limit": 999}, self.api, ctx(self.api))
        self.assertEqual(seen["limit"], 20, "limit 必须夹在 1–20")

    def test_connectivity_reports_api_failure_with_hint(self):
        def boom(path, params=None, raw=False, retries=3):
            raise RuntimeError("timed out")

        self.api.api._get = boom
        status, body = automation_api.dispatch("/api/automation/connectivity", "GET", {},
                                               None, self.api, ctx(self.api))
        self.assertEqual(status, 200)
        self.assertFalse(body["api"]["ok"])
        self.assertIn("timed out", body["api"]["error"])
        self.assertIn("hint", body)
        self.assertIn("b2.civitai.com", body["dns"])

    def test_connectivity_api_ok(self):
        self.api.api._get = lambda path, params=None, raw=False, retries=3: {"items": []}
        status, body = automation_api.dispatch("/api/automation/connectivity", "GET", {},
                                               None, self.api, ctx(self.api))
        self.assertEqual(status, 200)
        self.assertTrue(body["api"]["ok"])
        self.assertEqual(body["proxy"]["enabled"], False)


class ExpandedSurfaceTest(unittest.TestCase):
    """第二批接口：schema / resolve / events / history / prompts / library-op / images / workflow / storage"""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.TemporaryDirectory()
        self.api = FakeApi(__import__("pathlib").Path(self.tmpdir.name))
        os.makedirs(os.path.join(self.api.cfg["comfyui_models_root"], "loras"), exist_ok=True)

    def call(self, path, method="GET", query=None, body=None):
        return automation_api.dispatch(path, method, query or {}, body, self.api, ctx(self.api))

    def test_schema_is_self_describing(self):
        status, body = self.call("/api/automation/schema")
        self.assertEqual(status, 200)
        self.assertEqual(body["config"]["route_by_type"]["type"], "bool")
        self.assertEqual(body["config"]["download_name_mode"]["options"],
                         ["original", "civitai", "chinese"])
        self.assertIn("loras", body["routing"]["core_folders"])
        self.assertEqual(body["routing"]["type_to_folder"]["LORA"], "loras")
        self.assertIn("verify", body["library_ops"])
        self.assertIn(".preview.png", body["sidecars"]["preview_any_of"])

    def test_resolve_lists_versions_and_files(self):
        status, body = self.call("/api/automation/resolve", "POST", body={"url": "https://civitai.red/models/456"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["versions"][0]["id"], 123)
        self.assertEqual(body["files"][0]["name"], "demo.safetensors")

    def test_resolve_rejects_empty_target(self):
        status, body = self.call("/api/automation/resolve", "POST", body={})
        self.assertEqual(status, 400)
        self.assertIn("至少给一个", body["msg"])

    def test_download_by_model_id_builds_site_url(self):
        status, body = self.call("/api/automation/download", "POST",
                                 body={"model_id": 456, "version_id": 123, "wait": False})
        self.assertEqual(status, 200, body)
        self.assertIn("/models/456?modelVersionId=123", self.api.submitted[-1]["url"])

    def test_download_batch_submits_each_url(self):
        status, body = self.call("/api/automation/download", "POST",
                                 body={"urls": ["https://civitai.red/models/456?modelVersionId=123",
                                                "https://civitai.red/models/789?modelVersionId=999"],
                                       "wait": True, "timeout": 2})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(self.api.submitted[-2:]), 2)
        self.assertEqual(len(body["items"]), 2)
        self.assertTrue(all(i["ok"] for i in body["items"]), body)

    def test_download_file_ids_expand_into_separate_targets(self):
        status, body = self.call("/api/automation/download", "POST",
                                 body={"url": "https://civitai.red/models/456?modelVersionId=123",
                                       "file_ids": [1, 2], "wait": False})
        self.assertEqual(status, 200, body)
        urls = [s["url"].split("fileId=")[-1] for s in self.api.submitted[-2:]]
        self.assertEqual(urls, ["1", "2"])

    def test_download_rejects_urls_plus_file_ids(self):
        status, body = self.call("/api/automation/download", "POST",
                                 body={"urls": ["https://civitai.red/models/456"], "file_ids": [1]})
        self.assertEqual(status, 400)
        self.assertIn("不能和 urls 同时用", body["msg"])

    def test_download_rejects_bad_file_ids(self):
        status, body = self.call("/api/automation/download", "POST",
                                 body={"url": "https://civitai.red/models/456", "file_ids": "1"})
        self.assertEqual(status, 400)
        self.assertIn("file_ids", body["msg"])

    def test_events_expose_seq_cursor(self):
        automation_api.record_event("GET", "/api/automation/state", 200, 1.5, "ok")
        status, body = self.call("/api/automation/events", query={"since": ["0"]})
        self.assertEqual(status, 200)
        self.assertTrue(body["events"], body)
        self.assertGreaterEqual(body["next_since"], body["events"][-1]["seq"])
        status, body = self.call("/api/automation/events", query={"since": [str(body["next_since"])]})
        self.assertEqual(body["events"], [])

    def test_history_is_newest_first_and_compact(self):
        status, body = self.call("/api/automation/history", query={"limit": ["1"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["items"][0]["id"], "h2")          # finished_at 更大在前
        self.assertNotIn("manual_binding", body["items"][0])
        status, body = self.call("/api/automation/history", query={"detail": ["1"], "status": ["done"]})
        self.assertEqual(body["items"][0]["id"], "h1")

    def test_prompts_reads_info_meta(self):
        path = make_tree(os.path.join(self.tmpdir.name, "loras", "demo"))
        status, body = self.call("/api/automation/prompts", query={"path": [path]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["images"][0]["prompt"], "a cat")
        self.assertEqual(body["images"][0]["params"]["steps"], 28)

    def test_prompts_missing_file_is_404(self):
        status, body = self.call("/api/automation/prompts", query={"path": ["Z:/nope.safetensors"]})
        self.assertEqual(status, 404)

    def test_library_op_waits_and_reports_progress(self):
        status, body = self.call("/api/automation/library/op", "POST", body={"op": "verify"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.api.mm_calls[-1], ("mm_verify", None))
        self.assertFalse(body["progress"]["running"])
        self.assertFalse(body["timed_out"])

    def test_library_op_rejects_unknown_and_unguarded_delete(self):
        status, body = self.call("/api/automation/library/op", "POST", body={"op": "nope"})
        self.assertEqual(status, 400)
        self.assertIn("op 必须是", body["msg"])
        status, body = self.call("/api/automation/library/op", "POST",
                                 body={"op": "dedupe-delete", "paths": ["D:/m/a.safetensors"]})
        self.assertEqual(status, 400)
        self.assertIn("confirm", body["msg"])
        status, body = self.call("/api/automation/library/op", "POST",
                                 body={"op": "dedupe-delete", "confirm": True,
                                       "paths": ["D:/m/a.safetensors"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.api.mm_calls[-1][0], "mm_dedupe_delete")

    def test_images_download_waits_for_progress(self):
        path = make_tree(os.path.join(self.tmpdir.name, "loras", "demo"))
        status, body = self.call("/api/automation/images/download", "POST",
                                 body={"path": path, "timeout": 5})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["timed_out"])
        self.assertEqual(body["state"]["total"], 3)

    def test_workflow_strips_preview_and_matches_locally(self):
        path = os.path.join(self.tmpdir.name, "wf.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}")
        status, body = self.call("/api/automation/workflow/analyze", "POST", body={"path": path})
        self.assertEqual(status, 200, body)
        self.assertNotIn("preview_b64", body)
        self.assertIn("pos_prompt", body)
        self.assertEqual(body["nodes"][0]["type"], "KSampler")
        self.assertEqual(body["edges_count"], 1)
        self.assertTrue(body["local_matches"][0]["local"])
        status, body = self.call("/api/automation/workflow/analyze", "POST",
                                 body={"path": path, "full": True})
        self.assertIn("edges", body)

    def test_storage_reports_type_dirs(self):
        status, body = self.call("/api/automation/storage")
        self.assertEqual(status, 200)
        folders = {d["folder"]: d for d in body["type_dirs"]}
        self.assertTrue(folders["loras"]["exists"])
        self.assertFalse(folders["checkpoints"]["exists"])
        self.assertTrue(body["routing"]["enabled"])


class BatchAndFilesTest(unittest.TestCase):
    """一次 HTTP 多次调用（batch）、字段投影（fields）、文件管理（move/rename）"""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.TemporaryDirectory()
        self.api = FakeApi(__import__("pathlib").Path(self.tmpdir.name))
        self.api.tasks.append({"id": "t1", "filename": "demo.safetensors", "status": "done",
                               "dest_dir": "D:/m", "routed_dir": "D:/m/loras/demo",
                               "progress": 100.0, "downloaded": 7, "total": 7,
                               "error": "",
                               "info": {"routed_dir": "D:/m/loras/demo"}})

    def call(self, path, method="GET", query=None, body=None):
        return automation_api.dispatch(path, method, query or {}, body, self.api, ctx(self.api))

    def test_batch_runs_calls_in_order_and_keeps_http_status(self):
        status, body = self.call("/api/automation/batch", "POST", body={"calls": [
            {"method": "GET", "path": "/api/automation/tasks",
             "query": {"limit": 5, "fields": "id,status"}},
            {"method": "GET", "path": "/api/automation/schema"}]})
        self.assertEqual(status, 200, body)                 # batch 本身恒 200
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["executed"], 2)
        self.assertEqual(body["results"][0]["http_status"], 200)
        row = body["results"][0]["result"]["tasks"][0]
        self.assertEqual(set(row), {"id", "status"})        # fields 生效
        self.assertIn("config", body["results"][1]["result"])

    def test_batch_rejects_foreign_path_and_nesting(self):
        status, body = self.call("/api/automation/batch", "POST", body={"calls": [
            {"method": "POST", "path": "/api/rpc", "body": {"method": "x"}},
            {"method": "POST", "path": "/api/automation/batch", "body": {"calls": []}}]})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertEqual([r["http_status"] for r in body["results"]], [400, 400])
        self.assertIn("hint", body["results"][0]["result"])

    def test_batch_stop_on_error(self):
        status, body = self.call("/api/automation/batch", "POST", body={
            "calls": [{"method": "GET", "path": "/api/automation/nope"},
                      {"method": "GET", "path": "/api/automation/tasks"}],
            "stop_on_error": True})
        self.assertEqual(len(body["results"]), 1)
        self.assertEqual(body["results"][0]["http_status"], 404)
        self.assertEqual(body["requested"], 2)

    def test_batch_rejects_empty_and_too_many(self):
        status, body = self.call("/api/automation/batch", "POST", body={"calls": []})
        self.assertEqual(status, 400)
        self.assertIn("hint", body)
        status, body = self.call("/api/automation/batch", "POST",
                                 body={"calls": [{"path": "/api/automation/state"}] * 51})
        self.assertEqual(status, 400)

    def test_fields_support_dotted_paths(self):
        status, body = self.call("/api/automation/tasks", query={"fields": ["id,info.routed_dir"]})
        self.assertEqual(status, 200)
        row = body["tasks"][0]
        self.assertEqual(row["id"], "t1")
        self.assertEqual(row["info"]["routed_dir"], "D:/m/loras/demo")
        self.assertIn("count", body)                        # 元信息永远保留

    def test_fields_on_library_rows(self):
        status, body = self.call("/api/automation/library", query={"fields": ["name,type"]})
        self.assertEqual(status, 200)
        self.assertEqual(set(body["rows"][0]), {"name", "type"})
        self.assertIn("total", body)

    def test_move_requires_existing_absolute_dir(self):
        status, body = self.call("/api/automation/move", "POST", body={"path": "D:/m/a.safetensors"})
        self.assertEqual(status, 400)
        self.assertIn("hint", body)
        status, body = self.call("/api/automation/move", "POST",
                                 body={"path": "D:/m/a.safetensors", "dest_dir": "Z:/nope"})
        self.assertEqual(status, 400)
        self.assertIn("不存在", body["msg"])

    def test_move_dispatches_to_the_right_action(self):
        import tempfile
        d = tempfile.mkdtemp(dir=self.tmpdir.name)
        for body, expect in (
                ({"path": "D:/m/a.safetensors", "dest_dir": d}, "file"),
                ({"task_id": "t1", "dest_dir": d}, "task"),
                ({"history_id": "h1", "dest_dir": d}, "history")):
            status, out = self.call("/api/automation/move", "POST", body=body)
            self.assertEqual(status, 200, out)
            self.assertEqual(self.api.moves[-1][0], expect)
        status, out = self.call("/api/automation/move", "POST", body={"dest_dir": d})
        self.assertEqual(status, 400)

    def test_rename_single_and_batch(self):
        status, out = self.call("/api/automation/rename", "POST",
                                body={"path": "D:/m/a.safetensors", "new_name": "b"})
        self.assertEqual(status, 200, out)
        self.assertEqual(self.api.renames[-1], ("file", "D:/m/a.safetensors", "b"))
        status, out = self.call("/api/automation/rename", "POST",
                                body={"paths": ["D:/m/a.safetensors"], "preview": True})
        self.assertEqual(status, 200, out)
        self.assertEqual(self.api.renames[-1], ("preview", ["D:/m/a.safetensors"]))
        self.call("/api/automation/rename", "POST", body={"paths": ["D:/m/a.safetensors"]})
        self.assertEqual(self.api.renames[-1], ("batch", ["D:/m/a.safetensors"]))
        status, out = self.call("/api/automation/rename", "POST", body={})
        self.assertEqual(status, 400)
        self.assertIn("hint", out)

    def test_organize_and_cleanup_are_library_ops(self):
        status, out = self.call("/api/automation/library/op", "POST",
                                body={"op": "organize", "paths": ["D:/m/a.safetensors"]})
        self.assertEqual(status, 200, out)
        self.assertEqual(self.api.mm_calls[-1], ("mm_organize", ["D:/m/a.safetensors"]))
        status, out = self.call("/api/automation/library/op", "POST",
                                body={"op": "cleanup", "paths": ["D:/m/a.safetensors"]})
        self.assertEqual(status, 400)
        self.assertIn("confirm", out["msg"])



class FsAndMigrateTest(unittest.TestCase):
    """目录浏览（AI 选落点）与数据目录迁移的门禁"""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.TemporaryDirectory()
        self.api = FakeApi(__import__("pathlib").Path(self.tmpdir.name))

    def call(self, path, method="GET", query=None, body=None):
        return automation_api.dispatch(path, method, query or {}, body, self.api, ctx(self.api))

    def test_fs_lists_drives_without_path(self):
        status, body = self.call("/api/automation/fs")
        self.assertEqual(status, 200)
        self.assertTrue(body["entries"])
        self.assertTrue(all(e["dir"] for e in body["entries"]))

    def test_fs_lists_one_level_dirs_only_by_default(self):
        os.makedirs(os.path.join(self.tmpdir.name, "sub"), exist_ok=True)
        with open(os.path.join(self.tmpdir.name, "a.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        status, body = self.call("/api/automation/fs", query={"path": [self.tmpdir.name]})
        self.assertEqual(status, 200)
        names = [e["name"] for e in body["entries"]]
        self.assertIn("sub", names)
        self.assertNotIn("a.txt", names)
        self.assertEqual(os.path.normcase(body["parent"]),
                         os.path.normcase(os.path.dirname(self.tmpdir.name)))
        status, body = self.call("/api/automation/fs",
                                 query={"path": [self.tmpdir.name], "dirs_only": ["0"]})
        self.assertIn("a.txt", [e["name"] for e in body["entries"]])

    def test_fs_missing_dir_is_404_with_hint(self):
        status, body = self.call("/api/automation/fs", query={"path": ["Z:/definitely/not/here"]})
        self.assertEqual(status, 404)
        self.assertIn("hint", body)

    def test_migrate_requires_confirm(self):
        status, body = self.call("/api/automation/storage/migrate", "POST",
                                 body={"directory": self.tmpdir.name})
        self.assertEqual(status, 400)
        self.assertIn("confirm", body["msg"])
        self.assertIn("hint", body)
        status, body = self.call("/api/automation/storage/migrate", "POST", body={})
        self.assertEqual(status, 400)

    def test_migrate_delegates_when_confirmed(self):
        self.api.migrate_data_storage = lambda d, replace=False: {"ok": True, "directory": d}
        status, body = self.call("/api/automation/storage/migrate", "POST",
                                 body={"directory": self.tmpdir.name, "confirm": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["directory"], self.tmpdir.name)


if __name__ == "__main__":
    unittest.main()
