# -*- mode: python ; coding: utf-8 -*-

import sys
from pathlib import Path


runtime_dll_names = (
    "libcrypto-3-x64.dll",
    "libssl-3-x64.dll",
    "liblzma.dll",
    "libbz2.dll",
    "libmpdec-4.dll",
    "libexpat.dll",
    "ffi.dll",
)
runtime_dll_dir = Path(sys.prefix) / "Library" / "bin"
runtime_binaries = [
    (str(runtime_dll_dir / name), ".")
    for name in runtime_dll_names
    if (runtime_dll_dir / name).is_file()
]

a = Analysis(
    ['updater.py'],
    pathex=[],
    binaries=runtime_binaries,
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'pandas', 'tkinter', '_tkinter'],
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
    name='AALC Updater',
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
    icon=['assets\\logo\\Updater.ico'],
)
