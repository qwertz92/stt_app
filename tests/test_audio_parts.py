"""A long recording goes to a remote batch provider in parts.

OpenAI, Groq and Azure LLM Speech take the whole recording in one request and
refuse it past their limit -- or, for OpenAI's two 2,000-token models, return
a transcript cut short that reads like a complete one. These tests drive the
shared splitter and the loop the three providers run over its parts; the
provider files test that each one is wired to them.
"""

from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from stt_app.config import (
    AUDIO_SAMPLE_RATE,
    DEFAULT_SILENCE_GATE_THRESHOLD,
    REMOTE_BATCH_MAX_PART_SECONDS,
    REMOTE_BATCH_MAX_REQUEST_BYTES,
    REMOTE_BATCH_MODEL_MAX_PART_SECONDS,
    RemotePartLimit,
    remote_batch_part_limit,
)
from stt_app.transcriber._audio_parts import (
    max_part_frames,
    split_for_request,
    transcribe_in_parts,
)
from stt_app.transcriber._pcm_audio import pcm16_wav_bytes, split_into_passes
from stt_app.transcriber.base import (
    TranscriptionCanceled,
    TranscriptionError,
    gap_marker,
    transcript_has_gap,
)


def _wav_bytes(
    samples: np.ndarray,
    sample_rate: int = AUDIO_SAMPLE_RATE,
    *,
    sample_width: int = 2,
) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(sample_width)
        handle.setframerate(sample_rate)
        if sample_width == 2:
            handle.writeframes(samples.astype("<i2").tobytes())
        else:
            handle.writeframes(b"\x00" * sample_width * samples.size)
    return buffer.getvalue()


def _read_wav(data: bytes) -> tuple[bytes, int, int, int]:
    """`(frames, sample_rate, channels, sample_width)` of a WAV part."""
    with wave.open(io.BytesIO(data), "rb") as handle:
        return (
            handle.readframes(handle.getnframes()),
            handle.getframerate(),
            handle.getnchannels(),
            handle.getsampwidth(),
        )


def _noise(seconds: float, sample_rate: int = AUDIO_SAMPLE_RATE) -> np.ndarray:
    """Loud enough everywhere that only an inserted gap is the quietest spot.

    The whole int16 range, extremes included: a part's samples go through
    float32 and back, and a scale that is off by one step only shows on
    samples of magnitude 16384 and above.
    """
    rng = np.random.default_rng(7)
    count = int(seconds * sample_rate)
    samples = rng.integers(-32768, 32768, count, dtype=np.int16)
    samples[:2] = (-32768, 32767)
    return samples


def _stereo_wav_bytes(left: np.ndarray, right: np.ndarray, sample_rate: int) -> bytes:
    interleaved = np.empty(left.size * 2, dtype="<i2")
    interleaved[0::2] = left
    interleaved[1::2] = right
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(interleaved.tobytes())
    return buffer.getvalue()


def _silence(samples: np.ndarray, start_s: float, stop_s: float) -> slice:
    gap = slice(int(start_s * AUDIO_SAMPLE_RATE), int(stop_s * AUDIO_SAMPLE_RATE))
    samples[gap] = 0
    return gap


# The bound the tests split at. At a bound this short the cut is looked for in
# the second half of each window (the search is at most half the bound), so
# every part but the last is 10-20 s long here; the real bounds are 300 s and
# more. The byte cap is the real one, which a 16 kHz part of 20 s is far from.
_BOUND = 20.0
_LIMIT = RemotePartLimit(seconds=_BOUND, max_bytes=25_000_000)
# A cap a short test signal can cross: at 16 kHz 30 s (960,044 bytes) are the
# tighter bound, at 44.1 and 48 kHz the 2,000,000 bytes are.
_SMALL_CAP = RemotePartLimit(seconds=30.0, max_bytes=2_000_000)


def _three_part_recording() -> tuple[np.ndarray, slice, slice]:
    """50 s of noise with a pause where each of the two cuts has to land."""
    samples = _noise(50.0)
    return samples, _silence(samples, 16.0, 16.4), _silence(samples, 31.0, 31.4)


# --------------------------------------------------------------------------
# The split
# --------------------------------------------------------------------------


