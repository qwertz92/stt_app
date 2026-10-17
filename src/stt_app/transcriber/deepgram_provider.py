"""Deepgram remote transcription provider.

Batch transcription via Deepgram's REST API (direct HTTP, no SDK needed).
Streaming transcription via Deepgram WebSocket API (`/v1/listen`).
API key stored via keyring (settings_dialog / secret_store).

Uses the nova-3 model by default (Deepgram's most capable speech model).
No Deepgram SDK required; streaming needs `websocket-client`.
"""

from __future__ import annotations

import json
import logging
import math
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..config import (
    AUDIO_BLOCK_DURATION_MS,
    AUDIO_CHANNELS,
    AUDIO_INPUT_BUFFER_S,
    AUDIO_SAMPLE_RATE,
    DEEPGRAM_API_HOSTS,
    DEFAULT_CUSTOM_VOCABULARY,
    DEFAULT_DEEPGRAM_MODEL,
    DEFAULT_DEEPGRAM_REGION,
    DOC_SSL_PROXY_PATH,
    language_modes_for_selection,
    normalize_deepgram_region,
    parse_custom_vocabulary,
)
from ..ssl_utils import create_ssl_context
from ..ssl_utils import is_ssl_error as _is_ssl_error
from ._http_utils import audio_content_type, http_error_suffix
from .base import (
    AudioInput,
    ITranscriber,
    ProgressReporter,
    StreamingCallback,
    StreamingErrorCallback,
    TranscriptionError,
)

logger = logging.getLogger(__name__)

