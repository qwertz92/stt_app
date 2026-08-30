import ast
import io
import logging
import math
import struct
import subprocess
import sys
import textwrap
import threading
import time
import types
import wave
from pathlib import Path

import pytest

from stt_app.transcriber import local_faster_whisper
from stt_app.transcriber.base import TranscriptionCanceled, TranscriptionError
from stt_app.transcriber.local_faster_whisper import LocalFasterWhisperTranscriber


class Segment:
    def __init__(self, text):
        self.text = text


class FakeModel:
    def __init__(self):
        self.calls = []
        self.next_text = "hello world"

    def transcribe(
        self, audio_source, language=None, vad_filter=True, initial_prompt=None
    ):
        self.calls.append(
            {
                "audio_source": audio_source,
                "language": language,
                "vad_filter": vad_filter,
                "initial_prompt": initial_prompt,
            }
        )
        words = self.next_text.split(" ")
        return [Segment(word) for word in words], {"language": "en"}


class ExplodingModel:
    def transcribe(
        self, audio_source, language=None, vad_filter=True, initial_prompt=None
    ):
        raise RuntimeError("model failed")


class MissingDependencyModel:
    def transcribe(
        self, audio_source, language=None, vad_filter=True, initial_prompt=None
    ):
        exc = ModuleNotFoundError("No module named 'requests'")
        exc.name = "requests"
        raise exc


def _build_wav_bytes(sample_rate=16000):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * 160)
    return buffer.getvalue()


def _build_pcm16_chunk(sample_count=320):
    """Audible 16-bit PCM.

    The streaming path gates each window on audio energy -- faster-whisper
    invents words from silence -- so a near-zero waveform is (correctly)
    skipped, and these streaming tests would then assert against a path
    that never runs.
    """
    import math
    import struct

    return b"".join(
        struct.pack("<h", int(6000 * math.sin(index / 8.0)))
        for index in range(sample_count)
    )


def test_local_transcriber_transcribe_batch_from_bytes():
    model = FakeModel()
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=lambda *args, **kwargs: model,
    )

    text = transcriber.transcribe_batch(_build_wav_bytes())

    assert text == "hello world"
    assert len(model.calls) == 1
    assert model.calls[0]["language"] is None


class _GeneratorModel:
    """Yields segments lazily so a cancel can stop decoding between segments."""

    def __init__(self):
        self.yielded = []

    def transcribe(
        self, audio_source, language=None, vad_filter=True, initial_prompt=None
    ):
        def gen():
            for word in ("one", "two", "three"):
                self.yielded.append(word)
                yield Segment(word)

        return gen(), {"language": "en"}


def test_transcribe_batch_aborts_between_segments_on_cancel():
    model = _GeneratorModel()
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=lambda *args, **kwargs: model,
    )
    # Keyed on decoding progress, not on a call count, so the cancel lands
    # between segments wherever the earlier checks happen to fall. Note this
    # test therefore says nothing about the check *before* the model load --
    # `test_a_cancel_before_the_run_does_not_load_the_model` covers that, and
    # keying this one on progress is exactly what stopped it covering both.
    def cancel_check():
        return bool(model.yielded)

    transcriber.set_cancel_check(cancel_check)

    with pytest.raises(TranscriptionCanceled):
        transcriber.transcribe_batch(_build_wav_bytes())

    # Stopped early: it did not consume all three segments.
    assert model.yielded == ["one"]


def test_a_cancel_before_the_run_does_not_load_the_model():
    """The check has to sit above `_ensure_model`, not only above the decode.

    A job cancelled while it waited in the single-worker queue would otherwise
    still pull a multi-gigabyte model into memory to throw the result away --
    and, worse, hold the shared runtime lease while doing it. Keying the
    between-segments test on decoding progress left this uncovered: deleting
    the pre-load check passes every other test in the suite.
    """
    loaded: list[str] = []

    def _factory(*_args, **_kwargs):
        loaded.append("model")
        return FakeModel()

    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=_factory,
    )
    transcriber.set_cancel_check(lambda: True)

    with pytest.raises(TranscriptionCanceled):
        transcriber.transcribe_batch(_build_wav_bytes())

    assert loaded == [], (
        "the model was loaded for a job that had already been cancelled"
    )


def test_transcribe_batch_completes_when_cancel_check_stays_false():
    model = FakeModel()
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=lambda *args, **kwargs: model,
    )
    transcriber.set_cancel_check(lambda: False)

    assert transcriber.transcribe_batch(_build_wav_bytes()) == "hello world"


def test_local_transcriber_sets_language_when_explicit():
    model = FakeModel()
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="de",
        model_factory=lambda *args, **kwargs: model,
    )

    transcriber.transcribe_batch(_build_wav_bytes())

    assert model.calls[0]["language"] == "de"


def test_local_transcriber_rejects_non_whisper_language_hint():
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="ast",
    )

    assert transcriber._language_arg() is None


def test_local_transcriber_wraps_model_errors():
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=lambda *args, **kwargs: ExplodingModel(),
    )

    with pytest.raises(TranscriptionError):
        transcriber.transcribe_batch(_build_wav_bytes())


def test_local_transcriber_missing_dependency_message_contains_fix_hint():
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=lambda *args, **kwargs: MissingDependencyModel(),
    )

    with pytest.raises(TranscriptionError) as error:
        transcriber.transcribe_batch(_build_wav_bytes())

    message = str(error.value)
    assert "requests" in message
    assert "uv sync --group dev" in message


def test_local_transcriber_reuses_model_instance_between_calls():
    model = FakeModel()
    create_calls = {"count": 0}

    def factory(*args, **kwargs):
        create_calls["count"] += 1
        return model

    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=factory,
    )

    transcriber.transcribe_batch(_build_wav_bytes())
    transcriber.transcribe_batch(_build_wav_bytes())

    assert create_calls["count"] == 1


def test_local_transcriber_streaming_roundtrip_with_partial_callback():
    model = FakeModel()
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=0.0,
        stream_partial_min_audio_s=0.0,
        model_factory=lambda *args, **kwargs: model,
    )
    partials = []

    transcriber.start_stream(on_partial=partials.append)
    transcriber.push_audio_chunk(_build_pcm16_chunk())
    transcriber.push_audio_chunk(_build_pcm16_chunk())
    text = transcriber.stop_stream()

    assert text == "hello world"
    assert partials


def test_local_transcriber_streaming_requires_active_session():
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        model_factory=lambda *args, **kwargs: FakeModel(),
    )

    with pytest.raises(TranscriptionError):
        transcriber.push_audio_chunk(b"abc")

    with pytest.raises(TranscriptionError):
        transcriber.stop_stream()


def test_local_transcriber_streaming_cannot_start_twice():
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        model_factory=lambda *args, **kwargs: FakeModel(),
    )
    transcriber.start_stream()

    with pytest.raises(TranscriptionError):
        transcriber.start_stream()

    transcriber.stop_stream()


