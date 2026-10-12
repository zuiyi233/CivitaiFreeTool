"""media_meta 单测：PNG/mp4 字节 fixture 全部在测试里手写，不联网、不依赖 ffmpeg。"""
import io
import json
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import media_meta

PNG_SIG = b"\x89PNG\r\n\x1a\n"
CMT = b"\xa9cmt"
DES = b"\xa9des"
PROMPT = b"prom"


# --------------------------------------------------------------------- PNG 构造器

def chunk(typ, data=b""):
    """PNG 块：长度 + 类型 + 数据 + CRC32。"""
    return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)


def texp(keyword, value):
    return chunk(b"tEXt", keyword.encode("latin-1") + b"\x00" + value.encode("latin-1"))


def ztxtp(keyword, value):
    return chunk(b"zTXt", keyword.encode("latin-1") + b"\x00" + b"\x00" + zlib.compress(value.encode("utf-8")))


def itxt(keyword, value, flags=0):
    payload = value.encode("utf-8")
    if flags:
        payload = zlib.compress(payload)
    body = keyword.encode("latin-1") + b"\x00" + bytes([flags, 0]) + b"\x00" + b"\x00" + payload
    return chunk(b"iTXt", body)


def png_bytes(chunks, size=(8, 6)):
    body = chunk(b"IHDR", struct.pack(">IIBBBBB", size[0], size[1], 8, 2, 0, 0, 0))
    scanline = b"\x00" + b"\xff\x00\x00" * size[0]
    body += chunk(b"IDAT", zlib.compress(scanline * size[1]))
    for item in chunks:
        body += item
    return PNG_SIG + body + chunk(b"IEND")


# --------------------------------------------------------------------- mp4 构造器

def box(typ, payload=b""):
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def ftyp():
    return box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isomiso2avc1mp41")


def mdat(size=400):
    return box(b"mdat", b"\x11" * size)


def mvhd(duration=96000, timescale=1000, version=0):
    if version == 1:
        head = b"\x01\x00\x00\x00" + struct.pack(">QQ", 0, 0) + struct.pack(">IQ", timescale, duration)
    else:
        head = b"\x00\x00\x00\x00" + struct.pack(">IIII", 0, 0, timescale, duration)
    return box(b"mvhd", head)


def tkhd(width=640, height=360, version=0):
    if version == 1:
        body = (b"\x01\x00\x00\x07" + struct.pack(">QQIIQ", 0, 0, 1, 0, 96) + b"\x00" * 8 +
                struct.pack(">HHHH", 0, 0, 0x0100, 0) + b"\x00" * 36 + struct.pack(">II", width << 16, height << 16))
    else:
        body = (b"\x00\x00\x00\x07" + struct.pack(">IIIII", 0, 0, 1, 0, 96) + b"\x00" * 8 +
                struct.pack(">HHHH", 0, 0, 0x0100, 0) + b"\x00" * 36 + struct.pack(">II", width << 16, height << 16))
    return box(b"tkhd", body)


def mdhd(timescale=24000, version=0):
    """mdhd 时间基：v0 的 timescale 在第 12 字节，v1 的 creation/modification 是 64 位。"""
    if version == 1:
        head = b"\x01\x00\x00\x00" + struct.pack(">QQ", 0, 0) + struct.pack(">I", timescale) + struct.pack(">Q", 0)
    else:
        head = b"\x00\x00\x00\x00" + struct.pack(">III", 0, 0, timescale) + struct.pack(">I", 0)
    return box(b"mdhd", head + b"\x00" * 4)


def hdlr(handler=b"vide"):
    """hdlr：ver/flags(4) + pre-defined(4) + handler_type(4)，fps 只认视频轨。"""
    return box(b"hdlr", b"\x00" * 8 + handler + b"\x00" * 5)


def stts(entries):
    body = b"\x00" * 4 + struct.pack(">I", len(entries))
    for count, delta in entries:
        body += struct.pack(">II", count, delta)
    return box(b"stts", body)


def video_trak(width=1024, height=576, timescale=24000, mdhd_version=0, entries=((240, 1001),),
               handler=b"vide"):
    """tkhd + mdia(mdhd/hdlr/minf/stbl(stts))：契约 §15.1.5 的 fps 来源。"""
    media = mdhd(timescale, mdhd_version) + hdlr(handler)
    if entries:
        media += box(b"minf", box(b"stbl", stts(entries)))
    return box(b"trak", tkhd(width, height) + box(b"mdia", media))


def itunes_data(text, type_flag=1):
    """ilst 项的 data 子盒：size+'data'+version/flags+locale 共 16 字节头。"""
    return box(b"data", struct.pack(">II", type_flag << 24, 0) + text.encode("utf-8"))


def ilst_comment(text, key=CMT, type_flag=1):
    return box(b"meta", b"\x00" * 4 + box(b"hdlr", b"\x00" * 4 + b"mdta" + b"\x00" * 9) +
               box(b"ilst", box(key, itunes_data(text, type_flag))))


def udta_ilst(text, key=CMT):
    return box(b"udta", ilst_comment(text, key))


def udta_plain(typ, text):
    """QuickTime 风格：udta 直接子盒，4 字节 version/flags 后接文本。"""
    return box(b"udta", box(typ, b"\x00" * 4 + text.encode("utf-8")))


def moov(children, udta=None, track_udta=None):
    body = mvhd()
    trak = box(b"trak", tkhd() + (track_udta or b""))
    return box(b"moov", body + trak + (udta or b"") + b"".join(children))


def mp4_bytes(*boxes):
    return b"".join(boxes)


# ------------------------------------------------------------------- 图 fixture

def api_graph(prompt_text="1girl, solo", negative="lowres, bad", seed=1234, ckpt="SD/animagineXL.safetensors",
              lora="loras\\detail_lora.safetensors", width=1024, height=576):
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "2": {"class_type": "LoraLoader", "inputs": {"lora_name": lora, "strength_model": 0.8,
                                                     "model": ["1", 0], "clip": ["1", 1]}},
        "3": {"class_type": "KSampler", "inputs": {"seed": seed, "steps": 25, "cfg": 6.5,
                                                   "sampler_name": "dpmpp_2m", "scheduler": "karras",
                                                   "denoise": 1.0, "model": ["2", 0], "positive": ["45", 0],
                                                   "negative": ["8", 0], "latent_image": ["5", 0]}},
        "45": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt_text, "clip": ["2", 1]}},
        "8": {"class_type": "CLIPTextEncode", "inputs": {"text": negative, "clip": ["2", 1]}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
    }


def ui_graph(prompt_text="city skyline at dusk"):
    """只有 UI 工作流：单一 CLIPTextEncode，正负用途无法核实。"""
    return {"nodes": [{"id": 1, "type": "CheckpointLoaderSimple", "mode": 0,
                       "widgets_values": ["animagineXL.safetensors"]},
                      {"id": 2, "type": "LoraLoader", "mode": 0,
                       "widgets_values": ["detail_lora.safetensors", 0.8, 0.8]},
                      {"id": 45, "type": "CLIPTextEncode", "mode": 0, "widgets_values": [prompt_text]}],
            "links": [], "groups": []}


