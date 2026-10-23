import os
import sys
import unittest
from unittest import mock

import mcp_autostart


class AutoStartTest(unittest.TestCase):
    def setUp(self):
        mcp_autostart.PROC = None
        self.cfg = {"mcp_http_autostart": True}

    def test_build_args_uses_all_toolsets_and_watchdog(self):
        args = mcp_autostart.build_args("C:/x/CivitaiFreeToolMCP.exe", 47532)
        self.assertIn("--http", args)
        self.assertIn("--exit-with-bridge", args)
        self.assertEqual(args[args.index("--port") + 1], "47532")
        self.assertEqual(args[args.index("--toolsets") + 1], "core,vision,admin")

    def test_source_runs_never_spawn(self):
        with mock.patch.object(sys, "frozen", False, create=True), \
                mock.patch.object(mcp_autostart, "subprocess") as sp:
            self.assertIsNone(mcp_autostart.maybe_start("C:/app", self.cfg))
            sp.Popen.assert_not_called()

    def test_opt_out_and_missing_exe_skip(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(mcp_autostart.os.path, "isfile", return_value=False), \
                mock.patch.object(mcp_autostart, "subprocess") as sp:
            self.assertIsNone(mcp_autostart.maybe_start("C:/app", {"mcp_http_autostart": False}))
            self.assertIsNone(mcp_autostart.maybe_start("C:/app", self.cfg))
            sp.Popen.assert_not_called()

    def test_busy_port_skips(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(mcp_autostart.os.path, "isfile", return_value=True), \
                mock.patch.object(mcp_autostart.mcp_paths, "port_open", return_value=True), \
                mock.patch.object(mcp_autostart, "subprocess") as sp:
            self.assertIsNone(mcp_autostart.maybe_start("C:/app", self.cfg))
            sp.Popen.assert_not_called()

    def test_spawns_hidden_sidecar(self):
        proc = mock.Mock(pid=4242)
        proc.poll.return_value = None
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(mcp_autostart.os.path, "isfile", return_value=True), \
                mock.patch.object(mcp_autostart.mcp_paths, "port_open", return_value=False), \
                mock.patch.object(mcp_autostart, "subprocess") as sp:
            sp.Popen.return_value = proc
            sp.DEVNULL = subprocess_devnull()
            out = mcp_autostart.maybe_start("C:/app", self.cfg)
        self.assertIs(out, proc)
        cmd = sp.Popen.call_args.args[0]
        self.assertTrue(str(cmd[0]).endswith("CivitaiFreeToolMCP.exe"))
        kwargs = sp.Popen.call_args.kwargs
        self.assertEqual(kwargs["cwd"], "C:/app")
        self.assertEqual(kwargs["stdout"], subprocess_devnull())
        if os.name == "nt":
            self.assertEqual(kwargs["creationflags"], mcp_autostart.CREATE_NO_WINDOW)

    def test_status_reports_token_without_secret(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            token_file = Path(tmp) / "token.txt"
            token_file.write_text("SECRET-VALUE\n", encoding="utf-8")
            with mock.patch.object(mcp_autostart.mcp_paths, "port_open", return_value=True), \
                    mock.patch.object(mcp_autostart.mcp_paths, "token_path", return_value=token_file):
                st = mcp_autostart.status(self.cfg)
        self.assertTrue(st["running"])
        self.assertEqual(st["port"], 47532)
        self.assertTrue(st["token_file"].endswith("token.txt"))
        self.assertTrue(st["token_present"])
        self.assertNotIn("SECRET-VALUE", repr(st))


def subprocess_devnull():
    import subprocess
    return subprocess.DEVNULL


if __name__ == "__main__":
    unittest.main()
