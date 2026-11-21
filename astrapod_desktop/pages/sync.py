"""Jellyfin sync: log in, choose a scope, preview the difference, apply.
A successful sign-in is kept across restarts until the owner signs out, and
the 资料库 pages browse Jellyfin through it. The iPod is the one the device
page's serial port reaches."""
from __future__ import annotations

import os

from ..qt import QtCore, QtGui, QtWidgets, Qt, Signal
from ..widgets import (ChevronComboBox, PageHeader, PasswordLineEdit, SegmentedControl, ToggleSwitch,
                       button, label, make_badge, make_card, qcolor, set_badge)
from . import Page

R = QtCore.QRectF
KIND_ROLE = Qt.ItemDataRole.UserRole + 40
SCOPE_PREFIXES = {"all": "", "library": "音乐库：", "playlist": "播放列表："}


def disk_mode_option(window, layout, key, action):
    row = QtWidgets.QHBoxLayout()
    column = QtWidgets.QVBoxLayout()
    column.setSpacing(2)
    column.addWidget(label("磁盘模式", "field-label"))
    hint = f"传输更快。{action}期间，iPod 显示 USB 画面。"
    column.addWidget(label(hint, "helper", wrap=True))
    row.addLayout(column, 1)
    switch = ToggleSwitch()
    switch.setAccessibleName("磁盘模式")
    switch.setAccessibleDescription(hint)
    switch.setChecked(window.prefs.get(key) is True)
    switch.toggled.connect(lambda checked: window.save_pref(key, checked))
    row.addWidget(switch)
    layout.addLayout(row)
    return switch


class DiffDelegate(QtWidgets.QStyledItemDelegate):
    def __init__(self, page):
        super().__init__(page)
        self.page = page

    def sizeHint(self, option, _index):
        return QtCore.QSize(option.rect.width(), 30)

    def paint(self, painter, option, index):
        t = self.page.tokens
        kind = index.data(KIND_ROLE)
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = R(option.rect).adjusted(4, 1, -4, -1)
        if option.state & QtWidgets.QStyle.StateFlag.State_Selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(t.row_selected))
            painter.drawRoundedRect(rect, 7, 7)
        color = {"add": t.success, "remove": t.danger}.get(kind, t.text_muted)
        mark = R(rect.left() + 8, rect.center().y() - 9, 18, 18)
        painter.setPen(Qt.PenStyle.NoPen)
        tint = QtGui.QColor(color)
        tint.setAlphaF(0.16)
        painter.setBrush(tint)
        painter.drawEllipse(mark)
        painter.setPen(QtGui.QPen(QtGui.QColor(color), 1.8))
        c = mark.center()
        painter.drawLine(QtCore.QPointF(c.x() - 4, c.y()), QtCore.QPointF(c.x() + 4, c.y()))
        if kind == "add":
            painter.drawLine(QtCore.QPointF(c.x(), c.y() - 4), QtCore.QPointF(c.x(), c.y() + 4))
        elif kind == "same":
            painter.drawLine(QtCore.QPointF(c.x() - 4, c.y() + 3), QtCore.QPointF(c.x() + 4, c.y() + 3))
            painter.drawLine(QtCore.QPointF(c.x() - 4, c.y() - 3), QtCore.QPointF(c.x() + 4, c.y() - 3))
        font = QtGui.QFont(option.font)
        font.setPixelSize(13)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(t.text_primary if kind != "same" else t.text_secondary))
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        metrics = QtGui.QFontMetrics(font)
        painter.drawText(rect.adjusted(36, 0, -110, 0), Qt.AlignmentFlag.AlignVCenter,
                         metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(rect.width() - 150)))
        font.setPixelSize(11)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(t.text_muted))
        note = {"add": "将写入 iPod", "remove": "将从 iPod 删除", "same": "iPod 已有"}.get(kind, "")
        painter.drawText(rect.adjusted(0, 0, -10, 0),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, note)
        painter.restore()


class StatTile(QtWidgets.QFrame):
    def __init__(self, caption: str, parent=None):
        super().__init__(parent)
        self.setProperty("card", True)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(0)
        self.value = label("0", "stat-value")
        self.caption = label(caption, "stat-label")
        layout.addWidget(self.value)
        layout.addWidget(self.caption)


