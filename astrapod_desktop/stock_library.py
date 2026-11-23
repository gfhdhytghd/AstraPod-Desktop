"""Adapt a mounted Apple library to the desktop library and artwork views."""
from contextlib import ExitStack
from dataclasses import asdict
from hashlib import sha256
import json

import hrstock
import hrstockdevice
from hrstockdb import DatabasePlaylist
from podlink_settings import write_private

from . import contracts
from .artwork import SIZES


def playlist_path(directory, serial, commit_id):
    key = sha256(json.dumps((serial, commit_id)).encode()).hexdigest()
    return directory / "stock-playlists" / f"{key}.json"


def cached_playlists(directory, serial, commit_id):
    try:
        rows = json.loads(playlist_path(directory, serial, commit_id).read_text("utf-8"))
        return tuple(DatabasePlaylist(row["id"], row["name"], tuple(row["track_ids"]),
                                      row["is_master"]) for row in rows)
    except (OSError, ValueError, TypeError, KeyError):
        return None


class StockLibrarySession:
    def __init__(self, root, identity, stack: ExitStack):
        self.root = root
        self.identity = identity
        self.stack = stack
        self._artwork = None
        self._artwork_open = False

    def snapshot(self, library, *, full):
        info = hrstockdevice.detect(self.root, authorize=full)
        if info is None:
            raise RuntimeError("原厂 iPod 上没有可读取的 Apple 音乐资料库。")
        commit_id = hrstock.library_identity(self.root)
        tracks = None if full else library.cache.load(self.identity, commit_id)
        playlists = cached_playlists(library.cache_dir, self.identity, commit_id)
        if tracks is None or playlists is None:
            snapshot = hrstock.read_library(self.root)
            if hrstock.library_identity(self.root) != commit_id:
                raise RuntimeError("iPod 资料库在读取期间发生变化，请重新读取。")
            tracks, playlists = list(snapshot.tracks), snapshot.playlists
            library.cache.store(self.identity, commit_id, tracks)
            write_private(playlist_path(library.cache_dir, self.identity, commit_id),
                          json.dumps([asdict(playlist) for playlist in playlists]))
        summary = contracts.DeviceSummary(
            "ipod6g", info.firmware_version or "", info.capacity_bytes, info.free_bytes,
            len(tracks), commit_id, True)
        return summary, tracks, playlists, info

    def artwork(self, track, size="coverflow"):
        if not self._artwork_open:
            self._artwork = self.stack.enter_context(hrstock.artwork_library(self.root))
            self._artwork_open = True
        if self._artwork is not None:
            thumbnail = self._artwork.track_artwork(
                track.index, contracts.album_key(track), SIZES[size])
            if thumbnail is not None:
                return contracts.Artwork(*thumbnail)
        return None

    def detail(self, track):
        path = hrstockdevice.checked_path(self.root, *track.path.removeprefix("/").split("/"),
                                          required=True)
        return contracts.TrackDetail(path.stat().st_size, path.suffix.removeprefix("."), 0, 0,
                                     self.artwork(track, "list") is not None)
