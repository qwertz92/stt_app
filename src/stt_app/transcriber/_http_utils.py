"""Shared HTTP helpers for REST-based remote transcription providers.

These helpers exist so the OpenAI and ElevenLabs providers (and any future
HTTP-only provider) do not duplicate identical multipart encoding and SSL
error formatting.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from ..config import DOC_SSL_PROXY_PATH

_AUDIO_CONTENT_TYPE_BY_SUFFIX = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".webm": "audio/webm",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
}


def audio_content_type(filename: str) -> str:
    """Return a deterministic audio MIME type for supported import suffixes."""
    return _AUDIO_CONTENT_TYPE_BY_SUFFIX.get(
        Path(str(filename or "")).suffix.lower(),
        "application/octet-stream",
    )


def _quoted_header_parameter(value: str, *, label: str) -> str:
    normalized = str(value)
    if "\r" in normalized or "\n" in normalized:
        raise ValueError(f"Multipart {label} must not contain CR or LF characters.")
    return normalized.replace("\\", "\\\\").replace('"', '\\"')


def multipart_form_data(
    *,
    fields: list[tuple[str, str]],
    file_field: tuple[str, str, bytes, str],
) -> tuple[bytes, str]:
    """Encode a multipart/form-data request body.

    ``file_field`` is ``(form_field_name, filename, file_bytes, content_type)``.
    Returns ``(body_bytes, content_type_header_value)``.
    """
    boundary = f"stt-app-{secrets.token_hex(24)}"
    lines: list[bytes] = []

    for name, value in fields:
        quoted_name = _quoted_header_parameter(name, label="field name")
        lines.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                (
                    f'Content-Disposition: form-data; name="{quoted_name}"\r\n\r\n'
                ).encode("utf-8"),
                f"{value}\r\n".encode("utf-8"),
            ]
        )

    field_name, filename, data, content_type = file_field
    quoted_field_name = _quoted_header_parameter(field_name, label="file field name")
    quoted_filename = _quoted_header_parameter(filename, label="filename")
    safe_content_type = str(content_type).strip()
    if not safe_content_type or "\r" in safe_content_type or "\n" in safe_content_type:
        raise ValueError("Multipart content type must be a non-empty single line.")
    lines.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="{quoted_field_name}"; '
                f'filename="{quoted_filename}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {safe_content_type}\r\n\r\n".encode("utf-8"),
            data,
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )

    body = b"".join(lines)
    return body, f"multipart/form-data; boundary={boundary}"


def normalize_transcript_text(value: object) -> str:
    """Collapse whitespace runs and trim, defensively handling ``None``."""
    return " ".join(str(value or "").strip().split()).strip()


def format_ssl_error_message(provider_name: str) -> str:
    """Return the standard SSL/proxy error message for a remote provider."""
    return (
        f"{provider_name}: SSL certificate verification failed "
        "(likely a corporate proxy such as Zscaler). "
        "Set SSL_CERT_FILE or REQUESTS_CA_BUNDLE to your corporate CA .pem. "
        f"See {DOC_SSL_PROXY_PATH} for details."
    )
