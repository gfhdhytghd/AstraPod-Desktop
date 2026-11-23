"""Device: the iPod front from the Rockbox manual, its facts and the
capacity bar, scrolling as one page. Below SMALL_BELOW the drawing shrinks
to leave the facts room."""
from __future__ import annotations

from pathlib import Path

from .. import fmt
from ..artwork import NO_ART
from ..qt import QtCore, QtGui, QtSvg, QtWidgets, Qt
from ..widgets import (CapacityBar, PageHeader, button, label, make_badge,
                       make_card, set_badge)
from . import Page
from .stock_screen import StockCoverPanel, StockMount, discover_stock

R = QtCore.QRectF
P = QtCore.QPointF
MODEL_NAMES = {"ipod6g": "iPod classic", "ipodvideo": "iPod video"}
ASSETS = Path(__file__).resolve().parent.parent / "assets"
# The LCD within either front drawing, as fractions of its width and height.
SCREEN = (0.0978, 0.0638, 0.9022, 0.4224)
# The hero card's inner width below which the drawing is drawn at SMALL_SCALE.
SMALL_BELOW = 640
SMALL_SCALE = 0.6
_renderers: dict = {}


def device_name(summary) -> str:
    """The sidebar name of a device: its model, or plain iPod."""
    return MODEL_NAMES.get(summary.model, "iPod") if summary is not None else "iPod"


def _home_screen(stock: bool) -> QtGui.QImage:
    filename = "stock-classic-home.png" if stock else "home-screen.png"
    if filename not in _renderers:
        _renderers[filename] = QtGui.QImage(str(ASSETS / filename))
    return _renderers[filename]


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
        self.stock = False
        self.cover_panel = StockCoverPanel(self, window.library_state)
        self.scale = 1.0
        self.setFixedSize(230, 380)

    def set_scale(self, scale: float) -> None:
        self.scale = scale
        self.setFixedSize(round(230 * scale), round(380 * scale))
        self.update()

    def set_state(self, model: str, connected: bool, stock: StockMount | None) -> None:
        self.model = model or "ipod6g"
        self.connected = connected
        self.stock = stock is not None
        self.cover_panel.set_source(stock)
        self.update()

    def paintEvent(self, _event) -> None:
        t = self.window_.theme_manager.tokens
        dark = t.name == "dark"
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.scale(self.scale, self.scale)
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
        self._paint_screen(painter, screen)
        glare = QtGui.QLinearGradient(screen.topLeft(), screen.bottomRight())
        glare.setColorAt(0, QtGui.QColor(255, 255, 255, 34))
        glare.setColorAt(0.45, QtGui.QColor(255, 255, 255, 0))
        painter.setBrush(glare)
        painter.drawRect(screen)
        painter.end()

    def _paint_screen(self, painter, screen) -> None:
        if not self.connected:
            painter.setBrush(QtGui.QColor("#16171B"))
            painter.drawRect(screen)
            painter.setPen(QtGui.QColor("#6B6E78"))
            font = QtGui.QFont(self.font())
            font.setPixelSize(round(11 / self.scale))
            painter.setFont(font)
            painter.drawText(screen, Qt.AlignmentFlag.AlignCenter, "未连接")
            painter.setPen(Qt.PenStyle.NoPen)
            return
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(screen, _home_screen(self.stock))
        if self.stock:
            # Match the cover panel in the 320x240 menu asset.
            title = screen.height() * 27 / 240
            panel = R(screen.center().x(), screen.top() + title,
                      screen.width() / 2, screen.height() - title)
            self.cover_panel.paint(painter, panel)


