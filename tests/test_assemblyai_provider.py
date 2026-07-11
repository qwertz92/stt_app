"""Tests for AssemblyAI transcription provider."""

from __future__ import annotations

import types
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from stt_app.transcriber.assemblyai_provider import AssemblyAITranscriber
from stt_app.transcriber.base import TranscriptionError


# ---------------------------------------------------------------------------
# Fake assemblyai module for injection
# ---------------------------------------------------------------------------


def _make_fake_aai(transcript_text: str = "hello world", error: str | None = None):
    """Build a fake ``assemblyai`` module with controllable behavior."""
    aai = types.ModuleType("assemblyai")

    class FakeTranscriptStatus:
        error = "error"
        completed = "completed"

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

        def wait_for_completion(self):
            return self

    class FakeTranscriber:
        calls: list = []

        def transcribe(self, audio_file, config=None):
            FakeTranscriber.calls.append({"audio_file": audio_file, "config": config})
            return FakeTranscript()

        def upload_file(self, audio_file):
            FakeTranscriber.calls.append({"upload_file": audio_file})
            return "https://assemblyai.test/uploaded.wav"

        def submit(self, audio_url, config=None):
            FakeTranscriber.calls.append({"audio_url": audio_url, "config": config})
            return FakeTranscript()

    class FakeSettings:
        api_key = ""
        base_url = ""

    aai.TranscriptStatus = FakeTranscriptStatus
    aai.TranscriptionConfig = FakeTranscriptionConfig
    aai.Transcriber = FakeTranscriber
    aai.settings = FakeSettings()

    # Reset call tracking
    FakeTranscriber.calls = []

    return aai


class FakeStreamingClient:
    """Fake Universal-Streaming (v3) client for testing streaming."""

    def __init__(self, api_key=""):
        self.api_key = api_key
        self.handlers: dict = {}
        self.connect_params = None
        self.connected = False
        self.terminated = False
        self.streamed_chunks: list[bytes] = []

    def on(self, event, handler):
        self.handlers[getattr(event, "value", event)] = handler

    def connect(self, params):
        self.connect_params = params
        self.connected = True

    def stream(self, chunk: bytes):
        self.streamed_chunks.append(chunk)

    def disconnect(self, terminate=False):
        self.terminated = bool(terminate)
        self.connected = False

    # -- test helpers -------------------------------------------------------

    def emit_turn(self, transcript, turn_order=0, end_of_turn=False, formatted=False):
        event = SimpleNamespace(
            type="Turn",
            transcript=transcript,
            turn_order=turn_order,
            end_of_turn=end_of_turn,
            turn_is_formatted=formatted,
        )
        self.handlers["Turn"](self, event)

    def emit_error(self, error):
        self.handlers["Error"](self, error)


def _make_streaming_transcriber(api_key="key"):
    fake_aai = _make_fake_aai()
    clients: list[FakeStreamingClient] = []

    def factory(key):
        client = FakeStreamingClient(api_key=key)
        clients.append(client)
        return client

    transcriber = AssemblyAITranscriber(
        api_key=api_key,
        aai_module=fake_aai,
        streaming_client_factory=factory,
    )
    return transcriber, clients


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
            api_key="key", language_mode="ast", aai_module=fake_aai
        )
        config = t._build_config()
        assert config.kwargs.get("language_detection") is True
        assert "language_code" not in config.kwargs

    def test_batch_model_uses_universal_2_when_selected(self):
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(
            api_key="key",
            model="universal-2",
            aai_module=fake_aai,
        )
        config = t._build_config()
        assert config.kwargs.get("speech_models") == ["universal-2"]

    def test_batch_model_uses_universal_3_with_fallback(self):
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(
            api_key="key",
            model="universal-3-pro",
            aai_module=fake_aai,
        )
        config = t._build_config()
        assert config.kwargs.get("speech_models") == [
            "universal-3-pro",
            "universal-2",
        ]

    def test_legacy_batch_model_is_rejected(self):
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(
            api_key="key",
            model="nano",
            aai_module=fake_aai,
        )
        with pytest.raises(TranscriptionError, match="Unsupported AssemblyAI model"):
            t._build_config()

    def test_custom_vocabulary_sets_word_boost(self):
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(
            api_key="key",
            aai_module=fake_aai,
            custom_vocabulary="Kubernetes, Splunk SOAR",
        )
        config = t._build_config()
        assert config.kwargs.get("word_boost") == ["Kubernetes", "Splunk SOAR"]

    def test_empty_custom_vocabulary_omits_word_boost(self):
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(api_key="key", aai_module=fake_aai)
        config = t._build_config()
        assert "word_boost" not in config.kwargs

    def test_progress_callback_splits_upload_and_polling_phases(self, tmp_path):
        fake_aai = _make_fake_aai(transcript_text="done")
        t = AssemblyAITranscriber(api_key="test-key", aai_module=fake_aai)
        progress: list[str] = []
        t.set_progress_callback(progress.append)
        wav = tmp_path / "test.wav"
        wav.write_bytes(b"RIFF fake wav data")

        result = t.transcribe_batch(str(wav))

        assert result == "done"
        assert progress == [
            "Uploading audio to AssemblyAI...",
            "Upload complete. Submitting transcription to AssemblyAI...",
            "AssemblyAI is transcribing audio...",
        ]
        assert fake_aai.Transcriber.calls[0]["upload_file"] == str(wav)
        assert fake_aai.Transcriber.calls[1]["audio_url"] == (
            "https://assemblyai.test/uploaded.wav"
        )


