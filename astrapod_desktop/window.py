"""The main window: a navigation drawer that folds into a rail below
RAIL_WIDTH, a top app bar with the search bar and status chip, the page
stack, the import FAB and the snackbar. The theme's seed follows the
desktop's accent colour unless the owner chose cover colours or a swatch.

The drawer's 资料库 pages browse Jellyfin. Each device is a node under 设备:
the node opens its device page, and expanding it lists the same five views
of that device's own library, keyed "<device>.<view>"."""
from __future__ import annotations

from dataclasses import dataclass
import json

import podlink_m3
import podlink_settings
from podlink_ui_theme import DEFAULT_SEED, make_app_icon, ui_prefs_path

from . import icons
from .artwork import NO_ART, ArtStore
from .contracts import album_key
from .disk_mode import DiskMode
from .jellyfin import JellyfinLibrary
from .jobs import JobRunner
from .library import LibraryState
from .models import SongsModel
from .qt import QtCore, QtGui, QtWidgets, Qt, Signal
from .widgets import (Fab, LcdDisplay, PageStack, RippleFilter, Sidebar,
                      StatusDot, WindowSurface, emphasized, label, refresh_button_icons,
                      set_motion)
from .pages.agents import AgentsPage
from .pages.albums import AlbumsPage
from .pages.browse import BrowsePage
from .pages.device import MODEL_NAMES, DevicePage, device_name
from .pages.extensions import ExtensionsPage
from .pages.extras import ExtrasPage
from .pages.importer import ImportPage
from .pages.info import InfoDialog
from .pages.logs import LogsPage
from .pages.settings import SettingsPage
from .pages.songs import SongsPage
from .pages.sync import SyncPage
from .pages.usage import UsagePage

LIBRARY_VIEWS = [("songs", "歌曲", "songs"), ("albums", "专辑", "albums"),
                 ("artists", "艺人", "artists"), ("genres", "类型", "genres"),
                 ("composers", "作曲者", "composers")]
# The devices the backend knows; each is a sidebar node with LIBRARY_VIEWS
# of its own library under it.
DEVICES = ["device"]
SECTIONS = [
    ("设备", [(key, "iPod", "device") for key in DEVICES]),
    ("资料库", LIBRARY_VIEWS),
    ("添加音乐", [("import", "直接导入", "import"), ("sync", "Jellyfin 同步", "sync")]),
    ("远程", [("agents", "Agent", "agents"), ("usage", "Usage", "usage")]),
    ("电脑", [("extensions", "扩展", "extensions"), ("logs", "日志", "logs"),
            ("settings", "设置", "settings"), ("extras", "日历与联系人", "artists")]),
]


RAIL_WIDTH = 1100
DRAWER = 272
RAIL = 72
FAB_PAGES = tuple(f"{device}.{view}" for device in DEVICES for view, _t, _i in LIBRARY_VIEWS)
_RIPPLES: list[RippleFilter] = []


def ripple_filter() -> RippleFilter:
    """The one ripple filter on the application, shared by every window."""
    if not _RIPPLES:
        ripples = RippleFilter(QtWidgets.QApplication.instance())
        QtWidgets.QApplication.instance().installEventFilter(ripples)
        _RIPPLES.append(ripples)
    return _RIPPLES[0]


@dataclass
class Services:
    library: object
    daemon: object
    plugins: object
    sync: object
    prober: object
    appearance: object = None
    demo: bool = False