class SyncPage(Page):
    key = "sync"
    source_state = Signal()         # session, restoring or source changed

    def __init__(self, window, parent=None):
        super().__init__(window, parent)
        self.backend = window.services.sync
        self.source = None
        self.plan = None
        self.scopes: list = []
        self.busy = False
        self.restoring = False
        self.session = None
        self.auth_mode = "password"
        self.password_inputs: list[PasswordLineEdit] = []
        settings = window.settings

        # Everything above the footer scrolls, so a short window never
        # squeezes the cards.
        self.layout_.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(host)
        body.setContentsMargins(28, 18, 28, 0)
        body.setSpacing(14)
        self.header = PageHeader("Jellyfin 同步", "把 Jellyfin 的曲库或播放列表单向同步到 iPod")
        body.addWidget(self.header)

        top = QtWidgets.QHBoxLayout()
        top.setSpacing(14)
        conn, conn_layout = make_card(10)
        head = QtWidgets.QHBoxLayout()
        head.addWidget(label("服务器", "card-title"))
        self.connection_badge = make_badge("未连接")
        head.addWidget(self.connection_badge)
        head.addStretch(1)
        self.auth_switch = SegmentedControl()
        self.auth_switch.add_option("password", "账号密码")
        self.auth_switch.add_option("token", "访问令牌")
        self.auth_switch.set_current("password")
        self.auth_switch.changed.connect(self._auth_changed)
        head.addWidget(self.auth_switch)
        conn_layout.addLayout(head)
        self.url = QtWidgets.QLineEdit()
        self.url.setPlaceholderText("https://jellyfin.example.com")
        self.url.setText(os.environ.get("JELLYFIN_URL") or settings.get("JELLYFIN_URL", ""))
        self.url.setAccessibleName("Jellyfin 地址")
        conn_layout.addWidget(self.url)
        self.auth_stack = QtWidgets.QStackedWidget()
        self.auth_stack.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                                      QtWidgets.QSizePolicy.Policy.Fixed)
        pwd = QtWidgets.QWidget()
        pwd_row = QtWidgets.QHBoxLayout(pwd)
        pwd_row.setContentsMargins(0, 0, 0, 0)
        pwd_row.setSpacing(10)
        self.username = QtWidgets.QLineEdit()
        self.username.setPlaceholderText("用户名")
        self.username.setAccessibleName("Jellyfin 用户名")
        self.password = PasswordLineEdit("密码（提交后立即清空）")
        self.password.setAccessibleName("Jellyfin 密码")
        self.password_inputs.append(self.password)
        pwd_row.addWidget(self.username, 1)
        pwd_row.addWidget(self.password, 1)
        self.auth_stack.addWidget(pwd)
        tok = QtWidgets.QWidget()
        tok_row = QtWidgets.QHBoxLayout(tok)
        tok_row.setContentsMargins(0, 0, 0, 0)
        self.token = PasswordLineEdit("用户访问令牌（留空读取已有设置）")
        self.token.setAccessibleName("Jellyfin 访问令牌")
        self.password_inputs.append(self.token)
        tok_row.addWidget(self.token)
        self.auth_stack.addWidget(tok)
        conn_layout.addWidget(self.auth_stack)
        self.account = QtWidgets.QWidget()
        account_row = QtWidgets.QHBoxLayout(self.account)
        account_row.setContentsMargins(0, 0, 0, 0)
        self.account_label = label("", "field-label", wrap=True)
        account_row.addWidget(self.account_label, 1)
        self.sign_out_button = button("退出登录", None, None, self.sign_out)
        account_row.addWidget(self.sign_out_button)
        self.account.setVisible(False)
        conn_layout.addWidget(self.account)
        bottom = QtWidgets.QHBoxLayout()
        self.auth_hint = label("", "helper", wrap=True)
        bottom.addWidget(self.auth_hint, 1)
        self.connect_button = button("连接", "primary", None, self.connect_source)
        bottom.addWidget(self.connect_button)
        conn_layout.addLayout(bottom)
        top.addWidget(conn, 3)

        scope, scope_layout = make_card(10)
        scope_layout.addWidget(label("范围", "card-title"))
        self.scope = ChevronComboBox()
        self.scope.setPlaceholderText("先连接 Jellyfin")
        self.scope.setAccessibleName("同步范围")
        self.scope.currentIndexChanged.connect(self.invalidate_plan)
        self.scope.currentIndexChanged.connect(self._remember_scope)
        scope_layout.addWidget(self.scope)
        port_row = QtWidgets.QHBoxLayout()
        self.port_label = label("", "helper", wrap=True)
        port_row.addWidget(self.port_label, 1)
        self.port_button = button("更改", "ghost", None, lambda: window.navigate("settings"))
        self.port_button.setToolTip("在设置中更改 iPod 串口")
        port_row.addWidget(self.port_button)
        scope_layout.addLayout(port_row)
        self._show_port()
        window.port_changed.connect(self._port_changed)
        self.disk_mode = disk_mode_option(window, scope_layout, "sync_disk_mode", "同步")
        scope_layout.addStretch(1)
        scope_row = QtWidgets.QHBoxLayout()
        scope_row.addWidget(label("预览时，iPod 必须处于 PodLink USB 模式", "helper", wrap=True), 1)
        self.preview_button = button("预览", None, "sync", self.preview)
        scope_row.addWidget(self.preview_button)
        scope_layout.addLayout(scope_row)
        for card in (conn, scope):
            card.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                               QtWidgets.QSizePolicy.Policy.Preferred)
        top.addWidget(scope, 2)
        body.addLayout(top)

        tiles = QtWidgets.QHBoxLayout()
        tiles.setSpacing(14)
        self.tile_add = StatTile("将写入 iPod")
        self.tile_same = StatTile("iPod 已有，不重复写入")
        self.tile_remove = StatTile("将从 iPod 删除")
        for tile in (self.tile_add, self.tile_same, self.tile_remove):
            tiles.addWidget(tile)
        body.addLayout(tiles)

        preview, preview_layout = make_card(8, expand=True, margins=(8, 10, 8, 8))
        filter_row = QtWidgets.QHBoxLayout()
        filter_row.setContentsMargins(8, 0, 8, 0)
        filter_row.addWidget(label("预览", "card-title"))
        filter_row.addStretch(1)
        self.filter = SegmentedControl()
        for key, text in (("all", "全部"), ("add", "写入"), ("same", "已有"), ("remove", "删除")):
            self.filter.add_option(key, text)
        self.filter.set_current("all")
        self.filter.changed.connect(lambda _k: self._fill_list())
        filter_row.addWidget(self.filter)
        preview_layout.addLayout(filter_row)
        self.list_model = QtGui.QStandardItemModel(self)
        self.details = QtWidgets.QListView()
        self.details.setModel(self.list_model)
        self.details.setItemDelegate(DiffDelegate(self))
        self.details.setUniformItemSizes(True)
        self.details.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.details.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.details.setAccessibleName("同步预览")
        self.placeholder = label("连接 Jellyfin、选好范围后点「预览」，这里会列出要写入和删除的歌曲。",
                                 "helper", wrap=True)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_stack = QtWidgets.QStackedWidget()
        self.preview_stack.addWidget(self.placeholder)
        self.preview_stack.addWidget(self.details)
        preview_layout.addWidget(self.preview_stack, 1)
        self.preview_stack.setMinimumHeight(120)
        body.addWidget(preview, 1)
        scroll.setWidget(host)
        self.layout_.addWidget(scroll, 1)

        footer = QtWidgets.QFrame()
        footer.setObjectName("FooterBar")
        footer_layout = QtWidgets.QHBoxLayout(footer)
        footer_layout.setContentsMargins(18, 12, 18, 12)
        column = QtWidgets.QVBoxLayout()
        column.setSpacing(6)
        self.status = label("连接 iPod 后预览同步变更。", None, wrap=True)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        column.addWidget(self.status)
        column.addWidget(self.progress)
        footer_layout.addLayout(column, 1)
        self.apply_button = button("应用同步", "primary", None, self.confirm)
        self.apply_button.setMinimumWidth(116)
        footer_layout.addWidget(self.apply_button)
        footer_wrap = QtWidgets.QWidget()
        wrap = QtWidgets.QVBoxLayout(footer_wrap)
        wrap.setContentsMargins(28, 0, 28, 20)
        wrap.addWidget(footer)
        self.layout_.addWidget(footer_wrap)

        for field in (self.url, self.username, self.password, self.token):
            field.textChanged.connect(self.invalidate_source)
            field.returnPressed.connect(self._connect_return)
        self._auth_hint()
        self._update_metrics(None)
        try:
            saved = self.backend.load_session()
        except (OSError, ValueError):
            saved = None
        if saved and saved.get("url") and saved.get("token"):
            self._use_session(saved)
            self.restore_session()
        else:
            self._show_account()
            self.update_buttons()

    # Auth -----------------------------------------------------------
    def _auth_changed(self, mode: str) -> None:
        if mode in ("password", "token"):
            self.auth_mode = mode
            self.auth_stack.setCurrentIndex(0 if mode == "password" else 1)
            self._auth_hint()
            self.invalidate_source()

    def _auth_hint(self) -> None:
        if self.session is not None:
            self.auth_hint.setText("登录会保留到退出登录为止。AstraPod 只保存访问令牌，不保存密码。")
            return
        self.auth_hint.setText(
            "只支持 HTTPS。密码只用来登录，不保存也不写入日志；登录后保留访问令牌。"
            if self.auth_mode == "password"
            else "只支持 HTTPS。连接成功后保留这个令牌，直到退出登录；留空则读取已有设置。")

    # Saved session ------------------------------------------------------
    def _use_session(self, record: dict) -> None:
        """Show this saved sign-in; the URL field follows it."""
        self.session = dict(record)
        blocked = self.url.blockSignals(True)
        self.url.setText(record.get("url", ""))
        self.url.blockSignals(blocked)
        self._show_account()

    def _show_account(self) -> None:
        signed_in = self.session is not None
        self.account.setVisible(signed_in)
        self.auth_stack.setVisible(not signed_in)
        self.auth_switch.setVisible(not signed_in)
        if signed_in:
            name = self.session.get("user_name") or "Jellyfin 用户"
            self.account_label.setText(f"已登录为 {name}")
        self.connect_button.setText("重新连接" if signed_in else "连接")
        self._auth_hint()

    def restore_session(self) -> None:
        """Reconnect with the saved token in the background."""
        if self.session is None or self.restoring or self.busy:
            return
        record = dict(self.session)
        self.restoring = True
        self.status.setText("正在用保存的登录连接 Jellyfin…")
        self.update_buttons()
        self.window_.runner.run("正在连接 Jellyfin…", lambda _job: self.backend.restore(record),
                                lambda result: self._restored(record, result),
                                lambda error: self._restore_failed(record, error),
                                device=False, foreground=False)

    def _restored(self, record, result) -> None:
        self.restoring = False
        if self.session != record:
            self.update_buttons()
            return
        self.connected(result)
        wanted = record.get("source") or {}
        for index, scope in enumerate(self.scopes):
            if scope.kind == wanted.get("kind") and scope.item_id == wanted.get("id"):
                self.scope.setCurrentIndex(index)
                break
        self.update_buttons()

    def _restore_failed(self, record, error) -> None:
        self.restoring = False
        if self.session != record:
            self.update_buttons()
            return
        if getattr(error, "status", None) == 401:
            self._forget_session()
            self.status.setText("Jellyfin 登录已失效，请重新登录。")
        else:
            self.status.setText(f"无法连接 Jellyfin：{error}。登录仍保留，可以稍后点「重新连接」。")
        self.update_buttons()

    def _forget_session(self) -> None:
        try:
            self.backend.clear_session()
        except OSError as error:
            self.window_.show_error(error)
        self.session = None
        self.source = None
        self.scopes = []
        blocked = self.scope.blockSignals(True)
        self.scope.clear()
        self.scope.blockSignals(blocked)
        self.invalidate_plan()
        self._show_account()

    def sign_out(self) -> None:
        if self.busy:
            return
        self._forget_session()
        self.status.setText("已退出 Jellyfin 登录。")
        self.update_buttons()

    def _save_session(self, record: dict) -> None:
        try:
            self.backend.save_session(record)
        except (OSError, ValueError) as error:
            self.window_.show_error(error)
            return
        self._use_session(record)

    def _remember_scope(self, index: int) -> None:
        if self.session is None or self.source is None or not 0 <= index < len(self.scopes):
            return
        scope = self.scopes[index]
        source = {"kind": scope.kind, "id": scope.item_id}
        if self.session.get("source") != source:
            self._save_session({**self.session, "source": source})

    def _connect_return(self) -> None:
        if not self.busy:
            self.connect_source()

    # iPod port ----------------------------------------------------------
    def port(self) -> str:
        """The chosen port; empty means auto-detect."""
        return self.window_.prefs.get("port") or ""

    def _show_port(self) -> None:
        port = self.port()
        self.port_label.setText(f"iPod 串口：{port}" if port else "iPod 串口：自动识别")

    def _port_changed(self, _port: str) -> None:
        self._show_port()
        self.invalidate_plan()

    # State ------------------------------------------------------------
    def update_buttons(self) -> None:
        interactive = not self.busy and not self.restoring
        for control in (self.username, self.password, self.token, self.scope,
                        self.port_button, self.connect_button, self.sign_out_button, self.disk_mode):
            control.setEnabled(interactive)
        self.url.setEnabled(interactive and self.session is None)
        self.auth_switch.set_options_enabled(interactive)
        index = self.scope.currentIndex()
        self.preview_button.setEnabled(interactive and self.source is not None
                                       and 0 <= index < len(self.scopes))
        self.apply_button.setEnabled(interactive and self.plan is not None
                                     and bool(self.plan.additions or self.plan.removals))
        self.progress.setVisible(self.busy or self.restoring)
        if self.busy:
            set_badge(self.connection_badge, "处理中…", "busy")
        elif self.source is not None:
            set_badge(self.connection_badge, "已连接", "success")
        elif self.restoring:
            set_badge(self.connection_badge, "已登录，正在连接…", "busy")
        elif self.session is not None:
            set_badge(self.connection_badge, "已登录，未连接", "danger")
        else:
            set_badge(self.connection_badge, "未连接", "neutral")
        self.source_state.emit()

    def _update_metrics(self, plan) -> None:
        add = len(plan.additions) if plan else 0
        same = len(plan.originals) if plan else 0
        remove = len(plan.removals) if plan else 0
        self.tile_add.value.setText(str(add))
        self.tile_same.value.setText(str(same))
        self.tile_remove.value.setText(str(remove))
        t = self.tokens
        self.tile_add.value.setStyleSheet(f"color: {t.success};" if add else "")
        self.tile_remove.value.setStyleSheet(f"color: {t.danger};" if remove else "")

    def invalidate_source(self, *_args) -> None:
        self.source = None
        self.invalidate_plan()

    def invalidate_plan(self, *_args) -> None:
        self.plan = None
        self.list_model.clear()
        self.preview_stack.setCurrentWidget(self.placeholder)
        self._update_metrics(None)
        self.update_buttons()

    def job(self, status: str, action, success, *, device: bool = True) -> None:
        self.busy = True
        self.status.setText(status)
        self.update_buttons()
        self.window_.runner.run(status, lambda _job: action(), lambda r: self._finish(success, r),
                                self._failed, device=device)

    def _finish(self, success, result) -> None:
        self.busy = False
        success(result)
        self.update_buttons()

    def _failed(self, error) -> None:
        self.busy = False
        self.invalidate_plan()
        self.status.setText("操作失败。")
        self.update_buttons()
        self.window_.show_error(error)

    # Steps ------------------------------------------------------------
    def connect_source(self) -> None:
        if self.busy or self.restoring:
            return
        if self.session is not None:
            self.restore_session()
            return
        url = self.url.text()
        backend = self.backend
        if self.auth_mode == "password":
            username, password = self.username.text(), self.password.text()
            self.password.clear_secret()

            def action():
                source, scopes = backend.connect_password(url, username, password)
                return source, scopes, backend.session_record(source, url, username.strip())
            status = "正在登录 Jellyfin 并读取曲库和播放列表…"
        else:
            token = self.token.text()

            def action():
                source, scopes = backend.connect_token(url, token)
                return source, scopes, backend.session_record(source, url)
            status = "正在读取 Jellyfin 曲库和播放列表…"
        self.source = None
        self.invalidate_plan()
        # Signing in never touches the iPod, so it does not queue behind it.
        self.job(status, action, self.signed_in, device=False)

    def signed_in(self, result) -> None:
        source, scopes, record = result
        self._save_session(record)
        self.token.clear_secret()
        self.connected((source, scopes))
        self._remember_scope(self.scope.currentIndex())

    def connected(self, result) -> None:
        self.source, self.scopes = result
        blocked = self.scope.blockSignals(True)
        try:
            self.scope.clear()
            for scope in self.scopes:
                self.scope.addItem(SCOPE_PREFIXES.get(scope.kind, "") + scope.label)
            if self.scopes:
                self.scope.setCurrentIndex(0)
        finally:
            self.scope.blockSignals(blocked)
        self.status.setText("Jellyfin 已连接。选择范围后预览。")

    def preview(self) -> None:
        index = self.scope.currentIndex()
        if self.busy or self.source is None or not 0 <= index < len(self.scopes):
            return
        scope, source, port = self.scopes[index], self.source, self.port()
        self.job("正在比较 Jellyfin 和 iPod 的曲库…",
                 lambda: self.backend.preview(source, scope, port), self.show_plan)

    def show_plan(self, plan) -> None:
        self.plan = plan
        self._fill_list()
        self._update_metrics(plan)
        self.preview_stack.setCurrentWidget(self.details)
        self.status.setText(
            f"目标 {len(plan.tracks)} 首：写入 {len(plan.additions)} 首，"
            f"iPod 已有 {len(plan.originals)} 首，删除 {len(plan.removals)} 首。")

    def _fill_list(self) -> None:
        self.list_model.clear()
        plan = self.plan
        if plan is None:
            return
        wanted = self.filter.current()
        rows = [("add", track.label) for track in plan.additions]
        rows += [("same", track.label) for track in plan.originals]
        rows += [("remove", name) for name in sorted(plan.removals)]
        for kind, text in rows:
            if wanted in (None, "all", kind):
                item = QtGui.QStandardItem(text)
                item.setData(kind, KIND_ROLE)
                item.setEditable(False)
                self.list_model.appendRow(item)

    def preview_rows(self) -> list[tuple[str, str]]:
        return [(self.list_model.item(r).data(KIND_ROLE), self.list_model.item(r).text())
                for r in range(self.list_model.rowCount())]

    def build_confirm_dialog(self, plan):
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
        box.setWindowTitle("确认删除同步副本")
        box.setText(f"从 iPod 删除 {len(plan.removals)} 首同步副本？")
        box.setInformativeText("只会删除预览里列出的、由 Jellyfin 同步写入的副本。")
        cancel = box.addButton("取消", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        apply = box.addButton("应用同步", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        apply.setProperty("variant", "danger")
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        return box, cancel, apply

    def confirm_removals(self, plan) -> bool:
        box, _cancel, apply = self.build_confirm_dialog(plan)
        box.exec()
        return box.clickedButton() is apply

    def confirm(self) -> None:
        plan = self.plan
        if plan is None or self.source is None or self.busy:
            return
        if not plan.removals:
            self.sync(plan)
            return
        if self.confirm_removals(plan) and self.plan is plan and self.source is not None \
                and not self.busy:
            self.sync(plan)

    def sync(self, plan) -> None:
        source, port = self.source, self.port()
        disk_mode = self.disk_mode.isChecked()
        apply = self.backend.apply_disk if disk_mode else self.backend.apply
        status = ("正在通过磁盘模式同步…" if disk_mode
                  else "正在同步。高采样率音频会先在电脑上转换…")
        self.job(status, lambda: apply(source, plan, port), self.sync_complete)

    def sync_complete(self, result) -> None:
        added, removed = result
        self.invalidate_plan()
        self.status.setText(f"同步完成：写入 {added} 首，删除 {removed} 首。")
        self.window_.flash(f"Jellyfin 同步完成：写入 {added} 首，删除 {removed} 首")

    def apply_theme(self, tokens) -> None:
        for field in self.password_inputs:
            field.update_theme(tokens)
        self.scope.update_theme(tokens)
        self.disk_mode.tokens = tokens
        self.disk_mode.update()
        self._update_metrics(self.plan)
        self.details.viewport().update()
