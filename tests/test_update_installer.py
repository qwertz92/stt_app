from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from stt_app import update_installer
from stt_app.update_checker import INSTALLER_ASSET_NAME, UpdateCheckResult
from stt_app.update_installer import (
    UpdateDownloadCancelled,
    download_verified_installer,
    verify_windows_publisher_signature,
)


class _BytesResponse:
    def __init__(self, raw: bytes, url: str) -> None:
        self._raw = raw
        self._url = url
        self._offset = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def geturl(self) -> str:
        return self._url

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._raw) - self._offset
        chunk = self._raw[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


def _result(payload: bytes) -> UpdateCheckResult:
    base = "https://github.com/qwertz92/stt_app/releases/download/v1.0.0/"
    return UpdateCheckResult(
        current_version="0.9.0",
        latest_version="1.0.0",
        latest_tag="v1.0.0",
        update_available=True,
        installer_url=base + INSTALLER_ASSET_NAME,
        installer_size=len(payload),
        installer_checksum_url=base + f"{INSTALLER_ASSET_NAME}.sha256",
    )


def test_download_verified_installer_checks_size_and_sha256(tmp_path):
    payload = b"verified installer bytes"
    result = _result(payload)
    checksum = f"{hashlib.sha256(payload).hexdigest()}  {INSTALLER_ASSET_NAME}\n"
    progress = []

    def urlopen(request, timeout):
        assert timeout == 30.0
        if request.full_url.endswith(".sha256"):
            return _BytesResponse(checksum.encode("ascii"), request.full_url)
        return _BytesResponse(payload, request.full_url)

    path = download_verified_installer(
        result,
        tmp_path,
        progress=lambda current, total: progress.append((current, total)),
        urlopen=urlopen,
    )

    assert path == tmp_path / INSTALLER_ASSET_NAME
    assert path.read_bytes() == payload
    assert progress[-1] == (len(payload), len(payload))
    assert not (tmp_path / f"{INSTALLER_ASSET_NAME}.partial").exists()


def test_download_verified_installer_removes_partial_on_cancel(tmp_path):
    payload = b"x" * (update_installer._DOWNLOAD_CHUNK_BYTES + 1)
    result = _result(payload)
    checksum = hashlib.sha256(payload).hexdigest().encode("ascii")
    cancel = False

    def on_progress(_current, _total):
        nonlocal cancel
        cancel = True

    def urlopen(request, **_kwargs):
        raw = checksum if request.full_url.endswith(".sha256") else payload
        return _BytesResponse(raw, request.full_url)

    with pytest.raises(UpdateDownloadCancelled):
        download_verified_installer(
            result,
            tmp_path,
            progress=on_progress,
            cancelled=lambda: cancel,
            urlopen=urlopen,
        )

    assert not (tmp_path / f"{INSTALLER_ASSET_NAME}.partial").exists()
    assert not (tmp_path / INSTALLER_ASSET_NAME).exists()


def test_download_verified_installer_rejects_untrusted_redirect(tmp_path):
    payload = b"installer"
    result = _result(payload)

    def urlopen(_request, **_kwargs):
        return _BytesResponse(b"ignored", "https://attacker.example/update")

    with pytest.raises(ValueError, match="untrusted host"):
        download_verified_installer(result, tmp_path, urlopen=urlopen)


def test_download_verified_installer_rejects_checksum_mismatch(tmp_path):
    payload = b"installer"
    result = _result(payload)

    def urlopen(request, **_kwargs):
        raw = b"0" * 64 if request.full_url.endswith(".sha256") else payload
        return _BytesResponse(raw, request.full_url)

    with pytest.raises(ValueError, match="checksum did not match"):
        download_verified_installer(result, tmp_path, urlopen=urlopen)


def test_verify_windows_publisher_signature_accepts_valid_status(monkeypatch):
    monkeypatch.setattr(update_installer.sys, "platform", "win32")
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout="Valid\nCN=Example Publisher\n",
        )

    valid, detail = verify_windows_publisher_signature(
        Path("update.exe"),
        runner=runner,
        trusted_publishers=frozenset({"CN=Example Publisher"}),
    )

    assert valid is True
    assert detail == "CN=Example Publisher"
    assert calls[0][1]["env"][update_installer._INSTALLER_PATH_ENV_VAR] == (
        "update.exe"
    )


def test_verify_windows_publisher_signature_rejects_unpinned_publisher(monkeypatch):
    monkeypatch.setattr(update_installer.sys, "platform", "win32")
    valid, detail = verify_windows_publisher_signature(
        Path("update.exe"),
        runner=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="Valid\nCN=Unexpected Publisher\n",
        ),
    )

    assert valid is False
    assert "does not trust" in detail


def test_verify_windows_publisher_signature_rejects_unsigned(monkeypatch):
    monkeypatch.setattr(update_installer.sys, "platform", "win32")
    valid, detail = verify_windows_publisher_signature(
        Path("update.exe"),
        runner=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="NotSigned\n",
        ),
    )

    assert valid is False
    assert "NotSigned" in detail


@pytest.mark.parametrize(
    "folder",
    [
        "a$(hostname)",
        "a$HOME",
        "with space",
        "semi;colon",
        "plain",
    ],
)
def test_the_installer_path_never_reaches_powershell_as_source(monkeypatch, folder):
    """Everything after `-Command` is parsed as PowerShell, including argv tail.

    Passing the path as a trailing token had PowerShell re-parse it in argument
    mode: measured against the real `powershell.exe`, a directory named
    `a$(hostname)` made it execute `hostname` and then look for the installer
    under `aHomeBase`. A path merely containing `$` breaks the check with no
    malice at all, and `$` is legal in a Windows account name. The path must
    therefore travel in the environment, where argument-mode expansion hands it
    over as one opaque string.
    """
    monkeypatch.setattr(update_installer.sys, "platform", "win32")
    installer = Path("C:/base") / folder / "stt_app-win-x64-setup.exe"
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="NotSigned\n")

    verify_windows_publisher_signature(installer, runner=runner)

    command, kwargs = calls[0]
    assert kwargs["env"][update_installer._INSTALLER_PATH_ENV_VAR] == str(installer)
    for token in command:
        assert str(installer) not in token, (
            f"the path is still a command token, which PowerShell re-parses: {token!r}"
        )
        assert folder not in token, (
            f"a path component leaked into the command text: {token!r}"
        )
    # The environment must be inherited, not replaced: a bare two-entry env
    # loses SystemRoot and PowerShell refuses to start.
    assert len(kwargs["env"]) > 1
