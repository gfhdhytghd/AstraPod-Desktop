#!/usr/bin/env python3
"""AstraPod desktop connection settings and Jellyfin synchronization (Qt Widgets)."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from threading import Thread
from urllib.parse import urlsplit

_TOOLS_DIR = Path(__file__).resolve().parent
for _extra in (str(_TOOLS_DIR), str(_TOOLS_DIR / "herdremote")):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

import podlink_settings
from podlink_ui_theme import (
    QT_BINDING,
    RESOLVED_LABELS,
    THEME_LABELS,
    THEME_MODES,
    QtCore,
    QtGui,
    QtWidgets,
    Signal,
    ThemeManager,
    ThemeTokens,
    make_app_icon,
    make_eye_icon,
    make_theme_icon,
)
import hrmediasync
from hrjellyfin import https_base
from hrrufin import loopback_url

SCOPE_PREFIXES = {
    "all": "",
    "library": "音乐库 · ",
    "playlist": "播放列表 · ",
}

SETTING_CHOICES = {
    "CIDER_BACKEND": ("auto", "plugin", "rpc"),
    "HERDREMOTE_VOICE_FOCUS": ("on", "off"),
    "HERDREMOTE_KEY_TOOL": ("auto", "ydotool", "wtype"),
}

MEDIA_SETTING_KEYS = (
    "JELLYFIN_URL",
    "CIDER_API_URL",
    "CIDER_BACKEND",
    "RUFIN_API_URL",
)

AGENT_SETTING_KEYS = (
    "HERDR_SOCKET_PATH",
    "HERDREMOTE_VOICE_FOCUS",
    "HERDREMOTE_USAGE_CACHE_DIR",
    "HERDREMOTE_KEY_TOOL",
)

SETTING_PLACEHOLDERS = {
    "JELLYFIN_URL": "https://jellyfin.example.com",
    "CIDER_API_URL": "http://127.0.0.1:10767",
    "RUFIN_API_URL": "http://127.0.0.1:1717（留空自动识别）",
    "HERDR_SOCKET_PATH": "例如 /run/user/1000/herdr.sock",
    "HERDREMOTE_USAGE_CACHE_DIR": "留空使用默认缓存目录",
}


def _repolish(widget: QtWidgets.QWidget) -> None:
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()


def _set_badge(label: QtWidgets.QLabel, text: str, tone: str = "neutral") -> None:
    label.setText(text)
    if label.property("tone") != tone:
        label.setProperty("tone", tone)
        _repolish(label)


def _make_badge(text: str, tone: str = "neutral") -> QtWidgets.QLabel:
    badge = QtWidgets.QLabel(text)
    badge.setProperty("badge", True)
    badge.setProperty("tone", tone)
    badge.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
    badge.setSizePolicy(
        QtWidgets.QSizePolicy.Policy.Fixed,
        QtWidgets.QSizePolicy.Policy.Fixed,
    )
    return badge


def _make_card(spacing: int = 12, expand_vertical: bool = False) -> tuple[QtWidgets.QFrame, QtWidgets.QVBoxLayout]:
    card = QtWidgets.QFrame()
    card.setProperty("card", True)
    card.setSizePolicy(
        QtWidgets.QSizePolicy.Policy.Expanding,
        QtWidgets.QSizePolicy.Policy.Expanding
        if expand_vertical
        else QtWidgets.QSizePolicy.Policy.Minimum,
    )
    layout = QtWidgets.QVBoxLayout(card)
    layout.setContentsMargins(18, 14, 18, 14)
    layout.setSpacing(spacing)
    return card, layout


class ChevronComboBox(QtWidgets.QComboBox):
    """QComboBox with a crisp theme-aware vector dropdown chevron."""

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self._chevron_color = "#64748B"
        self._disabled_color = "#94A3B8"

    def update_theme(self, tokens: ThemeTokens) -> None:
        self._chevron_color = tokens.text_secondary
        self._disabled_color = tokens.text_disabled
        self.update()

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        try:
            color = self._chevron_color if self.isEnabled() else self._disabled_color
            pen = QtGui.QPen(QtGui.QColor(color), 1.6)
            pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            cx = self.width() - 14.0
            cy = self.height() / 2.0
            path = QtGui.QPainterPath()
            path.moveTo(cx - 4.0, cy - 1.8)
            path.lineTo(cx, cy + 2.2)
            path.lineTo(cx + 4.0, cy - 1.8)
            painter.drawPath(path)
        finally:
            painter.end()


class PasswordLineEdit(QtWidgets.QLineEdit):
    """Masked input with a theme-aware reveal/hide toggle action."""

    def __init__(self, placeholder: str = "", parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setPlaceholderText(placeholder)
        self.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self._revealed = False
        self._icon_color = "#64748B"
        self._toggle_action = self.addAction(
            make_eye_icon(False, self._icon_color),
            QtWidgets.QLineEdit.ActionPosition.TrailingPosition,
        )
        self._toggle_action.setToolTip("显示内容")
        self._toggle_action.triggered.connect(self.toggle_revealed)

    def toggle_revealed(self) -> None:
        self.set_revealed(not self._revealed)

    def set_revealed(self, revealed: bool) -> None:
        self._revealed = bool(revealed)
        self.setEchoMode(
            QtWidgets.QLineEdit.EchoMode.Normal
            if self._revealed
            else QtWidgets.QLineEdit.EchoMode.Password
        )
        self._toggle_action.setToolTip("隐藏内容" if self._revealed else "显示内容")
        self._refresh_icon()

    def clear_secret(self) -> None:
        self.clear()
        self.set_revealed(False)

    def update_theme(self, tokens: ThemeTokens) -> None:
        self._icon_color = tokens.text_muted
        self._refresh_icon()

    def _refresh_icon(self) -> None:
        self._toggle_action.setIcon(make_eye_icon(self._revealed, self._icon_color))


class SegmentedControl(QtWidgets.QFrame):
    """Compact pill-style exclusive segmented selector."""

    changed = Signal(str)

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setProperty("segmented", True)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self._layout = QtWidgets.QHBoxLayout(self)
        self._layout.setContentsMargins(3, 3, 3, 3)
        self._layout.setSpacing(2)
        self._group = QtWidgets.QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QtWidgets.QPushButton] = {}
        self._icon_modes: dict[str, str] = {}
        self._current: str | None = None

    def add_option(self, key: str, label: str, icon_mode: str | None = None) -> QtWidgets.QPushButton:
        button = QtWidgets.QPushButton(label, self)
        button.setCheckable(True)
        button.setProperty("segment", True)
        button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(QtCore.Qt.FocusPolicy.TabFocus)
        button.clicked.connect(lambda _checked=False, k=key: self._handle_click(k))
        self._group.addButton(button)
        self._layout.addWidget(button)
        self._buttons[key] = button
        if icon_mode is not None:
            self._icon_modes[key] = icon_mode
            button.setIconSize(QtCore.QSize(14, 14))
        return button

    def _handle_click(self, key: str) -> None:
        if key == self._current:
            self._buttons[key].setChecked(True)
            return
        self._current = key
        self.changed.emit(key)

    def set_current(self, key: str, emit: bool = False) -> None:
        button = self._buttons.get(key)
        if button is None:
            return
        changed = key != self._current
        self._current = key
        button.setChecked(True)
        if emit and changed:
            self.changed.emit(key)

    def current(self) -> str | None:
        return self._current

    def button(self, key: str) -> QtWidgets.QPushButton | None:
        return self._buttons.get(key)

    def set_options_enabled(self, enabled: bool) -> None:
        for button in self._buttons.values():
            button.setEnabled(enabled)

    def update_theme(self, tokens: ThemeTokens) -> None:
        for key, mode in self._icon_modes.items():
            button = self._buttons[key]
            color = tokens.text_primary if button.isChecked() else tokens.text_secondary
            button.setIcon(make_theme_icon(mode, color, 14))


class DiffHighlighter(QtGui.QSyntaxHighlighter):
    """Colors additions (＋) and removals (－) in the sync preview list."""

    def __init__(self, document: QtGui.QTextDocument, tokens: ThemeTokens):
        super().__init__(document)
        self._add_format = QtGui.QTextCharFormat()
        self._del_format = QtGui.QTextCharFormat()
        self.update_theme(tokens)

    def update_theme(self, tokens: ThemeTokens) -> None:
        self._add_format = QtGui.QTextCharFormat()
        self._add_format.setForeground(QtGui.QColor(tokens.success))
        self._add_format.setFontWeight(QtGui.QFont.Weight.DemiBold)

        self._del_format = QtGui.QTextCharFormat()
        self._del_format.setForeground(QtGui.QColor(tokens.danger))
        self._del_format.setFontWeight(QtGui.QFont.Weight.DemiBold)
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:
        if text.startswith("＋ ") or text.startswith("+ "):
            self.setFormat(0, len(text), self._add_format)
        elif text.startswith("－ ") or text.startswith("- "):
            self.setFormat(0, len(text), self._del_format)


class SyncWindow(QtWidgets.QMainWindow):
    """Main AstraPod desktop window for Jellyfin sync and HerdRemote settings."""

    _job_finished = Signal(object, object, object)

    def __init__(self, theme_manager: ThemeManager, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.theme_manager = theme_manager
        self.setWindowTitle("AstraPod · 电脑配套")
        self.setMinimumSize(680, 630)
        self.resize(900, 700)

        self.source = None
        self.plan = None
        self.scopes: list = []
        self.busy = False
        self.auth_mode = "password"
        self._password_inputs: list[PasswordLineEdit] = []
        self._combo_boxes: list[ChevronComboBox] = []
        self._theme_controls: list[SegmentedControl] = []

        self._job_finished.connect(
            self._finish_job,
            QtCore.Qt.ConnectionType.QueuedConnection,
        )

        initial_error = None
        try:
            self.settings = podlink_settings.load()
        except (OSError, ValueError) as error:
            self.settings = {}
            initial_error = error

        self._build_ui()
        self.theme_manager.theme_changed.connect(self._apply_theme_assets)
        self._apply_theme_assets(self.theme_manager.mode, self.theme_manager.resolved)
        self.update_buttons()

        if initial_error is not None:
            QtCore.QTimer.singleShot(0, lambda err=initial_error: self.show_error(err))

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QtWidgets.QWidget(self)
        root_layout = QtWidgets.QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        root_layout.addWidget(self._build_header())

        self.pages = QtWidgets.QStackedWidget(root)
        self.sync_widget = self._build_sync_page()
        self.settings_widget = self._build_settings_page()
        self.pages.addWidget(self.sync_widget)
        self.pages.addWidget(self.settings_widget)
        root_layout.addWidget(self.pages, 1)

        self.setCentralWidget(root)

    def _build_header(self) -> QtWidgets.QFrame:
        header = QtWidgets.QFrame()
        header.setObjectName("HeaderBar")
        layout = QtWidgets.QHBoxLayout(header)
        layout.setContentsMargins(20, 12, 20, 12)
        layout.setSpacing(12)

        brand = QtWidgets.QHBoxLayout()
        brand.setSpacing(10)
        self.brand_icon = QtWidgets.QLabel()
        self.brand_icon.setFixedSize(30, 30)
        brand_title = QtWidgets.QLabel("AstraPod")
        brand_title.setObjectName("BrandTitle")
        brand_sub = QtWidgets.QLabel("电脑配套")
        brand_sub.setObjectName("BrandSubtitle")
        brand_sub.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        brand.addWidget(self.brand_icon)
        brand.addWidget(brand_title)
        brand.addWidget(brand_sub)
        layout.addLayout(brand)

        layout.addStretch(1)

        self.nav_switch = SegmentedControl(header)
        self.nav_switch.add_option("sync", "Jellyfin 同步")
        self.nav_switch.add_option("settings", "连接设置")
        self.nav_switch.set_current("sync")
        self.nav_switch.changed.connect(self._on_nav_changed)
        layout.addWidget(self.nav_switch)

        layout.addStretch(1)

        self.header_theme_switch = self._create_theme_control(header)
        layout.addWidget(self.header_theme_switch)
        return header

    def _create_theme_control(self, parent: QtWidgets.QWidget) -> SegmentedControl:
        control = SegmentedControl(parent)
        for mode in THEME_MODES:
            control.add_option(mode, THEME_LABELS[mode], icon_mode=mode)
        control.set_current(self.theme_manager.mode)
        control.changed.connect(self._on_theme_selected)
        self._theme_controls.append(control)
        return control

    def _build_sync_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(20, 16, 20, 16)
        outer.setSpacing(12)

        # Card 1: Jellyfin Connection & Login Mode
        conn_card, conn_layout = _make_card(spacing=10)
        conn_header = QtWidgets.QHBoxLayout()
        conn_header.setSpacing(10)
        conn_title = QtWidgets.QLabel("Jellyfin 服务器")
        conn_title.setProperty("role", "card-title")
        self.connection_badge = _make_badge("未连接", "neutral")

        self.auth_switch = SegmentedControl(conn_card)
        self.auth_switch.add_option("password", "账号密码")
        self.auth_switch.add_option("token", "访问令牌")
        self.auth_switch.set_current("password")
        self.auth_switch.changed.connect(self._on_auth_mode_changed)

        conn_header.addWidget(conn_title)
        conn_header.addWidget(self.connection_badge)
        conn_header.addStretch(1)
        conn_header.addWidget(self.auth_switch)
        conn_layout.addLayout(conn_header)

        url_row = QtWidgets.QHBoxLayout()
        url_row.setSpacing(10)
        self.url = QtWidgets.QLineEdit()
        self.url.setPlaceholderText("https://jellyfin.example.com")
        self.url.setText(os.environ.get("JELLYFIN_URL", self.settings.get("JELLYFIN_URL", "")))
        self.connect_button = QtWidgets.QPushButton("连接 Jellyfin")
        self.connect_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.connect_button.clicked.connect(self.connect_source)
        url_row.addWidget(self.url, 1)
        url_row.addWidget(self.connect_button)
        conn_layout.addLayout(url_row)

        self.auth_stack = QtWidgets.QStackedWidget()
        self.auth_stack.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )

        # Auth Page 0: Username + Password (default)
        pwd_page = QtWidgets.QWidget()
        pwd_row = QtWidgets.QHBoxLayout(pwd_page)
        pwd_row.setContentsMargins(0, 0, 0, 0)
        pwd_row.setSpacing(10)
        self.username = QtWidgets.QLineEdit()
        self.username.setPlaceholderText("Jellyfin 用户名")
        self.password = PasswordLineEdit("Jellyfin 密码（提交后立即清空）")
        self._password_inputs.append(self.password)
        pwd_row.addWidget(self.username, 1)
        pwd_row.addWidget(self.password, 1)
        self.auth_stack.addWidget(pwd_page)

        # Auth Page 1: Access Token (alternative)
        token_page = QtWidgets.QWidget()
        token_row = QtWidgets.QHBoxLayout(token_page)
        token_row.setContentsMargins(0, 0, 0, 0)
        token_row.setSpacing(10)
        self.token = PasswordLineEdit("Jellyfin 用户访问令牌（留空读取已有设置）")
        self._password_inputs.append(self.token)
        token_row.addWidget(self.token, 1)
        self.auth_stack.addWidget(token_page)

        self.auth_stack.setCurrentIndex(0)
        conn_layout.addWidget(self.auth_stack)

        self.auth_hint = QtWidgets.QLabel()
        self.auth_hint.setProperty("role", "helper")
        self.auth_hint.setWordWrap(True)
        conn_layout.addWidget(self.auth_hint)
        self._update_auth_hint()

        outer.addWidget(conn_card)

        # Card 2: Sync Scope & Device Port
        scope_card, scope_layout = _make_card(spacing=10)
        scope_header = QtWidgets.QHBoxLayout()
        scope_header.setSpacing(10)
        scope_title = QtWidgets.QLabel("同步范围与设备")
        scope_title.setProperty("role", "card-title")
        scope_tip = QtWidgets.QLabel("连接 iPod 的 PodLink USB 模式后点击预览")
        scope_tip.setProperty("role", "helper")
        scope_header.addWidget(scope_title)
        scope_header.addStretch(1)
        scope_header.addWidget(scope_tip)
        scope_layout.addLayout(scope_header)

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setSpacing(10)
        self.scope = ChevronComboBox()
        self._combo_boxes.append(self.scope)
        self.scope.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        self.scope.setPlaceholderText("请先连接 Jellyfin 选择同步范围")
        self.scope.currentIndexChanged.connect(self.invalidate_plan)

        self.port = QtWidgets.QLineEdit()
        self.port.setPlaceholderText("iPod 串口（留空自动识别）")
        self.port.textChanged.connect(self.invalidate_plan)

        self.preview_button = QtWidgets.QPushButton("预览同步")
        self.preview_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.preview_button.clicked.connect(self.preview)

        toolbar.addWidget(self.scope, 3)
        toolbar.addWidget(self.port, 2)
        toolbar.addWidget(self.preview_button)
        scope_layout.addLayout(toolbar)
        outer.addWidget(scope_card)

        # Card 3: Preview Diff Area
        preview_card, preview_layout = _make_card(spacing=10, expand_vertical=True)
        preview_header = QtWidgets.QHBoxLayout()
        preview_header.setSpacing(8)
        preview_title = QtWidgets.QLabel("同步预览")
        preview_title.setProperty("role", "card-title")
        self.metric_total = _make_badge("目标 0 首", "neutral")
        self.metric_added = _make_badge("新增 0 首", "neutral")
        self.metric_removed = _make_badge("删除 0 首", "neutral")
        preview_header.addWidget(preview_title)
        preview_header.addStretch(1)
        preview_header.addWidget(self.metric_total)
        preview_header.addWidget(self.metric_added)
        preview_header.addWidget(self.metric_removed)
        preview_layout.addLayout(preview_header)

        self.details = QtWidgets.QPlainTextEdit()
        self.details.setObjectName("DiffPreview")
        self.details.setReadOnly(True)
        self.details.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.details.setMinimumHeight(88)
        self.details.document().setDocumentMargin(10)
        self.details.setPlaceholderText(
            "连接 Jellyfin 并点击「预览同步」后，将在此显示待新增（＋）与待删除（－）的曲目清单。"
        )
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
        mono.setPointSizeF(max(9.5, mono.pointSizeF()))
        self.details.setFont(mono)
        self.diff_highlighter = DiffHighlighter(self.details.document(), self.theme_manager.tokens)
        preview_layout.addWidget(self.details, 1)
        outer.addWidget(preview_card, 1)

        # Footer Bar: Status + Progress + Apply Button
        footer = QtWidgets.QFrame()
        footer.setObjectName("FooterBar")
        footer.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        footer_layout = QtWidgets.QHBoxLayout(footer)
        footer_layout.setContentsMargins(18, 12, 18, 12)
        footer_layout.setSpacing(14)

        status_col = QtWidgets.QVBoxLayout()
        status_col.setSpacing(6)
        self.status = QtWidgets.QLabel("同步使用 iPod 的 PodLink USB 模式。")
        self.status.setWordWrap(True)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        status_col.addWidget(self.status)
        status_col.addWidget(self.progress)

        self.apply_button = QtWidgets.QPushButton("应用同步")
        self.apply_button.setProperty("variant", "primary")
        self.apply_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.apply_button.setMinimumWidth(116)
        self.apply_button.clicked.connect(self.confirm)

        footer_layout.addLayout(status_col, 1)
        footer_layout.addWidget(self.apply_button)
        outer.addWidget(footer)

        # Invalidate source when credential inputs change; Enter triggers connect
        self.url.textChanged.connect(self.invalidate_source)
        self.username.textChanged.connect(self.invalidate_source)
        self.password.textChanged.connect(self.invalidate_source)
        self.token.textChanged.connect(self.invalidate_source)

        for field in (self.url, self.username, self.password, self.token):
            field.returnPressed.connect(self._on_connect_return)

        return page

    def _build_settings_page(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        page_layout = QtWidgets.QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)
        layout.setContentsMargins(22, 18, 22, 16)
        layout.setSpacing(14)

        # Card 1: Appearance / Theme
        theme_card, theme_layout = _make_card(spacing=10)
        theme_header = QtWidgets.QHBoxLayout()
        theme_title = QtWidgets.QLabel("界面外观")
        theme_title.setProperty("role", "card-title")
        self.theme_effective_badge = _make_badge("", "accent")
        theme_header.addWidget(theme_title)
        theme_header.addStretch(1)
        theme_header.addWidget(self.theme_effective_badge)
        theme_layout.addLayout(theme_header)

        theme_row = QtWidgets.QHBoxLayout()
        theme_row.setSpacing(12)
        theme_label = QtWidgets.QLabel("颜色主题")
        theme_label.setProperty("role", "field-label")
        self.settings_theme_switch = self._create_theme_control(theme_card)
        theme_row.addWidget(theme_label)
        theme_row.addStretch(1)
        theme_row.addWidget(self.settings_theme_switch)
        theme_layout.addLayout(theme_row)

        theme_helper = QtWidgets.QLabel(
            "支持跟随系统配色或手动切换浅色、深色模式。主题偏好保存在独立的界面配置文件中。"
        )
        theme_helper.setProperty("role", "helper")
        theme_helper.setWordWrap(True)
        theme_layout.addWidget(theme_helper)
        layout.addWidget(theme_card)

        self.entries: dict[str, QtWidgets.QWidget] = {}

        # Card 2: Media & Players + Private Tokens
        media_card, media_layout = _make_card(spacing=12)
        media_title = QtWidgets.QLabel("媒体服务与播放器")
        media_title.setProperty("role", "card-title")
        media_layout.addWidget(media_title)
        media_form = self._build_settings_form(
            [key for key in MEDIA_SETTING_KEYS if key in podlink_settings.FIELDS]
        )
        media_layout.addLayout(media_form)

        divider = QtWidgets.QFrame()
        divider.setObjectName("Divider")
        media_layout.addWidget(divider)

        token_subtitle = QtWidgets.QLabel("播放器私密令牌")
        token_subtitle.setProperty("role", "section-subtitle")
        media_layout.addWidget(token_subtitle)

        token_form = QtWidgets.QFormLayout()
        token_form.setHorizontalSpacing(14)
        token_form.setVerticalSpacing(10)
        token_form.setLabelAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        token_form.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        self.player_tokens: list[PasswordLineEdit] = []
        for player in ("Cider RPC", "Rufin"):
            field = PasswordLineEdit(f"{player} 令牌（留空保留原设置）")
            self._password_inputs.append(field)
            lbl = QtWidgets.QLabel(f"{player} 令牌")
            lbl.setProperty("role", "field-label")
            lbl.setMinimumWidth(210)
            token_form.addRow(lbl, field)
            self.player_tokens.append(field)
        media_layout.addLayout(token_form)
        layout.addWidget(media_card)

        # Card 3: Agent / Voice / Usage
        agent_keys = [key for key in AGENT_SETTING_KEYS if key in podlink_settings.FIELDS]
        if agent_keys:
            agent_card, agent_layout = _make_card(spacing=12)
            agent_title = QtWidgets.QLabel("Agent、语音与 Usage")
            agent_title.setProperty("role", "card-title")
            agent_layout.addWidget(agent_title)
            agent_layout.addLayout(self._build_settings_form(agent_keys))
            layout.addWidget(agent_card)

        # Fallback card for any additional fields defined in podlink_settings.FIELDS
        known = set(MEDIA_SETTING_KEYS) | set(AGENT_SETTING_KEYS)
        extra_keys = [key for key in podlink_settings.FIELDS if key not in known]
        if extra_keys:
            extra_card, extra_layout = _make_card(spacing=12)
            extra_title = QtWidgets.QLabel("其他设置")
            extra_title.setProperty("role", "card-title")
            extra_layout.addWidget(extra_title)
            extra_layout.addLayout(self._build_settings_form(extra_keys))
            layout.addWidget(extra_card)

        layout.addStretch(1)
        scroll.setWidget(container)
        page_layout.addWidget(scroll, 1)

        # Pinned Footer Bar for Settings
        footer_wrap = QtWidgets.QWidget()
        footer_wrap_layout = QtWidgets.QVBoxLayout(footer_wrap)
        footer_wrap_layout.setContentsMargins(22, 8, 22, 18)
        footer_wrap_layout.setSpacing(0)

        footer = QtWidgets.QFrame()
        footer.setObjectName("FooterBar")
        footer.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Fixed,
        )
        footer_layout = QtWidgets.QHBoxLayout(footer)
        footer_layout.setContentsMargins(18, 12, 18, 12)
        footer_layout.setSpacing(14)

        self.settings_status = QtWidgets.QLabel(
            "Cider 和 Rufin 由 HerdRemote 控制。设置会在新版 HerdRemote 下次启动时读取。"
        )
        self.settings_status.setWordWrap(True)
        self.save_button = QtWidgets.QPushButton("保存设置")
        self.save_button.setProperty("variant", "primary")
        self.save_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.save_button.setMinimumWidth(116)
        self.save_button.clicked.connect(self.save)

        footer_layout.addWidget(self.settings_status, 1)
        footer_layout.addWidget(self.save_button)
        footer_wrap_layout.addWidget(footer)
        page_layout.addWidget(footer_wrap)

        return page

    def _build_settings_form(self, keys: list[str]) -> QtWidgets.QFormLayout:
        form = QtWidgets.QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
        )
        form.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )

        for key in keys:
            label_text, default = podlink_settings.FIELDS[key]
            value = self.settings.get(key, os.environ.get(key, default))
            label = QtWidgets.QLabel(label_text)
            label.setProperty("role", "field-label")
            label.setMinimumWidth(210)

            choices = SETTING_CHOICES.get(key)
            if choices is not None:
                widget = ChevronComboBox()
                self._combo_boxes.append(widget)
                widget.setEditable(True)
                widget.addItems(list(choices))
                widget.setCurrentText(value)
                widget.setSizePolicy(
                    QtWidgets.QSizePolicy.Policy.Expanding,
                    QtWidgets.QSizePolicy.Policy.Fixed,
                )
            else:
                widget = QtWidgets.QLineEdit(value)
                placeholder = SETTING_PLACEHOLDERS.get(key)
                if placeholder:
                    widget.setPlaceholderText(placeholder)

            form.addRow(label, widget)
            self.entries[key] = widget
        return form

    # ------------------------------------------------------------------
    # Theme & Navigation Handlers
    # ------------------------------------------------------------------
    def _on_nav_changed(self, page_key: str) -> None:
        self.pages.setCurrentIndex(0 if page_key == "sync" else 1)

    def _on_theme_selected(self, mode: str) -> None:
        try:
            self.theme_manager.apply(mode, persist=True)
        except OSError as error:
            self.show_error(error)

    def _apply_theme_assets(self, mode: str, resolved: str) -> None:
        tokens = self.theme_manager.tokens
        for control in self._theme_controls:
            control.set_current(mode, emit=False)
            control.update_theme(tokens)

        app_icon = make_app_icon(tokens, 30)
        self.brand_icon.setPixmap(app_icon.pixmap(30, 30))
        self.setWindowIcon(app_icon)

        for field in self._password_inputs:
            field.update_theme(tokens)

        for combo in self._combo_boxes:
            combo.update_theme(tokens)

        self.diff_highlighter.update_theme(tokens)
        effective_text = RESOLVED_LABELS.get(resolved, resolved)
        if mode == "system":
            _set_badge(self.theme_effective_badge, f"随系统 · 当前{effective_text}", "accent")
        else:
            _set_badge(self.theme_effective_badge, f"当前：{effective_text}", "accent")

    def _on_auth_mode_changed(self, mode: str) -> None:
        if mode not in ("password", "token"):
            return
        self.auth_mode = mode
        self.auth_stack.setCurrentIndex(0 if mode == "password" else 1)
        self._update_auth_hint()
        self.invalidate_source()

    def _update_auth_hint(self) -> None:
        if self.auth_mode == "password":
            self.auth_hint.setText(
                "默认使用账号密码登录（仅限 HTTPS）。密码在提交后立即清空，不会保存或写入日志。"
            )
        else:
            self.auth_hint.setText(
                "使用 Jellyfin 用户访问令牌登录（仅限 HTTPS）。令牌仅保留在当前窗口会话中，留空将读取已有设置。"
            )

    def _on_connect_return(self) -> None:
        if not self.busy:
            self.connect_source()

    # ------------------------------------------------------------------
    # Settings Persistence
    # ------------------------------------------------------------------
    @staticmethod
    def _entry_text(widget: QtWidgets.QWidget) -> str:
        if isinstance(widget, QtWidgets.QComboBox):
            return widget.currentText()
        if isinstance(widget, QtWidgets.QLineEdit):
            return widget.text()
        raise TypeError(f"unsupported setting widget: {type(widget).__name__}")

    def save(self) -> None:
        values = {key: self._entry_text(widget).strip() for key, widget in self.entries.items()}
        try:
            if values.get("JELLYFIN_URL"):
                https_base(values["JELLYFIN_URL"])
            if values.get("RUFIN_API_URL"):
                loopback_url(values["RUFIN_API_URL"])
            if values.get("CIDER_API_URL"):
                cider = urlsplit(values["CIDER_API_URL"])
                if (cider.scheme not in ("http", "https") or not cider.hostname
                        or cider.username is not None or cider.query or cider.fragment):
                    raise ValueError("Cider API URL is invalid")
            for key, choices in SETTING_CHOICES.items():
                if values.get(key) and values[key] not in choices:
                    raise ValueError(f"{key}: {', '.join(choices)}")
            podlink_settings.save_player_tokens(*(field.text().strip()
                                                  for field in self.player_tokens))
            podlink_settings.save(values)
        except (OSError, ValueError) as error:
            self.show_error(error)
            return

        for field in self.player_tokens:
            field.clear_secret()
        self.settings = values
        if values.get("JELLYFIN_URL", "") != self.url.text():
            self.url.setText(values.get("JELLYFIN_URL", ""))
        self.settings_status.setText("设置已保存。新版 HerdRemote 会在下次启动时读取。")

    # ------------------------------------------------------------------
    # Dialogs & Window Lifecycle
    # ------------------------------------------------------------------
    def show_error(self, error) -> None:
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Icon.Critical)
        box.setWindowTitle("操作失败")
        box.setText("操作失败")
        box.setInformativeText(str(error))
        ok_btn = box.addButton("确定", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(ok_btn)
        box.exec()

    def build_confirm_dialog(self, plan):
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setWindowTitle("确认删除同步副本")
        box.setText(f"删除 iPod 上的 {len(plan.removals)} 首同步副本？")
        box.setInformativeText("只删除预览列出的同步副本。")
        cancel_btn = box.addButton("取消", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        apply_btn = box.addButton("应用同步", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        apply_btn.setProperty("variant", "danger")
        box.setDefaultButton(cancel_btn)
        box.setEscapeButton(cancel_btn)
        return box, cancel_btn, apply_btn

    def confirm_removals(self, plan) -> bool:
        box, _cancel_btn, apply_btn = self.build_confirm_dialog(plan)
        box.exec()
        return box.clickedButton() is apply_btn

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self.busy:
            event.ignore()
            return
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Sync State Machine & Background Worker
    # ------------------------------------------------------------------
    def update_buttons(self) -> None:
        interactive = not self.busy
        for control in (
            self.url,
            self.username,
            self.password,
            self.token,
            self.scope,
            self.port,
            self.connect_button,
        ):
            control.setEnabled(interactive)
        self.auth_switch.set_options_enabled(interactive)

        scope_idx = self.scope.currentIndex()
        can_preview = interactive and self.source is not None and 0 <= scope_idx < len(self.scopes)
        self.preview_button.setEnabled(can_preview)

        can_apply = (
            interactive
            and self.plan is not None
            and bool(self.plan.additions or self.plan.removals)
        )
        self.apply_button.setEnabled(can_apply)
        self.progress.setVisible(self.busy)

        if self.busy:
            _set_badge(self.connection_badge, "处理中…", "busy")
        elif self.source is not None:
            _set_badge(self.connection_badge, f"已连接 · {len(self.scopes)} 项范围", "success")
        else:
            _set_badge(self.connection_badge, "未连接", "neutral")

    def _update_metrics(self, plan) -> None:
        if plan is None:
            _set_badge(self.metric_total, "目标 0 首", "neutral")
            _set_badge(self.metric_added, "新增 0 首", "neutral")
            _set_badge(self.metric_removed, "删除 0 首", "neutral")
            return
        added_count = len(plan.additions)
        removed_count = len(plan.removals)
        _set_badge(self.metric_total, f"目标 {len(plan.tracks)} 首", "neutral")
        _set_badge(
            self.metric_added,
            f"新增 {added_count} 首",
            "success" if added_count else "neutral",
        )
        _set_badge(
            self.metric_removed,
            f"删除 {removed_count} 首",
            "danger" if removed_count else "neutral",
        )

    def invalidate_source(self, *_args) -> None:
        self.source = None
        self.invalidate_plan()

    def invalidate_plan(self, *_args) -> None:
        self.plan = None
        self.details.clear()
        self._update_metrics(None)
        self.update_buttons()

    def job(self, status: str, action, success) -> None:
        self.busy = True
        self.status.setText(status)
        self.update_buttons()

        def work() -> None:
            try:
                result = action()
            except (OSError, RuntimeError, ValueError, TypeError) as error:
                self._job_finished.emit(success, None, error)
            else:
                self._job_finished.emit(success, result, None)

        Thread(target=work, daemon=True).start()

    def _finish_job(self, success, result, error) -> None:
        self.busy = False
        if error is not None:
            self.invalidate_plan()
            self.status.setText("操作失败。")
            self.show_error(error)
        else:
            success(result)
        self.update_buttons()

    def connect_source(self) -> None:
        if self.busy:
            return
        url = self.url.text()
        auth_mode = self.auth_mode

        if auth_mode == "password":
            username = self.username.text()
            password = self.password.text()
            # Clear password immediately after capture; never persist or log.
            self.password.clear_secret()

            def connect():
                factory = getattr(hrmediasync.JellyfinSource, "with_password", None)
                if factory is None:
                    raise RuntimeError(
                        "当前后端版本不支持 Jellyfin 账号密码登录，请改用访问令牌或更新后端。"
                    )
                source = factory(url, username, password)
                return source, source.scopes()

            status = "正在登录 Jellyfin 并读取曲库和播放列表…"
        else:
            token = self.token.text()

            def connect():
                source = hrmediasync.JellyfinSource(url, token)
                return source, source.scopes()

            status = "正在读取 Jellyfin 曲库和播放列表…"

        self.source = None
        self.invalidate_plan()
        self.job(status, connect, self.connected)

    def connected(self, result) -> None:
        self.source, self.scopes = result
        blocked = self.scope.blockSignals(True)
        try:
            self.scope.clear()
            for scope in self.scopes:
                prefix = SCOPE_PREFIXES.get(scope.kind, "")
                self.scope.addItem(prefix + scope.label)
            if self.scopes:
                self.scope.setCurrentIndex(0)
        finally:
            self.scope.blockSignals(blocked)
        self.status.setText("Jellyfin 已连接。选择同步范围并预览。")

    def preview(self) -> None:
        idx = self.scope.currentIndex()
        if self.busy or self.source is None or not (0 <= idx < len(self.scopes)):
            return
        scope = self.scopes[idx]
        source, port = self.source, self.port.text().strip()
        self.job(
            "正在比较 Jellyfin 和 iPod 曲库…",
            lambda: hrmediasync.preview(source, scope, port),
            self.show_plan,
        )

    def show_plan(self, plan) -> None:
        self.plan = plan
        rows = [f"＋ {track.label}" for track in plan.additions]
        rows.extend(f"＝ {track.label}（iPod 已有）" for track in plan.originals)
        rows.extend(f"－ {name}" for name in sorted(plan.removals))
        self.details.setPlainText("\n".join(rows))
        self._update_metrics(plan)
        self.status.setText(
            f"目标 {len(plan.tracks)} 首。新增 {len(plan.additions)} 首，"
            f"匹配 iPod 原有曲目 {len(plan.originals)} 首，删除 {len(plan.removals)} 首。"
        )

    def confirm(self) -> None:
        plan = self.plan
        if plan is None or self.source is None or self.busy:
            return
        if not plan.removals:
            self.sync(plan)
            return
        if self.confirm_removals(plan) and self.plan is plan and self.source is not None and not self.busy:
            self.sync(plan)

    def sync(self, plan) -> None:
        source, port = self.source, self.port.text().strip()
        self.job(
            "正在同步。高采样率音频会在电脑端转换…",
            lambda: hrmediasync.apply(source, plan, port),
            self.sync_complete,
        )

    def sync_complete(self, result) -> None:
        added, removed = result
        self.invalidate_plan()
        self.status.setText(f"已同步。写入 {added} 首，删除 {removed} 首。")


def create_application(argv: list[str] | None = None) -> tuple[QtWidgets.QApplication, ThemeManager, SyncWindow]:
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("AstraPod")
    app.setOrganizationName("PodBox")
    theme_manager = ThemeManager(app)
    theme_manager.apply(theme_manager.mode, persist=False)
    window = SyncWindow(theme_manager)
    return app, theme_manager, window


def main(argv: list[str] | None = None) -> int:
    app, _theme_manager, window = create_application(argv)
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    sys.exit(main())
