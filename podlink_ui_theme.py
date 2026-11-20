"""Qt binding loader, adaptive Light/Dark/System theming, and UI preference storage."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import podlink_settings

try:
    from PySide6 import QtCore, QtGui, QtWidgets

    Signal = QtCore.Signal
    Slot = QtCore.Slot
    QT_BINDING = "PySide6"
except ImportError:
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets

        Signal = QtCore.pyqtSignal
        Slot = QtCore.pyqtSlot
        QT_BINDING = "PyQt6"
    except ImportError as exc:
        raise SystemExit(
            "AstraPod 桌面界面需要 PySide6（推荐）或 PyQt6。"
            "请先在当前 Python 环境中安装 PySide6 或 PyQt6。"
        ) from exc

THEME_MODES = ("system", "light", "dark")
THEME_LABELS = {
    "system": "随系统",
    "light": "浅色",
    "dark": "深色",
}
RESOLVED_LABELS = {
    "light": "浅色模式",
    "dark": "深色模式",
}


@dataclass(frozen=True)
class ThemeTokens:
    name: str
    bg_window: str
    bg_header: str
    bg_card: str
    bg_subtle: str
    bg_input: str
    bg_input_disabled: str
    bg_code: str
    border: str
    border_strong: str
    border_focus: str
    text_primary: str
    text_secondary: str
    text_muted: str
    text_disabled: str
    text_on_accent: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_soft: str
    accent_text: str
    success: str
    success_bg: str
    success_border: str
    danger: str
    danger_hover: str
    danger_pressed: str
    danger_bg: str
    danger_border: str
    warning: str
    warning_bg: str
    warning_border: str
    scrollbar_handle: str
    scrollbar_hover: str


LIGHT_TOKENS = ThemeTokens(
    name="light",
    bg_window="#F4F6F9",
    bg_header="#FFFFFF",
    bg_card="#FFFFFF",
    bg_subtle="#ECEFF4",
    bg_input="#FFFFFF",
    bg_input_disabled="#F3F4F6",
    bg_code="#F8FAFC",
    border="#E2E8F0",
    border_strong="#CBD5E1",
    border_focus="#2563EB",
    text_primary="#0F172A",
    text_secondary="#334155",
    text_muted="#64748B",
    text_disabled="#94A3B8",
    text_on_accent="#FFFFFF",
    accent="#2563EB",
    accent_hover="#1D4ED8",
    accent_pressed="#1E40AF",
    accent_soft="#EFF6FF",
    accent_text="#1D4ED8",
    success="#15803D",
    success_bg="#ECFDF5",
    success_border="#BBF7D0",
    danger="#DC2626",
    danger_hover="#B91C1C",
    danger_pressed="#991B1B",
    danger_bg="#FEF2F2",
    danger_border="#FECACA",
    warning="#B45309",
    warning_bg="#FFFBEB",
    warning_border="#FDE68A",
    scrollbar_handle="#CBD5E1",
    scrollbar_hover="#94A3B8",
)

DARK_TOKENS = ThemeTokens(
    name="dark",
    bg_window="#0F1218",
    bg_header="#151922",
    bg_card="#171C26",
    bg_subtle="#202634",
    bg_input="#11151D",
    bg_input_disabled="#161B24",
    bg_code="#10141C",
    border="#262E3D",
    border_strong="#364154",
    border_focus="#60A5FA",
    text_primary="#F1F5F9",
    text_secondary="#CBD5E1",
    text_muted="#8B98AD",
    text_disabled="#5B667A",
    text_on_accent="#FFFFFF",
    accent="#3B82F6",
    accent_hover="#60A5FA",
    accent_pressed="#2563EB",
    accent_soft="#172554",
    accent_text="#93C5FD",
    success="#4ADE80",
    success_bg="#0B2A1A",
    success_border="#166534",
    danger="#F87171",
    danger_hover="#EF4444",
    danger_pressed="#DC2626",
    danger_bg="#351419",
    danger_border="#7F1D1D",
    warning="#FBBF24",
    warning_bg="#33240B",
    warning_border="#78350F",
    scrollbar_handle="#364154",
    scrollbar_hover="#4B5870",
)


def tokens_for(resolved: str) -> ThemeTokens:
    return DARK_TOKENS if resolved == "dark" else LIGHT_TOKENS


def ui_prefs_path() -> Path:
    return podlink_settings.settings_path().with_name("desktop-ui.json")


def load_theme_preference(path: Path | None = None) -> str:
    target = path or ui_prefs_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "system"
    if not isinstance(payload, dict):
        return "system"
    mode = payload.get("theme")
    return mode if mode in THEME_MODES else "system"


def save_theme_preference(mode: str, path: Path | None = None) -> None:
    if mode not in THEME_MODES:
        raise ValueError(f"unsupported theme mode: {mode}")
    target = path or ui_prefs_path()
    payload: dict[str, object] = {}
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            payload.update(existing)
    except (OSError, ValueError):
        pass
    payload["theme"] = mode
    podlink_settings.write_private(target, json.dumps(payload, ensure_ascii=False, indent=2))


def detect_system_theme(app: QtWidgets.QApplication,
                        fallback_palette: QtGui.QPalette | None = None) -> str:
    color_scheme_enum = getattr(QtCore.Qt, "ColorScheme", None)
    hints = app.styleHints() if app is not None else None
    if color_scheme_enum is not None and hints is not None and hasattr(hints, "colorScheme"):
        scheme = hints.colorScheme()
        if scheme == color_scheme_enum.Dark:
            return "dark"
        if scheme == color_scheme_enum.Light:
            return "light"
    palette = fallback_palette if fallback_palette is not None else app.palette()
    window_color = palette.color(QtGui.QPalette.ColorRole.Window)
    return "dark" if window_color.lightnessF() < 0.5 else "light"


def resolve_theme(mode: str, app: QtWidgets.QApplication,
                  fallback_palette: QtGui.QPalette | None = None) -> str:
    if mode in ("light", "dark"):
        return mode
    return detect_system_theme(app, fallback_palette)


def build_palette(tokens: ThemeTokens) -> QtGui.QPalette:
    palette = QtGui.QPalette()
    Role = QtGui.QPalette.ColorRole
    Group = QtGui.QPalette.ColorGroup

    mapping = {
        Role.Window: tokens.bg_window,
        Role.WindowText: tokens.text_primary,
        Role.Base: tokens.bg_input,
        Role.AlternateBase: tokens.bg_subtle,
        Role.ToolTipBase: tokens.bg_card,
        Role.ToolTipText: tokens.text_primary,
        Role.Text: tokens.text_primary,
        Role.Button: tokens.bg_card,
        Role.ButtonText: tokens.text_primary,
        Role.BrightText: tokens.text_on_accent,
        Role.Highlight: tokens.accent,
        Role.HighlightedText: tokens.text_on_accent,
        Role.Link: tokens.accent_text,
    }
    for role, color in mapping.items():
        qcolor = QtGui.QColor(color)
        palette.setColor(Group.Active, role, qcolor)
        palette.setColor(Group.Inactive, role, qcolor)

    if hasattr(Role, "PlaceholderText"):
        placeholder = QtGui.QColor(tokens.text_muted)
        palette.setColor(Group.Active, Role.PlaceholderText, placeholder)
        palette.setColor(Group.Inactive, Role.PlaceholderText, placeholder)
        palette.setColor(Group.Disabled, Role.PlaceholderText, QtGui.QColor(tokens.text_disabled))

    disabled_text = QtGui.QColor(tokens.text_disabled)
    disabled_bg = QtGui.QColor(tokens.bg_input_disabled)
    palette.setColor(Group.Disabled, Role.WindowText, disabled_text)
    palette.setColor(Group.Disabled, Role.Text, disabled_text)
    palette.setColor(Group.Disabled, Role.ButtonText, disabled_text)
    palette.setColor(Group.Disabled, Role.Base, disabled_bg)
    palette.setColor(Group.Disabled, Role.Button, disabled_bg)
    return palette


def build_stylesheet(t: ThemeTokens) -> str:
    return f"""
