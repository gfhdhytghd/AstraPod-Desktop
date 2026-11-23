"""Identify an Apple stock-firmware iPod Classic from an explicitly selected volume.

Detection reads the iPod layout and device metadata. Linux can also associate
the volume's filesystem device number with its USB disk in sysfs. Classic models share a USB product ID, which cannot identify the exact
generation. A raw SCSI inquiry is read-only,
and uses polkit only if the unprivileged inquiry fails for lack of permission.
Passive enumeration may disable interactive authorization.

References: libgpod's itdb_device.c and tools/ipod-scsi.c, the USB ID
Repository, and the iPod SysInfoExtended captures at dstaley/ipod-sysinfo.
"""
from dataclasses import dataclass, field
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
from xml.etree import ElementTree
from xml.parsers.expat import ExpatError


SYS_DEV_BLOCK = Path("/sys/dev/block")
MOUNTINFO = Path("/proc/self/mountinfo")
MAX_SYSINFO = 256 * 1024
MODEL_NUMBERS = {
    "classic-6g": ("MB029", "MB145", "MB147", "MB150"),
    "classic-6.5g": ("MB562", "MB565"),
    "classic-7g": ("MC293", "MC297"),
}
_GENERATION_BY_MODEL = {number: generation for generation, numbers in MODEL_NUMBERS.items()
                        for number in numbers}
SERIAL_MODELS = {
    "Y5N": "MB029", "YMU": "MB145",
    "YMV": "MB147", "YMX": "MB150", "2C5": "MB562", "2C7": "MB565",
    "9ZS": "MC293", "9ZU": "MC297",
}
UPDATER_GENERATIONS = {
    24: "classic-6g", 33: "classic-6.5g", 35: "classic-7g", 38: "classic-7g",
}
CLASSIC_USB_PRODUCT = "1261"
DISPLAY_MODELS = {
    "classic-6g": "iPod Classic 6G",
    "classic-6.5g": "iPod Classic 6.5G",
    "classic-7g": "iPod Classic 7G",
    "classic": "iPod Classic",
}


class StockLayoutError(RuntimeError):
    """An iPod path is unsafe for stock-firmware access."""


class UnsupportedStockModel(RuntimeError):
    """The detected stock iPod is outside the supported Classic family."""


def _unsupported_model():
    return UnsupportedStockModel("Unsupported stock iPod model. Only iPod Classic 6G, 6.5G and 7G are supported.")


class StockProbeError(RuntimeError):
    """The user could not authorize the read-only identity probe."""


@dataclass(frozen=True)
class StockDevice:
    root: Path
    control: Path
    tunes: Path
    database: Path
    device_dir: Path | None
    model: str
    model_number: str | None
    generation: str | None
    firewire_guid: str | None = field(repr=False)
    db_version: int | None
    sqlite: bool
    checksum: int | None
    sysinfo_extended: bytes | None = field(repr=False)


@dataclass(frozen=True)
class _UsbDisk:
    disk: Path | None
    number: str
    vendor: str
    product: str
    manufacturer: str
    serial: str = field(repr=False)
    cached_vpd: Path


def _child(parent, name):
    # Use native lookup first (FAT handles casing), then support extracted
    # volumes on case-sensitive filesystems.
    child = parent / name
    try:
        return child, child.lstat()
    except FileNotFoundError:
        matches = [item for item in parent.iterdir() if item.name.casefold() == name.casefold()]
        if len(matches) > 1:
            raise StockLayoutError(f"Ambiguous iPod path: {child}")
        if matches:
            return matches[0], matches[0].lstat()
    return child, None


def checked_path(root, *parts, directory=False, required=False):
    """Find a same-filesystem path with the given components and expected type.

    Return None for a missing path unless required=True. Reject traversal,
    symlinks and special files. The caller may supply its pinned volume root.
    """
    current = Path(root)
    device = current.stat().st_dev
    for index, name in enumerate(parts):
        if (not isinstance(name, str) or not name or name in {".", ".."}
                or any(char in name for char in ("/", "\\", "\x00"))):
            raise StockLayoutError(f"Invalid iPod path component: {name!r}")
        current, info = _child(current, name)
        if info is None:
            if required:
                raise FileNotFoundError(current)
            return None
        mode_check = stat.S_ISDIR if directory or index + 1 < len(parts) else stat.S_ISREG
        if info.st_dev != device or not mode_check(info.st_mode):
            raise StockLayoutError(f"Unsafe iPod path: {current}")
    return current


