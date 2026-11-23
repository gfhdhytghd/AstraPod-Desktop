from pathlib import Path
import plistlib
from subprocess import CompletedProcess

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
    <key>SerialNumber</key><string>00000000Y5N</string>
    <key>UpdaterFamilyID</key><integer>24</integer>
    </dict></plist>"""
    _, _, device = stock_volume(tmp_path, extended=data)
    info = stock.detect(tmp_path)
    assert info.generation == "classic-6g"
    assert info.model_number == "MB029"
    assert plistlib.loads(info.sysinfo_extended)["UpdaterFamilyID"] == 24
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
