"""发布资产重建：所有附件都从当前源码树打，并把"版本/清单对得上"变成硬门禁。

为什么要有这个脚本：v2.6.32 的 Release 附件里两份扩展 ZIP 实际是 2.3.0 / 1.4.0 时代的包
（ZIP 里没有 lib/model-status.js、sidepanel.html、lib/tab-index.js），而 scripts/release_verified.py
的哈希校验全绿——它只保证"上传的文件 == 清单里的文件"，不保证"文件是从这次提交打出来的"。
手工复制的那一步一旦漏做就静默发旧包，用户从 Release 装到的扩展少几个阶段的功能。
这里把"打包"和"判定"收进同一条命令，任何一项对不上就直接失败，不落半成品。

跑法：
    pyinstaller --clean --noconfirm CivitaiFreeToolCLI.spec
    pyinstaller --clean --noconfirm CivitaiFreeToolMCP.spec
    python scripts/build_release_assets.py --version 2.8.0 --assets-dir .tools/release2800
然后：
    python scripts/release_github.py 2.8.0 docs/releases/v2.8.0.md --assets-dir .tools/release2800 \
        --repo zuiyi233/CivitaiFreeTool --remote origin
"""
import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE_NAME = 'CivitaiFreeToolWeb.exe'
CLI_EXE_NAME = 'CivitaiFreeToolCLI.exe'
MCP_EXE_NAME = 'CivitaiFreeToolMCP.exe'
PORTABLE_ROOT = 'CivitaiFreeTool'
SKIP_NAMES = {'.DS_Store', 'Thumbs.db', 'desktop.ini'}
SKIP_DIR_NAMES = {'__pycache__', '.git', '.idea'}
# 关键文件缺席＝ZIP 是旧树打的（2.6.32 那次就是这么烂掉的，只是没人看得见）
REQUIRED = {
    'chrome-extension': [
        'manifest.json', 'background.js', 'popup.html', 'popup.js', 'tabs-common.js',
        'sidepanel.html', 'sidepanel.js', 'lib/util.js', 'lib/idb.js', 'lib/model-status.js',
        'lib/tab-index.js', 'lib/tabrows.js', 'lib/enrich.js',
        'content/model-page.js', 'content/list-status.js', 'content/content.css',
        'icons/icon48.png',
    ],
    'firefox-extension': [
        'manifest.json', 'background.js', 'popup.html', 'popup.js', 'tabs-common.js',
        'lib/model-status.js', 'lib/tab-index.js', 'lib/tabrows.js', 'lib/enrich.js',
        'icons/icon48.png',
    ],
}
USAGE_LINES = [
    '解压到个人可写目录后运行 CivitaiFreeToolWeb.exe。首次运行配置默认写在 EXE 旁，可在设置修改软件信息储存位置。',
    '命令行/脚本调用：CivitaiFreeToolCLI.exe --bridge http://127.0.0.1:47531 doctor（用法见 docs/ai-cli-webhook.md）。',
    'MCP 侧车 CivitaiFreeToolMCP.exe 与桌面端同目录，桌面端启动时会自动拉起它（47532，Bearer token 只写本机文件）。',
    '更新旧版请只替换 EXE，保留 user_config.json、任务/历史和缓存。',
    'Chrome：加载已解压的 chrome-extension 目录。',
    'Firefox：使用 Release 中的独立 Firefox ZIP 临时加载，长期安装需要 Mozilla 签名。',
]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    return subprocess.run(['git', '-C', ROOT, *args], check=True,
                          stdout=subprocess.PIPE).stdout.decode('utf-8', 'replace').strip()


def iter_tree(rel_dir):
    """扩展目录里的文件（相对该目录），排序、排除垃圾与备份。"""
    base = os.path.join(ROOT, rel_dir)
    out = []
    for cur, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIR_NAMES and not d.endswith('.backup'))
        for name in sorted(files):
            if name in SKIP_NAMES or name.endswith(('.orig', '.rej', '.zip')):
                continue
            abs_path = os.path.join(cur, name)
            out.append(os.path.relpath(abs_path, base).replace('\\', '/'))
    return sorted(out)


