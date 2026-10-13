# -*- coding: utf-8 -*-
"""video_thumb 回归网：外壳出首帧（真实 mp4 实测）+ 包装层的安全边界（打桩）。

约定：真实库用例只在显式设置 CIVITAI_TEST_REAL_VIDEO_DIR 且目录存在时跑，并且**只读**那个目录
（先把样本拷进临时目录，跑完再核对源目录条目一字未动）。其余用例全部临时目录 + 打桩，
不触网、不依赖原生 DLL、不碰用户配置。"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image

import image_gallery
import video_thumb

REAL_DIR = os.environ.get("CIVITAI_TEST_REAL_VIDEO_DIR", r"D:\ComfyUI_Mie_2026_V8.0\ComfyUI\output")
REAL_SAMPLES = ("H3VM_REF2VA_MAINFIRST_00001.mp4", "H3VM_REF2VA_MAINFIRST_00002.mp4")


def jpeg(width=320, height=200, noise=True, quality=80):
    """造一张真能解码的 JPEG；noise=False 时是纯色（用来对照「空白海报」长什么样）。"""
    im = Image.new("RGB", (width, height))
    if noise:
        im.putdata([((x * 7 + y * 13) % 256, (x * 3) % 256, (y * 5) % 256)
                    for y in range(height) for x in range(width)])
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def spread(data):
    """灰度标准差 + 不同灰度个数：真视频帧两项都远高于纯色/全黑占位图。"""
    with Image.open(io.BytesIO(data)) as im:
        small = im.convert("L").resize((32, 32))
        px = list(small.get_flattened_data()) if hasattr(small, "get_flattened_data") else list(small.getdata())
    mean = sum(px) / len(px)
    return (sum((p - mean) ** 2 for p in px) / len(px)) ** 0.5, len(set(px))


def make_file_link(link, target):
    """Windows 优先符号链接（要开发者模式/管理员），目录目标退化成 junction（普通权限即可）。

    注意 `_winapi.CreateJunction` 自己会建 dest 目录，先 mkdir 反而报「文件已存在」。"""
    try:
        os.symlink(target, link, target_is_directory=os.path.isdir(target))
        return True
    except (OSError, SystemError):
        pass
    if os.name == "nt" and os.path.isdir(target):
        try:
            import _winapi
            _winapi.CreateJunction(target, link)
            return True
        except Exception:
            pass
    return False


class WrapperUnitTests(unittest.TestCase):
    """包装层：打桩替掉原生调用，只验自己的契约（原子独占写、拒链接、拒坏字节、绝不抛）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.video = self.root / "v.mp4"
        self.video.write_bytes(b"fake-video-bytes")           # 包装层不解析视频，内容无所谓
        self.out = self.root / "image_01.jpg"

    def _extract(self, data=None, exc=None):
        def fake(_path, _max_size):
            if exc is not None:
                raise exc
            return data
        return fake

    # --- 成功路径 ---
    def test_writes_valid_jpeg_and_leaves_no_tmp(self):
        data = jpeg()
        with patch.object(video_thumb, "_extract", self._extract(data)):
            self.assertTrue(video_thumb.first_frame(str(self.video), str(self.out), 640))
        self.assertEqual(self.out.read_bytes(), data)
        with Image.open(str(self.out)) as im:
            self.assertEqual((im.width, im.height), (320, 200))
        # 原子写：不留 .tmp，也不在模型目录里造第二个文件
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["image_01.jpg", "v.mp4"])

    def test_tmp_goes_through_the_shared_exclusive_helper(self):
        """落盘必须复用 image_gallery.open_exclusive_tmp（同目录同名 .tmp），不再自己造第二套。"""
        real = image_gallery.open_exclusive_tmp          # 打桩之前先抓住真实现
        seen = {}

        def spy(path):
            seen["path"] = path
            return real(path)

        with patch.object(video_thumb, "_extract", self._extract(jpeg())), \
             patch.object(image_gallery, "open_exclusive_tmp", spy):
            self.assertTrue(video_thumb.first_frame(str(self.video), str(self.out)))
        self.assertEqual(seen["path"], str(self.out) + ".tmp")
        self.assertTrue(self.out.is_file())

    def test_leftover_plain_tmp_is_replaced_not_reused(self):
        """残留 .tmp 是下载中断的常态：清掉重来，但绝不清目标本身。"""
        (self.root / "image_01.jpg.tmp").write_bytes(b"stale")
        with patch.object(video_thumb, "_extract", self._extract(jpeg())):
            self.assertTrue(video_thumb.first_frame(str(self.video), str(self.out)))
        self.assertFalse((self.root / "image_01.jpg.tmp").exists())
        self.assertTrue(self.out.is_file())

    def test_tmp_that_is_a_junction_is_refused(self):
        """预先在 .tmp 名字上放一个指向别处的链接 → 独占创建必失败，且链接后面不能被写。"""
        external = self.root / "external"
        external.mkdir()
        link = str(self.out) + ".tmp"
        if not make_file_link(link, str(external)):
            self.skipTest("本机无法创建符号链接/junction（需开发者模式或管理员）")
        with patch.object(video_thumb, "_extract", self._extract(jpeg())):
            self.assertFalse(video_thumb.first_frame(str(self.video), str(self.out)))
        self.assertFalse(self.out.exists())
        self.assertEqual(sorted(external.iterdir()), [])
        self.assertFalse(video_thumb.last_error() == "")

    # --- 拒写 ---
    def test_refuses_reparse_point_output_simulated(self):
        """守卫分支（无建链接权限的机器也覆盖得到）：目标被判为重解析点就绝不写。"""
        self.out.write_bytes(b"DO NOT TOUCH")
        real = image_gallery.is_reparse_path
        with patch.object(image_gallery, "is_reparse_path", lambda p: True if p == str(self.out) else real(p)), \
             patch.object(video_thumb, "_extract", self._extract(jpeg())):
            self.assertFalse(video_thumb.first_frame(str(self.video), str(self.out)))
        self.assertEqual(self.out.read_bytes(), b"DO NOT TOUCH")
        self.assertIn("链接", video_thumb.last_error())

    def test_refuses_symlink_or_junction_output(self):
        external = self.root / "elsewhere.jpg"
        external.write_bytes(b"DO NOT TOUCH")
        if not make_file_link(str(self.out), str(external)):
            self.skipTest("本机无法创建符号链接/junction（需开发者模式或管理员）")
        self.assertTrue(image_gallery.is_reparse_path(str(self.out)))   # 守卫前提：islink 看不出来
        with patch.object(video_thumb, "_extract", self._extract(jpeg())):
            self.assertFalse(video_thumb.first_frame(str(self.video), str(self.out)))
        self.assertEqual(external.read_bytes(), b"DO NOT TOUCH")        # 没顺着链接写出去

    def test_refuses_reparse_output_dir(self):
        """目标目录本身是 junction 时，写出去就跑到模型目录之外——必须拒。"""
        external = self.root / "outside"
        external.mkdir()
        link_dir = self.root / "m.images"
        if not make_file_link(str(link_dir), str(external)):
            self.skipTest("本机无法创建符号链接/junction（需开发者模式或管理员）")
        with patch.object(video_thumb, "_extract", self._extract(jpeg())):
            self.assertFalse(video_thumb.first_frame(str(self.video), str(link_dir / "image_01.jpg")))
        self.assertEqual(sorted(external.iterdir()), [])

    def test_refuses_alternate_data_stream_target(self):
        """目标文件名带冒号 = 往别的文件的备用数据流里塞字节，必须在调用外壳之前就拒。"""
        calls = []
        with patch.object(video_thumb, "_extract", lambda p, m: calls.append(p) or jpeg()):
            self.assertFalse(video_thumb.first_frame(str(self.video), str(self.out) + ":evil"))
        self.assertEqual(calls, [])
        self.assertFalse(any(":" in n for n in os.listdir(self.root)))

    def test_does_not_create_missing_dir(self):
        """只写「给定的那个目录」：目录不存在就失败，绝不顺手 mkdir 到别处。"""
        missing = self.root / "nope" / "image_01.jpg"
        calls = []
        with patch.object(video_thumb, "_extract", lambda p, m: calls.append(p) or jpeg()):
            self.assertFalse(video_thumb.first_frame(str(self.video), str(missing)))
        self.assertFalse(missing.parent.exists())
        self.assertEqual(calls, [])                                      # 守卫在调用外壳之前就回绝

    def test_refuses_non_image_bytes(self):
        for bad in (b"x" * 4000, b"GIF89a" + b"\0" * 4000, b"\xff\xd8\xff" + b"\0" * 4000, b"", None,
                   bytearray(b"nope"), "字符串"):
            with self.subTest(len=len(bad) if isinstance(bad, (bytes, bytearray, str)) else None):
                with patch.object(video_thumb, "_extract", self._extract(bad)):
                    self.assertFalse(video_thumb.first_frame(str(self.video), str(self.out)))
                self.assertFalse(self.out.exists())
                self.assertFalse((self.root / "image_01.jpg.tmp").exists())
                self.assertTrue(video_thumb.last_error())

    def test_rejects_oversize_poster_and_documents_solid_color_baseline(self):
        """超出 max_size 的结果判为无效；纯色帧能解码但「看起来是空的」——真实用例靠这个差值判内容。"""
        with patch.object(video_thumb, "_extract", self._extract(jpeg(noise=False))):
            self.assertTrue(video_thumb.first_frame(str(self.video), str(self.out)))
        std, uniq = spread(self.out.read_bytes())
        self.assertLess(std, 1.0)                                        # 纯色样本：标准差≈0
        self.assertLessEqual(uniq, 2)
        self.out.unlink()
        big = jpeg(1200, 900)
        with patch.object(video_thumb, "_extract", self._extract(big)):
            self.assertFalse(video_thumb.first_frame(str(self.video), str(self.out), 640))
        self.assertFalse(self.out.exists())
        self.assertIn("max_size", video_thumb.last_error())
        with patch.object(video_thumb, "_extract", self._extract(big)):
            self.assertTrue(video_thumb.first_frame(str(self.video), str(self.out), 1280))
        self.assertTrue(self.out.is_file())

    def test_existing_poster_not_overwritten_by_default(self):
        self.out.write_bytes(b"already-there")
        with patch.object(video_thumb, "_extract", self._extract(jpeg())):
            self.assertFalse(video_thumb.first_frame(str(self.video), str(self.out)))
            self.assertEqual(self.out.read_bytes(), b"already-there")
            self.assertTrue(video_thumb.first_frame(str(self.video), str(self.out), overwrite=True))
        self.assertNotEqual(self.out.read_bytes(), b"already-there")

    def test_missing_video_never_reaches_native(self):
        calls = []
        with patch.object(video_thumb, "_extract", lambda p, m: calls.append(p) or jpeg()):
            self.assertFalse(video_thumb.first_frame(str(self.root / "missing.mp4"), str(self.out)))
            self.assertFalse(video_thumb.first_frame(str(self.root), str(self.out)))   # 目录当输入
            self.assertFalse(video_thumb.first_frame("", str(self.out)))
            self.assertFalse(video_thumb.first_frame(None, str(self.out)))
        self.assertEqual(calls, [])
        self.assertFalse(self.out.exists())

    # --- 绝不抛 ---
    def test_returns_false_when_helper_unavailable(self):
        with patch.object(video_thumb, "_helper", lambda: None):
            self.assertFalse(video_thumb.first_frame(str(self.video), str(self.out)))
        self.assertFalse(self.out.exists())
        with patch.object(video_thumb, "_probe", {"done": True, "helper": None}):
            self.assertFalse(video_thumb.available())
            self.assertIsNone(video_thumb._extract(str(self.video), 640))
        with patch.object(sys, "platform", "linux"):
            self.assertFalse(video_thumb.first_frame(str(self.video), str(self.out)))

    def test_never_raises(self):
        boom = [RuntimeError("原生炸了"), MemoryError("GDI+ 炸了"), ZeroDivisionError("除零"),
                OSError(22, "磁盘拒绝"), ValueError("路径有病")]
        for exc in boom:
            with self.subTest(exc=type(exc).__name__):
                with patch.object(video_thumb, "_extract", self._extract(exc=exc)):
                    self.assertFalse(video_thumb.first_frame(str(self.video), str(self.out)))
        with patch.object(video_thumb, "_extract", self._extract(jpeg())), \
             patch.object(image_gallery, "open_exclusive_tmp", side_effect=OSError("独占失败")):
            self.assertFalse(video_thumb.first_frame(str(self.video), str(self.out)))
        with patch.object(video_thumb, "_extract", self._extract(jpeg())), \
             patch.object(video_thumb.os, "replace", side_effect=OSError("替换失败")):
            self.assertFalse(video_thumb.first_frame(str(self.video), str(self.out)))
            self.assertFalse((self.root / "image_01.jpg.tmp").exists())               # 失败要自己扫干净
        self.assertFalse(self.out.exists())
        # 参数怎么喂都不许抛
        for args in [(None, None), ("", ""), (str(self.video), None), (str(self.video), str(self.out)),
                     (str(self.video), str(self.out), 0), (str(self.video), str(self.out), "abc")]:
            with patch.object(video_thumb, "_extract", self._extract(jpeg())):
                self.assertIn(video_thumb.first_frame(*args), (True, False))

    def test_last_error_cleared_on_success(self):
        with patch.object(video_thumb, "_extract", self._extract(b"bad")):
            self.assertFalse(video_thumb.first_frame(str(self.video), str(self.out)))
            self.assertTrue(video_thumb.last_error())
        with patch.object(video_thumb, "_extract", self._extract(jpeg())):
            self.assertTrue(video_thumb.first_frame(str(self.video), str(self.out)))
            self.assertEqual(video_thumb.last_error(), "")

    def test_jpeg_size_fallback_without_pillow(self):
        """没有 Pillow 时也要能认出 JPEG 尺寸（打包环境有，裸环境没有，不能因此写坏文件）。"""
        import builtins
        data = jpeg(321, 199)
        real_import = builtins.__import__

        def blocked(name, *a, **k):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError("no pillow")
            return real_import(name, *a, **k)

        with patch.object(builtins, "__import__", blocked):
            self.assertEqual(video_thumb._decode(data), (321, 199))
            self.assertIsNone(video_thumb._decode(b"\xff\xd8\xff" + b"x" * 4000))
            self.assertIsNone(video_thumb._decode(b"x" * 4000))


