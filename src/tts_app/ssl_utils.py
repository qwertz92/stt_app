"""Shared SSL error detection and CA-bundle resolution utilities.

Used by all remote transcription providers and the download script to detect
corporate proxy / Zscaler SSL certificate verification failures and to
resolve custom CA bundles.
"""

from __future__ import annotations

import os
import ssl
from pathlib import Path


def resolve_ca_bundle() -> str | None:
    """Find a custom CA bundle path from environment variables.

    Checks ``SSL_CERT_FILE`` and ``REQUESTS_CA_BUNDLE`` (in that order).
    Returns the path string if the file exists, otherwise ``None``.

    This is needed because different HTTP libraries honour different
    environment variables:

    * ``httpx`` (used by the Groq SDK) does **not** read
      ``REQUESTS_CA_BUNDLE`` and does not always respect ``SSL_CERT_FILE``.
    * ``urllib.request`` (used by OpenAI / Deepgram providers) reads
      ``SSL_CERT_FILE`` only via Python's ``ssl`` module, not
      ``REQUESTS_CA_BUNDLE``.
    * ``requests`` (used by AssemblyAI SDK) reads ``REQUESTS_CA_BUNDLE``.

    By resolving the bundle path centrally we can pass it **explicitly** to
    every HTTP library, guaranteeing it takes effect regardless of which env
    var the user has set.
    """
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(var, "").strip()
        if path and Path(path).is_file():
            return path
    return None


def create_ssl_context() -> ssl.SSLContext | None:
    """Create an SSL context using a custom CA bundle if available.

    Returns ``None`` when no custom CA bundle is configured, which tells
    callers to use the library default.  When a bundle *is* found, returns
    an ``ssl.SSLContext`` that trusts the certificates in that bundle.
    """
    ca_bundle = resolve_ca_bundle()
    if ca_bundle is None:
        return None
    ctx = ssl.create_default_context(cafile=ca_bundle)
    return ctx


def is_ssl_error(exc: Exception) -> bool:
    """Check if an exception is caused by SSL certificate verification failure.

    Walks the exception chain (``__cause__``) to detect chained SSL errors
    (e.g. ``requests`` wrapping an ``urllib3`` wrapping an ``ssl`` error).
    """
    msg = str(exc).lower()
    ssl_markers = (
        "certificate_verify_failed",
        "ssl: certificate_verify_failed",
        "certificate verify failed",
        "unable to get local issuer certificate",
        "self-signed certificate",
        "sslcertverificationerror",
    )
    for marker in ssl_markers:
        if marker in msg:
            return True
    # Walk the exception chain (__cause__).
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        return is_ssl_error(cause)
    return False
