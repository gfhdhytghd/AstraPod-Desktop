"""Handoff v1 contract: production C and Python over framed in-process PodLink."""
import ast
import ctypes
import logging
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else ROOT
sys.path[:0] = [str(ROOT / 'tools'), str(SOURCE / 'tools/herdremote')]
import hrhandoff
import podlink


def compile_firmware(root, out):
    # Share the hardware ports from the FU75 fixture. The firmware under test
    # comes from the nominated tree, including every formatter and parser.
    fixture = ast.parse((ROOT / 'tools/tests/fu75_handoff_test.py').read_text())
    ports = next(ast.literal_eval(node.value) for node in fixture.body
                 if isinstance(node, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == 'ports' for t in node.targets))
    source = (root / 'apps-ipod/screens/playback/handoff.c').read_text()
    source = re.sub(r'^#include .*$', '', source, flags=re.M)
    c = out / 'firmware.c'
    sheet = '#define TEST_FU76_SHEET\n' if 'handoff_sheet_run' in source else ''
    c.write_text(sheet + ports + source + r'''
void device_start(void) {
    handoff_init();
    cable = online = true;
    activity = ACTIVITY_WPS;
    status = AUDIO_STATUS_PLAY;
    answer = ACTION_STD_OK;
}
void device_clock(long tick, unsigned long position) {
    current_tick = tick;
    sample = position;
}
void device_begin(void) { handoff_begin(); }
void device_decline(void) { answer = ACTION_STD_MENU; }
void device_unplug(void) { cable = false; handoff_reset(); }
int device_step(void) { return handoff_step(); }
int device_state(void) { return (playing() ? 1 : 0) | (fade ? 2 : 0) | (active ? 4 : 0); }
const char *device_log(void) { return log_line; }
unsigned device_send(char *msg, unsigned len) { return handoff_next(msg, len); }
void device_receive(const unsigned char *msg, unsigned len) {
    struct podlink_message m = {PODLINK_TEXT, len, msg};
    assert(handoff_receive(&m));
}
''')
    # The FU75 UI port simulates an arbitrarily long dwell. Use real elapsed
    # time instead in the wire test, on both sides of the synthetic cable.
    c.write_text(c.read_text().replace('current_tick+=10000;', '')
                       .replace('current_tick += 10000;', ''))
    so = out / 'firmware.so'
    subprocess.run(['cc', '-std=gnu11', '-fPIC', '-shared', '-fsanitize=undefined',
                    '-fno-sanitize-recover=all', str(c), '-o', str(so)], check=True)
    return so


