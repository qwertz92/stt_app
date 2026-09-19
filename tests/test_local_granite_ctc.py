"""Tests for IBM Granite Speech 5.0 470M TurboCTC (numpy + ONNX Runtime).

None of these needs the real 551 MB graph: the feature extractor is pure numpy
and is checked against a reference produced by the real `transformers`
processor, and `transcribe_batch` runs against a fake `InferenceSession` that
returns prepared logits.
"""

from __future__ import annotations

import io
import json
import logging
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from stt_app.config import (
    GRANITE_CTC_MODEL_SIZE,
    LOCAL_BATCH_ONLY_MODELS,
    LOCAL_ENGLISH_ONLY_MODELS,
    LOCAL_GRANITE_CTC_MODEL_SIZES,
    LOCAL_MODEL_RUNTIME,
    LOCAL_ONNX_MODEL_SIZES,
    MODEL_REPO_MAP,
    language_modes_for_selection,
    supports_streaming,
)
from stt_app.settings_store import AppSettings
from stt_app.transcriber.base import TranscriptionCanceled, TranscriptionError
from stt_app.transcriber.factory import create_transcriber
from stt_app.transcriber.local_granite_ctc import (
    FEATURE_SIZE,
    HOP_LENGTH,
    LocalGraniteCtcTranscriber,
    ctc_greedy_token_ids,
    extract_features,
    split_into_passes,
)

_REFERENCE = Path(__file__).resolve().parent / "data" / "granite_ctc_reference.npz"


def test_the_model_is_registered_as_a_local_cpu_only_batch_only_model():
    assert LOCAL_GRANITE_CTC_MODEL_SIZES == (GRANITE_CTC_MODEL_SIZE,)
    assert GRANITE_CTC_MODEL_SIZE in LOCAL_ONNX_MODEL_SIZES
    assert GRANITE_CTC_MODEL_SIZE in LOCAL_BATCH_ONLY_MODELS
    assert GRANITE_CTC_MODEL_SIZE in LOCAL_ENGLISH_ONLY_MODELS
    assert supports_streaming("local", GRANITE_CTC_MODEL_SIZE) is False
    assert LOCAL_MODEL_RUNTIME[GRANITE_CTC_MODEL_SIZE] == "granite-ctc"
    assert (
        MODEL_REPO_MAP[GRANITE_CTC_MODEL_SIZE]
        == "qwertz92/granite-speech-5.0-470m-turboctc-onnx"
    )


def test_the_device_picker_does_not_claim_to_control_this_model():
    """CPU-only through ONNX Runtime: a device policy never reaches it."""
    from stt_app.config import DEVICE_AWARE_LOCAL_MODELS

    assert GRANITE_CTC_MODEL_SIZE not in DEVICE_AWARE_LOCAL_MODELS


# --------------------------------------------------------------------------
# Feature extractor
#
# The graph is fed by `GraniteSpeech5FeatureExtractor`, which lives in
# `transformers` and pulls torch and torchaudio with it. This module
# reimplements it in numpy, so the only thing that proves it right is a
# reference produced by the real one: `tests/data/granite_ctc_reference.npz`
# holds a 7,840-sample int16 waveform (49 mel frames -- an odd count, so the
# padding branch runs -- with a stretch of digital silence in it) and the
# 25 x 320 features transformers 5.16 / torch 2.11 / torchaudio 2.11 produced
# from it.
# --------------------------------------------------------------------------


def _reference_case() -> tuple[np.ndarray, np.ndarray]:
    with np.load(_REFERENCE) as data:
        pcm = np.asarray(data["pcm"], dtype="<i2")
        features = np.asarray(data["features"], dtype=np.float32)
    return pcm.astype(np.float32) / 32768.0, features


def test_features_match_the_real_transformers_processor():
    waveform, reference = _reference_case()

    features = extract_features(waveform)

    assert features.shape == reference.shape
    assert features.dtype == np.float32
    assert float(np.abs(features - reference).max()) < 1e-4


