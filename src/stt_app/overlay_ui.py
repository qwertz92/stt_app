from __future__ import annotations

import contextlib
import functools
import logging
import sys
from collections.abc import Callable

from PySide6 import QtCore, QtGui, QtWidgets

from . import raw_mouse_input
from .config import (
    DEFAULT_OVERLAY_OPACITY_PERCENT,
    LANGUAGE_MODE_LABELS,
    OVERLAY_COMPACT_DETAIL_MAX_HEIGHT,
    OVERLAY_DETAIL_MIN_HEIGHT,
    OVERLAY_ERROR_ACTION_INSERT,
    OVERLAY_ERROR_ACTION_NONE,
    OVERLAY_HEIGHT,
    OVERLAY_INITIAL_DETAIL,
    OVERLAY_MARGIN_X,
    OVERLAY_MARGIN_Y,
    OVERLAY_MAX_HEIGHT,
    OVERLAY_OPACITY_MAX_PERCENT,
    OVERLAY_OPACITY_MIN_PERCENT,
    OVERLAY_QUEUE_MAX_HEIGHT,
    OVERLAY_QUEUE_MIN_HEIGHT,
    OVERLAY_STATE_COLORS,
    OVERLAY_WIDTH,
    QUEUE_ROW_KIND_TRANSCRIPTION,
    QUEUE_ROW_KIND_UNDELIVERED,
)
from .settings_dialog_helpers import ElidingLabel
from .ui_feedback import restore_vertical_scrollbar
from .window_focus import is_shell_surface_window

RECORD_BUTTON_START_TEXT = "Record"
logger = logging.getLogger(__name__)

RECORD_BUTTON_STOP_TEXT = "Stop"
# The pin button swaps between these two; "Floating" is the wider one.
PIN_BUTTON_PINNED_TEXT = "Pinned"
PIN_BUTTON_FLOATING_TEXT = "Floating"
# Copy swaps to "Copied" after a successful copy.
COPY_BUTTON_TEXT = "Copy"
COPY_BUTTON_COPIED_TEXT = "Copied"
CLEAR_BUTTON_TEXT = "Clear"
RECORD_BUTTON_CAPTIONS = (RECORD_BUTTON_START_TEXT, RECORD_BUTTON_STOP_TEXT)
PIN_BUTTON_CAPTIONS = (PIN_BUTTON_PINNED_TEXT, PIN_BUTTON_FLOATING_TEXT)
COPY_BUTTON_CAPTIONS = (COPY_BUTTON_TEXT, COPY_BUTTON_COPIED_TEXT)

# Native z-order on Windows (`OverlayUI._apply_native_z_order`).
_GWL_EXSTYLE = -20
_WS_EX_TOPMOST = 0x00000008
_WS_EX_NOACTIVATE = 0x08000000
_HWND_TOP = 0
_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040
_GW_HWNDPREV = 3
_GA_ROOT = 2
_Z_ORDER_WALK_LIMIT = 10_000


@functools.cache
def _overlay_user32():
    """The overlay's own `user32` handle with every signature it calls declared.

    Its own `WinDLL`, never the process-wide `ctypes.windll.user32`, so the
    declarations cannot change other callers (docs/agents/windows-platform.md).
    """
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    signatures = {
        "SetWindowPos": (
            (
                wt.HWND,
                wt.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wt.UINT,
            ),
            wt.BOOL,
        ),
        "GetForegroundWindow": ((), wt.HWND),
        "GetWindowLongW": ((wt.HWND, ctypes.c_int), wt.LONG),
        "SetWindowLongW": ((wt.HWND, ctypes.c_int, wt.LONG), wt.LONG),
        "IsWindowVisible": ((wt.HWND,), wt.BOOL),
        "IsIconic": ((wt.HWND,), wt.BOOL),
        "GetWindow": ((wt.HWND, wt.UINT), wt.HWND),
        "WindowFromPoint": ((wt.POINT,), wt.HWND),
        "GetAncestor": ((wt.HWND, wt.UINT), wt.HWND),
    }
    for name, (argtypes, restype) in signatures.items():
        function = getattr(user32, name)
        function.argtypes = argtypes
        function.restype = restype
    return user32


def _window_to_stay_behind(user32, overlay_hwnd: int) -> int:
    """The window a floating overlay may go directly behind, or 0 for none.

    That is the foreground window, unless going behind it would hide the
    overlay where nobody looks (the desktop, a minimised window) or make it
    topmost after all (behind a topmost window it joins the topmost band).
    """
    foreground = int(user32.GetForegroundWindow() or 0)
    if not foreground or foreground == overlay_hwnd:
        return 0
    if not user32.IsWindowVisible(foreground) or user32.IsIconic(foreground):
        return 0
    if int(user32.GetWindowLongW(foreground, _GWL_EXSTYLE) or 0) & _WS_EX_TOPMOST:
        return 0
    if is_shell_surface_window(user32, foreground):
        return 0
    return foreground


def _top_level_window_at(user32, x: int, y: int) -> int:
    """The top-level window under the screen point (physical pixels), or 0."""
    import ctypes.wintypes as wt

    child = user32.WindowFromPoint(wt.POINT(x, y))
    return int(user32.GetAncestor(child, _GA_ROOT) or 0) if child else 0


def _is_above(user32, upper: int, lower: int) -> bool:
    """Is `upper` anywhere above `lower` in the z-order?

    Walks up from `lower`, which sits near the top when it is the foreground
    window. Bounded: the z-order may change during the walk (GetWindow docs).
    """
    hwnd = lower
    for _ in range(_Z_ORDER_WALK_LIMIT):
        hwnd = int(user32.GetWindow(hwnd, _GW_HWNDPREV) or 0)
        if not hwnd:
            return False
        if hwnd == upper:
            return True
    return False


# Language button chrome around its caption: the stylesheet reserves 8 px on
# the left and 26 px on the right for the chevron, plus a 1 px border per side
# and 2 px of rounding headroom. The microphone button has the same chrome.
_LANGUAGE_BUTTON_CHROME_PX = 38

# Footer geometry: [microphone menu, the only stretch][opacity slider][value].
# The slider was the stretch before the microphone button; at 96 px it keeps a
# usable 2-3 px per percent step over its 30-100 % range, and the value label
# stays wide enough for "100%" at the larger text sizes.
_OPACITY_SLIDER_WIDTH = 96
_OPACITY_VALUE_LABEL_WIDTH = 40
MICROPHONE_SYSTEM_DEFAULT_CAPTION = "Mic: System default"
_MICROPHONE_BLOCKED_TOOLTIP = "The microphone can be changed after this recording."

# Header geometry. The header row is [Record][Pinned] <state label>
# [Clear][Copy], and the label is its only stretching item: Qt hands the label
# exactly the span the four fixed-width buttons leave over, so the centre of
# the status text is the centre of that span, not of the header. The two spans
# are made equal in `OverlayUI._balance_header_flanks`; these are the widths
# each button needs for its own captions before that balancing runs.
_HEADER_SPACING = 6
_RECORD_BUTTON_WIDTH = 78
_PIN_BUTTON_WIDTH = 74
# Copy swaps its caption to "Copied" and must not reflow, so both text-action
# buttons are sized for the wider of the two captions.
_TEXT_ACTION_BUTTON_WIDTH = 64


# Cancel, Retry and Insert share one slot and must stay the same size.
_ACTION_SLOT_CAPTIONS = ("Retry", "Cancel", "Insert")
# The queue panel's own two buttons. Named because they are pinned where
# they are built and grown where the font is known, and the two must agree.
_QUEUE_CLEAR_BUTTON_HEIGHT = 20
_QUEUE_CANCEL_BUTTON_WIDTH = 58
_QUEUE_CANCEL_BUTTON_HEIGHT = 20
# Every row button is sized for both captions, so a row of either kind has the
# same button width and the labels beside them wrap at the same width.
_QUEUE_ROW_BUTTON_CAPTIONS = ("Cancel", "Dismiss")
_QUEUE_ROW_BUTTONS = {
    QUEUE_ROW_KIND_TRANSCRIPTION: ("Cancel", "Cancel this transcription."),
    QUEUE_ROW_KIND_UNDELIVERED: (
        "Dismiss",
        (
            "Remove this transcript from the list without inserting it. "
            "It stays in history."
        ),
    ),
}


def _fit_button_height(
    button: QtWidgets.QAbstractButton,
    height: int,
    *captions: str,
) -> None:
    """Grow a pinned height to fit the captions, leaving the width free.

    For buttons that are deliberately not width-pinned -- "Clear queue" sizes
    itself to its caption and sits at the right edge of the queue header --
    where only the height is a pixel constant chosen for 9 pt.
    """
    tallest = height
    original = button.text()
    try:
        for caption in captions or (original,):
            button.setText(caption)
            tallest = max(tallest, button.sizeHint().height())
    finally:
        button.setText(original)
    button.setFixedHeight(tallest)


def _fit_button(
    button: QtWidgets.QAbstractButton,
    width: int,
    height: int,
    *captions: str,
) -> None:
    """Pin ``width`` x ``height``, widened to whatever the captions need.

    See `OverlayUI._fit_buttons_to_font` for why the constants alone are not
    enough. `sizeHint` is computed from the content and the style and is not
    clamped by an existing fixed size, and the result is derived from
    ``width``/``height`` rather than from the button's current size -- so
    calling this on an already-pinned button is idempotent, and a call made
    while the button was still detached (where the style gives it the
    platform's larger default padding) is fully corrected by a later one.
    `set_transcription_queue` relies on that for the per-row Cancel.
    """
    original = button.text()
    widest = 0
    tallest = 0
    try:
        for caption in captions or (original,):
            button.setText(caption)
            hint = button.sizeHint()
            widest = max(widest, hint.width())
            tallest = max(tallest, hint.height())
    finally:
        button.setText(original)
    button.setFixedSize(max(width, widest), max(height, tallest))


def _queue_entry(item) -> tuple[int, str, str]:
    """One queue row as ``(token, label, kind)``; an unknown kind is a
    transcription, the kind every caller had before waiting inserts."""
    token, label, *rest = item
    kind = str(rest[0]) if rest else QUEUE_ROW_KIND_TRANSCRIPTION
    if kind not in _QUEUE_ROW_BUTTONS:
        kind = QUEUE_ROW_KIND_TRANSCRIPTION
    return int(token), str(label), kind


def _queue_title(entries, *, badge_shown: bool = False) -> str:
    """Count running transcriptions and waiting inserts apart.

    A queued job is a dictation recording, which the tray's messages call
    "Recording HH:MM:SS"; "file" named something the user never handled.
    With the not-inserted badge shown, the badge carries the waiting count
    and the title leaves it out rather than count the same rows twice.
    """
    waiting = sum(1 for _t, _l, kind in entries if kind == QUEUE_ROW_KIND_UNDELIVERED)
    running = len(entries) - waiting
    transcribing = f"Transcribing {running} recording" + ("" if running == 1 else "s")
    if badge_shown:
        return transcribing if running else ""
    if not waiting:
        return transcribing
    if not running:
        noun = "transcript" if waiting == 1 else "transcripts"
        return f"{waiting} {noun} not inserted"
    return f"{transcribing} · {waiting} not inserted"


class _RebuildableMenu(QtWidgets.QMenu):
    """A menu whose items are rebuilt from state, never while it is open.

    `QMenu.clear()` deletes the actions under an open popup, among them one
    the user has chosen whose `triggered` has not run yet. A request made
    while the popup is visible is kept and run on the event-loop turn after
    it hides: Qt hides the popup first and triggers the chosen action after
    that, and a rebuild in between would delete it.
    """

    def __init__(self, parent: QtWidgets.QWidget, rebuild: Callable[[], None]) -> None:
        super().__init__(parent)
        self._rebuild = rebuild
        self._rebuild_pending = False
        self.aboutToHide.connect(self._rebuild_after_hide)

    def request_rebuild(self) -> None:
        if self.isVisible():
            self._rebuild_pending = True
            return
        self._rebuild_pending = False
        self._rebuild()

    def _rebuild_after_hide(self) -> None:
        if self._rebuild_pending:
            QtCore.QTimer.singleShot(0, self._run_pending_rebuild)

    def _run_pending_rebuild(self) -> None:
        if self._rebuild_pending and not self.isVisible():
            self._rebuild_pending = False
            self._rebuild()


