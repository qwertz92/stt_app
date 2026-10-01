"""Ending a child process together with the processes it started.

`subprocess.run(timeout=...)` kills the direct child only and then reads its
pipes to the end, so a grandchild that inherited them -- what `wsl.exe -e ...`
or a `.cmd` wrapper starts -- holds the call open for its whole life: a key
command with a 2 s timeout took 15.1 s that way. `run_bounded` is the bounded
form, and `kill_process_tree` the one place that knows how to end a tree.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Sequence

# How long the pipes are read after the tree was killed. A descendant that
# survived the kill (a policy refused it, it left the tree) still holds them,
# and an unbounded read would wait for it to exit.
_DRAIN_AFTER_KILL_S = 2.0


def kill_process_tree(process: subprocess.Popen) -> None:
    """Every road to ending the child and its descendants, failures swallowed.

    On POSIX the child must have been started in a session of its own
    (`start_new_session=True`), which makes its pid the group id `killpg`
    addresses.
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
                timeout=5,
                check=False,
            )
            process.wait(timeout=3.0)
            return
        except Exception:
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3.0)
            return
        except Exception:
            try:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3.0)
                return
            except Exception:
                pass
    try:
        process.terminate()
        process.wait(timeout=3.0)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=3.0)
        except Exception:
            pass


def run_bounded(
    arguments: Sequence[str],
    *,
    timeout: float,
    **popen_kwargs,
) -> subprocess.CompletedProcess:
    """`subprocess.run(..., capture_output=True, timeout=...)`, bounded.

    Same result object and the same `subprocess.TimeoutExpired`, but a child
    that outlives `timeout` -- or an interrupt landing while it runs -- takes
    its whole process tree with it, and the read after the kill is bounded
    too. Worst case: `timeout` plus the kill (taskkill's 5 s, a 3 s wait) plus
    `_DRAIN_AFTER_KILL_S`.
    """
    if os.name != "nt":
        popen_kwargs.setdefault("start_new_session", True)
    process = subprocess.Popen(
        list(arguments),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **popen_kwargs,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        kill_process_tree(process)
        try:
            process.communicate(timeout=_DRAIN_AFTER_KILL_S)
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(
        list(arguments), process.returncode, stdout, stderr
    )
