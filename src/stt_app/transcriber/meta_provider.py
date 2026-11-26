"""Meta Muse Voice Transcribe remote transcription provider (batch only).

`POST https://api.meta.ai/v1/asr/transcribe`, multipart: a `request` part of
JSON settings (`model`, `mode`, `audioEncoding`, an optional `languageBias`
list of language names and `keywords` list) and an `audio` part holding a WAV
file (mono, 16-bit, 16 or 24 kHz, at most 10 minutes and 32 MB); the answer is
JSON with a `transcript` string. Read on the vendor's page
(https://dev.meta.ai/docs/speech-to-text) on 2026-11-26;
`docs/agents/remote-providers.md` quotes it.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
import wave
from pathlib import Path

import numpy as np

from ..config import (
    DEFAULT_CUSTOM_VOCABULARY,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_META_MODEL,
    DEFAULT_SILENCE_GATE_THRESHOLD,
    ENGINE_LABELS,
    META_LANGUAGE_NAMES,
    META_MODELS,
    language_modes_for_selection,
    parse_custom_vocabulary,
    remote_batch_part_limit,
)
from ..ssl_utils import is_ssl_error as _is_ssl_error
from ._audio_parts import transcribe_in_parts
from ._http_utils import (
    format_ssl_error_message,
    http_error_suffix,
    multipart_form_data,
    open_request,
    transcript_from_json,
)
from ._pcm_audio import pcm16_wav_bytes, resample_linear
from .base import (
    AudioInput,
    ITranscriber,
    ProgressReporter,
    StreamingCallback,
    TranscriptionError,
)
from .local_onnx_asr import _read_wav_float32

META_API_BASE = "https://api.meta.ai/v1"
_TRANSCRIBE_URL = f"{META_API_BASE}/asr/transcribe"
_UPLOAD_PROGRESS = "Uploading audio to Meta and waiting for transcription..."
_PROVIDER_NAME = ENGINE_LABELS["meta"]
# The bound of a request that only checks the key, as the other engines' tests.
_KEY_CHECK_TIMEOUT_S = 15
# "Sample rate: 16 kHz or 24 kHz", mono, 16-bit integer PCM.
_ACCEPTED_SAMPLE_RATES = (16_000, 24_000)
_TARGET_SAMPLE_RATE = 16_000
_SCRUBBED = "[hidden]"
# The connection test's request: one second of digital silence. Meta documents
# no endpoint that only checks a key, and a request that fails before it
# produces a transcript, a 401 included, "is not billed"; a one-second answer
# costs 1/3600 of $0.18.
_TEST_AUDIO_SECONDS = 1


def _wav_for_meta(audio: bytes, filename: str) -> bytes:
    """The recording as the WAV Meta takes: mono, 16-bit, 16 or 24 kHz.

    The app records exactly that and it goes out untouched. A 16-bit PCM
    import at another rate or in stereo is mixed down and resampled to 16 kHz
    mono, as the local models are fed. Anything else (8- or 24-bit, float, not
    a WAV) is refused: the app has no decoder for it and Meta takes nothing
    else.
    """

    def refusal(what: str) -> TranscriptionError:
        return TranscriptionError(
            f"{_PROVIDER_NAME} takes 16-bit PCM WAV; {filename} is {what}. "
            "Convert it to 16-bit PCM WAV first."
        )

    try:
        with wave.open(io.BytesIO(audio), "rb") as handle:
            channels, width, rate = (
                handle.getnchannels(),
                handle.getsampwidth(),
                handle.getframerate(),
            )
    except Exception as exc:
        if str(exc).startswith(("unknown format", "unknown extended format")):
            # The RIFF header is a WAV's; its sample format is not PCM (a
            # WAVE_FORMAT_EXTENSIBLE float file says "unknown extended format").
            raise refusal(f"a WAV file in a format that is not PCM ({exc})") from exc
        raise refusal(f"not a WAV file that can be read ({exc})") from exc
    if width != 2:
        raise refusal(f"{width * 8}-bit WAV audio")
    if channels == 1 and rate in _ACCEPTED_SAMPLE_RATES:
        return audio
    samples, source_rate = _read_wav_float32(io.BytesIO(audio))
    resampled = resample_linear(samples, source_rate, _TARGET_SAMPLE_RATE)
    pcm = (np.clip(resampled, -1.0, 1.0) * 32767.0).astype("<i2")
    return pcm16_wav_bytes(pcm.tobytes(), _TARGET_SAMPLE_RATE)


class MetaTranscriber(ProgressReporter, ITranscriber):
    def __init__(
        self,
        api_key: str,
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        model: str = DEFAULT_META_MODEL,
        custom_vocabulary: str = DEFAULT_CUSTOM_VOCABULARY,
        silence_gate_threshold: float = DEFAULT_SILENCE_GATE_THRESHOLD,
        request_timeout_s: int = 300,
    ) -> None:
        ProgressReporter.__init__(self)
        if not api_key:
            raise TranscriptionError(
                "Meta API key is missing. Enter your key in Settings -> Providers."
            )
        self._api_key = api_key
        self._model = model if model in META_MODELS else DEFAULT_META_MODEL
        self.set_language_mode(language_mode)
        self._keywords = parse_custom_vocabulary(custom_vocabulary)
        self._silence_gate_threshold = float(silence_gate_threshold)
        # The request is synchronous: the answer comes once the whole part is
        # transcribed, so the socket timeout has to cover that.
        self._request_timeout_s = max(5, int(request_timeout_s))

    def _normalize_language_mode(self, mode: str) -> str:
        normalized = (mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        if normalized not in language_modes_for_selection("meta", self._model):
            normalized = DEFAULT_LANGUAGE_MODE
        return normalized

    def _scrub(self, text: str) -> str:
        """`text` without the key, also as JSON writes it."""
        for form in {json.dumps(self._api_key)[1:-1], self._api_key}:
            text = text.replace(form, _SCRUBBED)
        return text

    def _format_error(self, exc: Exception) -> str:
        if _is_ssl_error(exc):
            return format_ssl_error_message(_PROVIDER_NAME)
        return self._scrub(str(exc))

    def _request_settings(self) -> str:
        settings: dict[str, object] = {
            "model": self._model,
            "mode": "PUSH_TO_TALK",
            "audioEncoding": "WAV",
        }
        if self._language_mode != DEFAULT_LANGUAGE_MODE:
            settings["languageBias"] = [META_LANGUAGE_NAMES[self._language_mode]]
        if self._keywords:
            settings["keywords"] = self._keywords
        return json.dumps(settings)

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        return transcribe_in_parts(
            audio_source,
            self._transcribe_request,
            limit=remote_batch_part_limit("meta", self._model),
            progress_text=_UPLOAD_PROGRESS,
            raise_if_canceled=self._raise_if_canceled,
            silence_threshold=self._silence_gate_threshold,
        )

    def _transcribe_request(
        self,
        audio_source: AudioInput,
        progress_text: str,
        timeout_s: float | None = None,
    ) -> str:
        """One request, for a whole recording or one part of it."""
        try:
            if isinstance(audio_source, (bytes, bytearray)):
                audio_bytes = bytes(audio_source)
                filename = "audio.wav"
            else:
                path = Path(audio_source)
                audio_bytes = path.read_bytes()
                filename = path.name or "audio.wav"
            body, content_type = multipart_form_data(
                fields=[("request", self._request_settings(), "application/json")],
                file_field=(
                    "audio",
                    "audio.wav",
                    _wav_for_meta(audio_bytes, filename),
                    "audio/wav",
                ),
            )
            request = urllib.request.Request(_TRANSCRIBE_URL, data=body, method="POST")
            request.add_header("Authorization", f"Bearer {self._api_key}")
            request.add_header("Accept", "application/json")
            request.add_header("Content-Type", content_type)
            self._emit_progress(progress_text)
            with open_request(
                request,
                timeout=timeout_s or self._request_timeout_s,
                provider_name=_PROVIDER_NAME,
            ) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise TranscriptionError(
                    f"{_PROVIDER_NAME}: Authentication failed (HTTP 401). "
                    "The API key is invalid or expired."
                ) from exc
            if exc.code == 429:
                raise TranscriptionError(
                    f"{_PROVIDER_NAME}: Rate limit exceeded (HTTP 429). "
                    "Wait a moment and try again."
                ) from exc
            raise TranscriptionError(
                f"{_PROVIDER_NAME} transcription failed (HTTP {exc.code})"
                f"{http_error_suffix(exc, self._scrub)}"
            ) from exc
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(
                f"{_PROVIDER_NAME} transcription failed: {self._format_error(exc)}"
            ) from exc
        return transcript_from_json(
            payload,
            prefix=f"{_PROVIDER_NAME} transcription failed",
            redact=self._scrub,
            field="transcript",
        )

    def test_connection(self) -> tuple[bool, str]:
        silence = pcm16_wav_bytes(
            bytes(2 * _TARGET_SAMPLE_RATE * _TEST_AUDIO_SECONDS), _TARGET_SAMPLE_RATE
        )
        try:
            self._transcribe_request(
                silence, _UPLOAD_PROGRESS, timeout_s=_KEY_CHECK_TIMEOUT_S
            )
        except TranscriptionError as exc:
            return False, str(exc)
        return True, "Connection OK — API key is valid."

    def start_stream(self, on_partial: StreamingCallback | None = None) -> None:
        raise NotImplementedError(
            "Meta streaming is not implemented in this project. "
            "Use batch mode, or use local/AssemblyAI/Deepgram for streaming."
        )

    def push_audio_chunk(
        self, chunk: bytes, *, block_timeout_s: float | None = None
    ) -> None:
        raise NotImplementedError("Meta streaming is not implemented in this project.")

    def stop_stream(self) -> str:
        raise NotImplementedError("Meta streaming is not implemented in this project.")

    def abort_stream(self) -> None:
        raise NotImplementedError("Meta streaming is not implemented in this project.")
