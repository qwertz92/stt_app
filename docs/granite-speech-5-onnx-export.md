# Granite Speech 5.0 ONNX Export

This document covers conversion only. The resulting model is not selectable in
the application yet.

## Pinned source and graph contract

- Source: `ibm-granite/granite-speech-5.0-470m-turboctc`
- Source revision: `18ca3c1de6cd092b5a30c39fb0f04550b38ed1a0`
- Source weight SHA-256:
  `8b98a8c34fd5fcb081caef719638eded31bb6d197d62053eefc5c1703aaf1ad4`
- License: Apache-2.0
- ONNX opset: 17
- Input: `input_features`, float32 `[batch_size, feature_frames, 320]`, with
  `feature_frames >= 4`
- Output: `logits`, float32 `[batch_size, floor(feature_frames / 4), 16384]`

Audio feature extraction and CTC decoding remain outside the graph. Use the
source model's `AutoProcessor` for both. The graph accepts dynamic time lengths
from four frames upward; the PyTorch source model rejects one to three frames.
Multiple equal-length clips can share a batch; padded unequal-length batches
need an attention-mask input that this first export deliberately does not expose,
so run those clips separately.

## Export

Run the commands from the repository root in WSL. The environments live under
ignored `build/` paths and do not change the application's dependencies.

```bash
uv venv --python 3.12 build/export-env
uv pip install --python build/export-env/bin/python \
  --index-url https://download.pytorch.org/whl/cpu \
  'torch==2.14.0+cpu'
uv pip install --python build/export-env/bin/python \
  'transformers==5.16.0' 'onnx==1.22.0' 'onnxscript==0.7.1' \
  'safetensors==0.8.0' 'packaging==26.3' 'numpy==2.5.2'

hf download ibm-granite/granite-speech-5.0-470m-turboctc \
  --revision 18ca3c1de6cd092b5a30c39fb0f04550b38ed1a0 \
  --local-dir build/upstream-ibm

timeout 1800 env HF_HUB_OFFLINE=1 \
  build/export-env/bin/python scripts/export_granite_speech5_onnx.py \
  --model build/upstream-ibm \
  --revision 18ca3c1de6cd092b5a30c39fb0f04550b38ed1a0 \
  --local-files-only
```

The exporter writes `build/granite-speech5-onnx/model.onnx` only after a full
ONNX checker pass. It also writes `model.export.json` with the source and output
hashes. Pass `--force` only when intentionally replacing an existing result;
the old file remains untouched until the replacement passes that checker.

The stock model uses `Tensor.unfold` for its two time-subsampling residuals.
PyTorch's legacy exporter cannot preserve a dynamic input length through that
operation. The export script replaces only those residual operations with the
equivalent `avg_pool1d`; the full-model pre-export check requires numerical
equality before serialization. The newer `torch.export`-based ONNX path in
PyTorch 2.14 still fails earlier on Granite's symbolic attention reshape, so the
script uses the legacy path and compensates with explicit boundary validation.

## FP16 and dynamic INT8 variants

The precision converter reads the checked FP32 graph, writes to a temporary
file, runs the full ONNX checker, and only then replaces the destination. FP16
keeps float32 graph inputs and outputs. INT8 uses per-channel signed INT8
weights for eligible `MatMul`/`Gemm` nodes and dynamically quantizes their
activations at inference time.

```bash
timeout 900 build/onnx-env/bin/python \
  scripts/convert_granite_speech5_onnx_precision.py \
  --input build/granite-speech5-onnx/model.onnx \
  --output build/granite-speech5-onnx/model_fp16.onnx \
  --precision fp16

timeout 1200 build/onnx-env/bin/python \
  scripts/convert_granite_speech5_onnx_precision.py \
  --input build/granite-speech5-onnx/model.onnx \
  --output build/granite-speech5-onnx/model_int8.onnx \
  --precision int8
```

INT8 is intentionally a mixed graph: 163 `MatMulInteger` nodes are quantized,
while 49 `MatMul` nodes plus convolution, normalization, softmax, and other
operators remain floating point. Static INT8 was not produced because it needs
a representative calibration corpus and a fixed target runtime. BF16 would
have the same storage width as FP16 without improving the current DirectML or
CPU deployment path. INT4 is deferred until a target runtime and a broader WER
evaluation justify its additional compatibility and accuracy risk.

## Independent validation

Validation intentionally uses PyTorch 2.11 rather than the 2.14 export runtime.
It downloads 20 pinned LibriSpeech dummy clips, creates fresh PyTorch reference
logits, unloads PyTorch, and then compares ONNX Runtime output. Synthetic cases
exercise lengths 4, 127-129, 255-257, and 511-513, plus dynamic batch size two.

