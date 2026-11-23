"""Exercise Disk Mode without a real tty, block device or UDisks."""
from contextlib import contextmanager, nullcontext
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import sys
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "herdremote")]

import hrdisk
import hrdisksync as disk
import hrlibrary
import hrmediasync as sync
import podlink

IDENTITY = "a" * 24
SERIAL = "212345678ABCDEF"
TTY = Path("/dev/ttyACM-test")


def name(mark, identity=IDENTITY):
    return identity + mark * 40 + ".flac"


def media(tmp_path, content=b"sample audio"):
    source = tmp_path / "source.flac"
    source.write_bytes(content)
    return podlink.MediaTrack(source, source, 44100, 44100)


@contextmanager
def media_disk(root, check=lambda: None):
    fd = os.open(root, disk.DIR_FLAGS)
    try:
        yield disk.MediaDisk(fd, check)
    finally:
        os.close(fd)


@pytest.fixture
def device(monkeypatch, tmp_path):
    root = tmp_path / "volume"
    root.mkdir()
    usb = Path("/sys/devices/mock/usb/1-2")
    player = TTY, (usb, SERIAL)
    mount = ("123", root, "/", "vfat")
    st_dev = root.stat().st_dev
    volume = Mock(spec=disk.Volume)
    volume.mount.return_value = mount
    volume.mounted.return_value = mount
    volume.mounts.return_value = [mount]
    volume.number = f"{os.major(st_dev)}:{os.minor(st_dev)}"
    volume.present.return_value = False
    volume.eject.side_effect = lambda: volume.present.configure_mock(return_value=False)
    link = SimpleNamespace(opened=True, port=SimpleNamespace(port=str(TTY)), send=Mock())
    control = hrdisk.Control(str(tmp_path / "disk.sock"))
    control.start()
    control.hello(hrdisk.CAPABILITY, Mock())
    finished = Event()

    def serve():
        while not finished.wait(.005):
            control.poll(link)
            if link.send.called:
                volume.present.return_value = True
                return

    worker = Thread(target=serve, daemon=True)
    worker.start()
    monkeypatch.setattr(hrdisk, "default_socket", lambda: control.path)
    monkeypatch.setattr(disk, "selected_player", lambda _port: player)
    monkeypatch.setattr(disk, "usb_identity", lambda _path: player[1])
    monkeypatch.setattr(disk, "find_volume",
                        lambda _identity: volume if volume.present() else None)
    monkeypatch.setattr(disk.shutil, "which", lambda _tool: "/mock")
    refresh = Mock()
    monkeypatch.setattr(disk, "refresh_after_eject", refresh)
    monkeypatch.setattr(sync, "connect_device", lambda _port: nullcontext((link, SERIAL)))
    try:
        yield root, player, volume, link, refresh
    finally:
        finished.set()
        worker.join()
        control.close()


def jellyfin_plan(tmp_path, monkeypatch, root):
    folder = root / "Music/PodLink/Jellyfin"
    folder.mkdir(parents=True)
    (folder / name("1")).write_bytes(b"old")
    (folder / name("2", "b" * 24)).write_bytes(b"other account")
    (folder.parent / name("3")).write_bytes(b"MS1 import")
    (root / "keep").write_bytes(b"unmanaged")
    track = sync.Track("id", "new", ".flac", name("4"))
    scope = sync.Scope("all", "All")
    plan = sync.Plan(IDENTITY, scope, SERIAL, (track,), frozenset({name("1")}))
    source = SimpleNamespace(identity=IDENTITY, tracks=Mock(return_value=plan.tracks))
    monkeypatch.setattr(sync, "inventory", lambda *_: plan.owned)
    monkeypatch.setattr(sync, "prepare_track", lambda *_: media(tmp_path))
    return folder, source, plan


