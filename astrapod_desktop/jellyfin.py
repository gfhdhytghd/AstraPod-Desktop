"""The Jellyfin library behind the sidebar's 资料库 pages.

It is read-only and follows the session the Jellyfin 同步 page holds: signing
in, restoring or signing out there changes what these pages show. Tracks and
covers are read as network jobs (device=False), so they never wait behind the
iPod, and only once a Jellyfin page has been opened.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import calendar
import time

from . import contracts
from .artwork import NO_ART, SIZES, newest, requeue
from .models import SongsModel
from .qt import QtCore, QtGui, Signal


@dataclass(frozen=True)
class JellyfinTrack:
    """A Jellyfin audio item with the attributes the library pages read."""
    index: int
    path: str
    title: str
    artist: str
    album: str
    album_artist: str
    genre: str
    composer: str
    grouping: str
    comment: str
    year: int
    disc: int
    track: int
    length_ms: int
    bitrate_kbps: int
    play_count: int
    rating: int
    play_time_ms: int
    last_played: int
    mtime: int
    managed: bool
    item_id: str
    album_id: str


def _epoch(value) -> int:
    """Jellyfin's ISO 8601 UTC date as epoch seconds; 0 when absent."""
    if not isinstance(value, str) or len(value) < 19:
        return 0
    try:
        return calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0


def _int(value) -> int:
    return value if type(value) is int and value > 0 else 0


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _names(values) -> list[str]:
    return [v for v in values if isinstance(v, str) and v] if isinstance(values, list) else []


def track_from_item(row: dict, index: int) -> JellyfinTrack | None:
    """One Items row (Fields=Genres,People,MediaSources,DateCreated,Path)."""
    item_id = row.get("Id")
    if not isinstance(item_id, str) or not item_id or row.get("Type", "Audio") != "Audio":
        return None
    sources = row.get("MediaSources")
    source = sources[0] if isinstance(sources, list) and sources and isinstance(sources[0], dict) else {}
    people = row.get("People") if isinstance(row.get("People"), list) else []
    composers = [p.get("Name") for p in people
                 if isinstance(p, dict) and p.get("Type") == "Composer"]
    user = row.get("UserData") if isinstance(row.get("UserData"), dict) else {}
    artists = _names(row.get("Artists"))
    return JellyfinTrack(
        index=index, path=_text(row.get("Path")) or _text(source.get("Path")),
        title=_text(row.get("Name")), artist=", ".join(artists),
        album=_text(row.get("Album")),
        album_artist=_text(row.get("AlbumArtist")) or (artists[0] if artists else ""),
        genre=", ".join(_names(row.get("Genres"))), composer=", ".join(_names(composers)),
        grouping="", comment="", year=_int(row.get("ProductionYear")),
        disc=_int(row.get("ParentIndexNumber")), track=_int(row.get("IndexNumber")),
        length_ms=_int(row.get("RunTimeTicks")) // 10_000,
        bitrate_kbps=_int(source.get("Bitrate")) // 1000,
        play_count=_int(user.get("PlayCount")), rating=0, play_time_ms=0,
        last_played=_epoch(user.get("LastPlayedDate")), mtime=_epoch(row.get("DateCreated")),
        managed=False, item_id=item_id, album_id=_text(row.get("AlbumId")))


class JellyfinArt(QtCore.QObject):
    """Album covers from Jellyfin, kept in memory. get() has ArtStore's
    contract: a QImage, NO_ART, or None while the cover loads."""

    art_ready = Signal(object)          # album key

    BATCH = 24

    def __init__(self, library: "JellyfinLibrary", parent=None):
        super().__init__(parent)
        self.library = library
        self._memory: OrderedDict = OrderedDict()
        self._queued: dict = {}
        self._inflight: set = set()
        self._none: set = set()
        self._generation = 0
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._fetch)

    def clear(self) -> None:
        self._generation += 1
        self._memory.clear()
        self._queued.clear()
        self._inflight.clear()
        self._none.clear()

    def get(self, track, size: str = "coverflow"):
        key = (contracts.album_key(track), size)
        image = self._memory.get(key)
        if image is not None:
            self._memory.move_to_end(key)
            return image
        if key in self._none or self.library.connection is None:
            return NO_ART
        if key not in self._inflight:
            requeue(self._queued, key, track)
            if not self._timer.isActive():
                self._timer.start()
        return None

    def _fetch(self) -> None:
        connection = self.library.connection
        if not self._queued or connection is None:
            return
        generation = self._generation
        batch = newest(self._queued, self.BATCH)
        self._inflight.update(key for key, _track in batch)
        backend = self.library.backend

        def work(job):
            for key, track in batch:
                if job.cancelled:
                    break
                try:
                    data = backend.album_art(connection, track, SIZES.get(key[1], 128))
                except OSError:
                    data = None
                job.progress(data=(key, QtGui.QImage.fromData(data) if data else QtGui.QImage()))

        def progress(_fraction, _text, result):
            if generation != self._generation:
                return
            key, image = result
            self._inflight.discard(key)
            if image.isNull():
                self._none.add(key)
            else:
                self._memory[key] = image
                while len(self._memory) > 600:
                    self._memory.popitem(last=False)
            self.art_ready.emit(key[0])

        def done(_result):
            if generation != self._generation:
                return
            for key, _track in batch:
                if key in self._inflight:
                    self._inflight.discard(key)
                    self._none.add(key)
                    self.art_ready.emit(key[0])
            if self._queued and not self._timer.isActive():
                self._timer.start()

        self.library.runner.run("读取 Jellyfin 插图", work, done, done, device=False,
                                foreground=False, on_progress=progress)


