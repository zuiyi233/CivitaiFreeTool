# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the stdlib-only CLI client (console mode, onefile).

Build with:
    pyinstaller --clean --noconfirm CivitaiFreeToolCLI.spec
"""
from pathlib import Path

a = Analysis(
    ["cft_cli.py"],
    pathex=[str(Path(SPECPATH))],
    binaries=[],
    # ai_contract.py 正常会被静态分析收集；datas 只是冻结后可直接查看的兜底副本。
    datas=[("ai_contract.py", ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CivitaiFreeToolCLI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=["icon.ico"],
)