def test_apply_ms2_layout_and_eject_before_refresh(device, tmp_path, monkeypatch):
    root, player, volume, link, refresh = device
    folder, source, plan = jellyfin_plan(tmp_path, monkeypatch, root)
    refresh.side_effect = lambda *_: volume.eject.assert_called_once()
    assert disk.apply(source, plan, str(TTY), serial_access=nullcontext) == (1, 1)
    assert link.send.call_args.args[1].startswith(b"DK\t1\t")
    assert (folder / name("4")).read_bytes() == b"sample audio"
    assert not (folder / name("1")).exists()
    assert (folder / name("2", "b" * 24)).read_bytes() == b"other account"
    assert (folder.parent / name("3")).read_bytes() == b"MS1 import"
    assert (root / "keep").read_bytes() == b"unmanaged"
    assert not list(folder.glob("*.part"))
    refresh.assert_called_once_with(player[1], nullcontext)


def test_stale_mounted_inventory_aborts_and_ejects(device, tmp_path, monkeypatch):
    root, _, volume, _, refresh = device
    folder, source, plan = jellyfin_plan(tmp_path, monkeypatch, root)
    (folder / name("5")).write_bytes(b"new since preview")
    with pytest.raises(RuntimeError, match="changed after preview"):
        disk.apply(source, plan, str(TTY), serial_access=nullcontext)
    assert not (folder / name("4")).exists()
    assert (folder / name("1")).exists()
    volume.eject.assert_called_once()
    refresh.assert_not_called()


def test_changed_source_preserves_removals_and_refreshes_additions(device, tmp_path, monkeypatch):
    root, _, volume, _, refresh = device
    folder, source, plan = jellyfin_plan(tmp_path, monkeypatch, root)
    source.tracks.side_effect = [plan.tracks, ()]
    with pytest.raises(RuntimeError, match="deletion was skipped"):
        disk.apply(source, plan, str(TTY), serial_access=nullcontext)
    assert (folder / name("4")).exists()
    assert (folder / name("1")).exists()
    volume.eject.assert_called_once()
    refresh.assert_called_once()


def test_direct_import_reuses_transcode_and_reports_content_hash(device, tmp_path, monkeypatch):
    root, _, volume, _, refresh = device
    source = media(tmp_path).source
    commands = []

    def ffmpeg(*args):
        commands.append(args)
        if args[0] == "ffprobe":
            return '{"streams":[{"sample_rate":"96000","codec_name":"flac"}]}'
        Path(args[-1]).write_bytes(b"converted FLAC")

    monkeypatch.setattr(hrlibrary, "_probe_tags", lambda _path: None)
    monkeypatch.setattr(podlink, "media_command", ffmpeg)
    events = []
    result = disk.import_files(str(TTY), [source], events.append, False,
                               serial_access=nullcontext)
    expected = hashlib.sha256(b"converted FLAC").hexdigest() + ".flac"
    assert result[0]["device"] == f"/Music/PodLink/{expected}"
    assert result[0]["sample_rate_hz"] == 48000
    assert (root / "Music/PodLink" / expected).read_bytes() == b"converted FLAC"
    assert commands[1][commands[1].index("-ar") + 1] == "48000"
    assert [event["state"] for event in events] == ["probe", "upload", "upload", "done"]
    volume.eject.assert_called_once()
    refresh.assert_called_once()


def test_disk_loss_during_publish_leaves_only_previous_tracks(device, tmp_path):
    root, player, volume, _, refresh = device
    with pytest.raises(RuntimeError, match="失去挂载"):
        with disk.disk_session(player, nullcontext) as mounted:
            mounted.publish(media(tmp_path), name("1"))

            def disconnect(_sent):
                volume.present.return_value = False

            mounted.publish(media(tmp_path), name("2"), disconnect)
    folder = root / "Music/PodLink/Jellyfin"
    assert {file.name for file in folder.iterdir()} == {name("1")}
    volume.eject.assert_called_once()
    refresh.assert_called_once()


def test_bad_hash_does_not_publish(tmp_path, monkeypatch):
    prepared = media(tmp_path)
    fsync = os.fsync

    def corrupt(fd):
        fsync(fd)
        if os.fstat(fd).st_size == len(b"sample audio"):
            os.pwrite(fd, b"x", 0)

    monkeypatch.setattr(disk.os, "fsync", corrupt)
    with media_disk(tmp_path) as mounted, pytest.raises(RuntimeError, match="校验失败"):
        mounted.publish(prepared)
    assert not list((tmp_path / "Music/PodLink").iterdir())


