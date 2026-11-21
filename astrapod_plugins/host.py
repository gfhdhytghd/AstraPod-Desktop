"""Discover and supervise input plugins without importing Qt."""
from copy import deepcopy
from dataclasses import dataclass, replace
import fcntl
import json
import logging
import os
from pathlib import Path
import selectors
import signal
import subprocess
import tempfile
import threading
import time

from .actions import Actions, finite
from .manifest import discover, settings_record
from .sdk import MAX_LINE, PHASES, REMOTES, STATES, decode, encode


LOG = logging.getLogger(__name__)
PACKAGE = Path(__file__).resolve().parent
TOOLS = PACKAGE.parent
POLL_INTERVAL = .05
CONFIG_INTERVAL = .25
BACKOFF_MIN = .5
BACKOFF_MAX = 30
STABLE_TIME = 30
STOP_GRACE = 20  # Apple IR can be finishing a bounded Disk Mode eject.


@dataclass(frozen=True)
class PluginInfo:
    id: str
    name: str
    version: str
    description: str
    enabled: bool
    state: str
    device: str | None
    detail: str
    permissions: tuple[str, ...]
    settings_schema: dict
    last_event: dict | None
    mode: str | None = None


@dataclass
class _Child:
    process: subprocess.Popen
    settings: dict
    started: float
    pending: bytes = b''
    deadline: float | None = None
    error: str | None = None


