# -*- coding: utf-8 -*-
"""图片加载性能改动回归网 —— webui 侧（视频判定/体积上限/补帧幂等/本地优先）。

钉住任务清单第 1、6、8 条及第 3、4、7、9 条的 webui 接线。全部临时目录 + Mock，
不触网、不写用户配置、不碰真实模型库；api_key 一律 fixture 假值。"""
import inspect,json,os,shutil,sys,threading,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
import config,image_gallery,webui

MB=1024*1024
U0='https://image.civitai.com/a/h0/original=true/f0.png'
U1='https://image.civitai.com/a/h1/original=true/f1.png'
U2='https://image.civitai.com/a/h2/original=true/f2.png'
V1='https://image.civitai.com/a/h1/original=true/clip1.mp4'
V2='https://image.civitai.com/a/h2/original=true/clip2.mp4'
POSTER1='https://image.civitai.com/a/h1/original=true/poster1.jpeg'

def make_api(cfg=None):
    api=webui.Api.__new__(webui.Api)
    api.cfg={'images_dir_autofill':True} if cfg is None else cfg
    api._cover_cache={}
    return api

def jpeg(color='blue',size=(60,40)):
    import io
    out=io.BytesIO();Image.new('RGB',size,color).save(out,format='JPEG');return out.getvalue()

def write_model(d,name='m',images=()):
    """temp 下的假模型 + .civitai.info（find_info_file 的真实查找顺序）。"""
    model=Path(d)/(name+'.safetensors');model.write_bytes(b'fake-model-bytes')
    (Path(d)/(name+'.civitai.info')).write_text(json.dumps({'name':name,'images':list(images)}),encoding='utf-8')
    return str(model)

def make_dir_link(link,target):
    """Windows 优先 junction（普通权限即可），否则 symlink；都创建不了返回 False（跳过该用例）。"""
    if os.name=='nt':
        try:
            import _winapi;_winapi.CreateJunction(target,link);return True
        except Exception:pass
    try:
        os.symlink(target,link,target_is_directory=True);return True
    except (OSError,SystemError):
        return False

class VideoEntryJudgementTests(unittest.TestCase):
    # 钉住第 1 条（webui 侧）：真实库 type 100% 缺失，只按 type 判会误把 mp4 当静帧
    def test_is_video_entry_matrix(self):
        f=webui._is_video_entry
        self.assertTrue(f({'url':V1,'width':1920,'height':1080}))                     # 无 type + .mp4 → 视频
        self.assertTrue(f({'type':'video','url':U0}))                                 # type 兜底
        self.assertTrue(f({'type':'video','url':U0,'imageUrl':POSTER1}))
        self.assertFalse(f({'type':'image','url':U0}))                                # type='image' → 静帧
        self.assertFalse(f({'url':U1}))
        self.assertTrue(f({'url':'https://cdn.example/V.MP4?token=abc#f'}))           # 大写尾缀 + query/fragment
        self.assertTrue(f({'url':'https://image.civitai.com/a/b/orig/clip.webm'}))
        self.assertTrue(f({'url':'https://x/c.mov'}));self.assertTrue(f({'url':'https://x/c.mkv'}));self.assertTrue(f({'url':'https://x/c.avi'}))
        self.assertTrue(f({'url':U0,'imageUrl':V2}))                                  # imageUrl 是 mp4 的情形
        self.assertFalse(f({'url':'https://x/img.jpeg?v=.mp4'}))                      # query 里的尾缀不算
        for junk in (None,'string',123,[],{}):
            self.assertFalse(f(junk))                                                 # 非 dict 不抛错

    def test_video_url_ext_and_consistency(self):
        self.assertEqual(webui.VIDEO_URL_EXTS,image_gallery.VIDEO_SUFFIXES)           # 两处尾缀表必须同口径
        self.assertTrue(webui._video_url_ext(V1).endswith('.mp4'))
        self.assertTrue(webui._video_url_ext('https://x/a/b/FILE.MP4?t=1').endswith('.mp4'))
        self.assertEqual(webui._video_url_ext(U0),'');self.assertEqual(webui._video_url_ext(''),'')
        self.assertEqual(webui._video_url_ext(None),'')
        self.assertEqual(webui._video_source_url({'url':'  https://x/a.mp4  '}),'https://x/a.mp4')
        self.assertEqual(webui._video_source_url(None),'');self.assertEqual(webui._video_source_url('x'),'')

    def test_pick_cover_url_never_returns_mp4_as_cover(self):
        self.assertEqual(webui._pick_cover_url([{'type':'video','url':V1},{'url':U0}]),U0)          # 静帧优先
        self.assertEqual(webui._pick_cover_url([{'url':V1,'imageUrl':POSTER1}]),POSTER1)            # 只剩视频 → poster
        self.assertEqual(webui._pick_cover_url([{'url':V1}]),'')                                    # 老 info 无 poster
        self.assertEqual(webui._pick_cover_url(['garbage',{'url':U0}]),U0)                          # 非 dict 不抛错
        self.assertEqual(webui._pick_cover_url(None),'')

