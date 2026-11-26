"""One Soniox realtime session: a WebSocket, a sender thread and its text.

`wss://stt-rt.soniox.com/transcribe-websocket` (`stt-rt.<region>.soniox.com`
in a region project). The key goes in the `Authorization: Bearer <key>` header
of the connection (the `api_key` field of the start message is deprecated and
refused after 2027-01-15); the first text frame is the JSON start message
(`model`, `audio_format`, `sample_rate`, `num_channels`, `language_hints`,
`context`); audio follows as binary frames of raw PCM; the answer is a stream of
JSON messages `{"tokens": [{"text", "is_final", ...}], "final_audio_proc_ms",
"total_audio_proc_ms"}` in which a final token arrives once and the non-final
tokens are the whole provisional tail, replaced by each message. An empty text
frame ends the stream; the server then sends the remaining tokens and
`{"finished": true}` and closes. An error is a message with `error_code`,
`error_type` and `error_message`, after which the server closes. Pages read
2026-11-26 (https://docs.soniox.com/api-reference/stt/websocket-api.mdx,
.../authentication/websocket-authentication.mdx, .../stt/rt/connection-keepalive.mdx);
`docs/agents/remote-providers.md` lists them with the rest.

A session is one object, so a callback of an earlier session can never touch a
later one: the transcriber holds the current session and drops it when it ends.
"""

from __future__ import annotations

import json
import logging
import math
import queue
import threading
import time

from ..config import (
    AUDIO_BLOCK_DURATION_MS,
    AUDIO_INPUT_BUFFER_S,
)
from ..ssl_utils import is_ssl_error as _is_ssl_error
from ._http_utils import Redactor, format_ssl_error_message
from .base import StreamingCallback, StreamingErrorCallback, TranscriptionError

logger = logging.getLogger(__name__)

_PROVIDER = "Soniox"
_SENDER_STOP = object()
_KEEPALIVE_DUE = object()
# "If no message arrives within [20 s], the stream ends with a 408
# `request_timeout` error" and any client message resets it, so the sender
# writes `{"type": "keepalive"}` whenever its queue stayed empty this long: a
# start stall or a stop's backlog wait can be 12 s with the socket open. Steady
# audio never reaches the interval. The vendor bills "the full stream
# duration, not only the audio processed", so a bridged gap costs its length.
_KEEPALIVE_INTERVAL_S = 5.0
# One gap is bridged for at most this long, as for Deepgram: the capture's
# input buffer (a starved callback thread delivers up to that much as a burst)
# plus a margin. Past it the microphone is dead, not slow, and the sender ends
# the stream with a message that names the cause instead of leaving the overlay
# on "Listening" until the hotkey.
_KEEPALIVE_MAX_GAP_S = AUDIO_INPUT_BUFFER_S + 10.0
# The bound of a starved callback thread's burst plus the 32 blocks (3.2 s) the
# queue held before the capture buffer grew (`docs/agents/streaming.md`).
_AUDIO_QUEUE_MAX_CHUNKS = (
    math.ceil(AUDIO_INPUT_BUFFER_S * 1000 / AUDIO_BLOCK_DURATION_MS) + 32
)
# "Maximum 2,097,144 bytes per message. A larger message ends the stream with a
# 400 error." A block is 3.2 kB; only a caller handing over more is split.
_AUDIO_MESSAGE_MAX_BYTES = 2_097_144
_CONNECT_TIMEOUT_S = 8.0
_SENDER_DRAIN_TIMEOUT_S = 2.0
_CONTROL_SEND_TIMEOUT_S = 1.0
# How long a stop waits for `{"finished": true}` after the end-of-stream frame.
# Past it the text received so far is returned (never discarded) and the wait
# is logged: the audio is already with Soniox, and the remaining tokens are the
# last words of the dictation.
_FINISH_WAIT_S = 10.0
_SOCKET_CLOSE_TIMEOUT_S = 1.0
# Markers the model puts into the token stream (`<end>` after endpoint
# detection, `<fin>` after a finalize); the app asks for neither, and a marker
# is never text.
_MARKER_TOKENS = frozenset({"<end>", "<fin>"})
_KEEPALIVE_MESSAGE = json.dumps({"type": "keepalive"})


