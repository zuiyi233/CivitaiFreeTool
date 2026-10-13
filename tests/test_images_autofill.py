# -*- coding: utf-8 -*-
"""图片加载性能改动回归网 —— webui 侧（视频判定/体积上限/补帧幂等/本地优先）。

钉住任务清单第 1、6、8 条及第 3、4、7、9 条的 webui 接线。全部临时目录 + Mock，
不触网、不写用户配置、不碰真实模型库；api_key 一律 fixture 假值。"""
import inspect,json,os,shutil,sys,threading,time,unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
import config,image_gallery,webui


class FakeResp:
    """可控假响应：同时支持 r.read() 与 r.read(size)，这样 pre-fix(整读)与 post-fix(流式)都能喂。"""
    def __init__(self,data,headers=None):self.data=data;self.pos=0;self.headers=headers or {}
    def read(self,size=None):
        if size is None:
            piece=self.data[self.pos:];self.pos=len(self.data);return piece
        piece=self.data[self.pos:self.pos+size];self.pos+=len(piece);return piece
    def __enter__(self):return self
    def __exit__(self,*a):return False

def fake_opener(data,headers=None):
    opener=Mock();opener.open=Mock(return_value=FakeResp(data,headers));return opener

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

    def test_video_ext_tables_stay_in_sync(self):
        import reverse_parse
        # 尾缀表在两处（reverse_parse 判定 / image_gallery 显示档拒写），不同步就会出现
        # 「一边判视频、一边当静帧送去 PIL 解码」。webui 侧不再自己留一份。
        self.assertEqual(reverse_parse.VIDEO_URL_EXTS,image_gallery.VIDEO_SUFFIXES)
        self.assertFalse(hasattr(webui,'VIDEO_URL_EXTS'))
        self.assertFalse(hasattr(webui,'_video_url_ext'))
        self.assertEqual(webui._video_source_url({'url':'  https://x/a.mp4  '}),'https://x/a.mp4')
        self.assertEqual(webui._video_source_url(None),'');self.assertEqual(webui._video_source_url('x'),'')

    def test_gui_cover_path_never_picks_mp4(self):
        """gui.py:442 直接调 reverse_parse.pick_cover_url（不经 webui），必须在共享判定里一起修好。"""
        import reverse_parse
        self.assertEqual(reverse_parse.pick_cover_url([{'url':V1},{'url':U0}]),U0)      # 无 type 的 mp4 不占封面
        self.assertEqual(reverse_parse.pick_cover_url([{'url':V1}]),'')                 # 只剩视频且无 poster → 无封面
        self.assertEqual(reverse_parse.pick_cover_url([{'url':V1,'imageUrl':POSTER1}]),POSTER1)
        # webui 判定必须是同一份实现（否则两处会再次分叉）
        for entry in ({'url':V1},{'url':U0},{'type':'video','url':U0},{'url':POSTER1}):
            self.assertEqual(webui._is_video_entry(entry),reverse_parse.is_video_image_entry(entry))

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
            t=webui._images_fill_targets(os.path.join(d,'m'),[{'url':V1}],want_posters=False)
            self.assertEqual([k for k,_,_ in t],['video'])
            self.assertTrue(t[0][2].endswith('video_01.mp4'))
            self.assertFalse(any(dest.endswith('image_01.jpg') for _,_,dest in t))

    def test_poster_target_is_local_mp4_not_remote_url(self):
        # 首帧目标与上面同图（image_01.jpg）但语义不同：kind='poster'，源是**本地 mp4 路径**，
        # 由 video_thumb 取帧，绝不把远程 mp4 直链交给图片下载器。
        # want_posters 默认跟 video_thumb.available() 走，测试必须显式给定，否则同一行断言
        # 在带 native DLL 的机器上与不带它的机器上会得出不同结论（门禁曾因此在 Windows 上转红）。
        with TemporaryDirectory() as d:
            base=os.path.join(d,'m');out=os.path.join(base+'.images')
            t=webui._images_fill_targets(base,[{'url':V1}],want_posters=True)
            self.assertEqual([k for k,_,_ in t],['video','poster'])
            self.assertEqual(t[1][1],os.path.join(out,'video_01.mp4'))
            self.assertEqual(t[1][2],os.path.join(out,'image_01.jpg'))
            self.assertFalse(any(k=='image' and u==V1 for k,u,_ in t))
            self.assertEqual(webui._images_fill_targets(base,[{'url':V1}],want_videos=False,want_posters=True),
                             [])                                            # 本次没落原片、磁盘上也没有 → 不凭空挂首帧

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

    def test_poster_skipped_when_its_video_never_landed(self):
        # 首帧依赖原片：_download_video 走 .tmp + 原子替换，失败即不留文件。这时 poster 记「跳过」，
        # 绝不能再记一次「失败」——一次下载故障被计成两个坏消息，界面还会把它说成"本机取不到首帧"。
        with TemporaryDirectory() as d:
            model=write_model(d); base=os.path.splitext(model)[0]
            mp4=os.path.join(base+'.images','video_01.mp4')
            targets=[('video',V1,mp4),('poster',mp4,os.path.join(base+'.images','image_01.jpg'))]
            with patch.object(webui,'_download_video',return_value=False) as dv,\
                 patch('video_thumb.first_frame',return_value=False) as ff,\
                 patch.object(webui.shutil,'disk_usage',return_value=Mock(free=10**12)):
                stats=webui._images_fill_run(model,targets,{'video_max_mb':100})
            self.assertEqual((stats['downloaded'],stats['failed'],stats['skipped']),(0,1,1),stats)
            dv.assert_called_once(); ff.assert_not_called()
            self.assertIn('视频原片没落成',stats['msg'],stats['msg'])

    def test_partial_run_does_not_hide_failures(self):
        # 有帧成、有帧没成：只报「补帧完成」等于把没落成的瞒下来，界面得能把坏消息说全。
        with TemporaryDirectory() as d:
            model=write_model(d); base=os.path.splitext(model)[0]
            targets=[('image',U0,os.path.join(base+'.images','image_01.png')),
                     ('image',U1,os.path.join(base+'.images','image_02.png'))]
            def half(url,dest,**kw):
                Path(dest).parent.mkdir(exist_ok=True);Path(dest).write_bytes(b'img');return url==U0
            with patch.object(webui,'_download_image_limited',side_effect=half),\
                 patch.object(webui.shutil,'disk_usage',return_value=Mock(free=10**12)):
                stats=webui._images_fill_run(model,targets,{})
            self.assertEqual((stats['downloaded'],stats['failed']),(1,1),stats)
            self.assertIn('1 帧失败',stats['msg'],stats['msg'])

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
            self.assertTrue(r['ok']);self.assertEqual((r['downloaded'],r['existing']),(3,0))
            self.assertEqual(sorted(calls),sorted([('image',20*MB),('image',20*MB),('video',100*MB)]))
            for name in ('image_01.png','image_02.jpg','video_02.mp4'):
                self.assertTrue((Path(d)/'m.images'/name).is_file(),name)
            self.assertEqual(json.loads(api.get_img_dl_state())['done'],3)
            with patch.object(webui,'_download_image_limited',side_effect=AssertionError('已存在不得重下')),\
                 patch.object(webui,'_download_video',side_effect=AssertionError('已存在不得重下')):
                r2=json.loads(api.download_all_images(model))
            self.assertTrue(r2['ok']);self.assertEqual((r2['downloaded'],r2['existing']),(0,3))  # 已存在单独计，不虚报新下载

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
            # 远程 mp4 直链 + isVideo=True + 第三格 poster（这条 info 没有 imageUrl，所以是空串）；
            # 绝不 thumb 化、绝不塞给 <img>。
            self.assertEqual(items,[[V1,True,'',0]])   # 第四格 = info.images[] 序号，抓帧回传按它落盘
            # info 带 imageUrl（新 info）→ poster 给到缩略档 URL，前端 <video poster> 与自动轮播都靠它
            items_p,_=self.frames(api,d,[{'url':V1,'imageUrl':POSTER1}])
            self.assertEqual(len(items_p),1)
            self.assertTrue(items_p[0][1])
            self.assertIn('width=%d'%webui._WATERFALL_THUMB_W,items_p[0][2],items_p[0][2])
            self.assertIn('poster1',items_p[0][2])
            model2=write_model(d,'m2',[{'url':V2}])
            vdir=Path(d)/'m2.images';vdir.mkdir();(vdir/'video_01.mp4').write_bytes(b'f'*4096)
            items2=json.loads(api.get_covers_multi([model2],size=220,n=3))[model2]
        self.assertEqual(len(items2),1)
        self.assertTrue(items2[0][0].startswith('file:///'),items2);self.assertTrue(items2[0][1])

    def test_local_first_frame_file_becomes_video_poster(self):
        # 缺陷 2 的本地侧：<名>.images/image_01.jpg 就是 video_01.mp4 的首帧（补帧/前端抓帧落的约定名）。
        # 以前本地 mp4 分支直接 return，连 poster 都不给 → 起播前一片空白。
        api=make_api()
        with TemporaryDirectory() as d,patch.object(webui.posix_compat,'thumb_cache_dir',return_value=None):
            model=write_model(d,'m',[{'url':V1}])
            idir=Path(d)/'m.images';idir.mkdir()
            (idir/'video_01.mp4').write_bytes(b'f'*4096)
            Image.new('RGB',(60,40),'red').save(idir/'image_01.jpg')
            items=json.loads(api.get_covers_multi([model],size=220,n=3))[model]
        # 视频项的首帧静图只归它自己：同一份字节不再以「独立静帧」身份出第二次。曾按字典序把
        # image_01.jpg 抓成封面挂在第 1 帧、视频帧再挂一份 poster —— 载荷翻倍，还把一段视频
        # 伪装成一帧照片（卡片主位已由 get_covers 出这张图，轮播里再叠一次纯属重复）。
        self.assertEqual(len(items),1,items)
        frame=items[0]
        self.assertTrue(frame[1],'视频帧仍要标 isVideo')
        self.assertTrue(frame[0].startswith('file:///'),frame[0])
        self.assertEqual(len(frame),4,'视频帧必须带 poster 与 info 序号两格')
        self.assertEqual(frame[3],0,'序号错一位就会把首帧写进别的帧的文件名')
        self.assertTrue(frame[2].startswith('data:image/jpeg;base64,'),frame[2][:60])

    def test_still_frames_still_lead_the_carousel(self):
        # 反面对照：视频在 info 里排第 2 帧时，第 1 帧的本地静帧照旧先出，首帧静图不会抢它的位。
        api=make_api()
        with TemporaryDirectory() as d,patch.object(webui.posix_compat,'thumb_cache_dir',return_value=None):
            model=write_model(d,'m',[{'url':U0},{'url':V1}])
            idir=Path(d)/'m.images';idir.mkdir()
            (idir/'video_02.mp4').write_bytes(b'f'*4096)
            Image.new('RGB',(60,40),'red').save(idir/'image_02.jpg')
            Image.new('RGB',(60,40),'blue').save(idir/'image_01.png')
            items=json.loads(api.get_covers_multi([model],size=220,n=3))[model]
        self.assertEqual(len(items),2,items)
        self.assertFalse(items[0][1],items[0])                          # 第 1 帧是静帧封面
        self.assertTrue(items[1][1],items[1])                           # 第 2 帧是视频
        self.assertEqual(items[1][3],1,'视频帧序号按 image_02 认，不是按张数')
        self.assertTrue(items[1][2].startswith('data:image/jpeg;base64,'))

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

    def test_video_stays_in_covers_after_its_poster_lands(self):
        # 补帧把首帧落成 image_01.jpg 之后，第二次打开详情绝不能因此把视频从 covers 里挤掉。
        # 曾按「本地图片张数」倒推占位序号，视频项的首帧静图也被算成一个静帧槽位，于是远程循环
        # `if i in local_img_slots: continue` 正好跳过那条视频——首帧刚落盘，视频反倒整个消失，
        # 而触发它的正是默认开启的首次打开补帧（等于本改动自己把自己要修的画面删了）。
        api=make_api();api.cfg={}
        with TemporaryDirectory() as d:
            model=write_model(d,'m',[{'url':V1}])
            idir=Path(d)/'m.images';idir.mkdir()
            (idir/'video_01.mp4').write_bytes(b'f'*4096)
            Image.new('RGB',(60,40),'red').save(idir/'image_01.jpg')
            api._start_images_autofill=Mock(return_value={'started':False,'reason':'fixture'})
            covers=json.loads(api.get_model_detail(model))['covers']
        vids=[c for c in covers if c.get('type')=='video']
        self.assertEqual(len(vids),1,covers)
        self.assertTrue(vids[0]['url'].startswith('file:///'),vids[0]['url'])
        self.assertTrue(vids[0]['poster'].startswith('data:image/jpeg;base64,'),'首帧静图要跟着视频项一起出')
        self.assertEqual(len(covers),1,'首帧静图不许再立一张独立静帧封面（同一份字节出两次）')

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

