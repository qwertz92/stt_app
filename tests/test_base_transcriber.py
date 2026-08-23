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

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<de-DE> Hallo Welt", " Hallo Welt"),
        ("Hallo <de-DE> Welt", "Hallo Welt"),
        ("<|en|>Hello there", "Hello there"),
        ("<|de-DE|>Hallo", "Hallo"),
        ("<zh-Hans-CN> Ni hao", " Ni hao"),
        # Lower case too -- a model may emit either.
        ("<de-de> Hallo", " Hallo"),
        ("<en-us> Hi", " Hi"),
        ("<es-419> Hola", " Hola"),
        ("Text <en-US> mitten drin", "Text mitten drin"),
    ],
)
def test_inline_locale_markers_are_removed_from_a_transcript(text, expected):
    """Nemotron emits its detected locale inline in automatic mode.

    The marker is model metadata that leaked through the decoder, never
    something the user said -- but it was pasted into the document as if it
    were. Reported from real use as "<de-DE>" appearing mid-sentence.
    """
    assert strip_language_tags(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # Web components and framework tags: hyphenated, and a pattern of
        # "<xx-anything>" deletes every one of them without trace.
        "Web component <my-widget> einbinden",
        "Vue: <el-button> klicken",
        "Ionic <ion-button> ist ein Element",
        "Polymer <dom-if> Template",
        "Angular <ng-container> bleibt",
        "Datei <log-2026> oeffnen",
        # Locale SHAPE but not a language the app knows. Matching by
        # shape alone deleted every one of these from real dictation.
        "Erstelle einen <to-DO> Eintrag",
        "Wirf einen <err-404> Fehler",
        "Lies die <job-ID> aus",
        "Klicke <btn-OK> an",
        "Oeffne <log-100> bitte",
        "Das <|pad|> Token bleibt",
        "<|bos|> und <|eos|> bleiben",
        # Plain markup and maths.
        "Verwende <div> und </div>",
        "<tr> ist eine Tabellenzeile, nicht Tuerkisch",
        "<br> und <td> und <html> bleiben",
        "Er sagte a < b und dann c > d",
        "Ich bin <3 dieses Feature",
        "Text ohne Tags",
        "",
    ],
)
def test_stripping_locale_markers_leaves_real_dictation_alone(text):
    """Deleting real speech is a worse bug than the one being fixed.

    A dictation about front-end code is full of hyphenated angle-bracket
    words. Matching subtags by shape -- an uppercase region or a title-case
    script -- is what separates a locale from a component name.
    """
    assert strip_language_tags(text) == text


def test_stripping_preserves_the_spaces_between_decoded_chunks():
    """The streaming path strips per chunk and concatenates the results.

    Trimming the ends of each chunk welds the last word of one onto the
    first word of the next: "Guten" + " Tag" + " heute" became
    "Guten Tagheute".
    """
    chunks = ["Guten", " Tag", " <de-DE> heute", " ist"]
    assert "".join(strip_language_tags(chunk) for chunk in chunks) == (
        "Guten Tag heute ist"
    )
