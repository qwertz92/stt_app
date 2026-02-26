from __future__ import annotations

import sys

from PySide6 import QtCore, QtGui, QtWidgets

from .config import (
    OVERLAY_DETAIL_MIN_HEIGHT,
    OVERLAY_HEIGHT,
    OVERLAY_INITIAL_DETAIL,
    OVERLAY_MARGIN_X,
    OVERLAY_MARGIN_Y,
    OVERLAY_MAX_HEIGHT,
    OVERLAY_STATE_COLORS,
    OVERLAY_WIDTH,
)


class OverlayUI(QtWidgets.QWidget):
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

        self._state_label = QtWidgets.QLabel("Idle")
        self._state_label.setAlignment(QtCore.Qt.AlignCenter)
        state_font = QtGui.QFont()
        state_font.setBold(True)
        self._state_label.setFont(state_font)

        self._copy_button = QtWidgets.QPushButton("Copy")
        self._copy_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._copy_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._copy_button.setFixedWidth(64)
        self._copy_button.setFixedHeight(24)
        self._copy_button.setToolTip("Copy overlay text")
        self._copy_button.clicked.connect(self.copy_detail_text)

        self._detail_label = QtWidgets.QLabel(OVERLAY_INITIAL_DETAIL)
        self._detail_label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)
        self._detail_label.setWordWrap(True)
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

        container = QtWidgets.QFrame()
        container.setObjectName("overlayContainer")

        self._layout = QtWidgets.QVBoxLayout(container)
        self._layout.setContentsMargins(14, 10, 14, 10)
        self._layout.setSpacing(4)

        self._header_widget = QtWidgets.QWidget()
        header = QtWidgets.QHBoxLayout(self._header_widget)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        left_spacer = QtWidgets.QLabel("")
        left_spacer.setFixedWidth(self._copy_button.width())
        header.addWidget(left_spacer)
        header.addWidget(self._state_label, 1)
        header.addWidget(self._copy_button, 0, QtCore.Qt.AlignRight)

        self._layout.addWidget(self._header_widget)
        self._layout.addWidget(self._detail_scroll)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(container)

        self.resize(OVERLAY_WIDTH, OVERLAY_HEIGHT)
        self.set_state("Idle", OVERLAY_INITIAL_DETAIL)

    def set_state(self, state: str, detail: str = "") -> None:
        self._state_label.setText(state)
        self._detail_label.setText(detail)
        self._copy_button.setEnabled(bool(detail.strip()))
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

    def move_to_corner(self) -> None:
        screen = QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            return

        geometry = screen.availableGeometry()
        x = geometry.right() - self.width() - OVERLAY_MARGIN_X
        y = geometry.top() + OVERLAY_MARGIN_Y
        self.move(x, y)

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

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_detail_height()

    def _show_detail_context_menu(self, pos) -> None:
        menu = QtWidgets.QMenu(self)
        copy_action = menu.addAction("Copy text")
        selected = menu.exec(self._detail_label.mapToGlobal(pos))
        if selected == copy_action:
            self.copy_detail_text()

    def _update_detail_height(self) -> None:
        margins = self._layout.contentsMargins()
        spacing = self._layout.spacing()
        available_width = max(
            80,
            self.width() - margins.left() - margins.right() - 4,
        )
        self._detail_label.setFixedWidth(available_width)
        self._detail_label.adjustSize()

        content_height = self._detail_label.sizeHint().height()
        header_height = self._header_widget.sizeHint().height()
        max_detail_height = max(
            OVERLAY_DETAIL_MIN_HEIGHT,
            OVERLAY_MAX_HEIGHT
            - (margins.top() + margins.bottom() + header_height + spacing),
        )
        desired_detail_height = max(
            OVERLAY_DETAIL_MIN_HEIGHT,
            min(max_detail_height, content_height + 6),
        )
        self._detail_scroll.setFixedHeight(desired_detail_height)

        desired_window_height = (
            margins.top()
            + margins.bottom()
            + header_height
            + spacing
            + desired_detail_height
        )
        desired_window_height = max(
            OVERLAY_HEIGHT,
            min(OVERLAY_MAX_HEIGHT, desired_window_height),
        )
        if self.height() != desired_window_height:
            self.resize(self.width(), desired_window_height)

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
