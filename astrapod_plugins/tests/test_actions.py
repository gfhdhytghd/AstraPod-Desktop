"""Check the allowed action arguments and HerdRemote's real lease guard."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

from astrapod_plugins.actions import Actions
from usb_remote_key.host_fallback import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'herdremote'))
import hrdisk
import hrfallback


class ActionsTests(unittest.TestCase):
    def test_ep0_values_and_permission(self):
        button = Mock(return_value=True)
        actions = Actions(button=button)
        request = dict(action='ipod.button', args={'key': 'select', 'value': 0})
        with self.assertRaisesRegex(ValueError, 'permission required: ep0'):
            actions.dispatch(('usb',), request)
        with self.assertRaisesRegex(ValueError, 'invalid EP0'):
            actions.dispatch(('ep0',), request | {'args': {'key': 'select', 'value': 2}})
        button.assert_not_called()
        actions.dispatch(('ep0',), request)
        button.assert_called_once_with(2, 0)

    def test_key_and_voice_relay_preserves_daemon_lease_guard(self):
        now = 10.
        keys = Mock()
        keys.request.return_value = None
        voice = Mock(engaged=False)
        voice.press.return_value = (True, '')
        executor = hrfallback.Executor(keys, voice, clock=lambda: now)
        with tempfile.TemporaryDirectory(dir=os.environ.get('XDG_RUNTIME_DIR')) as root:
            control = hrdisk.Control(str(Path(root) / 'hr' / 'disk.sock'), fallback=executor)
            control.start()
            stop = threading.Event()

            def serve():
                while not stop.is_set():
                    control.poll(None)
                    stop.wait(.001)

            listener = threading.Thread(target=serve)
            listener.start()
            try:
                owner = Client(control.path, clock=lambda: now)
                actions = Actions(fallback=Client(control.path))
                for moment, mode in ((10., 'pending'), (10.5, 'pending'), (11., 'host-fallback')):
                    now = moment
                    self.assertEqual(owner.status(False, False), mode)
                request = dict(action='keys.tap', args={'key': 'enter', 'count': 1},
                               lease=owner.credentials())
                actions.dispatch(('keys',), request)
                self.assertEqual(keys.request.call_args.args, ('', 'enter', 1))
                self.assertTrue(keys.request.call_args.kwargs['guard']())
                keys.request.reset_mock()
                with self.assertRaisesRegex(ValueError, 'HerdRemote refused'):
                    actions.dispatch(('keys',), request)
                forged = owner.credentials() | {'token': '0' * 32}
                with self.assertRaisesRegex(ValueError, 'HerdRemote refused'):
                    actions.dispatch(('keys',), request | {'lease': forged})
                keys.request.assert_not_called()

                called = threading.Event()
                voice.press.side_effect = lambda target: (called.set() or True, '')
                actions.dispatch(('voice',), dict(action='voice.press', args={},
                                                  lease=owner.credentials()))
                self.assertTrue(called.wait(1))
                voice.press.assert_called_once_with('-')
                discarded = threading.Event()
                voice.discard.side_effect = discarded.set
                actions.dispatch(('voice',), dict(action='voice.cancel', args={},
                                                  lease=owner.credentials()))
                self.assertTrue(discarded.wait(1))
            finally:
                stop.set()
                listener.join()
                control.close()
