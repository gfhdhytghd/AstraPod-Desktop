"""Integration coverage using temporary stock databases and generated media."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "herdremote"))

import hrmediasync
import hrstock
from test_hrstockdb import ITUNES, create_fixture, libgpod


@pytest.fixture
def audio(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required")
    source = tmp_path / "Song.flac"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=660:sample_rate=96000:duration=0.25",
                    "-metadata", "title=Song", "-metadata", "artist=Artist",
                    "-metadata", "album=Album", "-c:a", "flac", str(source)],
                   check=True)
    return source


@pytest.mark.parametrize("model,checksum,compressed", [
    ("MA002", 0, False), ("MC297", 1, False), ("MC293", 2, True),
])
def test_import_adds_track_and_keeps_previous_database(tmp_path, audio, model, checksum, compressed):
    root = tmp_path / "volume"
    create_fixture(root, model, checksum, compressed)
    db = root / ITUNES / ("iTunesCDB" if compressed else "iTunesDB")
    original = db.read_bytes()
    events = []
    records = hrstock.import_files(root, [audio, audio], events.append)
    assert len(records) == 1
    assert records[0]["source_sample_rate_hz"] == 96000
    assert records[0]["sample_rate_hz"] == 48000
    assert db.with_name(db.name + ".astrapod-bak").read_bytes() == original
    if compressed:
        assert (root / ITUNES / "iTunesDB.astrapod-bak").read_bytes() == b""
    library = hrstock.read_library(root)
    assert len(library.tracks) == 1
    track, = library.tracks
    assert track.title == "Song"
    assert library.playlists[0].track_ids == (track.index,)
    assert track.path == records[0]["device"]
    assert (root / track.path[1:]).suffix == ".m4a"
    assert (root / track.path[1:]).stat().st_size > 0
    assert [item["state"] for item in events if item["state"] != "upload"] == [
        "probe", "probe", "skip", "done"]
    assert hrstock.import_files(root, [audio]) == []


class Source:
    def __init__(self, identity, audio, *, title="Song"):
        self.identity = identity * 24
        self.audio = audio
        self.downloads = []
        self.desired = (hrmediasync.Track(
            identity, title, ".flac", self.identity + "f" * 40 + ".flac",
            hrmediasync.music_tags(title, ["Artist"], "Album", 250, 0, 0)),)

    def tracks(self, scope):
        return self.desired

    def download(self, track, path):
        self.downloads.append(track)
        shutil.copyfile(self.audio, path)


def test_jellyfin_reconciliation_keeps_other_accounts_and_originals(tmp_path, audio):
    root = tmp_path / "volume"
    create_fixture(root, "MA446")
    scope = hrmediasync.Scope("all", "fixture")
    alice = Source("a", audio, title="Different title")
    bob = Source("b", audio, title="Different title")
    local_reference = hrstock.import_files(root, [audio])[0]["device"]
    original = Source("c", audio)
    duplicate = hrstock.preview(root, original, scope)
    assert duplicate.originals == original.desired
    assert hrstock.apply(root, original, duplicate) == (0, 0)
    assert not original.downloads
    plan = hrstock.preview(root, alice, scope)
    assert hrstock.apply(root, alice, plan) == (1, 0)
    with pytest.raises(RuntimeError, match="changed after preview"):
        hrstock.apply(root, alice, plan)
    assert hrstock.apply(root, bob, hrstock.preview(root, bob, scope)) == (1, 0)
    assert hrstock.apply(root, bob, hrstock.preview(root, bob, scope)) == (0, 0)

    before = hrstock.read_library(root)
    alice_copy = before.tracks[1]
    bob_copy = before.tracks[2]
    assert alice_copy.managed and bob_copy.managed
    alice.desired = ()
    assert hrstock.apply(root, alice, hrstock.preview(root, alice, scope)) == (0, 1)
    after = hrstock.read_library(root)
    assert after.tracks == (before.tracks[0], bob_copy)
    assert after.playlists[0].track_ids == (before.tracks[0].index, bob_copy.index)
    assert not (root / alice_copy.path[1:]).exists()
    assert (root / bob_copy.path[1:]).is_file()
    assert (root / local_reference[1:]).is_file()


def test_rockbox_markers_never_allow_transaction(tmp_path, audio):
    root = tmp_path / "volume"
    create_fixture(root)
    forbidden = root / "Music/PodLink"
    forbidden.mkdir(parents=True)
    protected = forbidden / "keep"
    protected.write_bytes(b"PodBox audio")
    assert hrstock.detect(root) is None
    db = root / ITUNES / "iTunesDB"
    old = db.read_bytes()
    with pytest.raises(RuntimeError, match="PodLink"):
        hrstock.import_files(root, [audio])
    assert protected.read_bytes() == b"PodBox audio"
    assert db.read_bytes() == old
    assert not db.with_name("iTunesDB.astrapod-bak").exists()


def test_sync_preserves_a_file_referenced_by_an_unowned_record(tmp_path, audio):
    from dataclasses import replace

    root = tmp_path / "volume"
    create_fixture(root)
    scope = hrmediasync.Scope("all", "fixture")
    source = Source("a", audio)
    assert hrstock.apply(root, source, hrstock.preview(root, source, scope)) == (1, 0)

    # Simulate an external library manager making another DB record for the
    # same file. Removing the owned record must leave the other playable.
    with hrstock.hrstockdb.open_database(root) as database:
        owned, = database.tracks
        copied = replace(owned, id=0, title="Independent library entry")
        remaining_id = database.add_track(copied, filetype="Apple Lossless audio file",
                                          sample_rate=48000)
        database.save()

    source.desired = ()
    assert hrstock.apply(root, source, hrstock.preview(root, source, scope)) == (0, 1)
    remaining, = hrstock.read_library(root).tracks
    assert remaining.index == remaining_id and not remaining.managed
    assert (root / remaining.path[1:]).is_file()


def test_changed_server_aborts_before_any_volume_write(tmp_path, audio, monkeypatch):
    root = tmp_path / "volume"
    create_fixture(root)
    scope = hrmediasync.Scope("all", "fixture")
    source = Source("a", audio)
    plan = hrstock.preview(root, source, scope)
    initial_db = (root / ITUNES / "iTunesDB").read_bytes()
    download = source.download

    def change_during_download(track, path):
        download(track, path)
        source.desired = ()

    monkeypatch.setattr(source, "download", change_during_download)
    with pytest.raises(RuntimeError, match="Jellyfin changed"):
        hrstock.apply(root, source, plan)
    assert (root / ITUNES / "iTunesDB").read_bytes() == initial_db
    assert not (root / ITUNES / "iTunesDB.astrapod-bak").exists()
    assert not hrstock.read_library(root).tracks


def test_external_change_during_journal_copy_preserves_external_database(tmp_path, audio):
    root = tmp_path / "volume"
    create_fixture(root)
    database = root / ITUNES / "iTunesDB"
    original = database.read_bytes()
    external = original + b"external update"

    def update_on_copy(event):
        if event["state"] == "upload" and event["sent"]:
            database.write_bytes(external)

    with pytest.raises(RuntimeError, match="database changed"):
        hrstock.import_files(root, [audio], update_on_copy)
    assert database.read_bytes() == external
    assert not database.with_name("iTunesDB.astrapod-bak").exists()
    assert not tuple((root / "iPod_Control/Music").glob("F*/*.m4a"))
    assert not hrstock.hrstocktxn.pending(database.parent)
