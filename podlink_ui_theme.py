"""Qt binding loader, Material 3 theming (Light/Dark/System, seed colour) and UI preference storage."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import podlink_m3
import podlink_settings

try:
    from PySide6 import QtCore, QtGui, QtSvg, QtWidgets

    Signal = QtCore.Signal
    Slot = QtCore.Slot
    QT_BINDING = "PySide6"
except ImportError:
    try:
        from PyQt6 import QtCore, QtGui, QtSvg, QtWidgets

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
    "system": "跟随系统",
    "light": "浅色",
    "dark": "深色",
}
RESOLVED_LABELS = {
    "light": "浅色模式",
    "dark": "深色模式",
}


M3_ROLES = tuple(podlink_m3.LIGHT_TONES)
DEFAULT_SEED = "#E0245E"
SEED_SWATCHES = ("#E0245E", "#6750A4", "#1E6FD9", "#00897B", "#7CB342", "#F4A100")
COLOR_SOURCES = ("system", "cover", "swatch")
COLOR_SOURCE_LABELS = {"system": "跟随系统", "cover": "封面取色", "swatch": "自选颜色"}
# GNOME's org.gnome.desktop.interface accent-color names, as libadwaita draws them.
GNOME_ACCENTS = {
    "blue": "#3584E4", "teal": "#2190A4", "green": "#3A944A", "yellow": "#C88800",
    "orange": "#ED5B00", "red": "#E62D42", "pink": "#D56199", "purple": "#9141AC",
    "slate": "#6F8396",
}

# Shape scale (corner radii in px, 1 px = 1 dp on the desktop).
SHAPE_XS, SHAPE_S, SHAPE_M, SHAPE_L, SHAPE_XL = 4, 8, 12, 16, 28


@dataclass(frozen=True)
class ThemeTokens:
    """Material 3 colour roles for one seed and mode.

    The role fields are M3's names. The older names after them (bg_*, text_*,
    accent*, glass*, …) are aliases onto those roles, kept because the painted
    widgets read them; "rgba(r,g,b,a)" values are state layers.
    """
    name: str
    seed: str
    primary: str
    on_primary: str
    primary_container: str
    on_primary_container: str
    secondary: str
    on_secondary: str
    secondary_container: str
    on_secondary_container: str
    tertiary: str
    on_tertiary: str
    tertiary_container: str
    on_tertiary_container: str
    error: str
    on_error: str
    error_container: str
    on_error_container: str
    surface: str
    on_surface: str
    surface_variant: str
    on_surface_variant: str
    surface_dim: str
    surface_bright: str
    surface_container_lowest: str
    surface_container_low: str
    surface_container: str
    surface_container_high: str
    surface_container_highest: str
    outline: str
    outline_variant: str
    inverse_surface: str
    inverse_on_surface: str
    inverse_primary: str
    scrim: str
    on_success: str
    success_container: str
    on_success_container: str
    warning_container: str
    on_warning_container: str
    info: str
    # Layout surfaces: the window behind panes, panes, and raised areas in them.
    window_bg: str
    pane: str
    pane_high: str
    hover_layer: str
    press_layer: str
    # Aliases for the painted widgets.
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
    glass: str
    glass_strong: str
    glass_border: str
    sidebar_glass: str
    hairline: str
    row_hover: str
    row_selected: str
    shadow: str
    lcd_bg: str
    lcd_text: str
    gauge_track: str
    cap_audio: str
    cap_other: str
    cap_free: str
    star: str
    working: str
    waiting: str
    done: str
    idle: str


FONT_UI = ("Roboto Flex", "Roboto", "Noto Sans CJK SC", "Source Han Sans CN",
           "HarmonyOS Sans SC", "sans-serif")
FONT_DISPLAY = FONT_UI
FONT_NUMERIC = ("Roboto Flex", "Roboto", "Noto Sans CJK SC", "sans-serif")
FONT_MONO = ("JetBrains Mono", "Roboto Mono", "Noto Sans Mono CJK SC", "monospace")


def qcolor(value: str) -> "QtGui.QColor":
    """Parse a token colour, including the "rgba(r,g,b,a)" form."""
    text = value.strip()
    if text.startswith("rgba(") and text.endswith(")"):
        r, g, b, a = (int(float(part)) for part in text[5:-1].split(","))
        return QtGui.QColor(r, g, b, a)
    return QtGui.QColor(text)


def font_family_css(families: tuple[str, ...]) -> str:
    return ", ".join(f'"{name}"' if " " in name else name for name in families)


def _layer(color: str, opacity: float) -> str:
    """A state layer: the role colour at an opacity, as rgba()."""
    r, g, b = podlink_m3.argb_to_rgb(podlink_m3.hex_to_argb(color))
    return f"rgba({r},{g},{b},{round(opacity * 255)})"


def _over(color: str, base: str, opacity: float) -> str:
    """A state layer flattened onto an opaque base colour."""
    fg = podlink_m3.argb_to_rgb(podlink_m3.hex_to_argb(color))
    bg = podlink_m3.argb_to_rgb(podlink_m3.hex_to_argb(base))
    mixed = [round(b + (f - b) * opacity) for f, b in zip(fg, bg)]
    return "#%02X%02X%02X" % tuple(mixed)


def build_tokens(seed: str, dark: bool) -> ThemeTokens:
    r = podlink_m3.scheme(podlink_m3.hex_to_argb(seed), dark)
    window_bg = r["surface"] if dark else r["surface_container"]
    pane = r["surface_container"] if dark else r["surface_container_lowest"]
    pane_high = r["surface_container_high"] if dark else r["surface_container_low"]
    roles = {k: v for k, v in r.items() if k not in ("shadow", "success", "warning", "on_success")}
    return ThemeTokens(
        name="dark" if dark else "light",
        seed=seed,
        **roles,
        on_success=r["on_success"],
        window_bg=window_bg, pane=pane, pane_high=pane_high,
        hover_layer=_layer(r["on_surface"], 0.08),
        press_layer=_layer(r["on_surface"], 0.12),
        bg_window=window_bg,
        bg_header=r["surface_container"],
        bg_card=pane,
        bg_subtle=pane_high,
        bg_input=pane,
        bg_input_disabled=_over(r["on_surface"], pane, 0.04),
        bg_code=r["surface_container_low"],
        border=r["outline_variant"],
        border_strong=r["outline"],
        border_focus=r["primary"],
        text_primary=r["on_surface"],
        text_secondary=r["on_surface_variant"],
        text_muted=r["on_surface_variant"],
        text_disabled=_over(r["on_surface"], pane, 0.38),
        text_on_accent=r["on_primary"],
        accent=r["primary"],
        accent_hover=_over(r["on_primary"], r["primary"], 0.08),
        accent_pressed=_over(r["on_primary"], r["primary"], 0.12),
        accent_soft=r["secondary_container"],
        accent_text=r["primary"],
        success=r["success"],
        success_bg=r["success_container"],
        success_border=r["success_container"],
        danger=r["error"],
        danger_hover=_over(r["on_error"], r["error"], 0.08),
        danger_pressed=_over(r["on_error"], r["error"], 0.12),
        danger_bg=r["error_container"],
        danger_border=r["error_container"],
        warning=r["warning"],
        warning_bg=r["warning_container"],
        warning_border=r["warning_container"],
        scrollbar_handle=r["outline_variant"],
        scrollbar_hover=r["outline"],
        glass=pane,
        glass_strong=pane_high,
        glass_border=r["outline_variant"],
        sidebar_glass=r["surface_container_low"],
        hairline=r["outline_variant"],
        row_hover=_layer(r["on_surface"], 0.08),
        row_selected=r["secondary_container"],
        shadow="rgba(0,0,0,110)" if dark else "rgba(0,0,0,46)",
        lcd_bg=r["surface_container_high"],
        lcd_text=r["on_surface"],
        gauge_track=r["secondary_container"],
        cap_audio=r["primary"],
        cap_other=r["tertiary"],
        cap_free=r["secondary_container"],
        star=r["primary"],
        working=r["success"],
        waiting=r["warning"],
        done=r["info"],
        idle=r["outline"],
    )


LIGHT_TOKENS = build_tokens(DEFAULT_SEED, False)
DARK_TOKENS = build_tokens(DEFAULT_SEED, True)


def tokens_for(resolved: str, seed: str = DEFAULT_SEED) -> ThemeTokens:
    return build_tokens(seed, resolved == "dark")


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


def qt_hint_scheme(app: QtWidgets.QApplication | None) -> str | None:
    """Qt's own light/dark answer, or None when the platform theme has none."""
    color_scheme_enum = getattr(QtCore.Qt, "ColorScheme", None)
    hints = app.styleHints() if app is not None else None
    if color_scheme_enum is not None and hints is not None and hasattr(hints, "colorScheme"):
        scheme = hints.colorScheme()
        if scheme == color_scheme_enum.Dark:
            return "dark"
        if scheme == color_scheme_enum.Light:
            return "light"
    return None


