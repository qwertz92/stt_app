"""Tests for the pure-Python onnx-asr local engine (Parakeet TDT, Canary)."""

from __future__ import annotations

import io
import threading
import time
import wave

import numpy as np
import pytest

from stt_app.config import (
    CANARY_MODEL_SIZE,
    LOCAL_BATCH_ONLY_MODELS,
    LOCAL_EXPLICIT_LANGUAGE_MODELS,
    LOCAL_ONNX_ASR_MODEL_SIZES,
    LOCAL_ONNX_MODEL_SIZES,
    MODEL_REPO_MAP,
    PARAKEET_MODEL_SIZE,
    language_modes_for_selection,
    supports_streaming,
)
from stt_app.settings_store import AppSettings
from stt_app.transcriber.base import TranscriptionCanceled, TranscriptionError
from stt_app.transcriber.factory import create_transcriber
from stt_app.transcriber.local_onnx_asr import (
    LocalOnnxAsrTranscriber,
    _CancelWatchdog,
    _RunAbortHandle,
)


def _wav_bytes(samples: np.ndarray, sample_rate: int = 16000, channels: int = 1):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.astype("<i2").tobytes())
    return buffer.getvalue()


class _FakeModel:
    def __init__(self):
        self.calls: list[dict] = []

    def recognize(self, waveform, **kwargs):
        self.calls.append({"waveform": waveform, **kwargs})
        return "  recognized text  "


def _transcriber_with_fake_model(model_size: str, language_mode: str = "auto"):
    transcriber = LocalOnnxAsrTranscriber(
        model_size=model_size, language_mode=language_mode
    )
    fake = _FakeModel()
    transcriber._model = fake
    return transcriber, fake


def test_both_models_are_registered_as_local_onnx_batch_only():
    for model_name in LOCAL_ONNX_ASR_MODEL_SIZES:
        assert model_name in LOCAL_ONNX_MODEL_SIZES
        assert model_name in LOCAL_BATCH_ONLY_MODELS
        assert model_name in MODEL_REPO_MAP
        assert supports_streaming("local", model_name) is False


def test_factory_routes_both_models_to_the_onnx_asr_runtime():
    for model_name in LOCAL_ONNX_ASR_MODEL_SIZES:
        transcriber = create_transcriber(
            AppSettings(engine="local", model_size=model_name, language_mode="de")
        )
        assert isinstance(transcriber, LocalOnnxAsrTranscriber)


def test_canary_can_never_select_auto():
    """onnx-asr hardcodes the <|en|> source/target token, so without an explicit
    language Canary *translates* German into English instead of transcribing."""
    assert CANARY_MODEL_SIZE in LOCAL_EXPLICIT_LANGUAGE_MODELS
    assert "auto" not in language_modes_for_selection("local", CANARY_MODEL_SIZE)

    transcriber = LocalOnnxAsrTranscriber(CANARY_MODEL_SIZE, language_mode="auto")
    assert transcriber._language_mode != "auto"

    transcriber.set_language_mode("auto")
    assert transcriber._language_mode != "auto"


def test_canary_sends_its_language_and_rejects_an_untrained_one():
    """An untrained ISO code raises KeyError deep inside onnx-asr, so it must be
    normalized away before the request."""
    transcriber, fake = _transcriber_with_fake_model(CANARY_MODEL_SIZE, "de")
    transcriber.transcribe_batch(_wav_bytes(np.zeros(1600, dtype=np.int16) + 100))
    assert fake.calls[0]["language"] == "de"

    transcriber.set_language_mode("zz")
    assert transcriber._language_mode in language_modes_for_selection(
        "local", CANARY_MODEL_SIZE
    )


def test_parakeet_sends_no_language_because_the_model_ignores_it():
    """Parakeet TDT v3 accepts `language=` but produces byte-identical output
    for any value, so passing one would only fake control."""
    assert language_modes_for_selection("local", PARAKEET_MODEL_SIZE) == ("auto",)

    transcriber, fake = _transcriber_with_fake_model(PARAKEET_MODEL_SIZE)
    transcriber.transcribe_batch(_wav_bytes(np.zeros(1600, dtype=np.int16) + 100))
    assert "language" not in fake.calls[0]