def test_local_transcriber_abort_stream_ends_session_without_error():
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=10.0,
        stream_partial_min_audio_s=10.0,
        model_factory=lambda *args, **kwargs: FakeModel(),
    )
    transcriber.start_stream()
    transcriber.push_audio_chunk(_build_pcm16_chunk())

    transcriber.abort_stream()

    with pytest.raises(TranscriptionError):
        transcriber.stop_stream()


def test_timed_out_stale_stream_worker_cannot_mutate_next_session(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    first_partials = []
    second_partials = []
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=0.0,
        stream_partial_min_audio_s=0.0,
        model_factory=lambda *args, **kwargs: FakeModel(),
    )

    def fake_transcribe(max_window_seconds=None, *, session=None):
        assert session is not None
        if session.generation == 1:
            entered.set()
            assert release.wait(timeout=2)
            return "retired text"
        return "current text"

    monkeypatch.setattr(
        transcriber,
        "_transcribe_current_stream_buffer",
        fake_transcribe,
    )
    monkeypatch.setattr(local_faster_whisper, "STREAMING_ABORT_JOIN_TIMEOUT_S", 0.01)

    transcriber.start_stream(on_partial=first_partials.append)
    with transcriber._stream_lock:
        retired_thread = transcriber._stream_thread
    assert retired_thread is not None
    transcriber.push_audio_chunk(_build_pcm16_chunk())
    assert entered.wait(timeout=1)
    transcriber.abort_stream()

    transcriber.start_stream(on_partial=second_partials.append)
    transcriber.push_audio_chunk(_build_pcm16_chunk())
    assert transcriber.stop_stream() == "current text"

    release.set()
    retired_thread.join(timeout=1)
    assert retired_thread.is_alive() is False
    assert first_partials == []
    assert second_partials == ["current text"]


def test_local_transcriber_streaming_reports_runtime_error_immediately():
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=0.0,
        stream_partial_min_audio_s=0.0,
        model_factory=lambda *args, **kwargs: ExplodingModel(),
    )
    errors = []

    transcriber.start_stream(on_partial=lambda _text: None, on_error=errors.append)
    with transcriber._stream_lock:
        transcriber._stream_pcm_buffer.extend(_build_pcm16_chunk())
        transcriber._stream_last_partial_at = 0.0
        transcriber._stream_last_partial_size = 0
    transcriber._maybe_emit_partial()

    assert errors
    assert errors[0].startswith("Local streaming failed:")

    with pytest.raises(TranscriptionError, match="Local streaming failed"):
        transcriber.stop_stream()


def test_a_failing_partial_callback_is_logged_once_and_keeps_the_stream_alive(caplog):
    """A dead live-insertion path must be visible, and must not flood.

    This callback is what puts live text on screen and into the document, so
    swallowing its failure silently makes a broken delivery path
    indistinguishable from a user who simply stopped talking. It runs roughly
    every 350 ms, though, so logging per call would bury the rest of the log
    -- hence once per session, the same shape as `noise_floor_warned`.

    The transcription itself must survive: the next partial carries the whole
    merged text again, so one lost delivery costs nothing.
    """
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=0.0,
        stream_partial_min_audio_s=0.0,
        model_factory=lambda *args, **kwargs: FakeModel(),
    )
    errors = []

    def explode(_text):
        raise RuntimeError("the overlay is gone")

    transcriber.start_stream(on_partial=explode, on_error=errors.append)
    logger_name = "stt_app.transcriber.local_faster_whisper"
    with caplog.at_level(logging.ERROR, logger=logger_name):
        for _ in range(4):
            with transcriber._stream_lock:
                transcriber._stream_pcm_buffer.extend(_build_pcm16_chunk())
                transcriber._stream_last_partial_at = 0.0
                transcriber._stream_last_partial_size = 0
            transcriber._maybe_emit_partial()

    session = transcriber._stream_session
    assert session is not None
    assert session.result.merged_text, "the failing callback stopped the decode"
    assert errors == [], f"a failed delivery was escalated to the user: {errors}"

    tracebacks = [
        record
        for record in caplog.records
        if record.exc_info and record.name == logger_name
    ]
    assert len(tracebacks) == 1, (
        f"expected exactly one logged traceback, got {len(tracebacks)}"
    )
    assert "live text is not being delivered" in tracebacks[0].getMessage()

    assert transcriber.stop_stream()


def test_stream_partial_uses_configured_window(monkeypatch):
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=0.0,
        stream_partial_min_audio_s=0.0,
        stream_partial_window_s=2.5,
        model_factory=lambda *args, **kwargs: FakeModel(),
    )
    calls = []

    def fake_transcribe(max_window_seconds=None, *, session=None):
        calls.append(max_window_seconds)
        return "partial text"

    monkeypatch.setattr(
        transcriber,
        "_transcribe_current_stream_buffer",
        fake_transcribe,
    )

    transcriber.start_stream(on_partial=lambda _text: None)
    transcriber.push_audio_chunk(_build_pcm16_chunk(1_600))
    transcriber.stop_stream()

    assert 2.5 in calls
    assert None in calls


def test_local_streaming_fast_finalize_merges_live_text(monkeypatch):
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=0.0,
        stream_partial_min_audio_s=0.0,
        stream_partial_window_s=2.5,
        stream_final_full_pass=False,
        model_factory=lambda *args, **kwargs: FakeModel(),
    )
    calls = []
    responses = iter(["hello there my", "there my friend"])

    def fake_transcribe(max_window_seconds=None, *, session=None):
        calls.append(max_window_seconds)
        return next(responses)

    monkeypatch.setattr(
        transcriber,
        "_transcribe_current_stream_buffer",
        fake_transcribe,
    )

    transcriber.start_stream(on_partial=lambda _text: None)
    transcriber.push_audio_chunk(_build_pcm16_chunk(1_600))
    text = transcriber.stop_stream()

    # The trailing window is merged into the accumulated live text by word
    # overlap instead of re-transcribing the whole recording.
    assert text == "hello there my friend"
    assert calls == [2.5, 2.5]


def test_local_streaming_fast_finalize_without_partials_uses_tail(monkeypatch):
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=10.0,
        stream_partial_min_audio_s=10.0,
        stream_partial_window_s=2.5,
        stream_final_full_pass=False,
        model_factory=lambda *args, **kwargs: FakeModel(),
    )

    def fake_transcribe(max_window_seconds=None, *, session=None):
        return "short note"

    monkeypatch.setattr(
        transcriber,
        "_transcribe_current_stream_buffer",
        fake_transcribe,
    )

    transcriber.start_stream(on_partial=lambda _text: None)
    transcriber.push_audio_chunk(_build_pcm16_chunk())
    text = transcriber.stop_stream()

    assert text == "short note"


