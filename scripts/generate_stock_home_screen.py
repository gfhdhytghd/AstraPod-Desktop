"""Draw the text-free Classic preview background with Qt."""
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from astrapod_desktop.pages.stock_screen import (
    LCD_HEIGHT, LCD_WIDTH, MENU_TOP, PANEL_X, ROW_PITCH, TITLE_HEIGHT,
)
from astrapod_desktop.qt import Qt, QtCore, QtGui


def gradient(top, bottom, first, last):
    brush = QtGui.QLinearGradient(0, top, 0, bottom)
    brush.setColorAt(0, QtGui.QColor(first))
    brush.setColorAt(1, QtGui.QColor(last))
    return brush


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QtGui.QGuiApplication([])
    image = QtGui.QImage(LCD_WIDTH, LCD_HEIGHT, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor("#ffffff"))
    with QtGui.QPainter(image) as painter:
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.fillRect(PANEL_X, TITLE_HEIGHT, LCD_WIDTH - PANEL_X, LCD_HEIGHT - TITLE_HEIGHT,
                         gradient(TITLE_HEIGHT, LCD_HEIGHT, "#8498ab", "#394552"))
        painter.fillRect(0, 0, LCD_WIDTH, TITLE_HEIGHT,
                         gradient(0, TITLE_HEIGHT, "#f8f8f8", "#b7bdc5"))
        painter.fillRect(0, TITLE_HEIGHT - 1, LCD_WIDTH, 1, QtGui.QColor("#8e969f"))

        painter.setPen(QtGui.QPen(QtGui.QColor("#474e56"), 1))
        painter.setBrush(QtGui.QColor("#ecf0f1"))
        painter.drawRoundedRect(QtCore.QRectF(286, 6, 24, 12), 2, 2)
        painter.fillRect(310, 9, 2, 6, QtGui.QColor("#474e56"))
        painter.fillRect(288, 8, 20, 8, gradient(8, 16, "#a5e466", "#51983c"))

        center = MENU_TOP + ROW_PITCH / 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor("#225a9d"))
        painter.drawRoundedRect(QtCore.QRectF(5, MENU_TOP + 1, PANEL_X - 11, ROW_PITCH - 2), 4, 4)
        painter.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 2))
        painter.drawPolyline(QtGui.QPolygonF([
            QtCore.QPointF(PANEL_X - 19, center - 4),
            QtCore.QPointF(PANEL_X - 15, center),
            QtCore.QPointF(PANEL_X - 19, center + 4),
        ]))
        painter.fillRect(PANEL_X - 1, TITLE_HEIGHT, 1, LCD_HEIGHT - TITLE_HEIGHT,
                         QtGui.QColor("#78889b"))

    output = ROOT / "astrapod_desktop/assets/stock-classic-home.png"
    if not image.save(str(output), "PNG"):
        raise SystemExit(f"Could not write {output}")
    app.quit()


if __name__ == "__main__":
    main()
