from contextlib import contextmanager
import multiprocessing
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "herdremote"))
import hrstocktxn as txn


@contextmanager
def pinned(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield Path(f"/proc/self/fd/{fd}")
    finally:
        os.close(fd)


def write(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return path


@pytest.fixture
def library(tmp_path):
    with pinned(tmp_path) as root:
        tunes = root / "iPod_Control/iTunes"
        db = write(tunes / "iTunesDB", b"old db")
        old = write(root / "iPod_Control/Music/F00/OLD1.mp3", b"old music")
        new = root / "iPod_Control/Music/F01/NEW1.mp3"
        track = write(tmp_path / "track.mp3", b"new music")
        staged_db = write(tmp_path / "staged.db", b"new db")
        yield root, tunes, db, old, new, track, staged_db


def publish(library, extra=None, **kwargs):
    root, tunes, db, old, new, track, staged_db = library
    txn.publish(root, tunes, db, replacements={db: staged_db, **(extra or {})},
                additions={new: track}, removals=[old], **kwargs)


def test_pinned_publication_keeps_backup_and_replaces_related_files(library, tmp_path):
    root, tunes, db, old, new, _, _ = library
    art = root / "iPod_Control/Artwork/ArtworkDB"
    sqlite = tunes / "iTunes Library.itlp/Library.itdb"
    play_counts = write(tunes / "Play Counts", b"consumed")
    write(tunes / "iTunesDB.astrapod-bak", b"previous backup")

    publish(library, {
        art: write(tmp_path / "new artwork", b"art"),
        sqlite: write(tmp_path / "new sqlite", b"sqlite"),
        play_counts: None,
    })

    assert db.read_bytes() == b"new db"
    assert (tunes / "iTunesDB.astrapod-bak").read_bytes() == b"old db"
    assert new.read_bytes() == b"new music"
    assert art.read_bytes() == b"art"
    assert sqlite.read_bytes() == b"sqlite"
    assert not old.exists()
    assert not play_counts.exists()
    assert not txn.pending(tunes)


def test_compressed_publication_keeps_both_previous_databases(library, tmp_path):
    root, tunes, db, *_ = library
    cdb = write(tunes / "iTunesCDB", b"old cdb")
    placeholder = write(tmp_path / "placeholder", b"")
    rewrite = write(tmp_path / "compressed.db", b"new cdb")
    txn.publish(root, tunes, cdb, replacements={db: placeholder, cdb: rewrite},
                additions={}, removals=[])
    assert cdb.read_bytes() == b"new cdb"
    assert db.read_bytes() == b""
    assert (tunes / "iTunesDB.astrapod-bak").read_bytes() == b"old db"
    assert (tunes / "iTunesCDB.astrapod-bak").read_bytes() == b"old cdb"


def test_failure_after_primary_replace_restores_metadata_and_media(library, tmp_path, monkeypatch):
    _, tunes, db, old, new, *_ = library
    backup = write(tunes / "iTunesDB.astrapod-bak", b"previous backup")
    aux = write(tunes / "Location.itdb", b"old aux")
    deleted = write(tunes / "Play Counts", b"old play counts")
    replace = txn._replace

    def fail_after_replace(source, target, **kwargs):
        replace(source, target, **kwargs)
        if target == db and source.name.startswith("new-"):
            assert new.read_bytes() == b"new music"
            assert old.read_bytes() == b"old music"
            assert aux.read_bytes() == b"new aux"
            raise OSError("simulated fsync failure")

    monkeypatch.setattr(txn, "_replace", fail_after_replace)
    with pytest.raises(OSError, match="simulated fsync failure"):
        publish(library, {aux: write(tmp_path / "aux", b"new aux"), deleted: None})
    assert db.read_bytes() == b"old db"
    assert backup.read_bytes() == b"previous backup"
    assert old.read_bytes() == b"old music"
    assert aux.read_bytes() == b"old aux"
    assert deleted.read_bytes() == b"old play counts"
    assert not new.exists()
    assert not txn.pending(tunes)


@pytest.mark.parametrize("committed", [False, True])
def test_restart_recovers_after_process_exit(library, monkeypatch, committed):
    root, tunes, db, old, new, *_ = library
    replace = txn._replace

    def child():
        def stop(source, target, **kwargs):
            replace(source, target, **kwargs)
            if ((committed and target.name == txn._MARKER)
                    or (not committed and target == db)):
                os._exit(7)

        monkeypatch.setattr(txn, "_replace", stop)
        publish(library)

    process = multiprocessing.get_context("fork").Process(target=child)
    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.kill()
        process.join()
    assert process.exitcode == 7
    assert txn.pending(tunes)
    assert old.read_bytes() == b"old music"
    assert new.read_bytes() == b"new music"
    txn.recover(root, tunes, db)
    assert not txn.pending(tunes)
    assert db.read_bytes() == (b"new db" if committed else b"old db")
    assert new.exists() == committed
    assert old.exists() != committed


def test_collision_after_prepare_preserves_other_media(library, monkeypatch):
    _, tunes, db, old, new, *_ = library
    rename = txn._rename_new

    def collision(source, target):
        if target == new:
            target.write_bytes(b"new music")
        rename(source, target)

    monkeypatch.setattr(txn, "_rename_new", collision)
    with pytest.raises(FileExistsError):
        publish(library)
    assert new.read_bytes() == b"new music"
    assert old.exists()
    assert db.read_bytes() == b"old db"
    assert not txn.pending(tunes)


def test_cleanup_recovery_does_not_remove_replaced_media(library, monkeypatch):
    root, tunes, db, old, new, *_ = library
    unlink = txn._unlink

    def failure(path):
        if path == old:
            raise OSError("simulated disconnect")
        unlink(path)

    with monkeypatch.context() as patch:
        patch.setattr(txn, "_unlink", failure)
        with pytest.raises(txn.CleanupPendingError, match="publication committed"):
            publish(library)
    assert txn.pending(tunes)
    assert db.read_bytes() == b"new db"
    assert old.read_bytes() == b"old music"
    assert new.read_bytes() == b"new music"
    old.write_bytes(b"other audio")
    txn.recover(root, tunes, db)
    assert old.read_bytes() == b"other audio"
    assert not txn.pending(tunes)


def test_rejects_symlink_and_unmanaged_music(library, tmp_path):
    root, tunes, db, *_ = library
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "iPod_Control/Artwork").symlink_to(outside, target_is_directory=True)
    staged = write(tmp_path / "staged", b"new data")
    with pytest.raises(ValueError, match="Unsafe"):
        publish(library, {root / "iPod_Control/Artwork/ArtworkDB": staged})
    with pytest.raises(ValueError, match="cannot change"):
        publish(library, {root / "Music/PodLink/file.mp3": staged})
    assert list(outside.iterdir()) == []
    assert db.read_bytes() == b"old db"
    assert not txn.pending(tunes)
    (root / ".Rockbox").mkdir()
    with pytest.raises(RuntimeError, match="Rockbox"):
        publish(library)
    (root / ".Rockbox").rmdir()
    (root / "Music/PodLink").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="PodLink"):
        publish(library)


