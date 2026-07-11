from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from . import __version__

GITHUB_RELEASES_URL = "https://github.com/qwertz92/stt_app/releases"
GITHUB_LATEST_RELEASE_API = (
    "https://api.github.com/repos/qwertz92/stt_app/releases/latest"
)

_UrlOpen = Callable[..., Any]
_MAX_RELEASE_RESPONSE_BYTES = 1_000_000
_SEMVER_RE = re.compile(
    r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z.-]+))?$"
)


@dataclass(slots=True)
class UpdateCheckResult:
    current_version: str
    latest_version: str = ""
    latest_tag: str = ""
    release_url: str = GITHUB_RELEASES_URL
    update_available: bool = False
    error: str = ""


def _version_parts(value: str) -> tuple[tuple[int, int, int], tuple] | None:
    match = _SEMVER_RE.fullmatch(str(value or "").strip())
    if match is None:
        return None
    core = tuple(int(part) for part in match.groups()[:3])
    prerelease = match.group(4)
    if prerelease is None:
        return core, (1,)
    identifiers = []
    for identifier in prerelease.split("."):
        if not identifier:
            return None
        identifiers.append(
            (0, int(identifier)) if identifier.isdigit() else (1, identifier.lower())
        )
    return core, (0, *identifiers)


def is_newer_version(latest: str, current: str) -> bool:
    latest_parts = _version_parts(latest)
    current_parts = _version_parts(current)
    if latest_parts is None or current_parts is None:
        return False
    return latest_parts > current_parts


def _trusted_release_url(value: object) -> str:
    candidate = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return GITHUB_RELEASES_URL
    if (
        parsed.scheme.lower() != "https"
        or str(parsed.hostname or "").lower() != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not parsed.path.startswith("/qwertz92/stt_app/releases")
        or parsed.query
        or parsed.fragment
    ):
        return GITHUB_RELEASES_URL
    return urllib.parse.urlunsplit(("https", "github.com", parsed.path, "", ""))


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
        raw = response.read(_MAX_RELEASE_RESPONSE_BYTES + 1)
    if len(raw) > _MAX_RELEASE_RESPONSE_BYTES:
        raise ValueError("GitHub release response exceeded the size limit.")
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
        if _version_parts(latest_tag) is None:
            return UpdateCheckResult(
                current_version=current_version,
                error="GitHub returned an invalid release tag.",
            )
        latest_version = (
            latest_tag[1:] if latest_tag.lower().startswith("v") else latest_tag
        )
        release_url = _trusted_release_url(payload.get("html_url"))
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
