"""Offline tests for the EP0 plugin and framebuffer commands."""
import errno
import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import zlib

import pytest

import plugin_smoke as smoke
import podlink_shell as shell


class Player:
    bus = 1
    address = 2
    chunk = 256
    pixels = struct.pack("<4H", 0xF800, 0x07E0, 0x001F, 0xFFFF) + bytes(153592)

    def __init__(self):
        self.generation = 0
        self.name = b""
        self.state = self.reason = self.result = 0
        self.capture = 0
        self.pending = False
        self.audio = 0
        self.commands = []
        self.keys = []
        self.exit_presses = 0

    def ctrl_transfer(self, direction, request, value, index, payload, *, timeout):
        assert timeout == 1000
        if request == 0x5B:
            assert direction == 0x40 and payload == b""
            assert self.state == 2
            self.keys.append((value, index))
            return 0
        assert (request, index) == (0x5D, 0x4951)
        if direction == 0x40:
            self.commands.append(value)
            if value == 9:
                self.generation += 1
                self.name = payload
                self.state, self.reason, self.result = 2, 0, 0
                self.exit_presses = 0
            elif value == 10:
                self.exit_presses += 1
                if self.name != b"rb_info" or self.exit_presses == 2:
                    self.state, self.reason = 0, 1
                else:
                    assert self.state == 2
            elif value == 11:
                self.capture += 1
                self.pending = True
            elif value in (2, 4):
                assert self.state == 0
                self.audio = int(value == 2)
            else:
                pytest.fail(f"Unexpected command {value}")
            return len(payload)
        assert direction == 0xC0
        if value == 0:
            assert payload == 16
            completed = len(self.commands)
            return struct.pack("<4I", 0, completed, completed, 0)
        if value == 3:
            assert payload == 52
            return struct.pack("<IIIIi32s", 1, self.generation, self.state, self.reason,
                               self.result, self.name)
        if value == 4:
            assert payload == 16
            state = (1 if self.pending else 2) if self.capture else 0
            self.pending = False
            return struct.pack("<IIHHHH", self.capture, 153600, 320, 240, self.chunk, state)
        if value == 5:
            assert payload == 4
            return struct.pack("<I", self.audio)
        start = (value - 0x100) * self.chunk
        data = self.pixels[start:start + self.chunk]
        assert len(data) == payload
        return data


def test_find_player_skips_unreadable_usb_candidates(monkeypatch):
    class Unreadable:
        idVendor = 0x05AC

        def __init__(self, product, error):
            self.idProduct = product
            self.error = error
            self.serial_reads = 0

        @property
        def serial_number(self):
            self.serial_reads += 1
            raise self.error

    trackpad = Unreadable(0x0324, ValueError("The device has no langid"))
    bad_player = Unreadable(0x1261, shell.usb.core.USBError("Access denied", errno=errno.EACCES))
    player = SimpleNamespace(idVendor=0x05AC, idProduct=0x1261, serial_number="target")
    monkeypatch.setattr(shell.usb.core, "find", lambda **kwargs: iter((trackpad, bad_player, player)))

    assert shell.find_player("target") is player
    assert trackpad.serial_reads == 0
    assert bad_player.serial_reads == 1


def test_plugin_launch_retries_stall_after_previous_exit(monkeypatch):
    player = Player()
    monkeypatch.setattr(shell.time, "sleep", lambda _: None)
    snow = shell.plugin_launch(player, "snow")
    shell.plugin_exit(player)
    assert shell.plugin_status(player).generation == snow.generation
    transfer = player.ctrl_transfer
    attempts = []

    def busy(direction, request, value, index, data, **kwargs):
        if direction == 0x40 and value == 9:
            attempts.append(data)
            if len(attempts) == 1:
                raise shell.usb.core.USBError("Pipe error", errno=errno.EPIPE)
        return transfer(direction, request, value, index, data, **kwargs)

    monkeypatch.setattr(player, "ctrl_transfer", busy)
    stopwatch = shell.plugin_launch(player, "stopwatch")
    assert stopwatch.generation == snow.generation + 1
    assert attempts == [b"stopwatch", b"stopwatch"]
    assert player.commands == [9, 10, 9]