class CapBytesTests(unittest.TestCase):
    # 钉住第 6 条（webui 侧）
    def test_default_caps(self):
        self.assertEqual(webui._image_cap_bytes(None),20*MB)
        self.assertEqual(webui._video_cap_bytes(None),100*MB)   # 主控刚从 512MB 改小，绝不测回旧值
        self.assertEqual(webui._video_cap_bytes({}),100*MB)
        self.assertEqual(webui._image_cap_bytes({}),20*MB)
        self.assertEqual(webui._image_cap_bytes({'image_max_mb':50}),50*MB)

    def test_dirty_values_fall_back_to_default(self):
        self.assertEqual(webui._image_cap_bytes({'image_max_mb':0}),20*MB)
        self.assertEqual(webui._image_cap_bytes({'image_max_mb':-7}),20*MB)
        self.assertEqual(webui._image_cap_bytes({'image_max_mb':'脏值'}),20*MB)
        self.assertEqual(webui._image_cap_bytes({'image_max_mb':None}),20*MB)
        self.assertEqual(webui._video_cap_bytes({'video_max_mb':0}),100*MB)
        self.assertEqual(webui._video_cap_bytes({'video_max_mb':'x'}),100*MB)

    def test_download_video_default_matches_video_cap(self):
        default=inspect.signature(webui._download_video).parameters['max_bytes'].default
        self.assertEqual(default,100*MB)   # 不留比设置更大的暗档

    def test_fetch_deadline_s(self):
        self.assertEqual(webui._fetch_deadline_s(None),120)
        self.assertEqual(webui._fetch_deadline_s({}),120)
        self.assertEqual(webui._fetch_deadline_s({'fetch_deadline_s':1}),5)      # 下限 5
        self.assertEqual(webui._fetch_deadline_s({'fetch_deadline_s':10**6}),3600)
        self.assertEqual(webui._fetch_deadline_s({'fetch_deadline_s':'abc'}),120)
        self.assertEqual(webui._fetch_deadline_s({'fetch_deadline_s':0}),120)

class FillDirWritableTests(unittest.TestCase):
    def test_plain_and_existing_checks(self):
        with TemporaryDirectory() as d:
            model=write_model(d)
            self.assertTrue(webui._fill_dir_writable(os.path.join(d,'m.images'),model))            # 首次补帧目录还不存在
            blocker=Path(d)/'blocker.images';blocker.write_bytes(b'not-a-dir')
            self.assertFalse(webui._fill_dir_writable(str(blocker),model))                          # 同名文件占位
            self.assertFalse(webui._fill_dir_writable(os.path.join(d,'elsewhere','m.images'),model))# 不在模型同层

    def test_symlink_or_junction_outside_is_rejected(self):
        # 注意：Python 的 os.path.islink 对 Windows junction 返回 False，拒绝靠的是「realpath 必须落在模型同层」这一支。
        # 所以夹具把外部目录放在**另一个** TemporaryDirectory（模型目录之外），与线上威胁模型一致。
        with TemporaryDirectory() as d,TemporaryDirectory() as out:
            model=write_model(d)
            external=Path(out)/'archive';external.mkdir();(external/'marker.txt').write_text('keep',encoding='utf-8')
            link=os.path.join(d,'m.images')
            if not make_dir_link(link,str(external)):self.skipTest('本机无法创建符号链接/junction')
            self.assertFalse(webui._fill_dir_writable(link,model))
            r=make_api()._start_images_autofill(model)
            self.assertFalse(r['started']);self.assertIn('已拒绝',r['reason'])
            self.assertEqual([p.name for p in external.iterdir()],['marker.txt'])                   # 目标目录零写入

