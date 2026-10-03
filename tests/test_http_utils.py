from __future__ import annotations

import io
import urllib.error

import pytest

from stt_app.transcriber._http_utils import (
    audio_content_type,
    http_error_suffix,
    multipart_form_data,
    read_http_error_detail,
)
from stt_app.transcriber.base import TranscriptionError


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("speech.wav", "audio/wav"),
        ("speech.MP3", "audio/mpeg"),
        ("speech.flac", "audio/flac"),
        ("speech.ogg", "audio/ogg"),
        ("speech.opus", "audio/ogg"),
        ("speech.webm", "audio/webm"),
        ("speech.m4a", "audio/mp4"),
        ("speech.aac", "audio/aac"),
        ("speech.unknown", "application/octet-stream"),
    ],
)
def test_audio_content_type_is_suffix_aware(filename, expected):
    assert audio_content_type(filename) == expected


def test_multipart_boundaries_are_random_and_match_the_body():
    first_body, first_header = multipart_form_data(
        fields=[("model", "test")],
        file_field=("file", "audio.wav", b"data", "audio/wav"),
    )
    second_body, second_header = multipart_form_data(
        fields=[("model", "test")],
        file_field=("file", "audio.wav", b"data", "audio/wav"),
    )

    assert first_header != second_header
    first_boundary = first_header.removeprefix("multipart/form-data; boundary=")
    assert f"--{first_boundary}\r\n".encode() in first_body
    assert first_body.endswith(f"--{first_boundary}--\r\n".encode())
    assert second_body != first_body


@pytest.mark.parametrize(
    "file_field",
    [
        ("file\r\nX-Injected: yes", "audio.wav", b"data", "audio/wav"),
        ("file", "audio.wav\r\nX-Injected: yes", b"data", "audio/wav"),
        ("file", "audio.wav", b"data", "audio/wav\r\nX-Injected: yes"),
    ],
)
def test_multipart_rejects_header_injection(file_field):
    with pytest.raises(ValueError, match="must"):
        multipart_form_data(fields=[], file_field=file_field)


def test_multipart_escapes_quoted_header_parameters():
    body, _header = multipart_form_data(
        fields=[('model"variant', "test")],
        file_field=("file", 'my "audio".wav', b"data", "audio/wav"),
    )

    assert b'name="model\\"variant"' in body
    assert b'filename="my \\"audio\\".wav"' in body


def _http_error(body: bytes, code: int = 400, reason: str = "Bad Request"):
    return urllib.error.HTTPError(
        "https://api.example/x", code, reason, {}, io.BytesIO(body)
    )


def test_the_error_detail_is_what_the_provider_said_not_the_status_phrase():
    """`HTTPError.reason` is only "Bad Request".

    A message built from it throws away the one part that tells the user what
    to change -- OpenAI's "Invalid file format", ElevenLabs' quota text,
    Deepgram's rejected parameter -- and four providers built theirs that way
    while Azure alone read the body.
    """
    assert (
        read_http_error_detail(
            _http_error(b'{"error": {"message": "Invalid file format."}}')
        )
        == "Invalid file format."
    )
    assert (
        read_http_error_detail(_http_error(b'{"error": "model not found"}'))
        == "model not found"
    )
    assert read_http_error_detail(_http_error(b'{"message": "Quota exceeded"}')) == (
        "Quota exceeded"
    )
    assert read_http_error_detail(_http_error(b'{"err_msg": "invalid model"}')) == (
        "invalid model"
    )


def test_a_non_json_error_body_is_passed_through_and_capped():
    assert read_http_error_detail(_http_error(b"upstream request timeout")) == (
        "upstream request timeout"
    )
    assert len(read_http_error_detail(_http_error(b'"' + b"x" * 900 + b'"'))) == 300


@pytest.mark.parametrize(
    ("body", "title"),
    [
        pytest.param(
            b"<html><head><title>Zscaler - Access Denied</title></head><body>"
            + b"blocked by policy " * 40
            + b"</body></html>",
            "Zscaler - Access Denied",
            id="proxy-403",
        ),
        pytest.param(
            b"<html>\r\n<head><title>413 Request Entity Too Large</title></head>\r\n"
            b"<body><center><h1>413 Request Entity Too Large</h1></center></body></html>",
            "413 Request Entity Too Large",
            id="nginx-413",
        ),
        pytest.param(
            b"\xef\xbb\xbf<!DOCTYPE html><TITLE>\n  Proxy &amp; Auth\n</TITLE>",
            "Proxy & Auth",
            id="bom-entities-whitespace",
        ),
        pytest.param(
            b"<html><title>" + b"x" * 400 + b"</title></html>",
            "x" * 77 + "...",
            id="long-title",
        ),
    ],
)
def test_a_markup_error_page_is_reported_by_its_title_not_pasted(body, title):
    """A proxy's block page (403 Zscaler, 407, nginx 413) used to be pasted
    into the message as markup (review of 2026-10-03)."""
    detail = read_http_error_detail(_http_error(body))

    assert detail == (
        f'the reply was an HTML page titled "{title}" (a proxy or firewall block page?)'
    )
    assert "<" not in detail
    assert http_error_suffix(_http_error(body)) == f": {detail}"