def test_the_blockwise_stft_is_the_single_pass_result(monkeypatch):
    """A long recording must not materialise one `frames x 512` matrix, so the
    STFT runs in blocks. The blocking is invisible only if it computes the same
    thing, which a block size no real input reaches cannot show.

    The same thing to within rounding, not to the bit. The mel projection is a
    BLAS product, and which kernel sums a row depends on the block's shape and
    on the CPU OpenBLAS picked its kernels for. Measured on 2026-09-19 over this
    fixture and twenty real clips with `OPENBLAS_CORETYPE` set to Zen, Haswell,
    SkylakeX, Sandybridge, Nehalem and Core2: never more than one float32 step
    (1.2e-7), and not zero under Zen, Haswell and Core2 -- so bit equality
    passed on the developer's machine and failed on a CI runner with another
    CPU. A block whose offsets restart, a block shifted by one frame and a
    dropped last block each move a feature by more than 1.4.
    """
    from stt_app.transcriber import local_granite_ctc

    waveform, _reference = _reference_case()
    single_pass = extract_features(waveform)

    monkeypatch.setattr(local_granite_ctc, "_STFT_BLOCK_FRAMES", 3)
    blocked = extract_features(waveform)

    assert blocked.shape == single_pass.shape
    assert float(np.abs(blocked - single_pass).max()) < 1e-6


@pytest.mark.parametrize(
    ("mel_frames", "expected_rows"),
    [
        (49, 25),  # odd: padded up to 50 mel frames, stacked in pairs
        (50, 25),  # even: no padding
        (1, 1),
        (2, 1),
    ],
)
def test_the_frame_count_is_padded_up_to_a_whole_stacked_pair(
    mel_frames, expected_rows
):
    features = extract_features(np.zeros(mel_frames * HOP_LENGTH, dtype=np.float32))

    assert features.shape == (expected_rows, FEATURE_SIZE)


def test_audio_shorter_than_one_mel_frame_yields_no_features():
    """Not an exception: `transcribe_batch` decides what a too-short recording
    returns, and it cannot do that if the extractor raises first."""
    assert extract_features(np.zeros(100, dtype=np.float32)).shape == (0, FEATURE_SIZE)


# --------------------------------------------------------------------------
# CTC greedy decoding
# --------------------------------------------------------------------------


def _logits_for(ids: list[int], vocabulary: int = 8) -> np.ndarray:
    logits = np.zeros((len(ids), vocabulary), dtype=np.float32)
    logits[np.arange(len(ids)), ids] = 1.0
    return logits


def test_greedy_decode_collapses_repeats_and_drops_blanks():
    # blank=0: "a a" stays two tokens because a blank separates them, while the
    # doubled 5 is one emission the frame rate spread over two frames.
    assert ctc_greedy_token_ids(_logits_for([0, 5, 5, 0, 5, 0, 7, 7])) == [5, 5, 7]


def test_greedy_decode_of_silence_is_empty():
    assert ctc_greedy_token_ids(_logits_for([0, 0, 0])) == []


# --------------------------------------------------------------------------
# Splitting a long recording into passes
# --------------------------------------------------------------------------


def test_a_recording_at_or_below_the_bound_is_one_pass():
    samples = np.zeros(16_000 * 4, dtype=np.float32)

    assert len(split_into_passes(samples, 16_000, max_seconds=4.0)) == 1


def test_every_window_stays_within_the_bound_and_the_windows_are_the_input():
    rng = np.random.default_rng(5)
    samples = rng.standard_normal(16_000 * 10).astype(np.float32)

    windows = split_into_passes(
        samples, 16_000, max_seconds=3.0, search_seconds=1.0
    )

    assert len(windows) > 1
    assert all(window.size <= 16_000 * 3 for window in windows)
    assert np.array_equal(np.concatenate(windows), samples)


def test_the_cut_lands_on_the_quietest_stretch_of_the_window():
    """Cutting mid-word would lose the word: the two passes share no audio, so
    the split point is the quietest 20 ms frame near the end of the window."""
    sample_rate = 16_000
    rng = np.random.default_rng(11)
    samples = rng.standard_normal(sample_rate * 6).astype(np.float32)
    silence = slice(int(sample_rate * 2.5), int(sample_rate * 2.9))
    samples[silence] = 0.0

    windows = split_into_passes(
        samples, sample_rate, max_seconds=3.0, search_seconds=1.0
    )

    assert silence.start <= windows[0].size <= silence.stop


