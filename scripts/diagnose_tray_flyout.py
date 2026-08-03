"""Log what happens to the Windows 11 tray overflow flyout during a click.

The flyout ("hidden icons", class ``TopLevelWindowForOverflowXamlIsland``)
closes when this app's tray menu opens, but stays open for some other apps.
Foreground changes alone did not explain it, so this script records a timeline
of the real interaction: which window owns the foreground, and when the flyout
window is destroyed or hidden.

Usage (Windows, from the repository root)::

    python scripts/diagnose_tray_flyout.py

Then, while it runs:
  1. Click the "^" chevron so the hidden-icons flyout opens.
  2. Right-click THIS app's tray icon and move the mouse over the menu.
  3. Press Esc, open the flyout again and right-click another app's icon
     (ChatGPT/Claude) for comparison.

The timeline is printed and written to ``tray_flyout_diagnosis.txt`` in the
system temp directory.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
import tempfile
import time
from pathlib import Path

POLL_SECONDS = 0.03
DEFAULT_DURATION_SECONDS = 40.0
FLYOUT_CLASSES = (
    "TopLevelWindowForOverflowXamlIsland",
    "NotifyIconOverflowWindow",
)
# Menu windows, so the timeline shows whether the flyout closes before or
# after the menu appears: "#32768" is the native Win32 menu, Qt popups carry
# "QWindowPopup" in their class name. Chromium's own menu widgets share the
# generic Chrome_WidgetWin class with ordinary browser windows and are left
# out on purpose — matching them buries the timeline in noise.
MENU_CLASS_FRAGMENTS = ("#32768", "QWindowPopup")

user32 = ctypes.windll.user32


def _window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _window_title(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buffer, 256)
    return buffer.value


GWL_STYLE = -16
GWL_EXSTYLE = -20
GW_OWNER = 4


def _describe(hwnd: int) -> str:
    """Identify a window well enough to compare two implementations.

    Foreground changes alone did not explain the dismissal — the reference
    apps' menus take the foreground too — so the window's styles and its owner
    are logged as well.
    """
    if not hwnd:
        return "<none>"
    style = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
    ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
    owner = user32.GetWindow(hwnd, GW_OWNER)
    owner_text = f"{owner} [{_window_class(owner)}]" if owner else "<none>"
    return (
        f"{hwnd} [{_window_class(hwnd)}] {_window_title(hwnd)[:32]!r} "
        f"style=0x{style:08x} ex=0x{ex_style:08x} owner={owner_text}"
    )


def _find_flyouts() -> list[tuple[int, str, bool]]:
    found: list[tuple[int, str, bool]] = []

    @ctypes.WINFUNCTYPE(
        ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
    )
    def _callback(hwnd, _lparam):
        class_name = _window_class(hwnd)
        if class_name in FLYOUT_CLASSES:
            found.append((hwnd, class_name, bool(user32.IsWindowVisible(hwnd))))
        return True

    user32.EnumWindows(_callback, 0)
    return found


def _find_menus() -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(
        ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
    )
    def _callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        class_name = _window_class(hwnd)
        if any(fragment in class_name for fragment in MENU_CLASS_FRAGMENTS):
            found.append((hwnd, class_name))
        return True

    user32.EnumWindows(_callback, 0)
    return found


def main() -> int:
    if sys.platform != "win32":
        print("This diagnosis only applies to Windows.")
        return 1

    duration = DEFAULT_DURATION_SECONDS
    if len(sys.argv) > 1:
        duration = float(sys.argv[1])

    print(__doc__)
    print(f"Recording for {duration:.0f} seconds — start clicking now.\n")

    started = time.perf_counter()
    timeline: list[str] = []
    last_foreground = None
    last_flyouts: list[tuple[int, str, bool]] = []
    last_menus: list[tuple[int, str]] = []

    while time.perf_counter() - started < duration:
        elapsed = time.perf_counter() - started
        foreground = user32.GetForegroundWindow()
        if foreground != last_foreground:
            timeline.append(f"{elapsed:7.3f}s foreground -> {_describe(foreground)}")
            last_foreground = foreground
        flyouts = _find_flyouts()
        if flyouts != last_flyouts:
            if not flyouts:
                timeline.append(f"{elapsed:7.3f}s flyout    -> gone")
            for hwnd, class_name, visible in flyouts:
                timeline.append(
                    f"{elapsed:7.3f}s flyout    -> {hwnd} [{class_name}] "
                    f"visible={visible}"
                )
            last_flyouts = flyouts
        menus = _find_menus()
        if menus != last_menus:
            if not menus:
                timeline.append(f"{elapsed:7.3f}s menu      -> closed")
            for hwnd, class_name in menus:
                timeline.append(
                    f"{elapsed:7.3f}s menu      -> {hwnd} [{class_name}] shown"
                )
            last_menus = menus
        time.sleep(POLL_SECONDS)

    report = "\n".join(timeline) or "Nothing changed — was the flyout opened?"
    print(report)
    output = Path(tempfile.gettempdir()) / "tray_flyout_diagnosis.txt"
    output.write_text(report + "\n", encoding="utf-8")
    print(f"\nWritten to {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
