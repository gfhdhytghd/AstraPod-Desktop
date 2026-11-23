"""Import the iPod Classic preview font from a local Apple restore image."""
from __future__ import annotations

import argparse
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile

from .backends import cache_root

FONT_NAME = "HelveticaBold.ttf"
# The same font bytes appear in the 6G, 6.5G and 7G restore images.
FONT_SHA256 = "ec0d7994fbab2ed50ecb20de6c664cad158a17713c3d4de8a0e3e4f56743a08c"
FIRMWARE_MEMBER = re.compile(r"Firmware-(24|33|35|38)\.9\.\d+(?:\.\d+)?")
FIRMWARE_BLOCK = 4096
DIRECTORY_ENTRY = struct.Struct("<4s4s8I")


def font_path() -> Path:
    return cache_root() / "stock-classic" / FONT_NAME


def is_stock_font(data: bytes) -> bool:
    return sha256(data).hexdigest() == FONT_SHA256


def _resource_volume(firmware: bytes) -> memoryview:
    """Locate the plaintext FAT16 volume in the RSRC directory entry."""
    if len(firmware) < FIRMWARE_BLOCK or firmware[0x100:0x104] != b"]ih[":
        raise ValueError("Missing iPod firmware directory")
    directory = struct.unpack_from("<I", firmware, 0x104)[0] + FIRMWARE_BLOCK
    # The firmware directory occupies one block. Entries end at the first
    # non-ATA record. Positions in each entry exclude the initial block.
    for offset in range(directory, min(directory + FIRMWARE_BLOCK, len(firmware))
                        - DIRECTORY_ENTRY.size + 1, DIRECTORY_ENTRY.size):
        device, kind, _, start, size, _, entry, *_ = DIRECTORY_ENTRY.unpack_from(firmware, offset)
        if device != b"!ATA":
            break
        if kind != b"crsr":
            continue
        boot = FIRMWARE_BLOCK + start + entry
        end = boot + size
        if end <= len(firmware) and firmware[boot + 43:boot + 62] == b"IPODRESOURCFAT16   ":
            return memoryview(firmware)[boot:end]
        break
    raise ValueError("Missing iPod Classic FAT16 resource volume")


def import_font(ipsw: Path) -> Path:
    extractor = shutil.which("7zz") or shutil.which("7z")
    if extractor is None:
        raise ValueError("Install 7-Zip (7zz or 7z) to import a Classic IPSW")
    with ZipFile(ipsw) as restore:
        members = [item for item in restore.infolist() if FIRMWARE_MEMBER.fullmatch(item.filename)]
        if len(members) != 1 or not 0 < members[0].file_size <= 100 * 1024 * 1024:
            raise ValueError("Expected one iPod Classic (24, 33, 35 or 38) firmware image")
        volume = _resource_volume(restore.read(members[0]))

    target = font_path()
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with TemporaryDirectory(dir=target.parent) as scratch:
        image = Path(scratch) / "resources.fat"
        image.write_bytes(volume)
        result = subprocess.run(
            [extractor, "x", "-so", str(image), f"Resources/Fonts/{FONT_NAME}"],
            check=True, capture_output=True, timeout=30,
        )
        if not is_stock_font(result.stdout):
            raise ValueError("The resource does not match the verified Classic Helvetica Bold font")
        cached = Path(scratch) / FONT_NAME
        cached.write_bytes(result.stdout)
        cached.chmod(0o600)
        os.replace(cached, target)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ipsw", type=Path, help="path to a local iPod Classic IPSW")
    args = parser.parse_args()
    try:
        print(import_font(args.ipsw))
    except (BadZipFile, OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
