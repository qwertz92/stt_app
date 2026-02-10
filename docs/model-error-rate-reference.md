# Model Error-Rate Reference (WER/CER)

This file summarizes published error-rate references relevant to models offered in this app.

Important:
- WER/CER values are dataset-specific and decoding-specific.
- They are references, not guaranteed real-world values on your own microphone/audio.
- There is no single universal "true WER" per model.

## Models offered in app

- `tiny`
- `base`
- `small`
- `medium`
- `large-v3`

## 1) English benchmark reference (Whisper paper, beam-search table)

Source basis:
- Whisper paper source archive (`tables/english-bs5fb.tex`)
- Metric is WER (%), lower is better.

Table below uses:
- LibriSpeech test-clean WER
- mean WER over 14 English benchmark sets from that table

| Model | LibriSpeech test-clean WER | Mean WER over 14 English sets |
|---|---:|---:|
| tiny | 6.7 | 21.71 |
| base | 4.9 | 17.61 |
| small | 3.3 | 13.96 |
| medium | 2.7 | 12.50 |
| large-v3 | n/a in this table (see note) | n/a in this table |

Note for `large-v3`:
- the Whisper paper table predates `large-v3` and reports `large` / `large-v2`.
- closest paper row is `large-v2`: LibriSpeech test-clean WER 2.5, mean 11.80.

## 2) Multilingual examples by language (Whisper paper FLEURS table)

Source basis:
- Whisper paper source archive (`tables/fleurs-asr.tex`)
- WER (%), lower is better.

Selected languages (for practical comparison):

### German (FLEURS)
| Model | WER (%) |
|---|---:|
| tiny | 27.8 |
| base | 17.9 |
| small | 10.2 |
| medium | 6.5 |
| large-v3 | n/a in this table (closest `large-v2`: 4.5) |

### English (FLEURS)
| Model | WER (%) |
|---|---:|
| tiny | 12.4 |
| base | 8.9 |
| small | 6.1 |
| medium | 4.4 |
| large-v3 | n/a in this table (closest `large-v2`: 4.2) |

## 3) Multilingual Speech (MLS) reference

Source basis:
- Whisper paper source archive (`tables/mls-asr.tex`)
- WER (%), lower is better.

### MLS English
| Model | WER (%) |
|---|---:|
| tiny | 15.7 |
| base | 11.7 |
| small | 8.3 |
| medium | 6.8 |
| large-v3 | n/a in this table (closest `large-v2`: 6.2) |

### MLS German
| Model | WER (%) |
|---|---:|
| tiny | 24.9 |
| base | 17.7 |
| small | 10.5 |
| medium | 7.4 |
| large-v3 | n/a in this table (closest `large-v2`: 5.5) |

## 4) Distil benchmark note (faster-whisper README)

Published benchmark snippet includes:
- `distil-whisper-large-v3` YT Commons WER: `13.527` in a GPU comparison table.

This does not directly replace the Whisper paper rows above and should be treated as a separate benchmark context.

## 5) How to use this reference in practice

1. Use these values to choose candidates (`small`, `medium`, maybe larger).
2. Run local latency/RTF benchmark on your hardware (`scripts/benchmark_local.py`).
3. Validate quality with your own language/domain audio (best predictor for production experience).

## Sources

- Whisper paper source archive:
  - https://arxiv.org/e-print/2212.04356
- Whisper repository/model docs:
  - https://github.com/openai/whisper
  - https://raw.githubusercontent.com/openai/whisper/main/model-card.md
- faster-whisper benchmark table:
  - https://github.com/SYSTRAN/faster-whisper
