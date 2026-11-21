"""Entry point: `podlink_gui.py [--tray] [--demo]`."""
from __future__ import annotations

import sys

from .qt import FONT_UI, QtGui, QtWidgets, ThemeManager
from .window import MainWindow, Services


def real_services() -> Services:
    from .backends import DaemonClient, DeviceLibrary, FileProber, PluginBridge, SyncBackend
    return Services(library=DeviceLibrary(), daemon=DaemonClient(), plugins=PluginBridge.create(),
                    sync=SyncBackend(), prober=FileProber())


def demo_services(count: int = 0, delay: bool = True, appearance=None) -> Services:
    """Fakes for everything but the desktop's appearance, which the demo
    reads from the real system unless one is passed."""
    from .backends import PluginBridge
    from .fakes import FakeDaemon, FakeLibrary, FakePluginHost, FakeProber, FakeSync
    host = FakePluginHost()
    host.start()
    return Services(library=FakeLibrary(count, delay=delay), daemon=FakeDaemon(),
                    plugins=PluginBridge(host), sync=FakeSync(), prober=FakeProber(),
                    appearance=appearance)


def create_application(argv=None, services: Services | None = None):
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("AstraPod")
    app.setApplicationDisplayName("AstraPod")
    app.setOrganizationName("PodBox")
    app.setDesktopFileName("astrapod")
    font = QtGui.QFont()
    font.setFamilies(list(FONT_UI))
    font.setPixelSize(13)
    app.setFont(font)
    services = services or real_services()
    theme_manager = ThemeManager(app, appearance=services.appearance)
    theme_manager.apply(theme_manager.mode, persist=False)
    window = MainWindow(theme_manager, services)
    return app, theme_manager, window


def main(argv=None) -> int:
    args = list(sys.argv if argv is None else argv)
    tray_only = "--tray" in args
    demo = "--demo" in args
    qt_args = [a for a in args if a not in ("--tray", "--demo")]
    services = demo_services() if demo else None
    app, _theme, window = create_application(qt_args, services)
    window.daemon.start()
    if demo:
        window.services.plugins.running = True
        window.library_state.refresh(True)
    else:
        from .runtime import DesktopRuntime
        DesktopRuntime(app, window)
    if not tray_only or window.runtime is None or window.runtime.tray is None:
        window.show()
    return int(app.exec())
