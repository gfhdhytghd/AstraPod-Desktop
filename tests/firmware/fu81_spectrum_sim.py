#!/usr/bin/env python3
"""Test the real SDL WPS and %Sb renderer with a synthetic resident peer.

Instrument disposable source copies with --instrument SOURCE. Build those
copies normally as SDL simulators. Run BUILD BASE_ZIP OUT on each simulator.
The same visible-bars assertion must fail on the unmodified firmware.
"""
import argparse
import json
import math
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import time
from zipfile import ZipFile

from PIL import ImageChops, ImageGrab

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'herdremote'))
from hrspectrum import FRAME, RATE, WINDOW, band_levels


def function(source, name):
    match = re.search(r'(?m)^[\w *]+\b' + name + r'\([^;{}]*\)\s*\{', source)
    assert match, name
    end, depth = match.end(), 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[match.start():end]


def synthetic_packet(left_hz, right_hz):
    pcm = struct.pack('<2048h', *(
        int(9000 * math.sin(math.tau * hz * i / RATE))
        for i in range(WINDOW) for hz in (left_hz, right_hz)))
    return FRAME.pack(b'CSP1', 41, 0, band_levels(pcm))


def instrument(source):
    playback = source / 'apps-ipod/screens/playback'
    cider = (playback / 'cider.c').read_text()
    implemented = 'static bool spectrum_receive(' in cider
    header = playback / 'cider.h'
    code = header.read_text()
    for name in ('cider_active', 'cider_connected', 'cider_spectrum_wps', 'cider_spectrum_levels'):
        stub = re.search(r'static inline (\w+ ' + name + r'\([^;{}]*\))\s*\{[^{}]*\}', code)
        if stub:
            code = code.replace(stub.group(0), stub.group(1) + ';')
    header.write_text(code)
    header = playback / 'cider_wps.h'
    code = header.read_text()
    stub = function(code, 'cider_wps_update')
    header.write_text(code.replace(stub, 'void cider_wps_update(struct wps_state *s);'))

    adapter = r'''
#include "config.h"
#include "kernel.h"
#include "system.h"
#include "system/activity.h"
#include "lcd.h"
#include "podlink.h"
#include "metadata.h"
#include "screens/playback/cider.h"
#include "screens/playback/cider_wps.h"
#include "screens/playback/wps.h"
#include "root_menu.h"
#include "audio/spectrum_meter.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static struct cider_state state = { .generation = 41, .available = true,
                                    .have = true, .rate = 1000 };
static struct mp3entry track = { .title = "FU81 Synthetic sweep",
                                 .artist = "HerdRemote simulator", .length = 30000 };
static long started, last_packet;
bool cider_active(void) { return true; }
bool cider_connected(void) { return true; }
int cider_browse_covers(bool all) { (void)all; return GO_TO_PREVIOUS; }
void cider_wps_update(struct wps_state *s)
{
    track.elapsed = started ? (current_tick - started) * 10 : 0;
    s->id3 = &track;
    s->paused = !state.rate;
}
'''
    if implemented:
        adapter += 'static struct mutex cider_mutex;\n'
        adapter += cider[cider.index('static volatile bool spectrum_visible;'):
                         cider.index('static struct cider_store_stats diag;')]
        adapter += '\n'.join(function(cider, name) for name in (
            'u32', 'spectrum_wanted', 'spectrum_receive', 'spectrum_next',
            'cider_spectrum_wps', 'cider_spectrum_levels'))
    else:
        # Replay the identical wire input against the older application's
        # demux, which has no CSP1 handler.
        adapter += r'''
static bool spectrum_receive(const struct podlink_message *m)
{
    (void)m;
    return false;
}
'''
    packets = (synthetic_packet(150, 1000), synthetic_packet(4000, 12000))
    adapter += '\nstatic const unsigned char packets[2][26] = {\n'
    adapter += ',\n'.join('    {' + ', '.join(map(str, packet)) + '}' for packet in packets)
    adapter += '\n};\n'
    adapter += f'#define HAS_SPECTRUM {int(implemented)}\n'
    adapter += r'''
void fu81_peer_tick(void)
{
    if (!started)
    {
        started = current_tick;
#if HAS_SPECTRUM
        mutex_init(&cider_mutex);
        spectrum_generation = state.generation;
#endif
        fprintf(stderr, "FU81 WPS started\n");
    }

    long elapsed = current_tick - started;
    unsigned char packet[26];
    memcpy(packet, packets[elapsed < 120 ? 0 : 1], sizeof packet);
    struct podlink_message m = {.type = PODLINK_BINARY, .data = packet,
                                .length = sizeof packet};
#if HAS_SPECTRUM
    char request[48];
    unsigned char levels[16];
    unsigned int n = spectrum_next(request, sizeof request);
    if (n) fprintf(stderr, "FU81 subscription %s\n", request);
    if (elapsed < 5 && spectrum_subscribed)
    {
        state.rate = 0;
        assert(spectrum_next(request, sizeof request) && !strcmp(request, "cs\t41\t0"));
        uint16_t previous = spectrum_id;
        state.rate = 1000;
        assert(spectrum_next(request, sizeof request) && spectrum_id != previous);
        packet[8] = spectrum_id;
        packet[9] = spectrum_id >> 8;
        packet[0] = 'X';
        assert(!spectrum_receive(&m));
        packet[0] = 'C';
        m.length--;
        assert(spectrum_receive(&m) && !cider_spectrum_levels(levels));
        m.length++;
        packet[4] = 40;
        assert(spectrum_receive(&m) && !cider_spectrum_levels(levels));
        packet[4] = 41;
        packet[8]--;
        assert(spectrum_receive(&m) && !cider_spectrum_levels(levels));
        packet[8]++;
        packet[10] = 101;
        assert(spectrum_receive(&m) && !cider_spectrum_levels(levels));
        fprintf(stderr, "FU81 wire rejection checks passed\n");
    }
    packet[8] = spectrum_id;
    packet[9] = spectrum_id >> 8;
#endif
    if (elapsed >= 40 && elapsed < 200 && current_tick - last_packet >= 4)
    {
        last_packet = current_tick;
        bool accepted = spectrum_receive(&m);
        if (elapsed == 40 || elapsed == 120)
            fprintf(stderr, "FU81 phase %ld recognized=%d\n", elapsed, accepted);
    }
}
'''
    (source / 'apps-ipod/sim/fu81_peer.c').write_text(adapter)
    sources = source / 'apps-ipod/SOURCES'
    code = sources.read_text()
    sources.write_text(code.replace('sim/shim-board.c\n', 'sim/shim-board.c\nsim/fu81_peer.c\n'))
    skin = source / 'apps-ipod/skin/skin_display.c'
    code = skin.read_text()
    marker = '            if (pm)\n                peak_meter_peek();'
    assert marker in code
    skin.write_text(code.replace(marker, '            extern void fu81_peer_tick(void);\n'
                                 '            if (skin == WPS) fu81_peer_tick();\n' + marker))


