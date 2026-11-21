"""Version 1 JSON-lines framing and plugin-side stdio helpers."""
import json
import os
import sys
import time


API = 1
MAX_LINE = 4096
PHASES = ('down', 'repeat', 'up', 'tap')
STATES = ('running', 'waiting', 'error')


def encode(record):
    data = json.dumps(record, allow_nan=False, ensure_ascii=True,
                      separators=(',', ':')).encode('ascii') + b'\n'
    if len(data) > MAX_LINE:
        raise ValueError('JSON line exceeds 4096 bytes')
    return data


def decode(line):
    if len(line) > MAX_LINE or not line.endswith(b'\n'):
        raise ValueError('invalid JSON line length or terminator')
    record = json.loads(line.decode('utf-8'), parse_constant=_invalid_constant)
    if not isinstance(record, dict):
        raise ValueError('expected a JSON object')
    return record


def _invalid_constant(value):
    raise ValueError(f'invalid JSON number: {value}')


class Plugin:
    """Read initial settings, send input/status and exchange action requests."""

    def __init__(self, stdin=None, stdout=None):
        self.stdin = sys.stdin.buffer if stdin is None else stdin
        self.stdout = sys.stdout.buffer if stdout is None else stdout
        self.sequence = 0
        record = self.receive()
        if (record.get('type') != 'settings' or type(record.get('api')) is not int
                or record['api'] != API or not isinstance(record.get('settings'), dict)):
            raise ValueError('expected API 1 settings')
        self.settings = record['settings']
        self.source = record['source']

    def receive(self):
        line = self.stdin.readline(MAX_LINE + 1)
        if not line:
            raise EOFError('plugin host disconnected')
        return decode(line)

    def send(self, record):
        self.stdout.write(encode(record))
        self.stdout.flush()

    def input(self, key, phase, t=None):
        self.send(dict(type='input', key=key, phase=phase, source=self.source,
                       t=time.monotonic() if t is None else t))

    def status(self, state, device=None, detail='', **fields):
        self.send(dict(type='status', state=state, device=device, detail=detail, **fields))

    def request_action(self, action, *, lease=None, **args):
        self.sequence += 1
        record = dict(type='action', id=self.sequence, action=action, args=args)
        if lease is not None:
            record['lease'] = lease
        self.send(record)
        return self.sequence

    def action(self, action, *, lease=None, **args):
        request_id = self.request_action(action, lease=lease, **args)
        reply = self.receive()
        if reply.get('type') != 'action_result' or reply.get('id') != request_id:
            raise ValueError('unexpected action reply')
        return reply

    def try_send(self, record):
        """Publish one observation on an O_NONBLOCK pipe."""
        try:
            os.write(self.stdout.fileno(), encode(record))
            return True
        except (BlockingIOError, BrokenPipeError):
            return False