def detect_system_theme(app: QtWidgets.QApplication,
                        fallback_palette: QtGui.QPalette | None = None,
                        appearance: "SystemAppearance | None" = None) -> str:
    scheme = appearance.scheme(app) if appearance is not None else qt_hint_scheme(app)
    if scheme:
        return scheme
    palette = fallback_palette if fallback_palette is not None else app.palette()
    window_color = palette.color(QtGui.QPalette.ColorRole.Window)
    return "dark" if window_color.lightnessF() < 0.5 else "light"


def resolve_theme(mode: str, app: QtWidgets.QApplication,
                  fallback_palette: QtGui.QPalette | None = None,
                  appearance: "SystemAppearance | None" = None) -> str:
    if mode in ("light", "dark"):
        return mode
    return detect_system_theme(app, fallback_palette, appearance)


# The desktop's appearance -------------------------------------------------
PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_SETTINGS = "org.freedesktop.portal.Settings"
APPEARANCE = "org.freedesktop.appearance"


def _command(argv: list[str], timeout: float) -> str | None:
    """stdout of a short read-only command, or None. Only absolute
    environment facts are relied on: the session bus address and PATH that
    a systemd user service inherits."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def portal_value(text: str | None):
    """The plain value inside busctl's JSON reply to Settings.ReadOne."""
    try:
        value = json.loads(text) if text else None
    except ValueError:
        return None
    while isinstance(value, dict) and "data" in value:
        value = value["data"]
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
            value = value[0]
    return value