# ---------------------------------------------------------------------------
# Tests: real-time streaming
# ---------------------------------------------------------------------------


class TestAssemblyAIStreaming:
    def test_start_stream_connects(self):
        """start_stream creates a v3 streaming client and connects."""
        t, clients = _make_streaming_transcriber()
        t.start_stream(on_partial=lambda text: None)
        assert len(clients) == 1
        client = clients[0]
        assert client.connected is True
        assert client.api_key == "key"
        params = client.connect_params
        assert params.sample_rate == 16000
        assert str(params.encoding) == "pcm_s16le"
        assert str(params.speech_model) == "u3-rt-pro"
        assert params.language_detection is True
        assert params.format_turns is None
        t.abort_stream()

    def test_start_stream_passes_custom_vocabulary_as_u3_keyterms(self):
        fake_aai = _make_fake_aai()
        clients: list[FakeStreamingClient] = []

        def factory(key):
            client = FakeStreamingClient(api_key=key)
            clients.append(client)
            return client

        transcriber = AssemblyAITranscriber(
            api_key="key",
            aai_module=fake_aai,
            streaming_client_factory=factory,
            custom_vocabulary="Kubernetes, Splunk SOAR",
        )

        transcriber.start_stream()

        assert clients[0].connect_params.keyterms_prompt == [
            "Kubernetes",
            "Splunk SOAR",
        ]
        transcriber.abort_stream()

    def test_push_audio_chunk_forwards_data(self):
        """push_audio_chunk sends data to the streaming client."""
        t, clients = _make_streaming_transcriber()
        t.start_stream()
        t.push_audio_chunk(b"\x01\x00" * 160)
        client = clients[0]
        assert len(client.streamed_chunks) == 1
        assert client.streamed_chunks[0] == b"\x01\x00" * 160
        t.abort_stream()

    def test_stop_stream_returns_accumulated_text(self):
        """stop_stream returns all completed turns joined in order."""
        t, clients = _make_streaming_transcriber()
        t.start_stream()

        client = clients[0]
        client.emit_turn("Hello world.", turn_order=0, end_of_turn=True)
        client.emit_turn("How are you?", turn_order=1, end_of_turn=True)

        result = t.stop_stream()
        assert result == "Hello world. How are you?"
        assert client.terminated is True

    def test_stop_stream_includes_current_turn(self):
        """stop_stream includes the in-progress turn text."""
        t, clients = _make_streaming_transcriber()
        t.start_stream()

        client = clients[0]
        client.emit_turn("Hello.", turn_order=0, end_of_turn=True)
        client.emit_turn("How are", turn_order=1)

        result = t.stop_stream()
        assert result == "Hello. How are"

    def test_growing_turn_replaces_previous_text(self):
        """Growing transcripts of one turn replace the previous text."""
        t, clients = _make_streaming_transcriber()
        partials = []
        t.start_stream(on_partial=lambda text: partials.append(text))

        client = clients[0]
        client.emit_turn("Hel", turn_order=0)
        client.emit_turn("Hello wor", turn_order=0)
        client.emit_turn("Hello world", turn_order=0)

        assert len(partials) == 3
        assert partials[-1] == "Hello world"

        result = t.stop_stream()
        assert result == "Hello world"

    def test_formatted_turn_replaces_unformatted_text(self):
        """The formatted end-of-turn transcript replaces the raw turn text."""
        t, clients = _make_streaming_transcriber()
        t.start_stream()

        client = clients[0]
        client.emit_turn("hello world", turn_order=0, end_of_turn=True)
        client.emit_turn(
            "Hello world.",
            turn_order=0,
            end_of_turn=True,
            formatted=True,
        )

        result = t.stop_stream()
        # Should NOT duplicate: "Hello world." only once.
        assert result == "Hello world."

    def test_abort_stream_discards_text(self):
        """abort_stream closes the connection and discards all text."""
        t, clients = _make_streaming_transcriber()
        t.start_stream()

        client = clients[0]
        client.emit_turn("Some text", turn_order=0, end_of_turn=True)
        t.abort_stream()

        assert client.connected is False
        assert t._stream_turns == {}

    def test_on_partial_callback_receives_combined_text(self):
        """on_partial callback receives completed turns + current turn."""
        t, clients = _make_streaming_transcriber()
        received = []
        t.start_stream(on_partial=lambda text: received.append(text))

        client = clients[0]
        client.emit_turn("First sentence.", turn_order=0, end_of_turn=True)
        client.emit_turn("Second", turn_order=1)

        assert len(received) == 2
        assert received[0] == "First sentence."
        assert received[1] == "First sentence. Second"
        t.abort_stream()

    def test_stop_stream_terminates_session(self):
        """stop_stream terminates the streaming session."""
        t, clients = _make_streaming_transcriber()
        t.start_stream()
        client = clients[0]
        t.stop_stream()
        assert client.terminated is True
        assert client.connected is False

    def test_push_chunk_without_start_is_noop(self):
        """push_audio_chunk before start_stream fails clearly."""
        t, _clients = _make_streaming_transcriber()
        with pytest.raises(TranscriptionError, match="not active"):
            t.push_audio_chunk(b"\x00\x00" * 160)

    def test_on_error_callback_receives_runtime_error(self):
        t, clients = _make_streaming_transcriber()
        errors = []
        t.start_stream(on_error=errors.append)

        client = clients[0]
        client.emit_error(RuntimeError("WebSocket disconnected"))

        assert errors == ["AssemblyAI streaming failed: WebSocket disconnected"]
        t.abort_stream()

    def test_on_error_stores_error(self):
        """A streaming error is stored for later reporting."""
        t, clients = _make_streaming_transcriber()
        t.start_stream()

        clients[0].emit_error(RuntimeError("WebSocket disconnected"))

        # If no text was received, stop_stream should raise with the error.
        with pytest.raises(TranscriptionError, match="WebSocket disconnected"):
            t.stop_stream()

    def test_on_error_with_text_returns_text(self):
        """If text was received before an error, stop_stream returns it."""
        t, clients = _make_streaming_transcriber()
        t.start_stream()

        client = clients[0]
        client.emit_turn("Hello.", turn_order=0, end_of_turn=True)
        client.emit_error(RuntimeError("late error"))

        # Text was collected before the error → return it.
        result = t.stop_stream()
        assert result == "Hello."

    def test_empty_turn_transcript_ignored(self):
        """Empty turn transcripts are not recorded."""
        t, clients = _make_streaming_transcriber()
        t.start_stream()

        client = clients[0]
        client.emit_turn("", turn_order=0, end_of_turn=True)
        client.emit_turn("Hello.", turn_order=1, end_of_turn=True)

        result = t.stop_stream()
        assert result == "Hello."

    def test_connect_failure_raises_transcription_error(self):
        """Connection failure raises TranscriptionError."""
        fake_aai = _make_fake_aai()

        class FailingClient(FakeStreamingClient):
            def connect(self, params):
                raise ConnectionError("WebSocket refused")

        client = FailingClient(api_key="key")
        t = AssemblyAITranscriber(
            api_key="key",
            aai_module=fake_aai,
            streaming_client_factory=lambda _key: client,
        )
        with pytest.raises(TranscriptionError, match="failed to connect"):
            t.start_stream()
        assert t._stream_client is None
        assert client.terminated is True

    def test_connect_error_via_handler_raises(self):
        """Errors reported through the error handler during connect raise."""
        fake_aai = _make_fake_aai()

        class HandlerErrorClient(FakeStreamingClient):
            def connect(self, params):
                self.handlers["Error"](self, RuntimeError("Not Authorized"))

        t = AssemblyAITranscriber(
            api_key="key",
            aai_module=fake_aai,
            streaming_client_factory=lambda key: HandlerErrorClient(api_key=key),
        )
        with pytest.raises(TranscriptionError, match="Not Authorized"):
            t.start_stream()
        assert t._stream_client is None

    def test_old_client_callbacks_cannot_mutate_new_session(self):
        t, clients = _make_streaming_transcriber()
        old_partials: list[str] = []
        old_errors: list[str] = []
        t.start_stream(on_partial=old_partials.append, on_error=old_errors.append)
        old_client = clients[-1]
        t.abort_stream()

        new_partials: list[str] = []
        new_errors: list[str] = []
        t.start_stream(on_partial=new_partials.append, on_error=new_errors.append)
        new_client = clients[-1]

        old_client.emit_turn("stale text", turn_order=0)
        old_client.emit_error(RuntimeError("stale error"))

        assert new_partials == []
        assert new_errors == []
        new_client.emit_turn("current text", turn_order=0)
        assert new_partials == ["current text"]
        assert t.stop_stream() == "current text"

    def test_starting_session_blocks_reentry_and_abort_retires_client(self):
        fake_aai = _make_fake_aai()
        connect_entered = threading.Event()
        release_connect = threading.Event()

        class BarrierClient(FakeStreamingClient):
            def connect(self, params):
                connect_entered.set()
                assert release_connect.wait(timeout=2.0)
                super().connect(params)

        client = BarrierClient(api_key="key")
        t = AssemblyAITranscriber(
            api_key="key",
            aai_module=fake_aai,
            streaming_client_factory=lambda _key: client,
        )
        start_errors: list[Exception] = []

        def start() -> None:
            try:
                t.start_stream()
            except Exception as exc:
                start_errors.append(exc)

        worker = threading.Thread(target=start)
        worker.start()
        assert connect_entered.wait(timeout=1.0)

        with pytest.raises(TranscriptionError, match="already active"):
            t.start_stream()
        t.abort_stream()
        release_connect.set()
        worker.join(timeout=2.0)

        assert not worker.is_alive()
        assert len(start_errors) == 1
        assert "stopped while connecting" in str(start_errors[0])
        assert client.terminated is True
        assert t._stream_state == "idle"


