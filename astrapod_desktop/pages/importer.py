"""Import: iTunes' "Add to Library" — review, then import to the iPod."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .. import fmt, icons
from ..backends import expand_paths, music_tags_for
from ..jobs import Cancelled
from ..qt import QtCore, QtGui, QtWidgets, Qt, Signal
from ..widgets import (DropFrame, PageHeader, ToggleSwitch, button, configure_table, draw_art,
                       label, qcolor)
from . import Page
from .sync import disk_mode_option

R = QtCore.QRectF
ITEM_ROLE = Qt.ItemDataRole.UserRole + 30


@dataclass
class ImportItem:
    path: Path
    probed: object = None
    state: str = "probing"   # probing ready duplicate error uploading done skipped failed cancelled
    include: bool = True
    progress: float = 0.0
    error: str = ""
    cover: QtGui.QImage | None = None

    @property
    def title(self) -> str:
        return (self.probed.title if self.probed and self.probed.title else self.path.stem)

    @property
    def artist(self) -> str:
        return self.probed.artist if self.probed else ""


STATE_TEXT = {
    "probing": "正在读取标签", "ready": "准备导入", "duplicate": "iPod 上已有",
    "error": "无法导入", "uploading": "正在上传", "done": "已导入",
    "skipped": "已跳过", "failed": "失败", "cancelled": "已取消", "queued": "等待上传",
}


class ImportModel(QtCore.QAbstractTableModel):
    HEADERS = ["", "歌曲", "专辑", "时间", "格式", "状态"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[ImportItem] = []
        self.rows: dict[str, int] = {}

    def add(self, paths) -> list[ImportItem]:
        fresh = [ImportItem(Path(p)) for p in paths if str(p) not in self.rows]
        if not fresh:
            return []
        start = len(self.items)
        self.beginInsertRows(QtCore.QModelIndex(), start, start + len(fresh) - 1)
        for offset, item in enumerate(fresh):
            self.items.append(item)
            self.rows[str(item.path)] = start + offset
        self.endInsertRows()
        return fresh

    def clear(self) -> None:
        self.beginResetModel()
        self.items.clear()
        self.rows.clear()
        self.endResetModel()

    def changed(self, item: ImportItem) -> None:
        row = self.rows.get(str(item.path))
        if row is not None:
            self.dataChanged.emit(self.index(row, 0), self.index(row, len(self.HEADERS) - 1))

    def item(self, path) -> ImportItem | None:
        row = self.rows.get(str(path))
        return self.items[row] if row is not None else None

    def rowCount(self, parent=QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QtCore.QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        return None

    def flags(self, index):
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        item = self.items[index.row()]
        if index.column() == 0 and item.state in ("ready", "duplicate"):
            return base | Qt.ItemFlag.ItemIsUserCheckable
        return base

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self.items[index.row()]
        column = index.column()
        probed = item.probed
        if role == ITEM_ROLE:
            return item
        if role == Qt.ItemDataRole.CheckStateRole and column == 0:
            if item.state not in ("ready", "duplicate"):
                return None
            return Qt.CheckState.Checked if item.include else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.DisplayRole:
            if column == 2:
                return probed.album if probed else ""
            if column == 3:
                return fmt.duration(probed.length_ms) if probed and probed.length_ms else ""
            if column == 4:
                if not probed or not probed.codec:
                    return item.path.suffix.lstrip(".").upper()
                rate = f"{probed.sample_rate / 1000:g} kHz" if probed.sample_rate else ""
                bits = f"{probed.bits} bit" if probed.bits else ""
                return " ".join(x for x in (probed.codec.upper(), rate, bits) if x)
        if role == Qt.ItemDataRole.ToolTipRole:
            return item.error or str(item.path)
        return None

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role == Qt.ItemDataRole.CheckStateRole and index.column() == 0:
            item = self.items[index.row()]
            item.include = Qt.CheckState(value) == Qt.CheckState.Checked
            self.changed(item)
            return True
        return False


class ImportDelegate(QtWidgets.QStyledItemDelegate):
    def __init__(self, page):
        super().__init__(page)
        self.page = page

    def paint(self, painter, option, index):
        column = index.column()
        if column not in (0, 1, 5):
            super().paint(painter, option, index)
            return
        # Background and selection only: the view reuses one option for every
        # cell, so its text would otherwise be the previous column's.
        background = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(background, index)
        background.text = ""
        background.features &= ~QtWidgets.QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        style = option.widget.style() if option.widget else QtWidgets.QApplication.style()
        style.drawControl(QtWidgets.QStyle.ControlElement.CE_ItemViewItem, background, painter,
                          option.widget)
        item = index.data(ITEM_ROLE)
        t = self.page.tokens
        if column == 0:
            self._paint_check(painter, R(option.rect), item, t)
            return
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = R(option.rect)
        font = QtGui.QFont(option.font)
        if column == 1:
            art = R(rect.left() + 6, rect.top() + 6, rect.height() - 12, rect.height() - 12)
            draw_art(painter, art, item.cover, item.path.stem, 5, 0.4,
                     loading=item.state == "probing", tokens=t)
            text = rect.adjusted(art.width() + 16, 6, -6, -6)
            font.setPixelSize(13)
            font.setWeight(QtGui.QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.setPen(QtGui.QColor(t.text_primary if item.include or item.state not in
                                        ("ready", "duplicate") else t.text_disabled))
            metrics = QtGui.QFontMetrics(font)
            painter.drawText(R(text.left(), text.top(), text.width(), text.height() / 2),
                             Qt.AlignmentFlag.AlignBottom,
                             metrics.elidedText(item.title, Qt.TextElideMode.ElideRight, int(text.width())))
            font.setWeight(QtGui.QFont.Weight.Normal)
            font.setPixelSize(12)
            painter.setFont(font)
            painter.setPen(QtGui.QColor(t.text_muted))
            metrics = QtGui.QFontMetrics(font)
            sub = item.artist or item.path.name
            painter.drawText(R(text.left(), text.center().y() + 1, text.width(), text.height() / 2),
                             Qt.AlignmentFlag.AlignTop,
                             metrics.elidedText(sub, Qt.TextElideMode.ElideRight, int(text.width())))
        else:
            self._paint_state(painter, rect, item, t, font)
        painter.restore()

    def _paint_check(self, painter, rect, item, t) -> None:
        if item.state not in ("ready", "duplicate"):
            return
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        box = R(rect.center().x() - 8, rect.center().y() - 8, 16, 16)
        if item.include:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(t.accent))
            painter.drawRoundedRect(box, 4, 4)
            icons.paint(painter, "check", box.adjusted(2, 2, -2, -2), QtGui.QColor(t.text_on_accent))
        else:
            painter.setPen(QtGui.QPen(qcolor(t.scrollbar_hover), 1.4))
            painter.setBrush(qcolor(t.glass_strong))
            painter.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if index.column() == 0 and event.type() == QtCore.QEvent.Type.MouseButtonRelease:
            item = index.data(ITEM_ROLE)
            if item.state in ("ready", "duplicate"):
                model.setData(index, Qt.CheckState.Unchecked.value if item.include
                              else Qt.CheckState.Checked.value, Qt.ItemDataRole.CheckStateRole)
                return True
        return super().editorEvent(event, model, option, index)

    def _paint_state(self, painter, rect, item, t, font) -> None:
        tone = {"duplicate": (t.warning, t.warning_bg), "error": (t.danger, t.danger_bg),
                "failed": (t.danger, t.danger_bg), "done": (t.success, t.success_bg),
                "uploading": (t.accent_text, t.accent_soft)}.get(
            item.state, (t.text_secondary, t.row_hover))
        x = rect.left() + 6
        font.setPixelSize(11)
        font.setWeight(QtGui.QFont.Weight.DemiBold)
        painter.setFont(font)
        metrics = QtGui.QFontMetrics(font)
        chips = [(STATE_TEXT.get(item.state, item.state), tone)]
        probed = item.probed
        if probed is not None and probed.converts and item.state in ("ready", "duplicate", "uploading"):
            chips.append((f"转为 {48 if probed.sample_rate % 44100 else 44.1:g} kHz",
                          (t.accent_text, t.accent_soft)))
        for text, (fg, bg) in chips:
            width = metrics.horizontalAdvance(text) + 14
            chip = R(x, rect.center().y() - 10, width, 20)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(bg))
            painter.drawRoundedRect(chip, 10, 10)
            painter.setPen(qcolor(fg))
            painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, text)
            x += width + 6
        if item.state == "uploading":
            bar = R(x + 4, rect.center().y() - 2, max(20.0, rect.right() - x - 14), 4)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(t.gauge_track))
            painter.drawRoundedRect(bar, 2, 2)
            painter.setBrush(QtGui.QColor(t.accent))
            painter.drawRoundedRect(R(bar.left(), bar.top(), bar.width() * item.progress, 4), 2, 2)


class ImportPage(Page):
    key = "import"

    def __init__(self, window, parent=None):
        super().__init__(window, parent)
        self.job = None
        self.duplicate_index: dict = {}
        self.header = PageHeader("直接导入", "把电脑上的 FLAC、M4A 和 MP3 加入 iPod")
        self.add_files_button = self.header.add(button("添加文件…", None, "plus", self.add_files))
        self.add_folder_button = self.header.add(button("添加文件夹…", None, "folder", self.add_folder))
        self.clear_button = self.header.add(button("清空列表", "ghost", None, self.clear))
        self.layout_.addWidget(self.header)

        self.drop = DropFrame()
        self.drop.setProperty("card", True)
        self.drop.files_dropped.connect(self.add_paths)
        drop_layout = QtWidgets.QVBoxLayout(self.drop)
        drop_layout.setContentsMargins(4, 4, 4, 4)
        self.stack = QtWidgets.QStackedWidget()
        self.target = DropTarget(self)
        self.target.clicked.connect(self.add_files)
        self.model = ImportModel(self)
        self.table = QtWidgets.QTableView()
        self.table.setModel(self.model)
        configure_table(self.table, 52)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.horizontalHeader().setSectionsMovable(False)
        self.table.setItemDelegate(ImportDelegate(self))
        for column, width in enumerate((34, 330, 190, 60, 150, 220)):
            self.table.setColumnWidth(column, width)
        self.table.setAccessibleName("待导入文件")
        self.stack.addWidget(self.target)
        self.stack.addWidget(self.table)
        drop_layout.addWidget(self.stack)
        self.layout_.addWidget(self.drop, 1)

        footer = QtWidgets.QFrame()
        footer.setObjectName("FooterBar")
        footer_layout = QtWidgets.QHBoxLayout(footer)
        footer_layout.setContentsMargins(18, 12, 18, 12)
        footer_layout.setSpacing(14)
        column = QtWidgets.QVBoxLayout()
        column.setSpacing(6)
        self.summary = label("还没有选择文件。", None, wrap=True)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        column.addWidget(self.summary)
        column.addWidget(self.progress)
        self.disk_mode = disk_mode_option(window, column)
        footer_layout.addLayout(column, 1)
        footer_layout.addWidget(label("跳过 iPod 上已有的歌曲", "field-label"))
        self.skip_check = ToggleSwitch()
        self.skip_check.setAccessibleName("跳过 iPod 上已有的歌曲")
        self.skip_check.setChecked(True)
        self.skip_check.toggled.connect(self._skip_toggled)
        footer_layout.addWidget(self.skip_check)
        self.cancel_button = button("取消", None, None, self.cancel)
        self.cancel_button.setVisible(False)
        footer_layout.addWidget(self.cancel_button)
        self.import_button = button("导入", "primary", "import", self.start)
        self.import_button.setMinimumWidth(120)
        footer_layout.addWidget(self.import_button)
        self.layout_.addWidget(footer)
        self.model.dataChanged.connect(lambda *_: self.update_summary())
        self._duplicate_target = None
        window.disk.changed.connect(self._disk_changed)
        window.disk.status.connect(self.summary.setText)
        window.library_state.changed.connect(self._rebuild_duplicates)
        self._rebuild_duplicates()
        self.update_summary()

    # Adding ---------------------------------------------------------
    def add_files(self) -> None:
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "添加到 iPod", str(Path.home() / "Music"),
            "音乐文件 (*.flac *.m4a *.mp3);;所有文件 (*)")
        if paths:
            self.add_paths(paths)

    def add_folder(self) -> None:
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "添加文件夹到 iPod",
                                                          str(Path.home() / "Music"))
        if path:
            self.add_paths([path])

    def add_paths(self, paths) -> None:
        if self.job is not None:
            self.window_.flash("正在导入，完成后再添加文件。")
            return
        files = expand_paths(paths)
        fresh = self.model.add(files)
        if not files:
            self.window_.flash("没有找到可以导入的 FLAC、M4A 或 MP3 文件。")
        self.stack.setCurrentWidget(self.table if self.model.items else self.target)
        self.update_summary()
        if fresh:
            self._probe(fresh)

    def clear(self) -> None:
        if self.job is None:
            self.model.clear()
            self.stack.setCurrentWidget(self.target)
            self.update_summary()

    def _probe(self, items) -> None:
        prober = self.window_.services.prober

        def work(job):
            with ThreadPoolExecutor(max_workers=4) as pool:
                for item, result in zip(items, pool.map(lambda i: prober.probe(i.path), items)):
                    job.progress(None, None, (item, result))
            return len(items)

        self.window_.runner.run("正在读取标签…", work, lambda _n: self.update_summary(),
                                lambda error: self.window_.show_error(error),
                                device=False, foreground=False, on_progress=self._probed)

    def _probed(self, _fraction, _text, data) -> None:
        item, result = data
        item.probed = result
        if result.cover is not None:
            image = QtGui.QImage(str(result.cover))
            item.cover = None if image.isNull() else image
        if result.error:
            item.state, item.error, item.include = "error", result.error, False
        elif self._is_duplicate(result):
            item.state = "duplicate"
            item.include = not self.skip_check.isChecked()
        else:
            item.state = "ready"
        self.model.changed(item)

    # Duplicates ------------------------------------------------------
    def _disk_changed(self):
        disk = self.window_.disk
        target = (disk.mode, disk.serial, disk.volume)
        if target != self._duplicate_target:
            self._duplicate_target = target
            self._rebuild_duplicates()
            self.update_summary()
        self._update_import_enabled()

    def _rebuild_duplicates(self) -> None:
        window = self.window_
        library, disk = window.library_state, window.disk
        tracks = library.tracks if disk.mode == "podlink" and disk.serial == library.serial else ()
        index: dict = {}
        for track in tracks:
            tags = music_tags_for(track.title, track.artist, track.album, track.length_ms,
                                  track.disc, track.track)
            if tags is not None:
                index.setdefault(tags.key, []).append(tags)
        self.duplicate_index = index
        for item in self.model.items:
            if item.state in ("ready", "duplicate") and item.probed is not None:
                duplicate = self._is_duplicate(item.probed)
                if duplicate:
                    item.include = not self.skip_check.isChecked()
                elif item.state == "duplicate" and self.skip_check.isChecked():
                    item.include = True
                item.state = "duplicate" if duplicate else "ready"
                self.model.changed(item)

    def _is_duplicate(self, probed) -> bool:
        tags = music_tags_for(probed.title, probed.artist, probed.album, probed.length_ms,
                              probed.disc, probed.track)
        if tags is None:
            return False
        return any(tags.matches(other) for other in self.duplicate_index.get(tags.key, ()))

    def _skip_toggled(self, skip: bool) -> None:
        for item in self.model.items:
            if item.state == "duplicate":
                item.include = not skip
                self.model.changed(item)

    # Summary --------------------------------------------------------
    def selected(self) -> list[ImportItem]:
        return [i for i in self.model.items if i.include and i.state in ("ready", "duplicate")]

    def update_summary(self) -> None:
        items = self.model.items
        if self.job is not None or self.window_.disk.transition:
            return
        if not items:
            self.summary.setText("还没有选择文件。也可以把文件拖到窗口里。")
            self.import_button.setEnabled(False)
            self.import_button.setText("导入")
            return
        probing = sum(1 for i in items if i.state == "probing")
        duplicates = sum(1 for i in items if i.state == "duplicate")
        errors = sum(1 for i in items if i.state == "error")
        converts = sum(1 for i in items if i.probed is not None and i.probed.converts
                       and i.state in ("ready", "duplicate") and i.include)
        chosen = self.selected()
        size = sum(i.probed.size_bytes for i in chosen if i.probed)
        parts = [f"{len(items)} 个文件"]
        if probing:
            parts.append(f"{probing} 个正在读取标签")
        if duplicates:
            parts.append(f"{duplicates} 首 iPod 上已有")
        if errors:
            parts.append(f"{errors} 个无法导入")
        if converts:
            parts.append(f"{converts} 首高采样率音频会先在电脑上转换为 48 kHz 以内")
        if self.skip_check.isChecked() and not self.duplicate_index:
            parts.append("导入时会读取 iPod 资料库查重")
        self.summary.setText("，".join(parts) + "。")
        self.import_button.setText(f"导入 {len(chosen)} 首（{fmt.size(size)}）" if chosen else "导入")
        self._update_import_enabled()

    def _update_import_enabled(self):
        disk = self.window_.disk
        self.import_button.setEnabled(
            self.job is None and disk.available and not disk.transition
            and bool(self.selected()) and not any(i.state == "probing" for i in self.model.items))

    # Running --------------------------------------------------------
    def start(self) -> None:
        chosen = self.selected()
        disk = self.window_.disk
        if not chosen or self.job is not None or disk.transition or not disk.available:
            return
        for item in chosen:
            item.state, item.progress = "queued", 0.0
            self.model.changed(item)
        skip = self.skip_check.isChecked()
        volume, player = disk.volume, disk.player
        cached_library = self.window_.library_state.snapshot()
        library = self.window_.library_state.library
        paths = [item.path for item in chosen]
        counts = {"done": 0, "skip": 0, "error": 0}

        def work(job):
            def progress(event):
                job.progress(None, None, event)
                if event.get("state") == "probe":
                    job.check()

            try:
                job.check()
                if volume is not None:
                    library.import_disk(paths, progress, skip_duplicates=skip, volume=volume,
                                        on_write=lambda: disk.written.emit(player))
                else:
                    with library.session() as session:
                        if hasattr(session, "import_files"):
                            session.import_files(paths, progress, skip_duplicates=skip,
                                                 cached_library=cached_library)
                        else:
                            upload_each(session, paths, progress)
            except Cancelled:
                return {"cancelled": True}
            return {"cancelled": False}

        def on_progress(_fraction, _text, event):
            item = self.model.item(event.get("file"))
            if item is None:
                return
            state = event.get("state")
            if state == "probe":
                item.state = "uploading"
            elif state == "skip":
                item.state = "skipped"
                counts["skip"] += 1
            elif state == "upload":
                item.state = "uploading"
                total = event.get("total") or 0
                item.progress = (event.get("sent") or 0) / total if total else 0.0
            elif state == "done":
                item.state, item.progress = "done", 1.0
                counts["done"] += 1
            elif state == "error":
                item.state, item.error = "failed", str(event.get("error") or "")
                counts["error"] += 1
            self.model.changed(item)
            finished = counts["done"] + counts["skip"] + counts["error"]
            fraction = (finished + (item.progress if item.state == "uploading" else 0)) / len(paths)
            self.progress.setValue(int(fraction * 1000))
            self.window_.lcd.set_activity(f"正在导入「{item.title}」（{finished + 1}/{len(paths)}）",
                                          fraction)
            self.table.scrollTo(self.model.index(self.model.rows[str(item.path)], 0))

        def done(result):
            self._finish(result.get("cancelled"), counts, volume is not None, None)

        def failed(error):
            self._finish(False, counts, volume is not None, error)

        self.job = self.window_.runner.run("正在导入到 iPod…", work, done, failed,
                                           on_progress=on_progress)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.cancel_button.setVisible(True)
        self.cancel_button.setEnabled(True)
        self.import_button.setEnabled(False)
        for widget in (self.add_files_button, self.add_folder_button, self.clear_button,
                       self.skip_check):
            widget.setEnabled(False)
        self.summary.setText(f"正在导入 {len(paths)} 首。可以在两首之间取消。")

    def cancel(self) -> None:
        if self.job is not None:
            self.job.cancel()
            self.cancel_button.setEnabled(False)
            self.summary.setText("当前这首上传完后停止…")

    def _finish(self, cancelled: bool, counts: dict, disk: bool, error) -> None:
        self.job = None
        for item in self.model.items:
            if item.state in ("queued", "uploading"):
                item.state = "cancelled" if cancelled or error is None else "failed"
                self.model.changed(item)
        self.progress.setVisible(False)
        self.cancel_button.setVisible(False)
        for widget in (self.add_files_button, self.add_folder_button, self.clear_button,
                       self.skip_check):
            widget.setEnabled(True)
        text = f"已导入 {counts['done']} 首"
        if counts["skip"]:
            text += f"，跳过 {counts['skip']} 首重复"
        if counts["error"]:
            text += f"，{counts['error']} 首失败"
        if cancelled:
            text += "，其余已取消"
        text += "。"
        if counts["done"]:
            text += ("关闭磁盘模式后，iPod 会更新资料库。" if disk else
                     "iPod 正在更新资料库，稍后重新读取即可看到新歌曲。")
        if error is not None and not isinstance(error, Cancelled):
            text = f"导入中断：{error}。{text}"
            self.window_.show_error(error)
        self.update_summary()
        self.summary.setText(text)
        self.window_.flash(text)

    def apply_theme(self, tokens) -> None:
        self.skip_check.tokens = tokens
        self.disk_mode.tokens = tokens
        self.disk_mode.update()
        self.target.update()


def upload_each(session, paths, progress) -> None:
    """MS1 path for an hrlibrary without import_files: one file at a time.
    Like import_files, it refreshes the iPod's library even when cancelled."""
    import tempfile
    import podlink
    uploaded = 0
    try:
        with tempfile.TemporaryDirectory(prefix="astrapod-import-") as tmp:
            for number, path in enumerate(paths):
                event = {"file": str(path), "sent": 0, "total": 0, "error": None}
                progress({**event, "state": "probe"})
                try:
                    folder = Path(tmp) / str(number)
                    folder.mkdir()
                    prepared = podlink.prepare_media([Path(path)], folder)[0]
                    size = prepared.upload.stat().st_size
                    progress({**event, "state": "upload", "total": size})
                    podlink.upload_media_track(session.link, prepared)
                    uploaded += 1
                    progress({**event, "state": "done", "sent": size, "total": size})
                except (OSError, RuntimeError, ValueError, TimeoutError) as error:
                    progress({**event, "state": "error", "error": str(error)})
    finally:
        if uploaded:
            podlink.refresh_media(session.link)


