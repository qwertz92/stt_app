# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all


datas = [('src/stt_app/webgpu_asr_runner.mjs', 'stt_app')]
binaries = []
hiddenimports = [
    'stt_app.main',
    'stt_app.local_model_download_worker',
    'stt_app.local_model_scan_worker',
    'onnxruntime_genai',
]
ort_genai_datas, ort_genai_binaries, ort_genai_hiddenimports = collect_all(
    'onnxruntime_genai'
)
datas.extend(ort_genai_datas)
binaries.extend(ort_genai_binaries)
hiddenimports.extend(ort_genai_hiddenimports)
for source, target in (
    ('package.json', '.'),
    ('package-lock.json', '.'),
    ('node_modules', 'node_modules'),
):
    if Path(source).exists():
        datas.append((source, target))

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='stt_app',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
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
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='stt_app',
)
