"""IR arbitration and Usage gestures. No direct keys, voice, USB or serial I/O."""
import json
import logging
import os
import secrets
import socket
import time

LOG = logging.getLogger('podbox-usbkey')
RELEASE = .150
TAP_WINDOW = .200
CODES = {2:'menu', 3:'menu', 4:'centre', 7:'right', 8:'left', 11:'plus', 13:'minus'}


class Client:
    def __init__(self, path=None, *, clock=time.monotonic):
        root = os.environ.get('XDG_RUNTIME_DIR', '/run/user/%d' % os.getuid())
        self.path = path or os.path.join(root, 'podbox-herdremote', 'disk.sock')
        self.clock = clock
        self.identity = secrets.token_hex(16)
        self.sequence = 0
        self.token = None

    def request(self, op, **fields):
        return self.exchange(dict(op=op, **self.credentials(op), **fields))

    def credentials(self, op='action'):
        self.sequence += 1
        fields = dict(id=self.identity, seq=self.sequence, sent=self.clock())
        if op != 'status':
            fields['token'] = self.token
        return fields

    def exchange(self, record):
        try:
            with socket.socket(socket.AF_UNIX) as channel:
                channel.settimeout(.05)
                channel.connect(self.path)
                channel.sendall(b'ir-' + json.dumps(record, separators=(',', ':')).encode('ascii') + b'\n')
                reply = b''
                while b'\n' not in reply and len(reply) < 512:
                    chunk = channel.recv(512-len(reply))
                    if not chunk:
                        return {}
                    reply += chunk
            value = json.loads(reply)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}  # Never retry an uncertain action or queue it for replay.

    def status(self, ep0, disk):
        reply = self.request('status', ep0=ep0, disk=disk)
        if reply.get('mode') in ('pending', 'ipod', 'host-fallback'):
            self.token = reply.get('token')
            return reply['mode']
        return None

    def action(self, op, **fields):
        return self.request(op, **fields)


class Gestures:
    def __init__(self, client):
        self.client = client
        self.key = None
        self.first = self.last = self.next_repeat = 0.0
        self.repeat_interval = .16
        self.repeats = self.taps = 0
        self.tap_at = 0.0

    def reset(self):
        if self.key == 'centre':
            self.client.action('discard')
        self.key, self.taps = None, 0

    def _end(self, now, discard=False):
        if self.key == 'centre':
            short = now - self.first < .3
            self.client.action('discard' if discard or short else 'release')
            if not discard and short:
                self.taps += 1
                self.tap_at = now + TAP_WINDOW
                if self.taps == 3:
                    self.client.action('noop', key='triple')
                    self.taps = 0
            else:
                self.taps = 0
        elif self.key == 'menu':
            self.client.action('noop', key='menu')
        self.key = None

    def poll(self, now):
        if self.key is not None and now - self.last >= RELEASE:
            # Firmware also ends the sampled remote hold on its silence timeout.
            self._end(self.last + RELEASE)
        if self.key == 'left' and now >= self.next_repeat:
            self.client.action('key', key='bs', count=1)
            self.next_repeat = now + self.repeat_interval
            self.repeat_interval = max(.05, self.repeat_interval-.01)
        if self.taps and self.key != 'centre' and now > self.tap_at:
            if self.taps == 1:
                self.client.action('key', key='enter', count=1)
            else:
                self.client.action('noop', key='double')
            self.taps = 0

    def frame(self, key, now, span=0.0):
        self.poll(now)
        if key == 'repeat':
            if self.key is None:
                return
            self.last = now
            if self.key in ('plus','minus'):
                self.repeats += 1
                if self.repeats > 1:
                    self._volume(max(0, now-self.first))
            return
        if key is None:
            return
        if self.key is not None:
            self._end(now, discard=key == 'menu')
        self.key, self.first, self.last = key, now-span if key in ('plus','minus') else now, now
        self.repeats = 0
        self.next_repeat, self.repeat_interval = now+.31, .16
        if key == 'centre':
            self.client.action('press')
        elif key == 'left':
            self.client.action('key', key='bs', count=1)
        elif key == 'right':
            self.taps = 0
            self.client.action('noop', key='right')
        elif key == 'menu':
            self.taps = 0
        elif key in ('plus','minus'):
            self._volume(0)

    def _volume(self, age):
        count = 1 if age < .5 else 3 if age < 1 else 10 if age < 4 else 1 if age < 7 else 3
        key = ('left' if self.key == 'plus' else 'right') if age < 4 else (
              'up' if self.key == 'plus' else 'down')
        self.client.action('key', key=key, count=count)


class Router:
    def __init__(self, remote, disk, forwarder, volume, client=None):
        self.remote, self.disk, self.forwarder, self.volume = remote, disk, forwarder, volume
        self.client = client or Client()
        self.gestures = Gestures(self.client)
        self.mode = 'ipod'
        self.reported_mode = None
        self.next_probe = 0.0
        self.held = None
        self.last = float('-inf')
        self.blocked = False

    def poll(self, now):
        self.disk.poll(now)
        if now - self.last >= RELEASE:
            self.held, self.blocked = None, False
        if now >= self.next_probe:
            self.next_probe = now + .1
            mode = self.client.status(bool(self.remote.probe(now)), bool(self.disk.active))
            self.reported_mode = mode
            mode = 'host-fallback' if mode == 'host-fallback' else 'ipod' if mode in ('ipod','pending') else self.mode
            if mode != self.mode:
                self.forwarder.release(now)
                self.volume.key = None
                self.gestures.reset()
                self.blocked = self.held is not None
                self.mode = mode
                LOG.info('IR mode %s', mode)
        if self.mode == 'host-fallback':
            self.gestures.poll(now)
        else:
            self.forwarder.poll(now)

    def frame(self, frame, now, span_seconds=0.0):
        self.poll(now)
        if frame is None:
            return
        key = ('repeat' if frame == b'' else CODES.get(frame[2])
               if len(frame) == 4 and frame[:2] == b'\xee\x87' else None)
        if key is None:
            self.disk.frame(frame, now, span_seconds)
            if self.mode == 'ipod':
                self.forwarder.frame(frame, now, span_seconds)
                self.volume.frame(frame, now, span_seconds)
            return
        if key == 'repeat' and self.held is None:
            return
        if key != 'repeat':
            # A new full frame starts a gesture, including repeated volume taps.
            self.held, self.blocked = key, False
        self.last = now
        # FU42 sees every MENU report, even while fallback owns the other keys.
        self.disk.frame(frame, now, span_seconds)
        if self.blocked:
            return
        if self.mode == 'host-fallback':
            if self.disk.active and self.held == 'menu':
                self.gestures.reset()  # MENU is exclusively host eject here.
            else:
                self.gestures.frame(key, now, span_seconds)
        else:
            self.forwarder.frame(frame, now, span_seconds)
            self.volume.frame(frame, now, span_seconds)

    def release(self, now):
        self.forwarder.release(now)
        self.volume.key = None
        self.gestures.reset()
        self.held, self.blocked = None, False