def test_no_overwrite_or_path_escape(tmp_path):
    prepared = media(tmp_path)
    with media_disk(tmp_path) as mounted:
        published = mounted.publish(prepared, name("1"))
        with pytest.raises(FileExistsError):
            mounted.publish(media(tmp_path, b"new contents"), name("1"))
        with pytest.raises(ValueError):
            mounted.publish(prepared, "../outside.flac")
        with pytest.raises(RuntimeError, match="仅可删除"):
            mounted.remove(["../outside.flac"], IDENTITY)
    assert (tmp_path / published.lstrip("/")).read_bytes() == b"sample audio"


def test_rejects_symlink_parent(tmp_path):
    root = tmp_path / "volume"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "Music").symlink_to(outside, target_is_directory=True)
    with media_disk(root) as mounted, pytest.raises(OSError):
        mounted.publish(media(tmp_path))
    assert list(outside.iterdir()) == []


def test_volume_discovery_matches_uuid_vid_pid_serial_and_usb_port(tmp_path, monkeypatch):
    uuids = tmp_path / "uuids"
    uuids.mkdir()
    sys_block = tmp_path / "sys_block"
    sys_block.mkdir()

    def add(uuid, path, serial, block_name, product="1261", manufacturer="PodBox"):
        usb = tmp_path / path
        sys_dev = usb / "target" / block_name
        sys_dev.mkdir(parents=True)
        attributes = {"idVendor": "05ac", "idProduct": product, "serial": serial,
                      "manufacturer": manufacturer}
        for key, value in attributes.items():
            (usb / key).write_text(value)
        (sys_dev / "dev").write_text("8:17\n")
        node = tmp_path / block_name
        node.touch()
        (uuids / uuid).symlink_to(node)
        (sys_block / block_name).symlink_to(sys_dev)

    add("ABC0-9090", "1-5", "1" + SERIAL[1:], "sdb1")
    add("ABC0-9091", "1-6", "1" + SERIAL[1:], "sdc1")
    add("ABC0-9092", "1-7", "199999999", "sdd1", manufacturer="Apple Inc.")
    add("ABC0-9093", "1-8", "1" + SERIAL[1:], "sde1", "9999")
    monkeypatch.setattr(disk, "UUIDS", uuids)
    monkeypatch.setattr(disk, "SYS_BLOCK", sys_block)
    monkeypatch.setattr(disk, "run_host", lambda *_args, **_kw: "vfat\n")
    volume = disk.find_volume((tmp_path / "1-5", SERIAL))
    assert volume.uuid_path.name == "ABC0-9090"
    assert volume.block.name == "sdb1"
    assert volume.present()
    assert disk.disk_player(volume) == (tmp_path / "1-5", SERIAL)
    assert disk.find_volume((tmp_path / "1-7", SERIAL)) is None
    assert disk.find_volume((tmp_path / "1-8", SERIAL)) is None
    with pytest.raises(RuntimeError, match="多个 FAT 卷"):
        disk.current_volume()
    with pytest.raises(RuntimeError, match="多个 FAT 卷"):
        disk.current_volume(SERIAL)
    (uuids / "ABC0-9091").unlink()
    assert disk.current_volume(SERIAL) == volume
    (uuids / "ABC0-9090").unlink()
    assert disk.current_volume(SERIAL) is None
    stock = disk.current_volume()
    assert stock.block.name == "sdd1"
    assert disk.disk_player(stock) is None


