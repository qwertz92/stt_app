"""Send a long recording to a remote batch provider in parts.

OpenAI, Groq and Azure LLM Speech take the whole recording in one request, and
each refuses one past its limit -- or, for OpenAI's two token-capped models,
returns a transcript cut short that reads like a complete one. The limits, a
seconds bound and a byte cap per engine, and the documented figures they
derive from live in `config` (`remote_batch_part_limit`). A recording past
either is cut at quiet points by the splitter Granite CTC uses for its passes,
the parts go out one after another through the provider's own single-request
code, and the texts are joined.
"""

from __future__ import annotations

import io
import logging
import wave
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import numpy as np

from .. import silero_vad
from ..config import SILERO_BATCH_STOP_AFTER_SPEECH_S, RemotePartLimit
from ..vad import measure_peak_windowed_rms
from ._http_utils import recovered_text_suffix
from ._pcm_audio import pcm16_wav_bytes, split_into_passes
from .base import AudioInput, TranscriptionError, gap_marker
from .local_onnx_asr import _read_wav_float32

logger = logging.getLogger(__name__)

# What `pcm16_wav_bytes` writes for a part: a 44-byte header, then two bytes
# per frame (16-bit mono) at the input's own rate, whatever the input's own
# channel count and sample width.
_PART_WAV_HEADER_BYTES = 44
_PART_BYTES_PER_FRAME = 2


def max_part_frames(limit: RemotePartLimit, sample_rate: int) -> int:
    """The most frames one part at `sample_rate` carries: the seconds bound,
    or the frames whose WAV fits the byte cap, whichever is fewer.

    At the app's 16 kHz the seconds bound is the fewer for every engine (a
    test pins it), so a dictation is cut exactly where it was before the cap
    existed; an import at 44.1 or 48 kHz carries up to three times the bytes
    per second and reaches the cap first.
    """
    by_seconds = int(limit.seconds * sample_rate)
    by_bytes = (limit.max_bytes - _PART_WAV_HEADER_BYTES) // _PART_BYTES_PER_FRAME
    return min(by_seconds, by_bytes)


class _DeclaredWav(NamedTuple):
    frames: int
    sample_rate: int
    size_bytes: int


def _wav_source(audio_source: AudioInput) -> io.BytesIO | str:
    """What `wave.open` takes: a buffer, or a path as `str` (not `Path`)."""
    if isinstance(audio_source, (bytes, bytearray)):
        return io.BytesIO(bytes(audio_source))
    return str(Path(audio_source))