class GuiDownloadCapTests(unittest.TestCase):
    # finding #1：gui 下载链路（设为封面 / 封面下载）必须与 webui/image_gallery 共用同一档上限与时限
    def test_consults_shared_cap_and_deadline(self):
        import gui
        with TemporaryDirectory() as d:
            dest=os.path.join(d,'cover.png')
            resp=FakeResp(b'\x89PNG'+b'x'*400)
            opener=Mock();opener.open=Mock(return_value=resp)
            cap=Mock(return_value=20*MB);dl=Mock(return_value=120.0)
            with patch('civitai_api.build_opener',return_value=opener),\
                 patch.object(image_gallery,'static_limit_bytes',cap),\
                 patch.object(image_gallery,'fetch_deadline_seconds',dl):
                ok=gui._download_image(U0,dest,cfg={'image_max_mb':20,'fetch_deadline_s':120})
            self.assertTrue(ok)
            cap.assert_called()   # 复用共享上限实现，不再自己 r.read() 无限读
            dl.assert_called()    # wall-clock 截止时间也来自同一处

    def test_respects_size_cap(self):
        import gui
        with TemporaryDirectory() as d:
            dest=os.path.join(d,'cover.png')
            # 2MB 的 PNG-magic 流，但 image_max_mb=1 → 必须中止、绝不落盘
            resp=FakeResp(b'\x89PNG'+b'x'*(2*MB))
            opener=Mock();opener.open=Mock(return_value=resp)
            with patch('civitai_api.build_opener',return_value=opener):
                ok=gui._download_image(U0,dest,cfg={'image_max_mb':1})
            self.assertFalse(ok)
            self.assertFalse(os.path.exists(dest),'超上限不得写出文件')

