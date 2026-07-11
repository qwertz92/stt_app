from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .update_checker import INSTALLER_ASSET_NAME, UpdateCheckResult

_CHECKSUM_RESPONSE_LIMIT = 4096
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_ALLOWED_REDIRECT_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
# Populate this in the release that introduces Authenticode signing. A valid
# signature from an arbitrary publisher is not sufficient for automatic install.
TRUSTED_WINDOWS_PUBLISHER_SUBJECTS: frozenset[str] = frozenset()
_SHA256_RE = re.compile(r"^([0-9a-fA-F]{64})(?:[ \t]+\*?([^\r\n]+))?[ \t]*$")


class UpdateDownloadCancelled(RuntimeError):
    pass


def _response_url(response: Any, requested_url: str) -> str:
    geturl = getattr(response, "geturl", None)
    return str(geturl() if callable(geturl) else requested_url)


def _is_trusted_download_response_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.lower() == "https"
        and str(parsed.hostname or "").lower() in _ALLOWED_REDIRECT_HOSTS
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and parsed.path
        and not parsed.fragment
    )


def _open_trusted_url(url: str, *, timeout_s: float, urlopen) -> Any:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/octet-stream", "User-Agent": "stt_app-updater"},
    )
    response = urlopen(request, timeout=timeout_s)
    if not _is_trusted_download_response_url(_response_url(response, url)):
        response.close()
        raise ValueError("GitHub redirected the update download to an untrusted host.")
    return response


def _expected_sha256(raw: bytes) -> str:
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("The installer checksum is not valid ASCII text.") from exc
    match = _SHA256_RE.fullmatch(text)
    if match is None:
        raise ValueError("The installer checksum has an invalid format.")
    filename = str(match.group(2) or "").strip()
    if filename and filename != INSTALLER_ASSET_NAME:
        raise ValueError("The checksum does not identify the expected installer.")
    return match.group(1).lower()


def download_verified_installer(
    result: UpdateCheckResult,
    destination_dir: Path,
    *,
    progress: Callable[[int, int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    timeout_s: float = 30.0,
    urlopen=urllib.request.urlopen,
) -> Path:
    if not result.supports_in_app_update:
        raise ValueError("This release does not provide a verifiable installer.")
    destination_dir.mkdir(parents=True, exist_ok=True)
    target = destination_dir / INSTALLER_ASSET_NAME
    partial = target.with_suffix(f"{target.suffix}.partial")
    partial.unlink(missing_ok=True)

    with _open_trusted_url(
        result.installer_checksum_url,
        timeout_s=timeout_s,
        urlopen=urlopen,
    ) as response:
        checksum_raw = response.read(_CHECKSUM_RESPONSE_LIMIT + 1)
    if len(checksum_raw) > _CHECKSUM_RESPONSE_LIMIT:
        raise ValueError("The installer checksum exceeded the size limit.")
    expected_hash = _expected_sha256(checksum_raw)

    digest = hashlib.sha256()
    downloaded = 0
    total = int(result.installer_size)
    try:
        with (
            _open_trusted_url(
                result.installer_url,
                timeout_s=timeout_s,
                urlopen=urlopen,
            ) as response,
            partial.open("wb") as destination,
        ):
            while True:
                if cancelled is not None and cancelled():
                    raise UpdateDownloadCancelled("The update download was cancelled.")
                chunk = response.read(_DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > total:
                    raise ValueError("The installer exceeded its declared size.")
                digest.update(chunk)
                destination.write(chunk)
                if progress is not None:
                    progress(downloaded, total)
            destination.flush()
            os.fsync(destination.fileno())
        if downloaded != total:
            raise ValueError("The installer size did not match the release metadata.")
        if digest.hexdigest().lower() != expected_hash:
            raise ValueError("The installer SHA-256 checksum did not match.")
        os.replace(partial, target)
        return target
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def verify_windows_publisher_signature(
    installer_path: Path,
    *,
    runner=subprocess.run,
    trusted_publishers: frozenset[str] = TRUSTED_WINDOWS_PUBLISHER_SUBJECTS,
) -> tuple[bool, str]:
    if sys.platform != "win32":
        return False, "Publisher-signature verification is available only on Windows."
    command = (
        "& { param([string]$InstallerPath) "
        "$signature = Get-AuthenticodeSignature -LiteralPath $InstallerPath; "
        "Write-Output $signature.Status; "
        "if ($signature.SignerCertificate) { "
        "Write-Output $signature.SignerCertificate.Subject } }"
    )
    try:
        completed = runner(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
                str(installer_path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception as exc:
        return False, f"Could not verify the Windows publisher signature: {exc}"
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode == 0 and lines and lines[0].lower() == "valid":
        publisher = lines[1] if len(lines) > 1 else ""
        if publisher and publisher in trusted_publishers:
            return True, publisher
        return (
            False,
            "Windows verified the signature, but this app version does not trust "
            f"its publisher identity ({publisher or 'unknown publisher'}).",
        )
    status = lines[0] if lines else "verification failed"
    return False, f"Windows reported the publisher signature as {status}."
