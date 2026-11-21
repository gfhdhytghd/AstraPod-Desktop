"""Item models for the library: songs table, albums grid, browser lists.

SongsModel sorts its own row list in Python (one key per row, one list sort)
and SongsProxy only filters; a proxy that sorted would call back into Python
for every comparison, which is what makes a 10k-row header click stall.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import functools

from . import contracts, fmt
from .qt import QtCore, Qt

TRACK_ROLE = Qt.ItemDataRole.UserRole + 1
RATING_ROLE = Qt.ItemDataRole.UserRole + 2
ALBUM_ROLE = Qt.ItemDataRole.UserRole + 3
COUNT_ROLE = Qt.ItemDataRole.UserRole + 4


@dataclass(frozen=True)
class Column:
    key: str
    title: str
    width: int
    visible: bool
    right: bool = False


COLUMNS = [
    Column("title", "名称", 260, True),
    Column("length", "时间", 64, True, True),
    Column("artist", "艺人", 170, True),
    Column("album", "专辑", 190, True),
    Column("album_artist", "专辑艺人", 150, False),
    Column("genre", "类型", 110, True),
    Column("composer", "作曲者", 130, False),
    Column("year", "年份", 56, False, True),
    Column("disc", "光盘", 52, False, True),
    Column("track", "音轨", 52, False, True),
    Column("bitrate", "比特率", 92, False, True),
    Column("plays", "播放次数", 76, True, True),
    Column("rating", "评分", 92, True),
    Column("last_played", "上次播放", 92, False, True),
    Column("mtime", "修改日期", 140, False),
    Column("kind", "种类", 130, False),
    Column("path", "位置", 320, False),
]
COLUMN_INDEX = {column.key: i for i, column in enumerate(COLUMNS)}


TEXT_FIELDS = ("title", "artist", "album", "album_artist", "genre", "composer")


def collation_ranks(values) -> dict[str, int]:
    """Order strings as a Chinese reader expects (pinyin), numbers numerically."""
    collator = QtCore.QCollator(QtCore.QLocale(QtCore.QLocale.Language.Chinese,
                                               QtCore.QLocale.Country.China))
    collator.setNumericMode(True)
    collator.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    ordered = sorted(set(values), key=functools.cmp_to_key(collator.compare))
    return {value: rank for rank, value in enumerate(ordered)}


class SongsModel(QtCore.QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.tracks: list = []
        self.haystack: list[str] = []
        self.recency: dict[int, int] = {}
        self.sort_column = COLUMN_INDEX["artist"]
        self.sort_order = Qt.SortOrder.AscendingOrder
        self.ranks: dict[str, dict] = {}

    def set_tracks(self, tracks) -> None:
        self.beginResetModel()
        self.tracks = list(tracks)
        self._index()
        self._sort_list()
        self.endResetModel()

    def _index(self) -> None:
        self.haystack = [
            " ".join((t.title, t.artist, t.album, t.album_artist, t.genre,
                      t.composer, str(t.year or ""))).casefold()
            for t in self.tracks]
        played = sorted((t for t in self.tracks if t.last_played),
                        key=lambda t: t.last_played, reverse=True)
        self.recency = {t.index: rank for rank, t in enumerate(played, 1)}
        self.ranks = {name: collation_ranks(getattr(t, name) or "" for t in self.tracks)
                      for name in TEXT_FIELDS}

    def rowCount(self, parent=QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.tracks)

    def columnCount(self, parent=QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(COLUMNS):
            column = COLUMNS[section]
            if role == Qt.ItemDataRole.DisplayRole:
                return column.title
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return int((Qt.AlignmentFlag.AlignRight if column.right
                            else Qt.AlignmentFlag.AlignLeft) | Qt.AlignmentFlag.AlignVCenter)
        return None

    def display(self, track, key: str) -> str:
        if key == "title":
            return track.title or track.path.rsplit("/", 1)[-1]
        if key == "length":
            return fmt.duration(track.length_ms)
        if key in ("artist", "album", "album_artist", "genre", "composer"):
            return getattr(track, key) or ""
        if key in ("year", "disc", "track"):
            value = getattr(track, key)
            return str(value) if value else ""
        if key == "bitrate":
            return f"{track.bitrate_kbps} kbps" if track.bitrate_kbps else ""
        if key == "plays":
            return str(track.play_count) if track.play_count else ""
        if key == "last_played":
            rank = self.recency.get(track.index)
            return f"最近 #{rank}" if rank else ""
        if key == "mtime":
            return fmt.date(track.mtime)
        if key == "kind":
            return fmt.kind(track.path)
        if key == "path":
            return track.path
        return ""

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        track = self.tracks[index.row()]
        column = COLUMNS[index.column()]
        if role == Qt.ItemDataRole.DisplayRole:
            if column.key == "rating":
                return None
            return self.display(track, column.key)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int((Qt.AlignmentFlag.AlignRight if column.right
                        else Qt.AlignmentFlag.AlignLeft) | Qt.AlignmentFlag.AlignVCenter)
        if role == TRACK_ROLE:
            return track
        if role == RATING_ROLE:
            return track.rating
        if role == Qt.ItemDataRole.ToolTipRole and column.key == "path":
            return track.path
        return None

    def text_key(self, track, name: str) -> tuple:
        value = getattr(track, name) or ""
        if name == "title" and not value:
            value = track.path
        return (value == "", self.ranks.get(name, {}).get(value, 0))

    def sort_key(self, key: str):
        text = self.text_key
        if key in ("artist", "album_artist"):
            # iTunes sorts an artist's songs by album, disc and track.
            return lambda t: (text(t, key), text(t, "album"), t.disc, t.track)
        if key == "album":
            return lambda t: (text(t, "album"), t.disc, t.track)
        if key in TEXT_FIELDS:
            return lambda t: text(t, key)
        return {
            "path": lambda t: t.path.casefold(),
            "length": lambda t: t.length_ms, "year": lambda t: t.year,
            "disc": lambda t: t.disc, "track": lambda t: t.track,
            "bitrate": lambda t: t.bitrate_kbps, "plays": lambda t: t.play_count,
            "rating": lambda t: t.rating, "last_played": lambda t: t.last_played,
            "mtime": lambda t: t.mtime, "kind": lambda t: fmt.kind(t.path),
        }[key]

    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        if not 0 <= column < len(COLUMNS):
            return
        self.sort_column, self.sort_order = column, order
        self.layoutAboutToBeChanged.emit()
        old = list(self.tracks)
        self._sort_list()
        position = {id(t): row for row, t in enumerate(self.tracks)}
        persistent = self.persistentIndexList()
        if persistent:
            moved = [self.index(position[id(old[i.row()])], i.column()) for i in persistent]
            self.changePersistentIndexList(persistent, moved)
        self.layoutChanged.emit()

    def _sort_list(self) -> None:
        if self.sort_column < 0:
            return
        key = self.sort_key(COLUMNS[self.sort_column].key)
        pairs = sorted(zip(self.tracks, self.haystack), key=lambda pair: key(pair[0]),
                       reverse=self.sort_order == Qt.SortOrder.DescendingOrder)
        self.tracks = [p[0] for p in pairs]
        self.haystack = [p[1] for p in pairs]

    def remove_track(self, track) -> None:
        for row, item in enumerate(self.tracks):
            if item.index == track.index:
                self.beginRemoveRows(QtCore.QModelIndex(), row, row)
                del self.tracks[row]
                del self.haystack[row]
                self.endRemoveRows()
                return


class SongsProxy(QtCore.QSortFilterProxyModel):
    """Search terms and field constraints; sorting is passed to the source."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.terms: list[str] = []
        self.constraints: dict[str, str] = {}
        self.setDynamicSortFilter(False)

    def set_search(self, text: str) -> None:
        terms = text.casefold().split()
        if terms != self.terms:
            self.beginFilterChange()
            self.terms = terms
            self.endFilterChange()

    def set_constraints(self, constraints: dict[str, str]) -> None:
        if constraints != self.constraints:
            self.beginFilterChange()
            self.constraints = dict(constraints)
            self.endFilterChange()

    def filterAcceptsRow(self, row, parent) -> bool:
        model = self.sourceModel()
        if self.terms:
            hay = model.haystack[row]
            for term in self.terms:
                if term not in hay:
                    return False
        if self.constraints:
            track = model.tracks[row]
            for key, value in self.constraints.items():
                if field_value(track, key) != value:
                    return False
        return True

    def sort(self, column, order=Qt.SortOrder.AscendingOrder) -> None:
        self.sourceModel().sort(column, order)

    def tracks(self) -> list:
        model = self.sourceModel()
        return [model.tracks[self.mapToSource(self.index(row, 0)).row()]
                for row in range(self.rowCount())]