QMainWindow, QDialog {{
    background-color: {t.bg_window};
    color: {t.text_primary};
}}

QWidget {{
    color: {t.text_primary};
    font-size: 13px;
}}

QFrame#HeaderBar {{
    background-color: {t.bg_header};
    border-bottom: 1px solid {t.border};
}}

QLabel#BrandTitle {{
    font-size: 15px;
    font-weight: 700;
    color: {t.text_primary};
}}

QLabel#BrandSubtitle {{
    font-size: 11px;
    font-weight: 600;
    color: {t.text_muted};
    background-color: {t.bg_subtle};
    border: 1px solid {t.border};
    border-radius: 5px;
    padding: 2px 7px;
}}

QFrame[segmented="true"] {{
    background-color: {t.bg_subtle};
    border: 1px solid {t.border};
    border-radius: 9px;
    padding: 2px;
}}

QPushButton[segment="true"] {{
    background-color: transparent;
    color: {t.text_secondary};
    border: none;
    border-radius: 7px;
    padding: 5px 12px;
    font-weight: 500;
}}

QPushButton[segment="true"]:hover {{
    color: {t.text_primary};
    background-color: {t.bg_card};
}}

QPushButton[segment="true"]:checked {{
    background-color: {t.bg_card};
    color: {t.text_primary};
    font-weight: 600;
    border: 1px solid {t.border};
}}