class DropTarget(QtWidgets.QWidget):
    clicked = Signal()

    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("把音乐拖到这里，或按回车选择文件")

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:
        t = self.page.tokens
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = R(self.rect()).adjusted(18, 18, -18, -18)
        hovering = self.page.drop.hovering
        pen = QtGui.QPen(QtGui.QColor(t.accent) if hovering or self.hasFocus()
                         else qcolor(t.scrollbar_handle), 1.6, Qt.PenStyle.DashLine)
        pen.setDashPattern([5, 4])
        painter.setPen(pen)
        painter.setBrush(qcolor(t.row_selected) if hovering else Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, 16, 16)
        center = rect.center()
        glyph = R(center.x() - 30, center.y() - 70, 60, 60)
        icons.paint(painter, "import", glyph, QtGui.QColor(t.accent) if hovering else qcolor(t.text_muted))
        font = QtGui.QFont(self.font())
        font.setPixelSize(22)
        font.setWeight(QtGui.QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(t.text_primary))
        painter.drawText(R(rect.left(), center.y() + 2, rect.width(), 30), Qt.AlignmentFlag.AlignCenter,
                         "松开即可添加" if hovering else "把音乐拖到这里")
        font = QtGui.QFont(self.font())
        font.setPixelSize(13)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(t.text_muted))
        painter.drawText(R(rect.left(), center.y() + 36, rect.width(), 40), Qt.AlignmentFlag.AlignCenter,
                         "文件或整个文件夹都可以，导入前会先列出每首歌让你确认。\n"
                         "支持 FLAC、M4A 和 MP3；高于 48 kHz 的音频会先在电脑上转换。")
        painter.end()
