"""Stock iPod Classic operations on a caller-selected, mounted Linux volume.

detect(root) identifies an iPod Classic and raises UnsupportedStockModel for
unsupported stock iPods. For passive discovery use
detect(root, authorize=False). read_library(root) returns DeviceTrack rows and
playlists whose track_ids refer to row.index (the persistent Apple database
ID). Paths are absolute within the volume and ratings are 0–10. The managed
field marks Jellyfin-owned copies and play_time_ms is unavailable (zero).
random_artwork(root, previous=None) reads one random album cover from the
stock artwork database, excluding the previous album key.

preview(root, source, scope) returns an hrmediasync.Plan. inventory(root,
identity, tracks=()) returns its device, owned and matched fields for callers
that fetched the source rows. apply(root, source, plan) returns (added, removed)
and requires a stock preview. import_files(root, paths, progress=None,
skip_duplicates=True) uses the same event and result dictionaries as hrlibrary.

Call from a worker thread. The caller selects the mount and safely ejects it.
Writes can ask polkit to authorize a read-only identity query. recover(root)
finishes or rolls back an interrupted write. Reads report a pending transaction
and writes recover it before opening the database.
"""
from collections import defaultdict
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from secrets import choice
import shutil
import stat
from string import ascii_uppercase
from tempfile import TemporaryDirectory

import hrlibrary
import hrmediasync
import hrstockdb
import hrstockmedia
import hrstockphoto
import hrstockvideo
import hrstocktxn
from hrstockdevice import (MODEL_NUMBERS, UnsupportedStockModel, assert_stock_layout,
                           checked_path, detect)

_OWNERS = ".astrapod-jellyfin.json"
_METADATA = ("iTunes", "Device", "Artwork")
_STOCK_FILES = {name.casefold(): name for name in (
    "iTunesDB", "iTunesCDB", "iTunes Library.itlp", "SysInfo", "SysInfoExtended", "HashInfo",
    "ArtworkDB", "Play Counts", "iTunesStats", "PlayCounts.plist",
)}
_MUSIC_FOLDER = re.compile(r"F[0-9]{2}", re.IGNORECASE | re.ASCII)
_MUSIC_FILE = re.compile(r"[A-Z0-9]{4,8}\.(mp3|m4a|wav)", re.IGNORECASE | re.ASCII)


@dataclass(frozen=True)
class StockLibrary:
    tracks: tuple[hrlibrary.DeviceTrack, ...]
    playlists: tuple[hrstockdb.DatabasePlaylist, ...]


@dataclass(frozen=True)
class StockInventory:
    device: str
    owned: frozenset[str]
    matched: frozenset[str]


@dataclass(frozen=True)
class _Owner:
    id: int
    path: str
    size: int


def _require_device(root, *, authorize=False):
    device = detect(root, authorize=authorize)
    if device is None:
        raise RuntimeError("No stock iPod database on the selected volume")
    return device


