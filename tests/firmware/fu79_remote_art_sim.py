#!/usr/bin/env python3
"""Exercise Rufin cover rendering with real HerdRemote and an offline SDL peer."""
import argparse
import collections
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from zipfile import ZipFile

from PIL import Image, ImageGrab

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def instrument(source):
    def replace(name, marker, replacement):
        path = source / name
        content = path.read_text()
        assert marker in content, marker
        path.write_text(content.replace(marker, replacement, 1))
    replace('firmware/export/config/sim.h', '#undef HAVE_USB_PODLINK',
            '#define HAVE_USB_PODLINK')
    replace('firmware/SOURCES', 'usbstack/podlink_rust_platform.c\n', '')
    replace('apps-ipod/SOURCES', 'sim/shim-usb.c',
            'sim/shim-usb.c\nsim/fu79-peer.c')
    replace('apps-ipod/input/action.c', '#include "action.h"',
            '#include "action.h"\n#include "system/activity.h"')
    replace('apps-ipod/root_menu.c', '    next_screen = root_menu_setup_screens();',
            '    extern int fu79_scene(int);\n    next_screen = fu79_scene(root_menu_setup_screens());')
    # Only the report's native build uses the hardware memory cap. SDL pthread
    # creates host-sized stacks and the resident structs have 64-bit pointers.
    replace('apps-ipod/screens/playback/cider.c', '< 1152 * 1024,', '< 2048 * 1024,')
    shutil.copyfile(HERE / 'fixtures/fu79/peer.c', source / 'apps-ipod/sim/fu79-peer.c')


def prepare(root, package, scene):
    rockbox = root / '.rockbox'
    if not rockbox.exists():
        with ZipFile(package) as assets:
            for name in assets.namelist():
                if not name.startswith(('.rockbox/tools/', '.rockbox/rockbox.ipod')):
                    assets.extract(name, root)
    (rockbox / 'wps/fu79.wps').write_text(
        '%V(0,0,320,240,-)\n%Cl(70,25,180,180,c,c)\n%Cd\n%ac%it\n')
    (rockbox / 'config01.cfg').unlink(missing_ok=True)
    (rockbox / 'nvram.bin').unlink(missing_ok=True)
    (rockbox / 'config.cfg').write_text(
        'font: /.rockbox/fonts/18-noto-sans.fnt\n'
        'wps: /.rockbox/wps/fu79.wps\nsbs: /.rockbox/wps/scrim.sbs\n'
        'background color: 000000\nstatusbar: off\nbacklight timeout: off\n'
        'database album art: on\ndatabase art row height: 48\n'
        'album covers statusbar: off\nstart in screen: root\n'
        + ('theme: /.rockbox/themes/scrim.cfg\n' if scene in ('albums','tracks') else ''))


