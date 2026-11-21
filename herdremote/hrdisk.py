"""Private local control socket; the daemon remains the only serial owner."""
import os
from pathlib import Path
import secrets
import socket
import stat
import struct

BORROW_SERIAL = b'borrow-serial-v1\n'
ENTER_DISK = b'enter-disk-v1'
CAPABILITY = b'DH\t1\tdisk-mode-v1'


def default_socket():
    return os.path.join(os.environ.get('XDG_RUNTIME_DIR', '/run/user/%d' % os.getuid()),
                        'podbox-herdremote', 'disk.sock')


class Control:
    def __init__(self, path=None, *, fallback=None, release_serial=None):
        self.path = path or default_socket()
        self.fallback = fallback
        self.release_serial = release_serial
        self.serial_lease = None
        self.server = None
        self.reset()

    def reset(self):
        self.token = None

    def hello(self, payload, send):
        if payload != CAPABILITY:
            return False
        if self.token is None:
            self.token = secrets.token_hex(16).encode('ascii')
            send(b'DA\t1\t' + self.token)
        return True

    def start(self):
        root = Path(self.path).parent
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError('disk socket directory must be owned by this user and mode 0700')
        # Do not displace a running owner. A stale socket is removed only after
        # connect explicitly reports that there is no listener.
        if os.path.lexists(self.path):
            with socket.socket(socket.AF_UNIX) as probe:
                try:
                    probe.connect(self.path)
                except ConnectionRefusedError:
                    os.unlink(self.path)
                else:
                    raise RuntimeError('disk control socket already in use')
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(self.path)
        os.chmod(self.path, 0o600)
        self.server.listen(4)
        self.server.setblocking(False)

    def poll(self, link):
        if self.serial_lease is not None:
            try:
                self.serial_lease.recv(1)
            except BlockingIOError:
                pass
            except OSError:
                self.release_lease()
            else:
                self.release_lease()
        if self.server is None:
            return
        try:
            channel, _ = self.server.accept()
        except BlockingIOError:
            return
        with channel:
            channel.settimeout(.05)
            try:
                _, uid, _ = struct.unpack('3i', channel.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i')))
                if uid != os.getuid():
                    reply = b'5 unauthorized\n'
                else:
                    request = b''
                    while b'\n' not in request and len(request) < 512:
                        part = channel.recv(512 - len(request))
                        if not part:
                            break
                        request += part
                    if request.startswith(b'ir-') and self.fallback is not None:
                        channel.sendall(self.fallback.handle(request, link))
                        return
                    if request == BORROW_SERIAL and self.release_serial is not None:
                        if self.serial_lease is not None:
                            channel.sendall(b'5 serial already borrowed\n')
                            return
                        self.release_serial()
                        channel.sendall(b'0 serial released\n')
                        self.serial_lease = channel.dup()
                        self.serial_lease.setblocking(False)
                        return
                    target = request.removesuffix(b'\n').split(b'\t')
                    if not request.endswith(b'\n') or target[0] != ENTER_DISK or len(target) not in (1, 2):
                        channel.sendall(b'5 invalid request\n')
                        return
                if uid != os.getuid():
                    pass
                elif link is None or not link.opened:
                    reply = b'3 no PodLink session\n'
                elif len(target) == 2 and target[1] != os.fsencode(
                        os.path.realpath(link.port.port)):
                    reply = b'5 PodLink session is for a different player\n'
                elif self.token is None:
                    reply = b'4 firmware lacks disk-mode-v1 hello\n'
                else:
                    # Consume before writing: uncertain writes are never replayed.
                    token, self.token = self.token, None
                    import podlink
                    link.send(podlink.TEXT, b'DK\t1\t' + token)
                    reply = b'0 disk request sent\n'
                channel.sendall(reply)
            except (OSError, TimeoutError):
                return

    def release_lease(self):
        if self.serial_lease is not None:
            self.serial_lease.close()
            self.serial_lease = None

    def close(self):
        self.release_lease()
        if self.fallback is not None:
            self.fallback.close()
        if self.server is not None:
            self.server.close()
            self.server = None
            os.unlink(self.path)