def assert_stock_layout(root):
    """Reject a volume with Rockbox or PodLink markers, including broken links."""
    root = Path(root)
    if _child(root, ".rockbox")[1] is not None:
        raise StockLayoutError("Rockbox volume cannot be accessed as a stock iPod")
    music = checked_path(root, "Music", directory=True)
    if music is not None and _child(music, "PodLink")[1] is not None:
        raise StockLayoutError("PodLink volume cannot be accessed as a stock iPod")


def _read_text(path):
    try:
        return path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return ""


def _usb_disk(device):
    number = f"{os.major(device)}:{os.minor(device)}"
    try:
        sys_device = (SYS_DEV_BLOCK / number).resolve(strict=True)
    except OSError:
        return None
    disk = sys_device.parent if (sys_device / "partition").exists() else sys_device
    for parent in disk.parents:
        vendor = _read_text(parent / "idVendor").lower()
        if vendor:
            block = Path("/dev") / disk.name if re.fullmatch(r"sd[a-z]+", disk.name) else None
            return _UsbDisk(block, _read_text(disk / "dev"), vendor,
                            _read_text(parent / "idProduct").lower(),
                            _read_text(parent / "manufacturer"), _read_text(parent / "serial"),
                            disk / "device" / "vpd_pg80")
    return None


def mounted_apple_volumes():
    """Return mount IDs and paths for mounted Apple USB filesystem roots."""
    if not sys.platform.startswith("linux"):
        return ()
    volumes = []
    for line in MOUNTINFO.read_text().splitlines():
        before, _, after = line.partition(" - ")
        mount_id, _, device, root, path, *_ = before.split()
        if root != "/" or after.split()[0] not in ("vfat", "hfsplus"):
            continue
        major, minor = map(int, device.split(":"))
        usb = _usb_disk(os.makedev(major, minor))
        if usb and usb.vendor == "05ac":
            path = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), path)
            volumes.append((mount_id, Path(path)))
    return tuple(volumes)


def _guid(value):
    if type(value) is int and 0 < value < 2**64:
        return f"{value:016x}"
    if isinstance(value, str):
        value = value.strip().lower().removeprefix("0x")
        if re.fullmatch(r"[0-9a-f]{16}", value) and int(value, 16):
            return value
    return None


def _number(value):
    if type(value) is int:
        return value
    if isinstance(value, str):
        match = re.match(r"\s*(0[xX][0-9a-fA-F]+|[0-9]+)\b", value)
        if match and len(match[1]) <= 18:
            digits = match[1]
            return int(digits, 16 if digits.lower().startswith("0x") else 10)
    return None


def _read_metadata(path):
    if path is None:
        return b""
    with path.open("rb") as file:
        data = file.read(MAX_SYSINFO + 1)
    return data if len(data) <= MAX_SYSINFO else b""


def _basic_metadata(path):
    data = _read_metadata(path)
    props = {}
    for line in data.decode("ascii", "replace").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            props[key.strip().lower()] = value.strip()
    return props


def _extended_metadata(data):
    if not data:
        return {}, None
    try:
        props = plistlib.loads(data)
    except (plistlib.InvalidFileException, ValueError, TypeError, OverflowError, ExpatError,
            RecursionError):
        # Early iPods put keyed entries in an array, which plistlib rejects.
        # A few simple top-level properties still identify the device.
        try:
            root = ElementTree.fromstring(data)
        except ElementTree.ParseError:
            return {}, None
        top = root.find("dict")
        elements = list(top) if top is not None else []
        props = {}
        for key, value in zip(elements, elements[1:]):
            if key.tag != "key" or not key.text:
                continue
            if value.tag == "integer" and _number(value.text) is not None:
                props[key.text] = _number(value.text)
            elif value.tag == "string" and value.text is not None:
                props[key.text] = value.text
            elif value.tag in {"true", "false"}:
                props[key.text] = value.tag == "true"
    if not isinstance(props, dict):
        return {}, None
    # Use a well-formed XML plist in staging, including when input is binary
    # or has the irregular arrays above. Do not rewrite the mounted copy.
    try:
        normalized = plistlib.dumps(props, sort_keys=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return {}, None
    return {key.lower(): value for key, value in props.items()}, normalized


def _model_number(props):
    for key in ("modelnumstr", "modelnumber", "producttype"):
        value = props.get(key)
        if isinstance(value, str):
            match = re.fullmatch(r"M?([ABC]\d{3})(?:[A-Z]+(?:/A)?)?", value.strip().upper())
            if match:
                return "M" + match[1]
    for key in ("serialnumber", "pszserialnumber"):
        value = props.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Z0-9]{11,12}", value.strip().upper()):
            number = SERIAL_MODELS.get(value.strip().upper()[-3:])
            if number:
                return number
    return None


