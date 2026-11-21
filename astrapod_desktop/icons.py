"""Vector icons painted on a 24-unit grid, cached per name, colour and size."""
from __future__ import annotations

import math

from .qt import QtCore, QtGui, Qt

_CACHE: dict[tuple, QtGui.QIcon] = {}
P = QtCore.QPointF
R = QtCore.QRectF


def _stroke(p: QtGui.QPainter, color: QtGui.QColor, width: float = 1.7) -> None:
    pen = QtGui.QPen(color, width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)


def _fill(p: QtGui.QPainter, color: QtGui.QColor) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)


def _note(p, c):
    _stroke(p, c)
    p.drawLine(P(15, 4.5), P(15, 15.5))
    p.drawLine(P(15, 4.5), P(19.5, 6))
    _fill(p, c)
    p.drawEllipse(P(11.6, 16.2), 3.6, 2.9)


def _device(p, c):
    _stroke(p, c)
    p.drawRoundedRect(R(6, 2.5, 12, 19), 2.6, 2.6)
    p.drawRoundedRect(R(8, 4.8, 8, 6), 1, 1)
    p.drawEllipse(P(12, 16.2), 3.1, 3.1)
    _fill(p, c)
    p.drawEllipse(P(12, 16.2), 1.0, 1.0)


def _albums(p, c):
    _stroke(p, c)
    p.drawRoundedRect(R(3.5, 6.5, 14, 14), 2, 2)
    p.drawLine(P(7, 3.5), P(19, 3.5))
    p.drawLine(P(20.5, 5), P(20.5, 17))
    p.drawEllipse(P(10.5, 13.5), 3.2, 3.2)


def _artists(p, c):
    _stroke(p, c)
    p.drawEllipse(P(12, 8.5), 4, 4)
    path = QtGui.QPainterPath(P(4.5, 20.5))
    path.cubicTo(P(5.5, 15), P(18.5, 15), P(19.5, 20.5))
    p.drawPath(path)


def _genres(p, c):
    _stroke(p, c)
    for x, h in ((5, 8), (9.5, 14), (14, 11), (18.5, 16)):
        p.drawLine(P(x, 20), P(x, 20 - h))


def _composers(p, c):
    _stroke(p, c)
    for y in (6, 10, 14, 18):
        p.drawLine(P(3.5, y), P(20.5, y))
    _fill(p, c)
    p.drawEllipse(P(9, 16), 2.4, 1.9)
    p.drawEllipse(P(16, 12), 2.4, 1.9)


def _playlist(p, c):
    _stroke(p, c)
    for y in (6, 11, 16):
        p.drawLine(P(3.5, y), P(13, y))
    p.drawLine(P(18, 7), P(18, 17))
    _fill(p, c)
    p.drawEllipse(P(16, 17.5), 2.6, 2.1)


def _import(p, c):
    _stroke(p, c)
    p.drawLine(P(12, 3.5), P(12, 14.5))
    path = QtGui.QPainterPath(P(7.5, 10.5))
    path.lineTo(P(12, 15))
    path.lineTo(P(16.5, 10.5))
    p.drawPath(path)
    frame = QtGui.QPainterPath(P(4, 14))
    frame.lineTo(P(4, 19.5))
    frame.quadTo(P(4, 20.5), P(5, 20.5))
    frame.lineTo(P(19, 20.5))
    frame.quadTo(P(20, 20.5), P(20, 19.5))
    frame.lineTo(P(20, 14))
    p.drawPath(frame)


def _sync(p, c):
    _stroke(p, c)
    p.drawArc(R(4.5, 4.5, 15, 15), 30 * 16, 150 * 16)
    p.drawArc(R(4.5, 4.5, 15, 15), 210 * 16, 150 * 16)
    for (x, y), (dx, dy) in (((18.5, 8.2), (-3, 0.3)), ((5.5, 15.8), (3, -0.3))):
        p.drawLine(P(x, y), P(x + dx, y + dy))
        p.drawLine(P(x, y), P(x + dy * 8, y - abs(dx)))


def _agents(p, c):
    _stroke(p, c)
    p.drawRoundedRect(R(3.5, 5, 17, 13), 3, 3)
    p.drawLine(P(8, 10), P(10.5, 12))
    p.drawLine(P(10.5, 12), P(8, 14))
    p.drawLine(P(12.5, 14), P(16, 14))


def _usage(p, c):
    _stroke(p, c)
    p.drawArc(R(3.5, 5, 17, 17), 0, 180 * 16)
    p.drawLine(P(12, 13.5), P(16, 9))
    _fill(p, c)
    p.drawEllipse(P(12, 13.5), 1.5, 1.5)