def serve(args, sock, stop, trace):
    sys.path.insert(0, str(args.host / 'tools/herdremote'))
    import hrcider

    def offline(*_args, **_kwargs):
        raise AssertionError('unexpected backend request')

    class Rufin:
        pass

    keys = [hashlib.sha256(f'fixture album {n}'.encode()).hexdigest()
            for n in range(args.albums)]
    keys[0] = 'a' * 64

    class Library:
        generation = 7

        def page(self, route, offset, limit, stale=False):
            tracks = route.startswith('album/')
            rows = [dict(id=str(i), title=f'Fixture {"track" if tracks else "album"} {i:02d}',
                         subtitle='Fixture artist', art=keys[0 if tracks else i],
                         duration=180000, count=6, kind='song' if tracks else 'album',
                         route='play/library-songs/track' if tracks else 'album/fixture',
                         bg='000000', year=2020)
                    for i in range(6 if tracks else len(keys))]
            return dict(generation=self.generation, offset=offset, total=len(rows),
                        stale=False, items=rows[offset:offset+limit])

    cache = hrcider.Cache(args.out / 'host-cache')
    for tier, maximum in [('M', 8), ('XS', 8), ('S', 4), ('L', 4)]:
        for i, key in enumerate(keys):
            cache.put_art(key,tier,(args.fixtures / f'{tier}-{i % maximum}.aat').read_bytes())
    rufin = Rufin()
    bridge = hrcider.Bridge(client=Rufin(), cache=cache, fetch=offline, rufin=rufin)
    bridge.client = rufin
    bridge.library = bridge.rufin_library = Library()
    bridge.available = True
    bridge.reason = ''
    bridge.state = dict(duration=180000, position=10000, rate=1000, volume=50,
                        shuffle=0, repeat=0, id='track', art=keys[0], bg='000000',
                        title='Rufin fixture track', artist='Fixture artist', album='Fixture album')
    workers = [bridge._work, bridge._pages]
    if hasattr(bridge, 'fill_jobs'):
        workers.extend([bridge._work, lambda: bridge._work(bridge.fill_jobs), bridge._rufin_inventory])
    for work in workers:
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        bridge.threads.append(worker)

    pending = collections.deque()
    commands = collections.deque()
    finish = time.monotonic()
    start = finish

    def send(kind, payload):
        nonlocal finish
        now = time.monotonic()
        # Observed warm M ready→ACK was about 160 ms. Model a shared wire
        # plus a per-frame transport cost separately from host rendering.
        finish = max(now, finish) + (0.010 + len(payload) / 240000)
        pending.append((finish, struct.pack('<BH', kind,len(payload))+payload))
        if kind == 3:
            detail = list(struct.unpack_from('<4sII',payload))
            detail[0] = detail[0].decode()
        else:
            detail = payload.split(b'\n',1)[0].decode()
        trace.append(['send',round(now-start,4),round(finish-start,4),detail])

    sock.settimeout(.005)
    buffer = b''
    try:
        while not stop.is_set():
            try:
                received = sock.recv(4096)
                if not received:
                    return
                buffer += received
                while len(buffer) >= 2:
                    length = struct.unpack_from('<H', buffer)[0]
                    if len(buffer) < length + 2:
                        break
                    record, buffer = buffer[2:length+2], buffer[length+2:]
                    commands.append((time.monotonic() + .025, record))
            except TimeoutError:
                pass
            now = time.monotonic()
            while commands and commands[0][0] <= now:
                _, command = commands.popleft()
                trace.append(['receive', round(now-start,4), command.decode()])
                if command.startswith(b'ci\t') and bridge.opened and now - bridge.last_state < .25:
                    # The SDL boot can enqueue two hellos before the synthetic
                    # link delivers CA. Neither is a reconnect on this socket.
                    continue
                if args.no_art and command.startswith((b'cr\t', b'cb\t')):
                    continue
                bridge.command(command)
            if bridge.opened and now - bridge.last_state > 1.0:
                bridge._state()
            bridge.poll(send)
            while pending and pending[0][0] <= now:
                _, frame = pending.popleft()
                sock.sendall(frame)
    except (BrokenPipeError, ConnectionResetError):
        if not stop.is_set():
            raise
    finally:
        bridge.close()


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


# Cover interiors exclude antialiased text, borders and the shelf shadows.
REGIONS = {
    'flow': ((103,174,216,197), [(107,56,211,128), (229,57,255,128),
                                (269,60,287,126), (296,65,316,123)]),
    'albums': ((69,47,230,78), [(13,45 + 48*i,51,81 + 48*i) for i in range(4)]),
    'tracks': ((48,116,240,140), [(21,57,58,94)]),
    'wps': ((68,15,251,35), [(82,45,237,192)]),
}


def analyze(directory, scene, verify=False):
    label, covers = REGIONS[scene]
    paths = sorted(directory.glob('*.png'))
    assert paths, f'No LCD frames in {directory}'
    with Image.open(paths[-1]) as last:
        shades = list(last.crop(label).convert('L').get_flattened_data())
    text = [i for i, value in enumerate(shades) if value > 160]
    assert len(text) >= 30, f'No label in {paths[-1]}'
    entered = None
    first = [None] * len(covers)
    for path in paths:
        elapsed = float(path.stem)
        with Image.open(path) as frame:
            if entered is None:
                shades = list(frame.crop(label).convert('L').get_flattened_data())
                if sum(shades[i] > 140 for i in text) < len(text) * .7:
                    continue
                entered = elapsed
            for i, rect in enumerate(covers):
                if first[i] is None and len(frame.crop(rect).getcolors(65536)) >= 30:
                    first[i] = elapsed
    assert entered is not None, f'No {scene} screen in {directory}'
    waits = [round((present-entered)*1000) if present is not None else None
             for present in first]
    result = dict(scene=scene, entered=entered, present=first, wait_ms=waits,
                  visible_within_300ms=sum(wait is not None and wait<=300 for wait in waits))
    (directory / 'pixels.json').write_text(json.dumps(result, indent=2)+'\n')
    if verify:
        assert None not in waits, f'{scene}: some delivered covers stayed empty: {waits}'
        if scene in ('albums','flow'):
            assert max(waits) <= 300, f'{scene}: visible covers missed 300 ms: {waits}'
    return result


