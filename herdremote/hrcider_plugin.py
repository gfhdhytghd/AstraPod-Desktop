"""Authenticated renderer bridge. Metadata only; no Apple credentials or audio."""
import json
import logging
import os
from pathlib import Path
import queue
import secrets
import stat
import threading
import time

log = logging.getLogger('herdremote.cider')
VERSION = 1
MAX_BYTES = 2 * 1024 * 1024
MAX_PENDING = 16
REASONS = frozenset(('offline', 'unauthorized', 'timeout', 'unsupported-host',
                     'unsupported-operation', 'invalid-request', 'reply-too-large', 'host-error', 'busy'))


class PluginError(OSError):
    def __init__(self, reason):
        self.plugin_reason = reason if reason in REASONS else 'host-error'
        super().__init__('Cider plugin: ' + self.plugin_reason)


def install_secret(path):
    """Create once, refuse symlinks and insecure existing files. Never log it."""
    path = Path(path).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'w') as out:
            out.write(secrets.token_hex(32) + '\n')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('PodBox plugin secret must be an owned regular 0600 file')
        value = source.read(66).strip()
    if len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('Invalid PodBox plugin secret')
    return value


def default_secret_path():
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home()/'.config'))) / 'podbox/cider-plugin.secret'


class PluginTransport:
    """One renderer connection, bounded synchronous requests, push state cache."""
    def __init__(self, secret, port=10768, origins=None):
        self.secret, self.port = secret, port
        self.origins = origins or ['http://127.0.0.1:10767', 'http://localhost:10767']
        self.lock = threading.RLock()
        self.outgoing = queue.Queue(MAX_PENDING)
        self.sender_stop = threading.Event()
        self.sender = None
        self.ws = None
        self.state = None
        self.reason = ''
        self.pending = {}
        self.serial = 0
        self.callbacks = []
        self.server = self.thread = None
        self.closed = False

    @property
    def connected(self):
        with self.lock:
            return self.ws is not None

    def start(self):
        from websockets.sync.server import serve
        with self.lock:
            if self.server or self.closed:
                return
            # websockets DEBUG frame logs would include the authentication
            # frame. Use a private silent protocol logger at every log level.
            protocol_log = logging.Logger('podbox.plugin.protocol', logging.CRITICAL + 1)
            self.server = serve(self._connection, '127.0.0.1', self.port,
                                origins=self.origins, max_size=MAX_BYTES,
                                max_queue=4, open_timeout=2, close_timeout=.2,
                                ping_interval=10, ping_timeout=5,
                                compression=None, logger=protocol_log)
            self.port = self.server.socket.getsockname()[1]
            self.thread = threading.Thread(target=self.server.serve_forever, name='cider-plugin', daemon=True)
            self.thread.start()
            self.sender = threading.Thread(target=self._send_worker, name='cider-plugin-send', daemon=True)
            self.sender.start()

    def _send_worker(self):
        # A stalled peer must not trap a request caller in socket.send(). One
        # bounded sender serves all requests; heartbeat/close releases its I/O.
        while not self.sender_stop.is_set():
            try: ws, key, raw, deadline = self.outgoing.get(timeout=.1)
            except queue.Empty: continue
            with self.lock:
                if time.monotonic() >= deadline or ws is not self.ws or key not in self.pending:
                    continue
            try: ws.send(raw)
            except Exception:
                with self.lock: slot = self.pending.get(key)
                if slot:
                    try: slot.put_nowait({'error': 'offline'})
                    except queue.Full: pass

    def _emit(self, kind):
        with self.lock:
            callbacks = list(self.callbacks)
        for callback in callbacks:
            try:
                callback({'type': kind}, time.monotonic())
            except Exception:
                log.debug('Cider plugin event consumer failed')

    def _connection(self, ws):
        # Server Origin checking prevents browser cross-site use. Authentication
        # is inside a bounded frame so secrets never occur in URLs or headers.
        try:
            hello = json.loads(ws.recv(timeout=2))
            if not isinstance(hello, dict) or hello.get('v') != VERSION or hello.get('type') != 'hello':
                ws.close(1008, 'hello required'); return
            token = hello.get('secret')
            if not isinstance(token, str) or not secrets.compare_digest(token, self.secret):
                ws.close(1008, 'authentication failed'); return
            state = hello.get('state')
            if not isinstance(state, dict):
                ws.close(1008, 'state required'); return
            from hrcider import Client
            Client._playback_valid(state, True)
            with self.lock:
                if self.ws is not None or self.closed:
                    ws.close(1013, 'already connected'); return
                self.ws, self.state = ws, state
                self.reason = hello.get('reason') if hello.get('reason') in REASONS else ''
            ws.send(json.dumps({'v': VERSION, 'type': 'hello', 'ok': True}))
            log.info('Cider plugin connected protocol=1')
            self._emit('playbackStatus.nowPlayingItemDidChange')
            for raw in ws:
                value = json.loads(raw)
                if not isinstance(value, dict) or value.get('v') != VERSION:
                    ws.close(1008, 'protocol'); break
                if value.get('type') == 'reply':
                    with self.lock:
                        slot = self.pending.get(value.get('id'))
                    if slot:
                        try: slot.put_nowait(value)
                        except queue.Full: pass
                elif value.get('type') == 'event' and isinstance(value.get('state'), dict):
                    Client._playback_valid(value['state'], True)
                    with self.lock:
                        self.state = value['state']
                        self.reason = value.get('reason') if value.get('reason') in REASONS else ''
                    self._emit(str(value.get('event', '')))
                else:
                    ws.close(1008, 'protocol'); break
        except Exception:
            # Never log a peer's frame, exception text, headers or credentials.
            pass
        finally:
            disconnected = False
            with self.lock:
                if self.ws is ws:
                    disconnected = True
                    self.ws, self.state, self.reason = None, None, ''
                    for slot in self.pending.values():
                        try: slot.put_nowait({'error': 'offline'})
                        except queue.Full: pass
            if disconnected:
                log.info('Cider plugin disconnected')
                self._emit('playbackStatus.playbackStateDidChange')

    def request(self, path, body=None, timeout=.8, method=None):
        deadline = time.monotonic() + max(.01, min(10, timeout))
        with self.lock:
            ws = self.ws
            if not ws:
                raise PluginError('offline')
            if len(self.pending) >= MAX_PENDING:
                raise PluginError('busy')
            self.serial += 1
            key = str(self.serial)
            slot = self.pending[key] = queue.Queue(1)
        try:
            raw = json.dumps(dict(v=VERSION, type='request', id=key, path=path,
                                  body=body, method=method or ('GET' if body is None else 'POST'),
                                  timeoutMs=max(1, int((deadline-time.monotonic())*1000))))
            if len(raw.encode()) > 16 * 1024:
                raise PluginError('invalid-request')
            try: self.outgoing.put_nowait((ws, key, raw, deadline))
            except queue.Full: raise PluginError('busy') from None
            try: result = slot.get(timeout=max(0, deadline-time.monotonic()))
            except queue.Empty: raise PluginError('timeout') from None
            if result.get('error'):
                raise PluginError(result['error'])
            data = result.get('data')
            if not isinstance(data, (dict, list)):
                raise PluginError('host-error')
            return data
        except PluginError:
            raise
        except Exception:
            raise PluginError('offline') from None
        finally:
            with self.lock:
                self.pending.pop(key, None)

    def playback(self):
        with self.lock:
            if self.reason:
                raise PluginError(self.reason)
            if not self.ws or not self.state:
                raise PluginError('offline')
            return dict(self.state)

    def close(self):
        with self.lock:
            self.closed = True
            self.sender_stop.set()
            ws, server = self.ws, self.server
        if ws: ws.close(1001, 'host shutdown')
        if server: server.shutdown()
        if self.thread: self.thread.join(timeout=3)
        if self.sender: self.sender.join(timeout=3)


