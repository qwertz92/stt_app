"""One ElevenLabs realtime speech-to-text session (Scribe v3 Live over a WebSocket).

`ElevenLabsTranscriber` creates one `ElevenLabsRealtimeSession` per dictation
and delegates `start_stream` / `push_audio_chunk` / `stop_stream` /
`abort_stream` to it. A session is used once, so a late callback of a retired
socket can only touch the session it belongs to: that object is the generation
scope that the Deepgram provider keeps in counters.

The protocol, with the vendor pages it was read from, is in
`docs/agents/remote-providers.md` ("ElevenLabs realtime streaming"). It has
never been run against the live service (no key was available); the tests use
a fake socket.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import queue
import threading
import time
import urllib.parse
from collections.abc import Callable

from ..config import (
    AUDIO_BLOCK_DURATION_MS,
    AUDIO_CHANNELS,
    AUDIO_INPUT_BUFFER_S,
    AUDIO_SAMPLE_RATE,
    ELEVENLABS_REALTIME_URL,
    ELEVENLABS_STREAMING_MODEL,
    ELEVENLABS_VAD_SILENCE_S,
)
from ..ssl_utils import is_ssl_error
from ._http_utils import format_ssl_error_message, key_redactor
from .base import StreamingCallback, StreamingErrorCallback, TranscriptionError

logger = logging.getLogger(__name__)

_PROVIDER = "ElevenLabs streaming"
_CONNECT_TIMEOUT_S = 8.0
# The server closes a session that received no client message for 15 s. Audio
# normally flows every 100 ms; when the queue stayed empty this long (a start
# stall, a stop's backlog wait) the sender writes 0.1 s of silence. Whether the
# vendor bills that silence is undocumented (its pricing page says per second,
# no minimum, and nothing about silence). 5 s leaves room for a late sender
# thread.
_KEEPALIVE_S = 5.0
# One gap is bridged for at most this long: the capture's input buffer plus a
# margin (the same reasoning as Deepgram's, docs/agents/streaming.md). Past it
# the microphone is dead, not slow, and the stream ends with a sentence that
# names the cause.
_MAX_GAP_S = AUDIO_INPUT_BUFFER_S + 10.0
_QUEUE_MAX_CHUNKS = (
    math.ceil(AUDIO_INPUT_BUFFER_S * 1000 / AUDIO_BLOCK_DURATION_MS) + 32
)
_DRAIN_TIMEOUT_S = 2.0
# After the final commit the answer is awaited at most this long; the text of
# the last partial is used when it does not arrive.
_FINAL_COMMIT_WAIT_S = 3.0
# The wait ends once an answer arrived, no partial is pending and no transcript
# message came for this long: a VAD commit for earlier audio can land before the
# answer to the final commit. A guess; no live call has confirmed the gap.
_FINAL_QUIET_S = 0.3
_SEND_TIMEOUT_S = 1.0
_CLOSE_TIMEOUT_S = 1.0
_SENTINEL = object()
_SILENCE = bytes(round(AUDIO_SAMPLE_RATE * 0.1) * AUDIO_CHANNELS * 2)

# What each documented error message_type means to the user. Every one ends the
# session; the two below it are answers to a commit and do not.
_ERROR_SENTENCES = {
    "auth_error": "authentication failed. The API key is invalid or expired.",
    "unaccepted_terms": (
        "the account has not accepted the terms. Accept them in your ElevenLabs account."
    ),
    "quota_exceeded": "quota exceeded. Check your ElevenLabs plan.",
    "rate_limited": "rate limit exceeded. Wait a moment and try again.",
    "queue_overflow": "the service is overloaded. Try again in a moment.",
    "resource_exhausted": "the service is out of resources. Try again in a moment.",
    "session_time_limit_exceeded": "the maximum session time was reached.",
    "chunk_size_exceeded": "an audio chunk was too large.",
    "input_error": "the service rejected the audio input.",
    "invalid_request": "the service rejected the request.",
    "transcriber_error": "the transcription failed on the service.",
    "error": "the service reported an error.",
}
_NON_FATAL = frozenset({"insufficient_audio_activity", "commit_throttled"})
_MAX_DETAIL_CHARS = 300


def _bounded(
    action: Callable[[], None], *, timeout_s: float, name: str
) -> Exception | None:
    """Run a blocking socket call on a daemon thread; the error, or a timeout."""
    errors: list[Exception] = []
    done = threading.Event()

    def run() -> None:
        try:
            action()
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    try:
        threading.Thread(
            target=run, name=f"stt_app_elevenlabs_{name}", daemon=True
        ).start()
    except Exception as exc:
        return exc
    if not done.wait(timeout=timeout_s):
        return TimeoutError(f"timed out: {name}")
    return errors[0] if errors else None


def _chunk_message(pcm: bytes, *, commit: bool) -> str:
    return json.dumps(
        {
            "message_type": "input_audio_chunk",
            "audio_base_64": base64.b64encode(pcm).decode("ascii"),
            "commit": commit,
            "sample_rate": AUDIO_SAMPLE_RATE,
        }
    )


class ElevenLabsRealtimeSession:
    """The socket, the sender thread and the transcript of one dictation."""

    def __init__(
        self,
        *,
        api_key: str,
        language_code: str | None,
        websocket_module,
        on_partial: StreamingCallback | None,
        on_error: StreamingErrorCallback | None,
    ) -> None:
        self._api_key = api_key
        self._scrub = key_redactor(api_key)
        self._language_code = language_code
        self._websocket = websocket_module
        self._on_partial = on_partial
        self._on_error = on_error
        self._lock = threading.Lock()
        self._state = "starting"  # starting -> active -> retiring
        self._ws = None
        self._thread: threading.Thread | None = None
        self._sender: threading.Thread | None = None
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX_CHUNKS)
        self._sender_drained = threading.Event()
        self._ready = threading.Event()
        # Set by abort and by a failure: the sender writes nothing more.
        self._halted = threading.Event()
        self._closed = threading.Event()
        # The final wait (`_await_final_answer`): `_awaiting_answer` is set when
        # the final commit is sent, `_answered` once a commit answer arrived after.
        self._awaiting_answer = False
        self._answered = False
        self._last_transcript_at = 0.0
        self._started = False
        self._error: str | None = None
        self._error_reported = False
        self._finals: list[str] = []
        self._partial_text = ""
        self._parse_failure_logged = False
        self._callback_failure_logged = False

    # -- the URL and the handshake ------------------------------------------------

    def url(self) -> str:
        """The endpoint with its parameters; the key is a header, never here."""
        params: dict[str, str] = {
            "model_id": ELEVENLABS_STREAMING_MODEL,
            "audio_format": f"pcm_{AUDIO_SAMPLE_RATE}",
            "commit_strategy": "vad",
            "vad_silence_threshold_secs": str(ELEVENLABS_VAD_SILENCE_S),
        }
        if self._language_code:
            params["language_code"] = self._language_code
        return f"{ELEVENLABS_REALTIME_URL}?{urllib.parse.urlencode(params)}"

    def connect(self) -> None:
        """Open the socket and wait for `session_started` (a worker thread only)."""
        try:
            ws = self._websocket.WebSocketApp(
                self.url(),
                header=[f"xi-api-key: {self._api_key}"],
                on_open=lambda _ws: None,
                on_message=self._on_message,
                on_error=self._on_socket_error,
                on_close=self._on_close,
            )
            thread = threading.Thread(
                target=ws.run_forever, name="stt_app_elevenlabs_stream", daemon=True
            )
            sender = threading.Thread(
                target=self._send_loop, name="stt_app_elevenlabs_send", daemon=True
            )
        except Exception as exc:
            raise TranscriptionError(
                self._scrub(f"{_PROVIDER} failed to initialize: {exc}")
            ) from exc
        with self._lock:
            if self._state != "starting":
                raise TranscriptionError(
                    f"{_PROVIDER} session was stopped while connecting."
                )
            self._ws, self._thread, self._sender = ws, thread, sender
        try:
            thread.start()
        except Exception as exc:
            self._close_socket()
            raise TranscriptionError(
                self._scrub(f"{_PROVIDER} failed to start: {exc}")
            ) from exc

        self._ready.wait(timeout=_CONNECT_TIMEOUT_S)
        with self._lock:
            stopped = self._state != "starting"
            ok = self._started and self._error is None and not self._closed.is_set()
            error = self._error
            if ok and not stopped:
                self._state = "active"
                try:
                    sender.start()
                except Exception as exc:
                    ok, error = (
                        False,
                        self._scrub(f"{_PROVIDER} failed to start: {exc}"),
                    )
                else:
                    return
            if not stopped:
                self._state = "retiring"
        self._close_socket()
        thread.join(timeout=2.0)
        if stopped:
            raise TranscriptionError(
                f"{_PROVIDER} session was stopped while connecting."
            )
        if error is not None:
            raise TranscriptionError(error)
        raise TranscriptionError(
            f"{_PROVIDER} failed to connect: the service did not answer with "
            "session_started in time."
        )

    # -- socket callbacks ---------------------------------------------------------

    def _fail(self, message: str) -> None:
        """Keep the first reason the session failed; tell the user once if it ran."""
        with self._lock:
            if self._error is None:
                self._error = message
            notify = (
                self._state == "active"
                and self._on_error is not None
                and not self._error_reported
            )
            callback = self._on_error
            if notify:
                self._error_reported = True
        self._halted.set()
        self._ready.set()
        if notify and callback is not None:
            try:
                callback(message)
            except Exception:
                logger.exception(
                    "ElevenLabs streaming error callback failed; the failure was not reported."
                )

    def _on_socket_error(self, _ws, error) -> None:
        with self._lock:
            if self._state == "retiring":
                # Our own stop or abort closed the socket (websocket-client
                # reports a close frame here before `on_close`): not a failure.
                return
        self._fail(self._describe_socket_error(error))

    def _on_close(self, _ws, status, reason) -> None:
        # Also ends the final wait of a stop.
        self._closed.set()
        with self._lock:
            if self._state == "retiring":
                return
        detail = f" (code {status}: {self._clip(reason, 100)})" if status else ""
        self._fail(
            f"{_PROVIDER} failed: the connection was closed by the service{detail}."
        )
        self._ready.set()

    def _describe_socket_error(self, error) -> str:
        if is_ssl_error(error):
            return format_ssl_error_message(_PROVIDER)
        status = getattr(error, "status_code", None)
        if isinstance(status, int) and 1000 <= status <= 4999:
            # websocket-client reports a server's close frame as an exception
            # whose `status_code` is the close code, not an HTTP status.
            return (
                f"{_PROVIDER} failed: the connection was closed by the service "
                f"(code {status})."
            )
        if status in (401, 403):
            return f"{_PROVIDER} failed: authentication failed (HTTP {status}). The API key is invalid or expired."
        if status == 429:
            return (
                f"{_PROVIDER} failed: too many sessions or requests (HTTP 429). "
                "Wait a moment and try again."
            )
        if isinstance(status, int):
            return f"{_PROVIDER} failed: the service refused the connection (HTTP {status})."
        return self._scrub(f"{_PROVIDER} failed: {error}")

    def _on_message(self, _ws, message) -> None:
        try:
            payload = json.loads(str(message))
        except Exception:
            with self._lock:
                already = self._parse_failure_logged
                self._parse_failure_logged = True
            if not already:
                logger.warning(
                    "ElevenLabs sent a message that is not JSON; it was dropped. "
                    "Logged once per session."
                )
            return
        if not isinstance(payload, dict):
            return
        kind = payload.get("message_type")
        if kind == "session_started":
            with self._lock:
                self._started = True
            self._ready.set()
        elif kind in ("partial_transcript", "committed_transcript"):
            text = payload.get("text")
            if not isinstance(text, str):
                self._fail(
                    f"{_PROVIDER} failed: the service sent a {kind} without text."
                )
                return
            self._take_transcript(
                committed=kind == "committed_transcript", text=text.strip()
            )
        elif kind in _NON_FATAL:
            logger.warning("ElevenLabs: %s: %s", kind, self._detail(payload))
            with self._lock:
                if self._awaiting_answer:
                    self._answered = True
        elif kind == "warning":
            logger.warning(
                "ElevenLabs warning: %s",
                self._clip(payload.get("warning", ""), _MAX_DETAIL_CHARS),
            )
        elif kind in _ERROR_SENTENCES or (
            isinstance(kind, str) and kind.endswith("error") and "error" in payload
        ):
            sentence = _ERROR_SENTENCES.get(kind, "the service reported an error.")
            detail = self._detail(payload)
            suffix = f" ({detail})" if detail and kind not in ("auth_error",) else ""
            self._fail(f"{_PROVIDER} failed: {sentence}{suffix}")

    def _clip(self, value, limit: int) -> str:
        """Scrub the key out first, then cut: a key must not survive as a prefix."""
        return self._scrub(str(value))[:limit]

    def _detail(self, payload: dict) -> str:
        value = payload.get("error")
        return self._clip(value, _MAX_DETAIL_CHARS) if value else ""

    def _take_transcript(self, *, committed: bool, text: str) -> None:
        with self._lock:
            self._last_transcript_at = time.monotonic()
            if committed:
                if text:
                    self._finals.append(text)
                self._partial_text = ""
                if self._awaiting_answer:
                    self._answered = True
            else:
                self._partial_text = text
            combined = self._combined_locked()
            callback = self._on_partial
        if callback is None or not text or not combined:
            return
        try:
            callback(combined)
        except Exception:
            with self._lock:
                already = self._callback_failure_logged
                self._callback_failure_logged = True
            if not already:
                logger.exception(
                    "ElevenLabs streaming partial callback failed; live text is "
                    "not being delivered. Logged once per session."
                )

    def _combined_locked(self) -> str:
        parts = [*self._finals, self._partial_text]
        return " ".join(part for part in parts if part).strip()

    # -- audio --------------------------------------------------------------------

    def push(self, chunk: bytes, *, block_timeout_s: float | None) -> None:
        payload = bytes(chunk or b"")
        if not payload:
            return
        with self._lock:
            if self._state != "active":
                raise TranscriptionError("Streaming session is not active.")
            if block_timeout_s is None:
                # The state check and the offer stay atomic, so a stop that
                # holds the lock cannot have a chunk slip in behind its sentinel.
                try:
                    self._queue.put_nowait(payload)
                except queue.Full as exc:
                    self._record_queue_full_locked()
                    raise self._queue_full_error() from exc
                return
        try:
            self._queue.put(payload, timeout=max(0.0, float(block_timeout_s)))
        except queue.Full as exc:
            with self._lock:
                self._record_queue_full_locked()
            raise self._queue_full_error() from exc

    def _record_queue_full_locked(self) -> None:
        if self._error is None:
            self._error = f"{_PROVIDER} failed: the connection cannot keep up with microphone audio."

    @staticmethod
    def _queue_full_error() -> TranscriptionError:
        return TranscriptionError(
            "ElevenLabs streaming audio queue is full; "
            "the connection cannot keep up with microphone audio."
        )

    def _send_loop(self) -> None:
        """The one thread that writes to the socket (audio and silence)."""
        idle_s = 0.0
        while True:
            try:
                item = self._queue.get(timeout=_KEEPALIVE_S)
            except queue.Empty:
                item = None
                idle_s += _KEEPALIVE_S
            if item is _SENTINEL:
                self._sender_drained.set()
                return
            # Not the state: a stop retires the session first and still needs
            # every queued chunk written before its final commit.
            if self._halted.is_set():
                return
            ws = self._ws
            if item is None:
                if idle_s > _MAX_GAP_S:
                    logger.warning(
                        "ElevenLabs streaming: no audio for %.0f s; the microphone has "
                        "probably stopped delivering. Ending the stream.",
                        idle_s,
                    )
                    self._fail(
                        "The microphone delivered no audio for more than "
                        f"{int(_MAX_GAP_S)} seconds, so the stream was ended."
                    )
                    return
                message, idle_s_next = _chunk_message(_SILENCE, commit=False), idle_s
            else:
                message, idle_s_next = _chunk_message(item, commit=False), 0.0
            try:
                ws.send(message)
            except Exception as exc:
                self._fail(self._describe_socket_error(exc))
                return
            idle_s = idle_s_next

    # -- stop and abort -----------------------------------------------------------

    def finish(self) -> str:
        """Flush the audio, commit the rest, close; the text of the session."""
        with self._lock:
            if self._state != "active":
                if self._state == "starting":
                    self._state = "retiring"
                    self._on_partial = self._on_error = None
                    # Wakes `connect`, which then tears the socket down.
                    self._ready.set()
                raise TranscriptionError("Streaming session is not active.")
            self._state = "retiring"
            # A normal close after the commit is not a runtime failure.
            self._on_error = None
            ws, thread, sender = self._ws, self._thread, self._sender
        try:
            if self._failed_or_closed():
                # Nothing to commit to: the text so far is all there will be.
                pass
            elif self._drain_sender(sender):
                with self._lock:
                    self._awaiting_answer, self._answered = True, False
                    self._last_transcript_at = time.monotonic()
                send_error = _bounded(
                    lambda: ws.send(_chunk_message(b"", commit=True)),
                    timeout_s=_SEND_TIMEOUT_S,
                    name="commit",
                )
                if send_error is None:
                    self._await_final_answer()
                else:
                    self._keep_error(self._describe_socket_error(send_error))
                    logger.warning(
                        "ElevenLabs streaming: the final commit could not be sent; "
                        "the last words may be missing from the text."
                    )
            else:
                self._keep_error(
                    f"{_PROVIDER} failed: queued audio could not be sent in time."
                )
                logger.warning(
                    "ElevenLabs streaming: queued audio could not be sent before the "
                    "stop; the last words may be missing from the text."
                )
        finally:
            self._close_socket()
            if sender is not None and sender.is_alive():
                sender.join(timeout=0.2)
            if thread is not None:
                thread.join(timeout=2.0)
        with self._lock:
            text = self._combined_locked()
            error = self._error
            self._on_partial = None
        if error is not None and not text:
            raise TranscriptionError(error)
        return text

    def _await_final_answer(self) -> None:
        """Wait for the answer to the final commit, and for the server to go quiet."""
        deadline = time.monotonic() + _FINAL_COMMIT_WAIT_S
        while True:
            now = time.monotonic()
            if now >= deadline or self._closed.is_set():
                return
            with self._lock:
                if self._error is not None:
                    return
                done = (
                    self._answered
                    and not self._partial_text
                    and now - self._last_transcript_at >= _FINAL_QUIET_S
                )
            if done:
                return
            time.sleep(0.02)

    def _failed_or_closed(self) -> bool:
        with self._lock:
            return self._error is not None or self._closed.is_set()

    def _keep_error(self, message: str) -> None:
        with self._lock:
            if self._error is None:
                self._error = message

    def _drain_sender(self, sender: threading.Thread | None) -> bool:
        """Put the sentinel behind every queued chunk; true once the sender is done."""
        if sender is None:
            return False
        deadline = time.monotonic() + _DRAIN_TIMEOUT_S
        while True:
            try:
                self._queue.put_nowait(_SENTINEL)
                break
            except queue.Full:
                if not sender.is_alive() or time.monotonic() >= deadline:
                    return False
                time.sleep(0.01)
        if not self._sender_drained.wait(timeout=max(0.0, deadline - time.monotonic())):
            return False
        sender.join(timeout=0.5)
        return not sender.is_alive()

    def abort(self) -> None:
        """Drop the session without waiting for text."""
        with self._lock:
            self._state = "retiring"
            self._on_partial = self._on_error = None
            ws, thread, sender = self._ws, self._thread, self._sender
        self._halted.set()
        self._ready.set()
        if ws is not None:
            self._close_socket()
        try:
            self._queue.put_nowait(_SENTINEL)
        except queue.Full:
            pass
        for worker in (sender, thread):
            if worker is not None and worker.is_alive():
                worker.join(timeout=0.2)

    def _close_socket(self) -> None:
        ws = self._ws
        if ws is None:
            return
        error = _bounded(ws.close, timeout_s=_CLOSE_TIMEOUT_S, name="close")
        if error is not None:
            logger.warning(
                "ElevenLabs streaming socket close failed; the connection may still be open.",
                exc_info=error,
            )