class FillTargetsTests(unittest.TestCase):
    def test_naming_and_video_split(self):
        with TemporaryDirectory() as d:
            base=os.path.join(d,'m')
            images=[{'url':U0},{'url':V1,'imageUrl':POSTER1},{'url':'https://image.civitai.com/a/h3/original=true/f3.webp?token=x'}]
            t=webui._images_fill_targets(base,images)
            self.assertEqual(t,[('image',U0,os.path.join(base+'.images','image_01.png')),
                                ('image',POSTER1,os.path.join(base+'.images','image_02.jpg')),
                                ('video',V1,os.path.join(base+'.images','video_02.mp4')),
                                ('image','https://image.civitai.com/a/h3/original=true/f3.webp?token=x',os.path.join(base+'.images','image_03.webp'))])
            self.assertNotIn('video',[k for k,_,_ in webui._images_fill_targets(base,images,want_videos=False)])
            self.assertEqual(webui._images_fill_targets(base,[None,'x',{'url':None},{}]),[])  # 脏条目不抛错

    def test_mp4_without_type_never_stored_as_jpg(self):
        # 旧实现只按 type=='video' 判 → mp4 原片会被存成 image_XX.jpg，这是本次修复的核心回归点
        with TemporaryDirectory() as d:
            t=webui._images_fill_targets(os.path.join(d,'m'),[{'url':V1}])
            self.assertEqual([k for k,_,_ in t],['video'])
            self.assertTrue(t[0][2].endswith('video_01.mp4'))
            self.assertFalse(any(dest.endswith('image_01.jpg') for _,_,dest in t))

    def test_pending_targets_size_rules(self):
        with TemporaryDirectory() as d:
            img_ok=Path(d)/'image_01.jpg';img_ok.write_bytes(b'x'*50)
            img_zero=Path(d)/'image_02.jpg';img_zero.write_bytes(b'')
            vid_small=Path(d)/'video_03.mp4';vid_small.write_bytes(b'x'*500)      # <=1KB 视为截断，重下
            vid_ok=Path(d)/'video_04.mp4';vid_ok.write_bytes(b'x'*2048)
            targets=[('image',U0,str(img_ok)),('image',U1,str(img_zero)),('video',V1,str(vid_small)),('video',V2,str(vid_ok))]
            pending=webui._fill_pending_targets(targets)
            self.assertEqual([(k,u) for k,u,_ in pending],[('image',U1),('video',V1)])