def test_busy_unmount_never_ejects(monkeypatch, tmp_path):
    volume = disk.Volume(tmp_path / "uuid", tmp_path / "block", "8:2", ())
    monkeypatch.setattr(disk.Volume, "present", lambda _: True)
    monkeypatch.setattr(disk.Volume, "mounts", lambda _: [("1", tmp_path, "/", "vfat")])
    monkeypatch.setattr(disk, "run_host", Mock(side_effect=RuntimeError("device is busy")))
    eject = Mock()
    monkeypatch.setattr(disk, "eject_drive", eject)
    with pytest.raises(RuntimeError, match="busy"):
        volume.eject()
    eject.assert_not_called()


def test_daemon_rejects_wrong_port_before_token_is_consumed(device, tmp_path, monkeypatch):
    _, _, volume, link, _ = device
    with pytest.raises(RuntimeError, match="different player"):
        disk.enter_disk(Path("/dev/other"), disk.selected_player(None)[1],
                        hrdisk.default_socket())
    link.send.assert_not_called()
    volume.mount.assert_not_called()


def test_waits_for_disk_hello_after_releasing_serial(monkeypatch, tmp_path):
    usb = Path("/usb/1-2"), SERIAL
    monkeypatch.setattr(disk, "usb_identity", lambda _path: usb)
    mounted = object()
    find = Mock(side_effect=[None, mounted])
    monkeypatch.setattr(disk, "find_volume", find)
    request = Mock(side_effect=[b"3 no PodLink session\n",
                                b"4 firmware lacks disk-mode-v1 hello\n",
                                b"0 disk request sent\n"])
    monkeypatch.setattr(disk, "request_disk", request)
    monkeypatch.setattr(disk.time, "sleep", lambda _time: None)
    assert disk.enter_disk(TTY, usb, "mock.sock") is mounted
    assert request.call_count == 3


def test_mount_discovery_and_orderly_eject(monkeypatch, tmp_path):
    table = tmp_path / "mountinfo"
    table.write_text("")
    mount = tmp_path / "iPod Music"
    escaped = str(mount).replace(" ", r"\040")
    commands = []
    volume = disk.Volume(tmp_path / "uuid", tmp_path / "sdb1", "8:17", ())
    monkeypatch.setattr(disk, "MOUNTINFO", table)
    monkeypatch.setattr(disk.Volume, "present", lambda _: True)

    def run_host(tool, command, *args):
        commands.append((tool, command, *args))
        if command == "mount":
            table.write_text(f"55 42 8:17 / {escaped} rw - vfat /dev/sdb1 rw\n")
        else:
            assert command == "unmount"
            table.write_text("")

    def eject(block, _run):
        assert not volume.mounts()
        commands.append(("eject", block))
        return True

    monkeypatch.setattr(disk, "run_host", run_host)
    monkeypatch.setattr(disk, "eject_drive", eject)
    assert volume.mount() == ("55", mount, "/", "vfat")
    assert volume.mount() == ("55", mount, "/", "vfat")  # reuse automount
    volume.eject()
    assert commands == [
        ("udisksctl", "mount", "--no-user-interaction", "-b", str(volume.block)),
        ("udisksctl", "unmount", "--no-user-interaction", "-b", str(volume.block)),
        ("eject", str(volume.block)),
    ]


def test_mount_authorization_retry_stays_accessible_to_desktop(monkeypatch, tmp_path):
    monkeypatch.setattr(disk, "mount_directory", lambda: tmp_path)
    monkeypatch.setattr(disk.Volume, "present", lambda _: True)
    tmp_path.chmod(0o700)
    volume = disk.Volume(tmp_path / "uuid", tmp_path / "sdb1", "8:17", ())
    mounts = []
    commands = []
    monkeypatch.setattr(disk.Volume, "mounts", lambda _: mounts)

    def run_host(*command, **_options):
        commands.append(command)
        if command[0] == "udisksctl":
            if command[1] == "mount":
                raise disk.HostAuthorizationError("NotAuthorizedCanObtain")
            raise RuntimeError("NotMounted")
        assert command[0] == "pkexec"
        if command[1] == "mount":
            mounts.append(("55", Path(command[-1]), "/", "vfat"))
        else:
            assert command[1:] == ("umount", str(mounts[0][1]))
            mounts.clear()

    monkeypatch.setattr(disk, "run_host", run_host)
    eject = Mock(return_value=True)
    monkeypatch.setattr(disk, "eject_drive", eject)
    mounted = volume.mount(read_only=True)[1]
    assert mounted.parent == tmp_path
    assert mounted.name.startswith(disk.MOUNT_PREFIX)
    assert commands[0][-2:] == ("--options", "ro")
    assert commands[1][:5] == ("pkexec", "mount", "-t", "vfat", "-o")
    assert f"uid={os.getuid()}" in commands[1][5].split(",")
    assert "ro" in commands[1][5].split(",")
    assert commands[1][-2] == str(volume.uuid_path)
    volume.eject()
    assert not mounted.exists()
    assert commands[2][:2] == ("udisksctl", "unmount")
    eject.assert_called_once()


