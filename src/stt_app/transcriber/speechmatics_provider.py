"""Speechmatics remote transcription provider (batch only).

A Speechmatics batch transcription is a job: `POST /v2/jobs/` with the audio
as `data_file` and a JSON `config` returns the job id, `GET /v2/jobs/{id}` is
polled until the job's status is terminal, and `GET
/v2/jobs/{id}/transcript?format=txt` fetches the text. Read on the vendor's
pages on 2026-09-27; `docs/agents/remote-providers.md` quotes them.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..config import (
    DEFAULT_CUSTOM_VOCABULARY,
    DEFAULT_LANGUAGE_MODE,
    DEFAULT_SILENCE_GATE_THRESHOLD,
    DEFAULT_SPEECHMATICS_MODEL,
    SPEECHMATICS_API_HOSTS,
    SPEECHMATICS_BATCH_MAX_WAIT_S,
    SPEECHMATICS_LANGUAGE_CODES,
    SPEECHMATICS_MELIA_MODEL,
    SPEECHMATICS_MELIA_UNAVAILABLE_TEXT,
    SPEECHMATICS_MODELS,
    SPEECHMATICS_POLL_INTERVAL_S,
    language_modes_for_selection,
    normalize_speechmatics_region,
    parse_custom_vocabulary,
    remote_batch_part_limit,
    speechmatics_model_available_in,
)
from ..ssl_utils import create_ssl_context
from ..ssl_utils import is_ssl_error as _is_ssl_error
from ._audio_parts import transcribe_in_parts
from ._http_utils import (
    audio_content_type,
    format_ssl_error_message,
    http_error_suffix,
    multipart_form_data,
    normalize_transcript_text,
)
from ._job_poll import fetch_with_retries, poll_job
from .base import (
    AudioInput,
    ITranscriber,
    ProgressReporter,
    StreamingCallback,
    TranscriptionError,
)

_PROVIDER = "Speechmatics"
_UPLOAD_PROGRESS = "Uploading audio to Speechmatics and waiting for transcription..."
# `JobDetails.status`: running, done, rejected, deleted, expired. Every one
# but `running` ends the job; a status a later API version adds is waited out.
_TERMINAL_STATUSES = ("done", "rejected", "deleted", "expired")
# Answers to a status or transcript request that a retry cannot change: the
# key is refused, or the job is gone ("404 ... status of `expired`", "410
# Gone: File Expired / File deleted from the storage").
_FINAL_HTTP_CODES = (401, 403, 404, 410)
# Enhanced and Standard need a chosen language (`SPEECHMATICS_LANGUAGE_MODES`
# has no Auto). A stored Auto reaching them -- a settings file written while
# Melia or another engine was selected -- is sent as German, the app's
# primary language, as the Cohere runtime does.
_FALLBACK_LANGUAGE = "de"
_RETENTION_NOTE = (
    "Speechmatics keeps a job's transcript for 7 days; it can be fetched from "
    "its jobs API until then."
)


class SpeechmaticsTranscriber(ProgressReporter, ITranscriber):
    def __init__(
        self,
        api_key: str,
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        model: str = DEFAULT_SPEECHMATICS_MODEL,
        region: str = "",
        custom_vocabulary: str = DEFAULT_CUSTOM_VOCABULARY,
        silence_gate_threshold: float = DEFAULT_SILENCE_GATE_THRESHOLD,
        request_timeout_s: int = 120,
        poll_interval_s: float = SPEECHMATICS_POLL_INTERVAL_S,
        max_wait_s: float = SPEECHMATICS_BATCH_MAX_WAIT_S,
    ) -> None:
        ProgressReporter.__init__(self)
        if not api_key:
            raise TranscriptionError(
                "Speechmatics API key is missing. "
                "Enter your key in Settings -> Providers."
            )
        self._api_key = api_key
        self._model = (
            model if model in SPEECHMATICS_MODELS else DEFAULT_SPEECHMATICS_MODEL
        )
        self._region = normalize_speechmatics_region(region)
        if not speechmatics_model_available_in(self._model, self._region):
            # "Melia 1 is available for Batch transcription in the EU and US
            # regions only": the job could only be rejected after the upload.
            raise TranscriptionError(
                f"{SPEECHMATICS_MELIA_UNAVAILABLE_TEXT} Choose the eu1 or us1 "
                "region, or the Enhanced or Standard model."
            )
        self._base_url = f"https://{SPEECHMATICS_API_HOSTS[self._region]}/v2"
        # Needs self._model, so this must run after it is assigned above.
        self.set_language_mode(language_mode)
        # Sent by Enhanced and Standard only (`_job_config`); Melia's custom
        # dictionary is "Not yet.", and the factory does not hand it the terms.
        self._vocabulary = parse_custom_vocabulary(custom_vocabulary)
        self._silence_gate_threshold = float(silence_gate_threshold)
        self._request_timeout_s = max(5, int(request_timeout_s))
        self._poll_interval_s = max(0.0, float(poll_interval_s))
        self._max_wait_s = max(0.0, float(max_wait_s))

    def _normalize_language_mode(self, mode: str) -> str:
        normalized = (mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        supported = language_modes_for_selection("speechmatics", self._model)
        if normalized in supported:
            return normalized
        if DEFAULT_LANGUAGE_MODE in supported:
            return DEFAULT_LANGUAGE_MODE
        return _FALLBACK_LANGUAGE

    def _job_config(self) -> dict[str, Any]:
        """The `config` field of the job, by model.

        Melia: `"language": "multi"` with an optional `language_hints` list --
        the page's own example is `{"model": "melia-1", "language": "multi",
        "language_hints": ["en", "ar"]}`. Enhanced and Standard: the language
        code, plus `additional_vocab` entries for the custom vocabulary (up to
        20,000 items; the app sends at most 100).
        """
        language = SPEECHMATICS_LANGUAGE_CODES.get(
            self._language_mode, self._language_mode
        )
        transcription: dict[str, Any] = {"model": self._model}
        if self._model == SPEECHMATICS_MELIA_MODEL:
            transcription["language"] = "multi"
            if self._language_mode != DEFAULT_LANGUAGE_MODE:
                transcription["language_hints"] = [language]
        else:
            transcription["language"] = language
            if self._vocabulary:
                transcription["additional_vocab"] = [
                    {"content": term} for term in self._vocabulary
                ]
        return {"type": "transcription", "transcription_config": transcription}

    def _request(self, url: str, *, data: bytes | None = None, content_type: str = ""):
        request = urllib.request.Request(
            url, data=data, method="POST" if data else "GET"
        )
        request.add_header("Authorization", f"Bearer {self._api_key}")
        if content_type:
            request.add_header("Content-Type", content_type)
        return urllib.request.urlopen(
            request, timeout=self._request_timeout_s, context=create_ssl_context()
        )

    def _format_error(self, exc: Exception) -> str:
        if _is_ssl_error(exc):
            return format_ssl_error_message(_PROVIDER)
        return str(exc)

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        return transcribe_in_parts(
            audio_source,
            self._transcribe_request,
            limit=remote_batch_part_limit("speechmatics", self._model),
            progress_text=_UPLOAD_PROGRESS,
            raise_if_canceled=self._raise_if_canceled,
            silence_threshold=self._silence_gate_threshold,
        )

    def _transcribe_request(self, audio_source: AudioInput, progress_text: str) -> str:
        """One job, for a whole recording or one part of it."""
        try:
            if isinstance(audio_source, bytes):
                audio_bytes = bytes(audio_source)
                filename = "audio.wav"
            else:
                path = Path(audio_source)
                audio_bytes = path.read_bytes()
                filename = path.name or "audio.wav"
            body, content_type = multipart_form_data(
                fields=[("config", json.dumps(self._job_config()))],
                file_field=(
                    "data_file",
                    filename,
                    audio_bytes,
                    audio_content_type(filename),
                ),
            )
            self._emit_progress(progress_text)
            with self._request(
                f"{self._base_url}/jobs/", data=body, content_type=content_type
            ) as resp:
                created = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            raise TranscriptionError(self._submit_error(exc)) from exc
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError(
                f"Speechmatics transcription failed: {self._format_error(exc)}"
            ) from exc

        job_id = str(created.get("id") or "") if isinstance(created, dict) else ""
        job = poll_job(
            lambda: self._fetch_job(job_id),
            status_of=lambda answer: answer["status"],
            terminal=_TERMINAL_STATUSES,
            provider=_PROVIDER,
            job_id=job_id,
            max_wait_s=self._max_wait_s,
            interval_s=self._poll_interval_s,
        )
        if job["status"] != "done":
            raise TranscriptionError(self._job_failure(job, job_id))
        text = fetch_with_retries(
            lambda: self._fetch_transcript(job_id),
            provider=_PROVIDER,
            job_id=job_id,
            interval_s=self._poll_interval_s,
            note=_RETENTION_NOTE,
        )
        return normalize_transcript_text(text)

    @staticmethod
    def _submit_error(exc: urllib.error.HTTPError) -> str:
        if exc.code in (401, 403):
            return (
                f"Speechmatics: Authentication failed (HTTP {exc.code}). "
                "The API key is invalid or expired."
            )
        if exc.code == 429:
            return (
                "Speechmatics: Rate limit exceeded (HTTP 429). "
                "Wait a moment and try again."
            )
        return f"Speechmatics transcription failed (HTTP {exc.code}){http_error_suffix(exc)}"

    def _fetch_job(self, job_id: str) -> dict[str, Any]:
        """One status request. A `TranscriptionError` ends the poll at once;
        anything else raised here is a failed fetch the poll retries."""
        try:
            with self._request(f"{self._base_url}/jobs/{job_id}") as resp:
                answer = json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            if exc.code in _FINAL_HTTP_CODES:
                raise TranscriptionError(
                    f"Speechmatics could not report the job's status (HTTP "
                    f"{exc.code}){http_error_suffix(exc)}. Job id {job_id}."
                ) from exc
            raise
        job = answer.get("job") if isinstance(answer, dict) else None
        if not isinstance(job, dict) or not isinstance(job.get("status"), str):
            # Retried by the poll: an answer without a status says nothing.
            raise ValueError("the status answer has no job status")
        return job

    def _fetch_transcript(self, job_id: str) -> str:
        try:
            with self._request(
                f"{self._base_url}/jobs/{job_id}/transcript?format=txt"
            ) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code in _FINAL_HTTP_CODES:
                raise TranscriptionError(
                    f"Speechmatics finished the job, but its transcript could not "
                    f"be fetched (HTTP {exc.code}){http_error_suffix(exc)}. Job id "
                    f"{job_id}."
                ) from exc
            raise

    @staticmethod
    def _job_failure(job: dict[str, Any], job_id: str) -> str:
        messages = [
            str(error.get("message")).strip()
            for error in job.get("errors") or []
            if isinstance(error, dict) and str(error.get("message") or "").strip()
        ]
        reason = f": {'; '.join(messages)[:300]}" if messages else ""
        return (
            f"Speechmatics did not transcribe the recording (job status "
            f"{job['status']}){reason}. Job id {job_id}."
        )

    def test_connection(self) -> tuple[bool, str]:
        try:
            with self._request(f"{self._base_url}/jobs/") as resp:
                if resp.status == 200:
                    return True, "Connection OK — API key is valid."
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return False, (
                    f"Authentication failed (HTTP {exc.code}). "
                    "The API key is invalid or expired."
                )
            return False, f"API returned HTTP {exc.code}{http_error_suffix(exc)}"
        except Exception as exc:
            return False, f"Connection failed: {self._format_error(exc)}"
        return False, "Unexpected response from the Speechmatics API."

    def start_stream(self, on_partial: StreamingCallback | None = None) -> None:
        raise NotImplementedError(
            "Speechmatics streaming is not implemented in this project. "
            "Use batch mode, or use local/AssemblyAI/Deepgram for streaming."
        )

    def push_audio_chunk(
        self, chunk: bytes, *, block_timeout_s: float | None = None
    ) -> None:
        raise NotImplementedError(
            "Speechmatics streaming is not implemented in this project."
        )

    def stop_stream(self) -> str:
        raise NotImplementedError(
            "Speechmatics streaming is not implemented in this project."
        )

    def abort_stream(self) -> None:
        raise NotImplementedError(
            "Speechmatics streaming is not implemented in this project."
        )
