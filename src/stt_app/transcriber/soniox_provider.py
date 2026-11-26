"""Soniox remote transcription provider.

Batch: an async transcription is four calls on `https://api.soniox.com/v1`
(`https://api.<region>.soniox.com/v1` in a region project): `POST /files`
uploads the audio (multipart `file`, answer `{"id": ...}`), `POST
/transcriptions` creates the job (JSON `model`, `file_id`, `language_hints`,
`context`), `GET /transcriptions/{id}` is polled until its `status` is
terminal, and `GET /transcriptions/{id}/transcript` answers `{"id", "text",
"tokens"}`. Soniox keeps both objects until they are deleted ("automatically
deleted after 30 days"), so the transcription and the file are deleted
afterwards (`DELETE`, 204). Every request carries `Authorization: Bearer
<key>`. Read on the vendor's pages and OpenAPI schema on 2026-11-26;
`docs/agents/remote-providers.md` lists each URL.

Streaming is a WebSocket session (`soniox_stream.py`); this class holds the
current one and builds its start message.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..config import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    DEFAULT_CUSTOM_VOCABULARY,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_SILENCE_GATE_THRESHOLD,
    DEFAULT_SONIOX_MODEL,
    ENGINE_LABELS,
    SONIOX_API_HOSTS,
    SONIOX_BATCH_MAX_WAIT_S,
    SONIOX_CONTEXT_MAX_CHARS,
    SONIOX_MODELS,
    SONIOX_POLL_INTERVAL_S,
    SONIOX_REALTIME_HOSTS,
    SONIOX_REALTIME_MODEL,
    language_modes_for_selection,
    normalize_soniox_region,
    parse_custom_vocabulary,
    remote_batch_part_limit,
)
from ..ssl_utils import is_ssl_error as _is_ssl_error
from ._audio_parts import transcribe_in_parts
from ._http_utils import (
    audio_content_type,
    format_ssl_error_message,
    http_error_suffix,
    json_from_reply,
    key_redactor,
    missing_field_error,
    multipart_form_data,
    open_request,
    transcript_from_json,
)
from ._job_poll import fetch_with_retries, poll_job
from .base import (
    AudioInput,
    ITranscriber,
    ProgressReporter,
    StreamingCallback,
    StreamingErrorCallback,
    TranscriptionError,
)
from .soniox_stream import SonioxStreamSession

logger = logging.getLogger(__name__)

_PROVIDER = ENGINE_LABELS["soniox"]
_UPLOAD_PROGRESS = "Uploading audio to Soniox and waiting for transcription..."
# `TranscriptionStatus`: queued, processing, completed, error (the OpenAPI
# schema). The error-handling reference writes `failed` for the same outcome,
# so both end the wait; a status a later API version adds is waited out.
_TERMINAL_STATUSES = ("completed", "error", "failed")
_COMPLETED = "completed"
# Answers to a status or transcript request that a retry cannot change: the key
# is refused, or the transcription is gone (`transcription_not_found`).
_FINAL_HTTP_CODES = (401, 403, 404)
# The bound of the requests that only tidy up or check the key.
_SHORT_REQUEST_TIMEOUT_S = 15
_RETENTION_NOTE = (
    "Soniox keeps a transcription and its audio file for up to 30 days; the "
    "transcript can be fetched from its API until then."
)


def fit_terms(terms: list[str]) -> list[str]:
    """The terms that fit Soniox's context, in order.

    "Up to 8,000 tokens (about 10,000 characters)" for the whole context, and
    a request over it is refused with `invalid_request`
    (https://docs.soniox.com/stt/concepts/context.mdx). The app's 100 terms
    only reach it with very long entries.
    """
    kept: list[str] = []
    used = 0
    for term in terms:
        used += len(term) + 3
        if used > SONIOX_CONTEXT_MAX_CHARS:
            break
        kept.append(term)
    return kept


class SonioxTranscriber(ProgressReporter, ITranscriber):
    def __init__(
        self,
        api_key: str,
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        model: str = DEFAULT_SONIOX_MODEL,
        region: str = "",
        custom_vocabulary: str = DEFAULT_CUSTOM_VOCABULARY,
        silence_gate_threshold: float = DEFAULT_SILENCE_GATE_THRESHOLD,
        request_timeout_s: int = 120,
        poll_interval_s: float = SONIOX_POLL_INTERVAL_S,
        max_wait_s: float = SONIOX_BATCH_MAX_WAIT_S,
    ) -> None:
        ProgressReporter.__init__(self)
        if not api_key:
            raise TranscriptionError(
                "Soniox API key is missing. Enter your key in Settings -> Providers."
            )
        self._api_key = api_key
        self._scrub = key_redactor(api_key)
        self._model = model if model in SONIOX_MODELS else DEFAULT_SONIOX_MODEL
        self._region = normalize_soniox_region(region)
        self._base_url = f"https://{SONIOX_API_HOSTS[self._region]}/v1"
        self.set_language_mode(language_mode)
        self._vocabulary = parse_custom_vocabulary(custom_vocabulary)
        self._silence_gate_threshold = float(silence_gate_threshold)
        self._request_timeout_s = max(5, int(request_timeout_s))
        self._poll_interval_s = max(0.0, float(poll_interval_s))
        self._max_wait_s = max(0.0, float(max_wait_s))
        self._stream_lock = threading.Lock()
        self._stream_session: SonioxStreamSession | None = None

    def _normalize_language_mode(self, mode: str) -> str:
        normalized = (mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        if normalized not in language_modes_for_selection("soniox", self._model):
            normalized = DEFAULT_LANGUAGE_MODE
        return normalized

    def _format_error(self, exc: Exception) -> str:
        if _is_ssl_error(exc):
            return format_ssl_error_message(_PROVIDER)
        return self._scrub(str(exc))

    def _create_payload(self, file_id: str) -> dict[str, Any]:
        """The JSON of `POST /transcriptions`.

        A chosen language is a hint, and `language_hints_strict` makes the
        model "rely more on language hints": choosing German in the app means
        German. Auto sends neither. The custom vocabulary is `context.terms`.
        """
        return {
            "model": self._model,
            "file_id": file_id,
            **self._hints_and_context(),
        }

    def _hints_and_context(self) -> dict[str, Any]:
        """The language hints and context terms of a batch or realtime request."""
        fields: dict[str, Any] = {}
        if self._language_mode != DEFAULT_LANGUAGE_MODE:
            fields["language_hints"] = [self._language_mode]
            fields["language_hints_strict"] = True
        terms = fit_terms(self._vocabulary)
        if terms:
            fields["context"] = {"terms": terms}
        return fields

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
        content_type: str = "",
        timeout_s: float | None = None,
    ):
        request = urllib.request.Request(
            f"{self._base_url}{path}", data=data, method=method
        )
        request.add_header("Authorization", f"Bearer {self._api_key}")
        request.add_header("Accept", "application/json")
        if content_type:
            request.add_header("Content-Type", content_type)
        return open_request(
            request,
            timeout=timeout_s or self._request_timeout_s,
            provider_name=_PROVIDER,
        )

    def _job_request(self, path: str, job_id: str):
        """`_request` (a GET) for a job that exists.

        `open_request` refuses a redirect or an unsendable header with a
        `TranscriptionError` of its own; the job is already running, so that
        error names it like every other poll error (`_job_poll`).
        """
        try:
            return self._request("GET", path)
        except TranscriptionError as exc:
            raise TranscriptionError(f"{exc} Job id {job_id}.") from exc

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        return transcribe_in_parts(
            audio_source,
            self._transcribe_request,
            limit=remote_batch_part_limit("soniox", self._model),
            progress_text=_UPLOAD_PROGRESS,
            raise_if_canceled=self._raise_if_canceled,
            silence_threshold=self._silence_gate_threshold,
        )

    def _transcribe_request(self, audio_source: AudioInput, progress_text: str) -> str:
        """One transcription, for a whole recording or one part of it.

        What stays on Soniox's side is decided here. The file is kept until
        the job is over ("deleting it earlier fails the transcription with
        `file_not_found`"); once the job ended, the transcription and the file
        are deleted, unless the transcript of a completed job could not be
        fetched (both stay, and the error names the id). A wait that gave up
        (budget, quit) leaves both too: the job may still complete, and
        Soniox deletes them itself after 30 days.
        """
        file_id = self._upload(audio_source, progress_text)
        transcription_id = ""
        delete_transcription = False
        delete_file = True
        try:
            transcription_id = self._create(file_id)
            delete_file = False
            job = poll_job(
                lambda: self._fetch_transcription(transcription_id),
                status_of=lambda answer: answer["status"],
                terminal=_TERMINAL_STATUSES,
                provider=_PROVIDER,
                job_id=transcription_id,
                max_wait_s=self._max_wait_s,
                interval_s=self._poll_interval_s,
            )
            delete_transcription = delete_file = True
            if job["status"] != _COMPLETED:
                raise TranscriptionError(self._job_failure(job, transcription_id))
            try:
                return fetch_with_retries(
                    lambda: self._fetch_transcript(transcription_id),
                    provider=_PROVIDER,
                    job_id=transcription_id,
                    interval_s=self._poll_interval_s,
                    note=_RETENTION_NOTE,
                )
            except TranscriptionError:
                delete_transcription = delete_file = False
                raise
        finally:
            if delete_transcription:
                self._delete(f"/transcriptions/{transcription_id}")
            if delete_file:
                self._delete(f"/files/{file_id}")

    def _read_audio(self, audio_source: AudioInput) -> tuple[bytes, str]:
        if isinstance(audio_source, (bytes, bytearray)):
            return bytes(audio_source), "audio.wav"
        path = Path(audio_source)
        return path.read_bytes(), path.name or "audio.wav"

    def _http_failure(self, exc: urllib.error.HTTPError, doing: str) -> str:
        """The sentence for a refused request; `doing` is what was attempted."""
        if exc.code in (401, 403):
            return (
                f"{_PROVIDER}: Authentication failed (HTTP {exc.code}). The API "
                "key is invalid or expired, or belongs to a project in another "
                f"region than the one selected ({SONIOX_API_HOSTS[self._region]})."
            )
        if exc.code == 402:
            return (
                f"{_PROVIDER}: the balance or budget is used up (HTTP 402)"
                f"{http_error_suffix(exc, self._scrub)}"
            )
        if exc.code == 429:
            return (
                f"{_PROVIDER}: Rate or capacity limit exceeded (HTTP 429)"
                f"{http_error_suffix(exc, self._scrub)}"
            )
        return (
            f"{_PROVIDER} {doing} failed (HTTP {exc.code})"
            f"{http_error_suffix(exc, self._scrub)}"
        )

    def _post_for_id(
        self,
        path: str,
        *,
        data: bytes,
        content_type: str,
        doing: str,
        progress_text: str = "",
    ) -> str:
        """One POST that answers the id of the object it created."""
        try:
            if progress_text:
                self._emit_progress(progress_text)
            with self._request(
                "POST", path, data=data, content_type=content_type
            ) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            raise TranscriptionError(self._http_failure(exc, doing)) from exc
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(
                f"{_PROVIDER} {doing} failed: {self._format_error(exc)}"
            ) from exc
        prefix = f"{_PROVIDER} {doing} failed"
        created = json_from_reply(payload, prefix=prefix, redact=self._scrub)
        object_id = created.get("id") if isinstance(created, dict) else None
        if not isinstance(object_id, str) or not object_id:
            raise missing_field_error(
                created, prefix=prefix, field="id", redact=self._scrub
            )
        return object_id

    def _upload(self, audio_source: AudioInput, progress_text: str) -> str:
        try:
            audio_bytes, filename = self._read_audio(audio_source)
            body, content_type = multipart_form_data(
                fields=[],
                file_field=(
                    "file",
                    filename,
                    audio_bytes,
                    audio_content_type(filename),
                ),
            )
        except Exception as exc:
            raise TranscriptionError(
                f"{_PROVIDER} upload failed: {self._format_error(exc)}"
            ) from exc
        return self._post_for_id(
            "/files",
            data=body,
            content_type=content_type,
            doing="upload",
            progress_text=progress_text,
        )

    def _create(self, file_id: str) -> str:
        return self._post_for_id(
            "/transcriptions",
            data=json.dumps(self._create_payload(file_id)).encode("utf-8"),
            content_type="application/json",
            doing="transcription request",
        )

    def _delete(self, path: str) -> None:
        """Delete one object on Soniox's side, best effort.

        The transcript is already in hand (or the job failed), so a refused or
        failed deletion never turns into an error of the dictation; it is
        logged, and Soniox deletes the object itself after 30 days.
        """
        try:
            with self._request(
                "DELETE", path, timeout_s=_SHORT_REQUEST_TIMEOUT_S
            ) as resp:
                resp.read()
        except Exception as exc:
            logger.warning(
                "soniox_cleanup_failed path=%s error=%s",
                path,
                self._scrub(f"{type(exc).__name__}: {exc}"),
            )

    def _fetch_transcription(self, transcription_id: str) -> dict[str, Any]:
        """One status request. A `TranscriptionError` ends the poll at once;
        anything else raised here is a failed fetch the poll retries."""
        path = f"/transcriptions/{transcription_id}"
        try:
            with self._job_request(path, transcription_id) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in _FINAL_HTTP_CODES:
                raise TranscriptionError(
                    f"{_PROVIDER} could not report the job's status (HTTP "
                    f"{exc.code}){http_error_suffix(exc, self._scrub)}. Job id "
                    f"{transcription_id}."
                ) from exc
            raise
        try:
            answer = json_from_reply(payload, prefix=_PROVIDER, redact=self._scrub)
        except TranscriptionError as exc:
            # A failed fetch, not a failure of the job: one proxy page between
            # good polls must not cost the user a second upload.
            raise ValueError(str(exc).removeprefix(f"{_PROVIDER}: ")) from exc
        if not isinstance(answer, dict) or not isinstance(answer.get("status"), str):
            # Retried by the poll: an answer without a status says nothing.
            raise ValueError("the status answer has no job status")
        return answer

    def _fetch_transcript(self, transcription_id: str) -> str:
        path = f"/transcriptions/{transcription_id}/transcript"
        try:
            with self._job_request(path, transcription_id) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in _FINAL_HTTP_CODES:
                raise TranscriptionError(
                    f"{_PROVIDER} finished the job, but its transcript could not "
                    f"be fetched (HTTP {exc.code})"
                    f"{http_error_suffix(exc, self._scrub)}. Job id "
                    f"{transcription_id}. {_RETENTION_NOTE}"
                ) from exc
            raise
        try:
            return transcript_from_json(
                payload,
                prefix=f"{_PROVIDER} transcript",
                redact=self._scrub,
                field="text",
            )
        except TranscriptionError as exc:
            raise TranscriptionError(
                f"{exc} Job id {transcription_id}. {_RETENTION_NOTE}"
            ) from exc

    def _job_failure(self, job: dict[str, Any], transcription_id: str) -> str:
        parts = [
            self._scrub(str(job.get(key) or "")).strip()
            for key in ("error_type", "error_message")
        ]
        detail = ": ".join(part for part in parts if part)[:300]
        reason = f", {detail}" if detail else ""
        return (
            f"{_PROVIDER} did not transcribe the recording (job status "
            f"{job['status']}{reason}). Job id {transcription_id}."
        )

    def test_connection(self) -> tuple[bool, str]:
        """`GET /models`: answers for a valid key and spends no audio."""
        try:
            with self._request(
                "GET", "/models", timeout_s=_SHORT_REQUEST_TIMEOUT_S
            ) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return False, (
                    f"Authentication failed (HTTP {exc.code}). The API key is "
                    "invalid or expired, or belongs to a project in another "
                    f"region than the one selected ({SONIOX_API_HOSTS[self._region]})."
                )
            return (
                False,
                f"API returned HTTP {exc.code}{http_error_suffix(exc, self._scrub)}",
            )
        except TranscriptionError as exc:
            return False, str(exc)
        except Exception as exc:
            return False, f"Connection failed: {self._format_error(exc)}"
        try:
            answer = json_from_reply(
                payload, prefix="The Soniox model list", redact=self._scrub
            )
        except TranscriptionError as exc:
            return False, str(exc)
        if isinstance(answer, dict) and isinstance(answer.get("models"), list):
            return True, "Connection OK — API key is valid."
        return False, "Unexpected response from the Soniox API."

    def _get_websocket_module(self):
        try:
            import websocket  # type: ignore
        except ImportError as exc:
            raise TranscriptionError(
                "Soniox streaming requires 'websocket-client'. "
                "Install it with: pip install websocket-client"
            ) from exc
        return websocket

    def _start_message(self) -> str:
        """The first frame of a realtime session.

        Always the realtime model (`stt-rt-v6`): the async model is not one.
        The key is in the handshake header, never here (the `api_key` field is
        deprecated). Language and vocabulary are those of the batch request.
        """
        message: dict[str, Any] = {
            "model": SONIOX_REALTIME_MODEL,
            "audio_format": "pcm_s16le",
            "sample_rate": AUDIO_SAMPLE_RATE,
            "num_channels": AUDIO_CHANNELS,
        }
        message.update(self._hints_and_context())
        return json.dumps(message)

    def start_stream(
        self,
        on_partial: StreamingCallback | None = None,
        on_error: StreamingErrorCallback | None = None,
    ) -> None:
        websocket = self._get_websocket_module()
        host = SONIOX_REALTIME_HOSTS[self._region]
        session = SonioxStreamSession(
            websocket=websocket,
            url=f"wss://{host}/transcribe-websocket",
            host=host,
            api_key=self._api_key,
            scrub=self._scrub,
            start_message=self._start_message(),
            on_partial=on_partial,
            on_error=on_error,
        )
        with self._stream_lock:
            if self._stream_session is not None:
                raise TranscriptionError("Streaming session already active.")
            # Published before the handshake so a stop or an abort during it
            # reaches this session instead of finding nothing.
            self._stream_session = session
        try:
            session.connect()
        except BaseException:
            with self._stream_lock:
                if self._stream_session is session:
                    self._stream_session = None
            raise

    def push_audio_chunk(
        self, chunk: bytes, *, block_timeout_s: float | None = None
    ) -> None:
        session = self._stream_session
        if session is None:
            raise TranscriptionError("Streaming session is not active.")
        session.push(chunk, block_timeout_s)

    def stop_stream(self) -> str:
        with self._stream_lock:
            session = self._stream_session
        if session is None:
            raise TranscriptionError("Streaming session is not active.")
        try:
            return session.stop()
        finally:
            with self._stream_lock:
                if self._stream_session is session:
                    self._stream_session = None

    def abort_stream(self) -> None:
        with self._stream_lock:
            session = self._stream_session
            self._stream_session = None
        if session is not None:
            session.abort()