def _extensions(p, c):
    _stroke(p, c)
    path = QtGui.QPainterPath()
    path.moveTo(5, 8)
    path.lineTo(9.5, 8)
    path.arcTo(R(9.5, 5, 5, 5), 180, -180)
    path.lineTo(19, 8)
    path.lineTo(19, 12.5)
    path.arcTo(R(16.5, 12.5, 5, 5), 90, -180)
    path.lineTo(19, 20)
    path.lineTo(5, 20)
    path.closeSubpath()
    p.drawPath(path)


def _settings(p, c):
    _stroke(p, c)
    p.drawEllipse(P(12, 12), 3, 3)
    for i in range(8):
        a = math.radians(i * 45)
        p.drawLine(P(12 + 6 * math.cos(a), 12 + 6 * math.sin(a)),
                   P(12 + 8.5 * math.cos(a), 12 + 8.5 * math.sin(a)))
    p.drawEllipse(P(12, 12), 6, 6)


def _logs(p, c):
    _stroke(p, c)
    p.drawRoundedRect(R(4.5, 3.5, 15, 17), 2, 2)
    for y, w in ((8, 9), (12, 7), (16, 9)):
        p.drawLine(P(8, y), P(8 + w, y) if w < 9 else P(16, y))


def _play(p, c):
    _fill(p, c)
    path = QtGui.QPainterPath(P(8, 5))
    path.lineTo(P(19, 12))
    path.lineTo(P(8, 19))
    path.closeSubpath()
    p.drawPath(path)


def _skip(p, c, forward):
    _fill(p, c)
    for offset in (0, 7):
        path = QtGui.QPainterPath()
        if forward:
            path.moveTo(4 + offset, 6)
            path.lineTo(11 + offset, 12)
            path.lineTo(4 + offset, 18)
        else:
            path.moveTo(20 - offset, 6)
            path.lineTo(13 - offset, 12)
            path.lineTo(20 - offset, 18)
        path.closeSubpath()
        p.drawPath(path)


def _mic(p, c):
    _stroke(p, c)
    p.drawRoundedRect(R(9, 3, 6, 11), 3, 3)
    p.drawArc(R(5.5, 6.5, 13, 11), 200 * 16, 140 * 16)
    p.drawLine(P(12, 17.5), P(12, 21))


def _search(p, c):
    _stroke(p, c, 1.8)
    p.drawEllipse(P(10.5, 10.5), 6, 6)
    p.drawLine(P(15, 15), P(20, 20))


def _refresh(p, c):
    _stroke(p, c)
    p.drawArc(R(4.5, 4.5, 15, 15), 60 * 16, 280 * 16)
    path = QtGui.QPainterPath(P(15.5, 3))
    path.lineTo(P(16.8, 6.4))
    path.lineTo(P(13.2, 7.4))
    p.drawPath(path)


def _info(p, c):
    _stroke(p, c)
    p.drawEllipse(P(12, 12), 8.5, 8.5)
    p.drawLine(P(12, 11), P(12, 16.5))
    _fill(p, c)
    p.drawEllipse(P(12, 7.8), 1.1, 1.1)


def _trash(p, c):
    _stroke(p, c)
    p.drawLine(P(4.5, 6.5), P(19.5, 6.5))
    p.drawLine(P(9.5, 6.5), P(10, 4))
    p.drawLine(P(10, 4), P(14, 4))
    p.drawLine(P(14, 4), P(14.5, 6.5))
    path = QtGui.QPainterPath(P(6.5, 6.5))
    path.lineTo(P(7.5, 20))
    path.lineTo(P(16.5, 20))
    path.lineTo(P(17.5, 6.5))
    p.drawPath(path)


def _copy(p, c):
    _stroke(p, c)
    p.drawRoundedRect(R(8, 8, 12, 12), 2, 2)
    path = QtGui.QPainterPath(P(16, 5.5))
    path.lineTo(P(16, 5))
    path.quadTo(P(16, 4), P(15, 4))
    path.lineTo(P(5, 4))
    path.quadTo(P(4, 4), P(4, 5))
    path.lineTo(P(4, 15))
    path.quadTo(P(4, 16), P(5, 16))
    path.lineTo(P(5.5, 16))
    p.drawPath(path)


def _plus(p, c):
    _stroke(p, c, 1.9)
    p.drawLine(P(12, 5), P(12, 19))
    p.drawLine(P(5, 12), P(19, 12))


def _folder(p, c):
    _stroke(p, c)
    path = QtGui.QPainterPath(P(3.5, 7))
    path.lineTo(P(3.5, 18.5))
    path.lineTo(P(20.5, 18.5))
    path.lineTo(P(20.5, 8.5))
    path.lineTo(P(11.5, 8.5))
    path.lineTo(P(9.5, 5.5))
    path.lineTo(P(4.5, 5.5))
    path.closeSubpath()
    p.drawPath(path)


