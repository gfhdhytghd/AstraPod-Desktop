import math
import struct
from unittest.mock import Mock

import pytest

import hrcider
import hrspectrum as spectrum


def sine(left, right=0, amplitude=1200):
    return struct.pack('<2048h', *(
        int(amplitude * math.sin(math.tau * hz * i / spectrum.RATE))
        for i in range(spectrum.WINDOW) for hz in (left, right)))


@pytest.mark.parametrize('band', range(8))
def test_sine_sweep_and_channel_layout(band):
    expected_levels = [45, 52, 48, 57, 64, 68, 73, 77]
    levels = spectrum.band_levels(sine(spectrum.FREQUENCIES[band],
                                      spectrum.FREQUENCIES[7 - band]))
    assert levels.index(max(levels[:8])) == band
    assert levels[8:].index(max(levels[8:])) == 7 - band
    assert levels[band] == expected_levels[band]
    assert levels[15 - band] == expected_levels[7 - band]
    assert spectrum.band_levels(sine(0, spectrum.FREQUENCIES[band]))[:8] == bytes(8)


def test_silence_and_clipping():
    assert spectrum.band_levels(bytes(spectrum.PCM_BYTES)) == bytes(16)
    assert spectrum.band_levels(sine(12000, amplitude=32767))[7] == 100


def test_versioned_lease_and_scheduler_priority(tmp_path):
    bridge = hrcider.Bridge(client=Mock(), cache=hrcider.Cache(tmp_path), volume=Mock())
    bridge.spectrum = Mock(process=None)
    bridge.spectrum.poll.return_value = None
    sent = []
    send = lambda *frame: sent.append(frame)
    try:
        # An old firmware gets neither the capability response nor capture.
        assert bridge.command(b'ci\t1\t131072\tcn18,loading')
        bridge.poll(send)
        assert not any(b'spectrum1' in body for _, body in sent)
        bridge.command(f'cs\t{bridge.generation}\t1'.encode())
        bridge.spectrum.subscribe.assert_not_called()
        bridge.spectrum.poll.assert_not_called()

        assert bridge.command(b'ci\t1\t131072\tneighbors,warm,play-ack,localstore,remote-name,loading,localdiag-live,spectrum1')
        gen = bridge.generation
        bridge.poll(send)
        bridge.poll(send)
        assert (2, f'CF\t{gen}\tspectrum1'.encode()) in sent
        for invalid in (f'cs\t{gen + 1}\t1', f'cs\t{gen}\t65536', f'cs\t{gen}\t-1',
                        f'cs\t{gen}\t1\textra'):
            bridge.command(invalid.encode())
        bridge.spectrum.subscribe.assert_not_called()
        bridge.command(f'cs\t{gen}\t65535'.encode())
        assert bridge.spectrum.subscribe.call_args.args[0] == 65535
        bridge.command(f'cs\t{gen}\t0'.encode())
        assert bridge.spectrum.subscribe.call_args.args[0] == 0

        bridge.state = {'rate': 1000}
        bridge.available = True
        bridge.scheduler.push(4, 3, b'art waiting for credit', bulk=True)
        bridge.poll(send)
        assert bridge.spectrum.poll.call_args.args[1:4] == (gen, True, False)
        bridge.scheduler.grant(100)
        bridge.poll(send)
        assert sent[-1] == (3, b'art waiting for credit')
        assert bridge.spectrum.poll.call_args.args[3] is False
        bridge.spectrum.poll.return_value = spectrum.FRAME.pack(b'CSP1', gen, 1, bytes(16))
        bridge.poll(send)
        assert bridge.spectrum.poll.call_args.args[3] is True
        assert sent[-1] == (3, bridge.spectrum.poll.return_value)
        assert len(sent[-1][1]) == 26
        bridge.disconnect()
        bridge.spectrum.close.assert_called_once()
    finally:
        bridge.close()


class SyntheticMonitor:
    def __init__(self, monkeypatch):
        self.process = Mock(returncode=None)
        self.process.poll.return_value = None
        self.popen = Mock(return_value=self.process)
        self.pcm = b''
        self.diagnostics = b''
        monkeypatch.setattr(spectrum.subprocess, 'Popen', self.popen)
        monkeypatch.setattr(spectrum.os, 'set_blocking', Mock())
        monkeypatch.setattr(spectrum, '_read', self.read)

    def read(self, pipe):
        if pipe is self.process.stdout:
            result, self.pcm = self.pcm, b''
        else:
            result, self.diagnostics = self.diagnostics, b''
        return result


def test_capture_play_pause_visibility_and_lease(monkeypatch):
    monitor = SyntheticMonitor(monkeypatch)
    capture = spectrum.Spectrum()

    def poll(now, playing=True, idle=True, delay=.01):
        return capture.poll(now, 55, playing, idle, lambda: delay)

    assert poll(10.0) is None
    assert not monitor.popen.called
    capture.subscribe(7, 10.0)
    assert poll(10.0, playing=False) is None
    assert not monitor.popen.called
    assert poll(10.0) is None
    assert '--device=@DEFAULT_MONITOR@' in monitor.popen.call_args.args[0]

    # The sink still has 100ms pending. No early bars when that is the case.
    monitor.diagnostics = b'Time: 1.000 sec; Latency: -100000 usec.\r'
    monitor.pcm = sine(1000, 4000)
    assert poll(10.04) is None
    assert poll(10.08) is None
    frame = poll(10.10, delay=.03)
    assert spectrum.FRAME.unpack(frame) == (b'CSP1', 55, 7, spectrum.band_levels(sine(1000, 4000)))
    assert poll(10.12) is None

    monitor.diagnostics = b'Time: 1.100 sec; Latency: 10000 usec.\r'
    monitor.pcm = sine(1000, 4000)
    assert poll(10.14, idle=False) is None
    assert poll(10.16) is None  # pressured frame is discarded
    monitor.pcm = sine(1000, 4000)
    assert poll(10.20, delay=None) is None
    monitor.pcm = sine(1000, 4000)
    assert poll(10.24, delay=.2) is None  # already too late across PodLink

    assert poll(10.28, playing=False) is None
    monitor.process.terminate.assert_called_once()
    monitor.process.poll.return_value = 0
    assert poll(10.30, playing=False) is None
    capture.subscribe(0, 10.30)
    assert poll(10.31) is None
    assert monitor.popen.call_count == 1
    capture.subscribe(8, 10.40)
    monitor.process.poll.return_value = None
    assert poll(10.40) is None
    assert monitor.popen.call_count == 2
    assert poll(11.91) is None
    assert monitor.process.terminate.call_count == 2
