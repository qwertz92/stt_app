"""Tests for transcriber base interface defaults."""

from __future__ import annotations

import logging

import pytest

from stt_app.transcriber.base import (
    ITranscriber,
    TranscriptionCanceled,
    strip_language_tags,
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


def test_no_cancel_check_never_cancels():
    t = MinimalTranscriber()
    assert t._is_cancel_requested() is False
    t._raise_if_canceled()


def test_a_requested_cancel_raises():
    t = MinimalTranscriber()
    t.set_cancel_check(lambda: True)
    assert t._is_cancel_requested() is True
    with pytest.raises(TranscriptionCanceled):
        t._raise_if_canceled()


def test_a_raising_cancel_check_is_logged_once_not_once_per_poll(caplog):
    """A broken check must not fail the run, and must not flood the log.

    The ONNX/WebGPU reader polls this every 0.25 s for the whole
    transcription, so logging the traceback per poll wrote the same stack
    several times a second and buried everything else in the log file.
    """
    t = MinimalTranscriber()
    t.set_cancel_check(lambda: (_ for _ in ()).throw(ValueError("check exploded")))

    with caplog.at_level(logging.ERROR, logger="stt_app.transcriber.base"):
        for _ in range(5):
            assert t._is_cancel_requested() is False
            t._raise_if_canceled()

    tracebacks = [record for record in caplog.records if record.exc_info]
    assert len(tracebacks) == 1
    assert "MinimalTranscriber" in tracebacks[0].getMessage()


def test_every_transcriber_re_arms_the_log_through_the_base_setter():
    """A subclass override must not drop the latch reset.

    The runtimes are cached for the whole app lifetime and a fresh cancel
    check is installed per job, so an override that only assigns
    `_cancel_check` turns "logged once per installed check" into once per
    process -- and the second broken check that session is then silent.
    """
    from stt_app.transcriber.local_faster_whisper import (
        LocalFasterWhisperTranscriber,
    )
    from stt_app.transcriber.local_onnx_asr import LocalOnnxAsrTranscriber

    for transcriber in (
        MinimalTranscriber(),
        LocalFasterWhisperTranscriber(model_size="small"),
        LocalOnnxAsrTranscriber(model_size="parakeet-tdt-0.6b-v3"),
    ):
        transcriber.set_cancel_check(
            lambda: (_ for _ in ()).throw(ValueError("boom"))
        )
        transcriber._is_cancel_requested()
        assert transcriber._cancel_check_failed is True, type(transcriber).__name__

        transcriber.set_cancel_check(lambda: False)

        assert transcriber._cancel_check_failed is False, type(transcriber).__name__


def test_installing_a_new_cancel_check_re_arms_the_log():
    """The latch is per installed check, not per transcriber.

    The runtime is cached and reused across dictations, so latching for the
    object's lifetime would hide a check that starts failing later.
    """
    t = MinimalTranscriber()
    t.set_cancel_check(lambda: (_ for _ in ()).throw(ValueError("boom")))
    t._is_cancel_requested()
    assert t._cancel_check_failed is True

    t.set_cancel_check(lambda: False)

    assert t._cancel_check_failed is False

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<de-DE> Hallo Welt", " Hallo Welt"),
        ("Hallo <de-DE> Welt", "Hallo Welt"),
        ("<|en|>Hello there", "Hello there"),
        ("<|de-DE|>Hallo", "Hallo"),
        ("<zh-Hans-CN> Ni hao", " Ni hao"),
        ("<es-419> Hola", " Hola"),
        # An upper-case language subtag is still a locale.
        ("<DE-DE> Hallo", " Hallo"),
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
        # 23 of the supported language codes are ordinary English or
        # German words. Matching the region case-insensitively deleted
        # every one of these out of real dictation.
        "Der Code ist <as-is> zu uebernehmen",
        "Das ist ein <no-go> fuer uns",
        "Pruefe ob <is-ok> gesetzt ist",
        "Setze <my-id> auf null",
        "Es war <so-so>",
        "<it-is> wahr und <he-is> da",
        "<or-so> etwa",
        # A lower-case region is deliberately NOT a locale. That is
        # exactly what keeps the words above intact, and no model in
        # use emits "<de-de>" -- the observed forms are "<de-DE>" and
        # "<|en|>".
        "<de-de> bleibt stehen",
        "<en-us> bleibt auch",
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