class SetModelCoverGuardTests(unittest.TestCase):
    # finding #2：设为封面写进用户模型目录，必须走补帧引入的 可写/重解析点 守卫
    def test_rejects_junction_images_dir(self):
        api=make_api();api.cfg={}
        with TemporaryDirectory() as d,TemporaryDirectory() as out:
            model=write_model(d)
            external=Path(out)/'archive';external.mkdir();(external/'marker.txt').write_text('keep',encoding='utf-8')
            idir=os.path.join(d,'m.images')
            if not make_dir_link(idir,str(external)):self.skipTest('本机无法创建符号链接/junction')
            def fake_dl(url,dest,**kw):Path(dest).write_bytes(b'cover');return True
            # 本地无图 + 在线 URL → 旧实现会顺着 junction 把封面写到模型目录之外
            with patch.object(webui,'_download_image',side_effect=fake_dl):
                r=api.set_model_cover(model,'',img_url=U0)
            self.assertFalse(r['ok'])
            self.assertEqual([p.name for p in external.iterdir()],['marker.txt'],'守卫必须在下载前拦住，外部目录零写入')

    def test_normal_local_cover_still_succeeds(self):
        api=make_api();api.cfg={}
        with TemporaryDirectory() as d:
            model=write_model(d)
            base=os.path.splitext(model)[0]
            local=Path(d)/'pick.png';Image.new('RGB',(50,50),'red').save(local)
            r=api.set_model_cover(model,str(local))
            self.assertTrue(r['ok'],r)
            self.assertTrue(os.path.isfile(base+'.preview.png'))

