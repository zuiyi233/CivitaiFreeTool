"""发布资产重建的门禁测试（scripts/build_release_assets.py）。

要钉住的事故：v2.6.32 的 Release 附件里两份扩展 ZIP 是 2.3.0 / 1.4.0 时代的包，
而 release_verified.py 的哈希校验全绿 —— 它只比对"上传的文件 == 清单里的文件"，
从不问"这个 ZIP 是不是当前源码树打的"。所以这里钉两条：
  1) 只要 ZIP 里的 manifest 版本 / 关键文件 / 条目内容与源码树不一致，check_zip 必须炸；
  2) 一次完整 build 出来的 6 个附件自洽（大小、SHA256、CRLF 清单、版本与 commit 绑定），
     并且 release_github.py 认的那份必需文件集合完全对得上。
"""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
import build_release_assets as bra  # noqa: E402

ROOT = bra.ROOT
VERSION = bra.app_version()
# release_verified.py 认可的必需附件集合（名字规则不许漂）
REQUIRED_NAMES = {
    'CivitaiFreeToolWeb-%s.exe' % VERSION,
    'CivitaiFreeTool-Windows-v%s.zip' % VERSION,
    'CivitaiFreeTool-ChromeExtension.zip',
    'CivitaiFreeTool-FirefoxExtension.zip',
    'CivitaiFreeTool-Source-v%s.zip' % VERSION,
    'SHA256SUMS.txt',
}


def zip_from_tree(rel_dir, mutate=None):
    """按源码树打一个 ZIP；mutate(entries) 用来制造"旧包/缺文件/多文件"的病灶。"""
    entries = [(n, open(os.path.join(ROOT, rel_dir, n.replace('/', os.sep)), 'rb').read())
               for n in bra.iter_tree(rel_dir)]
    if mutate:
        entries = mutate(entries)
    fd, path = tempfile.mkstemp(suffix='.zip')
    os.close(fd)
    bra.write_zip(path, entries)
    return path


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = []

    def tearDown(self):
        for p in self._tmp:
            shutil.rmtree(p, ignore_errors=True)

    def _mktmp(self):
        d = tempfile.mkdtemp(prefix='cft-assets-')
        self._tmp.append(d)
        return d


