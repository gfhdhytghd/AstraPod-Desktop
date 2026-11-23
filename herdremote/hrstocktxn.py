"""Durable publication of an already staged stock iPod library on Linux.

The caller pins the mounted root with an open directory descriptor and holds
its exclusive flock through publication or recovery. The journal lives on the
same volume as the files it protects. Recovery rolls back an unfinished
publication or finishes removing media after a committed publication.
"""

import ctypes
from dataclasses import dataclass
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from hrstockdevice import assert_stock_layout


_JOURNAL = ".astrapod-transaction"
_GARBAGE = ".astrapod-transaction-cleanup"
_PLAN = "plan.json"
_MARKER = "committed"
_SLOT = re.compile(r"(?:old|new|audio)-[0-9]+\Z")
_F_DIRECTORY = re.compile(r"f[0-9]{2}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CHUNK = 1024 * 1024


class RecoveryPendingError(RuntimeError):
    """Publication failed and rollback must resume when the disk is available."""


class CleanupPendingError(RuntimeError):
    """Publication committed and obsolete media cleanup must resume later."""


@dataclass(frozen=True)
class _Change:
    path: Path
    old_digest: str | None
    has_replacement: bool


@dataclass(frozen=True)
class _Media:
    path: Path
    digest: str


@dataclass(frozen=True)
class _Plan:
    changes: tuple[_Change, ...]
    additions: tuple[_Media, ...]
    removals: tuple[_Media, ...]


def _safe_parts(root, path):
    parts = path.relative_to(root).parts
    if not parts or any(part == ".." or part.endswith((".", " "))
                        or any(character in '<>:"\\|?*' or ord(character) < 32
                               for character in part) for part in parts):
        raise ValueError(f"Invalid stock iPod path: {path}")
    return tuple(part.casefold() for part in parts)


def _checked_stat(path, device, expected):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if info.st_dev != device or not expected(info.st_mode):
        raise ValueError(f"Unsafe stock iPod path: {path}")
    return info


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class _Volume:
    def __init__(self, root, tunes, primary):
        self.root, self.tunes = Path(root), Path(tunes)
        self.device = self.root.stat().st_dev
        self.journal = self.tunes / _JOURNAL
        self.garbage = self.tunes / _GARBAGE
        if _safe_parts(self.root, self.tunes) != ("ipod_control", "itunes"):
            raise ValueError("Invalid stock iPod iTunes directory")
        assert_stock_layout(self.root)
        self.check(self.tunes, directory=True, required=True)
        self.primary = Path(primary)
        self.check_primary(self.primary)

    def check(self, path, *, directory=False, required=False, create=False):
        parts = path.relative_to(self.root).parts
        _safe_parts(self.root, path)
        parent = self.root
        for part in parts[:-1]:
            child = parent / part
            if _checked_stat(child, self.device, stat.S_ISDIR) is None and create:
                child.mkdir()
                _fsync_directory(parent)
            parent = child
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        info = _checked_stat(path, self.device, expected)
        if required and info is None:
            raise FileNotFoundError(path)
        return info

    def check_primary(self, path):
        if path.parent != self.tunes or path.name.casefold() not in ("itunesdb", "itunescdb"):
            raise ValueError(f"Invalid stock iPod database: {path}")
        self.check(path)

    def check_scope(self, path, *, media=False, backup=False):
        parts = _safe_parts(self.root, path)
        if media:
            valid = (len(parts) == 4 and parts[:2] == ("ipod_control", "music")
                     and _F_DIRECTORY.fullmatch(parts[2]))
        else:
            valid = (len(parts) >= 3 and parts[0] == "ipod_control"
                     and parts[1] in ("itunes", "artwork", "device")
                     and (parts[1] != "itunes" or parts[2] not in (_JOURNAL, _GARBAGE)))
            if parts[-1].endswith(".astrapod-bak"):
                valid = valid and backup and len(parts) == 3 and parts[1] == "itunes"
                valid = valid and parts[-1] in ("itunesdb.astrapod-bak", "itunescdb.astrapod-bak")
        if not valid:
            raise ValueError(f"Stock iPod transaction cannot change {path}")
        return parts

    def backups(self):
        # The compressed layout also has a traditional iTunesDB placeholder.
        filenames = {}
        for path in self.tunes.iterdir():
            name = path.name.casefold()
            if name in ("itunesdb", "itunescdb", "itunesdb.astrapod-bak", "itunescdb.astrapod-bak"):
                if name in filenames:
                    raise ValueError(f"Ambiguous stock iPod database name: {name}")
                filenames[name] = path
        names = ("iTunesDB", "iTunesCDB") if self.primary.name.casefold() == "itunescdb" else ("iTunesDB",)
        backups = {}
        for name in names:
            source = filenames.get(name.casefold())
            if source is not None:
                self.check(source, required=True)
                backup_name = name + ".astrapod-bak"
                target = filenames.get(backup_name.casefold(), self.tunes / backup_name)
                backups[target] = source
        return backups


def _hash(path):
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f"Stock iPod file is not regular: {path}")
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _copy(source, destination, *, digest=False, report=None):
    """Copy to a new file and make its contents durable before any rename."""
    fingerprint = hashlib.sha256() if digest else None
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(source_fd, "rb") as incoming, destination.open("xb") as outgoing:
        info = os.fstat(incoming.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"Stock iPod source is not a regular file: {source}")
        while chunk := incoming.read(_CHUNK):
            outgoing.write(chunk)
            if fingerprint is not None:
                fingerprint.update(chunk)
            if report is not None:
                report(incoming.tell())
        if (incoming.tell() != info.st_size or
                os.fstat(incoming.fileno()).st_mtime_ns != info.st_mtime_ns):
            raise OSError(f"Stock iPod source changed during copy: {source}")
        outgoing.flush()
        os.fsync(outgoing.fileno())
    return fingerprint.hexdigest() if fingerprint is not None else None


