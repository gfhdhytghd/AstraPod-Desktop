"""CalDAV and local contacts, synced through the existing Disk Mode lane."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from .. import calendar_sync, contact_format, contact_sync
from ..caldav import Client, Collection, KINDS
from ..extras_storage import PlayerDisk, Store, fingerprint, secret
from ..qt import QtCore, QtGui, QtWidgets, Qt
from ..widgets import PageHeader, PasswordLineEdit, button, label, make_card
from . import Page
from .sync import disk_mode_option

ACCOUNT = "caldav.json"
USER_ROLE = Qt.ItemDataRole.UserRole


class ContactDialog(QtWidgets.QDialog):
    def __init__(self, card, parent):
        super().__init__(parent)
        self.setWindowTitle("编辑联系人")
        self.resize(570, 620)
        self.card = card
        self.fields = {}
        self.initial = {}
        layout = QtWidgets.QVBoxLayout(self)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        layout.addWidget(scroll)
        contents = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(contents)
        scroll.setWidget(contents)
        for name, caption in contact_format.FIELDS.items():
            values = contact_format.values(card, name, visible=True)
            if name in contact_format.REPEATED or name == "NOTE":
                editor = QtWidgets.QPlainTextEdit("\n".join(values))
                editor.setMaximumHeight(68)
                if name in contact_format.REPEATED:
                    editor.setPlaceholderText("每行一项")
            else:
                editor = QtWidgets.QLineEdit(values[0] if values else "")
            form.addRow(caption, editor)
            self.fields[name] = editor
            self.initial[name] = (editor.toPlainText() if isinstance(editor, QtWidgets.QPlainTextEdit)
                                  else editor.text())
        if contact_format.values(card, "PHOTO"):
            form.addRow(label("保留联系人照片。", "helper"))
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def result_card(self):
        changes = {}
        for name, editor in self.fields.items():
            text = editor.toPlainText() if isinstance(editor, QtWidgets.QPlainTextEdit) else editor.text()
            if text == self.initial[name]:
                continue
            if name in contact_format.REPEATED:
                changes[name] = [row for row in text.splitlines() if row.strip()]
            else:
                changes[name] = [text] if text or name == "FN" else []
                if name != "NOTE":
                    changes[name].extend(contact_format.values(self.card, name, visible=True)[1:])
        return contact_format.edit(self.card, changes)


class ExtrasPage(Page):
    key = "extras"

    def __init__(self, window):
        super().__init__(window)
        self.demo = TemporaryDirectory(prefix="astrapod-extras-demo-") if window.services.demo else None
        self.store = Store(Path(self.demo.name) / "state" if self.demo else None)
        self.collections = []
        self.cards = {}
        self.account = {}
        self.loading = False

        self.layout_.addWidget(PageHeader("日历与联系人"))
        disk_card, disk_layout = make_card()
        self.disk_switch = disk_mode_option(window, disk_layout)
        self.layout_.addWidget(disk_card)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._calendar_tab(), "日历与待办")
        self.tabs.addTab(self._contacts_tab(), "联系人")
        self.layout_.addWidget(self.tabs, 1)

        bottom = QtWidgets.QHBoxLayout()
        self.status = label("", "helper", wrap=True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        bottom.addWidget(self.status, 1)
        self.conflict_button = button("打开冲突副本", "secondary", slot=self.open_conflicts)
        self.conflict_button.setToolTip(str(self.store.root))
        self.conflict_button.hide()
        bottom.addWidget(self.conflict_button)
        self.layout_.addLayout(bottom)
        window.disk.changed.connect(self._buttons)
        window.disk.status.connect(self.status.setText)
        window.runner.busy_changed.connect(self._buttons)
        self._buttons()

    def _calendar_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        account = QtWidgets.QFormLayout()
        self.url = QtWidgets.QLineEdit()
        self.username = QtWidgets.QLineEdit()
        self.password = PasswordLineEdit("密码或应用专用密码")
        account.addRow("CalDAV 服务器", self.url)
        account.addRow("用户名", self.username)
        account.addRow("密码", self.password)
        layout.addLayout(account)
        row = QtWidgets.QHBoxLayout()
        self.discover_button = button("连接并列出日历", "secondary", slot=self.discover)
        row.addWidget(self.discover_button)
        row.addStretch()
        layout.addLayout(row)

        self.calendars = QtWidgets.QListWidget()
        self.calendars.setAccessibleName("选择要同步的日历")
        self.calendars.itemChanged.connect(self._selected)
        layout.addWidget(self.calendars, 1)
        destinations = QtWidgets.QFormLayout()
        self.targets = {kind: QtWidgets.QComboBox() for kind in KINDS}
        destinations.addRow("设备新日程", self.targets["VEVENT"])
        destinations.addRow("设备新待办", self.targets["VTODO"])
        layout.addLayout(destinations)
        self.calendar_sync = button("同步日历与待办", "primary", "sync", self.sync_calendars)
        layout.addWidget(self.calendar_sync)
        self.url.textChanged.connect(self._buttons)
        self.username.textChanged.connect(self._buttons)
        self.password.textChanged.connect(self._buttons)
        return tab

    def _contacts_tab(self):
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        row = QtWidgets.QHBoxLayout()
        self.import_button = button("导入 vCard", "secondary", "import", self.import_contacts)
        self.new_button = button("新建联系人", "secondary", slot=lambda: self.edit_contact(None))
        self.edit_button = button("编辑联系人", "secondary", slot=self.edit_selected)
        for action in (self.import_button, self.new_button, self.edit_button):
            row.addWidget(action)
        row.addStretch()
        layout.addLayout(row)
        self.contacts = QtWidgets.QListWidget()
        self.contacts.setAccessibleName("联系人")
        self.contacts.itemSelectionChanged.connect(self._buttons)
        self.contacts.itemDoubleClicked.connect(lambda _item: self.edit_selected())
        layout.addWidget(self.contacts, 1)
        self.contact_sync = button("同步联系人", "primary", "sync", self.sync_contacts)
        layout.addWidget(self.contact_sync)
        return tab

    def shown(self):
        if self.loading:
            return
        try:
            if not self.account:
                self.account = self.store.read(ACCOUNT, {})
                self.url.setText(self.account.get("url", "https://demo.invalid" if self.demo else ""))
                self.username.setText(self.account.get("user", "demo" if self.demo else ""))
                self._show_collections([Collection(*row) for row in self.account.get("collections", [])])
            self._show_cards(contact_sync.load(self.store))
            self.conflict_button.setVisible(any(self.store.root.glob("conflict-*")))
        except (OSError, ValueError) as error:
            self._failed(error)

    def _identity(self):
        return self.url.text().strip(), self.username.text().strip()

    def _same_account(self):
        return self._identity() == (self.account.get("url"), self.account.get("user"))

    def _buttons(self, *_args):
        if not hasattr(self, "contact_sync"):
            return
        disk = self.window_.disk
        ready = self.demo is not None or (disk.mode == "disk" and disk.player is not None)
        busy = self.loading or self.window_.busy or disk.transition
        self.discover_button.setEnabled(not busy and all(self._identity()))
        self.calendar_sync.setEnabled(not busy and ready and self._same_account()
                                      and not self.password.text() and bool(self._selected_urls()))
        self.contact_sync.setEnabled(not busy and ready)
        self.import_button.setEnabled(not busy)
        self.new_button.setEnabled(not busy)
        self.edit_button.setEnabled(not busy and self.contacts.currentItem() is not None)
        for widget in (self.url, self.username, self.password, self.calendars, *self.targets.values()):
            widget.setEnabled(not busy)

    def _selected_urls(self):
        return {item.data(USER_ROLE) for i in range(self.calendars.count())
                if (item := self.calendars.item(i)).checkState() == Qt.CheckState.Checked}

    def _show_collections(self, collections):
        self.collections = collections
        selected = set(self.account.get("selected", ()))
        with QtCore.QSignalBlocker(self.calendars):
            self.calendars.clear()
            for calendar in collections:
                types = "、".join("日程" if code == "VEVENT" else "待办" for code in calendar.kinds)
                item = QtWidgets.QListWidgetItem(f"{calendar.name} · {types}")
                item.setData(USER_ROLE, calendar.url)
                item.setCheckState(Qt.CheckState.Checked if calendar.url in selected
                                   else Qt.CheckState.Unchecked)
                self.calendars.addItem(item)
        self._selected(self.account.get("targets", {}))

    def _selected(self, previous=None):
        selected = self._selected_urls()
        for kind, picker in self.targets.items():
            prior = previous.get(kind) if isinstance(previous, dict) else picker.currentData()
            available = [calendar for calendar in self.collections
                         if calendar.url in selected and kind in calendar.kinds]
            picker.clear()
            picker.addItem("保留在设备收件箱", None)
            for calendar in available:
                picker.addItem(calendar.name, calendar.url)
            index = picker.findData(prior) if prior else -1
            picker.setCurrentIndex(index if index >= 0 else 0)
        self._buttons()

    def _run(self, message, work, done, *, device=False):
        self.loading = True
        self.status.setText(message)
        self._buttons()

        def finish(value, callback):
            self.loading = False
            callback(value)
            self.conflict_button.setVisible(any(self.store.root.glob("conflict-*")))
            self._buttons()

        self.window_.runner.run(message, lambda _job: work(),
                                lambda value: finish(value, done),
                                lambda error: finish(error, self._failed), device=device)

    def _failed(self, error):
        self.status.setText(f"操作失败：{error}")

    def discover(self):
        url, user = self._identity()
        password = self.password.text()
        same = self._same_account()
        config = self.account.copy() if same else {}
        account_id = fingerprint(url, user)

        def work():
            with self.store.locked():
                if self.demo:
                    client = DemoCalendar()
                else:
                    if not password and not same:
                        raise ValueError("请输入密码或应用专用密码。")
                    client = Client(url, user, password or secret(account_id))
                collections = client.discover()
                if password and not self.demo:
                    secret(account_id, password)
                config.update(url=url, user=user,
                              collections=[(c.url, c.name, c.kinds) for c in collections])
                self.store.write(ACCOUNT, config)
                return collections

        def done(collections):
            self.account = config
            self.password.clear_secret()
            self._show_collections(collections)
            self.status.setText(f"找到 {len(collections)} 个日历。")

        self._run("正在查找 CalDAV 日历…", work, done)

    @contextmanager
    def _player(self, volume, player):
        from hrdisksync import MediaDisk, disk_player, mounted_media
        if self.demo is not None:
            root = Path(self.demo.name) / "player"
            root.mkdir(exist_ok=True)
            fd = os.open(root, os.O_DIRECTORY | os.O_RDONLY)
            try:
                yield PlayerDisk(MediaDisk(fd, lambda: None)), "demo"
            finally:
                os.close(fd)
        else:
            if volume is None or player is None or disk_player(volume) != player:
                raise RuntimeError("请挂载所选 AstraPod 的磁盘后重试。")
            with mounted_media(volume) as disk:
                yield PlayerDisk(disk), player[1]

    def sync_calendars(self):
        if not self.calendar_sync.isEnabled():
            return
        selected = self._selected_urls()
        calendars = [calendar for calendar in self.collections if calendar.url in selected]
        targets = {kind: picker.currentData() for kind, picker in self.targets.items()}
        url, user = self._identity()
        account_id = fingerprint(url, user)
        config = dict(self.account, selected=sorted(selected), targets=targets)
        disk_mode = self.window_.disk
        volume, player = disk_mode.volume, disk_mode.player

        def work():
            with self.store.locked():
                self.store.write(ACCOUNT, config)
                client = DemoCalendar() if self.demo else Client(url, user, secret(account_id))
                with self._player(volume, player) as (disk, serial):
                    return calendar_sync.sync(client, self.store, disk, serial, account_id, calendars, targets)

        def done(result):
            self.account = config
            calendars, pending, conflicts = result
            text = f"同步 {calendars} 个日历。"
            if pending:
                text += f"设备收件箱保留 {pending} 项。请选择同步目标日历。"
            self._report(text, conflicts)

        self._run("正在同步日历与待办…", work, done, device=True)

    def _show_cards(self, cards, selected=None):
        if selected is None and self.contacts.currentItem() is not None:
            selected = self.contacts.currentItem().data(USER_ROLE)
        self.cards = cards
        self.contacts.clear()
        for uid, card in sorted(cards.items(), key=lambda row: contact_format.title(row[1]).casefold()):
            item = QtWidgets.QListWidgetItem(contact_format.title(card))
            item.setData(USER_ROLE, uid)
            self.contacts.addItem(item)
            if uid == selected:
                self.contacts.setCurrentItem(item)
        self._buttons()

    def import_contacts(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "导入联系人", "", "vCard (*.vcf *.VCF)")
        if files:
            self._run("正在导入联系人…", lambda: contact_sync.import_files(self.store, files),
                      lambda result: self._contacts_done(result[0], "导入完成。", result[1]))

    def _contacts_done(self, cards, message, conflicts=()):
        self._show_cards(cards)
        self._report(f"{message}电脑有 {len(cards)} 位联系人。", conflicts)

    def edit_selected(self):
        if self.edit_button.isEnabled() and (item := self.contacts.currentItem()) is not None:
            self.edit_contact(item.data(USER_ROLE))

    def edit_contact(self, uid):
        previous = self.cards.get(uid)
        dialog = ContactDialog(previous if previous is not None else contact_format.new(), self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        card = dialog.result_card()
        if not contact_format.values(card, "FN", visible=True)[0].strip():
            self.status.setText("请输入显示名称。")
            return
        self._run("正在保存联系人…", lambda: contact_sync.save(self.store, card, previous),
                  lambda cards: self._contacts_done(cards, "联系人已保存。"))

    def sync_contacts(self):
        if not self.contact_sync.isEnabled():
            return
        disk_mode = self.window_.disk
        volume, player = disk_mode.volume, disk_mode.player

        def work():
            with self.store.locked(), self._player(volume, player) as (disk, serial):
                return contact_sync.sync(self.store, disk, serial)

        self._run("正在同步联系人…", work,
                  lambda result: self._contacts_done(result[0], "同步完成。", result[1]), device=True)

    def _report(self, message, conflicts):
        if conflicts:
            message += f" 发现 {len(conflicts)} 项冲突。"
            box = QtWidgets.QMessageBox(self)
            box.setWindowTitle("同步冲突")
            box.setText(message)
            box.setTextFormat(Qt.TextFormat.PlainText)
            box.setDetailedText("\n".join(conflicts))
            box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            box.open()
        self.status.setText(message)

    def open_conflicts(self):
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.store.root)))


class DemoCalendar:
    url = "https://demo.invalid/calendar/"
    sample = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//AstraPod Demo//EN\r\n"
        "BEGIN:VEVENT\r\nUID:demo-event\r\nDTSTAMP:20261127T120000Z\r\n"
        "DTSTART:20261128T120000Z\r\nSUMMARY:示例日程\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )

    def discover(self):
        return [Collection(self.url, "示例日历", KINDS)]

    def snapshot(self, _url, cached):
        return cached or {"token": "demo", "ctag": None,
                          "resources": {self.url + "demo.ics": {"body": self.sample, "etag": '"demo"'}}}
