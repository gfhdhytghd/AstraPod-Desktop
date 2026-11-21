"""Log: HerdRemote's output and the daemon's log events, filterable."""
from __future__ import annotations

import time

from ..qt import QtGui, QtWidgets, FONT_NUMERIC
from ..widgets import PageHeader, button, label, make_card
from . import Page

LIMIT = 5000


class LogsPage(Page):
    key = "logs"
    searchable = True

    def __init__(self, window, parent=None):
        super().__init__(window, parent)
        self.lines: list[str] = []
        self.filter = ""
        self.header = PageHeader("日志", "后台远程服务的输出")
        self.pause = self.header.add(button("暂停滚动", None, None, None))
        self.pause.setCheckable(True)
        self.header.add(button("复制", None, "copy", self.copy))
        self.header.add(button("清空", "ghost", None, self.clear))
        self.layout_.addWidget(self.header)
        card, card_layout = make_card(0, expand=True, margins=(4, 4, 4, 4))
        self.view = QtWidgets.QPlainTextEdit()
        self.view.setObjectName("LogView")
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(LIMIT)
        self.view.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        mono = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
        mono.setPointSizeF(max(9.5, mono.pointSizeF()))
        self.view.setFont(mono)
        self.view.setAccessibleName("日志")
        card_layout.addWidget(self.view)
        self.layout_.addWidget(card, 1)
        self.count = label("", "helper")
        self.layout_.addWidget(self.count)
        window.daemon.log_line.connect(self.append)

    def append(self, line: str) -> None:
        stamped = f"{time.strftime('%H:%M:%S')}  {line.rstrip()}"
        self.lines.append(stamped)
        if len(self.lines) > LIMIT:
            del self.lines[: len(self.lines) - LIMIT]
        self.count.setText(f"{len(self.lines)} 行")
        if not self.filter or self.filter in stamped.casefold():
            bar = self.view.verticalScrollBar()
            at_end = bar.value() >= bar.maximum() - 4
            self.view.appendPlainText(stamped)
            if at_end and not self.pause.isChecked():
                bar.setValue(bar.maximum())

    def set_search(self, text: str) -> None:
        self.filter = text.casefold().strip()
        shown = [l for l in self.lines if not self.filter or self.filter in l.casefold()]
        self.view.setPlainText("\n".join(shown))
        self.view.verticalScrollBar().setValue(self.view.verticalScrollBar().maximum())
        self.count.setText(f"显示 {len(shown)} 行，共 {len(self.lines)} 行" if self.filter
                           else f"{len(self.lines)} 行")

    def copy(self) -> None:
        QtWidgets.QApplication.clipboard().setText(self.view.toPlainText())
        self.window_.flash("已复制日志")

    def clear(self) -> None:
        self.lines.clear()
        self.view.clear()
        self.count.setText("")
