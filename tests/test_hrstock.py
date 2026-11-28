"""Integration coverage using temporary stock databases and generated media."""
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "herdremote"))

import hrmediasync
import hrstock
from test_hrstockdb import DEVICE, ITUNES, create_fixture, libgpod


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
    ("MB029", 1, False), ("MC297", 1, False), ("MC293", 2, True),
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
    create_fixture(root, "MB565")
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


def test_jellyfin_preview_survives_mount_path_change_for_same_device(tmp_path, audio):
    root = tmp_path / "read-only-mount"
    create_fixture(root)
    remounted = tmp_path / "writable-mount"
    shutil.copytree(root, remounted)
    scope = hrmediasync.Scope("all", "fixture")
    source = Source("a", audio)
    key = ("usb-serial", "1234-ABCD")

    plan = hrstock.preview(root, source, scope, volume_key=key)
    with pytest.raises(RuntimeError, match="changed after preview"):
        hrstock.apply(remounted, source, plan, volume_key=("different-serial", key[1]))
    assert not hrstock.read_library(remounted).tracks
    assert hrstock.apply(remounted, source, plan, volume_key=key) == (1, 0)


def test_import_cancellation_before_commit_leaves_database_and_media_unchanged(tmp_path, audio):
    root = tmp_path / "volume"
    create_fixture(root)
    database = root / ITUNES / "iTunesDB"
    original = database.read_bytes()

    class Cancelled(Exception):
        pass

    def before_commit():
        raise Cancelled()

    with pytest.raises(Cancelled):
        hrstock.import_files(root, [audio], before_commit=before_commit)
    assert database.read_bytes() == original
    assert not database.with_name("iTunesDB.astrapod-bak").exists()
    assert not tuple((root / "iPod_Control/Music").glob("F*/*.m4a"))


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


@pytest.fixture
def movie(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required")
    source = tmp_path / "Test Episode.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=960x540:rate=50:duration=1",
                    "-c:v", "mpeg4", str(source)], check=True)
    return source


@pytest.fixture
def pictures(tmp_path):
    if not shutil.which("magick"):
        pytest.skip("ImageMagick is required")
    folder = tmp_path / "Weekend"
    folder.mkdir()
    sources = [folder / "red.jpg", folder / "blue.heic"]
    for color, path in zip(("red", "blue"), sources):
        subprocess.run(["magick", "-size", "96x64", f"xc:{color}", str(path)], check=True)
    return sources


@pytest.mark.parametrize("model,checksum,compressed", [
    ("MB029", 1, False), ("MC293", 2, True),
])
def test_import_photo_albums_and_stock_video(tmp_path, pictures, movie, model, checksum,
                                            compressed, monkeypatch):
    from hrstockphoto import _counts
    import hrstockvideo

    used_encoders = []
    command = hrstockvideo.media_command

    def encode(*args):
        if "-c:v" in args and "-f" in args and args[-2] == "ipod":
            encoder = args[args.index("-c:v") + 1]
            used_encoders.append(encoder)
            if compressed and encoder == "h264_nvenc":
                raise RuntimeError("simulated unavailable NVIDIA encoder")
        return command(*args)

    monkeypatch.setattr(hrstockvideo, "media_command", encode)
    root = tmp_path / "volume"
    create_fixture(root, model, checksum, compressed)
    database = root / ITUNES / ("iTunesCDB" if compressed else "iTunesDB")
    if compressed:
        sysinfo = root / DEVICE / "SysInfoExtended"
        props = plistlib.loads(sysinfo.read_bytes())
        props["AlbumArt"] = [{"FormatId": 1055, "PixelFormat": "4C353635",
                              "RenderWidth": 128, "RenderHeight": 128, "DisplayWidth": 128}]
        sysinfo.write_bytes(plistlib.dumps(props))
    original = database.read_bytes()
    assert len(hrstock.import_files(root, pictures)) == 2
    assert database.read_bytes() == original
    assert _counts(root) == (2, 2)
    assert {p.name for p in (root / "Photos/Thumbs").iterdir()} == {
        "F1024_1.ithmb", "F1066_1.ithmb", "F1067_1.ithmb",
    }
    assert (root / "Photos/Thumbs/F1024_1.ithmb").stat().st_size == 2 * 320 * 240 * 2

    records = hrstock.import_files(root, [movie, pictures[0]], video_kind="tv-show")
    assert len(records) == 2
    assert _counts(root) == (3, 2)
    video = root / records[0]["device"][1:]
    assert video.suffix == ".m4v"
    assert hrstockvideo._compatible(*hrstockvideo._probe(video))
    assert used_encoders[0] == "h264_nvenc"
    if compressed:
        assert used_encoders == ["h264_nvenc", "libx264"]
    with hrstock._session(root) as (_, db):
        track, = db.tracks
        assert track.media_type == 64
        assert track.title == "Test Episode"
        assert track.has_artwork


