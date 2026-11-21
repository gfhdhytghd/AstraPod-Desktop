import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1]))
sys.path.insert(0, str(Path(__file__).parents[2]))
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import hrdisk
import podlink_disk


def test_capability_authorizes_one_send_and_reset():
    control = hrdisk.Control()
    send = Mock()
    assert not control.hello(b'DH\t1\told', send)
    assert control.token is None
    assert control.hello(hrdisk.CAPABILITY, send)
    token = control.token
    assert len(token) == 32
    send.assert_called_once_with(b'DA\t1\t' + token)
    control.hello(hrdisk.CAPABILITY, send)
    assert send.call_count == 1
    control.reset()
    assert control.token is None


def test_private_socket_gate_request_cli(tmp_path):
    tmp_path.chmod(0o700)
    control = hrdisk.Control(str(tmp_path / 'disk.sock'))
    control.start()
    link = SimpleNamespace(opened=True, send=Mock())
    def call():
        return podlink_disk.enter(control.path, .15, lambda: False)
    def run():
        worker = threading.Thread(target=lambda: result.append(call()))
        worker.start()
        while worker.is_alive():
            control.poll(link)
            time.sleep(.001)
        worker.join()
    try:
        result = []
        run()
        assert result.pop() == 4
        link.send.assert_not_called()
        control.hello(hrdisk.CAPABILITY, Mock())
        token = control.token
        run()
        assert result.pop() == 2
        assert link.send.call_args.args[1] == b'DK\t1\t' + token
        assert control.token is None
        link.opened = False
        run()
        assert result.pop() == 3
    finally:
        control.close()


def test_cli_already_present_and_missing_daemon(tmp_path):
    assert podlink_disk.enter(str(tmp_path/'absent'), .01, lambda: True) == 0
    assert podlink_disk.enter(str(tmp_path/'absent'), .01, lambda: False) == 3


def test_cli_waits_for_volume(tmp_path):
    path = str(tmp_path/'socket')
    server = socket.socket(socket.AF_UNIX)
    server.bind(path)
    server.listen()
    def serve():
        client, _ = server.accept()
        with client:
            assert client.recv(64) == b'enter-disk-v1\n'
            client.sendall(b'0 disk request sent\n')
    thread = threading.Thread(target=serve)
    thread.start()
    calls = []
    def present():
        calls.append(1)
        return len(calls) >= 3
    try:
        assert podlink_disk.enter(path, 1, present) == 0
    finally:
        thread.join()
        server.close()


def test_serial_lease_releases_on_disconnect_and_refuses_second_owner(tmp_path):
    tmp_path.chmod(0o700)
    relinquish = Mock()
    control = hrdisk.Control(str(tmp_path / 'disk.sock'), release_serial=relinquish)
    control.start()
    try:
        with socket.socket(socket.AF_UNIX) as first, socket.socket(socket.AF_UNIX) as second:
            first.settimeout(1)
            first.connect(control.path)
            first.sendall(hrdisk.BORROW_SERIAL)
            control.poll(None)
            assert first.recv(64) == b'0 serial released\n'
            assert control.serial_lease is not None

            second.settimeout(1)
            second.connect(control.path)
            second.sendall(hrdisk.BORROW_SERIAL)
            control.poll(None)
            assert second.recv(64) == b'5 serial already borrowed\n'
            relinquish.assert_called_once()
        control.poll(None)
        assert control.serial_lease is None
    finally:
        control.close()
