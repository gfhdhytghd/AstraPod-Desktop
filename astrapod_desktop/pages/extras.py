"""CalDAV, local calendars and contacts over the existing Disk Mode lane."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from .. import calendar_format, calendar_sync, contact_format, contact_sync, local_calendar
from ..caldav import Client, Collection, KINDS
from ..extras_storage import PlayerDisk, Store, fingerprint, secret
from ..qt import QtCore, QtGui, QtWidgets, Qt
from ..widgets import PageHeader, PasswordLineEdit, button, label, make_card
from . import Page
from .sync import disk_mode_option

ACCOUNT = "caldav.json"
USER_ROLE = Qt.ItemDataRole.UserRole


class CalendarEntryDialog(QtWidgets.QDialog):
    def __init__(self, entries, parent):
        super().__init__(parent)
        self.setWindowTitle("本地日历")
        self.resize(480, 380)
        layout = QtWidgets.QVBoxLayout(self)
        self.items = QtWidgets.QListWidget()
        self.items.setAccessibleName("本地日程与待办")
        for uid, recurrence, component in entries:
            title = str(component.get("SUMMARY", "未命名"))
            kind = "待办" if component.name == "VTODO" else "日程"
            stamp = component.get("RECURRENCE-ID") or component.get("DTSTART") or component.get("DUE")
            date = getattr(stamp, "dt", None)
            suffix = f" · {date.isoformat()}" if date is not None else ""
            row = QtWidgets.QListWidgetItem(f"{title} · {kind}{suffix}")
            row.setData(USER_ROLE, (uid, recurrence))
            self.items.addItem(row)
        layout.addWidget(self.items, 1)
        actions = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        edit = actions.addButton("编辑条目", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        self.items.currentItemChanged.connect(lambda current, _old: edit.setEnabled(current is not None))
        edit.setEnabled(self.items.count() > 0)
        actions.accepted.connect(self.accept)
        actions.rejected.connect(self.reject)
        self.items.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(actions)
        if self.items.count():
            self.items.setCurrentRow(0)


class CalendarEditDialog(QtWidgets.QDialog):
    def __init__(self, component, parent):
        super().__init__(parent)
        todo = component.name == "VTODO"
        self.setWindowTitle("编辑待办" if todo else "编辑日程")
        self.resize(510, 340)
        fields = [("SUMMARY", "标题"), ("DTSTART", "开始")]
        fields.append(("DUE", "截止") if todo else ("DTEND", "结束"))
        fields.append(("STATUS", "状态") if todo else ("LOCATION", "地点"))
        fields.append(("DESCRIPTION", "备注"))
        self.editors = {}
        self.initial = {}
        form = QtWidgets.QFormLayout(self)
        for field, title in fields:
            value = component.get(field)
            stamp = getattr(value, "dt", None)
            text = stamp.isoformat() if stamp is not None else str(value or "")
            if field == "STATUS":
                editor = QtWidgets.QComboBox()
                for status, caption in (("", "未设置"), ("NEEDS-ACTION", "未完成"),
                                        ("IN-PROCESS", "进行中"), ("COMPLETED", "已完成"),
                                        ("CANCELLED", "已取消")):
                    editor.addItem(caption, status)
                if editor.findData(text) < 0:
                    editor.addItem(text, text)
                editor.setCurrentIndex(editor.findData(text))
            elif field == "DESCRIPTION":
                editor = QtWidgets.QPlainTextEdit(text)
            else:
                editor = QtWidgets.QLineEdit(text)
                if field in ("DTSTART", "DTEND", "DUE"):
                    editor.setPlaceholderText("YYYY-MM-DD 或 YYYY-MM-DDTHH:MM")
            self.editors[field] = editor
            self.initial[field] = text
            form.addRow(title, editor)
        actions = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        actions.accepted.connect(self.accept)
        actions.rejected.connect(self.reject)
        form.addRow(actions)

    def changes(self):
        result = {}
        for field, editor in self.editors.items():
            if isinstance(editor, QtWidgets.QComboBox):
                value = editor.currentData()
            elif isinstance(editor, QtWidgets.QPlainTextEdit):
                value = editor.toPlainText()
            else:
                value = editor.text()
            if value != self.initial[field]:
                result[field] = value
        return result


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
        self.calendars.currentItemChanged.connect(self._buttons)
        layout.addWidget(self.calendars, 1)
        files = QtWidgets.QHBoxLayout()
        self.import_ics = button("导入 ICS", "secondary", slot=self.import_calendars)
        self.edit_ics = button("编辑本地日历", "secondary", slot=self.edit_local_calendar)
        self.export_ics = button("导出 ICS", "secondary", slot=self.export_calendar)
        for action in (self.import_ics, self.edit_ics, self.export_ics):
            files.addWidget(action)
        files.addStretch()
        layout.addLayout(files)
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
        selected = self._selected_urls()
        remote_ready = self._same_account() and not self.password.text()
        includes_remote = bool(selected - {local_calendar.LOCAL.url})
        self.calendar_sync.setEnabled(not busy and ready and bool(selected)
                                      and (remote_ready or not includes_remote))
        current = self._current_calendar()
        is_local = current is not None and current.url == local_calendar.LOCAL.url
        self.import_ics.setEnabled(not busy)
        self.edit_ics.setEnabled(not busy and is_local)
        self.export_ics.setEnabled(not busy and current is not None and (is_local or remote_ready))
        self.contact_sync.setEnabled(not busy and ready)
        self.import_button.setEnabled(not busy)
        self.new_button.setEnabled(not busy)
        self.edit_button.setEnabled(not busy and self.contacts.currentItem() is not None)
        for widget in (self.url, self.username, self.password, self.calendars, *self.targets.values()):
            widget.setEnabled(not busy)

    def _selected_urls(self):
        return {item.data(USER_ROLE) for i in range(self.calendars.count())
                if (item := self.calendars.item(i)).checkState() == Qt.CheckState.Checked}

    def _current_calendar(self):
        item = self.calendars.currentItem()
        url = item.data(USER_ROLE) if item is not None else None
        return next((calendar for calendar in self.collections if calendar.url == url), None)

    def _show_collections(self, collections):
        previous = self._current_calendar()
        self.collections = [local_calendar.LOCAL, *collections]
        selected = set(self.account.get("selected", ()))
        with QtCore.QSignalBlocker(self.calendars):
            self.calendars.clear()
            for calendar in self.collections:
                types = "、".join("日程" if code == "VEVENT" else "待办" for code in calendar.kinds)
                item = QtWidgets.QListWidgetItem(f"{calendar.name} · {types}")
                item.setData(USER_ROLE, calendar.url)
                item.setCheckState(Qt.CheckState.Checked if calendar.url in selected
                                   else Qt.CheckState.Unchecked)
                self.calendars.addItem(item)
                if previous is not None and previous.url == calendar.url:
                    self.calendars.setCurrentItem(item)
            if self.calendars.currentItem() is None:
                self.calendars.setCurrentRow(0)
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
        checked = self._selected_urls()
        config["selected"] = sorted(checked if same else checked & {local_calendar.LOCAL.url})
        config["targets"] = {kind: picker.currentData() for kind, picker in self.targets.items()
                             if same or picker.currentData() == local_calendar.LOCAL.url}
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

    def import_calendars(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "导入日历文件", "", "iCalendar (*.ics *.ICS)")
        if not paths:
            return

        def done(result):
            items, conflicts = result
            self.calendars.setCurrentRow(0)
            self._report(f"导入完成。本地日历有 {len(items)} 组条目。", conflicts)

        self._run("正在导入 ICS…", lambda: local_calendar.import_files(self.store, paths), done)

    def edit_local_calendar(self):
        try:
            items = local_calendar.load(self.store)
            entries = list(local_calendar.entries(items))
        except (OSError, ValueError) as error:
            self._failed(error)
            return
        if not entries:
            self.status.setText("本地日历为空。请导入 ICS 文件或同步设备条目。")
            return
        chooser = CalendarEntryDialog(entries, self)
        if chooser.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        uid, recurrence = chooser.items.currentItem().data(USER_ROLE)
        component = next(item for key, instance, item in entries if (key, instance) == (uid, recurrence))
        dialog = CalendarEditDialog(component, self)
        while dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            try:
                updated = local_calendar.edit(items[uid], recurrence, dialog.changes())
            except ValueError as error:
                QtWidgets.QMessageBox.warning(self, "无法保存日历条目", str(error))
                continue
            if updated != items[uid]:
                self._run("正在保存条目…",
                          lambda: local_calendar.save(self.store, uid, items[uid], updated),
                          lambda _result: self.status.setText("本地日历条目已保存。"))
            break

    def export_calendar(self):
        collection = self._current_calendar()
        if collection is None or not self.export_ics.isEnabled():
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出日历文件", "calendar.ics", "iCalendar (*.ics)")
        if not path:
            return
        if not path.lower().endswith(".ics"):
            # Confirm the final filename, including its suffix, in the save dialog.
            self.status.setText("请输入以 .ics 结尾的文件名。")
            return
        url, user = self._identity()

        def work():
            with self.store.locked():
                if collection.url == local_calendar.LOCAL.url:
                    data = calendar_format.merge(local_calendar.load(self.store), name=collection.name)
                else:
                    client = DemoCalendar() if self.demo else Client(url, user, secret(fingerprint(url, user)))
                    data = calendar_sync.export_dav(client, collection)
                local_calendar.export_file(path, data)

        self._run("正在导出 ICS…", work, lambda _result: self.status.setText("日历已导出。"))

    def sync_calendars(self):
        if not self.calendar_sync.isEnabled():
            return
        selected = self._selected_urls()
        calendars = [calendar for calendar in self.collections if calendar.url in selected]
        targets = {kind: picker.currentData() for kind, picker in self.targets.items()}
        url, user = self._identity()
        account_id = fingerprint(self.account.get("url"), self.account.get("user"))
        includes_remote = bool(selected - {local_calendar.LOCAL.url})
        config = dict(self.account, selected=sorted(selected), targets=targets)
        disk_mode = self.window_.disk
        volume, player = disk_mode.volume, disk_mode.player

        def work():
            with self.store.locked():
                self.store.write(ACCOUNT, config)
                client = None
                if includes_remote:
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