class PluginHost:
    def __init__(self, config_path=None, *, repo_dir=PACKAGE, user_dir=None, actions=None):
        config_dir = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
        data_dir = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share')
        runtime_dir = Path(os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}')
        self.config_path = Path(config_path or config_dir / 'astrapod/plugins.json')
        self.runtime_dir = runtime_dir / 'astrapod-plugins'
        self.manifests = discover(repo_dir, user_dir or data_dir / 'astrapod/plugins')
        self._actions = Actions() if actions is None else actions
        self._lock = threading.RLock()
        self._callbacks = []
        self._worker = None
        self._stop = threading.Event()
        self._info = {
            m.id: PluginInfo(m.id, m.name, m.version, m.description, False, 'disabled',
                             None, '', m.permissions, m.settings_schema, None)
            for m in self.manifests.values()
        }

    def _read_config(self):
        try:
            config = json.loads(self.config_path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {}
        if not isinstance(config, dict):
            raise ValueError('plugin config must be a JSON object')
        for plugin_id, fields in config.items():
            if (not isinstance(fields, dict) or type(fields.get('enabled', False)) is not bool
                    or not isinstance(fields.get('settings', {}), dict)):
                raise ValueError(f'{plugin_id}: invalid plugin config')
            if plugin_id in self.manifests:
                self.manifests[plugin_id].settings(fields.get('settings', {}))
        return config

    def _update_config(self, plugin_id, *, enabled=None, settings=None):
        manifest = self.manifests[plugin_id]
        path = self.config_path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.with_name(path.name + '.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            config = self._read_config()
            fields = config.setdefault(plugin_id, {})
            if enabled is not None:
                fields['enabled'] = enabled
            if settings is not None:
                overrides = {**fields.get('settings', {}), **settings}
                manifest.settings(overrides)
                fields['settings'] = overrides
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    json.dump(config, stream, indent=2, allow_nan=False)
                    stream.write('\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                    stream.close()
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)

    def list(self):
        config = self._read_config()
        with self._lock:
            rows = deepcopy(list(self._info.values()))
        result = []
        for row in rows:
            if config.get(row.id, {}).get('enabled', False):
                state = 'waiting' if row.state == 'disabled' else row.state
                result.append(replace(row, enabled=True, state=state))
            else:
                result.append(replace(row, state='disabled', device=None, detail='', mode=None))
        return result

    def enable(self, plugin_id, enabled):
        if type(enabled) is not bool:
            raise ValueError('enabled must be a boolean')
        self._update_config(plugin_id, enabled=enabled)

    def settings(self, plugin_id):
        fields = self._read_config().get(plugin_id, {})
        return self.manifests[plugin_id].settings(fields.get('settings', {}))

    def set_settings(self, plugin_id, settings):
        if not isinstance(settings, dict):
            raise ValueError('settings must be an object')
        self._update_config(plugin_id, settings=settings)

    def subscribe(self, callback):
        with self._lock:
            self._callbacks.append(callback)

        def unsubscribe():
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)

        return unsubscribe

    @property
    def running(self):
        with self._lock:
            return self._worker is not None and self._worker.is_alive()

    def start(self):
        with self._lock:
            if self.running:
                return
            self._read_config()
            self._stop.clear()
            self._worker = threading.Thread(target=self._supervise, name='astrapod-plugins',
                                            daemon=True)
            self._worker.start()

    def stop(self):
        with self._lock:
            worker = self._worker
            self._stop.set()
        if worker is not None and worker is not threading.current_thread():
            worker.join()

    def _publish(self, record, **fields):
        plugin_id = record['source']
        with self._lock:
            old = self._info[plugin_id]
            new = replace(old, **fields)
            self._info[plugin_id] = new
            if record['type'] == 'status' and old == new:
                return
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            try:
                callback(record.copy())
            except Exception:
                LOG.exception('%s: plugin subscriber failed', plugin_id)

    def _status(self, plugin_id, state, device=None, detail='', mode=None):
        record = dict(type='status', source=plugin_id, state=state, device=device, detail=detail)
        if mode is not None:
            record['mode'] = mode
        self._publish(record, state=state, device=device, detail=detail, mode=mode)

    def _launch(self, selector, manifest, settings, now):
        self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.runtime_dir / (manifest.id + '.lock')
        with lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return None
            env = os.environ.copy()
            paths = [str(TOOLS)]
            if env.get('PYTHONPATH'):
                paths.append(env['PYTHONPATH'])
            env['PYTHONPATH'] = os.pathsep.join(paths)
            process = subprocess.Popen(manifest.command(), cwd=manifest.entry.parent, env=env,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0,
                                       start_new_session=True, pass_fds=(lock.fileno(),))
        try:
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
            self._send(process, settings_record(manifest.id, settings))
            selector.register(process.stdout, selectors.EVENT_READ, manifest.id)
        except (OSError, ValueError):
            self._signal(process, signal.SIGKILL)
            process.wait()
            self._close_pipes(process)
            raise
        return _Child(process, settings, now)

    @staticmethod
    def _send(process, record):
        os.write(process.stdin.fileno(), encode(record))

    @staticmethod
    def _signal(process, signum):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass

    @staticmethod
    def _close_pipes(process):
        process.stdin.close()
        process.stdout.close()

    def _retire(self, selector, child, now, error=None):
        if child.deadline is not None:
            return
        selector.unregister(child.process.stdout)
        child.deadline = now + STOP_GRACE
        child.error = error
        # Let the bridge release buttons and discard voice before forcing exit.
        try:
            child.process.terminate()
        except ProcessLookupError:
            pass

    def _message(self, plugin_id, process, record):
        kind = record.get('type')
        if kind == 'input':
            key, phase, stamp = record.get('key'), record.get('phase'), record.get('t')
            if not isinstance(key, str) or not key.strip() or phase not in PHASES or not finite(stamp) or stamp < 0:
                raise ValueError('invalid input event')
            event = dict(type='input', key=key, phase=phase, source=plugin_id, t=stamp)
            if record.get('remote') in REMOTES:
                event['remote'] = record['remote']
            self._publish(event, last_event=event)
        elif kind == 'status':
            state, device, detail = record.get('state'), record.get('device'), record.get('detail')
            mode = record.get('mode')
            if (state not in STATES or (device is not None and not isinstance(device, str))
                    or not isinstance(detail, str) or mode not in (None, 'ipod', 'host-fallback', 'pending')):
                raise ValueError('invalid status event')
            self._status(plugin_id, state, device, detail, mode)
        elif kind == 'action':
            request_id = record.get('id')
            if type(request_id) is not int or not 0 <= request_id < 2**64:
                raise ValueError('invalid action request ID')
            reply = dict(type='action_result', id=request_id, ok=True)
            try:
                self._actions.dispatch(self.manifests[plugin_id].permissions, record)
            except (ValueError, OSError) as exc:
                reply.update(ok=False, error=str(exc))
            self._send(process, reply)
        else:
            raise ValueError('unknown plugin message')

    def _read_child(self, plugin_id, child):
        try:
            data = os.read(child.process.stdout.fileno(), MAX_LINE)
        except BlockingIOError:
            return
        if not data:
            raise EOFError('plugin closed stdout')
        lines = (child.pending + data).split(b'\n')
        child.pending = lines.pop()
        for line in lines:
            self._message(plugin_id, child.process, decode(line + b'\n'))
        if len(child.pending) >= MAX_LINE:
            raise ValueError('unterminated plugin record exceeds line limit')

    def _supervise(self):
        children = {}
        retry = {}
        delays = {}
        config = {}
        next_config = 0
        config_error = None

        def failed(plugin_id, detail, now, started=None):
            delay = delays.get(plugin_id, BACKOFF_MIN)
            if started is not None and now - started >= STABLE_TIME:
                delay = BACKOFF_MIN
            retry[plugin_id] = now + delay
            delays[plugin_id] = min(delay * 2, BACKOFF_MAX)
            self._status(plugin_id, 'error', detail=f'{detail}. Retry in {delay:g} s')

        with selectors.DefaultSelector() as selector:
            try:
                while not self._stop.is_set() or children:
                    for entry, _ in selector.select(POLL_INTERVAL):
                        if self._stop.is_set():
                            break
                        child = children[entry.data]
                        try:
                            self._read_child(entry.data, child)
                        except (OSError, ValueError, EOFError, RecursionError) as exc:
                            self._retire(selector, child, time.monotonic(), str(exc))

                    now = time.monotonic()
                    stopping = self._stop.is_set()
                    if not stopping and now >= next_config:
                        next_config = now + CONFIG_INTERVAL
                        try:
                            config = self._read_config()
                            config_error = None
                        except (OSError, ValueError, RecursionError) as exc:
                            if str(exc) != config_error:
                                config_error = str(exc)
                                LOG.error('Cannot read plugin configuration: %s', exc)

                    for plugin_id, manifest in self.manifests.items():
                        fields = config.get(plugin_id, {})
                        enabled = not stopping and fields.get('enabled', False)
                        child = children.get(plugin_id)
                        settings = manifest.settings(fields.get('settings', {}))
                        if not enabled:
                            retry.pop(plugin_id, None)
                            delays.pop(plugin_id, None)
                        if child is not None:
                            code = child.process.poll()
                            if code is not None:
                                if child.deadline is None:
                                    selector.unregister(child.process.stdout)
                                self._close_pipes(child.process)
                                del children[plugin_id]
                                if enabled and (child.deadline is None or child.error):
                                    failed(plugin_id, child.error or f'Plugin exited ({code})', now,
                                           child.started)
                            elif child.deadline is not None:
                                if now >= child.deadline:
                                    self._signal(child.process, signal.SIGKILL)
                            elif not enabled or child.settings != settings:
                                self._retire(selector, child, now)
                            continue
                        if not enabled:
                            self._status(plugin_id, 'waiting' if fields.get('enabled') else 'disabled')
                        elif now >= retry.get(plugin_id, 0):
                            try:
                                child = self._launch(selector, manifest, settings, now)
                            except (OSError, ValueError) as exc:
                                failed(plugin_id, str(exc), now)
                                continue
                            if child is None:
                                retry[plugin_id] = now + BACKOFF_MIN
                                self._status(plugin_id, 'waiting', detail='Plugin is running in another host')
                            else:
                                children[plugin_id] = child
                                self._status(plugin_id, 'waiting', detail='Starting plugin')
            finally:
                for child in children.values():
                    self._signal(child.process, signal.SIGKILL)
                    child.process.wait()
                    self._close_pipes(child.process)
                for plugin_id in self.manifests:
                    self._status(plugin_id, 'waiting' if config.get(plugin_id, {}).get('enabled') else 'disabled')
