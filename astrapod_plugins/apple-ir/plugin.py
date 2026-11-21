"""Observe decoded Apple IR input without changing bridge routing."""
import logging
import os
import select

from astrapod_plugins.sdk import Plugin
from usb_remote_key import usb_remote_key as bridge
from usb_remote_key.host_fallback import CODES


KEYS = {'menu': 'menu', 'centre': 'play', 'left': 'left', 'right': 'right',
        'plus': 'vol_up', 'minus': 'vol_down'}
# The aluminium remote sends Select (0x5c/0x5d) or Play/Pause (0x5e/0x5f) as a
# prefix frame, then a second frame whose code carries no meaning; a centre
# frame without a prefix therefore comes from the plastic remote.
PREFIXES = {0x5c: 'select', 0x5e: 'play'}
PAIR_SECONDS = 0.2


class Observations:
    def __init__(self, source, send, *, report_input=True, host_closed=lambda: False):
        self.source = source
        self.send = send
        self.report_input = report_input
        self.host_closed = host_closed
        self.key = None
        self.remote = None
        self.prefix = None
        self.last = 0.0
        self.device = None
        self.error = ''
        self.status = None

    def _input(self, phase, now):
        record = dict(type='input', key=self.key, phase=phase, source=self.source, t=now)
        if self.remote is not None:
            record['remote'] = self.remote
        self.send(record)

    def _release(self, now):
        if self.key is not None:
            self._input('up', now)
            self.key = None

    def _expire(self, now):
        if self.key is not None and now - self.last >= bridge.RELEASE_SECONDS:
            self._release(self.last + bridge.RELEASE_SECONDS)

    def poll(self, now, mode):
        if self.host_closed():
            raise SystemExit
        self._expire(now)
        state = 'running' if self.device else 'waiting'
        detail = self.error or ('' if self.device else 'Waiting for Apple IR receiver')
        status = dict(type='status', state=state, device=self.device, detail=detail,
                      mode=mode or 'pending')
        if status != self.status and self.send(status):
            self.status = status

    def receiver(self, path, now, error=''):
        self.device, self.error = path, error
        if path is None:
            self._expire(now)
            self._release(now)

    def frame(self, frame, now):
        if not self.report_input or frame is None:
            return
        self._expire(now)
        if not frame:
            if self.key is not None:
                self.last = now
                self._input('repeat', now)
            return
        self._release(now)
        prefix, self.prefix = self.prefix, None
        if len(frame) == 4 and frame[:2] == b'\xee\x87':
            if (frame[2] & 0xfe) in PREFIXES:
                self.prefix = (PREFIXES[frame[2] & 0xfe], now)
                return
            if prefix is not None and now - prefix[1] <= PAIR_SECONDS:
                self.key, self.remote = prefix[0], 'aluminium'
            else:
                self.key = KEYS.get(CODES.get(frame[2]))
                self.remote = 'plastic' if self.key == 'play' else None
            if self.key is not None:
                self.last = now
                self._input('down', now)


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    plugin = Plugin()
    os.set_blocking(plugin.stdout.fileno(), False)

    def host_closed():
        return bool(select.select([plugin.stdin], [], [], 0)[0])

    bridge.run(Observations(plugin.source, plugin.try_send,
                            report_input=plugin.settings['report_input'],
                            host_closed=host_closed))


if __name__ == '__main__':
    main()