class TestGates(TempDirCase):

    def test_current_tree_passes(self):
        for rel in ('chrome-extension', 'firefox-extension'):
            p = zip_from_tree(rel)
            try:
                info = bra.check_zip(p, rel)
                self.assertEqual(info['version'], bra.manifest_of(rel)['version'])
                self.assertGreater(info['entries'], 10)
            finally:
                os.remove(p)

    def test_stale_version_inside_zip_is_rejected(self):
        # 这就是 2.6.32 的形状：ZIP 存在、哈希存在，但里面是被换掉的旧 manifest
        def bump(entries):
            out = []
            for name, data in entries:
                if name == 'manifest.json':
                    m = json.loads(data.decode('utf-8'))
                    m['version'] = '2.3.0'
                    data = json.dumps(m, ensure_ascii=False).encode('utf-8')
                out.append((name, data))
            return out
        p = zip_from_tree('chrome-extension', bump)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                bra.check_zip(p, 'chrome-extension')
            self.assertIn('2.3.0', str(ctx.exception))
        finally:
            os.remove(p)

    def test_missing_key_files_is_rejected(self):
        # 旧包里根本没有状态那几个文件：删掉一条关键文件就必须炸
        drop = 'lib/model-status.js'
        p = zip_from_tree('chrome-extension', lambda es: [e for e in es if e[0] != drop])
        try:
            with self.assertRaises(RuntimeError) as ctx:
                bra.check_zip(p, 'chrome-extension')
            self.assertIn(drop, str(ctx.exception))
        finally:
            os.remove(p)

    def test_entry_not_in_source_tree_is_rejected(self):
        p = zip_from_tree('firefox-extension',
                          lambda es: es + [('leftover.js', b'// not in the source tree\n')])
        try:
            with self.assertRaises(RuntimeError) as ctx:
                bra.check_zip(p, 'firefox-extension')
            self.assertIn('leftover.js', str(ctx.exception))
        finally:
            os.remove(p)

    def test_tampered_file_bytes_is_rejected(self):
        def tamper(entries):
            return [(n, (b'// tampered\n' if n == 'lib/util.js' else d)) for n, d in entries]
        p = zip_from_tree('chrome-extension', tamper)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                bra.check_zip(p, 'chrome-extension')
            self.assertIn('逐字节', str(ctx.exception))
        finally:
            os.remove(p)

    def test_directory_entry_and_nested_zip_are_rejected(self):
        p = zip_from_tree('chrome-extension', lambda es: es + [('icons/', b'')])
        try:
            with self.assertRaises(RuntimeError):
                bra.check_zip(p, 'chrome-extension')
        finally:
            os.remove(p)
        p = zip_from_tree('chrome-extension', lambda es: es + [('old.zip', b'PK\x03\x04')])
        try:
            with self.assertRaises(RuntimeError):
                bra.check_zip(p, 'chrome-extension')
        finally:
            os.remove(p)

    def test_iter_tree_skips_junk_and_backup_dirs(self):
        # iter_tree 按模块级 ROOT 取路径，所以这里临时把 ROOT 指向一份带垃圾的副本
        work = self._mktmp()
        fake_root = os.path.join(work, 'root')
        shutil.copytree(os.path.join(ROOT, 'chrome-extension'),
                        os.path.join(fake_root, 'chrome-extension'))
        junk = os.path.join(fake_root, 'chrome-extension')
        open(os.path.join(junk, '.DS_Store'), 'w').close()
        open(os.path.join(junk, 'Thumbs.db'), 'w').close()
        open(os.path.join(junk, 'leftover.zip'), 'w').close()
        os.makedirs(os.path.join(junk, '__pycache__'))
        open(os.path.join(junk, '__pycache__', 'x.pyc'), 'w').close()
        os.makedirs(os.path.join(junk, 'icons.backup'))
        open(os.path.join(junk, 'icons.backup', 'icon16.png'), 'w').close()
        original = bra.ROOT
        bra.ROOT = fake_root
        try:
            names = bra.iter_tree('chrome-extension')
        finally:
            bra.ROOT = original
        self.assertIn('manifest.json', names)
        self.assertIn('lib/model-status.js', names)
        self.assertFalse([n for n in names if n.endswith(('.DS_Store', 'Thumbs.db', '.zip', '.pyc'))], names)
        self.assertFalse([n for n in names if n.startswith('__pycache__/') or '.backup/' in n], names)


