"""Tests for AssemblyAI transcription provider."""

from __future__ import annotations

import io
import logging
import threading
import time
import types
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

    class FakeTranscriber:
        calls: list = []

        def upload_file(self, audio_file):
            FakeTranscriber.calls.append({"upload_file": audio_file})
            return "https://assemblyai.test/uploaded.wav"

        def submit(self, audio_file, config=None):
            FakeTranscriber.calls.append(
                {"audio_file": audio_file, "config": config}
            )
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
        # The real `_BaseStreamingClient.on` appends to a list per event; an
        # overwrite here would hide an accumulate-vs-replace regression.
        self.handlers.setdefault(getattr(event, "value", event), []).append(handler)

    def emit(self, key, *args):
        for handler in self.handlers[key]:
            handler(self, *args)

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
        self.emit("Turn", event)

    def emit_error(self, error):
        self.emit("Error", error)


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

            def submit(self, audio_file, config=None):
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
            def submit(self, audio_file, config=None):
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

        with (
            patch.dict("sys.modules", {"assemblyai": None}),
            pytest.raises(TranscriptionError, match=r"assemblyai.*not installed"),
        ):
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

    def test_batch_model_uses_only_universal_3_5_when_selected(self):
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(
            api_key="key",
            model="universal-3-5-pro",
            aai_module=fake_aai,
        )
        config = t._build_config()
        assert config.kwargs.get("speech_models") == ["universal-3-5-pro"]

    def test_legacy_batch_model_is_rejected(self):
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(
            api_key="key",
            model="nano",
            aai_module=fake_aai,
        )
        with pytest.raises(TranscriptionError, match="Unsupported AssemblyAI model"):
            t._build_config()

    def test_custom_vocabulary_sets_keyterms_prompt(self):
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(
            api_key="key",
            aai_module=fake_aai,
            custom_vocabulary="Kubernetes, Splunk SOAR",
        )
        config = t._build_config()
        assert config.kwargs.get("keyterms_prompt") == [
            "Kubernetes",
            "Splunk SOAR",
        ]
        assert "word_boost" not in config.kwargs

    def test_empty_custom_vocabulary_omits_keyterms_prompt(self):
        fake_aai = _make_fake_aai()
        t = AssemblyAITranscriber(api_key="key", aai_module=fake_aai)
        config = t._build_config()
        assert "keyterms_prompt" not in config.kwargs

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
        assert fake_aai.Transcriber.calls[1]["audio_file"] == (
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
        assert str(params.speech_model) == "universal-3-5-pro"
        assert params.language_detection is None
        assert params.format_turns is None
        t.abort_stream()

    def test_start_stream_passes_custom_vocabulary_as_u3_5_keyterms(self):
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

    def test_a_stop_that_raises_still_frees_the_provider(self, monkeypatch):
        """`_shutdown_streaming_client` bounds the SDK disconnect with a helper
        thread, so `Thread.start()` alone can raise between marking the session
        `retiring` and resetting it. The reset was then skipped, and
        `start_stream` refuses anything that is not `idle` -- so every later
        dictation failed with "Streaming session already active" for the life
        of the app, with the remote session still open and billed.
        """
        t, _clients = _make_streaming_transcriber()
        t.start_stream()

        def _cannot_start_a_thread(*_args, **_kwargs):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(t, "_shutdown_streaming_client", _cannot_start_a_thread)

        with pytest.raises(RuntimeError, match="new thread"):
            t.stop_stream()

        assert t._stream_state == "idle", (
            "the provider was left retiring and refuses every later dictation"
        )
        monkeypatch.undo()
        t.start_stream()
        t.abort_stream()

    def test_an_abort_that_raises_still_frees_the_provider(self, monkeypatch):
        """The abort path marks `retiring` too, and had the same gap."""
        t, _clients = _make_streaming_transcriber()
        t.start_stream()

        def _cannot_start_a_thread(*_args, **_kwargs):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(t, "_shutdown_streaming_client", _cannot_start_a_thread)

        with pytest.raises(RuntimeError, match="new thread"):
            t.abort_stream()

        assert t._stream_state == "idle"
        monkeypatch.undo()
        t.start_stream()
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
                self.emit("Error", RuntimeError("Not Authorized"))

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


    def test_a_stop_during_the_handshake_retires_the_session(self):
        """Refusing the stop is not enough; the session has to be retired.

        `stop_stream` raised for a session that was still connecting and
        changed nothing else. The handshake then finished, found the state
        still "starting", and published the session as active -- owned by
        nobody, because the caller had already been told the stop failed and
        had torn its own state down. Every later dictation was then refused
        with "Streaming session already active" for the rest of the app's
        life, and the remote socket stayed open and billed.

        `abort_stream` always handled this and has its own test; `stop_stream`
        is the path the controller actually takes when
        `_await_stream_connect` times out, and it was the untested one.
        """
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
        runtime_errors: list[str] = []
        start_errors: list[Exception] = []

        def start() -> None:
            try:
                t.start_stream(on_error=runtime_errors.append)
            except Exception as exc:
                start_errors.append(exc)

        worker = threading.Thread(target=start)
        worker.start()
        assert connect_entered.wait(timeout=1.0)

        with pytest.raises(TranscriptionError, match="not active"):
            t.stop_stream()

        release_connect.set()
        worker.join(timeout=2.0)
        assert not worker.is_alive()

        assert len(start_errors) == 1
        assert "stopped while connecting" in str(start_errors[0])
        assert client.terminated is True, (
            "the handshake published a client that nobody owns"
        )
        assert t._stream_state == "idle", (
            "the session was left mid-flight, so every later dictation is "
            "refused with 'already active'"
        )
        assert runtime_errors == [], (
            f"a refused stop reported a runtime failure: {runtime_errors}"
        )


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
        from stt_app.settings_store import AppSettings
        from stt_app.transcriber.factory import create_transcriber
        from stt_app.transcriber.local_faster_whisper import (
            LocalFasterWhisperTranscriber,
        )

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


def test_the_connection_test_uses_an_ssl_context_like_every_other_provider(
    monkeypatch,
):
    """Without one, a TLS-intercepting proxy failed a key that was fine.

    Transcription goes through the SDK, i.e. `requests`, which reads
    REQUESTS_CA_BUNDLE; this call goes through urllib, which does not. So
    behind a corporate proxy the Remote tab reported a broken key while
    dictation with that same key worked -- and its advice named only
    REQUESTS_CA_BUNDLE, which could not fix what had just failed.
    """
    import urllib.request

    from stt_app.transcriber import assemblyai_provider as provider

    seen: dict[str, object] = {}
    sentinel = object()

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def _fake_urlopen(req, timeout=None, context=None):
        seen["context"] = context
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(provider, "create_ssl_context", lambda: sentinel)
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    ok, message = AssemblyAITranscriber(
        api_key="key", aai_module=_make_fake_aai()
    ).test_connection()

    assert ok is True, message
    assert seen["context"] is sentinel, "the connection test ran without TLS setup"


def test_a_failed_connection_test_reports_what_the_api_said(monkeypatch):
    """`exc.reason` is the status phrase; the body says which key is wrong."""
    import urllib.error
    import urllib.request

    from stt_app.transcriber import assemblyai_provider as provider

    def _fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(
            "https://api.assemblyai.com/v2/transcript",
            402,
            "Payment Required",
            {},
            io.BytesIO(b'{"error": "This account has run out of credits"}'),
        )

    monkeypatch.setattr(provider, "create_ssl_context", lambda: None)
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    ok, message = AssemblyAITranscriber(
        api_key="key", aai_module=_make_fake_aai()
    ).test_connection()

    assert ok is False
    assert "run out of credits" in message, message


def _stream_log_records(caplog, module_suffix):
    name = f"stt_app.transcriber.{module_suffix}"
    return [r for r in caplog.records if r.name == name]


def test_a_partial_callback_that_raises_is_logged_once_per_session(caplog):
    """A dead live-insertion path must not look like a user who stopped talking.

    The callback is what puts live text on screen and into the document, so a
    bare `pass` made a broken one indistinguishable from silence. It stays
    swallowed -- the next turn carries the whole combined text again -- and it
    stays latched, because it runs on every turn revision.
    """
    t, clients = _make_streaming_transcriber()

    def _broken(_text):
        raise RuntimeError("the controller slot is gone")

    with caplog.at_level(logging.DEBUG):
        t.start_stream(on_partial=_broken)
        client = clients[0]
        client.emit_turn("Hello", turn_order=0)
        client.emit_turn("Hello world", turn_order=0)
        client.emit_turn("Hello world.", turn_order=0, end_of_turn=True)

        first = _stream_log_records(caplog, "assemblyai_provider")
        assert len(first) == 1, [r.getMessage() for r in first]
        assert "partial callback failed" in first[0].getMessage()
        assert first[0].exc_info is not None, "the traceback was dropped"

        # Swallowed, so the session is unharmed.
        assert t.stop_stream() == "Hello world."

        # A new session logs again: the latch is per session, not per
        # process. `_reset_stream_state_locked` is what clears it, so the
        # identical re-initialization in `start_stream` is redundant --
        # every write of `_stream_state = "idle"` is in `__init__` or that
        # reset, and `start_stream` refuses to run in any other state. It
        # is kept because the five neighbouring assignments are redundant
        # for the same reason; removing only this one would read as a
        # difference that is not there.
        t.start_stream(on_partial=_broken)
        clients[1].emit_turn("Again", turn_order=0)

    assert len(_stream_log_records(caplog, "assemblyai_provider")) == 2


def test_an_error_callback_that_raises_is_logged(caplog):
    """That callback is the only path a stream failure takes to the user."""
    t, clients = _make_streaming_transcriber()

    def _broken(_message):
        raise RuntimeError("the controller slot is gone")

    with caplog.at_level(logging.DEBUG):
        t.start_stream(on_error=_broken)
        clients[0].emit_error(RuntimeError("socket died"))

    records = _stream_log_records(caplog, "assemblyai_provider")
    assert len(records) == 1, [r.getMessage() for r in records]
    assert "error callback failed" in records[0].getMessage()


class _PendingTranscript:
    """A job AssemblyAI has accepted but not finished."""

    def __init__(self, status="queued", transcript_id="t-123"):
        self.status = status
        self.id = transcript_id
        self.error = None
        self.text = None


def _pending_aai(*, statuses, transcript_id="t-123"):
    """A fake module whose status fetch walks `statuses`, then stays queued.

    The fetch is modelled the way the real SDK models it: one HTTP call
    (`api.get_transcript`) wrapped back into a transcript by
    `Transcript.from_response`. `get_by_id` is present and poisoned, because
    in the real SDK it is `wait_for_completion()` -- an unbounded
    `while True:` -- so a caller that reaches for it has put this loop's
    deadline around the very wait it exists to replace.
    """
    aai = _make_fake_aai()
    remaining = list(statuses)
    fetched: list[str] = []

    class _SubmittingTranscriber:
        def submit(self, audio_file, config=None):
            return _PendingTranscript(transcript_id=transcript_id)

        def upload_file(self, audio_file):
            return "https://assemblyai.test/uploaded.wav"

    class _HttpClient:
        pass

    class _Client:
        http_client = _HttpClient()

        @staticmethod
        def get_default():
            return _Client

    def _get_transcript(http_client, tid):
        assert http_client is _Client.http_client, http_client
        fetched.append(tid)
        if len(fetched) > _FAKE_MAX_FETCHES:
            raise AssertionError(
                f"the poll loop fetched {len(fetched)} times without finishing"
            )
        status = remaining.pop(0) if remaining else "queued"
        out = _PendingTranscript(status=status, transcript_id=tid)
        if status == "completed":
            out.text = "at last"
        return out

    class _TranscriptClass:
        @staticmethod
        def from_response(*, client, response):
            assert client is _Client, client
            return response

        @staticmethod
        def get_by_id(tid):
            raise AssertionError(
                "get_by_id is the SDK's own unbounded wait_for_completion, "
                "not a status fetch -- calling it puts the deadline around "
                "the wait instead of replacing it"
            )

    aai.Transcriber = _SubmittingTranscriber
    aai.Transcript = _TranscriptClass
    aai.Client = _Client
    aai.api = types.SimpleNamespace(get_transcript=_get_transcript)
    return aai, fetched


# A poll loop that has lost its deadline sleeps `min(interval, max(deadline -
# now, 0.0))`, which is 0.0 once the budget is spent -- so the fake clock stops
# advancing and the loop spins forever. A wall-clock cap cannot see that; a
# call cap can, and it turns "the bound is gone" into a failure naming itself
# rather than a run that hangs with no output.
# The interval is slept in `ASSEMBLYAI_SHUTDOWN_POLL_S` slices so a quit can
# end it; a full thirty-minute budget at the 3 s default interval is 600
# intervals of 6 slices. The fetch cap is the guard that cannot be starved.
_FAKE_CLOCK_MAX_SLEEPS = 20000
# And a cap on the operation that always runs: a mutation that deletes the
# sleep makes the sleep-count guard unreachable and hangs pytest instead of
# failing it (measured: 23.5 million iterations in 2 s, guard never fired).
_FAKE_MAX_FETCHES = 5000


def _fake_clock(monkeypatch, provider):
    """Make the poll loop deterministic and instant."""
    now = [0.0]
    sleeps = [0]

    def _sleep(seconds):
        sleeps[0] += 1
        if sleeps[0] > _FAKE_CLOCK_MAX_SLEEPS:
            raise AssertionError(
                f"the poll loop slept {sleeps[0]} times without finishing -- "
                "it is not bounded by its deadline any more"
            )
        now[0] += seconds

    monkeypatch.setattr(provider.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(provider.time, "sleep", _sleep)
    return now


def test_a_job_that_never_finishes_gives_the_worker_back(monkeypatch):
    """The SDK's `wait_for_completion` is `while True:` with no bound at all.

    A job AssemblyAI leaves in `queued` therefore held the app's single
    transcription worker for the rest of the session -- and blocked process
    exit with it, because `ThreadPoolExecutor` joins its workers from an atexit
    hook and `shutdown(wait=False, cancel_futures=True)` does not release one
    that is already running (measured: the interpreter never exits).
    """
    from stt_app.transcriber import assemblyai_provider as provider

    aai, fetched = _pending_aai(statuses=[])
    monkeypatch.setattr(provider, "ASSEMBLYAI_BATCH_MAX_WAIT_S", 10.0)
    now = _fake_clock(monkeypatch, provider)

    t = AssemblyAITranscriber(api_key="key", aai_module=aai)
    with pytest.raises(TranscriptionError, match="did not finish"):
        t.transcribe_batch(b"RIFF....WAVE")

    assert fetched, "it gave up without ever polling"
    assert now[0] == pytest.approx(10.0), "the budget was over- or under-spent"


def test_the_timeout_names_the_transcript_so_it_can_be_fetched_later(monkeypatch):
    from stt_app.transcriber import assemblyai_provider as provider

    aai, _fetched = _pending_aai(statuses=[], transcript_id="t-abc999")
    monkeypatch.setattr(provider, "ASSEMBLYAI_BATCH_MAX_WAIT_S", 6.0)
    _fake_clock(monkeypatch, provider)

    t = AssemblyAITranscriber(api_key="key", aai_module=aai)
    with pytest.raises(TranscriptionError) as excinfo:
        t.transcribe_batch(b"RIFF....WAVE")

    assert "t-abc999" in str(excinfo.value), str(excinfo.value)


def test_a_slow_job_is_still_delivered(monkeypatch):
    """The bound must not turn a slow job into a failure."""
    from stt_app.transcriber import assemblyai_provider as provider

    aai, fetched = _pending_aai(statuses=["processing", "processing", "completed"])
    monkeypatch.setattr(provider, "ASSEMBLYAI_BATCH_MAX_WAIT_S", 60.0)
    _fake_clock(monkeypatch, provider)

    t = AssemblyAITranscriber(api_key="key", aai_module=aai)

    assert t.transcribe_batch(b"RIFF....WAVE") == "at last"
    assert len(fetched) == 3


def test_a_job_with_no_id_fails_at_once_instead_of_polling_nothing(monkeypatch):
    from stt_app.transcriber import assemblyai_provider as provider

    aai, fetched = _pending_aai(statuses=[], transcript_id="")
    monkeypatch.setattr(provider, "ASSEMBLYAI_BATCH_MAX_WAIT_S", 600.0)
    _fake_clock(monkeypatch, provider)

    t = AssemblyAITranscriber(api_key="key", aai_module=aai)
    with pytest.raises(TranscriptionError, match="no transcript id"):
        t.transcribe_batch(b"RIFF....WAVE")

    assert fetched == [], "it polled with an empty id"


def test_an_already_finished_job_is_never_polled_again(monkeypatch):
    """The common case must cost no extra request."""
    from stt_app.transcriber import assemblyai_provider as provider

    aai, fetched = _pending_aai(statuses=[])

    class _ImmediateTranscriber:
        def submit(self, audio_file, config=None):
            done = _PendingTranscript(status="completed")
            done.text = "hello world"
            return done

        def upload_file(self, audio_file):
            return "https://assemblyai.test/uploaded.wav"

    aai.Transcriber = _ImmediateTranscriber
    _fake_clock(monkeypatch, provider)

    t = AssemblyAITranscriber(api_key="key", aai_module=aai)

    assert t.transcribe_batch(b"RIFF....WAVE") == "hello world"
    assert fetched == []


def test_a_disconnect_thread_that_cannot_start_does_not_break_teardown(
    monkeypatch, caplog
):
    """Every caller is a teardown path with state to retire below it."""
    import logging as _logging
    import threading as _threading

    class _RefusingThread(_threading.Thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(_threading, "Thread", _RefusingThread)

    t, _clients = _make_streaming_transcriber()
    t._stream_client = None

    with caplog.at_level(_logging.DEBUG):
        # Must not raise: the callers have a lease to release afterwards.
        t._shutdown_streaming_client(object(), join_timeout_s=0.1)

    records = _stream_log_records(caplog, "assemblyai_provider")
    assert any("disconnect thread" in r.getMessage() for r in records), [
        r.getMessage() for r in records
    ]


def test_a_stream_worker_that_cannot_start_leaves_no_session_behind(monkeypatch):
    """`_stream_active` was set inside the lock before the thread started.

    A thread that cannot start therefore left it True for the process
    lifetime: every later dictation was refused with "Streaming session
    already active", and `abort_stream` raised "cannot join thread before it
    is started" instead of clearing it.
    """
    import threading as _threading

    from stt_app.transcriber.local_faster_whisper import LocalFasterWhisperTranscriber

    t = LocalFasterWhisperTranscriber(model_size="tiny")
    real_thread = _threading.Thread

    class _RefusingThread(real_thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(_threading, "Thread", _RefusingThread)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        t.start_stream()

    assert t._stream_active is False
    assert t._stream_session is None
    assert t._stream_thread is None

    monkeypatch.setattr(_threading, "Thread", real_thread)
    # The session slot is free again, so a retry is accepted rather than
    # refused with "Streaming session already active".
    t.start_stream()
    t.abort_stream()


def test_a_nemotron_worker_that_cannot_start_leaves_no_session_behind(monkeypatch):
    import threading as _threading

    from stt_app.transcriber.local_nemotron import LocalNemotronTranscriber

    t = LocalNemotronTranscriber()
    real_thread = _threading.Thread

    class _RefusingThread(real_thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(_threading, "Thread", _RefusingThread)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        t.start_stream()

    assert t._stream_active is False
    assert t._stream_run is None
    assert t._stream_thread is None
    assert t._stream_workers == {}, "the retired worker entry leaked"


def test_the_status_fetch_uses_names_the_installed_sdk_actually_has():
    """The fake models three SDK names; if the SDK drops one, it hides it.

    `_fetch_transcript` deliberately steps around `Transcript.get_by_id`, so
    every batch dictation now depends on `Client.get_default`,
    `api.get_transcript` and `Transcript.from_response`. An SDK upgrade that
    renames any of them breaks batch transcription outright, and a fake
    module cannot notice.
    """
    import assemblyai as real_aai
    import assemblyai.api as real_api

    assert callable(real_aai.Client.get_default)
    assert callable(real_api.get_transcript)
    assert callable(real_aai.Transcript.from_response)
    assert real_aai.api is real_api, "aai.api is how the provider reaches it"


def test_a_status_fetch_returns_without_waiting_for_the_job(monkeypatch):
    """One HTTP call, then control comes back -- against the real SDK.

    Only `api.get_transcript` is stubbed. With `get_by_id` the same setup
    never returned: measured at 46 status polls inside a single call, still
    blocked after 12.0 s against a 3.0 s budget, because `get_by_id` is
    `wait_for_completion()`.
    """
    import assemblyai as real_aai
    import assemblyai.api as real_api
    from assemblyai import types as real_types

    monkeypatch.setattr(real_aai.settings, "api_key", "key-for-a-local-test")
    # A regression here is an endless wait, not a wrong answer, so the stub
    # refuses the second poll: the test then fails in milliseconds instead of
    # hanging the run with no output naming the cause.
    monkeypatch.setattr(real_aai.settings, "polling_interval", 0.01)
    calls: list[str] = []

    def _one_poll(http_client, transcript_id):
        calls.append(transcript_id)
        if len(calls) > 1:
            raise AssertionError(
                "the fetch polled twice, i.e. it is waiting for the job "
                "instead of reading its status once"
            )
        return real_types.TranscriptResponse(
            id=transcript_id,
            status=real_types.TranscriptStatus.queued,
            audio_url="http://assemblyai.test/a.wav",
        )

    monkeypatch.setattr(real_api, "get_transcript", _one_poll)

    fetched = AssemblyAITranscriber._fetch_transcript(real_aai, "t-real-sdk")

    assert calls == ["t-real-sdk"], "it polled more than once, i.e. it waited"
    assert fetched.status == real_types.TranscriptStatus.queued
    assert fetched.id == "t-real-sdk"
    # `api.get_transcript` hands back a `TranscriptResponse`, not a
    # `Transcript`. Both carry status/text/error, so returning the raw one
    # works right up until it does not -- and `_wait_for_transcript`'s early
    # return hands back the `Transcript` that `submit` produced, so skipping
    # `from_response` makes one function return two different types.
    assert isinstance(fetched, real_aai.Transcript), type(fetched)


def test_a_finished_job_is_not_made_to_wait_for_the_polling_interval(monkeypatch):
    """Ask first, sleep between asks -- not the other way round.

    `submit(poll=False)` always reports `queued`, so the early return above
    the loop never fires and every batch dictation enters it. Sleeping at the
    top of the loop therefore spent a full polling interval before the first
    question was ever asked, on every single dictation -- measured at 3.27 s
    for a job the service had already finished.
    """
    from stt_app.transcriber import assemblyai_provider as provider

    aai, fetched = _pending_aai(statuses=["completed"])
    monkeypatch.setattr(provider, "ASSEMBLYAI_BATCH_MAX_WAIT_S", 60.0)
    now = _fake_clock(monkeypatch, provider)

    t = AssemblyAITranscriber(api_key="key", aai_module=aai)

    assert t.transcribe_batch(b"RIFF....WAVE") == "at last"
    assert len(fetched) == 1
    assert now[0] == 0.0, f"it slept {now[0]}s before asking whether it was done"


class TestStreamStopBudget:
    """The app's stop bound has to contain the SDK's text-bearing teardown, not equal it.

    "Text-bearing" is the terminate wait plus the thread joins. The websocket
    close that follows can take `websockets`' 10 s `close_timeout` on a dead
    peer, and the bound deliberately does not contain that: nothing is
    dispatched once the read thread is joined, and the turn is stored before
    the joins begin.
    """

    def test_the_outer_bound_clears_the_sdk_teardown_it_contains(self):
        from stt_app.transcriber import assemblyai_provider as provider

        floor = (
            provider.ASSEMBLYAI_STREAM_TERMINATE_TIMEOUT_S
            + 2 * provider.ASSEMBLYAI_SDK_THREAD_LOOP_S
        )

        assert floor < provider.ASSEMBLYAI_STREAM_STOP_JOIN_TIMEOUT_S, (
            "the join gives up while `disconnect(terminate=True)` is still "
            "waiting for the server's final Turn"
        )

    def test_the_sdk_still_leaves_the_websocket_close_timeout_to_the_library(self):
        """The comment above rests on this: if the SDK ever bounds the close
        itself, the worst case changes and the model here must be re-derived."""
        import inspect

        from assemblyai.streaming.v3 import client as sdk_client

        source = inspect.getsource(sdk_client)
        assert "websocket_connect(" in source
        assert "close_timeout" not in source

    def test_the_terminate_timeout_is_pinned_on_the_real_options(self, monkeypatch):
        """Inheriting the SDK's default puts the two numbers back together.

        Driven through the provider's own construction, against the real
        `StreamingClientOptions`: asserting on an options object the test
        built itself would pass whether or not the provider passes anything.
        """
        import assemblyai.streaming.v3 as sdk

        from stt_app.transcriber import assemblyai_provider as provider

        built: list = []

        class _CapturingClient:
            def __init__(self, options):
                built.append(options)
                self.handlers: dict = {}

            def on(self, event, handler):
                self.handlers[getattr(event, "value", event)] = handler

            def connect(self, params):
                pass

            def disconnect(self, terminate=False):
                pass

        monkeypatch.setattr(sdk, "StreamingClient", _CapturingClient)
        # A value the SDK's own default is not: the shipped constant happens
        # to equal that default, so comparing against it cannot tell "pinned"
        # apart from "inherited" -- which is exactly what this test is for.
        monkeypatch.setattr(
            provider, "ASSEMBLYAI_STREAM_TERMINATE_TIMEOUT_S", 3.25
        )
        assert sdk.StreamingClientOptions(api_key="k").terminate_timeout != 3.25

        t = AssemblyAITranscriber(api_key="key", aai_module=_make_fake_aai())
        t.start_stream()
        try:
            assert len(built) == 1
            assert isinstance(built[0], sdk.StreamingClientOptions)
            assert built[0].terminate_timeout == 3.25
        finally:
            t.abort_stream()

    def test_a_final_turn_during_the_sdk_teardown_is_still_delivered(
        self, monkeypatch
    ):
        """The turn the old bound dropped, with no error to show for it.

        The server sends the last Turn after Terminate, i.e. inside the window
        `disconnect(terminate=True)` is waiting through -- so a bound that
        expires first resets the session and `_on_turn_event` discards it.
        """
        from stt_app.transcriber import assemblyai_provider as provider

        monkeypatch.setattr(
            provider, "ASSEMBLYAI_STREAM_TERMINATE_TIMEOUT_S", 0.05
        )
        monkeypatch.setattr(provider, "ASSEMBLYAI_SDK_THREAD_LOOP_S", 0.01)
        monkeypatch.setattr(
            provider, "ASSEMBLYAI_STREAM_STOP_JOIN_TIMEOUT_S", 0.40
        )

        t, clients = _make_streaming_transcriber()
        t.start_stream()
        client = clients[0]
        client.emit_turn("das war der anfang", turn_order=0, end_of_turn=True)

        def _slow_disconnect(terminate=False):
            # The SDK dispatches the server's last Turn from its read thread
            # while `disconnect` is still inside its terminate wait.
            time.sleep(0.10)
            client.emit_turn("und das war das ende", turn_order=1, end_of_turn=True)
            client.terminated = bool(terminate)
            client.connected = False

        client.disconnect = _slow_disconnect

        text = t.stop_stream()

        assert "das war der anfang" in text, text
        assert "und das war das ende" in text, text

    def test_the_stop_join_actually_uses_the_constant(self, monkeypatch):
        """A literal at the call site ignores the budget this class defines.

        Pinned by making the disconnect outlast the bound: with the constant
        honoured `stop_stream` gives up at it, with a literal 5.0 it does not.
        """
        from stt_app.transcriber import assemblyai_provider as provider

        monkeypatch.setattr(
            provider, "ASSEMBLYAI_STREAM_STOP_JOIN_TIMEOUT_S", 0.15
        )

        t, clients = _make_streaming_transcriber()
        t.start_stream()
        client = clients[0]
        client.emit_turn("etwas gesagt", turn_order=0, end_of_turn=True)

        released = threading.Event()

        def _hanging_disconnect(terminate=False):
            released.wait(5.0)

        client.disconnect = _hanging_disconnect
        started = time.monotonic()
        try:
            t.stop_stream()
            elapsed = time.monotonic() - started
        finally:
            released.set()

        assert elapsed < 1.0, f"the join ignored its budget: {elapsed:.2f}s"


class TestTransientFetchFailures:
    """One failed status fetch must not abort a job the service will finish."""

    def test_a_transient_fetch_error_is_retried_within_the_budget(self, monkeypatch):
        from stt_app.transcriber import assemblyai_provider as provider

        aai, fetched = _pending_aai(statuses=["queued", "processing", "completed"])
        real_get = aai.api.get_transcript
        blips = [TimeoutError("Read timed out")]

        def _flaky(http_client, tid):
            if blips:
                fetched.append(tid)
                raise blips.pop()
            return real_get(http_client, tid)

        aai.api.get_transcript = _flaky
        _fake_clock(monkeypatch, provider)
        t = AssemblyAITranscriber(api_key="key", aai_module=aai)

        assert t.transcribe_batch(b"RIFF....WAVE") == "at last"
        assert len(fetched) == 4

    def test_persistent_fetch_errors_fail_naming_the_transcript_id(self, monkeypatch):
        from stt_app.transcriber import assemblyai_provider as provider

        aai, fetched = _pending_aai(statuses=[], transcript_id="tid-later-999")

        def _always_fails(http_client, tid):
            fetched.append(tid)
            raise TimeoutError("Read timed out")

        aai.api.get_transcript = _always_fails
        _fake_clock(monkeypatch, provider)
        t = AssemblyAITranscriber(api_key="key", aai_module=aai)

        with pytest.raises(TranscriptionError) as excinfo:
            t.transcribe_batch(b"RIFF....WAVE")

        message = str(excinfo.value)
        assert "tid-later-999" in message
        assert "Read timed out" in message
        assert len(fetched) == provider.ASSEMBLYAI_MAX_CONSECUTIVE_FETCH_FAILURES

    def test_a_failure_count_is_reset_by_a_successful_fetch(self, monkeypatch):
        from stt_app.transcriber import assemblyai_provider as provider

        limit = provider.ASSEMBLYAI_MAX_CONSECUTIVE_FETCH_FAILURES
        statuses = []
        for _ in range(3):
            statuses.extend(["queued"] * (limit - 1) + ["processing"])
        statuses.append("completed")
        aai, fetched = _pending_aai(statuses=list(statuses))
        real_get = aai.api.get_transcript
        # Fail every fetch except one in `limit`, so a count that is never
        # reset crosses the limit while a reset one never does.
        calls = [0]

        def _mostly_failing(http_client, tid):
            calls[0] += 1
            if calls[0] % limit != 0:
                fetched.append(tid)
                raise ConnectionError("blip")
            return real_get(http_client, tid)

        aai.api.get_transcript = _mostly_failing
        _fake_clock(monkeypatch, provider)
        t = AssemblyAITranscriber(api_key="key", aai_module=aai)

        assert t.transcribe_batch(b"RIFF....WAVE") == "at last"


class TestSetupFailuresAreWrapped:
    def test_a_broken_sdk_module_surfaces_as_a_transcription_error(self, monkeypatch):
        """`_configure` ran before the try; an odd SDK escaped as an AttributeError."""

        aai = types.SimpleNamespace()  # no `settings`, no `Transcriber`
        t = AssemblyAITranscriber(api_key="key", aai_module=aai)

        with pytest.raises(TranscriptionError, match="AssemblyAI"):
            t.transcribe_batch(b"RIFF....WAVE")


def test_a_shutdown_ends_the_poll_within_one_slice(monkeypatch):
    """`executor.shutdown(wait=False, cancel_futures=True)` does not stop a
    running worker, and the executor's exit handler joins it -- so a job the
    service leaves queued kept the process alive for the whole thirty-minute
    budget after the user quit, still holding the single-instance lock. The
    poll now reads the app-wide shutdown flag between slices of its sleep."""
    from stt_app.transcriber import assemblyai_provider as provider
    from stt_app.transcriber.base import (
        request_transcription_shutdown,
        reset_transcription_shutdown_for_tests,
    )

    aai, fetched = _pending_aai(statuses=["queued"])
    aai.settings.polling_interval = 3.0
    now = [0.0]

    def _sleep(seconds):
        now[0] += seconds
        # The quit lands during the first slice of the first interval.
        request_transcription_shutdown()

    monkeypatch.setattr(provider.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(provider.time, "sleep", _sleep)
    t = AssemblyAITranscriber(api_key="key", aai_module=aai)
    try:
        with pytest.raises(TranscriptionError) as excinfo:
            t.transcribe_batch(b"RIFF....WAVE")
    finally:
        reset_transcription_shutdown_for_tests()

    assert len(fetched) == 1, "it fetched again after the shutdown"
    assert now[0] <= provider.ASSEMBLYAI_SHUTDOWN_POLL_S
    message = str(excinfo.value)
    assert "shutting down" in message
    assert "t-123" in message, "the id is the only way to recover the job"


def test_a_fetch_result_without_a_status_is_a_fetch_failure(monkeypatch):
    """A response the SDK wraps into an object with no `status` raised
    `AttributeError` outside the retry arm: one attempt, no retry, and a
    message without the transcript id."""
    from stt_app.transcriber import assemblyai_provider as provider

    aai, fetched = _pending_aai(statuses=["queued"])
    original = aai.Transcript.from_response

    class _NoStatus:
        text = None

    aai.Transcript.from_response = staticmethod(
        lambda *, client, response: _NoStatus()
    )
    _fake_clock(monkeypatch, provider)
    t = AssemblyAITranscriber(api_key="key", aai_module=aai)
    try:
        with pytest.raises(TranscriptionError) as excinfo:
            t.transcribe_batch(b"RIFF....WAVE")
    finally:
        aai.Transcript.from_response = original

    assert len(fetched) == provider.ASSEMBLYAI_MAX_CONSECUTIVE_FETCH_FAILURES
    message = str(excinfo.value)
    assert "could not fetch" in message
    assert "t-123" in message


def test_the_loop_sleeps_one_polling_interval_between_fetches(monkeypatch):
    """Between two non-terminal fetches it sleeps `polling_interval`, once.

    Pinned because the fake-clock guard sits inside the patched sleep: a loop
    that stops sleeping makes that guard unreachable, and only the fetch cap
    then turns the hang into a failure. Measured with the sleep deleted:
    23.5 million iterations in 2 s and the sleep guard never fired.
    """
    from stt_app.transcriber import assemblyai_provider as provider

    aai, fetched = _pending_aai(statuses=["queued", "processing", "completed"])
    aai.settings.polling_interval = 2.0
    now = _fake_clock(monkeypatch, provider)
    t = AssemblyAITranscriber(api_key="key", aai_module=aai)

    assert t.transcribe_batch(b"RIFF....WAVE") == "at last"
    assert len(fetched) == 3
    assert now[0] == 4.0, "two non-terminal fetches, one interval after each"