class FillRunTests(unittest.TestCase):
    def test_downloads_and_state(self):
        with TemporaryDirectory() as d:
            images=[{'url':U0},{'url':V1,'imageUrl':POSTER1}]
            model=write_model(d,images=images)
            def fake_img(url,dest,**kw):Path(dest).write_bytes(b'img');return True
            def fake_vid(url,dest,**kw):Path(dest).write_bytes(b'v'*2048);return True
            state={}
            targets=webui._images_fill_targets(os.path.splitext(model)[0],images)
            with patch.object(webui,'_download_image_limited',side_effect=fake_img),patch.object(webui,'_download_video',side_effect=fake_vid),\
                 patch.object(webui.shutil,'disk_usage',return_value=Mock(free=10**12)):
                stats=webui._images_fill_run(model,targets,{'image_max_mb':20,'video_max_mb':100},state=state)
            self.assertEqual((stats['downloaded'],stats['failed'],stats['skipped']),(3,0,0))
            for name in ('image_01.png','image_02.jpg','video_02.mp4'):
                self.assertTrue(os.path.isfile(os.path.join(d,'m.images',name)),name)
            self.assertEqual(state['done'],3)
            # 幂等：全部已落盘 -> pending 为空，再跑零下载
            pending=webui._fill_pending_targets(targets)
            self.assertEqual(pending,[])

    def test_cancel_stops_remaining(self):
        with TemporaryDirectory() as d:
            model=write_model(d)
            cancel=threading.Event();cancel.set()
            with patch.object(webui,'_download_image_limited',Mock()) as dl:
                stats=webui._images_fill_run(model,[('image',U0,os.path.join(d,'m.images','image_01.png'))],{},state=None,cancel=cancel)
            self.assertEqual(stats['downloaded'],0);self.assertIn('已取消',stats['msg']);dl.assert_not_called()

    def test_total_deadline_stops_remaining(self):
        with TemporaryDirectory() as d:
            model=write_model(d)
            with patch.object(webui,'_download_image_limited',Mock()) as dl:
                stats=webui._images_fill_run(model,[('image',U0,os.path.join(d,'m.images','image_01.png'))],{},deadline_s=0)
            self.assertIn('超出总时限',stats['msg']);dl.assert_not_called()

    def test_free_space_math_and_skip(self):
        with patch.object(webui.shutil,'disk_usage',return_value=Mock(free=60*MB)):
            ok,_note=webui._fill_free_space_ok('X:/',{},'image');self.assertTrue(ok)          # 20+32=52MB < 60MB
            ok,note=webui._fill_free_space_ok('X:/',{},'video');self.assertFalse(ok)          # 100+32=132MB > 60MB
            self.assertIn('磁盘剩余',note);self.assertIn('已跳过',note)                          # msg 写明原因
        with patch.object(webui.shutil,'disk_usage',side_effect=OSError):
            ok,note=webui._fill_free_space_ok('X:/',{},'image');self.assertFalse(ok);self.assertIn('跳过',note)
        with TemporaryDirectory() as d:
            model=write_model(d)
            with patch.object(webui.shutil,'disk_usage',return_value=Mock(free=1024)),patch.object(webui,'_download_image_limited',Mock()) as dl:
                stats=webui._images_fill_run(model,[('image',U0,os.path.join(d,'m.images','image_01.png'))],{})
            self.assertEqual((stats['skipped'],stats['downloaded']),(1,0));self.assertIn('磁盘剩余',stats['msg']);dl.assert_not_called()

    def test_junction_out_dir_rejected_with_zero_external_writes(self):
        with TemporaryDirectory() as d,TemporaryDirectory() as out:
            model=write_model(d)
            external=Path(out)/'archive';external.mkdir()
            if not make_dir_link(os.path.join(d,'m.images'),str(external)):self.skipTest('本机无法创建符号链接/junction')
            with patch.object(webui,'_download_image_limited',Mock()) as dl:
                stats=webui._images_fill_run(model,[('image',U0,os.path.join(d,'m.images','image_01.png'))],{})
            self.assertEqual(stats['downloaded'],0);self.assertIn('已跳过',stats['msg'])
            self.assertEqual(list(external.iterdir()),[]);dl.assert_not_called()

