#!/usr/bin/env python3
"""Run only after review. Never enables, reloads or restarts Cider."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

SOURCE = Path(__file__).resolve().parent
sys.path.insert(0, str(SOURCE.parent / 'herdremote'))
from hrcider_plugin import default_secret_path, install_secret

IDENTIFIER = 'org.podbox.cider-bridge'
# Only the inspected build has the import aliases used by plugin.js.

def install(profile, secret_file, port, assets=None):
    profile = Path(profile).expanduser()
    assets = Path(assets) if assets is not None else (
        profile/'update.cup' if (profile/'update.cup').exists() else Path('/usr/lib/cider/resources/spa/assets'))
    if not 1 <= port <= 65535:
        raise ValueError('Invalid loopback port')
    expected = json.loads((SOURCE/'host-assets.json').read_text())
    for name, digest in expected.items():
        if assets.is_file():
            with zipfile.ZipFile(assets) as source:
                data = source.read('spa/assets/'+name)
        else:
            data = (assets/name).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Unsupported Cider build; re-inspect host assets first')
    if profile.is_symlink() or (profile/'plugins').is_symlink():
        raise ValueError('Profile and plugins directory must not be symlinks')
    install_secret(secret_file)
    plugins = profile/'plugins'
    plugins.mkdir(parents=True, exist_ok=True)
    target = plugins/IDENTIFIER
    if target.is_symlink():
        raise ValueError('Plugin directory must not be a symlink')
    stage = Path(tempfile.mkdtemp(prefix='.podbox-stage-', dir=plugins))
    os.chmod(stage, 0o700)
    try:
        for name in ('plugin.yml','plugin.js','core.mjs','renderer.mjs'):
            shutil.copyfile(SOURCE/name, stage/name)
            os.chmod(stage/name, 0o600)
        fd = os.open(stage/'connection.mjs', os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as out:
            # Cider serves plugin assets over HTTP. Never put the secret in an
            # asset, even if the filesystem mode is 0600.
            out.write('export default '+json.dumps({'url':f'ws://127.0.0.1:{port}/','assets':expected})+';\n')
        if target.exists():
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
            backups = profile/'podbox-backups'
            if backups.is_symlink(): raise ValueError('Backup directory must not be a symlink')
            backups.mkdir(mode=0o700, exist_ok=True)
            backup = backups/(IDENTIFIER+'-'+stamp)
            target.rename(backup)
            print('Previous plugin backed up:', backup)
        stage.rename(target)
    finally:
        if stage.exists(): shutil.rmtree(stage)
    print('Plugin copied to:', target)
    print('Verified renderer assets from:', assets)
    print('Secret file (0600):', secret_file)
    print('Set CIDER_BACKEND=auto (default), plugin, or rpc in the HerdRemote environment.')
    print(f'Set CIDER_PLUGIN_PORT={port} and CIDER_PLUGIN_SECRET_FILE to the same secret file if customized.')
    print('In Cider Settings > Extensions > Plugins, refresh the list and enable PodBox Cider Bridge.')
    print('On first enable, paste the bridge secret from the 0600 secret file into the PodBox prompt.')
    print('The plugin remembers this PodBox secret in Cider origin storage; it is never an Apple credential.')
    print('If replacing a loaded plugin, reload the renderer manually after review to unload the previous code.')
    print('No enable flag, Cider configuration, playback, or process was changed by this script.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, default=Path.home()/'.config/sh.cider.genten')
    parser.add_argument('--secret-file', type=Path, default=default_secret_path())
    parser.add_argument('--port', type=int, default=10768)
    parser.add_argument('--assets', type=Path, help='Asset directory or active update.cup; default: profile OTA, then built-in assets')
    args = parser.parse_args()
    install(args.profile, args.secret_file, args.port, args.assets)

if __name__ == '__main__':
    main()
