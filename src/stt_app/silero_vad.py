"""A speech check beside the energy gates: the Silero VAD v6 graph.

The energy gates in `vad.py` measure loudness, and loudness cannot tell a
knuckle on the desk from a short word -- `config.py` records both at the same
longest run. Silero VAD scores each 32 ms window for speech with a small
recurrent network, which is what separates the two.

The graph is not a new dependency. faster-whisper ships it as package data
(`faster_whisper/assets/silero_vad_v6.onnx`, 1,245,151 bytes in 1.2.1, MIT
licence, "Copyright (c) 2020-present Silero Team", github.com/snakers4/
silero-vad) for its own `vad_filter`, and ONNX Runtime -- the CPU build the app
already carries for onnx-asr and Nemotron -- runs it. The file is found with
`importlib.util.find_spec`, which locates the package without importing it:
faster-whisper's own `__init__` pulls in CTranslate2, tokenizers and the Hub
client, which a data-file lookup must not pay for.

The contract is faster-whisper's own runner (`faster_whisper.vad.
SileroVADModel`): 16 kHz mono float32, windows of 512 samples, each fed with
the 64 samples before it as context (zeros before the first window), input
`[windows, 576]`, and a recurrent state `h`/`c` of shape `[1, 1, 128]` carried
from one call to the next. One call over N windows equals N calls that carry
the state (measured bit-identical), so a long recording can be scanned block
by block and stop early.

Every failure answers ``None`` -- no faster-whisper, no file, a graph whose
inputs differ, a session that cannot be built, a run that raises, audio that
is not 16 kHz mono 16-bit. ``None`` means "could not measure", and every
caller must read it as "do not gate": this check may only ever take a
decision away from the energy gate, never add one on its own.
"""

from __future__ import annotations

import importlib.util
import io
import logging
import struct
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import (
    SILERO_BATCH_MIN_PROBABILITY,
    SILERO_BATCH_QUIET_SPEECH_GAIN,
    SILERO_LOAD_RETRY_S,
    SILERO_SPEECH_PROBABILITY,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BLOCK_WINDOWS",
    "SAMPLE_RATE_HZ",
    "SILERO_SPEECH_PROBABILITY",
    "WINDOW_SECONDS",
    "SpeechCheck",
    "SpeechMeasurement",
    "check_speech_pcm16",
    "check_speech_wav",
    "load_failed_recently",
    "loaded_session",
    "measure_speech_pcm16",
    "silero_asset_path",
    "start_loading",
]

SAMPLE_RATE_HZ = 16_000
_WINDOW_SAMPLES = 512
_CONTEXT_SAMPLES = 64
WINDOW_SECONDS = _WINDOW_SAMPLES / SAMPLE_RATE_HZ
# One run of the graph covers this many windows (~1 s). The batch scan checks
# its early stop and its budget between blocks, so the block is the unit of
# work a stopped scan may overshoot by.
BLOCK_WINDOWS = 32
_STATE_SHAPE = (1, 1, 128)
_ASSET_NAME = "silero_vad_v6.onnx"
_INPUTS = ("input", "h", "c")
_OUTPUTS = ("speech_probs", "hn", "cn")

_session_lock = threading.Lock()
# Guards only `_loader_thread`; held for microseconds, so the Qt thread may
# take it (see `start_loading`).
_loader_lock = threading.Lock()
_session: object | None = None
# When the last load failed (`_now()` seconds), or None. A failure is not
# forever: a scanner or a backup tool holding the file for a moment would
# otherwise switch the check off until the app restarts.
_load_failed_at: float | None = None
_loader_thread: threading.Thread | None = None
_now = time.monotonic


@dataclass(frozen=True)
class SpeechMeasurement:
    """What one scan found. Seconds count whole 32 ms windows."""

    max_probability: float
    # Windows at or above SILERO_SPEECH_PROBABILITY, in seconds.
    speech_seconds: float
    scanned_seconds: float
    total_seconds: float
    # False when the scan stopped early or ran out of budget: the rest of the
    # audio was never looked at, so a low `max_probability` proves nothing.
    complete: bool


