"""Exercise Disk Mode without a real tty, block device or UDisks."""
from contextlib import contextmanager, nullcontext
import hashlib
import os
from pathlib import Path
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

    def add(uuid, path, serial, block_name, product="1261"):
        usb = tmp_path / path
        sys_dev = usb / "target" / block_name
        sys_dev.mkdir(parents=True)
        for key, value in {"idVendor": "05ac", "idProduct": product, "serial": serial}.items():
            (usb / key).write_text(value)
        (sys_dev / "dev").write_text("8:17\n")
        node = tmp_path / block_name
        node.touch()
        (uuids / uuid).symlink_to(node)
        (sys_block / block_name).symlink_to(sys_dev)

    add("ABC0-9090", "1-5", "1" + SERIAL[1:], "sdb1")
    add("ABC0-9091", "1-6", "1" + SERIAL[1:], "sdc1")
    add("ABC0-9092", "1-7", "199999999", "sdd1")
    add("ABC0-9093", "1-8", "1" + SERIAL[1:], "sde1", "9999")
    monkeypatch.setattr(disk, "UUIDS", uuids)
    monkeypatch.setattr(disk, "SYS_BLOCK", sys_block)
    monkeypatch.setattr(disk, "run_host", lambda *_args, **_kw: "vfat\n")
    volume = disk.find_volume((tmp_path / "1-5", SERIAL))
    assert volume.uuid_path.name == "ABC0-9090"
    assert volume.block.name == "sdb1"
    assert volume.present()
    assert disk.find_volume((tmp_path / "1-7", SERIAL)) is None
    assert disk.find_volume((tmp_path / "1-8", SERIAL)) is None


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
