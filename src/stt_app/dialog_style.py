"""Shared styling for the app's message boxes and small dialogs.

Qt renders a bare :class:`QMessageBox` with the palette it inherits, and on
Windows that palette is not always the one the surrounding dialog uses. The
observed failure is a message box whose body text arrives in the *disabled*
grey while the background stays white, which is close to unreadable. Rather
than depend on whatever palette a given machine hands us, every dialog the app
raises itself carries these explicit colours.

``update_ui`` re-exports :data:`DIALOG_STYLESHEET` under its historical name.
"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

DIALOG_STYLESHEET = """
QMessageBox, QDialog {
    background-color: #f7f9fc;
    color: #1f2933;
}
QMessageBox QLabel, QDialog QLabel {
    color: #1f2933;
}
QPushButton {
    min-height: 26px;
    padding: 5px 12px;
    color: #1f2933;
    background-color: #f7f9fc;
    border: 1px solid #9aa8b7;
    border-radius: 4px;
}
QPushButton:hover:enabled {
    color: #0b315c;
    background-color: #dbeafe;
    border-color: #4f83c2;
}
QPushButton:pressed:enabled {
    color: #082544;
    background-color: #bfdbfe;
    border-color: #2563a6;
}
QPushButton:disabled {
    color: #6b7280;
    background-color: #e5e7eb;
    border-color: #c7cdd4;
}
QPushButton[primary="true"] {
    color: #ffffff;
    background-color: #1769aa;
    border-color: #12558a;
}
QPushButton[primary="true"]:hover:enabled {
    color: #ffffff;
    background-color: #125a96;
    border-color: #0d4779;
}
QPushButton[primary="true"]:pressed:enabled {
    color: #ffffff;
    background-color: #0d4779;
    border-color: #08375f;
}
QProgressBar {
    min-height: 18px;
    color: #1f2933;
    background-color: #e5e7eb;
    border: 1px solid #aeb8c5;
    border-radius: 4px;
    text-align: center;
}
QProgressBar::chunk {
    background-color: #2f80c9;
    border-radius: 3px;
}
"""


# Qt gives a message box `LinksAccessibleByMouse` and nothing else, so its text
# cannot be selected — an error message could only be transcribed by hand or
# screenshotted. Every box the app shows gets these instead.
SELECTABLE_TEXT_FLAGS = (
    QtCore.Qt.TextSelectableByMouse | QtCore.Qt.TextSelectableByKeyboard
)


def apply_dialog_style(widget: QtWidgets.QWidget) -> None:
    """Give ``widget`` the app's readable dialog colours."""
    widget.setStyleSheet(DIALOG_STYLESHEET)


def make_label_selectable(label: QtWidgets.QLabel) -> None:
    """Let the user select and copy an inline status/error label.

    These carry provider and runtime errors verbatim, which is exactly the text
    worth pasting into a bug report.
    """
    label.setTextInteractionFlags(SELECTABLE_TEXT_FLAGS)


def make_message_text_selectable(box: QtWidgets.QMessageBox) -> None:
    """Let the user select and copy a message box's text.

    Also covers the detail area, which carries the long provider/runtime errors
    that are the ones actually worth copying.
    """
    try:
        box.setTextInteractionFlags(SELECTABLE_TEXT_FLAGS)
    except Exception:
        return
    for label in box.findChildren(QtWidgets.QLabel):
        label.setTextInteractionFlags(SELECTABLE_TEXT_FLAGS)
    for view in box.findChildren(QtWidgets.QTextEdit):
        view.setReadOnly(True)
        view.setTextInteractionFlags(SELECTABLE_TEXT_FLAGS)


class _SelectableMessageTextFilter(QtCore.QObject):
    """Makes every message box selectable, whoever created it.

    The app raises most of its boxes through `QMessageBox.critical` and friends,
    which build and show the box in one call and give no chance to configure it.
    Filtering on show is the only place that reaches all of them, and it also
    covers boxes Qt raises on its own.
    """

    # Bound as class attributes: this runs for *every* event in the process, so
    # the rejection path must not pay for attribute lookups on QtCore/QtWidgets.
    _SHOW = QtCore.QEvent.Type.Show
    _BOX = QtWidgets.QMessageBox

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() is self._SHOW and isinstance(obj, self._BOX):
            make_message_text_selectable(obj)
        return False


_SELECTABLE_FILTER: _SelectableMessageTextFilter | None = None


def install_selectable_message_text(app: QtWidgets.QApplication) -> None:
    """Install the app-wide filter once."""
    global _SELECTABLE_FILTER
    if _SELECTABLE_FILTER is not None:
        return
    _SELECTABLE_FILTER = _SelectableMessageTextFilter(app)
    app.installEventFilter(_SELECTABLE_FILTER)


def styled_message_box(
    *,
    icon: QtWidgets.QMessageBox.Icon,
    title: str,
    text: str,
    buttons: QtWidgets.QMessageBox.StandardButtons | QtWidgets.QMessageBox.StandardButton,
    default_button: QtWidgets.QMessageBox.StandardButton,
    parent: QtWidgets.QWidget | None = None,
) -> QtWidgets.QMessageBox:
    """Build a message box that is readable regardless of the host palette.

    The convenience statics (``QMessageBox.question`` and friends) give no
    chance to style the box before it is shown, so callers that care about
    contrast build the instance here instead.
    """
    box = QtWidgets.QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(buttons)
    box.setDefaultButton(default_button)
    apply_dialog_style(box)
    make_message_text_selectable(box)
    return box
