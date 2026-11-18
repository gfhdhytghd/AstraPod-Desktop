"""Read the default sink monitor and publish bounded, best-effort WPS levels."""
import collections
import functools
import logging
import math
import os
import re
import struct
import subprocess

log = logging.getLogger(__name__)
CAPABILITY = 'spectrum1'
RATE = 48000
FREQUENCIES = (60, 150, 400, 1000, 2500, 4000, 7000, 12000)
GAINS = (177, 280, 229, 362, 572, 724, 958, 1254)
WINDOW = 1024
PCM_BYTES = WINDOW * 4
FRAME = struct.Struct('<4sIH16s')
MONITOR = ('parec', '--raw', '--verbose', '--device=@DEFAULT_MONITOR@',
           '--format=s16le', '--rate=48000', '--channels=2',
           '--latency-msec=20', '--process-time-msec=10',
           '--client-name=HerdRemote', '--stream-name=Playback spectrum')
LATENCY = re.compile(rb'Latency: (-?\d+) usec\.')
READ_LIMIT = 65536

try:
    dot = math.sumprod
except AttributeError:
    def dot(a, b):
        return sum(x * y for x, y in zip(a, b))


@functools.lru_cache(maxsize=1)
def filters():
    kernels = []
    for i, (frequency, gain) in enumerate(zip(FREQUENCIES, GAINS)):
        count = WINDOW if i < 2 else 256
        phase = [2 * math.pi * frequency * j / RATE for j in range(count)]
        kernels.append((tuple(map(math.cos, phase)), tuple(map(math.sin, phase)), gain))
    return kernels


def band_levels(pcm):
    """Same rectangular bins, window sizes, Q8 gains and log range as the iPod."""
    samples = struct.unpack('<2048h', pcm)
    levels = bytearray()
    for channel in (samples[::2], samples[1::2]):
        for cos, sin, gain in filters():
            window = channel[-len(cos):]
            raw = int(math.hypot(dot(window, cos), dot(window, sin)) / (len(cos) // 2))
            magnitude = min(32767, raw * gain // 256)
            level = int((math.log(max(1, magnitude)) - 4) * 100 / 6)
            levels.append(max(0, min(100, level)))
    return bytes(levels)


def _read(pipe):
    try:
        return os.read(pipe.fileno(), READ_LIMIT)
    except BlockingIOError:
        return b''


class Spectrum:
    """Nonblocking capture, owned by the PodLink loop. PCM never leaves memory."""
    def __init__(self):
        self.token = self.lease = 0
        self.process = None
        self.stopping = None
        self.retry_at = 0
        self.pcm = b''
        self.diagnostics = b''
        self.pending = collections.deque(maxlen=6)
        self.latency = 0.020
        self.sample_at = self.last_pcm_at = 0

    def subscribe(self, token, now):
        if self.token != token:
            self.stop(now)
        self.token, self.lease = token, now + 1.5

    def stop(self, now):
        self.pending.clear()
        self.pcm = self.diagnostics = b''
        if self.process is not None and self.stopping is None:
            self.process.terminate()
            self.stopping = now

    def reset(self, now):
        self.token = self.lease = 0
        self.retry_at = now
        self.stop(now)

    def close(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process.stdout.close()
            self.process.stderr.close()
            self.process = self.stopping = None

    def _capture(self, enabled, now):
        if not enabled:
            self.stop(now)
        if self.process is not None:
            if self.process.poll() is not None:
                if self.stopping is None:
                    log.debug('Playback spectrum monitor exited: %s', self.process.returncode)
                    self.retry_at = now + 2
                self.process.stdout.close()
                self.process.stderr.close()
                self.process = self.stopping = None
                self.pcm = self.diagnostics = b''
                self.pending.clear()
            elif self.stopping is not None:
                if now - self.stopping >= 0.25:
                    self.process.kill()
                return False
        if not enabled:
            return False
        if self.process is None and now >= self.retry_at:
            try:
                self.process = subprocess.Popen(
                    MONITOR, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, bufsize=0, env=dict(os.environ, LC_ALL='C'))
            except OSError as error:
                log.debug('Playback spectrum monitor unavailable: %s', error)
                self.retry_at = now + 2
                return False
            os.set_blocking(self.process.stdout.fileno(), False)
            os.set_blocking(self.process.stderr.fileno(), False)
            self.latency = 0.020
            self.sample_at = self.last_pcm_at = now
        return self.process is not None

    def poll(self, now, generation, playing, idle, delay):
        enabled = bool(self.token and now < self.lease and playing)
        if not self._capture(enabled, now):
            return None

        diagnostics = self.diagnostics + _read(self.process.stderr)
        lines = diagnostics.rsplit(b'\r', 1)
        self.diagnostics = lines[-1][-256:]
        if len(lines) == 2 and (matches := LATENCY.findall(lines[0])):
            self.latency = int(matches[-1]) / 1_000_000
        pcm = _read(self.process.stdout)
        if pcm:
            if now - self.last_pcm_at > .250:
                self.pcm = b''
                self.pending.clear()
            self.last_pcm_at = now
            self.pcm += pcm
            aligned = len(self.pcm) & ~3
            self.pcm = self.pcm[max(0, aligned - PCM_BYTES):]
        elif now - self.last_pcm_at > 1.0:
            self.stop(now)
            self.retry_at = now + 2
            return None

        # Read out the monitor even under pressure, but keep no unsent work
        # behind artwork, localstore, controls or a busy serial transmitter.
        one_way = delay() if idle else None
        if one_way is None or len(pcm) == READ_LIMIT:
            self.pending.clear()
            return None
        if pcm and len(self.pcm) >= PCM_BYTES and now - self.sample_at >= .030:
            self.sample_at = now
            # The middle of the long window belongs half a window before
            # its last sample. PA monitor latency is signed and includes
            # the sink delay: negative means the sound has yet to play.
            audible_at = now - self.latency - WINDOW / (2 * RATE)
            if now - .125 <= audible_at <= now + .200:
                while self.pending and self.pending[-1][0] >= audible_at:
                    self.pending.pop()
                self.pending.append((audible_at, band_levels(self.pcm[:PCM_BYTES])))

        # Send ahead by one half RTT and one iPod tick. If capture itself is
        # late, send immediately while still useful. Collapse overdue frames.
        arrives = now + one_way + .010
        newest = None
        while self.pending and self.pending[0][0] <= arrives + .010:
            audible_at, levels = self.pending.popleft()
            if arrives - audible_at <= .125:
                newest = levels
        if newest is not None:
            return FRAME.pack(b'CSP1', generation, self.token, newest)