class ExclusiveTmpTests(unittest.TestCase):
    """finding #3 的落地口径：独占创建要挡的是「目录 / 链接」这类预置陷阱；上次中断留下的普通 .tmp
    必须清掉重来——一律拒绝会让那一张图永远补不进去（补帧每次打开详情都会重试同一批目标）。"""

    def _run(self, fn, dest, data):
        with patch('civitai_api.build_opener', return_value=fake_opener(data)):
            return fn(U0, dest, proxy=None, verify=True)

    def _writers(self):
        import gui
        return ((gui._download_image, 'c.png', b'\x89PNG' + b'x' * 400),
                (webui._download_image_limited, 'c.png', b'\x89PNG' + b'x' * 400),
                (webui._download_video, 'c.mp4', b'v' * 4096))

    def test_stale_plain_tmp_is_reclaimed(self):
        for fn, name, data in self._writers():
            with self.subTest(fn.__name__):
                with TemporaryDirectory() as d:
                    dest = os.path.join(d, name); tmp = Path(dest + '.tmp')
                    tmp.write_bytes(b'STALE-FROM-KILLED-PROCESS')
                    self.assertTrue(self._run(fn, dest, data), '普通残留 .tmp 必须清掉重来，不能永久失败')
                    self.assertTrue(os.path.isfile(dest))
                    self.assertFalse(tmp.exists())

    def test_directory_at_tmp_is_refused_without_touching_anything(self):
        for fn, name, data in self._writers():
            with self.subTest(fn.__name__):
                with TemporaryDirectory() as d:
                    dest = os.path.join(d, name); os.mkdir(dest + '.tmp')
                    self.assertFalse(self._run(fn, dest, data))
                    self.assertFalse(os.path.exists(dest))
                    self.assertTrue(os.path.isdir(dest + '.tmp'), '拒绝时绝不能删掉别人的东西')

    def test_symlink_tmp_is_refused_and_left_in_place(self):
        # 旧兜底是无条件 exists+unlink：它会把这根链接本身删掉，守卫沦为静默删除
        for fn, name, data in self._writers():
            with self.subTest(fn.__name__):
                with TemporaryDirectory() as d:
                    outside = Path(d) / 'outside.bin'; outside.write_bytes(b'victim')
                    link = os.path.join(d, name + '.tmp')
                    try:
                        os.symlink(str(outside), link)
                    except (OSError, SystemError):
                        self.skipTest('本机无法创建文件符号链接（需开发者模式或管理员）')
                    dest = os.path.join(d, name)
                    self.assertFalse(self._run(fn, dest, data))
                    self.assertTrue(os.path.islink(link), '拒绝时不能顺手删掉预置的链接')
                    self.assertEqual(outside.read_bytes(), b'victim')
                    self.assertFalse(os.path.exists(dest))

