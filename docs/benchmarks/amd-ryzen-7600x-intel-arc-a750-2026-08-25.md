# Benchmark: AMD Ryzen 7600X and Intel Arc A750

Date: 2026-08-25 (the stored timestamp is 2026-08-25 22:34 UTC)

This is the run that made `parakeet-tdt-0.6b-v3` the default model and the
one every Parakeet figure in this repository cites. Nemotron was measured here
too, but its published figures come from the 2026-07-10 run on a 28.1-second
clip; both are quoted in `AGENTS.md`, each named with its own run. The values
come from `benchmark_history.json`; they are not estimates. It is the first
run to measure Parakeet and the first to compare all three ONNX device targets
in one sitting. Canary was selected for it but produced no measurement -- see
the error table at the end -- so no Canary figure can cite this run, or any
other retained here.

The 24.30-second German speech recording is not committed because it
contains private audio, and neither are the decoded transcripts.

## Hardware and runtime

| Component | Value |
| --------- | ----- |
| CPU | AMD Ryzen 5 7600X 6-Core Processor, 12 logical CPUs |
| GPU | Intel(R) Arc(TM) A750 Graphics (driver 32.0.101.8860) |
| RAM | 31.6 GB |
| OS | Windows 11 (10.0.26200) AMD64 |
| App/source | `0.9.0`, source `6f3ff3e9ff73` |
| Python | CPython 3.12.10 64bit |
| faster-whisper / CTranslate2 | 1.2.1 / 4.8.1 |
| Transformers.js / ONNX Runtime Node | 4.1.0 / 1.24.3 |
| ONNX Runtime / ORT GenAI | 1.28.0 / 0.15.2 |
| Node.js | v24.18.0 |
| Nemotron providers | CPU only |

## Settings

| Setting | Value |
| ------- | ----- |
| Audio duration | 24.30 seconds |
| Compute type | `int8` for faster-whisper |
| Runs | 2 measured runs per model |
| Beam size | 5 |
| Language | Auto |
| Warm-up | yes |
| VAD | no |
| ONNX targets | webgpu, dml, cpu |

## Results

Lower times and RTF are better. Load is measured separately. Average and RTF
cover all measured runs of that case.

**Agreement** is how much of each transcript's word sequence matches
`large-v3`'s, computed exactly as:

```python
tokens = re.findall(r"\w+", transcript.lower())
difflib.SequenceMatcher(None, reference_tokens, candidate_tokens,
                        autojunk=False).ratio()
```

Every detail there is load-bearing and was got wrong once. `autojunk=False`
matters because `difflib` silently discards popular elements of the *second*
sequence once it exceeds 200 items, which halved the score of the one
transcript long enough to trigger it (Plus, 378 tokens: 1.4% with the
heuristic on, 2.8% with it off). Argument order matters for the same reason.
Both runs of every case produced byte-identical transcripts, so run 1, run 2
and any average give the same number.

**What this measure can and cannot do.** It is not a word error rate: there is
no human reference, it is one 24.3-second German recording, and `large-v3` is
a stand-in for the truth rather than the truth -- on the single token that
separates the leaders it is the one that is wrong, writing `transkriptiere`,
which is not a German word, where Parakeet, `large-v3-turbo` and Cohere all
write the correct `transkribiere`.

So it **cannot order the leading cluster**. Re-run against each of the 13
working transcripts as the reference in turn, Parakeet's rank moves between
1st and 8th, `large-v3-turbo`'s between 1st and 6th, Cohere's between 1st and
8th; those differences are one or two tokens out of 52. What it *does* support,
and what it is here for, is robust under every choice of reference: Plus ranks
last of 12 every time and NAR 11th or 12th -- neither transcribed the
recording -- and `tiny` ranks 10th or 11th every time, clearly the weakest of
the models that did.

| Model | Device | Load | Runs | Average | RTF | Agreement |
| ----- | ------ | ---: | ---- | ------: | --: | --------: |
| `tiny` | `cpu` | 0.62s | 0.80s, 0.81s | 0.80s | 0.033 | 82.7% |
| `base` | `cpu` | 0.33s | 1.66s, 1.74s | 1.70s | 0.070 | 89.1% |
| `small` | `cpu` | 0.96s | 3.69s, 3.77s | 3.73s | 0.154 | 91.3% |
| `medium` | `cpu` | 2.50s | 10.09s, 9.68s | 9.89s | 0.407 | 95.1% |
| `large-v3` | `cpu` | 4.45s | 15.28s, 15.01s | 15.15s | 0.623 | 100% (reference) |
| `large-v3-turbo` | `cpu` | 2.18s | 9.18s, 9.08s | 9.13s | 0.376 | 97.1% |
| `cohere-transcribe-03-2026` | `webgpu` | 3.74s | 2.05s, 1.97s | 2.01s | 0.083 | 97.1% |
| `cohere-transcribe-03-2026` | `cpu` | 2.91s | 3.22s, 3.22s | 3.22s | 0.132 | 97.1% |
| `granite-4.0-1b-speech` | `webgpu` | 4.43s | 2.42s, 2.36s | 2.39s | 0.098 | 92.2% |
| `granite-4.0-1b-speech` | `cpu` | 2.26s | 10.00s, 9.86s | 9.93s | 0.409 | 92.2% |
| `granite-speech-4.1-2b` | `webgpu` | 4.47s | 2.42s, 2.39s | 2.41s | 0.099 | 97.1% |
| `granite-speech-4.1-2b` | `cpu` | 2.33s | 11.23s, 11.13s | 11.18s | 0.460 | 97.1% |
| `granite-speech-4.1-2b-plus` | `cpu` | 7.77s | 101.11s, 100.55s | 100.83s | 4.149 | 2.8% |
| `granite-speech-4.1-2b-nar` | `cpu` | 4.52s | 11.19s, 10.53s | 10.86s | 0.447 | 63.2% |
| `nemotron-3.5-asr-streaming-0.6b-int4` | `cpu` | 1.78s | 5.11s, 5.01s | 5.06s | 0.208 | 90.4% |
| `parakeet-tdt-0.6b-v3` | `cpu` | 1.92s | 1.04s, 1.03s | 1.03s | 0.043 | 98.1% |