class _OverlayLanguageButton(QtWidgets.QPushButton):
    _ARROW_AREA_WIDTH = 22
    _ARROW_HALF_WIDTH = 4
    _ARROW_HALF_HEIGHT = 2
    _ARROW_COLOR = QtGui.QColor("#f0f4f8")
    _DISABLED_ARROW_COLOR = QtGui.QColor("#8894a2")

    def _menu_arrow_rect(self) -> QtCore.QRect:
        content_rect = self.contentsRect()
        width = min(self._ARROW_AREA_WIDTH, content_rect.width())
        return QtCore.QRect(
            content_rect.right() - width + 1,
            content_rect.top(),
            width,
            content_rect.height(),
        )

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        super().paintEvent(event)
        arrow_rect = self._menu_arrow_rect()
        if arrow_rect.isEmpty():
            return

        center = arrow_rect.center()
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(
            QtGui.QPen(
                self._ARROW_COLOR if self.isEnabled() else self._DISABLED_ARROW_COLOR,
                1.5,
            )
        )
        path = QtGui.QPainterPath()
        path.moveTo(
            center.x() - self._ARROW_HALF_WIDTH,
            center.y() - self._ARROW_HALF_HEIGHT,
        )
        path.lineTo(center.x(), center.y() + self._ARROW_HALF_HEIGHT)
        path.lineTo(
            center.x() + self._ARROW_HALF_WIDTH,
            center.y() - self._ARROW_HALF_HEIGHT,
        )
        painter.drawPath(path)


class _OverlayMicrophoneButton(_OverlayLanguageButton):
    """The footer's microphone menu: takes the width the opacity slider
    leaves and elides its caption rather than widening the overlay.

    Device names run to 50 characters ("Headset Microphone (Oculus Virtual
    Audio Device)"), so a caption-sized button would push the overlay wider
    for one long name. The size hint is the chrome alone, which keeps
    `_target_window_width` (it sums the footer's hint) where it was; the
    layout hands the button the rest of the row, and the caption is elided
    to whatever that is. The whole caption is in the menu and the tooltip.
    A suffix such as " (not connected)" is never elided: only the name in
    front of it gives way.
    """

    def __init__(self) -> None:
        super().__init__("")
        self._caption = ""
        self._suffix = ""
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)

    def full_caption(self) -> str:
        return self._caption + self._suffix

    def set_caption(self, caption: str, suffix: str = "") -> None:
        self._caption = caption
        self._suffix = suffix
        self._elide_caption()

    def _elide_caption(self) -> None:
        metrics = self.fontMetrics()
        room = max(0, self.width() - _LANGUAGE_BUTTON_CHROME_PX)
        name_room = max(0, room - metrics.horizontalAdvance(self._suffix))
        self.setText(
            metrics.elidedText(self._caption, QtCore.Qt.ElideRight, name_room)
            + self._suffix
        )

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._elide_caption()

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(_LANGUAGE_BUTTON_CHROME_PX, super().sizeHint().height())

    def minimumSizeHint(self) -> QtCore.QSize:
        return self.sizeHint()


def _device_part(name: str) -> str:
    """ "Microphone (HyperX QuadCast S)" -> "HyperX QuadCast S", or "".

    Windows names an input endpoint "<role> (<device>)", so on an eliding
    button the role is what survives and the device what is cut. The device
    is the bracket group the name ends with, matched by depth, so
    "Microphone (Realtek(R) Audio)" gives "Realtek(R) Audio" and "Headset
    (Oculus) (Rift)" gives "Rift" rather than "Oculus) (Rift". "" when the
    name does not have that form.
    """
    if not name.endswith(")"):
        return ""
    depth = 0
    for index in range(len(name) - 1, -1, -1):
        if name[index] == ")":
            depth += 1
        elif name[index] == "(":
            depth -= 1
            if depth == 0:
                head = name[:index]
                if head.endswith(" ") and head.strip():
                    return name[index + 1 : -1]
                return ""
    return ""


def _device_caption_names(names: tuple[str, ...]) -> dict[str, str]:
    """The caption name of each device: its device part, unless another
    device has the same one.

    Realtek lists one device under several roles ("Microphone (Realtek HD
    Audio)", "Stereo Mix (Realtek HD Audio)", "Line In (...)"); without the
    role every one of them read "Realtek HD Audio". Kept whole only then,
    because elsewhere the role is what elision keeps and the device what it
    cuts. Only the caption is shortened; the menu and the tooltip carry the
    full name.
    """
    parts = {name: _device_part(name) for name in names}
    counts: dict[str, int] = {}
    for part in parts.values():
        counts[part] = counts.get(part, 0) + 1
    return {
        name: part if part and counts[part] == 1 else name
        for name, part in parts.items()
    }


class _OverlayRecordButton(QtWidgets.QPushButton):
    """Record/Stop button whose state indicator is a generated icon.

    Putting "●"/"■" into the caption is not centered — both glyphs sit on the
    font baseline, so the dot rendered 1.5 px below the button's middle and the
    square 1 px, and since the glyphs differ in height the indicator jumped
    when the state changed. A real button icon is laid out by Qt (vertically
    centered, fixed distance to the caption) and the two shapes are drawn at
    the same size, so nothing moves between states.
    """

    # Even size: an odd icon cannot be centred exactly in an even-height
    # button, which left it half a pixel high. The gap to the caption is part
    # of the icon because Qt places icon and text almost flush.
    _INDICATOR_SIZE = 8
    _INDICATOR_GAP = 5
    _COLOR = QtGui.QColor("#f0f4f8")

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self._recording = False
        self._indicator_icons = {
            False: self._build_indicator_icon(circle=True),
            True: self._build_indicator_icon(circle=False),
        }
        self.setIconSize(
            QtCore.QSize(
                self._INDICATOR_SIZE + self._INDICATOR_GAP,
                self._INDICATOR_SIZE,
            )
        )
        self.setIcon(self._indicator_icons[False])

    @classmethod
    def _build_indicator_icon(cls, *, circle: bool) -> QtGui.QIcon:
        scale = 4  # drawn oversized, so the shape stays smooth when scaled
        size = cls._INDICATOR_SIZE * scale
        pixmap = QtGui.QPixmap(size + cls._INDICATOR_GAP * scale, size)
        pixmap.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(cls._COLOR)
        shape = QtCore.QRect(0, 0, size, size)
        if circle:
            painter.drawEllipse(shape)
        else:
            painter.drawRect(shape.adjusted(scale, scale, -scale, -scale))
        painter.end()
        return QtGui.QIcon(pixmap)

    def set_recording(self, recording: bool) -> None:
        normalized = bool(recording)
        if normalized == self._recording:
            return
        self._recording = normalized
        self.setIcon(self._indicator_icons[normalized])


