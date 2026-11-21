"""Extensions: input plugins, their settings, and a live input monitor."""
from __future__ import annotations

from pathlib import Path
import time

from .. import icons
from ..qt import QtCore, QtGui, QtSvg, QtWidgets, Qt
from ..widgets import (ChevronComboBox, PageHeader, ToggleSwitch, button, discard, label,
                       make_badge, make_card, set_badge)
from . import Page
from .songs import EmptyState

STATE = {"running": ("运行中", "success"), "waiting": ("等待设备", "busy"),
         "error": ("出错", "danger"), "disabled": ("已停用", "neutral")}
PERMISSIONS = {"usb": "USB 设备", "serial": "串口", "ep0": "iPod 按键通道",
               "keys": "模拟键盘", "voice": "语音输入"}
KEYS = {"menu": "菜单", "select": "确定", "play": "播放/暂停", "left": "上一首",
        "right": "下一首", "up": "上", "down": "下", "vol_up": "音量 +", "vol_down": "音量 −"}
PHASES = {"down": "按下", "repeat": "长按", "up": "松开", "tap": "轻按"}
ASSETS = Path(__file__).resolve().parent.parent / "assets"
REMOTES = {"aluminium": "铝合金（第二代）", "plastic": "塑料（第一代）"}
RING = {"key-right": 0, "key-vol_up": 90, "key-left": 180, "key-vol_down": 270}
REMOTE_PLUGIN = "apple-ir"
GLYPHS = {"key-menu": "glyph-menu", "key-play": "glyph-play"}  # drawn over the highlight
HOLD_SECONDS = 0.6  # a held button stays lit this long after its last repeat
_remote_svgs: dict = {}


def normalize_schema(schema) -> list[dict]:
    """Field dicts with "key", from a list, a key→field map or JSON Schema."""
    if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
        schema = schema["properties"]
    fields = []
    if isinstance(schema, dict):
        for key, spec in schema.items():
            spec = dict(spec) if isinstance(spec, dict) else {"default": spec}
            spec.setdefault("key", key)
            fields.append(spec)
    elif isinstance(schema, list):
        for spec in schema:
            if isinstance(spec, dict):
                spec = dict(spec)
                spec.setdefault("key", spec.get("name") or spec.get("id"))
                if spec["key"]:
                    fields.append(spec)
    for spec in fields:
        if "choices" not in spec and "enum" in spec:
            spec["choices"] = spec["enum"]
        if not isinstance(spec.get("labels"), dict):
            spec.pop("labels", None)
        kind = str(spec.get("type") or "").lower()
        if spec.get("choices"):
            kind = "enum"
        elif kind in ("integer", "int"):
            kind = "int"
        elif kind in ("number", "float", "double"):
            kind = "float"
        elif kind in ("boolean", "bool"):
            kind = "bool"
        elif kind not in ("secret", "password"):
            kind = "string"
        spec["type"] = kind
    return fields


