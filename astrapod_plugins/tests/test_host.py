"""Exercise the real host using private config and device-free subprocesses."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from astrapod_plugins import host as host_module
from astrapod_plugins.actions import Actions
from astrapod_plugins.host import PluginHost
from astrapod_plugins.manifest import discover, load_manifest


FAKE_PLUGIN = """
import sys
from astrapod_plugins.sdk import Plugin

plugin = Plugin()
plugin.status('running', device='fake')
plugin.input(plugin.settings['key'], 'tap')
if plugin.settings['action']:
    result = plugin.action('keys.tap', key='enter', count=1)
    plugin.status('running', device='fake', detail=result.get('error', 'accepted'))
if plugin.settings['crash']:
    raise SystemExit(4)
sys.stdin.read()
"""


def write_plugin(root, plugin_id='fake-input', **overrides):
    directory = root / plugin_id
    directory.mkdir(parents=True)
    (directory / 'plugin.py').write_text(FAKE_PLUGIN)
    manifest = {
        'id': plugin_id, 'name': 'Fake Input', 'kind': 'input', 'version': '1',
        'api': 1, 'entry': 'plugin.py', 'description': 'Fake process for tests.',
        'permissions': [], 'settings': {
            'key': {'type': 'string', 'default': 'select'},
            'crash': {'type': 'boolean', 'default': False},
            'action': {'type': 'boolean', 'default': False},
        }
    }
    path = directory / 'plugin.json'
    path.write_text(json.dumps(manifest | overrides))
    return path


class HostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=os.environ.get('XDG_RUNTIME_DIR'))
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, XDG_RUNTIME_DIR=str(self.root))
        env.start()
        self.addCleanup(env.stop)
        self.repository = self.root / 'repo'
        self.user = self.root / 'user'
        self.config = self.root / 'config' / 'plugins.json'
        self.path = write_plugin(self.repository)

    def host(self, actions=None):
        host = PluginHost(self.config, repo_dir=self.repository, user_dir=self.user,
                          actions=actions)
        self.addCleanup(host.stop)
        return host

    def wait_for(self, predicate):
        limit = time.monotonic() + 5
        while time.monotonic() < limit:
            if predicate():
                return
            time.sleep(.01)
        self.fail('condition did not become true')

    def test_manifest_validation(self):
        valid = json.loads(self.path.read_text())
        self.assertEqual(load_manifest(self.path).settings({})['key'], 'select')
        cases = [
            {'id': 'wrong'}, {'name': ''}, {'kind': 'output'}, {'api': True},
            {'api': 2}, {'entry': '/bin/sh'}, {'entry': 'missing.py'},
            {'permissions': ['shell']}, {'permissions': ['keys', 'keys']},
            {'settings': {'key': {'type': 'integer', 'default': True}}},
            {'settings': {'bad': {'type': 'unknown', 'default': ''}}},
            {'settings': {'key': {'type': 'string', 'default': 'x' * 4096}}},
            {'settings': {'key': {'type': 'string', 'default': 'c', 'enum': ['a', 'b']}}},
            {'settings': {'key': {'type': 'string', 'default': 'a', 'enum': 'ab'}}},
        ]
        outside = self.root / 'outside.py'
        outside.write_text('')
        self.path.with_name('link.py').symlink_to(outside)
        cases.append({'entry': 'link.py'})
        for override in cases:
            with self.subTest(override=override.keys()):
                self.path.write_text(json.dumps(valid | override))
                with self.assertRaises(ValueError):
                    load_manifest(self.path)

    def test_discovery_and_precedence(self):
        write_plugin(self.user, name='Shadow')
        write_plugin(self.user, plugin_id='user-input')
        write_plugin(self.user, plugin_id='invalid', api=2)
        with self.assertLogs('astrapod_plugins.manifest', 'WARNING') as messages:
            manifests = discover(self.repository, self.user)
        self.assertEqual(set(manifests), {'fake-input', 'user-input'})
        self.assertEqual(manifests['fake-input'].name, 'Fake Input')
        self.assertEqual(len(messages.output), 2)
        self.assertIn('apple-ir', discover(host_module.PACKAGE))

    def test_settings_persist_and_validate_atomically(self):
        host = self.host()
        host.set_settings('fake-input', {'key': 'pedal'})
        host.enable('fake-input', True)
        host.set_settings('fake-input', {'action': True})
        before = self.config.read_bytes()
        for values in ({'action': 1}, {'unknown': None}):
            with self.assertRaises(ValueError):
                host.set_settings('fake-input', values)
            self.assertEqual(self.config.read_bytes(), before)
        fresh = self.host()
        self.assertTrue(fresh.list()[0].enabled)
        self.assertEqual(fresh.settings('fake-input'),
                         {'key': 'pedal', 'action': True, 'crash': False})

    def test_supervision_settings_subscription_and_disable(self):
        host = self.host()
        events = []
        unsubscribe = host.subscribe(events.append)
        host.start()
        self.assertEqual(host.list()[0].state, 'disabled')
        host.enable('fake-input', True)
        self.wait_for(lambda: any(event['type'] == 'input' for event in events))
        first = host.list()[0]
        self.assertEqual((first.state, first.device), ('running', 'fake'))
        self.assertEqual(first.last_event['source'], 'fake-input')
        self.assertEqual(first.last_event['key'], 'select')

        self.host().set_settings('fake-input', {'key': 'pedal'})
        self.wait_for(lambda: host.list()[0].last_event['key'] == 'pedal')
        unsubscribe()
        count = len(events)
        host.enable('fake-input', False)
        self.assertEqual(host.list()[0].state, 'disabled')
        host.stop()
        self.assertEqual(len(events), count)

    def test_restart_backoff_and_single_owner(self):
        host = self.host()
        host.enable('fake-input', True)
        host.set_settings('fake-input', {'crash': True})
        errors = []
        starts = []

        def event(record):
            if record.get('state') == 'error':
                errors.append(record['detail'])
            if record.get('detail') == 'Starting plugin':
                starts.append(time.monotonic())

        host.subscribe(event)
        with patch.multiple(host_module, BACKOFF_MIN=.1, BACKOFF_MAX=.2):
            host.start()
            self.wait_for(lambda: len(errors) >= 3)
            self.assertEqual([s.rsplit('Retry in ', 1)[-1] for s in errors[:3]],
                             ['0.1 s', '0.2 s', '0.2 s'])
            self.assertGreaterEqual(starts[1] - starts[0], .1)
            self.assertGreaterEqual(starts[2] - starts[1], .2)
            host.stop()

        host.set_settings('fake-input', {'crash': False})
        host.start()
        self.wait_for(lambda: host.list()[0].state == 'running')
        standby = self.host()
        standby.start()
        self.wait_for(lambda: 'another host' in standby.list()[0].detail)
        host.stop()
        self.wait_for(lambda: standby.list()[0].state == 'running')

    def test_permission_refusal_in_child_protocol(self):
        daemon = Mock()
        button = Mock()
        host = self.host(Actions(fallback=daemon, button=button))
        host.enable('fake-input', True)
        host.set_settings('fake-input', {'action': True})
        host.start()
        self.wait_for(lambda: host.list()[0].detail == 'permission required: keys')
        daemon.exchange.assert_not_called()
        button.assert_not_called()

    def test_host_attributes_input_to_child_and_rejects_bad_record(self):
        host = self.host()
        self.path.with_name('plugin.py').write_text("""