@dataclass(frozen=True)
class SpeechCheck:
    """The answer to "does this audio hold no speech", from up to two scans.

    ``natural`` scans the audio as recorded. ``amplified`` scans a copy
    multiplied by the check's gain and clipped to full scale, and exists only
    when the natural scan was complete and stayed below the cut -- the one
    case in which it can change the answer. Silero scores by level as well as
    by shape: speech near the quietest level the Audio tab lets the silence
    gate admit scored as low as 0.025 as recorded and 0.246 or more amplified
    (`config.py`, SILERO_BATCH_QUIET_SPEECH_GAIN).
    """

    natural: SpeechMeasurement
    amplified: SpeechMeasurement | None
    min_probability: float

    @property
    def no_speech(self) -> bool:
        """True only when BOTH scans covered all the audio below the cut.

        A scan that stopped early or ran out of budget, an amplified scan that
        failed, and an amplified scan that reached the cut all answer False.
        """
        amplified = self.amplified
        return (
            self.natural.complete
            and self.natural.max_probability < self.min_probability
            and amplified is not None
            and amplified.complete
            and amplified.max_probability < self.min_probability
        )


def silero_asset_path() -> Path | None:
    """The graph faster-whisper ships, or None when it is not installed."""
    try:
        spec = importlib.util.find_spec("faster_whisper")
    except (ImportError, ValueError):
        return None
    if spec is None:
        return None
    locations = list(spec.submodule_search_locations or [])
    if not locations and spec.origin:
        locations = [str(Path(spec.origin).parent)]
    for location in locations:
        candidate = Path(location) / "assets" / _ASSET_NAME
        if candidate.is_file():
            return candidate
    return None


def _build_session():
    """Load the graph and check that it takes what this module feeds it.

    The options are faster-whisper's: one thread each way (the graph is tiny
    and runs beside a transcriber that wants the cores), no memory arena, and
    ONNX Runtime's own console logging off -- a windowed build has no console,
    and a failure is reported through this module's logger instead.
    """
    import onnxruntime

    path = silero_asset_path()
    if path is None:
        raise FileNotFoundError(
            f"{_ASSET_NAME} not found in the faster_whisper package"
        )
    options = onnxruntime.SessionOptions()
    options.inter_op_num_threads = 1
    options.intra_op_num_threads = 1
    options.enable_cpu_mem_arena = False
    options.log_severity_level = 4
    session = onnxruntime.InferenceSession(
        str(path), providers=["CPUExecutionProvider"], sess_options=options
    )
    inputs = {item.name: list(item.shape) for item in session.get_inputs()}
    outputs = [item.name for item in session.get_outputs()]
    if (
        set(inputs) != set(_INPUTS)
        or inputs["input"][-1:] != [_CONTEXT_SAMPLES + _WINDOW_SAMPLES]
        or inputs["h"] != list(_STATE_SHAPE)
        or inputs["c"] != list(_STATE_SHAPE)
        or outputs != list(_OUTPUTS)
    ):
        raise ValueError(
            f"{path.name} has another contract (inputs {inputs}, outputs {outputs})"
        )
    return session


def _in_backoff() -> bool:
    failed_at = _load_failed_at
    return failed_at is not None and _now() - failed_at < SILERO_LOAD_RETRY_S


def _get_session():
    """The shared session, built on first use; None when it cannot be built.

    This builds on the calling thread (123-275 ms cold, most of it importing
    ONNX Runtime), so the Qt thread never calls it: it asks
    `loaded_session` and `start_loading` instead. A failed load is
    remembered for SILERO_LOAD_RETRY_S: every post-pause streaming partial
    asks, and retrying there would pay the failure -- and log it -- every
    350 ms. ONNX Runtime allows concurrent `run` calls on one session, so the
    stream worker and the Qt thread share it; only the build is serialized.
    """
    global _session, _load_failed_at
    session = _session
    if session is not None or _in_backoff():
        return session
    with _session_lock:
        if _session is None and not _in_backoff():
            try:
                _session = _build_session()
                _load_failed_at = None
            except Exception as exc:
                _load_failed_at = _now()
                logger.warning(
                    "silero_vad_unavailable: the speech check is off for %.0f s "
                    "and the energy gates decide alone. error=%s",
                    SILERO_LOAD_RETRY_S,
                    exc,
                )
        return _session


def load_failed_recently() -> bool:
    """Whether the last load failed less than SILERO_LOAD_RETRY_S ago."""
    return _in_backoff()


def loaded_session():
    """The session if it is already built, else None; never builds or waits."""
    return _session


