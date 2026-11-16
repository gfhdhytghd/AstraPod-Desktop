"""Local IR fallback authorization and existing keyboard/voice executors."""
import json
import logging
import math
import re
import secrets
import threading
import time

log = logging.getLogger('herdremote.ir')
DEBOUNCE = 1.0
LEASE = .75
MAX_AGE = .1
ID = re.compile('[0-9a-f]{32}\\Z')


class Executor:
    def __init__(self, keys, voice, *, clock=time.monotonic):
        self.keys, self.voice, self.clock = keys, voice, clock
        self.owner = None
        self.sequence = -1
        self.expires = 0.0
        self.bad_since = None
        self.mode = 'pending'
        self.token = secrets.token_hex(16)
        self.condition = threading.Condition(threading.RLock())
        self.talking = False
        self.stop_kind = None
        self.closed = False
        self.ep0 = self.disk = False
        self.link = None

    @staticmethod
    def ready(link):
        return bool(link is not None and link.opened)

    @property
    def active(self):
        return self.mode == 'host-fallback' and self.clock() < self.expires and not self.closed

    def _invalidate(self, mode='pending'):
        if self.mode == 'host-fallback':
            self.keys.reset()  # Cancel only our work; leave FU43 requests alone.
        self.mode = mode
        self.token = secrets.token_hex(16)
        if self.talking:
            self.stop_kind = 'discard'
            self.condition.notify_all()
        elif mode == 'host-fallback' and self.voice.engaged:
            # EP0 can fail while CDC/Dock still work. Cancel an iPod-owned
            # recording before admitting direct IR voice; never borrow it.
            self.talking = True
            threading.Thread(target=self._clear_old_voice, daemon=True,
                             name='ir-voice-handoff').start()

    def valid(self, token, sent):
        now = self.clock()
        return (not self.closed and not (self.ep0 and not self.disk and self.ready(self.link)) and
                self.mode == 'host-fallback' and
                token == self.token and now < self.expires and
                0 <= now - sent < MAX_AGE)

    def handle(self, raw, link):
        try:
            message = json.loads(raw[3:])  # ir- followed by one JSON record
            if not isinstance(message, dict):
                raise ValueError()
            sent, sequence = message['sent'], message['seq']
            owner = message['id']
            now = self.clock()
            if (not isinstance(owner, str) or not ID.fullmatch(owner) or
                    type(sequence) is not int or sequence < 0 or
                    type(sent) not in (int, float) or not math.isfinite(sent) or
                    not 0 <= now - sent < MAX_AGE):
                raise ValueError()
            with self.condition:
                self.link = link
                if self.closed:
                    raise ValueError()
                if self.owner != owner:
                    if now < self.expires:
                        raise ValueError()
                    self._invalidate()
                    self.owner, self.sequence, self.bad_since = owner, -1, None
                if sequence <= self.sequence:
                    raise ValueError()
                self.sequence = sequence
                if message['op'] == 'status':
                    if type(message['ep0']) is not bool or type(message['disk']) is not bool:
                        raise ValueError()
                    if now >= self.expires:
                        self._invalidate()
                        self.bad_since = None
                    self.expires = now + LEASE
                    self.ep0, self.disk = message['ep0'], message['disk']
                    good = self.ep0 and not self.disk and self.ready(link)
                    if good:
                        self.bad_since = None
                        if self.mode != 'ipod':
                            self._invalidate('ipod')
                    else:
                        if self.bad_since is None:
                            self.bad_since = now
                        if now - self.bad_since >= DEBOUNCE and self.mode != 'host-fallback':
                            self._invalidate('host-fallback')
                    return self._reply(mode=self.mode, token=self.token)
                if not self.valid(message.get('token'), sent):
                    raise ValueError()
                op = message['op']
                if op == 'key':
                    key, count = message['key'], message['count']
                    if type(count) is not int or key not in ('bs', 'enter', 'left', 'right', 'up', 'down'):
                        raise ValueError()
                    if count not in ((1,3) if key in ('up','down') else (1,3,10)) or (key in ('bs','enter') and count != 1):
                        raise ValueError()
                    result = self.keys.request('', key, count, scaled=False, silent=True,
                        guard=lambda token=self.token, sent=sent: self.valid(token, sent))
                    if result is not None:
                        return self._reply(ok=False)
                    log.info('host-fallback action key-%s count=%d', key, count)
                elif op == 'press':
                    if self.talking:
                        return self._reply(ok=False)
                    self.talking, self.stop_kind = True, None
                    threading.Thread(target=self._talk, args=(self.token,), daemon=True,
                                     name='ir-voice').start()
                    log.info('host-fallback action voice-start')
                elif op in ('release', 'discard'):
                    if self.talking:
                        self.stop_kind = op
                        self.condition.notify_all()
                elif op == 'noop' and message.get('key') in ('menu','right','double','triple'):
                    log.info('host-fallback action noop-%s', message['key'])
                else:
                    raise ValueError()
                return self._reply(ok=True)
        except (ValueError, KeyError, TypeError, UnicodeError):
            return self._reply(ok=False)

    @staticmethod
    def _reply(**fields):
        return json.dumps(fields, separators=(',', ':')).encode('ascii') + b'\n'

    def _clear_old_voice(self):
        try:
            self.voice.discard()
            log.info('host-fallback action voice-handoff-discard')
        finally:
            with self.condition:
                self.talking = False
                self.condition.notify_all()

    def _talk(self, token):
        try:
            # Usage has zero selectable rows, therefore its vd target is '-'.
            # Use exactly the configured HoldToTalk object and current focus.
            ok, _ = self.voice.press('-')
            with self.condition:
                while (ok and not self.stop_kind and not self.closed and
                       token == self.token and self.mode == 'host-fallback' and
                       self.clock() < self.expires):
                    self.condition.wait(max(.01, self.expires - self.clock()))
                kind = self.stop_kind
                valid = not self.closed and token == self.token and self.clock() < self.expires
            if kind == 'release' and valid:
                self.voice.release()
                log.info('host-fallback action voice-release')
            else:
                self.voice.discard()  # Lost bridge, mode change, short tap, MENU.
                log.info('host-fallback action voice-discard')
        finally:
            with self.condition:
                self.talking, self.stop_kind = False, None
                self.condition.notify_all()

    def close(self):
        with self.condition:
            owned = self.talking
            self.closed = True
            self._invalidate()
        if owned:
            # Complete cancellation before Daemon's generic voice.stop() can
            # submit the recording. HoldToTalk serializes concurrent stops.
            self.voice.discard()