@pytest.mark.parametrize(("code", "attempt_count"),
                         [(errno.EPIPE, len(shell.EP0_STALL_DELAYS) + 1), (errno.ENODEV, 1)])
def test_ep0_retries_are_bounded_and_stall_only(monkeypatch, code, attempt_count):
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(args)
        raise shell.usb.core.USBError("Control transfer failed", errno=code)

    monkeypatch.setattr(shell.time, "sleep", lambda _: None)
    player = SimpleNamespace(ctrl_transfer=fail)
    with pytest.raises(shell.usb.core.USBError) as error:
        shell.plugin_exit(player)
    assert error.value.errno == code
    assert attempts == [(0x40, 0x5D, 10, 0x4951, b"")] * attempt_count


def test_missing_track_stops_before_first_plugin(tmp_path, monkeypatch):
    player = Player()
    player.audio = 1  # A different track was playing before the smoke test.
    transfer = player.ctrl_transfer

    def missing(direction, request, value, index, data, **kwargs):
        if direction == 0x40 and value == 2:
            player.commands.append(value)
            return 0  # Firmware acknowledges play even when the fixture is absent.
        return transfer(direction, request, value, index, data, **kwargs)

    monkeypatch.setattr(player, "ctrl_transfer", missing)
    monkeypatch.setattr(shell, "find_player", lambda _: player)
    monkeypatch.setattr(shell.time, "sleep", lambda _: None)
    monkeypatch.setattr(smoke, "time", SimpleNamespace(
        monotonic=iter(i / 20 for i in range(10000)).__next__, sleep=lambda _: None,
    ))
    monkeypatch.setattr(smoke, "usb_takeover", lambda *args: pytest.fail("Unexpected USB takeover"))

    out = tmp_path / "evidence"
    with pytest.raises(RuntimeError, match="irq-dma44.flac"):
        smoke.run("test", out, None)
    assert player.commands[:2] == [4, 2]
    assert 9 not in player.commands
    assert sorted(p.name for p in out.iterdir()) == ["result.json"]


def test_cli_launch_status_and_exit(monkeypatch, capsys):
    player = Player()
    monkeypatch.setattr(shell, "find_player", lambda serial: player)
    shell.execute("test", "plugin-launch", "stopwatch")
    assert "plugin=stopwatch state=running" in capsys.readouterr().out
    shell.execute("test", "plugin-exit")
    shell.execute("test", "plugin-status")
    assert "state=idle reason=normal result=0 generation=1" in capsys.readouterr().out
    with pytest.raises(ValueError, match="basename"):
        shell.execute("test", "plugin-launch", "../stopwatch")
    player.reason, player.result = 3, -2
    assert shell.plugin_status(player).result == -2
    assert player.commands == [9, 10]


def test_screenshot_png_channels_and_layout(tmp_path, monkeypatch):
    player = Player()
    player.chunk = 224  # Exercise the final short chunk.
    monkeypatch.setattr(shell.time, "sleep", lambda _: None)
    destination = tmp_path / "frame.png"
    shell.screenshot(player, destination)
    png = destination.read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack_from(">II", png, 16) == (320, 240)
    n, kind = struct.unpack_from(">I4s", png, 33)
    assert kind == b"IDAT"
    pixels = zlib.decompress(png[41:41 + n])
    assert len(pixels) == 240 * (1 + 320 * 3)
    assert pixels[:13] == bytes.fromhex("00 ff0000 00ff00 0000ff ffffff")
    assert player.commands == [11]


@pytest.mark.parametrize("failure", ("short", "stale"))
def test_failed_capture_never_writes_png(tmp_path, monkeypatch, failure):
    player = Player()
    original = player.ctrl_transfer

    def control(direction, request, value, index, payload, **kwargs):
        reply = original(direction, request, value, index, payload, **kwargs)
        if direction == 0xC0 and value == 0x100:
            if failure == "short":
                return reply[:-1]
            player.capture += 1
        return reply

    monkeypatch.setattr(player, "ctrl_transfer", control)
    monkeypatch.setattr(shell.time, "sleep", lambda _: None)
    destination = tmp_path / "frame.png"
    with pytest.raises(RuntimeError, match="Short|changed"):
        shell.screenshot(player, destination)
    assert not destination.exists()