class TestFullBuild(TempDirCase):
    def test_build_is_self_consistent(self):
        if os.environ.get('CFT_SKIP_RELEASE_BUILD_IN_TESTS') == '1':
            self.skipTest('按需跳过：这条会跑 git archive 并产出全套附件')
        out = self._mktmp()
        fake_exe = os.path.join(out, 'CivitaiFreeToolWeb.exe')
        with open(fake_exe, 'wb') as f:
            f.write(b'MZ' + bytes(4096))
        fake_cli = os.path.join(out, 'CivitaiFreeToolCLI.exe')
        with open(fake_cli, 'wb') as f:
            f.write(b'MZ' + bytes(2048))
        notes = os.path.join(ROOT, 'docs', 'releases', 'v%s.md' % VERSION)
        bra.build(out, VERSION, fake_exe, notes, cli_exe_path=fake_cli)
        names = {n for n in os.listdir(out)
                 if n not in ('CivitaiFreeToolWeb.exe', 'CivitaiFreeToolCLI.exe')}
        self.assertEqual(names, REQUIRED_NAMES | {'release_assets.json'},
                         '附件集合必须正好等于 release_github.py 认可的那 6 个')
        manifest = json.loads(io.open(os.path.join(out, 'release_assets.json'), encoding='utf-8').read())
        self.assertEqual(manifest['version'], VERSION)
        self.assertEqual(manifest['commit'], bra.git('rev-parse', 'HEAD'))
        for a in manifest['assets']:
            data = open(os.path.join(out, a['name']), 'rb').read()
            self.assertEqual(len(data), a['size'], a['name'] + ' 大小不符')
            self.assertEqual(hashlib.sha256(data).hexdigest(), a['sha256'], a['name'] + ' 哈希不符')
        raw = open(os.path.join(out, 'SHA256SUMS.txt'), 'rb').read()
        self.assertFalse(raw.startswith(b'\xef\xbb\xbf'), 'SHA256SUMS.txt 不许有 BOM')
        self.assertIn(b'\r\n', raw)
        self.assertNotIn(b'\n', raw.replace(b'\r\n', b''), '行尾只能是 CRLF')
        lines = [l for l in raw.decode('utf-8').split('\r\n') if l]
        self.assertEqual(len(lines), 5, '五个附件各一行')
        self.assertEqual(lines, sorted(lines, key=lambda l: l.split('  ')[1]), '必须按文件名排序')
        for line in lines:
            digest, name = line.split('  ')
            self.assertEqual(digest, hashlib.sha256(open(os.path.join(out, name), 'rb').read()).hexdigest())
        # 便携 ZIP 里的扩展与独立 ZIP 同源，用户按 README 加载的不是另一份
        portable = os.path.join(out, 'CivitaiFreeTool-Windows-v%s.zip' % VERSION)
        with zipfile.ZipFile(portable) as z:
            self.assertEqual(z.read(bra.PORTABLE_ROOT + '/使用说明.txt').decode('utf-8'),
                             bra.usage_text(VERSION))
            for rel in ('chrome-extension', 'firefox-extension'):
                packed = json.loads(z.read('%s/%s/manifest.json' % (bra.PORTABLE_ROOT, rel)).decode('utf-8'))
                self.assertEqual(packed['version'], bra.manifest_of(rel)['version'])
            self.assertEqual(z.read('%s/%s' % (bra.PORTABLE_ROOT, 'CivitaiFreeToolWeb.exe')),
                             open(fake_exe, 'rb').read())
            self.assertEqual(z.read('%s/%s' % (bra.PORTABLE_ROOT, bra.CLI_EXE_NAME)),
                             open(fake_cli, 'rb').read())
            self.assertTrue([n for n in z.namelist() if n.startswith(bra.PORTABLE_ROOT + '/chrome-extension/lib/model-status.js')],
                            '便携包里的扩展必须含状态模块（旧包就是缺这些才暴露的）')

    def test_usage_text_only_swaps_version(self):
        # 这份文案在上一版便携包里有原件：除首行版本号外逐字不许漂，改它要显式更新这里的期望值
        expected = '\n'.join([
            'CivitaiFreeTool v9.9.9',
            '解压到个人可写目录后运行 CivitaiFreeToolWeb.exe。首次运行配置默认写在 EXE 旁，可在设置修改软件信息储存位置。',
            '命令行/脚本调用：CivitaiFreeToolCLI.exe --bridge http://127.0.0.1:47531 doctor（用法见 docs/ai-cli-webhook.md）。',
            '更新旧版请只替换 EXE，保留 user_config.json、任务/历史和缓存。',
            'Chrome：加载已解压的 chrome-extension 目录。',
            'Firefox：使用 Release 中的独立 Firefox ZIP 临时加载，长期安装需要 Mozilla 签名。',
        ]) + '\n'
        self.assertEqual(bra.usage_text('9.9.9'), expected)
        self.assertIn('v2.7.0', bra.usage_text('2.7.0').split('\n')[0])

    def test_missing_cli_exe_refuses(self):
        out = self._mktmp()
        fake_exe = os.path.join(out, 'CivitaiFreeToolWeb.exe')
        with open(fake_exe, 'wb') as f:
            f.write(b'MZ' + bytes(4096))
        notes = os.path.join(ROOT, 'docs', 'releases', 'v%s.md' % VERSION)
        with self.assertRaises(RuntimeError) as ctx:
            bra.build(out, VERSION, fake_exe, notes,
                      cli_exe_path=os.path.join(out, 'missing-cli.exe'))
        self.assertIn('CLI EXE', str(ctx.exception))

    def test_wrong_version_refuses(self):
        out = self._mktmp()
        notes = os.path.join(ROOT, 'docs', 'releases', 'v%s.md' % VERSION)
        with self.assertRaises(RuntimeError) as ctx:
            bra.build(out, '0.0.1', os.path.join(out, 'none.exe'), notes)
        self.assertIn('APP_VERSION', str(ctx.exception))


if __name__ == '__main__':
    unittest.main(verbosity=2)
