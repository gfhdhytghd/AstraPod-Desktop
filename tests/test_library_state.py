"""Stock library refresh follows attachment state without retrying a lost mount."""
from contextlib import nullcontext
from pathlib import Path
import sys
from unittest.mock import Mock

from astrapod_desktop.jobs import JobRunner
from astrapod_desktop.library import LibraryState
from astrapod_desktop.qt import QtWidgets

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'herdremote'))
from hrstockdevice import DeviceState, StockDeviceDisconnected


def test_stock_unplug_is_quiet_until_reattachment(capsys):
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(["test"])
    runner = JobRunner()
    runner.inline = True
    device = DeviceState("stock", serial="APPLE")
    session = Mock()
    session.identity = "APPLE"
    summary = Mock()
    session.snapshot.return_value = summary, ["track"], (), Mock()
    backend = Mock(device=device)
    opening = Mock(side_effect=[
        StockDeviceDisconnected("The selected iPod is no longer connected"),
        nullcontext(session),
    ])
    backend.session_for_device.return_value = opening
    state = LibraryState(backend, runner, Mock())
    state.source = "device"
    failed = Mock()
    state.failed.connect(failed)

    state.refresh()
    app.processEvents()
    assert not state.loading and not state.error and state.source == "cache"
    for _ in range(3):
        state.refresh()
    backend.device = DeviceState("none")
    state.device_changed(backend.device)
    state.refresh(True)
    assert opening.call_count == 1
    assert not capsys.readouterr().err
    failed.assert_not_called()

    backend.device = device
    state.device_changed(device)
    state.refresh()
    app.processEvents()
    assert opening.call_count == 2
    assert state.source == "device" and state.tracks == ["track"]
