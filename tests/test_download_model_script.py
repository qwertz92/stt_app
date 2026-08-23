"""The standalone download script must not delete another process's bytes."""

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load_download_model():
    """Import the script as a real module.

    `runpy.run_path` returns a *copy* of the executed namespace, so
    patching that dict does not reach the functions' own globals and the
    real implementation runs regardless.
    """
    spec = importlib.util.spec_from_file_location(
        "_stt_download_model_script", _SCRIPTS / "download_model.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(_SCRIPTS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(_SCRIPTS))
    return module


def test_cancelling_while_waiting_for_the_slot_deletes_nothing(monkeypatch):
    """Ctrl+C during the cross-process wait must leave partials alone.

    The wait exists because another process owns the cache directory, so
    the incomplete files in it are that process's resume point. Deleting
    them makes a multi-gigabyte download the app has queued restart from
    zero -- the same failure the coordinator's explicit-interest rule was
    written to prevent inside one process.
    """
    module = _load_download_model()
    download_model = module.download_model
    cleaned = []

    def _never_gets_the_slot(model_name, model_dir, download, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "run_coordinated_download", _never_gets_the_slot)
    monkeypatch.setattr(
        module,
        "cleanup_incomplete_model_download",
        lambda *a, **k: cleaned.append(a) or (0, 0),
    )

    with pytest.raises(SystemExit) as excinfo:
        download_model("small", "D:/models")

    assert excinfo.value.code == 130
    assert cleaned == [], "cancelled while waiting, yet partial files were deleted"


def test_cancelling_our_own_download_still_cleans_up(monkeypatch):
    """The original behaviour must survive: our own partials are ours."""
    module = _load_download_model()
    download_model = module.download_model
    cleaned = []

    def _runs_then_interrupted(model_name, model_dir, download, **kwargs):
        download()

    def _download_interrupted(model_name, model_dir=""):
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "run_coordinated_download", _runs_then_interrupted)
    monkeypatch.setattr(module, "download_model_snapshot", _download_interrupted)
    monkeypatch.setattr(
        module,
        "cleanup_incomplete_model_download",
        lambda *a, **k: cleaned.append(a) or (2, 4096),
    )

    with pytest.raises(SystemExit) as excinfo:
        download_model("small", "D:/models")

    assert excinfo.value.code == 130
    assert cleaned == [("small", "D:/models")]
