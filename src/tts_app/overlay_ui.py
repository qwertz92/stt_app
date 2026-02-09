from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from .config import (
    OVERLAY_HEIGHT,
    OVERLAY_INITIAL_DETAIL,
    OVERLAY_MARGIN_X,
    OVERLAY_MARGIN_Y,
    OVERLAY_STATE_COLORS,
    OVERLAY_WIDTH,
)


class OverlayUI(QtWidgets.QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Dictation")
        self.setWindowFlags(
            QtCore.Qt.Tool
            | QtCore.Qt.FramelessWindowHint
            | QtCore.Qt.WindowStaysOnTopHint
        )
        self.setAttribute(QtCore.Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(QtCore.Qt.WA_TranslucentBackground, True)

        self._state_label = QtWidgets.QLabel("Idle")
        self._state_label.setAlignment(QtCore.Qt.AlignCenter)
        state_font = QtGui.QFont()
        state_font.setBold(True)
        self._state_label.setFont(state_font)

        self._detail_label = QtWidgets.QLabel(OVERLAY_INITIAL_DETAIL)
        self._detail_label.setAlignment(QtCore.Qt.AlignCenter)
        self._detail_label.setWordWrap(True)
        self._detail_label.setTextInteractionFlags(
            QtCore.Qt.TextSelectableByMouse | QtCore.Qt.TextSelectableByKeyboard
        )
        self._detail_label.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self._detail_label.customContextMenuRequested.connect(
            self._show_detail_context_menu
        )

        container = QtWidgets.QFrame()
        container.setObjectName("overlayContainer")

        layout = QtWidgets.QVBoxLayout(container)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(4)
        layout.addWidget(self._state_label)
        layout.addWidget(self._detail_label)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(container)

        self.resize(OVERLAY_WIDTH, OVERLAY_HEIGHT)
        self.set_state("Idle", OVERLAY_INITIAL_DETAIL)

    def set_state(self, state: str, detail: str = "") -> None:
        self._state_label.setText(state)
        self._detail_label.setText(detail)

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

    def _show_detail_context_menu(self, pos) -> None:
        menu = QtWidgets.QMenu(self)
        copy_action = menu.addAction("Copy text")
        selected = menu.exec(self._detail_label.mapToGlobal(pos))
        if selected == copy_action:
            QtGui.QGuiApplication.clipboard().setText(self._detail_label.text())
