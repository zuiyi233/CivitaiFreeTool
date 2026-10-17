# -*- coding: utf-8 -*-
"""站点根（site_domain）必须同时决定公开 API / tRPC / 下载链接的 host。

背景：公开 API 基址曾硬编码 civitai.com，在「只有 civitai.red 可达」的网络里，
下载与元数据请求全部 15s 超时（实测），而镜像站 2s 就能返回。
"""
import unittest

import civitai_api


class SiteBaseTest(unittest.TestCase):
    def test_api_and_trpc_follow_site_domain(self):
        api = civitai_api.CivitaiAPI(site_base="civitai.red")
        self.assertEqual(api.api_base, "https://civitai.red/api/v1")
        self.assertEqual(api.trpc_base, "https://civitai.red/api/trpc")

    def test_full_url_site_domain_normalized(self):
        api = civitai_api.CivitaiAPI(site_base="https://civitai.red/")
        self.assertEqual(api.api_base, "https://civitai.red/api/v1")

    def test_default_stays_civitai_com(self):
        api = civitai_api.CivitaiAPI()
        self.assertEqual(api.api_base, "https://civitai.com/api/v1")

    def test_file_download_url_rewrites_host_keeps_query(self):
        api = civitai_api.CivitaiAPI(site_base="civitai.red")
        url = api.file_download_url(
            {"id": 1},
            {"id": 2, "name": "a.safetensors",
             "downloadUrl": "https://civitai.com/api/download/models/1?type=Model&format=SafeTensor"})
        self.assertTrue(url.startswith("https://civitai.red/api/download/models/1?"), url)
        self.assertIn("fileId=2", url)
        self.assertIn("format=SafeTensor", url)

    def test_file_download_url_rewrites_even_without_file_id(self):
        api = civitai_api.CivitaiAPI(site_base="civitai.red")
        url = api.file_download_url(
            {"id": 1},
            {"name": "a.safetensors",
             "downloadUrl": "https://civitai.com/api/download/models/1?type=Model"})
        self.assertTrue(url.startswith("https://civitai.red/api/download/models/1?"), url)

    def test_build_download_url_follows_site_base(self):
        api = civitai_api.CivitaiAPI(site_base="civitai.red")
        self.assertEqual(api.build_download_url(9), "https://civitai.red/api/download/models/9")

    def test_foreign_host_still_rejected(self):
        api = civitai_api.CivitaiAPI(site_base="civitai.red")
        with self.assertRaises(civitai_api.CivitaiError):
            api.file_download_url({}, {"downloadUrl": "https://evil.example/api/download/models/1"})


if __name__ == "__main__":
    unittest.main()