def _back(p, c):
    _stroke(p, c, 1.9)
    path = QtGui.QPainterPath(P(14.5, 5))
    path.lineTo(P(7.5, 12))
    path.lineTo(P(14.5, 19))
    p.drawPath(path)


def _check(p, c):
    _stroke(p, c, 2)
    path = QtGui.QPainterPath(P(5, 12.5))
    path.lineTo(P(10, 17.5))
    path.lineTo(P(19, 7))
    p.drawPath(path)


def _close(p, c):
    _stroke(p, c, 1.9)
    p.drawLine(P(6.5, 6.5), P(17.5, 17.5))
    p.drawLine(P(17.5, 6.5), P(6.5, 17.5))


def star_path(cx: float, cy: float, outer: float, inner: float) -> QtGui.QPainterPath:
    path = QtGui.QPainterPath()
    for i in range(10):
        radius = outer if i % 2 == 0 else inner
        angle = math.radians(-90 + i * 36)
        point = P(cx + radius * math.cos(angle), cy + radius * math.sin(angle))
        if i == 0:
            path.moveTo(point)
        else:
            path.lineTo(point)
    path.closeSubpath()
    return path


def _star(p, c):
    _fill(p, c)
    p.drawPath(star_path(12, 12.5, 9, 3.9))


def _warning(p, c):
    _stroke(p, c)
    path = QtGui.QPainterPath(P(12, 3.5))
    path.lineTo(P(21, 19.5))
    path.lineTo(P(3, 19.5))
    path.closeSubpath()
    p.drawPath(path)
    p.drawLine(P(12, 9.5), P(12, 14))
    _fill(p, c)
    p.drawEllipse(P(12, 16.8), 1.0, 1.0)


def _key(p, c):
    _stroke(p, c)
    p.drawRoundedRect(R(3.5, 6, 17, 12), 2.5, 2.5)
    for x in (7, 10.5, 14, 17):
        p.drawPoint(P(x, 10))
    p.drawLine(P(8, 14.5), P(16, 14.5))


def _remote(p, c):
    _stroke(p, c)
    p.drawRoundedRect(R(7.5, 2.5, 9, 19), 3, 3)
    p.drawEllipse(P(12, 9), 2.6, 2.6)
    p.drawLine(P(10, 15), P(14, 15))
    p.drawLine(P(10, 18), P(14, 18))


def _link(p, c):
    _stroke(p, c)
    p.drawRoundedRect(R(3, 9, 10, 6), 3, 3)
    p.drawRoundedRect(R(11, 9, 10, 6), 3, 3)


def _disc(p, c):
    _stroke(p, c)
    p.drawEllipse(P(12, 12), 8.5, 8.5)
    p.drawEllipse(P(12, 12), 2.2, 2.2)


DRAW = {
    "songs": _note, "note": _note, "device": _device, "albums": _albums,
    "artists": _artists, "genres": _genres, "composers": _composers,
    "playlist": _playlist, "import": _import, "sync": _sync,
    "agents": _agents, "usage": _usage,
    "extensions": _extensions, "settings": _settings, "logs": _logs,
    "play": _play,
    "prev": lambda p, c: _skip(p, c, False), "next": lambda p, c: _skip(p, c, True),
    "mic": _mic, "search": _search, "refresh": _refresh,
    "info": _info, "trash": _trash, "copy": _copy, "plus": _plus,
    "folder": _folder, "back": _back, "check": _check, "close": _close,
    "star": _star, "warning": _warning, "key": _key, "remote": _remote,
    "link": _link, "disc": _disc,
}


def paint(painter: QtGui.QPainter, name: str, rect: QtCore.QRectF,
          color: QtGui.QColor | str) -> None:
    color = QtGui.QColor(color)
    painter.save()
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    painter.translate(rect.topLeft())
    painter.scale(rect.width() / 24.0, rect.height() / 24.0)
    DRAW[name](painter, color)
    painter.restore()


def icon(name: str, color: QtGui.QColor | str, size: int = 18) -> QtGui.QIcon:
    color = QtGui.QColor(color)
    key = (name, color.rgba(), size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    scale = 3
    pixmap = QtGui.QPixmap(size * scale, size * scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(scale)
    painter = QtGui.QPainter(pixmap)
    try:
        paint(painter, name, R(0, 0, size, size), color)
    finally:
        painter.end()
    result = QtGui.QIcon(pixmap)
    _CACHE[key] = result
    return result
