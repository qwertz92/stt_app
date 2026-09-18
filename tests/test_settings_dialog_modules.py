"""Import-hygiene tests for the split settings_dialog modules.

The dialog is composed from mixin siblings; each mixin reaches the facade
lazily to avoid an import cycle. Importing a mixin module *directly* (before the
facade) must therefore succeed. This has to run in a fresh interpreter because
once the facade is imported in-process the cycle is masked.
"""

import subprocess
import sys
from pathlib import Path

import pytest

import stt_app

# Read off the package directory, not written out: the hand-kept list went
# stale twice -- it never gained `settings_dialog_audio`, and then not
# `settings_dialog_hotkeys` either -- and a mixin missing from it is simply not
# checked, which no run can show.
_MIXIN_MODULES = sorted(
    f"stt_app.{path.stem}"
    for path in Path(stt_app.__file__).parent.glob("settings_dialog_*.py")
)


def test_the_mixin_list_found_the_modules():
    # A glob that matches nothing parametrizes nothing and passes.
    assert "stt_app.settings_dialog_helpers" in _MIXIN_MODULES
    assert "stt_app.settings_dialog_hotkeys" in _MIXIN_MODULES
    assert len(_MIXIN_MODULES) >= 10


@pytest.mark.parametrize("module", _MIXIN_MODULES)
def test_mixin_module_imports_cold(module):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        # A bound against a hang, not a speed claim.
        timeout=120,
    )
    assert result.returncode == 0, result.stderr


def test_facade_reexports_patched_names_cold():
    # The six externally-patched functions must resolve on the facade so test
    # monkeypatches of stt_app.settings_dialog.<name> keep working.
    code = (
        "import stt_app.settings_dialog as sd;"
        "names=['run_benchmark_cases','_scan_cached_models',"
        "'start_model_download_process','delete_cached_model',"
        "'estimate_cached_model_bytes','cleanup_incomplete_model_download',"
        "'TranscriptEditDialog','SettingsDialog'];"
        "assert all(hasattr(sd,n) for n in names), "
        "[n for n in names if not hasattr(sd,n)]"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