def start_loading() -> None:
    """Build the session on a daemon thread, unless it is built, being built
    or inside the retry backoff. Idempotent and never waits for a build: the
    controller calls it on the Qt thread at start, on every settings reload
    with the silence gate on, and from a stop that found no session yet.

    It takes `_loader_lock`, never `_session_lock`: the loader holds
    `_session_lock` for the whole build, and a stop that waited on it would
    freeze the Qt thread for as long as the build takes.
    """
    global _loader_thread
    if _session is not None or _in_backoff():
        return
    with _loader_lock:
        loader = _loader_thread
        if loader is not None and loader.is_alive():
            return
        # Asked again under the lock: a loader that finished between the
        # check above and here needs no successor.
        if _session is not None or _in_backoff():
            return
        # `_get_session` is looked up when the thread runs, so a test that
        # replaces it is honoured.
        loader = threading.Thread(
            target=lambda: _get_session(), name="stt_app_silero_load", daemon=True
        )
        try:
            # Started under the lock, so a second caller cannot see the new
            # thread as not yet alive and start another.
            loader.start()
        except RuntimeError as exc:
            # No thread available: the check stays off for this stop and the
            # next stop asks again.
            logger.warning("silero_vad_load_thread_not_started error=%s", exc)
            return
        _loader_thread = loader


def reset_silero_for_tests() -> None:
    """Forget the cached session and any remembered load failure."""
    global _session, _load_failed_at
    with _session_lock:
        _session = None
        _load_failed_at = None


def _windows(samples: np.ndarray) -> np.ndarray:
    """``samples`` zero-padded to whole windows, as ``[windows, 512]``."""
    padded = np.pad(samples, (0, -samples.size % _WINDOW_SAMPLES))
    return padded.reshape(-1, _WINDOW_SAMPLES)


def _run(session, frames: np.ndarray, state, tail: np.ndarray):
    """Score ``frames``; returns (probabilities, state, tail for the next call).

    ``tail`` is the last 64 samples before ``frames`` -- the context of the
    first window -- and the returned tail is the same for the next block. New
    arrays throughout: faster-whisper's runner writes the context through a
    view into the caller's audio, which this module must not copy.
    """
    context = np.empty((frames.shape[0], _CONTEXT_SAMPLES), dtype=np.float32)
    context[0] = tail
    context[1:] = frames[:-1, -_CONTEXT_SAMPLES:]
    batch = np.concatenate([context, frames], axis=1)
    h, c = state
    probabilities, h, c = session.run(list(_OUTPUTS), {"input": batch, "h": h, "c": c})
    return (
        np.asarray(probabilities, dtype=np.float32).reshape(-1),
        (h, c),
        frames[-1, -_CONTEXT_SAMPLES:].copy(),
    )


def _initial_state():
    return (
        np.zeros(_STATE_SHAPE, dtype=np.float32),
        np.zeros(_STATE_SHAPE, dtype=np.float32),
        np.zeros(_CONTEXT_SAMPLES, dtype=np.float32),
    )


