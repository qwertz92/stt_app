"""OpenAI remote transcription provider.

Batch transcription uses OpenAI's /v1/audio/transcriptions endpoint.
Streaming mode is implemented as chunked partial re-transcription over the same
batch API, compatible with the app's existing streaming controller contract.
"""

from __future__ import annotations

import io
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

from ..config import (
    AUDIO_SAMPLE_RATE,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_OPENAI_MODEL,
    DOC_SSL_PROXY_PATH,
    STREAMING_ABORT_JOIN_TIMEOUT_S,
    STREAMING_PARTIAL_INTERVAL_S,
    STREAMING_PARTIAL_MIN_AUDIO_S,
    STREAMING_PARTIAL_WINDOW_S,
    VALID_LANGUAGE_MODES,
    OPENAI_MODELS,
)
from ..ssl_utils import is_ssl_error as _is_ssl_error
from .base import AudioInput, ITranscriber, StreamingCallback, TranscriptionError

OPENAI_API_BASE = "https://api.openai.com/v1"
_STREAM_SENTINEL = object()


def _multipart_form_data(
    *,
    fields: list[tuple[str, str]],
    file_field: tuple[str, str, bytes, str],
) -> tuple[bytes, str]:
    boundary = f"tts-app-{int(time.time() * 1000)}-{os.getpid()}"
    lines: list[bytes] = []

    for name, value in fields:
        lines.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    "utf-8"
                ),
                f"{value}\r\n".encode("utf-8"),
            ]
        )

    field_name, filename, data, content_type = file_field
    lines.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"),
            data,
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )

    body = b"".join(lines)
    content_type_header = f"multipart/form-data; boundary={boundary}"
    return body, content_type_header


