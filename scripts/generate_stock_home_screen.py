"""Draw the Classic menu using Qt. Run with QT_QPA_PLATFORM=offscreen."""
from pathlib import Path
import os

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor, QFont, QGuiApplication, QImage, QLinearGradient, QPainter, QPen, QPolygonF,
)


def gradient(top, bottom, first, last):
    brush = QLinearGradient(0, top, 0, bottom)
    brush.setColorAt(0, QColor(first))
    brush.setColorAt(1, QColor(last))
    return brush


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication([])
    image = QImage(320, 240, QImage.Format.Format_RGB32)
    image.fill(QColor("#ffffff"))
    with QPainter(image) as painter:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(160, 27, 160, 213, gradient(27, 240, "#e6e8eb", "#aeb5bf"))
        painter.fillRect(0, 0, 320, 27, gradient(0, 27, "#f8f8f8", "#b7bdc5"))
        painter.fillRect(0, 26, 320, 1, QColor("#8e969f"))

        font = QFont("DejaVu Sans")
        font.setPixelSize(17)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#21252a"))
        painter.drawText(QRectF(0, 0, 320, 26), Qt.AlignmentFlag.AlignCenter, "iPod")

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#354356"))
        painter.drawPolygon(QPolygonF([QPointF(9, 7), QPointF(9, 20), QPointF(19, 13.5)]))
        painter.setPen(QPen(QColor("#474e56"), 1))
        painter.setBrush(QColor("#ecf0f1"))
        painter.drawRoundedRect(QRectF(286, 7, 24, 12), 2, 2)
        painter.fillRect(310, 10, 2, 6, QColor("#474e56"))
        painter.fillRect(288, 9, 20, 8, gradient(9, 17, "#a5e466", "#51983c"))

        font.setPixelSize(16)
        font.setBold(False)
        painter.setFont(font)
        items = ("Music", "Videos", "Photos", "Podcasts", "Extras", "Settings", "Shuffle Songs")
        for index, item in enumerate(items):
            y = 27 + index * 28
            if index == 0:
                painter.fillRect(0, y, 159, 28, gradient(y, y + 28, "#62b6f0", "#1b62bb"))
            painter.setPen(QColor("#ffffff" if index == 0 else "#25292f"))
            painter.drawText(QRectF(9, y, 135, 28), Qt.AlignmentFlag.AlignVCenter, item)
        painter.setPen(QPen(QColor("#ffffff"), 2))
        painter.drawPolyline(QPolygonF([QPointF(145, 36), QPointF(150, 41), QPointF(145, 46)]))
        painter.fillRect(159, 27, 1, 213, QColor("#b8bec5"))

    output = Path(__file__).resolve().parents[1] / "astrapod_desktop/assets/stock-classic-home.png"
    if not image.save(str(output), "PNG"):
        raise SystemExit(f"Could not write {output}")
    app.quit()


if __name__ == "__main__":
    main()
