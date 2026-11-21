"""Get Info: a read-only sheet with iTunes' tabs and copy buttons."""
from __future__ import annotations

from shiboken6 import isValid as shiboken_valid

from .. import fmt, icons
from ..artwork import NO_ART
from ..qt import QtCore, QtGui, QtWidgets, Qt
from ..widgets import ArtLabel, button, label

PENDING = "正在从 iPod 读取…"


def sort_name(text: str) -> str:
    stripped = (text or "").strip()
    for prefix in ("The ", "A ", "An "):
        if stripped.startswith(prefix):
            return stripped[len(prefix):]
    return stripped


class InfoDialog(QtWidgets.QDialog):
    def __init__(self, window, tracks: list, parent=None):
        super().__init__(parent or window)
        self.window_ = window
        self.tracks = list(tracks)
        self.position = 0
        self.fields: dict[str, QtWidgets.QLineEdit] = {}
        self.setWindowTitle("简介")
        self.setModal(False)
        self.resize(660, 580)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(12)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(14)
        self.thumb = ArtLabel(76, radius=8, shadow=0.6)
        head.addWidget(self.thumb)
        column = QtWidgets.QVBoxLayout()
        column.setSpacing(2)
        self.title = label("", "hero-title")
        self.subtitle = label("", "page-subtitle")
        self.counter = label("", "helper")
        column.addStretch(1)
        column.addWidget(self.title)
        column.addWidget(self.subtitle)
        column.addWidget(self.counter)
        column.addStretch(1)
        head.addLayout(column, 1)
        self.prev_button = button("", "ghost", "back", lambda: self.step(-1))
        self.prev_button.setToolTip("上一首")
        self.prev_button.setAccessibleName("上一首")
        self.next_button = button("", "ghost", None, lambda: self.step(1))
        self.next_button.setToolTip("下一首")
        self.next_button.setAccessibleName("下一首")
        head.addWidget(self.prev_button, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(self.next_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(head)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._summary_tab(), "摘要")
        self.tabs.addTab(self._form_tab([
            ("title", "名称"), ("artist", "艺人"), ("album_artist", "专辑艺人"),
            ("album", "专辑"), ("composer", "作曲者"), ("grouping", "归类"),
            ("genre", "类型"), ("year", "年份"), ("track_no", "音轨"),
            ("disc_no", "光盘"), ("rating", "评分"), ("comment", "注释")]), "详细信息")
        self.tabs.addTab(self._art_tab(), "插图")
        self.tabs.addTab(self._form_tab([
            ("sort_title", "排序名称"), ("sort_artist", "排序艺人"),
            ("sort_album_artist", "排序专辑艺人"), ("sort_album", "排序专辑"),
            ("sort_composer", "排序作曲者"), ("play_count", "播放次数"),
            ("play_time", "累计播放时长"), ("last_played", "上次播放"),
            ("managed", "可在电脑上删除")]), "排序与选项")
        self.tabs.addTab(self._form_tab([
            ("path", "位置"), ("folder", "文件夹"), ("filename", "文件名"),
            ("kind_file", "种类"), ("codec", "编码"), ("size_file", "大小"),
            ("mtime", "修改日期"), ("index", "数据库编号")]), "文件")
        layout.addWidget(self.tabs, 1)

        footer = QtWidgets.QHBoxLayout()
        self.copy_all = button("复制全部信息", None, "copy", self.copy_everything)
        footer.addWidget(self.copy_all)
        footer.addStretch(1)
        close = button("完成", "primary", None, self.accept)
        close.setDefault(True)
        footer.addWidget(close)
        layout.addLayout(footer)
        window.art.art_ready.connect(self._art_ready)
        self._paint_icons()
        self.show_track()

    # Layout helpers ---------------------------------------------------
    def _row(self, grid: QtWidgets.QGridLayout, row: int, key: str, caption: str) -> None:
        name = label(caption, "field-label")
        name.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        value = QtWidgets.QLineEdit()
        value.setReadOnly(True)
        value.setProperty("readonlyField", True)
        value.setAccessibleName(caption)
        copy = QtWidgets.QToolButton()
        copy.setToolTip(f"复制{caption}")
        copy.setAccessibleName(f"复制{caption}")
        copy.setProperty("icon_name", "copy")
        copy.setCursor(Qt.CursorShape.PointingHandCursor)
        copy.clicked.connect(lambda _c=False, f=value, n=caption: self.copy_value(n, f.text()))
        grid.addWidget(name, row, 0)
        grid.addWidget(value, row, 1)
        grid.addWidget(copy, row, 2)
        self.fields[key] = value

    def _form_tab(self, rows) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(page)
        grid.setContentsMargins(4, 14, 4, 4)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)
        grid.setColumnMinimumWidth(0, 104)
        for row, (key, caption) in enumerate(rows):
            self._row(grid, row, key, caption)
        grid.setRowStretch(len(rows), 1)
        return page

    def _summary_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(page)
        layout.setContentsMargins(4, 14, 4, 4)
        layout.setSpacing(18)
        self.big_art = ArtLabel(196, radius=10, shadow=1.2)
        layout.addWidget(self.big_art, 0, Qt.AlignmentFlag.AlignTop)
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)
        grid.setColumnStretch(1, 1)
        rows = [("kind", "种类"), ("size", "大小"), ("bitrate", "比特率"),
                ("sample_rate", "采样率"), ("channels", "声道"), ("length", "时间"),
                ("modified", "修改日期"), ("plays", "播放次数"), ("artwork", "插图"),
                ("where", "位置")]
        for row, (key, caption) in enumerate(rows):
            self._row(grid, row, key, caption)
        grid.setRowStretch(len(rows), 1)
        layout.addLayout(grid, 1)
        return page

    def _art_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(4, 14, 4, 4)
        self.art_large = ArtLabel(300, radius=12, shadow=1.3)
        layout.addWidget(self.art_large, 0, Qt.AlignmentFlag.AlignHCenter)
        self.art_note = label("", "helper", wrap=True)
        self.art_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.art_note)
        layout.addStretch(1)
        return page

    def _paint_icons(self) -> None:
        t = self.window_.theme_manager.tokens
        for widget in self.findChildren(QtWidgets.QToolButton):
            if widget.property("icon_name"):
                widget.setIcon(icons.icon(widget.property("icon_name"), t.text_muted, 14))
        self.prev_button.setIcon(icons.icon("back", t.text_secondary, 16))
        flipped = icons.icon("back", t.text_secondary, 16).pixmap(32, 32).transformed(
            QtGui.QTransform().scale(-1, 1))
        self.next_button.setIcon(QtGui.QIcon(flipped))
        self.copy_all.setIcon(icons.icon("copy", t.text_secondary, 16))
        for art in (self.thumb, self.big_art, self.art_large):
            art.tokens = t

    # Content ----------------------------------------------------------
    @property
    def track(self):
        return self.tracks[self.position]

    def step(self, delta: int) -> None:
        position = self.position + delta
        if 0 <= position < len(self.tracks):
            self.position = position
            self.show_track()

    def show_track(self) -> None:
        t = self.track
        model = self.window_.songs_model
        self.title.setText(t.title or t.path.rsplit("/", 1)[-1])
        self.subtitle.setText(" — ".join(x for x in (t.artist, t.album) if x))
        many = len(self.tracks) > 1
        self.counter.setText(f"第 {self.position + 1} 首，共 {len(self.tracks)} 首" if many else "")
        self.prev_button.setVisible(many)
        self.next_button.setVisible(many)
        self.prev_button.setEnabled(self.position > 0)
        self.next_button.setEnabled(self.position < len(self.tracks) - 1)
        folder, _, filename = t.path.rpartition("/")
        stars = t.rating / 2
        values = {
            "title": t.title, "artist": t.artist, "album_artist": t.album_artist,
            "album": t.album, "composer": t.composer, "grouping": t.grouping,
            "genre": t.genre, "year": str(t.year or ""),
            "track_no": str(t.track or ""), "disc_no": str(t.disc or ""),
            "rating": (f"{stars:g} 星（Rockbox {t.rating}/10）" if t.rating else "未评分"),
            "comment": t.comment,
            "sort_title": sort_name(t.title), "sort_artist": sort_name(t.artist),
            "sort_album_artist": sort_name(t.album_artist), "sort_album": sort_name(t.album),
            "sort_composer": sort_name(t.composer),
            "play_count": str(t.play_count), "play_time": fmt.long_duration(t.play_time_ms),
            "last_played": model.display(t, "last_played") or "从未播放",
            "managed": "是，PodLink 导入" if t.managed else "否，只能在 iPod 上管理",
            "path": t.path, "folder": folder or "/", "filename": filename,
            "mtime": fmt.date(t.mtime) or "未知", "index": str(t.index),
            "kind": fmt.kind(t.path), "kind_file": fmt.kind(t.path),
            "bitrate": f"{t.bitrate_kbps} kbps" if t.bitrate_kbps else "未知",
            "length": fmt.duration(t.length_ms), "modified": fmt.date(t.mtime) or "未知",
            "plays": str(t.play_count), "where": t.path,
        }
        for key, value in values.items():
            self._set(key, value)
        for key in ("size", "sample_rate", "channels", "artwork", "codec", "size_file"):
            self._set(key, PENDING)
        self.art_note.setText(PENDING)
        self._show_art()
        self.window_.show_track_art(t)
        self.window_.track_detail(t, self._detail)

    def _set(self, key: str, value: str) -> None:
        field = self.fields.get(key)
        if field is not None:
            field.setText(value or "")
            field.setCursorPosition(0)

    def _detail(self, track, detail, error) -> None:
        if track is not self.track or not shiboken_valid(self):
            return
        if error is not None:
            for key in ("size", "sample_rate", "channels", "artwork", "codec", "size_file"):
                self._set(key, "无法读取")
            self.art_note.setText(f"无法从 iPod 读取文件信息：{error}")
            return
        self._set("size", fmt.size(detail.size_bytes))
        self._set("size_file", f"{fmt.size(detail.size_bytes)}（{detail.size_bytes:,} 字节）")
        self._set("sample_rate", f"{detail.sample_rate / 1000:g} kHz" if detail.sample_rate else "未知")
        self._set("channels", {1: "单声道", 2: "立体声"}.get(detail.channels, f"{detail.channels} 声道"))
        self._set("codec", detail.codec.upper() if detail.codec else "未知")
        self._set("kind", fmt.kind(track.path, detail.codec))
        self._set("kind_file", fmt.kind(track.path, detail.codec))
        self._set("artwork", "有插图" if detail.has_art else "没有插图")
        if not detail.has_art:
            self.art_note.setText("这首歌没有内嵌插图，专辑文件夹里也没有封面图片。")

    def _show_art(self) -> None:
        art = self.window_.art
        large = art.get(self.track, "wps")
        small = art.get(self.track, "coverflow")
        best = large if isinstance(large, QtGui.QImage) else small
        image = best if isinstance(best, QtGui.QImage) else None
        seed = self.track.album
        for widget in (self.thumb, self.big_art, self.art_large):
            widget.set_image(image, seed)
        if image is not None:
            self.art_note.setText(f"iPod 插图缓存，{image.width()} × {image.height()} 像素")
        elif large is NO_ART and small is NO_ART:
            self.art_note.setText("iPod 的插图缓存里没有这张专辑的封面。")

    def _art_ready(self, key) -> None:
        from ..contracts import album_key
        if key == album_key(self.track):
            self._show_art()

    # Clipboard ---------------------------------------------------------
    def copy_value(self, caption: str, text: str) -> None:
        QtWidgets.QApplication.clipboard().setText(text)
        self.window_.flash(f"已复制{caption}")

    def all_text(self) -> str:
        lines = []
        for index in range(self.tabs.count()):
            page = self.tabs.widget(index)
            for grid in page.findChildren(QtWidgets.QGridLayout):
                for row in range(grid.rowCount()):
                    name = grid.itemAtPosition(row, 0)
                    value = grid.itemAtPosition(row, 1)
                    if name and value:
                        lines.append(f"{name.widget().text()}：{value.widget().text()}")
        seen, unique = set(), []
        for line in lines:
            if line not in seen:
                seen.add(line)
                unique.append(line)
        return "\n".join(unique)

    def copy_everything(self) -> None:
        QtWidgets.QApplication.clipboard().setText(self.all_text())
        self.window_.flash("已复制全部信息")
