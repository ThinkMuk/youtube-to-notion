# -*- mode: python ; coding: utf-8 -*-

import glob
import os

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

# yt_dlp_ejs ships the YouTube n-challenge solver as package data
# (yt/solver/*.min.js) that yt-dlp loads at runtime; without collecting it the
# frozen exe silently falls back to 360p-only VOD downloads.
# codex_cli_bin includes the native CLI, its helper executables and package
# manifest. Preserve the complete package layout for bundled_codex_path().
for pkg in ("faster_whisper", "ctranslate2", "yt_dlp_ejs", "codex_cli_bin"):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

# Bundle the NVIDIA CUDA runtime DLLs (cuBLAS/cuDNN) from the nvidia-*-cu12
# wheels so GPU transcription works on machines without a CUDA Toolkit install.
# Placed at the bundle root, where the runtime DLL-path registration in
# yln/transcriber.py makes them findable. Skipped gracefully when the wheels
# are not installed (CPU-only build).
try:
    import nvidia

    for _base in nvidia.__path__:
        for _dll in glob.glob(os.path.join(_base, "*", "bin", "*.dll")):
            binaries.append((_dll, "."))
except ImportError:
    pass

a = Analysis(
    ["yln/__main__.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="YoutubeLiveNotion",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="YoutubeLiveNotion",
)
