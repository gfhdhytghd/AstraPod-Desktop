"""The library the window shows: summary and tracks, from cache or device."""
from __future__ import annotations

from .qt import QtCore, Signal


class LibraryState(QtCore.QObject):
    changed = Signal()
    failed = Signal(str)
    track_removed = Signal(object)

    editable = True
    search_hint = "搜索 iPod 资料库"

    def __init__(self, library, runner, art, parent=None):
        super().__init__(parent)
        self.library = library
        self.runner = runner
        self.art = art
        self.serial = None
        self.summary = None
        self.stock_info = None
        self.absent_stock = None
        self.stock = False
        self.playlists = ()
        self.tracks: list = []
        self.source = "none"          # none | cache | device
        self.loading = False
        self.error = ""
        self.songs_model = None         # the window's SongsModel of these tracks

    def load_cached(self) -> None:
        last = self.library.last()
        if last is None:
            return
        self.serial, self.summary, self.tracks = last[:3]
        self.stock = len(last) == 4
        self.playlists = last[3] if self.stock else ()
        self.source = "cache"
        self.art.set_serial(self.art_identity)
        self.changed.emit()

    @property
    def art_identity(self):
        if self.stock and self.summary is not None:
            return f"stock:{self.serial}:{self.summary.commit_id}"
        return self.serial

    @property
    def can_delete(self):
        device = getattr(self.library, "device", None)
        return not self.stock and (device is None or
                                   (device.kind == "podbox-podlink" and device.serial == self.serial))

    def device_changed(self, device):
        if device != self.absent_stock:
            self.absent_stock = None
        mode = "stock" if self.stock else "podbox-podlink"
        if self.source == "device" and (device.serial != self.serial or device.kind != mode):
            self.source = "cache"
            self.stock_info = None
        self.art.enabled = self.source == "device"
        self.changed.emit()

    def snapshot(self):
        if self.serial and self.summary is not None and self.summary.database_ready:
            if len(self.tracks) == self.summary.track_count:
                return self.serial, self.summary.commit_id, tuple(self.tracks)
        return None

    def refresh(self, full: bool = False) -> None:
        """Summary first; tracks come from the cache unless the commit moved."""
        if self.loading:
            return
        library = self.library
        device = getattr(library, "device", None)
        if device is not None and (device.kind == "none" or device == self.absent_stock):
            return
        self.loading = True
        self.error = ""
        session_for_device = getattr(library, "session_for_device", None)
        open_session = session_for_device(authorize=full) if session_for_device else library.session
        self.changed.emit()

        def work(job):
            if device is not None and device.kind == "stock":
                from hrstockdevice import StockDeviceDisconnected

                try:
                    with open_session() as session:
                        job.progress(None, "正在读取 iPod 信息…")
                        summary, tracks, playlists, info = session.snapshot(library, full=full)
                        library.remember(session.identity, summary, stock=True)
                        return session.identity, summary, tracks, playlists, info
                except StockDeviceDisconnected:
                    return None
            with open_session() as session:
                job.progress(None, "正在读取 iPod 信息…")
                summary = session.summary()
                serial = session.identity
                tracks = None if full else library.cache.load(serial, summary.commit_id)
                if tracks is None or len(tracks) != summary.track_count:
                    total = max(1, summary.track_count)

                    def progress(done):
                        job.check()
                        job.progress(done / total, f"正在读取资料库 {done}/{total}")
                    def byte_progress(done, size):
                        job.check()
                        job.progress(done / size, f"正在读取资料库 {done:,}/{size:,} 字节")

                    tracks = session.tracks(progress, byte_progress=byte_progress)
                    library.cache.store(serial, summary.commit_id, tracks)
                library.remember(serial, summary)
                return serial, summary, tracks, (), None

        def completed(callback, result):
            if getattr(library, "device", None) == device:
                callback(result)
            else:
                self.loading = False
                self.changed.emit()

        self.runner.run("正在读取 iPod 资料库…", work,
                        lambda result: completed(self._loaded if result is not None else self._stock_disconnected,
                                                 result),
                        lambda error: completed(self._failed, error))

    def _loaded(self, result) -> None:
        self.serial, self.summary, self.tracks, self.playlists, self.stock_info = result
        self.stock = self.stock_info is not None
        self.source = "device"
        self.loading = False
        self.art.set_serial(self.art_identity)
        self.art.enabled = True
        self.art.reset_misses()
        self.changed.emit()

    def _stock_disconnected(self, _result) -> None:
        self.loading = False
        self.absent_stock = self.library.device
        if self.source == "device":
            self.source = "cache"
        self.stock_info = None
        self.art.enabled = False
        self.changed.emit()

    def _failed(self, error) -> None:
        self.loading = False
        self.error = str(error) or type(error).__name__
        self.changed.emit()
        self.failed.emit(self.error)

    def status_line(self) -> str:
        if self.loading:
            return "正在从 iPod 读取…"
        if self.source == "device":
            return "来自已连接的 iPod"
        if self.source == "cache":
            return "上次读取的资料库；iPod 连接后可重新读取"
        if self.error:
            return f"读取失败：{self.error}"
        return "iPod 上的全部歌曲"

    def empty_content(self, kind: str, noun: str) -> tuple:
        """(title, text, action, slot) for a page with nothing to show."""
        read = ("读取 iPod 资料库", lambda: self.refresh(True))
        if kind == "songs":
            return ("还没有资料库", "用 USB 连接 iPod Classic，然后读取资料库。"
                    "读取过的资料库会保存在电脑上，下次打开时直接显示。", *read)
        if kind == "albums":
            return "还没有专辑", "读取 iPod 资料库后，专辑和插图会显示在这里。", *read
        return f"还没有{noun}", f"读取 iPod 资料库后可以按{noun}浏览歌曲。", *read

    def delete(self, track, done=None) -> None:
        if not self.can_delete:
            if done is not None:
                done(RuntimeError("请连接歌曲来源的 PodBox，或在 Jellyfin 同步中移除原厂固件的歌曲。"))
            return
        library = self.library
        session_for_device = getattr(library, "session_for_device", None)
        open_session = session_for_device() if session_for_device else library.session

        def work(_job):
            with open_session() as session:
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