def measure_speech_pcm16(
    pcm: bytes,
    sample_rate: int,
    *,
    stop_after_speech_s: float | None = None,
    max_scan_s: float | None = None,
    gain: float = 1.0,
) -> SpeechMeasurement | None:
    """Scan 16-bit mono PCM for speech, block by block.

    ``stop_after_speech_s`` ends the scan once that much speech has been
    found: the answer to "is there speech" is then settled, and the rest of
    a long recording costs nothing. ``max_scan_s`` bounds the audio scanned
    from the start, rounded up to a whole window. Either leaves ``complete``
    False when audio was left unscanned. None for any rate but
    16 kHz (the graph would score it as something it is not), for fewer than
    one sample, and whenever the detector is unavailable or a run fails.
    ``gain`` multiplies the samples before scoring, clipped to full scale.
    """
    if sample_rate != SAMPLE_RATE_HZ or len(pcm) < 2:
        return None
    session = _get_session()
    if session is None:
        return None
    total = len(pcm) // 2
    limit = total
    if max_scan_s is not None:
        # Rounded up to whole windows of real audio: a budget ending inside a
        # window would otherwise zero-pad that window's tail, and a scan that
        # reached the end that way would call audio it never saw scanned.
        wanted = max(1, round(max_scan_s * SAMPLE_RATE_HZ))
        limit = min(total, -(-wanted // _WINDOW_SAMPLES) * _WINDOW_SAMPLES)
    # Only the part the budget allows is converted: for ten minutes of audio
    # the conversion alone cost more than the bounded scan it feeds.
    samples = np.frombuffer(pcm, dtype="<i2", count=limit).astype(np.float32) / 32768.0
    if gain != 1.0:
        samples = np.clip(samples * np.float32(gain), -1.0, 1.0)
    frames = _windows(samples)
    h, c, tail = _initial_state()
    state = (h, c)
    max_probability = 0.0
    speech_windows = 0
    scanned_windows = 0
    stop_windows = (
        None
        if stop_after_speech_s is None
        else int(np.ceil(stop_after_speech_s / WINDOW_SECONDS))
    )
    try:
        for first in range(0, frames.shape[0], BLOCK_WINDOWS):
            block = frames[first:first + BLOCK_WINDOWS]
            probabilities, state, tail = _run(session, block, state, tail)
            scanned_windows += block.shape[0]
            max_probability = max(max_probability, float(probabilities.max()))
            speech_windows += int(
                np.count_nonzero(probabilities >= SILERO_SPEECH_PROBABILITY)
            )
            if stop_windows is not None and speech_windows >= stop_windows:
                break
    except Exception as exc:
        logger.warning("silero_vad_run_failed error=%s", exc)
        return None
    scanned = min(scanned_windows * _WINDOW_SAMPLES, total)
    return SpeechMeasurement(
        max_probability=max_probability,
        speech_seconds=speech_windows * WINDOW_SECONDS,
        scanned_seconds=scanned / SAMPLE_RATE_HZ,
        total_seconds=total / SAMPLE_RATE_HZ,
        complete=scanned >= total,
    )


def _read_pcm16_mono(wav_bytes: bytes) -> tuple[bytes, int] | None:
    """A WAV file's PCM and rate; None unless it is 16-bit mono and readable."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            if wav_file.getnchannels() != 1 or wav_file.getsampwidth() != 2:
                return None
            rate = wav_file.getframerate()
            pcm = wav_file.readframes(wav_file.getnframes())
    except (wave.Error, EOFError, OSError, ValueError, struct.error):
        return None
    return pcm, rate


def check_speech_pcm16(
    pcm: bytes,
    sample_rate: int,
    *,
    min_probability: float = SILERO_BATCH_MIN_PROBABILITY,
    gain: float = SILERO_BATCH_QUIET_SPEECH_GAIN,
    stop_after_speech_s: float | None = None,
    max_scan_s: float | None = None,
) -> SpeechCheck | None:
    """Whether 16 kHz 16-bit mono PCM holds no speech; None when unmeasured.

    The natural scan runs first; the amplified one only when the natural scan
    covered all the audio and stayed below ``min_probability``. Read the
    result through `SpeechCheck.no_speech`, and None as "do not gate": the
    detector is unavailable, the rate is not 16 kHz, or the scan failed.
    The two scans share ``stop_after_speech_s`` and ``max_scan_s``, so the
    worst case costs two bounded scans.
    """
    natural = measure_speech_pcm16(
        pcm,
        sample_rate,
        stop_after_speech_s=stop_after_speech_s,
        max_scan_s=max_scan_s,
    )
    if natural is None:
        return None
    amplified = None
    if natural.complete and natural.max_probability < min_probability:
        amplified = measure_speech_pcm16(
            pcm,
            sample_rate,
            stop_after_speech_s=stop_after_speech_s,
            max_scan_s=max_scan_s,
            gain=gain,
        )
    return SpeechCheck(
        natural=natural, amplified=amplified, min_probability=min_probability
    )


def check_speech_wav(
    wav_bytes: bytes,
    *,
    min_probability: float = SILERO_BATCH_MIN_PROBABILITY,
    gain: float = SILERO_BATCH_QUIET_SPEECH_GAIN,
    stop_after_speech_s: float | None = None,
    max_scan_s: float | None = None,
) -> SpeechCheck | None:
    """`check_speech_pcm16` for a WAV file's bytes; None unless 16-bit mono."""
    read = _read_pcm16_mono(wav_bytes)
    if read is None:
        return None
    pcm, rate = read
    return check_speech_pcm16(
        pcm,
        rate,
        min_probability=min_probability,
        gain=gain,
        stop_after_speech_s=stop_after_speech_s,
        max_scan_s=max_scan_s,
    )