QFrame[card="true"] {{
    background-color: {t.bg_card};
    border: 1px solid {t.border};
    border-radius: 12px;
}}

QLabel[role="card-title"] {{
    font-size: 14px;
    font-weight: 600;
    color: {t.text_primary};
}}

QLabel[role="section-subtitle"] {{
    font-size: 12px;
    font-weight: 600;
    color: {t.text_secondary};
}}

QLabel[role="field-label"] {{
    font-size: 13px;
    color: {t.text_secondary};
}}

QLabel[role="helper"] {{
    font-size: 12px;
    color: {t.text_muted};
}}

QLabel[badge="true"] {{
    font-size: 12px;
    font-weight: 600;
    border-radius: 10px;
    padding: 2px 10px;
}}

QLabel[badge="true"][tone="neutral"] {{
    background-color: {t.bg_subtle};
    color: {t.text_secondary};
    border: 1px solid {t.border};
}}

QLabel[badge="true"][tone="success"] {{
    background-color: {t.success_bg};
    color: {t.success};
    border: 1px solid {t.success_border};
}}

QLabel[badge="true"][tone="danger"] {{
    background-color: {t.danger_bg};
    color: {t.danger};
    border: 1px solid {t.danger_border};
}}

QLabel[badge="true"][tone="busy"] {{
    background-color: {t.warning_bg};
    color: {t.warning};
    border: 1px solid {t.warning_border};
}}

QLabel[badge="true"][tone="accent"] {{
    background-color: {t.accent_soft};
    color: {t.accent_text};
    border: 1px solid {t.border_focus};
}}

QLineEdit, QComboBox {{
    background-color: {t.bg_input};
    color: {t.text_primary};
    border: 1px solid {t.border_strong};
    border-radius: 8px;
    padding: 7px 11px;
    min-height: 20px;
    selection-background-color: {t.accent};
    selection-color: {t.text_on_accent};
}}

QLineEdit:hover, QComboBox:hover {{
    border-color: {t.text_muted};
}}

QLineEdit:focus, QComboBox:focus {{
    border: 1.5px solid {t.border_focus};
    padding: 6.5px 10.5px;
}}

QLineEdit:disabled, QComboBox:disabled {{
    background-color: {t.bg_input_disabled};
    color: {t.text_disabled};
    border-color: {t.border};
}}

QComboBox {{
    padding-right: 32px;
}}

QComboBox QLineEdit {{
    border: none;
    padding: 0px;
    margin: 0px;
    background: transparent;
    min-height: 0px;
}}

QComboBox::drop-down {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 28px;
    border-left: 1px solid {t.border};
    border-top-right-radius: 8px;
    border-bottom-right-radius: 8px;
    background: transparent;
}}

QComboBox::down-arrow {{
    image: none;
    width: 0px;
    height: 0px;
}}

QComboBox QAbstractItemView {{
    background-color: {t.bg_card};
    color: {t.text_primary};
    border: 1px solid {t.border_strong};
    border-radius: 8px;
    padding: 4px;
    selection-background-color: {t.accent};
    selection-color: {t.text_on_accent};
    outline: 0;
}}

QPlainTextEdit#DiffPreview {{
    background-color: {t.bg_code};
    color: {t.text_primary};
    border: 1px solid {t.border};
    border-radius: 9px;
    padding: 2px;
    selection-background-color: {t.accent};
    selection-color: {t.text_on_accent};
}}

QPushButton {{
    background-color: {t.bg_card};
    color: {t.text_primary};
    border: 1px solid {t.border_strong};
    border-radius: 8px;
    padding: 7px 16px;
    font-weight: 600;
    min-height: 20px;
}}

QPushButton:hover {{
    background-color: {t.bg_subtle};
    border-color: {t.text_muted};
}}

QPushButton:pressed {{
    background-color: {t.border};
}}

QPushButton:disabled {{
    background-color: {t.bg_input_disabled};
    color: {t.text_disabled};
    border-color: {t.border};
}}

QPushButton[variant="primary"] {{
    background-color: {t.accent};
    color: {t.text_on_accent};
    border: 1px solid {t.accent};
}}

QPushButton[variant="primary"]:hover {{
    background-color: {t.accent_hover};
    border-color: {t.accent_hover};
}}

QPushButton[variant="primary"]:pressed {{
    background-color: {t.accent_pressed};
    border-color: {t.accent_pressed};
}}