## What this run settled

- **`parakeet-tdt-0.6b-v3` is not the fastest case in this run, and the
  distinction is the whole argument for it.** `tiny` is quicker -- 0.033
  against 0.043, 1.29x -- and it is the *only* model that is. It is also the
  weakest of the models that transcribed the recording, robustly so: 82.7%
  against `large-v3`, and 10th or 11th of 12 whichever transcript is taken as
  the reference. Parakeet sits in the leading cluster, which this measure
  cannot rank internally. So the claim the default rests on is "fastest of the
  models that transcribed the recording", not "most accurate" -- and that is
  what made it the default a fresh install uses.
- Against `small`, the previous default, on the same recording and the same
  device: 0.043 against 0.154, i.e. **3.6x faster**. It is also ahead on
  agreement, 98.1% against 91.3%, and that particular comparison does survive
  every choice of reference -- unlike the differences inside the leading
  cluster. The per-run values are 0.0428/0.0423 and 0.1520/0.1553; where this
  repository quotes 0.042 and 0.152 it is naming the faster run of each rather
  than the mean this file publishes, and the ratio is 3.6x either way.
- It is also faster than every GPU case measured. The quickest of those is
  `cohere-transcribe-03-2026` at 0.083 on WebGPU, so Parakeet on plain CPU is
  **1.9x** faster than the best local GPU result and no GPU path is needed for
  the best local latency. (Granite Speech 4.1 2B at 0.099 is the *slowest* of
  the three GPU cases, not the quickest; an earlier version of this file and
  of `AGENTS.md` compared against it and reported 2.3x.)
- Granite Speech 4.1 Plus and NAR were retired on the strength of these
  numbers, and the agreement column says why as plainly as the RTF does: both
  fall back to CPU, NAR at 0.43-0.46 with 63.2% agreement and 43 words against
  the reference's 52, and Plus at 4.14-4.16 with 2.8% agreement and 378 words
  -- it looped one clause until it hit the token limit, which is also why its
  RTF is so bad.
- DirectML is not usable for the Cohere/Granite runtime on this machine;
  every `dml` case failed and the results above show only what ran.

## Cases that did not produce a measurement

| Model | Device | Error |
| ----- | ------ | ----- |
| `cohere-transcribe-03-2026` | `dml` | ONNX/WebGPU transcription failed: Error: Non-zero status code returned while running MultiHeadAttention node. Name:'/... |
| `granite-4.0-1b-speech` | `dml` | ONNX/WebGPU transcription failed: Error: Non-zero status code returned while running Reshape node. Name:'node_view' S... |
| `granite-speech-4.1-2b` | `dml` | ONNX/WebGPU transcription failed: Error: Non-zero status code returned while running Reshape node. Name:'node_view' S... |
| `granite-speech-4.1-2b-plus` | `webgpu` | ONNX/WebGPU transcription failed: Error: Non-zero status code returned while running Einsum node. Name:'/encoder/laye... |
| `granite-speech-4.1-2b-plus` | `dml` | ONNX/WebGPU transcription failed: Error: Non-zero status code returned while running FusedMatMul node. Name:'/encoder... |
| `granite-speech-4.1-2b-nar` | `webgpu` | ONNX/WebGPU transcription failed: Error: Non-zero status code returned while running Einsum node. Name:'/encoder/laye... |
| `granite-speech-4.1-2b-nar` | `dml` | ONNX/WebGPU transcription failed: Error: Non-zero status code returned while running FusedMatMul node. Name:'/encoder... |
| `canary-1b-v2` | `auto` | Canary cannot detect the language. Choose the language spoken in the sample before benchmarking it; with the wrong on... |

`canary-1b-v2` has no measurement in any run retained on this machine, so no
Canary RTF -- and no ratio derived from one -- should be quoted anywhere until
one exists.
