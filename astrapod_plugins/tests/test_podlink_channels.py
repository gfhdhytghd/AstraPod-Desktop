"""Exercise APL1 through the resident bridge and a real plugin subprocess."""
from collections import deque
from pathlib import Path
import json
import socket
import sys
import tempfile
import time
import zlib

import pytest

from astrapod_plugins.host import PluginHost
from astrapod_plugins.manifest import load_manifest
from astrapod_plugins.podlink_files import PodLinkFiles
from astrapod_plugins.podlink_wire import (
    END, MAX_CREDIT, MAX_DATA, MAX_LOCAL, MSF1, U16, U32,
    Op, Packet, Status, decode_local, encode_local, registration,
)
import podlink

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "herdremote"))
from hrpluginlink import PluginLink


NAME = b"astrapod.echo"
CHANNEL = 1
CHILD = Path(__file__).resolve().parents[1] / "podlink-echo"


class Harness:
    def __init__(self, bridge):
        self.bridge = bridge
        self.decoder = podlink.Decoder()
        self.from_host = deque()
        self.seq = 1

    def send(self, kind, payload):
        raw = podlink.encode(kind, self.seq, payload)
        self.seq += 1
        decoded, = self.decoder.feed(raw)
        self.from_host.append(Packet.decode(decoded.payload))

    def best_effort_delay(self):
        return .01

    def device(self, packet):
        raw = podlink.encode(podlink.BINARY, self.seq, packet.encode())
        self.seq += 1
        decoder = podlink.Decoder()
        assert not decoder.feed(raw[:7])
        assert not decoder.feed(raw[7:20])
        frame, = decoder.feed(raw[20:])
        assert self.bridge.receive(frame)

    def spin(self, test, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.bridge.poll(self)
            if test():
                return
            time.sleep(.01)
        pytest.fail("deadline while pumping plugin channel")

    def expect(self, op, transfer=0, *, channel=CHANNEL):
        self.spin(lambda: self.from_host)
        packet = self.from_host.popleft()
        assert (packet.op, packet.channel, packet.transfer) == (op, channel, transfer), packet
        return packet.data


def test_packet_layout_and_manifest(tmp_path):
    packet = Packet(Op.PUSH, 0x12345678, 0x87654321, U32.pack(7) + b"data.bin")
    assert packet.encode() == bytes.fromhex(
        "41504c31 06000000 78563412 21436587 07000000 646174612e62696e"
    )
    assert Packet.decode(packet.encode()) == packet
    with pytest.raises(ValueError):
        Packet(Op.PULL, 1, 2, b"../data.bin")
    with pytest.raises(ValueError):
        Packet(Op.TEXT, 1, data=b"\xff")
    with pytest.raises(ValueError):
        Packet(Op.CHUNK, 1, 2, MSF1 + U32.pack(0) + bytes(MAX_DATA + 1))
    modified = bytearray(packet.encode())
    modified[6] = 1
    with pytest.raises(ValueError):
        Packet.decode(modified)

    raw = json.loads((CHILD / "plugin.json").read_text())
    directory = tmp_path / "podlink-echo"
    directory.mkdir()
    path = directory / "plugin.json"
    (directory / "plugin.py").touch()
    path.write_text(json.dumps(raw))
    manifest = load_manifest(path)
    assert manifest.kind == "podlink"
    assert manifest.device_channels == ("astrapod.echo",)
    for channels in ([], ["UPPER"], ["a", "a"], ["a" * 33], ["x/y"], [9]):
        raw["device_channels"] = channels
        path.write_text(json.dumps(raw))
        with pytest.raises(ValueError, match="device_channels"):
            load_manifest(path)


def test_file_crc_cancel_timeout_and_symlink(tmp_path):
    now = [0]
    files = PodLinkFiles(tmp_path, clock=lambda: now[0])
    target = tmp_path / "file.bin"
    target.write_bytes(b"prior")
    push = Packet(Op.PUSH, 1, 1, U32.pack(2) + b"file.bin")
    assert [p.op for p in files.handle(1, push)] == [Op.ACCEPT, Op.CREDIT]
    assert files.handle(1, Packet(Op.CHUNK, 1, 1, MSF1 + U32.pack(0) + b"AB")) == ()
    failed, = files.handle(1, Packet(Op.FINISH, 1, 1, END.pack(2, 0)))
    assert failed == Packet.stop(1, Status.BAD_FRAME, 1)
    assert target.read_bytes() == b"prior"
    assert not list(tmp_path.glob(".apl1-*"))

    files.handle(1, push)
    assert files.handle(1, Packet.stop(1, Status.CANCELLED, 1)) == ()
    assert not list(tmp_path.glob(".apl1-*"))

    files.handle(1, push)
    assert files.handle(2, Packet(Op.CHUNK, 1, 1, MSF1 + U32.pack(0) + b"AB")) == ()
    now[0] = 11
    assert files.expire() == (1, Packet.stop(1, Status.TIMEOUT, 1))
    assert not list(tmp_path.glob(".apl1-*"))

    files.handle(1, push)
    result, = files.handle(1, Packet(Op.CHUNK, 1, 1, MSF1 + U32.pack(1) + b"A"))
    assert result == Packet.stop(1, Status.BAD_FRAME, 1)
    assert target.read_bytes() == b"prior"
    assert not list(tmp_path.glob(".apl1-*"))

    files.handle(1, Packet(Op.PUSH, 1, 3, U32.pack(0) + b"empty.bin"))
    assert files.handle(1, Packet(Op.FINISH, 1, 3, END.pack(0, 0))) == (
        Packet(Op.DONE, 1, 3),)
    assert (tmp_path / "empty.bin").read_bytes() == b""

    files.handle(1, Packet(Op.PULL, 1, 4, b"empty.bin"))
    assert files.handle(1, Packet(Op.CREDIT, 1, 4, b"\x01")) == (
        Packet(Op.FINISH, 1, 4, END.pack(0, 0)),)
    now[0] += 11
    assert files.handle(1, Packet(Op.CREDIT, 1, 4, b"\x01")) == ()
    assert files.expire() == (1, Packet.stop(1, Status.TIMEOUT, 4))

    (tmp_path / "alias").symlink_to(target)
    reply, = files.handle(1, Packet(Op.PULL, 1, 2, b"alias"))
    assert reply == Packet.stop(1, Status.IO, 2)


@pytest.fixture()
def bridged(tmp_path, monkeypatch):
    with tempfile.TemporaryDirectory(prefix="apl1-") as run:
        path = Path(run)
        bridge = PluginLink(path / "plugin.sock")
        bridge.start()
        monkeypatch.setenv("XDG_RUNTIME_DIR", run)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
        directory = tmp_path / "transfer"
        directory.mkdir()
        host = PluginHost(tmp_path / "config.json", user_dir=tmp_path / "unused",
                          podlink_socket=bridge.path)
        host.set_settings("podlink-echo", {"directory": str(directory)})
        host.enable("podlink-echo", True)
        host.start()
        harness = Harness(bridge)
        try:
            harness.spin(lambda: any("astrapod.echo" in names for names in bridge.peers.values()))
            yield harness, directory
        finally:
            host.stop()
            bridge.close()


def test_echo_file_round_trip(bridged):
    harness, directory = bridged
    harness.device(Packet(Op.OPEN, CHANNEL, data=NAME))
    harness.expect(Op.OPEN_OK)
    message = "AstraPod 回显".encode()
    binary = bytes(range(256)) * 8
    for op, payload in ((Op.TEXT, message), (Op.BINARY, binary)):
        harness.device(Packet(op, CHANNEL, data=payload))
        assert harness.expect(op) == payload
    file_data = bytes(i % 251 for i in range(5037))
    size = U32.pack(len(file_data))
    crc = zlib.crc32(file_data)
    harness.device(Packet(Op.PUSH, CHANNEL, 1, size + b"roundtrip.bin"))
    assert harness.expect(Op.ACCEPT, 1) == size
    credits = harness.expect(Op.CREDIT, 1)[0]
    offset = 0
    while offset < len(file_data):
        assert credits > 0
        data = file_data[offset:offset + MAX_DATA]
        harness.device(Packet(Op.CHUNK, CHANNEL, 1, MSF1 + U32.pack(offset) + data))
        offset += len(data)
        credits -= 1
        if offset < len(file_data):
            credits += harness.expect(Op.CREDIT, 1)[0]
    harness.device(Packet(Op.FINISH, CHANNEL, 1, END.pack(len(file_data), crc)))
    harness.expect(Op.DONE, 1)
    assert (directory / "roundtrip.bin").read_bytes() == file_data

    harness.device(Packet(Op.PULL, CHANNEL, 2, b"roundtrip.bin"))
    assert harness.expect(Op.ACCEPT, 2) == size
    pulled = bytearray()
    while len(pulled) < len(file_data):
        harness.device(Packet(Op.CREDIT, CHANNEL, 2, bytes([MAX_CREDIT])))
        for _ in range(MAX_CREDIT):
            block = harness.expect(Op.CHUNK, 2)
            assert block[:8] == MSF1 + U32.pack(len(pulled))
            pulled.extend(block[8:])
            if len(pulled) == len(file_data):
                break
    assert harness.expect(Op.FINISH, 2) == END.pack(len(file_data), crc)
    harness.device(Packet(Op.DONE, CHANNEL, 2))
    assert pulled == file_data


def test_link_loss_aborts_and_reopens(bridged):
    harness, directory = bridged
    result = directory / "prior.bin"
    result.write_bytes(b"prior")
    harness.device(Packet(Op.OPEN, CHANNEL, data=NAME))
    harness.expect(Op.OPEN_OK)
    harness.device(Packet(Op.PUSH, CHANNEL, 1, U32.pack(5) + b"prior.bin"))
    harness.expect(Op.ACCEPT, 1)
    harness.expect(Op.CREDIT, 1)
    harness.device(Packet(Op.CHUNK, CHANNEL, 1, MSF1 + U32.pack(0) + b"a"))
    harness.expect(Op.CREDIT, 1)
    assert list(directory.glob(".apl1-*"))
    harness.bridge.link_lost()
    harness.spin(lambda: not list(directory.glob(".apl1-*")))
    assert result.read_bytes() == b"prior"
    harness.device(Packet(Op.OPEN, CHANNEL, data=NAME))
    harness.expect(Op.OPEN_OK)
    harness.device(Packet(Op.BINARY, CHANNEL, data=b"new session"))
    assert harness.expect(Op.BINARY) == b"new session"


def test_unmatched_duplicate_and_stale_tokens():
    with tempfile.TemporaryDirectory(prefix="apl1-") as run:
        bridge = PluginLink(Path(run) / "plugin.sock")
        bridge.start()
        harness = Harness(bridge)
        try:
            assert not bridge.receive(podlink.Frame(podlink.BINARY, 1, b"MSF1\0\0\0\0data"))
            harness.device(Packet(Op.OPEN, CHANNEL, data=NAME))
            assert harness.expect(Op.CLOSE) == U16.pack(Status.NO_HANDLER)
            with (socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as first,
                  socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as second):
                for peer in (first, second):
                    peer.settimeout(1.0)
                    peer.connect(str(bridge.path))
                    peer.send(registration([NAME.decode()]))
                harness.spin(lambda: sum(bool(names) for names in bridge.peers.values()) == 2)
                harness.device(Packet(Op.OPEN, CHANNEL, data=NAME))
                old, opened = decode_local(first.recv(MAX_LOCAL))
                assert opened.op == Op.OPEN
                first.send(encode_local(old, Packet(Op.OPEN_OK, CHANNEL)))
                harness.expect(Op.OPEN_OK)
                bridge.link_lost()
                assert decode_local(first.recv(MAX_LOCAL))[1].op == Op.CLOSE
                harness.device(Packet(Op.OPEN, CHANNEL, data=NAME))
                new, _ = decode_local(first.recv(MAX_LOCAL))
                assert new != old
                first.send(encode_local(old, Packet(Op.CLOSE, CHANNEL, data=U16.pack(Status.OK))))
                second.send(encode_local(new, Packet(Op.CLOSE, CHANNEL, data=U16.pack(Status.OK))))
                first.send(encode_local(new, Packet(Op.OPEN_OK, CHANNEL)))
                harness.expect(Op.OPEN_OK)
                assert not harness.from_host
        finally:
            bridge.close()
