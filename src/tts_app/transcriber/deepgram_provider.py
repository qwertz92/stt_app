"""Deepgram remote transcription provider.

Batch transcription via Deepgram's REST API (direct HTTP, no SDK needed).
API key stored via keyring (settings_dialog / secret_store).

Uses the nova-3 model by default (Deepgram's most capable speech model).
No additional Python package required — uses stdlib urllib.
"""

from __future__ import annotations

import json
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

from ..config import AUDIO_CHANNELS, AUDIO_SAMPLE_RATE
from ..ssl_utils import is_ssl_error as _is_ssl_error
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

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        """Transcribe audio via Deepgram pre-recorded API.

        Accepts WAV bytes, a file path, or a Path object.
        """
        temp_path: Path | None = None
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

            with urllib.request.urlopen(req, timeout=120) as resp:
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
                    "corporate CA .pem, or switch to the local provider."
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
                    "corporate proxy (Zscaler). Set SSL_CERT_FILE or "
                    "REQUESTS_CA_BUNDLE to your corporate CA .pem file."
                )
            return False, f"Connection failed: {exc}"

        return False, "Unexpected response from Deepgram API."

    # -- Streaming stubs --------------------------------------------------------

    def start_stream(self, on_partial: StreamingCallback | None = None) -> None:
        raise NotImplementedError(
            "Deepgram streaming is not yet implemented. "
            "Use batch mode with Deepgram, or use local/AssemblyAI for streaming."
        )

    def push_audio_chunk(self, chunk: bytes) -> None:
        raise NotImplementedError("Deepgram streaming is not yet implemented.")

    def stop_stream(self) -> str:
        raise NotImplementedError("Deepgram streaming is not yet implemented.")

    def abort_stream(self) -> None:
        raise NotImplementedError("Deepgram streaming is not yet implemented.")
