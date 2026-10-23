import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import mcp_setup


class McpSetupTest(unittest.TestCase):
    def test_stdio_entry(self):
        entry = mcp_setup.server_entry(Path("C:/x/CivitaiFreeToolMCP.exe"), "stdio",
                                       "core,vision", "", "cursor")
        self.assertTrue(entry["command"].endswith("CivitaiFreeToolMCP.exe"))
        self.assertEqual(entry["args"], ["--stdio", "--toolsets", "core,vision"])
        self.assertEqual(entry["env"]["CFT_BRIDGE_URL"], "http://127.0.0.1:47531")

    def test_http_entry_has_type_for_claude_only(self):
        cursor = mcp_setup.server_entry(None, "http", "", "T", "cursor")
        claude = mcp_setup.server_entry(None, "http", "", "T", "claude")
        self.assertEqual(cursor["url"], "http://127.0.0.1:47532/mcp")
        self.assertNotIn("type", cursor)
        self.assertEqual(claude["type"], "http")
        self.assertEqual(cursor["headers"]["Authorization"], "Bearer T")

    def test_merge_preserves_other_servers_and_backs_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp.json"
            path.write_text(json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "x"}}}),
                            encoding="utf-8")
            backup = mcp_setup.merge_config(path, "civitai-free-tool", {"command": "cft"})
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["theme"], "dark")
            self.assertEqual(sorted(data["mcpServers"]), ["civitai-free-tool", "other"])
            self.assertTrue(backup and Path(backup).is_file())

    def test_invalid_json_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mcp.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                mcp_setup.merge_config(path, "civitai-free-tool", {})
            self.assertEqual(path.read_text(encoding="utf-8"), "{not json")

    def test_apply_never_echoes_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            token_file = tmp / "token.txt"
            token_file.write_text("SUPER-SECRET-TOKEN\n", encoding="utf-8")
            target = tmp / "client.json"
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = mcp_setup.main(["--client", "cursor", "--transport", "http",
                                       "--path", str(target), "--token-file", str(token_file),
                                       "--apply"])
            self.assertEqual(code, 0)
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(data["mcpServers"]["civitai-free-tool"]["headers"]["Authorization"],
                             "Bearer SUPER-SECRET-TOKEN")
            self.assertNotIn("SUPER-SECRET-TOKEN", out.getvalue() + err.getvalue())

    def test_dry_run_http_uses_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "client.json"
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = mcp_setup.main(["--client", "cursor", "--transport", "http",
                                       "--path", str(target), "--token-file", str(Path(tmp) / "missing")])
            self.assertEqual(code, 0)
            self.assertIn("<TOKEN_FROM_FILE>", out.getvalue())
            self.assertFalse(target.exists())

    def test_dry_run_never_prints_an_existing_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            token_file = tmp / "token.txt"
            token_file.write_text("DO-NOT-PRINT-ME\n", encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = mcp_setup.main(["--client", "claude", "--transport", "http",
                                       "--token-file", str(token_file)])
            self.assertEqual(code, 0)
            text = out.getvalue() + err.getvalue()
            self.assertNotIn("DO-NOT-PRINT-ME", text)
            self.assertIn("<TOKEN_FROM_FILE>", out.getvalue())


if __name__ == "__main__":
    unittest.main()