def _model_generation(model_number, props):
    updater = _number(props.get("updaterfamilyid", props.get("updaterfamily")))
    family = _number(props.get("familyid", props.get("ipodfamily")))
    if ((model_number is not None and model_number not in _GENERATION_BY_MODEL)
            or (updater is not None and updater not in UPDATER_GENERATIONS)
            or (family is not None and family != 11)):
        raise _unsupported_model()
    return (_GENERATION_BY_MODEL.get(model_number) or UPDATER_GENERATIONS.get(updater)
            or ("classic" if family == 11 else None))


def _path_checksum(path):
    if path is None:
        return None
    with path.open("rb") as file:
        header = file.read(0x32)
    byteorder = {b"mhbd": "little", b"dbhm": "big"}.get(header[:4])
    if byteorder and len(header) == 0x32 and int.from_bytes(header[4:8], byteorder) >= 0x32:
        return int.from_bytes(header[0x30:0x32], byteorder)
    return None


def _vendor_payload(response, page):
    if len(response) < 4 or response[1] != page:
        return b""
    length = int.from_bytes(response[2:4], "big")
    if length > len(response) - 4:
        return b""
    return response[4:4 + length]


# Run a bounded set of read-only INQUIRY calls in one process, so polkit
# presents one prompt for one probe. Pass only the disk and its expected
# device number to this fixed program. System executables use a trusted PATH.
_SCSI_INQUIRY = r"""
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

sg_inq, disk, expected = sys.argv[1:]
if not re.fullmatch(r"/dev/sd[a-z]+", disk):
    sys.exit(2)
def page(fd, code):
    result = subprocess.run(
        [sg_inq, "--force", "--raw", "--len=252", f"--page=0x{code:02x}", f"/proc/self/fd/{fd}"],
        capture_output=True, timeout=3, env={"LC_ALL": "C"}, pass_fds=(fd,),
    )
    if result.returncode:
        message = result.stderr.lower()
        if b"permission denied" in message or b"operation not permitted" in message:
            sys.exit(77)
        return b""
    response = result.stdout
    if len(response) < 4 or response[1] != code:
        return b""
    length = int.from_bytes(response[2:4], "big")
    return response[4:4 + length] if length <= len(response) - 4 else b""

try:
    sysdev = (Path("/sys/dev/block") / expected).resolve(strict=True)
    if sysdev.name != Path(disk).name or not any(
        (parent / "idVendor").is_file()
        and (parent / "idVendor").read_text().strip() == "05ac"
        and (parent / "idProduct").read_text().strip() == "1261"
        and not (parent / "manufacturer").read_text().strip().lower().startswith(("podbox", "rockbox"))
        and not re.fullmatch(r".AA0000000000360", (parent / "serial").read_text().strip(), re.I)
        for parent in sysdev.parents
    ):
        sys.exit(2)
    fd = os.open(disk, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(fd)
    if not stat.S_ISBLK(info.st_mode) or f"{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}" != expected:
        sys.exit(2)
    offsets = page(fd, 0xc0).partition(b"\0")[0]
    if offsets and len(set(offsets)) == len(offsets) and all(code >= 0xc1 for code in offsets):
        chunks = [page(fd, code) for code in offsets]
        if all(chunks):
            sys.stdout.buffer.write(b"".join(chunks).rstrip(b"\0"))
except PermissionError:
    sys.exit(77)
except (OSError, subprocess.SubprocessError):
    sys.exit(2)
"""