def test_a_recording_within_the_bound_is_the_very_object_it_was_given(tmp_path):
    """What goes out for a recording at or below the bound must be exactly
    today's request, so nothing is decoded or re-encoded on that road."""
    at_the_bound = _wav_bytes(_noise(_BOUND))
    path = tmp_path / "recording.wav"
    path.write_bytes(at_the_bound)

    for source in (at_the_bound, path, str(path)):
        parts = split_for_request(source, _LIMIT)
        assert len(parts) == 1
        assert parts[0] is source


def test_a_long_recording_is_cut_at_quiet_points_into_parts_within_the_bound():
    samples, first_gap, second_gap = _three_part_recording()
    source = _wav_bytes(samples)

    parts = split_for_request(source, _LIMIT)

    assert len(parts) == 3
    decoded = [_read_wav(part) for part in parts]
    # Every part is a WAV of the recording's own format.
    assert {(rate, channels, width) for _, rate, channels, width in decoded} == {
        (AUDIO_SAMPLE_RATE, 1, 2)
    }
    frame_counts = [len(frames) // 2 for frames, *_ in decoded]
    assert all(count <= _BOUND * AUDIO_SAMPLE_RATE for count in frame_counts)
    # Nothing dropped, nothing doubled: the parts are the recording.
    assert b"".join(frames for frames, *_ in decoded) == samples.tobytes()
    # The parts share no audio, so a cut inside a word would lose it.
    assert first_gap.start <= frame_counts[0] <= first_gap.stop
    second_cut = frame_counts[0] + frame_counts[1]
    assert second_gap.start <= second_cut <= second_gap.stop


def test_a_long_recording_given_as_a_path_is_split_as_well(tmp_path):
    samples = _noise(25.0)
    path = tmp_path / "import.wav"
    path.write_bytes(_wav_bytes(samples))

    parts = split_for_request(path, _LIMIT)

    assert len(parts) == 2
    assert b"".join(_read_wav(part)[0] for part in parts) == samples.tobytes()


@pytest.mark.parametrize(
    "make_source",
    [
        # An imported MP3: the splitter cannot decode it, the provider can.
        lambda tmp_path: b"ID3\x04\x00" + b"\x00" * 400_000,
        # 24-bit PCM, which the shared WAV reader refuses.
        lambda tmp_path: _wav_bytes(np.zeros(25 * AUDIO_SAMPLE_RATE), sample_width=3),
        # A path that is not there: the provider's own missing-file message.
        lambda tmp_path: tmp_path / "missing.wav",
    ],
    ids=["mp3", "24-bit-wav", "missing-path"],
)
def test_audio_the_splitter_cannot_read_goes_out_as_it_came(tmp_path, make_source):
    source = make_source(tmp_path)

    parts = split_for_request(source, _LIMIT)

    assert len(parts) == 1
    assert parts[0] is source


def test_an_engine_without_a_bound_is_never_split():
    source = _wav_bytes(_noise(25.0))

    parts = split_for_request(source, None)

    assert len(parts) == 1
    assert parts[0] is source


# --------------------------------------------------------------------------
# The byte cap
# --------------------------------------------------------------------------
#
# A part is encoded as 16-bit mono at the file's own rate, so the seconds
# bound, derived from the app's 16 kHz, holds three times the bytes at 48 kHz:
# ten minutes of a 48 kHz import is 57.6 MB against OpenAI's 25 MB.


@pytest.mark.parametrize(
    ("sample_rate", "expected_frames"),
    [
        # 30 s is 960,044 bytes here: the seconds bound is the tighter one.
        (16_000, 30 * 16_000),
        # The frames whose WAV -- 44-byte header plus two bytes a frame -- fits
        # 2,000,000 bytes: (2,000,000 - 44) // 2.
        (44_100, 999_978),
        (48_000, 999_978),
    ],
)
def test_a_part_carries_the_fewer_frames_of_the_seconds_and_the_byte_cap(
    sample_rate, expected_frames
):
    frames = max_part_frames(_SMALL_CAP, sample_rate)

    assert frames == expected_frames
    # Measured on the encoder the parts go through, not on the arithmetic
    # above: the part fits, and one frame more would not whenever the byte
    # cap is the bound.
    assert len(pcm16_wav_bytes(b"\x00\x00" * frames, sample_rate)) <= 2_000_000
    if sample_rate != 16_000:
        one_more = pcm16_wav_bytes(b"\x00\x00" * (frames + 1), sample_rate)
        assert len(one_more) > 2_000_000


@pytest.mark.parametrize("sample_rate", [44_100, 48_000])
def test_an_import_past_the_byte_cap_goes_out_in_parts_under_it(sample_rate):
    """50 s at 44.1 or 48 kHz is 4.4-4.8 MB. The pause at 25 s is where the
    30 s bound alone would cut -- a first part of 2.2-2.4 MB against the 2 MB
    cap, which fits 22.7 s at 44.1 kHz and 20.8 s at 48 kHz. Without it the
    cuts fall wherever the noise happens to be quietest, and on this noise a
    split by the seconds alone passed at 44.1 kHz by chance."""
    samples = _noise(50.0, sample_rate)
    samples[int(25.0 * sample_rate) : int(25.4 * sample_rate)] = 0
    source = _wav_bytes(samples, sample_rate)

    parts = split_for_request(source, _SMALL_CAP)

    assert len(parts) >= 3
    assert all(len(part) <= _SMALL_CAP.max_bytes for part in parts)
    decoded = [_read_wav(part) for part in parts]
    assert {(rate, channels, width) for _, rate, channels, width in decoded} == {
        (sample_rate, 1, 2)
    }
    assert b"".join(frames for frames, *_ in decoded) == samples.tobytes()


def test_an_import_within_the_seconds_bound_but_over_the_cap_is_split():
    """25 s is within the 30 s bound; at 48 kHz it is 2.4 MB."""
    samples = _noise(25.0, 48_000)
    source = _wav_bytes(samples, 48_000)

    parts = split_for_request(source, _SMALL_CAP)

    assert len(parts) == 2
    assert all(len(part) <= _SMALL_CAP.max_bytes for part in parts)
    assert b"".join(_read_wav(part)[0] for part in parts) == samples.tobytes()


def test_an_import_within_both_bounds_is_the_very_object_it_was_given():
    """20 s at 48 kHz is 1,920,044 bytes: under the cap and the bound."""
    source = _wav_bytes(_noise(20.0, 48_000), 48_000)

    parts = split_for_request(source, _SMALL_CAP)

    assert len(parts) == 1
    assert parts[0] is source


def test_a_stereo_import_over_the_cap_goes_out_as_its_mono_mix():
    """20 s of 16 kHz stereo is 1.28 MB against a 1 MB cap, but its mono mix is
    640,044 bytes: one part, and not the file that is over the cap."""
    channel = _noise(20.0)
    source = _stereo_wav_bytes(channel, channel, AUDIO_SAMPLE_RATE)
    limit = RemotePartLimit(seconds=30.0, max_bytes=1_000_000)

    parts = split_for_request(source, limit)

    assert len(parts) == 1
    assert len(parts[0]) <= limit.max_bytes
    frames, rate, channels, width = _read_wav(parts[0])
    assert (rate, channels, width) == (AUDIO_SAMPLE_RATE, 1, 2)
    assert frames == channel.tobytes()


def test_a_16_khz_recording_keeps_the_parts_the_seconds_bound_gives():
    """The byte cap must not move a single cut of what the app records: its
    16 kHz WAV reaches every engine's seconds bound first (next test)."""
    limit = remote_batch_part_limit("openai", "gpt-4o-mini-transcribe")
    assert limit is not None
    samples = _noise(limit.seconds + 1.0)

    parts = split_for_request(_wav_bytes(samples), limit)

    waveform = samples.astype(np.float32) / 32768.0
    seconds_only = split_into_passes(
        waveform, AUDIO_SAMPLE_RATE, max_seconds=limit.seconds
    )
    assert [len(_read_wav(part)[0]) // 2 for part in parts] == [
        window.size for window in seconds_only
    ]


@pytest.mark.parametrize(
    ("engine", "model"),
    [(engine, "") for engine in REMOTE_BATCH_MAX_PART_SECONDS]
    + list(REMOTE_BATCH_MODEL_MAX_PART_SECONDS),
)
def test_at_16_khz_the_seconds_bound_is_always_the_tighter_one(engine, model):
    limit = remote_batch_part_limit(engine, model)

    assert limit is not None
    assert max_part_frames(limit, AUDIO_SAMPLE_RATE) == int(
        limit.seconds * AUDIO_SAMPLE_RATE
    )


def test_every_engine_with_a_seconds_bound_has_a_byte_cap():
    assert set(REMOTE_BATCH_MAX_REQUEST_BYTES) == set(REMOTE_BATCH_MAX_PART_SECONDS)
    assert {engine for engine, _ in REMOTE_BATCH_MODEL_MAX_PART_SECONDS} <= set(
        REMOTE_BATCH_MAX_PART_SECONDS
    )


# --------------------------------------------------------------------------
# The loop over the parts
# --------------------------------------------------------------------------

_UPLOAD = "Uploading audio to Example and waiting for transcription..."


class _Requests:
    """A provider's single-request code: records what it was handed."""

    def __init__(self, answers: list[str | Exception]) -> None:
        self._answers = answers
        self.sources: list[object] = []
        self.progress: list[str] = []

    def __call__(self, source, progress_text: str) -> str:
        self.sources.append(source)
        self.progress.append(progress_text)
        answer = self._answers[len(self.sources) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer


def _never_canceled() -> None:
    return None


def _three_parts() -> bytes:
    return _wav_bytes(_three_part_recording()[0])


def test_the_parts_are_sent_in_order_and_their_texts_joined():
    requests = _Requests(["erster teil", "zweiter teil", "dritter teil"])

    text = transcribe_in_parts(
        _three_parts(),
        requests,
        limit=_LIMIT,
        progress_text=_UPLOAD,
        raise_if_canceled=_never_canceled,
        silence_threshold=DEFAULT_SILENCE_GATE_THRESHOLD,
    )

    assert text == "erster teil zweiter teil dritter teil"
    assert len(requests.sources) == 3
    assert requests.progress == [
        f"Transcribing part {index} of 3. {_UPLOAD}" for index in (1, 2, 3)
    ]


def _answer_by_level(source, progress_text: str) -> str:
    """A service that hears nothing in a silent part and words in a loud one."""
    frames = _read_wav(source)[0]
    samples = np.frombuffer(frames, dtype="<i2")
    return "" if not np.any(samples) else f"teil {progress_text.split()[2]}"


def test_a_silent_part_that_comes_back_empty_is_skipped():
    """A long pause in an imported meeting is a part with nothing to say; its
    empty text leaves no double space and fails nothing."""
    samples = _noise(50.0)
    _silence(samples, 16.0, 36.0)
    parts = split_for_request(_wav_bytes(samples), _LIMIT)
    silent = [
        index
        for index, part in enumerate(parts, start=1)
        if not np.any(np.frombuffer(_read_wav(part)[0], dtype="<i2"))
    ]
    assert silent, "the fixture must produce a part that is all silence"

    text = transcribe_in_parts(
        _wav_bytes(samples),
        _answer_by_level,
        limit=_LIMIT,
        progress_text=_UPLOAD,
        raise_if_canceled=_never_canceled,
        silence_threshold=DEFAULT_SILENCE_GATE_THRESHOLD,
    )

    expected = [f"teil {index}" for index in range(1, len(parts) + 1)]
    expected = [text for index, text in enumerate(expected, 1) if index not in silent]
    assert text == " ".join(expected)


def test_a_part_with_sound_that_comes_back_empty_leaves_a_marker_and_the_rest_goes_on(
    caplog,
):
    """One part a provider answered with nothing must not cost the whole
    recording: the other parts are minutes of speech each. The gap is marked
    where it sits, with its place in the recording, so the transcript does not
    read like a complete one and the user knows which stretch to listen to."""
    requests = _Requests(["erster teil", "", "dritter teil"])

    with caplog.at_level("WARNING", logger="stt_app.transcriber._audio_parts"):
        text = transcribe_in_parts(
            _three_parts(),
            requests,
            limit=_LIMIT,
            progress_text=_UPLOAD,
            raise_if_canceled=_never_canceled,
            silence_threshold=DEFAULT_SILENCE_GATE_THRESHOLD,
        )

    assert text == "erster teil [no text returned for 0:16-0:31] dritter teil"
    assert len(requests.sources) == 3
    assert "remote_audio_part_empty index=2 count=3" in caplog.text


def test_a_recording_whose_parts_all_come_back_empty_with_sound_fails():
    """Nothing at all from a recording that holds sound is what a single
    request returning nothing is (docs/agents/controller-and-jobs.md, "Empty
    model text is a failure"): an Error with Retry, not a transcript made of
    markers."""
    requests = _Requests(["", "", ""])

    with pytest.raises(TranscriptionError) as excinfo:
        transcribe_in_parts(
            _three_parts(),
            requests,
            limit=_LIMIT,
            progress_text=_UPLOAD,
            raise_if_canceled=_never_canceled,
            silence_threshold=DEFAULT_SILENCE_GATE_THRESHOLD,
        )

    assert "No part of the 3 returned text" in str(excinfo.value)
    assert len(requests.sources) == 3


def test_a_recording_whose_parts_are_all_silent_comes_back_empty():
    samples = np.zeros(int(50.0 * AUDIO_SAMPLE_RATE), dtype=np.int16)
    requests = _Requests(["", "", "", "", ""])

    text = transcribe_in_parts(
        _wav_bytes(samples),
        requests,
        limit=_LIMIT,
        progress_text=_UPLOAD,
        raise_if_canceled=_never_canceled,
        silence_threshold=DEFAULT_SILENCE_GATE_THRESHOLD,
    )

    assert text == ""
    assert len(requests.sources) > 1


def _quiet_middle_recording() -> bytes:
    """Three parts; the middle one is room tone at about 0.002 RMS -- above a
    threshold of 0.001, below the default 0.004."""
    samples, first_gap, second_gap = _three_part_recording()
    rng = np.random.default_rng(11)
    middle = slice(first_gap.stop, second_gap.start)
    count = middle.stop - middle.start
    samples[middle] = rng.integers(-110, 111, count, dtype=np.int16)
    return _wav_bytes(samples)


@pytest.mark.parametrize(
    ("threshold", "expected"),
    [
        (0.004, "erster teil dritter teil"),
        (0.001, "erster teil [no text returned for 0:16-0:31] dritter teil"),
    ],
)
def test_the_configured_threshold_decides_whether_an_empty_part_held_sound(
    threshold, expected
):
    """The silence gate's threshold as the user set it, not its default: a
    user who raised it for a noisy room calls that room tone silence."""
    requests = _Requests(["erster teil", "", "dritter teil"])

    text = transcribe_in_parts(
        _quiet_middle_recording(),
        requests,
        limit=_LIMIT,
        progress_text=_UPLOAD,
        raise_if_canceled=_never_canceled,
        silence_threshold=threshold,
    )

    assert text == expected


def test_one_part_is_todays_request_its_message_and_its_error():
    source = _wav_bytes(_noise(2.0))
    failure = TranscriptionError("Example: Rate limit exceeded (HTTP 429).")
    requests = _Requests([failure])

    with pytest.raises(TranscriptionError) as excinfo:
        transcribe_in_parts(
            source,
            requests,
            limit=_LIMIT,
            progress_text=_UPLOAD,
            raise_if_canceled=_never_canceled,
            silence_threshold=DEFAULT_SILENCE_GATE_THRESHOLD,
        )

    assert excinfo.value is failure
    assert len(requests.sources) == 1
    assert requests.sources[0] is source
    assert requests.progress == [_UPLOAD]


def test_a_failed_part_names_itself_and_carries_the_text_before_it():
    """Returning the parts that worked would hand back a transcript that reads
    like a complete one; the error carries them instead."""
    failure = TranscriptionError("Example: Rate limit exceeded (HTTP 429).")
    requests = _Requests(["erster teil", failure, "never requested"])

    with pytest.raises(TranscriptionError) as excinfo:
        transcribe_in_parts(
            _three_parts(),
            requests,
            limit=_LIMIT,
            progress_text=_UPLOAD,
            raise_if_canceled=_never_canceled,
            silence_threshold=DEFAULT_SILENCE_GATE_THRESHOLD,
        )

    message = str(excinfo.value)
    assert "part 2 of 3" in message
    assert "Example: Rate limit exceeded (HTTP 429)." in message
    assert message.endswith(' Received before the failure: "erster teil"')
    assert excinfo.value.__cause__ is failure
    assert len(requests.sources) == 2


def test_a_first_part_that_fails_claims_no_earlier_text():
    requests = _Requests([TranscriptionError("Example failed."), "unused", "unused"])

    with pytest.raises(TranscriptionError) as excinfo:
        transcribe_in_parts(
            _three_parts(),
            requests,
            limit=_LIMIT,
            progress_text=_UPLOAD,
            raise_if_canceled=_never_canceled,
            silence_threshold=DEFAULT_SILENCE_GATE_THRESHOLD,
        )

    assert "part 1 of 3" in str(excinfo.value)
    assert "Received before the failure" not in str(excinfo.value)


def test_a_cancel_between_parts_sends_no_further_request():
    requests = _Requests(["erster teil", "zweiter teil", "dritter teil"])

    def _cancel_once_a_part_is_back() -> None:
        if requests.sources:
            raise TranscriptionCanceled()

    with pytest.raises(TranscriptionCanceled):
        transcribe_in_parts(
            _three_parts(),
            requests,
            limit=_LIMIT,
            progress_text=_UPLOAD,
            raise_if_canceled=_cancel_once_a_part_is_back,
            silence_threshold=DEFAULT_SILENCE_GATE_THRESHOLD,
        )

    assert len(requests.sources) == 1


def test_a_cancel_during_the_split_sends_nothing():
    """Splitting a large import takes a second; a cancel pressed meanwhile
    must not still upload (and pay for) the first part."""
    requests = _Requests(["erster teil", "zweiter teil", "dritter teil"])

    def _canceled() -> None:
        raise TranscriptionCanceled()

    with pytest.raises(TranscriptionCanceled):
        transcribe_in_parts(
            _three_parts(),
            requests,
            limit=_LIMIT,
            progress_text=_UPLOAD,
            raise_if_canceled=_canceled,
            silence_threshold=DEFAULT_SILENCE_GATE_THRESHOLD,
        )

    assert requests.sources == []


def test_a_cancel_during_the_split_into_one_part_sends_nothing():
    """A stereo import over the cap is decoded and re-encoded as one mono
    part, which takes as long as a split into several; the single-part road
    skipped the cancel check the loop makes."""
    channel = _noise(20.0)
    source = _stereo_wav_bytes(channel, channel, AUDIO_SAMPLE_RATE)
    requests = _Requests(["ganzer text"])

    def _canceled() -> None:
        raise TranscriptionCanceled()

    with pytest.raises(TranscriptionCanceled):
        transcribe_in_parts(
            source,
            requests,
            limit=RemotePartLimit(seconds=30.0, max_bytes=1_000_000),
            progress_text=_UPLOAD,
            raise_if_canceled=_canceled,
            silence_threshold=DEFAULT_SILENCE_GATE_THRESHOLD,
        )

    assert requests.sources == []


def test_a_recording_exactly_at_the_byte_cap_is_the_very_object_it_was_given():
    source = _wav_bytes(_noise(5.0))
    at_cap = RemotePartLimit(seconds=30.0, max_bytes=len(source))
    over_cap = RemotePartLimit(seconds=30.0, max_bytes=len(source) - 1)

    assert split_for_request(source, at_cap)[0] is source
    assert split_for_request(source, over_cap)[0] is not source


def test_a_file_that_is_not_a_wav_and_over_the_cap_is_logged(caplog):
    """It is sent as it came (the provider answers for it), but a refusal that
    follows has to be explainable from the log."""
    mp3_like = b"ID3" + b"\x00" * 2_000
    limit = RemotePartLimit(seconds=30.0, max_bytes=1_000)

    with caplog.at_level("WARNING", logger="stt_app.transcriber._audio_parts"):
        parts = split_for_request(mp3_like, limit)

    assert parts[0] is mp3_like
    assert "remote_audio_not_split" in caplog.text
    assert "not a WAV" in caplog.text


# --------------------------------------------------------------------------
# The bounds
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("engine", "model", "size_limit_bytes", "duration_limit_s"),
    [
        # 25 MB per file, read as the smaller decimal megabytes; 1,400 s is the
        # duration cap reported second-hand.
        ("openai", "gpt-transcribe", 25_000_000, 1400),
        ("openai", "whisper-1", 25_000_000, 1400),
        ("openai", "gpt-4o-transcribe", 25_000_000, 1400),
        ("openai", "gpt-4o-mini-transcribe", 25_000_000, 1400),
        # Groq's free tier; the app cannot tell a key's tier.
        ("groq", "whisper-large-v3-turbo", 25_000_000, None),
        ("groq", "whisper-large-v3", 25_000_000, None),
        # The REST reference's "shorter than 2 hours ... smaller than 250 MB".
        ("azure", "mai-transcribe-2", 250_000_000, 7200),
    ],
)
def test_a_part_at_the_bound_stays_under_the_documented_limit(
    engine, model, size_limit_bytes, duration_limit_s
):
    limit = remote_batch_part_limit(engine, model)

    assert limit is not None
    assert limit.max_bytes == size_limit_bytes
    # A part's WAV at every rate an import plausibly has, measured on the
    # encoder the parts go through.
    for sample_rate in (8_000, 16_000, 22_050, 44_100, 48_000, 96_000):
        frames = max_part_frames(limit, sample_rate)
        header = len(pcm16_wav_bytes(b"", sample_rate))
        assert header + 2 * frames <= size_limit_bytes, sample_rate
    if duration_limit_s is not None:
        assert limit.seconds < duration_limit_s


@pytest.mark.parametrize(
    "engine", ["deepgram", "elevenlabs", "assemblyai", "funasr", "local"]
)
def test_engines_whose_limit_no_dictation_reaches_are_sent_whole(engine):
    """Deepgram 2 GB, ElevenLabs 3 GB / 10 h, AssemblyAI 2.2 GB / 10 h; Fun-ASR
    streams over a WebSocket, and the local runtimes split on their own."""
    assert remote_batch_part_limit(engine) is None


def test_adjacent_empty_parts_with_sound_share_one_marker():
    """Two gaps in a row are one stretch to listen to, not two."""
    requests = _Requests(["", "", "dritter teil"])

    text = transcribe_in_parts(
        _three_parts(),
        requests,
        limit=_LIMIT,
        progress_text=_UPLOAD,
        raise_if_canceled=_never_canceled,
        silence_threshold=DEFAULT_SILENCE_GATE_THRESHOLD,
    )

    assert text == "[no text returned for 0:00-0:31] dritter teil"
    assert transcript_has_gap(text)


def test_a_marker_never_names_a_stretch_of_no_length():
    """A tail a fraction of a second long -- a hotkey click after the last
    cut -- rounds to one second in both directions; the range must still read
    as a stretch, so the start rounds down and the end up."""
    assert gap_marker(180.2, 180.4) == "[no text returned for 3:00-3:01]"
    assert gap_marker(3599.6, 3661.0) == "[no text returned for 59:59-1:01:01]"


def test_a_failed_part_after_a_gap_carries_the_marker_in_its_recovered_text():
    requests = _Requests(["erster teil", "", TranscriptionError("Example: HTTP 500.")])

    with pytest.raises(TranscriptionError) as excinfo:
        transcribe_in_parts(
            _three_parts(),
            requests,
            limit=_LIMIT,
            progress_text=_UPLOAD,
            raise_if_canceled=_never_canceled,
            silence_threshold=DEFAULT_SILENCE_GATE_THRESHOLD,
        )

    assert "erster teil [no text returned for 0:16-0:31]" in str(excinfo.value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("erster teil [no text returned for 0:16-0:31] dritter teil", True),
        ("[no text returned for 59:59-1:01:01]", True),
        ("he said [no text returned] once", False),
        ("plain dictation", False),
    ],
)
def test_a_gap_is_recognised_by_the_exact_marker_only(text, expected):
    assert transcript_has_gap(text) is expected