def test_smoke_default_cases_and_playback(tmp_path, monkeypatch):
    player = Player()
    monkeypatch.setattr(shell, "find_player", lambda serial: player)
    monkeypatch.setattr(shell.time, "sleep", lambda _: None)
    monkeypatch.setattr(smoke, "time", type("Clock", (), {
        "monotonic": staticmethod(iter(i / 10 for i in range(10000)).__next__),
        "sleep": staticmethod(lambda seconds: None),
    }))

    def capture(_player, output):
        output.write_text(str(len(player.keys)))
        if player.state == 2 and player.name == b"oscilloscope":
            assert player.audio == 1

    def usb_case(serial, current, output, socket):
        assert (serial, current, socket) == ("test", player, "fake-disk.sock")
        assert player.state == player.audio == 0
        return {"state": "idle", "reason": "usb"}

    monkeypatch.setattr(shell, "screenshot", capture)
    monkeypatch.setattr(smoke, "usb_takeover", usb_case)
    out = tmp_path / "evidence"
    smoke.run("test", out, "fake-disk.sock")
    result = json.loads((out / "result.json").read_text())
    assert list(result) == [case[0] for case in smoke.CASES] + ["usb-takeover"]
    assert result["oscilloscope"]["playback"] is True
    assert result["usb-takeover"]["reason"] == "usb"
    for name, keys, _ in smoke.CASES:
        assert (out / f"{name}-before.png").is_file()
        assert (out / f"{name}-after.png").is_file()
        assert result[name]["exit"]["reason"] == "normal"
        assert keys
    assert player.commands.count(9) == 7
    assert player.commands.count(10) == 8
    assert player.commands[:3] == [4, 2, 9]
    assert player.commands.count(4) == 2
    assert player.keys[::2] == [(key, 1) for _, keys, _ in smoke.CASES for key in keys]
    assert player.keys[1::2] == [(key, 0) for _, keys, _ in smoke.CASES for key in keys]


def test_usb_takeover_requires_same_player_and_usb_exit(tmp_path, monkeypatch):
    from serial.tools import list_ports

    player = Player()
    serial = "2AABB"
    device = Path("/sys/player"), serial
    order = []

    def eject():
        order.append("eject")
        player.state, player.reason = 0, 2

    def enter_disk(tty, identity, socket):
        assert (tty, identity, socket) == (Path("/dev/ttyACM1"), device, "disk.sock")
        assert player.name == b"snow" and player.state == 2
        order.append("disk")
        return SimpleNamespace(eject=eject)

    def capture(current, output, label):
        assert current is player and output == tmp_path
        order.append(label)

    monkeypatch.setattr(list_ports, "comports", lambda: [
        SimpleNamespace(vid=0x05AC, pid=0x1261, device="/dev/ttyACM0", serial_number="2OTHER"),
        SimpleNamespace(vid=0x05AC, pid=0x1261, device="/dev/ttyACM1", serial_number=serial),
    ])
    monkeypatch.setitem(sys.modules, "hrdisk", SimpleNamespace(default_socket=lambda: "disk.sock"))
    monkeypatch.setitem(sys.modules, "hrdisksync", SimpleNamespace(
        selected_player=lambda tty: (Path(tty), device), enter_disk=enter_disk
    ))
    monkeypatch.setattr(smoke, "capture", capture)
    monkeypatch.setattr(shell, "find_player", lambda presented: player if presented == serial else None)
    monkeypatch.setattr(shell.time, "sleep", lambda _: None)
    result = smoke.usb_takeover(serial, player, tmp_path, None)
    assert result["reason"] == "usb"
    assert order == ["usb-before", "disk", "eject", "usb-returned"]