def make_clients(rpc_class, env=os.environ):
    """Delay importing hrcider until it has finished defining Client."""
    mode = env.get('CIDER_BACKEND', 'auto')
    if mode not in ('auto', 'plugin', 'rpc'):
        raise ValueError('CIDER_BACKEND must be auto, plugin or rpc')
    rpc = rpc_class(env.get('CIDER_API_URL', 'http://127.0.0.1:10767'), env.get('CIDER_API_TOKEN'))
    if mode == 'rpc':
        return rpc
    from hrcider import CiderPluginClient
    transport = PluginTransport(install_secret(env.get('CIDER_PLUGIN_SECRET_FILE', default_secret_path())),
                                int(env.get('CIDER_PLUGIN_PORT', '10768')), [rpc.base])
    return Backend(rpc, CiderPluginClient(transport), mode)


class Backend:
    """Select once per public operation; never replay a failed mutation on RPC."""
    def __init__(self, rpc, plugin, mode='auto'):
        self.rpc, self.plugin, self.mode = rpc, plugin, mode
        self.base, self.token = rpc.base, rpc.token

    @property
    def using_plugin(self):
        return self.mode == 'plugin' or self.plugin.transport.connected

    @property
    def rpc_observer(self):
        return getattr(self.rpc, 'rpc_observer', None)

    @rpc_observer.setter
    def rpc_observer(self, callback):
        self.rpc.rpc_observer = self.plugin.rpc_observer = callback

    def __getattr__(self, name):
        return getattr(self.plugin if self.using_plugin else self.rpc, name)

    def start(self):
        try:
            self.plugin.transport.start()
        except OSError:
            if self.mode == 'plugin': raise
            log.warning('Cider plugin listener unavailable; using RPC')

    def close(self):
        self.plugin.transport.close()

    def watch_events(self, stop, callback):
        from hrcider_events import playback_events
        transport = self.plugin.transport
        transport.callbacks.append(callback)
        rpc_stop = None
        worker = None
        try:
            while not stop.wait(.1):
                if self.using_plugin:
                    if rpc_stop: rpc_stop.set()
                elif self.mode == 'auto' and (worker is None or not worker.is_alive()):
                    rpc_stop = threading.Event()
                    worker = threading.Thread(target=playback_events, args=(self.rpc, rpc_stop, callback), daemon=True)
                    worker.start()
        finally:
            if rpc_stop: rpc_stop.set()
            if worker: worker.join(timeout=3)
            transport.callbacks.remove(callback)