def test_transcribe_current_stream_buffer_trims_to_window_size(monkeypatch):
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_sample_rate=16_000,
        model_factory=lambda *args, **kwargs: FakeModel(),
    )
    transcriber._stream_pcm_buffer = bytearray(
        _build_pcm16_chunk(sample_count=16_000 * 6)
    )
    observed = {"seconds": 0.0}

    def fake_batch(wav_bytes):
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            observed["seconds"] = wav_file.getnframes() / float(wav_file.getframerate())
        return "ok"

    monkeypatch.setattr(transcriber, "transcribe_batch", fake_batch)

    text = transcriber._transcribe_current_stream_buffer(max_window_seconds=2.0)

    assert text == "ok"
    assert observed["seconds"] == pytest.approx(2.0, rel=0.03)


class HubOfflineModel:
    def transcribe(
        self, audio_source, language=None, vad_filter=True, initial_prompt=None
    ):
        raise OSError(
            "An error happened while trying to locate the files on the Hub "
            "and we cannot find the appropriate snapshot folder for the "
            "specified revision on the local disk. Please check your internet "
            "connection and try again."
        )


def test_local_transcriber_hub_offline_message_is_actionable():
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=lambda *args, **kwargs: HubOfflineModel(),
    )

    with pytest.raises(TranscriptionError) as error:
        transcriber.transcribe_batch(_build_wav_bytes())

    message = str(error.value)
    assert "not cached locally" in message
    assert "Offline mode" in message
    assert "restricted" in message.lower()


def test_offline_mode_passes_local_files_only():
    """offline_mode=True must pass local_files_only=True to WhisperModel."""
    captured_kwargs = {}

    def capturing_factory(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return FakeModel()

    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=capturing_factory,
        offline_mode=True,
    )
    transcriber.transcribe_batch(_build_wav_bytes())

    assert captured_kwargs.get("local_files_only") is True


def test_model_dir_passes_download_root():
    """model_dir must be forwarded as download_root to WhisperModel."""
    captured_kwargs = {}

    def capturing_factory(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return FakeModel()

    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=capturing_factory,
        model_dir="/tmp/my-models",
    )
    transcriber.transcribe_batch(_build_wav_bytes())

    assert captured_kwargs.get("download_root") == "/tmp/my-models"


def test_default_model_dir_omits_download_root():
    """When model_dir is empty, download_root should not be passed."""
    captured_kwargs = {}

    def capturing_factory(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return FakeModel()

    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=capturing_factory,
        model_dir="",
    )
    transcriber.transcribe_batch(_build_wav_bytes())

    assert "download_root" not in captured_kwargs


def test_custom_vocabulary_passes_initial_prompt_to_batch_transcribe():
    model = FakeModel()
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=lambda *args, **kwargs: model,
        custom_vocabulary="Kubernetes, Splunk SOAR",
    )

    transcriber.transcribe_batch(_build_wav_bytes())

    assert model.calls[0]["initial_prompt"] == "Kubernetes, Splunk SOAR"


def test_custom_vocabulary_passes_initial_prompt_to_streaming_transcribe():
    model = FakeModel()
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=0.0,
        stream_partial_min_audio_s=0.0,
        model_factory=lambda *args, **kwargs: model,
        custom_vocabulary="Kubernetes, Splunk SOAR",
    )

    transcriber.start_stream(on_partial=lambda _text: None)
    transcriber.push_audio_chunk(_build_pcm16_chunk())
    transcriber.stop_stream()

    assert model.calls
    assert all(
        call["initial_prompt"] == "Kubernetes, Splunk SOAR" for call in model.calls
    )


def test_empty_custom_vocabulary_omits_initial_prompt():
    model = FakeModel()
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        language_mode="auto",
        model_factory=lambda *args, **kwargs: model,
        custom_vocabulary="",
    )

    transcriber.transcribe_batch(_build_wav_bytes())

    assert model.calls[0]["initial_prompt"] is None


def test_streaming_silence_after_speech_cannot_overwrite_the_transcript():
    """A dictation that ends in silence must keep its text.

    faster-whisper invents words from silence, and an invented window can never
    be aligned against the accumulated text, so the merge replaced everything
    with it. This drives the real worker: audible speech, then more than one
    window's worth of silence that the model "transcribes" anyway. Both the
    partial path and the finalizer must refuse to decode that silence.
    """
    lock = threading.Lock()
    decoded = []

    class _CountingModel:
        def transcribe(self, *args, **kwargs):
            with lock:
                text = "real speech here" if not decoded else "hallucinated subtitle"
                decoded.append(text)
            segment = types.SimpleNamespace(text=text)
            info = types.SimpleNamespace(language="en", language_probability=1.0)
            return [segment], info

    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=0.0,
        stream_partial_min_audio_s=0.0,
        stream_final_full_pass=False,
        model_factory=lambda *args, **kwargs: _CountingModel(),
    )
    def _wait_for(predicate, what):
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError(f"timed out waiting for {what}")

    transcriber.start_stream(on_partial=lambda text: None)
    # The worker thread owns the buffer, so wait for it to drain the queue
    # rather than racing it.
    transcriber.push_audio_chunk(_build_pcm16_chunk(1600))
    _wait_for(lambda: bool(decoded), "the speech window to be decoded")
    with lock:
        decodes_after_speech = len(decoded)

    # Longer than stream_partial_window_s, so the finalizer's trailing window
    # holds nothing but this silence.
    silent = b"".join(
        struct.pack("<h", int(20 * math.sin(index / 8.0))) for index in range(1600)
    )
    silent_chunks = int(transcriber.stream_partial_window_s * 10) + 40
    for _ in range(silent_chunks):
        transcriber.push_audio_chunk(silent)
    expected_bytes = len(_build_pcm16_chunk(1600)) + silent_chunks * len(silent)
    _wait_for(
        lambda: transcriber._stream_session is not None
        and len(transcriber._stream_session.pcm_buffer) >= expected_bytes,
        "the silence to reach the worker",
    )

    final_text = transcriber.stop_stream().strip()

    with lock:
        assert len(decoded) == decodes_after_speech, "silence must never be decoded"
    assert final_text == "real speech here"


def test_streaming_partial_callback_receives_the_merged_transcript():
    """The callback must carry the whole transcript, not the latest window.

    The controller's locked-prefix insertion compares against what it already
    pasted; a raw window does not contain that text, so live insertion froze for
    the rest of the session once the window rolled past it.
    """
    # The second window overlaps the first but does not contain it: only the
    # merge produces the full text, so emitting the raw window is detectable.
    windows = iter(["first window text", "window text plus more"])
    seen = []

    class _WindowModel:
        def transcribe(self, *args, **kwargs):
            segment = types.SimpleNamespace(text=next(windows, "plus more"))
            info = types.SimpleNamespace(language="en", language_probability=1.0)
            return [segment], info

    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=0.0,
        stream_partial_min_audio_s=0.0,
        stream_final_full_pass=False,
        model_factory=lambda *args, **kwargs: _WindowModel(),
    )
    transcriber.start_stream(on_partial=seen.append)
    for _ in range(2):
        transcriber.push_audio_chunk(_build_pcm16_chunk(1600))
        transcriber._maybe_emit_partial()
    transcriber.stop_stream()

    assert seen[-1] == "first window text plus more"
    assert all(text.startswith("first window text") for text in seen)
    assert "window text plus more" not in seen, "raw window leaked to the callback"


