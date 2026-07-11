import io
import threading
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
    return b"\x01\x00" * sample_count


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