def field_value(track, key: str) -> str:
    if key == "artist":
        return track.album_artist or track.artist or ""
    if key == "album":
        return "\0".join(contracts.album_key(track))
    return getattr(track, key) or ""


def summary_line(tracks) -> str:
    total_ms = sum(t.length_ms for t in tracks)
    size = sum(fmt.estimated_bytes(t.length_ms, t.bitrate_kbps) for t in tracks)
    if not tracks:
        return "没有歌曲"
    return f"{fmt.songs(len(tracks))}，{fmt.long_duration(total_ms)}，约 {fmt.size(size)}"


@dataclass
class Album:
    key: tuple
    title: str
    artist: str
    year: int
    genre: str
    tracks: list = field(default_factory=list)

    @property
    def total_ms(self) -> int:
        return sum(t.length_ms for t in self.tracks)

    @property
    def lead(self):
        return self.tracks[0]


def group_albums(tracks) -> list[Album]:
    albums: dict[tuple, Album] = {}
    for track in tracks:
        key = contracts.album_key(track)
        album = albums.get(key)
        if album is None:
            album = albums[key] = Album(key, track.album or "未知专辑",
                                        track.album_artist or track.artist or "未知艺人",
                                        track.year, track.genre)
        album.tracks.append(track)
        album.year = album.year or track.year
    for album in albums.values():
        album.tracks.sort(key=lambda t: (t.disc, t.track, t.title.casefold()))
    artist_rank = collation_ranks(a.artist for a in albums.values())
    title_rank = collation_ranks(a.title for a in albums.values())
    return sorted(albums.values(),
                  key=lambda a: (artist_rank[a.artist], a.year, title_rank[a.title]))


