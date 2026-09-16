"""The app's default model cache must be the one huggingface_hub downloads into.

With an empty Model Dir the faster-whisper download passes no `cache_dir`, so
the library's own resolution decides where the files land, while the app's
"is it cached" check and the download slot's lock identity ask
`default_hf_cache_dir()`. Two answers mean the model is never found where it
was written and every dictation re-enters the download path.

Each combination runs in its own interpreter: `huggingface_hub` computes
`constants.HF_HUB_CACHE` at import, so one process can answer for exactly one
environment. The child's environment is built from scratch rather than copied,
because `pytest_configure` sets `HF_HOME` and `HF_HUB_CACHE` for this process
and an inherited value would make every combination look like "both set".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import stt_app
from stt_app import model_download_coordinator
from stt_app.transcriber.local_webgpu_asr import default_hf_cache_dir

# What a child process needs on Windows to start Python and reach the venv at
# all. Nothing about Hugging Face survives from this process.
_INHERITED_VARIABLES = (
    "PATH",
    "SYSTEMROOT",
    "PATHEXT",
    "COMSPEC",
    "TEMP",
    "TMP",
    "WINDIR",
)

_WORKER = """
import json, sys
sys.path.insert(0, sys.argv[1])
import huggingface_hub.constants as constants
from stt_app.transcriber import local_faster_whisper, local_webgpu_asr

print(json.dumps({
    "library": constants.HF_HUB_CACHE,
    "webgpu_asr": local_webgpu_asr.default_hf_cache_dir(),
    "faster_whisper": local_faster_whisper.default_hf_cache_dir(),
}))
"""

# One directory each, so a wrong answer names which variable it came from.
_COMBINATIONS = {
    "both set": ("HF_HOME", "HF_HUB_CACHE"),
    "HF_HUB_CACHE only": ("HF_HUB_CACHE",),
    "HF_HOME only": ("HF_HOME",),
    "XDG_CACHE_HOME only": ("XDG_CACHE_HOME",),
    "neither set": (),
}


def _same_directory(left: str, right: str) -> bool:
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(
        os.path.normpath(right)
    )


def _ask_a_child_process(tmp_path: Path, variables: tuple[str, ...]) -> dict[str, str]:
    """Answer the cache question in a fresh interpreter under `variables`."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in _INHERITED_VARIABLES
    }
    environment.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(tmp_path / "appdata"),
            "LOCALAPPDATA": str(tmp_path / "localappdata"),
            "HF_HUB_OFFLINE": "1",
        }
    )
    for name in variables:
        environment[name] = str(tmp_path / name.lower())
    if "HF_HUB_CACHE" in variables:
        # The variable names the hub directory itself, not a root above it.
        environment["HF_HUB_CACHE"] = str(tmp_path / "hf_hub_cache" / "hub")

    source_root = str(Path(stt_app.__file__).resolve().parents[1])
    completed = subprocess.run(
        [sys.executable, "-c", _WORKER, source_root],
        env=environment,
        capture_output=True,
        text=True,
        # A bound against a hang, not a speed claim: the child imports the app
        # and huggingface_hub, which takes about a second here.
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


@pytest.mark.parametrize(
    "combination", sorted(_COMBINATIONS), ids=lambda name: name.replace(" ", "_")
)
def test_the_default_cache_is_the_one_huggingface_hub_resolves(tmp_path, combination):
    """`HF_HUB_CACHE` wins outright, then `HF_HOME`, then `XDG_CACHE_HOME`.

    The app checked `HF_HOME` first and ignored `XDG_CACHE_HOME` entirely, so
    with both variables set it looked in `$HF_HOME/hub` while the library
    downloaded into `$HF_HUB_CACHE`.
    """
    answers = _ask_a_child_process(tmp_path, _COMBINATIONS[combination])

    for caller in ("webgpu_asr", "faster_whisper"):
        assert _same_directory(answers[caller], answers["library"]), (
            f"{combination}: {caller} answered {answers[caller]!r}, "
            f"huggingface_hub resolves {answers['library']!r}"
        )


def test_the_download_lock_identity_is_that_same_directory():
    """The slot must lock the directory the download actually writes into.

    An empty Model Dir means "the default cache", and the lock is keyed on the
    cache directory rather than on the model, so a lock identity derived from a
    second resolution of that default would let two writers into one blob tree.
    """
    expected = os.path.normcase(
        os.path.abspath(os.path.normpath(default_hf_cache_dir()))
    )

    assert model_download_coordinator._cache_lock_resource("") == expected
    assert model_download_coordinator._cache_lock_resource("   ") == expected
