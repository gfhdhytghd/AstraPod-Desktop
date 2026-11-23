"""Songs: the full library table, and the SongsTable reused by browsers."""
from __future__ import annotations

from .. import icons, models
from ..models import COLUMNS, COLUMN_INDEX, SongsProxy
from ..qt import QtCore, QtGui, QtWidgets, Qt, Signal
from ..widgets import (PageHeader, StarDelegate, button, configure_table, label,
                       make_card)
from . import Page


class SongsTable(QtWidgets.QTableView):
    """A filtered view of a library's SongsModel. A library that is not
    editable (Jellyfin) offers only copying: no info and no delete."""

    info_requested = Signal(list)
    delete_requested = Signal(list)

    def __init__(self, window, columns_pref: str | None = "columns", model=None,
                 keys: list[str] | None = None, defaults: list[str] | None = None,
                 parent=None, library=None):
        super().__init__(parent)
        self.window_ = window
        self.library = library or window.library_state
        self.defaults = defaults
        self.columns_pref = columns_pref
        self.fixed_keys = keys
        self.proxy = SongsProxy(self)
        source = model or self.library.songs_model
        self.proxy.setSourceModel(source)
        self.setModel(self.proxy)
        configure_table(self)
        self.stars = StarDelegate(models.RATING_ROLE, self)
        self.stars.tokens = window.theme_manager.tokens
        window.theme_manager.theme_changed.connect(
            lambda *_: self.apply_theme(window.theme_manager.tokens))
        self.setItemDelegateForColumn(COLUMN_INDEX["rating"], self.stars)
        header = self.horizontalHeader()
        if keys is None:
            header.setSortIndicator(source.sort_column, source.sort_order)
            self.setSortingEnabled(True)
            header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            header.customContextMenuRequested.connect(self.column_menu)
            header.sectionResized.connect(self._save_widths)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self.row_menu)
        self.doubleClicked.connect(self._double_clicked)
        self.selectionModel().currentRowChanged.connect(self._current_changed)
        for column in COLUMNS:
            self.setColumnWidth(COLUMN_INDEX[column.key], column.width)
        widths = window.prefs.get("column_widths", {}) if keys is None else {}
        for key, width in widths.items() if isinstance(widths, dict) else ():
            if key in COLUMN_INDEX and isinstance(width, int):
                self.setColumnWidth(COLUMN_INDEX[key], max(36, width))
        self.apply_columns()
        self.setAccessibleName("歌曲列表")

    def _double_clicked(self, _index) -> None:
        if self.library.editable:
            self.info_requested.emit(self.selected_tracks())

    def _current_changed(self, current, _previous) -> None:
        track = current.data(models.TRACK_ROLE) if current.isValid() else None
        if track is not None:
            self.window_.show_track_art(track, self.library.art)

    def visible_keys(self) -> list[str]:
        if self.fixed_keys is not None:
            return list(self.fixed_keys)
        stored = self.window_.prefs.get(self.columns_pref) if self.columns_pref else None
        if isinstance(stored, list) and stored:
            keys = [key for key in stored if key in COLUMN_INDEX]
            if "title" not in keys:
                keys.insert(0, "title")
            return keys
        if self.defaults:
            return list(self.defaults)
        return [column.key for column in COLUMNS if column.visible]

    def apply_columns(self) -> None:
        keys = set(self.visible_keys())
        for column in COLUMNS:
            self.setColumnHidden(COLUMN_INDEX[column.key], column.key not in keys)
        if self.fixed_keys:
            header = self.horizontalHeader()
            for position, key in enumerate(self.fixed_keys):
                header.moveSection(header.visualIndex(COLUMN_INDEX[key]), position)

    def column_menu(self, position) -> None:
        menu = QtWidgets.QMenu(self)
        keys = self.visible_keys()
        for column in COLUMNS:
            action = menu.addAction(column.title)
            action.setCheckable(True)
            action.setChecked(column.key in keys)
            action.setEnabled(column.key != "title")
            action.toggled.connect(lambda on, k=column.key: self.toggle_column(k, on))
        menu.exec(self.horizontalHeader().mapToGlobal(position))

    def toggle_column(self, key: str, on: bool) -> None:
        keys = self.visible_keys()
        if on and key not in keys:
            keys.append(key)
        elif not on and key in keys and key != "title":
            keys.remove(key)
        self.window_.save_pref(self.columns_pref, [c.key for c in COLUMNS if c.key in keys])
        for table in self.window_.findChildren(SongsTable):
            if table.columns_pref == self.columns_pref:
                table.apply_columns()

    def _save_widths(self, index, _old, new) -> None:
        dragging = QtWidgets.QApplication.mouseButtons() & Qt.MouseButton.LeftButton
        if self.columns_pref and new > 0 and dragging:
            widths = dict(self.window_.prefs.get("column_widths") or {})
            widths[COLUMNS[index].key] = new
            self.window_.prefs["column_widths"] = widths
            self.window_.save_prefs_later()

    def selected_tracks(self) -> list:
        rows = sorted({index.row() for index in self.selectionModel().selectedRows()})
        return [self.proxy.index(row, 0).data(models.TRACK_ROLE) for row in rows]

    def row_menu(self, position) -> None:
        tracks = self.selected_tracks()
        if not tracks:
            return
        t = self.window_.theme_manager.tokens
        menu = QtWidgets.QMenu(self)
        if self.library.editable:
            menu.addAction(icons.icon("info", t.text_secondary, 16), "显示简介",
                           lambda: self.info_requested.emit(tracks))
        menu.addAction(icons.icon("copy", t.text_secondary, 16), "复制歌曲信息",
                       lambda: self.copy(tracks))
        if not self.library.editable or not self.library.can_delete:
            menu.exec(self.viewport().mapToGlobal(position))
            return
        menu.addSeparator()
        remove = menu.addAction(icons.icon("trash", t.danger, 16), "从 iPod 删除…",
                                lambda: self.delete_requested.emit(tracks))
        remove.setEnabled(all(track.managed for track in tracks))
        if not remove.isEnabled():
            remove.setText("从 iPod 删除…（只能删除 PodLink 导入的歌曲）")
        menu.exec(self.viewport().mapToGlobal(position))

    def copy(self, tracks) -> None:
        lines = ["\t".join((t.title, t.artist, t.album, self.model().sourceModel().display(t, "length")))
                 for t in tracks]
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))
        self.window_.flash("已复制 %d 首歌曲的信息" % len(tracks))

    def keyPressEvent(self, event) -> None:
        editable = self.library.editable
        if (editable and self.library.can_delete
                and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)):
            tracks = self.selected_tracks()
            if tracks:
                self.delete_requested.emit(tracks)
                return
        if editable and (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                or (event.key() == Qt.Key.Key_I
                    and event.modifiers() & Qt.KeyboardModifier.ControlModifier)):
            tracks = self.selected_tracks()
            if tracks:
                self.info_requested.emit(tracks)
                return
        if event.matches(QtGui.QKeySequence.StandardKey.Copy):
            tracks = self.selected_tracks()
            if tracks:
                self.copy(tracks)
                return
        super().keyPressEvent(event)

    def apply_theme(self, tokens) -> None:
        self.stars.tokens = tokens
        self.viewport().update()


