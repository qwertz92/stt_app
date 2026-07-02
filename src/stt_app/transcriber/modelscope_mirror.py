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
* downloads them (resumable, size-verified) into either
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
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

# ModelScope is API-compatible enough for our narrow needs. The endpoint can be
# overridden for testing or if a different mirror host becomes preferable.
MODELSCOPE_ENDPOINT = os.environ.get(
    "STT_APP_MODELSCOPE_ENDPOINT", "https://www.modelscope.cn"
).rstrip("/")

# ModelScope's default branch is "master" (not "main").
DEFAULT_REVISION = "master"

_CHUNK_BYTES = 1 << 20

ProgressCallback = Callable[[str], None]


class ModelScopeError(RuntimeError):
    """Raised when a ModelScope listing or download cannot be completed."""


def modelscope_fallback_enabled() -> bool:
    """Return True unless the user explicitly disabled the ModelScope fallback."""
    flag = os.environ.get("STT_APP_DISABLE_MODELSCOPE", "").strip().lower()
    return flag not in {"1", "true", "yes", "on"}


def _api_files_url(repo_id: str, revision: str) -> str:
    return (
        f"{MODELSCOPE_ENDPOINT}/api/v1/models/{repo_id}"
        f"/repo/files?Revision={revision}&Recursive=true"
    )


def _revisions_url(repo_id: str) -> str:
    return f"{MODELSCOPE_ENDPOINT}/api/v1/models/{repo_id}/revisions"


def _resolve_url(repo_id: str, revision: str, path: str) -> str:
    return f"{MODELSCOPE_ENDPOINT}/models/{repo_id}/resolve/{revision}/{path}"


def _open(url: str, headers: dict[str, str] | None = None, timeout: float = 60):
    request = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 (fixed https host)


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
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return bool(data.get("Success"))


def list_repo_files(
    repo_id: str, revision: str = DEFAULT_REVISION, timeout: float = 30
) -> list[tuple[str, int]]:
    """Return ``[(relative_path, size_bytes), …]`` for every blob in the repo."""
    try:
        with _open(_api_files_url(repo_id, revision), timeout=timeout) as response:
            data = json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ModelScopeError(
            f"ModelScope file listing failed for '{repo_id}': {exc}"
        ) from exc
    if not data.get("Success"):
        raise ModelScopeError(
            f"ModelScope does not host '{repo_id}' at revision '{revision}'."
        )
    files: list[tuple[str, int]] = []
    for entry in data.get("Data", {}).get("Files", []):
        if entry.get("Type") == "blob":
            files.append((str(entry.get("Path", "")), int(entry.get("Size", 0) or 0)))
    if not files:
        raise ModelScopeError(f"ModelScope repo '{repo_id}' returned no files.")
    return files


def _matches(path: str, allow_patterns: tuple[str, ...] | list[str] | None) -> bool:
    if not allow_patterns:
        return True
    return any(fnmatch.fnmatch(path, pattern) for pattern in allow_patterns)


def _download_file(
    repo_id: str,
    revision: str,
    path: str,
    dest: Path,
    size: int,
    progress: ProgressCallback | None = None,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = dest.stat().st_size if dest.exists() else 0
    if size and have == size:
        return  # already complete
    headers: dict[str, str] = {}
    mode = "wb"
    if 0 < have < size:
        headers["Range"] = f"bytes={have}-"
        mode = "ab"
    elif have > size:
        have = 0  # corrupt/oversized – restart

    url = _resolve_url(repo_id, revision, path)
    try:
        with _open(url, headers=headers, timeout=120) as response, open(
            dest, mode
        ) as handle:
            while True:
                chunk = response.read(_CHUNK_BYTES)
                if not chunk:
                    break
                handle.write(chunk)
    except (urllib.error.URLError, OSError) as exc:
        raise ModelScopeError(
            f"ModelScope download failed for '{path}': {exc}"
        ) from exc

    if size:
        final = dest.stat().st_size
        if final != size:
            raise ModelScopeError(
                f"ModelScope size mismatch for '{path}': got {final}, want {size}."
            )


def _select_files(
    repo_id: str,
    revision: str,
    allow_patterns: tuple[str, ...] | list[str] | None,
) -> list[tuple[str, int]]:
    files = [
        (path, size)
        for (path, size) in list_repo_files(repo_id, revision)
        if _matches(path, allow_patterns)
    ]
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
    for path, size in _select_files(repo_id, revision, allow_patterns):
        if progress is not None:
            progress(f"ModelScope {repo_id}: {path}")
        _download_file(repo_id, revision, path, dest / path, size, progress)
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
    snapshot = root / "snapshots" / revision
    for path, size in _select_files(repo_id, revision, allow_patterns):
        if progress is not None:
            progress(f"ModelScope {repo_id}: {path}")
        _download_file(repo_id, revision, path, snapshot / path, size, progress)
    refs = root / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    (refs / "main").write_text(revision, encoding="utf-8")
    return str(snapshot)