@pytest.fixture
def mounted_player(device, monkeypatch):
    root, player, volume, link, refresh = device
    volume.usb = (player[1][0], "1" + SERIAL[1:])
    volume.manufacturer = "PodBox"
    volume.present.return_value = True
    no_serial = Mock(side_effect=AssertionError("Disk Mode must not use PodLink"))
    monkeypatch.setattr(disk, "selected_player", no_serial)
    monkeypatch.setattr(sync, "connect_device", no_serial)
    database = root / ".rockbox/library/database"
    shutil.copytree(Path(__file__).parent / "fixtures/tagcache", database)
    yield root, volume
    volume.mount.assert_not_called()
    volume.eject.assert_not_called()
    link.send.assert_not_called()
    refresh.assert_not_called()
    no_serial.assert_not_called()


def test_mounted_preview_uses_real_tagcache_and_existing_local_files(mounted_player):
    root, volume = mounted_player
    folder = root / "Music/PodLink/Jellyfin"
    folder.mkdir(parents=True)
    (folder / name("1")).write_bytes(b"owned")
    (folder / name("2", "b" * 24)).write_bytes(b"other account")
    (folder.parent / "音楽.flac").touch()
    tracks = (sync.Track("a", "a", ".flac", name("3"),
                         sync.music_tags("東京", ["Artist", "Guest"], "Fixture Album", 250, 2, 1)),
              sync.Track("b", "b", ".flac", name("4"),
                         sync.music_tags("Pilot", ["Artist", "Guest"], "Fixture Album", 250, 2, 2)))
    source = SimpleNamespace(identity=IDENTITY, tracks=Mock(side_effect=AssertionError))
    plan = disk.preview_mounted(source, sync.Scope("all", "All"), volume, source_tracks=tracks)
    assert plan.device == SERIAL
    assert plan.owned == {name("1")}
    assert plan.matched == {name("3")}
    assert plan.additions == (tracks[1],)
    source.tracks.assert_not_called()


def test_mounted_apply_rechecks_target_and_inventory(mounted_player, tmp_path, monkeypatch):
    root, volume = mounted_player
    folder, source, plan = jellyfin_plan(tmp_path, monkeypatch, root)
    written = Mock()
    with pytest.raises(RuntimeError, match="changed after preview"):
        disk.apply_mounted(source, replace(plan, device="299999"), volume, on_write=written)
    other = folder / name("5")
    other.touch()
    with pytest.raises(RuntimeError, match="changed after preview"):
        disk.apply_mounted(source, plan, volume, on_write=written)
    written.assert_not_called()
    other.unlink()
    assert disk.apply_mounted(source, plan, volume, on_write=written) == (1, 1)
    written.assert_called_once_with()
    assert (folder / name("4")).read_bytes() == b"sample audio"
    assert not (folder / name("1")).exists()