def test_publish_reports_copy_progress(library):
    _, _, _, _, new, track, _ = library
    track.write_bytes(b"x" * (2 * txn._CHUNK + 1))
    events = []
    publish(library, progress=lambda path, sent: events.append((path, sent)))
    assert events == [(new, txn._CHUNK), (new, 2 * txn._CHUNK), (new, 2 * txn._CHUNK + 1)]
    assert new.stat().st_size == 2 * txn._CHUNK + 1


def test_recovery_resumes_after_old_database_was_restored(library, monkeypatch):
    root, tunes, db, old, new, *_ = library
    replace = txn._replace

    def fail_a_write_and_its_rollback(source, target, **kwargs):
        replace(source, target, **kwargs)
        if target == db:
            raise OSError("simulated fsync failure")

    with monkeypatch.context() as patch:
        patch.setattr(txn, "_replace", fail_a_write_and_its_rollback)
        with pytest.raises(txn.RecoveryPendingError, match="Rollback is pending"):
            publish(library)
    assert txn.pending(tunes)
    assert db.read_bytes() == b"old db"
    assert old.exists()
    assert new.exists()
    txn.recover(root, tunes, db)
    assert db.read_bytes() == b"old db"
    assert old.exists()
    assert not new.exists()
    assert not txn.pending(tunes)


def test_recovery_uses_journal_database_when_primary_selection_changed(library, tmp_path,
                                                               monkeypatch):
    root, tunes, db, old, new, *_ = library
    cdb = tunes / "iTunesCDB"
    compressed = write(tmp_path / "new compressed db", b"new compressed db")
    replace = txn._replace

    def child():
        def stop(source, target, **kwargs):
            replace(source, target, **kwargs)
            if target == cdb:
                os._exit(7)

        monkeypatch.setattr(txn, "_replace", stop)
        publish(library, {cdb: compressed})

    process = multiprocessing.get_context("fork").Process(target=child)
    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.kill()
        process.join()
    assert process.exitcode == 7
    assert cdb.read_bytes() == b"new compressed db"
    txn.recover(root, tunes, cdb)
    assert db.read_bytes() == b"old db"
    assert old.exists()
    assert not cdb.exists()
    assert not new.exists()
    assert not txn.pending(tunes)
