"""Local NeMo ASR models served by the pure-Python `onnx-asr` runtime.

This is a third local ONNX path, separate from the Cohere/Granite Node runtime
(`local_webgpu_asr`) and from Nemotron's ONNX Runtime GenAI path
(`local_nemotron`). It needs no Node.js and no additional ONNX Runtime: onnx-asr
resolves the same `onnxruntime` distribution the app already carries.

Both models are batch-only. Their download, cache detection, size estimation and
deletion go through the shared layouts in `local_webgpu_asr`, so only inference
lives here.
"""

from __future__ import annotations

import io
import logging
import threading
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np

from ..config import (
    AUDIO_SAMPLE_RATE,
    CANARY_MODEL_SIZE,
    DEFAULT_LANGUAGE_MODE,
    DOC_MODELS_PATH,
    LOCAL_ONNX_ASR_MODEL_SIZES,
    PARAKEET_MODEL_SIZE,
    language_modes_for_selection,
)
from .base import (
    AudioInput,
    ITranscriber,
    ProgressReporter,
    TranscriptionCanceled,
    TranscriptionError,
    canceled_download_is_a_cancel,
)

logger = logging.getLogger(__name__)

# onnx-asr's registry name for each app model.
_ONNX_ASR_MODEL_NAMES: dict[str, str] = {
    PARAKEET_MODEL_SIZE: "nemo-parakeet-tdt-0.6b-v3",
    CANARY_MODEL_SIZE: "nemo-canary-1b-v2",
}

# How often the watchdog asks whether the user cancelled. ONNX Runtime reacts
# to the terminate flag within a few milliseconds, so this interval is the
# whole perceived cancel latency.
_CANCEL_POLL_INTERVAL_S = 0.1
# Depth of the search for the model's ONNX sessions. Verified against onnx-asr
# 0.12: two levels for the encoder/decoder (``model.asr._encoder``), three for
# the resamplers (``model.resampler._preprocessors[rate]``) and **four** for
# the mel preprocessor, which onnx-asr wraps twice
# (``model.asr._preprocessor.preprocessor._preprocessor``, i.e.
# ``ConcurrentPreprocessor`` -> ``OnnxPreprocessor`` -> ``InferenceSession``).
# 5 leaves one level of headroom.
_SESSION_SEARCH_MAX_DEPTH = 5


class _RunAbortHandle:
    """The abort switch shared by every ONNX Runtime call of one recognize().

    ONNX Runtime aborts a run that is already executing when ``terminate`` is
    set on the ``RunOptions`` it was started with, from any thread. That is the
    only way to interrupt this runtime: onnx-asr exposes no callback, and its
    encoder pass is a single call that runs for seconds on a long recording.
    """

    def __init__(self) -> None:
        import onnxruntime as rt

        self.options = rt.RunOptions()
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True
        # Latches: ONNX Runtime never clears it, which is why each transcription
        # builds a fresh handle instead of reusing one.
        self.options.terminate = True


class _CancelWatchdog:
    """Polls the cancel check and trips the abort handle when it fires."""

    def __init__(
        self,
        handle: _RunAbortHandle,
        cancel_check: Callable[[], bool] | None,
    ) -> None:
        self._handle = handle
        self._cancel_check = cancel_check
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._cancel_check is None:
            return
        self._thread = threading.Thread(
            target=self._poll,
            name="stt_app_onnx_asr_cancel",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=_CANCEL_POLL_INTERVAL_S * 5)

    def _poll(self) -> None:
        check = self._cancel_check
        if check is None:
            return
        while not self._stop.wait(_CANCEL_POLL_INTERVAL_S):
            try:
                canceled = bool(check())
            except Exception:
                logger.exception("onnx-asr cancel check raised; ignoring")
                return
            if canceled:
                self._handle.abort()
                return


def _pcm_bytes_to_float32(data: bytes) -> np.ndarray:
    """Convert 16-bit little-endian PCM to the float32 waveform onnx-asr wants."""
    if len(data) % 2:
        data = data[:-1]
    samples = np.frombuffer(data, dtype="<i2").astype(np.float32)
    return samples / 32768.0


