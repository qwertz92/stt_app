"""Generate the application icon assets.

Renders the stt_app microphone mark with QPainter and writes:

- ``src/stt_app/assets/app_icon.ico`` (multi-size, PNG-compressed entries)
- ``src/stt_app/assets/app_icon.png`` (256 px preview/source image)

Run from the repository root: ``uv run python scripts/generate_app_icon.py``.
The output files are committed so normal builds never need to regenerate
them; rerun this script only when the design changes.
"""

from __future__ import annotations

import io
import os
import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = REPO_ROOT / "src" / "stt_app" / "assets"
ICO_SIZES = (256, 128, 64, 48, 32, 24, 16)
BASE_SIZE = 1024

BACKGROUND_TOP = "#33599c"
BACKGROUND_BOTTOM = "#102a54"
FOREGROUND = "#ffffff"


def _render_base_image(size: int):
    from PySide6 import QtCore, QtGui

    image = QtGui.QImage(size, size, QtGui.QImage.Format_ARGB32)
    image.fill(QtCore.Qt.transparent)

    painter = QtGui.QPainter(image)
    painter.setRenderHint(QtGui.QPainter.Antialiasing, True)

    # Rounded-square background with a subtle vertical gradient.
    gradient = QtGui.QLinearGradient(0, 0, 0, size)
    gradient.setColorAt(0.0, QtGui.QColor(BACKGROUND_TOP))
    gradient.setColorAt(1.0, QtGui.QColor(BACKGROUND_BOTTOM))
    painter.setBrush(QtGui.QBrush(gradient))
    painter.setPen(QtCore.Qt.NoPen)
    corner = size * 0.22
    painter.drawRoundedRect(QtCore.QRectF(0, 0, size, size), corner, corner)

    foreground = QtGui.QColor(FOREGROUND)
    stroke = size * 0.055

    # Microphone capsule.
    body_width = size * 0.26
    body_height = size * 0.40
    body_left = (size - body_width) / 2
    body_top = size * 0.17
    body_rect = QtCore.QRectF(body_left, body_top, body_width, body_height)
    painter.setBrush(foreground)
    painter.drawRoundedRect(body_rect, body_width / 2, body_width / 2)

    # Holder arc around the capsule bottom.
    pen = QtGui.QPen(foreground)
    pen.setWidthF(stroke)
    pen.setCapStyle(QtCore.Qt.RoundCap)
    painter.setPen(pen)
    painter.setBrush(QtCore.Qt.NoBrush)
    holder_margin = size * 0.085
    holder_rect = QtCore.QRectF(
        body_left - holder_margin,
        body_top + holder_margin,
        body_width + 2 * holder_margin,
        body_height + holder_margin,
    )
    # Qt angles are in 1/16 degree, counterclockwise from 3 o'clock.
    painter.drawArc(holder_rect, -200 * 16, 220 * 16)

    # Stem and base.
    center_x = size / 2
    stem_top = holder_rect.bottom()
    stem_bottom = size * 0.80
    painter.drawLine(
        QtCore.QPointF(center_x, stem_top),
        QtCore.QPointF(center_x, stem_bottom),
    )
    base_half = size * 0.11
    painter.drawLine(
        QtCore.QPointF(center_x - base_half, stem_bottom),
        QtCore.QPointF(center_x + base_half, stem_bottom),
    )

    painter.end()
    return image


def _scaled_png_bytes(image, size: int) -> bytes:
    from PySide6 import QtCore, QtGui

    scaled = image.scaled(
        size,
        size,
        QtCore.Qt.KeepAspectRatio,
        QtCore.Qt.SmoothTransformation,
    )
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.WriteOnly)
    writer = QtGui.QImageWriter(buffer, b"png")
    if not writer.write(scaled):
        raise RuntimeError(f"Failed to encode {size}px PNG: {writer.errorString()}")
    return bytes(buffer.data())


def _build_ico(png_entries: list[tuple[int, bytes]]) -> bytes:
    """Assemble an ICO container from PNG-compressed entries."""
    header = struct.pack("<HHH", 0, 1, len(png_entries))
    directory = io.BytesIO()
    payload = io.BytesIO()
    offset = len(header) + 16 * len(png_entries)
    for size, png_bytes in png_entries:
        size_byte = 0 if size >= 256 else size
        directory.write(
            struct.pack(
                "<BBBBHHII",
                size_byte,
                size_byte,
                0,  # no palette
                0,  # reserved
                1,  # color planes
                32,  # bits per pixel
                len(png_bytes),
                offset,
            )
        )
        payload.write(png_bytes)
        offset += len(png_bytes)
    return header + directory.getvalue() + payload.getvalue()


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtGui

    app = QtGui.QGuiApplication.instance() or QtGui.QGuiApplication([])
    _ = app

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    base_image = _render_base_image(BASE_SIZE)

    png_entries = [(size, _scaled_png_bytes(base_image, size)) for size in ICO_SIZES]
    ico_path = ASSETS_DIR / "app_icon.ico"
    ico_path.write_bytes(_build_ico(png_entries))

    png_path = ASSETS_DIR / "app_icon.png"
    png_path.write_bytes(dict(png_entries)[256])

    print(f"Wrote {ico_path} ({ico_path.stat().st_size} bytes)")
    print(f"Wrote {png_path} ({png_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
