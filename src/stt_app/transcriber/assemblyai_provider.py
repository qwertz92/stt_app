"""AssemblyAI remote transcription provider.

Batch transcription via the AssemblyAI Python SDK.
Real-time streaming via AssemblyAI's Universal-Streaming (v3) WebSocket API
(``assemblyai.streaming.v3.StreamingClient``); the legacy v2
``RealtimeTranscriber`` API has been retired by AssemblyAI.
Requires: pip install assemblyai
API key stored via keyring (settings_dialog / secret_store).

The batch provider uses Universal-3-Pro + Universal-2 speech models with
automatic language detection enabled.
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from ..app_paths import temp_audio_dir
from ..config import (
    AUDIO_SAMPLE_RATE,
    DEFAULT_ASSEMBLYAI_MODEL,
    DOC_SSL_PROXY_PATH,
    language_modes_for_selection,
)
from ..ssl_utils import is_ssl_error as _is_ssl_error
from .base import (
    AudioInput,
    ITranscriber,
    ProgressReporter,
    StreamingCallback,
    StreamingErrorCallback,
    TranscriptionError,
)


def _default_assemblyai():
    """Lazy import to avoid hard dependency at module level."""
    try:
        import assemblyai as aai  # type: ignore

        return aai
    except ImportError:
        raise TranscriptionError(
            "The 'assemblyai' package is not installed. "
            "Install it with: pip install assemblyai  "
            "(or: uv add assemblyai)"
        ) from None


class AssemblyAITranscriber(ProgressReporter, ITranscriber):
    """Batch transcription using AssemblyAI's REST API via the official SDK.

    Parameters
    ----------
    api_key : str
        AssemblyAI API key (required).
    language_mode : str
        ``"auto"`` for automatic language detection,
        or a language code like ``"de"`` / ``"en"``.
    aai_module :
        Injected ``assemblyai`` module (for testing).
    """

    def __init__(
        self,
        api_key: str,
        language_mode: str = "auto",
        model: str = DEFAULT_ASSEMBLYAI_MODEL,
        *,
        aai_module=None,
        streaming_client_factory=None,
    ) -> None:
        ProgressReporter.__init__(self)
        if not api_key:
            raise TranscriptionError(
                "AssemblyAI API key is missing. "
                "Enter your key in Settings → Remote Provider API Keys."
            )
        self._api_key = api_key
        self._language_mode = (language_mode or "auto").strip().lower()
        self._model = (model or DEFAULT_ASSEMBLYAI_MODEL).strip().lower()
        self._aai = aai_module  # None → lazy import on first use
        self._streaming_client_factory = streaming_client_factory
        self._stream_lock = threading.Lock()
        self._stream_client = None
        self._stream_on_partial: StreamingCallback | None = None
        self._stream_on_error: StreamingErrorCallback | None = None
        self._stream_turns: dict[int, str] = {}
        self._stream_error: Exception | None = None
        self._stream_error_reported = False

    def _get_aai(self):
        if self._aai is None:
            self._aai = _default_assemblyai()
        return self._aai

    def _configure(self):
        """Set API key on the assemblyai global settings."""
        aai = self._get_aai()
        aai.settings.api_key = self._api_key

    def _build_config(self):
        """Build a TranscriptionConfig for the current language mode."""
        aai = self._get_aai()

        kwargs: dict = {}
        selected_model = self._model or DEFAULT_ASSEMBLYAI_MODEL
        kwargs["speech_models"] = self._speech_models_for_selection(selected_model)

        if self._language_mode == "auto":
            kwargs["language_detection"] = True
        else:
            supported_modes = language_modes_for_selection(
                "assemblyai",
                self._model,
            )
            if self._language_mode in supported_modes:
                kwargs["language_code"] = self._language_mode
                kwargs["language_detection"] = False
            else:
                # Unknown language code → fall back to auto detection.
                kwargs["language_detection"] = True

        return aai.TranscriptionConfig(**kwargs)

    @staticmethod
    def _speech_models_for_selection(model: str) -> list[str]:
        selected = (model or DEFAULT_ASSEMBLYAI_MODEL).strip().lower()
        if selected == "universal-2":
            return ["universal-2"]
        if selected == "universal-3-pro":
            return ["universal-3-pro", "universal-2"]
        raise TranscriptionError(
            "Unsupported AssemblyAI model: "
            f"{model}. Choose universal-3-pro or universal-2."
        )

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        """Transcribe audio via AssemblyAI batch API.

        Accepts WAV bytes, a file path, or a Path object.
        """
        self._configure()
        aai = self._get_aai()

        temp_path: Path | None = None
        try:
            if isinstance(audio_source, bytes):
                # Write WAV bytes to a temp file for the SDK.
                with tempfile.NamedTemporaryFile(
                    suffix=".wav",
                    delete=False,
                    dir=str(temp_audio_dir()),
                ) as handle:
                    handle.write(audio_source)
                    temp_path = Path(handle.name)
                file_path = str(temp_path)
            else:
                file_path = str(audio_source)

            config = self._build_config()
            transcriber = aai.Transcriber()
            if self._progress_callback is not None:
                self._emit_progress("Uploading audio to AssemblyAI...")
                audio_url = transcriber.upload_file(file_path)
                self._emit_progress(
                    "Upload complete. Submitting transcription to AssemblyAI..."
                )
                transcript = transcriber.submit(audio_url, config=config)
                self._emit_progress("AssemblyAI is transcribing audio...")
                transcript = transcript.wait_for_completion()
            else:
                transcript = transcriber.transcribe(file_path, config=config)

            if transcript.status == aai.TranscriptStatus.error:
                raise TranscriptionError(
                    f"AssemblyAI transcription failed: {transcript.error}"
                )

            text = transcript.text or ""
            return text.strip()

        except TranscriptionError:
            raise
        except FileNotFoundError as exc:
            raise TranscriptionError(
                "AssemblyAI transcription failed: missing file path. "
                "This can happen when the input file does not exist or when "
                "TEMP/TMP points to a non-existent folder."
            ) from exc
        except Exception as exc:
            if _is_ssl_error(exc):
                raise TranscriptionError(
                    "AssemblyAI: SSL certificate verification failed "
                    "(likely a corporate proxy such as Zscaler). "
                    "Set REQUESTS_CA_BUNDLE to your corporate CA .pem, "
                    "or switch to the local provider.\n"
                    f"See {DOC_SSL_PROXY_PATH} for details."
                ) from exc
            raise TranscriptionError(f"AssemblyAI transcription failed: {exc}") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    # -- Connection test --------------------------------------------------------

    def test_connection(self) -> tuple[bool, str]:
        """Test API connectivity and key validity.

        Returns ``(success, message)`` where *success* is ``True`` when the
        key is accepted by the AssemblyAI API.
        """
        import urllib.error
        import urllib.request

        url = "https://api.assemblyai.com/v2/transcript?limit=1"
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", self._api_key)

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return True, "Connection OK — API key is valid."
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return False, (
                    "Authentication failed (HTTP 401). "
                    "The API key is invalid or expired."
                )
            return False, f"API returned HTTP {exc.code}: {exc.reason}"
        except Exception as exc:
            if _is_ssl_error(exc):
                return False, (
                    "SSL certificate verification failed — likely a "
                    "corporate proxy (Zscaler). Set REQUESTS_CA_BUNDLE "
                    "to your corporate CA .pem file.\n"
                    f"See {DOC_SSL_PROXY_PATH} for details."
                )
            return False, f"Connection failed: {exc}"

        return False, "Unexpected response from AssemblyAI API."

    # -- Streaming via Universal-Streaming (v3) --------------------------------

    def _format_stream_error(self, error: Exception) -> str:
        if _is_ssl_error(error):
            return (
                "AssemblyAI streaming failed: SSL certificate verification failed "
                "(likely a corporate proxy such as Zscaler)."
            )
        return f"AssemblyAI streaming failed: {error}"

    def _notify_stream_error(self, error: Exception) -> None:
        with self._stream_lock:
            callback = self._stream_on_error
            if callback is None or self._stream_error_reported:
                return
            self._stream_error_reported = True

        try:
            callback(self._format_stream_error(error))
        except Exception:
            pass

    def _stream_combined_text_locked(self) -> str:
        parts = [self._stream_turns[order] for order in sorted(self._stream_turns)]
        return " ".join(p for p in parts if p).strip()

    def _reset_stream_state_locked(self) -> None:
        self._stream_client = None
        self._stream_on_partial = None
        self._stream_on_error = None
        self._stream_turns = {}
        self._stream_error = None
        self._stream_error_reported = False

    @staticmethod
    def _shutdown_streaming_client(client, *, join_timeout_s: float) -> None:
        """Terminate the session on a helper thread.

        ``StreamingClient.disconnect`` joins the SDK's reader/writer threads,
        which can hang on a dead connection, so the join is bounded here.
        """

        def _disconnect() -> None:
            try:
                client.disconnect(terminate=True)
            except Exception:
                pass

        worker = threading.Thread(
            target=_disconnect,
            name="stt_app_assemblyai_disconnect",
            daemon=True,
        )
        worker.start()
        worker.join(timeout=join_timeout_s)

    def start_stream(
        self,
        on_partial: StreamingCallback | None = None,
        on_error: StreamingErrorCallback | None = None,
    ) -> None:
        """Start a Universal-Streaming (v3) session.

        The ``on_partial`` callback receives the accumulated transcript text
        (all completed turns + the current turn) each time an update arrives
        from the server.
        """
        from assemblyai.streaming.v3 import (
            Encoding,
            SpeechModel,
            StreamingClient,
            StreamingClientOptions,
            StreamingEvents,
            StreamingParameters,
        )

        with self._stream_lock:
            if self._stream_client is not None:
                raise TranscriptionError("Streaming session already active.")
            self._stream_on_partial = on_partial
            self._stream_on_error = on_error
            self._stream_turns = {}
            self._stream_error = None
            self._stream_error_reported = False

        try:
            if self._streaming_client_factory is not None:
                client = self._streaming_client_factory(self._api_key)
            else:
                client = StreamingClient(
                    StreamingClientOptions(api_key=self._api_key)
                )
            client.on(StreamingEvents.Turn, self._on_turn_event)
            client.on(StreamingEvents.Error, self._on_stream_error_event)
            client.connect(
                StreamingParameters(
                    sample_rate=AUDIO_SAMPLE_RATE,
                    encoding=Encoding.pcm_s16le,
                    speech_model=SpeechModel.universal_streaming_multilingual,
                    language_detection=True,
                    format_turns=True,
                )
            )
        except Exception as exc:
            with self._stream_lock:
                self._reset_stream_state_locked()
            if _is_ssl_error(exc):
                raise TranscriptionError(
                    "AssemblyAI streaming: SSL certificate verification failed "
                    "(likely a corporate proxy such as Zscaler)."
                ) from exc
            raise TranscriptionError(
                f"AssemblyAI streaming: failed to connect: {exc}"
            ) from exc

        # The SDK reports some connect failures through the error handler
        # instead of raising, so check for a recorded error before going live.
        with self._stream_lock:
            connect_error = self._stream_error
            if connect_error is None:
                self._stream_client = client
        if connect_error is not None:
            self._shutdown_streaming_client(client, join_timeout_s=1.0)
            with self._stream_lock:
                self._reset_stream_state_locked()
            raise TranscriptionError(
                self._format_stream_error(connect_error)
            ) from connect_error

    def push_audio_chunk(self, chunk: bytes) -> None:
        """Queue a raw PCM16 audio chunk for the streaming session.

        ``StreamingClient.stream`` only enqueues the chunk for the SDK's
        writer thread, so this is safe to call from the audio callback.
        """
        payload = bytes(chunk or b"")
        if not payload:
            return
        with self._stream_lock:
            client = self._stream_client
        if client is None:
            raise TranscriptionError("Streaming session is not active.")
        try:
            client.stream(payload)
        except Exception as exc:
            raise TranscriptionError(
                f"AssemblyAI streaming: failed to send audio: {exc}"
            ) from exc

    def stop_stream(self) -> str:
        """Finalize the streaming session and return accumulated text."""
        with self._stream_lock:
            client = self._stream_client
            self._stream_client = None
            # Drop the error callback first; close events after a normal
            # stop must not surface as runtime failures.
            self._stream_on_error = None
            if client is None:
                raise TranscriptionError("Streaming session is not active.")

        self._shutdown_streaming_client(client, join_timeout_s=5.0)

        with self._stream_lock:
            text = self._stream_combined_text_locked()
            error = self._stream_error
            self._reset_stream_state_locked()

        if error and not text:
            raise TranscriptionError(self._format_stream_error(error))

        return text

    def abort_stream(self) -> None:
        """Abort the streaming session immediately, discarding all text."""
        with self._stream_lock:
            client = self._stream_client
            self._stream_client = None
            self._stream_on_partial = None
            self._stream_on_error = None
        if client is not None:
            self._shutdown_streaming_client(client, join_timeout_s=0.5)

        with self._stream_lock:
            self._reset_stream_state_locked()

    # -- Streaming callbacks (called from the SDK reader thread) ---------------

    def _on_turn_event(self, _client, event) -> None:
        """Handle a Turn event.

        ``transcript`` holds the finalized words of one turn and grows as the
        turn progresses; with ``format_turns`` a formatted version of the
        same turn arrives last, so the text is keyed by ``turn_order``.
        """
        text = str(getattr(event, "transcript", "") or "").strip()
        if not text:
            return
        turn_order = int(getattr(event, "turn_order", 0) or 0)

        with self._stream_lock:
            self._stream_turns[turn_order] = text
            callback = self._stream_on_partial
            combined = self._stream_combined_text_locked()

        if callback is not None and combined:
            try:
                callback(combined)
            except Exception:
                pass

    def _on_stream_error_event(self, _client, error) -> None:
        """Handle errors from the streaming session."""
        if not isinstance(error, Exception):
            error = RuntimeError(str(error))
        with self._stream_lock:
            if self._stream_error is None:
                self._stream_error = error
        self._notify_stream_error(error)
