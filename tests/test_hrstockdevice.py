from pathlib import Path
import plistlib
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "herdremote"))

import hrstockdevice as stock


GUID = "000a270012345678"


def stock_volume(root, *, model=None, extended=None, scheme=0):
    control = root / "iPod_Control"
    tunes = control / "iTunes"
    tunes.mkdir(parents=True)
    database(tunes / "iTunesDB", scheme)
    device = control / "Device"
    device.mkdir()
    if model:
        (device / "SysInfo").write_text(f"ModelNumStr: {model}\nFirewireGuid: 0x{GUID}\n")
    if extended is not None:
        data = extended if isinstance(extended, bytes) else plistlib.dumps(extended)
        (device / "SysInfoExtended").write_bytes(data)
    return control, tunes, device


def database(path, scheme):
    header = b"mhbd" + (0xF4).to_bytes(4, "little") + bytes(0x28)
    path.write_bytes(header + scheme.to_bytes(2, "little") + bytes(0xF4 - 0x32))


@pytest.fixture(autouse=True)
def no_host_probe(monkeypatch, tmp_path):
    monkeypatch.setattr(stock, "SYS_DEV_BLOCK", tmp_path / "sys-dev-block")
    monkeypatch.setattr(stock, "_ejected_attachment", None)


@pytest.mark.parametrize(("number", "generation", "scheme"), [
    ("MB029", "classic-6g", 1),
    ("MB565", "classic-6.5g", 1),
    ("MC297LL/A", "classic-7g", 1),
])
def test_reads_stock_model_from_sysinfo(tmp_path, number, generation, scheme):
    stock_volume(tmp_path, model=number)
    info = stock.detect(tmp_path)
    assert info.generation == generation
    assert info.firewire_guid == GUID
    assert info.checksum == scheme


@pytest.mark.parametrize("model", ["MA147LL/A", "A450", "MB999", None])
def test_rejects_unsupported_stock_model(tmp_path, model):
    stock_volume(tmp_path, model=model)
    with pytest.raises(stock.UnsupportedStockModel, match="Only iPod Classic"):
        stock.detect(tmp_path)


def test_cdb_and_sqlite_capabilities(tmp_path):
    _, tunes, _ = stock_volume(tmp_path, extended={
        "SerialNumber": "000000009ZS",
        "DBVersion": 4,
        "SQLiteDB": True,
        "FireWireGUID": GUID.upper(),
    })
    compressed = tunes / "iTunesCDB"
    database(compressed, 2)
    info = stock.detect(tmp_path)
    assert info.database == compressed
    assert info.model_number == "MC293"
    assert info.firewire_guid == GUID
    assert info.db_version == 4
    assert info.checksum == 2
    assert info.sqlite


def test_hash72_from_database_without_extended_info(tmp_path):
    stock_volume(tmp_path, model="MC297", scheme=2)
    assert stock.detect(tmp_path).checksum == 2


def test_early_ipod_irregular_xml_is_normalized(tmp_path):
    data = b"""<?xml version="1.0" encoding="UTF-8"?>
    <plist version="1.0"><dict>
    <key>ImageSpecifications</key><array><key>1019</key><dict/></array>
    <key>AlbumArt</key><array><key>1069</key>
    <dict><key>FormatId</key><integer>1069</integer></dict></array>
    <key>SerialNumber</key><string>00000000Y5N</string>
    <key>UpdaterFamilyID</key><integer>24</integer>
    </dict></plist>"""
    _, _, device = stock_volume(tmp_path, extended=data)
    info = stock.detect(tmp_path)
    assert info.generation == "classic-6g"
    assert info.model_number == "MB029"
    normalized = plistlib.loads(info.sysinfo_extended)
    assert normalized["UpdaterFamilyID"] == 24
    assert normalized["AlbumArt"] == [{"FormatId": 1069}]
    assert "ImageSpecifications" not in normalized
    assert (device / "SysInfoExtended").read_bytes() == data


