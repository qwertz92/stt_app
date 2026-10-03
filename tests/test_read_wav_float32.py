"""`_read_wav_float32` decodes a whole import into one mono float32 waveform.

A long import is held in memory next to that waveform (a two-hour 48 kHz stereo
file is 1.4 GB on disk), so how many copies the reader makes decides whether
splitting it survives. The output must stay what the original whole-file
decode produced; `_reference_decode` below is that decode.
"""

from __future__ import annotations

import io
import tracemalloc
import wave

import numpy as np
import pytest

from stt_app.transcriber import local_onnx_asr
from stt_app.transcriber.local_onnx_asr import _read_wav_float32


def _reference_decode(frames: bytes, channels: int) -> np.ndarray:
    """The decode as it was before the block-wise reader: every sample as its
    own float32 array, then the channel mean."""
    if len(frames) % 2:
        frames = frames[:-1]
    waveform = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        usable = (waveform.size // channels) * channels
        waveform = waveform[:usable].reshape(-1, channels).mean(axis=1)
    return waveform


def _wav(frames: bytes, *, channels: int, rate: int = 16_000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames)
    return buffer.getvalue()


def _random_pcm(sample_count: int, seed: int = 7) -> bytes:
    rng = np.random.default_rng(seed)
    return rng.integers(-32768, 32768, sample_count, dtype="<i2").tobytes()


@pytest.mark.parametrize("channels", [1, 2, 3, 6])
@pytest.mark.parametrize("sample_rate", [8_000, 16_000, 44_100, 48_000])
def test_every_channel_count_and_rate_decodes_as_the_whole_file_decode_did(
    monkeypatch, channels, sample_rate
):
    # A block of 1000 frames, so a few thousand frames cross several block
    # boundaries and a boundary falling inside a frame would show.
    monkeypatch.setattr(local_onnx_asr, "_WAV_BLOCK_FRAMES", 1000)
    frames = _random_pcm(channels * 4321)
    payload = _wav(frames, channels=channels, rate=sample_rate)

    waveform, rate = _read_wav_float32(io.BytesIO(payload))

    expected = _reference_decode(frames, channels)
    assert rate == sample_rate
    assert waveform.dtype == np.float32
    assert waveform.tobytes() == expected.tobytes()


def test_a_path_decodes_the_same_as_the_bytes(tmp_path):
    frames = _random_pcm(2 * 5000)
    payload = _wav(frames, channels=2)
    path = tmp_path / "clip.wav"
    path.write_bytes(payload)

    from_path, _ = _read_wav_float32(path)
    from_bytes, _ = _read_wav_float32(io.BytesIO(payload))

    assert from_path.tobytes() == from_bytes.tobytes()
    assert from_path.tobytes() == _reference_decode(frames, 2).tobytes()


@pytest.mark.parametrize("channels", [1, 2, 3])
@pytest.mark.parametrize("cut", [0, 1, 2, 3, 5, 7])
def test_a_file_cut_inside_its_data_decodes_what_the_whole_file_decode_did(
    monkeypatch, channels, cut
):
    """A truncated recording: the header still declares every frame, the data
    ends mid-frame or mid-sample. The old decode dropped a trailing odd byte
    and then a trailing incomplete frame, and so does this."""
    monkeypatch.setattr(local_onnx_asr, "_WAV_BLOCK_FRAMES", 1000)
    frames = _random_pcm(channels * 2500)
    payload = _wav(frames, channels=channels)
    truncated = payload[: len(payload) - cut]

    waveform, _ = _read_wav_float32(io.BytesIO(truncated))

    expected = _reference_decode(frames[: len(frames) - cut], channels)
    assert waveform.tobytes() == expected.tobytes()


def test_an_empty_data_chunk_is_an_empty_waveform():
    waveform, rate = _read_wav_float32(io.BytesIO(_wav(b"", channels=2)))

    assert waveform.size == 0
    assert waveform.dtype == np.float32
    assert rate == 16_000


def test_a_header_declaring_far_more_frames_than_the_file_holds_allocates_the_file():
    """The header's frame count is not trusted for the size of the buffer: a
    small file declaring 4 billion frames must not ask for 16 GB."""
    payload = bytearray(_wav(_random_pcm(100), channels=1))
    payload[40:44] = (0xFFFFFFF0).to_bytes(4, "little")  # data chunk size

    waveform, _ = _read_wav_float32(io.BytesIO(bytes(payload)))

    assert waveform.size == 100


@pytest.mark.parametrize(
    ("channels", "cut_bytes"),
    [(1, 0), (2, 0), (1, 3)],
    ids=["mono", "stereo", "truncated-mono"],
)
def test_decoding_holds_about_one_mono_float32_copy_not_five_file_sizes(
    channels, cut_bytes
):
    """Measured with tracemalloc (numpy reports its buffers to it) on 40 MB of
    16-bit audio: the whole-file decode peaked at several file sizes; the
    block-wise decode peaks at the mono float32 result plus one block. The
    `BytesIO` input is built before tracing starts, so only what the reader
    allocates counts."""
    frames = _random_pcm(20_000_000)  # 40 MB of PCM, whatever the channel count
    # A truncated file declares more frames than it holds, so the reader
    # fills less than it allocated; that case once cost a second full copy.
    payload = _wav(frames, channels=channels)
    source = io.BytesIO(payload[: len(payload) - cut_bytes])
    # A second reference to the bytes makes BytesIO copy them on first read,
    # which tracemalloc would charge to the reader.
    del payload
    file_bytes = len(source.getvalue())

    tracemalloc.start()
    try:
        waveform, _ = _read_wav_float32(source)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # The result plus one block (a few MB) and the interpreter's small change.
    assert peak <= waveform.nbytes + 16 * 1024 * 1024
    assert peak < 3 * file_bytes