def test_transcribe_accepts_wav_bytes_raw_pcm_and_a_path(tmp_path):
    samples = (np.sin(np.arange(1600) / 10.0) * 3000).astype(np.int16)
    transcriber, fake = _transcriber_with_fake_model(PARAKEET_MODEL_SIZE)

    assert transcriber.transcribe_batch(_wav_bytes(samples)) == "recognized text"
    assert transcriber.transcribe_batch(samples.tobytes()) == "recognized text"
    wav_path = tmp_path / "clip.wav"
    wav_path.write_bytes(_wav_bytes(samples))
    assert transcriber.transcribe_batch(str(wav_path)) == "recognized text"

    for call in fake.calls:
        waveform = call["waveform"]
        assert waveform.dtype == np.float32
        assert waveform.size == samples.size
        # 16-bit PCM scaled into [-1, 1), not left as raw integers.
        assert np.abs(waveform).max() <= 1.0
        assert call["sample_rate"] == 16000


def test_stereo_wav_is_downmixed_to_mono():
    left = np.full(800, 1000, dtype=np.int16)
    right = np.full(800, 3000, dtype=np.int16)
    interleaved = np.empty(1600, dtype=np.int16)
    interleaved[0::2] = left
    interleaved[1::2] = right

    transcriber, fake = _transcriber_with_fake_model(PARAKEET_MODEL_SIZE)
    transcriber.transcribe_batch(_wav_bytes(interleaved, channels=2))

    waveform = fake.calls[0]["waveform"]
    assert waveform.size == 800
    assert waveform.max() == pytest.approx(2000 / 32768.0, rel=1e-3)


def test_empty_audio_returns_empty_without_calling_the_model():
    transcriber, fake = _transcriber_with_fake_model(PARAKEET_MODEL_SIZE)
    assert transcriber.transcribe_batch(b"") == ""
    assert fake.calls == []


def test_streaming_is_not_offered():
    transcriber = LocalOnnxAsrTranscriber(PARAKEET_MODEL_SIZE)
    with pytest.raises(NotImplementedError):
        transcriber.start_stream()


def test_unknown_model_is_rejected():
    with pytest.raises(TranscriptionError):
        LocalOnnxAsrTranscriber("not-a-model")


def test_offline_mode_reports_a_missing_model_instead_of_downloading(monkeypatch):
    transcriber = LocalOnnxAsrTranscriber(
        PARAKEET_MODEL_SIZE, offline_mode=True, model_dir=""
    )
    monkeypatch.setattr(
        "stt_app.transcriber.local_webgpu_asr.resolve_cached_webgpu_model_path",
        lambda *_args, **_kwargs: None,
    )
    with pytest.raises(TranscriptionError, match="not cached locally"):
        transcriber._resolve_model_path()


def test_bytes_and_path_wav_decoding_validate_identically(tmp_path):
    """The bytes branch was a copy of the path reader that had dropped the
    sample-width guard, so 24-bit audio was silently reinterpreted as 16-bit."""
    import struct

    def wav_with_width(width: int) -> bytes:
        frames = b"\x00" * (width * 100)
        header = b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVE"
        header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000,
                                        16000 * width, width, width * 8)
        header += b"data" + struct.pack("<I", len(frames))
        return header + frames

    transcriber, _fake = _transcriber_with_fake_model(PARAKEET_MODEL_SIZE)
    for width in (1, 3, 4):
        payload = wav_with_width(width)
        with pytest.raises(TranscriptionError, match="16-bit"):
            transcriber.transcribe_batch(payload)
        path = tmp_path / f"w{width}.wav"
        path.write_bytes(payload)
        with pytest.raises(TranscriptionError, match="16-bit"):
            transcriber.transcribe_batch(str(path))


