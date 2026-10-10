from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _requirement_base(value: str) -> str:
    return value.split(";", 1)[0].strip()


def _pinned_requirements(name: str) -> set[str]:
    return {
        line.strip()
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "-r "))
    }


def _project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_windows_requirements_match_direct_runtime_dependencies():
    project_dependencies = {
        _requirement_base(value) for value in _project()["project"]["dependencies"]
    }

    assert _pinned_requirements("requirements-win.txt") == project_dependencies


def test_windows_dev_requirements_match_the_dev_group():
    # A pip-only machine (uv is blocked on the owner's work PC) installs from
    # these files; ruff stayed at 0.16.10 here after the lock moved to 0.17.0.
    dev_group = {
        _requirement_base(value) for value in _project()["dependency-groups"]["dev"]
    }

    assert _pinned_requirements("requirements-dev-win.txt") == dev_group


def test_installed_av_decodes_a_file_the_way_faster_whisper_opens_it(tmp_path):
    # faster-whisper 1.2.1 requires only av>=11 but calls
    # av.open(..., metadata_errors="ignore"), which av 19.0.x rejects with
    # TypeError. Every file-path transcription goes through this call, so an av
    # bump that breaks it must fail here and not in the user's first dictation.
    import wave

    import numpy as np
    from faster_whisper.audio import decode_audio

    path = tmp_path / "silence.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(np.zeros(1600, dtype="<i2").tobytes())

    assert decode_audio(str(path), sampling_rate=16000).shape == (1600,)
