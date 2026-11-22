"""Exercise APL1 through the resident bridge and a real plugin subprocess."""
from collections import deque
from pathlib import Path
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import zlib

import pytest

from astrapod_plugins.host import PluginHost
from astrapod_plugins.manifest import load_manifest
from astrapod_plugins.podlink_client import Binding, HostLink, QUEUE_LIMIT
from astrapod_plugins.podlink_files import PodLinkFiles
from astrapod_plugins.podlink_wire import (
    END, MAX_CREDIT, MAX_DATA, MAX_FILE, MAX_LOCAL, MSF1, U16, U32,
    Op, Packet, Status, decode_local, encode_local, registration,
)
import podlink

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "herdremote"))
import hrpluginlink
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
        self.max_message = podlink.MAX_MESSAGE
        self.delay = .01

    def send(self, kind, payload):
        if len(payload) > self.max_message:
            raise ValueError("message exceeds negotiated maximum")
        raw = podlink.encode(kind, self.seq, payload)
        self.seq += 1
        decoded, = self.decoder.feed(raw)
        self.from_host.append(Packet.decode(decoded.payload))

    def best_effort_delay(self):
        return self.delay

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
            harness.host = host
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


def test_firmware_peer_round_trip(bridged, tmp_path):
    peer = os.environ.get("ASTRAPOD_FIRMWARE_APL_PEER")
    if not peer:
        pytest.skip("set ASTRAPOD_FIRMWARE_APL_PEER to the firmware podlink_apl fixture")
    harness, directory = bridged
    device_root = tmp_path / "device"
    rockbox = device_root / ".rockbox"
    rockbox.mkdir(parents=True)
    fixture = bytes(i % 251 for i in range(5000))
    (rockbox / "podlink-echo.in").write_bytes(fixture)

    with subprocess.Popen([peer, "--peer", str(device_root)], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        os.set_blocking(process.stdout.fileno(), False)
        pending = b""
        deadline = time.monotonic() + 15
        try:
            while time.monotonic() < deadline:
                try:
                    incoming = os.read(process.stdout.fileno(), 8192)
                except BlockingIOError:
                    incoming = None
                if incoming == b"":
                    process.wait(timeout=max(.01, deadline - time.monotonic()))
                    break
                if incoming:
                    pending += incoming
                    *records, pending = pending.split(b"\n")
                    for record in records:
                        harness.device(Packet.decode(bytes.fromhex(record.decode("ascii"))))
                if process.poll() is None:
                    harness.bridge.poll(harness)
                    while harness.from_host:
                        packet = harness.from_host.popleft()
                        process.stdin.write(packet.encode().hex().encode("ascii") + b"\n")
                        process.stdin.flush()
                time.sleep(.005)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
        assert process.returncode == 0, process.stderr.read().decode(errors="replace")
        assert not pending
        assert harness.bridge.binding is None

    assert (directory / "from-ipod.bin").read_bytes() == fixture
    assert (rockbox / "podlink-echo.out").read_bytes() == fixture
    assert not list(directory.glob(".apl1-*"))
    assert not list(rockbox.glob("*.apl-*"))


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


def test_negotiated_limit_closes_only_plugin_channel(bridged):
    harness, directory = bridged
    (directory / "large.bin").write_bytes(bytes(MAX_DATA))
    harness.max_message = 512
    harness.device(Packet(Op.OPEN, CHANNEL, data=NAME))
    harness.expect(Op.OPEN_OK)
    harness.device(Packet(Op.PULL, CHANNEL, 1, b"large.bin"))
    assert harness.expect(Op.ACCEPT, 1) == U32.pack(MAX_DATA)
    harness.device(Packet(Op.CREDIT, CHANNEL, 1, b"\x01"))
    assert harness.expect(Op.CLOSE) == U16.pack(Status.LIMIT)

    harness.device(Packet(Op.OPEN, CHANNEL + 1, data=NAME))
    harness.expect(Op.OPEN_OK, channel=CHANNEL + 1)
    harness.device(Packet(Op.BINARY, CHANNEL + 1, data=b"still connected"))
    assert harness.expect(Op.BINARY, channel=CHANNEL + 1) == b"still connected"


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



def test_first_registration_keeps_its_claim_until_disconnect():
    with tempfile.TemporaryDirectory(prefix="apl1-") as run:
        bridge = PluginLink(Path(run) / "plugin.sock")
        bridge.start()
        harness = Harness(bridge)
        try:
            with (socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as waiting,
                  socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as owner):
                for peer in (waiting, owner):
                    peer.settimeout(.25)
                    peer.connect(str(bridge.path))
                harness.spin(lambda: len(bridge.peers) == 2)
                owner.send(registration([NAME.decode()]))
                harness.spin(lambda: sum(bool(names) for names in bridge.peers.values()) == 1)
                waiting.send(registration([NAME.decode()]))
                harness.spin(lambda: sum(bool(names) for names in bridge.peers.values()) == 2)
                harness.device(Packet(Op.OPEN, CHANNEL, data=NAME))
                token, opened = decode_local(owner.recv(MAX_LOCAL))
                assert opened.op == Op.OPEN
                owner.send(encode_local(token, Packet(Op.OPEN_OK, CHANNEL)))
                harness.expect(Op.OPEN_OK)
                owner.close()
                assert harness.expect(Op.CLOSE) == U16.pack(Status.LOST)
                harness.device(Packet(Op.OPEN, CHANNEL + 1, data=NAME))
                new, opened = decode_local(waiting.recv(MAX_LOCAL))
                assert opened.channel == CHANNEL + 1
                assert new != token
                waiting.send(encode_local(new, Packet(Op.OPEN_OK, CHANNEL + 1)))
                harness.expect(Op.OPEN_OK, channel=CHANNEL + 1)
        finally:
            bridge.close()


def test_rejected_opens_leave_data_and_close_capacity(bridged):
    harness, _ = bridged
    harness.device(Packet(Op.OPEN, CHANNEL, data=NAME))
    harness.expect(Op.OPEN_OK)
    harness.delay = None
    for channel in range(CHANNEL + 1, CHANNEL + 20):
        harness.device(Packet(Op.OPEN, channel, data=NAME))
    harness.device(Packet(Op.TEXT, CHANNEL, data=b"echo with rejections queued"))
    harness.spin(lambda: any(packet.op == Op.TEXT for packet in harness.bridge.out)
                 or harness.bridge.binding is None)
    assert harness.bridge.binding is not None

    peer = harness.bridge.binding.peer
    peer.shutdown(socket.SHUT_RDWR)
    harness.spin(lambda: harness.bridge.binding is None)
    harness.delay = .01
    assert harness.expect(Op.CLOSE) == U16.pack(Status.LOST)


def test_open_ack_waiting_for_serial_still_expires(bridged, monkeypatch):
    harness, _ = bridged
    now = [0]
    monkeypatch.setattr(hrpluginlink, "time", SimpleNamespace(monotonic=lambda: now[0]))
    harness.delay = None
    harness.device(Packet(Op.OPEN, CHANNEL, data=NAME))
    harness.spin(lambda: bool(harness.bridge.out))
    now[0] += hrpluginlink.OPEN_TIMEOUT
    harness.delay = .01
    assert harness.expect(Op.CLOSE) == U16.pack(Status.TIMEOUT)
    assert harness.bridge.binding is None



def test_failed_commit_preserves_file_and_oversize_source_is_rejected(tmp_path, monkeypatch):
    files = PodLinkFiles(tmp_path)
    target = tmp_path / "data.bin"
    target.write_bytes(b"previous")
    with (tmp_path / "large.bin").open("wb") as source:
        source.truncate(MAX_FILE + 1)
    assert files.handle(1, Packet(Op.PULL, CHANNEL, 1, b"large.bin")) == (
        Packet.stop(CHANNEL, Status.LIMIT, 1),)
    assert files.transfer is None

    def fail(_fd):
        raise OSError("simulated sync failure")

    monkeypatch.setattr(os, "fsync", fail)
    files.handle(1, Packet(Op.PUSH, CHANNEL, 2, U32.pack(3) + b"data.bin"))
    files.handle(1, Packet(Op.CHUNK, CHANNEL, 2, MSF1 + U32.pack(0) + b"new"))
    finish = Packet(Op.FINISH, CHANNEL, 2, END.pack(3, zlib.crc32(b"new")))
    assert files.handle(1, finish) == (Packet.stop(CHANNEL, Status.IO, 2),)
    assert files.transfer is None
    assert target.read_bytes() == b"previous"
    assert not list(tmp_path.glob(".apl1-*"))


def test_close_and_cancel_have_capacity_at_host_and_bridge():
    with tempfile.TemporaryDirectory(prefix="apl1-") as run:
        bridge = PluginLink(Path(run) / "plugin.sock")
        bridge.start()
        harness = Harness(bridge)
        host = HostLink(bridge.path)
        received = deque()

        def deliver(plugin, token, packet):
            received.append((plugin, token, packet))
            return True

        def poll():
            host.poll({NAME.decode(): "echo"}, deliver)
            return any(bridge.peers.values())

        try:
            harness.spin(poll)
            harness.device(Packet(Op.OPEN, CHANNEL, data=NAME))
            host.poll({NAME.decode(): "echo"}, deliver)
            _, token, _ = received.popleft()
            host.send("echo", token, Packet(Op.OPEN_OK, CHANNEL))
            host.poll({NAME.decode(): "echo"}, deliver)
            harness.expect(Op.OPEN_OK)
            assert host.binding == Binding(NAME.decode(), "echo", token, CHANNEL)

            message = Packet(Op.TEXT, CHANNEL, data=b"queued")
            for _ in range(QUEUE_LIMIT):
                host.send("echo", token, message)
            cancel = Packet.stop(CHANNEL, Status.CANCELLED, 1)
            host.send("echo", token, cancel)
            host.poll({NAME.decode(): "echo"}, deliver)
            harness.delay = None
            bridge.poll(harness)
            assert len(bridge.out) == QUEUE_LIMIT
            host.poll({NAME.decode(): "echo"}, deliver)
            bridge.poll(harness)
            assert bridge.out[-1] == cancel

            for _ in range(QUEUE_LIMIT):
                host.send("echo", token, message)
            host.send("echo", token, Packet.stop(CHANNEL, Status.OK))
            assert len(host.out) == 1
            host.poll({NAME.decode(): "echo"}, deliver)
            bridge.poll(harness)
            assert not bridge.out
            harness.delay = .01
            assert harness.expect(Op.CLOSE) == U16.pack(Status.OK)
        finally:
            host.close(deliver)
            bridge.close()


@pytest.mark.parametrize("reason", [Status.BAD_FRAME, Status.LIMIT])
def test_active_invalid_frame_closes_only_its_channel(bridged, reason):
    harness, _ = bridged
    harness.device(Packet(Op.OPEN, CHANNEL, data=NAME))
    harness.expect(Op.OPEN_OK)

    def invalid(channel):
        raw = bytearray(Packet(Op.TEXT, channel).encode())
        if reason == Status.BAD_FRAME:
            raw[5] = 1
        else:
            raw.extend(b"A" * (MAX_DATA + 1))
        assert harness.bridge.receive(podlink.Frame(podlink.BINARY, 1, bytes(raw)))

    invalid(CHANNEL + 1)
    assert harness.bridge.binding is not None
    invalid(CHANNEL)
    assert harness.expect(Op.CLOSE) == U16.pack(reason)
    assert harness.bridge.binding is None
    harness.device(Packet(Op.OPEN, CHANNEL + 2, data=NAME))
    harness.expect(Op.OPEN_OK, channel=CHANNEL + 2)


def test_companion_disable_releases_channel_and_cleans_file(bridged):
    harness, directory = bridged
    target = directory / "prior.bin"
    target.write_bytes(b"previous")
    harness.device(Packet(Op.OPEN, CHANNEL, data=NAME))
    harness.expect(Op.OPEN_OK)
    harness.device(Packet(Op.PUSH, CHANNEL, 1, U32.pack(20) + b"prior.bin"))
    harness.expect(Op.ACCEPT, 1)
    harness.expect(Op.CREDIT, 1)
    assert list(directory.glob(".apl1-*"))

    harness.host.enable("podlink-echo", False)
    assert harness.expect(Op.CLOSE) == U16.pack(Status.LOST)
    harness.spin(lambda: not list(directory.glob(".apl1-*")))
    assert target.read_bytes() == b"previous"
    harness.device(Packet(Op.OPEN, CHANNEL + 1, data=NAME))
    assert harness.expect(Op.CLOSE, channel=CHANNEL + 1) == U16.pack(Status.NO_HANDLER)