QPushButton[variant="primary"]:disabled {{
    background-color: {t.bg_subtle};
    color: {t.text_disabled};
    border-color: {t.border};
}}

QPushButton[variant="danger"] {{
    background-color: {t.danger};
    color: {t.text_on_accent};
    border: 1px solid {t.danger};
}}

QPushButton[variant="danger"]:hover {{
    background-color: {t.danger_hover};
    border-color: {t.danger_hover};
}}

QPushButton[variant="danger"]:pressed {{
    background-color: {t.danger_pressed};
    border-color: {t.danger_pressed};
}}

QFrame#FooterBar {{
    background-color: {t.bg_card};
    border: 1px solid {t.border};
    border-radius: 12px;
}}

QFrame#Divider {{
    background-color: {t.border};
    max-height: 1px;
    min-height: 1px;
}}

QProgressBar {{
    background-color: {t.bg_subtle};
    border: none;
    border-radius: 3px;
    max-height: 5px;
    min-height: 5px;
}}

QProgressBar::chunk {{
    background-color: {t.accent};
    border-radius: 3px;
}}

QScrollArea {{
    background-color: transparent;
    border: none;
}}

QScrollArea > QWidget > QWidget {{
    background-color: transparent;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px 2px 2px 2px;
}}

QScrollBar::handle:vertical {{
    background: {t.scrollbar_handle};
    min-height: 28px;
    border-radius: 3px;
}}

QScrollBar::handle:vertical:hover {{
    background: {t.scrollbar_hover};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
    height: 0px;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px 2px 2px 2px;
}}

QScrollBar::handle:horizontal {{
    background: {t.scrollbar_handle};
    min-width: 28px;
    border-radius: 3px;
}}

QScrollBar::handle:horizontal:hover {{
    background: {t.scrollbar_hover};
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
    width: 0px;
}}

QToolTip {{
    background-color: {t.bg_card};
    color: {t.text_primary};
    border: 1px solid {t.border_strong};
    border-radius: 6px;
    padding: 4px 8px;
}}