def test_photo_and_video_publication_rolls_back(tmp_path, pictures, movie, monkeypatch):
    root = tmp_path / "volume"
    create_fixture(root, "MB029")
    database = root / ITUNES / "iTunesDB"
    original = database.read_bytes()
    assert hrstock.import_files(root, [pictures[0]])
    photo_dir = root / "Photos"
    photos_before = {p.relative_to(photo_dir): p.read_bytes()
                     for p in photo_dir.rglob("*") if p.is_file()}
    replace = hrstock.hrstocktxn._replace

    def fail(source, target, **kwargs):
        if target.name == "Photo Database" and target.parts[-2] == "Photos":
            if source.name.startswith("new-"):
                raise OSError("simulated photo write failure")
        return replace(source, target, **kwargs)

    monkeypatch.setattr(hrstock.hrstocktxn, "_replace", fail)
    with pytest.raises(OSError, match="simulated photo write failure"):
        hrstock.import_files(root, [movie, pictures[0]])
    assert database.read_bytes() == original
    assert {p.relative_to(photo_dir): p.read_bytes()
            for p in photo_dir.rglob("*") if p.is_file()} == photos_before
    assert not list(root.rglob("*.m4v"))


@pytest.mark.parametrize("model,checksum,compressed", [
    ("MB029", 1, False), ("MC293", 2, True),
])
def test_stock_sync_reconciles_saved_playlists(tmp_path, model, checksum, compressed):
    from test_hrstockdb import add_playlist, example_row

    root = tmp_path / "volume"
    create_fixture(root, model, checksum, compressed)
    with hrstock.hrstockdb.open_database(root) as db:
        first, second = (db.add_track(example_row(n), filetype="MPEG audio file",
                                      sample_rate=44100) for n in (1, 2))
        add_playlist(hrstock.hrstockdb._library(), db, "New Playlist 1", (second,))
        db.save()
    original = hrstock.read_library(root).playlists[1]

    directory = root / "pLAYLISTS"
    directory.mkdir()
    playlist = directory / "New Playlist 1.M3U8"
    playlist.write_bytes(b"\xef\xbb\xbf#EXTM3U\r\n#EXTINF:1,Track\r\n"
                         b"../ipod_control/music/F07/ab01.MP3\r\n"
                         b"C:\\iPod_Control\\Music\\F07\\AB02.mp3\r\n"
                         b"/Music/Unregistered.flac\r\n"
                         b"/iPod_Control/Music/F07/AB01.mp3\r\n")
    ignored = directory / "Ignored.m3u"
    ignored.write_text("/iPod_Control/Music/F07/AB01.mp3\n")
    scope = hrmediasync.Scope("all", "fixture")
    source = Source("a", None)
    source.desired = ()
    reports = []

    plan = hrstock.preview(root, source, scope)
    assert not plan.additions and not plan.removals
    assert plan.playlist_report.changes == 1
    assert plan.playlist_report.unmatched == (("/pLAYLISTS/New Playlist 1.M3U8",
                                         "/Music/Unregistered.flac"),)
    assert hrstock.apply(root, source, plan, playlist_report=reports.append) == (0, 0)
    assert reports == [plan.playlist_report]

    master, untouched, saved = hrstock.read_library(root).playlists
    assert master.track_ids == (first, second)
    assert untouched == original
    assert saved.name == "New Playlist 2"
    assert saved.track_ids == (first, second, first)
    assert not source.downloads

    database = root / ITUNES / ("iTunesCDB" if compressed else "iTunesDB")
    db_before = database.read_bytes()
    index = root / ITUNES / hrstock.hrstockplaylists.INDEX
    index_before = index.read_bytes()
    plan = hrstock.preview(root, source, scope)
    assert plan.playlist_report.changes == 0
    assert hrstock.apply(root, source, plan) == (0, 0)
    assert database.read_bytes() == db_before
    assert index.read_bytes() == index_before

    playlist.write_text("/iPod_Control/Music/F07/AB02.mp3\n")
    with pytest.raises(RuntimeError, match="playlists changed after preview"):
        hrstock.apply(root, source, plan)
    assert database.read_bytes() == db_before
    assert hrstock.apply(root, source, hrstock.preview(root, source, scope)) == (0, 0)
    updated = hrstock.read_library(root).playlists[-1]
    assert updated.id == saved.id
    assert updated.track_ids == (second,)

    playlist.unlink()
    plan = hrstock.preview(root, source, scope)
    assert plan.playlist_report.changes == 1
    assert hrstock.apply(root, source, plan) == (0, 0)
    assert hrstock.read_library(root).playlists == (master, original)
    assert hrstock.preview(root, source, scope).playlist_report.changes == 0


