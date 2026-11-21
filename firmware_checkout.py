"""Locate the AstraPod sources used by native helpers and contract tests."""
import os
from pathlib import Path


def find_firmware(path=None):
    """Return a source checkout, using ASTRAPOD_FIRMWARE when set."""
    explicit = path or os.environ.get('ASTRAPOD_FIRMWARE')
    sibling = Path(__file__).resolve().parent.parent
    candidates = [Path(explicit)] if explicit else [sibling / 'AstraPod', sibling / 'podbox']
    for candidate in candidates:
        root = candidate.expanduser().resolve()
        if (root / 'apps-ipod/SOURCES').is_file() and (root / 'firmware/SOURCES').is_file():
            return root
    return None


MISSING_FIRMWARE = 'AstraPod firmware checkout missing. Set ASTRAPOD_FIRMWARE to its path.'
