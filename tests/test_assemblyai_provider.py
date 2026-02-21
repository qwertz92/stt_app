"""Tests for AssemblyAI transcription provider."""

from __future__ import annotations

import types
from unittest.mock import patch

import pytest

from tts_app.transcriber.assemblyai_provider import AssemblyAITranscriber
from tts_app.transcriber.base import TranscriptionError


# ---------------------------------------------------------------------------
# Fake assemblyai module for injection
# ---------------------------------------------------------------------------


def _make_fake_aai(transcript_text: str = "hello world", error: str | None = None):
    """Build a fake ``assemblyai`` module with controllable behavior."""
    aai = types.ModuleType("assemblyai")

    class FakeTranscriptStatus:
        error = "error"
        completed = "completed"

    class FakeSpeechModel:
        universal_3_pro = "universal-3-pro"
        universal_2 = "universal-2"

    class FakeTranscriptionConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeTranscript:
        def __init__(self):
            if error:
                self.status = FakeTranscriptStatus.error
                self.error = error
                self.text = None
            else:
                self.status = FakeTranscriptStatus.completed
                self.error = None
                self.text = transcript_text

    class FakeTranscriber:
        calls: list = []

        def transcribe(self, audio_file, config=None):
            FakeTranscriber.calls.append({"audio_file": audio_file, "config": config})
            return FakeTranscript()

    class FakeSettings:
        api_key = ""
        base_url = ""

    # Real-time streaming types
    class FakeRealtimeFinalTranscript:
        """Represents a finalized transcript segment."""

        def __init__(self, text=""):
            self.text = text

    class FakeRealtimePartialTranscript:
        """Represents an in-progress partial transcript."""

        def __init__(self, text=""):
            self.text = text

    class FakeRealtimeTranscriber:
        """Fake RealtimeTranscriber for testing streaming."""

        instances: list = []

        def __init__(self, *, sample_rate=16000, on_data=None, on_error=None):
            self.sample_rate = sample_rate
            self.on_data = on_data
            self.on_error = on_error
            self.connected = False
            self.closed = False
            self.streamed_chunks: list[bytes] = []
            FakeRealtimeTranscriber.instances.append(self)

        def connect(self):
            self.connected = True

        def stream(self, chunk: bytes):
            self.streamed_chunks.append(chunk)

        def close(self):
            self.closed = True
            self.connected = False

    aai.TranscriptStatus = FakeTranscriptStatus
    aai.SpeechModel = FakeSpeechModel
    aai.TranscriptionConfig = FakeTranscriptionConfig
    aai.Transcriber = FakeTranscriber
    aai.settings = FakeSettings()
    aai.RealtimeFinalTranscript = FakeRealtimeFinalTranscript
    aai.RealtimePartialTranscript = FakeRealtimePartialTranscript
    aai.RealtimeTranscriber = FakeRealtimeTranscriber

    # Reset call tracking
    FakeTranscriber.calls = []
    FakeRealtimeTranscriber.instances = []

    return aai


# ---------------------------------------------------------------------------
# Tests: constructor validation
# ---------------------------------------------------------------------------


class TestAssemblyAITranscriberInit:
    def test_missing_api_key_raises(self):
        with pytest.raises(TranscriptionError, match="API key is missing"):
            AssemblyAITranscriber(api_key="")

    def test_none_api_key_raises(self):
        with pytest.raises(TranscriptionError, match="API key is missing"):
            AssemblyAITranscriber(api_key=None)

    def test_valid_api_key_accepted(self):
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(api_key="test-key", aai_module=fake_aai)
        assert t._api_key == "test-key"


# ---------------------------------------------------------------------------
# Tests: batch transcription
# ---------------------------------------------------------------------------


