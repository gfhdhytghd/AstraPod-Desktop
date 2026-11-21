"""Offline tests for the PodLink Shell recording control requests."""

import struct

import pytest

import podlink_shell as shell


class Player:
    bus = 1
    address = 2

    def __init__(self, revision):
        self.revision = revision
        self.state = 0
        self.path = b""
        self.commands = []

    def ctrl_transfer(self, direction, request, value, index, length, *, timeout):
        assert (request, index, timeout) == (0x5D, 0x4951, 1000)
        if direction == 0xC0 and value == 0:
            assert length == 16
            count = len(self.commands)
            return struct.pack("<4I", 0, count, count, 0)
        if direction == 0xC0 and value == 1:
            assert length == 96
            return struct.pack("<II88s", self.revision, self.state, self.path)
        assert direction == 0x40 and length == b""
        self.commands.append(value)
        if value == 7 and not self.revision:
            self.state = 4
        elif value == 8:
            self.state = 2
            self.path = b"/Recordings/PodLink-0001.wav"
        else:
            assert value in (6, 7)
            self.state = 1


@pytest.mark.parametrize(("source", "command"), [("line-in", 6), ("mic", 7)])
def test_record_start_stop_fetch(monkeypatch, capsys, source, command):
    player = Player(1)
    monkeypatch.setattr(shell, "find_player", lambda serial: player)
    monkeypatch.setattr(shell.time, "sleep", lambda seconds: None)
    calls = []
    monkeypatch.setattr(shell, "record_fetch", lambda *args: calls.append(args))

    shell.execute("serial", "record-info")
    assert "hw_revision=1 inputs=line-in,mic" in capsys.readouterr().out
    shell.execute("serial", "record-start", source)
    with pytest.raises(RuntimeError, match="Stop recording"):
        shell.execute("serial", "record-fetch", "capture.wav")
    shell.execute("serial", "record-stop")
    shell.execute("serial", "record-fetch", "capture.wav")

    assert player.commands == [command, 8]
    assert calls == [("serial", player.path.decode(), "capture.wav")]


def test_record_start_reports_unavailable_mic(monkeypatch):
    player = Player(0)
    monkeypatch.setattr(shell, "find_player", lambda serial: player)
    monkeypatch.setattr(shell.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="input unsupported"):
        shell.execute("serial", "record-start", "mic")