class DownloadAllImagesHonestCountsTests(unittest.TestCase):
    # finding #4：download_all_images 不得把「已存在」计入「新下载」
    def test_new_and_existing_reported_separately(self):
        api=make_api()
        with TemporaryDirectory() as d:
            model=write_model(d,images=[{'url':U0},{'url':U1},{'url':U2}])
            def fake_img(url,dest,**kw):Path(dest).write_bytes(b'img');return True
            with patch.object(webui,'_download_image_limited',side_effect=fake_img),\
                 patch.object(webui.shutil,'disk_usage',return_value=Mock(free=10**12)):
                r=json.loads(api.download_all_images(model))
            self.assertTrue(r['ok'])
            self.assertEqual((r['downloaded'],r['existing']),(3,0),'首轮全是新下载')
            # 幂等第二轮：0 新下载、3 已存在
            with patch.object(webui,'_download_image_limited',side_effect=AssertionError('已存在不得重下')):
                r2=json.loads(api.download_all_images(model))
            self.assertTrue(r2['ok'])
            self.assertEqual((r2['downloaded'],r2['existing']),(0,3),'已存在必须单独计，不得混进新下载')

class RemoteThumbCacheTests(unittest.TestCase):
    """缺陷 1：瀑布流的远程帧以前只把 CDN 直链交给浏览器，软件侧零持久化 —— 第二次打开照样从头拉。
    现在同一档字节要永久落盘，第二次由 get_covers_multi 直接出 data:，且完全不再发请求。"""

    def _opener(self,data=None):
        resp=FakeResp(data if data is not None else jpeg())
        opener=Mock();opener.open=Mock(return_value=resp);return opener

    def test_second_read_is_pure_disk(self):
        api=make_api()
        with TemporaryDirectory() as d,patch.object(webui.config,'APP_DIR',d),\
             patch.object(webui.posix_compat,'thumb_cache_dir',return_value=None),\
             patch('urllib.request.build_opener',return_value=self._opener()):
            model=write_model(d,'m',[{'url':U0}])
            first=json.loads(api.get_covers_multi([model],size=220,n=3))[model]
            self.assertTrue(first[0][0].startswith('https://'),first)     # 未入盘：仍给 CDN 缩略档，别拖慢首屏
            self.assertIn('width=%d'%webui._WATERFALL_THUMB_W,first[0][0])
            api.get_cover_b64(U0,webui._WATERFALL_THUMB_W)                # 浏览器/后台入盘队列做的同一件事
            with patch('urllib.request.build_opener',side_effect=AssertionError('命中磁盘缓存后不得再发请求')):
                second=json.loads(api.get_covers_multi([model],size=220,n=3))[model]
        self.assertTrue(second[0][0].startswith('data:image/jpeg;base64,'),second[0][0][:40])

    def test_cache_key_size_is_the_served_width(self):
        # 浏览器拿的那一档必须与我们落盘/查键的那一档同一个数，否则第二次永远不命中
        api=make_api()
        with TemporaryDirectory() as d,patch.object(webui.config,'APP_DIR',d),\
             patch.object(webui.posix_compat,'thumb_cache_dir',return_value=None),\
             patch('urllib.request.build_opener',return_value=self._opener()):
            api.get_cover_b64(U0,webui._WATERFALL_THUMB_W)
            hit=api._url_thumb_b64(U0,webui._WATERFALL_THUMB_W)
            miss=api._url_thumb_b64(U0,512)
            self.assertTrue(hit)
            self.assertEqual(miss,'','换一档就不该命中间一条缓存')
            self.assertTrue(os.path.isfile(api._detail_cache_path(U0,webui._WATERFALL_THUMB_W)))

    def test_oversize_tier_clamps_to_the_same_key_on_both_sides(self):
        # 夹取只在 _detail_cache_path 里做一次：读侧以前原样拼键、写侧先夹到 64..1024，
        # 越界的 size 于是永远读不到自己刚写下的那张（第二次打开照样跑网络）。
        api=make_api()
        self.assertEqual(api._detail_cache_path(U0,5000),api._detail_cache_path(U0,webui._THUMB_SIZE_MAX))
        self.assertEqual(api._detail_cache_path(U0,1),api._detail_cache_path(U0,webui._THUMB_SIZE_MIN))
        self.assertEqual(webui._thumb_size('脏'),webui._THUMB_SIZE_MAX)
        self.assertEqual(webui._thumb_size(None),webui._THUMB_SIZE_MAX)

    def test_detail_tier_is_what_the_front_end_asks_for(self):
        # 详情缩略条：后端出帧档位 == get_cover_b64 的默认 size == 前端补图那一档。
        # 三个数分叉时同一帧会被取两次、落盘两份，第二次打开详情还得再跑一趟网络。
        import inspect
        self.assertEqual(webui._DETAIL_THUMB_W,
                         inspect.signature(webui.Api.get_cover_b64).parameters['size'].default)
        self.assertEqual(webui._DETAIL_THUMB_W,webui._thumb_size(webui._DETAIL_THUMB_W))

    def test_image_max_mb_does_not_gate_the_thumbnail_fetch(self):
        # config.image_max_mb 管的是原图归档预算，能被设到 1MB。曾拿它当缩略图上限，
        # 设置一低就 `len(data) > cap` 直接返回 ''，瀑布流与详情的缩略图整片空白（静默的功能缺失）。
        big=Image.frombytes('RGB',(1200,1200),os.urandom(1200*1200*3))
        buf=__import__('io').BytesIO();big.save(buf,format='JPEG',quality=95);blob=buf.getvalue()
        self.assertGreater(len(blob),1024*1024,'夹具得真过 1MB 才钉得住这条')
        api=make_api({'image_max_mb':1})
        with TemporaryDirectory() as d,patch.object(webui.config,'APP_DIR',d),\
             patch.object(webui.posix_compat,'thumb_cache_dir',return_value=None),\
             patch('urllib.request.build_opener',return_value=self._opener(blob)):
            b64=api.get_cover_b64(U0,webui._WATERFALL_THUMB_W)
            self.assertTrue(b64,'原图上限压不到缩略图')
            self.assertEqual(api._url_thumb_b64(U0,webui._WATERFALL_THUMB_W),b64)

    def test_warm_requeues_after_the_queue_drains(self):
        # pending 必须在处理完后清掉：只加不减会让 256 键攒满，之后整条入盘链路永久哑掉。
        # 顺带钉住收工判定的锁顺序——put 挪进锁内、worker 取锁后再看队列，
        # 否则「worker 刚置 None 收工、这一单才被挂上」会让键留在 pending 里永远没人取。
        api=make_api()
        calls=[]
        with TemporaryDirectory() as d,patch.object(webui.config,'APP_DIR',d),\
             patch.object(api,'get_cover_b64',side_effect=lambda u,size: calls.append((u,size)) or 'x'):
            api._warm_url_thumb(U0,460)
            for _ in range(100):
                if calls:break
                time.sleep(0.05)
            reg=api._thumb_warm_registry()
            self.assertEqual(calls,[(U0,460)],calls)
            for _ in range(100):
                if not reg['pending']:break
                time.sleep(0.05)
            self.assertEqual(reg['pending'],set(),'取完就还，队列才排得进下一次')
            api._warm_url_thumb(U0,460)               # 同一个 URL 第二次仍要排得进去
            for _ in range(100):
                if len(calls)>=2:break
                time.sleep(0.05)
        self.assertEqual(calls,[(U0,460),(U0,460)],calls)

    def test_warm_queue_dedups_and_persists(self):
        api=make_api()
        calls=[]
        with TemporaryDirectory() as d,patch.object(webui.config,'APP_DIR',d),\
             patch.object(api,'get_cover_b64',side_effect=lambda u,size: calls.append((u,size)) or 'x'):
            api._warm_url_thumb(U0,460)
            api._warm_url_thumb(U0,460)          # 同一 URL 只排一次
            api._warm_url_thumb(U1,460)
            for _ in range(100):
                if len(calls)>=2:break
                time.sleep(0.05)
        self.assertEqual(sorted(calls),[(U0,460),(U1,460)],calls)

    def test_switch_off_disables_read_and_warm(self):
        api=make_api({'images_dir_autofill':False,'cache_detail_images':False})
        with TemporaryDirectory() as d,patch.object(webui.config,'APP_DIR',d),\
             patch.object(api,'get_cover_b64',return_value='ignored'):
            api._warm_url_thumb(U0,460)
            time.sleep(0.2)
            self.assertEqual(list(Path(d).rglob('*.jpg')),[])
            self.assertEqual(api._url_thumb_b64(U0,460),'','关掉开关后既不写也不读')

