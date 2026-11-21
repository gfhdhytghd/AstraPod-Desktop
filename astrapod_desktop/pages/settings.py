"""Settings: appearance, the iPod port, the remote's media services, T3, agents and voice."""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import podlink_settings
from podlink_ui_theme import (COLOR_SOURCE_LABELS, COLOR_SOURCES, RESOLVED_LABELS,
                              SEED_SWATCHES, THEME_LABELS, THEME_MODES)

from .. import icons
from ..qt import QtCore, QtGui, QtWidgets, Qt
from ..widgets import (ChevronComboBox, PasswordLineEdit, SegmentedControl, ToggleSwitch,
                       PageHeader, button, label, make_badge, make_card, set_badge)
from . import Page
from .port import PortPicker

SETTING_CHOICES = {
    "CIDER_BACKEND": ("plugin", "auto", "rpc"),
    "HERDREMOTE_VOICE_FOCUS": ("on", "off"),
    "HERDREMOTE_KEY_TOOL": ("auto", "ydotool", "wtype"),
    "T3_CONTROL": ("on", "off"),
    "T3_FOLLOW_IPOD": ("on", "off"),
    "AGENT_CONTROL": ("herdr", "t3"),
}
AGENT_CONTROL_LABELS = {"herdr": "herdr", "t3": "T3 Code"}
AGENT_CONTROL_HELP = {
    "herdr": "iPod 的 Agents 和这里的 Agent 页只显示 herdr 的工作区和 Agent。",
    "t3": "iPod 的 Agents 和这里的 Agent 页只显示 T3 Code 的项目和线程。",
}
MEDIA_SETTING_KEYS = ("JELLYFIN_URL", "CIDER_API_URL", "CIDER_BACKEND", "RUFIN_API_URL")
# Shown only while Agent control is set to herdr.
HERDR_ONLY_KEYS = ("HERDR_SOCKET_PATH", "HERDREMOTE_VOICE_FOCUS")
AGENT_SETTING_KEYS = ("HERDR_SOCKET_PATH", "HERDREMOTE_VOICE_FOCUS",
                      "HERDREMOTE_USAGE_CACHE_DIR", "HERDREMOTE_KEY_TOOL")
SETTING_PLACEHOLDERS = {
    "T3_BIN": "留空自动查找 t3",
    "JELLYFIN_URL": "https://jellyfin.example.com",
    "CIDER_API_URL": "http://127.0.0.1:10767",
    "RUFIN_API_URL": "http://127.0.0.1:1717（留空自动识别）",
    "HERDR_SOCKET_PATH": "例如 /run/user/1000/herdr.sock",
    "HERDREMOTE_USAGE_CACHE_DIR": "留空使用默认缓存目录",
}


QUICK_REPLY_DEFAULT = "Continue\nYes\nNo\n/compact"
QUICK_REPLY_LIMIT = 8
QUICK_REPLY_BYTES = 60