class JellyfinLibrary(QtCore.QObject):
    """Tracks of the signed-in Jellyfin account, with the surface the
    library pages read from LibraryState."""

    changed = Signal()

    editable = False
    search_hint = "搜索 Jellyfin 资料库"

    def __init__(self, backend, runner, open_sign_in, parent=None):
        super().__init__(parent)
        self.backend = backend
        self.runner = runner
        self.open_sign_in = open_sign_in
        self.connection = None
        self.state = "signed_out"       # signed_out | connecting | offline | online
        self.tracks: list = []
        self.loaded = False
        self.loading = False
        self.error = ""
        self.progress_text = ""
        self.wanted = False
        self._generation = 0
        self.songs_model = SongsModel(self)
        self.art = JellyfinArt(self, self)

    def follow(self, sync_page) -> None:
        """Take the sign-in state and connection of the Jellyfin 同步 page."""
        connection = sync_page.source
        if sync_page.session is None:
            state = "signed_out"
        elif connection is not None:
            state = "online"
        elif sync_page.restoring:
            state = "connecting"
        else:
            state = "offline"
        if connection is self.connection and state == self.state:
            return
        self.state = state
        if connection is not self.connection:
            self.connection = connection
            self._generation += 1
            self.tracks = []
            self.loaded = self.loading = False
            self.error = ""
            self.art.clear()
            self.songs_model.set_tracks([])
        self.changed.emit()
        if self.wanted:
            self.ensure_loaded()

    def ensure_loaded(self) -> None:
        """Read the library the first time a Jellyfin page is shown."""
        self.wanted = True
        if not self.loaded and not self.error:
            self.refresh()

    def refresh(self, _full: bool = False) -> None:
        if self.connection is None or self.loading:
            return
        connection, generation, backend = self.connection, self._generation, self.backend
        self.loading = True
        self.error = ""
        self.progress_text = "正在读取 Jellyfin 资料库…"
        self.changed.emit()

        def work(job):
            def progress(done, total):
                job.check()
                job.progress(done / max(1, total), f"正在读取 Jellyfin 资料库 {done:,}/{total:,}")
            return backend.library_tracks(connection, progress)

        def done(tracks):
            if generation != self._generation:
                return
            self.loading, self.loaded = False, True
            self.tracks = list(tracks)
            self.songs_model.set_tracks(self.tracks)
            self.changed.emit()

        def failed(error):
            if generation != self._generation:
                return
            self.loading = False
            self.error = str(error) or type(error).__name__
            self.changed.emit()

        def progress(_fraction, text, _data):
            if generation == self._generation and text:
                self.progress_text = text
                self.changed.emit()

        self.runner.run("正在读取 Jellyfin 资料库…", work, done, failed, device=False,
                        foreground=False, on_progress=progress)

    def status_line(self) -> str:
        if self.state == "online":
            if self.loading:
                return self.progress_text
            if self.error:
                return f"读取失败：{self.error}"
            return "来自 Jellyfin，只能浏览" if self.loaded else "Jellyfin 已连接"
        return {"signed_out": "没有登录 Jellyfin", "connecting": "正在连接 Jellyfin…",
                "offline": "无法连接 Jellyfin"}[self.state]

    def empty_content(self, _kind: str, noun: str) -> tuple:
        """(title, text, action, slot) for a page with nothing to show."""
        sign_in = ("前往 Jellyfin 同步", self.open_sign_in)
        if self.state == "signed_out":
            return ("没有登录 Jellyfin",
                    f"在「Jellyfin 同步」页登录后，这里会显示 Jellyfin 资料库里的{noun}。", *sign_in)
        if self.state == "connecting":
            return "正在连接 Jellyfin…", "正在用保存的登录连接服务器。", None, None
        if self.state == "offline":
            return ("无法连接 Jellyfin",
                    "登录仍然保留。可以在「Jellyfin 同步」页点「重新连接」。", *sign_in)
        if self.error:
            return "读取 Jellyfin 资料库失败", self.error, "重试", self.refresh
        if self.loading or not self.loaded:
            return "正在读取 Jellyfin 资料库…", self.progress_text, None, None
        return f"Jellyfin 里没有{noun}", "这个账号在 Jellyfin 里看不到任何音乐。", None, None
