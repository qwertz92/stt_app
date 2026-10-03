"""`scripts/diagnose_paste_target.py`: its argument checks and missing readings."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "diagnose_paste_target.py"


def _load():
    spec = importlib.util.spec_from_file_location("diagnose_paste_target", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_negative_interval_is_refused_with_a_clear_message(capsys):
    """`time.sleep(-1)` raised ValueError after the first reading."""
    script = _load()
    with pytest.raises(SystemExit) as exited:
        script.main(["--interval", "-1"])
    assert exited.value.code == 2
    assert "--interval: must not be negative" in capsys.readouterr().err


class _Check:
    def __init__(self, *, accept):
        self.accept = accept

    def request(self, callback):
        return self.accept


def test_a_missing_reading_names_its_own_reason(monkeypatch):
    """A refused request and a check that never answered are different
    failures; both were printed as "the previous check is still running"."""
    script = _load()
    monkeypatch.setattr(script, "READING_TIMEOUT_SECONDS", 0.01)

    _reading, _ms, refused = script._read_once(_Check(accept=False))
    _reading, _ms, unanswered = script._read_once(_Check(accept=True))

    assert refused == "no answer (the previous check is still running)"
    assert unanswered == "no answer within 0.01 s"
