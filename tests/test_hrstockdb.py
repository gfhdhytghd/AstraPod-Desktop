"""Exercise the native writer against disposable stock iPod fixture trees."""

import ctypes
import hmac
import os
from pathlib import Path
import plistlib
import sqlite3
import struct
import sys
import zlib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "herdremote"))

import hrstockdb as stock


GUID = "1234567890abcdef"
# Key for GUID above checked against MilanMarocchi/podbox's independent
# hash58Key implementation (5cff077, src/itdb/hash58.cpp).
HASH58_KEY = bytes.fromhex("f44fb0d8855ee673311625ea46ee547fba2933b3").ljust(64, b"\0")
ITUNES = Path("iPod_Control/iTunes")
DEVICE = Path("iPod_Control/Device")
SQLITE = ITUNES / "iTunes Library.itlp"


@pytest.fixture(autouse=True)
def libgpod():
    try:
        return stock._library()
    except stock.StockDatabaseUnavailable as exc:
        pytest.skip(str(exc))


def create_fixture(root, model="MA002", checksum=0, compressed=False):
    directory = root / DEVICE
    directory.mkdir(parents=True)
    (directory / "SysInfo").write_text(f"ModelNumStr: {model}\nFirewireGuid: {GUID}\n")
    if checksum == 2 or compressed:
        (directory / "SysInfoExtended").write_bytes(plistlib.dumps({
            "DBVersion": {0: 0, 1: 3, 2: 4}[checksum],
            "SQLiteDB": compressed,
            "FireWireGUID": GUID,
        }))
        # Fixture-only IV and random bytes used to exercise the hash72 writer.
        (directory / "HashInfo").write_bytes(
            b"HASHv0" + bytes.fromhex(GUID).ljust(20, b"\0") + bytes(range(28))
        )
    initialize = stock._library().itdb_init_ipod
    initialize.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, stock._ErrorOut]
    initialize.restype = ctypes.c_int
    stock._native_call("could not initialize iPod fixture", initialize, os.fsencode(root),
                       model.encode(), b"Fixture iPod")


def example_row(n=1):
    return stock.DatabaseTrack(
        path=f":iPod_Control:Music:F07:AB{n:02}.mp3",
        title=f"新曲 {n}", artist="Björk", album="Album", album_artist="Album Artist",
        comment="Comment", composer="Composer", grouping="Group", genre="Genre",
        year=2003, disc=2, track=n, length_ms=1250, bitrate_kbps=192, size_bytes=30000,
        play_count=4, rating=60, last_played=1700000001, mtime=1700000000,
    )


def add_playlist(library, database, name, track_ids):
    library.itdb_playlist_new.argtypes = [ctypes.c_char_p, ctypes.c_int]
    library.itdb_playlist_new.restype = stock._PlaylistPointer
    library.itdb_playlist_add.argtypes = [stock._DbPointer, stock._PlaylistPointer, ctypes.c_int]
    library.itdb_playlist_add.restype = None
    playlist = library.itdb_playlist_new(name.encode("utf-8"), 0)
    library.itdb_playlist_add(database._get_pointer(), playlist, -1)
    tracks = {track.contents.dbid: track for track in database._native_tracks()}
    for track_id in track_ids:
        library.itdb_playlist_add_track(playlist, tracks[track_id], -1)


@pytest.mark.parametrize("model,checksum", [("MA002", 0), ("MA446", 0), ("MB029", 1),
                                             ("MB565", 1), ("MC297", 1), ("MC293", 2)])
def test_round_trip_with_track_and_playlist_removal(tmp_path, libgpod, model, checksum):
    create_fixture(tmp_path, model, checksum)
    with stock.open_database(tmp_path) as db:
        assert db.tracks == ()
        first = db.add_track(example_row(1), filetype="MPEG audio file", sample_rate=44100)
        second = db.add_track(example_row(2), filetype="MPEG audio file", sample_rate=48000)
        add_playlist(libgpod, db, "Test", (first, second, first))
        assert ITUNES / "iTunesDB" in db.save()
        expected = db.tracks, db.playlists
    with stock.open_database(tmp_path) as db:
        assert (db.tracks, db.playlists) == expected
        assert db.playlists[0].is_master
        assert db.playlists[0].track_ids == (first, second)
        assert db.playlists[1].track_ids == (first, second, first)
        assert db.tracks[1].path == example_row(2).path
        assert db.tracks[1].title == "新曲 2"
        assert db.tracks[1].play_count == 4
        db.remove_tracks([first, 0])
        db.save()
    with stock.open_database(tmp_path) as db:
        assert [track.id for track in db.tracks] == [second]
        assert [playlist.track_ids for playlist in db.playlists] == [(second,), (second,)]
    scheme, signature = stock._signature(tmp_path / ITUNES / "iTunesDB")
    assert scheme == checksum
    if checksum == 1:
        hashed = bytearray((tmp_path / ITUNES / "iTunesDB").read_bytes())
        hashed[0x18:0x20] = bytes(8)
        hashed[0x32:0x46] = bytes(20)
        hashed[0x58:0x6C] = bytes(20)
        assert hmac.digest(HASH58_KEY, hashed, "sha1") == signature
    elif checksum == 2:
        assert signature[:14] == b"\x01\x00" + bytes(range(12))