class AlbumsModel(QtCore.QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.all: list[Album] = []
        self.albums: list[Album] = []
        self.terms: list[str] = []
        self.rows_by_key: dict = {}

    def set_tracks(self, tracks) -> None:
        self.beginResetModel()
        self.all = group_albums(tracks)
        self._filter()
        self.endResetModel()

    def set_search(self, text: str) -> None:
        terms = text.casefold().split()
        if terms == self.terms:
            return
        self.beginResetModel()
        self.terms = terms
        self._filter()
        self.endResetModel()

    def _filter(self) -> None:
        if not self.terms:
            self.albums = list(self.all)
        else:
            self.albums = [a for a in self.all
                           if all(term in f"{a.title} {a.artist} {a.genre} {a.year}".casefold()
                                  for term in self.terms)]
        self.rows_by_key = {album.key: row for row, album in enumerate(self.albums)}

    def rowCount(self, parent=QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.albums)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        album = self.albums[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return album.title
        if role == ALBUM_ROLE:
            return album
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{album.title}\n{album.artist}"
        return None

    def art_changed(self, key) -> None:
        row = self.rows_by_key.get(key)
        if row is not None:
            index = self.index(row)
            self.dataChanged.emit(index, index)


class ValuesModel(QtCore.QAbstractListModel):
    """Distinct values of one field with counts; row 0 is "all"."""

    def __init__(self, key: str, all_label: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.all_label = all_label
        self.values: list[tuple[str, str, int]] = []
        self.total = 0

    def set_tracks(self, tracks, terms: list[str] | None = None) -> None:
        self.beginResetModel()
        counts: dict[str, list] = {}
        for track in tracks:
            value = field_value(track, self.key)
            label = (track.album or "") if self.key == "album" else value
            entry = counts.setdefault(value, [label, 0])
            entry[1] += 1
        rows = [(value, label or "未知", count) for value, (label, count) in counts.items()]
        if terms:
            rows = [r for r in rows if all(t in r[1].casefold() for t in terms)]
        ranks = collation_ranks(r[1] for r in rows)
        rows.sort(key=lambda r: (r[0] == "", ranks[r[1]]))
        self.values = rows
        self.total = len(tracks)
        self.endResetModel()

    def rowCount(self, parent=QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.values) + 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if role == Qt.ItemDataRole.DisplayRole:
            if row == 0:
                return f"{self.all_label}（{len(self.values)}）"
            return self.values[row - 1][1]
        if role == COUNT_ROLE:
            return self.total if row == 0 else self.values[row - 1][2]
        if role == Qt.ItemDataRole.UserRole:
            return None if row == 0 else self.values[row - 1][0]
        return None
