"""Alibaba Fun-ASR remote transcription provider (DashScope Model Studio).

Fun-ASR is Alibaba's LLM-based speech-recognition family. Its batch
"recording file recognition" API is asynchronous and requires a publicly
reachable file URL (OSS), which does not fit a local dictation app. The
**real-time WebSocket** API, however, accepts local audio bytes, so this
provider drives that API in a batch fashion: connect, stream the recorded
audio, then collect the final transcript.

Remote, cloud-only. Needs only a DashScope API key (no per-resource endpoint);
the international (Singapore) endpoint is used by default. Note: Fun-ASR does
**not** document German support — its value here is broad coverage of Chinese
(incl. dialects) and East/Southeast-Asian languages. See
docs/funasr-and-fleurs-evaluation.md.

Protocol: DashScope WebSocket (run-task -> task-started -> binary audio ->
finish-task -> result-generated* -> task-finished).
Docs: https://www.alibabacloud.com/help/en/model-studio/real-time-speech-recognition
"""

from __future__ import annotations

import io
import json
import time
import uuid
import wave
from pathlib import Path

from ..config import (
    AUDIO_SAMPLE_RATE,
    DEFAULT_FUNASR_MODEL,
    DEFAULT_LANGUAGE_MODE,
    FUNASR_BATCH_MAX_WAIT_S,
    FUNASR_LANGUAGE_HINTS,
    FUNASR_MODELS,
    FUNASR_WS_URL_INTL,
    language_modes_for_selection,
)
from ..ssl_utils import create_ssl_context
from ..ssl_utils import is_ssl_error as _is_ssl_error
from ._http_utils import format_ssl_error_message, normalize_transcript_text
from .base import (
    AudioInput,
    ITranscriber,
    ProgressReporter,
    StreamingCallback,
    TranscriptionError,
)

# Send audio in ~256 ms chunks (8192 bytes of 16 kHz mono PCM16).
_AUDIO_CHUNK_BYTES = 8192


class _FunAsrInterrupted(Exception):
    """The stream ended before the service said the task was finished.

    Private to this module: every caller turns it into a `TranscriptionError`
    with its own context, and `_collect_transcript` additionally names the
    text the service had already delivered.
    """


# The recovered text goes into a user-facing error, and the overlay's error
# detail is selectable, so it can be copied out of the failure. Bounded so a
# very long dictation cannot fill the overlay end to end; the audio is kept
# for Retry either way.
_RECOVERED_TEXT_MAX_CHARS = 2000
# Consecutive frames that are not events (binary, non-JSON, non-object)
# before the receive loop gives up. A real socket blocks in `recv`, so
# the loop cannot spin on its own; a peer that floods unusable frames
# could make it, and the total budget alone would then pin a core and
# the single transcription worker for up to thirty minutes (measured:
# 1.37 million receive calls in 0.31 s against an instant fake). The
# service never sends such frames, so any real flood is a fault.
_MAX_UNUSABLE_FRAMES = 1000


