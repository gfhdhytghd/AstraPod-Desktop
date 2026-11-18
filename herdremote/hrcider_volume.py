"""HyprV monitor volume shared by every remote playback source."""
import json
import os
from pathlib import Path
import socket
import subprocess


def percent(value):
    return max(0, min(100, int(value)))


class MonitorVolume:
    def __init__(self, env=None, run=subprocess.run):
        env = os.environ if env is None else env
        home = Path(env.get('HOME') or Path.home())
        runtime = Path(env.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}')
        self.socket = runtime / 'hyprv-monitor-volume-control.sock'
        self.state = home / '.local/state/hyprv/monitor-volume.json'
        self.helper = home / '.config/HyprV/hypr/scripts/monitor-volume.py'
        self.run = run
        self.value = (0, False)
        self.file_version = None

    @staticmethod
    def decode(data, response=True):
        try:
            state = json.loads(data)
            if not isinstance(state, dict) or (response and 'error' in state):
                raise ValueError
            return state
        except (UnicodeError, ValueError) as error:
            raise OSError('Monitor volume service error') from error

    def _cli(self, action, value=None):
        args = ['python3', str(self.helper), action]
        if value is not None:
            args.append(str(value))
        try:
            reply = self.run(args, capture_output=True, timeout=20, check=True)
        except (subprocess.SubprocessError, OSError) as error:
            raise OSError('Monitor volume helper unavailable') from error
        return self.decode(reply.stdout)

    def _socket(self, action, value=None):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            try:
                client.connect(str(self.socket))
            except (FileNotFoundError, ConnectionRefusedError):
                return None
            client.sendall((json.dumps(dict(action=action, value=value)) + '\n').encode())
            with client.makefile('rb') as stream:
                reply = stream.readline(8192)
            if not reply.endswith(b'\n'):
                raise OSError('Monitor volume service closed before the reply')
            return self.decode(reply)

    def _remember(self, state, estimated=None):
        volume, muted = state.get('volume_percent'), state.get('muted')
        if type(volume) is int and isinstance(muted, bool):
            self.value = percent(volume), muted
        elif state.get('hardware') is False and estimated is not None:
            self.value = estimated, estimated == 0

    def read(self):
        # The service atomically publishes accepted targets and confirmed
        # readbacks here. Reading does not queue a slow DDC transaction.
        try:
            with self.state.open('rb') as stream:
                info = os.fstat(stream.fileno())
                version = info.st_ino, info.st_mtime_ns
                if version != self.file_version:
                    self._remember(self.decode(stream.read(8192), response=False))
                    self.file_version = version
        except OSError:
            pass
        return self.value

    def set(self, value):
        desired = percent(value)
        response = self._socket('set', desired)
        if response is None or response.get('fallback') is True:
            response = self._cli('set', desired)
        self._remember(response, desired)

    def step(self, delta):
        if not delta:
            return
        current, _ = self.read()
        if self.file_version is not None:
            # The LG service adds one percentage point per desktop key step.
            # Its file includes pending targets so bursts can share one set.
            target = percent(current + delta)
            response = self._socket('set', target)
            if response is not None and response.get('fallback') is not True:
                self._remember(response)
                return
            if response is None:
                status = self._cli('status')
                if status.get('hardware') is True:
                    self._remember(status)
                    target = percent(self.value[0] + delta)
                    self._remember(self._cli('set', target), target)
                    return
        else:
            # A fresh session may not yet have observed a service state file.
            status = self._socket('status')
            if status and status.get('hardware') is True:
                self._remember(status)
                self.set(self.value[0] + delta)
                return
        # The helper owns sink detection and its relative fallback. Its
        # fallback reply has no volume readback, so retain the estimate.
        direction = 'inc' if delta > 0 else 'dec'
        for _ in range(abs(delta)):
            response = self._cli(direction)
            self._remember(response, percent(self.value[0] + (1 if delta > 0 else -1)))
