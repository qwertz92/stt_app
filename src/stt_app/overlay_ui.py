from __future__ import annotations

import contextlib
import logging
import sys

from PySide6 import QtCore, QtGui, QtWidgets

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
)
from .ui_feedback import restore_vertical_scrollbar

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

# Language button chrome around its caption: the stylesheet reserves 8 px on
# the left and 26 px on the right for the chevron, plus a 1 px border per side
# and 2 px of rounding headroom.
_LANGUAGE_BUTTON_CHROME_PX = 38

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
                self._ARROW_COLOR
                if self.isEnabled()
                else self._DISABLED_ARROW_COLOR,
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
    queue_cancel_requested = QtCore.Signal(int)
    queue_clear_requested = QtCore.Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Dictation")

        self._always_on_top = True
        self._temporary_foreground_active = False
        self._temporary_foreground_uses_window_flag = False
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
        self._idle_default_detail = OVERLAY_INITIAL_DETAIL
        self._manual_positioned = False
        self._screen_change_connected = False
        self._state_background = ""
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
            for s in ("Idle", "Listening", "Processing", "Done", "Error")
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
        self._language_button.setFixedSize(
            self._widest_language_caption_width(), 22
        )
        self._language_menu = QtWidgets.QMenu(self._language_button)
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

        self._footer_widget = QtWidgets.QWidget()
        footer = QtWidgets.QHBoxLayout(self._footer_widget)
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(8)
        self._opacity_caption = QtWidgets.QLabel("Opacity")
        self._opacity_caption.setSizePolicy(
            QtWidgets.QSizePolicy.Fixed,
            QtWidgets.QSizePolicy.Fixed,
        )
        self._opacity_value_label = QtWidgets.QLabel("")
        self._opacity_value_label.setMinimumWidth(40)
        self._opacity_value_label.setAlignment(
            QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter
        )
        self._opacity_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
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
        footer.addWidget(self._opacity_caption)
        footer.addWidget(self._opacity_slider, 1)
        footer.addWidget(self._opacity_value_label)

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
        # Equalise the two button groups before they are laid out, so the
        # stretching state label between them is centred on the header — and
        # therefore on the overlay, whose horizontal margins are symmetric.
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
        self._layout.activate()
        self.layout().activate()
        self._initial_compact_size = QtCore.QSize(
            self._target_window_width(), self._compact_window_height()
        )
        self.set_state("Idle", OVERLAY_INITIAL_DETAIL)
        self.set_opacity_percent(DEFAULT_OVERLAY_OPACITY_PERCENT, emit_signal=False)
        self._sync_always_on_top_button()

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

    def _base_window_flags(self) -> QtCore.Qt.WindowType:
        flags = QtCore.Qt.Tool | QtCore.Qt.FramelessWindowHint
        if self._always_on_top or self._temporary_foreground_uses_window_flag:
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

    def _apply_window_flags(self, *, raise_window: bool = False) -> bool | None:
        desired_flags = self._base_window_flags()
        if getattr(self, "_applied_window_flags", None) != desired_flags:
            # ``setWindowFlags`` destroys and recreates the native window,
            # which shows as a visible blink. Reveals fire on every hotkey
            # press, so only pay that cost when the flags actually change.
            was_visible = self.isVisible()
            self.setWindowFlags(desired_flags)
            self._applied_window_flags = desired_flags
            if was_visible or raise_window:
                self.show()
        elif raise_window and not self.isVisible():
            self.show()
        if raise_window:
            self.raise_()
        if sys.platform == "win32":
            self._apply_noactivate_style()
            return self._apply_native_z_order()
        return None

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
            self._temporary_foreground_uses_window_flag = False
            self._temporary_foreground_timer.stop()
        self._sync_always_on_top_button()
        self._apply_window_flags(raise_window=normalized)
        if emit_signal:
            self.always_on_top_changed.emit(normalized)

    def reveal_temporarily(self, duration_ms: int = 1800) -> None:
        if not self._always_on_top:
            self._temporary_foreground_active = True
            self._temporary_foreground_timer.start(max(1, int(duration_ms)))
        native_z_order_applied = self._apply_window_flags(raise_window=True)
        if (
            not self._always_on_top
            and sys.platform == "win32"
            and native_z_order_applied is False
        ):
            self._temporary_foreground_uses_window_flag = True
            self._apply_window_flags(raise_window=True)
        self._reposition_within_current_screen()

    def restore_visibility(self) -> None:
        """Restore overlay visibility and native z-order after a system resume."""
        self.reveal_temporarily()

    def _clear_temporary_foreground(self) -> None:
        if self._always_on_top or not self._temporary_foreground_active:
            return
        self._temporary_foreground_active = False
        self._temporary_foreground_uses_window_flag = False
        self._apply_window_flags()

    def set_state(
        self,
        state: str,
        detail: str = "",
        *,
        compact: bool | None = None,
        copy_text: str | None = None,
        error_action: str | None = None,
    ) -> None:
        """Render an overlay state.

        ``copy_text`` overrides what the Copy action puts into the clipboard.
        It is used when the detail area shows more than the plain transcript
        (an insertion error plus the transcript preview, for example), so Copy
        still yields exactly the transcript.

        ``error_action`` selects the follow-up action offered in the Error
        state: ``OVERLAY_ERROR_ACTION_INSERT`` when the transcription itself
        succeeded and only the insertion failed, otherwise Retry.
        """
        if state == "Idle" and detail.strip():
            self._idle_default_detail = detail
        self._state_label.setText(state)
        self._detail_label.setText(detail)
        self._copy_text = copy_text
        has_detail = bool(detail.strip())
        if compact is None:
            self._compact_mode = state in {"Idle", "Listening", "Processing"}
        else:
            self._compact_mode = compact
        self._copy_button.setEnabled(has_detail or bool(copy_text))
        self._edit_button.setEnabled(has_detail and state == "Done")
        self._clear_button.setEnabled(has_detail and state in {"Done", "Error"})
        self._sync_record_button(state)
        self._sync_action_slot(state, error_action)
        self._reset_pos_button.setEnabled(True)
        self._language_change_blocked = state in {"Listening", "Processing"}
        self._sync_language_button()
        self._reset_copy_button_feedback()
        # Style before measuring: the container's stylesheet border becomes
        # part of its contents margins, so measuring first would size the
        # window for an unstyled container and leave it below its own layout
        # minimum (the window then refused to shrink to the computed target).
        self._apply_state_stylesheet(state)
        self._update_detail_height()
        scrollbar = self._detail_scroll.verticalScrollBar()
        # Errors lead with the reason and may be followed by a long transcript
        # preview, so keep the reason in view; every other state shows the end
        # of the transcript.
        scrollbar.setValue(0 if state == "Error" else scrollbar.maximum())

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
            is_error
            and not show_insert
            and error_action != OVERLAY_ERROR_ACTION_NONE
        )
        self._retry_button.setEnabled(show_retry)
        self._insert_button.setEnabled(show_insert)
        self._cancel_button.setEnabled(state in {"Listening", "Processing"})
        self._retry_button.setVisible(show_retry)
        self._insert_button.setVisible(show_insert)
        self._cancel_button.setVisible(not (show_retry or show_insert))

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
        self._language_menu.clear()
        for mode in self._language_modes:
            action = self._language_menu.addAction(
                LANGUAGE_MODE_LABELS.get(mode, mode)
            )
            action.setCheckable(True)
            action.setChecked(mode == self._language_mode)
            action.triggered.connect(
                lambda _checked=False, value=mode: self._select_language(value)
            )
        self._sync_language_button()

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
            tooltip = (
                f"Language is fixed to {label} for the selected engine and model."
            )
        else:
            tooltip = f"Current language: {label}. Click to change it."
        self._language_button.setToolTip(tooltip)

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
        self._manual_positioned = False

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
        self._manual_positioned = True

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
        self._manual_positioned = self._initial_corner is None

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
            except Exception:
                pass
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
            import ctypes

            hwnd = int(self.winId())
            _GWL_EXSTYLE = -20
            _WS_EX_NOACTIVATE = 0x08000000
            style = ctypes.windll.user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, _GWL_EXSTYLE, style | _WS_EX_NOACTIVATE
            )
        except Exception:
            pass

    def _apply_native_z_order(self) -> bool:
        """Reassert topmost state without activating the overlay on Windows."""
        if sys.platform != "win32":
            return False
        try:
            import ctypes
            import ctypes.wintypes

            set_window_pos = ctypes.WinDLL("user32", use_last_error=True).SetWindowPos
            set_window_pos.argtypes = (
                ctypes.wintypes.HWND,
                ctypes.wintypes.HWND,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.wintypes.UINT,
            )
            set_window_pos.restype = ctypes.wintypes.BOOL
            hwnd = ctypes.wintypes.HWND(int(self.winId()))
            insert_after = ctypes.wintypes.HWND(-1 if (
                self._always_on_top or self._temporary_foreground_active
            ) else -2)
            flags = 0x0001 | 0x0002 | 0x0010 | 0x0040
            return bool(set_window_pos(
                hwnd,
                insert_after,
                0,
                0,
                0,
                0,
                flags,
            ))
        except Exception:
            return False

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
        self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
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
        self._manual_positioned = True
        self.move(target)
        event.accept()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton and self._drag_active:
            self._drag_active = False
            self._manual_positioned = True
            self._reposition_within_current_screen()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _show_detail_context_menu(self, pos) -> None:
        menu = QtWidgets.QMenu(self)
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
        previous_size = QtCore.QSize(self.size())
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
            scrollbar_width = (
                self._detail_scroll.verticalScrollBar().sizeHint().width()
            )
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
        self._reposition_within_current_screen(previous_size)

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
        chrome_width = (
            frame.left() + frame.right() + margins.left() + margins.right()
        )
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
        self._queue_title_label = QtWidgets.QLabel("")
        self._queue_title_label.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Fixed,
        )
        self._queue_clear_button = QtWidgets.QPushButton("Clear queue")
        self._queue_clear_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._queue_clear_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._queue_clear_button.setFixedHeight(20)
        self._queue_clear_button.setToolTip(
            "Cancel all queued and running transcriptions."
        )
        self._queue_clear_button.clicked.connect(self.queue_clear_requested.emit)
        queue_header.addWidget(self._queue_title_label, 1)
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
        self._queue_entries: list[tuple[int, str]] = []

    def _clear_queue_rows(self) -> None:
        while self._queue_rows_layout.count():
            item = self._queue_rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def _build_queue_row(self, token: int, label: str) -> QtWidgets.QWidget:
        row = QtWidgets.QWidget()
        row_layout = QtWidgets.QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        text_label = QtWidgets.QLabel(str(label))
        text_label.setTextFormat(QtCore.Qt.PlainText)
        text_label.setWordWrap(True)
        text_label.setToolTip(str(label))
        text_label.setMinimumWidth(0)
        text_label.setSizePolicy(
            QtWidgets.QSizePolicy.Expanding,
            QtWidgets.QSizePolicy.Preferred,
        )
        cancel_button = QtWidgets.QPushButton("Cancel")
        cancel_button.setCursor(QtCore.Qt.PointingHandCursor)
        cancel_button.setFocusPolicy(QtCore.Qt.NoFocus)
        cancel_button.setFixedSize(58, 20)
        cancel_button.setToolTip("Cancel this transcription.")
        cancel_button.clicked.connect(
            lambda _checked=False, t=int(token): self.queue_cancel_requested.emit(t)
        )
        row_layout.addWidget(text_label, 1)
        row_layout.addWidget(cancel_button, 0, QtCore.Qt.AlignRight)
        return row

    def set_transcription_queue(self, items) -> None:
        """Render the in-flight transcription queue with per-item cancel.

        ``items`` is a list of ``(token, label)`` pairs. An empty list hides
        the queue panel entirely.
        """
        entries = [(int(token), str(label)) for token, label in (items or [])]
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
            count = len(entries)
            self._queue_title_label.setText(
                f"Transcribing {count} file" + ("" if count == 1 else "s")
            )
            for token, label in entries:
                row = self._build_queue_row(token, label)
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

    def _reposition_within_current_screen(
        self,
        previous_size: QtCore.QSize | None = None,
    ) -> None:
        if self._drag_active:
            # The user is positioning the window right now; nothing may move it
            # until the drag ends (mouseReleaseEvent runs the final clamp).
            return
        screen = self._current_screen()
        if screen is None:
            return

        target = QtCore.QPoint(self.pos())
        if not self._manual_positioned and self._initial_corner:
            target = self._position_for_corner(screen, self._initial_corner)
        elif previous_size is not None:
            target = self._clamp_point_to_screen(target, screen)
        else:
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
        self.set_state("Idle", self._idle_default_detail, compact=True)
        self.ensure_compact_size()
        # Deferred, and therefore no longer about the state it was clearing: a
        # queued transcription delivering inside that one event-loop turn puts
        # a transcript on screen, and an unconditional `ensure_compact_size`
        # then shrinks the box around it. The policy call re-checks.
        QtCore.QTimer.singleShot(
            0, self.ensure_compact_size_unless_showing_a_result
        )
