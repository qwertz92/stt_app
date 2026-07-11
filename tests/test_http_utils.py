from __future__ import annotations

import pytest

from stt_app.transcriber._http_utils import audio_content_type, multipart_form_data


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