class Toast(QtWidgets.QWidget):
    """The M3 snackbar: inverse surface, bottom centre, rising in."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.text = ""
        self.opacity = 0.0
        self.tokens = None
        self._base_y = 0
        self._anim = QtCore.QVariantAnimation(self)
        self._anim.setDuration(250)
        self._anim.setEasingCurve(emphasized())
        self._anim.valueChanged.connect(self._step)
        self._hide = QtCore.QTimer(self)
        self._hide.setSingleShot(True)
        self._hide.timeout.connect(lambda: self._fade(0.0))
        self.hide()

    def _font(self) -> QtGui.QFont:
        font = QtGui.QFont(self.font())
        font.setPixelSize(14)
        return font

    def _step(self, value) -> None:
        self.opacity = float(value)
        if self.opacity <= 0.01:
            self.hide()
        self.move(self.x(), self._base_y + round(12 * (1 - self.opacity)))
        self.update()

    def _fade(self, target: float) -> None:
        self._anim.stop()
        if not self.parentWidget().window().property("motion_on"):
            self._step(target)
            return
        self._anim.setStartValue(self.opacity)
        self._anim.setEndValue(target)
        self._anim.start()

    def show_text(self, text: str) -> None:
        self.text = text
        width = QtGui.QFontMetrics(self._font()).horizontalAdvance(text) + 32
        parent = self.parentWidget()
        width = max(288, min(width, 568, parent.width() - 32))
        self._base_y = parent.height() - 48 - 24
        self.setGeometry((parent.width() - width) // 2, self._base_y, width, 48)
        self.show()
        self.raise_()
        self._fade(1.0)
        self._hide.start(4000)

    def paintEvent(self, _event) -> None:
        t = self.tokens
        if t is None:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setOpacity(self.opacity)
        rect = QtCore.QRectF(self.rect())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(t.inverse_surface))
        painter.drawRoundedRect(rect, 4, 4)
        font = self._font()
        painter.setFont(font)
        painter.setPen(QtGui.QColor(t.inverse_on_surface))
        metrics = QtGui.QFontMetrics(font)
        painter.drawText(rect.adjusted(16, 0, -16, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         metrics.elidedText(self.text, Qt.TextElideMode.ElideRight, int(rect.width() - 32)))
        painter.end()


class LinkChip(QtWidgets.QWidget):
    """The iPod link line at the foot of the drawer; the rail keeps only
    the dot and moves the text into the tooltip."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout_ = QtWidgets.QHBoxLayout(self)
        self.layout_.setContentsMargins(18, 4, 8, 4)
        self.layout_.setSpacing(12)
        self.dot = StatusDot()
        self.layout_.addWidget(self.dot)
        column = QtWidgets.QVBoxLayout()
        column.setSpacing(0)
        self.title = label("iPod 未连接", "section-subtitle")
        self.detail = label("", "helper")
        column.addWidget(self.title)
        column.addWidget(self.detail)
        self.layout_.addLayout(column, 1)

    def set_compact(self, compact: bool) -> None:
        self.compact = compact
        self.title.setVisible(not compact)
        self.detail.setVisible(not compact)
        self.layout_.setContentsMargins(0 if compact else 18, 4, 0 if compact else 8, 4)
        self.layout_.setStretch(1, 0 if compact else 1)
        self.layout_.setAlignment(Qt.AlignmentFlag.AlignHCenter if compact else Qt.AlignmentFlag.AlignLeft)
        self.refresh_tooltip()

    def refresh_tooltip(self) -> None:
        self.setToolTip("\n".join(x for x in (self.title.text(), self.detail.text()) if x)
                        if getattr(self, "compact", False) else "")