class FunAsrTranscriber(ProgressReporter, ITranscriber):
    """Batch transcription via Alibaba Fun-ASR over the DashScope WebSocket API.

    Parameters
    ----------
    api_key : str
        DashScope API key (Singapore/international region). Required.
    language_mode : str
        ``"auto"`` for multilingual auto-detect, or a supported language code
        (sent as a ``language_hints`` entry). German is not supported.
    model : str
        Fun-ASR realtime model. Defaults to ``fun-asr-realtime``.
    ws_url : str
        DashScope inference WebSocket URL. Defaults to the Singapore endpoint.
    request_timeout_s : int
        Overall socket/read timeout budget.
    """

    def __init__(
        self,
        api_key: str,
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        model: str = DEFAULT_FUNASR_MODEL,
        ws_url: str = FUNASR_WS_URL_INTL,
        request_timeout_s: int = 60,
    ) -> None:
        ProgressReporter.__init__(self)
        if not api_key:
            raise TranscriptionError(
                "Fun-ASR (DashScope) API key is missing. "
                "Enter your key in Settings -> Remote Provider API Keys."
            )
        self._api_key = api_key
        self._model = model if model in FUNASR_MODELS else DEFAULT_FUNASR_MODEL
        # Needs self._model, so this must run after it is assigned above.
        self.set_language_mode(language_mode)
        self._ws_url = ws_url or FUNASR_WS_URL_INTL
        self._request_timeout_s = max(5, int(request_timeout_s))
        self._ws_module = None

    def _normalize_language_mode(self, mode: str) -> str:
        normalized = (mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        if normalized not in language_modes_for_selection("funasr", self._model):
            normalized = DEFAULT_LANGUAGE_MODE
        return normalized

    # -- websocket-client lazy import ------------------------------------------

    def _get_websocket_module(self):
        if self._ws_module is not None:
            return self._ws_module
        try:
            import websocket  # type: ignore
        except ImportError as exc:
            raise TranscriptionError(
                "Fun-ASR requires 'websocket-client'. "
                "Install it with: pip install websocket-client"
            ) from exc
        self._ws_module = websocket
        return websocket

    def _connect(self, timeout_s: float):
        websocket = self._get_websocket_module()
        sslopt = None
        ssl_ctx = create_ssl_context()
        if ssl_ctx is not None:
            sslopt = {"context": ssl_ctx}
        return websocket.create_connection(
            self._ws_url,
            header=[f"Authorization: bearer {self._api_key}"],
            timeout=timeout_s,
            sslopt=sslopt,
        )

    # -- Request building -------------------------------------------------------

    def _funasr_language(self) -> str:
        return FUNASR_LANGUAGE_HINTS.get(self._language_mode, self._language_mode)

    def _run_task_message(self, task_id: str, sample_rate: int) -> str:
        # `heartbeat` is documented on the vendor's client-events page as
        # default false, with the contract that the connection is then closed
        # after a period of continuously silent audio. A recording with a
        # long pause is exactly that, and this provider uploads the whole
        # recording over the realtime protocol. Unverified against the live
        # service from here; the CLOSE-frame handling in `_recv_event` is
        # what bounds the damage if it is ignored.
        parameters: dict = {
            "format": "pcm",
            "sample_rate": int(sample_rate),
            "heartbeat": True,
        }
        if self._language_mode != DEFAULT_LANGUAGE_MODE:
            parameters["language_hints"] = [self._funasr_language()]
        return json.dumps(
            {
                "header": {
                    "action": "run-task",
                    "task_id": task_id,
                    "streaming": "duplex",
                },
                "payload": {
                    "task_group": "audio",
                    "task": "asr",
                    "function": "recognition",
                    "model": self._model,
                    "parameters": parameters,
                    "input": {},
                },
            }
        )

    @staticmethod
    def _finish_task_message(task_id: str) -> str:
        return json.dumps(
            {
                "header": {
                    "action": "finish-task",
                    "task_id": task_id,
                    "streaming": "duplex",
                },
                "payload": {"input": {}},
            }
        )

    @staticmethod
    def _pcm_from_audio(audio_source: AudioInput) -> tuple[bytes, int]:
        """Return ``(pcm16_mono_bytes, sample_rate)`` from a WAV input."""
        if isinstance(audio_source, bytes):
            raw = bytes(audio_source)
        else:
            raw = Path(audio_source).read_bytes()
        try:
            with wave.open(io.BytesIO(raw), "rb") as wav:
                n_channels = wav.getnchannels()
                sampwidth = wav.getsampwidth()
                sample_rate = wav.getframerate()
                frames = wav.readframes(wav.getnframes())
        except (wave.Error, EOFError) as exc:
            raise TranscriptionError(
                "Fun-ASR expects WAV/PCM audio; the input could not be parsed "
                "as a WAV file."
            ) from exc
        if sampwidth != 2:
            raise TranscriptionError(
                "Fun-ASR expects 16-bit PCM audio."
            )
        if n_channels > 1:
            import numpy as np

            samples = np.frombuffer(frames, dtype=np.int16)
            samples = samples.reshape(-1, n_channels).mean(axis=1)
            frames = samples.astype(np.int16).tobytes()
        return frames, sample_rate or AUDIO_SAMPLE_RATE

    # -- Event handling ---------------------------------------------------------

    def _recv_event(self, ws, deadline: float) -> dict:
        """Receive one JSON event, skipping any binary frames.

        Bounded in total, and the socket timeout is tightened to whatever is
        left of that budget. `websocket-client` answers a server PING inside
        `recv_data_frame` without ever returning to its caller, and each
        arriving frame restarts the socket timeout, so a service that pings
        but never sends `task-finished` parked this loop forever -- holding
        the app's single `max_workers=1` transcription worker and, through
        that executor's atexit join, stopping the process from exiting.
        Measured against a connection that never finishes: 198 receive calls
        in 4 s with no exit in sight.
        """
        unusable = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _FunAsrInterrupted(
                    "Fun-ASR did not finish the transcription within "
                    f"{int(FUNASR_BATCH_MAX_WAIT_S / 60)} minutes."
                )
            if unusable > _MAX_UNUSABLE_FRAMES:
                raise _FunAsrInterrupted(
                    f"Fun-ASR sent {unusable} frames in a row that were not "
                    "events."
                )
            ws.settimeout(min(remaining, self._request_timeout_s))
            message = ws.recv()
            unusable += 1
            if isinstance(message, (bytes, bytearray)):
                continue
            if not message:
                # `websocket-client` returns "" for every opcode that is
                # neither TEXT nor BINARY, and PING/PONG never reach here
                # because `recv_data_frame` answers them itself -- so this is
                # the server's CLOSE frame. Reading it as "nothing yet, keep
                # waiting" spun this loop until some later socket error, which
                # then reported a connection fault instead of a closed stream
                # (measured: 202 receive calls after the close, still going).
                raise _FunAsrInterrupted(
                    "Fun-ASR closed the connection before the transcription "
                    "was finished."
                )
            try:
                payload = json.loads(message)
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict):
                return payload

    @staticmethod
    def _event_name(message: dict) -> str:
        header = message.get("header")
        if isinstance(header, dict):
            return str(header.get("event", ""))
        return ""

    @staticmethod
    def _sentence_from(message: dict) -> tuple[str, bool]:
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return "", False
        output = payload.get("output")
        if not isinstance(output, dict):
            return "", False
        sentence = output.get("sentence")
        if isinstance(sentence, dict):
            return str(sentence.get("text", "")), bool(
                sentence.get("sentence_end", False)
            )
        # Defensive fallback for a flatter schema.
        if isinstance(output.get("text"), str):
            return str(output["text"]), True
        return "", False

    def _fail_message(self, message: dict) -> str:
        header = message.get("header")
        if isinstance(header, dict):
            detail = header.get("error_message") or header.get("error_code")
            if detail:
                return f"Fun-ASR task failed: {detail}"
        return "Fun-ASR task failed."

    # -- Batch transcription ----------------------------------------------------

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        try:
            pcm, sample_rate = self._pcm_from_audio(audio_source)
        except TranscriptionError:
            raise
        except FileNotFoundError as exc:
            raise TranscriptionError(
                "Fun-ASR transcription failed: missing file path."
            ) from exc

        task_id = uuid.uuid4().hex
        self._emit_progress(
            "Uploading audio to Fun-ASR (DashScope) and waiting for transcription..."
        )
        try:
            ws = self._connect(self._request_timeout_s)
        except Exception as exc:
            raise self._connect_error(exc) from exc

        deadline = time.monotonic() + FUNASR_BATCH_MAX_WAIT_S
        try:
            ws.send(self._run_task_message(task_id, sample_rate))
            started = self._recv_event(ws, deadline)
            event = self._event_name(started)
            if event == "task-failed":
                raise TranscriptionError(self._fail_message(started))
            if event != "task-started":
                raise TranscriptionError(
                    f"Fun-ASR: unexpected first event '{event or 'none'}'."
                )

            # Stream the whole recording, then read results. This is sized for
            # dictation-length clips; a multi-minute file could in theory back
            # up the socket buffers (the streaming path would need a sender
            # thread, as Deepgram does).
            for offset in range(0, len(pcm), _AUDIO_CHUNK_BYTES):
                ws.send_binary(pcm[offset : offset + _AUDIO_CHUNK_BYTES])

            ws.send(self._finish_task_message(task_id))
            return self._collect_transcript(ws, deadline)
        except TranscriptionError:
            raise
        except _FunAsrInterrupted as exc:
            raise TranscriptionError(str(exc)) from exc
        except Exception as exc:
            if _is_ssl_error(exc):
                raise TranscriptionError(format_ssl_error_message("Fun-ASR")) from exc
            raise TranscriptionError(
                f"Fun-ASR transcription failed: {exc}"
            ) from exc
        finally:
            try:
                ws.close()
            except Exception:
                pass

    @staticmethod
    def _recovered_suffix(finalized: list[str], current: str) -> str:
        """Name what the service had already delivered, if anything.

        Every exit other than `task-finished` used to discard the sentences
        already received -- measured: two finished sentences lost to a server
        close, to a read timeout, and to a `task-failed`. The WAV is kept for
        Retry, so nothing is unrecoverable, but a re-run costs the whole
        upload and transcription again and this text is often the entire
        dictation minus its last words. It is reported rather than returned:
        returning it would hand back a silently truncated transcript that
        reads exactly like a complete one.
        """
        parts = [part for part in [*finalized, current] if part]
        # One guard, not two: an empty `parts` joins to "" and so does a list
        # of nothing but whitespace, and both reach the same answer here.
        text = normalize_transcript_text(" ".join(parts))
        if not text:
            return ""
        if len(text) > _RECOVERED_TEXT_MAX_CHARS:
            text = text[:_RECOVERED_TEXT_MAX_CHARS].rstrip() + "..."
        return f' Received before the failure: "{text}"'

    def _collect_transcript(self, ws, deadline: float) -> str:
        finalized: list[str] = []
        current = ""
        try:
            while True:
                message = self._recv_event(ws, deadline)
                event = self._event_name(message)
                if event == "result-generated":
                    text, sentence_end = self._sentence_from(message)
                    if sentence_end:
                        # A final carries the whole sentence and replaces the
                        # partials that led up to it. A final with *no* text
                        # ends the sentence too, and the last partial is then
                        # the best text there is for it: dropping it -- which
                        # `if text: append` on its own did, followed by the
                        # unconditional reset -- returned a truncated
                        # transcript as a clean success. Measured: partial
                        # 'Hallo', empty final, task-finished -> ''.
                        final_text = text or current
                        if final_text:
                            finalized.append(final_text)
                        current = ""
                    elif text:
                        current = text
                elif event == "task-finished":
                    break
                elif event == "task-failed":
                    raise TranscriptionError(
                        self._fail_message(message)
                        + self._recovered_suffix(finalized, current)
                    )
        except TranscriptionError:
            raise
        except _FunAsrInterrupted as exc:
            raise TranscriptionError(
                f"{exc}{self._recovered_suffix(finalized, current)}"
            ) from exc
        except Exception as exc:
            if _is_ssl_error(exc):
                raise TranscriptionError(format_ssl_error_message("Fun-ASR")) from exc
            raise TranscriptionError(
                "Fun-ASR transcription failed: "
                f"{exc}{self._recovered_suffix(finalized, current)}"
            ) from exc
        if current:
            finalized.append(current)
        return normalize_transcript_text(" ".join(p for p in finalized if p))

    # -- Error mapping ----------------------------------------------------------

    def _connect_error(self, exc: Exception) -> TranscriptionError:
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            return TranscriptionError(
                f"Fun-ASR: Authentication failed (HTTP {status}). "
                "The DashScope API key is invalid, or it is not a "
                "Singapore-region key."
            )
        if status == 429:
            return TranscriptionError(
                "Fun-ASR: Rate limit exceeded (HTTP 429). "
                "Wait a moment and try again."
            )
        if _is_ssl_error(exc):
            return TranscriptionError(format_ssl_error_message("Fun-ASR"))
        return TranscriptionError(f"Fun-ASR connection failed: {exc}")

    # -- Connection test --------------------------------------------------------

    def test_connection(self) -> tuple[bool, str]:
        """Validate the key by opening a task and waiting for ``task-started``."""
        try:
            ws = self._connect(min(self._request_timeout_s, 15))
        except Exception as exc:
            return False, str(self._connect_error(exc))
        try:
            task_id = uuid.uuid4().hex
            ws.send(self._run_task_message(task_id, AUDIO_SAMPLE_RATE))
            message = self._recv_event(
                ws, time.monotonic() + min(self._request_timeout_s, 15)
            )
            event = self._event_name(message)
            if event == "task-started":
                return True, "Connection OK — API key is valid."
            if event == "task-failed":
                return False, self._fail_message(message)
            return False, f"Unexpected response from Fun-ASR ('{event or 'none'}')."
        except Exception as exc:
            if _is_ssl_error(exc):
                return False, format_ssl_error_message("Fun-ASR")
            return False, f"Connection failed: {exc}"
        finally:
            try:
                ws.close()
            except Exception:
                pass

    # -- Streaming stubs --------------------------------------------------------
    # Fun-ASR's API is realtime, but this app wires it up for batch mode only.

    def start_stream(self, on_partial: StreamingCallback | None = None) -> None:
        raise NotImplementedError(
            "Fun-ASR streaming is not implemented in this app. Use batch mode, "
            "or use local/AssemblyAI/Deepgram for streaming."
        )

    def push_audio_chunk(self, chunk: bytes) -> None:
        raise NotImplementedError("Fun-ASR streaming is not implemented.")

    def stop_stream(self) -> str:
        raise NotImplementedError("Fun-ASR streaming is not implemented.")

    def abort_stream(self) -> None:
        raise NotImplementedError("Fun-ASR streaming is not implemented.")
