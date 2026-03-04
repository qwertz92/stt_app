"""Tests for transcriber base interface defaults."""

from __future__ import annotations

import pytest

from stt_app.transcriber.base import ITranscriber


class MinimalTranscriber(ITranscriber):
    """Concrete subclass that only implements transcribe_batch."""

    def transcribe_batch(self, wav_bytes: bytes) -> str:
        return "text"


def test_default_start_stream_raises():
    t = MinimalTranscriber()
    with pytest.raises(NotImplementedError):
        t.start_stream()


def test_default_push_audio_chunk_raises():
    t = MinimalTranscriber()
    with pytest.raises(NotImplementedError):
        t.push_audio_chunk(b"chunk")


def test_default_stop_stream_raises():
    t = MinimalTranscriber()
    with pytest.raises(NotImplementedError):
        t.stop_stream()


def test_default_abort_stream_raises():
    t = MinimalTranscriber()
    with pytest.raises(NotImplementedError):
        t.abort_stream()



