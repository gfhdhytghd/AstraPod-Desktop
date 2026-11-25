# AstraPod Desktop

AstraPod Desktop is the Linux companion for [AstraPod iPod Classic firmware][firmware].
The desktop app browses the iPod library, imports music, and syncs Jellyfin media.
HerdRemote owns the PodLink USB session and connects the iPod's Agents screen,
remote music and input plugins to the computer. This repository also includes
the PodLink CLI and the Apple IR bridge. The Cider plugin that HerdRemote uses
by default lives in [AstraPod-Cider][cider].

## Install

Use Python 3.10 or later and a Linux desktop session. The GUI uses Qt 6.
Grant your user access to the player's serial port. Install libusb for PodLink
Shell and Apple IR, and FFmpeg (including `ffprobe`) for media import and
sync. [HerdRemote's guide][herdremote] covers herdr, T3, voice and keyboard tools.

```sh
git clone https://github.com/gfhdhytghd/AstraPod-Desktop.git
cd AstraPod-Desktop
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Run

From the checkout root:

```sh
.venv/bin/python podlink_gui.py           # --tray for the tray, --demo for fake data
.venv/bin/python herdremote/herdremote.py # headless alternative to the GUI
.venv/bin/python -m astrapod_plugins list
.venv/bin/python -m astrapod_plugins run  # standalone plugin host
```

Connect the iPod in PodLink (non-disk) mode. Open **System > Agents** on the
iPod for HerdRemote. The GUI supervises its own HerdRemote process. Use one
HerdRemote process or other PodLink serial client at a time. Run the standalone
plugin host with `ASTRAPOD_PLUGIN_HOST=off` in the GUI's environment.

For serial diagnostics, stop HerdRemote and run `.venv/bin/python podlink.py info`.
PodLink Shell uses a separate USB endpoint and can run while HerdRemote is
connected. See the [Apple IR setup][ir] and the [Cider bridge setup][cider]
for those optional integrations.

On LCD type 1 firmware, `podlink_shell.py` supports `lcd-timing` and
`lcd-te-sync` without opening a device menu. `lcd-pacing on|off|get` switches
the UI animation clock between the panel refresh and 60 fps on any LCD, and
`lcd-pacing status` prints the refresh period and the last animation's rate,
frames and timings. Run `lcd-diag` to save a report, then use `log-fetch` to
download it.
Use `ui-perf OUTPUT` to fetch an existing UI performance log.
See the [PodLink Shell guide][shell] for command arguments and paths.

For plugin launch, screen capture and device checks, see the
[iPod plugin smoke test](docs/plugin-smoke.md).

### Disk Mode for media transfers

Start with the iPod connected in PodLink mode. Enable **磁盘模式** on the
Jellyfin sync or direct import page to transfer through USB mass storage.
Jellyfin preview and the import duplicate check still use PodLink. The app
mounts the selected iPod's FAT volume with UDisks2, ejects it after the job,
then queues a Rockbox database update on the restored PodLink connection.
Keep `/Music/PodLink` inside the player's database scan roots. Disk Mode needs
`udisksctl`, `gdbus`, `lsblk` and HerdRemote from the same desktop checkout.

## User services

The supplied units expect the checkout and its `.venv` at
`~/.local/share/astrapod/desktop`. Clone or symlink the checkout there.
For another location, edit `WorkingDirectory` and `ExecStart` in the unit
you install under `~/.config/systemd/user/`.

| Unit | Use |
| --- | --- |
| [`herdremote.service`](herdremote/herdremote.service) | Standalone daemon for a headless setup. Use this when the GUI is absent. |
| [`astrapod-plugins.service`](astrapod_plugins/astrapod-plugins.service) | Standalone [input plugin host][plugins]. Use this with `ASTRAPOD_PLUGIN_HOST=off` in the GUI. |
| [`podbox-usbkey.service`](usb_remote_key/podbox-usbkey.service) | Standalone Apple IR bridge. Use this as an alternative to the `apple-ir` plugin. |

Copy the selected unit to `~/.config/systemd/user/`, run
`systemctl --user daemon-reload`, then `systemctl --user enable --now UNIT`.
For a desktop tray launcher, set `ExecStart` to the absolute path to
`.venv/bin/python` followed by the absolute path to `podlink_gui.py` and
`--tray`. Launch it from the graphical session.

## Tests

Install pytest in the venv. Use a short private runtime directory so socket
tests are isolated from the running daemon.

```sh
.venv/bin/python -m pip install pytest
export XDG_RUNTIME_DIR="$(mktemp -d -p /tmp)"
.venv/bin/python -m pytest -q herdremote/tests
.venv/bin/python -m pytest -q astrapod_plugins/tests usb_remote_key/tests tests/test_podlink.py
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/test_podlink_gui.py
```

Some HerdRemote tests use Node.js for the Cider peer and the system
`ffmpeg`/`ffprobe` executables for media fixtures. The Cider peer tests need
an AstraPod-Cider checkout: set `ASTRAPOD_CIDER_PLUGIN`, or place it at
`../AstraPod-Cider`. Without it they are skipped.

The [firmware contract probes](tests/firmware/README.md) require firmware
sources. Set `ASTRAPOD_FIRMWARE` to that checkout, or place it at
`../AstraPod` or `../podbox` relative to this repository.

## Protocols and license

The firmware repository documents [PodLink][podlink], [HerdRemote][protocol],
[PodLink Shell][shell] and the [Cider remote protocol][cider-protocol].
For host configuration, see [HerdRemote usage](docs/herdremote-usage.md)
and [IR host fallback](docs/ir-host-fallback.md).

AstraPod Desktop is licensed under GPL-2.0-or-later. See [LICENSE](LICENSE).

[firmware]: https://github.com/gfhdhytghd/AstraPod
[podlink]: https://github.com/gfhdhytghd/AstraPod/blob/master/docs/podlink.md
[protocol]: https://github.com/gfhdhytghd/AstraPod/blob/master/docs/podbox/herdremote-protocol.md
[shell]: https://github.com/gfhdhytghd/AstraPod/blob/master/docs/podbox/podlink-shell.md
[cider-protocol]: https://github.com/gfhdhytghd/AstraPod/blob/master/docs/podbox/cider-remote-protocol.md
[herdremote]: herdremote/README.md
[ir]: usb_remote_key/README.md
[plugins]: docs/input-plugins.md
[cider]: https://github.com/gfhdhytghd/AstraPod-Cider
