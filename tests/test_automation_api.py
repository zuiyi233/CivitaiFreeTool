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
        self.repairs = []

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
                           "postprocess": {"done": True}, "info": {}})
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

    def repair_download_postprocess(self, task_id, stages=None):
        self.repairs.append((task_id, stages))
        for t in self.tasks:
            if t.get("id") == task_id:
                t["postprocess_repair"] = {"stages": stages or ["metadata"], "done": True}
                t["postprocess_errors"] = []
        return {"ok": True, "started": True, "task_id": task_id,
                "stages": stages or ["metadata"]}

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
            out = automation_api._verify_dir(d, "demo.safetensors",
                                            expect=[".civitai.info", ".json", "_评论.txt"])
            self.assertTrue(out["ok"], out)
            self.assertTrue(out["bundle_ok"], out)
            self.assertTrue(out["exists"])
            self.assertEqual(out["images"]["count"], 1)
            self.assertEqual(out["sidecars"]["missing"], [])
            self.assertEqual(out["sidecars"]["expected_missing"], [])

    def test_missing_sidecar_keeps_file_ok_but_fails_bundle(self):
        """ok 只对模型文件负责：配置要求写的附属文件缺失走 bundle_ok。

        分开的理由：没配 API Key 时评论文件本就不会写，算进 ok 会让每次 verify 假失败。
        """
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            make_tree(d, with_sidecars=False)
            out = automation_api._verify_dir(d, "demo.safetensors", expect=[".civitai.info"])
            self.assertTrue(out["ok"], out)
            self.assertFalse(out["bundle_ok"])
            self.assertIn("demo.civitai.info", out["sidecars"]["missing"])
            self.assertEqual(out["sidecars"]["expected_missing"], ["demo.civitai.info"])

    def test_no_expectation_means_bundle_ok(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            make_tree(d, with_sidecars=False)
            out = automation_api._verify_dir(d, "demo.safetensors")
            self.assertTrue(out["ok"])
            self.assertTrue(out["bundle_ok"])
            self.assertIn("demo.civitai.info", out["sidecars"]["missing"])

    def test_video_cover_satisfies_preview(self):
        """封面是视频的模型没有 .preview.png：<stem>.mp4 必须能顶上，否则误报缺失。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = make_tree(d)
            os.remove(os.path.join(d, "demo.preview.png"))
            out = automation_api._verify_dir(d, "demo.safetensors")
            self.assertEqual(out["preview"], "")
            self.assertIn("demo.preview.png|.mp4", out["sidecars"]["missing"])
            open(os.path.join(d, "demo.mp4"), "wb").write(b"v")
            out = automation_api._verify_dir(d, "demo.safetensors")
            self.assertEqual(out["preview"], "demo.mp4")
            self.assertNotIn("demo.preview.png|.mp4", out["sidecars"]["missing"])
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
        self.assertEqual(f["download_url"], "https://civitai.red/api/download/models/123?fileId=1",
                         "plan 必须把可用的下载 URL 交给 AI，不能只留空串")
        self.assertNotIn("download_url_error", f)
        self.assertEqual(self.api.submitted, [], "plan 绝不能入队")
        self.assertFalse(os.path.isdir(f["routed_dir"]), "plan 绝不能建目录")

    def test_plan_without_civ_reports_reason_not_name_error(self):
        self.api.api = None
        status, body = automation_api.dispatch("/api/automation/plan", "POST", {},
                                              {"url": "https://civitai.red/models/456"}, self.api, ctx(self.api))
        self.assertEqual(status, 500, body)
        self.assertIn("API 未就绪", body["msg"])
        plan = automation_api._plan_payload(self.api, {}, 456, 123,
                                            {"id": 123, "name": "v1", "baseModel": "PonyXL"},
                                            {"id": 456, "name": "Demo", "type": "LORA"},
                                            [{"id": 1, "name": "demo.safetensors", "type": "Model",
                                              "primary": True, "sizeKB": 1000}])
        f = plan["files"][0]
        self.assertEqual(f["download_url"], "")
        self.assertNotIn("not defined", f.get("download_url_error", ""),
                         "缺客户端要如实说明，不能把 NameError 当成错误信息")

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

    def test_download_skips_when_target_exists(self):
        """已下载过的模型不该被再传一遍（实测 284MB 白下 + 覆盖同名文件）。"""
        dest = os.path.join(self.api.cfg["comfyui_models_root"], "loras", "demo")
        make_tree(dest)
        status, body = automation_api.dispatch("/api/automation/download", "POST", {},
                                               {"url": "https://civitai.red/models/456",
                                                "wait": True, "timeout": 5}, self.api, ctx(self.api))
        self.assertEqual(status, 200, body)
        self.assertEqual(body.get("skipped"), "exists", body)
        self.assertIn("force", body.get("hint") or "")
        self.assertEqual(self.api.submitted, [], "已存在时绝不能入队")

    def test_download_force_redownloads_existing_target(self):
        dest = os.path.join(self.api.cfg["comfyui_models_root"], "loras", "demo")
        make_tree(dest)
        status, body = automation_api.dispatch("/api/automation/download", "POST", {},
                                               {"url": "https://civitai.red/models/456",
                                                "wait": True, "timeout": 5, "force": True},
                                               self.api, ctx(self.api))
        self.assertEqual(status, 200, body)
        self.assertNotEqual(body.get("skipped"), "exists")
        self.assertEqual(len(self.api.submitted), 1)

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

    def test_verify_passes_through_download_hash_verdict(self):
        d = os.path.join(self.api.cfg["comfyui_models_root"], "loras", "demo")
        make_tree(d)
        self.api.tasks.append({"id": "t-hash", "filename": "demo.safetensors", "status": "done",
                               "dest_dir": d, "routed_dir": d,
                               "expected_sha256": "AA" * 32, "actual_sha256": "aa" * 32,
                               "verification_status": "verified"})
        status, body = automation_api.dispatch("/api/automation/verify", "GET",
                                               {"task_id": ["t-hash"]}, None, self.api, ctx(self.api))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["verification_status"], "verified")
        self.assertEqual(body["expected_sha256"], "AA" * 32)
        self.assertTrue(body["sha256_ok"], "下载时已 verified 的，不能只给 AI 一个 null")

    def test_verify_reports_unchecked_hash_as_unchecked(self):
        d = os.path.join(self.api.cfg["comfyui_models_root"], "loras", "demo")
        make_tree(d)
        self.api.tasks.append({"id": "t-none", "filename": "demo.safetensors", "status": "done",
                               "dest_dir": d, "routed_dir": d,
                               "expected_sha256": "", "actual_sha256": "",
                               "verification_status": "unverified"})
        status, body = automation_api.dispatch("/api/automation/verify", "GET",
                                               {"task_id": ["t-none"]}, None, self.api, ctx(self.api))
        self.assertEqual(status, 200, body)
        self.assertIsNone(body["sha256_ok"], "没校验过就如实报 null，不许替用户宣布通过")

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



class RepairAndCacheTest(unittest.TestCase):
    """收尾重跑入口 + plan/resolve 的请求复用（30s 缓存）"""

    def setUp(self):
        import tempfile
        automation_api._API_CACHE.clear()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.api = FakeApi(__import__("pathlib").Path(self.tmpdir.name))
        self.api.tasks.append({"id": "t1", "filename": "demo.safetensors", "status": "done",
                               "dest_dir": "D:/m", "routed_dir": "D:/m/loras/demo",
                               "progress": 100.0, "downloaded": 7, "total": 7,
                               "error": "", "info": {}, "postprocess_errors": []})

    def tearDown(self):
        automation_api._API_CACHE.clear()

    def call(self, path, method="GET", query=None, body=None):
        return automation_api.dispatch(path, method, query or {}, body, self.api, ctx(self.api))

    def test_repair_waits_and_reports_errors(self):
        status, out = self.call("/api/automation/tasks/repair", "POST",
                                body={"task_id": "t1", "stages": ["metadata"], "timeout": 3})
        self.assertEqual(status, 200, out)
        self.assertTrue(out["finished"], out)
        self.assertFalse(out["timed_out"])
        self.assertEqual(out["postprocess_errors"], [])
        self.assertEqual(self.api.repairs[-1], ("t1", ["metadata"]))

    def test_repair_requires_task_id_and_list_stages(self):
        status, out = self.call("/api/automation/tasks/repair", "POST", body={})
        self.assertEqual(status, 400)
        self.assertIn("hint", out)
        status, out = self.call("/api/automation/tasks/repair", "POST",
                                body={"task_id": "t1", "stages": "metadata"})
        self.assertEqual(status, 400)
        self.assertIn("metadata", out["hint"])

    def test_repair_times_out_without_finishing(self):
        status, out = self.call("/api/automation/tasks/repair", "POST",
                                body={"task_id": "ghost", "timeout": 1})
        self.assertEqual(status, 200, out)
        self.assertFalse(out["finished"])
        self.assertTrue(out["timed_out"])
        self.assertIn("hint", out)

    def test_repair_without_wait_returns_202(self):
        status, out = self.call("/api/automation/tasks/repair", "POST",
                                body={"task_id": "t1", "wait": False})
        self.assertEqual(status, 202, out)
        self.assertTrue(out["started"])

    def test_plan_then_resolve_fetch_each_resource_once(self):
        civ = self.api.api
        calls = {"model": 0, "version": 0}
        real_m, real_v = civ.get_model, civ.get_model_version

        def counted_m(mid):
            calls["model"] += 1
            return real_m(mid)

        def counted_v(vid):
            calls["version"] += 1
            return real_v(vid)

        civ.get_model, civ.get_model_version = counted_m, counted_v
        self.call("/api/automation/plan", "POST", body={"url": "https://civitai.red/models/456"})
        self.call("/api/automation/resolve", "POST", body={"url": "https://civitai.red/models/456"})
        self.assertEqual(calls, {"model": 1, "version": 1}, calls)

    def test_plan_uses_embedded_model_without_second_request(self):
        """`/v1/model-versions/<id>` 自带 model{name,type}：plan 不该再拉一次模型详情。"""
        civ = self.api.api
        real_v = civ.get_model_version
        calls = {"model": 0}

        def version_with_model(vid):
            v = dict(real_v(vid))
            v["model"] = {"id": 456, "name": "Demo", "type": "LORA"}
            return v

        def counted_model(mid):
            calls["model"] += 1
            return {"id": 456, "name": "Demo", "type": "LORA", "modelVersions": [{"id": 123}]}

        civ.get_model_version, civ.get_model = version_with_model, counted_model
        status, out = self.call("/api/automation/plan", "POST",
                                body={"url": "https://civitai.red/models/456"})
        self.assertEqual(status, 200, out)
        self.assertEqual(calls["model"], 0, "plan 不该为命名/分流再请求一次模型详情")
        self.assertEqual(out["model"]["name"], "Demo")
        self.assertEqual(out["model"]["type"], "LORA")

    def test_cache_expires(self):
        civ = self.api.api
        calls = {"n": 0}
        real = civ.get_model_version
        civ.get_model_version = lambda vid: (calls.__setitem__("n", calls["n"] + 1), real(vid))[1]
        self.call("/api/automation/plan", "POST", body={"url": "https://civitai.red/models/456"})
        automation_api._API_CACHE.clear()          # 模拟 TTL 过期
        self.call("/api/automation/plan", "POST", body={"url": "https://civitai.red/models/456"})
        self.assertEqual(calls["n"], 2)


class PostprocessWaitTest(unittest.TestCase):
    """status=done 之后收尾还在写 sidecar：download(wait=1,verify=1) 必须等它。

    实测事故：9.1MB 模型下载成功，但 verify 在收尾线程写完 sidecar 前返回 ok=false，
    AI 只能重试或人工重跑——这是调用层卡点，不是下载失败。
    """

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.TemporaryDirectory()
        self.api = FakeApi(__import__("pathlib").Path(self.tmpdir.name))

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_waits_until_postprocess_done(self):
        import threading
        import time
        task = {"id": "t1", "status": "done", "postprocess": {"done": False}}
        self.api.tasks.append(task)

        def finish():
            time.sleep(0.4)
            task["postprocess"]["done"] = True

        threading.Thread(target=finish, daemon=True).start()
        t0 = time.time()
        extra = automation_api._wait_postprocess(self.api, [task], timeout=10)
        self.assertTrue(task["postprocess"]["done"])
        self.assertLess(time.time() - t0, 5)
        self.assertGreaterEqual(extra, 0.3, extra)

    def test_task_without_marker_does_not_burn_full_timeout(self):
        import time
        legacy = {"id": "old", "status": "done"}
        self.api.tasks.append(legacy)
        t0 = time.time()
        automation_api._wait_postprocess(self.api, [legacy], timeout=30)
        self.assertLess(time.time() - t0, 6, "没有 postprocess 标记的历史任务不该等满 30s")

    def test_failed_task_is_not_waited(self):
        import time
        bad = {"id": "t2", "status": "error", "postprocess": {"done": False}}
        self.api.tasks.append(bad)
        t0 = time.time()
        automation_api._wait_postprocess(self.api, [bad], timeout=30)
        self.assertLess(time.time() - t0, 1)

    def test_expect_sidecars_follows_config(self):
        cfg = self.api.cfg
        self.assertIn("_评论.txt", automation_api._expect_sidecars(self.api))
        cfg["api_key"] = ""                        # 没 Key 就不会写评论文件
        self.assertNotIn("_评论.txt", automation_api._expect_sidecars(self.api))
        cfg["metadata_format"] = "civitai"         # 这种格式不写 .json
        self.assertEqual(automation_api._expect_sidecars(self.api), [".civitai.info"])
        cfg["gen_metadata"] = False
        self.assertEqual(automation_api._expect_sidecars(self.api), [])


class GalleryFakeApi(FakeApi):
    """资源库（ComfyUI output 媒体库）桩：lib_* 全部离线，且记录收到的参数。"""

    def __init__(self, tmp):
        super().__init__(tmp)
        self.lib_calls = []
        self.gallery_items = [
            {"id": 1, "filename": "a.png", "kind": "image", "starred": False, "tags": []},
            {"id": 2, "filename": "b.mp4", "kind": "video", "starred": True,
             "tags": [{"id": 3, "name": "精选"}]},
            {"id": 3, "filename": "c.png", "kind": "image", "starred": False, "tags": []},
        ]
        self.gallery_facets = {"kinds": {"image": 2, "video": 1}, "starred": 1, "total": 3}
        self.scan_states = []
        self.scan_result = {"started": True, "msg": ""}
        self.deletable = {1, 2}

    def lib_list(self, flt_json):
        flt = json.loads(flt_json)
        self.lib_calls.append(("lib_list", flt))
        offset = int(flt.get("offset") or 0)
        limit = int(flt.get("limit") or 50)
        return json.dumps({"ok": True, "items": self.gallery_items[offset:offset + limit],
                           "total": len(self.gallery_items), "offset": offset, "limit": limit,
                           "facets": self.gallery_facets}, ensure_ascii=False)

    def lib_detail(self, item_id):
        self.lib_calls.append(("lib_detail", item_id))
        row = next((r for r in self.gallery_items if r["id"] == item_id), None)
        if row is None:
            return json.dumps({"ok": False, "msg": "条目不存在"}, ensure_ascii=False)
        return json.dumps(dict({"ok": True, "width": 1024, "height": 1024}, **row),
                          ensure_ascii=False)

    def lib_tags(self):
        self.lib_calls.append(("lib_tags",))
        return json.dumps([{"id": 3, "name": "精选", "color": "#888",
                            "parent_id": None, "count": 2}], ensure_ascii=False)

    def lib_set_star_batch(self, ids_json, on):
        ids = json.loads(ids_json)
        self.lib_calls.append(("lib_set_star_batch", ids, on))
        return {"ok": True, "count": len(ids)}

    def lib_tag_add_batch(self, ids_json, tag_ids_json):
        ids, tag_ids = json.loads(ids_json), json.loads(tag_ids_json)
        self.lib_calls.append(("lib_tag_add_batch", ids, tag_ids))
        return {"ok": True, "count": len(ids), "tags": {str(i): tag_ids for i in ids}}

    def lib_tag_remove_batch(self, ids_json, tag_ids_json):
        ids, tag_ids = json.loads(ids_json), json.loads(tag_ids_json)
        self.lib_calls.append(("lib_tag_remove_batch", ids, tag_ids))
        return {"ok": True, "count": len(ids), "tags": {}}

    def lib_tag_save(self, tag_id, name, color, parent_id=-1):
        self.lib_calls.append(("lib_tag_save", tag_id, name, color, parent_id))
        if not name:
            return {"ok": False, "id": 0, "msg": "标签名不能为空"}
        return {"ok": True, "id": 7, "msg": ""}

    def lib_tag_delete(self, tag_id):
        self.lib_calls.append(("lib_tag_delete", tag_id))
        return {"ok": True, "removed": 1}

    def lib_delete(self, item_id):
        self.lib_calls.append(("lib_delete", item_id))
        if item_id in self.deletable:
            return {"ok": True, "msg": "已移入回收站"}
        return {"ok": False, "msg": "文件不存在"}

    def lib_scan(self):
        self.lib_calls.append(("lib_scan",))
        return dict(self.scan_result)

    def lib_scan_state(self):
        self.lib_calls.append(("lib_scan_state",))
        if self.scan_states:
            return self.scan_states.pop(0)
        return {"running": False, "phase": "done", "done": 1, "total": 1,
                "msg": "扫描完成", "roots": [], "errors": []}


class GalleryApiTest(unittest.TestCase):
    """资源库 6 个端点：列表/详情/标签/写操作/删除/扫描（全部走 dispatch）。"""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.TemporaryDirectory()
        self.api = GalleryFakeApi(__import__("pathlib").Path(self.tmpdir.name))

    def tearDown(self):
        self.tmpdir.cleanup()

    def call(self, path, method="GET", query=None, body=None):
        return automation_api.dispatch(path, method, query or {}, body, self.api, ctx(self.api))

    def test_manifest_lists_all_six_gallery_endpoints(self):
        status, body = self.call("/api/automation")
        self.assertEqual(status, 200)
        routes = {(e["method"], e["path"]) for e in body["endpoints"]}
        for route in (("GET", "/api/automation/gallery"),
                      ("GET", "/api/automation/gallery/item"),
                      ("GET", "/api/automation/gallery/tags"),
                      ("POST", "/api/automation/gallery/op"),
                      ("GET", "/api/automation/gallery/scan"),
                      ("POST", "/api/automation/gallery/scan")):
            self.assertIn(route, routes, route)

    def test_gallery_list_overrides_paging_and_trims_fields(self):
        status, body = self.call("/api/automation/gallery", query={
            "filter": ['{"kind":"image","starred":true,"limit":999,"offset":7}'],
            "limit": ["2"], "offset": ["1"], "with_facets": ["1"],
            "fields": ["id,filename"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["offset"], 1)
        self.assertEqual(body["limit"], 2)
        self.assertEqual([row["id"] for row in body["items"]], [2, 3])
        self.assertEqual(set(body["items"][0]), {"id", "filename"})
        self.assertEqual(body["facets"]["starred"], 1)
        name, flt = self.api.lib_calls[-1]
        self.assertEqual(name, "lib_list")
        self.assertEqual(flt["limit"], 2)
        self.assertEqual(flt["offset"], 1)
        self.assertEqual(flt["with_facets"], 1)
        self.assertEqual(flt["kind"], "image")
        self.assertTrue(flt["starred"])

    def test_gallery_list_facets_none_becomes_empty_dict(self):
        self.api.gallery_facets = None
        status, body = self.call("/api/automation/gallery", query={"with_facets": ["1"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["facets"], {})

    def test_gallery_list_rejects_bad_filter_and_paging(self):
        status, body = self.call("/api/automation/gallery", query={"filter": ["{bad"]})
        self.assertEqual(status, 400)
        self.assertIn("hint", body)
        status, body = self.call("/api/automation/gallery", query={"filter": ["[1,2]"]})
        self.assertEqual(status, 400)
        self.assertIn("JSON 对象", body["msg"])
        for query in ({"limit": ["501"]}, {"limit": ["0"]}, {"offset": ["-1"]},
                      {"limit": ["abc"]}):
            status, body = self.call("/api/automation/gallery", query=query)
            self.assertEqual(status, 400, query)
            self.assertIn("hint", body, query)

    def test_gallery_item_found_and_missing(self):
        status, body = self.call("/api/automation/gallery/item",
                                 query={"id": ["2"], "fields": ["id,item.filename"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["id"], 2)
        self.assertEqual(body["item"]["filename"], "b.mp4")
        self.assertNotIn("kind", body["item"])
        self.assertEqual(self.api.lib_calls[-1], ("lib_detail", 2))

        status, body = self.call("/api/automation/gallery/item", query={"id": ["999"]})
        self.assertEqual(status, 404, body)
        self.assertIn("gallery", body["hint"])
        for bad in ("0", "-2", "abc", ""):
            status, _body = self.call("/api/automation/gallery/item", query={"id": [bad]})
            self.assertEqual(status, 400, bad)

    def test_gallery_tags_shape_and_fields(self):
        status, body = self.call("/api/automation/gallery/tags",
                                 query={"fields": ["id,name"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["count"], 1)
        self.assertEqual(set(body["tags"][0]), {"id", "name"})
        self.assertEqual(body["tags"][0]["name"], "精选")

    def test_gallery_star_dedupes_and_reports_requested(self):
        status, body = self.call("/api/automation/gallery/op", "POST",
                                 body={"op": "star", "ids": [2, 1, 2]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True, "op": "star", "count": 2, "requested": 2})
        self.assertEqual(self.api.lib_calls[-1], ("lib_set_star_batch", [2, 1], True))

        status, body = self.call("/api/automation/gallery/op", "POST",
                                 body={"op": "star", "ids": [1], "on": False})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.api.lib_calls[-1], ("lib_set_star_batch", [1], False))

    def test_gallery_tag_add_and_remove_keep_bottom_shape(self):
        status, body = self.call("/api/automation/gallery/op", "POST",
                                 body={"op": "tag-add", "ids": [1, 2], "tag_ids": [3, 3]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["op"], "tag-add")
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["tags"], {"1": [3], "2": [3]})
        self.assertEqual(self.api.lib_calls[-1], ("lib_tag_add_batch", [1, 2], [3]))

        status, body = self.call("/api/automation/gallery/op", "POST",
                                 body={"op": "tag-remove", "ids": [1], "tag_ids": [3]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["op"], "tag-remove")
        self.assertEqual(self.api.lib_calls[-1], ("lib_tag_remove_batch", [1], [3]))

    def test_gallery_tag_save_and_delete(self):
        status, body = self.call("/api/automation/gallery/op", "POST",
                                 body={"op": "tag-save", "name": "精选", "color": "#888"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["id"], 7)
        self.assertEqual(self.api.lib_calls[-1], ("lib_tag_save", 0, "精选", "#888", -1))

        status, body = self.call("/api/automation/gallery/op", "POST",
                                 body={"op": "tag-save", "name": "   "})
        self.assertEqual(status, 400)
        self.assertIn("hint", body)

        status, body = self.call("/api/automation/gallery/op", "POST",
                                 body={"op": "tag-delete", "id": 3})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["removed"], 1)
        self.assertEqual(self.api.lib_calls[-1], ("lib_tag_delete", 3))

    def test_gallery_op_rejects_unknown_and_bad_ids(self):
        status, body = self.call("/api/automation/gallery/op", "POST", body={"op": "explode"})
        self.assertEqual(status, 400)
        for op in automation_api.GALLERY_OPS:
            self.assertIn(op, body["msg"])
        for bad in ({"op": "star", "ids": "1"}, {"op": "star", "ids": []},
                    {"op": "star", "ids": [0]}, {"op": "star", "ids": ["x"]},
                    {"op": "tag-add", "ids": [1], "tag_ids": []},
                    {"op": "star", "ids": list(range(1, 502))}):
            status, body = self.call("/api/automation/gallery/op", "POST", body=bad)
            self.assertEqual(status, 400, bad)
            self.assertIn("hint", body, bad)

    def test_gallery_delete_requires_confirm_and_aggregates_results(self):
        for body in ({"op": "delete", "ids": [1]},
                     {"op": "delete", "ids": [1], "confirm": False},
                     {"op": "delete", "ids": [1], "confirm": "false"}):
            status, out = self.call("/api/automation/gallery/op", "POST", body=body)
            self.assertEqual(status, 400, body)
            self.assertIn("confirm", out["msg"])
            self.assertIn("sidecar", out["hint"])
        self.assertFalse([c for c in self.api.lib_calls if c[0] == "lib_delete"],
                         "未确认时绝不能调用 lib_delete")

        status, out = self.call("/api/automation/gallery/op", "POST",
                                body={"op": "delete", "ids": [1, 2, 999], "confirm": True})
        self.assertEqual(status, 200, out)
        self.assertFalse(out["ok"])
        self.assertEqual(out["op"], "delete")
        self.assertEqual(out["deleted"], 2)
        self.assertEqual(out["failed"], 1)
        self.assertEqual([r["id"] for r in out["results"]], [1, 2, 999])
        self.assertEqual([r["ok"] for r in out["results"]], [True, True, False])
        self.assertTrue(out["results"][2]["msg"])
        self.assertEqual([c for c in self.api.lib_calls if c[0] == "lib_delete"],
                         [("lib_delete", 1), ("lib_delete", 2), ("lib_delete", 999)])

    def test_gallery_scan_waits_until_running_false(self):
        self.api.scan_states = [
            {"running": True, "phase": "walk", "done": 0, "total": 3,
             "msg": "", "roots": [], "errors": []},
            {"running": False, "phase": "done", "done": 3, "total": 3,
             "msg": "扫描完成", "roots": [], "errors": []},
        ]
        status, body = self.call("/api/automation/gallery/scan", "POST",
                                 body={"wait": True, "timeout": 5})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["started"])
        self.assertFalse(body["timed_out"])
        self.assertEqual(body["state"]["done"], 3)
        self.assertFalse(body["state"]["running"])
        self.assertIn(("lib_scan",), self.api.lib_calls)
        self.assertGreaterEqual(
            len([c for c in self.api.lib_calls if c[0] == "lib_scan_state"]), 2)

    def test_gallery_scan_already_running_still_waits(self):
        self.api.scan_result = {"started": False, "msg": "资源库扫描已在进行"}
        self.api.scan_states = [
            {"running": True, "phase": "parse", "done": 1, "total": 2,
             "msg": "", "roots": [], "errors": []},
            {"running": False, "phase": "done", "done": 2, "total": 2,
             "msg": "扫描完成", "roots": [], "errors": []},
        ]
        status, body = self.call("/api/automation/gallery/scan", "POST",
                                 body={"wait": 1, "timeout": 5})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["started"])
        self.assertEqual(body["msg"], "资源库扫描已在进行")
        self.assertFalse(body["state"]["running"])
        self.assertFalse(body["timed_out"])

    def test_gallery_scan_state_endpoint(self):
        self.api.scan_states = [{"running": True, "phase": "walk", "done": 0, "total": 9,
                                 "msg": "", "roots": [], "errors": []}]
        status, body = self.call("/api/automation/gallery/scan")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["state"]["running"])
        self.assertEqual(body["state"]["total"], 9)

    def test_gallery_scan_without_wait_returns_current_state(self):
        status, body = self.call("/api/automation/gallery/scan", "POST",
                                 body={"wait": False, "timeout": 0})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["started"])
        self.assertFalse(body["timed_out"])
        self.assertIn("state", body)
        self.assertEqual(self.api.lib_calls[0], ("lib_scan",))


class PlacementTest(unittest.TestCase):
    """Read-only placement verdict + ComfyUI root suggestions. Never moves files."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "models"
        for name in ("loras", "checkpoints", "vae"):
            (self.root / name).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_routed_under_models_root(self):
        dest = str(self.root / "loras" / "demo")
        out = automation_api._placement(dest, {"comfyui_models_root": str(self.root)})
        self.assertEqual(out["status"], "routed")
        self.assertEqual(out["folder"], "loras")
        self.assertTrue(out["under_models_root"])
        self.assertEqual(out["hint"], "")

    def test_file_at_root_needs_routing(self):
        out = automation_api._placement(str(self.root), {"comfyui_models_root": str(self.root)})
        self.assertEqual(out["status"], "under_root")
        self.assertIn("route_by_type", out["hint"])

    def test_outside_configured_roots(self):
        other = str(self.root.parent / "elsewhere")
        out = automation_api._placement(other, {"comfyui_models_root": str(self.root)})
        self.assertEqual(out["status"], "outside_root")
        self.assertFalse(out["under_configured_root"])
        self.assertIn("comfyui_models_root", out["hint"])

    def test_missing_root_explains_why(self):
        out = automation_api._placement(str(self.root / "loras"), {})
        self.assertEqual(out["status"], "outside_root")
        self.assertIn("未配置 comfyui_models_root", out["hint"])

    def test_pending_when_not_resolved(self):
        out = automation_api._placement("", {})
        self.assertEqual(out["status"], "pending")
        self.assertTrue(out["hint"])

    def test_candidates_found_from_parent(self):
        incoming = self.root / "incoming"
        incoming.mkdir()
        found = automation_api._model_root_candidates({"download_dir": str(incoming)})
        self.assertEqual(found["suggested"], str(self.root))
        self.assertTrue(any("上级" in row["reason"] for row in found["candidates"]))

    def test_single_type_dir_is_not_a_candidate(self):
        lonely = self.root.parent / "lonely"
        (lonely / "loras").mkdir(parents=True)
        found = automation_api._model_root_candidates({"download_dir": str(lonely)})
        self.assertEqual(found["candidates"], [])

    def test_plan_checks_default_destination_when_routing_off(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            api = FakeApi(Path(tmp))
            api.cfg.update({"route_by_type": False, "comfyui_models_root": "",
                            "download_dir": str(self.root), "download_target_dir": ""})
            (self.root / "demo.safetensors").write_bytes(b"weights")
            status, body = automation_api._plan(api, None, {"url": "https://civitai.red/models/456"})
            self.assertEqual(status, 200, body)
            item = body["files"][0]
            self.assertTrue(item["exists"], "非分流时也要能在默认目录认出已下载")
            self.assertEqual(item["existing_path"], str(self.root / "demo.safetensors"))
            self.assertEqual(item["placement"]["status"], "under_root")
            self.assertIn("route_by_type", item["placement"]["hint"])


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
