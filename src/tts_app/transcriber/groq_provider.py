"""Groq remote transcription provider.

Batch transcription via the Groq Python SDK.
Requires: pip install groq
API key stored via keyring (settings_dialog / secret_store).

Supported models: whisper-large-v3, whisper-large-v3-turbo.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from ..config import DEFAULT_GROQ_MODEL, DOC_SSL_PROXY_PATH
from ..ssl_utils import create_ssl_context, is_ssl_error as _is_ssl_error
from .base import AudioInput, ITranscriber, StreamingCallback, TranscriptionError


def _default_groq():
    """Lazy import to avoid hard dependency at module level."""
    try:
        from groq import Groq  # type: ignore

        return Groq
    except ImportError:
        raise TranscriptionError(
            "The 'groq' package is not installed. "
            "Install it with: pip install groq  "
            "(or: uv add groq)"
        )


class GroqTranscriber(ITranscriber):
    """Batch transcription using Groq's audio transcription API.

    Parameters
    ----------
    api_key : str
        Groq API key (required).
    language_mode : str
        ``"auto"`` for automatic language detection,
        or a language code like ``"de"`` / ``"en"``.
    model : str
        Groq whisper model name.  Defaults to ``whisper-large-v3-turbo``.
    groq_client_class :
        Injected ``Groq`` class (for testing).
    """

    def __init__(
        self,
        api_key: str,
        language_mode: str = "auto",
        model: str = DEFAULT_GROQ_MODEL,
        *,
        groq_client_class=None,
    ) -> None:
        if not api_key:
            raise TranscriptionError(
                "Groq API key is missing. "
                "Enter your key in Settings → Remote Provider API Keys."
            )
        self._api_key = api_key
        self._language_mode = (language_mode or "auto").strip().lower()
        self._model = model or DEFAULT_GROQ_MODEL
        self._groq_class = groq_client_class  # None → lazy import on first use

    def _get_groq_class(self):
        if self._groq_class is None:
            self._groq_class = _default_groq()
        return self._groq_class

    def _build_client(self):
        """Create a Groq client instance.

        When a custom CA bundle is detected (via ``SSL_CERT_FILE`` or
        ``REQUESTS_CA_BUNDLE``), an ``httpx.Client(verify=<SSLContext>)``
        is passed so that corporate proxy / Zscaler certificates are
        trusted regardless of which env var the user has set.
        """
        cls = self._get_groq_class()
        ssl_ctx = create_ssl_context()
        if ssl_ctx is not None:
            try:
                import httpx  # noqa: F811 - runtime import

                http_client = httpx.Client(verify=ssl_ctx)
                return cls(api_key=self._api_key, http_client=http_client)
            except Exception:
                pass  # fall through to default client
        return cls(api_key=self._api_key)

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        """Transcribe audio via Groq batch API.

        Accepts WAV bytes, a file path, or a Path object.
        """
        client = self._build_client()

        temp_path: Path | None = None
        try:
            if isinstance(audio_source, bytes):
                # Write WAV bytes to a temp file for the SDK.
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
                    handle.write(audio_source)
                    temp_path = Path(handle.name)
                file_path = str(temp_path)
            else:
                file_path = str(audio_source)

            kwargs: dict = {
                "model": self._model,
                "temperature": 0.0,
                "response_format": "text",
            }

            if self._language_mode != "auto":
                kwargs["language"] = self._language_mode

            with open(file_path, "rb") as audio_file:
                kwargs["file"] = (Path(file_path).name, audio_file)
                transcription = client.audio.transcriptions.create(**kwargs)

            # response_format="text" returns a string directly.
            if isinstance(transcription, str):
                return transcription.strip()

            # Fallback: object with .text attribute (json response format).
            text = getattr(transcription, "text", "") or ""
            return text.strip()

        except TranscriptionError:
            raise
        except Exception as exc:
            if _is_ssl_error(exc):
                raise TranscriptionError(
                    "Groq: SSL certificate verification failed "
                    "(likely a corporate proxy such as Zscaler). "
                    "Set SSL_CERT_FILE or REQUESTS_CA_BUNDLE to your "
                    f"corporate CA .pem, or switch to the local provider. "
                    f"See {DOC_SSL_PROXY_PATH} for details."
                ) from exc
            # Surface authentication and rate-limit errors clearly.
            exc_type = type(exc).__name__
            if "AuthenticationError" in exc_type:
                raise TranscriptionError(
                    "Groq: Authentication failed — the API key is invalid "
                    "or expired."
                ) from exc
            if "RateLimitError" in exc_type:
                raise TranscriptionError(
                    "Groq: Rate limit exceeded. Wait a moment and try again, "
                    "or upgrade your Groq plan."
                ) from exc
            raise TranscriptionError(
                f"Groq transcription failed: {exc}"
            ) from exc
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
        key is accepted by the Groq API.
        """
        try:
            client = self._build_client()
            # List available models to verify key.
            models = client.models.list()
            model_ids = [m.id for m in models.data] if hasattr(models, "data") else []
            whisper_found = any("whisper" in mid for mid in model_ids)
            if whisper_found:
                return True, "Connection OK — API key is valid."
            return True, "Connection OK — API key is valid (no whisper models listed)."
        except Exception as exc:
            if _is_ssl_error(exc):
                return False, (
                    "SSL certificate verification failed — likely a "
                    "corporate proxy (Zscaler). Set SSL_CERT_FILE or "
                    "REQUESTS_CA_BUNDLE to your corporate CA .pem file. "
                    f"See {DOC_SSL_PROXY_PATH} for details."
                )
            exc_type = type(exc).__name__
            if "AuthenticationError" in exc_type:
                return False, (
                    "Authentication failed — the API key is invalid or expired."
                )
            return False, f"Connection failed: {exc}"

    # -- Streaming stubs --------------------------------------------------------

    def start_stream(self, on_partial: StreamingCallback | None = None) -> None:
        raise NotImplementedError(
            "Groq streaming is not yet implemented. "
            "Use batch mode with Groq, or use local provider for streaming."
        )

    def push_audio_chunk(self, chunk: bytes) -> None:
        raise NotImplementedError("Groq streaming is not yet implemented.")

    def stop_stream(self) -> str:
        raise NotImplementedError("Groq streaming is not yet implemented.")

    def abort_stream(self) -> None:
        raise NotImplementedError("Groq streaming is not yet implemented.")