class MainWindow(QtWidgets.QMainWindow):
    device_changed = Signal()
    port_changed = Signal(str)          # the saved iPod port; empty is auto-detect

    def __init__(self, theme_manager, services: Services, parent=None):
        super().__init__(parent)
        self.theme_manager = theme_manager
        self.services = services
        self.runtime = None
        self.device = None
        self._stock_read_started = False
        self.setWindowTitle("AstraPod")
        self.setMinimumSize(720, 560)
        self.resize(1260, 820)
        self.setAcceptDrops(True)
        self.prefs = self._load_prefs()
        self.motion = bool(self.prefs.get("motion", True))
        self.setProperty("motion_on", self.motion)
        set_motion(self.motion)
        self.color_source = theme_manager.color_source
        self.compact = False
        self.ripples = ripple_filter()
        self._seed_timer = QtCore.QTimer(self)
        self._seed_timer.setSingleShot(True)
        self._seed_timer.setInterval(220)
        self._seed_timer.timeout.connect(self._apply_seed)
        self._pending_seed = None
        try:
            self.settings = podlink_settings.load()
            self._settings_error = None
        except (OSError, ValueError) as error:
            self.settings = {}
            self._settings_error = error
        self.searches: dict[str, str] = {}
        self.details: dict = {}
        self._ambient_track = None
        self._save_timer = QtCore.QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self.save_prefs)

        services.library.port = self.prefs.get("port", "")
        self.runner = JobRunner(self)
        self.art = ArtStore(services.library, self.runner, self)
        self.art.art_ready.connect(lambda key: self._ambient_art_ready(key, self.art))
        self.library_state = LibraryState(services.library, self.runner, self.art, self)
        self.songs_model = SongsModel(self)
        self.library_state.songs_model = self.songs_model
        self.jellyfin = JellyfinLibrary(services.sync, self.runner,
                                        lambda: self.navigate("sync"), self)
        self.jellyfin.art.art_ready.connect(
            lambda key: self._ambient_art_ready(key, self.jellyfin.art))
        self._ambient_art = self.art
        self.daemon = services.daemon
        self.disk = DiskMode(services.sync, self.daemon, self.runner, self)
        self.runner.activity.connect(self._activity)
        self.library_state.changed.connect(self._library_changed)
        self.library_state.track_removed.connect(self.songs_model.remove_track)

        self.ipods: list[dict] = []
        self._link_key = None
        self._disk_link_key = None
        self._build()
        self.disk.set_port(services.library.port)
        self.port_changed.connect(self.disk.set_port)
        self.daemon.state_changed.connect(self._daemon_state)
        self.daemon.connection_changed.connect(lambda _c: self._daemon_state(self.daemon.state))
        self.theme_manager.theme_changed.connect(self._apply_theme)
        self.theme_manager.system_changed.connect(self._system_appearance_changed)
        self._apply_theme(theme_manager.mode, theme_manager.resolved)
        self.stack.animate = self.motion
        self._apply_compact(self.width() < RAIL_WIDTH)
        self.library_state.load_cached()
        self.navigate(self.prefs.get("page") if self.prefs.get("page") in self.pages else "songs")
        self._daemon_state(self.daemon.state)
        self._device_timer = QtCore.QTimer(self)
        self._device_timer.setInterval(1500)
        self._device_timer.timeout.connect(self._poll_device)
        if hasattr(services.library, "detect_device"):
            self._poll_device()
            self._device_timer.start()
        if self._settings_error is not None:
            QtCore.QTimer.singleShot(0, lambda: self.show_error(self._settings_error))

    # Preferences ------------------------------------------------------
    def _load_prefs(self) -> dict:
        try:
            data = json.loads(ui_prefs_path().read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_prefs(self) -> None:
        current = self._load_prefs()
        current.update({k: v for k, v in self.prefs.items() if k != "theme"})
        try:
            podlink_settings.write_private(ui_prefs_path(), json.dumps(current, ensure_ascii=False, indent=2))
        except OSError:
            pass

    def save_prefs_later(self) -> None:
        self._save_timer.start()

    def save_pref(self, key: str, value) -> None:
        changed = self.prefs.get(key) != value
        self.prefs[key] = value
        if key == "port":
            self.services.library.port = value
        self.save_prefs()
        if key == "port" and changed:
            self._poll_device()
            self.port_changed.emit(value)

    # Layout -------------------------------------------------------------
    def _build(self) -> None:
        self.surface = WindowSurface()
        self.setCentralWidget(self.surface)
        root = QtWidgets.QHBoxLayout(self.surface)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.side_panel = QtWidgets.QWidget()
        side = self.side_layout = QtWidgets.QVBoxLayout(self.side_panel)
        side.setContentsMargins(4, 16, 4, 14)
        side.setSpacing(6)
        self.brand = QtWidgets.QHBoxLayout()
        self.brand.setContentsMargins(24, 0, 0, 4)
        self.brand.setSpacing(12)
        self.brand_icon = QtWidgets.QLabel()
        self.brand_icon.setFixedSize(32, 32)
        self.brand.addWidget(self.brand_icon)
        self.brand_name = label("AstraPod", "sidebar-brand")
        self.brand.addWidget(self.brand_name)
        self.brand.addStretch(1)
        side.addLayout(self.brand)
        self.sidebar = Sidebar()
        for header, pages in SECTIONS:
            self.sidebar.add_header(header)
            for key, text, icon_name in pages:
                self.sidebar.add_page(key, text, icon_name)
                if key in DEVICES:
                    for view, view_text, view_icon in LIBRARY_VIEWS:
                        self.sidebar.add_page(f"{key}.{view}", view_text, view_icon, parent=key)
        expanded = self.prefs.get("sidebar_expanded")
        for key in expanded if isinstance(expanded, list) else ():
            self.sidebar.set_expanded(key, True)
        self.sidebar.expanded_changed.connect(self._node_expanded)
        self.sidebar.page_selected.connect(self.navigate)
        self.sidebar.files_dropped.connect(self.import_paths)
        side.addWidget(self.sidebar, 1)
        self.link_chip = LinkChip()
        side.addWidget(self.link_chip)
        self.side_panel.setFixedWidth(DRAWER)
        root.addWidget(self.side_panel)

        main = QtWidgets.QVBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(16, 12, 24, 4)
        bar.setSpacing(16)
        self.search = QtWidgets.QLineEdit()
        self.search.setObjectName("SearchField")
        self.search.setPlaceholderText("搜索")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(220)
        self.search.setMaximumWidth(560)
        self.search.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                  QtWidgets.QSizePolicy.Policy.Fixed)
        self.search.setAccessibleName("搜索")
        self.search_action = self.search.addAction(QtGui.QIcon(), QtWidgets.QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self._search_changed)
        bar.addWidget(self.search, 3)
        bar.addStretch(1)
        self.lcd = LcdDisplay()
        self.lcd.clicked.connect(self._lcd_clicked)
        bar.addWidget(self.lcd, 2)
        main.addLayout(bar)

        self.stack = PageStack()
        self.pages = {}
        for page in (DevicePage(self), *self._library_pages(self.library_state, "device."),
                     *self._library_pages(self.jellyfin), ImportPage(self), SyncPage(self),
                     AgentsPage(self), UsagePage(self),
                     ExtensionsPage(self), LogsPage(self), SettingsPage(self), ExtrasPage(self)):
            self.pages[page.key] = page
            self.stack.addWidget(page)
        sync = self.pages["sync"]
        sync.source_state.connect(lambda: self.jellyfin.follow(sync))
        self.jellyfin.follow(sync)
        main.addWidget(self.stack, 1)
        root.addLayout(main, 1)
        self.fab = Fab("导入音乐", "import", self.surface)
        self.fab.setToolTip("导入音乐（Ctrl+O）")
        self.fab.clicked.connect(self._add_files)
        self.toast = Toast(self.surface)

        for sequence, slot in (("Ctrl+F", self.focus_search), ("Ctrl+O", self._add_files),
                               ("Ctrl+R", self.refresh_library),
                               ("Ctrl+,", lambda: self.navigate("settings"))):
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(sequence), self)
            shortcut.activated.connect(slot)
        keys = [key for _h, pages in SECTIONS for key, _t, _i in pages]
        for number, key in enumerate(keys[:9], 1):
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(f"Ctrl+{number}"), self)
            shortcut.activated.connect(lambda k=key: self.navigate(k))

    def _library_pages(self, library, prefix: str = "") -> list:
        """The five views of one library, keyed prefix + view."""
        return [SongsPage(self, library, prefix + "songs"),
                AlbumsPage(self, library, prefix + "albums"),
                *(BrowsePage(self, view, library, prefix + view)
                  for view in ("artists", "genres", "composers"))]

    def _node_expanded(self, _key: str, _expanded: bool) -> None:
        self.prefs["sidebar_expanded"] = [key for key in DEVICES if self.sidebar.is_expanded(key)]
        self.save_prefs_later()

    # Navigation --------------------------------------------------------
    def navigate(self, key: str) -> None:
        page = self.pages.get(key)
        if page is None:
            return
        if self.sidebar.current_key() != key:
            self.sidebar.select(key)
            return
        previous = self.stack.currentWidget()
        if previous is not None and getattr(previous, "searchable", False):
            self.searches[previous.key] = self.search.text()
        self.stack.show_page(page)
        blocked = self.search.blockSignals(True)
        self.search.setText(self.searches.get(key, ""))
        self.search.blockSignals(blocked)
        self.search.setEnabled(page.searchable)
        self.search.setPlaceholderText(
            ("筛选日志" if key == "logs" else getattr(page, "search_hint", "搜索资料库"))
            if page.searchable else "")
        self.fab.setVisible(key in FAB_PAGES)
        self._place_fab()
        page.shown()
        if key != self.prefs.get("page"):
            self.prefs["page"] = key
            self.save_prefs_later()

    def current_page(self):
        return self.stack.currentWidget()

    def refresh_library(self) -> None:
        """Re-read the library the current page shows; the iPod's elsewhere."""
        library = getattr(self.current_page(), "library", None) or self.library_state
        library.refresh(False)

    def focus_search(self) -> None:
        if self.search.isEnabled():
            self.search.setFocus()
            self.search.selectAll()

    def _search_changed(self, text: str) -> None:
        page = self.current_page()
        if page is not None and page.searchable:
            page.set_search(text)

    # Theme ---------------------------------------------------------------
    def select_theme(self, mode: str) -> None:
        try:
            self.theme_manager.apply(mode, persist=True)
        except OSError as error:
            self.show_error(error)

    def set_motion(self, on: bool) -> None:
        self.motion = bool(on)
        self.setProperty("motion_on", self.motion)
        set_motion(self.motion)
        self.stack.animate = self.motion
        self.save_pref("motion", self.motion)

    def _apply_theme(self, _mode, _resolved) -> None:
        t = self.theme_manager.tokens
        icon = make_app_icon()
        self.brand_icon.setPixmap(icon.pixmap(32, 32))
        self.setWindowIcon(icon)
        self.ripples.tokens = t
        self.surface.set_tokens(t)
        self.fab.tokens = t
        self.fab.update()
        self.lcd.tokens = t
        self.lcd.update()
        self.toast.tokens = t
        self.sidebar.set_tokens(t)
        self.search_action.setIcon(icons.icon("search", t.on_surface_variant, 24))
        refresh_button_icons(self, t)
        for page in self.pages.values():
            page.apply_theme(t)
        self._daemon_state(self.daemon.state)

    # Seed colour ---------------------------------------------------------
    @property
    def fallback_seed(self) -> str:
        """The swatch the owner picked; used only with the swatch source."""
        seed = self.prefs.get("seed")
        return seed if isinstance(seed, str) and seed.startswith("#") else DEFAULT_SEED

    def base_seed(self) -> str:
        """The seed when no cover applies: the chosen swatch, or the
        desktop's accent colour."""
        if self.color_source == "swatch":
            return self.fallback_seed
        return self.theme_manager.system_seed()

    def set_ambient_image(self, image: QtGui.QImage) -> None:
        """Seed the theme from a cover: the hue holding the most chroma,
        or the base seed for a grey one."""
        if image is None or image.isNull():
            return
        small = image.scaled(32, 32, Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        small = small.convertToFormat(QtGui.QImage.Format.Format_ARGB32)
        pixels = [small.pixel(x, y) for y in range(small.height()) for x in range(small.width())]
        seed = podlink_m3.seed_from_pixels(pixels)
        self._queue_seed(podlink_m3.argb_to_hex(seed) if seed is not None else self.base_seed())

    def show_track_art(self, track, art=None) -> None:
        """With the cover source, the theme follows this track's cover,
        read from art (the iPod's by default); otherwise, or without a
        cover, the base seed holds."""
        self._ambient_track = track
        self._ambient_art = art or self.art
        if self.color_source != "cover" or track is None:
            self._queue_seed(self.base_seed())
            return
        image = self._ambient_art.get(track, "coverflow")
        if isinstance(image, QtGui.QImage):
            self.set_ambient_image(image)
        elif image is NO_ART:
            self._queue_seed(self.base_seed())

    def _ambient_art_ready(self, key, art) -> None:
        track = self._ambient_track
        if track is not None and key == album_key(track) and art is self._ambient_art:
            self.show_track_art(track, self._ambient_art)

    def _queue_seed(self, seed: str) -> None:
        self._pending_seed = seed
        self._seed_timer.start()

    def _apply_seed(self) -> None:
        if self._pending_seed:
            self.theme_manager.set_seed(self._pending_seed)

    def set_color_source(self, source: str) -> None:
        if source == self.color_source:
            return
        self.color_source = source
        self.theme_manager.color_source = source
        self.save_pref("color_source", source)
        self.pages["settings"].show_color_source()
        self.show_track_art(self._ambient_track, self._ambient_art)

    def set_fallback_seed(self, seed: str) -> None:
        """Picking a swatch also selects the swatch source."""
        self.theme_manager.swatch_seed = seed
        self.save_pref("seed", seed)
        if self.color_source != "swatch":
            self.set_color_source("swatch")
        else:
            self.show_track_art(self._ambient_track, self._ambient_art)

    def _system_appearance_changed(self) -> None:
        self.pages["settings"].show_color_source()
        self.show_track_art(self._ambient_track, self._ambient_art)

    # Navigation width ----------------------------------------------------
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        compact = self.width() < RAIL_WIDTH
        if compact != self.compact:
            self._apply_compact(compact)
        self._place_fab()

    def _apply_compact(self, compact: bool) -> None:
        """Below RAIL_WIDTH the drawer becomes a 72 px rail, edge to edge."""
        self.compact = compact
        self.side_panel.setFixedWidth(RAIL if compact else DRAWER)
        self.side_layout.setContentsMargins(0 if compact else 4, 16, 0 if compact else 4, 14)
        self.brand_name.setVisible(not compact)
        self.brand_icon.setVisible(not compact)
        self.sidebar.set_compact(compact)
        self.link_chip.set_compact(compact)
        self._place_fab()

    def _place_fab(self) -> None:
        if not hasattr(self, "fab"):
            return
        area = self.stack.geometry()
        self.fab.move(area.right() - self.fab.width() - 12, area.bottom() - self.fab.height() - 12)
        self.fab.raise_()
        if self.toast.isVisible():
            self.toast.raise_()

    # Library --------------------------------------------------------------
    def _library_changed(self) -> None:
        state = self.library_state
        if state.tracks is not getattr(self, "_shown_tracks", None):
            self._shown_tracks = state.tracks
            self.songs_model.set_tracks(state.tracks)
            if not state.tracks:
                self.show_track_art(None)
        if state.error and not state.loading:
            self.flash(f"读取 iPod 失败：{state.error}")
        for key in DEVICES:
            self.sidebar.set_text(key, device_name(state.summary))
        self._update_lcd()

    def track_detail(self, track, callback) -> None:
        """callback(track, detail, error) on the GUI thread; cached per track."""
        state = self.library_state
        key = (state.art_identity, track.index, track.path)
        if self.device is not None and state.source != "device":
            callback(track, None, RuntimeError("请连接歌曲来源的 iPod 并读取资料库。"))
            return
        if key in self.details:
            callback(track, self.details[key], None)
            return
        library = self.services.library

        session_for_device = getattr(library, "session_for_device", None)
        open_session = session_for_device() if session_for_device else library.session

        def work(_job):
            with open_session() as session:
                return session.detail(track)

        def done(detail):
            self.details[key] = detail
            callback(track, detail, None)

        self.runner.run("正在读取文件信息…", work, done, lambda e: callback(track, None, e),
                        foreground=False)

    def show_info(self, tracks) -> None:
        if not tracks:
            return
        dialog = InfoDialog(self, tracks)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dialog.show()
        self.info_dialog = dialog

    def confirm_delete(self, tracks) -> None:
        if not self.library_state.can_delete:
            return
        managed = [t for t in tracks if t.managed]
        if not managed:
            self.flash("只能删除用 PodLink 导入或 Jellyfin 同步到 iPod 的歌曲。")
            return
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setWindowTitle("从 iPod 删除")
        if len(managed) == 1:
            box.setText(f"从 iPod 删除「{managed[0].title}」？")
        else:
            box.setText(f"从 iPod 删除 {len(managed)} 首歌曲？")
        skipped = len(tracks) - len(managed)
        note = "文件会从 iPod 上永久删除，电脑上的原文件不受影响。"
        if skipped:
            note += f"另外 {skipped} 首不是 PodLink 导入的，会保留。"
        box.setInformativeText(note)
        cancel = box.addButton("取消", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        remove = box.addButton("删除", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        remove.setProperty("variant", "danger")
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        self.delete_box = box
        box.exec()
        if box.clickedButton() is remove:
            self.delete_tracks(managed)

    def delete_tracks(self, tracks) -> None:
        pending = list(tracks)

        def next_one(error=None):
            if error is not None:
                self.show_error(error)
                return
            if pending:
                self.library_state.delete(pending.pop(0), next_one)
            else:
                self.flash(f"已从 iPod 删除 {len(tracks)} 首歌曲")

        next_one()

    # Import --------------------------------------------------------------
    def import_paths(self, paths) -> None:
        self.navigate("import")
        self.pages["import"].add_paths(paths)

    def _add_files(self) -> None:
        self.navigate("import")
        self.pages["import"].add_files()

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self.import_paths(paths)

    # Daemon state ---------------------------------------------------------
    def _poll_device(self) -> None:
        library = self.services.library
        detect = getattr(library, "detect_device", None)
        if detect is None:
            return
        device = detect()
        if device != self.device:
            self.device = library.device = device
            self._stock_read_started = False
            self.library_state.device_changed(device)
            self.disk.set_device(device)
            self.device_changed.emit()
            self._daemon_state(self.daemon.state)
        self._maybe_read_stock()

    def _maybe_read_stock(self) -> None:
        state = self.library_state
        if (self.device is not None and self.device.kind == "stock"
                and self.isVisible() and not self.isMinimized() and not self.busy
                and not state.loading and not self._stock_read_started):
            self._stock_read_started = True
            state.refresh(False)

    def link_state(self) -> dict:
        remote = self.daemon.state.get("link") or {}
        device = self.device
        if device is None:
            return remote
        mode = device.link_mode
        return dict(connected=mode != "none", mode=mode, serial=device.serial,
                    port=device.port, borrowed=mode == "podlink" and remote.get("borrowed", False),
                    firmware=remote.get("firmware") if mode == "podlink" else None)

    def _daemon_state(self, _state: dict) -> None:
        link = self.link_state()
        disk_key = (self.daemon.connected, link.get("mode"), link.get("serial"),
                    link.get("port"), link.get("borrowed"))
        if disk_key != self._disk_link_key:
            self._disk_link_key = disk_key
            self.disk.update()
        t = self.theme_manager.tokens
        if not self.daemon.connected and not link.get("connected"):
            self.link_chip.dot.set_state(QtGui.QColor(t.idle), False)
            self.link_chip.title.setText("后台服务未连接")
            self.link_chip.detail.setText("远程、Agent 和 Usage 暂不可用")
        elif link.get("connected"):
            self.link_chip.dot.set_state(QtGui.QColor(t.working), link.get("borrowed", False))
            self.link_chip.title.setText("iPod 已连接")
            mode = {"podlink": "PodLink", "disk": "磁盘模式",
                    "stock": "Apple 原厂固件"}.get(link.get("mode"), "")
            detail = "电脑正在使用串口" if link.get("borrowed") else " ".join(
                x for x in (mode, link.get("port") or "") if x)
            self.link_chip.detail.setText(detail)
        else:
            self.link_chip.dot.set_state(QtGui.QColor(t.idle), False)
            self.link_chip.title.setText("iPod 未连接")
            self.link_chip.detail.setText("用 USB 连接 iPod Classic")
        self.link_chip.refresh_tooltip()
        key = (bool(link.get("connected")), link.get("port"), link.get("mode"))
        if key != self._link_key:
            self._link_key = key
            self.detect_ipods()
        self._update_lcd()

    def _update_lcd(self) -> None:
        if self.lcd.activity:
            return
        link = self.link_state()
        state = self.library_state
        if link.get("connected"):
            if state.source == "device":
                detail = f"{len(state.tracks):,} 首歌曲"
            else:
                detail = "Apple 原厂固件" if link.get("mode") == "stock" else "已连接，尚未读取资料库"
            self.lcd.set_idle("iPod 已连接", detail, connected=True)
        elif state.tracks:
            self.lcd.set_idle("AstraPod", f"资料库：{len(state.tracks):,} 首歌曲（上次读取）")
        else:
            self.lcd.set_idle("AstraPod", "连接 iPod 后开始")

    # Choosing among several iPods ----------------------------------------
    def detect_ipods(self) -> None:
        """With more than one iPod attached, the status chip becomes a menu."""
        detect = getattr(self.services.library, "detect_ports", None)
        self.ipods = detect() if detect is not None else []
        several = len(self.ipods) > 1
        self.lcd.set_menu(several, f"检测到 {len(self.ipods)} 台 iPod，点按选择要连接的一台"
                          if several else "")

    def ipod_label(self, ipod: dict) -> str:
        state = self.library_state
        if state.summary is not None and ipod.get("serial") and ipod["serial"] == state.serial:
            name = MODEL_NAMES.get(state.summary.model, "iPod")
        else:
            name = ipod.get("product") or "iPod"
        return f"{name} · {ipod.get('serial') or ipod['port'].rsplit('/', 1)[-1]}"

    def device_menu(self) -> QtWidgets.QMenu:
        menu = QtWidgets.QMenu(self)
        chosen = self.prefs.get("port") or ""
        group = QtGui.QActionGroup(menu)
        for ipod in self.ipods:
            action = menu.addAction(self.ipod_label(ipod))
            action.setCheckable(True)
            action.setChecked(ipod["port"] == chosen)
            action.setToolTip(ipod["port"])
            action.triggered.connect(lambda _checked=False, port=ipod["port"]: self.choose_ipod(port))
            group.addAction(action)
        menu.addSeparator()
        menu.addAction("查看设备页", lambda: self.navigate("device"))
        return menu

    def choose_ipod(self, port: str) -> None:
        """Connect to this iPod from now on: the saved port, used by the
        library and sync."""
        self.save_pref("port", port)
        self.library_state.refresh(False)

    def _lcd_clicked(self) -> None:
        self.detect_ipods()
        if len(self.ipods) < 2:
            self.navigate("device")
            return
        menu = self.device_menu()
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self.lcd.mapToGlobal(QtCore.QPoint(0, self.lcd.height() + 4)))

    def _activity(self, text: str, fraction) -> None:
        self.lcd.set_activity(text, fraction)
        if not text:
            self._update_lcd()

    # Settings ---------------------------------------------------------------
    def settings_saved(self, values: dict) -> None:
        self.settings = values
        sync = self.pages["sync"]
        if sync.session is None and values.get("JELLYFIN_URL", "") != sync.url.text():
            sync.url.setText(values.get("JELLYFIN_URL", ""))
        if self.daemon.connected:
            self.daemon.request("reload", lambda ok, r: None if ok else self.restart_remote())
        elif self.runtime is not None and not self.busy:
            self.runtime.reload_remote()

    def restart_remote(self) -> None:
        if self.runtime is not None:
            self.runtime.reload_remote()
            self.flash("正在重新启动后台远程服务")

    # Feedback ---------------------------------------------------------------
    def flash(self, text: str) -> None:
        self.toast.show_text(text)

    def show_error(self, error) -> None:
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Icon.Critical)
        box.setWindowTitle("操作失败")
        box.setText("操作失败")
        box.setInformativeText(str(error) or type(error).__name__)
        box.setDefaultButton(box.addButton("确定", QtWidgets.QMessageBox.ButtonRole.AcceptRole))
        box.exec()

    # Lifecycle --------------------------------------------------------------
    @property
    def busy(self) -> bool:
        return self.runner.busy

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._maybe_read_stock()

    def closeEvent(self, event) -> None:
        if self.busy or (self.runtime is not None and self.runtime.tray is not None):
            event.ignore()
            if not self.busy:
                self.hide()
            else:
                self.flash("正在处理，完成后才能关闭窗口。")
            return
        self.save_prefs()
        super().closeEvent(event)
