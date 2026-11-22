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
    HEADER, MAGIC, MAX_DATA, MAX_LOCAL, REGISTER, U16, Op, Packet, Status,
    decode_local, encode_local, parse_registration, socket_path,
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
    answered: bool = False


class PluginLink:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else socket_path()
        self.listener = self.lock = None
        self.peers = {}
        self.owners = {}
        self.binding = None
        self.out = deque()
        self.rejections = deque()
        self.termination = None
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
        self.rejections.clear()
        self.termination = None

    def _register(self, peer, names):
        previous = self.peers[peer]
        self.peers[peer] = names
        for name in previous - names:
            if self.owners[name] is not peer:
                continue
            del self.owners[name]
            for waiting, registered in self.peers.items():
                if name in registered:
                    self.owners[name] = waiting
                    break
        for name in names - previous:
            self.owners.setdefault(name, peer)
        if (self.binding is not None and self.binding.peer is peer
                and self.binding.name not in names):
            self._release(Status.LOST, notify_device=True)

    def _drop_peer(self, peer):
        self._register(peer, frozenset())
        del self.peers[peer]
        peer.close()

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
        self.out.clear()
        packet = Packet.stop(binding.channel, status)
        if notify_peer:
            self._local(binding, packet)
        if notify_device:
            self.termination = packet

    def _reject(self, channel, status):
        # Another channel's rejected OPEN must not consume the active queue.
        if len(self.rejections) < MAX_OUT:
            self.rejections.append(Packet.stop(channel, status))

    def receive(self, frame):
        """Consume only APL1 BINARY envelopes from the established link."""
        if frame.type != podlink.BINARY or not frame.payload.startswith(MAGIC):
            return False
        try:
            packet = Packet.decode(frame.payload)
        except ValueError:
            if self.binding is not None and len(frame.payload) >= HEADER.size:
                _, op, channel, transfer = HEADER.unpack_from(frame.payload)
                if channel == self.binding.channel:
                    status = Status.BAD_FRAME
                    if (op in (Op.TEXT, Op.BINARY) and not transfer
                            and frame.payload[5:8] == b"\0\0\0"
                            and len(frame.payload) > HEADER.size + MAX_DATA):
                        status = Status.LIMIT
                    self._release(status, notify_device=True, notify_peer=True)
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
        elif packet.op in (Op.OPEN_OK, Op.ACCEPT):
            self._release(Status.BAD_FRAME, notify_device=True, notify_peer=True)
        elif binding.deadline is None:
            self._local(binding, packet)
        return True

    def _open(self, packet):
        if self.binding is not None:
            if self.binding.channel != packet.channel:
                self._reject(packet.channel, Status.BUSY)
            return
        name = packet.data.decode("ascii")
        peer = self.owners.get(name)
        if peer is None:
            self._reject(packet.channel, Status.NO_HANDLER)
            return
        binding = Binding(name, packet.channel, next(self.tokens), peer,
                          time.monotonic() + OPEN_TIMEOUT)
        self.binding = binding
        self._local(binding, packet)

    def _from_peer(self, peer, raw):
        if raw.startswith(REGISTER):
            self._register(peer, parse_registration(raw))
            return
        token, packet = decode_local(raw)
        binding = self.binding
        if (binding is None or binding.peer is not peer
                or binding.token != token or binding.channel != packet.channel):
            return
        if packet.op == Op.CLOSE:
            self._release(U16.unpack(packet.data)[0], notify_device=True)
            return
        if not binding.answered:
            if packet.op != Op.OPEN_OK:
                return
            binding.answered = True
        elif packet.op in (Op.OPEN, Op.OPEN_OK, Op.PUSH, Op.PULL):
            self._release(Status.BAD_FRAME, notify_device=True, notify_peer=True)
            return
        if len(self.out) >= MAX_OUT + (packet.op == Op.CANCEL):
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
        if (link is None or not (self.termination or self.out or self.rejections)
                or link.best_effort_delay() is None):
            return
        if self.termination is not None:
            packet, self.termination = self.termination, None
        elif self.out:
            packet = self.out.popleft()
        else:
            packet = self.rejections.popleft()

        payload = packet.encode()
        if len(payload) <= link.max_message:
            link.send(podlink.BINARY, payload)
            if packet.op == Op.OPEN_OK:
                self.binding.deadline = None
        elif self.binding is not None and packet.channel == self.binding.channel:
            self._release(Status.LIMIT, notify_device=True, notify_peer=True)