def _tone(sample_count, amplitude):
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(index / 8.0)))
        for index in range(sample_count)
    )


def test_a_transient_after_a_long_pause_cannot_replace_or_extend_the_transcript():
    """A click ending a long pause must not become an invented sentence.

    A window following a pause longer than the window itself cannot be aligned,
    so it is either appended on trust or it replaces everything. Both are
    catastrophic when the window holds nothing but a keyboard click and the
    model's invention: appending grew the transcript without bound and pasted
    the junk live, replacing wiped the real dictation. A peak measurement does
    not separate the cases -- a 5 ms transient clears it -- so the decision uses
    how much *speech* the decoded window holds.
    """
    decoded = []

    class _HallucinatingModel:
        def transcribe(self, *args, **kwargs):
            text = "hello world" if not decoded else "Thank you for watching."
            decoded.append(text)
            segment = types.SimpleNamespace(text=text)
            info = types.SimpleNamespace(language="en", language_probability=1.0)
            return [segment], info

    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=0.0,
        stream_partial_min_audio_s=0.0,
        stream_final_full_pass=False,
        model_factory=lambda *args, **kwargs: _HallucinatingModel(),
    )
    speech = _tone(1600, 6000)
    quiet = _tone(1600, 20)
    click = _tone(80, 9000) + _tone(1520, 20)

    transcriber.start_stream(on_partial=lambda text: None)
    transcriber.push_audio_chunk(speech)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not decoded:
        time.sleep(0.01)
    assert decoded == ["hello world"]

    quiet_chunks = int(transcriber.stream_partial_window_s * 10) + 20
    for _ in range(3):
        for _ in range(quiet_chunks):
            transcriber.push_audio_chunk(quiet)
        transcriber.push_audio_chunk(click)
        time.sleep(0.05)
        transcriber._maybe_emit_partial()
        assert transcriber._stream_session.result.merged_text == "hello world"

    assert transcriber.stop_stream().strip() == "hello world"
    assert decoded == ["hello world"], "a transient must not be decoded"

def _ms(milliseconds, amplitude, sample_rate=16000):
    return _tone(int(sample_rate * milliseconds / 1000), amplitude)


@pytest.mark.parametrize(
    ("label", "tail", "must_append"),
    [
        # 200 ms is comfortably above the post-pause cut. The cut does NOT
        # sit above every desk transient -- a key clack measures exactly it
        # and a knuckle knock more -- see the table in config.py.
        ("a short spoken word", _ms(200, 6000), True),
        ("a keyboard click", _ms(5, 9000) + _ms(95, 20), False),
    ],
    ids=["short-word", "click"],
)
def test_only_real_speech_after_a_pause_extends_the_transcript(
    label, tail, must_append
):
    """The post-pause gate has to cut between a transient and a short word.

    Both directions are transcript loss. Too permissive and a keyboard click
    appends an invented sentence that is pasted into the document; too
    strict and a short answer after a pause -- "Ja.", "Stop." -- is deleted
    with no error and no log above DEBUG. An earlier threshold of 0.35 s did
    exactly that: measured, a 150 ms word reports 0.20 s and was rejected.
    """
    outputs = iter(["hello world"] + [f"after{index}" for index in range(300)])

    class _Model:
        def transcribe(self, *args, **kwargs):
            segment = types.SimpleNamespace(text=next(outputs, "x"))
            info = types.SimpleNamespace(language="en", language_probability=1.0)
            return [segment], info

    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=0.0,
        stream_partial_min_audio_s=0.0,
        stream_final_full_pass=False,
        model_factory=lambda *args, **kwargs: _Model(),
    )
    transcriber.start_stream(on_partial=lambda text: None)
    transcriber.push_audio_chunk(_ms(100, 6000))
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not (
        transcriber._stream_session
        and transcriber._stream_session.result.merged_text
    ):
        time.sleep(0.01)
    assert transcriber._stream_session.result.merged_text == "hello world"

    quiet_chunks = int(transcriber.stream_partial_window_s * 10) + 25
    for _ in range(quiet_chunks):
        transcriber.push_audio_chunk(_ms(100, 20))
    time.sleep(0.15)
    transcriber.push_audio_chunk(tail)
    time.sleep(0.2)
    transcriber._maybe_emit_partial()

    merged = transcriber._stream_session.result.merged_text
    transcriber.stop_stream()
    if must_append:
        assert merged.startswith("hello world") and merged != "hello world", (
            f"{label} after a pause was dropped from the transcript: {merged!r}"
        )
    else:
        assert merged == "hello world", (
            f"{label} after a pause changed the transcript: {merged!r}"
        )


def test_the_post_pause_gate_uses_the_fine_bucket_end_to_end():
    """Wiring guard: the bucket size must reach the real decision.

    tests/test_vad.py passes window_ms explicitly, so it never exercises the
    call in _stream_window_has_speech. Removing that argument leaves the meter
    on the batch gate's 100 ms buckets, where typing at 120 wpm measures a 1.5 s
    "speech" run and authorises appending a hallucinated window.
    """
    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=0.0,
        stream_partial_min_audio_s=0.0,
        stream_final_full_pass=False,
        model_factory=lambda *args, **kwargs: None,
    )

    class _Session:
        def __init__(self, pcm):
            self.pcm_buffer = bytearray(pcm)

    def typing(count, gap_ms):
        audio = _ms(200, 20)
        for _ in range(count):
            audio += _ms(5, 9000) + _ms(gap_ms, 20)
        return audio

    assert transcriber._stream_window_has_speech(_Session(typing(15, 100))) is False, (
        "typing at 120 wpm was accepted as speech; the fine bucket is not wired"
    )
    assert transcriber._stream_window_has_speech(_Session(typing(12, 150))) is False
    spoken = _ms(200, 20) + _ms(200, 6000) + _ms(200, 20)
    assert transcriber._stream_window_has_speech(_Session(spoken)) is True, (
        "a real 200 ms word was rejected"
    )


