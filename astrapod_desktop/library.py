"""The library the window shows: summary and tracks, from cache or device."""
from __future__ import annotations

from .qt import QtCore, Signal


class LibraryState(QtCore.QObject):
    changed = Signal()
    failed = Signal(str)
    track_removed = Signal(object)

    def __init__(self, library, runner, art, parent=None):
        super().__init__(parent)
        self.library = library
        self.runner = runner
        self.art = art
        self.serial = None
        self.summary = None
        self.tracks: list = []
        self.source = "none"          # none | cache | device
        self.loading = False
        self.error = ""

    def load_cached(self) -> None:
        last = self.library.last()
        if last is None:
            return
        self.serial, self.summary, self.tracks = last
        self.source = "cache"
        self.art.set_serial(self.serial)
        self.changed.emit()

    def refresh(self, full: bool = False) -> None:
        """Summary first; tracks come from the cache unless the commit moved."""
        if self.loading:
            return
        self.loading = True
        self.error = ""
        library = self.library
        self.changed.emit()

        def work(job):
            with library.session() as session:
                job.progress(None, "正在读取 iPod 信息…")
                summary = session.summary()
                serial = session.identity
                tracks = None if full else library.cache.load(serial, summary.commit_id)
                if tracks is None:
                    total = max(1, summary.track_count)

                    def progress(done):
                        job.check()
                        job.progress(done / total, f"正在读取资料库 {done}/{total}")
                    tracks = session.tracks(progress)
                    library.cache.store(serial, summary.commit_id, tracks)
                library.remember(serial, summary)
                return serial, summary, tracks

        self.runner.run("正在读取 iPod 资料库…", work, self._loaded, self._failed)

    def _loaded(self, result) -> None:
        self.serial, self.summary, self.tracks = result
        self.source = "device"
        self.loading = False
        self.art.set_serial(self.serial)
        self.art.reset_misses()
        self.changed.emit()

    def _failed(self, error) -> None:
        self.loading = False
        self.error = str(error) or type(error).__name__
        self.changed.emit()
        self.failed.emit(self.error)

    def delete(self, track, done=None) -> None:
        library = self.library

        def work(_job):
            with library.session() as session:
                session.delete(track)
                return session.summary()

        def finished(summary):
            self.tracks = [t for t in self.tracks if t.index != track.index]
            # The iPod renumbers its database after the refresh, so the cache
            # is left for the next full read to replace.
            self.summary = summary
            self.track_removed.emit(track)
            self.changed.emit()
            if done is not None:
                done(None)

        def failed(error):
            if done is not None:
                done(error)

        self.runner.run(f"正在删除「{track.title}」…", work, finished, failed)