class AutofillGateTests(unittest.TestCase):
    def test_switch_off_never_starts_thread(self):
        api=make_api({'images_dir_autofill':False})
        with patch.object(webui.threading,'Thread',side_effect=AssertionError('开关关闭不得起线程')):
            r=api._start_images_autofill('anything')
        self.assertFalse(r['started']);self.assertIn('关闭',r['reason'])

    def test_preconditions(self):
        api=make_api()
        r=api._start_images_autofill('Z:/nope/such.model');self.assertFalse(r['started']);self.assertIn('不存在',r['reason'])
        with TemporaryDirectory() as d:
            model=write_model(d,images=[])
            r=api._start_images_autofill(model);self.assertFalse(r['started']);self.assertIn('没有示例图',r['reason'])
            api._storage_migrating=True
            r=api._start_images_autofill(model);self.assertIn('迁移',r['reason'])
            api._storage_migrating=False
            # 本地已齐 → 不起线程（幂等：重复打开详情零成本）
            model2=write_model(d,name='m2',images=[{'url':U0}])
            imgs=Path(d)/'m2.images';imgs.mkdir();(imgs/'image_01.png').write_bytes(b'x'*40)
            with patch.object(webui.threading,'Thread',side_effect=AssertionError('本地已齐不得起线程')):
                r=api._start_images_autofill(model2)
            self.assertFalse(r['started']);self.assertIn('已齐全',r['reason'])

    def test_single_task_gate_and_registry(self):
        api=make_api()
        with TemporaryDirectory() as d:
            m1=write_model(d,'a',[{'url':U0}]);m2=write_model(d,'b',[{'url':U1}])
            thread=Mock();thread.start=Mock()
            with patch.object(webui.threading,'Thread',return_value=thread),patch.object(webui,'_images_fill_run',return_value={'msg':'补帧完成','downloaded':1,'failed':0,'skipped':0}):
                r1=api._start_images_autofill(m1);r2=api._start_images_autofill(m2);r3=api._start_images_autofill(m1)
                self.assertTrue(r1['started']);self.assertEqual(thread.start.call_count,1)
                self.assertFalse(r2['started']);self.assertIn('已有补帧任务在跑',r2['reason'])   # 全局同时只 1 个
                self.assertFalse(r3['started'])
                reg=api._autofill_registry();key=os.path.normcase(os.path.abspath(m1))
                self.assertEqual(reg['current'],key)
                # current 释放但同模型任务未结束 → 拒绝重复起
                reg['current']=''
                r4=api._start_images_autofill(m1);self.assertFalse(r4['started']);self.assertIn('已在进行',r4['reason'])
                # worker 收尾：finished + current 释放；随后允许新任务（帧未落盘则重新排队）
                job=reg['jobs'][key]
                api._images_autofill_worker(key,job,[('image',U0,os.path.join(d,'a.images','image_01.png'))])
                self.assertTrue(job['finished']);self.assertEqual(reg['current'],'')
                st=json.loads(api.get_images_autofill_state(m1))
                self.assertFalse(st['running']);self.assertEqual(st['msg'],'补帧完成')   # 状态可查、任务已收尾
                r5=api._start_images_autofill(m1)
                self.assertTrue(r5['started']);self.assertEqual(thread.start.call_count,2)     # 任务结束后闸门释放

    def test_state_and_cancel_rpc(self):
        api=make_api()
        with TemporaryDirectory() as d:
            m1=write_model(d,'a',[{'url':U0}])
            with patch.object(webui.threading,'Thread',return_value=Mock()):
                api._start_images_autofill(m1)
            reg=api._autofill_registry();key=os.path.normcase(os.path.abspath(m1))
            st=json.loads(api.get_images_autofill_state())        # 不带 path -> 当前任务
            self.assertTrue(st['running']);self.assertEqual(st['total'],1)
            c=json.loads(api.cancel_images_autofill(m1))
            self.assertTrue(c['canceled']);self.assertTrue(reg['jobs'][key]['cancel'].is_set())
            self.assertIn('没有正在进行的补帧任务',json.loads(api.cancel_images_autofill(os.path.join(d,'ghost.safetensors')))['msg'])

class DownloadAllImagesTests(unittest.TestCase):
    def test_guardrails(self):
        api=make_api()
        self.assertIn('file not exists',json.loads(api.download_all_images(''))['msg'])
        with TemporaryDirectory() as d,TemporaryDirectory() as out:
            model=write_model(d,images=[])
            self.assertIn('no images in info',json.loads(api.download_all_images(model))['msg'])
            model2=write_model(d,'m2',[{'url':U0}])
            external=Path(out)/'archive';external.mkdir()
            if not make_dir_link(os.path.join(d,'m2.images'),str(external)):self.skipTest('本机无法创建符号链接/junction')
            r=json.loads(api.download_all_images(model2))
            self.assertFalse(r['ok']);self.assertIn('已拒绝',r['msg'])
            self.assertEqual(list(external.iterdir()),[])

    def test_happy_then_idempotent(self):
        api=make_api()
        with TemporaryDirectory() as d:
            model=write_model(d,images=[{'url':U0},{'url':V1,'imageUrl':POSTER1}])
            calls=[]
            def fake_img(url,dest,**kw):
                Path(dest).write_bytes(b'img');calls.append(('image',kw.get('max_bytes')));return True
            def fake_vid(url,dest,**kw):
                Path(dest).write_bytes(b'v'*2048);calls.append(('video',kw.get('max_bytes')));return True
            with patch.object(webui,'_download_image_limited',side_effect=fake_img),patch.object(webui,'_download_video',side_effect=fake_vid),\
                 patch.object(webui.shutil,'disk_usage',return_value=Mock(free=10**12)):
                r=json.loads(api.download_all_images(model))
            self.assertTrue(r['ok']);self.assertEqual(r['downloaded'],3)
            self.assertEqual(sorted(calls),sorted([('image',20*MB),('image',20*MB),('video',100*MB)]))
            for name in ('image_01.png','image_02.jpg','video_02.mp4'):
                self.assertTrue((Path(d)/'m.images'/name).is_file(),name)
            self.assertEqual(json.loads(api.get_img_dl_state())['done'],3)
            with patch.object(webui,'_download_image_limited',side_effect=AssertionError('已存在不得重下')),\
                 patch.object(webui,'_download_video',side_effect=AssertionError('已存在不得重下')):
                r2=json.loads(api.download_all_images(model))
            self.assertTrue(r2['ok']);self.assertEqual(r2['downloaded'],3)