# --------------------------------------------------------------------------
# The transcriber, against a fake InferenceSession
#
# The real graph is 551 MB, so these build the model folder the loader expects
# -- including the `preprocessor_config.json` it validates and a tiny
# byte-level BPE `tokenizer.json` -- and hand the class a session that returns
# prepared logits.
# --------------------------------------------------------------------------

_REAL_PREPROCESSOR_CONFIG = {
    "sample_rate": 16000,
    "n_fft": 512,
    "win_length": 400,
    "hop_length": 160,
    "n_mels": 80,
    "stack_factor": 2,
    "deltas": True,
    "delta_win_length": 3,
    "logmel_floor_db": 8.0,
}

# id 0 is the CTC blank; the leading "G-with-dot" is the byte-level BPE's space.
_TINY_VOCABULARY = {"<blank>": 0, "hello": 1, "Ġworld": 2, "Ġagain": 3}


def _write_tiny_tokenizer(path: Path) -> None:
    from tokenizers import Tokenizer, decoders, models

    tokenizer = Tokenizer(models.BPE(vocab=dict(_TINY_VOCABULARY), merges=[]))
    tokenizer.decoder = decoders.ByteLevel()
    tokenizer.save(str(path))


def _model_root(tmp_path: Path, preprocessor: dict | None = None) -> str:
    """A cached snapshot of the model, in the flat layout the download writes."""
    root = tmp_path / "models"
    folder = root / "granite-speech-5.0-470m-turboctc-onnx"
    (folder / "onnx").mkdir(parents=True, exist_ok=True)
    # Never opened: the test replaces `onnxruntime.InferenceSession`.
    (folder / "onnx" / "model_int8.onnx").write_bytes(b"")
    (folder / "config.json").write_text(
        json.dumps({"pad_token_id": 0}), encoding="utf-8"
    )
    (folder / "preprocessor_config.json").write_text(
        json.dumps(
            _REAL_PREPROCESSOR_CONFIG if preprocessor is None else preprocessor
        ),
        encoding="utf-8",
    )
    _write_tiny_tokenizer(folder / "tokenizer.json")
    return str(root)


class _FakeSession:
    """The graph's one-input/one-output session, with prepared logits.

    `token_ids` is one token sequence per call; the last one repeats, so a
    single sequence answers every window of a split recording.
    """

    def __init__(self, token_ids: list[list[int]] | None = None) -> None:
        self._token_ids = token_ids or [[1, 2]]
        self.features: list[np.ndarray] = []
        self.run_options: list[object] = []

    def run(self, _output_names, input_feed, run_options=None):
        self.features.append(np.asarray(input_feed["input_features"]))
        self.run_options.append(run_options)
        index = min(len(self.features) - 1, len(self._token_ids) - 1)
        return [_logits_for(self._token_ids[index], len(_TINY_VOCABULARY))[None]]


def _install_session(monkeypatch, session: object) -> None:
    import onnxruntime

    monkeypatch.setattr(
        onnxruntime, "InferenceSession", lambda *_args, **_kwargs: session
    )


def _transcriber(
    tmp_path,
    monkeypatch,
    *,
    session: object | None = None,
    language_mode: str = "auto",
    preprocessor: dict | None = None,
) -> tuple[LocalGraniteCtcTranscriber, object]:
    fake = _FakeSession() if session is None else session
    _install_session(monkeypatch, fake)
    transcriber = LocalGraniteCtcTranscriber(
        GRANITE_CTC_MODEL_SIZE,
        language_mode=language_mode,
        # Offline: no test may ever reach a download.
        offline_mode=True,
        model_dir=_model_root(tmp_path, preprocessor),
    )
    return transcriber, fake


def _wav_bytes(
    samples: np.ndarray,
    sample_rate: int = 16000,
    channels: int = 1,
) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.astype("<i2").tobytes())
    return buffer.getvalue()


def _speech_pcm(seconds: float, sample_rate: int = 16000) -> np.ndarray:
    count = int(seconds * sample_rate)
    tone = np.sin(np.arange(count) / 12.0) * 6000.0
    return tone.astype(np.int16)


