"""Albums: an artwork grid with lazy covers, and an album's own page."""
from __future__ import annotations

import time

from .. import fmt, models
from ..artwork import NO_ART
from ..models import ALBUM_ROLE, AlbumsModel, SongsModel
from ..qt import QtCore, QtGui, QtWidgets, Qt, Signal
from ..widgets import ArtLabel, PageHeader, button, draw_art, label, make_card
from . import Page
from .songs import EmptyState, SongsTable

R = QtCore.QRectF
TILE_W, TILE_H, ART = 184, 236, 156


class AlbumDelegate(QtWidgets.QStyledItemDelegate):
    def __init__(self, view: "AlbumGrid"):
        super().__init__(view)
        self.view = view

    def sizeHint(self, _option, _index):
        return QtCore.QSize(TILE_W, TILE_H)

    def paint(self, painter, option, index):
        view = self.view
        album = index.data(ALBUM_ROLE)
        tokens = view.window_.theme_manager.tokens
        lift = view.hover.get(index.row(), 0.0)
        reveal = view.reveal_amount(index)
        if reveal <= 0:
            return
        painter.save()
        painter.setOpacity(reveal)
        rect = R(option.rect)
        scale = 1.0 + 0.035 * lift
        side = ART * scale
        art_rect = R(rect.center().x() - side / 2, rect.top() + 12 - 5 * lift + (ART - side) / 2,
                     side, side)
        art_rect.translate(0, (1 - reveal) * 10)
        image = view.window_.art.get(album.lead, "coverflow")
        draw_art(painter, art_rect, image if image is not NO_ART else None, album.title,
                 radius=9, shadow=1.0 + 0.9 * lift, loading=image is None, tokens=tokens)
        selected = bool(option.state & QtWidgets.QStyle.StateFlag.State_Selected)
        if selected:
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QtGui.QPen(QtGui.QColor(tokens.accent), 2.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(art_rect.adjusted(-3, -3, 3, 3), 11, 11)
        font = QtGui.QFont(option.font)
        font.setPixelSize(13)
        font.setWeight(QtGui.QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(tokens.text_primary))
        text_rect = R(rect.center().x() - ART / 2, rect.top() + 12 + ART + 10, ART, 18)
        metrics = QtGui.QFontMetrics(font)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignLeft,
                         metrics.elidedText(album.title, Qt.TextElideMode.ElideRight, ART))
        font.setWeight(QtGui.QFont.Weight.Normal)
        font.setPixelSize(12)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(tokens.text_muted))
        metrics = QtGui.QFontMetrics(font)
        painter.drawText(text_rect.translated(0, 18), Qt.AlignmentFlag.AlignLeft,
                         metrics.elidedText(album.artist, Qt.TextElideMode.ElideRight, ART))
        painter.restore()


