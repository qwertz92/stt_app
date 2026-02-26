"""Deepgram remote transcription provider.

Batch transcription via Deepgram's REST API (direct HTTP, no SDK needed).
Streaming transcription via Deepgram WebSocket API (`/v1/listen`).
API key stored via keyring (settings_dialog / secret_store).

Uses the nova-3 model by default (Deepgram's most capable speech model).
No Deepgram SDK required; streaming needs `websocket-client`.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..config import AUDIO_SAMPLE_RATE, DOC_SSL_PROXY_PATH
from ..ssl_utils import create_ssl_context, is_ssl_error as _is_ssl_error
from .base import AudioInput, ITranscriber, StreamingCallback, TranscriptionError

DEEPGRAM_API_BASE = "https://api.deepgram.com/v1"
DEFAULT_DEEPGRAM_MODEL = "nova-3"


class DeepgramTranscriber(ITranscriber):
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
    ) -> None:
        if not api_key:
            raise TranscriptionError(
                "Deepgram API key is missing. "
                "Enter your key in Settings → Remote Provider API Keys."
            )
        self._api_key = api_key
        self._language_mode = (language_mode or "auto").strip().lower()
        self._model = model or DEFAULT_DEEPGRAM_MODEL
        self._stream_lock = threading.Lock()
        self._stream_ws = None
        self._stream_thread: threading.Thread | None = None
        self._stream_ws_module = None
        self._stream_connected = threading.Event()
        self._stream_closed = threading.Event()
        self._stream_error: Exception | None = None
        self._stream_on_partial: StreamingCallback | None = None
        self._stream_finals: list[str] = []
        self._stream_partial_text = ""

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        """Transcribe audio via Deepgram pre-recorded API.

        Accepts WAV bytes, a file path, or a Path object.
        """
        try:
            if isinstance(audio_source, bytes):
                audio_data = audio_source
            else:
                file_path = Path(audio_source)
                audio_data = file_path.read_bytes()

            # Build query parameters.
            params: dict[str, str] = {
                "model": self._model,
                "smart_format": "true",
            }

            if self._language_mode == "auto":
                params["detect_language"] = "true"
            else:
                params["language"] = self._language_mode

            url = f"{DEEPGRAM_API_BASE}/listen?{urllib.parse.urlencode(params)}"

            req = urllib.request.Request(url, data=audio_data, method="POST")
            req.add_header("Authorization", f"Token {self._api_key}")
            req.add_header("Content-Type", "audio/wav")

            ssl_ctx = create_ssl_context()
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
            raise TranscriptionError(
                f"Deepgram transcription failed: {exc}"
            ) from exc

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

    def _stream_combined_text(self) -> str:
        parts = list(self._stream_finals)
        if self._stream_partial_text:
            parts.append(self._stream_partial_text)
        return " ".join(p for p in parts if p).strip()

    def start_stream(self, on_partial: StreamingCallback | None = None) -> None:
        with self._stream_lock:
            if self._stream_ws is not None:
                raise TranscriptionError("Streaming session already active.")

        websocket = self._get_websocket_module()
        params: dict[str, str] = {
            "model": self._model,
            "encoding": "linear16",
            "sample_rate": str(AUDIO_SAMPLE_RATE),
            "channels": "1",
            "interim_results": "true",
            "smart_format": "true",
        }
        if self._language_mode == "auto":
            params["detect_language"] = "true"
        else:
            params["language"] = self._language_mode

        url = f"wss://api.deepgram.com/v1/listen?{urllib.parse.urlencode(params)}"

        self._stream_connected.clear()
        self._stream_closed.clear()
        self._stream_error = None
        self._stream_on_partial = on_partial
        self._stream_finals = []
        self._stream_partial_text = ""

        def _on_open(_ws):
            self._stream_connected.set()

        def _on_message(_ws, message):
            self._handle_stream_message(message)

        def _on_error(_ws, error):
            if isinstance(error, Exception):
                self._stream_error = error
            else:
                self._stream_error = Exception(str(error))

        def _on_close(_ws, _status, _reason):
            self._stream_closed.set()

        ws = websocket.WebSocketApp(
            url,
            header=[f"Authorization: Token {self._api_key}"],
            on_open=_on_open,
            on_message=_on_message,
            on_error=_on_error,
            on_close=_on_close,
        )
        thread = threading.Thread(
            target=ws.run_forever,
            name="tts_app_deepgram_stream",
            daemon=True,
        )
        with self._stream_lock:
            self._stream_ws = ws
            self._stream_thread = thread
        thread.start()

        if self._stream_connected.wait(timeout=8.0):
            return

        with self._stream_lock:
            active_ws = self._stream_ws
            self._stream_ws = None
            self._stream_thread = None
        if active_ws is not None:
            try:
                active_ws.close()
            except Exception:
                pass

        error = self._stream_error
        if error is not None:
            detail = str(error)
            if _is_ssl_error(error):
                detail = (
                    "SSL certificate verification failed (likely a corporate "
                    f"proxy). See {DOC_SSL_PROXY_PATH} for details."
                )
            raise TranscriptionError(f"Deepgram streaming failed to connect: {detail}") from error
        raise TranscriptionError("Deepgram streaming failed to connect (timeout).")

    def push_audio_chunk(self, chunk: bytes) -> None:
        payload = bytes(chunk or b"")
        if not payload:
            return
        with self._stream_lock:
            ws = self._stream_ws
        if ws is None:
            raise TranscriptionError("Streaming session is not active.")

        websocket = self._get_websocket_module()
        try:
            ws.send(payload, opcode=websocket.ABNF.OPCODE_BINARY)
        except Exception as exc:
            raise TranscriptionError(
                f"Deepgram streaming: failed to send audio: {exc}"
            ) from exc

    def stop_stream(self) -> str:
        with self._stream_lock:
            ws = self._stream_ws
            thread = self._stream_thread
            self._stream_ws = None
            self._stream_thread = None

        if ws is None or thread is None:
            raise TranscriptionError("Streaming session is not active.")

        try:
            ws.send(json.dumps({"type": "Finalize"}))
        except Exception:
            pass
        try:
            ws.close()
        except Exception:
            pass
        thread.join(timeout=5.0)

        text = self._stream_combined_text()
        error = self._stream_error
        self._stream_on_partial = None
        self._stream_finals = []
        self._stream_partial_text = ""
        self._stream_connected.clear()
        self._stream_closed.clear()
        self._stream_error = None

        if error is not None and not text:
            detail = str(error)
            if _is_ssl_error(error):
                detail = (
                    "SSL certificate verification failed (likely a corporate "
                    f"proxy). See {DOC_SSL_PROXY_PATH} for details."
                )
            raise TranscriptionError(f"Deepgram streaming failed: {detail}") from error
        return text

    def abort_stream(self) -> None:
        with self._stream_lock:
            ws = self._stream_ws
            thread = self._stream_thread
            self._stream_ws = None
            self._stream_thread = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout=0.2)

        self._stream_on_partial = None
        self._stream_finals = []
        self._stream_partial_text = ""
        self._stream_connected.clear()
        self._stream_closed.clear()
        self._stream_error = None

    def _handle_stream_message(self, message) -> None:
        try:
            payload = json.loads(str(message))
        except Exception:
            return
        if not isinstance(payload, dict):
            return

        channel = payload.get("channel")
        if not isinstance(channel, dict):
            return
        alternatives = channel.get("alternatives")
        if not isinstance(alternatives, list) or not alternatives:
            return
        first = alternatives[0]
        if not isinstance(first, dict):
            return
        transcript = str(first.get("transcript", "")).strip()
        if not transcript:
            return

        is_final = bool(payload.get("is_final", False))
        if is_final:
            self._stream_finals.append(transcript)
            self._stream_partial_text = ""
        else:
            self._stream_partial_text = transcript

        callback = self._stream_on_partial
        if callback is None:
            return
        combined = self._stream_combined_text()
        if not combined:
            return
        try:
            callback(combined)
        except Exception:
            pass
