"""Artists, Genres and Composers: iTunes' column browser over the songs."""
from __future__ import annotations

from .. import models
from ..models import COUNT_ROLE, ValuesModel
from ..qt import QtCore, QtGui, QtWidgets, Qt
from ..widgets import PageHeader, label, make_card, qcolor
from . import Page
from .songs import EmptyState, SongsTable

R = QtCore.QRectF


class ValueDelegate(QtWidgets.QStyledItemDelegate):
    def __init__(self, page):
        super().__init__(page)
        self.page = page

    def sizeHint(self, option, _index):
        return QtCore.QSize(option.rect.width(), 30)

    def paint(self, painter, option, index):
        t = self.page.tokens
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = R(option.rect).adjusted(4, 1, -4, -1)
        if option.state & QtWidgets.QStyle.StateFlag.State_Selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(t.row_selected))
            painter.drawRoundedRect(rect, 7, 7)
        elif option.state & QtWidgets.QStyle.StateFlag.State_MouseOver:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(t.row_hover))
            painter.drawRoundedRect(rect, 7, 7)
        font = QtGui.QFont(option.font)
        font.setPixelSize(13)
        if index.row() == 0:
            font.setWeight(QtGui.QFont.Weight.DemiBold)
        painter.setFont(font)
        count = str(index.data(COUNT_ROLE) or "")
        metrics = QtGui.QFontMetrics(font)
        count_width = metrics.horizontalAdvance(count) + 8
        painter.setPen(QtGui.QColor(t.text_primary))
        text = metrics.elidedText(index.data(Qt.ItemDataRole.DisplayRole) or "",
                                  Qt.TextElideMode.ElideRight, int(rect.width() - count_width - 20))
        painter.drawText(rect.adjusted(10, 0, -count_width, 0),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        painter.setPen(QtGui.QColor(t.text_muted))
        painter.drawText(rect.adjusted(0, 0, -10, 0),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, count)
        painter.restore()


def value_list(page, model) -> QtWidgets.QListView:
    view = QtWidgets.QListView()
    view.setModel(model)
    view.setItemDelegate(ValueDelegate(page))
    view.setMouseTracking(True)
    view.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    view.setUniformItemSizes(True)
    view.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    return view


class BrowsePage(Page):
    searchable = True
    FIELDS = {
        "artists": ("艺人", "artist", "全部艺人", "artists"),
        "genres": ("类型", "genre", "全部类型", "genres"),
        "composers": ("作曲者", "composer", "全部作曲者", "composers"),
    }

    def __init__(self, window, kind: str, library=None, key: str | None = None, parent=None):
        super().__init__(window, parent)
        self.key = key or kind
        self.kind = kind
        self.library = library = library or window.library_state
        self.search_hint = library.search_hint
        title, self.field, all_label, icon_name = self.FIELDS[kind]
        self.title = title
        self.header = PageHeader(title)
        self.layout_.addWidget(self.header)
        self.terms: list[str] = []
        self.values = ValuesModel(self.field, all_label, self)
        self.albums = ValuesModel("album", "全部专辑", self)

        self.body = QtWidgets.QStackedWidget()
        split = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(12)
        left, left_layout = make_card(4, expand=True, margins=(6, 10, 6, 6))
        left_layout.addWidget(label(title, "section-subtitle"))
        self.value_view = value_list(self, self.values)
        self.value_view.setAccessibleName(title)
        left_layout.addWidget(self.value_view)
        split.addWidget(left)
        middle, middle_layout = make_card(4, expand=True, margins=(6, 10, 6, 6))
        middle_layout.addWidget(label("专辑", "section-subtitle"))
        self.album_view = value_list(self, self.albums)
        self.album_view.setAccessibleName("专辑")
        middle_layout.addWidget(self.album_view)
        split.addWidget(middle)
        right, right_layout = make_card(0, expand=True, margins=(4, 4, 4, 4))
        self.table = SongsTable(window, "browse_columns",
                                defaults=["title", "length", "artist", "album", "rating"],
                                library=library)
        self.table.info_requested.connect(window.show_info)
        self.table.delete_requested.connect(window.confirm_delete)
        right_layout.addWidget(self.table)
        split.addWidget(right)
        split.setSizes([230, 210, 700])
        split.setStretchFactor(2, 1)
        self.empty = EmptyState(icon_name, *library.empty_content(kind, title))
        self.body.addWidget(split)
        self.body.addWidget(self.empty)
        self.layout_.addWidget(self.body, 1)
        self.status = label("", "helper")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.layout_.addWidget(self.status)

        self.value_view.selectionModel().currentChanged.connect(self._value_changed)
        self.album_view.selectionModel().currentChanged.connect(self._album_changed)
        library.changed.connect(self.reload)
        self.reload()

    def reload(self) -> None:
        tracks = self.library.tracks
        if not tracks:
            self.empty.set_content(*self.library.empty_content(self.kind, self.title))
        if tracks is getattr(self, "_shown", None):
            return
        self._shown = tracks
        self.values.set_tracks(self._matching())
        self.value_view.setCurrentIndex(self.values.index(0))
        self._value_changed()
        self.body.setCurrentIndex(0 if tracks else 1)
        self.header.subtitle.setText(f"{len(self.values.values)} 项" if tracks else "")

    def _matching(self) -> list:
        tracks = self.library.tracks
        if not self.terms:
            return tracks
        out = []
        for t in tracks:
            hay = " ".join((t.title, t.artist, t.album, t.album_artist, t.genre,
                            t.composer, str(t.year or ""))).casefold()
            if all(term in hay for term in self.terms):
                out.append(t)
        return out

    def _selected_value(self):
        index = self.value_view.currentIndex()
        return index.data(Qt.ItemDataRole.UserRole) if index.isValid() else None

    def _value_changed(self, *_args) -> None:
        value = self._selected_value()
        tracks = self._matching()
        if value is not None:
            tracks = [t for t in tracks if models.field_value(t, self.field) == value]
        self.albums.set_tracks(tracks)
        self.album_view.setCurrentIndex(self.albums.index(0))
        self._album_changed()

    def _album_changed(self, *_args) -> None:
        constraints = {}
        value = self._selected_value()
        if value is not None:
            constraints[self.field] = value
        index = self.album_view.currentIndex()
        album = index.data(Qt.ItemDataRole.UserRole) if index.isValid() else None
        if album is not None:
            constraints["album"] = album
        self.table.proxy.set_constraints(constraints)
        self.status.setText(models.summary_line(self.table.proxy.tracks()))

    def set_search(self, text: str) -> None:
        self.terms = text.casefold().split()
        self.table.proxy.set_search(text)
        self._shown = None
        current = self._selected_value()
        self.reload()
        if current is not None:
            for row, (value, _label, _count) in enumerate(self.values.values, 1):
                if value == current:
                    self.value_view.setCurrentIndex(self.values.index(row))
                    break
        self.status.setText(models.summary_line(self.table.proxy.tracks()))

    def shown(self) -> None:
        if not self.library.editable:
            self.library.ensure_loaded()

    def apply_theme(self, tokens) -> None:
        self.table.apply_theme(tokens)
        self.empty.apply_theme(tokens)
        self.value_view.viewport().update()
        self.album_view.viewport().update()
