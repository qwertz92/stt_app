"""AssemblyAI remote transcription provider.

Batch transcription via the AssemblyAI Python SDK.
Real-time streaming via AssemblyAI's WebSocket API (RealtimeTranscriber).
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
)
from ..ssl_utils import is_ssl_error as _is_ssl_error
from .base import AudioInput, ITranscriber, StreamingCallback, TranscriptionError


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
        )


class AssemblyAITranscriber(ITranscriber):
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
    ) -> None:
        if not api_key:
            raise TranscriptionError(
                "AssemblyAI API key is missing. "
                "Enter your key in Settings → Remote Provider API Keys."
            )
        self._api_key = api_key
        self._language_mode = (language_mode or "auto").strip().lower()
        self._model = (model or DEFAULT_ASSEMBLYAI_MODEL).strip().lower()
        self._aai = aai_module  # None → lazy import on first use

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

        if selected_model in {"best", "nano"} and hasattr(aai, "SpeechModel"):
            speech_model = getattr(aai.SpeechModel, selected_model, None)
            if speech_model is not None:
                kwargs["speech_model"] = speech_model
            else:
                kwargs["speech_models"] = [selected_model]
        else:
            kwargs["speech_models"] = [selected_model]

        if self._language_mode == "auto":
            kwargs["language_detection"] = True
        else:
            # Map short codes to AssemblyAI language codes.
            _LANG_MAP = {
                "de": "de",
                "en": "en",
                "es": "es",
                "fr": "fr",
                "pt": "pt",
                "it": "it",
            }
            lang_code = _LANG_MAP.get(self._language_mode)
            if lang_code:
                kwargs["language_code"] = lang_code
                kwargs["language_detection"] = False
            else:
                # Unknown language code → fall back to auto detection.
                kwargs["language_detection"] = True

        return aai.TranscriptionConfig(**kwargs)

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

    # -- Streaming via RealtimeTranscriber --------------------------------------

    def start_stream(self, on_partial: StreamingCallback | None = None) -> None:
        """Start a real-time streaming session via AssemblyAI WebSocket API.

        The ``on_partial`` callback receives the accumulated transcript text
        (all finalized sentences + current partial) each time an update
        arrives from the server.
        """
        self._configure()
        aai = self._get_aai()

        self._stream_lock = threading.Lock()
        self._stream_on_partial = on_partial
        self._stream_finals: list[str] = []
        self._stream_current_partial: str = ""
        self._stream_error: Exception | None = None

        try:
            rt = aai.RealtimeTranscriber(
                sample_rate=AUDIO_SAMPLE_RATE,
                on_data=self._on_rt_data,
                on_error=self._on_rt_error,
            )
            rt.connect()
        except Exception as exc:
            if _is_ssl_error(exc):
                raise TranscriptionError(
                    "AssemblyAI streaming: SSL certificate verification failed "
                    "(likely a corporate proxy such as Zscaler)."
                ) from exc
            raise TranscriptionError(
                f"AssemblyAI streaming: failed to connect: {exc}"
            ) from exc

        self._rt_transcriber = rt

    def push_audio_chunk(self, chunk: bytes) -> None:
        """Send a raw PCM16 audio chunk to the real-time session."""
        rt = getattr(self, "_rt_transcriber", None)
        if rt is None:
            return
        try:
            rt.stream(chunk)
        except Exception as exc:
            raise TranscriptionError(
                f"AssemblyAI streaming: failed to send audio: {exc}"
            ) from exc

    def stop_stream(self) -> str:
        """Finalize the streaming session and return accumulated text."""
        rt = getattr(self, "_rt_transcriber", None)
        self._rt_transcriber = None
        if rt is not None:
            try:
                rt.close()
            except Exception:
                pass

        with self._stream_lock:
            parts = list(self._stream_finals)
            if self._stream_current_partial:
                parts.append(self._stream_current_partial)
            self._stream_finals = []
            self._stream_current_partial = ""
            error = self._stream_error

        text = " ".join(p for p in parts if p).strip()

        if error and not text:
            raise TranscriptionError(
                f"AssemblyAI streaming failed: {error}"
            )

        return text

    def abort_stream(self) -> None:
        """Abort the streaming session immediately, discarding all text."""
        rt = getattr(self, "_rt_transcriber", None)
        self._rt_transcriber = None
        if rt is not None:
            try:
                rt.close()
            except Exception:
                pass

        with self._stream_lock:
            self._stream_finals = []
            self._stream_current_partial = ""

    # -- Real-time callbacks (called from WebSocket thread) -------------------

    def _on_rt_data(self, transcript) -> None:
        """Handle incoming transcript data from the WebSocket."""
        aai = self._get_aai()
        text = getattr(transcript, "text", "") or ""

        with self._stream_lock:
            if isinstance(transcript, aai.RealtimeFinalTranscript):
                if text:
                    self._stream_finals.append(text)
                self._stream_current_partial = ""
            else:
                self._stream_current_partial = text

            # Build full accumulated text for the callback.
            parts = list(self._stream_finals)
            if self._stream_current_partial:
                parts.append(self._stream_current_partial)
            combined = " ".join(p for p in parts if p).strip()

        callback = self._stream_on_partial
        if callback and combined:
            callback(combined)

    def _on_rt_error(self, error) -> None:
        """Handle errors from the WebSocket."""
        with self._stream_lock:
            self._stream_error = error
