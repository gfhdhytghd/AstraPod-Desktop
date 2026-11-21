"""Paths shared by the standalone firmware contract probes."""
from pathlib import Path
import sys

DESKTOP = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(DESKTOP), str(DESKTOP / 'herdremote')]

from firmware_checkout import MISSING_FIRMWARE, find_firmware


def require_firmware(path=None):
    root = find_firmware(path)
    if root is None:
        print(f'SKIP: {MISSING_FIRMWARE}')
        sys.exit(0)
    return root