def test_the_factory_routes_the_model_to_this_runtime(tmp_path):
    transcriber = create_transcriber(
        AppSettings(
            engine="local",
            model_size=GRANITE_CTC_MODEL_SIZE,
            model_dir=str(tmp_path),
            offline_mode=True,
        )
    )

    assert isinstance(transcriber, LocalGraniteCtcTranscriber)
    assert transcriber.runtime_device == "cpu"
    assert transcriber.gpu_available is False
    assert transcriber.runtime_status_text() == "ONNX Runtime active on CPU"


def test_a_wav_path_wav_bytes_and_raw_pcm_all_transcribe(tmp_path, monkeypatch):
    transcriber, fake = _transcriber(tmp_path, monkeypatch)
    pcm = _speech_pcm(1.0)
    wav_path = tmp_path / "clip.wav"
    wav_path.write_bytes(_wav_bytes(pcm))

    assert transcriber.transcribe_batch(str(wav_path)) == "hello world"
    assert transcriber.transcribe_batch(_wav_bytes(pcm)) == "hello world"
    assert transcriber.transcribe_batch(pcm.tobytes()) == "hello world"

    for features in fake.features:
        assert features.shape == (1, 50, FEATURE_SIZE)
        assert features.dtype == np.float32


def test_a_48_khz_wav_is_resampled_to_the_rate_the_graph_was_trained_on(
    tmp_path, monkeypatch
):
    transcriber, fake = _transcriber(tmp_path, monkeypatch)

    transcriber.transcribe_batch(_wav_bytes(_speech_pcm(1.0, 48_000), 48_000))

    # One second of audio is 100 mel frames and therefore 50 stacked frames,
    # whatever rate it arrived at.
    assert fake.features[0].shape == (1, 50, FEATURE_SIZE)


def test_a_wav_declaring_no_sample_rate_is_a_transcription_error(
    tmp_path, monkeypatch
):
    """This runtime resamples, so a header rate of 0 was a `ZeroDivisionError`
    out of `transcribe_batch` -- a raw exception where the controller expects
    a `TranscriptionError` it can show and keep the recording for."""
    transcriber, fake = _transcriber(tmp_path, monkeypatch)
    payload = bytearray(_wav_bytes(_speech_pcm(0.5)))
    payload[24:28] = (0).to_bytes(4, "little")

    with pytest.raises(TranscriptionError, match="sample rate"):
        transcriber.transcribe_batch(bytes(payload))

    assert fake.features == []


def test_a_stereo_wav_is_downmixed_to_mono(tmp_path, monkeypatch):
    transcriber, fake = _transcriber(tmp_path, monkeypatch)
    mono = _speech_pcm(1.0)
    interleaved = np.repeat(mono, 2)

    transcriber.transcribe_batch(_wav_bytes(interleaved, channels=2))

    assert fake.features[0].shape == (1, 50, FEATURE_SIZE)


def test_empty_and_very_short_audio_return_nothing_without_running_the_graph(
    tmp_path, monkeypatch
):
    """Fewer than four stacked frames produce no graph output at all. Too short
    to hold a word is an empty transcript, never an exception."""
    transcriber, fake = _transcriber(tmp_path, monkeypatch)

    assert transcriber.transcribe_batch(b"") == ""
    assert transcriber.transcribe_batch(_speech_pcm(0.05).tobytes()) == ""
    assert fake.features == []

    # 100 ms is five stacked frames and does reach the graph.
    assert transcriber.transcribe_batch(_speech_pcm(0.1).tobytes()) == "hello world"


def test_a_recording_past_the_bound_runs_several_passes_and_joins_them(
    tmp_path, monkeypatch
):
    """One pass allocates activations for the whole recording (2.4 GB at 563 s
    measured), so a long one is transcribed in consecutive windows."""
    transcriber, fake = _transcriber(
        tmp_path, monkeypatch, session=_FakeSession([[1], [2], [3]])
    )

    text = transcriber.transcribe_batch(_speech_pcm(190.0).tobytes())

    assert len(fake.features) == 2
    assert text == "hello world"
    # No window may exceed the bound the memory measurement set.
    assert all(features.shape[1] <= 180 * 50 for features in fake.features)