def read_portal(key: str):
    return portal_value(_command(
        ["busctl", "--user", "--json=short", "--timeout=2", "call", PORTAL_SERVICE, PORTAL_PATH,
         PORTAL_SETTINGS, "ReadOne", "ss", APPEARANCE, key], 3))


def portal_scheme(value) -> str | None:
    """color-scheme: 1 prefers dark, 2 prefers light, 0 has no preference."""
    return {1: "dark", 2: "light"}.get(value) if isinstance(value, int) else None


def portal_accent(value) -> str | None:
    """accent-color is three doubles in 0..1; anything else means unset."""
    if (not isinstance(value, list) or len(value) != 3
            or not all(isinstance(c, (int, float)) and 0 <= c <= 1 for c in value)):
        return None
    return "#" + "".join(f"{round(c * 255):02X}" for c in value)


def read_gsettings(key: str) -> str | None:
    text = _command(["gsettings", "get", "org.gnome.desktop.interface", key], 2)
    return text.strip().strip("'") if text else None


def gsettings_scheme(value: str | None) -> str | None:
    return {"prefer-dark": "dark", "prefer-light": "light", "default": "light"}.get(value or "")


def gsettings_accent(value: str | None) -> str | None:
    return GNOME_ACCENTS.get((value or "").lower())


