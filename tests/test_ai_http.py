# -*- coding: utf-8 -*-
"""AI HTTP 投影：/ai /tools /tool /guide、mode=text、错误 code。全部离线。"""
import base64
import json
import unittest
from pathlib import Path

import automation_api


def _png_b64():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (20, 40, 60)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


class FakeCiv:
    def _get(self, path, params=None, retries=1):
        return {"items": [{"id": 1, "name": "Demo", "type": "LORA", "nsfw": False,
                           "stats": {}, "modelVersions": [{"id": 2, "name": "v1",
                                                           "baseModel": "PonyXL"}]}],
                "metadata": {"totalItems": 1}}


class FakeApi:
    def __init__(self, tmp):
        self.cfg = {"comfyui_models_root": str(tmp / "models"), "route_by_type": True,
                    "site_domain": "civitai.red", "download_dir": str(tmp),
                    "api_key": "secret"}
        self.api = FakeCiv()
        self.tasks = []
        self.lib_calls = []

    def get_tasks(self):
        return list(self.tasks)

    def get_download_file_choice(self):
        return None

    def get_scan_rows(self):
        return json.dumps([{"path": "D:/m/a.safetensors", "name": "a", "type": "LORA",
                            "type_filter": "loras", "base": "PonyXL", "size": 1,
                            "ver": "v1", "verId": "1", "modelId": "2"}])

    def get_scan_state(self):
        return {"running": False, "msg": "", "total": 1, "roots": []}

    def get_data_storage_info(self):
        return {"current": "D:/data"}

    def error_log_path(self):
        return ""

    def get_model_detail(self, path):
        return {"ok": True, "info": {"name": "Demo", "type": "LORA", "baseModel": "PonyXL",
                                     "trainedWords": ["demo"]},
                "covers": [{"b64": _png_b64(), "local": True,
                            "local_path": getattr(self, "cover_path", path),
                            "prompt": "a cat", "negative": "bad"}]}

    def lib_list(self, flt_json):
        flt = json.loads(flt_json)
        self.lib_calls.append(("lib_list", flt))
        return json.dumps({"ok": True, "items": [{"id": 1, "filename": "a.png",
                                                  "kind": "image", "prompt": "a cat"}],
                           "total": 1, "offset": 0, "limit": 50, "facets": {}})

    def lib_detail(self, item_id):
        self.lib_calls.append(("lib_detail", item_id))
        return json.dumps({"ok": True, "id": item_id, "filename": "a.png", "kind": "image",
                           "width": 512, "height": 512, "prompt": "a cat",
                           "preview": "data:image/jpeg;base64," + _png_b64()})

    def lib_set_star_batch(self, ids_json, on):
        self.lib_calls.append(("lib_set_star_batch", json.loads(ids_json), on))
        return {"ok": True, "count": len(json.loads(ids_json))}

    def lib_tags(self):
        return json.dumps([])

    def dismiss_ui(self, targets=None, permanent=True):
        self.dismissed = {"targets": targets, "permanent": permanent}
        return {"ok": True, "dismissed": list(targets or []), "permanent": permanent,
                "onboarding_done": True, "auto_guides": False, "ui_dismiss_rev": 1}


def ctx(api):
    return {
        "version": "2.7.0",
        "state": lambda: {"ok": True, "version": "2.7.0",
                          "config": {"download_dir": api.cfg["download_dir"],
                                     "route_by_type": True},
                          "pending_file_choice": None,
                          "tasks": [{"id": "t%d" % i, "status": "done"} for i in range(8)],
                          "ui": {"mode": "browser"}},
        "extension": lambda: {"ok": True},
        "config_keys": ["route_by_type", "download_dir"],
        "data_dir": "D:/data", "error_log": "",
        "save_config": lambda body: (200, {"ok": True}),
    }


class AiHttpTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.api = FakeApi(Path(self.tmp.name))
        self.ctx = ctx(self.api)
        self.model_path = Path(self.tmp.name) / "a.safetensors"
        self.model_path.write_bytes(b"weights")
        self.cover_path = Path(self.tmp.name) / "a.preview.png"
        self.cover_path.write_bytes(base64.b64decode(_png_b64()))
        self.api.cover_path = str(self.cover_path)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, path, method="GET", query=None, body=None):
        return automation_api.dispatch(path, method, query or {}, body, self.api, self.ctx)

    def test_ai_capabilities(self):
        st, body = self.call("/api/automation/ai")
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["version"], "2.7.0")
        self.assertEqual(body["toolsets"]["core"]["count"], 19)
        self.assertIn("limits", body)
        self.assertIn("guide", body)

    def test_tools_projection(self):
        st, body = self.call("/api/automation/tools", query={"format": ["openai"], "profile": ["text"]})
        self.assertEqual(st, 200)
        self.assertEqual(body["count"], 19)
        self.assertEqual(body["tools"][0]["type"], "function")
        self.assertIn("name", body["tools"][0]["function"])
        st, body = self.call("/api/automation/tools", query={"format": ["anthropic"], "profile": ["vision"]})
        self.assertEqual(st, 200)
        self.assertEqual(body["count"], 24)
        self.assertIn("input_schema", body["tools"][0])

    def test_tools_rejects_bad_profile(self):
        st, body = self.call("/api/automation/tools", query={"profile": ["nope"]})
        self.assertEqual(st, 400)
        self.assertEqual(body["code"], "invalid_argument")
        self.assertTrue(body["hint"])

    def test_guide_has_vision_tools(self):
        st, body = self.call("/api/automation/guide", query={"profile": ["vision"]})
        self.assertEqual(st, 200)
        self.assertIn("cft_view_gallery", body["guide"])
        st, body = self.call("/api/automation/guide",
                             query={"profile": ["text"], "format": ["markdown"]})
        self.assertEqual(st, 200)
        self.assertIn("text model guide", body["markdown"])

    def test_tool_capabilities_and_unknown(self):
        st, body = self.call("/api/automation/tool", "POST",
                             body={"name": "cft_capabilities", "arguments": {}})
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["tool"], "cft_capabilities")
        self.assertTrue(body["content"])
        st, body = self.call("/api/automation/tool", "POST",
                             body={"name": "cft_nope", "arguments": {}})
        self.assertEqual(st, 404)
        self.assertEqual(body["code"], "not_found")

    def test_get_state_compact(self):
        st, body = self.call("/api/automation/tool", "POST",
                             body={"name": "cft_get_state",
                                   "arguments": {"compact": True, "task_limit": 2}})
        self.assertEqual(st, 200)
        self.assertEqual(len(body["data"]["tasks"]), 2)
        self.assertIn("blockers", body["data"])
        self.assertEqual(body["data"]["config"].get("route_by_type"), True)

    def test_state_compact_endpoint(self):
        st, body = self.call("/api/automation/state",
                             query={"compact": ["1"], "task_limit": ["3"]})
        self.assertEqual(st, 200)
        self.assertEqual(len(body["tasks"]), 3)
        self.assertNotIn("api_key", body.get("config") or {})

    def test_list_models_and_gallery_filter(self):
        st, body = self.call("/api/automation/tool", "POST",
                             body={"name": "cft_list_models",
                                   "arguments": {"limit": 5, "fields": ["path", "name"]}})
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["rows"][0]["name"], "a")
        st, body = self.call("/api/automation/tool", "POST",
                             body={"name": "cft_browse_gallery",
                                   "arguments": {"kind": "image", "starred": True, "limit": 10}})
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["items"][0]["id"], 1)
        flt = [c for c in self.api.lib_calls if c[0] == "lib_list"][-1][1]
        self.assertEqual(flt["kind"], "image")
        self.assertEqual(flt["starred"], True)

    def test_annotate_gallery(self):
        st, body = self.call("/api/automation/tool", "POST",
                             body={"name": "cft_annotate_gallery",
                                   "arguments": {"op": "star", "ids": [1, 2], "on": True}})
        self.assertEqual(st, 200)
        self.assertEqual(self.api.lib_calls[-1], ("lib_set_star_batch", [1, 2], True))

    def test_describe_model_is_text_only(self):
        st, body = self.call("/api/automation/tool", "POST",
                             body={"name": "cft_describe_model",
                                   "arguments": {"path": str(self.model_path)}})
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["model"], "Demo")
        self.assertTrue(body["content"][0]["text"].startswith("模型:"))
        self.assertNotIn("b64", json.dumps(body["data"]))

    def test_model_and_gallery_mode_text_strip_media(self):
        st, body = self.call("/api/automation/model",
                             query={"path": [str(self.model_path)], "mode": ["text"]})
        self.assertEqual(st, 200)
        self.assertGreaterEqual(body["media_omitted"], 1)
        self.assertNotIn("b64", json.dumps(body["detail"]))
        st, body = self.call("/api/automation/gallery/item",
                             query={"id": ["1"], "mode": ["text"]})
        self.assertEqual(st, 200)
        self.assertGreaterEqual(body["media_omitted"], 1)
        self.assertFalse(body["item"].get("preview"))

    def test_vision_model_and_gallery_content(self):
        st, body = self.call("/api/automation/tool", "POST",
                             body={"name": "cft_view_model",
                                   "arguments": {"path": str(self.model_path), "limit_images": 1}})
        self.assertEqual(st, 200)
        self.assertTrue(any(c.get("type") == "image" for c in body["content"]))
        self.assertNotIn("b64", json.dumps(body["data"]))
        st, body = self.call("/api/automation/vision/gallery",
                             query={"ids": ["1"], "content_format": ["openai"]})
        self.assertEqual(st, 200)
        self.assertTrue(any(c.get("type") == "image_url" for c in body["content"]))
        self.assertNotIn("b64", json.dumps(body["data"]))
        st, body = self.call("/api/automation/vision/model",
                             query={"path": [str(self.model_path)], "delivery": ["paths"]})
        self.assertEqual(st, 200)
        self.assertFalse(any(c.get("type") == "image" for c in body["content"]))
        self.assertTrue(body["data"]["items"][0].get("image_path") or
                        body["data"]["items"][0].get("path"))

    def test_vision_contact_sheet_and_inspect(self):
        st, body = self.call("/api/automation/vision/contact-sheet",
                             query={"source": ["gallery"], "ids": ["1"]})
        self.assertEqual(st, 200)
        self.assertTrue(body.get("data", {}).get("mapping"))
        self.assertTrue(any(c.get("type") == "image" for c in body["content"]))
        st, body = self.call("/api/automation/vision/analyze", "POST",
                             body={"data_b64": _png_b64(), "filename": "x.png"})
        self.assertEqual(st, 200)
        self.assertEqual(body["data"]["format"], "PNG")
        self.assertTrue(any(c.get("type") == "image" for c in body["content"]))

    def test_ui_dismiss_endpoint(self):
        st, body = self.call("/api/automation/ui/dismiss", "POST",
                             body={"targets": ["onboarding"], "permanent": True})
        self.assertEqual(st, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(self.api.dismissed, {"targets": ["onboarding"], "permanent": True})

    def test_ui_dismiss_defaults_to_all(self):
        st, body = self.call("/api/automation/ui/dismiss", "POST", body={})
        self.assertEqual(st, 200)
        self.assertEqual(self.api.dismissed, {"targets": ["all"], "permanent": True})
        self.assertEqual(body["dismissed"], ["all"])

    def test_ui_dismiss_rejects_bad_target(self):
        st, body = self.call("/api/automation/ui/dismiss", "POST",
                             body={"targets": ["model_detail"]})
        self.assertEqual(st, 400)
        self.assertIn("onboarding", body["msg"])
        self.assertIn("permanent", body["hint"])

    def test_verify_missing_args_has_code(self):
        st, body = self.call("/api/automation/verify")
        self.assertEqual(st, 400)
        self.assertEqual(body["code"], "invalid_argument")
        self.assertFalse(body["retryable"])
        self.assertTrue(body["hint"])

    def test_error_codes_are_added_to_existing_errors(self):
        st, body = self.call("/api/automation/fs", query={"path": ["Z:/definitely/not/here"]})
        self.assertEqual(st, 404)
        self.assertEqual(body["code"], "not_found")
        self.assertFalse(body["retryable"])


if __name__ == "__main__":
    unittest.main()
