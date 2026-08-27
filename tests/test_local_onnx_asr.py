"""Tests for the pure-Python onnx-asr local engine (Parakeet TDT, Canary)."""

from __future__ import annotations

import gc
import io
import threading
import time
import wave
import weakref

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


class _OnnxAsrShapedModel:
    """The onnx-asr 0.12 object graph, at the depths it really uses.

    Encoder/decoder sit two levels down, the resampler's preprocessors three,
    and the mel preprocessor **four** -- onnx-asr wraps it as
    ``ConcurrentPreprocessor.preprocessor -> OnnxPreprocessor._preprocessor``.
    A search that stopped at three would leave that one uncancelable.
    """

    class _Asr:
        def __init__(self):
            self._encoder = _StubSession()
            self._decoder_joint = _StubSession()
            self._preprocessor = _OnnxAsrShapedModel._ConcurrentPreprocessor()

    class _ConcurrentPreprocessor:
        def __init__(self):
            self.preprocessor = _OnnxAsrShapedModel._OnnxPreprocessor()

    class _OnnxPreprocessor:
        def __init__(self):
            self._preprocessor = _StubSession()

    class _Resampler:
        def __init__(self):
            self._preprocessors = {16000: _StubSession(), 8000: _StubSession()}

    def __init__(self):
        self.asr = _OnnxAsrShapedModel._Asr()
        self.resampler = _OnnxAsrShapedModel._Resampler()

    def sessions(self):
        return [
            self.asr._encoder,
            self.asr._decoder_joint,
            self.asr._preprocessor.preprocessor._preprocessor,
            *self.resampler._preprocessors.values(),
        ]


def test_every_session_of_the_loaded_model_is_wrapped(monkeypatch):
    """The deepest session onnx-asr uses is four levels down.

    A shallower search would leave the mel preprocessor -- and therefore part
    of every run -- uncancelable, while still reporting hooks as installed.
    """
    _patch_session_type(monkeypatch)
    model = _OnnxAsrShapedModel()

    transcriber = LocalOnnxAsrTranscriber(PARAKEET_MODEL_SIZE)

    assert transcriber._install_cancel_hooks(model) == 5
    for session in model.sessions():
        assert "run" in session.__dict__


def test_a_close_between_the_two_locks_stops_the_run_instead_of_unhooking_it():
    """`transcribe_batch` takes its two locks sequentially, not nested.

    It releases `_model_lock` after fetching the model and only then acquires
    `_inference_lock`, so a `close()` landing in that gap gets both
    uncontended and unwraps the very sessions this run is about to use. The
    watchdog would then set `terminate` on a `RunOptions` nobody passes and
    the transcription would finish in full with no log line -- the cancel
    silently off. The progress callback fires in exactly that gap, which is
    how this drives it deterministically.

    It raises a `TranscriptionError` and not a `TranscriptionCanceled`: a
    `close()` also runs for a settings save and for a resume-driven reset,
    and the controller renders a cancel as a bare "canceled" with no text and
    no Retry, which would present a runtime the user did not stop as one they
    did -- and drop the recording.
    """
    transcriber, _fake = _transcriber_with_fake_model(PARAKEET_MODEL_SIZE)
    transcriber.set_progress_callback(lambda _message: transcriber.close())

    with pytest.raises(TranscriptionError, match="closed while this transcription"):
        transcriber.transcribe_batch(_wav_bytes(np.zeros(1600, dtype=np.float32)))


def test_close_waits_for_a_run_in_flight_before_unwrapping(monkeypatch):
    """Unwrapping mid-run would silently switch that run back to plain `run`.

    The watchdog would keep setting `terminate` on a `RunOptions` object
    nobody passes any more, so the transcription would finish in full with no
    log line -- the cancel this class exists for, turned off. No caller
    reaches that today (every close path waits for the runtime lease first),
    but the class must not depend on its callers for that.
    """
    _patch_session_type(monkeypatch)
    transcriber = LocalOnnxAsrTranscriber(PARAKEET_MODEL_SIZE)
    transcriber._model = _OnnxAsrShapedModel()
    transcriber._install_cancel_hooks(transcriber._model)
    finished = threading.Event()

    transcriber._inference_lock.acquire()
    closer = threading.Thread(target=lambda: (transcriber.close(), finished.set()))
    closer.start()
    try:
        assert not finished.wait(0.3), "close() unwrapped during a run"
    finally:
        transcriber._inference_lock.release()
    closer.join(timeout=5.0)

    assert finished.is_set()
    assert transcriber._model is None
    assert transcriber._wrapped_sessions == []


def test_close_restores_every_session_so_the_model_can_be_freed(monkeypatch):
    """Wrapping ``run`` creates a reference cycle that outlives ``close()``.

    ``session.run = wrapper`` puts the wrapper in the session's own
    ``__dict__``, and the wrapper holds the original *bound* method, whose
    ``__self__`` is that session. With the cyclic collector switched off
    nothing below can be freed unless the wrapper is removed again -- which is
    what a user sees as "the model is still in memory after cancelling".
    """
    _patch_session_type(monkeypatch)
    transcriber = LocalOnnxAsrTranscriber(PARAKEET_MODEL_SIZE)
    model = _OnnxAsrShapedModel()
    transcriber._install_cancel_hooks(model)
    transcriber._model = model
    refs = [weakref.ref(session) for session in model.sessions()]
    refs.append(weakref.ref(model))

    gc.disable()
    try:
        del model
        transcriber.close()
        # Refcounting alone has to be enough here.
        assert [ref() for ref in refs] == [None] * len(refs)
    finally:
        gc.enable()
    assert transcriber._wrapped_sessions == []


def test_reloading_a_model_does_not_keep_the_previous_sessions_alive(monkeypatch):
    """A second load must release the first load's sessions.

    ``_wrapped_sessions`` is what ``close()`` walks, so a stale entry would
    both pin a discarded runtime and try to unwrap a session that is gone.
    """
    _patch_session_type(monkeypatch)
    transcriber = LocalOnnxAsrTranscriber(PARAKEET_MODEL_SIZE)
    first = _OnnxAsrShapedModel()
    transcriber._install_cancel_hooks(first)
    ref = weakref.ref(first.sessions()[0])

    second = _OnnxAsrShapedModel()
    transcriber._install_cancel_hooks(second)

    assert set(map(id, transcriber._wrapped_sessions)) == set(
        map(id, second.sessions())
    )
    gc.disable()
    try:
        del first
        assert ref() is None
    finally:
        gc.enable()


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


def test_onnx_runtime_still_offers_the_terminate_switch_we_rely_on():
    """The whole mid-run cancel rests on this one ONNX Runtime API.

    ``RunOptions.terminate`` is the only way to stop an ``InferenceSession``
    call from another thread. If an upgrade renames or removes it, the cancel
    silently stops working -- every unit test above uses a stub session and
    would still pass. It latches: ORT never clears the flag, which is why the
    handle is rebuilt per transcription instead of reused.
    """
    import onnxruntime as rt

    options = rt.RunOptions()
    assert options.terminate is False
    options.terminate = True
    assert options.terminate is True


def test_the_watchdog_without_a_cancel_check_starts_no_thread():
    handle = _RunAbortHandle()
    watchdog = _CancelWatchdog(handle, None)
    watchdog.start()
    assert watchdog._thread is None
    watchdog.stop()
    assert handle.aborted is False
