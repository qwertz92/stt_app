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

        self._copy_button = QtWidgets.QPushButton("Copy")
        self._copy_button.setCursor(QtCore.Qt.PointingHandCursor)
        self._copy_button.setFocusPolicy(QtCore.Qt.NoFocus)
        self._copy_button.setFixedWidth(56)
        self._copy_button.setFixedHeight(22)
        self._copy_button.setToolTip("Copy overlay text")
        self._copy_button.clicked.connect(self.copy_detail_text)

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

        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        left_spacer = QtWidgets.QLabel("")
        left_spacer.setFixedWidth(self._copy_button.width())
        header.addWidget(left_spacer)
        header.addWidget(self._state_label, 1)
        header.addWidget(self._copy_button, 0, QtCore.Qt.AlignRight)

        layout.addLayout(header)
        layout.addWidget(self._detail_label)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(container)

        self.resize(OVERLAY_WIDTH, OVERLAY_HEIGHT)
        self.set_state("Idle", OVERLAY_INITIAL_DETAIL)

    def set_state(self, state: str, detail: str = "") -> None:
        self._state_label.setText(state)
        self._detail_label.setText(detail)
        self._copy_button.setEnabled(bool(detail.strip()))

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

    def _show_detail_context_menu(self, pos) -> None:
        menu = QtWidgets.QMenu(self)
        copy_action = menu.addAction("Copy text")
        selected = menu.exec(self._detail_label.mapToGlobal(pos))
        if selected == copy_action:
            self.copy_detail_text()

    def copy_detail_text(self) -> None:
        text = self._detail_label.text()
        if not text:
            return
        QtGui.QGuiApplication.clipboard().setText(text)
