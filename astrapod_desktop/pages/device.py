"""Device: the iPod front from the Rockbox manual, its facts, the serial port
it is reached through, and the capacity bar."""
from __future__ import annotations

from pathlib import Path

from .. import fmt
from ..artwork import NO_ART
from ..qt import QtCore, QtGui, QtSvg, QtWidgets, Qt
from ..widgets import (CapacityBar, PageHeader, button, label, make_badge,
                       make_card, set_badge)
from . import Page
from .port import PortPicker

R = QtCore.QRectF
P = QtCore.QPointF
MODEL_NAMES = {"ipod6g": "iPod classic", "ipodvideo": "iPod video"}
ASSETS = Path(__file__).resolve().parent.parent / "assets"
# The LCD within either front drawing, as fractions of its width and height.
SCREEN = (0.0978, 0.0638, 0.9022, 0.4224)
_renderers: dict = {}


def device_name(summary) -> str:
    """The sidebar name of a device: its model, or plain iPod."""
    return MODEL_NAMES.get(summary.model, "iPod") if summary is not None else "iPod"


def _home_screen() -> QtGui.QImage:
    """The AstraPod home menu, captured from the simulator at 320x240."""
    if "home" not in _renderers:
        _renderers["home"] = QtGui.QImage(str(ASSETS / "home-screen.png"))
    return _renderers["home"]


def _front_renderer(model: str):
    if model not in _renderers:
        _renderers[model] = QtSvg.QSvgRenderer(str(ASSETS / f"{model}-front.svg"))
    return _renderers[model]


class IPodDrawing(QtWidgets.QWidget):
    """Front view of the player; the screen shows the home menu while connected."""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window_ = window
        self.model = "ipod6g"
        self.connected = False
        self.setFixedSize(230, 380)

    def set_state(self, model: str, connected: bool) -> None:
        self.model = model or "ipod6g"
        self.connected = connected
        self.update()

    def paintEvent(self, _event) -> None:
        t = self.window_.theme_manager.tokens
        dark = t.name == "dark"
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        body = R(22, 10, 186, 314)
        # Floor shadow
        shadow = QtGui.QRadialGradient(P(body.center().x(), body.bottom() + 26), 110)
        shadow.setColorAt(0, QtGui.QColor(0, 0, 0, 90 if dark else 55))
        shadow.setColorAt(1, QtGui.QColor(0, 0, 0, 0))
        painter.save()
        painter.translate(0, body.bottom() + 26)
        painter.scale(1, 0.12)
        painter.translate(0, -(body.bottom() + 26))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shadow)
        painter.drawEllipse(P(body.center().x(), body.bottom() + 26), 110, 110)
        painter.restore()
        model = "ipodvideo" if self.model == "ipodvideo" else "ipod6g"
        renderer = _front_renderer(model)
        renderer.render(painter, body)
        screen = R(body.left() + body.width() * SCREEN[0], body.top() + body.height() * SCREEN[1],
                   body.width() * (SCREEN[2] - SCREEN[0]), body.height() * (SCREEN[3] - SCREEN[1]))
        painter.setPen(Qt.PenStyle.NoPen)
        self._paint_screen(painter, screen, t)
        glare = QtGui.QLinearGradient(screen.topLeft(), screen.bottomRight())
        glare.setColorAt(0, QtGui.QColor(255, 255, 255, 34))
        glare.setColorAt(0.45, QtGui.QColor(255, 255, 255, 0))
        painter.setBrush(glare)
        painter.drawRect(screen)
        painter.end()

    def _paint_screen(self, painter, screen, t) -> None:
        if not self.connected:
            painter.setBrush(QtGui.QColor("#16171B"))
            painter.drawRect(screen)
            painter.setPen(QtGui.QColor("#6B6E78"))
            font = QtGui.QFont(self.font())
            font.setPixelSize(11)
            painter.setFont(font)
            painter.drawText(screen, Qt.AlignmentFlag.AlignCenter, "未连接")
            painter.setPen(Qt.PenStyle.NoPen)
            return
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(screen, _home_screen())