class EmptyState(QtWidgets.QWidget):
    """Centered glyph, a sentence and one action."""

    def __init__(self, icon_name: str, title: str, text: str, action: str | None = None,
                 slot=None, parent=None):
        super().__init__(parent)
        self.icon_name = icon_name
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch(1)
        self.glyph = QtWidgets.QLabel()
        self.glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.glyph)
        self.title = label(title, "hero-title")
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title)
        self.text = label(text, "page-subtitle", wrap=True)
        self.text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.text)
        self.slot = slot
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        self.action = button(action or "", "primary", slot=lambda: self.slot and self.slot())
        self.action.setVisible(bool(action))
        row.addWidget(self.action)
        row.addStretch(1)
        layout.addSpacing(8)
        layout.addLayout(row)
        layout.addStretch(1)

    def set_content(self, title: str, text: str, action: str | None = None, slot=None) -> None:
        self.title.setText(title)
        self.text.setText(text)
        self.slot = slot
        self.action.setText(action or "")
        self.action.setVisible(bool(action))

    def apply_theme(self, tokens) -> None:
        self.glyph.setPixmap(icons.icon(self.icon_name, tokens.text_disabled, 56).pixmap(56, 56))


class SongsPage(Page):
    key = "songs"
    searchable = True

    def __init__(self, window, library=None, key: str = "songs", parent=None):
        super().__init__(window, parent)
        self.key = key
        self.library = library = library or window.library_state
        self.search_hint = library.search_hint
        self.header = PageHeader("歌曲")
        self.playlist_filter = None
        self._playlists = ()
        if hasattr(library, "playlists"):
            self.playlist_filter = self.header.add(QtWidgets.QComboBox())
            self.playlist_filter.setAccessibleName("播放列表")
            self.playlist_filter.hide()
            self.playlist_filter.currentIndexChanged.connect(self._filter_playlist)
        if library.editable:
            self.refresh_button = self.header.add(button("读取 iPod", None, "refresh",
                                                         lambda: library.refresh(True)))
            self.refresh_button.setToolTip("从 iPod 重新读取整个资料库")
            self.info_button = self.header.add(button("显示简介", None, "info", self._info))
        else:
            self.refresh_button = self.header.add(button("重新读取", None, "refresh",
                                                         lambda: library.refresh(True)))
            self.refresh_button.setToolTip("从 Jellyfin 重新读取资料库")
        self.layout_.addWidget(self.header)

        card, card_layout = make_card(0, expand=True, margins=(4, 4, 4, 4))
        self.stack = QtWidgets.QStackedWidget()
        self.table = SongsTable(window, library=library)
        self.table.info_requested.connect(window.show_info)
        self.table.delete_requested.connect(window.confirm_delete)
        self.empty = EmptyState("songs", *library.empty_content("songs", "歌曲"))
        self.stack.addWidget(self.table)
        self.stack.addWidget(self.empty)
        card_layout.addWidget(self.stack)
        self.layout_.addWidget(card, 1)

        self.status = label("", "helper")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.layout_.addWidget(self.status)
        library.changed.connect(self.update_state)
        self.table.proxy.rowsInserted.connect(self.update_status)
        self.table.proxy.rowsRemoved.connect(self.update_status)
        self.table.proxy.modelReset.connect(self.update_status)
        self.table.proxy.layoutChanged.connect(self.update_status)
        self.update_state()

    def _info(self) -> None:
        tracks = self.table.selected_tracks()
        if tracks:
            self.window_.show_info(tracks)

    def _filter_playlist(self) -> None:
        index = self.playlist_filter.currentIndex()
        ids = frozenset(self._playlists[index - 1].track_ids) if index > 0 else None
        self.table.proxy.set_track_ids(ids)
        self.update_status()

    def _update_playlists(self) -> None:
        combo = self.playlist_filter
        if combo is None:
            return
        playlists = tuple(item for item in self.library.playlists if not item.is_master)
        if self._playlists == playlists:
            return
        selected = combo.currentData()
        self._playlists = playlists
        with QtCore.QSignalBlocker(combo):
            combo.clear()
            combo.addItem("所有歌曲")
            for playlist in playlists:
                combo.addItem(playlist.name, playlist.id)
            combo.setCurrentIndex(max(0, combo.findData(selected)))
        combo.setVisible(bool(playlists))
        self._filter_playlist()

    def update_state(self) -> None:
        library = self.library
        self.stack.setCurrentWidget(self.table if library.tracks else self.empty)
        if not library.tracks:
            self.empty.set_content(*library.empty_content("songs", "歌曲"))
        self.refresh_button.setEnabled(not library.loading and (library.editable or
                                                                library.connection is not None))
        self._update_playlists()
        self.header.subtitle.setText(library.status_line())
        self.update_status()

    def update_status(self, *_args) -> None:
        self.status.setText(models.summary_line(self.table.proxy.tracks())
                            if self.library.tracks else "")

    def shown(self) -> None:
        if not self.library.editable:
            self.library.ensure_loaded()

    def set_search(self, text: str) -> None:
        self.table.proxy.set_search(text)
        self.update_status()

    def apply_theme(self, tokens) -> None:
        self.table.apply_theme(tokens)
        self.empty.apply_theme(tokens)
