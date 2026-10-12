# -*- coding: utf-8 -*-
"""GET /api/media（资源库媒体流式端点）回归：真实 HTTP 服务 + 临时目录里的真文件。

桥层启动写法对齐 tests/test_bridge_origin.py（urllib 驱动）与 test_browser_bridge.py
（setUpClass 起服务 / tearDownClass 注销注入），但用 ("127.0.0.1", 0) 取临时端口：
默认 47531 可能被真实主程序占用，临时端口才保证测试互不干扰。
"""
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stderr
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import browser_bridge

# 4096 字节确定性负载：端点只搬运字节、不解析媒体，故无需真实 PNG/mp4 结构
PAYLOAD = bytes(bytearray((i * 7 + 3) % 251 for i in range(4096)))
BIG_SIZE = 2 * 1024 * 1024


class MediaStreamTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        base = os.path.realpath(cls.temp.name)
        cls.root = os.path.join(base, "library")          # 配置进白名单的根
        cls.outside = os.path.join(base, "outside")        # 不在白名单里的目录
        os.makedirs(cls.root)
        os.makedirs(cls.outside)
        cls.png = cls._write(cls.root, "a.png", PAYLOAD)
        cls.mp4 = cls._write(cls.root, "clip.mp4", PAYLOAD)
        cls.upper = cls._write(cls.root, "UPPER.PNG", PAYLOAD)
        cls.empty = cls._write(cls.root, "empty.png", b"")
        cls.bad_ext = cls._write(cls.root, "weights.txt", PAYLOAD)
        cls.big = cls._write(cls.root, "big.mp4", bytes(bytearray(range(256))) * (BIG_SIZE // 256))
        cls.secret = cls._write(cls.outside, "secret.png", PAYLOAD)
        # 根内 → 根外的链接（符号链接或 Windows junction），用于验「软链逃逸」
        cls.escape_file = os.path.join(cls.root, "escape.png")
        cls.escape_dir_file = os.path.join(cls.root, "linkdir", "secret.png")
        cls._make_escape_link()
        cls.roots = [cls.root]

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), browser_bridge._Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={"poll_interval": 0.2}, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]
        cls.base_url = "http://127.0.0.1:%d" % cls.port
        browser_bridge.set_media_roots(lambda: list(cls.roots))

    @classmethod
    def tearDownClass(cls):
        browser_bridge.set_media_roots(None)
        try:
            cls.server.shutdown()
        finally:
            cls.server.server_close()
            cls.temp.cleanup()

    # ---------------- 工具 ----------------
    @staticmethod
    def _write(directory, name, data):
        path = os.path.join(directory, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    @classmethod
    def _make_escape_link(cls):
        """根内建链接指向根外：Windows 无符号链接权限时退化成目录 junction（同一逃逸面）"""
        try:
            os.symlink(cls.secret, cls.escape_file)
            cls.escape_target = cls.escape_file
            return
        except OSError:
            pass
        try:
            import _winapi
            _winapi.CreateJunction(cls.outside, os.path.join(cls.root, "linkdir"))
            cls.escape_target = cls.escape_dir_file
        except Exception:
            cls.escape_target = None

    def request(self, path, headers=None):
        req = urllib.request.Request(self.base_url + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def media(self, p, range_header=None, origin=None, encode="default"):
        safe = "" if encode == "strict" else "/"
        headers = {}
        if range_header:
            headers["Range"] = range_header
        if origin:
            headers["Origin"] = origin
        return self.request("/api/media?p=" + urllib.parse.quote(p, safe=safe), headers)

    def assertDenied(self, status, headers, body, expect_code, leak=None):
        """拒绝必须发生在写出任何文件字节之前：错误体是 JSON、长度自洽、不含文件内容"""
        self.assertEqual(status, expect_code, body[:200])
        ctype = headers.get("Content-Type") or ""
        self.assertTrue(ctype.startswith("application/json"), ctype)
        self.assertEqual(int(headers.get("Content-Length") or -1), len(body))
        payload = (leak if leak is not None else PAYLOAD)[:256]
        self.assertNotIn(payload, body)
        text = body.decode("utf-8", "replace")
        self.assertNotIn("Traceback", text)
        self.assertNotIn("File \"", text)
        d = json.loads(text)
        self.assertFalse(d["ok"])
        self.assertTrue(d["msg"])
        self.assertLessEqual(len(d["msg"]), browser_bridge._MEDIA_MSG_MAX)
        return d

    # ---------------- 200 / 206 ----------------
    def test_full_body_200(self):
        st, h, body = self.media(self.png)
        self.assertEqual(st, 200)
        self.assertEqual(body, PAYLOAD)
        self.assertEqual(int(h["Content-Length"]), len(PAYLOAD))   # 长度必须精确
        self.assertEqual(h["Content-Type"], "image/png")
        self.assertEqual(h["Accept-Ranges"], "bytes")
        self.assertEqual(h["Cache-Control"], "private, max-age=3600")
        self.assertNotIn("Content-Range", h)

    def test_video_content_type_and_uppercase_ext(self):
        for p, want in ((self.mp4, "video/mp4"), (self.upper, "image/png")):
            st, h, body = self.media(p)
            self.assertEqual((st, h["Content-Type"], body), (200, want, PAYLOAD), p)

    def test_a3_style_url_encoding(self):
        """§5.4 用 urllib.parse.quote(abspath)（默认 safe='/'）拼 URL —— 必须能对上"""
        st, h, body = self.media(self.png, encode="default")
        self.assertEqual((st, body), (200, PAYLOAD))
        # 文件名里的 & = % 与中文不得被解析成额外查询参数（quote 会编码它们）
        tricky = self._write(self.root, "a&b=c 90%.png", PAYLOAD)
        for encode in ("default", "strict"):
            st, h, body = self.media(tricky, encode=encode)
            self.assertEqual((st, body), (200, PAYLOAD), encode)

    def test_range_head(self):
        st, h, body = self.media(self.png, "bytes=0-99")
        self.assertEqual(st, 206)
        self.assertEqual(body, PAYLOAD[:100])
        self.assertEqual(h["Content-Range"], "bytes 0-99/%d" % len(PAYLOAD))
        self.assertEqual(int(h["Content-Length"]), 100)
        self.assertEqual(h["Accept-Ranges"], "bytes")

    def test_range_open_end(self):
        st, h, body = self.media(self.png, "bytes=100-")
        self.assertEqual(st, 206)
        self.assertEqual(body, PAYLOAD[100:])
        self.assertEqual(h["Content-Range"], "bytes 100-%d/%d" % (len(PAYLOAD) - 1, len(PAYLOAD)))
        self.assertEqual(int(h["Content-Length"]), len(PAYLOAD) - 100)

    def test_range_suffix(self):
        st, h, body = self.media(self.png, "bytes=-50")
        self.assertEqual(st, 206)
        self.assertEqual(body, PAYLOAD[-50:])
        self.assertEqual(h["Content-Range"], "bytes %d-%d/%d" % (len(PAYLOAD) - 50, len(PAYLOAD) - 1, len(PAYLOAD)))

    def test_range_clamped_end(self):
        st, h, body = self.media(self.png, "bytes=4000-999999")
        self.assertEqual(st, 206)
        self.assertEqual(body, PAYLOAD[4000:])
        self.assertEqual(h["Content-Range"], "bytes 4000-%d/%d" % (len(PAYLOAD) - 1, len(PAYLOAD)))
        self.assertEqual(int(h["Content-Length"]), len(PAYLOAD) - 4000)

    def test_ranges_via_parse_helper(self):
        """纯函数侧：Range 解析矩阵（None = 交给 416）"""
        pm = browser_bridge.parse_media_range
        size = 100
        self.assertEqual(pm("bytes=0-99", size), (0, 99))
        self.assertEqual(pm("bytes=100-", size), None)
        self.assertEqual(pm("bytes=50-", size), (50, 99))
        self.assertEqual(pm("bytes=10-999", size), (10, 99))
        self.assertEqual(pm("bytes=-30", size), (70, 99))
        self.assertEqual(pm("bytes=-300", size), (0, 99))
        self.assertEqual(pm("bytes=90-10", size), None)     # 起点在终点之后
        self.assertEqual(pm("bytes=0-1,5-6", size), None)   # 多范围按非法
        self.assertEqual(pm("bytes=abc-def", size), None)
        self.assertEqual(pm("bytes=-", size), None)
        self.assertEqual(pm("bytes=-0", size), None)
        self.assertEqual(pm("megabytes=0-1", size), None)
        self.assertEqual(pm("bytes=0-", 0), None)           # 空文件任何范围都不可满足
        self.assertEqual(pm("bytes=0-0", 0), None)

    def test_invalid_ranges_416(self):
        for bad in ("bytes=9999-", "bytes=5-2", "bytes=0-1,3-4", "bytes=abc", "bytes=-0", "bytes="):
            st, h, body = self.media(self.png, bad)
            self.assertEqual(st, 416, bad)
            self.assertEqual(h["Content-Range"], "bytes */%d" % len(PAYLOAD), bad)
            self.assertEqual(int(h["Content-Length"]), len(body))
            self.assertNotIn(PAYLOAD[:64], body)

    def test_empty_file(self):
        st, h, body = self.media(self.empty)
        self.assertEqual((st, body), (200, b""))
        self.assertEqual(int(h["Content-Length"]), 0)
        st, h, body = self.media(self.empty, "bytes=0-")     # 不除零、不挂起
        self.assertEqual(st, 416)
        self.assertEqual(h["Content-Range"], "bytes */0")

    def test_multi_chunk_streaming_exact_length(self):
        """跨多个 64KB 分块：Content-Length 必须与实际写出的字节数完全一致"""
        st, h, body = self.media(self.big)
        self.assertEqual(st, 200)
        self.assertEqual(len(body), BIG_SIZE)
        self.assertEqual(int(h["Content-Length"]), BIG_SIZE)
        st, h, body = self.media(self.big, "bytes=%d-" % (BIG_SIZE - 1000))
        self.assertEqual((st, len(body), h["Content-Range"]),
                         (206, 1000, "bytes %d-%d/%d" % (BIG_SIZE - 1000, BIG_SIZE - 1, BIG_SIZE)))

    # ---------------- 拒绝 ----------------
    def test_missing_p_and_empty_p(self):
        self.assertDenied(*self.request("/api/media"), 403)
        self.assertDenied(*self.request("/api/media?p="), 403)
        self.assertDenied(*self.media(""), 403)

    def test_relative_path_rejected(self):
        for rel in ("a.png", "library/a.png", ".", "./a.png"):
            self.assertDenied(*self.media(rel), 403)

    def test_traversal_rejected(self):
        for bad in (os.path.join(self.root, "..", "outside", "secret.png"),
                    os.path.join(self.root, "sub", "..", "..", "outside", "secret.png"),
                    self.root + "\\..\\outside\\secret.png",
                    self.root + "/../outside/secret.png"):
            st, h, body = self.media(bad)
            self.assertDenied(st, h, body, 403, leak=PAYLOAD[:256])

    def test_nul_byte_rejected(self):
        st, h, body = self.media(self.png[:-4] + "\x00.evil.png")
        self.assertDenied(st, h, body, 403)

    def test_extension_outside_media_set_rejected(self):
        for p in (self.bad_ext, os.path.join(self.root, "noext"),
                  os.path.join(self.root, "x.safetensors"), os.path.join(self.root, "x.exe")):
            st, h, body = self.media(p)
            self.assertDenied(st, h, body, 403)

    def test_outside_root_rejected_as_404(self):
        st, h, body = self.media(self.secret)
        d = self.assertDenied(st, h, body, 404)
        self.assertNotIn(os.path.basename(self.secret), d["msg"])  # 不回显路径

    def test_missing_file_same_as_outside_root(self):
        """越界与「文件不存在」同码同文案：状态位不能用来枚举白名单根"""
        ghost = os.path.join(self.root, "ghost.png")
        st1, h1, b1 = self.media(self.secret)
        st2, h2, b2 = self.media(ghost)
        self.assertEqual((st1, st2), (404, 404))
        self.assertEqual(json.loads(b1)["msg"], json.loads(b2)["msg"])

    def test_sibling_prefix_is_not_inside_root(self):
        """前缀陷阱：library_backup 不是 library 的子路径"""
        sibling = os.path.join(os.path.dirname(self.root), "library_backup")
        os.makedirs(sibling, exist_ok=True)
        p = self._write(sibling, "sib.png", PAYLOAD)
        self.assertDenied(*self.media(p), 404)

    def test_symlink_escape_rejected(self):
        if self.escape_target is None:
            self.skipTest("当前环境既不能建符号链接也不能建 junction（需管理员/开发者模式）")
        st, h, body = self.media(self.escape_target)
        self.assertDenied(st, h, body, 404)

    def test_origin_gate_still_applies(self):
        """不能因为新增端点就放松来源校验"""
        st, h, body = self.media(self.png, origin="https://evil.example.com")
        self.assertDenied(st, h, body, 403)
        self.assertIn("来源被拒绝", json.loads(body)["msg"])
        # 合法来源照常工作
        st, h, body = self.media(self.png, origin="http://127.0.0.1:%d" % self.port)
        self.assertEqual((st, body), (200, PAYLOAD))

    def test_roots_not_configured_403(self):
        browser_bridge.set_media_roots(None)
        try:
            st, h, body = self.media(self.png)
            self.assertDenied(st, h, body, 403)
            self.assertIn("资源库", json.loads(body)["msg"])
        finally:
            browser_bridge.set_media_roots(lambda: list(self.roots))
        self.assertEqual(self.media(self.png)[0], 200)   # 恢复后可用

    def test_roots_empty_or_raising(self):
        for fn in (lambda: [], lambda: None, _boom):
            browser_bridge.set_media_roots(fn)
            try:
                self.assertDenied(*self.media(self.png), 404)
            finally:
                browser_bridge.set_media_roots(lambda: list(self.roots))
        self.assertEqual(self.media(self.png)[0], 200)

    # ---------------- 客户端提前断开 ----------------
    def test_client_abort_mid_stream_keeps_server_alive(self):
        raw = socket.create_connection(("127.0.0.1", self.port), timeout=15)
        stderr_capture = __import__("io").StringIO()
        try:
            req = ("GET /api/media?p=%s HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   "Range: bytes=0-\r\nConnection: close\r\n\r\n"
                   % urllib.parse.quote(self.big, safe="")).encode("ascii")
            with redirect_stderr(stderr_capture):
                raw.sendall(req)
                head = raw.recv(8192)                     # 只读个头，剩下的还在队列里
                self.assertIn(b"206", head.split(b"\r\n", 1)[0])
                # SO_LINGER(l_onoff=1, l_linger=0) → close 直接发 RST，服务端后续 write 必报错
                raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                raw.close()
                # 后续请求必须照常应答（处理线程没挂、没被堆栈打断）
                st, h, body = self.media(self.png, "bytes=0-9")
                self.assertEqual((st, body), (206, PAYLOAD[:10]))
                self.assertEqual(self.request("/api/heartbeat")[0], 200)
                time.sleep(0.2)                            # 给服务端把这次断开处理完
        finally:
            try:
                raw.close()
            except OSError:
                pass
        self.assertNotIn("Traceback", stderr_capture.getvalue(),
                         "客户端断开在 stderr 喷了堆栈: %s" % stderr_capture.getvalue()[:400])

    def test_unreachable_path_does_not_hang(self):
        """不存在的盘符/深层目录：只能得到 403/404，不得抛异常或挂住（不测 UNC，避免名称解析超时）"""
        for p in ("Z:\\nope\\a.png", os.path.join(self.root, "sub", "deeper", "x.png")):
            st, h, body = self.media(p)
            self.assertIn(st, (403, 404), p)
            self.assertNotIn(PAYLOAD[:64], body)


def _boom():
    raise RuntimeError("roots 提供者炸了也不该影响桥服务")


if __name__ == "__main__":
    unittest.main(verbosity=2)
