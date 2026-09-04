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
    assert read_http_error_detail(
        _http_error(b'{"error": {"message": "Invalid file format."}}')
    ) == "Invalid file format."
    assert read_http_error_detail(
        _http_error(b'{"error": "model not found"}')
    ) == "model not found"
    assert read_http_error_detail(_http_error(b'{"message": "Quota exceeded"}')) == (
        "Quota exceeded"
    )
    assert read_http_error_detail(_http_error(b'{"err_msg": "invalid model"}')) == (
        "invalid model"
    )


def test_a_non_json_error_body_is_passed_through_and_capped():
    """A provider must not be able to push an HTML error page into a dialog."""
    assert read_http_error_detail(_http_error(b"<html>Gateway timeout</html>")) == (
        "<html>Gateway timeout</html>"
    )
    assert len(read_http_error_detail(_http_error(b'"' + b"x" * 900 + b'"'))) == 300


def test_an_unreadable_or_empty_body_falls_back_to_the_status_phrase():
    assert read_http_error_detail(_http_error(b"")) == ""
    assert http_error_suffix(_http_error(b"")) == ": Bad Request"
    assert http_error_suffix(
        _http_error(b'{"error": {"message": "nope"}}')
    ) == ": nope"


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
    exc = urllib.error.HTTPError(
        "https://api.example/x", 400, "Bad Request", {}, body
    )

    detail = read_http_error_detail(exc)

    assert body.read_amounts == [_MAX_ERROR_BODY_BYTES], body.read_amounts
    assert len(detail) <= 300, len(detail)
    assert body.tell() <= _MAX_ERROR_BODY_BYTES, body.tell()


def test_a_normal_json_error_body_is_still_read_and_unwrapped():
    """The bound must not clip a real provider error object."""
    from stt_app.transcriber._http_utils import _MAX_ERROR_BODY_BYTES

    padding = "a" * 4000
    body = (
        '{"padding": "' + padding + '", '
        '"error": {"message": "Invalid file format"}}'
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
        b'\'scribe_v2\' model. You specified \'scribe_v1\'.", '
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


def test_a_detail_that_is_a_plain_string_is_kept_as_before():
    assert read_http_error_detail(_http_error(b'{"detail": "Not Found"}')) == "Not Found"