class RemotePad(QtWidgets.QWidget):
    """The Apple Remote the user owns, on its side with the IR window to the right
    so that, at its real proportions, MENU stays legible in a short card. Each
    button lights while it is held and is outlined once it repeats."""

    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.model = "aluminium"
        self.lit: dict[str, float] = {}
        self.held: dict[str, float] = {}
        self.long: set[str] = set()
        self.last = None
        self.setMinimumSize(300, 120)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                           QtWidgets.QSizePolicy.Policy.Fixed)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._fade)

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(640, 170)

    def renderer(self):
        if self.model not in _remote_svgs:
            _remote_svgs[self.model] = QtSvg.QSvgRenderer(
                str(ASSETS / f"apple-remote-{self.model}.svg"))
        return _remote_svgs[self.model]

    def set_model(self, model: str) -> None:
        model = model if model in REMOTES else "aluminium"
        if model != self.model:
            self.model = model
            self.lit.clear()
            self.held.clear()
            self.long.clear()
            self.update()
        self.setAccessibleName(f"Apple Remote：{REMOTES[model]}")

    def element(self, key) -> str | None:
        """The SVG id of the button that sends `key` on this model."""
        if self.model == "plastic" and key == "select":
            key = "play"
        element = f"key-{key}"
        return element if self.renderer().elementExists(element) else None

    def flash(self, key: str, phase: str) -> None:
        self.last = (key, phase)
        element = self.element(key)
        if element is None:
            return
        self.lit[element] = 1.0
        if phase in ("down", "repeat"):
            self.held[element] = time.monotonic()
            if phase == "repeat":
                self.long.add(element)
        else:
            self.held.pop(element, None)
        self._timer.start()
        self.update()

    def _fade(self) -> None:
        now = time.monotonic()
        for element in list(self.lit):
            if now - self.held.get(element, -HOLD_SECONDS) < HOLD_SECONDS:
                continue
            self.held.pop(element, None)
            self.lit[element] -= 0.06
            if self.lit[element] <= 0:
                del self.lit[element]
                self.long.discard(element)
        if not self.lit:
            self._timer.stop()
        self.update()

    def _shape(self, renderer, element) -> QtGui.QPainterPath:
        path = QtGui.QPainterPath()
        if element not in RING:
            path.addEllipse(renderer.boundsOnElement(element))
            return path
        outer = renderer.boundsOnElement("ring")
        centre = "key-play" if self.model == "plastic" else "key-select"
        inner = renderer.boundsOnElement(centre)
        angle = RING[element]
        path.arcMoveTo(outer, angle - 45)
        path.arcTo(outer, angle - 45, 90)
        path.arcTo(inner, angle + 45, -90)
        path.closeSubpath()
        return path

    def scale_for(self, view) -> float:
        return min((self.width() - 8) / view.height(), (self.height() - 8) / view.width())

    def view_transform(self, view, scale) -> QtGui.QTransform:
        """SVG (portrait, IR end up) to widget: turned a quarter clockwise, centred."""
        transform = QtGui.QTransform()
        transform.translate(self.width() / 2, self.height() / 2)
        transform.rotate(90)
        transform.scale(scale, scale)
        transform.translate(-view.center().x(), -view.center().y())
        return transform

    def paintEvent(self, _event) -> None:
        renderer = self.renderer()
        view = renderer.viewBoxF()
        scale = self.scale_for(view)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setTransform(self.view_transform(view, scale))
        renderer.render(painter, view)
        accent = QtGui.QColor(self.page.tokens.accent)
        for element, glow in self.lit.items():
            fill = QtGui.QColor(accent)
            fill.setAlphaF(0.6 * glow)
            shape = self._shape(renderer, element)
            painter.setBrush(fill)
            painter.setPen(Qt.PenStyle.NoPen)
            if element in self.long:
                edge = QtGui.QColor(accent)
                edge.setAlphaF(glow)
                painter.setPen(QtGui.QPen(edge, 3 / scale))
            painter.drawPath(shape)
            glyph = GLYPHS.get(element, element if element in RING else None)
            if glyph:
                renderer.render(painter, glyph, renderer.boundsOnElement(glyph))
        painter.end()


