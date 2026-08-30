from __future__ import annotations

import hashlib
import importlib.util
import io
import sys
import zipfile
from pathlib import Path

import pytest


def _load_setup_node_module():
    root = Path(__file__).resolve().parents[1]
    script_path = root / "scripts" / "setup_node_windows.py"
    spec = importlib.util.spec_from_file_location("setup_node_windows", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["setup_node_windows"] = module
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


class _Response(io.BytesIO):
    """Stands in for an `http.client.HTTPResponse`.

    `geturl()` is part of that contract and is what the redirect check reads:
    a response whose final URL left https, or the allowed hosts, is refused.
    """

    def __init__(self, payload: bytes, url: str = "https://nodejs.org/dist/x"):
        super().__init__(payload)
        self._url = url

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_validated_version_rejects_url_and_path_injection():
    module = _load_setup_node_module()

    with pytest.raises(ValueError, match=r"Invalid Node\.js version"):
        module._validated_version("24.18.0/../../payload")


def test_download_verifies_published_checksum(monkeypatch, tmp_path):
    module = _load_setup_node_module()
    archive = _zip_bytes({"node-v24.18.0-win-x64/node.exe": b"node"})
    checksum = hashlib.sha256(archive).hexdigest()

    def fake_urlopen(url, timeout):
        assert timeout in {30, 120}
        if str(url).endswith("SHASUMS256.txt"):
            return _Response(
                f"{checksum}  node-v24.18.0-win-x64.zip\n".encode("ascii"),
                url=str(url),
            )
        return _Response(archive, url=str(url))

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    destination = tmp_path / "node.zip"

    module._download("24.18.0", destination)

    assert destination.read_bytes() == archive


def test_download_rejects_mismatched_checksums_from_all_mirrors(
    monkeypatch, tmp_path
):
    module = _load_setup_node_module()
    archive = b"not-the-published-archive"

    def fake_urlopen(url, timeout):
        assert timeout in {30, 120}
        if str(url).endswith("SHASUMS256.txt"):
            return _Response(
                ("0" * 64 + "  node-v24.18.0-win-x64.zip\n").encode("ascii"),
                url=str(url),
            )
        return _Response(archive, url=str(url))

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    destination = tmp_path / "node.zip"

    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        module._download("24.18.0", destination)

    assert not destination.exists()


def test_safe_extract_rejects_parent_traversal(tmp_path):
    module = _load_setup_node_module()
    archive_path = tmp_path / "malicious.zip"
    archive_path.write_bytes(_zip_bytes({"../outside.txt": b"escaped"}))
    target = tmp_path / "target"

    with (
        zipfile.ZipFile(archive_path) as archive,
        pytest.raises(RuntimeError, match="Unsafe path"),
    ):
        module._extract_zip_safely(archive, target)

    assert not (tmp_path / "outside.txt").exists()


def test_safe_extract_allows_expected_node_layout(tmp_path):
    module = _load_setup_node_module()
    archive_path = tmp_path / "node.zip"
    archive_path.write_bytes(
        _zip_bytes({"node-v24.18.0-win-x64/node.exe": b"node"})
    )
    target = tmp_path / "target"

    with zipfile.ZipFile(archive_path) as archive:
        module._extract_zip_safely(archive, target)

    assert (target / "node-v24.18.0-win-x64" / "node.exe").read_bytes() == b"node"


def test_the_checksum_is_fetched_from_nodejs_org_even_for_a_mirror_archive(
    monkeypatch, tmp_path
):
    """A mirror that supplies both cannot fail its own verification.

    The archive and SHASUMS256.txt used to come from whichever root was being
    tried, so for the npmmirror fallback the mirror vouched for its own bytes,
    which cannot detect a substituted archive from that mirror. Only nodejs.org
    publishes the authoritative list.
    """
    module = _load_setup_node_module()
    archive = _zip_bytes({"node-v24.18.0-win-x64/node.exe": b"node"})
    checksum = hashlib.sha256(archive).hexdigest()
    requested: list[str] = []

    def fake_urlopen(url, timeout):
        url = str(url)
        requested.append(url)
        if url.endswith("SHASUMS256.txt"):
            return _Response(
                f"{checksum}  node-v24.18.0-win-x64.zip\n".encode("ascii"), url=url
            )
        if "nodejs.org" in url:
            raise OSError("blocked by the corporate proxy")
        return _Response(archive, url=url)

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    module._download("24.18.0", tmp_path / "node.zip")

    checksum_urls = [u for u in requested if u.endswith("SHASUMS256.txt")]
    assert checksum_urls, "no checksum was fetched"
    assert all("nodejs.org" in u for u in checksum_urls), checksum_urls
    assert any("npmmirror" in u for u in requested), "the mirror was never used"


def test_a_download_redirected_off_https_is_refused(monkeypatch, tmp_path):
    """`urlopen` follows a redirect to plaintext http without complaint.

    CPython's HTTPRedirectHandler allows http, https, ftp and "", with no host
    restriction, so an https request can silently end up on http elsewhere.
    """
    module = _load_setup_node_module()
    archive = _zip_bytes({"node-v24.18.0-win-x64/node.exe": b"node"})

    def fake_urlopen(url, timeout):
        return _Response(archive, url="http://mirror.example.com/node.zip")

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match=r"Could not download Node\.js"):
        module._download("24.18.0", tmp_path / "node.zip")


