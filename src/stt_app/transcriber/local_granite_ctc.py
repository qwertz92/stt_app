"""IBM Granite Speech 5.0 470M TurboCTC on the app's own ONNX Runtime.

A fourth local ONNX path, next to the Cohere/Granite Node runtime
(`local_webgpu_asr`), Nemotron's ONNX Runtime GenAI path (`local_nemotron`) and
the onnx-asr models (`local_onnx_asr`). It is the only one that owns its
`InferenceSession` directly: the graph is a single CTC encoder with one input
and one output, so there is no runtime library between this module and ONNX
Runtime. The parts a runtime would normally provide live here -- the log-mel
feature extractor in numpy, and the greedy CTC decode plus a byte-level BPE
`tokenizers` decode -- which adds no dependency: `onnxruntime`, `numpy` and
`tokenizers` are all already installed.

The model is English-only, batch-only and CPU-only, and it writes lower-case
text without punctuation; its BPE vocabulary holds no multi-letter upper-case
token, so that is what it was trained to produce and not something to
post-process away.

Download, cache detection, size estimation and deletion go through the shared
layouts in `local_webgpu_asr`, and the resolve-or-download step through
`local_onnx_asr`, so only the feature extraction and inference live here.
"""

from __future__ import annotations

import io
import json
import logging
import math
import threading
from pathlib import Path

import numpy as np

from ..config import (
    AUDIO_SAMPLE_RATE,
    DEFAULT_LANGUAGE_MODE,
    DOC_MODELS_PATH,
    GRANITE_CTC_MODEL_SIZE,
    LOCAL_GRANITE_CTC_MODEL_SIZES,
    language_modes_for_selection,
)
from ._pcm_audio import resample_linear
from .base import (
    AudioInput,
    ITranscriber,
    ProgressReporter,
    TranscriptionCanceled,
    TranscriptionError,
)
from .local_onnx_asr import (
    _CancelWatchdog,
    _pcm_bytes_to_float32,
    _read_wav_float32,
    _RunAbortHandle,
    resolve_or_download_onnx_model,
)

logger = logging.getLogger(__name__)

# The feature parameters of `GraniteSpeech5FeatureExtractor`, hard-coded here
# and verified against the model folder's own `preprocessor_config.json` at
# load time (`_verify_preprocessor_config`).
N_FFT = 512
WIN_LENGTH = 400
HOP_LENGTH = 160
N_MELS = 80
STACK_FACTOR = 2
DELTA_WIN_LENGTH = 3
LOGMEL_FLOOR_DB = 8.0
#: One frame of the graph's input: [log-mel(80), delta(80)] for each of the two
#: stacked mel frames.
FEATURE_SIZE = STACK_FACTOR * 2 * N_MELS

#: Checked against the model folder's own `preprocessor_config.json` at load
#: time. Every value here is baked into the extractor above, so a snapshot
#: exported with different ones has to be refused rather than transcribed.
_EXPECTED_PREPROCESSOR_CONFIG: dict[str, object] = {
    "sample_rate": AUDIO_SAMPLE_RATE,
    "n_fft": N_FFT,
    "win_length": WIN_LENGTH,
    "hop_length": HOP_LENGTH,
    "n_mels": N_MELS,
    "stack_factor": STACK_FACTOR,
    "deltas": True,
    "delta_win_length": DELTA_WIN_LENGTH,
    "logmel_floor_db": LOGMEL_FLOOR_DB,
}

#: The one graph of the three the repository ships that the app downloads.
_GRAPH_RELATIVE_PATH = "onnx/model_int8.onnx"

#: CTC blank, `pad_token_id` in the model's `config.json`.
BLANK_ID = 0
#: The graph subsamples by 4, so fewer than four stacked frames produce no
#: output at all. 4 frames are 8 mel frames, i.e. 80 ms of audio.
_MIN_GRAPH_FRAMES = 4

