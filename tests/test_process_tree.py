"""`run_bounded`: real child processes, not stand-ins.

The key command of the custom endpoint is the only caller, and its tests there
drive the same runner through a provider. These pin the runner's own
contract: what a child that exits while a descendant holds its pipes returns.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from stt_app import process_tree
from stt_app.process_tree import run_bounded

# A wrapper that starts its tool with the inherited pipes and exits at once
# (`start /b tool`, `Start-Process` without `-Wait`): the tool prints later.
_WRAPPER = """
import subprocess, sys
subprocess.Popen(
    [sys.executable, "-c", sys.argv[1]], stdout=sys.stdout, stderr=sys.stderr
)
"""


def _run(tool_code: str, *, timeout: float, **kwargs):
    return run_bounded(
        [sys.executable, "-c", _WRAPPER, tool_code],
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        **kwargs,
    )


def test_a_token_printed_after_the_wrapper_exited_is_returned():
    """The wrapper exited with code 0 and printed nothing; the pipes are held
    by the tool it started, which prints a second later. Ending the tree 0.5 s
    after the wrapper's exit made that "printed no token". Output that has not
    arrived yet is waited for; the call returns when the tool closes the pipes
    (review of dedcde4)."""
    started = time.monotonic()

    completed = _run(
        "import time; time.sleep(1.2); print('late-token')",
        timeout=15,
        text=True,
        encoding="utf-8",
    )

    elapsed = time.monotonic() - started
    assert completed.returncode == 0
    assert completed.stdout.strip() == "late-token"
    assert elapsed >= 1.0, "it returned before the tool had printed"


def test_a_silent_descendant_is_waited_for_only_until_the_timeout():
    """What holds the pipes may be a forgotten process that never prints: the
    wait for output is the call's own timeout, not forever, and the descendant
    is ended with the tree."""
    started = time.monotonic()

    with pytest.raises(subprocess.TimeoutExpired):
        _run("import time; time.sleep(60)", timeout=2.0, text=True)

    assert time.monotonic() - started < 12


def test_a_finished_wrapper_with_output_does_not_wait_for_its_descendant(
    monkeypatch,
):
    """The other half: output that has arrived is complete once the wrapper
    exited, and a descendant that merely holds the pipes is ended after the
    grace period instead of failing a token that was already there."""
    monkeypatch.setattr(process_tree, "_PIPES_GRACE_AFTER_EXIT_S", 0.3)
    code = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "stdout=sys.stdout, stderr=sys.stderr); "
        "print('token', flush=True)"
    )
    started = time.monotonic()

    completed = run_bounded(
        [sys.executable, "-c", code],
        timeout=15,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )

    assert completed.stdout.strip() == "token"
    assert completed.returncode == 0
    assert time.monotonic() - started < 8


def test_a_token_survives_a_tree_kill_that_cannot_reach_the_orphan(monkeypatch):
    """Without a job object (a nested job forbids one) `taskkill /T` cannot
    reach a descendant whose parent exited, so the pipes stay open after the
    kill. The token had arrived, so the output collected so far is the result
    rather than a timeout."""

    def _no_job():
        raise OSError("no job object here")

    monkeypatch.setattr(process_tree, "_WindowsJob", _no_job)
    monkeypatch.setattr(process_tree, "_PIPES_GRACE_AFTER_EXIT_S", 0.3)
    monkeypatch.setattr(process_tree, "_DRAIN_AFTER_KILL_S", 0.5)
    # Ends by itself, so the orphan does not outlive the test for long.
    code = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(6)'], "
        "stdout=sys.stdout, stderr=sys.stderr); "
        "print('token', flush=True)"
    )

    completed = run_bounded(
        [sys.executable, "-c", code],
        timeout=15,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )

    assert completed.stdout.strip() == "token"


def test_a_failing_wrapper_is_not_waited_for_its_descendant(monkeypatch):
    """A non-zero exit is an answer already: its message is complete, and
    waiting out the timeout for a descendant would only delay the error."""
    monkeypatch.setattr(process_tree, "_PIPES_GRACE_AFTER_EXIT_S", 0.3)
    code = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "stdout=sys.stdout, stderr=sys.stderr); "
        "sys.stderr.write('login first\\n'); sys.exit(3)"
    )
    started = time.monotonic()

    completed = run_bounded(
        [sys.executable, "-c", code],
        timeout=15,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 3
    assert completed.stderr.strip() == "login first"
    assert time.monotonic() - started < 8


def test_text_and_bytes_results_match_subprocess_run():
    code = "import sys; sys.stdout.buffer.write(b'a\\r\\nb\\n\\xc3\\xa9'); sys.exit(0)"

    as_text = run_bounded(
        [sys.executable, "-c", code],
        timeout=15,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    as_bytes = run_bounded(
        [sys.executable, "-c", code], timeout=15, stdin=subprocess.DEVNULL
    )

    assert as_text.stdout == "a\nb\né"
    assert as_bytes.stdout == b"a\r\nb\n\xc3\xa9"
    assert as_bytes.stderr == b""