from astrapod_plugins.sdk import Plugin

plugin = Plugin()
plugin.send(dict(type='input', source='apple-ir', key='pedal', phase='tap', t=1))
plugin.send(dict(type='input', key='pedal', phase='invalid', t=1))
plugin.receive()
""")
        host.enable('fake-input', True)
        host.start()
        self.wait_for(lambda: host.list()[0].state == 'error')
        row = host.list()[0]
        self.assertEqual(row.last_event['source'], 'fake-input')
        self.assertIn('invalid input event', row.detail)

    def test_host_passes_known_remote_model_only(self):
        host = self.host()
        events = []
        host.subscribe(events.append)
        self.path.with_name('plugin.py').write_text("""
from astrapod_plugins.sdk import Plugin

plugin = Plugin()
plugin.send(dict(type='input', key='select', phase='down', t=1, remote='aluminium'))
plugin.send(dict(type='input', key='play', phase='down', t=2, remote='wood'))
plugin.receive()
""")
        host.enable('fake-input', True)
        host.start()
        self.wait_for(lambda: sum(event['type'] == 'input' for event in events) == 2)
        inputs = [event for event in events if event['type'] == 'input']
        self.assertEqual(inputs[0]['remote'], 'aluminium')
        self.assertNotIn('remote', inputs[1])

    def test_force_stop_after_grace_period(self):
        self.path.with_name('plugin.py').write_text("""
import os
import signal
import sys
from astrapod_plugins.sdk import Plugin

plugin = Plugin()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
plugin.status('running', device=str(os.getpid()))
sys.stdin.read()
""")
        host = self.host()
        host.enable('fake-input', True)
        with patch.object(host_module, 'STOP_GRACE', .1):
            host.start()
            self.wait_for(lambda: host.list()[0].state == 'running')
            pid = int(host.list()[0].device)
            host.stop()
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_cli_lifecycle_with_user_plugin(self):
        write_plugin(self.root / 'astrapod/plugins')
        env = dict(os.environ, XDG_DATA_HOME=str(self.root))
        cmd = [sys.executable, '-m', 'astrapod_plugins', '--config', str(self.config)]

        def cli(*args):
            return subprocess.run([*cmd, *args], cwd=host_module.TOOLS, env=env,
                                  check=True, capture_output=True, text=True).stdout

        rows = json.loads(cli('list', '--json'))
        self.assertEqual({row['id']: row['enabled'] for row in rows},
                         {'apple-ir': False, 'fake-input': False})
        cli('enable', 'fake-input')
        log_path = self.root / 'events.jsonl'
        with log_path.open('wb') as output:
            process = subprocess.Popen([*cmd, 'run', '--events'], cwd=host_module.TOOLS,
                                       env=env, stdout=output, stderr=subprocess.DEVNULL)
            try:
                self.wait_for(lambda: '"key": "select"' in log_path.read_text())
                cli('disable', 'fake-input')
                self.wait_for(lambda: '"state": "disabled"' in log_path.read_text())
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        self.assertEqual(process.returncode, 0)