class TestAssemblyAITranscribeBatch:
    def test_transcribe_file_path(self, tmp_path):
        """Transcription with a file path passes through correctly."""
        fake_aai = _make_fake_aai(transcript_text="Hallo Welt")
        t = AssemblyAITranscriber(
            api_key="test-key", language_mode="de", aai_module=fake_aai
        )

        wav = tmp_path / "test.wav"
        wav.write_bytes(b"RIFF fake wav data")

        result = t.transcribe_batch(str(wav))
        assert result == "Hallo Welt"
        assert len(fake_aai.Transcriber.calls) == 1
        assert fake_aai.Transcriber.calls[0]["audio_file"] == str(wav)

    def test_transcribe_bytes_creates_temp_file(self):
        """Transcription with WAV bytes creates a temp file."""
        fake_aai = _make_fake_aai(transcript_text="hello world")
        t = AssemblyAITranscriber(api_key="test-key", aai_module=fake_aai)

        result = t.transcribe_batch(b"RIFF fake wav data")
        assert result == "hello world"
        assert len(fake_aai.Transcriber.calls) == 1
        # File path should end with .wav
        assert fake_aai.Transcriber.calls[0]["audio_file"].endswith(".wav")

    def test_transcribe_empty_result(self):
        """Empty transcript text returns empty string."""
        fake_aai = _make_fake_aai(transcript_text="")
        t = AssemblyAITranscriber(api_key="test-key", aai_module=fake_aai)
        result = t.transcribe_batch(b"RIFF fake")
        assert result == ""

    def test_transcribe_none_text_returns_empty(self):
        """None transcript text returns empty string."""
        fake_aai = _make_fake_aai(transcript_text="")
        # Override to return None

        class PatchedTranscript:
            status = fake_aai.TranscriptStatus.completed
            error = None
            text = None

        class PatchedTranscriber:
            calls = []

            def transcribe(self, audio_file, config=None):
                PatchedTranscriber.calls.append(
                    {"audio_file": audio_file, "config": config}
                )
                return PatchedTranscript()

        fake_aai.Transcriber = PatchedTranscriber
        t = AssemblyAITranscriber(api_key="test-key", aai_module=fake_aai)
        result = t.transcribe_batch(b"RIFF fake")
        assert result == ""

    def test_transcribe_strips_whitespace(self):
        """Result text is stripped of whitespace."""
        fake_aai = _make_fake_aai(transcript_text="  trimmed text  ")
        t = AssemblyAITranscriber(api_key="test-key", aai_module=fake_aai)
        result = t.transcribe_batch(b"RIFF fake")
        assert result == "trimmed text"


# ---------------------------------------------------------------------------
# Tests: error handling
# ---------------------------------------------------------------------------


class TestAssemblyAIErrorHandling:
    def test_api_error_raises_transcription_error(self):
        """AssemblyAI API error status raises TranscriptionError."""
        fake_aai = _make_fake_aai(error="Authentication failed")
        t = AssemblyAITranscriber(api_key="bad-key", aai_module=fake_aai)

        with pytest.raises(TranscriptionError, match="Authentication failed"):
            t.transcribe_batch(b"RIFF fake")

    def test_exception_during_transcribe_raises(self):
        """Unexpected exception during transcription raises TranscriptionError."""
        fake_aai = _make_fake_aai()

        class ExplodingTranscriber:
            def transcribe(self, audio_file, config=None):
                raise ConnectionError("Network unreachable")

        fake_aai.Transcriber = ExplodingTranscriber
        t = AssemblyAITranscriber(api_key="test-key", aai_module=fake_aai)

        with pytest.raises(TranscriptionError, match="Network unreachable"):
            t.transcribe_batch(b"RIFF fake")

    def test_missing_assemblyai_package(self):
        """Lazy import failure gives actionable error message."""
        t = AssemblyAITranscriber.__new__(AssemblyAITranscriber)
        t._api_key = "test-key"
        t._language_mode = "auto"
        t._aai = None  # Force lazy import

        with patch.dict("sys.modules", {"assemblyai": None}):
            with pytest.raises(TranscriptionError, match="assemblyai.*not installed"):
                t._get_aai()


# ---------------------------------------------------------------------------
# Tests: API key configuration
# ---------------------------------------------------------------------------


class TestAssemblyAIConfiguration:
    def test_api_key_set_on_configure(self):
        """_configure() sets the API key on the aai settings object."""
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(api_key="my-secret-key", aai_module=fake_aai)
        t._configure()
        assert fake_aai.settings.api_key == "my-secret-key"


# ---------------------------------------------------------------------------
# Tests: language configuration
# ---------------------------------------------------------------------------


class TestAssemblyAILanguageConfig:
    def test_auto_language_enables_detection(self):
        """language_mode='auto' enables language_detection in config."""
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(
            api_key="key", language_mode="auto", aai_module=fake_aai
        )
        config = t._build_config()
        assert config.kwargs.get("language_detection") is True

    def test_specific_language_disables_detection(self):
        """language_mode='de' sets language_code and disables detection."""
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(
            api_key="key", language_mode="de", aai_module=fake_aai
        )
        config = t._build_config()
        assert config.kwargs.get("language_code") == "de"
        assert config.kwargs.get("language_detection") is False

    def test_english_language(self):
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(
            api_key="key", language_mode="en", aai_module=fake_aai
        )
        config = t._build_config()
        assert config.kwargs.get("language_code") == "en"
        assert config.kwargs.get("language_detection") is False

    def test_unknown_language_falls_back_to_auto(self):
        """Unknown language code falls back to auto detection."""
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(
            api_key="key", language_mode="zh", aai_module=fake_aai
        )
        config = t._build_config()
        assert config.kwargs.get("language_detection") is True
        assert "language_code" not in config.kwargs


# ---------------------------------------------------------------------------
# Tests: real-time streaming
# ---------------------------------------------------------------------------


