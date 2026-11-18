#!/usr/bin/env python3
"""Exercise the firmware palette with real native cover bytes and mocked playback."""
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys

from PIL import Image, ImageDraw

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS / 'tests/albumart'))
from host import ROOT, OUT, FLAGS, CC, function, run, compile_sources
sys.path.insert(0, str(ROOT / 'tools/herdremote'))
import hrcider


SKIN = 'apps-ipod/skin/skin_albumart_color.c'
text = (ROOT / SKIN).read_text()


def between(start, end):
    return text[text.index(start):text.index(end, text.index(start))]


WPS = 'apps-ipod/screens/playback/cider_wps.c'
wps_text = (ROOT / WPS).read_text()
supports_palette = 'cider_wps_art_generation' in wps_text
palette_funcs = (
    between('#define PALETTE_MAX_DIM', '/* Shared by screen')
    + function(SKIN, 'palette_from_remote')
    if supports_palette else ''
)
filter_config = '#define SKINNABLE_SCREENS_COUNT 3\n'
(OUT / 'extractor.inc').write_text(
    between('#define AA_SETTLE_TICKS', '/* Change to a new pair')
    + between('static void apply_colors', '/* ---------------------------------------------------------------------- *\n * The palette\'s own source')
    + filter_config
    + between('#define AA_FILTER_SLOTS', 'static struct skin_albumart *lyrics_skin_blur')
    + function(SKIN, 'art_filtered')
    + function(SKIN, 'remember_filtered')
    + function(SKIN, 'skin_albumart_art_opened')
    + function(SKIN, 'palette_from_slot')
    + palette_funcs
    + function(SKIN, 'dynamic_colors_check_extraction')
    + function(SKIN, 'skin_albumart_filter'))
wps_funcs = ['cider_wps_enter', 'cider_wps_update', 'wps_art_handle',
             'wps_art_bitmap', 'wps_art_pin']
if supports_palette:
    wps_funcs += ['cider_wps_art_generation', 'cider_wps_art_key']
(OUT / 'wps.inc').write_text(
    wps_text[wps_text.index('static struct mp3entry id3;'):wps_text.index('void cider_wps_enter')]
    + ''.join(function(WPS, name) for name in wps_funcs))
(OUT / 'copy_art.inc').write_text(
    function('apps-ipod/screens/playback/cider.c', 'cider_copy_art'))

probe = OUT / 'remote-palette'
objects = compile_sources(['apps-ipod/draw/color.c', 'apps-ipod/draw/img_filter.c',
                           'firmware/common/strlcpy.c', 'firmware/common/strmemccpy.c',
                           'lib/fixedpoint/fixedpoint.c',
                           'apps-ipod/screens/playback/cider_model.c'])
extra = ['-DTEST_PALETTE_TIER'] if supports_palette else []
run([CC, *FLAGS, *extra, '-I' + str(OUT), TOOLS / 'tests/albumart/remote_palette_probe.c',
     *objects, '-Wl,--gc-sections', '-o', probe], 'link-palette')

native = OUT / 'native'
subprocess.run([sys.executable, TOOLS / 'herdremote/build-cover-thumb.py', native],
               check=True)
native = native / 'cover-thumb'
if supports_palette:
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
if supports_palette:
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
    if supports_palette:
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