def test_the_segment_floor_is_wired_into_the_real_stream_worker():
    """Wiring guard: the bound only helps if the transcriber actually sets it.

    tests/test_streaming_text.py calls the merge directly, so deleting the
    three wiring lines in this module leaves the whole suite green -- the same
    gap that let round 4's critical bug ship.
    """
    outputs = iter(
        ["erster teil der nachricht", "erfundener einschub", "voellig anderes fenster"]
    )

    class _Model:
        def transcribe(self, *args, **kwargs):
            segment = types.SimpleNamespace(text=next(outputs, "x"))
            info = types.SimpleNamespace(language="de", language_probability=1.0)
            return [segment], info

    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=0.0,
        stream_partial_min_audio_s=0.0,
        stream_final_full_pass=False,
        model_factory=lambda *args, **kwargs: _Model(),
    )
    transcriber.start_stream(on_partial=lambda text: None)
    transcriber.push_audio_chunk(_ms(300, 6000))
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not (
        transcriber._stream_session
        and transcriber._stream_session.result.merged_text
    ):
        time.sleep(0.01)
    spoken = transcriber._stream_session.result.merged_text
    assert spoken == "erster teil der nachricht"

    # A pause longer than the window, then speech again: the text before the
    # pause becomes the floor.
    quiet_chunks = int(transcriber.stream_partial_window_s * 10) + 25
    for _ in range(quiet_chunks):
        transcriber.push_audio_chunk(_ms(100, 20))
    time.sleep(0.15)
    transcriber.push_audio_chunk(_ms(300, 6000))
    time.sleep(0.2)
    transcriber._maybe_emit_partial()

    assert transcriber._stream_session.result.segment_floor == spoken, (
        "the pause did not close off the earlier text; a later unalignable "
        "window can still destroy the whole dictation"
    )
    assert transcriber._stream_session.result.merged_text.startswith(spoken)
    transcriber.stop_stream()


@pytest.mark.parametrize(
    ("label", "amplitudes", "expected"),
    [
        ("a room above the gate throughout", [400] * 60, True),
        # The case a latching flag could never report: the session starts
        # quiet -- as every session does, between the hotkey and the first
        # word -- and the fan comes on afterwards.
        ("a fan that starts mid-dictation", [20] * 10 + [400] * 60, True),
        ("an ordinary quiet room", [20, 6000] * 30, False),
    ],
)
def test_a_noise_floor_above_the_gate_is_reported(
    monkeypatch, label, amplitudes, expected
):
    """A room louder than the gate disables the pause machinery silently.

    Everything keys off silence_gate_threshold: with no quiet slice,
    silent_seconds never accumulates, new_segment never fires, and no pause
    ever closes off a segment -- so the protection against a bad window is
    absent. Nothing in the UI shows that, so the log has to.
    """
    monkeypatch.setattr(local_faster_whisper, "_NOISE_FLOOR_WARN_AFTER_S", 0.4)

    class _Model:
        def transcribe(self, *args, **kwargs):
            segment = types.SimpleNamespace(text="text")
            info = types.SimpleNamespace(language="de", language_probability=1.0)
            return [segment], info

    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=0.0,
        stream_partial_min_audio_s=0.0,
        stream_final_full_pass=False,
        model_factory=lambda *args, **kwargs: _Model(),
    )
    transcriber.start_stream(on_partial=lambda text: None)
    try:
        for amplitude in amplitudes:
            transcriber.push_audio_chunk(_ms(100, amplitude))
            transcriber._maybe_emit_partial()
            time.sleep(0.01)
        warned = transcriber._stream_session.result.noise_floor_warned
    finally:
        transcriber.stop_stream()

    assert warned is expected, (
        f"{label}: warned={warned}, expected {expected}"
    )

def _stream_with(model_texts, *, silence_gate_enabled=True):
    outputs = iter(list(model_texts) + ["x"] * 200)

    class _Model:
        def transcribe(self, *args, **kwargs):
            segment = types.SimpleNamespace(text=next(outputs, "x"))
            info = types.SimpleNamespace(language="de", language_probability=1.0)
            return [segment], info

    return LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=0.0,
        stream_partial_min_audio_s=0.0,
        stream_final_full_pass=False,
        silence_gate_enabled=silence_gate_enabled,
        model_factory=lambda *args, **kwargs: _Model(),
    )


@pytest.mark.parametrize("pause_seconds", [6.0, 7.2, 7.5, 8.5, 12.0])
def test_no_pause_length_can_destroy_the_earlier_dictation(pause_seconds):
    """A thinking pause must never cost the text before it.

    The floor used to be pinned only by a pause of at least
    `stream_partial_window_s`, but alignment already fails a little earlier:
    around 7.2-8.0 s a window shares too few words with the accumulated text to
    anchor on, and there was no floor yet either. The replace fallback then
    wiped the whole dictation -- from the document and from history -- and
    because the accumulated text went backwards the locked prefix could never
    advance again, so live insertion froze for the rest of the session too.
    """
    transcriber = _stream_with(
        [
            "das ist der erste",
            "das ist der erste teil meiner",
            "das ist der erste teil meiner nachricht",
            "und jetzt kommt der zweite teil",
        ]
    )
    transcriber.start_stream(on_partial=lambda text: None)
    try:
        for _ in range(3):
            transcriber.push_audio_chunk(_ms(300, 6000))
            time.sleep(0.08)
            transcriber._maybe_emit_partial()
        before = transcriber._stream_session.result.merged_text
        assert before.startswith("das ist der erste")

        for _ in range(int(pause_seconds * 10)):
            transcriber.push_audio_chunk(_ms(100, 20))
        time.sleep(0.12)
        transcriber.push_audio_chunk(_ms(400, 6000))
        time.sleep(0.2)
        transcriber._maybe_emit_partial()

        merged = transcriber._stream_session.result.merged_text
        assert merged.startswith("das ist der erste"), (
            f"a {pause_seconds}s pause destroyed the earlier dictation: {merged!r}"
        )
    finally:
        transcriber.stop_stream()


def test_hallucinated_windows_cannot_grow_the_transcript_without_bound():
    """The 896-junk-words case, driven the way it actually happens.

    Two things an earlier version of this test got wrong, and both made it
    prove nothing:

    - `push_audio_chunk` only enqueues; the buffer grows on the worker
      thread. A tight loop with no yield never lets the worker run, so only
      the three real-speech windows were ever decoded and both assertions
      were tautologies. The decode count is asserted here.
    - Giving every hallucinated window a distinct text means no two of them
      can align, so the dangerous path is never entered. Whisper repeats the
      same invented phrase across windows that share 96% of their audio, and
      two identical windows align trivially -- pinning that made the phrase
      permanent and the next drift appended a fresh one after it. Measured
      before the fix: 53 words from 4 of real speech, growing linearly.
    """
    decoded = []
    real_windows = [
        "das ist",
        "das ist der erste",
        "das ist der erste teil",
        "das ist der erste teil meiner nachricht",
    ]

    class _Model:
        def transcribe(self, *args, **kwargs):
            index = len(decoded)
            if index < len(real_windows):
                text = real_windows[index]
            else:
                # The same phrase for ten windows, then a drift -- the shape
                # whisper actually produces from noise.
                text = f"Untertitelung des ZDF {(index - len(real_windows)) // 10}"
            decoded.append(text)
            segment = types.SimpleNamespace(text=text)
            info = types.SimpleNamespace(language="de", language_probability=1.0)
            return [segment], info

    transcriber = LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=0.0,
        stream_partial_min_audio_s=0.0,
        stream_final_full_pass=False,
        silence_gate_enabled=False,
        model_factory=lambda *args, **kwargs: _Model(),
    )
    transcriber.start_stream(on_partial=lambda text: None)
    try:
        for _ in real_windows:
            transcriber.push_audio_chunk(_ms(300, 6000))
            time.sleep(0.06)
            transcriber._maybe_emit_partial()
        real = transcriber._stream_session.result.merged_text
        spoken_decodes = len(decoded)
        assert real.startswith("das ist"), real

        for _ in range(120):
            time.sleep(0.004)
            transcriber.push_audio_chunk(_ms(100, 20))
            transcriber._maybe_emit_partial()

        merged = transcriber._stream_session.result.merged_text
        # Count here, not after stop_stream(): the drain calls
        # _maybe_emit_partial for every queued chunk, so a starved worker
        # still reaches 120 decodes on the way out and a count read
        # afterwards can never detect that `merged` was measured too early.
        decoded_before_stop = len(decoded)
    finally:
        transcriber.stop_stream()

    assert decoded_before_stop > spoken_decodes + 40, (
        f"only {decoded_before_stop} windows were decoded before the "
        "transcript was read; the hallucination loop never reached the "
        "model, so this test proves nothing"
    )
    assert merged.startswith("das ist"), (
        f"the real speech was destroyed by hallucinations: {merged!r}"
    )
    assert len(merged.split()) <= 12, (
        f"the transcript grew to {len(merged.split())} words over "
        f"{len(decoded) - spoken_decodes} hallucinated windows: {merged!r}"
    )