class CoversMultiTests(unittest.TestCase):
    def frames(self,api,d,images,size=220,n=3,name='m'):
        model=write_model(d,name,images)
        return json.loads(api.get_covers_multi([model],size=size,n=n))[model],model

    def test_local_first_no_remote_for_local_slots(self):
        api=make_api()
        with TemporaryDirectory() as d,patch.object(webui.posix_compat,'thumb_cache_dir',return_value=None):
            imgs=Path(d)/'m.images';imgs.mkdir()
            Image.new('RGB',(50,50),'red').save(imgs/'image_01.jpg')
            Image.new('RGB',(50,50),'green').save(imgs/'image_02.jpg')
            items,_=self.frames(api,d,[{'url':U0},{'url':U1},{'url':U2}])
        self.assertEqual(len(items),3)
        self.assertTrue(all(u.startswith('data:') for u,_ in items[:2]),items)
        self.assertEqual(items[2],['https://image.civitai.com/a/h2/width=460/f2.png',False])
        self.assertFalse(any('f0.png' in u or 'f1.png' in u for u,_ in items))  # 本地已落盘序号不再出远程 URL，同帧不重复
        self.assertFalse(any(v for _,v in items))

    def test_only_image_02_local_cover_still_suppresses_remote(self):
        # 同上口径的边界：<名>.images/ 只有 image_02.jpg（首帧缺、二帧在）时，
        # 该 local 封面即 info[1] 那帧，远程 f1 URL 也不应再出现（本地优先、同帧不重复）。
        # 曾以 xfail 钉住：封面只按文件路径 emitted 去重，序号分支条件落空后穿透到远程分支；
        # 现在按序号槽位抑制（cover_slot），这条转为正式回归守卫。
        api=make_api()
        with TemporaryDirectory() as d,patch.object(webui.posix_compat,'thumb_cache_dir',return_value=None):
            imgs=Path(d)/'m.images';imgs.mkdir()
            Image.new('RGB',(50,50),'green').save(imgs/'image_02.jpg')
            items,_=self.frames(api,d,[{'url':U0},{'url':U1}])
        self.assertFalse(any('f1.png' in u for u,_ in items),items)

    def test_all_video_model_returns_playable_src(self):
        api=make_api()
        with TemporaryDirectory() as d,patch.object(webui.posix_compat,'thumb_cache_dir',return_value=None):
            items,_=self.frames(api,d,[{'url':V1}])
            self.assertEqual(items,[[V1,True]])                        # 远程 mp4 直链 + isVideo=True，绝不 thumb 化/绝不塞给 <img>
            model2=write_model(d,'m2',[{'url':V2}])
            vdir=Path(d)/'m2.images';vdir.mkdir();(vdir/'video_01.mp4').write_bytes(b'f'*4096)
            items2=json.loads(api.get_covers_multi([model2],size=220,n=3))[model2]
        self.assertEqual(len(items2),1)
        self.assertTrue(items2[0][0].startswith('file:///'),items2);self.assertTrue(items2[0][1])

