"""One Disk Mode state and transition lane for both transfer pages."""
from .qt import QtCore, Signal


class DiskMode(QtCore.QObject):
    changed = Signal()
    status = Signal(str)
    written = Signal(object)

    def __init__(self, backend, daemon, runner, parent):
        super().__init__(parent)
        self.backend = backend
        self.daemon = daemon
        self.runner = runner
        self.port = ""
        self.mode = "none"
        self.volume = None
        self.serial = None
        self.player = None
        self.problem = ""
        self.transition = False
        self.borrowed = False
        self._known_serials = {}
        self._dirty = {}
        self.written.connect(self._mark_written)
        self.runner.busy_changed.connect(lambda _busy: self.changed.emit())

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.update)
        self.timer.start()

    def _mark_written(self, device):
        if device is not None:
            self._dirty[device[1]] = device

    def set_port(self, port):
        self.port = port
        self.update()

    def update(self):
        link = self.daemon.state.get("link") or {}
        link_mode = (link.get("mode") or "none") if self.daemon.connected else "none"
        live_serial = link.get("serial") if link_mode == "podlink" else None
        live_port = link.get("port") if live_serial else None
        try:
            selected_serial = self.backend.disk_serial(self.port, live_port, live_serial)
            if self.port and selected_serial:
                self._known_serials[self.port] = selected_serial
            if self.port and not selected_serial:
                selected_serial = self._known_serials.get(self.port)
            volume = self.backend.disk_volume(link_mode, self.port, selected_serial)
            player = self.backend.disk_player(volume) if volume is not None else None
            problem = ("暂不支持 Apple 原厂固件的音乐资料库。"
                       if volume is not None and player is None else "")
        except (OSError, RuntimeError) as error:
            volume, player, problem = None, None, str(error)
        mode = "disk" if volume is not None or link_mode == "disk" else link_mode
        serial = live_serial if mode == "podlink" else None
        borrowed = bool(link.get("borrowed")) if mode == "podlink" else False
        values = (mode, volume, player, serial, problem, borrowed)
        if (self.mode, self.volume, self.player, self.serial, self.problem, self.borrowed) != values:
            self.mode, self.volume, self.player, self.serial, self.problem, self.borrowed = values
            self.changed.emit()
            if problem:
                self.status.emit(problem)
        if (self.mode == "podlink" and not self.transition and not self.runner.busy
                and not self.borrowed and live_serial in self._dirty):
            self._refresh(self._dirty.pop(live_serial))

    @property
    def available(self):
        return self.mode == "podlink" or self.player is not None

    @property
    def can_switch(self):
        return ((self.mode == "podlink" or self.volume is not None)
                and not self.transition and not self.runner.busy and not self.borrowed)

    def request(self, enabled):
        if not self.can_switch or enabled == (self.mode == "disk"):
            self.changed.emit()
            return
        volume, port = self.volume, self.port
        if enabled:
            action = lambda: self.backend.enter_disk(port)
            self._run("正在进入磁盘模式并挂载…", action, "iPod 已进入磁盘模式。")
        else:
            self._run("正在安全弹出 iPod…", lambda: self.backend.eject_disk(volume),
                      "iPod 磁盘已弹出。")

    def _refresh(self, device):
        self._run("正在请求 iPod 更新资料库…", lambda: self.backend.refresh_disk(device),
                  "iPod 已收到资料库更新请求。")

    def _run(self, message, action, success):
        self.transition = True
        self.changed.emit()
        self.status.emit(message)

        def finish(text):
            # Keep refresh scheduling disabled until this transition reports its result.
            self.update()
            self.transition = False
            self.changed.emit()
            self.status.emit(self.problem or text)
            self.update()

        self.runner.run(message, lambda _job: action(), lambda _value: finish(success),
                        lambda error: finish(f"磁盘模式操作失败：{error}"))