@pytest.mark.parametrize("marker", [".RockBox", "music/podlink"])
def test_stock_db_on_rockbox_volume_is_ignored(tmp_path, marker):
    stock_volume(tmp_path, model="MB565")
    (tmp_path / marker).mkdir(parents=True)
    assert stock.detect(tmp_path) is None
    with pytest.raises(stock.StockLayoutError):
        stock.assert_stock_layout(tmp_path)


def test_broken_symlink_marker_is_also_rejected(tmp_path):
    stock_volume(tmp_path, model="MB565")
    (tmp_path / ".rockbox").symlink_to("missing")
    assert stock.detect(tmp_path) is None


def test_follows_fat_casing_but_not_symlinks(tmp_path):
    control, tunes, _ = stock_volume(tmp_path, model="MB565")
    (tunes / "iTunesDB").rename(tunes / "itunesdb")
    control.rename(tmp_path / "IPOD_CONTROL")
    assert stock.detect(tmp_path).database.name == "itunesdb"
    tunes = tmp_path / "IPOD_CONTROL" / "iTunes"
    (tunes / "itunesdb").unlink()
    (tunes / "iTunesDB").symlink_to(tmp_path / "external")
    (tmp_path / "external").write_text("untouched")
    assert stock.detect(tmp_path) is None


def usb_descriptor(tmp_path, monkeypatch, *, serial=GUID, vendor="05ac", model="1261"):
    usb = tmp_path / "sys" / "1-1"
    disk = usb / "usb-storage" / "block" / "sdb"
    partition = disk / "sdb2"
    partition.mkdir(parents=True)
    (partition / "partition").touch()
    (disk / "dev").write_text("8:16")
    for key, value in (("idVendor", vendor), ("idProduct", model),
                       ("serial", serial), ("manufacturer", "Apple")):
        (usb / key).write_text(value)
    stock.SYS_DEV_BLOCK.mkdir()
    stat = tmp_path.stat().st_dev
    (stock.SYS_DEV_BLOCK / f"{stock.os.major(stat)}:{stock.os.minor(stat)}").symlink_to(partition)
    monkeypatch.setattr(stock, "_probe_scsi", lambda *_args: b"")
    return disk


def test_discovery_reads_mounted_apple_usb_roots(tmp_path, monkeypatch):
    usb_descriptor(tmp_path, monkeypatch)
    stat = tmp_path.stat().st_dev
    number = f"{stock.os.major(stat)}:{stock.os.minor(stat)}"
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"10 1 {number} / /run/media/iPod\\040Classic rw - vfat /dev/sdb2 rw\n"
        f"11 1 {number} /subdir /mnt/bind rw - vfat /dev/sdb2 rw\n"
        f"12 1 999:999 / /mnt/not-apple rw - vfat /dev/sdc1 rw\n"
        f"13 1 {number} / /mnt/non-ipod rw - ext4 /dev/sdb2 rw\n"
    )
    monkeypatch.setattr(stock, "MOUNTINFO", mountinfo)
    assert stock.mounted_apple_volumes() == (("10", Path("/run/media/iPod Classic")),)


def test_rejects_non_classic_apple_usb_before_raw_probe(tmp_path, monkeypatch):
    stock_volume(tmp_path)
    usb_descriptor(tmp_path, monkeypatch, model="1209")
    monkeypatch.setattr(stock, "_probe_scsi", lambda *_args: pytest.fail("Unexpected raw probe"))
    with pytest.raises(stock.UnsupportedStockModel, match="Only iPod Classic"):
        stock.detect(tmp_path)


def test_sysfs_usb_serial_without_raw_access(tmp_path, monkeypatch):
    stock_volume(tmp_path, model="MB565")
    disk = usb_descriptor(tmp_path, monkeypatch)
    (tmp_path / "iPod_Control/Device/SysInfo").write_text("ModelNumStr: MB565\n")
    info = stock.detect(tmp_path)
    assert stock._usb_disk(tmp_path.stat().st_dev).disk == Path("/dev") / disk.name
    assert info.firewire_guid == GUID
    assert info.generation == "classic-6.5g"


