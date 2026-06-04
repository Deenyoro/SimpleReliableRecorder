# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for SimpleReliableRecorder (one-file, windowed).

Bundles a full ffmpeg binary plus the soundcard / soundfile / numpy native
backends so the produced executable runs with zero prerequisites. The watchdog
role reuses this same executable via the --watchdog argument, so the whole app
ships as a single file.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = []
binaries = []
hiddenimports = []

# Bundle the full ffmpeg binary so screen recording works with zero prereqs.
# build.py stages it into ./ffmpeg/ with the OS-correct name; paths.ffmpeg_path()
# resolves it from _MEIPASS/ffmpeg/<name> at runtime.
_ffname = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
_ff = os.path.join("ffmpeg", _ffname)
if os.path.isfile(_ff):
    datas.append((_ff, "ffmpeg"))

# These packages ship compiled extensions / data files (numpy, libsndfile via
# soundfile, the CFFI backends used by soundcard). Collect everything so the
# frozen app can find them.
for _pkg in ("numpy", "soundfile", "soundcard", "pystray", "PIL"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

hiddenimports += collect_submodules("soundcard")
hiddenimports += ["cffi", "_cffi_backend", "numpy", "screeninfo", "psutil",
                  "pystray", "PIL", "PIL.Image", "PIL.ImageDraw", "keyboard"]
# pystray picks a backend at runtime; ensure the Windows one is bundled.
if sys.platform == "win32":
    hiddenimports += ["pystray._win32"]
elif sys.platform == "darwin":
    hiddenimports += ["pystray._darwin"]
else:
    hiddenimports += ["pystray._xorg", "pystray._appindicator"]

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

_icon = os.path.join("assets", "icon.ico")

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="SimpleReliableRecorder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=(_icon if os.path.isfile(_icon) else None),
)
