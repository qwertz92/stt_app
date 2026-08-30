"""ModelScope mirror fallback for model downloads.

Some corporate proxies (e.g. Zscaler) block Hugging Face wholesale under a
"Generative AI and ML Applications" category rule. When that happens every
Hugging Face download fails before a single byte arrives, and the app cannot
fetch local transcription models at all.

`modelscope.cn` (Alibaba's ModelScope) mirrors the same `onnx-community/…`,
`Systran/…` and related repositories under identical repo IDs and serves the
large LFS weights from its own CDN instead of redirecting back to Hugging Face.
It is a separate domain that is typically not caught by the same category
block, so it works as a drop-in fallback source.

This module implements a minimal, dependency-free downloader (stdlib only) that:

* lists a repo's files through the ModelScope API,
* filters them with the same ``allow_patterns`` the Hugging Face paths use,
* downloads them (resumable, size- and checksum-verified) into either
  - a flat repo folder (ONNX / Nemotron models, which the app loads from a
    plain directory), or
  - the Hugging Face hub cache ``models--<repo>/snapshots/<rev>`` layout
    (faster-whisper models, which are loaded through ``huggingface_hub``).

The public entry points are :func:`download_repo_to_dir`,
:func:`download_faster_whisper_to_cache`, :func:`repo_available` and
:func:`list_repo_files`.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath

# ModelScope is API-compatible enough for our narrow needs. The endpoint can be
# overridden for testing or if a different mirror host becomes preferable.
MODELSCOPE_ENDPOINT = os.environ.get(
    "STT_APP_MODELSCOPE_ENDPOINT", "https://www.modelscope.cn"
).rstrip("/")

# ModelScope's default branch is "master" (not "main").
DEFAULT_REVISION = "master"

_CHUNK_BYTES = 1 << 20

# Partial transfers get a suffix of our own. ``huggingface_hub`` uses
# ``.incomplete`` for the very same destinations, and resuming one of those as
# if it were ours corrupts the result -- see ``_discard_foreign_partials``.
_PARTIAL_SUFFIX = ".ms-part"
_FOREIGN_PARTIAL_SUFFIXES = (".incomplete",)

ProgressCallback = Callable[[str], None]


class ModelScopeError(RuntimeError):
    """Raised when a ModelScope listing or download cannot be completed."""


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects that would downgrade a model download to plaintext."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() != "https":
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                "ModelScope redirected to a non-HTTPS URL.",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_HTTPS_OPENER = urllib.request.build_opener(_HttpsOnlyRedirectHandler())
_CONTENT_RANGE_PATTERN = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)


def modelscope_fallback_enabled() -> bool:
    """Return True unless the user explicitly disabled the ModelScope fallback."""
    flag = os.environ.get("STT_APP_DISABLE_MODELSCOPE", "").strip().lower()
    return flag not in {"1", "true", "yes", "on"}


def _validated_endpoint() -> str:
    endpoint = str(MODELSCOPE_ENDPOINT or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ModelScopeError(
            "ModelScope endpoint must be an HTTPS URL without credentials, "
            "a query, or a fragment."
        )
    return endpoint


def _validated_revision(revision: str) -> str:
    value = str(revision or "").strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ModelScopeError(f"Unsafe ModelScope revision '{revision}'.")
    return value


def _validated_repo_path(path: str) -> PurePosixPath:
    """Return a normalized repository-relative path or reject it.

    ModelScope's file listing is remote input. Repository paths use POSIX
    separators, so accepting Windows separators would make traversal checks
    platform-dependent on the Windows production build.
    """
    value = str(path or "")
    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(value)
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or value.startswith("/")
        or windows_path.drive
        or windows_path.root
        or posix_path.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or posix_path.as_posix() != value
    ):
        raise ModelScopeError(f"Unsafe ModelScope repository path '{value}'.")
    return posix_path


def _contained_destination(root: Path, repo_path: str) -> Path:
    relative = _validated_repo_path(repo_path)
    resolved_root = root.resolve(strict=False)
    candidate = resolved_root.joinpath(*relative.parts).resolve(strict=False)
    if not candidate.is_relative_to(resolved_root):
        raise ModelScopeError(
            f"ModelScope repository path escapes the destination: '{repo_path}'."
        )
    return candidate


def _api_files_url(repo_id: str, revision: str) -> str:
    endpoint = _validated_endpoint()
    encoded_repo = urllib.parse.quote(repo_id, safe="/")
    query = urllib.parse.urlencode(
        {"Revision": _validated_revision(revision), "Recursive": "true"},
        quote_via=urllib.parse.quote,
    )
    return f"{endpoint}/api/v1/models/{encoded_repo}/repo/files?{query}"


def _revisions_url(repo_id: str) -> str:
    endpoint = _validated_endpoint()
    encoded_repo = urllib.parse.quote(repo_id, safe="/")
    return f"{endpoint}/api/v1/models/{encoded_repo}/revisions"


def _resolve_url(repo_id: str, revision: str, path: str) -> str:
    endpoint = _validated_endpoint()
    encoded_repo = urllib.parse.quote(repo_id, safe="/")
    encoded_revision = urllib.parse.quote(_validated_revision(revision), safe="")
    safe_path = _validated_repo_path(path)
    encoded_path = "/".join(
        urllib.parse.quote(part, safe="") for part in safe_path.parts
    )
    return f"{endpoint}/models/{encoded_repo}/resolve/{encoded_revision}/{encoded_path}"


def _open(url: str, headers: dict[str, str] | None = None, timeout: float = 60):
    if urllib.parse.urlsplit(url).scheme.lower() != "https":
        raise ModelScopeError("ModelScope requests require HTTPS.")
    request = urllib.request.Request(url, headers=headers or {})
    return _HTTPS_OPENER.open(request, timeout=timeout)


def repo_available(
    repo_id: str, revision: str = DEFAULT_REVISION, timeout: float = 15
) -> bool:
    """Best-effort check whether ModelScope hosts ``repo_id``.

    Never raises; returns False on any network or parsing error so callers can
    use it as a cheap guard before attempting a fallback download.
    """
    try:
        with _open(_revisions_url(repo_id), timeout=timeout) as response:
            data = json.load(response)
    except (ModelScopeError, urllib.error.URLError, OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("Success"))


def list_repo_files(
    repo_id: str, revision: str = DEFAULT_REVISION, timeout: float = 30
) -> list[tuple[str, int, str | None]]:
    """Return ``[(relative_path, size_bytes, sha256_or_None), …]`` for the repo.

    The checksum is what lets a download prove it transferred the bytes it
    meant to; ModelScope omits it for some entries, hence the ``None``.
    """
    try:
        with _open(_api_files_url(repo_id, revision), timeout=timeout) as response:
            data = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ModelScopeError(
            f"ModelScope file listing failed for '{repo_id}': {exc}"
        ) from exc
    if not isinstance(data, dict) or not data.get("Success"):
        raise ModelScopeError(
            f"ModelScope does not host '{repo_id}' at revision '{revision}'."
        )
    data_payload = data.get("Data", {})
    entries = data_payload.get("Files", []) if isinstance(data_payload, dict) else []
    if not isinstance(entries, list):
        raise ModelScopeError(
            f"ModelScope repo '{repo_id}' returned an invalid file listing."
        )
    files: list[tuple[str, int, str | None]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("Type") != "blob":
            continue
        raw_path = entry.get("Path")
        if not isinstance(raw_path, str):
            raise ModelScopeError(
                f"ModelScope repo '{repo_id}' returned an invalid file path."
            )
        path = _validated_repo_path(raw_path).as_posix()
        try:
            size = int(entry.get("Size", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ModelScopeError(
                f"ModelScope returned an invalid size for '{path}'."
            ) from exc
        if size < 0:
            raise ModelScopeError(f"ModelScope returned a negative size for '{path}'.")
        files.append((path, size, _normalized_sha256(entry.get("Sha256"))))
    if not files:
        raise ModelScopeError(f"ModelScope repo '{repo_id}' returned no files.")
    return files


def _matches(path: str, allow_patterns: tuple[str, ...] | list[str] | None) -> bool:
    if not allow_patterns:
        return True
    return any(fnmatch.fnmatch(path, pattern) for pattern in allow_patterns)


def _normalized_sha256(raw: object) -> str | None:
    """Return a lowercase hex SHA-256, or ``None`` when ModelScope omits it."""
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        return None
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _discard_foreign_partials(dest: Path) -> None:
    """Remove partial files this module did not create.

    ``huggingface_hub`` parks its own aborted downloads next to the destination
    as ``<name>.incomplete``. That is the same place -- and, historically, the
    same name -- this module used, so a Hugging Face download that a corporate
    proxy cut short used to be picked up here as a resumable ModelScope
    transfer. The mirror then issued a ranged request and *appended* its bytes
    to somebody else's prefix, producing a file of exactly the right length
    whose contents were two different downloads glued together. Size alone
    cannot detect that, which is why the checksum below exists and why these
    files are deleted rather than resumed.
    """
    for suffix in _FOREIGN_PARTIAL_SUFFIXES:
        candidate = dest.with_name(f"{dest.name}{suffix}")
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            continue


def _download_file(
    repo_id: str,
    revision: str,
    path: str,
    dest: Path,
    size: int,
    progress: ProgressCallback | None = None,
    sha256: str | None = None,
) -> None:
    _validated_repo_path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _discard_foreign_partials(dest)
    incomplete = dest.with_name(f"{dest.name}{_PARTIAL_SUFFIX}")
    expected_size = int(size)
    if expected_size < 0:
        raise ModelScopeError(f"ModelScope returned a negative size for '{path}'.")

    try:
        if dest.exists():
            final_size = dest.stat().st_size
            if final_size == expected_size:
                # A published file of the right length is not proof of a clean
                # one: the append bug this module now guards against produced
                # exactly that. Re-verify when a digest is available so an
                # already-corrupt file heals instead of being trusted forever.
                if sha256 and _file_sha256(dest) != sha256:
                    dest.unlink()
                else:
                    incomplete.unlink(missing_ok=True)
                    return
            elif (
                sha256
                and final_size < expected_size
                and not incomplete.exists()
            ):
                # An older version of this module published its partials at
                # the final name, so a short file there may be a resumable
                # prefix of ours. It may equally be somebody else's truncated
                # file: unlike a `.ms-part`, nothing about the final name says
                # we wrote it, because we only ever publish complete, verified
                # files there. Appending to a foreign prefix yields a file of
                # exactly the right length holding two different downloads --
                # the corruption `_discard_foreign_partials` exists for -- and
                # only the digest can detect it. So adopt it when ModelScope
                # published a digest for this entry, and start from zero when
                # it did not (it omits one for some entries).
                os.replace(dest, incomplete)
            else:
                dest.unlink()

        have = incomplete.stat().st_size if incomplete.exists() else 0
        # A partial carrying our own suffix was written by this module, so
        # resuming it is sound even when ModelScope publishes no digest for the
        # file. Refusing to resume without one would throw away multi-GB
        # transfers on exactly the flaky, proxied links that need resume most;
        # the corruption this module guards against came from resuming a
        # *foreign* prefix, which the suffix and _discard_foreign_partials now
        # rule out.
        if have == expected_size:
            _publish_download(incomplete, dest, expected_size, sha256)
            return
        if have > expected_size:
            incomplete.unlink()
            have = 0

        url = _resolve_url(repo_id, revision, path)
        headers = {"Range": f"bytes={have}-"} if have > 0 else {}
        with _open(url, headers=headers, timeout=120) as response:
            mode = "ab" if have > 0 else "wb"
            expected_response_bytes: int | None = None
            if have > 0:
                status = _response_status(response)
                if status == 200:
                    # The server ignored Range and sent the complete object.
                    # Restart through the incomplete file instead of appending.
                    have = 0
                    mode = "wb"
                elif status == 206:
                    expected_response_bytes = _validate_content_range(
                        response,
                        start=have,
                        total=expected_size,
                    )
                else:
                    raise ModelScopeError(
                        f"ModelScope resume for '{path}' returned HTTP {status}."
                    )
            elif _response_status(response) != 200:
                raise ModelScopeError(
                    f"ModelScope download for '{path}' did not return HTTP 200."
                )

            with incomplete.open(mode) as handle:
                written = have
                response_bytes = 0
                while True:
                    chunk = response.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    handle.write(chunk)
                    written += len(chunk)
                    response_bytes += len(chunk)
                    if written > expected_size:
                        raise ModelScopeError(
                            f"ModelScope size mismatch for '{path}': "
                            f"received more than {expected_size} bytes."
                        )
                handle.flush()
                os.fsync(handle.fileno())
            if (
                expected_response_bytes is not None
                and response_bytes != expected_response_bytes
            ):
                with incomplete.open("r+b") as handle:
                    handle.truncate(have)
                    handle.flush()
                    os.fsync(handle.fileno())
                raise ModelScopeError(
                    f"ModelScope resume body for '{path}' does not match Content-Range."
                )
    except ModelScopeError:
        raise
    except (urllib.error.URLError, OSError) as exc:
        raise ModelScopeError(
            f"ModelScope download failed for '{path}': {exc}"
        ) from exc

    final_size = incomplete.stat().st_size
    if final_size != expected_size:
        raise ModelScopeError(
            f"ModelScope size mismatch for '{path}': "
            f"got {final_size}, want {expected_size}."
        )
    _publish_download(incomplete, dest, expected_size, sha256)


def _response_status(response) -> int:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        status = getcode() if callable(getcode) else 200
    return int(status)


def _response_header(response, name: str) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    return str(getter(name, "") if callable(getter) else "").strip()


def _validate_content_range(response, *, start: int, total: int) -> int:
    raw = _response_header(response, "Content-Range")
    match = _CONTENT_RANGE_PATTERN.fullmatch(raw)
    if match is None:
        raise ModelScopeError("ModelScope resume response has no valid Content-Range.")
    actual_start, actual_end = int(match.group(1)), int(match.group(2))
    actual_total = match.group(3)
    if (
        actual_start != start
        or actual_end < actual_start
        or actual_end >= total
        or actual_total == "*"
        or int(actual_total) != total
    ):
        raise ModelScopeError(
            "ModelScope resume response does not match the requested byte range."
        )
    return actual_end - actual_start + 1


def _publish_download(
    incomplete: Path,
    dest: Path,
    expected_size: int,
    sha256: str | None = None,
) -> None:
    if incomplete.stat().st_size != expected_size:
        raise ModelScopeError(
            f"Refusing to publish an incomplete ModelScope download for '{dest.name}'."
        )
    # Size alone cannot tell a clean transfer from a resumed one that glued two
    # different sources together, so verify the checksum whenever ModelScope
    # published one. A corrupt file is deleted rather than kept, otherwise the
    # next run would "resume" it and reproduce the same broken result.
    if sha256:
        actual = _file_sha256(incomplete)
        if actual != sha256:
            incomplete.unlink(missing_ok=True)
            raise ModelScopeError(
                f"ModelScope checksum mismatch for '{dest.name}': "
                f"got {actual}, want {sha256}."
            )
    # Windows requires a writable descriptor for ``FlushFileBuffers``.
    with incomplete.open("r+b") as handle:
        os.fsync(handle.fileno())
    if incomplete.stat().st_size != expected_size:
        raise ModelScopeError(
            f"ModelScope download changed before publication for '{dest.name}'."
        )
    os.replace(incomplete, dest)
    _fsync_directory(dest.parent)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _select_files(
    repo_id: str,
    revision: str,
    allow_patterns: tuple[str, ...] | list[str] | None,
) -> list[tuple[str, int, str | None]]:
    files: list[tuple[str, int, str | None]] = []
    for entry in list_repo_files(repo_id, revision):
        # Older callers (and the tests) hand back ``(path, size)`` pairs; the
        # live API also carries a checksum. Accept both shapes.
        path, size = entry[0], entry[1]
        digest = _normalized_sha256(entry[2]) if len(entry) > 2 else None
        safe_path = _validated_repo_path(path).as_posix()
        if _matches(safe_path, allow_patterns):
            files.append((safe_path, int(size), digest))
    if not files:
        raise ModelScopeError(
            f"No files in '{repo_id}' matched the requested patterns."
        )
    # Smallest first: config/tokenizer land before multi-GB weights, so an
    # interrupted run leaves the cheap metadata in place for a quick resume.
    return sorted(files, key=lambda item: item[1])


def download_repo_to_dir(
    repo_id: str,
    dest_dir: str | Path,
    allow_patterns: tuple[str, ...] | list[str] | None = None,
    revision: str = DEFAULT_REVISION,
    progress: ProgressCallback | None = None,
) -> str:
    """Flat mirror used for ONNX / Nemotron models.

    Downloads every matching file into ``dest_dir`` preserving its relative
    path (e.g. ``onnx/audio_encoder_q4.onnx_data``). Returns ``dest_dir``.
    """
    dest = Path(dest_dir)
    for path, size, digest in _select_files(repo_id, revision, allow_patterns):
        if progress is not None:
            progress(f"ModelScope {repo_id}: {path}")
        target = _contained_destination(dest, path)
        _download_file(repo_id, revision, path, target, size, progress, digest)
    return str(dest)


def download_faster_whisper_to_cache(
    repo_id: str,
    cache_dir: str | Path,
    allow_patterns: tuple[str, ...] | list[str] | None = None,
    revision: str = DEFAULT_REVISION,
    progress: ProgressCallback | None = None,
) -> str:
    """Mirror a faster-whisper repo into the Hugging Face hub cache layout.

    Produces ``models--<org>--<name>/snapshots/<revision>/…`` plus a
    ``refs/main`` pointer so ``huggingface_hub`` (and therefore faster-whisper
    with ``local_files_only=True``) resolves it exactly like a real download.
    Returns the snapshot directory path.
    """
    folder = "models--" + repo_id.replace("/", "--")
    root = Path(cache_dir) / folder
    safe_revision = _validated_revision(revision)
    snapshot = root / "snapshots" / safe_revision
    for path, size, digest in _select_files(repo_id, revision, allow_patterns):
        if progress is not None:
            progress(f"ModelScope {repo_id}: {path}")
        target = _contained_destination(snapshot, path)
        _download_file(repo_id, revision, path, target, size, progress, digest)
    refs = root / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    ref_path = refs / "main"
    ref_incomplete = refs / "main.incomplete"
    with ref_incomplete.open("w", encoding="utf-8") as handle:
        handle.write(safe_revision)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(ref_incomplete, ref_path)
    _fsync_directory(refs)
    return str(snapshot)
