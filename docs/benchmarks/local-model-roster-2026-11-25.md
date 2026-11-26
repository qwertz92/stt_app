# Local speech-model measurements, 2026-11-25

Measured for the local-roster revision of 2026-11-25 (`docs/agents/local-models-and-downloads.md`,
"Roster of 2026-11-25"). The audio clips and the scripts stayed in the measuring session's scratch
folder and are not in the repository; FLEURS is public, so the set can be rebuilt from the
description below. Rows for the multilingual TurboCTC export belong to the branch
`turboctc-multilingual`, which is not part of the app's roster.

## Evaluation set

- Source: Google FLEURS `test` split, revision `70bb2e84` of `google/fleurs`, licence CC-BY-4.0. The head of `de_de` and `en_us` `test.tar.gz` was streamed (about 114 MB and 73 MB).
- 25 clips per language, picked evenly from the first 120 streamed clips: German 349.5 s with 490 reference words, English 233.9 s with 532 reference words. The clips were converted to 16-bit 16 kHz mono WAV.
- No private recording and no History entry was used. `bench_recording.wav` was skipped because no reference text exists.
- WER is corpus WER with one normaliser for every model (NFKC, casefold, anything that is not a letter or digit becomes a space, apostrophes dropped; numbers are not converted).
- RTF is processing time divided by audio time, after one unscored warm-up clip. The PC was shared with test runs and the owner's app during every run, so RTF is noisy (quiet-PC smoke run: Parakeet v3 0.052). WER is the deciding figure.
- Caveats: about 500 words per language, so one point of WER is about five words and differences below roughly 1 point are noise. FLEURS may be part of Whisper's training data, which flatters Whisper.

## Full set (25 + 25 clips)

| Model | Runtime, device | Download | de WER % | en WER % | RTF de / en |
|---|---|---|---|---|---|
| Parakeet TDT 0.6B v3 | onnx-asr, CPU | 670 MB | 6.50 | 4.89 | 0.085 / 0.084 |
| **Fluid Speech Pro** (rev fa588e9, int8) | onnx-asr 0.12.0, CPU | 801 MB | **3.25** | **3.57** | 0.22 / 0.26 |
| Canary 1B v2 | onnx-asr, CPU | 1,029 MB | 3.66 | 5.45 | 0.15 / 0.16 |
| Granite 5.0 470M TurboCTC (shipped English INT8) | CPU | 552 MB | n/a (English only) | 5.83 | 0.023 |
| Granite 5.0 470M TurboCTC multilingual INT8 (my export of 91dcb06) | CPU | 551 MB | 4.27 | 4.14 | 0.023 / 0.024 |
| same, fp32 export | CPU | 1.9 GB | 3.46 | 4.70 | 0.068 |
| Whisper small | faster-whisper int8, CPU | 486 MB | 6.10 | 5.64 | 0.18 / 0.38 |
| Whisper medium | faster-whisper int8, CPU | 1,531 MB | 3.46 | 4.14 | 0.42 / 0.75 |
| Whisper large-v3-turbo | faster-whisper int8, CPU | 1,622 MB | 2.64 | 3.38 | 0.48 / 0.73 |
| Cohere Transcribe | Node, WebGPU (Arc A750) | 2,128 MB | 3.25 | 4.32 | 0.062 |
| Granite 4.0 1B (as shipped, legacy prompt) | WebGPU | | 10.37 | 11.47 | 0.11 |
| Granite 4.1 2B (as shipped, legacy prompt) | WebGPU | | 10.57 | 13.53 | 0.088 |
| Granite 5.0 2B, legacy prompt | WebGPU | | 15.04 | 6.20 | 0.08 |
| Granite 5.0 2B, language-ID prompt (lower case, unpunctuated) | WebGPU | | 4.88 | 5.45 | 0.087 / 0.098 |
| **Granite 5.0 2B, punctuation prompt only (what the app sends in Auto)** | WebGPU, onnxruntime-node 1.29.0 | 1,730 MB | **3.46** | **4.89** | 0.098 |
| **Granite 5.0 2B, language-ID + punctuation (what the app sends once a language is chosen)** | WebGPU, final runner | 1,730 MB | **3.46** | **5.26** | 0.073 / 0.077 |

