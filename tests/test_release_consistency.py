# -*- coding: utf-8 -*-
"""发版身份一致性：APP_VERSION / Windows 版本资源 / CHANGELOG / 发布说明 必须同源。

起因：2.6.31 构建时只改了 webui.APP_VERSION，version_info.txt 还写着 2.6.30
（而且它内部的 filevers 元组与 FileVersion 字符串本来就互相不一致），
结果产物文件名是 v2.6.31、资源里报的却是 2.6.30——用户看属性对话框会以为拿错了包。
这四处在发版时是手工一起改的（见 docs/lan-access-contract.md:114），所以必须有测试钉住。
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


class ReleaseIdentityTests(unittest.TestCase):
    def setUp(self):
        import webui
        self.version = webui.APP_VERSION
        self.assertTrue(re.fullmatch(r"\d+\.\d+\.\d+", self.version),
                        "APP_VERSION 必须是 x.y.z（现在是 %r）" % self.version)

    def test_windows_version_resource_matches_everywhere(self):
        src = read("version_info.txt")
        nums = re.findall(r"(?:filevers|prodvers)=\((\d+), (\d+), (\d+), (\d+)\)", src)
        self.assertEqual(len(nums), 2, "version_info.txt 里 filevers/prodvers 应当各一处")
        want = tuple(int(x) for x in self.version.split(".")) + (0,)
        for raw in nums:
            self.assertEqual(tuple(int(x) for x in raw), want,
                             "version_info.txt 的数值版本 %s 与 APP_VERSION %s 不一致" % (raw, self.version))
        for key in ("FileVersion", "ProductVersion"):
            m = re.search(r"StringStruct\(u'%s', u'([^']+)'\)" % key, src)
            self.assertIsNotNone(m, "version_info.txt 缺 %s" % key)
            self.assertEqual(m.group(1), self.version,
                             "version_info.txt 的 %s=%s 与 APP_VERSION %s 不一致" % (key, m.group(1), self.version))

    def test_changelog_top_entry_and_release_note_track_the_version(self):
        head = read("CHANGELOG.md")
        first = re.search(r"^## v(\d+\.\d+\.\d+)", head, re.M)
        self.assertIsNotNone(first, "CHANGELOG.md 找不到形如 `## vX.Y.Z` 的条目")
        self.assertEqual(first.group(1), self.version,
                         "CHANGELOG 最新条目是 v%s，但 APP_VERSION 是 %s" % (first.group(1), self.version))
        self.assertIn("v%s" % self.version, head.splitlines()[0],
                      "CHANGELOG 标题的范围上界没跟着版本号走")
        self.assertTrue(os.path.exists(os.path.join(ROOT, "docs", "releases", "v%s.md" % self.version)),
                        "缺少 docs/releases/v%s.md（RELEASING.md 要求同步发布说明）" % self.version)

    def test_release_note_names_the_built_artifact(self):
        note = read(os.path.join("docs", "releases", "v%s.md" % self.version))
        self.assertIn("v%s" % self.version, note, "发布说明里没提到自己的版本号")
        self.assertIn("EXE", note, "发布说明要交代升级方式（只替换 EXE）")


if __name__ == "__main__":
    unittest.main()
