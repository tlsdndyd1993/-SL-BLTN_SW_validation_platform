# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec — Screen & Camera Recorder (SWC 분리 후)
§3.5: 새 SWC 패키지 추가 시 collect_submodules() 리스트 업데이트 필수.
"""
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# 각 SWC 서브모듈 전부 수집
hidden = []
for pkg in ['common', 'engine_swc', 'kernel_swc', 'can_swc', 'ui_swc']:
    hidden += collect_submodules(pkg)

# cv2 / numpy / mss 전체 수집 (§3.5)
binaries, datas, hiddenimports_auto = [], [], []
for pkg in ['cv2', 'numpy', 'mss']:
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports_auto += h

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden + hiddenimports_auto,
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

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='ScreenCameraRecorder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=True, upx_exclude=[],
    name='ScreenCameraRecorder',
)