def test_an_empty_window_is_skipped_rather_than_joined_as_a_gap(
    tmp_path, monkeypatch
):
    transcriber, fake = _transcriber(
        tmp_path, monkeypatch, session=_FakeSession([[1], [0], [3]])
    )

    assert transcriber.transcribe_batch(_speech_pcm(190.0).tobytes()) == "hello"
    assert len(fake.features) == 2


def test_the_model_is_loaded_once_and_reloaded_after_close(tmp_path, monkeypatch):
    transcriber, fake = _transcriber(tmp_path, monkeypatch)
    loads: list[int] = []
    import onnxruntime

    monkeypatch.setattr(
        onnxruntime,
        "InferenceSession",
        lambda *_a, **_k: (loads.append(1), fake)[1],
    )

    transcriber.transcribe_batch(_speech_pcm(0.5).tobytes())
    transcriber.transcribe_batch(_speech_pcm(0.5).tobytes())
    assert loads == [1]

    transcriber.close()
    assert transcriber.transcribe_batch(_speech_pcm(0.5).tobytes()) == "hello world"
    assert loads == [1, 1]


def test_a_close_between_the_two_locks_stops_the_run_instead_of_using_it(
    tmp_path, monkeypatch
):
    """`transcribe_batch` takes its two locks sequentially: it releases the
    model lock before acquiring the inference lock, so a `close()` in that gap
    gets both uncontended and drops the session this run was about to use. A
    `TranscriptionError`, not a cancel: nobody asked to stop, so the recording
    must be kept for Retry. The progress callback fires in exactly that gap.
    """
    transcriber, _fake = _transcriber(tmp_path, monkeypatch)
    transcriber.preload_model()
    transcriber.set_progress_callback(lambda _message: transcriber.close())

    with pytest.raises(TranscriptionError, match="closed while this transcription"):
        transcriber.transcribe_batch(_speech_pcm(0.5).tobytes())


def test_the_preprocessor_config_is_checked_and_names_the_key_that_differs(
    tmp_path, monkeypatch
):
    """The extractor hard-codes the parameters. A re-export with others must
    fail loudly instead of feeding the graph features it cannot read."""
    changed = dict(_REAL_PREPROCESSOR_CONFIG, hop_length=320)
    transcriber, _fake = _transcriber(tmp_path, monkeypatch, preprocessor=changed)

    with pytest.raises(TranscriptionError) as excinfo:
        transcriber.preload_model()

    message = str(excinfo.value)
    assert "hop_length" in message
    assert "320" in message and "160" in message


def test_a_preprocessor_config_that_is_missing_a_key_is_refused(
    tmp_path, monkeypatch
):
    incomplete = {
        key: value
        for key, value in _REAL_PREPROCESSOR_CONFIG.items()
        if key != "n_mels"
    }
    transcriber, _fake = _transcriber(tmp_path, monkeypatch, preprocessor=incomplete)

    with pytest.raises(TranscriptionError, match="n_mels"):
        transcriber.preload_model()


def test_a_missing_model_in_offline_mode_says_so_instead_of_downloading(tmp_path):
    transcriber = LocalGraniteCtcTranscriber(
        GRANITE_CTC_MODEL_SIZE, offline_mode=True, model_dir=str(tmp_path)
    )

    with pytest.raises(TranscriptionError, match="not cached locally"):
        transcriber.preload_model()


def test_an_unknown_model_id_is_rejected():
    with pytest.raises(TranscriptionError):
        LocalGraniteCtcTranscriber("not-a-model")


def test_streaming_is_not_offered():
    with pytest.raises(NotImplementedError):
        LocalGraniteCtcTranscriber(GRANITE_CTC_MODEL_SIZE).start_stream()


def test_the_language_picker_offers_auto_and_english_only():
    assert language_modes_for_selection("local", GRANITE_CTC_MODEL_SIZE) == (
        "auto",
        "en",
    )