_STREAM_SEND_SENTINEL = object()
# What the sender's queue answers when nothing arrived for a whole interval.
_STREAM_KEEPALIVE_DUE = object()
# A starved machine can deliver no microphone audio for up to 12 s (the capture
# watchdog's hard limit), and a stop during such a stall waits as long. The
# socket is open the whole time, and Deepgram closes a live stream that is
# silent on the wire -- so the sender writes a KeepAlive whenever its queue
# stayed empty for this long. Deepgram's documentation, read 2026-10-17:
#
# * https://developers.deepgram.com/docs/audio-keep-alive ("Audio Keep Alive"):
#   "Send a `KeepAlive` message every 3-5 seconds to prevent the 10-second
#   timeout that triggers a `NET-0001` error and closes the connection.
#   Ensure the message is sent as a text WebSocket frame -- sending it as
#   binary may result in incorrect handling and potential connection issues."
#   The message is `{"type": "KeepAlive"}`; "The server will not send a
#   response back"; "If no audio data or `KeepAlive` messages are sent within
#   a 10-second window, the connection will close with a `NET-0001` error."
#   Its examples send one every 3 seconds. It does not say whether a
#   KeepAlive may be sent while audio flows, and it does not mention
#   Finalize; the Finalize and CloseStream pages
#   (https://developers.deepgram.com/docs/finalize, .../close-stream) do not
#   mention KeepAlive either.
# * https://developers.deepgram.com/docs/stt-troubleshooting-websocket-data-and-net-errors
#   lists 1011 `NET-0001` ("has not received a Binary or Text frame from the
#   client within the timeout window") and 1011 `NET-0002` ("No audio was
#   received on the stream within the no-audio timeout window ... Send audio,
#   or a `KeepAlive` message, to keep the stream open"; close reason
#   `no_audio_timeout`). Its NET-0002 checklist says "Unlike NET-0001,
#   KeepAlive messages do reset this timeout" (link markup omitted): the
#   vendor's own evidence that a KeepAlive-only gap AFTER audio has gone out
#   holds. The page gives no length for the NET-0002 window. Its NET-0001
#   checklist contradicts the page above:
#   "Using `KeepAlive` messages alone will not prevent closure; you must send
#   at least one audio message", after "Ensure audio is sent within 10 seconds
#   of opening the connection". Not verified live (no key here), so both
#   readings are covered: when the first wait runs out in a session that has
#   sent no audio at all (a start stall -- the case that sentence would lose),
#   the sender writes one frame of digital silence, `_STREAM_SILENCE_FRAME`,
#   instead of the first KeepAlive, and KeepAlives from then on. Silence is
#   never written once audio has gone out.
#
# 3 s is the page's example and the low end of its 3-5 s: the sender thread
# can be late under the same CPU load that starves the capture, and the
# margin to 10 s is what absorbs it. Steady audio never reaches the interval,
# so no KeepAlive is sent while audio flows.
_STREAM_KEEPALIVE_INTERVAL_S = 3.0
# One gap is bridged for at most this long. Nothing in the app notices a
# microphone that stops delivering after its first block (the capture's
# watchdog watches only the first block, and the device listener defers its
# refresh while a recording runs), so before the KeepAlive Deepgram's own close
# after about 10 s was the only alarm, and the user saw the red streaming
# error. A KeepAlive that never stops would turn that into an overlay that
# says "Listening" until the hotkey. The bound is the capture's input buffer
# plus a margin: PortAudio holds at most AUDIO_INPUT_BUFFER_S (20 s) for a
# callback thread that is merely starved, and delivers it as a burst when the
# thread runs again, so a gap that short can still be a slow microphone; the
# longest wait the app itself puts on the audio path is 12 s (the start
# watchdog's hard limit, a stop's backlog wait). Past the buffer the audio is
# lost anyway, and 10 s more is margin for a late sender thread and for a
# stall that ends just after the buffer overflowed. Beyond that the microphone
# is dead, not slow: the sender stops writing KeepAlives (and the silence
# frame), Deepgram's close fires after its own 10 s, and the existing
# streaming-failure path runs. Real audio after the cap, if the socket is
# still open, is sent as usual and ends the gap. Gaps are counted in whole
# waits (`_STREAM_KEEPALIVE_INTERVAL_S` each), not from a clock.
_STREAM_KEEPALIVE_MAX_GAP_S = AUDIO_INPUT_BUFFER_S + 10.0
# The stream is opened as 16-bit linear PCM (`encoding=linear16`: 2 bytes per
# sample) at the capture's sample rate and channel count; the one silence
# frame is built from the same constants, a tenth of a second long (about 3 kB
# of billed audio), so it decodes as the stream the URL declared.
_STREAM_BYTES_PER_SAMPLE = 2
_STREAM_SILENCE_FRAME_S = 0.1
_STREAM_SILENCE_FRAME = bytes(
    round(AUDIO_SAMPLE_RATE * _STREAM_SILENCE_FRAME_S)
    * AUDIO_CHANNELS
    * _STREAM_BYTES_PER_SAMPLE
)
# The microphone hands over one block per callback, and a callback thread that
# was starved delivers everything PortAudio buffered meanwhile -- up to
# AUDIO_INPUT_BUFFER_S -- as one burst of `put_nowait` calls, far faster than
# the sender thread is scheduled. So the queue holds that buffer plus the
# 32 blocks (3.2 s) it held before the buffer grew; it stays bounded, which is
# all it is for: a socket that stops taking audio fails the stream once this
# much is waiting, about 23 s of audio (about 740 kB) instead of 3.2 s.
_STREAM_AUDIO_QUEUE_MAX_CHUNKS = (
    math.ceil(AUDIO_INPUT_BUFFER_S * 1000 / AUDIO_BLOCK_DURATION_MS) + 32
)
_STREAM_SENDER_DRAIN_TIMEOUT_S = 2.0
_STREAM_FINALIZE_QUIET_PERIOD_S = 0.25
_STREAM_FINALIZE_MAX_WAIT_S = 1.25
_STREAM_CONTROL_SEND_TIMEOUT_S = 1.0
_STREAM_SERVER_CLOSE_TIMEOUT_S = 2.0
_STREAM_SOCKET_CLOSE_TIMEOUT_S = 1.0


def _control_message(message_type: str) -> str:
    """The JSON text of a Deepgram control message (`Finalize`, `KeepAlive`, ...)."""
    return json.dumps({"type": message_type})


