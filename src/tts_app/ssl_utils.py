"""Shared SSL error detection and CA-bundle resolution utilities.

Used by all remote transcription providers and the download script to detect
corporate proxy / Zscaler SSL certificate verification failures and to
resolve custom CA bundles.
"""

from __future__ import annotations

import os
import ssl
from pathlib import Path


def inject_system_trust_store() -> bool:
    """Inject OS certificate store into Python's ssl module.

    Uses the ``truststore`` package to make Python trust every certificate
    that the operating system trusts (e.g. the Windows Certificate Store).
    This automatically handles corporate proxy CAs (Zscaler, BlueCoat,
    Forcepoint, …) without any manual env-var configuration, because IT
    typically installs the proxy CA into the OS trust store.

    Call this **once**, early in startup, before any HTTPS connection.

    Returns ``True`` if injection succeeded, ``False`` otherwise (e.g. when
    ``truststore`` is not installed).
    """
    try:
        import truststore  # type: ignore

        truststore.inject_into_ssl()
        return True
    except Exception:
        return False


def sync_ca_bundle_env_vars() -> None:
    """Ensure both ``SSL_CERT_FILE`` and ``REQUESTS_CA_BUNDLE`` are set.

    Different HTTP libraries read different environment variables:

    * ``requests`` (AssemblyAI, HuggingFace) → ``REQUESTS_CA_BUNDLE``
    * ``httpx`` (Groq) → ``SSL_CERT_FILE`` (or explicit verify)
    * ``urllib`` (OpenAI, Deepgram) → ``SSL_CERT_FILE``

    If the user has set only *one* of them, copy its value to the other
    so that all libraries benefit.  Does nothing if neither is set or if
    both are already pointing to existing files.
    """
    ssl_cert = os.environ.get("SSL_CERT_FILE", "").strip()
    requests_bundle = os.environ.get("REQUESTS_CA_BUNDLE", "").strip()

    if ssl_cert and Path(ssl_cert).is_file() and not requests_bundle:
        os.environ["REQUESTS_CA_BUNDLE"] = ssl_cert
    elif requests_bundle and Path(requests_bundle).is_file() and not ssl_cert:
        os.environ["SSL_CERT_FILE"] = requests_bundle


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
