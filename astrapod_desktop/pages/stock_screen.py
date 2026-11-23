"""Typography and cover slideshow for the Classic home preview."""
from __future__ import annotations

import random

from ..artwork import artwork_to_image
from ..contracts import album_key
from ..qt import QtCore, QtGui, Qt
from ..stock_font import font_path, is_stock_font

import hrstockdevice


LCD_WIDTH = 320
LCD_HEIGHT = 240
TITLE_HEIGHT = 24
ROW_PITCH = 25
PANEL_X = 160
MENU_TOP = TITLE_HEIGHT
MENU_ITEMS = ("Music", "Videos", "Photos", "Podcasts", "Extras", "Settings", "Shuffle Songs")
FALLBACK_FAMILY = "Liberation Sans"


def _preview_font(data: bytes, size: int) -> QtGui.QRawFont:
    if data:
        font = QtGui.QRawFont(data, size, QtGui.QFont.HintingPreference.PreferVerticalHinting)
        if font.isValid():
            return font
    fallback = QtGui.QFont(FALLBACK_FAMILY)
    fallback.setPixelSize(size)
    fallback.setBold(True)
    return QtGui.QRawFont.fromFont(fallback)


def _draw_label(painter, font, label, x, y, height, center=False):
    # These are fixed English labels. Lay them out directly from the face so
    # an installed font with the same family name cannot replace the IPSW face.
    glyphs = font.glyphIndexesForString(label)
    positions = []
    width = 0.0
    for advance in font.advancesForGlyphIndexes(glyphs):
        positions.append(QtCore.QPointF(width, 0))
        width += advance.x()
    run = QtGui.QGlyphRun()
    run.setRawFont(font)
    run.setGlyphIndexes(glyphs)
    run.setPositions(positions)
    if center:
        x -= width / 2
    baseline = y + (height + font.ascent() - font.descent()) / 2
    painter.drawGlyphRun(QtCore.QPointF(x, baseline), run)


def render_stock_home(background: QtGui.QImage) -> QtGui.QImage:
    try:
        with font_path().open("rb") as cached:
            data = cached.read(512 * 1024)
    except OSError:
        data = b""
    if not is_stock_font(data):
        data = b""

    title = _preview_font(data, 14)
    menu = _preview_font(data, 16)
    image = background.copy()
    with QtGui.QPainter(image) as painter:
        painter.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
        painter.setPen(QtGui.QColor("#21252a"))
        _draw_label(painter, title, "iPod", LCD_WIDTH / 2, 0, TITLE_HEIGHT - 1, center=True)
        for index, label in enumerate(MENU_ITEMS):
            painter.setPen(QtGui.QColor("#ffffff" if index == 0 else "#25292f"))
            _draw_label(painter, menu, label, 12, MENU_TOP + index * ROW_PITCH, ROW_PITCH)
    return image


def _cached_cover(library, serial, tracks, previous, job):
    albums = {}
    for track in tracks:
        artist, album = album_key(track)
        key = artist, album or track.title
        if key != previous:
            albums.setdefault(key, []).append(track)
    keys = list(albums)
    random.shuffle(keys)
    for key in keys:
        for size in ("wps", "coverflow", "list"):
            for track in albums[key]:
                job.check()
                path = library.art_path(serial, track, size)
                if not path.is_file():
                    continue
                reader = QtGui.QImageReader(str(path))
                dimensions = reader.size()
                if not dimensions.isValid():
                    continue
                if max(dimensions.width(), dimensions.height()) > 320:
                    dimensions.scale(320, 320, Qt.AspectRatioMode.KeepAspectRatio)
                    reader.setScaledSize(dimensions)
                image = reader.read()
                if not image.isNull():
                    return key, image
    return None


def _load_cover(device, library, serial, tracks, previous, job):
    import hrstock

    job.check()
    try:
        with hrstockdevice.stock_mount(device) as root:
            result = hrstock.random_artwork(root, previous)
    except (OSError, RuntimeError):
        result = None
    if result is not None:
        key, art = result
        return key, artwork_to_image(art)
    if serial:
        return _cached_cover(library, serial, tracks, previous, job)
    return None