class Rufin:
    name = 'Rufin'

    def __init__(self):
        self.volume = .72
        self.position = self.anchor = 0
        self.playing = False
        self.volumes = []
        self.pauses = 0
        self.fail_seek = False

    def handoff_match(self, track):
        assert track == ('Song', 'Artist', 'Album', 180000)
        return ('mock',)

    def handoff_state(self):
        elapsed = max(0, hrhandoff.millis() - self.anchor)
        return dict(ids={'mock'}, playing=self.playing, loading=False, volume=self.volume,
                    position=self.position + elapsed // 100 * 100)

    def handoff_volume(self, value):
        self.volume = value
        self.volumes.append(value)

    def handoff_play(self, matched):
        assert self.volume == 0 and matched == ('mock',)
        self.playing = True

    def handoff_seek(self, position):
        if self.fail_seek:
            raise OSError('private mock error detail')
        self.position, self.anchor = position, hrhandoff.millis()

    def handoff_pause(self):
        self.pauses += 1
        self.playing = False


class Cable:
    def __init__(self, lib):
        self.firmware = ctypes.CDLL(str(lib))
        self.firmware.device_clock.argtypes = [ctypes.c_long, ctypes.c_ulong]
        self.firmware.device_log.restype = ctypes.c_char_p
        self.firmware.device_receive.argtypes = [ctypes.c_char_p, ctypes.c_uint]
        self.origin = hrhandoff.millis()
        self.player = Rufin()
        self.host = hrhandoff.Controller(lambda: self.player, lambda player: None)
        self.rx = [podlink.Decoder(), podlink.Decoder()]
        self.words = [[], []]
        self.out = ctypes.create_string_buffer(384)
        self.firmware.device_start()
        self.clock()
        self.send(0, hrhandoff.CAPABILITY)
        self.firmware.device_begin()

    def clock(self):
        dt = hrhandoff.millis() - self.origin
        self.firmware.device_clock(4200 + dt // 10, 17000 + dt)

    def send(self, side, payload):
        self.words[side].append(payload)
        packet = podlink.encode(podlink.TEXT, len(self.words[side]), payload)
        for start in range(0, len(packet), 17):
            for frame in self.rx[side].feed(packet[start:start + 17]):
                assert frame.type == podlink.TEXT
                if side:
                    assert self.host.command(frame.payload)
                else:
                    self.firmware.device_receive(frame.payload, len(frame.payload))

    def step(self, drop=None):
        self.clock()
        self.host.poll(lambda msg: self.send(0, msg))
        result = self.firmware.device_step()
        size = self.firmware.device_send(self.out, len(self.out))
        if size:
            payload = self.out.raw[:size]
            if drop is None or not payload.startswith(drop):
                self.send(1, payload)
        return result

    def until(self, condition, *, drop=None, timeout=7):
        expire = time.monotonic() + timeout
        while time.monotonic() < expire:
            self.step(drop)
            if condition():
                return
            time.sleep(.002)
        raise AssertionError(f'Timed out: {self.firmware.device_log()!r}, '
                             f'host={self.words[0]}, device={self.words[1]}')


def scenario(lib, mode):
    cable = Cable(lib)
    fw, player, host = cable.firmware, cable.player, cable.host
    try:
        if mode == 'decline':
            fw.device_decline()
            cable.until(lambda: b'outcome=declined' in fw.device_log())
            cable.until(lambda: host.proposal.finished)
            assert fw.device_state() == 1 and player.volumes == []
            assert [p.split(b'\t')[2] for p in cable.words[1]] == [b'q', b'x']
        elif mode == 'timeout':
            cable.until(lambda: b'outcome=timeout' in fw.device_log(), drop=b'hf\t1\tp\t')
            assert fw.device_state() == 1 and player.volumes == []
        elif mode == 'host-fail':
            player.fail_seek = True
            cable.until(lambda: b'outcome=host-failed' in fw.device_log())
            cable.until(lambda: host.proposal.finished)
            assert cable.words[0][-1].endswith(b'\tfail\thost')
            assert fw.device_state() == 1 and player.pauses == 1 and player.volume == .72
        elif mode == 'unplug':
            cable.until(lambda: any(0 < v < .72 for v in player.volumes))
            fw.device_unplug()
            host.disconnect()
            cable.until(lambda: host.retiring.finished)
            assert b'outcome=unplug' in fw.device_log()
            assert fw.device_state() == 1 and player.pauses == 1 and player.volume == .72
        else:
            cable.until(lambda: host.proposal is not None and host.proposal.finished)
            expected = [b'q', b'a', *([b'p'] * 5), b'g', b'v', b'd']
            assert [p.split(b'\t')[2] for p in cable.words[1]] == expected, (
                f'expected accept/sync/go/fade/done, got {cable.words[1]}; '
                f'host={cable.words[0]}; firmware={fw.device_log()!r}')
            assert [p.split(b'\t')[3] for p in cable.words[0][1:]] == (
                [b'offer', *([b'ping'] * 5), b'go', b'at', b'end'])
            for seq, (ping, reply) in enumerate(zip(cable.words[0][2:7], cable.words[1][2:7])):
                assert len(reply.split(b'\t')) == 9
                assert reply.split(b'\t')[4:6] == [ping.split(b'\t')[-1], str(seq).encode()]
            assert fw.device_state() == 4 and b'outcome=accepted' in fw.device_log()
            assert player.volumes == sorted(player.volumes) and player.volume == .72
        print(f'{mode}: {fw.device_log().decode().strip()}', flush=True)
    finally:
        host.close()


def main():
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    lib = compile_firmware(SOURCE, out)
    for mode in ('success', 'decline', 'timeout', 'unplug', 'host-fail'):
        # A new device instance for each virtual cable. dlopen coalesces by
        # path, so give each fixture an independent copy of the same build.
        fresh = out / f'{mode}.so'
        fresh.write_bytes(lib.read_bytes())
        scenario(fresh, mode)


if __name__ == '__main__':
    main()
