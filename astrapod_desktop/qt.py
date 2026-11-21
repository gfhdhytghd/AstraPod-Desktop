"""Qt binding and import paths shared by the package."""
from __future__ import annotations

from pathlib import Path
import sys

TOOLS_DIR = Path(__file__).resolve().parents[1]
for _extra in (str(TOOLS_DIR), str(TOOLS_DIR / "herdremote")):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

from podlink_ui_theme import (  # noqa: E402
    QT_BINDING,
    FONT_DISPLAY,
    FONT_NUMERIC,
    FONT_UI,
    QtCore,
    QtGui,
    QtWidgets,
    Signal,
    ThemeManager,
    ThemeTokens,
    qcolor,
    tokens_for,
)

if QT_BINDING == "PySide6":
    from PySide6 import QtNetwork, QtSvg  # noqa: E402
else:
    from PyQt6 import QtNetwork, QtSvg  # noqa: E402

Qt = QtCore.Qt

__all__ = [
    "FONT_DISPLAY", "FONT_NUMERIC", "FONT_UI", "Qt", "QtCore", "QtGui",
    "QtNetwork", "QtSvg", "QtWidgets", "Signal", "ThemeManager", "ThemeTokens", "TOOLS_DIR",
    "qcolor", "tokens_for",
]