class DetailAndBridgeTests(unittest.TestCase):
    def test_get_model_detail_video_entry_and_autofill_hook(self):
        api=make_api();api.cfg={}
        with TemporaryDirectory() as d:
            model=write_model(d,images=[{'url':U0},{'url':V1}])
            api._start_images_autofill=Mock(return_value={'started':False,'reason':'fixture'})
            result=json.loads(api.get_model_detail(model))
        covers=result['covers']
        by_url={c.get('url'):c for c in covers}
        self.assertEqual(by_url[U0]['type'],'image')
        self.assertEqual(by_url[V1]['type'],'video')                    # mp4 出 video 条目，不进 PIL 缩略管线
        self.assertEqual(by_url[V1]['orig_url'],V1)
        api._start_images_autofill.assert_called_once_with(model,'detail')  # 打开详情顺带排队补帧

    def test_gallery_preview_forwards_display_tier(self):
        self.assertEqual(webui._gallery_display_tier({}),(1440,80))
        self.assertEqual(webui._gallery_display_tier(None),(1440,80))
        self.assertEqual(webui._gallery_display_tier({'gallery_display_width':'脏'}),(1440,80))
        self.assertEqual(webui._gallery_display_tier({'gallery_display_width':10,'gallery_display_quality':20}),(256,40))
        self.assertEqual(webui._gallery_display_tier({'gallery_display_width':99999,'gallery_display_quality':200}),(4096,100))
        item={'url':U0,'width':2048};cfg={'gallery_display_width':1440,'gallery_display_quality':80}
        with patch.object(image_gallery,'preview',return_value={'ok':True}) as p:
            r=webui._gallery_preview(item,cfg)
        self.assertEqual(p.call_args.kwargs,{'max_width':1440,'quality':80});self.assertTrue(r['ok'])

        def legacy(item,cfg):return {'ok':'legacy-default-tier'}     # 上游没接档位时退回其默认，不丢帧
        with patch.object(image_gallery,'preview',legacy):
            self.assertEqual(webui._gallery_preview(item,cfg),{'ok':'legacy-default-tier'})

    def test_get_gallery_image_goes_through_tier_bridge(self):
        api=make_api();api._gallery_item=Mock(return_value={'url':U0,'width':2048})
        with patch.object(webui,'_gallery_preview',return_value={'ok':True,'downgraded':True}) as g:
            r=api.get_gallery_image('m.safetensors',0)
        self.assertTrue(r['ok']);self.assertEqual(g.call_args[0][1],api.cfg)
        with patch.object(webui,'_gallery_preview',side_effect=OSError('cdn down')):
            self.assertFalse(api.get_gallery_image('m.safetensors',0)['ok'])

    def test_info_images_normalization(self):
        api=make_api()
        with TemporaryDirectory() as d:
            model=write_model(d,images=[{'url':U0},'garbage',None])
            self.assertEqual(api._info_images(model),[{'url':U0}])
            (Path(d)/'m.civitai.info').write_text('{半截json',encoding='utf-8')
            self.assertEqual(api._info_images(model),[])
            self.assertEqual(api._info_images(os.path.join(d,'ghost.safetensors')),[])

class ConfigDefaultsTests(unittest.TestCase):
    # 钉住第 8/6 条的配置面
    def test_defaults_and_clamps(self):
        for key,value in [('gallery_cache_mb',8192),('image_max_mb',20),('video_max_mb',100),('fetch_deadline_s',120),
                          ('gallery_display_width',1440),('gallery_display_quality',80),('images_dir_autofill',True)]:
            self.assertEqual(config.DEFAULTS[key],value)
        cfg={'gallery_display_width':100,'gallery_display_quality':200,'image_max_mb':0,'video_max_mb':99999,
             'fetch_deadline_s':1,'gallery_cache_mb':1,'images_dir_autofill':False,'gallery_display_width2':0}
        config.normalize_ui_preferences(cfg)
        self.assertEqual((cfg['gallery_display_width'],cfg['gallery_display_quality']),(256,100))
        self.assertEqual((cfg['image_max_mb'],cfg['video_max_mb']),(1,4096))
        self.assertEqual((cfg['fetch_deadline_s'],cfg['gallery_cache_mb']),(5,64))
        self.assertFalse(cfg['images_dir_autofill'])
        cfg2={'gallery_display_width':'脏','gallery_display_quality':None,'fetch_deadline_s':'脏','images_dir_autofill':None}
        config.normalize_ui_preferences(cfg2)
        self.assertEqual((cfg2['gallery_display_width'],cfg2['gallery_display_quality'],cfg2['fetch_deadline_s']),(1440,80,120))
        self.assertTrue(cfg2['images_dir_autofill'])   # 只有显式 false 才关

if __name__=='__main__':unittest.main()
