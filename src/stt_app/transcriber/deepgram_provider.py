"""Deepgram remote transcription provider.

Batch transcription via Deepgram's REST API (direct HTTP, no SDK needed).
Streaming transcription via Deepgram WebSocket API (`/v1/listen`).
API key stored via keyring (settings_dialog / secret_store).

Uses the nova-3 model by default (Deepgram's most capable speech model).
No Deepgram SDK required; streaming needs `websocket-client`.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..config import (
    AUDIO_SAMPLE_RATE,
    DEFAULT_CUSTOM_VOCABULARY,
    DEFAULT_DEEPGRAM_MODEL,
    DOC_SSL_PROXY_PATH,
    language_modes_for_selection,
    parse_custom_vocabulary,
)
from ..ssl_utils import create_ssl_context, is_ssl_error as _is_ssl_error
from ._http_utils import audio_content_type
from .base import (
    AudioInput,
    ITranscriber,
    ProgressReporter,
    StreamingCallback,
    StreamingErrorCallback,
    TranscriptionError,
)

DEEPGRAM_API_BASE = "https://api.deepgram.com/v1"

_STREAM_SEND_SENTINEL = object()
_STREAM_AUDIO_QUEUE_MAX_CHUNKS = 32
_STREAM_SENDER_DRAIN_TIMEOUT_S = 2.0
_STREAM_FINALIZE_QUIET_PERIOD_S = 0.25
_STREAM_FINALIZE_MAX_WAIT_S = 1.25
_STREAM_CONTROL_SEND_TIMEOUT_S = 1.0
_STREAM_SERVER_CLOSE_TIMEOUT_S = 2.0
_STREAM_SOCKET_CLOSE_TIMEOUT_S = 1.0


class DeepgramTranscriber(ProgressReporter, ITranscriber):
    """Batch transcription using Deepgram's pre-recorded audio REST API.

    Parameters
    ----------
    api_key : str
        Deepgram API key (required).
    language_mode : str
        ``"auto"`` for automatic language detection,
        or a language code like ``"de"`` / ``"en"``.
    model : str
        Deepgram model name.  Defaults to ``nova-3``.
    """

    def __init__(
        self,
        api_key: str,
        language_mode: str = "auto",
        model: str = DEFAULT_DEEPGRAM_MODEL,
        custom_vocabulary: str = DEFAULT_CUSTOM_VOCABULARY,
    ) -> None:
        ProgressReporter.__init__(self)
        if not api_key:
            raise TranscriptionError(
                "Deepgram API key is missing. "
                "Enter your key in Settings → Remote Provider API Keys."
            )
        self._api_key = api_key
        self._language_mode = (language_mode or "auto").strip().lower()
        self._model = model or DEFAULT_DEEPGRAM_MODEL
        if self._language_mode not in language_modes_for_selection(
            "deepgram",
            self._model,
        ):
            self._language_mode = "auto"
        self._vocabulary_terms = parse_custom_vocabulary(custom_vocabulary)
        self._stream_lock = threading.Lock()
        self._stream_generation = 0
        self._stream_state = "idle"
        self._stream_ws = None
        self._stream_thread: threading.Thread | None = None
        self._stream_send_queue: queue.Queue | None = None
        self._stream_send_thread: threading.Thread | None = None
        self._stream_sender_drained = threading.Event()
        self._stream_ws_module = None
        self._stream_connected = threading.Event()
        self._stream_closed = threading.Event()
        self._stream_finalize_received = threading.Event()
        self._stream_error: Exception | None = None
        self._stream_on_partial: StreamingCallback | None = None
        self._stream_on_error: StreamingErrorCallback | None = None
        self._stream_finals: list[str] = []
        self._stream_partial_text = ""
        self._stream_error_reported = False
        self._stream_last_message_at = 0.0

    def _stream_combined_text_locked(self) -> str:
        parts = list(self._stream_finals)
        if self._stream_partial_text:
            parts.append(self._stream_partial_text)
        return " ".join(p for p in parts if p).strip()

    def _vocabulary_query_param_name(self) -> str:
        """Return the biasing query param name for the selected model.

        nova-3 models use the repeated ``keyterm`` parameter; nova-2 (and
        earlier) models use the repeated ``keywords`` parameter instead.
        """
        return (
            "keyterm"
            if self._model.strip().lower().startswith("nova-3")
            else "keywords"
        )

    def _apply_vocabulary_params(self, params: dict[str, object]) -> None:
        if not self._vocabulary_terms:
            return
        params[self._vocabulary_query_param_name()] = list(self._vocabulary_terms)

    def _format_stream_error(self, error: Exception) -> str:
        detail = str(error)
        if _is_ssl_error(error):
            detail = (
                "SSL certificate verification failed (likely a corporate "
                f"proxy). See {DOC_SSL_PROXY_PATH} for details."
            )
        return f"Deepgram streaming failed: {detail}"

    def _stream_session_matches_locked(self, generation: int, ws) -> bool:
        return (
            generation == self._stream_generation
            and ws is self._stream_ws
            and self._stream_state != "idle"
        )

    def _notify_stream_error(
        self,
        error: Exception,
        *,
        generation: int,
        ws,
    ) -> None:
        with self._stream_lock:
            if not self._stream_session_matches_locked(generation, ws):
                return
            callback = self._stream_on_error
            if callback is None or self._stream_error_reported:
                return
            self._stream_error_reported = True

        try:
            callback(self._format_stream_error(error))
        except Exception:
            pass

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        """Transcribe audio via Deepgram pre-recorded API.

        Accepts WAV bytes, a file path, or a Path object.
        """
        try:
            if isinstance(audio_source, bytes):
                audio_data = audio_source
                filename = "audio.wav"
            else:
                file_path = Path(audio_source)
                audio_data = file_path.read_bytes()
                filename = file_path.name or "audio.wav"

            # Build query parameters.
            params: dict[str, object] = {
                "model": self._model,
                "smart_format": "true",
            }

            if self._language_mode == "auto":
                params["detect_language"] = "true"
            else:
                params["language"] = self._language_mode
            self._apply_vocabulary_params(params)

            url = (
                f"{DEEPGRAM_API_BASE}/listen?"
                f"{urllib.parse.urlencode(params, doseq=True)}"
            )

            req = urllib.request.Request(url, data=audio_data, method="POST")
            req.add_header("Authorization", f"Token {self._api_key}")
            req.add_header("Content-Type", audio_content_type(filename))

            ssl_ctx = create_ssl_context()
            self._emit_progress(
                "Uploading audio to Deepgram and waiting for transcription..."
            )
            with urllib.request.urlopen(req, timeout=120, context=ssl_ctx) as resp:
                body = json.loads(resp.read().decode("utf-8"))

            # Extract transcript from response.
            # Response structure: results.channels[0].alternatives[0].transcript
            text = self._extract_transcript(body)
            return text.strip()

        except TranscriptionError:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise TranscriptionError(
                    "Deepgram: Authentication failed (HTTP 401). "
                    "The API key is invalid or expired."
                ) from exc
            if exc.code == 402:
                raise TranscriptionError(
                    "Deepgram: Insufficient credits (HTTP 402). "
                    "Check your Deepgram account balance."
                ) from exc
            if exc.code == 429:
                raise TranscriptionError(
                    "Deepgram: Rate limit exceeded (HTTP 429). "
                    "Wait a moment and try again."
                ) from exc
            raise TranscriptionError(
                f"Deepgram transcription failed (HTTP {exc.code}): {exc.reason}"
            ) from exc
        except Exception as exc:
            if _is_ssl_error(exc):
                raise TranscriptionError(
                    "Deepgram: SSL certificate verification failed "
                    "(likely a corporate proxy such as Zscaler). "
                    "Set SSL_CERT_FILE or REQUESTS_CA_BUNDLE to your "
                    "corporate CA .pem, or switch to the local provider. "
                    f"See {DOC_SSL_PROXY_PATH} for details."
                ) from exc
            raise TranscriptionError(f"Deepgram transcription failed: {exc}") from exc

    @staticmethod
    def _extract_transcript(body: dict) -> str:
        """Extract the transcript text from a Deepgram JSON response."""
        try:
            channels = body["results"]["channels"]
            if not channels:
                return ""
            alternatives = channels[0].get("alternatives", [])
            if not alternatives:
                return ""
            return alternatives[0].get("transcript", "")
        except (KeyError, IndexError, TypeError):
            return ""

    # -- Connection test --------------------------------------------------------

    def test_connection(self) -> tuple[bool, str]:
        """Test API connectivity and key validity.

        Returns ``(success, message)`` where *success* is ``True`` when the
        key is accepted by the Deepgram API.
        """
        url = f"{DEEPGRAM_API_BASE}/projects"
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Token {self._api_key}")

        try:
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
            return False, f"API returned HTTP {exc.code}: {exc.reason}"
        except Exception as exc:
            if _is_ssl_error(exc):
                return False, (
                    "SSL certificate verification failed — likely a "
                    "corporate proxy (Zscaler). Set SSL_CERT_FILE or "
                    "REQUESTS_CA_BUNDLE to your corporate CA .pem file. "
                    f"See {DOC_SSL_PROXY_PATH} for details."
                )
            return False, f"Connection failed: {exc}"

        return False, "Unexpected response from Deepgram API."

    # -- Streaming stubs --------------------------------------------------------
    def _get_websocket_module(self):
        if self._stream_ws_module is not None:
            return self._stream_ws_module
        try:
            import websocket  # type: ignore
        except ImportError as exc:
            raise TranscriptionError(
                "Deepgram streaming requires 'websocket-client'. "
                "Install it with: pip install websocket-client"
            ) from exc
        self._stream_ws_module = websocket
        return websocket

    def _reset_stream_state_locked(self) -> None:
        self._stream_state = "idle"
        self._stream_ws = None
        self._stream_thread = None
        self._stream_send_queue = None
        self._stream_send_thread = None
        self._stream_on_partial = None
        self._stream_on_error = None
        self._stream_finals = []
        self._stream_partial_text = ""
        self._stream_error = None
        self._stream_error_reported = False
        self._stream_last_message_at = 0.0

    def _record_stream_error(
        self,
        generation: int,
        ws,
        error: Exception,
        *,
        notify: bool,
    ) -> None:
        with self._stream_lock:
            if not self._stream_session_matches_locked(generation, ws):
                return
            if self._stream_error is None:
                self._stream_error = error
        if notify:
            self._notify_stream_error(
                error,
                generation=generation,
                ws=ws,
            )

    def start_stream(
        self,
        on_partial: StreamingCallback | None = None,
        on_error: StreamingErrorCallback | None = None,
    ) -> None:
        websocket = self._get_websocket_module()
        params: dict[str, object] = {
            "model": self._model,
            "encoding": "linear16",
            "sample_rate": str(AUDIO_SAMPLE_RATE),
            "channels": "1",
            "interim_results": "true",
            "smart_format": "true",
        }
        if self._language_mode == "auto":
            # The live API rejects detect_language; multilingual
            # code-switching ("multi") is the streaming equivalent on
            # nova-2/nova-3.
            params["language"] = "multi"
        else:
            params["language"] = self._language_mode
        self._apply_vocabulary_params(params)

        url = (
            f"wss://api.deepgram.com/v1/listen?"
            f"{urllib.parse.urlencode(params, doseq=True)}"
        )

        with self._stream_lock:
            if self._stream_state != "idle":
                raise TranscriptionError("Streaming session already active.")
            self._stream_generation += 1
            generation = self._stream_generation
            self._stream_state = "starting"
            self._stream_error = None
            self._stream_on_partial = on_partial
            self._stream_on_error = on_error
            self._stream_finals = []
            self._stream_partial_text = ""
            self._stream_error_reported = False
            self._stream_last_message_at = 0.0
            connected = threading.Event()
            closed = threading.Event()
            finalize_received = threading.Event()
            sender_drained = threading.Event()
            self._stream_connected = connected
            self._stream_closed = closed
            self._stream_finalize_received = finalize_received
            self._stream_sender_drained = sender_drained

        def _on_open(callback_ws):
            with self._stream_lock:
                if not self._stream_session_matches_locked(
                    generation,
                    callback_ws,
                ):
                    return
                connected.set()

        def _on_message(callback_ws, message):
            self._handle_stream_message(generation, callback_ws, message)

        def _on_error(callback_ws, error):
            if not isinstance(error, Exception):
                error = RuntimeError(str(error))
            with self._stream_lock:
                if not self._stream_session_matches_locked(
                    generation,
                    callback_ws,
                ):
                    return
                if self._stream_error is None:
                    self._stream_error = error
                if self._stream_state == "starting":
                    connected.set()
            self._notify_stream_error(
                error,
                generation=generation,
                ws=callback_ws,
            )

        def _on_close(callback_ws, _status, _reason):
            unexpected_error = None
            with self._stream_lock:
                if not self._stream_session_matches_locked(
                    generation,
                    callback_ws,
                ):
                    return
                closed.set()
                if self._stream_state in {"starting", "active"}:
                    if self._stream_error is None:
                        unexpected_error = RuntimeError(
                            "WebSocket connection closed unexpectedly."
                        )
                        self._stream_error = unexpected_error
                    if self._stream_state == "starting":
                        connected.set()
            if unexpected_error is not None:
                self._notify_stream_error(
                    unexpected_error,
                    generation=generation,
                    ws=callback_ws,
                )

        try:
            ws = websocket.WebSocketApp(
                url,
                header=[f"Authorization: Token {self._api_key}"],
                on_open=_on_open,
                on_message=_on_message,
                on_error=_on_error,
                on_close=_on_close,
            )
        except Exception as exc:
            with self._stream_lock:
                if (
                    generation == self._stream_generation
                    and self._stream_state == "starting"
                ):
                    self._reset_stream_state_locked()
            raise TranscriptionError(
                f"Deepgram streaming failed to initialize: {exc}"
            ) from exc

        thread = threading.Thread(
            target=ws.run_forever,
            name="stt_app_deepgram_stream",
            daemon=True,
        )
        send_queue: queue.Queue = queue.Queue(maxsize=_STREAM_AUDIO_QUEUE_MAX_CHUNKS)
        send_thread = threading.Thread(
            target=self._stream_send_worker,
            args=(
                generation,
                ws,
                send_queue,
                sender_drained,
                websocket,
            ),
            name="stt_app_deepgram_send",
            daemon=True,
        )
        with self._stream_lock:
            session_was_stopped = (
                generation != self._stream_generation
                or self._stream_state != "starting"
            )
            if not session_was_stopped:
                self._stream_ws = ws
                self._stream_thread = thread
                self._stream_send_queue = send_queue
                self._stream_send_thread = send_thread
        if session_was_stopped:
            self._close_streaming_socket(ws)
            raise TranscriptionError(
                "Deepgram streaming session was stopped while connecting."
            )
        try:
            thread.start()
        except Exception as exc:
            with self._stream_lock:
                if self._stream_session_matches_locked(generation, ws):
                    self._stream_state = "retiring"
                    self._stream_on_partial = None
                    self._stream_on_error = None
            self._close_streaming_socket(ws)
            with self._stream_lock:
                if self._stream_session_matches_locked(generation, ws):
                    self._reset_stream_state_locked()
            raise TranscriptionError(
                f"Deepgram streaming failed to start: {exc}"
            ) from exc

        connected.wait(timeout=8.0)
        with self._stream_lock:
            session_matches = self._stream_session_matches_locked(generation, ws)
            error = self._stream_error if session_matches else None
            ready = (
                session_matches
                and self._stream_state == "starting"
                and connected.is_set()
                and not closed.is_set()
                and error is None
            )
            if ready:
                self._stream_state = "active"
                try:
                    send_thread.start()
                except Exception as exc:
                    error = exc
                    self._stream_error = exc
                    self._stream_state = "retiring"
                    self._stream_on_partial = None
                    self._stream_on_error = None
                    ready = False
                else:
                    return
            if session_matches:
                self._stream_state = "retiring"
                self._stream_on_partial = None
                self._stream_on_error = None

        self._close_streaming_socket(ws)
        thread.join(timeout=2.0)
        with self._stream_lock:
            if self._stream_session_matches_locked(generation, ws):
                self._reset_stream_state_locked()

        if error is not None:
            detail = str(error)
            if _is_ssl_error(error):
                detail = (
                    "SSL certificate verification failed (likely a corporate "
                    f"proxy). See {DOC_SSL_PROXY_PATH} for details."
                )
            raise TranscriptionError(
                f"Deepgram streaming failed to connect: {detail}"
            ) from error
        if not session_matches:
            raise TranscriptionError(
                "Deepgram streaming session was stopped while connecting."
            )
        raise TranscriptionError("Deepgram streaming failed to connect (timeout).")

    def push_audio_chunk(self, chunk: bytes) -> None:
        """Queue a PCM16 chunk for the sender thread without blocking.

        The bounded queue prevents an unavailable socket from consuming memory
        indefinitely. Saturation fails the stream instead of silently dropping
        audio, preserving transcript integrity and the PortAudio callback's
        nonblocking contract.
        """
        payload = bytes(chunk or b"")
        if not payload:
            return
        with self._stream_lock:
            if self._stream_state != "active":
                raise TranscriptionError("Streaming session is not active.")
            send_queue = self._stream_send_queue
            if send_queue is None:
                raise TranscriptionError("Streaming session is not active.")
            try:
                send_queue.put_nowait(payload)
            except queue.Full as exc:
                error = RuntimeError(
                    "audio queue is full because the WebSocket sender fell behind"
                )
                if self._stream_error is None:
                    self._stream_error = error
                raise TranscriptionError(
                    "Deepgram streaming audio queue is full; "
                    "the connection cannot keep up with microphone audio."
                ) from exc

    def _stream_send_worker(
        self,
        generation: int,
        ws,
        send_queue: queue.Queue,
        sender_drained: threading.Event,
        websocket,
    ) -> None:
        while True:
            item = send_queue.get()
            if item is _STREAM_SEND_SENTINEL:
                sender_drained.set()
                return
            with self._stream_lock:
                if (
                    not self._stream_session_matches_locked(generation, ws)
                    or self._stream_send_queue is not send_queue
                ):
                    return
            try:
                ws.send(item, opcode=websocket.ABNF.OPCODE_BINARY)
            except Exception as exc:
                self._record_stream_error(
                    generation,
                    ws,
                    exc,
                    notify=True,
                )
                return

    @staticmethod
    def _drain_stream_sender(
        send_queue: queue.Queue | None,
        send_thread: threading.Thread | None,
        sender_drained: threading.Event,
        *,
        timeout_s: float,
    ) -> bool:
        if send_queue is None or send_thread is None:
            return False
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                send_queue.put_nowait(_STREAM_SEND_SENTINEL)
                break
            except queue.Full:
                if not send_thread.is_alive() or time.monotonic() >= deadline:
                    return False
                time.sleep(0.01)
        while not sender_drained.is_set():
            if not send_thread.is_alive() or time.monotonic() >= deadline:
                return False
            sender_drained.wait(timeout=min(0.05, deadline - time.monotonic()))
        send_thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return not send_thread.is_alive()

    @staticmethod
    def _send_stream_control(ws, message_type: str) -> Exception | None:
        completed = threading.Event()
        errors: list[Exception] = []

        def send() -> None:
            try:
                ws.send(json.dumps({"type": message_type}))
            except Exception as exc:
                errors.append(exc)
            finally:
                completed.set()

        worker = threading.Thread(
            target=send,
            name=f"stt_app_deepgram_{message_type.lower()}",
            daemon=True,
        )
        worker.start()
        if not completed.wait(timeout=_STREAM_CONTROL_SEND_TIMEOUT_S):
            return TimeoutError(f"timed out sending Deepgram {message_type}")
        return errors[0] if errors else None

    @staticmethod
    def _close_streaming_socket(ws) -> None:
        def close() -> None:
            try:
                ws.close()
            except Exception:
                pass

        worker = threading.Thread(
            target=close,
            name="stt_app_deepgram_close",
            daemon=True,
        )
        worker.start()
        worker.join(timeout=_STREAM_SOCKET_CLOSE_TIMEOUT_S)

    def _wait_for_finalize_drain(
        self,
        generation: int,
        ws,
        finalize_received: threading.Event,
        closed: threading.Event,
    ) -> None:
        started = time.monotonic()
        deadline = started + _STREAM_FINALIZE_MAX_WAIT_S
        quiet_deadline = started + _STREAM_FINALIZE_QUIET_PERIOD_S
        last_seen = 0.0
        while time.monotonic() < deadline:
            if finalize_received.is_set() or closed.is_set():
                return
            with self._stream_lock:
                if not self._stream_session_matches_locked(generation, ws):
                    return
                last_message_at = self._stream_last_message_at
            if last_message_at > last_seen:
                last_seen = last_message_at
                quiet_deadline = time.monotonic() + _STREAM_FINALIZE_QUIET_PERIOD_S
            if time.monotonic() >= quiet_deadline:
                return
            finalize_received.wait(timeout=0.05)

    def stop_stream(self) -> str:
        with self._stream_lock:
            ws = self._stream_ws
            generation = self._stream_generation
            thread = self._stream_thread
            send_queue = self._stream_send_queue
            send_thread = self._stream_send_thread
            sender_drained = self._stream_sender_drained
            finalize_received = self._stream_finalize_received
            closed = self._stream_closed

            if ws is None or thread is None or self._stream_state != "active":
                raise TranscriptionError("Streaming session is not active.")
            self._stream_state = "retiring"
            # A normal server close can produce a socket-level close callback;
            # it is not a runtime failure after finalization has started.
            self._stream_on_error = None

        # Flush queued audio before finalizing; the sender exits on the
        # sentinel after sending everything queued ahead of it. Finalize and
        # CloseStream must never overtake audio frames.
        drained = self._drain_stream_sender(
            send_queue,
            send_thread,
            sender_drained,
            timeout_s=_STREAM_SENDER_DRAIN_TIMEOUT_S,
        )
        if not drained:
            self._record_stream_error(
                generation,
                ws,
                RuntimeError("timed out while draining queued streaming audio"),
                notify=False,
            )
        else:
            control_error = self._send_stream_control(ws, "Finalize")
            if control_error is not None:
                self._record_stream_error(
                    generation,
                    ws,
                    control_error,
                    notify=False,
                )
            else:
                # Deepgram may acknowledge this with from_finalize=true, but
                # that response is explicitly not guaranteed for empty buffers.
                self._wait_for_finalize_drain(
                    generation,
                    ws,
                    finalize_received,
                    closed,
                )

            if control_error is None:
                # CloseStream asks Deepgram to flush any remaining audio, send
                # final Results plus Metadata, and close the WebSocket.
                control_error = self._send_stream_control(ws, "CloseStream")
                if control_error is not None:
                    self._record_stream_error(
                        generation,
                        ws,
                        control_error,
                        notify=False,
                    )
                else:
                    closed.wait(timeout=_STREAM_SERVER_CLOSE_TIMEOUT_S)

        if not closed.is_set():
            self._close_streaming_socket(ws)
        if send_thread is not None and send_thread.is_alive():
            send_thread.join(timeout=0.2)
        thread.join(timeout=2.0)

        with self._stream_lock:
            if not self._stream_session_matches_locked(generation, ws):
                raise TranscriptionError("Streaming session is not active.")
            text = self._stream_combined_text_locked()
            error = self._stream_error
            self._reset_stream_state_locked()

        if error is not None and not text:
            raise TranscriptionError(self._format_stream_error(error)) from error
        return text

    def abort_stream(self) -> None:
        with self._stream_lock:
            ws = self._stream_ws
            generation = self._stream_generation
            thread = self._stream_thread
            send_queue = self._stream_send_queue
            send_thread = self._stream_send_thread
            connected = self._stream_connected
            if self._stream_state == "idle":
                return
            self._stream_state = "retiring"
            self._stream_send_queue = None
            self._stream_send_thread = None
            self._stream_on_partial = None
            self._stream_on_error = None
            connected.set()

        if ws is not None:
            self._close_streaming_socket(ws)
        if send_queue is not None:
            try:
                send_queue.put_nowait(_STREAM_SEND_SENTINEL)
            except queue.Full:
                pass
        if send_thread is not None and send_thread.is_alive():
            send_thread.join(timeout=0.2)
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.2)

        with self._stream_lock:
            if ws is None or self._stream_session_matches_locked(generation, ws):
                self._reset_stream_state_locked()

    def _handle_stream_message(self, generation: int, ws, message) -> None:
        try:
            payload = json.loads(str(message))
        except Exception:
            return
        if not isinstance(payload, dict):
            return

        transcript = ""
        channel = payload.get("channel")
        if isinstance(channel, dict):
            alternatives = channel.get("alternatives")
            if isinstance(alternatives, list) and alternatives:
                first = alternatives[0]
                if isinstance(first, dict):
                    transcript = str(first.get("transcript", "")).strip()

        is_final = bool(payload.get("is_final", False))
        with self._stream_lock:
            if not self._stream_session_matches_locked(generation, ws):
                return
            self._stream_last_message_at = time.monotonic()
            if bool(payload.get("from_finalize", False)):
                self._stream_finalize_received.set()
            if transcript:
                if is_final:
                    self._stream_finals.append(transcript)
                    self._stream_partial_text = ""
                else:
                    self._stream_partial_text = transcript
            callback = self._stream_on_partial
            combined = self._stream_combined_text_locked()
        if callback is None or not transcript or not combined:
            return
        try:
            callback(combined)
        except Exception:
            pass