class SaveModelVideoPosterTests(unittest.TestCase):
    """缺陷 2 的落盘入口：老 info 没有 imageUrl，纯 stdlib 又解不了 H.264，首帧只能由浏览器抓了回传。"""

    def _b64(self,color=(20,40,60)):
        import io,base64
        buf=io.BytesIO();Image.new('RGB',(80,50),color).save(buf,'JPEG');return base64.b64encode(buf.getvalue()).decode()

    def test_saves_under_agreed_name_and_is_read_back(self):
        api=make_api()
        with TemporaryDirectory() as d,patch.object(webui.posix_compat,'thumb_cache_dir',return_value=None):
            model=write_model(d,'m',[{'url':V1}])
            r=api.save_model_video_poster(model,0,'data:image/jpeg;base64,'+self._b64())
            self.assertTrue(r['ok'],r)
            dest=Path(d)/'m.images'/'image_01.jpg'
            self.assertTrue(dest.is_file(),'必须落在与补帧同名的约定路径上')
            items=json.loads(api.get_covers_multi([model],size=220,n=3))[model]
            video=[f for f in items if len(f)>1 and f[1]][-1]
            self.assertTrue(video[2].startswith('data:image/jpeg;base64,'),'回传一次后详情/瀑布流就该出本地首帧')
            # 幂等：已有本地首帧就不覆盖
            before=dest.read_bytes()
            r2=api.save_model_video_poster(model,0,'data:image/jpeg;base64,'+self._b64((200,10,10)))
            self.assertTrue(r2['ok'],r2);self.assertEqual(dest.read_bytes(),before)

    def test_rejects_non_video_index_and_bad_bytes(self):
        api=make_api()
        with TemporaryDirectory() as d:
            model=write_model(d,'m',[{'url':U0},{'url':V1}])
            self.assertFalse(api.save_model_video_poster(model,0,self._b64())['ok'],'静帧序号不收')
            self.assertFalse(api.save_model_video_poster(model,9,self._b64())['ok'],'越界序号不收')
            self.assertFalse(api.save_model_video_poster(model,1,'@@not-base64@@')['ok'])
            self.assertFalse(api.save_model_video_poster(model,1,'')['ok'])
            self.assertFalse(api.save_model_video_poster(os.path.join(d,'nope.safetensors'),1,self._b64())['ok'])
            self.assertFalse((Path(d)/'m.images').exists(),'静帧/越界/坏数据都不该建出目录')

    def test_junction_images_dir_rejected_with_zero_external_writes(self):
        api=make_api()
        with TemporaryDirectory() as d:
            external=Path(d)/'external';external.mkdir()
            model=write_model(d,'m',[{'url':V1}])
            link=Path(d)/'m.images'
            if not make_dir_link(str(link),str(external)):self.skipTest('本机无法创建符号链接/junction')
            r=api.save_model_video_poster(model,0,'data:image/jpeg;base64,'+self._b64())
            self.assertFalse(r['ok'],r)
            self.assertEqual(list(external.iterdir()),[],'被拒时绝不能往链接指向的目录写任何东西')