@contextmanager
def _volume(volume_root, *, write=False, authorize=True):
    original = Path(volume_root).absolute()
    fd = os.open(original, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            mode = fcntl.LOCK_EX if write else fcntl.LOCK_SH
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Another stock iPod library operation is in progress") from error

        pinned = Path("/proc/self/fd") / str(fd)
        opened = os.fstat(fd)

        def check():
            current = original.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise RuntimeError("The selected iPod mount changed")
            assert_stock_layout(pinned)

        check()
        device = _require_device(original)
        check()
        tunes = pinned / device.tunes.relative_to(device.root)
        primary = pinned / device.database.relative_to(device.root)
        if hrstocktxn.pending(tunes):
            if not write:
                raise RuntimeError("The iPod has an interrupted AstraPod write. Recover the volume first.")
            hrstocktxn.recover(pinned, tunes, primary)
            device = _require_device(original)
        unknown_compressed_model_info = (
            device.database.name.casefold() == "itunescdb" and device.sysinfo_extended is None)
        if write and authorize and (not device.model_number or unknown_compressed_model_info
                                   or not device.firewire_guid):
            device = _require_device(original, authorize=True)
        check()
        yield device, pinned, tunes, check
    finally:
        os.close(fd)


def recover(root):
    """Recover a pending transaction under the volume lock."""
    with _volume(root, write=True, authorize=False):
        pass


def _metadata(root):
    result = {}
    device_id = root.stat().st_dev

    def walk(directory, relative):
        for item in directory.iterdir():
            name = item.name.casefold()
            if name.startswith(".astrapod") or name.endswith(".astrapod-bak"):
                continue
            info = item.lstat()
            if info.st_dev != device_id:
                raise RuntimeError(f"iPod metadata crosses a filesystem: {item.name}")
            key = relative / (_STOCK_FILES.get(name, item.name)
                              if len(relative.parts) == 2 else item.name)
            if key in result:
                raise RuntimeError(f"Ambiguous stock metadata path: {item.name}")
            if stat.S_ISDIR(info.st_mode):
                walk(item, key)
            elif stat.S_ISREG(info.st_mode):
                result[key] = (item, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            else:
                raise RuntimeError(f"Unsafe iPod metadata path: {item.name}")

    for name in _METADATA:
        directory = checked_path(root, "iPod_Control", name, directory=True)
        if directory is not None:
            walk(directory, Path("iPod_Control") / name)
    return result


def _destination(root, relative):
    parts = relative.parts
    music_metadata = len(parts) >= 3 and parts[0] == "iPod_Control" and parts[1] in _METADATA
    photo_metadata = relative == hrstockphoto.PHOTO_DB or (
        relative.parent == hrstockphoto.THUMBS and hrstockphoto.THUMB_NAME.fullmatch(relative.name))
    if not (music_metadata or photo_metadata) or ".." in parts:
        raise RuntimeError(f"Unexpected libgpod output path: {relative}")
    parent = root
    for depth in range(1, len(parts) + 1):
        existing = checked_path(root, *parts[:depth], directory=(depth < len(parts)))
        if existing is None:
            return parent.joinpath(*parts[depth - 1:])
        parent = existing
    return parent


def _row_path(path):
    parts = path.removeprefix(":").split(":")
    if not path or any(part in ("", ".", "..") or "/" in part or "\\" in part
                       or "\0" in part for part in parts):
        return ""
    return "/" + "/".join(parts)


def _music_parts(path):
    parts = path.split("/")[1:]
    if (path.startswith("/") and len(parts) == 4
            and tuple(part.casefold() for part in parts[:2]) == ("ipod_control", "music")
            and _MUSIC_FOLDER.fullmatch(parts[2]) and _MUSIC_FILE.fullmatch(parts[3])):
        return parts
    return None


def _read_owners(root, tunes):
    path = checked_path(root, *tunes.relative_to(root).parts, _OWNERS)
    if path is None:
        return None, {}
    raw = path.read_bytes()
    try:
        blob = json.loads(raw)
        if blob["version"] != 1 or not isinstance(blob["tracks"], dict):
            raise ValueError
        owners = {}
        pointers = set()
        for name, row in blob["tracks"].items():
            owner = _Owner(**row)
            if (not hrmediasync.SYNC_NAME.fullmatch(name)
                    or type(owner.id) is not int or not 0 < owner.id < 2**64
                    or type(owner.size) is not int or owner.size <= 0
                    or not isinstance(owner.path, str) or not _music_parts(owner.path)
                    or owner.id in pointers):
                raise ValueError
            pointers.add(owner.id)
            owners[name] = owner
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Invalid AstraPod Jellyfin ownership index") from error
    return raw, owners


def _tags(track):
    return hrmediasync.music_tags(track.title, track.artist.split(";"), track.album,
                                 track.length_ms, track.disc, track.track)


def _stage_sysinfo(device, stage):
    path = stage / "iPod_Control/Device/SysInfoExtended"
    if device.sysinfo_extended is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(device.sysinfo_extended)


def _open_database(device, stage, *, artwork_only=False):
    generation = device.generation
    fallback = "classic-6g" if generation == "classic" else generation
    model = device.model_number or MODEL_NUMBERS[fallback][0]
    return hrstockdb.open_database(
        stage, model_number=model, firewire_guid=device.firewire_guid,
        checksum=device.checksum, sqlite=device.sqlite, artwork_only=artwork_only)


def _ownership(root, owners, tracks):
    by_id = {track.id: track for track in tracks}
    result = {}
    for name, owner in owners.items():
        track = by_id.get(owner.id)
        if (track and _row_path(track.path).casefold() == owner.path.casefold()
                and track.size_bytes == owner.size):
            path = checked_path(root, *_music_parts(owner.path))
            # A missing file can be repaired by replacing its old DB record.
            # A differently sized file may belong to another library manager.
            if path is None or path.stat().st_size == owner.size:
                result[name] = (owner.id, path)
    return result



class _Session:
    def __init__(self, device, root, tunes, check, stage, volume_key=None):
        self.device = device
        self.root = root
        self.tunes = tunes
        self.check = check
        self.stage = stage
        self.files = _metadata(root)
        self.owners_raw, self.owners = _read_owners(root, tunes)
        self.additions = {}
        self.removals = []
        self.folders = None
        for relative, (source, *_) in self.files.items():
            dest = stage / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest, follow_symlinks=False)
        _stage_sysinfo(device, stage)
        self.primary = root / device.database.relative_to(device.root)
        self.validate_snapshot()
        fingerprint = sha256()
        for relative in sorted(self.files):
            if relative.parts[1] == "iTunes":
                with (stage / relative).open("rb") as stream:
                    fingerprint.update(os.fsencode(relative) + b"\0")
                    digest = sha256()
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                    fingerprint.update(digest.digest())
        fingerprint.update(self.owners_raw or b"")
        if volume_key is None:
            opened = root.stat()
            volume_key = (opened.st_dev, opened.st_ino)
        fingerprint.update(json.dumps(volume_key).encode())
        self.identity = "stock:" + fingerprint.hexdigest()

    def validate_snapshot(self):
        self.check()
        if self.files != _metadata(self.root) or self.owners_raw != _read_owners(
                self.root, self.tunes)[0]:
            raise RuntimeError("The iPod database changed during the operation")

    def open_database(self):
        return _open_database(self.device, self.stage)

    def ownership(self, tracks):
        return _ownership(self.root, self.owners, tracks)

    def inventory(self, identity, candidates, tracks):
        owned = self.ownership(tracks)
        managed = {track_id for track_id, _ in owned.values()}
        lookup = defaultdict(list)
        for candidate in candidates:
            if candidate.tags:
                lookup[candidate.tags.key].append(candidate)
        matched = set()
        if lookup:
            for track in tracks:
                if track.id in managed or track.media_type not in (0, 1):
                    continue
                tags = _tags(track)
                if not tags:
                    continue
                matches = [candidate.name for candidate in lookup.get(tags.key, ())
                           if candidate.tags.matches(tags)]
                path = _row_path(track.path)
                if len(matches) == 1 and path and checked_path(self.root, *path[1:].split("/")):
                    matched.add(matches[0])
        desired = {candidate.name for candidate in candidates}
        return StockInventory(self.identity,
                              frozenset(name for name, (_, path) in owned.items()
                                        if (path is not None or name not in desired)
                                        and hrmediasync.owned_name(name, identity)),
                              frozenset(matched))

    def add(self, database, media, *, name=None):
        control = checked_path(self.root, "iPod_Control", directory=True, required=True)
        music = checked_path(self.root, control.name, "Music", directory=True)
        if music is None:
            music = control / "Music"
        if self.folders is None:
            existing = [folder.name for folder in music.iterdir()
                        if _MUSIC_FOLDER.fullmatch(folder.name)] if music.exists() else []
            self.folders = existing or [f"F{index:02d}" for index in range(20)]
        folder = choice(self.folders)
        while True:
            filename = "".join(choice(ascii_uppercase) for _ in range(8)) + media.suffix
            path = music / folder / filename
            if path not in self.additions and checked_path(
                    self.root, *path.relative_to(self.root).parts) is None:
                break
        on_disk = "/" + path.relative_to(self.root).as_posix()
        record = hrstockdb.DatabaseTrack(
            path=on_disk.replace("/", ":"), title=media.title, artist=media.artist,
            album=media.album, album_artist=media.album_artist, genre=media.genre,
            composer=media.composer, grouping=media.grouping, comment=media.comment,
            year=media.year, disc=media.disc, track=media.track, length_ms=media.length_ms,
            bitrate_kbps=media.bitrate_kbps, size_bytes=media.size_bytes,
            media_type=media.media_type, show=media.show, episode=media.episode,
            season=media.season, episode_number=media.episode_number)
        track_id = database.add_track(record, filetype=media.filetype,
                                      sample_rate=media.sample_rate, artwork=media.artwork)
        self.additions[path] = media.upload
        if name is not None:
            self.owners[name] = _Owner(track_id, on_disk, media.size_bytes)
        return on_disk

    def commit(self, database, *, owner_update=False, progress=None, photos=()):
        replacements = {}
        if database is not None:
            changed = database.save()
            replacements = {_destination(self.root, path): self.stage / path for path in changed}
            replacements.update({_destination(self.root, path): None
                                 for path in database.removed_files})
        else:
            primary_stage = Path("iPod_Control/iTunes") / _STOCK_FILES[self.primary.name.casefold()]
            replacements[self.primary] = self.stage / primary_stage
        photo_files = None
        if photos:
            photo_files = hrstockphoto.snapshot(self.root)
            photo_stage = self.stage / "photo-stage"
            (photo_stage / hrstockphoto.THUMBS).mkdir(parents=True)
            for relative, (source, _) in photo_files.items():
                shutil.copyfile(source, photo_stage / relative, follow_symlinks=False)
            (photo_stage / "iPod_Control/Device").mkdir(parents=True)
            output = hrstockphoto.write(photo_stage, photos)
            photo_paths = photo_files.keys() | output.keys()
            for relative in sorted(photo_paths, key=lambda path:
                                   (path == hrstockphoto.PHOTO_DB, path)):
                target = _destination(self.root, relative)
                replacements[target] = output[relative][0] if relative in output else None
        if owner_update:
            index = self.stage / _OWNERS
            index.write_text(json.dumps({"version": 1, "tracks": {
                name: {"id": owner.id, "path": owner.path, "size": owner.size}
                for name, owner in self.owners.items()
            }}, sort_keys=True), encoding="utf-8")
            replacements[_destination(self.root, Path("iPod_Control/iTunes") / _OWNERS)] = index
        if replacements.get(self.primary) is None:
            raise RuntimeError("libgpod did not emit the active iPod database")
        def validate():
            self.validate_snapshot()
            if photo_files is not None and photo_files != hrstockphoto.snapshot(self.root):
                raise RuntimeError("The iPod Photo Database changed during the operation")

        validate()
        hrstocktxn.publish(self.root, self.tunes, self.primary, replacements=replacements,
                           additions=self.additions, removals=self.removals, progress=progress,
                           precommit=validate)


@contextmanager
def _session(root, *, write=False, volume_key=None):
    with (_volume(root, write=write) as (device, pinned, tunes, check),
          TemporaryDirectory(prefix="astrapod-stock-") as directory):
        session = _Session(device, pinned, tunes, check, Path(directory), volume_key)
        with session.open_database() as database:
            yield session, database


def _device_row(track, managed):
    return hrlibrary.DeviceTrack(
        index=track.id, path=_row_path(track.path), title=track.title, artist=track.artist,
        album=track.album, album_artist=track.album_artist, genre=track.genre,
        composer=track.composer, grouping=track.grouping, comment=track.comment,
        year=track.year, disc=track.disc, track=track.track, length_ms=track.length_ms,
        bitrate_kbps=track.bitrate_kbps, play_count=track.play_count,
        rating=min(10, max(0, round(track.rating / 10))), play_time_ms=0,
        last_played=track.last_played, mtime=track.mtime, managed=managed)


def _library_files(device, pinned, tunes):
    primary = pinned / device.database.relative_to(device.root)
    yield _STOCK_FILES[primary.name.casefold()], primary
    sidecars = ["Play Counts", "iTunesStats", "PlayCounts.plist", "OTGPlaylistInfo"]
    sidecars.extend(sorted(item.name for item in tunes.iterdir()
                           if re.fullmatch(r"OTGPlaylistInfo_\d+", item.name, re.I)))
    for name in sidecars:
        path = checked_path(tunes, name)
        if path is not None:
            yield name, path


def read_library(root):
    with (_volume(root) as (device, pinned, tunes, check),
          TemporaryDirectory(prefix="astrapod-stock-library-") as directory):
        stage = Path(directory)
        target = stage / "iPod_Control/iTunes"
        target.mkdir(parents=True)
        for name, path in _library_files(device, pinned, tunes):
            shutil.copyfile(path, target / name, follow_symlinks=False)
        _stage_sysinfo(device, stage)
        _, owners = _read_owners(pinned, tunes)
        with _open_database(device, stage) as database:
            tracks = database.tracks
            managed = {track_id for track_id, _ in _ownership(pinned, owners, tracks).values()}
            result = StockLibrary(tuple(_device_row(track, track.id in managed) for track in tracks),
                                  database.playlists)
        check()
        return result


def library_identity(root):
    """Content identity for cached stock tracks, playlists and artwork."""
    with _volume(root) as (device, pinned, tunes, check):
        digest = sha256()
        files = list(_library_files(device, pinned, tunes))
        files.extend((name, path) for name, path in (
            (_OWNERS, checked_path(tunes, _OWNERS)),
            ("ArtworkDB", checked_path(pinned, "iPod_Control", "Artwork", "ArtworkDB")),
        ) if path is not None)
        for name, path in files:
            value = sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    value.update(chunk)
            digest.update(os.fsencode(name) + b"\0" + value.digest())
        check()
        return digest.hexdigest()


@contextmanager
def artwork_library(root):
    """Open the native artwork index once for a batch of cover requests."""
    with (_volume(root) as (device, pinned, _tunes, check),
          TemporaryDirectory(prefix="astrapod-stock-art-") as directory,
          ExitStack() as thumbnails):
        artwork = checked_path(pinned, "iPod_Control", "Artwork", directory=True)
        index = checked_path(artwork, "ArtworkDB") if artwork else None
        if index is None:
            yield None
            return

        stage = Path(directory)
        stage_art = stage / "iPod_Control/Artwork"
        stage_art.mkdir(parents=True)
        stage_db = stage / "iPod_Control/iTunes" / _STOCK_FILES[device.database.name.casefold()]
        stage_db.parent.mkdir()
        shutil.copyfile(pinned / device.database.relative_to(device.root), stage_db)
        shutil.copyfile(index, stage_art / "ArtworkDB")
        _stage_sysinfo(device, stage)
        # ArtworkDB stores offsets into large .ithmb files. libgpod's thumbnail
        # reader seeks to those offsets, so stage only the DBs and pin the blobs
        # for the duration of this read. Writer sessions still own full copies.
        volume_id = pinned.stat().st_dev
        for path in artwork.iterdir():
            if path.suffix.casefold() != ".ithmb":
                continue
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            thumbnails.callback(os.close, fd)
            info = os.fstat(fd)
            if info.st_dev != volume_id or not stat.S_ISREG(info.st_mode):
                raise RuntimeError(f"Unsafe iPod thumbnail file: {path.name}")
            (stage_art / path.name).symlink_to(f"/proc/self/fd/{fd}")
        check()
        with _open_database(device, stage, artwork_only=True) as database:
            yield database
        check()


def random_artwork(root, previous=None):
    with artwork_library(root) as database:
        result = database.random_artwork(previous, size=256) if database else None
        if result is not None:
            album, thumbnail = result
            return album, hrlibrary.Artwork(*thumbnail)


def inventory(root, identity, tracks=(), *, volume_key=None):
    with _session(root, volume_key=volume_key) as (session, database):
        return session.inventory(identity, tracks, database.tracks)


def preview(root, source, scope, *, volume_key=None):
    """Use the caller-validated physical volume key across remounts, if supplied."""
    tracks = source.tracks(scope)
    current = inventory(root, source.identity, tracks, volume_key=volume_key)
    return hrmediasync.Plan(source.identity, scope, current.device, tracks,
                            current.owned, current.matched)


def apply(root, source, plan, *, volume_key=None):
    if not plan.device.startswith("stock:"):
        raise RuntimeError("Build a stock iPod preview before applying this plan")
    hrmediasync.validate_source(source, plan)
    with _session(root, write=True, volume_key=volume_key) as (session, database):
        tracks = database.tracks
        current = session.inventory(plan.identity, plan.tracks, tracks)
        if current != StockInventory(plan.device, plan.owned, plan.matched):
            raise RuntimeError("iPod changed after preview; build a new preview")
        additions = plan.additions
        if not additions and not plan.removals:
            return 0, 0

        with TemporaryDirectory(prefix="astrapod-stock-media-") as directory:
            staging = Path(directory)
            downloaded = {}
            for index, track in enumerate(additions):
                if not hrmediasync.owned_name(track.name, plan.identity):
                    raise ValueError("Invalid Jellyfin track ownership")
                folder = staging / str(index)
                folder.mkdir()
                downloaded[track.name] = hrstockmedia.prepare_track(source, track, folder)
            hrmediasync.validate_source(source, plan)
            if session.inventory(plan.identity, plan.tracks, tracks) != current:
                raise RuntimeError("iPod media changed during sync; build a new preview")
            owned = session.ownership(tracks)
            names = plan.removals | (owned.keys() & downloaded.keys())
            removal_ids = {owned[name][0] for name in names}
            references = {_row_path(track.path).casefold() for track in tracks
                          if track.id not in removal_ids}
            for name in sorted(names):
                _, path = owned[name]
                if path is not None and session.owners[name].path.casefold() not in references:
                    session.removals.append(path)
                del session.owners[name]
            database.remove_tracks(removal_ids)
            for name, media in downloaded.items():
                session.add(database, media, name=name)
            session.commit(database, owner_update=True)
    return len(additions), len(plan.removals)


def import_files(root, paths, progress=None, skip_duplicates=True, *, before_commit=None,
                 video_kind=None):
    def event(path, state, *, sent=0, total=0, error=None):
        if progress is not None:
            progress(dict(file=str(path), state=state, sent=sent, total=total, error=error))

    stored = []
    photos = []
    with (_session(root, write=True) as (session, database),
          TemporaryDirectory(prefix="astrapod-stock-import-") as directory):
        known = defaultdict(list)
        if skip_duplicates:
            for track in database.tracks:
                if track.media_type not in (0, 1):
                    continue
                tags = _tags(track)
                path = _row_path(track.path)
                if tags and path and checked_path(session.root, *path[1:].split("/")):
                    known[tags.key].append(tags)
        for index, value in enumerate(paths):
            path = Path(value)
            event(path, "probe")
            try:
                suffix = path.suffix.lower()
                folder = Path(directory) / str(index)
                folder.mkdir()
                if suffix in hrstockphoto.PHOTO_FORMATS:
                    image = folder / "photo.jpg"
                    hrstockphoto.normalize(path, image)
                    album = path.absolute().parent.name or "Imported Photos"
                    photos.append((path, image, album))
                    event(path, "upload", total=image.stat().st_size)
                    continue
                is_video = suffix in hrstockvideo.VIDEO_SUFFIXES
                tags = hrstockmedia.probe_music_tags(path) if skip_duplicates and not is_video else None
                if tags and any(tags.matches(other) for other in known[tags.key]):
                    event(path, "skip")
                    continue
                media = (hrstockvideo.prepare_video(path, folder, kind=video_kind) if is_video
                         else hrstockmedia.prepare_media(path, folder))
            except (OSError, RuntimeError, ValueError) as error:
                event(path, "error", error=str(error))
                continue
            event(path, "upload", total=media.size_bytes)
            device_path = session.add(database, media)
            stored.append((path, media, device_path))
            if tags:
                known[tags.key].append(tags)

        if stored or photos:
            if before_commit is not None:
                before_commit()
            uploads = {session.root / device_path[1:]: (path, media)
                       for path, media, device_path in stored}

            def report(target, sent):
                path, media = uploads[target]
                event(path, "upload", sent=sent, total=media.size_bytes)

            try:
                session.commit(database if stored else None,
                               progress=report if progress is not None else None,
                               photos=[(image, album) for _, image, album in photos])
            except (OSError, RuntimeError, ValueError) as error:
                for path, media, _ in stored:
                    event(path, "error", total=media.size_bytes, error=str(error))
                for path, image, _ in photos:
                    event(path, "error", total=image.stat().st_size, error=str(error))
                raise
        for path, image, _ in photos:
            size = image.stat().st_size
            event(path, "done", sent=size, total=size)
    for path, media, _ in stored:
        event(path, "done", sent=media.size_bytes, total=media.size_bytes)
    return [dict(source=str(path), device=device_path,
                 source_sample_rate_hz=media.source_rate, sample_rate_hz=media.sample_rate)
            for path, media, device_path in stored] + [
                dict(source=str(path), device="/Photos/Photo Database", album=album)
                for path, _, album in photos]
