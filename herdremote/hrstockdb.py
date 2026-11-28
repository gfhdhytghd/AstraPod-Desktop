"""Native libgpod bridge for a disposable stock iPod metadata staging tree.

Stage database files in a private host directory before calling open_database().
A writer stages iTunes, Device and Artwork in full. A track reader needs the
active database, Play Counts and OTGPlaylistInfo sidecars. libgpod may modify
metadata while opening a database. Only pass staging paths here. A cover reader
may stage references to pinned thumbnail blobs, provided it never calls save().
save() returns changed
files relative to that directory. removed_files lists paths consumed or removed
by libgpod.
The caller publishes those changes and any media files as one transaction.

The optional Linux runtime is libgpod.so.4 (Arch extra/libgpod). Its public
Itdb structures provide data fields for which libgpod has no accessor API.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from ctypes import (
    CDLL, POINTER, Structure, byref, c_char_p, c_float, c_int, c_int16, c_int32,
    c_int64, c_uint8, c_uint16, c_uint32, c_uint64, c_void_p, cast, sizeof, string_at,
)
from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
import plistlib
import random
import re
from threading import Lock
import time
from xml.parsers.expat import ExpatError


_CONTROL = Path("iPod_Control")
_ITUNES = _CONTROL / "iTunes"
_DEVICE = _CONTROL / "Device"
_ARTWORK = _CONTROL / "Artwork"
_ITUNES_DB = _ITUNES / "iTunesDB"
_COMPRESSED_DB = _ITUNES / "iTunesCDB"
_HASH_INFO = _DEVICE / "HashInfo"
_SQLITE_DIR = _ITUNES / "iTunes Library.itlp"
_SQLITE_FILES = (
    "Dynamic.itdb", "Extras.itdb", "Genius.itdb", "Library.itdb",
    "Locations.itdb", "Locations.itdb.cbk",
)
_SUPPORTED_TRACK_PATH = re.compile(r":iPod_Control:Music:F[0-9]{2}:[^:/\\\x00]+", re.I)
_CLASSIC_NUMBERS = frozenset(("B029", "B147", "B145", "B150", "B562", "B565", "C293", "C297"))
_DB_VERSIONS = {1: 3, 2: 4}
_SQL_COMMANDS = "com.apple.mobile.iTunes.SQLMusicLibraryPostProcessCommands"
_NATIVE_IO_LOCK = Lock()


class StockDatabaseError(RuntimeError):
    """The stock database could not be read or written."""


class StockDatabaseUnavailable(StockDatabaseError):
    """The installed native library does not provide the required ABI."""


@dataclass(frozen=True)
class DatabaseTrack:
    id: int = 0
    path: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    album_artist: str = ""
    genre: str = ""
    composer: str = ""
    grouping: str = ""
    comment: str = ""
    year: int = 0
    disc: int = 0
    track: int = 0
    length_ms: int = 0
    bitrate_kbps: int = 0
    play_count: int = 0
    rating: int = 0
    last_played: int = 0
    mtime: int = 0
    size_bytes: int = 0
    media_type: int = 1
    has_artwork: bool = False
    show: str = ""
    episode: str = ""
    season: int = 0
    episode_number: int = 0


@dataclass(frozen=True)
class DatabasePlaylist:
    id: int
    name: str
    track_ids: tuple[int, ...]
    is_master: bool


class _GList(Structure):
    pass


_List = POINTER(_GList)
_GList._fields_ = [("data", c_void_p), ("next", _List), ("prev", _List)]


class _Error(Structure):
    _fields_ = [("domain", c_uint32), ("code", c_int), ("message", c_char_p)]


class _DatabasePrivate(Structure):
    # libgpod 0.8.x has no accessor for hidden firmware category playlists.
    _fields_ = [("mhsd5_playlists", _List)]


class _Database(Structure):
    _fields_ = [
        ("tracks", _List), ("playlists", _List), ("filename", c_char_p),
        ("device", c_void_p), ("version", c_uint32), ("id", c_uint64),
        ("tzoffset", c_int32), ("reserved_int2", c_int32),
        ("priv", POINTER(_DatabasePrivate)),
    ]


class _Playlist(Structure):
    _fields_ = [
        ("itdb", c_void_p), ("name", c_char_p), ("type", c_uint8),
        ("flag1", c_uint8), ("flag2", c_uint8), ("flag3", c_uint8),
        ("num", c_int), ("members", _List), ("is_spl", c_int),
        ("timestamp", c_int64), ("id", c_uint64),
    ]


# Public Itdb_Track prefix through mediatype from libgpod 0.8.x's itdb.h.
# Strings use c_void_p because native track destruction calls g_free on them.
class _Track(Structure):
    _fields_ = [
        (name, c_void_p) for name in (
            "itdb title ipod_path album artist genre filetype comment category composer grouping "
            "description podcasturl podcastrss chapterdata subtitle tvshow tvepisode tvnetwork "
            "albumartist keywords sort_artist sort_title sort_album sort_albumartist sort_composer "
            "sort_tvshow"
        ).split()
    ] + [
        ("id", c_uint32), ("size", c_uint32), ("tracklen", c_int32),
        ("cd_nr", c_int32), ("cds", c_int32), ("track_nr", c_int32),
        ("tracks", c_int32), ("bitrate", c_int32), ("samplerate", c_uint16),
        ("samplerate_low", c_uint16), ("year", c_int32), ("volume", c_int32),
        ("soundcheck", c_uint32), ("time_added", c_int64), ("time_modified", c_int64),
        ("time_played", c_int64), ("bookmark_time", c_uint32), ("rating", c_uint32),
        ("playcount", c_uint32), ("playcount2", c_uint32), ("recent_playcount", c_uint32),
        ("transferred", c_int), ("BPM", c_int16), ("app_rating", c_uint8),
        ("type1", c_uint8), ("type2", c_uint8), ("compilation", c_uint8),
        ("starttime", c_uint32), ("stoptime", c_uint32), ("checked", c_uint8),
        ("dbid", c_uint64), ("drm_userid", c_uint32), ("visible", c_uint32),
        ("filetype_marker", c_uint32), ("artwork_count", c_uint16),
        ("artwork_size", c_uint32), ("samplerate2", c_float), ("unk126", c_uint16),
        ("unk132", c_uint32), ("time_released", c_int64), ("unk144", c_uint16),
        ("explicit_flag", c_uint16), ("unk148", c_uint32), ("unk152", c_uint32),
        ("skipcount", c_uint32), ("recent_skipcount", c_uint32), ("last_skipped", c_uint32),
        ("has_artwork", c_uint8), ("skip_when_shuffling", c_uint8),
        ("remember_playback_position", c_uint8), ("flag4", c_uint8), ("dbid2", c_uint64),
        ("lyrics_flag", c_uint8), ("movie_flag", c_uint8), ("mark_unplayed", c_uint8),
        ("unk179", c_uint8), ("unk180", c_uint32), ("pregap", c_uint32),
        ("samplecount", c_uint64), ("unk196", c_uint32), ("postgap", c_uint32),
        ("unk204", c_uint32), ("mediatype", c_uint32),
        ("season_nr", c_uint32), ("episode_nr", c_uint32),
    ]


_DbPointer = POINTER(_Database)
_TrackPointer = POINTER(_Track)
_PlaylistPointer = POINTER(_Playlist)
_ErrorPointer = POINTER(_Error)
_ErrorOut = POINTER(_ErrorPointer)


@lru_cache(maxsize=1)
def _library() -> CDLL:
    if os.name != "posix" or sizeof(c_void_p) != 8:
        raise StockDatabaseUnavailable("Stock iPod support requires 64-bit libgpod.")
    try:
        library = CDLL("libgpod.so.4")
        signatures = (
            ("g_strdup", c_void_p, c_char_p),
            ("g_error_free", None, _ErrorPointer),
            ("g_list_delete_link", _List, _List, _List),
            ("g_list_free", None, _List),
            ("itdb_parse", _DbPointer, c_char_p, _ErrorOut),
            ("itdb_free", None, _DbPointer),
            ("itdb_write", c_int, _DbPointer, _ErrorOut),
            ("itdb_device_supports_artwork", c_int, c_void_p),
            ("itdb_playlist_mpl", _PlaylistPointer, _DbPointer),
            ("itdb_playlist_is_mpl", c_int, _PlaylistPointer),
            ("itdb_playlist_is_podcasts", c_int, _PlaylistPointer),
            ("itdb_playlist_new", _PlaylistPointer, c_char_p, c_int),
            ("itdb_playlist_add", None, _DbPointer, _PlaylistPointer, c_int),
            ("itdb_playlist_remove", None, _PlaylistPointer),
            ("itdb_playlist_add_track", None, _PlaylistPointer, _TrackPointer, c_int32),
            ("itdb_track_new", _TrackPointer),
            ("itdb_track_free", None, _TrackPointer),
            ("itdb_track_add", None, _DbPointer, _TrackPointer, c_int32),
            ("itdb_track_has_thumbnails", c_int, _TrackPointer),
            ("itdb_track_set_thumbnails", c_int, _TrackPointer, c_char_p),
        )
        for name, result, *arguments in signatures:
            function = getattr(library, name)
            function.restype = result
            function.argtypes = arguments
    except (OSError, AttributeError) as exc:
        raise StockDatabaseUnavailable(
            "Stock iPod support requires libgpod.so.4 (the libgpod package)."
        ) from exc
    return library


@lru_cache(maxsize=1)
def _thumbnail_library() -> CDLL:
    library = _library()
    try:
        signatures = (
            ("itdb_track_get_thumbnail", c_void_p, _TrackPointer, c_int, c_int),
            ("gdk_pixbuf_get_pixels", c_void_p, c_void_p),
            ("gdk_pixbuf_get_width", c_int, c_void_p),
            ("gdk_pixbuf_get_height", c_int, c_void_p),
            ("gdk_pixbuf_get_rowstride", c_int, c_void_p),
            ("gdk_pixbuf_get_n_channels", c_int, c_void_p),
            ("g_object_unref", None, c_void_p),
        )
        for name, result, *arguments in signatures:
            function = getattr(library, name)
            function.restype = result
            function.argtypes = arguments
    except AttributeError as exc:
        raise StockDatabaseUnavailable("libgpod does not provide GdkPixbuf artwork support.") from exc
    return library


def _thumbnail(track, size):
    library = _thumbnail_library()
    pixbuf = library.itdb_track_get_thumbnail(track, size, size)
    if not pixbuf:
        return None
    try:
        width = library.gdk_pixbuf_get_width(pixbuf)
        height = library.gdk_pixbuf_get_height(pixbuf)
        stride = library.gdk_pixbuf_get_rowstride(pixbuf)
        channels = library.gdk_pixbuf_get_n_channels(pixbuf)
        pixels = library.gdk_pixbuf_get_pixels(pixbuf)
        if not (0 < width <= 1024 and 0 < height <= 1024 and pixels
                and channels in (3, 4) and stride >= width * channels):
            return None
        data = b"".join(string_at(pixels + row * stride, width * channels)
                        for row in range(height))
        if channels == 4:
            rgb = bytearray(width * height * 3)
            for channel in range(3):
                rgb[channel::3] = data[channel::4]
            data = bytes(rgb)
        return width, height, data
    finally:
        library.g_object_unref(pixbuf)


def _native_call(operation: str, function, *args):
    error = _ErrorPointer()
    # libgpod's SQLite writer and signature code share process-wide state.
    with _NATIVE_IO_LOCK:
        result = function(*args, byref(error))
    if error:
        message = _text(error.contents.message)
        _library().g_error_free(error)
        if isinstance(result, _DbPointer) and result:
            _library().itdb_free(result)
        raise StockDatabaseError(f"{operation}: {message}")
    if not result:
        raise StockDatabaseError(operation)
    return result


def _entries(head: _List) -> Iterator[int]:
    while head:
        node = head.contents
        yield node.data
        head = node.next


def _text(value: int | bytes | None) -> str:
    if not value:
        return ""
    data = value if isinstance(value, bytes) else cast(value, c_char_p).value
    return data.decode("utf-8", errors="replace")


_TRACK_STRINGS = {
    "ipod_path": "path", "title": "title", "album": "album", "artist": "artist",
    "genre": "genre", "comment": "comment", "composer": "composer", "grouping": "grouping",
    "albumartist": "album_artist", "tvshow": "show", "tvepisode": "episode",
}
_TRACK_NUMBERS = {
    "size": "size_bytes", "tracklen": "length_ms", "cd_nr": "disc", "track_nr": "track",
    "bitrate": "bitrate_kbps", "year": "year", "time_modified": "mtime",
    "time_played": "last_played", "rating": "rating", "playcount": "play_count",
    "mediatype": "media_type", "season_nr": "season", "episode_nr": "episode_number",
}


def _signature(path: Path) -> tuple[int, bytes]:
    with path.open("rb") as stream:
        header = stream.read(0xA0)
    if len(header) < 24 or header[:4] != b"mhbd":
        raise StockDatabaseError("Invalid iTunes database header.")
    header_size = int.from_bytes(header[4:8], "little")
    if header_size < 24 or header_size > path.stat().st_size:
        raise StockDatabaseError("Invalid iTunes database header size.")
    if header_size < 0x32:
        return 0, b""
    scheme = int.from_bytes(header[0x30:0x32], "little")
    if scheme == 1 and header_size >= 0x6C:
        return scheme, header[0x58:0x6C]
    if scheme == 2 and header_size >= 0xA0:
        return scheme, header[0x72:0xA0]
    if scheme == 0:
        return scheme, b""
    raise StockDatabaseError(f"Unsupported iTunes database checksum scheme {scheme}.")


def _stage_path(root: Path, relative: Path) -> Path:
    target = root / relative
    if target.exists() or not target.parent.is_dir():
        return target
    matches = [child for child in target.parent.iterdir() if child.name.casefold() == target.name.casefold()]
    if len(matches) > 1:
        raise StockDatabaseError(f"Ambiguous stock database path: {relative}")
    return matches[0] if matches else target


def _library_files(root: Path) -> dict[Path, tuple[int, int, int, int]]:
    result = {}

    def record(path: Path) -> None:
        stat = path.stat()
        result[path.relative_to(root)] = (
            stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
        )

    for folder in (_ITUNES, _ARTWORK):
        for path in (root / folder).rglob("*"):
            if path.is_file():
                record(path)
    hash_info = root / _HASH_INFO
    if hash_info.is_file() and hash_info.stat().st_size == 54:
        with hash_info.open("rb") as stream:
            if stream.read(6) == b"HASHv0":
                record(hash_info)
    return result


def _device_metadata(
    root: Path, model_number: str | None, firewire_guid: str | None, source_scheme: int,
    source_compressed: bool, checksum: int | None, sqlite: bool | None, artwork_only: bool,
) -> tuple[int, bool, bool]:
    # Prefer the actual database scheme, then the capabilities in SysInfoExtended.
    # Use known model fallbacks when neither source supplies a signature variant.
    extended_path = _stage_path(root, _DEVICE / "SysInfoExtended")
    extended = {}
    if extended_path.is_file():
        try:
            extended = plistlib.loads(extended_path.read_bytes())
        except (ValueError, plistlib.InvalidFileException, OverflowError, ExpatError) as exc:
            raise StockDatabaseError("Cannot parse staged SysInfoExtended.") from exc
        if not isinstance(extended, dict):
            raise StockDatabaseError("SysInfoExtended must contain a property dictionary.")

    sysinfo_path = _stage_path(root, _DEVICE / "SysInfo")
    sysinfo = {}
    if sysinfo_path.is_file():
        with sysinfo_path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                key, separator, value = line.partition(":")
                if separator:
                    sysinfo[key.strip()] = value.strip()
    model_number = model_number or sysinfo.get("ModelNumStr", "")
    model_key = re.match(r"^[A-Z]?([BC][0-9]{3})", model_number.upper())
    model_code = model_key[1] if model_key else ""
    if model_code not in _CLASSIC_NUMBERS:
        raise StockDatabaseError("Expected an iPod Classic model (6G, 6.5G or 7G).")
    device_version = extended.get("DBVersion", 0)
    if type(device_version) is not int or device_version not in (0, 3, 4):
        raise StockDatabaseError(f"Unsupported SysInfoExtended DBVersion {device_version!r}.")
    scheme = source_scheme or checksum or (2 if device_version == 4 else 1)
    if scheme not in _DB_VERSIONS:
        raise StockDatabaseError(f"Unsupported iTunes database checksum scheme {scheme}.")
    compressed = source_compressed or bool(sqlite) or extended.get("SQLiteDB") is True

    raw_guid = firewire_guid or extended.get("FireWireGUID") or sysinfo.get("FirewireGuid")
    guid = str(raw_guid or "").lower().removeprefix("0x")
    valid_guid = bool(re.fullmatch(r"[0-9a-f]{16}", guid)) and guid != "0" * 16
    model = "M" + model_code
    if sysinfo.get("ModelNumStr") != model or sysinfo.get("FirewireGuid") != (guid if valid_guid else None):
        sysinfo["ModelNumStr"] = model
        if valid_guid:
            sysinfo["FirewireGuid"] = guid
        else:
            sysinfo.pop("FirewireGuid", None)
        sysinfo_path.parent.mkdir(parents=True, exist_ok=True)
        sysinfo_path.write_text(
            "".join(f"{key}: {value}\n" for key, value in sysinfo.items()), encoding="utf-8"
        )

    if artwork_only and not (extended.get("AlbumArt") or extended.get("AlbumArt2")):
        # libgpod uses its Classic model table only when the plist is absent.
        extended_path.unlink(missing_ok=True)
        return scheme, compressed, False

    if extended or scheme != 1 or compressed:
        # libgpod handles format capabilities from this plist. Never let it run
        # SQL supplied by the mounted volume or override the verified model.
        extended.pop(_SQL_COMMANDS, None)
        # An empty serial keeps libgpod on the selected model without
        # triggering its NULL-serial assertion for every track.
        extended["SerialNumber"] = ""
        extended["DBVersion"] = _DB_VERSIONS[scheme]
        extended["SQLiteDB"] = compressed
        if valid_guid:
            extended["FireWireGUID"] = guid
        else:
            extended.pop("FireWireGUID", None)
        extended_path.parent.mkdir(parents=True, exist_ok=True)
        extended_path.write_bytes(plistlib.dumps(extended))

    hash_info = _stage_path(root, _HASH_INFO)
    if hash_info.is_file():
        with hash_info.open("rb") as stream:
            data = stream.read(55)
        if len(data) != 54 or not data.startswith(b"HASHv0"):
            hash_info.unlink()
        elif hash_info.name != _HASH_INFO.name:
            hash_info.rename(root / _HASH_INFO)

    return scheme, compressed, valid_guid and not artwork_only


class StockDatabase:
    """Stock database handle backed by a private host staging directory."""

    def __init__(
        self, root: Path, pointer: _DbPointer, checksum: int, writable: bool, compressed: bool,
        baseline: dict[Path, tuple[int, int, int, int]],
    ):
        self._root = root
        self._pointer = pointer
        self._checksum = checksum
        self._writable = writable
        self._compressed = compressed
        self._baseline = baseline
        self.removed_files: tuple[Path, ...] = ()

    def _get_pointer(self) -> _DbPointer:
        if not self._pointer:
            raise StockDatabaseError("The stock database is closed.")
        return self._pointer

    def _native_tracks(self) -> Iterator[_TrackPointer]:
        for addr in _entries(self._get_pointer().contents.tracks):
            yield cast(addr, _TrackPointer)

    def _native_playlists(self) -> Iterator[_PlaylistPointer]:
        for addr in _entries(self._get_pointer().contents.playlists):
            yield cast(addr, _PlaylistPointer)

    @property
    def tracks(self) -> tuple[DatabaseTrack, ...]:
        tracks = []
        for pointer in self._native_tracks():
            track = pointer.contents
            fields = {name: _text(getattr(track, field)) for field, name in _TRACK_STRINGS.items()}
            fields.update({name: getattr(track, field) for field, name in _TRACK_NUMBERS.items()})
            tracks.append(DatabaseTrack(
                id=track.dbid or track.id,
                has_artwork=bool(_library().itdb_track_has_thumbnails(pointer)), **fields,
            ))
        return tuple(tracks)

    def track_artwork(self, track_id, album_key, size):
        """Use this track's thumbnail, or a thumbnail from the same album."""
        library = _library()
        fallback = []
        for pointer in self._native_tracks():
            track = pointer.contents
            if not library.itdb_track_has_thumbnails(pointer):
                continue
            if (track.dbid or track.id) == track_id:
                thumbnail = _thumbnail(pointer, size)
                if thumbnail is not None:
                    return thumbnail
            elif album_key[0] and (
                    _text(track.album).strip().casefold(),
                    _text(track.albumartist or track.artist).strip().casefold()) == album_key:
                fallback.append(pointer)
        for pointer in fallback:
            thumbnail = _thumbnail(pointer, size)
            if thumbnail is not None:
                return thumbnail
        return None

    def random_artwork(self, previous, size):
        """Decode one random album thumbnail, excluding the previous album."""
        library = _library()
        albums = {}
        for pointer in self._native_tracks():
            track = pointer.contents
            if track.mediatype not in (0, 1) or not library.itdb_track_has_thumbnails(pointer):
                continue
            album = (_text(track.albumartist or track.artist).strip(),
                     _text(track.album).strip() or _text(track.title))
            if album != previous:
                albums.setdefault(album, []).append(pointer)
        keys = list(albums)
        random.shuffle(keys)
        for album in keys:
            for pointer in albums[album]:
                thumbnail = _thumbnail(pointer, size)
                if thumbnail is not None:
                    return album, thumbnail
        return None

    @property
    def playlists(self) -> tuple[DatabasePlaylist, ...]:
        playlists = []
        for pointer in self._native_playlists():
            playlist = pointer.contents
            tracks = (cast(addr, _TrackPointer).contents for addr in _entries(playlist.members))
            playlists.append(DatabasePlaylist(
                playlist.id, _text(playlist.name), tuple(track.dbid or track.id for track in tracks),
                bool(_library().itdb_playlist_is_mpl(pointer)),
            ))
        return tuple(playlists)

    def update_playlists(
        self, updates: Iterable[tuple[int | None, str, Iterable[int]]], removals: Iterable[int],
    ) -> tuple[int, ...]:
        """Update static playlists by ID and return their IDs in input order."""
        library = _library()
        database = self._get_pointer()
        playlists = {pointer.contents.id: pointer for pointer in self._native_playlists()}
        tracks = {track.contents.dbid or track.contents.id: track
                  for track in self._native_tracks()}

        def existing(playlist_id):
            playlist = playlists[playlist_id]
            if (playlist.contents.is_spl or library.itdb_playlist_is_mpl(playlist)
                    or library.itdb_playlist_is_podcasts(playlist)):
                raise StockDatabaseError("Cannot replace a special stock iPod playlist.")
            return playlist

        for playlist_id in removals:
            library.itdb_playlist_remove(existing(playlist_id))

        ids = []
        for playlist_id, name, members in updates:
            if playlist_id is None:
                playlist = library.itdb_playlist_new(name.encode("utf-8"), False)
                library.itdb_playlist_add(database, playlist, -1)
            else:
                playlist = existing(playlist_id)
                library.g_list_free(playlist.contents.members)
                playlist.contents.members = _List()
                playlist.contents.num = 0
            for track_id in reversed(tuple(members)):
                library.itdb_playlist_add_track(playlist, tracks[track_id], 0)
            ids.append(playlist.contents.id)
        return tuple(ids)

    def add_track(
        self, row: DatabaseTrack, *, filetype: str, sample_rate: int, artwork: Path | None = None,
    ) -> int:
        if not _SUPPORTED_TRACK_PATH.fullmatch(row.path):
            raise StockDatabaseError("A new track must use an Apple Music/Fxx path.")
        extension = Path(row.path).suffix.lower()
        if extension not in (".mp3", ".m4a", ".wav", ".m4v"):
            raise StockDatabaseError(f"Unsupported stock iPod file extension: {extension}")
        video = extension == ".m4v"
        if video != (row.media_type in (2, 32, 64)):
            raise StockDatabaseError("Video metadata and file extension must agree.")
        if not 0 < sample_rate <= 48000 or not 0 < row.size_bytes <= 0xFFFFFFFF:
            raise StockDatabaseError("Invalid sample rate or file size for a stock track.")
        if not 0 < row.length_ms <= 0x7FFFFFFF:
            raise StockDatabaseError("Invalid duration for a stock track.")

        library = _library()
        database = self._get_pointer()
        master = library.itdb_playlist_mpl(database)
        if not master:
            raise StockDatabaseError("The stock database has no master playlist.")
        if row.id and any(row.id == (track.contents.dbid or track.contents.id)
                          for track in self._native_tracks()):
            raise StockDatabaseError("The stock database already contains this track ID.")

        strings = {field: getattr(row, name) for field, name in _TRACK_STRINGS.items()}
        strings["filetype"] = filetype
        encoded = {field: value.replace("\x00", "").encode("utf-8") for field, value in strings.items()}
        pointer = library.itdb_track_new()
        try:
            track = pointer.contents
            for field, value in encoded.items():
                if value:
                    setattr(track, field, library.g_strdup(value))
            for field, name in _TRACK_NUMBERS.items():
                setattr(track, field, getattr(row, name))

            track.samplecount = row.length_ms * sample_rate // 1000
            track.samplerate = sample_rate
            track.time_added = int(time.time())
            track.transferred = 1
            track.type2 = int(extension == ".mp3")
            track.movie_flag = int(video)
            track.skip_when_shuffling = int(video)
            track.remember_playback_position = int(video)
            track.filetype_marker = int.from_bytes(
                extension[1:].upper().ljust(4).encode("ascii"), "big"
            )
            track.dbid = row.id
            if artwork is not None and library.itdb_device_supports_artwork(database.contents.device):
                if not library.itdb_track_set_thumbnails(pointer, os.fsencode(artwork)):
                    raise StockDatabaseError("libgpod could not load the track artwork.")
        except BaseException:
            library.itdb_track_free(pointer)
            raise

        library.itdb_track_add(database, pointer, -1)
        library.itdb_playlist_add_track(master, pointer, -1)
        return track.dbid

    def remove_tracks(self, ids: Iterable[int]) -> None:
        ids = set(ids)
        if not ids:
            return
        database = self._get_pointer().contents
        to_remove = [
            addr for addr in _entries(database.tracks)
            if (cast(addr, _TrackPointer).contents.dbid or
                cast(addr, _TrackPointer).contents.id) in ids
        ]
        if not to_remove:
            return
        addresses = set(to_remove)
        library = _library()

        # Remove every occurrence before freeing any track. A playlist can
        # contain the same track several times, and libgpod leaves the links
        # alone when the track is removed from its database.
        def without_removed(head: _List) -> _List:
            node = head
            while node:
                next_addr = cast(node.contents.next, c_void_p).value
                if node.contents.data in addresses:
                    head = library.g_list_delete_link(head, node)
                node = cast(next_addr, _List)
            return head

        for playlist in self._native_playlists():
            playlist.contents.members = without_removed(playlist.contents.members)
        if database.priv:
            for addr in _entries(database.priv.contents.mhsd5_playlists):
                playlist = cast(addr, _PlaylistPointer).contents
                playlist.members = without_removed(playlist.members)
        database.tracks = without_removed(database.tracks)
        for addr in to_remove:
            library.itdb_track_free(cast(addr, _TrackPointer))

    def save(self) -> tuple[Path, ...]:
        """Write changes into the staging tree and return paths to publish."""
        database = self._get_pointer()
        if not self._writable:
            raise StockDatabaseError("Stock iPod writes require a supported model and any signing GUID.")
        if not _library().itdb_playlist_mpl(database):
            raise StockDatabaseError("The stock database has no master playlist.")
        before = self._baseline
        _native_call("libgpod could not write the stock database", _library().itdb_write, database)
        after = _library_files(self._root)
        main = _COMPRESSED_DB if self._compressed else _ITUNES_DB
        scheme, signature = _signature(self._root / main)
        if scheme != self._checksum or (scheme and not any(signature)):
            raise StockDatabaseError("libgpod did not write the expected stock database signature.")
        mandatory = {_ITUNES_DB, main}
        if self._compressed:
            mandatory.update(_SQLITE_DIR / filename for filename in _SQLITE_FILES)
            if (self._root / _ITUNES_DB).stat().st_size != 0:
                raise StockDatabaseError("libgpod did not write the compressed database placeholder.")
            if any(path not in after or after[path][1] == 0 for path in mandatory - {_ITUNES_DB}):
                raise StockDatabaseError("libgpod did not complete the stock SQLite database bundle.")

        # libgpod reads this fallback plist but only clears its canonical Play
        # Counts and iTunesStats sidecars. Once counters are in the database,
        # retire the plist too so a subsequent parse cannot add them again.
        pending = {path.name.casefold(): path for path in before if path.parent == _ITUNES}
        plist = pending.get("playcounts.plist")
        if plist in after and not pending.keys() & {"play counts", "itunesstats"}:
            (self._root / plist).unlink()
            del after[plist]

        self.removed_files = tuple(sorted(before.keys() - after.keys() - {_HASH_INFO}))
        changed = {path for path, stat in after.items() if before.get(path) != stat}
        return tuple(sorted(changed | mandatory))


@contextmanager
def open_database(
    stage_root: Path, *, model_number: str | None = None, firewire_guid: str | None = None,
    checksum: int | None = None, sqlite: bool | None = None, artwork_only: bool = False,
) -> Iterator[StockDatabase]:
    """Parse a stock library in staging and close its native handle on exit."""
    library = _library()
    root = Path(stage_root).resolve()
    source = _stage_path(root, _COMPRESSED_DB)
    compressed = source.is_file()
    if not compressed:
        source = _stage_path(root, _ITUNES_DB)
    source_scheme, _ = _signature(source)
    checksum, compressed, writable = _device_metadata(
        root, model_number, firewire_guid, source_scheme, compressed, checksum, sqlite, artwork_only,
    )
    if checksum == 2 and compressed and source.stat().st_size < 0xA0:
        raise StockDatabaseError("Truncated iTunes database hash72 header.")
    baseline = _library_files(root)
    pointer = _native_call("libgpod could not open the stock database", library.itdb_parse,
                           os.fsencode(root))
    database = StockDatabase(root, pointer, checksum, writable, compressed, baseline)
    try:
        yield database
    finally:
        database._pointer = None
        library.itdb_free(pointer)