class DevicePage(Page):
    key = "device"

    def __init__(self, window, parent=None):
        super().__init__(window, parent)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(host)
        layout.setContentsMargins(28, 18, 28, 20)
        layout.setSpacing(14)
        self.header = PageHeader("iPod")
        self.refresh_button = self.header.add(button("刷新", None, "refresh", self.refresh))
        layout.addWidget(self.header)

        self.hero, hero_layout = make_card(0, margins=(26, 22, 30, 22))
        self.hero_row = row = QtWidgets.QHBoxLayout()
        row.setSpacing(34)
        self.drawing = IPodDrawing(window)
        row.addWidget(self.drawing, 0, Qt.AlignmentFlag.AlignTop)
        facts = QtWidgets.QVBoxLayout()
        facts.setSpacing(6)
        name_row = QtWidgets.QHBoxLayout()
        self.name = label("iPod", "page-title", wrap=True)
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
            value = label("—", "card-title", wrap=True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(name, i, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(value, i, 1)
            self.values[key] = value
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
        layout.addWidget(self.hero)

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
        layout.addWidget(cap)
        layout.addStretch(1)
        self.scroll.setWidget(host)
        self.layout_.addWidget(self.scroll)
        self.scroll.viewport().installEventFilter(self)

        self.stock_mount = StockMount()
        self._probing = False
        self._probe_timer = QtCore.QTimer(self)
        self._probe_timer.setInterval(2000)
        self._probe_timer.timeout.connect(self._probe_stock)
        window.installEventFilter(self)
        window.library_state.changed.connect(self.update_view)
        window.daemon.state_changed.connect(lambda _s: self.update_view())
        self.update_view()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.scroll.viewport() and event.type() == QtCore.QEvent.Type.Resize:
            self._reflow(event.size().width())
        elif watched is self.window_ and event.type() in (
                QtCore.QEvent.Type.WindowStateChange, QtCore.QEvent.Type.DynamicPropertyChange):
            self._sync_screen_activity()
        return super().eventFilter(watched, event)

    def _reflow(self, width: int) -> None:
        margins = self.hero.layout().contentsMargins()
        inner = width - 56 - margins.left() - margins.right()
        small = inner < SMALL_BELOW
        if (self.drawing.scale < 1) != small:
            self.drawing.set_scale(SMALL_SCALE if small else 1.0)
            self.hero_row.setSpacing(20 if small else 34)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_screen_activity()

    def hideEvent(self, event) -> None:
        self._probe_timer.stop()
        self.drawing.cover_panel.set_visible(False, self.window_.motion)
        super().hideEvent(event)

    def _sync_screen_activity(self) -> None:
        window = self.window_
        visible = self.isVisible() and not window.isMinimized()
        self.drawing.cover_panel.set_visible(visible, window.motion)
        link = window.daemon.state.get("link") or {}
        if visible and not (window.daemon.demo or link.get("connected") or link.get("borrowed")):
            if not self._probe_timer.isActive():
                self._probe_timer.start()
                self._probe_stock()
        else:
            self._probe_timer.stop()

    def _probe_stock(self, force: bool = False) -> None:
        if self._probing:
            return
        previous = StockMount() if force else self.stock_mount
        self._probing = True

        def done(result):
            self._probing = False
            if self._probe_timer.isActive() and (force or result != self.stock_mount):
                if force:
                    self.drawing.cover_panel.set_source(None)
                self.stock_mount = result
                self.update_view()

        self.window_.runner.run("识别原厂 iPod", lambda _job: discover_stock(previous),
                                done, lambda _error: done(StockMount()),
                                foreground=False, device=False)

    def refresh(self) -> None:
        if self._probe_timer.isActive() and (self.stock_mount.root or self.stock_mount.unsupported):
            self._probe_stock(force=True)
            return
        self.window_.library_state.refresh(False)

    def update_view(self) -> None:
        state = self.window_.library_state
        link = self.window_.daemon.state.get("link") or {}
        if link.get("connected") or link.get("borrowed"):
            self.stock_mount = StockMount()
        stock = self.stock_mount if self.stock_mount.root is not None else None
        summary = None if stock else state.summary
        connected = bool(stock or link.get("connected") or state.source == "device")
        model = summary.model if summary else "ipod6g"
        self.name.setText(MODEL_NAMES.get(model, "iPod"))
        if stock:
            set_badge(self.link_badge, "已连接，原厂固件", "success")
        elif link.get("connected"):
            mode = {"podlink": "PodLink", "disk": "磁盘模式"}.get(link.get("mode"), "已连接")
            set_badge(self.link_badge, f"已连接，{mode}", "success")
        elif state.loading:
            set_badge(self.link_badge, "正在读取…", "busy")
        else:
            set_badge(self.link_badge, "未连接", "neutral")
        if stock:
            self.tagline.setText("显示原厂 iPod Classic 的主菜单。")
        elif self.stock_mount.unsupported:
            self.tagline.setText("原厂固件仅支持 iPod Classic 第六、六点五和第七代。")
        elif summary is None:
            self.tagline.setText("用 USB 连接 iPod 并打开 PodLink，然后点刷新读取它的信息。")
        elif state.source == "cache":
            self.tagline.setText("显示的是上次读取的信息。")
        else:
            self.tagline.setText("信息来自刚才连接的 iPod。")
        serial = None if stock else link.get("serial") or state.serial
        values = {
            "model": MODEL_NAMES.get(model, model) + ("（第六、七代）" if model == "ipod6g" else
                                                      "（第五代）" if model == "ipodvideo" else ""),
            "firmware": ("Apple 原厂固件" if stock else
                         (summary.firmware if summary else link.get("firmware")) or "—"),
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
        self.drawing.set_state(model, connected, stock)
        self._sync_screen_activity()

    def shown(self) -> None:
        state = self.window_.library_state
        if state.summary is not None:
            self.update_view()

    def apply_theme(self, tokens) -> None:
        self.capacity.tokens = tokens
        self.drawing.update()
