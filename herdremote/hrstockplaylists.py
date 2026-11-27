"""Read AstraPod's saved playlist catalogue and plan changes to the stock library."""

from dataclasses import dataclass
from hashlib import sha256
from itertools import count
import json
import os
from pathlib import PurePosixPath
import posixpath
import re
import stat

from hrstockdevice import checked_path


INDEX = ".astrapod-playlists.json"
_OTG_SAVE = re.compile(r"New Playlist [1-9][0-9]*", re.IGNORECASE | re.ASCII)
_LIVE_QUEUE = ".rockbox/on-the-go.m3u8"


@dataclass(frozen=True)
class PlaylistReport:
    changes: int
    unmatched: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PlaylistEdits:
    kept: dict[str, tuple[int, str]]
    updates: dict[str, tuple[int | None, str, tuple[int, ...]]]
    removals: tuple[int, ...]
    report: PlaylistReport

    def apply(self, database):
        index = self.kept.copy()
        ids = database.update_playlists(self.updates.values(), self.removals)
        for source, playlist_id in zip(self.updates, ids, strict=True):
            index[source] = (playlist_id, self.updates[source][1])
        return json.dumps(index, sort_keys=True)


def _read(path, filesystem):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if info.st_dev != filesystem or not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"Unsafe AstraPod playlist data: {path.name}")
        return stream.read()


def _catalogue(root, filesystem):
    config = checked_path(root, ".rockbox", "config.cfg")
    if config is None:
        config = checked_path(root, ".rockbox", "default-config.cfg")
    location = "/Playlists"
    if config is not None:
        for line in _read(config, filesystem).decode("utf-8-sig").splitlines():
            key, _, value = line.partition(":")
            if key.strip().casefold() == "playlist catalog directory":
                location = value.strip() or "/Playlists"
    path = PurePosixPath(location)
    if not path.is_absolute() or path.anchor != "/":
        raise RuntimeError(f"Invalid AstraPod playlist directory: {location!r}")
    return checked_path(root, *path.parts[1:], directory=True)


def snapshot(root, tunes):
    """Read source files and the ownership index from the pinned stock volume."""
    filesystem = root.stat().st_dev
    directory = _catalogue(root, filesystem)
    files = {}
    seen = set()
    pending = [directory] if directory is not None else []
    while pending:
        for path in pending.pop().iterdir():
            relative = path.relative_to(root).as_posix()
            key = relative.casefold()
            if key == _LIVE_QUEUE or key in ("ipod_control", ".rockbox"):
                continue
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                if info.st_dev != filesystem:
                    raise RuntimeError(f"AstraPod playlist directory crosses a filesystem: {relative}")
                pending.append(path)
            elif path.suffix.casefold() == ".m3u8":
                if key in seen:
                    raise RuntimeError(f"Ambiguous AstraPod playlist path: {relative}")
                seen.add(key)
                files[relative] = _read(path, filesystem)
            elif stat.S_ISLNK(info.st_mode):
                raise RuntimeError(f"Unsafe path in AstraPod playlist directory: {relative}")
    index = checked_path(tunes, INDEX)
    return Snapshot(tuple(sorted(files.items())), _read(index, filesystem) if index else b"")


def _owners(raw):
    if not raw:
        return {}
    try:
        index = json.loads(raw)
        if not isinstance(index, dict):
            raise ValueError
        owners = {}
        ids = set()
        for source, (playlist_id, name) in index.items():
            if (type(playlist_id) is not int or not 0 < playlist_id < 2**64
                    or playlist_id in ids or not isinstance(name, str) or not name):
                raise ValueError
            owners[source] = (playlist_id, name)
            ids.add(playlist_id)
        return owners
    except (TypeError, ValueError) as error:
        raise RuntimeError("Invalid AstraPod playlist ownership index") from error


def _entry_path(playlist, entry):
    # The firmware accepts both separator styles and resolves relative entries
    # against the saved playlist. Drive letters are ignored on the player.
    entry = re.sub(r"^[A-Za-z]:", "", entry.replace("\\", "/"))
    absolute = posixpath.join("/" + playlist.parent.as_posix(), entry)
    return "/" + posixpath.normpath(absolute).lstrip("/").casefold()


def _free_name(name, occupied, reserved):
    if name.casefold() not in occupied:
        return name
    if _OTG_SAVE.fullmatch(name):
        candidates = (f"New Playlist {number}" for number in count(1))
    else:
        candidates = (f"{name} ({number})" for number in count(2))
    return next(candidate for candidate in candidates
                if candidate.casefold() not in occupied and candidate.casefold() not in reserved)


@dataclass(frozen=True)
class Snapshot:
    files: tuple[tuple[str, bytes], ...]
    index: bytes

    @property
    def revision(self):
        digest = sha256(sha256(self.index).digest())
        for source, raw in self.files:
            digest.update(os.fsencode(source) + b"\0")
            digest.update(sha256(raw).digest())
        return digest.hexdigest()

    def plan(self, playlists, track_paths):
        by_id = {playlist.id: playlist for playlist in playlists if not playlist.is_master}
        owned = {source: by_id[playlist_id]
                 for source, (playlist_id, name) in _owners(self.index).items()
                 if playlist_id in by_id and by_id[playlist_id].name == name}
        sources = {source.casefold() for source, _ in self.files}
        removals = tuple(playlist.id for source, playlist in owned.items() if source not in sources)
        occupied = {playlist.name.casefold() for playlist in playlists if playlist.id not in removals}
        reserved = {PurePosixPath(source).stem.casefold() for source, _ in self.files}

        updates, kept, unmatched = {}, {}, []
        for source, raw in self.files:
            path = PurePosixPath(source)
            members = []
            for entry in raw.decode("utf-8-sig").splitlines():
                entry = entry.rstrip(" \t")
                if not entry or entry.startswith("#"):
                    continue
                normalized = _entry_path(path, entry)
                track_id = track_paths.get(normalized)
                if track_id is None:
                    unmatched.append(("/" + source, entry))
                else:
                    members.append(track_id)
            track_ids = tuple(members)
            key = source.casefold()
            playlist = owned.get(key)
            if playlist is not None:
                kept[key] = (playlist.id, playlist.name)
                if track_ids != playlist.track_ids:
                    updates[key] = (playlist.id, playlist.name, track_ids)
            else:
                name = _free_name(path.stem, occupied, reserved)
                updates[key] = (None, name, track_ids)
                occupied.add(name.casefold())
        return PlaylistEdits(kept, updates, removals,
                             PlaylistReport(len(updates) + len(removals), tuple(unmatched)))
