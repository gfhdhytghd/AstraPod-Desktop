"""Album artwork: memory cache, on-disk PNG cache, lazy fetch from the iPod.

Art is keyed by album, not track. get() never touches the disk: a memory
miss queues the album for a background disk pass, and a miss on disk queues
it for the device. Queued albums are fetched in one background device job,
so a page of covers costs one serial borrow rather than one per cover. Both
queues serve the most recently requested album first, so the covers on
screen load before the ones scrolled past.
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


def newest(queue: dict, count: int) -> list:
    """The last count (key, track) pairs of queue, newest first, removed."""
    batch = list(queue.items())[-count:][::-1]
    for key, _track in batch:
        del queue[key]
    return batch


def requeue(queue: dict, key, track) -> None:
    """Add key to queue, or move it to the newest end."""
    queue.pop(key, None)
    queue[key] = track


class ArtStore(QtCore.QObject):
    art_ready = Signal(object)          # album key

    BATCH = 24
    DISK_BATCH = 48

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
        self._on_disk: dict = {}        # waiting for the disk pass
        self._reading: set = set()
        self._missing: set = set()      # not on disk; the device has to say
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(220)
        self._timer.timeout.connect(self._fetch)
        self._disk_timer = QtCore.QTimer(self)
        self._disk_timer.setSingleShot(True)
        self._disk_timer.setInterval(0)
        self._disk_timer.timeout.connect(self._read_disk)

    def set_serial(self, serial) -> None:
        if serial != self.serial:
            self.serial = serial
            self._memory.clear()
            self._queued.clear()
            self._none.clear()
            self._on_disk.clear()
            self._missing.clear()

    def reset_misses(self) -> None:
        self._none.clear()
        self._missing.clear()

    def get(self, track, size: str = "coverflow"):
        """QImage, NO_ART, or None while loading."""
        key = (contracts.album_key(track), size)
        image = self._memory.get(key)
        if image is not None:
            self._memory.move_to_end(key)
            return image
        if key in self._none or self.serial is None:
            return NO_ART
        if key not in self._missing:
            if key not in self._reading:
                requeue(self._on_disk, key, track)
                if not self._disk_timer.isActive():
                    self._disk_timer.start()
            return None
        if not self.enabled:
            return NO_ART
        if (self.serial, key) not in self._inflight:
            requeue(self._queued, key, track)
            if not self._timer.isActive():
                self._timer.start()
        return None

    def _remember(self, key, image) -> None:
        self._memory[key] = image
        self._memory.move_to_end(key)
        while len(self._memory) > 600:
            self._memory.popitem(last=False)

    def _read_disk(self) -> None:
        """Look up and decode a batch of cached covers off the GUI thread."""
        if not self._on_disk or self.serial is None:
            return
        serial = self.serial
        # Oldest first, so the newest misses end up newest in the device queue.
        batch = newest(self._on_disk, self.DISK_BATCH)[::-1]
        self._reading.update(key for key, _track in batch)
        library = self.library

        def work(job):
            for key, track in batch:
                path = library.art_path(serial, track, key[1])
                image = QtGui.QImage(str(path)) if path.exists() else QtGui.QImage()
                if not image.isNull():
                    found = image
                elif path.with_suffix(".none").exists():
                    found = NO_ART
                else:
                    found = None
                job.progress(data=(key, track, found))

        def progress(_fraction, _text, result):
            key, track, found = result
            self._reading.discard(key)
            if self.serial != serial:
                return
            if found is NO_ART:
                self._none.add(key)
            elif found is not None:
                self._remember(key, found)
            else:
                self._missing.add(key)
                if self.enabled:
                    requeue(self._queued, key, track)
                    if not self._timer.isActive():
                        self._timer.start()
                    return
            self.art_ready.emit(key[0])

        def done(_result):
            self._reading.difference_update(key for key, _track in batch)
            if self._on_disk and not self._disk_timer.isActive():
                self._disk_timer.start()

        self.runner.run("读取插图缓存", work, done, done, device=False, foreground=False,
                        on_progress=progress)

    def _fetch(self) -> None:
        if not self.enabled:
            self._queued.clear()
        if not self._queued:
            return
        serial = self.serial
        batch = newest(self._queued, self.BATCH)
        for key, _track in batch:
            self._inflight.add((serial, key))
        library = self.library
        session_for_device = getattr(library, "session_for_device", None)
        open_session = session_for_device() if session_for_device else library.session
        remaining = {key for key, _track in batch}

        def work(job):
            with open_session() as session:
                for key, track in batch:
                    if job.cancelled:
                        break
                    art = session.artwork(track, key[1])
                    path = library.art_path(serial, track, key[1])
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if art is None:
                        path.with_suffix(".none").touch()
                        image = None
                    else:
                        image = artwork_to_image(art)
                        image.save(str(path), "PNG")
                    job.progress(data=(key, image))

        def progress(_fraction, _text, result):
            key, image = result
            remaining.discard(key)
            self._inflight.discard((serial, key))
            if self.serial != serial:
                return
            self._missing.discard(key)
            if image is None:
                self._none.add(key)
            else:
                self._remember(key, image)
            self.art_ready.emit(key[0])

        def done(_result):
            self._inflight.difference_update((serial, key) for key in remaining)
            if self._queued and not self._timer.isActive():
                self._timer.start()

        def failed(_error):
            done(None)
            if self.serial == serial:
                self._none.update(remaining)
                for key in remaining:
                    self.art_ready.emit(key[0])

        self.runner.run("读取插图", work, done, failed, device=True, foreground=False,
                        on_progress=progress)


def placeholder_colors(seed: str) -> tuple[QtGui.QColor, QtGui.QColor]:
    digest = hashlib.sha1(seed.encode()).digest()
    hue = digest[0] / 255.0
    return (QtGui.QColor.fromHslF(hue, 0.28, 0.62),
            QtGui.QColor.fromHslF((hue + 0.07) % 1.0, 0.34, 0.44))