def manifest_of(rel_dir):
    with io.open(os.path.join(ROOT, rel_dir, 'manifest.json'), encoding='utf-8') as f:
        return json.load(f)


def write_zip(out_path, entries):
    """entries: [(归档名, 字节)]，只用 deflate、只放文件、不放目录项。"""
    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for arcname, data in entries:
            z.writestr(arcname, data)


def check_zip(zip_path, rel_dir):
    """把打好的 ZIP 再读一遍，逐项判定它确实来自当前源码树。"""
    src_manifest = manifest_of(rel_dir)
    with zipfile.ZipFile(zip_path) as z:
        bad = z.testzip()
        if bad:
            raise RuntimeError('%s 内条目损坏：%s' % (zip_path, bad))
        infos = z.infolist()
        names = [i.filename for i in infos]
        dirs = [n for n in names if n.endswith('/')]
        if dirs:
            raise RuntimeError('%s 不该有目录项：%s' % (zip_path, dirs))
        nested = [n for n in names if n.lower().endswith('.zip')]
        if nested:
            raise RuntimeError('%s 不该嵌套 ZIP（manifest 必须在根目录）：%s' % (zip_path, nested))
        if 'manifest.json' not in names:
            raise RuntimeError('%s 根目录没有 manifest.json' % zip_path)
        packed = json.loads(z.read('manifest.json').decode('utf-8'))
        if packed.get('version') != src_manifest.get('version'):
            raise RuntimeError('%s 里的版本 %s 与源码 manifest 的 %s 不一致 —— 这就是发旧包的形状'
                               % (zip_path, packed.get('version'), src_manifest.get('version')))
        missing = [n for n in REQUIRED[rel_dir] if n not in names]
        if missing:
            raise RuntimeError('%s 缺关键文件：%s' % (zip_path, ', '.join(missing)))
        source = sorted(iter_tree(rel_dir))
        if sorted(names) != source:
            # 少文件与多文件都要炸：少文件正是"旧包"的形状（源码里已有、ZIP 里还没有）
            lost = [n for n in source if n not in names]
            extra = [n for n in names if n not in source]
            raise RuntimeError('%s 的条目与源码树不一致；缺 %s，多 %s'
                               % (zip_path, lost or '（无）', extra or '（无）'))
        for name in names:
            with open(os.path.join(ROOT, rel_dir, name.replace('/', os.sep)), 'rb') as f:
                want = sha256(f.read())
            if want != sha256(z.read(name)):
                raise RuntimeError('%s 的 %s 与源码逐字节不一致' % (zip_path, name))
        return {'version': packed['version'], 'entries': len(names),
                'key': 'scripting' in (packed.get('permissions') or [])}


def app_version():
    for line in io.open(os.path.join(ROOT, 'webui.py'), encoding='utf-8'):
        if line.startswith('APP_VERSION'):
            return line.split('=')[1].strip().strip('"\'')
    raise RuntimeError('webui.py 里找不到 APP_VERSION')


def usage_text(version):
    return '\n'.join(['CivitaiFreeTool v%s' % version] + USAGE_LINES) + '\n'


