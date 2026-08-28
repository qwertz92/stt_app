"""`scripts/smoke_test.py` had no tests at all.

It is the script a user runs when the app misbehaves, so it sees broken
installs by definition, and two rounds of review found it writing to exactly
the configuration it was asked to diagnose: `SettingsStore.load` rewrote or
quarantined the settings file, and the *path lookup* underneath it renamed a
legacy install's whole data folder. Both are silent when they succeed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    path = PROJECT_ROOT / "scripts" / "smoke_test.py"
    spec = importlib.util.spec_from_file_location("smoke_test_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def smoke_test():
    return _load_script()


def _tree(root: Path) -> list[tuple[str, bytes]]:
    return sorted(
        (p.relative_to(root).as_posix(), p.read_bytes() if p.is_file() else b"<dir>")
        for p in root.rglob("*")
    )


def test_reading_the_settings_leaves_a_legacy_install_exactly_where_it_was(
    smoke_test, monkeypatch, tmp_path
):
    """The whole data folder used to move, not just the file.

    `settings_path()` -> `appdata_root()` renames `tts_app` to `stt_app`, so
    merely asking where the settings live migrated the user's settings,
    history and recordings.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    legacy = tmp_path / "tts_app"
    legacy.mkdir()
    (legacy / "settings.json").write_text(
        json.dumps({"engine": "local", "model_size": "tiny"}), encoding="utf-8"
    )
    (legacy / "transcript_history.json").write_text("[]", encoding="utf-8")
    before = _tree(tmp_path)

    settings, problem = smoke_test._read_settings_without_touching_them()

    assert problem is None
    assert settings is not None
    assert settings.model_size == "tiny", "it must read the real configuration"
    assert _tree(tmp_path) == before
    assert (tmp_path / "stt_app").exists() is False


def test_reading_the_settings_creates_nothing_on_a_fresh_machine(
    smoke_test, monkeypatch, tmp_path
):
    # Its own directory, because the autouse sandbox puts a home directory in
    # `tmp_path` and this asserts on an empty tree.
    appdata = tmp_path / "appdata-root"
    appdata.mkdir()
    monkeypatch.setenv("APPDATA", str(appdata))

    settings, problem = smoke_test._read_settings_without_touching_them()

    assert (settings, problem) == (None, None)
    assert list(appdata.iterdir()) == []


def test_a_corrupt_settings_file_is_reported_and_left_alone(smoke_test, monkeypatch, tmp_path):
    """Silence here is worse than the quarantine it replaced.

    Loading a throwaway copy repairs the copy and returns defaults, so the
    script would report a clean install and then call the default model "the
    configured local model" -- offering to download 670 MB the user may not
    use. The real file must survive untouched *and* the problem must surface.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    folder = tmp_path / "stt_app"
    folder.mkdir()
    broken = folder / "settings.json"
    broken.write_text("{not json", encoding="utf-8")
    before = _tree(tmp_path)

    settings, problem = smoke_test._read_settings_without_touching_them()

    assert settings is None
    assert problem and "cannot be read" in problem
    assert _tree(tmp_path) == before, "the real settings file was rewritten or quarantined"


def test_valid_settings_that_need_normalizing_are_not_rewritten(
    smoke_test, monkeypatch, tmp_path
):
    """`SettingsStore.load` rewrites whenever the stored payload is not the
    normalized one, which a partial file always is."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    folder = tmp_path / "stt_app"
    folder.mkdir()
    (folder / "settings.json").write_text('{"engine": "local"}', encoding="utf-8")
    before = _tree(tmp_path)

    settings, problem = smoke_test._read_settings_without_touching_them()

    assert problem is None
    assert settings is not None and settings.engine == "local"
    assert _tree(tmp_path) == before


@pytest.mark.parametrize(
    ("payload", "expect_problem"),
    [
        ('{"engine": "local", "model_size": "tiny"}', False),
        # `SettingsStore` requires a top-level object, so these parse as JSON
        # and are still discarded by the app. The first version of the check
        # asked only "is this JSON", which let all three back onto the
        # silent-defaults path it was written to close.
        ("[]", True),
        ("null", True),
        ("5", True),
        ('"a string"', True),
        ("{not json", True),
    ],
)
def test_only_a_json_object_counts_as_readable_settings(
    smoke_test, monkeypatch, tmp_path, payload, expect_problem
):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    folder = tmp_path / "stt_app"
    folder.mkdir()
    (folder / "settings.json").write_text(payload, encoding="utf-8")

    settings, problem = smoke_test._read_settings_without_touching_them()

    assert (problem is not None) == expect_problem, (settings, problem)
    assert (settings is None) == expect_problem


def test_a_damaged_primary_is_recovered_from_the_backup_and_still_reported(
    smoke_test, monkeypatch, tmp_path
):
    """`SettingsStore.load` falls back to `.bak`, so this install works.

    Reading only the primary declared a working install broken, returned 1
    under `--strict`, and skipped the model check the user asked for -- while
    the app itself starts fine on the recovered settings. The damage is still
    worth naming, so the problem is reported *and* the settings come back.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    folder = tmp_path / "stt_app"
    folder.mkdir()
    (folder / "settings.json").write_text("{not json", encoding="utf-8")
    (folder / "settings.json.bak").write_text(
        json.dumps({"engine": "openai", "model_size": "tiny"}), encoding="utf-8"
    )
    before = _tree(tmp_path)

    settings, problem = smoke_test._read_settings_without_touching_them()

    assert settings is not None and settings.engine == "openai", (settings, problem)
    assert problem and "cannot be read" in problem
    assert _tree(tmp_path) == before


def test_a_fresh_install_still_checks_the_model_it_would_use(
    smoke_test, monkeypatch, tmp_path, capsys
):
    """No settings file is not a reason to check nothing and return 0.

    The app runs on defaults there, so the default model is what this machine
    would load. Skipping it made `--check-model --strict` a no-op on exactly
    the clean install the check exists for.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    loaded: list[str] = []

    class _Transcriber:
        def preload_model(self):
            loaded.append("yes")

    monkeypatch.setattr(sys, "argv", ["smoke_test.py", "--check-model", "--strict"])
    monkeypatch.setattr(
        "stt_app.transcriber.factory.create_transcriber",
        lambda settings, **kwargs: _Transcriber(),
    )

    code = smoke_test.main()

    out = capsys.readouterr().out
    assert loaded == ["yes"], out
    assert "No saved settings yet" in out
    assert code == 0


def test_every_step_line_is_flushed_as_it_is_printed(smoke_test, monkeypatch, tmp_path):
    """A run killed mid-step produced a zero-byte log.

    Redirected stdout is block-buffered on Windows, and the model check can
    wait indefinitely on the machine-wide download lock -- which is exactly
    the situation a user runs a diagnostic in. Without a flush per step the
    log did not even say which step it died in.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path))
    flushes: list[int] = []
    printed: list[str] = []

    class _Stream:
        def write(self, text):
            printed.append(text)
            return len(text)

        def flush(self):
            flushes.append(len(printed))

    monkeypatch.setattr(sys, "argv", ["smoke_test.py"])
    monkeypatch.setattr(sys, "stdout", _Stream())
    smoke_test.main()

    body = "".join(printed)
    steps = [line for line in body.splitlines() if line.startswith("[")]
    assert steps, body
    assert len(flushes) >= len(steps), (
        f"{len(steps)} step lines but only {len(flushes)} flushes: {steps}"
    )