def _rename_new(source, destination):
    # Linux vfat supports RENAME_NOREPLACE, including case-folded collisions.
    renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
                          ctypes.c_uint)
    if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _unlink(path):
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def _replace(source, target, *, new=False):
    if new:
        _rename_new(source, target)
    else:
        os.replace(source, target)
    _fsync_directory(target.parent)
    if source.parent != target.parent:
        _fsync_directory(source.parent)


def _write_plan(volume, plan):
    def relative(path):
        return str(path.relative_to(volume.root))

    document = {
        "version": 1,
        "changes": [[relative(item.path), item.old_digest, item.has_replacement] for item in plan.changes],
        "additions": [[relative(item.path), item.digest] for item in plan.additions],
        "removals": [[relative(item.path), item.digest] for item in plan.removals],
    }
    temporary = volume.journal / "plan.tmp"
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(document, stream, ensure_ascii=True)
        stream.flush()
        os.fsync(stream.fileno())
    # This fsync also persists the directory entries for all staged payloads.
    _replace(temporary, volume.journal / _PLAN)


def _load_plan(volume):
    path = volume.journal / _PLAN
    volume.check(path, required=True)
    with path.open(encoding="utf-8") as stream:
        document = json.load(stream)

    seen = set()

    def checked_target(relative, **scope):
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise ValueError("Invalid stock iPod journal path")
        target = volume.root / relative
        parts = volume.check_scope(target, **scope)
        if parts in seen:
            raise ValueError("Duplicate target in stock iPod journal")
        seen.add(parts)
        volume.check(target)
        return target

    def media(rows):
        for relative, digest in rows:
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                raise ValueError("Invalid stock iPod journal digest")
            yield _Media(checked_target(relative, media=True), digest)

    if not isinstance(document, dict) or type(document.get("version")) is not int or document["version"] != 1:
        raise ValueError("Unsupported stock iPod journal version")
    for field, count in (("changes", 3), ("additions", 2), ("removals", 2)):
        rows = document.get(field)
        if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != count
                                            for row in rows):
            raise ValueError(f"Invalid stock iPod journal {field}")
    changes = []
    for relative, old, new in document["changes"]:
        if (type(new) is not bool or
                old is not None and (not isinstance(old, str) or not _DIGEST.fullmatch(old))):
            raise ValueError("Invalid stock iPod journal file state")
        changes.append(_Change(checked_target(relative, backup=True), old, new))
    if not changes or not (changes[-1].old_digest and changes[-1].has_replacement):
        raise ValueError("Stock iPod journal has no database replacement")
    volume.check_primary(changes[-1].path)
    return _Plan(tuple(changes), tuple(media(document["additions"])),
                 tuple(media(document["removals"])))


def _remove_matching(volume, item):
    if volume.check(item.path) is not None and _hash(item.path) == item.digest:
        _unlink(item.path)


def _rollback(volume, plan):
    # Rename backups in place so rollback can run even on a full disk. The
    # original digest lets recovery recognize a previously restored file.
    for index, change in enumerate(plan.changes):
        current = volume.check(change.path)
        if change.old_digest is not None:
            if current is not None and _hash(change.path) == change.old_digest:
                continue
            backup = volume.journal / f"old-{index}"
            volume.check(backup, required=True)
            if _hash(backup) != change.old_digest:
                raise OSError(f"Stock iPod journal backup is damaged: {change.path}")
            _replace(backup, change.path)
        elif change.has_replacement and volume.check(volume.journal / f"new-{index}") is None:
            _unlink(change.path)
    for index, item in enumerate(plan.additions):
        # If a no-replace rename failed, the staged file still exists. Leave
        # the colliding destination alone, even if its bytes match the input.
        if volume.check(volume.journal / f"audio-{index}") is None:
            _remove_matching(volume, item)


def _delete_garbage(volume):
    if volume.check(volume.garbage, directory=True) is None:
        return
    allowed = {_PLAN, _MARKER, "plan.tmp", "commit.tmp"}
    for path in volume.garbage.iterdir():
        name = path.name.casefold()
        if name not in allowed and not _SLOT.fullmatch(name):
            raise ValueError(f"Unexpected file in stock iPod journal: {path}")
        volume.check(path, required=True)
        path.unlink()
    volume.garbage.rmdir()
    _fsync_directory(volume.tunes)


