# -*- coding: utf-8 -*-
"""视频落盘扩展名：跟源一致，不再一律假造 .mp4。

背景：`_local_video_file` 早期固定 `video_XX.mp4`。C 站示例项里确实存在 .avi（
`reverse_parse.VIDEO_URL_EXTS` 认它），把 .avi 存成 .mp4 会让浏览器按 mp4 解码失败，
而前端反查序号的正则 `video_(\\d+)\\.[a-z0-9]+` 本来就认任意扩展名 —— 保留真扩展名是安全的。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import webui


class VideoExtTest(unittest.TestCase):
    def test_ext_follows_source_and_falls_back_to_mp4(self):
        self.assertEqual(webui._video_ext_for("https://x/a/b.avi?token=1"), ".avi")
        self.assertEqual(webui._video_ext_for("https://x/a/b.WEBM"), ".webm")
        self.assertEqual(webui._video_ext_for("https://x/a/b.mp4"), ".mp4")
        self.assertEqual(webui._video_ext_for("https://x/a/b"), ".mp4")
        self.assertEqual(webui._video_ext_for(""), ".mp4")
        self.assertEqual(webui._video_ext_for("https://x/a/b.png"), ".mp4")

    def test_local_video_file_keeps_extension(self):
        base = os.path.join("D:", "models", "demo")
        self.assertEqual(webui._local_video_file(base, 3), os.path.join(base + ".images", "video_03.mp4"))
        self.assertEqual(webui._local_video_file(base, 3, ".avi"),
                         os.path.join(base + ".images", "video_03.avi"))

    def test_find_local_video_accepts_any_known_ext(self):
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, "demo")
            os.makedirs(base + ".images")
            self.assertEqual(webui._find_local_video(base, 1), "")
            with open(os.path.join(base + ".images", "video_01.avi"), "wb") as f:
                f.write(b"x" * 2048)
            self.assertTrue(webui._find_local_video(base, 1).endswith("video_01.avi"))

    def test_fill_targets_uses_source_extension(self):
        images = [{"type": "video", "url": "https://image.civitai.com/a/b.avi"}]
        targets = webui._images_fill_targets("D:/m/demo", images, want_posters=False)
        video = [t for t in targets if t[0] == "video"]
        self.assertEqual(len(video), 1)
        self.assertTrue(video[0][2].endswith("video_01.avi"), video[0][2])


if __name__ == "__main__":
    unittest.main()
