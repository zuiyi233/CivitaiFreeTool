# -*- coding: utf-8 -*-
"""图片加载性能改动（显示档/视频判定/体积上限/元数据诚实性）回归网 —— image_gallery 侧。

钉住的事实（对应任务清单第 1~7、9 条）：全部使用 fixture 假值与临时目录，不触网、
不读用户配置；真实库对拍用例默认跳过（需 CIVITAI_TEST_REAL_LIBRARY 且目录存在）。"""
import base64,hashlib,io,json,os,sys,time,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image,PngImagePlugin
import config,image_gallery,webui
from comfy_metadata import PARSER_REVISION

MB=1024*1024
# 真实四段式：/{auth}/{hash}/{params}/{file}，params 段 100% 是 original=true
BIG='https://image.civitai.com/auth123/hashabc/original=true/img.png'
NARROW='https://image.civitai.com/auth123/hashabc/original=true/narrow.png'
VIDEO='https://image.civitai.com/auth123/hashabc/original=true/clip.mp4'

def png_bytes(size=(2048,1024),parameters='mountain landscape\nNegative prompt: blur\nSteps: 26, Seed: 7'):
    out=io.BytesIO();meta=PngImagePlugin.PngInfo()
    if parameters is not None:meta.add_text('parameters',parameters)
    Image.new('RGB',size,'blue').save(out,format='PNG',pnginfo=meta)
    return out.getvalue()

class StreamedResponse:
    """按请求大小喂字节：返回 len==size 表示流未结束，短/空块表示结束。"""
    def __init__(self,data):self.data=data;self.pos=0;self.reads=0
    def read(self,size):
        self.reads+=1
        piece=self.data[self.pos:self.pos+size];self.pos+=len(piece);return piece

class CtxStreamedResponse(StreamedResponse):
    def __enter__(self):return self
    def __exit__(self,*a):return False

class VideoDetectionTests(unittest.TestCase):
    # 钉住第 1 条（image_gallery 侧）：type 缺失时按 URL 尾缀判视频
    def test_suffix_set_and_is_video_source(self):
        self.assertEqual(image_gallery.VIDEO_SUFFIXES,('.mp4','.webm','.mov','.mkv','.avi'))
        for ext in image_gallery.VIDEO_SUFFIXES:
            self.assertTrue(image_gallery.is_video_source('https://image.civitai.com/a/b/orig/x'+ext))
        self.assertTrue(image_gallery.is_video_source(VIDEO))
        # query/fragment 与大写尾缀都要认（真实 URL 带 token）
        self.assertTrue(image_gallery.is_video_source('https://cdn.example/v.MP4?token=abc#frag'))
        self.assertFalse(image_gallery.is_video_source('https://cdn.example/img.jpeg?x=.mp4'))
        self.assertFalse(image_gallery.is_video_source(BIG))
        self.assertFalse(image_gallery.is_video_source(''))
        self.assertFalse(image_gallery.is_video_source(None))
        self.assertFalse(image_gallery.is_video_source('https://[bad-url'))  # 不抛错

    def test_is_video_item_type_or_url_or_orig_url(self):
        f=image_gallery._is_video_item
        self.assertTrue(f({'url':VIDEO}))                                # 无 type + .mp4 → 视频
        self.assertTrue(f({'type':'video','url':BIG}))                  # type 兜底
        self.assertTrue(f({'url':BIG,'orig_url':VIDEO}))                # orig_url 尾缀
        self.assertFalse(f({'type':'image','url':BIG}))                 # type='image' 静帧
        self.assertFalse(f({'url':BIG,'width':2048}))
        for junk in (None,'x',123,[],{}):
            self.assertFalse(f(junk))  # 非 dict 输入不抛错
        self.assertFalse(f(None));self.assertFalse(f('string'));self.assertFalse(f(123))
        self.assertTrue(f({'type':'Video','url':BIG}))  # type 大小写不敏感

    def test_video_bytes_never_enter_pil_path(self):
        # 视频条目走 _video_bytes 原样字节；read_bytes 返回 fmt='VIDEO'
        with patch.object(image_gallery,'_fetch',return_value=b'fake-mp4-bytes') as fetch:
            data,fmt,w,h,orig=image_gallery.read_bytes({'url':VIDEO,'width':1920,'height':1080},{})
        self.assertEqual((data,fmt),(b'fake-mp4-bytes','VIDEO'))
        self.assertEqual((w,h),(1920,1080))
        # 视频上限必须是 100MB（_video_bytes 的 limit 参数位）
        self.assertEqual(fetch.call_args[0][2],100*MB)