class NativeContractTests(unittest.TestCase):
    """原生入口的调用形状：返回 byte[]/None、失败不抛、LastError 有内容。"""

    def test_helper_probe_is_idempotent(self):
        self.assertIs(video_thumb.available(), video_thumb.available())
        self.assertIs(video_thumb._helper(), video_thumb._helper())

    def test_native_call_shape(self):
        if sys.platform != "win32":
            self.skipTest("外壳出图只在 Windows 存在")
        if not video_thumb.available():
            self.skipTest("原生 DLL 未编译：python scripts/build_native.py")
        helper = video_thumb._helper()
        self.assertIsNone(helper.FirstFrameJpeg(os.path.join(tempfile.gettempdir(), "cft-不存在.mp4"), 640, 82))
        self.assertTrue(str(helper.LastError))
        self.assertIsNone(helper.FirstFrameJpeg(tempfile.gettempdir(), 640, 82))     # 目录当输入
        self.assertTrue(str(helper.LastError))


class RealShellFirstFrameTests(unittest.TestCase):
    """真实 mp4 实测：外壳必须给出**有内容**的首帧，而且对源目录零写入。

    跑法：CIVITAI_TEST_REAL_VIDEO_DIR=<放 mp4 的目录> python -m pytest tests/test_video_thumb.py -k Real"""

    def _samples(self):
        if sys.platform != "win32":
            return None
        if not os.environ.get("CIVITAI_TEST_REAL_VIDEO_DIR"):
            return None
        if not os.path.isdir(REAL_DIR):
            return None
        found = [os.path.join(REAL_DIR, n) for n in REAL_SAMPLES if os.path.isfile(os.path.join(REAL_DIR, n))]
        if len(found) < 2:
            return None
        return found

    def snapshot(self, folder):
        return {n: (os.path.getsize(os.path.join(folder, n)), os.path.getmtime(os.path.join(folder, n)))
                for n in os.listdir(folder)}

    def test_real_videos_yield_content_full_frame(self):
        samples = self._samples()
        if samples is None:
            self.skipTest("真实库实测需显式设置 CIVITAI_TEST_REAL_VIDEO_DIR 且目录里有样本")
        if not video_thumb.available():
            self.skipTest("原生 DLL 未编译：python scripts/build_native.py")
        from media_meta import read_mp4_meta
        before = {os.path.dirname(p): self.snapshot(os.path.dirname(p)) for p in samples}
        with tempfile.TemporaryDirectory() as tmp:
            for src in samples:
                video = os.path.join(tmp, os.path.basename(src))
                shutil.copy2(src, video)                     # 只在临时目录里玩，源目录只读
                out = os.path.join(tmp, "image_%02d.jpg" % (samples.index(src) + 1))
                meta = read_mp4_meta(src) or {}
                ok = video_thumb.first_frame(video, out, 640)
                self.assertTrue(ok, "%s 出帧失败：%s" % (os.path.basename(src), video_thumb.last_error()))
                data = open(out, "rb").read()
                self.assertGreaterEqual(len(data), video_thumb.MIN_JPEG_BYTES)
                self.assertLessEqual(len(data), video_thumb.MAX_JPEG_BYTES)
                with Image.open(io.BytesIO(data)) as im:
                    im.load()
                    self.assertEqual(im.format, "JPEG")
                    w, h = im.size
                self.assertLessEqual(max(w, h), 640)
                self.assertGreater(w, 16)
                if meta.get("width") and meta.get("height"):     # 比例必须跟原片一致，否则是拉伸/裁切
                    self.assertAlmostEqual(w / h, meta["width"] / meta["height"], delta=0.02)
                std, uniq = spread(data)
                self.assertGreater(std, 5.0, "海报几乎是纯色，不像真帧")
                self.assertGreaterEqual(uniq, 16, "灰度层次太少，不像真帧")
                # 二次调用不覆盖，且本地已有首帧时不再花一次解码时间
                self.assertFalse(video_thumb.first_frame(video, out, 640))
                self.assertEqual(open(out, "rb").read(), data)
            self.assertEqual(sorted(os.listdir(tmp)), sorted(["image_01.jpg", "image_02.jpg",
                                                              os.path.basename(samples[0]),
                                                              os.path.basename(samples[1])]))   # 无 .tmp 残留
        for folder, snap in before.items():                      # 源目录：条目一字未动（含无 Thumbs.db）
            self.assertEqual(self.snapshot(folder), snap)

    def test_real_video_rejects_truncated_file(self):
        samples = self._samples()
        if samples is None or not video_thumb.available():
            self.skipTest("真实库实测需 CIVITAI_TEST_REAL_VIDEO_DIR 与已编译的原生 DLL")
        with tempfile.TemporaryDirectory() as tmp:
            trunc = os.path.join(tmp, "trunc.mp4")
            with open(samples[0], "rb") as f:
                with open(trunc, "wb") as g:
                    g.write(f.read(2048))
            out = os.path.join(tmp, "image_01.jpg")
            self.assertFalse(video_thumb.first_frame(trunc, out))
            self.assertFalse(os.path.exists(out))
            self.assertTrue(video_thumb.last_error())


if __name__ == "__main__":
    unittest.main()