def stop(child):
    if child and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def capture(build, base, out):
    out.mkdir(parents=True, exist_ok=False)
    disk = Path(tempfile.mkdtemp(prefix='fu81-simdisk-', dir=build))
    with ZipFile(base) as package:
        for name in package.namelist():
            if name.startswith(tuple('.rockbox/' + folder + '/' for folder in
                                     ('fonts', 'wps', 'themes', 'icons', 'backdrops'))):
                package.extract(name, disk)
    (disk / '.rockbox/config.cfg').write_text(
        'font: /.rockbox/fonts/18-noto-sans.fnt\n'
        'font bold: /.rockbox/fonts/18-noto-sans-bold.fnt\n'
        'wps: scrim\nsbs: scrim\nforeground color: FFFFFF\n'
        'background color: 080D17\nstatusbar: off\nstart in screen: wps\n'
        'backlight timeout: off\nscrollbar: off\n')

    sim = server = None
    frames = []
    with (out / 'xvfb.log').open('w') as xlog, (out / 'simulator.log').open('w') as slog:
        try:
            read, write = os.pipe()
            server = subprocess.Popen(['Xvfb', '-displayfd', str(write), '-screen',
                                       '0', '800x600x24', '-nolisten', 'tcp'],
                                      pass_fds=(write,), stderr=xlog)
            os.close(write)
            with os.fdopen(read) as ready:
                display = ':' + ready.readline().strip()
            env = dict(os.environ, DISPLAY=display, SDL_VIDEODRIVER='x11',
                       SDL_AUDIODRIVER='dummy')
            sim = subprocess.Popen([str(build / 'rockboxui'), '--root', str(disk),
                                    '--nobackground', '--zoom', '1'], cwd=build,
                                   env=env, stdout=slog, stderr=subprocess.STDOUT)
            for _ in range(300):
                if 'FU81 WPS started' in (out / 'simulator.log').read_text(errors='replace'):
                    break
                assert sim.poll() is None, 'SDL simulator exited'
                time.sleep(.02)
            else:
                raise AssertionError('Remote WPS did not start')
            start = time.monotonic()
            for i in range(106):
                time.sleep(max(0, start + i / 25 - time.monotonic()))
                frames.append(ImageGrab.grab(xdisplay=display)
                              .crop((240, 180, 560, 420)).convert('RGB'))
        finally:
            stop(sim)
            stop(server)

    viewport = (0, 200, 80, 240)
    reference = frames[6].crop(viewport)

    def changed(first, second):
        diff = ImageChops.difference(first, second)
        return sum(max(rgb) > 20 for rgb in diff.get_flattened_data())

    signal = [changed(reference, frame.crop(viewport)) for frame in frames]
    phase_change = changed(frames[20].crop(viewport), frames[42].crop(viewport))
    for number, name in ((6, 'before'), (20, 'bass'), (42, 'treble'),
                         (57, 'decay'), (100, 'after')):
        frames[number].save(out / (name + '.png'))
    frames[0].save(out / 'remote-spectrum.gif', save_all=True, append_images=frames[1:],
                   duration=40, loop=0)
    (out / 'capture.json').write_text(json.dumps(
        dict(viewport=viewport, fps=25, region_pixels_changed=signal,
             phase_change=phase_change), indent=2) + '\n')
    assert min(signal[14:25]) > 100, 'Remote WPS spectrum bars did not respond to CSP1'
    assert min(signal[37:47]) > 100 and phase_change > 100, 'Band pattern did not change'
    assert 0 < signal[57] < signal[45], 'Bars did not decay after frames stopped'
    assert max(signal[-8:]) == 0, 'Spectrum did not return to silence'
    assert 'wire rejection checks passed' in (out / 'simulator.log').read_text()
    print('PASS: SDL remote WPS bars track synthetic stereo CSP1 frames, then decay')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instrument', type=Path)
    parser.add_argument('paths', type=Path, nargs='*')
    args = parser.parse_args()
    if args.instrument:
        instrument(args.instrument.resolve())
    else:
        capture(*(arg.resolve() for arg in args.paths))