def _audio_queue_full_error() -> TranscriptionError:
    """The one wording for a rejected chunk, raised from both push paths."""
    return TranscriptionError(
        "Deepgram streaming audio queue is full; "
        "the connection cannot keep up with microphone audio."
    )


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
    region : str
        ``"us"`` (the default global endpoint) or ``"eu"``
        (``api.eu.deepgram.com``); batch, streaming and the connection test
        all use it.
    """

    def __init__(
        self,
        api_key: str,
        language_mode: str = "auto",
        model: str = DEFAULT_DEEPGRAM_MODEL,
        custom_vocabulary: str = DEFAULT_CUSTOM_VOCABULARY,
        region: str = DEFAULT_DEEPGRAM_REGION,
    ) -> None:
        ProgressReporter.__init__(self)
        if not api_key:
            raise TranscriptionError(
                "Deepgram API key is missing. Enter your key in Settings -> Providers."
            )
        self._api_key = api_key
        self._host = DEEPGRAM_API_HOSTS[normalize_deepgram_region(region)]
        self._model = model or DEFAULT_DEEPGRAM_MODEL
        # Needs self._model, so this must run after it is assigned above
        # (reordered from the model assignment's original position below it).
        self.set_language_mode(language_mode)
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
        self._stream_partial_callback_failed = False
        self._stream_message_parse_failed = False
        self._stream_last_message_at = 0.0

    def _normalize_language_mode(self, mode: str) -> str:
        normalized = (mode or "auto").strip().lower()
        if normalized not in language_modes_for_selection("deepgram", self._model):
            normalized = "auto"
        return normalized

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
            # `_stream_error_reported` above already makes this at most
            # once per session. Swallowing stays right -- there is
            # nowhere else to report to -- but this callback is the only
            # path a stream failure takes to the user, so losing it
            # silently leaves the overlay mid-session forever.
            logger.exception(
                "Deepgram streaming error callback failed; the failure "
                "was not reported."
            )

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
                f"https://{self._host}/v1/listen?"
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
                f"Deepgram transcription failed (HTTP {exc.code})"
                # The body, not `exc.reason`: Deepgram names the
                # rejected parameter there.
                f"{http_error_suffix(exc)}"
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
        url = f"https://{self._host}/v1/projects"
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
            return False, (f"API returned HTTP {exc.code}{http_error_suffix(exc)}")
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
        self._stream_partial_callback_failed = False
        self._stream_message_parse_failed = False
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
            "channels": str(AUDIO_CHANNELS),
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
            f"wss://{self._host}/v1/listen?{urllib.parse.urlencode(params, doseq=True)}"
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
            self._stream_partial_callback_failed = False
            self._stream_message_parse_failed = False
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

    def push_audio_chunk(
        self, chunk: bytes, *, block_timeout_s: float | None = None
    ) -> None:
        """Queue a PCM16 chunk for the sender thread.

        The bounded queue prevents an unavailable socket from consuming memory
        indefinitely. Saturation fails the stream instead of silently dropping
        audio, preserving transcript integrity.

        `block_timeout_s` is `None` on the PortAudio callback, which keeps the
        original nonblocking `put_nowait` contract. A number is a caller that
        may wait -- the controller's preconnect flush, which hands over every
        block recorded during the handshake at once: the queue held 32
        chunks (3.2 s; now `_STREAM_AUDIO_QUEUE_MAX_CHUNKS`, about 23 s)
        against a buffer that may hold 62.5 s, and the burst was fast
        enough (about 45 us for 33 pushes) that the sender thread never got
        scheduled, so the overflow was rejected on a healthy socket. Waiting is
        what lets the sender drain; the wait is bounded, and running out of it
        fails the stream exactly as saturation does.

        The wait happens with `_stream_lock` released. `stop_stream` and
        `abort_stream` take that lock first thing, and the latter is reached
        from the Qt thread, so holding it across a multi-second wait would
        freeze the UI. Queueing into a session that is retired in the meantime
        is harmless: the sender checks the session before sending and the queue
        is dropped with it.
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
            if block_timeout_s is None:
                # Unchanged: the state check and the offer stay atomic, so a
                # stop that has taken the lock cannot have a chunk slip in
                # behind its drain sentinel.
                try:
                    send_queue.put_nowait(payload)
                except queue.Full as exc:
                    self._record_audio_queue_full_locked()
                    raise _audio_queue_full_error() from exc
                return
        # `max(0.0, ...)`: `Queue.put` answers a negative timeout with a
        # `ValueError`, which is not one of the two exception types this
        # method's callers handle.
        try:
            send_queue.put(payload, timeout=max(0.0, float(block_timeout_s)))
        except queue.Full as exc:
            with self._stream_lock:
                self._record_audio_queue_full_locked()
            raise _audio_queue_full_error() from exc

    def _record_audio_queue_full_locked(self) -> None:
        """Keep the first reason this stream failed. The caller holds the lock."""
        if self._stream_error is None:
            self._stream_error = RuntimeError(
                "audio queue is full because the WebSocket sender fell behind"
            )

    def _stream_send_worker(
        self,
        generation: int,
        ws,
        send_queue: queue.Queue,
        sender_drained: threading.Event,
        websocket,
    ) -> None:
        """The one thread that writes audio to the socket.

        It also writes the KeepAlive (see `_STREAM_KEEPALIVE_INTERVAL_S`) when
        the queue stayed empty for a whole interval, so a KeepAlive can never
        overlap an audio frame, and it is gone -- the sentinel ends the loop --
        before `Finalize` and `CloseStream` are sent (the sender barrier in
        `_drain_stream_sender`), so none follows them. In a session that has
        sent no audio yet, the first such wait writes one frame of silence
        (`_STREAM_SILENCE_FRAME`) instead; a session that has sent audio never
        gets silence. One gap is bridged for at most
        `_STREAM_KEEPALIVE_MAX_GAP_S`: past it neither is written, and real
        audio that arrives later ends the gap and is sent as usual.
        """
        audio_sent = False
        idle_announced = False
        idle_s = 0.0  # whole waits since the last real audio frame
        cap_logged = False
        while True:
            try:
                item = send_queue.get(timeout=_STREAM_KEEPALIVE_INTERVAL_S)
            except queue.Empty:
                item = _STREAM_KEEPALIVE_DUE
                idle_s += _STREAM_KEEPALIVE_INTERVAL_S
            if item is _STREAM_SEND_SENTINEL:
                sender_drained.set()
                return
            with self._stream_lock:
                if (
                    not self._stream_session_matches_locked(generation, ws)
                    or self._stream_send_queue is not send_queue
                ):
                    return
            if item is _STREAM_KEEPALIVE_DUE and idle_s > _STREAM_KEEPALIVE_MAX_GAP_S:
                if not cap_logged:
                    cap_logged = True
                    logger.warning(
                        "Deepgram streaming: no audio for more than %.0f s, "
                        "longer than the capture's input buffer can account "
                        "for; the microphone has probably stopped delivering. "
                        "KeepAlive frames stop here so that Deepgram closes the "
                        "stream and the streaming error reaches the user. "
                        "Logged once per gap.",
                        _STREAM_KEEPALIVE_MAX_GAP_S,
                    )
                continue
            try:
                if item is _STREAM_KEEPALIVE_DUE and not audio_sent:
                    idle_announced = True
                    logger.info(
                        "Deepgram streaming: no audio %.0f s after the connection "
                        "opened; sending one frame of digital silence, then "
                        "KeepAlive frames while the gap lasts. Logged once per "
                        "session.",
                        _STREAM_KEEPALIVE_INTERVAL_S,
                    )
                    ws.send(
                        _STREAM_SILENCE_FRAME,
                        opcode=websocket.ABNF.OPCODE_BINARY,
                    )
                    audio_sent = True
                elif item is _STREAM_KEEPALIVE_DUE:
                    if not idle_announced:
                        idle_announced = True
                        logger.info(
                            "Deepgram streaming: no audio for %.0f s, sending "
                            "KeepAlive frames to hold the connection open. "
                            "Logged once per session.",
                            _STREAM_KEEPALIVE_INTERVAL_S,
                        )
                    ws.send(_control_message("KeepAlive"))
                else:
                    ws.send(item, opcode=websocket.ABNF.OPCODE_BINARY)
                    audio_sent = True
                    idle_s = 0.0
                    cap_logged = False
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
                ws.send(_control_message(message_type))
            except Exception as exc:
                errors.append(exc)
            finally:
                completed.set()

        worker = threading.Thread(
            target=send,
            name=f"stt_app_deepgram_{message_type.lower()}",
            daemon=True,
        )
        try:
            worker.start()
        except Exception as exc:
            # Under thread exhaustion this raised straight out of the
            # finalize, skipping the socket close below it -- so the
            # connection stayed open and billed, which is the very
            # thing the surrounding try/finally was added for. This
            # function already reports by returning.
            return exc
        if not completed.wait(timeout=_STREAM_CONTROL_SEND_TIMEOUT_S):
            return TimeoutError(f"timed out sending Deepgram {message_type}")
        return errors[0] if errors else None

    @staticmethod
    def _close_streaming_socket(ws) -> None:
        def close() -> None:
            try:
                ws.close()
            except Exception:
                # The bounded join below cannot tell a refused close
                # from a clean one, and a socket that stays open stays
                # billed until the process exits.
                logger.warning(
                    "Deepgram streaming socket close failed; the "
                    "connection may still be open.",
                    exc_info=True,
                )

        worker = threading.Thread(
            target=close,
            name="stt_app_deepgram_close",
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            # Deliberately not closed inline instead: `close()` is on a
            # thread because it can hang, and hanging here would block
            # the finalize worker. `run_forever` is a daemon thread, so
            # the cost is a socket Deepgram bills until its own timeout,
            # not a process that cannot exit.
            logger.warning(
                "Could not start the Deepgram close thread; the connection may "
                "stay open until the server times it out.",
                exc_info=True,
            )
            return
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
            self._stream_state = "retiring"
            # A normal server close can produce a socket-level close callback;
            # it is not a runtime failure after finalization has started.
            self._stream_on_error = None

        retired = False
        try:
            text, error = self._finalize_stream(
                generation,
                ws,
                thread,
                send_queue,
                send_thread,
                sender_drained,
                finalize_received,
                closed,
            )
            retired = True
        finally:
            if not retired:
                self._retire_stream_state_if_ours(generation, ws)

        if error is not None and not text:
            raise TranscriptionError(self._format_stream_error(error)) from error
        return text

    def _finalize_stream(
        self,
        generation,
        ws,
        thread,
        send_queue,
        send_thread,
        sender_drained,
        finalize_received,
        closed,
    ) -> tuple[str, Exception | None]:
        try:
            text, error = self._finalize_stream_control(
                generation,
                ws,
                send_queue,
                send_thread,
                sender_drained,
                finalize_received,
                closed,
            )
        finally:
            # Never conditional on the block above succeeding: the whole
            # point of this teardown is that the socket does not stay
            # open, and every statement in it can raise.
            if not closed.is_set():
                self._close_streaming_socket(ws)
            if send_thread is not None and send_thread.is_alive():
                send_thread.join(timeout=0.2)
            thread.join(timeout=2.0)
        return text, error

    def _finalize_stream_control(
        self,
        generation,
        ws,
        send_queue,
        send_thread,
        sender_drained,
        finalize_received,
        closed,
    ) -> tuple[str, Exception | None]:
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

        with self._stream_lock:
            if not self._stream_session_matches_locked(generation, ws):
                raise TranscriptionError("Streaming session is not active.")
            text = self._stream_combined_text_locked()
            error = self._stream_error
            self._reset_stream_state_locked()
        return text, error

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

        try:
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
        finally:
            with self._stream_lock:
                if ws is None or self._stream_session_matches_locked(generation, ws):
                    self._reset_stream_state_locked()

    def _handle_stream_message(self, generation: int, ws, message) -> None:
        try:
            payload = json.loads(str(message))
        except Exception:
            with self._stream_lock:
                # Session-scoped like every other write here: a retired
                # socket delivering one bad frame after a replacement
                # started would otherwise latch the *new* session's flag
                # and silence its own protocol failures.
                if not self._stream_session_matches_locked(generation, ws):
                    return
                already_logged = self._stream_message_parse_failed
                self._stream_message_parse_failed = True
            if not already_logged:
                # Dropping it stays right -- there is nothing to read --
                # but a message that will not parse may have carried a
                # transcript, and that is invisible otherwise. Latched,
                # because a protocol change makes every frame fail.
                logger.warning(
                    "Deepgram sent a message that is not JSON; it was "
                    "dropped. Logged once per session.",
                    exc_info=True,
                )
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
            with self._stream_lock:
                already_logged = self._stream_partial_callback_failed
                self._stream_partial_callback_failed = True
            if not already_logged:
                # Latched, like `partial_callback_failed` in the local
                # faster-whisper path: this runs on every interim
                # result, so an unbounded log would flood. Swallowing
                # stays right -- the next partial carries the whole
                # combined text again -- but a dead live-insertion path
                # must not look like a user who simply stopped talking.
                logger.exception(
                    "Deepgram streaming partial callback failed; live "
                    "text is not being delivered. Logged once per "
                    "session."
                )