def quick_reply_problem(text: str) -> str | None:
    """Why this quick-reply list cannot be saved, or None."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) > QUICK_REPLY_LIMIT:
        return f"最多 {QUICK_REPLY_LIMIT} 条，现在有 {len(lines)} 条。"
    for number, line in enumerate(lines, 1):
        if len(line.encode("utf-8")) > QUICK_REPLY_BYTES:
            return f"第 {number} 条超过 {QUICK_REPLY_BYTES} 字节（中文每字 3 字节）。"
    return None


def validate(values: dict) -> None:
    from hrjellyfin import https_base
    from hrrufin import loopback_url
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
    problem = quick_reply_problem(values.get("AGENT_QUICK_REPLIES", ""))
    if problem:
        raise ValueError(f"快捷回复：{problem}")


class SettingsPage(Page):
    key = "settings"

    def __init__(self, window, parent=None):
        super().__init__(window, parent)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.entries: dict[str, QtWidgets.QWidget] = {}
        self.combos: list[ChevronComboBox] = []
        self.password_inputs: list[PasswordLineEdit] = []
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        host = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(host)
        layout.setContentsMargins(28, 18, 28, 12)
        layout.setSpacing(14)
        layout.addWidget(PageHeader("设置"))

        look, look_layout = make_card(10)
        head = QtWidgets.QHBoxLayout()
        head.addWidget(label("外观", "card-title"))
        head.addStretch(1)
        self.theme_badge = make_badge("", "accent")
        head.addWidget(self.theme_badge)
        look_layout.addLayout(head)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(label("颜色主题", "field-label"))
        row.addStretch(1)
        self.theme_switch = SegmentedControl()
        for mode in THEME_MODES:
            self.theme_switch.add_option(mode, THEME_LABELS[mode], icon_mode=mode)
        self.theme_switch.set_current(window.theme_manager.mode)
        self.theme_switch.changed.connect(window.select_theme)
        row.addWidget(self.theme_switch)
        look_layout.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        column = QtWidgets.QVBoxLayout()
        column.setSpacing(2)
        column.addWidget(label("界面颜色", "field-label"))
        self.color_source_help = label("", "helper", wrap=True)
        column.addWidget(self.color_source_help)
        row.addLayout(column, 1)
        self.color_source = SegmentedControl()
        for source in COLOR_SOURCES:
            self.color_source.add_option(source, COLOR_SOURCE_LABELS[source])
        self.color_source.set_current(window.color_source)
        self.color_source.changed.connect(window.set_color_source)
        row.addWidget(self.color_source)
        look_layout.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        column = QtWidgets.QVBoxLayout()
        column.setSpacing(2)
        column.addWidget(label("自选颜色", "field-label"))
        column.addWidget(label("点一个颜色即改用自选颜色。", "helper"))
        row.addLayout(column)
        row.addStretch(1)
        self.swatches = {}
        group = QtWidgets.QButtonGroup(self)
        group.setExclusive(True)
        self.swatch_group = group
        for seed in SEED_SWATCHES:
            swatch = SeedSwatch(seed)
            swatch.clicked.connect(lambda _on=False, s=seed: self._pick_seed(s))
            group.addButton(swatch)
            row.addWidget(swatch)
            self.swatches[seed] = swatch
        look_layout.addLayout(row)
        self.show_color_source()
        row = QtWidgets.QHBoxLayout()
        row.addWidget(label("界面动画（翻页、涟漪和指示器）", "field-label"))
        row.addStretch(1)
        self.motion = ToggleSwitch()
        self.motion.setAccessibleName("界面动画")
        self.motion.setChecked(window.motion)
        self.motion.toggled.connect(window.set_motion)
        row.addWidget(self.motion)
        look_layout.addLayout(row)
        layout.addWidget(look)

        device, device_layout = make_card(10)
        device_layout.addWidget(label("iPod", "card-title"))
        form = self._form()
        self.port_picker = PortPicker(window)
        self.port = self.port_picker.field
        self.port_badge = self.port_picker.badge
        self.port_auto_button = self.port_picker.auto_button
        self.port_detect_button = self.port_picker.detect_button
        self.port_help = self.port_picker.help
        form.addRow(self._caption("iPod 串口"), self.port_picker)
        device_layout.addLayout(form)
        device_layout.addWidget(self.port_help)
        device_layout.addWidget(label("读取资料库、导入和同步时，电脑会暂时从后台远程服务借用这个串口。"
                                      "连接了多台 iPod 时，也可以点右上角的 iPod 状态选择一台。",
                                      "helper", wrap=True))
        layout.addWidget(device)

        media, media_layout = make_card(12)
        media_layout.addWidget(label("iPod 遥控用的媒体服务", "card-title"))
        media_layout.addWidget(label(
            "后台远程服务用这些地址，让 iPod 浏览并控制电脑上的 Cider、Rufin 和 Jellyfin。"
            "AstraPod 本身不播放音乐；Jellyfin 地址也是同步页的默认服务器。", "helper", wrap=True))
        media_layout.addLayout(self._settings_form(
            [k for k in MEDIA_SETTING_KEYS if k in podlink_settings.FIELDS]))
        divider = QtWidgets.QFrame()
        divider.setObjectName("Divider")
        media_layout.addWidget(divider)
        media_layout.addWidget(label("Cider 与 Rufin 令牌", "section-subtitle"))
        token_form = self._form()
        self.player_tokens: list[PasswordLineEdit] = []
        for player in ("Cider RPC", "Rufin"):
            field = PasswordLineEdit(f"{player} 令牌（留空保留原设置）")
            field.setAccessibleName(f"{player} 令牌")
            self.password_inputs.append(field)
            token_form.addRow(self._caption(f"{player} 令牌"), field)
            self.player_tokens.append(field)
        media_layout.addLayout(token_form)
        layout.addWidget(media)

        agent_keys = [k for k in AGENT_SETTING_KEYS if k in podlink_settings.FIELDS]
        self.agent_control = None
        self.agent_form = None
        if agent_keys or "AGENT_CONTROL" in podlink_settings.FIELDS:
            agents, agents_layout = make_card(12)
            agents_layout.addWidget(label("Agent、语音与 Usage", "card-title"))
            if "AGENT_CONTROL" in podlink_settings.FIELDS:
                agents_layout.addLayout(self._agent_control_row())
            if agent_keys:
                self.agent_form = self._settings_form(agent_keys)
                agents_layout.addLayout(self.agent_form)
            layout.addWidget(agents)
        self.t3_card = self._t3_card()
        layout.addWidget(self.t3_card)
        self._agent_control_changed()
        if "AGENT_QUICK_REPLIES" in podlink_settings.FIELDS:
            layout.addWidget(self._quick_reply_card())
        known = set(MEDIA_SETTING_KEYS) | set(AGENT_SETTING_KEYS) | {
            "T3_CONTROL", "T3_FOLLOW_IPOD", "T3_BIN", "AGENT_QUICK_REPLIES", "AGENT_CONTROL"}
        extra = [k for k in podlink_settings.FIELDS if k not in known]
        if extra:
            other, other_layout = make_card(12)
            other_layout.addWidget(label("其他设置", "card-title"))
            other_layout.addLayout(self._settings_form(extra))
            layout.addWidget(other)

        services, services_layout = make_card(10)
        services_layout.addWidget(label("后台服务", "card-title"))
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        self.remote_badge = make_badge("")
        self.daemon_badge = make_badge("")
        self.plugin_badge = make_badge("")
        self.restart_button = button("重新启动", None, "refresh", window.restart_remote)
        for row_index, (caption, badge) in enumerate((("HerdRemote 远程服务", self.remote_badge),
                                                      ("桌面接口", self.daemon_badge),
                                                      ("输入插件宿主", self.plugin_badge))):
            grid.addWidget(label(caption, "field-label"), row_index, 0)
            grid.addWidget(badge, row_index, 1, Qt.AlignmentFlag.AlignLeft)
        grid.addWidget(self.restart_button, 0, 2)
        grid.setColumnStretch(1, 1)
        services_layout.addLayout(grid)
        layout.addWidget(services)
        layout.addStretch(1)
        scroll.setWidget(host)
        self.layout_.addWidget(scroll, 1)

        footer_wrap = QtWidgets.QWidget()
        wrap = QtWidgets.QVBoxLayout(footer_wrap)
        wrap.setContentsMargins(28, 6, 28, 18)
        footer = QtWidgets.QFrame()
        footer.setObjectName("FooterBar")
        footer_layout = QtWidgets.QHBoxLayout(footer)
        footer_layout.setContentsMargins(18, 12, 18, 12)
        self.status = label("保存后，后台远程服务会重新读取设置。",
                            None, wrap=True)
        footer_layout.addWidget(self.status, 1)
        self.save_button = button("保存设置", "primary", None, self.save)
        self.save_button.setMinimumWidth(116)
        footer_layout.addWidget(self.save_button)
        wrap.addWidget(footer)
        self.layout_.addWidget(footer_wrap)
        self.update_services()

    # iPod port ---------------------------------------------------------
    def detect_port(self) -> None:
        self.port_picker.detect()

    def port_override(self) -> str:
        """The explicit port to save; empty means auto-detect."""
        return self.port_picker.override()

    # Agent control ---------------------------------------------------------
    def _agent_control_row(self) -> QtWidgets.QHBoxLayout:
        row = QtWidgets.QHBoxLayout()
        column = QtWidgets.QVBoxLayout()
        column.setSpacing(2)
        column.addWidget(label("Agent 控制", "field-label"))
        self.agent_control_help = label("", "helper", wrap=True)
        column.addWidget(self.agent_control_help)
        row.addLayout(column, 1)
        self.agent_control = SegmentedControl()
        for key in SETTING_CHOICES["AGENT_CONTROL"]:
            self.agent_control.add_option(key, AGENT_CONTROL_LABELS[key])
        value = self.window_.settings.get("AGENT_CONTROL",
                                          os.environ.get("AGENT_CONTROL") or "herdr")
        self.agent_control.set_current(value if value in AGENT_CONTROL_LABELS else "herdr")
        self.agent_control.changed.connect(lambda _k: self._apply_agent_control())
        row.addWidget(self.agent_control)
        self.entries["AGENT_CONTROL"] = self.agent_control
        self.agent_control_help.setText(AGENT_CONTROL_HELP[self.agent_control.current()])
        return row

    def _apply_agent_control(self) -> None:
        """Agent control takes effect at once, without the rest of the form."""
        self._agent_control_changed()
        try:
            values = podlink_settings.load()
            values["AGENT_CONTROL"] = self.agent_control.current()
            podlink_settings.save(values)
        except (OSError, ValueError) as error:
            self.window_.show_error(error)
            return
        self.window_.settings_saved({**self.window_.settings,
                                     "AGENT_CONTROL": values["AGENT_CONTROL"]})

    def _agent_control_changed(self) -> None:
        if self.agent_control is None:
            return
        control = self.agent_control.current()
        self.agent_control_help.setText(AGENT_CONTROL_HELP[control])
        if self.agent_form is not None:
            for key in HERDR_ONLY_KEYS:
                if key in self.entries:
                    self.agent_form.setRowVisible(self.entries[key], control == "herdr")
        self.t3_card.setVisible(control == "t3")
        self._t3_update()

    # T3 Code ------------------------------------------------------------
    def _t3_card(self) -> QtWidgets.QFrame:
        card, card_layout = make_card(10)
        head = QtWidgets.QHBoxLayout()
        head.addWidget(label("T3 Code", "card-title"))
        head.addStretch(1)
        self.t3_badge = make_badge("未连接")
        head.addWidget(self.t3_badge)
        card_layout.addLayout(head)
        self.t3_usage = label("", "helper", wrap=True)
        card_layout.addWidget(self.t3_usage)
        self.t3_switch = None
        if "T3_CONTROL" in podlink_settings.FIELDS and self.agent_control is None:
            row = QtWidgets.QHBoxLayout()
            row.addWidget(label("在 iPod 上控制 T3", "field-label"))
            row.addStretch(1)
            self.t3_switch = ToggleSwitch()
            self.t3_switch.setAccessibleName("在 iPod 上控制 T3")
            value = self.window_.settings.get("T3_CONTROL", os.environ.get("T3_CONTROL") or "on")
            self.t3_switch.setChecked(value != "off")
            self.t3_switch.toggled.connect(lambda _on: self._t3_update())
            row.addWidget(self.t3_switch)
            card_layout.addLayout(row)
            self.entries["T3_CONTROL"] = self.t3_switch
        self.t3_follow_switch = None
        if "T3_FOLLOW_IPOD" in podlink_settings.FIELDS:
            row = QtWidgets.QHBoxLayout()
            column = QtWidgets.QVBoxLayout()
            column.setSpacing(2)
            column.addWidget(label("T3 跟随 iPod 选择", "field-label"))
            column.addWidget(label("在 iPod 上选中 T3 线程时，电脑上的 T3 窗口切到同一线程。不会把窗口提到最前。",
                                   "helper", wrap=True))
            row.addLayout(column, 1)
            self.t3_follow_switch = ToggleSwitch()
            self.t3_follow_switch.setAccessibleName("T3 跟随 iPod 选择")
            value = self.window_.settings.get("T3_FOLLOW_IPOD",
                                              os.environ.get("T3_FOLLOW_IPOD") or "on")
            self.t3_follow_switch.setChecked(value != "off")
            row.addWidget(self.t3_follow_switch)
            card_layout.addLayout(row)
            self.entries["T3_FOLLOW_IPOD"] = self.t3_follow_switch
        status = QtWidgets.QHBoxLayout()
        self.t3_status = label("未连接", None, wrap=True)
        status.addWidget(self.t3_status, 1)
        self.t3_connect_button = button("连接 T3", "primary", None, self.t3_connect)
        self.t3_disconnect_button = button("断开 T3", None, None, self.t3_disconnect)
        status.addWidget(self.t3_disconnect_button)
        status.addWidget(self.t3_connect_button)
        card_layout.addLayout(status)
        self.t3_error = label("", "error", wrap=True)
        self.t3_error.setVisible(False)
        card_layout.addWidget(self.t3_error)
        if "T3_BIN" in podlink_settings.FIELDS:
            card_layout.addLayout(self._settings_form(["T3_BIN"]))
        self.t3_busy = False
        self.window_.daemon.state_changed.connect(lambda _s: self._t3_update())
        self._t3_update()
        return card

    def _t3_update(self) -> None:
        t3 = self.window_.daemon.state.get("t3") or {}
        connected = bool(t3.get("connected"))
        session = bool(t3.get("session_label"))
        saved = (self.window_.daemon.state.get("agents") or {}).get("control") == "t3"
        if not t3:
            set_badge(self.t3_badge, "未连接", "neutral")
            self.t3_status.setText("后台远程服务还没有报告 T3 的状态。")
        elif t3.get("error"):
            set_badge(self.t3_badge, "连接出错", "danger")
            self.t3_status.setText(str(t3["error"]))
        elif connected:
            set_badge(self.t3_badge, "已连接", "success")
            parts = [f"T3 {t3['server_version']}" if t3.get("server_version") else "T3"]
            if t3.get("session_label"):
                parts.append(f"会话：{t3['session_label']}")
            self.t3_status.setText("，".join(parts))
        elif session and not saved:
            set_badge(self.t3_badge, "未连接", "neutral")
            self.t3_status.setText("已为 iPod 签发 T3 会话，正在切换 Agent 控制。")
        elif session:
            set_badge(self.t3_badge, "正在连接", "busy")
            self.t3_status.setText("已为 iPod 签发 T3 会话，正在连接 T3。")
        else:
            set_badge(self.t3_badge, "未连接", "neutral")
            self.t3_status.setText("还没有为 iPod 签发 T3 会话。")
        allowed = self.t3_switch is None or self.t3_switch.isChecked()
        if self.agent_control is not None:
            self.t3_usage.setText("连接后，iPod 的 Agents 显示 T3 项目和线程。")
        else:
            self.t3_usage.setText("连接后，iPod 的 Agents 中会出现 T3 项目和线程。电脑端只保存连接。")
        if self.t3_follow_switch is not None:
            self.t3_follow_switch.setEnabled(allowed)
        online = self.window_.daemon.connected
        self.t3_connect_button.setEnabled(online and allowed and not session and not self.t3_busy)
        self.t3_disconnect_button.setEnabled(online and session and not self.t3_busy)

    def _t3_request(self, op: str, done_text: str) -> None:
        if self.t3_busy:
            return
        self.t3_busy = True
        self.t3_error.setVisible(False)
        self._t3_update()

        def finished(ok, result):
            self.t3_busy = False
            if ok:
                self.window_.flash(done_text)
            else:
                self.t3_error.setText(f"没有完成：{result}")
                self.t3_error.setVisible(True)
            self._t3_update()

        self.window_.daemon.request(op, finished)

    def t3_connect(self) -> None:
        self._t3_request("t3_connect", "已为 iPod 签发 T3 会话")

    def t3_disconnect(self) -> None:
        self._t3_request("t3_disconnect", "已断开 T3，iPod 上的会话已撤销")

    # Quick replies ----------------------------------------------------------
    def _quick_reply_card(self) -> QtWidgets.QFrame:
        card, card_layout = make_card(10)
        card_layout.addWidget(label("快捷回复", "card-title"))
        card_layout.addWidget(label(
            f"iPod 的 Agents 操作菜单里提供这些回复，选中后发给 Agent。每行一条，"
            f"最多 {QUICK_REPLY_LIMIT} 条，每条不超过 {QUICK_REPLY_BYTES} 字节。", "helper", wrap=True))
        stored = self.window_.settings.get("AGENT_QUICK_REPLIES")
        self.quick_replies = QtWidgets.QPlainTextEdit(stored or QUICK_REPLY_DEFAULT)
        self.quick_replies.setAccessibleName("快捷回复，每行一条")
        self.quick_replies.setTabChangesFocus(True)
        self.quick_replies.setFixedHeight(150)
        self.quick_replies.textChanged.connect(self._check_quick_replies)
        card_layout.addWidget(self.quick_replies)
        self.quick_reply_error = label("", "error", wrap=True)
        self.quick_reply_error.setVisible(False)
        card_layout.addWidget(self.quick_reply_error)
        self.entries["AGENT_QUICK_REPLIES"] = self.quick_replies
        return card

    def _check_quick_replies(self) -> None:
        problem = quick_reply_problem(self.quick_replies.toPlainText())
        self.quick_reply_error.setText(problem or "")
        self.quick_reply_error.setVisible(bool(problem))
        self.save_button.setEnabled(problem is None)

    def _caption(self, text: str) -> QtWidgets.QLabel:
        widget = label(text, "field-label")
        widget.setMinimumWidth(210)
        return widget

    @staticmethod
    def _form() -> QtWidgets.QFormLayout:
        form = QtWidgets.QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        return form

    def _settings_form(self, keys) -> QtWidgets.QFormLayout:
        form = self._form()
        settings = self.window_.settings
        for key in keys:
            caption, default = podlink_settings.FIELDS[key]
            value = settings.get(key, os.environ.get(key) or default)
            choices = SETTING_CHOICES.get(key)
            if choices is not None:
                widget = ChevronComboBox()
                widget.setEditable(True)
                widget.addItems(list(choices))
                widget.setCurrentText(value)
                self.combos.append(widget)
            else:
                widget = QtWidgets.QLineEdit(value)
                if key in SETTING_PLACEHOLDERS:
                    widget.setPlaceholderText(SETTING_PLACEHOLDERS[key])
            widget.setAccessibleName(caption)
            form.addRow(self._caption(caption), widget)
            self.entries[key] = widget
        return form

    @staticmethod
    def entry_text(widget) -> str:
        if isinstance(widget, SegmentedControl):
            return widget.current() or ""
        if isinstance(widget, ToggleSwitch):
            return "on" if widget.isChecked() else "off"
        if isinstance(widget, QtWidgets.QPlainTextEdit):
            return "\n".join(line.strip() for line in widget.toPlainText().splitlines()
                             if line.strip())
        if isinstance(widget, QtWidgets.QComboBox):
            return widget.currentText()
        if isinstance(widget, QtWidgets.QLineEdit):
            return widget.text()
        raise TypeError(f"unsupported setting widget: {type(widget).__name__}")

    def save(self) -> None:
        values = {key: self.entry_text(widget).strip() for key, widget in self.entries.items()}
        try:
            validate(values)
            podlink_settings.save_player_tokens(*(f.text().strip() for f in self.player_tokens))
            podlink_settings.save(values)
        except (OSError, ValueError) as error:
            self.window_.show_error(error)
            return
        for field in self.player_tokens:
            field.clear_secret()
        self.window_.save_pref("port", self.port_override())
        self.window_.settings_saved(values)
        self.status.setText("设置已保存。")

    def update_services(self) -> None:
        runtime = self.window_.runtime
        if runtime is None:
            set_badge(self.remote_badge, "未托管（演示或测试）", "neutral")
        elif runtime.remote_running():
            set_badge(self.remote_badge, "运行中", "success")
        else:
            set_badge(self.remote_badge, "正在重启…", "busy")
        self.restart_button.setEnabled(runtime is not None)
        daemon = self.window_.daemon
        set_badge(self.daemon_badge, "已连接" if daemon.connected else "未连接",
                  "success" if daemon.connected else "neutral")
        plugins = self.window_.services.plugins
        if not plugins.available:
            set_badge(self.plugin_badge, "不可用", "neutral")
        else:
            running = getattr(plugins, "running", False)
            set_badge(self.plugin_badge, "运行中" if running else "未运行",
                      "success" if running else "neutral")

    def shown(self) -> None:
        self.update_services()

    def _pick_seed(self, seed: str) -> None:
        self.window_.set_fallback_seed(seed)

    def show_color_source(self) -> None:
        """Mark the current colour source and say which colour it gives."""
        window = self.window_
        source = window.color_source
        self.color_source.set_current(source)
        accent, origin = window.theme_manager.system_accent()
        where = {"portal": "XDG 门户 accent-color", "gsettings": "GNOME 设置 accent-color",
                 "palette": "Qt 调色板的高亮色"}.get(origin)
        system = (f"系统强调色 {accent}（{where}）" if accent
                  else "系统没有提供强调色，使用默认颜色")
        self.color_source_help.setText({
            "system": f"{system}。系统设置改变后自动更新。",
            "cover": f"跟随所选歌曲的封面；没有封面时用{system}。",
            "swatch": f"使用下面选中的颜色 {window.fallback_seed.upper()}。",
        }[source])
        self.swatch_group.setExclusive(False)
        for seed, swatch in self.swatches.items():
            swatch.setChecked(source == "swatch" and seed == window.fallback_seed.upper())
        self.swatch_group.setExclusive(True)

    def apply_theme(self, tokens) -> None:
        mode = self.window_.theme_manager.mode
        resolved = self.window_.theme_manager.resolved
        self.theme_switch.set_current(mode)
        self.theme_switch.update_theme(tokens)
        if self.agent_control is not None:
            self.agent_control.update_theme(tokens)
        self.port_picker.show_state(refill=False)
        self.motion.tokens = tokens
        self.color_source.update_theme(tokens)
        for swatch in self.swatches.values():
            swatch.tokens = tokens
            swatch.update()
        if self.t3_switch is not None:
            self.t3_switch.tokens = tokens
        if self.t3_follow_switch is not None:
            self.t3_follow_switch.tokens = tokens
        for combo in self.combos:
            combo.update_theme(tokens)
        for field in self.password_inputs:
            field.update_theme(tokens)
        text = RESOLVED_LABELS.get(resolved, resolved)
        set_badge(self.theme_badge, f"跟随系统，当前为{text}" if mode == "system" else f"当前为{text}",
                  "accent")


class SeedSwatch(QtWidgets.QAbstractButton):
    """A 40 px seed colour choice; the chosen one shows a check."""

    def __init__(self, seed: str, parent=None):
        super().__init__(parent)
        self.seed = seed
        self.tokens = None
        self.setCheckable(True)
        self.setFixedSize(40, 40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("ripple_radius", 20)
        self.setAccessibleName(f"主题色 {seed}")
        self.setToolTip(seed)

    def paintEvent(self, _event) -> None:
        t = self.tokens
        if t is None:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(4, 4, -4, -4)
        if self.isChecked() or self.hasFocus():
            painter.setPen(QtGui.QPen(QtGui.QColor(t.on_surface if self.isChecked() else t.secondary), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(self.rect().adjusted(1, 1, -1, -1))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(self.seed))
        painter.drawEllipse(rect)
        if self.isChecked():
            icons.paint(painter, "check", QtCore.QRectF(rect).adjusted(7, 7, -7, -7), QtGui.QColor("#FFFFFF"))
        painter.end()