class StockCoverPanel(QtCore.QObject):
    HOLD_MS = 5500
    FADE_MS = 1000

    def __init__(self, drawing, library_state):
        super().__init__(drawing)
        self.drawing = drawing
        self.state = library_state
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(33)
        self.timer.timeout.connect(self._advance)
        self.clock = QtCore.QElapsedTimer()
        self.mount = None
        self.tracks = None
        self.serial = None
        self.visible = False
        self.motion = False
        self.current = self.next = None
        self.elapsed = 0
        self.direction = 1
        self.exhausted = False
        self.loading = False
        self.revision = 0

    def set_source(self, mount) -> None:
        matches = mount is not None and self.state.stock and self.state.serial == mount.serial
        tracks = self.state.tracks if matches else ()
        serial = self.state.art_identity if matches else None
        if mount != self.mount or (mount is not None and
                                   (tracks is not self.tracks or serial != self.serial)):
            self.mount, self.tracks, self.serial = mount, tracks, serial
            self.revision += 1
            self.timer.stop()
            self.current = self.next = None
            self.elapsed = 0
            self.exhausted = False
            self.drawing.update()
            self._sync()

    def set_visible(self, visible: bool, motion: bool) -> None:
        if (visible, motion) != (self.visible, self.motion):
            self.visible, self.motion = visible, motion
            self._sync()
            self.drawing.update()

    def _sync(self):
        if not (self.visible and self.mount is not None):
            self.timer.stop()
            return
        if self.current is not None and self.motion and not self.exhausted:
            if not self.timer.isActive():
                self.clock.start()
                self.timer.start()
        else:
            self.timer.stop()

        if self.current is None or self.motion:
            self._prefetch()

    def _prefetch(self):
        if self.next is not None or self.loading or self.exhausted:
            return
        device, revision = self.mount, self.revision
        library, serial, tracks = self.state.library, self.serial, tuple(self.tracks)
        previous = self.current[0] if self.current is not None else None

        def work(job):
            return _load_cover(device, library, serial, tracks, previous, job)

        def done(result):
            self.loading = False
            if self.revision == revision:
                if result is None:
                    self.exhausted = True
                    self.elapsed = 0
                elif self.current is None:
                    self.current = result
                else:
                    self.next = result
            self._sync()
            self.drawing.update()

        self.loading = True
        self.state.runner.run("读取原厂封面", work, done, lambda _error: done(None),
                              foreground=False)

    def _advance(self):
        self.elapsed += self.clock.restart()
        if self.next is None:
            self.elapsed = min(self.elapsed, self.HOLD_MS)
        elif self.elapsed >= self.HOLD_MS + self.FADE_MS:
            self.current, self.next = self.next, None
            self.elapsed = 0
            self.direction *= -1
            self._prefetch()
        self.drawing.update()

    def paint(self, painter, rect):
        if self.current is None:
            return
        painter.save()
        painter.setClipRect(rect, Qt.ClipOperation.IntersectClip)
        if self.motion and not self.exhausted:
            progress = self.elapsed / (self.HOLD_MS + self.FADE_MS)
            self._draw_cover(painter, rect, self.current[1], progress, self.direction)
            if self.next is not None and self.elapsed > self.HOLD_MS:
                fade = (self.elapsed - self.HOLD_MS) / self.FADE_MS
                painter.setOpacity(fade)
                self._draw_cover(painter, rect, self.next[1], 0, -self.direction)
        else:
            self._draw_cover(painter, rect, self.current[1], 0.5, 0)
        painter.restore()

    @staticmethod
    def _draw_cover(painter, rect, image, progress, direction):
        scale = max(rect.width() / image.width(), rect.height() / image.height())
        scale *= 1.03 + 0.14 * progress
        width, height = image.width() * scale, image.height() * scale
        pan = direction * (2 * progress - 1)
        x = rect.center().x() - width / 2 + (width - rect.width()) * 0.35 * pan
        y = rect.center().y() - height / 2 + (height - rect.height()) * 0.25 * pan
        painter.drawImage(QtCore.QRectF(x, y, width, height), image)
