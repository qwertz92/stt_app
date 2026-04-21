# Benchmark: AMD Ryzen 7600X and Intel Arc A750

Date: 2026-04-22

## Hardware

| Component | Value |
| --------- | ----- |
| CPU | AMD Ryzen 7600X |
| GPU | Intel Arc A750 8GB |
| RAM | 32GB DDR5 6000 |

## Settings

| Setting | Value |
| ------- | ----- |
| Compute type | `int8` |
| Runs | 3 |
| Beam size | 5 |
| Language | Auto |
| Warmup | yes |
| VAD | no |

## Results

| Model | Runtime | Load | Average | RTF | Status |
| ----- | ------- | ---- | ------- | --- | ------ |
| `tiny` | auto/int8 | 0.21s | 1.37s | 0.026 | ok |
| `small` | auto/int8 | 0.86s | 7.91s | 0.151 | ok |
| `medium` | auto/int8 | 2.56s | 22.21s | 0.423 | ok |
| `large-v3-turbo` | auto/int8 | 2.48s | 18.61s | 0.355 | ok |
| `cohere-transcribe-03-2026` | webgpu/onnx-q4 | 5.33s | 3.75s | 0.071 | ok |
| `cohere-transcribe-03-2026` | cpu/onnx-q4 | 2.17s | 7.20s | 0.137 | ok |
| `granite-4.0-1b-speech` | webgpu/onnx-q4 | 3.86s | 3.12s | 0.059 | ok |
| `granite-4.0-1b-speech` | cpu/onnx-q4 | 1.84s | 19.98s | 0.381 | ok |

## Summary

- Fastest average latency: `tiny` on auto (1.37s)
- Best real-time factor: `tiny` on auto (0.026)
- RTF < 1.0 means faster than real-time.
