#!/usr/bin/env python3
"""Build HerdRemote's native cover decoder with the iPod 6G simulator flags."""
from pathlib import Path
import shlex
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
SOURCES = [
    'tools/herdremote/cover_thumb.c',
    *('apps-ipod/draw/' + name + '.c' for name in
      ('jpeg_load', 'png_load', 'gif_load', 'webp_load', 'resize', 'bmp', 'image_load')),
    'apps-ipod/files/byte_stream.c',
    'apps-ipod/files/inflate.c',
    *('lib/rbcodec/metadata/' + name + '.c' for name in
      ('id3tags', 'ogg', 'vorbis', 'metadata_common', 'replaygain')),
]


def build(output):
    output.mkdir(parents=True, exist_ok=True)
    config = output / 'config'
    config.mkdir(exist_ok=True)
    if not (config / 'Makefile').exists():
        subprocess.run([ROOT / 'tools/configure', '--target=ipod6g',
                        '--type=s', '--appsdir=apps-ipod'], cwd=config, check=True)
    dry = subprocess.check_output(
        ['make', '-n', str(config / 'apps-ipod/draw/jpeg_load.o')],
        cwd=config, text=True)
    command = shlex.split(next(line for line in dry.splitlines()
                               if ' -c ' in line and 'jpeg_load.c' in line))
    flags = command[:command.index('-c')]
    subprocess.run([*flags, '-ffunction-sections', '-fdata-sections',
                    *(ROOT / source for source in SOURCES),
                    '-Wl,--gc-sections', '-o', output / 'cover-thumb'], check=True)


if __name__ == '__main__':
    if len(sys.argv) != 2:
        sys.exit('Usage: build-cover-thumb.py OUTPUT_DIR')
    build(Path(sys.argv[1]).resolve())
