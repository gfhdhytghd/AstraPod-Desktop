"""Nonblocking connection from the plugin supervisor to HerdRemote."""
from collections import deque
from dataclasses import dataclass
from pathlib import Path
import socket

from .podlink_wire import (
    MAX_LOCAL, Op, Packet, Status, decode_local, encode_local, registration,
    socket_path,
)


QUEUE_LIMIT = 4


@dataclass(frozen=True)
class Binding:
    name: str
    plugin: str
    token: int
    channel: int


class HostLink:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else socket_path()
        self.socket = None
        self.routes = {}
        self.registered = None
        self.binding = None
        self.out = deque()

    def _connect(self):
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        peer.setblocking(False)
        try:
            peer.connect(str(self.path))
        except OSError:
            peer.close()
            return
        self.socket = peer

    def close(self, deliver):
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        self.registered = None
        self.out.clear()
        binding, self.binding = self.binding, None
        if binding is not None:
            deliver(binding.plugin, binding.token,
                    Packet.stop(binding.channel, Status.LOST))

    def send(self, plugin, token, packet):
        binding = self.binding
        if (binding is None or binding.plugin != plugin or binding.token != token
                or binding.channel != packet.channel):
            return
        if packet.op == Op.CLOSE:
            self.out.clear()
            self.binding = None
        elif len(self.out) >= QUEUE_LIMIT + (packet.op == Op.CANCEL):
            raise OSError("plugin PodLink queue full")
        self.out.append(encode_local(token, packet))

    def _receive(self, token, packet, deliver):
        binding = self.binding
        if packet.op == Op.OPEN:
            if binding is not None:
                return
            name = packet.data.decode("ascii")
            plugin = self.routes.get(name)
            if plugin is None:
                if len(self.out) >= QUEUE_LIMIT:
                    raise OSError("plugin PodLink control queue full")
                self.out.append(encode_local(token, Packet.stop(packet.channel, Status.NO_HANDLER)))
                return
            binding = Binding(name, plugin, token, packet.channel)
            self.binding = binding
        if (binding is None or token != binding.token or packet.channel != binding.channel):
            return
        if packet.op == Op.CLOSE:
            self.binding = None
            self.out.clear()
        if not deliver(binding.plugin, token, packet):
            self.binding = None
            self.out.clear()
            self.out.append(encode_local(token, Packet.stop(packet.channel, Status.BUSY)))

    def poll(self, routes, deliver):
        self.routes = routes
        if self.binding and routes.get(self.binding.name) != self.binding.plugin:
            self.close(deliver)
        if self.socket is None and routes:
            self._connect()
        if self.socket is None:
            return
        peer = self.socket
        try:
            names = frozenset(routes)
            if names != self.registered:
                peer.send(registration(names))
                self.registered = names
            for _ in range(QUEUE_LIMIT):
                if not self.out:
                    break
                peer.send(self.out[0])
                self.out.popleft()
            for _ in range(QUEUE_LIMIT):
                raw, _, flags, _ = peer.recvmsg(MAX_LOCAL)
                if not raw or flags & socket.MSG_TRUNC:
                    raise ValueError("closed or oversized PodLink socket record")
                self._receive(*decode_local(raw), deliver)
        except BlockingIOError:
            pass
        except (OSError, ValueError):
            self.close(deliver)