def test_stock_sync_reads_custom_catalogue_and_ignores_live_queue(tmp_path, monkeypatch):
    import hrstockdevice

    root = tmp_path / "volume"
    create_fixture(root)
    usb = hrstockdevice._UsbDisk(None, "", "05ac", hrstockdevice.CLASSIC_USB_PRODUCT,
                                 "Apple", "", root / "no-vpd")
    monkeypatch.setattr(hrstockdevice, "_usb_disk", lambda _: usb)
    firmware = root / ".rockbox"
    firmware.mkdir()
    (firmware / "config.cfg").write_text("playlist catalog directory: /.rockbox\n")
    (firmware / "on-the-go.m3u8").write_text("/Music/Live Queue.flac\n")
    custom = firmware / "Custom"
    custom.mkdir()
    (custom / "测试.m3u8").write_text("/Music/未匹配.flac\n")
    source = Source("a", None)
    source.desired = ()
    scope = hrmediasync.Scope("all", "fixture")

    plan = hrstock.preview(root, source, scope)
    assert plan.playlist_report.changes == 1
    assert plan.playlist_report.unmatched == (("/.rockbox/Custom/测试.m3u8", "/Music/未匹配.flac"),)
    assert hrstock.apply(root, source, plan) == (0, 0)
    saved = hrstock.read_library(root).playlists[-1]
    assert saved.name == "测试"
    assert saved.track_ids == ()


def test_stock_sync_rolls_back_playlist_index_if_database_publication_fails(tmp_path, monkeypatch):
    root = tmp_path / "volume"
    create_fixture(root)
    playlist = root / "Playlists" / "New Playlist 1.m3u8"
    playlist.parent.mkdir()
    playlist.write_text("/Music/Unregistered.flac\n")
    source = Source("a", None)
    source.desired = ()
    scope = hrmediasync.Scope("all", "fixture")
    plan = hrstock.preview(root, source, scope)
    database = root / ITUNES / "iTunesDB"
    original = database.read_bytes()
    index = database.with_name(hrstock.hrstockplaylists.INDEX)
    replace = hrstock.hrstocktxn._replace
    published_index = []

    def fail(source, target, **kwargs):
        if target.name == database.name and not published_index:
            published_index.append(target.with_name(index.name).is_file())
            raise OSError("synthetic database publication failure")
        return replace(source, target, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(hrstock.hrstocktxn, "_replace", fail)
        with pytest.raises(OSError, match="synthetic database publication failure"):
            hrstock.apply(root, source, plan)
    assert published_index == [True]
    assert not index.exists()
    assert database.read_bytes() == original
    assert hrstock.apply(root, source, plan) == (0, 0)
    assert index.is_file()


def test_stock_sync_rechecks_playlist_sources_before_publishing(tmp_path, monkeypatch):
    root = tmp_path / "volume"
    create_fixture(root)
    playlist = root / "Playlists" / "New Playlist 1.m3u8"
    playlist.parent.mkdir()
    playlist.write_text("/Music/First.flac\n")
    source = Source("a", None)
    source.desired = ()
    scope = hrmediasync.Scope("all", "fixture")
    plan = hrstock.preview(root, source, scope)
    database = root / ITUNES / "iTunesDB"
    original = database.read_bytes()
    prepare = hrstock.hrstocktxn._prepare

    def change_playlist(*args, **kwargs):
        staged = prepare(*args, **kwargs)
        playlist.write_text("/Music/Second.flac\n")
        return staged

    with monkeypatch.context() as patch:
        patch.setattr(hrstock.hrstocktxn, "_prepare", change_playlist)
        with pytest.raises(RuntimeError, match="playlists changed during sync"):
            hrstock.apply(root, source, plan)
    assert database.read_bytes() == original
    assert not database.with_name(hrstock.hrstockplaylists.INDEX).exists()
    assert playlist.read_text() == "/Music/Second.flac\n"
    refreshed = hrstock.preview(root, source, scope)
    assert refreshed.playlist_report.unmatched == (("/Playlists/New Playlist 1.m3u8",
                                                   "/Music/Second.flac"),)
    assert hrstock.apply(root, source, refreshed) == (0, 0)
