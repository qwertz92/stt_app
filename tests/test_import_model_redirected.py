"""`import_model.py` run as a real subprocess with its output redirected.

Every other test for this script uses `capsys`, which cannot see either of the
defects here **by construction**:

- `capsys` swaps `sys.stdout` for a UTF-8 in-memory buffer, so the cp1252
  encoder that runs on a redirected Windows pipe never runs. `--validate-only`
  crashed with `UnicodeEncodeError` and exit 1 on a *complete, valid* model,
  and 19 passing tests said the script was fine.
- `capsys` buffers `out` and `err` separately and the assertions concatenate
  them, so the relative order of the two streams is unobservable. Redirected,
  stdout is block-buffered and stderr is not, so stdout lines land *after* the
  errors that refer to them.

Both are exactly the "capture the output and send it to me" situation the
script exists for, so they are tested the way a user would hit them.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "import_model.py"

# `validate_model_files` rejects a `model.bin` below a size floor as an
# incomplete download, so a "complete" fixture needs a real one.
_MODEL_BIN_BYTES = 11_000_000  # just over import_model's 10 MB floor


def _complete_model(folder: Path) -> Path:
    folder.mkdir(parents=True)
    (folder / "config.json").write_text("{}", encoding="utf-8")
    (folder / "tokenizer.json").write_text("{}", encoding="utf-8")
    (folder / "vocabulary.txt").write_text("x\n", encoding="utf-8")
    (folder / "model.bin").write_bytes(b"\0" * _MODEL_BIN_BYTES)
    return folder


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    """Run with stdout and stderr merged into one pipe, as `> log 2>&1` does."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def test_validate_only_on_a_complete_model_does_not_crash_when_redirected(tmp_path):
    """The success path printed U+2713, which cp1252 cannot encode."""
    source = _complete_model(tmp_path / "faster-whisper-small")

    result = _run("--validate-only", "--model", "small", str(source))

    assert "UnicodeEncodeError" not in result.stdout, result.stdout
    assert "Traceback" not in result.stdout, result.stdout
    assert result.returncode == 0, result.stdout
    assert "all required files are present" in result.stdout


def test_validate_only_answers_even_when_the_folder_name_is_unrecognised(tmp_path):
    """The verdict is the question `--validate-only` was asked.

    A complete model under a name this script cannot map printed `Source:` and
    `Found files:` and then exited 1 with only "Could not auto-detect the model
    name" -- never saying whether the files were in fact complete.
    """
    source = _complete_model(tmp_path / "weird-folder-name")

    result = _run("--validate-only", str(source))

    assert "all required files are present" in result.stdout, result.stdout
    assert "Could not auto-detect the model name" in result.stdout
    assert result.returncode == 1


def test_the_model_line_precedes_the_file_complaints_in_a_captured_log(tmp_path):
    """stdout is block-buffered when redirected; stderr is not."""
    source = tmp_path / "faster-whisper-small"
    source.mkdir()
    (source / "config.json").write_text("{}", encoding="utf-8")

    result = _run("--validate-only", str(source))

    out = result.stdout
    assert "Detected model: small" in out, out
    assert "MISSING FILES" in out
    assert out.index("Detected model: small") < out.index("MISSING FILES"), (
        f"the model line landed after the errors that refer to it:\n{out}"
    )