def test_allowed_download_urls_cover_the_redirect_cases():
    module = _load_setup_node_module()

    assert module._is_allowed_download_url("https://nodejs.org/dist/v1/x.zip")
    assert module._is_allowed_download_url("https://cdn.npmmirror.com/x.zip")
    assert not module._is_allowed_download_url("http://nodejs.org/dist/v1/x.zip")
    assert not module._is_allowed_download_url("https://nodejs.org.evil.com/x.zip")
    assert not module._is_allowed_download_url("https://evil.com/nodejs.org/x.zip")
    assert not module._is_allowed_download_url("https://user:pw@nodejs.org/x.zip")


def test_a_half_extracted_node_is_not_reported_as_ready(monkeypatch, tmp_path):
    """node.exe alone is not a usable install; the app runs `npm install`.

    An extraction cut short by Ctrl+C, a full disk or an AV quarantine leaves
    node.exe in place with npm missing, and the old check passed it. --force
    could not repair it either: it only skipped the already-available early
    return, while the download guard still saw node.exe and fetched nothing.
    """
    module = _load_setup_node_module()
    node_root = tmp_path / "node-v24.18.0-win-x64"
    node_root.mkdir(parents=True)
    (node_root / "node.exe").write_bytes(b"node")

    assert module._missing_node_files(node_root) == [
        "npm.cmd",
        "npx.cmd",
        "node_modules/npm/bin/npm-cli.js",
    ]

    downloads: list[Path] = []

    def fake_download(version, dest_zip):
        downloads.append(dest_zip)
        dest_zip.write_bytes(
            _zip_bytes(
                {
                    "node-v24.18.0-win-x64/node.exe": b"node",
                    "node-v24.18.0-win-x64/npm.cmd": b"npm",
                    "node-v24.18.0-win-x64/npx.cmd": b"npx",
                    "node-v24.18.0-win-x64/node_modules/npm/bin/npm-cli.js": b"cli",
                }
            )
        )

    monkeypatch.setattr(module, "_download", fake_download)
    monkeypatch.setattr(module, "_existing_node", lambda: None)
    monkeypatch.setattr(module, "_set_node_path_env", lambda _exe: True)

    assert module.install("24.18.0", tmp_path, force=False, skip_ca=True) == 0
    assert downloads, "the incomplete install was accepted instead of repaired"
    assert module._missing_node_files(node_root) == []


def test_force_keeps_the_working_install_when_the_download_fails(
    monkeypatch, tmp_path
):
    """Deleting first turned a repair into a destruction.

    The audience for this script is a machine whose network blocks
    nodejs.org, so a failed download is the expected case -- and `--force`
    removed the tree before fetching, leaving no Node at all and breaking
    the Cohere/Granite runtimes that were working a moment earlier.
    """
    module = _load_setup_node_module()
    node_root = tmp_path / "node-v24.18.0-win-x64"
    (node_root / "node_modules" / "npm" / "bin").mkdir(parents=True)
    for name in ("node.exe", "npm.cmd", "npx.cmd"):
        (node_root / name).write_bytes(name.encode())
    (node_root / "node_modules" / "npm" / "bin" / "npm-cli.js").write_bytes(b"cli")
    assert module._missing_node_files(node_root) == []

    def refuse(_version, _dest_zip):
        raise RuntimeError("Could not download Node.js")

    monkeypatch.setattr(module, "_download", refuse)
    monkeypatch.setattr(module, "_existing_node", lambda: None)
    monkeypatch.setattr(module, "_set_node_path_env", lambda _exe: True)

    with pytest.raises(RuntimeError, match=r"Could not download Node\.js"):
        module.install("24.18.0", tmp_path, force=True, skip_ca=True)

    assert module._missing_node_files(node_root) == [], (
        "the working install was destroyed before a replacement existed"
    )


def test_a_tree_that_cannot_be_removed_is_reported_and_extracted_over(
    monkeypatch, tmp_path, capsys
):
    """`ignore_errors=True` hid a half-deleted tree; the archive supplies all files."""
    module = _load_setup_node_module()
    node_root = tmp_path / "node-v24.18.0-win-x64"
    node_root.mkdir(parents=True)
    (node_root / "node.exe").write_bytes(b"old")

    def fake_download(_version, dest_zip):
        dest_zip.write_bytes(
            _zip_bytes(
                {
                    "node-v24.18.0-win-x64/node.exe": b"new",
                    "node-v24.18.0-win-x64/npm.cmd": b"npm",
                    "node-v24.18.0-win-x64/npx.cmd": b"npx",
                    "node-v24.18.0-win-x64/node_modules/npm/bin/npm-cli.js": b"cli",
                }
            )
        )

    real_rmtree = module.shutil.rmtree

    def refuse_rmtree(path, *args, **kwargs):
        # `module.shutil` is the global module, so `TemporaryDirectory`'s own
        # cleanup comes through here too; only the node tree may refuse.
        if Path(path) == node_root:
            raise OSError("node.exe is in use")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(module, "_download", fake_download)
    monkeypatch.setattr(module.shutil, "rmtree", refuse_rmtree)
    monkeypatch.setattr(module, "_existing_node", lambda: None)
    monkeypatch.setattr(module, "_set_node_path_env", lambda _exe: True)

    assert module.install("24.18.0", tmp_path, force=True, skip_ca=True) == 0

    out = capsys.readouterr().out
    assert "could not remove" in out, out
    assert (node_root / "node.exe").read_bytes() == b"new"
    assert module._missing_node_files(node_root) == []