class TestAssemblyAIStreaming:
    def test_start_stream_connects(self):
        """start_stream creates a RealtimeTranscriber and connects."""
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(api_key="key", aai_module=fake_aai)
        t.start_stream(on_partial=lambda text: None)
        assert len(fake_aai.RealtimeTranscriber.instances) == 1
        rt = fake_aai.RealtimeTranscriber.instances[0]
        assert rt.connected is True
        assert rt.sample_rate == 16000
        t.abort_stream()

    def test_push_audio_chunk_forwards_data(self):
        """push_audio_chunk sends data to the real-time transcriber."""
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(api_key="key", aai_module=fake_aai)
        t.start_stream()
        t.push_audio_chunk(b"\x01\x00" * 160)
        rt = fake_aai.RealtimeTranscriber.instances[0]
        assert len(rt.streamed_chunks) == 1
        assert rt.streamed_chunks[0] == b"\x01\x00" * 160
        t.abort_stream()

    def test_stop_stream_returns_accumulated_text(self):
        """stop_stream returns all finalized text joined."""
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(api_key="key", aai_module=fake_aai)
        t.start_stream()

        # Simulate final transcripts arriving via on_data callback.
        rt = fake_aai.RealtimeTranscriber.instances[0]
        rt.on_data(fake_aai.RealtimeFinalTranscript("Hello world."))
        rt.on_data(fake_aai.RealtimeFinalTranscript("How are you?"))

        result = t.stop_stream()
        assert result == "Hello world. How are you?"

    def test_stop_stream_includes_current_partial(self):
        """stop_stream includes the last partial if no final followed."""
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(api_key="key", aai_module=fake_aai)
        t.start_stream()

        rt = fake_aai.RealtimeTranscriber.instances[0]
        rt.on_data(fake_aai.RealtimeFinalTranscript("Hello."))
        # Simulate a partial transcript (not yet finalized).
        rt.on_data(fake_aai.RealtimePartialTranscript("How are"))

        result = t.stop_stream()
        assert result == "Hello. How are"

    def test_partial_replaced_by_next_partial(self):
        """Each partial transcript replaces the previous one."""
        fake_aai = _make_fake_aai()
        partials = []
        t = AssemblyAITranscriber(api_key="key", aai_module=fake_aai)
        t.start_stream(on_partial=lambda text: partials.append(text))

        rt = fake_aai.RealtimeTranscriber.instances[0]
        rt.on_data(fake_aai.RealtimePartialTranscript("Hel"))
        rt.on_data(fake_aai.RealtimePartialTranscript("Hello wor"))
        rt.on_data(fake_aai.RealtimePartialTranscript("Hello world"))

        # Each partial replaces the previous, so callback gets growing text.
        assert len(partials) == 3
        assert partials[-1] == "Hello world"

        result = t.stop_stream()
        # Only the last partial is included (no finals sent).
        assert result == "Hello world"

    def test_final_clears_partial(self):
        """A FinalTranscript clears the current partial."""
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(api_key="key", aai_module=fake_aai)
        t.start_stream()

        rt = fake_aai.RealtimeTranscriber.instances[0]
        rt.on_data(fake_aai.RealtimePartialTranscript("Hello world"))
        rt.on_data(fake_aai.RealtimeFinalTranscript("Hello world."))

        result = t.stop_stream()
        # Should NOT duplicate: "Hello world." only once.
        assert result == "Hello world."

    def test_abort_stream_discards_text(self):
        """abort_stream closes the connection and discards all text."""
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(api_key="key", aai_module=fake_aai)
        t.start_stream()

        rt = fake_aai.RealtimeTranscriber.instances[0]
        rt.on_data(fake_aai.RealtimeFinalTranscript("Some text"))
        t.abort_stream()

        assert rt.closed is True
        # After abort, stop_stream should return empty.
        # (In practice, abort replaces stop_stream — but the state is cleared.)
        assert t._stream_finals == []
        assert t._stream_current_partial == ""

    def test_on_partial_callback_receives_combined_text(self):
        """on_partial callback receives finals + current partial combined."""
        fake_aai = _make_fake_aai()
        received = []
        t = AssemblyAITranscriber(api_key="key", aai_module=fake_aai)
        t.start_stream(on_partial=lambda text: received.append(text))

        rt = fake_aai.RealtimeTranscriber.instances[0]
        rt.on_data(fake_aai.RealtimeFinalTranscript("First sentence."))
        rt.on_data(fake_aai.RealtimePartialTranscript("Second"))

        assert len(received) == 2
        assert received[0] == "First sentence."
        assert received[1] == "First sentence. Second"
        t.abort_stream()

    def test_stop_stream_closes_connection(self):
        """stop_stream closes the WebSocket connection."""
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(api_key="key", aai_module=fake_aai)
        t.start_stream()
        rt = fake_aai.RealtimeTranscriber.instances[0]
        t.stop_stream()
        assert rt.closed is True

    def test_push_chunk_without_start_is_noop(self):
        """push_audio_chunk before start_stream is a no-op."""
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(api_key="key", aai_module=fake_aai)
        # Should not raise.
        t.push_audio_chunk(b"\x00\x00" * 160)

    def test_on_error_stores_error(self):
        """on_error callback stores the error for later reporting."""
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(api_key="key", aai_module=fake_aai)
        t.start_stream()

        rt = fake_aai.RealtimeTranscriber.instances[0]
        rt.on_error(RuntimeError("WebSocket disconnected"))

        # If no text was received, stop_stream should raise with the error.
        with pytest.raises(TranscriptionError, match="WebSocket disconnected"):
            t.stop_stream()

    def test_on_error_with_text_returns_text(self):
        """If text was received before an error, stop_stream returns it."""
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(api_key="key", aai_module=fake_aai)
        t.start_stream()

        rt = fake_aai.RealtimeTranscriber.instances[0]
        rt.on_data(fake_aai.RealtimeFinalTranscript("Hello."))
        rt.on_error(RuntimeError("late error"))

        # Text was collected before the error → return it.
        result = t.stop_stream()
        assert result == "Hello."

    def test_empty_final_transcript_ignored(self):
        """Empty FinalTranscript text is not appended to finals."""
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(api_key="key", aai_module=fake_aai)
        t.start_stream()

        rt = fake_aai.RealtimeTranscriber.instances[0]
        rt.on_data(fake_aai.RealtimeFinalTranscript(""))
        rt.on_data(fake_aai.RealtimeFinalTranscript("Hello."))

        result = t.stop_stream()
        assert result == "Hello."

    def test_connect_failure_raises_transcription_error(self):
        """Connection failure raises TranscriptionError."""
        fake_aai = _make_fake_aai()

        class FailingRT:
            def __init__(self, **kwargs):
                pass

            def connect(self):
                raise ConnectionError("WebSocket refused")

        fake_aai.RealtimeTranscriber = FailingRT

        t = AssemblyAITranscriber(api_key="key", aai_module=fake_aai)
        with pytest.raises(TranscriptionError, match="failed to connect"):
            t.start_stream()