## First 10 clips per language (the same subset for every model in this table)

| Model | de WER % | en WER % | RTF de / en |
|---|---|---|---|
| Whisper large-v3 | 0.57 | 4.98 | 0.61 / 0.86 |
| Whisper large-v3-turbo | 2.30 | 4.48 | 0.55 / 0.78 |
| Whisper medium | 0.57 | 3.98 | 0.44 / 0.91 |
| Whisper small | 4.02 | 6.47 | 0.19 / 0.36 |
| Fluid Speech Pro | 0.57 | 3.48 | 0.16 to 0.20 / 0.18 to 0.20 (two runs) |
| Canary 1B v2 | 1.15 | 4.48 | 0.13 / 0.14 |
| Parakeet v3 | 7.47 | 3.98 | 0.052 to 0.067 / 0.046 to 0.064 (two runs) |
| Cohere | 1.72 | 4.48 | 0.081 / 0.071 |
| Granite 5.0 2B (punctuation prompt) | 0.57 | 8.46 | 0.10 / 0.11 |
| TurboCTC multilingual INT8 | 1.15 | 6.47 | 0.024 / 0.021 |
| TurboCTC multilingual fp32 | 1.15 | 5.97 | 0.072 / 0.067 |
| TurboCTC English (shipped) | n/a | 4.98 | 0.023 |

Whisper large-v3 was only run on the 10-clip subset (a full run needs more than ten minutes on the shared PC).

## Findings

- Parakeet v3 in Auto mode answered in English on about 3 of 25 German clips ("In this period in the European Geschichte ..."), which is most of its 6.5 % German WER. Fluid Speech Pro and Canary did not.
- Granite 4.x's shipped prompt makes the models paraphrase; the 5.0 2B needs IBM's punctuation instruction (and the language-ID sentence when a language is picked) or it switches language.
- The multilingual TurboCTC runs on the app's existing feature extractor, greedy CTC and `tokenizers` decode. Output is punctuated and capitalised, `<|nospeech|>` (id 1) is a special token the default decode skips (silence decodes to an empty string), blank is id 0. It has no language input.
- INT8 against fp32 for the multilingual TurboCTC: German 4.27 against 3.46, English 4.14 against 4.70 on the full set. The two differ by amounts inside the sample's noise; INT8 is 3 times faster and a third the size.

- The prepared hosted folder (`agent40\hosted\granite-speech-5.0-470m-turboctc-multilingual-onnx`) was also run through the app's own `LocalGraniteCtcTranscriber` (second commit): German 4.27 %, English 4.14 %, RTF 0.015 / 0.018, identical WER to the direct-session run above.

## Not measured

- Parakeet Ultra: no official export exists, and the community one is not downloaded here. The Ultra removal rests on fluid-speech-pro beating v3 (Ultra was behind v3 on WER in the project's earlier measurement, `docs/models.md`).
- distil-large-v3.5 (not installed; kept by rule), Nemotron, and the owner's private recording.
- No German-only or English-only noise, accent or long-form test; FLEURS clips are 10 to 20 s read sentences.

## Downloads (all into a throwaway folder, nothing into the user cache or the app's model dir)

- `FluidVoice-Labs/fluid-speech-pro-onnx` at fa588e9f87f1b9ad0ca2e2d8f7eaf84dc3803581, int8 pair plus config, vocab, LICENSE, NOTICE, README: about 801 MB. CC-BY-4.0.
- `ibm-granite/granite-speech-5.0-2b-ONNX` (the q4 files, run through the app's own download code in a throwaway model dir): 1,729,810,523 bytes. Apache-2.0.
- `ibm-granite/granite-speech-5.0-470m-turboctc` at 91dcb0605daf65f88dbb04fef015c423241d941d, `model.safetensors`: 946,311,576 bytes. Apache-2.0.
- `qwertz92/granite-speech-5.0-470m-turboctc-onnx` rev e6e3b4d: the owner's export scripts and README only (small).
- FLEURS head: about 187 MB streamed, 25 + 25 clips kept.