def build(assets_dir, version, exe_path, notes_path, tag=None, cli_exe_path=None,
          mcp_exe_path=None):
    tag = tag or ('v' + version)
    if app_version() != version:
        raise RuntimeError('APP_VERSION=%s 与要发布的 %s 不一致；先同步版本号再打附件'
                           % (app_version(), version))
    if not os.path.exists(notes_path):
        raise RuntimeError('缺少版本说明：%s' % notes_path)
    cli_exe_path = cli_exe_path or os.path.join(os.path.dirname(exe_path), CLI_EXE_NAME)
    if not os.path.isfile(cli_exe_path):
        raise RuntimeError('缺少 CLI EXE：%s（先 pyinstaller --clean --noconfirm '
                           'CivitaiFreeToolCLI.spec）' % cli_exe_path)
    # 冻结版只会拉起「同目录」的 MCP 侧车；便携包里没有它，用户就永远拿不到 MCP 自启动。
    mcp_exe_path = mcp_exe_path or os.path.join(os.path.dirname(exe_path), MCP_EXE_NAME)
    if not os.path.isfile(mcp_exe_path):
        raise RuntimeError('缺少 MCP EXE：%s（先 pyinstaller --clean --noconfirm '
                           'CivitaiFreeToolMCP.spec）' % mcp_exe_path)
    os.makedirs(assets_dir, exist_ok=True)

    report = []
    ext_paths = {}
    for rel_dir, asset_name in [('chrome-extension', 'CivitaiFreeTool-ChromeExtension.zip'),
                                ('firefox-extension', 'CivitaiFreeTool-FirefoxExtension.zip')]:
        out = os.path.join(assets_dir, asset_name)
        entries = [(n, open(os.path.join(ROOT, rel_dir, n.replace('/', os.sep)), 'rb').read())
                   for n in iter_tree(rel_dir)]
        write_zip(out, entries)
        info = check_zip(out, rel_dir)
        ext_paths[rel_dir] = out
        report.append('%s：%s，%d 个条目，顶层 manifest，无目录项/无嵌套，逐字节等于源码树'
                      % (asset_name, info['version'], info['entries']))

    notes_bytes = open(notes_path, 'rb').read()
    usage_bytes = usage_text(version).encode('utf-8')
    portable = os.path.join(assets_dir, 'CivitaiFreeTool-Windows-%s.zip' % tag)
    entries = [(PORTABLE_ROOT + '/' + EXE_NAME, open(exe_path, 'rb').read()),
               (PORTABLE_ROOT + '/' + CLI_EXE_NAME, open(cli_exe_path, 'rb').read()),
               (PORTABLE_ROOT + '/' + MCP_EXE_NAME, open(mcp_exe_path, 'rb').read()),
               (PORTABLE_ROOT + '/更新说明.md', notes_bytes),
               (PORTABLE_ROOT + '/使用说明.txt', usage_bytes)]
    for rel_dir in ('chrome-extension', 'firefox-extension'):
        src_zip = ext_paths[rel_dir]
        with zipfile.ZipFile(src_zip) as z:
            for name in z.namelist():
                entries.append(('%s/%s/%s' % (PORTABLE_ROOT, rel_dir, name), z.read(name)))
    write_zip(portable, entries)
    with zipfile.ZipFile(portable) as z:
        names = z.namelist()
        if [n for n in names if n.endswith('/')]:
            raise RuntimeError('便携 ZIP 不该有目录项')
        for must in (PORTABLE_ROOT + '/' + EXE_NAME,
                     PORTABLE_ROOT + '/' + CLI_EXE_NAME,
                     PORTABLE_ROOT + '/' + MCP_EXE_NAME,
                     PORTABLE_ROOT + '/更新说明.md', PORTABLE_ROOT + '/使用说明.txt'):
            if must not in names:
                raise RuntimeError('便携 ZIP 缺 %s' % must)
        if z.read(PORTABLE_ROOT + '/' + CLI_EXE_NAME) != open(cli_exe_path, 'rb').read():
            raise RuntimeError('便携 ZIP 里的 CLI EXE 与打包输入不一致')
        if z.read(PORTABLE_ROOT + '/' + MCP_EXE_NAME) != open(mcp_exe_path, 'rb').read():
            raise RuntimeError('便携 ZIP 里的 MCP EXE 与打包输入不一致')
        if z.read(PORTABLE_ROOT + '/使用说明.txt') != usage_bytes:
            raise RuntimeError('便携 ZIP 里的使用说明.txt 与生成的文本不一致')
        # 便携包里的扩展必须与独立 ZIP 同源，否则用户按 README 加载到的是另一份
        for rel_dir in ('chrome-extension', 'firefox-extension'):
            got = json.loads(z.read('%s/%s/manifest.json' % (PORTABLE_ROOT, rel_dir)).decode('utf-8'))
            want = manifest_of(rel_dir)
            if got.get('version') != want.get('version'):
                raise RuntimeError('便携 ZIP 里的 %s 版本 %s != 源码 %s'
                                   % (rel_dir, got.get('version'), want.get('version')))
    report.append('CivitaiFreeTool-Windows-%s.zip：%d 个条目，含 %s + %s + %s（与独立扩展 ZIP 同源）'
                  % (tag, len(names), EXE_NAME, CLI_EXE_NAME, MCP_EXE_NAME))

    source_zip = os.path.join(assets_dir, 'CivitaiFreeTool-Source-%s.zip' % tag)
    subprocess.run(['git', '-C', ROOT, 'archive', 'HEAD', '-o', source_zip], check=True)
    with zipfile.ZipFile(source_zip) as z:
        top = {n.split('/')[0] for n in z.namelist() if n}
    if PORTABLE_ROOT in top or 'CHANGELOG.md' not in top:
        raise RuntimeError('源码 ZIP 不该加前缀，根级应是仓库本身：%s' % sorted(top)[:6])
    report.append('CivitaiFreeTool-Source-%s.zip：git archive HEAD，根级 %d 个顶层条目'
                  % (tag, len(top)))

    names5 = ['CivitaiFreeTool-ChromeExtension.zip', 'CivitaiFreeTool-FirefoxExtension.zip',
              'CivitaiFreeTool-Source-%s.zip' % tag, 'CivitaiFreeTool-Windows-%s.zip' % tag,
              '%s-%s.exe' % ('CivitaiFreeToolWeb', version)]
    exe_copy = os.path.join(assets_dir, names5[4])
    if os.path.abspath(exe_path) != os.path.abspath(exe_copy):
        with open(exe_path, 'rb') as src, open(exe_copy, 'wb') as dst:
            dst.write(src.read())
    lines = []
    assets = []
    for name in sorted(names5):
        data = open(os.path.join(assets_dir, name), 'rb').read()
        digest = sha256(data)
        lines.append('%s  %s' % (digest, name))
        assets.append({'name': name, 'size': len(data), 'sha256': digest})
    sums = os.path.join(assets_dir, 'SHA256SUMS.txt')
    with open(sums, 'wb') as f:
        f.write(('\r\n'.join(lines) + '\r\n').encode('utf-8'))
    assets.append({'name': 'SHA256SUMS.txt', 'size': os.path.getsize(sums),
                   'sha256': sha256(open(sums, 'rb').read())})
    with io.open(os.path.join(assets_dir, 'release_assets.json'), 'w', encoding='utf-8') as f:
        f.write(json.dumps({'version': version, 'commit': git('rev-parse', 'HEAD'),
                            'assets': assets}, ensure_ascii=False, indent=2))
    report.append('SHA256SUMS.txt：%d 行（CRLF、按文件名排序、无 BOM）+ release_assets.json（commit=%s）'
                  % (len(lines), git('rev-parse', 'HEAD')[:12]))
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--version', required=True)
    p.add_argument('--assets-dir', required=True)
    p.add_argument('--exe', default=os.path.join(ROOT, 'dist', EXE_NAME))
    p.add_argument('--cli-exe', default=os.path.join(ROOT, 'dist', CLI_EXE_NAME))
    p.add_argument('--mcp-exe', default=os.path.join(ROOT, 'dist', MCP_EXE_NAME))
    p.add_argument('--notes', default=None, help='更新说明来源，默认 docs/releases/v<version>.md')
    args = p.parse_args()
    notes = args.notes or os.path.join(ROOT, 'docs', 'releases', 'v%s.md' % args.version)
    if not os.path.exists(args.exe):
        raise SystemExit('找不到 EXE：%s（先按 docs/RELEASING.md 第 3 步打包）' % args.exe)
    if not os.path.exists(args.cli_exe):
        raise SystemExit('找不到 CLI EXE：%s（先 pyinstaller --clean --noconfirm '
                         'CivitaiFreeToolCLI.spec）' % args.cli_exe)
    if not os.path.exists(args.mcp_exe):
        raise SystemExit('找不到 MCP EXE：%s（先 pyinstaller --clean --noconfirm '
                         'CivitaiFreeToolMCP.spec）' % args.mcp_exe)
    for line in build(args.assets_dir, args.version, args.exe, notes,
                      cli_exe_path=args.cli_exe, mcp_exe_path=args.mcp_exe):
        print('OK', line)
    print('\n附件目录就绪：%s' % os.path.abspath(args.assets_dir))


if __name__ == '__main__':
    main()