# ---------------------------------------------------------------------------
# Tests: factory routing
# ---------------------------------------------------------------------------


class TestFactoryAssemblyAI:
    def test_factory_creates_assemblyai_transcriber(self):
        """create_transcriber routes engine='assemblyai' correctly."""
        from tts_app.settings_store import AppSettings
        from tts_app.transcriber.factory import create_transcriber

        class FakeSecretStore:
            def get_api_key(self, provider):
                if provider == "assemblyai":
                    return "test-factory-key"
                return None

        settings = AppSettings(engine="assemblyai", language_mode="de")
        t = create_transcriber(settings, secret_store=FakeSecretStore())
        assert isinstance(t, AssemblyAITranscriber)
        assert t._api_key == "test-factory-key"
        assert t._language_mode == "de"

    def test_factory_assemblyai_no_secret_store(self):
        """create_transcriber with no secret_store gives empty API key → error on use."""
        from tts_app.settings_store import AppSettings
        from tts_app.transcriber.factory import create_transcriber

        # Without secret_store, api_key will be empty → TranscriptionError
        with pytest.raises(TranscriptionError, match="API key is missing"):
            settings = AppSettings(engine="assemblyai")
            create_transcriber(settings, secret_store=None)

    def test_factory_local_unchanged(self):
        """Local engine routing still works after factory changes."""
        from tts_app.transcriber.factory import create_transcriber
        from tts_app.transcriber.local_faster_whisper import (
            LocalFasterWhisperTranscriber,
        )
        from tts_app.settings_store import AppSettings

        settings = AppSettings(engine="local", model_size="small")
        t = create_transcriber(settings)
        assert isinstance(t, LocalFasterWhisperTranscriber)


# ---------------------------------------------------------------------------
# Tests: settings_store assemblyai key
# ---------------------------------------------------------------------------


class TestSettingsStoreAssemblyAI:
    def test_has_assemblyai_key_default_false(self):
        from tts_app.settings_store import AppSettings

        s = AppSettings()
        assert s.has_assemblyai_key is False

    def test_has_assemblyai_key_from_dict(self):
        from tts_app.settings_store import AppSettings

        s = AppSettings.from_dict({"has_assemblyai_key": True})
        assert s.has_assemblyai_key is True

    def test_assemblyai_in_valid_engines(self):
        from tts_app.config import VALID_ENGINES

        assert "assemblyai" in VALID_ENGINES

    def test_assemblyai_engine_validated(self):
        from tts_app.settings_store import AppSettings

        s = AppSettings.from_dict({"engine": "assemblyai"})
        assert s.engine == "assemblyai"