def test_cached_vpd_supplies_guid(tmp_path, monkeypatch):
    stock_volume(tmp_path, model="MC293")
    disk = usb_descriptor(tmp_path, monkeypatch, serial="")
    vpd = disk / "device" / "vpd_pg80"
    vpd.parent.mkdir()
    vpd.write_bytes(b"\x00\x80\x00\x10" + GUID.encode("ascii"))
    (tmp_path / "iPod_Control/Device/SysInfo").write_text("ModelNumStr: MC293\n")
    assert stock.detect(tmp_path).firewire_guid == GUID


@pytest.mark.parametrize(("vendor", "serial"), [
    ("05ac", "1AA0000000000360"),
    ("1d6b", GUID),
])
def test_nonstock_usb_descriptor_prevents_raw_probe(tmp_path, monkeypatch, vendor, serial):
    stock_volume(tmp_path)
    usb_descriptor(tmp_path, monkeypatch, vendor=vendor, serial=serial)

    def unexpected(*_args):
        pytest.fail("A non-stock disk must not be probed")

    monkeypatch.setattr(stock, "_probe_scsi", unexpected)
    assert stock.detect(tmp_path, authorize=True) is None


def test_dual_boot_markers_allowed_only_on_running_apple_firmware(tmp_path, monkeypatch):
    _, _, device = stock_volume(tmp_path, model="MC297")
    usb_descriptor(tmp_path, monkeypatch)
    (tmp_path / ".rockbox").mkdir()
    (tmp_path / "Music/PodLink").mkdir(parents=True)
    (device / "SysInfo").write_text(
        "ModelNumStr: MC297\nvisibleBuildID: 0x02058000 (2.0.5)\n"
        "pszSerialNumber: 012345679ZU\n"
    )
    fd = stock.os.open(tmp_path, stock.os.O_RDONLY | stock.os.O_DIRECTORY)
    try:
        pinned = Path(f"/proc/self/fd/{fd}")
        assert stock.assert_stock_layout(pinned).product == "1261"
        info = stock.detect(pinned, authorize=False)
    finally:
        stock.os.close(fd)
    assert info.firmware_version == "2.0.5"
    assert info.serial_number == "012345679ZU"
    usage = stock.os.statvfs(tmp_path)
    assert info.capacity_bytes == usage.f_blocks * usage.f_frsize
    assert 0 <= info.free_bytes <= info.capacity_bytes
    (tmp_path / "sys/1-1/serial").write_text("1AA0000000000360")
    assert stock.detect(tmp_path, authorize=False) is None


@pytest.mark.parametrize(("basic", "extended", "expected"), [
    ("visibleBuildID: 0x01218000 (1.2.1)", {"BuildID": "9.0.5"}, "1.2.1"),
    ("visibleBuildID: 0x01218000", {"VisibleBuildID": "2.0.5"}, "2.0.5"),
    ("buildID: 0x01208000", {}, "1.2"),
    ("buildID: 0x01208000", {"VisibleBuildID": 0x02058000}, "2.0.5"),
    ("BuildID: invalid", {"VisibleBuildID": "0x0A118000"}, None),
])
def test_firmware_version_formats(tmp_path, basic, extended, expected):
    _, _, device = stock_volume(tmp_path, model="MB565", extended=extended)
    path = device / "SysInfo"
    path.write_text(path.read_text() + basic + "\n")
    assert stock.detect(tmp_path).firmware_version == expected


def test_missing_firmware_version_uses_scsi_properties(tmp_path, monkeypatch):
    stock_volume(tmp_path, model="MC297")
    usb_descriptor(tmp_path, monkeypatch)
    monkeypatch.setattr(stock, "_probe_scsi", lambda *_args: plistlib.dumps({
        "SerialNumber": "012345679ZU", "VisibleBuildID": "2.0.5",
    }))
    assert stock.detect(tmp_path, authorize=False).firmware_version == "2.0.5"


