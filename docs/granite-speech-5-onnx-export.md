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

- ONNX size: 1,892,339,420 bytes
- ONNX SHA-256:
  `cc647a0681142d6651dc14bf582cf2ef06da1b1d0978f078bf66b56a407fcddb`
- Full-model subsampling replacement maximum absolute difference: `0.0`
- Validation: 31/31 exact CTC token streams and decoded transcripts
- Maximum absolute logit difference: `0.0009813308715820312`
- Weighted mean absolute logit difference: `0.000002765029560252943`
- Minimum token argmax agreement: `1.0`
- Dynamic feature lengths were smoke-tested through 2,048 frames; the focused
  parity gate covers 4 through 1,470 frames

The generated graph and reports are ignored build artifacts, not Git content.
FP16/INT8 conversion, WebGPU/DirectML verification, Hugging Face publication,
and application integration are separate follow-up work.

## Related community export

`diarizeapp/granite-speech-5.0-470m-turboctc-onnx` appeared while this work was
in progress. Its graph passed an independent 20-clip comparison, but the model
repository does not include conversion code or a parity report. This repository
therefore retains its independently reproduced exporter and validation gate.