@pytest.mark.parametrize(
    "body",
    [
        b"<html><body><h1>502 Bad Gateway</h1></body></html>",
        b"<html><title></title></html>",
        b"<html><title>never closed",
        b"\xef\xbb\xbf  <!DOCTYPE html><body>blocked</body>",
        b"<HEAD><meta charset=utf-8></HEAD>",
        b"<body>blocked</body>",
    ],
)
def test_a_markup_error_page_without_a_title_is_named_as_a_page(body):
    assert read_http_error_detail(_http_error(body)) == (
        "the reply was an HTML page (a proxy or firewall block page?)"
    )


@pytest.mark.parametrize(
    "body",
    [
        (
            b'<?xml version="1.0"?><Error><Code>AccessDenied</Code>'
            b"<Message>Denied by bucket policy</Message></Error>"
        ),
        b"<Error><Message>Denied by bucket policy</Message></Error>",
        b"<error>plain text in angle brackets</error>",
    ],
)
def test_an_xml_error_body_is_not_called_an_html_page(body):
    """Only real HTML is a block page; an XML error carries the provider's
    message, which the HTML sentence threw away (review of 2026-10-03)."""
    detail = read_http_error_detail(_http_error(body))
    assert "HTML page" not in detail
    assert detail == body.decode().strip()[:300]


def test_a_title_search_is_bounded_on_a_page_of_unclosed_titles():
    """64 KB of `<title>` without a close was quadratic: 3.95 s."""
    import time

    body = b"<title>" * 9000
    started = time.monotonic()
    detail = read_http_error_detail(_http_error(body))
    assert time.monotonic() - started < 1.0
    assert detail == "the reply was an HTML page (a proxy or firewall block page?)"


def test_an_unreadable_or_empty_body_falls_back_to_the_status_phrase():
    assert read_http_error_detail(_http_error(b"")) == ""
    assert http_error_suffix(_http_error(b"")) == ": Bad Request"
    assert http_error_suffix(_http_error(b'{"error": {"message": "nope"}}')) == ": nope"


def test_reading_a_body_that_raises_is_not_an_error_of_its_own():
    class _Unreadable(urllib.error.HTTPError):
        def read(self, *_args, **_kwargs):
            raise OSError("connection reset")

    exc = _Unreadable("https://api.example/x", 500, "Server Error", {}, io.BytesIO(b""))
    assert read_http_error_detail(exc) == ""
    assert http_error_suffix(exc) == ": Server Error"


class _CountingBody(io.BytesIO):
    """Reports how much was actually pulled off the response."""

    def __init__(self, size: int):
        super().__init__(b"x" * size)
        self.read_amounts: list[int | None] = []

    def read(self, amt=None):
        self.read_amounts.append(amt)
        return super().read(amt)


def test_a_huge_error_body_is_not_pulled_into_memory_whole():
    """The 300-char cap is applied to the extracted text, not to the read.

    Measured before this: a 50,000,000-byte error body was read in full to
    produce a 300-character detail. `urlopen(timeout=...)` bounds each socket
    read, not the total, so nothing else stopped it.
    """
    from stt_app.transcriber._http_utils import _MAX_ERROR_BODY_BYTES

    body = _CountingBody(50_000_000)
    exc = urllib.error.HTTPError("https://api.example/x", 400, "Bad Request", {}, body)

    detail = read_http_error_detail(exc)

    assert body.read_amounts == [_MAX_ERROR_BODY_BYTES], body.read_amounts
    assert len(detail) <= 300, len(detail)
    assert body.tell() <= _MAX_ERROR_BODY_BYTES, body.tell()