def test_compressed_database_and_sqlite_bundle(tmp_path):
    create_fixture(tmp_path, "MC297", checksum=2, compressed=True)
    hash_path = tmp_path / DEVICE / "HashInfo"
    hash_info = hash_path.read_bytes()
    hash_path.unlink()
    with stock.open_database(tmp_path) as db:
        track_id = db.add_track(example_row(), filetype="MPEG audio file", sample_rate=44100)
        changes = set(db.save())
    assert (tmp_path / ITUNES / "iTunesDB").read_bytes() == b""
    assert ITUNES / "iTunesCDB" in changes
    assert {SQLITE / name for name in stock._SQLITE_FILES} <= changes
    assert DEVICE / "HashInfo" in changes
    assert hash_path.read_bytes() == hash_info
    assert DEVICE / "SysInfo" not in changes
    assert DEVICE / "SysInfoExtended" not in changes
    for name in stock._SQLITE_FILES:
        path = tmp_path / SQLITE / name
        if path.suffix == ".itdb":
            with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as database:
                assert database.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    with stock.open_database(tmp_path) as db:
        assert db.tracks[0].id == track_id
        assert db.playlists[0].track_ids == (track_id,)


def test_classic_cannot_write_without_guid(tmp_path):
    create_fixture(tmp_path, "MC297", checksum=1)
    (tmp_path / DEVICE / "SysInfo").write_text("ModelNumStr: MC297\n")
    original = (tmp_path / ITUNES / "iTunesDB").read_bytes()
    with stock.open_database(tmp_path) as db:
        with pytest.raises(stock.StockDatabaseError, match="signing GUID"):
            db.save()
    assert (tmp_path / ITUNES / "iTunesDB").read_bytes() == original


def test_hash72_missing_hash_info_fails_without_overwriting_db(tmp_path):
    create_fixture(tmp_path, "MC293", checksum=2)
    (tmp_path / DEVICE / "HashInfo").unlink()
    original = (tmp_path / ITUNES / "iTunesDB").read_bytes()
    with stock.open_database(tmp_path) as db:
        with pytest.raises(stock.StockDatabaseError, match="missing HashInfo"):
            db.save()
    assert (tmp_path / ITUNES / "iTunesDB").read_bytes() == original


def test_bad_database_header_is_rejected_before_native_parse(tmp_path):
    (tmp_path / ITUNES).mkdir(parents=True)
    (tmp_path / ITUNES / "iTunesDB").write_bytes(b"mhbd" + bytes(20))
    with pytest.raises(stock.StockDatabaseError, match="header size"):
        with stock.open_database(tmp_path):
            pytest.fail("Opened a corrupted iTunesDB")


def test_writes_and_preserves_artwork(tmp_path):
    def png_chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload)))

    create_fixture(tmp_path)
    artwork = tmp_path / "cover.png"
    artwork.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(b"\0\xff\0\x80"))
        + png_chunk(b"IEND", b"")
    )
    with stock.open_database(tmp_path) as db:
        db.add_track(example_row(), filetype="MPEG audio file", sample_rate=44100, artwork=artwork)
        emitted = db.save()
    assert Path("iPod_Control/Artwork/ArtworkDB") in emitted
    assert any(path.suffix == ".ithmb" for path in emitted)
    with stock.open_database(tmp_path) as db:
        assert db.tracks[0].has_artwork
        db.add_track(example_row(2), filetype="MPEG audio file", sample_rate=44100)
        db.save()
    with stock.open_database(tmp_path) as db:
        assert [track.has_artwork for track in db.tracks] == [True, False]


def test_play_counts_is_consumed_once(tmp_path):
    create_fixture(tmp_path)
    with stock.open_database(tmp_path) as db:
        db.add_track(example_row(), filetype="MPEG audio file", sample_rate=44100)
        db.save()
    counts = ITUNES / "Play Counts"
    (tmp_path / counts).write_bytes(
        struct.pack("<4sIII", b"mhdp", 0x60, 12, 1) + bytes(0x60 - 16)
        + struct.pack("<III", 2, 0, 0)
    )
    with stock.open_database(tmp_path) as db:
        assert db.tracks[0].play_count == 6
        assert ITUNES / "Play Counts.bak" in db.save()
        assert db.removed_files == (counts,)
    with stock.open_database(tmp_path) as db:
        assert db.tracks[0].play_count == 6


def test_casefolded_staging_and_detected_metadata_overrides(tmp_path):
    create_fixture(tmp_path, "MB029", checksum=1)
    (tmp_path / DEVICE / "SysInfo").unlink()
    (tmp_path / ITUNES / "iTunesDB").rename(tmp_path / ITUNES / "ITUNESDB")
    with stock.open_database(tmp_path, model_number="MB029", firewire_guid=GUID, checksum=1) as db:
        db.add_track(example_row(), filetype="MPEG audio file", sample_rate=44100)
        assert ITUNES / "iTunesDB" in db.save()
    assert stock._signature(tmp_path / ITUNES / "iTunesDB")[0] == 1
