# NVIDIA Parakeet — Evaluation for tts_app

This document summarizes our evaluation of NVIDIA's Parakeet ASR models for potential integration into tts_app.

## Current project status

- **Status:** Not implemented by design.
- **Decision:** Keep the app focused on faster-whisper local inference and selected remote providers.
- **Reason:** The GPU-only/runtime footprint tradeoff is still unfavorable for the target audience.

## Summary

**Verdict: Not recommended for implementation at this time.** Parakeet delivers excellent quality but requires an NVIDIA GPU, introduces massive dependencies (PyTorch + NeMo), and uses a completely different framework from the current faster-whisper/CTranslate2 stack. The added complexity would benefit only users with NVIDIA GPUs.

---

## What is Parakeet?

Parakeet is NVIDIA's family of speech recognition models built on the [FastConformer-TDT](https://arxiv.org/abs/2305.05084) architecture, released under the permissive CC-BY-4.0 license.

| Model | Languages | Params | Size |
|-------|-----------|--------|------|
| [parakeet-tdt-0.6b-v2](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2) | English only | 600M | ~1.2 GB |
| [parakeet-tdt-0.6b-v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) | 25 European languages (incl. German) | 600M | ~1.2 GB |

Key features:

- Automatic punctuation and capitalization
- Word-level and segment-level timestamps
- Long audio support (up to 24 min with full attention, 3h with local attention)
- Automatic language detection (v3)
- Streaming inference supported via chunked decoding

---

## Quality comparison

### English (LibriSpeech clean)

| Model | WER (%) |
|-------|--------:|
| **Parakeet v3** | **1.93** |
| **Parakeet v2** | **1.69** |
| Whisper large-v3 (est.) | ~2.5 |
| Whisper large-v3-turbo | ~2.5 |
| distil-large-v3.5 | ~2.5 |
| Whisper small | 3.3 |

### German (FLEURS)

| Model | WER (%) |
|-------|--------:|
| **Parakeet v3** | **5.04** |
| Whisper large-v3 (est.) | ~4.5 |
| Whisper medium | 6.5 |
| Whisper small | 10.2 |

**Conclusion:** Parakeet v3 is state-of-the-art for English and competitive for German. However, the quality difference vs Whisper large-v3 is small for German dictation use cases.

---

## Technical requirements

| Requirement | Parakeet | Current app (faster-whisper) |
|-------------|----------|------------------------------|
| **GPU** | NVIDIA required (Ampere, Volta, Hopper, Blackwell) | Optional (CPU works) |
| **GPU RAM** | Min 2 GB | Not needed |
| **Framework** | NeMo (PyTorch) | CTranslate2 |
| **Runtime** | `nemo_toolkit[asr]` + PyTorch | `faster-whisper` + `ctranslate2` |
| **Install size** | ~2-4 GB additional (PyTorch + NeMo) | ~200 MB |
| **OS** | Linux preferred, Windows possible | Windows 11 |
| **CPU inference** | Not officially supported | Primary use case |

### The GPU problem

Most target users of this app run on corporate laptops with Intel or AMD integrated graphics — no NVIDIA GPU. Parakeet would only benefit the subset of users with NVIDIA GPUs, while adding significant installation complexity for everyone.

### The dependency problem

`nemo_toolkit[asr]` pulls in PyTorch (~800 MB wheel) plus numerous scientific computing packages. This would:

- 3-5x the total installation size
- Increase install time significantly
- Complicate corporate/offline deployment
- Require CUDA toolkit for GPU inference

---

## Integration approach (if implemented)

This section is intentionally kept as a historical/technical reference only. It does **not** describe the current runtime.

If NVIDIA GPU usage becomes a priority (e.g. power users, server deployment), Parakeet could be added as an **optional** engine:

1. **New transcriber provider**: `src/tts_app/transcriber/nemo_provider.py` implementing the `ITranscriber` interface.
2. **Optional dependency group**: `uv sync --group nemo` to install NeMo/PyTorch only when needed.
3. **GPU detection**: Check for CUDA availability at engine selection time, show clear error if no GPU.
4. **Config changes**: Add `"nemo"` to `VALID_ENGINES`, Parakeet models to a separate model map.

Usage would be:

```python
import nemo.collections.asr as nemo_asr
model = nemo_asr.models.ASRModel.from_pretrained("nvidia/parakeet-tdt-0.6b-v3")
output = model.transcribe(["audio.wav"])
text = output[0].text
```

Streaming is possible via NeMo's [chunked inference script](https://github.com/NVIDIA/NeMo/blob/main/examples/asr/asr_chunked_inference/rnnt/speech_to_text_streaming_infer_rnnt.py).

---

## Recommendation

| Factor | Assessment |
|--------|-----------|
| Quality | Excellent — state-of-the-art for English, competitive for German |
| Practicality | Poor — GPU-only, massive deps, Linux-preferred |
| Target audience fit | Low — most users have Intel/AMD laptops without NVIDIA GPUs |
| Implementation effort | High — new framework, new provider, optional dep management |
| Cost/benefit ratio | Unfavorable for current scope |

**Keep this evaluation on file.** Revisit if:

- The app expands to server/cloud deployment (where GPUs are common)
- NeMo adds a lightweight CPU inference path
- ONNX export of Parakeet models becomes mature and well-supported
- User demand for NVIDIA GPU acceleration increases

---

## Sources

- [nvidia/parakeet-tdt-0.6b-v2](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2) — English-only model card
- [nvidia/parakeet-tdt-0.6b-v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) — Multilingual model card / technical report
- [NVIDIA NeMo Toolkit](https://github.com/NVIDIA/NeMo) — Framework
- [Parakeet v3 Technical Report](https://arxiv.org/abs/2509.14128) — Full paper