def tiny_ui_graph(prompt_text="small one"):
    """能塞进小块上限的 UI 工作流。"""
    return {"nodes": [{"id": 1, "type": "CLIPTextEncode", "mode": 0, "widgets_values": [prompt_text]}]}


def comment_json(prompt=None, workflow=None):
    payload = {}
    if prompt is not None:
        payload["prompt"] = json.dumps(prompt, ensure_ascii=False)
    if workflow is not None:
        payload["workflow"] = json.dumps(workflow, ensure_ascii=False)
    return json.dumps(payload, ensure_ascii=False)


def h3_api_graph(prompt_text="a girl dancing, 1girl"):
    """真实 ComfyUI 输出形状：SamplerCustomAdvanced + BasicGuider.conditioning 来自自定义节点，
    没有 KSampler/positive 字段，提示词与底模只存在于标准字段名（prompt/unet_name/turbo_lora）。"""
    return {
        "287": {"class_type": "SamplerCustomAdvanced",
                "inputs": {"noise": ["90", 0], "guider": ["299", 0], "sampler": ["91", 0],
                           "sigmas": ["92", 0], "latent_image": ["93", 0]}},
        "299": {"class_type": "BasicGuider", "inputs": {"model": ["999", 0], "conditioning": ["316", 0]}},
        "316": {"class_type": "MiniMaxH3ReferenceToVideo",
                "inputs": {"clip": ["13", 0], "vae": ["97", 0], "prompt": prompt_text,
                           "width": 1024, "height": 576}},
        "13": {"class_type": "CLIPLoader",
               "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_int8_convrot.safetensors", "type": "minimax"}},
        "999": {"class_type": "H3VMMasterLoader",
                "inputs": {"unet_name": "models\\unet\\minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors",
                           "turbo_lora": "loras/minimax_h3_fl2v_turbo_8step.safetensors",
                           "lora_strength": 1.0, "gpu_mode": "high"}},
        "90": {"class_type": "RandomNoise", "inputs": {"noise_seed": 777}},
        "91": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "92": {"class_type": "BasicScheduler", "inputs": {"scheduler": "simple", "steps": 8, "denoise": 1.0}},
        "93": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 576, "batch_size": 1}},
    }


def custom_param_api_graph(seed=2026081201, steps="8", cfg=1.0, sampler=" Euler ", scheduler="beta57"):
    """SamplerCustomAdvanced 全家桶：参数只散落在自定义节点的标准字段名里，
    链式回溯一个都拿不到（真实库里 1676 条就是这个形状）。字段名故意混大小写与空格。"""
    return {
        "1": {"class_type": "SamplerCustomAdvanced",
              "inputs": {"noise": ["2", 0], "guider": ["3", 0], "sampler": ["4", 0],
                         "sigmas": ["5", 0], "latent_image": ["7", 0]}},
        "2": {"class_type": "RandomNoise", "inputs": {" NOISE_SEED ": seed}},
        "3": {"class_type": "CFGGuider", "inputs": {"model": ["6", 0], "positive": ["8", 0],
                                                    "negative": ["9", 0], "cfg": cfg}},
        "4": {"class_type": "KSamplerSelect", "inputs": {"SAMPLER_NAME": sampler}},
        "5": {"class_type": "CustomSigmas", "inputs": {"num_steps": steps, "Scheduler": scheduler}},
        "6": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "models/unet/qwen_fp8.safetensors"}},
        "7": {"class_type": "EmptyLatentImage", "inputs": {"width": 768, "height": 512, "batch_size": 1}},
        "8": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat"}},
        "9": {"class_type": "CLIPTextEncode", "inputs": {"text": "lowres"}},
    }


def doc_noise_api_graph(prompt_text="1girl, custom conditioning"):
    """文档/面板类节点的 text/prompt 是备忘文字，不能当提示词回收。"""
    return {
        "1": {"class_type": "Note", "inputs": {"text": "记得把 CFG 调低", "show_source": "None"}},
        "2": {"class_type": "MarkdownNote", "inputs": {"text": "# 工作流说明"}},
        "3": {"class_type": "Text Panel", "inputs": {"prompt": "面板里的备忘文字"}},
        "4": {"class_type": "PrimitiveStringMultiline",
              "inputs": {"value": "primitive 常量", "prompt": "primitive 字段"}},
        "5": {"class_type": "ComfyUI-ExpressionNode", "inputs": {"expression": "1+1", "prompt": "表达式输出"}},
        "6": {"class_type": "AnythingIndex", "inputs": {"prompt": "索引占位"}},
        "7": {"class_type": "Reroute", "inputs": {"flow": ["8", 0], "prompt": "转发占位"}},
        "8": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {"prompt": prompt_text}},
    }


def names(value):
    return sorted(os.path.basename(str(v).replace("\\", "/")) for v in value)


class MediaMetaTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def write(self, name, data):
        path = self.root / name
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_bytes(data)
        return str(path)

    def assert_shape(self, result):
        self.assertEqual(sorted(result), sorted(media_meta.RESULT_KEYS))
        self.assertIsInstance(result["has_meta"], bool)
        self.assertIsInstance(result["source"], str)
        self.assertIsInstance(result["prompt"], str)
        self.assertIsInstance(result["negative"], str)
        self.assertIsInstance(result["note"], str)
        self.assertIsInstance(result["checkpoint"], str)
        self.assertIsInstance(result["loras"], list)
        self.assertIsInstance(result["models"], list)
        self.assertIsInstance(result["extra"], dict)
        for key in ("seed", "steps", "width", "height"):
            self.assertTrue(result[key] is None or isinstance(result[key], int), key)
        self.assertTrue(result["cfg"] is None or isinstance(result["cfg"], float))
        self.assertTrue(result["duration"] is None or isinstance(result["duration"], float))
        self.assertTrue(result["fps"] is None or isinstance(result["fps"], float))
        json.dumps(result, allow_nan=False)


