"""Assert the suite is actually isolated from the developer's model cache.

The isolation existed for a whole round without doing anything, and nothing
noticed, because the environment variable and the value `huggingface_hub`
actually uses are two different things:

`huggingface_hub` computes `constants.HF_HUB_CACHE` and `HF_HUB_OFFLINE` **at
import**. A test module that imports it at module scope is imported during
collection, before any fixture runs, so setting the variables from a fixture
left the constants pointing at `~/.cache/huggingface/hub` with offline off --
while `os.environ` said otherwise. `download_model_snapshot` passes no
`cache_dir` when Model Dir is empty, so it is the *constant* that decides where
a real download lands.

Moving the writes into `pytest_configure` fixed it, and that fix was verified
by hand. This file is what keeps it fixed: it asserts the constants, not the
variables. One ordinary-looking refactor is enough to break it again --
`tests/conftest.py` already imports `stt_app.transcriber.local_faster_whisper`
at module scope, and that module escapes only because its
`from faster_whisper import WhisperModel` sits inside a function.
"""

from __future__ import annotations

import os
from pathlib import Path

# **Module scope, deliberately.** This is the whole mechanism under test:
# pytest imports test modules during collection, and `huggingface_hub` freezes
# its cache constants at import. Importing it inside the test functions instead
# would import it *after* the fixtures have run, so the constants would be
# correct even with the broken fixture-based isolation -- the test would pass
# either way when this file is run on its own. `test_modelscope_mirror.py`
# imports it at module scope too, which is what made the original defect real.
from huggingface_hub import constants


def _under(child: str, parent: str) -> bool:
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
    except (ValueError, OSError):
        return False
    return True


def test_hugging_face_resolved_its_cache_constants_from_the_isolated_environment():
    """The constants, not `os.environ` -- the two disagreed for a whole round."""
    expected_root = os.environ.get("HF_HOME")
    assert expected_root, "pytest_configure did not set HF_HOME"

    real_cache = Path.home() / ".cache" / "huggingface"
    assert not _under(constants.HF_HUB_CACHE, str(real_cache)), (
        f"huggingface_hub froze its cache at the developer's real one "
        f"({constants.HF_HUB_CACHE}). Something imported huggingface_hub "
        "before pytest_configure ran."
    )
    assert _under(constants.HF_HUB_CACHE, expected_root), (
        f"HF_HUB_CACHE is {constants.HF_HUB_CACHE}, expected it under "
        f"{expected_root}"
    )


def test_hugging_face_is_in_offline_mode_for_the_whole_suite():
    """A download that escapes the stub must fail fast, not fetch 486 MB."""
    assert constants.HF_HUB_OFFLINE is True, (
        "HF_HUB_OFFLINE was read before pytest_configure set it, so any "
        "download that escapes the pre-fetch stub reaches the network"
    )


def test_the_modelscope_mirror_is_disabled_for_the_whole_suite():
    assert os.environ.get("STT_APP_DISABLE_MODELSCOPE") == "1"


def test_the_isolated_cache_directory_exists_and_is_not_shared_with_the_user():
    root = Path(os.environ["HF_HOME"])
    assert root.is_dir()
    assert "stt-hf-cache-" in root.name, root