# ---------------------------------------------------------------------------
# Tests: factory routing
# ---------------------------------------------------------------------------


class TestFactoryAssemblyAI:
    def test_factory_creates_assemblyai_transcriber(self):
        """create_transcriber routes engine='assemblyai' correctly."""
        from stt_app.settings_store import AppSettings
        from stt_app.transcriber.factory import create_transcriber

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
        from stt_app.settings_store import AppSettings
        from stt_app.transcriber.factory import create_transcriber

        # Without secret_store, api_key will be empty → TranscriptionError
        with pytest.raises(TranscriptionError, match="API key is missing"):
            settings = AppSettings(engine="assemblyai")
            create_transcriber(settings, secret_store=None)

    def test_factory_local_unchanged(self):
        """Local engine routing still works after factory changes."""
        from stt_app.transcriber.factory import create_transcriber
        from stt_app.transcriber.local_faster_whisper import (
            LocalFasterWhisperTranscriber,
        )
        from stt_app.settings_store import AppSettings

        settings = AppSettings(engine="local", model_size="small")
        t = create_transcriber(settings)
        assert isinstance(t, LocalFasterWhisperTranscriber)


# ---------------------------------------------------------------------------
# Tests: settings_store assemblyai key
# ---------------------------------------------------------------------------


class TestSettingsStoreAssemblyAI:
    def test_has_assemblyai_key_default_false(self):
        from stt_app.settings_store import AppSettings

        s = AppSettings()
        assert s.has_assemblyai_key is False

    def test_has_assemblyai_key_from_dict(self):
        from stt_app.settings_store import AppSettings

        s = AppSettings.from_dict({"has_assemblyai_key": True})
        assert s.has_assemblyai_key is True

    def test_assemblyai_in_valid_engines(self):
        from stt_app.config import VALID_ENGINES

        assert "assemblyai" in VALID_ENGINES

    def test_assemblyai_engine_validated(self):
        from stt_app.settings_store import AppSettings

        s = AppSettings.from_dict({"engine": "assemblyai"})
        assert s.engine == "assemblyai"