class ReadImageTextChunksTests(MediaMetaTestCase):
    def test_text_ztxt_itxt_all_decoded(self):
        path = self.write("tri.png", png_bytes([
            texp("parameters", "Steps: 20"),
            ztxtp("prompt", json.dumps(api_graph())),
            itxt("workflow", json.dumps(ui_graph()), flags=1)]))
        found = media_meta.read_image_text_chunks(path, keys=("prompt", "workflow", "parameters"))
        self.assertEqual(found["parameters"], "Steps: 20")
        self.assertEqual(json.loads(found["prompt"])["3"]["class_type"], "KSampler")
        self.assertEqual(json.loads(found["workflow"])["nodes"][0]["type"], "CheckpointLoaderSimple")

    def test_itxt_uncompressed_utf8(self):
        path = self.write("i.png", png_bytes([itxt("prompt", "少女, 1girl")]))
        self.assertEqual(media_meta.read_image_text_chunks(path, keys=("prompt",))["prompt"], "少女, 1girl")

    def test_chunks_after_idat_are_read(self):
        """ComfyUI 把 workflow/prompt 写在 IDAT 之后，读到 IDAT 不能停。"""
        path = self.write("after_idat.png", png_bytes([
            texp("prompt", json.dumps(api_graph("late chunk"))),
            ztxtp("workflow", json.dumps(ui_graph()))], size=(64, 64)))
        found = media_meta.read_image_text_chunks(path)
        self.assertIn("prompt", found)
        self.assertIn("workflow", found)
        result = media_meta.extract(path)
        self.assertEqual(result["prompt"], "late chunk")

    def test_stops_early_when_all_keys_found(self):
        path = self.write("early.png", png_bytes([
            texp("prompt", '{"a": 1}'),
            texp("workflow", json.dumps(ui_graph("should not be read")))]))
        found = media_meta.read_image_text_chunks(path, keys=("prompt",))
        self.assertEqual(sorted(found), ["prompt"])

    def test_stops_at_iend(self):
        tail = chunk(b"IEND") + texp("prompt", '{"x": 1}')
        path = self.write("iend.png", png_bytes([texp("workflow", json.dumps(ui_graph())), tail]))
        found = media_meta.read_image_text_chunks(path)
        self.assertNotIn("prompt", found)

    def test_oversized_chunk_skipped_not_fatal(self):
        limit = 160
        big = texp("prompt", json.dumps(api_graph("too big for the chunk cap")))
        path = self.write("big.png", png_bytes([big, texp("workflow", json.dumps(tiny_ui_graph("small one")))]))
        self.assertGreater(len(big), limit + 8)
        found = media_meta.read_image_text_chunks(path, max_chunk_bytes=limit)
        self.assertEqual(sorted(found), ["workflow"])
        result = media_meta.extract(path, max_chunk_bytes=limit)
        self.assertTrue(result["has_meta"])
        self.assertEqual(result["prompt"], "small one")
        self.assertEqual(result["source"], "png_text")

    def test_chunk_count_cap(self):
        filler = [texp("k%02d" % i, "x") for i in range(media_meta.MAX_PNG_CHUNKS + 6)]
        path = self.write("many.png", png_bytes(filler + [texp("prompt", json.dumps(api_graph()))]))
        found = media_meta.read_image_text_chunks(path)
        self.assertNotIn("prompt", found)
        self.assertLessEqual(len(found), media_meta.MAX_PNG_CHUNKS)
        result = media_meta.extract(path)
        self.assertFalse(result["has_meta"])
        self.assert_shape(result)

    def test_first_keyword_wins(self):
        path = self.write("dup.png", png_bytes([texp("prompt", "first"), texp("prompt", "second")],
                                               size=(4, 4)))
        self.assertEqual(media_meta.read_image_text_chunks(path, keys=("prompt",))["prompt"], "first")

    def test_reader_never_reads_whole_file(self):
        """契约 §2.3：流式 seek+read，禁止整文件 read()（IDAT 3MB 也不会被读进来）。"""
        body = (PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 6, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", b"\x00" * (3 * 1024 * 1024))
                + texp("prompt", json.dumps(api_graph("after huge"))) + chunk(b"IEND"))
        path = self.write("huge.png", body)
        sizes = []
        real_open = io.open

        class Spy:
            def __init__(self, handle):
                self._handle = handle

            def read(self, *args):
                sizes.append(args[0] if args else None)
                return self._handle.read(*args)

            def __getattr__(self, name):
                return getattr(self._handle, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._handle.close()

        with patch("builtins.open", side_effect=lambda *a, **k: Spy(real_open(*a, **k))):
            result = media_meta.extract(path)
        self.assertEqual(result["prompt"], "after huge")
        self.assertTrue(sizes)
        self.assertTrue(all(n is not None and n <= 1024 for n in sizes), sorted(set(sizes))[:6])

    def test_large_high_entropy_chunk_is_streamed_and_parsed(self):
        """真实 ComfyUI PNG 的 workflow 块常有几 MB：按块读，不整文件读。"""
        blob = os.urandom(600_000).hex()          # 高熵负载，zlib 压不动
        heavy = ui_graph(blob)
        text = json.dumps(heavy)
        path = self.write("heavy.png", png_bytes([ztxtp("workflow", text)], size=(4, 4)))
        self.assertGreater(os.path.getsize(path), 500_000)
        found = media_meta.read_image_text_chunks(path)
        self.assertEqual(found["workflow"], text)
        result = media_meta.extract(path)
        self.assertTrue(result["has_meta"])
        self.assertEqual(result["extra"]["graph"], "ui")
        self.assertEqual(result["prompt"], blob)

    def test_bogus_length_does_not_raise(self):
        bad = struct.pack(">I4s", 0xFFFFFFFF, b"tEXt") + b"\x00" * 4
        path = self.write("bad.png", png_bytes([bad, texp("workflow", json.dumps(ui_graph()))]))
        result = media_meta.extract(path)
        self.assert_shape(result)

    def test_not_a_png(self):
        path = self.write("fake.png", b"\x89PNJ" + b"\x00" * 40)
        self.assertEqual(media_meta.read_image_text_chunks(path), {})


class ExtractPngTests(MediaMetaTestCase):
    def test_api_graph_fields(self):
        path = self.write("api.png", png_bytes([texp("prompt", json.dumps(api_graph())),
                                                ztxtp("workflow", json.dumps(ui_graph()))]))
        r = media_meta.extract(path)
        self.assert_shape(r)
        self.assertTrue(r["has_meta"])
        self.assertEqual(r["source"], "png_text")
        self.assertEqual(r["prompt"], "1girl, solo")
        self.assertEqual(r["negative"], "lowres, bad")
        self.assertEqual(r["checkpoint"], "animagineXL.safetensors")
        self.assertEqual(r["loras"], ["detail_lora.safetensors"])
        self.assertEqual(names(r["models"]), ["animagineXL.safetensors", "detail_lora.safetensors"])
        self.assertEqual((r["seed"], r["steps"], r["cfg"]), (1234, 25, 6.5))
        self.assertEqual(r["sampler"], "dpmpp_2m")
        self.assertEqual(r["scheduler"], "karras")
        self.assertEqual((r["width"], r["height"]), (1024, 576))
        self.assertEqual(r["extra"]["graph"], "api")
        self.assertEqual(r["extra"]["ui_nodes"], 3)
        self.assertEqual(sorted(r["extra"]["chunk_keys"]), ["prompt", "workflow"])
        self.assertEqual(r["extra"]["generation"], {"denoise": 1.0})

    def test_ui_only_workflow_falls_back(self):
        path = self.write("ui.png", png_bytes([ztxtp("workflow", json.dumps(ui_graph("dusk night")))]))
        r = media_meta.extract(path)
        self.assert_shape(r)
        self.assertTrue(r["has_meta"])
        self.assertEqual(r["prompt"], "dusk night")
        self.assertEqual(r["extra"]["graph"], "ui")
        self.assertIn("animagineXL.safetensors", names(r["models"]))
        self.assertEqual(r["checkpoint"], "")
        self.assertEqual(r["loras"], [])
        self.assertIn("UI 工作流", r["note"])

    def test_dimensions_fall_back_to_image_header(self):
        path = self.write("hdr.png", png_bytes([texp("workflow", json.dumps({"nodes": []}))], size=(40, 24)))
        r = media_meta.extract(path)
        self.assertEqual((r["width"], r["height"]), (40, 24))
        self.assertTrue(r["has_meta"])

    def test_plain_text_prompt_chunk_is_kept_as_text(self):
        path = self.write("plain.png", png_bytes([texp("prompt", "a very old sd checkpoint note")]))
        r = media_meta.extract(path)
        self.assertTrue(r["has_meta"])
        self.assertEqual(r["prompt"], "a very old sd checkpoint note")
        self.assertEqual(r["source"], "png_text")

    def test_nan_in_json_does_not_raise(self):
        raw = '{"1": {"class_type": "KSampler", "inputs": {"seed": NaN, "cfg": Infinity, "positive": "x"}}}'
        path = self.write("nan.png", png_bytes([texp("prompt", raw)]))
        r = media_meta.extract(path)
        self.assert_shape(r)
        self.assertTrue(r["has_meta"])
        self.assertIsNone(r["seed"])
        self.assertIsNone(r["cfg"])

    def test_non_dict_json_does_not_raise(self):
        for payload in ('["just", "a", "list"]', '"bare string"', '{"nothing": "relevant"}'):
            path = self.write("junk.png", png_bytes([texp("prompt", payload)]))
            r = media_meta.extract(path)
            self.assert_shape(r)
            self.assertEqual(r["checkpoint"], "")

    def test_corrupt_png_bytes(self):
        path = self.write("corrupt.png", PNG_SIG + os.urandom(2048))
        r = media_meta.extract(path)
        self.assert_shape(r)
        self.assertFalse(r["has_meta"])
        self.assertEqual(r["source"], "none")

    def test_truncated_png(self):
        data = png_bytes([ztxtp("workflow", json.dumps(ui_graph()))], size=(64, 64))
        path = self.write("cut.png", data[: len(data) // 3])
        r = media_meta.extract(path)
        self.assert_shape(r)


class ExtractImageFallbackTests(MediaMetaTestCase):
    def test_exif_user_comment_json(self):
        from PIL import Image
        exif = Image.Exif()
        exif[0x9286] = "ASCII\x00\x00\x00" + json.dumps({"prompt": "1girl, exif comment"})
        for fmt, name, src in (("JPEG", "c.jpg", "jpeg_exif"), ("WEBP", "c.webp", "webp_exif")):
            buf = io.BytesIO()
            Image.new("RGB", (12, 8)).save(buf, fmt, exif=exif.tobytes())
            path = self.write(name, buf.getvalue())
            r = media_meta.extract(path)
            self.assert_shape(r)
            self.assertTrue(r["has_meta"], name)
            self.assertEqual(r["source"], src, name)
            self.assertEqual(r["prompt"], "1girl, exif comment", name)
            self.assertEqual((r["width"], r["height"]), (12, 8), name)

    def test_gif_is_indexed_but_not_parsed(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("P", (6, 4)).save(buf, "GIF")
        path = self.write("a.gif", buf.getvalue())
        r = media_meta.extract(path)
        self.assert_shape(r)
        self.assertFalse(r["has_meta"])
        self.assertEqual(r["source"], "none")
        self.assertEqual((r["width"], r["height"]), (6, 4))


class ReadMp4MetaTests(MediaMetaTestCase):
    def test_moov_after_mdat(self):
        text = comment_json(prompt=api_graph("video prompt"))
        data = mp4_bytes(ftyp(), mdat(512), moov([], udta=udta_ilst(text)))
        path = self.write("head.mp4", data)
        meta = media_meta.read_mp4_meta(path)
        self.assertEqual(sorted(meta), sorted(("comment", "description", "prompt", "workflow",
                                               "duration", "width", "height", "fps")))
        self.assertEqual(meta["comment"], text)
        self.assertEqual(meta["duration"], 96.0)
        self.assertIsNone(meta["fps"])
        self.assertEqual((meta["width"], meta["height"]), (640, 360))

    def test_track_level_udta_and_description(self):
        data = mp4_bytes(ftyp(), mdat(64), moov([], track_udta=udta_ilst("track text", key=DES)))
        meta = media_meta.read_mp4_meta(self.write("track.mov", data))
        self.assertEqual(meta["description"], "track text")

    def test_container_wins_over_track(self):
        data = mp4_bytes(ftyp(), mdat(64),
                         moov([], udta=udta_plain(CMT, "container text"),
                              track_udta=udta_plain(CMT, "track text")))
        self.assertEqual(media_meta.read_mp4_meta(self.write("prio.mp4", data))["comment"], "container text")

    def test_standalone_prompt_atom(self):
        data = mp4_bytes(ftyp(), mdat(32), moov([], udta=box(b"udta", box(PROMPT, b"\x00" * 4 + b'{"1": {"class_type": "KSampler", "inputs": {"seed": 9}}}'))))
        meta = media_meta.read_mp4_meta(self.write("solo.mp4", data))
        self.assertIn("KSampler", meta["prompt"])

    def test_binary_cover_art_is_ignored(self):
        data = mp4_bytes(ftyp(), mdat(32), moov([], udta=box(b"udta", box(b"ilst", box(CMT, itunes_data("\u00ff\u0000junk", type_flag=13))))))
        self.assertIsNone(media_meta.read_mp4_meta(self.write("cover.mp4", data))["comment"])

    def test_missing_moov(self):
        path = self.write("nomoov.mp4", mp4_bytes(ftyp(), mdat(256)))
        info = {}
        meta = media_meta.read_mp4_meta(path, info=info)
        self.assertFalse(info["moov_found"])
        self.assertIsNone(meta["duration"])

    def test_mvhd_version_one(self):
        body = box(b"moov", mvhd(duration=150, timescale=30, version=1))
        meta = media_meta.read_mp4_meta(self.write("v1.mp4", mp4_bytes(ftyp(), body)))
        self.assertAlmostEqual(meta["duration"], 5.0)

    def test_tkhd_version_one(self):
        body = box(b"moov", mvhd() + box(b"trak", tkhd(720, 1280, version=1)))
        meta = media_meta.read_mp4_meta(self.write("v1t.mp4", mp4_bytes(ftyp(), body)))
        self.assertEqual((meta["width"], meta["height"]), (720, 1280))


class Mp4FpsTests(MediaMetaTestCase):
    """契约 §15.1.5：fps = stts 采样数合计 / (delta 合计 / mdhd 时间基)；拿不到就 None，绝不默认 30。"""

    def meta(self, name, *boxes):
        return media_meta.read_mp4_meta(self.write(name, mp4_bytes(*boxes)))

    def test_23_976_non_integer_fps(self):
        """24000/1001 的 23.976 是最常见的 ntsc 帧率：3 位小数保留，不四舍五入成 24。"""
        meta = self.meta("fps.mp4", ftyp(), mdat(64), moov([video_trak()]))
        self.assertEqual(meta["fps"], 23.976)

    def test_mdhd_version_one_timescale(self):
        meta = self.meta("fpsv1.mp4", ftyp(), moov([video_trak(timescale=1000, mdhd_version=1,
                                                              entries=((300, 33),))]))
        self.assertEqual(meta["fps"], 30.303)

    def test_stts_entries_sum_counts_and_ticks(self):
        """两条 entry：采样数 10+20，tick 是 10*100 + 20*50 = 2000 → 30 帧 / 2 秒 = 15fps。"""
        meta = self.meta("multi.mp4", ftyp(), moov([video_trak(timescale=1000,
                                                              entries=((10, 100), (20, 50)))]))
        self.assertEqual(meta["fps"], 15.0)

    def test_rounded_to_three_decimals(self):
        meta = self.meta("round.mp4", ftyp(), moov([video_trak(timescale=30000, entries=((100, 1001),))]))
        self.assertEqual(meta["fps"], 29.97)

    def test_missing_stts_is_none(self):
        meta = self.meta("nostts.mp4", ftyp(), moov([video_trak(entries=())]))
        self.assertIsNone(meta["fps"])
        self.assertIsNone(media_meta.extract(self.write("nostts2.mp4", mp4_bytes(
            ftyp(), mdat(64), moov([video_trak(entries=())]))))["fps"])

    def test_audio_track_is_not_mistaken_for_video(self):
        """只有声音的轨（tkhd 宽高 0 + hdlr 'soun'）不算 fps，哪怕 mdhd/stts 齐备。"""
        meta = self.meta("aud.mp4", ftyp(), box(b"moov", mvhd() + video_trak(
            width=0, height=0, handler=b"soun", timescale=44100, entries=((441, 1024),))))
        self.assertIsNone(meta["fps"])
        self.assertIsNone(meta["width"])

    def test_absurd_stts_entry_count_is_refused(self):
        garbage = box(b"stts", b"\x00" * 4 + struct.pack(">I", 0xFFFFFFFF) + b"\x00" * 16)
        trak = box(b"trak", tkhd() + box(b"mdia", mdhd(1000) + hdlr() + box(b"minf", box(b"stbl", garbage))))
        self.assertIsNone(self.meta("junk.mp4", ftyp(), box(b"moov", mvhd() + trak))["fps"])

    def test_extract_carries_fps_and_dims(self):
        data = mp4_bytes(ftyp(), mdat(512), moov([video_trak()], udta=udta_ilst(
            comment_json(prompt=custom_param_api_graph()))))
        r = media_meta.extract(self.write("full.mp4", data))
        self.assert_shape(r)
        self.assertEqual(r["fps"], 23.976)
        self.assertEqual(r["duration"], 96.0)

    def test_fps_from_moov_in_tail_window(self):
        """真实 SaveVideo 没有 faststart：moov 落在尾窗口也要算得出 fps。"""
        body = moov([video_trak()])
        path = self.write("tailfps.mp4", mp4_bytes(ftyp(), mdat(4096), body))
        r = media_meta.extract(path, max_window_bytes=len(body) + 16)
        self.assertEqual(r["fps"], 23.976)
        self.assertEqual(r["extra"]["mp4"]["window"], "head+tail")
        self.assertEqual(r["extra"]["mp4"]["moov_at"], "tail")

    def test_image_result_fps_is_none(self):
        path = self.write("still.png", png_bytes([texp("prompt", json.dumps(api_graph()))]))
        self.assertIsNone(media_meta.extract(path)["fps"])


class ExtractVideoTests(MediaMetaTestCase):
    def test_merged_comment_json(self):
        text = comment_json(prompt=api_graph("merged prompt", seed=777), workflow=ui_graph("merged wf"))
        path = self.write("merged.mp4", mp4_bytes(ftyp(), mdat(600), moov([], udta=udta_ilst(text))))
        r = media_meta.extract(path)
        self.assert_shape(r)
        self.assertTrue(r["has_meta"])
        self.assertEqual(r["source"], "mp4_udta")
        self.assertEqual(r["prompt"], "merged prompt")
        self.assertEqual(r["seed"], 777)
        self.assertEqual(r["checkpoint"], "animagineXL.safetensors")
        self.assertEqual((r["width"], r["height"]), (1024, 576))
        self.assertEqual(r["duration"], 96.0)
        self.assertEqual(r["extra"]["mp4"]["window"], "whole")
        self.assertTrue(r["extra"]["mp4"]["moov_found"])

    def test_moov_at_tail_is_found(self):
        """ffmpeg/ComfyUI SaveVideo 的 moov 写在 mdat 之后（没有 faststart）。"""
        text = comment_json(prompt=api_graph("tail prompt", seed=42), workflow=tiny_ui_graph("tail wf"))
        body = moov([], udta=udta_ilst(text))
        path = self.write("tail.mp4", mp4_bytes(ftyp(), mdat(4096), body))
        window = len(body) + 16
        self.assertLess(window, 4096, "fixture 需要真的走到首尾窗口分支")
        r = media_meta.extract(path, max_window_bytes=window)
        self.assert_shape(r)
        self.assertTrue(r["has_meta"])
        self.assertEqual(r["source"], "mp4_udta")
        self.assertEqual(r["prompt"], "tail prompt")
        self.assertEqual(r["seed"], 42)
        self.assertEqual(r["checkpoint"], "animagineXL.safetensors")
        self.assertEqual(r["duration"], 96.0)
        self.assertEqual((r["width"], r["height"]), (1024, 576))
        self.assertEqual(r["extra"]["mp4"]["window"], "head+tail")
        self.assertEqual(r["extra"]["mp4"]["moov_at"], "tail")
        self.assertEqual(sorted(r["extra"]["mp4"]["tags"]), ["comment"])

    def test_track_only_udta_still_parsed(self):
        text = comment_json(prompt={"1": {"class_type": "KSampler", "inputs": {"seed": 8}}})
        path = self.write("trk.mp4", mp4_bytes(ftyp(), mdat(64), moov([], track_udta=udta_ilst(text))))
        r = media_meta.extract(path)
        self.assertTrue(r["has_meta"])
        self.assertEqual(r["seed"], 8)
        self.assertEqual(r["extra"]["mp4"]["moov_found"], True)

    def test_head_window_only_when_faststart(self):
        text = comment_json(prompt={"1": {"class_type": "KSampler", "inputs": {"seed": 5}}})
        head = moov([], udta=udta_plain(CMT, text))
        path = self.write("fast.mp4", mp4_bytes(ftyp(), head, mdat(4096)))
        r = media_meta.extract(path, max_window_bytes=len(ftyp()) + len(head) + 8)
        self.assertTrue(r["has_meta"])
        self.assertEqual(r["seed"], 5)
        self.assertEqual(r["duration"], 96.0)
        self.assertEqual(r["extra"]["mp4"]["window"], "head+tail")
        self.assertEqual(r["extra"]["mp4"]["moov_at"], "head")

    def test_non_json_comment_is_notified_not_used(self):
        path = self.write("plain.mp4", mp4_bytes(ftyp(), mdat(64), moov([], udta=udta_ilst("just prose"))))
        r = media_meta.extract(path)
        self.assertFalse(r["has_meta"])
        self.assertEqual(r["source"], "none")
        self.assertIn("不是 JSON", r["note"])
        self.assertEqual(r["duration"], 96.0)
        self.assertEqual((r["width"], r["height"]), (640, 360))

    def test_video_without_metadata(self):
        path = self.write("bare.mp4", mp4_bytes(ftyp(), mdat(128), moov([])))
        r = media_meta.extract(path)
        self.assert_shape(r)
        self.assertFalse(r["has_meta"])
        self.assertEqual(r["source"], "none")
        self.assertTrue(r["extra"]["mp4"]["moov_found"])
        self.assertEqual(r["duration"], 96.0)

    def test_garbage_and_truncated_video_never_raise(self):
        for name, data in (("g1.mp4", os.urandom(4096)),
                           ("g2.mp4", b"\x00\x00\x00\x18ftyp" + os.urandom(64)),
                           ("g3.mov", mp4_bytes(ftyp(), mdat(64), moov([], udta=udta_ilst('{"prompt": '))[:80]))):
            r = media_meta.extract(self.write(name, data))
            self.assert_shape(r)
            self.assertFalse(r["has_meta"], name)

    def test_oversized_moov_declaration_stops_window(self):
        fake = struct.pack(">I", 0x7FFFFFFF) + b"moov" + b"\x00" * 64
        r = media_meta.extract(self.write("huge.mp4", ftyp() + mdat(64) + fake + mvhd()))
        self.assert_shape(r)

    def test_webm_is_indexed_only(self):
        path = self.write("clip.webm", b"\x1a\x45\xdf\xa3" + os.urandom(300))
        r = media_meta.extract(path)
        self.assert_shape(r)
        self.assertFalse(r["has_meta"])
        self.assertEqual(r["extra"]["ebml"], "v1 不解析 EBML")


class FieldFallbackTests(MediaMetaTestCase):
    """字段名回退：链条走不到时按标准字段名回收 prompt/底模/LoRA，并记录来源。"""

    def test_h3_custom_conditioning_recovers_everything(self):
        path = self.write("h3.png", png_bytes([texp("prompt", json.dumps(h3_api_graph()))]))
        r = media_meta.extract(path)
        self.assert_shape(r)
        self.assertTrue(r["has_meta"])
        self.assertEqual(r["prompt"], "a girl dancing, 1girl")
        self.assertEqual(r["negative"], "")          # 回退只还原文本，不发明负向
        self.assertEqual(r["checkpoint"], "minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors")
        self.assertEqual(r["loras"], ["minimax_h3_fl2v_turbo_8step.safetensors"])
        self.assertIn("minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors", names(r["models"]))
        self.assertEqual(r["extra"]["prompt_source"], "name_fallback")
        self.assertEqual(r["extra"]["classification_source"], "field_name")
        self.assertIn("字段名", r["note"])
        # 参数按字段名回收（契约 §15.1）：noise_seed/steps/sampler_name/scheduler 都是标准字段名。
        self.assertEqual(r["seed"], 777)
        self.assertEqual(r["steps"], 8)
        self.assertEqual((r["sampler"], r["scheduler"]), ("euler", "simple"))
        self.assertIsNone(r["cfg"])                  # 图里没有任何 cfg 字段名，不臆造
        self.assertEqual(r["extra"]["params_source"], "field_name")
        self.assertEqual(r["extra"]["params_fields"], ["noise_seed", "sampler_name", "scheduler", "steps"])
        self.assertEqual((r["width"], r["height"]), (1024, 576))

    def test_chain_result_wins_when_sampler_chain_resolves(self):
        graph = api_graph("chain prompt")
        graph["99"] = {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "unused/other.safetensors"}}
        path = self.write("chain.png", png_bytes([texp("prompt", json.dumps(graph))]))
        r = media_meta.extract(path)
        self.assertEqual(r["checkpoint"], "animagineXL.safetensors")
        self.assertEqual(r["loras"], ["detail_lora.safetensors"])
        self.assertNotIn("other.safetensors", r["checkpoint"] + "".join(r["loras"]))
        self.assertEqual(r["extra"]["classification_source"], "chain")
        self.assertEqual(r["extra"]["prompt_source"], "chain")

    def test_document_node_text_is_not_prompt(self):
        path = self.write("docs.png", png_bytes([texp("prompt", json.dumps(doc_noise_api_graph()))]))
        r = media_meta.extract(path)
        self.assertEqual(r["prompt"], "1girl, custom conditioning")
        for junk in ("记得把 CFG 调低", "# 工作流说明", "面板里的备忘文字", "primitive 字段",
                     "表达式输出", "索引占位", "转发占位"):
            self.assertNotIn(junk, r["prompt"])

    def test_fallback_dedupes_joins_and_caps(self):
        graph = {
            "1": {"class_type": "CustomGenA", "inputs": {"prompt": "aaa"}},
            "2": {"class_type": "CustomGenB", "inputs": {"text": "aaa", "caption": "bbb"}},
            "3": {"class_type": "CustomGenC", "inputs": {"positive_text": "c" * 5000}},
        }
        path = self.write("join.png", png_bytes([texp("prompt", json.dumps(graph))]))
        r = media_meta.extract(path)
        self.assertEqual(r["prompt"][:9], "aaa | bbb")
        self.assertEqual(len(r["prompt"]), media_meta.MAX_FALLBACK_PROMPT_CHARS)

    def test_mp4_comment_payload_uses_field_fallback(self):
        """SaveVideo 写的 {"prompt": "<escaped JSON>"} 同样走回退，不回归原 comment 通路。"""
        text = comment_json(prompt=h3_api_graph("video girl dancing"))
        path = self.write("h3.mp4", mp4_bytes(ftyp(), mdat(512), moov([], udta=udta_ilst(text))))
        r = media_meta.extract(path)
        self.assert_shape(r)
        self.assertEqual(r["source"], "mp4_udta")
        self.assertEqual(r["prompt"], "video girl dancing")
        self.assertEqual(r["checkpoint"], "minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors")
        self.assertEqual(r["extra"]["prompt_source"], "name_fallback")
        self.assertEqual(r["extra"]["classification_source"], "field_name")

    def test_sidecar_api_graph_uses_field_fallback(self):
        png = self.write("h3sc.png", png_bytes([]))
        (self.root / "h3sc.json").write_text(json.dumps({"prompt": h3_api_graph("sidecar h3")}),
                                             encoding="utf-8")
        r = media_meta.extract(png)
        self.assertEqual(r["source"], "sidecar")
        self.assertEqual(r["prompt"], "sidecar h3")
        self.assertEqual(r["extra"]["prompt_source"], "name_fallback")

    def test_ui_only_graph_gets_no_field_source_claim(self):
        path = self.write("uih3.png", png_bytes([ztxtp("workflow", json.dumps(ui_graph("dusk")))]))
        r = media_meta.extract(path)
        self.assertNotIn("classification_source", r["extra"])
        self.assertNotIn("prompt_source", r["extra"])

    def test_empty_fallback_does_not_emit_sources(self):
        graph = {"1": {"class_type": "KSampler", "inputs": {"seed": 3}}}
        path = self.write("bare.png", png_bytes([texp("prompt", json.dumps(graph))]))
        r = media_meta.extract(path)
        self.assert_shape(r)
        self.assertEqual(r["checkpoint"], "")
        self.assertEqual(r["prompt"], "")
        self.assertNotIn("classification_source", r["extra"])
        self.assertNotIn("prompt_source", r["extra"])


class ParamFieldFallbackTests(MediaMetaTestCase):
    """契约 §15.1：参数（seed/steps/cfg/sampler/scheduler）按字段名回收 —— 链式优先、值域校验、
    多值不选边，来源如实写进 extra.params_source / params_fields。"""

    def params(self, graph, name="p.png"):
        path = self.write(name, png_bytes([texp("prompt", json.dumps(graph))]))
        return media_meta.extract(path)

    def test_custom_node_params_recovered_by_field_name(self):
        path = self.write("params.png", png_bytes([texp("prompt", json.dumps(custom_param_api_graph()))]))
        r = media_meta.extract(path)
        self.assert_shape(r)
        self.assertTrue(r["has_meta"])
        self.assertEqual((r["seed"], r["steps"], r["cfg"]), (2026081201, 8, 1.0))
        self.assertEqual((r["sampler"], r["scheduler"]), ("Euler", "beta57"))
        self.assertEqual((r["width"], r["height"]), (768, 512))
        self.assertEqual(r["extra"]["params_source"], "field_name")
        self.assertEqual(r["extra"]["params_fields"],
                         ["cfg", "noise_seed", "num_steps", "sampler_name", "scheduler"])

    def test_mixed_case_and_padded_field_names_match(self):
        graph = {"1": {"class_type": "CustomNoise", "inputs": {"Rand_Seed": 5, "STEP_COUNT": 7,
                                                              "GUIDANCE_SCALE": 3.5, "SOLVER": "lms",
                                                              "Schedule": "karras"}}}
        r = self.params(graph, "case.png")
        self.assertEqual((r["seed"], r["steps"], r["cfg"]), (5, 7, 3.5))
        self.assertEqual((r["sampler"], r["scheduler"]), ("lms", "karras"))

    def test_conflicting_values_are_left_blank_and_noted(self):
        graph = {
            "1": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["2", 0], "guider": ["a", 0]}},
            "2": {"class_type": "RandomNoise", "inputs": {"noise_seed": 111}},
            "3": {"class_type": "Seed (rgthree)", "inputs": {"seed": 222}},
            "4": {"class_type": "BasicScheduler", "inputs": {"steps": 4, "scheduler": "simple"}},
            "5": {"class_type": "H3VMMasterLoader", "inputs": {"steps": "10", "scheduler": "beta57"}},
            "6": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        }
        r = self.params(graph, "conflict.png")
        self.assert_shape(r)
        self.assertIsNone(r["seed"])
        self.assertIsNone(r["steps"])
        self.assertEqual(r["scheduler"], "")
        self.assertEqual(r["sampler"], "euler")
        for text in ("工作流里有 2 个不同的 seed，未按字段名猜测。",
                     "工作流里有 2 个不同的 steps，未按字段名猜测。",
                     "工作流里有 2 个不同的 scheduler，未按字段名猜测。"):
            self.assertIn(text, r["note"])
        self.assertEqual(r["extra"]["params_source"], "field_name")
        self.assertEqual(r["extra"]["params_fields"], ["sampler_name"])

    def test_chain_values_are_never_overwritten(self):
        graph = api_graph("chain prompt")
        graph["9"] = {"class_type": "RandomNoise", "inputs": {"noise_seed": 999}}
        graph["10"] = {"class_type": "BasicScheduler", "inputs": {"steps": 99, "scheduler": "karras"}}
        r = self.params(graph, "chain.png")
        self.assertEqual(r["seed"], 1234)          # KSampler 链上的值优先
        self.assertEqual((r["steps"], r["cfg"]), (25, 6.5))
        self.assertEqual((r["sampler"], r["scheduler"]), ("dpmpp_2m", "karras"))
        self.assertEqual(r["extra"]["params_source"], "chain")
        self.assertNotIn("params_fields", r["extra"])
        self.assertNotIn("未按字段名猜测", r["note"])   # 链式已有的字段冲突不写 note

    def test_partially_chained_graph_is_marked_field_name(self):
        graph = api_graph("chain prompt")
        graph["11"] = {"class_type": "CustomSigmas", "inputs": {"step_count": 40}}
        del graph["3"]["inputs"]["steps"]
        r = self.params(graph, "partial.png")
        self.assertEqual(r["steps"], 40)
        self.assertEqual(r["seed"], 1234)
        self.assertEqual(r["extra"]["params_source"], "field_name")
        self.assertEqual(r["extra"]["params_fields"], ["step_count"])

    def test_out_of_range_and_non_scalar_values_rejected(self):
        cases = (("seed", -1, None), ("seed", 2 ** 64, None), ("seed", 2 ** 64 - 1, 2 ** 64 - 1),
                 ("seed", True, None), ("seed", ["9", 0], None), ("seed", "not a number", None),
                 ("steps", 0, None), ("steps", 10001, None), ("steps", 10000, 10000),
                 ("cfg", 100.5, None), ("cfg", 0, 0.0), ("cfg", "6.5", 6.5),
                 ("sampler", "dpmpp_9x_magic", ""), ("scheduler", "magic_schedule", ""))
        for index, (field, value, expected) in enumerate(cases):
            graph = {"1": {"class_type": "CustomNode", "inputs": {field: value}}}
            r = self.params(graph, "domain%d.png" % index)
            self.assertEqual(r[field], expected, "%s=%r" % (field, value))
            if expected in (None, ""):
                self.assertNotIn("params_source", r["extra"])
            else:
                self.assertEqual(r["extra"]["params_source"], "field_name")

    def test_invalid_values_do_not_create_conflicts(self):
        """表外/越界的候选值压根不是候选，不该把唯一的合法值挤成冲突。"""
        graph = {
            "1": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "dpmpp_9x_magic"}},
            "2": {"class_type": "CustomSamplerSelect", "inputs": {"sampler": "euler_ancestral_cfg_pp"}},
            "3": {"class_type": "BasicScheduler", "inputs": {"scheduler": "linear_quadratic", "steps": 20}},
        }
        r = self.params(graph, "noconflict.png")
        self.assertEqual(r["sampler"], "euler_ancestral_cfg_pp")
        self.assertEqual(r["scheduler"], "linear_quadratic")
        self.assertEqual(r["steps"], 20)
        self.assertNotIn("未按字段名猜测", r["note"])

    def test_known_tables_are_measured_union_official(self):
        for value in ("euler", "res_multistep", "er_sde", "euler_ancestral"):     # 真实库实测四个
            self.assertIn(value, media_meta.KNOWN_SAMPLERS)
        for value in ("simple", "beta57", "beta", "sgm_uniform"):                 # 实测四个调度器
            self.assertIn(value, media_meta.KNOWN_SCHEDULERS)
        for value in ("karras", "exponential", "ddim_uniform", "normal", "kl_optimal", "linear_quadratic"):
            self.assertIn(value, media_meta.KNOWN_SCHEDULERS)                     # 官方枚举并集
        for junk in ("Euler", "dpmpp_magic", "", "scheduler"):
            self.assertNotIn(junk, media_meta.KNOWN_SAMPLERS)
        self.assertTrue(all(name == name.lower() for name in media_meta.KNOWN_SAMPLERS))
        self.assertTrue(all(name == name.lower() for name in media_meta.KNOWN_SCHEDULERS))

    def test_mp4_comment_params_recovered(self):
        text = comment_json(prompt=custom_param_api_graph(seed=484799350375457, steps="10",
                                                          sampler="res_multistep", scheduler="simple"))
        path = self.write("params.mp4", mp4_bytes(ftyp(), mdat(512),
                                                  moov([video_trak()], udta=udta_ilst(text))))
        r = media_meta.extract(path)
        self.assert_shape(r)
        self.assertEqual(r["source"], "mp4_udta")
        self.assertEqual((r["seed"], r["steps"], r["cfg"], r["sampler"], r["scheduler"]),
                         (484799350375457, 10, 1.0, "res_multistep", "simple"))
        self.assertEqual(r["extra"]["params_source"], "field_name")
        self.assertEqual(r["fps"], 23.976)

    def test_sidecar_params_recovered(self):
        png = self.write("sc.png", png_bytes([]))
        (self.root / "sc.json").write_text(json.dumps({"prompt": custom_param_api_graph(seed=9)}),
                                          encoding="utf-8")
        r = media_meta.extract(png)
        self.assertEqual(r["source"], "sidecar")
        self.assertEqual(r["seed"], 9)
        self.assertEqual(r["extra"]["params_source"], "field_name")

    def test_ui_only_graph_gets_no_params_provenance(self):
        path = self.write("uip.png", png_bytes([ztxtp("workflow", json.dumps(ui_graph("dusk")))]))
        r = media_meta.extract(path)
        self.assertNotIn("params_source", r["extra"])
        self.assertNotIn("params_fields", r["extra"])
        self.assertIsNone(r["seed"])