def _declared_wav(audio_source: AudioInput) -> _DeclaredWav | None:
    """What a WAV header declares, and the file's size, without decoding.

    A recording within the limit is the common case and must go out untouched,
    so it is recognised from the header alone. None for anything that is not a
    WAV this can open -- an imported MP3, a path that is not there -- which the
    provider is then sent as it always was, and answers for.
    """
    try:
        with wave.open(_wav_source(audio_source), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
        if isinstance(audio_source, (bytes, bytearray)):
            size = len(audio_source)
        else:
            size = Path(audio_source).stat().st_size
    except Exception:
        return None
    if rate <= 0:
        return None
    return _DeclaredWav(frames, rate, size)


def _wav_parts(audio_source: AudioInput, limit: RemotePartLimit) -> list[AudioInput]:
    """WAV parts within `limit`, cut at quiet points.

    Decoded by the WAV reader the local runtimes share, so it takes what they
    take: 16-bit PCM, averaged to mono. For a mono recording -- every one the
    app makes -- the parts' samples concatenate back to the input's exactly;
    a multi-channel import goes out as its mono mix. The float round trip is
    exact for 16-bit samples: `k / 32768` is representable in float32.
    """
    waveform, sample_rate = _read_wav_float32(_wav_source(audio_source))
    windows = split_into_passes(
        waveform, sample_rate, max_samples=max_part_frames(limit, sample_rate)
    )
    parts: list[AudioInput] = []
    for window in windows:
        # One float32 copy per part, rounded in place: a part can be an hour of
        # audio (Azure), and every extra temporary is 230 MB. No clip is
        # needed: the reader decodes 16-bit PCM only, so a sample is k / 32768
        # with k in [-32768, 32767] and a mono mix is a mean of such values,
        # which scales back inside the same range.
        scaled = window * 32768.0
        np.rint(scaled, out=scaled)
        parts.append(pcm16_wav_bytes(scaled.astype("<i2").tobytes(), sample_rate))
    return parts


def _size_bytes(audio_source: AudioInput) -> int | None:
    if isinstance(audio_source, (bytes, bytearray)):
        return len(audio_source)
    try:
        return Path(audio_source).stat().st_size
    except OSError:
        return None


def _holds_sound(part: AudioInput, threshold: float) -> bool:
    """Whether an empty part may hold speech the provider failed to return.

    First the level: a part whose loudest 100 ms window stays below
    `threshold`, the silence gate's threshold as the user set it, is silent.
    Then the Silero speech check the whole-recording gate uses
    (`silero_vad.check_speech_wav`, both scans, so quiet speech is not
    missed): a part it finds speech-free is a click or noise, not a stretch
    the provider skipped, and gets no marker. The scan runs on this worker
    thread over the whole part -- about 1.6 ms per second of speechless audio
    per scan, only for a part the provider answered with nothing.

    Unmeasurable counts as sound, at both steps: calling a part silent
    without having measured it is how a stretch of speech would be dropped
    unmarked.
    """
    if not isinstance(part, (bytes, bytearray)):
        return True
    level = measure_peak_windowed_rms(bytes(part))
    if level is not None and level < threshold:
        return False
    speech = silero_vad.check_speech_wav(
        bytes(part), stop_after_speech_s=SILERO_BATCH_STOP_AFTER_SPEECH_S
    )
    return speech is None or not speech.no_speech


def wav_seconds(audio: AudioInput) -> float:
    """A WAV's duration from its header; 0 for audio this cannot read (an MP3,
    a path that is not there). A part `_wav_parts` wrote always parses."""
    declared = _declared_wav(audio)
    if declared is None:
        return 0.0
    return declared.frames / declared.sample_rate


def split_for_request(
    audio_source: AudioInput,
    limit: RemotePartLimit | None,
) -> list[AudioInput]:
    """The recording as one request, or as WAV parts within `limit`.

    Returns `[audio_source]` -- the very object -- when there is no limit,
    when the recording is within both its seconds and its bytes, and when it
    cannot be split (not a WAV, a WAV the shared reader refuses such as 24-bit
    PCM): each of those goes out exactly as before this existed. A recording
    past either goes out as re-encoded parts, even when that is one part: a
    stereo file within the seconds but over the cap is sent as its mono mix,
    which fits, rather than as the file the provider would refuse.
    """
    if limit is None:
        return [audio_source]
    declared = _declared_wav(audio_source)
    if declared is None:
        size = _size_bytes(audio_source)
        if size is not None and size > limit.max_bytes:
            # An imported MP3 or M4A has no duration this can read and no
            # splitter here; the provider answers for it, most likely with a
            # refusal the log should be able to explain.
            logger.warning(
                "remote_audio_not_split bytes=%d max_bytes=%d reason=not a WAV "
                "this can read",
                size,
                limit.max_bytes,
            )
        return [audio_source]
    # The file as it is: its own duration and its own size. Whether its frames
    # would fit a part (`max_part_frames`) need not be asked here: for a 16-bit
    # WAV, the only kind the reader decodes, a size within the cap already
    # means they do.
    seconds = declared.frames / declared.sample_rate
    if seconds <= limit.seconds and declared.size_bytes <= limit.max_bytes:
        return [audio_source]
    try:
        parts = _wav_parts(audio_source, limit)
    except Exception as exc:
        # Past the limit but unreadable here (or too large to decode): sent
        # whole, as before, and most likely refused by the provider -- which
        # is why this is a warning.
        logger.warning(
            "remote_audio_not_split seconds=%.1f bytes=%d max_seconds=%.0f "
            "max_bytes=%d reason=%s",
            seconds,
            declared.size_bytes,
            limit.seconds,
            limit.max_bytes,
            exc,
        )
        return [audio_source]
    logger.info(
        "remote_audio_split parts=%d seconds=%.1f bytes=%d sample_rate=%d "
        "max_seconds=%.0f max_bytes=%d",
        len(parts),
        seconds,
        declared.size_bytes,
        declared.sample_rate,
        limit.seconds,
        limit.max_bytes,
    )
    return parts


def transcribe_in_parts(
    audio_source: AudioInput,
    transcribe_request: Callable[[AudioInput, str], str],
    *,
    limit: RemotePartLimit | None,
    progress_text: str,
    raise_if_canceled: Callable[[], None],
    silence_threshold: float,
) -> str:
    """Transcribe a recording through one request, or one request per part.

    `transcribe_request(audio, progress_text)` is the provider's own
    single-request code: it emits `progress_text` and raises
    `TranscriptionError` for a request that failed. The language and the
    custom vocabulary live on the provider, so every part carries them.

    A recording within `limit` is one call with the provider's own message and
    its own errors -- today's request, byte for byte. A longer one is one call
    per part, in order, with the cancel hook checked before every part (a
    request in flight runs to its end, as a single request always has, and a
    cancel discards the parts already transcribed); the texts are joined with
    one space. A part that fails raises a `TranscriptionError` naming it and
    carrying the text of the parts before it, never a partial transcript that
    reads like a complete one.

    A part that comes back empty is not a failure of the recording. A silent
    one -- its loudest window below `silence_threshold`, the silence gate's
    threshold as the user set it -- is skipped; one that holds sound leaves a
    marker naming its stretch of the recording (`[no text returned for
    3:00-6:00]`) and the parts after it are still sent, because failing would
    throw away every other part's minutes of speech, and skipping it silently
    would hand back a transcript with a hole that reads like a complete one.
    Only a recording of which no part returned text, while one held sound,
    fails -- the single request's rule ("Empty model text is a failure").
    """
    parts = split_for_request(audio_source, limit)
    if len(parts) == 1:
        if parts[0] is not audio_source:
            # Re-encoded (a stereo import over the byte cap goes out as its
            # mono mix): the split took its time, as before a part below.
            raise_if_canceled()
        return transcribe_request(parts[0], progress_text)
    count = len(parts)
    pieces: list[str] = []
    texts: list[str] = []
    empty_with_sound = 0
    # The stretch of consecutive empty parts with sound not yet written out:
    # adjacent gaps are one stretch to listen to, so they share one marker.
    gap_start: float | None = None
    start = 0.0

    def close_gap(end_s: float) -> None:
        nonlocal gap_start
        if gap_start is not None:
            pieces.append(gap_marker(gap_start, end_s))
            gap_start = None

    for index, part in enumerate(parts, start=1):
        # Before the first part as well: splitting a large import takes a
        # second, and a cancel pressed meanwhile must not upload a part.
        raise_if_canceled()
        end = start + wav_seconds(part)
        try:
            text = transcribe_request(
                part, f"Transcribing part {index} of {count}. {progress_text}"
            )
        except TranscriptionError as exc:
            close_gap(start)
            raise TranscriptionError(
                f"Transcribing part {index} of {count} failed: {exc}"
                f"{recovered_text_suffix(pieces, '')}"
            ) from exc
        if text:
            close_gap(start)
            texts.append(text)
            pieces.append(text)
        elif _holds_sound(part, silence_threshold):
            empty_with_sound += 1
            # The log carries the place, never text: there is none.
            logger.warning(
                "remote_audio_part_empty index=%d count=%d start_s=%.1f end_s=%.1f",
                index,
                count,
                start,
                end,
            )
            if gap_start is None:
                gap_start = start
        else:
            close_gap(start)
            logger.info("remote_audio_part_silent index=%d count=%d", index, count)
        start = end
    close_gap(start)
    if not texts and empty_with_sound:
        raise TranscriptionError(
            f"No part of the {count} returned text, although the recording holds sound."
        )
    return " ".join(pieces)