def _probe_scsi(usb, authorize):
    if usb.disk is None or not re.fullmatch(r"\d+:\d+", usb.number):
        return b""
    sg_inq = shutil.which("sg_inq", path=os.defpath)
    python = shutil.which("python3", path=os.defpath)
    if not sg_inq or not python:
        return b""
    command = [python, "-I", "-S", "-c", _SCSI_INQUIRY, sg_inq, str(usb.disk), usb.number]
    try:
        result = subprocess.run(command, capture_output=True, timeout=12)
        if result.returncode == 77 and authorize:
            pkexec = shutil.which("pkexec", path=os.defpath)
            if not pkexec:
                raise StockProbeError("Reading the iPod identity requires pkexec")
            result = subprocess.run([pkexec, *command], capture_output=True, timeout=120)
            if result.returncode:
                raise StockProbeError("Could not authorize the read-only iPod identity probe")
    except subprocess.TimeoutExpired as error:
        if authorize:
            raise StockProbeError("Timed out reading the iPod identity") from error
        return b""
    return result.stdout if result.returncode == 0 and len(result.stdout) <= MAX_SYSINFO else b""


def detect(volume_root, *, authorize=True):
    """Identify a stock Classic, return None for other layouts, or reject an unsupported iPod."""
    try:
        root = Path(volume_root).resolve(strict=True)
        if not root.is_dir():
            return None
        assert_stock_layout(root)
        control = checked_path(root, "iPod_Control", directory=True)
        if control is None:
            return None
        tunes = checked_path(control, "iTunes", directory=True)
        if tunes is None:
            return None
        database = checked_path(tunes, "iTunesDB")
        compressed = checked_path(tunes, "iTunesCDB")
        if database is None and compressed is None:
            return None
        device_dir = checked_path(control, "Device", directory=True)
        basic_path = checked_path(device_dir, "SysInfo") if device_dir else None
        extended_path = checked_path(device_dir, "SysInfoExtended") if device_dir else None
        basic = _basic_metadata(basic_path)
        props, extended = _extended_metadata(_read_metadata(extended_path))
        header_checksum = _path_checksum(compressed or database)
    except (FileNotFoundError, StockLayoutError):
        return None

    usb = _usb_disk(root.stat().st_dev) if sys.platform.startswith("linux") else None
    if usb and (usb.vendor != "05ac"
                or usb.manufacturer.casefold().startswith(("podbox", "rockbox"))
                or re.fullmatch(r".AA0000000000360", usb.serial, re.IGNORECASE)):
        return None

    if usb and usb.product != CLASSIC_USB_PRODUCT:
        raise _unsupported_model()

    metadata = basic | props
    model_number = _model_number(metadata)
    generation = _model_generation(model_number, metadata)
    guid = _guid(metadata.get("firewireguid"))
    if usb:
        try:
            payload = _vendor_payload(usb.cached_vpd.read_bytes(), 0x80).decode("ascii").strip("\x00 \r\n")
        except (OSError, UnicodeError):
            payload = ""
        guid = guid or _guid(usb.serial) or _guid(payload)
        model_number = (model_number or _model_number({"serialnumber": usb.serial})
                        or _model_number({"serialnumber": payload}))
        generation = _model_generation(model_number, metadata)
        if not guid or not model_number or (compressed is not None and not props):
            probe_props, probed = _extended_metadata(_probe_scsi(usb, authorize))
            if probe_props:
                props = props | probe_props
                extended = (plistlib.dumps(plistlib.loads(extended) | plistlib.loads(probed))
                            if extended else probed)
                metadata = basic | props
                model_number = _model_number(metadata) or model_number
                generation = _model_generation(model_number, metadata) or generation
                guid = _guid(metadata.get("firewireguid")) or guid
        generation = generation or "classic"
    if generation is None:
        raise _unsupported_model()

    db_version = _number(metadata.get("dbversion"))
    sqlite = (compressed is not None or metadata.get("sqlitedb") is True
              or str(metadata.get("sqlitedb", "")).lower() in {"true", "1"})
    checksum = (header_checksum if header_checksum in {1, 2, 3}
                else {4: 2, 5: 3}.get(db_version, 1))
    return StockDevice(root, control, tunes, compressed or database, device_dir,
                       DISPLAY_MODELS[generation],
                       model_number, generation, guid, db_version, sqlite, checksum,
                       extended)
