# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


datas = [
    ('src/stt_app/webgpu_asr_runner.mjs', 'stt_app'),
    ('src/stt_app/assets/app_icon.ico', 'stt_app/assets'),
]
binaries = []
hiddenimports = [
    'stt_app.main',
    'stt_app.local_model_download_worker',
    'stt_app.local_model_scan_worker',
    'stt_app.benchmark_process',
    'stt_app.benchmark_worker',
    'onnxruntime_genai',
    'comtypes',
    # onnx-asr's loader imports every model class statically, so one entry
    # covers Parakeet and Canary. huggingface_hub is imported lazily inside
    # its resolver and would otherwise be missed.
    'onnx_asr',
    'huggingface_hub',
]
ort_genai_datas, ort_genai_binaries, ort_genai_hiddenimports = collect_all(
    'onnxruntime_genai'
)
datas.extend(ort_genai_datas)
binaries.extend(ort_genai_binaries)
hiddenimports.extend(ort_genai_hiddenimports)
# onnx-asr keeps its mel/resampler graphs as package *data*
# (onnx_asr/preprocessors/data/*.onnx plus fbanks.npz), loaded through
# importlib.resources. hiddenimports alone does not bundle those, and without
# them every model fails while constructing its preprocessor.
onnx_asr_datas, onnx_asr_binaries, onnx_asr_hiddenimports = collect_all('onnx_asr')
datas.extend(onnx_asr_datas)
binaries.extend(onnx_asr_binaries)
hiddenimports.extend(onnx_asr_hiddenimports)
# The Cohere and Granite Speech runtimes are Node.js, so these three are not
# optional for a release build. Skipping a missing one silently produced a
# bundle in which selecting either model fails at runtime with a
# missing-runtime error, and nothing upstream noticed: `npm ci` deletes
# node_modules before installing, so a failed one leaves the tree empty, and
# the build script used to read no exit codes at all.
_missing_node_runtime = [
    source
    for source, _target in (
        ('package.json', '.'),
        ('package-lock.json', '.'),
        ('node_modules', 'node_modules'),
    )
    if not Path(source).exists()
]
if _missing_node_runtime and not os.environ.get('STT_APP_ALLOW_MISSING_NODE_RUNTIME'):
    raise SystemExit(
        'Missing JavaScript runtime files for the bundle: '
        + ', '.join(_missing_node_runtime)
        + '. Run "npm ci --omit=dev" first, or set '
        'STT_APP_ALLOW_MISSING_NODE_RUNTIME=1 to build without the '
        'Cohere/Granite Speech models.'
    )
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
    icon='src/stt_app/assets/app_icon.ico',
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
