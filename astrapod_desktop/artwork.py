"""Album artwork: memory cache, on-disk PNG cache, lazy fetch from the iPod.

Art is keyed by album, not track. A miss on disk queues the album for the
device; queued albums are fetched in one background device job, so a page
of covers costs one serial borrow rather than one per cover.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib

from . import contracts
from .qt import QtCore, QtGui, Signal

NO_ART = object()
SIZES = {"list": 44, "coverflow": 128, "wps": 300}


def artwork_to_image(art) -> QtGui.QImage:
    image = QtGui.QImage(art.rgb888, art.width, art.height, art.width * 3,
                         QtGui.QImage.Format.Format_RGB888)
    return image.copy()


class ArtStore(QtCore.QObject):
    art_ready = Signal(object)          # album key

    BATCH = 24

    def __init__(self, library, runner, parent=None):
        super().__init__(parent)
        self.library = library
        self.runner = runner
        self.serial = None
        self.enabled = True
        self._memory: OrderedDict = OrderedDict()
        self._queued: dict = {}
        self._inflight: set = set()
        self._none: set = set()
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(220)
        self._timer.timeout.connect(self._fetch)

    def set_serial(self, serial) -> None:
        if serial != self.serial:
            self.serial = serial
            self._memory.clear()
            self._queued.clear()
            self._none.clear()

    def reset_misses(self) -> None:
        self._none.clear()

    def get(self, track, size: str = "coverflow"):
        """QImage, NO_ART, or None while loading."""
        key = (contracts.album_key(track), size)
        image = self._memory.get(key)
        if image is not None:
            self._memory.move_to_end(key)
            return image
        if key in self._none:
            return NO_ART
        if self.serial is not None:
            path = self.library.art_path(self.serial, track, size)
            if path.exists():
                loaded = QtGui.QImage(str(path))
                if not loaded.isNull():
                    self._remember(key, loaded)
                    return loaded
            if path.with_suffix(".none").exists():
                self._none.add(key)
                return NO_ART
        if not self.enabled or self.serial is None:
            return NO_ART
        if key not in self._inflight:
            self._queued[key] = track
            if not self._timer.isActive():
                self._timer.start()
        return None

    def _remember(self, key, image) -> None:
        self._memory[key] = image
        self._memory.move_to_end(key)
        while len(self._memory) > 600:
            self._memory.popitem(last=False)

    def _fetch(self) -> None:
        if not self._queued:
            return
        batch = list(self._queued.items())[: self.BATCH]
        for key, _track in batch:
            self._queued.pop(key, None)
            self._inflight.add(key)
        serial = self.serial
        library = self.library

        def work(job):
            results = []
            with library.session() as session:
                for key, track in batch:
                    if job.cancelled:
                        break
                    art = session.artwork(track, key[1])
                    path = library.art_path(serial, track, key[1])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if art is None:
                        path.with_suffix(".none").touch()
                        results.append((key, None))
                    else:
                        image = artwork_to_image(art)
                        image.save(str(path), "PNG")
                        results.append((key, image))
            return results

        def done(results):
            for key, image in results:
                self._inflight.discard(key)
                if image is None:
                    self._none.add(key)
                else:
                    self._remember(key, image)
                self.art_ready.emit(key[0])
            for key, _track in batch:
                self._inflight.discard(key)
            if self._queued and not self._timer.isActive():
                self._timer.start()

        def failed(_error):
            for key, _track in batch:
                self._inflight.discard(key)
                self._none.add(key)
                self.art_ready.emit(key[0])

        self.runner.run("读取插图", work, done, failed, device=True, foreground=False)


def placeholder_colors(seed: str) -> tuple[QtGui.QColor, QtGui.QColor]:
    digest = hashlib.sha1(seed.encode()).digest()
    hue = digest[0] / 255.0
    return (QtGui.QColor.fromHslF(hue, 0.28, 0.62),
            QtGui.QColor.fromHslF((hue + 0.07) % 1.0, 0.34, 0.44))
