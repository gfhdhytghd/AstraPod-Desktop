"""Route allowlisted actions to the EP0 bridge or the guarded daemon."""
import math
import time

from usb_remote_key import host_fallback, usb_remote_key as bridge


PERMISSION = {
    'ipod.button': 'ep0',
    'keys.tap': 'keys',
    'voice.press': 'voice',
    'voice.release': 'voice',
    'voice.cancel': 'voice',
}
BUTTONS = {
    'menu': (bridge.MENU, (0, 1)), 'select': (bridge.SELECT, (0, 1)),
    'vol_up': (bridge.VOL_UP, (1, 2)), 'vol_down': (bridge.VOL_DOWN, (1, 2)),
}
KEY_COUNTS = {
    'bs': (1,), 'enter': (1,), 'left': (1, 3, 10), 'right': (1, 3, 10),
    'up': (1, 3), 'down': (1, 3),
}
VOICE = {'voice.press': 'press', 'voice.release': 'release', 'voice.cancel': 'discard'}


def finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _button(key, value):
    try:
        import usb.core
        import usb.util
    except ImportError as exc:
        raise ValueError('ipod.button requires PyUSB') from exc
    remote = bridge.USBRemote(usb.core, usb.util)
    try:
        return remote.send(value, time.monotonic(), key)
    finally:
        remote.close()


class Actions:
    def __init__(self, *, fallback=None, button=_button):
        self.fallback = host_fallback.Client() if fallback is None else fallback
        self.button = button

    def dispatch(self, permissions, request):
        action = request.get('action')
        if not isinstance(action, str) or action not in PERMISSION:
            raise ValueError('unknown action')
        permission = PERMISSION[action]
        if permission not in permissions:
            raise ValueError(f'permission required: {permission}')
        args = request.get('args')
        if not isinstance(args, dict):
            raise ValueError('expected action args object')
        if action == 'ipod.button':
            key, value = args.get('key'), args.get('value')
            if not isinstance(key, str) or key not in BUTTONS or type(value) is not int:
                raise ValueError('invalid EP0 key or value')
            button, values = BUTTONS[key]
            if args.keys() != {'key', 'value'} or value not in values:
                raise ValueError('invalid EP0 key or value')
            if not self.button(button, value):
                raise ValueError('EP0 unavailable')
            return

        lease = request.get('lease')
        if (not isinstance(lease, dict) or lease.keys() != {'id', 'seq', 'sent', 'token'}
                or type(lease['seq']) is not int or not 0 <= lease['seq'] < 2**64
                or not finite(lease['sent'])
                or not all(isinstance(lease[key], str) and len(lease[key]) == 32
                           for key in ('id', 'token'))):
            raise ValueError('a HerdRemote fallback lease is required')
        if action == 'keys.tap':
            key, count = args.get('key'), args.get('count')
            if (args.keys() != {'key', 'count'} or not isinstance(key, str)
                    or key not in KEY_COUNTS or type(count) is not int or count not in KEY_COUNTS[key]):
                raise ValueError('invalid key or count')
            op = 'key'
        else:
            if args:
                raise ValueError('voice actions take no arguments')
            op = VOICE[action]
        if self.fallback.exchange(dict(op=op, **lease, **args)).get('ok') is not True:
            raise ValueError('HerdRemote refused the action or did not reply')
