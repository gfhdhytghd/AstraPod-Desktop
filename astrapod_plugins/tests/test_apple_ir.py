"""Replay captured IR frames through the production decoder and observer."""
from contextlib import ExitStack
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
import os
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from astrapod_plugins.manifest import load_manifest
from astrapod_plugins.sdk import MAX_LINE, Plugin, encode
from usb_remote_key import disk_mode, host_fallback, usb_remote_key as bridge


TOOLS = Path(__file__).resolve().parents[2]
FIXTURES = TOOLS / 'usb_remote_key/tests/fixtures'
spec = spec_from_file_location('apple_ir', TOOLS / 'astrapod_plugins/apple-ir/plugin.py')
apple_ir = module_from_spec(spec)
spec.loader.exec_module(apple_ir)


class AppleIRTests(unittest.TestCase):
    def test_captured_menu_and_centre_frames(self):
        decoder = bridge.NECDecoder()
        records = []
        observer = apple_ir.Observations('apple-ir', records.append)
        for line in (FIXTURES / 'ir-gestures-raw.log').read_bytes().splitlines():
            stamp, payload = line.split(maxsplit=1)
            now = float(stamp)
            frame = decoder.line(payload)
            if frame is not None:
                observer.frame(frame, now)
        observer.poll(now + 1, 'ipod')
        inputs = [r for r in records if r['type'] == 'input']
        self.assertEqual(sum(r['key'] == 'menu' and r['phase'] == 'down' for r in inputs), 22)
        self.assertEqual(sum(r['key'] == 'play' and r['phase'] == 'down' for r in inputs), 21)
        self.assertEqual(sum(r['phase'] == 'up' for r in inputs), 43)
        self.assertGreater(sum(r['phase'] == 'repeat' for r in inputs), 0)
        self.assertTrue(all(r['source'] == 'apple-ir' for r in inputs))
        self.assertEqual(sorted(r['t'] for r in inputs), [r['t'] for r in inputs])

    def test_physical_keys_repeat_release_and_receiver_loss(self):
        records = []
        observer = apple_ir.Observations('apple-ir', lambda r: records.append(r) or True)
        observer.receiver('receiver', 1.)
        observer.poll(1., 'pending')
        observer.frame(b'', 1.)
        observer.frame(bytes.fromhex('ee870b59'), 2.)
        observer.frame(b'', 2.1)
        observer.poll(2.251, 'ipod')
        observer.frame(bytes.fromhex('ee870d59'), 3.)
        observer.frame(bytes.fromhex('ee870859'), 3.1)
        observer.frame(bytes.fromhex('ee870759'), 3.2)
        observer.receiver(None, 3.3, 'gone')
        observer.poll(3.3, None)
        self.assertEqual([(r['key'], r['phase']) for r in records if r['type'] == 'input'],
                         [('vol_up', 'down'), ('vol_up', 'repeat'), ('vol_up', 'up'),
                          ('vol_down', 'down'), ('vol_down', 'up'), ('left', 'down'),
                          ('left', 'up'), ('right', 'down'), ('right', 'up')])
        self.assertAlmostEqual(records[3]['t'], 2.25)
        self.assertEqual(records[-1], dict(type='status', state='waiting', device=None,
                                          detail='gone', mode='pending'))

    def test_aluminium_prefix_frames_name_the_remote(self):
        records = []
        observer = apple_ir.Observations('apple-ir', lambda r: records.append(r) or True)
        frames = [
            (1.0, 'ee875c59'), (1.1, 'ee870559'), (1.2, ''),   # aluminium Select, held
            (2.0, 'ee875f59'), (2.1, 'ee870459'),              # aluminium Play/Pause
            (3.0, 'ee870459'),                                 # plastic centre
            (4.0, 'ee875d59'), (4.5, 'ee870459'),              # prefix too old to pair
            (5.0, 'ee875e59'), (5.5, 'ee870259'),              # lost pair, then Menu
        ]
        for now, frame in frames:
            observer.frame(bytes.fromhex(frame), now)
        observer.poll(6., 'ipod')
        inputs = [(r['key'], r['phase'], r.get('remote'))
                  for r in records if r['type'] == 'input']
        self.assertEqual(inputs, [
            ('select', 'down', 'aluminium'), ('select', 'repeat', 'aluminium'),
            ('select', 'up', 'aluminium'), ('play', 'down', 'aluminium'),
            ('play', 'up', 'aluminium'), ('play', 'down', 'plastic'), ('play', 'up', 'plastic'),
            ('play', 'down', 'plastic'), ('play', 'up', 'plastic'),
            ('menu', 'down', None), ('menu', 'up', None)])

    def test_remote_setting_is_an_enum(self):
        manifest = load_manifest(TOOLS / 'astrapod_plugins/apple-ir/plugin.json')
        self.assertEqual(manifest.settings({})['remote'], 'auto')
        self.assertEqual(manifest.settings({'remote': 'plastic'})['remote'], 'plastic')
        with self.assertRaises(ValueError):
            manifest.settings({'remote': 'wood'})

    def test_reporting_setting_does_not_hide_status(self):
        records = []
        observer = apple_ir.Observations('apple-ir', lambda r: records.append(r) or True,
                                        report_input=False)
        observer.receiver('receiver', 1.)
        observer.frame(bytes.fromhex('ee870259'), 1.)
        observer.poll(1., 'host-fallback')
        observer.poll(1.5, 'host-fallback')
        self.assertEqual(records, [dict(type='status', state='running', device='receiver',
                                        detail='', mode='host-fallback')])

    def test_full_observation_pipe_drops_input(self):
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        with os.fdopen(writer, 'wb', buffering=0) as stream:
            os.set_blocking(writer, False)
            settings = encode(dict(type='settings', api=1, source='apple-ir', settings={}))
            plugin = Plugin(stdin=BytesIO(settings), stdout=stream)
            try:
                while True:
                    os.write(writer, b' ' * MAX_LINE)
            except BlockingIOError:
                pass
            self.assertFalse(plugin.try_send(dict(type='input', key='play', phase='down', t=1)))

    def test_bridge_with_observer_keeps_ep0_and_fallback_delivery(self):
        lines = (FIXTURES / 'ir-gestures-raw.log').read_bytes().splitlines()
        edges = [line.split(maxsplit=1)[1] for line in lines if b' EDGE ' in line]
        payload = b'\n'.join(edges[:68]) + b'\n'

        def replay(mode, observer):
            now = 100.
            handlers = {}
            remote = Mock()
            remote.probe.return_value = True
            disk = Mock(active=False)
            client = Mock()
            client.status.return_value = mode

            def readable(*args):
                nonlocal now
                if now == 100. and remote.send.call_count == 0:
                    return ([1234], [], [])
                if now == 100.:
                    now = 100.2
                else:
                    handlers[bridge.signal.SIGTERM](bridge.signal.SIGTERM, None)
                return ([], [], [])

            usb = ModuleType('usb')
            usb.core, usb.util = ModuleType('usb.core'), ModuleType('usb.util')
            with ExitStack() as stack:
                stack.enter_context(patch.dict(sys.modules, {
                    'usb': usb, 'usb.core': usb.core, 'usb.util': usb.util}))
                stack.enter_context(patch.object(bridge, 'receiver_path', return_value='receiver'))
                stack.enter_context(patch.object(bridge, 'open_receiver', return_value=1234))
                stack.enter_context(patch.object(bridge, 'USBRemote', return_value=remote))
                stack.enter_context(patch.object(disk_mode, 'DiskMode', return_value=disk))
                stack.enter_context(patch.object(host_fallback, 'Client', return_value=client))
                stack.enter_context(patch.object(bridge.signal, 'signal', side_effect=handlers.__setitem__))
                stack.enter_context(patch.object(bridge.os, 'read', return_value=payload))
                stack.enter_context(patch.object(bridge.os, 'close'))
                stack.enter_context(patch.object(bridge.select, 'select', side_effect=readable))
                stack.enter_context(patch.object(bridge.time, 'monotonic', side_effect=lambda: now))
                stack.enter_context(patch.object(bridge.time, 'sleep'))
                bridge.run(observer)
            return remote.send.call_args_list, client.action.call_args_list

        for mode in ('ipod', 'host-fallback'):
            with self.subTest(mode=mode):
                records = []
                observer = apple_ir.Observations('apple-ir', lambda r: records.append(r) or True)
                expected = replay(mode, None)
                self.assertEqual(replay(mode, observer), expected)
                self.assertEqual([(r['key'], r['phase']) for r in records if r['type'] == 'input'],
                                 [('menu', 'down'), ('menu', 'up')])
                usb_commands, fallback_commands = expected
                self.assertEqual([call.args[2] for call in usb_commands],
                                 [bridge.HEARTBEAT, bridge.MENU, bridge.MENU]
                                 if mode == 'ipod' else [bridge.HEARTBEAT])
                self.assertEqual([call.args for call in fallback_commands],
                                 [] if mode == 'ipod' else [('noop',)])
