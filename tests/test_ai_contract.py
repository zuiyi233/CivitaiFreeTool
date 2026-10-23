# -*- coding: utf-8 -*-
"""ai_contract：工具表、profiles、schema、错误码的离线契约测试。"""
import json
import unittest

import ai_contract


class ContractTest(unittest.TestCase):
    def test_tool_names_unique_and_toolsets_cover_all(self):
        names = list(ai_contract.TOOLS)
        self.assertEqual(len(names), len(set(names)))
        covered = []
        for names_in_set in ai_contract.TOOLSETS.values():
            covered.extend(names_in_set)
        self.assertEqual(sorted(covered), sorted(names))

    def test_every_tool_has_schema_and_annotations(self):
        for name, tool in ai_contract.TOOLS.items():
            self.assertTrue(name.startswith("cft_"), name)
            self.assertEqual(tool["inputSchema"].get("type"), "object", name)
            self.assertFalse(tool["inputSchema"].get("additionalProperties", True), name)
            for key in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
                self.assertIsInstance(tool["annotations"].get(key), bool, (name, key))
            json.dumps(tool["inputSchema"])

    def test_profiles_are_ordered_and_expected_sizes(self):
        self.assertEqual(len(ai_contract.tools_for_profile("text")), 19)
        self.assertEqual(len(ai_contract.tools_for_profile("vision")), 24)
        self.assertEqual(len(ai_contract.tools_for_profile("admin")), 24)
        self.assertEqual(len(ai_contract.tools_for_profile("all")), 29)
        self.assertEqual(ai_contract.tools_for_profile("nope")[0]["toolset"], "core")

    def test_destructive_tools_are_marked(self):
        for name in ("cft_migrate_storage", "cft_delete_gallery", "cft_control_tasks",
                     "cft_manage_library"):
            self.assertTrue(ai_contract.TOOLS[name]["annotations"]["destructiveHint"], name)
        self.assertFalse(ai_contract.TOOLS["cft_get_state"]["annotations"]["destructiveHint"])

    def test_public_projection_shapes(self):
        tool = ai_contract.TOOLS["cft_get_state"]
        mcp = ai_contract.public_tool(tool, format="mcp")
        self.assertIn("inputSchema", mcp)
        self.assertNotIn("type", mcp)
        openai = ai_contract.public_tool(tool, format="openai")
        self.assertEqual(openai["type"], "function")
        self.assertIn("parameters", openai["function"])
        anthropic = ai_contract.public_tool(tool, format="anthropic")
        self.assertIn("input_schema", anthropic)

    def test_error_normalization_is_stable(self):
        out = ai_contract.normalize_error(404, {"msg": "x"})
        self.assertEqual(out["code"], "not_found")
        self.assertFalse(out["retryable"])
        out = ai_contract.normalize_error(500, {"msg": "x"})
        self.assertEqual(out["code"], "internal_error")
        self.assertTrue(out["retryable"])
        out = ai_contract.normalize_error(400, {"code": "destructive_requires_confirm", "msg": "x"})
        self.assertEqual(out["code"], "destructive_requires_confirm")
        self.assertFalse(out["retryable"])

    def test_resources_and_prompts_are_well_formed(self):
        uris = [r["uri"] for r in ai_contract.RESOURCES]
        self.assertEqual(len(uris), len(set(uris)))
        for r in ai_contract.RESOURCES:
            self.assertTrue(r["uri"].startswith("cft://"))
            self.assertTrue(r["name"] and r["description"])
        names = [p["name"] for p in ai_contract.PROMPTS]
        self.assertEqual(len(names), len(set(names)))
        for p in ai_contract.PROMPTS:
            if p.get("arguments"):
                self.assertIn("{", p["template"])
            self.assertTrue(p["title"] and p["description"])


if __name__ == "__main__":
    unittest.main()
