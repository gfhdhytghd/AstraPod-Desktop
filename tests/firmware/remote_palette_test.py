#!/usr/bin/env python3
"""Exercise the firmware palette with real native cover bytes and mocked playback."""
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys

from PIL import Image, ImageDraw

from _support import DESKTOP, require_firmware

ROOT = require_firmware()
# The shared firmware builder takes its root and output from env and argv.
os.environ['ART_TEST_ROOT'] = str(ROOT)
if len(sys.argv) == 1:
    sys.argv.append(str(DESKTOP / 'build-remote-palette'))
sys.path.insert(0, str(ROOT / 'tools/tests/albumart'))
from host import OUT, FLAGS, CC, function, run, compile_sources
import hrcider


PALETTE = 'apps-ipod/metadata/album_palette.c'
text = (ROOT / PALETTE).read_text()


def between(start, end):
    return text[text.index(start):text.index(end, text.index(start))]


(OUT / 'extractor.inc').write_text(
    between('#define PALETTE_MAX_DIM', 'void album_palette_alloc(')
    .replace('static struct mutex palette_lock;\n', '')
    + function(PALETTE, 'album_palette_alloc')
    + between('static int compute_luminance', '/* Palettes worked out ahead')
    + 'static unsigned palette_miss;\n'
    + ''.join(function(PALETTE, name) for name in (
        'remote_key_changed', 'palette_from_remote', 'album_art_current', 'palette_from_bitmap',
        'album_palette_refresh', 'rgb888', 'album_palette_snapshot',
        'album_palette_generation', 'album_art_render_background')))
(OUT / 'copy_art.inc').write_text(
    function('apps-ipod/screens/playback/cider.c', 'cider_copy_art')
    + function('apps-ipod/screens/playback/cider.c', 'cider_current_art'))

probe = OUT / 'remote-palette'
objects = compile_sources(['apps-ipod/draw/color.c', 'apps-ipod/draw/img_filter.c',
                           'firmware/common/strlcpy.c', 'firmware/common/strmemccpy.c',
                           'lib/fixedpoint/fixedpoint.c',
                           'apps-ipod/screens/playback/cider_model.c'])
run([CC, *FLAGS, '-I' + str(OUT), Path(__file__).with_name('remote_palette_probe.c'),
     *objects, '-Wl,--gc-sections', '-o', probe], 'link-palette')

native = OUT / 'native'
subprocess.run([sys.executable, DESKTOP / 'herdremote/build-cover-thumb.py', native],
               env={**os.environ, 'ASTRAPOD_FIRMWARE': str(ROOT)}, check=True)
native = native / 'cover-thumb'
hrcider.COVER_THUMB = native
files = OUT / 'fixtures'
files.mkdir(exist_ok=True)


def save(image, fmt):
    out = io.BytesIO()
    image.save(out, fmt)
    return out.getvalue()


# Same quadrants and PNG alpha as albumart/decode.py, scaled to the host cap.
quad = Image.new('RGB', (240, 144))
draw = ImageDraw.Draw(quad)
for box, colour in [((0, 0, 119, 71), 'red'), ((120, 0, 239, 71), 'green'),
                    ((0, 72, 119, 143), 'blue'), ((120, 72, 239, 143), 'yellow')]:
    draw.rectangle(box, fill=colour)
alpha = quad.convert('RGBA')
alpha.putalpha(128)
# Solid covers from test_cider.py and test_cider_prefetch.py.
red = save(Image.new('RGB', (240, 240), (255, 0, 0)), 'PNG')
covers = {
    'quadrants': save(quad, 'JPEG'),
    'translucent': save(alpha, 'PNG'),
    'webp': save(quad, 'WEBP'),
    'solid-red': red,
    'solid-blue': save(Image.new('RGB', (240, 240), (30, 80, 140)), 'PNG'),
}
# Commercial artwork stays out of the repository; point FU84_SHAUN at a local copy.
if os.environ.get('FU84_SHAUN'):
    covers['shaun'] = Path(os.environ['FU84_SHAUN']).read_bytes()
if os.environ.get('FU84_COVER'):
    full = Path(os.environ['FU84_COVER']).read_bytes()
    # Mock Rufin returning the provided asset above its advertised size.
    # Rufin's production artwork adapter contains an oversized response.
    from hrrufin import RufinClient
    client = RufinClient.__new__(RufinClient)
    client._raw = lambda path: full
    covers['original-copy'] = client.artwork('fixture')


def to_local(raw, label):
    path = files / (label + '.image')
    path.write_bytes(raw)
    with path.open('rb') as source:
        return subprocess.check_output([native], stdin=source)


a = files / 'a.aat'
a.write_bytes(hrcider.render(red, 'L'))
a_palette = files / 'a.P.aat'
a_palette.write_bytes(hrcider.render(red, 'P'))
failed = []
for name, raw in covers.items():
    local = files / (name + '.rgb565')
    b = files / (name + '.L.aat')
    p = files / (name + '.P.aat')
    medium = files / (name + '.M.aat')
    local.write_bytes(to_local(full if name == 'original-copy' else raw, name))
    b.write_bytes(hrcider.render(raw, 'L'))
    medium.write_bytes(hrcider.render(raw, 'M'))
    if name == 'original-copy':
        bridge = hrcider.Bridge.__new__(hrcider.Bridge)
        bridge.rufin, bridge.rufin_library = client, None
        bridge.jellyfin_library = None
        key = 'b' * 64
        bridge.templates = {key: 'rufin:fixture'}
        with_cache = hrcider.Cache(files / 'rufin-cache')
        try:
            bridge.cache = with_cache
            bridge.cache.put_art(key, 'original', raw)
            p.write_bytes(bridge._artwork(key, 'P'))
            assert bridge.cache.get_art(key, 'P1') == p.read_bytes()
        finally:
            with_cache.close()
    else:
        p.write_bytes(hrcider.render(raw, 'P'))
    assert p.read_bytes()[12:] == local.read_bytes()
    print(name, 'local', hashlib.sha256(local.read_bytes()).hexdigest(),
          'wire L', hashlib.sha256(b.read_bytes()).hexdigest(), flush=True)
    result = run([probe, local, a, b, p, a_palette, medium], name, check=False)
    print(result.stdout, result.stderr, sep='', flush=True)
    if result.returncode:
        failed.append(name)
assert not failed, failed
