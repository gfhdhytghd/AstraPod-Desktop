import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import subprocess
import pytest

spec = importlib.util.spec_from_file_location('disk_mode', Path(__file__).parents[1]/'disk_mode.py')
disk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(disk)


def test_unmount_then_eject():
    run = Mock(side_effect=[SimpleNamespace(returncode=0, stderr=''),
                           SimpleNamespace(returncode=0, stdout="(<objectpath '/org/freedesktop/UDisks2/drives/Apple_iPod_123'>,)\n"),
                           SimpleNamespace(returncode=0, stderr='')])
    assert disk.clean(True, lambda: '/dev/fake1', run)
    commands = [c.args[0] for c in run.call_args_list]
    assert commands[0] == ['udisksctl','unmount','-b','/dev/fake1']
    assert commands[1] == ['gdbus', 'call', '--system', '--dest', disk.UDISKS,
                           '--object-path', '/org/freedesktop/UDisks2/block_devices/fake1',
                           '--method', 'org.freedesktop.DBus.Properties.Get', '--timeout', '2',
                           disk.UDISKS + '.Block', 'Drive']
    assert commands[2] == ['gdbus', 'call', '--system', '--dest', disk.UDISKS,
                           '--object-path', '/org/freedesktop/UDisks2/drives/Apple_iPod_123',
                           '--method', disk.UDISKS + '.Drive.Eject', '--timeout', '12',
                           "{'auth.no_user_interaction': <true>}"]
    assert [c.kwargs['timeout'] for c in run.call_args_list] == [2, 3, 13]


@pytest.mark.parametrize('response', [
    SimpleNamespace(returncode=1, stderr='NotAuthorized'),
    SimpleNamespace(returncode=0, stdout="(<objectpath '/'>,)"),
    SimpleNamespace(returncode=0, stdout="(<objectpath '/org/freedesktop/UDisks2/drives/a'>,) garbage"),
    SimpleNamespace(returncode=0, stdout="(<objectpath '/wrong/drives/a'>,)"),
    FileNotFoundError('gdbus'), subprocess.TimeoutExpired('gdbus', 3),
])
def test_drive_lookup_failure_never_ejects(response):
    run = Mock(side_effect=[SimpleNamespace(returncode=0, stderr=''), response])
    assert not disk.clean(True, lambda: '/dev/fake1', run)
    assert run.call_count == 2


@pytest.mark.parametrize('response', [
    SimpleNamespace(returncode=1, stderr='NotAuthorized: inactive session'),
    SimpleNamespace(returncode=1, stderr='DeviceBusy'),
    subprocess.TimeoutExpired('gdbus', 13), OSError('bus unavailable'),
])
def test_eject_failure_has_no_fallback(response):
    run = Mock(side_effect=[SimpleNamespace(returncode=1, stderr='NotMounted'),
                           SimpleNamespace(returncode=0, stdout="(<objectpath '/org/freedesktop/UDisks2/drives/a'>,)"),
                           response])
    assert not disk.clean(True, lambda: '/dev/fake1', run)
    assert run.call_count == 3


def test_object_component_escaping():
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="(<objectpath '/'>,)"))
    assert not disk.eject_drive('/dev/dm-0', run)
    assert '/org/freedesktop/UDisks2/block_devices/dm_2d0' in run.call_args.args[0]


def test_busy_never_force_or_ack():
    run = Mock(return_value=SimpleNamespace(returncode=1, stderr='device busy'))
    assert not disk.clean(True, lambda: '/dev/fake1', run)
    assert run.call_count == 1
    assert not disk.clean(False, lambda: None, run)
    assert run.call_count == 1


def test_body_only_unmount_and_notmounted():
    run = Mock(return_value=SimpleNamespace(returncode=1, stderr='org.freedesktop.UDisks2.Error.NotMounted'))
    assert disk.clean(False, lambda: '/dev/fake1', run)
    assert run.call_count == 1


def test_remote_long_menu_short_unchanged(monkeypatch):
    remote = Mock()
    mode = disk.DiskMode(remote)
    mode.submit = Mock()
    mode.active = True
    try:
        mode.frame(b'\xee\x87\x02\x00', 0)
        for i in range(1, 10):
            mode.frame(b'', i*.1)
        mode.submit.assert_not_called()
        mode.frame(b'', 1.0)
        mode.submit.assert_called_once_with(True, None)
        mode.frame(b'', 1.1)
        assert mode.submit.call_count == 1
        mode.frame(b'', 1.5)  # silence broke the hold
        assert not mode.down
    finally:
        mode.close()


def test_remote_normal_mode_no_eject():
    mode = disk.DiskMode(Mock())
    mode.submit = Mock()
    try:
        mode.frame(b'\xee\x87\x03\x00', 0)
        for i in range(1, 20):
            mode.frame(b'', i*.1)
        mode.submit.assert_not_called()
    finally:
        mode.close()


def test_host_flush_success_only_acknowledges_matching_notification():
    remote = Mock()
    request = 1234
    remote.disk_status.return_value = bytes([1, 1, 1, 0]) + request.to_bytes(4, 'little')
    mode = disk.DiskMode(remote)
    done = Mock()
    done.done.return_value = True
    done.result.return_value = True
    mode.close()
    mode = disk.DiskMode(remote)
    mode.job, mode.request = done, request
    mode.seen = request
    try:
        mode.poll(1)
        remote.disk_ack.assert_called_once_with(request)
        done.result.return_value = False
        mode.job, mode.request = done, request + 1
        mode.poll(1.01)
        remote.disk_ack.assert_called_once()
    finally:
        mode.close()
