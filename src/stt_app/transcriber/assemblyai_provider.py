"""AssemblyAI remote transcription provider.

Batch transcription via the AssemblyAI Python SDK.
Real-time streaming via AssemblyAI's Universal-3.5 Pro (v3) WebSocket API
(``assemblyai.streaming.v3.StreamingClient``); the legacy v2
``RealtimeTranscriber`` API has been retired by AssemblyAI.
Requires: pip install assemblyai
API key stored via keyring (settings_dialog / secret_store).

The batch provider uses the explicitly selected speech model with automatic
language detection enabled. It does not request a fallback model silently.
"""

from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path

from ..app_paths import temp_audio_dir
from ..config import (
    AUDIO_SAMPLE_RATE,
    DEFAULT_ASSEMBLYAI_MODEL,
    DEFAULT_CUSTOM_VOCABULARY,
    language_modes_for_selection,
    parse_custom_vocabulary,
)
from ..ssl_utils import create_ssl_context
from ..ssl_utils import is_ssl_error as _is_ssl_error
from ._http_utils import format_ssl_error_message, http_error_suffix
from .base import (
    AudioInput,
    ITranscriber,
    ProgressReporter,
    StreamingCallback,
    StreamingErrorCallback,
    TranscriptionError,
)

logger = logging.getLogger(__name__)


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
        custom_vocabulary: str = DEFAULT_CUSTOM_VOCABULARY,
    ) -> None:
        ProgressReporter.__init__(self)
        if not api_key:
            raise TranscriptionError(
                "AssemblyAI API key is missing. "
                "Enter your key in Settings → Remote Provider API Keys."
            )
        self._api_key = api_key
        # No class-specific validation: the base ``_normalize_language_mode``
        # (strip/lower with an "auto" fallback) already matches what this
        # provider needs. Actual language-code validity is decided per
        # request in ``_build_config`` (falls back to language detection).
        self.set_language_mode(language_mode)
        self._model = (model or DEFAULT_ASSEMBLYAI_MODEL).strip().lower()
        self._aai = aai_module  # None → lazy import on first use
        self._streaming_client_factory = streaming_client_factory
        self._word_boost = parse_custom_vocabulary(custom_vocabulary)
        self._stream_lock = threading.Lock()
        self._stream_generation = 0
        self._stream_state = "idle"
        self._stream_client = None
        self._stream_on_partial: StreamingCallback | None = None
        self._stream_on_error: StreamingErrorCallback | None = None
        self._stream_turns: dict[int, str] = {}
        self._stream_error: Exception | None = None
        self._stream_error_reported = False
        self._stream_partial_callback_failed = False

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

        if self._word_boost:
            kwargs["keyterms_prompt"] = self._word_boost

        return aai.TranscriptionConfig(**kwargs)

    @staticmethod
    def _speech_models_for_selection(model: str) -> list[str]:
        selected = (model or DEFAULT_ASSEMBLYAI_MODEL).strip().lower()
        if selected == "universal-2":
            return ["universal-2"]
        if selected == "universal-3-5-pro":
            return ["universal-3-5-pro"]
        raise TranscriptionError(
            "Unsupported AssemblyAI model: "
            f"{model}. Choose universal-3-5-pro or universal-2."
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
                    format_ssl_error_message("AssemblyAI")
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
            # Every other provider's REST call passes this; this one did not,
            # so behind a TLS-intercepting proxy the connection test failed
            # while transcription itself worked -- the SDK goes through
            # `requests`, which reads REQUESTS_CA_BUNDLE, whereas urllib does
            # not. The test then reported a broken key that was fine.
            ssl_ctx = create_ssl_context()
            with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
                if resp.status == 200:
                    return True, "Connection OK — API key is valid."
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                return False, (
                    "Authentication failed (HTTP 401). "
                    "The API key is invalid or expired."
                )
            return False, (
                f"API returned HTTP {exc.code}{http_error_suffix(exc)}"
            )
        except Exception as exc:
            if _is_ssl_error(exc):
                # The shared message, which names SSL_CERT_FILE too:
                # this call goes through urllib, and urllib does not read
                # REQUESTS_CA_BUNDLE, so the old advice could not fix what had
                # just failed.
                return False, format_ssl_error_message("AssemblyAI")
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

    def _stream_session_matches_locked(self, generation: int, client) -> bool:
        return (
            generation == self._stream_generation
            and client is self._stream_client
            and self._stream_state != "idle"
        )

    def _notify_stream_error(
        self,
        error: Exception,
        *,
        generation: int,
        client,
    ) -> None:
        with self._stream_lock:
            if not self._stream_session_matches_locked(generation, client):
                return
            callback = self._stream_on_error
            if callback is None or self._stream_error_reported:
                return
            self._stream_error_reported = True

        try:
            callback(self._format_stream_error(error))
        except Exception:
            # `_stream_error_reported` above already makes this at most
            # once per session. Swallowing stays right -- there is
            # nowhere else to report to -- but this callback is the only
            # path a stream failure takes to the user, so losing it
            # silently leaves the overlay mid-session forever.
            logger.exception(
                "AssemblyAI streaming error callback failed; the failure "
                "was not reported."
            )

    def _stream_combined_text_locked(self) -> str:
        parts = [self._stream_turns[order] for order in sorted(self._stream_turns)]
        return " ".join(p for p in parts if p).strip()

    def _reset_stream_state_locked(self) -> None:
        self._stream_state = "idle"
        self._stream_client = None
        self._stream_on_partial = None
        self._stream_on_error = None
        self._stream_turns = {}
        self._stream_error = None
        self._stream_error_reported = False
        self._stream_partial_callback_failed = False

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
                # A `disconnect` that raises before terminating leaves
                # the SDK's reader/writer threads running against a dead
                # socket, and the bounded join below cannot tell that
                # apart from a clean shutdown.
                logger.warning(
                    "AssemblyAI streaming disconnect failed; SDK "
                    "threads may still be running.",
                    exc_info=True,
                )

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
        """Start a Universal-3.5 Pro (v3) streaming session.

        The ``on_partial`` callback receives the accumulated transcript text
        (all completed turns + the current turn) each time an update arrives
        from the server.
        """
        from assemblyai.streaming.v3 import (
            Encoding,
            StreamingClient,
            StreamingClientOptions,
            StreamingEvents,
            StreamingParameters,
        )

        with self._stream_lock:
            if self._stream_state != "idle":
                raise TranscriptionError("Streaming session already active.")
            self._stream_generation += 1
            generation = self._stream_generation
            self._stream_state = "starting"
            self._stream_on_partial = on_partial
            self._stream_on_error = on_error
            self._stream_turns = {}
            self._stream_error = None
            self._stream_error_reported = False
            self._stream_partial_callback_failed = False

        client = None
        try:
            if self._streaming_client_factory is not None:
                client = self._streaming_client_factory(self._api_key)
            else:
                client = StreamingClient(StreamingClientOptions(api_key=self._api_key))
            with self._stream_lock:
                if (
                    generation != self._stream_generation
                    or self._stream_state != "starting"
                ):
                    raise TranscriptionError(
                        "AssemblyAI streaming session was stopped while connecting."
                    )
                self._stream_client = client

            client.on(
                StreamingEvents.Turn,
                lambda callback_client, event: self._on_turn_event(
                    generation,
                    client,
                    callback_client,
                    event,
                ),
            )
            client.on(
                StreamingEvents.Error,
                lambda callback_client, error: self._on_stream_error_event(
                    generation,
                    client,
                    callback_client,
                    error,
                ),
            )
            stream_kwargs = {
                "sample_rate": AUDIO_SAMPLE_RATE,
                "encoding": Encoding.pcm_s16le,
                "speech_model": "universal-3-5-pro",
            }
            if self._word_boost:
                # U3.5 Pro accepts up to 100 terms. The
                # shared vocabulary parser already caps the app input at 100.
                stream_kwargs["keyterms_prompt"] = self._word_boost
            client.connect(StreamingParameters(**stream_kwargs))
        except Exception as exc:
            with self._stream_lock:
                if (
                    generation == self._stream_generation
                    and self._stream_state == "starting"
                ):
                    self._stream_state = "retiring"
                    self._stream_on_partial = None
                    self._stream_on_error = None
            if client is not None:
                self._shutdown_streaming_client(client, join_timeout_s=1.0)
            with self._stream_lock:
                if (
                    generation == self._stream_generation
                    and self._stream_state == "retiring"
                    and (client is None or client is self._stream_client)
                ):
                    self._reset_stream_state_locked()
            if isinstance(exc, TranscriptionError):
                raise
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
            session_matches = self._stream_session_matches_locked(generation, client)
            connect_error = self._stream_error if session_matches else None
            if session_matches and self._stream_state == "starting":
                if connect_error is None:
                    self._stream_state = "active"
                else:
                    self._stream_state = "retiring"
                    self._stream_on_partial = None
                    self._stream_on_error = None
            connected = session_matches and self._stream_state == "active"
        if not connected and connect_error is None:
            self._shutdown_streaming_client(client, join_timeout_s=1.0)
            with self._stream_lock:
                if self._stream_session_matches_locked(generation, client):
                    self._reset_stream_state_locked()
            raise TranscriptionError(
                "AssemblyAI streaming session was stopped while connecting."
            )
        if connect_error is not None:
            self._shutdown_streaming_client(client, join_timeout_s=1.0)
            with self._stream_lock:
                if self._stream_session_matches_locked(generation, client):
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
            active = self._stream_state == "active"
        if client is None or not active:
            raise TranscriptionError("Streaming session is not active.")
        try:
            client.stream(payload)
        except Exception as exc:
            raise TranscriptionError(
                f"AssemblyAI streaming: failed to send audio: {exc}"
            ) from exc

    def _retire_stream_state_if_ours(self, generation: int, session) -> None:
        """Hand the provider back to `idle` after a stop/abort that raised.

        Everything between marking a session `retiring` and resetting it is
        network work -- draining the sender, control frames, closing the
        socket, joining threads -- and each of those spawns a short-lived
        thread, so `Thread.start()` alone can raise. A raise anywhere in that
        span skipped the reset and left the state at `retiring` for good, and
        `start_stream` refuses anything that is not `idle`: every later
        dictation then failed with "Streaming session already active" for the
        life of the app, with the remote socket still open and billed. That is
        the same end state the `starting` branch in `stop_stream` exists to
        prevent, reached through the other door.

        A session that no longer matches belongs to a replacement and is left
        alone, which is also why this cannot undo a normal completion.
        """
        with self._stream_lock:
            if self._stream_session_matches_locked(generation, session):
                self._reset_stream_state_locked()

    def stop_stream(self) -> str:
        """Finalize the streaming session and return accumulated text."""
        with self._stream_lock:
            client = self._stream_client
            generation = self._stream_generation
            if client is None or self._stream_state != "active":
            # Refusing is not enough while the handshake is still running.
            # `start_stream` would then finish, find the state still
            # "starting", publish the session as "active" -- and nobody owns
            # it: the caller has already been told the stop failed and has
            # torn its own state down. Every later dictation is then refused
            # with "Streaming session already active" for the rest of the
            # app's life, and the remote socket stays open and billed.
            # Marking it retiring is exactly what `abort_stream` does, and
            # both handshakes already have the branch that tears the client
            # down when they come back to a state that is no longer
            # "starting".
                if self._stream_state == "starting":
                    self._stream_state = "retiring"
                    self._stream_on_partial = None
                    self._stream_on_error = None
                raise TranscriptionError("Streaming session is not active.")
            # Drop the error callback only for a stop that proceeds: close
            # events after a normal stop must not surface as runtime failures.
            # Dropping it above the guard silenced a session that survived the
            # refusal, so a dead socket was never reported for the rest of it.
            self._stream_on_error = None
            self._stream_state = "retiring"

        retired = False
        try:
            self._shutdown_streaming_client(client, join_timeout_s=5.0)

            with self._stream_lock:
                if not self._stream_session_matches_locked(generation, client):
                    raise TranscriptionError("Streaming session is not active.")
                text = self._stream_combined_text_locked()
                error = self._stream_error
                self._reset_stream_state_locked()
                retired = True
        finally:
            if not retired:
                self._retire_stream_state_if_ours(generation, client)

        if error and not text:
            raise TranscriptionError(self._format_stream_error(error))

        return text

    def abort_stream(self) -> None:
        """Abort the streaming session immediately, discarding all text."""
        with self._stream_lock:
            client = self._stream_client
            generation = self._stream_generation
            if self._stream_state == "idle":
                return
            if client is None:
                self._reset_stream_state_locked()
                return
            self._stream_state = "retiring"
            self._stream_on_partial = None
            self._stream_on_error = None
        try:
            if client is not None:
                self._shutdown_streaming_client(client, join_timeout_s=0.5)
        finally:
            self._retire_stream_state_if_ours(generation, client)

    # -- Streaming callbacks (called from the SDK reader thread) ---------------

    def _on_turn_event(
        self,
        generation: int,
        expected_client,
        callback_client,
        event,
    ) -> None:
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
            if (
                callback_client is not expected_client
                or not self._stream_session_matches_locked(
                    generation,
                    expected_client,
                )
            ):
                return
            self._stream_turns[turn_order] = text
            callback = self._stream_on_partial
            combined = self._stream_combined_text_locked()

        if callback is not None and combined:
            try:
                callback(combined)
            except Exception:
                with self._stream_lock:
                    already_logged = self._stream_partial_callback_failed
                    self._stream_partial_callback_failed = True
                if not already_logged:
                    # Latched, like `partial_callback_failed` in the
                    # local faster-whisper path: this runs on every turn
                    # revision, so an unbounded log would flood.
                    # Swallowing stays right -- the next partial carries
                    # the whole combined text again -- but a dead
                    # live-insertion path must not look like a user who
                    # simply stopped talking.
                    logger.exception(
                        "AssemblyAI streaming partial callback failed; "
                        "live text is not being delivered. Logged once "
                        "per session."
                    )

    def _on_stream_error_event(
        self,
        generation: int,
        expected_client,
        callback_client,
        error,
    ) -> None:
        """Handle errors from the streaming session."""
        if not isinstance(error, Exception):
            error = RuntimeError(str(error))
        with self._stream_lock:
            if (
                callback_client is not expected_client
                or not self._stream_session_matches_locked(
                    generation,
                    expected_client,
                )
            ):
                return
            if self._stream_error is None:
                self._stream_error = error
        self._notify_stream_error(
            error,
            generation=generation,
            client=expected_client,
        )
