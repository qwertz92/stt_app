"""Azure LLM Speech (MAI-Transcribe) remote transcription provider.

Batch transcription via Azure Speech "fast transcription" REST API with
enhanced mode enabled (`:transcribe` endpoint). This is a remote, cloud-only
service from Microsoft Foundry; there is no local/ONNX runtime for it.

The enhanced mode is backed by the Microsoft AI (MAI) team's MAI-Transcribe
models (`mai-transcribe-1.5`, `mai-transcribe-1`), which combine a speech
model with an LLM for high accuracy and multilingual support.

Unlike the other remote providers, Azure needs *two* pieces of configuration:
an API key (the Speech resource key) and a per-resource endpoint, for example
``https://<resource>.cognitiveservices.azure.com``. Both are required.

Docs: https://learn.microsoft.com/azure/ai-services/speech-service/llm-speech
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
import wave
from pathlib import Path

from ..config import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    AZURE_LOCALE_OVERRIDES,
    AZURE_SPEECH_API_VERSION,
    AZURE_SPEECH_MODELS,
    DEFAULT_AZURE_SPEECH_MODEL,
    DEFAULT_LANGUAGE_MODE,
    language_modes_for_selection,
)
from ..ssl_utils import create_ssl_context, is_ssl_error as _is_ssl_error
from ._http_utils import (
    audio_content_type,
    format_ssl_error_message,
    multipart_form_data,
    normalize_transcript_text,
)
from .base import (
    AudioInput,
    ITranscriber,
    ProgressReporter,
    StreamingCallback,
    TranscriptionError,
)

_TRANSCRIBE_PATH = "/speechtotext/transcriptions:transcribe"

def normalize_azure_endpoint(endpoint: str) -> str:
    """Return a clean ``https://host`` base URL for a Speech resource.

    Accepts a full endpoint URL, a bare host, or just the resource name.
    Raises :class:`TranscriptionError` when *endpoint* is empty.
    """
    value = (endpoint or "").strip()
    if not value:
        raise TranscriptionError(
            "Azure endpoint is missing. Enter your Speech resource endpoint "
            "(for example https://<resource>.cognitiveservices.azure.com) in "
            "Settings -> Remote Provider API Keys."
        )
    if "://" not in value:
        # Bare host ("res.cognitiveservices.azure.com") or resource name ("res").
        if "." not in value:
            value = f"{value}.cognitiveservices.azure.com"
        value = f"https://{value}"
    return value.rstrip("/")


def build_transcribe_url(endpoint: str) -> str:
    """Build the full ``:transcribe`` URL (with api-version) from *endpoint*."""
    base = normalize_azure_endpoint(endpoint)
    if "/speechtotext/transcriptions" in base:
        url = base
    else:
        url = f"{base}{_TRANSCRIBE_PATH}"
    if "api-version=" not in url:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}api-version={AZURE_SPEECH_API_VERSION}"
    return url


def _content_type_for(filename: str) -> str:
    return audio_content_type(filename)


def _silent_wav_bytes(duration_s: float = 1.0) -> bytes:
    """Return a tiny mono PCM16 WAV of silence for connection testing.

    Kept at ~1 second so the service does not reject it as too short, while
    still consuming a negligible amount of quota.
    """
    frame_count = max(1, int(AUDIO_SAMPLE_RATE * duration_s))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(AUDIO_CHANNELS)
        wav.setsampwidth(2)  # PCM16
        wav.setframerate(AUDIO_SAMPLE_RATE)
        wav.writeframes(b"\x00\x00" * frame_count)
    return buffer.getvalue()


class AzureLlmSpeechTranscriber(ProgressReporter, ITranscriber):
    """Batch transcription using Azure LLM Speech (MAI-Transcribe).

    Parameters
    ----------
    api_key : str
        Azure Speech resource key (required).
    endpoint : str
        Per-resource endpoint URL or resource name (required).
    language_mode : str
        ``"auto"`` for automatic multilingual detection, or a language code
        like ``"de"`` / ``"en"`` to send a ``locales`` hint.
    model : str
        MAI-Transcribe model name. Defaults to ``mai-transcribe-1.5``.
    """

    def __init__(
        self,
        api_key: str,
        endpoint: str = "",
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        model: str = DEFAULT_AZURE_SPEECH_MODEL,
        request_timeout_s: int = 120,
    ) -> None:
        ProgressReporter.__init__(self)
        if not api_key:
            raise TranscriptionError(
                "Azure Speech key is missing. "
                "Enter your key in Settings -> Remote Provider API Keys."
            )
        # Validate eagerly so a misconfigured endpoint fails fast and clearly.
        self._transcribe_url = build_transcribe_url(endpoint)
        self._api_key = api_key
        self._model = (
            model if model in AZURE_SPEECH_MODELS else DEFAULT_AZURE_SPEECH_MODEL
        )
        self._language_mode = (
            (language_mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        )
        if self._language_mode not in language_modes_for_selection(
            "azure",
            self._model,
        ):
            self._language_mode = DEFAULT_LANGUAGE_MODE
        self._request_timeout_s = max(5, int(request_timeout_s))

    # -- Request building -------------------------------------------------------

    def _azure_locale(self) -> str:
        return AZURE_LOCALE_OVERRIDES.get(
            self._language_mode, self._language_mode
        )

    def _definition(self) -> dict:
        definition: dict = {
            "enhancedMode": {
                "enabled": True,
                "model": self._model,
            }
        }
        if self._language_mode != DEFAULT_LANGUAGE_MODE:
            definition["locales"] = [self._azure_locale()]
        return definition

    def _build_request(
        self,
        audio_bytes: bytes,
        filename: str,
    ) -> urllib.request.Request:
        body, content_type = multipart_form_data(
            fields=[("definition", json.dumps(self._definition()))],
            file_field=(
                "audio",
                filename,
                audio_bytes,
                _content_type_for(filename),
            ),
        )
        req = urllib.request.Request(self._transcribe_url, data=body, method="POST")
        req.add_header("Ocp-Apim-Subscription-Key", self._api_key)
        req.add_header("Content-Type", content_type)
        return req

    # -- Batch transcription ----------------------------------------------------

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        try:
            if isinstance(audio_source, bytes):
                audio_bytes = bytes(audio_source)
                filename = "audio.wav"
            else:
                path = Path(audio_source)
                audio_bytes = path.read_bytes()
                filename = path.name or "audio.wav"

            req = self._build_request(audio_bytes, filename)
            ssl_ctx = create_ssl_context()
            self._emit_progress(
                "Uploading audio to Azure LLM Speech and waiting for transcription..."
            )
            with urllib.request.urlopen(
                req, timeout=self._request_timeout_s, context=ssl_ctx
            ) as resp:
                payload = resp.read()

            body = json.loads(payload.decode("utf-8", errors="replace"))
            return self._extract_transcript(body)
        except FileNotFoundError as exc:
            raise TranscriptionError(
                "Azure transcription failed: missing file path. "
                "This can happen when the input file does not exist or when "
                "TEMP/TMP points to a non-existent folder."
            ) from exc
        except TranscriptionError:
            raise
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        except Exception as exc:
            if _is_ssl_error(exc):
                raise TranscriptionError(
                    format_ssl_error_message("Azure")
                ) from exc
            raise TranscriptionError(
                f"Azure transcription failed: {exc}"
            ) from exc

    @staticmethod
    def _extract_transcript(body: object) -> str:
        if not isinstance(body, dict):
            return ""
        combined = body.get("combinedPhrases")
        if isinstance(combined, list) and combined:
            texts = [
                str(item.get("text", ""))
                for item in combined
                if isinstance(item, dict)
            ]
            return normalize_transcript_text(" ".join(t for t in texts if t))
        # Some responses may carry a single top-level text field.
        if isinstance(body.get("text"), str):
            return normalize_transcript_text(body["text"])
        return ""

    def _http_error(self, exc: urllib.error.HTTPError) -> TranscriptionError:
        detail = self._read_error_detail(exc)
        if exc.code in (401, 403):
            return TranscriptionError(
                f"Azure: Authentication failed (HTTP {exc.code}). "
                "The Speech resource key is invalid, or the key does not "
                "match the configured endpoint/region."
            )
        if exc.code == 404:
            return TranscriptionError(
                "Azure: Endpoint not found (HTTP 404). Check that the endpoint "
                "URL is correct and that the resource is in a region that "
                "supports LLM Speech."
            )
        if exc.code == 429:
            return TranscriptionError(
                "Azure: Rate limit exceeded (HTTP 429). "
                "Wait a moment and try again."
            )
        if exc.code == 400:
            hint = f" {detail}" if detail else ""
            return TranscriptionError(
                "Azure: Bad request (HTTP 400). Enhanced mode may not be "
                "supported in this region, or the audio/format is invalid."
                f"{hint}"
            )
        suffix = f": {detail}" if detail else f": {exc.reason}"
        return TranscriptionError(
            f"Azure transcription failed (HTTP {exc.code}){suffix}"
        )

    @staticmethod
    def _read_error_detail(exc: urllib.error.HTTPError) -> str:
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            return ""
        if not raw:
            return ""
        try:
            parsed = json.loads(raw)
        except Exception:
            return raw.strip()[:300]
        if isinstance(parsed, dict):
            error = parsed.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])[:300]
            if parsed.get("message"):
                return str(parsed["message"])[:300]
        return raw.strip()[:300]

    # -- Connection test --------------------------------------------------------

    def test_connection(self) -> tuple[bool, str]:
        """Validate the endpoint + key by transcribing a short silent clip.

        A successful (empty) transcription confirms the key, endpoint, region,
        and enhanced-mode availability. Consumes a negligible amount of quota.
        """
        try:
            req = self._build_request(_silent_wav_bytes(), "connection-test.wav")
            ssl_ctx = create_ssl_context()
            with urllib.request.urlopen(req, timeout=20, context=ssl_ctx) as resp:
                if 200 <= resp.status < 300:
                    return True, "Connection OK — endpoint and key are valid."
                return False, f"Unexpected response: HTTP {resp.status}."
        except urllib.error.HTTPError as exc:
            error = self._http_error(exc)
            return False, str(error)
        except TranscriptionError as exc:
            return False, str(exc)
        except Exception as exc:
            if _is_ssl_error(exc):
                return False, format_ssl_error_message("Azure")
            return False, f"Connection failed: {exc}"

    # -- Streaming stubs --------------------------------------------------------
    # Azure LLM Speech is a synchronous file-based ("fast transcription") API;
    # this app only wires it up for batch mode.

    def start_stream(self, on_partial: StreamingCallback | None = None) -> None:
        raise NotImplementedError(
            "Azure LLM Speech streaming is not implemented in this app. "
            "Use batch mode, or use local/AssemblyAI/Deepgram for streaming."
        )

    def push_audio_chunk(self, chunk: bytes) -> None:
        raise NotImplementedError("Azure LLM Speech streaming is not implemented.")

    def stop_stream(self) -> str:
        raise NotImplementedError("Azure LLM Speech streaming is not implemented.")

    def abort_stream(self) -> None:
        raise NotImplementedError("Azure LLM Speech streaming is not implemented.")
