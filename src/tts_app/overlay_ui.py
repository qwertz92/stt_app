from __future__ import annotations

import sys

from PySide6 import QtCore, QtGui, QtWidgets

from .config import (
    DEFAULT_OVERLAY_OPACITY_PERCENT,
    OVERLAY_DETAIL_MIN_HEIGHT,
    OVERLAY_HEIGHT,
    OVERLAY_INITIAL_DETAIL,
    OVERLAY_MARGIN_X,
    OVERLAY_OPACITY_MAX_PERCENT,
    OVERLAY_OPACITY_MIN_PERCENT,
    OVERLAY_MARGIN_Y,
    OVERLAY_MAX_HEIGHT,
    OVERLAY_STATE_COLORS,
    OVERLAY_WIDTH,
)


class OverlayUI(QtWidgets.QWidget):
    history_requested = QtCore.Signal()
    retry_requested = QtCore.Signal()
    cancel_requested = QtCore.Signal()
    opacity_changed = QtCore.Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Dictation")

        flags = (
            QtCore.Qt.Tool
            | QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
        )
        if hasattr(QtCore.Qt, "WindowDoesNotAcceptFocus"):
            flags |= QtCore.Qt.WindowDoesNotAcceptFocus
        self.setWindowFlags(flags)
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)

        self._copy_feedback_timer = QtCore.QTimer(self)
        self._copy_feedback_timer.setSingleShot(True)
        self._copy_feedback_timer.setInterval(850)
        self._copy_feedback_timer.timeout.connect(self._reset_copy_button_feedback)
        self._drag_active = False
        self._drag_offset = QtCore.QPoint(0, 0)
        self._initial_position: QtCore.QPoint | None = None
        self._initial_corner: str | None = None
        self._compact_mode = False

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

        self._history_button = QtWidgets.QPushButton("History")
        self._history_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._history_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._history_button.setFixedWidth(68)
        self._history_button.setFixedHeight(24)
        self._history_button.setToolTip("Show recent transcriptions")
        self._history_button.clicked.connect(self.history_requested.emit)

        self._copy_button = QtWidgets.QPushButton("Copy")
        self._copy_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._copy_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._copy_button.setFixedWidth(64)
        self._copy_button.setFixedHeight(24)
        self._copy_button.setToolTip("Copy overlay text")
        self._copy_button.clicked.connect(self.copy_detail_text)

        self._retry_button = QtWidgets.QPushButton("Retry")
        self._retry_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._retry_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._retry_button.setFixedHeight(22)
        self._retry_button.clicked.connect(self.retry_requested.emit)

        self._cancel_button = QtWidgets.QPushButton("Cancel")
        self._cancel_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._cancel_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._cancel_button.setFixedHeight(22)
        self._cancel_button.clicked.connect(self.cancel_requested.emit)

        self._reset_pos_button = QtWidgets.QPushButton("Reset Pos")
        self._reset_pos_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._reset_pos_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._reset_pos_button.setFixedHeight(22)
        self._reset_pos_button.clicked.connect(self.reset_position)

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

        self._layout = QtWidgets.QVBoxLayout(container)
        self._layout.setContentsMargins(14, 10, 14, 10)
        self._layout.setSpacing(4)

        self._header_widget = QtWidgets.QWidget()
        header = QtWidgets.QHBoxLayout(self._header_widget)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        header.addWidget(self._history_button, 0, QtCore.Qt.AlignLeft)
        header.addWidget(self._state_label, 1)
        header.addWidget(self._copy_button, 0, QtCore.Qt.AlignRight)

        self._controls_widget = QtWidgets.QWidget()
        controls = QtWidgets.QHBoxLayout(self._controls_widget)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)
        controls.addWidget(self._retry_button)
        controls.addWidget(self._cancel_button)
        controls.addWidget(self._reset_pos_button)

        self._layout.addWidget(self._header_widget)
        self._layout.addWidget(self._controls_widget)
        self._layout.addWidget(self._detail_scroll)
        self._layout.addWidget(self._footer_widget)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(container)

        self.resize(OVERLAY_WIDTH, OVERLAY_HEIGHT)
        self.set_state("Idle", OVERLAY_INITIAL_DETAIL)
        self.set_opacity_percent(DEFAULT_OVERLAY_OPACITY_PERCENT, emit_signal=False)

    def set_state(self, state: str, detail: str = "", *, compact: bool | None = None) -> None:
        self._state_label.setText(state)
        self._detail_label.setText(detail)
        if compact is None:
            self._compact_mode = state in {"Idle", "Listening", "Processing"}
        else:
            self._compact_mode = compact
        self._copy_button.setEnabled(bool(detail.strip()))
        self._retry_button.setEnabled(state == "Error")
        self._cancel_button.setEnabled(state in {"Listening", "Processing"})
        self._reset_pos_button.setEnabled(True)
        self._reset_copy_button_feedback()
        self._update_detail_height()
        self._detail_scroll.verticalScrollBar().setValue(
            self._detail_scroll.verticalScrollBar().maximum()
        )

        bg = OVERLAY_STATE_COLORS.get(state, OVERLAY_STATE_COLORS["Idle"])
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

    def move_to_corner(self, corner: str = "top-right") -> None:
        screen = QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            return

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
        self.move(x, y)
        self._initial_position = QtCore.QPoint(x, y)
        self._initial_corner = normalized

    def set_initial_position(self, point: QtCore.QPoint) -> None:
        self._initial_position = QtCore.QPoint(point)
        self._initial_corner = None

    def reset_position(self) -> None:
        self.ensure_compact_size()
        if self._initial_corner:
            self.move_to_corner(self._initial_corner)
            return
        if self._initial_position is None:
            return
        target = QtCore.QPoint(self._initial_position)
        screen = QtGui.QGuiApplication.screenAt(target)
        if screen is None:
            screen = QtGui.QGuiApplication.primaryScreen()
        if screen is not None:
            geometry = screen.availableGeometry()
            max_x = geometry.right() - self.width()
            max_y = geometry.bottom() - self.height()
            clamped_x = max(geometry.left(), min(target.x(), max_x))
            clamped_y = max(geometry.top(), min(target.y(), max_y))
            target = QtCore.QPoint(clamped_x, clamped_y)
        self.move(target)

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
        if sys.platform == "win32":
            self._apply_noactivate_style()

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
        self.move(target)
        event.accept()

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.LeftButton and self._drag_active:
            self._drag_active = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _show_detail_context_menu(self, pos) -> None:
        menu = QtWidgets.QMenu(self)
        copy_action = menu.addAction("Copy text")
        selected = menu.exec(self._detail_label.mapToGlobal(pos))
        if selected == copy_action:
            self.copy_detail_text()

    def _update_detail_height(self) -> None:
        margins = self._layout.contentsMargins()
        spacing = self._layout.spacing()
        available_width = max(80, self._detail_scroll.viewport().width() - 2)
        if available_width <= 82:
            available_width = max(
                80,
                self.width() - margins.left() - margins.right() - 4,
            )
        self._detail_label.setFixedWidth(available_width)
        self._detail_label.adjustSize()

        content_height = self._detail_label.sizeHint().height()
        header_height = self._header_widget.sizeHint().height()
        controls_height = self._controls_widget.sizeHint().height()
        footer_height = self._footer_widget.sizeHint().height()
        max_detail_height = max(
            OVERLAY_DETAIL_MIN_HEIGHT,
            OVERLAY_MAX_HEIGHT
            - (
                margins.top()
                + margins.bottom()
                + header_height
                + controls_height
                + footer_height
                + (spacing * 3)
            ),
        )
        if self._compact_mode:
            desired_detail_height = OVERLAY_DETAIL_MIN_HEIGHT
        else:
            desired_detail_height = max(
                OVERLAY_DETAIL_MIN_HEIGHT,
                min(max_detail_height, content_height + 6),
            )
        self._detail_scroll.setFixedHeight(desired_detail_height)

        if self._compact_mode:
            desired_window_height = self._compact_window_height()
        else:
            desired_window_height = (
                margins.top()
                + margins.bottom()
                + header_height
                + controls_height
                + footer_height
                + (spacing * 3)
                + desired_detail_height
            )
        desired_window_height = max(
            OVERLAY_HEIGHT,
            min(OVERLAY_MAX_HEIGHT, desired_window_height),
        )
        if self.height() != desired_window_height:
            self.resize(self.width(), desired_window_height)

    def _compact_window_height(self) -> int:
        margins = self._layout.contentsMargins()
        spacing = self._layout.spacing()
        return (
            margins.top()
            + margins.bottom()
            + self._header_widget.sizeHint().height()
            + self._controls_widget.sizeHint().height()
            + self._footer_widget.sizeHint().height()
            + (spacing * 3)
            + OVERLAY_DETAIL_MIN_HEIGHT
        )

    def ensure_compact_size(self) -> None:
        self._compact_mode = True
        target_height = max(
            OVERLAY_HEIGHT,
            min(OVERLAY_MAX_HEIGHT, self._compact_window_height()),
        )
        if self.width() != OVERLAY_WIDTH or self.height() != target_height:
            self.resize(OVERLAY_WIDTH, target_height)
        self._detail_scroll.setFixedHeight(OVERLAY_DETAIL_MIN_HEIGHT)
        self._update_detail_height()

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
        self._copy_button.setProperty("copied", copied)
        self._copy_button.setText("Copied" if copied else "Copy")
        self._copy_button.style().unpolish(self._copy_button)
        self._copy_button.style().polish(self._copy_button)
        self._copy_button.update()

    def _reset_copy_button_feedback(self) -> None:
        self._set_copy_button_feedback(False)

    def copy_detail_text(self) -> None:
        text = self._detail_label.text()
        if not text:
            return
        try:
            QtGui.QGuiApplication.clipboard().setText(text)
        except Exception:
            return
        self._set_copy_button_feedback(True)
        self._copy_feedback_timer.start()