class SonioxStreamSession:
    def __init__(
        self,
        *,
        websocket,
        url: str,
        host: str,
        api_key: str,
        scrub: Redactor,
        start_message: str,
        on_partial: StreamingCallback | None,
        on_error: StreamingErrorCallback | None,
    ) -> None:
        self._websocket = websocket
        self._url = url
        self._host = host
        self._api_key = api_key
        self._scrub = scrub
        self._start_message = start_message
        self._on_partial = on_partial
        self._on_error = on_error
        self._lock = threading.Lock()
        # new -> connecting -> active -> ending -> closed
        self._state = "new"
        self._ws = None
        self._thread: threading.Thread | None = None
        self._sender: threading.Thread | None = None
        self._queue: queue.Queue = queue.Queue(maxsize=_AUDIO_QUEUE_MAX_CHUNKS)
        self._connected = threading.Event()
        self._closed = threading.Event()
        self._finished = threading.Event()
        self._sender_drained = threading.Event()
        self._error: Exception | None = None
        self._error_reported = False
        self._callback_failed = False
        self._parse_failed = False
        self._finals: list[str] = []
        self._partial = ""
        self._last_emitted = ""

    # -- text ---------------------------------------------------------------

    def _combined_locked(self) -> str:
        return "".join([*self._finals, self._partial]).strip()

    def _format_error(self, error: Exception) -> str:
        if _is_ssl_error(error):
            return (
                f"{_PROVIDER} streaming failed: {format_ssl_error_message(_PROVIDER)}"
            )
        return f"{_PROVIDER} streaming failed: {self._scrub(str(error))}"

    # -- errors -------------------------------------------------------------

    def _record_error(self, error: Exception, *, notify: bool) -> None:
        with self._lock:
            if self._state in ("closed", "new"):
                return
            if self._error is None:
                self._error = error
        if notify:
            self._notify(error)

    def _notify(self, error: Exception) -> None:
        with self._lock:
            callback = self._on_error
            if (
                self._state not in ("connecting", "active")
                or callback is None
                or self._error_reported
            ):
                return
            self._error_reported = True
        try:
            callback(self._format_error(error))
        except Exception:
            # Once per session by `_error_reported`; the callback is the only
            # way a stream failure reaches the user, so its failure is logged.
            logger.exception(
                "Soniox streaming error callback failed; the failure was not reported."
            )

    def _error_from_frame(self, payload: dict) -> TranscriptionError:
        code = payload.get("error_code")
        kind = str(payload.get("error_type") or "error")
        detail = str(payload.get("error_message") or "").strip()
        if code in (401, 403):
            return TranscriptionError(
                f"authentication failed (HTTP {code}): the API key is invalid or "
                "expired, or belongs to a project in another region than the "
                f"one selected ({self._host})"
            )
        if code == 402:
            return TranscriptionError(
                f"the balance or budget is used up (HTTP 402, {kind}): {detail}"
            )
        return TranscriptionError(f"{detail or kind} ({kind}, HTTP {code})")

    # -- socket callbacks ---------------------------------------------------

    def _handle_open(self, ws) -> None:
        with self._lock:
            if self._state != "connecting":
                return
        try:
            ws.send(self._start_message)
        except Exception as exc:
            self._record_error(exc, notify=False)
        self._connected.set()

    def _handle_error(self, ws, error) -> None:
        if not isinstance(error, Exception):
            error = RuntimeError(str(error))
        self._record_error(error, notify=True)
        with self._lock:
            if self._state == "connecting":
                self._connected.set()

    def _handle_close(self, ws, _status, _reason) -> None:
        unexpected = None
        with self._lock:
            self._closed.set()
            if (
                self._state in ("connecting", "active")
                and self._error is None
                and not self._finished.is_set()
            ):
                unexpected = RuntimeError("WebSocket connection closed unexpectedly.")
                self._error = unexpected
            if self._state == "connecting":
                self._connected.set()
        if unexpected is not None:
            self._notify(unexpected)

    def _handle_message(self, ws, message) -> None:
        try:
            payload = json.loads(
                message.decode("utf-8", errors="replace")
                if isinstance(message, (bytes, bytearray))
                else str(message)
            )
        except Exception:
            with self._lock:
                already = self._parse_failed
                self._parse_failed = True
            if not already:
                logger.warning(
                    "Soniox sent a message that is not JSON; it was dropped. "
                    "Logged once per session."
                )
            return
        if not isinstance(payload, dict):
            return
        if payload.get("error_code") is not None or payload.get("error_message"):
            error = self._error_from_frame(payload)
            self._record_error(error, notify=True)
            with self._lock:
                self._finished.set()
            return
        finals: list[str] = []
        provisional: list[str] = []
        tokens = payload.get("tokens")
        for token in tokens if isinstance(tokens, list) else []:
            text = token.get("text") if isinstance(token, dict) else None
            if not isinstance(text, str) or text in _MARKER_TOKENS:
                continue
            (finals if token.get("is_final") is True else provisional).append(text)
        ended_early = False
        with self._lock:
            if self._state not in ("connecting", "active", "ending"):
                return
            self._finals.extend(finals)
            self._partial = "".join(provisional)
            combined = self._combined_locked()
            emit = bool(combined) and combined != self._last_emitted
            if emit:
                self._last_emitted = combined
            callback = self._on_partial
            if payload.get("finished") is True:
                self._finished.set()
                ended_early = self._state == "active"
        if ended_early:
            self._record_error(
                RuntimeError("the service ended the stream before it was stopped"),
                notify=True,
            )
        if not emit or callback is None:
            return
        try:
            callback(combined)
        except Exception:
            with self._lock:
                already = self._callback_failed
                self._callback_failed = True
            if not already:
                logger.exception(
                    "Soniox streaming partial callback failed; live text is "
                    "not being delivered. Logged once per session."
                )

    # -- lifecycle ----------------------------------------------------------

    def connect(self) -> None:
        """Open the socket and send the start message; the sender starts once
        the connection is up. Raises `TranscriptionError` and leaves nothing
        open when it cannot."""
        try:
            ws = self._websocket.WebSocketApp(
                self._url,
                header=[f"Authorization: Bearer {self._api_key}"],
                on_open=self._handle_open,
                on_message=self._handle_message,
                on_error=self._handle_error,
                on_close=self._handle_close,
            )
        except Exception as exc:
            raise TranscriptionError(
                f"{_PROVIDER} streaming failed to initialize: {self._scrub(str(exc))}"
            ) from exc
        thread = threading.Thread(
            target=ws.run_forever, name="stt_app_soniox_stream", daemon=True
        )
        sender = threading.Thread(
            target=self._send_worker, name="stt_app_soniox_send", daemon=True
        )
        with self._lock:
            if self._state != "new":
                raise TranscriptionError("Streaming session already used.")
            self._state = "connecting"
            self._ws = ws
            self._thread = thread
            self._sender = sender
        failure: Exception | None = None
        try:
            thread.start()
        except Exception as exc:
            failure = exc
        if failure is None:
            self._connected.wait(timeout=_CONNECT_TIMEOUT_S)
            with self._lock:
                ready = (
                    self._state == "connecting"
                    and self._connected.is_set()
                    and not self._closed.is_set()
                    and self._error is None
                )
                if ready:
                    try:
                        sender.start()
                    except Exception as exc:
                        failure = exc
                        self._error = exc
                    else:
                        self._state = "active"
                        return
                state = self._state
                failure = failure or self._error
        else:
            with self._lock:
                state = self._state
        with self._lock:
            self._state = "closed"
        self._close_socket(ws)
        thread.join(timeout=2.0)
        if failure is not None:
            detail = (
                format_ssl_error_message(_PROVIDER)
                if _is_ssl_error(failure)
                else self._scrub(str(failure))
            )
            raise TranscriptionError(
                f"{_PROVIDER} streaming failed to connect: {detail}"
            ) from failure
        if state != "connecting":
            raise TranscriptionError(
                f"{_PROVIDER} streaming session was stopped while connecting."
            )
        raise TranscriptionError(f"{_PROVIDER} streaming failed to connect (timeout).")

    def push(self, chunk: bytes, block_timeout_s: float | None) -> None:
        """Queue a PCM16 chunk for the sender (never blocks the audio callback).

        `block_timeout_s` is `None` on the PortAudio callback: a full queue
        fails the stream instead of waiting. A number is a caller that may wait
        (the controller's preconnect flush hands over seconds of audio at once),
        with the lock released so a stop or an abort is never held up by it.
        """
        payload = bytes(chunk or b"")
        if not payload:
            return
        pieces = [
            payload[start : start + _AUDIO_MESSAGE_MAX_BYTES]
            for start in range(0, len(payload), _AUDIO_MESSAGE_MAX_BYTES)
        ]
        for piece in pieces:
            with self._lock:
                if self._state != "active":
                    raise TranscriptionError("Streaming session is not active.")
                if block_timeout_s is None:
                    try:
                        self._queue.put_nowait(piece)
                    except queue.Full as exc:
                        self._queue_full_locked()
                        raise self._queue_full_error() from exc
                    continue
            try:
                self._queue.put(piece, timeout=max(0.0, float(block_timeout_s)))
            except queue.Full as exc:
                with self._lock:
                    self._queue_full_locked()
                raise self._queue_full_error() from exc

    def _queue_full_locked(self) -> None:
        if self._error is None:
            self._error = RuntimeError(
                "audio queue is full because the WebSocket sender fell behind"
            )

    @staticmethod
    def _queue_full_error() -> TranscriptionError:
        return TranscriptionError(
            f"{_PROVIDER} streaming audio queue is full; the connection cannot "
            "keep up with microphone audio."
        )

    def _send_worker(self) -> None:
        """The one thread that writes to the socket: audio, and the keepalive
        when the queue stayed empty for an interval, so the two never overlap
        and no keepalive follows the stop's barrier."""
        ws = self._ws
        idle_s = 0.0
        announced = False
        while True:
            try:
                item = self._queue.get(timeout=_KEEPALIVE_INTERVAL_S)
            except queue.Empty:
                item = _KEEPALIVE_DUE
                idle_s += _KEEPALIVE_INTERVAL_S
            if item is _SENDER_STOP:
                self._sender_drained.set()
                return
            with self._lock:
                if self._state not in ("active", "ending"):
                    return
            if item is _KEEPALIVE_DUE and idle_s > _KEEPALIVE_MAX_GAP_S:
                logger.warning(
                    "Soniox streaming: no audio for %.0f s, longer than the "
                    "capture's input buffer can account for; the microphone has "
                    "probably stopped delivering. Ending the stream.",
                    idle_s,
                )
                self._record_error(
                    TranscriptionError(
                        "The microphone delivered no audio for more than "
                        f"{int(_KEEPALIVE_MAX_GAP_S)} seconds, so the stream was ended."
                    ),
                    notify=True,
                )
                return
            try:
                if item is _KEEPALIVE_DUE:
                    if not announced:
                        announced = True
                        logger.info(
                            "Soniox streaming: no audio for %.0f s, sending "
                            "keepalive frames. Logged once per session.",
                            _KEEPALIVE_INTERVAL_S,
                        )
                    ws.send(_KEEPALIVE_MESSAGE)
                else:
                    ws.send(item, opcode=self._websocket.ABNF.OPCODE_BINARY)
                    idle_s = 0.0
            except Exception as exc:
                self._record_error(exc, notify=True)
                return

    def _drain_sender(self) -> bool:
        sender = self._sender
        if sender is None:
            return False
        deadline = time.monotonic() + _SENDER_DRAIN_TIMEOUT_S
        while True:
            try:
                self._queue.put_nowait(_SENDER_STOP)
                break
            except queue.Full:
                if not sender.is_alive() or time.monotonic() >= deadline:
                    return False
                time.sleep(0.01)
        while not self._sender_drained.is_set():
            if not sender.is_alive() or time.monotonic() >= deadline:
                return False
            self._sender_drained.wait(timeout=min(0.05, deadline - time.monotonic()))
        sender.join(timeout=max(0.0, deadline - time.monotonic()))
        return not sender.is_alive()

    def _send_end_of_stream(self, ws) -> Exception | None:
        """The empty text frame, on a short-lived thread: a send can hang."""
        done = threading.Event()
        errors: list[Exception] = []

        def send() -> None:
            try:
                ws.send("")
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        try:
            threading.Thread(
                target=send, name="stt_app_soniox_end", daemon=True
            ).start()
        except Exception as exc:
            return exc
        if not done.wait(timeout=_CONTROL_SEND_TIMEOUT_S):
            return TimeoutError("timed out sending the end of the stream")
        return errors[0] if errors else None

    def _wait_for_finish(self) -> bool:
        """Whether `finished` (or an error frame) arrived before the bound; a
        socket that closed without it ends the wait too."""
        deadline = time.monotonic() + _FINISH_WAIT_S
        while not self._finished.is_set():
            if self._closed.is_set():
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._finished.wait(timeout=min(0.05, remaining))
        return True

    def _close_socket(self, ws) -> None:
        def close() -> None:
            try:
                ws.close()
            except Exception:
                logger.warning(
                    "Soniox streaming socket close failed; the connection may "
                    "still be open.",
                    exc_info=True,
                )

        worker = threading.Thread(
            target=close, name="stt_app_soniox_close", daemon=True
        )
        try:
            worker.start()
        except Exception:
            logger.warning(
                "Could not start the Soniox close thread; the connection may "
                "stay open until the server times it out.",
                exc_info=True,
            )
            return
        worker.join(timeout=_SOCKET_CLOSE_TIMEOUT_S)

    def stop(self) -> str:
        """Flush the audio, end the stream, wait for the last tokens, close.

        Returns the text (finals plus the provisional tail); raises only when
        there is none and something failed.
        """
        with self._lock:
            connecting = self._state == "connecting"
            if self._state != "active" and not connecting:
                raise TranscriptionError("Streaming session is not active.")
            if not connecting:
                self._state = "ending"
        if connecting:
            # A stop during the handshake: `connect` would otherwise finish
            # into a session nobody owns. It sees the closed state and raises.
            self.abort()
            raise TranscriptionError("Streaming session is not active.")
        with self._lock:
            # A normal server close is not a runtime failure any more.
            self._on_error = None
            ws, thread, sender = self._ws, self._thread, self._sender
        try:
            if not self._drain_sender():
                self._record_error(
                    RuntimeError("timed out while draining queued streaming audio"),
                    notify=False,
                )
            else:
                error = self._send_end_of_stream(ws)
                if error is not None:
                    self._record_error(error, notify=False)
                elif not self._wait_for_finish():
                    logger.warning(
                        "Soniox streaming: no 'finished' message within %.0f s "
                        "of the end of the stream; the text received so far is "
                        "returned.",
                        _FINISH_WAIT_S,
                    )
        finally:
            if not self._closed.is_set():
                self._close_socket(ws)
            if sender is not None and sender.is_alive():
                sender.join(timeout=0.2)
            if thread is not None:
                thread.join(timeout=2.0)
            with self._lock:
                text = self._combined_locked()
                error = self._error
                self._state = "closed"
        if error is not None and not text:
            raise TranscriptionError(self._format_error(error)) from error
        return text

    def abort(self) -> None:
        """Drop the session without waiting for the service."""
        with self._lock:
            if self._state in ("new", "closed"):
                self._state = "closed"
                return
            self._state = "closed"
            self._on_partial = None
            self._on_error = None
            ws, thread, sender = self._ws, self._thread, self._sender
        self._connected.set()
        try:
            if ws is not None:
                self._close_socket(ws)
            try:
                self._queue.put_nowait(_SENDER_STOP)
            except queue.Full:
                pass
            if sender is not None and sender.is_alive():
                sender.join(timeout=0.2)
            if thread is not None and thread.is_alive():
                thread.join(timeout=0.2)
        finally:
            self._closed.set()
