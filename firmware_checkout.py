"""Locate the sibling AstraPod checkouts used by native helpers and contract tests."""
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


def find_cider_plugin(path=None):
    """Return an AstraPod-Cider checkout, using ASTRAPOD_CIDER_PLUGIN when set."""
    explicit = path or os.environ.get('ASTRAPOD_CIDER_PLUGIN')
    root = Path(explicit) if explicit else Path(__file__).resolve().parent.parent / 'AstraPod-Cider'
    root = root.expanduser().resolve()
    return root if (root / 'core.mjs').is_file() and (root / 'mock-host.mjs').is_file() else None


MISSING_CIDER_PLUGIN = 'AstraPod-Cider checkout missing. Set ASTRAPOD_CIDER_PLUGIN to its path.'
