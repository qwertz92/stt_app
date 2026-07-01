"""Import-hygiene tests for the split settings_dialog modules.

The dialog is composed from mixin siblings; each mixin reaches the facade
lazily to avoid an import cycle. Importing a mixin module *directly* (before the
facade) must therefore succeed. This has to run in a fresh interpreter because
once the facade is imported in-process the cycle is masked.
"""

import subprocess
import sys

import pytest

_MIXIN_MODULES = [
    "stt_app.settings_dialog_helpers",
    "stt_app.settings_dialog_general",
    "stt_app.settings_dialog_local",
    "stt_app.settings_dialog_benchmark",
    "stt_app.settings_dialog_remote",
    "stt_app.settings_dialog_history",
    "stt_app.settings_dialog_import",
    "stt_app.settings_dialog_persistence",
]


@pytest.mark.parametrize("module", _MIXIN_MODULES)
def test_mixin_module_imports_cold(module):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
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
