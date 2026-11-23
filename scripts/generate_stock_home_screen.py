"""Draw the text-free Classic preview background with Qt.

Colours and shapes are measured from a Classic 1.1.2 LCD capture and drawn
as gradients and polygons; no firmware artwork is copied.
"""
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from astrapod_desktop.pages.stock_screen import (
    LCD_HEIGHT, LCD_WIDTH, MENU_TOP, PANEL_X, ROW_PITCH, TITLE_HEIGHT, paint_split_shadow,
)
from astrapod_desktop.qt import Qt, QtCore, QtGui

P = QtCore.QPointF
# An elliptical radial gradient, 2.2 times wider than tall, centred near
# the top of the split. Stops are (radius, colour).
PANEL_CENTRE = (140, 10)
PANEL_ASPECT = 0.45
PANEL_STOPS = ((18, "#9aafc0"), (63, "#8b9dac"), (108, "#72828d"),
               (153, "#596670"), (198, "#49525b"), (243, "#404f53"))
# The battery's glossy fill, one colour per interior row from the top.
BATTERY_FILL = ("#a5d38c", "#defbce", "#ceefb5", "#a5e37b", "#a5df7b",
                "#6baa42", "#4a9229", "#529a31", "#5aa639", "#529a31")
BOLT = ((140.75, 4.4), (137.6, 10.0), (139.85, 10.25),
        (140.35, 14.05), (143.4, 7.7), (140.8, 7.9))


def gradient(top, bottom, first, last):
    brush = QtGui.QLinearGradient(0, top, 0, bottom)
    brush.setColorAt(0, QtGui.QColor(first))
    brush.setColorAt(1, QtGui.QColor(last))
    return brush


def draw_panel(painter):
    radius = PANEL_STOPS[-1][0]
    brush = QtGui.QRadialGradient(0, 0, radius)
    for stop, colour in PANEL_STOPS:
        brush.setColorAt(stop / radius, QtGui.QColor(colour))
    brush = QtGui.QBrush(brush)
    brush.setTransform(QtGui.QTransform().translate(*PANEL_CENTRE).scale(1 / PANEL_ASPECT, 1))
    panel = QtCore.QRectF(PANEL_X, 0, LCD_WIDTH - PANEL_X, LCD_HEIGHT)
    painter.fillRect(panel, brush)
    paint_split_shadow(painter, panel)


def draw_menu(painter):
    title = gradient(0.5, TITLE_HEIGHT - 1.5, "#ffffff", "#9b9b9d")
    title.setColorAt(8 / 18, QtGui.QColor("#dad8db"))
    painter.fillRect(0, 0, PANEL_X, TITLE_HEIGHT - 1, title)
    painter.fillRect(0, TITLE_HEIGHT - 1, PANEL_X, 1, QtGui.QColor("#5a595a"))
    bottom = MENU_TOP + ROW_PITCH - 1
    painter.fillRect(0, MENU_TOP, PANEL_X, 1, QtGui.QColor("#94c7de"))
    painter.fillRect(0, MENU_TOP + 1, PANEL_X, ROW_PITCH - 2,
                     gradient(MENU_TOP + 1.5, bottom - 0.5, "#04a5e3", "#0065d5"))
    painter.fillRect(0, bottom, PANEL_X, 1, QtGui.QColor("#004184"))

    pen = QtGui.QPen(QtGui.QColor("white"), 3)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    painter.setPen(pen)
    painter.drawPolyline(QtGui.QPolygonF([P(145.75, 27), P(151, 32.5), P(145.75, 38)]))


def draw_battery(painter):
    shell = QtGui.QColor("#676567")
    highlight = QtGui.QColor(255, 255, 255, 220)
    painter.setPen(Qt.PenStyle.NoPen)
    # A light edge one pixel below the shell and its terminal.
    terminal = QtCore.QRectF(152.25, 7, 2.5, 4)
    painter.setBrush(highlight)
    painter.drawRoundedRect(QtCore.QRectF(129, 4, 24, 12), 0.8, 0.8)
    painter.drawRoundedRect(terminal.translated(0, 1), 1, 1)
    painter.setBrush(shell)
    painter.drawRoundedRect(QtCore.QRectF(129, 3, 24, 12), 0.8, 0.8)
    painter.drawRoundedRect(terminal, 1, 1)
    painter.fillRect(QtCore.QRectF(152, 9, 1, 5), QtGui.QColor("#7b797b"))
    painter.fillRect(QtCore.QRectF(153, 8, 1, 2.25), QtGui.QColor("#c6c0cc"))
    fill = QtGui.QLinearGradient(0, 4, 0, 14)
    for row, colour in enumerate(BATTERY_FILL):
        fill.setColorAt((row + 0.5) / len(BATTERY_FILL), QtGui.QColor(colour))
    painter.fillRect(QtCore.QRectF(130, 4, 22, 10), fill)

    # The charge bolt: the preview is only shown while the iPod is on USB power.
    bolt = QtGui.QPolygonF([P(x, y) for x, y in BOLT])
    painter.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 140), 1.1))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPolygon(bolt)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QColor("#3b4a2e"))
    painter.drawPolygon(bolt)


def to_rgb565(image):
    """Round every pixel to the 16-bit colours the Classic LCD can show."""
    def levels(bits):
        top = (1 << bits) - 1
        return [(q << (8 - bits)) | (q >> (2 * bits - 8))
                for q in (round(value * top / 255) for value in range(256))]
    five, six = levels(5), levels(6)
    for y in range(image.height()):
        for x in range(image.width()):
            colour = image.pixelColor(x, y)
            image.setPixelColor(x, y, QtGui.QColor(
                five[colour.red()], six[colour.green()], five[colour.blue()]))


def main():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QtGui.QGuiApplication([])
    image = QtGui.QImage(LCD_WIDTH, LCD_HEIGHT, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor("#ffffff"))
    with QtGui.QPainter(image) as painter:
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        draw_panel(painter)
        draw_menu(painter)
        draw_battery(painter)
    to_rgb565(image)

    output = ROOT / "astrapod_desktop/assets/stock-classic-home.png"
    if not image.save(str(output), "PNG"):
        raise SystemExit(f"Could not write {output}")
    app.quit()


if __name__ == "__main__":
    main()