class DisplayUrlTests(unittest.TestCase):
    # 钉住第 2、3、4 条
    def test_replaces_existing_params_segment_only(self):
        r=image_gallery.display_url(BIG,1440,80,2048)
        self.assertEqual(r,'https://image.civitai.com/auth123/hashabc/width=1440,quality=80/img.png')
        self.assertNotIn('original=true',r)
        self.assertEqual(r.split('/'),['https:','','image.civitai.com','auth123','hashabc','width=1440,quality=80','img.png'])
        # 已有 width 段是替换不是追加
        r2=image_gallery.display_url('https://image.civitai.com/a/h/width=450/i.png',1440,80,2048)
        self.assertEqual(r2,'https://image.civitai.com/a/h/width=1440,quality=80/i.png')
        self.assertEqual(r2.count('width='),1)

    def test_anti_upsample_both_directions(self):
        # natural>max_width：必须带 width=（实测均值 461KB vs 原图 3235KB）
        wide=image_gallery.display_url(BIG,1440,80,2048)
        self.assertIn('width=1440',wide)
        # natural<=max_width：绝不能再带 width=（实测上采样 682KB -> 1218800B）
        narrow=image_gallery.display_url(NARROW,1440,80,1200)
        self.assertNotIn('width=',narrow)
        self.assertIn('quality=80',narrow)
        # 恰好等于档位也不加 width（<=）
        self.assertNotIn('width=',image_gallery.display_url(NARROW,1440,80,1440))
        # natural 未知：保守按档位加 width
        self.assertIn('width=1440',image_gallery.display_url(BIG,1440,80,None))

    def test_refuses_everything_it_cannot_rewrite_safely(self):
        self.assertIsNone(image_gallery.display_url('https://image.civitai.com/a/b/img.png',1440,80,2048))  # 无 params 段
        self.assertIsNone(image_gallery.display_url('https://image.civitai.com/a/original=true',1440,80,2048))  # params 是最后一段
        self.assertIsNone(image_gallery.display_url('https://evil.example/a/original=true/i.png',1440,80))    # 非官方 CDN
        self.assertIsNone(image_gallery.display_url('http://image.civitai.com/a/original=true/i.png',1440,80))  # 非 https
        self.assertIsNone(image_gallery.display_url('https://u:pw@media.civitai.com/a/original=true/i.png',1440,80,2048))  # userinfo
        self.assertIsNone(image_gallery.display_url('https://u@image.civitai.com/a/original=true/i.png',1440,80,2048))
        self.assertIsNone(image_gallery.display_url(VIDEO,1440,80,2048))   # 视频绝不改写
        self.assertIsNone(image_gallery.display_url(BIG,None,None,2048))   # 无档位请求 = 用原图
        self.assertIsNone(image_gallery.display_url(None,1440,80))

    def test_query_preserved_original_dropped_for_quality_only(self):
        r=image_gallery.display_url(BIG+'?token=abc',1440,80,2048)
        self.assertTrue(r.endswith('img.png?token=abc'),r)
        self.assertNotIn('#',r)  # fragment 丢弃是 urlunsplit 语义
        q=image_gallery.display_url(BIG,None,80,None)  # 只降 quality 也必须剥 original=true，否则 quality 变空操作
        self.assertIn('quality=80',q);self.assertNotIn('original=true',q);self.assertNotIn('width=',q)

    def test_never_emits_format_or_resize(self):
        for url in (image_gallery.display_url(BIG,1440,80,2048),
                    image_gallery.display_url(NARROW,1440,80,1200),
                    image_gallery.display_url(BIG,None,80,None),
                    image_gallery.display_url('https://image.civitai.com/a/h/width=450/i.png',1000,60,2000)):
            self.assertNotIn('format=',url);self.assertNotIn('resize=',url)

    def test_tier_values_clamped_and_clipped(self):
        # natural 比档位还宽时才保留 width=；钳制到 4096/100
        self.assertEqual(image_gallery.display_url(BIG,10**9,999,8000),'https://image.civitai.com/auth123/hashabc/width=4096,quality=100/img.png')
        self.assertIsNone(image_gallery.display_url(BIG,0,0,2048))  # 0/负数视为无档位

    def test_natural_width_actually_reaches_the_guard(self):
        """防上采样必须在「生产链路」成立，不只是 display_url 单测成立。
        复核时发现 covers 没带 width → read_bytes 里 natural_width 恒为 None，
        68%（328/479）本来就 ≤1440px 的图照样被 width=1440 顶大（实测 682KB→1.2MB）。
        这里从 get_model_detail 一路打到真正请求的 URL，钉住接线。"""
        import webui
        with TemporaryDirectory() as d:
            model = Path(d) / "m.safetensors"; model.write_bytes(b"fixture")
            (Path(d) / "m.civitai.info").write_text(json.dumps({"name": "m", "images": [
                {"url": NARROW, "width": 1200, "height": 800},   # 比档位窄：不该加 width
                {"url": BIG, "width": 2048, "height": 1536},     # 比档位宽：该加 width
            ]}), encoding="utf-8")
            api = webui.Api.__new__(webui.Api)
            api.cfg = {"cache_original_images": True, "image_max_mb": 20, "video_max_mb": 100,
                       "fetch_deadline_s": 120, "gallery_display_width": 1440, "gallery_display_quality": 80,
                       "proxy_enabled": False, "ssl_verify": True, "models_dir": d, "models_dirs": [d],
                       "images_dir_autofill": False}
            detail = json.loads(api.get_model_detail(str(model)))
            by_url = {c["url"]: c for c in detail["covers"] if c.get("type") == "image"}
            self.assertEqual(by_url[NARROW].get("width"), 1200, "covers 必须把 info 的 width 传下去")
            self.assertEqual(by_url[BIG].get("width"), 2048)
            asked = []

            def spy(url, cfg, *a, **k):
                asked.append(url)
                raise OSError("stop before decode")
            with patch("image_gallery._fetch", side_effect=spy):
                for item, expect_width in ((by_url[NARROW], False), (by_url[BIG], True)):
                    asked.clear()
                    try:
                        image_gallery.read_bytes(item, api.cfg, 1440, 80)
                    except Exception:
                        pass
                    self.assertTrue(asked, "远程档应走到 _fetch")
                    self.assertEqual("width=" in asked[0], expect_width,
                                     "%s 期望 width 段=%s，实际 %s" % (item["url"][-22:], expect_width, asked[0]))