def _read_wav_float32(source: str | Path | io.BytesIO) -> tuple[np.ndarray, int]:
    """Decode a 16-bit PCM WAV from a path or an in-memory buffer.

    Both the path and the bytes branch go through here so they cannot drift on
    validation; an earlier copy of this logic omitted the sample-width check and
    silently reinterpreted 24-bit audio as 16-bit.
    """
    handle_source = source if isinstance(source, io.BytesIO) else str(source)
    try:
        with wave.open(handle_source, "rb") as handle:
            sample_width = handle.getsampwidth()
            channels = handle.getnchannels()
            sample_rate = handle.getframerate()
            frames = handle.readframes(handle.getnframes())
    except TranscriptionError:
        raise
    except Exception as exc:
        raise TranscriptionError(f"Could not read WAV audio: {exc}") from exc
    if sample_width != 2:
        raise TranscriptionError(
            "Only 16-bit PCM WAV audio is supported by this local model."
        )
    waveform = _pcm_bytes_to_float32(frames)
    if channels > 1:
        # Average to mono; the models are single-channel.
        usable = (waveform.size // channels) * channels
        waveform = waveform[:usable].reshape(-1, channels).mean(axis=1)
    return waveform, sample_rate


class LocalOnnxAsrTranscriber(ITranscriber, ProgressReporter):
    """Batch transcription for Parakeet TDT and Canary via `onnx-asr`."""

    def __init__(
        self,
        model_size: str,
        language_mode: str = DEFAULT_LANGUAGE_MODE,
        *,
        offline_mode: bool = False,
        model_dir: str = "",
    ) -> None:
        ProgressReporter.__init__(self)
        if model_size not in LOCAL_ONNX_ASR_MODEL_SIZES:
            raise TranscriptionError(f"Unsupported onnx-asr model '{model_size}'.")
        self.model_size = model_size
        self.offline_mode = bool(offline_mode)
        self.model_dir = model_dir or ""
        self._language_mode = self._normalize_language_mode(language_mode)
        self._model: object | None = None
        self._model_lock = threading.Lock()
        # One recognize() at a time: the abort handle below is per call and the
        # wrapped sessions are shared, so overlapping runs would let a cancel
        # for one abort the other.
        self._inference_lock = threading.Lock()
        self._abort_handle: _RunAbortHandle | None = None
        # The sessions whose run() we replaced, so close() can put the
        # originals back -- see `_unwrap_cancel_hooks`.
        self._wrapped_sessions: list[object] = []
        # Reported to the benchmark and the runtime status line. This runtime is
        # CPU-only by construction: onnx-asr offers no DirectML path that can
        # coexist with the ONNX Runtime the app already ships (installing
        # `onnxruntime-directml` overwrites it and breaks onnxruntime-genai).
        self.runtime_device = "cpu"
        self.gpu_available = False
        self.runtime_details_text = ""
        self.runtime_warning = (
            "This model runs on CPU. onnx-asr has no GPU path here that can "
            "coexist with the app's ONNX Runtime."
        )

    def runtime_status_text(self) -> str:
        return "onnx-asr runtime active on CPU"

    # -- language ---------------------------------------------------------

    def _normalize_language_mode(self, mode: str) -> str:
        """Restrict to the modes this model was actually trained for.

        Canary has no auto-detect: `onnx-asr` hardcodes the `<|en|>` source and
        target token, so an unset language makes it *translate* into English
        instead of transcribing. It therefore never accepts ``auto``, and an
        untrained code would raise ``KeyError`` deep inside the runtime, so it
        is rejected here instead.
        """
        requested = str(mode or DEFAULT_LANGUAGE_MODE).strip().lower()
        supported = language_modes_for_selection("local", self.model_size)
        if requested in supported:
            return requested
        fallback = supported[0]
        # Never silently: for Canary a *wrong* language is as destructive as
        # none, because it translates into the requested language instead of
        # transcribing. The UI keeps `auto` out of its picker, but a stored
        # `auto` can still arrive here from an older history entry or another
        # engine's settings snapshot.
        logger.warning(
            "Language '%s' is not supported by '%s'; using '%s' instead. "
            "A wrong language makes this model translate rather than "
            "transcribe.",
            requested,
            self.model_size,
            fallback,
        )
        return fallback

    def _recognize_kwargs(self) -> dict[str, str]:
        # Parakeet TDT v3 is implicitly multilingual and ignores the argument,
        # so passing it would only invite the illusion of control.
        if self.model_size == PARAKEET_MODEL_SIZE:
            return {}
        return {"language": self._language_mode}

    # -- model lifecycle --------------------------------------------------

    def _resolve_model_path(self) -> Path:
        from .local_webgpu_asr import resolve_cached_webgpu_model_path

        cached = resolve_cached_webgpu_model_path(self.model_size, self.model_dir)
        if cached is not None:
            return cached
        if self.offline_mode:
            raise TranscriptionError(
                f"Local model '{self.model_size}' is not cached locally. "
                f"Disable Offline mode or download it first. See {DOC_MODELS_PATH}."
            )
        from ..model_download_coordinator import run_coordinated_download
        from .local_faster_whisper import download_model_snapshot

        # Through the single slot, like every other download in the process.
        with canceled_download_is_a_cancel():
            run_coordinated_download(
                self.model_size,
                self.model_dir,
                lambda: download_model_snapshot(self.model_size, self.model_dir),
                # `_is_cancel_requested`, not the raw attribute: a check that
                # raises must never fail the work, and the coordinator re-raises
                # whatever escapes it.
                cancel_check=self._is_cancel_requested,
            )
        cached = resolve_cached_webgpu_model_path(self.model_size, self.model_dir)
        if cached is None:
            raise TranscriptionError(
                f"Downloaded '{self.model_size}' but no complete int8 snapshot "
                f"was found. See {DOC_MODELS_PATH}."
            )
        return cached

    def _load_model(self) -> object:
        try:
            import onnx_asr
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise TranscriptionError(
                "The onnx-asr runtime is not installed. Reinstall the "
                "application dependencies and try again."
            ) from exc

        model_path = self._resolve_model_path()
        self._emit_progress(
            f"Loading {self.model_size}: {self.runtime_status_text()}..."
        )
        try:
            model = onnx_asr.load_model(
                _ONNX_ASR_MODEL_NAMES[self.model_size],
                str(model_path),
                quantization="int8",
            )
        except Exception as exc:
            raise TranscriptionError(
                f"Failed to load local model '{self.model_size}': {exc}"
            ) from exc
        self._install_cancel_hooks(model)
        return model

    def _install_cancel_hooks(self, model: object) -> int:
        """Route every ONNX Runtime call of this model through our RunOptions.

        onnx-asr has no cancel hook of its own: ``recognize()`` is one blocking
        call, and for a minute of audio its encoder pass alone runs for seconds
        while pressing Cancel does nothing -- the transcription keeps a CPU core
        busy and blocks the single transcription worker behind it. Wrapping the
        sessions is what makes ``RunOptions.terminate`` reachable.

        Returns the number of wrapped sessions. Zero means the runtime's layout
        changed; the transcription still runs, only its mid-run cancel is gone.
        """
        import onnxruntime as rt

        sessions: list[object] = []
        seen: set[int] = set()
        # An explicit work list rather than a recursive local function: a
        # closure that calls itself is a reference cycle through its own cell,
        # and the cell also holds `sessions`. That kept every session alive
        # until the cyclic collector ran -- defeating the unwrapping below.
        pending: list[tuple[object, int]] = [(model, 0)]
        while pending:
            obj, depth = pending.pop()
            if depth > _SESSION_SEARCH_MAX_DEPTH or id(obj) in seen:
                continue
            seen.add(id(obj))
            if isinstance(obj, rt.InferenceSession):
                sessions.append(obj)
                continue
            if isinstance(obj, dict):
                children: object = obj.values()
            elif isinstance(obj, (list, tuple, set)):
                children = obj
            elif hasattr(obj, "__dict__"):
                children = vars(obj).values()
            else:
                continue
            # Snapshot: ``vars(obj).values()`` is a live view.
            pending.extend(
                (child, depth + 1)
                for child in list(children)  # type: ignore[call-overload]
            )

        self._unwrap_cancel_hooks()
        for session in sessions:
            self._wrap_session_run(session)
        self._wrapped_sessions = sessions
        if not sessions:
            logger.warning(
                "No ONNX sessions found on the loaded '%s' model; a running "
                "transcription cannot be canceled.",
                self.model_size,
            )
        return len(sessions)

    def _wrap_session_run(self, session: object) -> None:
        original = session.run  # type: ignore[attr-defined]

        def run_with_abort(
            output_names: object,
            input_feed: object,
            run_options: object = None,
            _original: Callable[..., object] = original,
        ) -> object:
            handle = self._abort_handle
            if handle is None:
                return _original(output_names, input_feed, run_options)
            if handle.aborted:
                # Between two calls of a decode loop: stop without waiting for
                # ONNX Runtime to notice the flag.
                raise TranscriptionCanceled()
            if run_options is not None:
                logger.debug(
                    "Replacing caller-supplied RunOptions so the run stays "
                    "cancelable; onnx-asr 0.12 passes none."
                )
            return _original(output_names, input_feed, handle.options)

        session.run = run_with_abort  # type: ignore[attr-defined]

    def _unwrap_cancel_hooks(self) -> None:
        """Restore each session's own ``run`` so the model can be freed.

        ``session.run = run_with_abort`` stores the wrapper in the session's
        instance ``__dict__``, and the wrapper holds the original *bound*
        method, whose ``__self__`` is that same session. That is a reference
        cycle, so dropping ``self._model`` freed nothing until the cyclic
        collector happened to run a generation-2 pass -- a several-hundred-
        megabyte runtime stayed resident after ``close()``, which is exactly
        the symptom cancelling was supposed to fix. The wrapper also closes
        over ``self``, so the transcriber was pinned by the session too.
        """
        for session in self._wrapped_sessions:
            try:
                session.__dict__.pop("run", None)
            except Exception:  # pragma: no cover - defensive
                logger.debug(
                    "Could not restore an ONNX session's run().", exc_info=True
                )
        self._wrapped_sessions = []

    def preload_model(self) -> None:
        with self._model_lock:
            if self._model is None:
                self._model = self._load_model()

    def close(self) -> None:
        # `_inference_lock` too, in the same order `transcribe_batch` takes
        # them. Unwrapping while a `recognize()` is in flight would silently
        # switch that run back to the unhooked `run`: the watchdog would keep
        # setting `terminate` on a `RunOptions` nobody passes any more, and
        # the run would finish in full with no log line -- turning the cancel
        # this class exists for back off. No caller reaches that today (every
        # close path waits for the runtime lease), but the class must not
        # depend on that.
        with self._model_lock, self._inference_lock:
            self._unwrap_cancel_hooks()
            self._model = None

    # -- transcription ----------------------------------------------------

    def transcribe_batch(self, audio_source: AudioInput) -> str:
        self._raise_if_canceled()
        if isinstance(audio_source, (str, Path)):
            waveform, sample_rate = _read_wav_float32(Path(audio_source))
        elif isinstance(audio_source, (bytes, bytearray)):
            payload = bytes(audio_source)
            if payload[:4] == b"RIFF":
                waveform, sample_rate = _read_wav_float32(io.BytesIO(payload))
            else:
                waveform = _pcm_bytes_to_float32(payload)
                sample_rate = AUDIO_SAMPLE_RATE
        else:
            raise TranscriptionError("Unsupported audio input for a local model.")

        if waveform.size == 0:
            return ""

        with self._model_lock:
            if self._model is None:
                self._model = self._load_model()
            model = self._model

        self._emit_progress(f"Transcribing with {self.runtime_status_text()}...")
        with self._inference_lock:
            self._raise_if_canceled()
            if self._model is not model:
                # `close()` takes both locks, but this method takes them
                # *sequentially* -- it releases `_model_lock` above before
                # acquiring this one. A close landing in that gap gets both
                # uncontended and unwraps the sessions this run is about to
                # use, so the watchdog below would set `terminate` on a
                # `RunOptions` nobody passes and the run would finish in full
                # with no log line: the cancel silently off. Nothing reaches
                # that today (the runtime lease serialises every close path),
                # and this is what keeps that from being a dependency on the
                # callers.
                # A `TranscriptionError`, not a cancel. Every `close()`
                # path is serialised against this one today, so the branch is
                # defensive -- the resume-driven reset in particular cannot
                # reach this class at all, because it filters on
                # `LOCAL_WEBGPU_MODEL_SIZES`, which holds neither Parakeet nor
                # Canary. What decides the type is what each one does to the
                # recording: the controller's cancel path calls
                # `_drop_request_audio`, discarding the WAV and writing no
                # overlay state, while the error path promotes the audio for
                # Retry and shows the reason. Nobody asked to stop here, so
                # losing the recording would be wrong.
                raise TranscriptionError(
                    "The local runtime was closed while this transcription "
                    "was starting. Try again."
                )
            handle = _RunAbortHandle()
            self._abort_handle = handle
            # The base method, like every other call site: `_poll` gives
            # up permanently on the first raise, so the raw attribute
            # would switch the mid-run cancel off for this transcription
            # with no log line.
            watchdog = _CancelWatchdog(handle, self._is_cancel_requested)
            watchdog.start()
            try:
                text = model.recognize(  # type: ignore[attr-defined]
                    waveform,
                    sample_rate=sample_rate,
                    **self._recognize_kwargs(),
                )
            except TranscriptionCanceled:
                raise
            except Exception as exc:
                if handle.aborted:
                    # ONNX Runtime reports the abort as a generic Fail; it is a
                    # user cancel, not a transcription failure.
                    raise TranscriptionCanceled() from exc
                raise TranscriptionError(
                    f"Local transcription failed for '{self.model_size}': {exc}"
                ) from exc
            finally:
                watchdog.stop()
                self._abort_handle = None
        return str(text or "").strip()