```bash
uv venv --python 3.12 build/onnx-env
uv pip install --python build/onnx-env/bin/python \
  --index-url https://download.pytorch.org/whl/cpu \
  'torch==2.11.0+cpu' 'torchaudio==2.11.0+cpu'
uv pip install --python build/onnx-env/bin/python \
  'transformers==5.16.0' 'onnx==1.22.0' 'onnxruntime==1.29.0' \
  'onnxscript==0.7.1' 'safetensors==0.8.0' 'soundfile==0.14.0' \
  'packaging==26.3' 'numpy==2.5.2'

timeout 1800 env HF_HUB_OFFLINE=1 \
  build/onnx-env/bin/python scripts/validate_granite_speech5_onnx.py \
  --model build/upstream-ibm \
  --revision 18ca3c1de6cd092b5a30c39fb0f04550b38ed1a0 \
  --onnx build/granite-speech5-onnx/model.onnx \
  --fixtures build/granite-speech5-fixtures \
  --report build/granite-speech5-onnx/validation.json \
  --local-files-only
```

The fixture fetch still needs network access the first time; `HF_HUB_OFFLINE`
only prevents an accidental unpinned model fetch. Later runs reuse the fixture
directory. The validator disables ONNX Runtime telemetry before importing the
native runtime.

## Verified result (2026-09-04)

- FP32: 1,892,339,420 bytes (1.763 GiB), SHA-256
  `cc647a0681142d6651dc14bf582cf2ef06da1b1d0978f078bf66b56a407fcddb`
- FP16: 946,797,887 bytes (902.937 MiB), SHA-256
  `5ae14135ac9ac0fbdce8ab8566dc45de5d25111baa0db43d94f4821d7b284359`
- Dynamic INT8: 551,294,349 bytes (525.756 MiB), SHA-256
  `8173a50ea67f864801971a40622a2e5c7d62fd230912bde94b745f92c74d60e9`
- Full-model subsampling replacement maximum absolute difference: `0.0`
- FP32 validation: 31/31 exact CTC token streams and decoded transcripts;
  maximum absolute logit difference `0.0009813308715820312`, weighted mean
  `0.000002765029560252943`
- FP16 comparison: 31/31 exact CTC token streams and decoded transcripts;
  maximum absolute logit difference `0.7867527008056641`, weighted mean
  `0.0019913188516136287`
- INT8 comparison: 30/31 exact decoded transcripts, 19/20 on real audio;
  minimum frame-level argmax agreement `0.9852941176470589`
- Dynamic feature lengths were smoke-tested through 2,048 frames; the focused
  parity gate covers 4 through 1,470 frames

The INT8 mismatch is on the longest 29.4-second fixture. FP32 decodes the
relevant phrase as `and a paintings`, INT8 as `and adam paintings`, and the
dataset reference is `AND AT EM PAINTINGS`. This tiny comparison proves graph
health, not equal corpus-level WER or that INT8 is more accurate.

## Local performance

The benchmark used one 29.4-second, mono 16 kHz LibriSpeech clip. Timings are
warm graph inference medians after one warm-up; feature extraction is measured
separately and model load is excluded from RTFx. CPU results use ONNX Runtime
1.29 in WSL on an AMD Ryzen 5 7600X (6 cores, 12 threads). GPU results use
`onnxruntime-node` 1.24.3 with DirectML on an Intel Arc A750, driver
32.0.101.8860.

| Variant | CPU 12-thread load | CPU inference | CPU RTFx | DirectML load | DirectML inference | DirectML RTFx |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| FP32 | 9.64 s | 0.968 s | 30.36x | 2.30 s | 0.107 s | **275.55x** |
| FP16 | 4.99 s | 8.495 s | 3.46x | **1.32 s** | 0.179 s | 164.39x |
| Dynamic INT8 | **3.03 s** | **0.282 s** | **104.43x** | 1.50 s | 0.137 s | 214.08x |

Feature extraction took 0.11-0.12 seconds. FP16 is unsuitable for this CPU
provider: ONNX Runtime reports missing FP16 CPU kernels and its inference is
about 8.8 times slower than FP32. INT8 is about 3.4 times faster than FP32 on
CPU. On this particular DirectML/Arc stack, FP32 is fastest despite being the
largest graph; lower precision is not a universal speed guarantee.

The 1-to-6 thread speedup was 2.84x for FP32 and 3.23x for INT8. Going from 6
to 12 threads added only 1.45x and 1.25x respectively. That, full CPU
saturation, and weight reuse across hundreds of frames show that warm inference
is not purely memory-bandwidth-bound. INT8 moves it closer to memory and runtime
overhead limits. Cold load is much more clearly storage/memory-bound: reducing
the graph from FP32 to INT8 cut measured load from 9.64 to 3.03 seconds and peak
CPU-process RSS from about 3.72 GB to 1.71 GB. Hardware counters were not
available, so no stronger bandwidth claim is made.

The generated graphs and reports remain ignored build artifacts. Application
integration is separate follow-up work.

## Related community export

`diarizeapp/granite-speech-5.0-470m-turboctc-onnx` appeared while this work was
in progress. Its graph passed an independent 20-clip comparison, but the model
repository does not include conversion code or a parity report. This repository
therefore retains its independently reproduced exporter and validation gate.