class CacheTierKeyTests(unittest.TestCase):
    # 钉住第 5 条
    def test_cache_tier_strings(self):
        self.assertEqual(image_gallery.cache_tier(),'', )
        self.assertEqual(image_gallery.cache_tier(None,None),'')
        self.assertEqual(image_gallery.cache_tier(0,0),'')
        self.assertEqual(image_gallery.cache_tier(1440,80),':w1440q80')
        self.assertEqual(image_gallery.cache_tier(1440,None),':w1440')
        self.assertEqual(image_gallery.cache_tier(None,80),':q80')
        self.assertEqual(image_gallery.cache_tier(5000,150),':w4096q100')

    def test_empty_tier_key_byte_identical_to_legacy_algorithm(self):
        # 用户已有 68 个缓存文件：tier='' 的键若变，等于全库重下载。手算旧算法对拍。
        item={'url':BIG};cfg={'cache_original_images':True}
        with TemporaryDirectory() as d,patch.object(config,'APP_DIR',d):
            expected=Path(d)/'gallery_cache'/hashlib.sha256(BIG.encode()).hexdigest()
            self.assertEqual(image_gallery.cache_key(item,cfg),expected)
            self.assertEqual(image_gallery.cache_key(item,cfg,False,''),expected)
            self.assertEqual(image_gallery.cache_key(item,cfg,tier=''),expected)
            # metadata=True 的既有语义（解析器版本 + api_key 域隔离）不变
            src=BIG+':'+PARSER_REVISION+':meta:'+hashlib.sha256(b'fixture-fake-key').hexdigest()
            self.assertEqual(image_gallery.cache_key(item,{**cfg,'api_key':'fixture-fake-key'},True),
                             Path(d)/'gallery_cache'/hashlib.sha256(src.encode()).hexdigest())
            # local_path 条目同样对齐旧算法（resolve+mtime_ns+size）
            p=Path(d)/'img.png';p.write_bytes(b'x');st=os.stat(p)
            lsrc=str(Path(p).resolve())+':'+str(st.st_mtime_ns)+':'+str(st.st_size)
            self.assertEqual(image_gallery.cache_key({'local_path':str(p)},cfg),
                             Path(d)/'gallery_cache'/hashlib.sha256(lsrc.encode()).hexdigest())

    def test_tier_keys_isolated_and_no_cross_hits(self):
        with TemporaryDirectory() as d,patch.object(config,'APP_DIR',d):
            item={'url':BIG};cfg={'cache_original_images':True}
            self.assertNotEqual(image_gallery.cache_key(item,cfg,False,':w1440q80'),image_gallery.cache_key(item,cfg,False,''))
            image_gallery.cache_store(item,cfg,'original',b'tier-bytes',':w1440q80')
            self.assertIsNone(image_gallery.cache_load(item,cfg,'original'))              # 原图档读不到显示档
            self.assertEqual(image_gallery.cache_load(item,cfg,'original',':w1440q80'),b'tier-bytes')
            image_gallery.cache_store(item,cfg,'original',b'orig-bytes')
            self.assertEqual(image_gallery.cache_load(item,cfg,'original'),b'orig-bytes') # 写入原图档不影响显示档
            self.assertEqual(image_gallery.cache_load(item,cfg,'original',':w1440q80'),b'tier-bytes')

    def test_tier_cache_disabled_or_symlink_still_guarded(self):
        with TemporaryDirectory() as d,patch.object(config,'APP_DIR',d):
            self.assertIsNone(image_gallery.cache_key({'url':BIG},{},False,':w1440q80'))  # 开关关闭一律无键

