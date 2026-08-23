import io
import math
import struct
import threading
import time
import types
import wave

import pytest

from stt_app.transcriber.base import TranscriptionCanceled, TranscriptionError
from stt_app.transcriber import local_faster_whisper
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
    checks = {"count": 0}

    def cancel_check():
        checks["count"] += 1
        # False for the pre-decode check, True once the first segment is in.
        return checks["count"] >= 2

    transcriber.set_cancel_check(cancel_check)

    with pytest.raises(TranscriptionCanceled):
        transcriber.transcribe_batch(_build_wav_bytes())

    # Stopped early: it did not consume all three segments.
    assert model.yielded == ["one"]


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
        # 200 ms: just above the post-pause cut, which sits above every
        # separable desk transient. A 150 ms word is deliberately below it --
        # see test_the_known_cost_of_the_post_pause_gate_is_a_very_short_word.
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
      before the fix: 52 words from 4 of real speech, growing linearly.
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