class AlbumGrid(QtWidgets.QListView):
    """Icon grid; hovered covers lift, and a fresh grid fades in row by row."""

    album_opened = Signal(object)

    def __init__(self, window, model: AlbumsModel, parent=None):
        super().__init__(parent)
        self.window_ = window
        self.setModel(model)
        self.setViewMode(QtWidgets.QListView.ViewMode.IconMode)
        self.setResizeMode(QtWidgets.QListView.ResizeMode.Adjust)
        self.setMovement(QtWidgets.QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setGridSize(QtCore.QSize(TILE_W, TILE_H))
        self.setSpacing(0)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        self.setItemDelegate(AlbumDelegate(self))
        self.setAccessibleName("专辑")
        self.hover: dict[int, float] = {}
        self.hover_row = -1
        self.revealed_at = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._animate)
        self.activated.connect(lambda index: self.album_opened.emit(index.data(ALBUM_ROLE)))
        model.modelReset.connect(self.reveal)

    def resizeEvent(self, event) -> None:
        available = self.viewport().width() - 4
        columns = max(1, available // TILE_W)
        self.setGridSize(QtCore.QSize(available // columns, TILE_H))
        super().resizeEvent(event)

    def reveal(self) -> None:
        self.revealed_at = time.monotonic() if self.window_.motion else 0.0
        self.hover.clear()
        self._timer.start()

    def reveal_amount(self, index) -> float:
        if not self.revealed_at:
            return 1.0
        rect = self.visualRect(index)
        order = max(0, rect.top()) / TILE_H * 4 + rect.left() / TILE_W
        t = time.monotonic() - self.revealed_at - order * 0.03
        return max(0.0, min(1.0, t / 0.3))

    def mouseMoveEvent(self, event) -> None:
        index = self.indexAt(event.position().toPoint())
        row = index.row() if index.isValid() else -1
        if row != self.hover_row:
            self.hover_row = row
            self._timer.start()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:
        self.hover_row = -1
        self._timer.start()
        super().leaveEvent(event)

    def _animate(self) -> None:
        busy = False
        for row in set(self.hover) | ({self.hover_row} if self.hover_row >= 0 else set()):
            target = 1.0 if row == self.hover_row else 0.0
            value = self.hover.get(row, 0.0)
            value += (target - value) * 0.25
            if abs(target - value) < 0.02:
                value = target
            else:
                busy = True
            if value == 0.0:
                self.hover.pop(row, None)
            else:
                self.hover[row] = value
            index = self.model().index(row, 0)
            self.viewport().update(self.visualRect(index).adjusted(-12, -12, 12, 12))
        if self.revealed_at:
            if time.monotonic() - self.revealed_at > 1.6:
                self.revealed_at = 0.0
            busy = True
            self.viewport().update()
        if not busy:
            self._timer.stop()


class AlbumDetail(QtWidgets.QWidget):
    back = Signal()

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window_ = window
        self.album = None
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(22)
        self.art = ArtLabel(212, radius=12, shadow=1.4)
        top.addWidget(self.art, 0, Qt.AlignmentFlag.AlignTop)
        info = QtWidgets.QVBoxLayout()
        info.setSpacing(4)
        self.back_button = button("全部专辑", "ghost", "back", self.back.emit)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.back_button)
        row.addStretch(1)
        info.addLayout(row)
        info.addSpacing(6)
        self.title = label("", "page-title", wrap=True)
        self.artist = label("", "card-title")
        self.meta = label("", "page-subtitle")
        self.totals = label("", "helper")
        for widget in (self.title, self.artist, self.meta):
            info.addWidget(widget)
        info.addStretch(1)
        actions = QtWidgets.QHBoxLayout()
        self.info_button = button("显示简介", None, "info", self._info)
        actions.addWidget(self.info_button)
        actions.addStretch(1)
        info.addLayout(actions)
        info.addWidget(self.totals)
        top.addLayout(info, 1)
        layout.addLayout(top)
        card, card_layout = make_card(0, expand=True, margins=(4, 4, 4, 4))
        self.model = SongsModel(self)
        self.model.sort_column = -1
        self.table = SongsTable(window, None, self.model,
                                ["track", "title", "length", "artist", "plays", "rating"])
        self.table.info_requested.connect(window.show_info)
        self.table.delete_requested.connect(window.confirm_delete)
        card_layout.addWidget(self.table)
        layout.addWidget(card, 1)
        window.art.art_ready.connect(self._art_ready)

    def show_album(self, album) -> None:
        self.album = album
        self.title.setText(album.title)
        self.artist.setText(album.artist)
        bits = [f"{album.year} 年" if album.year else "", album.genre]
        self.meta.setText("，".join(b for b in bits if b))
        self.totals.setText(f"{fmt.songs(len(album.tracks))}，{fmt.long_duration(album.total_ms)}")
        self.model.set_tracks(album.tracks)
        for key, width in (("track", 48), ("title", 320), ("length", 64), ("artist", 200),
                           ("plays", 80)):
            self.table.setColumnWidth(models.COLUMN_INDEX[key], width)
        self._load_art()

    def _load_art(self) -> None:
        if self.album is None:
            return
        image = self.window_.art.get(self.album.lead, "wps")
        if image is None or image is NO_ART:
            small = self.window_.art.get(self.album.lead, "coverflow")
            image = small if isinstance(small, QtGui.QImage) else image
        self.art.tokens = self.window_.theme_manager.tokens
        self.art.set_image(image if isinstance(image, QtGui.QImage) else None, self.album.title)
        self.window_.show_track_art(self.album.lead)

    def _art_ready(self, key) -> None:
        if self.album is not None and key == self.album.key:
            self._load_art()

    def _info(self) -> None:
        tracks = self.table.selected_tracks() or (self.album.tracks if self.album else [])
        if tracks:
            self.window_.show_info(tracks[:1] if not self.table.selected_tracks() else tracks)


class AlbumsPage(Page):
    key = "albums"
    searchable = True

    def __init__(self, window, parent=None):
        super().__init__(window, parent)
        self.stack = QtWidgets.QStackedWidget()
        grid_page = QtWidgets.QWidget()
        grid_layout = QtWidgets.QVBoxLayout(grid_page)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setSpacing(14)
        self.header = PageHeader("专辑")
        grid_layout.addWidget(self.header)
        self.model = AlbumsModel(self)
        self.grid = AlbumGrid(window, self.model)
        self.grid.album_opened.connect(self.open_album)
        self.empty = EmptyState("albums", "还没有专辑", "读取 iPod 资料库后，专辑和插图会显示在这里。",
                                "读取 iPod 资料库", lambda: window.library_state.refresh(True))
        self.inner = QtWidgets.QStackedWidget()
        self.inner.addWidget(self.grid)
        self.inner.addWidget(self.empty)
        grid_layout.addWidget(self.inner, 1)
        self.detail = AlbumDetail(window)
        self.detail.back.connect(self.close_album)
        self.stack.addWidget(grid_page)
        self.stack.addWidget(self.detail)
        self.layout_.addWidget(self.stack)
        window.library_state.changed.connect(self.reload)
        window.art.art_ready.connect(self.model.art_changed)
        self.reload()

    def reload(self) -> None:
        tracks = self.window_.library_state.tracks
        if tracks is not getattr(self, "_shown", None):
            self._shown = tracks
            self.model.set_tracks(tracks)
        self.inner.setCurrentWidget(self.grid if tracks else self.empty)
        self.header.subtitle.setText(f"{len(self.model.albums)} 张专辑" if tracks else "")

    def open_album(self, album) -> None:
        if album is None:
            return
        self.detail.show_album(album)
        self.stack.setCurrentWidget(self.detail)
        self.detail.table.setFocus()

    def close_album(self) -> None:
        self.stack.setCurrentWidget(self.stack.widget(0))
        self.grid.setFocus()

    def set_search(self, text: str) -> None:
        self.model.set_search(text)
        self.header.subtitle.setText(f"{len(self.model.albums)} 张专辑"
                                     if self.window_.library_state.tracks else "")

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Backspace) and \
                self.stack.currentWidget() is self.detail:
            self.close_album()
            return
        super().keyPressEvent(event)