class SidecarTests(MediaMetaTestCase):
    def test_png_uses_shared_sidecar(self):
        png = self.write("out.png", png_bytes([]))
        (self.root / "out.json").write_text(json.dumps({"prompt": api_graph("sidecar prompt")}),
                                            encoding="utf-8")
        r = media_meta.extract(png)
        self.assert_shape(r)
        self.assertTrue(r["has_meta"])
        self.assertEqual(r["source"], "sidecar")
        self.assertEqual(r["prompt"], "sidecar prompt")
        self.assertEqual(r["checkpoint"], "animagineXL.safetensors")
        self.assertEqual(r["extra"]["sidecar"], "out.json")

    def test_workflow_json_suffix_is_second_choice(self):
        png = self.write("two.png", png_bytes([]))
        (self.root / "two.json").write_text("{ broken", encoding="utf-8")
        (self.root / "two.workflow.json").write_text(json.dumps(ui_graph("from workflow json")), encoding="utf-8")
        r = media_meta.extract(png)
        self.assertTrue(r["has_meta"])
        self.assertEqual(r["prompt"], "from workflow json")
        self.assertEqual(r["extra"]["sidecar"], "two.workflow.json")

    def test_video_shares_sidecar_but_dims_still_from_container(self):
        data = mp4_bytes(ftyp(), mdat(64), moov([]))
        path = self.write("shared.mp4", data)
        (self.root / "shared.json").write_text(comment_json(prompt={"1": {"class_type": "KSampler",
                                                                         "inputs": {"seed": 3}}}),
                                               encoding="utf-8")
        r = media_meta.extract(path)
        self.assertTrue(r["has_meta"])
        self.assertEqual(r["source"], "sidecar")
        self.assertEqual(r["seed"], 3)
        self.assertEqual((r["width"], r["height"]), (640, 360))
        self.assertEqual(r["duration"], 96.0)

    def test_sidecar_oversize_is_skipped(self):
        png = self.write("big.png", png_bytes([]))
        (self.root / "big.json").write_text(json.dumps({"prompt": "x" * 400}), encoding="utf-8")
        with patch.object(media_meta, "MAX_SIDECAR_BYTES", 64):
            r = media_meta.extract(png)
        self.assertFalse(r["has_meta"])
        self.assertIn("8MB", r["note"])

    def test_sidecar_non_dict_is_ignored(self):
        png = self.write("arr.png", png_bytes([]))
        (self.root / "arr.json").write_text("[1, 2, 3]", encoding="utf-8")
        r = media_meta.extract(png)
        self.assert_shape(r)
        self.assertFalse(r["has_meta"])
        self.assertIn("不是 JSON 对象", r["note"])

    def test_embedded_metadata_beats_sidecar(self):
        png = self.write("both.png", png_bytes([texp("prompt", json.dumps(api_graph("embedded")))]))
        (self.root / "both.json").write_text(json.dumps({"prompt": api_graph("from sidecar")}), encoding="utf-8")
        r = media_meta.extract(png)
        self.assertEqual(r["prompt"], "embedded")
        self.assertEqual(r["source"], "png_text")
        self.assertNotIn("sidecar", r["extra"])

    def test_json_file_does_not_read_itself(self):
        path = self.write("only.json", json.dumps({"prompt": "self"}))
        r = media_meta.extract(path)
        self.assert_shape(r)
        self.assertEqual(r["source"], "none")