@pytest.mark.parametrize(("product", "manufacturer", "serial", "kind"), [
    ("1261", "Apple Inc.", "000000012345", "stock"),
    ("1261", "Apple Inc.", "1AA0000000000360", "podbox-disk"),
    ("1261", "PodBox", "1ABCD", "podbox-disk"),
    ("1261", "PodBox", "4AA0000000000360", "none"),
    ("1209", "Apple Inc.", "000000012345", "none"),
])
def test_running_firmware_discovery_without_mount(tmp_path, monkeypatch,
                                                  product, manufacturer, serial, kind):
    disk = usb_descriptor(tmp_path, monkeypatch, serial=serial, model=product)
    usb = tmp_path / "sys/1-1"
    (usb / "manufacturer").write_text(manufacturer)
    block = tmp_path / "sdb2"
    block.touch()
    partition = disk / block.name
    (partition / "dev").write_text("8:18")
    block_dir = tmp_path / "sys-block"
    block_dir.mkdir()
    (block_dir / block.name).symlink_to(partition)
    uuids = tmp_path / "by-uuid"
    uuids.mkdir()
    (uuids / "ABCD-1234").symlink_to(block)
    monkeypatch.setattr(stock.hrdisksync, "SYS_BLOCK", block_dir)
    monkeypatch.setattr(stock.hrdisksync, "UUIDS", uuids)
    monkeypatch.setattr(stock.list_ports, "comports", lambda: [])
    calls = []

    def run(*args, **kwargs):
        calls.append(args[0])
        return "vfat"

    monkeypatch.setattr(stock.hrdisksync, "run_host", run)
    state = stock.current_device()
    assert state.kind == kind
    assert (state.volume.block if state.volume else None) == (block if kind != "none" else None)
    assert calls == (["lsblk"] if product == "1261" else [])


def test_podlink_tty_takes_priority_over_mass_storage(monkeypatch):
    first = SimpleNamespace(vid=0x05ac, pid=0x1261, device="/dev/ttyACM1", serial_number="2A")
    second = SimpleNamespace(vid=0x05ac, pid=0x1261, device="/dev/ttyACM2", serial_number="2B")
    monkeypatch.setattr(stock.list_ports, "comports", lambda: [first, second])
    monkeypatch.setattr(stock.hrdisksync, "current_volume", lambda **_: pytest.fail("TTY takes priority"))
    assert stock.current_device().kind == "none"
    assert stock.current_device(second.device) == stock.DeviceState(
        "podbox-podlink", port=second.device, serial=second.serial_number
    )


def test_podlink_by_id_preserves_disk_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(stock.list_ports, "comports", lambda: [])
    volume = stock.hrdisksync.Volume(tmp_path / "uuid", tmp_path / "sdb1", "8:17",
                                    (tmp_path, "1ABCD"), "PodBox")

    def find(serial=None, **_):
        if serial == "2ABCD":
            return volume
        raise RuntimeError("Several disks are attached")

    monkeypatch.setattr(stock.hrdisksync, "current_volume", find)
    assert stock.current_device().kind == "none"
    assert stock.current_device("/dev/serial/by-id/usb-PodBox_AstraPod_2ABCD-if00").volume == volume
    assert stock.current_device("/dev/ttyACM1", podlink_serial="2ABCD").volume == volume


@pytest.mark.parametrize("write", [False, True])
def test_stock_mount_permissions_and_cleanup(tmp_path, monkeypatch, write):
    calls = []
    mounted = []
    st_dev = tmp_path.stat().st_dev
    uuid_path = tmp_path / "ABCD-1234"
    uuid_path.symlink_to("/dev/sdb2")

    def eject():
        calls.append("eject")
        mounted.clear()

    volume = SimpleNamespace(
        uuid_path=uuid_path,
        block=Path("/dev/sdb2"), number=f"{stock.os.major(st_dev)}:{stock.os.minor(st_dev)}",
        manufacturer="Apple Inc.", usb=(tmp_path, "000000012345"), filesystem="vfat",
        present=lambda: True, mounts=lambda: mounted.copy(), mounted=lambda: mounted[0],
        eject=eject,
    )
    state = stock.DeviceState("stock", volume=volume)
    monkeypatch.setattr(stock.os, "statvfs", lambda _: SimpleNamespace(
        f_flag=0 if write else stock.os.ST_RDONLY,
    ))

    def run(*command, timeout):
        calls.append(command)
        if command[1] == "mount":
            mounted.append(("42", tmp_path, "/", "vfat"))
        else:
            mounted.clear()

    monkeypatch.setattr(stock.hrdisksync, "run_host", run)
    with pytest.raises(ValueError, match="operation failure"):
        with stock.stock_mount(state, write=write) as root:
            assert root == tmp_path
            raise ValueError("operation failure")
    mount = ("udisksctl", "mount", "-b", str(volume.block), "--no-user-interaction")
    unmount = ("udisksctl", "unmount", "-b", str(volume.block), "--no-user-interaction")
    if write:
        assert calls == [mount, "eject"]
        assert stock._recently_ejected(volume)
        uuid_path.unlink()
        uuid_path.symlink_to("/dev/sdb2")
        assert not stock._recently_ejected(volume)
    else:
        assert calls == [mount + ("-o", "ro"), unmount]