def test_mounted_import_uses_disk_duplicate_index(mounted_player, tmp_path, monkeypatch):
    root, volume = mounted_player
    folder = root / "Music/Local"
    folder.mkdir(parents=True)
    (folder / "2.flac").touch()
    prepared = media(tmp_path)
    duplicate = tmp_path / "duplicate.flac"
    duplicate.touch()
    monkeypatch.setattr(podlink, "prepare_media", lambda *_: [prepared])

    def tags(path):
        title, number = ("Pilot", 2) if path == duplicate else ("Track 3", 3)
        return sync.music_tags(title, ["Artist", "Guest"], "Fixture Album", 250, 2, number)

    monkeypatch.setattr(hrlibrary, "_probe_tags", tags)
    events = []
    written = Mock()
    result = disk.import_mounted(volume, [duplicate, prepared.source], events.append, True,
                                 on_write=written)
    assert [item["source"] for item in result] == [str(prepared.source)]
    assert [event["state"] for event in events if event["file"] == str(duplicate)] == ["probe", "skip"]
    assert (root / result[0]["device"].lstrip("/")).read_bytes() == b"sample audio"
    written.assert_called_once_with()


def test_mounted_import_records_write_even_when_fsync_fails(mounted_player, tmp_path, monkeypatch):
    root, volume = mounted_player
    folder = root / "Music/PodLink"
    folder.mkdir(parents=True)
    prepared = media(tmp_path)
    monkeypatch.setattr(podlink, "prepare_media", lambda *_: [prepared])
    monkeypatch.setattr(hrlibrary, "_probe_tags", lambda _: None)
    monkeypatch.setattr(disk.os, "fsync", Mock(side_effect=OSError("sync failed")))
    written = Mock()
    with pytest.raises(OSError, match="sync failed"):
        disk.import_mounted(volume, [prepared.source], None, False, on_write=written)
    written.assert_called_once_with()
    assert list(folder.iterdir()) == []


def test_stock_firmware_refuses_media_before_a_mount_or_write(mounted_player):
    root, volume = mounted_player
    volume.manufacturer = "Apple Inc."
    assert disk.disk_player(volume) is None
    with pytest.raises(RuntimeError, match="Apple 原厂固件"):
        disk.preview_mounted(None, None, volume)
    with pytest.raises(RuntimeError, match="Apple 原厂固件"):
        disk.apply_mounted(None, None, volume)
    with pytest.raises(RuntimeError, match="Apple 原厂固件"):
        disk.import_mounted(volume, [], None, False)
    volume.mounted.assert_not_called()
    assert not (root / "Music").exists()


def test_refresh_targets_reconnected_player_on_renumbered_tty(monkeypatch):
    from serial.tools import list_ports

    usb = Path("/usb/1-2"), SERIAL
    monkeypatch.setattr(list_ports, "comports", lambda: [
        SimpleNamespace(device="/dev/ttyACM0"), SimpleNamespace(device="/dev/ttyACM2")])
    monkeypatch.setattr(disk, "usb_identity", lambda path: usb if path.name == "ttyACM2"
                        else (Path("/usb/1-3"), SERIAL))
    link = Mock()
    serial = Mock(return_value=nullcontext((link, SERIAL)))
    monkeypatch.setattr(sync, "connect_device", serial)
    monkeypatch.setattr(podlink, "expect_media", Mock())
    disk.refresh_after_eject(usb, nullcontext)
    serial.assert_called_once_with("/dev/ttyACM2")
    link.send.assert_called_once_with(podlink.TEXT, b"MS1 REFRESH")
    podlink.expect_media.assert_called_once_with(link, b"MS1 QUEUED")


def test_mounted_apply_revalidates_local_matches(mounted_player):
    root, volume = mounted_player
    local = root / "Music/PodLink/音楽.flac"
    local.parent.mkdir(parents=True)
    local.touch()
    match = sync.music_tags("東京", ["Artist", "Guest"], "Fixture Album", 250, 2, 1)
    source_track = sync.Track("a", "a", ".flac", name("3"), match)
    source = SimpleNamespace(identity=IDENTITY, tracks=lambda _: (source_track,))
    plan = disk.preview_mounted(source, sync.Scope("all", "All"), volume)
    assert plan.matched == {source_track.name}
    local.unlink()
    written = Mock()
    with pytest.raises(RuntimeError, match="changed after preview"):
        disk.apply_mounted(source, plan, volume, on_write=written)
    written.assert_not_called()