# One pass allocates activations for the whole recording. Peak working set
# measured on the prototype of this runtime: 1.0 GB after the load, 1.4 GB for
# 188 s and 2.4 GB for 563 s, so a long recording is transcribed in consecutive
# windows instead.
_MAX_PASS_SECONDS = 180.0
# How far back from the end of a window the split point is looked for. The cut
# is the quietest 20 ms frame in that stretch, because the two passes share no
# audio and a cut inside a word loses it.
_SPLIT_SEARCH_SECONDS = 15.0
_SPLIT_FRAME_SAMPLES = 320
# Frames per STFT block. At 512 samples per frame this is an 8 MB float32 slice,
# which keeps a nine-minute recording from materialising one `frames x 512`
# matrix.
_STFT_BLOCK_FRAMES = 4096


def _mel_filterbank() -> np.ndarray:
    """`torchaudio.functional.melscale_fbanks(htk, norm=None)`, (n_freqs, n_mels)."""
    n_freqs = N_FFT // 2 + 1
    all_freqs = np.linspace(0.0, AUDIO_SAMPLE_RATE // 2, n_freqs)
    mel_max = 2595.0 * math.log10(1.0 + (AUDIO_SAMPLE_RATE / 2.0) / 700.0)
    mel_points = np.linspace(0.0, mel_max, N_MELS + 2)
    freq_points = 700.0 * (10.0 ** (mel_points / 2595.0) - 1.0)
    freq_diff = freq_points[1:] - freq_points[:-1]
    slopes = freq_points[None, :] - all_freqs[:, None]
    down = -slopes[:, :-2] / freq_diff[:-1]
    up = slopes[:, 2:] / freq_diff[1:]
    return np.maximum(0.0, np.minimum(down, up)).astype(np.float32)


def _analysis_window() -> np.ndarray:
    """A periodic Hann window of `WIN_LENGTH`, centred in an `N_FFT` frame.

    What `torch.stft(n_fft=512, win_length=400)` does with a shorter window:
    it zero-pads it symmetrically, 56 samples each side.
    """
    window = np.zeros(N_FFT, dtype=np.float32)
    offset = (N_FFT - WIN_LENGTH) // 2
    window[offset : offset + WIN_LENGTH] = 0.5 - 0.5 * np.cos(
        2.0 * np.pi * np.arange(WIN_LENGTH) / WIN_LENGTH
    )
    return window


_FILTERS = _mel_filterbank()
_WINDOW = _analysis_window()


def _mel_spectrogram(audio: np.ndarray, num_frames: int) -> np.ndarray:
    """Power mel spectrogram of `audio`, `num_frames` x `N_MELS`.

    The STFT is `torch.stft(center=True, pad_mode="reflect")`: the signal is
    reflect-padded by half a frame on both sides, so frame *t* is centred on
    sample `t * HOP_LENGTH`.
    """
    signal = np.pad(audio, N_FFT // 2, mode="reflect")
    offsets = np.arange(N_FFT)
    mel = np.empty((num_frames, N_MELS), dtype=np.float32)
    for first in range(0, num_frames, _STFT_BLOCK_FRAMES):
        last = min(first + _STFT_BLOCK_FRAMES, num_frames)
        starts = HOP_LENGTH * np.arange(first, last)
        frames = signal[starts[:, None] + offsets[None, :]] * _WINDOW
        power = np.abs(np.fft.rfft(frames, axis=1)) ** 2
        mel[first:last] = power @ _FILTERS
    return mel


def extract_features(waveform: np.ndarray) -> np.ndarray:
    """Turn a mono 16 kHz float waveform into the graph's `input_features`.

    Returns `(frames, FEATURE_SIZE)` float32, where each row holds two stacked
    mel frames as `[log-mel, delta]` each. Empty for audio shorter than one mel
    frame; the caller decides what a recording that short means.
    """
    audio = np.asarray(waveform, dtype=np.float32)
    mel_frames = audio.shape[0] // HOP_LENGTH
    # Whole stacked pairs only, so the waveform is right-padded when the mel
    # frame count is odd.
    num_frames = STACK_FACTOR * -(-mel_frames // STACK_FACTOR)
    if num_frames == 0:
        return np.zeros((0, FEATURE_SIZE), dtype=np.float32)
    needed = (num_frames - 1) * HOP_LENGTH + 1
    if audio.shape[0] < needed:
        audio = np.pad(audio, (0, needed - audio.shape[0]))

    mel = _mel_spectrogram(audio, num_frames)
    logmel = np.log10(np.maximum(mel, 1e-10))
    # The floor is relative to the loudest bin of the whole clip, so digital
    # silence cannot dominate the scale.
    logmel = np.maximum(logmel, logmel.max() - LOGMEL_FLOOR_DB) / 4.0 + 1.0
    # `torchaudio.functional.compute_deltas(win_length=3)`: a centred
    # difference with the edges replicated along time.
    padded = np.pad(logmel, ((1, 1), (0, 0)), mode="edge")
    deltas = (padded[2:] - padded[:-2]) / 2.0
    stacked = np.concatenate((logmel, deltas), axis=1)
    return stacked.reshape(-1, FEATURE_SIZE).astype(np.float32)


def ctc_greedy_token_ids(logits: np.ndarray) -> list[int]:
    """Greedy CTC decode: argmax per frame, collapse repeats, drop blanks."""
    best = np.asarray(logits).argmax(axis=-1)
    keep = np.ones(best.shape[0], dtype=bool)
    keep[1:] = best[1:] != best[:-1]
    return [int(token) for token in best[keep] if token != BLANK_ID]


def _quietest_cut(
    samples: np.ndarray,
    start: int,
    limit: int,
    search_samples: int,
) -> int:
    """Index of the quietest 20 ms frame in the last stretch before `limit`.

    Always strictly between `start` and `limit`, so the caller makes progress
    and no window exceeds its bound. Falls back to `limit` when the searched
    stretch does not hold a whole frame.
    """
    search_start = max(start + _SPLIT_FRAME_SAMPLES, limit - search_samples)
    usable = (limit - search_start) // _SPLIT_FRAME_SAMPLES * _SPLIT_FRAME_SAMPLES
    if usable < _SPLIT_FRAME_SAMPLES:
        return limit
    region = samples[search_start : search_start + usable]
    energies = (region.reshape(-1, _SPLIT_FRAME_SAMPLES) ** 2).mean(axis=1)
    return search_start + int(energies.argmin()) * _SPLIT_FRAME_SAMPLES


def split_into_passes(
    samples: np.ndarray,
    sample_rate: int = AUDIO_SAMPLE_RATE,
    *,
    max_seconds: float = _MAX_PASS_SECONDS,
    search_seconds: float = _SPLIT_SEARCH_SECONDS,
) -> list[np.ndarray]:
    """Cut a waveform into consecutive windows of at most `max_seconds`.

    The windows concatenate back to the input exactly -- nothing is dropped and
    nothing overlaps -- and a recording at or below the bound is returned
    unsplit. `max_seconds` and `search_seconds` are arguments so a test can
    drive the split on a short signal.
    """
    max_samples = int(max_seconds * sample_rate)
    search_samples = max(_SPLIT_FRAME_SAMPLES, int(search_seconds * sample_rate))
    if max_samples < _SPLIT_FRAME_SAMPLES or samples.size <= max_samples:
        return [samples]

    windows: list[np.ndarray] = []
    start = 0
    while samples.size - start > max_samples:
        cut = _quietest_cut(samples, start, start + max_samples, search_samples)
        windows.append(samples[start:cut])
        start = cut
    windows.append(samples[start:])
    return windows


class LocalGraniteCtcTranscriber(ITranscriber, ProgressReporter):
    """Batch transcription for Granite Speech 5.0 TurboCTC on ONNX Runtime."""

    def __init__(
        self,
        model_size: str = GRANITE_CTC_MODEL_SIZE,
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        *,
        offline_mode: bool = False,
        model_dir: str = "",
    ) -> None:
        ProgressReporter.__init__(self)
        if model_size not in LOCAL_GRANITE_CTC_MODEL_SIZES:
            raise TranscriptionError(f"Unsupported Granite CTC model '{model_size}'.")
        self.model_size = model_size
        self.offline_mode = bool(offline_mode)
        self.model_dir = model_dir or ""
        self._language_mode = self._normalize_language_mode(language_mode)
        self._session: object | None = None
        self._tokenizer: object | None = None
        self._model_lock = threading.Lock()
        # One run at a time: the abort handle below is per transcription, so
        # overlapping runs would let a cancel for one abort the other.
        self._inference_lock = threading.Lock()
        self._abort_handle: _RunAbortHandle | None = None
        # Reported to the benchmark and the runtime status line. This graph is
        # an INT8 CTC encoder built for the CPU provider; the app ships no ONNX
        # Runtime with a GPU provider, and installing one would break Nemotron.
        self.runtime_device = "cpu"
        self.gpu_available = False
        self.runtime_details_text = ""
        self.runtime_warning = (
            "This model runs on CPU through the ONNX Runtime the app already "
            "ships. It is English-only and writes lower-case text without "
            "punctuation."
        )

    def runtime_status_text(self) -> str:
        return "ONNX Runtime active on CPU"

    # -- language ---------------------------------------------------------

    def _normalize_language_mode(self, mode: str) -> str:
        """Restrict to what this model can answer with.

        The graph takes no language input at all: it decodes English, and a
        German selection would otherwise look like it had been applied. Auto
        and English are the same request here, and both are accepted so the
        app-wide default needs no exception.
        """
        requested = str(mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        supported = language_modes_for_selection("local", self.model_size)
        if requested in supported:
            return requested
        fallback = supported[0]
        logger.warning(
            "Language '%s' is not supported by '%s'; using '%s' instead. This "
            "model transcribes English and takes no language input.",
            requested,
            self.model_size,
            fallback,
        )
        return fallback

    # -- model lifecycle --------------------------------------------------

    def _verify_preprocessor_config(self, model_path: Path) -> None:
        """Refuse a re-export whose feature parameters are not the ones here.

        The extractor above hard-codes them, so a snapshot built with another
        `hop_length` or mel count would feed the graph something it cannot
        read and transcribe garbage without a single error.
        """
        path = model_path / "preprocessor_config.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TranscriptionError(
                f"Could not read the feature configuration of "
                f"'{self.model_size}' ({path}): {exc}"
            ) from exc
        if not isinstance(payload, dict):
            payload = {}
        for key, expected in _EXPECTED_PREPROCESSOR_CONFIG.items():
            found = payload.get(key)
            if found != expected:
                raise TranscriptionError(
                    f"'{self.model_size}' was exported with {key}={found!r}, "
                    f"but this app's feature extractor is fixed to "
                    f"{key}={expected!r}. Delete the model and download it "
                    f"again. See {DOC_MODELS_PATH}."
                )

    def _load_model(self) -> tuple[object, object]:
        model_path = resolve_or_download_onnx_model(
            self.model_size,
            self.model_dir,
            offline_mode=self.offline_mode,
            # `_is_cancel_requested`, not the raw attribute: a check that
            # raises must never fail the work.
            cancel_check=self._is_cancel_requested,
        )
        self._verify_preprocessor_config(model_path)
        self._emit_progress(
            f"Loading {self.model_size}: {self.runtime_status_text()}..."
        )
        try:
            import onnxruntime as rt
            from tokenizers import Tokenizer

            tokenizer = Tokenizer.from_file(str(model_path / "tokenizer.json"))
            session = rt.InferenceSession(
                str(model_path / _GRAPH_RELATIVE_PATH),
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:
            raise TranscriptionError(
                f"Failed to load local model '{self.model_size}': {exc}"
            ) from exc
        return session, tokenizer

    def preload_model(self) -> None:
        with self._model_lock:
            if self._session is None:
                self._session, self._tokenizer = self._load_model()

    def close(self) -> None:
        # `_inference_lock` too, in the same order `transcribe_batch` takes
        # them, so the session is never dropped while a run is using it.
        with self._model_lock, self._inference_lock:
            self._session = None
            self._tokenizer = None

    # -- transcription ----------------------------------------------------

    def _waveform_from(self, audio_source: AudioInput) -> np.ndarray:
        if isinstance(audio_source, (str, Path)):
            waveform, sample_rate = _read_wav_float32(Path(audio_source))
        elif isinstance(audio_source, (bytes, bytearray)):
            payload = bytes(audio_source)
            if payload[:4] == b"RIFF":
                waveform, sample_rate = _read_wav_float32(io.BytesIO(payload))
            else:
                waveform = _pcm_bytes_to_float32(payload)
                sample_rate = AUDIO_SAMPLE_RATE
        else:
            raise TranscriptionError("Unsupported audio input for a local model.")
        return resample_linear(waveform, sample_rate, AUDIO_SAMPLE_RATE)

    def _transcribe_window(
        self,
        window: np.ndarray,
        session: object,
        tokenizer: object,
        handle: _RunAbortHandle,
    ) -> str:
        features = extract_features(window)
        if features.shape[0] < _MIN_GRAPH_FRAMES:
            # The graph subsamples by 4 and would produce no frame at all.
            return ""
        logits = session.run(  # type: ignore[attr-defined]
            None,
            {"input_features": features[None]},
            run_options=handle.options,
        )[0][0]
        return str(
            tokenizer.decode(ctc_greedy_token_ids(logits))  # type: ignore[attr-defined]
        ).strip()

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        self._raise_if_canceled()
        waveform = self._waveform_from(audio_source)
        if waveform.size == 0:
            return ""

        with self._model_lock:
            if self._session is None:
                self._session, self._tokenizer = self._load_model()
            session = self._session
            tokenizer = self._tokenizer

        self._emit_progress(f"Transcribing with {self.runtime_status_text()}...")
        with self._inference_lock:
            self._raise_if_canceled()
            if self._session is not session:
                # This method takes its two locks *sequentially*: it releases
                # `_model_lock` above before acquiring this one, so a `close()`
                # landing in that gap gets both uncontended and drops the
                # session this run was about to use. A `TranscriptionError`
                # and not a cancel, because the controller's cancel path
                # discards the recording while the error path keeps it for
                # Retry -- and nobody asked to stop here.
                raise TranscriptionError(
                    "The local runtime was closed while this transcription "
                    "was starting. Try again."
                )
            handle = _RunAbortHandle()
            self._abort_handle = handle
            # The base method, and only when a check is installed: `_poll`
            # gives up permanently on its first raise, and
            # `self._is_cancel_requested` is never None, so passing it
            # unconditionally would spawn a poll thread for every run.
            watchdog = _CancelWatchdog(
                handle,
                self._is_cancel_requested if self._cancel_check is not None else None,
            )
            watchdog.start()
            try:
                pieces: list[str] = []
                for window in split_into_passes(waveform):
                    # Between windows, because each one is a fresh graph call
                    # that would otherwise run to its end.
                    self._raise_if_canceled()
                    text = self._transcribe_window(window, session, tokenizer, handle)
                    if text:
                        pieces.append(text)
                transcript = " ".join(pieces)
            except TranscriptionCanceled:
                raise
            except Exception as exc:
                if handle.aborted:
                    # ONNX Runtime reports the abort as a generic failure; it
                    # is a user cancel, not a transcription failure.
                    raise TranscriptionCanceled() from exc
                raise TranscriptionError(
                    f"Local transcription failed for '{self.model_size}': {exc}"
                ) from exc
            finally:
                watchdog.stop()
                self._abort_handle = None
        return transcript.strip()
