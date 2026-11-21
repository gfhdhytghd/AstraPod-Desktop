"""Route APL1 frames between the resident PodLink and local plugin hosts."""
from collections import deque
from dataclasses import dataclass
import fcntl
from itertools import count
from pathlib import Path
import socket
import stat
import time

import podlink
from astrapod_plugins.podlink_wire import (
    MAGIC, MAX_LOCAL, REGISTER, Op, Packet, Status, decode_local, encode_local,
    parse_registration, socket_path,
)


MAX_PEERS = 4
MAX_OUT = 4
OPEN_TIMEOUT = 3.0


@dataclass
class Binding:
    name: str
    channel: int
    token: int
    peer: socket.socket
    deadline: float | None


class PluginLink:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else socket_path()
        self.listener = self.lock = None
        self.peers = {}
        self.binding = None
        self.out = deque()
        self.tokens = count(1)

    def start(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = self.path.with_suffix(".lock").open("a")
        listener = None
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                existing = self.path.lstat()
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISSOCK(existing.st_mode):
                    raise OSError(f"not a socket: {self.path}")
                self.path.unlink()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            listener.setblocking(False)
            listener.bind(str(self.path))
            self.path.chmod(0o600)
            listener.listen(MAX_PEERS)
        except OSError:
            if listener is not None:
                listener.close()
                self.path.unlink(missing_ok=True)
            lock.close()
            raise
        self.lock = lock
        self.listener = listener

    def close(self):
        self.link_lost()
        for peer in list(self.peers):
            self._drop_peer(peer)
        if self.listener is not None:
            self.listener.close()
            self.path.unlink(missing_ok=True)
            self.listener = None
            self.lock.close()
            self.lock = None

    def link_lost(self):
        self._release(Status.LOST, notify_peer=True)
        self.out.clear()

    def _drop_peer(self, peer):
        self.peers.pop(peer, None)
        peer.close()
        if self.binding is not None and self.binding.peer is peer:
            self._release(Status.LOST, notify_device=True)

    def _local(self, binding, packet):
        try:
            binding.peer.send(encode_local(binding.token, packet))
            return True
        except OSError:
            self._drop_peer(binding.peer)
            return False

    def _release(self, status, *, notify_peer=False, notify_device=False):
        binding = self.binding
        if binding is None:
            return
        self.binding = None
        self.out = deque(packet for packet in self.out
                         if packet.channel != binding.channel)
        packet = Packet.stop(binding.channel, status)
        if notify_peer:
            self._local(binding, packet)
        if notify_device:
            self._queue_control(packet)

    def _queue_control(self, packet):
        if len(self.out) < MAX_OUT + 1:
            self.out.append(packet)

    def receive(self, frame):
        """Consume only APL1 BINARY envelopes from the established link."""
        if frame.type != podlink.BINARY or not frame.payload.startswith(MAGIC):
            return False
        try:
            packet = Packet.decode(frame.payload)
        except ValueError:
            return True
        if packet.op == Op.OPEN:
            self._open(packet)
            return True
        binding = self.binding
        if binding is None or binding.channel != packet.channel:
            return True
        if packet.op == Op.CLOSE:
            self._release(Status.OK)
            self._local(binding, packet)
        elif binding.deadline is None and packet.op != Op.OPEN_OK:
            self._local(binding, packet)
        return True

    def _open(self, packet):
        if self.binding is not None:
            if self.binding.channel != packet.channel:
                self._queue_control(Packet.stop(packet.channel, Status.BUSY))
            return
        name = packet.data.decode("ascii")
        peer = next((peer for peer, names in self.peers.items() if name in names), None)
        if peer is None:
            self._queue_control(Packet.stop(packet.channel, Status.NO_HANDLER))
            return
        binding = Binding(name, packet.channel, next(self.tokens), peer,
                          time.monotonic() + OPEN_TIMEOUT)
        self.binding = binding
        self._local(binding, packet)

    def _from_peer(self, peer, raw):
        if raw.startswith(REGISTER):
            self.peers[peer] = parse_registration(raw)
            if (self.binding and self.binding.peer is peer
                    and self.binding.name not in self.peers[peer]):
                self._release(Status.LOST, notify_device=True)
            return
        token, packet = decode_local(raw)
        binding = self.binding
        if (binding is None or binding.peer is not peer
                or binding.token != token or binding.channel != packet.channel):
            return
        if packet.op == Op.CLOSE:
            self._release(Status.OK)
            self._queue_control(packet)
            return
        if binding.deadline is not None:
            if packet.op != Op.OPEN_OK:
                return
            binding.deadline = None
        elif packet.op in (Op.OPEN, Op.OPEN_OK, Op.PUSH, Op.PULL):
            self._release(Status.BAD_FRAME, notify_device=True, notify_peer=True)
            return
        if len(self.out) >= MAX_OUT:
            self._release(Status.BUSY, notify_device=True, notify_peer=True)
            return
        self.out.append(packet)

    def poll(self, link):
        """Process a bounded slice of local traffic and one serial frame."""
        if self.listener is None:
            return
        try:
            peer, _ = self.listener.accept()
        except BlockingIOError:
            pass
        else:
            peer.setblocking(False)
            if len(self.peers) < MAX_PEERS:
                self.peers[peer] = frozenset()
            else:
                peer.close()
        for peer in list(self.peers):
            for _ in range(MAX_OUT):
                try:
                    raw, _, flags, _ = peer.recvmsg(MAX_LOCAL)
                    if not raw or flags & socket.MSG_TRUNC:
                        raise ValueError("closed or oversized local packet")
                    self._from_peer(peer, raw)
                except BlockingIOError:
                    break
                except (OSError, ValueError):
                    self._drop_peer(peer)
                    break
                if peer not in self.peers:
                    break
        binding = self.binding
        if binding and binding.deadline is not None and binding.deadline <= time.monotonic():
            self._release(Status.TIMEOUT, notify_device=True, notify_peer=True)
        if link is not None and self.out and link.best_effort_delay() is not None:
            packet = self.out.popleft()
            link.send(podlink.BINARY, packet.encode())