class OverlayUI(QtWidgets.QWidget):
    record_toggle_requested = QtCore.Signal()
    history_requested = QtCore.Signal()
    edit_requested = QtCore.Signal()
    retry_requested = QtCore.Signal()
    insert_again_requested = QtCore.Signal()
    cancel_requested = QtCore.Signal()
    opacity_changed = QtCore.Signal(int)
    always_on_top_changed = QtCore.Signal(bool)
    language_changed = QtCore.Signal(str)
    # The persisted microphone name the user picked; "" is the system default.
    microphone_changed = QtCore.Signal(str)
    # The microphone menu is about to open: the controller answers with a
    # fresh `set_microphone_options`, so the menu lists today's devices.
    microphone_menu_requested = QtCore.Signal()
    queue_cancel_requested = QtCore.Signal(int)
    queue_clear_requested = QtCore.Signal()
    # The Clear button was pressed; carries the text the cleared state
    # offered for copying, so the controller can retire a pending Insert
    # offer that was on screen -- and only that one.
    detail_cleared = QtCore.Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Dictation")

        self._always_on_top = True
        self._temporary_foreground_active = False
        # Windows only: SetWindowPos failed, so Qt's WindowStaysOnTopHint
        # carries topmost (recreating the window) until topmost is dropped.
        self._topmost_uses_window_flag = False
        # Windows only: a floating overlay was put on top of the normal band
        # and stays above the window being typed in until the user clicks
        # into that window (`_on_desktop_mouse_press`).
        self._waiting_for_click = False
        # The window the mouse raw-input watch delivers to, 0 while off.
        self._click_watch_hwnd = 0
        # The top-level window under a press made while one of the overlay's
        # menus was open, judged once it has closed (`_after_menu_closed`).
        self._press_during_menu = 0
        initial_flags = self._base_window_flags()
        self.setWindowFlags(initial_flags)
        self._applied_window_flags = initial_flags
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)

        self._copy_feedback_timer = QtCore.QTimer(self)
        self._copy_feedback_timer.setSingleShot(True)
        self._copy_feedback_timer.setInterval(850)
        self._copy_feedback_timer.timeout.connect(self._reset_copy_button_feedback)
        self._temporary_foreground_timer = QtCore.QTimer(self)
        self._temporary_foreground_timer.setSingleShot(True)
        self._temporary_foreground_timer.timeout.connect(
            self._clear_temporary_foreground
        )
        self._drag_active = False
        self._drag_offset = QtCore.QPoint(0, 0)
        self._initial_position: QtCore.QPoint | None = None
        self._initial_corner: str | None = None
        self._initial_compact_size: QtCore.QSize | None = None
        self._compact_mode = False
        self._queue_visible = False
        self._language_modes = ("auto",)
        self._language_mode = "auto"
        self._language_change_blocked = False
        self._microphone_entries: tuple[tuple[str, str], ...] = ()
        self._microphone_selected = ""
        self._microphone_default_name = ""
        self._microphone_change_blocked = False
        self._idle_default_detail = OVERLAY_INITIAL_DETAIL
        self._manual_positioned = False
        # Where the user put the overlay, as opposed to where it currently
        # sits: a tall transcript can push it up to stay on screen, and the
        # position it is pushed to must not become the new preference.
        self._manual_anchor: QtCore.QPoint | None = None
        self._screen_change_connected = False
        self._state_background = ""
        self._state = "Idle"
        self._detail = ""
        self._copy_text: str | None = None
        self._geometry_batch_depth = 0
        self._geometry_batch_dirty = False

        self._state_label = QtWidgets.QLabel("Idle")
        self._state_label.setAlignment(QtCore.Qt.AlignCenter)
        self._state_label.setWordWrap(False)
        self._state_label.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Fixed,
        )
        state_font = QtGui.QFont()
        state_font.setBold(True)
        self._state_label.setFont(state_font)
        # Fix stable width: ensure the label reserves space for the widest
        # state text so that _target_window_width() returns a constant value
        # across all overlay states and prevents horizontal jumping.
        _state_fm = QtGui.QFontMetrics(state_font)
        _max_state_w = max(
            _state_fm.horizontalAdvance(s)
            for s in ("Idle", "Starting", "Listening", "Processing", "Done", "Error")
        )
        self._state_label.setMinimumWidth(_max_state_w)

        # Primary action: start/stop dictation without touching the keyboard.
        # Fixed width for both captions so the caption swap cannot reflow the
        # header row.
        self._record_button = _OverlayRecordButton(RECORD_BUTTON_START_TEXT)
        self._record_button.setObjectName("overlayRecordButton")
        self._record_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._record_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._record_button.setFixedWidth(_RECORD_BUTTON_WIDTH)
        self._record_button.setFixedHeight(24)
        self._record_button.clicked.connect(self.record_toggle_requested.emit)

        self._history_button = QtWidgets.QPushButton("History")
        self._history_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._history_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._history_button.setFixedWidth(68)
        self._history_button.setFixedHeight(22)
        self._history_button.clicked.connect(self.history_requested.emit)

        self._always_on_top_button = QtWidgets.QPushButton("")
        self._always_on_top_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._always_on_top_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._always_on_top_button.setCheckable(True)
        self._always_on_top_button.setFixedWidth(_PIN_BUTTON_WIDTH)
        self._always_on_top_button.setFixedHeight(24)
        self._always_on_top_button.clicked.connect(self._on_always_on_top_clicked)

        self._copy_button = QtWidgets.QPushButton(COPY_BUTTON_TEXT)
        self._copy_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._copy_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._copy_button.setFixedWidth(_TEXT_ACTION_BUTTON_WIDTH)
        self._copy_button.setFixedHeight(24)
        self._copy_button.clicked.connect(self.copy_detail_text)

        self._edit_button = QtWidgets.QPushButton("Edit")
        self._edit_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._edit_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._edit_button.setFixedWidth(58)
        self._edit_button.setFixedHeight(22)
        self._edit_button.setEnabled(False)
        self._edit_button.clicked.connect(self.edit_requested.emit)

        self._clear_button = QtWidgets.QPushButton(CLEAR_BUTTON_TEXT)
        self._clear_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._clear_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._clear_button.setFixedWidth(_TEXT_ACTION_BUTTON_WIDTH)
        self._clear_button.setFixedHeight(24)
        self._clear_button.clicked.connect(self.clear_detail_text)

        self._retry_button = QtWidgets.QPushButton("Retry")
        self._retry_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._retry_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._retry_button.setFixedSize(64, 22)
        self._retry_button.clicked.connect(self.retry_requested.emit)

        self._cancel_button = QtWidgets.QPushButton("Cancel")
        self._cancel_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._cancel_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._cancel_button.setFixedSize(64, 22)
        self._cancel_button.clicked.connect(self.cancel_requested.emit)

        self._insert_button = QtWidgets.QPushButton("Insert")
        self._insert_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._insert_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._insert_button.setFixedSize(64, 22)
        self._insert_button.setToolTip(
            "Insert this transcript into the focused window again."
        )
        self._insert_button.clicked.connect(self.insert_again_requested.emit)

        self._reset_pos_button = QtWidgets.QPushButton("Reset Pos")
        self._reset_pos_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._reset_pos_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._reset_pos_button.setFixedSize(74, 22)
        self._reset_pos_button.clicked.connect(self.reset_position)

        self._language_button = _OverlayLanguageButton("")
        self._language_button.setObjectName("overlayLanguageButton")
        self._language_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._language_button.setFocusPolicy(QtCore.Qt.NoFocus)
        # Measure instead of guessing, like the state label above: the previous
        # fixed 130 px offered only 94 px of caption area and clipped the
        # longest language names (Luxembourgish, Northern Sotho, ...), which
        # faster-whisper's ~100 languages expose by default.
        self._language_button.setFixedSize(self._widest_language_caption_width(), 22)
        self._language_menu = _RebuildableMenu(
            self._language_button, self._fill_language_menu
        )
        self._language_menu.aboutToHide.connect(self._schedule_after_menu_closed)
        self._language_button.clicked.connect(self._show_language_menu)
        self._rebuild_language_menu()

        self._detail_label = QtWidgets.QLabel(OVERLAY_INITIAL_DETAIL)
        self._detail_label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        self._detail_label.setWordWrap(True)
        self._detail_label.setTextFormat(QtCore.Qt.PlainText)
        self._detail_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse | QtCore.Qt.TextSelectableByKeyboard
        )
        self._detail_label.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self._detail_label.customContextMenuRequested.connect(
            self._show_detail_context_menu
        )
        self._detail_label.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Preferred,
        )

        self._detail_scroll = QtWidgets.QScrollArea()
        self._detail_scroll.setWidgetResizable(True)
        self._detail_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._detail_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self._detail_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._detail_scroll.setFocusPolicy(QtCore.Qt.NoFocus)
        self._detail_scroll.setWidget(self._detail_label)
        # `actionTriggered` fires for the wheel, a drag, a click on the
        # track and the keys, and never for `setValue`: the one signal that
        # tells the user's scrolling from the range changes a relayout
        # makes. (This overlay's stylesheet removes the scrollbar's step
        # buttons, so there are no arrows to click.)
        self._detail_user_scrolled = False
        self._detail_user_value = 0
        detail_bar = self._detail_scroll.verticalScrollBar()
        detail_bar.actionTriggered.connect(self._on_detail_scroll_action)
        detail_bar.rangeChanged.connect(self._on_detail_range_changed)

        self._footer_widget = QtWidgets.QWidget()
        footer = QtWidgets.QHBoxLayout(self._footer_widget)
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(8)
        self._microphone_button = _OverlayMicrophoneButton()
        self._microphone_button.setObjectName("overlayMicrophoneButton")
        self._microphone_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._microphone_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._microphone_button.setFixedHeight(22)
        self._microphone_menu = _RebuildableMenu(
            self._microphone_button, self._fill_microphone_menu
        )
        self._microphone_menu.aboutToHide.connect(self._schedule_after_menu_closed)
        self._microphone_button.clicked.connect(self._show_microphone_menu)
        self._opacity_value_label = QtWidgets.QLabel("")
        self._opacity_value_label.setFixedWidth(_OPACITY_VALUE_LABEL_WIDTH)
        self._opacity_value_label.setAlignment(
            QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter
        )
        self._opacity_value_label.setToolTip("Overlay opacity")
        self._opacity_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self._opacity_slider.setFixedWidth(_OPACITY_SLIDER_WIDTH)
        self._opacity_slider.setToolTip("Overlay opacity")
        self._opacity_slider.setRange(
            OVERLAY_OPACITY_MIN_PERCENT,
            OVERLAY_OPACITY_MAX_PERCENT,
        )
        self._opacity_slider.setFocusPolicy(QtCore.Qt.NoFocus)
        self._opacity_slider.setSingleStep(1)
        self._opacity_slider.setPageStep(5)
        self._opacity_slider.setTickInterval(5)
        self._opacity_slider.setTickPosition(QtWidgets.QSlider.NoTicks)
        self._opacity_slider.valueChanged.connect(self._on_opacity_slider_changed)
        footer.addWidget(self._microphone_button, 1)
        footer.addWidget(self._opacity_slider)
        footer.addWidget(self._opacity_value_label)
        self._rebuild_microphone_menu()

        container = QtWidgets.QFrame()
        container.setObjectName("overlayContainer")
        self._container = container

        self._layout = QtWidgets.QVBoxLayout(container)
        self._layout.setContentsMargins(14, 10, 14, 10)
        self._layout.setSpacing(4)

        self._header_widget = QtWidgets.QWidget()
        header = QtWidgets.QHBoxLayout(self._header_widget)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(_HEADER_SPACING)
        # The two button groups are equalised in `_fit_buttons_to_font`, at the
        # end of this constructor rather than here: it has to run after the
        # first `set_state` has applied the stylesheet, and it changes the
        # widths it then balances. Setting a width after `addWidget` is fine --
        # the layout reads sizes when it is activated, which happens there too.
        header.addWidget(self._record_button, 0, QtCore.Qt.AlignLeft)
        header.addWidget(self._always_on_top_button, 0, QtCore.Qt.AlignLeft)
        header.addWidget(self._state_label, 1)
        header.addWidget(self._clear_button, 0, QtCore.Qt.AlignRight)
        header.addWidget(self._copy_button, 0, QtCore.Qt.AlignRight)

        self._controls_widget = QtWidgets.QWidget()
        controls = QtWidgets.QHBoxLayout(self._controls_widget)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)
        controls.addWidget(self._history_button)
        # Cancel and Retry never apply at the same time, so they share one slot
        # of identical size: exactly one of them is visible, which keeps the row
        # width constant and shows only the action that is actually available.
        controls.addWidget(self._cancel_button)
        controls.addWidget(self._retry_button)
        controls.addWidget(self._insert_button)
        controls.addWidget(self._edit_button)
        controls.addWidget(self._reset_pos_button)
        controls.addWidget(self._language_button)

        self._build_queue_widget()

        self._layout.addWidget(self._header_widget)
        self._layout.addWidget(self._controls_widget)
        self._layout.addWidget(self._queue_widget)
        self._layout.addWidget(self._detail_scroll)
        self._layout.addWidget(self._footer_widget)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(container)

        self.resize(OVERLAY_WIDTH, OVERLAY_HEIGHT)
        # The baseline is computed structurally, never measured after a
        # `set_state`. Compact states grow to fit, so measuring after any
        # detail line that needs more than OVERLAY_DETAIL_MIN_HEIGHT bakes
        # that overflow into the baseline -- and `_update_detail_height`
        # then adds the same overflow again on top. Measuring a *short*
        # line instead only moves the threshold: OVERLAY_DETAIL_MIN_HEIGHT
        # is a fixed 42 px, so at the larger system font sizes Windows
        # offers under Accessibility > Text size even "Ready." overflows it
        # (measured at 24 pt: a 191 px baseline against a 184 px structural
        # height, so every one-line state rendered 14 px too tall).
        # Apply a state first so the container carries its stylesheet: the
        # border becomes part of the contents margins, and measuring without
        # it lands 2 px under the real layout minimum (the same trap
        # `set_state` documents). Then take the baseline *structurally*
        # rather than from `self.size()`.
        self.set_state("Idle", "Ready.")
        # Only now does a button's `sizeHint` include the padding and border
        # the stylesheet adds, so this is the first point at which the sizes
        # below can be measured rather than guessed.
        self._fit_buttons_to_font()
        self._layout.activate()
        self.layout().activate()
        self._initial_compact_size = QtCore.QSize(
            self._target_window_width(), self._compact_window_height()
        )
        self.set_state("Idle", OVERLAY_INITIAL_DETAIL)
        self.set_opacity_percent(DEFAULT_OVERLAY_OPACITY_PERCENT, emit_signal=False)
        self._sync_always_on_top_button()

    def _fit_buttons_to_font(self) -> None:
        """Grow any pinned button whose caption no longer fits, then balance.

        Every button size in this file is a pixel constant chosen for the
        default 9 pt Segoe UI. Windows' Accessibility > "Text size" raises the
        application font's point size *without* changing the DPI, so Qt's
        device-pixel-ratio does not scale these constants with it and the
        captions are simply cut off. Measured on this machine before this
        pass: at 9 pt everything fits, so the shipped layout is unchanged; at
        11.2 pt Record needs 82 px against its pinned 78 and Reset Pos 80
        against 74; at 13.5 pt nine buttons clip and every one of them is 4 px
        too short; at 18 pt Record needs 108x34 against 78x24.

        Each entry lists every caption its button can ever show, because a
        caption swap -- Record/Stop, Pinned/Floating, Copy/Copied -- must not
        reflow its row. Sizing from the caption a button happens to carry at
        construction time would leave the wider one clipped.
        """
        for button, width, height, captions in (
            (self._record_button, _RECORD_BUTTON_WIDTH, 24, RECORD_BUTTON_CAPTIONS),
            (self._history_button, 68, 22, ()),
            (self._always_on_top_button, _PIN_BUTTON_WIDTH, 24, PIN_BUTTON_CAPTIONS),
            (self._copy_button, _TEXT_ACTION_BUTTON_WIDTH, 24, COPY_BUTTON_CAPTIONS),
            (self._edit_button, 58, 22, ()),
            (self._clear_button, _TEXT_ACTION_BUTTON_WIDTH, 24, ()),
            (self._reset_pos_button, 74, 22, ()),
            # Cancel, Retry and Insert never apply at the same time and share
            # one slot, so all three are sized for the widest of the three
            # captions: a slot whose width followed the action it shows would
            # move the controls row on every state change.
            (self._retry_button, 64, 22, _ACTION_SLOT_CAPTIONS),
            (self._cancel_button, 64, 22, _ACTION_SLOT_CAPTIONS),
            (self._insert_button, 64, 22, _ACTION_SLOT_CAPTIONS),
        ):
            _fit_button(button, width, height, *captions)
        # The width is already measured from the captions; only the height was
        # a constant, and it clips from 13.5 pt onward.
        menu_button_height = max(22, self._language_button.sizeHint().height())
        self._language_button.setFixedSize(
            self._widest_language_caption_width(), menu_button_height
        )
        # Its width is whatever the footer leaves (the caption elides), so
        # only the height is fitted, to the language button's: the two menus
        # look alike, and at 13.5 pt a 22 px button clips its caption.
        self._microphone_button.setFixedHeight(menu_button_height)
        # Fixed at its widest text, so the microphone button beside it never
        # shifts while the slider crosses 100 %. Measured: "100%" is 28 px at
        # 9 pt, 36 at 11.25 and 45 at 13.5, so the 40 px floor gives way there.
        value_label = self._opacity_value_label
        value_label.setFixedWidth(
            max(
                _OPACITY_VALUE_LABEL_WIDTH,
                value_label.fontMetrics().horizontalAdvance("100%") + 2,
            )
        )
        # The queue panel exists from the constructor, so its header button is
        # reachable here. Its width is deliberately not pinned -- it sizes to
        # its caption at the right edge of the header -- so only the height
        # constant needs to grow. The per-row Cancel cannot be done here: rows
        # are built at runtime, one per in-flight transcription.
        _fit_button_height(
            self._queue_clear_button,
            _QUEUE_CLEAR_BUTTON_HEIGHT,
            "Clear queue",
        )
        # The badge beside it is exactly as tall, so showing or hiding it
        # never changes the queue header's height.
        self._not_inserted_badge.setFixedHeight(
            self._queue_clear_button.maximumHeight()
        )
        # Balanced last, because it widens the narrower group from the sizes
        # set above. The stretching state label between the two groups is
        # centred on the header -- and so on the overlay, whose horizontal
        # margins are symmetric -- only while both groups are equally wide.
        self._balance_header_flanks(
            _HEADER_SPACING,
            (
                (self._record_button, RECORD_BUTTON_CAPTIONS),
                (self._always_on_top_button, PIN_BUTTON_CAPTIONS),
            ),
            (
                (self._clear_button, (CLEAR_BUTTON_TEXT,)),
                (self._copy_button, COPY_BUTTON_CAPTIONS),
            ),
        )

    @staticmethod
    def _balance_header_flanks(
        spacing: int,
        left: tuple[tuple[QtWidgets.QAbstractButton, tuple[str, ...]], ...],
        right: tuple[tuple[QtWidgets.QAbstractButton, tuple[str, ...]], ...],
    ) -> None:
        """Give the header's two button groups identical total widths.

        The state label is the header's only stretching item, so Qt gives it
        the span the fixed-width buttons leave over and ``AlignCenter`` puts
        the text in the middle of *that span*. The span's midpoint is the
        header's midpoint only while both groups are equally wide; otherwise
        the status text sits half the difference off centre. Measured on the
        unbalanced header: 78 + 6 + 74 = 158 px on the left against
        64 + 6 + 64 = 134 px on the right, so "Idle", "Listening",
        "Processing", "Done" and "Error" all rendered 12 px right of the
        overlay's centre line -- 7 px until the 78 px Record button replaced
        the 68 px History button as the first item.

        Widening the narrower group's buttons removes the difference where it
        arises. A fixed spacer between the label and Clear would centre the
        text just as exactly, but it leaves visibly unequal gaps on either
        side of the text, and it is a compensating constant that has to be
        re-derived by hand whenever a button width changes.

        Each button comes with every caption it can ever show, because the
        fallback below has to size an unpinned button for its *widest* one,
        not for whatever it happens to display at construction time -- the
        pin button is still empty here, and Copy still says "Copy" rather
        than "Copied".
        """

        def pinned_width(
            button: QtWidgets.QAbstractButton, captions: tuple[str, ...]
        ) -> int:
            # ``minimumWidth`` is the width ``setFixedWidth`` pinned, which is
            # the deliberate constant -- and the right source, because a
            # pinned width is often deliberately below the style's natural
            # width, so ``sizeHint()`` would discard it.
            if button.minimumWidth() == button.maximumWidth():
                return button.minimumWidth()
            # Not pinned: ``minimumWidth`` is the style minimum instead, near
            # zero, so this group would measure far too narrow and the deficit
            # spread over its members would pin this button under its own
            # caption. The flanks still come out equal in that case, so
            # neither the centring nor the no-jump assertion can see it --
            # hence the fallback and the log rather than a silent wrong
            # number. Raising instead would trade a clipped caption for an
            # overlay that does not open at all.
            original = button.text()
            widest = button.sizeHint().width()
            try:
                for caption in captions:
                    button.setText(caption)
                    widest = max(widest, button.sizeHint().width())
            finally:
                button.setText(original)
            logger.warning(
                "Header button %r is not fixed-width (%d..%d); sizing it from "
                "the widest of %s so the caption is not clipped.",
                original or captions[0] if captions else original,
                button.minimumWidth(),
                button.maximumWidth(),
                ", ".join(captions) or "its current caption",
            )
            return max(button.minimumWidth(), widest)

        def group_width(
            buttons: tuple[tuple[QtWidgets.QAbstractButton, tuple[str, ...]], ...],
        ) -> int:
            return sum(
                pinned_width(button, captions) for button, captions in buttons
            ) + spacing * (len(buttons) - 1)

        target = max(group_width(left), group_width(right))
        for group in (left, right):
            missing = target - group_width(group)
            if missing <= 0:
                continue
            share, remainder = divmod(missing, len(group))
            for index, (button, captions) in enumerate(group):
                extra = share + (1 if index < remainder else 0)
                button.setFixedWidth(pinned_width(button, captions) + extra)

    @property
    def always_on_top(self) -> bool:
        return self._always_on_top

    def _wants_topmost(self) -> bool:
        return self._always_on_top or self._temporary_foreground_active

    def _base_window_flags(self) -> QtCore.Qt.WindowType:
        flags = QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint
        if sys.platform == "win32":
            # Topmost is switched natively (`_apply_native_z_order`). Changing
            # this flag means `setWindowFlags`, which destroys and recreates
            # the native window: the overlay vanished and reappeared on every
            # Pinned/Floating click. Only a failed SetWindowPos falls back.
            stays_on_top = self._topmost_uses_window_flag
        else:
            stays_on_top = self._always_on_top
        if stays_on_top:
            flags |= QtCore.Qt.WindowStaysOnTopHint
        if hasattr(QtCore.Qt, "WindowDoesNotAcceptFocus"):
            flags |= QtCore.Qt.WindowDoesNotAcceptFocus
        return flags

    def _sync_always_on_top_button(self) -> None:
        checked = bool(self._always_on_top)
        self._always_on_top_button.setChecked(checked)
        self._always_on_top_button.setText(
            PIN_BUTTON_PINNED_TEXT if checked else PIN_BUTTON_FLOATING_TEXT
        )
        self._always_on_top_button.setToolTip(
            "Keep the overlay above other windows."
            if checked
            else "Allow the overlay to stay behind other windows."
        )

    def _apply_window_flags(self, *, raise_window: bool = False) -> None:
        """Apply the topmost state; `raise_window` also shows the overlay."""
        if not self._wants_topmost():
            self._topmost_uses_window_flag = False
        self._sync_qt_window_flags(show=raise_window)
        if raise_window:
            self.raise_()
        if sys.platform != "win32":
            return
        self._apply_noactivate_style()
        if (
            not self._apply_native_z_order()
            and self._wants_topmost()
            and not self._topmost_uses_window_flag
        ):
            self._topmost_uses_window_flag = True
            self._sync_qt_window_flags(show=raise_window)
        self._sync_click_watch()

    def _sync_qt_window_flags(self, *, show: bool) -> None:
        desired_flags = self._base_window_flags()
        if self._applied_window_flags != desired_flags:
            # ``setWindowFlags`` destroys and recreates the native window,
            # which shows as a visible blink. On Windows the flags change only
            # when SetWindowPos fails (`_base_window_flags`).
            was_visible = self.isVisible()
            self.setWindowFlags(desired_flags)
            self._applied_window_flags = desired_flags
            if was_visible or show:
                self.show()
        elif show and not self.isVisible():
            self.show()

    def _on_always_on_top_clicked(self, checked: bool) -> None:
        self.set_always_on_top(checked, emit_signal=True)

    def set_always_on_top(
        self,
        enabled: bool,
        *,
        emit_signal: bool = False,
    ) -> None:
        normalized = bool(enabled)
        if self._always_on_top == normalized:
            self._sync_always_on_top_button()
            return
        self._always_on_top = normalized
        if normalized:
            self._temporary_foreground_active = False
            self._temporary_foreground_timer.stop()
        self._sync_always_on_top_button()
        self._apply_window_flags(raise_window=normalized)
        if emit_signal:
            self.always_on_top_changed.emit(normalized)

    def reveal_temporarily(self, duration_ms: int = 1800) -> None:
        if not self._always_on_top:
            self._temporary_foreground_active = True
            self._temporary_foreground_timer.start(max(1, int(duration_ms)))
        self._apply_window_flags(raise_window=True)
        self._reposition_within_current_screen()

    def paint_now(self) -> None:
        """Paint the current content at once, without running the event loop.

        For a caller about to hold the Qt thread (a stop waiting for the
        microphone's backlog): `processEvents` would also deliver queued
        signals and hotkeys into the middle of what that caller is doing.
        """
        if self.isVisible():
            self.repaint()

    def restore_visibility(self) -> None:
        """Restore overlay visibility and native z-order after a system resume."""
        self.reveal_temporarily()

    def _clear_temporary_foreground(self) -> None:
        if self._always_on_top or not self._temporary_foreground_active:
            return
        self._temporary_foreground_active = False
        self._apply_window_flags()

    def set_state(
        self,
        state: str,
        detail: str = "",
        *,
        compact: bool | None = None,
        copy_text: str | None = None,
        error_action: str | None = None,
        editable: bool = False,
        starting: bool = False,
    ) -> None:
        """Render an overlay state.

        ``copy_text`` overrides what the Copy action puts into the clipboard.
        It is used when the detail area shows more than the plain transcript
        (an insertion error plus the transcript preview, for example), so Copy
        still yields exactly the transcript.

        ``error_action`` selects the follow-up action offered in the Error
        state: ``OVERLAY_ERROR_ACTION_INSERT`` when the transcription itself
        succeeded and only the insertion failed, otherwise Retry.

        ``editable`` enables Edit on a state other than Done: the Error of a
        transcript that was not inserted, whose edit is what Insert and the
        re-paste then paste (the controller decides; owner's rule
        2026-10-09).

        ``starting`` paints a Listening state as "Starting" in its own colour:
        the dictation has begun but the microphone is not open yet, so nothing
        is recorded and the Listening green would invite speech too early.
        Every control still behaves as in Listening.
        """
        if state == "Idle" and detail.strip():
            self._idle_default_detail = detail
        shown_state = "Starting" if starting and state == "Listening" else state
        self._state_label.setText(shown_state)
        self._detail_label.setText(detail)
        self._state = state
        self._detail = detail
        self._copy_text = copy_text
        has_detail = bool(detail.strip())
        if compact is None:
            self._compact_mode = state in {"Idle", "Listening", "Processing"}
        else:
            self._compact_mode = compact
        self._copy_button.setEnabled(has_detail or bool(copy_text))
        self._edit_button.setEnabled(has_detail and (state == "Done" or editable))
        self._clear_button.setEnabled(has_detail and state in {"Done", "Error"})
        self._sync_record_button(state)
        self._sync_action_slot(state, error_action)
        self._reset_pos_button.setEnabled(True)
        self._language_change_blocked = state in {"Listening", "Processing"}
        self._sync_language_button()
        # Only while recording: the running capture keeps its microphone (a
        # warm-stream retarget waits for it), so a pick made now would
        # describe a recording it does not apply to. Processing is fine --
        # the capture has ended and a switch affects the next recording.
        self._microphone_change_blocked = state == "Listening"
        self._sync_microphone_button()
        self._reset_copy_button_feedback()
        # Style before measuring: the container's stylesheet border becomes
        # part of its contents margins, so measuring first would size the
        # window for an unstyled container and leave it below its own layout
        # minimum (the window then refused to shrink to the computed target).
        self._apply_state_stylesheet(shown_state)
        self._update_detail_height()
        # Errors lead with the reason and may be followed by a long transcript
        # preview, so keep the reason in view; every other state shows the end
        # of the transcript. A paint supersedes the user's scrolling, and
        # `_on_detail_range_changed` holds this position through the relayout
        # that a batch or a queue change makes afterwards.
        self._detail_user_scrolled = False
        self._detail_scroll.verticalScrollBar().setValue(self._detail_rest_value())

    def _sync_record_button(self, state: str) -> None:
        recording = state == "Listening"
        self._record_button.setText(
            RECORD_BUTTON_STOP_TEXT if recording else RECORD_BUTTON_START_TEXT
        )
        self._record_button.set_recording(recording)
        self._record_button.setToolTip(
            "Stop dictation and transcribe."
            if recording
            else "Start dictation (same as the recording hotkey)."
        )
        if self._record_button.property("recording") != recording:
            self._record_button.setProperty("recording", recording)
            self._record_button.style().unpolish(self._record_button)
            self._record_button.style().polish(self._record_button)
            self._record_button.update()

    def _sync_action_slot(self, state: str, error_action: str | None = None) -> None:
        """Show the one follow-up action that applies to the current state.

        Cancel, Retry and Insert are mutually exclusive and share one slot of
        identical fixed size, so swapping them keeps the row width constant.
        Retry re-transcribes, which is meaningless when the transcription
        succeeded and only the insertion failed — that case offers Insert.
        """
        is_error = state == "Error"
        show_insert = is_error and error_action == OVERLAY_ERROR_ACTION_INSERT
        # OVERLAY_ERROR_ACTION_NONE means exactly that. Without it "anything
        # that is not Insert" fell through to Retry, which re-transcribes the
        # last failed recording -- wrong for an error whose transcript already
        # exists, and actively harmful when that recording is a different one.
        show_retry = (
            is_error and not show_insert and error_action != OVERLAY_ERROR_ACTION_NONE
        )
        self._retry_button.setEnabled(show_retry)
        self._insert_button.setEnabled(show_insert)
        self._cancel_button.setEnabled(state in {"Listening", "Processing"})
        self._retry_button.setVisible(show_retry)
        self._insert_button.setVisible(show_insert)
        self._cancel_button.setVisible(not (show_retry or show_insert))

    @property
    def state(self) -> str:
        """The state name last passed to `set_state`."""
        return self._state

    @property
    def detail(self) -> str:
        """The detail text last passed to `set_state`."""
        return self._detail

    @property
    def copy_text(self) -> str | None:
        """The ``copy_text`` last passed to `set_state`: what Copy yields
        instead of the detail, the transcript of an Insert offer."""
        return self._copy_text

    @property
    def detail_is_being_read(self) -> bool:
        """True while the detail is scrolled off its rest position or selected.

        `set_state` scrolls an Error back to the top (every other state to
        the bottom) and `setText` drops any selection, so a writer that
        repaints the same text periodically -- the preload progress poll
        with an Insert offer pending -- has to ask first: it reset the
        scroll and the selection every 600 ms for the length of a download.

        The rest position is held by `_on_detail_range_changed` through the
        relayouts that follow a paint, so a value off it is the user's
        doing -- before that, a batched Done answered True at 392 of 408 px
        with nobody touching the overlay. The user's own position is held
        the same way, and `_detail_user_scrolled` is what says they chose
        one: a queue relayout can clamp that position onto the rest
        position for as long as the rows are shown (measured: 296 of 296
        with the user one step up), and the poll must not take that moment
        to paint over what they are reading. Scrolling back to the rest
        position hands the hold back; a one-way latch here left the poll
        blocked for the rest of a download after the user had returned to
        the bottom.
        """
        scrollbar = self._detail_scroll.verticalScrollBar()
        scrolled = (
            self._detail_user_scrolled or scrollbar.value() != self._detail_rest_value()
        )
        return scrolled or self._detail_label.hasSelectedText()

    def _detail_rest_value(self) -> int:
        """Where a paint leaves the detail: an Error at the top, the rest at the end."""
        scrollbar = self._detail_scroll.verticalScrollBar()
        return 0 if self._state == "Error" else scrollbar.maximum()

    def _on_detail_scroll_action(self, _action: int) -> None:
        """Record where the user put the detail, or that they put it back.

        `actionTriggered` is emitted with `sliderPosition()` already at the
        position the action chose and `value()` not yet updated, so this is
        the moment to read it. Set once and cleared only by the next paint,
        the flag disarmed the hold for good after the first scroll: back at
        the bottom, the next relayout clamped the value exactly as before
        c012ab0 (measured: 286 of 302 with the user at the rest position),
        and because `detail_is_being_read` then answered True the preload
        poll, the one writer that would have painted, stayed away for the
        rest of the download.
        """
        position = self._detail_scroll.verticalScrollBar().sliderPosition()
        self._detail_user_value = position
        self._detail_user_scrolled = position != self._detail_rest_value()

    def _on_detail_range_changed(self, _minimum: int, maximum: int) -> None:
        """Hold the rest position, or the user's, through a relayout.

        `set_state` scrolls to the rest position, and the relayout that can
        follow -- the geometry a `batched_update` applies at its end, queue
        rows appearing beside a long transcript -- changes the range after
        that. Qt clamps the value to an intermediate maximum and leaves it
        there when the range grows again, so a batched Done showed 392 of
        408 px of its transcript with the last line hidden, and
        `detail_is_being_read` answered True for nothing the user had done.
        The user's own position gets the same treatment, bounded by the
        range: a drag to 312 of 408 was clamped to 302 by the queue rows
        appearing and left at 286 when they went away.
        """
        scrollbar = self._detail_scroll.verticalScrollBar()
        if self._detail_user_scrolled:
            scrollbar.setValue(min(self._detail_user_value, maximum))
        else:
            scrollbar.setValue(self._detail_rest_value())

    def _apply_state_stylesheet(self, state: str) -> None:
        bg = OVERLAY_STATE_COLORS.get(state, OVERLAY_STATE_COLORS["Idle"])
        if bg != self._state_background:
            self.setStyleSheet(
                f"""
            QFrame#overlayContainer {{
                background-color: {bg};
                border: 1px solid rgba(255,255,255,0.25);
                border-radius: 10px;
            }}
            QLabel {{
                color: #ffffff;
            }}
            QScrollArea {{
                background: transparent;
            }}
            QScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            QScrollBar:vertical {{
                width: 14px;
                background: transparent;
                margin: 2px 0 2px 0;
            }}
            QScrollBar::handle:vertical {{
                min-height: 24px;
                border-radius: 6px;
                background: rgba(255,255,255,0.45);
                border: 1px solid rgba(255,255,255,0.3);
            }}
            QScrollBar::handle:vertical:hover {{
                background: rgba(255,255,255,0.62);
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: rgba(0,0,0,0.12);
                border-radius: 6px;
            }}
            QSlider::groove:horizontal {{
                height: 6px;
                border-radius: 3px;
                background: rgba(255,255,255,0.28);
            }}
            QSlider::sub-page:horizontal {{
                background: rgba(255,255,255,0.7);
                border-radius: 3px;
            }}
            QSlider::handle:horizontal {{
                width: 12px;
                margin: -4px 0;
                border-radius: 6px;
                border: 1px solid rgba(255,255,255,0.75);
                background: rgba(0,0,0,0.45);
            }}
            QSlider::handle:horizontal:hover {{
                background: rgba(255,255,255,0.35);
            }}
            QPushButton {{
                border: 1px solid rgba(255,255,255,0.35);
                border-radius: 6px;
                background-color: rgba(0,0,0,0.2);
                color: #ffffff;
                padding: 0 8px;
            }}
            QPushButton#overlayLanguageButton {{
                padding: 0 26px 0 8px;
            }}
            QPushButton#overlayMicrophoneButton {{
                padding: 0 26px 0 8px;
                text-align: left;
            }}
            /* Primary action: same fill as its neighbours (a lighter fill
               reads as a permanent hover state) and a brighter border to mark
               it. Recording tints it red without changing the box. */
            QPushButton#overlayRecordButton {{
                border-color: rgba(255,255,255,0.7);
            }}
            QPushButton#overlayRecordButton[recording="true"] {{
                background-color: rgba(190,60,60,0.42);
                border-color: rgba(255,190,190,0.85);
            }}
            QPushButton#overlayRecordButton[recording="true"]:hover {{
                background-color: rgba(205,75,75,0.55);
            }}
            QPushButton:hover {{
                background-color: rgba(255,255,255,0.18);
            }}
            QPushButton:pressed {{
                background-color: rgba(255,255,255,0.26);
                padding-top: 1px;
            }}
            QPushButton[copied="true"] {{
                background-color: rgba(120,255,160,0.35);
                border-color: rgba(190,255,215,0.65);
            }}
            QPushButton:disabled {{
                color: rgba(255,255,255,0.55);
                border-color: rgba(255,255,255,0.2);
            }}
            QLabel[queueRowKind="undelivered"] {{
                color: #ffd98a;
            }}
            /* Not inserted: amber with dark text, apart from every state
               colour (Error red, Listening green), whatever the state. */
            QLabel#overlayNotInsertedBadge {{
                background-color: #ffb300;
                color: #1f1400;
                border: 1px solid #ffe082;
                border-radius: 4px;
                padding: 0 6px;
                font-weight: bold;
            }}
                """
            )
            self._state_background = bg

    def set_language_options(
        self,
        modes: tuple[str, ...],
        selected_mode: str,
    ) -> None:
        normalized_modes = tuple(
            dict.fromkeys(
                str(mode).strip().lower() for mode in modes if str(mode).strip()
            )
        ) or ("auto",)
        normalized_selected = str(selected_mode or "auto").strip().lower()
        self._language_modes = normalized_modes
        self._language_mode = (
            normalized_selected
            if normalized_selected in normalized_modes
            else normalized_modes[0]
        )
        self._rebuild_language_menu()

    def _rebuild_language_menu(self) -> None:
        self._language_menu.request_rebuild()
        self._sync_language_button()

    def _fill_language_menu(self) -> None:
        self._language_menu.clear()
        for mode in self._language_modes:
            action = self._language_menu.addAction(LANGUAGE_MODE_LABELS.get(mode, mode))
            action.setCheckable(True)
            action.setChecked(mode == self._language_mode)
            action.triggered.connect(
                lambda _checked=False, value=mode: self._select_language(value)
            )

    def _select_language(self, mode: str) -> None:
        if self._language_change_blocked or mode not in self._language_modes:
            return
        if mode == self._language_mode:
            self._rebuild_language_menu()
            return
        self._language_mode = mode
        self._rebuild_language_menu()
        self.language_changed.emit(mode)

    def _show_language_menu(self) -> None:
        if not self._language_button.isEnabled():
            return
        self._language_menu.popup(
            self._language_button.mapToGlobal(
                QtCore.QPoint(0, self._language_button.height())
            )
        )

    def _widest_language_caption_width(self) -> int:
        metrics = QtGui.QFontMetrics(self._language_button.font())
        widest = max(
            metrics.horizontalAdvance(self._language_caption(label))
            for label in LANGUAGE_MODE_LABELS.values()
        )
        return widest + _LANGUAGE_BUTTON_CHROME_PX

    @staticmethod
    def _language_caption(label: str) -> str:
        return f"Lang: {label}"

    def _sync_language_button(self) -> None:
        label = LANGUAGE_MODE_LABELS.get(self._language_mode, self._language_mode)
        has_choices = len(self._language_modes) > 1
        self._language_button.setText(self._language_caption(label))
        self._language_button.setEnabled(
            has_choices and not self._language_change_blocked
        )
        if self._language_change_blocked:
            tooltip = "Language can be changed after the current operation finishes."
        elif not has_choices:
            tooltip = f"Language is fixed to {label} for the selected engine and model."
        else:
            tooltip = f"Current language: {label}. Click to change it."
        self._language_button.setToolTip(tooltip)

    def set_microphone_options(
        self,
        entries: tuple[tuple[str, str], ...],
        selected: str,
        default_name: str = "",
    ) -> None:
        """The microphone menu: `audio_devices.input_device_choices` entries
        (``(label, persisted name)``), the persisted selection and the device
        the system default resolves to (for the caption)."""
        self._microphone_entries = tuple(
            (str(label), str(value)) for label, value in entries
        )
        self._microphone_selected = str(selected or "")
        self._microphone_default_name = str(default_name or "")
        self._rebuild_microphone_menu()

    def _rebuild_microphone_menu(self) -> None:
        self._microphone_menu.request_rebuild()
        self._sync_microphone_button()

    def _fill_microphone_menu(self) -> None:
        self._microphone_menu.clear()
        for index, (label, value) in enumerate(self._microphone_entries):
            if index == 1:
                self._microphone_menu.addSeparator()
            action = self._microphone_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(value == self._microphone_selected)
            action.triggered.connect(
                lambda _checked=False, name=value: self._select_microphone(name)
            )

    def _select_microphone(self, name: str) -> None:
        if self._microphone_change_blocked:
            return
        if name == self._microphone_selected:
            self._rebuild_microphone_menu()
            return
        self._microphone_selected = name
        self._rebuild_microphone_menu()
        self.microphone_changed.emit(name)

    def _show_microphone_menu(self) -> None:
        if not self._microphone_button.isEnabled():
            return
        self.microphone_menu_requested.emit()
        self._microphone_menu.popup(
            self._microphone_button.mapToGlobal(
                QtCore.QPoint(0, self._microphone_button.height())
            )
        )

    def _selected_microphone_label(self) -> str:
        for label, value in self._microphone_entries:
            if value == self._microphone_selected:
                return label
        return self._microphone_selected or "System default"

    def _microphone_caption(self) -> tuple[str, str]:
        """The caption and the suffix that must survive elision."""
        names = tuple(
            dict.fromkeys(
                (
                    *(value for _label, value in self._microphone_entries if value),
                    *(name for name in (self._microphone_default_name,) if name),
                )
            )
        )
        caption_names = _device_caption_names(names)
        if not self._microphone_selected:
            name = self._microphone_default_name
            if not name:
                return MICROPHONE_SYSTEM_DEFAULT_CAPTION, ""
            return f"Mic: Default · {caption_names[name]}", ""
        # The label is the name plus " (not connected)" when it is missing;
        # that suffix stays visible, the name is shortened and elided.
        selected = self._microphone_selected
        suffix = self._selected_microphone_label().removeprefix(selected)
        return f"Mic: {caption_names.get(selected, selected)}", suffix

    def _sync_microphone_button(self) -> None:
        self._microphone_button.set_caption(*self._microphone_caption())
        self._microphone_button.setEnabled(not self._microphone_change_blocked)
        self._microphone_button.setToolTip(
            _MICROPHONE_BLOCKED_TOOLTIP
            if self._microphone_change_blocked
            else f"Microphone: {self._selected_microphone_label()}. "
            "Click to choose another."
        )

    def move_to_corner(
        self,
        corner: str = "top-right",
        *,
        screen: QtGui.QScreen | None = None,
    ) -> None:
        screen = screen or self._current_screen()
        if screen is None:
            return

        normalized = str(corner or "top-right").strip().lower()
        target = self._position_for_corner(screen, normalized)
        self.move(target)
        self._initial_position = QtCore.QPoint(target)
        self._initial_corner = normalized
        self._claim_manual_position(None)

    def apply_corner_setting(self, corner: str) -> None:
        """Apply the configured corner without discarding a dragged position.

        Moves the overlay only when the configured corner actually changed;
        re-applying an unchanged setting (e.g. saving unrelated settings)
        must not reset a manually dragged overlay.
        """
        normalized = str(corner or "top-right").strip().lower()
        if normalized == self._initial_corner:
            return
        self.move_to_corner(normalized)

    def set_initial_position(self, point: QtCore.QPoint) -> None:
        self._initial_position = QtCore.QPoint(point)
        self._initial_corner = None
        self._claim_manual_position(point)

    def reset_position(self) -> None:
        self.ensure_compact_size_unless_showing_a_result()
        if self._initial_corner:
            self.move_to_corner(
                self._initial_corner,
                screen=self._current_screen(),
            )
            return
        if self._initial_position is None:
            return
        target = QtCore.QPoint(self._initial_position)
        screen = QtGui.QGuiApplication.screenAt(target)
        if screen is None:
            screen = self._current_screen()
        if screen is not None:
            target = self._clamp_point_to_screen(target, screen)
        self.move(target)
        self._claim_manual_position(
            self._initial_position if self._initial_corner is None else None
        )

    def nativeEvent(self, event_type, message):
        """Prevent window activation on mouse click (Windows).

        On Windows, ``WindowDoesNotAcceptFocus`` does not reliably prevent
        the OS from activating the window on the first click.  By
        intercepting ``WM_MOUSEACTIVATE`` and returning ``MA_NOACTIVATE``
        we ensure the copy button responds on the very first click without
        stealing focus from the target application.
        """
        if sys.platform == "win32" and event_type == b"windows_generic_MSG":
            try:
                import ctypes
                import ctypes.wintypes

                msg = ctypes.wintypes.MSG.from_address(int(message))
                _WM_MOUSEACTIVATE = 0x0021
                _MA_NOACTIVATE = 3
                if msg.message == _WM_MOUSEACTIVATE:
                    return True, _MA_NOACTIVATE
                if (
                    msg.message == raw_mouse_input.WM_INPUT
                    and self._click_watch_hwnd
                    and raw_mouse_input.mouse_button_pressed(msg.lParam)
                ):
                    # `pt` is where the cursor was when the press happened.
                    # Handled after this window procedure returns, because
                    # moving the overlay re-enters it.
                    QtCore.QTimer.singleShot(
                        0,
                        functools.partial(
                            self._on_desktop_mouse_press, msg.pt.x, msg.pt.y
                        ),
                    )
            except Exception:
                pass
        # Not handled: DefWindowProc must still see a WM_INPUT (cleanup).
        return super().nativeEvent(event_type, message)

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        """Apply ``WS_EX_NOACTIVATE`` each time the window is shown.

        Qt's ``WindowDoesNotAcceptFocus`` flag is not always honoured on
        Windows.  Setting ``WS_EX_NOACTIVATE`` directly via the Win32 API
        is more reliable.  We re-apply it on every show because Qt may
        reset extended window styles when updating stylesheets or flags.
        """
        super().showEvent(event)
        handle = self.windowHandle()
        if handle is not None and not self._screen_change_connected:
            handle.screenChanged.connect(self._on_screen_changed)
            self._screen_change_connected = True
        if self._compact_mode:
            self.ensure_compact_size()
        else:
            self._update_detail_height()
        if sys.platform == "win32":
            self._apply_noactivate_style()
            if self._wants_topmost():
                # Qt's flag does not carry topmost here (`_base_window_flags`),
                # so the first show, and a show after a recreation, sets it.
                self._apply_native_z_order()
            else:
                # A shown window appears on top of its band, so a floating
                # overlay is above the window being typed in again.
                self._waiting_for_click = True
                self._sync_click_watch()

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        super().hideEvent(event)
        self._waiting_for_click = False
        self._sync_click_watch()

    def shutdown(self) -> None:
        """Remove the mouse raw-input watch before the application quits."""
        self._waiting_for_click = False
        self._sync_click_watch()

    def _on_screen_changed(self, _screen: QtGui.QScreen | None) -> None:
        if self._compact_mode:
            self.ensure_compact_size()
        else:
            self._update_detail_height()
        if sys.platform == "win32":
            self._apply_noactivate_style()
        self._reposition_within_current_screen()

    def _apply_noactivate_style(self) -> None:
        """Set ``WS_EX_NOACTIVATE`` on the native window handle."""
        try:
            user32 = _overlay_user32()
            hwnd = int(self.winId())
            style = int(user32.GetWindowLongW(hwnd, _GWL_EXSTYLE) or 0)
            user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, style | _WS_EX_NOACTIVATE)
        except Exception:
            pass

    def _apply_native_z_order(self) -> bool:
        """Set the topmost state with SetWindowPos, never activating the overlay.

        Topmost while pinned or revealed. Otherwise `HWND_NOTOPMOST`, which
        puts the overlay on top of the normal band -- above the window being
        typed in, where it stays until the user clicks into that window
        (`_on_desktop_mouse_press`). Going straight behind the foreground
        window instead (8a72099) made a floating overlay vanish the moment
        Floating was clicked or a reveal ended.
        """
        if sys.platform != "win32":
            return False
        try:
            user32 = _overlay_user32()
            hwnd = int(self.winId())
            flags = _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOACTIVATE
            handle = self.windowHandle()
            # False inside `showEvent`, which Qt sends before showing the
            # native window; showing it from there would show it unpainted.
            shown = handle is not None and handle.isVisible()
            if shown:
                # Re-shows a window the system hid (resume, 2026-06-08).
                flags |= _SWP_SHOWWINDOW
            if self._wants_topmost():
                self._waiting_for_click = False
                return bool(user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, flags))
            # A hidden overlay starts waiting in its `showEvent`.
            self._waiting_for_click = shown
            return bool(user32.SetWindowPos(hwnd, _HWND_NOTOPMOST, 0, 0, 0, 0, flags))
        except Exception:
            return False

    def _sync_click_watch(self) -> None:
        """Watch mouse presses only while a floating overlay waits for one.

        The watch (`raw_mouse_input`) delivers a `WM_INPUT` for every mouse
        event on the desktop, movements included, so it runs only between
        the overlay landing on top of the normal band and the click that
        sends it behind the window being typed in.
        """
        if sys.platform != "win32":
            return
        wanted = self._waiting_for_click and not self._wants_topmost()
        hwnd = int(self.winId()) if wanted else 0
        if hwnd == self._click_watch_hwnd:
            return
        if hwnd:
            self._click_watch_hwnd = (
                hwnd if raw_mouse_input.watch_mouse_presses(hwnd) else 0
            )
        else:
            raw_mouse_input.stop_watching_mouse_presses()
            self._click_watch_hwnd = 0

    def _on_desktop_mouse_press(self, x: int, y: int) -> None:
        """Go behind the window being typed in once the user clicks into it.

        Windows raises a window when it is activated, not when the user
        clicks into the window that is already active, so a floating overlay
        stayed above the editor until it was minimised and restored. Every
        other press leaves the overlay where it is: on the overlay itself; on
        an inactive window, which its activation raises above the overlay;
        and on a desktop, taskbar, topmost or minimised foreground window
        (`_window_to_stay_behind`), where the overlay keeps waiting.
        """
        if not self._waiting_for_click or self._wants_topmost():
            return
        try:
            user32 = _overlay_user32()
            pressed = _top_level_window_at(user32, x, y)
            if self._owns_widget(QtWidgets.QApplication.activePopupWidget()):
                # The open menu is the foreground window, and closing it
                # re-activates and raises the editor: judged after that. Only
                # the overlay's own menus judge it when they close
                # (`_after_menu_closed`); kept for another popup it never was.
                self._press_during_menu = pressed
                return
            self._follow_press(user32, pressed)
        except Exception:
            logger.debug(
                "Following a click behind the foreground failed", exc_info=True
            )
        finally:
            self._sync_click_watch()

    def _follow_press(self, user32, pressed: int) -> None:
        """Go behind the foreground window if `pressed` is it, below the overlay."""
        hwnd = int(self.winId())
        foreground = _window_to_stay_behind(user32, hwnd)
        if not foreground or pressed != foreground:
            return
        if not _is_above(user32, hwnd, foreground):
            # A window was activated above the overlay since it waits.
            self._waiting_for_click = False
            return
        flags = _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOACTIVATE
        # Refused behind a higher-integrity window (access denied, e.g. an
        # elevated Task Manager): the overlay stays above it, waiting.
        if user32.SetWindowPos(hwnd, foreground, 0, 0, 0, 0, flags):
            self._waiting_for_click = False

    def _owns_widget(self, widget: QtWidgets.QWidget | None) -> bool:
        """Whether ``widget`` is this overlay or parented to it, across window
        boundaries (a menu is its own window, so `isAncestorOf` says no)."""
        while widget is not None:
            if widget is self:
                return True
            widget = widget.parentWidget()
        return False

    def _schedule_after_menu_closed(self) -> None:
        # One event-loop turn later: the popup is gone by then.
        QtCore.QTimer.singleShot(0, self._after_menu_closed)

    def _after_menu_closed(self) -> None:
        """Put a waiting overlay back above the editor its menu let in front.

        An open menu is the foreground window; when it closes, Windows
        re-activates the editor and raises it above the floating overlay
        (measured 2026-10-10), although the user never clicked into it. A
        press that closed the menu is judged now, against the editor.

        `HWND_TOP`, not `HWND_NOTOPMOST`: that "has no effect if the window
        is already a non-topmost window" (SetWindowPos docs; measured: the
        overlay stayed below the editor).
        """
        pressed, self._press_during_menu = self._press_during_menu, 0
        if not self._waiting_for_click or self._wants_topmost():
            return
        try:
            user32 = _overlay_user32()
            flags = _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOACTIVATE
            user32.SetWindowPos(int(self.winId()), _HWND_TOP, 0, 0, 0, 0, flags)
            if pressed:
                self._follow_press(user32, pressed)
        except Exception:
            logger.debug("Restoring the overlay after a menu failed", exc_info=True)
        finally:
            self._sync_click_watch()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_detail_height()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() != QtCore.Qt.LeftButton:
            super().mousePressEvent(event)
            return
        child = self.childAt(event.position().toPoint())
        if isinstance(child, QtWidgets.QAbstractButton):
            super().mousePressEvent(event)
            return
        self._drag_active = True
        self._drag_offset = (
            event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        )
        event.accept()

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if not self._drag_active:
            super().mouseMoveEvent(event)
            return
        target = event.globalPosition().toPoint() - self._drag_offset
        # Claim the manual position on the first movement, not on release. A
        # drag during startup competes with the preload's overlay updates, and
        # each of those repositions a not-yet-manual overlay back to its
        # configured corner — so the window jumped out from under the cursor.
        self._claim_manual_position(target)
        self.move(target)
        event.accept()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton and self._drag_active:
            self._drag_active = False
            self._claim_manual_position(self.pos())
            self._reposition_within_current_screen()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _show_detail_context_menu(self, pos) -> None:
        menu = QtWidgets.QMenu(self)
        menu.aboutToHide.connect(self._schedule_after_menu_closed)
        copy_action = menu.addAction("Copy text")
        clear_action = menu.addAction("Clear text from overlay")
        clear_action.setEnabled(self._clear_button.isEnabled())
        selected = menu.exec(self._detail_label.mapToGlobal(pos))
        if selected == copy_action:
            self.copy_detail_text()
        elif selected == clear_action:
            self.clear_detail_text()

    @contextlib.contextmanager
    def batched_update(self):
        """Apply several state changes as one visual step.

        Finishing a transcription first clears the queue panel and then
        publishes the result text. Applied one after the other, the window
        shrinks for the empty queue and grows again for the transcript: the
        user sees the window jump twice, and the frame in between shows the
        previous content at the already-changed size. Inside this context the
        content is updated normally but the geometry is recomputed once, at the
        end, and the resize plus repaint happen together.
        """
        self._geometry_batch_depth += 1
        try:
            yield
        finally:
            self._geometry_batch_depth -= 1
            if self._geometry_batch_depth == 0 and self._geometry_batch_dirty:
                self._geometry_batch_dirty = False
                self._commit_batched_geometry()

    def _defer_geometry(self) -> bool:
        if self._geometry_batch_depth <= 0:
            return False
        self._geometry_batch_dirty = True
        return True

    def _commit_batched_geometry(self) -> None:
        # Suppress painting across the resize so the window cannot be shown at
        # its new size with the old content still in the backing store, then
        # repaint synchronously so size and content land in the same frame.
        repaint_needed = self.isVisible() and self.updatesEnabled()
        if repaint_needed:
            self.setUpdatesEnabled(False)
        try:
            if self._compact_mode:
                self.ensure_compact_size()
            else:
                self._update_detail_height()
        finally:
            if repaint_needed:
                self.setUpdatesEnabled(True)
                self.repaint()

    def _container_frame_margins(self) -> QtCore.QMargins:
        """Contents margins the styled container adds around the inner layout.

        The container's stylesheet border contributes 1 px per side. Ignoring
        it made every computed size 2 px smaller than the layout's real
        minimum, which both defeated ``OVERLAY_MAX_HEIGHT`` and left the window
        unable to reach its own computed target size.
        """
        self._container.ensurePolished()
        return self._container.contentsMargins()

    def _update_detail_height(self) -> None:
        if self._defer_geometry():
            return
        self._apply_queue_scroll_height()
        margins = self._layout.contentsMargins()
        spacing = self._layout.spacing()
        frame = self._container_frame_margins()
        frame_height = frame.top() + frame.bottom()
        height_cap = self._window_height_cap()
        target_window_width = self._target_window_width()
        target_content_width = max(
            80,
            target_window_width
            - frame.left()
            - frame.right()
            - margins.left()
            - margins.right()
            - 4,
        )

        header_height = self._header_widget.sizeHint().height()
        controls_height = self._controls_widget.sizeHint().height()
        footer_height = self._footer_widget.sizeHint().height()
        queue_extent = self._queue_extent()
        max_detail_height = max(
            OVERLAY_DETAIL_MIN_HEIGHT,
            height_cap
            - (
                frame_height
                + margins.top()
                + margins.bottom()
                + header_height
                + controls_height
                + footer_height
                + queue_extent
                + (spacing * 3)
            ),
        )

        # Wrap the detail text at a width derived from the *target* window
        # width, never from the live viewport: the viewport width changes
        # with deferred queue resizes and scrollbar visibility, and
        # re-wrapping the same text a moment later made it visibly jump.
        wrap_width = max(80, target_content_width - 2)
        self._detail_label.setFixedWidth(wrap_width)
        self._detail_label.adjustSize()
        content_height = self._detail_label.sizeHint().height()
        compact_detail_cap = max(
            OVERLAY_DETAIL_MIN_HEIGHT,
            min(max_detail_height, OVERLAY_COMPACT_DETAIL_MAX_HEIGHT),
        )
        shown_detail_height = (
            compact_detail_cap if self._compact_mode else max_detail_height
        )
        if content_height + 6 > shown_detail_height:
            # The vertical scrollbar will appear; re-wrap once at the final
            # (narrower) width so the layout is stable from the start.
            scrollbar_width = self._detail_scroll.verticalScrollBar().sizeHint().width()
            narrowed_width = max(80, wrap_width - scrollbar_width)
            if narrowed_width != wrap_width:
                self._detail_label.setFixedWidth(narrowed_width)
                self._detail_label.adjustSize()
                content_height = self._detail_label.sizeHint().height()

        if self._compact_mode:
            # Grow to fit rather than pinning to the minimum: pinning clipped
            # the hotkey notice, and the user could only reach it by scrolling
            # a two-line box.
            desired_detail_height = max(
                OVERLAY_DETAIL_MIN_HEIGHT,
                min(compact_detail_cap, content_height + 6),
            )
        else:
            desired_detail_height = max(
                OVERLAY_DETAIL_MIN_HEIGHT,
                min(max_detail_height, content_height + 6),
            )
        self._detail_scroll.setFixedHeight(desired_detail_height)

        if self._compact_mode:
            # Only the overflow is added, so short status text still produces
            # exactly the captured compact size that ensure_compact_size() uses.
            desired_window_height = self._compact_target_size().height() + (
                desired_detail_height - OVERLAY_DETAIL_MIN_HEIGHT
            )
        else:
            desired_window_height = (
                frame_height
                + margins.top()
                + margins.bottom()
                + header_height
                + controls_height
                + footer_height
                + queue_extent
                + (spacing * 3)
                + desired_detail_height
            )
        desired_window_height = self._bounded_window_height(desired_window_height)
        self._resize_window(QtCore.QSize(target_window_width, desired_window_height))
        self._reposition_within_current_screen()

    def _resize_window(self, target: QtCore.QSize) -> None:
        """Resize the overlay window, refreshing the layout constraints first.

        ``QWidget.resize`` clamps the requested size to the widget's current
        minimum size, and that minimum is only recomputed when the layout is
        activated (normally deferred to the next event loop pass). Right after
        shrinking the detail/queue areas the window therefore still carries the
        *previous* state's larger minimum, which silently swallowed the resize:
        a long transcript followed by a short error message left the overlay at
        its expanded height. Activating the layouts makes the new minimum take
        effect before the resize, so growing and shrinking both work.
        """
        if self.size() == target:
            return
        self._layout.activate()
        root_layout = self.layout()
        if root_layout is not None:
            root_layout.activate()
        if self.size() != target:
            self.resize(target)

    def _compact_window_height(self) -> int:
        margins = self._layout.contentsMargins()
        spacing = self._layout.spacing()
        frame = self._container_frame_margins()
        return (
            frame.top()
            + frame.bottom()
            + margins.top()
            + margins.bottom()
            + self._header_widget.sizeHint().height()
            + self._controls_widget.sizeHint().height()
            + self._footer_widget.sizeHint().height()
            + self._queue_extent()
            + (spacing * 3)
            + OVERLAY_DETAIL_MIN_HEIGHT
        )

    def _compact_target_size(self) -> QtCore.QSize:
        if self._initial_compact_size is not None:
            # The captured baseline excludes the queue panel; add its extent so
            # compact mode grows to fit any visible queue rows.
            target_height = self._bounded_window_height(
                self._initial_compact_size.height() + self._queue_extent()
            )
            return QtCore.QSize(
                self._initial_compact_size.width(),
                target_height,
            )
        return QtCore.QSize(
            self._target_window_width(),
            self._bounded_window_height(self._compact_window_height()),
        )

    def _window_height_cap(self) -> int:
        if not self._queue_visible:
            return OVERLAY_MAX_HEIGHT
        # With a queue the overlay may grow past the normal transcript cap, but
        # it stays bounded (the queue scrolls beyond this) instead of expanding
        # to full screen height. Never exceed the current screen.
        cap = OVERLAY_QUEUE_MAX_HEIGHT
        screen = self._current_screen()
        if screen is not None:
            available = screen.availableGeometry().height() - (OVERLAY_MARGIN_Y * 2)
            cap = min(cap, max(OVERLAY_HEIGHT, available))
        return max(OVERLAY_HEIGHT, cap)

    def _apply_queue_scroll_height(self) -> None:
        """Bound the scrollable queue panel so the detail area keeps its room.

        The queue rows get as much height as fits within the window cap after
        the fixed chrome and a minimum detail area; anything beyond scrolls.
        """
        if not self._queue_visible or not hasattr(self, "_queue_scroll"):
            return
        margins = self._layout.contentsMargins()
        spacing = self._layout.spacing()
        frame = self._container_frame_margins()
        non_queue_fixed = (
            frame.top()
            + frame.bottom()
            + margins.top()
            + margins.bottom()
            + self._header_widget.sizeHint().height()
            + self._controls_widget.sizeHint().height()
            + self._footer_widget.sizeHint().height()
            + (spacing * 3)
        )
        queue_layout = self._queue_widget.layout()
        queue_spacing = queue_layout.spacing() if queue_layout is not None else 0
        queue_overhead = (
            self._queue_header_widget.sizeHint().height()
            + queue_spacing
            + spacing  # main-layout gap before the queue block (see _queue_extent)
        )
        available_for_rows = (
            self._window_height_cap()
            - non_queue_fixed
            - queue_overhead
            - OVERLAY_DETAIL_MIN_HEIGHT
        )
        # Measure via the rows layout, not the widget: the widget's own
        # sizeHint is inflated by the minimum height we set below, which would
        # make the measurement self-reinforcing across queue changes.
        rows_layout = self._queue_rows_widget.layout()
        natural = (
            rows_layout.sizeHint().height()
            if rows_layout is not None
            else self._queue_rows_widget.sizeHint().height()
        )
        # Keep the rows widget at its full content height so the scroll area
        # actually scrolls (widgetResizable would otherwise compress the rows to
        # fit the viewport, hiding the overflow instead of scrolling it).
        if self._queue_rows_widget.minimumHeight() != natural:
            self._queue_rows_widget.setMinimumHeight(natural)
        rows_height = min(natural, max(OVERLAY_QUEUE_MIN_HEIGHT, available_for_rows))
        rows_height = max(0, int(rows_height))
        if self._queue_scroll.height() != rows_height:
            self._queue_scroll.setFixedHeight(rows_height)

    def _bounded_window_height(self, desired_height: int) -> int:
        return max(
            OVERLAY_HEIGHT,
            min(self._window_height_cap(), int(desired_height)),
        )

    def _target_window_width(self) -> int:
        margins = self._layout.contentsMargins()
        frame = self._container_frame_margins()
        chrome_width = frame.left() + frame.right() + margins.left() + margins.right()
        content_width = max(
            OVERLAY_WIDTH - chrome_width,
            self._header_widget.sizeHint().width(),
            self._controls_widget.sizeHint().width(),
            self._footer_widget.sizeHint().width(),
        )
        return max(OVERLAY_WIDTH, content_width + chrome_width)

    def _should_preserve_size_on_reset(self) -> bool:
        return bool(self._detail_label.text().strip()) and (
            self._state_label.text() in {"Done", "Error"}
        )

    def ensure_compact_size_unless_showing_a_result(self) -> None:
        """Return to the compact box, but never over a transcript or an error.

        `ensure_compact_size` pins the detail area back to the compact cap and
        sets `_compact_mode`, so applying it to a finished `Done` truncates the
        transcript the user is reading -- scrolled to the *top*, so the end of
        the dictation is what disappears -- and leaves the overlay in compact
        mode under a `Done` label, where every later reveal keeps it small.
        Reset Pos has always made this distinction; the settings-save and
        clear-text paths called `ensure_compact_size` outright.
        """
        if self._should_preserve_size_on_reset():
            self._update_detail_height()
            return
        self.ensure_compact_size()

    def ensure_compact_size(self) -> None:
        self._compact_mode = True
        if self._defer_geometry():
            return
        self._apply_queue_scroll_height()
        self._detail_scroll.setFixedHeight(OVERLAY_DETAIL_MIN_HEIGHT)
        self._resize_window(self._compact_target_size())
        self._update_detail_height()

    # -- Transcription queue panel -------------------------------------------

    def _build_queue_widget(self) -> None:
        self._queue_widget = QtWidgets.QWidget()
        self._queue_widget.setObjectName("overlayQueue")
        queue_layout = QtWidgets.QVBoxLayout(self._queue_widget)
        queue_layout.setContentsMargins(0, 0, 0, 0)
        queue_layout.setSpacing(2)

        self._queue_header_widget = QtWidgets.QWidget()
        queue_header = QtWidgets.QHBoxLayout(self._queue_header_widget)
        queue_header.setContentsMargins(0, 0, 0, 0)
        queue_header.setSpacing(6)
        # Eliding: beside the badge a long title must give way, never widen
        # the overlay (its policy lets the layout ignore its text width).
        self._queue_title_label = ElidingLabel("")
        # How many transcripts were not inserted and the hotkey that inserts
        # them (owner's request 2026-10-09): amber, apart from Error red and
        # Listening green, and here because no state change touches the
        # queue panel, so a recording that starts cannot paint over it. Its
        # height is the Clear queue button's (`_fit_buttons_to_font`), so
        # showing it never changes the header row's height; only the title
        # beside it gets narrower.
        self._not_inserted_badge = QtWidgets.QLabel("")
        self._not_inserted_badge.setObjectName("overlayNotInsertedBadge")
        self._not_inserted_badge.setAlignment(QtCore.Qt.AlignCenter)
        self._not_inserted_badge.setTextFormat(QtCore.Qt.PlainText)
        self._not_inserted_badge.setSizePolicy(
            QtWidgets.QSizePolicy.Preferred,
            QtWidgets.QSizePolicy.Fixed,
        )
        # An explicit minimum, so the layout's minimum width does not grow by
        # the badge's text (a QLabel's minimum size hint is all of it).
        self._not_inserted_badge.setMinimumWidth(1)
        self._not_inserted_badge.setVisible(False)
        self._queue_clear_button = QtWidgets.QPushButton("Clear queue")
        self._queue_clear_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._queue_clear_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._queue_clear_button.setFixedHeight(_QUEUE_CLEAR_BUTTON_HEIGHT)
        self._queue_clear_button.setToolTip(
            "Cancel all queued and running transcriptions and dismiss the "
            "transcripts that were not inserted."
        )
        self._queue_clear_button.clicked.connect(self.queue_clear_requested.emit)
        queue_header.addWidget(self._queue_title_label, 1)
        queue_header.addWidget(self._not_inserted_badge, 0)
        queue_header.addWidget(self._queue_clear_button, 0, QtCore.Qt.AlignRight)
        queue_layout.addWidget(self._queue_header_widget)

        self._queue_rows_widget = QtWidgets.QWidget()
        self._queue_rows_layout = QtWidgets.QVBoxLayout(self._queue_rows_widget)
        self._queue_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._queue_rows_layout.setSpacing(2)

        # Scroll the queue rows (like the transcript detail) so a long queue is
        # fully viewable while the overlay stays bounded instead of growing to
        # full screen height.
        self._queue_scroll = QtWidgets.QScrollArea()
        self._queue_scroll.setWidgetResizable(True)
        self._queue_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._queue_scroll.setFocusPolicy(QtCore.Qt.NoFocus)
        self._queue_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._queue_scroll.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self._queue_scroll.setWidget(self._queue_rows_widget)
        queue_layout.addWidget(self._queue_scroll)

        self._queue_widget.setVisible(False)
        self._queue_entries: list[tuple[int, str, str]] = []

    def _clear_queue_rows(self) -> None:
        while self._queue_rows_layout.count():
            item = self._queue_rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def _build_queue_row(self, token: int, label: str, kind: str) -> QtWidgets.QWidget:
        row = QtWidgets.QWidget()
        row_layout = QtWidgets.QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        # One line, elided, with the whole label in its tooltip: wrapped, a
        # long label grew its row, and a label that changes in place -- a
        # failed re-paste turns "Not inserted" into "Possibly inserted, check
        # the window" -- grew the rows from 32 to 40 px and the overlay from
        # 230 to 246 px under the user's eyes.
        text_label = ElidingLabel(str(label))
        text_label.setTextFormat(QtCore.Qt.PlainText)
        # The stylesheet colours a waiting insert apart from a transcription;
        # colour only, so the row's size never depends on its kind.
        text_label.setProperty("queueRowKind", kind)
        text_label.setMinimumWidth(0)
        caption, tooltip = _QUEUE_ROW_BUTTONS[kind]
        cancel_button = QtWidgets.QPushButton(caption)
        cancel_button.setCursor(QtCore.Qt.PointingHandCursor)
        cancel_button.setFocusPolicy(QtCore.Qt.NoFocus)
        cancel_button.setFixedSize(
            _QUEUE_CANCEL_BUTTON_WIDTH, _QUEUE_CANCEL_BUTTON_HEIGHT
        )
        cancel_button.setToolTip(tooltip)
        cancel_button.clicked.connect(
            lambda _checked=False, t=int(token): self.queue_cancel_requested.emit(t)
        )
        row_layout.addWidget(text_label, 1)
        row_layout.addWidget(cancel_button, 0, QtCore.Qt.AlignRight)
        return row

    def set_transcription_queue(self, items) -> None:
        """Render the in-flight transcription queue with per-item cancel.

        ``items`` is a list of ``(token, label)`` or ``(token, label, kind)``
        entries; ``kind`` is `QUEUE_ROW_KIND_TRANSCRIPTION` (the default, a
        Cancel button) or `QUEUE_ROW_KIND_UNDELIVERED` (a finished transcript
        that was not inserted, a Dismiss button). Both buttons emit
        `queue_cancel_requested` with the row's token. An empty list hides
        the queue panel entirely.
        """
        entries = [_queue_entry(item) for item in (items or [])]
        if entries == self._queue_entries:
            # Nothing about the queue changed, and a rebuild is not free: it
            # deletes every row widget, so the panel scrolls back to the top
            # and a Cancel press whose release lands after the rebuild hits a
            # button that no longer exists. The geometry already matches these
            # rows, because the call that rendered them computed it.
            return
        self._queue_entries = entries
        scroll_bar = self._queue_scroll.verticalScrollBar()
        previous_scroll = scroll_bar.value() if self._queue_visible else 0
        self._clear_queue_rows()
        if entries:
            self._sync_queue_title()
            for token, label, kind in entries:
                row = self._build_queue_row(token, label, kind)
                self._queue_rows_layout.addWidget(row)
                # Qt shows a widget added to a visible parent only once the
                # event loop delivers its ShowToParent event, and a hidden
                # widget's layout item reports itself empty. So without this
                # every measurement below saw a rows layout of height 0 --
                # measured: 0 synchronously against 42 and 64 px afterwards --
                # and the whole geometry pass ran for an empty queue. Inside
                # `batched_update` that is worse than a stale number, because
                # the batch repaints synchronously: the user got a real frame
                # with the rows area collapsed, then a second resize one turn
                # later. Only the *first* render escaped it, because
                # `setVisible(True)` on the panel shows its children with it.
                row.show()
                # Grown only now, and this is the one moment it can be done.
                # Measured detached the same button reports 81x26 at 9 pt
                # against its real 54x18: outside the container it carries the
                # platform's default padding instead of the overlay
                # stylesheet's, so sizing it in `_build_queue_row` would pin a
                # number 23 px too wide at the default font. Attached it is
                # accurate, and it needs to grow: 62x22 at 11.25 pt and 89x34
                # at 18 pt against the pinned 58x20, on the one control that
                # cancels a runaway transcription.
                cancel_button = row.findChild(QtWidgets.QPushButton)
                if cancel_button is not None:
                    _fit_button(
                        cancel_button,
                        _QUEUE_CANCEL_BUTTON_WIDTH,
                        _QUEUE_CANCEL_BUTTON_HEIGHT,
                        *_QUEUE_ROW_BUTTON_CAPTIONS,
                    )
            self._queue_visible = True
            self._queue_widget.setVisible(True)
        else:
            self._queue_visible = False
            self._queue_widget.setVisible(False)

        if entries and previous_scroll:
            restore_vertical_scrollbar(self._queue_scroll, previous_scroll)
        self._refresh_queue_layout_geometry()
        self._refresh_size_after_queue_change()
        # Re-assert once the layout settles. Switching between very different
        # queue sizes (or hiding a queue that had grown the window) can leave a
        # stale pending resize from the previous state, so recompute the size
        # after the event loop drains.
        QtCore.QTimer.singleShot(0, self._refresh_size_after_queue_change)

    def set_not_inserted_badge(self, text: str, tooltip: str = "") -> None:
        """Show how many transcripts were not inserted, or hide with "".

        The controller writes the count and the re-paste hotkey's label
        ("2 not inserted · Ctrl+Alt+F10"); the badge sits in the queue
        panel's header, which the waiting rows keep visible.
        """
        text = str(text or "")
        badge = self._not_inserted_badge
        if text == badge.text() and str(tooltip) == badge.toolTip():
            return
        badge.setText(text)
        badge.setToolTip(str(tooltip))
        badge.setVisible(bool(text))
        self._sync_queue_title()

    def _sync_queue_title(self) -> None:
        self._queue_title_label.setText(
            _queue_title(
                self._queue_entries,
                badge_shown=bool(self._not_inserted_badge.text()),
            )
            if self._queue_entries
            else ""
        )

    def _queue_extent(self) -> int:
        if not self._queue_visible:
            return 0
        return self._queue_widget.sizeHint().height() + self._layout.spacing()

    def _refresh_queue_layout_geometry(self) -> None:
        self._queue_rows_widget.updateGeometry()
        self._queue_widget.updateGeometry()
        self._layout.invalidate()
        self._layout.activate()

    def _refresh_size_after_queue_change(self) -> None:
        if self._compact_mode:
            self.ensure_compact_size()
        else:
            self._update_detail_height()

    def _current_screen(self) -> QtGui.QScreen | None:
        frame = self.frameGeometry()
        for point in (frame.center(), frame.topLeft(), self.pos()):
            screen = QtGui.QGuiApplication.screenAt(point)
            if screen is not None:
                return screen
        handle = self.windowHandle()
        if handle is not None and handle.screen() is not None:
            return handle.screen()
        return QtGui.QGuiApplication.primaryScreen()

    def _position_for_corner(
        self,
        screen: QtGui.QScreen,
        corner: str,
    ) -> QtCore.QPoint:
        geometry = screen.availableGeometry()
        normalized = str(corner or "top-right").strip().lower()
        if normalized.endswith("left"):
            x = geometry.left() + OVERLAY_MARGIN_X
        else:
            x = geometry.right() - self.width() - OVERLAY_MARGIN_X
        if normalized.startswith("bottom"):
            y = geometry.bottom() - self.height() - OVERLAY_MARGIN_Y
        else:
            y = geometry.top() + OVERLAY_MARGIN_Y
        return QtCore.QPoint(x, y)

    def _claim_manual_position(self, point: QtCore.QPoint | None) -> None:
        """Record where the user wants the overlay, or that they never said.

        The flag and the anchor are one fact in two fields, and a writer
        that sets only the flag leaves the previous drag's anchor behind.
        Both are written here and nowhere else."""
        self._manual_positioned = point is not None
        self._manual_anchor = QtCore.QPoint(point) if point is not None else None

    def _clamp_point_to_screen(
        self,
        point: QtCore.QPoint,
        screen: QtGui.QScreen,
    ) -> QtCore.QPoint:
        geometry = screen.availableGeometry()
        max_x = geometry.right() - self.width()
        max_y = geometry.bottom() - self.height()
        clamped_x = max(geometry.left(), min(point.x(), max_x))
        clamped_y = max(geometry.top(), min(point.y(), max_y))
        return QtCore.QPoint(clamped_x, clamped_y)

    def _reposition_within_current_screen(self) -> None:
        if self._drag_active:
            # The user is positioning the window right now; nothing may move it
            # until the drag ends (mouseReleaseEvent runs the final clamp).
            return
        screen = self._current_screen()
        if screen is None:
            return

        if not self._manual_positioned and self._initial_corner:
            target = self._position_for_corner(screen, self._initial_corner)
        else:
            # Clamp where the user put it, not where it currently is. Both
            # branches used to clamp `self.pos()`, and `previous_size` was
            # accepted and never read -- so a transcript tall enough to run off
            # the bottom pushed the window up to fit and the next state took
            # that pushed-up position as the new one. Measured on a 1392 px
            # screen: an overlay dragged to y=1233 came back to y=1097 and
            # stayed there, and the ceiling is what the tallest state ever
            # shown costs, up to `OVERLAY_MAX_HEIGHT` (392 px) or more with a
            # queue.
            target = QtCore.QPoint(self._manual_anchor or self.pos())
            target = self._clamp_point_to_screen(target, screen)

        if target != self.pos():
            self.move(target)

    def _on_opacity_slider_changed(self, value: int) -> None:
        self.set_opacity_percent(value, emit_signal=True)

    def set_opacity_percent(self, value: int, *, emit_signal: bool = False) -> None:
        clamped = max(
            OVERLAY_OPACITY_MIN_PERCENT,
            min(OVERLAY_OPACITY_MAX_PERCENT, int(value)),
        )
        slider_value = int(self._opacity_slider.value())
        if slider_value != clamped:
            blocker = QtCore.QSignalBlocker(self._opacity_slider)
            self._opacity_slider.setValue(clamped)
            del blocker
        self._opacity_value_label.setText(f"{clamped}%")
        self.setWindowOpacity(clamped / 100.0)
        if emit_signal:
            self.opacity_changed.emit(clamped)

    def _set_copy_button_feedback(self, copied: bool) -> None:
        copied = bool(copied)
        # Before the guard: `setText` is itself guarded by Qt, so it stays
        # outside and the caption can never be left behind by an early return.
        self._copy_button.setText(
            COPY_BUTTON_COPIED_TEXT if copied else COPY_BUTTON_TEXT
        )
        if bool(self._copy_button.property("copied")) is copied:
            # `set_state` resets this, and streaming calls `set_state` about
            # three times a second, so without the guard every partial forced
            # a full stylesheet re-resolution and repaint of an unchanged
            # button. The shared `ui_feedback.set_button_feedback_state` opens
            # with exactly this check; this private copy omitted it.
            return
        self._copy_button.setProperty("copied", copied)
        self._copy_button.style().unpolish(self._copy_button)
        self._copy_button.style().polish(self._copy_button)
        self._copy_button.update()

    def _reset_copy_button_feedback(self) -> None:
        self._set_copy_button_feedback(False)

    def copy_detail_text(self) -> None:
        text = self._copy_text or self._detail_label.text()
        if not text:
            return
        try:
            QtGui.QGuiApplication.clipboard().setText(text)
        except Exception:
            return
        self._set_copy_button_feedback(True)
        self._copy_feedback_timer.start()

    def clear_detail_text(self) -> None:
        if not self._detail_label.text().strip():
            return
        cleared_copy_text = self._copy_text or ""
        self.set_state("Idle", self._idle_default_detail, compact=True)
        self.ensure_compact_size()
        # After the clear, so a controller that looks sees Idle. It used to
        # learn nothing: a pending Insert offer dismissed here came back
        # with the next status paint, 600 ms later during a preload.
        self.detail_cleared.emit(cleared_copy_text)
        # Deferred, and therefore no longer about the state it was clearing: a
        # queued transcription delivering inside that one event-loop turn puts
        # a transcript on screen, and an unconditional `ensure_compact_size`
        # then shrinks the box around it. The policy call re-checks.
        QtCore.QTimer.singleShot(0, self.ensure_compact_size_unless_showing_a_result)