class OpenAITranscriber(ITranscriber):
    def __init__(
        self,
        api_key: str,
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        model: str = DEFAULT_OPENAI_MODEL,
        stream_sample_rate: int = AUDIO_SAMPLE_RATE,
        stream_partial_interval_s: float = STREAMING_PARTIAL_INTERVAL_S,
        stream_partial_min_audio_s: float = STREAMING_PARTIAL_MIN_AUDIO_S,
        stream_partial_window_s: float = STREAMING_PARTIAL_WINDOW_S,
        request_timeout_s: int = 120,
    ) -> None:
        if not api_key:
            raise TranscriptionError(
                "OpenAI API key is missing. "
                "Enter your key in Settings -> Remote Provider API Keys."
            )
        self._api_key = api_key
        self._language_mode = (language_mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        if self._language_mode not in VALID_LANGUAGE_MODES:
            self._language_mode = DEFAULT_LANGUAGE_MODE
        self._model = model if model in OPENAI_MODELS else DEFAULT_OPENAI_MODEL
        self._request_timeout_s = max(5, int(request_timeout_s))

        self.stream_sample_rate = max(1, int(stream_sample_rate))
        self.stream_partial_interval_s = max(0.0, float(stream_partial_interval_s))
        self.stream_partial_min_audio_s = max(0.0, float(stream_partial_min_audio_s))
        self.stream_partial_window_s = max(0.0, float(stream_partial_window_s))

        self._stream_lock = threading.Lock()
        self._stream_active = False
        self._stream_on_partial: StreamingCallback | None = None
        self._stream_queue: queue.Queue[bytes | object] | None = None
        self._stream_thread: threading.Thread | None = None
        self._stream_pcm_buffer = bytearray()
        self._stream_error: Exception | None = None
        self._stream_final_text: str = ""
        self._stream_latest_text: str = ""
        self._stream_last_partial_at = 0.0
        self._stream_last_partial_size = 0
        self._stream_abort_requested = False

    def _auth_header(self) -> str:
        return f"Bearer {self._api_key}"

    def _format_error(self, exc: Exception) -> str:
        if _is_ssl_error(exc):
            return (
                "OpenAI: SSL certificate verification failed "
                "(likely a corporate proxy such as Zscaler). "
                "Set SSL_CERT_FILE or REQUESTS_CA_BUNDLE to your corporate CA .pem. "
                f"See {DOC_SSL_PROXY_PATH} for details."
            )
        return str(exc)

    def _normalize_text(self, value: str) -> str:
        return " ".join(str(value or "").strip().split()).strip()

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        try:
            if isinstance(audio_source, bytes):
                audio_bytes = bytes(audio_source)
                filename = "audio.wav"
            else:
                path = Path(audio_source)
                audio_bytes = path.read_bytes()
                filename = path.name or "audio.wav"

            fields: list[tuple[str, str]] = [("model", self._model)]
            if self._language_mode != DEFAULT_LANGUAGE_MODE:
                fields.append(("language", self._language_mode))
            fields.append(("response_format", "json"))

            body, content_type = _multipart_form_data(
                fields=fields,
                file_field=("file", filename, audio_bytes, "audio/wav"),
            )

            req = urllib.request.Request(
                f"{OPENAI_API_BASE}/audio/transcriptions",
                data=body,
                method="POST",
            )
            req.add_header("Authorization", self._auth_header())
            req.add_header("Content-Type", content_type)

            with urllib.request.urlopen(req, timeout=self._request_timeout_s) as resp:
                payload = resp.read()

            try:
                parsed = json.loads(payload.decode("utf-8", errors="replace"))
            except Exception:
                return self._normalize_text(payload.decode("utf-8", errors="replace"))

            if isinstance(parsed, dict):
                text = parsed.get("text", "")
                return self._normalize_text(str(text))
            return self._normalize_text(str(parsed))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise TranscriptionError(
                    "OpenAI: Authentication failed (HTTP 401). "
                    "The API key is invalid or expired."
                ) from exc
            if exc.code == 429:
                raise TranscriptionError(
                    "OpenAI: Rate limit exceeded (HTTP 429). "
                    "Wait a moment and try again."
                ) from exc
            detail = exc.reason or "unknown error"
            raise TranscriptionError(
                f"OpenAI transcription failed (HTTP {exc.code}): {detail}"
            ) from exc
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(
                f"OpenAI transcription failed: {self._format_error(exc)}"
            ) from exc

    def test_connection(self) -> tuple[bool, str]:
        model_name = urllib.parse.quote(self._model, safe="")
        req = urllib.request.Request(
            f"{OPENAI_API_BASE}/models/{model_name}",
            method="GET",
        )
        req.add_header("Authorization", self._auth_header())
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
            return False, f"Connection failed: {self._format_error(exc)}"
        return False, "Unexpected response from OpenAI API."

    def start_stream(self, on_partial: StreamingCallback | None = None) -> None:
        with self._stream_lock:
            if self._stream_active:
                raise TranscriptionError("Streaming session already active.")
            self._stream_active = True
            self._stream_on_partial = on_partial
            self._stream_queue = queue.Queue()
            self._stream_pcm_buffer = bytearray()
            self._stream_error = None
            self._stream_final_text = ""
            self._stream_latest_text = ""
            self._stream_last_partial_at = time.monotonic()
            self._stream_last_partial_size = 0
            self._stream_abort_requested = False
            thread = threading.Thread(
                target=self._stream_worker,
                name="tts_app_openai_stream_worker",
                daemon=True,
            )
            self._stream_thread = thread
        thread.start()

    def push_audio_chunk(self, chunk: bytes) -> None:
        payload = bytes(chunk or b"")
        if not payload:
            return
        with self._stream_lock:
            stream_active = self._stream_active
            stream_queue = self._stream_queue
        if not stream_active or stream_queue is None:
            raise TranscriptionError("Streaming session is not active.")
        stream_queue.put(payload)

    def stop_stream(self) -> str:
        with self._stream_lock:
            if not self._stream_active:
                raise TranscriptionError("Streaming session is not active.")
            stream_queue = self._stream_queue
            stream_thread = self._stream_thread

        if stream_queue is None or stream_thread is None:
            raise TranscriptionError("Streaming session was not initialized correctly.")

        stream_queue.put(_STREAM_SENTINEL)
        stream_thread.join()

        with self._stream_lock:
            stream_error = self._stream_error
            text = self._stream_final_text
            self._reset_stream_fields()

        if stream_error is not None:
            detail = self._format_error(
                stream_error
                if isinstance(stream_error, Exception)
                else Exception(str(stream_error))
            )
            raise TranscriptionError(f"OpenAI streaming failed: {detail}") from stream_error
        return text.strip()

    def abort_stream(self) -> None:
        with self._stream_lock:
            if not self._stream_active:
                return
            self._stream_abort_requested = True
            stream_queue = self._stream_queue
            stream_thread = self._stream_thread

        if stream_queue is not None:
            stream_queue.put(_STREAM_SENTINEL)
        if stream_thread is not None:
            stream_thread.join(timeout=STREAMING_ABORT_JOIN_TIMEOUT_S)

        with self._stream_lock:
            self._reset_stream_fields()

    def _reset_stream_fields(self) -> None:
        self._stream_active = False
        self._stream_on_partial = None
        self._stream_queue = None
        self._stream_thread = None
        self._stream_pcm_buffer = bytearray()
        self._stream_error = None
        self._stream_final_text = ""
        self._stream_latest_text = ""
        self._stream_last_partial_size = 0
        self._stream_last_partial_at = 0.0
        self._stream_abort_requested = False

    def _stream_worker(self) -> None:
        while True:
            with self._stream_lock:
                stream_queue = self._stream_queue
                if self._stream_abort_requested:
                    return
            if stream_queue is None:
                return

            item = stream_queue.get()
            if item is _STREAM_SENTINEL:
                break

            if isinstance(item, (bytes, bytearray)) and item:
                with self._stream_lock:
                    self._stream_pcm_buffer.extend(item)

            self._maybe_emit_partial()

        with self._stream_lock:
            aborted = self._stream_abort_requested

        if aborted:
            with self._stream_lock:
                if self._stream_final_text == "":
                    self._stream_final_text = self._stream_latest_text
            return

        try:
            final_text = self._transcribe_current_stream_buffer()
        except Exception as exc:
            with self._stream_lock:
                self._stream_error = exc
            return

        with self._stream_lock:
            self._stream_final_text = final_text

    def _maybe_emit_partial(self) -> None:
        with self._stream_lock:
            callback = self._stream_on_partial
            if self._stream_abort_requested:
                return
            now = time.monotonic()
            elapsed = now - self._stream_last_partial_at
            min_audio_bytes = int(
                self.stream_partial_min_audio_s * self.stream_sample_rate * 2
            )
            current_size = len(self._stream_pcm_buffer)
            has_new_audio = current_size > self._stream_last_partial_size
            should_emit = (
                callback is not None
                and has_new_audio
                and current_size >= min_audio_bytes
                and elapsed >= self.stream_partial_interval_s
            )

        if not should_emit:
            return

        try:
            text = self._transcribe_current_stream_buffer(
                max_window_seconds=self.stream_partial_window_s
            )
        except Exception as exc:
            with self._stream_lock:
                self._stream_error = exc
            return

        with self._stream_lock:
            self._stream_latest_text = text

        if callback is not None and text.strip():
            try:
                callback(text)
            except Exception:
                pass

        with self._stream_lock:
            self._stream_last_partial_at = time.monotonic()
            self._stream_last_partial_size = len(self._stream_pcm_buffer)

    def _transcribe_current_stream_buffer(
        self,
        max_window_seconds: float | None = None,
    ) -> str:
        with self._stream_lock:
            snapshot = bytes(self._stream_pcm_buffer)
        if not snapshot:
            return ""
        if max_window_seconds is not None and max_window_seconds > 0:
            max_bytes = int(max_window_seconds * self.stream_sample_rate * 2)
            if max_bytes > 0 and len(snapshot) > max_bytes:
                snapshot = snapshot[-max_bytes:]
        wav_bytes = self._pcm16_to_wav_bytes(snapshot)
        return self.transcribe_batch(wav_bytes)

    def _pcm16_to_wav_bytes(self, pcm_bytes: bytes) -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.stream_sample_rate)
            wav_file.writeframes(pcm_bytes)
        return buffer.getvalue()