def test_importing_one_transcriber_does_not_pull_in_every_provider_sdk():
    """`stt_app.transcriber` resolves its names lazily (PEP 562).

    Importing any submodule runs the package first. While the package imported
    every provider eagerly, the two worker subprocesses -- which only ever scan
    a directory or download a file -- each paid for the AssemblyAI, Deepgram,
    OpenAI, Groq, ElevenLabs, Azure and Fun-ASR modules at every launch.
    """
    probe = textwrap.dedent(
        r"""
        import sys
        import stt_app.transcriber.local_faster_whisper  # noqa: F401
        loaded = sorted(
            name
            for name in sys.modules
            if name.startswith("stt_app.transcriber.")
        )
        print("\n".join(loaded))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    loaded = set(result.stdout.split())
    providers = {
        "stt_app.transcriber.assemblyai_provider",
        "stt_app.transcriber.azure_provider",
        "stt_app.transcriber.deepgram_provider",
        "stt_app.transcriber.elevenlabs_provider",
        "stt_app.transcriber.factory",
        "stt_app.transcriber.funasr_provider",
        "stt_app.transcriber.groq_provider",
        "stt_app.transcriber.openai_provider",
    }
    assert loaded & providers == set(), (
        "importing one local transcriber dragged in "
        f"{sorted(loaded & providers)}"
    )
    assert "stt_app.transcriber.local_faster_whisper" in loaded


# The package's public surface, spelled out so a name cannot be dropped by
# editing the lazy map alone -- `__all__` is derived from that map, so a
# deletion there would silently shrink the API without failing anything.
_TRANSCRIBER_PACKAGE_EXPORTS = {
    "AssemblyAITranscriber",
    "AzureLlmSpeechTranscriber",
    "DeepgramTranscriber",
    "ElevenLabsTranscriber",
    "FunAsrTranscriber",
    "GroqTranscriber",
    "ITranscriber",
    "LocalFasterWhisperTranscriber",
    "OpenAITranscriber",
    "TranscriptionError",
    "create_transcriber",
    "find_cached_models",
}


def test_every_exported_transcriber_name_still_resolves():
    """The lazy map must stay in step with what the package promises."""
    import stt_app.transcriber as package

    assert set(package.__all__) == _TRANSCRIBER_PACKAGE_EXPORTS
    for name in package.__all__:
        assert getattr(package, name) is not None, name
    missing = "ThisNameDoesNotExist"
    with pytest.raises(AttributeError):
        getattr(package, missing)


def test_the_typed_imports_and_the_lazy_map_name_the_same_modules():
    """A name typed for static checkers but absent from the map is an
    ``AttributeError`` at runtime that no type checker can see, and the
    reverse hides the name from every editor and linter."""
    import stt_app.transcriber as package

    source = Path(package.__file__).read_text(encoding="utf-8")
    typed: dict[str, str] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.If):
            continue
        if not (isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"):
            continue
        for statement in ast.walk(node):
            if isinstance(statement, ast.ImportFrom):
                module = "." * statement.level + (statement.module or "")
                for alias in statement.names:
                    typed[alias.asname or alias.name] = module

    assert typed == package._LAZY_ATTRIBUTES, (
        "the TYPE_CHECKING imports and the lazy map disagree: "
        f"typed-only={sorted(set(typed) - set(package._LAZY_ATTRIBUTES))}, "
        f"lazy-only={sorted(set(package._LAZY_ATTRIBUTES) - set(typed))}"
    )


def _slow_decode_stream(model_texts):
    """A stream whose partials only fire when the test asks for one.

    `stream_partial_interval_s=0` lets the worker thread decode on every
    drained chunk, which makes the sequence of model outputs depend on thread
    timing. A large interval plus resetting `last_partial_at` by hand puts
    every decode in the test.
    """
    outputs = iter(list(model_texts) + ["x"] * 50)

    class _Model:
        def transcribe(self, *args, **kwargs):
            segment = types.SimpleNamespace(text=next(outputs, "x"))
            info = types.SimpleNamespace(language="de", language_probability=1.0)
            return [segment], info

    return LocalFasterWhisperTranscriber(
        model_size="small",
        stream_partial_interval_s=3600.0,
        stream_partial_min_audio_s=0.0,
        stream_final_full_pass=False,
        model_factory=lambda *args, **kwargs: _Model(),
    )


def _push_and_wait(transcriber, chunk, timeout=5.0):
    """Push audio and wait for the worker to append it to the buffer."""
    session = transcriber._stream_session
    target = len(session.pcm_buffer) + len(chunk)
    transcriber.push_audio_chunk(chunk)
    deadline = time.monotonic() + timeout
    while len(session.pcm_buffer) < target:
        if time.monotonic() > deadline:
            raise AssertionError("the stream worker never drained the chunk")
        time.sleep(0.01)


def _emit_partial_now(transcriber):
    transcriber._stream_session.result.last_partial_at = 0.0
    transcriber._maybe_emit_partial()


def test_a_decode_slower_than_the_window_keeps_the_earlier_transcript():
    """Two windows that share no audio must append, not replace.

    A decode that takes about as long as `stream_partial_window_s` -- a large
    model on a slow machine -- advances the trailing window by more than its
    own length, so consecutive windows are disjoint and cannot be aligned.
    With continuous speech `silent_seconds` never accumulates, so no pause ever
    pinned `segment_floor` and the unalignable window replaced the entire
    accumulated transcript. Whatever the length of the dictation, only the last
    window survived.
    """
    # The two windows must share no words either: three shared trailing words
    # let the merge's re-anchor search find a seam that the audio says cannot
    # exist, and the window is then silently swallowed instead -- a different
    # loss, and one that would have made this test pass for the wrong reason.
    transcriber = _slow_decode_stream(
        ["erster teil der nachricht", "und dann kam etwas ganz anderes"]
    )
    transcriber.start_stream(on_partial=lambda text: None)
    try:
        # More than one window of audio between the two decodes: window two
        # starts after window one ended.
        _push_and_wait(transcriber, _ms(9000, 6000))
        _emit_partial_now(transcriber)
        first = transcriber._stream_session.result.merged_text
        assert first == "erster teil der nachricht", first

        _push_and_wait(transcriber, _ms(9000, 6000))
        _emit_partial_now(transcriber)
        merged = transcriber._stream_session.result.merged_text
        warned = transcriber._stream_session.result.slow_decode_warned
    finally:
        transcriber.stop_stream()

    assert merged == "erster teil der nachricht und dann kam etwas ganz anderes", (
        f"the disjoint window replaced the transcript instead of appending: "
        f"{merged!r}"
    )
    assert warned, "a machine that cannot keep up was never reported"


def test_a_disjoint_window_of_silence_is_not_appended_on_trust():
    """The one unguarded append in the design, and why it had to be closed.

    The pause route demands `_stream_window_has_speech` before it appends a
    window that shares no audio with what came before. The slow-decode route
    reaches the same append through different geometry and demanded nothing.
    A decode near RTF 1 makes each increment about as long as the window, so
    one keystroke in it defeats `_stream_slice_is_quiet` -- a peak measure
    over the whole increment -- `silent_seconds` resets, the pause route never
    runs, and a mostly-silent window is decoded, invented, appended AND pinned
    as the floor. Once per decode: measured over five whisper-typical silence
    hallucinations, 23 words against the 5 a bounded replace keeps.

    Here the increment is one loud second followed by eight silent ones, so
    the increment is not quiet but the eight-second window that is actually
    decoded is silence.
    """
    transcriber = _slow_decode_stream(
        ["erster teil der nachricht", "Untertitel von Stephanie Geiges"]
    )
    transcriber.start_stream(on_partial=lambda text: None)
    try:
        _push_and_wait(transcriber, _ms(9000, 6000))
        _emit_partial_now(transcriber)
        assert (
            transcriber._stream_session.result.merged_text
            == "erster teil der nachricht"
        )

        # Loud enough not to be skipped, silent where it counts.
        _push_and_wait(transcriber, _ms(1000, 6000) + _ms(8000, 0))
        _emit_partial_now(transcriber)
        merged = transcriber._stream_session.result.merged_text
    finally:
        transcriber.stop_stream()

    assert merged != (
        "erster teil der nachricht Untertitel von Stephanie Geiges"
    ), (
        "a hallucination decoded from eight seconds of silence was appended on "
        f"trust: {merged!r}"
    )


def test_the_decoded_window_is_measured_by_its_recorded_range():
    """Not the trailing window -- by the time we can ask, they differ.

    Disjointness is only knowable after the decode, and this path exists
    because that decode lasted longer than the window itself, so the capture
    thread appended a whole window's worth of new audio meanwhile. Measuring
    "the trailing window" would then measure audio the model never saw, which
    is the wrong question in exactly the case that matters.
    """
    transcriber = _slow_decode_stream(["x"])
    silence = _ms(8000, 0)
    loud = _ms(8000, 6000)
    session = types.SimpleNamespace(
        pcm_buffer=bytearray(silence + loud),
        result=local_faster_whisper._StreamResult(
            last_window_start=0,
            last_window_end=len(silence),
        ),
    )

    assert transcriber._decoded_window_has_speech(session) is False, (
        "the silent range that was actually decoded was reported as speech"
    )
    assert transcriber._stream_window_has_speech(session) is True, (
        "precondition: the trailing window is loud, so the two questions "
        "really do have different answers here"
    )


def test_audio_that_cannot_be_measured_is_never_appended_on_trust():
    """"Cannot tell" must fall back to the safe aligning path.

    Appending is the risky direction: it is what grew a transcript to 896
    invented words during two minutes of an open microphone. The meter
    returns ``None`` rather than ``0.0`` for audio it cannot measure, and
    that has to be refused, not waved through.
    """
    transcriber = _slow_decode_stream(["x"])

    assert transcriber._pcm_has_enough_speech_to_append(b"") is False
    # One byte is not a whole int16 sample, so there is nothing to measure.
    assert transcriber._pcm_has_enough_speech_to_append(b"\x00") is False


def test_a_disjoint_final_window_of_silence_is_not_appended_on_trust():
    """The finalizer needs the same proof as the partial path.

    Here the loss is worse: the fast finalizer runs once, at the moment the
    transcript is handed over, so an invented window appended on trust goes
    straight into the document and into history.

    The tail is silence plus one short transient, which is what makes it
    reach the decode at all: `_stream_tail_window_is_silent` is a peak
    measure and the transient defeats it, while the 20 ms-bucketed longest
    run stays far below the append cut.
    """
    transcriber = _slow_decode_stream(
        ["erster teil der nachricht", "Untertitel von Stephanie Geiges"]
    )
    transcriber.start_stream(on_partial=lambda text: None)
    try:
        _push_and_wait(transcriber, _ms(9000, 6000))
        _emit_partial_now(transcriber)
        assert (
            transcriber._stream_session.result.merged_text
            == "erster teil der nachricht"
        )
        # Exactly one window of new audio, so the finalizer's window starts
        # where the partial's ended: disjoint, with nothing shared.
        _push_and_wait(
            transcriber, _ms(20, 6000) + _ms(7980, 0)
        )
        final_text = transcriber.stop_stream()
    finally:
        transcriber.abort_stream()

    assert final_text != (
        "erster teil der nachricht Untertitel von Stephanie Geiges"
    ), (
        "the finalizer appended a hallucination decoded from a silent window: "
        f"{final_text!r}"
    )


def test_a_final_window_that_shares_no_audio_keeps_the_dictation():
    """The finalizer has the same seam, and there it costs everything at once.

    A partial that decodes slowly enough leaves the trailing window the
    finalizer takes disjoint from the last decoded partial, so the fast
    finalization replaced the whole dictation with its last few seconds --
    a single step, at the moment the text is handed over.
    """
    transcriber = _slow_decode_stream(
        ["erster teil der nachricht", "der letzte satz"]
    )
    transcriber.start_stream(on_partial=lambda text: None)
    try:
        _push_and_wait(transcriber, _ms(9000, 6000))
        _emit_partial_now(transcriber)
        assert (
            transcriber._stream_session.result.merged_text
            == "erster teil der nachricht"
        )
        _push_and_wait(transcriber, _ms(9000, 6000))
        final_text = transcriber.stop_stream()
    finally:
        transcriber.abort_stream()

    assert final_text.startswith("erster teil der nachricht"), (
        f"the finalizer dropped everything but its own window: {final_text!r}"
    )
    assert "der letzte satz" in final_text, final_text


def test_overlapping_windows_still_merge_by_alignment():
    """The guard must not fire while the windows do overlap.

    Treating an ordinary rolling window as a new segment would append the
    words the two windows share, duplicating them at every partial.
    """
    transcriber = _slow_decode_stream(
        ["das ist der erste", "das ist der erste teil"]
    )
    transcriber.start_stream(on_partial=lambda text: None)
    try:
        _push_and_wait(transcriber, _ms(1000, 6000))
        _emit_partial_now(transcriber)
        _push_and_wait(transcriber, _ms(1000, 6000))
        _emit_partial_now(transcriber)
        merged = transcriber._stream_session.result.merged_text
        warned = transcriber._stream_session.result.slow_decode_warned
    finally:
        transcriber.stop_stream()

    assert merged == "das ist der erste teil", merged
    assert not warned, "overlapping windows were reported as a slow decode"


@pytest.mark.parametrize(
    ("label", "previous_end", "window_start", "expected"),
    [
        ("windows that overlap by a lot", 288000, 32000, False),
        ("windows that overlap by one byte", 288000, 287999, False),
        # Adjacent with nothing in common: the last sample of one window and
        # the first of the next. There is no shared audio, so there is nothing
        # for the merge to anchor on -- `<=` here would let the whole
        # transcript be replaced at exactly this offset.
        ("windows that are exactly adjacent", 288000, 288000, True),
        ("windows with a gap between them", 288000, 320000, True),
        ("nothing decoded yet", 0, 0, False),
    ],
)
@pytest.mark.parametrize("pause_explains_the_gap", [False, True])
def test_disjoint_windows_are_recognised_at_the_boundary(
    label, previous_end, window_start, expected, pause_explains_the_gap
):
    """The verdict is geometry; only the warning depends on the cause.

    Two windows share no audio either because a decode took longer than the
    window (speech in the gap was never decoded -- a real defect worth a
    warning) or because the silence gate skipped every partial during a pause,
    which stops `last_window_end` advancing while the buffer grows. The gap is
    then measured silence and nothing was lost. Warning there was false three
    times over, and because the warning latches per session, one ordinary
    thinking pause consumed it and the genuine case could never be reported
    again.
    """
    transcriber = _slow_decode_stream(["x"])
    session = types.SimpleNamespace(
        result=local_faster_whisper._StreamResult(last_window_start=window_start)
    )

    disjoint = transcriber._window_shares_no_audio_with_the_last(
        session,
        previous_end,
        pause_explains_the_gap=pause_explains_the_gap,
    )

    assert disjoint is expected, label
    assert session.result.slow_decode_warned is (
        expected and not pause_explains_the_gap
    ), f"{label}: the warning did not follow the cause"


def test_a_dying_stream_worker_is_reported_instead_of_vanishing():
    """A worker that raises must not look like a silent dictation.

    Only the decode inside `_maybe_emit_partial` and the finalization were
    guarded. The energy meters, the merge and the buffer append were not, so an
    exception there simply ended the thread: `stop_stream` joined a dead worker,
    found no error and an empty `final_text`, and the whole dictation reached
    the user as "No speech detected". A windowed build has no stderr either, so
    `threading.excepthook` printed the traceback nowhere.
    """
    # The worker emits on every drained chunk here, so the meter is reached on
    # the worker thread -- driving `_maybe_emit_partial` from the test would
    # raise on the test's own stack and prove nothing about the thread.
    transcriber = _stream_with(["etwas gesprochenes"])

    def _boom(_pcm_bytes):
        raise MemoryError("the meter died")

    reported: list[str] = []
    transcriber.start_stream(
        on_partial=lambda text: None, on_error=reported.append
    )
    try:
        transcriber._stream_slice_is_quiet = _boom
        _push_and_wait(transcriber, _ms(400, 6000))
        worker = transcriber._stream_thread
        deadline = time.monotonic() + 5.0
        while worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not worker.is_alive(), "the worker survived the raising meter"

        # Reported when it happens, not only when the user next presses stop.
        # `session.result.error` alone is read at stop time, so until then the
        # live text just stops advancing -- indistinguishable from having
        # stopped talking -- and everything said meanwhile is lost undecoded.
        assert reported, (
            "the worker died without telling the controller, so the session "
            "kept recording into nothing"
        )
        assert "the meter died" in reported[0], reported

        with pytest.raises(TranscriptionError) as failure:
            transcriber.stop_stream()
    finally:
        transcriber.abort_stream()

    assert "the meter died" in str(failure.value), failure.value
    assert len(reported) == 1, f"the failure was reported twice: {reported}"


def test_the_stream_worker_guard_keeps_the_first_error():
    """A later failure must not replace the one that explains the session.

    The decode and the finalization record their own error and then return
    normally, so the top-level guard sees them only when something raises on
    the way out -- a partial callback notification, say. Overwriting there
    would replace "the model could not be loaded" with whatever failed while
    reporting it.
    """
    transcriber = _stream_with(["x"])
    transcriber.start_stream(on_partial=lambda text: None)
    session = transcriber._stream_session
    try:
        first = TranscriptionError("the decode failed")
        session.result.error = first

        def _boom(_session):
            raise RuntimeError("the notification failed")

        transcriber._run_stream_worker = _boom
        transcriber._stream_worker(session)

        assert session.result.error is first, session.result.error
    finally:
        transcriber.abort_stream()


@pytest.mark.parametrize(
    ("label", "buffer_seconds", "window_seconds"),
    [
        ("a buffer longer than the window", 30.0, 8.0),
        ("a buffer shorter than the window", 2.0, 8.0),
        ("no window at all", 30.0, None),
        ("a zero window", 30.0, 0.0),
    ],
)
def test_the_trailing_window_copies_only_the_window_it_reports(
    label, buffer_seconds, window_seconds
):
    """The offsets have to describe exactly the bytes that come back.

    They are what `_window_shares_no_audio_with_the_last` compares, so a slice
    that does not match the reported range would decide the append-or-replace
    question from audio it did not decode. Copying only the window is the
    other half: `bytes(pcm_buffer)` grows with the dictation -- 3.14 ms at
    fifteen minutes against a flat 0.10 ms -- and a partial runs every 350 ms.
    """
    transcriber = _stream_with(["x"])
    session = types.SimpleNamespace(
        pcm_buffer=bytearray(_ms(int(buffer_seconds * 1000), 6000))
    )
    total = len(session.pcm_buffer)

    snapshot, start, end = transcriber._trailing_window(session, window_seconds)

    assert end == total, label
    assert snapshot == bytes(session.pcm_buffer[start:end]), label
    if window_seconds:
        expected = min(total, int(window_seconds * transcriber.stream_sample_rate * 2))
    else:
        expected = total
    assert len(snapshot) == expected, (
        f"{label}: copied {len(snapshot)} bytes, expected {expected}"
    )
    assert start == total - expected, label