class DevicePage(Page):
    key = "device"

    def __init__(self, window, parent=None):
        super().__init__(window, parent)
        self.header = PageHeader("iPod")
        self.refresh_button = self.header.add(button("刷新", None, "refresh", self.refresh))
        self.layout_.addWidget(self.header)

        hero, hero_layout = make_card(0, margins=(26, 22, 30, 22))
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(34)
        self.drawing = IPodDrawing(window)
        row.addWidget(self.drawing, 0, Qt.AlignmentFlag.AlignTop)
        facts = QtWidgets.QVBoxLayout()
        facts.setSpacing(6)
        name_row = QtWidgets.QHBoxLayout()
        self.name = label("iPod", "page-title")
        self.link_badge = make_badge("未连接")
        name_row.addWidget(self.name)
        name_row.addSpacing(6)
        name_row.addWidget(self.link_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        name_row.addStretch(1)
        facts.addLayout(name_row)
        self.tagline = label("", "page-subtitle", wrap=True)
        facts.addWidget(self.tagline)
        facts.addSpacing(14)
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(22)
        grid.setVerticalSpacing(10)
        self.values: dict[str, QtWidgets.QLabel] = {}
        for i, (key, caption) in enumerate((("model", "型号"), ("firmware", "固件"),
                                            ("serial", "序列号"), ("capacity", "容量"),
                                            ("tracks", "歌曲"), ("database", "数据库"))):
            name = label(caption, "stat-label")
            value = label("—", "card-title")
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(name, i, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(value, i, 1)
            self.values[key] = value
        self.port_picker = PortPicker(window, autosave=True)
        self.port = self.port_picker.field
        grid.addWidget(label("串口", "stat-label"), 6, 0,
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(self.port_picker, 6, 1)
        grid.addWidget(self.port_picker.help, 7, 1)
        grid.setColumnStretch(1, 1)
        facts.addLayout(grid)
        facts.addStretch(1)
        actions = QtWidgets.QHBoxLayout()
        self.read_button = button("读取资料库", "primary", "songs",
                                  lambda: window.library_state.refresh(True))
        self.songs_button = button("查看歌曲", None, None,
                                   lambda: window.navigate(f"{self.key}.songs"))
        actions.addWidget(self.read_button)
        actions.addWidget(self.songs_button)
        actions.addStretch(1)
        facts.addLayout(actions)
        row.addLayout(facts, 1)
        hero_layout.addLayout(row)
        self.layout_.addWidget(hero)

        cap, cap_layout = make_card(10, margins=(26, 18, 26, 18))
        cap_head = QtWidgets.QHBoxLayout()
        cap_head.addWidget(label("容量", "card-title"))
        cap_head.addStretch(1)
        self.cap_total = label("", "helper")
        cap_head.addWidget(self.cap_total)
        cap_layout.addLayout(cap_head)
        self.capacity = CapacityBar()
        cap_layout.addWidget(self.capacity)
        self.cap_note = label("音频按每首歌的时长和比特率估算。", "helper")
        cap_layout.addWidget(self.cap_note)
        self.layout_.addWidget(cap)
        self.layout_.addStretch(1)

        window.library_state.changed.connect(self.update_view)
        window.daemon.state_changed.connect(lambda _s: self.update_view())
        self.update_view()

    def refresh(self) -> None:
        self.window_.library_state.refresh(False)

    def update_view(self) -> None:
        state = self.window_.library_state
        link = self.window_.daemon.state.get("link") or {}
        summary = state.summary
        connected = bool(link.get("connected")) or state.source == "device"
        model = summary.model if summary else "ipod6g"
        self.name.setText(MODEL_NAMES.get(model, "iPod"))
        if link.get("connected"):
            mode = {"podlink": "PodLink", "disk": "磁盘模式"}.get(link.get("mode"), "已连接")
            set_badge(self.link_badge, f"已连接，{mode}", "success")
        elif state.loading:
            set_badge(self.link_badge, "正在读取…", "busy")
        else:
            set_badge(self.link_badge, "未连接", "neutral")
        if summary is None:
            self.tagline.setText("用 USB 连接 iPod 并打开 PodLink，然后点刷新读取它的信息。")
        elif state.source == "cache":
            self.tagline.setText("显示的是上次读取的信息。")
        else:
            self.tagline.setText("信息来自刚才连接的 iPod。")
        serial = link.get("serial") or state.serial
        values = {
            "model": MODEL_NAMES.get(model, model) + ("（第六、七代）" if model == "ipod6g" else
                                                      "（第五代）" if model == "ipodvideo" else ""),
            "firmware": (summary.firmware if summary else link.get("firmware")) or "—",
            "serial": serial or "—",
            "capacity": fmt.size(summary.total_bytes) if summary else "—",
            "tracks": f"{summary.track_count:,} 首" if summary else "—",
            "database": ("—" if summary is None else
                         f"就绪，第 {summary.commit_id} 次提交" if summary.database_ready
                         else "正在更新"),
        }
        for key, value in values.items():
            self.values[key].setText(value)
        self.refresh_button.setEnabled(not state.loading)
        self.read_button.setEnabled(not state.loading)
        self.songs_button.setVisible(bool(state.tracks))
        if summary:
            audio = sum(fmt.estimated_bytes(t.length_ms, t.bitrate_kbps) for t in state.tracks)
            used = max(0, summary.total_bytes - summary.free_bytes)
            audio = min(audio, used)
            other = max(0, used - audio)
            self.capacity.set_usage(summary.total_bytes, audio, other, summary.free_bytes,
                                    animate=self.window_.motion and self.isVisible())
            self.cap_total.setText(f"共 {fmt.size(summary.total_bytes)}，可用 {fmt.size(summary.free_bytes)}")
        else:
            self.capacity.set_usage(1, 0, 0, 1, animate=False)
            self.cap_total.setText("")
        self.drawing.set_state(model, connected)

    def shown(self) -> None:
        state = self.window_.library_state
        if state.summary is not None:
            self.update_view()

    def apply_theme(self, tokens) -> None:
        self.port_picker.show_state(refill=False)
        self.capacity.tokens = tokens
        self.drawing.update()