def test_malformed_wav_surfaces_a_transcription_error(tmp_path):
    transcriber, _fake = _transcriber_with_fake_model(PARAKEET_MODEL_SIZE)
    with pytest.raises(TranscriptionError):
        transcriber.transcribe_batch(b"RIFF" + b"\x00" * 20)


def test_dropping_an_unsupported_language_is_logged(caplog):
    """A wrong language makes Canary translate, so the substitution must be
    diagnosable rather than silent."""
    import logging

    with caplog.at_level(logging.WARNING):
        transcriber = LocalOnnxAsrTranscriber(CANARY_MODEL_SIZE, language_mode="auto")

    assert transcriber._language_mode != "auto"
    assert any("translate" in record.message for record in caplog.records)


# --------------------------------------------------------------------------
# Cancelling a running transcription
#
# onnx-asr exposes no cancel hook: `recognize()` is one blocking call. Pressing
# Cancel therefore did nothing -- the run kept a core busy, held its model in
# memory, and blocked the single transcription worker behind it. The engine now
# routes every ONNX Runtime call through a RunOptions it can terminate.
# --------------------------------------------------------------------------


class _AbortAwareModel:
    """Stands in for a long ONNX run that ONNX Runtime aborts on terminate."""

    def __init__(self, transcriber, *, fail_message="Exiting due to terminate flag"):
        self._transcriber = transcriber
        self._fail_message = fail_message
        self.started = 0

    def recognize(self, _waveform, **_kwargs):
        self.started += 1
        for _ in range(200):
            handle = self._transcriber._abort_handle
            if handle is not None and handle.aborted:
                # ONNX Runtime reports the abort as a generic Fail.
                raise RuntimeError(self._fail_message)
            time.sleep(0.01)
        return "finished"


def test_a_cancel_before_the_run_starts_never_reaches_the_model():
    transcriber, fake = _transcriber_with_fake_model(PARAKEET_MODEL_SIZE)
    transcriber.set_cancel_check(lambda: True)
    with pytest.raises(TranscriptionCanceled):
        transcriber.transcribe_batch(_wav_bytes(np.zeros(1600, dtype=np.int16) + 100))
    assert fake.calls == []


def test_a_job_canceled_while_queued_does_not_load_the_model_first():
    """The check has to come before the load, not only before the run: a job
    canceled while it waited in the queue would otherwise still pull a
    multi-gigabyte model into memory just to throw the result away."""
    transcriber = LocalOnnxAsrTranscriber(PARAKEET_MODEL_SIZE)
    loads: list[int] = []

    def _never() -> object:
        loads.append(1)
        raise AssertionError("the model must not be loaded for a canceled job")

    transcriber._load_model = _never  # type: ignore[method-assign]
    transcriber.set_cancel_check(lambda: True)
    with pytest.raises(TranscriptionCanceled):
        transcriber.transcribe_batch(_wav_bytes(np.zeros(1600, dtype=np.int16) + 100))
    assert loads == []


def test_a_cancel_during_the_run_aborts_it_and_reports_a_cancel():
    transcriber = LocalOnnxAsrTranscriber(PARAKEET_MODEL_SIZE)
    model = _AbortAwareModel(transcriber)
    transcriber._model = model
    canceled = threading.Event()
    transcriber.set_cancel_check(canceled.is_set)
    threading.Timer(0.05, canceled.set).start()

    with pytest.raises(TranscriptionCanceled):
        transcriber.transcribe_batch(_wav_bytes(np.zeros(1600, dtype=np.int16) + 100))

    assert model.started == 1
    # The handle is per call, so the next transcription starts uncancelled.
    assert transcriber._abort_handle is None


def test_a_real_runtime_failure_is_not_relabelled_as_a_cancel():
    """Only an abort we asked for becomes TranscriptionCanceled; anything else
    must stay a failure the user is told about."""

    class _BrokenModel:
        def recognize(self, _waveform, **_kwargs):
            raise RuntimeError("graph is corrupt")

    transcriber = LocalOnnxAsrTranscriber(PARAKEET_MODEL_SIZE)
    transcriber._model = _BrokenModel()
    transcriber.set_cancel_check(lambda: False)
    with pytest.raises(TranscriptionError, match="graph is corrupt"):
        transcriber.transcribe_batch(_wav_bytes(np.zeros(1600, dtype=np.int16) + 100))