class RobustnessTests(MediaMetaTestCase):
    def test_missing_file(self):
        r = media_meta.extract(str(self.root / "nope.png"))
        self.assert_shape(r)
        self.assertEqual(r["source"], "none")
        self.assertIn("不存在", r["note"])

    def test_odd_inputs_never_raise(self):
        directory = self.root / "adir"
        directory.mkdir()
        (self.root / "empty.png").write_bytes(b"")
        cases = [str(directory), str(self.root / "empty.png"), "", None, 12345,
                 str(self.root / "nope.mp4"), str(self.root / "nope.json"), b"bytes path",
                 self.root / "path object.png"]
        for case in cases:
            r = media_meta.extract(case)
            self.assert_shape(r)

    def test_ext_argument_is_honoured(self):
        path = self.write("renamed.dat", png_bytes([texp("prompt", json.dumps(api_graph("via ext")))]))
        r = media_meta.extract(path, ext="PNG")
        self.assertEqual(r["prompt"], "via ext")
        self.assertEqual(r["source"], "png_text")

    def test_extra_is_capped(self):
        payload = {"generation": {"note": "y" * 5000}, "chunk_keys": ["a" * 900], "mp4": {"tags": ["prompt"]}}
        with patch.object(media_meta, "MAX_EXTRA_BYTES", 1024):
            capped = media_meta._cap_extra(payload)
        self.assertTrue(capped["truncated"])
        self.assertLessEqual(len(json.dumps(capped, ensure_ascii=False, allow_nan=False).encode("utf-8")), 1024)

    def test_extra_within_limit_is_untouched(self):
        self.assertEqual(media_meta._cap_extra({"graph": "api"}), {"graph": "api"})

    def test_blank_result_shape(self):
        blank = media_meta.blank_result()
        self.assertEqual(sorted(blank), sorted(media_meta.RESULT_KEYS))
        self.assertEqual(blank["source"], "none")
        self.assertEqual(blank["extra"], {})
        # 契约 §15.1.6：fps 必须在恒定键里，否则写库与详情会炸
        self.assertIn("fps", blank)
        self.assertIsNone(blank["fps"])
        self.assertIn("fps", media_meta.RESULT_KEYS)

    def test_spec_numbers_are_contract_values(self):
        self.assertEqual(media_meta.MAX_CHUNK_BYTES, 8 * 1024 * 1024)
        self.assertEqual(media_meta.MAX_PNG_CHUNKS, 64)
        self.assertEqual(media_meta.MAX_WINDOW_BYTES, 16 * 1024 * 1024)
        self.assertEqual(media_meta.MAX_SIDECAR_BYTES, 8 * 1024 * 1024)
        self.assertEqual(media_meta.MAX_EXTRA_BYTES, 64 * 1024)


if __name__ == "__main__":
    unittest.main()
