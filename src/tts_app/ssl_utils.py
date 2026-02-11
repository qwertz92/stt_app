"""Shared SSL error detection utilities.

Used by local transcriber, AssemblyAI provider, and download script to detect
corporate proxy / Zscaler SSL certificate verification failures.
"""

from __future__ import annotations


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