class ExtensionsPage(Page):
    key = "extensions"

    def __init__(self, window, parent=None):
        super().__init__(window, parent)
        self.plugins = window.services.plugins
        self.infos: list = []
        self.fields: dict[str, QtWidgets.QWidget] = {}
        self.detected = None
        self.header = PageHeader("扩展", "电脑上的输入设备：红外接收器、手柄等，以插件方式接入")
        self.host_badge = self.header.add(make_badge(""))
        self.layout_.addWidget(self.header)

        self.body = QtWidgets.QStackedWidget()
        split = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(14)
        split.setChildrenCollapsible(False)
        left, left_layout = make_card(6, expand=True, margins=(8, 12, 8, 8))
        left_layout.addWidget(label("输入插件", "section-subtitle"))
        self.list = QtWidgets.QListWidget()
        self.list.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setAccessibleName("输入插件")
        self.list.currentRowChanged.connect(lambda _r: self.show_plugin())
        left_layout.addWidget(self.list)
        self.fallback = label("", "helper", wrap=True)
        left_layout.addWidget(self.fallback)
        split.addWidget(left)

        right_host = QtWidgets.QWidget()
        right = QtWidgets.QVBoxLayout(right_host)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(14)
        detail, detail_layout = make_card(10)
        head = QtWidgets.QHBoxLayout()
        self.name = label("", "hero-title")
        self.state_badge = make_badge("")
        self.toggle = ToggleSwitch()
        self.toggle.setAccessibleName("启用插件")
        self.toggle.clicked.connect(self.toggle_plugin)
        head.addWidget(self.name)
        head.addWidget(self.state_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        head.addStretch(1)
        head.addWidget(label("启用", "field-label"))
        head.addWidget(self.toggle)
        detail_layout.addLayout(head)
        self.description = label("", "page-subtitle", wrap=True)
        detail_layout.addWidget(self.description)
        self.device = label("", "helper", wrap=True)
        detail_layout.addWidget(self.device)
        self.permissions = QtWidgets.QHBoxLayout()
        self.permissions.setSpacing(6)
        detail_layout.addLayout(self.permissions)
        divider = QtWidgets.QFrame()
        divider.setObjectName("Divider")
        detail_layout.addWidget(divider)
        detail_layout.addWidget(label("设置", "card-title"))
        self.form_host = QtWidgets.QWidget()
        self.form = QtWidgets.QFormLayout(self.form_host)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setHorizontalSpacing(14)
        self.form.setVerticalSpacing(8)
        detail_layout.addWidget(self.form_host)
        save_row = QtWidgets.QHBoxLayout()
        self.form_status = label("", "helper")
        save_row.addWidget(self.form_status, 1)
        self.save_button = button("保存插件设置", "primary", None, self.save_settings)
        save_row.addWidget(self.save_button)
        detail_layout.addLayout(save_row)
        detail.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                             QtWidgets.QSizePolicy.Policy.Fixed)
        right.addWidget(detail)

        monitor, monitor_layout = make_card(8, expand=True)
        monitor_head = QtWidgets.QHBoxLayout()
        monitor_head.addWidget(label("输入监视器", "card-title"))
        monitor_head.addStretch(1)
        self.last_key = label("按一下遥控器试试", "helper")
        monitor_head.addWidget(self.last_key)
        monitor_layout.addLayout(monitor_head)
        self.pad = RemotePad(self)
        monitor_layout.addWidget(self.pad)
        self.events = QtWidgets.QListWidget()
        self.events.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.events.setAccessibleName("最近的输入事件")
        self.events.setMinimumHeight(120)
        monitor_layout.addWidget(self.events, 1)
        right.addWidget(monitor, 1)
        right_scroll = QtWidgets.QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setWidget(right_host)
        split.addWidget(right_scroll)
        split.setSizes([280, 720])
        split.setStretchFactor(1, 1)
        self.empty = EmptyState("extensions", "插件宿主不可用",
                                getattr(self.plugins, "error", None) or "这个版本没有安装输入插件宿主。")
        self.body.addWidget(split)
        self.body.addWidget(self.empty)
        self.layout_.addWidget(self.body, 1)

        self.plugins.plugins_changed.connect(self.reload)
        self.plugins.event.connect(self.on_event)
        window.daemon.state_changed.connect(self._daemon_state)
        self.reload()

    # Data -------------------------------------------------------------
    def reload(self) -> None:
        available = self.plugins.available
        self.body.setCurrentIndex(0 if available else 1)
        set_badge(self.host_badge, "插件宿主运行中" if getattr(self.plugins, "running", False)
                  else "插件宿主未运行", "success" if getattr(self.plugins, "running", False) else "neutral")
        if not available:
            return
        try:
            self.infos = self.plugins.list()
        except (OSError, RuntimeError, ValueError) as error:
            self.window_.flash(f"无法读取插件列表：{error}")
            self.infos = []
        current = self.list.currentRow()
        blocked = self.list.blockSignals(True)
        self.list.clear()
        for info in self.infos:
            text, _tone = STATE.get(info.state, (info.state, "neutral"))
            item = QtWidgets.QListWidgetItem(f"{info.name}\n{text}")
            item.setSizeHint(QtCore.QSize(0, 50))
            item.setIcon(icons.icon("remote" if "ir" in info.id else "key",
                                    self.tokens.accent if info.enabled else self.tokens.text_muted, 22))
            self.list.addItem(item)
        self.list.blockSignals(blocked)
        if self.infos:
            self.list.setCurrentRow(min(max(0, current), len(self.infos) - 1))
        self.show_plugin()
        self.show_remote()
        self._daemon_state(self.window_.daemon.state)

    def current(self):
        row = self.list.currentRow()
        return self.infos[row] if 0 <= row < len(self.infos) else None

    def show_plugin(self) -> None:
        info = self.current()
        if info is None:
            return
        self.name.setText(f"{info.name}")
        text, tone = STATE.get(info.state, (info.state or "未知", "neutral"))
        set_badge(self.state_badge, text, tone)
        self.toggle.setChecked(bool(info.enabled))
        self.description.setText(f"{info.description}（版本 {info.version}）")
        detail = info.detail or ""
        self.device.setText(f"设备：{info.device}" + (f"。{detail}" if detail else "") if info.device
                            else (detail or "没有检测到设备"))
        while self.permissions.count():
            item = self.permissions.takeAt(0)
            if item.widget():
                discard(item.widget())
        self.permissions.addWidget(label("权限：", "helper"))
        for permission in info.permissions or []:
            self.permissions.addWidget(make_badge(PERMISSIONS.get(permission, permission), "accent"))
        self.permissions.addStretch(1)
        self._build_form(info)

    def _build_form(self, info) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        self.fields.clear()
        try:
            values = self.plugins.settings(info.id)
        except (OSError, RuntimeError, ValueError, KeyError):
            values = {}
        specs = normalize_schema(info.settings_schema)
        for spec in specs:
            key = spec["key"]
            value = values.get(key, spec.get("default"))
            kind = spec["type"]
            if kind == "bool":
                widget = ToggleSwitch()
                widget.tokens = self.tokens
                widget.setChecked(bool(value))
            elif kind == "enum":
                widget = ChevronComboBox()
                widget.update_theme(self.tokens)
                labels = spec.get("labels") or {}
                for choice in spec.get("choices") or []:
                    widget.addItem(str(labels.get(choice, choice)), choice)
                index = widget.findData(value)
                widget.setCurrentIndex(max(0, index))
                if info.id == REMOTE_PLUGIN and key == "remote":
                    widget.currentIndexChanged.connect(
                        lambda _i, w=widget: self.show_remote(w.currentData()))
            elif kind == "int":
                widget = QtWidgets.QSpinBox()
                widget.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
                widget.setRange(int(spec.get("min", -1_000_000)), int(spec.get("max", 1_000_000)))
                widget.setValue(int(value or 0))
                widget.lineEdit().setTextMargins(12, 0, 0, 0)
            elif kind == "float":
                widget = QtWidgets.QDoubleSpinBox()
                widget.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
                widget.setDecimals(2)
                widget.setSingleStep(0.05)
                widget.setRange(float(spec.get("min", -1e6)), float(spec.get("max", 1e6)))
                widget.setValue(float(value or 0))
                widget.lineEdit().setTextMargins(12, 0, 0, 0)
            else:
                widget = QtWidgets.QLineEdit("" if value is None else str(value))
                if kind in ("secret", "password"):
                    widget.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
                if spec.get("placeholder"):
                    widget.setPlaceholderText(str(spec["placeholder"]))
            caption = label(str(spec.get("label") or spec.get("description") or key), "field-label",
                            wrap=True)
            if spec.get("description"):
                caption.setToolTip(str(spec["description"]))
                widget.setToolTip(str(spec["description"]))
            widget.setAccessibleName(caption.text())
            self.fields[key] = widget
            self.form.addRow(caption, widget)
        self.save_button.setEnabled(bool(specs))
        self.form_status.setText("" if specs else "这个插件没有可调整的设置。")

    def form_values(self) -> dict:
        values = {}
        for key, widget in self.fields.items():
            if isinstance(widget, ToggleSwitch):
                values[key] = widget.isChecked()
            elif isinstance(widget, QtWidgets.QComboBox):
                values[key] = widget.currentData()
            elif isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
                values[key] = widget.value()
            else:
                values[key] = widget.text()
        return values

    # Actions -----------------------------------------------------------
    def toggle_plugin(self, on: bool) -> None:
        info = self.current()
        if info is None:
            return
        try:
            self.plugins.enable(info.id, on)
        except (OSError, RuntimeError, ValueError) as error:
            self.window_.show_error(error)
        self.reload()

    def save_settings(self) -> None:
        info = self.current()
        if info is None:
            return
        try:
            self.plugins.set_settings(info.id, self.form_values())
        except (OSError, RuntimeError, ValueError) as error:
            self.window_.show_error(error)
            return
        self.form_status.setText("已保存。插件会用新设置重新启动。")
        self.window_.flash(f"已保存 {info.name} 的设置")

    def show_remote(self, choice=None) -> None:
        """Draw the saved (or previewed) model; "auto" follows the remote seen last."""
        if choice is None:
            try:
                choice = self.plugins.settings(REMOTE_PLUGIN).get("remote", "auto")
            except (OSError, RuntimeError, ValueError, KeyError):
                choice = "auto"
        auto = choice not in REMOTES
        model = (self.detected or "aluminium") if auto else choice
        self.pad.set_model(model)
        note = "自动识别" if auto and self.detected else "未识别，按铝合金画" if auto else "手动选择"
        self.pad.setToolTip(f"{REMOTES[model]}（{note}）")

    def on_event(self, message: dict) -> None:
        if message.get("type") == "input":
            key, phase = message.get("key"), message.get("phase")
            remote = message.get("remote")
            if message.get("source") == REMOTE_PLUGIN and remote in REMOTES and remote != self.detected:
                self.detected = remote
                self.show_remote()
            self.pad.flash(key, phase)
            text = f"{KEYS.get(key, key)}，{PHASES.get(phase, phase)}"
            self.last_key.setText(f"最后一次：{text}")
            item = QtWidgets.QListWidgetItem(
                f"{time.strftime('%H:%M:%S')}  {text}  来自 {message.get('source')}")
            self.events.insertItem(0, item)
            while self.events.count() > 80:
                self.events.takeItem(self.events.count() - 1)
        elif message.get("type") == "status":
            QtCore.QTimer.singleShot(0, self.reload)

    def _daemon_state(self, state: dict) -> None:
        fallback = (state.get("input") or {}).get("ir_fallback")
        self.fallback.setText({
            "ipod": "红外按键现在发送给 iPod。",
            "host-fallback": "iPod 没有连接，红外按键改为直接控制电脑。",
            "pending": "正在确认红外按键的去向…",
        }.get(fallback, ""))

    def apply_theme(self, tokens) -> None:
        self.toggle.tokens = tokens
        for widget in self.fields.values():
            if isinstance(widget, ToggleSwitch):
                widget.tokens = tokens
            elif isinstance(widget, ChevronComboBox):
                widget.update_theme(tokens)
        self.pad.update()
        self.empty.apply_theme(tokens)
        if self.plugins.available:
            self.reload()
