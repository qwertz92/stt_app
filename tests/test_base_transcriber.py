"""Tests for transcriber base interface defaults."""

from __future__ import annotations

import pytest

from stt_app.transcriber.base import ITranscriber, strip_language_tags


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

def test_inline_locale_markers_are_removed_from_a_transcript():
    """Nemotron emits its detected locale inline in automatic mode.

    The marker is model metadata that leaked through the decoder, never
    something the user said -- but it was pasted into the document as if it
    were. Reported from real use as "<de-DE>" appearing mid-sentence.
    """
    assert strip_language_tags("<de-DE> Hallo Welt") == "Hallo Welt"
    assert strip_language_tags("Hallo <de-DE> Welt") == "Hallo Welt"
    assert strip_language_tags("<|en|>Hello there") == "Hello there"
    assert strip_language_tags("<zh-Hans-CN> Ni hao") == "Ni hao"


def test_stripping_locale_markers_leaves_real_angle_brackets_alone():
    """A dictation about code must survive unchanged.

    A broader pattern would eat "<html>", "a < b" or "<3" out of the
    transcript, which is a worse bug than the one being fixed.
    """
    for text in (
        "Er sagte a < b und dann c > d",
        "Das <html> Tag bleibt stehen",
        "Ich bin <3 dieses Feature",
        "Verwende <div> und </div>",
        "<tr> ist eine Tabellenzeile, nicht Tuerkisch",
        "<br> und <td> bleiben",
        "Text ohne Tags",
        "",
    ):
        assert strip_language_tags(text) == text, text