class VideoPosterFillTests(unittest.TestCase):
    """真实库 107 个视频帧的 imageUrl 实测 100% 为空 —— 首帧只能由程序从本地原片里取。
    取帧能力在 video_thumb（Windows 外壳），这里钉的是补帧管线：什么时候排、幂等、失败不炸。"""

    def _targets(self,base,images,**kw):
        return webui._images_fill_targets(base,images,**kw)

    def test_poster_target_follows_video_target(self):
        with TemporaryDirectory() as d:
            base=os.path.join(d,'m.safetensors')[:-len('.safetensors')]
            t=self._targets(base,[{'url':V1}],want_posters=True)
            kinds=[(k,os.path.basename(dest)) for k,_src,dest in t]
            self.assertEqual(kinds,[('video','video_01.mp4'),('poster','image_01.jpg')],t)
            self.assertEqual(t[1][1],t[0][2],'poster 的源就是这一批刚落盘的原片')

    def test_no_poster_target_when_helper_unavailable_or_poster_exists(self):
        with TemporaryDirectory() as d:
            base=os.path.join(d,'m')
            self.assertEqual([k for k,_s,_dst in self._targets(base,[{'url':V1}],want_posters=False)],['video'])
            # info 自带 imageUrl 时首帧走远程下载，不再让外壳重复出一张
            self.assertEqual([k for k,_s,_dst in self._targets(base,[{'url':V1,'imageUrl':POSTER1}],want_posters=True)],['image','video'])

    def test_existing_video_without_poster_is_queued_locally(self):
        # 存量：原片早就在盘上、没有首帧 → 清单里要有 poster 项，且 _fill_pending_targets 认为它待办（零网络）
        with TemporaryDirectory() as d:
            base=os.path.join(d,'m')
            idir=Path(d)/'m.images';idir.mkdir();(idir/'video_01.mp4').write_bytes(b'f'*4096)
            t=self._targets(base,[{'url':V1}],want_videos=False,want_posters=True)
            self.assertEqual([k for k,_s,_dst in t],['poster'],t)
            self.assertEqual([k for k,_s,_dst in webui._fill_pending_targets(t)],['poster'])
            (idir/'image_01.jpg').write_bytes(b'x'*2048)
            self.assertEqual(webui._fill_pending_targets(self._targets(base,[{'url':V1}],want_videos=False,want_posters=True)),[],
                             '已有首帧就不再排，重复打开详情零成本')

    def test_run_calls_helper_and_counts_it(self):
        with TemporaryDirectory() as d:
            base=os.path.join(d,'m');idir=Path(d)/'m.images';idir.mkdir()
            vdir=idir/'video_01.mp4';vdir.write_bytes(b'f'*4096)
            poster=idir/'image_01.jpg'
            def fake(src,dst,max_size=640):
                Path(dst).write_bytes(b'\xff\xd8fakejpeg');return True
            with patch.object(webui,'_fill_free_space_ok',return_value=(True,'')),\
                 patch('video_thumb.first_frame',side_effect=fake):
                stats=webui._images_fill_run(os.path.join(d,'m.safetensors'),
                                             [('poster',str(vdir),str(poster))],{})
            self.assertEqual(stats['downloaded'],1,stats);self.assertTrue(poster.is_file())

    def test_run_survives_helper_failure(self):
        with TemporaryDirectory() as d:
            base=os.path.join(d,'m');idir=Path(d)/'m.images';idir.mkdir()
            vdir=idir/'video_01.mp4';vdir.write_bytes(b'f'*4096)
            with patch.object(webui,'_fill_free_space_ok',return_value=(True,'')),\
                 patch('video_thumb.first_frame',return_value=False),\
                 patch('video_thumb.last_error',return_value='外壳没有缩略图提供程序'):
                stats=webui._images_fill_run(os.path.join(d,'m.safetensors'),
                                             [('poster',str(vdir),str(idir/'image_01.jpg'))],{})
            self.assertEqual((stats['downloaded'],stats['failed']),(0,1),stats)
            self.assertFalse((idir/'image_01.jpg').exists())
            self.assertIn('外壳',stats['msg'],stats['msg'])

if __name__=='__main__':unittest.main()
