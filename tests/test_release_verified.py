"""发布流程测试：假 Git/HTTP/凭据，不触碰真实 GitHub 或认证信息。"""
import unittest,sys,json,io,hashlib,importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit,parse_qs
from urllib.error import HTTPError
spec=importlib.util.spec_from_file_location('release_verified',Path(__file__).resolve().parents[1]/'scripts'/'release_verified.py')
release=importlib.util.module_from_spec(spec);spec.loader.exec_module(release)

class ReleaseTests(unittest.TestCase):
    def fixture(self,d):
        folder=Path(d);names=['CivitaiFreeToolWeb-2.6.0.exe','CivitaiFreeTool-Windows-v2.6.0.zip','CivitaiFreeTool-ChromeExtension.zip','CivitaiFreeTool-FirefoxExtension.zip','CivitaiFreeTool-Source-v2.6.0.zip','SHA256SUMS.txt']
        assets=[]
        for name in names:
            data=('fixture '+name).encode();(folder/name).write_bytes(data);assets.append({'name':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        manifest={'version':'2.6.0','commit':'a'*40,'assets':assets};(folder/'release_assets.json').write_text(json.dumps(manifest),encoding='utf-8');(folder/'notes.md').write_text('Fixture release notes',encoding='utf-8')
        return folder,manifest,['release','2.6.0',str(folder/'notes.md'),'--assets-dir',str(folder)]
    def test_wrong_remote_commit_blocks_before_authentication(self):
        with TemporaryDirectory() as d:
            folder,m,argv=self.fixture(d)
            with patch.object(sys,'argv',argv),patch.object(release,'git',side_effect=['a'*40,'b'*40+'\trefs/heads/main']),patch.object(release,'authentication') as auth:
                with self.assertRaises(RuntimeError):release.main()
                auth.assert_not_called()
    def test_bad_local_hash_blocks_before_authentication(self):
        with TemporaryDirectory() as d:
            folder,m,argv=self.fixture(d);(folder/m['assets'][0]['name']).write_bytes(b'changed')
            with patch.object(sys,'argv',argv),patch.object(release,'git',side_effect=['a'*40,'a'*40+'\trefs/heads/main','https://github.com/ADVICEsama/CivitaiFreeTool.git']),patch.object(release,'authentication') as auth:
                with self.assertRaises(RuntimeError):release.main()
                auth.assert_not_called()
    def test_release_repo_defaults_to_official_and_matches_pushed_remote(self):
        with TemporaryDirectory() as d:
            folder,m,argv=self.fixture(d);seen=[]
            class Opener:
                def open(self,req,timeout=0):
                    url=req.full_url;seen.append(url);method=req.get_method()
                    if '/releases/tags/' in url:raise HTTPError(url,404,'fixture absent',None,None)
                    if method=='POST' and url.endswith('/releases'):
                        data={'id':77,'draft':True,'assets':[],'html_url':'https://github.com/fixture/CivitaiFreeTool/releases/tag/v2.6.0','upload_url':'https://uploads.github.com/fixture{?name}'}
                    elif 'uploads.github.com' in url:
                        name=parse_qs(urlsplit(url).query)['name'][0];a=next(x for x in m['assets'] if x['name']==name)
                        data={'size':len(req.data),'digest':'sha256:'+a['sha256'],'browser_download_url':'https://example.invalid/'+name}
                    else:data={'draft':False,'html_url':'https://github.com/ADVICEsama/CivitaiFreeTool/releases/tag/v2.6.0'}
                    return io.BytesIO(json.dumps(data).encode())
            with patch.object(sys,'argv',argv),patch.object(release,'git',side_effect=['a'*40,'a'*40+'\trefs/heads/main','https://github.com/ADVICEsama/CivitaiFreeTool.git']),patch.object(release,'authentication',return_value='fixture-not-real'),patch.object(release.urllib.request,'build_opener',return_value=Opener()):release.main()
            self.assertIn('https://api.github.com/repos/ADVICEsama/CivitaiFreeTool/releases',seen)
    def test_repo_flag_publishes_to_the_fork(self):
        with TemporaryDirectory() as d:
            folder,m,argv=self.fixture(d);argv=argv+['--repo','zuiyi233/CivitaiFreeTool'];seen=[]
            class Opener:
                def open(self,req,timeout=0):
                    url=req.full_url;seen.append(url);method=req.get_method()
                    if '/releases/tags/' in url:raise HTTPError(url,404,'fixture absent',None,None)
                    if method=='POST' and url.endswith('/releases'):
                        data={'id':77,'draft':True,'assets':[],'html_url':'https://github.com/fixture/CivitaiFreeTool/releases/tag/v2.6.0','upload_url':'https://uploads.github.com/fixture{?name}'}
                    elif 'uploads.github.com' in url:
                        name=parse_qs(urlsplit(url).query)['name'][0];a=next(x for x in m['assets'] if x['name']==name)
                        data={'size':len(req.data),'digest':'sha256:'+a['sha256'],'browser_download_url':'https://example.invalid/'+name}
                    else:data={'draft':False,'html_url':'https://github.com/zuiyi233/CivitaiFreeTool/releases/tag/v2.6.0'}
                    return io.BytesIO(json.dumps(data).encode())
            with patch.object(sys,'argv',argv),patch.object(release,'git',side_effect=['a'*40,'a'*40+'\trefs/heads/main','git@github.com:zuiyi233/CivitaiFreeTool.git']),patch.object(release,'authentication',return_value='fixture-not-real'),patch.object(release.urllib.request,'build_opener',return_value=Opener()):release.main()
            api_calls=[u for u in seen if u.startswith('https://api.github.com/')]
            self.assertTrue(api_calls and all('/repos/zuiyi233/CivitaiFreeTool' in u for u in api_calls),api_calls)
            self.assertFalse(any('ADVICEsama' in u for u in seen),'二开发布不许碰到官方仓库')
    def test_repo_mismatch_with_pushed_remote_blocks_before_authentication(self):
        with TemporaryDirectory() as d:
            folder,m,argv=self.fixture(d);argv=argv+['--repo','someone/else']
            with patch.object(sys,'argv',argv),patch.object(release,'git',side_effect=['a'*40,'a'*40+'\trefs/heads/main','https://github.com/zuiyi233/CivitaiFreeTool.git']),patch.object(release,'authentication') as auth:
                with self.assertRaises(RuntimeError):release.main()
                auth.assert_not_called()
    def test_repo_accepts_url_and_ssh_forms(self):
        self.assertEqual(release.normalize_repo('https://github.com/ADVICEsama/CivitaiFreeTool.git'),'ADVICEsama/CivitaiFreeTool')
        self.assertEqual(release.normalize_repo('git@github.com:zuiyi233/CivitaiFreeTool'),'zuiyi233/CivitaiFreeTool')
        self.assertEqual(release.normalize_repo(' zuiyi233/CivitaiFreeTool/ '),'zuiyi233/CivitaiFreeTool')
        for bad in ('CivitaiFreeTool','a/b/c',''):
            with self.assertRaises(RuntimeError):release.normalize_repo(bad)
    def test_all_assets_verified_before_publication(self):
        with TemporaryDirectory() as d:
            folder,m,argv=self.fixture(d);uploaded=[];published=[]
            class Opener:
                def open(self,req,timeout=0):
                    url=req.full_url;method=req.get_method()
                    if '/releases/tags/' in url:raise HTTPError(url,404,'fixture absent',None,None)
                    if method=='POST' and url.endswith('/releases'):
                        body=json.loads(req.data);self.assertion=body['draft'];assert body['target_commitish']=='a'*40
                        data={'id':77,'html_url':'https://github.com/ADVICEsama/CivitaiFreeTool/releases/tag/v2.6.0','draft':True,'assets':[],'upload_url':'https://uploads.github.com/fixture{?name}'}
                    elif 'uploads.github.com' in url:
                        name=parse_qs(urlsplit(url).query)['name'][0];asset=next(a for a in m['assets'] if a['name']==name);uploaded.append(name)
                        data={'size':len(req.data),'digest':'sha256:'+hashlib.sha256(req.data).hexdigest(),'browser_download_url':'https://example.invalid/'+name}
                    else:
                        assert method=='PATCH' and len(uploaded)==6;published.append(True);data={'html_url':'https://github.com/ADVICEsama/CivitaiFreeTool/releases/tag/v2.6.0','draft':False}
                    return io.BytesIO(json.dumps(data).encode())
            with patch.object(sys,'argv',argv),patch.object(release,'git',side_effect=['a'*40,'a'*40+'\trefs/heads/main','https://github.com/ADVICEsama/CivitaiFreeTool.git']),patch.object(release,'authentication',return_value='fixture-not-real'),patch.object(release.urllib.request,'build_opener',return_value=Opener()):release.main()
            self.assertEqual(len(uploaded),6);self.assertEqual(published,[True]);self.assertFalse(json.loads((folder/'github-release-receipt.json').read_text())['draft'])
    def test_credentials_removed_on_external_redirect(self):
        req=release.urllib.request.Request('https://api.github.com/asset',headers={'Authorization':'Bearer fixture-not-real'})
        redirect=release.SafeRedirect().redirect_request(req,None,302,'fixture',{},'https://objects.githubusercontent.com/asset')
        self.assertNotIn('Authorization',dict(redirect.header_items()))

if __name__=='__main__':unittest.main()
