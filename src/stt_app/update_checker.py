from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from . import __version__

GITHUB_RELEASES_URL = "https://github.com/qwertz92/stt_app/releases"
GITHUB_LATEST_RELEASE_API = (
    "https://api.github.com/repos/qwertz92/stt_app/releases/latest"
)

_UrlOpen = Callable[..., Any]


@dataclass(slots=True)
class UpdateCheckResult:
    current_version: str
    latest_version: str = ""
    latest_tag: str = ""
    release_url: str = GITHUB_RELEASES_URL
    update_available: bool = False
    error: str = ""


def _version_parts(value: str) -> tuple[int, ...]:
    normalized = str(value or "").strip().lower()
    if normalized.startswith("v"):
        normalized = normalized[1:]
    parts: list[int] = []
    for part in normalized.split("."):
        digits = ""
        for char in part:
            if not char.isdigit():
                break
            digits += char
        if digits == "":
            break
        parts.append(int(digits))
    return tuple(parts)


def is_newer_version(latest: str, current: str) -> bool:
    latest_parts = _version_parts(latest)
    current_parts = _version_parts(current)
    if not latest_parts or not current_parts:
        return False
    length = max(len(latest_parts), len(current_parts))
    return latest_parts + (0,) * (length - len(latest_parts)) > current_parts + (
        0,
    ) * (length - len(current_parts))


def _latest_release_payload(
    *,
    timeout_s: float,
    urlopen: _UrlOpen,
) -> dict[str, Any]:
    request = urllib.request.Request(
        GITHUB_LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"stt_app/{__version__}",
        },
    )
    with urlopen(request, timeout=timeout_s) as response:
        raw = response.read()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("GitHub returned an unexpected response.")
    return payload


def check_for_updates(
    *,
    current_version: str = __version__,
    timeout_s: float = 5.0,
    urlopen: _UrlOpen = urllib.request.urlopen,
) -> UpdateCheckResult:
    try:
        payload = _latest_release_payload(timeout_s=timeout_s, urlopen=urlopen)
        latest_tag = str(payload.get("tag_name", "")).strip()
        if not latest_tag:
            return UpdateCheckResult(
                current_version=current_version,
                error="GitHub did not return a release tag.",
            )
        latest_version = (
            latest_tag[1:] if latest_tag.lower().startswith("v") else latest_tag
        )
        release_url = str(payload.get("html_url") or GITHUB_RELEASES_URL)
        return UpdateCheckResult(
            current_version=current_version,
            latest_version=latest_version,
            latest_tag=latest_tag,
            release_url=release_url,
            update_available=is_newer_version(latest_version, current_version),
        )
    except urllib.error.URLError as exc:
        return UpdateCheckResult(
            current_version=current_version,
            error=f"Could not reach GitHub: {exc.reason}",
        )
    except Exception as exc:
        return UpdateCheckResult(
            current_version=current_version,
            error=f"Update check failed: {exc}",
        )
