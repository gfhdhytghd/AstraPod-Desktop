"""Songs: the full library table, and the SongsTable reused by browsers."""
from __future__ import annotations

from .. import icons, models
from ..models import COLUMNS, COLUMN_INDEX, SongsProxy
from ..qt import QtCore, QtGui, QtWidgets, Qt, Signal
from ..widgets import (PageHeader, StarDelegate, button, configure_table, label,
                       make_card)
from . import Page


class SongsTable(QtWidgets.QTableView):
    """A filtered view of the window's shared SongsModel."""

    info_requested = Signal(list)
    delete_requested = Signal(list)

    def __init__(self, window, columns_pref: str | None = "columns", model=None,
                 keys: list[str] | None = None, defaults: list[str] | None = None,
                 parent=None):
        super().__init__(parent)
        self.window_ = window
        self.defaults = defaults
        self.columns_pref = columns_pref
        self.fixed_keys = keys
        self.proxy = SongsProxy(self)
        source = model or window.songs_model
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
        self.doubleClicked.connect(lambda _i: self.info_requested.emit(self.selected_tracks()))
        self.selectionModel().currentRowChanged.connect(self._current_changed)
        for column in COLUMNS:
            self.setColumnWidth(COLUMN_INDEX[column.key], column.width)
        widths = window.prefs.get("column_widths", {}) if keys is None else {}
        for key, width in widths.items() if isinstance(widths, dict) else ():
            if key in COLUMN_INDEX and isinstance(width, int):
                self.setColumnWidth(COLUMN_INDEX[key], max(36, width))
        self.apply_columns()
        self.setAccessibleName("歌曲列表")

    def _current_changed(self, current, _previous) -> None:
        track = current.data(models.TRACK_ROLE) if current.isValid() else None
        if track is not None:
            self.window_.show_track_art(track)

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
        menu.addAction(icons.icon("info", t.text_secondary, 16), "显示简介",
                       lambda: self.info_requested.emit(tracks))
        menu.addAction(icons.icon("copy", t.text_secondary, 16), "复制歌曲信息",
                       lambda: self.copy(tracks))
        menu.addSeparator()
        remove = menu.addAction(icons.icon("trash", t.danger, 16), "从 iPod 删除…",
                                lambda: self.delete_requested.emit(tracks))
        remove.setEnabled(all(track.managed for track in tracks))
        if not remove.isEnabled():
            remove.setText("从 iPod 删除…（只能删除 PodLink 导入的歌曲）")
        menu.exec(self.viewport().mapToGlobal(position))

    def copy(self, tracks) -> None:
        lines = ["\t".join((t.title, t.artist, t.album, self.window_.songs_model.display(t, "length")))
                 for t in tracks]
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))
        self.window_.flash("已复制 %d 首歌曲的信息" % len(tracks))

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            tracks = self.selected_tracks()
            if tracks:
                self.delete_requested.emit(tracks)
                return
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
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
        self.action = None
        if action:
            row = QtWidgets.QHBoxLayout()
            row.addStretch(1)
            self.action = button(action, "primary", slot=slot)
            row.addWidget(self.action)
            row.addStretch(1)
            layout.addSpacing(8)
            layout.addLayout(row)
        layout.addStretch(1)

    def apply_theme(self, tokens) -> None:
        self.glyph.setPixmap(icons.icon(self.icon_name, tokens.text_disabled, 56).pixmap(56, 56))


class SongsPage(Page):
    key = "songs"
    searchable = True

    def __init__(self, window, parent=None):
        super().__init__(window, parent)
        self.header = PageHeader("歌曲")
        self.refresh_button = self.header.add(button("读取 iPod", None, "refresh",
                                                     lambda: window.library_state.refresh(True)))
        self.refresh_button.setToolTip("从 iPod 重新读取整个资料库")
        self.info_button = self.header.add(button("显示简介", None, "info", self._info))
        self.layout_.addWidget(self.header)

        card, card_layout = make_card(0, expand=True, margins=(4, 4, 4, 4))
        self.stack = QtWidgets.QStackedWidget()
        self.table = SongsTable(window)
        self.table.info_requested.connect(window.show_info)
        self.table.delete_requested.connect(window.confirm_delete)
        self.empty = EmptyState("songs", "还没有资料库",
                                "用 USB 连接 iPod 并进入 PodLink 模式，然后读取资料库。"
                                "读取过的资料库会保存在电脑上，下次打开时直接显示。",
                                "读取 iPod 资料库", lambda: window.library_state.refresh(True))
        self.stack.addWidget(self.table)
        self.stack.addWidget(self.empty)
        card_layout.addWidget(self.stack)
        self.layout_.addWidget(card, 1)

        self.status = label("", "helper")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.layout_.addWidget(self.status)
        window.library_state.changed.connect(self.update_state)
        self.table.proxy.rowsInserted.connect(self.update_status)
        self.table.proxy.rowsRemoved.connect(self.update_status)
        self.table.proxy.modelReset.connect(self.update_status)
        self.table.proxy.layoutChanged.connect(self.update_status)
        self.update_state()

    def _info(self) -> None:
        tracks = self.table.selected_tracks()
        if tracks:
            self.window_.show_info(tracks)

    def update_state(self) -> None:
        state = self.window_.library_state
        self.stack.setCurrentWidget(self.table if state.tracks else self.empty)
        self.refresh_button.setEnabled(not state.loading)
        if state.loading:
            self.header.subtitle.setText("正在从 iPod 读取…")
        elif state.source == "device":
            self.header.subtitle.setText("来自已连接的 iPod")
        elif state.source == "cache":
            self.header.subtitle.setText("上次读取的资料库；iPod 连接后可重新读取")
        elif state.error:
            self.header.subtitle.setText(f"读取失败：{state.error}")
        else:
            self.header.subtitle.setText("iPod 上的全部歌曲")
        self.update_status()

    def update_status(self, *_args) -> None:
        self.status.setText(models.summary_line(self.table.proxy.tracks())
                            if self.window_.library_state.tracks else "")

    def set_search(self, text: str) -> None:
        self.table.proxy.set_search(text)
        self.update_status()

    def apply_theme(self, tokens) -> None:
        self.table.apply_theme(tokens)
        self.empty.apply_theme(tokens)