class SystemAppearance(QtCore.QObject):
    """Light/dark and the accent colour from the desktop: the XDG desktop
    portal first, then Qt's style hints (light/dark only), then GNOME's
    gsettings, then the Qt palette's Highlight for the accent. Emits
    changed when the portal or gsettings report a new value."""

    changed = Signal()

    def __init__(self, parent: QtCore.QObject | None = None, listen: bool = True):
        super().__init__(parent)
        self.values: dict[str, object] = {}
        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(150)
        self._debounce.timeout.connect(self._reread)
        self._monitor = None
        self.refresh()
        if listen:
            self._listen()

    def refresh(self) -> None:
        self.values = {
            "portal_scheme": portal_scheme(read_portal("color-scheme")),
            "portal_accent": portal_accent(read_portal("accent-color")),
            "gsettings_scheme": gsettings_scheme(read_gsettings("color-scheme")),
            "gsettings_accent": gsettings_accent(read_gsettings("accent-color")),
        }

    def scheme(self, app: QtWidgets.QApplication | None) -> str | None:
        return (self.values.get("portal_scheme") or qt_hint_scheme(app)
                or self.values.get("gsettings_scheme"))

    def accent(self, palette: QtGui.QPalette | None = None) -> tuple[str | None, str]:
        """(hex colour, where it came from) or (None, "")."""
        if self.values.get("portal_accent"):
            return self.values["portal_accent"], "portal"
        if self.values.get("gsettings_accent"):
            return self.values["gsettings_accent"], "gsettings"
        if palette is not None:
            highlight = palette.color(QtGui.QPalette.ColorRole.Highlight)
            if highlight.isValid() and highlight.saturationF() > 0.05:
                return highlight.name().upper(), "palette"
        return None, ""

    def _listen(self) -> None:
        try:
            from PySide6 import QtDBus
        except ImportError:
            QtDBus = None
        if QtDBus is not None and QT_BINDING == "PySide6":
            bus = QtDBus.QDBusConnection.sessionBus()
            if bus.isConnected():
                bus.connect(PORTAL_SERVICE, PORTAL_PATH, PORTAL_SETTINGS, "SettingChanged",
                            self, QtCore.SLOT("_setting_changed(QDBusMessage)"))
        # GNOME's accent-color reaches the portal only through some backends,
        # so its own change notifications are watched as well.
        self._monitor = QtCore.QProcess(self)
        self._monitor.setProgram("gsettings")
        self._monitor.setArguments(["monitor", "org.gnome.desktop.interface"])
        self._monitor.setStandardInputFile(QtCore.QProcess.nullDevice())
        self._monitor.readyReadStandardOutput.connect(self._monitor_output)
        self._monitor.start()

    @Slot("QDBusMessage")
    def _setting_changed(self, message) -> None:
        arguments = message.arguments()
        if arguments and arguments[0] == APPEARANCE:
            self._debounce.start()

    def _monitor_output(self) -> None:
        text = bytes(self._monitor.readAllStandardOutput()).decode("utf-8", "replace")
        if re.search(r"^(color-scheme|accent-color):", text, re.MULTILINE):
            self._debounce.start()

    def _reread(self) -> None:
        before = dict(self.values)
        self.refresh()
        if self.values != before:
            self.changed.emit()

    def stop(self) -> None:
        if self._monitor is not None and self._monitor.state() != QtCore.QProcess.ProcessState.NotRunning:
            self._monitor.kill()
            self._monitor.waitForFinished(500)


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
    """Material 3 components as Qt style sheet rules.

    Type scale: headline-medium page titles, title-medium card titles,
    body-medium text, label-large buttons. Shapes follow SHAPE_*.
    """
    ui = font_family_css(FONT_UI)
    mono = font_family_css(FONT_MONO)
    hover = t.hover_layer
    press = t.press_layer
    primary_hover = _layer(t.primary, 0.08)
    primary_press = _layer(t.primary, 0.12)
    return f"""
QMainWindow, QDialog, QMessageBox {{
    background-color: {t.window_bg};
    color: {t.on_surface};
}}

QDialog, QMessageBox {{
    background-color: {t.surface_container_high};
}}

QWidget {{
    color: {t.on_surface};
    font-family: {ui};
    font-size: 14px;
}}

QLabel {{
    background: transparent;
}}

/* Type scale ----------------------------------------------------------- */
QLabel[role="page-title"] {{
    font-size: 28px;
    font-weight: 400;
    color: {t.on_surface};
}}

QLabel[role="page-subtitle"] {{
    font-size: 14px;
    color: {t.on_surface_variant};
}}

QLabel[role="hero-title"] {{
    font-size: 24px;
    font-weight: 400;
}}

QLabel[role="numeric"] {{
    font-feature-settings: "tnum";
}}

QLabel[role="stat-value"] {{
    font-size: 28px;
    font-weight: 400;
}}

QLabel[role="stat-label"] {{
    font-size: 12px;
    font-weight: 500;
    color: {t.on_surface_variant};
}}

QLabel[role="sidebar-brand"] {{
    font-size: 22px;
    font-weight: 400;
    color: {t.on_surface};
}}

QLabel[role="card-title"] {{
    font-size: 16px;
    font-weight: 500;
    color: {t.on_surface};
}}

QLabel[role="section-subtitle"] {{
    font-size: 14px;
    font-weight: 500;
    color: {t.on_surface_variant};
}}

QLabel[role="field-label"] {{
    font-size: 14px;
    color: {t.on_surface};
}}

QLabel[role="helper"] {{
    font-size: 12px;
    color: {t.on_surface_variant};
}}

QLabel[role="error"] {{
    font-size: 12px;
    color: {t.error};
}}

/* Cards: tonal panes on the window surface ------------------------------ */
QFrame[card="true"] {{
    background-color: {t.pane};
    border: none;
    border-radius: {SHAPE_L}px;
}}

QFrame[card="outlined"] {{
    background-color: transparent;
    border: 1px solid {t.outline_variant};
    border-radius: {SHAPE_M}px;
}}

QFrame#FooterBar {{
    background-color: {t.surface_container_high};
    border: none;
    border-radius: {SHAPE_L}px;
}}

QFrame#Divider {{
    background-color: {t.outline_variant};
    border: none;
    max-height: 1px;
    min-height: 1px;
}}

/* Status chips (assist-chip shape) -------------------------------------- */
QLabel[badge="true"] {{
    font-size: 12px;
    font-weight: 500;
    border-radius: {SHAPE_S}px;
    padding: 4px 10px;
}}

QLabel[badge="true"][tone="neutral"] {{
    background-color: transparent;
    color: {t.on_surface_variant};
    border: 1px solid {t.outline_variant};
}}

QLabel[badge="true"][tone="success"] {{
    background-color: {t.success_container};
    color: {t.on_success_container};
    border: 1px solid {t.success_container};
}}

QLabel[badge="true"][tone="danger"] {{
    background-color: {t.error_container};
    color: {t.on_error_container};
    border: 1px solid {t.error_container};
}}

QLabel[badge="true"][tone="busy"] {{
    background-color: {t.warning_container};
    color: {t.on_warning_container};
    border: 1px solid {t.warning_container};
}}

QLabel[badge="true"][tone="accent"] {{
    background-color: {t.secondary_container};
    color: {t.on_secondary_container};
    border: 1px solid {t.secondary_container};
}}

/* Buttons: outlined by default; filled, tonal, text and error variants --- */
QPushButton {{
    background-color: transparent;
    color: {t.primary};
    border: 1px solid {t.outline};
    border-radius: 20px;
    padding: 0px 22px;
    min-height: 38px;
    font-size: 14px;
    font-weight: 500;
}}

QPushButton:hover {{
    background-color: {primary_hover};
}}

QPushButton:pressed {{
    background-color: {primary_press};
}}

QPushButton:focus {{
    border: 2px solid {t.primary};
    padding: 0px 21px;
}}

QPushButton:disabled {{
    color: {_layer(t.on_surface, 0.38)};
    border-color: {_layer(t.on_surface, 0.12)};
    background-color: transparent;
}}

/* Every variant keeps a 1px border, transparent where unseen: a radius over
   half the button's height makes Qt draw square corners. */
QPushButton[variant="primary"] {{
    background-color: {t.primary};
    color: {t.on_primary};
    border: 1px solid transparent;
    padding: 0px 24px;
}}

QPushButton[variant="primary"]:hover {{
    background-color: {t.accent_hover};
}}

QPushButton[variant="primary"]:pressed {{
    background-color: {t.accent_pressed};
}}

QPushButton[variant="primary"]:focus {{
    border: 2px solid {t.on_primary_container};
    padding: 0px 22px;
}}

QPushButton[variant="primary"]:disabled, QPushButton[variant="tonal"]:disabled,
QPushButton[variant="danger"]:disabled, QPushButton[variant="option"]:disabled {{
    background-color: {_layer(t.on_surface, 0.12)};
    color: {_layer(t.on_surface, 0.38)};
    border: 1px solid transparent;
}}

QPushButton[variant="tonal"], QPushButton[variant="option"] {{
    background-color: {t.secondary_container};
    color: {t.on_secondary_container};
    border: 1px solid transparent;
    padding: 0px 24px;
}}

QPushButton[variant="tonal"]:hover, QPushButton[variant="option"]:hover {{
    background-color: {_over(t.on_secondary_container, t.secondary_container, 0.08)};
}}

QPushButton[variant="tonal"]:pressed, QPushButton[variant="option"]:pressed {{
    background-color: {_over(t.on_secondary_container, t.secondary_container, 0.12)};
}}

QPushButton[variant="tonal"]:focus, QPushButton[variant="option"]:focus {{
    border: 2px solid {t.primary};
    padding: 0px 22px;
}}

QPushButton[variant="danger"] {{
    background-color: {t.error};
    color: {t.on_error};
    border: 1px solid transparent;
    padding: 0px 24px;
}}

QPushButton[variant="danger"]:hover {{
    background-color: {t.danger_hover};
}}

QPushButton[variant="danger"]:pressed {{
    background-color: {t.danger_pressed};
}}

QPushButton[variant="ghost"] {{
    background-color: transparent;
    color: {t.primary};
    border: 1px solid transparent;
    padding: 0px 12px;
}}

QPushButton[variant="ghost"]:hover {{
    background-color: {primary_hover};
}}

QPushButton[variant="ghost"]:pressed {{
    background-color: {primary_press};
}}

QPushButton[variant="ghost"]:focus {{
    border: 2px solid {t.primary};
    padding: 0px 10px;
}}

QPushButton[variant="ghost"]:disabled {{
    border: 1px solid transparent;
}}

QToolButton {{
    background-color: transparent;
    border: none;
    border-radius: 20px;
    padding: 6px;
    color: {t.on_surface_variant};
}}

QToolButton:hover {{
    background-color: {hover};
}}

QToolButton:pressed {{
    background-color: {press};
}}

QToolButton:focus {{
    border: 2px solid {t.primary};
}}

/* Segmented buttons ------------------------------------------------------ */
QFrame[segmented="true"] {{
    background-color: transparent;
    border: 1px solid {t.outline};
    border-radius: 20px;
}}

QPushButton[segment="true"] {{
    background-color: transparent;
    color: {t.on_surface};
    border: none;
    border-left: 1px solid {t.outline};
    border-radius: 0px;
    padding: 0px 16px;
    min-height: 38px;
    font-weight: 500;
}}

QPushButton[segment="true"][first="true"] {{
    border-left: none;
    border-top-left-radius: 19px;
    border-bottom-left-radius: 19px;
}}

QPushButton[segment="true"][last="true"] {{
    border-top-right-radius: 19px;
    border-bottom-right-radius: 19px;
}}

QPushButton[segment="true"]:hover {{
    background-color: {hover};
}}

QPushButton[segment="true"]:checked {{
    background-color: {t.secondary_container};
    color: {t.on_secondary_container};
}}

QPushButton[segment="true"]:focus {{
    padding: 0px 16px;
    border-top: none;
    border-bottom: none;
    border-right: none;
    background-color: {_layer(t.on_surface, 0.10)};
}}

QPushButton[segment="true"]:checked:focus {{
    background-color: {_over(t.on_secondary_container, t.secondary_container, 0.10)};
}}

/* Text fields: outlined ---------------------------------------------- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
    background-color: transparent;
    color: {t.on_surface};
    border: 1px solid {t.outline};
    border-radius: {SHAPE_XS}px;
    padding: 8px 12px;
    min-height: 22px;
    selection-background-color: {t.primary};
    selection-color: {t.on_primary};
}}

/* The editor's inset comes from its text margins: this padding is lost
   when the style sheet is replaced. */
QAbstractSpinBox QLineEdit {{
    background: transparent;
    border: none;
    padding: 0px;
    min-height: 0px;
}}

/* Without an explicit origin, a styled spin box with no buttons
   gives its editor a width of 1 pixel. */
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    width: 0px;
    border: none;
    subcontrol-origin: border;
}}

QLineEdit:hover, QComboBox:hover, QSpinBox:hover, QDoubleSpinBox:hover {{
    border-color: {t.on_surface};
}}

QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 2px solid {t.primary};
    padding: 7px 11px;
}}

QLineEdit:disabled, QComboBox:disabled {{
    color: {_layer(t.on_surface, 0.38)};
    border-color: {_layer(t.on_surface, 0.12)};
}}

QLineEdit[readonlyField="true"] {{
    background-color: transparent;
    border: none;
    border-bottom: 1px solid {t.outline_variant};
    border-radius: 0px;
    padding: 6px 2px;
}}

QLineEdit[readonlyField="true"]:hover, QLineEdit[readonlyField="true"]:focus {{
    border: none;
    border-bottom: 1px solid {t.primary};
    padding: 6px 2px;
}}

/* Search bar: full-pill, surface-container-high ------------------------- */
QLineEdit#SearchField {{
    background-color: {t.surface_container_high};
    border: none;
    border-radius: 24px;
    padding: 0px 16px 0px 4px;
    min-height: 48px;
    font-size: 16px;
    color: {t.on_surface};
}}

QLineEdit#SearchField:hover {{
    background-color: {_over(t.on_surface, t.surface_container_high, 0.08)};
}}

QLineEdit#SearchField:focus {{
    border: none;
    padding: 0px 16px 0px 4px;
    background-color: {t.surface_container_highest};
}}

QLineEdit#SearchField:disabled {{
    background-color: {_layer(t.on_surface, 0.04)};
}}

QComboBox {{
    padding-right: 36px;
}}

QComboBox:focus {{
    padding-right: 35px;
}}

QComboBox QLineEdit {{
    border: none;
    padding: 0px;
    background: transparent;
}}

QComboBox::drop-down {{
    border: none;
    width: 36px;
}}

QComboBox::down-arrow {{
    image: none;
    width: 0px;
    height: 0px;
}}

QComboBox QAbstractItemView {{
    background-color: {t.surface_container};
    color: {t.on_surface};
    border: none;
    border-radius: {SHAPE_XS}px;
    padding: 8px 0px;
    outline: none;
    selection-background-color: {hover};
    selection-color: {t.on_surface};
}}

QPlainTextEdit, QTextEdit {{
    background-color: transparent;
    color: {t.on_surface};
    border: 1px solid {t.outline};
    border-radius: {SHAPE_XS}px;
    padding: 6px 8px;
    selection-background-color: {t.primary};
    selection-color: {t.on_primary};
}}

QPlainTextEdit:focus, QTextEdit:focus {{
    border: 2px solid {t.primary};
    padding: 5px 7px;
}}

QPlainTextEdit#DiffPreview, QPlainTextEdit#LogView {{
    background-color: {t.surface_container_low};
    border: none;
    border-radius: {SHAPE_M}px;
    font-family: {mono};
    font-size: 12px;
    padding: 10px 12px;
}}

/* Progress, sliders --------------------------------------------------- */
QProgressBar {{
    background-color: {t.secondary_container};
    border: none;
    border-radius: 2px;
    max-height: 4px;
    min-height: 4px;
    text-align: center;
    color: transparent;
}}

QProgressBar::chunk {{
    background-color: {t.primary};
    border-radius: 2px;
}}

QSlider::groove:horizontal {{
    height: 4px;
    background: {t.secondary_container};
    border-radius: 2px;
}}

QSlider::sub-page:horizontal {{
    background: {t.primary};
    border-radius: 2px;
}}

QSlider::handle:horizontal {{
    background: {t.primary};
    width: 20px;
    height: 20px;
    margin: -8px 0;
    border-radius: 10px;
}}

/* Lists and the songs data table ---------------------------------------- */
QTableView, QTreeView, QListView {{
    background-color: transparent;
    alternate-background-color: transparent;
    border: none;
    outline: none;
    gridline-color: transparent;
    selection-background-color: {t.secondary_container};
    selection-color: {t.on_secondary_container};
}}

QTableView::item, QTreeView::item {{
    border: none;
    border-bottom: 1px solid {t.outline_variant};
    padding: 0px 10px;
}}

QTableView::item:hover, QTreeView::item:hover {{
    background-color: {hover};
}}

QTableView::item:selected, QTreeView::item:selected, QListView::item:selected {{
    background-color: {t.secondary_container};
    color: {t.on_secondary_container};
}}

QListView::item {{
    border-radius: {SHAPE_M}px;
    padding: 6px 10px;
}}

QListView::item:hover {{
    background-color: {hover};
}}

QHeaderView {{
    background-color: transparent;
    border: none;
}}

QHeaderView::section {{
    background-color: transparent;
    color: {t.on_surface_variant};
    border: none;
    border-bottom: 1px solid {t.outline_variant};
    padding: 0px 10px;
    min-height: 44px;
    font-size: 13px;
    font-weight: 500;
}}

QHeaderView::section:hover {{
    background-color: {hover};
    color: {t.on_surface};
}}

QAbstractScrollArea::corner, QTableCornerButton::section {{
    background: transparent;
    border: none;
}}

/* Tabs: primary tabs with a 3 px indicator ------------------------------ */
QTabWidget::pane {{
    border: none;
    border-top: 1px solid {t.outline_variant};
    top: -1px;
}}

QTabBar {{
    background: transparent;
    qproperty-drawBase: 0;
}}

QTabBar::tab {{
    background: transparent;
    color: {t.on_surface_variant};
    border: none;
    border-bottom: 3px solid transparent;
    padding: 12px 16px 9px 16px;
    font-size: 14px;
    font-weight: 500;
}}

QTabBar::tab:selected {{
    color: {t.primary};
    border-bottom: 3px solid {t.primary};
}}

QTabBar::tab:hover {{
    background-color: {hover};
}}

QCheckBox {{
    spacing: 10px;
}}

QSplitter::handle {{
    background: transparent;
}}

QScrollArea {{
    background: transparent;
    border: none;
}}

QScrollArea > QWidget > QWidget {{
    background: transparent;
}}

QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 4px 2px 4px 0px;
}}

QScrollBar::handle:vertical {{
    background: {t.outline_variant};
    border-radius: 4px;
    min-height: 32px;
}}

QScrollBar::handle:vertical:hover {{
    background: {t.outline};
}}

QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0px;
    width: 0px;
}}

QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
}}

QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 0px 4px 2px 4px;
}}

QScrollBar::handle:horizontal {{
    background: {t.outline_variant};
    border-radius: 4px;
    min-width: 32px;
}}

QScrollBar::handle:horizontal:hover {{
    background: {t.outline};
}}

/* Plain tooltip and menus ---------------------------------------------- */
QToolTip {{
    background-color: {t.inverse_surface};
    color: {t.inverse_on_surface};
    border: none;
    border-radius: {SHAPE_XS}px;
    padding: 4px 8px;
    font-size: 12px;
}}

QMenu {{
    background-color: {t.surface_container};
    color: {t.on_surface};
    border: none;
    border-radius: {SHAPE_XS}px;
    padding: 8px 0px;
}}

QMenu::item {{
    padding: 9px 24px 9px 16px;
    background: transparent;
}}

QMenu::item:selected {{
    background-color: {hover};
}}

QMenu::item:disabled {{
    color: {_layer(t.on_surface, 0.38)};
}}

QMenu::separator {{
    height: 1px;
    background: {t.outline_variant};
    margin: 8px 0px;
}}

QMenu::indicator {{
    width: 18px;
    height: 18px;
    left: 8px;
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


BRAND_DIR = Path(__file__).resolve().parent / "astrapod_desktop" / "assets"
BRAND_ICON_SIZES = (16, 22, 24, 32, 48, 64, 128, 256)


def _svg_icon(name: str) -> QtGui.QIcon:
    renderer = QtSvg.QSvgRenderer(str(BRAND_DIR / name))
    icon = QtGui.QIcon()
    for size in BRAND_ICON_SIZES:
        image = QtGui.QImage(size, size, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(image)
        try:
            renderer.render(painter)
        finally:
            painter.end()
        icon.addPixmap(QtGui.QPixmap.fromImage(image))
    return icon


def make_app_icon() -> QtGui.QIcon:
    """The AstraPod B series app icon, at every size a window or dock asks for."""
    return _svg_icon("astrapod-app.svg")


def make_tray_icon() -> QtGui.QIcon:
    """The app icon, or on macOS the single-colour mark as a template image,
    which the menu bar tints to match its own appearance."""
    if sys.platform != "darwin":
        return make_app_icon()
    icon = _svg_icon("astrapod-mono.svg")
    icon.setIsMask(True)
    return icon


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


def load_color_source(path: Path | None = None) -> str:
    """Where the seed colour comes from; follows the system unless the
    owner chose the cover or a swatch."""
    try:
        payload = json.loads((path or ui_prefs_path()).read_text(encoding="utf-8"))
        source = payload.get("color_source") if isinstance(payload, dict) else None
    except (OSError, ValueError):
        source = None
    return source if source in COLOR_SOURCES else "system"


def load_seed_preference(path: Path | None = None) -> str:
    """The fallback seed colour saved with the UI preferences."""
    try:
        payload = json.loads((path or ui_prefs_path()).read_text(encoding="utf-8"))
        seed = payload.get("seed") if isinstance(payload, dict) else None
        if isinstance(seed, str):
            podlink_m3.hex_to_argb(seed)
            return seed.upper()
    except (OSError, ValueError):
        pass
    return DEFAULT_SEED


class ThemeManager(QtCore.QObject):
    """Applies Follow-System / Light / Dark with a Material 3 seed colour,
    and persists the mode. system_changed fires when the desktop's
    light/dark or accent colour changes."""

    theme_changed = Signal(str, str)
    system_changed = Signal()

    def __init__(self, app: QtWidgets.QApplication,
                 prefs_path: Path | None = None,
                 parent: QtCore.QObject | None = None,
                 appearance: SystemAppearance | None = None):
        super().__init__(parent)
        self.app = app
        self.prefs_path = prefs_path
        self._system_palette = QtGui.QPalette(app.palette())
        self._applying = False
        self.appearance = appearance if appearance is not None else SystemAppearance(self)
        self.appearance.changed.connect(self._on_appearance_changed)
        if hasattr(self.appearance, "stop"):
            app.aboutToQuit.connect(self.appearance.stop)
        self.mode = load_theme_preference(self.prefs_path)
        self.color_source = load_color_source(self.prefs_path)
        self.swatch_seed = load_seed_preference(self.prefs_path)
        self.seed = self.swatch_seed if self.color_source == "swatch" else self.system_seed()
        self.resolved = resolve_theme(self.mode, self.app, self._system_palette, self.appearance)
        self.tokens = tokens_for(self.resolved, self.seed)

        if "Fusion" in QtWidgets.QStyleFactory.keys():
            self.app.setStyle("Fusion")

        hints = self.app.styleHints()
        if hints is not None and hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(self._on_system_scheme_changed)

    def system_accent(self) -> tuple[str | None, str]:
        return self.appearance.accent(self._system_palette)

    def system_seed(self) -> str:
        return self.system_accent()[0] or DEFAULT_SEED

    def _on_appearance_changed(self) -> None:
        if self.mode == "system":
            self.apply("system", persist=False)
        self.system_changed.emit()

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
            resolved = resolve_theme(target_mode, self.app, self._system_palette,
                                     self.appearance)
            tokens = tokens_for(resolved, self.seed)
            self.mode = target_mode
            self.resolved = resolved
            self.tokens = tokens

            self.app.setPalette(build_palette(tokens))
            self.app.setStyleSheet(build_stylesheet(tokens))
            self.app.setWindowIcon(make_app_icon())
        finally:
            self._applying = False

        if persist:
            save_theme_preference(target_mode, self.prefs_path)

        self.theme_changed.emit(self.mode, self.resolved)
        return self.resolved

    def set_seed(self, seed: str) -> bool:
        """Re-theme from a new source colour; False when it is unchanged."""
        seed = podlink_m3.argb_to_hex(podlink_m3.hex_to_argb(seed))
        if seed == self.seed:
            return False
        self.seed = seed
        self.apply(persist=False)
        return True