class CapAndDeadlineTests(unittest.TestCase):
    # 钉住第 6 条（image_gallery 侧）
    def test_static_video_store_limits(self):
        self.assertEqual(image_gallery.static_limit_bytes({}),20*MB)
        self.assertEqual(image_gallery.video_limit_bytes({}),100*MB)
        self.assertEqual(image_gallery.store_limit_bytes({}),100*MB)   # 单文件阈值跟随最大口径，19MB 视频要存得下
        self.assertEqual(image_gallery.store_limit_bytes({'video_max_mb':30,'image_max_mb':20}),30*MB)
        self.assertEqual(image_gallery.static_limit_bytes({'image_max_mb':'脏值'}),20*MB)
        self.assertEqual(image_gallery.static_limit_bytes({'image_max_mb':0}),20*MB)      # 0 回退默认
        self.assertEqual(image_gallery.static_limit_bytes({'image_max_mb':None}),20*MB)
        self.assertEqual(image_gallery.video_limit_bytes({'video_max_mb':-5}),100*MB)      # 负数=脏值→回退默认，与 webui._video_cap_bytes 同一口径
        self.assertEqual(image_gallery.static_limit_bytes({'image_max_mb':10**6}),4096*MB) # 上界与 config.normalize 最宽的一条对齐，不偷偷收窄

    def test_fetch_deadline_seconds(self):
        self.assertEqual(image_gallery.fetch_deadline_seconds({}),120.0)
        self.assertEqual(image_gallery.fetch_deadline_seconds({'fetch_deadline_s':0}),120.0)
        self.assertEqual(image_gallery.fetch_deadline_seconds({'fetch_deadline_s':'脏'}),120.0)
        self.assertEqual(image_gallery.fetch_deadline_seconds({'fetch_deadline_s':-9}),120.0)     # 负数=脏值→回退默认
        self.assertEqual(image_gallery.fetch_deadline_seconds({'fetch_deadline_s':10**9}),3600.0) # 上限=config normalize 的 3600
        # 同一个配置键在两处必须算出同一个值（曾经 webui 回默认、这里回 1MB）
        for cfg in ({},{'image_max_mb':-5},{'image_max_mb':0},{'video_max_mb':-7}):
            self.assertEqual(image_gallery.static_limit_bytes(cfg),webui._image_cap_bytes(cfg))
            self.assertEqual(image_gallery.video_limit_bytes(cfg),webui._video_cap_bytes(cfg))
        self.assertEqual(int(image_gallery.fetch_deadline_seconds({'fetch_deadline_s':45})),webui._fetch_deadline_s({'fetch_deadline_s':45}))

    def test_read_capped_returns_complete_stream(self):
        r=StreamedResponse(b'image-bytes'*3)
        self.assertEqual(image_gallery._read_capped(r,10**6,time.monotonic()+60,'TOO','DEADLINE',chunk=4),b'image-bytes'*3)

    def test_read_capped_deadline_raises_never_half_picture(self):
        r=StreamedResponse(b'x'*10**6)
        with patch('image_gallery.time.monotonic',side_effect=[0.0,1000.0]):
            with self.assertRaises(ValueError) as ctx:
                image_gallery._read_capped(r,10**6,5.0,'TOO','TIMEOUT-MSG',chunk=4)
        self.assertIn('TIMEOUT-MSG',str(ctx.exception))
        self.assertEqual(r.reads,1)  # 超时立刻中止，读到的部分被丢弃、绝不返回

    def test_read_capped_too_large_distinct_messages(self):
        future=time.monotonic()+60
        with self.assertRaises(ValueError) as ctx:
            image_gallery._read_capped(StreamedResponse(b'x'*10),7,future,image_gallery._too_large_message(False,{}),'T',chunk=4)
        self.assertIn('图片超过 20 MB',str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            image_gallery._read_capped(StreamedResponse(b'x'*10),7,future,image_gallery._too_large_message(True,{}),'T',chunk=4)
        self.assertIn('视频超过 100 MB',str(ctx.exception))
        self.assertNotIn('图片超过',str(ctx.exception))

    def test_timeout_message_is_chinese_and_states_budget(self):
        msg=image_gallery._timeout_message({})
        self.assertTrue(msg.startswith('下载超时'),msg);self.assertIn('120 秒',msg)

    def test_fetch_wraps_opener_and_read_capped(self):
        opener=Mock();resp=CtxStreamedResponse(b'full-picture-bytes')
        opener.open.return_value=resp
        with patch('civitai_api.build_opener',return_value=opener):
            data=image_gallery._fetch(BIG,{},10*MB,time.monotonic()+60,'TOO','TIMEOUT')
        self.assertEqual(data,b'full-picture-bytes')
        req=opener.open.call_args[0][0]
        self.assertEqual(req.headers['Referer'],'https://civitai.com/')
        over=CtxStreamedResponse(b'x'*40)
        opener.open.return_value=over
        with patch('civitai_api.build_opener',return_value=opener):
            with self.assertRaises(ValueError) as ctx:
                image_gallery._fetch(BIG,{},20,time.monotonic()+60,'图片超过 20 MB','TIMEOUT')
        self.assertIn('图片超过',str(ctx.exception))

class ReadBytesTierTests(unittest.TestCase):
    # 钉住第 2、6 条在 read_bytes 的接线
    def test_tier_fetches_display_url_and_reports_not_original(self):
        item={'url':BIG,'width':2048}
        with patch.object(image_gallery,'_fetch',return_value=png_bytes((1440,720))) as fetch:
            data,fmt,w,h,orig=image_gallery.read_bytes(item,{},1440,80)
        fetch.assert_called_once()
        self.assertIn('width=1440,quality=80',fetch.call_args[0][0])
        self.assertEqual(fetch.call_args[0][2],20*MB)   # 静帧 20MB 上限显式传入
        self.assertFalse(orig)

    def test_natural_narrow_requests_quality_only(self):
        item={'url':NARROW,'width':1200}
        with patch.object(image_gallery,'_fetch',return_value=png_bytes((1200,800))) as fetch:
            image_gallery.read_bytes(item,{},1440,80)
        url=fetch.call_args[0][0]
        self.assertNotIn('width=',url);self.assertIn('quality=80',url)

    def test_no_tier_matches_legacy_original_semantics(self):
        item={'url':BIG,'width':2048}
        with patch.object(image_gallery,'_fetch',return_value=png_bytes()) as fetch:
            data,fmt,w,h,orig=image_gallery.read_bytes(item,{})
        self.assertEqual(fetch.call_args[0][0],image_gallery.original_url(BIG))
        self.assertTrue(orig)

    def test_local_path_never_downgraded_and_never_stored_as_tier(self):
        with TemporaryDirectory() as d:
            p=Path(d)/'img.png';p.write_bytes(png_bytes((800,600)))
            with patch.object(image_gallery,'_fetch',side_effect=AssertionError('local must not hit network')):
                data,fmt,w,h,orig=image_gallery.read_bytes({'local_path':str(p)},{},1440,80)
            self.assertEqual(data,p.read_bytes());self.assertTrue(orig)  # 本地原图 = 原图档

    def test_display_tier_failure_falls_back_to_original(self):
        item={'url':BIG,'width':2048}
        with patch.object(image_gallery,'_fetch',side_effect=[OSError('CDN 404'),png_bytes()]) as fetch:
            data,fmt,w,h,orig=image_gallery.read_bytes(item,{},1440,80)
        self.assertEqual(fetch.call_count,2)
        self.assertIn('width=1440',fetch.call_args_list[0][0][0])
        self.assertEqual(fetch.call_args_list[1][0][0],image_gallery.original_url(BIG))
        self.assertTrue(orig)  # 回落成功 = 拿到的就是原图

    def test_video_preview_attempt_rejected_without_network(self):
        with patch.object(image_gallery,'_fetch',Mock()) as fetch,patch('civitai_api.build_opener',side_effect=AssertionError('no network')):
            with self.assertRaises(ValueError) as ctx:
                image_gallery.preview({'url':VIDEO},{})
        self.assertIn('视频不能生成图片预览',str(ctx.exception))
        fetch.assert_not_called()

class PreviewHonestyTests(unittest.TestCase):
    # 钉住第 7 条：显示档字节绝不用于提取生成参数
    def test_tier_without_original_cache_admits_downgraded_metadata(self):
        item={'url':BIG,'width':2048}
        with patch.object(image_gallery,'_fetch',return_value=png_bytes((1440,720),parameters=None)):
            r=image_gallery.preview(item,{},1440,80)   # cfg={} -> 缓存关 -> 必然没有原图缓存
        self.assertTrue(r['ok'])
        self.assertTrue(r['downgraded'])
        self.assertEqual(r['display_tier'],':w1440q80')
        self.assertFalse(r['metadata_from_original'])
        self.assertIn('转码',r['metadata_note'])  # 中文说明
        self.assertEqual(r['meta'].get('prompt'),None)  # 绝不把档位字节当参数源

    def test_tier_with_cached_original_extracts_params_from_original_bytes(self):
        with TemporaryDirectory() as d,patch.object(config,'APP_DIR',d):
            item={'url':BIG,'width':2048};cfg={'cache_original_images':True,'api_key':'fixture-fake-key'}
            image_gallery.cache_store(item,cfg,'original',png_bytes())  # 原图档（带 parameters）
            with patch.object(image_gallery,'_fetch',return_value=png_bytes((1440,720),parameters=None)) as fetch:
                r=image_gallery.preview(item,cfg,1440,80)
            self.assertTrue(r['downgraded'])
            self.assertTrue(r['metadata_from_original'])
            self.assertEqual(r['meta']['prompt'],'mountain landscape')
            self.assertEqual(fetch.call_count,1)  # 元数据绝不触发第二次原图下载（_original_info_bytes 只读缓存）

    def test_original_tier_behaves_exactly_like_before(self):
        with TemporaryDirectory() as d,patch.object(config,'APP_DIR',d):
            p=Path(d)/'img.png';p.write_bytes(png_bytes((640,480)))
            r=image_gallery.preview({'local_path':str(p)},{})
            self.assertFalse(r['downgraded']);self.assertEqual(r['display_tier'],'')
            self.assertTrue(r['metadata_from_original'])
            self.assertEqual(r['meta']['prompt'],'mountain landscape');self.assertEqual(r['meta']['Steps'],'26')

    def test_preview_cache_per_tier_no_cross_hit(self):
        with TemporaryDirectory() as d,patch.object(config,'APP_DIR',d):
            item={'url':BIG,'width':2048};cfg={'cache_original_images':True}
            with patch.object(image_gallery,'_fetch',return_value=png_bytes((1440,720),parameters=None)):
                a=image_gallery.preview(item,cfg,1440,80)
            self.assertFalse(a.get('cache_hit'))
            self.assertIsNotNone(image_gallery.cache_load(item,cfg,'preview',':w1440q80'))
            self.assertIsNone(image_gallery.cache_load(item,cfg,'preview',''))   # 原图档 preview 未写过
            with patch.object(image_gallery,'_fetch',side_effect=AssertionError('同档命中不得再联网')):
                b=image_gallery.preview(item,cfg,1440,80)
            self.assertTrue(b.get('cache_hit'));self.assertEqual(b['display_tier'],':w1440q80')

class RealLibraryParityTests(unittest.TestCase):
    """第 9 条：真实库只读对拍。CI 默认跳过，仅当 CIVITAI_TEST_REAL_LIBRARY 指向存在目录。"""
    ROOT=os.environ.get('CIVITAI_TEST_REAL_LIBRARY',r'D:\ComfyUI_Mie_2026_V8.0\ComfyUI\models')
    SUBS=('loras','diffusion_models','checkpoints')

    def scan(self):
        entries=[]
        for sub in self.SUBS:
            root=os.path.join(self.ROOT,sub)
            for dirpath,_dirs,files in os.walk(root):
                for fn in files:
                    if fn.endswith('.info.json') or fn.endswith('.civitai.info'):
                        try:info=json.load(open(os.path.join(dirpath,fn),encoding='utf-8'))
                        except (OSError,ValueError):continue
                        if isinstance(info,dict) and isinstance(info.get('images'),list):
                            entries.extend(im for im in info['images'] if isinstance(im,dict))
        return entries

    def test_read_only_parity(self):
        if not (os.environ.get('CIVITAI_TEST_REAL_LIBRARY') and os.path.isdir(os.path.join(self.ROOT,self.SUBS[0]))):
            self.skipTest('真实库对拍需要显式设置 CIVITAI_TEST_REAL_LIBRARY 且目录存在（只读）')
        entries=self.scan()
        self.assertEqual(len(entries),479)
        mp4=[im for im in entries if str(im.get('url','')).split('?')[0].lower().endswith('.mp4')]
        self.assertEqual(len(mp4),107)
        self.assertEqual(sum(1 for im in entries if 'type' in im),0)   # type 字段 100% 缺失
        self.assertTrue(all(isinstance(im.get('width'),int) and im['width']>0 for im in entries))
        self.assertTrue(image_gallery._is_video_item(mp4[0]))           # 新实现必须把它们判成视频
        self.assertEqual(sum(1 for im in entries if (im.get('width') or 10**9)<=1440),328)  # 防上采样覆盖 68% 条目

if __name__=='__main__':unittest.main()