def _retire_journal(volume):
    # Keep the journal intact while rollback is still possible. Its atomic
    # rename marks the point when its backups are safe to garbage-collect.
    _replace(volume.journal, volume.garbage, new=True)
    _delete_garbage(volume)


def pending(tunes):
    """Return whether a reader must wait for exclusive transaction recovery."""
    return any(os.path.lexists(Path(tunes) / name) for name in (_JOURNAL, _GARBAGE))


def recover(root, tunes, primary):
    """Roll back an interrupted write or finish cleanup after a commit."""
    volume = _Volume(root, tunes, primary)
    _delete_garbage(volume)
    if volume.check(volume.journal, directory=True) is None:
        return
    committed = volume.check(volume.journal / _MARKER) is not None
    if volume.check(volume.journal / _PLAN) is not None:
        plan = _load_plan(volume)
        if committed:
            for item in plan.removals:
                _remove_matching(volume, item)
        else:
            _rollback(volume, plan)
    elif committed:
        raise ValueError("Committed stock iPod journal is missing its plan")
    _retire_journal(volume)


def _prepare(volume, replacements, additions, removals, progress):
    seen = set()

    def unique(path, **scope):
        parts = volume.check_scope(path, **scope)
        if parts in seen:
            raise ValueError(f"Duplicate target in stock iPod transaction: {path}")
        seen.add(parts)
        return volume.check(path)

    changes = []
    for index, (target, source) in enumerate(replacements.items()):
        original = None
        if unique(target, backup=True) is not None:
            original = _copy(target, volume.journal / f"old-{index}", digest=True)
        if source is not None:
            _copy(source, volume.journal / f"new-{index}")
        changes.append(_Change(target, original, source is not None))

    added = []
    for index, (target, source) in enumerate(additions.items()):
        if unique(target, media=True) is not None:
            raise FileExistsError(target)
        report = partial(progress, target) if progress is not None else None
        digest = _copy(source, volume.journal / f"audio-{index}", digest=True, report=report)
        added.append(_Media(target, digest))

    removed = []
    for target in removals:
        if unique(target, media=True) is not None:
            removed.append(_Media(target, _hash(target)))
    return _Plan(tuple(changes), tuple(added), tuple(removed))


def _publish(volume, plan):
    for index, item in enumerate(plan.additions):
        volume.check(item.path, create=True)
        _replace(volume.journal / f"audio-{index}", item.path, new=True)
    for index, change in enumerate(plan.changes):
        volume.check(change.path, create=change.has_replacement)
        if change.has_replacement:
            _replace(volume.journal / f"new-{index}", change.path, new=change.old_digest is None)
        elif change.old_digest is not None:
            _unlink(change.path)

    temporary = volume.journal / "commit.tmp"
    with temporary.open("xb") as stream:
        os.fsync(stream.fileno())
    _replace(temporary, volume.journal / _MARKER)


def publish(root, tunes, primary, *, replacements, additions, removals, progress=None,
            precommit=None):
    """Publish staged files and retain the previous database as .astrapod-bak.

    Destinations must be absolute paths below the pinned root. A value of None
    deletes an auxiliary metadata file. The authoritative database must have
    a replacement in every transaction. Removed audio is deleted after commit.
    precommit, if supplied, validates the live snapshot after staging finishes.
    If supplied, progress(target, bytes_copied) runs while staging audio.
    """
    volume = _Volume(root, tunes, primary)
    recover(root, tunes, primary)
    replacements = {Path(path): source for path, source in replacements.items()}
    additions = {Path(path): source for path, source in additions.items()}
    removals = tuple(Path(path) for path in removals)
    for path in replacements:
        volume.check_scope(path)
    if replacements.get(volume.primary) is None:
        raise ValueError("The stock iPod transaction must replace the database")
    volume.check(volume.primary, required=True)
    primary_source = replacements.pop(volume.primary)
    replacements = {**volume.backups(), **replacements, volume.primary: primary_source}
    volume.journal.mkdir()
    try:
        _fsync_directory(volume.tunes)
        plan = _prepare(volume, replacements, additions, removals, progress)
        if precommit is not None:
            precommit()
        _write_plan(volume, plan)
        _publish(volume, plan)
    except BaseException as error:
        try:
            # A failed fsync may leave a visible but undurable commit marker.
            # No old media has been deleted yet, so rollback remains possible.
            _unlink(volume.journal / _MARKER)
            recover(root, tunes, primary)
        except (OSError, RuntimeError, ValueError) as recovery:
            raise RecoveryPendingError(
                f"Stock iPod publication failed ({error}). Rollback is pending ({recovery})."
            ) from error
        raise
    try:
        recover(root, tunes, primary)
    except (OSError, RuntimeError, ValueError) as cleanup:
        raise CleanupPendingError(
            f"Stock iPod publication committed. Cleanup is pending ({cleanup})."
        ) from cleanup
