"""The iPod serial port picker, on the device page and in Settings.

The field shows the auto-detected port in a muted colour; typing another path
makes it a manual override. The saved choice is the window's "port" pref,
where an empty value means auto-detect. Every picker follows that pref when
it changes.
"""
from __future__ import annotations

from ..qt import QtWidgets
from ..widgets import button, label, make_badge, set_badge

PORT_EXAMPLE = "例如 /dev/serial/by-id/usb-…-if00"


class PortPicker(QtWidgets.QWidget):
    """Field, state badge, "use auto-detect" and re-detect buttons; `help`
    is a separate label for the caller to place. With autosave the choice
    is saved as soon as editing finishes."""

    def __init__(self, window, autosave: bool = False, parent=None):
        super().__init__(parent)
        self.window_ = window
        self.autosave = autosave
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.field = QtWidgets.QLineEdit()
        self.field.setAccessibleName("iPod 串口")
        self.field.textChanged.connect(self._typed)
        self.field.editingFinished.connect(self._editing_finished)
        row.addWidget(self.field, 1)
        self.badge = make_badge("")
        row.addWidget(self.badge)
        self.auto_button = button("改用自动识别", "ghost", None, self.use_auto)
        row.addWidget(self.auto_button)
        self.detect_button = button("", None, "refresh", self.detect)
        self.detect_button.setToolTip("重新检测 iPod 串口")
        self.detect_button.setAccessibleName("重新检测 iPod 串口")
        row.addWidget(self.detect_button)
        self.help = label("", "helper", wrap=True)
        self.detected = None
        self._filling = False
        self.manual = False
        self.load_saved(detect=False)
        self._link_key = self._link_state_key(window.daemon.state)
        window.daemon.state_changed.connect(self._link_changed)
        window.port_changed.connect(self._saved_changed)
        self.detect()

    def _saved_changed(self, _port: str) -> None:
        self.load_saved(detect=False)

    def load_saved(self, detect: bool = True) -> None:
        saved = self.window_.prefs.get("port") or ""
        self.manual = bool(saved)
        self._fill(saved)
        if detect:
            self.detect()
        else:
            self.show_state()

    def detect(self) -> None:
        detect = getattr(self.window_.services.library, "detect_port", None)
        self.detected = detect() if detect is not None else None
        self.show_state()

    def use_auto(self) -> None:
        self.manual = False
        self._fill(self.detected or "")
        self.show_state()
        if self.autosave:
            self.commit()

    def override(self) -> str:
        """The explicit port to save; empty means auto-detect."""
        return self.field.text().strip() if self.manual else ""

    def commit(self) -> None:
        if self.override() != (self.window_.prefs.get("port") or ""):
            self.window_.save_pref("port", self.override())

    def _fill(self, text: str) -> None:
        self._filling = True
        try:
            self.field.setText(text)
        finally:
            self._filling = False

    def _typed(self, text: str) -> None:
        if self._filling:
            return
        text = text.strip()
        self.manual = bool(text) and text != self.detected
        self.show_state(refill=False)

    def _editing_finished(self) -> None:
        self.show_state()
        if self.autosave:
            self.commit()

    def show_state(self, refill: bool = True) -> None:
        detected = self.detected
        if not self.manual and refill and not self.field.hasFocus():
            self._fill(detected or "")
        self.field.setPlaceholderText(PORT_EXAMPLE)
        self.field.setProperty("auto_value", not self.manual)
        tokens = self.window_.theme_manager.tokens
        self.field.setStyleSheet(f"color: {tokens.text_secondary};"
                                 if not self.manual and tokens is not None else "")
        self.auto_button.setVisible(self.manual)
        if self.manual:
            set_badge(self.badge, "手动指定", "busy")
            self.help.setText(
                f"使用手动填写的串口。自动识别结果：{detected}。" if detected
                else "使用手动填写的串口。现在没有自动识别到 iPod。")
        elif detected:
            set_badge(self.badge, "自动识别", "accent")
            self.help.setText("已自动识别到 iPod。要改用别的串口，直接修改上面的路径。")
        else:
            set_badge(self.badge, "未检测到", "neutral")
            self.help.setText("没有检测到 iPod。用 USB 连接并打开 PodLink 后点刷新，"
                              "或手动填写串口。")

    @staticmethod
    def _link_state_key(state: dict):
        link = state.get("link") or {}
        return bool(link.get("connected")), link.get("port"), link.get("mode")

    def _link_changed(self, state: dict) -> None:
        key = self._link_state_key(state)
        if key != self._link_key:
            self._link_key = key
            self.detect()
