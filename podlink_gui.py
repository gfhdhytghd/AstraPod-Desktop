#!/usr/bin/env python3
"""AstraPod desktop entry point; the application is tools/astrapod_desktop/."""
from __future__ import annotations

from pathlib import Path
import sys

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from astrapod_desktop.app import create_application, demo_services, main, real_services  # noqa: E402,F401
from astrapod_desktop.runtime import DesktopRuntime  # noqa: E402,F401
from astrapod_desktop.window import MainWindow, Services  # noqa: E402,F401

if __name__ == "__main__":
    sys.exit(main())
