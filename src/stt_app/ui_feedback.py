from __future__ import annotations

from collections.abc import Iterable

from PySide6 import QtWidgets

BUTTON_FEEDBACK_STYLESHEET = """
QPushButton {
    min-height: 24px;
    padding: 4px 10px;
    border: 1px solid #aeb8c5;
    border-radius: 4px;
    background-color: #f7f9fc;
}
QPushButton:hover:enabled {
    background-color: #eef5ff;
    border-color: #7ea8e6;
}
QPushButton:pressed:enabled {
    background-color: #dcecff;
    border-color: #1a73e8;
}
QPushButton:disabled {
    color: #777;
    background-color: #f1f3f4;
    border-color: #d5d9df;
}
QPushButton[feedbackState="success"] {
    color: #1b5e20;
    background-color: #dff5e0;
    border-color: #89c88f;
}
QPushButton[feedbackState="success"]:hover:enabled {
    background-color: #d7f0d9;
    border-color: #6fb978;
}
QPushButton[feedbackState="success"]:pressed:enabled {
    background-color: #c9eacc;
    border-color: #4f9f59;
}
"""


def reserve_button_width_for_texts(
    button: QtWidgets.QPushButton,
    texts: Iterable[str],
) -> None:
    current_text = button.text()
    width = button.minimumWidth()
    button.ensurePolished()
    try:
        for text in texts:
            button.setText(str(text))
            width = max(width, button.sizeHint().width())
    finally:
        button.setText(current_text)
    width = max(width, button.sizeHint().width())
    button.setMinimumWidth(width)


def set_button_feedback_state(
    button: QtWidgets.QPushButton,
    state: str | None,
) -> None:
    value = str(state or "")
    if button.property("feedbackState") == value:
        return
    button.setProperty("feedbackState", value)
    style = button.style()
    style.unpolish(button)
    style.polish(button)
    button.update()


def restore_vertical_scrollbar(
    widget: QtWidgets.QAbstractScrollArea,
    value: int,
) -> None:
    do_items_layout = getattr(widget, "doItemsLayout", None)
    if callable(do_items_layout):
        do_items_layout()
    scroll_bar = widget.verticalScrollBar()
    scroll_bar.setValue(max(0, min(int(value), scroll_bar.maximum())))