def test_a_normal_json_error_body_is_still_read_and_unwrapped():
    """The bound must not clip a real provider error object."""
    from stt_app.transcriber._http_utils import _MAX_ERROR_BODY_BYTES

    padding = "a" * 4000
    body = (
        '{"padding": "' + padding + '", "error": {"message": "Invalid file format"}}'
    ).encode("utf-8")
    assert len(body) < _MAX_ERROR_BODY_BYTES

    assert read_http_error_detail(_http_error(body)) == "Invalid file format"


def test_a_message_nested_under_detail_is_unwrapped_not_stringified():
    """ElevenLabs' documented shape is `{"detail": {"message": ...}}`.

    The key loop found `detail`, a dict, and `str()`-ed the whole thing: the
    user was shown Python dict syntax with the request id and the parameter
    name, capped mid-dict at 300 characters. Read from the vendor's own error
    page, not assumed.
    """
    body = (
        b'{"detail": {"type": "validation_error", "code": "invalid_parameters", '
        b'"message": "The \'keyterms\' parameter is only supported with the '
        b"'scribe_v2' model. You specified 'scribe_v1'.\", "
        b'"status": "invalid_parameters", "request_id": "3c807fc4c3a1705f9638ecc7", '
        b'"param": "keyterms"}}'
    )

    detail = read_http_error_detail(_http_error(body))

    assert detail == (
        "The 'keyterms' parameter is only supported with the 'scribe_v2' model. "
        "You specified 'scribe_v1'."
    )
    assert "{" not in detail


def test_a_detail_object_without_a_message_still_shows_something_readable():
    body = b'{"detail": {"status": "quota_exceeded", "code": 429}}'

    detail = read_http_error_detail(_http_error(body))

    # The inner `status`, not `str()` of the dict: the parent produced
    # "{'status': 'quota_exceeded', 'code': 429}", which the two assertions
    # this test used to make also accepted.
    assert detail == "quota_exceeded"


def test_an_error_string_under_detail_is_the_message():
    """A LiteLLM/FastAPI gateway answers 403 `{"detail": {"error": "..."}}`;
    the object was shown as JSON text (review of 2026-10-03)."""
    body = b'{"detail": {"error": "Authentication Error, user not allowed"}}'

    assert read_http_error_detail(_http_error(body)) == (
        "Authentication Error, user not allowed"
    )


def test_an_error_object_in_a_successful_reply_is_named():
    from stt_app.transcriber._http_utils import transcript_from_json

    with pytest.raises(TranscriptionError, match="model not found") as raised:
        transcript_from_json(
            b'{"error": {"message": "model not found"}}', prefix="Custom endpoint"
        )
    assert "HTTP 200" in str(raised.value)
    # Without an error member the old message stays.
    with pytest.raises(TranscriptionError, match="has no 'text' field"):
        transcript_from_json(b'{"result": "x"}', prefix="Custom endpoint")


def test_a_detail_that_is_a_plain_string_is_kept_as_before():
    assert (
        read_http_error_detail(_http_error(b'{"detail": "Not Found"}')) == "Not Found"
    )


def test_a_status_phrase_under_error_keeps_the_detail_beside_it():
    """Speechmatics answers `{"code", "error", "detail"}`, where `error` is
    only the status phrase and `detail` is what to change; reading `error`
    alone handed the user "Forbidden" for a missing entitlement."""
    body = b'{"code": 403, "detail": "Entitlement check failed", "error": "Forbidden"}'

    assert read_http_error_detail(_http_error(body)) == (
        "Forbidden: Entitlement check failed"
    )


def test_a_detail_that_already_repeats_the_error_is_not_doubled():
    body = b'{"error": "Job rejected", "detail": "Job rejected: file too long"}'

    assert read_http_error_detail(_http_error(body)) == "Job rejected: file too long"


def test_the_recovered_text_suffix_lives_here_once_for_every_caller():
    """Fun-ASR's recovered-text convention (what a failure leaves behind goes
    into the error, bounded) is shared with the loop over a long recording's
    parts. One definition, imported by both, so the two cannot drift apart:
    Fun-ASR's own tests hold its behaviour, the parts tests the loop's."""
    from stt_app.transcriber import _audio_parts, _http_utils, funasr_provider

    shared = _http_utils.recovered_text_suffix
    assert funasr_provider.recovered_text_suffix is shared
    assert _audio_parts.recovered_text_suffix is shared
    assert not hasattr(funasr_provider.FunAsrTranscriber, "_recovered_suffix")
    assert not hasattr(funasr_provider, "_RECOVERED_TEXT_MAX_CHARS")
    assert _http_utils.RECOVERED_TEXT_MAX_CHARS == 2000
