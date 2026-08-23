"""Error text must be selectable so it can be copied into a bug report.

Qt gives a QMessageBox only `LinksAccessibleByMouse`, so an error could
previously be captured only by retyping it or taking a screenshot.
"""

from __future__ import annotations

import pytest
from PySide6 import QtCore, QtWidgets

from stt_app.dialog_style import (
    install_selectable_message_text,
    make_label_selectable,
    styled_message_box,
)


def _app() -> QtWidgets.QApplication:
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _is_selectable(widget) -> bool:
    return bool(widget.textInteractionFlags() & QtCore.Qt.TextSelectableByMouse)


@pytest.mark.platform_dependent
def test_qt_message_boxes_are_not_selectable_by_default():
    """Pins the Qt behaviour the filter exists to correct."""
    _app()
    box = QtWidgets.QMessageBox()
    box.setText("boom")
    assert not _is_selectable(box)


def test_the_app_filter_makes_every_message_box_selectable():
    """Most boxes come from the QMessageBox convenience statics, which build and
    show in one call and cannot be configured by the caller, so the filter is
    the only place that reaches them."""
    app = _app()
    install_selectable_message_text(app)

    box = QtWidgets.QMessageBox(
        QtWidgets.QMessageBox.Critical, "Download failed", "connection reset by peer"
    )
    box.show()
    app.processEvents()
    try:
        assert _is_selectable(box)
        labels = [label for label in box.findChildren(QtWidgets.QLabel) if label.text()]
        assert labels
        assert all(_is_selectable(label) for label in labels)
    finally:
        box.close()


def test_a_long_error_is_wrapped_and_kept_whole():
    app = _app()
    install_selectable_message_text(app)
    message = (
        "Model download failed for granite-speech-4.1-2b-plus: "
        "HTTPSConnectionPool(host=huggingface.co, port=443): Max retries exceeded "
        "with url /resolve/main/int8/encoder.onnx_data (caused by ConnectionReset)"
    )
    box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Critical, "Failed", message)
    box.show()
    app.processEvents()
    try:
        shown = " ".join(
            label.text() for label in box.findChildren(QtWidgets.QLabel) if label.text()
        )
        assert message in shown, "the message must not be elided or truncated"
    finally:
        box.close()


def test_styled_message_box_is_selectable_without_the_filter():
    _app()
    box = styled_message_box(
        icon=QtWidgets.QMessageBox.Warning,
        title="t",
        text="something went wrong",
        buttons=QtWidgets.QMessageBox.Ok,
        default_button=QtWidgets.QMessageBox.Ok,
    )
    assert _is_selectable(box)


def test_make_label_selectable_marks_an_inline_status_label():
    _app()
    label = QtWidgets.QLabel("Download failed: disk full")
    assert not _is_selectable(label)
    make_label_selectable(label)
    assert _is_selectable(label)


def test_installing_the_filter_twice_is_harmless():
    app = _app()
    install_selectable_message_text(app)
    install_selectable_message_text(app)
    box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Information, "t", "x")
    box.show()
    app.processEvents()
    try:
        assert _is_selectable(box)
    finally:
        box.close()