def run(args):
    args.out.mkdir(parents=True, exist_ok=False)
    new_root = args.root is None
    if new_root:
        args.root = args.out / 'simdisk'
    prepare(args.root, args.package, args.scene)
    trace, frames, errors = [], [], []
    stop = threading.Event()

    def handle(connection):
        with connection:
            try:
                serve(args, connection, stop, trace)
            except Exception as error:
                errors.append(error)
                stop.set()

    with tempfile.TemporaryDirectory(prefix='fu79-') as short, socket.socket(socket.AF_UNIX) as listener:
        path = str(Path(short) / 'peer')
        listener.bind(path)
        listener.listen(1)
        listener.settimeout(10)
        rd, wr = os.pipe()
        with (args.out / 'sim.log').open('w') as log:
            display = subprocess.Popen(['Xvfb', '-displayfd', str(wr), '-screen', '0', '800x600x24'],
                                       pass_fds=(wr,), stderr=log)
            os.close(wr)
            sim = thread = None
            try:
                with os.fdopen(rd) as display_name:
                    name = ':' + display_name.readline().strip()
                env = dict(os.environ, DISPLAY=name, SDL_VIDEODRIVER='x11', SDL_AUDIODRIVER='dummy',
                           FU79_PEER=path, FU79_SCENE=args.scene)
                sim = subprocess.Popen([str(args.sim), '--root', str(args.root),
                                        '--nobackground', '--zoom', '1', '--screenshot'],
                                       cwd=args.sim.parent, env=env, stdout=log, stderr=log)
                connection, _ = listener.accept()
                thread = threading.Thread(target=handle,args=(connection,),daemon=True)
                thread.start()
                start = time.monotonic()
                deadline = start + args.seconds
                scroll = collections.deque(args.scroll)
                while time.monotonic() < deadline and not stop.is_set() and sim.poll() is None:
                    elapsed = time.monotonic() - start
                    if scroll and elapsed >= scroll[0]:
                        scroll.popleft()
                        subprocess.run(['xdotool','key','--delay','0','Down'],env=env,check=True)
                    if args.scene in REGIONS:
                        image = ImageGrab.grab(xdisplay=name).crop((240,180,560,420))
                        frames.append((round(elapsed,4),image))
                    time.sleep(.025)
                if errors:raise errors[0]
                assert sim.poll() is None, f'SDL exited with {sim.returncode}'
            finally:
                stop.set()
                if sim is not None:stop_process(sim)
                stop_process(display)
                if thread is not None:thread.join(4)
                (args.out / 'wire.json').write_text(json.dumps(trace,indent=2)+'\n')
                for elapsed,image in frames:
                    image.save(args.out / f'{elapsed:.4f}.png')
    print(f'{args.scene}: {len(frames)} LCD samples, {len(trace)} protocol events')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instrument',type=Path)
    parser.add_argument('--sim',type=lambda p: Path(p).resolve())
    parser.add_argument('--host',type=lambda p: Path(p).resolve(),default=REPO)
    parser.add_argument('--fixtures',type=lambda p: Path(p).resolve())
    parser.add_argument('--package',type=Path)
    parser.add_argument('--out',type=lambda p: Path(p).resolve())
    parser.add_argument('--root',type=lambda p: Path(p).resolve())
    parser.add_argument('--scene',choices=['flow','wps','albums','tracks','idle'])
    parser.add_argument('--seconds',type=float,default=4.)
    parser.add_argument('--scroll',type=float,nargs='*',default=[])
    parser.add_argument('--no-art',action='store_true')
    parser.add_argument('--albums',type=int,default=12)
    parser.add_argument('--analyze',type=Path)
    parser.add_argument('--verify',action='store_true')
    arguments = parser.parse_args()
    if arguments.instrument:
        instrument(arguments.instrument)
    elif arguments.analyze:
        print(analyze(arguments.analyze, arguments.scene, arguments.verify))
    else:
        logging.basicConfig(filename=arguments.out.parent / 'host-sim.log', level=logging.INFO)
        run(arguments)
