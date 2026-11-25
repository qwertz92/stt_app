"""Which engines `release_check_providers.py` calls, and with which settings.

Pure standard-library helpers, kept apart from the script so that a unit test
can import them: the script itself redirects `APPDATA` and creates a sandbox
the moment it is imported, which a test must never do.
"""

from __future__ import annotations

import json
from pathlib import Path

# The only fields read from the user's real settings file. Regions and the
# Azure and custom endpoints decide WHERE a request goes, so a check against
# the wrong host proves nothing; every one of them is a plain non-secret
# string. Keys are never in `settings.json` (they live in the credential
# manager), and the custom key command is deliberately not read: it would run
# a command of the user's.
REAL_SETTINGS_FIELDS = (
    "assemblyai_region",
    "deepgram_region",
    "speechmatics_region",
    "azure_endpoint",
    "custom_endpoint",
    "custom_model",
    "custom_api_mode",
)


def real_settings_fields(
    appdata: str | None, app_folder_names: tuple[str, ...]
) -> dict[str, str]:
    """The non-secret routing fields of the user's saved settings, read-only.

    `appdata` is the real `APPDATA` as it was before the sandbox redirect.
    Nothing is created, moved or written (the app's own path helpers are not
    used because they migrate a legacy folder). A missing, unreadable or
    malformed file gives an empty dict, so the check then runs with the app's
    defaults.
    """
    if not appdata:
        return {}
    for name in app_folder_names:
        path = Path(appdata) / name / "settings.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            field: data[field].strip()
            for field in REAL_SETTINGS_FIELDS
            if isinstance(data.get(field), str) and data[field].strip()
        }
    return {}


def skip_reason(engine: str, has_key: bool, fields: dict[str, str]) -> str | None:
    """Why this engine cannot be called, or `None` when it can."""
    if not has_key:
        return (
            f"no {engine} key in the Windows credential manager; "
            "store one in Settings > Providers"
        )
    if engine == "azure" and not fields.get("azure_endpoint"):
        return "no Azure endpoint in the saved settings (Settings > Providers)"
    if engine == "custom" and not fields.get("custom_endpoint"):
        return "no custom endpoint base URL in the saved settings"
    return None


def select_engines(
    requested: list[str] | None, available: tuple[str, ...]
) -> list[str]:
    """The engines of this run, in the app's own order.

    No `--engine` means every one of `available`. An unknown name is an
    error, never a silent no-op, so a typo cannot pass as "nothing failed".
    """
    if not requested:
        return list(available)
    wanted = {name.strip().lower() for name in requested}
    unknown = sorted(wanted - set(available))
    if unknown:
        raise ValueError(
            f"unknown engine {', '.join(unknown)}; choose from {', '.join(available)}"
        )
    return [engine for engine in available if engine in wanted]
