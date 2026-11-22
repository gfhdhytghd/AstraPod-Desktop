"""Tray, close-to-tray, the supervised HerdRemote child and the plugin host."""
from __future__ import annotations

import os
import sys

from podlink_ui_theme import make_tray_icon

from .qt import TOOLS_DIR, QtCore, QtWidgets


class DesktopRuntime(QtCore.QObject):
    """Keeps the remote engine and the input plugins alive behind the tray."""

    def __init__(self, app, window, start_plugins: bool = True):
        super().__init__(app)
        self.app = app
        self.window = window
        self.stopping = False
        self.remote = QtCore.QProcess(self)
        self.remote.setProgram(sys.executable)
        self.remote.setArguments(["-u", str(TOOLS_DIR / "herdremote" / "herdremote.py")])
        self.remote.setProcessChannelMode(QtCore.QProcess.ProcessChannelMode.MergedChannels)
        self.remote.readyReadStandardOutput.connect(self._remote_output)
        self.remote.finished.connect(self.schedule_remote)
        self.remote.errorOccurred.connect(self.schedule_remote)
        self.retry = QtCore.QTimer(self)
        self.retry.setSingleShot(True)
        self.retry.timeout.connect(self.start_remote)
        self._partial = b""
        self.plugins = window.services.plugins
        self.plugin_retry = QtCore.QTimer(self)
        self.plugin_retry.setSingleShot(True)
        self.plugin_retry.timeout.connect(self.start_plugins)
        app.aboutToQuit.connect(self.stop)

        services = window.services
        services.library.borrow = True
        services.sync.borrow = True
        window.runtime = self

        self.tray = None
        if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QtWidgets.QSystemTrayIcon(self)
            self.tray.setToolTip("AstraPod：iPod 资料库、同步与远程")
            self.menu = QtWidgets.QMenu(window)
            self.menu.addAction("打开 AstraPod", self.show_window)
            self.menu.addAction("Agent", lambda: self.show_page("agents"))
            self.menu.addSeparator()
            self.menu.addAction("退出 AstraPod", self.quit)
            self.tray.setContextMenu(self.menu)
            self.tray.activated.connect(self.activate_tray)
            self.tray.setIcon(make_tray_icon())
            self.tray.show()
            app.setQuitOnLastWindowClosed(False)
        self.start_remote()
        if start_plugins and os.environ.get("ASTRAPOD_PLUGIN_HOST", "on") != "off":
            self.start_plugins()

    def show_window(self) -> None:
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()

    def show_page(self, key: str) -> None:
        self.window.navigate(key)
        self.show_window()

    def activate_tray(self, reason) -> None:
        if reason == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger:
            self.show_window()

    # HerdRemote child -----------------------------------------------------
    def remote_running(self) -> bool:
        return self.remote.state() == QtCore.QProcess.ProcessState.Running

    def start_remote(self) -> None:
        if not self.stopping and self.remote.state() == QtCore.QProcess.ProcessState.NotRunning:
            self.remote.start()

    def schedule_remote(self, *_args) -> None:
        if not self.stopping:
            self.retry.start(2000)

    def reload_remote(self) -> None:
        if self.remote.state() == QtCore.QProcess.ProcessState.Running:
            self.remote.terminate()
        else:
            self.schedule_remote()

    def _remote_output(self) -> None:
        data = bytes(self.remote.readAllStandardOutput())
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.flush()
        except (OSError, ValueError, AttributeError):
            pass
        self._partial += data
        *lines, self._partial = self._partial.split(b"\n")
        logs = self.window.pages.get("logs")
        for line in lines:
            if logs is not None and line.strip():
                logs.append(line.decode("utf-8", "replace"))

    # Plugin host -----------------------------------------------------------
    def start_plugins(self) -> None:
        if self.stopping or not self.plugins.available:
            return
        try:
            self.plugins.start()
        except (OSError, RuntimeError, ValueError) as error:
            print(f"astrapod: plugin host failed to start: {error}", file=sys.stderr)
            self.plugin_retry.start(10_000)

    # Quit --------------------------------------------------------------------
    def quit(self) -> None:
        if self.window.busy:
            self.show_window()
            self.window.flash("正在处理，完成后才能退出。")
        else:
            self.app.quit()

    def stop(self) -> None:
        self.stopping = True
        self.retry.stop()
        self.plugin_retry.stop()
        try:
            self.plugins.stop()
        except (OSError, RuntimeError, ValueError):
            pass
        if self.remote.state() != QtCore.QProcess.ProcessState.NotRunning:
            self.remote.terminate()
            if not self.remote.waitForFinished(5500):
                self.remote.kill()
                self.remote.waitForFinished(1500)