def test_a_raising_cancel_check_does_not_fail_the_transcription():
    def broken_check():
        raise ValueError("check exploded")

    transcriber, fake = _transcriber_with_fake_model(PARAKEET_MODEL_SIZE)
    transcriber.set_cancel_check(broken_check)
    assert (
        transcriber.transcribe_batch(_wav_bytes(np.zeros(1600, dtype=np.int16) + 100))
        == "recognized text"
    )
    assert len(fake.calls) == 1


class _StubSession:
    """Shaped like onnxruntime.InferenceSession for the hook installer."""

    def __init__(self):
        self.seen_run_options = []

    def run(self, _output_names, _input_feed, run_options=None):
        self.seen_run_options.append(run_options)
        return ["out"]


def _patch_session_type(monkeypatch):
    import onnxruntime

    monkeypatch.setattr(onnxruntime, "InferenceSession", _StubSession)


def test_every_session_of_the_loaded_model_is_wrapped(monkeypatch):
    """onnx-asr keeps its sessions two and three levels down; a shallower search
    would leave the resamplers -- and therefore part of the run -- uncancelable."""
    _patch_session_type(monkeypatch)

    class _Asr:
        def __init__(self):
            self._encoder = _StubSession()
            self._decoder = _StubSession()

    class _Resampler:
        def __init__(self):
            self._preprocessors = {16000: _StubSession(), 8000: _StubSession()}

    class _Model:
        def __init__(self):
            self.asr = _Asr()
            self.resampler = _Resampler()

    transcriber = LocalOnnxAsrTranscriber(PARAKEET_MODEL_SIZE)
    assert transcriber._install_cancel_hooks(_Model()) == 4


def test_a_model_without_sessions_is_reported_but_still_usable(monkeypatch, caplog):
    import logging

    _patch_session_type(monkeypatch)
    transcriber = LocalOnnxAsrTranscriber(PARAKEET_MODEL_SIZE)
    with caplog.at_level(logging.WARNING):
        assert transcriber._install_cancel_hooks(object()) == 0
    assert any("cannot be canceled" in record.message for record in caplog.records)


def test_a_wrapped_session_forwards_our_run_options_and_stops_between_calls():
    transcriber = LocalOnnxAsrTranscriber(PARAKEET_MODEL_SIZE)
    session = _StubSession()
    transcriber._wrap_session_run(session)

    # No transcription in flight: the caller's own options are left alone.
    session.run(["y"], {"x": 1})
    assert session.seen_run_options == [None]

    handle = _RunAbortHandle()
    transcriber._abort_handle = handle
    session.run(["y"], {"x": 1})
    assert session.seen_run_options[-1] is handle.options

    handle.abort()
    with pytest.raises(TranscriptionCanceled):
        session.run(["y"], {"x": 1})
    # The aborted call never reached the runtime.
    assert len(session.seen_run_options) == 2


def test_the_watchdog_trips_the_handle_and_stops_cleanly():
    handle = _RunAbortHandle()
    canceled = threading.Event()
    watchdog = _CancelWatchdog(handle, canceled.is_set)
    watchdog.start()
    try:
        assert handle.aborted is False
        canceled.set()
        deadline = time.monotonic() + 2.0
        while not handle.aborted and time.monotonic() < deadline:
            time.sleep(0.01)
        assert handle.aborted is True
        assert handle.options.terminate is True
    finally:
        watchdog.stop()


def test_the_watchdog_without_a_cancel_check_starts_no_thread():
    handle = _RunAbortHandle()
    watchdog = _CancelWatchdog(handle, None)
    watchdog.start()
    assert watchdog._thread is None
    watchdog.stop()
    assert handle.aborted is False