@pytest.mark.parametrize("mode", ["auto", "en"])
def test_a_supported_language_is_kept(mode):
    transcriber = LocalGraniteCtcTranscriber(GRANITE_CTC_MODEL_SIZE)
    transcriber.set_language_mode(mode)

    assert transcriber._language_mode == mode


def test_an_unsupported_language_falls_back_and_says_why(caplog):
    """The model is English-only and takes no language input at all, so a
    German selection would otherwise look like it had been applied."""
    with caplog.at_level(logging.WARNING):
        transcriber = LocalGraniteCtcTranscriber(
            GRANITE_CTC_MODEL_SIZE, language_mode="de"
        )

    assert transcriber._language_mode == "auto"
    assert any("English" in record.message for record in caplog.records)


# --- cancelling ----------------------------------------------------------


def test_a_cancel_before_the_load_never_builds_the_session(tmp_path, monkeypatch):
    transcriber, fake = _transcriber(tmp_path, monkeypatch)
    transcriber.set_cancel_check(lambda: True)

    with pytest.raises(TranscriptionCanceled):
        transcriber.transcribe_batch(_speech_pcm(0.5).tobytes())

    assert fake.features == []
    assert transcriber._session is None


def test_a_cancel_between_windows_stops_the_remaining_passes(tmp_path, monkeypatch):
    canceled = threading.Event()

    class _CancelAfterFirstWindow(_FakeSession):
        def run(self, output_names, input_feed, run_options=None):
            canceled.set()
            return super().run(output_names, input_feed, run_options)

    transcriber, fake = _transcriber(
        tmp_path, monkeypatch, session=_CancelAfterFirstWindow()
    )
    transcriber.set_cancel_check(canceled.is_set)

    with pytest.raises(TranscriptionCanceled):
        transcriber.transcribe_batch(_speech_pcm(190.0).tobytes())

    assert len(fake.features) == 1


def test_a_cancel_during_a_run_is_reported_as_a_cancel(tmp_path, monkeypatch):
    """ONNX Runtime reports an aborted run as a generic failure, so only the
    abort handle can tell a cancel from a broken graph."""

    class _AbortAwareSession(_FakeSession):
        def __init__(self, transcriber_holder):
            super().__init__()
            self._holder = transcriber_holder

        def run(self, _output_names, _input_feed, run_options=None):
            for _ in range(200):
                handle = self._holder[0]._abort_handle
                if handle is not None and handle.aborted:
                    raise RuntimeError("Exiting due to terminate flag")
                time.sleep(0.01)
            raise AssertionError("the run was never aborted")

    holder: list[LocalGraniteCtcTranscriber] = []
    transcriber, _fake = _transcriber(
        tmp_path, monkeypatch, session=_AbortAwareSession(holder)
    )
    holder.append(transcriber)
    canceled = threading.Event()
    transcriber.set_cancel_check(canceled.is_set)
    threading.Timer(0.05, canceled.set).start()

    with pytest.raises(TranscriptionCanceled):
        transcriber.transcribe_batch(_speech_pcm(0.5).tobytes())

    # The handle is per call, so the next transcription starts uncancelled.
    assert transcriber._abort_handle is None


def test_a_failure_without_an_abort_stays_a_failure(tmp_path, monkeypatch):
    class _BrokenSession(_FakeSession):
        def run(self, _output_names, _input_feed, run_options=None):
            raise RuntimeError("graph is corrupt")

    transcriber, _fake = _transcriber(
        tmp_path, monkeypatch, session=_BrokenSession()
    )
    transcriber.set_cancel_check(lambda: False)

    with pytest.raises(TranscriptionError, match="graph is corrupt"):
        transcriber.transcribe_batch(_speech_pcm(0.5).tobytes())


def test_the_run_carries_our_run_options_so_a_cancel_can_reach_it(
    tmp_path, monkeypatch
):
    """`RunOptions.terminate` is the only way to stop a call from another
    thread. A run started without our options is uncancellable, and nothing
    else in the transcript would show it."""
    transcriber, fake = _transcriber(tmp_path, monkeypatch)
    transcriber.set_cancel_check(lambda: False)

    transcriber.transcribe_batch(_speech_pcm(0.5).tobytes())

    assert fake.run_options[0] is not None
    assert fake.run_options[0].terminate is False
