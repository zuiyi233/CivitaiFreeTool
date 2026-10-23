# -*- coding: utf-8 -*-
"""OpenAPI 3.1 投影与每工具执行路径：全部离线，不启动桥、不碰磁盘。"""
import json
import unittest

import ai_contract
import automation_api
import openapi_projection


def make_ctx(host="127.0.0.1:47531"):
    return {
        "version": "2.7.0",
        "host": host,
        "state": lambda: {"ok": True, "version": "2.7.0", "config": {},
                          "pending_file_choice": None, "tasks": [], "ui": {}},
        "extension": lambda: {"ok": True},
        "config_keys": ["route_by_type"],
        "rpc_methods": [],
        "data_dir": "D:/data",
        "error_log": "",
    }


class OpenApiProjectionTest(unittest.TestCase):
    def call(self, path, method="GET", query=None, body=None, ctx=None, api=None):
        return automation_api.dispatch(path, method, query or {}, body,
                                       api if api is not None else object(),
                                       ctx or make_ctx())

    def test_document_counts_and_schema_identity(self):
        expected = {"text": 19, "vision": 24, "admin": 24, "all": 29}
        for profile, count in expected.items():
            st, doc = self.call("/api/automation/openapi.json", query={"profile": [profile]})
            self.assertEqual(st, 200, doc)
            self.assertEqual(doc["openapi"], "3.1.0")
            self.assertEqual(len(doc["paths"]), count)
            self.assertEqual(doc["x-cft-profile"], profile)
            for tool in ai_contract.tools_for_profile(profile):
                path = "/api/automation/tool/" + tool["name"]
                operation = doc["paths"][path]["post"]
                self.assertEqual(operation["operationId"], tool["name"])
                self.assertEqual(operation["requestBody"]["content"]["application/json"]["schema"],
                                 tool["inputSchema"])
                self.assertEqual(operation["x-cft-toolset"], tool["toolset"])
                self.assertEqual(operation["x-cft-annotations"], tool["annotations"])

    def test_transport_discovery_is_contract_derived(self):
        _, doc = self.call("/api/automation/openapi.json", query={"profile": ["text"]})
        transports = doc["x-cft-transports"]
        self.assertEqual(set(transports), {"mcp", "openapi", "sse", "webhook", "cli"})
        self.assertEqual(transports["webhook"]["events"],
                         list(ai_contract.WEBHOOK_DEFAULT_EVENT_GROUPS))
        self.assertEqual(transports["cli"]["exit_codes"], dict(ai_contract.CLI_EXIT_CODES))
        self.assertEqual(transports["webhook"]["signature"], "HMAC-SHA256")

    def test_profiles_control_admin_visibility(self):
        _, text = self.call("/api/automation/openapi.json", query={"profile": ["text"]})
        _, all_doc = self.call("/api/automation/openapi.json", query={"profile": ["all"]})
        self.assertNotIn("/api/automation/tool/cft_migrate_storage", text["paths"])
        self.assertIn("/api/automation/tool/cft_migrate_storage", all_doc["paths"])
        self.assertTrue(all_doc["paths"]["/api/automation/tool/cft_migrate_storage"]["post"]
                        ["x-cft-requires-confirm"])

    def test_bad_profile_rejected(self):
        st, body = self.call("/api/automation/openapi.json", query={"profile": ["nope"]})
        self.assertEqual(st, 400)
        self.assertEqual(body["code"], "invalid_argument")
        self.assertIn("text", body["hint"])

    def test_servers_only_trust_loopback_host(self):
        _, doc = self.call("/api/automation/openapi.json",
                           query={"profile": ["text"]},
                           ctx=make_ctx("127.0.0.1:47531"))
        self.assertEqual(doc["servers"][0]["url"], "http://127.0.0.1:47531")
        _, doc = self.call("/api/automation/openapi.json",
                           query={"profile": ["text"]},
                           ctx=make_ctx("evil.example:47531"))
        self.assertEqual(doc["servers"][0]["url"], ai_contract.DEFAULT_BRIDGE)
        self.assertEqual(openapi_projection.server_url("localhost"), "http://localhost:47531")
        self.assertEqual(openapi_projection.server_url("localhost:5000"),
                         "http://localhost:5000")

    def test_event_catalog_is_advertised(self):
        _, doc = self.call("/api/automation/openapi.json", query={"profile": ["text"]})
        types = {item["type"] for item in doc["x-cft-events"]}
        self.assertIn("task.finished", types)
        self.assertIn("ui.overlays.changed", types)
        self.assertEqual(len(types), len(ai_contract.EVENTS))

    def test_capabilities_advertise_openapi_and_sse(self):
        st, body = self.call("/api/automation/tool", "POST",
                             body={"name": "cft_capabilities", "arguments": {}})
        self.assertEqual(st, 200, body)
        caps = body["data"]["caps"]
        self.assertTrue(caps["openapi"])
        self.assertTrue(caps["sse"])
        self.assertIn("task.finished", caps["events"])
        self.assertIn("events/stream", caps["transports"]["sse"])

    def test_manifest_lists_new_routes(self):
        st, body = self.call("/api/automation")
        self.assertEqual(st, 200)
        paths = {(row["method"], row["path"]) for row in body["endpoints"]}
        self.assertIn(("GET", "/api/automation/openapi.json"), paths)
        self.assertIn(("POST", "/api/automation/tool/{name}"), paths)
        self.assertIn(("GET", "/api/automation/events/stream"), paths)

    def test_alias_matches_generic_tool(self):
        st1, generic = self.call("/api/automation/tool", "POST",
                                 body={"name": "cft_capabilities", "arguments": {}})
        st2, alias = self.call("/api/automation/tool/cft_capabilities", "POST", body={})
        self.assertEqual(st1, 200)
        self.assertEqual(st2, 200, alias)
        self.assertEqual(generic["ok"], alias["ok"])
        self.assertEqual(generic["tool"], alias["tool"])
        self.assertEqual(generic["data"]["toolsets"], alias["data"]["toolsets"])

    def test_alias_content_format_query(self):
        st, body = self.call("/api/automation/tool/cft_capabilities", "POST",
                             query={"content_format": ["openai"]}, body={})
        self.assertEqual(st, 200, body)
        self.assertTrue(body["ok"])

    def test_alias_unknown_tool(self):
        st, body = self.call("/api/automation/tool/cft_not_a_tool", "POST", body={})
        self.assertEqual(st, 404)
        self.assertEqual(body["code"], "not_found")
        self.assertTrue(body["hint"])

    def test_alias_rejects_non_object_body(self):
        st, body = self.call("/api/automation/tool/cft_capabilities", "POST", body=[])
        self.assertEqual(st, 400)
        self.assertEqual(body["code"], "invalid_argument")

    def test_alias_get_method_rejected(self):
        st, body = self.call("/api/automation/tool/cft_capabilities")
        self.assertEqual(st, 405)
        self.assertTrue(body["hint"])
        self.assertIn("openapi.json", body["hint"])

    def test_document_is_json_serializable_and_has_no_server_paths(self):
        _, doc = self.call("/api/automation/openapi.json", query={"profile": ["all"]})
        encoded = json.dumps(doc, ensure_ascii=False)
        self.assertNotIn("D:/", encoded)
        self.assertNotIn("\\\\", encoded)


if __name__ == "__main__":
    unittest.main()