QMessageBox {{
    background-color: {t.bg_card};
    color: {t.text_primary};
}}
"""


def _render_icon(size: int, draw_fn) -> QtGui.QIcon:
    scale = 2
    pixmap = QtGui.QPixmap(size * scale, size * scale)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(scale)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    try:
        draw_fn(painter, float(size))
    finally:
        painter.end()
    return QtGui.QIcon(pixmap)


def make_app_icon(tokens: ThemeTokens, size: int = 32) -> QtGui.QIcon:
    def draw(p: QtGui.QPainter, s: float) -> None:
        grad = QtGui.QLinearGradient(0, 0, s, s)
        grad.setColorAt(0.0, QtGui.QColor(tokens.accent))
        grad.setColorAt(1.0, QtGui.QColor(tokens.accent_pressed))
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(grad)
        p.drawRoundedRect(QtCore.QRectF(1.5, 1.5, s - 3.0, s - 3.0), s * 0.24, s * 0.24)

        # Minimalist AstraPod silhouette (screen + click wheel)
        body = QtCore.QRectF(s * 0.27, s * 0.16, s * 0.46, s * 0.68)
        p.setBrush(QtGui.QColor("#FFFFFF"))
        p.drawRoundedRect(body, s * 0.08, s * 0.08)

        screen = QtCore.QRectF(s * 0.33, s * 0.22, s * 0.34, s * 0.24)
        p.setBrush(QtGui.QColor(tokens.accent_pressed))
        p.drawRoundedRect(screen, s * 0.04, s * 0.04)

        center = QtCore.QPointF(s * 0.50, s * 0.66)
        wheel_r = s * 0.125
        p.setPen(QtGui.QPen(QtGui.QColor(tokens.accent), max(1.6, s * 0.065)))
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        p.drawEllipse(center, wheel_r, wheel_r)

    return _render_icon(size, draw)


def make_eye_icon(visible: bool, color: str, size: int = 16) -> QtGui.QIcon:
    def draw(p: QtGui.QPainter, s: float) -> None:
        pen = QtGui.QPen(QtGui.QColor(color), 1.5)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)

        path = QtGui.QPainterPath()
        path.moveTo(s * 0.12, s * 0.50)
        path.quadTo(s * 0.50, s * 0.16, s * 0.88, s * 0.50)
        path.quadTo(s * 0.50, s * 0.84, s * 0.12, s * 0.50)
        p.drawPath(path)

        p.drawEllipse(QtCore.QPointF(s * 0.50, s * 0.50), s * 0.14, s * 0.14)
        if not visible:
            p.drawLine(QtCore.QPointF(s * 0.20, s * 0.80), QtCore.QPointF(s * 0.80, s * 0.20))

    return _render_icon(size, draw)


def make_theme_icon(mode: str, color: str, size: int = 14) -> QtGui.QIcon:
    def draw(p: QtGui.QPainter, s: float) -> None:
        pen = QtGui.QPen(QtGui.QColor(color), 1.4)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)

        cx, cy = s * 0.5, s * 0.5
        if mode == "light":
            p.drawEllipse(QtCore.QPointF(cx, cy), s * 0.19, s * 0.19)
            for dx, dy in (
                (0, -1), (0, 1), (-1, 0), (1, 0),
                (-0.7, -0.7), (0.7, -0.7), (-0.7, 0.7), (0.7, 0.7),
            ):
                p.drawLine(
                    QtCore.QPointF(cx + dx * s * 0.30, cy + dy * s * 0.30),
                    QtCore.QPointF(cx + dx * s * 0.40, cy + dy * s * 0.40),
                )
        elif mode == "dark":
            outer = QtGui.QPainterPath()
            outer.addEllipse(QtCore.QPointF(cx, cy), s * 0.34, s * 0.34)
            cut = QtGui.QPainterPath()
            cut.addEllipse(QtCore.QPointF(cx + s * 0.16, cy - s * 0.12), s * 0.28, s * 0.28)
            crescent = outer.subtracted(cut)
            p.setBrush(QtGui.QColor(color))
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.drawPath(crescent)
        else:
            # Split contrast circle for Follow System
            r = s * 0.33
            p.drawEllipse(QtCore.QPointF(cx, cy), r, r)
            half = QtGui.QPainterPath()
            half.moveTo(cx, cy - r)
            half.arcTo(QtCore.QRectF(cx - r, cy - r, 2 * r, 2 * r), 90, 180)
            half.closeSubpath()
            p.setBrush(QtGui.QColor(color))
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.drawPath(half)

    return _render_icon(size, draw)


class ThemeManager(QtCore.QObject):
    """Applies Follow-System / Light / Dark themes and persists UI preference."""

    theme_changed = Signal(str, str)

    def __init__(self, app: QtWidgets.QApplication,
                 prefs_path: Path | None = None,
                 parent: QtCore.QObject | None = None):
        super().__init__(parent)
        self.app = app
        self.prefs_path = prefs_path
        self._system_palette = QtGui.QPalette(app.palette())
        self._applying = False
        self.mode = load_theme_preference(self.prefs_path)
        self.resolved = resolve_theme(self.mode, self.app, self._system_palette)
        self.tokens = tokens_for(self.resolved)

        if "Fusion" in QtWidgets.QStyleFactory.keys():
            self.app.setStyle("Fusion")

        hints = self.app.styleHints()
        if hints is not None and hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(self._on_system_scheme_changed)

    def _sync_qt_color_scheme(self, mode: str) -> None:
        hints = self.app.styleHints()
        color_scheme_enum = getattr(QtCore.Qt, "ColorScheme", None)
        if hints is None or color_scheme_enum is None:
            return
        if mode == "light" and hasattr(hints, "setColorScheme"):
            hints.setColorScheme(color_scheme_enum.Light)
        elif mode == "dark" and hasattr(hints, "setColorScheme"):
            hints.setColorScheme(color_scheme_enum.Dark)
        elif mode == "system":
            if hasattr(hints, "unsetColorScheme"):
                hints.unsetColorScheme()
            elif hasattr(hints, "setColorScheme") and hasattr(color_scheme_enum, "Unknown"):
                hints.setColorScheme(color_scheme_enum.Unknown)

    def _on_system_scheme_changed(self, *_args) -> None:
        if self._applying or self.mode != "system":
            return
        self.apply("system", persist=False)

    def apply(self, mode: str | None = None, persist: bool = True) -> str:
        target_mode = self.mode if mode is None else mode
        if target_mode not in THEME_MODES:
            raise ValueError(f"unsupported theme mode: {target_mode}")

        self._applying = True
        try:
            self._sync_qt_color_scheme(target_mode)
            resolved = resolve_theme(target_mode, self.app, self._system_palette)
            tokens = tokens_for(resolved)
            self.mode = target_mode
            self.resolved = resolved
            self.tokens = tokens

            self.app.setPalette(build_palette(tokens))
            self.app.setStyleSheet(build_stylesheet(tokens))
            self.app.setWindowIcon(make_app_icon(tokens, 32))
        finally:
            self._applying = False

        if persist:
            save_theme_preference(target_mode, self.prefs_path)

        self.theme_changed.emit(self.mode, self.resolved)
        return self.resolved