def test_scsi_plist_supplies_model_and_guid(tmp_path, monkeypatch):
    stock_volume(tmp_path)
    usb_descriptor(tmp_path, monkeypatch, serial="")
    monkeypatch.setattr(stock, "_probe_scsi", lambda *_args: plistlib.dumps({
        "FireWireGUID": GUID, "SerialNumber": "00000000YMX", "DBVersion": 3,
    }))
    info = stock.detect(tmp_path)
    assert (info.model_number, info.generation, info.firewire_guid) == ("MB150", "classic-6g", GUID)


def test_exact_release_from_updater_family_without_model_number(tmp_path):
    stock_volume(tmp_path, extended={"FamilyID": 11, "UpdaterFamilyID": 38, "DBVersion": 3})
    info = stock.detect(tmp_path)
    assert info.generation == "classic-7g"
    assert info.model_number is None
    assert info.checksum == 1


@pytest.mark.parametrize(("serial", "model_number"), [("000000009ZU", "MC297"), ("00000000000", None)])
def test_stale_sysinfo_model_yields_to_updater_family_and_matching_serial(tmp_path, serial, model_number):
    stock_volume(tmp_path, model="MB029", extended={
        "FamilyID": 11, "UpdaterFamilyID": 35, "SerialNumber": serial, "VisibleBuildID": "2.0.4",
    })
    info = stock.detect(tmp_path, authorize=False)
    assert info.generation == "classic-7g"
    assert info.model_number == model_number
    assert info.firmware_version == "2.0.4"


@pytest.mark.parametrize(("returncode", "authorize", "escalates"), [
    (77, True, True),
    (77, False, False),
    (2, True, False),
    (0, True, False),
])
def test_raw_sg_inquiry_escalates_only_for_permissions(monkeypatch, returncode, authorize, escalates):
    usb = stock._UsbDisk(Path("/dev/sdb"), "8:16", "05ac", "1261", "Apple", GUID, Path("/unused"))
    monkeypatch.setattr(stock.shutil, "which", lambda program, **kw: "/usr/bin/" + program)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        payload = b"plist" if returncode == 0 or escalates else b""
        return CompletedProcess(command, returncode if len(calls) == 1 else 0, payload, b"")

    monkeypatch.setattr(stock.subprocess, "run", run)
    assert stock._probe_scsi(usb, authorize) == (b"plist" if returncode == 0 or escalates else b"")
    assert calls[0][:3] == ["/usr/bin/python3", "-I", "-S"]
    assert calls[0][-3:] == ["/usr/bin/sg_inq", "/dev/sdb", "8:16"]
    assert len(calls) == 1 + escalates
    if escalates:
        assert calls[1] == ["/usr/bin/pkexec", *calls[0]]


def test_raw_sg_inquiry_cancelled_authorization(monkeypatch):
    usb = stock._UsbDisk(Path("/dev/sdb"), "8:16", "05ac", "1261", "Apple", GUID, Path("/unused"))
    monkeypatch.setattr(stock.shutil, "which", lambda program, **kw: "/usr/bin/" + program)
    monkeypatch.setattr(stock.subprocess, "run", lambda command, **kw: CompletedProcess(
        command, 126 if command[0] == "/usr/bin/pkexec" else 77, b"", b"",
    ))
    with pytest.raises(stock.StockProbeError, match="Could not authorize"):
        stock._probe_scsi(usb, True)
