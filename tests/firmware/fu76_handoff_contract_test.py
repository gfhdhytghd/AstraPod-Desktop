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



PCM_PORTS = r'''
#define MIX_AMP_UNITY 65536
#define PCM_MIXER_CHAN_PLAYBACK 0
#define PCMBUF_SAMPLE_SIZE 4
static unsigned int handoff_gain = MIX_AMP_UNITY;
static unsigned long handoff_start;
static volatile unsigned long handoff_at;
static bool handoff_fading, handoff_hold, handoff_ended;
struct chunkdesc { unsigned long elapsed; unsigned size; int pos_key; };
static struct chunkdesc desc = {.pos_key = 1}, *current_desc = &desc;
static void (*timer)(void);
static void tick_add_task(void (*handler)(void)) { timer = handler; fade = handoff_start; }
static void tick_remove_task(void (*handler)(void))
{ assert(timer == handler); timer = NULL; fade = 0; }
static unsigned int mixer_channel_get_bytes_waiting(int channel) { (void)channel; return 0; }
static unsigned int mixer_get_frequency(void) { return 44100; }
static void pcm_play_lock(void) {}
static void pcm_play_unlock(void) {}
static void pcmbuf_update_volume(void) {}
static void audio_pcmbuf_track_change(bool value) { (void)value; }
static bool audio_pcmbuf_may_play(void) { return false; }
static void pcmbuf_play_start(void) {}
'''

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
    baseline = 'pcmbuf_handoff_at' not in source
    pcm = ''
    if not baseline:
        audio = (root / 'apps-ipod/audio/pcmbuf.c').read_text()
        begin = audio.index('static unsigned long handoff_position(')
        pcm = PCM_PORTS + audio[begin:audio.index('#endif', audio.index(
            'unsigned long pcmbuf_handoff_position(', begin))]
    c.write_text(('#define TEST_BASELINE\n' if baseline else '#define TEST_REAL_PCM\n') +
                 sheet + ports + pcm + source + r'''
void device_start(void) {
    handoff_init();
    cable = online = true;
    activity = ACTIVITY_WPS;
    status = AUDIO_STATUS_PLAY;
    answer = ACTION_STD_OK;
}
void device_clock(long tick, unsigned long position) {
#ifdef TEST_REAL_PCM
    while (timer && current_tick < tick)
    {
        current_tick++;
        desc.elapsed = position - (tick - current_tick) * 10;
        timer();
    }
    desc.elapsed = position;
#else
    if (fade && captured == -1ul && tick >= (long)fade)
        captured = position - (tick - fade) * 10;
#endif
    current_tick = tick;
    sample = position;
}
void device_begin(void) { handoff_begin(); }
void device_decline(void) { answer = ACTION_STD_MENU; }
void device_wait(void) { answer = -1; }
void device_accept(void) { answer = ACTION_STD_OK; }
void device_unplug(void) { cable = false; handoff_reset(); }
int device_step(void) { return handoff_step(); }
int device_preparing(void) { return sheet.preparing; }
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
                       .replace('current_tick += 10000;', '')
                       .replace('result = advance(answer == ACTION_STD_OK ? DIALOG_ACCEPT : DIALOG_CANCEL);',
                                'result = advance(answer == -1 ? DIALOG_CONTINUE : '
                                'answer == ACTION_STD_OK ? DIALOG_ACCEPT : DIALOG_CANCEL);'))
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
        self.match_delay = 0

    def handoff_match(self, track):
        assert track == ('Song', 'Artist', 'Album', 180000)
        if self.match_delay:
            time.sleep(self.match_delay)
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


class SlowRufin(Rufin):
    handoff_position_period_ms = 500

    def handoff_seek(self, position):
        self.prior = super().handoff_state()['position']
        self.echo_at = hrhandoff.millis() + (65 if self.anchor else 300)
        self.anchor = hrhandoff.millis() + 300
        self.position = position

    def handoff_state(self):
        state = super().handoff_state()
        if self.anchor:
            now = hrhandoff.millis()
            state['position'] = self.prior if now < self.echo_at else (
                self.position + max(0, now // 500 * 500 - self.anchor))
        return state


class Cable:
    def __init__(self, lib, slow=False, busy=False, match_delay=0):
        self.firmware = ctypes.CDLL(str(lib))
        self.firmware.device_clock.argtypes = [ctypes.c_long, ctypes.c_ulong]
        self.firmware.device_log.restype = ctypes.c_char_p
        self.firmware.device_receive.argtypes = [ctypes.c_char_p, ctypes.c_uint]
        self.origin = hrhandoff.millis()
        self.player = SlowRufin() if slow else Rufin()
        self.player.match_delay = match_delay
        self.slow = slow or busy
        self.busy = busy
        self.next_ui = 0
        self.flight = []
        self.host = hrhandoff.Controller(lambda: self.player, lambda player: None)
        self.rx = [podlink.Decoder(), podlink.Decoder()]
        self.words = [[], []]
        self.sent_at = [[], []]
        self.out = ctypes.create_string_buffer(384)
        self.firmware.device_start()
        self.clock()
        self.send(0, hrhandoff.CAPABILITY)
        self.firmware.device_begin()
        self.device_send()

    def clock(self):
        dt = hrhandoff.millis() - self.origin
        self.firmware.device_clock(4200 + dt // 10, 17000 + dt)

    def send(self, side, payload):
        self.words[side].append(payload)
        self.sent_at[side].append(hrhandoff.millis())
        if self.slow and payload != hrhandoff.CAPABILITY:
            key = sum(len(words) for words in self.words)
            delay = ((58, 63, 71) if self.busy else (50, 54, 59))[key % 3]
            self.flight.append((hrhandoff.millis() + delay, side, payload))
        else:
            self.deliver(side, payload)

    def deliver(self, side, payload):
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
        while self.flight and self.flight[0][0] <= hrhandoff.millis():
            _, side, payload = self.flight.pop(0)
            self.deliver(side, payload)
        self.host.poll(lambda msg: self.send(0, msg))
        result = 0
        if not self.busy or hrhandoff.millis() >= self.next_ui:
            result = self.firmware.device_step()
            self.next_ui = hrhandoff.millis() + 400
        self.device_send(drop)
        return result

    def device_send(self, drop=None):
        size = self.firmware.device_send(self.out, len(self.out))
        if size:
            payload = self.out.raw[:size]
            if drop is None or not payload.startswith(drop):
                self.send(1, payload)

    def until(self, condition, *, drop=None, timeout=7):
        expire = time.monotonic() + timeout
        while time.monotonic() < expire:
            self.step(drop)
            if condition():
                return
            time.sleep(.002)
        raise AssertionError(f'Timed out: {self.firmware.device_log()!r}, '
                             f'host={self.words[0]}, device={self.words[1]}')


def no_offer_yet(cable):
    return all(b'\toffer\t' not in payload for payload in cable.words[0]) and all(
        b'\ta\t' not in payload for payload in cable.words[1])


def scenario(lib, mode):
    cable = Cable(lib, slow=mode == 'latency', busy=mode in ('busy', 'presync'),
                  match_delay=.4 if mode == 'early' else 0)
    fw, player, host = cable.firmware, cable.player, cable.host
    try:
        if mode == 'early':
            cable.until(lambda: fw.device_preparing() and any(b'\tp\t' in p for p in cable.words[1]))
            assert no_offer_yet(cable)
            assert player.volumes == [] and not player.playing
        if mode == 'presync':
            fw.device_wait()
            cable.until(lambda: sum(b'\tp\t' in p for p in cable.words[1]) >= 7)
            assert player.volumes == [] and not player.playing
            fw.device_accept()
        if mode == 'decline':
            fw.device_decline()
            cable.until(lambda: b'outcome=declined' in fw.device_log())
            cable.until(lambda: host.proposal.finished)
            assert fw.device_state() == 1 and player.volumes == []
            assert [p.split(b'\t')[2] for p in cable.words[1]][-1] == b'x'
            assert all(p.split(b'\t')[2] in (b'q', b'p', b'x') for p in cable.words[1])
        elif mode == 'timeout':
            cable.until(lambda: b'outcome=timeout' in fw.device_log(), drop=b'hf\t1\tp\t', timeout=10)
            assert fw.device_state() == 1 and player.volumes == []
        elif mode == 'verify-timeout':
            cable.until(lambda: b'outcome=timeout' in fw.device_log(), drop=b'hf\t1\tv\t')
            cable.until(lambda: host.proposal.finished)
            assert fw.device_state() == 1 and player.pauses == 1 and player.volume == .72
            assert not any(0 < v < .72 for v in player.volumes)
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
            cable.until(lambda: host.proposal is not None and host.proposal.finished, timeout=10)
            events = [p.split(b'\t')[2] for p in cable.words[1]]
            assert events[0] == b'q' and events[-3:] == [b'g', b'v', b'd'], (
                f'handoff did not complete: {cable.words[1]}; '
                f'host={cable.words[0]}; firmware={fw.device_log()!r}')
            assert events.count(b'a') == 1
            assert events.count(b'p') >= 5
            pings = {p.split(b'\t')[-1]: p for p in cable.words[0] if b'\tping\t' in p}
            for response in (p.split(b'\t') for p in cable.words[1] if b'\tp\t' in p):
                assert len(response) == 9
                assert response[5] == pings[response[4]].split(b'\t')[-2]
                assert int(response[7]) - int(response[6]) <= 2
            if cable.busy:
                go = next(i for i, p in enumerate(cable.words[0]) if b'\tgo\t' in p)
                target = int(cable.words[0][go].split(b'\t')[4])
                boundary = cable.origin + (target - 4200) * 10
                ack = cable.sent_at[1][events.index(b'g')]
                verify = cable.sent_at[1][events.index(b'v')]
                assert 0 <= ack - cable.sent_at[0][go] <= 100
                assert 0 <= verify - boundary <= 30
            if mode == 'presync':
                assert events.index(b'a') >= 8 and b'p' not in events[events.index(b'a'):]
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
    modes = sys.argv[3:] or ('success', 'latency', 'busy', 'early', 'presync', 'decline',
                             'timeout', 'verify-timeout', 'unplug', 'host-fail')
    for mode in modes:
        # A new device instance for each virtual cable. dlopen coalesces by
        # path, so give each fixture an independent copy of the same build.
        fresh = out / f'{mode}.so'
        fresh.write_bytes(lib.read_bytes())
        scenario(fresh, mode)


if __name__ == '__main__':
    main()
