"""Tests for transcriber base interface and remote placeholders."""

from __future__ import annotations

import pytest

from tts_app.transcriber.base import ITranscriber, TranscriptionError
from tts_app.transcriber.remote_placeholders import (
    AzureTranscriber,
    DeepgramTranscriber,
    OpenAITranscriber,
)


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


def test_openai_placeholder_raises():
    t = OpenAITranscriber()
    with pytest.raises(NotImplementedError, match="Phase 2"):
        t.transcribe_batch(b"")


def test_azure_placeholder_raises():
    t = AzureTranscriber()
    with pytest.raises(NotImplementedError, match="Phase 2"):
        t.transcribe_batch(b"")


def test_deepgram_placeholder_raises():
    t = DeepgramTranscriber()
    with pytest.raises(NotImplementedError, match="Phase 2"):
        t.transcribe_batch(b"")
