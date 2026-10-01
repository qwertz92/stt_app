"""Shared fakes for the REST providers' tests: a fake HTTP response and
readers that parse a sent multipart body back into its parts.

Parsed out of the encoded body rather than read off the provider, because
what the service receives is the body: a repeated field is only repeated if
the encoder actually wrote it twice.
"""

from __future__ import annotations

import io
import json
import urllib.error
import wave

import numpy as np


def _parts(request) -> list[tuple[str, bytes]]:
    boundary = request.get_header("Content-type").split("boundary=", 1)[1]
    parts: list[tuple[str, bytes]] = []
    for part in request.data.split(f"--{boundary}".encode())[1:-1]:
        head, _, body = part.partition(b"\r\n\r\n")
        parts.append((head.decode("utf-8", errors="replace"), body[: -len(b"\r\n")]))
    return parts


def sent_fields(request) -> list[tuple[str, str]]:
    """The non-file multipart fields of a request, in the order they were sent."""
    fields: list[tuple[str, str]] = []
    for headers, body in _parts(request):
        if "filename=" in headers:
            continue
        name = headers.split('name="', 1)[1].split('"', 1)[0]
        fields.append((name, body.decode("utf-8")))
    return fields


def sent_file(request) -> tuple[str, str, bytes]:
    """The file part of a request: its field name, file name and bytes."""
    for headers, body in _parts(request):
        if "filename=" in headers:
            name = headers.split('name="', 1)[1].split('"', 1)[0]
            filename = headers.split('filename="', 1)[1].split('"', 1)[0]
            return name, filename, body
    raise AssertionError("the request carried no file")


def fake_response(payload: bytes | str, status: int = 200, headers=None):
    data = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    response_headers = dict(headers or {})

    class _Headers:
        def get(self, name, default=None):
            for key, value in response_headers.items():
                if key.lower() == name.lower():
                    return value
            return default

        def get_content_type(self):
            value = self.get("Content-Type", "application/json")
            return value.split(";", 1)[0].strip().lower()

    class _Resp:
        def __init__(self):
            self.status = status
            self.headers = _Headers()

        def read(self, *_args):
            return data

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    return _Resp()


def json_response(payload, status: int = 200):
    return fake_response(json.dumps(payload), status)


def http_error(url: str, code: int, body: bytes | str = b""):
    data = body if isinstance(body, bytes) else body.encode("utf-8")
    return urllib.error.HTTPError(url, code, "Error", {}, io.BytesIO(data))


def wav_seconds(seconds: float) -> bytes:
    """A 16 kHz mono 16-bit WAV, the format the app records."""
    count = int(seconds * 16_000)
    tone = (np.sin(np.arange(count) / 12.0) * 6000.0).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(tone.tobytes())
    return buffer.getvalue()


def seconds_of(wav: bytes) -> float:
    with wave.open(io.BytesIO(wav), "rb") as handle:
        return handle.getnframes() / handle.getframerate()
